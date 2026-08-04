"""macOS Keychain backend for mem's variable store.

mem stores variable *values* — API tokens, database passwords, bastion
hostnames — in the login Keychain instead of in a plaintext file. The
Keychain encrypts them at rest under the user's login password and mediates
access through the OS, which a 0600 file cannot do: anything running as the
user (a backup, a synced folder, a malicious postinstall script, someone with
the unlocked disk) can read a 0600 file, and nothing warns you about it.

The whole backend is `/usr/bin/security`, driven with `subprocess`. That is a
hard requirement, not a preference: mem takes no new dependencies
(Constitution Principle I forbids anything that could drag a networking
library in), and `security` ships with every macOS install, so there is
nothing to install and nothing to trust beyond the OS itself.

Three details of `security` shape this module, all established by running it
rather than by reading the man page:

**1. The secret never goes in argv.**  ``security add-generic-password -w
<secret>`` is the documented form, and it is also the form that puts the
secret in the process table where any process on the machine can read it with
``ps``.  ``security``'s own help says so ("Use of the -p or -w options is
insecure"). Its suggested alternative — ``-w`` as the last option, which
prompts — needs a controlling terminal: with a pipe on stdin it exits 2, and
with ``-w`` omitted entirely it silently stores an *empty* password. So the
prompt is unusable from a program. What does work is ``security -i``, the
tool's interactive mode: it reads command lines from **stdin**, so the secret
travels down a pipe our own process owns and argv contains only
``["/usr/bin/security", "-i"]``.

**2. Values are handed over hex-encoded (``-X``).**  ``security -i`` splits
its input lines into words with shell-like quoting, so a value containing a
quote, a backslash or a newline could not be passed as text without inventing
a quoting scheme — and a quoting bug here means a silently corrupted secret.
``-X`` takes the password as hexadecimal, which is ``[0-9a-f]`` and therefore
immune to the tokenizer. The value is still stored as the real bytes: reading
the item back with ``security find-generic-password -w`` outside mem returns
the actual secret, so mem is not a lock-in layer over your own credentials.

**3. Reading back uses ``-g``, not ``-w``.**  ``-w`` prints the password raw
when it is printable and as bare lowercase hex when it is not — and those two
are indistinguishable, so a perfectly ordinary 32-character hex API key reads
back as 16 bytes of binary garbage. ``-g`` is unambiguous: it prints
``password: 0x<HEX>  "<escaped>"`` whenever the value contains anything
outside printable ASCII (or a backslash), and ``password: "<value>"`` when it
does not — and in that second case no escaping is possible, so stripping the
outer quotes is exact.

The one honest limitation: ``security -i`` truncates input lines at 4096
characters, and it does not fail cleanly when it does — it executes the
truncated head as a command. Observed directly: an 8 KB value produced a
Keychain item holding a silently truncated secret. Values are therefore
length-checked against the exact command line before it is sent, and a value
that would not fit is refused (:class:`KeychainValueTooLong`) rather than
half-stored. That caps a value at roughly 2 KB — ample for tokens and
passwords, not enough for a 4096-bit private key.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys

# The generic-password "service" every mem variable is filed under. Chosen to
# be unmistakably mem's own and greppable: searching Keychain Access for
# "mem-cli" finds exactly mem's items and nothing else.
SERVICE = "mem-cli-vars"

SECURITY_BIN = "/usr/bin/security"

# `security` exit code for errSecItemNotFound. It is the one non-zero status
# that is not an error: it means "no such variable", which callers handle.
_ITEM_NOT_FOUND = 44

# Ceiling on one `security -i` input line. The real limit is 4096 characters,
# past which the tool splits the line and runs the head as a command — which
# for us would mean storing a truncated secret. 3900 leaves room for the
# terminating newline and for any future option added to the command.
_MAX_COMMAND_LINE = 3900

# Every `security` call is bounded. The Keychain can put up an authorization
# dialog, and mem runs inside the user's shell prompt: without a timeout a
# dialog nobody is looking at would hang the terminal forever. 60s is long
# enough for a user who is actually there to click "Allow".
_TIMEOUT_SECONDS = 60

# Variable names and keychain paths are interpolated into the command line
# that `security -i` parses, so both are restricted to characters that cannot
# start a new word, quote, or command. Without this a variable named
# `A\ndelete-generic-password ...` would be a command injection into our own
# subprocess — the exact class of bug this module exists to avoid.
_SAFE_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")
_SAFE_PATH = re.compile(r"^[A-Za-z0-9_./-]+$")

# Prefix of the line `find-generic-password -g` writes to stderr.
_PASSWORD_PREFIX = "password: "

# Environment override naming the keychain to use. Unset — the normal case —
# means the user's default (login) keychain. It exists so the end-to-end test
# can point mem at a throwaway keychain instead of the developer's own, and it
# doubles as the escape hatch for anyone who keeps credentials in a separate
# keychain.
KEYCHAIN_ENV = "MEM_KEYCHAIN"


class KeychainError(RuntimeError):
    """A Keychain operation failed. The message is meant for the user."""


class KeychainUnavailable(KeychainError):
    """The Keychain cannot be reached at all.

    Not macOS, no `security` binary, a locked keychain, or a user who declined
    the access prompt. Separate from :class:`KeychainError` because it is the
    case where mem must refuse to store a secret rather than fall back to
    plaintext.
    """


class KeychainValueTooLong(KeychainError):
    """The value does not fit in one `security -i` command line."""


def unavailable_reason() -> str | None:
    """Why the Keychain cannot be used, or None if it looks usable.

    Deliberately cheap and syntactic: platform and binary only, no subprocess.
    A locked keychain or a declined authorization prompt cannot be detected
    without actually performing an operation, so those surface as a
    :class:`KeychainUnavailable` from the operation itself, carrying
    `security`'s own message. Guessing here would mean either a subprocess on
    every `mem run`, or a confident answer that is wrong.
    """
    if sys.platform != "darwin":
        return f"the macOS Keychain is not available on this platform ({sys.platform})"
    if not os.access(SECURITY_BIN, os.X_OK):
        return f"{SECURITY_BIN} is missing or not executable"
    return None


def is_available() -> bool:
    """Whether the Keychain backend can be attempted at all."""
    return unavailable_reason() is None


def label_for(name: str) -> str:
    """Keychain Access display name for a variable.

    The default label is the service name, which would show every mem variable
    as an identical row. Prefixing keeps them sorted together and still names
    the variable. No spaces: the label goes through the `security -i`
    tokenizer.
    """
    return f"{SERVICE}:{name}"


def _keychain_path() -> str | None:
    """The keychain file to operate on, or None for the user's default."""
    path = os.environ.get(KEYCHAIN_ENV)
    if not path:
        return None
    if not _SAFE_PATH.match(path):
        raise KeychainError(
            f"{KEYCHAIN_ENV} contains characters mem will not pass to "
            f"`security`: {path!r}"
        )
    return path


def _validate_name(name: str) -> None:
    """Reject a variable name that could alter the command `security` runs."""
    if not _SAFE_NAME.match(name):
        raise KeychainError(
            f"Refusing to use {name!r} as a Keychain account name: only "
            f"letters, digits, '_', '.' and '-' are allowed."
        )


def _run(argv: list[str], stdin: bytes = b"") -> tuple[int, bytes, bytes]:
    """Run `security` and return (returncode, stdout, stderr).

    The single point where this module touches the OS, so tests can replace it
    with an in-memory keychain and the default suite never goes near the
    developer's real one.
    """
    try:
        proc = subprocess.run(
            argv,
            input=stdin,
            capture_output=True,
            timeout=_TIMEOUT_SECONDS,
        )
    except FileNotFoundError as exc:  # security binary vanished mid-flight
        raise KeychainUnavailable(f"{SECURITY_BIN} is not available") from exc
    except subprocess.TimeoutExpired as exc:
        raise KeychainUnavailable(
            f"`security` did not answer within {_TIMEOUT_SECONDS}s — the "
            f"Keychain may be waiting for an authorization you did not see"
        ) from exc
    except OSError as exc:
        raise KeychainUnavailable(f"could not run {SECURITY_BIN}: {exc}") from exc
    return proc.returncode, proc.stdout, proc.stderr


def _fail(action: str, stderr: bytes) -> KeychainUnavailable:
    """Build the error for a failed `security` call, quoting its own message.

    `security`'s messages ("The user name or passphrase you entered is not
    correct", "User interaction is not allowed") say more about what went
    wrong than anything mem could infer, so they are passed through verbatim
    rather than replaced with a generic sentence.
    """
    detail = stderr.decode("utf-8", errors="replace").strip().splitlines()
    message = detail[0] if detail else "no output"
    return KeychainUnavailable(f"{action} failed: {message}")


def _interactive_line(command: str) -> None:
    """Send one command line to `security -i`, raising on failure.

    Length is checked here rather than by the caller because this is the only
    place that knows the final line, and a line over the limit is not a
    rejected command — it is a truncated one that `security` happily executes.
    """
    encoded = command.encode("utf-8")
    if len(encoded) > _MAX_COMMAND_LINE:
        raise KeychainValueTooLong(
            "value is too large for the Keychain: `security` truncates "
            f"commands longer than {_MAX_COMMAND_LINE} bytes, and this one "
            f"is {len(encoded)}. Store large secrets in a file and keep the "
            "path in the variable instead."
        )
    code, _out, err = _run([SECURITY_BIN, "-i"], stdin=encoded + b"\n")
    if code != 0:
        raise _fail("Keychain write", err)


def set_secret(name: str, value: str) -> None:
    """Store (or replace) a variable's value in the Keychain.

    ``-U`` updates an existing item instead of failing, which makes
    ``mem vars set`` on an existing name do what the user means. The value
    goes over stdin as hex; see the module docstring for why both halves of
    that matter.
    """
    _validate_name(name)
    payload = value.encode("utf-8")
    # `-X ""` is a parse error ("must specify valid hex digits"), so the empty
    # value — legitimate: a variable can be deliberately empty — takes the one
    # text form with no quoting risk at all.
    data = f"-X {payload.hex()}" if payload else '-w ""'
    parts = [
        "add-generic-password",
        "-U",
        "-s",
        SERVICE,
        "-a",
        name,
        "-l",
        label_for(name),
        data,
    ]
    path = _keychain_path()
    if path:
        parts.append(path)
    _interactive_line(" ".join(parts))


def get_secret(name: str) -> str | None:
    """Read a variable's value back, or None if the Keychain has no such item.

    Raises :class:`KeychainError` when the item exists but cannot be read —
    a locked keychain, a denied prompt, or output mem does not understand.
    Returning None there would be indistinguishable from "never stored", and
    mem would go on to prompt the user for a value it already has.
    """
    _validate_name(name)
    argv = [SECURITY_BIN, "find-generic-password", "-s", SERVICE, "-a", name, "-g"]
    path = _keychain_path()
    if path:
        argv.append(path)
    code, _out, err = _run(argv)
    if code == _ITEM_NOT_FOUND:
        return None
    if code != 0:
        raise _fail("Keychain read", err)
    return parse_password_output(err)


def delete_secret(name: str) -> bool:
    """Remove a variable from the Keychain. False if it was not there."""
    _validate_name(name)
    argv = [SECURITY_BIN, "delete-generic-password", "-s", SERVICE, "-a", name]
    path = _keychain_path()
    if path:
        argv.append(path)
    code, _out, err = _run(argv)
    if code == _ITEM_NOT_FOUND:
        return False
    if code != 0:
        raise _fail("Keychain delete", err)
    return True


def parse_password_output(stderr: bytes) -> str:
    """Extract the secret from `find-generic-password -g` output.

    Public so the exact format `security` emits can be pinned by tests against
    recorded real output — the parsing, not the subprocess, is where a wrong
    answer would silently corrupt a credential.

    Two forms, and which one appears is decided by the data, not by us:

    - ``password: 0x6C696E65310A  "line1\\012"`` when the value contains any
      byte outside printable ASCII, or a backslash. The hex is authoritative.
    - ``password: "inter secret"`` otherwise. Nothing inside can be escaped —
      a backslash would have forced the hex form — so the outer quotes come
      off and what remains is the value byte for byte, including any embedded
      double quotes (``a"b`` really does print as ``"a"b"``).

    An empty value prints as ``password: `` with nothing after it.
    """
    text = stderr.decode("utf-8", errors="replace")
    for line in text.splitlines():
        if not line.startswith(_PASSWORD_PREFIX):
            continue
        body = line[len(_PASSWORD_PREFIX) :]
        if body.startswith("0x"):
            digits = body[2:].split(" ", 1)[0]
            try:
                raw = bytes.fromhex(digits)
            except ValueError as exc:
                raise KeychainError(
                    f"Keychain returned password data mem cannot decode: {body!r}"
                ) from exc
            try:
                return raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise KeychainError(
                    "Keychain item is not valid UTF-8, so it cannot be used "
                    "as a variable value"
                ) from exc
        if body == "":
            return ""
        if len(body) >= 2 and body.startswith('"') and body.endswith('"'):
            return body[1:-1]
        raise KeychainError(
            f"Keychain returned password data in an unexpected format: {body!r}"
        )
    raise KeychainError("Keychain returned no password data")

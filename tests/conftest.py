"""Shared test fixtures for the mem test suite.

Three invariants are enforced here for *every* test, whether it asks for them
or not:

1. **No test may ever touch the developer's real ``~/.mem``.**  ``tmp_mem_dir``
   is ``autouse`` so isolation is the default rather than something each test
   has to remember to opt into, and ``$HOME`` itself is redirected so even code
   that calls ``Path.home()`` directly lands in a throwaway directory.

2. **No test may run real on-device inference.**  Apple Foundation Models
   inference costs seconds per call and its output is non-deterministic, so
   the unit suite pretends the SDK is unavailable unless a test explicitly
   opts in with ``@pytest.mark.ai`` (deselected by default via ``addopts``).

3. **No test may touch the developer's real Keychain.**  ``fake_keychain`` is
   ``autouse`` and replaces the single function in ``mem.keychain`` that
   spawns ``/usr/bin/security`` with an in-memory model of that binary. Tests
   marked ``keychain_live`` (deselected by default) keep the real one, and
   point it at a throwaway keychain file of their own.
"""

from __future__ import annotations

import shlex
import sys
import time
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock

import pytest

from mem import keychain
from mem import models
from mem import patterns
from mem import storage


def _ensure_apple_fm_importable() -> None:
    """Make ``apple_fm_sdk`` importable so ``mock.patch`` can target it.

    ``mem.patterns`` imports the SDK lazily, so on a machine without
    ``apple-fm-sdk`` installed a call like
    ``patch("apple_fm_sdk.LanguageModelSession")`` would raise ImportError at
    patch time and the AI tests would fail for the wrong reason.

    Only stub when the real package is genuinely missing. The previous version
    of this shim lived at the top of ``test_patterns.py`` and keyed off
    ``"apple_fm_sdk" not in sys.modules``, which was true even when the real
    SDK *was* installed (it is imported lazily). The result was a
    collection-order-dependent suite: running ``test_capture.py`` alone did
    ~1.5s of real inference, while running the whole suite silently replaced
    the SDK with a MagicMock. Deciding by importability instead of by
    ``sys.modules`` makes the behaviour deterministic.
    """
    try:
        import apple_fm_sdk  # noqa: F401
    except ImportError:
        stub = ModuleType("apple_fm_sdk")
        stub.LanguageModelSession = MagicMock  # type: ignore[attr-defined]
        stub.generable = lambda *a, **kw: lambda cls: cls  # type: ignore[attr-defined]
        stub.guide = lambda *a, **kw: None  # type: ignore[attr-defined]
        sys.modules["apple_fm_sdk"] = stub


_ensure_apple_fm_importable()


@pytest.fixture(autouse=True)
def tmp_mem_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect MEM_DIR *and* ``$HOME`` to temporary directories.

    Autouse on purpose: a test that forgets to request isolation would read
    and write the developer's real history. Making isolation opt-out rather
    than opt-in removes that entire failure mode.

    ``$HOME`` is redirected separately from ``MEM_DIR`` because
    ``storage.MEM_DIR`` is computed once at import time from ``Path.home()``;
    monkeypatching the module constant covers ``mem.storage``, but any code
    (or subprocess) resolving ``~`` on its own would still escape. Pointing
    ``$HOME`` at an empty directory closes that hole, and keeps the two paths
    distinct so an accidental ``Path.home() / ".mem"`` shows up as an empty
    directory instead of silently sharing state with MEM_DIR.
    """
    # Deliberately dot-prefixed and distinctive: `tmp_path` is shared with the
    # test body, and a plain "home" collides with fixtures that create their
    # own (test_e2e.py builds one for its subprocesses).
    fake_home = tmp_path / ".isolated-home"
    fake_home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("USERPROFILE", str(fake_home))

    monkeypatch.setattr(storage, "MEM_DIR", tmp_path)
    # Group constants are computed at import time from MEM_DIR, so patch them too
    monkeypatch.setattr(storage, "GROUPS_DIR", tmp_path / "groups")
    monkeypatch.setattr(storage, "GROUPS_REPOS_DIR", tmp_path / "groups" / "repos")
    monkeypatch.setattr(
        storage, "GROUPS_GLOBAL_FILE", tmp_path / "groups" / "_global.json"
    )
    monkeypatch.setattr(storage, "SYNC_COUNTER_FILE", tmp_path / ".sync_counter")
    monkeypatch.setattr(storage, "VARS_FILE", tmp_path / "vars.json")

    # `mem.picks` resolves its path per call and honours $MEM_DIR, because it
    # is imported by the interactive finder and cannot reach into
    # `mem.storage` (which would drag Pydantic into the fast path). Setting
    # the variable puts pick counters in the same throwaway directory as
    # everything else, instead of a second one nobody thinks to look in.
    monkeypatch.setenv("MEM_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture(autouse=True)
def _no_real_inference(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Force the "SDK unavailable" branch unless a test is marked ``ai``.

    Without this, any code path reaching ``patterns.generate_session_summary``
    or ``patterns._generalize_commands`` would run a real on-device model:
    seconds per call, non-deterministic text, and a suite whose runtime
    depends on whether the machine has Apple Intelligence enabled.

    Tests that exercise the AI code path re-patch ``_apple_fm_available`` to
    ``True`` themselves (with a mocked ``LanguageModelSession``), which shadows
    this fixture for the duration of their ``with`` block.
    """
    if "ai" in request.keywords:
        return
    monkeypatch.setattr(patterns, "_apple_fm_available", lambda: False)


class FakeKeychain:
    """An in-memory model of ``/usr/bin/security``, accurate where it matters.

    Substituted for :func:`mem.keychain._run`, so everything above it — the
    command line mem builds, the hex encoding, the parsing of the output — is
    exercised for real. Only the process boundary is faked.

    The fidelity that earns this its keep is :meth:`_render_password`: the
    output format of ``find-generic-password -g`` is reproduced from output
    recorded off the real binary, including the rule that decides between the
    quoted and the hexadecimal form. mem's decoder is only as correct as that
    rule, so guessing it here would make the round-trip tests agree with a
    fiction.
    """

    #: Real `security -i` truncates an input line here and executes the head,
    #: which is how a too-long value became a *silently truncated* Keychain
    #: item. mem must never hand it a line this long.
    MAX_LINE = 4096

    def __init__(self) -> None:
        self.items: dict[tuple[str, str], bytes] = {}
        #: Every (argv, stdin) pair, so tests can assert what reached the
        #: process table.
        self.calls: list[tuple[list[str], bytes]] = []
        #: When set, every operation fails with this message — a locked
        #: keychain, or a user who clicked Deny.
        self.failure: str | None = None

    # -- helpers ----------------------------------------------------------

    def secret(self, name: str, service: str = keychain.SERVICE) -> str | None:
        """The stored value for a variable, decoded, or None."""
        raw = self.items.get((service, name))
        return None if raw is None else raw.decode("utf-8")

    @staticmethod
    def _render_password(data: bytes) -> bytes:
        """Reproduce what ``find-generic-password -g`` writes to stderr."""
        if not data:
            return b"password: \n"
        # Verified against the real tool: a backslash forces the hex form even
        # though it is printable, while a double quote does not.
        printable = all(0x20 <= b <= 0x7E and b != 0x5C for b in data)
        if printable:
            return b'password: "' + data + b'"\n'
        escaped = bytearray()
        for byte in data:
            if 0x20 <= byte <= 0x7E and byte != 0x5C:
                escaped.append(byte)
            else:
                escaped += f"\\{byte:03o}".encode("ascii")
        return b"password: 0x" + data.hex().upper().encode() + b'  "' + escaped + b'"\n'

    @staticmethod
    def _not_found() -> tuple[int, bytes, bytes]:
        return (
            44,
            b"",
            b"security: SecKeychainSearchCopyNext: The specified item could "
            b"not be found in the keychain.\n",
        )

    # -- the fake binary --------------------------------------------------

    def run(self, argv: list[str], stdin: bytes = b"") -> tuple[int, bytes, bytes]:
        """Stand in for :func:`mem.keychain._run`."""
        self.calls.append((list(argv), stdin))
        if self.failure is not None:
            return 1, b"", f"security: {self.failure}\n".encode()

        if argv[1:] == ["-i"]:
            line = stdin.decode("utf-8").rstrip("\n")
            assert len(line) <= self.MAX_LINE, (
                "mem sent security a command line long enough to be truncated "
                "and half-executed"
            )
            return self._interactive(shlex.split(line))
        return self._command(argv[1:])

    def _interactive(self, tokens: list[str]) -> tuple[int, bytes, bytes]:
        assert tokens[0] == "add-generic-password", tokens
        opts = self._options(tokens[1:])
        service, account = opts["-s"], opts["-a"]
        if "-X" in opts:
            data = bytes.fromhex(opts["-X"])
        else:
            data = opts.get("-w", "").encode("utf-8")
        if (service, account) in self.items and "-U" not in opts:
            return 1, b"", b"security: The specified item already exists.\n"
        self.items[(service, account)] = data
        return 0, b"", b""

    def _command(self, tokens: list[str]) -> tuple[int, bytes, bytes]:
        verb, opts = tokens[0], self._options(tokens[1:])
        key = (opts["-s"], opts["-a"])
        if verb == "find-generic-password":
            if key not in self.items:
                return self._not_found()
            return 0, b'keychain: "fake"\n', self._render_password(self.items[key])
        if verb == "delete-generic-password":
            if key not in self.items:
                return self._not_found()
            del self.items[key]
            return 0, b"password has been deleted.\n", b""
        raise AssertionError(f"unexpected security command: {verb}")

    @staticmethod
    def _options(tokens: list[str]) -> dict[str, str]:
        """Parse `security`'s flags, keeping value-less flags like -U and -g."""
        opts: dict[str, str] = {}
        i = 0
        while i < len(tokens):
            token = tokens[i]
            if not token.startswith("-"):
                opts.setdefault("keychain", token)
                i += 1
            elif token in {"-U", "-g", "-w"} and (
                i + 1 >= len(tokens) or tokens[i + 1].startswith("-")
            ):
                opts[token] = ""
                i += 1
            elif token in {"-s", "-a", "-l", "-X", "-w"}:
                opts[token] = tokens[i + 1]
                i += 2
            else:
                opts[token] = ""
                i += 1
        return opts


@pytest.fixture(autouse=True)
def fake_keychain(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> FakeKeychain:
    """Keep ``/usr/bin/security`` away from the developer's login keychain.

    Autouse for the same reason ``tmp_mem_dir`` is: a test that forgot to ask
    for isolation would not fail, it would quietly write the fixture's fake
    tokens into the real Keychain and leave them there. The default suite must
    be runnable on a laptop without side effects.

    Tests marked ``keychain_live`` keep the real subprocess — they are
    deselected by default and supply their own throwaway keychain.
    """
    fake = FakeKeychain()
    if "keychain_live" not in request.keywords:
        monkeypatch.delenv(keychain.KEYCHAIN_ENV, raising=False)
        monkeypatch.setattr(keychain, "_run", fake.run)
    return fake


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """Create a real (empty) git repository in a temp dir and return its root.

    Used by repo-detection tests so they assert against a repository they
    created, instead of against whatever directory pytest happens to run in.
    """
    import subprocess

    root = tmp_path / "workrepo"
    root.mkdir()
    subprocess.run(
        ["git", "init", "-q"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    # macOS resolves /var -> /private/var, and `git rev-parse --show-toplevel`
    # returns the resolved path. Return the resolved form so tests can compare
    # with `==` instead of a fuzzy `endswith`.
    return root.resolve()


def make_command(
    command: str = "git status",
    ts: int | None = None,
    dir: str = "/Users/test/projects/myapp",
    repo: str | None = "/Users/test/projects/myapp",
    exit_code: int = 0,
    duration_ms: int = 50,
    session: str | None = None,
) -> models.CapturedCommand:
    """Factory for creating CapturedCommand instances in tests."""
    return models.CapturedCommand(
        command=command,
        ts=ts or int(time.time()),
        dir=dir,
        repo=repo,
        exit_code=exit_code,
        duration_ms=duration_ms,
        session=session,
    )

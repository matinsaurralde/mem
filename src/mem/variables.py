"""Variable parsing, resolution, and management for mem.

This module handles all variable logic:
- Detecting $VAR_NAME tokens in commands (excluding common shell variables)
- Processing $$VAR_NAME escape sequences (resolved at execution time)
- Merging detected variables with explicit --var declarations
- Resolving variables at runtime from multiple sources
- Checking variable resolution status for display

Variable names follow the convention [A-Z][A-Z0-9_]+ (uppercase, starting
with a letter, minimum 2 characters). Common shell variables ($HOME, $PATH, etc.) are excluded
from detection via a fixed built-in list.
"""

from __future__ import annotations

import os
import re
from typing import Any

import click

from mem.models import VarDeclaration

# Fixed built-in list of shell variables excluded from detection.
# These are universally present in shell environments and are never
# intended as mem variables. Users can escape with $$ for edge cases.
EXCLUDED_SHELL_VARS: set[str] = {
    "HOME",
    "USER",
    "PATH",
    "PWD",
    "OLDPWD",
    "SHELL",
    "TERM",
    "EDITOR",
    "VISUAL",
    "LANG",
    "LC_ALL",
    "DISPLAY",
    "HOSTNAME",
    "LOGNAME",
    "MAIL",
    "SHLVL",
    "TMPDIR",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_CACHE_HOME",
}

# Regex to match $VAR_NAME tokens (uppercase, starting with letter).
# Does NOT match $$ escapes, $(subshell), or $((arithmetic)).
_VAR_PATTERN = re.compile(r"(?<!\$)\$([A-Z][A-Z0-9_]+)")

# Regex to detect $$ escape sequences for replacement.
_ESCAPE_PATTERN = re.compile(r"\$\$([A-Z][A-Z0-9_]+)")


def parse_variables(cmd: str) -> list[str]:
    """Detect $VAR_NAME tokens in a command string.

    Returns a deduplicated list of variable names found in the command,
    excluding common shell variables from the built-in exclusion list.
    Order is preserved (first occurrence).
    """
    seen: set[str] = set()
    result: list[str] = []
    for match in _VAR_PATTERN.finditer(cmd):
        name = match.group(1)
        if name not in EXCLUDED_SHELL_VARS and name not in seen:
            seen.add(name)
            result.append(name)
    return result


def process_escapes(cmd: str) -> str:
    """Replace $$VAR_NAME escape sequences with $VAR_NAME.

    Allows users to preserve shell variables that should be expanded
    at runtime by the shell, not by mem. The doubled dollar sign is
    stripped to a single dollar sign in the stored command.
    """
    return _ESCAPE_PATTERN.sub(r"$\1", cmd)


def merge_var_declarations(
    detected: list[str],
    explicit: list[tuple[str, str | None]],
) -> list[VarDeclaration]:
    """Merge detected variable tokens with explicit --var declarations.

    Deduplicates by name. If a --var declaration names a variable that
    was also detected in the command text, the declaration enriches
    the existing variable with a default value (no duplicate created).

    Args:
        detected: Variable names found in command text via parse_variables().
        explicit: (name, default) pairs from --var flags.

    Returns:
        Merged list of VarDeclaration objects.
    """
    # Build a dict preserving detection order, then overlay explicit defaults
    var_map: dict[str, str | None] = {name: None for name in detected}

    for name, default in explicit:
        if name in var_map:
            # Enrich existing detection with default value
            var_map[name] = default
        else:
            # Add explicitly declared variable not found in command text
            var_map[name] = default

    return [
        VarDeclaration(name=name, default=default) for name, default in var_map.items()
    ]


def resolve_variables(
    var_list: list[VarDeclaration],
    inline_args: dict[str, str],
    stored_vars: dict[str, Any] | None = None,
    prompt_fn: Any = None,
    allow_prompt: bool = True,
) -> dict[str, tuple[str, str]]:
    """Resolve variables from multiple sources in priority order.

    Priority chain (highest to lowest):
    1. Inline arguments (VAR=VALUE passed to mem run)
    2. Shell environment (os.environ)
    3. Persistent store (~/.mem/vars.json)
    4. Default value (from --var NAME=default at save time)
    5. Interactive prompt (visible input)

    Args:
        var_list: Variable declarations from the saved command.
        inline_args: {name: value} from command-line VAR=VALUE pairs.
        stored_vars: dict from VarsFile.vars (StoredVariable objects with .value).
        prompt_fn: Optional callable for prompting (for testing). Defaults to click.prompt.
        allow_prompt: If False, skip interactive prompts (for --yes mode).

    Returns:
        {name: (value, source)} where source describes where the value came from.
    """
    if stored_vars is None:
        stored_vars = {}
    if prompt_fn is None:
        prompt_fn = click.prompt

    resolved: dict[str, tuple[str, str]] = {}

    # Collect all prompts upfront (FR-006)
    needs_prompt: list[VarDeclaration] = []

    for var in var_list:
        name = var.name

        # Priority 1: inline arguments
        if name in inline_args:
            resolved[name] = (inline_args[name], "arguments")
            continue

        # Priority 2: shell environment
        env_val = os.environ.get(name)
        if env_val is not None:
            resolved[name] = (env_val, "environment")
            continue

        # Priority 3: persistent store
        if name in stored_vars:
            resolved[name] = (stored_vars[name].value, "store")
            continue

        # Priority 4: default value (resolve immediately when prompts disabled)
        if var.default is not None:
            if not allow_prompt:
                resolved[name] = (var.default, "default")
                continue
            needs_prompt.append(var)
            continue

        # Priority 5: needs interactive prompt
        needs_prompt.append(var)

    # Prompt for all unresolved variables upfront.
    #
    # allow_prompt=False previously only short-circuited the *default* branch,
    # so a variable with no source and no default still reached prompt_fn —
    # which under `--yes` is click.prompt on a stdin nobody is watching. In CI
    # that is a hang, not an error. Callers that disable prompting are
    # responsible for reporting what is missing; here it simply stays unresolved.
    if not allow_prompt:
        return resolved

    for var in needs_prompt:
        value = prompt_fn(f"  ${var.name}", default=var.default or "")
        # If user accepted default, use default
        if value == "" and var.default is not None:
            value = var.default
        source = (
            "default" if var.default is not None and value == var.default else "prompt"
        )
        resolved[var.name] = (value, source)

    return resolved


def check_resolution_status(
    var_list: list[VarDeclaration],
    stored_vars: dict[str, Any] | None = None,
    group_name: str = "group",
) -> list[tuple[str, str, str]]:
    """Check resolution status of variables for display in listings.

    Returns [(name, status, hint), ...] where:
    - status is "resolved" or "unset"
    - hint describes the source or suggests how to provide the value.

    Only checks non-interactive sources (env, store, default).
    Does not check inline args (those are runtime-only).
    """
    if stored_vars is None:
        stored_vars = {}

    result: list[tuple[str, str, str]] = []

    for var in var_list:
        name = var.name

        # Check environment
        if name in os.environ:
            result.append((name, "resolved", "from environment"))
            continue

        # Check persistent store
        if name in stored_vars:
            result.append((name, "resolved", "from store"))
            continue

        # Check default
        if var.default is not None:
            result.append((name, "resolved", f"default: {var.default}"))
            continue

        # Unset
        result.append(
            (name, "unset", f"pass inline: mem run {group_name} {name}=<value>")
        )

    return result


# Minimum token length that could plausibly be a hardcoded credential.
# Real secrets (JWTs, API keys, base64 strings) are almost always 16+ chars.
_MIN_CREDENTIAL_TOKEN_LEN = 16


def _command_may_contain_credentials(cmd: str) -> bool:
    """Quick heuristic to skip AI detection on simple commands.

    Returns True only if the command plausibly contains hardcoded secrets.
    Avoids sending trivial commands (echo, ls, cd) to the on-device model.
    """
    import shlex

    # Known credential-context keywords — if present, always check
    _CREDENTIAL_KEYWORDS = {
        "bearer",
        "authorization",
        "password",
        "token",
        "secret",
        "api_key",
        "apikey",
        "private_key",
        "access_key",
    }
    lower = cmd.lower()
    if any(kw in lower for kw in _CREDENTIAL_KEYWORDS):
        return True

    # Check for any token long enough to be a credential
    try:
        tokens = shlex.split(cmd)
    except ValueError:
        tokens = cmd.split()
    return any(len(t.strip("'\"")) >= _MIN_CREDENTIAL_TOKEN_LEN for t in tokens)


# Deterministic credential shapes, used where per-command AI inference is not
# an option. Each entry is a shape that is a credential essentially whenever it
# appears — a keyword assigned a non-trivial value, a bearer token, a vendor
# key with its documented prefix, or a password embedded in a connection URL.
_CREDENTIAL_SHAPES: tuple[re.Pattern[str], ...] = (
    # KEY=value / key: value where the key names a secret.
    re.compile(
        r"(?i)\b(?:pass|passwd|password|token|secret|api[_-]?key|apikey"
        r"|access[_-]?key|secret[_-]?key|private[_-]?key|auth[_-]?token"
        r"|client[_-]?secret|credential)\b\s*[=:]\s*[\"']?[^\s\"']{6,}"
    ),
    # Long-form flag with the value separated by a space: --password hunter2.
    re.compile(
        r"(?i)--(?:pass|password|token|secret|api-?key|access-?key|auth)"
        r"\s+[\"']?[^\s\"']{6,}"
    ),
    # Authorization headers, with or without the header name.
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{16,}"),
    # Vendor tokens, identified by their documented prefixes.
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\bglpat-[A-Za-z0-9_-]{16,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{20,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    # user:password@host in a connection string.
    re.compile(r"://[^\s/:@]+:[^\s/@]{4,}@"),
)


def looks_like_credential(cmd: str) -> bool:
    """Report whether a command plausibly carries a secret, without any AI.

    Why this exists next to :func:`detect_credentials`: that function is the
    accurate one, but it costs one on-device inference call per command. That
    is the right trade when the user is saving a single command, and the wrong
    one for bulk work — a shell history import scans tens of thousands of
    lines, where per-command inference would take hours and would also run on
    a machine that may not have the SDK installed at all, silently detecting
    nothing.

    Deliberately biased towards false positives: a skipped command costs the
    user one line of history they can re-add by hand, while a missed one
    writes their production token into ~/.mem forever.
    """
    return any(shape.search(cmd) for shape in _CREDENTIAL_SHAPES)


def _normalize_var_name(name: str) -> str:
    """Normalize a suggested variable name to UPPER_SNAKE_CASE.

    Handles CamelCase, mixed-case, and invalid characters from AI output.
    """
    # Convert CamelCase to UPPER_SNAKE
    result = re.sub(r"(?<=[a-z])(?=[A-Z])", "_", name)
    result = result.upper()
    # Strip invalid characters
    result = re.sub(r"[^A-Z0-9_]", "_", result)
    # Collapse multiple underscores
    result = re.sub(r"_+", "_", result)
    return result.strip("_")


def _extract_value_from_syntax(original_value: str, cmd: str) -> str:
    """Extract the actual secret value from flag=value or KEY=value syntax.

    The AI sometimes returns 'GITHUB_TOKEN=ghp_abc...' or '--password=secret'
    as the original_value. We need just the secret part so that .replace()
    doesn't destroy the command structure.
    """
    # Pattern: --flag=value or -f=value (CLI flag syntax)
    flag_match = re.match(r"^--?[a-zA-Z][\w-]*=(.+)$", original_value)
    if flag_match:
        value = flag_match.group(1)
        if value in cmd:
            return value

    # Pattern: ENV_VAR=value (environment variable assignment)
    env_match = re.match(r"^[A-Z][A-Z0-9_]+=(.+)$", original_value)
    if env_match:
        value = env_match.group(1)
        if value in cmd:
            return value

    # Pattern: AnyIdentifier=value — mixed-case option assignments such as
    # `ssh -o StrictHostKeyChecking=no`. Without this the whole expression was
    # treated as the secret, and at 24 characters it cleared the minimum-length
    # filter and got offered as DB_PASSWORD. Reducing it to the right-hand side
    # lets the existing length check reject `no` on its own merits, which fixes
    # the class rather than blacklisting one option name.
    generic_match = re.match(r"^[A-Za-z][\w.-]*=(.*)$", original_value)
    if generic_match:
        value = generic_match.group(1)
        if value and value in cmd:
            return value

    return original_value


def _looks_like_hostname(value: str) -> bool:
    """Check if a value looks like a hostname or domain, not a credential."""
    # Hostnames: words joined by dots, no special chars typical of secrets
    if re.match(r"^[a-zA-Z0-9]([a-zA-Z0-9-]*\.)+[a-zA-Z]{2,}$", value):
        return True
    # IP addresses
    if re.match(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$", value):
        return True
    return False


def _deduplicate_detections(
    detections: list[tuple[str, str, str]],
    cmd: str,
) -> list[tuple[str, str, str]]:
    """Filter out invalid and duplicate credential detections.

    Removes:
    - Values not actually present in the command (AI hallucinations)
    - URLs and hostnames (not credentials)
    - Very short values (< 8 chars, not plausible secrets)
    - Duplicate original_value entries
    - Values that are substrings of other detected values

    Also extracts the actual secret from flag=value syntax when the AI
    returns the whole expression as original_value.
    """
    result: list[tuple[str, str, str]] = []
    seen_values: set[str] = set()

    for original_value, suggested_name, reason in detections:
        # Extract actual value from flag=value or ENV=value syntax
        value = _extract_value_from_syntax(original_value, cmd)

        # Skip hallucinated values not in the command
        if value not in cmd:
            continue
        # Skip URLs
        if value.startswith(("http://", "https://")):
            continue
        # Skip hostnames and IP addresses
        if _looks_like_hostname(value):
            continue
        # Skip very short values
        if len(value) < 8:
            continue
        # Skip duplicates
        if value in seen_values:
            continue
        # Normalize suggested name to UPPER_SNAKE_CASE
        normalized = _normalize_var_name(suggested_name)
        if not re.match(r"^[A-Z][A-Z0-9_]+$", normalized):
            continue

        seen_values.add(value)
        result.append((value, normalized, reason))

    # Remove values that are substrings of other detected values
    filtered: list[tuple[str, str, str]] = []
    for i, (val_i, name_i, reason_i) in enumerate(result):
        is_subset = any(
            val_i in val_j and val_i != val_j
            for j, (val_j, _, _) in enumerate(result)
            if i != j
        )
        if not is_subset:
            filtered.append((val_i, name_i, reason_i))

    return filtered


def _apple_fm_available() -> bool:
    """Check if Apple Foundation Models SDK is available."""
    try:
        import apple_fm_sdk  # noqa: F401

        return True
    except ImportError:
        return False


async def _detect_credentials_async(cmd: str) -> list[tuple[str, str, str]]:
    """Use Apple FM to detect credentials in a command.

    Returns [(original_value, suggested_name, reason), ...].
    """
    import apple_fm_sdk as fm

    from mem._generable import CredentialList

    session = fm.LanguageModelSession()
    prompt = (
        "Analyze this shell command and find ONLY hardcoded sensitive values "
        "that should be replaced with environment variables.\n\n"
        "RULES FOR original_value:\n"
        "- Return ONLY the literal secret value, not the flag or key name.\n"
        "- For --password=mysecret, return 'mysecret' not '--password=mysecret'.\n"
        "- For GITHUB_TOKEN=ghp_abc, return 'ghp_abc' not 'GITHUB_TOKEN=ghp_abc'.\n"
        "- For postgres://user:pass@host, return 'pass' not the full URL.\n\n"
        "RULES FOR suggested_name:\n"
        "- Derive the name from the service or tool in the command.\n"
        "- Examples: STRIPE_API_KEY, GITHUB_TOKEN, REDIS_PASSWORD, DB_PASSWORD.\n"
        "- NEVER use generic prefixes like ACME_.\n\n"
        "ONLY flag these types of values:\n"
        "- API tokens or keys (long alphanumeric/base64 strings)\n"
        "- Passwords passed inline (after --password, -p, -a, etc.)\n"
        "- Bearer tokens in Authorization headers\n"
        "- Embedded passwords in connection strings\n\n"
        "DO NOT flag:\n"
        "- URLs, hostnames, IP addresses, or domain names\n"
        "- The command name, subcommands, or usernames\n"
        "- Short strings, regular arguments, or file paths\n"
        "- Port numbers, known constants, or registry addresses\n\n"
        "If NO credentials found, return an EMPTY list.\n\n"
        f"Command: {cmd}"
    )

    result = await session.respond(prompt, generating=CredentialList)
    return [(c.original_value, c.suggested_name, c.reason) for c in result.credentials]


def detect_credentials(cmd: str) -> list[tuple[str, str, str]]:
    """Detect potential credentials in a command via Apple FM.

    Returns [(original_value, suggested_name, reason), ...].
    Returns empty list if SDK is unavailable or detection fails.
    """
    if not _apple_fm_available():
        return []

    # Pre-filter: skip commands unlikely to contain credentials
    if not _command_may_contain_credentials(cmd):
        return []

    try:
        import asyncio

        raw = asyncio.run(_detect_credentials_async(cmd))
        return _deduplicate_detections(raw, cmd)
    except Exception:
        return []


# --- Deterministic redaction -------------------------------------------------
#
# Why this exists alongside detect_credentials(): they answer different
# questions under different constraints.
#
# detect_credentials() is an *interactive suggestion*. It runs Apple FM, so it
# needs the optional `ai` extra, costs seconds, is non-deterministic, and is
# allowed to be wrong in both directions because a human confirms every hit at
# the prompt. It exists to help you turn a secret you already typed into a
# named variable.
#
# redact_secrets() is a *boundary*. It runs on every string that leaves the
# machine's owner and reaches an AI agent over MCP. It therefore has to work
# with no optional dependency, in microseconds, identically on every run, and
# with nobody watching. Those requirements rule out an AI detector entirely, so
# this is a fixed rule set — pattern-based, deliberately conservative, and
# documented for exactly what it does and does not catch.
#
# What it does NOT do, on purpose: there is no generic "long high-entropy
# string" rule. Shell history is full of git SHAs, base64 file contents, UUIDs,
# checksums and container digests; a length-or-entropy heuristic would redact
# those and turn a search result into noise. Secrets are caught by the shape of
# the *context* they appear in (a known key prefix, a credential-ish name, an
# auth header, a URL userinfo field), not by looking secret.

REDACTED = "[REDACTED]"

# A name that, when it appears on the left of an assignment or as a flag,
# means the value to its right is a credential. The leading `[A-Za-z0-9_.-]*`
# lets a prefix through (PGPASSWORD, AWS_SECRET_ACCESS_KEY, --api-key) while
# still requiring the name to *end* at one of the alternatives — so BYPASSED,
# PASSTHROUGH and KEYBOARD do not match.
_SECRET_WORD = (
    r"(?:passwords?|passwd|passphrase|secrets?|tokens?|api[-_]?keys?|"
    r"access[-_]?key|secret[-_]?key|private[-_]?key|credentials?|"
    r"auth[-_]?token|mysql_pwd)"
)
_SECRET_NAME = rf"[A-Za-z0-9_.-]*{_SECRET_WORD}"

# The same idea, but requiring a prefix: `aws_secret_access_key`, not a bare
# `secret`. Used only by the whitespace-separated rule, where a bare word is
# far too weak a signal — `kubectl get secret my-app-secret` would lose the
# resource name, and `grep token /var/log/app.log` would lose the path.
_QUALIFIED_SECRET_NAME = rf"[A-Za-z0-9_.-]*[_.-]{_SECRET_WORD}"

# A value that is a variable reference, not a secret. `mem run` deliberately
# stores `$API_TOKEN` rather than the token, and redacting the placeholder
# would destroy the one piece of information the agent actually needs: which
# variable the runbook expects.
_PLACEHOLDER = re.compile(r"^\$\$?\{?[A-Za-z_][A-Za-z0-9_]*\}?$")

_QUOTED_OR_BARE = r"\"[^\"]*\"|'[^']*'|\S+"

# For whitespace-separated forms only: a value that starts with a dash is the
# next flag, not the secret (`--password --verbose` means "prompt me").
_QUOTED_OR_BARE_VALUE = r"\"[^\"]*\"|'[^']*'|[^-\s]\S*"


def _is_placeholder(value: str) -> bool:
    """True if a captured value is a shell/mem variable reference."""
    return bool(_PLACEHOLDER.match(value.strip("\"'")))


def _redact_tail(match: re.Match[str]) -> str:
    """Replace the last group of a match with the redaction marker.

    Used by the assignment and flag rules, which capture the name and the
    separator so they can be preserved: `--token=[REDACTED]` still tells the
    reader (and the agent) what kind of value belonged there.
    """
    value = match.group(match.re.groups)
    if _is_placeholder(value):
        return match.group(0)
    prefix = match.group(0)[: match.start(match.re.groups) - match.start(0)]
    return prefix + REDACTED


def _redact_userinfo(match: re.Match[str]) -> str:
    """Redact the password half of a `user:pass` pair, keeping the user.

    Keeping the username is deliberate: it is rarely the secret and it is
    often the part that makes a history entry recognisable. Purely numeric
    right-hand sides are left alone because `-u 1000:1000` (docker) and
    `:8080` are far more common in a shell history than a numeric password.

    Group 4, when the pattern has one, is the trailing `@` of a URL. It has
    to be put back explicitly — an earlier version dropped it and turned
    ``postgres://admin:pw@db/app`` into a URL with no host separator.
    """
    prefix, user, secret = match.group(1), match.group(2), match.group(3)
    suffix = match.group(4) if match.re.groups >= 4 else ""
    if secret.isdigit() or _is_placeholder(secret):
        return match.group(0)
    return f"{prefix}{user}:{REDACTED}{suffix}"


# Ordered on purpose. Context rules (an auth header, a URL userinfo field, a
# credential-ish name) run before the shape rules (vendor prefixes, JWTs) so
# that a token inside a header is redacted *as* the header's value and the
# surrounding words survive. Running them the other way round produced
# `Authorization: [REDACTED] [REDACTED]`, because the shape rule replaced the
# token and the context rule then redacted the scheme word left behind.
_REDACTIONS: list[tuple[re.Pattern[str], object]] = [
    # 1. PEM private key blobs, complete or truncated. `[^-]*` matches the
    #    algorithm word ("RSA ", " OPENSSH ") without escaping the dashes.
    (
        re.compile(
            r"-----BEGIN[^-]*PRIVATE KEY-----[\s\S]*?-----END[^-]*PRIVATE KEY-----"
        ),
        REDACTED,
    ),
    (re.compile(r"-----BEGIN[^-]*PRIVATE KEY-----[\s\S]*"), REDACTED),
    # 2. Authorization headers. The scheme word is optional and captured so it
    #    is preserved: `Authorization: Bearer [REDACTED]` still tells a reader
    #    which auth mechanism the command used.
    (
        re.compile(
            r"(?i)(authorization\s*:\s*)((?:bearer|basic|token)\s+)?"
            r"([^\"'\s]+)"
        ),
        _redact_tail,
    ),
    # 3. A bare `Bearer <token>` outside a named header.
    (
        re.compile(r"(?i)\b(bearer|basic)(\s+)([A-Za-z0-9._~+/=-]{8,})"),
        _redact_tail,
    ),
    # 4. Credentials embedded in a URL: scheme://user:pass@host.
    (re.compile(r"(://)([^/\s:@]+):([^/\s@]+)(@)"), _redact_userinfo),
    # 5. `curl -u user:pass` / `--user user:pass`.
    (re.compile(r"(?i)((?:^|\s)-u\s+|--user[= ])([^\s:]+):(\S+)"), _redact_userinfo),
    # 6. NAME=value assignments — covers `PGPASSWORD=…`, `--token=…`,
    #    `export STRIPE_SECRET_KEY=…` and every line of a leaked .env.
    (
        re.compile(rf"(?i)\b({_SECRET_NAME})(\s*=\s*)({_QUOTED_OR_BARE})"),
        _redact_tail,
    ),
    # 7. `--password hunter2` — the space-separated long-flag form.
    (
        re.compile(rf"(?i)(--{_SECRET_NAME}\s+)({_QUOTED_OR_BARE_VALUE})"),
        _redact_tail,
    ),
    # 8. `aws configure set aws_secret_access_key wJalr…` — a qualified name
    #    followed by its value with no flag syntax at all. The weakest signal
    #    in the table, so it is fenced in from three sides: the name must be
    #    qualified (see _QUALIFIED_SECRET_NAME), it must not be a `$VAR`
    #    reference (`curl -u $USER:$DEPLOY_TOKEN https://…` would otherwise
    #    swallow the URL that follows), and the value must be neither a path
    #    nor a URL, so `grep api_key ./src/config.py` survives intact.
    (
        re.compile(
            rf"(?i)(?<![\w./$-])({_QUALIFIED_SECRET_NAME})(\s+)"
            rf"(?!https?://)(\"[^\"]*\"|'[^']*'|[^-/~.\s]\S{{7,}})"
        ),
        _redact_tail,
    ),
    # 9. Vendor tokens with a documented prefix, anywhere they appear. The
    #    prefix exists precisely so that scanners can recognise them.
    (
        re.compile(
            r"\b(?:"
            r"gh[pousr]_[A-Za-z0-9]{16,}"
            r"|github_pat_[A-Za-z0-9_]{20,}"
            r"|glpat-[A-Za-z0-9_-]{16,}"
            r"|xox[abposr]-[A-Za-z0-9-]{10,}"
            r"|sk-ant-[A-Za-z0-9_-]{16,}"
            r"|sk-[A-Za-z0-9]{20,}"
            r"|[sr]k_(?:live|test)_[A-Za-z0-9]{10,}"
            r"|AIza[0-9A-Za-z_-]{35}"
            r"|npm_[A-Za-z0-9]{36}"
            r"|(?:A3T[A-Z0-9]|AKIA|ASIA|ABIA|ACCA)[A-Z0-9]{16}"
            r")\b"
        ),
        REDACTED,
    ),
    # 10. JWTs — three base64url segments, the first always starting `eyJ`
    #     because that is a base64-encoded `{"`.
    (
        re.compile(r"\beyJ[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}"),
        REDACTED,
    ),
]


def redact_secrets(text: str) -> str:
    """Remove credential-shaped substrings from *text*.

    This is the single choke point for anything mem hands to an AI agent.
    It is idempotent — running it on already-redacted text is a no-op — so
    callers may apply it defensively without mangling output.

    Known gaps, stated rather than hidden:

    - A bare positional secret with no context (``deploy.sh a1b2c3d4e5f6``)
      is indistinguishable from an ordinary argument and is not redacted.
    - Attached short flags (``mysql -phunter2``, ``redis-cli -a pw``) are not
      redacted: ``-p`` collides with ``mkdir -p``, ``cp -p`` and ``docker -p``,
      and mangling those was judged worse than missing a password that the
      shell already exposes in ``ps``.
    - Custom in-house token formats with no recognisable prefix are missed.

    The honest summary: this removes the credential shapes that are known and
    common. It is not a guarantee that no secret can pass, which is why agent
    access is opt-in and audited rather than merely filtered.
    """
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)  # type: ignore[arg-type]
    return text

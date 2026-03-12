"""Variable parsing, resolution, and management for mem.

This module handles all variable logic:
- Detecting $VAR_NAME tokens in commands (excluding common shell variables)
- Processing $$VAR_NAME escape sequences
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

    # Prompt for all unresolved variables upfront
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


def substitute_variables(
    cmd: str,
    resolved: dict[str, tuple[str, str]],
) -> str:
    """Replace $VAR_NAME tokens with resolved values.

    Performs literal string replacement — no shell expansion,
    no nested variable references.
    """
    result = cmd
    # Replace longer names first to avoid prefix collisions:
    # without sorting, replacing $API before $API_KEY would corrupt
    # "$API_KEY" into "resolved_value_KEY".
    for name, (value, _source) in sorted(
        resolved.items(), key=lambda item: len(item[0]), reverse=True
    ):
        result = result.replace(f"${name}", value)
    return result


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

    @fm.generable("Detected credential in a shell command")
    class CredentialDetection:
        original_value: str = fm.guide(
            "The literal sensitive value found in the command"
        )
        suggested_name: str = fm.guide(
            "A descriptive variable name like ACME_API_TOKEN"
        )
        reason: str = fm.guide(
            "Why this looks like a credential (e.g., 'JWT token', 'API key')"
        )

    session = fm.LanguageModelSession()
    prompt = (
        "Analyze this shell command and identify any sensitive values "
        "(API tokens, passwords, secrets, keys, long base64 strings). "
        "For each one, suggest a descriptive variable name based on the "
        "context of the command.\n\n"
        f"Command: {cmd}"
    )

    result = await session.respond(prompt, generating=CredentialDetection)
    if isinstance(result, list):
        return [(c.original_value, c.suggested_name, c.reason) for c in result]
    return [(result.original_value, result.suggested_name, result.reason)]


def detect_credentials(cmd: str) -> list[tuple[str, str, str]]:
    """Detect potential credentials in a command via Apple FM.

    Returns [(original_value, suggested_name, reason), ...].
    Returns empty list if SDK is unavailable or detection fails.
    """
    if not _apple_fm_available():
        return []

    try:
        import asyncio

        return asyncio.run(_detect_credentials_async(cmd))
    except Exception:
        return []

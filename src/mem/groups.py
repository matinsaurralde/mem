"""Business logic for groups and saved commands — mem's active memory.

Active memory is what the user explicitly decided to keep, as opposed to
passive memory (auto-captured shell history). Two concepts live here:

- **Saved commands**: flat bookmarks, not executable via ``mem run``
- **Named groups**: ordered, annotated, executable runbooks

Both are scoped per git repository or globally, stored as plain JSON
in ~/.mem/groups/. This module owns scope resolution, validation,
duplicate detection, shadow detection, and import/export formatting.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

import click

from mem import storage
from mem.capture import get_git_repo
from mem.models import Group, GroupCommand, GroupFile, SavedCommand, VarDeclaration
from mem.variables import merge_var_declarations, parse_variables, process_escapes

GROUP_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9-]*$")


def resolve_scope(global_flag: bool) -> Path:
    """Return the path to the data file for the target scope.

    Uses the current git repo for scoping unless --global is set.
    Falls back to global scope when outside a git repo.
    """
    if global_flag:
        return storage.GROUPS_GLOBAL_FILE

    repo = get_git_repo(os.getcwd())
    if repo is None:
        return storage.GROUPS_GLOBAL_FILE
    sanitized = storage.sanitize_repo_name(repo)
    return storage.group_file_path(sanitized)


def validate_group_name(name: str) -> None:
    """Raise click.BadParameter if the group name is invalid.

    Group names must start with a lowercase letter and contain
    only lowercase letters, digits, and hyphens.
    """
    if not GROUP_NAME_PATTERN.match(name):
        raise click.BadParameter(
            f"Invalid group name '{name}'. "
            "Use lowercase letters, numbers, and hyphens (e.g., 'my-group').",
            param_hint="'name'",
        )


def detect_shadows(repo_data: GroupFile, global_data: GroupFile) -> set[str]:
    """Return group names that exist in both repo and global scopes.

    Shadowed global groups are still accessible via --global, but
    the repo group takes precedence by default.
    """
    return set(repo_data.groups.keys()) & set(global_data.groups.keys())


def save_command(
    scope_path: Path,
    cmd: str,
    comment: str | None = None,
    group_name: str | None = None,
    description_callback: Any = None,
    explicit_vars: list[tuple[str, str | None]] | None = None,
) -> tuple[bool, list[VarDeclaration]]:
    """Save a command to the saved list or a named group.

    Returns True if saved, False if duplicate (same cmd string).
    Creates the group if it doesn't exist, using description_callback
    to prompt the user for an optional group description.
    """
    data = _load_group_file(scope_path)

    # Detect $VAR_NAME tokens BEFORE escape processing — the regex
    # negative lookbehind (?<!\$) correctly skips $$VAR in the original text.
    # If we escaped first, $$VAR would become $VAR and be falsely detected.
    detected_names = parse_variables(cmd)

    # Process escape sequences ($$VAR -> $VAR in stored text)
    stored_cmd = process_escapes(cmd)

    # Merge detected vars with explicit --var declarations
    var_list = merge_var_declarations(detected_names, explicit_vars or [])
    vars_field = var_list if var_list else None

    if group_name is None:
        # Save to the flat saved list
        if any(s.cmd == stored_cmd for s in data.saved):
            return False, []
        data.saved.append(
            SavedCommand(cmd=stored_cmd, comment=comment, vars=vars_field)
        )
    else:
        validate_group_name(group_name)
        if group_name not in data.groups:
            desc = None
            if description_callback:
                desc = description_callback(group_name)
            data.groups[group_name] = Group(description=desc, commands=[])

        group = data.groups[group_name]
        if any(c.cmd == stored_cmd for c in group.commands):
            return False, []
        group.commands.append(
            GroupCommand(cmd=stored_cmd, comment=comment, vars=vars_field)
        )

    storage.write_group_file(scope_path, data)
    return True, var_list


def get_last_captured_command(repo: str | None) -> str:
    """Read the last command from the repo's JSONL history file.

    Used by ``mem save !`` to grab the most recently captured command
    without the user having to retype it.
    """
    sanitized = storage.sanitize_repo_name(repo) if repo else "_global"
    path = storage.repo_file(sanitized)

    if not path.exists():
        raise click.ClickException(
            "No captured history found. Run some commands first."
        )

    last_line = None
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            if stripped:
                last_line = stripped

    if last_line is None:
        raise click.ClickException(
            "No captured history found. Run some commands first."
        )

    try:
        data = json.loads(last_line)
        return data["command"]
    except (json.JSONDecodeError, KeyError):
        raise click.ClickException("Could not read last command from history.")


def list_all(repo_path: Path | None, global_path: Path) -> dict:
    """Load both scopes and return structured data for display.

    Returns a dict with repo_data, global_data, shadows set,
    and repo_name extracted from the file path.
    """
    global_data = _load_group_file(global_path)
    repo_data = None
    shadows: set[str] = set()
    repo_name = None

    if repo_path is not None:
        repo_data = _load_group_file(repo_path)
        shadows = detect_shadows(repo_data, global_data)
        repo_name = repo_path.stem

    return {
        "repo_data": repo_data,
        "global_data": global_data,
        "shadows": shadows,
        "repo_name": repo_name,
    }


def resolve_group(
    name: str,
    repo_path: Path | None,
    global_path: Path,
    force_global: bool = False,
) -> tuple[Group, str, Path, set[str]]:
    """Resolve a group by name, checking repo scope first then global.

    Returns (group, scope_label, file_path, shadows).
    Repo scope takes precedence unless force_global is True.
    """
    global_data = _load_group_file(global_path)
    shadows: set[str] = set()

    if force_global:
        if name not in global_data.groups:
            raise click.ClickException(f"Group '{name}' not found in global scope.")
        return global_data.groups[name], "global", global_path, shadows

    if repo_path is not None:
        repo_data = _load_group_file(repo_path)
        shadows = detect_shadows(repo_data, global_data)

        if name in repo_data.groups:
            repo_name = repo_path.stem
            return repo_data.groups[name], repo_name, repo_path, shadows

    if name in global_data.groups:
        return global_data.groups[name], "global", global_path, shadows

    raise click.ClickException(f"Group '{name}' not found.")


def export_markdown(group_name: str, group: Group) -> str:
    """Export a group as a markdown document with a command table.

    Output is designed to render correctly in GitHub READMEs
    and can be piped to a file for documentation.
    """
    lines = [f"## {group_name}"]
    if group.description:
        lines.append(f"> {group.description}")
    lines.append("")
    lines.append("| Command | Description |")
    lines.append("|---|---|")
    for cmd in group.commands:
        comment = cmd.comment or ""
        lines.append(f"| `{cmd.cmd}` | {comment} |")
    return "\n".join(lines) + "\n"


def export_json(group_name: str, group: Group) -> str:
    """Export a group as formatted JSON for sharing or backup."""
    return json.dumps(
        {group_name: group.model_dump()},
        indent=2,
        ensure_ascii=False,
    )


def import_from_json(file_path: Path) -> list[GroupCommand]:
    """Parse a JSON file and extract commands.

    Accepts two formats: the export format ({"name": {"commands": [...]}})
    or a flat commands list ({"commands": [...]}).
    """
    try:
        data = json.loads(file_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise click.ClickException(f"Invalid JSON in {file_path}: {e}")

    if not isinstance(data, dict):
        raise click.ClickException("Expected a JSON object.")

    # Handle {"commands": [...]} format
    if "commands" in data:
        cmds = data["commands"]
    else:
        # Handle {"group_name": {"commands": [...]}} export format
        first_value = next(iter(data.values()), {})
        if isinstance(first_value, dict):
            cmds = first_value.get("commands", [])
        else:
            raise click.ClickException("Cannot parse group structure from JSON.")

    if not isinstance(cmds, list):
        raise click.ClickException("Expected 'commands' to be a list.")

    try:
        return [GroupCommand(cmd=c["cmd"], comment=c.get("comment")) for c in cmds]
    except (KeyError, TypeError) as e:
        raise click.ClickException(f"Malformed command entry in {file_path}: {e}")


def import_from_markdown(file_path: Path) -> list[GroupCommand]:
    """Parse a markdown file and extract commands from a table.

    Expects a table with | Command | Description | columns
    where commands are wrapped in backticks.
    """
    text = file_path.read_text(encoding="utf-8")
    commands: list[GroupCommand] = []
    in_table = False

    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            if line:
                in_table = False
            continue

        # Skip header separator (|---|---|)
        if re.match(r"^\|[-\s|]+\|$", line):
            in_table = True
            continue

        # Skip header row (| Command | Description |)
        if not in_table:
            if "Command" in line and "Description" in line:
                continue
            continue

        # Parse table row
        cells = [c.strip() for c in line.split("|")[1:-1]]
        if len(cells) >= 2:
            cmd_cell = cells[0]
            comment_cell = cells[1] if len(cells) > 1 else ""

            match = re.search(r"`(.+?)`", cmd_cell)
            if match:
                cmd = match.group(1)
                comment = comment_cell.strip() or None
                commands.append(GroupCommand(cmd=cmd, comment=comment))

    if not commands:
        raise click.ClickException("No commands found in markdown table.")

    return commands


def _load_group_file(path: Path) -> GroupFile:
    """Load a group file with user-friendly error handling."""
    try:
        return storage.read_group_file(path)
    except ValueError:
        raise click.ClickException(
            f"Malformed JSON in {path}. Fix manually or delete the file."
        )

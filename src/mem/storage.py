"""
Storage layer for mem — pure file I/O with JSONL and JSON.

All mem data lives in ~/.mem/ as plain text files. This module is the
only place in the codebase that touches the filesystem for data storage.

Why JSONL over SQLite: append-only writes are trivially safe (no write
conflicts, no transactions). Files are human-readable and composable
with standard Unix tools (cat, grep, tail, jq). Zero dependencies
beyond Python stdlib.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from mem.models import CapturedCommand, GroupFile, PatternFile, WorkSession

MEM_DIR = Path.home() / ".mem"

# --- Named Groups storage ---
GROUPS_DIR = MEM_DIR / "groups"
GROUPS_REPOS_DIR = GROUPS_DIR / "repos"
GROUPS_GLOBAL_FILE = GROUPS_DIR / "_global.json"


def repo_file(repo: str) -> Path:
    """Path to a repo's command history file."""
    return MEM_DIR / "repos" / f"{repo}.jsonl"


def session_file(date: str) -> Path:
    """Path to a day's session file. Date format: YYYY-MM-DD."""
    return MEM_DIR / "sessions" / f"{date}.jsonl"


def pattern_file(tool: str) -> Path:
    """Path to a tool's pattern file."""
    return MEM_DIR / "patterns" / f"{tool}.json"


def ensure_dirs() -> None:
    """Create storage directories if they don't exist."""
    (MEM_DIR / "repos").mkdir(parents=True, exist_ok=True)
    (MEM_DIR / "sessions").mkdir(parents=True, exist_ok=True)
    (MEM_DIR / "patterns").mkdir(parents=True, exist_ok=True)
    (MEM_DIR / "groups" / "repos").mkdir(parents=True, exist_ok=True)


def sanitize_repo_name(name: str) -> str:
    """Sanitize a repository name for use as a filename.

    Replaces non-alphanumeric characters (except hyphens) with hyphens.
    Handles repos with special chars or spaces in their names.
    """
    return re.sub(r"[^a-zA-Z0-9-]", "-", name).strip("-")


def append_command(cmd: CapturedCommand) -> None:
    """Append a captured command to the appropriate repo file.

    Commands inside a git repo go to repos/<repo>.jsonl.
    Commands outside any repo go to repos/_global.jsonl.
    Append-only writes are atomic at the OS level for single lines.
    """
    ensure_dirs()
    repo = sanitize_repo_name(cmd.repo) if cmd.repo else "_global"
    path = repo_file(repo)
    with path.open("a", encoding="utf-8") as f:
        f.write(cmd.to_jsonl() + "\n")


def read_commands(repo: str) -> Iterator[CapturedCommand]:
    """Lazily read commands from a repo's JSONL file.

    Yields one CapturedCommand per line. Skips corrupted lines
    (logs a warning to stderr) rather than failing the entire read.
    """
    path = repo_file(repo)
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as f:
        for line_num, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                yield CapturedCommand.from_jsonl(line)
            except Exception:
                print(
                    f"warning: skipping corrupted line {line_num} in {path.name}",
                    file=sys.stderr,
                )


def read_all_commands() -> Iterator[CapturedCommand]:
    """Iterate commands across ALL repo files.

    Used for cross-repo operations like stats and pattern extraction.
    Reads each .jsonl file in repos/ directory.
    """
    repos_dir = MEM_DIR / "repos"
    if not repos_dir.exists():
        return
    for path in sorted(repos_dir.glob("*.jsonl")):
        repo = path.stem
        yield from read_commands(repo)


def write_patterns(pf: PatternFile) -> None:
    """Write a pattern file atomically.

    Uses write-to-tmp-then-rename to prevent partial writes.
    If the process crashes mid-write, the old file remains intact.
    """
    ensure_dirs()
    path = pattern_file(pf.tool)
    tmp_path = path.with_suffix(".json.tmp")
    tmp_path.write_text(pf.model_dump_json(indent=2), encoding="utf-8")
    tmp_path.rename(path)


def read_patterns(tool: str) -> PatternFile | None:
    """Read a tool's pattern file, or None if it doesn't exist."""
    path = pattern_file(tool)
    if not path.exists():
        return None
    try:
        return PatternFile.model_validate_json(path.read_text(encoding="utf-8"))
    except Exception:
        print(f"warning: corrupted pattern file {path.name}", file=sys.stderr)
        return None


def append_session(session: WorkSession) -> None:
    """Append a work session to the appropriate daily session file."""
    ensure_dirs()
    dt = datetime.fromtimestamp(session.started_at, tz=timezone.utc)
    date = dt.strftime("%Y-%m-%d")
    path = session_file(date)
    with path.open("a", encoding="utf-8") as f:
        f.write(session.to_jsonl() + "\n")


def read_sessions(date: str) -> Iterator[WorkSession]:
    """Read sessions from a specific day's file."""
    path = session_file(date)
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as f:
        for line_num, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                yield WorkSession.from_jsonl(line)
            except Exception:
                print(
                    f"warning: skipping corrupted session line {line_num} in {path.name}",
                    file=sys.stderr,
                )


def read_all_sessions() -> Iterator[WorkSession]:
    """Iterate sessions across ALL session files."""
    sessions_dir = MEM_DIR / "sessions"
    if not sessions_dir.exists():
        return
    for path in sorted(sessions_dir.glob("*.jsonl")):
        date = path.stem
        yield from read_sessions(date)


def rotate(
    keep_commands_days: int = 90, keep_sessions_days: int = 30
) -> tuple[int, int]:
    """Rotate old data from storage.

    - Commands older than keep_commands_days are removed from repo JSONL files.
    - Session files older than keep_sessions_days are deleted entirely.
    - Pattern files are NEVER rotated (accumulated learning).

    Returns (commands_removed, session_files_removed).
    """
    import time

    now = int(time.time())
    cmd_cutoff = now - (keep_commands_days * 86400)
    session_cutoff = now - (keep_sessions_days * 86400)

    commands_removed = 0

    # Rotate repo JSONL files
    repos_dir = MEM_DIR / "repos"
    if repos_dir.exists():
        for path in repos_dir.glob("*.jsonl"):
            lines_kept = []
            lines_total = 0
            with path.open("r", encoding="utf-8") as f:
                for line in f:
                    line_stripped = line.strip()
                    if not line_stripped:
                        continue
                    lines_total += 1
                    try:
                        data = json.loads(line_stripped)
                        if data.get("ts", 0) > cmd_cutoff:
                            lines_kept.append(line_stripped)
                        else:
                            commands_removed += 1
                    except json.JSONDecodeError:
                        lines_kept.append(line_stripped)  # keep corrupted lines

            if len(lines_kept) < lines_total:
                if lines_kept:
                    # Atomic write: write to temp file then rename to avoid
                    # data loss if the process is interrupted mid-write.
                    tmp = path.with_suffix(".jsonl.tmp")
                    tmp.write_text(
                        "\n".join(lines_kept) + "\n", encoding="utf-8"
                    )
                    tmp.rename(path)
                else:
                    path.unlink()

    # Rotate session files by date in filename
    session_files_removed = 0
    sessions_dir = MEM_DIR / "sessions"
    if sessions_dir.exists():
        cutoff_date = datetime.fromtimestamp(
            session_cutoff, tz=timezone.utc
        ).strftime("%Y-%m-%d")
        for path in sessions_dir.glob("*.jsonl"):
            if path.stem < cutoff_date:
                path.unlink()
                session_files_removed += 1

    return commands_removed, session_files_removed


def forget_commands(query: str) -> int:
    """Remove all commands matching query from ALL storage files.

    Scrubs from both repo JSONL files AND session files (rewrites
    sessions to remove matching command text). Privacy-first means
    no traces left anywhere.

    Returns total number of removed entries.
    """
    removed = 0

    # Scrub from repo files
    repos_dir = MEM_DIR / "repos"
    if repos_dir.exists():
        for path in repos_dir.glob("*.jsonl"):
            lines_kept = []
            with path.open("r", encoding="utf-8") as f:
                for line in f:
                    line_stripped = line.strip()
                    if not line_stripped:
                        continue
                    try:
                        data = json.loads(line_stripped)
                        if query in data.get("command", ""):
                            removed += 1
                        else:
                            lines_kept.append(line_stripped)
                    except json.JSONDecodeError:
                        lines_kept.append(line_stripped)

            if lines_kept:
                tmp = path.with_suffix(".jsonl.tmp")
                tmp.write_text(
                    "\n".join(lines_kept) + "\n", encoding="utf-8"
                )
                tmp.rename(path)
            else:
                path.unlink()

    # Scrub from session files
    sessions_dir = MEM_DIR / "sessions"
    if sessions_dir.exists():
        for path in sessions_dir.glob("*.jsonl"):
            sessions_kept = []
            with path.open("r", encoding="utf-8") as f:
                for line in f:
                    line_stripped = line.strip()
                    if not line_stripped:
                        continue
                    try:
                        data = json.loads(line_stripped)
                        # Remove matching commands from the session's command list
                        cmds = [
                            c for c in data.get("commands", []) if query not in c
                        ]
                        if cmds:
                            data["commands"] = cmds
                            sessions_kept.append(
                                json.dumps(data, ensure_ascii=False)
                            )
                        # If all commands removed, drop the entire session
                    except json.JSONDecodeError:
                        sessions_kept.append(line_stripped)

            if sessions_kept:
                tmp = path.with_suffix(".jsonl.tmp")
                tmp.write_text(
                    "\n".join(sessions_kept) + "\n", encoding="utf-8"
                )
                tmp.rename(path)
            else:
                path.unlink()

    return removed


# --- Named Groups storage ---


def group_file_path(repo: str | None) -> Path:
    """Path for a scope's group data file.

    Repo-scoped files live under groups/repos/<sanitized_repo>.json.
    Global file is groups/_global.json.
    """
    if repo is None:
        return GROUPS_GLOBAL_FILE
    return GROUPS_REPOS_DIR / f"{sanitize_repo_name(repo)}.json"


def read_group_file(path: Path) -> GroupFile:
    """Read and parse a group file. Return empty GroupFile if missing.

    Raises ValueError on malformed JSON so callers can present
    a user-friendly error without losing the corrupt file on disk.
    """
    if not path.exists():
        return GroupFile()
    try:
        return GroupFile.model_validate_json(path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"error: cannot read {path}: {e}", file=sys.stderr)
        raise ValueError(f"Malformed data in {path}") from e


def write_group_file(path: Path, data: GroupFile) -> None:
    """Write group data atomically (tmp + rename pattern).

    Creates parent directories if needed. Uses the same atomic
    write strategy as write_patterns to prevent corruption.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".json.tmp")
    tmp_path.write_text(data.model_dump_json(indent=2), encoding="utf-8")
    tmp_path.rename(path)

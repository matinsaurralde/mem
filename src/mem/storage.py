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

import fcntl
import json
import os
import re
import stat
import sys
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator, Sequence

from mem.models import CapturedCommand, GroupFile, PatternFile, VarsFile, WorkSession

MEM_DIR = Path.home() / ".mem"

# Everything under ~/.mem is owner-only. A shell history is at least as
# sensitive as ~/.zsh_history (0600) — it records whatever was typed,
# including the secrets that were typed by mistake.
DIR_MODE = 0o700
FILE_MODE = 0o600

# --- Named Groups storage ---
GROUPS_DIR = MEM_DIR / "groups"
GROUPS_REPOS_DIR = GROUPS_DIR / "repos"
GROUPS_GLOBAL_FILE = GROUPS_DIR / "_global.json"

# --- Variable store ---
VARS_FILE = MEM_DIR / "vars.json"


def _lock_file() -> Path:
    """Path to the global write lock.

    Derived at call time rather than stored in a module constant so that
    tests (and anything else) that repoint MEM_DIR cannot end up locking —
    or worse, writing to — the real ~/.mem.
    """
    return MEM_DIR / ".lock"


_lock_depth = 0


@contextmanager
def exclusive_lock() -> Iterator[None]:
    """Serialize every mutating storage operation behind a single lock.

    Why one global lock instead of one per file: rotate() and
    forget_commands() rewrite every JSONL in one pass, so per-file locking
    would need a defined acquisition order to stay deadlock-free. Appends
    take microseconds and already run in a detached background process, so
    the contention a single lock adds is not observable at shell speed.

    Why a dedicated lock file instead of locking the data file: rotate()
    and forget_commands() replace data files via rename(), so a lock held
    on a data file would be guarding an inode that is no longer the one at
    that path — an appender could acquire "the lock" on the orphaned inode
    and write into a file that has already been unlinked.

    Re-entrant within a process. flock() is tied to the open file
    description, so a nested acquisition through a second file descriptor
    would deadlock against its own outer hold — and nesting is normal here:
    forget_commands() has to scrub patterns, groups and vars, each of which
    locks on its own.
    """
    global _lock_depth

    if _lock_depth > 0:
        _lock_depth += 1
        try:
            yield
        finally:
            _lock_depth -= 1
        return

    path = _lock_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    _harden_dir(path.parent)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, FILE_MODE)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        _lock_depth = 1
        try:
            yield
        finally:
            _lock_depth = 0
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def try_sync_lock() -> bool:
    """Take a non-blocking lock for the background sync.

    Returns False if another sync already holds it, in which case the caller
    should simply exit — this is a periodic task, so skipping a run costs
    nothing and the next capture triggers another.

    Deliberately separate from :func:`exclusive_lock`: that one serializes
    short writes and must block, while a sync can hold this for minutes and
    must never make a second one wait for it. The file descriptor is leaked on
    purpose — the lock is meant to live until the process exits.
    """
    path = MEM_DIR / ".sync.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    _harden_dir(path.parent)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, FILE_MODE)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return False
    return True


def _harden_dir(path: Path) -> None:
    """Force a directory to DIR_MODE, ignoring the ambient umask."""
    try:
        if stat.S_IMODE(path.stat().st_mode) != DIR_MODE:
            path.chmod(DIR_MODE)
    except OSError:
        pass  # A directory we cannot stat or chmod is not ours to fix.


def _harden_file(path: Path) -> None:
    """Force a file to FILE_MODE.

    Also acts as the migration path for existing installs: O_CREAT only
    applies its mode when it actually creates the file, so history written
    before this change stays 0644 until something chmods it.
    """
    try:
        if stat.S_IMODE(path.stat().st_mode) != FILE_MODE:
            path.chmod(FILE_MODE)
    except OSError:
        pass


def _fsync_dir(path: Path) -> None:
    """fsync a directory so a rename() into it survives power loss.

    Renaming is atomic with respect to other processes, but the directory
    entry itself is not durable until the directory is synced. For
    vars.json that difference is the user's credentials.
    """
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def atomic_write(path: Path, data: str, mode: int = FILE_MODE) -> None:
    """Replace a file's contents atomically and durably.

    Uses mkstemp for the temporary name. The previous implementation derived
    it from the target path, so two processes writing the same file raced on
    one shared `.tmp` and the loser's rename() failed with FileNotFoundError.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    _harden_dir(path.parent)
    fd, tmp_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
        _fsync_dir(path.parent)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _append_line(path: Path, line: str) -> None:
    """Append one line to a JSONL file, creating it owner-only."""
    path.parent.mkdir(parents=True, exist_ok=True)
    _harden_dir(path.parent)
    existed = path.exists()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, FILE_MODE)
    try:
        os.write(fd, (line + "\n").encode("utf-8"))
    finally:
        os.close(fd)
    if existed:
        _harden_file(path)


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
    """Create storage directories if they don't exist, owner-only.

    mkdir(mode=...) is masked by the ambient umask and only applies to
    directories it actually creates, so the mode is enforced explicitly —
    which also upgrades the 0755 directories left by earlier versions.
    """
    for path in (
        MEM_DIR,
        MEM_DIR / "repos",
        MEM_DIR / "sessions",
        MEM_DIR / "patterns",
        MEM_DIR / "groups",
        MEM_DIR / "groups" / "repos",
    ):
        path.mkdir(parents=True, exist_ok=True)
        _harden_dir(path)


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
    # The lock is what keeps an append from landing between rotate()'s read
    # and its rename(), which would drop the command silently.
    with exclusive_lock():
        _append_line(path, cmd.to_jsonl())


# Characters that every JSON encoder emits verbatim inside a string. No
# encoder escapes them, so a run made only of these appears byte-for-byte in
# the raw line — which is what makes the prefilter below safe.
_JSON_STABLE_RUN = re.compile(r"[A-Za-z0-9_.\-]+")

# Below this length a needle matches almost every line, so the scan costs more
# than it saves.
_MIN_NEEDLE_LEN = 3


def prefilter_needles(terms: Iterable[str]) -> list[str]:
    """Reduce search terms to substrings safe to look for in a raw JSON line.

    Answering a query currently costs ~140ms per 100k commands, and 82% of
    that is ``json.loads`` on lines that are then thrown away. Testing the raw
    line for a substring first skips the parse for everything that cannot
    match, at 25ms per 100k.

    The trap is that a raw JSONL line is *encoded*: ``grep \\d`` is stored as
    ``grep \\\\d`` and ``echo "hi"`` as ``echo \\"hi\\"``, so searching the raw
    text for the term as typed would silently miss real matches — a wrong
    answer is far worse than a slow one.

    So each term is reduced to its longest run of characters no JSON encoder
    ever escapes. If a parsed command contains the term, it contains that run,
    and the run therefore appears verbatim in the line. The filter can only
    ever admit too much, never too little. Terms that reduce to nothing useful
    (a lone ``|``, an accented word, a two-letter run) simply do not
    contribute a needle: the query still works, it just parses more lines.

    Non-ASCII characters never enter a needle, even though Pydantic emits
    them raw: these files are meant to be edited by hand, and an editor that
    writes ``ensure_ascii`` JSON would turn ``café`` into ``caf\\u00e9``. The
    ASCII run around them still counts, so ``café`` narrows to ``caf``.
    """
    needles: list[str] = []
    for term in terms:
        runs = _JSON_STABLE_RUN.findall(term)
        if not runs:
            continue
        longest = max(runs, key=len)
        if len(longest) >= _MIN_NEEDLE_LEN:
            needles.append(longest.lower())
    return needles


def read_commands(
    repo: str, needles: Sequence[str] | None = None
) -> Iterator[CapturedCommand]:
    """Lazily read commands from a repo's JSONL file.

    Yields one CapturedCommand per line. Skips corrupted lines
    (logs a warning to stderr) rather than failing the entire read.

    ``needles`` is an optional set of lowercase substrings from
    :func:`prefilter_needles`; a line missing any of them is skipped without
    being parsed. Callers that want every command leave it unset.
    """
    path = repo_file(repo)
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as f:
        for line_num, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            if needles:
                lowered = line.lower()
                if not all(needle in lowered for needle in needles):
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
    with exclusive_lock():
        atomic_write(pattern_file(pf.tool), pf.model_dump_json(indent=2))


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
    with exclusive_lock():
        _append_line(path, session.to_jsonl())


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

    with exclusive_lock():
        # Rotate repo JSONL files
        repos_dir = MEM_DIR / "repos"
        if repos_dir.exists():
            for path in sorted(repos_dir.glob("*.jsonl")):
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
                        except json.JSONDecodeError:
                            lines_kept.append(line_stripped)  # keep corrupted lines
                            continue
                        ts = data.get("ts")
                        # A missing timestamp means unknown age, not epoch 0.
                        # Deleting it would be silent data loss, and it would be
                        # inconsistent with keeping lines we cannot parse at all.
                        if not isinstance(ts, (int, float)) or ts > cmd_cutoff:
                            lines_kept.append(line_stripped)
                        else:
                            commands_removed += 1

                if len(lines_kept) < lines_total:
                    if lines_kept:
                        atomic_write(path, "\n".join(lines_kept) + "\n")
                    else:
                        path.unlink()

        # Rotate session files by date in filename
        session_files_removed = 0
        sessions_dir = MEM_DIR / "sessions"
        if sessions_dir.exists():
            cutoff_date = datetime.fromtimestamp(
                session_cutoff, tz=timezone.utc
            ).strftime("%Y-%m-%d")
            for path in sorted(sessions_dir.glob("*.jsonl")):
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

    with exclusive_lock():
        # Scrub from repo files
        repos_dir = MEM_DIR / "repos"
        if repos_dir.exists():
            for path in sorted(repos_dir.glob("*.jsonl")):
                lines_kept = []
                matched = 0
                with path.open("r", encoding="utf-8") as f:
                    for line in f:
                        line_stripped = line.strip()
                        if not line_stripped:
                            continue
                        try:
                            data = json.loads(line_stripped)
                        except json.JSONDecodeError:
                            lines_kept.append(line_stripped)
                            continue
                        if query in data.get("command", ""):
                            matched += 1
                        else:
                            lines_kept.append(line_stripped)

                # Only touch a file that actually held a match. Rewriting every
                # file on every forget churned data needlessly, and unlinking on
                # "no lines kept" deleted files the query never matched.
                if matched:
                    removed += matched
                    if lines_kept:
                        atomic_write(path, "\n".join(lines_kept) + "\n")
                    else:
                        path.unlink()

        # Scrub from session files
        sessions_dir = MEM_DIR / "sessions"
        if sessions_dir.exists():
            for path in sorted(sessions_dir.glob("*.jsonl")):
                sessions_kept = []
                matched = False
                with path.open("r", encoding="utf-8") as f:
                    for line in f:
                        line_stripped = line.strip()
                        if not line_stripped:
                            continue
                        try:
                            data = json.loads(line_stripped)
                        except json.JSONDecodeError:
                            sessions_kept.append(line_stripped)
                            continue
                        cmds = [c for c in data.get("commands", []) if query not in c]
                        if len(cmds) != len(data.get("commands", [])):
                            matched = True
                        if query in (data.get("summary") or ""):
                            # An AI-generated summary can quote the command.
                            data["summary"] = ""
                            matched = True
                        if cmds:
                            data["commands"] = cmds
                            sessions_kept.append(json.dumps(data, ensure_ascii=False))
                        else:
                            # Every command in the session was forgotten: the
                            # session itself has nothing left to describe.
                            matched = True

                if matched:
                    if sessions_kept:
                        atomic_write(path, "\n".join(sessions_kept) + "\n")
                    else:
                        path.unlink()

        _scrub_patterns(query)
        _scrub_groups(query)
        _scrub_vars(query)
        _scrub_session_state(query)

    return removed


def _scrub_patterns(query: str) -> None:
    """Drop extracted patterns whose text still carries the forgotten command."""
    patterns_dir = MEM_DIR / "patterns"
    if not patterns_dir.exists():
        return
    for path in sorted(patterns_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        kept = [
            p
            for p in data.get("patterns", [])
            if query not in p.get("pattern", "") and query not in p.get("example", "")
        ]
        processed = [c for c in data.get("processed_commands", []) if query not in c]
        if kept == data.get("patterns", []) and processed == data.get(
            "processed_commands", []
        ):
            continue
        data["patterns"] = kept
        data["processed_commands"] = processed
        atomic_write(path, json.dumps(data, indent=2, ensure_ascii=False))


def _scrub_groups(query: str) -> None:
    """Drop saved commands and runbook steps containing the forgotten command."""
    if not GROUPS_DIR.exists():
        return
    for path in sorted(GROUPS_DIR.rglob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        changed = False

        saved = [c for c in data.get("saved", []) if not _entry_matches(c, query)]
        if len(saved) != len(data.get("saved", [])):
            data["saved"] = saved
            changed = True

        for name, group in list(data.get("groups", {}).items()):
            cmds = [
                c for c in group.get("commands", []) if not _entry_matches(c, query)
            ]
            if len(cmds) != len(group.get("commands", [])):
                group["commands"] = cmds
                changed = True
            if query in (group.get("description") or ""):
                group["description"] = None
                changed = True
            data["groups"][name] = group

        if changed:
            atomic_write(path, json.dumps(data, indent=2, ensure_ascii=False))


def _entry_matches(entry: dict, query: str) -> bool:
    """True if a saved/group command entry carries the query anywhere.

    Checks the comment and variable defaults as well as the command itself:
    a credential can just as easily sit in a `--var NAME=default`.
    """
    if query in entry.get("cmd", ""):
        return True
    if query in (entry.get("comment") or ""):
        return True
    for var in entry.get("vars") or []:
        if query in (var.get("default") or "") or query in var.get("name", ""):
            return True
    return False


def _scrub_vars(query: str) -> None:
    """Drop stored variables whose value is the forgotten text."""
    if not VARS_FILE.exists():
        return
    try:
        data = json.loads(VARS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    stored = data.get("vars", {})
    kept = {
        name: v
        for name, v in stored.items()
        if query not in v.get("value", "") and query not in name
    }
    if len(kept) != len(stored):
        data["vars"] = kept
        atomic_write(VARS_FILE, json.dumps(data, indent=2, ensure_ascii=False))


def _scrub_session_state(query: str) -> None:
    """Scrub the in-flight session buffer.

    Without this, `mem forget` reported success while the open session still
    held the command in memory-backed state — and wrote it back out to
    sessions/ at the next session boundary. The secret came back after the
    user was told it was gone.
    """
    path = MEM_DIR / ".session_state.json"
    if not path.exists():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    cmds = [c for c in data.get("commands", []) if query not in c]
    if len(cmds) == len(data.get("commands", [])):
        return
    if cmds:
        data["commands"] = cmds
        atomic_write(path, json.dumps(data, ensure_ascii=False))
    else:
        # Nothing left to flush; dropping the state just starts a new session.
        path.unlink()


# --- Sync counter ---

SYNC_COUNTER_FILE = MEM_DIR / ".sync_counter"
SYNC_THRESHOLD = 20


def read_sync_counter() -> int:
    """Read the number of captures since last sync."""
    if not SYNC_COUNTER_FILE.exists():
        return 0
    try:
        return int(SYNC_COUNTER_FILE.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        return 0


def increment_sync_counter() -> int:
    """Increment capture counter and return new value.

    Read and write happen under the lock: unsynchronized read-then-write
    meant two shells capturing at the same moment both read N and both wrote
    N+1, so one capture never counted and the sync threshold drifted.
    """
    ensure_dirs()
    with exclusive_lock():
        count = read_sync_counter() + 1
        atomic_write(SYNC_COUNTER_FILE, str(count))
    return count


def reset_sync_counter() -> None:
    """Reset capture counter after a sync."""
    ensure_dirs()
    with exclusive_lock():
        atomic_write(SYNC_COUNTER_FILE, "0")


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
    with exclusive_lock():
        atomic_write(path, data.model_dump_json(indent=2))


# --- Variable store ---


def read_vars_file() -> VarsFile:
    """Read the persistent variable store. Returns empty if missing/corrupted.

    The vars file is global (not repo-scoped) because credentials and
    environment values typically apply across projects.
    """
    if not VARS_FILE.exists():
        return VarsFile()
    try:
        return VarsFile.model_validate_json(VARS_FILE.read_text(encoding="utf-8"))
    except Exception:
        print(
            f"warning: corrupted vars file {VARS_FILE}, treating as empty",
            file=sys.stderr,
        )
        return VarsFile()


def write_vars_file(data: VarsFile) -> None:
    """Write the variable store atomically with restrictive permissions.

    Uses tmp+rename pattern for crash safety. File permissions are set
    to 0600 (owner read/write only) since the file may contain secrets.
    """
    with exclusive_lock():
        atomic_write(VARS_FILE, data.model_dump_json(indent=2))

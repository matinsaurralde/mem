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
import hashlib
import json
import os
import re
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator, NamedTuple, Sequence

from mem import _fsutil, keychain
from mem._fsutil import FILE_MODE, atomic_write
from mem._fsutil import fsync_dir as _fsync_dir
from mem._fsutil import harden_dir as _harden_dir
from mem._fsutil import harden_file as _harden_file
from mem.models import (
    AgentAccess,
    AgentAuditEntry,
    CapturedCommand,
    GroupFile,
    PatternFile,
    StoredVariable,
    VarsFile,
    WorkSession,
)

MEM_DIR = Path.home() / ".mem"

# --- Named Groups storage ---
GROUPS_DIR = MEM_DIR / "groups"
GROUPS_REPOS_DIR = GROUPS_DIR / "repos"
GROUPS_GLOBAL_FILE = GROUPS_DIR / "_global.json"

# --- Variable store ---
VARS_FILE = MEM_DIR / "vars.json"

# Key used for commands captured outside any git repo. Reserved: a real repo
# path can never produce it, because sanitize_repo_name() drops underscores.
GLOBAL_REPO_KEY = "_global"

# Length of the hex digest appended to a repo's history filename. 32 bits of
# sha256 is the trade-off point: long enough that an accidental collision needs
# tens of thousands of distinct repos on one machine, short enough that the
# filename stays something a human can read and match by eye in `ls`.
REPO_KEY_HASH_LEN = 8


def _lock_file() -> Path:
    """Path to the global write lock.

    Derived at call time rather than stored in a module constant so that
    tests (and anything else) that repoint MEM_DIR cannot end up locking —
    or worse, writing to — the real ~/.mem.
    """
    return MEM_DIR / ".lock"


@contextmanager
def exclusive_lock() -> Iterator[None]:
    """Serialize every mutating storage operation behind the shared lock.

    The implementation lives in :mod:`mem._fsutil` so that modules which
    cannot import Pydantic — the interactive finder writes a file too — take
    the *same* lock rather than a second one that serializes nothing against
    this one.
    """
    with _fsutil.exclusive_lock(_lock_file()):
        yield


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


def _append_lines(path: Path, lines: list[str]) -> None:
    """Append many lines to a JSONL file in a single write.

    Separate from :func:`_append_line` because a shell history import writes
    tens of thousands of entries at once: opening, writing and closing the
    file per entry turns a sub-second operation into a visible pause, and
    leaves a partially written import if the process dies halfway.
    """
    if not lines:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    _harden_dir(path.parent)
    existed = path.exists()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, FILE_MODE)
    try:
        os.write(fd, "".join(line + "\n" for line in lines).encode("utf-8"))
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
    """Sanitize a repository name into a readable, filesystem-safe slug.

    Replaces non-alphanumeric characters (except hyphens) with hyphens.
    Handles repos with special chars or spaces in their names.

    NOT injective, and never enough on its own to name a history file:
    ``/w/a-b/c`` and ``/w/a/b/c`` both collapse to ``w-a-b-c``. Use
    :func:`repo_key` for anything that decides which file a repo's data
    lands in — see the comment there.
    """
    return re.sub(r"[^a-zA-Z0-9-]", "-", name).strip("-")


def repo_key(repo: str | None) -> str:
    """Injective storage key for a repo path: ``<slug>-<hash>``.

    The slug alone loses information — every separator becomes a hyphen — so
    two unrelated repos used to share one history file. That merged their
    command histories (a correctness bug) and let ``forget``/``rotate`` on one
    repo reach into the other's data (a privacy bug).

    The suffix is a truncated sha256 of the *exact* original path, so distinct
    paths get distinct files while the name stays browsable with ``ls``/``cat``
    — a pure hash would have fixed the collision and destroyed the plain-text
    store that is the point of this project.

    The slug can come back empty (``/`` sanitizes to ``""``), in which case the
    digest alone names the file; it is hex, so it is always a safe bare
    filename with no leading hyphen and no path separators.
    """
    if not repo:
        return GLOBAL_REPO_KEY
    digest = hashlib.sha256(repo.encode("utf-8")).hexdigest()[:REPO_KEY_HASH_LEN]
    slug = sanitize_repo_name(repo)
    return f"{slug}-{digest}" if slug else digest


def legacy_repo_key(repo: str | None) -> str:
    """Storage key a pre-fix version of mem would have used for this repo.

    Kept as its own function so the migration never has to reimplement the
    buggy scheme inline, and so a future change to sanitize_repo_name() cannot
    silently orphan history written before the fix.
    """
    if not repo:
        return GLOBAL_REPO_KEY
    return sanitize_repo_name(repo)


def resolve_repo_key(repo: str | None) -> str:
    """Return a repo's storage key, migrating any legacy history file first.

    Every read and write path goes through here so that an install predating
    the collision fix finds its history under the new name on the very first
    command it runs — a migration the user has to invoke by hand is a
    migration most users never run.
    """
    key = repo_key(repo)
    if repo:
        _migrate_legacy_repo_file(repo, key)
    return key


def _migrate_legacy_repo_file(repo: str, key: str) -> None:
    """Move a repo's pre-fix history file onto the collision-free name.

    Cheap to call on every capture: the common case is one ``stat()`` that
    finds nothing, because after the first migration the legacy file is gone.
    """
    legacy = repo_file(legacy_repo_key(repo))
    if legacy == repo_file(key) or not legacy.exists():
        return

    with exclusive_lock():
        # Re-checked under the lock: a concurrent capture may have migrated
        # the same file between the stat() above and the lock being granted.
        if not legacy.exists():
            return
        _migrate_legacy_repo_file_locked(legacy, key)


def _migrate_legacy_repo_file_locked(legacy: Path, fallback_key: str) -> None:
    """Split a legacy history file across its rightful new-scheme files.

    A legacy file may be a *collided* file holding two repos' histories. Each
    captured line records the exact ``repo`` path it came from, so the split is
    done from that field rather than guessed — the entries go back to the repo
    that produced them and the privacy leak does not survive the migration.

    Honest limitation: a line that carries no ``repo`` (hand-edited, truncated,
    or corrupted) cannot be attributed. Those lines follow ``fallback_key``,
    i.e. they land in the history of whichever repo triggered the migration.
    Dropping them would be data loss and inventing an owner would be a guess;
    the fallback at least keeps them inside the slug they were already filed
    under, since every repo mapping to this legacy name shares that slug.

    The caller must hold :func:`exclusive_lock`.
    """
    raw = legacy.read_text(encoding="utf-8", errors="replace")
    # Seeded with the requesting repo so an empty legacy file still migrates
    # (and keeps existing) instead of being silently deleted.
    buckets: dict[str, list[str]] = {fallback_key: []}
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        buckets.setdefault(_owner_key_of_line(line, fallback_key), []).append(line)

    destination = repo_file(fallback_key)
    if list(buckets) == [fallback_key] and not destination.exists():
        # Uncollided file, free destination: one rename is both atomic and
        # crash-safe, and it preserves the inode (and its 0600 mode).
        os.replace(legacy, destination)
        _harden_file(destination)
        _fsync_dir(destination.parent)
        return

    for key, lines in buckets.items():
        if not lines:
            continue  # the seeded bucket, when every line had a real owner
        _merge_lines_into(repo_file(key), lines)
    legacy.unlink(missing_ok=True)
    _fsync_dir(legacy.parent)


def _owner_key_of_line(line: str, fallback_key: str) -> str:
    """Storage key of the repo that produced one legacy JSONL line."""
    try:
        entry = json.loads(line)
        owner = entry.get("repo")
    except (ValueError, AttributeError):
        return fallback_key
    return repo_key(owner) if isinstance(owner, str) and owner else fallback_key


def _merge_lines_into(dest: Path, lines: list[str]) -> None:
    """Fold migrated lines into a new-scheme file that already exists.

    Migrated lines go first: they predate anything written under the new
    scheme, and ``mem save !`` reads the last line as the most recent command.

    Lines already present verbatim are skipped. Writing every destination and
    only then unlinking the legacy file leaves a crash window in which the
    migration can run twice; skipping duplicates makes that replay a no-op
    instead of doubling the user's history.
    """
    existing = (
        dest.read_text(encoding="utf-8", errors="replace").splitlines()
        if dest.exists()
        else []
    )
    seen = set(existing)
    merged = [line for line in lines if line not in seen] + existing
    atomic_write(dest, "".join(f"{line}\n" for line in merged))


def append_command(cmd: CapturedCommand) -> None:
    """Append a captured command to the appropriate repo file.

    Commands inside a git repo go to repos/<slug>-<hash>.jsonl.
    Commands outside any repo go to repos/_global.jsonl.
    Append-only writes are atomic at the OS level for single lines.
    """
    ensure_dirs()
    # The lock is what keeps an append from landing between rotate()'s read
    # and its rename(), which would drop the command silently. Resolving the
    # key inside it keeps a legacy-file migration and the append that follows
    # in one critical section — the lock is re-entrant, so the nested
    # acquisition in the migration is free.
    with exclusive_lock():
        path = repo_file(resolve_repo_key(cmd.repo))
        _append_line(path, cmd.to_jsonl())


def append_commands(cmds: list[CapturedCommand]) -> int:
    """Append many commands in one locked pass, returning how many were written.

    Groups by destination file so each file is opened once. Holding the lock
    for the whole batch is what makes an import atomic with respect to
    rotate() and forget_commands(), which rewrite these files wholesale.
    """
    if not cmds:
        return 0
    ensure_dirs()
    by_repo: dict[str, list[str]] = {}
    for cmd in cmds:
        repo = resolve_repo_key(cmd.repo)
        by_repo.setdefault(repo, []).append(cmd.to_jsonl())
    with exclusive_lock():
        for repo, lines in by_repo.items():
            _append_lines(repo_file(repo), lines)
    return len(cmds)


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


_COMMAND_KEY = '"command"'
_COMMAND_PREFIX = '{"command":"'


def command_span(line: str) -> str:
    """The command text inside a raw JSONL line, without decoding the line.

    The prefilter used to scan the whole line, which includes the field
    *names*. Every record contains ``"command"``, ``"dir"``, ``"exit_code"``,
    ``"duration_ms"``, ``"session"`` and ``"imported"``, so searching for any
    of `command`, `dir`, `exit`, `code`, `port`, `ms`, `ts` or `session`
    matched **every line in the store** and the filter admitted all of them.
    Measured over 20,000 records: `exit` matched 20,000 and `git` matched
    2,000 — so the queries most likely to be slow were exactly the ones that
    got no speedup at all.

    It was never a correctness bug — ``_matches`` re-checks the parsed
    command, and the command field is the only thing it looks at — which is
    why it went unnoticed. Narrowing the scan to the same field the real
    filter uses makes the two agree.

    Falls back to the whole line for anything it cannot locate: a
    hand-written or truncated record must still be considered, and admitting
    too much is the safe direction.
    """
    # Fast path: this is how mem itself writes every record, so it is the
    # shape of essentially every line, and it skips the search entirely.
    if line.startswith(_COMMAND_PREFIX):
        index = len(_COMMAND_PREFIX)
    else:
        index = _locate_value(line)
        if index < 0:
            return line

    # Everything below is `str.find`, which runs in C. An earlier version
    # walked the line character by character in Python and cost more than the
    # `json.loads` it was there to avoid — 165ms per 100k records against
    # 137ms for simply parsing everything. A filter slower than the work it
    # skips is worse than no filter.
    cursor = index
    while True:
        end = line.find('"', cursor)
        if end == -1:
            return line  # unterminated: scan the whole line rather than none
        # A quote inside a JSON string is escaped, so the closing one is the
        # first preceded by an even number of backslashes.
        backslashes = 0
        position = end - 1
        while position >= index and line[position] == "\\":
            backslashes += 1
            position -= 1
        if backslashes % 2 == 0:
            return line[index:end]
        cursor = end + 1


def _locate_value(line: str) -> int:
    """Index just past the opening quote of the ``command`` value, or -1.

    Only reached for records mem did not write — a hand-edited file, or one
    produced by another tool — so it tolerates whitespace the compact
    encoder never emits.
    """
    start = line.find(_COMMAND_KEY)
    if start == -1:
        return -1
    index = start + len(_COMMAND_KEY)
    length = len(line)
    while index < length and line[index] in " \t":
        index += 1
    if index >= length or line[index] != ":":
        return -1
    index += 1
    while index < length and line[index] in " \t":
        index += 1
    if index >= length or line[index] != '"':
        return -1
    return index + 1


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
                lowered = command_span(line).lower()
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
                        # Imported history is exempt from retention. It is old
                        # by definition — that is the point of importing it —
                        # so the age rule would delete the entire import at the
                        # first background sync after it landed, which is the
                        # user asking mem to remember and mem forgetting twenty
                        # commands later. Retention exists to bound what the
                        # hook accumulates; the import is a one-off the user
                        # asked for and can undo with `mem forget`.
                        if data.get("imported"):
                            lines_kept.append(line_stripped)
                            continue
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
        _scrub_agent_audit(query)

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
    """Drop stored variables whose name or value carries the forgotten text.

    Matching a Keychain-backed variable costs one ``security`` call per
    variable, because the value is no longer in the file mem can read. That is
    the right trade here and only here: ``mem forget`` is rare, explicit and
    destructive, and the alternative — matching on names alone — would report
    success while leaving the secret in the Keychain.

    A value that cannot be read is kept and reported. Deleting it would throw
    away a credential on a guess, and silently keeping it would let ``forget``
    claim a completeness it did not achieve.
    """
    if not VARS_FILE.exists():
        return
    data = read_vars_file()
    doomed: list[str] = []
    for name, entry in sorted(data.vars.items()):
        if query in name:
            doomed.append(name)
            continue
        if entry.value is not None:
            if query in entry.value:
                doomed.append(name)
            continue
        try:
            value = keychain.get_secret(name)
        except keychain.KeychainError as exc:
            print(
                f"warning: could not check variable {name} against the "
                f"Keychain, leaving it in place ({exc})",
                file=sys.stderr,
            )
            continue
        if value is not None and query in value:
            doomed.append(name)

    if not doomed:
        return

    scrubbed = False
    for name in doomed:
        try:
            keychain.delete_secret(name)
        except keychain.KeychainError as exc:
            # The index entry stays too. A name the user can still see is a
            # name they can retry `mem vars remove` on; dropping it would
            # leave the secret in the Keychain with nothing pointing at it.
            print(
                f"warning: could not remove variable {name} from the "
                f"Keychain, leaving it in place ({exc})",
                file=sys.stderr,
            )
            continue
        data.vars.pop(name, None)
        scrubbed = True
    if scrubbed:
        write_vars_file(data)


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


def _scrub_agent_audit(query: str) -> None:
    """Drop audit entries whose recorded arguments carry the forgotten text.

    The audit log is append-only against *time*, not against the user: an
    agent's query string is stored, and a query is free text the user may
    later want gone. ``mem forget`` promises no traces anywhere, and this is
    one of the places a trace can hide.
    """
    path = agent_audit_file()
    if not path.exists():
        return
    kept: list[str] = []
    matched = 0
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                data = json.loads(stripped)
            except json.JSONDecodeError:
                kept.append(stripped)
                continue
            args = data.get("arguments") or {}
            if isinstance(args, dict) and any(query in str(v) for v in args.values()):
                matched += 1
            else:
                kept.append(stripped)

    if not matched:
        return
    if kept:
        atomic_write(path, "\n".join(kept) + "\n")
    else:
        path.unlink()


def _strings_in(value: object) -> Iterator[str]:
    """Every string anywhere inside a decoded JSON value."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from _strings_in(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings_in(item)


def _file_holds(path: Path, query: str, jsonl: bool = False) -> bool:
    """True if *query* appears in any string stored in *path*.

    Decodes the JSON rather than grepping the raw bytes, because the file is
    *encoded*: a query containing a quote or a backslash is written escaped,
    so a raw scan would answer "not here" about text that is very much here.
    Getting that backwards means telling someone a secret is gone when it is
    not, which is the worst answer this module can give.

    Falls back to a raw scan when the file will not parse — a damaged file is
    still a file that may be holding the text.
    """
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    documents = raw.splitlines() if jsonl else [raw]
    for document in documents:
        document = document.strip()
        if not document:
            continue
        try:
            data = json.loads(document)
        except json.JSONDecodeError:
            if query in document:
                return True
            continue
        if any(query in text for text in _strings_in(data)):
            return True
    return False


def forget_targets(query: str) -> list[str]:
    """Human-readable names of the places still holding *query*.

    ``forget_commands`` scrubs six destinations, but the CLI only ever
    previewed the first one — so ``mem forget`` on text that lives *only* in a
    saved runbook, a stored variable, an extracted pattern or the agent audit
    log reported "no matching commands found" and returned without scrubbing
    anything. A command saved but never run was unforgettable, which is
    precisely the case where someone is most likely to have pasted a
    credential.

    Deliberately reports a superset: a hit here only means the scrubbers get
    a chance to run, and a scrubber that matches nothing is a no-op. Missing a
    place is the failure that matters.
    """
    found: list[str] = []

    def note(label: str, condition: bool) -> None:
        if condition:
            found.append(label)

    groups = sorted(GROUPS_DIR.rglob("*.json")) if GROUPS_DIR.exists() else []
    note("saved commands and runbooks", any(_file_holds(p, query) for p in groups))

    note("stored variables", VARS_FILE.exists() and _file_holds(VARS_FILE, query))

    patterns_dir = MEM_DIR / "patterns"
    patterns = sorted(patterns_dir.glob("*.json")) if patterns_dir.exists() else []
    note("extracted patterns", any(_file_holds(p, query) for p in patterns))

    sessions_dir = MEM_DIR / "sessions"
    sessions = sorted(sessions_dir.glob("*.jsonl")) if sessions_dir.exists() else []
    note(
        "past work sessions",
        any(_file_holds(p, query, jsonl=True) for p in sessions),
    )

    state = MEM_DIR / ".session_state.json"
    note("the session in progress", state.exists() and _file_holds(state, query))

    audit = agent_audit_file()
    note(
        "the agent audit log",
        audit.exists() and _file_holds(audit, query, jsonl=True),
    )

    return found


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
    """Read the persistent variable *index*. Returns empty if missing/corrupted.

    The vars file is global (not repo-scoped) because credentials and
    environment values typically apply across projects.

    Since ADR-009 this file records which variables exist, not what they are:
    values live in the macOS Keychain. Entries written by an older mem still
    carry their value here until :func:`migrate_vars_to_keychain` moves it.
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
    """Write the variable index atomically with restrictive permissions.

    Uses tmp+rename pattern for crash safety. File permissions are still 0600:
    the values are in the Keychain now, but the list of variable names is
    itself worth keeping to ourselves, and any not-yet-migrated entry is a
    cleartext secret.
    """
    with exclusive_lock():
        atomic_write(VARS_FILE, data.model_dump_json(indent=2))


# --- Agent access (MCP) ---
#
# These two paths are derived at call time instead of being module constants
# like VARS_FILE, for the same reason as _lock_file(): a constant is bound to
# whatever MEM_DIR was at import time, so a test (or anything else) that
# repoints MEM_DIR would still read and write the developer's real ~/.mem.
# For the flag that decides whether an AI agent may read the user's shell
# history, "the test accidentally used the real file" is not an acceptable
# failure mode.


def agent_file() -> Path:
    """Path to the agent access flag."""
    return MEM_DIR / "agent.json"


def agent_audit_file() -> Path:
    """Path to the append-only agent audit log."""
    return MEM_DIR / "agent-audit.jsonl"


def read_agent_access() -> AgentAccess:
    """Read the agent access flag, defaulting to disabled.

    Every failure mode — missing file, unreadable file, corrupted JSON —
    resolves to disabled. A privacy switch must fail closed: the one thing
    it may never do is grant access because it could not read the answer.
    """
    path = agent_file()
    if not path.exists():
        return AgentAccess()
    try:
        return AgentAccess.model_validate_json(path.read_text(encoding="utf-8"))
    except Exception:
        print(
            f"warning: corrupted agent file {path.name}, treating as disabled",
            file=sys.stderr,
        )
        return AgentAccess()


def write_agent_access(access: AgentAccess) -> None:
    """Write the agent access flag atomically, owner-only."""
    ensure_dirs()
    with exclusive_lock():
        atomic_write(agent_file(), access.model_dump_json(indent=2))


def append_agent_audit(entry: AgentAuditEntry) -> None:
    """Append one agent request to the audit log.

    Append-only by design and never rotated by :func:`rotate`: the value of
    an audit trail is that it cannot be quietly shortened. It is scrubbed by
    ``mem forget`` (see :func:`_scrub_agent_audit`), which is a deliberate,
    user-initiated deletion rather than a background one.
    """
    ensure_dirs()
    with exclusive_lock():
        _append_line(agent_audit_file(), entry.to_jsonl())


def read_agent_audit() -> Iterator[AgentAuditEntry]:
    """Lazily read the agent audit log, skipping corrupted lines."""
    path = agent_audit_file()
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as f:
        for line_num, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                yield AgentAuditEntry.from_jsonl(line)
            except Exception:
                print(
                    f"warning: skipping corrupted line {line_num} in {path.name}",
                    file=sys.stderr,
                )


class MigrationResult(NamedTuple):
    """Outcome of one :func:`migrate_vars_to_keychain` pass.

    ``failed`` names the variables still sitting in plaintext, and ``reason``
    says why in one line the user can act on. Both are empty on the ordinary
    path where there was nothing left to move.
    """

    migrated: int
    failed: list[str]
    reason: str | None


def migrate_vars_to_keychain() -> MigrationResult:
    """Move any plaintext values out of vars.json and into the Keychain.

    Idempotent and cheap to call on every command that touches the store: the
    steady state is one file read that finds nothing to do.

    The ordering is the whole point. For each variable mem writes the value to
    the Keychain, **reads it back and compares**, and only then rewrites
    vars.json without it. A crash, a locked keychain or a declined prompt at
    any point leaves the plaintext copy exactly where it was, and the next
    invocation tries again — so the worst case is a value that is still
    insecure, never a value that is gone.

    The re-read under the lock before rewriting guards the other direction: a
    concurrent ``mem vars set`` may have replaced the entry since this pass
    started, and dropping *its* value because we confirmed the old one would
    be data loss disguised as a migration.
    """
    pending = {
        name: sv.value
        for name, sv in read_vars_file().vars.items()
        if sv.value is not None
    }
    if not pending:
        return MigrationResult(0, [], None)

    reason = keychain.unavailable_reason()
    if reason is not None:
        return MigrationResult(0, sorted(pending), reason)

    migrated = 0
    failed: list[str] = []
    failure_reason: str | None = None

    for name, value in sorted(pending.items()):
        try:
            keychain.set_secret(name, value)
            if keychain.get_secret(name) != value:
                raise keychain.KeychainError(
                    "the Keychain did not return the value just written"
                )
        except keychain.KeychainError as exc:
            failed.append(name)
            failure_reason = failure_reason or str(exc)
            continue

        with exclusive_lock():
            current = read_vars_file()
            entry = current.vars.get(name)
            if entry is None or entry.value != value:
                continue  # someone changed it under us; leave it to the next pass
            current.vars[name] = StoredVariable(value=None, last_used=entry.last_used)
            write_vars_file(current)
        migrated += 1

    return MigrationResult(migrated, failed, failure_reason)


def set_var(name: str, value: str, last_used: int = 0) -> None:
    """Store a variable's value in the Keychain and record it in the index.

    Raises :class:`mem.keychain.KeychainError` — and writes nothing at all —
    when the Keychain refuses. There is deliberately no plaintext fallback:
    mem promising encryption and quietly delivering a 0600 file would be worse
    than the plaintext store it replaced, because the user would stop
    worrying about it.

    The value is read back before the index is touched, so a name only appears
    in ``mem vars list`` once the value behind it is genuinely retrievable.
    """
    keychain.set_secret(name, value)
    if keychain.get_secret(name) != value:
        raise keychain.KeychainError(
            f"Keychain accepted {name} but did not return the same value; "
            f"refusing to record it"
        )
    with exclusive_lock():
        data = read_vars_file()
        data.vars[name] = StoredVariable(value=None, last_used=last_used)
        write_vars_file(data)


def get_var_value(name: str) -> str | None:
    """Value of one stored variable, from whichever backend holds it.

    None means "mem has no such variable". A Keychain that exists but cannot
    be read raises instead — see :func:`mem.keychain.get_secret` for why that
    distinction is worth keeping.
    """
    entry = read_vars_file().vars.get(name)
    if entry is None:
        return None
    if entry.value is not None:
        return entry.value  # not migrated yet
    return keychain.get_secret(name)


def load_var_values(
    names: Iterable[str],
) -> tuple[dict[str, StoredVariable], list[str]]:
    """Load the store entries for *names*, with their values filled in.

    Returns ``(entries, unreadable)``. Only the requested names are fetched:
    every Keychain read is a subprocess and potentially an OS prompt, so
    listing a runbook must not pull every secret the user owns out of the
    Keychain just to print a tick next to a name.

    A variable whose value cannot be read is reported in ``unreadable`` and
    left out of ``entries``, so resolution falls through to the next source
    instead of silently substituting an empty string. The caller is expected
    to tell the user.
    """
    stored = read_vars_file().vars
    entries: dict[str, StoredVariable] = {}
    unreadable: list[str] = []
    for name in names:
        entry = stored.get(name)
        if entry is None:
            continue
        if entry.value is not None:
            entries[name] = entry
            continue
        try:
            value = keychain.get_secret(name)
        except keychain.KeychainError:
            unreadable.append(name)
            continue
        if value is None:
            # In the index but not in the Keychain: the item was deleted with
            # Keychain Access, or a migration was interrupted after the file
            # was rewritten. Not resolvable, and not silently an empty value.
            unreadable.append(name)
            continue
        entries[name] = StoredVariable(value=value, last_used=entry.last_used)
    return entries, unreadable


def touch_vars(names: Iterable[str], when: int) -> None:
    """Record that variables were just used.

    Read-modify-write of the index under the lock, so two shells running
    runbooks at the same moment cannot lose each other's entries — the
    previous version of this read outside the lock and wrote the whole file
    back.
    """
    names = list(names)
    if not names:
        return
    with exclusive_lock():
        data = read_vars_file()
        changed = False
        for name in names:
            entry = data.vars.get(name)
            if entry is not None:
                entry.last_used = when
                changed = True
        if changed:
            write_vars_file(data)


def remove_var(name: str) -> bool:
    """Delete a variable from both the Keychain and the index.

    False if the index had no such name. The Keychain item is deleted even
    then: an item that exists there but not in the index is exactly what a
    half-failed earlier removal leaves behind, and leaving a secret
    encrypted-but-orphaned would make ``mem vars remove`` a lie.

    The Keychain goes first so that a Keychain failure leaves the store
    untouched and the error honest. The reverse order can only ever fail
    *after* the name is gone from the index, which is the state where the user
    is told the secret was removed and it was not.
    """
    keychain.delete_secret(name)
    with exclusive_lock():
        data = read_vars_file()
        existed = name in data.vars
        if existed:
            del data.vars[name]
            write_vars_file(data)
    return existed


def clear_vars() -> tuple[int, list[str]]:
    """Delete every stored variable from both backends.

    Returns ``(removed, kept)``, where ``kept`` names the variables whose
    Keychain item could not be deleted. Those keep their index entry too: a
    name the user can still see and retry is recoverable, an orphaned secret
    with nothing pointing at it is not.
    """
    data = read_vars_file()
    removed: list[str] = []
    kept: list[str] = []
    for name in sorted(data.vars):
        try:
            keychain.delete_secret(name)
        except keychain.KeychainError:
            kept.append(name)
            continue
        removed.append(name)

    with exclusive_lock():
        current = read_vars_file()
        for name in removed:
            current.vars.pop(name, None)
        write_vars_file(current)
    return len(removed), kept

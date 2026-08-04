"""
Import of pre-existing shell history files into mem.

mem only knows the commands typed after its hook was installed, which means a
fresh install answers every query with nothing until weeks of use have
accumulated. This module closes that gap by reading the history files the
user's shell has been writing all along — ``~/.zsh_history``,
``~/.bash_history`` and fish's ``fish_history`` — and folding them into the
same JSONL store the hook writes to.

Three properties matter more than anything else here:

1. **Honesty.** A history file records a command and, at best, when it ran.
   It records no exit code, no duration and no directory. Those are stored as
   ``None`` rather than invented (see :class:`mem.models.CapturedCommand`), and
   entries with no recorded timestamp are back-dated rather than stamped
   "now" — stamping them now would make years of old commands outrank
   everything the user actually ran today.
2. **Idempotency.** Frequency is the strongest term in the ranking formula, so
   importing the same file twice must not double it. See :func:`build_plan`.
3. **Not crashing.** Real history files contain invalid UTF-8, truncated
   entries and formats from shell versions that no longer exist. Every parser
   here degrades to "treat the line as a bare command" or "count it as
   unparsed", never to an exception.

No parser uses a YAML or TOML library: the formats are line-oriented and
regular, and mem takes no dependency it can avoid.
"""

from __future__ import annotations

import re
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from mem import storage
from mem.models import CapturedCommand
from mem.variables import looks_like_credential

# The shells mem can import from, in the order they are reported.
SUPPORTED_SHELLS: tuple[str, ...] = ("zsh", "bash", "fish")

# Spacing used to back-date undated commands when the file itself gives us no
# window to spread them across. One minute apart preserves their order without
# claiming a precision we do not have.
UNDATED_SPACING_SECONDS = 60


@dataclass
class HistoryEntry:
    """One command parsed out of a history file.

    ``ts`` is ``None`` when the file recorded no timestamp for this entry —
    the normal case for bash without ``HISTTIMEFORMAT`` and for zsh without
    ``EXTENDED_HISTORY``.
    """

    command: str
    ts: int | None = None


@dataclass
class ParseResult:
    """Entries recovered from one history file, plus what could not be read.

    ``failed_lines`` counts lines that announced a structure and then failed
    to honour it (an extended-zsh header with a non-numeric timestamp, a fish
    ``when:`` that is not an integer). Lines that are simply bare commands are
    not failures — that is what plain history looks like.
    """

    entries: list[HistoryEntry] = field(default_factory=list)
    failed_lines: int = 0


@dataclass
class FilePlan:
    """What importing one history file would do, computed without writing."""

    shell: str
    path: Path
    commands: list[CapturedCommand] = field(default_factory=list)
    duplicates: int = 0
    credentials: int = 0
    failed_lines: int = 0
    error: str | None = None


@dataclass
class ImportPlan:
    """The full plan for one ``mem import --from-shell-history`` invocation."""

    files: list[FilePlan] = field(default_factory=list)

    @property
    def total(self) -> int:
        """How many commands would be written."""
        return sum(len(f.commands) for f in self.files)

    @property
    def duplicates(self) -> int:
        """How many entries were already in the store."""
        return sum(f.duplicates for f in self.files)

    @property
    def credentials(self) -> int:
        """How many entries were withheld because they look like secrets."""
        return sum(f.credentials for f in self.files)

    @property
    def failed_lines(self) -> int:
        """How many lines could not be parsed."""
        return sum(f.failed_lines for f in self.files)


# --- file discovery --------------------------------------------------------


def default_history_path(shell: str) -> Path:
    """Where a shell keeps its history by default.

    Deliberately does not consult ``$HISTFILE``: the shell does not export it,
    so the value mem would see is whatever leaked from a parent process, not
    the user's real setting. ``--file`` is the supported override.
    """
    home = Path.home()
    if shell == "zsh":
        return home / ".zsh_history"
    if shell == "bash":
        return home / ".bash_history"
    if shell == "fish":
        return home / ".local" / "share" / "fish" / "fish_history"
    raise ValueError(f"unsupported shell: {shell}")


def detect_history_files(shell: str | None = None) -> list[tuple[str, Path]]:
    """Find the history files that actually exist on this machine.

    Returns (shell, path) pairs. Restricted to one shell when ``shell`` is
    given, in which case a missing file yields an empty list — the caller
    reports that as "nothing to import" rather than guessing another shell.
    """
    shells = (shell,) if shell else SUPPORTED_SHELLS
    found: list[tuple[str, Path]] = []
    for name in shells:
        path = default_history_path(name)
        if path.is_file():
            found.append((name, path))
    return found


def shell_for_path(path: Path) -> str | None:
    """Guess which shell wrote a file, from its name alone.

    Used only for ``--file`` without ``--shell``; returns ``None`` when the
    name carries no signal, so the caller can ask instead of guessing wrong.
    """
    name = path.name.lower()
    if "fish" in name:
        return "fish"
    if "zsh" in name:
        return "zsh"
    if "bash" in name or name == ".history":
        return "bash"
    return None


def read_history_text(path: Path) -> str:
    """Read a history file as text, tolerating bytes that are not UTF-8.

    Shell history is whatever the user typed, including pasted binary, and
    zsh additionally "metafies" some bytes on disk. ``errors="replace"``
    rather than ``surrogateescape`` because the text ends up inside a JSON
    document: lone surrogates survive decoding only to make ``json.dumps``
    raise later, which would move the crash somewhere much harder to explain.
    """
    return path.read_text(encoding="utf-8", errors="replace")


# --- zsh -------------------------------------------------------------------

# `: <start-epoch>:<elapsed-seconds>;<command>` — zsh's EXTENDED_HISTORY line.
_ZSH_EXTENDED = re.compile(r"^:\s*(\d+):(\d*);(.*)$")

# A line that clearly meant to be an extended-history header and is not one:
# `: 1712345678:0` with the `;` and the command lost to a truncated or
# interleaved write. Kept narrow so a plain-history command that merely starts
# with the `:` builtin is still imported as a command instead of being counted
# as corruption.
_ZSH_LOOKS_EXTENDED = re.compile(r"^:\s*\d+:")


def _continues_on_next_line(line: str) -> bool:
    """True when a zsh history line is continued by the line after it.

    zsh stores an embedded newline as a trailing backslash, so a trailing
    backslash normally means "there is more". A backslash that is itself
    escaped does not: ``echo a\\\\`` ends with two backslashes and is a
    complete command. Counting parity distinguishes the two.
    """
    trailing = len(line) - len(line.rstrip("\\"))
    return trailing % 2 == 1


def _split_lines(text: str) -> list[str]:
    """Split history text into lines, without inventing a trailing empty one.

    ``str.split("\\n")`` yields a final empty string for the newline that ends
    a well-formed file, and ``str.splitlines()`` also splits on form feeds and
    U+2028 — bytes that appear inside real commands and must not end an entry.
    """
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def parse_zsh(text: str) -> ParseResult:
    """Parse zsh history, extended or plain, deciding per line.

    Both formats appear in the same file in practice: EXTENDED_HISTORY is
    often enabled long after the file was created, leaving bare commands
    above timestamped ones. Detection is therefore per line, not per file.
    """
    result = ParseResult()
    pending: list[str] | None = None
    pending_ts: int | None = None

    for raw in _split_lines(text):
        line = raw.rstrip("\r")

        if pending is not None:
            # Inside a multi-line entry: the line is command text, whatever
            # it looks like. A blank line here is a blank line in the command.
            if _continues_on_next_line(line):
                pending.append(line[:-1])
                continue
            pending.append(line)
            result.entries.append(HistoryEntry("\n".join(pending), pending_ts))
            pending = None
            pending_ts = None
            continue

        if not line.strip():
            continue

        match = _ZSH_EXTENDED.match(line)
        if match:
            ts: int | None = int(match.group(1))
            command = match.group(3)
        elif _ZSH_LOOKS_EXTENDED.match(line):
            result.failed_lines += 1
            continue
        else:
            ts = None
            command = line

        if _continues_on_next_line(command):
            pending = [command[:-1]]
            pending_ts = ts
            continue
        if command.strip():
            result.entries.append(HistoryEntry(command, ts))

    if pending is not None:
        # A file that ends mid-continuation (truncated, or the shell is still
        # running). Keep what we have rather than discarding the entry.
        joined = "\n".join(pending)
        if joined.strip():
            result.entries.append(HistoryEntry(joined, pending_ts))

    return result


# --- bash ------------------------------------------------------------------

# `#1712345678` — what bash writes when HISTTIMEFORMAT is set. At least eight
# digits is required so that a user's own `#123` comment, which bash stores
# verbatim as a history entry, is kept as a command instead of being eaten as
# a timestamp.
_BASH_TIMESTAMP = re.compile(r"^#(\d{8,})$")


def parse_bash(text: str) -> ParseResult:
    """Parse bash history: bare commands, optionally preceded by `#<epoch>`.

    Multi-line commands are not reconstructed because bash does not record
    them recoverably: with the default ``cmdhist`` it flattens them onto one
    line with semicolons, and with ``lithist`` it writes raw newlines that are
    indistinguishable from two separate commands. Whatever bash wrote is what
    gets imported.
    """
    result = ParseResult()
    pending_ts: int | None = None

    for raw in _split_lines(text):
        line = raw.rstrip("\r").strip()
        if not line:
            continue

        match = _BASH_TIMESTAMP.match(line)
        if match:
            pending_ts = int(match.group(1))
            continue

        result.entries.append(HistoryEntry(line, pending_ts))
        # The timestamp belongs to exactly one command. Leaving it set would
        # silently stamp every following undated command with the same time.
        pending_ts = None

    return result


# --- fish ------------------------------------------------------------------

_FISH_CMD = re.compile(r"^-\s+cmd:\s?(.*)$")
_FISH_WHEN = re.compile(r"^\s+when:\s*(.*?)\s*$")


def _unescape_fish(value: str) -> str:
    """Undo fish's history escaping: `\\n` is a newline, `\\\\` a backslash.

    fish writes a YAML-ish file but escapes only those two sequences, so a
    left-to-right scan is a complete decoder — and one that cannot be
    surprised by the YAML features fish never emits.
    """
    out: list[str] = []
    i = 0
    while i < len(value):
        char = value[i]
        if char == "\\" and i + 1 < len(value):
            nxt = value[i + 1]
            if nxt == "n":
                out.append("\n")
                i += 2
                continue
            if nxt == "\\":
                out.append("\\")
                i += 2
                continue
        out.append(char)
        i += 1
    return "".join(out)


def parse_fish(text: str) -> ParseResult:
    """Parse fish's ``fish_history`` as line-oriented text.

    The format is a YAML subset fish writes itself:

    .. code-block:: yaml

        - cmd: git push
          when: 1712345678
          paths:
            - src/main.py

    Only ``cmd`` and ``when`` carry information mem stores; ``paths`` and any
    other indented key are skipped. Parsing this by hand rather than adding a
    YAML dependency is a deliberate constraint of the project.
    """
    result = ParseResult()
    command: str | None = None
    ts: int | None = None

    def flush() -> None:
        if command is not None and command.strip():
            result.entries.append(HistoryEntry(command, ts))

    for raw in _split_lines(text):
        line = raw.rstrip("\r")
        if not line.strip():
            continue

        match = _FISH_CMD.match(line)
        if match:
            flush()
            command = _unescape_fish(match.group(1))
            ts = None
            continue

        when = _FISH_WHEN.match(line)
        if when and command is not None:
            value = when.group(1)
            if value.isdigit():
                ts = int(value)
            else:
                # The entry itself is fine; only its timestamp is unreadable,
                # so it stays and gets back-dated with the undated ones.
                result.failed_lines += 1
            continue

        if not line.startswith((" ", "\t", "-")):
            # Neither an entry header nor an indented key: this file is not
            # shaped like fish history at all.
            result.failed_lines += 1

    flush()
    return result


_PARSERS = {"zsh": parse_zsh, "bash": parse_bash, "fish": parse_fish}


def parse_history(text: str, shell: str) -> ParseResult:
    """Dispatch to the parser for a shell."""
    try:
        parser = _PARSERS[shell]
    except KeyError:
        raise ValueError(f"unsupported shell: {shell}") from None
    return parser(text)


# --- timestamps ------------------------------------------------------------


def _file_times(path: Path) -> tuple[int | None, int]:
    """Return (creation time, modification time) for a history file.

    ``st_birthtime`` exists on macOS, which is the only platform mem targets,
    but is guarded anyway so the importer still works when tests or a future
    port run somewhere without it.
    """
    try:
        info = path.stat()
    except OSError:
        now = int(time.time())
        return None, now
    birth = getattr(info, "st_birthtime", None)
    return (int(birth) if birth else None), int(info.st_mtime)


def resolve_timestamps(entries: list[HistoryEntry], path: Path) -> None:
    """Give every entry a timestamp, in place, without inventing recency.

    Two kinds of entry need one: those the file never dated, and those whose
    recorded date is impossible — zero, negative, or in the future. A corrupt
    future timestamp is discarded rather than kept because recency decays
    backwards from "now", so such an entry would sit at the top of every
    search result forever.

    The strategy: history files are chronological, so an undated entry is
    bracketed by the nearest dated entries on either side of it, and each run
    of undated entries is spread evenly across that window. At the ends of the
    file, where there is no neighbour, the file's own timestamps supply the
    bound — creation time below, modification time above. Both are real
    evidence: nothing in the file was typed before it existed, and nothing
    after it was last written. Where the filesystem cannot supply a creation
    time, the lower bound falls back to one command per minute backwards.

    What this refuses to do matters more than the exact arithmetic. Stamping
    undated commands with the current time would make a decade of old history
    outrank what the user ran an hour ago, and dropping them would throw away
    most of a plain-history user's entire past. Ordering is preserved
    throughout, which is the part of the truth the file actually recorded.
    """
    now = int(time.time())
    for entry in entries:
        if entry.ts is not None and (entry.ts <= 0 or entry.ts > now):
            entry.ts = None
    if all(entry.ts is not None for entry in entries):
        return

    birth, mtime = _file_times(path)
    count = len(entries)
    index = 0
    while index < count:
        if entries[index].ts is not None:
            index += 1
            continue

        end = index
        while end < count and entries[end].ts is None:
            end += 1
        run = end - index

        # The dated entries bracketing this run, falling back to the file's
        # own lifetime at the edges.
        upper = entries[end].ts if end < count else mtime
        if index > 0:
            lower = entries[index - 1].ts
        elif birth is not None and birth < upper:
            lower = birth
        else:
            lower = upper - UNDATED_SPACING_SECONDS * run
        lower = max(lower, 0)
        if upper <= lower:
            # Degenerate window (a file whose mtime predates its own entries,
            # or two dated entries one second apart). Order still has to hold.
            upper = lower + run

        step = (upper - lower) / (run + 1)
        for offset in range(run):
            entries[index + offset].ts = max(int(lower + step * (offset + 1)), 0)
        index = end


# --- planning and application ---------------------------------------------


def _stored_command_counts() -> Counter[str]:
    """Count how many times each command text is already in the store."""
    return Counter(cmd.command for cmd in storage.read_all_commands())


def build_plan(sources: list[tuple[str, Path]]) -> ImportPlan:
    """Work out exactly what would be imported, touching nothing.

    ``--dry-run`` and the real import both call this, so what the user is
    shown and what is written can never disagree.

    Idempotency works by counting rather than by matching individual entries.
    For each distinct command text, the store already holds some number of
    copies; the first that many occurrences in the history file are treated as
    already imported and skipped, and only the surplus is written. Running the
    import twice therefore writes nothing the second time, while a history file
    that has grown since the last import contributes exactly its new entries.

    Counting against *every* stored command, not just previously imported
    ones, is deliberate: a user who installs mem and imports a month later has
    the same commands in both places, and inflating frequency is worse than
    under-counting it — frequency is 40% of the ranking, and it is the term
    that decides what surfaces first.
    """
    stored = _stored_command_counts()
    seen: Counter[str] = Counter()
    plan = ImportPlan()

    for shell, path in sources:
        file_plan = FilePlan(shell=shell, path=path)
        plan.files.append(file_plan)

        try:
            text = read_history_text(path)
        except OSError as exc:
            file_plan.error = str(exc)
            continue

        parsed = parse_history(text, shell)
        file_plan.failed_lines = parsed.failed_lines
        resolve_timestamps(parsed.entries, path)

        for entry in parsed.entries:
            if looks_like_credential(entry.command):
                file_plan.credentials += 1
                continue
            seen[entry.command] += 1
            if seen[entry.command] <= stored[entry.command]:
                file_plan.duplicates += 1
                continue
            file_plan.commands.append(
                CapturedCommand(
                    command=entry.command,
                    ts=entry.ts or 0,
                    # A history file records no directory, so there is no repo
                    # to attribute the command to. Guessing the current one
                    # would be a lie that the ranking's context term then acts
                    # on, so imported commands go to the global store.
                    dir="",
                    repo=None,
                    exit_code=None,
                    duration_ms=None,
                    imported=True,
                )
            )

    return plan


def apply_plan(plan: ImportPlan) -> int:
    """Write a plan's commands to the store and return how many were written."""
    commands = [cmd for f in plan.files for cmd in f.commands]
    return storage.append_commands(commands)

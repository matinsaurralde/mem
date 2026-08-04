"""The interactive finder — mem's replacement for the shell's Ctrl+R.

Two hard constraints shaped every decision in this module.

**First frame latency is the product.** Ctrl+R is muscle memory; anything a
person can perceive as a pause makes it feel broken, and the budget is a few
tens of milliseconds. Importing ``mem.cli`` costs ~180ms on a warm cache —
Pydantic alone is ~58ms, Rich ~23ms, Click ~11ms — so this module imports
none of them. It reads JSONL with ``json.loads``, ranks through
:mod:`mem.ranking` (standard library only), and draws with ANSI escapes.
``mem/_entry.py`` dispatches here before ``mem.cli`` is ever imported.

**Keystroke latency is the second product.** Parsing the whole history on
every keystroke is not affordable: 124ms per 100k commands. So the file is
read once as raw text (~20ms per 100k) and only the lines that survive a
substring test are parsed (~6ms to filter, ~14ms to parse a 10% hit rate).
An empty query does not rank at all — it shows the most recent commands,
read from the tail, which is what Ctrl+R means with nothing typed.

**stdout is the result channel.** The selected command is printed to stdout
and nothing else ever is; the interface is drawn on ``/dev/tty``. That is
what lets a shell widget capture the choice with ``$(...)`` while the user
watches the list, and it makes the finder pipeable.
"""

from __future__ import annotations

import codecs
import json
import os
import select
import sys
import termios
import time
import tty
import unicodedata
from typing import IO, Iterator, NamedTuple, Sequence

from mem import picks, ranking

# --- Terminal control --------------------------------------------------------

_ALT_SCREEN_ON = "\x1b[?1049h"
_ALT_SCREEN_OFF = "\x1b[?1049l"
_CURSOR_HIDE = "\x1b[?25l"
_CURSOR_SHOW = "\x1b[?25h"
_CLEAR = "\x1b[2J\x1b[H"
_CLEAR_LINE = "\x1b[2K"
_DIM = "\x1b[2m"
_BOLD = "\x1b[1m"
_REVERSE = "\x1b[7m"
_RED = "\x1b[31m"
_CYAN = "\x1b[36m"
_RESET = "\x1b[0m"

# Key codes read from a raw terminal.
_CTRL_C = "\x03"
_CTRL_G = "\x07"
_CTRL_N = "\x0e"
_CTRL_P = "\x10"
_CTRL_R = "\x12"
_CTRL_U = "\x15"
_CTRL_W = "\x17"
_ESC = "\x1b"
_ENTER = "\r"
_NEWLINE = "\n"
_BACKSPACE = "\x7f"
_BACKSPACE_ALT = "\x08"

# How many results to hold in memory. Nobody scrolls past a few screens, and
# an unbounded list turns a pathological query into a memory problem.
MAX_RESULTS = 200

# Rows reserved for the prompt and the help line.
_CHROME_ROWS = 3


class Entry(NamedTuple):
    """One history record, as the finder needs it.

    A tuple rather than a model on purpose: constructing 100k Pydantic
    objects is the cost this module exists to avoid.
    """

    command: str
    ts: int
    repo: str | None
    exit_code: int


class Result(NamedTuple):
    """A ranked entry plus how many times the command was run."""

    entry: Entry
    score: float
    frequency: int


# --- Loading -----------------------------------------------------------------


def history_files(mem_dir: str) -> list[str]:
    """Every history file, newest-modified first.

    Ordering by mtime means the repo the user is actually working in tends to
    be read first, which matters for the empty-query view.
    """
    repos = os.path.join(mem_dir, "repos")
    try:
        names = os.listdir(repos)
    except OSError:
        return []
    paths = [os.path.join(repos, n) for n in names if n.endswith(".jsonl")]
    return sorted(paths, key=lambda p: _mtime(p), reverse=True)


def _mtime(path: str) -> float:
    try:
        return os.stat(path).st_mtime
    except OSError:
        return 0.0


def read_raw_lines(paths: Sequence[str]) -> list[str]:
    """Read every history line as text, without parsing any of it.

    ``errors="replace"`` because a history file is whatever the user typed,
    and a single undecodable byte must not take down the finder.
    """
    lines: list[str] = []
    for path in paths:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                lines.extend(handle.read().splitlines())
        except OSError:
            continue
    return lines


def parse_entry(line: str) -> Entry | None:
    """Turn one raw JSONL line into an :class:`Entry`, or ``None`` if unusable.

    Deliberately forgiving: these files are documented as hand-editable, so a
    malformed line is a normal event and must never be fatal.
    """
    try:
        record = json.loads(line)
    except (ValueError, TypeError):
        return None
    if not isinstance(record, dict):
        # Valid JSON of the wrong shape: `[]`, `null`, a bare string. Only
        # reachable in a hand-edited file, which is exactly why it is checked.
        return None
    command = record.get("command")
    if not isinstance(command, str) or not command:
        return None
    ts = record.get("ts")
    if not isinstance(ts, (int, float)):
        ts = 0
    repo = record.get("repo")
    if repo is not None and not isinstance(repo, str):
        repo = None
    exit_code = record.get("exit_code")
    if not isinstance(exit_code, int):
        exit_code = 0
    return Entry(command=command, ts=int(ts), repo=repo, exit_code=exit_code)


# --- Filtering and ranking ---------------------------------------------------


def terms_of(query: str) -> list[str]:
    """Split a query into the terms a command must all contain.

    Matched independently so word order does not matter, which is how people
    remember commands. Identical to what ``mem <query>`` does.
    """
    return [t for t in query.lower().split() if t]


def matches(command: str, terms: Sequence[str]) -> bool:
    """True when every term appears somewhere in the command."""
    lowered = command.lower()
    return all(term in lowered for term in terms)


def candidate_lines(lines: Sequence[str], terms: Sequence[str]) -> Iterator[str]:
    """Yield raw lines that could match, skipping the parse for the rest.

    The needles come from :func:`mem.storage.prefilter_needles`' rule — the
    longest run of characters no JSON encoder rewrites — but that function
    lives in a module importing Pydantic, so the reduction is inlined here.
    Both are covered by a test asserting they agree.
    """
    needles = [n for n in (_stable_run(t) for t in terms) if n]
    if not needles:
        yield from lines
        return
    for line in lines:
        lowered = line.lower()
        if all(needle in lowered for needle in needles):
            yield line


_STABLE = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-")
_MIN_NEEDLE_LEN = 3


def _stable_run(term: str) -> str:
    """Longest run of characters that survive JSON encoding unchanged.

    A raw JSONL line is encoded: ``grep \\d`` is stored as ``grep \\\\d``.
    Searching the raw text for the term as typed would silently miss real
    matches, and a finder that quietly shows fewer results is worse than a
    slow one because nobody notices. A run of these characters appears
    verbatim in the line, so the filter can only ever admit too much.
    """
    best = ""
    current = ""
    for char in term:
        if char in _STABLE:
            current += char
            if len(current) > len(best):
                best = current
        else:
            current = ""
    return best.lower() if len(best) >= _MIN_NEEDLE_LEN else ""


def rank(
    lines: Sequence[str],
    query: str,
    current_repo: str | None,
    now: float,
    limit: int = MAX_RESULTS,
) -> list[Result]:
    """Rank history against a query, deduplicated by command text.

    With an empty query there is nothing to rank, so the most recent commands
    are returned instead — which is what Ctrl+R means before you type.
    """
    terms = terms_of(query)
    if not terms:
        return _most_recent(lines, limit)

    # Read once for the whole page: it is one small file, and reading it per
    # candidate would turn every keystroke into thousands of stat() calls.
    pick_weights = picks.load(now)
    frequency: dict[str, int] = {}
    newest: dict[str, Entry] = {}
    for line in candidate_lines(lines, terms):
        entry = parse_entry(line)
        if entry is None or not matches(entry.command, terms):
            continue
        frequency[entry.command] = frequency.get(entry.command, 0) + 1
        previous = newest.get(entry.command)
        # Represent each command by its most recent run: that is the one whose
        # repo and exit status describe what would happen if you ran it now.
        if previous is None or entry.ts > previous.ts:
            newest[entry.command] = entry

    results = [
        Result(
            entry=entry,
            score=ranking.score(
                command=entry.command,
                ts=entry.ts,
                repo=entry.repo,
                query=query,
                current_repo=current_repo,
                frequency=frequency[command],
                now=now,
                pick_weight=pick_weights.get(command, 0.0),
            ),
            frequency=frequency[command],
        )
        for command, entry in newest.items()
    ]
    results.sort(key=lambda r: r.score, reverse=True)
    return results[:limit]


def _most_recent(lines: Sequence[str], limit: int) -> list[Result]:
    """The newest distinct commands, without scoring anything.

    Walks backwards so the scan stops as soon as ``limit`` distinct commands
    have been seen — the whole point of not ranking an empty query.
    """
    seen: dict[str, Entry] = {}
    for line in reversed(lines):
        entry = parse_entry(line)
        if entry is None or entry.command in seen:
            continue
        seen[entry.command] = entry
        if len(seen) >= limit:
            break
    ordered = sorted(seen.values(), key=lambda e: e.ts, reverse=True)
    return [Result(entry=entry, score=0.0, frequency=1) for entry in ordered]


# --- Rendering ---------------------------------------------------------------


def relative_time(ts: int, now: float) -> str:
    """Compact age, at most four characters wide."""
    delta = int(now) - ts
    if delta < 60:
        return "now"
    if delta < 3600:
        return f"{delta // 60}m"
    if delta < 86400:
        return f"{delta // 3600}h"
    days = delta // 86400
    if days < 7:
        return f"{days}d"
    if days < 365:
        return f"{days // 7}w"
    return f"{days // 365}y"


def char_width(char: str) -> int:
    """How many terminal columns one character occupies.

    Counting codepoints instead would misalign every row containing CJK or
    emoji, which are two columns wide, and every row containing a combining
    accent, which is zero. ``unicodedata`` is standard library, so getting
    this right costs nothing.
    """
    if unicodedata.combining(char):
        return 0
    return 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1


def display_width(text: str) -> int:
    """Total terminal columns *text* occupies."""
    return sum(char_width(c) for c in text)


def _visible(text: str, width: int) -> str:
    """Fit text into *width* columns, stripping anything the terminal obeys.

    Control characters are replaced rather than escaped: a command containing
    a stray escape sequence would otherwise repaint the screen, set a colour
    that bleeds into every row after it, or rewrite the window title, purely
    by being *listed*. History is untrusted input — it is whatever somebody
    pasted into a shell.
    """
    cleaned = "".join(ch if ch.isprintable() or ch == " " else "?" for ch in text)
    if display_width(cleaned) <= width:
        return cleaned
    if width <= 1:
        return cleaned[:1] if width == 1 else ""
    # Leave one column for the ellipsis.
    out: list[str] = []
    used = 0
    for char in cleaned:
        step = char_width(char)
        if used + step > width - 1:
            break
        out.append(char)
        used += step
    return "".join(out) + "…"


def render(
    results: Sequence[Result],
    query: str,
    selected: int,
    rows: int,
    columns: int,
    now: float,
    total: int,
) -> str:
    """Build the whole frame as one string.

    One write per frame, because several small writes to a terminal in raw
    mode are visibly incremental — the list appears to be assembled rather
    than to change.
    """
    height = max(rows - _CHROME_ROWS, 1)
    first = _scroll_offset(selected, len(results), height)
    window = results[first : first + height]

    out = [_CLEAR]
    out.append(
        f"{_BOLD}mem{_RESET} {query}"
        f"{_DIM}▏{_RESET}  {_DIM}{len(results)}/{total}{_RESET}\r\n"
    )
    out.append(f"{_DIM}{'─' * max(columns, 1)}{_RESET}\r\n")

    for offset, result in enumerate(window):
        out.append(_render_row(result, first + offset == selected, columns, now))

    if not results:
        message = "no match" if query else "no history yet — mem records as you work"
        out.append(f"{_DIM}  {_visible(message, columns - 2)}{_RESET}\r\n")

    out.append(
        f"\x1b[{rows};1H{_CLEAR_LINE}{_DIM}"
        f"{_visible('↑↓ select · ⏎ accept · ^U clear · esc cancel', columns)}{_RESET}"
    )
    return "".join(out)


# " ▸ ✗ " — selection marker and exit status, always present so the command
# column never shifts between rows or as the results change underneath.
_PREFIX_WIDTH = 5


def _render_row(result: Result, is_selected: bool, columns: int, now: float) -> str:
    """One result line: marker, status, command, repo, age.

    Laid out so the metadata is flush right and the command starts at the
    same column on every row. The marker and the failure cross occupy their
    columns whether or not they are shown: a layout that reflows depending on
    whether a command failed is a layout that jitters while you type.
    """
    entry = result.entry
    age = relative_time(entry.ts, now)
    repo = os.path.basename(entry.repo) if entry.repo else ""

    meta = _visible(f"{repo} {age}".strip(), min(22, max(columns // 3, 0)))
    meta_width = display_width(meta)
    # The -1 reserves the single-column gap below. Without it a maximally long
    # command leaves no room for the gap, the `max(..., 1)` adds one anyway,
    # and the row runs one column past the edge and wraps.
    command = _visible(entry.command, max(columns - _PREFIX_WIDTH - meta_width - 1, 8))
    gap = max(columns - _PREFIX_WIDTH - display_width(command) - meta_width, 1)

    marker = "▸" if is_selected else " "
    status = f"{_RED}✗{_RESET}" if entry.exit_code != 0 else " "

    line = f" {marker} {status} {command}{' ' * gap}{_CYAN}{_DIM}{meta}{_RESET}"
    if is_selected:
        line = f"{_REVERSE}{line}{_RESET}"
    return line + "\r\n"


def _scroll_offset(selected: int, count: int, height: int) -> int:
    """Keep the selection on screen with as little scrolling as possible."""
    if count <= height:
        return 0
    half = height // 2
    return max(0, min(selected - half, count - height))


# --- Input -------------------------------------------------------------------


class Action(NamedTuple):
    """What one keypress means."""

    kind: str  # "insert" | "delete" | "clear" | "word" | "up" | "down" | "accept" | "cancel" | "noop"
    text: str = ""


def decode(key: str) -> Action:
    """Map a keypress to an action.

    Split out from the loop so every binding is testable without a terminal —
    which is the only way a raw-mode interface gets tested at all.
    """
    if key in (_CTRL_C, _CTRL_G):
        return Action("cancel")
    if key in (_ENTER, _NEWLINE):
        return Action("accept")
    if key in (_BACKSPACE, _BACKSPACE_ALT):
        return Action("delete")
    if key == _CTRL_U:
        return Action("clear")
    if key == _CTRL_W:
        return Action("word")
    if key in (_CTRL_P, "\x1b[A", "\x1bOA"):
        return Action("up")
    if key in (_CTRL_N, "\x1b[B", "\x1bOB", _CTRL_R):
        # Ctrl+R moves down: pressing it repeatedly is how everyone walks back
        # through matches, and the finder is bound to that key.
        return Action("down")
    if key == _ESC:
        return Action("cancel")
    if key.startswith(_ESC):
        return Action("noop")  # an escape sequence we do not bind
    if len(key) == 1 and (key.isprintable() or key == " "):
        return Action("insert", key)
    return Action("noop")


class KeyReader:
    """Reads keypresses from a raw terminal, one at a time.

    Deliberately built on ``os.read`` and an incremental decoder rather than
    a text file object. ``TextIOWrapper.read(1)`` does not return a lone
    ``"\\r"`` — it holds it, waiting for the byte that would tell it whether
    this is a bare carriage return or the start of ``"\\r\\n"``. A terminal in
    raw mode sends Enter as exactly that lone ``"\\r"``, so the finder hung on
    the single most important key, and ``newline=""`` does not help.

    Decoding incrementally also handles a multi-byte character correctly:
    pasting ``é`` delivers two bytes, and each on its own is not a character.
    """

    def __init__(self, fd: int) -> None:
        self.fd = fd
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")

    def read_key(self) -> str:
        """Read one keypress, collapsing escape sequences into a single token.

        Arrow keys arrive as three bytes. Reading them one at a time would
        move the selection and then insert ``[A`` into the query.
        """
        first = self._read_char()
        if first != _ESC:
            return first
        # An escape not followed by anything is a real Escape keypress; a
        # terminal sends the rest of a sequence in the same burst.
        second = self._read_char_if_ready()
        if not second:
            return _ESC
        if second in ("[", "O"):
            return _ESC + second + (self._read_char_if_ready() or "")
        return _ESC + second

    def _read_char(self) -> str:
        """Block until one whole character is available. "" means EOF."""
        while True:
            data = os.read(self.fd, 1)
            if not data:
                return ""
            text = self._decoder.decode(data)
            if text:
                return text[0]

    def _read_char_if_ready(self) -> str:
        """Read another character only if the terminal already sent it."""
        ready, _, _ = select.select([self.fd], [], [], 0.05)
        if not ready:
            return ""
        return self._read_char()


def delete_word(query: str) -> str:
    """Drop the last whitespace-delimited word, like Ctrl+W in a shell."""
    stripped = query.rstrip()
    cut = stripped.rfind(" ")
    return stripped[: cut + 1] if cut != -1 else ""


# --- The loop ----------------------------------------------------------------


class Finder:
    """The interactive session: state, input handling, and redrawing.

    Kept separate from :func:`main` so the whole interaction can be driven in
    a test without a pseudo-terminal.
    """

    def __init__(
        self,
        lines: Sequence[str],
        current_repo: str | None,
        query: str = "",
        now: float | None = None,
    ) -> None:
        self.lines = lines
        self.current_repo = current_repo
        self.query = query
        self.selected = 0
        self.now = time.time() if now is None else now
        self.results = rank(self.lines, self.query, self.current_repo, self.now)

    def apply(self, action: Action) -> str | None:
        """Apply an action. Returns "accept"/"cancel" when the session ends."""
        if action.kind in ("accept", "cancel"):
            return action.kind
        if action.kind == "up":
            self.selected = max(self.selected - 1, 0)
            return None
        if action.kind == "down":
            self.selected = min(self.selected + 1, max(len(self.results) - 1, 0))
            return None

        previous = self.query
        if action.kind == "insert":
            self.query += action.text
        elif action.kind == "delete":
            self.query = self.query[:-1]
        elif action.kind == "clear":
            self.query = ""
        elif action.kind == "word":
            self.query = delete_word(self.query)
        else:
            return None

        if self.query != previous:
            self.results = rank(self.lines, self.query, self.current_repo, self.now)
            # Any edit to the query invalidates the position: keeping index 7
            # over a completely different result set selects an unrelated
            # command, which is how a finder makes you run the wrong thing.
            self.selected = 0
        return None

    @property
    def choice(self) -> str | None:
        """The currently highlighted command, if there is one."""
        if not self.results:
            return None
        return self.results[self.selected].entry.command

    def frame(self, rows: int, columns: int) -> str:
        return render(
            self.results,
            self.query,
            self.selected,
            rows,
            columns,
            self.now,
            len(self.lines),
        )


def run(finder: Finder, keys: KeyReader, tty_out: IO[str]) -> str | None:
    """Drive the finder against an already-raw terminal. Returns the choice."""
    while True:
        rows, columns = _terminal_size(tty_out)
        tty_out.write(finder.frame(rows, columns))
        tty_out.flush()

        key = keys.read_key()
        if not key:  # the terminal went away
            return None
        outcome = finder.apply(decode(key))
        if outcome == "cancel":
            return None
        if outcome == "accept":
            return finder.choice


def _terminal_size(stream: IO[str]) -> tuple[int, int]:
    """Rows and columns, with a usable fallback when there is no terminal."""
    try:
        size = os.get_terminal_size(stream.fileno())
        return max(size.lines, _CHROME_ROWS + 1), max(size.columns, 20)
    except (OSError, ValueError):
        return 24, 80


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point. Prints the chosen command to stdout; exits 1 if cancelled.

    Everything the user sees goes to ``/dev/tty`` so that stdout carries only
    the result — that is what makes ``BUFFER=$(mem tui)`` work in a shell
    widget.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    # `--` ends the options. A shell widget passes the current command line
    # after it, and that line very often starts with a dash (`-la`, `--force`),
    # which would otherwise be swallowed as a flag.
    if "--" in args:
        query = " ".join(args[args.index("--") + 1 :])
    else:
        if "--help" in args or "-h" in args:
            print(_USAGE)
            return 0
        query = " ".join(a for a in args if not a.startswith("-"))
    mem_dir = os.environ.get("MEM_DIR") or os.path.join(os.path.expanduser("~"), ".mem")
    lines = read_raw_lines(history_files(mem_dir))
    finder = Finder(lines, _current_repo(), query=query)

    try:
        tty_in = open("/dev/tty", "rb", buffering=0)
        # newline="" so the "\r\n" ending every frame is written through
        # unchanged rather than translated on the way out.
        tty_out = open("/dev/tty", "w", encoding="utf-8", errors="replace", newline="")
    except OSError:
        # No controlling terminal — a pipe, a cron job, an editor's task
        # runner. Fall back to the best answer without any interaction.
        choice = finder.choice
        if choice is None:
            return 1
        print(choice)
        return 0

    with tty_in, tty_out:
        keys = KeyReader(tty_in.fileno())
        choice = _with_raw_terminal(
            tty_in.fileno(), tty_out, lambda: run(finder, keys, tty_out)
        )

    if choice is None:
        return 1
    # Recorded here and nowhere else: this is the one moment the user answers
    # the exact question the ranking spends the rest of its life guessing at.
    # After the terminal is restored, so a failure writing a ranking hint can
    # never leave someone with a shell in raw mode.
    picks.record(choice)
    print(choice)
    return 0


def _with_raw_terminal(fd: int, tty_out: IO[str], body):
    """Put the terminal in raw mode for *body*, and always put it back.

    The restore is in a ``finally`` with no exception filter on purpose: if
    this module crashes, the user is left with a terminal that echoes
    nothing and has no alternate screen to return from. A traceback is
    recoverable; a broken shell session is not.
    """
    saved = termios.tcgetattr(fd)
    tty_out.write(_ALT_SCREEN_ON + _CURSOR_HIDE)
    tty_out.flush()
    try:
        tty.setraw(fd)
        return body()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
        tty_out.write(_CURSOR_SHOW + _ALT_SCREEN_OFF)
        tty_out.flush()


def _current_repo() -> str | None:
    """The git repo containing the working directory, without running git.

    Walking up looking for ``.git`` costs microseconds; spawning ``git`` costs
    milliseconds we do not have. ``mem.capture.get_git_repo`` is the
    authority elsewhere, but it is not importable without paying for the
    module graph this finder exists to skip.
    """
    path = os.getcwd()
    while True:
        if os.path.exists(os.path.join(path, ".git")):
            return path
        parent = os.path.dirname(path)
        if parent == path:
            return None
        path = parent


_USAGE = """\
mem tui — interactive history finder

  mem tui [QUERY]

Type to filter, ↑/↓ or ^P/^N to move, ⏎ to accept, esc to cancel.
The selected command is printed to stdout; nothing else ever is.

Bound to Ctrl+R by the shell hook. Set MEM_NO_KEYBINDING=1 before
loading the hook to keep your shell's own Ctrl+R."""

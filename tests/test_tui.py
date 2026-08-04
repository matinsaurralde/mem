"""Tests for the interactive finder.

A raw-mode terminal interface is the classic "untestable" component, and the
usual result is that it ships untested. Two design choices make it testable
here, and both are worth keeping:

- key decoding is a pure function (``decode``) and state transitions are a
  pure method (``Finder.apply``), so every binding can be driven without a
  pseudo-terminal;
- a frame is built as one string (``render``) rather than written
  incrementally, so what the user would see is an ordinary return value.

What genuinely needs a terminal — raw mode, the alternate screen, restoring
``termios`` — is covered by driving the real binary through a pty.
"""

from __future__ import annotations

import fcntl
import json
import os
import pty
import re
import subprocess
import sys
import termios
import time
from pathlib import Path

import pytest

from mem import ranking, storage, tui

# --- Helpers -----------------------------------------------------------------


def line_of(
    command: str, ts: int = 0, repo: str | None = None, exit_code: int = 0
) -> str:
    """One raw JSONL line, exactly as storage writes it."""
    return json.dumps(
        {
            "command": command,
            "ts": ts or int(time.time()),
            "dir": "/w",
            "repo": repo,
            "exit_code": exit_code,
            "duration_ms": 1,
        }
    )


def strip_ansi(text: str) -> str:
    """Drop escape sequences so a frame can be asserted on as plain text."""
    return re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]", "", text)


def type_into(finder: tui.Finder, text: str) -> None:
    """Feed a string to the finder one keypress at a time."""
    for char in text:
        finder.apply(tui.decode(char))


# --- Parsing -----------------------------------------------------------------


class TestParsing:
    """History files are documented as hand-editable, so parsing must not trust."""

    def test_reads_a_normal_record(self):
        entry = tui.parse_entry(line_of("git push", ts=42, repo="/r", exit_code=1))

        assert entry == tui.Entry(command="git push", ts=42, repo="/r", exit_code=1)

    @pytest.mark.parametrize(
        "line",
        [
            "",
            "not json at all",
            "{",
            "[]",  # valid JSON, wrong shape
            '"a string"',
            "{}",  # no command
            '{"command": ""}',  # empty command
            '{"command": 42}',  # wrong type
            "null",
        ],
    )
    def test_an_unusable_line_is_skipped_not_fatal(self, line: str):
        assert tui.parse_entry(line) is None

    @pytest.mark.parametrize(
        ("field", "value"),
        [("ts", "yesterday"), ("repo", 7), ("exit_code", "zero")],
    )
    def test_a_bad_field_falls_back_instead_of_crashing(self, field: str, value):
        record = json.loads(line_of("ls"))
        record[field] = value

        entry = tui.parse_entry(json.dumps(record))

        assert entry is not None
        assert entry.command == "ls"

    def test_undecodable_bytes_do_not_break_the_read(self, tmp_path: Path):
        """A history file is whatever got typed, including invalid UTF-8."""
        path = tmp_path / "repos" / "_global.jsonl"
        path.parent.mkdir(parents=True)
        path.write_bytes(line_of("echo ok").encode() + b"\n" + b"\xff\xfe bad\n")

        lines = tui.read_raw_lines([str(path)])

        assert len(lines) == 2
        assert tui.parse_entry(lines[0]) is not None
        assert tui.parse_entry(lines[1]) is None

    def test_a_missing_file_is_not_an_error(self, tmp_path: Path):
        assert tui.read_raw_lines([str(tmp_path / "nope.jsonl")]) == []

    def test_no_history_directory_yields_no_files(self, tmp_path: Path):
        assert tui.history_files(str(tmp_path)) == []


# --- Filtering ---------------------------------------------------------------


class TestFiltering:
    """The prefilter is an optimisation and must be invisible."""

    @pytest.mark.parametrize(
        ("command", "query"),
        [
            (r"grep \d+ log", r"\d+"),
            ('echo "hi there"', '"hi'),
            ("curl -H 'X: {\"k\":1}'", '{"k"'),
            ("echo café", "café"),
            ("echo 日本語", "日本語"),
            ("awk '{print $1}'", "$1"),
            ("a|b", "a|b"),
        ],
    )
    def test_an_encoded_command_is_still_found(self, command: str, query: str):
        results = tui.rank([line_of(command)], query, None, now=time.time())

        assert [r.entry.command for r in results] == [command]

    def test_the_prefilter_agrees_with_the_storage_layer(self):
        """The reduction is inlined here to avoid importing Pydantic.

        Two implementations of the same rule is exactly the pattern that put
        a stale shell hook in front of every pip user, so they are pinned
        against each other.
        """
        terms = [
            "kubectl",
            r"\bword\b",
            '"quoted"',
            "path/to/file",
            "--flag=value",
            "a.b-c_d",
            "café",
            "ab",
            "|",
            "$1",
            "日本語",
        ]
        for term in terms:
            inlined = tui._stable_run(term)
            official = storage.prefilter_needles([term])

            assert [inlined] if inlined else [] == official, f"disagree on {term!r}"

    def test_every_term_must_match(self):
        lines = [line_of("docker compose up"), line_of("docker build .")]

        results = tui.rank(lines, "docker compose", None, now=time.time())

        assert [r.entry.command for r in results] == ["docker compose up"]

    def test_word_order_does_not_matter(self):
        results = tui.rank(
            [line_of("docker compose up")], "up docker", None, time.time()
        )

        assert len(results) == 1

    def test_matching_is_case_insensitive(self):
        results = tui.rank([line_of("DOCKER Compose")], "docker", None, time.time())

        assert len(results) == 1


# --- Ranking -----------------------------------------------------------------


class TestRanking:
    def test_it_ranks_exactly_like_the_search_command(self):
        """The finder and `mem <query>` must not order history differently.

        Both go through `mem.ranking`; this pins that they actually do.
        """
        now = time.time()
        lines = [line_of("git push origin main", ts=int(now), repo="/r")] * 3

        result = tui.rank(lines, "git push", "/r", now)[0]
        expected = ranking.score(
            command="git push origin main",
            ts=int(now),
            repo="/r",
            query="git push",
            current_repo="/r",
            frequency=3,
            now=now,
        )

        assert result.score == pytest.approx(expected)
        assert result.frequency == 3

    def test_duplicates_collapse_to_the_most_recent_run(self):
        """The newest occurrence is the one whose repo and status describe now."""
        now = int(time.time())
        lines = [
            line_of("make test", ts=now - 10_000, repo="/old", exit_code=1),
            line_of("make test", ts=now, repo="/new", exit_code=0),
        ]

        results = tui.rank(lines, "make", None, now=now)

        assert len(results) == 1
        assert results[0].entry.repo == "/new"
        assert results[0].frequency == 2

    def test_an_empty_query_shows_the_most_recent_commands(self):
        """That is what Ctrl+R means before you have typed anything."""
        now = int(time.time())
        lines = [
            line_of("oldest", ts=now - 300),
            line_of("middle", ts=now - 200),
            line_of("newest", ts=now - 100),
        ]

        results = tui.rank(lines, "", None, now=now)

        assert [r.entry.command for r in results] == ["newest", "middle", "oldest"]

    def test_an_empty_query_does_not_score_anything(self):
        """Ranking 100k commands to show "the last 20" would be pure waste."""
        results = tui.rank([line_of("a"), line_of("b")], "  ", None, time.time())

        assert all(r.score == 0.0 for r in results)

    def test_the_empty_query_view_is_deduplicated(self):
        now = int(time.time())
        lines = [line_of("ls", ts=now - i) for i in range(5)]

        assert len(tui.rank(lines, "", None, now=now)) == 1

    def test_results_are_capped(self):
        lines = [line_of(f"cmd-{i}", ts=i) for i in range(tui.MAX_RESULTS + 50)]

        assert len(tui.rank(lines, "cmd", None, time.time())) == tui.MAX_RESULTS
        assert len(tui.rank(lines, "", None, time.time())) == tui.MAX_RESULTS

    def test_no_history_yields_no_results(self):
        assert tui.rank([], "anything", None, time.time()) == []
        assert tui.rank([], "", None, time.time()) == []


# --- Key handling ------------------------------------------------------------


class TestKeyDecoding:
    @pytest.mark.parametrize(
        ("key", "kind"),
        [
            ("\x03", "cancel"),  # Ctrl+C
            ("\x07", "cancel"),  # Ctrl+G
            ("\x1b", "cancel"),  # Escape
            ("\r", "accept"),
            ("\n", "accept"),
            ("\x7f", "delete"),
            ("\x08", "delete"),
            ("\x15", "clear"),  # Ctrl+U
            ("\x17", "word"),  # Ctrl+W
            ("\x10", "up"),  # Ctrl+P
            ("\x1b[A", "up"),
            ("\x1bOA", "up"),  # application cursor mode
            ("\x0e", "down"),  # Ctrl+N
            ("\x1b[B", "down"),
            ("\x12", "down"),  # Ctrl+R again walks further back
            ("g", "insert"),
            (" ", "insert"),
            ("é", "insert"),
            ("\x1b[5~", "noop"),  # PageUp, unbound
            ("\x00", "noop"),
        ],
    )
    def test_a_key_means_what_it_should(self, key: str, kind: str):
        assert tui.decode(key).kind == kind

    def test_ctrl_r_walks_down_because_that_is_the_key_you_pressed(self):
        """Repeating the binding must move through matches, not re-open."""
        finder = tui.Finder([line_of("a1"), line_of("a2")], None, query="a")

        finder.apply(tui.decode("\x12"))

        assert finder.selected == 1

    @pytest.mark.parametrize(
        ("start", "expected"),
        [("git push origin", "git push "), ("git", ""), ("git ", ""), ("", "")],
    )
    def test_ctrl_w_deletes_a_word(self, start: str, expected: str):
        assert tui.delete_word(start) == expected


# --- Interaction -------------------------------------------------------------


class TestFinderState:
    @pytest.fixture
    def lines(self) -> list[str]:
        now = int(time.time())
        return [
            line_of("git push origin main", ts=now, repo="/r"),
            line_of("git pull --rebase", ts=now - 100, repo="/r"),
            line_of("docker compose up", ts=now - 200, repo="/r"),
        ]

    def test_typing_narrows_the_results(self, lines: list[str]):
        finder = tui.Finder(lines, "/r")
        assert len(finder.results) == 3

        type_into(finder, "git")

        assert len(finder.results) == 2

    def test_backspace_widens_them_again(self, lines: list[str]):
        finder = tui.Finder(lines, "/r")
        type_into(finder, "git")

        finder.apply(tui.decode("\x7f"))

        assert finder.query == "gi"
        assert len(finder.results) == 2

    def test_ctrl_u_clears_the_query(self, lines: list[str]):
        finder = tui.Finder(lines, "/r")
        type_into(finder, "docker")

        finder.apply(tui.decode("\x15"))

        assert finder.query == ""
        assert len(finder.results) == 3

    def test_editing_the_query_resets_the_selection(self, lines: list[str]):
        """Keeping index 2 over a new result set selects an unrelated command.

        That is how a finder makes someone run the wrong thing, so it is
        pinned rather than left to the implementation.
        """
        finder = tui.Finder(lines, "/r")
        finder.apply(tui.decode("\x0e"))
        finder.apply(tui.decode("\x0e"))
        assert finder.selected == 2

        type_into(finder, "g")

        assert finder.selected == 0

    def test_the_selection_cannot_leave_the_list(self, lines: list[str]):
        finder = tui.Finder(lines, "/r")

        for _ in range(10):
            finder.apply(tui.decode("\x0e"))
        assert finder.selected == len(finder.results) - 1

        for _ in range(10):
            finder.apply(tui.decode("\x10"))
        assert finder.selected == 0

    def test_moving_in_an_empty_list_does_not_crash(self):
        finder = tui.Finder([], None)

        finder.apply(tui.decode("\x0e"))
        finder.apply(tui.decode("\x10"))

        assert finder.choice is None

    def test_accept_returns_the_highlighted_command(self, lines: list[str]):
        finder = tui.Finder(lines, "/r")
        type_into(finder, "git")

        assert finder.apply(tui.decode("\r")) == "accept"
        assert finder.choice == "git push origin main"

    def test_cancel_ends_without_a_choice(self, lines: list[str]):
        finder = tui.Finder(lines, "/r")

        assert finder.apply(tui.decode("\x1b")) == "cancel"

    def test_an_initial_query_is_applied(self, lines: list[str]):
        """The shell widget hands over whatever is already on the line."""
        finder = tui.Finder(lines, "/r", query="docker")

        assert [r.entry.command for r in finder.results] == ["docker compose up"]


# --- Rendering ---------------------------------------------------------------


class TestRendering:
    def test_the_frame_shows_commands_and_the_match_count(self):
        finder = tui.Finder([line_of("git push"), line_of("ls")], None, query="git")

        frame = strip_ansi(finder.frame(rows=24, columns=80))

        assert "git push" in frame
        assert "1/2" in frame

    def test_the_selected_row_is_marked(self):
        finder = tui.Finder([line_of("a1"), line_of("a2")], None, query="a")
        finder.apply(tui.decode("\x0e"))

        frame = finder.frame(rows=24, columns=80)
        marked = [ln for ln in frame.split("\r\n") if "▸" in ln]

        assert len(marked) == 1
        assert "a2" in marked[0]

    def test_a_failed_command_is_flagged(self):
        finder = tui.Finder([line_of("bad-cmd", exit_code=127)], None, query="bad")

        assert "✗" in finder.frame(rows=24, columns=80)

    def test_empty_history_says_so_instead_of_showing_nothing(self):
        frame = strip_ansi(tui.Finder([], None).frame(rows=24, columns=80))

        assert "no history yet" in frame

    def test_no_match_is_distinguished_from_no_history(self):
        finder = tui.Finder([line_of("ls")], None, query="zzz")

        assert "no match" in strip_ansi(finder.frame(rows=24, columns=80))

    def test_a_command_cannot_repaint_the_terminal(self):
        """History is untrusted input — it is whatever got pasted into a shell.

        Listing a command that contains escape sequences must not let it
        move the cursor, clear the screen, or set a colour that bleeds into
        every row after it.
        """
        hostile = "echo \x1b[2J\x1b[31mgotcha\x1b]0;title\x07"
        finder = tui.Finder([line_of(hostile)], None, query="echo")

        frame = finder.frame(rows=24, columns=200)
        body = frame.split("\r\n")[2]  # the result row

        assert "\x1b[2J" not in body
        assert "\x1b]0;" not in body
        assert "gotcha" in body

    @pytest.mark.parametrize("columns", [20, 40, 80, 120])
    def test_long_commands_are_truncated_to_the_terminal_width(self, columns: int):
        """One column too many and every row wraps, doubling the list height."""
        finder = tui.Finder([line_of("x" * 500)], None, query="xxx")

        for line in strip_ansi(finder.frame(rows=24, columns=columns)).split("\r\n"):
            assert tui.display_width(line) <= columns, repr(line)

    def test_double_width_characters_do_not_overflow_the_row(self):
        """CJK and emoji are two columns wide; counting codepoints misaligns.

        A row that measures 80 characters but occupies 160 columns wraps, and
        the list silently becomes twice as tall as the finder thinks it is.
        """
        finder = tui.Finder(
            [line_of("echo 日本語のコマンドです" * 8)], None, query="echo"
        )

        for line in strip_ansi(finder.frame(rows=24, columns=80)).split("\r\n"):
            assert tui.display_width(line) <= 80, repr(line)

    def test_the_metadata_column_is_flush_right_on_every_row(self):
        """A layout that reflows per row is a layout that jitters as you type."""
        now = int(time.time())
        finder = tui.Finder(
            [
                line_of("short", ts=now, repo="/p/api"),
                line_of("a much longer command line here", ts=now - 90000, repo="/p/x"),
                line_of("failed one", ts=now - 100, repo="/p/api", exit_code=1),
            ],
            None,
        )

        rows = [
            line
            for line in strip_ansi(finder.frame(rows=24, columns=80)).split("\r\n")
            if line.strip() and "─" not in line and "select" not in line
        ][1:]  # drop the prompt line

        widths = {tui.display_width(line) for line in rows}
        assert len(rows) == 3
        assert widths == {80}, f"rows are not all 80 columns wide: {widths}"

    @pytest.mark.parametrize(
        ("char", "width"),
        [("a", 1), ("日", 2), ("→", 1), ("́", 0)],  # combining acute
    )
    def test_character_widths(self, char: str, width: int):
        assert tui.char_width(char) == width

    def test_a_tiny_terminal_still_renders(self):
        """Nobody uses a 3x20 terminal, but a split pane can get close."""
        finder = tui.Finder([line_of("git push origin main")], None, query="git")

        assert finder.frame(rows=4, columns=20)

    def test_the_list_scrolls_to_keep_the_selection_visible(self):
        lines = [line_of(f"cmd-{i:03d}", ts=i) for i in range(50)]
        finder = tui.Finder(lines, None, query="cmd")
        for _ in range(30):
            finder.apply(tui.decode("\x0e"))

        frame = finder.frame(rows=12, columns=80)

        assert "▸" in frame

    @pytest.mark.parametrize(
        ("delta", "expected"),
        [
            (0, "now"),
            (59, "now"),
            (60, "1m"),
            (3600, "1h"),
            (86400, "1d"),
            (8 * 86400, "1w"),
            (400 * 86400, "1y"),
        ],
    )
    def test_relative_time_stays_narrow(self, delta: int, expected: str):
        now = time.time()

        assert tui.relative_time(int(now) - delta, now) == expected


# --- The real terminal -------------------------------------------------------


REPO_ROOT = Path(__file__).resolve().parent.parent
MEM_BIN = Path(sys.executable).with_name("mem")

requires_binary = pytest.mark.skipif(
    not MEM_BIN.exists(), reason="the `mem` console script is not installed"
)


def _adopt_controlling_tty() -> None:
    """Make the inherited pty this process's *controlling* terminal.

    Runs in the child between fork and exec. Without it the test is testing
    nothing: the finder opens ``/dev/tty``, which resolves to the controlling
    terminal of the session — the one pytest was launched from — not to
    whatever pty was handed over as stdin. The child would draw its interface
    on the developer's screen and read keys nobody is sending.
    """
    os.setsid()
    fd = os.open(os.ttyname(0), os.O_RDWR)
    try:
        fcntl.ioctl(fd, termios.TIOCSCTTY, 0)
    except OSError:
        # On BSD/macOS the open() above already claimed it for a session
        # with no controlling terminal, and the ioctl is redundant.
        pass
    finally:
        os.close(fd)


def _spawn_finder(home: Path, worker: int, args: list[str] | None = None):
    """Start `mem tui` attached to *worker* as its controlling terminal."""
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["TERM"] = "xterm"
    env.pop("MEM_DIR", None)
    return subprocess.Popen(
        [str(MEM_BIN), "tui", *(args or [])],
        stdin=worker,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        cwd=str(home),
        text=True,
        preexec_fn=_adopt_controlling_tty,
    )


def drive_tty(home: Path, keys: str, timeout: float = 20.0) -> tuple[str, int]:
    """Run `mem tui` under a real pty, send keys, return (stdout, exit code).

    stdout is captured through a pipe while the interface is drawn on the
    pty, which is the whole contract: the result channel and the display
    channel are different file descriptors.
    """
    controller, worker = pty.openpty()
    proc = _spawn_finder(home, worker)
    os.close(worker)
    drained = _drain(controller)
    try:
        # Wait for the first frame before typing, the way a person would.
        # Without it the keys can arrive before raw mode is set and the
        # terminal line-buffers them into oblivion.
        _wait_for_frame(drained, time.monotonic() + timeout)
        for key in keys:
            os.write(controller, key.encode())
            time.sleep(0.02)
        stdout, _ = proc.communicate(timeout=timeout)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=timeout)
        os.close(controller)
    return stdout, proc.returncode


def _drain(fd: int) -> list[bytes]:
    """Continuously read the pty in a thread, returning the accumulating buffer.

    A pty holds about a kilobyte. A frame is bigger than that, so a test that
    reads once and then sends keys deadlocks: the child blocks writing the
    display and never gets to read the keystroke that would end it. Somebody
    has to keep looking at the screen.
    """
    import threading

    chunks: list[bytes] = []

    def reader() -> None:
        while True:
            try:
                data = os.read(fd, 65536)
            except OSError:
                return  # the child exited and closed its side
            if not data:
                return
            chunks.append(data)

    thread = threading.Thread(target=reader, daemon=True)
    thread.start()
    return chunks


def _wait_for_frame(chunks: list[bytes], deadline: float) -> None:
    """Block until the child has drawn something."""
    while time.monotonic() < deadline and not chunks:
        time.sleep(0.02)


def plant(home: Path, commands: list[str]) -> None:
    path = home / ".mem" / "repos" / "_global.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    now = int(time.time())
    path.write_text(
        "\n".join(line_of(c, ts=now - i) for i, c in enumerate(commands)) + "\n",
        encoding="utf-8",
    )


@requires_binary
class TestUnderARealTerminal:
    """What a pure-Python test cannot reach: raw mode and the tty split."""

    def test_accepting_prints_only_the_command_to_stdout(self, tmp_path: Path):
        """stdout carries the result and nothing else — the widget depends on it."""
        home = tmp_path / "home"
        home.mkdir()
        plant(home, ["git push origin main", "ls -la"])

        stdout, code = drive_tty(home, "git\r")

        assert code == 0
        assert stdout.strip() == "git push origin main"
        assert "\x1b" not in stdout, "the interface leaked into the result channel"

    def test_cancelling_prints_nothing_and_exits_nonzero(self, tmp_path: Path):
        """The shell widget keys off the exit status to leave the line alone."""
        home = tmp_path / "home"
        home.mkdir()
        plant(home, ["git push origin main"])

        stdout, code = drive_tty(home, "\x1b")

        assert code == 1
        assert stdout == ""

    def test_the_terminal_is_restored_on_exit(self, tmp_path: Path):
        """Leaving a shell in raw mode with no echo is unrecoverable for a user.

        Both ends of a pty share one set of termios flags, and the reading is
        taken from the *controller* on purpose: macOS revokes the worker when
        the session leader holding it as a controlling terminal exits, so
        ``tcgetattr`` on that side fails with ENOTTY the moment the finder
        quits — which is exactly when this needs to be measured.
        """
        home = tmp_path / "home"
        home.mkdir()
        plant(home, ["echo hello"])

        controller, worker = pty.openpty()
        before = termios.tcgetattr(controller)
        proc = _spawn_finder(home, worker)
        os.close(worker)
        drained = _drain(controller)
        try:
            _wait_for_frame(drained, time.monotonic() + 20)
            os.write(controller, b"\x1b")
            proc.communicate(timeout=20)
            after = termios.tcgetattr(controller)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=20)
            os.close(controller)

        assert after[3] == before[3], (
            "terminal local flags changed — echo and canonical mode are what "
            f"a user needs back: {before[3]} -> {after[3]}"
        )

    def test_it_works_without_a_controlling_terminal(self, tmp_path: Path):
        """Piped or run from a task runner, it answers instead of crashing."""
        home = tmp_path / "home"
        home.mkdir()
        plant(home, ["git push origin main", "ls"])

        env = dict(os.environ)
        env["HOME"] = str(home)
        env.pop("MEM_DIR", None)

        result = subprocess.run(
            [str(MEM_BIN), "tui", "--", "git"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            env=env,
            cwd=str(home),
            timeout=60,
            start_new_session=True,  # detach from the test runner's tty
        )

        assert result.returncode == 0
        assert result.stdout.strip() == "git push origin main"


@requires_binary
class TestStartupCost:
    """Ctrl+R is muscle memory; a perceptible pause makes it feel broken."""

    def test_the_finder_does_not_import_click_rich_or_pydantic(self):
        """The whole reason `_entry.py` exists.

        Asserted on the imported module list rather than on a stopwatch, so
        it holds on a loaded CI box: the cost is those three imports, and the
        contract is that the fast path never pays them.
        """
        code = (
            "import sys; import mem.tui; "
            "print([m for m in ('click','rich','pydantic') if m in sys.modules])"
        )
        result = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, timeout=60
        )

        assert result.stdout.strip() == "[]", result.stdout

    def test_the_entry_point_dispatches_without_importing_the_cli(self):
        """`mem tui` must reach the finder without Click ever being imported."""
        code = "\n".join(
            [
                "import sys",
                "sys.argv = ['mem', 'tui', '--help']",
                "import mem._entry",
                "try:",
                "    mem._entry.main()",
                "except SystemExit:",
                "    pass",
                "print('CLI_IMPORTED', 'mem.cli' in sys.modules)",
            ]
        )
        result = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, timeout=60
        )

        assert "CLI_IMPORTED False" in result.stdout, result.stdout + result.stderr

    def test_the_full_cli_still_works_through_the_new_entry_point(self, tmp_path: Path):
        env = dict(os.environ)
        env["HOME"] = str(tmp_path)
        env.pop("MEM_DIR", None)

        result = subprocess.run(
            [str(MEM_BIN), "--version"],
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )

        assert result.returncode == 0
        assert result.stdout.startswith("mem, version ")


class TestKeyReader:
    """Escape sequences and multi-byte input, driven through a real pipe.

    ``KeyReader`` exists because ``TextIOWrapper.read(1)`` will not return a
    lone ``"\\r"`` — it waits for the next byte to decide between a carriage
    return and the start of ``"\\r\\n"``, and Enter from a raw terminal is
    exactly that lone ``"\\r"``. A pipe reproduces the byte stream without
    needing a terminal.
    """

    @staticmethod
    def reader_over(data: bytes) -> tui.KeyReader:
        read_fd, write_fd = os.pipe()
        os.write(write_fd, data)
        os.close(write_fd)
        return tui.KeyReader(read_fd)

    def test_enter_arrives_as_a_single_carriage_return(self):
        """The regression that hung the finder on its most important key."""
        assert self.reader_over(b"\r").read_key() == "\r"

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            (b"g", "g"),
            (b"\x7f", "\x7f"),
            (b"\x1b[A", "\x1b[A"),  # up arrow
            (b"\x1b[B", "\x1b[B"),  # down arrow
            (b"\x1bOA", "\x1bOA"),  # up arrow, application cursor mode
            (b"\x1b", "\x1b"),  # a bare Escape keypress
            (b"\x1bx", "\x1bx"),  # alt-x: escape plus one character
        ],
    )
    def test_a_sequence_is_returned_as_one_token(self, raw: bytes, expected: str):
        """Read byte by byte, an arrow key would insert `[A` into the query."""
        assert self.reader_over(raw).read_key() == expected

    def test_a_multibyte_character_is_one_keypress(self):
        """Pasting `é` delivers two bytes, neither of which is a character."""
        assert self.reader_over("é".encode()).read_key() == "é"

    def test_consecutive_keys_are_returned_in_order(self):
        reader = self.reader_over(b"gi\x1b[Bt\r")

        assert [reader.read_key() for _ in range(5)] == ["g", "i", "\x1b[B", "t", "\r"]

    def test_a_closed_terminal_reads_as_empty(self):
        """The finder must exit rather than spin when its terminal goes away."""
        assert self.reader_over(b"").read_key() == ""


class TestRunLoop:
    """The loop itself, driven with a scripted reader and an in-memory screen."""

    class ScriptedKeys:
        def __init__(self, keys: str) -> None:
            self.keys = list(keys)

        def read_key(self) -> str:
            return self.keys.pop(0) if self.keys else ""

    def test_a_session_that_types_and_accepts_returns_the_command(self):
        import io

        finder = tui.Finder([line_of("git push origin main"), line_of("ls")], None)
        screen = io.StringIO()

        choice = tui.run(finder, self.ScriptedKeys("git\r"), screen)

        assert choice == "git push origin main"
        assert "git push" in strip_ansi(screen.getvalue())

    def test_cancelling_returns_nothing(self):
        import io

        finder = tui.Finder([line_of("git push")], None)

        assert tui.run(finder, self.ScriptedKeys("\x1b"), io.StringIO()) is None

    def test_the_loop_ends_when_the_terminal_closes(self):
        """No keys left and no terminal: exit, do not spin forever."""
        import io

        finder = tui.Finder([line_of("git push")], None)

        assert tui.run(finder, self.ScriptedKeys(""), io.StringIO()) is None

    def test_accepting_with_no_results_returns_nothing(self):
        import io

        finder = tui.Finder([line_of("ls")], None, query="nomatch")

        assert tui.run(finder, self.ScriptedKeys("\r"), io.StringIO()) is None


class TestRepoDetection:
    """Repo detection walks the tree instead of spawning git.

    `mem.capture.get_git_repo` is the authority elsewhere, but importing it
    costs the module graph this finder exists to skip, and spawning `git`
    costs milliseconds that are not in the budget.
    """

    def test_it_finds_the_repo_root_from_a_subdirectory(self, tmp_path, monkeypatch):
        repo = tmp_path / "project"
        (repo / ".git").mkdir(parents=True)
        deep = repo / "src" / "pkg"
        deep.mkdir(parents=True)
        monkeypatch.chdir(deep)

        assert tui._current_repo() == str(repo)

    def test_outside_a_repo_there_is_no_context(self, tmp_path, monkeypatch):
        plain = tmp_path / "not-a-repo"
        plain.mkdir()
        monkeypatch.chdir(plain)

        # tmp_path itself is not inside a repo on any supported CI image.
        assert tui._current_repo() in (None, str(plain))

    def test_a_git_file_counts_too(self, tmp_path, monkeypatch):
        """Worktrees and submodules have a `.git` *file*, not a directory."""
        repo = tmp_path / "worktree"
        repo.mkdir()
        (repo / ".git").write_text("gitdir: /elsewhere\n")
        monkeypatch.chdir(repo)

        assert tui._current_repo() == str(repo)

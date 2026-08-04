"""Contract tests for shell-history import (``mem.history`` + ``mem import``).

This is the feature that decides whether a fresh install of mem is useful on
day one or on day thirty, and it is the only code path that writes tens of
thousands of lines into ``~/.mem`` in one go. Three properties are therefore
tested harder than the rest:

- **The parsers survive real files.** History files are not a format anyone
  designed; they are whatever a shell happened to append, including invalid
  UTF-8, truncated entries, and two formats interleaved in one file.
- **Timestamps are honest.** Nothing may be stamped "now" that did not happen
  now, and file order — the one thing every history file really does record —
  must survive the import.
- **Importing twice is a no-op.** Frequency is 40% of the ranking formula, so
  a second import that doubled every count would quietly corrupt every search
  result mem ever returns.

Everything runs against the autouse ``tmp_mem_dir`` fixture, which redirects
both ``storage.MEM_DIR`` and ``$HOME``; ``Path.home()`` inside the importer
therefore lands in a throwaway directory and never reads the developer's own
``~/.zsh_history``.
"""

from __future__ import annotations

import json
import os
import stat
import time
from pathlib import Path
from typing import Iterator
from unittest.mock import patch

import pytest
from click.testing import CliRunner
from conftest import make_command

from mem import history, storage
from mem.cli import cli

DAY = 86400

# A plausible epoch far enough in the past that no test can collide with
# "now", and old enough that retention would delete it if it were captured.
T0 = 1_700_000_000


# --- helpers ---------------------------------------------------------------


@pytest.fixture
def home() -> Path:
    """The isolated ``$HOME`` the conftest fixture installed for this test."""
    return Path(os.environ["HOME"])


@pytest.fixture
def runner() -> CliRunner:
    """A CliRunner with a wide terminal so Rich never wraps assertion targets."""
    return CliRunner(env={"COLUMNS": "200"})


@pytest.fixture
def interactive() -> Iterator[None]:
    """Pretend stdin is a terminal so the confirmation prompt is reachable."""
    with patch("mem.cli._is_interactive", return_value=True):
        yield


def write_history(path: Path, text: str) -> Path:
    """Write a history file, creating its parents. Returns the path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def zsh_history(home: Path, text: str) -> Path:
    """Install ``text`` as the user's ~/.zsh_history."""
    return write_history(home / ".zsh_history", text)


def bash_history(home: Path, text: str) -> Path:
    """Install ``text`` as the user's ~/.bash_history."""
    return write_history(home / ".bash_history", text)


def fish_history(home: Path, text: str) -> Path:
    """Install ``text`` as the user's fish history."""
    return write_history(home / ".local" / "share" / "fish" / "fish_history", text)


def commands(result: history.ParseResult) -> list[str]:
    """The command text of every entry a parser recovered."""
    return [e.command for e in result.entries]


def stored_commands() -> list[str]:
    """Every command text currently in the store, in file order."""
    return [cmd.command for cmd in storage.read_all_commands()]


def import_now(sources: list[tuple[str, Path]]) -> history.ImportPlan:
    """Build and apply a plan in one step, returning the plan."""
    plan = history.build_plan(sources)
    history.apply_plan(plan)
    return plan


# ---------------------------------------------------------------------------
# zsh
# ---------------------------------------------------------------------------


class TestZshParser:
    """zsh writes two formats into the same file; both must be readable."""

    def test_extended_history_yields_command_and_timestamp(self) -> None:
        """`: <epoch>:<elapsed>;<command>` is zsh's EXTENDED_HISTORY line."""
        result = history.parse_zsh(f": {T0}:0;git push origin main\n")

        assert result.entries == [history.HistoryEntry("git push origin main", T0)]
        assert result.failed_lines == 0

    def test_elapsed_seconds_are_not_mistaken_for_the_timestamp(self) -> None:
        """The first number is when it started; the second is how long it took."""
        result = history.parse_zsh(f": {T0}:137;sleep 137\n")

        assert result.entries[0].ts == T0

    def test_plain_history_line_is_a_command_with_no_timestamp(self) -> None:
        """Without EXTENDED_HISTORY, a line is just the command."""
        result = history.parse_zsh("git status\n")

        assert result.entries == [history.HistoryEntry("git status", None)]

    def test_both_formats_are_detected_per_line_in_one_file(self) -> None:
        """EXTENDED_HISTORY is usually enabled long after the file was created.

        The undated lines above the dated ones are the user's older history,
        and a per-file format guess would have thrown away one half or the
        other.
        """
        result = history.parse_zsh(f"old bare command\n: {T0}:0;newer command\n")

        assert commands(result) == ["old bare command", "newer command"]
        assert result.entries[0].ts is None
        assert result.entries[1].ts == T0

    def test_semicolons_inside_the_command_are_preserved(self) -> None:
        """Only the first `;` separates the header from the command."""
        result = history.parse_zsh(f": {T0}:0;cd /tmp; ls; cd -\n")

        assert commands(result) == ["cd /tmp; ls; cd -"]

    def test_multi_line_entry_is_joined_on_the_backslash(self) -> None:
        """zsh stores an embedded newline as a trailing backslash."""
        raw = f": {T0}:0;for f in *; do\\\n  echo $f\\\ndone\n"

        result = history.parse_zsh(raw)

        assert commands(result) == ["for f in *; do\n  echo $f\ndone"]
        assert result.entries[0].ts == T0

    def test_multi_line_entry_works_in_plain_history_too(self) -> None:
        """Continuations are a property of the file, not of the format."""
        result = history.parse_zsh("echo one\\\necho two\n")

        assert commands(result) == ["echo one\necho two"]

    def test_escaped_backslash_at_end_of_line_does_not_continue(self) -> None:
        """`echo a\\\\` ends with a literal backslash and is a whole command.

        Counting parity is what separates it from a continuation; a naive
        ``endswith("\\\\")`` swallowed the following command into this one.
        """
        result = history.parse_zsh(f": {T0}:0;echo a\\\\\n: {T0 + 1}:0;echo b\n")

        assert commands(result) == ["echo a\\\\", "echo b"]

    def test_blank_line_inside_a_continuation_is_part_of_the_command(self) -> None:
        """Inside a multi-line entry every line is command text, blank or not."""
        result = history.parse_zsh("echo <<EOF\\\n\\\nEOF\n")

        assert commands(result) == ["echo <<EOF\n\nEOF"]

    def test_blank_lines_between_entries_are_skipped(self) -> None:
        """Blank separators are not commands."""
        result = history.parse_zsh(f"\n\n: {T0}:0;ls\n\n")

        assert commands(result) == ["ls"]

    def test_file_truncated_mid_continuation_keeps_what_survived(self) -> None:
        """A dangling backslash at EOF must not discard the entry."""
        result = history.parse_zsh(f": {T0}:0;echo start\\\n")

        assert commands(result) == ["echo start"]

    def test_corrupt_extended_header_is_counted_not_guessed(self) -> None:
        """Concurrent shells interleave writes and truncate a header mid-line."""
        result = history.parse_zsh(f": {T0}:0\n: {T0 + 1}:0;ls\n")

        assert commands(result) == ["ls"]
        assert result.failed_lines == 1

    def test_command_starting_with_the_colon_builtin_is_kept(self) -> None:
        """`:` is a real shell builtin; a `: ` prefix alone is not corruption."""
        result = history.parse_zsh(": is a no-op\n")

        assert commands(result) == [": is a no-op"]
        assert result.failed_lines == 0

    def test_crlf_line_endings_do_not_leak_into_the_command(self) -> None:
        """A history file edited on another platform still parses."""
        result = history.parse_zsh(f": {T0}:0;ls -la\r\n")

        assert commands(result) == ["ls -la"]

    def test_empty_file_yields_nothing_and_no_failures(self) -> None:
        """An empty history is a valid history."""
        result = history.parse_zsh("")

        assert result.entries == []
        assert result.failed_lines == 0


# ---------------------------------------------------------------------------
# bash
# ---------------------------------------------------------------------------


class TestBashParser:
    """bash writes bare commands, with `#<epoch>` lines when HISTTIMEFORMAT is set."""

    def test_bare_commands_are_imported_without_timestamps(self) -> None:
        """The default bash history records no time at all."""
        result = history.parse_bash("ls -la\ngit status\n")

        assert commands(result) == ["ls -la", "git status"]
        assert [e.ts for e in result.entries] == [None, None]

    def test_timestamp_line_dates_the_command_that_follows_it(self) -> None:
        """`#<epoch>` precedes its command; it never trails it."""
        result = history.parse_bash(f"#{T0}\nls -la\n#{T0 + 60}\ngit status\n")

        assert [(e.command, e.ts) for e in result.entries] == [
            ("ls -la", T0),
            ("git status", T0 + 60),
        ]

    def test_a_timestamp_dates_exactly_one_command(self) -> None:
        """Reusing it would silently stamp later, undated commands with it."""
        result = history.parse_bash(f"#{T0}\nls\ngit status\n")

        assert result.entries[0].ts == T0
        assert result.entries[1].ts is None

    def test_user_typed_comment_is_a_command_not_a_timestamp(self) -> None:
        """bash stores `# note to self` verbatim as a history entry."""
        result = history.parse_bash("# note to self\n")

        assert commands(result) == ["# note to self"]

    def test_short_numeric_comment_is_not_read_as_an_epoch(self) -> None:
        """`#42` is a comment. Real HISTTIMEFORMAT epochs are ten digits."""
        result = history.parse_bash("#42\nls\n")

        assert commands(result) == ["#42", "ls"]

    def test_blank_lines_are_skipped(self) -> None:
        """Trailing newlines are not empty commands."""
        result = history.parse_bash("ls\n\n\n")

        assert commands(result) == ["ls"]

    def test_empty_file_yields_nothing(self) -> None:
        """An empty history is a valid history."""
        assert history.parse_bash("").entries == []


# ---------------------------------------------------------------------------
# fish
# ---------------------------------------------------------------------------


class TestFishParser:
    """fish writes a YAML subset — parsed directly, with no YAML dependency."""

    def test_cmd_and_when_are_read(self) -> None:
        """The two keys mem stores."""
        result = history.parse_fish(f"- cmd: git push\n  when: {T0}\n")

        assert result.entries == [history.HistoryEntry("git push", T0)]

    def test_paths_block_is_ignored(self) -> None:
        """fish records the files a command touched; mem has nowhere to put them."""
        raw = f"- cmd: vim a.py\n  when: {T0}\n  paths:\n    - a.py\n    - b.py\n"

        result = history.parse_fish(raw)

        assert result.entries == [history.HistoryEntry("vim a.py", T0)]

    def test_multiple_entries_are_separated_by_their_cmd_lines(self) -> None:
        """A new `- cmd:` closes the previous entry."""
        raw = f"- cmd: one\n  when: {T0}\n- cmd: two\n  when: {T0 + 1}\n"

        assert [(e.command, e.ts) for e in history.parse_fish(raw).entries] == [
            ("one", T0),
            ("two", T0 + 1),
        ]

    def test_escaped_newline_is_restored(self) -> None:
        r"""fish escapes an embedded newline as the two characters ``\n``."""
        result = history.parse_fish(r"- cmd: echo a\necho b" + f"\n  when: {T0}\n")

        assert commands(result) == ["echo a\necho b"]

    def test_escaped_backslash_is_restored_and_does_not_eat_the_next_char(
        self,
    ) -> None:
        r"""``\\n`` is a literal backslash followed by an ``n``, not a newline."""
        result = history.parse_fish(r"- cmd: grep '\\n' file" + f"\n  when: {T0}\n")

        assert commands(result) == [r"grep '\n' file"]

    def test_entry_without_when_is_kept_undated(self) -> None:
        """fish omits `when` for entries written by very old versions."""
        result = history.parse_fish("- cmd: git status\n")

        assert result.entries == [history.HistoryEntry("git status", None)]

    def test_unreadable_when_costs_the_timestamp_not_the_command(self) -> None:
        """A corrupt date is not a reason to lose the command."""
        result = history.parse_fish("- cmd: git status\n  when: not-a-number\n")

        assert result.entries == [history.HistoryEntry("git status", None)]
        assert result.failed_lines == 1

    def test_a_file_that_is_not_fish_history_is_reported_as_unparsed(self) -> None:
        """Pointing --shell fish at a zsh history must be visible, not silent."""
        result = history.parse_fish("git status\nls -la\n")

        assert result.entries == []
        assert result.failed_lines == 2

    def test_blank_lines_between_entries_are_skipped(self) -> None:
        """fish separates blocks with blank lines when the file is hand-edited."""
        raw = f"- cmd: one\n  when: {T0}\n\n- cmd: two\n  when: {T0 + 1}\n"

        assert commands(history.parse_fish(raw)) == ["one", "two"]

    def test_empty_file_yields_nothing(self) -> None:
        """An empty history is a valid history."""
        assert history.parse_fish("").entries == []


# ---------------------------------------------------------------------------
# Encoding and I/O
# ---------------------------------------------------------------------------


class TestFileReading:
    """Real history files are bytes, not text, and mem must never crash on them."""

    def test_invalid_utf8_is_replaced_not_raised(self, home: Path) -> None:
        """A pasted binary blob or a latin-1 filename must not abort the import."""
        path = home / ".bash_history"
        path.write_bytes(b"ls -la\ncat caf\xe9.txt\ngit \xff\xfe status\n")

        text = history.read_history_text(path)

        assert "ls -la" in text
        assert len(history.parse_bash(text).entries) == 3

    def test_undecodable_bytes_survive_the_round_trip_to_jsonl(
        self, home: Path
    ) -> None:
        """The replacement character must be JSON-encodable.

        ``surrogateescape`` would decode these bytes too, and then explode
        inside ``json.dumps`` — one layer below where anyone would look.
        """
        path = home / ".bash_history"
        path.write_bytes(b"cat caf\xe9.txt\n")

        import_now([("bash", path)])

        line = (storage.MEM_DIR / "repos" / "_global.jsonl").read_text(encoding="utf-8")
        assert json.loads(line)["command"].startswith("cat caf")

    def test_missing_file_is_reported_not_silently_empty(self, home: Path) -> None:
        """A path that does not exist yields an error on its FilePlan."""
        plan = history.build_plan([("zsh", home / "nope" / ".zsh_history")])

        assert plan.total == 0
        assert plan.files[0].error is not None

    def test_unreadable_file_does_not_abort_the_other_shells(self, home: Path) -> None:
        """One broken source must not cost the user the sources that do work."""
        blocked = write_history(home / ".zsh_history", "should not be read\n")
        blocked.chmod(0o000)
        try:
            bash_history(home, "ls -la\n")
            plan = history.build_plan(
                [("zsh", blocked), ("bash", home / ".bash_history")]
            )
        finally:
            blocked.chmod(0o600)

        assert plan.files[0].error is not None
        assert [c.command for c in plan.files[1].commands] == ["ls -la"]

    def test_empty_file_imports_nothing_without_error(self, home: Path) -> None:
        """A zero-byte history is not an error condition."""
        plan = history.build_plan([("zsh", write_history(home / ".zsh_history", ""))])

        assert plan.total == 0
        assert plan.files[0].error is None


# ---------------------------------------------------------------------------
# Timestamps
# ---------------------------------------------------------------------------


class TestTimestampHonesty:
    """An imported command may not claim to be more recent than it is."""

    def test_undated_entries_are_not_stamped_now(self, home: Path) -> None:
        """The whole point: `mem git` must still surface today's work first."""
        path = zsh_history(home, "one\ntwo\nthree\n")

        plan = history.build_plan([("zsh", path)])

        now = int(time.time())
        assert all(cmd.ts < now for cmd in plan.files[0].commands)

    def test_undated_entries_keep_their_file_order(self, home: Path) -> None:
        """Order is the one thing every history file really does record."""
        path = zsh_history(home, "first\nsecond\nthird\n")

        stamps = [
            cmd.ts for cmd in history.build_plan([("zsh", path)]).files[0].commands
        ]

        assert stamps == sorted(stamps)

    def test_undated_entries_before_a_dated_one_are_older_than_it(
        self, home: Path
    ) -> None:
        """The bare lines above the first `: <epoch>;` line came first."""
        path = zsh_history(home, f"older\n: {T0}:0;newer\n")

        cmds = history.build_plan([("zsh", path)]).files[0].commands

        assert cmds[0].ts < T0
        assert cmds[1].ts == T0

    def test_undated_entries_after_a_dated_one_are_newer_than_it(
        self, home: Path
    ) -> None:
        """Back-dating everything below the oldest known time would invert them.

        bash writes `#<epoch>` only while HISTTIMEFORMAT is set, so a file can
        easily hold dated entries followed by undated ones that genuinely ran
        later.
        """
        path = bash_history(home, f"#{T0}\ndated\nlater one\nlater two\n")

        cmds = history.build_plan([("bash", path)]).files[0].commands

        assert cmds[0].ts == T0
        assert T0 < cmds[1].ts < cmds[2].ts

    def test_undated_run_between_two_dated_entries_lands_between_them(
        self, home: Path
    ) -> None:
        """The nearest known times on either side bracket the unknown ones."""
        path = zsh_history(home, f": {T0}:0;a\nb\nc\n: {T0 + 10_000}:0;d\n")

        cmds = history.build_plan([("zsh", path)]).files[0].commands

        assert T0 < cmds[1].ts < cmds[2].ts < T0 + 10_000

    def test_file_creation_time_bounds_a_fully_undated_file(self, home: Path) -> None:
        """Nothing in a file was typed before the file existed.

        The filesystem's birth time is real evidence, so it is used instead of
        an invented window when it is older than the last write.
        """
        path = zsh_history(home, "a\nb\nc\n")
        birth = getattr(path.stat(), "st_birthtime", None)
        if birth is None:
            pytest.skip("filesystem does not record a creation time")
        os.utime(path, (birth + 10 * DAY, birth + 10 * DAY))

        cmds = history.build_plan([("zsh", path)]).files[0].commands

        assert all(int(birth) <= cmd.ts <= int(birth) + 10 * DAY for cmd in cmds)

    def test_timestamp_in_the_future_is_discarded(self, home: Path) -> None:
        """Recency decays backwards from now, so a future date wins forever."""
        future = int(time.time()) + 365 * DAY
        path = zsh_history(home, f": {future}:0;impossible\n")

        cmds = history.build_plan([("zsh", path)]).files[0].commands

        assert cmds[0].ts <= int(time.time())

    def test_zero_timestamp_is_treated_as_unknown(self, home: Path) -> None:
        """`: 0:0;cmd` records the absence of a time, not the epoch."""
        path = zsh_history(home, f": 0:0;forgotten\n: {T0}:0;known\n")

        cmds = history.build_plan([("zsh", path)]).files[0].commands

        assert cmds[0].ts > 0

    def test_a_file_older_than_its_own_entries_still_orders_them(
        self, home: Path
    ) -> None:
        """A restored backup can carry an mtime that predates what it contains.

        The bracketing window collapses; ordering must survive anyway.
        """
        path = zsh_history(home, f": {T0}:0;dated\nundated one\nundated two\n")
        os.utime(path, (T0 - 10 * DAY, T0 - 10 * DAY))

        cmds = history.build_plan([("zsh", path)]).files[0].commands

        assert cmds[0].ts <= cmds[1].ts < cmds[2].ts

    def test_a_file_that_vanished_mid_import_does_not_crash(self, home: Path) -> None:
        """stat() can fail between reading the file and dating its entries."""
        birth, mtime = history._file_times(home / "gone.hist")

        assert birth is None
        assert mtime <= int(time.time())

    def test_no_command_is_ever_dropped_for_lacking_a_timestamp(
        self, home: Path
    ) -> None:
        """A plain-history user's entire past is undated; dropping it is absurd."""
        path = bash_history(home, "".join(f"cmd{i}\n" for i in range(50)))

        assert history.build_plan([("bash", path)]).total == 50


# ---------------------------------------------------------------------------
# What an imported command claims about itself
# ---------------------------------------------------------------------------


class TestImportedCommandShape:
    """Imported commands must not pretend to know what the file never recorded."""

    def test_exit_code_and_duration_are_unknown_not_zero(self, home: Path) -> None:
        """A `0` exit code would claim every imported command succeeded."""
        path = zsh_history(home, f": {T0}:5;flaky-deploy\n")

        cmd = history.build_plan([("zsh", path)]).files[0].commands[0]

        assert cmd.exit_code is None
        assert cmd.duration_ms is None

    def test_elapsed_seconds_are_not_repurposed_as_a_duration(self, home: Path) -> None:
        """zsh's elapsed field is whole seconds and is often 0 for real work.

        Promoting it to ``duration_ms`` would report a 5-second command as
        having taken 5 milliseconds.
        """
        path = zsh_history(home, f": {T0}:5;sleep 5\n")

        assert (
            history.build_plan([("zsh", path)]).files[0].commands[0].duration_ms is None
        )

    def test_repo_is_unknown_so_the_command_goes_to_the_global_store(
        self, home: Path
    ) -> None:
        """History files record no directory. Guessing the current repo is a lie
        the ranking's context term would then act on."""
        import_now([("zsh", zsh_history(home, f": {T0}:0;make test\n"))])

        assert (storage.MEM_DIR / "repos" / "_global.jsonl").exists()
        assert list((storage.MEM_DIR / "repos").glob("*.jsonl")) == [
            storage.MEM_DIR / "repos" / "_global.jsonl"
        ]

    def test_imported_flag_marks_the_provenance(self, home: Path) -> None:
        """Everything downstream needs to tell measured data from imported."""
        import_now([("zsh", zsh_history(home, f": {T0}:0;make test\n"))])

        assert all(cmd.imported for cmd in storage.read_all_commands())

    def test_hook_captured_commands_stay_unmarked(self) -> None:
        """The new field defaults to False, so old JSONL still reads correctly."""
        storage.append_command(make_command(command="git status"))

        assert [cmd.imported for cmd in storage.read_all_commands()] == [False]

    def test_pre_existing_jsonl_without_the_new_fields_still_loads(self) -> None:
        """Every line written by an earlier version must keep validating."""
        path = storage.MEM_DIR / "repos" / "_global.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "command": "git status",
                    "ts": T0,
                    "dir": "/w",
                    "repo": None,
                    "exit_code": 0,
                    "duration_ms": 12,
                }
            )
            + "\n",
            encoding="utf-8",
        )

        loaded = list(storage.read_all_commands())

        assert [
            (c.command, c.exit_code, c.duration_ms, c.imported) for c in loaded
        ] == [("git status", 0, 12, False)]

    def test_import_is_searchable_immediately(self, home: Path) -> None:
        """The entire point of the feature, asserted end to end."""
        from mem.search import search

        import_now([("zsh", zsh_history(home, f": {T0}:0;kubectl get pods\n"))])

        assert [cmd.command for cmd, _ in search("kubectl")] == ["kubectl get pods"]


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


class TestIdempotency:
    """Frequency is 40% of the ranking. Double-counting corrupts every result."""

    def test_importing_the_same_file_twice_writes_nothing_the_second_time(
        self, home: Path
    ) -> None:
        """The headline contract."""
        path = zsh_history(home, f": {T0}:0;git push\n: {T0 + 1}:0;git pull\n")

        first = import_now([("zsh", path)])
        second = import_now([("zsh", path)])

        assert first.total == 2
        assert second.total == 0
        assert second.duplicates == 2
        assert stored_commands() == ["git push", "git pull"]

    def test_repeated_commands_keep_their_frequency_within_one_import(
        self, home: Path
    ) -> None:
        """Deduplicating by command text alone would flatten the whole signal."""
        path = bash_history(home, "git status\nls\ngit status\ngit status\n")

        import_now([("bash", path)])

        assert stored_commands().count("git status") == 3

    def test_repeated_commands_are_not_doubled_by_a_second_import(
        self, home: Path
    ) -> None:
        """Three occurrences before, three after — not six."""
        path = bash_history(home, "git status\ngit status\ngit status\n")

        import_now([("bash", path)])
        import_now([("bash", path)])

        assert stored_commands().count("git status") == 3

    def test_a_history_file_that_grew_contributes_only_its_new_entries(
        self, home: Path
    ) -> None:
        """The normal case: import, keep working, import again a month later."""
        path = bash_history(home, "git status\nls\n")
        import_now([("bash", path)])

        path.write_text("git status\nls\nmake test\ngit status\n", encoding="utf-8")
        second = import_now([("bash", path)])

        assert second.total == 2
        assert sorted(stored_commands()) == sorted(
            ["git status", "ls", "make test", "git status"]
        )

    def test_commands_already_captured_by_the_hook_are_not_re_imported(
        self, home: Path
    ) -> None:
        """A user who installs mem and imports a month later has both copies."""
        for _ in range(3):
            storage.append_command(make_command(command="npm run dev", repo=None))
        path = bash_history(home, "npm run dev\nnpm run dev\nnpm run dev\n")

        plan = import_now([("bash", path)])

        assert plan.total == 0
        assert stored_commands().count("npm run dev") == 3

    def test_the_surplus_over_what_the_hook_captured_is_still_imported(
        self, home: Path
    ) -> None:
        """Under-counting is the safe direction, but not to the point of zero."""
        storage.append_command(make_command(command="npm run dev", repo=None))
        path = bash_history(home, "npm run dev\nnpm run dev\nnpm run dev\n")

        plan = import_now([("bash", path)])

        assert plan.total == 2
        assert stored_commands().count("npm run dev") == 3

    def test_the_same_command_in_two_shells_is_counted_once_per_occurrence(
        self, home: Path
    ) -> None:
        """A user with both shells really did run it in both."""
        zsh_history(home, "make test\n")
        bash_history(home, "make test\n")
        sources = history.detect_history_files()

        first = import_now(sources)
        second = import_now(sources)

        assert first.total == 2
        assert second.total == 0


# ---------------------------------------------------------------------------
# Privacy
# ---------------------------------------------------------------------------


class TestCredentialsAreWithheld:
    """~/.mem is the last place a leaked token should get a second home."""

    SECRETS = [
        "curl -H 'Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9'",
        "export GITHUB_TOKEN=ghp_0123456789abcdefghijklmnopqrstuvwxyz",
        "mysql --password hunter2seventeen -u root",
        "psql postgres://admin:s3cr3tpass@db.internal:5432/app",
        "aws configure set aws_access_key_id AKIAIOSFODNN7EXAMPLE",
        "openai --api-key sk-abcdefghijklmnopqrstuvwxyz0123",
    ]

    @pytest.mark.parametrize("secret", SECRETS)
    def test_a_command_carrying_a_secret_is_never_written(
        self, home: Path, secret: str
    ) -> None:
        """Not stored, not partially stored, not stored in a session file."""
        import_now([("bash", bash_history(home, secret + "\n"))])

        assert stored_commands() == []

    def test_withheld_commands_are_counted_for_the_user(self, home: Path) -> None:
        """Silently dropping history would look like a parser bug."""
        path = bash_history(
            home, "ls -la\nexport API_TOKEN=ghp_0123456789abcdefghijklmnop\n"
        )

        plan = history.build_plan([("bash", path)])

        assert plan.credentials == 1
        assert plan.total == 1

    @pytest.mark.parametrize(
        "benign",
        [
            "git commit -m 'rotate the password reset template'",
            "docker run -p 8080:80 nginx",
            "ssh -o StrictHostKeyChecking=no deploy@host",
            "kubectl get secrets",
            "ls -la /Users/someone/Documents/projects/very-long-path-here",
        ],
    )
    def test_ordinary_commands_are_not_mistaken_for_secrets(
        self, home: Path, benign: str
    ) -> None:
        """A detector that eats normal history is a detector nobody keeps on."""
        import_now([("bash", bash_history(home, benign + "\n"))])

        assert stored_commands() == [benign]


# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------


class TestRetentionKeepsImports:
    """Imported history is old by definition; the age rule would erase it all."""

    def test_rotate_does_not_delete_imported_commands(self, home: Path) -> None:
        """Otherwise the first background sync undoes the import."""
        ancient = int(time.time()) - 900 * DAY
        import_now([("zsh", zsh_history(home, f": {ancient}:0;git bisect start\n"))])

        storage.rotate(keep_commands_days=90)

        assert stored_commands() == ["git bisect start"]

    def test_rotate_still_deletes_old_captured_commands(self) -> None:
        """The exemption must be for imports only, not a retention bypass."""
        storage.append_command(
            make_command(command="stale", ts=int(time.time()) - 900 * DAY, repo=None)
        )

        storage.rotate(keep_commands_days=90)

        assert stored_commands() == []

    def test_forget_still_removes_an_imported_command(self, home: Path) -> None:
        """Exempt from retention must not mean exempt from the privacy scrub."""
        import_now([("zsh", zsh_history(home, f": {T0}:0;ssh prod-box\n"))])

        assert storage.forget_commands("prod-box") == 1
        assert stored_commands() == []


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


class TestDiscovery:
    """Auto-detection must find real files and invent none."""

    def test_all_three_shells_are_detected(self, home: Path) -> None:
        """A machine that has used all three has all three imported."""
        zsh_history(home, "a\n")
        bash_history(home, "b\n")
        fish_history(home, "- cmd: c\n")

        assert [s for s, _ in history.detect_history_files()] == ["zsh", "bash", "fish"]

    def test_only_files_that_exist_are_returned(self, home: Path) -> None:
        """No history file is a normal state, not an error to paper over."""
        bash_history(home, "b\n")

        assert [s for s, _ in history.detect_history_files()] == ["bash"]

    def test_shell_filter_restricts_detection(self, home: Path) -> None:
        """`--shell zsh` must not quietly import bash as well."""
        zsh_history(home, "a\n")
        bash_history(home, "b\n")

        assert [s for s, _ in history.detect_history_files("zsh")] == ["zsh"]

    def test_a_directory_is_not_a_history_file(self, home: Path) -> None:
        """`is_file`, not `exists` — fish's path is a directory on some setups."""
        (home / ".zsh_history").mkdir(parents=True)

        assert history.detect_history_files("zsh") == []

    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            (".zsh_history", "zsh"),
            (".bash_history", "bash"),
            ("fish_history", "fish"),
            ("zsh_history.bak", "zsh"),
            ("history.txt", None),
        ],
    )
    def test_shell_is_guessed_from_the_filename(
        self, name: str, expected: str | None
    ) -> None:
        """Only used for `--file`; `None` means ask rather than guess."""
        assert history.shell_for_path(Path("/tmp") / name) == expected

    def test_an_unsupported_shell_is_rejected_loudly(self) -> None:
        """A typo must not silently import nothing."""
        with pytest.raises(ValueError):
            history.default_history_path("csh")

        with pytest.raises(ValueError):
            history.parse_history("ls\n", "csh")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


class TestImportCLI:
    """`mem import --from-shell-history` — what the user actually types."""

    def test_dry_run_writes_nothing(self, runner: CliRunner, home: Path) -> None:
        """The user must be able to look before mem touches the store."""
        zsh_history(home, f": {T0}:0;git push\n")

        result = runner.invoke(cli, ["import", "--from-shell-history", "--dry-run"])

        assert result.exit_code == 0
        assert "dry run" in result.output
        assert not (storage.MEM_DIR / "repos").exists()

    def test_dry_run_reports_the_counts_per_shell(
        self, runner: CliRunner, home: Path
    ) -> None:
        """Per-shell numbers, not one opaque total."""
        zsh_history(home, f": {T0}:0;git push\n")
        bash_history(home, "ls -la\n")

        result = runner.invoke(cli, ["import", "--from-shell-history", "--dry-run"])

        assert "zsh" in result.output
        assert "bash" in result.output
        assert "2 commands would be imported" in result.output

    def test_yes_imports_without_prompting(self, runner: CliRunner, home: Path) -> None:
        """The scriptable path, matching `mem forget -y` and `mem run -y`."""
        zsh_history(home, f": {T0}:0;git push\n")

        result = runner.invoke(cli, ["import", "--from-shell-history", "--yes"])

        assert result.exit_code == 0
        assert "Imported 1 commands" in result.output
        assert stored_commands() == ["git push"]

    def test_declining_the_confirmation_writes_nothing(
        self, runner: CliRunner, home: Path, interactive: None
    ) -> None:
        """ "Are you sure" must mean it."""
        zsh_history(home, f": {T0}:0;git push\n")

        result = runner.invoke(cli, ["import", "--from-shell-history"], input="n\n")

        assert result.exit_code == 0
        assert stored_commands() == []

    def test_accepting_the_confirmation_imports(
        self, runner: CliRunner, home: Path, interactive: None
    ) -> None:
        """The interactive happy path."""
        zsh_history(home, f": {T0}:0;git push\n")

        runner.invoke(cli, ["import", "--from-shell-history"], input="y\n")

        assert stored_commands() == ["git push"]

    def test_non_interactive_without_yes_is_an_error_not_a_hang(
        self, runner: CliRunner, home: Path
    ) -> None:
        """Piped into a script, `click.confirm` would read a stdin nobody feeds."""
        zsh_history(home, f": {T0}:0;git push\n")

        result = runner.invoke(cli, ["import", "--from-shell-history"])

        assert result.exit_code != 0
        assert "--yes" in result.output
        assert stored_commands() == []

    def test_no_history_files_reports_where_it_looked(self, runner: CliRunner) -> None:
        """An error the user can act on."""
        result = runner.invoke(cli, ["import", "--from-shell-history", "--yes"])

        assert result.exit_code != 0
        assert ".zsh_history" in result.output

    def test_file_override_imports_exactly_that_file(
        self, runner: CliRunner, home: Path, tmp_path: Path
    ) -> None:
        """For a custom HISTFILE or a history restored from a backup."""
        zsh_history(home, ": 1:0;should not be read\n")
        custom = write_history(tmp_path / "elsewhere.hist", f": {T0}:0;from backup\n")

        result = runner.invoke(
            cli,
            [
                "import",
                "--from-shell-history",
                "--file",
                str(custom),
                "--shell",
                "zsh",
                "--yes",
            ],
        )

        assert result.exit_code == 0
        assert stored_commands() == ["from backup"]

    def test_file_override_infers_the_shell_from_the_name(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        """`--shell` is an override, not a requirement."""
        custom = write_history(tmp_path / ".bash_history", f"#{T0}\nls -la\n")

        runner.invoke(
            cli, ["import", "--from-shell-history", "--file", str(custom), "--yes"]
        )

        assert stored_commands() == ["ls -la"]

    def test_unrecognisable_filename_asks_instead_of_guessing(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        """Guessing wrong here silently produces garbage entries."""
        custom = write_history(tmp_path / "dump.txt", "ls -la\n")

        result = runner.invoke(
            cli, ["import", "--from-shell-history", "--file", str(custom), "--yes"]
        )

        assert result.exit_code != 0
        assert "--shell" in result.output

    def test_shell_flag_limits_which_history_is_read(
        self, runner: CliRunner, home: Path
    ) -> None:
        """`--shell bash` imports bash and nothing else."""
        zsh_history(home, ": 1:0;zsh only\n")
        bash_history(home, "bash only\n")

        runner.invoke(
            cli, ["import", "--from-shell-history", "--shell", "bash", "--yes"]
        )

        assert stored_commands() == ["bash only"]

    def test_second_run_reports_nothing_new(
        self, runner: CliRunner, home: Path
    ) -> None:
        """The user is told why nothing happened."""
        zsh_history(home, f": {T0}:0;git push\n")
        runner.invoke(cli, ["import", "--from-shell-history", "--yes"])

        result = runner.invoke(cli, ["import", "--from-shell-history", "--yes"])

        assert result.exit_code == 0
        assert "Nothing new to import" in result.output

    def test_summary_reports_every_outcome(self, runner: CliRunner, home: Path) -> None:
        """Imported, duplicates, credentials and unparsed lines — all four."""
        zsh_history(
            home,
            f": {T0}:0;git push\n"
            f": {T0 + 1}:0\n"
            f": {T0 + 2}:0;export TOKEN=ghp_0123456789abcdefghijklmnop\n"
            f": {T0 + 3}:0;make test\n",
        )
        storage.append_command(make_command(command="git push", repo=None))

        result = runner.invoke(cli, ["import", "--from-shell-history", "--yes"])

        assert "Imported 1 commands" in result.output
        assert "1 already known" in result.output
        assert "1 that look like credentials" in result.output
        assert "1 lines could not be parsed" in result.output

    def test_markup_in_an_imported_command_does_not_corrupt_the_output(
        self, runner: CliRunner, home: Path
    ) -> None:
        """A command is data. Rich must never read `[/]` in it as a tag.

        A bare `[/]` closes a style that was never opened, which raises
        MarkupError and takes the whole command down mid-output.
        """
        zsh_history(home, f": {T0}:0;sed 's/[a-z]//g' [/] file\n")

        result = runner.invoke(cli, ["import", "--from-shell-history", "--yes"])

        assert result.exit_code == 0
        assert result.exception is None
        assert stored_commands() == ["sed 's/[a-z]//g' [/] file"]

    def test_history_flags_are_rejected_on_a_group_import(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        """`mem import runbook.json --dry-run` must not silently ignore the flag."""
        payload = tmp_path / "runbook.json"
        payload.write_text('{"name": "ops", "commands": []}', encoding="utf-8")

        result = runner.invoke(cli, ["import", str(payload), "-t", "ops", "--dry-run"])

        assert result.exit_code != 0
        assert "--from-shell-history" in result.output

    def test_a_group_file_is_rejected_in_shell_history_mode(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        """The two modes read completely different formats."""
        payload = tmp_path / "runbook.json"
        payload.write_text("{}", encoding="utf-8")

        result = runner.invoke(
            cli, ["import", str(payload), "--from-shell-history", "--yes"]
        )

        assert result.exit_code != 0
        assert "--file" in result.output

    def test_group_import_still_works(self, runner: CliRunner, tmp_path: Path) -> None:
        """The pre-existing behaviour of `mem import` is untouched."""
        payload = tmp_path / "runbook.json"
        payload.write_text(
            json.dumps({"name": "ops", "commands": [{"cmd": "make deploy"}]}),
            encoding="utf-8",
        )

        result = runner.invoke(cli, ["import", str(payload), "-t", "ops", "-g"])

        assert result.exit_code == 0
        assert "Imported 1 commands to group 'ops'" in result.output


# ---------------------------------------------------------------------------
# Bulk storage
# ---------------------------------------------------------------------------


class TestBulkAppend:
    """An import is one write of many lines, and it must obey the same rules."""

    def test_every_command_is_written_in_order(self) -> None:
        """A batched write must not reorder or drop lines."""
        cmds = [make_command(command=f"cmd{i}", repo=None) for i in range(500)]

        assert storage.append_commands(cmds) == 500
        assert stored_commands() == [f"cmd{i}" for i in range(500)]

    def test_the_history_file_is_owner_only(self) -> None:
        """A bulk write may not be the one path that leaves 0644 behind."""
        storage.append_commands([make_command(command="ls", repo=None)])

        path = storage.MEM_DIR / "repos" / "_global.jsonl"
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    def test_commands_are_split_across_their_repo_files(self) -> None:
        """Grouping by destination must not merge two repos into one file."""
        storage.append_commands(
            [
                make_command(command="a", repo="/w/one"),
                make_command(command="b", repo="/w/two"),
            ]
        )

        # Derived, not spelled out: the repo-path-to-filename mapping now
        # carries a hash suffix so two repos cannot collide, and a test that
        # hard-codes the old names asserts the scheme instead of the split.
        assert sorted(p.name for p in (storage.MEM_DIR / "repos").glob("*.jsonl")) == (
            sorted(f"{storage.repo_key(r)}.jsonl" for r in ("/w/one", "/w/two"))
        )

    def test_an_empty_batch_creates_no_file(self) -> None:
        """Importing nothing must not leave an empty _global.jsonl behind."""
        assert storage.append_commands([]) == 0
        assert not (storage.MEM_DIR / "repos" / "_global.jsonl").exists()

    def test_appending_preserves_what_was_already_there(self) -> None:
        """The import adds to history; it never replaces it."""
        storage.append_command(make_command(command="earlier", repo=None))

        storage.append_commands([make_command(command="later", repo=None)])

        assert stored_commands() == ["earlier", "later"]

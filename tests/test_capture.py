"""Tests for command capture and session tracking."""

from __future__ import annotations

import subprocess
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from conftest import make_command
from mem import storage
from mem.capture import (
    SessionTracker,
    _spawn_background_sync,
    capture_command,
    get_git_repo,
    looks_like_terminal_noise,
)


class TestGetGitRepo:
    """Tests for git repository detection."""

    def test_detects_repo_root(self, git_repo: Path):
        """Returns the absolute root path of the repository the dir belongs to.

        Contract: the *full* resolved path of the repo root, not a basename
        and not the directory that was passed in.

        This test used to call ``get_git_repo(".")`` and assert
        ``repo.endswith("/mem")`` — i.e. it only passed because pytest happened
        to be launched from the mem checkout, and it would have passed just as
        well against a broken implementation that returned ``os.getcwd()``.
        It now builds its own repository so the assertion is exact and the
        result no longer depends on the working directory.
        """
        repo = get_git_repo(str(git_repo))
        assert repo == str(git_repo)

    def test_returns_root_from_nested_subdirectory(self, git_repo: Path):
        """A nested directory resolves to the repo ROOT, not to itself."""
        nested = git_repo / "src" / "deep" / "nested"
        nested.mkdir(parents=True)

        repo = get_git_repo(str(nested))
        assert repo == str(git_repo)
        assert repo != str(nested)

    def test_distinct_repos_with_same_basename_stay_distinct(self, tmp_path: Path):
        """Same folder name under different parents must not collide.

        This is the documented reason ``get_git_repo`` returns the full path
        instead of the basename, so it deserves a test that would fail if
        someone "simplified" it to ``Path(root).name``.
        """
        a = tmp_path / "client-a" / "api"
        b = tmp_path / "client-b" / "api"
        for path in (a, b):
            path.mkdir(parents=True)
            subprocess.run(
                ["git", "init", "-q"], cwd=path, check=True, capture_output=True
            )

        repo_a = get_git_repo(str(a))
        repo_b = get_git_repo(str(b))
        assert repo_a != repo_b
        assert repo_a == str(a.resolve())
        assert repo_b == str(b.resolve())

    def test_returns_none_outside_repo(self, tmp_path):
        """Returns None when not inside a git repo."""
        outside = tmp_path / "not-a-repo"
        outside.mkdir()
        repo = get_git_repo(str(outside))
        assert repo is None

    def test_returns_none_for_nonexistent_dir(self):
        """Returns None for a directory that doesn't exist."""
        repo = get_git_repo("/nonexistent/path/that/does/not/exist")
        assert repo is None

    def test_handles_timeout(self):
        """A hanging `git` is swallowed and reported as "no repo".

        The previous version of this test opened a ``patch`` block whose body
        was a bare ``pass``, so the timeout branch was never executed — it
        asserted nothing at all. ``subprocess.TimeoutExpired`` requires
        constructor arguments, which is presumably why it was skipped.
        """
        with patch(
            "mem.capture.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="git", timeout=5),
        ):
            assert get_git_repo("/some/path") is None

    def test_handles_missing_git_binary(self):
        """Returns None when the git executable is not installed."""
        with patch(
            "mem.capture.subprocess.run",
            side_effect=FileNotFoundError("git not found"),
        ):
            assert get_git_repo("/some/path") is None

    def test_uses_a_timeout(self, git_repo: Path):
        """git is invoked with a bounded timeout so a hung repo cannot wedge the shell.

        The capture hook runs on *every* prompt; an unbounded subprocess would
        freeze the user's terminal.
        """
        with patch("mem.capture.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = str(git_repo) + "\n"
            get_git_repo(str(git_repo))

        kwargs = mock_run.call_args.kwargs
        assert kwargs["timeout"] is not None
        assert kwargs["timeout"] <= 5


class TestCaptureCommand:
    """Tests for the capture_command pipeline."""

    def test_captures_with_metadata(self, tmp_mem_dir):
        """Capture stores command with all metadata fields."""
        before = int(time.time())
        with patch(
            "mem.capture.get_git_repo", return_value="/Users/test/projects/myapp"
        ):
            capture_command(
                raw="git status",
                directory="/Users/test/myapp",
                exit_code=0,
                duration_ms=42,
            )
        after = int(time.time())

        cmds = list(storage.read_all_commands())
        assert len(cmds) == 1
        cmd = cmds[0]
        assert cmd.command == "git status"
        assert cmd.repo == "/Users/test/projects/myapp"
        assert cmd.exit_code == 0
        assert cmd.duration_ms == 42
        # `dir` is the directory the command ran in, distinct from `repo`
        assert cmd.dir == "/Users/test/myapp"
        assert before <= cmd.ts <= after

    def test_routes_to_repo_file_not_global(self, tmp_mem_dir):
        """A command inside a repo lands in repos/<sanitized>.jsonl, not _global."""
        with patch(
            "mem.capture.get_git_repo", return_value="/Users/test/projects/myapp"
        ):
            capture_command("git status", "/Users/test/projects/myapp", 0, 1)

        repo_name = storage.repo_key("/Users/test/projects/myapp")
        assert [c.command for c in storage.read_commands(repo_name)] == ["git status"]
        assert list(storage.read_commands("_global")) == []

    def test_captures_without_repo(self, tmp_mem_dir):
        """Capture works outside of a git repo."""
        with patch("mem.capture.get_git_repo", return_value=None):
            capture_command(
                raw="ls -la",
                directory="/tmp",
                exit_code=0,
                duration_ms=5,
            )

        cmds = list(storage.read_all_commands())
        assert len(cmds) == 1
        assert cmds[0].repo is None

    def test_captures_failed_commands(self, tmp_mem_dir):
        """Commands with non-zero exit codes are still captured."""
        with patch(
            "mem.capture.get_git_repo", return_value="/Users/test/projects/myapp"
        ):
            capture_command(
                raw="make build",
                directory="/Users/test/myapp",
                exit_code=2,
                duration_ms=3500,
            )

        cmds = list(storage.read_all_commands())
        assert len(cmds) == 1
        assert cmds[0].exit_code == 2

    def test_session_tracking_failure_does_not_block_capture(self, tmp_mem_dir):
        """Session tracking errors are swallowed silently."""
        with (
            patch(
                "mem.capture.get_git_repo", return_value="/Users/test/projects/myapp"
            ),
            patch(
                "mem.capture.SessionTracker.update",
                side_effect=RuntimeError("session broken"),
            ),
        ):
            # Should not raise
            capture_command(
                raw="echo hello",
                directory="/Users/test/myapp",
                exit_code=0,
                duration_ms=1,
            )

        # Command was still captured despite session error
        cmds = list(storage.read_all_commands())
        assert len(cmds) == 1


class TestTerminalNoise:
    """Mouse-report garbage must never become a captured command.

    The two strings below were found verbatim in a real store. A `claude`
    process had died with SIGSEGV (exit 139) while mouse tracking was on, so
    every later mouse movement at the prompt reached zsh as an SGR mouse
    report `ESC [ < Cb ; Cx ; Cy M`. zle swallowed the unbound `ESC [ <`
    prefix and inserted the rest; Enter ran it, the shell said 127, and the
    hook captured it — which made it "the last command that failed" in
    `mem fix`.
    """

    # The 3-group real prefix of the 1689-character line, then the same shape
    # repeated to the length that was actually stored.
    REAL_PREFIX = "35;165;16M35;160;16M35;153;17M"
    LONG_LINE = REAL_PREFIX + "".join(
        f"{button};{column};{row}M"
        for button, column, row in (
            (35, 144 - i % 140, 17 + i // 8) if i % 5 else (65, 71, 26)
            for i in range(160)
        )
    )

    @pytest.mark.parametrize(
        "command",
        [
            pytest.param("65;50;35M", id="real-short-line"),
            pytest.param(REAL_PREFIX, id="real-long-line-prefix"),
            pytest.param(LONG_LINE, id="real-long-line-shape"),
            pytest.param("35;165;16m", id="button-release"),
            pytest.param("[<65;50;35M", id="csi-prefix-minus-esc"),
            pytest.param("\x1b[<65;50;35M", id="raw-sequence-intact"),
            pytest.param("[<35;165;16M[<35;160;16M", id="prefix-on-every-group"),
            pytest.param("65;50;35M ", id="trailing-space"),
        ],
    )
    def test_matches_mouse_reports(self, command: str):
        assert looks_like_terminal_noise(command) is True

    @pytest.mark.parametrize(
        "command",
        [
            "echo 65;50;35M",
            "awk '{print $1;$2}'",
            "sleep 1;ls",
            "65;50;35M echo",
            "65;50",  # two fields is not a report
            "65;50;35",  # no final byte
            "1;2;3M;",  # a stray separator is something else
            "ls",
            "",
            "[200~ls[201~",  # bracketed paste, deliberately left out
        ],
    )
    def test_leaves_real_commands_alone(self, command: str):
        assert looks_like_terminal_noise(command) is False

    def test_the_real_short_line_is_not_captured(self, tmp_mem_dir):
        """The exact line from the store: nothing written, nothing tracked."""
        with patch("mem.capture.get_git_repo") as repo_lookup:
            capture_command("65;50;35M", "/Users/test/myapp", 127, 22)

        assert list(storage.read_all_commands()) == []
        assert not (tmp_mem_dir / ".session_state.json").exists()
        assert storage.read_sync_counter() == 0
        # Dropped before the git subprocess, not after it.
        repo_lookup.assert_not_called()

    def test_the_real_long_line_is_not_captured(self, tmp_mem_dir):
        assert len(self.LONG_LINE) > 1000
        with patch("mem.capture.get_git_repo", return_value=None):
            capture_command(self.LONG_LINE, "/Users/test/myapp", 127, 703)

        assert list(storage.read_all_commands()) == []

    def test_a_command_that_merely_contains_a_report_is_captured(self, tmp_mem_dir):
        """The filter is about the whole line, so `echo 65;50;35M` survives."""
        with patch("mem.capture.get_git_repo", return_value=None):
            capture_command("echo 65;50;35M", "/Users/test/myapp", 0, 3)

        assert [c.command for c in storage.read_all_commands()] == ["echo 65;50;35M"]


class TestSessionTracker:
    """Tests for session boundary detection and lifecycle."""

    def test_first_command_starts_session(self, tmp_mem_dir):
        """First command creates a new session state."""
        tracker = SessionTracker()
        cmd = make_command(
            command="git status", ts=1000, repo="/Users/test/projects/myapp"
        )
        tracker.update(cmd)

        state = tracker._load_state()
        assert state is not None
        assert state.commands == ["git status"]
        assert state.last_command_ts == 1000
        assert state.last_repo == "/Users/test/projects/myapp"

    def test_subsequent_commands_extend_session(self, tmp_mem_dir):
        """Commands within timeout extend the current session."""
        tracker = SessionTracker()

        tracker.update(
            make_command(
                command="git status", ts=1000, repo="/Users/test/projects/myapp"
            )
        )
        tracker.update(
            make_command(command="git diff", ts=1010, repo="/Users/test/projects/myapp")
        )
        tracker.update(
            make_command(
                command="git add .", ts=1020, repo="/Users/test/projects/myapp"
            )
        )

        state = tracker._load_state()
        assert len(state.commands) == 3
        assert state.commands == ["git status", "git diff", "git add ."]
        assert state.last_command_ts == 1020

    def test_idle_timeout_closes_session(self, tmp_mem_dir):
        """More than 300s of idle time triggers session closure."""
        tracker = SessionTracker()

        # First session
        tracker.update(
            make_command(
                command="git status", ts=1000, repo="/Users/test/projects/myapp"
            )
        )
        tracker.update(
            make_command(command="git diff", ts=1010, repo="/Users/test/projects/myapp")
        )

        # 301 seconds later — triggers session close
        tracker.update(
            make_command(
                command="make build", ts=1311, repo="/Users/test/projects/myapp"
            )
        )

        # Old session should be persisted
        sessions = list(storage.read_all_sessions())
        assert len(sessions) == 1
        assert sessions[0].commands == ["git status", "git diff"]

        # New session started
        state = tracker._load_state()
        assert state.commands == ["make build"]

    def test_repo_change_closes_session(self, tmp_mem_dir):
        """Switching git repos triggers session closure."""
        tracker = SessionTracker()

        # Working in myapp
        tracker.update(
            make_command(
                command="git status", ts=1000, repo="/Users/test/projects/myapp"
            )
        )
        tracker.update(
            make_command(command="npm test", ts=1010, repo="/Users/test/projects/myapp")
        )

        # Switch to another repo (within timeout)
        tracker.update(
            make_command(
                command="git log", ts=1020, repo="/Users/test/projects/other-repo"
            )
        )

        # myapp session should be closed
        sessions = list(storage.read_all_sessions())
        assert len(sessions) == 1
        assert sessions[0].repo == "/Users/test/projects/myapp"
        assert sessions[0].commands == ["git status", "npm test"]

        # New session for other-repo
        state = tracker._load_state()
        assert state.last_repo == "/Users/test/projects/other-repo"

    @pytest.mark.parametrize(
        ("idle", "expect_closed"),
        [(299, False), (300, False), (301, True), (600, True)],
    )
    def test_idle_boundary_is_exactly_300_seconds(
        self, tmp_mem_dir, idle: int, expect_closed: bool
    ):
        """The documented boundary is ``idle > 300``: 300s continues, 301s splits.

        Parametrised around the boundary so that widening or narrowing the
        threshold (300 -> 3000, or ``>`` -> ``>=``) fails, instead of only a
        gross change being detected.
        """
        tracker = SessionTracker()
        tracker.update(
            make_command(command="cmd1", ts=1000, repo="/Users/test/projects/myapp")
        )
        tracker.update(
            make_command(
                command="cmd2", ts=1000 + idle, repo="/Users/test/projects/myapp"
            )
        )

        sessions = list(storage.read_all_sessions())
        state = tracker._load_state()
        if expect_closed:
            assert [s.commands for s in sessions] == [["cmd1"]]
            assert state.commands == ["cmd2"]
        else:
            assert sessions == []
            assert state.commands == ["cmd1", "cmd2"]

    @pytest.mark.parametrize(
        ("duration_ms", "expect_closed"),
        [(200_000, False), (0, True)],
    )
    def test_idle_is_think_time_not_the_raw_gap(
        self, tmp_mem_dir, duration_ms: int, expect_closed: bool
    ):
        """A command that ran longer than the threshold must not end its own session.

        Timestamps are completion times. Last command at t=1000, next one
        finishing at t=1400: if it *ran* for 200 s the user paused 200 s and
        the session continues; if it ran for 0 s they paused 400 s and it
        splits. `mem fix` and `mem promote` both subtract `duration_ms`; the
        tracker did not, which ADR-012 records as the tracker being wrong.
        """
        tracker = SessionTracker()
        tracker.update(make_command(command="cmd1", ts=1000, duration_ms=0))
        tracker.update(
            make_command(command="docker build .", ts=1400, duration_ms=duration_ms)
        )

        sessions = list(storage.read_all_sessions())
        state = tracker._load_state()
        if expect_closed:
            assert [s.commands for s in sessions] == [["cmd1"]]
            assert state.commands == ["docker build ."]
        else:
            assert sessions == []
            assert state.commands == ["cmd1", "docker build ."]

    def test_session_summary_fallback(self, tmp_mem_dir):
        """Session summary falls back to first command when AI unavailable."""
        tracker = SessionTracker()

        tracker.update(
            make_command(
                command="git status", ts=1000, repo="/Users/test/projects/myapp"
            )
        )
        tracker.update(
            make_command(command="git diff", ts=1010, repo="/Users/test/projects/myapp")
        )

        # Trigger close via timeout
        with patch("mem.patterns._apple_fm_available", return_value=False):
            tracker.update(
                make_command(
                    command="new cmd", ts=1311, repo="/Users/test/projects/myapp"
                )
            )

        sessions = list(storage.read_all_sessions())
        assert len(sessions) == 1
        assert sessions[0].summary == "git status (+1 more commands)"

    def test_single_command_session_summary(self, tmp_mem_dir):
        """Single-command session uses the command itself as summary."""
        tracker = SessionTracker()

        tracker.update(
            make_command(
                command="git status", ts=1000, repo="/Users/test/projects/myapp"
            )
        )

        # Trigger close via repo change
        with patch("mem.patterns._apple_fm_available", return_value=False):
            tracker.update(
                make_command(command="ls", ts=1010, repo="/Users/test/projects/other")
            )

        sessions = list(storage.read_all_sessions())
        assert len(sessions) == 1
        assert sessions[0].summary == "git status"

    def test_corrupted_state_starts_fresh(self, tmp_mem_dir):
        """Corrupted state file is treated as no state."""
        tracker = SessionTracker()
        storage.ensure_dirs()
        tracker._state_path.write_text("{{invalid json", encoding="utf-8")

        # Should not raise, starts fresh session
        tracker.update(
            make_command(
                command="git status", ts=1000, repo="/Users/test/projects/myapp"
            )
        )

        state = tracker._load_state()
        assert state is not None
        assert state.commands == ["git status"]

    def test_empty_commands_not_persisted(self, tmp_mem_dir):
        """Sessions with empty command lists are not saved."""
        tracker = SessionTracker()

        # Manually create state with no commands
        from mem.models import SessionState

        empty_state = SessionState(
            session_id="test123",
            last_command_ts=1000,
            last_repo="/Users/test/projects/myapp",
            commands=[],
        )
        tracker._close_session(empty_state)

        sessions = list(storage.read_all_sessions())
        assert len(sessions) == 0

    def test_state_file_is_owner_only_and_written_atomically(self, tmp_mem_dir):
        """The one file mem wrote with `write_text` and the umask.

        Every other file under ~/.mem is 0600 via `atomic_write`, and
        `storage._scrub_session_state` rewrites this same file atomically
        under the lock — so the tracker was the one unlocked, non-atomic,
        world-readable writer of a file holding the user's last commands.
        """
        import os
        import stat

        old_umask = os.umask(0o022)
        try:
            tracker = SessionTracker()
            tracker.update(make_command(command="export TOKEN=hunter2", ts=1000))
        finally:
            os.umask(old_umask)

        mode = stat.S_IMODE(tracker._state_path.stat().st_mode)
        assert mode == 0o600, f"session state is readable by others: {oct(mode)}"
        leftovers = [
            p.name for p in tracker._state_path.parent.iterdir() if ".tmp" in p.name
        ]
        assert leftovers == []

    def test_state_survives_reload(self, tmp_mem_dir):
        """State persisted to disk can be loaded by a new tracker instance."""
        tracker1 = SessionTracker()
        tracker1.update(
            make_command(
                command="git status", ts=1000, repo="/Users/test/projects/myapp"
            )
        )

        # New tracker instance loads existing state
        tracker2 = SessionTracker()
        state = tracker2._load_state()
        assert state is not None
        assert state.commands == ["git status"]

        # Continue the session
        tracker2.update(
            make_command(command="git diff", ts=1010, repo="/Users/test/projects/myapp")
        )
        state = tracker2._load_state()
        assert state.commands == ["git status", "git diff"]


class TestAutoSyncTrigger:
    """Contracts for the every-N-captures background pattern sync."""

    def test_default_threshold_is_twenty_captures(self, tmp_mem_dir):
        """Sync fires on the 20th capture — not the 5th, not the 200th.

        ``SYNC_THRESHOLD`` is a documented product decision ("auto-sync runs
        every 20 captures"). Every other test patches the constant to a small
        value, which means the shipped value itself was never exercised: a
        typo turning 20 into 200 would have gone unnoticed.
        """
        with (
            patch("mem.capture._spawn_background_sync") as mock_spawn,
            patch("mem.capture.get_git_repo", return_value=None),
        ):
            for i in range(19):
                capture_command(f"cmd{i}", "/tmp", 0, 1)
            assert mock_spawn.call_count == 0, "fired before the 20th capture"

            capture_command("cmd19", "/tmp", 0, 1)
            assert mock_spawn.call_count == 1

    def test_counter_resets_after_trigger(self, tmp_mem_dir):
        """The capture counter restarts at zero once a sync is spawned."""
        with (
            patch.object(storage, "SYNC_THRESHOLD", 3),
            patch("mem.capture._spawn_background_sync"),
            patch("mem.capture.get_git_repo", return_value=None),
        ):
            for i in range(3):
                capture_command(f"cmd{i}", "/tmp", 0, 1)

        assert storage.read_sync_counter() == 0

    def test_capture_survives_sync_failure(self, tmp_mem_dir):
        """A crashing background spawn must not lose the captured command."""
        with (
            patch.object(storage, "SYNC_THRESHOLD", 1),
            patch(
                "mem.capture._spawn_background_sync",
                side_effect=OSError("fork failed"),
            ),
            patch("mem.capture.get_git_repo", return_value=None),
        ):
            capture_command("echo hi", "/tmp", 0, 1)

        assert [c.command for c in storage.read_all_commands()] == ["echo hi"]

    def test_spawn_background_sync_is_detached_and_silent(self):
        """`mem _sync` runs in its own session with output discarded.

        If the child inherited the parent's stdout/stderr it would scribble
        over the user's prompt; if it stayed in the same process group it
        would be killed with the foreground shell job.
        """
        with patch("mem.capture.subprocess.Popen") as mock_popen:
            _spawn_background_sync()

        assert mock_popen.call_count == 1
        argv, kwargs = mock_popen.call_args.args[0], mock_popen.call_args.kwargs
        # Either the installed console script or the module fallback, but it
        # must always end up invoking the `_sync` command. Asserting the exact
        # argv would pin an implementation detail; asserting the command runs is
        # the contract — and the module form is precisely the one that silently
        # ran nothing for months.
        assert argv[-1] == "_sync"
        assert argv[1:] in (["_sync"], ["-m", "mem.cli", "_sync"])
        assert kwargs["stdout"] is subprocess.DEVNULL
        assert kwargs["stderr"] is subprocess.DEVNULL
        assert kwargs["start_new_session"] is True


class TestMemDoesNotRecordItself:
    """The hook hands every line to ``mem _capture``, including mem's own.

    Recording them made every repeated search answer itself, and it re-wrote
    secrets straight back into history: ``mem forget "API_KEY=sk-…"`` deleted
    the key, and the hook then captured the line that deleted it.
    """

    @pytest.mark.parametrize(
        ("raw", "secret"),
        [
            ('mem forget "API_KEY=sk-test123"', "sk-test123"),
            ("mem vars set TOKEN hunter2-value", "hunter2-value"),
            ("mem run api API_TOKEN=abc123-value", "abc123-value"),
            ('mem "check disk space"', "check disk space"),
        ],
    )
    def test_an_invocation_of_mem_is_written_nowhere(self, tmp_mem_dir, raw, secret):
        with (
            patch("mem.capture.get_git_repo", return_value=None),
            patch("mem.capture._spawn_background_sync"),
        ):
            capture_command(raw, "/tmp", 0, 5)

        leaked = [
            path
            for path in storage.MEM_DIR.rglob("*")
            if path.is_file() and secret in path.read_text(errors="replace")
        ]
        assert leaked == []
        assert storage.read_sync_counter() == 0

    def test_surrounding_whitespace_is_not_part_of_the_command(self, tmp_mem_dir):
        with (
            patch("mem.capture.get_git_repo", return_value=None),
            patch("mem.capture._spawn_background_sync"),
        ):
            capture_command("ls ", "/tmp", 0, 5)

        assert [c.command for c in storage.read_all_commands()] == ["ls"]

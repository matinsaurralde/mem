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

        repo_name = storage.sanitize_repo_name("/Users/test/projects/myapp")
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
        assert argv[1:] == ["-m", "mem.cli", "_sync"]
        assert kwargs["stdout"] is subprocess.DEVNULL
        assert kwargs["stderr"] is subprocess.DEVNULL
        assert kwargs["start_new_session"] is True

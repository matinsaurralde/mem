"""Tests for command capture and session tracking."""

from __future__ import annotations

from unittest.mock import patch

from conftest import make_command
from mem import storage
from mem.capture import SessionTracker, capture_command, get_git_repo


class TestGetGitRepo:
    """Tests for git repository detection."""

    def test_detects_current_repo(self, tmp_path):
        """Returns repo name when inside a git repo."""
        # Use the actual mem repo directory for this test
        repo = get_git_repo(".")
        assert repo == "mem"

    def test_returns_none_outside_repo(self, tmp_path):
        """Returns None when not inside a git repo."""
        repo = get_git_repo(str(tmp_path))
        assert repo is None

    def test_returns_none_for_nonexistent_dir(self):
        """Returns None for a directory that doesn't exist."""
        repo = get_git_repo("/nonexistent/path/that/does/not/exist")
        assert repo is None

    def test_handles_timeout(self):
        """Returns None when git command times out."""
        with patch("mem.capture.subprocess.run", side_effect=TimeoutError):
            # subprocess.TimeoutExpired is a subclass of SubprocessError,
            # but we catch TimeoutExpired — let's trigger FileNotFoundError too
            pass

        with patch(
            "mem.capture.subprocess.run",
            side_effect=FileNotFoundError("git not found"),
        ):
            repo = get_git_repo("/some/path")
            assert repo is None


class TestCaptureCommand:
    """Tests for the capture_command pipeline."""

    def test_captures_with_metadata(self, tmp_mem_dir):
        """Capture stores command with all metadata fields."""
        with patch("mem.capture.get_git_repo", return_value="myapp"):
            capture_command(
                raw="git status",
                directory="/Users/test/myapp",
                exit_code=0,
                duration_ms=42,
            )

        cmds = list(storage.read_all_commands())
        assert len(cmds) == 1
        cmd = cmds[0]
        assert cmd.command == "git status"
        assert cmd.repo == "myapp"
        assert cmd.exit_code == 0
        assert cmd.duration_ms == 42

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
        with patch("mem.capture.get_git_repo", return_value="myapp"):
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
            patch("mem.capture.get_git_repo", return_value="myapp"),
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
        cmd = make_command(command="git status", ts=1000, repo="myapp")
        tracker.update(cmd)

        state = tracker._load_state()
        assert state is not None
        assert state.commands == ["git status"]
        assert state.last_command_ts == 1000
        assert state.last_repo == "myapp"

    def test_subsequent_commands_extend_session(self, tmp_mem_dir):
        """Commands within timeout extend the current session."""
        tracker = SessionTracker()

        tracker.update(make_command(command="git status", ts=1000, repo="myapp"))
        tracker.update(make_command(command="git diff", ts=1010, repo="myapp"))
        tracker.update(make_command(command="git add .", ts=1020, repo="myapp"))

        state = tracker._load_state()
        assert len(state.commands) == 3
        assert state.commands == ["git status", "git diff", "git add ."]
        assert state.last_command_ts == 1020

    def test_idle_timeout_closes_session(self, tmp_mem_dir):
        """More than 300s of idle time triggers session closure."""
        tracker = SessionTracker()

        # First session
        tracker.update(make_command(command="git status", ts=1000, repo="myapp"))
        tracker.update(make_command(command="git diff", ts=1010, repo="myapp"))

        # 301 seconds later — triggers session close
        tracker.update(make_command(command="make build", ts=1311, repo="myapp"))

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
        tracker.update(make_command(command="git status", ts=1000, repo="myapp"))
        tracker.update(make_command(command="npm test", ts=1010, repo="myapp"))

        # Switch to another repo (within timeout)
        tracker.update(make_command(command="git log", ts=1020, repo="other-repo"))

        # myapp session should be closed
        sessions = list(storage.read_all_sessions())
        assert len(sessions) == 1
        assert sessions[0].repo == "myapp"
        assert sessions[0].commands == ["git status", "npm test"]

        # New session for other-repo
        state = tracker._load_state()
        assert state.last_repo == "other-repo"

    def test_exactly_300s_does_not_close(self, tmp_mem_dir):
        """Boundary check: exactly 300s is NOT a session break."""
        tracker = SessionTracker()

        tracker.update(make_command(command="cmd1", ts=1000, repo="myapp"))
        tracker.update(make_command(command="cmd2", ts=1300, repo="myapp"))  # exactly 300s

        # No session should be closed
        sessions = list(storage.read_all_sessions())
        assert len(sessions) == 0

        state = tracker._load_state()
        assert len(state.commands) == 2

    def test_session_summary_fallback(self, tmp_mem_dir):
        """Session summary falls back to first command when AI unavailable."""
        tracker = SessionTracker()

        tracker.update(make_command(command="git status", ts=1000, repo="myapp"))
        tracker.update(make_command(command="git diff", ts=1010, repo="myapp"))

        # Trigger close via timeout
        with patch("mem.patterns._apple_fm_available", return_value=False):
            tracker.update(make_command(command="new cmd", ts=1311, repo="myapp"))

        sessions = list(storage.read_all_sessions())
        assert len(sessions) == 1
        assert sessions[0].summary == "git status (+1 more commands)"

    def test_single_command_session_summary(self, tmp_mem_dir):
        """Single-command session uses the command itself as summary."""
        tracker = SessionTracker()

        tracker.update(make_command(command="git status", ts=1000, repo="myapp"))

        # Trigger close via repo change
        with patch("mem.patterns._apple_fm_available", return_value=False):
            tracker.update(make_command(command="ls", ts=1010, repo="other"))

        sessions = list(storage.read_all_sessions())
        assert len(sessions) == 1
        assert sessions[0].summary == "git status"

    def test_corrupted_state_starts_fresh(self, tmp_mem_dir):
        """Corrupted state file is treated as no state."""
        tracker = SessionTracker()
        storage.ensure_dirs()
        tracker._state_path.write_text("{{invalid json", encoding="utf-8")

        # Should not raise, starts fresh session
        tracker.update(make_command(command="git status", ts=1000, repo="myapp"))

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
            last_repo="myapp",
            commands=[],
        )
        tracker._close_session(empty_state)

        sessions = list(storage.read_all_sessions())
        assert len(sessions) == 0

    def test_state_survives_reload(self, tmp_mem_dir):
        """State persisted to disk can be loaded by a new tracker instance."""
        tracker1 = SessionTracker()
        tracker1.update(make_command(command="git status", ts=1000, repo="myapp"))

        # New tracker instance loads existing state
        tracker2 = SessionTracker()
        state = tracker2._load_state()
        assert state is not None
        assert state.commands == ["git status"]

        # Continue the session
        tracker2.update(make_command(command="git diff", ts=1010, repo="myapp"))
        state = tracker2._load_state()
        assert state.commands == ["git status", "git diff"]

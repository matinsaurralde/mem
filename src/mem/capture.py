"""
Shell history capture module.

Handles command capture from shell hooks and session tracking.
The capture pipeline: shell hook -> mem _capture -> this module -> storage.
"""

from __future__ import annotations

import json
import subprocess
import time
import uuid

from mem.models import CapturedCommand, SessionState, WorkSession
from mem import storage


def get_git_repo(directory: str) -> str | None:
    """Detect the current git repository's root path.

    Runs `git rev-parse --show-toplevel` to find the repo root
    and returns the full absolute path (e.g., /Users/me/projects/myapp).

    Uses the full path — not the basename — so repos with the same
    folder name under different parents stay isolated (e.g.,
    /work/client-a/api and /work/client-b/api are distinct).

    Returns None if the directory is not inside a git repository.

    Why subprocess over gitpython: zero dependencies. git is always
    available on macOS, and we only need one command.
    """
    try:
        result = subprocess.run(
            ["git", "-C", directory, "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
        return None
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None


def capture_command(raw: str, directory: str, exit_code: int, duration_ms: int) -> None:
    """Capture a shell command with full context and persist it.

    Called by the shell hook after every command execution.
    Builds a CapturedCommand with the current timestamp and git repo,
    then appends it to the appropriate JSONL file.
    """
    repo = get_git_repo(directory)
    cmd = CapturedCommand(
        command=raw,
        ts=int(time.time()),
        dir=directory,
        repo=repo,
        exit_code=exit_code,
        duration_ms=duration_ms,
    )
    storage.append_command(cmd)

    # Update session tracking
    try:
        tracker = SessionTracker()
        tracker.update(cmd)
    except Exception:
        pass  # Session tracking failure should never block capture


class SessionTracker:
    """Tracks work sessions across shell commands.

    A session is a coherent sequence of commands grouped by time
    proximity and repository context. Session boundaries are detected
    when:

    1. More than 300 seconds (5 minutes) of idle time between commands
    2. The user switches to a different git repository

    Why 300 seconds: Five minutes is long enough that brief interruptions
    (reading docs, bathroom breaks) don't split a session, but short
    enough that genuine context switches are detected. This threshold
    was chosen by observing that most developers maintain focus on a
    single task for at least 5 minutes, and breaks longer than that
    typically indicate a task switch.

    State is persisted in ~/.mem/.session_state.json so sessions
    survive shell restarts.
    """

    def __init__(self) -> None:
        self._state_path = storage.MEM_DIR / ".session_state.json"

    def _load_state(self) -> SessionState | None:
        """Load the current session state from disk."""
        if not self._state_path.exists():
            return None
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
            return SessionState(**data)
        except Exception:
            return None

    def _save_state(self, state: SessionState) -> None:
        """Persist session state to disk."""
        storage.ensure_dirs()
        self._state_path.write_text(state.model_dump_json(), encoding="utf-8")

    def _clear_state(self) -> None:
        """Remove session state file."""
        if self._state_path.exists():
            self._state_path.unlink()

    def update(self, cmd: CapturedCommand) -> None:
        """Process a new command and update session state.

        Detects session boundaries and closes sessions when:
        - More than 300 seconds have passed since the last command
        - The git repo has changed
        """
        state = self._load_state()

        if state is None:
            # Start a new session
            new_state = SessionState(
                session_id=uuid.uuid4().hex,
                last_command_ts=cmd.ts,
                last_repo=cmd.repo,
                commands=[cmd.command],
            )
            self._save_state(new_state)
            return

        idle_time = cmd.ts - state.last_command_ts
        repo_changed = cmd.repo != state.last_repo

        # Session boundary: >300s idle OR repo change
        if idle_time > 300 or repo_changed:
            self._close_session(state)
            # Start new session
            new_state = SessionState(
                session_id=uuid.uuid4().hex,
                last_command_ts=cmd.ts,
                last_repo=cmd.repo,
                commands=[cmd.command],
            )
            self._save_state(new_state)
        else:
            # Continue current session
            state.commands.append(cmd.command)
            state.last_command_ts = cmd.ts
            state.last_repo = cmd.repo
            self._save_state(state)

    def _close_session(self, state: SessionState) -> None:
        """Close and persist a completed session."""
        if not state.commands:
            return

        # Generate summary — use first command as fallback when AI unavailable
        summary = self._generate_summary(state.commands, state.last_repo)

        session = WorkSession(
            id=state.session_id,
            summary=summary,
            started_at=state.last_command_ts
            - (len(state.commands) * 10),  # approximate
            ended_at=state.last_command_ts,
            dir="",  # not tracked in state for simplicity
            repo=state.last_repo,
            commands=state.commands,
        )
        storage.append_session(session)

    def _generate_summary(self, commands: list[str], repo: str | None) -> str:
        """Generate a session summary.

        Uses Apple FM SDK if available, otherwise falls back to
        using the first command as the summary.
        """
        try:
            import asyncio
            from mem.patterns import generate_session_summary

            result = asyncio.run(generate_session_summary(commands))
            if result:
                return result
        except Exception:
            pass

        # Fallback: first command + count
        if len(commands) == 1:
            return commands[0]
        return f"{commands[0]} (+{len(commands) - 1} more commands)"

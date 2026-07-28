"""Destructive-path contract tests for the storage layer.

These tests guard the only two functions in mem that DELETE user data:
``storage.rotate`` (retention) and ``storage.forget_commands`` (privacy
scrub). They are written as a mutation-proof harness: every test states
the contract the function must honour, so a later refactor that inverts a
comparison, widens a delete, or silently drops a line turns the suite red.

Tests marked ``xfail(strict=True)`` describe contracts that are BROKEN
today. They assert the correct behaviour on purpose — when the bug is
fixed the xfail turns into an XPASS and CI fails, forcing the marker to
be removed.

Nothing here may touch the real ``~/.mem``: every test runs against the
``tmp_mem_dir`` fixture (or an explicitly patched ``storage.MEM_DIR``).
Note that ``monkeypatch.undo()`` is never called — it would revert the
fixture's patches too and point the code at the user's real home.
"""

from __future__ import annotations

import json
import multiprocessing as mp
import os
import pathlib
import stat
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

import pytest

from conftest import make_command
from mem import capture, patterns, storage
from mem.models import (
    CommandPattern,
    Group,
    GroupCommand,
    GroupFile,
    PatternFile,
    StoredVariable,
    VarsFile,
    WorkSession,
)

DAY = 86400

# A value that must never survive a `mem forget`. Distinctive enough that a
# substring search over every file in MEM_DIR is meaningful.
SECRET = "SUPERSECRET123"


# --- helpers ---------------------------------------------------------------


def command_line(command: str, ts: int, repo: str = "/w/app") -> str:
    """Build a raw JSONL line for a captured command, bypassing the models.

    Raw lines let a test inject shapes the models would reject (missing
    ``ts``, invalid JSON) which is exactly what rotate() must survive.
    """
    return json.dumps(
        {
            "command": command,
            "ts": ts,
            "dir": repo,
            "repo": repo,
            "exit_code": 0,
            "duration_ms": 1,
        }
    )


def write_repo_file(mem_dir: Path, name: str, lines: list[str]) -> Path:
    """Write raw lines to repos/<name>.jsonl and return the path."""
    path = mem_dir / "repos" / f"{name}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(line + "\n" for line in lines), encoding="utf-8")
    return path


def write_session_file(
    mem_dir: Path, date: str, sessions: list[dict[str, Any]]
) -> Path:
    """Write raw session dicts to sessions/<date>.jsonl and return the path."""
    path = mem_dir / "sessions" / f"{date}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(s) + "\n" for s in sessions),
        encoding="utf-8",
    )
    return path


def read_commands_raw(path: Path) -> list[str]:
    """Return the non-blank lines of a JSONL file."""
    if not path.exists():
        return []
    return [
        line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def commands_in(path: Path) -> list[str]:
    """Return the ``command`` field of every parseable line in a JSONL file."""
    out: list[str] = []
    for line in read_commands_raw(path):
        try:
            out.append(json.loads(line)["command"])
        except (json.JSONDecodeError, KeyError):
            continue
    return out


def days_ago_date(days: int) -> str:
    """UTC calendar date N days ago, in the YYYY-MM-DD form rotate() compares."""
    return datetime.fromtimestamp(time.time() - days * DAY, tz=timezone.utc).strftime(
        "%Y-%m-%d"
    )


def files_containing(mem_dir: Path, needle: str) -> list[str]:
    """Every file under mem_dir whose bytes contain needle (paths relative)."""
    hits: list[str] = []
    for path in sorted(mem_dir.rglob("*")):
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if needle in text:
            hits.append(str(path.relative_to(mem_dir)))
    return hits


def run_in_process(target: Callable[..., None], *args: object) -> None:
    """Run target in a real separate process and wait for a clean exit.

    Uses the "spawn" context so the child gets a pristine interpreter: it
    inherits none of the parent's monkeypatching, which is what makes these
    genuine multi-process races rather than same-process simulations.
    """
    proc = mp.get_context("spawn").Process(target=target, args=args)
    proc.start()
    proc.join(timeout=60)
    assert proc.exitcode == 0, f"worker process failed (exitcode={proc.exitcode})"


# --- worker entry points (must be importable module-level callables) -------


def _worker_append(mem_dir: str, prefix: str, count: int, ts: int) -> None:
    """Append `count` commands to the shared repo history from a child process."""
    from mem import storage as child_storage
    from mem.models import CapturedCommand

    child_storage.MEM_DIR = Path(mem_dir)
    for i in range(count):
        child_storage.append_command(
            CapturedCommand(
                command=f"{prefix}-{i}",
                ts=ts,
                dir="/w/app",
                repo="/w/app",
                exit_code=0,
                duration_ms=1,
            )
        )


def _worker_write_group(mem_dir: str, path: str, cmd: str) -> None:
    """Write a group file from a child process (same target path as the parent)."""
    from mem import storage as child_storage
    from mem.models import Group as ChildGroup
    from mem.models import GroupCommand as ChildGroupCommand
    from mem.models import GroupFile as ChildGroupFile

    child_storage.MEM_DIR = Path(mem_dir)
    child_storage.write_group_file(
        Path(path),
        ChildGroupFile(
            groups={"deploy": ChildGroup(commands=[ChildGroupCommand(cmd=cmd)])}
        ),
    )


def _worker_increment_counter(mem_dir: str) -> None:
    """Increment the auto-sync counter once from a child process."""
    from mem import storage as child_storage

    child_storage.MEM_DIR = Path(mem_dir)
    child_storage.SYNC_COUNTER_FILE = Path(mem_dir) / ".sync_counter"
    child_storage.increment_sync_counter()


# --- fixtures --------------------------------------------------------------


@pytest.fixture
def frozen_now(monkeypatch: pytest.MonkeyPatch) -> int:
    """Freeze the wall clock so retention boundaries are exact.

    rotate() calls time.time() itself; without freezing, a test that pins a
    timestamp one second inside the window becomes a coin flip on a slow
    machine (and the date-based session cutoff flips at UTC midnight).
    """
    now = int(time.time())
    monkeypatch.setattr(time, "time", lambda: float(now))
    return now


@pytest.fixture
def strict_umask() -> Iterator[None]:
    """Pin the process umask to 0022 so permission assertions are deterministic."""
    previous = os.umask(0o022)
    try:
        yield
    finally:
        os.umask(previous)


# --- rotate: retention boundaries -----------------------------------------


class TestRotateRetentionBoundaries:
    """rotate() must delete exactly the data outside the retention window."""

    def test_command_just_inside_window_survives(self, tmp_mem_dir, frozen_now):
        """A command 89 days old is inside the 90-day window and must be kept."""
        now = frozen_now
        path = write_repo_file(
            tmp_mem_dir, "w-app", [command_line("d89", now - 89 * DAY)]
        )

        removed, _ = storage.rotate()

        assert removed == 0
        assert commands_in(path) == ["d89"]

    def test_command_just_outside_window_is_removed(self, tmp_mem_dir, frozen_now):
        """A command 91 days old is outside the 90-day window and must be dropped."""
        now = frozen_now
        path = write_repo_file(
            tmp_mem_dir,
            "w-app",
            [command_line("d91", now - 91 * DAY), command_line("fresh", now)],
        )

        removed, _ = storage.rotate()

        assert removed == 1
        assert commands_in(path) == ["fresh"]

    def test_command_exactly_at_cutoff_is_removed(self, tmp_mem_dir, frozen_now):
        """The boundary is exclusive: ts == cutoff counts as expired."""
        now = frozen_now
        path = write_repo_file(
            tmp_mem_dir,
            "w-app",
            [
                command_line("exactly-90d", now - 90 * DAY),
                command_line("one-second-younger", now - 90 * DAY + 1),
            ],
        )

        removed, _ = storage.rotate()

        assert removed == 1
        assert commands_in(path) == ["one-second-younger"]

    def test_custom_retention_window_is_respected(self, tmp_mem_dir, frozen_now):
        """keep_commands_days is honoured, not hardcoded to 90."""
        now = frozen_now
        path = write_repo_file(
            tmp_mem_dir,
            "w-app",
            [command_line("d5", now - 5 * DAY), command_line("d9", now - 9 * DAY)],
        )

        removed, _ = storage.rotate(keep_commands_days=7)

        assert removed == 1
        assert commands_in(path) == ["d5"]

    def test_session_file_just_inside_window_survives(self, tmp_mem_dir, frozen_now):
        """A session file dated 29 days ago is inside the 30-day window."""
        path = write_session_file(tmp_mem_dir, days_ago_date(29), [{"id": "a"}])

        _, removed = storage.rotate()

        assert removed == 0
        assert path.exists()

    def test_session_file_exactly_at_cutoff_survives(self, tmp_mem_dir, frozen_now):
        """The session boundary compares dates with `<`: the cutoff day is kept."""
        path = write_session_file(tmp_mem_dir, days_ago_date(30), [{"id": "a"}])

        _, removed = storage.rotate()

        assert removed == 0
        assert path.exists()

    def test_session_file_just_outside_window_is_removed(self, tmp_mem_dir, frozen_now):
        """A session file dated 31 days ago is expired and must be deleted."""
        old = write_session_file(tmp_mem_dir, days_ago_date(31), [{"id": "old"}])
        recent = write_session_file(tmp_mem_dir, days_ago_date(1), [{"id": "recent"}])

        _, removed = storage.rotate()

        assert removed == 1
        assert not old.exists()
        assert recent.exists()

    def test_pattern_files_are_never_rotated(self, tmp_mem_dir, frozen_now):
        """Patterns are accumulated learning: retention must not touch them."""
        storage.write_patterns(
            PatternFile(
                tool="kubectl",
                patterns=[
                    CommandPattern(
                        pattern="kubectl get <resource>",
                        example="kubectl get pods",
                        frequency=3,
                    )
                ],
                last_updated=int(time.time()) - 400 * DAY,
            )
        )

        storage.rotate()

        assert storage.read_patterns("kubectl") is not None


# --- rotate: mutation guards ----------------------------------------------


class TestRotateMutationGuards:
    """Guards against the surviving mutant: an inverted retention comparison.

    If `data["ts"] > cutoff` is flipped to `<` (or the cutoff arithmetic is
    reversed), rotate() deletes the entire recent history instead of the old
    data. These tests fail loudly in that scenario.
    """

    def test_rotate_never_deletes_fresh_history(self, tmp_mem_dir, frozen_now):
        """With only fresh commands, rotate() must be a no-op — not a wipe."""
        now = frozen_now
        fresh = [command_line(f"fresh-{i}", now - i * 60) for i in range(10)]
        path = write_repo_file(tmp_mem_dir, "w-app", fresh)
        before = path.read_bytes()

        removed, sessions_removed = storage.rotate()

        assert removed == 0
        assert sessions_removed == 0
        assert path.exists(), (
            "rotate() deleted a history file containing only fresh data"
        )
        assert path.read_bytes() == before
        assert len(commands_in(path)) == 10

    def test_rotate_keeps_all_fresh_session_files(self, tmp_mem_dir, frozen_now):
        """With only fresh session files, rotate() must delete none of them."""
        paths = [
            write_session_file(tmp_mem_dir, days_ago_date(d), [{"id": f"s{d}"}])
            for d in (0, 1, 7, 29)
        ]

        _, removed = storage.rotate()

        assert removed == 0
        assert all(p.exists() for p in paths)

    def test_rotate_partitions_history_exactly(self, tmp_mem_dir, frozen_now):
        """Old half removed, fresh half kept — order preserved, count exact."""
        now = frozen_now
        path = write_repo_file(
            tmp_mem_dir,
            "w-app",
            [command_line(f"old-{i}", now - (100 + i) * DAY) for i in range(5)]
            + [command_line(f"new-{i}", now - i * DAY) for i in range(5)],
        )

        removed, _ = storage.rotate()

        assert removed == 5
        assert commands_in(path) == [f"new-{i}" for i in range(5)]

    def test_rotate_is_idempotent(self, tmp_mem_dir, frozen_now):
        """A second rotate() over already-rotated data must remove nothing more."""
        now = frozen_now
        path = write_repo_file(
            tmp_mem_dir,
            "w-app",
            [command_line("old", now - 200 * DAY), command_line("new", now)],
        )
        storage.rotate()
        after_first = path.read_bytes()

        removed, _ = storage.rotate()

        assert removed == 0
        assert path.read_bytes() == after_first

    def test_rotate_on_empty_storage_is_noop(self, tmp_mem_dir, frozen_now):
        """rotate() over a mem dir with no repos/ or sessions/ must not explode."""
        assert storage.rotate() == (0, 0)


# --- rotate: degenerate files ---------------------------------------------


class TestRotateDegenerateFiles:
    """rotate() must never destroy data it cannot interpret."""

    def test_empty_file_is_preserved(self, tmp_mem_dir, frozen_now):
        """A zero-byte history file survives rotation untouched."""
        path = write_repo_file(tmp_mem_dir, "w-app", [])

        removed, _ = storage.rotate()

        assert removed == 0
        assert path.exists()
        assert path.read_text(encoding="utf-8") == ""

    def test_blank_lines_only_file_is_preserved(self, tmp_mem_dir, frozen_now):
        """A file of blank lines has nothing expired: it must not be rewritten."""
        path = tmp_mem_dir / "repos" / "w-app.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n\n   \n", encoding="utf-8")

        removed, _ = storage.rotate()

        assert removed == 0
        assert path.read_text(encoding="utf-8") == "\n\n   \n"

    def test_invalid_json_lines_are_preserved(self, tmp_mem_dir, frozen_now):
        """Unparseable lines are kept: rotate() must not silently shred them."""
        now = frozen_now
        path = write_repo_file(
            tmp_mem_dir,
            "w-app",
            [
                "THIS IS NOT JSON",
                command_line("old", now - 200 * DAY),
                '{"command": "truncated"',
                command_line("new", now),
            ],
        )

        removed, _ = storage.rotate()

        assert removed == 1
        kept = read_commands_raw(path)
        assert "THIS IS NOT JSON" in kept
        assert '{"command": "truncated"' in kept
        assert commands_in(path) == ["new"]

    def test_invalid_json_only_file_is_not_deleted(self, tmp_mem_dir, frozen_now):
        """A history file made entirely of corrupt lines must survive rotation."""
        path = write_repo_file(tmp_mem_dir, "w-app", ["NOT JSON", "ALSO NOT JSON"])

        removed, _ = storage.rotate()

        assert removed == 0
        assert path.exists()
        assert read_commands_raw(path) == ["NOT JSON", "ALSO NOT JSON"]

    @pytest.mark.xfail(
        strict=True,
        reason="P1-ROTATE-TS: rotate() trata `ts` ausente como ts=0 y borra la "
        "entrada, aunque las lineas ilegibles si se conservan",
    )
    def test_entry_without_ts_is_preserved(self, tmp_mem_dir, frozen_now):
        """A valid JSON line without `ts` has unknown age: it must not be deleted.

        rotate() keeps lines it cannot parse at all, so a parseable line whose
        timestamp is merely missing must be kept too — deleting it is an
        undetectable data loss.
        """
        now = frozen_now
        path = write_repo_file(
            tmp_mem_dir,
            "w-app",
            [json.dumps({"command": "no-ts", "dir": "/w"}), command_line("new", now)],
        )

        removed, _ = storage.rotate()

        assert removed == 0
        assert commands_in(path) == ["no-ts", "new"]

    @pytest.mark.xfail(
        strict=True,
        reason="P1-ROTATE-TS: un archivo con solo entradas sin `ts` queda vacio "
        "tras rotate() y se borra del disco",
    )
    def test_file_of_entries_without_ts_is_not_deleted(self, tmp_mem_dir, frozen_now):
        """Every line missing `ts` must not translate into deleting the file."""
        path = write_repo_file(
            tmp_mem_dir,
            "w-app",
            [json.dumps({"command": "no-ts-1"}), json.dumps({"command": "no-ts-2"})],
        )

        storage.rotate()

        assert path.exists(), "rotate() deleted a history file it could not date"


# --- forget: every destination --------------------------------------------


class TestForgetScrubsEveryDestination:
    """`mem forget` promises no traces left anywhere. Every store must be scrubbed."""

    def test_forget_removes_from_repo_history(self, tmp_mem_dir):
        """Matching commands disappear from repos/*.jsonl; the rest stay."""
        now = int(time.time())
        path = write_repo_file(
            tmp_mem_dir,
            "w-app",
            [command_line(f"export TOKEN={SECRET}", now), command_line("ls -la", now)],
        )

        removed = storage.forget_commands(SECRET)

        assert removed == 1
        assert commands_in(path) == ["ls -la"]

    def test_forget_removes_from_session_files(self, tmp_mem_dir):
        """Matching commands are scrubbed from the command list of each session."""
        path = write_session_file(
            tmp_mem_dir,
            days_ago_date(0),
            [
                {
                    "id": "s1",
                    "summary": "deploy",
                    "started_at": 1,
                    "ended_at": 2,
                    "dir": "/w/app",
                    "repo": "/w/app",
                    "commands": [f"export TOKEN={SECRET}", "ls -la"],
                }
            ],
        )

        storage.forget_commands(SECRET)

        assert files_containing(tmp_mem_dir, SECRET) == []
        assert json.loads(path.read_text(encoding="utf-8"))["commands"] == ["ls -la"]

    def test_forget_drops_sessions_that_become_empty(self, tmp_mem_dir):
        """A session whose every command matched leaves no residue behind."""
        write_session_file(
            tmp_mem_dir,
            days_ago_date(0),
            [
                {
                    "id": "s1",
                    "summary": "deploy",
                    "started_at": 1,
                    "ended_at": 2,
                    "dir": "/w/app",
                    "repo": "/w/app",
                    "commands": [f"export TOKEN={SECRET}"],
                }
            ],
        )

        storage.forget_commands(SECRET)

        assert files_containing(tmp_mem_dir, SECRET) == []

    @pytest.mark.xfail(
        strict=True,
        reason="P0-4: forget_commands() no toca patterns/*.json; el comando "
        "sobrevive en `example` y en `processed_commands`",
    )
    def test_forget_scrubs_pattern_files(self, tmp_mem_dir):
        """Extracted patterns embed raw command text and must be scrubbed too."""
        storage.write_patterns(
            PatternFile(
                tool="curl",
                patterns=[
                    CommandPattern(
                        pattern="curl -H <header>",
                        example=f"curl -H 'auth: {SECRET}'",
                        frequency=2,
                    )
                ],
                last_updated=int(time.time()),
                processed_commands=[f"curl -H 'auth: {SECRET}'"],
            )
        )

        storage.forget_commands(SECRET)

        assert files_containing(tmp_mem_dir, SECRET) == []

    @pytest.mark.xfail(
        strict=True,
        reason="P0-4: forget_commands() no toca groups/**/*.json; los comandos "
        "guardados y los runbooks conservan el texto olvidado",
    )
    def test_forget_scrubs_group_files(self, tmp_mem_dir):
        """Saved commands and named groups must lose the forgotten command."""
        storage.write_group_file(
            storage.group_file_path(None),
            GroupFile(
                groups={
                    "deploy": Group(
                        commands=[GroupCommand(cmd=f"deploy --token {SECRET}")]
                    )
                }
            ),
        )
        storage.write_group_file(
            storage.group_file_path("/w/app"),
            GroupFile(
                groups={
                    "release": Group(
                        commands=[GroupCommand(cmd=f"publish --key {SECRET}")]
                    )
                }
            ),
        )

        storage.forget_commands(SECRET)

        assert files_containing(tmp_mem_dir, SECRET) == []

    @pytest.mark.xfail(
        strict=True,
        reason="P0-4: forget_commands() no toca vars.json; el valor de la "
        "variable persistente sobrevive al olvido",
    )
    def test_forget_scrubs_vars_file(self, tmp_mem_dir):
        """A stored variable holding the forgotten value must be scrubbed."""
        storage.write_vars_file(VarsFile(vars={"TOKEN": StoredVariable(value=SECRET)}))

        storage.forget_commands(SECRET)

        assert files_containing(tmp_mem_dir, SECRET) == []

    def test_session_state_holds_in_flight_commands(self, tmp_mem_dir, monkeypatch):
        """Precondition for the resurrection bug: the live session buffers commands.

        SessionTracker keeps the raw command text in .session_state.json until
        the session closes, which is exactly why forget must scrub that file.
        """
        monkeypatch.setattr(capture, "get_git_repo", lambda directory: "/w/app")
        monkeypatch.setattr(capture, "_spawn_background_sync", lambda: None)

        capture.capture_command(f"export TOKEN={SECRET}", "/w/app", 0, 12)

        state_path = tmp_mem_dir / ".session_state.json"
        assert SECRET in state_path.read_text(encoding="utf-8")

    @pytest.mark.xfail(
        strict=True,
        reason="P0-4: forget_commands() no toca .session_state.json; el comando "
        "de la sesion en curso sobrevive al olvido",
    )
    def test_forget_scrubs_session_state(self, tmp_mem_dir, monkeypatch):
        """The in-flight session buffer must lose the forgotten command too."""
        monkeypatch.setattr(capture, "get_git_repo", lambda directory: "/w/app")
        monkeypatch.setattr(capture, "_spawn_background_sync", lambda: None)
        capture.capture_command(f"export TOKEN={SECRET}", "/w/app", 0, 12)

        storage.forget_commands(SECRET)

        assert files_containing(tmp_mem_dir, SECRET) == []

    @pytest.mark.xfail(
        strict=True,
        reason="P0-4: el secreto sobrevive en .session_state.json y RESUCITA en "
        "sessions/ cuando la sesion se cierra despues del forget",
    )
    def test_forgotten_secret_does_not_resurrect_when_session_closes(
        self, tmp_mem_dir, monkeypatch
    ):
        """A forgotten command must not reappear later via the session flush.

        Timeline: capture a secret -> `mem forget` reports success -> the shell
        keeps working and the session boundary fires -> SessionTracker writes
        its (unscrubbed) buffer to sessions/. The secret is back on disk after
        the user was told it was gone.
        """
        monkeypatch.setattr(capture, "get_git_repo", lambda directory: "/w/app")
        monkeypatch.setattr(capture, "_spawn_background_sync", lambda: None)
        # Never run real on-device inference in the unit suite.
        monkeypatch.setattr(patterns, "_apple_fm_available", lambda: False)
        now = int(time.time())
        capture.capture_command(f"export TOKEN={SECRET}", "/w/app", 0, 12)

        assert storage.forget_commands(SECRET) == 1
        assert commands_in(tmp_mem_dir / "repos" / "w-app.jsonl") == []

        # >300s idle closes the session and flushes the buffer to sessions/.
        capture.SessionTracker().update(
            make_command(command="ls -la", ts=now + 400, dir="/w/app", repo="/w/app")
        )

        assert files_containing(tmp_mem_dir, SECRET) == [], (
            "the forgotten secret came back from .session_state.json"
        )


# --- forget: no-match must be a no-op --------------------------------------


class TestForgetWithoutMatches:
    """A query that matches nothing must not modify storage at all."""

    def test_no_match_returns_zero(self, tmp_mem_dir):
        """Nothing matched, nothing removed."""
        write_repo_file(
            tmp_mem_dir, "w-app", [command_line("ls -la", int(time.time()))]
        )

        assert storage.forget_commands("no-such-command") == 0

    def test_no_match_preserves_history_content(self, tmp_mem_dir):
        """The surviving commands are byte-identical to what was there before."""
        now = int(time.time())
        path = write_repo_file(
            tmp_mem_dir,
            "w-app",
            [command_line("ls -la", now), command_line("pwd", now)],
        )
        before = read_commands_raw(path)

        storage.forget_commands("no-such-command")

        assert read_commands_raw(path) == before

    @pytest.mark.xfail(
        strict=True,
        reason="P1-FORGET-NOOP: forget_commands() reescribe (tmp+rename) todos "
        "los JSONL aunque la query no matchee nada",
    )
    def test_no_match_does_not_rewrite_files(self, tmp_mem_dir):
        """No match means no rewrite: the files on disk must not be replaced.

        Rewriting on a no-op is not cosmetic — every rewrite is a window where
        a concurrent append is lost, and it silently reformats lines the
        function never had a reason to touch.
        """
        now = int(time.time())
        repo_path = write_repo_file(
            tmp_mem_dir,
            "w-app",
            [command_line("ls -la", now), command_line("pwd", now)],
        )
        session_path = write_session_file(
            tmp_mem_dir,
            days_ago_date(0),
            [
                {
                    "id": "s1",
                    "summary": "work",
                    "started_at": 1,
                    "ended_at": 2,
                    "dir": "/w/app",
                    "repo": "/w/app",
                    "commands": ["ls -la"],
                }
            ],
        )
        repo_inode = repo_path.stat().st_ino
        session_inode = session_path.stat().st_ino

        storage.forget_commands("no-such-command")

        assert repo_path.stat().st_ino == repo_inode
        assert session_path.stat().st_ino == session_inode

    @pytest.mark.xfail(
        strict=True,
        reason="P1-FORGET-NOOP: forget_commands() borra del disco cualquier "
        "JSONL que quede sin lineas, incluso si la query no matcheo nada",
    )
    def test_no_match_does_not_delete_empty_files(self, tmp_mem_dir):
        """An empty history file is not a match: forget must not unlink it."""
        empty_repo = write_repo_file(tmp_mem_dir, "w-app", [])
        empty_session = write_session_file(tmp_mem_dir, days_ago_date(0), [])

        storage.forget_commands("no-such-command")

        assert empty_repo.exists()
        assert empty_session.exists()

    def test_no_match_preserves_corrupted_lines(self, tmp_mem_dir):
        """Unparseable lines survive a forget that matched nothing."""
        path = write_repo_file(tmp_mem_dir, "w-app", ["NOT JSON"])

        storage.forget_commands("no-such-command")

        assert path.exists()
        assert read_commands_raw(path) == ["NOT JSON"]


# --- concurrency (P0-10) ---------------------------------------------------


class TestConcurrentWriters:
    """Multiple shells write to ~/.mem at once. No write may be lost or torn."""

    def test_concurrent_appends_are_atomic(self, tmp_mem_dir):
        """Three processes appending in parallel: every line lands and parses."""
        storage.ensure_dirs()
        now = int(time.time())
        procs = [
            mp.get_context("spawn").Process(
                target=_worker_append, args=(str(tmp_mem_dir), f"p{i}", 10, now)
            )
            for i in range(3)
        ]
        for proc in procs:
            proc.start()
        for proc in procs:
            proc.join(timeout=60)
        assert all(p.exitcode == 0 for p in procs)

        path = tmp_mem_dir / "repos" / "w-app.jsonl"
        lines = read_commands_raw(path)
        assert len(lines) == 30
        assert set(commands_in(path)) == {
            f"p{i}-{j}" for i in range(3) for j in range(10)
        }

    @pytest.mark.xfail(
        strict=True,
        reason="P0-10: rotate() reescribe con tmp+rename sin lock; un append "
        "concurrente entre la lectura y el rename se pierde para siempre",
    )
    def test_append_during_rotate_is_not_lost(self, tmp_mem_dir, monkeypatch):
        """A command captured while rotate() runs must still be in history.

        The race window is made deterministic instead of timing-dependent: a
        real child process appends exactly once between rotate()'s in-memory
        snapshot and its rename over the live file.
        """
        now = int(time.time())
        write_repo_file(
            tmp_mem_dir,
            "w-app",
            [command_line("old", now - 200 * DAY), command_line("kept", now)],
        )
        real_rename = pathlib.Path.rename
        fired = {"done": False}

        def racing_rename(self: Path, target: Any) -> Any:
            if self.name.endswith(".jsonl.tmp") and not fired["done"]:
                fired["done"] = True
                run_in_process(_worker_append, str(tmp_mem_dir), "concurrent", 1, now)
            return real_rename(self, target)

        with pytest.MonkeyPatch.context() as hook:
            hook.setattr(pathlib.Path, "rename", racing_rename)
            storage.rotate()

        assert fired["done"], "rotate() never reached the rename: test setup is stale"
        path = tmp_mem_dir / "repos" / "w-app.jsonl"
        assert commands_in(path) == ["kept", "concurrent-0"]

    def test_concurrent_appends_leave_no_torn_lines(self, tmp_mem_dir):
        """Every line in the history file is a complete, parseable JSON object."""
        storage.ensure_dirs()
        now = int(time.time())
        procs = [
            mp.get_context("spawn").Process(
                target=_worker_append, args=(str(tmp_mem_dir), f"w{i}", 15, now)
            )
            for i in range(2)
        ]
        for proc in procs:
            proc.start()
        for proc in procs:
            proc.join(timeout=60)

        # Non-vacuity guard: without it, two children that died on startup
        # would leave no file at all and the loop below would iterate zero
        # times, so the test would pass while proving nothing.
        assert [p.exitcode for p in procs] == [0, 0], "a writer process failed"

        path = tmp_mem_dir / "repos" / "w-app.jsonl"
        lines = list(read_commands_raw(path))
        assert lines, "no lines were written: the concurrency check is vacuous"
        for line in lines:
            json.loads(line)  # raises on a torn write

    @pytest.mark.xfail(
        strict=True,
        reason="P0-10: write_group_file() usa un path .tmp fijo y compartido; "
        "dos escritores concurrentes chocan y el rename revienta con "
        "FileNotFoundError",
    )
    def test_concurrent_group_writers_do_not_collide(self, tmp_mem_dir):
        """Two processes writing the same group file must not corrupt or crash.

        Both writers derive the same temporary path (<file>.json.tmp), so the
        second one renames the first one's temp file out from under it. The
        contract: last writer wins, the file stays valid, nobody raises.
        """
        path = storage.group_file_path(None)
        storage.write_group_file(path, GroupFile())
        real_rename = pathlib.Path.rename
        fired = {"done": False}

        def racing_rename(self: Path, target: Any) -> Any:
            if self.name.endswith(".json.tmp") and not fired["done"]:
                fired["done"] = True
                run_in_process(
                    _worker_write_group, str(tmp_mem_dir), str(path), "from-b"
                )
            return real_rename(self, target)

        with pytest.MonkeyPatch.context() as hook:
            hook.setattr(pathlib.Path, "rename", racing_rename)
            storage.write_group_file(
                path,
                GroupFile(
                    groups={"deploy": Group(commands=[GroupCommand(cmd="from-a")])}
                ),
            )

        assert fired["done"], "write_group_file() never renamed: test setup is stale"
        written = storage.read_group_file(path)
        assert [c.cmd for c in written.groups["deploy"].commands] in (
            ["from-a"],
            ["from-b"],
        )

    @pytest.mark.xfail(
        strict=True,
        reason="P0-10: increment_sync_counter() hace read-then-write sin lock; "
        "dos incrementos concurrentes cuentan como uno",
    )
    def test_increment_sync_counter_does_not_lose_updates(self, tmp_mem_dir):
        """Two concurrent increments must leave the counter at 2, not 1.

        A lost increment silently delays the auto-sync that extracts patterns,
        so the feature degrades invisibly on busy machines.
        """
        real_write_text = pathlib.Path.write_text
        fired = {"done": False}

        def racing_write_text(self: Path, data: str, **kwargs: Any) -> Any:
            if self.name == ".sync_counter" and not fired["done"]:
                fired["done"] = True
                run_in_process(_worker_increment_counter, str(tmp_mem_dir))
            return real_write_text(self, data, **kwargs)

        with pytest.MonkeyPatch.context() as hook:
            hook.setattr(pathlib.Path, "write_text", racing_write_text)
            storage.increment_sync_counter()

        assert fired["done"], "the counter was never written: test setup is stale"
        assert storage.read_sync_counter() == 2


# --- permissions (P0-5) ----------------------------------------------------


class TestStoragePermissions:
    """Shell history is sensitive: nothing under ~/.mem may be world-readable."""

    @pytest.mark.xfail(
        strict=True,
        reason="P0-5: ensure_dirs() usa mkdir con el umask por defecto y deja "
        "~/.mem en 0755 (world-readable) en vez de 0700",
    )
    def test_mem_dir_is_owner_only(self, tmp_path, monkeypatch, strict_umask):
        """~/.mem and its subdirectories must be created 0700."""
        mem_home = tmp_path / "mem_home"
        monkeypatch.setattr(storage, "MEM_DIR", mem_home)

        storage.ensure_dirs()

        assert stat.S_IMODE(mem_home.stat().st_mode) == 0o700
        assert stat.S_IMODE((mem_home / "repos").stat().st_mode) == 0o700

    @pytest.mark.xfail(
        strict=True,
        reason="P0-5: append_command() crea repos/*.jsonl con el umask por "
        "defecto y quedan en 0644 (world-readable) en vez de 0600",
    )
    def test_history_files_are_owner_only(self, tmp_mem_dir, strict_umask):
        """Command history files must be 0600 — they contain whatever was typed."""
        storage.append_command(make_command(command="export TOKEN=abc", repo="/w/app"))

        path = tmp_mem_dir / "repos" / "w-app.jsonl"
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    @pytest.mark.xfail(
        strict=True,
        reason="P0-5: append_session() crea sessions/*.jsonl con el umask por "
        "defecto y quedan en 0644 (world-readable) en vez de 0600",
    )
    def test_session_files_are_owner_only(self, tmp_mem_dir, strict_umask):
        """Session files embed the same command text and must be 0600 too."""
        now = int(time.time())
        storage.append_session(
            WorkSession(
                id="s1",
                summary="work",
                started_at=now,
                ended_at=now,
                dir="/w/app",
                repo="/w/app",
                commands=["export TOKEN=abc"],
            )
        )

        written = list((tmp_mem_dir / "sessions").glob("*.jsonl"))
        assert len(written) == 1
        assert stat.S_IMODE(written[0].stat().st_mode) == 0o600

    def test_vars_file_is_owner_only(self, tmp_mem_dir, strict_umask):
        """write_vars_file() already chmods 0600 — lock that behaviour in."""
        storage.write_vars_file(VarsFile(vars={"TOKEN": StoredVariable(value="abc")}))

        assert stat.S_IMODE((tmp_mem_dir / "vars.json").stat().st_mode) == 0o600


# --- repo name collisions --------------------------------------------------


class TestRepoNameCollisions:
    """get_git_repo() promises repo isolation; sanitize_repo_name() breaks it."""

    def test_sanitize_is_stable_for_a_single_repo(self, tmp_mem_dir):
        """Sanitization is deterministic — the same path always maps to one file."""
        assert storage.sanitize_repo_name("/w/a/b/c") == storage.sanitize_repo_name(
            "/w/a/b/c"
        )

    @pytest.mark.xfail(
        strict=True,
        reason="P1-COLLISION: '/w/a-b/c' y '/w/a/b/c' se sanitizan al mismo "
        "'w-a-b-c' y mezclan el historial de dos repos distintos",
    )
    def test_hyphen_in_path_does_not_collide_with_separator(self, tmp_mem_dir):
        """Two different repos must never share a history file."""
        storage.append_command(make_command(command="build-alpha", repo="/w/a-b/c"))
        storage.append_command(make_command(command="build-beta", repo="/w/a/b/c"))

        files = sorted((tmp_mem_dir / "repos").glob("*.jsonl"))
        assert len(files) == 2, f"two repos collapsed into {[f.name for f in files]}"
        assert [commands_in(f) for f in files] != [["build-alpha", "build-beta"]]

    @pytest.mark.xfail(
        strict=True,
        reason="P1-COLLISION: '/x/proj.api' y '/x/proj-api' se sanitizan al "
        "mismo 'x-proj-api' y mezclan el historial de dos repos distintos",
    )
    def test_dot_does_not_collide_with_hyphen(self, tmp_mem_dir):
        """A dotted repo name must not land in the hyphenated repo's file."""
        storage.append_command(
            make_command(command="deploy-dotted", repo="/x/proj.api")
        )
        storage.append_command(
            make_command(command="deploy-hyphen", repo="/x/proj-api")
        )

        files = sorted((tmp_mem_dir / "repos").glob("*.jsonl"))
        assert len(files) == 2, f"two repos collapsed into {[f.name for f in files]}"

    @pytest.mark.xfail(
        strict=True,
        reason="P1-COLLISION: al colisionar los nombres, forget/rotate sobre un "
        "repo tambien alcanzan el historial del otro",
    )
    def test_collision_leaks_history_across_repos(self, tmp_mem_dir):
        """Reading one repo's history must never surface another repo's commands."""
        storage.append_command(make_command(command="build-alpha", repo="/w/a-b/c"))
        storage.append_command(make_command(command="build-beta", repo="/w/a/b/c"))

        history = [c.command for c in storage.read_commands("w-a-b-c")]
        assert history != ["build-alpha", "build-beta"]

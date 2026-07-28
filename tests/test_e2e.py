"""End-to-end tests that drive the real `mem` binary through subprocess.

Every other test module imports `mem.*` directly, which makes an entire
class of defects invisible: entry points that are never reached, console
scripts that exit 0 without doing anything, shell hooks that are printed
but never parsed, files written outside `~/.mem`, and startup cost.

These tests therefore:

- run the installed console script (and `python -m mem.cli`) as a child
  process, never in-process;
- point ``$HOME`` at a pytest ``tmp_path`` so the developer's real
  ``~/.mem`` is never read nor written;
- work from an empty, non-git working directory so repo detection and
  scope resolution are deterministic.

Tests describing a bug that still exists are written asserting the
CORRECT behaviour and marked ``xfail(strict=True)``, so the CI turns red
the day the bug is fixed and the marker becomes stale.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
HOOKS_DIR = REPO_ROOT / "hooks"

# The console script lives next to the interpreter running the test suite
# (that is what `pip install -e .` produces inside a venv). Falling back to
# PATH keeps the suite usable when tests run against a system-wide install.
_CANDIDATE_BIN = Path(sys.executable).with_name("mem")
MEM_BIN: str | None = (
    str(_CANDIDATE_BIN) if _CANDIDATE_BIN.exists() else shutil.which("mem")
)

pytestmark = pytest.mark.skipif(
    MEM_BIN is None,
    reason="the `mem` console script is not installed (pip install -e .)",
)

# Generous cap: a subprocess call to mem should never take this long, but a
# loaded CI box can be slow. This only guards against hangs.
RUN_TIMEOUT = 60


def mem_env(home: Path, **extra: str) -> dict[str, str]:
    """Build a subprocess environment with ``$HOME`` redirected to *home*.

    Nothing else is stripped: the child still needs PATH (to find `git`)
    and the interpreter's own configuration.
    """
    env = dict(os.environ)
    env["HOME"] = str(home)
    env.pop("MEM_DIR", None)
    env.update(extra)
    return env


def run_mem(
    args: list[str],
    home: Path,
    cwd: Path,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run the `mem` console script with a temporary HOME and capture output."""
    assert MEM_BIN is not None
    return subprocess.run(
        [MEM_BIN, *args],
        cwd=str(cwd),
        env=env if env is not None else mem_env(home),
        capture_output=True,
        text=True,
        timeout=RUN_TIMEOUT,
    )


def run_module_sync(home: Path, cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run exactly what ``capture._spawn_background_sync`` spawns.

    ``capture.py`` builds the command as
    ``[sys.executable, "-m", "mem.cli", "_sync"]``; this helper reproduces
    it verbatim so the test breaks if that invocation stops working.
    """
    return subprocess.run(
        [sys.executable, "-m", "mem.cli", "_sync"],
        cwd=str(cwd),
        env=mem_env(home),
        capture_output=True,
        text=True,
        timeout=RUN_TIMEOUT,
    )


@pytest.fixture
def home(tmp_path: Path) -> Path:
    """An empty HOME for the CLI. Never the developer's real home."""
    h = tmp_path / "home"
    h.mkdir()
    return h


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    """An empty, non-git working directory to run the CLI from."""
    w = tmp_path / "work"
    w.mkdir()
    return w


def global_history(home: Path) -> Path:
    """Path of the JSONL file where repo-less commands are appended."""
    return home / ".mem" / "repos" / "_global.jsonl"


def plant_commands(home: Path, entries: list[tuple[str, int]]) -> None:
    """Write ``(command, ts)`` pairs straight into the global history file.

    Bypassing the CLI keeps setup fast and lets tests plant timestamps in
    the distant past, which is the only way to observe retention.
    """
    path = global_history(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        json.dumps(
            {
                "command": command,
                "ts": ts,
                "dir": "/tmp",
                "repo": None,
                "exit_code": 0,
                "duration_ms": 1,
            }
        )
        for command, ts in entries
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def history_commands(home: Path) -> list[str]:
    """Read back the command strings currently stored in global history."""
    path = global_history(home)
    if not path.exists():
        return []
    return [
        json.loads(line)["command"]
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


# Old enough to be dropped by the 90-day command retention in storage.rotate().
ANCIENT_AGE = 200 * 86400


class TestBackgroundSyncEntryPoint:
    """The auto-sync entry point spawned by `capture._spawn_background_sync`.

    Contract: `_sync` runs pattern extraction and `storage.rotate()`.
    Rotation is the observable half — it is deterministic, needs no AI,
    and purging a 200-day-old command proves the command body executed.
    """

    def test_console_script_sync_rotates_old_commands(
        self, home: Path, workdir: Path
    ) -> None:
        """`mem _sync` (console script) applies the 90-day command retention.

        This is the reference behaviour: the same work must happen through
        whichever entry point the background spawn uses.
        """
        now = int(time.time())
        plant_commands(
            home, [("zzancient --flag", now - ANCIENT_AGE), ("zzfresh", now)]
        )

        result = run_mem(["_sync"], home, workdir)

        assert result.returncode == 0
        assert history_commands(home) == ["zzfresh"]

    def test_python_dash_m_sync_runs_the_sync_command(
        self, home: Path, workdir: Path
    ) -> None:
        """`python -m mem.cli _sync` must actually execute the `_sync` command.

        This is the exact invocation `capture._spawn_background_sync` uses.
        Today it is a silent no-op: pattern extraction has never run
        automatically for any user, and because `storage.rotate()` is called
        from `_sync`, the 90d/30d retention has never run either.
        """
        now = int(time.time())
        plant_commands(
            home, [("zzancient --flag", now - ANCIENT_AGE), ("zzfresh", now)]
        )

        result = run_module_sync(home, workdir)

        assert result.returncode == 0
        assert history_commands(home) == ["zzfresh"]

    def test_threshold_capture_resets_the_sync_counter(
        self, home: Path, workdir: Path
    ) -> None:
        """Reaching SYNC_THRESHOLD captures resets the counter and spawns a sync.

        Green companion to the xfail below: it proves the capture side of the
        auto-sync pipeline is reached, isolating the defect to the spawned
        entry point rather than the trigger.
        """
        for i in range(20):
            result = run_mem(
                ["_capture", f"tool{i} run", str(workdir), "0", "10"], home, workdir
            )
            assert result.returncode == 0

        counter = (home / ".mem" / ".sync_counter").read_text(encoding="utf-8").strip()
        assert counter == "0"

    def test_capture_cycle_triggers_a_real_background_sync(
        self, home: Path, workdir: Path
    ) -> None:
        """Capturing SYNC_THRESHOLD commands must produce a real sync.

        Full-cycle contract, straight through the binary: after 20 captures
        the detached background process must do the work `_sync` promises.
        Each captured command uses a distinct tool name so pattern extraction
        stays below its 5-command floor and no AI inference is attempted.
        """
        now = int(time.time())
        plant_commands(home, [("zzancient --flag", now - ANCIENT_AGE)])

        for i in range(20):
            result = run_mem(
                ["_capture", f"tool{i} run", str(workdir), "0", "10"], home, workdir
            )
            assert result.returncode == 0

        # The sync is detached, so poll instead of sleeping a fixed amount.
        deadline = time.time() + 5
        while time.time() < deadline:
            if "zzancient --flag" not in history_commands(home):
                break
            time.sleep(0.1)

        assert "zzancient --flag" not in history_commands(home)
        assert "tool0 run" in history_commands(home)


class TestCaptureSearchRoundTrip:
    """What the shell hook writes must be findable by the user's next search."""

    def test_captured_command_is_searchable(self, home: Path, workdir: Path) -> None:
        """`mem _capture` then `mem <query>` returns the captured command.

        Exercised through the binary because the hook and the search run in
        two different processes with nothing shared but the files on disk.
        """
        capture = run_mem(
            ["_capture", "kubectl get pods -n prod", str(workdir), "0", "120"],
            home,
            workdir,
        )
        assert capture.returncode == 0
        assert global_history(home).exists()

        search = run_mem(["--json", "kubectl"], home, workdir)
        assert search.returncode == 0

        payload = json.loads(search.stdout)
        assert [entry["command"] for entry in payload] == ["kubectl get pods -n prod"]
        assert payload[0]["exit_code"] == 0
        assert payload[0]["duration_ms"] == 120

    def test_plain_search_output_contains_the_command(
        self, home: Path, workdir: Path
    ) -> None:
        """The default (Rich) rendering also shows the captured command."""
        run_mem(
            ["_capture", "terraform apply", str(workdir), "0", "900"], home, workdir
        )

        search = run_mem(["terraform"], home, workdir)

        assert search.returncode == 0
        assert "terraform apply" in search.stdout

    def test_search_without_matches_exits_zero(self, home: Path, workdir: Path) -> None:
        """An empty result set is not an error — the shell must stay happy."""
        result = run_mem(["nothing-was-ever-captured"], home, workdir)

        assert result.returncode == 0


SHELLS = ["zsh", "bash", "fish"]

# Syntax-check invocation per shell; None means the shell has no parse-only mode.
SYNTAX_CHECK = {
    "zsh": ["zsh", "-n"],
    "bash": ["bash", "-n"],
    "fish": ["fish", "--no-execute"],
}


class TestInitHook:
    """`mem init <shell>` is the install path — a broken hook breaks capture."""

    @pytest.mark.parametrize("shell", SHELLS)
    def test_prints_a_non_empty_hook(
        self, shell: str, home: Path, workdir: Path
    ) -> None:
        """Output is non-empty, exits 0 and mentions the capture entry point."""
        result = run_mem(["init", shell], home, workdir)

        assert result.returncode == 0
        assert result.stdout.strip(), f"mem init {shell} printed nothing"
        assert "mem _capture" in result.stdout

    def test_rejects_unsupported_shell(self, home: Path, workdir: Path) -> None:
        """An unknown shell fails loudly instead of printing a broken hook."""
        result = run_mem(["init", "powershell"], home, workdir)

        assert result.returncode != 0
        assert not result.stdout.strip()

    @pytest.mark.parametrize("shell", SHELLS)
    def test_hook_is_syntactically_valid(
        self, shell: str, home: Path, workdir: Path, tmp_path: Path
    ) -> None:
        """The printed hook parses under its own shell.

        Users install it with `eval "$(mem init zsh)"`, so a syntax error
        would break every new shell session.
        """
        checker = SYNTAX_CHECK[shell]
        if shutil.which(checker[0]) is None:
            pytest.skip(f"{checker[0]} is not installed on this machine")

        result = run_mem(["init", shell], home, workdir)
        hook_file = tmp_path / f"hook.{shell}"
        hook_file.write_text(result.stdout, encoding="utf-8")

        check = subprocess.run(
            [*checker, str(hook_file)],
            capture_output=True,
            text=True,
            timeout=RUN_TIMEOUT,
        )

        assert check.returncode == 0, check.stderr

    @pytest.mark.parametrize("shell", SHELLS)
    def test_printed_hook_matches_the_hooks_file(
        self, shell: str, home: Path, workdir: Path
    ) -> None:
        """What `mem init` prints is what `hooks/mem.<shell>` contains.

        `click.echo` appends one newline, so compare on stripped text.
        """
        result = run_mem(["init", shell], home, workdir)
        expected = (HOOKS_DIR / f"mem.{shell}").read_text(encoding="utf-8")

        assert result.stdout.strip() == expected.strip()


def strip_comments(text: str) -> list[str]:
    """Reduce a shell script to its executable lines (no comments, no blanks)."""
    return [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


class TestInlineHookDuplication:
    """`cli.py` keeps inline copies of the hooks as a pip-install fallback.

    Two files describing the same behaviour is a divergence waiting to
    happen (P1-14): the copy is what pip users actually install, and
    nothing keeps it in sync with `hooks/`.
    """

    @pytest.mark.parametrize("shell", SHELLS)
    def test_inline_fallback_is_functionally_identical(self, shell: str) -> None:
        """The inline copy runs the same code as the hooks file.

        Comments may differ; a single executable line may not, or pip users
        get different capture behaviour than source installs.
        """
        from mem import cli

        inline = getattr(cli, f"_{shell.upper()}_HOOK", None)
        if inline is None:
            pytest.skip(f"cli.py no longer keeps an inline {shell} hook")

        expected = (HOOKS_DIR / f"mem.{shell}").read_text(encoding="utf-8")

        assert strip_comments(inline) == strip_comments(expected)

    @pytest.mark.parametrize("shell", SHELLS)
    @pytest.mark.xfail(
        strict=True,
        reason=(
            "P1-14: cli.py hardcodes a second, comment-stripped copy of every "
            "hook instead of shipping hooks/ as package data — the copies "
            "already differ textually and nothing enforces they stay in sync"
        ),
    )
    def test_hooks_have_a_single_source_of_truth(self, shell: str) -> None:
        """There must be exactly one definition of each shell hook.

        Either `cli.py` stops carrying an inline copy (preferred: ship
        `hooks/` as package data), or the copy is byte-identical to the file.
        """
        from mem import cli

        inline = getattr(cli, f"_{shell.upper()}_HOOK", None)
        if inline is None:
            return  # no duplicate copy at all — contract satisfied

        expected = (HOOKS_DIR / f"mem.{shell}").read_text(encoding="utf-8")

        assert inline.strip() == expected.strip()


def tree(root: Path) -> set[str]:
    """Every path under *root*, relative and sorted-comparable."""
    return {str(p.relative_to(root)) for p in root.rglob("*")}


class TestFilesystemContainment:
    """Constitution: mem owns `~/.mem` and nothing else."""

    def test_cli_writes_only_inside_mem_dir(self, home: Path, workdir: Path) -> None:
        """A representative command run leaves no file outside `$HOME/.mem`.

        Also asserts the working directory stays pristine — the CLI must not
        drop state next to whatever project the user happens to be in.
        """
        commands = [
            ["init", "zsh"],
            ["_capture", "ls -la", str(workdir), "0", "5"],
            ["ls"],
            ["stats"],
            ["save", "echo hi", "--comment", "greeting"],
            ["list"],
            ["vars", "set", "TOKEN", "abc"],
            ["vars", "list"],
            ["--version"],
        ]
        for args in commands:
            result = run_mem(args, home, workdir)
            assert result.returncode == 0, (
                f"mem {' '.join(args)} failed: {result.stderr}"
            )

        written = tree(home)
        assert written, "the CLI wrote nothing at all — the check would be vacuous"

        stray = {p for p in written if p != ".mem" and not p.startswith(".mem/")}
        assert stray == set(), f"mem wrote outside ~/.mem: {sorted(stray)}"
        assert tree(workdir) == set(), "mem wrote into the working directory"


NETWORK_STUB = '''\
"""Installed via PYTHONPATH: makes any socket creation explode."""

import socket


class NetworkBlocked(RuntimeError):
    """Raised when code under test tries to open a socket."""


_real_socket = socket.socket


class _BlockedSocket(_real_socket):
    # Subclass rather than replace with a function: the stdlib `ssl` module
    # does `class SSLSocket(socket)` at import time and needs a real class.
    def __init__(self, *args, **kwargs):
        raise NetworkBlocked("network access is forbidden")


def _deny(*args, **kwargs):
    raise NetworkBlocked("network access is forbidden")


socket.socket = _BlockedSocket
socket.create_connection = _deny
'''


@pytest.fixture
def no_network_env(tmp_path: Path, home: Path) -> dict[str, str]:
    """Environment where every socket in the child process raises."""
    stub_dir = tmp_path / "netstub"
    stub_dir.mkdir()
    (stub_dir / "sitecustomize.py").write_text(NETWORK_STUB, encoding="utf-8")

    existing = os.environ.get("PYTHONPATH", "")
    python_path = f"{stub_dir}{os.pathsep}{existing}" if existing else str(stub_dir)
    return mem_env(home, PYTHONPATH=python_path)


class TestNoNetwork:
    """Constitution Principle I: mem never touches the network."""

    def test_stub_actually_blocks_sockets(self, no_network_env: dict[str, str]) -> None:
        """Guard for the guard: without this, the tests below prove nothing."""
        probe = subprocess.run(
            [sys.executable, "-c", "import socket; socket.socket()"],
            env=no_network_env,
            capture_output=True,
            text=True,
            timeout=RUN_TIMEOUT,
        )

        assert probe.returncode != 0
        assert "NetworkBlocked" in probe.stderr

    def test_capture_and_search_work_without_sockets(
        self, home: Path, workdir: Path, no_network_env: dict[str, str]
    ) -> None:
        """Capture, search and stats behave identically with networking dead."""
        capture = run_mem(
            ["_capture", "docker compose up -d", str(workdir), "0", "300"],
            home,
            workdir,
            env=no_network_env,
        )
        assert capture.returncode == 0, capture.stderr

        search = run_mem(["--json", "docker"], home, workdir, env=no_network_env)
        assert search.returncode == 0, search.stderr
        assert [e["command"] for e in json.loads(search.stdout)] == [
            "docker compose up -d"
        ]

        stats = run_mem(["stats"], home, workdir, env=no_network_env)
        assert stats.returncode == 0, stats.stderr
        assert "docker compose up -d" in stats.stdout

    def test_import_does_not_open_sockets(self, no_network_env: dict[str, str]) -> None:
        """Importing the package must not create a socket as a side effect."""
        result = subprocess.run(
            [sys.executable, "-c", "import mem.cli; print('imported')"],
            env=no_network_env,
            capture_output=True,
            text=True,
            timeout=RUN_TIMEOUT,
        )

        assert result.returncode == 0, result.stderr
        assert "imported" in result.stdout


class TestStartupCost:
    """The CLI runs on every prompt — startup time is a feature."""

    def test_version_returns_quickly(self, home: Path, workdir: Path) -> None:
        """`mem --version` completes well under half a second.

        The real target is <50ms; the assertion uses a deliberately generous
        500ms ceiling (best of three runs) so a loaded CI box cannot make it
        flaky. It exists to catch order-of-magnitude regressions, such as an
        import-time AI SDK load, not to police milliseconds.
        """
        durations = []
        for _ in range(3):
            started = time.perf_counter()
            result = run_mem(["--version"], home, workdir)
            durations.append(time.perf_counter() - started)
            assert result.returncode == 0
            assert "mem" in result.stdout

        assert min(durations) < 0.5, f"mem --version took {min(durations):.3f}s"

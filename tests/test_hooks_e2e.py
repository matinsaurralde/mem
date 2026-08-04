"""End-to-end tests for the shell hooks that feed mem.

The hooks under ``src/mem/hooks/`` are the entry point for *every* piece of data mem
ever stores, yet they are shell code and therefore invisible to a pure Python
test suite. These tests spawn real interactive shells (``zsh -f -i``,
``bash --norc --noprofile -i``, ``fish -i``), install the hook exactly the way
a user would (``mem init <shell>``), run commands, and assert on what actually
lands in ``<HOME>/.mem/repos/*.jsonl``.

Isolation contract
------------------
``storage.MEM_DIR`` is ``Path.home() / ".mem"``, resolved at import time in the
*child* process. Every shell spawned here therefore receives a ``HOME`` that
points at a pytest ``tmp_path`` and a scrubbed environment, which makes it
impossible for these tests to read from — or write to — the developer's real
history. ``_run_shell`` asserts this invariant before spawning anything.

Robustness contract
-------------------
The hooks fire ``mem _capture`` as a *detached background job*, so the JSONL
file is written after the shell has already exited. Nothing here sleeps for a
fixed amount of time waiting for that: every read polls until the expected
record shows up (or the store goes quiet) with a hard timeout.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Sequence

import pytest

# --- Shell discovery ---------------------------------------------------------

ZSH = shutil.which("zsh")
BASH = shutil.which("bash")
FISH = shutil.which("fish")

# Argv that gives each shell a pristine, config-free interactive session.
# Interactive mode is mandatory: bash only runs PROMPT_COMMAND and zsh only
# runs precmd/preexec when interactive, and those are what drive the capture.
_SHELL_ARGV: dict[str, list[str]] = {
    "zsh": ["-f", "-i"],
    "bash": ["--norc", "--noprofile", "-i"],
    "fish": ["-i"],
}

_SHELL_PATH: dict[str, str | None] = {"zsh": ZSH, "bash": BASH, "fish": FISH}

# --- mem executable ----------------------------------------------------------


def _find_mem_bin() -> Path | None:
    """Locate the ``mem`` console script that belongs to the running venv."""
    candidate = Path(sys.executable).parent / "mem"
    if candidate.exists():
        return candidate
    found = shutil.which("mem")
    return Path(found) if found else None


MEM_BIN = _find_mem_bin()

pytestmark = pytest.mark.skipif(
    MEM_BIN is None,
    reason="the `mem` console script is not installed (pip install -e .)",
)

requires_zsh = pytest.mark.skipif(ZSH is None, reason="zsh not available")
requires_bash = pytest.mark.skipif(BASH is None, reason="bash not available")
requires_fish = pytest.mark.skipif(FISH is None, reason="fish not available")


def _bash_has_subsecond_clock() -> bool:
    """Does the discovered bash expose ``$EPOCHREALTIME``?

    ``$EPOCHREALTIME`` arrived in bash 5.0. macOS still ships 3.2 as
    ``/bin/bash``, and there is no process-free sub-second clock there, so the
    hook falls back to ``date`` and second resolution. That is a real
    limitation of the platform, not of the hook, and the assertion below is
    skipped rather than quietly weakened.
    """
    if BASH is None:
        return False
    probe = subprocess.run(
        [BASH, "-c", 'printf %s "${EPOCHREALTIME:-}"'],
        capture_output=True,
        text=True,
        timeout=30,
    )
    return bool(probe.stdout.strip())


requires_subsecond_bash = pytest.mark.skipif(
    not _bash_has_subsecond_clock(),
    reason="bash < 5.0 has no sub-second clock without forking (macOS ships 3.2)",
)

# --- Polling knobs -----------------------------------------------------------

_POLL_INTERVAL = 0.02
_POLL_TIMEOUT = 20.0
_QUIET_PERIOD = 0.6


# --- Hook installation -------------------------------------------------------


@pytest.fixture(scope="session")
def hook_files(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    """Materialise ``mem init <shell>`` output once per session.

    Contract: ``mem init`` must emit usable hook code for every supported
    shell. Writing it to a file (instead of ``eval "$(mem init zsh)"`` inside
    every test shell) keeps the spawned sessions fast and keeps the noise of
    mem's own startup out of the assertions.
    """
    assert MEM_BIN is not None
    out_dir = tmp_path_factory.mktemp("mem-hooks")
    hooks: dict[str, Path] = {}
    for shell in ("zsh", "bash", "fish"):
        result = subprocess.run(
            [str(MEM_BIN), "init", shell],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
        )
        assert result.returncode == 0, f"mem init {shell} failed: {result.stderr}"
        assert result.stdout.strip(), f"mem init {shell} produced no hook code"
        path = out_dir / f"mem.{shell}"
        path.write_text(result.stdout, encoding="utf-8")
        hooks[shell] = path
    return hooks


# --- Shell runner ------------------------------------------------------------


class ShellResult:
    """Outcome of one scripted interactive shell session."""

    def __init__(self, returncode: int, stdout: str, stderr: str, home: Path) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.home = home

    @property
    def mem_dir(self) -> Path:
        """The ``~/.mem`` of the throwaway HOME this session ran under."""
        return self.home / ".mem"


def _child_env(home: Path) -> dict[str, str]:
    """Build a scrubbed environment for a spawned shell.

    Only what a shell genuinely needs is passed through, so nothing from the
    developer's session (ZDOTDIR, BASH_ENV, a real HOME) can leak in.
    """
    assert MEM_BIN is not None
    path = os.pathsep.join(
        [str(MEM_BIN.parent), "/usr/bin", "/bin", "/usr/sbin", "/sbin"]
    )
    return {
        "HOME": str(home),
        "PATH": path,
        "TERM": "dumb",
        "LANG": "en_US.UTF-8",
        # UTF-8 mode makes argv decoding in the `mem` child deterministic,
        # independent of whatever locale the CI box happens to have.
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
    }


def _run_shell(
    shell: str,
    lines: Sequence[str],
    home: Path,
    cwd: Path,
    hook_files: dict[str, Path],
    install_hook: bool = True,
    setup: Sequence[str] = (),
    env_extra: dict[str, str] | None = None,
) -> ShellResult:
    """Run ``lines`` inside a real interactive ``shell`` and return the result.

    ``setup`` lines run *before* the hook is installed (so they are never
    captured); ``lines`` run after. The session always terminates with ``exit``
    unless the caller supplied its own.
    """
    shell_path = _SHELL_PATH[shell]
    assert shell_path is not None, f"{shell} is not available"

    # Safety net for the isolation contract: never let a test point a shell at
    # the real home directory.
    assert home != Path.home(), "refusing to run a hook test against the real HOME"
    home.mkdir(parents=True, exist_ok=True)
    cwd.mkdir(parents=True, exist_ok=True)

    script: list[str] = list(setup)
    if install_hook:
        script.append(f"source {hook_files[shell]}")
    script.extend(lines)
    if not script or not script[-1].startswith("exit"):
        script.append("exit")

    env = _child_env(home)
    if env_extra:
        env.update(env_extra)

    proc = subprocess.run(
        [shell_path, *_SHELL_ARGV[shell]],
        input="\n".join(script) + "\n",
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )
    return ShellResult(proc.returncode, proc.stdout, proc.stderr, home)


# --- JSONL readers -----------------------------------------------------------


def _read_records(home: Path) -> list[dict[str, Any]]:
    """Read every captured record from a throwaway HOME's mem store."""
    repos = home / ".mem" / "repos"
    if not repos.exists():
        return []
    records: list[dict[str, Any]] = []
    for path in sorted(repos.glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def _commands(records: Sequence[dict[str, Any]]) -> list[str]:
    """Project records down to their command text."""
    return [r["command"] for r in records]


def _wait_until(
    home: Path,
    predicate: Callable[[list[dict[str, Any]]], bool],
    timeout: float = _POLL_TIMEOUT,
) -> list[dict[str, Any]]:
    """Poll the store until ``predicate`` holds, then return the records.

    Captures are written by a detached background process, so polling — never
    a fixed sleep — is the only non-flaky way to observe them.
    """
    deadline = time.monotonic() + timeout
    records = _read_records(home)
    while not predicate(records) and time.monotonic() < deadline:
        time.sleep(_POLL_INTERVAL)
        records = _read_records(home)
    return records


def _wait_for_command(
    home: Path, needle: str, timeout: float = _POLL_TIMEOUT
) -> list[dict[str, Any]]:
    """Poll until a record whose command contains ``needle`` shows up."""
    records = _wait_until(
        home, lambda recs: any(needle in c for c in _commands(recs)), timeout
    )
    assert any(needle in c for c in _commands(records)), (
        f"no captured command contained {needle!r}; got {_commands(records)}"
    )
    return records


def _wait_for_quiescence(
    home: Path, timeout: float = _POLL_TIMEOUT
) -> list[dict[str, Any]]:
    """Poll until the store stops growing for ``_QUIET_PERIOD`` seconds.

    Needed for *negative* assertions ("this must never be captured"): the
    background writers finish in an arbitrary order, so absence is only
    meaningful once every writer has had a chance to land.
    """
    deadline = time.monotonic() + timeout
    last_count = -1
    stable_since = time.monotonic()
    while time.monotonic() < deadline:
        records = _read_records(home)
        if len(records) != last_count:
            last_count = len(records)
            stable_since = time.monotonic()
        elif time.monotonic() - stable_since >= _QUIET_PERIOD:
            return records
        time.sleep(_POLL_INTERVAL)
    return _read_records(home)


def _find(records: Sequence[dict[str, Any]], needle: str) -> dict[str, Any]:
    """Return the single record whose command contains ``needle``."""
    matches = [r for r in records if needle in r["command"]]
    assert len(matches) == 1, (
        f"expected exactly one record containing {needle!r}, "
        f"got {[m['command'] for m in matches]}"
    )
    return matches[0]


# --- Per-test scratch --------------------------------------------------------


@pytest.fixture
def shell_home(tmp_path: Path) -> Path:
    """A throwaway ``$HOME`` for one spawned shell session.

    Deliberately *not* ``tmp_path / "home"``: conftest's autouse isolation
    fixture already owns that name for the pytest process itself, and the
    spawned shell must get a directory nothing else writes to.
    """
    home = tmp_path / "shell-home"
    home.mkdir()
    return home


@pytest.fixture
def shell_cwd(tmp_path: Path) -> Path:
    """A working directory outside any git repo (records land in _global)."""
    cwd = tmp_path / "work"
    cwd.mkdir()
    return cwd


# --- 1. Round trip -----------------------------------------------------------


class TestRoundTrip:
    """The hook must move a command from the shell into the JSONL store."""

    @requires_zsh
    def test_zsh_round_trip(
        self, hook_files: dict[str, Path], shell_home: Path, shell_cwd: Path
    ) -> None:
        """zsh: a typed command lands in the store with its exact text."""
        result = _run_shell("zsh", ["echo hola_zsh"], shell_home, shell_cwd, hook_files)
        assert result.stdout.strip() == "hola_zsh"

        records = _wait_for_command(shell_home, "hola_zsh")
        record = _find(records, "hola_zsh")
        assert record["command"] == "echo hola_zsh"
        assert record["dir"] == str(shell_cwd.resolve())
        assert record["exit_code"] == 0
        assert isinstance(record["ts"], int) and record["ts"] > 0

    @requires_bash
    def test_bash_round_trip(
        self, hook_files: dict[str, Path], shell_home: Path, shell_cwd: Path
    ) -> None:
        """bash: a typed command lands in the store with its exact text."""
        result = _run_shell(
            "bash", ["echo hola_bash"], shell_home, shell_cwd, hook_files
        )
        assert result.stdout.strip() == "hola_bash"

        records = _wait_for_command(shell_home, "hola_bash")
        record = _find(records, "hola_bash")
        assert record["command"] == "echo hola_bash"
        assert record["dir"] == str(shell_cwd.resolve())
        assert record["exit_code"] == 0

    @requires_fish
    def test_fish_round_trip(
        self, hook_files: dict[str, Path], shell_home: Path, shell_cwd: Path
    ) -> None:
        """fish: a typed command lands in the store with its exact text."""
        _run_shell("fish", ["echo hola_fish"], shell_home, shell_cwd, hook_files)

        records = _wait_for_command(shell_home, "hola_fish")
        record = _find(records, "hola_fish")
        assert record["command"] == "echo hola_fish"
        assert record["exit_code"] == 0

    @requires_zsh
    def test_zsh_records_failing_exit_code(
        self, hook_files: dict[str, Path], shell_home: Path, shell_cwd: Path
    ) -> None:
        """A non-zero exit code is stored verbatim, not swallowed."""
        _run_shell(
            "zsh",
            ["sh -c 'exit 3'", "echo sentinel_done"],
            shell_home,
            shell_cwd,
            hook_files,
        )
        records = _wait_for_command(shell_home, "sentinel_done")
        assert _find(records, "exit 3")["exit_code"] == 3

    @requires_bash
    def test_bash_records_failing_exit_code(
        self, hook_files: dict[str, Path], shell_home: Path, shell_cwd: Path
    ) -> None:
        """A non-zero exit code is stored verbatim, not swallowed."""
        _run_shell(
            "bash", ["false", "echo sentinel_done"], shell_home, shell_cwd, hook_files
        )
        records = _wait_for_command(shell_home, "sentinel_done")
        assert _find(records, "false")["exit_code"] == 1


# --- 2. duration_ms resolution ----------------------------------------------


class TestDurationResolution:
    """duration_ms must have millisecond resolution in every shell.

    zsh and bash both derive the duration from ``$SECONDS``, an integer.
    Anything faster than a second is stored as ``0`` and everything else is
    rounded to whole seconds — which is why ~68% of the real history has
    ``duration_ms == 0``. fish reads ``$CMD_DURATION`` (already milliseconds),
    so the three shells do not even agree with each other.
    """

    @requires_zsh
    def test_zsh_records_subsecond_duration(
        self, hook_files: dict[str, Path], shell_home: Path, shell_cwd: Path
    ) -> None:
        """A ~300ms command must be recorded as ~300ms, not 0 and not 1000."""
        _run_shell(
            "zsh",
            ["sleep 0.3", "echo sentinel_done"],
            shell_home,
            shell_cwd,
            hook_files,
        )
        records = _wait_for_command(shell_home, "sentinel_done")
        duration = _find(records, "sleep 0.3")["duration_ms"]
        assert 200 <= duration < 1000, (
            f"expected sub-second resolution, got duration_ms={duration}"
        )

    @requires_bash
    @requires_subsecond_bash
    def test_bash_records_subsecond_duration(
        self, hook_files: dict[str, Path], shell_home: Path, shell_cwd: Path
    ) -> None:
        """A ~300ms command must be recorded as ~300ms, not 0 and not 1000."""
        _run_shell(
            "bash",
            ["sleep 0.3", "echo sentinel_done"],
            shell_home,
            shell_cwd,
            hook_files,
        )
        records = _wait_for_command(shell_home, "sentinel_done")
        duration = _find(records, "sleep 0.3")["duration_ms"]
        assert 200 <= duration < 1000, (
            f"expected sub-second resolution, got duration_ms={duration}"
        )

    @pytest.mark.parametrize("shell", ["zsh", "bash", "fish"])
    def test_hook_source_uses_millisecond_clock(
        self, hook_files: dict[str, Path], shell: str
    ) -> None:
        """No hook may time commands with the integer ``$SECONDS`` counter.

        A static check on what ``mem init`` actually emits, so it covers the
        real installation path and not just the files in the repo.

        Comments are stripped first: each hook explains at length *why* it does
        not use ``$SECONDS``, and grepping the prose would fail on the very
        documentation of the fix.
        """
        source = hook_files[shell].read_text(encoding="utf-8")
        code = "\n".join(
            line
            for line in source.splitlines()
            if line.strip() and not line.strip().startswith("#")
        )
        assert "SECONDS" not in code.replace("CMD_DURATION", ""), (
            f"the {shell} hook measures duration with $SECONDS (integer seconds)"
        )

    @requires_bash
    def test_bash_epochrealtime_arithmetic_is_exact(
        self, hook_files: dict[str, Path], shell_cwd: Path
    ) -> None:
        """Pin the bash 5 clock path on a machine that cannot reach it.

        macOS ships bash 3.2, so ``test_bash_records_subsecond_duration`` skips
        here and the ``$EPOCHREALTIME`` branch would only ever be exercised on
        CI. Stubbing the variable makes the arithmetic — strip the decimal
        separator, divide by 1000 — deterministic under any bash, including
        the comma separator some locales produce.
        """
        script = f"""
        EPOCHREALTIME="1712345678.500000"
        source {hook_files["bash"]}
        _mem_clock; a=$_mem_ms
        EPOCHREALTIME="1712345679,250000"
        _mem_clock; b=$_mem_ms
        echo "$a $b $(( b - a ))"
        """
        proc = subprocess.run(
            [str(BASH), "-c", script],
            capture_output=True,
            text=True,
            cwd=str(shell_cwd),
            timeout=60,
        )
        assert proc.returncode == 0, proc.stderr
        start, end, elapsed = proc.stdout.split()
        assert start == "1712345678500"
        assert end == "1712345679250"
        assert elapsed == "750"

    @requires_bash
    def test_bash_falls_back_to_whole_seconds_without_epochrealtime(
        self, hook_files: dict[str, Path], shell_cwd: Path
    ) -> None:
        """Without ``$EPOCHREALTIME`` the clock still reports milliseconds.

        bash 3.2 has no process-free sub-second clock, so the fallback loses
        resolution — but it must not change the *unit*, or the same field
        would mean seconds for some users and milliseconds for others.
        """
        script = f"""
        unset EPOCHREALTIME
        source {hook_files["bash"]}
        _mem_clock; echo "$_mem_ms"
        """
        proc = subprocess.run(
            [str(BASH), "-c", script],
            capture_output=True,
            text=True,
            cwd=str(shell_cwd),
            timeout=60,
        )
        assert proc.returncode == 0, proc.stderr
        now_ms = int(proc.stdout.strip())
        assert now_ms % 1000 == 0, "the fallback has second resolution by construction"
        assert abs(now_ms / 1000 - time.time()) < 120, "not a plausible wall clock"


# --- 3. The leading-space convention (P0-5) ----------------------------------


class TestLeadingSpacePrivacy:
    """A command prefixed with a space must never be captured.

    ``HISTCONTROL=ignorespace`` (bash) and ``HIST_IGNORE_SPACE`` (zsh) make the
    leading space the universal "do not record this" gesture. For a
    privacy-first tool, capturing it anyway overrides an explicit user
    decision — the user typed a secret precisely because they believed it would
    not be persisted.
    """

    @requires_zsh
    def test_zsh_ignores_space_prefixed_command(
        self, hook_files: dict[str, Path], shell_home: Path, shell_cwd: Path
    ) -> None:
        """zsh + HIST_IGNORE_SPACE: ' echo secreto' must not be persisted."""
        _run_shell(
            "zsh",
            [" echo secreto_zsh", "echo sentinel_done"],
            shell_home,
            shell_cwd,
            hook_files,
            setup=["setopt HIST_IGNORE_SPACE"],
        )
        _wait_for_command(shell_home, "sentinel_done")
        records = _wait_for_quiescence(shell_home)
        assert not any("secreto_zsh" in c for c in _commands(records)), (
            f"space-prefixed command leaked into the store: {_commands(records)}"
        )

    @requires_bash
    def test_bash_ignores_space_prefixed_command(
        self, hook_files: dict[str, Path], shell_home: Path, shell_cwd: Path
    ) -> None:
        """bash + HISTCONTROL=ignorespace: ' echo secreto' must not persist."""
        _run_shell(
            "bash",
            [" echo secreto_bash", "echo sentinel_done"],
            shell_home,
            shell_cwd,
            hook_files,
            env_extra={"HISTCONTROL": "ignorespace"},
        )
        _wait_for_command(shell_home, "sentinel_done")
        records = _wait_for_quiescence(shell_home)
        assert not any("secreto_bash" in c for c in _commands(records)), (
            f"space-prefixed command leaked into the store: {_commands(records)}"
        )


# --- 4. bash pipelines and command lists -------------------------------------


class TestBashCommandLines:
    """bash must store the command *line*, not one simple command from it.

    The bash hook reads ``$BASH_COMMAND`` from a DEBUG trap and guards against
    re-entry, so only the first simple command of a pipeline or a ``&&``/``||``
    list survives. The stored record is not a truncation the user can see — it
    is a different command that means something else.
    """

    @requires_bash
    def test_bash_captures_full_pipeline(
        self, hook_files: dict[str, Path], shell_home: Path, shell_cwd: Path
    ) -> None:
        """'echo alpha | tr a-z A-Z' must be stored whole, not as 'echo alpha'."""
        _run_shell(
            "bash",
            ["echo alpha | tr a-z A-Z", "echo sentinel_done"],
            shell_home,
            shell_cwd,
            hook_files,
        )
        _wait_for_command(shell_home, "sentinel_done")
        records = _wait_for_quiescence(shell_home)
        assert "echo alpha | tr a-z A-Z" in _commands(records), (
            f"pipeline stored corrupted: {_commands(records)}"
        )

    @requires_bash
    def test_bash_captures_full_or_list(
        self, hook_files: dict[str, Path], shell_home: Path, shell_cwd: Path
    ) -> None:
        """'false || echo recovered' must be stored whole, not as 'false'."""
        _run_shell(
            "bash",
            ["false || echo recovered", "echo sentinel_done"],
            shell_home,
            shell_cwd,
            hook_files,
        )
        _wait_for_command(shell_home, "sentinel_done")
        records = _wait_for_quiescence(shell_home)
        assert "false || echo recovered" in _commands(records), (
            f"command list stored corrupted: {_commands(records)}"
        )

    @requires_zsh
    def test_zsh_captures_full_pipeline(
        self, hook_files: dict[str, Path], shell_home: Path, shell_cwd: Path
    ) -> None:
        """zsh gets this right today — preexec receives the whole line."""
        _run_shell(
            "zsh",
            ["echo alpha | tr a-z A-Z", "echo sentinel_done"],
            shell_home,
            shell_cwd,
            hook_files,
        )
        records = _wait_for_command(shell_home, "tr a-z A-Z")
        assert "echo alpha | tr a-z A-Z" in _commands(records)

    @requires_bash
    def test_bash_hook_does_not_capture_its_own_installation(
        self, hook_files: dict[str, Path], shell_home: Path, shell_cwd: Path
    ) -> None:
        """Installing the hook must not write a record.

        The DEBUG trap is armed by the hook file itself, so the very next
        simple command it sees is the hook's own ``PROMPT_COMMAND=`` line.
        mem's internals end up in the user's history.
        """
        _run_shell("bash", ["echo sentinel_done"], shell_home, shell_cwd, hook_files)
        _wait_for_command(shell_home, "sentinel_done")
        records = _wait_for_quiescence(shell_home)
        assert not any("_mem_" in c for c in _commands(records)), (
            f"the hook captured its own installation: {_commands(records)}"
        )


class TestBashMirrorsShellHistory:
    """bash reads the command line out of history, so it inherits its rules.

    The contract this pins down: *mem remembers exactly what your shell
    remembers, and nothing else.* Reading ``history 1`` is what makes
    pipelines and ``&&`` lists survive intact, and the price is that every
    mechanism the user has for keeping a line out of history now keeps it out
    of mem too. That price is the feature.
    """

    @requires_bash
    def test_bash_honours_histignore(
        self, hook_files: dict[str, Path], shell_home: Path, shell_cwd: Path
    ) -> None:
        """A command matched by HISTIGNORE never reaches the store."""
        _run_shell(
            "bash",
            ["echo hidden_zzz", "echo sentinel_done"],
            shell_home,
            shell_cwd,
            hook_files,
            env_extra={"HISTIGNORE": "echo hidden*"},
        )
        _wait_for_command(shell_home, "sentinel_done")
        records = _wait_for_quiescence(shell_home)
        assert not any("hidden_zzz" in c for c in _commands(records)), (
            f"HISTIGNORE was overridden: {_commands(records)}"
        )

    @requires_bash
    def test_bash_captures_nothing_when_history_is_disabled(
        self, hook_files: dict[str, Path], shell_home: Path, shell_cwd: Path
    ) -> None:
        """``set +o history`` silences capture instead of storing stale lines.

        This is the failure the old ``$BASH_COMMAND`` implementation was built
        to avoid: with history off, a naive ``history 1`` returns whatever was
        recorded last and mem stores a command the user never ran. The
        history-number check turns that into silence.
        """
        result = _run_shell(
            "bash",
            ["echo never_recorded", "echo also_never"],
            shell_home,
            shell_cwd,
            hook_files,
            setup=["set +o history"],
        )
        records = _wait_for_quiescence(shell_home)
        assert _commands(records) == [], (
            f"captured something with history disabled: {_commands(records)}"
        )
        assert "_mem" not in result.stderr, result.stderr

    @requires_bash
    def test_bash_counts_a_repeated_command_twice(
        self, hook_files: dict[str, Path], shell_home: Path, shell_cwd: Path
    ) -> None:
        """Frequency survives: two identical runs are two records.

        Reading history by number could have collapsed repeats. It does not,
        because bash's default HISTCONTROL records duplicates — and frequency
        is the signal mem's whole ranking rests on.
        """
        _run_shell(
            "bash",
            ["echo repeated_cmd", "echo repeated_cmd", "echo sentinel_done"],
            shell_home,
            shell_cwd,
            hook_files,
        )
        _wait_for_command(shell_home, "sentinel_done")
        records = _wait_for_quiescence(shell_home)
        repeats = [c for c in _commands(records) if c == "echo repeated_cmd"]
        assert len(repeats) == 2, f"lost a repeat: {_commands(records)}"


class TestBashPromptCommandIsPreserved:
    """Installing the hook must not destroy the user's own prompt hooks.

    mem prepends itself to PROMPT_COMMAND — it has to run first to read ``$?``
    before anything else overwrites it — but "first" must not mean "instead
    of". bash 5.1 turned PROMPT_COMMAND into an array, and assigning a string
    to an array variable replaces element 0 and silently drops the rest, so
    anyone using a prompt framework would have lost their prompt.
    """

    @requires_bash
    def test_existing_scalar_prompt_command_still_runs(
        self, hook_files: dict[str, Path], shell_home: Path, shell_cwd: Path
    ) -> None:
        """A PROMPT_COMMAND set before the hook keeps firing after it."""
        result = _run_shell(
            "bash",
            ["echo sentinel_done"],
            shell_home,
            shell_cwd,
            hook_files,
            setup=["PROMPT_COMMAND='echo PREEXISTING_PROMPT'"],
        )
        _wait_for_command(shell_home, "sentinel_done")
        assert "PREEXISTING_PROMPT" in result.stdout, (
            f"the hook ate the user's PROMPT_COMMAND: {result.stdout!r}"
        )

    @requires_bash
    def test_array_prompt_command_keeps_every_element(
        self, hook_files: dict[str, Path], shell_home: Path, shell_cwd: Path
    ) -> None:
        """Every element of an array PROMPT_COMMAND survives (bash >= 5.1).

        Skipped on older bash, where PROMPT_COMMAND is a plain string and the
        scenario cannot exist.
        """
        version = subprocess.run(
            [
                str(BASH),
                "-c",
                'printf "%s.%s" "${BASH_VERSINFO[0]}" "${BASH_VERSINFO[1]}"',
            ],
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout.strip()
        major, _, minor = version.partition(".")
        if (int(major), int(minor)) < (5, 1):
            pytest.skip(f"bash {version} has no array PROMPT_COMMAND")

        result = _run_shell(
            "bash",
            ["echo sentinel_done"],
            shell_home,
            shell_cwd,
            hook_files,
            setup=["PROMPT_COMMAND=('echo FIRST_ELEMENT' 'echo SECOND_ELEMENT')"],
        )
        _wait_for_command(shell_home, "sentinel_done")
        assert "FIRST_ELEMENT" in result.stdout, result.stdout
        assert "SECOND_ELEMENT" in result.stdout, (
            f"array elements past the first were dropped: {result.stdout!r}"
        )


# --- 5. The hook must not break the shell ------------------------------------


# Background-job notifications bash/zsh would print if the capture were not
# properly disowned, e.g. "[1]+  Done   mem _capture ...".
_JOB_NOTIFICATION_MARKERS = ("[1]", "[2]", "Done", "Terminated", "suspended")


class TestHookDoesNotBreakShell:
    """Installing the hook must be invisible to the user's shell semantics."""

    @pytest.mark.parametrize(
        "shell",
        [
            pytest.param("zsh", marks=requires_zsh),
            pytest.param("bash", marks=requires_bash),
        ],
    )
    def test_exit_status_survives_the_hook(
        self,
        shell: str,
        hook_files: dict[str, Path],
        shell_home: Path,
        shell_cwd: Path,
    ) -> None:
        """``$?`` on the next prompt is the user's exit code, not the hook's."""
        result = _run_shell(
            shell,
            ["sh -c 'exit 7'", 'echo "STATUS=$?"'],
            shell_home,
            shell_cwd,
            hook_files,
        )
        assert "STATUS=7" in result.stdout

    @pytest.mark.parametrize(
        "shell",
        [
            pytest.param("zsh", marks=requires_zsh),
            pytest.param("bash", marks=requires_bash),
        ],
    )
    def test_shell_exit_code_propagates(
        self,
        shell: str,
        hook_files: dict[str, Path],
        shell_home: Path,
        shell_cwd: Path,
    ) -> None:
        """``exit 42`` still exits the shell with 42 while the hook is loaded."""
        result = _run_shell(shell, ["exit 42"], shell_home, shell_cwd, hook_files)
        assert result.returncode == 42

    @pytest.mark.parametrize(
        "shell",
        [
            pytest.param("zsh", marks=requires_zsh),
            pytest.param("bash", marks=requires_bash),
        ],
    )
    def test_hook_adds_no_stdout_noise(
        self,
        shell: str,
        hook_files: dict[str, Path],
        shell_home: Path,
        shell_cwd: Path,
    ) -> None:
        """stdout is byte-identical with and without the hook installed."""
        lines = ["echo first", "echo second"]
        hooked = _run_shell(shell, lines, shell_home, shell_cwd, hook_files)
        bare_home = shell_home.parent / f"{shell_home.name}-bare"
        bare = _run_shell(
            shell,
            lines,
            bare_home,
            shell_cwd,
            hook_files,
            install_hook=False,
        )
        assert hooked.stdout == bare.stdout == "first\nsecond\n"

    @pytest.mark.parametrize(
        "shell",
        [
            pytest.param("zsh", marks=requires_zsh),
            pytest.param("bash", marks=requires_bash),
        ],
    )
    def test_hook_emits_no_job_control_messages(
        self,
        shell: str,
        hook_files: dict[str, Path],
        shell_home: Path,
        shell_cwd: Path,
    ) -> None:
        """The backgrounded capture is disowned, so no "[1]+ Done" ever shows.

        Only *new* stderr content counts: a shell driven from a pipe prints its
        own noise (prompts, "no job control in this shell"), which is not the
        hook's doing. The baseline run is subtracted line by line.
        """
        lines = ["echo first", "echo second"]
        hooked = _run_shell(shell, lines, shell_home, shell_cwd, hook_files)
        bare_home = shell_home.parent / f"{shell_home.name}-bare"
        bare = _run_shell(
            shell,
            lines,
            bare_home,
            shell_cwd,
            hook_files,
            install_hook=False,
        )
        baseline = set(bare.stderr.splitlines())
        extra = [line for line in hooked.stderr.splitlines() if line not in baseline]
        offenders = [
            line
            for line in extra
            if any(marker in line for marker in _JOB_NOTIFICATION_MARKERS)
        ]
        assert not offenders, f"hook leaked job-control output: {offenders}"

    @pytest.mark.parametrize(
        "shell",
        [
            pytest.param("zsh", marks=requires_zsh),
            pytest.param("bash", marks=requires_bash),
        ],
    )
    def test_capture_failure_never_reaches_the_user(
        self,
        shell: str,
        hook_files: dict[str, Path],
        shell_home: Path,
        shell_cwd: Path,
    ) -> None:
        """A broken ``mem`` on PATH must not disturb the shell.

        The hook redirects stderr to /dev/null precisely so a failing capture
        stays invisible. Simulated by shadowing ``mem`` with a script that
        always fails loudly.
        """
        fake_bin = shell_home / "fakebin"
        fake_bin.mkdir(parents=True, exist_ok=True)
        fake_mem = fake_bin / "mem"
        fake_mem.write_text(
            "#!/bin/sh\necho 'mem exploded' >&2\nexit 1\n", encoding="utf-8"
        )
        fake_mem.chmod(0o755)

        env = _child_env(shell_home)
        result = _run_shell(
            shell,
            ["echo still_works"],
            shell_home,
            shell_cwd,
            hook_files,
            env_extra={"PATH": f"{fake_bin}{os.pathsep}{env['PATH']}"},
        )
        assert result.stdout == "still_works\n"
        assert "mem exploded" not in result.stderr
        # Proves the shim really did shadow the real binary — otherwise the
        # assertions above would pass without ever exercising a failure.
        assert _commands(_wait_for_quiescence(shell_home)) == []


# --- 6. Fidelity of the command text -----------------------------------------


_TRICKY_COMMANDS = [
    "echo 'single $VAR quoted'",
    "echo \"double 'nested' quotes\"",
    "echo 'unicode: ñ á 日本語 🚀'",
    "echo 'back`tick` inside quotes'",
    "echo 'a;b&c|d'",
    'echo "tab\\tand\\\\backslash"',
]


class TestCommandTextFidelity:
    """Quotes, ``$``, backticks and unicode must survive shell -> JSONL.

    The command text travels through argv and is re-encoded as JSON, so any
    quoting or encoding slip corrupts history silently.
    """

    @pytest.mark.parametrize(
        "shell",
        [
            pytest.param("zsh", marks=requires_zsh),
            pytest.param("bash", marks=requires_bash),
        ],
    )
    def test_preserves_command_text(
        self,
        shell: str,
        hook_files: dict[str, Path],
        shell_home: Path,
        shell_cwd: Path,
    ) -> None:
        """Every tricky line is stored exactly as typed, unexpanded.

        All of them run in a single session on purpose: the assertion is about
        the text that lands in the store, and one shell round trip is enough
        to cover every case without paying the spawn cost per command.
        """
        _run_shell(
            shell,
            [*_TRICKY_COMMANDS, "echo sentinel_done"],
            shell_home,
            shell_cwd,
            hook_files,
        )
        _wait_for_command(shell_home, "sentinel_done")
        stored = _commands(_wait_for_quiescence(shell_home))
        missing = [c for c in _TRICKY_COMMANDS if c not in stored]
        assert not missing, f"commands mangled in transit: {missing}; stored={stored}"

    @requires_zsh
    def test_variables_are_stored_unexpanded(
        self, hook_files: dict[str, Path], shell_home: Path, shell_cwd: Path
    ) -> None:
        """``$HOME`` in a command is stored as ``$HOME``, not as its value."""
        _run_shell(
            "zsh",
            ['echo "home is $HOME"', "echo sentinel_done"],
            shell_home,
            shell_cwd,
            hook_files,
        )
        records = _wait_for_command(shell_home, "sentinel_done")
        record = _find(records, "home is")
        assert record["command"] == 'echo "home is $HOME"'
        assert str(shell_home) not in record["command"]


# --- 7. The hook must not block the prompt -----------------------------------


@pytest.mark.perf
class TestHookPerformance:
    """Capture is fire-and-forget: it must not add latency to the prompt.

    ``mem _capture`` costs ~150ms of Python interpreter startup. If the hook
    ever waited on it, a 12-command session would grow by ~1.8s. Measuring the
    delta against an identical hook-less session keeps the threshold immune to
    how fast the machine is.

    Marked ``perf`` and deselected by default. The delta is a real measurement,
    but an absolute wall-clock threshold cannot gate a pull request on a shared
    runner: this passed locally in 1.95s and failed on CI at 1.39s added over 12
    commands. Run it deliberately with ``pytest -m perf``, and treat the number
    as the input to the startup work rather than as a pass/fail signal.
    """

    _COMMAND_COUNT = 12  # stays under the 20-capture auto-sync threshold
    _MAX_ADDED_SECONDS = 1.0

    def _timed_run(
        self,
        shell: str,
        hook_files: dict[str, Path],
        home: Path,
        cwd: Path,
        install_hook: bool,
    ) -> float:
        """Wall-clock time of one scripted session, in seconds."""
        lines = [f"true {i}" for i in range(self._COMMAND_COUNT)]
        start = time.monotonic()
        _run_shell(shell, lines, home, cwd, hook_files, install_hook=install_hook)
        return time.monotonic() - start

    @pytest.mark.parametrize(
        "shell",
        [
            pytest.param("zsh", marks=requires_zsh),
            pytest.param("bash", marks=requires_bash),
        ],
    )
    def test_hook_does_not_delay_the_prompt(
        self,
        shell: str,
        hook_files: dict[str, Path],
        tmp_path: Path,
    ) -> None:
        """A hooked session is not measurably slower than a bare one."""
        cwd = tmp_path / "work"
        cwd.mkdir()
        # Best-of-two on each side: takes the noise floor of a busy CI box out
        # of the comparison without making the test long.
        bare = min(
            self._timed_run(
                shell, hook_files, tmp_path / f"bare{i}", cwd, install_hook=False
            )
            for i in range(2)
        )
        hooked = min(
            self._timed_run(
                shell, hook_files, tmp_path / f"hooked{i}", cwd, install_hook=True
            )
            for i in range(2)
        )
        assert hooked - bare < self._MAX_ADDED_SECONDS, (
            f"the hook added {hooked - bare:.3f}s over {self._COMMAND_COUNT} "
            f"commands (bare={bare:.3f}s, hooked={hooked:.3f}s) — "
            "capture is blocking the prompt"
        )


# --- Isolation guard ---------------------------------------------------------


@requires_zsh
def test_harness_writes_only_inside_the_temporary_home(
    hook_files: dict[str, Path], shell_home: Path, shell_cwd: Path
) -> None:
    """Every byte a hook test produces stays inside the throwaway HOME.

    Guards the isolation contract itself: if a refactor ever let the spawned
    shell inherit the developer's real HOME, this fails instead of silently
    appending to their history.
    """
    _run_shell("zsh", ["echo isolated"], shell_home, shell_cwd, hook_files)
    _wait_for_command(shell_home, "isolated")

    mem_dir = shell_home / ".mem"
    assert mem_dir.is_dir()
    written = [p for p in mem_dir.rglob("*") if p.is_file()]
    assert written, "the hook wrote nothing at all"
    for path in written:
        assert mem_dir in path.parents, f"{path} escaped the temporary HOME"

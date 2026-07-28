"""Tests for pattern extraction (with mocked Apple FM SDK)."""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass
from types import ModuleType
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from conftest import make_command
from mem import patterns, storage
from mem.models import PatternFile

# `apple_fm_sdk` is made importable by conftest (only stubbed when genuinely
# missing), so `patch("apple_fm_sdk.LanguageModelSession", ...)` always
# resolves. The stub used to live here and keyed off `sys.modules`, which made
# the whole suite's behaviour depend on collection order — see conftest.

# Captured before conftest's `_no_real_inference` fixture replaces the module
# attribute, so the availability probe itself can still be tested.
_REAL_APPLE_FM_AVAILABLE = patterns._apple_fm_available


# ---------------------------------------------------------------------------
# Helpers for mocking Apple FM SDK guided generation
# ---------------------------------------------------------------------------


def _make_mock_generalized(pattern: str):
    """Create a mock object that looks like a @fm.generable result."""
    obj = MagicMock()
    obj.pattern = pattern
    return obj


def _build_generalize_map(mapping: dict[str, str]):
    """Build an async side_effect for session.respond() from a command->pattern map.

    Matches the "Command: ..." line at the end of the prompt to avoid
    false matches against example commands in the prompt template.
    """

    async def _respond(prompt: str, generating=None):
        # Extract the actual command from the "Command: <cmd>" line
        raw_cmd = None
        for line in prompt.splitlines():
            if line.startswith("Command:"):
                raw_cmd = line.split("Command:", 1)[1].strip()
                break

        if raw_cmd and raw_cmd in mapping:
            return _make_mock_generalized(mapping[raw_cmd])

        # Fallback: return the raw command unchanged
        return _make_mock_generalized(raw_cmd or "unknown")

    return _respond


@dataclass
class MockSession:
    """Fake LanguageModelSession that delegates to respond_fn."""

    respond_fn: object

    async def respond(self, prompt: str, generating=None):
        return await self.respond_fn(prompt, generating=generating)


# ---------------------------------------------------------------------------
# Test cases: Heuristic fallback (no SDK)
# ---------------------------------------------------------------------------


class TestHeuristicFallback:
    """Tests for pattern extraction when Apple FM SDK is unavailable."""

    def test_kubectl_heuristic(self, tmp_mem_dir):
        """Heuristic groups identical commands by frequency."""
        now = int(time.time())
        cmds = [
            "kubectl get pods",
            "kubectl get services",
            "kubectl get deployments",
            "kubectl get pods",
            "kubectl get nodes",
            "kubectl describe pod api-7f9b",
        ]
        for cmd in cmds:
            storage.append_command(
                make_command(command=cmd, ts=now, repo="/Users/test/projects/infra")
            )

        with patch.object(patterns, "_apple_fm_available", return_value=False):
            patterns.run_pattern_extraction("kubectl")

        result = storage.read_patterns("kubectl")
        assert result is not None
        assert result.tool == "kubectl"

        # Without the model there is no generalization: every distinct command
        # is its own "pattern", counted exactly.
        assert {p.pattern: p.frequency for p in result.patterns} == {
            "kubectl get pods": 2,
            "kubectl get services": 1,
            "kubectl get deployments": 1,
            "kubectl get nodes": 1,
            "kubectl describe pod api-7f9b": 1,
        }
        # Most frequent first
        assert result.patterns[0].pattern == "kubectl get pods"
        # Every pattern is its own example in the heuristic path
        assert all(p.pattern == p.example for p in result.patterns)
        # The cache of processed commands is populated for the next run
        assert set(result.processed_commands) == set(cmds)

    def test_git_heuristic(self, tmp_mem_dir):
        """Git commands are grouped by exact match."""
        now = int(time.time())
        cmds = [
            "git checkout main",
            "git checkout feature-branch",
            "git checkout develop",
            "git status",
            "git status",
            "git push origin main",
        ]
        for cmd in cmds:
            storage.append_command(
                make_command(command=cmd, ts=now, repo="/Users/test/projects/myapp")
            )

        with patch.object(patterns, "_apple_fm_available", return_value=False):
            patterns.run_pattern_extraction("git")

        result = storage.read_patterns("git")
        assert result is not None
        assert result.tool == "git"
        assert {p.pattern: p.frequency for p in result.patterns} == {
            "git checkout main": 1,
            "git checkout feature-branch": 1,
            "git checkout develop": 1,
            "git status": 2,
            "git push origin main": 1,
        }
        # "git status" appears twice, rest once
        top = result.patterns[0]
        assert top.pattern == "git status"
        assert top.frequency == 2

    def test_empty_history(self, tmp_mem_dir):
        """No commands for a tool should skip extraction gracefully."""
        patterns.run_pattern_extraction("nonexistent")
        result = storage.read_patterns("nonexistent")
        assert result is None

    @pytest.mark.parametrize("count", [0, 1, 4])
    def test_below_five_commands_is_skipped(self, tmp_mem_dir, count: int):
        """Fewer than 5 samples is not enough signal to call anything a pattern."""
        now = int(time.time())
        for i in range(count):
            storage.append_command(
                make_command(
                    command=f"npm run task-{i}",
                    ts=now,
                    repo="/Users/test/projects/myapp",
                )
            )

        patterns.run_pattern_extraction("npm")
        assert storage.read_patterns("npm") is None

    def test_exactly_five_commands_is_enough(self, tmp_mem_dir):
        """Boundary: 5 is the documented minimum, and it must be inclusive."""
        now = int(time.time())
        for i in range(5):
            storage.append_command(
                make_command(
                    command=f"npm run task-{i}",
                    ts=now,
                    repo="/Users/test/projects/myapp",
                )
            )

        with patch.object(patterns, "_apple_fm_available", return_value=False):
            patterns.run_pattern_extraction("npm")

        result = storage.read_patterns("npm")
        assert result is not None
        assert sum(p.frequency for p in result.patterns) == 5

    def test_heuristic_limits_to_10_patterns(self, tmp_mem_dir):
        """Heuristic returns at most 10 patterns."""
        now = int(time.time())
        for i in range(20):
            storage.append_command(
                make_command(
                    command=f"tool subcommand-{i}",
                    ts=now,
                    repo="/Users/test/projects/myapp",
                )
            )

        with patch.object(patterns, "_apple_fm_available", return_value=False):
            patterns.run_pattern_extraction("tool")

        result = storage.read_patterns("tool")
        assert result is not None
        assert len(result.patterns) == 10

    def test_heuristic_keeps_the_most_frequent_ten(self, tmp_mem_dir):
        """The 10-pattern cap keeps the top of the distribution, not an arbitrary slice."""
        now = int(time.time())
        for i in range(15):
            # subcommand-i runs (i + 1) times, so 5..14 are the top ten
            for _ in range(i + 1):
                storage.append_command(
                    make_command(
                        command=f"tool subcommand-{i}",
                        ts=now,
                        repo="/Users/test/projects/myapp",
                    )
                )

        with patch.object(patterns, "_apple_fm_available", return_value=False):
            patterns.run_pattern_extraction("tool")

        result = storage.read_patterns("tool")
        assert result is not None
        assert [p.pattern for p in result.patterns] == [
            f"tool subcommand-{i}" for i in range(14, 4, -1)
        ]


# ---------------------------------------------------------------------------
# Test cases: AI-powered extraction (mocked SDK)
# ---------------------------------------------------------------------------


class TestAIExtraction:
    """Tests for pattern extraction with mocked Apple FM SDK."""

    def test_kubectl_generalization(self, tmp_mem_dir):
        """AI generalizes kubectl get <resource> from concrete commands."""
        now = int(time.time())
        cmds = [
            "kubectl get pods",
            "kubectl get services",
            "kubectl get deployments",
            "kubectl get pods",
            "kubectl get nodes",
            "kubectl describe pod api-7f9b",
        ]
        for cmd in cmds:
            storage.append_command(
                make_command(command=cmd, ts=now, repo="/Users/test/projects/infra")
            )

        generalize_map = {
            "kubectl get pods": "kubectl get <resource>",
            "kubectl get services": "kubectl get <resource>",
            "kubectl get deployments": "kubectl get <resource>",
            "kubectl get nodes": "kubectl get <resource>",
            "kubectl describe pod api-7f9b": "kubectl describe <resource> <name>",
        }

        mock_session = MockSession(respond_fn=_build_generalize_map(generalize_map))

        with (
            patch.object(patterns, "_apple_fm_available", return_value=True),
            patch("mem.patterns._get_generable_types", return_value=MagicMock()),
            patch("apple_fm_sdk.LanguageModelSession", return_value=mock_session),
        ):
            patterns.run_pattern_extraction("kubectl")

        result = storage.read_patterns("kubectl")
        assert result is not None
        assert result.tool == "kubectl"

        # All "kubectl get *" commands should collapse into one pattern
        pattern_map = {p.pattern: p for p in result.patterns}
        assert "kubectl get <resource>" in pattern_map
        assert pattern_map["kubectl get <resource>"].frequency == 5

        # describe is separate
        assert "kubectl describe <resource> <name>" in pattern_map
        assert pattern_map["kubectl describe <resource> <name>"].frequency == 1

    def test_git_generalization(self, tmp_mem_dir):
        """AI generalizes git branch/commit patterns."""
        now = int(time.time())
        cmds = [
            "git checkout main",
            "git checkout feature-auth",
            "git checkout develop",
            "git status",
            "git status",
            "git commit -m 'fix bug'",
            "git commit -m 'add feature'",
        ]
        for cmd in cmds:
            storage.append_command(
                make_command(command=cmd, ts=now, repo="/Users/test/projects/myapp")
            )

        generalize_map = {
            "git checkout main": "git checkout <branch>",
            "git checkout feature-auth": "git checkout <branch>",
            "git checkout develop": "git checkout <branch>",
            "git status": "git status",
            "git commit -m 'fix bug'": "git commit -m '<message>'",
            "git commit -m 'add feature'": "git commit -m '<message>'",
        }

        mock_session = MockSession(respond_fn=_build_generalize_map(generalize_map))

        with (
            patch.object(patterns, "_apple_fm_available", return_value=True),
            patch("mem.patterns._get_generable_types", return_value=MagicMock()),
            patch("apple_fm_sdk.LanguageModelSession", return_value=mock_session),
        ):
            patterns.run_pattern_extraction("git")

        result = storage.read_patterns("git")
        assert result is not None

        pattern_map = {p.pattern: p for p in result.patterns}
        assert "git checkout <branch>" in pattern_map
        assert pattern_map["git checkout <branch>"].frequency == 3
        assert "git status" in pattern_map
        assert pattern_map["git status"].frequency == 2
        assert "git commit -m '<message>'" in pattern_map
        assert pattern_map["git commit -m '<message>'"].frequency == 2

    def test_docker_generalization(self, tmp_mem_dir):
        """AI generalizes docker image/container patterns."""
        now = int(time.time())
        cmds = [
            "docker build -t myapp:latest .",
            "docker build -t api:v2 .",
            "docker stop abc123",
            "docker stop def456",
            "docker ps",
            "docker ps -a",
        ]
        for cmd in cmds:
            storage.append_command(
                make_command(command=cmd, ts=now, repo="/Users/test/projects/myapp")
            )

        generalize_map = {
            "docker build -t myapp:latest .": "docker build -t <image>:<tag> .",
            "docker build -t api:v2 .": "docker build -t <image>:<tag> .",
            "docker stop abc123": "docker stop <container_id>",
            "docker stop def456": "docker stop <container_id>",
            "docker ps": "docker ps",
            "docker ps -a": "docker ps -a",
        }

        mock_session = MockSession(respond_fn=_build_generalize_map(generalize_map))

        with (
            patch.object(patterns, "_apple_fm_available", return_value=True),
            patch("mem.patterns._get_generable_types", return_value=MagicMock()),
            patch("apple_fm_sdk.LanguageModelSession", return_value=mock_session),
        ):
            patterns.run_pattern_extraction("docker")

        result = storage.read_patterns("docker")
        assert result is not None

        pattern_map = {p.pattern: p for p in result.patterns}
        assert "docker build -t <image>:<tag> ." in pattern_map
        assert pattern_map["docker build -t <image>:<tag> ."].frequency == 2
        assert "docker stop <container_id>" in pattern_map
        assert pattern_map["docker stop <container_id>"].frequency == 2

    def test_frequency_sum_matches_input(self, tmp_mem_dir):
        """Total frequency across all patterns equals input command count."""
        now = int(time.time())
        cmds = [
            "terraform plan",
            "terraform apply",
            "terraform plan",
            "terraform plan",
            "terraform init",
            "terraform destroy",
        ]
        for cmd in cmds:
            storage.append_command(
                make_command(command=cmd, ts=now, repo="/Users/test/projects/infra")
            )

        # AI keeps no-arg subcommands as-is (nothing to generalize)
        generalize_map = {
            "terraform plan": "terraform plan",
            "terraform apply": "terraform apply",
            "terraform init": "terraform init",
            "terraform destroy": "terraform destroy",
        }

        mock_session = MockSession(respond_fn=_build_generalize_map(generalize_map))

        with (
            patch.object(patterns, "_apple_fm_available", return_value=True),
            patch("mem.patterns._get_generable_types", return_value=MagicMock()),
            patch("apple_fm_sdk.LanguageModelSession", return_value=mock_session),
        ):
            patterns.run_pattern_extraction("terraform")

        result = storage.read_patterns("terraform")
        assert result is not None
        total = sum(p.frequency for p in result.patterns)
        assert total == len(cmds)

    def test_example_is_real_command(self, tmp_mem_dir):
        """Each pattern's example field must be a real input command."""
        now = int(time.time())
        cmds = [
            "ssh user@host1",
            "ssh user@host2",
            "ssh admin@host3",
            "ssh root@host4",
            "ssh user@host5",
        ]
        for cmd in cmds:
            storage.append_command(make_command(command=cmd, ts=now, repo=None))

        generalize_map = {cmd: "ssh <user>@<host>" for cmd in cmds}
        mock_session = MockSession(respond_fn=_build_generalize_map(generalize_map))

        with (
            patch.object(patterns, "_apple_fm_available", return_value=True),
            patch("mem.patterns._get_generable_types", return_value=MagicMock()),
            patch("apple_fm_sdk.LanguageModelSession", return_value=mock_session),
        ):
            patterns.run_pattern_extraction("ssh")

        result = storage.read_patterns("ssh")
        assert result is not None
        assert len(result.patterns) == 1
        p = result.patterns[0]
        assert p.pattern == "ssh <user>@<host>"
        assert p.example in cmds
        assert p.frequency == 5


# ---------------------------------------------------------------------------
# Test cases: Deduplication efficiency
# ---------------------------------------------------------------------------


class TestDeduplication:
    """Tests verifying that dedup-first strategy reduces LLM calls."""

    @pytest.mark.asyncio
    async def test_identical_commands_call_llm_once(self):
        """50 identical commands should result in only 1 LLM call."""
        call_count = 0

        async def _counting_respond(prompt: str, generating=None):
            nonlocal call_count
            call_count += 1
            return _make_mock_generalized("git status")

        mock_session = MockSession(respond_fn=_counting_respond)

        with (
            patch.object(patterns, "_apple_fm_available", return_value=True),
            patch("mem.patterns._get_generable_types", return_value=MagicMock()),
            patch("apple_fm_sdk.LanguageModelSession", return_value=mock_session),
        ):
            commands = ["git status"] * 50
            result = await patterns.extract_patterns_for_tool("git", commands)

        assert call_count == 1  # Only 1 unique command
        assert len(result.patterns) == 1
        assert result.patterns[0].frequency == 50

    @pytest.mark.asyncio
    async def test_mixed_duplicates_minimize_calls(self):
        """10 commands with 3 unique should make exactly 3 LLM calls."""
        call_count = 0
        map_ = {
            "npm install": "npm install",
            "npm test": "npm test",
            "npm run build": "npm run <script>",
        }

        async def _counting_respond(prompt: str, generating=None):
            nonlocal call_count
            call_count += 1
            for cmd, pattern in map_.items():
                if cmd in prompt:
                    return _make_mock_generalized(pattern)
            return _make_mock_generalized("unknown")

        mock_session = MockSession(respond_fn=_counting_respond)

        with (
            patch.object(patterns, "_apple_fm_available", return_value=True),
            patch("mem.patterns._get_generable_types", return_value=MagicMock()),
            patch("apple_fm_sdk.LanguageModelSession", return_value=mock_session),
        ):
            commands = ["npm install"] * 4 + ["npm test"] * 3 + ["npm run build"] * 3
            result = await patterns.extract_patterns_for_tool("npm", commands)

        assert call_count == 3  # 3 unique commands
        total = sum(p.frequency for p in result.patterns)
        assert total == 10


# ---------------------------------------------------------------------------
# Test cases: sync_all_patterns
# ---------------------------------------------------------------------------


class TestSyncAllPatterns:
    def test_sync_warns_without_sdk(self, tmp_mem_dir, capsys):
        """sync_all_patterns prints warning when SDK is unavailable (non-silent)."""
        now = int(time.time())
        for i in range(6):
            storage.append_command(
                make_command(
                    command=f"make target-{i}",
                    ts=now,
                    repo="/Users/test/projects/myapp",
                )
            )

        with patch.object(patterns, "_apple_fm_available", return_value=False):
            new, updated = patterns.sync_all_patterns()

        assert (new, updated) == (1, 0)
        captured = capsys.readouterr()
        assert "pip install cli-mem[ai]" in captured.err

    def test_sync_silent_no_output(self, tmp_mem_dir, capsys):
        """sync_all_patterns(silent=True) produces no output."""
        now = int(time.time())
        for i in range(6):
            storage.append_command(
                make_command(
                    command=f"make target-{i}",
                    ts=now,
                    repo="/Users/test/projects/myapp",
                )
            )

        with patch.object(patterns, "_apple_fm_available", return_value=False):
            new, updated = patterns.sync_all_patterns(silent=True)

        assert (new, updated) == (1, 0)
        captured = capsys.readouterr()
        assert captured.err == ""
        assert captured.out == ""

    def test_second_sync_reports_updated_not_new(self, tmp_mem_dir):
        """A tool that already has a pattern file counts as updated, not new."""
        now = int(time.time())
        for i in range(6):
            storage.append_command(
                make_command(
                    command=f"make target-{i}",
                    ts=now,
                    repo="/Users/test/projects/myapp",
                )
            )

        with patch.object(patterns, "_apple_fm_available", return_value=False):
            assert patterns.sync_all_patterns(silent=True) == (1, 0)
            assert patterns.sync_all_patterns(silent=True) == (0, 1)

    def test_sync_skips_tools_below_threshold(self, tmp_mem_dir):
        """Tools with <5 commands are skipped entirely."""
        now = int(time.time())
        for cmd in ["rare-tool arg1", "rare-tool arg2", "rare-tool arg3"]:
            storage.append_command(make_command(command=cmd, ts=now))

        with patch.object(patterns, "_apple_fm_available", return_value=False):
            new, updated = patterns.sync_all_patterns()

        assert new == 0
        assert updated == 0

    def test_sync_multiple_tools(self, tmp_mem_dir):
        """Sync handles multiple tools independently."""
        now = int(time.time())
        for i in range(6):
            storage.append_command(
                make_command(
                    command=f"tool-a subcmd-{i}", ts=now, repo="/Users/test/projects/a"
                )
            )
            storage.append_command(
                make_command(
                    command=f"tool-b subcmd-{i}", ts=now, repo="/Users/test/projects/b"
                )
            )
        # tool-c has too few
        for i in range(3):
            storage.append_command(
                make_command(
                    command=f"tool-c subcmd-{i}", ts=now, repo="/Users/test/projects/c"
                )
            )

        with patch.object(patterns, "_apple_fm_available", return_value=False):
            new, updated = patterns.sync_all_patterns()

        assert new == 2  # tool-a and tool-b, NOT tool-c
        assert storage.read_patterns("tool-a") is not None
        assert storage.read_patterns("tool-b") is not None
        assert storage.read_patterns("tool-c") is None


class TestPatternCaching:
    """Verify that already-processed commands skip the LLM."""

    @pytest.mark.asyncio
    async def test_cached_commands_skip_llm(self):
        """Commands in already_processed set should not trigger LLM calls."""
        call_count = 0

        async def _counting_respond(prompt: str, generating=None):
            nonlocal call_count
            call_count += 1
            for line in prompt.splitlines():
                if line.startswith("Command:"):
                    cmd = line.split("Command:", 1)[1].strip()
                    return _make_mock_generalized(f"{cmd} <generalized>")
            return _make_mock_generalized("unknown")

        mock_session = MockSession(respond_fn=_counting_respond)

        with (
            patch.object(patterns, "_apple_fm_available", return_value=True),
            patch("mem.patterns._get_generable_types", return_value=MagicMock()),
            patch("apple_fm_sdk.LanguageModelSession", return_value=mock_session),
        ):
            # First call: 3 unique commands, all new
            commands = ["git status", "git log", "git diff", "git status", "git log"]
            await patterns.extract_patterns_for_tool("git", commands)

        assert call_count == 3  # 3 unique commands

        # Second call with cache: only 1 new command
        call_count = 0
        already_done = {"git status", "git log", "git diff"}

        with (
            patch.object(patterns, "_apple_fm_available", return_value=True),
            patch("mem.patterns._get_generable_types", return_value=MagicMock()),
            patch("apple_fm_sdk.LanguageModelSession", return_value=mock_session),
        ):
            commands2 = commands + ["git push"]
            await patterns.extract_patterns_for_tool("git", commands2, already_done)

        assert call_count == 1  # Only "git push" is new


class TestPatternCacheSurvivesResync:
    """The cache must not corrupt patterns that were already generalized.

    ``run_pattern_extraction`` persists one ``example`` per pattern, and
    ``extract_patterns_for_tool`` rebuilds its command->pattern cache as
    ``{p.example: p.pattern}``. Every other command that collapsed into the
    same pattern is therefore absent from the cache on the next run, yet it is
    also absent from ``new_cmds`` (it *was* processed), so it falls through to
    the ``cmd_to_pattern[cmd] = cmd`` fallback and re-enters the pattern file
    as a raw, ungeneralized "pattern".
    """

    TOOL = "kubectl"
    INITIAL = [
        "kubectl get pods",
        "kubectl get services",
        "kubectl get deployments",
        "kubectl get ingresses",
        "kubectl get nodes",
    ]
    GENERALIZED = "kubectl get <resource>"

    def _run(self, extra: str | None = None) -> PatternFile | None:
        """Append an optional new command, then run one extraction pass."""
        now = int(time.time())
        if extra is not None:
            storage.append_command(
                make_command(command=extra, ts=now, repo="/Users/test/projects/infra")
            )

        mapping = {cmd: self.GENERALIZED for cmd in self.INITIAL}
        if extra is not None:
            mapping[extra] = self.GENERALIZED
        mock_session = MockSession(respond_fn=_build_generalize_map(mapping))

        with (
            patch.object(patterns, "_apple_fm_available", return_value=True),
            patch("mem.patterns._get_generable_types", return_value=MagicMock()),
            patch("apple_fm_sdk.LanguageModelSession", return_value=mock_session),
        ):
            patterns.run_pattern_extraction(self.TOOL)

        return storage.read_patterns(self.TOOL)

    def _seed(self) -> None:
        now = int(time.time())
        for cmd in self.INITIAL:
            storage.append_command(
                make_command(command=cmd, ts=now, repo="/Users/test/projects/infra")
            )

    def test_first_extraction_collapses_every_variant(self, tmp_mem_dir):
        """Baseline (passes today): one pass produces exactly one pattern."""
        self._seed()

        result = self._run()

        assert result is not None
        assert {p.pattern: p.frequency for p in result.patterns} == {
            self.GENERALIZED: 5
        }

    def test_resync_with_no_new_commands_is_a_no_op(self, tmp_mem_dir):
        """Passes today only because the "nothing new" early return skips the merge."""
        self._seed()
        self._run()

        result = self._run()

        assert result is not None
        assert {p.pattern: p.frequency for p in result.patterns} == {
            self.GENERALIZED: 5
        }

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "P1-4: the pattern cache is keyed by {example -> pattern} and only one "
            "example is persisted per pattern, so every other already-generalized "
            "command hits the raw-command fallback and the pattern file degrades "
            "into raw commands on each sync"
        ),
    )
    def test_second_extraction_preserves_existing_patterns(self, tmp_mem_dir):
        """One new command must not un-generalize the commands already learned.

        After a second pass that adds a single new sibling command, the tool
        must still have exactly one pattern, now covering all six commands.
        """
        self._seed()
        self._run()

        result = self._run(extra="kubectl get secrets")

        assert result is not None
        assert {p.pattern: p.frequency for p in result.patterns} == {
            self.GENERALIZED: 6
        }

    @pytest.mark.xfail(
        strict=True,
        reason="P1-4: already-generalized commands reappear as raw patterns",
    )
    def test_resync_never_reintroduces_raw_commands_as_patterns(self, tmp_mem_dir):
        """No pattern may be a verbatim copy of an input command after a resync.

        Stated as an invariant rather than an exact count, so it keeps its
        meaning if the extraction strategy changes.
        """
        self._seed()
        self._run()

        result = self._run(extra="kubectl get secrets")

        assert result is not None
        raw_inputs = set(self.INITIAL) | {"kubectl get secrets"}
        leaked = {p.pattern for p in result.patterns} & raw_inputs
        assert leaked == set(), f"raw commands leaked back as patterns: {leaked}"


class TestGeneralizationFailures:
    """Behaviour when the on-device model refuses or errors on a command."""

    @pytest.mark.asyncio
    async def test_failed_generalization_falls_back_to_the_raw_command(self):
        """A model error degrades one command, it does not lose it."""

        async def _respond(prompt: str, generating=None):
            if "git bisect" in prompt:
                raise RuntimeError("context window exceeded")
            return _make_mock_generalized("git status")

        mock_session = MockSession(respond_fn=_respond)

        with (
            patch.object(patterns, "_apple_fm_available", return_value=True),
            patch("mem.patterns._get_generable_types", return_value=MagicMock()),
            patch("apple_fm_sdk.LanguageModelSession", return_value=mock_session),
        ):
            result = await patterns.extract_patterns_for_tool(
                "git", ["git status", "git bisect start"]
            )

        by_pattern = {p.pattern: p.frequency for p in result.patterns}
        assert by_pattern == {"git status": 1, "git bisect start": 1}

    @pytest.mark.asyncio
    async def test_total_frequency_is_conserved_even_with_failures(self):
        """Every input command is accounted for in exactly one pattern."""

        async def _respond(prompt: str, generating=None):
            raise RuntimeError("model unavailable")

        mock_session = MockSession(respond_fn=_respond)

        with (
            patch.object(patterns, "_apple_fm_available", return_value=True),
            patch("mem.patterns._get_generable_types", return_value=MagicMock()),
            patch("apple_fm_sdk.LanguageModelSession", return_value=mock_session),
        ):
            commands = ["a x", "a y", "a x", "a z"]
            result = await patterns.extract_patterns_for_tool("a", commands)

        assert sum(p.frequency for p in result.patterns) == len(commands)


class TestAppleFmProbe:
    """The availability probe decides between the AI and heuristic paths."""

    def test_reports_unavailable_when_the_sdk_cannot_be_imported(self, monkeypatch):
        """A missing (or broken) SDK must degrade, never raise."""
        monkeypatch.setitem(sys.modules, "apple_fm_sdk", None)
        assert _REAL_APPLE_FM_AVAILABLE() is False

    def test_reports_available_when_the_sdk_imports(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "apple_fm_sdk", ModuleType("apple_fm_sdk"))
        assert _REAL_APPLE_FM_AVAILABLE() is True

    def test_generable_type_declares_the_pattern_field(self, monkeypatch):
        """Guided generation must ask the model for a single `pattern` string."""
        captured: dict[str, object] = {}
        fake = ModuleType("apple_fm_sdk")

        def _generable(description: str):
            captured["description"] = description
            return lambda cls: cls

        fake.generable = _generable  # type: ignore[attr-defined]
        fake.guide = lambda text: text  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "apple_fm_sdk", fake)

        cls = patterns._get_generable_types()

        assert cls.__name__ == "GeneralizedCommand"
        assert set(cls.__annotations__) == {"pattern"}
        # patterns.py uses `from __future__ import annotations`, so the
        # annotation is kept as a string.
        assert cls.__annotations__["pattern"] == "str"
        assert isinstance(captured["description"], str)


class TestAutoSync:
    """Verify the sync counter and auto-trigger logic."""

    def test_counter_increment(self, tmp_mem_dir):
        assert storage.read_sync_counter() == 0
        assert storage.increment_sync_counter() == 1
        assert storage.increment_sync_counter() == 2
        assert storage.read_sync_counter() == 2

    def test_counter_reset(self, tmp_mem_dir):
        storage.increment_sync_counter()
        storage.increment_sync_counter()
        storage.reset_sync_counter()
        assert storage.read_sync_counter() == 0

    def test_capture_triggers_sync_at_threshold(self, tmp_mem_dir):
        """After SYNC_THRESHOLD captures, _spawn_background_sync is called."""
        from mem import capture

        with (
            patch.object(storage, "SYNC_THRESHOLD", 3),
            patch.object(capture, "_spawn_background_sync") as mock_spawn,
            patch.object(capture, "get_git_repo", return_value=None),
        ):
            capture.capture_command("cmd1", "/tmp", 0, 100)
            capture.capture_command("cmd2", "/tmp", 0, 100)
            assert mock_spawn.call_count == 0

            capture.capture_command("cmd3", "/tmp", 0, 100)
            assert mock_spawn.call_count == 1

            # Counter reset, so next 3 should trigger again
            capture.capture_command("cmd4", "/tmp", 0, 100)
            capture.capture_command("cmd5", "/tmp", 0, 100)
            assert mock_spawn.call_count == 1

            capture.capture_command("cmd6", "/tmp", 0, 100)
            assert mock_spawn.call_count == 2


# ---------------------------------------------------------------------------
# Test cases: Session summary generation
# ---------------------------------------------------------------------------


class TestSessionSummary:
    @pytest.mark.asyncio
    async def test_summary_without_sdk(self):
        """Returns None when SDK is unavailable."""
        with patch.object(patterns, "_apple_fm_available", return_value=False):
            result = await patterns.generate_session_summary(["git status"])
        assert result is None

    @pytest.mark.asyncio
    async def test_summary_with_sdk(self):
        """Returns AI-generated summary when SDK is available."""
        mock_session = MagicMock()
        mock_session.respond = AsyncMock(
            return_value="Debugging API authentication flow"
        )

        with (
            patch.object(patterns, "_apple_fm_available", return_value=True),
            patch("apple_fm_sdk.LanguageModelSession", return_value=mock_session),
        ):
            result = await patterns.generate_session_summary(
                [
                    "git checkout fix-auth",
                    "pytest tests/test_auth.py",
                    "vim src/auth.py",
                    "pytest tests/test_auth.py",
                    "git commit -m 'fix token refresh'",
                ]
            )

        assert result == "Debugging API authentication flow"
        mock_session.respond.assert_called_once()

    @pytest.mark.asyncio
    async def test_summary_handles_sdk_error(self):
        """Returns None when SDK raises an exception."""
        mock_session = MagicMock()
        mock_session.respond = AsyncMock(side_effect=RuntimeError("model unavailable"))

        with (
            patch.object(patterns, "_apple_fm_available", return_value=True),
            patch("apple_fm_sdk.LanguageModelSession", return_value=mock_session),
        ):
            result = await patterns.generate_session_summary(["git status"])

        assert result is None

    @pytest.mark.asyncio
    async def test_summary_sends_every_command_to_the_model(self):
        """The prompt must contain the full command list, one per line."""
        mock_session = MagicMock()
        mock_session.respond = AsyncMock(return_value="a summary")
        commands = ["git checkout fix-auth", "pytest -q", "git commit -m 'x'"]

        with (
            patch.object(patterns, "_apple_fm_available", return_value=True),
            patch("apple_fm_sdk.LanguageModelSession", return_value=mock_session),
        ):
            await patterns.generate_session_summary(commands)

        prompt = mock_session.respond.await_args.args[0]
        assert "\n".join(commands) in prompt


class TestNoRealInferenceByDefault:
    """Guard on the guard: the unit suite must never hit the on-device model.

    A single real ``generate_session_summary`` call costs ~1-3s and returns
    non-deterministic prose. Before this branch, whether the suite did real
    inference depended on module collection order (see conftest). If the
    autouse fixture is ever removed, this test fails immediately instead of
    the suite quietly getting slower and flakier.
    """

    def test_sdk_looks_unavailable_without_the_ai_marker(self):
        assert patterns._apple_fm_available() is False

    def test_session_summary_short_circuits(self):
        import asyncio

        assert asyncio.run(patterns.generate_session_summary(["git status"])) is None


@pytest.mark.ai
class TestRealAppleFoundationModels:
    """Opt-in tests that run the REAL on-device model.

    Deselected by default (``addopts = -m "not ai"``) because they are slow
    and non-deterministic. Run them with ``pytest -m ai`` on a machine with
    Apple Intelligence enabled.
    """

    def test_real_model_generalizes_a_command(self):
        import asyncio

        if not _REAL_APPLE_FM_AVAILABLE():
            pytest.skip("apple-fm-sdk is not installed")

        mapping = asyncio.run(
            patterns._generalize_commands("git", ["git checkout main"])
        )

        pattern = mapping["git checkout main"]
        assert isinstance(pattern, str) and pattern.strip()

    def test_real_model_summarizes_a_session(self):
        import asyncio

        if not _REAL_APPLE_FM_AVAILABLE():
            pytest.skip("apple-fm-sdk is not installed")

        summary = asyncio.run(
            patterns.generate_session_summary(
                ["git checkout fix-auth", "pytest tests/test_auth.py", "git commit"]
            )
        )

        assert isinstance(summary, str)
        assert summary.strip()

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


# ---------------------------------------------------------------------------
# Ensure apple_fm_sdk is importable even when the real package is absent.
# We insert a stub module into sys.modules so that patch() can resolve
# "apple_fm_sdk.LanguageModelSession" without triggering ImportError.
# ---------------------------------------------------------------------------

if "apple_fm_sdk" not in sys.modules:
    _stub = ModuleType("apple_fm_sdk")
    _stub.LanguageModelSession = MagicMock  # type: ignore[attr-defined]
    sys.modules["apple_fm_sdk"] = _stub


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
        assert len(result.patterns) > 0

        # Heuristic returns exact commands as patterns
        pattern_strs = [p.pattern for p in result.patterns]
        assert "kubectl get pods" in pattern_strs

        # "kubectl get pods" appears twice, so it should be the top pattern
        top = result.patterns[0]
        assert top.pattern == "kubectl get pods"
        assert top.frequency == 2

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
        # "git status" appears twice, rest once
        top = result.patterns[0]
        assert top.pattern == "git status"
        assert top.frequency == 2

    def test_empty_history(self, tmp_mem_dir):
        """No commands for a tool should skip extraction gracefully."""
        patterns.run_pattern_extraction("nonexistent")
        result = storage.read_patterns("nonexistent")
        assert result is None

    def test_too_few_commands_skipped(self, tmp_mem_dir):
        """Tools with fewer than 5 commands are skipped."""
        now = int(time.time())
        for cmd in ["npm install", "npm test"]:
            storage.append_command(
                make_command(command=cmd, ts=now, repo="/Users/test/projects/myapp")
            )

        patterns.run_pattern_extraction("npm")
        result = storage.read_patterns("npm")
        assert result is None

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
        assert len(result.patterns) <= 10


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

        assert new == 1
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

        assert new == 1
        captured = capsys.readouterr()
        assert captured.err == ""

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

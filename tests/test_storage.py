"""Tests for the JSONL storage layer."""

from __future__ import annotations

import time

from conftest import make_command
from mem import storage
from mem.models import CommandPattern, PatternFile


class TestAppendAndRead:
    def test_append_and_read_command(self, tmp_mem_dir):
        """Append a command, read it back, verify all fields match."""
        cmd = make_command(command="docker compose up -d", repo="myapp", exit_code=0, duration_ms=3200)
        storage.append_command(cmd)

        results = list(storage.read_commands("myapp"))
        assert len(results) == 1
        assert results[0].command == "docker compose up -d"
        assert results[0].repo == "myapp"
        assert results[0].exit_code == 0
        assert results[0].duration_ms == 3200

    def test_multiple_repos_isolated(self, tmp_mem_dir):
        """Commands in different repos stay in different files."""
        cmd_a = make_command(command="make test", repo="repo-a")
        cmd_b = make_command(command="cargo build", repo="repo-b")
        storage.append_command(cmd_a)
        storage.append_command(cmd_b)

        results_a = list(storage.read_commands("repo-a"))
        results_b = list(storage.read_commands("repo-b"))
        assert len(results_a) == 1
        assert results_a[0].command == "make test"
        assert len(results_b) == 1
        assert results_b[0].command == "cargo build"

    def test_global_fallback(self, tmp_mem_dir):
        """Commands with no repo go to _global.jsonl."""
        cmd = make_command(command="ls -la", repo=None)
        storage.append_command(cmd)

        results = list(storage.read_commands("_global"))
        assert len(results) == 1
        assert results[0].command == "ls -la"
        assert results[0].repo is None

    def test_read_nonexistent_returns_empty(self, tmp_mem_dir):
        """Reading a missing JSONL file yields an empty iterator."""
        results = list(storage.read_commands("nonexistent"))
        assert results == []


class TestPatterns:
    def test_atomic_pattern_write(self, tmp_mem_dir):
        """write_patterns uses tmp file rename — no partial writes."""
        pf = PatternFile(
            tool="kubectl",
            patterns=[
                CommandPattern(pattern="kubectl get <resource>", example="kubectl get pods", frequency=42),
            ],
            last_updated=int(time.time()),
        )
        storage.write_patterns(pf)

        # Verify the tmp file doesn't linger
        tmp_path = storage.pattern_file("kubectl").with_suffix(".json.tmp")
        assert not tmp_path.exists()

        # Verify the pattern file exists and is valid
        result = storage.read_patterns("kubectl")
        assert result is not None
        assert result.tool == "kubectl"
        assert len(result.patterns) == 1
        assert result.patterns[0].pattern == "kubectl get <resource>"

    def test_read_nonexistent_pattern_returns_none(self, tmp_mem_dir):
        """Reading a missing pattern file returns None."""
        result = storage.read_patterns("nonexistent")
        assert result is None


class TestReadAll:
    def test_read_all_commands(self, tmp_mem_dir):
        """read_all_commands iterates across all repo files."""
        storage.append_command(make_command(command="cmd1", repo="repo-a"))
        storage.append_command(make_command(command="cmd2", repo="repo-b"))
        storage.append_command(make_command(command="cmd3", repo=None))

        results = list(storage.read_all_commands())
        commands = {r.command for r in results}
        assert commands == {"cmd1", "cmd2", "cmd3"}


class TestCorruptedLines:
    def test_skips_corrupted_lines(self, tmp_mem_dir):
        """Corrupted JSONL lines are skipped, not fatal."""
        storage.ensure_dirs()
        path = storage.repo_file("myapp")
        cmd = make_command(command="good command")
        with path.open("a") as f:
            f.write(cmd.to_jsonl() + "\n")
            f.write("THIS IS NOT JSON\n")
            f.write(cmd.to_jsonl() + "\n")

        results = list(storage.read_commands("myapp"))
        assert len(results) == 2

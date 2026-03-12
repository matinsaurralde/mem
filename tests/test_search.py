"""Tests for the search and ranking engine."""

from __future__ import annotations

import time

from conftest import make_command
from mem import search, storage


class TestScoring:
    def test_ranks_frequent_commands_higher(self, tmp_mem_dir):
        """Commands run more often should rank higher."""
        now = int(time.time())
        # Run "git status" 5 times, "git log" once
        for _ in range(5):
            storage.append_command(
                make_command(
                    command="git status", ts=now, repo="/Users/test/projects/myapp"
                )
            )
        storage.append_command(
            make_command(command="git log", ts=now, repo="/Users/test/projects/myapp")
        )

        results = search.search("git", current_repo="/Users/test/projects/myapp")
        commands = [cmd.command for cmd, _ in results]
        assert commands[0] == "git status"

    def test_ranks_recent_commands_higher(self, tmp_mem_dir):
        """Recent commands should rank higher than old ones (same frequency)."""
        now = int(time.time())
        old = now - (30 * 86400)  # 30 days ago
        storage.append_command(
            make_command(
                command="docker build .", ts=old, repo="/Users/test/projects/myapp"
            )
        )
        storage.append_command(
            make_command(
                command="docker compose up", ts=now, repo="/Users/test/projects/myapp"
            )
        )

        results = search.search("docker", current_repo="/Users/test/projects/myapp")
        commands = [cmd.command for cmd, _ in results]
        assert commands[0] == "docker compose up"

    def test_context_boost_for_same_repo(self, tmp_mem_dir):
        """Commands from the current repo should rank higher."""
        now = int(time.time())
        storage.append_command(
            make_command(
                command="make test", ts=now, repo="/Users/test/projects/other-repo"
            )
        )
        storage.append_command(
            make_command(command="make test", ts=now, repo="/Users/test/projects/myapp")
        )

        results = search.search("make", current_repo="/Users/test/projects/myapp")
        # The myapp version should score higher due to context boost
        assert len(results) >= 1
        top_cmd, _ = results[0]
        assert top_cmd.repo == "/Users/test/projects/myapp"

    def test_deduplication_keeps_highest_score(self, tmp_mem_dir):
        """Same command string should appear only once, with highest score."""
        now = int(time.time())
        storage.append_command(
            make_command(
                command="npm run dev", ts=now, repo="/Users/test/projects/myapp"
            )
        )
        storage.append_command(
            make_command(
                command="npm run dev", ts=now - 86400, repo="/Users/test/projects/myapp"
            )
        )

        results = search.search("npm", current_repo="/Users/test/projects/myapp")
        commands = [cmd.command for cmd, _ in results]
        assert commands.count("npm run dev") == 1

    def test_empty_query_returns_empty(self, tmp_mem_dir):
        """Empty query should return no results."""
        storage.append_command(
            make_command(command="ls", repo="/Users/test/projects/myapp")
        )
        results = search.search("", current_repo="/Users/test/projects/myapp")
        assert results == []

    def test_no_matches_returns_empty(self, tmp_mem_dir):
        """Query with no matches returns empty list, not an error."""
        storage.append_command(
            make_command(command="git status", repo="/Users/test/projects/myapp")
        )
        results = search.search(
            "nonexistent-tool", current_repo="/Users/test/projects/myapp"
        )
        assert results == []

    def test_global_fallback_when_no_repo(self, tmp_mem_dir):
        """Search works when not inside any git repo."""
        now = int(time.time())
        storage.append_command(make_command(command="ls -la", ts=now, repo=None))

        results = search.search("ls", current_repo=None)
        assert len(results) == 1
        assert results[0][0].command == "ls -la"

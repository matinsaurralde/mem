"""Tests for pattern extraction (with mocked Apple FM SDK)."""

from __future__ import annotations

import time
from unittest.mock import patch

from conftest import make_command
from mem import patterns, storage


class TestPatternExtraction:
    def test_kubectl_patterns(self, tmp_mem_dir):
        """Given kubectl history, verify patterns contain generalized forms."""
        now = int(time.time())
        kubectl_cmds = [
            "kubectl get pods",
            "kubectl get services",
            "kubectl get deployments",
            "kubectl get pods",
            "kubectl get nodes",
            "kubectl describe pod api-7f9b",
        ]
        for cmd in kubectl_cmds:
            storage.append_command(make_command(command=cmd, ts=now, repo="infra"))

        with patch.object(patterns, "_apple_fm_available", return_value=False):
            patterns.run_pattern_extraction("kubectl")

        result = storage.read_patterns("kubectl")
        assert result is not None
        assert result.tool == "kubectl"
        assert len(result.patterns) > 0

    def test_git_patterns(self, tmp_mem_dir):
        """Given git history, verify patterns are extracted."""
        now = int(time.time())
        git_cmds = [
            "git checkout main",
            "git checkout feature-branch",
            "git checkout develop",
            "git status",
            "git status",
            "git push origin main",
        ]
        for cmd in git_cmds:
            storage.append_command(make_command(command=cmd, ts=now, repo="myapp"))

        with patch.object(patterns, "_apple_fm_available", return_value=False):
            patterns.run_pattern_extraction("git")

        result = storage.read_patterns("git")
        assert result is not None
        assert result.tool == "git"

    def test_empty_history(self, tmp_mem_dir):
        """No commands for a tool should skip extraction gracefully."""
        patterns.run_pattern_extraction("nonexistent")
        result = storage.read_patterns("nonexistent")
        assert result is None

    def test_too_few_commands_skipped(self, tmp_mem_dir):
        """Tools with fewer than 5 commands are skipped."""
        now = int(time.time())
        for cmd in ["npm install", "npm test"]:
            storage.append_command(make_command(command=cmd, ts=now, repo="myapp"))

        patterns.run_pattern_extraction("npm")
        result = storage.read_patterns("npm")
        assert result is None

    def test_graceful_degradation_without_sdk(self, tmp_mem_dir):
        """Pattern extraction works without apple-fm-sdk (heuristic fallback)."""
        now = int(time.time())
        for i in range(6):
            storage.append_command(
                make_command(command=f"docker build -t app:{i} .", ts=now, repo="myapp")
            )

        with patch.object(patterns, "_apple_fm_available", return_value=False):
            patterns.run_pattern_extraction("docker")

        result = storage.read_patterns("docker")
        assert result is not None
        assert len(result.patterns) > 0

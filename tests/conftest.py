"""Shared test fixtures for mem test suite."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from mem import models
from mem import storage


@pytest.fixture
def tmp_mem_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect MEM_DIR to a temporary directory for test isolation."""
    monkeypatch.setattr(storage, "MEM_DIR", tmp_path)
    return tmp_path


def make_command(
    command: str = "git status",
    ts: int | None = None,
    dir: str = "/Users/test/projects/myapp",
    repo: str | None = "/Users/test/projects/myapp",
    exit_code: int = 0,
    duration_ms: int = 50,
    session: str | None = None,
) -> models.CapturedCommand:
    """Factory for creating CapturedCommand instances in tests."""
    return models.CapturedCommand(
        command=command,
        ts=ts or int(time.time()),
        dir=dir,
        repo=repo,
        exit_code=exit_code,
        duration_ms=duration_ms,
        session=session,
    )

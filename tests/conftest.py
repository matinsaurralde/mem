"""Shared test fixtures for the mem test suite.

Two invariants are enforced here for *every* test, whether it asks for them
or not:

1. **No test may ever touch the developer's real ``~/.mem``.**  ``tmp_mem_dir``
   is ``autouse`` so isolation is the default rather than something each test
   has to remember to opt into, and ``$HOME`` itself is redirected so even code
   that calls ``Path.home()`` directly lands in a throwaway directory.

2. **No test may run real on-device inference.**  Apple Foundation Models
   inference costs seconds per call and its output is non-deterministic, so
   the unit suite pretends the SDK is unavailable unless a test explicitly
   opts in with ``@pytest.mark.ai`` (deselected by default via ``addopts``).
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock

import pytest

from mem import models
from mem import patterns
from mem import storage


def _ensure_apple_fm_importable() -> None:
    """Make ``apple_fm_sdk`` importable so ``mock.patch`` can target it.

    ``mem.patterns`` imports the SDK lazily, so on a machine without
    ``apple-fm-sdk`` installed a call like
    ``patch("apple_fm_sdk.LanguageModelSession")`` would raise ImportError at
    patch time and the AI tests would fail for the wrong reason.

    Only stub when the real package is genuinely missing. The previous version
    of this shim lived at the top of ``test_patterns.py`` and keyed off
    ``"apple_fm_sdk" not in sys.modules``, which was true even when the real
    SDK *was* installed (it is imported lazily). The result was a
    collection-order-dependent suite: running ``test_capture.py`` alone did
    ~1.5s of real inference, while running the whole suite silently replaced
    the SDK with a MagicMock. Deciding by importability instead of by
    ``sys.modules`` makes the behaviour deterministic.
    """
    try:
        import apple_fm_sdk  # noqa: F401
    except ImportError:
        stub = ModuleType("apple_fm_sdk")
        stub.LanguageModelSession = MagicMock  # type: ignore[attr-defined]
        stub.generable = lambda *a, **kw: lambda cls: cls  # type: ignore[attr-defined]
        stub.guide = lambda *a, **kw: None  # type: ignore[attr-defined]
        sys.modules["apple_fm_sdk"] = stub


_ensure_apple_fm_importable()


@pytest.fixture(autouse=True)
def tmp_mem_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect MEM_DIR *and* ``$HOME`` to temporary directories.

    Autouse on purpose: a test that forgets to request isolation would read
    and write the developer's real history. Making isolation opt-out rather
    than opt-in removes that entire failure mode.

    ``$HOME`` is redirected separately from ``MEM_DIR`` because
    ``storage.MEM_DIR`` is computed once at import time from ``Path.home()``;
    monkeypatching the module constant covers ``mem.storage``, but any code
    (or subprocess) resolving ``~`` on its own would still escape. Pointing
    ``$HOME`` at an empty directory closes that hole, and keeps the two paths
    distinct so an accidental ``Path.home() / ".mem"`` shows up as an empty
    directory instead of silently sharing state with MEM_DIR.
    """
    # Deliberately dot-prefixed and distinctive: `tmp_path` is shared with the
    # test body, and a plain "home" collides with fixtures that create their
    # own (test_e2e.py builds one for its subprocesses).
    fake_home = tmp_path / ".isolated-home"
    fake_home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("USERPROFILE", str(fake_home))

    monkeypatch.setattr(storage, "MEM_DIR", tmp_path)
    # Group constants are computed at import time from MEM_DIR, so patch them too
    monkeypatch.setattr(storage, "GROUPS_DIR", tmp_path / "groups")
    monkeypatch.setattr(storage, "GROUPS_REPOS_DIR", tmp_path / "groups" / "repos")
    monkeypatch.setattr(
        storage, "GROUPS_GLOBAL_FILE", tmp_path / "groups" / "_global.json"
    )
    monkeypatch.setattr(storage, "SYNC_COUNTER_FILE", tmp_path / ".sync_counter")
    monkeypatch.setattr(storage, "VARS_FILE", tmp_path / "vars.json")
    return tmp_path


@pytest.fixture(autouse=True)
def _no_real_inference(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Force the "SDK unavailable" branch unless a test is marked ``ai``.

    Without this, any code path reaching ``patterns.generate_session_summary``
    or ``patterns._generalize_commands`` would run a real on-device model:
    seconds per call, non-deterministic text, and a suite whose runtime
    depends on whether the machine has Apple Intelligence enabled.

    Tests that exercise the AI code path re-patch ``_apple_fm_available`` to
    ``True`` themselves (with a mocked ``LanguageModelSession``), which shadows
    this fixture for the duration of their ``with`` block.
    """
    if "ai" in request.keywords:
        return
    monkeypatch.setattr(patterns, "_apple_fm_available", lambda: False)


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """Create a real (empty) git repository in a temp dir and return its root.

    Used by repo-detection tests so they assert against a repository they
    created, instead of against whatever directory pytest happens to run in.
    """
    import subprocess

    root = tmp_path / "workrepo"
    root.mkdir()
    subprocess.run(
        ["git", "init", "-q"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    # macOS resolves /var -> /private/var, and `git rev-parse --show-toplevel`
    # returns the resolved path. Return the resolved form so tests can compare
    # with `==` instead of a fuzzy `endswith`.
    return root.resolve()


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

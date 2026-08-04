"""Durable, owner-only file primitives — standard library only.

These live apart from :mod:`mem.storage` for one reason: ``storage`` imports
Pydantic, and the interactive finder cannot afford that import. The finder
still has to write a file safely (it records which command you picked), and
the alternative to sharing this module is a second atomic-write
implementation that drifts from the first.

That is not a hypothetical worry in this codebase. A second copy of the shell
hooks is what put stale capture code in front of every pip user, and a second
copy of the ranking formula would have made the same history sort differently
depending on how it was asked for. One implementation, two callers.

``mem.storage`` re-exports everything here under its historical names, so
nothing that already imports from ``storage`` needs to change.
"""

from __future__ import annotations

import fcntl
import os
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

# Everything under ~/.mem is owner-only. A shell history is at least as
# sensitive as ~/.zsh_history (0600) — it records whatever was typed,
# including the secrets that were typed by mistake.
DIR_MODE = 0o700
FILE_MODE = 0o600


def harden_dir(path: Path) -> None:
    """Force a directory to DIR_MODE, ignoring the ambient umask."""
    try:
        if stat.S_IMODE(path.stat().st_mode) != DIR_MODE:
            path.chmod(DIR_MODE)
    except OSError:
        pass  # A directory we cannot stat or chmod is not ours to fix.


def harden_file(path: Path) -> None:
    """Force a file to FILE_MODE.

    Also acts as the migration path for existing installs: O_CREAT only
    applies its mode when it actually creates the file, so history written
    before this change stays 0644 until something chmods it.
    """
    try:
        if stat.S_IMODE(path.stat().st_mode) != FILE_MODE:
            path.chmod(FILE_MODE)
    except OSError:
        pass


def fsync_dir(path: Path) -> None:
    """fsync a directory so a rename() into it survives power loss.

    Renaming is atomic with respect to other processes, but the directory
    entry itself is not durable until the directory is synced. For
    vars.json that difference is the user's credentials.
    """
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def atomic_write(path: Path, data: str, mode: int = FILE_MODE) -> None:
    """Replace a file's contents atomically and durably.

    Uses mkstemp for the temporary name. An earlier implementation derived it
    from the target path, so two processes writing the same file raced on one
    shared `.tmp` and the loser's rename() failed with FileNotFoundError.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    harden_dir(path.parent)
    fd, tmp_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
        fsync_dir(path.parent)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


_lock_depth = 0


@contextmanager
def exclusive_lock(path: Path) -> Iterator[None]:
    """Serialize every mutating operation behind a single lock file.

    Why one global lock instead of one per file: rotate() and
    forget_commands() rewrite every JSONL in one pass, so per-file locking
    would need a defined acquisition order to stay deadlock-free. Appends take
    microseconds and already run in a detached background process, so the
    contention a single lock adds is not observable at shell speed.

    Why a dedicated lock file instead of locking the data file: those same
    operations replace data files via rename(), so a lock held on a data file
    would be guarding an inode that is no longer the one at that path — an
    appender could acquire "the lock" on the orphaned inode and write into a
    file that has already been unlinked.

    Re-entrant within a process. flock() is tied to the open file description,
    so a nested acquisition through a second file descriptor would deadlock
    against its own outer hold — and nesting is normal here: forget_commands()
    scrubs patterns, groups and vars, each of which locks on its own.

    The depth counter is module-level, so every caller — including the ones
    that cannot import ``mem.storage`` — shares one re-entrancy state. Callers
    must therefore pass the same lock path.
    """
    global _lock_depth

    if _lock_depth > 0:
        _lock_depth += 1
        try:
            yield
        finally:
            _lock_depth -= 1
        return

    path.parent.mkdir(parents=True, exist_ok=True)
    harden_dir(path.parent)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, FILE_MODE)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        _lock_depth = 1
        try:
            yield
        finally:
            _lock_depth = 0
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)

"""Experiment's platform-specific file persistence operations.

Callers still own locks, hashes, immutable identities, and recovery. File
flush/fsync failures always propagate. POSIX also syncs directory entries.
Windows' CRT cannot open directories for fsync; on Windows we validate the
directory and rely on flushed files plus same-directory os.replace and
readback for process-interruption recovery. This does NOT promise POSIX-like
directory persistence after power loss. No ACL changes or elevation are used.
"""
from __future__ import annotations

import errno
import os
import stat
from pathlib import Path

_WINDOWS = os.name == "nt"


def fsync_directory(path: Path) -> None:
    if _WINDOWS:
        # An explicit platform contract, not an exception-based fallback:
        # missing paths, non-directories and genuine access errors still fail.
        if not stat.S_ISDIR(path.stat().st_mode):
            raise NotADirectoryError(errno.ENOTDIR, "not a directory", str(path))
        return
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def fsync_file(path: Path, *, nofollow: bool = False) -> None:
    # Windows _commit/FlushFileBuffers requires a writable handle, even when
    # recovery only needs to re-flush existing bytes. O_RDWR never truncates.
    flags = os.O_RDWR if _WINDOWS else os.O_RDONLY
    flags |= getattr(os, "O_BINARY", 0)
    if nofollow:
        flags |= getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def retryable_io_error(exc: BaseException) -> bool:
    """Only identify transient I/O failures; do not loop on permission bugs."""
    return isinstance(exc, OSError) and (
        exc.errno in {errno.EINTR, errno.EAGAIN, errno.EBUSY}
        or getattr(exc, "winerror", None) in {32, 33}
    )

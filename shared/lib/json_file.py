"""Atomic JSON objects shared by cooperating processes.

Readers take the same sidecar lock as writers. POSIX permits reading an old inode
while it is replaced; Windows may deny either open or replacement in that race.
The lock also makes read/modify/write transactions indivisible on both platforms.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from shared.lib.filelock import exclusive
from shared.lib.filesystem import io_path


def read_object(path: Path) -> dict[str, Any]:
    with exclusive(path.with_suffix(path.suffix + ".lock")):
        return _read(path)


def _read(path: Path) -> dict[str, Any]:
    value = json.loads(io_path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object: {path}")
    return value


def write_object(path: Path, value: dict[str, Any], *, merge: bool = False) -> None:
    with exclusive(path.with_suffix(path.suffix + ".lock")):
        if merge:
            value = {**_read(path), **value}
        target = io_path(path)
        fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=target.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
                json.dump(value, stream, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        finally:
            Path(temporary).unlink(missing_ok=True)

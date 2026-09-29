"""Native filesystem paths for Python I/O (never for URLs or shell arguments)."""
from __future__ import annotations
import os
from pathlib import Path


def io_path(path: os.PathLike | str) -> Path:
    """Allow Windows file I/O beyond MAX_PATH without changing machine policy.

    Runtime state may be nested below a user-selected data directory. The extended
    absolute syntax works whether or not LongPathsEnabled is set by an admin.
    Keep normal paths everywhere else, particularly Git and browser references.
    """
    value = os.fspath(path)
    if os.name != "nt" or value.startswith("\\\\?\\"):
        return Path(value)
    value = os.path.abspath(value)
    if value.startswith("\\\\"):
        return Path("\\\\?\\UNC\\" + value[2:])
    return Path("\\\\?\\" + value)

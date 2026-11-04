"""Translate local Bash operands without changing native Core coordinates.

On Windows the selected shell is MSYS, whose /c, /tmp and other mounts are
not Windows root-relative paths. Ask that runtime's cygpath instead of guessing
its mount table. This is path observation only; callers still check authority.
"""
from __future__ import annotations

import os
import posixpath
import subprocess
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Iterator

from shared.lib.shell import bash_shell, msys_runtime_bin


_WINDOWS = os.name == "nt"
_RESOLUTION: ContextVar[tuple[bool, dict[str, str | None]] | None] = ContextVar(
    "experiment_bash_path_resolution", default=None,
)


@contextmanager
def shell_path_resolution(*, use_msys: bool | None = None) -> Iterator[None]:
    """Reuse observations within one analysis, never across runtime changes.

    Nested analyzers share the current cache. Remote payload analysis explicitly
    disables local MSYS conversion; native tool parameters never call this helper.
    """
    current = _RESOLUTION.get()
    if current is not None and (use_msys is None or use_msys == current[0]):
        yield
        return
    token = _RESOLUTION.set((True if use_msys is None else use_msys, {}))
    try:
        yield
    finally:
        _RESOLUTION.reset(token)


def _msys_absolute_path(path: str) -> str | None:
    # A newline-delimited input avoids MSYS command-line re-interpretation of
    # quotes/backslashes in filenames. These exceptional filenames are unknown.
    if any(char in path for char in ("\0", "\r", "\n")):
        return None
    try:
        runtime = msys_runtime_bin(bash_shell())
        if runtime is None:
            return None
        executable = Path(runtime) / "cygpath.exe"
        if not executable.is_file():
            return None
        result = subprocess.run(
            [str(executable), "-a", "-w", "-C", "UTF8", "-f", "-", "--"],
            input=path + "\n", capture_output=True, text=True,
            encoding="utf-8", timeout=2, shell=False,
            creationflags=subprocess.CREATE_NO_WINDOW,
            env={**os.environ, "LC_ALL": "C.UTF-8"},
        )
        if result.returncode != 0:
            return None
        converted = result.stdout.removesuffix("\n").removesuffix("\r")
        if (not converted or any(char in converted for char in ("\0", "\r", "\n"))
                or not os.path.isabs(converted)):
            return None
        return os.path.normpath(converted)
    except (OSError, UnicodeError, ValueError, subprocess.TimeoutExpired):
        return None


def normalize_bash_path(path: str, base: str | None = None) -> str | None:
    """Resolve a literal shell operand, returning None when MSYS is unavailable.

    Expansion/unquoting belongs to the caller. Native drive paths, UNC paths and
    relative operands keep their existing coordinates; only a Windows shell's
    single-slash absolute operands require runtime mount translation.
    """
    current = _RESOLUTION.get()
    use_msys = current is None or current[0]
    if _WINDOWS and use_msys and path.startswith("/") and not path.startswith("//"):
        # Core gives each payload its own TMP/TEMP, which changes MSYS /tmp.
        # This analyzer does not own that environment; the parent's Temp is not
        # evidence about the payload. An explicit native path remains usable.
        logical = posixpath.normpath(path.replace("\\", "/")).casefold()
        if logical == "/tmp" or logical.startswith("/tmp/"):
            return None
        cache = current[1] if current is not None else None
        if cache is not None and path in cache:
            return cache[path]
        converted = _msys_absolute_path(path)
        if cache is not None:
            cache[path] = converted
        return converted
    if not os.path.isabs(path):
        path = os.path.join(base or os.getcwd(), path)
    return os.path.abspath(path)

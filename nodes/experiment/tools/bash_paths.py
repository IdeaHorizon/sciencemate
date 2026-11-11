"""Translate shell operands using the selected execution target's coordinates.

On a local Windows target use the selected MSYS runtime's cygpath. On a WSL
target use Core's measured Linux-to-Windows mapping. Never borrow one target's
mount table for another. This observes paths only; callers still check authority.
"""
from __future__ import annotations

import os
import posixpath
import subprocess
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Iterator

from core import execution_target
from shared.lib.shell import bash_shell, msys_runtime_bin

if TYPE_CHECKING:
    from core.isolation import TargetPaths

_WINDOWS = os.name == "nt"


@dataclass
class _ShellPaths:
    # Explicit scheduler payloads have their own execution host and mount table.
    target: execution_target.ExecutionTarget | None
    wsl: TargetPaths | None = None
    mapped: dict[str, str | None] = field(default_factory=dict)


_RESOLUTION: ContextVar[_ShellPaths | None] = ContextVar(
    "experiment_bash_path_resolution", default=None,
)


def bash_execution_target() -> execution_target.ExecutionTarget:
    context = _RESOLUTION.get()
    if context is not None and context.target is not None:
        return context.target
    return execution_target.current()


@contextmanager
def shell_path_resolution(*, remote: bool | None = None) -> Iterator[None]:
    """Reuse observations within one analysis, never across runtime changes.

    Nested analyzers inherit the context unless they change the execution host.
    Native tool parameters are already in Core's coordinates.
    """
    current = _RESOLUTION.get()
    if current is not None and (remote is None or remote == (current.target is None)):
        yield
        return
    target = None if remote else execution_target.current()
    context = _ShellPaths(target)
    if _WINDOWS and target is not None and target.kind == "wsl":
        from core.isolation import target_paths

        # Core supplies the mapping. Only spawn_and_wait launches a payload.
        context.wsl = target_paths()
    token = _RESOLUTION.set(context)
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


def uses_wsl_paths() -> bool:
    context = _RESOLUTION.get()
    target = context.target if context is not None else execution_target.current()
    return target is not None and target.kind == "wsl"


def is_bash_absolute(path: str) -> bool:
    return posixpath.isabs(path) if uses_wsl_paths() else os.path.isabs(path)


def _wsl_cwd(path: str) -> str | None:
    """Translate a Core cwd only when a WSL operand needs it."""
    current = _RESOLUTION.get()
    if current is None:
        with shell_path_resolution():
            return _wsl_cwd(path)
    if current.wsl is None:
        return None
    try:
        return current.wsl.to_linux(os.path.abspath(path))
    except (OSError, ValueError, RuntimeError):
        return None


def resolve_bash_cwd(path: str, base: str | None) -> str | None:
    """Resolve a literal cd operand without leaving the shell's coordinates."""
    if not uses_wsl_paths():
        return normalize_bash_path(path, base) if base or os.path.isabs(path) else None
    if posixpath.isabs(path):
        return posixpath.normpath("/" + path.lstrip("/"))
    if base is None:
        return None
    if not posixpath.isabs(base):
        base = _wsl_cwd(base)
    return posixpath.normpath(posixpath.join(base, path)) if base else None


def normalize_bash_path(path: str, base: str | None = None) -> str | None:
    """Project a literal shell operand into the coordinates used for scope checks."""
    current = _RESOLUTION.get()
    if current is None:
        with shell_path_resolution():
            return normalize_bash_path(path, base)
    if current.target is not None and current.target.kind not in {"local", "wsl"}:
        return None
    if uses_wsl_paths():
        # Windows cannot represent Linux filename components containing these
        # characters. In particular, a backslash in Linux is not a separator.
        if current.wsl is None or any(char in path for char in '\0\r\n\\:'):
            return None
        if not base and not posixpath.isabs(path):
            base = os.getcwd()
        guest_path = resolve_bash_cwd(path, base)
        if guest_path is None or any(char in guest_path for char in '\0\r\n\\:'):
            return None
        if guest_path not in current.mapped:
            try:
                mapped = current.wsl.to_windows(guest_path)
            except (OSError, ValueError, RuntimeError):
                mapped = None
            # Path-role declarations expand host variables. A guest literal
            # that would change in that layer cannot identify a host target.
            if mapped and (not os.path.isabs(mapped)
                           or not os.path.splitdrive(mapped)[0]
                           or os.path.expandvars(mapped) != mapped):
                mapped = None
            current.mapped[guest_path] = mapped
        return current.mapped[guest_path]
    if (_WINDOWS and current.target is not None and current.target.is_local
            and path.startswith("/") and not path.startswith("//")):
        # Core gives each payload its own TMP/TEMP, which changes MSYS /tmp.
        # This analyzer does not own that environment; the parent's Temp is not
        # evidence about the payload. An explicit native path remains usable.
        logical = posixpath.normpath(path.replace("\\", "/")).casefold()
        if logical == "/tmp" or logical.startswith("/tmp/"):
            return None
        if path not in current.mapped:
            current.mapped[path] = _msys_absolute_path(path)
        return current.mapped[path]
    if not os.path.isabs(path):
        path = os.path.join(base or os.getcwd(), path)
    return os.path.abspath(path)

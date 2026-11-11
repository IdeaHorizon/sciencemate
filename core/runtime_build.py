"""Process-scoped source facts; observations only, never lifecycle authority.

The digest describes Python and supervisor source present when this process
first reports its build. Running payload directories must remain immutable.
Release signatures and deployment admission remain owned by the installer.
"""

from __future__ import annotations

import hashlib
import inspect
from functools import lru_cache
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]


@lru_cache(maxsize=1)
def _source_snapshot() -> tuple[str | None, tuple[tuple[str, str], ...]]:
    paths = set(_ROOT.glob("*.py"))
    for directory in ("core", "nodes", "shared", "platform/backend/app"):
        for path in (_ROOT / directory).rglob("*"):
            if path.suffix in {".py", ".in"} and "tests" not in path.relative_to(_ROOT).parts:
                paths.add(path)
    components = []
    digest = hashlib.sha256()
    try:
        for path in sorted(paths):
            name = path.relative_to(_ROOT).as_posix()
            sha = hashlib.sha256(path.read_bytes()).hexdigest()
            digest.update(f"{name}\0{sha}\n".encode())
            if name in {
                "core/job_wait.py",
                "core/remote_resources.py",
                "core/_ssh_agent.py.in",
                "nodes/experiment/tools/managed_ssh.py",
            }:
                components.append((name, sha))
    except OSError:
        return None, ()
    return digest.hexdigest(), tuple(components)


def facts(*, components: bool = False) -> dict:
    from core import remote_resources

    sha, entries = _source_snapshot()
    handler = remote_resources._handler
    result = {
        "sourceSha256": sha,
        "resourceAdapter": (
            f"{getattr(handler, '__module__', type(handler).__module__)}."
            f"{getattr(handler, '__qualname__', type(handler).__qualname__)}"
            if handler is not None
            else None
        ),
        "resourceAdapterSha256": _adapter_source(handler) if handler is not None else None,
    }
    if components:
        result["components"] = dict(entries)
    return result


def _adapter_source(handler) -> str | None:
    # The bridge can be installed outside the payload (the App's own package).
    try:
        source = inspect.getsourcefile(handler)
        return hashlib.sha256(Path(source).read_bytes()).hexdigest() if source else None
    except (OSError, TypeError):
        return None

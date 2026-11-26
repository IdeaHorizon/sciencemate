"""Read local and personal-server research context without moving projects.

Remote I/O belongs to the collector. Card delivery only reads the last scoped
snapshot, so an unavailable server cannot hold up local recommendations.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import quote
from weakref import WeakValueDictionary

from sqlalchemy import select

from app import assembly
from app.config import settings
from app.models.project import Project, ProjectStatus
from app.services import remote_servers
from app.services.other_homes import ThereIsNoSuchHomeError

logger = logging.getLogger(__name__)
REFRESH_SECONDS = 300
SERVER_TIMEOUT_SECONDS = 12
INPUT_LIMIT = 40
INPUT_CHARS = 4000
_locks = WeakValueDictionary()


@dataclass(frozen=True)
class ProjectContext:
    id: str
    name: str
    research_domain: str | None
    description: str | None
    home: str = ""
    user_inputs: tuple[str, ...] = ()
    last_active: datetime | None = None


def _servers() -> list[dict]:
    # Same boundary as /remote-servers: a personal desktop, never a shared
    # organisation backend or a remote runtime recursively following more hosts.
    if not assembly.can_choose_project_folders() or settings.remote_runtime_token:
        return []
    return [row for row in remote_servers.all_servers() if row.get("enabled", True)]


def _path(user_id: object, server_id: str) -> Path:
    key = hashlib.sha256(f"{user_id}\0{server_id}".encode()).hexdigest()
    return Path(settings.platform_data_root).expanduser() / "feed-project-contexts" / f"{key}.json"


def _read(user_id: object, server_id: str) -> dict:
    try:
        value = json.loads(_path(user_id, server_id).read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _context(row: dict, *, home: str = "") -> ProjectContext | None:
    from app.services.feed.curation import _coerce_datetime

    if not row.get("id") or row.get("status") != "active" or row.get("mine") is False:
        return None
    inputs = row.get("user_inputs")
    return ProjectContext(
        id=str(row["id"]), name=str(row.get("name") or "")[:300],
        research_domain=str(row.get("research_domain") or "")[:200],
        description=str(row.get("description") or "")[:12000], home=home,
        user_inputs=tuple(str(value)[:INPUT_CHARS] for value in inputs[-INPUT_LIMIT:]
                          if isinstance(value, str) and value.strip()) if isinstance(inputs, list) else (),
        last_active=_coerce_datetime(row.get("last_active")),
    )


def cached_remote_projects(user) -> list[ProjectContext]:
    result = []
    for server in _servers():
        snapshot = _read(user.id, server["id"])
        # Before the first collection, the sidebar's existing catalogue already
        # tells us these projects exist. It contains no conversation credentials.
        rows = snapshot.get("projects")
        if not isinstance(rows, list):
            rows = remote_servers.cached_projects(server["id"])
        for row in rows:
            if isinstance(row, dict) and (project := _context(row, home=server["id"])):
                result.append(project)
    return result


def remote_revision(user) -> str:
    rows = [asdict(project) for project in cached_remote_projects(user)]
    return hashlib.sha256(json.dumps(rows, sort_keys=True, default=str).encode()).hexdigest()


def collection_status(user) -> str:
    """Reading the cache never turns an unreachable server into an empty one."""
    snapshots = [_read(user.id, server["id"]) for server in _servers()]
    if any(snapshot.get("unavailable") for snapshot in snapshots):
        return "unavailable"
    if any(not snapshot.get("checked_at") for snapshot in snapshots):
        return "pending"
    return "current"


async def local_projects(db, user) -> list[ProjectContext]:
    from app.services.feed.curation import project_user_inputs, projects_last_active

    rows = (await db.scalars(select(Project).where(
        Project.owner_id == user.id, Project.status == ProjectStatus.ACTIVE,
    ).order_by(Project.updated_at.desc()))).all()
    active = await projects_last_active(db, [str(row.id) for row in rows])
    return [ProjectContext(
        id=str(row.id), name=row.name, research_domain=row.research_domain,
        description=row.description,
        user_inputs=tuple(value[:INPUT_CHARS] for value in await project_user_inputs(
            db, str(row.id), limit=INPUT_LIMIT,
        )), last_active=active.get(str(row.id)),
    ) for row in rows]


async def user_projects(db, user) -> list[ProjectContext]:
    from app.services.project_homes import the_same_project

    # A project can be seen through two connection aliases. Keep it once, with
    # a real local project taking precedence over an old remote snapshot.
    projects = await local_projects(db, user)
    seen = {the_same_project(p.id) for p in projects}
    for project in cached_remote_projects(user):
        key = the_same_project(project.id)
        if key not in seen:
            projects.append(project)
            seen.add(key)
    return projects


async def _legacy_contexts(server_id: str) -> list[dict]:
    """Older runtimes still expose project/session metadata and initial user inputs."""
    projects = await remote_servers.ask(server_id, "GET", "/api/v1/projects/")
    if not isinstance(projects, list):
        raise ValueError("Invalid remote project list")
    semaphore = asyncio.Semaphore(3)

    async def enrich(row):
        if not _context(row):
            return row
        async with semaphore:
            path = "/api/v1/projects/" + quote(str(row["id"]), safe="") + "/sessions"
            try:
                sessions = await remote_servers.ask(server_id, "GET", path)
                inputs = []
                # Compatibility only: the new endpoint returns recent user
                # inputs directly, without fetching assistant messages or tools.
                for session in sessions[:2]:
                    sid = quote(str(session["id"]), safe="")
                    messages = await remote_servers.ask(
                        server_id, "GET", path + f"/{sid}/messages", params={"limit": 100},
                    )
                    inputs.extend(str(item.get("content") or "")[:INPUT_CHARS]
                                  for item in messages.get("items", []) if item.get("role") == "user")
                return {**row, "user_inputs": inputs[-INPUT_LIMIT:]}
            except (ThereIsNoSuchHomeError, ValueError, TypeError, KeyError):
                return row

    return await asyncio.gather(*(enrich(row) for row in projects if isinstance(row, dict)))


async def refresh_remote_projects(user, *, force: bool = False) -> None:
    servers = _servers()
    if not servers:
        return
    key = str(Path(settings.platform_data_root).resolve()), str(user.id)
    lock = _locks.setdefault(key, asyncio.Lock())
    async with lock:
        async def refresh(server):
            sid = server["id"]
            previous = _read(user.id, sid)
            if not force and time.time() - previous.get("checked_at", 0) < REFRESH_SECONDS:
                return
            try:
                async with asyncio.timeout(SERVER_TIMEOUT_SECONDS):
                    try:
                        response = await remote_servers.ask(sid, "GET", "/api/v1/feed/project-contexts")
                        rows = response["projects"]
                    except ThereIsNoSuchHomeError as exc:
                        if exc.status not in {404, 405}:
                            raise
                        rows = await _legacy_contexts(sid)
                if not isinstance(rows, list):
                    raise ValueError("Invalid remote project context")
                cleaned = [
                    {**asdict(project), "status": "active"}
                    for row in rows if isinstance(row, dict) and (project := _context(row))
                ]
                remote_servers._write(_path(user.id, sid), json.dumps(
                    {"checked_at": time.time(), "projects": cleaned, "unavailable": False}, default=str,
                ).encode())
            except (ThereIsNoSuchHomeError, TimeoutError, OSError, ValueError, TypeError, KeyError) as exc:
                retry = {**previous, "checked_at": time.time(), "unavailable": True}
                if isinstance(exc, ThereIsNoSuchHomeError) and exc.status in {401, 403}:
                    # Lost access is not an offline cache hit.
                    retry["projects"] = []
                    retry["unavailable"] = False
                remote_servers._write(_path(user.id, sid), json.dumps(retry).encode())
                logger.info("Remote feed context unavailable for %s: %s", sid, type(exc).__name__)
        results = await asyncio.gather(*(refresh(server) for server in servers), return_exceptions=True)
        for server, result in zip(servers, results, strict=True):
            if isinstance(result, Exception):
                logger.warning("Could not refresh feed context for %s: %s", server["id"], type(result).__name__)

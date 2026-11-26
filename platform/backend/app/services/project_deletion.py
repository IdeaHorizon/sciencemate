"""Project deletion: persist intent, stop writers, remove owned data, then identity."""

from __future__ import annotations

import asyncio
import json
import shutil
import stat
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID
from weakref import WeakValueDictionary

from sqlalchemy import delete, or_, select, tuple_, update

from app.config import data_root, the_projects_home
from app.database import Base
from app.models.execution import SessionProjection
from app.models.project import Project
from app.services.say import SaidToAPerson

_locks = WeakValueDictionary()


class OpenProjectJobs(SaidToAPerson):
    """A worker exit is insufficient evidence that its external jobs settled."""


def _remove_tree(path: Path) -> None:
    if path.is_symlink():
        raise ValueError(f"Refusing to recursively delete a symbolic project root: {path}")
    if not path.exists():
        return

    def writable(function, filename, error):
        if not isinstance(error[1], PermissionError):
            raise error[1]
        target = Path(filename)
        if target.is_symlink():
            target.unlink()
        else:
            target.chmod(target.stat().st_mode | stat.S_IWUSR | stat.S_IXUSR)
            function(filename)

    shutil.rmtree(path, onerror=writable)


def project_sessions(project_id: str, session_ids: list[str]) -> list[str]:
    from app.services.project_repository import get_project_repository
    from app.services.spawn_ledger import ledger_dir

    repository = get_project_repository()
    repository._safe_id(project_id, "project id")
    result = set(session_ids)
    worktrees = repository._project_worktrees(project_id)
    if worktrees.is_dir() and not worktrees.is_symlink():
        result.update(path.name for path in worktrees.iterdir() if path.is_dir())
    for entry in ledger_dir().glob("*.json"):
        row = json.loads(entry.read_text(encoding="utf-8"))
        if row.get("project_id") == project_id:
            result.add(row["session_id"])
    return sorted(repository._safe_id(value, "session id") for value in result)


def remove_project_files(project_id: str, session_ids: list[str]) -> None:
    from app.services.harness_contract import worker_addressing
    from app.services.project_repository import get_project_repository
    from app.services.spawn_ledger import ledger_dir

    repository = get_project_repository()
    project_id = repository._safe_id(project_id, "project id")
    with repository._project_lock(project_id):
        root = repository.project_path(project_id)
        worktrees = repository._project_worktrees(project_id)
        # Location records are the installation's authority for chosen roots.
        # Never allow one project removal to contain another reserved root.
        protected = {
            Path.home().resolve(),
            Path(root.anchor),
            repository.repository_root,
            repository.worktree_root,
        }
        other_roots = [
            path.resolve()
            for key, path in repository.locations.custom_roots().items()
            if key != project_id
        ]
        if any(
            root.resolve() == path or root.resolve() in path.parents for path in protected
        ) or any(path == root.resolve() or root.resolve() in path.parents for path in other_roots):
            raise ValueError("Project root overlaps a shared or another project's directory")
        marker = root / "project.yaml"
        if marker.is_file():
            import yaml

            record = yaml.safe_load(marker.read_text(encoding="utf-8"))
            if not isinstance(record, dict) or str(record.get("project_id")) != project_id:
                raise ValueError("Project directory identity does not match the deletion target")
        # Remove worktrees before canonical Git, and preserve the location
        # record until all owned directories have been successfully removed.
        _remove_tree(worktrees)
        _remove_tree(root)
        _remove_tree(the_projects_home() / project_id)
        for session_id in session_ids:
            socket = worker_addressing().control_socket_path(
                data_root("sockets"), project_id, session_id
            )
            socket.unlink(missing_ok=True)
        for entry in ledger_dir().glob("*.json"):
            row = json.loads(entry.read_text(encoding="utf-8"))
            if row.get("project_id") == project_id:
                entry.unlink()
        repository.locations.unregister(project_id)
    # The OS lock is released before its path is unlinked (Windows included).
    (repository.repository_root / ".locks" / f"{project_id}.lock").unlink(missing_ok=True)


async def remove_project_rows(db, project_id: str) -> None:
    """Follow model ownership, including execution tables without a Project FK.

    SQLite installations may have FK enforcement disabled, so a claimed cascade
    is insufficient. Derive dependent rows from declared relationships instead
    of maintaining a second hand-written list of every project-owned table.
    """
    tables = list(Base.metadata.sorted_tables)
    scopes = {Project.__table__: Project.__table__.c.id == project_id}
    for table in tables:
        if "project_id" in table.c:
            scopes[table] = table.c.project_id == project_id

    def reference(constraint):
        parent = next(iter(constraint.elements)).column.table
        if parent not in scopes:
            return None
        columns = [element.parent for element in constraint.elements]
        target = select(*(element.column for element in constraint.elements)).where(scopes[parent])
        return (columns[0] if len(columns) == 1 else tuple_(*columns)).in_(target)

    while True:
        added = False
        for table in tables:
            if table in scopes:
                continue
            # A composite identity is one relationship, never an OR of its
            # individual columns. SET NULL denotes provenance, not ownership.
            links = [
                predicate
                for constraint in table.foreign_key_constraints
                if constraint.ondelete == "CASCADE"
                and (predicate := reference(constraint)) is not None
            ]
            if links:
                scopes[table] = or_(*links)
                added = True
        if not added:
            break
    for table in tables:
        for constraint in table.foreign_key_constraints:
            if (
                constraint.ondelete == "SET NULL"
                and (predicate := reference(constraint)) is not None
            ):
                await db.execute(
                    update(table)
                    .where(predicate)
                    .values({element.parent.name: None for element in constraint.elements})
                )
    for table in reversed(tables):
        if table in scopes and table is not Project.__table__:
            await db.execute(delete(table).where(scopes[table]))
    await db.execute(delete(Project).where(Project.id == project_id))


async def _remove_project_resources(db, project_id: str) -> None:
    from app.services import harness_sessions, local_execution

    sessions = list(
        (
            await db.scalars(
                select(SessionProjection.session_id).where(
                    SessionProjection.project_id == project_id
                )
            )
        ).all()
    )
    sessions = await asyncio.to_thread(project_sessions, project_id, sessions)
    await local_execution.stop_project_executions(project_id)
    await harness_sessions.harness_session_manager.terminate_project(project_id, sessions)
    # Reattachment may have registered a collector while stopping a worker.
    await local_execution.stop_project_executions(project_id)
    for session_id in sessions:
        await asyncio.to_thread(
            harness_sessions.reap_orphaned_session_worker, project_id, session_id
        )
        path = harness_sessions._session_lock_path(project_id, session_id)
        if path and path.is_file():
            row = json.loads(path.read_text(encoding="utf-8"))
            pid = row.get("pid")
            if isinstance(pid, int) and harness_sessions._process_is_a_runtime_worker(pid):
                raise RuntimeError(f"Worker {pid} is still alive; project data was retained")
    from app.services.harness_contract import jobs_module
    from app.services.project_homes import the_job_ledger_of

    jobs = await asyncio.to_thread(
        jobs_module().observe, the_job_ledger_of(project_id), only_open=True
    )
    unresolved = [job for job in jobs if job.scheduler_job_id or job.alive() is not False]
    if unresolved:
        identities = [
            f"{job.job_id} (scheduler={job.scheduler_job_id}, owner={job.by_user_id})"
            for job in unresolved
        ]
        raise OpenProjectJobs(
            "后台作业尚未结清，项目文件和记录已保留。请通过原执行方取消或核对终态后再删除："
            + ", ".join(identities),
            en="Background jobs remain unsettled; project files and records were retained. "
            "Cancel or reconcile them through their execution owner before deleting: "
            + ", ".join(identities),
        )
    await asyncio.to_thread(remove_project_files, project_id, sessions)
    await remove_project_rows(db, project_id)
    await db.commit()


async def delete_owned_project(db, project: Project) -> None:
    project_id = str(project.id)
    lock = _locks.setdefault(project_id, asyncio.Lock())
    async with lock:
        if project.deletion_requested_at is None:
            project.deletion_requested_at = datetime.now(UTC)
            await db.commit()
        try:
            await _remove_project_resources(db, project_id)
        except OpenProjectJobs:
            # No files have been removed. Release the deletion fence so the
            # owner can resume/reconcile the original jobs through normal tools.
            project.deletion_requested_at = None
            await db.commit()
            from app.services.harness_sessions import harness_session_manager

            harness_session_manager._retiring_projects.discard(project_id)
            raise


async def find_deletion_residue(db) -> list[dict]:
    """Read-only inventory of platform projects whose identity row is gone."""
    from app.services.project_repository import get_project_repository
    from app.services.spawn_ledger import ledger_dir

    repository = get_project_repository()
    existing = {str(value) for value in (await db.scalars(select(Project.id))).all()}
    candidates = set(repository.locations.custom_roots())
    for root in (repository.repository_root, repository.worktree_root, the_projects_home()):
        if root.is_dir():
            candidates.update(path.name for path in root.iterdir() if path.is_dir())
    for entry in ledger_dir().glob("*.json"):
        row = json.loads(entry.read_text(encoding="utf-8"))
        candidates.add(str(row.get("project_id") or ""))
    for table in Base.metadata.tables.values():
        if "project_id" in table.c:
            candidates.update(
                str(value)
                for value in (await db.scalars(select(table.c.project_id).distinct())).all()
                if value
            )
    result = []
    for value in sorted(candidates):
        try:
            project_id = str(UUID(value))
        except ValueError:
            continue  # CLI projects and shared installation directories are not platform projects.
        if project_id in existing:
            continue
        result.append(
            {
                "project_id": project_id,
                "repository_present": repository.project_path(project_id).exists(),
                "worktrees_present": repository._project_worktrees(project_id).exists(),
            }
        )
    return result


async def delete_residue(db, project_id: str) -> None:
    """Explicit personal-desktop maintenance; active projects are never eligible."""
    project_id = str(UUID(project_id))
    lock = _locks.setdefault(project_id, asyncio.Lock())
    async with lock:
        if await db.get(Project, project_id) is not None:
            raise ValueError("An existing project must be deleted through its own lifecycle")
        if project_id not in {row["project_id"] for row in await find_deletion_residue(db)}:
            return  # Retry after complete cleanup is idempotent.
        await _remove_project_resources(db, project_id)


async def resume_pending_deletions(factory) -> list[str]:
    """Resume only explicit durable deletion requests after a backend restart."""
    import logging

    completed = []
    async with factory() as db:
        ids = list(
            (
                await db.scalars(
                    select(Project.id).where(Project.deletion_requested_at.is_not(None))
                )
            ).all()
        )
    for project_id in ids:
        async with factory() as db:
            project = await db.get(Project, project_id)
            if project is None:
                continue
            try:
                await delete_owned_project(db, project)
                completed.append(str(project_id))
            except Exception:
                await db.rollback()
                logging.getLogger(__name__).exception(
                    "Project deletion remains pending for %s", project_id
                )
    return completed

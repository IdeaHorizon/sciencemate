"""Deletion owns the complete lifecycle, including retries and unrelated projects."""
# ruff: noqa: F811 -- pytest fixture imported for discovery and requested by name.

import asyncio
import sys
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from app.models.execution import Command, Run, RunAttempt, SessionMessage, SessionProjection
from app.models.project import Project, ProjectConfig, ProjectMembership
from app.services import harness_sessions, local_execution, project_deletion
from app.services.project_repository import get_project_repository
from tests.test_local_runtime_api import _headers, _token, runtime_client  # noqa: F401


async def create(client, name, *, folder=None):
    headers = _headers(await _token(client, "researcher@atrium.local"))
    data = {"name": name}
    if folder:
        data["root_folder"] = str(folder)
    response = await client.post("/api/v1/projects/", headers=headers, json=data)
    assert response.status_code == 201, response.text
    return headers, response.json()["id"]


@pytest.mark.asyncio
async def test_deletion_removes_project_directory(runtime_client):  # noqa: F811
    client, _ = runtime_client
    headers, project_id = await create(client, "Delete owned project data")
    root = get_project_repository().project_path(project_id)
    assert root.is_dir()
    response = await client.delete(f"/api/v1/projects/{project_id}", headers=headers)
    assert response.status_code == 204, response.text
    assert not root.exists(), "204 must mean project data was removed"


@pytest.mark.asyncio
async def test_pending_deletion_keeps_lifecycle_authorization_first(runtime_client):
    from datetime import UTC, datetime

    from fastapi import HTTPException

    from app.models.user import User
    from app.services.project_lifecycle import _the_project

    client, factory = runtime_client
    _, project_id = await create(client, "Private deletion")
    async with factory() as db:
        project = await db.get(Project, project_id)
        project.deletion_requested_at = datetime.now(UTC)
        await db.commit()
        stranger = User(
            id=str(uuid4()),
            email="stranger@example.test",
            display_name="Stranger",
            hashed_password="x",
            institution_id="elsewhere",
        )
        with pytest.raises(HTTPException) as refused:
            await _the_project(db, stranger, project_id)
        assert refused.value.status_code == 403
        owner = await db.get(User, project.owner_id)
        with pytest.raises(HTTPException) as pending:
            await _the_project(db, owner, project_id)
        assert pending.value.status_code == 409


@pytest.mark.asyncio
@pytest.mark.parametrize("custom", [False, True])
async def test_workers_and_retained_writers_stop_before_rows_and_files_are_removed(
    runtime_client,
    tmp_path,
    monkeypatch,
    custom,  # noqa: F811
):
    from app.config import settings

    monkeypatch.setattr(settings, "profile", "personal")
    client, factory = runtime_client
    headers, project_id = await create(
        client, "Deleting", folder=tmp_path / "chosen" if custom else None
    )
    _, other_id = await create(client, "Keep this project")
    repository = get_project_repository()
    other_root = repository.project_path(other_id)
    response = await client.post(
        f"/api/v1/projects/{project_id}/sessions", headers=headers, json={"title": "Work"}
    )
    assert response.status_code == 201, response.text
    session_id = response.json()["id"]
    root = repository.project_path(project_id)
    session_root = repository.session_path(project_id, session_id)
    async with factory() as db:
        scope = {
            "tenant_id": settings.runtime_tenant_id,
            "workspace_id": "w",
            "project_id": project_id,
            "session_id": session_id,
        }
        run = Run(id="run-deleting", **scope, status="waiting_permission")
        db.add(run)
        await db.flush()
        db.add(RunAttempt(run_id=run.id, **scope, attempt_no=1))
        db.add(SessionMessage(session_id=session_id, sequence=1, role="user", content="Task"))
        db.add(Command(**scope, actor_user_id="user", kind="turn", idempotency_key=uuid4().hex))
        await db.commit()

    process = await asyncio.create_subprocess_exec(
        sys.executable, "-c", "import time; time.sleep(60)"
    )

    class Worker:
        @property
        def alive(self):
            return process.returncode is None

    worker = Worker()

    async def terminate():
        process.terminate()
        await process.wait()

    session = SimpleNamespace(
        project_id=project_id, session_id=session_id, worker=worker, terminate=terminate
    )
    manager = harness_sessions.HarnessSessionManager()
    manager._sessions[manager._key(project_id, session_id)] = session
    monkeypatch.setattr(harness_sessions, "harness_session_manager", manager)
    finalized = []

    async def writer():
        try:
            await asyncio.sleep(60)
        finally:
            finalized.append("stopped")

    task = asyncio.create_task(writer())
    local_execution.retain_detached_execution(task, project_id=project_id)
    await asyncio.sleep(0)
    try:
        result = await client.delete(f"/api/v1/projects/{project_id}", headers=headers)
        assert result.status_code == 204, result.text
        assert process.returncode is not None and finalized == ["stopped"]
        assert not root.exists() and not session_root.exists()
        assert other_root.is_dir()
        async with factory() as db:
            for model in (
                Run,
                RunAttempt,
                SessionProjection,
                Command,
                ProjectConfig,
                ProjectMembership,
            ):
                assert (
                    await db.scalar(
                        select(func.count())
                        .select_from(model)
                        .where(model.project_id == project_id)
                    )
                    == 0
                )
            assert (
                await db.scalar(
                    select(func.count())
                    .select_from(SessionMessage)
                    .where(SessionMessage.session_id == session_id)
                )
                == 0
            )
            assert await db.get(Project, other_id) is not None
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


@pytest.mark.asyncio
async def test_cleanup_failure_retains_identity_and_blocks_new_work_until_retry(
    runtime_client, monkeypatch
):  # noqa: F811
    client, factory = runtime_client
    headers, project_id = await create(client, "Retry deletion")
    root = get_project_repository().project_path(project_id)
    remove = project_deletion._remove_tree

    def denied(path):
        if path == root:
            raise PermissionError("injected cleanup failure")
        remove(path)

    monkeypatch.setattr(project_deletion, "_remove_tree", denied)
    first = await client.delete(f"/api/v1/projects/{project_id}", headers=headers)
    assert first.status_code == 409, first.text
    async with factory() as db:
        project = await db.get(Project, project_id)
        assert project is not None and project.deletion_requested_at is not None
    assert (
        await client.post(
            f"/api/v1/projects/{project_id}/sessions",
            headers=headers,
            json={"title": "Must not restart"},
        )
    ).status_code == 403
    assert (
        await client.post(f"/api/v1/projects/{project_id}/restore", headers=headers)
    ).status_code == 409
    monkeypatch.setattr(project_deletion, "_remove_tree", remove)
    retry = await client.delete(f"/api/v1/projects/{project_id}", headers=headers)
    assert retry.status_code == 204, retry.text
    assert not root.exists()


@pytest.mark.asyncio
async def test_explicit_deletion_intent_survives_a_backend_restart(runtime_client, monkeypatch):  # noqa: F811
    from datetime import UTC, datetime

    client, factory = runtime_client
    _, project_id = await create(client, "Pending deletion")
    _, other_id = await create(client, "Unaffected")
    async with factory() as db:
        project = await db.get(Project, project_id)
        project.deletion_requested_at = datetime.now(UTC)
        await db.commit()
    monkeypatch.setattr(
        harness_sessions, "harness_session_manager", harness_sessions.HarnessSessionManager()
    )
    assert await project_deletion.resume_pending_deletions(factory) == [project_id]
    async with factory() as db:
        assert await db.get(Project, project_id) is None
        assert await db.get(Project, other_id) is not None


@pytest.mark.asyncio
async def test_historical_residue_scan_and_cleanup_preserve_existing_projects(
    runtime_client, monkeypatch
):  # noqa: F811
    from sqlalchemy import delete

    from app.config import settings

    monkeypatch.setattr(settings, "profile", "personal")
    client, factory = runtime_client
    headers, removed_id = await create(client, "Legacy orphan")
    _, existing_id = await create(client, "Keep")
    repository = get_project_repository()
    removed_root = repository.project_path(removed_id)
    async with factory() as db:
        # Exact legacy defect: the identity row is gone, its data is untouched.
        await db.execute(delete(Project).where(Project.id == removed_id))
        await db.commit()
    response = await client.get("/api/v1/projects/deletion-residue", headers=headers)
    assert response.status_code == 200, response.text
    ids = {row["project_id"] for row in response.json()["items"]}
    assert removed_id in ids and existing_id not in ids
    refused = await client.post(
        f"/api/v1/projects/deletion-residue/{existing_id}/cleanup", headers=headers
    )
    assert refused.status_code == 409
    for _ in range(2):
        cleaned = await client.post(
            f"/api/v1/projects/deletion-residue/{removed_id}/cleanup", headers=headers
        )
        assert cleaned.status_code == 204, cleaned.text
    assert not removed_root.exists()
    assert repository.project_path(existing_id).is_dir()


@pytest.mark.asyncio
async def test_unsettled_external_job_preserves_data_and_releases_fence(
    runtime_client, monkeypatch
):  # noqa: F811
    client, factory = runtime_client
    headers, project_id = await create(client, "External job")
    root = get_project_repository().project_path(project_id)
    from app.services.harness_contract import jobs_module

    jobs = jobs_module()
    pending = [SimpleNamespace(job_id="job-owned", scheduler_job_id="123", by_user_id="owner")]
    monkeypatch.setattr(jobs, "observe", lambda *a, **kw: pending)
    response = await client.delete(f"/api/v1/projects/{project_id}", headers=headers)
    assert response.status_code == 409, response.text
    assert "job-owned" in response.json()["detail"]["message"]
    assert root.is_dir()
    async with factory() as db:
        project = await db.get(Project, project_id)
        assert project is not None and project.deletion_requested_at is None
    assert project_id not in harness_sessions.harness_session_manager._retiring_projects
    # The original owner can reconcile the external job; a later delete works.
    pending.clear()
    response = await client.delete(f"/api/v1/projects/{project_id}", headers=headers)
    assert response.status_code == 204, response.text
    assert not root.exists()

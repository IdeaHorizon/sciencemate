"""启动对账回收孤儿：一条回收不了，其余照标照收（RFC_WORKER_START_IS_NOT_A_GATE §3.5，窄单 D）。

lujy 10-15 现场：旧会话的 run 目录 `stat` 拒绝访问，`reap_orphaned_session_worker → _session_lock_path`
抛 PermissionError，整个 `mark_orphaned_harness_runs` abort —— 六次启动六次
「Could not reconcile orphaned Harness Runs at startup」，别的孤儿一个都没标。
"""
from __future__ import annotations

# ruff: noqa: F811  （`runtime_client` 是借来的 pytest fixture，参数名遮住导入名是它的正常长相）
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.config import settings
from app.models.execution import Run, RunAttempt, SessionProjection
from app.models.model_backend import ModelBackendConfig
from app.models.project import Project
from app.models.user import User
from app.services import harness_sessions as hs
from tests.test_local_runtime_api import GROUP, runtime_client  # noqa: F401


async def _orphan(db, project, user, backend, *, run_id: str) -> Run:
    session = SessionProjection(
        tenant_id=settings.runtime_tenant_id, workspace_id=GROUP, project_id=str(project.id),
        session_id=uuid4().hex, initiating_user_id=user.id, title=run_id,
        git_base_commit_sha="0" * 40, created_by_user_id=user.id, model_backend_id=backend.id,
        policy_snapshot_id="policy-snapshot-1", research_settings_snapshot_id="research-snapshot-1",
        research_settings_snapshot={"snapshot_id": "research-snapshot-1"},
        knowledge_read_watermark={"claim_sequence": 1},
    )
    run = Run(
        id=run_id, tenant_id=settings.runtime_tenant_id, workspace_id=GROUP,
        project_id=str(project.id), session_id=session.session_id, status="running",
        summary={"executionKernel": "formal_harness"},
    )
    attempt = RunAttempt(
        tenant_id=settings.runtime_tenant_id, workspace_id=GROUP, project_id=str(project.id),
        session_id=session.session_id, run_id=run.id, attempt_no=1, status="running",
    )
    db.add_all([session, run, attempt])
    await db.flush()
    return run


@pytest.mark.asyncio
async def test_one_unreadable_run_dir_is_recorded_and_the_rest_are_still_reaped(runtime_client, monkeypatch) -> None:
    client, factory = runtime_client
    async with factory() as db:
        project = await db.scalar(select(Project).where(Project.name == "Researcher Project"))
        user = await db.scalar(select(User).where(User.email == "researcher@atrium.local"))
        backend = await db.scalar(select(ModelBackendConfig).where(
            ModelBackendConfig.scope_kind == "institution", ModelBackendConfig.provider == "demo"))
        broken = await _orphan(db, project, user, backend, run_id="run_sweep_broken")
        healthy = await _orphan(db, project, user, backend, run_id="run_sweep_healthy")
        await db.commit()

        async def no_adoption(*args, **kwargs):
            return False

        monkeypatch.setattr(hs.harness_session_manager, "adopt_live_worker", no_adoption)

        def reap(project_id: str, session_id: str):
            if session_id == broken.session_id:
                raise PermissionError(5, "拒绝访问。", str(hs.settings.project_worktree_root))
            return "pid 4242 terminated"

        monkeypatch.setattr(hs, "reap_orphaned_session_worker", reap)

        changed = await hs.mark_orphaned_harness_runs(db, reap_workers=True)
        await db.commit()
        await db.refresh(broken)
        await db.refresh(healthy)

    assert changed == 2, "一条读不到就 abort —— 其余孤儿没标"
    assert broken.summary["staleReapError"].startswith("PermissionError:")
    assert "拒绝访问" in broken.summary["staleReapError"]
    assert "staleReapedWorker" not in broken.summary
    assert healthy.summary["staleReapedWorker"] == "pid 4242 terminated"
    assert "staleReapError" not in healthy.summary

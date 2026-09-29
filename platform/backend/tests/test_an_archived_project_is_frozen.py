"""归档 = 冻结：只读、不再开会话、后台不再替它干活；什么都不丢，能恢复（`services/project_lifecycle`）。

从前项目状态有四种，没有任何地方因为它们做了不一样的事 —— 一个只改标签的按钮是假的。判据落在
「归档之后，什么做不了了」上，不落在状态字段写了什么上。
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import select

from app.models.execution import Run, RunStatus
from app.models.project import Project, ProjectStatus
from tests.organisation_profile import an_organisation_server  # noqa: F401
from tests.test_local_runtime_api import _headers, _token, runtime_client  # noqa: F401

ADMIN = "institution.admin@atrium.local"
LEAD = "researcher@atrium.local"


async def _as(client, email: str) -> dict:
    return _headers(await _token(client, email))


async def _project_id(factory, name: str) -> str:
    async with factory() as db:
        return str((await db.scalar(select(Project).where(Project.name == name))).id)


@pytest.mark.asyncio
async def test_an_archived_project_can_only_be_looked_at(an_organisation_server) -> None:  # noqa: F811
    client, factory = an_organisation_server
    pid = await _project_id(factory, "Researcher Project")
    me = await _as(client, LEAD)

    archived = await client.post(f"/api/v1/projects/{pid}/archive", headers=me)
    opened = await client.get(f"/api/v1/projects/{pid}", headers=me)
    new_session = await client.post(f"/api/v1/projects/{pid}/sessions", headers=me, json={"title": "再开一个"})
    renamed = await client.patch(f"/api/v1/projects/{pid}", headers=me, json={"name": "改个名"})

    assert archived.status_code == 200 and archived.json()["status"] == "archived", archived.text
    assert opened.json()["capabilities"] == ["view"], "归档了还能动手 —— 负责人也只能看"
    assert opened.json()["can_archive"] is True, "能力冻住了，恢复按钮还得画给负责人"
    assert new_session.status_code == 403, "归档的项目还能开会话"
    assert renamed.status_code == 403, "归档的项目还能改设置"

    restored = await client.post(f"/api/v1/projects/{pid}/restore", headers=me)
    assert restored.status_code == 200 and restored.json()["status"] == "active"
    assert "drive" in restored.json()["capabilities"], "恢复之后还是只能看"




@pytest.mark.asyncio
async def test_a_run_still_going_is_stopped_first(an_organisation_server, monkeypatch) -> None:  # noqa: F811
    """冻住一个正在干活的项目，那一轮写到一半的东西就成了没人收的尾巴。重启丢了的不算在跑。"""
    from app.config import settings
    from app.services import run_liveness

    client, factory = an_organisation_server
    pid = await _project_id(factory, "Researcher Project")
    async with factory() as db:
        db.add(Run(id="run_going", tenant_id=settings.runtime_tenant_id, workspace_id="w",
                   project_id=pid, session_id="s1", parent_run_id=None, status=RunStatus.RUNNING.value))
        await db.commit()
    seen = {"run_going": RunStatus.RUNNING.value}

    async def observed(_db, runs, **_kw):
        return {run.id: seen[run.id] for run in runs}

    monkeypatch.setattr(run_liveness, "observed_status_map", observed)
    me = await _as(client, LEAD)

    blocked = await client.post(f"/api/v1/projects/{pid}/archive", headers=me)
    seen["run_going"] = RunStatus.STALE_UNKNOWN.value
    after_a_restart = await client.post(f"/api/v1/projects/{pid}/archive", headers=me)

    assert blocked.status_code == 409 and "还在跑" in blocked.text
    assert after_a_restart.status_code == 200, "重启丢了的那一轮挡住了归档 —— 它永远不会自己结束"


@pytest.mark.asyncio
async def test_a_restart_does_not_resume_work_in_an_archived_project(db_session) -> None:
    from app.services.restart_resume import find_resumable_continuous_runs
    from tests.test_continuous_run_resumes_after_restart import _seed

    rid = await _seed(db_session)
    run = await db_session.get(Run, rid)
    project = await db_session.get(Project, run.project_id)
    project.status = ProjectStatus.ARCHIVED
    await db_session.commit()

    assert await find_resumable_continuous_runs(db_session) == [], "后台替一个归档的项目接着干活"


def test_the_two_states_that_never_did_anything_are_folded(tmp_path: Path) -> None:
    """暂停过的还在做（进行中），完成了的不做了（已归档）。"""
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    here = Path(__file__).resolve().parents[1] / "alembic" / "versions" / "051_project_two_states.py"
    spec = importlib.util.spec_from_file_location("m051", here)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'db.sqlite'}")
    with engine.begin() as conn:
        conn.execute(sa.text("CREATE TABLE projects (id TEXT, status TEXT)"))
        conn.execute(sa.text("INSERT INTO projects VALUES ('a','active'),('p','paused'),"
                             "('c','completed'),('x','archived')"))
        with Operations.context(MigrationContext.configure(conn)):
            migration.upgrade()
        rows = dict(conn.execute(sa.text("SELECT id, status FROM projects")).all())

    assert rows == {"a": "active", "p": "active", "c": "archived", "x": "archived"}
    assert {s.value for s in ProjectStatus} == {"active", "archived"}

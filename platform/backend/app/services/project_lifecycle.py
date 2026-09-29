"""项目归档与恢复 —— 「还在做 / 不做了」（`RFC_ORGANISATION_PAGE_20260923` §4.3）。

## 归档是什么

**冻结**，不是换个标签：
- 只读 —— 能力只剩「看」（`policies.has_project_capability`），开会话、发指令、发布、裁决、改设置、
  管成员一律不行；界面上的能力清单跟着变。
- 后台不再替它干活 —— 重启后的自动续跑跳过它（`restart_resume`）；资讯推送本来就只看进行中的。
- 什么都不丢 —— 知识、会话、产出原样在；交给组织的知识不受影响；能恢复。

从前状态有四种（进行中 / 暂停 / 已完成 / 已归档），没有任何地方因为它们做了不一样的事 —— 一个只改
标签的按钮是假的。现在两种，归档这一种是真的。

## 谁能做

现任负责人（`policies.stewards_it`；专业版再加同组织的管理员，和转交负责人同一个规则）。

## 什么时候不让归档

有一轮**正在跑**（断言在动、且真的还活着 —— 重启丢了的不算）：冻住一个正在干活的项目，那一轮
写到一半的东西就成了没人收的尾巴。先停下它。
"""
from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.execution import ASSERTS_ACTIVE_WORK, Run, RunStatus
from app.models.project import Project, ProjectStatus
from app.models.user import User
from app.policies import stewards_it


async def _the_project(db: AsyncSession, user: User, project_id: str) -> Project:
    project = await db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    if not await stewards_it(db, user, project):
        raise HTTPException(status_code=403, detail="只有项目负责人或组织管理员能归档 / 恢复这个项目")
    return project


async def _a_run_still_going(db: AsyncSession, project: Project) -> bool:
    from app.services import run_liveness

    rows = (await db.execute(select(Run).where(
        Run.project_id == str(project.id),
        Run.status.in_([s.value for s in ASSERTS_ACTIVE_WORK])))).scalars().all()
    if not rows:
        return False
    observed = await run_liveness.observed_status_map(db, rows)
    return any(observed.get(run.id) != RunStatus.STALE_UNKNOWN.value for run in rows)


async def archive(db: AsyncSession, user: User, project_id: str) -> Project:
    project = await _the_project(db, user, project_id)
    if project.status == ProjectStatus.ARCHIVED:
        return project
    if await _a_run_still_going(db, project):
        raise HTTPException(status_code=409, detail="这个项目有一轮还在跑 —— 先停下它再归档")
    project.status = ProjectStatus.ARCHIVED
    await db.flush()
    from app.services.project_record import commit_project_record

    await commit_project_record(db, project=project, user=user, message="lifecycle: archive")
    return project


async def restore(db: AsyncSession, user: User, project_id: str) -> Project:
    project = await _the_project(db, user, project_id)
    if project.status == ProjectStatus.ACTIVE:
        return project
    project.status = ProjectStatus.ACTIVE
    await db.flush()
    from app.services.project_record import commit_project_record

    await commit_project_record(db, project=project, user=user, message="lifecycle: restore")
    return project

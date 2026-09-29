"""项目的记录 —— 项目 / 资源 / 成员的当前投影，经 Git 权威边界提交进项目仓库。

个人版和专业版都走这里：这是「项目即 git 仓库」的一部分，不是组织治理。
（2026-09-28 从 project_governance 改名：那个名字让它被当成专业版的东西。）
"""

from __future__ import annotations

import hashlib
import json

import yaml
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.project import Project, ProjectConfig, ProjectMembership
from app.models.resource import ProjectResource
from app.models.user import User
from app.services.project_repository import (
    ProjectRepositoryError,
    get_project_repository,
    run_in_repository_thread,
)


def _dump(payload: dict) -> str:
    return yaml.safe_dump(payload, allow_unicode=True, sort_keys=False)



async def commit_project_record(
    db: AsyncSession,
    *,
    project: Project,
    user: User,
    message: str,
) -> str | None:
    """把项目 / 资源 / 成员的当前投影提交到项目仓库；返回那次提交的 sha。

    从前这里还要再写一行 `ProjectRevision` 并 CAS 推进 `current_revision_id`。
    可提交本身已经落在 git 上了（`commit_main_files` 带 `operation_id` 判幂等）——
    那一行是投影，而投影和事实分叉时不报错（RFC X1）。
    """
    repository = get_project_repository()
    status = await run_in_repository_thread(
        repository.initialize_project,
        project_id=str(project.id),
        name=project.name,
        description=project.description,
        research_domain=project.research_domain,
        owner_id=project.owner_id,
    )
    # initialize_project 可能刚把老仓迁到 Project v2 —— 提交必须落在**真实的**
    # git head 上。从前这里要提醒自己"别用库里那份陈旧投影"；现在没有第二份
    # 投影可用错了。
    git_base_commit = status.head_commit
    memberships = list(
        (
            await db.execute(
                select(ProjectMembership)
                .where(
                    ProjectMembership.project_id == project.id,
                    ProjectMembership.removed_at.is_(None),
                )
                .order_by(ProjectMembership.created_at, ProjectMembership.user_id)
            )
        )
        .scalars()
        .all()
    )
    resources = list(
        (
            await db.execute(
                select(ProjectResource)
                .where(ProjectResource.project_id == project.id)
                .order_by(ProjectResource.resource_type, ProjectResource.name)
            )
        )
        .scalars()
        .all()
    )
    config = await db.scalar(select(ProjectConfig).where(ProjectConfig.project_id == project.id))
    public_resources = [
        {
            "id": str(item.id),
            "type": item.resource_type,
            "name": item.name,
            "description": item.description,
            "provider": item.provider,
            "endpoint": item.endpoint,
            "workspace_binding": item.workspace_binding,
            "config": item.config or {},
            "secret_ref": item.secret_ref,
            "enabled": item.is_enabled,
        }
        for item in resources
    ]
    files = {
        "project.yaml": _dump(
            {
                "schema_version": 2,
                "project_id": str(project.id),
                "title": project.name,
                "description": project.description,
                "research_domain": project.research_domain,
                "status": project.status.value,
                "default_branch": "main",
                "publication_mode": (
                    "continuous"
                    if config and config.operation_mode.value == "autonomous"
                    else "interactive"
                ),
            }
        ),
        "access/members.yaml": _dump(
            {
                "schema_version": 1,
                "members": [
                    {"user_id": str(item.user_id), "role": item.role} for item in memberships
                ],
            }
        ),
        "resources/registry.yaml": _dump({"schema_version": 1, "resources": public_resources}),
        "resources/secrets.refs.yaml": _dump(
            {
                "schema_version": 1,
                "notice": "References only; credentials are never stored in Git.",
                "secrets": [
                    {
                        "resource_id": str(item.id),
                        "name": item.name,
                        "ref": item.secret_ref,
                    }
                    for item in resources
                    if item.secret_ref
                ],
            }
        ),
        ".research/orchestration/platform.yaml": _dump(
            {
                "schema_version": 2,
                "operation_mode": config.operation_mode.value if config else None,
                "reporting_level": config.reporting_level.value if config else None,
                "preferred_model": config.preferred_model if config else None,
                "tool_whitelist": config.tool_whitelist if config else None,
                "max_concurrent_branches": config.max_concurrent_branches if config else None,
                "cycle_soft_limit": config.cycle_soft_limit if config else None,
                "cycle_hard_limit": config.cycle_hard_limit if config else None,
            }
        ),
    }
    operation_material = json.dumps(files, sort_keys=True, separators=(",", ":"))
    operation_id = f"governance-{hashlib.sha256(operation_material.encode()).hexdigest()}"
    try:
        commit = await run_in_repository_thread(
            repository.commit_main_files,
            project_id=str(project.id),
            expected_main_commit=git_base_commit,
            files=files,
            message=message,
            operation_id=operation_id,
            actor_id=user.id,
        )
    except ProjectRepositoryError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "project_governance_commit_failed", "message": str(exc)},
        ) from exc
    if commit == git_base_commit:
        # 什么都没变 —— `commit_main_files` 幂等返回同一个 sha。
        return None
    return commit

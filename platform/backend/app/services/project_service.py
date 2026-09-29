"""Project Service — initializes the Git Project and its database projection.

This is the main entry point for creating a new research project. It:
1. Creates the Project record（同时初始化 Project Git 仓 + revision 0）
2. Creates the default ProjectConfig

项目的真实结构在 Git 里。08-27 拆除删掉了这里最后两样播种物：一条永远
没人读的 main Branch 行，和两条 total_budget 写死、consumed 恒为 0 的
Budget 行 —— 线上 42 个项目 84 行预算没有一行被消费过。
"""

import hashlib
import json

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.project import (
    EntryType,
    OperationMode,
    Project,
    ProjectConfig,
    ProjectMembership,
    ProjectMembershipRole,
    ProjectStatus,
    ReportingLevel,
)


async def _initialize_repository_and_owner_membership(
    db: AsyncSession, project: Project, owner_id: str
) -> ProjectMembership:
    """建仓 + 建 owner 成员关系。

    从前这里还要写一行 `ProjectRevision`（revision_no=0，manifest 里抄一份
    git head）并把它写进 `project.current_revision_id`。那一行是 git 的投影 ——
    项目的初始状态**就是**仓库的第一个提交（RFC X1）。
    """
    from app.services.project_repository import get_project_repository, run_in_repository_thread

    await run_in_repository_thread(
        get_project_repository().initialize_project,
        project_id=str(project.id),
        name=project.name,
        description=project.description,
        research_domain=project.research_domain,
        owner_id=owner_id,
    )
    membership = ProjectMembership(
        project_id=project.id,
        user_id=owner_id,
        role=ProjectMembershipRole.LEAD.value,
        created_by_user_id=owner_id,
        updated_by_user_id=owner_id,
    )
    db.add(membership)
    await db.flush()
    return membership

async def create_project(
    db: AsyncSession,
    owner_id: str,
    name: str,
    description: str | None = None,
    research_domain: str | None = None,
    entry_type: EntryType = EntryType.FUZZY_IDEA,
    operation_mode: OperationMode = OperationMode.ASSISTED,
    reporting_level: ReportingLevel = ReportingLevel.MEDIUM,
    preferred_model: str | None = None,
) -> dict:
    """Create a project: Git 仓 + 初始 revision + config + branch + 预算。

    v2.1 起项目的真实结构在 Git 里（六个节点目录），不在 nodes/edges 表里。
    返回值里的 nodes/edges 保留为空列表，只为不破坏调用方的形状。
    """
    # 1. Create project
    project = Project(
        owner_id=owner_id,
        name=name,
        description=description,
        research_domain=research_domain,
        entry_type=entry_type,
        status=ProjectStatus.ACTIVE,
    )
    db.add(project)
    await db.flush()  # get project.id
    await _initialize_repository_and_owner_membership(db, project, owner_id)

    # 2. Create config
    config = ProjectConfig(
        project_id=project.id,
        operation_mode=operation_mode,
        reporting_level=reporting_level,
        preferred_model=preferred_model,
    )
    db.add(config)

    return {"project": project, "config": config}

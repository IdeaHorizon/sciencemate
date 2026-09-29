"""Small, explicit RBAC policy layer used by API dependencies and services."""

from collections.abc import Awaitable, Callable

from fastapi import Depends, HTTPException
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.database import get_db
from app.models.project import (
    PROJECT_VISIBLE_TO_THE_ORGANISATION,
    Project,
    ProjectMembership,
    ProjectMembershipRole,
    ProjectStatus,
)
from app.models.user import User, UserRole

ROLE_PERMISSIONS: dict[str, tuple[str, ...]] = {
    UserRole.INSTITUTION_ADMIN.value: (
        "model_backends.manage",
        "institution.manage",
        "groups.manage",
        "users.manage",
        "projects.create",
        "projects.read_all",
        "projects.update_all",
    ),
    UserRole.RESEARCHER.value: (
        "projects.create",
        "projects.read_own",
        "projects.update_own",
        "model_backends.select",
    ),
}

PROJECT_ROLE_CAPABILITIES: dict[str, frozenset[str]] = {
    ProjectMembershipRole.LEAD.value: frozenset(
        {
            "view_session",
            "drive_session",
            "publish_changes",
            "review_decision",
            "manage_members",
            "manage_resources",
            "manage_settings",
        }
    ),
    ProjectMembershipRole.RESEARCHER.value: frozenset(
        {"view_session", "drive_session", "publish_changes", "review_decision"}
    ),
    ProjectMembershipRole.REVIEWER.value: frozenset({"view_session", "review_decision"}),
    ProjectMembershipRole.VIEWER.value: frozenset({"view_session"}),
}


def permissions_for(user: User) -> list[str]:
    return list(ROLE_PERMISSIONS.get(user.role, ROLE_PERMISSIONS[UserRole.RESEARCHER.value]))


def governance_scope_for(user: User) -> dict[str, str]:
    if user.role == UserRole.INSTITUTION_ADMIN.value:
        return {"kind": "institution", "id": user.institution_id, "name": user.institution_name}
    return {"kind": "individual", "id": user.id, "name": user.display_name}


async def visible_project_owner_ids(db: AsyncSession, user: User) -> list[str]:
    """谁建的项目整个都看得见：管理员 = 组里每个人（**包括停用了的** —— 人走了项目不能没主，
    管理员得看得见它才转交得了负责人）；其他人 = 自己。"""
    if user.role == UserRole.INSTITUTION_ADMIN.value:
        query = select(User.id).where(User.institution_id == user.institution_id)
    else:
        query = select(User.id).where(User.id == user.id, User.is_active.is_(True))
    return list((await db.execute(query)).scalars().all())


async def visible_project_ids(db: AsyncSession, user: User):
    """看得见的项目：自己建的、是成员的、管理员看组里全部，外加组里标着「组内可见」的。

    `RFC_ORGANISATION_PAGE_20260923` §3.3：可见范围 `organisation` = 组里所有人只读。
    看得见 ≠ 在「我的项目」里（`mine`，`api/v1/projects._project_response`）。
    """
    owner_ids = await visible_project_owner_ids(db, user)
    membership_ids = select(ProjectMembership.project_id).where(
        ProjectMembership.user_id == user.id,
        ProjectMembership.removed_at.is_(None),
    )
    colleagues = select(User.id).where(User.institution_id == user.institution_id)
    open_to_the_organisation = select(Project.id).where(
        Project.owner_id.in_(colleagues),
        Project.visibility == PROJECT_VISIBLE_TO_THE_ORGANISATION,
    )
    return select(Project.id).where(
        or_(Project.owner_id.in_(owner_ids), Project.id.in_(membership_ids),
            Project.id.in_(open_to_the_organisation))
    )


def my_project_ids(user: User):
    """「我的项目」：自己建的，和是成员的。管理员也一样 —— 组里别的项目在组织页的项目 tab 里
    （`RFC_ORGANISATION_PAGE_20260923` §3.3：看得见 ≠ 在你的列表里）。"""
    membership_ids = select(ProjectMembership.project_id).where(
        ProjectMembership.user_id == user.id,
        ProjectMembership.removed_at.is_(None),
    )
    return select(Project.id).where(or_(Project.owner_id == user.id, Project.id.in_(membership_ids)))


async def active_project_membership(
    db: AsyncSession, user: User, project: Project
) -> ProjectMembership | None:
    return await db.scalar(
        select(ProjectMembership).where(
            ProjectMembership.project_id == project.id,
            ProjectMembership.user_id == user.id,
            ProjectMembership.removed_at.is_(None),
        )
    )


async def _governance_can_view(db: AsyncSession, user: User, project: Project) -> bool:
    if project.owner_id == user.id:
        return True
    owner = await db.get(User, project.owner_id)
    if not owner or owner.institution_id != user.institution_id:
        return False
    return user.role == UserRole.INSTITUTION_ADMIN.value


async def has_project_capability(
    db: AsyncSession,
    user: User,
    project: Project,
    capability: str,
) -> bool:
    # 归档 = 冻结：只能看。开会话、发指令、发布、裁决、改设置、管成员一律不行 —— 规则在这一处，
    # 每个按能力把门的端点和界面上的能力清单（`effective_capabilities`）跟着一起变。恢复不走能力
    # （`project_lifecycle.restore` 按负责人 / 本组织管理员判）：否则冻住的项目就解不开了。
    if project.status == ProjectStatus.ARCHIVED and capability != "view_session":
        return False
    membership = await active_project_membership(db, user, project)
    if membership and capability in PROJECT_ROLE_CAPABILITIES.get(
        membership.role, frozenset()
    ):
        return True
    # Compatibility while legacy direct-created Projects await owner membership backfill.
    if project.owner_id == user.id:
        return capability in PROJECT_ROLE_CAPABILITIES[ProjectMembershipRole.LEAD.value]
    if capability in {"view_session", "manage_members", "manage_settings"}:
        if await _governance_can_view(db, user, project):
            return True
    # 组内可见的项目：同组织的人**只读**进得去（要动手，找负责人把你加进来）。
    if capability == "view_session" and project.visibility == PROJECT_VISIBLE_TO_THE_ORGANISATION:
        owner = await db.get(User, project.owner_id)
        return bool(owner and owner.institution_id == user.institution_id)
    return False


async def can_access_project(db: AsyncSession, user: User, project: Project) -> bool:
    return await has_project_capability(db, user, project, "view_session")


async def require_project_access(
    project_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Project:
    project = await db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if not await can_access_project(db, user, project):
        raise HTTPException(status_code=403, detail="Not authorized for this project")
    return project


# ── 谁能替项目做主 ─────────────────────────────────────────────────────────

#: 发行可以补充「还有谁能替项目做主」（专业版：同组织的管理员）。核心只认负责人。
STEWARDSHIP_EXTENSIONS: list[Callable[[AsyncSession, User, Project], Awaitable[bool]]] = []


async def stewards_it(db: AsyncSession, user: User, project: Project) -> bool:
    """这个人能不能替项目做主（转交负责人、归档、恢复）：现任负责人，或发行补充的那些人。

    不走能力（`has_project_capability`）：归档的项目能力全冻住了，恢复还得有人能做。
    """
    membership = await active_project_membership(db, user, project)
    if project.owner_id == user.id or bool(membership and membership.role == ProjectMembershipRole.LEAD.value):
        return True
    for also in STEWARDSHIP_EXTENSIONS:
        if await also(db, user, project):
            return True
    return False

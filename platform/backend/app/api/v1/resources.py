"""Project logical-resource registry and local compute inventory APIs."""

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.config import settings
from app.database import get_db
from app.models.resource import ProjectResource
from app.models.user import User, UserRole
from app.services import compute_grants
from app.schemas.compute import ComputeInventoryOut
from app.schemas.resource import (
    ProjectResourceCreate,
    ProjectResourceOut,
    ProjectResourceUpdate,
    ResourceType,
)
from app.services.compute_inventory import local_compute_inventory
from app.services.sessions import require_capability

router = APIRouter()
compute_router = APIRouter()


def _resource_response(resource: ProjectResource) -> dict:
    return {
        "id": resource.id,
        "project_id": resource.project_id,
        "resource_type": resource.resource_type,
        "name": resource.name,
        "description": resource.description,
        "provider": resource.provider,
        "endpoint": resource.endpoint,
        "workspace_binding": resource.workspace_binding,
        "config": resource.config or {},
        "is_enabled": resource.is_enabled,
        "health_status": "unknown",
        "has_secret_reference": bool(resource.secret_ref),
        "created_by_user_id": resource.created_by_user_id,
        "updated_by_user_id": resource.updated_by_user_id,
        "created_at": resource.created_at,
        "updated_at": resource.updated_at,
        "disabled_at": resource.disabled_at,
    }


async def _get_resource(
    db: AsyncSession, *, project_id: str, resource_id: str, for_update: bool = False
) -> ProjectResource:
    query = select(ProjectResource).where(
        ProjectResource.tenant_id == settings.runtime_tenant_id,
        ProjectResource.project_id == project_id,
        ProjectResource.id == resource_id,
    )
    if for_update:
        query = query.with_for_update()
    resource = await db.scalar(query)
    if not resource:
        raise HTTPException(status_code=404, detail="Project resource not found")
    return resource


async def _ensure_unique_name(
    db: AsyncSession,
    *,
    project_id: str,
    resource_type: str,
    name: str,
    exclude_id: str | None = None,
) -> None:
    query = select(ProjectResource.id).where(
        ProjectResource.tenant_id == settings.runtime_tenant_id,
        ProjectResource.project_id == project_id,
        ProjectResource.resource_type == resource_type,
        ProjectResource.name == name,
    )
    if exclude_id:
        query = query.where(ProjectResource.id != exclude_id)
    if await db.scalar(query):
        raise HTTPException(
            status_code=409,
            detail="A Project resource with this type and name already exists",
        )


@router.get("/{project_id}/resources", response_model=list[ProjectResourceOut])
async def list_project_resources(
    project_id: str,
    resource_type: ResourceType | None = None,
    include_disabled: bool = Query(default=False),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    await require_capability(db, user, project_id, "view_session")
    query = select(ProjectResource).where(
        ProjectResource.tenant_id == settings.runtime_tenant_id,
        ProjectResource.project_id == project_id,
    )
    if resource_type:
        query = query.where(ProjectResource.resource_type == resource_type)
    if not include_disabled:
        query = query.where(ProjectResource.is_enabled.is_(True))
    resources = list(
        (await db.scalars(query.order_by(ProjectResource.updated_at.desc()))).all()
    )
    return [_resource_response(item) for item in resources]


@router.post(
    "/{project_id}/resources",
    response_model=ProjectResourceOut,
    status_code=201,
)
async def create_project_resource(
    project_id: str,
    data: ProjectResourceCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    project = await require_capability(db, user, project_id, "manage_resources")
    await _ensure_unique_name(
        db,
        project_id=project_id,
        resource_type=data.resource_type,
        name=data.name,
    )
    resource = ProjectResource(
        tenant_id=settings.runtime_tenant_id,
        project_id=project_id,
        resource_type=data.resource_type,
        name=data.name,
        description=data.description,
        provider=data.provider,
        endpoint=data.endpoint,
        workspace_binding=data.workspace_binding,
        config=data.config,
        secret_ref=data.secret_ref,
        created_by_user_id=user.id,
        updated_by_user_id=user.id,
    )
    db.add(resource)
    await db.flush()
    from app.services.project_record import commit_project_record

    await commit_project_record(
        db, project=project, user=user, message=f"resources: add {resource.name}"
    )
    await db.refresh(resource)
    return _resource_response(resource)


@router.get(
    "/{project_id}/resources/{resource_id}",
    response_model=ProjectResourceOut,
)
async def get_project_resource(
    project_id: str,
    resource_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_capability(db, user, project_id, "view_session")
    return _resource_response(
        await _get_resource(db, project_id=project_id, resource_id=resource_id)
    )


@router.patch(
    "/{project_id}/resources/{resource_id}",
    response_model=ProjectResourceOut,
)
async def update_project_resource(
    project_id: str,
    resource_id: str,
    data: ProjectResourceUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    project = await require_capability(db, user, project_id, "manage_resources")
    resource = await _get_resource(
        db, project_id=project_id, resource_id=resource_id, for_update=True
    )
    updates = data.model_dump(exclude_unset=True)
    next_type = updates.get("resource_type", resource.resource_type)
    next_name = updates.get("name", resource.name)
    if next_type != resource.resource_type or next_name != resource.name:
        await _ensure_unique_name(
            db,
            project_id=project_id,
            resource_type=next_type,
            name=next_name,
            exclude_id=resource.id,
        )
    for field, value in updates.items():
        setattr(resource, field, value)
    if "is_enabled" in updates:
        resource.disabled_at = None if resource.is_enabled else datetime.now(UTC)
    resource.updated_by_user_id = user.id
    await db.flush()
    from app.services.project_record import commit_project_record

    await commit_project_record(
        db, project=project, user=user, message=f"resources: update {resource.name}"
    )
    await db.refresh(resource)
    return _resource_response(resource)


@router.delete("/{project_id}/resources/{resource_id}", status_code=204)
async def disable_project_resource(
    project_id: str,
    resource_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    project = await require_capability(db, user, project_id, "manage_resources")
    resource = await _get_resource(
        db, project_id=project_id, resource_id=resource_id, for_update=True
    )
    resource.is_enabled = False
    resource.disabled_at = resource.disabled_at or datetime.now(UTC)
    resource.updated_by_user_id = user.id
    await db.flush()
    from app.services.project_record import commit_project_record

    await commit_project_record(
        db, project=project, user=user, message=f"resources: disable {resource.name}"
    )
    return Response(status_code=204)


@compute_router.get("/inventory", response_model=ComputeInventoryOut)
async def get_local_compute_inventory(
    _user: User = Depends(get_current_user),
) -> dict:
    return local_compute_inventory()


# ── 算力授权：谁能用哪台机器、哪几张卡 ──────────────────────────────────────
#
# 在此之前「算力管理」在产品里不存在：`grants.yaml` 有两个消费方（agent 的提示
# 注入、预注册冻结门禁），却**没有任何写入方** —— 唯一的办法是登到那台机器上
# 手写 YAML。这两个端点把写的那一半接上。
#
# 授权是决策（谁准用什么），现状是事实（卡占没占）——只有前者进文件，后者每次
# 探。这条分工归 `core/capabilities`，这里不复述，只转发。


class _GrantWrite(BaseModel):
    """整份写回。按人分节，`default` 那一节对所有人生效。"""

    grants: dict[str, list[dict[str, Any]]]


@compute_router.get("/grants")
async def read_compute_grants(user: User = Depends(get_current_user)) -> dict[str, Any]:
    """整份授权 + 每条此刻探到的现状。"""
    try:
        return await compute_grants.read(user)
    except compute_grants.ComputeGrantsError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from None


@compute_router.put("/grants")
async def write_compute_grants(
    body: _GrantWrite,
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """改授权 —— 只有机构管理员改得动。

    它决定的是"谁能在哪台机器上跑东西"，而那台机器是整个组织共用的。让每个人
    自己改，等于没有这件事。
    """
    if user.role != UserRole.INSTITUTION_ADMIN.value:
        raise HTTPException(status_code=403, detail="只有机构管理员能改算力授权")
    # `probe_cmd` 是一条**服务器上用 shell 执行**的命令（`core.capabilities._probe`），每次
    # 有人看这一页、每个 agent 开一轮都会跑。网页上加一条，等于让浏览器里的人在服务器上跑
    # 任意命令（2026-09-24 读出来的）。文件里原本就有的（登到服务器上手写的）照样留着、照样
    # 能随整份写回；新的或改过的，网页不收。
    wanted = {str(g.get("probe_cmd")) for entries in body.grants.values()
              for g in entries if isinstance(g, dict) and g.get("probe_cmd")}
    if wanted:
        try:
            current = (await compute_grants.read(user)).get("grants") or {}
        except compute_grants.ComputeGrantsError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from None
        there = {str(g.get("probe_cmd")) for entries in current.values() if isinstance(entries, list)
                 for g in entries if isinstance(g, dict) and g.get("probe_cmd")}
        if wanted - there:
            raise HTTPException(
                status_code=422,
                detail="probe_cmd 是一条会在服务器上执行的命令，网页上加不了、改不了 —— "
                       "要写就登到服务器上手改 grants.yaml")
    try:
        return await compute_grants.write(user, body.grants)
    except compute_grants.ComputeGrantsError as exc:
        # 格式不合规由 harness 那边判（那里是格式的主人），它的话原样递出去。
        raise HTTPException(status_code=422, detail=str(exc)) from None

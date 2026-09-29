"""API router aggregation."""

from fastapi import APIRouter, Depends

from app.api.v1 import (
    artifacts,
    auth,
    capabilities,
    project_instructions,
    chat,
    execution,
    feed,
    kb,
    knowledge,
    literature,
    memory,
    projects,
    resources,
    session_changes,
    sessions,
    settings,
    update,
)
from app.auth import get_current_user
from app.policies import require_project_access

api_router = APIRouter()

# 不鉴权：前端要在拿到任何身份之前知道「这里要不要登录」。
api_router.include_router(capabilities.router, tags=["capabilities"])
# 组织、连接、服务器自升级三组路由由专业版挂（`app/pro`，见 assembly.wire_the_edition）。
api_router.include_router(
    project_instructions.router, tags=["instructions"], dependencies=[Depends(get_current_user)]
)
api_router.include_router(auth.router, prefix="/auth", tags=["auth"])
authenticated = [Depends(get_current_user)]

api_router.include_router(execution.router, tags=["execution"], dependencies=authenticated)
api_router.include_router(update.router, tags=["update"], dependencies=authenticated)
api_router.include_router(chat.router, prefix="/chat", tags=["chat"], dependencies=authenticated)
api_router.include_router(
    projects.router, prefix="/projects", tags=["projects"], dependencies=authenticated
)
api_router.include_router(
    sessions.router, prefix="/projects", tags=["sessions"], dependencies=authenticated
)
api_router.include_router(
    session_changes.router, prefix="/projects", tags=["session-changes"],
    dependencies=authenticated,
)
api_router.include_router(resources.router, prefix="/projects", tags=["resources"])
api_router.include_router(resources.compute_router, prefix="/compute", tags=["compute"])
api_router.include_router(
    artifacts.router,
    prefix="/projects/{project_id}/artifacts",
    tags=["artifacts"],
    dependencies=[*authenticated, Depends(require_project_access)],
)
api_router.include_router(
    artifacts.global_router, prefix="/artifacts", tags=["artifacts"], dependencies=authenticated
)
api_router.include_router(
    knowledge.router, prefix="/knowledge", tags=["knowledge"], dependencies=authenticated
)
api_router.include_router(
    memory.router, prefix="/memory", tags=["memory"], dependencies=authenticated
)
api_router.include_router(
    settings.router, prefix="/settings", tags=["settings"], dependencies=authenticated
)
# ── 科研资讯流 ──
api_router.include_router(feed.router, prefix="/feed", tags=["feed"], dependencies=authenticated)
api_router.include_router(literature.router, prefix="/literature", tags=["literature"], dependencies=authenticated)
# ── Knowledge System v2 ──
api_router.include_router(kb.router, prefix="/kb", tags=["kb"], dependencies=authenticated)

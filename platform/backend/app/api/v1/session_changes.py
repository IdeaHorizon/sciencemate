"""会话变更、冲突、发布 —— 三个端点，答案全部来自 git。

取代 `api/v1/revisions.py` 的十个端点。删掉的那七个（revisions 列表 / 单条、
artifact candidate、receipt、qualification…）在前端**零调用方**：它们是那份
独立版本账的管理面。留下的三个是界面真正在用的：本轮改了什么、有没有冲突、
发布。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.database import get_db
from app.models.user import User
from app.services.session_changes import (
    PublishConflictsFoundError,
    changes_payload,
    publish_session,
    read_session_changes,
)
from app.services.session_changes import resolve_conflict as _resolve_conflict
from app.services.sessions import get_session, require_capability

router = APIRouter()


class PublishRequest(BaseModel):
    message: str = Field(default="Publish Session changes", min_length=1, max_length=500)
    #: 客户端看到的 main head。给了就比对 —— 别人在这期间发布过，就让人先刷新，
    #: 而不是把两份改动悄悄叠在一起。
    expected_head_commit_sha: str | None = None


class ConflictResolveRequest(BaseModel):
    path: str = Field(min_length=1, max_length=1000)
    choice: str = Field(pattern="^(use_project|use_proposed)$")


@router.get("/{project_id}/sessions/{session_id}/change-set")
async def get_session_changes(
    project_id: str,
    session_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    project = await require_capability(db, user, project_id, "view_session")
    _, session = await get_session(db, user, project_id, session_id)
    return await changes_payload(str(project.id), session)


@router.get("/{project_id}/sessions/{session_id}/conflicts")
async def get_session_conflicts(
    project_id: str,
    session_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    project = await require_capability(db, user, project_id, "view_session")
    _, session = await get_session(db, user, project_id, session_id)
    changes = await read_session_changes(str(project.id), session)
    return [
        {"path": path, "status": "open", "resourceKey": path}
        for path in changes.conflicting_paths
    ]


@router.post("/{project_id}/sessions/{session_id}/publish")
async def publish_session_changes(
    project_id: str,
    session_id: str,
    data: PublishRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    project = await require_capability(db, user, project_id, "publish_changes")
    _, session = await get_session(db, user, project_id, session_id)
    try:
        commit = await publish_session(
            db, project=project, session=session, user=user,
            message=data.message,
            change_key=f"session:{session_id}:{session.git_head_commit_sha or 'head'}",
            expected_main_commit=data.expected_head_commit_sha,
        )
    except PublishConflictsFoundError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if commit is None:
        raise HTTPException(status_code=409, detail="Session has no Project changes")
    return {"commitSha": commit, "projectId": str(project.id), "sessionId": session_id}


@router.post("/{project_id}/sessions/{session_id}/conflicts/resolve")
async def resolve_session_conflict(
    project_id: str,
    session_id: str,
    data: ConflictResolveRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    project = await require_capability(db, user, project_id, "publish_changes")
    _, session = await get_session(db, user, project_id, session_id)
    return await _resolve_conflict(
        db, project=project, session=session, path=data.path, choice=data.choice, user=user,
    )

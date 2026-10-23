"""会话的环境状态与经人点头的修复（RFC_WORKER_START_IS_NOT_A_GATE §3.3 / §3.4，窄单 C 的后端那半）。

- `GET  …/environment`：能看会话的人都能读（条幅是给看的人的）。
- `POST …/environment/repair`：能驱动会话的人才能点（它以后端 = 用户自己的身份改数据目录的权限）。
  请求体只有 code 与路径，命令由服务端按表重算 —— 界面上显示的和真正执行的出自同一个函数。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.database import get_db
from app.models.user import User
from app.services.session_environment import RepairRefused, current_environment, run_repair
from app.services.sessions import get_session, require_capability, require_drive_access
from app.services.user_interface import Language, language_on_screen

router = APIRouter()


class RepairRequest(BaseModel):
    code: str
    path: str | None = None


@router.get("/{project_id}/sessions/{session_id}/environment")
async def get_session_environment(
    project_id: str,
    session_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_capability(db, user, project_id, "view_session")
    await get_session(db, user, project_id, session_id)
    return await current_environment(db, project_id=project_id, session_id=session_id)


@router.post("/{project_id}/sessions/{session_id}/environment/repair")
async def repair_session_environment(
    project_id: str,
    session_id: str,
    data: RepairRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    lang: Language = Depends(language_on_screen),
) -> dict:
    project, _session = await get_session(db, user, project_id, session_id)
    await require_drive_access(db, user=user, project=project, session_id=session_id)
    try:
        return await run_repair(db, project_id=project_id, session_id=session_id,
                                code=data.code, path=data.path)
    except RepairRefused as exc:
        raise HTTPException(status_code=409, detail=exc.said_in(lang)) from exc

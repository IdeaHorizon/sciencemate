"""指令是文件：读它、写它 —— **读写的就是 agent 读的那几个文件**。

三张表、一套发布流程、一个 997 行的治理前端，回答的问题只有一个 ——「这个课题
的指令是什么」。答案是一段文本，所以它就该是一个文件（RFC X2）。

X2 之后这里还剩最后一道缝：路径挑错了。

- 个人层写 `<data_root>/user/PROFILE.md` —— 路径里**没有 user id**，一个机构
  所有人共用一份；而 worker 的 harness home 是 `<data_root>/state/users/<uid>`，
  它的加载器读的是那底下的 `user/PROFILE.md`。
- 项目层写 `<data_root>/projects/<id>/PROJECT.md` —— 而 `core/directives_loader`
  在平台上一律用**会话 worktree 里的 `PROJECT.md`** 覆盖（Project v2 的权威）。
  用户在这个编辑器里改完，agent 一个字都读不到，两边都不报错。

现在两个入口写的都是被读的那一个（RFC X3）：

- `GET/PUT /me/instructions`            → `<harness home for this user>/user/PROFILE.md`
- `GET/PUT /projects/{id}/instructions` → 项目仓库 main 上的 `PROJECT.md`
                                          （经 `commit_main_files`，一次提交 = 一个版本）

第三个入口不再叫「冻结的那一份」：会话读的是当下的文件，所以它回答的是
**这个会话现在读到的是什么**（项目层取它自己 worktree 分支上那一份 —— git
就是它的冻结）。
"""
from __future__ import annotations

import hashlib

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.database import get_db
from app.models.execution import SessionProjection
from app.models.project import Project
from app.models.user import User
from app.services.instructions import (
    MAX_INSTRUCTION_LAYER_BYTES,
    PROJECT_FILENAME,
    personal_instruction_path,
    read_instruction_file,
    research_settings_instruction_path,
    write_instruction_file,
)
from app.services.project_repository import (
    ProjectRepositoryError,
    get_project_repository,
    run_in_repository_thread,
)
from app.services.sessions import require_capability

router = APIRouter()


class InstructionText(BaseModel):
    content: str = Field(default="", max_length=MAX_INSTRUCTION_LAYER_BYTES)


def _digest(content: str) -> str | None:
    return hashlib.sha256(content.encode("utf-8")).hexdigest() if content else None


def _payload(path, content: str) -> dict:
    return {"content": content, "path": str(path), "sha256": _digest(content)}


@router.get("/me/instructions")
async def read_personal_instructions(user: User = Depends(get_current_user)) -> dict:
    path = personal_instruction_path(user.id)
    return _payload(path, read_instruction_file(path))


@router.put("/me/instructions")
async def write_personal_instructions(
    data: InstructionText, user: User = Depends(get_current_user)
) -> dict:
    path = personal_instruction_path(user.id)
    write_instruction_file(path, data.content)
    return _payload(path, data.content)


async def _ensure_repository(project: Project) -> str:
    """确保项目仓库在，返回 main 当下的 sha。

    和 `project_record.commit_project_record` 用**同一条**建仓入口 ——
    它幂等，且会把老仓迁到 Project v2。自己另写一套"仓库在不在"的判断，就是
    第二份关于同一件事的代码，而两份迟早不一致。
    """
    status = await run_in_repository_thread(
        get_project_repository().initialize_project,
        project_id=str(project.id),
        name=project.name,
        description=project.description,
        research_domain=project.research_domain,
        owner_id=project.owner_id,
    )
    return status.head_commit


async def _read_project_md(project_id: str) -> tuple[str, str | None]:
    """项目仓库 main 上的 `PROJECT.md` + 那次提交的 sha。

    仓库还没建出来 = 这个项目此刻没有项目层指令，读出来是空 —— 不是故障。
    读接口不该顺手建仓：那会让"看一眼"变成一次写操作。
    """
    repository = get_project_repository()
    try:
        status = await run_in_repository_thread(repository.status, project_id)
        # `read_file_at_revision` 走 `_git`（文本模式 + strip）—— 尾部换行会被吃掉，
        # 于是「读回来的」和「文件里的」差一个字节，来回存一次就少一行。这一层
        # 的全部意义就是"你读到的就是 agent 读到的"，所以取按字节读的那条路。
        payload = await run_in_repository_thread(
            repository.read_worktree_file, project_id, PROJECT_FILENAME
        )
    except ProjectRepositoryError:
        return "", None
    return str(payload.get("content") or ""), status.head_commit


@router.get("/projects/{project_id}/instructions")
async def read_project_instructions(
    project_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await require_capability(db, user, project_id, "view_session")
    content, head = await _read_project_md(project_id)
    label = f"{PROJECT_FILENAME}@{head[:12]}" if head else PROJECT_FILENAME
    return {**_payload(label, content), "commit": head}


@router.put("/projects/{project_id}/instructions")
async def write_project_instructions(
    project_id: str,
    data: InstructionText,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """写项目层 —— 一次提交落在项目仓库 main 上。

    **版本就是 git**：这一次改动有 sha、有作者、有前一版，`git log PROJECT.md`
    就是这个项目的指令史。从前这里写的是数据根底下一个没人读的同名文件，既没有
    历史，也到不了 agent 手里。

    正在跑的会话不会被从脚下换掉：它读的是自己 worktree 分支上那一份，新版本
    随它下一次同步主干进来。
    """
    await require_capability(db, user, project_id, "manage_settings")
    project = await db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    head = await _ensure_repository(project)
    body = data.content if data.content.endswith("\n") else data.content + "\n"
    try:
        commit = await run_in_repository_thread(
            get_project_repository().commit_main_files,
            project_id=project_id,
            expected_main_commit=head,
            files={PROJECT_FILENAME: body},
            message="Update Project instructions",
            operation_id=f"project-instructions-{hashlib.sha256(body.encode()).hexdigest()}",
            actor_id=user.id,
        )
    except ProjectRepositoryError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "project_instructions_commit_failed", "message": str(exc)},
        ) from exc
    return {**_payload(f"{PROJECT_FILENAME}@{commit[:12]}", body), "commit": commit}


@router.get("/projects/{project_id}/sessions/{session_id}/instructions")
async def read_session_instructions(
    project_id: str,
    session_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """这个会话**现在**读到的指令 —— 两层各是什么、各自的 sha256。

    从前这个端点叫「开跑那一刻冻住的那一份」，答案取自会话行上的一列。那一列
    可以是 NULL（041 建列时没 backfill），于是这里对 041 之前的会话一律 404，
    而同一个缺失在跑轮那条路上是硬 raise —— 同一个空值，两处各编了一个说法。

    现在没有那一列可缺：项目层现读它自己 worktree 分支上的 `PROJECT.md`
    （git 就是它的冻结），个人层现读这个用户 harness home 里的两个文件。
    """
    await require_capability(db, user, project_id, "view_session")
    session = await db.get(SessionProjection, session_id)
    if session is None or str(session.project_id) != str(project_id):
        raise HTTPException(status_code=404, detail="Session not found")

    try:
        project_layer = await run_in_repository_thread(
            get_project_repository().read_worktree_file,
            project_id,
            PROJECT_FILENAME,
            session_id=session_id,
        )
        project_text = str(project_layer.get("content") or "")
    except ProjectRepositoryError:
        # 工作区还没建出来（或已被清掉）。这一层此刻读不到内容是**事实**，
        # 不是错误 —— 报空，别把整个端点变成 404。
        project_text = ""

    owner = session.created_by_user_id or session.initiating_user_id or user.id
    profile = read_instruction_file(personal_instruction_path(owner))
    settings_text = read_instruction_file(research_settings_instruction_path(owner))
    personal = "\n\n".join(part.strip() for part in (profile, settings_text) if part.strip())
    return {
        "sessionId": session_id,
        "layers": {
            "project": {
                "filename": PROJECT_FILENAME,
                "source": "session_worktree",
                "content": project_text,
                "sha256": _digest(project_text),
            },
            "personal": {
                "filename": "PROFILE.md",
                "source": "harness_home",
                "content": personal,
                "sha256": _digest(personal),
            },
        },
    }

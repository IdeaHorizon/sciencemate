"""Canonical Research Session, driver, and membership endpoints."""

import asyncio
import threading
from datetime import UTC, datetime, timedelta

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Response,
    UploadFile,
)
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.background import BackgroundTask

from app.auth import get_current_user
from app.config import settings
from app.database import get_db
from app.models.execution import SessionMessage, SessionProjection
from app.models.user import User
from app.schemas.session import (
    SessionArchiveOut,
    SessionCreate,
    SessionInterruptRequest,
    SessionMessagesOut,
    SessionOut,
    SessionRecoveryOut,
    SessionUpdate,
)
from app.services.app_events import record_app_event
from app.services.harness_contract import materials_module
from app.services.session_diagnostics import build_for_session, known_secret_values
from app.services.sessions import (
    create_session,
    get_session,
    recover_stale_session,
    require_drive_access,
    require_capability,
    session_is_empty,
    session_response,
    set_session_model_backend,
)

router = APIRouter()

#: note 是给人看的一句话，不是正文。它进 `.ref` 指针，别让它变成隐藏正文通道。
_MAX_NOTE_CHARS = 500


def _too_large_detail(size_bytes: int, max_bytes: int) -> dict:
    """契约必须送到调用方：上限多少、超了走哪条路，都在报错里说全。

    前端读同一个 `maxBytes` 做选文件时的预检 —— 一个数字，两处使用，不会
    出现"后端调了上限、前端还按老数字放行"。
    """
    return {
        "code": "material_too_large",
        "message": (
            f"文件 {size_bytes} 字节，超过本部署的单份材料上限 {max_bytes} 字节。"
            "更大的数据集别走上传：让 data 节点在计算侧就地取用，"
            "或在部署上给这个项目挂一个数据集绑定（sandbox_mount_bindings）。"
        ),
        "sizeBytes": size_bytes,
        "maxBytes": max_bytes,
    }


@router.get("/{project_id}/sessions", response_model=list[SessionOut])
async def list_sessions(
    project_id: str,
    include_archived: bool = Query(default=False, alias="includeArchived"),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    project = await require_capability(db, user, project_id, "view_session")
    query = select(SessionProjection).where(SessionProjection.project_id == project_id)
    if not include_archived:
        query = query.where(SessionProjection.lifecycle_status != "archived")
    sessions = list(
        (await db.execute(query.order_by(SessionProjection.updated_at.desc()))).scalars().all()
    )
    return [
        await session_response(db, user=user, project=project, session=item) for item in sessions
    ]


@router.post("/{project_id}/sessions", response_model=SessionOut, status_code=201)
async def post_session(
    project_id: str,
    data: SessionCreate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    project = await require_capability(db, user, project_id, "drive_session")
    session = await create_session(
        db,
        user=user,
        project=project,
        title=data.title,
        summary=data.summary,
        model_backend_id=data.model_backend_id,
    )
    return await session_response(db, user=user, project=project, session=session)


@router.get("/{project_id}/sessions/{session_id}", response_model=SessionOut)
async def session_detail(
    project_id: str,
    session_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    project, session = await get_session(db, user, project_id, session_id)
    return await session_response(db, user=user, project=project, session=session)


@router.post(
    "/{project_id}/sessions/{session_id}/recover",
    response_model=SessionRecoveryOut,
    status_code=201,
)
async def recover_session(
    project_id: str,
    session_id: str,
    response: Response,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Start one safe continuation; never claim to resume lost process state."""
    project = await require_capability(db, user, project_id, "drive_session")
    recovered, source_run, created = await recover_stale_session(
        db,
        user=user,
        project=project,
        source_session_id=session_id,
    )
    if not created:
        response.status_code = 200
    return {
        "status": "created" if created else "existing",
        "action": "start_new_session",
        "reason": "lost_runtime_state",
        "sourceSessionId": session_id,
        "sourceRunId": source_run.id,
        "suggestedMessage": (
            "Continue safely from the linked Session. Re-check any external side effects "
            "before repeating interrupted tools."
        ),
        "session": await session_response(
            db, user=user, project=project, session=recovered
        ),
    }




@router.patch("/{project_id}/sessions/{session_id}", response_model=SessionOut)
async def patch_session(
    project_id: str,
    session_id: str,
    data: SessionUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    project = await require_capability(db, user, project_id, "drive_session")
    _, session = await get_session(db, user, project_id, session_id)
    fields = data.model_dump(exclude_unset=True)
    # 后端 id 不能跟着 setattr 一起进去：那等于任何字符串都能写进会话，包括
    # 别的机构的后端。它要走 get_visible_backend 的可见性校验。
    backend_id = fields.pop("model_backend_id", None)
    for field, value in fields.items():
        setattr(session, field, value)
    if backend_id is not None:
        await set_session_model_backend(
            db, user=user, project=project, session=session, backend_id=backend_id
        )
    await db.flush()
    return await session_response(db, user=user, project=project, session=session)


@router.post("/{project_id}/sessions/{session_id}/archive", response_model=SessionArchiveOut)
async def archive_session(
    project_id: str,
    session_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """收起这个会话。**什么都没发生过的，直接删掉。**

    列表里最碍事的从来不是跑过的研究，是误建、开了没用、或者一开局就失败的
    空壳。归档它们只是把垃圾换个地方堆着。所以这里合成一个动作：有账要算的
    归档（记录一条不少），没账可算的直接消失。

    "空"的判据只有 `session_is_empty` 一处 —— 删除端点用的也是它。前端不
    自己去数消息条数：那等于把判据抄第二份，两份迟早不一样。
    """
    project = await require_capability(db, user, project_id, "drive_session")
    _, session = await get_session(db, user, project_id, session_id)
    if await session_is_empty(db, session_id):
        await db.delete(session)
        await db.flush()
        return {"outcome": "deleted", "session": None}
    session.lifecycle_status = "archived"
    session.archived_at = datetime.now(UTC)
    await db.flush()
    return {
        "outcome": "archived",
        "session": await session_response(db, user=user, project=project, session=session),
    }


@router.delete("/{project_id}/sessions/{session_id}", status_code=204)
async def delete_empty_session(
    project_id: str,
    session_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    await require_capability(db, user, project_id, "drive_session")
    _, session = await get_session(db, user, project_id, session_id)
    if not await session_is_empty(db, session_id):
        raise HTTPException(
            status_code=409,
            detail="Only an empty Session may be deleted; archive it instead",
        )
    await db.delete(session)
    await db.flush()
    return Response(status_code=204)


@router.get("/{project_id}/sessions/{session_id}/messages", response_model=SessionMessagesOut)
async def session_messages(
    project_id: str,
    session_id: str,
    after_sequence: int = Query(default=0, ge=0, alias="afterSequence"),
    limit: int = Query(default=200, ge=1, le=500),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await get_session(db, user, project_id, session_id)
    items = list(
        (
            await db.execute(
                select(SessionMessage)
                .where(
                    SessionMessage.session_id == session_id,
                    SessionMessage.sequence > after_sequence,
                )
                .order_by(SessionMessage.sequence)
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    actor_ids = {item.actor_user_id for item in items if item.actor_user_id}
    actors = {
        actor.id: actor
        for actor in (await db.execute(select(User).where(User.id.in_(actor_ids)))).scalars().all()
    }
    payload = []
    for item in items:
        actor = actors.get(item.actor_user_id)
        payload.append(
            {
                "id": item.id,
                "sessionId": item.session_id,
                "sequence": item.sequence,
                "role": item.role,
                "content": item.content,
                "actor": ({"id": actor.id, "displayName": actor.display_name} if actor else None),
                "actorUserId": item.actor_user_id,
                "commandId": item.command_id,
                "runId": item.run_id,
                "offerId": item.offer_id,
                "createdAt": item.created_at,
            }
        )
    return {"items": payload, "nextAfterSequence": items[-1].sequence if items else after_sequence}


@router.get("/{project_id}/sessions/{session_id}/diagnostics")
async def session_diagnostics(
    project_id: str,
    session_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> FileResponse:
    """这个会话的诊断包：全部运行记录 + 同一时段的后端日志，一个 zip。

    **能操作这个会话的人**（``drive_session``）才能下载：包里是整棵原始运行目录
    （工具输出、scratch、事件原件），比界面上任何一处给得都多；组织里只读的成员、
    审稿人能在界面上看会话，但不该整份拿走别人的运行记录。组织服务器上日志只收
    点名了这个会话或项目的记录（见 `app.services.session_diagnostics`）。

    打包要读很多文件，放到线程里跑，别让整台后端等它；**同一时间只打一个** ——
    几个慢的打包各占一个线程，会把仓库读写共用的那个线程池占满。第二个请求直接
    429，不排队（排队也是占着一条连接干等）。
    """
    project, session = await get_session(db, user, project_id, session_id)
    await require_drive_access(db, user=user, project=project, session_id=session_id)
    if not _DIAGNOSTICS_SLOT.acquire(blocking=False):
        raise HTTPException(
            status_code=429,
            detail="另一个诊断包正在生成，等它完成后再下载。",
            headers={"Retry-After": "30"},
        )
    try:
        return await _build_diagnostics_response(db, project_id, session_id, session)
    finally:
        _DIAGNOSTICS_SLOT.release()


#: 诊断包同一时间只打一个。用线程信号量而不是 asyncio 的：打包本来就在线程里，
#: 而且它不绑事件循环（测试里每条用例一个循环，asyncio 原语会跨循环报错）。
_DIAGNOSTICS_SLOT = threading.BoundedSemaphore(1)


async def _build_diagnostics_response(
    db: AsyncSession, project_id: str, session_id: str, session
) -> FileResponse:
    secrets = await known_secret_values(db)
    facts = {
        "title": session.title,
        "lifecycle_status": session.lifecycle_status,
        "created_at": session.created_at,
        "updated_at": session.updated_at,
        "model_backend_id": session.model_backend_id,
        "recovered_from_session_id": session.recovered_from_session_id,
        "git_branch": session.git_branch,
        "git_head_commit_sha": session.git_head_commit_sha,
        "git_worktree_path": session.git_worktree_path,
    }
    bundle = await asyncio.to_thread(
        build_for_session,
        project_id=project_id,
        session_id=session_id,
        session_facts=facts,
        session_created_at=session.created_at,
        secrets=secrets,
    )
    return FileResponse(
        bundle.path,
        media_type="application/zip",
        filename=bundle.filename,
        background=BackgroundTask(bundle.path.unlink, missing_ok=True),
    )


@router.post("/{project_id}/sessions/{session_id}/interrupt", response_model=dict)
async def interrupt_session(
    project_id: str,
    session_id: str,
    data: SessionInterruptRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """跑轮中插话：把一句话投递给**正在干活**的 harness 进程。

    CLI 里人可以在 child 跑着时直接打字，orchestrator 会决定把话注入给子节点
    还是取消它（`chat._handle_interrupt`）。平台此前没有这条路 —— UI 的输入框
    在 running 时直接禁用，人只能干等或 Stop 掉重来。

    为什么不做成一个 RPC：`platform_runtime --serve` 的请求循环严格串行，
    投递面按前端不同（CLI 是 stdin，平台是 HTTP），**决策不分叉** —— 两边都
    落到 `chat._handle_interrupt` 那一份。

    2026-08-23（P1）：平台这一侧的投递从工作区里的文件收件箱换成了 socket
    直达。收件箱当初存在的理由是"协议不能多路复用"，那件事已经修好了。

    只有 Session 的驱动者能插话：这跟发消息是同一类权限，不能让旁观者改方向。
    """
    from app.services.sessions import active_owning_run, interject_active_session

    project = await require_capability(db, user, project_id, "drive_session")
    _, session = await get_session(db, user, project_id, session_id)
    await require_drive_access(db, user=user, project=project, session_id=session_id)

    text = (data.text or "").strip()
    if not text:
        raise HTTPException(status_code=422, detail="interrupt text must not be empty")
    # 与 chat 入口的忙时分流走**同一个**落地函数：先落 user 消息行（插话是
    # 对话的一部分，2026-08-17 前只投收件箱，被取走后没有任何痕迹），再投
    # 收件箱。两个门面一份逻辑，别让它们各自演化。
    running = await active_owning_run(db, session=session)
    try:
        delivered, interjected_message_id, occupancy = await interject_active_session(
            db,
            session=session,
            text=text,
            author_user_id=user.id,
            run_id=str(running.id) if running else None,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except OSError as exc:
        raise HTTPException(status_code=409, detail=f"could not deliver: {exc}") from exc
    # 两个门面一份逻辑 —— 回执留痕也是（RFC P1）。只在 chat 那条路留痕的话，
    # 走 /interrupt 进来的插话事后就没有回音，而它们是同一件事。
    await record_app_event(
        db,
        session_id=str(session.session_id),
        kind="interject.queued",
        run_id=str(running.id) if running else None,
        payload={
            "messageId": interjected_message_id,
            # 送达面是 socket，走到这里就是**已经送到 worker 手里**了；
            # `occupancy` 是它自己报的当下状态，不是我们按 run 行推的。
            "delivery": "delivered_to_live_runtime",
            "occupancy": occupancy,
            "text": text[:280],
        },
        dedupe_key=interjected_message_id,
    )
    await db.commit()
    # 如实回执：**已投递**，不是"已处理"。agent 最多 3 秒后取走，随后跑一个
    # 决策轮决定怎么用它。谎称"已处理"会让用户以为方向已经改了。
    return {"status": "delivered", "deliveredAt": delivered}


@router.post("/{project_id}/sessions/{session_id}/stop", response_model=dict)
async def stop_session_turn(
    project_id: str,
    session_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """停止当前轮：投递一个**机械**停止信号给正在干活的 harness 进程。

    与 `/interrupt` 走同一条投递通道（收件箱），但语义完全不同：插话是开放的
    话，交给 mini 决策轮理解；停止是一个布尔，harness 侧直接写 kill_signal
    （与 CLI /stop 同一份逻辑），**不经过模型**。取消在模型调用与工具派发两个
    咽喉处生效 —— 正在跑的那一步会跑完，之后不再发起新的调用，本轮以
    cancelled 收尾。下一条用户消息照常开新轮（取消在轮边界自动解除）。

    没有活体进程 = 没有"当前轮"可停 → 409 如实说，而不是把一条停止扔进一个
    没人听的地方（下一轮开始时会作废存量 stop，但不该依赖那道保险）。

    权限同 /interrupt：只有 Session 驱动者能停 —— 这是改变执行进程的动作，
    不能让旁观者按。
    """
    from app.services.execution_view import has_live_runtime
    from app.services.harness_sessions import HarnessSessionError, harness_session_manager

    project = await require_capability(db, user, project_id, "drive_session")
    _, session = await get_session(db, user, project_id, session_id)
    await require_drive_access(db, user=user, project=project, session_id=session_id)

    # 与 view 的 `canStop` **同一个谓词**（不是同样的写法，是同一个函数）：
    # 按钮的可见性与这道准入若各写一份，就会重演「按钮亮着、按下去 409」。
    if not has_live_runtime(project_id, session_id):
        raise HTTPException(
            status_code=409,
            detail="Nothing is running in this Session right now — there is no current turn to stop.",
        )
    try:
        receipt = await harness_session_manager.deliver(
            project_id=project_id,
            session_id=session_id,
            kind="stop",
            text="停止当前轮",
            author=user.id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except HarnessSessionError as exc:
        raise HTTPException(status_code=409, detail=f"could not deliver: {exc}") from exc
    # 如实回执：**已送达 worker**（不再是"已写进一个它待会儿会来看的文件"）。
    # 正在进行的生成被立即切断（≤0.5s），正在跑的那一步会跑完，之后停。
    return {"status": "delivered", "itemId": receipt.get("item_id", "")}


@router.post("/{project_id}/sessions/{session_id}/files", response_model=dict, status_code=201)
async def add_file_to_session(
    project_id: str,
    session_id: str,
    file: UploadFile = File(...),
    note: str = Form(default=""),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """把用户交来的一份文件放进**这个会话的工作区**并提交指针。

    这是平台上传文件的**唯一**入口。它取代了两条各自只覆盖一半场景的老路：

      · 会话附件（`.research/runtime/attachments/`，gitignored、只此会话、无
        出处）—— agent 在别的会话里读不到，publish 也带不走。
      · 项目材料（直接 commit canonical main）—— **已存在的会话**读不到，
        因为那些分支早就分出去了。用户在会话里传文件，正好落进这个缺口。

    现在只有一个答案：文件就是这个 worktree 里 `sources/<名字>`
    这个真实文件，agent 下一轮就能 `read_file`，sandbox 里同路径可见；要跨
    会话复用就 publish，和其它任何改动同一条路。

    字节按内容寻址存在项目的字节池里，Git 只跟踪一份 `.ref` 指针（见
    `core/materials`）—— 所以这里没有"多大的文件走哪条路"的分档，只有一条
    上限 `settings.material_max_bytes`。
    """
    project = await require_capability(db, user, project_id, "drive_session")
    _, session = await get_session(db, user, project_id, session_id)
    await require_drive_access(db, user=user, project=project, session_id=session_id)

    if not file.filename:
        raise HTTPException(status_code=422, detail="这份文件没有名字，浏览器没把文件名传上来")
    if not session.git_worktree_path:
        raise HTTPException(status_code=409, detail="This Session has no Git worktree")

    # 粗筛在 `RequestBodyCapMiddleware`（读 body 之前，按 Content-Length）；
    # 这里是**精确**判据：流式计数，一个字节都不含 multipart 边界。
    maximum = settings.material_max_bytes

    from app.services.project_repository import (
        ProjectRepositoryError,
        get_project_repository,
        run_in_repository_thread,
    )

    # `core` 走契约取，不写模块顶部的 `from core import ...` —— App Server 进程里
    # `core` 不在 import path 上（部署 cwd 是 platform/backend），而 pytest 会把
    # 仓库根加进去：顶层 import 在测试里全绿、在真部署上进程直接起不来。
    materials = materials_module()
    repository = get_project_repository()
    try:
        reference, commit = await asyncio.to_thread(
            repository.add_session_material,
            project_id,
            session_id,
            filename=file.filename,
            stream=file.file,
            uploaded_by=str(user.id),
            note=note.strip()[:_MAX_NOTE_CHARS],
            max_bytes=maximum,
        )
    except materials.MaterialTooLargeError as exc:
        raise HTTPException(
            status_code=413, detail=_too_large_detail(exc.size_bytes, exc.max_bytes)
        ) from exc
    except materials.MaterialNameConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail={"code": "material_name_conflict", "message": str(exc)},
        ) from exc
    except materials.MaterialError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ProjectRepositoryError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    if commit is not None:
        session.git_head_commit_sha = commit

    # 上传是时间线上的一件事，不是草稿框里的一行字。老实现把「📎 附件：路径」
    # 塞进用户的输入草稿 —— 用户一删就没人知道这文件存在过，而模型全靠那行字。
    await record_app_event(
        db,
        session_id=session_id,
        kind="material.added",
        payload={
            "name": reference.name,
            "path": reference.path,
            "absolutePath": str(reference.absolute_path),
            "sha256": reference.sha256,
            "sizeBytes": reference.size_bytes,
            "actorId": str(user.id),
            "note": reference.note,
        },
        dedupe_key=f"material:{session_id}:{reference.sha256}:{reference.name}",
    )
    return {
        "name": reference.name,
        "path": reference.path,
        "absolutePath": str(reference.absolute_path),
        "sha256": reference.sha256,
        "sizeBytes": reference.size_bytes,
        "commitSha": commit,
        "committed": commit is not None,
    }


@router.post("/{project_id}/sessions/{session_id}/undo", response_model=dict)
async def undo_last_session_write(
    project_id: str,
    session_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """撤销本会话最近一次写。

    用 Git revert，不是"从备份恢复那个文件"：一次操作往往同时写产物、索引、
    账本，单独回滚一个文件会造出从未存在过的状态。而且本仓的规矩是"撤销 =
    留痕的反向操作，不是删除" —— revert 生成新 commit，历史完整。

    冻结产物不许回滚（预注册防篡改）。
    """
    from app.services.project_repository import get_project_repository, run_in_repository_thread

    project = await require_capability(db, user, project_id, "drive_session")
    await get_session(db, user, project_id, session_id)
    await require_drive_access(db, user=user, project=project, session_id=session_id)
    try:
        return await run_in_repository_thread(
            get_project_repository().revert_last_session_commit,
            project_id=project_id, session_id=session_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/{project_id}/projects-dreaming/skip", response_model=dict)
async def skip_project_dreaming(
    project_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """跳过本次 dreaming（KB 整理）。下次触发条件满足时会重新 mark。

    这是**项目级**的，不属于某个会话 —— dreaming 整理的是整个项目的 KB。
    """
    from app.services.harness_contract import (
        HarnessContractUnavailable,
        dreaming_scheduler,
    )

    await require_capability(db, user, project_id, "drive_session")
    try:
        dreaming_scheduler().clear_pending(project_id)
    except HarnessContractUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"status": "skipped"}


@router.post("/{project_id}/sessions/{session_id}/reset", response_model=dict)
async def reset_session_conversation(
    project_id: str,
    session_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """清对话历史。**memory / KB / 产物一律不动。**

    只在空闲时可用（和 CLI 同一条约束）：清的是正在被使用的对话，跑轮中清
    等于把 agent 的记忆从它手里抽走。

    用途：上下文被一段跑偏的讨论污染时，重开一段而不丢已经沉淀下来的东西。
    """
    from app.services.harness_sessions import (
        HarnessSessionError,
        harness_session_manager,
    )

    project = await require_capability(db, user, project_id, "drive_session")
    await get_session(db, user, project_id, session_id)
    await require_drive_access(db, user=user, project=project, session_id=session_id)
    try:
        return await harness_session_manager.reset_conversation(project_id, session_id)
    except HarnessSessionError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc











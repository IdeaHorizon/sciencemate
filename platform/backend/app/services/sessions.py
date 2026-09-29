"""Transactional Research Session, driver lease, and membership services."""

import logging
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from typing import NamedTuple
from decimal import Decimal
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import set_committed_value

from app.config import settings
from app.services import execution_view as _view
from app.services import run_liveness as _liveness
from app.models.execution import RunAttempt
from app.models.execution import REQUIRES_LIVE_RUNTIME_STATUSES
from app.models.execution import (
    AWAITING_HUMAN_RUN_STATUSES,
    UNFINISHED_RUN_STATUSES,
    ExecutionEvent,
    RunStatus,
    Run,
    SessionMessage,
    SessionProjection,
)
from app.models.model_backend import ModelBackendConfig
from app.models.project import Project
from app.models.user import User
from app.services.user_interface import language_for as _language_for
from app.policies import PROJECT_ROLE_CAPABILITIES, has_project_capability
from app.services.model_backends import effective_backend, get_visible_backend
from app.services.research_settings import (
    effective_research_settings,
    research_settings_snapshot,
)

logger = logging.getLogger(__name__)

PLACEHOLDER_SESSION_TITLES = frozenset(
    {"New conversation", "New research", "New research Session"}
)

# 跨进程线协议：harness 把「这一次呈递」整份挂在 pause 事件的这个键下
# （core/decision_offer.py 的 PAUSE_OFFER_KEY）。backend 不 import harness 包
# （独立进程、独立 venv —— 这里曾直接 import，把整个 backend 弄挂了），所以
# 像 HTTP header 名一样在边界两侧各写一次字面量；两侧一致由
# tests/test_offer_survives_the_whole_pipe.py 机械钉住。
PAUSE_OFFER_KEY = "offer"


MECHANICAL_TITLE_MAX_CHARS = 80


def _now() -> datetime:
    return datetime.now(UTC)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def build_platform_context_snapshot(
    *, project: Project, session: SessionProjection | None, backend: ModelBackendConfig
) -> dict:
    """Build the small authoritative context frozen for one Session."""
    # 基线是**一个 git 提交**。从前这里是 project_revisions 的一行 id ——
    # 而那行自己也只是 git head 的一份拷贝（RFC X1）。
    base_commit = session.git_base_commit_sha if session is not None else None
    return {
        "version": 1,
        "project": {"id": str(project.id), "name": project.name},
        "baseCommitSha": base_commit or None,
        "modelBackend": {
            "id": str(backend.id),
            "displayName": backend.display_name,
            "provider": backend.provider,
            "model": backend.model,
        },
    }


async def session_is_empty(db: AsyncSession, session_id: str) -> bool:
    """这个会话有没有发生过任何事 —— 归档与删除**共用**的那一个判据。

    两处各写一遍的话，"空"迟早在两边长成两个意思（其中一边还会先漂）。
    """
    for statement in (
        select(func.count()).select_from(SessionMessage).where(
            SessionMessage.session_id == session_id
        ),
        select(func.count()).select_from(Run).where(Run.session_id == session_id),
    ):
        if await db.scalar(statement):
            return False
    return True


async def set_session_model_backend(
    db: AsyncSession,
    *,
    user: User,
    project: Project,
    session: SessionProjection,
    backend_id: str,
) -> ModelBackendConfig:
    """换掉这个会话**往后**用的模型。

    换模型不是没有代价：同一份研究的不同轮次会由不同模型产出。原来的做法是
    干脆不提供接口（前端还写了一句"会话中途不允许换模型"），代价却落在别处
    —— 2026-08-13 实测：机构默认后端配错（地址空 + 别家的 key）只会 401，
    一个会话两次 run 全废、零产出，却被钉死在那个后端上，连自救的路都没有。
    锁定挡不住错误配置，只挡住了修正。

    所以这里让它可换，归因由**记录**承担：每条 run 的 summary 里记着它实际
    用的 `modelBackendId`，要追"这个结论谁产出的"看 run，不看 session。
    """
    backend = await get_visible_backend(db, user, backend_id)
    session.model_backend_id = backend.id
    # 快照跟着改。`platform_context_snapshot` 里也有一份 modelBackend，是送给
    # harness 的那一份；只改一处就会出现"UI 显示 A、harness 收到 B"。
    session.platform_context_snapshot = build_platform_context_snapshot(
        project=project, session=session, backend=backend
    )
    await db.flush()
    return backend


def mechanical_session_title(message: str) -> str:
    """把第一条消息机械截成一个标题。

    只有这一个定义。此前有两份：这里截 80 带省略号，`chat.py` 建会话时截 300
    原样存 —— 于是"这个标题是不是机器起的"没有可判的依据，两边各自演化。
    """
    title = " ".join(message.split())
    if not title:
        return ""
    if len(title) <= MECHANICAL_TITLE_MAX_CHARS:
        return title
    return f"{title[: MECHANICAL_TITLE_MAX_CHARS - 3].rstrip()}..."


def machine_made_title_forms(first_message: str) -> set[str]:
    """平台**已知会产出**的标题形态，逐字。

    两种，不是一种：`mechanical_session_title`（收敛空白、80 字符、带省略号），
    以及建会话时那条更老的路径留下的裸截断（`message.strip()[:300]`，既不收敛
    空白也不加省略号）。库里两种都大量存在 —— 只认一种，另一种就会被当成"人
    挑的标题"永远不动，而那恰好是绝大多数存量会话的样子。
    """
    stripped = first_message.strip()
    return {
        form
        for form in (mechanical_session_title(first_message), stripped[:300])
        if form
    }


def session_title_is_machine_made(session: SessionProjection, first_message: str) -> bool:
    """这个标题是平台自己起的，还是人挑的？

    **现算，不落字段**。存一个 `title_source` 就是把判决写进库里：规则一改
    （比如截断长度变了），库里那份标记立刻和事实对不上，而且不报错。判据本身
    是机械可判的 —— 标题要么还是占位符，要么逐字等于平台某条路径产出的截断，
    两者都只可能是平台写的；除此之外一律当人挑过，不碰。

    自动命名写进去之后，标题不再等于任何一种机械形态，这个函数自然返回
    False ——"只命名一次"不需要额外的标记来保证。反过来，命名失败留下的仍是
    机械标题，下一轮会自己再试。
    """
    current = (session.title or "").strip()
    if current in PLACEHOLDER_SESSION_TITLES:
        return True
    return bool(current) and current in machine_made_title_forms(first_message)


def set_session_title_from_first_message(session: SessionProjection, message: str) -> bool:
    """Replace only known placeholder titles with a bounded first-message title."""
    if session.title not in PLACEHOLDER_SESSION_TITLES:
        return False
    title = mechanical_session_title(message)
    if not title:
        return False
    session.title = title
    return True


async def require_capability(
    db: AsyncSession, user: User, project_id: str, capability: str
) -> Project:
    project = await db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if not await has_project_capability(db, user, project, capability):
        raise HTTPException(status_code=403, detail=f"Missing project capability: {capability}")
    return project


async def get_session(
    db: AsyncSession, user: User, project_id: str, session_id: str
) -> tuple[Project, SessionProjection]:
    project = await require_capability(db, user, project_id, "view_session")
    session = await db.scalar(
        select(SessionProjection).where(
            SessionProjection.session_id == session_id,
            SessionProjection.project_id == project_id,
        )
    )
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    return project, session


async def effective_capabilities(
    db: AsyncSession, user: User, project: Project, *, api_names: bool = False
) -> list[str]:
    capabilities = [
        capability
        for capability in sorted(
            {item for capabilities in PROJECT_ROLE_CAPABILITIES.values() for item in capabilities}
        )
        if await has_project_capability(db, user, project, capability)
    ]
    if not api_names:
        return capabilities
    aliases = {
        "view_session": "view",
        "drive_session": "drive",
        "publish_changes": "publish",
        "review_decision": "resolve",
        "manage_members": "manage_members",
        "manage_resources": "manage_resources",
        "manage_settings": "manage_settings",
    }
    return [aliases[item] for item in capabilities]


async def create_session(
    db: AsyncSession,
    *,
    user: User,
    project: Project,
    title: str,
    summary: str | None,
    model_backend_id: str | None,
) -> SessionProjection:
    if not await has_project_capability(db, user, project, "drive_session"):
        raise HTTPException(status_code=403, detail="Missing project capability: drive_session")
    backend = (
        await get_visible_backend(db, user, model_backend_id)
        if model_backend_id
        else await effective_backend(db, user)
    )
    personal_settings = research_settings_snapshot(await effective_research_settings(db, user=user))
    now = _now()
    session = SessionProjection(
        tenant_id=settings.runtime_tenant_id,
        workspace_id=user.group_id or user.institution_id,
        project_id=str(project.id),
        session_id=str(uuid4()),
        initiating_user_id=user.id,
        title=title.strip(),
        summary=summary,
        created_by_user_id=user.id,
        lifecycle_status="active",
        model_backend_id=backend.id,
        platform_context_snapshot=build_platform_context_snapshot(
            project=project, session=None, backend=backend
        ),
        policy_snapshot_id="local-runtime-v1",
        research_settings_snapshot_id=personal_settings["snapshot_id"],
        research_settings_snapshot=personal_settings,
        knowledge_read_watermark={},
    )
    db.add(session)
    await db.flush()
    from app.services.project_repository import get_project_repository, run_in_repository_thread

    repository = get_project_repository()
    repository_status = await run_in_repository_thread(
        repository.initialize_project,
        project_id=str(project.id),
        name=project.name,
        description=project.description,
        research_domain=project.research_domain,
        owner_id=project.owner_id,
    )
    # 会话从 main 的当前 head 分出去。从前这里要先看库里那行 revision 有没有
    # git_commit_sha、没有就补写回去 —— 那一行本来就是 git head 的拷贝。
    base_commit = repository_status.head_commit
    workspace = await run_in_repository_thread(
        repository.ensure_session_workspace,
        project_id=str(project.id),
        session_id=session.session_id,
        base_commit=base_commit,
        title=session.title,
        created_by=user.id,
    )
    session.git_branch = workspace.branch
    session.git_base_commit_sha = workspace.base_commit
    session.git_head_commit_sha = workspace.head_commit
    session.git_worktree_path = workspace.path
    # 基线要等工作区真的建出来才知道 —— 快照在那之前拼好，里面的 baseCommitSha
    # 还是空的。重拼一次，别让送给 harness 的那份说"没有基线"。
    session.platform_context_snapshot = build_platform_context_snapshot(
        project=project, session=session, backend=backend
    )
    await db.flush()
    # The final flush updates ``updated_at`` through the database-side
    # ``onupdate`` expression.  Refresh here so async response serialization
    # never attempts an implicit (and therefore invalid) lazy load.
    await db.refresh(session)
    return session


#: 「这个挂起的询问已经作废，人知道了」。带上它的 run 不再阻塞本会话。
#:
#: 为什么需要一个显式标记：光把 run 标成 `stale_unknown` 没用 —— 活性闸的
#: 拦截名单里本来就有 `STALE_UNKNOWN`。而拦截是对的：worker 没了的时候，
#: 用户在 UI 上看到的那个问题也没了，他那句"批准"要是被当成新一轮的开场白
#: 发给一个全新的 worker，就是**在一个不知道批什么的地方按了同意**。
#:
#: 所以解除的条件不是"标记成死的"，是"人确认过它死了"。
PAUSE_ABANDONED = "pauseAbandonedAt"


async def resume_stale_session(
    db: AsyncSession,
    *,
    user: User,
    project: Project,
    session_id: str,
) -> tuple[SessionProjection, list[str]]:
    """在**同一个 Session** 里接着跑 —— 丢掉死掉的暂停，留下对话。

    ## 为什么这条路本来就该有

    worker 进程没了，死掉的是**暂停**（在进程内存里，真的救不回来）。但
    **对话**不在内存里：`agent_loop` 每一轮都把 messages 写进这个 Session
    自己的 `messages_checkpoint.json`，而新 worker 用同一个 session_id 起来时
    `op=init` 会无条件 `_load_conversation` 把它读回来，还会顺手
    `recover_interrupted_decision_actions` 修被打断的决策。

    也就是说续跑一直是完整实现的，只是**没有入口**：唯一的恢复动作是
    `recover_stale_session`，它铸一个新的 session_id，而 state root 的路径是
    按 session_id 拼的 —— 新 worker 去新目录找 checkpoint，当然是空的。

    2026-08-13 实测：一次 App Server 重启之后，老 Session 的 checkpoint
    （15 条消息 / 152KB）好端端躺在磁盘上，agent 却从零开始重新读文件定位，
    它重启前刚测出来的 Numba 加速比和成本估算全部要重做一遍。

    **用不可恢复的那一半，判了可恢复的那一半死刑** —— 又是把两件事按一条
    规则处理。

    ## 和「开新 Session」的关系

    两个都是合法操作，留给人选：想甩掉一段又长又乱的上下文就开新的（上下文
    衰减是真实的硬墙），想接着原来的思路就续这一条。区别只是**由人决定**，
    而不是"平台重启了所以你只能开新的"。
    """
    from datetime import UTC, datetime

    if not await has_project_capability(db, user, project, "drive_session"):
        raise HTTPException(status_code=403, detail="Missing project capability: drive_session")

    session = await db.scalar(
        select(SessionProjection).where(
            SessionProjection.session_id == session_id,
            SessionProjection.project_id == str(project.id),
        )
    )
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    from app.services.harness_sessions import (
        HarnessSessionStaleError,
        assert_conversation_runtime_available,
        mark_orphaned_harness_runs,
    )

    try:
        await assert_conversation_runtime_available(
            db,
            project_id=str(project.id),
            conversation_id=session_id,
            user_id=user.id,
        )
    except HarnessSessionStaleError:
        pass
    else:
        # 运行时还活着 —— 没有东西需要恢复。直接发消息就行，别悄悄把一个
        # 活着的暂停标成作废。
        raise HTTPException(
            status_code=409,
            detail={
                "code": "session_runtime_alive",
                "message": "This Session is still live — send your message directly.",
            },
        )

    await mark_orphaned_harness_runs(db)
    blocked = (
        await db.execute(
            select(Run).where(
                Run.project_id == str(project.id),
                Run.session_id == session_id,
                # D11：判决不再落进 status，所以这里也不能按 status 认"卡住了"。
                # 候选取"需要活体运行时"的全部状态，死活由 runtime_lost 现算 ——
                # 判据从"库里写着什么"回到"现在还在不在"。
                Run.status.in_([status.value for status in REQUIRES_LIVE_RUNTIME_STATUSES]),
            )
        )
    ).scalars().all()

    abandoned: list[str] = []
    for run in blocked:
        summary = dict(run.summary) if isinstance(run.summary, dict) else {}
        if summary.get(PAUSE_ABANDONED):
            continue
        summary[PAUSE_ABANDONED] = datetime.now(UTC).isoformat()
        # 判决要说清楚**为什么**它不再拦路，否则下一个人看到 stale_unknown
        # 只会再查一遍今天这条链。
        summary["staleReason"] = summary.get("staleReason") or "runtime_lost"
        # D11：这里曾经把 waiting_human 盖成 stale_unknown。判决不落盘 ——
        # 人已经确认过"那个挂起的询问没了"，该事实由 `PAUSE_ABANDONED` 这个
        # **时间戳**记录（上面刚写），它是可观测的：人在某时刻确认过。
        # "所以这条 run 算什么状态"由消费方现算，别覆盖它当时在等人这个事实。
        run.summary = _liveness.witness(
            run, reason=summary["staleReason"], detected_at=summary[PAUSE_ABANDONED],
        ) | summary
        abandoned.append(run.id)
    await db.flush()
    return session, abandoned


async def recover_stale_session(
    db: AsyncSession,
    *,
    user: User,
    project: Project,
    source_session_id: str,
) -> tuple[SessionProjection, Run, bool]:
    """Create one explicit continuation Session for a lost Harness runtime.

    This deliberately does not claim to resume a Python process or an in-flight
    tool call.  It preserves the source Session's frozen execution context and
    starts a fresh worktree branch from the same base commit.  The unique lineage
    constraint makes retries idempotent.
    """
    if not await has_project_capability(db, user, project, "drive_session"):
        raise HTTPException(status_code=403, detail="Missing project capability: drive_session")

    source = await db.scalar(
        select(SessionProjection)
        .where(
            SessionProjection.session_id == source_session_id,
            SessionProjection.project_id == str(project.id),
        )
        .with_for_update()
    )
    if not source:
        raise HTTPException(status_code=404, detail="Session not found")

    # ── 两个问题，两个答案（2026-08-21）──────────────────────────────────
    #
    # 这里原来只有一个 `source_run = 最近更新的那一行`，同时回答两件事：
    #
    #   Q1「这个会话现在还能不能被驱动？」→ 该看**当前**状态，最新那条是对的。
    #   Q2「我们在接续哪个丢掉的 run？」  → 该看**平台记下运行时丢了**的那条。
    #
    # 一个 pause 作废之后用户接着发消息，同一 session 下就会多出更新的 run；
    # 于是 Q2 拿到的是那条续跑的 run，而不是真正丢掉的那条。两条 run 的
    # `updated_at` 落在同一刻度时才靠 `Run.id` 字典序兜底 —— 所以它平时是绿的、
    # 偶尔翻车：CI 上同一棵树 PR run 绿、push run 红（`6537bfb1` vs `33b79516`，
    # `git diff` 为空），红在 test_lost_kept_alive_pause_becomes_stale_unknown 的
    # `sourceRunId` 上。用「最近更新的那一行」回答「哪个 run 的运行时丢了」，
    # 本来就是拿"像不像"代替"是不是"。
    runs = list((await db.scalars(
        select(Run)
        .where(Run.session_id == source_session_id)
        .order_by(Run.updated_at.desc(), Run.id.desc())
    )).all())
    latest_run = runs[0] if runs else None

    #: 血缘锚点：「运行时丢了」不靠排序猜，读平台自己写下的事实 —— `staleReason`。
    #:
    #: 写入方都只在运行时真的丢了时才落它：`mark_orphaned_harness_runs`
    #: （app_server_restart）、`local_execution` 答复失败路径按
    #: `terminal_state_for` 落的 staleReason（harness_process_lost 等）、以及
    #: 作废 pause 时的 `runtime_lost`。普通失败走 `terminal_state_for` 的
    #: 最后一支，返回 `(failed, None)` —— **不写** staleReason。
    #:
    #: 所以没有任何一条带它时，最新那条就是丢掉的那条（run 直接 failed 的情形），
    #: 回退到 latest 是对的，不是兜底。
    lost_run = next(
        (run for run in runs
         if isinstance(run.summary, dict) and run.summary.get("staleReason")),
        None,
    ) or latest_run

    # 下面的**资格**判据一律只问"现在"，所以都读 latest。
    source_run = latest_run
    summary = latest_run.summary if latest_run and isinstance(latest_run.summary, dict) else {}

    # ── 恢复资格按"现在还能不能接着跑"判，不按"当初怎么死的"判 ──────────────
    # 原判据要求 staleReason == 'app_server_restart' —— 只认一种死法。
    # harness 子进程被 kill / OOM / 机器重启时标签对不上，于是**明明恢复不了
    # 的 session 被拒绝恢复**：聊天端点已经 409 并把 recover 链接指给用户，
    # 用户点了却得到 session_not_recoverable（E2E v13 实测：9 个 MD 全跑完、
    # 数据在磁盘上，卡在这里进不去汇总）。
    #
    # 平台本来就有权威的活性判据 —— assert_conversation_runtime_available
    # （聊天端点用的就是它）。同一个事实两处各判各的，必然对不上。这里复用
    # 它：能接着跑 → 不给恢复（避免误开分叉）；接不上 → 允许恢复。
    from app.services.harness_sessions import (
        HarnessSessionStaleError,
        assert_conversation_runtime_available,
        harness_session_manager,
    )

    runtime_lost = False
    runtime_lost_reason = ""
    #: 探针只认它名单里的状态（WAITING_HUMAN / STALE_UNKNOWN），对 failed /
    #: incomplete 一言不发 —— 那种"没结论"不能当成"还活着"。区分开。
    probe_was_inconclusive = bool(
        source_run
        and source_run.status
        not in {RunStatus.WAITING_HUMAN.value, RunStatus.STALE_UNKNOWN.value}
    )
    try:
        await assert_conversation_runtime_available(
            db,
            project_id=str(project.id),
            conversation_id=source_session_id,
            user_id=user.id,
        )
    except HarnessSessionStaleError as exc:
        runtime_lost = True
        runtime_lost_reason = str(exc)
    except Exception:
        # 判据自身出错时保守：不放行恢复（fail-closed），但别吞掉
        runtime_lost = False

    # 老路径（app server 重启导致的 stale）继续单独认，无需依赖上面的活性探测。
    #
    # D11：判据从「status 被盖成 stale_unknown」换成**现算**。平台不再把判决
    # 写进事实字段，所以这里也不能再从字段上读回那个判决 —— 它读的是注册表
    # 里此刻还有没有活 binding，配上平台当时留下的见证（staleReason）。
    #
    # 老库里已经被盖成 stale_unknown 的行仍然认（迁移前的数据不会自己变），
    # 但新数据走现算这条路。
    _binding = harness_session_manager.live_binding(str(project.id), source_session_id)
    _has_live = bool(_binding and source_run and _binding.run_id == source_run.id)
    # D10：注册表是**进程内缓存**，后端一重启就是空的 —— 只问它，会把一条刚
    # 重启后仍在推进的 run 判成"运行时丢了"，然后给用户一个不该出现的恢复入口
    # （2026-08-21 实测过这个形状的反面：正在跑的 run 被判死）。
    # 活动租约跨进程存活：worker 最近产出过真实进展，就还算它在动。
    _attempt = await db.scalar(
        select(RunAttempt)
        .where(RunAttempt.run_id == source_run.id)
        .order_by(RunAttempt.attempt_no.desc())
        .limit(1)
    ) if source_run else None
    # ⚠️ executionKernel 只能用来**排除**别的内核，不能用来要求它被写过。
    #
    # 这道门原来写的是 `== "formal_harness"`，而这个字段在很多 run 上根本没
    # 写过 —— 于是它们永远通不过，恢复入口对它们等于不存在。
    #
    # `mark_orphaned_harness_runs` 早就修过**同一个** bug（它的注释原话：
    # "executionKernel 在旧 run 上根本没写……于是它们永远通不过这道门"），改
    # 成了 `not in (None, "", "formal_harness") → 跳过`。但那份修复没走到这里
    # 来：同一个问题两份判据，只修了一份，另一份继续按老规矩拒人。
    #
    # 2026-08-22 实测代价：一条 writing run 的 worker 死在 `running` 上，平台
    # **连续见证了 6 次**（summary.runtimeWitness 6 条，条条写着
    # app_server_restart / observedStatus=running），6 次都没能按自己的见证放行
    # 恢复 —— 只因为 executionKernel 是 None。而 cancel 那一侧又正确地拒绝伪造
    # 一个它够不到的运行时的状态。两道闸各自都对，合起来把这条 run 永久焊死：
    # 论文早就编译好了，流程却再也走不动。
    kernel = summary.get("executionKernel")
    # `resumable is False` 曾是这里的一个合取项 —— 那是别处写下的**存储判决**，
    # 2026-08-24 起不再有人写它（它误伤过活 pause）。资格只看见证 + 现算。
    is_lost_formal_runtime = runtime_lost or bool(
        source_run
        and kernel in (None, "", "formal_harness")
        and summary.get("staleReason") == "app_server_restart"
        and (
            source_run.status == "stale_unknown"          # 迁移前的旧数据
            or _liveness.runtime_lost(
                source_run, has_live_binding=_has_live, attempt=_attempt)
        )
    )
    # ── 第三条判据：**没干完 + 没人在跑 = 可以接着跑** ─────────────────────
    #
    # 上面两条合起来仍然只覆盖"平台**没**发现它死了"的情形：
    #   · App Server 自己崩 → run 停在 stale_unknown → 认（第二条）
    #   · pause 活着但 attempt 关了 → 探针抛 stale → 认（第一条）
    #   · 子进程死掉、而 App Server **发现了** → run 记成 failed → **两条都不认**
    #
    # 于是出现一个反过来的结论：**平台越是及时发现故障，用户越是恢复不了。**
    # 实测（2026-08-10）：一个跑了 1 小时 15 分、已经 checkpoint 出 19 个产物的
    # 会话，子进程被杀之后点恢复得到 409 —— 那 19 个产物就搁浅在一条没人认领的
    # 分支上。
    #
    # 这个文件上面那段注释写着"恢复资格按'现在还能不能接着跑'判，不按'当初怎么
    # 死的'判"，方向是对的，但落地时又落回了状态名单（活性探针自己只扫
    # WAITING_HUMAN / STALE_UNKNOWN）。同一个"硬编码枚举"的毛病，深了一层。
    #
    # 真正的判据只有两问，都与死法无关：
    #   1) 现在有没有活着的运行时？（没有 → 不会有人跟它抢）
    #   2) 这个会话是不是**没走到干净的终点**？（是 → 还有活要接着干）
    # 资格判据只看 `latest_run` —— 这个会话**最新**的一个 run。这三个状态按定义
    # 就没人在跑，所以"有没有活运行时"不必再问一遍。（血缘锚点用的是另一条：
    # `lost_run`，见函数开头。两个问题两个答案。）
    #
    # 刻意**不**用进程内注册表（harness_session_manager）来判活：那份注册表是
    # per-process 的，uvicorn 一旦开成多 worker，另一个 worker 里活着的会话在这里
    # 会被看成"已死"，于是开出一条并行的接续会话 —— 两个驱动者写同一个 Git
    # 工作区。判据宁可保守。
    # ⚠️ `failed` **不等于**没有活运行时：harness 会话进程跨轮保活，上一轮失败
    # 之后它照样活着等下一条消息。只看 run 状态就放行，会在进程还在时多开一条
    # 接续会话（test_live_runtime_is_not_recoverable 当场抓到）。
    #
    # 所以这里必须问注册表。它是 per-process 的 —— 多 worker 下另一个 worker 里
    # 活着的会话在这里会被看成"已死"。两害相权：
    #   用它   → 多 worker 下**可能**多开一个分叉（每个 session 有自己的
    #            worktree，不会两个驱动者写同一处，代价是浪费与困惑）
    #   不用它 → 单 worker 下**必然**多开分叉（上一轮 failed 就触发）
    # 后者是现在就发生的错，所以用它。多 worker 时应改为共享事实（例如给
    # session 加一个带心跳的 runtime 租约），那是另一件事。
    from app.services.harness_sessions import harness_session_manager

    # 只在**探针没能给出结论**时才用这条兜底。探针回答的其实是"那个 pause 还
    # 接得上吗"（见它的 docstring），不是通用的"还活着吗" —— 所以它对 failed /
    # incomplete 这类 run 一言不发。这条补的正是那段空白。
    #
    # 但探针**明确说了话**时以它为准：
    #   · 它抛 stale  → 上面第一条已经放行
    #   · 它正常返回  → 说明它认得这个状态且判定还接得上 → 不许再被这条覆盖
    #   · 它自己炸了  → fail-closed，同样不许被这条覆盖
    if probe_was_inconclusive and (
        source_run
        and source_run.status in {s.value for s in UNFINISHED_RUN_STATUSES}
        and not harness_session_manager.has_live_runtime(
            str(project.id), source_session_id
        )
    ):
        is_lost_formal_runtime = True
    if not is_lost_formal_runtime:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "session_not_recoverable",
                "message": "Only a stale Session whose Harness runtime was lost can be recovered",
            },
        )
    if not source.git_base_commit_sha:
        # 恢复出来的会话要从**同一个基线**分出去。源会话连 git 基线都没有，
        # 说明它从没真正建起工作区 —— 接着它跑没有意义。
        # （从前这一条问的是 base_revision_id：同一件事的第二份记法。）
        raise HTTPException(
            status_code=409,
            detail={
                "code": "recovery_context_incomplete",
                "message": "The source Session has no frozen Git baseline",
            },
        )
    if source.model_backend_id:
        # A different Project member must not inherit a user-private credential
        # merely because they can view the stale Session.
        await get_visible_backend(db, user, source.model_backend_id)

    existing = await db.scalar(
        select(SessionProjection).where(
            SessionProjection.tenant_id == source.tenant_id,
            SessionProjection.recovered_from_session_id == source.session_id,
        )
    )
    if existing:
        return existing, lost_run, False

    now = _now()
    recovered = SessionProjection(
        tenant_id=source.tenant_id,
        workspace_id=source.workspace_id,
        project_id=source.project_id,
        session_id=str(uuid4()),
        initiating_user_id=user.id,
        title=f"{source.title} · recovered"[:300],
        summary="Safe continuation after the previous Harness runtime was lost.",
        created_by_user_id=user.id,
        lifecycle_status="active",
        recovered_from_session_id=source.session_id,
        recovery_source_run_id=lost_run.id,
        model_backend_id=source.model_backend_id,
        policy_snapshot_id=source.policy_snapshot_id,
        research_settings_snapshot_id=source.research_settings_snapshot_id,
        research_settings_snapshot=deepcopy(source.research_settings_snapshot),
        platform_context_snapshot=deepcopy(source.platform_context_snapshot),
        knowledge_read_watermark=deepcopy(source.knowledge_read_watermark),
    )
    db.add(recovered)
    await db.flush()
    from app.services.project_repository import get_project_repository, run_in_repository_thread

    repository = get_project_repository()
    repository_status = await run_in_repository_thread(
        repository.initialize_project,
        project_id=str(project.id),
        name=project.name,
        description=project.description,
        research_domain=project.research_domain,
        owner_id=project.owner_id,
    )
    project_base_commit = repository_status.head_commit
    # ── 从**死会话的 head** 开分支，不是从项目基线 ─────────────────────────
    #
    # 原来这里用的是 base_revision（源会话当初的起点）。后果：一个跑了几小时、
    # 已经 checkpoint 出十几个产物的会话，运行时一死，接续会话从**零**开始 ——
    # 那些提交还在源分支上，但没有任何人再读它们。
    #
    # 而 checkpoint 机制的原话是"让有用的半成品、blocker 报告、失败的尝试留在
    # Session 分支上，而不是被隐藏或删除"。存了，而唯一的续跑入口不读 ——
    # 又一次"机制存在但没接到路径"，且是最贵的一次：丢的是几小时的真实研究。
    #
    # 接续会话按定义是**同一份工作的继续**，不是项目的另一条分支，所以它的起点
    # 就该是源会话停下的地方。源分支保持原样不动（证据留痕），新分支从它长出去。
    source_head = (source.git_head_commit_sha or "").strip()
    base_commit = source_head or project_base_commit
    workspace = await run_in_repository_thread(
        repository.ensure_session_workspace,
        project_id=str(project.id),
        session_id=recovered.session_id,
        base_commit=base_commit,
        title=recovered.title,
        created_by=user.id,
    )
    recovered.git_branch = workspace.branch
    recovered.git_base_commit_sha = workspace.base_commit
    recovered.git_head_commit_sha = workspace.head_commit
    recovered.git_worktree_path = workspace.path
    await db.flush()
    await db.refresh(recovered)
    return recovered, lost_run, True


async def current_driver_id(db: AsyncSession, session: SessionProjection) -> str | None:
    """谁在开这个会话 —— 现算，不存。

    ## 为什么不是一个字段（2026-09-05 删租约）

    从前它是 `sessions.primary_driver_user_id` + `driver_lease_until`：拿、续、
    放、到期四个动作，三个端点，一台状态机。它要防的是「两个人同时对一个会话
    动手」，可那件事真正被挡住的地方在跑轮那一刻（会话被占用），不在这台
    状态机上。个人档更是从头到尾只有一个人 —— 一台永远只有一个参与者的
    状态机，剩下的只有它自己出错的可能（租约到期没人续、转移给已退出的成员、
    成员删除被一条过期租约挡住）。

    现算的答案是「最近一条用户消息的作者」：它不会过期、不需要维护、也不可能
    和事实分叉。没有人说过话就退回发起者、创建者。
    """
    latest = await db.scalar(
        select(SessionMessage.actor_user_id)
        .where(
            SessionMessage.session_id == session.session_id,
            SessionMessage.role == "user",
            SessionMessage.actor_user_id.is_not(None),
        )
        .order_by(SessionMessage.sequence.desc())
        .limit(1)
    )
    return latest or session.initiating_user_id or session.created_by_user_id


async def require_drive_access(
    db: AsyncSession,
    *,
    user: User,
    project: Project,
    session_id: str,
) -> SessionProjection:
    if not await has_project_capability(db, user, project, "drive_session"):
        raise HTTPException(status_code=403, detail="Missing project capability: drive_session")
    session = await db.scalar(
        select(SessionProjection)
        .where(
            SessionProjection.session_id == session_id,
            SessionProjection.project_id == str(project.id),
        )
        .with_for_update()
    )
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if session.lifecycle_status != "active":
        raise HTTPException(
            status_code=409, detail="Archived or completed Session cannot be driven"
        )
    return session


async def allocate_session_sequence(
    db: AsyncSession, session: SessionProjection
) -> int:
    """从会话**唯一**的序号发生器领一个号。

    消息和执行事件都走这里（事件侧的入口是
    `execution_ingest.EventIngestor._next_sequence`，同一条 SQL）。号在两者之间
    是全局递增且互不重复的 —— 那正是"这段活动发生在哪两条消息之间"这个问题
    有答案的前提。

    返回的是**刚分配出去的那个号**（`RETURNING` 拿的是自增后的值），所以
    `sessions.next_sequence` 的语义是"最后发出去的号"。
    """
    value = await db.scalar(
        update(SessionProjection)
        .where(SessionProjection.session_id == session.session_id)
        .values(next_sequence=SessionProjection.next_sequence + 1)
        .returning(SessionProjection.next_sequence)
        .execution_options(synchronize_session=False)
    )
    if value is None:  # 行不见了 —— 与其发一个猜的号，不如当场吵
        raise HTTPException(status_code=404, detail="Session not found")
    # 让内存里的实例跟上真值，但不标记为脏：真相在库里，这里只是抄一份给同一
    # 事务里后续代码看，不能反过来把它写回去。
    set_committed_value(session, "next_sequence", int(value))
    return int(value)


async def append_session_message(
    db: AsyncSession,
    *,
    session_id: str,
    role: str,
    content: str,
    actor_user_id: str | None,
    command_id: str | None = None,
    run_id: str | None = None,
    offer_id: str | None = None,
) -> SessionMessage:
    if command_id:
        existing = await db.scalar(
            select(SessionMessage).where(
                SessionMessage.command_id == command_id,
                SessionMessage.role == role,
            )
        )
        if existing:
            return existing
    session = await db.scalar(
        select(SessionProjection)
        .where(SessionProjection.session_id == session_id)
        .with_for_update()
    )
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    # 序号由**数据库**自己加，不在内存里加，而且全会话只有**一个**发生器。
    #
    # 上面的 `with_for_update()` 确实在库里锁了这一行，但 SQLAlchemy 对已经在
    # identity map 里的对象不会用 SELECT 回来的列值覆盖已加载的属性，而
    # `expire_on_commit=False`（`app/database.py:43`）让对象 commit 之后继续留在
    # map 里。于是**锁是数据库的锁，加的却是内存里的值** —— 两件事被当成了一件。
    # `UPDATE … SET n = n + 1 RETURNING n` 一条语句完成加锁、自增、读回，中间没有
    # 内存副本可以走岔（同一份实现在 `execution_ingest._next_sequence`）。
    #
    # 这个病 2026-08-11 在**事件**序号上确诊过一次（会话 bc1c7343 撞
    # `uq_events_sequence`，实测 8 次并发分配发出 [1,2,2,2,2,2,2,3]），修法只修了
    # 一个计数器，同一种写法在**消息**序号上原样留着，2026-08-20 会话 4adbea62
    # 又栽一次。而"两处同样的写法"其实是"两个计数器"这个更深问题的症状 ——
    # 现在两者领的是同一个号（见 `SessionProjection.next_sequence`）。
    next_sequence = await allocate_session_sequence(db, session)
    message = SessionMessage(
        session_id=session_id,
        sequence=next_sequence,
        actor_user_id=actor_user_id,
        role=role,
        content=content,
        command_id=command_id,
        run_id=run_id,
        offer_id=offer_id,
    )
    db.add(message)
    await db.flush()
    return message


# 会话"在不在忙"的判据：**顶层** run 的未终态。子节点 run 的状态永远停在
# queued（生命周期由父 run 驱动），拿它们判忙会恒为真 —— 见
# tests/test_a_child_run_never_blocks_the_session.py 记录的那次永久卡死。
ACTIVE_OWNING_RUN_STATUSES = (
    "queued",
    "dispatching",
    "running",
    "waiting_compute",
    "retrying",
)


async def active_owning_run(db: AsyncSession, *, session: SessionProjection) -> Run | None:
    return await db.scalar(
        select(Run)
        .where(
            Run.tenant_id == session.tenant_id,
            Run.project_id == session.project_id,
            Run.session_id == session.session_id,
            Run.parent_run_id.is_(None),
            Run.status.in_(list(ACTIVE_OWNING_RUN_STATUSES)),
        )
        .order_by(Run.created_at.desc())
    )


async def interject_active_session(
    db: AsyncSession,
    *,
    session: SessionProjection,
    text: str,
    author_user_id: str,
    run_id: str | None,
) -> tuple[str, str]:
    """跑轮中插话的**唯一**落地路径（chat 入口分流与 /interrupt 端点共用）。

    两步，顺序有讲究：

    1. 先落 user 消息行 —— 插话是对话的一部分，必须进记录。2026-08-17 实测：
       原来只投收件箱，用户问"跑的怎么样了"，收件箱文件被取走后没有任何
       痕迹（不成消息、无回应事件），像对着空气说话。
    2. 再送给 worker —— 它跑同一份决策轮逻辑（问进度就查进度答复 / 调方向
       就 inject / 要停就 cancel）。

    ## 投递面：从文件收件箱换成 socket（P1-2）

    收件箱是**补偿机制**，补的是"协议不能多路复用"这件事（原模块的文档里
    写得很清楚）。多路复用做完了（P1-1），话经 socket 直达 worker 进程，
    收件箱因此退休 —— 语义一个字不变：stop 仍机械直写 kill_signal 不过模型，
    message 仍进 `injected_messages` 由 agent_loop 开轮机械 drain。

    换来的两件事：投递**当场有回执**（worker 说它收下了、当下忙不忙），
    以及话不再要等最长 1 秒的轮询周期。

    返回 (item_id, message_id, occupancy)。`occupancy` 是 **worker 自己**报的
    当下状态（working / idle）—— 回执要说的第二句话（"收下了，但它前面还排着
    活"）必须来自现场，不能由调用方按自己的判据造一句。

    文本为空 / 没有活 worker 抛 ValueError。
    """
    from app.services.harness_sessions import harness_session_manager

    clean = (text or "").strip()
    if not clean:
        raise ValueError("interject text must not be empty")
    message = await append_session_message(
        db,
        session_id=str(session.session_id),
        role="user",
        content=clean,
        actor_user_id=author_user_id,
        run_id=run_id,
    )
    # message_id 随投递件走：worker 的回执和答复靠它锚回这条消息 —— 否则答复
    # 只能挂进 run 的活动窗口，渲染在提问上面（2026-08-18 实测）。
    receipt = await harness_session_manager.deliver(
        project_id=str(session.project_id),
        session_id=str(session.session_id),
        kind="message",
        text=clean,
        author=author_user_id,
        message_id=str(message.id),
    )
    # 连同**这条消息的 id** 一起交出去：回执要锚回它，否则事后对不上
    # 「平台那句话是在回应谁」（决策呈递那次的教训 —— 编一个 id 当身份，
    # 两边就再也对不上了）。
    return (
        str(receipt.get("item_id") or ""),
        str(message.id),
        str(receipt.get("occupancy") or ""),
    )


async def _pending_approval(db: AsyncSession, *, run: Run | None) -> dict | None:
    """这个 run 此刻在等人回答什么 —— **现算，不落第二份**。

    ## 为什么是推导而不是存一列

    "待审批"是一个由事件序推出来的事实：最近一条 `run.paused` 之后没有
    `run.resumed`。存成一列就是第二个真相源，而它一定会和事件日志分叉 ——
    分叉时两边都不报错，且没人清得干净（谁在 resume 的每条路径上都记得清空
    这一列？）。推导版本自动随 resume 消失。

    ## 这里在补的洞（2026-08-10，静默死锁 2 小时）

    harness 早就把完整问题送进来了：prompt、options、optionDetails（带
    description 和 recommended）、askingNodeType，连逐字的待执行命令都在
    `context` 里。这些**一直存在 execution_events 表里**。丢掉它们的是
    `_apply_event` —— 它只取 `reason` 改了 run.status，其余全扔；于是 API
    不暴露、UI 画不出来，人对着一个 `waiting_permission` 的转圈无从下手。

    证据不缺，是取证据的那一步没人写。
    """
    if run is None or run.status not in AWAITING_HUMAN_RUN_STATUSES:
        return None
    # ── 按**平台自己的单调序**排，不按墙钟（#887）──────────────────────────
    #
    # `sequence` 是会话级自增计数器，在摄取那一刻由 `UPDATE … RETURNING` 原子分配，
    # 库里有唯一约束 —— 它就是这条事件流的因果次序。`occurred_at` 是 worker 写
    # transcript 时的墙钟：父子两份 transcript 各由一个进程写，应用重启后还会从
    # 字节偏移重新接着读。拿它当主序，"哪条最新"就成了一个依赖运行环境的答案，
    # 而它答错的样子正是 #887 —— **已经批准过的第一张权限卡又被摆回人面前**。
    #
    # occurred_at 留作并列时的次序（同一时刻落的几条归并要稳定）。
    recent = list(
        (
            await db.scalars(
                select(ExecutionEvent)
                .where(
                    ExecutionEvent.run_id == run.id,
                    ExecutionEvent.kind.in_(("run.paused", "run.resumed")),
                )
                .order_by(
                    ExecutionEvent.sequence.desc(), ExecutionEvent.occurred_at.desc()
                )
                .limit(8)
            )
        ).all()
    )
    # 最新的是 resumed → 已经答过了，状态还没追上（事件先到、状态后写）。
    # 宁可少报一次也不要报一个已经答过的问题：后者会让人重复批准。
    if not recent or recent[0].kind != "run.paused":
        return None

    # ── 同一次暂停会落好几条 `run.paused`，把它们**归并**成一份 ─────────────
    #
    # 一个暂停时刻，ingest 侧有三个分支会各落一条，几十毫秒之内：
    #
    #   human_input_requested  {"reason", "prompt", "detailsUnavailable": true}
    #   loop_pause             {"reason", "prompt"}                  ← agent_loop 冒泡的简版
    #   run_paused             {"reason", "prompt", "context", "options",
    #                           "offer": {...}, "pauseKind", "resumable", …}
    #
    # 这里原来写的是 `max(open_pauses, key=lambda e: len(payload))` ——
    # **拿"哪份抄件字段多"当权威**。那不是判据，那是分叉的自白：它默认"字段多的
    # 那份一定是对的"，也默认三份说的是同一件事却从不检查。读早了（rich 那条还
    # 没 ingest）就拿到空的，人看到一个没有命令、没有选项的审批框 ——
    # 2026-08-11 实测拿到过 `选项: []` / `提问节点: None`。
    #
    # 现在按**呈递身份**归并：三条讲的是同一次呈递，那就把它们合成一份，
    # 后到的补齐先到的。谁字段多不再是判据，"它们是不是同一次呈递"才是。
    #
    # 只在**同一个未答复的暂停**内归并（碰到 resumed 就停），不会把上一次的
    # 问题翻出来；一旦窗口里出现了**两个不同的 offer_id**，说明呈递已经换了
    # 一次，只认最新那一次的那几条。
    open_pauses = []
    for item in recent:
        if item.kind != "run.paused":
            break
        open_pauses.append(item)

    def _offer_id_of(payload: dict) -> str | None:
        offer = payload.get(PAUSE_OFFER_KEY)
        if isinstance(offer, dict) and offer.get("offer_id"):
            return str(offer["offer_id"])
        # 老事件把身份平铺在外层；两种形状都认，但**只认这两处**，不再猜。
        for key in ("offer_id", "offerId"):
            if payload.get(key):
                return str(payload[key])
        return None

    newest_offer_id = next(
        (oid for oid in (_offer_id_of(dict(e.payload or {})) for e in open_pauses) if oid),
        None,
    )
    if newest_offer_id is not None:
        open_pauses = [
            e for e in open_pauses
            if _offer_id_of(dict(e.payload or {})) in (newest_offer_id, None)
        ]

    #: 这几个键说的是**那一条事件自己**的处境，不是那次呈递的处境。归并之后
    #: 它们会张冠李戴：`detailsUnavailable` 出自最简的那条，而合起来的这份
    #: 恰恰是有细节的。
    _PER_EVENT_KEYS = {"detailsUnavailable", "submissionId", "stepId"}
    # ── 子 run 已经走完了，父这边那张卡就不该还在（#887 第 2/3 条）──────────
    #
    # 父 run 的 `run.paused` 有一支是从 `subagent_call_paused` 投影来的：子节点停下
    # 问人，父这边跟着显示成"在等人"。它带着 `stepId`，而**那一步的结束由子
    # transcript 自己声明**（`step.completed` / `step.failed`，同一个 stepId）。
    #
    # 此前这里只看本 run 自己有没有 `run.resumed`。子 run 恢复并跑完、而父这边那条
    # resume 因为应用重启丢了的时候，这张卡就一直挂着 —— 人再点一次，批准的是一个
    # 早就过去的暂停（#887 现场：父 run 重放第一张权限卡，而子 run 两次批准都已生效）。
    #
    # 判据是**那一步结没结束**，不是"过了多久"：步骤结束事件是durable 的，重启也在。
    _step_ids = {
        str(dict(item.payload or {}).get("stepId") or "")
        for item in open_pauses
    } - {""}
    if _step_ids:
        _later_steps = list(
            (
                await db.scalars(
                    select(ExecutionEvent)
                    .where(
                        ExecutionEvent.session_id == run.session_id,
                        ExecutionEvent.kind.in_(("step.completed", "step.failed")),
                        ExecutionEvent.sequence > int(open_pauses[0].sequence or 0),
                    )
                    .order_by(ExecutionEvent.sequence.desc())
                    .limit(50)
                )
            ).all()
        )
        if any(str(dict(e.payload or {}).get("stepId") or "") in _step_ids
               for e in _later_steps):
            return None

    payload: dict = {}
    for item in reversed(open_pauses):  # 旧 → 新：后到的补齐、覆盖先到的
        for key, value in dict(item.payload or {}).items():
            if key in _PER_EVENT_KEYS:
                continue
            # 空值不许覆盖已有的实值 —— 削过字段的那条不该把完整的那条盖掉。
            if value in (None, "", [], {}):
                continue
            payload[key] = value
    event = open_pauses[0]
    reason = payload.get("reason") or run.status
    return {
        "runId": run.id,
        "reason": reason,
        # ── 这次呈递是哪一类，**由后端说**（2026-09-01）────────────────────
        #
        # 前端此前是拿 `reason == "waiting_permission"` 去猜的 —— 那是 run
        # 状态词表的一个取值，于是"前端不该有 run 状态词表"这条纪律在这里
        # 破了个口子；而破口处的判据漏了一支（decision_package），审批卡就会
        # 按普通提问渲染，把逐字命令折叠起来没人看见。
        "kind": (
            "permission"
            if reason == RunStatus.WAITING_PERMISSION.value
            or payload.get("pauseKind") in {"highrisk_confirm", "permission"}
            else "decision"
            if payload.get("pauseKind") in {"decision_package", "decision"}
            else "human_input"
        ),
        "prompt": payload.get("prompt"),
        # `context` 里是逐字的待执行内容（命令、workdir、命中类别）。审批一个
        # 看不见内容的高危操作没有意义 —— 这一段必须原样送到人面前。
        "context": payload.get("context"),
        "options": payload.get("options") or [],
        "optionDetails": payload.get("optionDetails") or [],
        "recommendedOptionIndex": payload.get("recommendedOptionIndex"),
        # 这一次呈递整份带上（含 choice id / offer_id / decision_id / facts）。
        # 上面那两个是既有前端读的兼容视图，取自同一份呈递。
        PAUSE_OFFER_KEY: payload.get(PAUSE_OFFER_KEY),
        "askingNodeType": payload.get("askingNodeType"),
        "pauseKind": payload.get("pauseKind"),
        "askedAt": event.occurred_at,
    }



def _iso_or_none(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


class DriveAccess(NamedTuple):
    """「这个人此刻能不能驱动这个会话」—— **一个答案**，后端算，前端不重算。

    此前这是两半：后端 `may_drive` 只看能力与归档，前端 `sessionComposerAccess`
    另外看驾驶权租约。两半的交集才是真的"能不能"，而没有任何一层持有那个交集
    —— 于是 `canSend=true` 与前端灰着的输入框可以同时成立，反过来也可以。

    `until` 是这个判断**会自己过期**的时刻（别人的驾驶权租约到点）。前端拿它
    去安排一次重新查询，而不是自己把租约再算一遍 —— 客户端可以决定"什么时候
    再问一次"，不可以决定"答案是什么"。
    """

    may_drive: bool
    reason: str | None = None
    until: datetime | None = None


async def drive_access_for(
    db: AsyncSession, *, user: User, project: Project, session: SessionProjection
) -> DriveAccess:
    """按拒绝的**结构性**程度从强到弱地判：归档 → 无能力 → 别人正在开。

    顺序是有意的：一个归档会话不该显示"等 X 释放驾驶权"，那会让人去等一件
    永远不会改变局面的事。
    """
    if session.archived_at is not None or session.lifecycle_status == "archived":
        return DriveAccess(False, "Archived sessions are read-only.")
    # 问能力源本身，不比对**别名**列表（api_names=True 会把 `drive_session`
    # 改写成 `drive`，比对内部名就永远为假，而那种错法没有任何红色）。
    if not await has_project_capability(db, user, project, "drive_session"):
        return DriveAccess(False, "You can view this Session, but cannot drive it.")
    # 「别人正在开」这一条随租约一起删了（2026-09-05）。有能力就能开：说话的
    # 那个人就成为当前驾驶者（current_driver_id 现算）。真正会互相踩到的是
    # **同一轮在飞时另一个人再发一条**，那由占用判据在跑轮那一刻挡住 ——
    # 它看得见现场，一台会过期的租约看不见。
    return DriveAccess(True)


async def session_execution_view(
    db: AsyncSession, *, user: User, project: Project, session: SessionProjection
) -> tuple[Run | None, str, dict]:
    """**会话局面的唯一生产者**。返回 (当前这一轮, 原始状态投影, view)。

    抽成一个函数是这次修复的一半：局面此前由 REST payload 自己拼一遍，而流式
    终帧（`done`）另发一套 `status` / `resumable` / `pause` 三个平铺字段 ——
    同一件事两种形状，前端于是被迫写两套解析、两套判据（`chat.terminal` 与
    `session.execution`），再写第三段代码决定谁赢。三份会各自演化，而分叉时
    没有任何一层报错。

    现在两条路送的是**同一个对象**。前端只认一种形状，也就没有"谁赢"这个问题。
    """
    # ── "会话在等人吗" 只能问**顶层** run（2026-08-22）──────────────────────
    #
    # 会话级待审批的语义是"这个会话此刻卡在一个问题上，不回答就不往下走"。
    # 能卡住会话的只有顶层 run —— 它是会话的当前一轮。子节点 run 是那一轮
    # **内部**的步骤，它的暂停由父 run 自己消化。
    #
    # 这里原来传 `latest_run`（全会话按 updated_at 最新的一条，含子 run）。
    # 现场：顶层 run 正跑着新一轮，而上一轮遗留的子 run 停在 waiting_human
    # 且刚好是最新更新的那条 —— 于是会话级 pendingApproval 拿到了它的旧提问，
    # UI 上「进行中」和「等你输入」同屏并存，人点了也没人听（那个 worker 早
    # 没了）。实测：同一时刻按 latest_run 判有卡片、按顶层 run 判没有。
    #
    # "最新更新的" 和 "当前这一轮" 是两个问题。前者答不了后者。
    #
    # 第一次修只把子 run 排除掉了，判据仍是 `updated_at` —— 于是同一个 bug 在
    # **顶层与顶层之间**原样复现。2026-08-23 会话 e46448f0 现场：
    #
    #   run_cedea504…  waiting_human  updated_at 14:20:58.342   ← 12 分钟前答过了
    #   run_378b00c9…  running        updated_at 14:20:58.240   ← 真正在跑的这一轮
    #
    # 那条被顶掉的旧 run 每次被触碰都比在跑的那条晚零点几秒，于是它稳定地赢下
    # "最新更新"，会话级 pendingApproval 一直拿它那个已经答过的提问 —— 连续
    # 模式下用户面前反复复活同一张决策卡，输入框还被锁成"先回答上面的问题"。
    #
    # `updated_at` 回答的是"谁最近被写过"，它会被活性回填、租约续期、任何一次
    # 触碰改写。"当前这一轮"只能问**谁最后开始** —— `started_at` 在 run.started
    # 时写一次，此后没有任何路径会改它。
    current_run = await db.scalar(
        select(Run)
        .where(Run.session_id == session.session_id, Run.parent_run_id.is_(None))
        .order_by(Run.started_at.desc().nullslast(), Run.id.desc())
        .limit(1)
    )
    observed = await _liveness.observed_status_map(db, [current_run] if current_run else [])
    pending_approval = await _pending_approval(db, run=current_run)
    drive_access = await drive_access_for(db, user=user, project=project, session=session)
    view = _view.build_session_view(
        current_run,
        lang=_language_for(user),
        observed_status=(observed.get(current_run.id) if current_run else None),
        # ── 卡片本体随局面一起下发（2026-09-01）──────────────────────────
        #
        # 它此前是 payload 顶层一个**平级**的 `pendingApproval`，与 `canSend`
        # 各走各的。两个字段回答同一个问题的两半，中间没有任何东西保证它们
        # 说得一致 —— 而它们确实不一致过 6 小时（见 execution_view 模块头）。
        # 现在卡片装在 `answer` 里：说了走卡片就一定带着卡片。
        pause=pending_approval,
        failure=(current_run.summary or {}).get("failure")
        if current_run and isinstance(current_run.summary, dict)
        else None,
        live_runtime=_view.has_live_runtime(session.project_id, session.session_id),
        may_drive=drive_access.may_drive,
        readonly_reason=drive_access.reason,
        readonly_until=_iso_or_none(drive_access.until),
    )
    # 呈现的是**现算**的状态，不是库里那一行（2026-08-23）：D11 之后没人再把
    # `run.status` 改成 stale_unknown，原样透传 = 后端每重启一次就多一条永远
    # 「运行中」的会话，而停止按钮同时回 409。见 `run_liveness.observed_status`。
    state = observed.get(current_run.id, current_run.status) if current_run else "idle"
    return current_run, state, view


async def _reap_orphaned_runs_of(
    db: AsyncSession, *, project_id: str, session_id: str
) -> None:
    """把本会话里"需要活体进程、却没有活体进程"的 run 收成 stale_unknown。

    只做**这一个会话**的对账，不扫全库：读会话状态是高频路径，而无主是稀有
    事件 —— 绝大多数调用一条都不会改。

    观察永不打断主流程：对账失败就照旧返回当前状态（会退回旧行为，即"要等
    下次重启"），不会让一次 GET 挂掉。
    """
    try:
        from app.services.harness_sessions import mark_orphaned_harness_runs
        from app.models.execution import REQUIRES_LIVE_RUNTIME_STATUSES

        rows = (
            await db.execute(
                select(Run.id).where(
                    Run.project_id == project_id,
                    Run.session_id == session_id,
                    Run.status.in_(REQUIRES_LIVE_RUNTIME_STATUSES),
                )
            )
        ).scalars().all()
        if not rows:
            return
        await mark_orphaned_harness_runs(db, run_ids=set(rows))
    except Exception:
        logger.exception("Could not reconcile orphaned Runs for session %s", session_id)


async def latest_context_window(db: AsyncSession, *, session_id: str) -> dict | None:
    """这个会话的调度器上一次请求把窗口占到了哪里。

    只认**顶层** run 的报告（`parent_run_id IS NULL`）：子节点各有各的
    context，它们的占用说的不是"这个会话"。事件本身由 harness 每次 LLM
    响应后自报（`context.updated`），这里不算，只取最新的那一条 ——
    一个问题一个出处。
    """
    row = await db.scalar(
        select(ExecutionEvent)
        .where(
            ExecutionEvent.session_id == session_id,
            ExecutionEvent.kind == "context.updated",
            ExecutionEvent.parent_run_id.is_(None),
        )
        .order_by(ExecutionEvent.occurred_at.desc(), ExecutionEvent.sequence.desc())
        .limit(1)
    )
    if row is None:
        return None
    return {**dict(row.payload or {}), "at": row.occurred_at, "runId": row.run_id}


async def session_response(
    db: AsyncSession, *, user: User, project: Project, session: SessionProjection
) -> dict:
    # ── 无主的 run 在这里现算，别等下次重启（2026-08-11）────────────────────
    #
    # 判据（`live_binding`：这个状态需要活体进程，注册表里有没有它）在任何
    # 时刻都成立，此前却只在 lifespan 启动时求值过一次 —— 而孤儿的成因不是
    # 后端重启。实测代价：harness 子进程死了，后端一直活着，DB 里那条 run
    # `running` 了一小时四十分钟；UI 上看着在干活，会话级锁占着，没人发现。
    #
    # 放在这里是因为这里正是"把会话状态讲给外面听"的收口 —— 谎就是在这一层
    # 讲出去的。作用域限本会话，绝大多数情况下一条都不会改。
    await _reap_orphaned_runs_of(
        db, project_id=str(project.id), session_id=session.session_id
    )
    await db.refresh(session)
    creator = await db.get(User, session.created_by_user_id) if session.created_by_user_id else None
    driver_id = await current_driver_id(db, session)
    driver = await db.get(User, driver_id) if driver_id else None
    backend = (
        await db.get(ModelBackendConfig, session.model_backend_id)
        if session.model_backend_id
        else None
    )
    run_stats = (
        await db.execute(
            select(
                func.count(Run.id),
                func.coalesce(func.sum(Run.total_tokens), 0),
                func.sum(Run.cost),
                func.coalesce(func.sum(Run.retry_count), 0),
                func.count(Run.id).filter(Run.usage_coverage == "complete"),
                # 成本未知的 run 有几条。SQL 的 SUM **跳过 NULL** —— 29 个 run
                # 里 28 个 cost 是 NULL（真模型没配价格）、1 个是 demo stub 的
                # 0.000000，SUM 出来就是 0.000000，界面于是说"这个会话花了 0 块"。
                # 那不是"零"，是"不知道"。（2026-08-31 实测：5900 万 token 的
                # 会话显示 US$0.00。）
                func.count(Run.id).filter(
                    Run.cost.is_(None), Run.total_tokens > 0
                ),
            ).where(Run.session_id == session.session_id)
        )
    ).one()
    latest_run = await db.scalar(
        select(Run)
        .where(Run.session_id == session.session_id)
        .order_by(Run.updated_at.desc(), Run.id.desc())
        .limit(1)
    )
    current_run, execution_state, execution_view = await session_execution_view(
        db, user=user, project=project, session=session
    )
    # 「有多少没发布的改动」「有没有冲突」现算自 git（RFC X1）。从前它们是
    # change_items / merge_conflicts 两张表的计数 —— 而那两张表是 git 的投影，
    # 投影和事实分叉时不报错，只是把一个错的数字画在界面上。
    from app.services.session_changes import read_session_changes

    changes = await read_session_changes(str(project.id), session)
    unpublished = changes.files_changed
    conflicts = len(changes.conflicting_paths)
    run_count = int(run_stats[0] or 0)
    # 有任何一条**真跑过**的 run 成本未知，整个会话的成本就是未知 ——
    # 报一个把未知当零加出来的数，比不报更糟：它看起来像事实。
    # 前端已经会把 null 渲染成 "cost unavailable"（SessionIndex.tsx），
    # 这里只要别替它编一个数出来。
    unknown_cost_runs = int(run_stats[5] or 0)
    cost_value = None if unknown_cost_runs else run_stats[2]
    return {
        "id": session.session_id,
        "projectId": session.project_id,
        "title": session.title,
        "summary": session.summary,
        "lifecycleStatus": session.lifecycle_status,
        "recoveredFromSessionId": session.recovered_from_session_id,
        "recoverySourceRunId": session.recovery_source_run_id,
        "createdByUserId": session.created_by_user_id,
        "primaryDriverUserId": driver_id,
        "modelBackendId": session.model_backend_id,
        "researchSettingsSnapshotId": session.research_settings_snapshot_id,
        "researchSettingsContextHash": (
            session.research_settings_snapshot.get("context_hash")
            if isinstance(session.research_settings_snapshot, dict)
            else None
        ),
        "creator": ({"id": creator.id, "displayName": creator.display_name} if creator else None),
        "primaryDriver": (
            {"id": driver.id, "displayName": driver.display_name} if driver else None
        ),
        "primaryDriverName": driver.display_name if driver else None,
        "createdByName": creator.display_name if creator else None,
        # 版本 = git 提交。界面上那个自增的 r3 随第二份版本账一起走了 ——
        # 取代它的是一个**能拿去 git show 的**短 sha。
        "baseCommitSha": changes.base_commit,
        "headCommitSha": changes.head_commit,
        "projectAdvanced": changes.behind_by > 0,
        "behindBy": changes.behind_by,
        "aheadBy": changes.ahead_by,
        "gitBranch": session.git_branch,
        "gitBaseCommitSha": session.git_base_commit_sha,
        "gitHeadCommitSha": session.git_head_commit_sha,
        "modelBackend": (
            {
                "id": backend.id,
                "displayName": backend.display_name,
                "provider": backend.provider,
                "model": backend.model,
            }
            if backend
            else None
        ),
        "modelBackendName": backend.display_name if backend else None,
        "effectiveCapabilities": await effective_capabilities(db, user, project, api_names=True),
        "capabilities": await effective_capabilities(db, user, project, api_names=True),
        # 同上：会话状态 = **当前这一轮**的状态，不是"最新被更新的那条 run"。
        # 子 run 的 updated_at 天然晚于父 run，用 latest_run 会让会话顶部标签
        # 显示成子节点的状态（例如上一轮遗留子 run 的 "Needs input"）。
        # ── 呈现的是**现算**的状态，不是库里那一行（2026-08-23）─────────────
        #
        # D11 之后没人再把 `run.status` 改成 stale_unknown（判决不落盘）。于是
        # 这里原样透传 = 后端每重启一次就多一条永远「运行中」的会话，而停止
        # 按钮同时回 409「没有当前轮可停」。见 `run_liveness.observed_status`。
        "executionState": execution_state,
        # ── 前端唯一该读的那个答案 ──────────────────────────────────────────
        #
        # 上面那个 executionState 是 13 值枚举的原始投影，留给还没迁完的读点；
        # `executionView` 是同一份事实的**成品**：三态 + 在等什么 + 结局 +
        # 能不能停 / 能不能发。前端从此不再自己从 runs 列表推导 —— 那条路
        # 在 2026-08-27 让「Queued + 转圈 + 停不掉的停止按钮」三个说法同屏并存。
        "executionView": execution_view,
        "runCount": run_count,
        # 上传上限由后端说，前端照它做选文件时的预检 —— 一个数字一处出处。
        # 前端自己写一个常量的话，调上限那天它会继续按老数字放行，然后用户
        # 传完 3 GiB 才收到 413。
        "materialMaxBytes": settings.material_max_bytes,
        "unpublishedChangeCount": int(unpublished),
        "conflictCount": int(conflicts),
        "usage": {
            "totalTokens": int(run_stats[1] or 0),
            "cost": float(cost_value) if isinstance(cost_value, Decimal) else cost_value,
            "currency": "USD" if cost_value is not None else None,
            "coverage": "complete"
            if run_count and int(run_stats[4] or 0) == run_count
            else "partial",
        },
        # 上一次请求的窗口占用（顶层 run 的 `context.updated` 最新一条）。
        # 在飞的那一轮由事件流实时补，这里给的是"此刻已知的最新"。
        "contextWindow": await latest_context_window(db, session_id=session.session_id),
        "retryCount": int(run_stats[3] or 0),
        "createdAt": session.created_at,
        "updatedAt": session.updated_at,
        "archivedAt": session.archived_at,
    }

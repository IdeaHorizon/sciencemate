"""Chat harness endpoints with persistent conversations.

Two scopes are intentionally separate:
- Global chat: sees a compact list of the user's projects and can open a new
  project with a single starting research node.  No previous-conversation
  context is injected (clean slate).
- Project chat: sees only the selected project's graph/scheduling context.
  When a conversation_id is provided, summaries from up to 3 previous
  conversations in the same project are injected into the system prompt.
"""

import json
import logging
import re
from datetime import datetime, timezone
from typing import Annotated, Any, Literal, assert_never

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from app.services.sse import sse_response
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import delete as sql_delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.services.user_interface import language_for
from app.config import settings
from app.database import get_db
from app.models.execution import Run, SessionMessage, SessionProjection
from app.models.project import Project
from app.models.user import User
from app.policies import can_access_project, has_project_capability
from app.services import run_failures
from app.services.app_events import record_app_event
from app.services.sessions import (
    create_session,
    get_session,
    mechanical_session_title,
    require_drive_access,
)

router = APIRouter()
logger = logging.getLogger(__name__)
LOCAL_SSE_KEEPALIVE_SECONDS = 180


class ChatMessageIn(BaseModel):
    role: Literal["user", "assistant"] = "user"
    content: str


class ChoiceAnswer(BaseModel):
    """人点了呈递里的**某一项** —— 身份是 `{offer_id, choice_id}`，附言可空。

    带上身份，harness 侧判定答复是一次集合成员检查；文案回传只是兼容层：
    2026-08-19 实测，界面印的选项与框架的合法集分叉时，文案解析会**静默丢弃**
    人的授权（点三次、三次都消失）。
    """

    kind: Literal["choice"]
    offer_id: str | None = None
    choice_id: str = Field(..., min_length=1)
    note: str = ""


class TextAnswer(BaseModel):
    """人说了一句话 —— 开新一轮、插话、或回答一个自由文本问题。"""

    kind: Literal["text"]
    text: str = Field(..., min_length=1)

    @field_validator("text")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        # 「空」只在这里定义一次。全空白和空串是同一件事 —— 让它在门口就 422，
        # 而不是穿过五层之后在某一层被静默吞掉。
        stripped = value.strip()
        if not stripped:
            raise ValueError("text must not be blank")
        return stripped


#: 一次提交只能是这两种之一，由 `kind` 判别。
#:
#: ## 为什么线格式是和类型，而不是 `message` + 可选 `choice`（2026-09-03）
#:
#: 旧格式里 `message` 可空当且仅当 `choice` 在场 —— 这条规则没有写在任何一处，
#: 于是卡片、workspace、hook、本模型四层各自写了一遍"有没有东西可发"，
#: 8-31 改了前两层、后两层原样：点了选项不写附言 → hook 静默 return →
#: 请求根本没出浏览器（cuib 09-03，会话 de4632cc）。同形事故此前已发生过三次。
#:
#: 和类型把"哪一种提交"变成解析结果：`choice` 分支附言天然可空，`text` 分支
#: 天然非空。下游没有第二个地方需要再判一次 —— 也就没有第二个地方可以判错。
#: 前端的 TS 类型由本模型**生成**（contracts/generate_wire_types.py），不是手抄。
Answer = Annotated[ChoiceAnswer | TextAnswer, Field(discriminator="kind")]


class ChatRequest(BaseModel):
    answer: Answer
    history: list[ChatMessageIn] = []
    conversation_id: str | None = None


async def _choice_as_words(db: AsyncSession, run_id: str, answer: "ChoiceAnswer") -> str:
    """人点的那一项，翻成一句给新 worker 的话：选项的 label（呈递方自己写的字）+ 附言。"""
    from app.models.execution import Decision, DecisionStatus

    decision = await db.scalar(
        select(Decision)
        .where(Decision.run_id == run_id, Decision.status == DecisionStatus.PENDING)
        .order_by(Decision.created_at.desc())
    )
    label = next(
        (
            str(item.get("label") or "").strip()
            for item in ((decision.choices if decision is not None else None) or [])
            if isinstance(item, dict) and item.get("choiceId") == answer.choice_id
        ),
        "",
    ) or answer.choice_id
    return " ".join(part for part in (label, answer.note.strip()) if part)


def turn_input_for(answer: ChoiceAnswer | TextAnswer) -> tuple[str, dict[str, Any] | None]:
    """把一次提交翻成执行层的 `(message, choice)` —— **整个后端只在这里翻一次**。

    执行层（`execute_local_turn` → harness `answer` op）沿用 message + choice 两个
    参数：它们是给模型/框架的载荷，不是"这次提交是什么"的判据。判据只在入口。
    """
    match answer:
        case ChoiceAnswer():
            return answer.note.strip(), {"offer_id": answer.offer_id, "choice_id": answer.choice_id}
        case TextAnswer():
            return answer.text, None
        case _:  # pragma: no cover - 穷举由类型检查保证
            assert_never(answer)


class CreatedProjectOut(BaseModel):
    id: str
    name: str
    start_node_type: str
    start_node_id: str | None = None
    auto_started: bool = False
    scheduler_state: str | None = None


class ExecutedAction(BaseModel):
    action: str  # "start_scheduler" | "stop_scheduler"
    success: bool
    detail: str | None = None


class ConceptAnnotationOut(BaseModel):
    """A concept mention detected in the reply text."""

    text: str
    start: int
    end: int
    concept_id: str
    concept_type: str


class ChatResponse(BaseModel):
    reply: str
    scope: Literal["global", "project"]
    conversation_id: str | None = None
    created_project: CreatedProjectOut | None = None
    executed_actions: list[ExecutedAction] = []
    suggested_actions: list[dict[str, Any]] = []
    concept_annotations: list[ConceptAnnotationOut] = []
    usage: dict[str, Any] = {}


def _format_declared_skills(skills: list[Any]) -> str:
    if not skills:
        return ""
    lines = ["## Declared Methodological Skills"]
    for skill in skills:
        if isinstance(skill, dict):
            name = skill.get("name", "unnamed_skill")
            desc = skill.get("description", "")
            purpose = skill.get("purpose", "")
            line = f"- **{name}**"
            if desc:
                line += f": {desc}"
            if purpose:
                line += f" Purpose: {purpose}"
            lines.append(line)
        else:
            lines.append(f"- **{skill}**")
    return "\n".join(lines)


def _append_harness_sections(system_parts: list[str], harness: Any) -> None:
    if harness.rules:
        system_parts.append("## Rules\n" + "\n".join(f"- {r}" for r in harness.rules))
    if harness.guidelines:
        system_parts.append("## Guidelines\n" + "\n".join(f"- {g}" for g in harness.guidelines))
    skills_block = _format_declared_skills(harness.skills)
    if skills_block:
        system_parts.append(skills_block)


def _tool_result_payload(result: dict[str, Any]) -> dict[str, Any]:
    payload = result.get("result")
    return payload if isinstance(payload, dict) else result



# `_annotate_reply`（用 concept_linker 给回复里的 KB 概念加链接）随本服务自己那套
# agent loop 一起退役：它定义在这里但**从未被调用**，而它依赖的 concept_linker 只
# 认本服务的 kb_concepts 表 —— 而概念现在由 harness 沉淀。要恢复这个能力应当基于
# harness KB 重做，而不是留一份够不到、且指向错库的实现。




def _say(phrase: dict[str, str], lang: str) -> str:
    """缺哪种语言就退回中文 —— 和 run_failures._say 同一条规则。"""
    return phrase.get(lang) or phrase["zh"]


# ── Streaming chat endpoints ─────────────────────────────────────


def _local_execution_stream(
    *,
    data: ChatRequest,
    user: User,
    db: AsyncSession,
    conversation: SessionProjection,
    project: Project,
    abandoned_pause: str | None = None,
) -> StreamingResponse:
    """Start a server-owned durable execution and expose SSE as one observer."""
    import asyncio

    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.services.local_execution import (
        execute_local_turn,
        retain_detached_execution,
    )

    if db.bind is None:
        raise RuntimeError("The request database Session is not bound")
    worker_sessions = async_sessionmaker(db.bind, expire_on_commit=False)
    user_id = str(user.id)
    session_id = str(conversation.session_id)
    project_id = str(project.id)
    # 「这次提交是什么」在入口已经解析成 `data.answer`；这里只把它翻成执行层的
    # 载荷（给模型的附言 + 对哪一次呈递选了哪一项），一处翻译，不再判断。
    message, choice_payload = turn_input_for(data.answer)
    event_queue: asyncio.Queue = asyncio.Queue()
    observer_attached = True
    saw_token_delta = False
    # 语言在这里取，不在下面那个 except 里取：`language_for` 读 ORM 属性，而失败
    # 路径上这一轮多半已经 commit 过（属性过期）或会话已关，再读就是一次惰性刷新
    # —— 在 async 会话上那是 MissingGreenlet，于是**兜底的错误事件自己也抛**，
    # 用户什么都收不到。同 local_execution 那一处（2026-09-16 实测）。
    failure_language = language_for(user)

    async def emit(event: dict) -> None:
        if observer_attached:
            await event_queue.put(event)

    async def on_progress(event: dict) -> None:
        nonlocal saw_token_delta
        if event.get("event") == "token.delta" and isinstance(event.get("text"), str):
            saw_token_delta = True
            await emit({"type": "token", "text": event["text"]})
            return
        await emit({"type": "progress", **event})

    async def run_worker() -> None:
        try:
            # 静默作废一个用户正看着的问题 = 他以为自己答上了，其实没有。
            # 续跑本身是对的，但这件事必须**说出来**。
            if abandoned_pause:
                await emit(
                    {
                        "type": "progress",
                        "event": "pause.abandoned",
                        "detail": (
                            "之前那个待回答的问题随运行时一起丢了，已作废。"
                            "对话记录完好，这条消息按新一轮处理。"
                        ),
                        "runId": abandoned_pause,
                        "iteration": 1,
                    }
                )
            async with worker_sessions() as worker_db:
                worker_user = await worker_db.get(User, user_id)
                worker_conversation = await worker_db.get(SessionProjection, session_id)
                worker_project = await worker_db.get(Project, project_id)
                if not worker_user or not worker_conversation or not worker_project:
                    raise RuntimeError("Detached execution context is no longer available")
                result = await execute_local_turn(
                    worker_db,
                    user=worker_user,
                    conversation=worker_conversation,
                    message=message,
                    project=worker_project,
                    on_progress=on_progress,
                    choice=choice_payload,
                )
            platform_failure = result.pop("platform_failure", None)
            if isinstance(platform_failure, dict):
                # 整份摊开，不逐个字段手抄。手抄的那份必然比源头少几个字段
                # （`detail` / `reference` 就是这么漏掉的），而且加新字段时
                # **两边都不报错**，只是流里悄悄少一块。
                await emit(
                    {
                        "type": "error",
                        "status": "failed",
                        "runId": result.get("run_id"),
                        "commandId": result.get("command_id"),
                        **platform_failure,
                    }
                )
                return
            reply = result.pop("reply")
            # Providers without a streaming callback still receive one honest
            # completed reply. Never simulate token streaming by slicing it.
            if not saw_token_delta and reply:
                await emit({"type": "token", "text": reply})
            await emit(
                {
                    "type": "done",
                    **result,
                    # 权威终稿随终帧送达（2026-08-17）。
                    #
                    # 流过 token 时这行以前是不发的，于是客户端手里只剩**逐个
                    # token 拼起来的一坨** —— 那是这一轮所有中间轮次的散文首尾
                    # 相接，不是回复。症状：同一段话在对话里出现两次（上面大字
                    # 是拼接产物，下面"第 N 轮"是同一段的叙述事件）。
                    #
                    # 流式的价值是**过程可见**，不是"过程即结论"。所以两者都保留
                    # 各自的职责：token 负责跑的时候有反馈，`reply` 负责收尾时
                    # 把消息收敛成回复契约认可的那一段。
                    "reply": reply,
                    "created_project": None,
                    "executed_actions": [],
                    "concept_annotations": [],
                }
            )
        except Exception as exc:
            logger.exception("Local execution failed")
            # 这里原来是 `{"type": "error", "message": str(exc)}` —— 兜底路径
            # 反而是全流程**唯一一处连脱敏都没有**的用户可见出口：任何在
            # `execute_local_turn` 之外炸的异常，连同它字符串里可能夹带的路径
            # 和令牌，原样进用户的会话。兜底路径最少被走到，也最少被看见。
            await emit(
                {
                    "type": "error",
                    "status": "failed",
                    **run_failures.describe(exc, lang=failure_language).as_record(),
                }
            )

    async def event_gen():
        nonlocal observer_attached
        task = asyncio.create_task(run_worker())
        retain_detached_execution(task)
        try:
            while True:
                try:
                    event = await asyncio.wait_for(
                        event_queue.get(), timeout=LOCAL_SSE_KEEPALIVE_SECONDS
                    )
                    yield f"data: {json.dumps(event, ensure_ascii=False, default=str)}\n\n"
                    if event["type"] in {"done", "error"}:
                        break
                except TimeoutError:
                    # A quiet long-running tool is not a failed Run. Keep the
                    # observer connection alive and continue waiting for the
                    # server-owned execution.
                    yield ": keepalive\n\n"
        finally:
            # The execution belongs to the App Server. Closing this generator
            # only removes one observer; durable event/run endpoints provide
            # the reconnect/polling path while the task continues.
            observer_attached = False

    # 这条流一整轮都在通过 `db` 写账（run/attempt/消息），会话要活到流结束。
    return sse_response(event_gen(), release=None)


@router.post("/global/stream")
async def global_chat_stream(
    data: ChatRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    """Global execution-only chat is retired; a durable Project Session is required."""
    raise HTTPException(
        status_code=409,
        detail="Choose or create a Project before starting a Research Session",
    )



@router.post("/projects/{project_id}/stream")
async def project_chat_stream(
    project_id: str,
    data: ChatRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    """Stream project chat response as SSE.

    Since project chat uses AgentLoop (multi-turn tool calling), we can't stream
    individual tokens. Instead, we stream progress events (tool calls, thinking)
    and then the final response as token chunks.
    """
    project = await db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if not await can_access_project(db, user, project):
        raise HTTPException(status_code=403, detail="Not authorized for this project")

    lang = language_for(user)
    conversation: SessionProjection | None = None
    if data.conversation_id:
        _, conversation = await get_session(db, user, project_id, data.conversation_id)
    from app.services.harness_sessions import (
        HarnessSessionStaleError,
        assert_conversation_runtime_available,
        harness_session_manager as _hsm,
    )

    # ── 先按「这次提交是什么」分派，再看别的 ───────────────────────────────
    #
    # 判别在入口解析时已经完成（`data.answer.kind`）。下面每个分支只处理一种
    # 提交；穷举由 `assert_never` 保证 —— 加第三种提交而漏了分支是类型错误。
    # 停在这张卡上的 worker 若是部署前起的旧一代，答复不能再送给它：送了它就带着
    # 旧代码继续跑、下一张卡照样按旧规矩停。换代与开新一轮同一条路（respawn +
    # pause 标成可恢复），人的这一下点击化成一句话交给新 worker：它的流程账本仍
    # 记着这个决定点，会重新呈递；连续档下当场自动放行。
    if isinstance(data.answer, ChoiceAnswer) and conversation is not None and (
        _hsm.paused_worker_is_stale(str(project.id), str(conversation.session_id))
    ):
        retired = await _hsm.retire_paused_worker(str(project.id), str(conversation.session_id))
        if retired is not None:
            data = data.model_copy(
                update={
                    "answer": TextAnswer(
                        kind="text",
                        text=await _choice_as_words(db, retired.run_id, data.answer),
                    )
                }
            )
    match data.answer:
        case ChoiceAnswer():
            # 点选项 = 回答**停着等人的那个 pause**。它只能投给一个活着且停着
            # 的 worker；没有就响亮地 409 —— 这张卡已经过期（重启、别人先答了、
            # 会话没了）。此前这条路排在占用分流**之后**：worker 在飞时人点的
            # 选项会被当成插话文案投进收件箱，choice_id 原地丢失，卡片原样留着。
            if conversation is None or _hsm.paused_binding(
                str(project.id), str(conversation.session_id)
            ) is None:
                raise HTTPException(
                    status_code=409,
                    detail={
                        **run_failures.copy_for("no_pause_to_answer", lang),
                        "message": _say(
                            {
                                "zh": "这个决策已经不在等回答了。刷新一下会话看它现在的样子。",
                                "en": (
                                    "This decision is no longer waiting for an answer. "
                                    "Refresh the Session to see its current state."
                                ),
                            },
                            lang,
                        ),
                        "sessionId": data.conversation_id,
                    },
                )
            # 答复要指回**它回答的那一次呈递**。运行时自己也核这条（offer_superseded），
            # 但那时答复已经排进队、等到锁空才被拒，人看到的是空气泡加同一张卡。
            # 入口就核，用运行时报上来的活呈递身份，一分钟前的卡当场 409。
            live_offer = _hsm.paused_offer_id(str(project.id), str(conversation.session_id))
            answered_offer = (data.answer.offer_id or "").strip()
            if answered_offer and live_offer and answered_offer != live_offer:
                raise HTTPException(
                    status_code=409,
                    detail={
                        **run_failures.copy_for("offer_superseded", lang),
                        "message": _say(
                            {
                                "zh": (
                                    "这个回答指向的是上一次呈递；运行已经走到新的一次了。"
                                    "刷新会话，回答当前那张卡。"
                                ),
                                "en": (
                                    "This answer addresses an earlier presentation; the run has "
                                    "moved on to a new one. Refresh the Session and answer the "
                                    "current card."
                                ),
                            },
                            lang,
                        ),
                        "sessionId": data.conversation_id,
                        "answeredOfferId": answered_offer,
                        "currentOfferId": live_offer,
                    },
                )
        case TextAnswer():
            if not conversation:
                conversation = await create_session(
                    db,
                    user=user,
                    project=project,
                    # 与 `set_session_title_from_first_message` 用同一个截断函数：两份
                    # 各自演化的截断会让"这标题是不是机器起的"变成不可判的问题，自动
                    # 命名也就无从判断该不该覆盖。
                    title=mechanical_session_title(data.answer.text) or "New research Session",
                    summary=None,
                    model_backend_id=None,
                )
        case _:  # pragma: no cover - 穷举由类型检查保证
            assert_never(data.answer)
    await require_drive_access(db, user=user, project=project, session_id=conversation.session_id)

    active_run = await db.scalar(
        select(Run)
        .where(
            Run.tenant_id == conversation.tenant_id,
            Run.project_id == str(project.id),
            Run.session_id == str(conversation.session_id),
            # 只看**顶层** run。子节点 run 的生命周期由父 run 驱动，它们从来不会
            # 被单独推到终态 —— 一次派发之后就永远停在 `queued`。
            #
            # 2026-08-12 实测（E2E v26）：父 run 正确地停在 waiting_permission
            # 等人批准一次真实作业提交，而 6 条子节点 run 全挂在 `queued`，
            # 于是这道闸恒为真：
            #
            #     409 session_run_active
            #     runId: run_53ac…/_orchestrator->_curator@d1   ← 子节点
            #
            # 人再也答不了那个 pause，研究永久卡死。
            #
            # 「顶层 run」和「子节点 run」是两种东西 —— 前者回答"这个会话在不在
            # 忙"，后者只是前者内部的一段。同一族问题今天已经在孤儿扫描上踩过
            # 一次（那次是误杀正在干活的子进程）。
            Run.parent_run_id.is_(None),
            Run.status.in_(
                [
                    "queued",
                    "dispatching",
                    "running",
                    "waiting_compute",
                    "retrying",
                ]
            ),
        )
        .order_by(Run.created_at.desc())
    )
    # 「这个会话现在能不能开新一轮」**只有一个判据**（RFC D10，2026-08-23
    # 删除清单）。
    #
    # 8-21 那次是两个真相源方向相反地各错一边：run 行说空闲（它早被平台盖成
    # `incomplete`），内存锁说忙（worker 停靠在 4 小时复查间隔上还攥着）。
    # 当时的止血是"两个都问，任一为真就走插话" —— 那让**症状**消失了，可
    # 两个真相源还在，只是暂时不打架。
    #
    # 现在拆掉那个第二源。判据是 `is_occupied()`：它自己已经是"本进程的锁
    # **或** worker 落在盘上的自报活动"，两者都是**现场**；run 行是平台对
    # worker 的转述，转述和现场分叉时永远听现场的。
    #
    # 拿掉 run 行之后反而修好了一类：run 行是 `running` 而 worker 其实早没了
    # （重启、接不回来）—— 从前那会被判成"忙"→ 走插话 → 送不到 → 409 把用户
    # 的话弹回去。现在它如实是空闲，这句话开一条新 run 从 checkpoint 续跑。
    #
    # `active_run` 保留，但只做它本来的事：给这条消息挂上归属的 run_id。
    #
    # 占用分流只对**一句话**成立（开新轮还是插话）。点选项的提交在上面已经
    # 按命令类型分派走了 answer 路 —— 它不受占用影响：worker 在飞时答复排队
    # （会话锁由 `claim()` 持有），不是被翻译成另一种东西。
    if isinstance(data.answer, TextAnswer) and _hsm.is_occupied(
        str(project.id), str(conversation.session_id)
    ):
        # ── 单一入口，机械分流（2026-08-17）─────────────────────────────────
        #
        # 会话正忙时，这句话是**插话**，不是新一轮：落 user 消息行 + 投收件箱，
        # 正在跑的 harness 进程用同一份决策轮逻辑处理（问进度就查进度答复、
        # 调方向就 inject、要停就 cancel）。
        #
        # 以前这里直接 409，前端被迫自己分叉一条 /interrupt 路径，并因为一个
        # 多余的 isDriver 检查把用户的话静默吞掉（实测：能打字、点发送无声
        # 无息）。"忙不忙、话该走哪"是后端的事实判断，不该由客户端猜。
        #
        # **插话永远承接，不看进程死活**（wangd 2026-08-17：「我在插话，为什么
        # 会得到 409？不应该是调度器能很好的承接吗」）。收件箱是 worktree 里
        # 的文件，取件的是 worker 自己轮询 —— 投递本来就不依赖后端内存里那张
        # 绑定表。进程真死了也不会产生死信：孤儿扫描会把无主 run 标 stale，
        # 下一条消息触发续跑，续跑的 drain 循环把排队的插话按 deferred 取走。
        # 走到这里 = `is_occupied()` 为真 = **确实有人在跑**。所以这里不再
        # 各自推断"活没活着"：从前那两个字段（`live` 由 live_binding 推、
        # `session_occupied` 由上面的分流判据推）在这个分支里恒为真，说的却
        # 像是两个独立事实。现在只留一个，而且来自**现场**——worker 在回执里
        # 自己报的当下状态。
        from app.services.sessions import interject_active_session

        # run 行可能已经不在了（占用由现场认定）—— 那这条插话就没有归属 run，
        # `interject_active_session` 的 `run_id` 本来就允许 None。**别为了填这个
        # 字段去编一个 id**：消息行挂错 run 比不挂更难查（决策呈递那次的教训）。
        active_run_id = str(active_run.id) if active_run else None
        try:
            delivered, interjected_message_id, occupancy = await interject_active_session(
                db,
                session=conversation,
                text=data.answer.text,
                author_user_id=user.id,
                run_id=active_run_id,
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "interject_undeliverable",
                    "message": str(exc),
                    "runId": active_run_id,
                },
            ) from exc
        # ── 回执留痕（RFC P1）────────────────────────────────────────────
        #
        # 下面那三句 `progress` 帧是**流内的**：断流即失，刷新页面、换设备、
        # 事后复盘都看不到平台当时答了什么。而用户那句话是持久化的
        # （`SessionMessage`）—— 于是同一次交互里"人说了什么"留下了、
        # "平台答了什么"没留下，事后只能看到一句没有回音的话。
        #
        # 事实进事实流；`progress` 帧保留，它负责的是**这一刻的即时反馈**。
        # 两者不是重复：一个回答"现在怎么样了"，一个回答"当时发生过什么"。
        await record_app_event(
            db,
            session_id=str(conversation.session_id),
            kind="interject.queued",
            run_id=active_run_id,
            payload={
                "messageId": interjected_message_id,
                # 送达 ≠ 已处理。两件事分开记，别让读的人自己脑补。
                "delivery": "delivered_to_live_runtime",
                "occupancy": occupancy,
                "text": data.answer.text[:280],
            },
            dedupe_key=interjected_message_id,
        )
        await db.commit()
        interject_frames = (
            {
                "type": "progress",
                "event": "interject.delivered",
                "detail": "已送达给正在跑的调度器 —— 它会在下一个边界处理这句话",
            },
            {
                # 收下 ≠ 马上被处理。ack 必须**同时**说这两句 ——「永远收下」
                # 如果不配一句诚实的现状，入队黑洞比一个响亮的 409 更糟
                # （409 至少是响的）。RFC D10 可寻址那一维的配套。
                "type": "progress",
                "event": "interject.occupancy",
                "detail": (
                    "上一轮还占着这个会话 —— 它会在下一个边界读到你这句话"
                    if occupancy == "working"
                    else "它此刻空闲 —— 这句话会被立即处理"
                ),
            },
            {
                # 如实回执：**已投递**，不是"已处理"。回应会以时间线事件的
                # 形式出现（决策轮的答复 → agent.message）。liveness 让前端
                # 把预期时延讲真话，而不是决定收不收。
                "type": "done",
                "routed": "interject",
                "status": "delivered",
                "occupancy": occupancy,
                "runId": active_run_id,
                "deliveredAt": delivered,
                "reply": "",
                "created_project": None,
                "executed_actions": [],
                "concept_annotations": [],
            },
        )

        async def _interject_stream():
            for event in interject_frames:
                yield f"data: {json.dumps(event, ensure_ascii=False, default=str)}\n\n"

        return sse_response(_interject_stream(), release=db)

    # ── Session 没有「关了」这个状态 ────────────────────────────────────────
    #
    # 会话真正的载体在磁盘上：worktree、分支、以及 `agent_loop` 每一轮都在写的
    # `messages_checkpoint.json`。worker 进程只是缓存 —— 没了就再起一个，`op=init`
    # 会无条件把 checkpoint 读回来，还会 `recover_interrupted_decision_actions`
    # 修被打断的决策。**续跑一直是完整实现的。**
    #
    # 原来这里 409，把用户导向"开一个新 Session"。而新 Session 意味着新
    # session_id，state root 的路径按 session_id 拼 —— 新 worker 去新目录找
    # checkpoint，当然是空的。2026-08-13 实测：老 Session 的 checkpoint
    # （15 条消息 / 152KB）好端端在磁盘上，agent 却从零重读文件定位，重启前刚
    # 测出来的性能数据和成本估算全部重做。
    #
    # worker 死掉，死的是**那个挂起的询问**（进程内存里，真没了）。对话不在
    # 内存里。用不可恢复的那一半判可恢复的那一半死刑 —— 又是两件事一条规则。
    #
    # 所以：把死掉的询问就地作废（并在流里告诉用户），然后照常开一轮新的。
    # 「开新 Session」仍然保留，但那是人主动甩掉一段上下文时的选择，不是平台
    # 重启之后的唯一出路。
    abandoned_pause: str | None = None
    try:
        await assert_conversation_runtime_available(
            db,
            project_id=str(project.id),
            conversation_id=str(conversation.session_id),
            user_id=user.id,
        )
    except HarnessSessionStaleError:
        from app.services.sessions import resume_stale_session

        _, abandoned = await resume_stale_session(
            db, user=user, project=project, session_id=str(conversation.session_id)
        )
        abandoned_pause = abandoned[0] if abandoned else None
    # The detached worker uses its own short-lived database Session. Publish
    # any newly created Session/snapshot/driver state before it reloads them.
    await db.commit()
    return _local_execution_stream(
        data=data, user=user, db=db, conversation=conversation, project=project,
        abandoned_pause=abandoned_pause,
    )

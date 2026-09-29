"""Transcript-to-event projection, durable idempotency, and Decision authority."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal, DecimalException
from math import isfinite
from pathlib import Path
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import set_committed_value

from app.models.execution import (
    CANCELLABLE_RUN_STATUSES,
    AttemptStatus,
    Command,
    Decision,
    DecisionAuthorityType,
    DecisionStatus,
    EventOrigin,
    EventVisibility,
    ExecutionEvent,
    Run,
    RunAttempt,
    RunStatus,
    SessionProjection,
    TERMINAL_RUN_STATUSES,
)
from app.services.redaction import DEFAULT_REDACTION_POLICY, RedactionPolicy
from app.services.run_status import project_run_status

#: 一条记录的 `file_identity` 有三种来源，两种由平台自己命名。名字在这里
#: 定义一次，读它的判据 import 它 —— 判据自己拼字面量，就是第二个真相源。
#:
#: - worker transcript：`file_identity_of()` 给的 `(设备:inode)`
#: - 同一行派生的叙述：transcript 身份 + `NARRATION_IDENTITY_SUFFIX`
#: - 平台自己写的记录（run_start / session_message…）：`LOCAL_RECORD_IDENTITY_PREFIX` 打头
NARRATION_IDENTITY_SUFFIX = "#narration"
LOCAL_RECORD_IDENTITY_PREFIX = "local-worker:"

# 跨进程线协议：harness 把「这一次呈递」整份挂在 pause 事件的这个键下
# （core/decision_offer.py 的 PAUSE_OFFER_KEY）。backend 不 import harness 包
# （独立进程、独立 venv —— 这里曾直接 import，把整个 backend 弄挂了），所以
# 像 HTTP header 名一样在边界两侧各写一次字面量；两侧一致由
# tests/test_offer_survives_the_whole_pipe.py 机械钉住。
PAUSE_OFFER_KEY = "offer"


log = logging.getLogger(__name__)

ADAPTER_VERSION = "1.0.0"
NORMAL_DECISION_CHOICES = [
    {"choiceId": "proceed", "label": "Continue"},
    {"choiceId": "revise", "label": "Revise this step"},
    {"choiceId": "redirect_upstream", "label": "Go back upstream"},
    {"choiceId": "abort", "label": "Stop research"},
    {"choiceId": "edit", "label": "Edit manually"},
]
# Review-failed action set: the harness deliberately withholds PROCEED until a
# valid review exists (or the reviewer retry cap restores it as an explicit
# human override).  Mirrors shared/tools/library/decision_package.py.
REVIEW_FAILED_DECISION_CHOICES = [
    {"choiceId": "retry_reviewer", "label": "Retry the reviewer"},
    {"choiceId": "revise", "label": "Revise this step"},
    {"choiceId": "redirect_upstream", "label": "Go back upstream"},
    {"choiceId": "abort", "label": "Stop research"},
    {"choiceId": "edit", "label": "Edit manually"},
]
#: 一条 workspace.changed 事件最多带多少 diff 正文。采集端已经限过额，这里
#: 是边界自己的天花板 —— 换个版本的 harness 不该能把几 MB 灌进事件库。
_MAX_WORKSPACE_PATCH_BYTES = 64_000
_CHOICE_LABELS = {
    str(choice["choiceId"]): str(choice["label"])
    for choice in (
        NORMAL_DECISION_CHOICES
        + REVIEW_FAILED_DECISION_CHOICES
    )
}


def decision_choices_for_event(raw: dict) -> list[dict[str, str]]:
    """Model the exact action set the harness presented.

    Preferred source: the event's own ``decision_options`` (the harness emits
    the truthful list).  Fallback for older transcripts without it: derive from
    ``review_failed`` — fail-closed is reserved for genuinely unknown actions,
    not for a known, deliberate action set.
    """
    options = raw.get("decision_options")
    if isinstance(options, list) and options and all(isinstance(o, str) for o in options):
        unknown = [o for o in options if o not in _CHOICE_LABELS]
        if unknown:
            raise UnsupportedDecisionActionSetError(
                f"transcript presented unknown Decision action(s): {unknown}"
            )
        return [{"choiceId": o, "label": _CHOICE_LABELS[o]} for o in options]
    if raw.get("review_failed") is not False:
        return [dict(choice) for choice in REVIEW_FAILED_DECISION_CHOICES]
    return [dict(choice) for choice in NORMAL_DECISION_CHOICES]


class IngestError(RuntimeError):
    """Base error for fail-closed ingest."""


class RecordRejectedError(IngestError):
    """This one record violates a content invariant; the store itself is fine.

    ## 为什么要把"这条记录不合格"和"库出了问题"分成两个异常

    2026-08-17 事故：orchestrator 在同一轮里重呈递 decision package（curator
    重跑后条款已变），派生出的 Decision.id 却相同 → 不可变快照守卫如实拒收
    这**一条**记录 → 异常一路穿到 `answer()` → **整个活着的 turn 被判死**，
    run 记成 failed，而 harness 侧研究早就做完了。用户看到的是"平台内部错误
    /请重发"，而重发撞上 resume 绑定校验，永远不可能成功。

    记录层守卫是**见证人，不是法官**：一条记录违反内容不变量，正确动作是把
    "拒收了什么、为什么"如实落成 durable 事件（`record.rejected`），让 turn
    继续 —— 证据可持久化，判决不可以由记录层做出。只有**库本身**不可信时
    （scope 行消失、序列分配失败、创建竞态无果），继续摄取才是在制造更多
    不可信状态，那些仍然抛 `IngestError` 基类、照旧致命。

    判据：这个异常说的是"**这条记录**怎么了"就用本类；说的是"**这个库**
    怎么了"就用基类。
    """


class TranscriptParseError(IngestError):
    """A complete JSONL line could not be parsed."""


class UnsupportedDecisionActionSetError(IngestError):
    """The transcript cannot prove which Decision action set was presented."""


class DecisionAuthorityRequiredError(IngestError):
    """A Decision event arrived without a frozen authority snapshot."""


class DecisionResponseRejectedError(RuntimeError):
    """A response failed capability, authority, choice, or state checks."""

    def __init__(self, message: str, *, code: str = "decision_response_rejected") -> None:
        super().__init__(message)
        self.code = code


class IdempotencyConflictError(RuntimeError):
    """An idempotency key was reused for a different command."""


@dataclass(frozen=True, slots=True)
class DecisionAuthoritySnapshot:
    authority_type: str
    authority_subjects: tuple[str, ...]
    required_approval_count: int
    action_set_version: str
    policy_snapshot_id: str
    expires_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.authority_type not in {item.value for item in DecisionAuthorityType}:
            raise ValueError("unsupported Decision authority type")
        if not self.authority_subjects:
            raise ValueError("Decision authority subjects cannot be empty")
        if any(
            not isinstance(subject, str) or not subject.strip()
            for subject in self.authority_subjects
        ):
            raise ValueError("Decision authority subjects cannot contain empty values")
        if len(set(self.authority_subjects)) != len(self.authority_subjects):
            raise ValueError("Decision authority subjects must be unique")
        if (
            not isinstance(self.action_set_version, str)
            or not self.action_set_version.strip()
            or not isinstance(self.policy_snapshot_id, str)
            or not self.policy_snapshot_id.strip()
        ):
            raise ValueError("Decision authority versions cannot be empty")
        if self.required_approval_count < 1:
            raise ValueError("Decision approval count must be positive")
        if self.required_approval_count > len(set(self.authority_subjects)):
            raise ValueError("Decision approval count exceeds unique authority subjects")


@dataclass(frozen=True, slots=True)
class IngestContext:
    tenant_id: str
    workspace_id: str
    project_id: str
    session_id: str
    run_id: str
    attempt_no: int = 1
    parent_run_id: str | None = None
    actor_user_id: str | None = None
    decision_authority: DecisionAuthoritySnapshot | None = None

    def __post_init__(self) -> None:
        for name in ("tenant_id", "workspace_id", "project_id", "session_id", "run_id"):
            if not getattr(self, name):
                raise ValueError(f"{name} is required")
        if self.attempt_no < 1:
            raise ValueError("attempt_no must be positive")



#: adapter_state 里存子节点身份的键。按 **file_identity** 分桶（每份 transcript
#: 一个 state），所以两个子节点不会串。
_CHILD_IDENTITY_KEY = "_child_run_identity"

#: adapter_state 里"从库里取回来的子 run id"的键。写它的是
#: `harness_transcript_ingest`（它有 db）；这里只读。两边同一个字面量会分叉，
#: 所以名字在这里定义一次，那边 import 过去。
RECOVERED_CHILD_RUN_KEY = "_recovered_child_run_id"


@dataclass(frozen=True, slots=True)
class ChildRunIdentity:
    """一份子节点 transcript 的身份 —— 谁在跑、归谁管。"""

    run_id: str
    node_type: str
    parent_run_id: str | None
    depth: int


def child_run_identity(raw: dict) -> ChildRunIdentity | None:
    """从 `run_start` 认出「这份 transcript 是哪个子节点的」。

    ## 为什么需要（wangd 2026-08-11 试用）

    > 「literature 都结束了，然后开始 hypothesis 了，它还是在下面显示一大坨」

    实测：子节点的 transcript **都被摄取了**（6 个不同文件），但事件全记在顶层
    那一条 run 上（`run_id` 只有 1 个）。库里分不开，前端再怎么渲染也分不开。

    身份只在每份 transcript 的**第一条** `run_start` 里：

        node_type / depth / sub_run_id / parent_run_id

    后续事件只带 tenant/session —— 所以要按文件记一次，见
    `remember_child_identity`。

    没有 depth / parent 的是顶层自己的 transcript，返回 None（别把
    orchestrator 当成自己的子节点）。
    """
    if raw.get("event") != "run_start":
        return None
    run_id = str(raw.get("sub_run_id") or "").strip()
    parent = str(raw.get("parent_run_id") or "").strip()
    try:
        depth = int(raw.get("depth") or 0)
    except (TypeError, ValueError):
        depth = 0
    if not run_id or not parent or depth <= 0:
        return None
    return ChildRunIdentity(
        run_id=run_id,
        node_type=str(raw.get("node_type") or "") or "unknown",
        parent_run_id=parent or None,
        depth=depth,
    )


def remember_child_identity(adapter_state: dict, raw: dict) -> ChildRunIdentity | None:
    """记住/取回这份 transcript 的子节点身份。

    `adapter_state` 是**按 file_identity 分桶**的（见
    `local_execution._harness_adapter_state`），所以两个子节点各记各的，不串。
    """
    found = child_run_identity(raw)
    if found is not None:
        adapter_state[_CHILD_IDENTITY_KEY] = found
        return found
    existing = adapter_state.get(_CHILD_IDENTITY_KEY)
    return existing if isinstance(existing, ChildRunIdentity) else None

@dataclass(frozen=True, slots=True)
class EventDraft:
    kind: str
    visibility: str
    payload: dict[str, Any]
    occurred_at: datetime


@dataclass(frozen=True, slots=True)
class IngestOutcome:
    event: ExecutionEvent | None
    duplicate: bool
    emitted_event_ids: tuple[str, ...] = field(default_factory=tuple)


def _hash_parts(*parts: object) -> str:
    digest = hashlib.sha256()
    for part in parts:
        digest.update(str(part).encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def _root_step_id(run_id: str, attempt_no: object, adapter_state: dict[str, Any]) -> str:
    """这份 transcript 的"根 step" id —— **唯一**的算法，三个调用点共用。

    两条判据缠在一起，缺一个就会把不同的活动记到同一张卡片上：

    1. **父子不能同 id**（2026-08-11 修）：父子 transcript 共用同一个 platform
       run，只按 `context.run_id` 算的话，子节点的每一次工具调用都记到调度器
       名下（E2E v23 实测：42 次全挂在 `project_chat activity`，literature 与
       hypothesis 各 0）。归属由**知道自己是谁**的一方声明 —— 子 run 用它自己的
       run id（`dispatchKey` = 它那份 transcript 的文件身份）。

    2. **同一个节点被派发第二次，也不能同 id**（2026-08-20 修）：平台侧的子 run
       id 是 `<parent>::_orchestrator->observation@d1` —— 它标识的是**槽位**
       （父 run + 节点类型 + 深度），不是这一次派发。于是 observation 被重新
       派发时算出与上一次相同的 step id，`_insert_event` 按 event_id 幂等，
       新的 `step.started` 被当成重复**静默吞掉**：
         · 右栏「当前」恒空（没有任何 step 是 running），UI 说"当前没有节点在跑"
           而它正跑着；
         · 新一轮的工具调用全部追加到**上一次那张已标"完成"的卡片**上
           （实测：同一张 observation 卡从 44 actions 涨到 62）。
       `dispatchKey` 天然带"第几次"，用它就同时满足两条。

       ⚠️ 这里一度用的是**目录名**，理由写着"每次派发都不同"—— 实测不成立：
       harness 重新派发同一个节点时复用同一个 run 目录（续跑要靠它找回
       checkpoint）。修复接对了地方，喂的却是一个不具备那个性质的值，于是
       缺陷原样活着（wangd 2026-08-21 实测：9 次派发只出现 4 张卡）。
       现在用文件身份 —— 新的一次派发是新文件，同一趟续跑是同一份文件。

    以前这个算法有三份抄件，正确的两份长在 `root_step_start` / `root_step_end`
    分支上 —— 而 harness 从来不发 `root_step_start`（全仓 0 次），真正跑的
    `_ensure_root_step` 里是会撞的那份。抄件的分叉不会报错，只会让 UI 说谎。
    """
    # `dispatchKey` = 这份子 transcript 的文件身份（见 `ingest_transcript_file`）。
    # 老 checkpoint 里存的是 `childRunId`（目录名）—— 读侧继续认它，否则平台
    # 重启后正在跑的那些 run 会**换一张卡片**：历史卡挂在老 id 上，新事件挂到
    # 新 id 上，同一趟裂成两半。字段换名不该让在飞的 run 断线。
    dispatch_key = adapter_state.get("dispatchKey") or adapter_state.get("childRunId")
    if dispatch_key:
        return f"step_{_hash_parts(run_id, 'child', str(dispatch_key))[:24]}"
    return f"step_{_hash_parts(run_id, attempt_no, 'root')[:24]}"


def _parse_time(value: Any) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise TranscriptParseError("raw transcript event is missing its occurrence time")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise TranscriptParseError("raw transcript event has an invalid occurrence time") from exc
    if parsed.tzinfo is None:
        raise TranscriptParseError("raw transcript occurrence time must include a timezone")
    return parsed


#: 活动租约的时长 —— 定义搬去了 `run_liveness`（活性判据只有一个家）；这里
#: re-export 是因为既有消费方按这个名字从本模块 import。
from app.services.run_liveness import ACTIVITY_LEASE_SECONDS  # noqa: E402

#: 租约续期的节流窗口：这个窗口内只写一次库（见 `_touch_activity_lease`）。
_LEASE_TOUCH_SECONDS = 30

_TRUNCATED_ERROR_ENVELOPE = re.compile(r'^\{"status":\s*"(error|failed)"')
_ERROR_TEXT_IN_TRUNCATED = re.compile(r'"error":\s*"((?:[^"\\]|\\.)*)"')
_ERROR_CODE_IN_TRUNCATED = re.compile(r'"error_code":\s*"([a-z_]+)"')


def _tool_failure_fields(raw: dict[str, Any], result: Any) -> dict[str, Any]:
    """失败事件的 errorCode / errorMessage —— **一处**决定，别在别处再抄一份。

    `errorCode` 是 harness 在产生失败的那一层盖的章（core/tool_errors）：
    `rejected` = 框架按设计说不（ReAct 的正常一步），`tool_exception` = 我们
    的代码崩了。显示层据此决定摆不摆到人面前；此前这里硬编码成 "tool_error"，
    等于把唯一的分类信息在这一跳丢掉，前端只能按字符串长相猜。
    """
    code: Any = raw.get("error_code")
    message: Any = raw.get("error")
    if isinstance(result, dict):
        code = result.get("error_code") or code
        message = result.get("error") or message
        if not message and result.get("returncode") not in (None, 0):
            # 子进程类工具的失败写在 returncode/stderr_tail 里。
            message = f"命令退出码 {result['returncode']}：" + str(
                result.get("stderr_tail") or "")[-300:]
    elif isinstance(result, str):
        found_code = _ERROR_CODE_IN_TRUNCATED.search(result)
        if found_code:
            code = found_code.group(1)
        found = _ERROR_TEXT_IN_TRUNCATED.search(result)
        if found:
            # 用 JSON 自己的解码器还原转义（\uXXXX / \n）—— `unicode_escape`
            # 会把已经是 UTF-8 的中文正文拆成乱码。
            try:
                message = json.loads(f'"{found.group(1)}"')
            except json.JSONDecodeError:
                message = found.group(1)
        elif not message:
            message = result
    fields = {
        "errorCode": str(code or "tool_error"),
        "errorMessage": str(message or "Tool execution failed"),
    }
    # 工具自己写的"下一步该干什么"。显示层一直在读 `error.recovery`，但在这一跳
    # 之前**没有任何写者** —— 于是每一类失败都退回按工具类型猜的兜底句，而兜底
    # 句对"这台机器没装 latexmk"说的是"去查你的源码"。产生失败的那一层最清楚
    # 下一步是谁的活，让它把话说完（长度由显示层自己设界）。
    recovery = result.get("recovery") if isinstance(result, dict) else None
    if not recovery:
        recovery = raw.get("recovery")
    if isinstance(recovery, str) and recovery.strip():
        fields["errorRecovery"] = recovery.strip()
    # 崩了的那一类要带**出事地点**。`KeyError: 'turns'` 说得出是什么，说不出
    # 在哪 —— harness 在 dispatch 那里已经把 traceback 末 3 行放进结果了，这一
    # 跳以前直接丢掉，于是取证视图里也没有，只能靠猜。本机库里有 5 类崩溃至今
    # 定位不到，全是因为这个。只进 `tool_exception`：其余各类的正文是我们自己
    # 写的指导，没有 traceback 可言。
    if fields["errorCode"] == "tool_exception" and isinstance(result, dict):
        tail = result.get("traceback_tail")
        if isinstance(tail, list) and tail:
            fields["errorTracebackTail"] = [str(line)[:400] for line in tail[-3:]]
    return fields


def _result_summary(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _validated_cost(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        cost = Decimal(str(value))
    except (DecimalException, ValueError) as exc:
        raise TranscriptParseError("usage cost must be a finite non-negative number") from exc
    if not cost.is_finite() or cost < 0:
        raise TranscriptParseError("usage cost must be a finite non-negative number")
    try:
        json_cost = float(cost)
    except (OverflowError, ValueError) as exc:
        raise TranscriptParseError("usage cost must be a finite non-negative number") from exc
    if not isfinite(json_cost):
        raise TranscriptParseError("usage cost must be a finite non-negative number")
    return cost


def _json_cost_number(cost: Decimal | None) -> float | None:
    """Encode a validated Decimal as the canonical JSON number representation."""
    return float(cost) if cost is not None else None


class TranscriptAdapter:
    """Deterministic adapter for current Harness JSONL facts."""

    def adapt(
        self,
        raw: dict[str, Any],
        *,
        context: IngestContext,
        adapter_state: dict[str, Any],
        event_id: str,
    ) -> EventDraft | None:
        raw_event = raw.get("event")
        at = _parse_time(raw.get("at"))
        attempt_no = int(raw.get("attempt_no") or context.attempt_no)

        if raw_event == "run_resumed_after_interruption":
            # 死亡续跑：**同一条**节点 run 接上 checkpoint 继续（2026-08-18）。
            # 平台的 run id 按轮次前缀，所以续跑的活动落在新一轮的 run 下 ——
            # 那是对的（活动确实发生在两个轮次里），但 UI 必须说清楚这是
            # "接着上次跑"，否则读起来仍旧是"它又新开了一个"，正是这次要
            # 消灭的观感。用已有的 `run.resumed` kind，不新造事件类型。
            return EventDraft(
                "run.resumed",
                EventVisibility.SUMMARY,
                {
                    "resumedFromTurn": int(raw.get("resumed_from_turn") or 0),
                    "owningRun": False,
                },
                at,
            )
        if raw_event == "run_start":
            # 一个用户命令 = 一个 Run；但 harness 在命令内跑**节点树**
            # （orchestrator → experiment → _reviewer → _curator …），每个子
            # 节点都有自己的 run_start/run_end。深度 1 才是这条命令自己的
            # 生命周期；子节点的终态绝不能关掉父命令的 attempt（实测事故：
            # experiment incomplete 把父 attempt 关成 failed，之后 decision
            # 又把 run 翻回 waiting_human，durable 状态自相矛盾 → 人工答复
            # 永远被拒，post-node 决策后 run 永久卡死）。
            depth = int(adapter_state.get("runDepth") or 0) + 1
            adapter_state["runDepth"] = depth
            owning = depth == 1 and bool(adapter_state.get("owningTranscript", True))
            node_type = str(raw.get("node_type") or "research")
            adapter_state["nodeType"] = node_type          # 展示用（步骤标题）
            if owning:
                adapter_state["owningNodeType"] = node_type
            adapter_state["runStartedEventId"] = event_id
            payload = {
                "nodeType": node_type,
                "attemptNo": attempt_no,
                "owningRun": owning,
            }
            if raw.get("model_backend_name"):
                payload["modelBackendName"] = str(raw["model_backend_name"])
            return EventDraft(
                "run.started",
                EventVisibility.SUMMARY,
                payload,
                at,
            )
        if raw_event == "worker_parked":
            # worker 自报「我在睡，睡到几点、为什么」（RFC D10 活动维）。
            #
            # 停靠中的 worker 什么都不产出，从平台看就是沉默 —— 而沉默按租约
            # 会衰减成 `unknown`。可它明明活着，且**只有它**知道自己要睡到几点。
            # 这条事件就是把那个事实交出来，让平台不必去猜。
            #
            # 它不是判决（"我还活着"），是事实（"我打算睡到 T，因为 R"）。
            # 死活仍由平台按租约现算：worker 说睡到 T，租约就跟到 T；真到点了
            # 还没动静，照样衰减为 unknown。自报延长的是**耐心**，不是豁免权。
            return EventDraft(
                "run.parked",
                EventVisibility.SUMMARY,
                {
                    "untilEpoch": int(raw.get("until_epoch") or 0),
                    "delaySeconds": int(raw.get("delay_s") or 0),
                    "why": str(raw.get("why") or ""),
                    "turn": raw.get("turn"),
                    # 停靠的结构化事实（#1083 第 3 条）：第几次复查、下次复查
                    # 时刻。没有它，公开视图只能说「它在睡」，说不出「它卡在
                    # blocked 上、30 分钟后自己复查」—— 而这两句话对读的人是
                    # 完全不同的两件事。
                    "park": raw.get("park") or None,
                },
                at,
            )
        if raw_event == "unattended_loop_stopped":
            # 自主循环停了 —— 这是用户最需要知道的一条，偏偏此前**没有消费方**：
            # worker 老老实实写了这个事件，平台一路不认，于是循环停机后界面上
            # 什么都没有，只剩一张永远转着的卡（yuankk 2026-09-17：从 8:30 起
            # 没有新迭代，也没有任何说明）。
            #
            # 渲染成一条 session 消息，而不是又发明一种只有我们自己看得懂的
            # 事件类型：用户界面已经会显示 session 消息，接上去就到得了人。
            detail = str(raw.get("detail") or "").strip()
            done = str(raw.get("reason") or "") == "research_complete"
            content = ("✅ 自主研究已完成，循环停止。" if done
                       else "🛑 自主研究循环已停止。")
            if detail:
                content += f"\n\n原因：{detail}"
            elif not done:
                content += f"\n\n原因：{raw.get('reason') or 'unknown'}"
            return EventDraft(
                "session.message",
                EventVisibility.SUMMARY,
                {"role": "system", "content": content},
                at,
            )
        if raw_event == "session_message":
            return EventDraft(
                "session.message",
                EventVisibility.SUMMARY,
                {
                    "role": str(raw.get("role") or "assistant"),
                    "content": str(raw.get("content") or ""),
                },
                at,
            )
        if raw_event == "artifact_created":
            return EventDraft(
                "artifact.created",
                EventVisibility.SUMMARY,
                {
                    "artifactId": str(raw.get("artifact_id") or ""),
                    "name": str(raw.get("name") or "Research artifact"),
                    "artifactType": str(raw.get("artifact_type") or "report"),
                },
                at,
            )
        if raw_event == "run_end":
            status = str(raw.get("status") or "completed")
            kind = {
                "completed": "run.completed",
                "completed_with_warning": "run.completed",
                "incomplete": "run.incomplete",
                "cancelled": "run.cancelled",
                "error": "run.failed",
                "failed": "run.failed",
                # `run.status_unknown → STALE_UNKNOWN` 这条路本来就在下面的
                # `status_by_kind` 里，只是**从 run_end 走不到** —— 于是想说
                # "运行时没了但工作没错"的调用方，发出来的值落到默认分支
                # `run.incomplete`，语义整个变了。机制存在但没接到路径。
                "stale_unknown": "run.status_unknown",
            }.get(status, "run.incomplete")
            depth = int(adapter_state.get("runDepth") or 0)
            # depth<=0：run_start 不在本次 checkpoint 窗口内（resume/legacy），
            # 保守按 owning 处理 —— 与历史行为一致。
            owning = depth <= 1 and bool(adapter_state.get("owningTranscript", True))
            adapter_state["runDepth"] = max(0, depth - 1)
            if owning:
                adapter_state.pop("owningNodeType", None)
            payload = {
                "status": status,
                "attemptNo": attempt_no,
                "owningRun": owning,
            }
            if raw.get("missing_required_outputs") is not None:
                payload["missingRequiredOutputs"] = raw["missing_required_outputs"]
            # 失败**原因**随失败事实一起过河。没有这两个字段时，前端对着
            # `run.failed` 只能说一句笼统的"失败"——2026-08-20 实测：hypothesis
            # 被模型服务读超时打死（failure_category=provider_unavailable 明明
            # 白白写在 run_end 里），用户在 UI 上看到的却只有"失败 · 31 actions"，
            # 火全撒在平台头上。
            if raw.get("failure_category"):
                payload["failureCategory"] = str(raw["failure_category"])
            if raw.get("failure_subcategory"):
                payload["failureSubcategory"] = str(raw["failure_subcategory"])
            # #1086：任务结局是和 `status` 分开的第二条轴（收尾健康 vs 活干成了
            # 没有）。这张 payload 是**按名点收**的 —— 不点名，run_end 里写得
            # 再清楚也到不了界面。`None` 照写：没报告过 ≠ 成功。
            _outcome = raw.get("node_task_outcome")
            if isinstance(_outcome, dict):
                payload["taskOutcome"] = str(_outcome.get("outcome") or "") or None
                if _outcome.get("detail"):
                    payload["taskOutcomeDetail"] = str(_outcome["detail"])[:500]
            else:
                payload["taskOutcome"] = None
            return EventDraft(kind, EventVisibility.SUMMARY, payload, at)
        if raw_event == "root_step_start":
            # 算法只有一份，见 `_root_step_id`（父子不同 id + 同节点再次派发
            # 也不同 id，两条判据缠在一起，缺一个 UI 就会把活动记错卡片）。
            step_id = _root_step_id(context.run_id, attempt_no, adapter_state)
            adapter_state["rootStepId"] = step_id
            title = str(
                raw.get("title") or f"{adapter_state.get('nodeType') or 'Research'} activity"
            )
            return EventDraft(
                "step.started",
                EventVisibility.STANDARD,
                {"stepId": step_id, "title": title},
                at,
            )
        if raw_event == "root_step_end":
            step_id = str(
                adapter_state.pop("rootStepId", None)
                or _root_step_id(context.run_id, attempt_no, adapter_state)
            )
            adapter_state.pop("activeTool", None)
            status = str(raw.get("status") or "completed")
            if status in {"completed", "completed_with_warning"}:
                return EventDraft(
                    "step.completed",
                    EventVisibility.STANDARD,
                    {"stepId": step_id, "status": status},
                    at,
                )
            return EventDraft(
                "step.failed",
                EventVisibility.STANDARD,
                {
                    "stepId": step_id,
                    "errorCode": status,
                    "errorMessage": str(raw.get("error") or f"Root step ended with {status}"),
                },
                at,
            )
        if raw_event == "subagent_call_start":
            step_id = f"step_{event_id[:24]}"
            node_type = str(raw.get("child_node_type") or "research")
            if raw.get("background") is True:
                return None
            foreground = dict(adapter_state.get("foregroundSteps") or {})
            if node_type in foreground:
                raise RecordRejectedError(
                    "overlapping foreground subagent starts cannot be associated safely"
                )
            foreground[node_type] = step_id
            adapter_state["foregroundSteps"] = foreground
            # **不在这里发 step.started。**
            #
            # 派发那一刻父节点不知道子 run 的 id（child_run_id 来自子节点跑完后的
            # summary），所以它给不出正确的 step id。之前它照发一个由父侧 event_id
            # 派生的 step，而子 transcript 又会声明自己的那一步 —— 同一个节点在
            # Trace 上出现两次，其中父侧那个永远是空的（工具事件挂在子侧）。
            #
            # 一个 step 只能有一个声明者，而只有子 run 知道自己是谁。这里只保留
            # foregroundSteps 记账（用于 pause 关联与重叠检测）。
            return None
        if raw_event in {"subagent_call_end", "subagent_call_paused"}:
            node_type = str(raw.get("child_node_type") or "research")
            foreground = dict(adapter_state.get("foregroundSteps") or {})
            step_id = foreground.pop(node_type, None)
            adapter_state["foregroundSteps"] = foreground
            if not step_id:
                child_run_id = raw.get("child_run_id")
                if not child_run_id:
                    return None
                step_id = str(child_run_id)
            if not step_id:
                return None
            status = str(raw.get("child_status") or "completed")
            if raw_event == "subagent_call_paused":
                return EventDraft(
                    "run.paused",
                    EventVisibility.SUMMARY,
                    {"reason": "waiting_human", "stepId": step_id},
                    at,
                )
            # 同理：这一步的开始与结束都由子 transcript 自己声明
            #（它的 root_step_end 会发 step.completed / step.failed）。
            # 父侧再发一次就是同一个 step 两个结束事件。
            return None
        if raw_event == "tool_call":
            step_id = adapter_state.get("rootStepId")
            if not step_id:
                return None
            tool_call_id = f"tool_{event_id[:24]}"
            tool_name = str(raw.get("name") or raw.get("tool_name") or "unknown_tool")
            adapter_state["activeTool"] = {
                "toolCallId": tool_call_id,
                "toolName": tool_name,
                "stepId": step_id,
                "turn": raw.get("turn"),
            }
            return EventDraft(
                "tool.started",
                EventVisibility.STANDARD,
                {
                    "stepId": step_id,
                    "toolCallId": tool_call_id,
                    "toolName": tool_name,
                    "arguments": raw.get("args") if isinstance(raw.get("args"), dict) else {},
                },
                at,
            )
        if raw_event in {"tool_result", "tool_long_running"}:
            active = adapter_state.get("activeTool")
            if not isinstance(active, dict):
                return None
            common = {
                "stepId": active["stepId"],
                "toolCallId": active["toolCallId"],
                "toolName": active["toolName"],
            }
            if raw_event == "tool_long_running":
                return EventDraft(
                    "tool.long_running",
                    EventVisibility.STANDARD,
                    {**common, "elapsedSeconds": int(raw.get("elapsed_seconds") or 0)},
                    at,
                )
            adapter_state.pop("activeTool", None)
            result = raw.get("result_preview")
            # 判"成没成"只认 envelope 的 status —— 结果被截断成字符串时也要认得出。
            # 上一版只对 dict 判，于是 harness 那边一超 500 字节就整体 dumps 成
            # 字符串的失败**全部落成 tool.completed**，界面显示为成功（本机库
            # 659 条）。harness 侧已改成截 body 不截 envelope，这里保留字符串
            # 兜底：老 transcript 回放和别的 producer 还会送进来。
            is_error = raw.get("status") in {"error", "failed"} or (
                isinstance(result, dict) and result.get("status") in {"error", "failed"}
            ) or (
                isinstance(result, str)
                and _TRUNCATED_ERROR_ENVELOPE.match(result) is not None
            )
            if is_error:
                return EventDraft(
                    "tool.failed",
                    EventVisibility.STANDARD,
                    {**common, **_tool_failure_fields(raw, result)},
                    at,
                )
            payload = {**common, "resultSummary": _result_summary(result)}
            if isinstance(result, dict) and isinstance(result.get("count"), int):
                payload["resultCount"] = result["count"]
            return EventDraft("tool.completed", EventVisibility.STANDARD, payload, at)
        if raw_event == "llm_response" and isinstance(raw.get("usage"), dict):
            payload = self._usage_payload(raw, adapter_state)
            return EventDraft("usage.updated", EventVisibility.SUMMARY, payload, at)
        if raw_event in {"llm_truncation_recovery_injected", "llm_retry_scheduled"}:
            return EventDraft(
                "run.retrying",
                EventVisibility.STANDARD,
                {
                    "attempt": int(raw.get("attempt") or 1),
                    "maxAttempts": int(raw.get("max_attempts") or 1),
                    "reason": str(raw_event),
                    "coverage": "partial",
                },
                at,
            )
        if raw_event == "void_turn_rolled_back":
            return EventDraft(
                "run.recovering",
                EventVisibility.STANDARD,
                {
                    "reason": "void_turn",
                    "turn": raw.get("turn"),
                    "attempt": raw.get("attempt"),
                    "maxAttempts": raw.get("max_attempts"),
                    "promptTokens": raw.get("prompt_tokens"),
                    "completionTokens": raw.get("completion_tokens"),
                },
                at,
            )
        if raw_event in {"run_paused", "loop_pause"}:
            payload = {"reason": str(raw.get("reason") or "waiting_human")}
            if raw.get("question"):
                payload["prompt"] = str(raw["question"])
            if raw.get("context"):
                payload["context"] = str(raw["context"])
            if isinstance(raw.get("options"), list):
                payload["options"] = raw["options"]
            # ── 这一次呈递：整份搬运，平台不认识它的内部字段 ────────────────
            #
            # 上一版在这里**又手写了一份投影**：把 option_details 逐字段重建成
            # {label, description, recommended}，并从 metadata 的索引重算
            # recommended。代价是选项的身份（`id`）、呈递的身份（`offer_id` /
            # `decision_id`）、以及呈递方附带的判断依据（`facts`）在这一跳全部
            # 消失 —— 于是 UI 只能拿文案当 id 回传，服务端拿它去撞合法动作集撞
            # 不上，答复被静默丢弃、原地重呈递。人点三次没反应，两边都不报错。
            #
            # 现在这里不再挑字段。`offer` 是 harness 的 `Offer.to_pause_payload()`
            # 原样（连 snake_case 都不改 —— 它是被运输的外来对象，不是平台自己的
            # 数据）。上游给 Choice 加任何字段都自动流到 UI，不用再改这一层。
            #
            # 机械判据：这个分支里**不许出现选项字段名**（label / description /
            # recommended）。出现了就是又长出一份会漏的抄件。
            meta = raw.get("metadata") or {}
            offer = raw.get(PAUSE_OFFER_KEY)
            if isinstance(offer, dict) and offer:
                payload[PAUSE_OFFER_KEY] = offer
                # 兼容视图：既有前端读的是 optionDetails / recommendedOptionIndex。
                # 它们**取自同一份呈递**，不是重建的，所以不可能与它分叉。
                details = offer.get("option_details")
                if isinstance(details, list) and details:
                    payload["optionDetails"] = details
                rec_index = offer.get("recommended_option_index")
                if isinstance(rec_index, int):
                    payload["recommendedOptionIndex"] = rec_index
            if meta.get("header"):
                payload["header"] = str(meta["header"])[:12]
            if raw.get("asking_node_type"):
                payload["askingNodeType"] = str(raw["asking_node_type"])
            if raw.get("pause_kind"):
                payload["pauseKind"] = str(raw["pause_kind"])
            if raw.get("resumable") is not None:
                payload["resumable"] = bool(raw["resumable"])
            return EventDraft(
                "run.paused",
                EventVisibility.SUMMARY,
                payload,
                at,
            )
        if raw_event == "loop_resume":
            return EventDraft(
                "run.resumed",
                EventVisibility.SUMMARY,
                {"attemptNo": attempt_no},
                at,
            )
        if raw_event in {"user_message_injected", "external_signal_received"}:
            return EventDraft(
                "run.injected",
                EventVisibility.SUMMARY,
                {"message": str(raw.get("message") or raw.get("signal") or "Instruction updated")},
                at,
            )
        if raw_event == "loop_cancelled":
            return EventDraft(
                "run.cancelled",
                EventVisibility.SUMMARY,
                {"reason": str(raw.get("reason") or "cancelled")},
                at,
            )
        if raw_event == "human_input_requested":
            return EventDraft(
                "run.paused",
                EventVisibility.SUMMARY,
                {
                    "reason": "waiting_human",
                    "detailsUnavailable": True,
                    "prompt": str(raw.get("question") or "Human input requested"),
                },
                at,
            )
        if raw_event == "decision_package_presented":
            decision_id = self._decision_id(raw, event_id, run_id=context.run_id)
            prompt = str(
                raw.get("prompt")
                or f"Decision required for {raw.get('source_node_type') or 'the current step'}"
            )
            payload = {
                "decisionId": decision_id,
                "subtype": "post_node",
                "prompt": prompt,
                "choices": decision_choices_for_event(raw),
                "reviewFailed": bool(raw.get("review_failed")),
                "reviewRetryCapped": bool(raw.get("review_retry_capped")),
                "requiredApprovalCount": (
                    context.decision_authority.required_approval_count
                    if context.decision_authority
                    else 1
                ),
            }
            recommended = raw.get("recommendedChoiceId") or raw.get("recommended_action")
            if recommended:
                payload["recommendedChoiceId"] = str(recommended)
            context_payload = dict(raw["context"]) if isinstance(raw.get("context"), dict) else {}
            # 同一个 producing run 的 package 可以被呈递多次（每次一个新
            # decision_id）；后一次呈递取代前一次。producingRunId 是"这些呈递
            # 属于同一个决定点"的作用域键，supersede 靠它找到该作废的前任。
            if raw.get("producing_run_id"):
                context_payload.setdefault("producingRunId", str(raw["producing_run_id"]))
            # 这次呈递的身份。人（和替人点推荐项的那条路）答的就是它 ——
            # 没有它，平台只能发一个 `offer_id=None` 的答复，运行时无从判它答的
            # 是哪一张，于是它会落在**下一张**卡上（2026-09-09 node20）。
            if raw.get("offer_id"):
                context_payload.setdefault("offerId", str(raw["offer_id"]))
            if context_payload:
                payload["context"] = context_payload
            return EventDraft("decision.required", EventVisibility.SUMMARY, payload, at)
        if raw_event in {
            "decision_answer_recorded",
            "decision_action_authorized",
            "decision_manual_edit_pending",
        }:
            decision_id = self._decision_id(raw, event_id, run_id=context.run_id)
            selected = str(raw.get("chosen_action") or raw.get("selectedChoiceId") or "edit")
            terminal = raw_event == "decision_answer_recorded"
            return EventDraft(
                "decision.resolved",
                EventVisibility.SUMMARY,
                {
                    "decisionId": decision_id,
                    "selectedChoiceId": selected,
                    "terminal": terminal,
                    "acceptedResponseCount": int(raw.get("accepted_response_count") or 1),
                    "requiredApprovalCount": int(raw.get("required_approval_count") or 1),
                },
                at,
            )
        if raw_event == "node_dispatch_announced":
            # 调度器派发节点前对用户说的那句话。**这是对话，不是执行日志** ——
            # 单独一个 kind，前端才能给它对话级的分量；混进 agent.message 就会
            # 跟子节点的每轮独白一个样式，实测 8 句被 51 句独白淹掉。
            return EventDraft(
                "orchestrator.said",
                EventVisibility.SUMMARY,
                {
                    "text": str(raw.get("user_note") or ""),
                    "aboutNodeType": str(raw.get("node_type") or ""),
                    "background": bool(raw.get("background")),
                },
                at,
            )
        if raw_event == "workspace_changed":
            # 一次工具调用改了哪些 Project 文件、各改多少行、**正文是什么**。
            # 此前它只走瞬时 progress 通道 —— 会话刷新即消失，UI 只能在顶部挂
            # 一块全量 diff 面板。落成 durable 事件后，前端在时间线原位渲染
            # 可展开的 "Edited foo.py +17 -0" 内联卡。
            #
            # diff 正文必须在事件里，不能等前端点开再回查：工作区是脏的，下一
            # 次工具调用就把那一刻覆盖了，事后重算只会得到一份**更晚的**、
            # 不同的 diff。证据在发生的当时落盘，判决才可以现算。
            payload = {
                "tool": str(raw.get("tool_name") or ""),
                "nodeType": str(raw.get("node_type") or ""),
                "filesChanged": int(raw.get("files_changed") or 0),
                "additions": int(raw.get("additions") or 0),
                "deletions": int(raw.get("deletions") or 0),
                # 采集端算好的结论：这次改动是不是纯平台内务（.research/ 记账）。
                # 判据只在产生事实的那一层算一次，这里原样透传。
                "internalOnly": bool(raw.get("internal_only")),
            }
            patch = raw.get("patch")
            if isinstance(patch, str) and patch.strip():
                # 采集端已经限过额，这里再限一次：边界不替上游许诺体积，
                # 换个版本的 harness（或旧事件重放）不该能把几 MB 灌进事件库。
                encoded = patch.encode("utf-8")
                truncated = bool(raw.get("patch_truncated"))
                if len(encoded) > _MAX_WORKSPACE_PATCH_BYTES:
                    patch = encoded[:_MAX_WORKSPACE_PATCH_BYTES].decode("utf-8", "ignore")
                    patch += "\n … diff 过长，平台在摄取时截断（完整内容在 Project Git 里）"
                    truncated = True
                payload["patch"] = patch
                payload["patchTruncated"] = truncated
            stats = raw.get("file_stats")
            if isinstance(stats, list):
                payload["files"] = [
                    {
                        "path": str(entry.get("path") or ""),
                        "additions": int(entry.get("additions") or 0),
                        "deletions": int(entry.get("deletions") or 0),
                        # "Created" 和 "Edited" 是两句不同的话，让采集端说，
                        # 别让 UI 从 `-0` 去猜。老事件没有这个字段。
                        "status": str(entry.get("status") or "modified"),
                    }
                    for entry in stats[:50]
                    if isinstance(entry, dict) and entry.get("path")
                ]
            else:
                # 老 harness：只有 paths，没有每文件行数。
                paths = raw.get("paths")
                if isinstance(paths, list):
                    payload["files"] = [{"path": str(p)} for p in paths[:50] if p]
            return EventDraft("workspace.changed", EventVisibility.SUMMARY, payload, at)
        if raw_event == "blocker_reported":
            # v2.1：节点报阻塞是新架构的一等机制（节点只报事实+证据+需求，
            # 由调度器 ReAct 决定怎么解）。此前平台完全不投影它 —— 节点报了
            # 阻塞，用户在 UI 上什么都看不到，只能看到 run 莫名 incomplete。
            # 归 SUMMARY 可见性：这正是用户需要知道、且往往需要人介入的那类事。
            return EventDraft(
                "run.blocked",
                EventVisibility.SUMMARY,
                {
                    "blockerId": str(raw.get("blocker_id") or ""),
                    "reportingNode": str(raw.get("reporting_node") or ""),
                    "category": str(raw.get("category") or "other"),
                    "summary": str(raw.get("summary") or "")[:4000],
                    "requestedAction": str(raw.get("requested_action") or "")[:4000],
                    "suggestedOwner": str(raw.get("suggested_owner") or "")[:200],
                    "retryableAfterChange": bool(raw.get("retryable_after_change", True)),
                    "evidencePaths": [
                        str(path) for path in (raw.get("evidence_paths") or [])
                    ][:50],
                },
                at,
            )
        if raw_event == "budget_soft_warn":
            return EventDraft(
                "budget.warning",
                EventVisibility.SUMMARY,
                {"message": str(raw.get("message") or "Budget threshold reached")},
                at,
            )
        if raw_event == "context_window":
            # 这一次请求把窗口占到了哪里（harness 每次 LLM 响应后自报）。
            # 界面上"当前上下文 xx / 窗口 · 到 70% 自动压缩"读的就是它。
            # 一条 raw 出一条事件：它与 `llm_response` 是同一时刻的两件事
            # （那条记花费，这条记占用），harness 侧就分成了两行。
            payload = self._context_window_payload(raw)
            if payload is None:
                return None
            return EventDraft("context.updated", EventVisibility.SUMMARY, payload, at)
        return None

    @staticmethod
    def _context_window_payload(raw: dict[str, Any]) -> dict[str, Any] | None:
        """把 harness 的 `context_window` 行翻成事件载荷。

        窗口不是正数就整条不要：没有窗口，百分比与压缩线都无从画起，
        而画一条按 0 算出来的"100%"比不画更糟。其余数字缺失一律取 0，
        `prompt_tokens` 缺失取 None —— 那是"服务端没报"，不是"报了 0"。
        """

        def _int(value: Any) -> int:
            try:
                return max(0, int(value or 0))
            except (TypeError, ValueError):
                return 0

        def _ratio(value: Any) -> float | None:
            if value is None:
                return None
            try:
                ratio = float(value)
            except (TypeError, ValueError):
                return None
            return ratio if 0 < ratio <= 1 else None

        window = _int(raw.get("window"))
        if window <= 0:
            return None
        raw_breakdown = raw.get("breakdown") if isinstance(raw.get("breakdown"), dict) else {}
        breakdown = {
            key: _int(raw_breakdown.get(key))
            for key in ("system", "tools", "toolResults", "summary", "framework", "conversation")
        }
        prompt_tokens = raw.get("prompt_tokens")
        last = raw.get("last_compaction")
        last_compaction = (
            {
                "turn": _int(last.get("turn")),
                "tokensBefore": _int(last.get("tokens_before")),
                "tokensAfter": _int(last.get("tokens_after")),
            }
            if isinstance(last, dict)
            else None
        )
        return {
            "turn": _int(raw.get("turn")),
            "promptTokens": _int(prompt_tokens) if prompt_tokens is not None else None,
            "estimatedTokens": _int(raw.get("est_tokens")),
            "effectiveTokens": _int(raw.get("effective_tokens")),
            "window": window,
            "configuredWindow": _int(raw.get("configured_window")) or window,
            "compressAt": _ratio(raw.get("compress_at")),
            "emergencyAt": _ratio(raw.get("emergency_at")),
            "breakdown": breakdown,
            "messageCount": _int(raw.get("n_messages")),
            "lastCompaction": last_compaction,
        }

    @staticmethod
    def _usage_payload(raw: dict[str, Any], adapter_state: dict[str, Any]) -> dict[str, Any]:
        usage = raw["usage"]
        prompt = int(usage.get("prompt_tokens") or usage.get("promptTokens") or 0)
        completion = int(usage.get("completion_tokens") or usage.get("completionTokens") or 0)
        total = int(usage.get("total_tokens") or usage.get("totalTokens") or prompt + completion)
        cost = _validated_cost(usage.get("cost"))
        coverage = str(usage.get("coverage") or "partial")
        cumulative = raw.get("usage_is_cumulative") is True or usage.get("scope") in {
            "run_cumulative",
            "cumulative",
        }
        if cumulative:
            previous = adapter_state.get("cumulativeUsage")
            if not isinstance(previous, dict):
                previous = {}
            previous_prompt = int(previous.get("promptTokens") or 0)
            previous_completion = int(previous.get("completionTokens") or 0)
            previous_total = int(previous.get("totalTokens") or 0)
            reset_observed = (
                prompt < previous_prompt
                or completion < previous_completion
                or total < previous_total
            )
            prompt_delta = prompt if reset_observed else prompt - previous_prompt
            completion_delta = completion if reset_observed else completion - previous_completion
            total_delta = total if reset_observed else total - previous_total
            cost_delta: Decimal | None = None
            if cost is not None:
                previous_cost = _validated_cost(previous.get("cost")) or Decimal("0")
                cost_delta = (
                    cost if reset_observed or cost < previous_cost else cost - previous_cost
                )
            adapter_state["cumulativeUsage"] = {
                "promptTokens": prompt,
                "completionTokens": completion,
                "totalTokens": total,
                "cost": str(cost) if cost is not None else None,
            }
            prompt = prompt_delta
            completion = completion_delta
            total = total_delta
            cost = cost_delta
            if reset_observed:
                coverage = "partial"

        payload: dict[str, Any] = {
            "promptTokens": max(0, prompt),
            "completionTokens": max(0, completion),
            "totalTokens": max(0, total),
            "cost": _json_cost_number(cost),
            "coverage": coverage if coverage in {"complete", "partial"} else "partial",
        }
        if usage.get("currency"):
            payload["currency"] = str(usage["currency"])
        return payload

    @staticmethod
    def _decision_id(raw: dict[str, Any], event_id: str, *, run_id: str) -> str:
        """一个 decision 属于**一次平台 run**。

        ## 为什么必须带上 run_id（2026-08-10 实测）

        原来 id 只派生自 harness 侧的 `producing_run_id`。那个 id **跨平台会话
        恢复是延续的** —— 恢复出来的会话继承同一个 Git 工作区，`.research` 里
        的 run 目录还在，harness 会用回同一个 producing run。

        于是恢复之后：同一个 decision_id ＋ **新的** session_id / run_id
        → `_validate_existing_decision` 的不可变判据（frozen 元组里含
        session_id / run_id）认为有人在篡改 → `IngestError` → **整轮死**。

        实测形状：v25 恢复会话跑到 experiment 提交模拟、停在高危审批那一刻，
        后端抛 `Decision immutable snapshot mutation rejected`，run 挂 44 分钟。
        **在恢复出来的会话里，任何一次审批 pause 都必然杀掉这一轮。**

        病在 id，不在守卫。守卫是对的：一个决定的条款不许在人回答之前变。
        但 session/run 不是"条款"，是"这是哪一次的决定" —— 两次不同的 run 问
        同一个问题，本来就是两个决定，人是**为这一次**批的。

        长度：`Decision.id` 是 String(128)；`decision_` + run_id(36) + 分隔 +
        harness run(~17) ≈ 63，放得下。（先量再改 —— 同一晚栽过一次
        `alembic_version.version_num` 是 varchar(32) 而我起了 34 字符的名字。）
        """
        explicit = raw.get("decision_id") or raw.get("decisionId")
        base = str(explicit) if explicit else (
            str(raw.get("producing_run_id") or "") or f"e{event_id[:24]}"
        )
        return f"decision_{run_id}_{base}"[:128]


def _reject_inconsistent_origin(origin: str, source: dict[str, Any]) -> None:
    """`adapter_derived` 必须说得出自己派生自哪些事件。

    ## 为什么把整组都搬过来

    这些不变量**读取端一直在执行**（浏览器解析器）：

        adapter_derived requires non-empty source.derivedFrom
        raw_transcript requires source.rawEvent, source.fileRef, and source.byteOffset

    但写入端不管。2026-08-12 我在同一个新事件上**连撞三次**，每次都是先写进去、
    部署、打开浏览器、发现整页空白、再回来查 —— 而每一次的报错都指向读取代码，
    离制造它的那行有十万八千里。

    契约只在出口检查，就是这个结果。区别于 `_reject_unserializable_source`
    （那条查**形状**：字段类型对不对），这条查**语义**：这个 origin 声称的
    来源，说得出具体是哪里吗。
    """
    source = source or {}
    if origin == EventOrigin.ADAPTER_DERIVED:
        derived = source.get("derivedFrom")
        if not isinstance(derived, list) or not derived:
            raise RecordRejectedError(
                f"origin={origin} claims to be derived from other events but source has no "
                f"derivedFrom: {source!r}"
            )
        return
    if origin == EventOrigin.RAW_TRANSCRIPT:
        missing = [
            key for key in ("rawEvent", "fileRef", "byteOffset")
            if source.get(key) in (None, "")
        ]
        if missing:
            raise RecordRejectedError(
                f"origin={origin} must say where in the transcript it came from; "
                f"missing {missing} in {source!r}"
            )


def _reject_unserializable_source(source: dict[str, Any]) -> None:
    """写入前就按**读取端的 schema** 校验 `source`。

    ## 为什么在这儿（2026-08-12 实测）

    我给叙述事件写了 `{"derivedFrom": "content"}`，而 `derivedFrom` 的类型是
    `list[str]`（一串事件 id）。写入一路顺利，直到用户打开会话：

        ValidationError: source.derivedFrom
          Input should be a valid list [input_value='content']
        → GET /sessions/{id}/events 500
        → UI: "Execution details unavailable"

    **一条坏事件放倒整个事件列表** —— 正在跑的那一轮什么都看不见，而写它的
    那次调用早就成功返回了。

    契约只在出口检查，等于让错误在**离制造它的人最远的地方**爆炸：写的时候
    没人拦，测试全绿，三天后在别人的浏览器里炸，报错还指向读取代码。

    所以搬到入口，用**同一个** pydantic 模型 —— 不是另写一份"允许的键"名单
    （那会和 schema 各自演化，且新字段默认漏过）。
    """
    from app.schemas.execution import ExecutionEventSourceResponse

    if not source:
        return
    try:
        ExecutionEventSourceResponse.model_validate(source)
    except Exception as exc:  # pydantic ValidationError
        raise RecordRejectedError(
            f"Event source does not match the shape the API returns: {source!r} — {exc}"
        ) from exc


class ExecutionIngestService:

    #: 短于这个长度的回复不值一条卡片（"好的"/"收到"/"明白"…）。
    #: 判据用**长度**而不是关键词名单 —— 名单对没见过的说法默认漏过
    #: （"护栏要扫盘不要写名单"的同款方向问题）。
    NARRATION_MIN_CHARS = 12

    @staticmethod
    def narration_draft(raw: dict) -> EventDraft | None:
        """把 agent 这一轮写的话落成一条可见事件；没写话就返回 None。

        ## 为什么加（wangd 2026-08-11 试用后的原话）

            「现在这个 UI 它每一步具体在干啥，我感觉看的一头雾水，
              它没有告诉这个用户，我现在干了啥？」

        UI 上是一串工具名（`Search literature — "Christmas pudding"`），而模型
        每轮都写了人话，就在 transcript 里躺着：

            [第 3 轮] 前两轮宽泛查询的结果不太理想——大量不相关论文，换更精准的词。

        用户想问的正是"它为什么突然搜圣诞布丁"，答案就在那段里。摄取器**读过**
        这条事件，只是只取了 usage，把正文丢了 —— 信息在系统里，没送到需要它的
        那一方，而这次那一方是用户。

        ## 只送达，不加工

        不总结、不改写、不判断"该不该显示"。一旦开始加工，就会有人问
        "它替我省略了什么"。前端决定怎么渲染。
        """
        if raw.get("event") == "interrupt_reply":
            # 待命轮对用户的答复 —— 这是**调度器在对话里说话**，不是节点内部
            # 独白。2026-08-18 先误映射成 `agent.message`，而同一天左栏把独白
            # 整体搬去了右栏 —— 于是用户问"现在跑的怎么样了"，调度器实实在在
            # 答了三段（transcript 里有），左栏一个字都不显示。
            #
            # `orchestrator.said` 的定义就是"调度器对用户说的话"，用它。
            # 没有 aboutNodeType：这句话不是在派谁，就是在回答。
            text = str(raw.get("text") or "").strip()
            if not text:
                return None
            try:
                occurred = _parse_time(raw.get("at"))
            except TranscriptParseError:
                return None
            return EventDraft(
                "orchestrator.said",
                EventVisibility.SUMMARY,
                {
                    "text": text,
                    "aboutNodeType": "",
                    # 这句话在回答哪条消息（插话锚定）。空 = 老事件 / CLI 投递
                    # （没有消息行），保持原渲染位置。
                    "repliesToMessageId": str(
                        raw.get("replies_to_message_id") or ""
                    ),
                },
                occurred,
            )
        if raw.get("event") in ("user_interrupt_received", "interrupt_deferred"):
            # 插话送达回执 —— **机械事实**（worker 已取走这句话 / 已排进下一轮），
            # 不是调度器说的话。旧路径把它发在 progress 通道：不落库、会被子节点
            # 的下一条工具进度覆盖，用户按下回车后看到的仍是
            # "Analyzing tool results"（2026-08-18 实测 2 分钟黑屏）。
            # 措辞归前端（按 deferred 分档），这里只送结构化事实。
            try:
                occurred = _parse_time(raw.get("at"))
            except TranscriptParseError:
                return None
            return EventDraft(
                "interrupt.acknowledged",
                EventVisibility.SUMMARY,
                {
                    "repliesToMessageId": str(
                        raw.get("replies_to_message_id")
                        or raw.get("message_id")
                        or ""
                    ),
                    "echo": str(raw.get("text") or "")[:200],
                    "deferred": raw.get("event") == "interrupt_deferred",
                },
                occurred,
            )
        if raw.get("event") != "llm_response":
            return None

        # `content` 是权威的完整正文。`content_preview` 是老 transcript（以及
        # 其它写入点）里那个硬截 500 字、**不留截断标记**的字段 —— 退回去读它
        # 的时候，我们无从判断它完不完整，所以就**如实说这是预览**，不去猜。
        # 猜错的两个方向都很难看：把半截话当完整发言，或者给完整发言加个假的
        # 省略号。
        text = str(raw.get("content") or "").strip()
        preview_only = False
        if not text:
            text = str(raw.get("content_preview") or "").strip()
            preview_only = bool(text)
        if len(text) < ExecutionIngestService.NARRATION_MIN_CHARS:
            return None
        # 时间缺失 / 不合法时不抛：这条是"观察"，不该打断摄取主流程。
        try:
            occurred = _parse_time(raw.get("at"))
        except TranscriptParseError:
            return None
        return EventDraft(
            "agent.message",
            EventVisibility.STANDARD,
            {
                "text": text,
                "turn": int(raw.get("turn") or 0),
                "nodeType": str(raw.get("node_type") or "") or None,
                "previewOnly": preview_only,
            },
            occurred,
        )

    def __init__(
        self,
        *,
        adapter: TranscriptAdapter | None = None,
        redaction_policy: RedactionPolicy | None = None,
    ) -> None:
        self.adapter = adapter or TranscriptAdapter()
        self.redaction_policy = redaction_policy or DEFAULT_REDACTION_POLICY

    async def ingest_raw_record(
        self,
        db: AsyncSession,
        *,
        context: IngestContext,
        file_identity: str,
        byte_offset: int,
        raw_line: bytes,
        raw: dict[str, Any],
        adapter_state: dict[str, Any],
    ) -> IngestOutcome:
        """Ingest one raw record; a content-rejected record becomes a witness event.

        记录层守卫的裁决权只到"这条记录不落库"为止。`RecordRejectedError`
        （内容不变量被违反）在这里降级成 `record.rejected` 见证事件：拒收的
        事实、原因、字节位置全部 durable，turn 继续活着。库级完整性错误
        （scope 消失/序列分配失败）照旧向上抛 —— 那时候连见证都写不进去。

        ## 这里**不能**包 SAVEPOINT（2026-08-17 实测，别再加回来）

        第一版把严格路径包进 `db.begin_nested()`，想的是"拒收发生在半途时
        回滚到记录开始前"。代价是把一个可幸存的状况变成了硬错误：SSE 观察者
        断开后这一轮转入后台继续跑（detached execution），请求级 session 随
        断开被拆掉，而 savepoint 正好跨在那上面 ——

            sqlalchemy.exc.OperationalError: no such savepoint: sa_savepoint_1
            [SQL: RELEASE SAVEPOINT sa_savepoint_1]

        摄取当场抛错、run 永远到不了 completed（实测三次挂一次，且这个文件里
        本来就有两处自己的 `begin_nested`，嵌套生命周期更难对齐）。

        而它要防的那件事**根本不会发生**：五个 `RecordRejectedError` 抛出点
        全在写入之前（`_reject_*` 在 `_insert_event` 顶部、早于任何 `db.add`；
        decision 快照校验在 `_insert_event` 之前）。此刻会话里可能挂着的只有
        scope 行、root step、叙述事件 —— 那些是从同一行**如实派生**出来的，
        本来就该留下，回滚它们反而是错的。
        """
        try:
            return await self._ingest_raw_record_strict(
                db,
                context=context,
                file_identity=file_identity,
                byte_offset=byte_offset,
                raw_line=raw_line,
                raw=raw,
                adapter_state=adapter_state,
            )
        except RecordRejectedError as exc:
            return await self._record_rejection_witness(
                db,
                context=context,
                file_identity=file_identity,
                byte_offset=byte_offset,
                raw_line=raw_line,
                raw=raw,
                reason=str(exc),
            )

    async def _record_rejection_witness(
        self,
        db: AsyncSession,
        *,
        context: IngestContext,
        file_identity: str,
        byte_offset: int,
        raw_line: bytes,
        raw: dict[str, Any],
        reason: str,
    ) -> IngestOutcome:
        """Durably record that one record was rejected, without judging the run."""
        raw_line_hash = hashlib.sha256(raw_line).hexdigest()
        source_identity = _hash_parts(context.run_id, file_identity, byte_offset, raw_line_hash)
        event_id = _hash_parts(context.session_id, source_identity, ADAPTER_VERSION)
        session, _run = await self._lock_scope(db, context)
        safe_reason = self.redaction_policy.sanitize(reason[:500]).value
        witness = await self._insert_event(
            db,
            session=session,
            context=context,
            # 幂等：重放同一条被拒记录 → 同一个见证事件 id → 去重早退，
            # 不会每次重启多长一条。
            event_id=_hash_parts(event_id, "rejected"),
            draft=EventDraft(
                "record.rejected",
                EventVisibility.SUMMARY,
                {
                    "reason": str(safe_reason),
                    "rawEvent": str(raw.get("event") or ""),
                },
                _parse_time(raw.get("at")),
            ),
            origin=EventOrigin.RECONCILIATION,
            source={
                "rawEvent": str(raw.get("event") or "unknown"),
                "fileRef": f"file_{_hash_parts(context.tenant_id, context.run_id, file_identity)[:24]}",
                "byteOffset": byte_offset,
            },
            source_identity=_hash_parts(source_identity, "rejected"),
            file_identity=f"{file_identity}#rejected",
            byte_offset=byte_offset,
            raw_line_hash=_hash_parts(raw_line_hash, "rejected"),
        )
        log.warning(
            "Rejected one transcript record (run=%s, file=%s, offset=%s): %s",
            context.run_id,
            file_identity,
            byte_offset,
            reason,
        )
        return IngestOutcome(witness, False, (witness.id,))

    async def _ingest_raw_record_strict(
        self,
        db: AsyncSession,
        *,
        context: IngestContext,
        file_identity: str,
        byte_offset: int,
        raw_line: bytes,
        raw: dict[str, Any],
        adapter_state: dict[str, Any],
    ) -> IngestOutcome:
        self._preflight_raw(raw, context=context)

        # ── 子节点的事件记在它自己名下（2026-08-11）────────────────────────
        #
        # 实测（会话 bc1c7343）：6 份子节点 transcript 都摄取了，但事件全挂在
        # 顶层那条 run 上（run_id 只有 1 个）。于是 UI 只能平铺成一坨
        # 「Research activity — 124 recorded actions」，看不出哪个动作属于谁。
        # wangd 原话：「literature 都结束了，然后开始 hypothesis 了，它还是在
        # 下面显示一大坨」。
        #
        # 身份只在每份 transcript 的第一条 run_start 里（后续事件只带
        # tenant/session），而 adapter_state 本来就按 file_identity 分桶 ——
        # 记一次，同文件后续都能归位。
        #
        # 在**入口**派生子 context，下游（_insert_event / step 归并 / decision
        # 作用域）一处都不用改 —— 它们读的都是 context.run_id。
        # 这一次提交的身份：worker 盖在每条 transcript 记录上（P1-5）。
        # 记进 adapter_state，本文件后续事件都用它 —— 包括那些 harness 没有
        # 逐条带上的（历史记录、adapter 自己派生的 step 事件）。
        if raw.get("submission_id"):
            adapter_state["submissionId"] = str(raw["submission_id"])

        # 这份 transcript 的事件归谁 —— **一个答案，两个来源同一个事实**：
        # 本轮读到过它的 `run_start`（内存），或者这份文件此前的事件已经落库
        # （持久，见 `harness_transcript_ingest._child_run_of`）。后者是前者的
        # 落盘结果，不是第二本账 —— 人答复 pause 是新的一轮，states 清零、又从
        # 字节偏移接着读，那条 run_start 不会被重读，只能从库里取回来（#947）。
        child = remember_child_identity(adapter_state, raw)
        child_run_id = (
            child.run_id if child is not None
            else adapter_state.get(RECOVERED_CHILD_RUN_KEY)
        )
        if child_run_id and child_run_id != context.run_id:
            context = replace(
                context,
                # harness 的 `sub_run_id`（`_orchestrator->_curator@d1`）是它在
                # **本次调用树里的位置**，只在一个会话内唯一。平台的 `Run.id`
                # 要求全局唯一 —— 直接拿来用，换个会话再跑一次 curator 就撞上
                # 老会话那一行：
                #
                #     IngestError: Run identity conflicts with existing tenant scope
                #
                # 而这恰好发生在**恢复**路径上：会话被打断 → recover 开新会话 →
                # 重跑同一批节点 → 第一条 run 当场失败。恢复机制形同虚设。
                #
                # 用平台侧那个全局唯一的父 id 限定它。父子关系顺带写在 id 里，
                # 看一眼就知道谁派的。
                # 分隔符是 `::` 不是 `/`：这个 id 要在 URL 里旅行
                # （`GET /api/v1/runs/{run_id}`），而 `/` 是**路径分隔符** ——
                # 用它当限定符，路由会把 id 切成两段，取详情永远 404，
                # UI 上就是「Execution record unavailable」（2026-08-12 实测，
                # 百分号编码也救不回来）。
                #
                # 「保证唯一性的分隔符」和「URL 的路径分隔符」是两件事，
                # 用同一个字符就等于让它们互相破坏。
                run_id=f"{context.run_id}::{child_run_id}",
                # 父 run 一律取**平台**这一侧的 run id，不用 transcript 里那个。
                #
                # harness 自己的 `parent_run_id` 是它的编排器名
                # （`orchestrator__<project>__session__<session>`）—— 那是**另一个
                # 标识空间**，平台的 `runs` 表里根本没有这个 id。写进去之后
                # "谁是谁的父 run" 这个问题在平台侧永远无解：
                #
                #     GET /sessions/{id}/events?runId=run_357d…&includeChildren=true
                #     → parent_run_id == run_357d… 永远不成立 → 一个子节点都取不到
                #
                # 实测就是这样：UI 上仍然只有编排器自己那一坨 197 条动作。
                # 两个标识空间被当成了一个 —— 名字像、含义不同，而分叉时不报错。
                parent_run_id=context.run_id,
            )

        redaction = self.redaction_policy.sanitize(raw)
        sanitized = redaction.value if isinstance(redaction.value, dict) else {"redacted": True}
        raw_line_hash = hashlib.sha256(raw_line).hexdigest()
        source_identity = _hash_parts(context.run_id, file_identity, byte_offset, raw_line_hash)
        event_id = _hash_parts(context.session_id, source_identity, ADAPTER_VERSION)

        session, run = await self._lock_scope(db, context)
        existing = await db.scalar(
            select(ExecutionEvent).where(
                ExecutionEvent.tenant_id == context.tenant_id,
                ExecutionEvent.run_id == context.run_id,
                ExecutionEvent.file_identity == file_identity,
                ExecutionEvent.byte_offset == byte_offset,
                ExecutionEvent.raw_line_hash == raw_line_hash,
            )
        )
        if existing:
            self._replay_adapter_state(
                existing=existing,
                raw=sanitized,
                adapter_state=adapter_state,
            )
            return IngestOutcome(existing, True, (existing.id,))

        emitted_ids: list[str] = []
        raw_event_name = sanitized.get("event")
        if raw_event_name == "tool_call":
            root = await self._ensure_root_step(
                db,
                session=session,
                run=run,
                context=context,
                adapter_state=adapter_state,
                occurred_at=_parse_time(sanitized.get("at")),
                fallback_source_event_id=event_id,
            )
            if root:
                emitted_ids.append(root.id)
        elif raw_event_name == "run_end":
            # 开卡是懒的（第一条 tool_call），关卡也得是懒的 —— 否则就要求每条终态
            # 路径都记得补发一个 `root_step_end`，而它们从来不记得（全仓 0 次）。
            closed = await self._close_root_step(
                db,
                session=session,
                context=context,
                adapter_state=adapter_state,
                raw=sanitized,
                occurred_at=_parse_time(sanitized.get("at")),
                fallback_source_event_id=event_id,
            )
            if closed:
                emitted_ids.append(closed.id)

        # ── agent 每轮写的话，额外落一条（2026-08-11）────────────────────
        #
        # `llm_response` 的 draft 位子被 usage.updated 占着（一条 raw 出一个
        # draft），而正文和 token 数是**两件事**：一个给用户看"它在干什么"，
        # 一个给记账。合成一条就必然丢一个 —— 之前丢的是正文。
        #
        # wangd 试用后的原话：「每一步具体在干啥，我感觉看的一头雾水」。
        # 模型其实每轮都写了人话，只是没送到。
        narration = self.narration_draft(sanitized)
        if narration is not None:
            narration_event = await self._insert_event(
                db,
                session=session,
                context=context,
                # 叙述就是「回答」本身，它最需要锚回提问。
                adapter_state_submission_id=str(adapter_state.get('submissionId') or ''),
                event_id=_hash_parts(event_id, "narration"),
                draft=narration,
                # `RAW_TRANSCRIPT` 而不是 `ADAPTER_DERIVED`：这条叙述是从 raw
                # 行**直接读出来的**（和同一行产出的 `usage.updated` 一模一样），
                # 不是从别的事件推导出来的。
                #
                # 选错的代价是真的：`adapter_derived` 的契约要求带非空
                # `derivedFrom`（一串它派生自的事件 id），而叙述没有源事件。
                # 这条契约只在**浏览器端解析器**里执行，于是违反它的后果不是
                # "这条事件不显示"，而是整页 `eventsQuery` 抛异常 → 那一轮的
                # 执行记录一片空白（2026-08-12 实测，查了很久）。
                origin=EventOrigin.RAW_TRANSCRIPT,
                # `derivedFrom` 是**事件 id 的列表**（见下面 revision 那两处）。
                # 我一开始写成 `"content"`（想说"从 content 字段派生"）—— 类型和
                # 语义都错了，而写入端没人校验，直到 `/sessions/{id}/events`
                # 序列化时才炸，且**一条坏事件放倒整个列表**：UI 上是
                # "Execution details unavailable"，正在跑的那一轮什么都看不见。
                # 现在 `_insert_event` 会在写入时按同一个 schema 校验。
                # 三件套齐全 —— `raw_transcript` 的契约要求
                # rawEvent + fileRef + byteOffset。这条叙述和同一行产出的
                # `usage.updated` 指向**同一个字节位置**，事实如此。
                source={
                    "rawEvent": str(sanitized.get("event") or "llm_response"),
                    "fileRef": f"file_{_hash_parts(context.tenant_id, context.run_id, file_identity)[:24]}",
                    "byteOffset": byte_offset,
                },
                source_identity=_hash_parts(source_identity, "narration"),
                # 去重唯一键是 (tenant, run, file_identity, byte_offset,
                # raw_line_hash) —— 一条 raw 行只能落一条事件。叙述是从**同一行**
                # 派生的第二条，所以给它一个派生标识，别去撞那条约束（那条约束
                # 是幂等重放的基石，绕不得）。
                file_identity=f"{file_identity}{NARRATION_IDENTITY_SUFFIX}",
                byte_offset=byte_offset,
                raw_line_hash=_hash_parts(raw_line_hash, "narration"),
            )
            emitted_ids.append(narration_event.id)

        draft = self.adapter.adapt(
            sanitized,
            context=context,
            adapter_state=adapter_state,
            event_id=event_id,
        )
        if draft is None:
            if redaction.warnings:
                source = {
                    "rawEvent": str(sanitized.get("event")),
                    "fileRef": (
                        f"file_{_hash_parts(context.tenant_id, context.run_id, file_identity)[:24]}"
                    ),
                    "byteOffset": byte_offset,
                }
                warning = await self._insert_event(
                    db,
                    session=session,
                    context=context,
                    # 脱敏警告也属于这一次提交。
                    adapter_state_submission_id=str(adapter_state.get('submissionId') or ''),
                    event_id=event_id,
                    draft=EventDraft(
                        "redaction.warning",
                        EventVisibility.SUMMARY,
                        {"codes": list(redaction.warnings)},
                        _parse_time(sanitized.get("at")),
                    ),
                    origin=EventOrigin.RAW_TRANSCRIPT,
                    source=source,
                    source_identity=source_identity,
                    file_identity=file_identity,
                    byte_offset=byte_offset,
                    raw_line_hash=raw_line_hash,
                )
                emitted_ids.append(warning.id)
                return IngestOutcome(warning, False, tuple(emitted_ids))
            return IngestOutcome(None, False, tuple(emitted_ids))
        if draft.kind == "decision.required" and not context.decision_authority:
            raise DecisionAuthorityRequiredError(
                "DecisionAuthority snapshot is required before allocating a Decision event"
            )
        if draft.kind == "decision.required":
            await self._validate_existing_decision(
                db,
                payload=draft.payload,
                context=context,
            )

        source = {
            "rawEvent": str(sanitized.get("event")),
            "fileRef": f"file_{_hash_parts(context.tenant_id, context.run_id, file_identity)[:24]}",
            "byteOffset": byte_offset,
        }
        event = await self._insert_event(
            db,
            session=session,
            context=context,
            adapter_state_submission_id=str(adapter_state.get('submissionId') or ''),
            event_id=event_id,
            draft=draft,
            origin=EventOrigin.RAW_TRANSCRIPT,
            source=source,
            source_identity=source_identity,
            file_identity=file_identity,
            byte_offset=byte_offset,
            raw_line_hash=raw_line_hash,
        )
        emitted_ids.append(event.id)
        await self._apply_projection(db, event=event, run=run, context=context)

        if redaction.warnings:
            warning = await self._insert_redaction_warning(
                db,
                session=session,
                context=context,
                source_event=event,
                warnings=redaction.warnings,
            )
            emitted_ids.append(warning.id)
        return IngestOutcome(event, False, tuple(emitted_ids))

    @staticmethod
    def _preflight_raw(raw: dict[str, Any], *, context: IngestContext) -> None:
        _parse_time(raw.get("at"))
        if raw.get("event") == "llm_response" and isinstance(raw.get("usage"), dict):
            _validated_cost(raw["usage"].get("cost"))
        if raw.get("event") != "decision_package_presented":
            return
        decision_choices_for_event(raw)  # unknown action set still fails closed
        if not context.decision_authority:
            raise DecisionAuthorityRequiredError(
                "DecisionAuthority snapshot is required before ingesting a Decision"
            )

    async def create_command(
        self,
        db: AsyncSession,
        *,
        context: IngestContext,
        kind: str,
        idempotency_key: str,
        payload: dict[str, Any],
    ) -> Command:
        if not context.actor_user_id or not idempotency_key:
            raise ValueError("actor_user_id and idempotency_key are required")
        redacted = self.redaction_policy.sanitize(payload).value
        safe_payload = redacted if isinstance(redacted, dict) else {"redacted": True}
        await self._lock_scope(db, context)
        existing = await db.scalar(
            select(Command).where(
                Command.tenant_id == context.tenant_id,
                Command.actor_user_id == context.actor_user_id,
                Command.idempotency_key == idempotency_key,
            )
        )
        if existing:
            same_request = (
                existing.kind == kind
                and existing.run_id == context.run_id
                and existing.payload == safe_payload
            )
            if not same_request:
                raise IdempotencyConflictError("idempotency key reused for a different command")
            return existing
        command = Command(
            tenant_id=context.tenant_id,
            workspace_id=context.workspace_id,
            project_id=context.project_id,
            session_id=context.session_id,
            run_id=context.run_id,
            actor_user_id=context.actor_user_id,
            kind=kind,
            idempotency_key=idempotency_key,
            payload=safe_payload,
        )
        db.add(command)
        await db.flush()
        return command

    async def complete_command(
        self,
        db: AsyncSession,
        *,
        tenant_id: str,
        command_id: str,
        result: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
    ) -> Command:
        """Persist only sanitized command output and terminal error details."""
        command = await db.scalar(
            select(Command)
            .where(Command.tenant_id == tenant_id, Command.id == command_id)
            .with_for_update()
        )
        if not command:
            raise RecordRejectedError("Command not found in tenant scope")
        if result is not None:
            safe_result = self.redaction_policy.sanitize(result).value
            command.result = safe_result if isinstance(safe_result, dict) else {"redacted": True}
        if error is not None:
            safe_error = self.redaction_policy.sanitize(error).value
            command.error = safe_error if isinstance(safe_error, dict) else {"redacted": True}
        await db.flush()
        return command

    def _replay_adapter_state(
        self,
        *,
        existing: ExecutionEvent,
        raw: dict[str, Any],
        adapter_state: dict[str, Any],
    ) -> None:
        """Rebuild deterministic Adapter state without replaying projections."""
        if existing.kind == "run.started":
            adapter_state["nodeType"] = existing.payload.get("nodeType") or "research"
            adapter_state["runStartedEventId"] = existing.id
            return
        if raw.get("event") == "root_step_start":
            adapter_state["rootStepId"] = existing.payload.get("stepId")
            return
        if raw.get("event") in {"root_step_end", "run_end"}:
            adapter_state.pop("rootStepId", None)
            adapter_state.pop("activeTool", None)
            return
        if existing.kind == "step.started" and raw.get("background") is not True:
            node_type = str(raw.get("child_node_type") or "research")
            foreground = dict(adapter_state.get("foregroundSteps") or {})
            foreground[node_type] = existing.payload.get("stepId")
            adapter_state["foregroundSteps"] = foreground
            return
        if existing.kind in {"step.completed", "step.failed"}:
            node_type = str(raw.get("child_node_type") or "research")
            foreground = dict(adapter_state.get("foregroundSteps") or {})
            foreground.pop(node_type, None)
            adapter_state["foregroundSteps"] = foreground
            return
        if existing.kind == "tool.started":
            adapter_state["rootStepId"] = existing.payload.get("stepId")
            adapter_state["activeTool"] = {
                "stepId": existing.payload.get("stepId"),
                "toolCallId": existing.payload.get("toolCallId"),
                "toolName": existing.payload.get("toolName"),
                "turn": raw.get("turn"),
            }
            return
        if existing.kind in {"tool.completed", "tool.failed"}:
            adapter_state.pop("activeTool", None)
            return
        if existing.kind == "usage.updated" and isinstance(raw.get("usage"), dict):
            self.adapter._usage_payload(raw, adapter_state)

    async def respond_to_decision(
        self,
        db: AsyncSession,
        *,
        tenant_id: str,
        decision_id: str,
        actor_user_id: str,
        choice_id: str,
        has_decision_respond_capability: bool,
        expected_accepted_response_count: int | None = None,
        actor_authority_subjects: set[str] | None = None,
    ) -> Decision:
        decision = await db.scalar(
            select(Decision)
            .where(Decision.tenant_id == tenant_id, Decision.id == decision_id)
            .with_for_update()
        )
        if not decision:
            raise DecisionResponseRejectedError("Decision not found")
        if not has_decision_respond_capability:
            raise DecisionResponseRejectedError(
                "decision.respond capability required",
                code="decision_respond_forbidden",
            )
        # 乐观并发用的是**应答条数**本身，不是一个另存的计数器 ——
        # 那个计数器就是 len(accepted_responses)，一份事实两处记就会分叉。
        if (
            expected_accepted_response_count is not None
            and len(decision.accepted_responses or []) != expected_accepted_response_count
        ):
            raise DecisionResponseRejectedError(
                "Decision changed after the response form was loaded",
                code="decision_response_stale",
            )
        if decision.status not in {
            DecisionStatus.PENDING,
            DecisionStatus.PARTIALLY_APPROVED,
        }:
            raise DecisionResponseRejectedError("Decision is not open", code="decision_not_open")
        expires_at = decision.expires_at
        if expires_at and expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        if expires_at and expires_at <= datetime.now(UTC):
            decision.status = DecisionStatus.EXPIRED
            raise DecisionResponseRejectedError("Decision has expired", code="decision_expired")

        claims = set(actor_authority_subjects or ()) | {actor_user_id}
        if not claims.intersection(decision.authority_subjects):
            raise DecisionResponseRejectedError(
                "actor is outside the frozen DecisionAuthority",
                code="decision_authority_forbidden",
            )
        valid_choices = {
            str(choice.get("choiceId"))
            for choice in decision.choices
            if isinstance(choice, dict) and choice.get("choiceId")
        }
        if choice_id not in valid_choices:
            raise DecisionResponseRejectedError(
                "stable choiceId is not in the frozen action set",
                code="invalid_decision_choice",
            )

        responses = list(decision.accepted_responses or [])
        prior = next(
            (response for response in responses if response.get("actorUserId") == actor_user_id),
            None,
        )
        if prior:
            if prior.get("choiceId") != choice_id:
                raise DecisionResponseRejectedError(
                    "authority subject already responded differently",
                    code="decision_response_conflict",
                )
            return decision
        responses.append(
            {
                "actorUserId": actor_user_id,
                "choiceId": choice_id,
                "at": datetime.now(UTC).isoformat(),
            }
        )
        decision.accepted_responses = responses
        choice_counts = Counter(response["choiceId"] for response in responses)
        if choice_counts[choice_id] >= decision.required_approval_count:
            decision.status = DecisionStatus.RESOLVED
            decision.selected_choice_id = choice_id
            decision.resolved_at = datetime.now(UTC)
        else:
            decision.status = DecisionStatus.PARTIALLY_APPROVED
        await db.flush()
        await db.refresh(decision)
        return decision

    async def _lock_scope(
        self, db: AsyncSession, context: IngestContext
    ) -> tuple[SessionProjection, Run]:
        await self._ensure_scope(db, context)
        session = await db.scalar(
            select(SessionProjection)
            .where(
                SessionProjection.tenant_id == context.tenant_id,
                SessionProjection.workspace_id == context.workspace_id,
                SessionProjection.project_id == context.project_id,
                SessionProjection.session_id == context.session_id,
            )
            .with_for_update()
        )
        if not session:
            raise IngestError("Session scope disappeared during ingest")
        run = await db.scalar(
            select(Run).where(Run.tenant_id == context.tenant_id, Run.id == context.run_id)
        )
        if not run:
            raise IngestError("Run scope disappeared during ingest")
        return session, run

    async def _ensure_scope(self, db: AsyncSession, context: IngestContext) -> None:
        session = await db.scalar(
            select(SessionProjection).where(SessionProjection.session_id == context.session_id)
        )
        if session and (
            session.tenant_id,
            session.workspace_id,
            session.project_id,
        ) != (context.tenant_id, context.workspace_id, context.project_id):
            raise IngestError("Session identity conflicts with existing tenant scope")
        if not session:
            session = SessionProjection(
                tenant_id=context.tenant_id,
                workspace_id=context.workspace_id,
                project_id=context.project_id,
                session_id=context.session_id,
                initiating_user_id=context.actor_user_id,
            )
            try:
                async with db.begin_nested():
                    db.add(session)
                    await db.flush()
            except IntegrityError:
                session = await db.scalar(
                    select(SessionProjection).where(
                        SessionProjection.session_id == context.session_id
                    )
                )
                if not session:
                    raise IngestError("Session creation raced but no durable row exists") from None
                if (
                    session.tenant_id,
                    session.workspace_id,
                    session.project_id,
                ) != (context.tenant_id, context.workspace_id, context.project_id):
                    raise IngestError(
                        "Session identity conflicts with concurrently created tenant scope"
                    ) from None
        elif context.actor_user_id and not session.initiating_user_id:
            session.initiating_user_id = context.actor_user_id

        run = await db.scalar(select(Run).where(Run.id == context.run_id))
        if run and (
            run.tenant_id,
            run.workspace_id,
            run.project_id,
            run.session_id,
        ) != (
            context.tenant_id,
            context.workspace_id,
            context.project_id,
            context.session_id,
        ):
            raise IngestError("Run identity conflicts with existing tenant scope")
        if not run:
            run = Run(
                id=context.run_id,
                tenant_id=context.tenant_id,
                workspace_id=context.workspace_id,
                project_id=context.project_id,
                session_id=context.session_id,
                parent_run_id=context.parent_run_id,
            )
            try:
                async with db.begin_nested():
                    db.add(run)
                    await db.flush()
            except IntegrityError:
                run = await db.scalar(select(Run).where(Run.id == context.run_id))
                if not run:
                    raise IngestError("Run creation raced but no durable row exists") from None
                if (
                    run.tenant_id,
                    run.workspace_id,
                    run.project_id,
                    run.session_id,
                ) != (
                    context.tenant_id,
                    context.workspace_id,
                    context.project_id,
                    context.session_id,
                ):
                    raise IngestError(
                        "Run identity conflicts with concurrently created tenant scope"
                    ) from None

    async def _next_sequence(self, db: AsyncSession, session: SessionProjection) -> int:
        """分配这个会话时间线上的下一个序号 —— 由数据库自己加，不在内存里加。

        消息也从**同一个**发生器领号（`sessions.allocate_session_sequence`）。
        两个计数器就是两条时间线，而把两条时间线上的号拿来比大小不会报错，
        只会静默地把顺序画错（见 `SessionProjection.next_sequence`）。

        原本是 `session.next_sequence += 1`。`_lock_scope` 确实用
        `with_for_update()` 在库里锁了这一行，但 SQLAlchemy 对**已在 identity map
        里**的对象不会用 SELECT 回来的列值覆盖已加载的属性，而
        `expire_on_commit=False`（`app/database.py:43`）让对象 commit 之后继续留在
        map 里。于是**锁是数据库的锁，加的却是内存里的值** —— 两件事被当成了一件。

        代价是真的：2026-08-11 会话 bc1c7343 撞 `uq_events_sequence` 的 613 号，
        整条 run 当场 `execution_failed`。实测 8 次并发分配发出
        `[1,2,2,2,2,2,2,3]`。

        `UPDATE … SET n = n + 1 RETURNING n` 一条语句完成加锁、自增、读回，中间
        没有内存副本可以走岔。
        """
        value = await db.scalar(
            update(SessionProjection)
            .where(
                SessionProjection.tenant_id == session.tenant_id,
                SessionProjection.session_id == session.session_id,
            )
            .values(next_sequence=SessionProjection.next_sequence + 1)
            .returning(SessionProjection.next_sequence)
            .execution_options(synchronize_session=False)
        )
        if value is None:  # 行不见了 —— 与其发一个猜的号，不如当场吵
            raise IngestError("Session row vanished while allocating an event sequence")
        # 让内存里的实例跟上真值，但不标记为脏：真相在库里，这里只是抄一份给同一
        # 事务里后续代码看，不能反过来把它写回去。
        set_committed_value(session, "next_sequence", value)
        return int(value)

    async def _insert_event(
        self,
        db: AsyncSession,
        *,
        session: SessionProjection,
        context: IngestContext,
        event_id: str,
        draft: EventDraft,
        origin: str,
        source: dict[str, Any],
        source_identity: str | None = None,
        file_identity: str | None = None,
        byte_offset: int | None = None,
        raw_line_hash: str | None = None,
        adapter_state_submission_id: str = "",
    ) -> ExecutionEvent:
        _reject_unserializable_source(source)
        _reject_inconsistent_origin(origin, source)
        # ── 这一次提交的身份，跟着每一条事件走（RFC P1-5 / 附录 S1）─────────
        #
        # worker 把它盖在每条 transcript 记录上（`State.append_transcript`），
        # adapter 记在 `adapter_state` 里，这里给**每一条**投影出来的事件补上。
        #
        # 盖在这个唯一出口上，而不是逐个事件类型手工带锚字段：后者是名单式
        # 护栏，新事件默认漏 —— 而"漏"的表现是渲染错位，不是报错。今晚已经
        # 手工修过两次同款（回执锚点、右栏箭头跳到最后一张同类卡）。
        submission_id = adapter_state_submission_id
        if submission_id and "submissionId" not in draft.payload:
            draft = replace(draft, payload={**draft.payload, "submissionId": submission_id})
        event = ExecutionEvent(
            id=event_id,
            tenant_id=context.tenant_id,
            workspace_id=context.workspace_id,
            project_id=context.project_id,
            session_id=context.session_id,
            run_id=context.run_id,
            parent_run_id=context.parent_run_id,
            attempt_no=context.attempt_no,
            sequence=await self._next_sequence(db, session),
            occurred_at=draft.occurred_at,
            origin=origin,
            source=source,
            kind=draft.kind,
            visibility=draft.visibility,
            payload=draft.payload,
            source_identity=source_identity,
            file_identity=file_identity,
            byte_offset=byte_offset,
            raw_line_hash=raw_line_hash,
            adapter_version=ADAPTER_VERSION,
        )
        db.add(event)
        await db.flush()
        return event

    async def _ensure_root_step(
        self,
        db: AsyncSession,
        *,
        session: SessionProjection,
        run: Run,
        context: IngestContext,
        adapter_state: dict[str, Any],
        occurred_at: datetime,
        fallback_source_event_id: str,
    ) -> ExecutionEvent | None:
        if adapter_state.get("rootStepId"):
            return None
        # 与两个 root_step_* 分支同一份算法（`_root_step_id`）。这里曾经是**唯一
        # 跑得到**的那处，却留着最旧的写法：同一个节点被再次派发时算出与上一次
        # 相同的 id，`step.started` 被幂等吞掉 —— UI 从此说"当前没有节点在跑"。
        step_id = _root_step_id(context.run_id, context.attempt_no, adapter_state)
        event_id = _hash_parts(context.session_id, step_id, ADAPTER_VERSION)
        existing = await db.get(ExecutionEvent, event_id)
        adapter_state["rootStepId"] = step_id
        if existing:
            return None
        source_event_id = str(adapter_state.get("runStartedEventId") or fallback_source_event_id)
        return await self._insert_event(
            db,
            session=session,
            context=context,
            adapter_state_submission_id=str(adapter_state.get('submissionId') or ''),
            event_id=event_id,
            draft=EventDraft(
                "step.started",
                EventVisibility.STANDARD,
                {
                    "stepId": step_id,
                    "title": (
                        f"{run.node_type or adapter_state.get('nodeType') or 'Research'} activity"
                    ),
                },
                occurred_at,
            ),
            origin=EventOrigin.ADAPTER_DERIVED,
            source={"derivedFrom": [source_event_id]},
        )

    #: run 的终态 → 这一步的终态。step 没有自己的真相源 —— run 的结论就是它的结论。
    _STEP_STATUS_FROM_RUN = {
        "completed": "step.completed",
        "completed_with_warning": "step.completed",
    }

    async def _close_root_step(
        self,
        db: AsyncSession,
        *,
        session: SessionProjection,
        context: IngestContext,
        adapter_state: dict[str, Any],
        raw: dict[str, Any],
        occurred_at: datetime,
        fallback_source_event_id: str,
    ) -> ExecutionEvent | None:
        """run 走到终态时，把 `_ensure_root_step` 开出来的那张卡也合上。

        ## 病根：懒开的步骤没有懒关的一端

        root step 是 `_ensure_root_step` 在第一条 `tool_call` 到达时**懒开**的 ——
        因为 harness 从不发 `root_step_start`（全仓 0 次）。而关闭那一端**只**认
        `root_step_end` 原始事件，它同样没人发（只有 local_execution 的异常路径
        发）。于是正常跑完的 run，卡片永远停在"进行中"：node20 实测 833 次
        `step.started` 对 5 次 `step.completed`。yuankk 2026-09-17 看到的
        「显示没能完成但又显示正在 writing」就是这个 —— 研究早停了，界面还在转。
        `subagent_call_end` 分支的注释甚至写着"它的 root_step_end 会发
        step.completed"，那是一个从不发生的期待。

        ## 修法：不给它第二个真相源

        补一个"记得发 root_step_end"的调用点是打补丁 —— 下一条终态路径照样会忘
        （现在已经忘了不止一条）。这一步的终态**本来就没有独立事实**：run 结束
        了，它就结束了。所以从 run 的终态推出来，和开那一端一样懒、一样由到达的
        事件触发。
        """
        step_id = adapter_state.get("rootStepId")
        if not step_id:
            return None                       # 压根没开过卡，无需关
        event_id = _hash_parts(context.session_id, str(step_id), "end", ADAPTER_VERSION)
        adapter_state.pop("rootStepId", None)
        adapter_state.pop("activeTool", None)
        if await db.get(ExecutionEvent, event_id):
            return None
        status = str(raw.get("status") or "completed")
        kind = self._STEP_STATUS_FROM_RUN.get(status, "step.failed")
        payload: dict[str, Any] = {"stepId": str(step_id)}
        if kind == "step.completed":
            payload["status"] = status
        else:
            payload["errorCode"] = status
            payload["errorMessage"] = str(
                raw.get("error") or raw.get("message") or f"run ended with {status}")
        return await self._insert_event(
            db,
            session=session,
            context=context,
            adapter_state_submission_id=str(adapter_state.get('submissionId') or ''),
            event_id=event_id,
            draft=EventDraft(kind, EventVisibility.STANDARD, payload, occurred_at),
            origin=EventOrigin.ADAPTER_DERIVED,
            source={"derivedFrom": [fallback_source_event_id]},
        )

    async def _insert_redaction_warning(
        self,
        db: AsyncSession,
        *,
        session: SessionProjection,
        context: IngestContext,
        source_event: ExecutionEvent,
        warnings: tuple[str, ...],
    ) -> ExecutionEvent:
        event_id = _hash_parts(context.session_id, source_event.id, "redaction", ADAPTER_VERSION)
        existing = await db.get(ExecutionEvent, event_id)
        if existing:
            return existing
        return await self._insert_event(
            db,
            session=session,
            context=context,
            event_id=event_id,
            draft=EventDraft(
                "redaction.warning",
                EventVisibility.SUMMARY,
                {"sourceEventId": source_event.id, "codes": list(warnings)},
                source_event.occurred_at,
            ),
            origin=EventOrigin.ADAPTER_DERIVED,
            source={"derivedFrom": [source_event.id]},
        )

    async def _apply_projection(
        self,
        db: AsyncSession,
        *,
        event: ExecutionEvent,
        run: Run,
        context: IngestContext,
    ) -> None:
        now = event.occurred_at
        status_by_kind = {
            "run.queued": RunStatus.QUEUED,
            "run.started": RunStatus.RUNNING,
            "run.resumed": RunStatus.RUNNING,
            "run.retrying": RunStatus.RETRYING,
            "run.completed": RunStatus.COMPLETED,
            "run.incomplete": RunStatus.INCOMPLETE,
            "run.failed": RunStatus.FAILED,
            "run.cancelled": RunStatus.CANCELLED,
            "run.status_unknown": RunStatus.STALE_UNKNOWN,
            "decision.required": RunStatus.WAITING_HUMAN,
            "permission.required": RunStatus.WAITING_PERMISSION,
        }
        # 只有**这条命令自己**的 run 生命周期能驱动 Run/attempt 状态机。
        # 子节点 run（experiment/_reviewer/_curator …）的 started/终态是
        # 信息性事件：UI 照常看得到（它们另有 step.* 投影），但不得关掉父
        # attempt，否则 post-node 决策后就再也恢复不了。
        # 旧事件没有 owningRun 字段 → 默认 True，保持历史行为。
        owning_run = bool(event.payload.get("owningRun", True))
        lifecycle_kinds = {
            "run.queued", "run.started", "run.resumed", "run.retrying",
            "run.completed", "run.incomplete", "run.failed", "run.cancelled",
            "run.status_unknown",
        }
        # ── 「关不关父 attempt」与「写不写自己这一行」是两个问题 ────────────
        #
        # 这道闸原来是整条 `return`：非 owning 的生命周期事件一个字都不写。
        # 它防的那个事故（子节点 incomplete 把**父** attempt 关成 failed）是真的，
        # 但那之后子 run 已经有了自己的 run id（`<parent>::<child>`）和自己的
        # Run/RunAttempt 行 —— `_close_attempt` 按 `RunAttempt.run_id == run.id`
        # 找，`run` 又是 `_lock_scope` 按**子** context 解出来的那一行。
        # 也就是说父 attempt 早就碰不到了，而这条 `return` 还在，
        # 顺手把**子 run 自己的终态**也一起扔了。
        #
        # 后果（2026-08-31 实测）：experiment 节点跑完 turn 153、1760 万 tokens、
        # 产物全部 frozen、`run.completed{missingRequiredOutputs: []}` 也发出来了，
        # 但它自己那一行永远停在 `queued`，连 RunAttempt 行都没建出来；
        # 随后被清扫判成 `stale_unknown` → 界面显示「Interrupted / Did not finish」。
        # 用户看到的是"这趟白跑了"，而磁盘上是一次完整成功的实验。
        #
        # 所以拆成两件事：自己那一行**照常写**；只有会波及父 run 的动作
        # （顶掉同会话的旧轮次）才认 owning。
        # 这是 [[feedback_two_things_one_rule]]：一条规则管两件事，
        # 保住了其中一件，另一件就悄悄丢了。
        if event.kind in lifecycle_kinds and not owning_run and run.parent_run_id is None:
            # 顶层 run 上出现非 owning 的生命周期事件 = 子节点事件被错记到了
            # 父行上（旧事故的形状）。这种仍旧不写。
            await db.flush()
            return
        # 三条分支合成一次写（D11）：状态变化经漏斗，出处随之落盘。
        # 原来是三处独立赋值，最后一处赢 —— 而"赢的是哪一处"从库里看不出来。
        projected: RunStatus | None = status_by_kind.get(event.kind)
        if (
            event.kind == "run.completed"
            and event.payload.get("status") == "completed_with_warning"
        ):
            projected = RunStatus.COMPLETED_WITH_WARNING
        if event.kind == "run.paused":
            projected = (
                RunStatus.WAITING_PERMISSION
                if event.payload.get("reason") == "waiting_permission"
                else RunStatus.WAITING_HUMAN
            )
        if projected is not None:
            # ── 终态不回退（#887 第 4 条）────────────────────────────────────
            #
            # 一条已经 completed 的 run 不该再被投影成 waiting_permission。
            # 没有任何合法路径能让一条真结束的 run 回到"在等人"—— `run_status`
            # 漏斗的注释早就这么写了，它只是**记下矛盾**然后照写，因为它刻意
            # 不做准入（政策归调用方）。这里就是那个调用方。
            #
            # 现场（#887）：父 run 已经 completed，一条迟到/重放的 `run.paused`
            # 把它打回 waiting_permission，UI 于是重放一张早就批准过的权限卡；
            # 服务端日志里那句 "a terminal was stamped too early" 正是这一刻。
            #
            # 只挡"终态 → 非终态"这一种。终态之间的改写（completed → failed）
            # 照旧放行并留痕：那是另一个问题，压在一起会把它一起藏掉。
            _was_terminal = run.status in {s.value for s in TERMINAL_RUN_STATUSES}
            if _was_terminal and projected not in TERMINAL_RUN_STATUSES:
                log.warning(
                    "Run %s is %s; refusing to project %s from %s (seq=%s)",
                    run.id, run.status, projected.value, event.kind, event.sequence,
                )
                _summary = dict(run.summary or {})
                _refused = list(_summary.get("refusedProjections") or [])
                _refused.append({
                    "terminal": str(run.status),
                    "refused": projected.value,
                    "kind": event.kind,
                    "eventId": str(event.id),
                    "sequence": int(event.sequence or 0),
                })
                _summary["refusedProjections"] = _refused[-24:]
                run.summary = _summary
                projected = None
        if projected is not None:
            project_run_status(
                run,
                projected,
                source="projector",
                evidence={
                    "eventId": str(event.id),
                    "kind": event.kind,
                    "sequence": int(event.sequence or 0),
                    "owningRun": owning_run,
                },
            )
        if event.kind == "run.started":
            run.node_type = event.payload.get("nodeType") or run.node_type
            run.started_at = run.started_at or now
            await self._ensure_attempt(db, run=run, context=context, started_at=now)
            # 「新的一轮开跑 = 之前那些轮结束了」只对**顶层**成立，且只有
            # owning 事件代表"这条命令自己开跑了"。子节点开跑不该顶掉任何轮次。
            if owning_run:
                await self._supersede_previous_turns(db, run=run, event=event)
        if event.kind in {"run.completed", "run.incomplete", "run.failed", "run.cancelled"}:
            run.ended_at = now
            await self._close_attempt(db, run=run, context=context, event=event)
        if event.kind in {"run.retrying", "tool.retrying"}:
            run.retry_count += 1
        if event.kind == "usage.updated":
            self._apply_usage(run, event.payload)
        if event.kind == "decision.required":
            await self._create_decision(db, event=event, context=context)
        if event.kind == "decision.resolved":
            await self._observe_decision_resolution(db, event=event, context=context)
        await self._touch_activity_lease(db, run=run, context=context, now=now, event=event)
        await db.flush()

    async def _supersede_previous_turns(
        self, db: AsyncSession, *, run: Run, event: ExecutionEvent
    ) -> None:
        """新的一轮开跑 = 之前那些轮**结束了**。给它们一个终态。

        ## 为什么必须在这里

        一个会话同一时刻只有一条"当前一轮"（会话面 RPC 是串行的，由
        `HarnessSession._operation_lock` 保证）。可 `Run` 行没有任何东西负责
        表达"你已经不是当前这一轮了" —— 上一轮如果停在 `waiting_human` 上，
        它就**永远**停在那里。

        2026-08-23 会话 e46448f0 的现场：13:39 的 post-node 决策卡在 13:40 的
        后端重启里丢了进程内的 pause，14:05 人手动答复被当成新的一轮开跑。
        旧 run 从此是一具尸体，但库里它还写着 `waiting_human` + 完整的 pause
        summary，于是会话级 pendingApproval 一直把那个已经答过的问题递给人 ——
        连续模式下同一张决策卡反复复活，而它背后连个能听的进程都没有。

        「从不更新的字段不是事实」在这里的形态是：**没有终局的状态不是状态**。
        补一条"答复要记得回去改上一条 run"是名单式修法（下次换个答复路径又漏）。
        真正机械的判据只有一个 —— 有人开了新的一轮。

        ## 边界

        - 只管**顶层** run：子节点 run 的生命周期由它的父 run 驱动。但被顶掉的
          那一轮名下的子 run 也一起收尾 —— 父都没了，它们更没有主。
        - 只管**更早开始**的：事件可能乱序补摄取，不能让一条迟到的 `run.started`
          把真正在跑的那条判死。
        - 只改非终态：已经有终态的一个字不动（终态被推翻这件事由
          `project_run_status` 自己记，不该由这里制造）。
        """
        if run.parent_run_id is not None:
            return
        started_at = run.started_at
        if started_at is None:
            return
        # 用现成的推导集，不写第四份名单。`CANCELLABLE` 的定义正是"还声称在
        # 进行中"（全部状态 - 终态 - stale_unknown）；stale_unknown 刻意留在外面
        # —— 那是"运行时丢了"，已经有自己的呈现（只读地照原样显示当时的问题）。
        live = {status.value for status in CANCELLABLE_RUN_STATUSES}
        stale_turns = list(
            (
                await db.scalars(
                    select(Run).where(
                        Run.session_id == run.session_id,
                        Run.parent_run_id.is_(None),
                        Run.id != run.id,
                        Run.status.in_(live),
                        Run.started_at.is_not(None),
                        Run.started_at < started_at,
                    )
                )
            ).all()
        )
        if not stale_turns:
            return
        superseded_ids = [turn.id for turn in stale_turns]
        descendants = list(
            (
                await db.scalars(
                    select(Run).where(
                        Run.session_id == run.session_id,
                        Run.parent_run_id.in_(superseded_ids),
                        Run.status.in_(live),
                    )
                )
            ).all()
        )
        for victim in [*stale_turns, *descendants]:
            project_run_status(
                victim,
                RunStatus.INCOMPLETE,
                source="superseded_by_next_turn",
                evidence={
                    "supersededBy": run.id,
                    "eventId": str(event.id),
                    "sequence": int(event.sequence or 0),
                },
            )
            victim.ended_at = victim.ended_at or event.occurred_at
        await db.flush()

    async def _touch_activity_lease(
        self,
        db: AsyncSession,
        *,
        run: Run,
        context: IngestContext,
        now: datetime,
        event: ExecutionEvent,
    ) -> None:
        """收到 worker 的事件 = 它此刻在动。续这条 attempt 的活动租约（RFC D10）。

        ## 为什么心跳挂在这里

        D10 定案写死了一句：

            心跳**必须由干活的那个循环自己发**（与真实进展耦合 —— 独立心跳
            线程会制造"看起来活着的僵尸"）。

        这里就是那个耦合点：一条事件被投影，意味着 worker 真的产出了一步
        （工具调用、叙述、用量、生命周期）。没有进展就没有事件，没有事件租约
        自己就过期了。反过来，如果另起一个定时线程去 ping，一个卡死在
        `kevent` 上的 worker 会一直"心跳正常" —— 那正是要避免的形状。

        ## 为什么租约放在 attempt 上而不是 run 上

        `RunAttempt` 是"这一次执行尝试"，天然带 `worker_id`。run 是逻辑上的
        一趟研究，可以横跨多次尝试（续跑、重试）。问"现在还有没有进程在动"，
        问的是尝试，不是那趟研究。

        字段 `lease_until` / `heartbeat_at` 2026 年就随表建好了，schema 也一直
        暴露着 —— **但从来没有一行代码写过它们**。机制存在，没接到路径。

        ## 节流

        每条事件都写库会把投影器变成写放大器（一次 run 上千条事件）。租约的
        用途是"几十秒粒度的死活"，不是精确计时，所以 `_LEASE_TOUCH_SECONDS`
        内只写一次。
        """
        attempt = await db.scalar(
            select(RunAttempt).where(
                RunAttempt.tenant_id == context.tenant_id,
                RunAttempt.run_id == run.id,
                RunAttempt.attempt_no == context.attempt_no,
            )
        )
        if attempt is None:
            return
        # ⚠️ 从库里读回来的时间可能是 **naive** 的（SQLite 不存时区，测试库就是
        # 这样），跟 aware 的 `now` 相减直接 TypeError。而这个异常发生在投影
        # 路径上 —— 它会被外层记成"这一轮执行失败"，把一条只是被重启打断的
        # run 盖成 `failed`（实测：一加租约，重启恢复用例当场红）。
        # 时间的时区归一化要在**用它之前**做，不能指望存的时候都对。
        last = attempt.heartbeat_at
        if last is not None:
            if last.tzinfo is None:
                last = last.replace(tzinfo=UTC)
            if (now - last).total_seconds() < _LEASE_TOUCH_SECONDS:
                return
        attempt.heartbeat_at = now
        lease = now + timedelta(seconds=ACTIVITY_LEASE_SECONDS)
        if event.kind == "run.parked":
            # worker 自报要睡到几点 —— 租约跟到那个点（多给一个租约周期的
            # 宽限，让它醒来后有时间发出第一条事件）。这是**它说的事实**，
            # 不是平台的推断；到点还没动静照样衰减为 unknown。
            until = int(event.payload.get("untilEpoch") or 0)
            if until > 0:
                parked_until = datetime.fromtimestamp(until, tz=UTC) + timedelta(
                    seconds=ACTIVITY_LEASE_SECONDS)
                lease = max(lease, parked_until)
        attempt.lease_until = lease

    async def _ensure_attempt(
        self,
        db: AsyncSession,
        *,
        run: Run,
        context: IngestContext,
        started_at: datetime,
    ) -> RunAttempt:
        attempt = await db.scalar(
            select(RunAttempt).where(
                RunAttempt.tenant_id == context.tenant_id,
                RunAttempt.run_id == run.id,
                RunAttempt.attempt_no == context.attempt_no,
            )
        )
        if not attempt:
            attempt = RunAttempt(
                tenant_id=context.tenant_id,
                workspace_id=context.workspace_id,
                project_id=context.project_id,
                session_id=context.session_id,
                run_id=run.id,
                attempt_no=context.attempt_no,
                status=AttemptStatus.RUNNING,
                started_at=started_at,
            )
            db.add(attempt)
        else:
            attempt.status = AttemptStatus.RUNNING
            attempt.started_at = attempt.started_at or started_at
        await db.flush()
        return attempt

    async def _close_attempt(
        self,
        db: AsyncSession,
        *,
        run: Run,
        context: IngestContext,
        event: ExecutionEvent,
    ) -> None:
        attempt = await db.scalar(
            select(RunAttempt).where(
                RunAttempt.tenant_id == context.tenant_id,
                RunAttempt.run_id == run.id,
                RunAttempt.attempt_no == context.attempt_no,
            )
        )
        if not attempt:
            attempt = await self._ensure_attempt(
                db, run=run, context=context, started_at=run.started_at or event.occurred_at
            )
        attempt.status = {
            "run.completed": AttemptStatus.COMPLETED,
            "run.cancelled": AttemptStatus.CANCELLED,
        }.get(event.kind, AttemptStatus.FAILED)
        attempt.exit_reason = str(event.payload.get("status") or event.kind)
        attempt.ended_at = event.occurred_at

    @staticmethod
    def _apply_usage(run: Run, payload: dict[str, Any]) -> None:
        had_usage = run.total_tokens > 0
        prompt = max(0, int(payload.get("promptTokens") or 0))
        completion = max(0, int(payload.get("completionTokens") or 0))
        total = max(0, int(payload.get("totalTokens") or prompt + completion))
        run.prompt_tokens += prompt
        run.completion_tokens += completion
        run.total_tokens += total
        incoming_coverage = (
            payload.get("coverage")
            if payload.get("coverage") in {"complete", "partial"}
            else "partial"
        )
        run.usage_coverage = (
            "partial"
            if (had_usage and run.usage_coverage == "partial") or incoming_coverage == "partial"
            else "complete"
        )
        cost = payload.get("cost")
        if cost is not None:
            parsed_cost = _validated_cost(cost)
            if parsed_cost is None:
                return
            run.cost = (run.cost or Decimal("0")) + parsed_cost
            currency = payload.get("currency")
            if run.cost_currency and currency and run.cost_currency != currency:
                run.cost_currency = None
                run.usage_coverage = "partial"
            elif currency:
                run.cost_currency = str(currency).upper()

    async def _create_decision(
        self, db: AsyncSession, *, event: ExecutionEvent, context: IngestContext
    ) -> Decision:
        authority = context.decision_authority
        if not authority:
            raise DecisionAuthorityRequiredError(
                "DecisionAuthority snapshot is required before ingest"
            )
        decision_id = str(event.payload["decisionId"])
        existing = await self._validate_existing_decision(
            db,
            payload=event.payload,
            context=context,
        )
        if existing:
            return existing
        decision = Decision(
            id=decision_id,
            tenant_id=context.tenant_id,
            workspace_id=context.workspace_id,
            project_id=context.project_id,
            session_id=context.session_id,
            run_id=context.run_id,
            attempt_no=context.attempt_no,
            subtype=str(event.payload["subtype"]),
            prompt=str(event.payload["prompt"]),
            context=dict(event.payload.get("context") or {}),
            choices=list(event.payload["choices"]),
            recommended_choice_id=event.payload.get("recommendedChoiceId"),
            authority_type=authority.authority_type,
            authority_subjects=list(authority.authority_subjects),
            required_approval_count=authority.required_approval_count,
            action_set_version=authority.action_set_version,
            policy_snapshot_id=authority.policy_snapshot_id,
            expires_at=authority.expires_at,
            status=(
                DecisionStatus.EXPIRED
                if authority.expires_at and authority.expires_at <= event.occurred_at
                else DecisionStatus.PENDING
            ),
        )
        db.add(decision)
        await db.flush()
        await self._supersede_prior_presentations(db, decision=decision)
        return decision

    async def _supersede_prior_presentations(
        self, db: AsyncSession, *, decision: Decision
    ) -> None:
        """Retire earlier presentations on the same Run: a Run has one live pause.

        新呈递落地 = 之前那些还没人回答的呈递不再可答。留着 pending 就是给 UI 和
        driver 一个永远等不到答案的幽灵决定：前端把它当活卡递给人，人点下去答
        的是一个早已不存在的 pause（2026-09-09 node20：observation 重跑后是新的
        producing run，旧卡按 producingRunId 作用域没被顶掉，人点了旧卡 →
        offer_superseded）。作用域只能是 run：同一条 run 同一时刻只有一个 pause，
        这是运行时的结构事实，不是名单。
        """
        candidates = await db.scalars(
            select(Decision).where(
                Decision.tenant_id == decision.tenant_id,
                Decision.session_id == decision.session_id,
                Decision.run_id == decision.run_id,
                Decision.subtype == decision.subtype,
                Decision.id != decision.id,
                Decision.status.in_(
                    [DecisionStatus.PENDING.value, DecisionStatus.PARTIALLY_APPROVED.value]
                ),
            )
        )
        for prior in candidates:
            prior.status = DecisionStatus.SUPERSEDED.value
            prior_context = dict(prior.context or {})
            prior_context["supersededByDecisionId"] = decision.id
            prior.context = prior_context
        await db.flush()

    async def _validate_existing_decision(
        self,
        db: AsyncSession,
        *,
        payload: dict[str, Any],
        context: IngestContext,
    ) -> Decision | None:
        authority = context.decision_authority
        if not authority:
            raise DecisionAuthorityRequiredError(
                "DecisionAuthority snapshot is required before ingest"
            )
        decision_id = str(payload["decisionId"])
        existing = await db.get(Decision, decision_id)
        if not existing:
            return None

        def normalized_time(value: datetime | None) -> datetime | None:
            if value is None:
                return None
            if value.tzinfo is None:
                value = value.replace(tzinfo=UTC)
            return value.astimezone(UTC)

        frozen = (
            existing.tenant_id,
            existing.workspace_id,
            existing.project_id,
            existing.session_id,
            existing.run_id,
            existing.subtype,
            existing.prompt,
            existing.context,
            existing.choices,
            existing.recommended_choice_id,
            existing.authority_type,
            tuple(existing.authority_subjects),
            existing.required_approval_count,
            existing.action_set_version,
            existing.policy_snapshot_id,
            normalized_time(existing.expires_at),
        )
        proposed = (
            context.tenant_id,
            context.workspace_id,
            context.project_id,
            context.session_id,
            context.run_id,
            str(payload["subtype"]),
            str(payload["prompt"]),
            dict(payload.get("context") or {}),
            list(payload["choices"]),
            payload.get("recommendedChoiceId"),
            authority.authority_type,
            authority.authority_subjects,
            authority.required_approval_count,
            authority.action_set_version,
            authority.policy_snapshot_id,
            normalized_time(authority.expires_at),
        )
        if frozen != proposed:
            # 守卫不动：一个决定的条款不许在人回答之前变。变了 = 这不是同一个
            # 决定 —— 正常路径下 harness 每次呈递都带新 decision_id，根本走不到
            # 这里；走到这里说明上游没带 id（老 transcript）或真的在篡改。拒收
            # 这条记录（RecordRejectedError → 落成 record.rejected 见证事件），
            # 但不再由这里判整轮死刑。
            raise RecordRejectedError("Decision immutable snapshot mutation rejected")
        return existing

    async def _observe_decision_resolution(
        self, db: AsyncSession, *, event: ExecutionEvent, context: IngestContext
    ) -> None:
        decision = await db.scalar(
            select(Decision).where(
                Decision.tenant_id == context.tenant_id,
                Decision.id == str(event.payload["decisionId"]),
            )
        )
        if not decision:
            return
        selected = event.payload.get("selectedChoiceId")
        # 运行时说这次呈递已经有了终局，它就有了终局。此前这里还要平台自己的
        # `accepted_responses` 计数够数才肯改状态 —— 答复不经平台记账那条路
        # （替人点推荐项、CLI）时计数是 0，行永远 pending，前端一直把它当活卡
        # 递给人。账面不许否决现场（D11）。
        if selected and event.payload.get("terminal"):
            decision.status = DecisionStatus.RESOLVED
            decision.selected_choice_id = str(selected)
            decision.resolved_at = event.occurred_at


execution_ingest_service = ExecutionIngestService()

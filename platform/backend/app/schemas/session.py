"""Canonical Research Session and project membership API contracts."""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


def _camel(value: str) -> str:
    head, *tail = value.split("_")
    return head + "".join(part.capitalize() for part in tail)


class ApiModel(BaseModel):
    model_config = ConfigDict(alias_generator=_camel, populate_by_name=True)


class IdentityOut(ApiModel):
    id: str
    display_name: str


class ModelBackendRefOut(ApiModel):
    id: str
    display_name: str
    provider: str
    model: str


class UsageOut(ApiModel):
    total_tokens: int = 0
    cost: float | None = None
    currency: str | None = None
    coverage: Literal["complete", "partial"] = "partial"


class SessionCreate(ApiModel):
    title: str = Field(min_length=1, max_length=300)
    summary: str | None = None
    model_backend_id: str | None = None


class SessionUpdate(ApiModel):
    title: str | None = Field(default=None, min_length=1, max_length=300)
    summary: str | None = None
    lifecycle_status: Literal["active", "completed"] | None = None
    model_backend_id: str | None = None


class SessionArchiveOut(ApiModel):
    """收起会话的结果。**做了哪一件必须说出来**——空会话是被删掉的，不是归档的。

    只回一个会话对象的话，前端只能靠"字段是不是 null"去猜发生了什么，然后
    在提示里编一句。发生了什么由后端说，UI 照着讲。
    """

    outcome: Literal["archived", "deleted"]
    session: "SessionOut | None" = None


class PendingApprovalOptionOut(ApiModel):
    #: 这个选项的**身份**。答复靠它回传，不靠文案 —— 文案在选项集变化时会撞不上，
    #: 人的授权会被静默丢弃（PR#554 就是为消灭这条路存在的）。
    #:
    #: 这一列此前**不在这个模型里**：呈递方给了 `id`（proceed / revise / …），
    #: ingest 也原样搬过来了，前端 `toChatPause` 也照 `option.id` 读 —— 唯独中间
    #: 这个 Pydantic 模型只列了 label/description/recommended，于是 id 在序列化时
    #: 被静默丢掉，`optionDetails[*].id` 全是 null，前端只好退回按文案回传。
    #: 又一次「字段被列出的地方数 = 会漏的地方数」。
    id: str | None = None
    label: str
    description: str = ""
    recommended: bool = False


class PendingApprovalOut(ApiModel):
    """人此刻被问到的那个问题，完整的。

    `context` 是逐字的待执行内容（命令、workdir、命中的风险类别）。它不是
    可省略的装饰 —— 让人批准一个看不见内容的高危操作，等于没有审批。
    """

    run_id: str
    reason: str
    #: 这次呈递是哪一类。**后端说了算** —— 前端曾经拿 `reason == "waiting_permission"`
    #: 去猜，那是 run 状态词表的取值，猜漏一支（decision_package）审批卡就会按
    #: 普通提问渲染、把逐字命令折叠起来。
    kind: Literal["permission", "decision", "human_input"] = "human_input"
    prompt: str | None = None
    context: str | None = None
    options: list[str] = []
    option_details: list[PendingApprovalOptionOut] = []
    recommended_option_index: int | None = None
    #: 这一次呈递的**原样投影**（harness `Offer.to_pause_payload()`）。
    #:
    #: 故意是不透明 dict：平台不认识它的内部字段，也就没有"漏抄一个字段"这个
    #: 动作可做。上面的 options / option_details / recommended_option_index 是
    #: 既有前端读的兼容视图，取自同一份呈递，不可能与它分叉。
    #:
    #: 缺席 = 这个 pause 不是一次带选项集的呈递（自由文本问答、老 checkpoint 恢复）。
    offer: dict[str, Any] | None = None
    asking_node_type: str | None = None
    pause_kind: str | None = None
    asked_at: datetime | None = None


class ExecutionWaitingOnOut(ApiModel):
    """在等**哪一类**东西。呈递的身份不在这儿 —— 它随卡片一起长在
    `AnswerAffordanceOut.pause` 里（卡片和卡片的身份是同一个东西）。"""

    kind: Literal["human", "permission", "compute"]


class AnswerAffordanceOut(ApiModel):
    """**答案从哪儿进来** —— 见 services/execution_view.py 的模块头。

    三个取值互斥且完备。`via == "pause"` 时 `pause` 必然非空：说了走卡片就
    一定带着卡片，「输入框关了但卡不在」因此写不出来。
    """

    via: Literal["composer", "pause", "none"]
    #: via == "pause" 时的卡片本体。
    pause: PendingApprovalOut | None = None
    #: via == "none" 时为什么。
    reason: str | None = None
    #: via == "none" 且这个判断会自己过期（别人的驾驶权租约）时的到期时刻。
    #: 客户端据此安排一次重新查询 —— 它决定何时再问，不决定答案是什么。
    until: datetime | None = None
    #: via == "composer" 但**本该**是卡片：我们在等人回答，却拿不出那个问题
    #: 本身（老会话、上游削过字段）。把输入框还给人，并如实说出来 ——
    #: 唯一比"给错入口"更坏的是"一个入口都不给"。
    degraded: str | None = None


class ExecutionViewOut(ApiModel):
    """一个会话/一条 run 此刻的局面 —— 后端现算，前端只渲染。

    见 services/execution_view.py 的模块头：这个类型存在的理由是前端曾经有
    9 套各自手写的状态集合，从 `/runs` 列表自己推导会话状态，而那条路会被
    永远停在 `queued` 的子 run 赢下排序。
    """

    phase: Literal["alive", "ended", "interrupted"]
    waiting_on: ExecutionWaitingOnOut | None = None
    outcome: Literal["ok", "ok_with_warning", "incomplete", "failed", "cancelled"] | None = None
    error: dict[str, Any] | None = None
    can_stop: bool
    label: str
    run_id: str | None = None
    since: str | None = None
    #: 答复入口。**会话**的 view 必带；一条历史 run 的 view 里没有这个键 ——
    #: 从历史 run 渲染出可点的卡片因此在构造上不可能。
    #:
    #: 这里没有 `can_send`：它曾经是一个答不出"让位给谁"的孤立布尔，
    #: 2026-09-01 把用户锁死 6 小时的正是它与另外两处判据的分叉。
    answer: AnswerAffordanceOut | None = None


class SessionOut(ApiModel):
    id: str
    project_id: str
    title: str
    summary: str | None
    lifecycle_status: str
    recovered_from_session_id: str | None
    recovery_source_run_id: str | None
    created_by_user_id: str | None
    primary_driver_user_id: str | None
    model_backend_id: str | None
    research_settings_snapshot_id: str | None
    research_settings_context_hash: str | None
    capabilities: list[str]
    creator: IdentityOut | None
    primary_driver: IdentityOut | None
    primary_driver_name: str | None
    created_by_name: str | None
    # 版本 = git 提交（RFC X1）。从前这里是 project_revisions 的行与自增号。
    base_commit_sha: str | None
    head_commit_sha: str | None
    project_advanced: bool
    ahead_by: int = 0
    behind_by: int = 0
    git_branch: str | None
    git_base_commit_sha: str | None
    git_head_commit_sha: str | None
    model_backend: ModelBackendRefOut | None
    model_backend_name: str | None
    effective_capabilities: list[str]
    execution_state: str
    #: **前端唯一该读的那个答案**：这个会话此刻是什么局面。
    #:
    #: 三态互斥且完备（alive / ended / interrupted），另带「在等什么」、
    #: 「结局是哪一种」、「能不能停」、「能不能发」。`canStop` 与 `/stop`
    #: 端点共用同一个谓词函数，所以「按钮亮着按下去 409」在构造上不可能。
    #:
    #: 上面那个 `execution_state` 是 13 值枚举的原始投影，留给尚未迁完的读点。
    execution_view: ExecutionViewOut
    #: 「此刻在等人回答什么」曾经是这里一个**平级**字段 `pending_approval`。
    #: 它和 `execution_view.can_send` 回答同一个问题的两半，而没有任何东西
    #: 保证它们说得一致 —— 2026-09-01 它们不一致了 6 小时（后端说"走卡片"、
    #: 前端一张卡都没画、输入框锁着）。现在它长在 `execution_view.answer.pause`
    #: 里：入口和入口里那张卡是同一个字段。
    run_count: int
    #: 单份用户文件的上限（字节）。前端拿它做选文件时的预检 —— 契约要送到
    #: 调用方，别让前端自己写一个会跟后端分叉的常量。
    material_max_bytes: int
    unpublished_change_count: int
    conflict_count: int
    usage: UsageOut
    retry_count: int
    created_at: datetime
    updated_at: datetime
    archived_at: datetime | None


class SessionRecoveryOut(ApiModel):
    status: Literal["created", "existing"]
    action: Literal["start_new_session"] = "start_new_session"
    reason: Literal["lost_runtime_state"] = "lost_runtime_state"
    source_session_id: str
    source_run_id: str
    suggested_message: str
    session: SessionOut


class SessionMessageOut(ApiModel):
    id: str
    session_id: str
    sequence: int
    role: str
    content: str
    actor: IdentityOut | None
    actor_user_id: str | None
    command_id: str | None
    run_id: str | None
    #: 这条消息就是哪一次呈递（有值时，待答卡片渲染在它的位置上）。
    offer_id: str | None = None
    created_at: datetime


class SessionMessagesOut(ApiModel):
    items: list[SessionMessageOut]
    next_after_sequence: int


class SessionInterruptRequest(ApiModel):
    """跑轮中插给正在干活的 agent 的一句话。"""

    text: str = Field(min_length=1, max_length=8000)


class ProjectMemberWrite(ApiModel):
    user_id: str | None = None
    email: str | None = None
    role: Literal["lead", "researcher", "reviewer", "viewer"]


class ProjectMemberUpdate(ApiModel):
    role: Literal["lead", "researcher", "reviewer", "viewer"]


class ProjectMemberOut(ApiModel):
    id: str
    user_id: str
    display_name: str
    email: str | None = None
    role: str
    joined_at: datetime
    created_at: datetime
    updated_at: datetime
    removed_at: datetime | None

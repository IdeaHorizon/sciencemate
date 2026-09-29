"""Durable Research App Server execution projections.

Harness files remain the scientific source of truth. These tables are the
tenant-scoped, queryable control-plane projection and never recreate Graph Node
or in-process scheduler state.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from uuid import uuid4

from sqlalchemy import (
    JSON,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    event,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.orm.base import NEVER_SET, NO_VALUE

from app.database import Base, UTCDateTime

IDENTITY_COLUMNS = ("tenant_id", "workspace_id", "project_id", "session_id")


def _json_type() -> JSON:
    return JSON().with_variant(JSONB(), "postgresql")


def _new_id() -> str:
    return str(uuid4())


class RunStatus(StrEnum):
    QUEUED = "queued"
    DISPATCHING = "dispatching"
    RUNNING = "running"
    WAITING_PERMISSION = "waiting_permission"
    WAITING_HUMAN = "waiting_human"
    WAITING_COMPUTE = "waiting_compute"
    RETRYING = "retrying"
    COMPLETED = "completed"
    COMPLETED_WITH_WARNING = "completed_with_warning"
    INCOMPLETE = "incomplete"
    FAILED = "failed"
    CANCELLED = "cancelled"
    STALE_UNKNOWN = "stale_unknown"


# ── RunStatus 的语义分类：只有这一份 ─────────────────────────────────────────
#
# 2026-08-10 普查：全仓有**五份**各自手写的 RunStatus 分区，散在五个文件里，
# 而且互相不一致 ——
#
#   harness_sessions._STATES_REQUIRING_LIVE_RUNTIME  漏了 DISPATCHING
#   execution.py 的终态集合                          含 stale_unknown
#   projects.py 的 _RESOLVED                         不含 incomplete / failed
#   活性探针                                          只认 WAITING_HUMAN / STALE_UNKNOWN
#   恢复判据                                          又一份
#
# 今晚三个缺陷全出自这里：回收器只扫 waiting_human（漏掉 running 的尸体）、
# 恢复只认 stale_unknown（越及时发现故障越恢复不了）、Trace 归属……每一次都是
# 某一份名单写漏或写反，而**分叉时两边都不报错**。
#
# 根因不是某份名单错了，是 13 个状态值**没有一份权威的语义分类**，于是每个
# 消费者都自己写一遍集合字面量。所以把语义放到枚举旁边，且用测试强制：
# 新增一个状态值而不给它分类，测试立刻红。

#: 已经走到定义好的终点。终态不需要活进程，也不会自己再变。
TERMINAL_RUN_STATUSES: frozenset[RunStatus] = frozenset({
    RunStatus.COMPLETED,
    RunStatus.COMPLETED_WITH_WARNING,
    RunStatus.INCOMPLETE,
    RunStatus.FAILED,
    RunStatus.CANCELLED,
})

#: 声称"有东西在跑/在等"，因而**以有活进程为前提**。进程没了它就是尸体。
#:
#: `WAITING_COMPUTE` 刻意**不在**这里：外部作业在集群排队时，harness 进程本来
#: 就可以不在（作业由调度器持有）。把它算进来会把正常等待判成故障。
#: `DISPATCHING` 在这里 —— 它是平台自己正在起进程的瞬时态，卡在这就是故障。
REQUIRES_LIVE_RUNTIME_STATUSES: frozenset[RunStatus] = frozenset({
    RunStatus.QUEUED,
    RunStatus.DISPATCHING,
    RunStatus.RUNNING,
    RunStatus.WAITING_PERMISSION,
    RunStatus.WAITING_HUMAN,
    RunStatus.RETRYING,
})

#: 「停下来等人」—— 需要活进程，但**不断言自己在动**。
#:
#: 与下面的 `ASSERTS_ACTIVE_WORK` 是同一枚硬币的两面，分开是因为**说错了的
#: 代价不同**：说"在跑"而其实没进程，用户看到一个不存在的进度条、且插不进话；
#: 说"在等你"而其实没进程，那句话仍然成立 —— 回答它会把 worker 重新拉起来
#: （续跑是全函数，见 RFC 异步运行时）。所以前者必须被现算纠正，后者不能。
PARKED_WAITING_FOR_HUMAN: frozenset[RunStatus] = frozenset({
    RunStatus.WAITING_PERMISSION,
    RunStatus.WAITING_HUMAN,
})

#: 断言"它此刻在动"的状态。**推导出来的，不是第四份手写名单**
#: （同 `UNFINISHED_RUN_STATUSES`：名单一多就会各自演化，而分叉时两边都不报错）。
ASSERTS_ACTIVE_WORK: frozenset[RunStatus] = (
    REQUIRES_LIVE_RUNTIME_STATUSES - PARKED_WAITING_FOR_HUMAN
)

#: 走到头了但**不是干净的结束** —— 还有活可以接着干。恢复资格看这个。
#:
#: 判据与"当初怎么死的"无关：平台是否及时发现故障，不该决定用户能不能续。
UNFINISHED_RUN_STATUSES: frozenset[RunStatus] = frozenset({
    RunStatus.INCOMPLETE,
    RunStatus.FAILED,
    RunStatus.CANCELLED,
    RunStatus.STALE_UNKNOWN,
})

#: 干净收尾 —— 没有遗留问题需要人处理。
CLEANLY_FINISHED_RUN_STATUSES: frozenset[RunStatus] = frozenset({
    RunStatus.COMPLETED,
    RunStatus.COMPLETED_WITH_WARNING,
})

# ── 以下都是**推导**出来的，不是第四第五份名单 ───────────────────────────────
# 推导保证它们随上面三份一起演化：改了上面，这里自动跟着对。

#: 还能取消 —— 还在进行中。`stale_unknown` 不在：运行时已经没了，没什么可取消的。
CANCELLABLE_RUN_STATUSES: frozenset[RunStatus] = frozenset(
    set(RunStatus) - TERMINAL_RUN_STATUSES - {RunStatus.STALE_UNKNOWN}
)

#: 事件流可以收尾了。比 TERMINAL 多一个 stale_unknown —— 运行时丢了就不会再
#: 有事件来，继续挂着只会让前端一直等。
STREAM_TERMINAL_RUN_STATUSES: frozenset[RunStatus] = frozenset(
    TERMINAL_RUN_STATUSES | {RunStatus.STALE_UNKNOWN}
)

#: 之前报的 blocker 可以视为不用再处理了：run 后来干净收尾，或被明确取消。
BLOCKER_RESOLVED_RUN_STATUSES: frozenset[RunStatus] = frozenset(
    CLEANLY_FINISHED_RUN_STATUSES | {RunStatus.CANCELLED}
)

#: **停下来等一个人回答**。这个分区存在的理由，见 2026-08-10 的 2 小时静默死锁：
#:
#:   harness 内核发出的 `run.paused` 事件，payload 里带着完整的问题、选项、
#:   推荐项、乃至逐字的待执行命令 —— 全都进了 execution_events 表。而
#:   `_apply_event` 只取了 `reason` 改 run.status，其余**整个丢掉**；session
#:   payload 不暴露它；前端那块渲染审批的代码被 `!transientRunHasCanonicalOwner`
#:   挡死，也就是说**只要 run 在库里登记过就永远不显示** —— 而真实科研 run
#:   全都是登记过的。
#:
#: 于是"等人"这个状态，人这一侧根本看不到问题。既不是没造，也不是造错，
#: 是六层里有四层没接上。
#:
#: `WAITING_COMPUTE` 不在这里：那是等机器，不是等人，没有人需要被叫醒。
AWAITING_HUMAN_RUN_STATUSES: frozenset[RunStatus] = frozenset({
    RunStatus.WAITING_PERMISSION,
    RunStatus.WAITING_HUMAN,
})


class AttemptStatus(StrEnum):
    CREATED = "created"
    LEASED = "leased"
    RUNNING = "running"
    RELEASED = "released"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    STALE_UNKNOWN = "stale_unknown"


class DecisionStatus(StrEnum):
    PENDING = "pending"
    PARTIALLY_APPROVED = "partially_approved"
    RESOLVED = "resolved"
    EXPIRED = "expired"
    CANCELLED = "cancelled"
    # 同一个决定点被重新呈递（新 decision_id、可能是新条款）后，前一次呈递
    # 不再可答。它不是被取消（没人否决它），也不是被解决（没人回答它）——
    # 它被**取代**了。三件事三个词，判据才不会互相污染。
    SUPERSEDED = "superseded"


class DecisionAuthorityType(StrEnum):
    INITIATING_USER = "initiating_user"
    NAMED_USERS = "named_users"
    PROJECT_ROLE = "project_role"
    APPROVAL_GROUP = "approval_group"


class EventOrigin(StrEnum):
    RAW_TRANSCRIPT = "raw_transcript"
    ADAPTER_DERIVED = "adapter_derived"
    APP_COMMAND = "app_command"
    RECONCILIATION = "reconciliation"


class EventVisibility(StrEnum):
    SUMMARY = "summary"
    STANDARD = "standard"
    TRACE = "trace"


class SessionProjection(Base):
    """Canonical durable Research Session and its sequence counters."""

    __tablename__ = "sessions"

    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    workspace_id: Mapped[str] = mapped_column(String(64), nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False)
    session_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    initiating_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    title: Mapped[str] = mapped_column(
        String(300), nullable=False, default="Recovered research session"
    )
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    # 驾驶权租约（primary_driver_user_id / driver_lease_until）2026-09-05 删除。
    # 「谁在开这个会话」是**现算**的：最近一条用户消息的作者（见
    # services.sessions.current_driver_id）。租约是一台状态机 —— 拿、续、放、
    # 到期 —— 而它要防的事（两个人同时对一个会话动手）在跑轮那一刻由占用判据
    # 挡住，那里够得着现场。个人档更是从头到尾只有一个人。
    lifecycle_status: Mapped[str] = mapped_column(String(24), nullable=False, default="active")
    recovered_from_session_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey(
            "sessions.session_id",
            ondelete="SET NULL",
            use_alter=True,
            name="fk_sessions_recovered_from_session",
        ),
        nullable=True,
        index=True,
    )
    recovery_source_run_id: Mapped[str | None] = mapped_column(
        String(128), nullable=True, index=True
    )
    # base_revision_id 2026-09-05 删（RFC X1）：会话的基线是 `git_base_commit_sha`
    # —— 一个能拿去 `git show` 的提交，而不是只在那张表里有意义的一行 id。
    git_branch: Mapped[str | None] = mapped_column(String(200), nullable=True)
    git_base_commit_sha: Mapped[str | None] = mapped_column(String(40), nullable=True)
    git_head_commit_sha: Mapped[str | None] = mapped_column(String(40), nullable=True)
    git_worktree_path: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    policy_snapshot_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    research_settings_snapshot_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True
    )
    research_settings_snapshot: Mapped[dict | None] = mapped_column(_json_type(), nullable=True)
    model_backend_id: Mapped[str | None] = mapped_column(Uuid(as_uuid=False), nullable=True)
    platform_context_snapshot: Mapped[dict | None] = mapped_column(_json_type(), nullable=True)
    knowledge_read_watermark: Mapped[dict | None] = mapped_column(_json_type(), nullable=True)
    #: 这个会话**唯一**的序号发生器（消息和执行事件都从它领号）。
    #:
    #: 从前这里是两个计数器（`next_message_sequence` / `next_event_sequence`），
    #: 各自从 0 开始。于是"消息 3"和"事件 3"是两个毫不相干的东西，而
    #: `session_messages.sequence` 与 `execution_events.sequence` 在类型上
    #: 长得一模一样 —— 谁把它们放在一起比较都不会报错。
    #:
    #: 前端就这么比了：`messageRunSegments` 用消息号切窗口、`inWindow` 拿事件号
    #: 去落窗口，注释里白纸黑字写着「消息和执行事件共用同一个会话级 sequence
    #: 空间」。实测（会话 e46448f0）消息号 1–5、事件号 1–869：一条 run 的全部
    #: 活动统统落进第一条消息的开放尾窗，13:32 的动作画在 13:17 的对话上面。
    #:
    #: "这段活动发生在哪两条消息之间" 是**一条时间线上的先后**问题。要它有
    #: 答案，两者就必须在同一个序里 —— 那只能有一个发生器。
    next_sequence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    archived_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)

    __table_args__ = (
        UniqueConstraint(*IDENTITY_COLUMNS, name="uq_sessions_scope"),
        UniqueConstraint(
            "tenant_id",
            "recovered_from_session_id",
            name="uq_sessions_recovery_source",
        ),
        CheckConstraint(
            "lifecycle_status IN ('active', 'completed', 'archived')",
            name="ck_sessions_lifecycle_status",
        ),
        CheckConstraint("next_sequence >= 0", name="ck_sessions_sequence_nonnegative"),
        Index("ix_sessions_tenant_project", "tenant_id", "project_id"),
    )


class SessionMessage(Base):
    """Append-only canonical Session message."""

    __tablename__ = "session_messages"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_new_id)
    session_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    actor_user_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    command_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    run_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    #: 这条消息**就是**哪一次呈递（`core.decision_offer.Offer.offer_id`）。
    #:
    #: run 停下来问人时，平台把问句原样写成一条 assistant 消息 —— 于是同一句话
    #: 在页面上出现两次：一次在对话流里，一次在能点的那张卡片里。从前靠**文案
    #: 相等**去掉后者（`pause-echo.ts`），而"拿文案当身份"正是 2026-08-19 那次
    #: 决策卡无限重现事故的引擎（见 `core/decision_offer.py` 开头）。
    #:
    #: 带上身份，去重就是一次集合成员判定：卡片渲染在**这条消息的位置**上，
    #: 正文不再画第二遍。改文案、加装饰字符、截断，都不影响。
    offer_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ("session_id",),
            ("sessions.session_id",),
            ondelete="CASCADE",
            name="fk_session_messages_session",
        ),
        UniqueConstraint("session_id", "sequence", name="uq_session_messages_sequence"),
        UniqueConstraint("command_id", "role", name="uq_session_messages_command_role"),
        CheckConstraint("sequence >= 1", name="ck_session_messages_sequence_positive"),
        CheckConstraint("role IN ('user', 'assistant', 'system')", name="ck_session_messages_role"),
        Index("ix_session_messages_session_sequence", "session_id", "sequence"),
    )


class Run(Base):
    """Logical Run projection with first-class usage and retry accounting."""

    __tablename__ = "runs"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    workspace_id: Mapped[str] = mapped_column(String(64), nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False)
    session_id: Mapped[str] = mapped_column(String(64), nullable=False)
    parent_run_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    node_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default=RunStatus.QUEUED)

    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    cost_currency: Mapped[str | None] = mapped_column(String(3), nullable=True)
    usage_coverage: Mapped[str] = mapped_column(String(20), nullable=False, default="partial")
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    summary: Mapped[dict | None] = mapped_column(_json_type(), nullable=True)

    #: 交付对账观测到的**不可达证据**，不是"别再试了"的判决。
    #:
    #: ``{missing_path, probe, error, detected_at}``。写它的只有交付对账
    #: （`deliverable_publishing._record_delivery_block`），读它的只有
    #: `deliverable_publishing.delivery_block_holds` —— 后者是这份记录的**唯一**
    #: 解读处：它现算 `missing_path` 现在还在不在，在了就当这条记录已作废。
    #:
    #: 存证据而不存判决，是因为判决会随世界变化而失效而记录不会自己更新：数据根
    #: 搬回来、`git worktree repair` 修好链接之后，一个写死的 "never_retry" 会永远
    #: 把一条本来能交付的 run 关在门外，而且没人会知道。存下"当时缺的是哪个路径"，
    #: 谁都能拿这条记录自己重跑一遍那个判断。
    delivery_block: Mapped[dict | None] = mapped_column(_json_type(), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)

    __table_args__ = (
        ForeignKeyConstraint(
            IDENTITY_COLUMNS,
            [f"sessions.{column}" for column in IDENTITY_COLUMNS],
            ondelete="CASCADE",
            name="fk_runs_session_scope",
        ),
        UniqueConstraint(*IDENTITY_COLUMNS, "id", name="uq_runs_scope_id"),
        CheckConstraint("prompt_tokens >= 0", name="ck_runs_prompt_tokens_nonnegative"),
        CheckConstraint("completion_tokens >= 0", name="ck_runs_completion_tokens_nonnegative"),
        CheckConstraint("total_tokens >= 0", name="ck_runs_total_tokens_nonnegative"),
        CheckConstraint("cost IS NULL OR cost >= 0", name="ck_runs_cost_nonnegative"),
        CheckConstraint("retry_count >= 0", name="ck_runs_retry_count_nonnegative"),
        CheckConstraint("usage_coverage IN ('complete', 'partial')", name="ck_runs_coverage"),
        Index("ix_runs_tenant_created", "tenant_id", created_at.desc(), id.desc()),
        Index("ix_runs_tenant_session", "tenant_id", "session_id"),
    )


class RunAttempt(Base):
    """One durable execution attempt for a logical Run."""

    __tablename__ = "run_attempts"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_new_id)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    workspace_id: Mapped[str] = mapped_column(String(64), nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False)
    session_id: Mapped[str] = mapped_column(String(64), nullable=False)
    run_id: Mapped[str] = mapped_column(String(128), nullable=False)
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default=AttemptStatus.CREATED)
    worker_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    # Frozen before dispatch. Containers are replaceable materializations of
    # this contract; neither a worker nor a resumed command may widen it.
    sandbox_manifest: Mapped[dict | None] = mapped_column(_json_type(), nullable=True)
    sandbox_manifest_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    exit_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)

    __table_args__ = (
        ForeignKeyConstraint(
            (*IDENTITY_COLUMNS, "run_id"),
            [*(f"runs.{column}" for column in IDENTITY_COLUMNS), "runs.id"],
            ondelete="CASCADE",
            name="fk_attempts_run_scope",
        ),
        UniqueConstraint("tenant_id", "run_id", "attempt_no", name="uq_attempts_tenant_run_number"),
        CheckConstraint("attempt_no >= 1", name="ck_attempts_number_positive"),
        Index("ix_attempts_tenant_session", "tenant_id", "session_id"),
    )


class ExecutionEvent(Base):
    """Sanitized canonical event; raw transcript bodies are never stored here."""

    __tablename__ = "execution_events"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    workspace_id: Mapped[str] = mapped_column(String(64), nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False)
    session_id: Mapped[str] = mapped_column(String(64), nullable=False)
    run_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    parent_run_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    attempt_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    origin: Mapped[str] = mapped_column(String(32), nullable=False)
    source: Mapped[dict] = mapped_column(_json_type(), nullable=False, default=dict)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    visibility: Mapped[str] = mapped_column(String(16), nullable=False)
    payload: Mapped[dict] = mapped_column(_json_type(), nullable=False, default=dict)

    source_identity: Mapped[str | None] = mapped_column(String(64), nullable=True)
    file_identity: Mapped[str | None] = mapped_column(String(200), nullable=True)
    byte_offset: Mapped[int | None] = mapped_column(Integer, nullable=True)
    raw_line_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    adapter_version: Mapped[str] = mapped_column(String(32), nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        ForeignKeyConstraint(
            IDENTITY_COLUMNS,
            [f"sessions.{column}" for column in IDENTITY_COLUMNS],
            ondelete="CASCADE",
            name="fk_events_session_scope",
        ),
        UniqueConstraint("tenant_id", "session_id", "sequence", name="uq_events_sequence"),
        UniqueConstraint(
            "tenant_id",
            "run_id",
            "file_identity",
            "byte_offset",
            "raw_line_hash",
            name="uq_events_raw_source",
        ),
        CheckConstraint("sequence >= 1", name="ck_events_sequence_positive"),
        CheckConstraint("schema_version = 1", name="ck_events_schema_v1"),
        Index("ix_events_session_sequence", "tenant_id", "session_id", "sequence"),
        Index("ix_events_tenant_run", "tenant_id", "run_id"),
    )


# ── `Run.status` 只能经漏斗写（RFC 异步运行时 D11）─────────────────────────
#
# 闸放在**列旁边**，不放在服务层：对手是"明天在某个新模块里直接
# `run.status = ...`"的那一次赋值，而它不会路过任何一个服务层函数。
# 护栏在被绕过那侧等于没有。
#
# 出处的记录在 `app.services.run_status.project_run_status` —— 政策与机制分开：
# 这里只回答"这次写是不是从漏斗来的"，不判断"该不该写"。


class RunStatusWriteError(RuntimeError):
    """有人绕过漏斗直接写 `Run.status`。

    绕过去的那一次不会留下出处，于是它盖错时没有任何痕迹 —— 而这正是
    2026-08-21 那条 run 收到两次终态、库里只剩最后一个值的成因。
    """


#: 漏斗正在写哪些实例。用**实例集合**而不是全局开关：并发的两次写不会
#: 互相授权（一个开关会让 A 的写给 B 开门）。
_RUN_STATUS_WRITERS: set[int] = set()


@contextmanager
def allow_run_status_write(run: "Run") -> Iterator[None]:
    """漏斗用它给自己那一次写开门。别的地方不许调 —— 调了就等于没有闸。"""
    token = id(run)
    _RUN_STATUS_WRITERS.add(token)
    try:
        yield
    finally:
        _RUN_STATUS_WRITERS.discard(token)


@event.listens_for(Run.status, "set", propagate=True)
def _refuse_run_status_writes_outside_the_funnel(target, value, oldvalue, _initiator):
    """第二个写者在**运行时**就撞墙，不用等某天有人去 grep。

    扫盘闸能告诉你"多了一个写点"，但它只在有人跑测试时说话。这条在生产里
    也成立 —— 而要防的那类缺陷恰好是"在生产里悄悄发生、库里只剩最后一个值"。

    两个合法例外：
      · **创建**（`oldvalue` 还没有值）—— 定初值不是改状态；
      · 从库里**装载**根本走不到这里（SQLAlchemy 装载不发 set 事件）。
    """
    if id(target) in _RUN_STATUS_WRITERS:
        return value
    if oldvalue in (NO_VALUE, NEVER_SET, None):
        return value
    raise RunStatusWriteError(
        f"Run.status 只能经 app.services.run_status.project_run_status 写"
        f"（试图 {oldvalue!r} → {value!r}）。直接赋值不留出处，"
        "而没有出处的状态变化正是 D11 要消灭的那一类。"
    )


class Command(Base):
    """Durable idempotent control-plane command."""

    __tablename__ = "commands"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=_new_id)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    workspace_id: Mapped[str] = mapped_column(String(64), nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False)
    session_id: Mapped[str] = mapped_column(String(64), nullable=False)
    run_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    actor_user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    payload: Mapped[dict] = mapped_column(_json_type(), nullable=False, default=dict)
    result: Mapped[dict | None] = mapped_column(_json_type(), nullable=True)
    error: Mapped[dict | None] = mapped_column(_json_type(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        ForeignKeyConstraint(
            IDENTITY_COLUMNS,
            [f"sessions.{column}" for column in IDENTITY_COLUMNS],
            ondelete="CASCADE",
            name="fk_commands_session_scope",
        ),
        UniqueConstraint(
            "tenant_id", "actor_user_id", "idempotency_key", name="uq_commands_idempotency"
        ),
        Index("ix_commands_tenant_session", "tenant_id", "session_id"),
    )


class Decision(Base):
    """Durable Decision with an immutable authority snapshot."""

    __tablename__ = "decisions"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    workspace_id: Mapped[str] = mapped_column(String(64), nullable=False)
    project_id: Mapped[str] = mapped_column(String(64), nullable=False)
    session_id: Mapped[str] = mapped_column(String(64), nullable=False)
    run_id: Mapped[str] = mapped_column(String(128), nullable=False)
    attempt_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default=DecisionStatus.PENDING)
    subtype: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    context: Mapped[dict] = mapped_column(_json_type(), nullable=False, default=dict)
    choices: Mapped[list] = mapped_column(_json_type(), nullable=False, default=list)
    recommended_choice_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    selected_choice_id: Mapped[str | None] = mapped_column(String(128), nullable=True)

    authority_type: Mapped[str] = mapped_column(String(32), nullable=False)
    authority_subjects: Mapped[list] = mapped_column(_json_type(), nullable=False, default=list)
    required_approval_count: Mapped[int] = mapped_column(Integer, nullable=False)
    action_set_version: Mapped[str] = mapped_column(String(64), nullable=False)
    policy_snapshot_id: Mapped[str] = mapped_column(String(128), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    accepted_responses: Mapped[list] = mapped_column(_json_type(), nullable=False, default=list)

    created_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    resolved_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)

    __table_args__ = (
        ForeignKeyConstraint(
            (*IDENTITY_COLUMNS, "run_id"),
            [*(f"runs.{column}" for column in IDENTITY_COLUMNS), "runs.id"],
            ondelete="CASCADE",
            name="fk_decisions_run_scope",
        ),
        CheckConstraint(
            "required_approval_count >= 1", name="ck_decisions_approval_count_positive"
        ),
        Index("ix_decisions_current", "tenant_id", "session_id", "status"),
    )


EXECUTION_TABLES = [
    SessionProjection.__table__,
    Run.__table__,
    RunAttempt.__table__,
    ExecutionEvent.__table__,
    Command.__table__,
    Decision.__table__,
]

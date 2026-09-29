"""Single-process local worker that persists the same canonical execution projection."""

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy import inspect as sa_inspect
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.execution import (
    TERMINAL_RUN_STATUSES,
    AttemptStatus,
    Command,
    Run,
    RunAttempt,
    RunStatus,
    SessionProjection,
)
from app.models.model_backend import ModelBackendConfig
from app.models.project import OperationMode, Project, ProjectConfig
from app.models.resource import ProjectResource
from app.models.user import User
from app.services import run_failures
from app.services.research_migration import upgrade_records_before_use
from app.services.execution_ingest import (
    PAUSE_OFFER_KEY,
    DecisionAuthoritySnapshot,
    ExecutionIngestService,
    IngestContext,
)
from app.services.execution_observers import (
    notify_execution_observers,
    publish_run_transient,
)
from app.services.app_events import record_app_event
from app.services.harness_runtime import harness_bridge_supported
from app.services.harness_sessions import (
    AppRunBinding,
    HarnessSessionStaleError,
    harness_session_manager,
    reasoning_backend,
)

# 这四个的实现在 `harness_transcript_ingest`：同一件事有实时与补齐两条到达
# 路径，实现只许有一份（补齐路径见 `session_event_replay`）。名字保持不变 ——
# 它们被几条按行为固定下来的测试点着名。
from app.services.harness_transcript_ingest import (  # noqa: F401
    _approved_runtime_root,
    _harness_adapter_state,
    _owning_transcript,
    _session_runtime_root,
    ingest_transcript_wrapper,
)
from app.services.instructions import publish_research_settings
from app.services.model_backends import effective_backend, resolve_role_bindings
from app.services.model_role_catalog import REASONING_ROLE
from app.services.attempts import latest_attempt, latest_attempt_no
from app.services.redaction import DEFAULT_REDACTION_POLICY
from app.services.research_settings import (
    compile_research_context,
    effective_research_settings,
    research_settings_snapshot,
    research_settings_snapshot_ref,
)
from app.services.run_liveness import ACTIVITY_LEASE_SECONDS
from app.services.run_status import project_run_status
from app.services.session_naming import schedule_session_autoname
from app.services.sessions import (
    append_session_message,
    build_platform_context_snapshot,
    set_session_title_from_first_message,
)
from app.services.user_interface import language_for


def _resource_sandbox_mounts(resource: ProjectResource) -> list[tuple[Path, str]]:
    """Resolve a logical resource binding through deployment-owned policy."""
    binding = str(resource.workspace_binding or "").strip()
    if not binding:
        return []
    declared = settings.sandbox_mount_bindings.get(binding)
    # A Project resource is a logical registration, not an availability claim.
    # An unconfigured binding simply grants no filesystem capability.
    if declared is None:
        return []
    if not isinstance(declared, dict):
        raise RuntimeError(f"Sandbox mount binding {binding!r} is invalid")
    raw_path = str(declared.get("path") or "").strip()
    mode = str(declared.get("mode") or "ro").strip().lower()
    candidate = Path(raw_path).expanduser()
    if not raw_path or not candidate.is_absolute() or mode not in {"ro", "rw"}:
        raise RuntimeError(f"Sandbox mount binding {binding!r} is invalid")
    try:
        path = candidate.resolve(strict=True)
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"Sandbox mount binding {binding!r} is unavailable") from exc
    return [(path, mode)]


async def attempt_the_worker_runs_in(
    db: AsyncSession, binding: "AppRunBinding | None", *, run_id: str = ""
) -> RunAttempt | None:
    """这个停着的 worker 真正跑在哪个 attempt 里 —— 按它自报的绑定取，取不到才退回
    "这条 run 最新的那个"。

    worker 的沙箱是它出生时那份，不随平台后来的记账变。answer / 和解 / 失败记账
    三处此前各自拿 `latest_attempt` 或 `attempt_no == 1`，与 worker 手里那个可以是
    三个不同的行 —— 账面上"最新的"可以是一次为答复错租出来的空壳。
    """
    attempt_id = str(getattr(binding, "sandbox_attempt_id", "") or "").strip()
    if attempt_id:
        found = await db.get(RunAttempt, attempt_id)
        if found is not None:
            return found
    run_id = run_id or str(getattr(binding, "run_id", "") or "")
    return await latest_attempt(db, run_id) if run_id else None


async def _freeze_attempt_sandbox_manifest(
    db: AsyncSession,
    *,
    context: IngestContext,
    operation_id: str,
    authorized_risk_classes: tuple[str, ...] = (),
) -> tuple[IngestContext, RunAttempt]:
    """Commit the attempt and its immutable capability before worker dispatch."""
    from app.services.harness_contract import sandbox_module
    from app.services.project_repository import get_project_repository

    sandbox = sandbox_module()

    workspace = (
        get_project_repository()
        .session_path(context.project_id, context.session_id)
        .resolve(strict=True)
    )
    grants: dict[Path, str] = {workspace: "rw"}
    # harness 自己的代码要**读得到**。
    #
    # 2026-09-06 真机实测（Mac 安装包，一个真课题跑到 experiment 节点）：模型写好
    # 了 `ising_mc.py` / `run_scan.py` / `analyze.py`，三条执行路径全被拒 ——
    # `safe_run_bash` / `safe_execute_python` 报
    # `read-only root was not frozen into this RunAttempt: …/app/harness`。
    #
    # 原因就在这一行：冻进 attempt 的挂载只有会话工作区一个（外加数据集资源）。
    # 而跑命令的工具要求 harness 根可读 —— 那是它要执行的代码本身。两边都对，
    # 合起来是一道**结构性**的墙：从平台这条路进来的 attempt，本机执行永远过不了
    # preflight。Docker 年代这堵墙看不见（harness 烘在镜像里，不需要挂载），
    # PR C 把 Docker 删掉之后它就露出来了，而 macOS 上又没有 docker 兜底。
    #
    # 只读，不可写：这不放宽任何写边界 —— 写边界仍然只有工作区那一个。
    harness_root = Path(settings.harness_root).expanduser().resolve()
    if harness_root.is_dir() and not harness_root.is_relative_to(workspace):
        grants[harness_root] = "ro"
    resources = list(
        (
            await db.scalars(
                select(ProjectResource).where(
                    ProjectResource.tenant_id == context.tenant_id,
                    ProjectResource.project_id == context.project_id,
                    ProjectResource.is_enabled.is_(True),
                )
            )
        ).all()
    )
    for resource in resources:
        if resource.resource_type not in {"dataset", "storage"}:
            continue
        for path, mode in _resource_sandbox_mounts(resource):
            if path == workspace or path.is_relative_to(workspace):
                continue
            existing_mode = grants.get(path)
            if existing_mode is not None and existing_mode != mode:
                raise RuntimeError(f"Conflicting sandbox modes for deployment binding path {path}")
            grants[path] = mode
    # SQL row order is not capability identity. Parent mounts precede children
    # so an explicitly writable child of a read-only dataset tree is stable.
    mounts = [
        (str(path), grants[path])
        for path in sorted(grants, key=lambda item: (len(item.parts), str(item)))
    ]

    latest = await db.scalar(
        select(RunAttempt)
        .where(
            RunAttempt.tenant_id == context.tenant_id,
            RunAttempt.run_id == context.run_id,
        )
        .order_by(RunAttempt.attempt_no.desc())
        .limit(1)
        .with_for_update()
    )
    terminal = {
        AttemptStatus.COMPLETED.value,
        AttemptStatus.FAILED.value,
        AttemptStatus.CANCELLED.value,
        AttemptStatus.STALE_UNKNOWN.value,
    }

    # 「谁来守」由隔离层说（core.isolation.attempt_capability）：原生后端
    # （seatbelt / Landlock+bwrap）冻后端名。Docker 镜像身份随 PR C 一起没了。
    from app.services.harness_contract import isolation_module

    capability = isolation_module().attempt_capability()

    def build(attempt_id: str):
        manifest = sandbox.SandboxManifest(
            attempt_id=attempt_id,
            run_id=context.run_id,
            mounts=tuple(mounts),
            ceiling=sandbox.SandboxCeiling.from_environment(),
            authorized_risk_classes=tuple(sorted(set(authorized_risk_classes))),
            backend=capability["backend"],
        )
        manifest.validate()
        return manifest

    attempt = latest
    if attempt is None:
        attempt = RunAttempt(
            tenant_id=context.tenant_id,
            workspace_id=context.workspace_id,
            project_id=context.project_id,
            session_id=context.session_id,
            run_id=context.run_id,
            attempt_no=1,
            status=AttemptStatus.LEASED,
        )
        db.add(attempt)
        await db.flush()
    elif attempt.status in terminal:
        attempt = RunAttempt(
            tenant_id=context.tenant_id,
            workspace_id=context.workspace_id,
            project_id=context.project_id,
            session_id=context.session_id,
            run_id=context.run_id,
            attempt_no=attempt.attempt_no + 1,
            status=AttemptStatus.LEASED,
        )
        db.add(attempt)
        await db.flush()

    candidate = build(str(attempt.id))
    if attempt.sandbox_manifest_hash and attempt.sandbox_manifest_hash != candidate.sha256:
        # The prior capability is immutable. A changed mount/image/resource
        # contract is a new execution attempt, never an in-place widening.
        attempt.status = AttemptStatus.RELEASED
        attempt.exit_reason = "sandbox_capability_changed"
        attempt.ended_at = datetime.now(UTC)
        replacement = RunAttempt(
            tenant_id=context.tenant_id,
            workspace_id=context.workspace_id,
            project_id=context.project_id,
            session_id=context.session_id,
            run_id=context.run_id,
            attempt_no=attempt.attempt_no + 1,
            status=AttemptStatus.LEASED,
        )
        db.add(replacement)
        await db.flush()
        attempt = replacement
        candidate = build(str(attempt.id))

    attempt.sandbox_manifest = candidate.canonical_payload()
    attempt.sandbox_manifest_hash = candidate.sha256
    attempt.worker_id = f"local-dispatch:{operation_id}"
    now = datetime.now(UTC)
    attempt.heartbeat_at = now
    attempt.lease_until = now + timedelta(seconds=ACTIVITY_LEASE_SECONDS)
    context = replace(context, attempt_no=attempt.attempt_no)
    await db.flush()
    return context, attempt


logger = logging.getLogger(__name__)

ProgressCallback = Callable[[dict], Awaitable[None]]
# task → 它是**哪一类**后台工作。
#
# 生命周期管理对所有类别都一样（关服时全部收掉，不能泄漏）；但"还有几个在
# 跑"这个问题，问的人心里指的是某一类。原来这里是个无标签的 set，于是会话
# 自动命名（`_autoname`）和用户的研究执行（`run_worker`）落进同一个计数器，
# 任何针对其一的断言都会被另一个扰动 —— 实测 `test_sse_observer_disconnect_
# does_not_cancel_server_owned_execution` 单跑必红（数到 2），整套跑才碰巧
# 是 1，且 xdist 一分派就随机红。
#
# 两件事就要两个口径。收拢仍按全体，计数按类别。
_DETACHED_EXECUTION_TASKS: dict[asyncio.Task[None], str] = {}

EXECUTION_KIND = "execution"
"""用户的研究执行 —— SSE 断开也不许杀。"""

HOUSEKEEPING_KIND = "housekeeping"
"""平台自己的杂活（会话命名之类）—— 没跑完也不影响研究是否在进行。"""

REJOIN_KIND = "rejoin"
"""接回上一个后端派出去、此刻仍在飞的那一轮（#785）—— 和 execution 一样不许
被 SSE 断开杀掉；关机时同样撤销（worker 不受影响，下一个后端再接）。"""


@dataclass(frozen=True)
class RejoinTarget:
    """「这一轮不是我发起的」—— execute_local_turn 的 rejoin 模式要知道的全部。

    - `request_id`：worker 正在跑（或刚跑完）的那次 RPC 的 id（activity.turn_id）。
    - `run_id`：那一轮所属的平台 Run（接回来的绑定里的 run_id）。
    - `disk_result`：worker 在断连窗口里已经跑完时，从 events.jsonl 取出的终止
      result（`data` 那份）。非空 = 不再等 socket。
    - `before_wait`：登记好观众之后、开始等终止事件之前要做的事 —— 补断连
      窗口里落盘的事件。放在这个位置是因为两段之间不能有缝。
    """

    request_id: str
    run_id: str
    disk_result: dict | None = None
    before_wait: Callable[[], Awaitable[None]] | None = None


def retain_detached_execution(task: asyncio.Task[None], *, kind: str = EXECUTION_KIND) -> None:
    """Keep a server-owned execution alive after its HTTP observer disconnects."""
    _DETACHED_EXECUTION_TASKS[task] = kind

    def release(completed: asyncio.Task[None]) -> None:
        _DETACHED_EXECUTION_TASKS.pop(completed, None)
        if not completed.cancelled():
            # Retrieve any unexpected exception so the event loop does not emit
            # an unowned-task warning. The runner normally converts failures
            # into durable Run state before returning.
            completed.exception()

    task.add_done_callback(release)


async def shutdown_detached_executions() -> None:
    """Stop server-owned tasks during application shutdown, never on SSE close."""
    tasks = list(_DETACHED_EXECUTION_TASKS)
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


def detached_execution_count(kind: str | None = EXECUTION_KIND) -> int:
    """还有几个后台 task 在跑。`kind=None` = 不分类别，全体。"""
    if kind is None:
        return len(_DETACHED_EXECUTION_TASKS)
    return sum(1 for k in _DETACHED_EXECUTION_TASKS.values() if k == kind)


def _now() -> str:
    return datetime.now(UTC).isoformat()


async def _open_command_for_run(db: AsyncSession, *, run_id: str) -> "Command | None":
    """上一个后端替这一轮建的、还没收尾的 command（#785）。

    找不到不是错误：老后端可能在建 command 之前就退场了。找到多条（不该发生）
    取最新的一条 —— 收尾只认一次。
    """
    return await db.scalar(
        select(Command)
        .where(
            Command.tenant_id == settings.runtime_tenant_id,
            Command.run_id == run_id,
            Command.result.is_(None),
            Command.error.is_(None),
        )
        .order_by(Command.created_at.desc())
        .limit(1)
    )


def _offer_id(pause: dict) -> str | None:
    """这次呈递的 id。取不到就是取不到 —— 不编一个。

    整份呈递由 harness 挂在 pause 的 `offer` 键下（`core.decision_offer` 的
    `PAUSE_OFFER_KEY`），中间各层只搬运它、不按字段名列举它。这里也一样：
    只从那一个对象里取，不去别处凑一个"看起来像 id"的值。
    """
    offer = pause.get(PAUSE_OFFER_KEY) if isinstance(pause, dict) else None
    if not isinstance(offer, dict):
        return None
    value = offer.get("offer_id")
    return str(value) if isinstance(value, str) and value.strip() else None


def _pause_status(pause: dict) -> str:
    metadata = pause.get("metadata") if isinstance(pause.get("metadata"), dict) else {}
    return (
        "waiting_permission"
        if metadata.get("type") in {"highrisk_confirm", "permission"}
        else "waiting_human"
    )


async def _validate_resume_binding(
    db: AsyncSession,
    *,
    binding: AppRunBinding,
    user_id: str,
    project_id: str,
    conversation_id: str,
) -> Run:
    """Prove an in-memory pause still addresses the same durable running attempt."""
    if (
        binding.user_id != user_id
        or binding.conversation_id != conversation_id
        or binding.session_id != conversation_id
    ):
        raise HarnessSessionStaleError(
            "The live pause is not bound to this user and Research Session"
        )
    run = await db.scalar(
        select(Run).where(
            Run.tenant_id == settings.runtime_tenant_id,
            Run.project_id == project_id,
            Run.session_id == conversation_id,
            Run.id == binding.run_id,
        )
    )
    if run is None:
        raise HarnessSessionStaleError("The live pause no longer matches an actionable durable Run")
    # ── 活 pause 是最强的活性证据，账面与它矛盾时一律听现场 ────────────────
    #
    # 这个函数只在 `paused_binding` 存在时被调，而那个属性要求 session.alive
    # 且 session.paused —— 一个活体进程**此刻**正停在 pause 上等人。这不是
    # 记录，是现场。账面（run.status / summary / attempt.status）说它死了、
    # 不可续、attempt 关了 —— 那是账面自己的失败：
    #   · 2026-08-17：decision 记录被拒 → 整轮误判 failed → 用户重发永远撞
    #     "stale"，session 永久卡死。
    #   · 2026-08-24：attempt 出生 100ms 被清扫误判 stale_unknown → 33 分钟后
    #     用户答复时这里按账本拒答、再往账上盖 resumable=false —— 于是 UI
    #     照常呈卡片（run 还是 waiting_human）而每次答复都被拒，**两出口互指**，
    #     用户无限撞墙。曾经这里有三道按账面拒答的闸（状态白名单 / resumable
    #     标志 / attempt 必须 RUNNING），每一道都把"账本"当权威、把活着的
    #     现场当嫌疑人 —— 方向反了，全部删除。
    #
    # 唯一不和解的两个状态，判据是**人的明确意志**，不是账面推断：
    #   · CANCELLED —— 人按过停止，续跑违背它；
    #   · COMPLETED —— 与"还在等答复"真矛盾（pause 注册表才是坏的），续跑
    #     会在一个已冻结的结论后面接着写。
    if run.status in {RunStatus.CANCELLED.value, RunStatus.COMPLETED.value}:
        raise HarnessSessionStaleError(
            "The live pause addresses a Run that a human already closed "
            f"({run.status}); answering it would contradict that decision"
        )
    attempt = await attempt_the_worker_runs_in(db, binding)
    summary = dict(run.summary) if isinstance(run.summary, dict) else {}
    # `summary.resumable` 曾经也是这里的一个合取项 —— 那是一条**关于未来的
    # 落盘判决**（"这个暂停还能不能续"），而这一整段的立场恰恰是：账面不许
    # 否决活现场。判决进了账面，就会出现"账面说不能续 → 拒答 → 再往账面盖一次
    # 不能续"的自我加固（2026-08-24 用户无限撞墙那次）。剩下的合取项全是**事实**
    # （状态、见证、attempt 有没有关），事实可以留。
    contradicted = (
        run.status not in {RunStatus.WAITING_HUMAN.value, RunStatus.WAITING_PERMISSION.value}
        or bool(summary.get("staleReason"))
        or attempt is None
        or attempt.status != AttemptStatus.RUNNING.value
        or attempt.ended_at is not None
    )
    if not contradicted:
        return run
    # 和解 = 把账面改回现场事实，然后放行。每一个写都有一个**当下可观测**的
    # 东西支撑（worker 真实存在的活 pause），并把观测记进 summary —— 这属于
    # "转述事实"，不是 D11 禁止的"落判决"。
    observed_at = datetime.now(UTC).isoformat()
    summary["reconciledFromLivePause"] = {
        "previousStatus": run.status,
        "at": observed_at,
    }
    summary.pop("staleReason", None)
    summary.pop("staleDetectedAt", None)
    run.summary = summary
    if run.status not in {
        RunStatus.WAITING_HUMAN.value,
        RunStatus.WAITING_PERMISSION.value,
    }:
        # pause 的具体类别（human/permission）在账面失败时已经被冲掉，从现场
        # 只能确知"在等人答复"。两种 waiting 状态对 resume 路径等价。
        project_run_status(
            run,
            RunStatus.WAITING_HUMAN,
            source="live_pause_reconciliation",
            evidence={
                "previousStatus": summary["reconciledFromLivePause"]["previousStatus"],
                "observedAt": observed_at,
                "pauseId": str(binding.run_id),
            },
        )
    if attempt is not None and (
        attempt.status != AttemptStatus.RUNNING.value or attempt.ended_at is not None
    ):
        # attempt 被谁关的不重要（清扫误判 / 记账层失败）：它承载的 pause 就在
        # 眼前活着，这一趟没结束。ended_at 必须一起清 —— `observed_status` 拿
        # 它当"这一趟结束了"的正面证据，留着它等于账面继续说两种话。
        attempt.status = AttemptStatus.RUNNING.value
        attempt.exit_reason = None
        attempt.ended_at = None
    # attempt 行整个不存在时不造一行假的：后续 ingest（`_ensure_attempt` /
    # `_close_attempt`）会按事件如实补。放行即可。
    logger.warning(
        "run %s: ledger contradicted a live pause (was %s); reconciled from the scene",
        run.id,
        summary["reconciledFromLivePause"]["previousStatus"],
    )
    await db.flush()
    return run


@dataclass(frozen=True)
class AutonomyPolicy:
    """这一趟"自己往下跑"的完整授权说明。

    两个事实**同源**：一次配置读取同时给出"要不要无人值守"和"预授权哪些
    高危类别"。分成两个函数各读一次，就是同一个问题两个真相源 —— 而分叉时
    两边都不报错（模式开着、授权范围读成空，症状是"自主跑但每个高危点都
    停"，正是 2026-08-10 那次静默挂两小时的形状）。
    """

    unattended: bool = False
    authorized_risk_classes: tuple[str, ...] = ()
    #: UI 上的三档，原样送到 worker：assisted / autonomous / continuous。自主档不预
    #: 授权任何类别时列表是空的，与协作在 worker 眼里没有区别 —— 档位要显式说。
    mode: str = "assisted"
    #: 想无人值守但这台机器守不住 —— 给人看的一句话（缺什么、还能怎么办）。
    #: 非空 ⇒ `unattended` 已经被降回 False。
    blocked_reason: str = ""
    #: 放行但要标注：资源墙弱，跑飞没有自动刹车。
    resource_wall_note: str = ""


async def project_autonomy_policy(db: AsyncSession, project: Project | None) -> AutonomyPolicy:
    """UI 上选的档位要给 harness 什么授权。**这个问题只有这一个答案。**

    此前这个模式只被用来决定"跑完要不要自动 publish 版本"；harness 那边的
    AUTO_APPROVE_ENABLED 只有 CLI(chat.py) 设过，平台没有任何通往它的路径 ——
    于是从 UI 出发不管选哪种模式，每个决策点都停人（E2E v16 实测）。

    `authorized_risk_classes` 是 2026-08-10 补的第三段：自主模式此前只有
    "全停"一档，于是 experiment 提交真实作业时必然停下等一个不存在的人。
    """
    if project is None:
        return AutonomyPolicy()
    try:
        config = await db.scalar(
            select(ProjectConfig).where(ProjectConfig.project_id == project.id)
        )
    except Exception:
        # 读一个**可选设置**失败，绝不能改变执行失败的形状。第一版把这次查询
        # 放在包住模型调用的 try 里，于是它一抛异常就顶掉了真正的 failure，
        # `failure["code"]` 直接消失（test_failed_formal_turn_survives_reload
        # 立刻变红）。读不到 = 不是 autonomous，就这么简单。
        return AutonomyPolicy()
    if not (config and config.operation_mode == OperationMode.AUTONOMOUS):
        return AutonomyPolicy()
    raw = config.autonomous_authorized_risk_classes or []
    classes = tuple(str(c).strip() for c in raw if isinstance(c, str) and str(c).strip())
    mode = "continuous" if "*" in classes else "autonomous"

    # 这台机器守不守得住"放着不管地跑"。`missing_for_unattended` 从执行器分档
    # 那天起就一直在算，却**没有任何人读它做决定**（issue #798）——一个算出来
    # 没人消费的判据，等于那道防线不在场。判决在这里做，因为这里是"要不要无人
    # 值守"唯一的答案。
    from app import assembly
    from app.services.unattended import judge_unattended

    verdict = judge_unattended(
        _machine_missing_for_unattended(),
        weak_resource_walls_are_acceptable=assembly.weak_resource_walls_are_acceptable(),
    )
    if not verdict.allowed:
        # 降的是"无人值守续轮"，不是档位：决策卡仍按自主 / 连续自动放行。
        return AutonomyPolicy(
            unattended=False, authorized_risk_classes=classes, blocked_reason=verdict.reason,
            mode=mode,
        )
    return AutonomyPolicy(
        unattended=True, authorized_risk_classes=classes, resource_wall_note=verdict.note,
        mode=mode,
    )


def _machine_missing_for_unattended() -> list[str]:
    """这台机器离"能放着不管地跑"还差哪几条。

    事实由启动时的探针写进 `app.state.execution_boundary`（`main._record_what_
    this_machine_enforces`）。读不到就当"什么都不缺" —— 桥没开时根本不会有
    无人值守的作业，在那里凭一个读不到的事实拒绝，只会制造一个指不到病因的
    拒绝。
    """
    try:
        from app.main import app as _app

        record = getattr(_app.state, "execution_boundary", None) or {}
    except Exception:  # noqa: BLE001 - 读不到事实不该改变执行的形状
        return []
    return [str(item) for item in (record.get("missing_for_unattended") or [])]


def answer_pending_decisions_with_recommendation(project_id: str) -> int:
    """切成连续档的那一刻，替人点掉**已经挂在屏幕上**的推荐项。

    切档送达（broadcast_autonomy → sync）只改变"以后的决策点要不要停"。
    切的那一刻已经停着的那张卡，worker 只是攥着 pause 等一次 answer 派发 ——
    开关翻了它也不会自己走。2026-08-24 现场：人对着 post_node 决策卡把档位
    切成「连续」，卡还在，一切照旧等人；对用户来说就是"连续模式不生效"。

    只替人点**呈递方自己声明过推荐**的那一项，且只在 `waiting_human` 上：

    - `waiting_permission`（高危审批）不碰。连续档对高危点的放行发生在执行
      那一刻的 bypass；回头把一条已经拍在人脸上的高危命令自动批掉，是拿两个
      各自无害的机制组合出销毁。
    - 没有推荐项的问题不猜。

    作答走 `execute_local_turn` —— 和人点按钮**同一条路**：决策落账、消息
    落盘、run 状态推进全部沿用既有路径，不另起一套"自动答复"。

    返回排出去的任务数（每个停着的会话一个；条件复核在任务里做，因为它要
    读库，而调用方在一个刚 commit 完的请求事务里）。
    """
    scheduled = 0
    for binding in harness_session_manager.paused_bindings_for_project(project_id):
        task = asyncio.create_task(_answer_with_recommendation(binding))
        retain_detached_execution(task)
        scheduled += 1
    return scheduled


async def _answer_with_recommendation(binding: "AppRunBinding") -> None:
    from app.database import get_session_factory
    from app.models.execution import Decision, DecisionStatus

    async with get_session_factory()() as db:
        run = await db.get(Run, binding.run_id)
        if run is None or run.status != RunStatus.WAITING_HUMAN.value:
            return
        decision = await db.scalar(
            select(Decision)
            .where(
                Decision.run_id == binding.run_id,
                Decision.status == DecisionStatus.PENDING,
            )
            .order_by(Decision.created_at.desc())
        )
        if decision is None or not decision.recommended_choice_id:
            return
        recommended = str(decision.recommended_choice_id)
        # 替人点的也是**这一张卡**：答复必须带这次呈递的身份。此前这里发的是
        # `offer_id=None`，运行时无从判它答的是哪一张，于是它落在了人没看见的
        # 下一张卡上（2026-09-09 node20）。没有身份的卡不替人答。
        offer_id = str((decision.context or {}).get("offerId") or "").strip()
        if not offer_id:
            return
        label = next(
            (
                str(choice.get("label") or "").strip()
                for choice in (decision.choices or [])
                if isinstance(choice, dict) and choice.get("choiceId") == recommended
            ),
            None,
        )
        if not label:
            # 推荐了一个不在选项集里的 id —— 呈递自身不一致，不猜，留给人。
            return
        user = await db.get(User, binding.user_id)
        conversation = await db.get(SessionProjection, binding.session_id)
        project = await db.get(Project, decision.project_id)
        if user is None or conversation is None or project is None:
            return
        # 出手前最后一刻还停着才答，把"与人同时点按钮"的窗口收到最小。
        # （真撞上也只是两个一模一样的答复走同一把会话锁，后到的那个照
        # 决策账本的"已答"处理 —— 与两个人同时点按钮完全同形。）
        if (
            harness_session_manager.paused_binding(str(project.id), str(conversation.session_id))
            is None
        ):
            return

        async def _no_observer(_event: dict) -> None:
            return None

        await execute_local_turn(
            db,
            user=user,
            conversation=conversation,
            message=label,
            project=project,
            on_progress=_no_observer,
            choice={"offer_id": offer_id, "choice_id": recommended},
        )


def rejoin_adopted_session(session: "_ProjectHarnessSession") -> None:
    """接回一个活 worker 之后：把它正在跑（或刚跑完）的那一轮接到本进程（#785）。

    这是 `HarnessSessionManager.set_adoption_handler` 的生产接线。同步、只起
    一个后台任务 —— 它在注册表锁里被调，不许 await。任务按 `REJOIN_KIND` 记在
    分离执行表里：SSE 断开不杀，关机时撤销（worker 不受影响，下一个后端再接）。
    """
    task = asyncio.create_task(_rejoin_worker_session(session))
    retain_detached_execution(task, kind=REJOIN_KIND)


async def _rejoin_worker_session(session: "_ProjectHarnessSession") -> None:
    """接回那一轮的两种形状，同一条收尾。

    1. **在飞**（接回时 activity 自报 working / parked，带 turn_id）：登记观众、
       补断连窗口、等终止 result 从 socket 来。停靠中的 worker 也在这一类 ——
       它在 `run_unattended` 这次 RPC 里，醒了接着跑，最终的 result 一样从这里收。
    2. **断连窗口里跑完了**（接回时已 idle / waiting_human，而 DB 上的 run 仍是
       需要活进程的状态）：终止 result 已经在 events.jsonl 里（JsonlEmitter 先
       落盘再转发），从盘上取出来收尾，不等一个不会再来的 socket 事件。

    两种都走 `execute_local_turn(rejoin=…)`：收尾只有一份实现。

    「哪条 run」：activity 里的绑定优先（working / parked / waiting_human 都带）；
    worker 回到 idle 时它自己会把绑定清掉（"从不更新的字段不是事实"），那就按
    会话找 —— 会话面严格串行，一个 session 同一时刻至多一条顶层 run 在飞。
    """
    from app.database import get_session_factory
    from app.models.execution import REQUIRES_LIVE_RUNTIME_STATUSES
    from app.services.harness_sessions import _replay_worker_events, terminal_result_on_disk

    live_statuses = {status.value for status in REQUIRES_LIVE_RUNTIME_STATUSES}
    factory = get_session_factory()
    async with factory() as db:
        binding = session.binding
        if binding is not None:
            run = await db.get(Run, binding.run_id)
            owner_id = binding.user_id
        else:
            run = await db.scalar(
                select(Run)
                .where(
                    Run.project_id == session.project_id,
                    Run.session_id == session.session_id,
                    Run.parent_run_id.is_(None),
                    Run.status.in_(sorted(live_statuses)),
                )
                .order_by(Run.created_at.desc())
                .limit(1)
            )
            owner_id = session.owner_user_id
        if run is None or run.status not in live_statuses:
            return  # 上一个后端已经收过尾，或者这条 run 根本不在 —— 没有账要补
        user = await db.get(User, owner_id)
        conversation = await db.get(SessionProjection, run.session_id)
        project = await db.get(Project, run.project_id)
        if user is None or conversation is None or project is None:
            logger.warning(
                "Cannot rejoin run %s: its user/session/project row is missing", run.id
            )
            return
        request_id = session.inflight_request_id
        disk_result: dict | None = None
        if not request_id:
            disk_result, request_id = terminal_result_on_disk(run)
            if disk_result is None:
                # 既没有在飞的一轮，盘上也没有这条 run 的终止 result：那它就是
                # 真的没收到派发（上一个后端在建行之后、发 RPC 之前退场了）。
                # 留给启动对账按既有判据处理，这里不编一个结局、也不认主。
                return
            disk_result = {**disk_result, "_session_resumable": session.paused}
        if binding is None:
            # 让接下来的路径（占用判据、答复闸、下一次接回）都认得这条 run 有主。
            session.binding = binding = AppRunBinding(
                user.id, str(conversation.session_id), run.id, run.session_id
            )

        async def _fill_the_gap() -> None:
            # 断连到重连之间落盘的事件（幂等：按水位补）。在飞那一类放在观众
            # 登记**之后**跑（`before_wait`），两段之间才没有缝。
            async with factory() as replay_db:
                await _replay_worker_events(replay_db, run=run, owner_user_id=binding.user_id)
                await replay_db.commit()

        if disk_result is not None:
            await _fill_the_gap()

        async def _no_observer(_event: dict) -> None:
            return None

        try:
            await execute_local_turn(
                db,
                user=user,
                conversation=conversation,
                message="",
                project=project,
                on_progress=_no_observer,
                rejoin=RejoinTarget(
                    request_id=request_id,
                    run_id=run.id,
                    disk_result=disk_result,
                    before_wait=None if disk_result is not None else _fill_the_gap,
                ),
            )
        except asyncio.CancelledError:
            raise  # 关机：worker 照跑，下一个后端再接
        except Exception:
            # execute_local_turn 自己把失败落成 run 的终态了（transport_failure）；
            # 这里只留一条日志，别让一个后台任务的异常变成"未被等待"的警告。
            logger.exception("Rejoining run %s ended in an error", run.id)


def interrupted_by_our_own_shutdown(exc: BaseException) -> bool:
    """这次子进程没了，是**我们自己**在关机时掐的吗？

    子进程那边只看得见一个 `-15`（SIGTERM），而 `-15` 可能来自我们的重启，也
    可能来自 OOM killer 或运维手工 kill。区分它们需要的信息只有 App Server
    自己有：进没进关机流程。

    分不清的代价是真的（2026-08-12 实测）：重启一次，在跑的 run 落成
    `failed` + "Review the request, then continue or revise the request" ——
    **研究本身没问题，是平台把它掐了**，而用户被建议去改一件没错的东西。

    「这次研究失败了」和「平台重启打断了它」是两件事，终态和给用户的话都不同。
    """
    from app.services.harness_sessions import HarnessSessionProcessError
    from app.services.lifecycle import is_shutting_down

    return is_shutting_down() and isinstance(exc, HarnessSessionProcessError)


def terminal_state_for(exc: BaseException) -> tuple[str, str | None]:
    """这次失败该落什么终态 —— **只在这里判一次**。返回 (run 状态, staleReason)。

    ## 为什么必须是一个函数

    "决定终态"原来散在**三处**：`is_answer` 分支、fail-loud 记账、以及记账自己
    也失败时的兜底路径。2026-08-12 我给前两处接上了"平台重启打断"的区分，实测
    仍然落成 `failed` —— 因为真正写下终态的是第三处，而它不知道有这回事。

    加防线之前先问"覆盖全不全"，不然新机制只是**又一条没接到所有路径的路**。
    三处各判一次 = 三份会各自演化的判据，且分叉时谁都不报错。

    ## 三类，不是两类

        HarnessSessionStaleError  运行时丢了，原因不明   → stale_unknown
        我们自己关机掐的           工作没错，接着做就行   → stale_unknown
        其它                      这次研究确实失败了      → failed

    前两类都进可恢复的那一类，但 `staleReason` 不同 —— "不知道它怎么了"和
    "我们把它掐了"对排查是完全不同的信息。
    """
    from app.services.harness_sessions import HarnessSessionStaleError

    if interrupted_by_our_own_shutdown(exc):
        return RunStatus.STALE_UNKNOWN.value, "app_server_shutdown"
    if isinstance(exc, HarnessSessionStaleError):
        return RunStatus.STALE_UNKNOWN.value, "harness_process_lost"
    if getattr(exc, "code", None) == "pause_pending":
        # 第四类：**根本不是失败**。run 正停在一个决策点上等人，守卫拦下了一次
        # 重入 —— 拦截成功是防线在工作，不是事故。
        #
        # 落成 `failed` 的代价不对称到离谱：2026-08-13 实测，一个跑完全部产出、
        # 拿到 reviewer APPROVE 4/5 的 run，在 pause 之后 47ms 被这条路径判死。
        # 下游只要按 `status='failed'` 判活儿，已过审的成品就成了废品。
        #
        # 正确终态就是它**本来已经在的**那个：waiting_human。这里不改写它。
        return RunStatus.WAITING_HUMAN.value, None
    return RunStatus.FAILED.value, None


def _sanitized_platform_failure(exc: Exception, *, run_id: str = "", lang: str = "zh") -> dict[str, object]:
    """这次失败对用户的说法。文案与分类都在 `run_failures` 里，这里只是接线。

    原来这个函数自己拼 `f"…: {str(exc)}"` —— 于是一条 SQLAlchemy 的
    `IntegrityError`（连同整条 INSERT 语句和全部参数）直接出现在会话正文里。
    脱敏做了，但**脱敏干净的 SQL 转储仍然是 SQL 转储**：脱敏和"翻译成人话"是
    两件事，被一个函数当成一件做了。

    现在 `str(exc)` 只经由 `detail` 落地，正文一律取自文案表 —— 新异常泄露原文
    这件事从"默认发生"变成"不可能发生"。
    """
    known_cause = "app_server_restarted" if interrupted_by_our_own_shutdown(exc) else None
    failure = run_failures.describe(
        exc,
        reference=run_id,
        # 平台比异常本身更清楚这次是怎么回事：子进程只看得见一个 -15。
        known_cause=known_cause,
        lang=lang,
    ).as_record()
    # 翻译成人话是给**用户**的；技术真相必须同时留给运维，否则这条 run 死得
    # 无从追查。
    #
    # 2026-08-18 实测：一条 experiment run 做完全部裁决后失败，用户看到的是
    # 兜底文案「平台在记录这次运行时撞上了内部错误」，而服务端日志里**一个字
    # 都没有** —— 这个模块此前连 logger 都没有。239 万 tokens 的工作报废，
    # 事后无法定位是哪一步炸的。
    #
    # 文案表那句「完整技术细节挂在 reference 上」得有个地方兑现：reference 就是
    # run_id，用它在日志里找得到这一条。
    if known_cause == "app_server_restarted":
        # 自己重启打断的，不是故障：留一行可检索的记录就够，不刷栈。
        logger.info(
            "run %s interrupted by app server shutdown (code=%s)",
            run_id or "<unknown>",
            failure.get("code"),
        )
    else:
        logger.exception(
            "run %s failed: code=%s exc=%s",
            run_id or "<unknown>",
            failure.get("code"),
            type(exc).__name__,
        )
    exit_code = getattr(exc, "exit_code", None)
    if isinstance(exit_code, int):
        failure["exitCode"] = exit_code
    return failure


def _demo_reply(message: str, project: Project | None) -> str:
    cleaned = " ".join(message.split())
    if project is None:
        return (
            f"我已收到这项研究请求：“{cleaned[:160]}”。\n\n"
            "当前使用的是本机 deterministic demo 模型后端；本次对话已经真实创建 "
            "Session、Run、Attempt 和持久化事件。你可以先创建或打开一个 Project，再让我把"
            "任务推进为项目内的研究产物。"
        )
    return (
        f"已在项目「{project.name}」中推进任务：“{cleaned[:180]}”。\n\n"
        "本次执行已完成需求规范化、项目上下文对齐和第一版研究备忘录生成。"
        "备忘录已作为真实 Artifact 写入数据库，同时记录了完整的 Run/Event 执行轨迹。\n\n"
        "建议下一步：明确交付物类型、时间范围、必须使用的数据/文献，以及可验收的"
        "成功标准。"
    )


async def _model_reply(
    role_bindings,
    message: str,
    project: Project | None,
    user: User,
    project_id: str,
    conversation_id: str,
    run_id: str,
    session_id: str,
    platform_context_snapshot: dict | None,
    is_answer: bool,
    on_progress: ProgressCallback,
    on_protocol_event: ProgressCallback,
    sandbox_attempt: RunAttempt | None = None,
    autonomy: "AutonomyPolicy | None" = None,
    choice: dict | None = None,
    rejoin: "RejoinTarget | None" = None,
):
    if rejoin is not None:
        # 接回在飞那一轮（#785）：请求不由本进程发出，只接它的终止 result。
        # worker 在断连窗口里就跑完了的那种，result 已在 events.jsonl 里
        # （`disk_result`），不再等 socket —— 它不会再来第二次。
        if rejoin.disk_result is not None:
            result = rejoin.disk_result
        else:
            result = await harness_session_manager.rejoin(
                user_id=user.id,
                project_id=project_id,
                session_id=session_id,
                run_id=run_id,
                request_id=rejoin.request_id,
                on_progress=on_progress,
                on_protocol_event=on_protocol_event,
                before_wait=rejoin.before_wait,
            )
    elif is_answer:
        result = await harness_session_manager.answer(
            user=user,
            project_id=project_id,
            conversation_id=conversation_id,
            run_id=run_id,
            session_id=session_id,
            answer=message,
            choice=choice,
            on_progress=on_progress,
            on_protocol_event=on_protocol_event,
            # 答复也是一次派发，同样从库里取当下的授权范围。少了这一句，
            # 授权就只有"出发时"那一个写入时机，中途改配置永远不生效。
            autonomy=autonomy,
            sandbox_attempt=sandbox_attempt,
        )
    else:
        backend = reasoning_backend(role_bindings)
        if backend.provider == "demo":
            reply = _demo_reply(message, project)
            return (
                reply,
                {
                    "prompt_tokens": max(1, len(message) // 3),
                    "completion_tokens": max(1, len(reply) // 3),
                    "total_tokens": max(2, (len(message) + len(reply)) // 3),
                    "cost": 0,
                    "currency": "USD",
                    "coverage": "complete",
                },
                None,
            )
        if not settings.harness_bridge_enabled:
            raise RuntimeError("The formal Harness bridge is disabled for non-demo model backends")
        if not harness_bridge_supported(backend):
            raise RuntimeError(
                f"Provider '{backend.provider}' is not supported by the formal Harness bridge"
            )
        result = await harness_session_manager.turn(
            user=user,
            project_id=project_id,
            conversation_id=conversation_id,
            run_id=run_id,
            session_id=session_id,
            message=message,
            role_bindings=role_bindings,
            platform_context_snapshot=platform_context_snapshot,
            on_progress=on_progress,
            on_protocol_event=on_protocol_event,
            autonomy=autonomy,
            sandbox_attempt=sandbox_attempt,
        )
    status = str(result.get("status") or "")
    if status not in {"completed", "paused", "cancelled", "void", "failed"}:
        raise RuntimeError(f"Harness returned unsupported terminal status '{status or 'missing'}'")
    pause = result.get("pause_event") if isinstance(result.get("pause_event"), dict) else {}
    reply = str(result.get("final_text") or "")
    if status == "paused":
        reply = str(pause.get("question") or "Harness execution is waiting for human input.")
    elif not reply and status == "cancelled":
        reply = "Harness execution was cancelled."
    elif not reply and status == "void":
        reply = "Harness execution ended without a valid scientific result."
    elif not reply and status == "failed":
        reply = "Harness execution failed."
    raw_tokens = (
        result.get("tokens_used_delta")
        if result.get("tokens_used_delta") is not None
        else result.get("tokens_used")
    )
    tokens = max(0, int(raw_tokens or 0))
    return (
        reply,
        {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": tokens,
            "cost": None,
            "currency": None,
            "coverage": "partial",
        },
        result,
    )


async def cancel_local_run(
    db: AsyncSession,
    *,
    user: User,
    run: Run,
    reason: str,
) -> bool:
    """Kill one live local Harness process and durably project cancellation."""
    if run.status == RunStatus.CANCELLED.value:
        return True
    killed = await harness_session_manager.cancel_run(
        user_id=str(user.id),
        project_id=str(run.project_id),
        session_id=str(run.session_id),
        run_id=str(run.id),
    )
    if not killed:
        return False

    service = ExecutionIngestService()
    context = IngestContext(
        tenant_id=run.tenant_id,
        workspace_id=run.workspace_id,
        project_id=run.project_id,
        session_id=run.session_id,
        run_id=run.id,
        actor_user_id=str(user.id),
    )
    raw = {"at": _now(), "event": "run_end", "status": "cancelled"}
    encoded = json.dumps(raw, ensure_ascii=False, sort_keys=True).encode()
    await service.ingest_raw_record(
        db,
        context=context,
        file_identity=f"app-cancel:{run.id}",
        byte_offset=0,
        raw_line=encoded,
        raw=raw,
        adapter_state={},
    )
    commands = list(
        (
            await db.scalars(
                select(Command).where(
                    Command.tenant_id == run.tenant_id,
                    Command.run_id == run.id,
                    # 同上：还没有结果的那些，就是还没收尾的那些。
                    Command.result.is_(None),
                    Command.error.is_(None),
                )
            )
        ).all()
    )
    for command in commands:
        # 取消也是一个**结果**：写下它，命令就不再"没有结果"了。
        command.error = {"code": "cancelled", "message": reason}
    refreshed_run = await db.get(Run, run.id, populate_existing=True)
    if refreshed_run:
        refreshed_run.summary = {
            **(refreshed_run.summary or {}),
            "title": "Execution cancelled",
            "cancelReason": reason,
        }
    await db.commit()
    await notify_execution_observers()
    return True


async def execute_local_turn(
    db: AsyncSession,
    *,
    user: User,
    conversation: SessionProjection,
    message: str,
    project: Project | None,
    on_progress: ProgressCallback,
    choice: dict | None = None,
    system_continuation: bool = False,
    rejoin: "RejoinTarget | None" = None,
) -> dict:
    """`system_continuation=True`：这一轮不是用户开口，是平台自己接着跑（重启后
    从断点续 continuous 研究）。它只改一件事 —— 那条驱动消息记成 `role="system"`
    而不是 `role="user"`，于是它在会话里显示成一条系统注记，不冒充用户说过的话
    （这正是 wangd 反对 startup_resume 的那一点：伪造用户消息）。驱动语义不变：
    continuous 项目照样走 run_unattended，一路跑到完成。

    `rejoin` 非空：这一轮**不是本进程发起的**（#785）。上一个后端把它派给 worker
    之后退场了（部署 / 崩溃），worker 一直在跑，事件一直在落盘；本进程接回那个
    worker 之后，由这里接上它的终止 result 并把**同一份收尾**（run 终态 / pause
    呈递 / 交付 / token 记账）在本进程跑完。所以它跳过的只有"发起"那几步 ——
    冻结沙箱 attempt、建 command、落用户消息、起标题 —— 那些上一个后端已经做过；
    收尾一步不少，也不另写一份。"""
    service = ExecutionIngestService()
    project_id = str(project.id) if project else f"global-{user.id[:8]}"
    # 这一趟会话的**全部**模型角色绑定，一次解析齐。会话上可以显式钉住主
    # 模型（conversation.model_backend_id），辅助角色仍按注册表当下的指派走。
    #
    # 为什么整份一起解析：worker 的绑定是 spawn 时定死的，指纹覆盖全部角色。
    # 少解析一个角色 = 那个角色永远是"未配置"，而 UI 上明明指派好了 —— 那
    # 正是这次要拆掉的那种缝（机制存在但没接到路径）。
    role_bindings = await resolve_role_bindings(db, user)
    pinned = (
        await db.get(ModelBackendConfig, conversation.model_backend_id)
        if conversation.model_backend_id
        else None
    )
    if pinned is not None:
        role_bindings[REASONING_ROLE] = pinned
    elif REASONING_ROLE not in role_bindings:
        role_bindings[REASONING_ROLE] = await effective_backend(db, user)
    backend = role_bindings[REASONING_ROLE]
    backend_id = str(backend.id)
    if project and not conversation.platform_context_snapshot:
        conversation.platform_context_snapshot = build_platform_context_snapshot(
            project=project,
            session=conversation,
            backend=backend,
        )
        await db.flush()
    platform_context_snapshot = (
        dict(conversation.platform_context_snapshot)
        if isinstance(conversation.platform_context_snapshot, dict)
        else None
    )
    actor_user_id = str(user.id)
    # 这个人用哪种语言读失败文案 —— **在这里取，不在失败路径上取**。
    #
    # `language_for(user)` 读的是 ORM 属性。走到失败路径时这一轮已经
    # commit 过（SQLAlchemy 默认让属性过期），再读就要触发一次惰性刷新；
    # 在 async 会话上那是 `MissingGreenlet`。而那次抛出发生在
    # 「fail-loud 记账」的 try 里，被吞掉之后整条记账路径跳过，用户收到的
    # 是兜底文案 `execution_failed` —— 真实原因（记录升级被拒）连同它的
    # 可执行建议一起消失。2026-09-16 合 i18n 时实测：
    # `test_a_refused_upgrade_fails_this_turn_and_hands_over_the_migrator_reason`
    # 拿到的 code 是 execution_failed。
    #
    # 事实要在它还读得出来的时候取下来。
    language = language_for(user)
    # 平台自己接着跑的那一轮，驱动消息记成系统注记、不挂在任何用户名下 ——
    # 显示层据 role 区分（ChatMessages 已有 role==="system" 支路），不会画成
    # "You 说了……"。role 是唯一的真相源，不另发一个"这是不是系统消息"的标志。
    incoming_role = "system" if system_continuation else "user"
    incoming_actor = None if system_continuation else actor_user_id
    # 名字不能和 `research_settings.research_settings_snapshot`（函数）撞 ——
    # 同名局部变量会把那个 helper 在本函数里遮成一个 dict，而遮住之后的调用
    # 报的是 "'dict' object is not callable"，指向的原因跟真因毫无关系。
    session_research_settings = (
        dict(conversation.research_settings_snapshot)
        if isinstance(conversation.research_settings_snapshot, dict)
        else None
    )
    research_settings_ref = research_settings_snapshot_ref(session_research_settings)
    # 研究设置是结构化的，指令层是文本 —— 渲染成文本落到这个用户的 harness
    # home 里，worker 每轮和 PROFILE.md 一起读。
    #
    # 从前它走两条路，**两条都不好**：
    #   · 拼进指令快照的 personal 层 —— 冻在建会话那一刻，此后用户改设置对这个
    #     会话永久无效（nidy 那个会话活了 19 天）；
    #   · 一个 `research_profile_snapshot` 字段随请求下发 —— `platform_runtime`
    #     一个字都不读（零消费者），而测试恰恰断言在这一条上。
    #
    # ⚠️ 行为变化：下发的是**当下**的有效设置，不是建会话时那一份。个人偏好
    # 冻三个星期不是特性；而 harness home 按用户分（不按会话分），一个用户的
    # 两个会话本来也放不下两份不同的冻结值。会话行上的
    # `research_settings_snapshot` 保留为"它建出来时是什么样"的记录。
    live_settings = research_settings_snapshot(await effective_research_settings(db, user=user))
    publish_research_settings(user.id, compile_research_context(live_settings))
    # 接回在飞那一轮时，"这是不是在答一个 pause"这个问题不成立：那一轮是什么
    # 由 worker 的 result 说了算，run 的身份由接回来的绑定给定。
    paused_binding = (
        None
        if rejoin is not None
        else harness_session_manager.paused_binding(project_id, str(conversation.session_id))
    )
    if paused_binding:
        await _validate_resume_binding(
            db,
            binding=paused_binding,
            user_id=user.id,
            project_id=project_id,
            conversation_id=str(conversation.session_id),
        )
    is_answer = paused_binding is not None
    if rejoin is not None:
        run_id = rejoin.run_id
    else:
        run_id = paused_binding.run_id if paused_binding else f"run_{uuid4().hex}"
    session_id = paused_binding.session_id if paused_binding else str(conversation.session_id)
    operation_id = uuid4().hex
    context = IngestContext(
        tenant_id=settings.runtime_tenant_id,
        workspace_id=user.group_id or user.institution_id,
        project_id=project_id,
        session_id=session_id,
        run_id=run_id,
        actor_user_id=actor_user_id,
        decision_authority=DecisionAuthoritySnapshot(
            authority_type="initiating_user",
            authority_subjects=(user.id,),
            required_approval_count=1,
            action_set_version="harness-normal-v1",
            policy_snapshot_id="local-runtime-v1",
        ),
    )
    state: dict = {}
    harness_states: dict[str, dict] = {}
    offset = 0
    command: Command | None = None

    async def ingest(raw: dict) -> None:
        nonlocal context, offset
        # 摄取前对齐到**当前那一次** attempt。写错一位，此后所有事件会静默
        # 记到别的 attempt 名下（唯一约束不会报错，只是分到另一行去）。
        attempt_no = await latest_attempt_no(db, run_id)
        if attempt_no and attempt_no != context.attempt_no:
            context = replace(context, attempt_no=attempt_no)
        raw = {"at": _now(), **raw}
        encoded = json.dumps(raw, ensure_ascii=False, sort_keys=True).encode()
        await service.ingest_raw_record(
            db,
            context=context,
            file_identity=f"local-worker:{run_id}:{operation_id}",
            byte_offset=offset,
            raw_line=encoded,
            raw=raw,
            adapter_state=state,
        )
        offset += len(encoded) + 1
        # The control-plane projection must be visible while model/tool work is
        # still running. Keep every projection transaction short and durable.
        await db.commit()
        await notify_execution_observers()

    # 事实的写者，和这一轮的写者，是两个协程。
    #
    # `ingest_harness_protocol` 由 **reader task** 调（harness_sessions
    # `_read_forever`），`execute_local_turn` 自己跑在 worker task 上。它们从前
    # 共用 `db` 这一个 AsyncSession —— 一个 session、两个写者、两套事务边界。
    # 三件事全是它的推论：
    #
    #   · 2026-08-25 00:03 事故：终止事件派发给主协程之后，reader 还在摄取
    #     后面两条 transcript（events.jsonl 里 result 在 .789976、transcript
    #     在 .794000/.794122）。主协程此时已经在 `_connection_for_bind()` 里
    #     取连接 → InvalidRequestError → `except` 里的 `db.rollback()` 跟着炸
    #     成 IllegalStateChangeError，**原始异常被吞掉**，用户拿到一句没有信息
    #     量的兜底文案。
    #   · turn 中途的 commit 替 reader 半截的摄取定了稿，反之亦然。
    #   · turn 的 rollback 把 reader 已经落下的**事实**一起销毁 —— 那一轮
    #     4463055 token、两小时的研究，库里 0 条 assistant 消息。
    #
    # 事实是"已经发生的"，不可回滚；这一轮算成还是算败是判断，本来就该能回滚。
    # 两者共用一条事务边界，回滚就必然误伤。所以摄取在**自己的事务**里跑完、
    # 自己提交；`db` 从此只有 turn 协程一个写者。
    _fact_writer = asyncio.Lock()

    async def _drain_fact_writer() -> None:
        """等在飞的摄取落完，然后让 `db` 重新去库里读。

        终止事件到达不等于 reader 读完了 —— 它后面还跟着几条。这一轮的收尾
        必须在**事实全部落库之后**开始，否则它读到的是半截。锁本身就是屏障：
        拿得到 = 没有摄取在飞。
        """
        async with _fact_writer:
            pass
        # 摄取在另一个 session 里提交；这一轮手上的 ORM 对象是提交**之前**的
        # 副本（`expire_on_commit=False`，见 app/database.py）。只刷新事实写者
        # 真正会碰的那两行。
        #
        # ⚠️ 不用 `expire_all()`：异步下它是个陷阱 —— 过期属性下一次被读到时
        # 要发一次 IO，而那次 IO 不在 greenlet 里，当场 MissingGreenlet，而且
        # 报错点会落在某个无辜的属性访问上，跟真正的原因隔着十万八千里。
        await db.refresh(conversation)
        run_row = await db.get(Run, run_id)
        if run_row is not None:
            await db.refresh(run_row)

    async def ingest_harness_protocol(event: dict) -> None:
        if event.get("type") == "token_delta":
            text = event.get("text")
            if isinstance(text, str) and text:
                text = text[:65_536]
                await publish_run_transient(
                    tenant_id=context.tenant_id,
                    session_id=session_id,
                    run_id=run_id,
                    event={"type": "token", "text": text},
                )
                await on_progress(
                    {
                        "event": "token.delta",
                        "text": text,
                        "transient": True,
                    }
                )
            return
        if event.get("type") != "transcript" or not isinstance(event.get("event"), dict):
            return
        # 一次摄取 = 一个事务。`_fact_writer` 保证事实这一侧也是单写者
        # （reader 只有一个 task，锁在这里是"屏障拿得到"的那半边意义）。
        from app.database import get_session_factory

        async with _fact_writer, get_session_factory()() as fact_db:
            await _ingest_one_fact(fact_db, event)
        await notify_execution_observers()

    async def _ingest_one_fact(fact_db: AsyncSession, event: dict) -> None:
        # 摄取本身只有一份实现（补齐路径调的是同一个函数）。
        fact_context = context
        attempt_no = await latest_attempt_no(fact_db, run_id)
        if attempt_no and attempt_no != context.attempt_no:
            fact_context = replace(context, attempt_no=attempt_no)
        native_event = await ingest_transcript_wrapper(
            fact_db,
            service=service,
            context=fact_context,
            event=event,
            harness_states=harness_states,
        )
        if native_event.get("event") == "workspace_changed":
            await on_progress(
                {
                    "event": "workspace.changed",
                    "tool": native_event.get("tool_name"),
                    "detail": (
                        f"{native_event.get('node_type') or 'node'} changed "
                        f"{int(native_event.get('files_changed') or 0)} Project file(s)"
                    ),
                    "iteration": 1,
                }
            )
        elif native_event.get("event") == "workspace_checkpoint_requested":
            from app.services.project_repository import (
                get_project_repository,
                run_in_repository_thread,
            )

            # checkpoint 失败**见证，不杀轮**。这里是记账层：科研工作在它跑到
            # 这一行之前就已经全部落在磁盘上了，commit 只是把它抄进 Git 历史。
            # 抄写失败让整轮陪葬，是 2026-08-22 事故的放大器 —— 一条
            # ProjectRepositoryError 从这里一路冒成 RuntimeError，用户看到
            # "内部错误"，几小时的 run 报废，而工作区里所有产物完好无损。
            # 枚举口径是"对 HEAD 的累积脏路径"（core/project_workspace.py
            # `workspace_snapshot` 的约定），跳过一次不丢任何东西：下一次
            # checkpoint 自然连同这次的改动一起重试。契约违规（越界写/符号
            # 链接/像凭据）同样适用 —— 拒绝的对象是**这次入库**，不是这一轮
            # 科研；沙箱才是写边界的墙（core/sandbox.py），这里只是第二道
            # 见证。吵的义务由下面的 workspace.checkpoint_failed 事件承担。
            checkpoint = None
            expected_head_commit = str(
                await fact_db.scalar(
                    select(SessionProjection.git_head_commit_sha).where(
                        SessionProjection.session_id == session_id
                    )
                )
                or ""
            )
            try:
                checkpoint = await run_in_repository_thread(
                    get_project_repository().checkpoint_session_workspace,
                    project_id=project_id,
                    session_id=session_id,
                    node_type=str(native_event.get("node_type") or "node"),
                    run_id=str(native_event.get("run_id") or "run"),
                    run_status=str(native_event.get("run_status") or "incomplete"),
                    workspace_prefix=str(native_event.get("workspace_prefix") or ""),
                    paths=[str(path) for path in native_event.get("paths") or []],
                    expected_head_commit=expected_head_commit,
                )
            except Exception as exc:
                logger.error(
                    "session %s: workspace checkpoint refused (turn continues): %s",
                    session_id,
                    exc,
                    exc_info=True,
                )
                await on_progress(
                    {
                        "event": "workspace.checkpoint_failed",
                        "tool": "git_checkpoint",
                        "detail": (
                            f"这次工作区入库被拒绝：{exc}。工作本身都在磁盘上，"
                            "没有丢失；下一次 checkpoint 会连同这次的改动一起重试。"
                        ),
                        "iteration": 1,
                    }
                )
            if checkpoint is not None and checkpoint.commit_sha:
                await fact_db.execute(
                    update(SessionProjection)
                    .where(SessionProjection.session_id == session_id)
                    .values(git_head_commit_sha=checkpoint.commit_sha)
                )
                # Git 那条提交在上一行返回时就已经不可撤销 —— 它是事实，必须
                # 跟事实一起落，不能挂在"这一轮算不算成功"下面。
                #
                # 从前这里有一句突兀的 `db.commit()`，注释解释它为什么必须
                # **立刻**落库：后面还隔着两个 `on_progress`，客户端一断开它们
                # 就抛，函数尾部的 commit 再也跑不到，于是磁盘上有提交、DB 不
                # 知道，下一次 checkpoint 拿陈旧期望值 fail-closed，会话当场
                # 失败（2026-08-13 E2E v26：代价是一次 8.9 小时无人值守的跑）。
                #
                # 那个坑的成因就是"不可撤销的事实被放进了可回滚的那条边界"。
                # 事实有了自己的事务之后，它不再需要那句抢跑的 commit。
            if checkpoint is not None and checkpoint.frozen_violations:
                # P6 冻结守卫拦下了对冻结路径的改动。这必须是**吵**的：静默
                # 恢复会让节点以为自己的写入成功了，然后基于一个不存在的状态
                # 继续推理（v19 里 dry_run 静默返回 success 的同款教训）。
                await on_progress(
                    {
                        "event": "workspace.frozen_paths_protected",
                        "tool": "git_checkpoint",
                        "detail": (
                            "冻结路径的改动被拒绝入库："
                            + "; ".join(
                                f"{p}（{action}）" for p, action in checkpoint.frozen_violations
                            )
                        ),
                        "iteration": 1,
                    }
                )
            if checkpoint is not None and (checkpoint.oversized_excluded or checkpoint.bulk_excluded):
                # 排除不是失败，但必须有人听见：此前 `oversized_excluded` 算出来
                # 没有任何读者，一个被剔掉的文件和一个入库的文件在界面上看不出区别。
                lines = [
                    f"{path}（{size} 字节，超过单文件上限）"
                    for path, size in checkpoint.oversized_excluded
                ] + [
                    f"{directory}/（{count} 个文件，{size} 字节，整目录超过 checkpoint 预算）"
                    for directory, size, count in checkpoint.bulk_excluded
                ]
                await on_progress(
                    {
                        "event": "workspace.checkpoint_excluded",
                        "tool": "git_checkpoint",
                        "detail": (
                            "这些内容留在工作区、没有入库："
                            + "; ".join(lines)
                            + "。数据集请用 extract_material 解到材料池，大文件登记外部存储。"
                        ),
                        "iteration": 1,
                    }
                )
            if checkpoint is not None:
                await on_progress(
                    {
                        "event": "workspace.checkpointed",
                        "tool": "git_checkpoint",
                        "detail": (
                            f"Checkpointed {len(checkpoint.paths)} Project file(s) "
                            f"with run status {checkpoint.status}"
                        ),
                        "iteration": 1,
                    }
                )
        await fact_db.commit()

    # 在进入包住模型调用的 try **之前**读一次：这是个可选设置，它的查询不能
    # 与失败路径交织。第一版放在 try 里面，查询一抛异常就污染了事务，随后
    # 记录失败的写入跟着变形，`failure["code"]` 直接消失
    # （test_failed_formal_turn_survives_reload_and_can_retry 立刻变红）。
    autonomy = await project_autonomy_policy(db, project)
    if autonomy.blocked_reason or autonomy.resource_wall_note:
        # 说出来。一个只写进日志的判决，对用户等于没发生 —— 他会一直以为
        # 自主档开着，然后奇怪为什么每个决策点都停下来问他。
        await record_app_event(
            db,
            session_id=session_id,
            kind="autonomy.downgraded" if autonomy.blocked_reason else "autonomy.weak_walls",
            run_id=run_id,
            payload={"reason": autonomy.blocked_reason or autonomy.resource_wall_note},
        )

    sandbox_attempt: RunAttempt | None = None
    try:
        if project is not None and rejoin is None and not is_answer:
            # 记录格式升级的**唯一**触发点：这个项目要被用了，而 worker 还没起。
            #
            # 它曾经挂在 `main.lifespan` 上按进程生命周期扫全部项目 —— 被拒的
            # 工作区只在日志里留一句话，撞墙的用户读到的是另一套说法，重试要
            # 退出整个应用。搬到这里之后三件事一起成立：没有旧信封时它只是一次
            # glob；升不上去就是这一轮的失败，迁移器的原话原样进技术细节；
            # 「重试」不再需要一条专门的路 —— 发下一条消息就是重试。
            #
            # 跳过 rejoin / is_answer 的理由是同一个：那两条路上 worker 已经攥着
            # 这个工作区了（它读得到才跑得起来），升级只会被自己挡回来。
            await upgrade_records_before_use(
                db,
                conversation,
                project_id=project_id,
                owner_user_id=conversation.created_by_user_id or user.id,
            )
        if rejoin is not None:
            # 发起那几步（冻结沙箱 attempt / 建 command / 落用户消息 / 起标题）
            # 上一个后端做过了。这里只把它留下的 command 找回来（找不到不拦：
            # 老后端没落成也不该让这一轮的收尾没人做），并把"本进程接手了这一轮"
            # 记进事实流 —— 事后看时间线，重启处断的不是研究，是谁在记账。
            command = await _open_command_for_run(db, run_id=run_id)
            await record_app_event(
                db,
                session_id=session_id,
                kind="run.rejoined",
                run_id=run_id,
                payload={
                    "requestId": rejoin.request_id,
                    "source": "events_file" if rejoin.disk_result is not None else "live_socket",
                },
                dedupe_key=f"rejoin:{run_id}:{rejoin.request_id}",
            )
            await db.commit()
        elif is_answer:
            # 答复送给的是一个**已经在跑**的 worker：它的沙箱在 spawn 那一刻就定了，
            # 事实写在 activity.json 的 app_binding 里（接管时读回来的就是它）。这里
            # 不许再冻结一份清单 —— 换过 release 后 harness_root 路径不同、hash 不同，
            # 上面那段"能力变了 = 新 attempt"的规矩会把 worker 真正跑着的 attempt 释放
            # 掉并新租一个，随后答复的绑定与 worker 的对不上，一次好端端的答复被记成
            # "运行时丢了"（2026-09-09 node20，部署后第一次点卡）。那条规矩管的是
            # **起 worker** 的那一刻；答复只能用 worker 手里那个 attempt。
            if backend.provider != "demo":
                sandbox_attempt = await attempt_the_worker_runs_in(db, paused_binding)
                if sandbox_attempt is not None:
                    context = replace(context, attempt_no=int(sandbox_attempt.attempt_no or 1))
                await db.commit()
            await ingest({"event": "session_message", "role": "user", "content": message})
            await ingest({"event": "loop_resume"})
            await on_progress(
                {
                    "event": "run.resumed",
                    "detail": "Resuming the live Harness pause",
                    "iteration": 1,
                    "runId": run_id,
                    "sessionId": session_id,
                    "projectId": project_id,
                }
            )
        else:
            await ingest(
                {
                    "event": "run_start",
                    "node_type": "project_chat" if project else "global_chat",
                    "model_backend_name": backend.display_name,
                }
            )
            if backend.provider != "demo":
                context, sandbox_attempt = await _freeze_attempt_sandbox_manifest(
                    db,
                    context=context,
                    operation_id=operation_id,
                    authorized_risk_classes=autonomy.authorized_risk_classes,
                )
                await db.commit()
        if rejoin is None:
            command = await service.create_command(
                db,
                context=context,
                kind="run.answer" if is_answer else "run.start",
                idempotency_key=f"chat:{conversation.session_id}:{operation_id}",
                payload={
                    "message": message,
                    "modelBackendId": backend.id,
                    "researchSettingsSnapshot": research_settings_ref,
                    "platformContextVersion": (
                        platform_context_snapshot.get("version")
                        if platform_context_snapshot
                        else None
                    ),
                },
            )
            await db.commit()
            # Persist the command immediately.  A Research Session is a durable
            # record, so leaving or reloading the page must not make an in-flight
            # request disappear until the Harness produces its final response.
            await append_session_message(
                db,
                session_id=session_id,
                role=incoming_role,
                content=message,
                actor_user_id=incoming_actor,
                command_id=command.id,
                run_id=run_id,
            )
            set_session_title_from_first_message(conversation, message)
            await db.commit()
            # 机械标题只是兜底 —— 它是"这段话的开头"，不是"这是什么课题"。真正的
            # 短标题在后台跟这一轮并行地起（研究要几分钟，命名一秒回来），等这一轮
            # 收尾时前端本来就会重取会话，正好拿到。命名失败无声保留机械标题。
            await schedule_session_autoname(
                user_id=actor_user_id,
                project_id=project_id,
                session_id=session_id,
                backend=backend,
            )
            if not is_answer:
                await ingest({"event": "session_message", "role": "user", "content": message})
                await on_progress(
                    {
                        "event": "run.started",
                        "detail": f"Using {backend.display_name}",
                        "iteration": 1,
                        "runId": run_id,
                        "sessionId": session_id,
                        "projectId": project_id,
                    }
                )
        reply, usage, harness_result = await _model_reply(
            role_bindings,
            message,
            project,
            user,
            project_id,
            str(conversation.session_id),
            run_id,
            session_id,
            platform_context_snapshot,
            is_answer,
            on_progress,
            ingest_harness_protocol,
            sandbox_attempt=sandbox_attempt,
            autonomy=autonomy,
            choice=choice,
            rejoin=rejoin,
        )
        # 终止事件到达 ≠ reader 读完了。`_read_forever` 每读一行是"先摄取、再
        # 把事件放进等它的那个队列"，所以主协程被唤醒时，队列后面还排着几行 ——
        # 2026-08-25 那次事故里是 4 毫秒内的两条 transcript。收尾必须等它们落完
        # 再开始，否则读到的是半截事实。
        await _drain_fact_writer()
        if harness_result:
            transcript_path = Path(str(harness_result.get("transcript_path") or "")).resolve()
            state_root = _approved_runtime_root(transcript_path, project_id, session_id)
            if state_root is None or not transcript_path.is_file():
                raise RuntimeError("Harness result transcript escaped the configured state root")
            projected_run = await db.get(Run, run_id)
            token_delta = max(
                0,
                int(usage.get("total_tokens") or 0)
                - (projected_run.total_tokens if projected_run else 0),
            )
            if token_delta:
                await ingest(
                    {
                        "event": "llm_response",
                        "usage": {
                            "prompt_tokens": 0,
                            "completion_tokens": 0,
                            "total_tokens": token_delta,
                            "coverage": "partial",
                        },
                    }
                )
        else:
            await ingest({"event": "llm_response", "usage": usage})

        # The demo backend has no Harness tool loop, but it must exercise the
        # same Project v2 boundary instead of reviving the legacy Artifact bus.
        # Treat it as an isolated extension node and checkpoint its small memo.
        if project and not harness_result:
            from app.services.project_repository import (
                get_project_repository,
                run_in_repository_thread,
            )

            repository = get_project_repository()
            workspace = await run_in_repository_thread(
                repository.session_status, str(project.id), session_id
            )
            workspace_prefix = f"runs/extensions/demo/{run_id}"
            relative = f"{workspace_prefix}/response.md"
            memo = Path(workspace.path) / relative
            memo.parent.mkdir(parents=True, exist_ok=True)
            memo.write_text(
                f"# Demo research response\n\n## Request\n\n{message}\n\n"
                f"## Initial response\n\n{reply}\n",
                encoding="utf-8",
            )
            checkpoint = await run_in_repository_thread(
                repository.checkpoint_session_workspace,
                project_id=str(project.id),
                session_id=session_id,
                node_type="demo",
                run_id=run_id,
                run_status="completed",
                workspace_prefix=workspace_prefix,
                paths=[relative],
                expected_head_commit=str(conversation.git_head_commit_sha or ""),
            )
            if checkpoint.commit_sha:
                conversation.git_head_commit_sha = checkpoint.commit_sha
                await db.execute(
                    update(SessionProjection)
                    .where(SessionProjection.session_id == session_id)
                    .values(git_head_commit_sha=checkpoint.commit_sha)
                )
            await on_progress(
                {
                    "event": "workspace.checkpointed",
                    "tool": "git_checkpoint",
                    "detail": (
                        "Checkpointed the demo response in the Project worktree "
                        f"at {checkpoint.commit_sha}"
                    ),
                    "iteration": 1,
                }
            )

        harness_paused = bool(harness_result and harness_result.get("status") == "paused")
        raw_harness_status = (
            str(harness_result.get("status") or "completed") if harness_result else "completed"
        )
        projected_status = {
            "completed": "completed",
            "paused": "waiting_human",
            "cancelled": "cancelled",
            "void": "incomplete",
            "failed": "failed",
        }[raw_harness_status]
        pause_resumable = bool(
            harness_paused and harness_result and harness_result.get("_session_resumable")
        )
        raw_pause = harness_result.get("pause_event") if harness_result else None
        sanitized_pause = DEFAULT_REDACTION_POLICY.sanitize(raw_pause or {}).value
        pause = sanitized_pause if isinstance(sanitized_pause, dict) else {}
        pause_metadata = pause.get("metadata") if isinstance(pause.get("metadata"), dict) else {}
        pause_reason = _pause_status(pause)
        if harness_paused:
            projected_status = pause_reason

        # 交付（referee approve 即自动 publish，wangd 2026-08-23）：本轮就完成的 run
        # （demo / 单轮课题，projected_status 这一轮就是 completed）在这里即时发，好让
        # done 帧带上 project_commit_sha。多节点 / 后台完成的（root 完成由 transcript
        # ingestion / replay / 孤儿 reconcile 确立、本轮 projected_status 不是 completed，
        # 见 E2E v33：那趟 run.summary 全空、这段不在场）由 delivery_scheduler 的后台
        # 对账器按 **DB root run 状态** 兜。两条都走同一实现 deliver_completed_run
        # （含 ④ curator MEMORY.md flush + marker 幂等守卫），先到先发、不会双发。
        auto_published_commit_sha = None
        _config = (
            await db.scalar(select(ProjectConfig).where(ProjectConfig.project_id == project.id))
            if project
            else None
        )
        if (
            project
            and _config
            and _config.operation_mode == OperationMode.AUTONOMOUS
            and projected_status == "completed"
        ):
            from app.config import data_root
            from app.services.deliverable_publishing import deliver_completed_run

            try:
                _outcome = await deliver_completed_run(
                    db,
                    run_id=run_id,
                    project=project,
                    session=conversation,
                    user=user,
                    state_root=data_root("state").resolve(),
                )
                if _outcome is not None:
                    auto_published_commit_sha = _outcome.get("commit_sha")
                    _staged = _outcome.get("delivered") or []
                    if _outcome.get("revision_no") is not None:
                        _detail = f"Published Project revision {_outcome['revision_no']}"
                        if _staged:
                            _detail += f" ({len(_staged)} deliverable(s): {', '.join(_staged)})"
                        if _outcome.get("flushed_memory"):
                            _detail += "; flushed MEMORY.md"
                        await on_progress(
                            {
                                "event": "workspace.auto_published",
                                "tool": "git_publish",
                                "detail": _detail,
                                "iteration": 1,
                            }
                        )
                    if _outcome.get("skipped"):
                        await on_progress(
                            {
                                "event": "workspace.deliverables_staged",
                                "tool": "git_publish",
                                "detail": "skipped (evidence missing): "
                                + "; ".join(
                                    f"{s['name']}: {s['reason']}" for s in _outcome["skipped"]
                                ),
                                "iteration": 1,
                            }
                        )
                    if _outcome.get("publish_error"):
                        await on_progress(
                            {
                                "event": "workspace.publish_deferred",
                                "tool": "git_publish",
                                "detail": _outcome["publish_error"],
                                "iteration": 1,
                            }
                        )
            except Exception as _deliver_exc:  # noqa: BLE001 - 交付失败不许静默
                await on_progress(
                    {
                        "event": "workspace.deliverables_stage_failed",
                        "tool": "git_publish",
                        "detail": (
                            f"delivery failed: {type(_deliver_exc).__name__}: {_deliver_exc}"
                        ),
                        "iteration": 1,
                    }
                )
        await ingest({"event": "session_message", "role": "assistant", "content": reply})
        if harness_paused:
            await ingest(
                {
                    "event": "run_paused",
                    "reason": pause_reason,
                    "question": pause.get("question"),
                    "context": pause.get("context"),
                    "options": pause.get("options"),
                    # 这一次呈递**整份**带上。ingest 的 pause 分支只认 `offer`
                    # 这一个键（它按"平台不认识呈递的内部字段"重写过），从它里面
                    # 取 option_details / recommended_option_index。下面那几个平铺
                    # 字段**没有任何消费者**：写它们的那一版和读它们的那一版分别
                    # 演化过，于是 run.paused 事件落库只剩裸 options，会话投影拿到
                    # optionDetails=[] / offer=null / offerId=null，UI 一边显示
                    # 「Choose a response below」一边渲染零个选项，而 canSend=false
                    # 连自由文本都发不了 —— run 卡死，两边都不报错。
                    # 平铺字段保留只为兼容旧读者；权威是 offer。
                    PAUSE_OFFER_KEY: pause.get(PAUSE_OFFER_KEY),
                    "option_details": pause.get("option_details"),
                    "offer_id": pause.get("offer_id"),
                    "decision_id": pause.get("decision_id"),
                    "asking_node_type": pause.get("asking_node_type"),
                    "pause_kind": pause_metadata.get("type") or "human_input",
                    "resumable": pause_resumable,
                }
            )
        else:
            await ingest({"event": "run_end", "status": projected_status})
        run = await db.get(Run, run_id)
        primary_artifact_id = None
        if run:
            run.summary = {
                "title": (
                    "Permission required"
                    if harness_paused and pause_reason == "waiting_permission"
                    else "Human input required"
                    if harness_paused
                    else "Research request completed"
                    if projected_status == "completed"
                    else "Research request did not complete"
                ),
                "assistantMessage": reply,
                "artifactId": primary_artifact_id,
                "artifactIds": [],
                "modelBackendId": backend.id,
                "executionKernel": "local_demo" if backend.provider == "demo" else "formal_harness",
                # 这一趟**实际**用了哪种驱动，跟着这次执行走。
                #
                # 2026-08-12：wangd 问「你没开 continuous？」，我答不上来 ——
                # 平台哪儿都没记。只能去翻 `project_configs`，而那张表是**现在**
                # 的配置，不是**当时**跑的那个。配置改过之后，历史 run 到底是
                # 自主还是助理跑的，就再也没人知道了。
                #
                # 决定行为的事实必须跟着它影响的那次执行一起落盘 —— 否则事后
                # 复盘只能靠猜（判决可以现算，事实不行）。
                "drive": "unattended" if (autonomy and autonomy.unattended) else "assisted",
                "harnessRunId": harness_result.get("run_id") if harness_result else None,
                "pause": pause if harness_paused else None,
                "researchSettingsSnapshot": research_settings_ref,
                "projectCommitSha": auto_published_commit_sha,
            }
        if harness_paused and not pause_resumable:
            # 记账要落在 worker 真正跑着的那个 attempt 上。此前这里按 `attempt_no == 1`
            # 取行 —— 与答复用的、与 worker 手里的可以是三个不同的行。
            attempt = (
                sandbox_attempt
                if sandbox_attempt is not None
                else await attempt_the_worker_runs_in(db, paused_binding, run_id=run_id)
            )
            if attempt:
                # checkpoint 的位置**不落库**：它在盘上，`pause_pending_path`
                # 每次从 harness 结果里现取。原先这里把它抄进
                # `attempt.checkpoint_ref` —— 那一列全仓零读者，抄了没人看。
                attempt.status = AttemptStatus.RELEASED
                attempt.exit_reason = pause_reason
                attempt.ended_at = datetime.now(UTC)
        if command is not None:
            await service.complete_command(
                db,
                tenant_id=settings.runtime_tenant_id,
                command_id=command.id,
                result={
                    "runId": run_id,
                    "artifactId": primary_artifact_id,
                    "status": projected_status,
                },
            )
        await append_session_message(
            db,
            session_id=session_id,
            role="assistant",
            content=reply,
            actor_user_id=None,
            command_id=command.id if command is not None else None,
            run_id=run_id,
            # 停下来问人时，这条消息**就是**那一次呈递的持久形态。带上它的身份，
            # 页面才能把可点的那张卡片画在这条消息的位置上，而不是靠比对文案去
            # 猜"这两句是不是同一句"。
            offer_id=_offer_id(pause) if harness_paused else None,
        )
        # Persist the user-facing Session identity in the same transaction as
        # the terminal execution state.  Streaming response cleanup must not
        # be able to cancel this update after the terminal event is queued.
        if rejoin is None and set_session_title_from_first_message(conversation, message):
            await db.execute(
                update(SessionProjection)
                .where(SessionProjection.session_id == session_id)
                .values(title=conversation.title)
            )
        await db.commit()
        await notify_execution_observers()
        # ── 终帧送的是**同一个 view**，不是它的第三种形状（2026-09-01）────────
        #
        # 这里原来发 `status` / `resumable` / `pause` 三个平铺字段，而 REST
        # payload 发 `executionView` + `pendingApproval`，两边形状不同、语义
        #重叠。前端因此长出两套解析（`chat.terminal` 与 `session.execution`）
        # 和第三段"谁赢"的代码（`livePause = canonicalPause ?? terminal.pause`）
        # —— 三份各自演化，分叉时没有任何一层报错。
        #
        # 现在两条路送同一个对象，由**同一个函数**生产。客户端只认一种形状，
        # 也就没有"谁赢"这个问题可问。
        from app.services.sessions import session_execution_view

        _, _, view = await session_execution_view(
            db, user=user, project=project, session=conversation
        )
        return {
            "reply": reply,
            "command_id": command.id if command is not None else None,
            "run_id": run_id,
            "session_id": session_id,
            "conversation_id": conversation.session_id,
            "artifact_id": primary_artifact_id,
            "model_backend_id": backend.id,
            "status": projected_status,
            "view": view,
            "project_commit_sha": auto_published_commit_sha,
        }
    except asyncio.CancelledError:
        # Task cancellation is reserved for App Server shutdown. A browser/SSE
        # disconnect never cancels this server-owned task, and explicit Run
        # cancellation continues to flow through ``cancel_local_run``.
        await db.rollback()
        raise
    except Exception as exc:
        # 主键从身份映射里取，**不读属性**。回滚会让这一轮手上的 ORM 对象全部
        # 过期（flush 失败时回滚之前就已经过期了），此后读 `command.id` 是一次
        # 惰性刷新 —— 在 async 会话上那是 MissingGreenlet：fail-loud 记账自己
        # 先炸，失败一路掉进兜底（2026-09-24 CI run 3685：事件 INSERT 撞唯一
        # 约束 → 这一行 MissingGreenlet → 兜底）。只有异常落在**开着的事务**
        # 里才会这样，所以"模型调用失败"那类测试一直是绿的。
        command_id = sa_inspect(command).identity[0] if command is not None else None
        await db.rollback()
        if harness_session_manager.was_cancelled(run_id):
            # terminal-state: delegated to cancel_local_run
            #   —— 它已经 ingest 了 `run_end status=cancelled`。这里再写一次会
            #   和取消方抢同一个事实。委托要**写出来**而不是靠读代码的人推断，
            #   所以这行标记留着（给读代码的人和将来的静态检查）。
            #
            #   注：原文这里说"scripts/audit_run_exit_paths.py 认这个标记"。
            #   那个脚本 2026-08-11 已删除 —— 它对真实历史回放给出 0 真 6 假，
            #   而且把真 bug 重新注入之后输出一模一样（变异测试没过）。
            #   量具本身会撒谎时，比没有量具更糟。标记的价值不依赖那个脚本。
            harness_session_manager.clear_cancelled(run_id)
            return {
                "reply": "Harness execution was cancelled.",
                "command_id": command_id,
                "run_id": run_id,
                "session_id": session_id,
                "conversation_id": conversation.session_id,
                "artifact_id": None,
                "model_backend_id": backend_id,
                "status": "cancelled",
                # 没有 `view`：这条路径上库里的事务未必还能查。缺席是诚实的
                # ——客户端据此重新查一次会话，而不是拿一份编出来的局面往下走。
            }
        if is_answer:
            # ⚠️ 不变量：**任何一条失败路径都必须给 run 一个终态**，不是"大部分路径"。
            #
            # 这里原本只在 `HarnessSessionStaleError` 时标状态，别的异常直接
            # raise —— 而下面那整段 fail-loud 记账（run_end status=failed）在
            # `is_answer` 分支根本走不到。于是"回答 pause"这条路上，非 stale
            # 的异常**什么都不记**。
            #
            # 2026-08-10 E2E v25 实测：批准一次高危提交之后 LLM 后端返回
            # 503（`HarnessSessionError`，不是 Stale），harness 会话结束，
            # 而 run 停在 `running` 挂了 **2 小时 25 分**，没有任何终态事件。
            # `running` 明明在 REQUIRES_LIVE_RUNTIME_STATUSES 里（声称"需要活
            # 进程"），却没有任何人核对进程还在不在。
            #
            # 而"回答 pause"正是**无人值守审批流最常走的那条路** —— 覆盖漏掉
            # 的恰好是最没人看着的那条。
            terminal_status, stale_reason = terminal_state_for(exc)
            stale = terminal_status == RunStatus.STALE_UNKNOWN.value
            # `waiting_human` 是唯一"pause 还活着、还能被答复"的结论，所以
            # resumable 从终态推导，不再无条件写死 False。写死的代价见
            # `_require_actionable_pause`：run 停在 waiting_*、answer 因
            # 不可恢复被拒、turn 因 pause_pending 被拒 —— 两条路同时堵死。
            # 「这个 pause 还能不能被答复」—— 一个**现算**的局部判断，只在这
            # 一趟收尾里用来决定要不要关 attempt。它不落盘：写进 run.summary
            # 就成了一条关于未来的判决，而账面判决会自我加固（2026-08-24：
            # 账面说不能续 → 拒答 → 再往账面盖一次不能续，用户无限撞墙）。
            still_answerable = terminal_status == RunStatus.WAITING_HUMAN.value
            run = await db.get(Run, run_id)
            if run:
                # 出处先落，状态后写 —— 漏斗会把 summary 整份取走再加一条日志，
                # 顺序反了这几个字段会被它盖掉。
                run.summary = {
                    **(run.summary or {}),
                    "staleReason": stale_reason,
                    "failure": _sanitized_platform_failure(exc, run_id=run_id, lang=language),
                }
                project_run_status(
                    run,
                    terminal_status,
                    source="transport_failure",
                    evidence={
                        "exception": type(exc).__name__,
                        "staleReason": stale_reason,
                        "path": "answer",
                    },
                )
            # 记账要落在 worker 真正跑着的那个 attempt 上。此前这里按 `attempt_no == 1`
            # 取行 —— 与答复用的、与 worker 手里的可以是三个不同的行。
            attempt = (
                sandbox_attempt
                if sandbox_attempt is not None
                else await attempt_the_worker_runs_in(db, paused_binding, run_id=run_id)
            )
            # 还能被答复 = 这一趟没结束，attempt 必须继续 RUNNING。关掉它等于
            # 制造 `_require_actionable_pause` 里那个自相矛盾的状态：pause 挂着
            # 而它所属的 attempt 已关闭，于是 answer 被判 stale、turn 被判
            # pause_pending，run 永远卡在 waiting_*。
            if attempt and not still_answerable:
                attempt.status = AttemptStatus.STALE_UNKNOWN if stale else AttemptStatus.FAILED
                attempt.exit_reason = "harness_process_lost" if stale else "answer_turn_failed"
                attempt.ended_at = datetime.now(UTC)
            await db.commit()
            raise RuntimeError(str(exc)) from exc
        # Persist one fail-loud operation if the provider/tool failed before commit.
        try:
            failure = _sanitized_platform_failure(exc, run_id=run_id, lang=language)
            command = await db.get(Command, command_id) if command_id is not None else None
            # rejoin 模式下 command 可能本来就找不到（上一个后端没落成）——
            # 那也不许在这里替它编一条 run_start / 用户消息：那一轮的发起
            # 已经发生过，事实流里有它自己的记录。
            if command is None and rejoin is None:
                state = {}
                offset = 0
                await ingest(
                    {
                        "event": "run_start",
                        "node_type": "project_chat" if project else "global_chat",
                    }
                )
                command = await service.create_command(
                    db,
                    context=context,
                    kind="run.start",
                    idempotency_key=f"chat:{session_id}:{operation_id}",
                    payload={
                        "message": message,
                        "modelBackendId": backend_id,
                        "researchSettingsSnapshot": research_settings_ref,
                    },
                )
                await db.commit()
                await ingest({"event": "session_message", "role": "user", "content": message})
            # 终态只判一次（`terminal_state_for`），这里的 step 结论必须跟着它。
            # 写死 "failed" 是 2026-08-13 那次的直接凶手：run 已经如实 paused，
            # 47ms 后这条路径又把同一个 step 标成 failed，成品被判废。
            recorded_status = terminal_state_for(exc)[0]
            step_failed = recorded_status != RunStatus.WAITING_HUMAN.value
            await ingest({"event": "root_step_start"})
            await ingest(
                {
                    "event": "root_step_end",
                    "status": "failed" if step_failed else "paused",
                    "error": failure["message"] if step_failed else None,
                }
            )
            await ingest(
                {
                    "event": "session_message",
                    "role": "system",
                    "content": failure["message"],
                }
            )
            # 关机打断 → 可恢复态。同一个理由：研究没错，是平台把它掐了。
            await ingest(
                {
                    "event": "run_end",
                    "status": recorded_status,
                }
            )
            if command is not None:
                await service.complete_command(
                    db,
                    tenant_id=settings.runtime_tenant_id,
                    command_id=command.id,
                    error=failure,
                )
            run = await db.get(Run, run_id)
            if run:
                run.summary = {
                    # 标题跟着失败本身走 —— 平台重启打断时写 "Execution failed"
                    # 是在陈述一件没发生的事。
                    "title": str(failure.get("title") or "Execution failed"),
                    "staleReason": terminal_state_for(exc)[1],
                    "failure": failure,
                    "assistantMessage": None,
                    "modelBackendId": backend_id,
                    "executionKernel": "formal_harness",
                    "drive": ("unattended" if (autonomy and autonomy.unattended) else "assisted"),
                    "researchSettingsSnapshot": research_settings_ref,
                }
            if rejoin is None:
                await append_session_message(
                    db,
                    session_id=session_id,
                    role=incoming_role,
                    content=message,
                    actor_user_id=incoming_actor,
                    command_id=command.id if command is not None else None,
                    run_id=run_id,
                )
            # ── 一次失败**只有一个出口**（wangd 2026-08-22）────────────────
            #
            # 上面那条 `run.summary["failure"]` 是完整的一份：title / body /
            # recovery / retryable / detail / reference。前端按 `retryable`
            # 分档渲染 —— 能接着跑的画成一行灰字，要用户动手的才画成横幅，
            # 而且带着"现在能做什么"。
            #
            # 这条 system 消息是同一件事的**第二份、更笨的抄件**：只有
            # `title. body` 一句话，没有 recovery、没有分档，渲染成
            # `message-system-alert`（红图标 + role="alert"）。2026-08-22 现场
            # 于是同一句话出现两次 —— 一行灰的说"再发一次即可"，紧接着一行
            # 红的说"平台在记录这次运行时撞上了内部错误"。
            #
            # 两份抄件必然分叉，而且分叉时两边都不报错（[[一个问题一个真相源]]）。
            # 删掉抄件，不是删掉事实：执行事件里那条 `session_message` 一个字
            # 没动（上面 `ingest` 的那条），取证照旧。
            #
            # 保留的唯一情形是**权威那份没写成**：`run` 行不在时 summary 无处
            # 可落，此时这条消息是失败仅存的出口。判据是"权威那份到底在不在"，
            # 不是"我们希望它在"。
            if run is None:
                await append_session_message(
                    db,
                    session_id=session_id,
                    role="system",
                    content=failure["message"],
                    actor_user_id=None,
                    command_id=command.id if command is not None else None,
                    run_id=run_id,
                )
            persisted_session = await db.get(SessionProjection, session_id)
            if persisted_session:
                set_session_title_from_first_message(persisted_session, message)
            await db.commit()
            return {
                "reply": "",
                "command_id": command.id,
                "run_id": run_id,
                "session_id": session_id,
                "conversation_id": session_id,
                "artifact_id": None,
                "model_backend_id": backend_id,
                "status": "failed",
                # 同上：没有 `view` = "这一帧答不出局面，去重新查一次"。
                "platform_failure": failure,
            }
        except Exception:
            await db.rollback()
            # 最后一道：连**记账本身**都失败了（多半是这条连接的事务已经废了）。
            # 到这里再什么都不做，run 就停在一个声称"需要活进程"的状态上，而
            # 进程已经没了 —— 正是 2026-08-10 那次挂 2 小时 25 分的形状，只是
            # 换了个触发点。
            #
            # 用一条**独立的**连接把状态写掉：原来那条已经进了 aborted 状态，
            # 在它上面再发任何语句都会被 postgres 拒（InFailedSQLTransactionError）
            # —— 同一晚也栽过一次。
            try:
                from app.database import get_session_factory

                async with get_session_factory()() as rescue:
                    dead = await rescue.get(Run, run_id)
                    if dead and dead.status not in TERMINAL_RUN_STATUSES:
                        # 兜底路径也走同一个判定 —— 它才是真正写下终态的那一处
                        # （记账自身失败时前面两处都没跑完）。
                        rescue_status, rescue_reason = terminal_state_for(exc)
                        # 出处先落，状态后写（同上面 answer 那一处）。状态也只能经
                        # 漏斗写：这里原来直接赋值，D11 的运行时闸（08-23）之后
                        # 每一次都当场 RunStatusWriteError —— 兜底从那天起一次都
                        # 没写成过，run 停在 running。扫盘闸按名字里带 `run` 筛，
                        # `dead` 从它眼皮底下过去了。
                        dead.summary = {
                            **(dead.summary or {}),
                            "staleReason": rescue_reason or (dead.summary or {}).get("staleReason"),
                            "failure": _sanitized_platform_failure(exc, run_id=run_id, lang=language),
                            "note": "fail-loud 记账自身失败，状态由兜底路径写入",
                        }
                        project_run_status(
                            dead,
                            rescue_status,
                            source="transport_failure",
                            evidence={
                                "exception": type(exc).__name__,
                                "staleReason": rescue_reason,
                                "path": "rescue",
                            },
                        )
                        await rescue.commit()
            except Exception:
                import logging

                logging.getLogger("local_execution").exception(
                    "Could not record a terminal state for run %s", run_id
                )
        raise RuntimeError(str(exc)) from exc

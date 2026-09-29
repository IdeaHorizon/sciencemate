"""连续档研究被后端重启打断后，自动从断点续跑 —— 不需要人再开口（Layer B）。

## 为什么存在（wangd 2026-08-24）

后端重部署会 SIGKILL worker，把一条正在跑的 continuous 研究打断成僵尸。此前平台
对此的态度是"重启就是中断，等人再发一条消息"（`main.py` 那段注释）——可 continuous
档的全部意义就是**不需要人**。让"重启"这件纯运维的事把锅甩给用户，正是这里要改的。

## 为什么不是 startup_resume 那个被删掉的补丁

那一版用 `op=turn` 伪造一条**用户消息**推进（"请继续"），假消息进了用户的聊天
记录，wangd 明确反对。这里两点不同：

  1. **只对 continuous 档**。它的驱动本来就是平台自己的续轮循环（run_unattended），
     不是用户消息 —— 续跑它天经地义，不是给别人的会话硬塞话。
  2. 那条驱动消息记成 **`role="system"`**（`execute_local_turn(system_continuation=True)`），
     显示成系统注记，不冒充用户说过的话。

真正的终极解仍是"worker 不跟着后端死"（RFC 异步运行时 C 层）；在那之前，这一层
让 continuous 研究**扛得住**重启，而不是每次重启都烂在半路。

## 判据要严 —— 误驱动的代价是烧钱（[[project_hypothesis_node_token_runaway]]）

一条 run 要被自动续跑，五个条件缺一不可：

  · 顶层 run（parent_run_id 为空）——子节点跟着父 run 续，不单独驱动
  · 现算状态是 `stale_unknown`（`run_liveness.observed_status` 判它运行时真丢了：
    attempt 已结束 + 没有活 binding + 租约过期）——不是"看着像停了"
  · 见证写着 `staleReason == "app_server_restart"`——是重启造成的，不是别的
  · 项目是 continuous 档（autonomous + 预授权全部高危 `"*"`）——只有这一档
    "不需要人"，assisted / 普通 autonomous 停下等人是**对的**，不能替它做主
  · 最近才被打断（`updated_at` 在窗口内）——别复活几天前就该放弃的老僵尸

再叠一层进程内幂等：正在续的 run 不重复驱动（`_resuming`）。有活 binding 的 run
天然不会被选中（那样 observed_status 就不是 stale_unknown 了）。
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.models.execution import ASSERTS_ACTIVE_WORK, Run, RunStatus

logger = logging.getLogger(__name__)

#: 只续"最近还在真跑"的。恒真：restart 打断的那条，最后一次真实进展就在不久前。
#:
#: ⚠️ recency 判据**不能**看 `run.updated_at`：`mark_orphaned_harness_runs` 写见证
#: 的那一刻把它顶到 now —— 于是几天前就停了的老僵尸在启动后看起来全都"刚更新过"。
#: 2026-08-24 本地实测：一次重启标了 25 条孤儿，用 updated_at 判据当场误续 3 条旧
#: 测试 run（[[feedback_run_it_to_find_the_seam]]：真跑一次才照出，单测里 updated_at
#: 是手设的，照不出这条缝）。
#:
#: 正解看 attempt 的 `heartbeat_at` —— 它只在 worker 产出**真实进展**时被续
#: （D10，execution_ingest._touch_activity_lease），mark_orphaned 一个字不碰。真被
#: restart 打断的 run 心跳就在不久前；老僵尸的心跳是几小时/几天前。
RESUME_WINDOW = timedelta(hours=2)

#: 一趟最多同时拉起几条。restart 一般只打断个位数条在跑的 run；设上限是防止
#: 某次异常状态下把几十条一起点着、瞬间打爆上游。
MAX_CONCURRENT_RESUMES = 5

#: 本进程正在续的 run —— 幂等。周期性对账每 60s 跑一次，正在续的不能重复点。
#: 进程重启即清空：那时该续的会由新一轮 reconcile 重新发现（这正是我们要的）。
_resuming: set[str] = set()


@dataclass(frozen=True)
class ResumeTarget:
    run_id: str
    project_id: str
    session_id: str


async def find_resumable_continuous_runs(
    db, *, now: datetime | None = None
) -> list[ResumeTarget]:
    """挑出该自动续跑的 continuous run。纯查询 + 现算，无副作用，可单测。"""
    from app.models.execution import RunAttempt
    from app.models.project import Project, ProjectStatus
    from app.services import run_liveness
    from app.services.local_execution import project_autonomy_policy

    at = now or datetime.now(UTC)
    active = {s.value for s in ASSERTS_ACTIVE_WORK}
    recency_cutoff = at - RESUME_WINDOW

    # 顶层 + 断言在动的 run。这里**不**按 updated_at 卡新鲜度（它被见证写顶到了
    # now）；新鲜度在下面按 attempt.heartbeat_at 判。updated_at 只当一个宽松的扫描
    # 上界，别扫到远古的行。
    rows = (
        await db.execute(
            select(Run).where(
                Run.parent_run_id.is_(None),
                Run.status.in_(list(active)),
                Run.updated_at >= at - timedelta(days=7),
            )
        )
    ).scalars().all()
    if not rows:
        return []

    observed = await run_liveness.observed_status_map(db, rows, now=at)

    # 每条候选 run 的最新 attempt —— 要它的 heartbeat_at 判"最近还在真跑吗"。
    attempts = (
        await db.execute(
            select(RunAttempt)
            .where(RunAttempt.run_id.in_([r.id for r in rows]))
            .order_by(RunAttempt.run_id, RunAttempt.attempt_no)
        )
    ).scalars().all()
    latest_attempt: dict[str, RunAttempt] = {}
    for attempt in attempts:
        latest_attempt[attempt.run_id] = attempt  # 升序到达，末个即最新

    targets: list[ResumeTarget] = []
    autonomy_cache: dict[str, bool] = {}
    for run in rows:
        if run.id in _resuming:
            continue
        if observed.get(run.id) != RunStatus.STALE_UNKNOWN.value:
            continue  # 运行时没真丢（还在跑 / 已续上 / 是别的状态）
        summary = run.summary if isinstance(run.summary, dict) else {}
        if summary.get("staleReason") != "app_server_restart":
            continue  # 不是重启打断的（真崩、上游拒绝…各有各的出口，不在这兜）
        # 最近还在真跑吗 —— 看真实进展心跳，不看被见证顶过的 updated_at。
        attempt = latest_attempt.get(run.id)
        heartbeat = attempt.heartbeat_at if attempt is not None else None
        if heartbeat is None:
            continue  # 从没产出过真实进展 —— 没有可续的断点
        if heartbeat.tzinfo is None:
            heartbeat = heartbeat.replace(tzinfo=UTC)  # SQLite 存的是 naive
        if heartbeat < recency_cutoff:
            continue  # 老僵尸：上一次真跑是很久以前，不主动复活
        # 项目是不是 continuous 档 —— 每个项目只解析一次。
        if run.project_id not in autonomy_cache:
            project = await db.get(Project, run.project_id)
            if project is None or project.status == ProjectStatus.ARCHIVED:
                # 归档 = 冻结：后台不再替它干活（`project_lifecycle`）。
                autonomy_cache[run.project_id] = False
                continue
            policy = await project_autonomy_policy(db, project)
            autonomy_cache[run.project_id] = bool(
                getattr(policy, "unattended", False)
                and "*" in (getattr(policy, "authorized_risk_classes", ()) or ())
            )
        if not autonomy_cache[run.project_id]:
            continue  # assisted / 普通 autonomous：停下等人是对的，不替它做主
        targets.append(
            ResumeTarget(run_id=run.id, project_id=run.project_id, session_id=run.session_id)
        )
    return targets


#: 平台自己接着跑那一轮的驱动语。它以 role="system" 落进会话（不冒充用户），
#: 内容如实说明发生了什么、要 agent 做什么。
_CONTINUATION_MESSAGE = (
    "（平台运行时刚重启）请从断点继续这项研究：先确认当前进度与已经完成的工作，"
    "再接着往下推进，不要从头重来。"
)


async def _resume_one(factory, target: ResumeTarget) -> None:
    """把一条 run 从断点续起来。开自己的 db 会话，镜像 chat 端点的 run_worker。"""
    from app.models.execution import SessionProjection
    from app.models.project import Project
    from app.models.user import User
    from app.services.local_execution import execute_local_turn

    async def _noop_progress(_event: dict) -> None:
        return None

    try:
        async with factory() as db:
            conversation = await db.get(SessionProjection, target.session_id)
            if conversation is None or conversation.archived_at is not None:
                return
            driver_id = conversation.initiating_user_id or conversation.created_by_user_id
            user = await db.get(User, driver_id) if driver_id else None
            project = await db.get(Project, target.project_id)
            if user is None or project is None:
                logger.warning(
                    "cannot resume run %s: missing driver user or project", target.run_id
                )
                return
            logger.info(
                "auto-resuming continuous run %s after app server restart", target.run_id
            )
            await execute_local_turn(
                db,
                user=user,
                conversation=conversation,
                message=_CONTINUATION_MESSAGE,
                project=project,
                on_progress=_noop_progress,
                system_continuation=True,
            )
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - 一条续跑失败不拖垮别的
        logger.exception("auto-resume failed for run %s", target.run_id)
    finally:
        _resuming.discard(target.run_id)


async def resume_interrupted_continuous_runs(factory) -> list[str]:
    """发现被重启打断的 continuous run，逐个 fire-and-forget 续起来。

    返回这一趟**新拉起**的 run_id 列表（供日志/测试）。续跑本身是小时级的长任务，
    这里只负责点火、不 await —— 与 chat 端点起 execute_local_turn 的方式一致。
    """
    async with factory() as db:
        targets = await find_resumable_continuous_runs(db)
    if not targets:
        return []

    started: list[str] = []
    for target in targets:
        if len(started) >= MAX_CONCURRENT_RESUMES:
            logger.warning(
                "%d interrupted continuous runs found; resuming first %d this pass",
                len(targets), MAX_CONCURRENT_RESUMES,
            )
            break
        if target.run_id in _resuming:
            continue
        _resuming.add(target.run_id)
        asyncio.create_task(
            _resume_one(factory, target), name=f"resume-{target.run_id[:12]}"
        )
        started.append(target.run_id)
    return started

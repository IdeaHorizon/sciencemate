"""「这条 run 的运行时还在不在」—— **读时现算，永不落盘**。

## 为什么不能写进 run.status（RFC 异步运行时 D11）

`run.status` 是事实字段：这条 run 在干什么。往里写「我猜它没主了」是把**判决**
塞进事实。判决错了没人知道，而下游已经按它行动过了。

2026-08-21 现场：一条正在正常推进的 run（子节点 hypothesis→_reviewer→observation
全跑完了）被判成 `stale_unknown`，UI 显示「这一轮没跑完」并**停止轮询五分钟**。
后端后来把 status 改回了 `running`，但那一行的 summary 里还留着
`staleReason: app_server_restart / resumable: false` —— 同一行里两个互相矛盾的
说法并存，而事件流里从头到尾没有一条 `run.status_unknown` 支撑那个判决。

判决不落盘就没有这个问题：它每次被问到时重算，事实变了答案自然跟着变
（[[证据可持久化，判决不可以]]、[[checkpoint 权威自愈]]同款）。

## 判据

    需要活体运行时的状态  ∧  注册表里没有它的活 binding  →  运行时不在

两个条件都取自**当下**：状态来自投影器写的事实，binding 来自进程注册表。
没有第三份记录，也就没有会分叉的抄件。

## 活动租约（D10，2026-08-21 接上）

注册表是**进程内缓存**，答不出"worker 刚起来还没登记"和"worker 死了"的区别。
D10 给的答案是租约：worker 每产出一步真实进展，投影器就续一次
`RunAttempt.lease_until`（见 `execution_ingest._touch_activity_lease`）。

于是活动有了三态，而不是两态：

    working   租约还没过期 —— 它刚刚还在动
    unknown   租约过期了 —— **只说"不知道"，不说"死了"**
    idle      这个状态本来就不需要活体运行时

`unknown` 不是判决。D10 原文：「沉默衰减为 `unknown`。`unknown` 只呈现、
不判决：杀不杀由人或显式机械政策定」。所以本模块**没有任何一个函数返回
"它死了"** —— 最强的结论就是"我不知道"，而"不知道"永远不该触发销毁性动作。

两个真相源都用上：租约答"最近还在不在动"（跨进程、可持久化），注册表答
"此刻这个后端手里还有没有它"（进程内、重启即空）。任一为真即视为活着 ——
**恒不误杀优先于恒不漏报**：漏报一个僵尸的代价是它多挂一会儿，误杀一条正在
跑的研究的代价是几小时的工作没了。
"""
from __future__ import annotations

from datetime import UTC, datetime

from app.models.execution import (
    ASSERTS_ACTIVE_WORK,
    REQUIRES_LIVE_RUNTIME_STATUSES,
    TERMINAL_RUN_STATUSES,
    Run,
    RunAttempt,
    RunStatus,
)

#: 活动租约的时长。沉默超过它 → 活动状态衰减为 `unknown`（D10：unknown 只
#: 呈现、不判决）。取值要比"一步研究动作的正常间隔"宽出一个量级 —— 模型
#: 一轮思考 + 一次工具调用几十秒是常态，太短会把正常思考判成沉默。
#: 续期在 `execution_ingest._touch_activity_lease`；消费在 `activity()` 和
#: `mark_orphaned_harness_runs` 的出生宽限。
ACTIVITY_LEASE_SECONDS = 180


def _status_value(run: Run) -> str:
    status = run.status
    return status.value if isinstance(status, RunStatus) else str(status or "")


def needs_live_runtime(run: Run) -> bool:
    """这条 run 的状态是否要求有活体进程撑着。"""
    return _status_value(run) in {status.value for status in REQUIRES_LIVE_RUNTIME_STATUSES}


def activity(run: Run, *, attempt: RunAttempt | None, now: datetime,
             has_live_binding: bool) -> str:
    """这条 run 此刻的**活动**状态：`working` / `unknown` / `idle`。

    三态是 D10 概念三拆里"活动"那一维的全部取值。注意没有 `dead` ——
    这个模块永远不下那个判决。
    """
    if not needs_live_runtime(run):
        return "idle"
    if has_live_binding:
        return "working"
    lease = attempt.lease_until if attempt is not None else None
    if lease is not None:
        # 同上：库里读回来的可能是 naive（SQLite 不存时区）。
        if lease.tzinfo is None:
            lease = lease.replace(tzinfo=UTC)
        if lease > now:
            return "working"
    return "unknown"


def runtime_lost(run: Run, *, has_live_binding: bool,
                 attempt: RunAttempt | None = None,
                 now: datetime | None = None) -> bool:
    """运行时是不是没了 —— 现算，调用方**不许**把结果写回 run。

    `has_live_binding` 由调用方从注册表取（`live_binding(...).run_id == run.id`，
    子 run 看父 run 的 binding）。参数化而不是在这里去问注册表：这个模块要能
    在测试里被真实历史回放喂进去，而回放没有进程。

    ⚠️ 名字里的 "lost" 是**给调用方的措辞**，语义是 `activity() == "unknown"`
    ——"我不知道它还在不在"，不是"它死了"。调用方据此**呈现**、决定要不要
    提供恢复入口；**不许**据此销毁任何东西（[[只留证据不判决]]）。

    传了 `attempt` 就同时看活动租约：worker 最近产出过真实进展 → 还活着，
    哪怕这个后端进程刚重启、注册表是空的。不传则退化为只看注册表（老调用点
    的行为不变）。
    """
    if now is None:
        now = datetime.now(UTC)
    return activity(run, attempt=attempt, now=now, has_live_binding=has_live_binding) == "unknown"


def observed_status(run: Run, *, attempt: RunAttempt | None,
                   now: datetime | None = None,
                   has_live_binding: bool) -> str:
    """这条 run **此刻对外应当呈现**的状态 —— 现算，同样不许写回 run。

    ## 为什么需要它（2026-08-23 实测）

    D11 把"它没了"从判决改成现算之后，`run.status` 就不再被任何人改成
    `stale_unknown` 了 —— 这是对的。但**没人把现算接到读的那一端**：三个
    对外投影（会话 `executionState` / `/runs` 列表 / 右栏研究进程）读的都还是
    `run.status` 原值。于是后端每重启一次就多几条永远显示"运行中"的 run。

    当天现场：8 条这样的 run（心跳最老的停了 1 天 9 小时），顶部徽章说
    「Running」，而停止按钮回 409「Nothing is running in this Session right
    now」—— 同一个问题两个真相源，方向相反各错一边。代价不止于难看：UI 认为
    在跑 → 输入框只给"插话" → 插话排进一个没有 worker 的 run → 没人消费。
    用户被锁在一个既停不掉也续不了的会话里（8-22 17:52 那条插话就是这么丢的）。

    ## 判据：只纠正"断言在动"的那一类，且要有正面证据

    两个条件缺一不可 ——

        run 断言自己在动（ASSERTS_ACTIVE_WORK）
        ∧ 承载它的那次 attempt **已经结束**，而没有新的 attempt 接上

    第一条把 `waiting_human` / `waiting_permission` 排除在外：那两句话在没有
    worker 时**仍然成立**，回答它会把 worker 重新拉起来。把它们也改判成
    `stale_unknown`，等于把一条能续的会话说成故障。

    第二条是**正面证据**，不是推断：得有一次 attempt 存在，且它的租约已经过期。
    没有任何 attempt（刚 queued 还没派）不改判 —— 没有证据就不下结论。

    ⚠️ 这里曾经还有第三个合取 `attempt.ended_at is not None`（"承载它的那一趟
    已经结束"），2026-08-27 删掉。删的理由是它挡住的不是它想挡的东西：

    - 它**想**挡 8-21 那次事故（一条刚起 9 秒、注册表还没登记的 run 被判死）。
      但真正挡住那一幕的是**租约**：派发时就写下 `lease_until = now + 180s`
      （`local_execution._freeze_attempt_sandbox_manifest`），所以刚出生的
      attempt 一定是 `working`，下面那个 `activity() != "unknown"` 直接返回。
      两道防线里只有一道在干活。
    - 它**实际**挡住的是 worker 被 SIGKILL 的场景：attempt 从来没被关掉，
      `ended_at` 永远是 None，于是租约过期多久都不改判 —— 一条永远显示
      「运行中」的僵尸，正是 D11 要根治的那一类。

    "那一趟结束了" 与 "那一趟还在动" 是两个问题（[[feedback_two_things_one_rule]]）。
    前者要 worker 主动写一笔才成立，而 worker 被杀时恰恰写不了。活性只能由
    租约回答。

    `has_live_binding` 是最后一道保险：注册表里明确有它 → 一定不改判。这条保险
    由下面的 `activity()` 独家实施 —— 它遇到 live binding 直接返回 `working`。
    这里曾经另有一个提前返回做同一件事，2026-08-27 删掉：变异验证显示拿掉它
    一条测试都不红（行为完全由 `activity()` 决定），也就是说它不是第二道防线，
    是同一道防线的第二份抄件 —— 而抄件只会各自演化。行为判据仍然钉着
    （test_a_live_binding_always_wins），换的只是由谁来实施。

    返回值用 `stale_unknown` 而不是新造一个值：它的语义正是"运行时丢了、活没
    干完"，而且前后端**已有的**消费方全都认得它（前端
    `INTERRUPTED_PARENT_STATUSES` / `ATTENTION_STATES` / `UNFINISHED_RUN_STATUSES`
    恢复资格）。新造一个值等于让每个消费方再写一遍名单。
    """
    status = _status_value(run)
    if status not in {s.value for s in ASSERTS_ACTIVE_WORK}:
        return status
    # 正面证据：得有一次 attempt 在（没有 attempt = 还没派过，不下结论）。
    if attempt is None:
        return status
    if activity(run, attempt=attempt, now=now or datetime.now(UTC),
                has_live_binding=has_live_binding) != "unknown":
        return status
    return RunStatus.STALE_UNKNOWN.value


def witness(run: Run, *, reason: str, detected_at: str) -> dict:
    """把「我怀疑它没主了」记成**见证**，不改 status。

    见证是可以持久化的 —— 它陈述的是"平台在某时刻观察到注册表里没有它"，
    这件事永远为真。判决（"所以它死了"）才是会随规则演化作废的那部分，
    留给读时现算。
    """
    summary = dict(run.summary) if isinstance(run.summary, dict) else {}
    witnesses = list(summary.get("runtimeWitness") or [])
    witnesses.append({"reason": reason, "at": detected_at, "observedStatus": _status_value(run)})
    # 只留最近若干条：见证是线索不是账本，无界增长会把 summary 撑爆。
    summary["runtimeWitness"] = witnesses[-8:]
    return summary


# ── 采集器：把上面那个纯判据接到真实的库和注册表上 ──────────────────────────
#
# 上面全部是纯函数（输入全靠参数），为的是能拿真实历史回放喂进去 —— 回放没有
# 进程，也没有 db。所以采集放在这里、判据放在上面，但**同一个模块**：
# "这条 run 在不在跑"只能有一个家，抄一份就会各自演化（[[一个问题一个真相源]]）。


async def observed_status_map(db, runs, *, now: datetime | None = None) -> dict[str, str]:
    """一批 run 的对外呈现状态 `{run_id: status}` —— 批量取输入，逐条现算。

    只有真的可能被改判的 run 才去查 attempt（绝大多数 run 是终态，一条 SQL
    都不欠）。注册表按 `(project_id, session_id)` 问一次就够，同一会话的父子
    run 共用那一次答案。
    """
    from sqlalchemy import select as _select

    from app.models.execution import RunAttempt as _RunAttempt

    runs = list(runs)
    if not runs:
        return {}
    at = now or datetime.now(UTC)
    active = {s.value for s in ASSERTS_ACTIVE_WORK}
    candidates = [run for run in runs if _status_value(run) in active]
    if not candidates:
        return {run.id: _status_value(run) for run in runs}

    rows = (
        await db.execute(
            _select(_RunAttempt)
            .where(_RunAttempt.run_id.in_([run.id for run in candidates]))
            .order_by(_RunAttempt.run_id, _RunAttempt.attempt_no)
        )
    ).scalars().all()
    # 同一 run 的 attempt 按 attempt_no 升序到达，后来的覆盖前面的 → 留下最后一次。
    latest: dict[str, object] = {}
    for row in rows:
        latest[row.run_id] = row

    # 注册表是进程内缓存，问它不花钱，但每个 (project, session) 只问一次。
    from app.services.harness_sessions import harness_session_manager

    bindings: dict[tuple[str, str], object] = {}

    def _has_live_binding(run) -> bool:
        key = (run.project_id, run.session_id)
        if key not in bindings:
            bindings[key] = harness_session_manager.live_binding(*key)
        binding = bindings[key]
        if binding is None:
            return False
        # 子 run 的死活取决于**父 run** —— 注册表只登记顶层那条
        # （一个 harness 子进程 = 一条 binding）。拿子 run 的 id 去比对，
        # 每一条子 run 都会被判成没主的（2026-08-12 已经栽过一次）。
        return binding.run_id == (run.parent_run_id or run.id)

    # 父 run 已经收场的，子 run 就不可能还在动 —— 一条子 run 的活性不是它自己
    # 的事（2026-08-27）。
    #
    # 现场：一条会话的根 run 05:48 就 failed 了，五条子 run 的 status 永远停在
    # `queued`（摄取层**有意**不让子节点的生命周期事件驱动 Run.status，否则子
    # 节点的终态会关掉父命令的 attempt）。于是这五条各自算下来都是"在跑"，
    # 右栏五张卡片永远转圈 —— 而它们等的那个调度器早就没了。
    #
    # 子 run 从来没有自己的 attempt（派发的是父 run 的进程），所以上面那条
    # "没有 attempt 就不下结论" 永远救着它们。父的终态才是这里唯一的证据。
    parent_ids = {run.parent_run_id for run in runs if run.parent_run_id} - {r.id for r in runs}
    parents: dict[str, object] = {run.id: run for run in runs}
    if parent_ids:
        from app.models.execution import Run as _Run

        for row in (await db.execute(_select(_Run).where(_Run.id.in_(parent_ids)))).scalars():
            parents[row.id] = row

    def _parent_is_over(run) -> bool:
        parent = parents.get(run.parent_run_id or "")
        if parent is None:
            return False
        parent_status = _status_value(parent)
        return parent_status in {s.value for s in TERMINAL_RUN_STATUSES} or (
            parent_status == RunStatus.STALE_UNKNOWN.value
        )

    out: dict[str, str] = {}
    for run in runs:
        if _status_value(run) not in active:
            out[run.id] = _status_value(run)
            continue
        if run.parent_run_id and _parent_is_over(run):
            # 这里原本还 AND 了一个 `not _has_live_binding(run)`。那条防线是为
            # 「父还在跑，别误杀它的子」写的（2026-08-12 栽过一次），本身是对的
            # —— 但父**已经收场**的时候它就过期了：注册表里那条 binding 属于
            # 停靠着等**下一轮**的 worker，不属于这条已经结束的父 run。
            #
            # 两个条件在答同一个问题（「还有没有东西在驱动这条子 run」），
            # 而它们不一致时，过期的那个赢了。2026-09-16 现场（yuankk）：
            # 父 run 13:47 failed，worker 停靠着没走 → binding 还在 →
            # 纠偏被跳过 → 右栏「postprocess 进行中 · 40 actions」一直转，
            # 而聊天区同时写着「这一轮没能完成」。重启后 binding 没了、同一行
            # 立刻算成 stale_unknown —— 这个缺陷只在有停靠 worker 时显形。
            #
            # 子 run 跑在父 run 的进程里（它从来没有自己的 attempt）。父的终态
            # 就是这里唯一、也是充分的证据。
            out[run.id] = RunStatus.STALE_UNKNOWN.value
            continue
        out[run.id] = observed_status(
            run,
            attempt=latest.get(run.id),
            now=at,
            has_live_binding=_has_live_binding(run),
        )
    return out

"""「编排出去的工作闭环了没」的**唯一权威推导**。

## 为什么有这个模块

同一个判据长出了两份实现：

  - `chat.py` 的 continuous 终态门禁（`_continuous_unresolved_terminal_producers`
    / `_continuous_pending_post_node_flows`，以及 `_continuous_followup` 里那段
    `status in ("complete", "blocked")` 的驳回）—— 它拦的是**模型自报**的
    `CONTINUOUS_STATUS: complete`；
  - `core/executor.py` 的 `compute_orchestration_closure`（issue #221）—— 它拦的是
    **run status / summary.json** 自报的 `completed`。

两处问的是同一个问题、读的是同一批机械账本（`pending_post_node_flow` 账本 / 子 run
的 summary.json / TaskList），今天口径也刻意对齐：**条目还在 `pending_post_node_flow`
里就算没走完**、**每个 node_type 只看最近一次尝试**、**磁盘对账一律走
`core.run_history`**。但**没有任何机制保证它们继续对齐**。

而"没有单一权威推导"这件事的后果 `core/run_history.py` 的模块文档已经写过一遍：
9 处各自实现"扫兄弟 run 推导状态"，每处自带一套过滤语义、排序规则、扫描上限，
口径漂移，最后同一天四个 PR 在收拾后果 —— 其中一个 PR 的第一版还等于又加了第 9 个
不一致的实现。这里不等它重演第二遍：判据收敛到这一处，两条路径都从这里取事实。

新增一类"未闭环信号" = 在这里加一个 collector，两条路径同时生效；不是在两个文件
里各写一遍、再各自漂移一遍。

## 一个判据，两个消费口径 —— 差异是**显式参数**，不是各自的实现细节

差异真实存在，而且都有理由。所以它们是 `ClosureScope` 上的字段：摆在一起、写着
为什么，而不是散在两个文件里靠注释互相提醒。

1. **`producer_node_types`**：终态门禁**故意只看 `writing`**。后来的
   project_synthesis 决策可以合法地取代一个被放弃的 literature / data /
   hypothesis 尝试 —— 但它不能把一份机械判定无效的稿子变成"完成的终交付物"。
   run status 侧则覆盖**本 run 编排过的全部** producing 节点：它汇报的是"这一 run
   派出去的活儿到底完没完"，放过任何一个都是 #221 原样复发。
   （`None` = 本 run 编排过的全部；显式集合 = 只看这些，且**无论 transcript 里有
   没有它们的事件都要去磁盘上找**，这正是 E2E-3 那个"成功的 writing run 对父进程
   完全隐形"的修法。）

2. **`include_project_tasks`**：只有 run status 侧看。
   TaskList 是**项目级长账**；continuous 若拿它当终态门禁，一条没人关掉的陈旧
   task 就能让持续模式永远停不下来（自动续轮会一直烧算力）。run status 侧没有这个
   风险 —— 它只是**如实汇报一次**，不驱动下一轮。

3. **`include_blocking_obligations`**：两条路径都看（原来只有 chat.py 看，见下）。

判定层**不许有副作用**：这里只读账本，不建目录、不写状态、不问 LLM。
"LLM 说已完成"从来不是这里的输入。

## 为什么 run status 也要看 blocking obligations（本次并轨做的决定）

原来只有 chat.py 的终态门禁看 `core.obligations`。但 obligations 收敛的正是"某个
节点欠着某样东西，没补齐项目不算完"这一件事，而 #221 的教训是**"机制存在但没接到
路径"**：如果终态门禁因为一条未了结的申诉拒绝 complete，而同一时刻 summary.json
写着 `completed`，那平台汇总读到的仍然是"任务已完成" —— 换了个字段的同一个 bug。
E2E-4 实测的现场（data 节点申诉"我需要 approved plan"，没人跟进，orchestrator
自己改道绕开，那条诉求悬到最后没有下文）本来就该在 run status 上看得见。

"项目级信号会不会误杀纯对话轮"这个风险由已有的防误杀门挡住：降级仍然要求**本 run
真的编排过 producing 工作**，而义务**不算**那份证据（它可能来自上一个 run，见
`OpenWork.orchestrated_producing_work`）。

只取 `blocking=True` 的那些 —— 非 blocking 义务（例如没测完的 prereg metric）
按定义不阻塞收尾，不该降级任何东西；它们照旧由 `obligations.render` 摆给模型看。
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from core import run_history

log = logging.getLogger(__name__)

# 子 run 的"终态 / 暂停"两类事件。`subagent_call_paused` **不带** status（子 run
# 当时确实还没有终态），但"暂停着"同样不是闭环，所以它必须进判定 —— 磁盘对账会把
# resume 之后真跑完的那次纠正回 completed（见 _reconcile_with_disk）。
_TERMINAL_CHILD_EVENTS = frozenset({"subagent_call_end", "subagent_call_paused"})

# TaskList 里"还没收尾"的状态（core/tasks.py 的 4 态里除 completed 之外全算）。
_OPEN_TASK_STATUSES = frozenset({"pending", "in_progress", "blocked"})

# post-producing 3-step flow 卡在哪一步的定位口径 —— 与
# `core/loop_hooks_builtin.py::_post_node_review_flow_reminder_on_turn_start`
# 选取待处理 entry 的判据保持一致。⚠️ 这里只用来给人**定位**，不参与"这条 flow
# 算不算 open"的判定：那件事的权威口径是"条目还在 pending_post_node_flow 里就没
# 走完"（见 open_post_node_flows）。
_FLOW_OPEN_REVIEW_STATES = frozenset({
    "pending", "failed_awaiting_human", "retry_authorized",
})
_FLOW_OPEN_DECISION_STATES = frozenset({
    "pending", "awaiting_human", "action_authorized",
    "action_in_progress", "awaiting_manual_edit",
})


# ── 事实记录 ────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ProducerAttempt:
    """某个 producing node_type **最近一次**尝试的权威快照。"""

    node_type: str
    run_id: str = ""
    status: str | None = None
    missing_required_outputs: tuple[str, ...] = ()
    at: str = ""
    source: str = "transcript"
    """`transcript` | `disk_reconciliation` —— 这条事实是谁给的。"""
    event: str = ""

    @property
    def resolved(self) -> bool:
        """闭环只认 `completed`。

        `None`（终态事件没带 status）/ `paused` / `in_flight` / `incomplete` /
        `error` / `cancelled` 一律不算 —— **"查不出来"不是"跑成了"**。
        """
        return self.status == "completed"

    def as_dict(self) -> dict:
        """两个消费方共用的投影底座（各自再加自己的字段）。"""
        return {
            "node_type": self.node_type,
            "run_id": self.run_id,
            "status": self.status,
            "missing_required_outputs": list(self.missing_required_outputs),
            "at": self.at,
            "source": self.source,
        }


@dataclass(frozen=True)
class StartedProducer:
    """起过、但 transcript 与磁盘上都没有任何终态/暂停记录的 producing 节点。

    后台子 run（`background=true`）还没回报 / 崩了 / 父进程在它落盘前就结束了都会
    落到这里。这类"消失的子 run"最容易被读成"没这回事"。
    """

    node_type: str
    started_attempts: int = 0


@dataclass(frozen=True)
class OpenTask:
    """项目 TaskList 里还没收尾的一条 task。"""

    task_id: str
    status: str
    title: str = ""
    owner_node: str | None = None
    blocked_reason: str | None = None


# ── 口径参数 ────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ClosureScope:
    """两条消费路径的差异，全部集中在这里（每一项的理由见模块文档）。

    ⚠️ 这些开关决定**收哪几本账**，不决定消费方拿它做什么。把一个 `False` 翻成
    `True` 只是让对应字段有值 —— 消费方那边的门禁也要跟着接上，否则就是"收了但
    没人看"（正是本模块要消灭的那类"机制存在但没接到路径"）。
    """

    name: str
    producer_node_types: frozenset[str] | None = None
    """`None` = 本 run 编排过的全部 producing 节点；显式集合 = 只看这些。"""
    include_project_tasks: bool = False
    include_blocking_obligations: bool = True


CONTINUOUS_TERMINAL = ClosureScope(
    name="continuous_terminal",
    # 刻意只看 writing：后来的 project_synthesis 决策可以合法取代被放弃的上游尝试。
    producer_node_types=frozenset({"writing"}),
    include_project_tasks=False,
    include_blocking_obligations=True,
)
"""chat.py 的 continuous 终态门禁：驳回模型自报的 `CONTINUOUS_STATUS: complete`。"""

RUN_STATUS = ClosureScope(
    name="run_status",
    producer_node_types=None,
    include_project_tasks=True,
    include_blocking_obligations=True,
)
"""core/executor.py 的 run status 判据（issue #221）：驳回 summary.json 的
`completed`。"""


# ── 汇总结果 ────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class OpenWork:
    """"这个 state 现在还有哪些编排出去的工作没闭环"的完整答案。"""

    scope: ClosureScope
    unresolved_producers: tuple[ProducerAttempt, ...] = ()
    started_without_terminal_record: tuple[StartedProducer, ...] = ()
    orchestrated_producing_nodes: tuple[str, ...] = ()
    started_attempts: dict[str, int] = field(default_factory=dict, repr=False)
    open_flow_entries: tuple[dict, ...] = ()
    open_curator_entries: tuple[dict, ...] = ()
    open_tasks: tuple[OpenTask, ...] = ()
    obligations: tuple[Any, ...] = ()
    """本项目全部未了结义务（含非 blocking）—— 给 `obligations.render` 复用，
    省掉同一轮里第二次全盘扫描。"""
    blocking_obligations: tuple[Any, ...] = ()

    @property
    def orchestrated_producing_work(self) -> bool:
        """本 run 真的编排过 producing 工作吗（防误杀的前置门）。

        证据只有两样：**本 run transcript 里有 producing 子 run 事件**，或
        **hook_state 里有 post-producing flow 账本条目**（后者只可能由本 run 里某个
        producing 子 run 成功后登记）。

        项目级信号（TaskList / obligations）**刻意不算**证据 —— 它们可能来自上一个
        run，拿它们当"编排过"会让每一轮纯对话都被降级，那只是把一个误报换成另一个。
        磁盘对账捞回来的孤儿 run 也不算：那是"补上事实"，不是"本 run 派过它"。
        """
        return bool(self.orchestrated_producing_nodes) or bool(self.open_flow_entries)

    @property
    def any_open(self) -> bool:
        return bool(
            self.unresolved_producers
            or self.started_without_terminal_record
            or self.open_flow_entries
            or self.open_curator_entries
            or self.open_tasks
            or self.blocking_obligations
        )


# ── 来源 1：producing 子 run 的最近一次尝试 ──────────────────────────────────

def _scan_child_run_events(
    state: Any, node_types: frozenset[str] | None,
) -> tuple[dict[str, ProducerAttempt], dict[str, int], set[str]]:
    """扫本 run 的 transcript：派过哪些 producing 子节点、每个最近一次是谁。

    返回 `(latest_by_node_type, started_count_by_node_type, seen_child_run_ids)`。

    只看 producing 子节点（`node_type` 不以 `_` 开头，口径同
    `core/run_history.py::RunRecord.is_producing`）。系统子节点（_reviewer /
    _curator）的闭环**故意不看它们自己的 run status**：一份精准抓到问题的
    review_critique 曾因 enum 漂移把 reviewer run 判成 incomplete，"检查机制反噬
    检查结果"这个坑框架已经按 artifact 角色修过（见 run_node.py
    `_import_required_outputs` 的注释）。它们的权威账本是 `pending_post_node_flow`
    的 review_state / curator_state —— 已由 `open_post_node_flows` 覆盖。

    **每个 node_type 只留最近一次**：literature 第一次 incomplete、修订后第二次
    completed，是正常的自我纠正，不是未闭环。transcript 是 append-only 的，它记下
    的那些 run 先后顺序它自己最清楚，所以"后面的事件覆盖前面的"就是时间序。
    """
    latest: dict[str, ProducerAttempt] = {}
    started: dict[str, int] = {}
    seen_run_ids: set[str] = set()

    path = getattr(state, "transcript_path", None)
    if path is None or not Path(path).exists():
        return latest, started, seen_run_ids
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return latest, started, seen_run_ids

    for line in lines:
        try:
            event = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(event, dict):
            continue
        node_type = str(event.get("child_node_type") or "")
        if not node_type or node_type.startswith("_"):
            continue
        if node_types is not None and node_type not in node_types:
            continue
        kind = str(event.get("event") or "")
        if kind == "subagent_call_start":
            started[node_type] = started.get(node_type, 0) + 1
            continue
        if kind not in _TERMINAL_CHILD_EVENTS:
            continue
        run_id = str(event.get("child_run_id") or "")
        if run_id:
            seen_run_ids.add(run_id)
        latest[node_type] = ProducerAttempt(
            node_type=node_type,
            run_id=run_id,
            # 终态事件没带 status 时**不当成通过**：留 None，由 resolved 判为未闭环。
            status=event.get("child_status") or (
                "paused" if kind == "subagent_call_paused" else None),
            at=str(event.get("at") or ""),
            source="transcript",
            event=kind,
        )
    return latest, started, seen_run_ids


def _attempt_from_record(rec: Any, *, at: str = "") -> ProducerAttempt:
    # in_flight（有 transcript 无 summary）= 还在跑，或者跑一半进程没了 —— 两者在
    # 磁盘上无法区分，都不是 completed，如实标 in_flight。
    return ProducerAttempt(
        node_type=str(rec.node_type or ""),
        run_id=rec.run_id,
        status="in_flight" if rec.in_flight else rec.status,
        missing_required_outputs=tuple(rec.missing_required_outputs),
        at=at,
        source="disk_reconciliation",
    )


def _is_newer_run(candidate: Any, baseline_rec: Any) -> bool:
    """candidate（RunRecord）是否比 baseline（RunRecord）新。

    **时间序不自己造** —— 用 `RunRecord.order_key`，也就是这个模块开头写的那条
    "权威推导只有一处：core.run_history"。它是 `(run_id 时间戳前缀, mtime,
    run_id)` 三级：前缀精确且与文件系统无关，解析不出来（测试里的自定义目录名
    如 `w-ok`）才退到 mtime。

    第一版我自己拿 run_id 前缀比，解析不出就保守返回 False —— 结果把 E2E-3 那个
    孤儿场景又弄坏了（既有测试 `test_all_consumers_agree_on_latest_run` 当场变红，
    它锁的正是"所有消费方对最近一次 run 必须给同一个答案"）。自造第二套时间序，
    就是在制造它要防的那种漂移。

    baseline 在磁盘上找不到（transcript 提到但没有 summary.json）→ 无从比较，
    返回 True 让磁盘那份胜出：磁盘是唯一事实来源。
    """
    if baseline_rec is None:
        return True
    return candidate.order_key > baseline_rec.order_key


def _reconcile_with_disk(
    state: Any,
    latest: dict[str, ProducerAttempt],
    *,
    seen_run_ids: set[str],
    probe_node_types: frozenset[str],
) -> None:
    """用磁盘上的 summary.json 校正 transcript 事件（原地改 `latest`）。

    transcript 不是"最近一次 run"的可靠来源，两个实测场景都会让只信事件的判据误判：

      - **孤儿 run**（E2E-3 实测）：子 run 跑完但**父进程在它写下
        `subagent_call_end` 之前就没了**（进程重启 / 崩溃），成功的那次对父完全
        隐形。现场：writing run 1785222389-e08990 跑满 46 轮、12 项 QC 全绿、PDF
        编译完成，父这边记录的仍是它前面那个失败的 0acefd —— 终态门禁于是永远
        驳回 complete，项目关不掉。
      - **pause → resume → completed**：cascade resume 只替换父的 tool_result，
        **不会**在父 transcript 里补一条 `subagent_call_end`，于是一个已经跑完的
        子 run 会被永远当成"卡在 paused"。

    对账规则刻意**不做跨来源的时间比较** —— 那正是接缝所在。transcript 是
    append-only 的：它记下的那些 run，先后顺序它自己最清楚；磁盘补的是**它从没见过
    的** run，那只可能是"父停止记录之后才落盘的"，也就是孤儿。所以规则是
    **"transcript 没见过的 run 取代它记的那次"**，不是"谁的时间戳大听谁的"。后者
    要求两个来源的时间可比，而文件系统时间戳粒度一粗就不可比（第一版按 mtime 比：
    macOS 全过、Linux 容器全挂）。

    权威推导只有一处：`core.run_history`（磁盘是唯一事实来源）。
    """
    root = getattr(state, "root", None)
    if root is None:
        return
    try:
        # limit=0（不截断）：load_runs 的 limit 限的是**返回条数**、不是扫描条数
        # （见其 docstring），所以截断只会让长 session 早期起的子 run 找不到、
        # 白白退回事件快照，省不下任何 I/O。
        # include_in_flight=True：正在跑 / 跑一半没了的子 run 也是"未闭环"的事实，
        # 派发与门禁类判定看不见它才是瞎。
        records = run_history.load_runs(
            Path(root).parent,
            project_id=getattr(state, "project_id", None),
            exclude_run_id=getattr(state, "run_id", None),
            include_in_flight=True,
            limit=0,
        )
    except Exception:      # noqa: BLE001 —— 对账失败不能让判定层挂
        log.warning("closure: run_history 对账失败", exc_info=True)
        return

    # ① 孤儿 run：transcript 没见过的那次取代它记的那次 —— **但只有更新的才配取代**。
    #
    # 原来是无条件取代，前提假设是"没被 transcript 见过 ⇒ 父停止记录之后才落盘 ⇒
    # 它是最新的"。这个假设对**崩溃的子 run** 不成立：子 run 抛异常死掉时父同样
    # 来不及写 `subagent_call_end`，于是它也"没被见过"。
    #
    # E2E-6 实测（2026-08-04）：writing 第一次 ReadTimeout 崩了（1785801560），
    # 第二次跑成了（1785806808，transcript 有记录）。崩溃那次因"没被见过"被当成
    # 孤儿，**反过来顶掉了后面那次成功的**，于是 latest[writing]=error → 永远
    # 不闭环。手稿早已冻结入库、全流程 PROCEED，项目却关不掉，orchestrator 只能
    # 报 blocked（它拒绝改 summary.json 造假，做得完全对）。
    #
    # 比较用 `RunRecord.order_key`（core.run_history 那套唯一权威时间序），
    # 不自造第二套 —— 详见 `_is_newer_run`。
    by_id = {r.run_id: r for r in records}
    for node_type in sorted(probe_node_types):
        unseen = [r for r in records
                  if r.node_type == node_type and r.run_id not in seen_run_ids]
        if not unseen:
            continue
        cand = unseen[0]                       # load_runs 已是新→旧
        known = latest.get(node_type)
        if known is not None and not _is_newer_run(cand, by_id.get(known.run_id)):
            continue                           # 旧的崩溃尝试不许顶掉新的成功
        latest[node_type] = _attempt_from_record(cand)

    # ② transcript 记下的那次也要用磁盘真值覆盖 —— 事件是**当时的快照**，
    #    summary.json 才是最终状态（同一个道理，同一个来源）。
    for node_type, attempt in list(latest.items()):
        rec = by_id.get(attempt.run_id)
        if rec is None:
            continue
        latest[node_type] = replace(
            attempt,
            status="in_flight" if rec.in_flight else rec.status,
            missing_required_outputs=(tuple(rec.missing_required_outputs)
                                      or attempt.missing_required_outputs),
        )


# ── 来源 2：post-producing flow 账本 ────────────────────────────────────────

def flow_open_step(entry: dict) -> str:
    """这条 flow 卡在 3 步（reviewer → curator → decision）的哪一步。"""
    if str(entry.get("review_state") or "") in _FLOW_OPEN_REVIEW_STATES:
        return "review"
    if False:  # curator 已退出 flow
        return "curator"
    if str(entry.get("decision_state") or "") in _FLOW_OPEN_DECISION_STATES:
        return "decision"
    # 三个字段都不在"开放态"里，条目却还挂在队列上 —— 出列只发生在 decision 被
    # 机械记账那一刻（decision_package.py 的 `flow_closed=True`）。所以这仍然是
    # 未闭环，只是卡点不在已知的三步枚举里。如实标出来，不假装它已经完了。
    return "queued_unknown_step"


def open_post_node_flows(state: Any) -> tuple[dict, ...]:
    """未走完的 post-producing flow 条目（**原始 entry**，投影交给各消费方）。

    权威口径：**条目还在 `pending_post_node_flow` 里就算没走完**。一条正式决策包
    只在被答复并**机械记账**之后才出列，所以它单纯的存在就比任何 LLM 写下的完成
    句子更强。`decision_state='awaiting_human'` 必须能扛过进程重启：内存里的 pause
    registry 重启后就没了，持久化的 flow 仍然需要被重新摆出来并解决。

    这里**不另发明**一套"哪些 state 才算 done" —— 两处口径各自演化正是 review 门
    完整性那一串 PR 反复在修的接缝（`flow_open_step` 只做定位，不参与判定）。
    """
    return tuple(
        entry for entry in (state.hook_state.get("pending_post_node_flow") or [])
        if isinstance(entry, dict)
    )


# ── 一条 flow 空转了没（2026-09-17）──────────────────────────────────────────
#
# 原来的空转熔断（run_node 的 `_MAX_ACTION_ATTEMPTS`）数的是**成功绑定过几次**，
# 而绑定本身挂在 `node_owes_post_node_flow(target)` 上。目标满足不了它的时候，
# 绑定被跳过 ⇒ 计数器一次都不加 ⇒ **熔断器恰好在最需要它的那一档结构性缺席**。
# yuankk 那条会话就这么转了 40 轮，账本上干干净净、一次 attempt 都没有。
#
# 所以计数必须搬到**跳不过去的那一侧**：每轮 hook 把这条 entry 摆到调度器面前一
# 次，就问一次"上次摆完到现在，它动过没有"。动过 = 归零；没动过 = 累加。这样不论
# 将来冒出哪种新的关不掉形态，它都在 N 轮内响，而不是 40 轮后静默停摆。
#
# 落盘的是**证据**（轮数 + 上次的状态指纹），不是判决 —— "算不算卡住"现算。

#: 同一条 flow 被摆到调度器面前这么多轮而状态一动不动，就是空转不是进展。
#: 与 run_node 的 `_MAX_ACTION_ATTEMPTS` 同一个数量级：它们回答的是同一个问题
#: （"还要不要再试一次"），只是从两侧观测。
MAX_FLOW_STALL_ROUNDS = 5

STALL_ROUNDS_KEY = "flow_stall_rounds"
STALL_SIGNATURE_KEY = "flow_stall_signature"


def flow_progress_signature(entry: dict) -> str:
    """这条 entry 的"有没有往前走"指纹。变了 = 有进展；没变 = 原地踏步。

    取的是**推进这条 flow 会改变**的那些字段：两个 state、授权目标、已起过几次、
    上次失败原因。刻意不含时间戳/轮次之类必然变化的东西 —— 那会让指纹永远"在变"，
    熔断器再次形同虚设。
    """
    return "|".join(str(entry.get(k) or "") for k in (
        "review_state",
        "decision_state",
        "authorized_action",
        "authorized_target_node",
        "action_attempt_count",
        "action_last_failure",
    ))


def note_flow_was_put_to_the_orchestrator(entry: dict) -> int:
    """记一次"这条 entry 又摆到调度器面前了"，返回累计空转轮数。

    调用方只有一个：每轮把 flow 提醒注入上下文的那个 hook。它是**跳不过去的**
    那一侧 —— 不管目标节点是什么类型、绑定成不成功，注入都照常发生。
    """
    signature = flow_progress_signature(entry)
    if entry.get(STALL_SIGNATURE_KEY) != signature:
        entry[STALL_SIGNATURE_KEY] = signature
        entry[STALL_ROUNDS_KEY] = 1
        return 1
    rounds = int(entry.get(STALL_ROUNDS_KEY) or 0) + 1
    entry[STALL_ROUNDS_KEY] = rounds
    return rounds


def flow_is_stalled(entry: dict) -> bool:
    """现算：这条 flow 已经空转到"再催也没用"的地步了。

    这同时是"`blocked` 还能不能被驳回"的判据。持续模式驳回 `blocked` 的理由是
    "你还有自己推得动的东西"；一条已经证明推不动的 flow 让这个理由不成立 ——
    继续驳回就成了"你被它卡住"和"你不许说你被它卡住"同时为真。
    """
    return int(entry.get(STALL_ROUNDS_KEY) or 0) > MAX_FLOW_STALL_ROUNDS


def stalled_post_node_flows(state: Any) -> tuple[dict, ...]:
    return tuple(e for e in open_post_node_flows(state) if flow_is_stalled(e))


def _open_project_tasks(state: Any) -> tuple[OpenTask, ...]:
    """项目 TaskList 里还没收尾的 task（pending / in_progress / blocked）。"""
    root = getattr(state, "project_root", None)
    if root is None:
        return ()
    jsonl = Path(root) / "tasks" / "tasks.jsonl"
    # 故意不直接 new TaskList()：它的 __init__ 会 mkdir + 之后 rewrite 视图。
    # 判定层不该有副作用；没建过 task 系统就是"没有未完成 task"。
    if not jsonl.exists():
        return ()
    try:
        from .tasks import TaskList
        tasks = TaskList(jsonl.parent).list_all()
    except Exception:      # noqa: BLE001 —— 账本读不动不能让判定层挂
        log.warning("closure: TaskList 读取失败", exc_info=True)
        return ()
    return tuple(
        OpenTask(task_id=t.id, status=t.status, title=(t.title or "")[:160],
                 owner_node=t.owner_node, blocked_reason=t.blocked_reason)
        for t in tasks if t.status in _OPEN_TASK_STATUSES
    )


# ── 来源 4：未了结义务 ──────────────────────────────────────────────────────

def _collect_obligations(state: Any) -> tuple[tuple[Any, ...], tuple[Any, ...]]:
    """`(全部, 其中 blocking 的)`。来源见 core/obligations.py。"""
    try:
        from core import obligations as _obl
        items = tuple(_obl.collect(state))
        return items, tuple(_obl.blocking(list(items)))
    except Exception:      # noqa: BLE001 —— 义务账读不动不能让判定层挂
        log.warning("closure: obligations 收集失败", exc_info=True)
        return (), ()


# ── 权威入口 ────────────────────────────────────────────────────────────────

def open_work(state: Any, scope: ClosureScope) -> OpenWork:
    """这个 state 现在还有哪些编排出去的工作没闭环。

    纯读：不建目录、不写状态、不问 LLM。`scope` 决定看哪些来源（见模块文档）。
    """
    latest, started, seen_run_ids = _scan_child_run_events(
        state, scope.producer_node_types)

    # 防误杀的证据必须在磁盘对账**之前**定格：孤儿 run 是"补上事实"，不是"本 run
    # 派过它"。对账之后再取 set(latest) 会把项目里别的 run 派的活儿算到本 run 头上。
    orchestrated = tuple(sorted(set(latest) | set(started)))

    # 显式 producer 集合要**无论 transcript 有没有事件都去磁盘上找**（E2E-3 那个
    # 隐形的 writing run）；`None` 口径下只探本 run 确实碰过的那些节点。
    probe = (scope.producer_node_types if scope.producer_node_types is not None
             else frozenset(orchestrated))
    _reconcile_with_disk(state, latest, seen_run_ids=seen_run_ids,
                         probe_node_types=probe)

    unresolved = tuple(sorted(
        (a for a in latest.values() if not a.resolved),
        key=lambda a: (a.at, a.node_type)))
    no_record = tuple(
        StartedProducer(node_type=nt, started_attempts=n)
        for nt, n in sorted(started.items()) if nt not in latest)

    flows = open_post_node_flows(state)
    # curator 已退出 post-producing flow（wangd 2026-08-19）——它是按需调取的
    # 后台节点，没有"未整合"这种未完成工作。legacy 镜像 pending_curator_integrations
    # 连同这里的口径一并删除。
    curator = ()
    tasks = _open_project_tasks(state) if scope.include_project_tasks else ()
    all_obl, blocking_obl = (
        _collect_obligations(state) if scope.include_blocking_obligations
        else ((), ()))

    return OpenWork(
        scope=scope,
        unresolved_producers=unresolved,
        started_without_terminal_record=no_record,
        orchestrated_producing_nodes=orchestrated,
        started_attempts=dict(started),
        open_flow_entries=flows,
        open_curator_entries=curator,
        open_tasks=tasks,
        obligations=all_obl,
        blocking_obligations=blocking_obl,
    )

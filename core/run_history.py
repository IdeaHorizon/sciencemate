"""run 历史的**唯一权威推导**。

## 为什么有这个模块

2026-07-28 E2E-3 那天 merge 的七个 PR 里，四个在修同一类缺陷 —— 两处代码各自
推导"某个 run 现在是什么状态"，口径不同，于是打架：

  - 修订基线的 note 说"读 run A"，`read_own_prior_attempt` 默认却读 run B
    （note 按"走得最远"挑，工具按"最近"挑）→ 节点照着 note 里的 id 去读，报
    "这个 run 里没有该 artifact"；
  - 终态门禁信 transcript 事件，真相在磁盘 —— 父进程在子 run 写下
    subagent_call_end 之前重启，那次**成功的 writing run 对父完全隐形**，
    项目永远关不掉；
  - 派发拦截只看本节点自己的历史，看不到"上游已经补齐了"，于是拦死不放。

排查时数出来：`chat.py` / `core/recall.py` / `shared/tools/run_node.py` 里共
**9 处**各自实现的"扫兄弟 run 推导状态"，每处自带一套过滤语义、排序规则、扫描
上限。修其中一处不解决问题 —— 事实上修门禁那次（PR#201）等于又加了第 9 个不
一致的实现（它按 mtime 排，其余 8 处按目录名排）。

**根因不是任何一处的逻辑写错了，是没有单一权威推导。** 这个模块就是那个权威：
磁盘是唯一事实来源，一套过滤语义、一套时间序、一套扫描策略。所有消费者走它。

## 统一时做的取舍（都跟至少一处旧实现不同）

1. **时间序：run_id 的时间戳前缀优先，mtime 兜底**（见 RunRecord.order_key）。
   旧实现 8 处按目录**名**字典序倒序 —— PR#201 第一版按名字比，把既有测试的
   `writing-failed` / `writing-completed` 判反了（'f' > 'c'）。
   而纯按 mtime 又把正确性挂在文件系统时间戳粒度上：macOS 全过、Linux 容器全挂。
   前缀是精确创建时刻且与文件系统无关，它才该是主键。

2. **project 过滤严格相等，`None` 只匹配 `None`。**
   `core/recall.py` 旧口径是 `if self_proj is not None and ...` —— self_proj 为
   None（anon state，例如 `run_node.py --harness X` 不带 `--project`）时**根本
   不过滤**，会读到所有项目的失败记录。这跟 PR#99 那个 89% 跨课题污染是同一类。

3. **一律排除自己那个 run。** 旧实现有的排有的不排。

4. **在飞 run 是一等公民**（`in_flight`）。只有进度指纹那处认它；派发/门禁类
   判定看不见"子节点正跑到第 22 轮"，把真进展当卡死。
"""
from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

_INFRA_FAILURE_KINDS = frozenset({
    "judge_invalid_json",
    "judge_provider_error",
    # judge 把整个输出预算烧在思维链上、正文一个字没吐（实测 completion=2000 /
    # reasoning=2000 / content=""）。预算翻倍重试用尽仍如此 → 平台的账。
    "judge_output_truncated",
})
"""判定层自身故障的 failure_kind —— 是平台的账，不是节点的。

`judge_cited_absent_evidence` **不在**此列：那是核实后带着完整信息重判过的，
重判仍 fail 是一次真实判断。
"""


_INCOMPLETE_GROUNDS = frozenset({
    "quality_or_missing",   # 缺必需产出 —— reevaluated_success 用
                            # was_evaluated + failure_signals 完整复核。
                            # ⚠️ 2026-08-10 起 **QC 不再是降级来源**（见
                            # executor.finalize_run 的说明）：检测照跑、结果照样
                            # 进 summary，但不判 run 死活。这个 ground 现在只剩
                            # "缺必需产出"一条，而 workspace 模式下它恒空。
    "orchestration_closure",  # 编排的工作没闭环 —— 与 QC 无关，复核维持原判
    "delivery_empty",       # v2.1 交付地板：工作区作用域零改动 —— 复核维持原判
                            # （没交东西是完整可解释的事实，不因规则演化而翻案）
})
"""`final_status` 被降级为 incomplete 的**全部**来源，逐条登记。

这是本次修复的防漂移锚点：判决改成"从证据现算"之后，复核只推翻自己能完整
解释的依据。若将来有人加了第三个降级来源却不登记，复核就会把那道新门当作
"看不懂就当没有"静默放行 —— 正是本次要根除的那类缺陷的镜像。
守卫测试机械地数 executor 里的赋值点，加了不登记直接红。
"""


EXTERNAL_FAILURE_CATEGORIES = frozenset({
    "provider_tool_call_protocol_error",  # 后端协议抽风（executor 分类）
    "framework_gate_blocked",             # 框架门禁死锁/循环（#426，executor 分类）
    "provider_unavailable",               # provider 断流/超时打穿重试预算（#480）
})
"""失败成因在**节点之外**的 failure_category —— 不进节点的卡死统计。

这是"这次失败算不算节点的账"这个问题的唯一真相源。消费方：
`consecutive_failures`（默认剔除）、`run_node._repeated_failure_for`。
注意与"该不该机械重派"是**两个问题**：协议抽风是瞬态、值得重试；门禁拒绝
是确定性的、原样重跑必然再撞（run_node._is_retryable_infra_failure 只含
前者）。别把两个集合合成一个。
"""


#: load_runs 默认扫描窗口。
DEFAULT_SCAN_LIMIT = 120


@dataclass(frozen=True)
class RunRecord:
    """一次 run 的权威快照。字段名与 summary.json 对齐，不另造术语。"""

    run_id: str
    state_dir: Path
    node_type: str | None = None
    project_id: str | None = None
    status: str | None = None
    missing_required_outputs: tuple[str, ...] = ()
    artifacts: tuple[dict, ...] = ()
    upstream_rework_requests: tuple[dict, ...] = ()
    failure_category: str | None = None
    failure_subcategory: str | None = None
    turns: int = 0
    tool_call_count: int = 0
    tokens_used: int = 0
    finished_at: float = 0.0
    """summary.json 的 mtime。**不是**主排序键 —— 见 order_key。"""
    in_flight: bool = False
    """有 transcript / messages_checkpoint 但没有 summary.json —— 正在跑，或者
    跑到一半进程没了。两者在磁盘上无法区分，由调用方按新鲜度自行判断。"""
    has_pause: bool = False
    raw: dict = field(default_factory=dict, repr=False)

    # ── 语义谓词：别让每个消费者各自拼字符串 ────────────────────────────────
    @property
    def is_completed(self) -> bool:
        """这个 run 成没成 —— **判决按当前规则现算，不是读文件里那个章**。

        范畴错误（E2E-5b 2026-08-03 实测）：`status` 是 finalize 那一刻用**当时
        的规则**盖的章，写进 summary.json 后永不更新；`failure_signals` 是每次
        读都按**当前规则**重算的。两者都自称回答"这个 run 成没成"，于是每改进
        一条判定规则，全部历史记录的 status 就悄悄作废一批 —— 而没人知道。

        现场：writing run 1785726557-3743e6 手稿 36KB 存好、PDF 编好、12 项 QC
        过 11 项，唯一挂的那项是 judge 自己把输出预算烧在思维链上（平台故障）。
        当时的代码记 `status=incomplete`。后来 #259/#273 把规则改对了，
        `failure_signals` 变空 —— 但那个章还在，于是熔断器翻历史账本时仍把它
        算作失败，`/continuous on` 一发就被同一段旧账重新掐死，无解循环。

        更要命的是 `consecutive_failures` **在同一个循环里混用两套认识论**：
        断链读 `is_completed`（冻结的判决），计数读 `failure_signals`（活的事实）。

        修法不是再去通知一个消费者（那是打补丁，今天已经打了三次），而是：
        **证据可以持久化，判决不可以。**
          - `status` 留在 summary.json 当历史报告（审计要知道"当年判的是什么"）
          - 决策一律走这里，从证据现算 → 规则每次改进，全部历史自动重判
        """
        if self.status == "completed":
            return True
        return self.reevaluated_success

    @property
    def reevaluated_success(self) -> bool:
        """`incomplete` 的旧判决按当前规则复核。

        **不对称**，对应两个方向截然不同的风险：
          - `completed` 一律维持 —— 推翻旧的成功判决会静默放行本该拦的 run；
          - `incomplete` 才复核 —— 维持冤案只是保守，平反它只会纠错。

        且**只推翻自己能完整解释的失败依据**。`final_status` 有两个降级来源
        （见 `_INCOMPLETE_GROUNDS` 的守卫测试）：QC/缺产出，以及编排闭环门禁。
        后者与 QC 无关、RunRecord 也没有它的判据，所以照旧维持 —— 复核绝不
        变成"看不懂的降级就当没有"。
        """
        if not self.was_evaluated:
            return False          # 没跑到评估 → 没有可复核的证据（撞 max_turns / 崩了）
        if self.failure_signals:
            return False          # 按当前规则仍有可归因于本节点的失败
        if self.closure_downgraded:
            return False          # 编排闭环没闭 —— 另一道门，不在复核范围
        if self.raw.get("delivery_empty"):
            return False          # 什么都没交 —— 事实完整可解释，维持原判
        return True

    @property
    def closure_downgraded(self) -> bool:
        """本 run 是否因「编排的工作没闭环」被降级（core/executor.py 的第二个降级源）。"""
        c = self.raw.get("orchestration_closure")
        return bool(isinstance(c, dict) and c.get("downgrades_status"))

    @property
    def externally_caused(self) -> bool:
        """这次失败的成因在节点之外 —— 不该进节点的卡死统计。

        两个判据，先证据后判决：
          - `gate_block_evidence`（#426 起持久化的机械证据：run 终结在框架
            门禁上 / 同类门禁拒绝达阈值）—— 证据在，成因就在框架，**与当年
            分类器怎么判无关**（规则改进后历史 run 凭证据自动重判）；
          - `failure_category ∈ EXTERNAL_FAILURE_CATEGORIES`（executor 写入
            的分类）。
        两者都没有（老 summary）→ False：不确定不当成外因。
        """
        if self.failure_category in EXTERNAL_FAILURE_CATEGORIES:
            return True
        return bool(self.raw.get("gate_block_evidence"))

    @property
    def blockers(self) -> tuple[dict, ...]:
        """本 run 通过 `report_blocker` 如实登记的结构化阻塞。

        它一直在 summary.json 里，只是从来没有消费方（#524）。注意：报了
        blocker 的 run 往往**同时是成功的**（必需产出齐、QC 全过），所以
        `consecutive_failures` 对它完全不在场 —— 这条线要单独有闸。
        """
        raw = self.raw.get("blockers")
        return tuple(b for b in (raw or []) if isinstance(b, dict))

    @property
    def blocked_situation(self) -> dict | None:
        """卡住的那一刻，上游局面长什么样（core/dispatch_gate.capture）。

        老 run 没有这个字段 —— 消费方据此**放行**，不把"证据不在场"当成
        "局面没变"。
        """
        from core.dispatch_gate import BLOCKED_SITUATION_KEY

        value = self.raw.get(BLOCKED_SITUATION_KEY)
        return value if isinstance(value, dict) else None

    @property
    def is_system_node(self) -> bool:
        return str(self.node_type or "").startswith("_")

    @property
    def is_producing(self) -> bool:
        """真正的产出节点 —— 服务节点不算。

        判据取自节点自己的 `post_run_flow` 声明（v2.1 起 literature / data /
        postprocess 是服务），而不是"名字不以下划线开头"。

        为什么要紧：`break_on_other_producing_success` 的语义是"上游状况已经
        变了，旧失败不该再锁着本节点"。**服务**跑成功不代表任何状况改变 ——
        它是被随手调用的。P5 实测：postprocess 连续 6 次栽在同两个 QC 上，中间
        夹了一次 data 服务成功，连续失败链就被判断开，熔断彻底哑火，
        orchestrator 一路重派烧掉 3M token。

        harness 读不到（未知节点 / 测试构造）→ 回落到旧口径，不把不确定当成
        "是服务"而放行。
        """
        if not self.node_type or self.is_system_node:
            return False
        try:
            from core.loader import load_harness

            return not load_harness(str(self.node_type)).is_service
        except Exception:
            return True

    @property
    def was_evaluated(self) -> bool:
        """真跑到过收尾评估。

        QC 层删除（2026-08-22）后判据 = finalize 写下了真正的终态判定。
        `error` 是 catch-all 收尸（异常冒泡时补写的 summary），没经过评估；
        `blocked` 是**中途停靠**（report_blocker），也没有走到评估 —— 把它
        平反成 completed 会切断熔断器的失败链（停靠的 run 该被跳过，不该
        算成功）。没有 summary 的 run 根本不会成为 RunRecord。原判据是
        "有 QC 结果"，防的是撞 max_turns 的 run 以"0 条失败"混入（E2E-3）
        —— 那类 run 缺必需产出，仍被 failure_signals 拦住，防线不丢。
        """
        return self.status in ("completed", "incomplete")

    @property
    def failure_signals(self) -> set[str]:
        """失败签名：缺失的必需产出（契约层事实）。

        QC 层删除（2026-08-22）后这里只剩机械契约信号。曾经混进来的 judge
        判定有两次实测事故：judge 自身故障被算成节点连败锁死派发（E2E-5b），
        judge 假阳性让熔断永远数不到真循环（2026-08-22 英国饮食五轮返工）。
        判官的意见现在只有一个出口：reviewer 的 critique，带证据、可申诉。
        """
        return {f"missing:{m}" for m in self.missing_required_outputs}

    @property
    def order_key(self) -> tuple[float, float, str]:
        """时间序：`(run_id 时间戳前缀, mtime, run_id)`。

        run_id 形如 `<unix_ts>-<hex>`，前缀是**精确的创建时刻**，与文件系统无关。
        第一版拿 mtime 当主键，正确性就挂在了文件系统时间戳粒度上 —— macOS 上
        5/5 全过、Linux 容器里 2/2 全挂：同一毫秒创建的文件 mtime 相同，退化到
        按名字比，顺序直接反了（复现方式：把 _mtime 量化到秒）。
        前缀解析不出来（测试里的自定义目录名）才退到 mtime。
        """
        prefix = self.run_id.split("-", 1)[0]
        ts = float(prefix) if prefix.isdigit() else 0.0
        return (ts, self.finished_at, self.run_id)

    def artifact_types(self) -> set[str]:
        return {a.get("type") for a in self.artifacts
                if isinstance(a, dict) and a.get("id")}


def _read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    return data if isinstance(data, dict) else None


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _to_record(d: Path) -> RunRecord | None:
    """一个 run 目录 → RunRecord。既不是完成的也不是在飞的目录返回 None。"""
    summary_path = d / "summary.json"
    s = _read_json(summary_path) if summary_path.exists() else None
    has_pause = (d / "pause_pending.json").exists()

    if s is None:
        # 没有 summary：可能正在跑，也可能跑一半进程没了。
        transcript = d / "transcript.jsonl"
        checkpoint = d / "messages_checkpoint.json"
        if not (transcript.exists() or checkpoint.exists() or has_pause):
            return None                      # 不是一个 run 目录
        node_type = project_id = None
        # transcript 首行的 run_start 带 node_type —— 在飞 run 唯一的身份来源
        if transcript.exists():
            try:
                with transcript.open(encoding="utf-8") as fh:
                    first = fh.readline()
                head = json.loads(first) if first.strip() else {}
                if isinstance(head, dict):
                    node_type = head.get("node_type")
                    project_id = head.get("project_id")
            except (OSError, ValueError, TypeError):
                pass
        return RunRecord(
            run_id=d.name, state_dir=d, node_type=node_type,
            project_id=project_id, status=None,
            finished_at=max(_mtime(transcript), _mtime(checkpoint)),
            in_flight=True, has_pause=has_pause,
        )

    def _tup(key: str) -> tuple:
        v = s.get(key) or []
        return tuple(v) if isinstance(v, list | tuple) else ()

    def _int(key: str) -> int:
        try:
            return int(s.get(key) or 0)
        except (TypeError, ValueError):
            return 0

    return RunRecord(
        run_id=d.name,
        state_dir=d,
        node_type=s.get("node_type"),
        project_id=s.get("project_id"),
        status=s.get("status"),
        missing_required_outputs=tuple(
            str(x) for x in _tup("missing_required_outputs")),
        artifacts=_tup("artifacts"),
        upstream_rework_requests=_tup("upstream_rework_requests"),
        failure_category=s.get("failure_category"),
        failure_subcategory=s.get("failure_subcategory"),
        turns=_int("turns"),
        tool_call_count=_int("tool_call_count"),
        tokens_used=_int("tokens_used"),
        finished_at=_mtime(summary_path),
        in_flight=False,
        has_pause=has_pause,
        raw=s,
    )


def load_runs(
    base_dir: Path | None,
    *,
    project_id: str | None,
    exclude_run_id: str | None = None,
    node_type: str | None = None,
    include_in_flight: bool = False,
    limit: int = DEFAULT_SCAN_LIMIT,
) -> list[RunRecord]:
    """兄弟 run 的权威列表，**新→旧**。

    project_id 严格相等（`None` 只匹配 `None`）—— 见模块文档取舍 2。
    排序见 RunRecord.order_key（run_id 时间戳前缀优先，mtime 兜底）—— 取舍 1。

    limit 限制的是**返回条数**，不是扫描条数：先全量建 record 再排序截断，
    否则"最近 N 个目录名"和"最近 N 次 run"在名字非时间戳时不是一回事。
    """
    if base_dir is None or not base_dir.exists():
        return []
    try:
        dirs = [p for p in base_dir.iterdir() if p.is_dir()]
    except OSError:
        return []

    out: list[RunRecord] = []
    for d in dirs:
        if exclude_run_id and d.name == exclude_run_id:
            continue
        rec = _to_record(d)
        if rec is None:
            continue
        if rec.in_flight and not include_in_flight:
            continue
        # 在飞 run 的 project_id 可能读不到（transcript 首行没带）——
        # 不能因此把它当成"别的项目的"丢掉，否则进度判定又瞎了。
        if rec.project_id != project_id and not (
                rec.in_flight and rec.project_id is None):
            continue
        if node_type is not None and rec.node_type != node_type:
            continue
        out.append(rec)

    out.sort(key=lambda r: r.order_key, reverse=True)
    return out[:limit] if limit and limit > 0 else out


def latest(runs: Iterable[RunRecord], *, node_type: str | None = None
           ) -> RunRecord | None:
    """最近一次 run（runs 已是新→旧）。"""
    for r in runs:
        if node_type is None or r.node_type == node_type:
            return r
    return None


def consecutive_failures(
    runs: Iterable[RunRecord],
    node_type: str,
    *,
    break_on_other_producing_success: bool = False,
    ignore_failure_categories: frozenset[str] = frozenset(),
) -> dict | None:
    """从最新往回数 node_type 的连续失败段，返回 {count, failed_runs, signals}。

    `break_on_other_producing_success`：遇到**别的** producing 节点成功即断链 ——
    上游状况已改变，旧失败不该继续锁着本节点（E2E-3：writing 被拦死，
    orchestrator 照指令退回上游、experiment 成功跑完 28 轮，writing 仍起不来，
    因为计数只在它**自己**成功时才清零）。

    `ignore_failure_categories`：调用方额外指定的剔除类别。**外因失败
    （externally_caused：协议抽风 / 框架门禁，见 EXTERNAL_FAILURE_CATEGORIES）
    无条件剔除**，不依赖调用方传对参数 —— #426 实测：框架门禁死锁造成的
    6 次 missing:clean_results 被算成节点连败，节点被永久拒绝派发，环境
    修好也无法解锁。框架自己造成的失败不能成为锁死节点的依据。

    频次而非交集（v3.5.1）：任一信号在连续失败段里出现 ≥2 次即算重复失败。
    全程交集过严 —— 中间夹一次别的失败原因，交集立刻变空，熔断就哑了。
    """
    counts: dict[str, int] = {}
    n_failed = 0
    for r in runs:
        if r.in_flight:
            continue
        if r.node_type != node_type:
            if (break_on_other_producing_success and r.is_producing
                    and r.is_completed):
                break
            continue
        if r.is_completed:
            break
        if r.externally_caused:
            continue
        if r.failure_category in ignore_failure_categories:
            continue
        sig = r.failure_signals
        if not sig:
            continue
        n_failed += 1
        for x in sig:
            counts[x] = counts.get(x, 0) + 1
    if not counts:
        return None
    top = max(counts.values())
    if top < 2:
        return None
    return {"count": top, "failed_runs": n_failed,
            "signals": sorted(k for k, v in counts.items() if v == top)}


def best_attempt(runs: Iterable[RunRecord], node_type: str,
                 required_types: Iterable[str] = ()) -> RunRecord | None:
    """挑**走得最远**的那次当修订基线 —— 不是最近那次。

    ① 产出了几个本节点的必需产物 → ② 有没有真被评估过 → ③ 挂了几条 check →
    ④ 才看新旧。第 ② 条见 RunRecord.was_evaluated 的说明。
    """
    required = set(required_types or ())
    cands = [r for r in runs
             if r.node_type == node_type and not r.in_flight and r.artifact_types()]
    if not cands:
        return None

    def _progress(r: RunRecord) -> tuple:
        types = r.artifact_types()
        n_req = len(types & required) if required else len(types)
        return (n_req, int(r.was_evaluated), r.finished_at)

    return max(cands, key=_progress)


def system_node_streak(runs: Iterable[RunRecord], system_types: Iterable[str]
                       ) -> int:
    """从最新往回数：连续多少个 run 是纯系统节点。撞到 producing 即停。"""
    allowed = set(system_types)
    streak = 0
    for r in runs:
        if r.in_flight:
            continue
        if (r.node_type or "") in allowed:
            streak += 1
        else:
            break
    return streak


@dataclass(frozen=True)
class ChildActivity:
    """在飞子 run 的可观测活动 —— 纯文件事实，不推状态、不问模型。

    调度器要判断"现在该不该叫模型",唯一诚实的依据就是这两个量：**有没有子节点
    在飞**、**它多久没写东西了**。别的都是猜。
    """

    n_inflight: int = 0
    node_types: tuple[str, ...] = ()
    total_bytes: int = 0
    last_write_ns: int = 0
    """在飞 transcript 的最新 mtime（纳秒）。0 = 一个都没有。"""

    @property
    def any_inflight(self) -> bool:
        return self.n_inflight > 0

    def quiet_seconds(self, now: float) -> float:
        """最近一次写入距今多少秒。没有在飞 run → inf（"静默"到极限）。

        读不到 mtime 也返回 inf：倾向于**唤醒模型**。反过来（当成刚写过）会让
        框架永久等待一个已经死掉的子节点，那比多叫一次模型坏得多。
        """
        if not self.last_write_ns:
            return float("inf")
        return max(0.0, now - self.last_write_ns / 1e9)


def child_activity(base_dir: Path | None, *, project_id: str | None,
                   exclude_run_id: str | None = None) -> ChildActivity:
    """在飞子 run 现在在干什么。

    进度指纹和调度器的机械等待都用这一个 —— 两处各自 glob 一遍
    "有 transcript 没 summary" 就是第二个事实来源。
    """
    runs = [r for r in load_runs(base_dir, project_id=project_id,
                                 exclude_run_id=exclude_run_id,
                                 include_in_flight=True, limit=0)
            if r.in_flight]
    total = latest = 0
    types: list[str] = []
    for r in runs:
        types.append(r.node_type or "?")
        try:
            st = (r.state_dir / "transcript.jsonl").stat()
        except OSError:
            continue
        total += st.st_size
        latest = max(latest, st.st_mtime_ns)
    return ChildActivity(n_inflight=len(runs), node_types=tuple(sorted(types)),
                         total_bytes=total, last_write_ns=latest)


def orphaned_runs(base_dir: Path | None, current_run_id: str) -> list[RunRecord]:
    """上次会话崩溃遗留的 run：等答复中断的，或跑一半进程没了的。

    不按 project 过滤 —— 启动扫描时还不知道自己属于哪个项目，而且遗留 run 本
    来就该全报。
    """
    if base_dir is None or not base_dir.exists():
        return []
    try:
        dirs = sorted(p for p in base_dir.iterdir() if p.is_dir())
    except OSError:
        return []
    out: list[RunRecord] = []
    for d in dirs:
        if d.name == current_run_id:
            continue
        rec = _to_record(d)
        if rec is not None and rec.in_flight:
            out.append(rec)
    return out

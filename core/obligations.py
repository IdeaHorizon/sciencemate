"""未了结的义务 —— 这个项目还欠什么没做。

## 为什么有这个模块

`_continuous_followup` 里长出了 5 段拼接的 recovery 文本 + 3 处终态检查，每段
配一个临时扫描函数：`runnable_steps` / `unresolved_producers` / `pending_flows` /
`_appeals` / `_prod_fail`。再往上加一段，就是第 6 段 —— 屎山的标准长法
（wangd: "调度器的 harness 如果一点一点增加补丁那就成屎山了，得根上思考"）。

但把这几段摆在一起看，它们说的是**同一件事**：

    某个节点欠着某样东西，没补齐之前项目不算完。

差别只在"谁发现的"：节点自己申诉、框架统计出重复失败、承诺账里 metric 没测、
决策包挂着没答。既然是同一件事，就该有同一个表示、同一次渲染、同一道终态门禁。

所以这里定义**一个**概念 `Obligation`，把散落的来源收敛进来。新增一种来源
= 加一个 collector 函数，不是再往提示词里拼一段。

## 关键设计：状态从历史推，不养状态机

申诉最容易的做法是给它加 open/discharged 字段、在各处维护。但那又是一份会腐坏
的状态（E2E-4 实测：cancel 路径直接把申诉丢了，我为此单独打过一个补丁）。

这里改成**从 run 历史推**：一条"experiment 欠 writing 一份 execution_parameters"
的申诉，只要 experiment 在申诉之后成功跑过一次，就算已了结。权威来源是磁盘上的
run 记录（core.run_history），不需要任何人去"标记完成"。

同一个思路在 `core/run_history.py` 里已经用过一次：**别再造第二个事实来源。**
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core import concessions as _concessions
from core import run_history

# 义务种类。新增来源在这里加一项 + 写一个 collector，不动渲染和门禁。
KIND_APPEAL = "appeal"                     # 节点自己申诉：根因在别处
KIND_REPEATED_FAILURE = "repeated_failure"  # 框架统计：同一节点反复挂同一处
KIND_UNMEASURED = "unmeasured_commitment"   # 预注册承诺里没测的 metric
KIND_UNANALYZED_RESULTS = "unanalyzed_results"  # 实验出了结果，Analysis 还没看
KIND_UNACCOUNTED_DESIGN = "unaccounted_design"  # 预注册声明的设计，没人交代兑现
KIND_UNFROZEN_MANUSCRIPT = "unfrozen_manuscript"  # 论文审过了却没冻结 = 没交付


@dataclass(frozen=True)
class Obligation:
    """一条"还欠着"的账。"""

    kind: str
    what: str                      # 缺什么
    owed_by: str | None = None     # 谁该补（节点 type）
    claimed_by: str | None = None  # 谁提出的
    acceptance: str = ""           # 补到什么程度算合格
    blocking: bool = True          # 不补齐就不能收尾
    source_run_id: str | None = None
    discharge_hint: str = ""       # 具体怎么了结
    extra: dict = field(default_factory=dict, repr=False)

    @property
    def key(self) -> tuple:
        """去重键 —— 同一个节点欠同一样东西只算一条。"""
        return (self.kind, self.owed_by or "", self.what[:120])


# ── 来源 1：节点主动申诉 ────────────────────────────────────────────────────

def _node_dir(node_type: str) -> str:
    """某个节点在工作区里的目录 —— 问 `_NODE_WORKSPACES`，不在这里写死目录名。

    此前这里写着 `"experiment"/"artifacts"`、`"paper/artifacts/…"` 四处字面量：
    节点表一改名，这四道账就静默读空（"读不到就保守"会把它渲染成"全都欠账"）。
    """
    from core.project_workspace import _NODE_WORKSPACES

    return _NODE_WORKSPACES[node_type]


def _collect_appeals(state: Any, runs: list) -> list[Obligation]:
    """节点用 request_upstream_rework 提的诉求，**且尚未被了结**。

    了结判定不查任何标记位：被点名的节点在申诉之后成功跑过一次，就算补了。
    这样 cancel 路径丢字段、curator 忘记标记之类的意外都不会让账变脏。
    """
    out: list[Obligation] = []
    for r in runs:
        for a in r.upstream_rework_requests:
            if not isinstance(a, dict):
                continue
            target = str(a.get("upstream_node") or "").strip()
            if not target:
                continue
            # 申诉之后，被点名节点有没有成功跑过
            later_success = any(
                s.node_type == target
                and s.is_completed
                and s.order_key > r.order_key
                for s in runs
            )
            if later_success:
                continue
            out.append(Obligation(
                kind=KIND_APPEAL,
                what=str(a.get("missing") or "（未写明）"),
                owed_by=target,
                claimed_by=str(a.get("requested_by_node") or r.node_type or ""),
                acceptance=str(a.get("acceptance") or ""),
                blocking=bool(a.get("blocking", True)),
                source_run_id=r.run_id,
                discharge_hint=(
                    f"用 run_node 启动 {target}，把上面的『缺什么 / 验收标准』"
                    f"原样写进 node_inputs；补齐后再重跑申诉方。"
                ),
            ))
    return out


# ── 来源 2：框架统计出的重复失败 ────────────────────────────────────────────

def _collect_repeated_failure(state: Any, runs: list) -> list[Obligation]:
    """同一个 producing 节点连续挂在同一组 check 上 = 根因多半在上游产物。"""
    target = next((r.node_type for r in runs if r.is_producing), None)
    if not target:
        return []
    hit = run_history.consecutive_failures(runs, target)
    if not hit or hit["count"] < 2:
        return []
    try:
        from core.upstream_routing import upstream_candidates
        cands = upstream_candidates(target)
    except Exception:
        cands = []
    cand_txt = " / ".join(cands[:5]) if cands else "（无上游候选：考虑降级产物或 blocked）"
    return [Obligation(
        kind=KIND_REPEATED_FAILURE,
        what=f"{target} 连续 {hit['count']} 次挂在 `{', '.join(hit['signals'][:3])}` 上",
        owed_by=None,          # 具体该谁补由 orchestrator 从候选里选
        claimed_by="framework",
        acceptance=f"上游补齐后 {target} 能通过这些 check",
        blocking=True,
        discharge_hint=(
            f"**禁止**原样重启 {target}。三选一：\n"
            f"    (a) 退回上游（首选）：从候选选一个 → {cand_txt}，"
            f"用 run_node 启动它，把验收标准写进 node_inputs，补完再重跑 {target}；\n"
            f"    (b) 上游确实补不齐 → 让 {target} 产降级产物（pilot / gap report），"
            f"如实标注缺口，不要硬写完整交付物；\n"
            f"    (c) 确属 human-only 卡点 → CONTINUOUS_STATUS: blocked 并说明。"
        ),
        extra={"candidates": cands, "count": hit["count"]},
    )]


# ── 来源 3：预注册承诺里没测的 metric ───────────────────────────────────────

def _collect_unmeasured(state: Any, runs: list) -> list[Obligation]:
    """冻结预注册承诺了要测、但至今没有 measured 记录的 metric。

    这条不 blocking：没测完不影响继续做别的，但影响"能不能把假设判定成
    validated/refuted"（那道门禁在 core/prereg_commitments.py）。摆出来是为了
    让 orchestrator 知道还欠着，别到最后才发现关不掉假设。
    """
    try:
        from core import prereg_commitments as pc
        questions = {qid: q for qid, q in pc.frozen_questions(state).items() if q.closure}
        if not questions:
            return []
    except Exception:
        return []
    out: list[Obligation] = []
    for qid in sorted(questions):
        missing = pc.unfulfilled_items(state, qid)
        if not missing:
            continue
        q = questions[qid]
        numeric = [r["key"] for r in missing if r["kind"] == pc.NUMERIC]
        statements = [r["key"] for r in missing if r["kind"] == pc.STATEMENT]
        parts = []
        if numeric:
            parts.append(f"没测的量：{', '.join(numeric)}")
        if statements:
            parts.append(f"没勾除的条目：{', '.join(statements)}")
        out.append(Obligation(
            kind=KIND_UNMEASURED,
            what=f"{qid} 的闭合条件还没兑现完 —— " + "；".join(parts),
            owed_by="experiment",
            claimed_by="pre_registration",
            acceptance=(
                "在 experiment_log 的 metadata 里写 "
                "`measured_metrics: {<metric>: {status: measured, value: …}}`（数值条）/ "
                "`closure_discharges: {<id>: {status: discharged, "
                "evidence: <产物 id>}}`（陈述条）；"
                "这个量确实测不了时，走预注册允许的定性降级并**公开申报** —— "
                "`{status: estimated, value: …, basis: <这个数从哪来>, "
                "degraded_reason: <为什么测不了>}`，两个字段都非空才算兑现，"
                "且下游必须标注为定性降级、不得写成已测得"
            ),
            blocking=False,
            discharge_hint=(
                f"这些条目没兑现完，{qid} 就关不掉 —— 带命题的问题只能停在 "
                "provisional，不能判 validated/refuted（闭合条件是合取，任一条"
                "没兑现就既不能证实也不能证伪）。"
            ),
            extra={"question_id": qid, "hypothesis_id": qid,
                   "is_hypothesis": q.is_hypothesis,
                   "metrics": numeric, "statements": statements},
        ))
    return out


# ── 来源 4：实验出了结果，Analysis 还没看过 ─────────────────────────────────

_ANALYSIS_NODE = "hypothesis"
_WRITING_NODE = "writing"


def _iter_research_state_records(worktree):
    """转发唯一实现 core/research_state_reader（一个问题一个真相源）。"""
    from core import research_state_reader as _rs

    yield from _rs.iter_records(worktree)


def _research_state_version(record: dict) -> int:
    from core import research_state_reader as _rs

    return _rs.version_of(record)


def _meta_of(record: dict) -> dict:
    from core import research_state_reader as _rs

    return _rs.metadata_of(record)


def _latest_research_state(worktree) -> tuple[int, float, set[str]] | None:
    """最新一版 research_state：(version, created_at 秒, 已入账的实验 run_id)。

    读文件本身，不读任何"我已经分析过了"的自证字段作为唯一依据 ——
    `created_at` 是产物层写的，进 Git；`completed_experiments` 是产出方写的，
    只当作额外的了结路径。
    """
    from datetime import datetime

    best: tuple[int, dict] | None = None
    for record in _iter_research_state_records(worktree) or []:
        version = _research_state_version(record)
        if version >= 1 and (best is None or version > best[0]):
            best = (version, record)
    if best is None:
        return None
    version, record = best
    meta = _meta_of(record)
    try:
        created = datetime.fromisoformat(str(record.get("created_at"))).timestamp()
    except (TypeError, ValueError):
        created = 0.0
    accounted = {str(x) for x in (meta.get("completed_experiments") or []) if x}
    return version, created, accounted


def _runs_requiring_verdict(worktree) -> dict[str, bool]:
    """每个 experiment run 自己声明的：这一趟跑出来的东西**需不需要**Analysis 裁决。

    权威来源是 experiment 每次收尾写的 `run_manifest`（`run_manifest.v1`）：
    `run_id` / `run_role` / `stage` / `requires_hypothesis_verdict` 全在里面，
    最后一项就是 `run_role == "primary" and stage == "simulation"`。

    ## 为什么要读它（issue #413）

    "欠 Analysis" 这条账此前只按 `experiment + completed` 一刀切。可编译检查、
    环境诊断、调试运行、secondary run 都会正常完成，却**没有任何科学结果需要
    裁决** —— 于是框架反复把 Analysis 推去读一堆没有结论的运行。

    experiment 早就把这个判断写进了 run_manifest，框架只是没读。属于典型的
    "机制存在但没接到路径"，不是"缺一个机制"。

    ## 读不到就保守

    没有 manifest 的历史运行（以及 manifest 写失败的运行）一律按 **True** 处理
    —— 少做一次分析的代价远大于多提醒一次。
    """
    import json
    from pathlib import Path

    out: dict[str, bool] = {}
    if not worktree:
        return out
    from core.ledger import iter_heads, workspace_store

    # run_manifest 留在研究记录里（artifact_policy 里它不是 run_local）：它是实验对
    # 预注册版本的绑定凭据，这里就是它的跨 run 读者。每个 run 一份身份
    # （experiment 侧按 run_id 命名），账本按类型取全部 head，再读正文。
    store = workspace_store(Path(worktree))
    records: list[tuple[str, dict]] = []
    for head in iter_heads(worktree, artifact_type="run_manifest"):
        record = store.record(head.artifact_id)
        if isinstance(record, dict):
            records.append((str(record.get("created_at") or ""), record))
    # 同一个 run 可能有起始版和收尾版 —— 按登记时间排序，后写的覆盖先写的。
    for _, record in sorted(records, key=lambda item: item[0]):
        manifest = record.get("content")
        if isinstance(manifest, str):
            try:
                manifest = json.loads(manifest)
            except ValueError:
                manifest = None
        if not isinstance(manifest, dict):
            manifest = {}
        meta = record.get("metadata")
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except ValueError:
                meta = {}
        meta = meta if isinstance(meta, dict) else {}
        run_id = str(manifest.get("run_id") or meta.get("run_id")
                     or record.get("produced_by_run_id") or "")
        if not run_id:
            continue
        required = manifest.get("requires_hypothesis_verdict")
        if required is None:
            # 老 manifest 没这个字段 → 用它的两个组成部分现算，还是没有就保守。
            role = manifest.get("run_role") or meta.get("run_role")
            stage = manifest.get("stage")
            if role is None and stage is None:
                # ── 兼容分支（owner: framework / wangd；#979 顺带观察）────────
                # 只对**存量** run_manifest 生效：2026-09-11 之前冻结的那些既没有
                # `requires_hypothesis_verdict`，也可能没有 run_role/stage，只剩这个
                # 已删字段。新 run 一律走上面第一级，这条对它们不触发。
                # 删除条件：项目库里不再有缺 run_role 的 run_manifest —— 在那之前
                # 删掉它会让历史 run 的"欠不欠裁决"全部退回保守的 True，把一批早已
                # 结清的义务重新翻出来。
                eligible = manifest.get("analysis_eligible")
                if eligible is None:
                    eligible = meta.get("analysis_eligible")
                required = True if eligible is None else bool(eligible)
            else:
                required = (str(role) == "primary"
                            and str(stage or "simulation") == "simulation")
        out[run_id] = bool(required)
    return out


def _collect_unanalyzed_results(state: Any, runs: list) -> list[Obligation]:
    """实验跑完了、Analysis 没被再调用过 —— 这条账框架能自己算，别靠模型想起来。

    ## 为什么加这条（E2E v23，2026-08-10 → 08-11 实测）

        08-10 06:47  research_state v1：completed_experiments=[]，
                     next_steps 自己写着"分析拟合 Arrhenius 与 VFT，按预注册
                     判定树裁决"
        08-11 02:20  experiment 跑完：76 分钟、9 个真实 LAMMPS 模拟
        08-11 02:25  → _reviewer → _curator → _reviewer 重试 → 进程死

        research_state 至今 **还是 v1**。

    计划自己写着下一步做分析，实验也真出了结果，然后没有任何机制提醒
    orchestrator 该回 Analysis。研究回路断在这，全靠模型记性。

    ## 判据用时间戳，不用列表

    `completed_experiments` 是产出方自己写的（自证，改个字段就能造假）；
    `created_at` 是产物层写的、进 Git。所以主判据是"最新 research_state 比
    这次实验还早"，列表只作为**额外的**了结路径。

    ## 只对"有结论要裁决"的运行记账（#413）

    哪些运行有结论要裁决，是 experiment 自己在 run_manifest 里声明的
    （`requires_hypothesis_verdict` = primary + simulation）。框架读那个声明，
    不自己造判据，也不按 node_type 一刀切。

    ## 它只看见，不判质量

    这条账说的是"结果出来之后 Analysis 没被调用过"，不是"分析做得对不对"。
    后者 reviewer 读得到产物，比任何计数器强。
    """
    worktree = getattr(state, "project_worktree", None)
    requires_verdict = _runs_requiring_verdict(worktree)
    finished = [
        r for r in runs
        if str(getattr(r, "node_type", "")) == "experiment"
        and str(getattr(r, "status", "")) == "completed"
        and float(getattr(r, "finished_at", 0) or 0) > 0
        # #413：只有 run 自己声明"这次的结果要 Analysis 裁决"才欠这笔账。
        # 编译检查 / 环境诊断 / 调试 / secondary run 正常完成，但没有科学结论。
        # 读不到声明（老运行、manifest 写失败）→ 保守当作要裁决。
        and requires_verdict.get(str(getattr(r, "run_id", "")), True)
    ]
    if not finished:
        return []
    latest = _latest_research_state(worktree)
    if latest is None:
        # 连 v1 都没有 = 还没走到 Analysis 这一步，是别的问题，不在这条账里吵。
        return []
    version, created, accounted = latest
    stale = [
        r for r in finished
        if float(r.finished_at) > created and str(r.run_id) not in accounted
    ]
    if not stale:
        return []
    ids = [str(r.run_id) for r in sorted(stale, key=lambda r: float(r.finished_at))]
    listed = ", ".join(ids[:4]) + ("…" if len(ids) > 4 else "")
    return [Obligation(
        kind=KIND_UNANALYZED_RESULTS,
        what=(f"experiment 已出结果（{listed}），但 research_state 还停在 "
              f"v{version}（早于这些实验）—— Analysis 没看过这批结果"),
        owed_by=_ANALYSIS_NODE,
        claimed_by="framework",
        acceptance=(f"{_ANALYSIS_NODE} 读 research_state v{version} + 直接读 "
                    f"experiments/ 的结果，产出 research_state v{version + 1}"),
        blocking=True,
        discharge_hint=(
            f"用 run_node 启动 `{_ANALYSIS_NODE}`，在 node_inputs 里点名要它消化 "
            f"{listed}：先 read_research_state()，再读 experiment/ 目录里的结果，"
            f"按预注册的判定树裁决假说，然后 update_research_state() 出 "
            f"v{version + 1}。\n"
            "**结果没被分析过就去 writing/收尾 = 做完实验直接写论文。**"
        ),
        extra={"runs": ids, "research_state_version": version},
    )]



# ── 来源 6：论文审过了却没冻结 = 没交付 ─────────────────────────────────────

def _latest_by_created(worktree, artifact_type: str):
    """worktree 账本里某类型最新（created_at 最大）的一份 record，连同它的路径。

    返回 (record, path) 或 None。跨节点读走账本，与 _frozen_designs 同套路子。
    """
    from core.ledger import workspace_store

    store = workspace_store(Path(worktree))
    best = None  # (ts, record, path)
    for head in store.heads().values():
        if head.artifact_type != artifact_type:
            continue
        rec = store.record(head.artifact_id)
        if not isinstance(rec, dict):
            continue
        ts = _stamp(rec.get("created_at"))
        if best is None or ts >= best[0]:
            best = (ts, rec, store.abs_path(head))
    if best is None:
        return None
    return best[1], best[2]


def _manuscript_pdf_hashes(worktree, manuscript: dict) -> dict[str, str]:
    variants = ((manuscript.get("metadata") or {}).get("pdf_variants") or {})
    hashes: dict[str, str] = {}
    if not isinstance(variants, dict):
        return hashes
    for name in ("review", "clean"):
        variant = variants.get(name)
        raw = variant.get("pdf_path") if isinstance(variant, dict) else None
        if not isinstance(raw, str) or not raw.strip():
            continue
        path = Path(raw)
        if not path.is_absolute():
            path = Path(worktree) / path
        if path.is_file():
            hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return hashes


def _latest_writing_verdict(worktree, artifact_id: str, manuscript: dict) -> str:
    """Latest verdict bound to this exact manuscript body and PDF bytes.

    ## 权威源是 metadata，不是 content（2026-08-23 E2E v29 实测）

    review_spec.md 强制 reviewer 在 `save_artifact` 的 **metadata** 里写结构化的
    `verdict` / `recommended_action` —— 那是契约字段、类型干净。`content` 是给人读的
    大 blob，实测里 reviewer 把整个 `decision` 对象塞成一个 **repr 风格的字符串**
    （`"{'action': 'proceed', ...}"`），json.loads 出来 `decision` 是 str 不是 dict，
    旧代码于是既取不到 action、也 fallback 到整串 → 返回一坨长字符串 → 不在
    ("approve","proceed") 集合里 → 冻结义务在一份真 approve 的稿子上**静默不触发**。

    从 content 猜结构是脆的；metadata 是 reviewer 被要求填的结构化契约。优先读它，
    content 只当最后兜底。（"报告不是事实"的反面：结构化契约字段才是。）
    """
    content_hash = hashlib.sha256(
        str(manuscript.get("content") or "").encode("utf-8")
    ).hexdigest()
    pdf_hashes = _manuscript_pdf_hashes(worktree, manuscript)
    found: list[tuple[float, dict]] = []
    from core.ledger import workspace_store

    _store = workspace_store(Path(worktree))
    for _head in _store.heads().values():
        if (_head.artifact_type != "review_critique"
                or not _head.artifact_id.startswith("review_critique__writing_critique")):
            continue
        candidate = _store.record(_head.artifact_id)
        if not isinstance(candidate, dict):
            continue
        metadata = candidate.get("metadata") or {}
        subject = metadata.get("review_subject") or {}
        content = str(candidate.get("content") or "")
        try:
            payload = json.loads(content)
        except (TypeError, ValueError):
            payload = {}
        payload_action = (
            (payload.get("recommended_action") or {}).get("action")
            if isinstance(payload, dict)
            and isinstance(payload.get("recommended_action"), dict)
            else None
        )
        if (
            candidate.get("produced_by_node_type") == "_reviewer"
            and candidate.get("content_hash")
            == hashlib.sha256(content.encode("utf-8")).hexdigest()
            and isinstance(payload, dict)
            and payload.get("_composed_by") == "compose_review_critique"
            and payload.get("review_subject") == subject
            and payload.get("verdict") == metadata.get("verdict")
            and payload_action == metadata.get("recommended_action")
            and metadata.get("review_incomplete") is not True
            and isinstance(subject, dict)
            and subject.get("artifact_id") == artifact_id
            and subject.get("version") == manuscript.get("version")
            and subject.get("content_hash") == content_hash
            and subject.get("pdf_variant_sha256") == pdf_hashes
        ):
            found.append((_stamp(candidate.get("created_at")), candidate))
    if not found:
        return ""
    rec = max(found, key=lambda item: item[0])[1]

    # ① 权威：metadata 的结构化契约字段
    meta = rec.get("metadata") or {}
    for key in ("recommended_action", "verdict"):
        val = str(meta.get(key) or "").strip().lower()
        if val:
            return val

    # ② 兜底：content（结构不稳，尽力而为）
    content = rec.get("content")
    inner: dict = {}
    if isinstance(content, str):
        try:
            inner = json.loads(content)
        except (ValueError, TypeError):
            inner = {}
    elif isinstance(content, dict):
        inner = content
    decision = inner.get("decision")
    if isinstance(decision, dict) and decision.get("action"):
        return str(decision.get("action")).strip().lower()
    return str(inner.get("recommended_action") or inner.get("verdict") or "").strip().lower()


def _collect_unfrozen_manuscript(state: Any, runs: list) -> list[Obligation]:
    """完成态论文写出来、审过了，却没冻结 —— 没冻结 = 账本没钉死 = 没交付。

    ## 病例（E2E v27 + v28，同一格两种坏法）

    - v27：reviewer approve 后 orchestrator 想 freeze，被"作者签字不能代签"机械拒
      （artifacts_extra 的 owner!=here），模型转而**伪造** frozen 字段绕过（#617 堵了）。
    - v28：reviewer 两审 approve，但没有任何一步真的 freeze，writing run 却 completed。
      manuscript 至今 `frozen=None`、账本上没有冻结行。

    两次同因：completion 门只检查"审查 flow 闭合"，而 flow 一闭合就从
    `pending_post_node_flow` 出列 —— **没有任何机制检查"被批准的论文真的冻结了"**。
    这条账补上这道判据。

    ## 为什么锚在产物、不锚 flow
    审查 flow 闭合即出列，事后看不到。`metadata.frozen` 是产物层字段、进 Git ——
    与「判决不落盘、事实落盘」一致。

    ## 三重触发，防两类误伤
    1. 最新 manuscript `preflight_status=passed` 且未冻结 —— blocked 的是"材料不足
       报告"（writing harness 明令不得称 Manuscript Complete），本不该冻，跳过。
    2. 最新 writing_critique 裁决是 approve/proceed —— 没审过（草稿期）或被打回
       （revise/redirect）都不催冻结，否则会催冻一份 reviewer 拒了的稿。
    3. 没有仍然 open、且引用这份 manuscript 的 post_node_flow —— 决策还在结算时
       别插队（那一步自己会拦）。

    了结：writing 自己 freeze_artifact 冻结它。**冻结是作者签字，orchestrator 不能
    代签** —— 所以 owed_by=writing，discharge 也指向 writing，不会造出一个够不着的门。
    """
    worktree = getattr(state, "project_worktree", None)
    if worktree is None:
        return []

    found = _latest_by_created(worktree, "manuscript")
    if found is None:
        return []
    rec, _ = found
    meta = rec.get("metadata") or {}

    # ① 完成态才欠冻结；材料不足报告 / 草稿不欠
    if str(meta.get("preflight_status") or "").strip().lower() != "passed":
        return []
    # 已冻结 = 账清
    if meta.get("frozen"):
        return []

    # ② 审查必须已 approve/proceed —— 没审过或被打回都不催冻
    artifact_id = str(rec.get("id") or f"manuscript__{rec.get('name', '')}")

    # ② 审查必须批准**当前这版**正文和 PDF。只认同名 critique 会让旧 approve
    # 在改稿/换 PDF 后继续催冻结，随后又被冻结门拒绝，形成无解循环。
    if _latest_writing_verdict(worktree, artifact_id, rec) not in (
        "approve",
        "proceed",
        "accept",
    ):
        return []

    # ③ 决策还在结算（flow 仍 open 且点名这份）时不插队
    for entry in (state.hook_state.get("pending_post_node_flow") or []):
        if isinstance(entry, dict) and artifact_id in (entry.get("artifact_ids") or []):
            return []

    return [Obligation(
        kind=KIND_UNFROZEN_MANUSCRIPT,
        what=(f"完成态论文 {artifact_id} 已写出并通过审查（preflight_status=passed、"
              f"reviewer approve），但没有冻结 —— 没冻结 = 账本没钉死、没算交付"),
        owed_by=_WRITING_NODE,
        claimed_by="framework",
        acceptance=(f"writing 自己 freeze_artifact(artifact_id='{artifact_id}') 冻结这份"
                    f"论文（作者签字），触发升 deliverable"),
        blocking=True,
        discharge_hint=(
            f"用 run_node 启动 `writing`，node_inputs 里点名「freeze 已通过审查的 "
            f"{artifact_id}」：writing 调 freeze_artifact(artifact_id='{artifact_id}')，"
            f"**不要重写论文、不要另存新版**。冻结是作者签字，orchestrator 代签会被"
            f"机械拒（owner!=here）。\n"
            f"**审查 approve 了却没冻结 = 论文没交付**（v27 伪造 / v28 干脆没冻，"
            f"都栽在这一格）。"
        ),
        extra={"artifact_id": artifact_id},
    )]


# ── 来源 5：预注册声明了设计，没人交代兑现 ──────────────────────────────────

_DESIGN_ACCOUNTING_KEYS = ("design_accounting", "expected_params_accounting")


def _stamp(value: object) -> float:
    """ISO 时间戳 → 秒。解析不了就返回 0（= 没有可用时间信息）。"""
    from datetime import datetime

    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except (TypeError, ValueError):
        return 0.0


def _frozen_designs(worktree) -> list[dict]:
    """**每一份**冻结预注册各自声明的实验设计。

    只认冻结的：没冻的还在改，拿它当承诺会在设计期就吵。

    ## 为什么是列表（issue #414）

    这里以前是 `_frozen_design()`：glob 出所有 `pre_registration*.json`，
    命中第一份冻结的就 `return` —— 一个项目多轮修订会留下 v1…v6 多份冻结预
    注册，而框架只看第一份，其余几份的设计声明**从来没有进过账**。同一个
    项目的多份承诺被混成一笔账，正是 issue #414 报的现象。

    返回每份的 `{id, params, frozen_at}`：`frozen_at` 取 `metadata.frozen_at`
    （freeze_artifact 写的），退回 `created_at`；两个都没有就是 0.0，
    表示"这份没有可用的时间信息"——判定那边据此退回宽松口径，见
    `_design_accounted_for`。
    """
    import json
    from pathlib import Path

    from core.ledger import iter_heads, workspace_store

    out: list[dict] = []
    if not worktree:
        return out
    store = workspace_store(Path(worktree))
    for head in iter_heads(worktree, artifact_type="pre_registration"):
        # 一个身份 = 一条账；head 可能是未冻结的修订草稿 —— 当前承诺是该身份
        # **最新冻结版**的设计（修订 + 重新冻结后 frozen_at 前移，时间判据自动
        # 把旧账重新记开）。
        record = store.latest_frozen(head.artifact_id)
        if record is None:
            continue
        meta = record.get("metadata") or {}
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except ValueError:
                continue
        params = meta.get("expected_params") or {}
        if not (isinstance(params, dict) and params):
            continue
        out.append({
            "id": head.artifact_id,
            "name": str(record.get("name") or head.artifact_id),
            "params": params,
            "frozen_at": _stamp(meta.get("frozen_at")) or _stamp(record.get("created_at")),
        })
    return out


def _design_accountings(worktree) -> list[dict]:
    """交代过设计兑现情况的每一版 research_state：`{at, text}`。

    `text` 是那份 accounting 的 JSON 序列化，用来判断它有没有**点名**某份
    预注册（点名了就只清那一份，不管先后）。
    """
    import json

    out: list[dict] = []
    for record in _iter_research_state_records(worktree) or []:
        meta = _meta_of(record)
        entries = [meta.get(k) for k in _DESIGN_ACCOUNTING_KEYS if meta.get(k)]
        if not entries:
            continue
        out.append({
            "at": _stamp(record.get("created_at")),
            "text": json.dumps(entries, ensure_ascii=False),
        })
    return out


def _design_accounted_for(design: dict, accountings: list[dict],
                          all_names: set[str]) -> bool:
    """这**一份**冻结预注册的设计，有没有被哪一版 research_state 交代过。

    逐笔 accounting 看，两种账分开算：

      · **点名的账**（正文里出现了某份预注册的 id 或 name）：只清它点到的那几份。
        点了名就说明它讲的是特定的某一份，不能顺带把别人的账也清了。
      · **笼统的账**：按时间——它写在这份预注册冻结**之后**才算数。一版
        research_state 只可能交代它写作时已经存在的东西；冻在它之后的那份，
        它不可能交代过（issue #414 的现场就是被一笔旧账清掉了新冻的 v2）。

    ⚠️ 笼统账缺时间戳时退回**宽松**口径（有账就算清）—— 宁可少记一笔，也不要
    造出一笔无法了结的账：这条义务是 blocking 的，判严了就是把项目锁死在一个
    模型无从解除的门上。
    """
    for acc in accountings:
        mentions_self = design["id"] in acc["text"] or design["name"] in acc["text"]
        if mentions_self:
            return True
        if any(n in acc["text"] for n in all_names):
            continue                        # 点的是别人的名，与这份无关
        if not (design["frozen_at"] and acc["at"]):
            return True                     # 没有可比的时间 → 老口径
        if acc["at"] >= design["frozen_at"]:
            return True
    return False


def _collect_unaccounted_design(state: Any, runs: list) -> list[Obligation]:
    """每一份冻结预注册声明的实验设计，都得有人交代兑现情况。

    ## 为什么加这条（E2E v23，2026-08-11 实测）

    冻结的预注册写着 7 个温度点 × 2 副本。实际跑出来 7 个点都有生产日志，但
    **只有 3 个点有轨迹**，另外 4 个既没轨迹、日志里也没有 MSD 列 —— 那 4 个点
    根本算不出扩散系数。预注册要 7 个点区分 Arrhenius / VFT，可用的只有 3 个。

    没有任何一层提出这件事：`_collect_unmeasured` 读的是预注册**正文**里的
    YAML metric 块（这份没写成那个形状 → 返回空 → 义务永不触发）；
    hypothesis 这一轮一次没跑过；reviewer 审的是一条作业提交记录。

    ## 一份预注册一笔账（issue #414，2026-08-12）

    此前这里是"取第一份冻结预注册 + 任意一版 research_state 里任意非空
    accounting"→ 一个布尔。多轮修订的项目会留下多份冻结预注册，于是
    **后面几份的设计声明从来没进过账，而且第一份的一笔账把所有份都清了**。
    现在逐份判定、逐份记账，义务文案里点名是哪一份。

    ## 只做"看见"，不做判决

    机器机械知道的只有一件事：**声明过一个设计，而没人交代过兑现情况**。
    至于"3 个点够不够判定"，那是 Analysis 读了数据才能下的判断 —— 硬编码成
    阈值就是又一个会凑会误伤的门（今晚下线 QC 判决层正是因为这个）。

    ## 判据从已有字段现算

    不要求节点在预注册里多写一份 `metrics` —— 多写一份就多一个会分叉的真相源。
    `expected_params` 本来就是机器可读的。
    """
    worktree = getattr(state, "project_worktree", None)
    designs = _frozen_designs(worktree)
    if not designs:
        return []
    accountings = _design_accountings(worktree)
    all_names = {d["id"] for d in designs} | {d["name"] for d in designs}
    out: list[Obligation] = []
    for design in designs:
        if _design_accounted_for(design, accountings, all_names):
            continue
        params = design["params"]
        shape = []
        for key in ("temperature_points", "n_replicas", "n_particles", "composition",
                    "density", "models"):
            value = params.get(key)
            if value is None:
                continue
            shape.append(f"{key}={len(value) if isinstance(value, list) else value}")
        if not shape:
            continue
        listed = "、".join(shape[:4])
        many = len(designs) > 1
        whose = f"冻结预注册 `{design['name']}`" if many else "冻结预注册"
        out.append(Obligation(
            kind=KIND_UNACCOUNTED_DESIGN,
            what=(f"{whose} 声明了实验设计（{listed}），但没有任何一版 "
                  f"research_state 交代过它的兑现情况"),
            owed_by=_ANALYSIS_NODE,
            claimed_by="pre_registration",
            acceptance=("research_state 的 metadata 里写 `design_accounting`：逐项说明"
                        "声明的设计点哪些实现了、哪些没有、没实现的原因"
                        + (f"，并点名这份预注册（`{design['name']}`）"
                           if many else "")),
            blocking=True,
            discharge_hint=(
                f"用 run_node 起 `{_ANALYSIS_NODE}`：拿实际产出对照冻结预注册 "
                f"`{design['name']}` 的 expected_params，把**每一项**的兑现情况"
                f"写进 research_state 的 `design_accounting`。\n"
                + ("这个项目有多份冻结预注册，每一份都要各自交代 —— "
                   "在 accounting 里写上是哪一份（用它的名字），"
                   "一笔笼统的账清不掉后冻的那几份。\n" if many else "")
                + "**如实写**：没实现的就写没实现 —— 这条账要的是交代，不是好看的数字。\n"
                "实际数据能否支撑判定，由你读了数据自己判断；框架不替你判，也不设阈值。"
            ),
            extra={"pre_registration": design["name"],
                   "artifact_id": design["id"],
                   "expected_params": {k: params.get(k) for k in list(params)[:8]}},
        ))
    return out


_COLLECTORS = (_collect_appeals, _collect_repeated_failure, _collect_unmeasured,
               _collect_unanalyzed_results, _collect_unaccounted_design,
               _collect_unfrozen_manuscript)


# ── 汇总 / 渲染 / 门禁 ──────────────────────────────────────────────────────

def collect(state: Any, *, scan_limit: int = 60) -> list[Obligation]:
    """本项目当前所有未了结的义务。blocking 的排前面。"""
    try:
        runs = run_history.load_runs(
            state.root.parent, project_id=state.project_id,
            exclude_run_id=state.run_id, limit=scan_limit)
    except Exception:
        runs = []
    out: list[Obligation] = []
    seen: set[tuple] = set()
    for fn in _COLLECTORS:
        try:
            items = fn(state, runs)
        except Exception:
            continue
        for o in items:
            if o.key in seen:
                continue
            seen.add(o.key)
            out.append(o)
    # 让步应答（判决拆除战役批 0）：blocking 义务若有在案让步记录，解除其
    # 拦截力但**不消失**——照渲染（标 🤝）、照进 summary、照交 referee 终审。
    # 在唯一收口处比对，三个消费端（render / closure / chat 终态闸）零改动生效。
    conceded = _concessions.load(state)
    if conceded:
        from dataclasses import replace as _replace
        out = [
            _replace(o, blocking=False,
                     extra={**o.extra, "conceded": conceded[_concessions.obligation_key_hash(
                         o.kind, o.owed_by, o.what)]})
            if o.blocking and _concessions.obligation_key_hash(
                o.kind, o.owed_by, o.what) in conceded
            else o
            for o in out
        ]
    out.sort(key=lambda o: (not o.blocking, o.kind))
    return out


def blocking(obligations: list[Obligation]) -> list[Obligation]:
    return [o for o in obligations if o.blocking]


def render(obligations: list[Obligation]) -> str:
    """给 orchestrator 看的一段账。空则返回空串。

    **一次渲染** —— 新增义务种类不再往提示词里拼新段落。
    """
    if not obligations:
        return ""
    lines = ["\n📋 **本项目未了结的义务**（应答前不算完成）："]
    for o in obligations:
        conceded = o.extra.get("conceded") if isinstance(o.extra, dict) else None
        mark = "🤝" if conceded else ("🔴" if o.blocking else "⬜")
        who = f"**{o.owed_by}** 该补" if o.owed_by else "待定归属"
        src = f"（{o.claimed_by} 提出）" if o.claimed_by else ""
        lines.append(f"  {mark} [{o.kind}] {who}{src}：{o.what[:160]}")
        if conceded:
            lines.append(
                f"      已让步（记录在案，待终审）：{str(conceded.get('reason'))[:160]}")
            continue
        if o.acceptance:
            lines.append(f"      验收：{o.acceptance[:160]}")
        if o.discharge_hint:
            lines.append(f"      → {o.discharge_hint}")
        if o.blocking:
            kh = _concessions.obligation_key_hash(o.kind, o.owed_by, o.what)
            lines.append(
                f"      应答方式：补齐（首选）；或 concede_obligation("
                f"key_hash='{kh}', reason=...) 公开让步——让步进永久账本与"
                f"稿件局限节，由终审裁决，且改不了任何 status。")
    lines.append(
        "  节点自己提的申诉比框架的机械统计更可信 —— 它最清楚缺什么。"
        "**不要忽略这些账去重跑申诉方**：同样的输入只会得到同样的失败。")
    return "\n".join(lines)

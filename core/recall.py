"""v1.0：experience retrieval primitive —— 把 KB 从"参考资料"升级到"agent 成长"。

设计第一性原理：
  KB 是状态。成长是状态 → 行为的提升。当前 framework 写 KB 强（curator /
  dreaming / Phase I），但**读 KB 死注入**（kb_query keyword → top-N 一次性
  塞）。agent 烧再多 token，KB 也只是死资料。

  能让 KB 真改变行为的，只有两个时点：
    A. **任务开始前**（pre-run briefing）—— 摆出"过去类似情境的人生总和"
    B. **关键决策时**（memory_recall tool）—— 调"特定决策的历史"

  两个时点共用底层 primitive：`recall(state, query, ...) → 6 类结构化 bucket`。
  - search_kb 是 lookup（"找 KB 里关于 X 的 claim"）
  - recall 是 experience retrieval（"我过去面对类似情境时，发生过啥？"）

返回 6 类 bucket（每类按 similarity desc, max k 条）：

  1. prior_runs        : 跨项目 prior 研究产物 chunks（manuscript /
                          analysis_report / survey_report 类）
  2. active_dead_ends  : 强相关 dead_end claim（org scope，跨项目）
  3. validated_methods : methodological claim，validated +
                          replication ≥ 1

embedding similarity 基于 v0.4.2 的 kb_vector_index；纯 Python，零额外 LLM call。

graceful degrade：KB / embedding 不可用时所有 bucket 为空 list；调用方应当
能处理空结果。
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

log = logging.getLogger("recall")

if TYPE_CHECKING:
    from core.state import State

# 用于 prior_runs：被识别为"研究产物"的 chunk source artifact types
# 这些 artifact 一旦入 chunk 就是 prior_runs 的语义载体。入 chunk 的路径是
# freeze_artifact —— 但只有 chunk_then_drop 类（experiment_log 等）+
# pre_registration 会在冻结时自动登记；manuscript / analysis_report /
# survey_report / research_plan 是 permanent 类，不会被自动登记。
RESEARCH_PRODUCT_ARTIFACT_TYPES = frozenset({
    "manuscript", "analysis_report", "survey_report",
    "experiment_log", "pre_registration", "research_plan",
})



@dataclass
class RecallResult:
    """结构化 recall 结果。每条 entry 是 dict，含 claim/chunk/memory id + 摘要。

本模块只管 **KB 与 run 历史**的经验召回。项目记忆（目标/铁律/叙事/手册）
    由 `core.memory_delivery` 送达 —— 两者曾经混在这里，代价是
    `consolidated_notes` 读 `memory/*.md` 而 curator 写 `worktree/MEMORY.md`
    分节，**写在 A 读在 B**，那条"把踩坑经验摆进 briefing"的补丁自己变成了
    另一个不可见。

    删掉的桶（2026-08-21）：
      · `pending_questions` —— 判据是 `claim_type == "question"`，而这个值
        从来不在 `CLAIM_TYPES` 里，从 v3 schema 合并那天起就是死的，
        briefing 却每次照常打印一段"open questions（0 条）"。
      · `user_directives` / `recent_observations` / `consolidated_notes`
        —— 全部是项目记忆，已归 memory_delivery。
    """
    prior_runs: list[dict] = field(default_factory=list)
    active_dead_ends: list[dict] = field(default_factory=list)
    validated_methods: list[dict] = field(default_factory=list)
    prior_failed_checks: list[dict] = field(default_factory=list)
    load_bearing_capital: list[dict] = field(default_factory=list)
    card_drafts_not_injected: int = 0     # 超出注入预算、没进 briefing 的草稿数

    def to_dict(self) -> dict:
        return {
            "prior_runs": self.prior_runs,
            "active_dead_ends": self.active_dead_ends,
            "validated_methods": self.validated_methods,
            "prior_failed_checks": self.prior_failed_checks,
            "load_bearing_capital": self.load_bearing_capital,
        }

    def total_items(self) -> int:
        return sum(len(v) for v in self.to_dict().values())

    def summary_counts(self) -> dict[str, int]:
        return {k: len(v) for k, v in self.to_dict().items()}


def recall(
    state: "State",
    query: str,
    *,
    k_per_category: int = 5,
    min_cosine: float = 0.7,
    include_categories: tuple[str, ...] = (
        "prior_runs", "active_dead_ends", "validated_methods",
        "prior_failed_checks", "load_bearing_capital",
    ),
) -> RecallResult:
    """experience retrieval：基于 embedding 相似度跨 project 找 6 类 bucket。

    Args:
      state: harness State；用 state.project_id / list_kb / list_memory
      query: 自然语言 query（节点的 research_question 或 LLM 决策上下文）
      k_per_category: 每 bucket max 条数（默认 5）
      min_cosine: similarity 阈值（默认 0.7；太低 noise 多）
      include_categories: 要 retrieve 的 bucket 集合（按需 opt-out 省成本）

    Returns:
      RecallResult，所有 6 bucket（部分按 include_categories 可能为空 list）。

    Robustness:
      - embedding 不可用 / KB 空 / index 空 → 该 bucket 返 [] 不抛
      - HARNESS_DISABLE_SEMANTIC_DEDUP=1 跳 embedding（测试 / CI 用）
    """
    result = RecallResult()
    if not query or not query.strip():
        return result

    # 1. embedding query vector（可能不可用）
    qvec = _try_embed_query(query)

    # 1.5 embedding 可用时，先确保向量索引已建（briefing 可能先于任何 write_kb 跑）
    if qvec is not None:
        _ensure_index_warm(state)

    # 2. claim-based buckets（dead_end / validated_methods）
    if qvec is not None and any(
        c in include_categories
        for c in ("active_dead_ends", "validated_methods")
    ):
        _fill_claim_buckets(
            state, qvec, result, k_per_category=k_per_category,
            min_cosine=min_cosine, include_categories=include_categories,
        )

    # 3. prior_runs：已停用。chunks 从未进向量索引（SUPPORTED_ENTITIES 只有
    #    claims/concepts），_fill_prior_runs 查 chunks 恒抛异常被吞、bucket 恒空，
    #    只在 briefing 里留一段误导性的"找到 0 条"。保留字段与函数以便将来给 chunks
    #    建索引后重新启用；当前不调用、不渲染。
    # if qvec is not None and "prior_runs" in include_categories:
    #     _fill_prior_runs(...)

    # 4–6（user_directives / recent_observations / consolidated_notes）已删：
    #    那三桶是**项目记忆**，现在由 core.memory_delivery 按 applies_to 机械
    #    送达。混在这里的代价已经付过 —— consolidated_notes 读 memory/*.md，
    #    而 curator 写 worktree/MEMORY.md 分节，写在 A 读在 B。

    # 7. prior_failed_checks：同 node_type 上一个失败 run 缺了哪些必需产物。
    #    直击 writing 重试循环——以前每次重试 briefing 的 prior_runs=0，同一个 QC
    #    坑被反复重踩 40+ 次，判定原因从不跨 run 传递。这里机械扫兄弟 run 的
    #    summary.json，把最近一次失败清单摆进 briefing。纯 advisory，不改控制流。
    if "prior_failed_checks" in include_categories:
        _fill_prior_failed_checks(state, result)

    # 8. load_bearing_capital：承重层全量注入（预算 ≤12 条，不依赖 embedding
    #    相似度 —— 决策资本的价值不取决于它和当前 query 的字面相关性，而在于
    #    它应当参与每一次方向性选择）。这是"设计时消费"的落地点。
    if "load_bearing_capital" in include_categories:
        _fill_load_bearing_capital(state, result)

    return result


# ─────────────────────────────────────────────────────────────────────────────
# helpers
# ─────────────────────────────────────────────────────────────────────────────


# recall 自己的进程级索引预热 flag（与 state.py 的 _SEMANTIC_INDEX_INITIALIZED
# 分开，因为 briefing 可能在本进程任何 write_kb 之前就跑 —— 那时写路径的懒构建
# 还没触发过，索引磁盘 manifest 缺失/过期，query_across_scopes 读到空索引后静默
# 返空、永不自建。这里在 recall 首次要用 embedding 时补一次 rebuild_if_needed。
_RECALL_INDEX_WARMED: dict[str, bool] = {}


def _ensure_index_warm(state) -> None:
    """embedding 可用时，确保 org + project 向量索引已构建（每 scope 每进程一次）。

    复用 state.py 写路径同款 rebuild_if_needed；模型缺失 / 被禁用时静默跳过
    （embedding bucket 本就返空，不影响 keyword / v2 bucket）。"""
    import os
    if os.getenv("HARNESS_DISABLE_SEMANTIC_DEDUP") == "1":
        return
    scope_key = f"project:{state.project_id}" if getattr(state, "project_id", None) else "org"
    if _RECALL_INDEX_WARMED.get(scope_key):
        return
    try:
        from core.embeddings import get_default_embedding_client
        from core.kb_vector_index import rebuild_if_needed
        client = get_default_embedding_client()
        rebuild_if_needed("org", client=client)
        if getattr(state, "project_id", None):
            rebuild_if_needed("project", state.project_id, client=client)
        _RECALL_INDEX_WARMED[scope_key] = True
    except Exception as e:
        log.debug("recall index warm skipped: %s", e)


def _try_embed_query(query: str):
    """返 normalized embedding vector 或 None（不可用时）。"""
    import os
    if os.getenv("HARNESS_DISABLE_SEMANTIC_DEDUP") == "1":
        return None
    try:
        import numpy as np
        from core.embeddings import get_default_embedding_client
        client = get_default_embedding_client()
        # EmbeddingClient.embed(texts) → np.ndarray shape=(n, dim)
        vec = client.embed([query.strip()])[0]
        # L2-normalize（kb_vector_index.query 假定 vectors 是 unit norm）
        n = float(np.linalg.norm(vec))
        if n == 0:
            return None
        return vec / n
    except Exception as e:
        log.debug("recall embedding unavailable: %s", e)
        return None


def _fill_claim_buckets(
    state, qvec, result, *,
    k_per_category: int, min_cosine: float,
    include_categories: tuple[str, ...],
):
    """从 claims index 查 top-k，按 claim_type / status 分类填 3 个 bucket。"""
    try:
        from core.kb_vector_index import query_across_scopes
        # 多取一些（k*5 covers 3 个 bucket 的总额）然后 categorize
        hits = query_across_scopes(
            "claims", qvec, top_k=k_per_category * 5,
            project_id=state.project_id, min_cosine=min_cosine,
        )
    except Exception as e:
        log.debug("recall claim query failed: %s", e)
        return

    for kid, sim, scope in hits:
        claim = state.get_kb_record("claims", kid)
        if claim is None:
            continue
        ct = claim.get("claim_type")
        st = claim.get("status")

        if ct == "dead_end" and "active_dead_ends" in include_categories:
            if len(result.active_dead_ends) < k_per_category:
                result.active_dead_ends.append({
                    "claim_id": kid,
                    "claim_text": (claim.get("claim_text") or "")[:200],
                    "dont_repeat_reason": (claim.get("dont_repeat_reason")
                                            or "")[:200],
                    "scope": scope,
                    "similarity": round(sim, 3),
                    "created_at": claim.get("created_at"),
                })
        # 原来还收 "theoretical" —— 类型已随 10→5 收敛并归一成 empirical；
        # validated_methods 收的是"怎么做"的结论，本来就该只有 methodological。
        elif (ct == "methodological"
              and st == "validated"
              and (claim.get("replication_count") or 0) >= 1
              and "validated_methods" in include_categories):
            if len(result.validated_methods) < k_per_category:
                result.validated_methods.append({
                    "claim_id": kid,
                    "claim_text": (claim.get("claim_text") or "")[:200],
                    "claim_type": ct,
                    "confidence": claim.get("confidence"),
                    "replication_count": claim.get("replication_count", 0),
                    "scope": scope,
                    "similarity": round(sim, 3),
                    "created_at": claim.get("created_at"),
                })


def _fill_prior_runs(state, qvec, result, *, k_per_category: int,
                      min_cosine: float):
    """从 chunks index 找跨项目研究产物 chunk。group by origin_run_id。

    研究产物：origin artifact 是 manuscript / analysis_report / survey_report /
    experiment_log / pre_registration / research_plan 之一。
    """
    try:
        from core.kb_vector_index import query_across_scopes
        # k 大些（拿了再 filter + dedup by run）
        hits = query_across_scopes(
            "chunks", qvec, top_k=k_per_category * 4,
            project_id=state.project_id, min_cosine=min_cosine,
        )
    except Exception as e:
        log.debug("recall chunk query failed: %s", e)
        return

    seen_runs: set[str] = set()
    for kid, sim, scope in hits:
        chunk = state.get_kb_record("chunks", kid)
        if chunk is None:
            continue
        origin_run = chunk.get("origin_run_id") or chunk.get("created_by_run_id")
        if not origin_run:
            continue
        # 跳过当前 run（不算 prior）
        if origin_run == state.run_id:
            continue
        # 按 origin_run dedup（每 run 只显示最相关的一个 chunk）
        if origin_run in seen_runs:
            continue
        seen_runs.add(origin_run)

        # 判断 origin artifact 是不是研究产物
        # chunk.source / chunk.metadata / chunk.origin_artifact_id 都可能含线索
        # 简化：text 前 60 字 + 来源 + 相似度即可（让 LLM 自己判断）
        text_preview = (chunk.get("text") or "")[:300].replace("\n", " ")
        result.prior_runs.append({
            "chunk_id": kid,
            "origin_run_id": origin_run,
            "origin_artifact_id": chunk.get("origin_artifact_id"),
            "created_by_node_type": chunk.get("created_by_node_type"),
            "created_at": chunk.get("created_at"),
            "scope": scope,
            "similarity": round(sim, 3),
            "text_preview": text_preview,
        })
        if len(result.prior_runs) >= k_per_category:
            break


def _dedup_by_text(records: list[dict]) -> list[dict]:
    """按 normalize(text) 去重（同一观察 legacy + v2 可能各一份），保留先出现的。"""
    seen: set[str] = set()
    out: list[dict] = []
    for m in records:
        key = " ".join((m.get("text") or "").lower().split())
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(m)
    return out


def _iso_to_ts(iso: str | None) -> float | None:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


def _age_label(iso: str | None) -> str | None:
    """ISO 时间 → 「今天 / N 天前」，供 briefing 给每条召回标年龄。

    为什么是人话不是 ISO 原文：模型对日期算术不可靠 ——「47 天前」能直接
    触发"这条可能过时了"的推理，`2026-05-15T02:41:36+00:00` 大概率被当
    装饰跳过（CC 对记忆召回做同款标注，源码注释给的就是这个理由）。

    算不出来（缺字段 / 格式坏 / 时钟漂移写出未来时间）返回 None，渲染层
    直接不标 —— 错的年龄比没有年龄更糟。
    """
    ts = _iso_to_ts(iso)
    if ts is None:
        return None
    days = int((datetime.now(timezone.utc).timestamp() - ts) // 86400)
    if days < 0:
        return None
    if days == 0:
        return "今天"
    return f"{days} 天前"


# ─────────────────────────────────────────────────────────────────────────────
# render: RecallResult → markdown briefing
# ─────────────────────────────────────────────────────────────────────────────


def _fill_load_bearing_capital(state, result, *, cap: int | None = None) -> None:
    """知识卡草稿注入（含 practice —— 那才是给决策看的部分）。

    不做 embedding 过滤："这条是否与当前 query 字面相关"本就不该决定它是否
    参与方向性判断。预算在**注入侧**（HARNESS_CARD_DRAFT_BUDGET，默认 12）：
    取最近 N 张，多出的只报个数 —— 起草侧不设配额（被拒的草稿连候选都当不成）。

    2026-08-21：数据源从 `load_bearing` 布尔位改成 `card_draft`。原来这是两套
    并行记账（承重层的 decision_relevance「会改变哪类选择」和知识卡的 practice
    「据此该怎么做」是同一个问题），合并后草稿既是本项目的决策依据，
    也是终态晋升的候选卡。
    """
    try:
        claims = state.list_kb("claims")
    except Exception as e:
        log.debug("card drafts unavailable: %s", e)
        return
    lb = [c for c in claims
          if isinstance(c.get("card_draft"), dict) and c.get("card_draft")
          and c.get("status") not in ("superseded", "refuted")]

    def _drafted_at(c) -> str:
        hist = c.get("card_draft_history") or []
        return str(hist[-1].get("at") or "") if hist else ""

    lb.sort(key=_drafted_at, reverse=True)
    if cap is None:
        import os as _os
        from shared.lib.kb_schema import CARD_DRAFT_BUDGET_DEFAULT
        cap = int(_os.getenv("HARNESS_CARD_DRAFT_BUDGET") or CARD_DRAFT_BUDGET_DEFAULT)
    result.card_drafts_not_injected = max(0, len(lb) - cap)
    for c in lb[:cap]:
        draft = c.get("card_draft") or {}
        result.load_bearing_capital.append({
            "drafted_at": _drafted_at(c) or c.get("created_at"),
            "claim_id": c.get("id"),
            "claim_text": (draft.get("statement")
                           or c.get("claim_text") or "")[:220],
            "decision_relevance": (draft.get("practice") or "")[:260],
            "why": (draft.get("why") or "")[:200],
            "domain": draft.get("domain"),
            "claim_type": c.get("claim_type"),
            "status": c.get("status"),
            "confidence": c.get("confidence"),
            "scope_dimensions": draft.get("applicability")
                                or c.get("scope_dimensions") or {},
        })


def _fill_prior_failed_checks(state, result, *, max_scan: int = 400) -> None:
    """同 node_type + 同 project 最近一次没交出必需产物的 run。

    口径全在 core.run_history。**修了一处旧的跨项目泄漏**：旧代码是
    `if self_proj is not None and s.get("project_id") != self_proj` —— self_proj
    为 None（anon state，例如 `run_node.py --harness X` 不带 `--project`）时根本
    不过滤，会把**所有项目**的失败记录读进 briefing。这跟 PR#99 那个 89% 跨课题
    污染是同一类。权威层一律严格相等（None 只匹配 None）。

    2026-08-23：渲染面原来还会遍历一个 `failed_checks` 键列出"未通过的 QC"。
    QC 判定层 #627 删除后**没有任何人再写那个键**，于是那段循环永远空转，而
    标题照旧写着"未通过以下 QC" —— 一句恒假的话挂在重试场景最显眼的位置。
    现在标题说的就是它真正知道的那件事：上一次没交出必需产物。
    """
    from core import run_history

    root = getattr(state, "root", None)
    if root is None:
        return
    for r in run_history.load_runs(
            root.parent, project_id=getattr(state, "project_id", None),
            exclude_run_id=getattr(state, "run_id", None),
            node_type=getattr(state, "node_type", None), limit=max_scan):
        if not r.missing_required_outputs:
            continue
        result.prior_failed_checks.append({
            "run_id": r.run_id,
            "status": r.status,
            "missing_required_outputs": list(r.missing_required_outputs),
        })
        return   # 只要最近一次


def render_briefing(result: RecallResult, *, query: str = "") -> str:
    """RecallResult → 给 LLM 看的结构化 markdown briefing。

    每段都标注 source（claim_id / chunk_id / memory_id）便于溯源，避免 LLM 编。
    """
    lines: list[str] = []
    lines.append("📚 **BRIEFING（自动）—— 基于 KB 你过去做过的类似工作**")
    if query:
        lines.append(f"_query: {query[:200]}_")
    lines.append("")

    if not result.total_items():
        lines.append("_（KB 尚无相关历史；各 bucket 全空。这是首次跑这个方向的项目，"
                      "或之前的 KB 与当前 query 无强相关。正常做即可。）_")
        return "\n".join(lines)

    # -2. 承重资本（决策依据，最该先看 —— 它存在的意义就是改变你的方向选择）
    if result.load_bearing_capital:
        lines.append(
            f"🏛️ **知识卡草稿（{len(result.load_bearing_capital)} 条）"
            f"—— 本项目花真实算力换来的决策依据，设计方案前先读。"
            f"它们在终态会成为晋升候选**"
        )
        for c in result.load_bearing_capital:
            age = _age_label(c.get("drafted_at"))
            lines.append(
                f"  ▸ [{c['claim_id']}｜{c.get('claim_type')}｜conf={c.get('confidence')}"
                + (f"｜{age}" if age else "")
                + f"] {c['claim_text']}"
            )
            if c.get("why"):
                lines.append(f"     → 机制：{c['why']}")
            if c.get("decision_relevance"):
                lines.append(f"     → 据此该怎么做：{c['decision_relevance']}")
            if c.get("scope_dimensions"):
                lines.append(f"     → 适用范围：{c['scope_dimensions']}")
        if result.card_drafts_not_injected:
            lines.append(f"  （另有 {result.card_drafts_not_injected} 张草稿未注入，"
                         f"search_kb(entity_type='claims') 可查）")
        lines.append(
            "  （若你的设计与上述结论冲突，必须在产出里显式说明理由；"
            "若确实无可用资本，也要说明"
            "「已检索承重层，无相关结论」而不是默认忽略。）"
        )
        lines.append("")

    # -1. prior_failed_checks（同 node_type 上一次失败挂在哪；重试场景最关键，放最前）
    if result.prior_failed_checks:
        for pf in result.prior_failed_checks:
            lines.append(
                f"🚨 **上一个同类型 run（{pf['run_id']}，status={pf.get('status')}）"
                f"没交出必需产物 —— 本轮先解决它，不要原样重跑：**"
            )
            lines.append(
                f"  ✗ 缺必需产物: {', '.join(pf['missing_required_outputs'])}"
            )
        lines.append("")

    # 1. prior_runs —— 仅在非空时渲染（当前 bucket 已停用，见 recall() 第 3 段；
    #    保留渲染分支以便 chunks 建索引后重新启用，不再打印恒空的"找到 0 条"误导段）
    if result.prior_runs:
        lines.append(
            f"🎯 **类似 prior runs**（embedding similarity；找到 {len(result.prior_runs)} 条）"
        )
        for r in result.prior_runs:
            lines.append(
                f"  • [run={r['origin_run_id'][:24]}…, node={r.get('created_by_node_type','?')}, "
                f"sim={r['similarity']}] {r['text_preview'][:200]}"
            )
        lines.append("")

    # 2. dead_ends
    lines.append(
        f"⚠️ **此领域 active dead_ends**（org-scope，{len(result.active_dead_ends)} 条）"
    )
    if result.active_dead_ends:
        for d in result.active_dead_ends:
            age = _age_label(d.get("created_at"))
            lines.append(
                f"  • [{d['claim_id']}, sim={d['similarity']}"
                + (f", {age}" if age else "")
                + f"] {d['claim_text']}"
            )
            if d["dont_repeat_reason"]:
                lines.append(f"    └ avoid: {d['dont_repeat_reason']}")
    else:
        lines.append("  _（无强相关 dead_end，可能是此方向首次踩或 KB 尚未沉淀）_")
    lines.append("")

    # 3. validated_methods
    lines.append(
        f"📐 **高置信 methodological claim**"
        f"（replication ≥ 1，{len(result.validated_methods)} 条）"
    )
    if result.validated_methods:
        for v in result.validated_methods:
            age = _age_label(v.get("created_at"))
            lines.append(
                f"  • [{v['claim_id']}, sim={v['similarity']}, "
                f"conf={v['confidence']}, rep={v['replication_count']}"
                + (f", {age}" if age else "")
                + f"] {v['claim_text']}"
            )
    else:
        lines.append("  _（无）_")
    lines.append("")

    # 4–6（open questions / directive / observation）已删：前者判据引用了
    # schema 里不存在的 claim_type="question"，从 v3 合并起恒空，却每次照常
    # 打印一段"（0 条）"—— 把"检索器没在找"讲成"KB 里没东西"。
    # 后两者是项目记忆，已归 core.memory_delivery 按适用面机械送达。

    lines.append("")
    lines.append(
        "_使用建议：以上信息**已经过相似度筛选 + 来源标注**。决策前看一眼相关"
        "dead_end + validated_method 能省大量试错。所有 claim_id / chunk_id "
        "可用 search_kb / get_kb_record 取详情。_"
    )
    lines.append("")
    lines.append(
        "⚠️ **这些是当时的记录，不是现状**：召回说 X 存在 ≠ X 现在还存在/"
        "还成立。要把某条**当事实引用**（写进产出、据此设计方案）时 —— 尤其"
        "是标着「N 天前」的 —— 先用 search_kb / get_kb_record 验证它没被"
        " supersede、没被 refute，涉及文件路径的用 read_file 确认还在，"
        "再落笔。"
    )
    return "\n".join(lines)

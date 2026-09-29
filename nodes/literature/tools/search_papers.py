"""search_papers —— 多源论文检索（Crossref + arXiv + bioRxiv + medRxiv + S2 + OpenAlex + PubMed + CNKI）
注册后 llm 可自主调。"""

from __future__ import annotations

import time
from typing import Any

from core.state import State
from core.tool_registry import ToolDefinition, register_tool

from .filter import compute_quality_score, compute_relevance_score, quality_score_breakdown
from .search_engines import Paper, SearchManager

# 搜索结果可以很多，但不应把所有候选直接塞入 LLM 上下文。完整结果仍
# 保存在 SearchManager 的本地缓存；这里只控制工具返回给模型的候选量。
MODEL_VISIBLE_PER_CALL = 30
MODEL_VISIBLE_PER_RUN = 120
MODEL_VISIBLE_DEDUP_FALLBACK = 5

_INDEX_REUSABLE_FIELDS = (
    "authors",
    "abstract",
    "year",
    "venue",
    "url",
    "pdf_url",
    "fields_of_study",
    "subjects",
    "pub_type",
    "pub_date",
    "keywords",
    "issn",
    "eissn",
    "nlm_id",
    "journal_abbr",
    "impact_factor",
)
_INDEX_CORE_FIELDS = ("title", "authors", "abstract", "year", "venue", "url")


def _paper_identity(paper: Paper) -> str:
    doi = str(paper.doi or "").strip().lower()
    if doi:
        return doi
    title = " ".join(str(paper.title or "").lower().split())
    return f"title:{title}" if title else ""


def _reuse_local_indexes(
    mgr: SearchManager, papers: list[Paper]
) -> tuple[set[str], set[str], dict]:
    """只按本轮候选身份复用 Index；本地库绝不增加候选。"""
    started = time.monotonic()
    hit_keys: set[str] = set()
    complete_keys: set[str] = set()
    local_index = getattr(mgr, "local_index", None)
    if not local_index or not hasattr(local_index, "get_papers_by_identity"):
        return (
            hit_keys,
            complete_keys,
            {
                "available": False,
                "candidate_count": len(papers),
                "hit_count": 0,
                "complete_hit_count": 0,
                "incomplete_hit_count": 0,
                "elapsed_seconds": round(time.monotonic() - started, 4),
            },
        )

    try:
        cached_by_key = local_index.get_papers_by_identity(papers)
    except Exception as exc:
        return (
            hit_keys,
            complete_keys,
            {
                "available": False,
                "candidate_count": len(papers),
                "hit_count": 0,
                "complete_hit_count": 0,
                "incomplete_hit_count": 0,
                "error_type": type(exc).__name__,
                "elapsed_seconds": round(time.monotonic() - started, 4),
            },
        )

    for paper in papers:
        key = _paper_identity(paper)
        cached = cached_by_key.get(key)
        if not key or cached is None:
            continue
        hit_keys.add(key)
        for field in _INDEX_REUSABLE_FIELDS:
            current = getattr(paper, field, None)
            cached_value = getattr(cached, field, None)
            if not current and cached_value:
                setattr(paper, field, cached_value)
                paper.metadata_provenance.setdefault(field, "local_index")
        paper.citations = max(
            int(getattr(paper, "citations", 0) or 0),
            int(getattr(cached, "citations", 0) or 0),
        )
        if all(getattr(paper, field, None) for field in _INDEX_CORE_FIELDS):
            complete_keys.add(key)

    return (
        hit_keys,
        complete_keys,
        {
            "available": True,
            "candidate_count": len(papers),
            "hit_count": len(hit_keys),
            "complete_hit_count": len(complete_keys),
            "incomplete_hit_count": len(hit_keys - complete_keys),
            "elapsed_seconds": round(time.monotonic() - started, 4),
        },
    )


def _year_int(value) -> int | None:
    try:
        return int(str(value)[:4])
    except (TypeError, ValueError):
        return None


async def _search_papers(
    state: State,
    query: str,
    max_results: int = 20,
    year_from: int | None = None,
    include_arxiv: bool | None = None,
    include_biorxiv: bool | None = None,
    include_medrxiv: bool | None = None,
    include_s2: bool | None = None,
    include_openalex: bool | None = None,
    include_pubmed: bool | None = None,
    include_crossref: bool | None = None,
    include_cnki: bool | None = None,
    crossref_query_mode: str = "full",
    **_: Any,
) -> dict:
    """从多源检索论文（arXiv + bioRxiv + medRxiv + S2 + OpenAlex + PubMed + Crossref + CNKI）。
    
    Use when: 新课题探索、多源对比。
    Do NOT use when: 只需单一来源（用对应专用工具）。
    """
    # query 非空由 parameters_schema 的 minLength=1 声明，派发口核取值。
    max_results = min(max(1, max_results), 100)

    source_switches = {
        "arxiv": include_arxiv,
        "biorxiv": include_biorxiv,
        "medrxiv": include_medrxiv,
        "semantic_scholar": include_s2,
        "openalex": include_openalex,
        "pubmed": include_pubmed,
        "crossref": include_crossref,
        "cnki": include_cnki,
    }
    if any(value is not None for value in source_switches.values()):
        # 兼容新增来源之前的调用：原有来源的省略项沿用历史默认开启；调用方
        # 不可能声明当时尚不存在的新来源，因此新增来源省略时不自动混入。
        legacy_sources = {"arxiv", "semantic_scholar", "openalex", "crossref", "cnki"}
        enabled_sources = {
            source
            for source, enabled in source_switches.items()
            if enabled is True or (enabled is None and source in legacy_sources)
        }
    else:
        # 无来源参数时，所有来源在同一层级并默认开启。
        enabled_sources = set(source_switches)

    # 所有 include_* 都关 → 参数错误，明确报错而不是"悄悄按默认全开"。
    # （issue #230：底层曾把空白名单当 falsy 回退成全部来源，"全关" 反而等于 "全开"）
    if not enabled_sources:
        return {"status": "error",
                "error": "至少要启用一个来源（include_arxiv / include_biorxiv / "
                         "include_medrxiv / include_s2 / include_openalex / "
                         "include_pubmed / include_crossref / include_cnki）"}

    try:
        mgr = SearchManager()
        # targeted_lookup 自动启用主题 + Crossref 题名双通道；调用方显式传 title
        # 时仍只走题名通道，普通搜索和 E2E 默认保持 full。
        effective_crossref_mode = crossref_query_mode
        if (effective_crossref_mode == "full" and state is not None
                and state.hook_state.get("_request_mode") == "targeted_lookup"):
            effective_crossref_mode = "dual"
        pipeline_started = time.monotonic()
        retrieval_started = time.monotonic()
        papers = await mgr.search_all(
            query,
            max_per_source=max_results,
            enabled_sources=enabled_sources,
            # 本轮远程检索决定候选；本地库只能按身份补字段，不能添加候选。
            include_local_catalog=False,
            use_query_cache=False,
            # 先召回、去重和预排，再补齐高价值候选。
            enrich_metadata=False,
            persist_results=False,
            **({"crossref_query_mode": effective_crossref_mode}
               if effective_crossref_mode != "full" else {}),
        )
        retrieval_seconds = round(time.monotonic() - retrieval_started, 4)
        # 审计信息要在排序/切片**之前**取：切片会退化成普通 list，属性就丢了
        audit = papers.audit_dict() if hasattr(papers, "audit_dict") else {}
        if year_from is not None:
            papers = [p for p in papers if not p.year or _year_int(p.year) is None
                      or _year_int(p.year) >= int(year_from)]

        _, complete_hit_keys, index_reuse = _reuse_local_indexes(mgr, papers)

        def score_and_sort(items: list[Paper]) -> None:
            for paper in items:
                relevance = compute_relevance_score(paper, query)
                paper.score = compute_quality_score(paper, relevance=relevance)
                # 仅 targeted dual/title 召回：题名通道命中是精确补查证据，
                # 给小幅稳定加权，防止被同一事件的宽泛主题结果挤掉。
                if (paper.metadata_provenance or {}).get("crossref_query_mode") == "title":
                    paper.score = min(1.0, paper.score + 0.035)
                paper.relevance_score = relevance
            items.sort(key=lambda paper: paper.score, reverse=True)

        # 预筛只排除靠后的 25%，降低补齐开销，同时保护“标题弱相关但摘要
        # 强相关”的论文。补齐后对全体候选重新评分。
        score_and_sort(papers)
        preliminary_keep_n = max(50, (len(papers) * 3 + 3) // 4)
        preliminary = papers[:min(preliminary_keep_n, len(papers))]

        attempted_enrichment_keys: set[str] = set()
        if state is not None:
            attempted_enrichment_keys = set(
                state.hook_state.get("_literature_enrichment_attempted_keys", [])
            )
        enrichment_targets = []
        for paper in preliminary:
            key = _paper_identity(paper)
            if (not key or key in complete_hit_keys
                    or key in attempted_enrichment_keys or not paper.doi):
                continue
            if any(not getattr(paper, field, None)
                   for field in _INDEX_CORE_FIELDS[1:]):
                enrichment_targets.append(paper)
                attempted_enrichment_keys.add(key)

        enrichment_started = time.monotonic()
        if enrichment_targets:
            await mgr._enrich_metadata_cross_source(
                enrichment_targets, set(enabled_sources)
            )
        enrichment_seconds = round(time.monotonic() - enrichment_started, 4)
        if state is not None:
            state.hook_state["_literature_enrichment_attempted_keys"] = sorted(
                attempted_enrichment_keys
            )

        score_and_sort(papers)

        # 全部远程候选统一写一次 Index；入库不会改变当前候选和排序。
        local_index = getattr(mgr, "local_index", None)
        index_persistence = {
            "attempted": bool(local_index and papers),
            "status": "skipped",
            "added_or_updated": 0,
            "skipped": 0,
        }
        if local_index and papers:
            try:
                added, skipped = local_index.add_papers(papers, query)
                index_persistence.update({
                    "status": "success",
                    "added_or_updated": int(added),
                    "skipped": int(skipped),
                })
            except Exception as exc:
                index_persistence.update({
                    "status": "error", "error_type": type(exc).__name__,
                })

        # 每次检索保留候选的 50%；若不足 50 篇则保留全部（等价于
        # “50% 后不到 50 篇就留下前 50 篇”，但不凭空制造结果）。
        keep_n = max(50, (len(papers) + 1) // 2)
        candidate_papers = papers[:min(keep_n, len(papers))]
        complete_preliminary_hits = sum(
            _paper_identity(paper) in complete_hit_keys for paper in preliminary
        )
        pipeline_timings = {
            "retrieval_seconds": retrieval_seconds,
            "index_reuse_seconds": index_reuse["elapsed_seconds"],
            "metadata_enrichment_seconds": enrichment_seconds,
            "total_seconds": round(time.monotonic() - pipeline_started, 4),
        }
        # 相关度/总分已经在全部候选上计算完成；只把小批量高分候选交给
        # LLM。跨多轮 search_papers 调用时，用 DOI/标题做运行级去重和总量
        # 上限，避免每轮 200+ 原始结果累积进上下文。完整候选仍由
        # SearchManager 写入本地缓存，不影响后续归档。
        seen_keys = set()
        if state is not None:
            seen_keys = set(state.hook_state.get("_literature_model_paper_keys", []))
        available = MODEL_VISIBLE_PER_RUN - len(seen_keys)
        model_visible_papers = []
        dedup_fallback_used = False
        if available > 0:
            for p in candidate_papers:
                key = (p.doi or "").strip().lower()
                if not key:
                    key = "title:" + " ".join((p.title or "").lower().split())
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                model_visible_papers.append(p)
                if len(model_visible_papers) >= min(MODEL_VISIBLE_PER_CALL, available):
                    break
            if state is not None:
                state.hook_state["_literature_model_paper_keys"] = sorted(seen_keys)

        # Landscape runs often issue focused follow-up queries after broad queries.
        # If every high-quality candidate was already shown once, returning zero
        # papers makes the model incorrectly treat the subtopic as empty. Return a
        # small audit-marked overlap sample instead; do not add it to seen_keys.
        if not model_visible_papers and candidate_papers:
            dedup_fallback_used = True
            model_visible_papers = candidate_papers[:MODEL_VISIBLE_DEDUP_FALLBACK]

        result = []
        for p in model_visible_papers:
            result.append({
                "title": p.title or "",
                "authors": ", ".join(p.authors[:5]) if p.authors else "",
                "year": p.year or "",
                "doi": p.doi or "",
                "url": p.url or "",
                "venue": p.venue or "",
                "source": getattr(p, 'source', '') or "",
                "abstract": (p.abstract or "")[:500],
                "citations": p.citations or 0,
                "quality_score": round(p.score, 4),
                "score_breakdown": quality_score_breakdown(p, getattr(p, "relevance_score", 0.0)),
                "index_completeness": {
                    field: bool(getattr(p, field, ""))
                    for field in ("title", "authors", "year", "doi", "url", "abstract", "venue", "source")
                },
                "metadata_provenance": dict(getattr(p, "metadata_provenance", {}) or {}),
            })

        # 来源审计（issue #230）：让 LLM 能自己看出"我要的源到底搜了没、结果来自哪"，
        # 而不是拿到 20 条就以为白名单生效了。returned_sources 从**切片后的实际返回**
        # 现算，跟 papers 字段一定对得上。
        returned_sources = sorted({p["source"].split("/", 1)[0].lower()
                                   for p in result if p.get("source")})
        returned_source_counts = {}
        for p in result:
            src = (p.get("source") or "").split("/", 1)[0].lower() or "<empty>"
            returned_source_counts[src] = returned_source_counts.get(src, 0) + 1
        return {"status": "success", "query": query,
                "requested_query_mode": effective_crossref_mode,
                "total": len(result),
                "candidate_count": len(candidate_papers),
                "model_visible_count": len(model_visible_papers),
                "model_visible_per_call_limit": MODEL_VISIBLE_PER_CALL,
                "model_visible_per_run_limit": MODEL_VISIBLE_PER_RUN,
                "dedup_fallback_used": dedup_fallback_used,
                "dedup_fallback_count": len(model_visible_papers) if dedup_fallback_used else 0,
                "candidate_truncated_for_context": len(candidate_papers) > len(model_visible_papers),
                "run_level_dedup_applied": state is not None,
                "papers": result,
                "requested_sources": audit.get("requested_sources", sorted(enabled_sources)),
                "attempted_sources": audit.get("attempted_sources", []),
                "unavailable_sources": audit.get("unavailable_sources", []),
                "returned_sources": returned_sources,
                "raw_source_counts": audit.get("raw_source_counts", {}),
                "deduped_source_counts": audit.get("deduped_source_counts", {}),
                "returned_source_counts": returned_source_counts,
                "source_diagnostics": audit.get("source_diagnostics", {}),
                "from_cache": audit.get("from_cache", False),
                "warnings": audit.get("warnings", []),
                "index_reuse": index_reuse,
                "index_persistence": index_persistence,
                "metadata_enrichment": {
                    "policy": "post_dedup_high_value_candidates_only",
                    "target_count": len(enrichment_targets),
                    "skipped_complete_cache_hits": complete_preliminary_hits,
                    "run_level_repeat_suppression": state is not None,
                    "elapsed_seconds": enrichment_seconds,
                },
                "pipeline_timings": pipeline_timings,
                "query_result_cache_used": False,
                "local_catalog_added_candidates": False,
                }

    except Exception as e:
        return {"status": "error", "error": f"搜索失败: {type(e).__name__}: {str(e)[:200]}"}


register_tool(
    ToolDefinition(
        name="search_papers",
        description=(
            "从多源（arXiv + bioRxiv + medRxiv + S2 + OpenAlex + PubMed + Crossref + CNKI）检索学术论文，"
            "自动去重、按综合质量分排序；候选由本轮远程结果决定，本地 Index "
            "只按 DOI/标题复用字段，不会把历史论文塞进当前结果。元数据补齐只作用于"
            "预筛后的高价值候选。\n\n"
            "**Use when**：\n"
            "  - 探索新课题，想看多源结果\n"
            "  - 需要完整元数据（标题/作者/DOI/摘要/期刊）\n\n"
            "**Do NOT use when**：\n"
            "  - 只想要单一来源 → 还是用本工具，把其它 include_* 设 false"
            "（关掉的源不会混进结果，见返回里的 returned_sources）\n\n"
            "**语言（重要）**：本工具**不做任何翻译**，传什么就搜什么。arXiv/S2/"
            "OpenAlex/Crossref 是英文库，CNKI 是中文库。要跨语言覆盖就你自己出词、"
            "分两次调用——一次传英文词搜国际库，一次传中文词搜 CNKI。\n\n"
            "**关键参数**：\n"
            "  - query: 检索词（英文搜国际库 / 中文搜 CNKI，工具不代翻译）\n"
            "  - max_results: 1-100，默认 20\n"
            "  - crossref_query_mode: full（默认主题检索）或 title（定向补查时按题名/事件词检索 Crossref）\n"
            "  - include_*: 控制各来源开关（关掉的源**不会**出现在结果里；"
            "全部关掉是参数错误，不会回退成全开）\n\n"
            "**返回**：papers 列表（按 quality_score 降序）+ 来源审计字段：\n"
            "  - requested_sources / attempted_sources / unavailable_sources / returned_sources\n"
            "  - index_reuse / metadata_enrichment / pipeline_timings：复用、补齐与阶段耗时\n"
            "  - query_result_cache_used 固定为 false：查询结果不缓存，只复用论文 Index\n"
            "  - warnings: 机器可读告警（如 CNKI 未配置 cookie → 零结果而非用别的源顶替）\n"
            "  被关掉的源一篇都不会返回；要的源不可用时是零结果 + warning，"
            "看到 total=0 先读 unavailable_sources / warnings 再决定换词还是换源。"
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "minLength": 1,
                    "description": "检索关键词。工具不翻译：英文词搜国际库，中文词搜 CNKI。",
                },
                "max_results": {
                    "type": "integer",
                    "default": 20,
                    "minimum": 1,
                    "maximum": 100,
                },
                "crossref_query_mode": {
                    "type": "string",
                    "enum": ["full", "title"],
                    "default": "full",
                    "description": "仅定向补查使用 title；普通检索保持 full。",
                },
                "year_from": {
                    "type": "integer",
                    "description": "可选：只取这一年及以后发表的；年份缺失的条目保留。",
                },
                "include_arxiv": {"type": "boolean", "default": True, "description": "含 arXiv"},
                "include_biorxiv": {
                    "type": "boolean",
                    "default": True,
                    "description": "含 bioRxiv 预印本",
                },
                "include_medrxiv": {
                    "type": "boolean",
                    "default": True,
                    "description": "含 medRxiv 预印本",
                },
                "include_s2": {
                    "type": "boolean",
                    "default": True,
                    "description": "含 Semantic Scholar",
                },
                "include_openalex": {
                    "type": "boolean",
                    "default": True,
                    "description": "含 OpenAlex",
                },
                "include_pubmed": {
                    "type": "boolean",
                    "default": True,
                    "description": "含 PubMed 生物医学文献",
                },
                "include_crossref": {
                    "type": "boolean",
                    "default": True,
                    "description": "含 Crossref",
                },
                "include_cnki": {"type": "boolean", "default": True, "description": "含 CNKI 知网"},
            },
            "required": ["query"],
        },
        allowed_node_types=["literature"],
        risk_level="low",
    ),
    _search_papers,
)

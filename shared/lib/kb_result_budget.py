"""KB 读取工具的有界投影 + token 预算（框架级不变量，2026-07）。

第一性原理：KB 是**无界增长**的存储；LLM context 是**有界稀缺**的资源。
两者之间的每个读取工具，其**输出大小必须与 KB 总量无关**——一个输出随 KB
规模 O(N) 增长的工具在架构上就是坏的，迟早撑爆任何 context 窗口。

v10c dogfood 实测根因：curator dreaming 一次 `search_kb(claims, limit=200)`
返回 159 条**全量** claim record = 333,875 字符 ≈ 83k tokens 塞进单条 tool
result，直接把 context 顶到 192k、触发暴力压缩、压不动、白烧 LLM。

解法（"find 与 read 分离"，像人用搜索引擎：先看一列标题+摘要，再点开少数
几条读全文，绝不下载整个索引）：
  - find/scan（哪些记录匹配？）→ 返回**有界的轻量投影**（id + 一行摘要 +
    过滤/排序用的那几个字段），永远不灌全文；受 token 预算硬顶。
  - read（这一条全文？）→ get_kb_record(id)，一次一条、全保真、O(1)。

关键：这是**信息保全**的——N 条匹配仍报告 total_matched=N，超预算时只是少
返回几条 record 并置 truncated=True + hint，让 LLM 收窄 filter 或逐条
get_kb_record，而不是把 KB 全文一次性倒进 context。
"""
from __future__ import annotations

import json
from typing import Any, Callable

# 单次工具结果的默认 token 预算。~12k tokens ≈ 48k 字符：足够放几十条摘要行，
# 又远低于任何合理 context 窗口的一小格，保证单条 tool result 不会独吞上下文。
DEFAULT_RESULT_TOKEN_BUDGET = 12_000

# 摘要投影里长文本字段的截断长度（字符）。
_TEXT_PREVIEW_CHARS = 200


def _clip(s: Any, n: int = _TEXT_PREVIEW_CHARS) -> str:
    text = "" if s is None else str(s)
    return text if len(text) <= n else text[:n] + "…"


def summarize_kb_record(entity_type: str, r: dict) -> dict:
    """把一条 KB record 投影成轻量摘要行（几十~两百字符），保留 id + 判断/
    定位用的关键字段。想看全文用 get_kb_record(entity, id)。"""
    base = {
        "id": r.get("id"),
        "scope": r.get("scope"),
        "status": r.get("status"),
    }
    if entity_type == "claims":
        base.update({
            "claim_type": r.get("claim_type"),
            "confidence": r.get("confidence"),
            "claim_text": _clip(r.get("claim_text")),
            "n_sources": len(r.get("sources") or []),
            "n_concepts": len(r.get("concept_ids") or []),
            "n_review_flips": len(r.get("review_history") or []),
        })
    elif entity_type == "concepts":
        base.update({
            "concept_type": r.get("concept_type"),
            "canonical_name": _clip(r.get("canonical_name"), 120),
            "n_aliases": len(r.get("aliases") or []),
            "usage_count": r.get("usage_count"),
        })
    elif entity_type == "experiments":
        base.update({
            "outcome": r.get("outcome"),
            "experiment_text": _clip(r.get("experiment_text") or r.get("setup_text")),
            "n_produced_claims": len(r.get("produced_claim_ids") or []),
        })
    elif entity_type == "chunks":
        base.update({
            "source": _clip(r.get("source"), 120),
            "text": _clip(r.get("text"), 160),
            "n_authors": len(r.get("author_concept_ids") or []),
        })
    return base


def _row_chars(row: dict) -> int:
    return len(json.dumps(row, ensure_ascii=False))


def budget_rows(
    rows: list[dict],
    *,
    max_tokens: int = DEFAULT_RESULT_TOKEN_BUDGET,
    projector: Callable[[dict], dict] | None = None,
) -> tuple[list[dict], bool]:
    """按 token 预算截断一批（已投影或原始）rows。

    projector：可选，先对每行投影再计费（search_kb 的 full 模式超预算时用它
    自动降级）。返回 (kept_rows, truncated)。char≈token×4 粗估，无 tiktoken
    依赖——这里只需要"别爆"，不需要精确计费。
    """
    char_budget = max_tokens * 4
    kept: list[dict] = []
    used = 0
    truncated = False
    for row in rows:
        projected = projector(row) if projector else row
        c = _row_chars(projected) + 2  # +2 逗号/换行开销
        if kept and used + c > char_budget:
            truncated = True
            break
        kept.append(projected)
        used += c
    return kept, truncated


def budget_kb_search(
    records: list[dict],
    entity_type: str,
    *,
    projection: str = "summary",
    max_tokens: int = DEFAULT_RESULT_TOKEN_BUDGET,
) -> dict:
    """search_kb 的统一返回构造：投影 + token 预算，保证输出与 KB 总量无关。

    projection:
      - "summary"（默认）：每条投影成摘要行（summarize_kb_record）。
      - "full"：返回原始 record；但若全量载荷超 max_tokens，**自动降级**为
        summary 并置 downgraded_to_summary=True + hint，绝不让单条结果爆上下文。

    返回统一 envelope：
      {status, entity_type, projection, total_matched, returned, truncated,
       records, hint?}
    total_matched = 过滤后命中的全部条数（信息保全，不因预算丢失计数）。
    """
    total_matched = len(records)

    if projection == "full":
        kept, truncated = budget_rows(records, max_tokens=max_tokens)
        if truncated:
            # full 模式超预算 → 降级为 summary（信息保全：所有匹配仍以摘要呈现）
            summary_rows = [summarize_kb_record(entity_type, r) for r in records]
            kept, truncated = budget_rows(summary_rows, max_tokens=max_tokens)
            return {
                "status": "success",
                "entity_type": entity_type,
                "projection": "summary",
                "downgraded_from": "full",
                "total_matched": total_matched,
                "returned": len(kept),
                "truncated": truncated,
                "records": kept,
                "hint": (
                    f"全量 record 超单次结果预算（{max_tokens} tok），已自动降级为"
                    f" summary 投影。要看某条全文用 get_kb_record(entity, id)；"
                    f"要缩小结果集加 filter（claim_type/status_filter/confidence"
                    f"_min/query 等）。"
                    + ("" if not truncated else
                       f" 摘要仍超预算，只返回前 {len(kept)}/{total_matched} 条。")
                ),
            }
        return {
            "status": "success",
            "entity_type": entity_type,
            "projection": "full",
            "total_matched": total_matched,
            "returned": len(kept),
            "truncated": False,
            "records": kept,
        }

    # summary（默认）
    rows, truncated = budget_rows(
        records, max_tokens=max_tokens,
        projector=lambda r: summarize_kb_record(entity_type, r),
    )
    out = {
        "status": "success",
        "entity_type": entity_type,
        "projection": "summary",
        "total_matched": total_matched,
        "returned": len(rows),
        "truncated": truncated,
        "records": rows,
    }
    if truncated:
        out["hint"] = (
            f"命中 {total_matched} 条，摘要超单次结果预算（{max_tokens} tok），"
            f"只返回前 {len(rows)} 条。加 filter 收窄，或对具体 id 用 "
            f"get_kb_record 看全文。"
        )
    elif projection == "summary":
        out["hint"] = "这是 summary 投影（每条只有摘要）。看某条全文用 get_kb_record(entity, id)。"
    return out

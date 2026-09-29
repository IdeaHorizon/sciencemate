"""KB record → embedding 用文本 —— per-claim_type template。

设计原则：
  1. claim_type 作 prefix → 让 embedder 区分 modality（empirical vs hypothesis 即使
     text 字面同也不该 collapse）
  2. concept_ids 解 hash → canonical_name + concept_type
  3. 关键 scope_dimensions 显式拼（dataset / regime / metric / sample_size）—— 影响
     semantic 的核心 attributes
  4. less essential 字段不进（confidence/status/created_at 随时间变但 semantic 不变）
  5. versioned constant —— 改 template 触发 rebuild_all
  6. 长度控制 < 250 token（multilingual-e5-small max 512）

**改 template 必须 ++ CLAIM_EMBED_TEMPLATE_VERSION，否则 manifest 不会触发 rebuild。**
"""
from __future__ import annotations

from typing import Callable

# ──────────────────────────────────────────────────────────────────────────
# Version constant —— 改任何 template 都 ++ 这个
# ──────────────────────────────────────────────────────────────────────────
CLAIM_EMBED_TEMPLATE_VERSION = 1
CONCEPT_EMBED_TEMPLATE_VERSION = 1


# ── Concept lookup helper ──────────────────────────────────────────────────

# Caller 必须提供 concept_lookup(concept_id) -> dict | None
# 返 {canonical_name, concept_type, description, ...} 或 None
ConceptLookup = Callable[[str], dict | None]


def _format_concepts(concept_ids: list[str], lookup: ConceptLookup,
                       max_n: int = 6) -> str:
    """concept_ids → 'GAP (method), QM9 (dataset), OOD generalization (phenomenon)'

    不存在的 concept 跳过。max_n 限制长度。
    """
    if not concept_ids:
        return ""
    parts: list[str] = []
    for cid in concept_ids[:max_n]:
        c = lookup(cid)
        if c is None:
            continue
        name = c.get("canonical_name") or cid
        ctype = c.get("concept_type") or "?"
        parts.append(f"{name} ({ctype})")
    return ", ".join(parts)


def _format_scope(scope_dimensions: dict | None) -> str:
    """scope_dimensions dict → 'dataset=QM9 regime=OOD metric=MAE sample_size=1000'

    固定 enumerate 顺序，None / 空跳过，避免噪音。
    """
    if not scope_dimensions:
        return ""
    # 固定顺序（核心 attributes 影响 claim semantic）
    keys_in_order = (
        "dataset", "regime", "split", "sample_size",
        "metric", "task", "domain", "hardware",
    )
    parts: list[str] = []
    for k in keys_in_order:
        v = scope_dimensions.get(k)
        if v is None or v == "":
            continue
        parts.append(f"{k}={v}")
    # 剩下没列举的字段也拼上（forward-compat）
    extra_keys = [k for k in scope_dimensions
                   if k not in keys_in_order and scope_dimensions[k]]
    for k in extra_keys[:3]:
        parts.append(f"{k}={scope_dimensions[k]}")
    return " ".join(parts)


def _truncate(text: str, max_chars: int = 800) -> str:
    """对超长字段截断，避免单 field 吃掉所有 token 预算。"""
    if not text:
        return ""
    t = text.strip()
    if len(t) <= max_chars:
        return t
    return t[: max_chars - 3] + "..."


# ── Per claim_type templates ───────────────────────────────────────────────

def _tpl_empirical(c: dict, lookup: ConceptLookup) -> str:
    return (
        f"[empirical claim]\n"
        f"text: {_truncate(c.get('claim_text', ''))}\n"
        f"about: {_format_concepts(c.get('concept_ids') or [], lookup)}\n"
        f"scope: {_format_scope(c.get('scope_dimensions'))}"
    )


def _tpl_methodological(c: dict, lookup: ConceptLookup) -> str:
    return (
        f"[methodological claim]\n"
        f"recipe: {_truncate(c.get('claim_text', ''))}\n"
        f"about: {_format_concepts(c.get('concept_ids') or [], lookup)}\n"
        f"scope: {_format_scope(c.get('scope_dimensions'))}"
    )


def _tpl_hypothesis(c: dict, lookup: ConceptLookup) -> str:
    """hypothesis 的核心是 falsification —— predicted + criteria 都进。"""
    fc_text = c.get("falsification_criteria_text") or ""
    fc_struct = c.get("falsification_criteria_structured") or {}
    if fc_struct:
        metric = fc_struct.get("metric", "?")
        op = fc_struct.get("comparison") or fc_struct.get("op", "?")
        thr = fc_struct.get("threshold", "?")
        dataset = fc_struct.get("dataset", "")
        struct_str = f"{metric} {op} {thr}"
        if dataset:
            struct_str += f" on {dataset}"
    else:
        struct_str = ""
    predicted = c.get("predicted_outcome") or ""
    return (
        f"[hypothesis claim]\n"
        f"hypothesis: {_truncate(c.get('claim_text', ''))}\n"
        f"predicted_outcome: {_truncate(predicted, 300)}\n"
        f"falsification: {struct_str}{' | ' + _truncate(fc_text, 200) if fc_text else ''}\n"
        f"about: {_format_concepts(c.get('concept_ids') or [], lookup)}"
    )


def _tpl_synthesis(c: dict, lookup: ConceptLookup,
                    claim_lookup: Callable[[str], dict | None] | None = None) -> str:
    """synthesis 的语义来自 sources —— 展开 source claim 前 100 字。"""
    src_ids = c.get("source_claim_ids") or c.get("sources") or []
    src_summaries: list[str] = []
    if claim_lookup is not None:
        for sid in src_ids[:5]:
            sc = claim_lookup(sid)
            if sc is None:
                continue
            text = _truncate(sc.get("claim_text", ""), 120)
            src_summaries.append(f"- {text}")
    src_block = "\n".join(src_summaries) if src_summaries else "(sources not resolved)"
    return (
        f"[synthesis claim]\n"
        f"summary: {_truncate(c.get('claim_text', ''))}\n"
        f"sources ({len(src_ids)}):\n{src_block}\n"
        f"about: {_format_concepts(c.get('concept_ids') or [], lookup)}"
    )


def _tpl_dead_end(c: dict, lookup: ConceptLookup) -> str:
    """dead_end 的 essence 是 reason 而非 fact。"""
    reason = (c.get("dont_repeat_reason") or c.get("reason") or "").strip()
    return (
        f"[dead_end claim]\n"
        f"failure: {_truncate(c.get('claim_text', ''))}\n"
        f"why_dont_repeat: {_truncate(reason, 600)}\n"
        f"about: {_format_concepts(c.get('concept_ids') or [], lookup)}\n"
        f"scope: {_format_scope(c.get('scope_dimensions'))}"
    )


def _tpl_default(c: dict, lookup: ConceptLookup,
                 claim_type: str = "claim") -> str:
    return (
        f"[{claim_type} claim]\n"
        f"text: {_truncate(c.get('claim_text', ''))}\n"
        f"about: {_format_concepts(c.get('concept_ids') or [], lookup)}\n"
        f"scope: {_format_scope(c.get('scope_dimensions'))}"
    )


# ── Dispatcher ─────────────────────────────────────────────────────────────

def claim_to_embed_text(
    claim: dict,
    *,
    concept_lookup: ConceptLookup,
    claim_lookup: Callable[[str], dict | None] | None = None,
) -> str:
    """KB claim record → 拼接的 embed text。

    claim_type 不在已知列表的，走 fallback default template（forward-compat）。
    """
    ct = (claim.get("claim_type") or "empirical").lower()
    if ct == "empirical":
        return _tpl_empirical(claim, concept_lookup)
    if ct == "methodological":
        return _tpl_methodological(claim, concept_lookup)
    if ct == "hypothesis":
        return _tpl_hypothesis(claim, concept_lookup)
    if ct == "synthesis":
        return _tpl_synthesis(claim, concept_lookup, claim_lookup)
    if ct == "dead_end":
        return _tpl_dead_end(claim, concept_lookup)
    # theoretical / causal / assumption / conjecture / replication → fallback
    return _tpl_default(claim, concept_lookup, ct)


# ── Concept template (for concept entity) ──────────────────────────────────

def concept_to_embed_text(concept: dict) -> str:
    """concept record → embed text。

    concept 本身要支持 search_kb semantic（找 "GAP" 时也能命中 "Gaussian Approximation
    Potential" alias）。把 canonical_name + aliases + description 一起 embed。
    """
    name = concept.get("canonical_name", "")
    ctype = concept.get("concept_type", "?")
    aliases = concept.get("aliases") or []
    desc = _truncate(concept.get("description", ""), 400)
    parts = [f"[concept] {name} (type={ctype})"]
    if aliases:
        parts.append(f"aliases: {', '.join(aliases[:6])}")
    if desc:
        parts.append(f"description: {desc}")
    return "\n".join(parts)


# ── Signature for manifest ─────────────────────────────────────────────────

def template_signature() -> str:
    """统一的 template signature —— 用来对比 manifest 决定是否 rebuild。

    格式：'claim_v<N>+concept_v<M>'。改 template 任一就要 bump version。
    """
    return f"claim_v{CLAIM_EMBED_TEMPLATE_VERSION}+concept_v{CONCEPT_EMBED_TEMPLATE_VERSION}"

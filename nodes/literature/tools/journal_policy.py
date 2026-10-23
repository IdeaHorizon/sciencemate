"""小红书期刊准入策略。

期刊映射是离线产物；本模块运行时只读取已冻结的映射表。只有人工按来源核验
过的二级学科映射才能进入推荐，不能把期刊名中的一个词当成学科证据。
"""
from __future__ import annotations

import csv
import os
from dataclasses import dataclass
from pathlib import Path
from .journal_scope import journal_key, scope_matches

@dataclass(frozen=True, slots=True)
class JournalDecision:
    accepted: bool
    reason: str
    domains: tuple[str, ...] = ()

def _norm(value: object) -> str:
    return journal_key(value)

def _path(env: str, default: str) -> Path:
    configured = os.environ.get(env, "").strip()
    return Path(configured).expanduser() if configured else Path(__file__).resolve().parents[3] / "docs" / default

_mapped: dict[str, list[dict[str, str]]] | None = None
_cas: dict[str, dict[str, str]] | None = None

def _read(path: Path) -> dict[str, dict[str, str]]:
    result: dict[str, dict[str, str]] = {}
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle, delimiter="\t"):
                key = _norm(row.get("journal_name"))
                if key:
                    result[key] = row
    except (OSError, csv.Error):
        return {}
    return result

def _load_mapped() -> dict[str, list[dict[str, str]]]:
    global _mapped
    if _mapped is None:
        _mapped = {}
        try:
            with _path("HARNESS_JOURNAL_DOMAIN_MAP", "xiaohongshu_journal_map.tsv").open(encoding="utf-8", newline="") as handle:
                for row in csv.DictReader(handle, delimiter="\t"):
                    key = _norm(row.get("journal_name"))
                    if key:
                        _mapped.setdefault(key, []).append(row)
        except (OSError, csv.Error):
            _mapped = {}
    return _mapped

def _load_cas() -> dict[str, dict[str, str]]:
    global _cas
    if _cas is None:
        _cas = _read(_path("HARNESS_CAS_Q1_Q2_JOURNALS", "cas_q1_q2_journals.tsv"))
    return _cas

def reset_cache() -> None:
    global _mapped, _cas
    _mapped = _cas = None

_MANUAL_BASES = frozenset({"manual_source_verified", "manual_expert_review"})

def decide(*, venue: str, source: str = "", pub_type: str = "", title: str = "", abstract: str = "") -> JournalDecision:
    """返回准入结果；不会在此处建立或修改期刊映射。"""
    source_id = str(source or "").strip().lower().split("/", 1)[0]
    if source_id == "arxiv" or "arxiv" in str(venue or "").lower():
        return JournalDecision(True, "arxiv_top_level", ("arxiv",))
    if not venue:
        return JournalDecision(False, "missing_venue")
    if pub_type and "journal" not in pub_type.lower():
        return JournalDecision(False, "not_journal_article")
    key = _norm(venue)
    mapped = _load_mapped().get(key)
    if mapped is not None:
        verified_codes = tuple(dict.fromkeys(
            x.strip() for row in mapped
            if row.get("分类状态") == "manually_verified" and row.get("分类依据") in _MANUAL_BASES
            for x in str(row.get("二级学科代码", "")).split(";") if x.strip()
        ))
        if verified_codes:
            codes = tuple(code for code in verified_codes if scope_matches(code, venue, title, abstract))
            if codes:
                return JournalDecision(True, "offline_mapped_journal", codes)
            return JournalDecision(False, "paper_outside_mapped_scope")
        return JournalDecision(False, "offline_mapping_not_manually_verified")
    generic = _load_cas().get(key)
    if generic is None:
        return JournalDecision(False, "journal_not_in_cas_mapping")
    if str(generic.get("cas_quartile", "")).strip() != "1" or str(generic.get("top", "")).strip() != "是":
        return JournalDecision(False, "generic_not_q1_top")
    # Q1 Top 证明的是期刊层级，不证明某篇论文属于哪一个二级学科。没有人工
    # 来源映射就停止，避免把标题关键词猜成不可审计的送达地址。
    return JournalDecision(False, "generic_q1_top_unclassified")

"""Academic retrieval shared by the search page and the literature node.

Owns question planning, source recall, index reuse, admission and ranking.
Presentation, browser/user authorization, and E2E evidence/report workflows
remain in their respective adapters. Do not import platform_runtime here.
"""
from __future__ import annotations

from shared.contrib.literature_identity import paper_identity
from .search_audit import audited_search

import asyncio
import html
import json
import re
import tempfile
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable


class AcademicSearchError(ValueError):
    def __init__(self, code: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


def _publication_category(paper: Any) -> str:
    from shared.contrib.publication_categories import publication_category
    return publication_category(paper)



def _normalized_academic_title(value: str) -> str:
    """用于识别原始输入与返回题名是否逐词相同。"""
    return " ".join(re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]", (value or "").lower()))



def _clean_literature_text(value: Any) -> str:
    """把来源返回的 JATS/HTML 变成人可读纯文本，不改写或总结内容。"""
    text = str(value or "")
    text = re.sub(r"<\s*(?:script|style)[^>]*>.*?<\s*/\s*(?:script|style)\s*>", " ", text, flags=re.IGNORECASE | re.DOTALL)
    # Keep scientific subscripts/superscripts before stripping markup. Plain
    # tag removal turns V<sub>OC</sub> into "V OC" and cm<sup>-2</sup> into
    # "cm -2", obscuring which quantity a value belongs to during translation.
    for tag, marker in (("sub", "_"), ("sup", "^")):
        pattern = rf"\s*<\s*(?:[\w-]+:)?{tag}\b[^>]*>\s*(.*?)\s*<\s*/\s*(?:[\w-]+:)?{tag}\s*>"
        text = re.sub(
            pattern,
            lambda match: marker + re.sub(r"<[^>]+>", "", match.group(1)).strip(),
            text, flags=re.IGNORECASE | re.DOTALL,
        )
    text = re.sub(r"<[^>]+>", " ", text)
    return " ".join(html.unescape(text).split())



def _cached_abstract_matches_title(title: str, abstract: str) -> bool:
    """Guard against an older index row carrying another paper's abstract."""
    if len(abstract) < 150:
        return True
    title_terms = {
        term for term in re.findall(r"[a-z]{5,}", title.lower())
        if term not in {"about", "after", "among", "between", "from", "their", "these", "those", "using", "without"}
    }
    if len(title_terms) < 3:
        return True
    abstract_terms = set(re.findall(r"[a-z]{5,}", abstract[:1200].lower()))
    return bool(title_terms & abstract_terms)



def _index_paper(paper: Any, query: str, *, relevance: float | None = None) -> dict[str, Any]:
    from nodes.literature.tools.filter import compute_relevance_score, quality_score_breakdown
    if relevance is None:
        relevance = compute_relevance_score(paper, query)
    breakdown = quality_score_breakdown(paper, relevance)
    title = _clean_literature_text(getattr(paper, "title", ""))
    abstract = _clean_literature_text(getattr(paper, "abstract", ""))
    venue = _clean_literature_text(getattr(paper, "venue", ""))
    authors = [
        cleaned for author in list(getattr(paper, "authors", []) or [])
        if (cleaned := _clean_literature_text(author))
    ]
    impact_factor = breakdown.get("impact_factor")
    if impact_factor in (None, ""):
        impact_factor = getattr(paper, "impact_factor", None)
    try:
        impact_factor = float(impact_factor) if impact_factor not in (None, "") else None
    except (TypeError, ValueError):
        impact_factor = None
    result = {
        "title": title,
        "title_zh": getattr(paper, "title_zh", None),
        "abstract_zh": getattr(paper, "abstract_zh", None),
        "authors": authors,
        "year": getattr(paper, "year", None),
        "pub_date": str(getattr(paper, "pub_date", "") or "") or None,
        "venue": venue or None,
        "doi": getattr(paper, "doi", None) or None, "url": getattr(paper, "url", None) or None,
        "abstract": abstract or None,
        "ai_summary": None,
        "source": str(getattr(paper, "source", "") or ""),
        "citations": int(getattr(paper, "citations", 0) or 0),
        "score": float(breakdown.get("total", 0.0)), "score_breakdown": breakdown,
        "cas_quartile": breakdown.get("cas_quartile"),
        "cas_top": bool(breakdown.get("cas_top")),
        "impact_factor": impact_factor,
        "impact_factor_year": breakdown.get("impact_factor_year"),
        "jcr_quartile": breakdown.get("jcr_quartile"),
        "cas_year": breakdown.get("cas_year"),
        "journal_metrics_match": breakdown.get("journal_metrics_match"),
        "metadata_provenance": dict(getattr(paper, "metadata_provenance", {}) or {}),
        "publication_category": _publication_category(paper),
    }
    result["index_completeness"] = {k: bool(result.get(k)) for k in ("title", "authors", "year", "venue", "doi", "abstract", "url")}
    return result



_ACADEMIC_SEARCH_MIN_RELEVANCE = 0.4



_ACADEMIC_QUERY_STOPWORDS = {
    "about", "against", "among", "and", "are", "for", "from", "how",
    "into", "of", "on", "or", "the", "their", "this", "through", "to",
    "using", "versus", "via", "what", "when", "where", "which", "with",
    "article", "articles", "case", "cases", "experimental", "find", "findings",
    "incorrect", "not", "paper", "papers", "related", "result", "results",
    "review", "reviews", "scholarly", "studies", "study", "task", "tasks",
    "type", "types",
}



_RELATIVE_DATE_QUERY_RE = re.compile(
    r"(?:今天|今日|本日|当天|today|this\s+day)",
    re.IGNORECASE,
)



def _academic_strategy_question(query: str) -> str:
    """把相对日期变成可检索的硬约束，不判断用户是不是在问学术问题。"""
    if not _RELATIVE_DATE_QUERY_RE.search(query):
        return query
    today = datetime.now().date().isoformat()
    return (
        f"{query}\n\n"
        f"检索硬约束：当前日期是 {today}。原问题中的‘今天/今日/today/this day’"
        "必须在每组英文检索词中解析为这个具体月日；不得退化成泛泛的 today、"
        "history 或 historical events。"
    )



def _academic_core_terms(query: str) -> list[str]:
    """第一组核心查询中不可被后续扩展查询稀释的实质词。"""
    terms = re.findall(
        r"[A-Za-z][A-Za-z0-9-]{2,}|\d+(?:\.\d+)?|[\u4e00-\u9fff]{2,}",
        query or "",
    )
    # A two-letter concept (AI, pH) can be the entire user query. Keep it as
    # an exact anchor instead of treating an otherwise valid query as empty.
    if not terms and re.fullmatch(r"[A-Za-z]{2}", (query or "").strip()):
        terms = [(query or "").strip()]
    return list(dict.fromkeys(
        term.lower()
        for term in terms
        if term.lower() not in _ACADEMIC_QUERY_STOPWORDS
    ))



def _academic_compact_query(query: str, *, max_terms: int = 8) -> str:
    """Keep the leading research concepts; instructions do not dilute relevance.

    A long translated question can contain 15-25 words. Scoring against all of
    them makes a good paper with a concise title/abstract miss the 0.4 gate.
    Search the compact form first and retain the complete query as a second
    retrieval angle.
    """
    terms = _academic_core_terms(query)
    compact = " ".join(terms[:max_terms])
    return compact if compact and compact.casefold() != query.casefold() else query



def _academic_boolean_facets(query: str) -> list[list[str]]:
    """Preserve topic facets and later explicit study-population constraints.

    The subject can follow a method and an outcome, for example ``machine
    learning AND early prediction AND sepsis``. Later clauses often list
    optional validations or publication types. OR alternatives remain options.
    """
    if not re.search(r"\bAND\b", query, re.IGNORECASE):
        return []
    pieces = re.split(r"\s+AND\s+", query, flags=re.IGNORECASE)
    facets = []
    population_terms = {"child", "children", "pediatric", "paediatric", "adolescent", "infant", "elderly", "older adult", "pregnant", "women", "men"}
    selected = [
        piece for index, piece in enumerate(pieces)
        if index < 3 or any(
            re.search(rf"\b{re.escape(term)}\b", piece, re.IGNORECASE)
            for term in population_terms
        )
    ]
    for piece in selected:
        alternatives = re.split(r"\s+OR\s+", piece.strip(" ()"), flags=re.IGNORECASE)
        terms = [" ".join(_academic_core_terms(value)) for value in alternatives]
        facets.append([value for value in terms if value])
    return facets if len(facets) >= 2 and all(facets) else []



def _academic_comparison_facets(
    facets: list[list[str]], original_query: str
) -> list[list[str]]:
    """A question comparing two approaches needs evidence about either one."""
    if len(facets) < 3 or not re.search(
        r"哪(?:一)?种更|哪个更|孰优|which\s+.+?\s+more|\bversus\b|\bvs\.?\b",
        original_query, re.IGNORECASE,
    ):
        return facets
    return [facets[0] + facets[1], *facets[2:]]



def _paper_matches_academic_facets(paper: dict[str, Any], facets: list[list[str]], *, strict_core: bool = False) -> bool:
    if not facets:
        return True
    text = f"{paper.get('title') or ''} {paper.get('abstract') or ''}".lower()
    title = str(paper.get("title") or "").lower()
    if any("language" in alternative.split() for facet in facets for alternative in facet):
        if not re.search(r"\blanguages?\b", title + " " + str(paper.get("abstract") or "")[:350].lower()):
            return False
    # An exact exposure target must not be satisfied solely by background
    # mentions in a study whose title explicitly names a different particle
    # class. Such abstracts often cite PM2.5 before describing coarse PM or
    # ultrafine particles as the actual measured exposure.
    if facets and any("pm2" in alternative.split() and "5" in alternative.split()
                      for alternative in facets[0]):
        target = r"\b(?:pm\s*2[.\- ]?5|fine particulate matter|fine particles|fine pm)\b"
        if re.search(r"\b(?:coarse particulate matter|coarse pm|ultrafine particles?)\b", title) \
                and not re.search(target, title):
            return False
        if not re.search(target, text):
            return False
    # New-onset outcomes are different from symptom control, exacerbations,
    # or absenteeism among patients who already have the condition.
    onset_facets = [facet for facet in facets if any(
        re.search(r"\b(?:incident|incidence|new-onset)\b", alternative)
        for alternative in facet
    )]
    if onset_facets:
        outcome_terms = set.intersection(*(
            set(alternative.split()) for alternative in onset_facets[0]
        )) if len(onset_facets[0]) > 1 else set()
        if re.search(r"\brisk of\b", title) and outcome_terms and not any(
            _academic_term_matches(term, title) for term in outcome_terms
        ):
            return False
        if re.search(r"\b(?:exacerbations?|symptoms?|absenteeism)\b", title) \
                and not (
                    re.search(r"\b(?:incident|incidence|onset|develop(?:ment|ing)?)\b", title)
                    or any(re.search(rf"\b(?:risk of\s+{re.escape(term)}|{re.escape(term)}\s+risk)\b", title)
                           for term in outcome_terms)
                ):
            return False
    if any(set(" ".join(facet).split()) & {"child", "children", "pediatric", "paediatric"} for facet in facets):
        if re.search(r"\b(?:elderly|older adults?|adult patients?|post-menopausal)\b", title):
            return False
    for index, facet in enumerate(facets):
        shared = set.intersection(*(set(item.split()) for item in facet)) if len(facet) > 1 else set()
        # A third OR group often lists alternative measurement methods. A
        # sparse abstract may name neither, while still studying the target.
        # Keep it for ranking but do not make it a hard admission condition.
        if not strict_core and index == 2 and len(facet) > 1 and not shared and all(len(item.split()) > 1 for item in facet):
            continue
        if shared and not all(
            _academic_term_matches(term, title + " " + str(paper.get("abstract") or "")[:350].lower())
            for term in shared
        ):
            return False
        if not any(
            sum(_academic_term_matches(term, text) for term in alternative.split())
            >= (
                len(alternative.split())
                if any(term.isdigit() for term in alternative.split())
                else (len(alternative.split()) + 1) // 2
            )
            for alternative in facet
        ):
            return False
    return True



def _academic_facet_title_coverage(
    paper: dict[str, Any], facets: list[list[str]]
) -> float:
    if not facets:
        return 0.0
    title = str(paper.get("title") or "").lower()
    return sum(
        any(
            all(_academic_term_matches(term, title) for term in alternative.split())
            for alternative in facet
        )
        for facet in facets
    ) / len(facets)



def _academic_period_focus(query: str) -> tuple[str, str, list[str]] | None:
    """Extract an explicitly named historical period and its following topic.

    A paper mentioning a period only in background should not outrank one
    whose title studies that period and the question's stated phenomenon.
    """
    match = re.search(
        r"\b(?:(?P<place>[A-Z][a-z]+)\s+)?"
        r"(?P<era>(?:Bronze|Iron|Stone|Copper|Middle|Dark)\s+Age)\b",
        query,
    )
    if not match:
        return None
    place = (match.group("place") or "").lower()
    following = query[match.end():].lstrip()
    # Models may write either "European Bronze Age population migration" or
    # "Bronze Age Europe population migration". In both cases Europe is the
    # place and the next words describe the phenomenon being studied.
    if not place:
        next_word = re.match(r"([A-Z][a-z]+)\b", following)
        if next_word:
            place = next_word.group(1).lower()
            following = following[next_word.end():]
    topic = _academic_core_terms(following)[:2]
    return place, match.group("era").lower(), topic



def _academic_period_focus_score(
    paper: dict[str, Any], focus: tuple[str, str, list[str]] | None
) -> float:
    if not focus:
        return 0.0
    place, era, topic = focus
    title = str(paper.get("title") or "").lower()
    text = f"{title} {paper.get('abstract') or ''}".lower()
    place_forms = [place, place[:-2]] if place.endswith("ean") else [place]
    place_hit = not place or any(value and value in text for value in place_forms)
    era_title = era in title
    topic_title = bool(topic) and any(_academic_term_matches(term, title) for term in topic)
    other_era_title = bool(re.search(r"\b[a-z]+ age\b", title)) and not era_title
    return (
        0.20 * place_hit + 0.25 * era_title + 0.55 * topic_title
        - 0.25 * other_era_title
    )



def _academic_period_method_terms(query: str) -> list[str]:
    """Keep an explicit analytical method in named-period searches.

    DNA studies are often titled as genomics/genetics. These are genuine
    equivalents, while a plain archaeology or news item is not evidence from
    the requested method merely because it mentions the period and migration.
    """
    if not _academic_period_focus(query):
        return []
    if re.search(r"\bDNA\b", query, re.IGNORECASE):
        return ["dna", "genom", "genetic"]
    return []



def _paper_has_academic_period(
    paper: dict[str, Any], focus: tuple[str, str, list[str]] | None
) -> bool:
    if not focus:
        return True
    era = focus[1]
    title = str(paper.get("title") or "").lower()
    abstract_opening = str(paper.get("abstract") or "")[:500].lower()
    # For a question about population migration, ancient animal DNA is a
    # different research subject even when the specimen dates to the era.
    if any(term.startswith(("populat", "migrat")) for term in focus[2]) and re.search(
        r"\b(?:rabbits?|pigs?|boars?|horses?|dogs?|cattle|sheep|goats?)\b", title
    ):
        return False
    return era in title or era in abstract_opening



def _paper_has_academic_method(
    paper: dict[str, Any], terms: list[str]
) -> bool:
    if not terms:
        return True
    # An incidental sentence in a long abstract is too weak to establish
    # that the requested method was actually used in the study.
    text = f"{paper.get('title') or ''} {str(paper.get('abstract') or '')[:300]}".lower()
    return any(_academic_term_matches(term, text) for term in terms)



def _academic_leading_title_coverage(
    paper: dict[str, Any], terms: list[str]
) -> float:
    leading = terms[:2]
    if not leading:
        return 0.0
    title = str(paper.get("title") or "").lower()
    return sum(_academic_term_matches(term, title) for term in leading) / len(leading)



def _academic_english_query_ok(value: str, original_query: str = "") -> bool:
    """Allow a one-word term only when the source is a bare short topic."""
    from nodes.literature.tools.strategy_generator import is_short_standalone_topic

    return (
        not re.search(r"[\u4e00-\u9fff]", value)
        and len(_academic_core_terms(value)) >= (
            1 if is_short_standalone_topic(original_query) else 2
        )
    )



def _academic_retry_strategy_better(current: dict[str, Any], retry: dict[str, Any]) -> bool:
    current_valid = _academic_english_query_ok(str(current.get("english_query") or ""))
    retry_valid = _academic_english_query_ok(str(retry.get("english_query") or ""))
    current_count = len(current.get("search_queries") or [])
    retry_count = len(retry.get("search_queries") or [])
    return retry_valid and (not current_valid or retry_count > current_count)



def _academic_strategy_queries(strategy: dict[str, Any], original_query: str) -> list[str]:
    """Search international sources with English terms when a Chinese prompt has them.

    The strategy response has a separate ``english_query`` field. Older code
    ignored it and used Chinese ``search_queries`` for English metadata, then
    required those Chinese phrases to appear in English titles/abstracts.
    """
    if strategy.get("bilingual_queries"):
        # The semantic plan already separates questions and preserves all core
        # concepts. Do not add truncated prefixes or the original paragraph.
        return list(dict.fromkeys(p["en"] for p in strategy["bilingual_queries"]))[:6]
    raw = strategy.get("search_queries") or []
    queries = list(dict.fromkeys(
        " ".join(str(item.get("query") or "").split())
        for item in raw
        if isinstance(item, dict) and str(item.get("query") or "").strip()
    ))
    if not re.search(r"[\u4e00-\u9fff]", original_query):
        return queries[:6] or [original_query]

    english = " ".join(str(strategy.get("english_query") or "").split())
    # A single broad acronym such as "DNA" is not a translation of a detailed
    # research question. Keep it out of the anchor position.
    if not _academic_english_query_ok(english, original_query):
        return queries[:6] or [original_query]

    facets = _academic_boolean_facets(english)
    if len(facets) >= 3 and len(_academic_comparison_facets(facets, original_query)) < len(facets):
        # A comparison may be answered by separate evidence for each option.
        # Searching only the full Boolean expression misses studies of one
        # option; the ordinary compact prefix can also omit the shared goal.
        goal = facets[2][0]
        angles = [f"{side[0]} {goal}" for side in facets[:2]]
        angles.extend(f"{side[0].split()[0]} {goal}" for side in facets[:2])
        return list(dict.fromkeys([english, *angles, original_query]))[:6]

    compact = _academic_compact_query(english)
    international = [compact, english, *(
        item for item in queries
        if not re.search(r"[\u4e00-\u9fff]", item)
    )]
    international = list(dict.fromkeys(international))[:5]
    # Preserve the original language for CNKI, but never use it as the anchor
    # for relevance checks on English papers.
    return [*international, original_query][:6]



_ACADEMIC_HYPHEN_MAP = str.maketrans({char: "-" for char in "‐‑﹣－"})



def _academic_term_matches(term: str, text: str) -> bool:
    """数字完整匹配；普通词匹配子串及常见属性名词的形容词词形。"""
    # Publishers commonly use U+2010/U+2011 for compound words, while search
    # strategies use ASCII hyphens. These are typography, not different terms.
    term = term.translate(_ACADEMIC_HYPHEN_MAP)
    if "-" in term:
        text = text.translate(_ACADEMIC_HYPHEN_MAP)
    if term and term[0].isdigit():
        return re.search(rf"(?<!\d){re.escape(term)}(?!\d)", text) is not None
    if not term:
        return False
    if len(term) == 2 and term.isalpha():
        return re.search(rf"(?<![A-Za-z]){re.escape(term)}(?![A-Za-z])", text) is not None
    if term in text:
        return True
    # Noun/adjective forms convey the same property (tolerance/tolerant,
    # resistance/resistant). Preserve the existing substring semantics, also
    # covering compounds such as thermotolerant, without adding topic synonyms.
    for noun_suffix, adjective_suffix in (("ance", "ant"), ("ence", "ent")):
        if term.endswith(noun_suffix) and len(term) - len(noun_suffix) >= 4:
            return term[:-len(noun_suffix)] + adjective_suffix in text
    return False



def _paper_has_anchor_coverage(paper: dict[str, Any], terms: list[str]) -> bool:
    """折中准入：至少两个原始锚点命中，且至少一个必须出现在标题。"""
    if not terms:
        return True
    title = str(paper.get("title") or "").lower()
    text = f"{title} {paper.get('abstract') or ''}".lower()
    required = min(2, len(terms))
    matched = sum(1 for term in terms if _academic_term_matches(term, text))
    title_matched = any(_academic_term_matches(term, title) for term in terms)
    return matched >= required and title_matched



def _academic_intent_coverage(
    paper: dict[str, Any], terms: list[str]
) -> float:
    """论文对原始问题限定词的覆盖率，不受扩展查询的高分稀释。

    六组扩展查询负责扩大召回，但排序还应回到原始问题：只命中其中一个宽泛
    角度的论文可以保留，不能仅凭期刊、引用或时效性排到完整覆盖原问题的论文
    前面。这是逐篇评分，不做主题去重或结果多样性压制。
    """
    if not terms:
        return 1.0
    text = f"{paper.get('title') or ''} {paper.get('abstract') or ''}".lower()
    matched = sum(
        1 for term in terms if _academic_term_matches(term, text)
    )
    return matched / len(terms)



def _academic_nonresearch_record(paper: dict[str, Any]) -> bool:
    """Exclude notices and source documents, including those mislabeled as articles."""
    from nodes.literature.tools.publication_types import is_journal_notice_title
    from shared.contrib.publication_relations import supplement_relation

    title = str(paper.get("title") or "").strip()
    venue = str(paper.get("venue") or "").lower()
    abstract_opening = str(paper.get("abstract") or "")[:100].lower().lstrip()
    if re.match(r"reviewed by\s*:", abstract_opening):
        return True
    if is_journal_notice_title(title) or supplement_relation(paper):
        return True
    if re.search(r"\b(?:science news|news & views|book review)\b", venue):
        return True
    # Crossref sometimes concatenates two reviewed book titles and their
    # authors into one apparent journal-article title.
    if re.search(r"\.\s+[A-Z][a-z]+\s+[A-Z][a-z]+\s+The\s+[A-Z]", title):
        return True
    if re.search(r"\bvol(?:ume)?\.?\s*\d+\b.*\bby\s+[A-Z][a-z]+\s+[A-Z]", title, re.IGNORECASE):
        return True
    return bool(re.search(
        r"^(?:book review\s*[:：]|review of\s+(?:the\s+)?book\s+|"
        r"(?:united nations|unesco)\s+(?:general assembly\s+)?(?:discusses|resolution|session)\b|"
        r"(?:the\s+)?resolution\s+on\s+the\s+return\b|"
        r"unesco:\s*\w+\s+session\b)",
        title, re.IGNORECASE,
    ))



def _academic_primary_focus_penalty(
    paper: dict[str, Any], terms: list[str], facets: list[list[str]]
) -> float:
    """Downrank a study of a complication or an incidental method mention."""
    title = str(paper.get("title") or "").lower()
    opening = str(paper.get("abstract") or "")[:500].lower()
    words = set(terms)
    facet_words = {
        alternative for facet in facets for alternative in facet
        if " " not in alternative and len(alternative) >= 5
    }
    predictive = bool(words & {"prediction", "predict", "detection", "early"})
    if predictive and any(re.search(
        rf"\b{re.escape(target)}[- ](?:associated|induced|related)\b", title
    ) for target in facet_words):
        return 0.30
    for facet in facets:
        if len(facet) < 2:
            continue
        shared = set.intersection(*(set(item.split()) for item in facet))
        if any(len(term) >= 5 and not _academic_term_matches(term, title) for term in shared):
            return 0.18
    if any(word.startswith("rerank") for word in words):
        # A RAG paper that merely mentions reranking deep in the abstract
        # should not outrank a study that evaluates the requested method.
        if not re.search(r"\bre[- ]?rank(?:ing|ed|er|ers)?\b", title + " " + opening):
            return 0.25
    return 0.0



def _academic_named_concept_gate(paper: dict[str, Any], terms: list[str]) -> bool:
    """Keep a named technique tied to its field, not a lexical homonym."""
    words = set(terms)
    if not {"optimal", "transport", "sinkhorn"} <= words:
        return True
    text = f"{paper.get('title') or ''} {str(paper.get('abstract') or '')[:500]}".lower()
    return "optimal transport" in text or "sinkhorn" in text



def _academic_exclusion_reason(
    paper: dict[str, Any], *, core_terms=None, mandatory_facets=None,
    period_focus=None, method_terms=None, strict_core=False,
) -> str | None:
    """First failing admission gate; counts are mutually exclusive."""
    if _academic_nonresearch_record(paper):
        return "nonresearch"
    if (paper.get("relevance_assessment") or {}).get("exclusion_reason") == "study_conditions":
        return "study_conditions"
    if paper.get("exact_title_match"):
        return None
    assessment = paper.get("relevance_assessment")
    if assessment:
        return assessment["exclusion_reason"]
    relevance = float(((paper.get("score_breakdown") or {}).get("raw") or {}).get("relevance", 0.0) or 0.0)
    if not relevance >= _ACADEMIC_SEARCH_MIN_RELEVANCE:
        return "relevance"
    if not _paper_has_anchor_coverage(paper, core_terms or []):
        return "anchor"
    if not _paper_matches_academic_facets(paper, mandatory_facets or [], strict_core=strict_core):
        return "facets"
    if not _paper_has_academic_period(paper, period_focus):
        return "period"
    if not _paper_has_academic_method(paper, method_terms or []):
        return "method"
    if not _academic_named_concept_gate(paper, core_terms or []):
        return "named_concept"
    return None



def _select_academic_search_results(
    papers: list[dict[str, Any]],
    *,
    limit: int | None,
    core_terms: list[str] | None = None,
    mandatory_facets: list[list[str]] | None = None,
    period_focus: tuple[str, str, list[str]] | None = None,
    method_terms: list[str] | None = None,
    strict_core: bool = False,
) -> tuple[list[dict[str, Any]], int]:
    """先做相关性准入，再按综合质量排序；返回结果绝不补位。"""
    eligible = [paper for paper in papers if _academic_exclusion_reason(
        paper, core_terms=core_terms, mandatory_facets=mandatory_facets,
        period_focus=period_focus, method_terms=method_terms, strict_core=strict_core,
    ) is None]
    # 综合质量分仍然决定相关论文之间的质量顺序，但额外保留 35% 给原始问题
    # 的要素覆盖，避免某个宽泛扩展查询把只沾边的高质量论文抬到最前面。
    for paper in eligible:
        assessment = paper.get("relevance_assessment") or {}
        intent_coverage = _academic_intent_coverage(
            paper, core_terms or []
        )
        paper["intent_coverage"] = round(intent_coverage, 4)
        facet_title_coverage = _academic_facet_title_coverage(
            paper, mandatory_facets or []
        )
        if assessment:
            intent_coverage = assessment["coverage"]
            facet_title_coverage = assessment["title_coverage"]
            paper["intent_coverage"] = intent_coverage
        period_score = _academic_period_focus_score(paper, period_focus)
        leading_title = _academic_leading_title_coverage(paper, core_terms or [])
        paper["ranking_score"] = round(
            float(paper.get("score") or 0.0) * (
                0.50 if mandatory_facets else 0.45 if period_focus else 0.50
            )
            + intent_coverage * (
                0.30 if mandatory_facets else 0.15 if period_focus else 0.30
            )
            + facet_title_coverage * (0.20 if mandatory_facets else 0.0)
            + period_score * (0.40 if period_focus and not mandatory_facets else 0.0)
            + leading_title * (0.20 if not mandatory_facets and not period_focus else 0.0)
            - _academic_primary_focus_penalty(
                paper, core_terms or [], mandatory_facets or []
            ),
            4,
        )
    eligible.sort(
        key=lambda paper: (
            bool(paper.get("exact_title_match")),
            {"direct": 2, "background": 1, "unconfirmed": 0}.get((paper.get("relevance_assessment") or {}).get("tier"), 2),
            float(paper.get("ranking_score") or 0.0),
            float(paper.get("score") or 0.0),
        ),
        reverse=True,
    )
    return eligible[:limit], len(papers) - len(eligible)



def _academic_search_warnings(audits: list[dict]) -> list[dict]:
    """One visible warning per source/failure; per-query audits remain intact."""
    warnings, seen = [], set()
    for audit in audits:
        for original in audit.get("warnings", []):
            warning = dict(original)
            sources = tuple(sorted(warning.get("sources", [])))
            if warning.get("http_status") == 429:
                key = (sources, 429)
                warning["detail"] = (
                    f"{'、'.join(sources)} 请求受到限流（HTTP 429），本轮结果可能缺少部分候选。"
                )
            else:
                key = (sources, warning.get("code"), warning.get("detail"))
            if key not in seen:
                seen.add(key)
                warnings.append(warning)
    return warnings



def _record_literature_performance(operation: str, **metrics: Any) -> None:
    """Keep recent aggregate timings locally, without paper bodies or credentials."""
    try:
        from core.paths import literature_cache_dir
        directory = literature_cache_dir() / "performance"
        directory.mkdir(parents=True, exist_ok=True)
        destination = directory / f"{time.time_ns()}-{uuid.uuid4().hex}.json"
        row = {"operation": operation, "at": datetime.now(UTC).isoformat(), **metrics}
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=directory, delete=False) as handle:
            json.dump(row, handle, ensure_ascii=False)
            temporary = Path(handle.name)
        temporary.replace(destination)
        for old in sorted(directory.glob("*.json"))[:-200]:
            old.unlink(missing_ok=True)
    except Exception:
        # A diagnostic write must never turn a successful search into an error.
        pass



async def run_literature_search_plan(request: dict[str, Any]) -> dict[str, Any]:
    from nodes.literature.tools.academic_strategy import generate_academic_strategy
    query = " ".join(str(request.get("query") or "").split())
    if not query or len(query) > 2000:
        raise AcademicSearchError("invalid_query", "请输入不超过 2000 字的检索问题")
    strategy, diagnostics = await asyncio.to_thread(generate_academic_strategy, _academic_strategy_question(query))
    plan = strategy.get("bilingual_queries") or []
    if not plan:
        _record_literature_performance("plan", query=query, status="failed", strategy_diagnostics=diagnostics)
        status = diagnostics.get("translation_request_status")
        message = ("模型生成中英文检索词时输出被截断，未能在自动恢复时限内生成完整结果，请稍后重试。"
                   if diagnostics.get("translation_status") == "truncated_output" else
                   "模型已返回内容，但中英文检索词未通过内容校验，请重试。"
                   if diagnostics.get("translation_status") == "invalid_output" else
                   "生成中英文检索词时模型响应超时，请稍后重试。" if status in {"read_timeout", "connect_timeout"} else
                   "模型未返回有效的中英文检索词，请稍后重试。")
        raise AcademicSearchError("strategy_translation_failed", message,
                           details={"status": diagnostics.get("translation_request_status")})
    return {"query": query, "search_plan": plan}



@audited_search
async def run_literature_search(
    request: dict[str, Any],
    progress: Callable[[dict[str, Any]], None] | None = None,
    *,
    enabled_sources: set[str] | None = None,
    year_from: int | None = None,
) -> dict[str, Any]:
    if not isinstance(request, dict):
        raise AcademicSearchError("invalid_request", "JSON request must be an object")
    query = " ".join(str(request.get("query") or "").split())
    if not query:
        raise AcademicSearchError("invalid_query", "query must be a non-empty string")
    # A null limit means successive source pages, not one unbounded HTTP request.
    # The finite value below sizes work inside a batch, never the cumulative result.
    unlimited = request.get("limit", 200) is None
    limit = max(1, int(request.get("limit") or 200))
    output_limit = None if unlimited else limit
    from nodes.literature.tools.academic_paging import collect_page, query_key, validate_continuation
    from nodes.literature.tools.academic_categories import CATEGORIES, apply_budget, read_budget
    category_limit = request.get("per_category_limit") if unlimited else None
    pagination_scope = request.get("pagination_scope", "category")
    category_paging = bool(category_limit and pagination_scope == "category")
    try:
        if pagination_scope not in {"query", "category"}:
            raise ValueError("Invalid pagination scope")
        if (request.get("continuation") or {}).get("pagination_scope", pagination_scope) != pagination_scope:
            raise ValueError("Changed pagination scope")
        paging_groups = validate_continuation(request.get("continuation"), query)
        if category_limit is not None and (type(category_limit) is not int or not 1 <= category_limit <= 200):
            raise ValueError("Invalid category limit")
        accepted = read_budget(request.get("continuation"), category_limit) if category_limit else {}
    except ValueError as exc:
        raise AcademicSearchError("invalid_continuation", "检索分页信息已失效，请重新搜索。") from exc
    next_groups = {}
    remote_refresh = bool(request.get("remote_refresh", False))
    started = time.perf_counter()
    progress_state = {"percent": 3}
    timings: dict[str, float] = {}
    stage_started = started

    def finish_stage(name: str) -> None:
        nonlocal stage_started
        now = time.perf_counter()
        timings[name] = round(now - stage_started, 3)
        stage_started = now

    def report(stage: str, detail: str, **extra: Any) -> None:
        if progress is None:
            return
        explicit_percent = extra.pop("progress_percent", None)
        stage_percent = {
            "strategy": 5,
            "exact_title": 10,
            "search": 15,
            "dedup": 72,
            "metadata_enrichment": 76,
            "scoring": 84,
            "final_abstract_enrichment": 88,
            "translation": 94,
            "finalize": 96,
            "complete": 100,
        }.get(stage)
        query_index = int(extra.get("query_index") or 0)
        query_count = int(extra.get("query_count") or 0)
        if stage == "search" and query_index and query_count:
            fraction = query_index / query_count
            if "完成" not in detail:
                fraction = (query_index - 1) / query_count
            stage_percent = 15 + round(fraction * 55)
        candidate = explicit_percent if explicit_percent is not None else stage_percent
        if candidate is not None:
            progress_state["percent"] = max(
                progress_state["percent"], min(100, int(candidate))
            )
        progress({
            "stage": stage,
            "detail": detail,
            "progress_percent": progress_state["percent"],
            **extra,
        })

    from nodes.literature.tools.search_engines import (
        CrossrefSearch, SearchManager, KNOWN_SOURCES, filter_papers_by_sources,
    )
    from nodes.literature.tools.local_index import get_index

    sources = ({"arxiv", "biorxiv", "medrxiv", "pubmed", "crossref", "cnki", "openalex"}
               if enabled_sources is None else set(enabled_sources))
    if not sources or sources - KNOWN_SOURCES:
        raise AcademicSearchError("invalid_sources", "至少启用一个有效的检索来源")

    def lookup_indexed_title():
        try:
            papers = get_index().find_exact_title(query)
            return filter_papers_by_sources(papers, sources)[0] if enabled_sources is not None else papers
        except Exception:
            # An unavailable local index must not prevent ordinary remote search.
            return []

    title_lookup_started = time.perf_counter()
    known_title_papers = await asyncio.to_thread(lookup_indexed_title)
    timings["indexed_title_lookup"] = round(time.perf_counter() - title_lookup_started, 3)

    async def lookup_exact_title():
        # This request uses the original input, so it does not need to wait for
        # the model. Keep its results separate until the normal merge point.
        import httpx
        lookup_started = time.perf_counter()
        try:
            if request.get("continuation"):
                return [], None
            if known_title_papers:
                return known_title_papers, None
            if "crossref" not in sources:
                return [], None
            async with httpx.AsyncClient(follow_redirects=True) as client:
                return await CrossrefSearch(client).search_title(
                    query, rows=min(limit, 30),
                ), None
        except Exception as exc:
            return [], type(exc).__name__
        finally:
            timings["exact_title"] = round(time.perf_counter() - lookup_started, 3)

    report("strategy", "已匹配库内完整标题，继续拆解问题并检索相关文献" if known_title_papers
           else "正在拆解研究问题，生成检索词")
    exact_title_task = asyncio.create_task(lookup_exact_title())
    strategy_started = time.perf_counter()
    try:
        from nodes.literature.tools.academic_strategy import generate_academic_strategy
        # A question can also be a paper's title. Preserve the exact hit as
        # an additional candidate, without replacing the question's strategy.
        strategy, diagnostics = await asyncio.to_thread(
            generate_academic_strategy,
            _academic_strategy_question(query),
        )
        # The generator owns bounded recovery; restarting it here would repeat
        # the same model calls and multiply both latency and upstream load.
        translation_valid = bool(strategy.get("bilingual_queries")) or (
            not re.search(r"[\u4e00-\u9fff]", query)
            or _academic_english_query_ok(str(strategy.get("english_query") or ""), query)
        )
        if diagnostics.get("bilingual_plan_required") and not strategy.get("bilingual_queries"):
            translation_valid = False
        if not translation_valid:
            semantic_issues = diagnostics.get(
                f"translation_attempt_{diagnostics.get('translation_attempts')}_semantic_issues"
            ) or []
            final_attempt = diagnostics.get("translation_attempts")
            finish_reason = diagnostics.get(
                f"translation_attempt_{final_attempt}_finish_reason"
            )
            http_status = diagnostics.get(
                f"translation_attempt_{final_attempt}_http_status"
            )
            request_status = diagnostics.get("translation_request_status")
            strategy_elapsed = round(time.perf_counter() - strategy_started, 3)
            if http_status == 429:
                message = "检索词生成所用的模型服务返回限流（HTTP 429），请稍后重试。"
            elif http_status in {401, 403}:
                message = f"检索词生成所用的模型服务拒绝了访问（HTTP {http_status}），请检查模型凭据或权限。"
            elif request_status in {"connect_timeout", "read_timeout", "write_timeout", "pool_timeout"}:
                message = (
                    f"检索词生成所用的模型服务响应超时"
                    f"（{final_attempt or 1} 次请求，累计 {strategy_elapsed:.1f} 秒），请稍后重试。"
                )
            elif http_status and http_status >= 400:
                message = f"检索词生成所用的模型服务请求失败（HTTP {http_status}），请检查模型服务。"
            elif finish_reason == "length":
                message = "模型生成中英文检索词时输出被截断，未能在自动恢复时限内生成完整结果，请稍后重试。"
            elif diagnostics.get("translation_status") == "request_failed":
                message = (
                    "模型未返回英文检索词正文，请稍后重试。"
                    if request_status == "ok" else
                    "检索词生成所用的模型服务未能完成请求，请检查模型连接后重试。"
                )
            else:
                message = (
                    "模型已返回内容，但检索词未通过内容校验，请重试本次学术检索。"
                    + (f"（校验：{','.join(map(str, semantic_issues))}）" if semantic_issues else "")
                    + (f"（模型结束原因：{finish_reason}）" if finish_reason else "")
                )
            _record_literature_performance(
                "search", request_id=request.get("request_id"), query=query,
                status="failed", failed_stage="strategy", error_code="strategy_translation_failed",
                timings={**timings, "strategy_generation": strategy_elapsed,
                         "total": round(time.perf_counter() - started, 3)},
                strategy_diagnostics=diagnostics,
            )
            raise AcademicSearchError(
                "strategy_translation_failed",
                message,
                details={
                    "strategy_status": diagnostics.get("status"),
                    "translation_status": diagnostics.get("translation_status"),
                    "translation_request_status": diagnostics.get("translation_request_status"),
                    "translation_attempts": diagnostics.get("translation_attempts"),
                    "semantic_issues": semantic_issues,
                    "finish_reason": finish_reason,
                    "http_status": http_status,
                    "elapsed_seconds": strategy_elapsed,
                },
            )
        queries = _academic_strategy_queries(strategy, query)
        paging_entries = ({query_key(category+"\n"+item):(item,category)
                           for item in queries for category in CATEGORIES} if category_paging else
                          {query_key(item):(item,None) for item in queries})
        if request.get("continuation") and set(paging_groups) - set(paging_entries):
            raise AcademicSearchError("invalid_continuation", "检索计划已改变，请重新搜索。")
        diagnostics["strategy_english_query"] = str(strategy.get("english_query") or "")[:300]
        diagnostics["strategy_translation_valid"] = translation_valid
    except (AcademicSearchError, asyncio.CancelledError):
        exact_title_task.cancel()
        await asyncio.gather(exact_title_task, return_exceptions=True)
        raise
    except Exception as exc:
        exact_title_task.cancel()
        await asyncio.gather(exact_title_task, return_exceptions=True)
        raise AcademicSearchError("strategy_generation_failed", "检索计划生成失败，请重试。") from exc
    search_plan = strategy.get("bilingual_queries") or []
    if search_plan:
        report("search_plan", f"已按研究问题生成 {len(search_plan)} 组中英文检索词",
               search_plan=search_plan)
    diagnostics["indexed_exact_title_matches"] = len(known_title_papers)
    timings["strategy_generation"] = round(time.perf_counter() - strategy_started, 3)
    _record_literature_performance(
        "strategy", request_id=request.get("request_id"), query=query,
        status=diagnostics.get("status"), elapsed_seconds=timings["strategy_generation"],
        strategy_diagnostics=diagnostics,
    )

    def source_progress(event: dict[str, Any]) -> None:
        # 单个远程来源的开始/完成顺序取决于网络响应速度，会在同一组查询内
        # 来回切换。它们保留在 SearchManager 的诊断和服务日志中，但不拿来
        # 驱动用户看到的主进度；主进度只呈现“第几组/整理/评分”等稳定阶段。
        if str(event.get("stage") or "") == "source":
            return
        report(
            str(event.get("stage") or "source"),
            str(event.get("detail") or "正在检索来源"),
            **{k: v for k, v in event.items() if k not in {"stage", "detail"}},
        )

    # 查询组数减少时，提高单组召回量，避免“短主题只生成一组”同时把总候选池
    # 缩到原来的六分之一。这里只扩大候选池；最终相关度准入规则保持不变。
    query_count = max(1, min(len(queries), 6))
    # 每个组数都有独立额度。Crossref（以及期刊专用补充通道）维持约
    # 100–180 条总召回；其余来源逐级平滑递减，避免档位跳变。
    crossref_limits = {1: 100, 2: 80, 3: 60, 4: 45, 5: 36, 6: 30}
    pubmed_limits = {1: 50, 2: 40, 3: 35, 4: 30, 5: 24, 6: 20}
    general_limits = {1: 30, 2: 28, 3: 26, 4: 24, 5: 22, 6: 20}
    crossref_limit = crossref_limits[query_count]
    pubmed_limit = pubmed_limits[query_count]
    general_limit = general_limits[query_count]
    academic_source_limits = {
        "arxiv": general_limit,
        "biorxiv": general_limit,
        "medrxiv": general_limit,
        "semantic_scholar": pubmed_limit,
        "pubmed": pubmed_limit,
        "crossref": crossref_limit,
        "openalex": pubmed_limit,
        "google_scholar": general_limit,
    }
    journal_recall_limit = crossref_limit

    from nodes.literature.tools.academic_evidence import EvidenceReview
    from nodes.literature.tools.academic_admission import assess_concepts
    reviewer = EvidenceReview(strategy)
    reviewer.extra_tasks.append(exact_title_task)
    preview_raw = []

    def source_results(batch):
        if not search_plan or not batch:
            return
        preview_raw.extend(batch)
        records = manager._merge_results([], preview_raw)
        if year_from is not None:
            records = [p for p in records if not str(p.year or "")[:4].isdigit()
                       or int(str(p.year)[:4]) >= int(year_from)]
        # Reuse metadata only for identities actually returned by a source.
        if manager.local_index:
            cached = manager.local_index.get_papers_by_identity(records)
            for p in records:
                old = cached.get(paper_identity(p))
                if (old and not p.abstract and old.abstract
                        and _cached_abstract_matches_title(p.title, old.abstract)):
                    p.abstract = old.abstract
        indexed = []
        for p in records:
            assessment = assess_concepts(p, strategy, _ACADEMIC_SEARCH_MIN_RELEVANCE)
            row = _index_paper(p, queries[0], relevance=assessment["relevance"] if assessment else None)
            row["relevance_assessment"] = assessment
            if not _academic_nonresearch_record(row):
                indexed.append(row)
        reviewer.submit(indexed)
        for row in indexed:
            reviewer.apply(row)
        selected, excluded = _select_academic_search_results(indexed, limit=min(limit, 50), strict_core=True)
        if selected:
            report("partial_results", f"已取得 {len(selected)} 篇候选，继续检索与核实",
                   partial_result={"query": query, "papers": selected, "total": len(selected),
                                   "search_plan": search_plan, "decomposed_queries": queries,
                                   "candidate_count": len(indexed), "excluded_low_relevance": excluded,
                                   "source_counts": {}, "warnings": [], "search_mode": "remote",
                                   "requested_sources": sorted(sources), "attempted_sources": [],
                                   "unavailable_sources": [], "from_cache": False})

    async with reviewer:
        manager = SearchManager(
            progress_callback=source_progress,
            results_callback=source_results,
            arxiv_timeout_seconds=10,
            # UI 和 E2E 共用召回额度；不改变 SearchManager 的默认值或资讯采集策略。
            per_source_limits=academic_source_limits,
        )
        if manager.local_index:
            manager.local_index.register_harvest_query(query)
            for item in queries:
                manager.local_index.register_harvest_query(item)
        # 完整标题命中补充确定的候选，同时照常检索相关文献。
        if search_plan:
            # CNKI uses the separate managed-browser job queue, initiated by the
            # search-plan event; do not also invoke the legacy cookie search here.
            sources.discard("cnki")
        all_papers, audits = [], []
        exact_title_keys: set[str] = set()
        journal_recall_keys: set[str] = set()
        candidate_metadata_sources = {"openalex", "openaire"}
        final_metadata_sources = {"pubmed"}
        slow_abstract_sources = {"publisher_landing", "oa_pdf_discovery"}
        from nodes.literature.tools.filter import compute_relevance_score

        finish_stage("strategy_ready")
        report("search", f"已生成 {len(queries)} 组检索词，开始联网检索", query_count=len(queries))
        # One group per processing batch bounds latency independently of result
        # count. Rotate unfinished groups so broad queries do not starve others.
        pending_groups = (dict(paging_groups) if request.get("continuation") else
                          dict.fromkeys(paging_entries)) if unlimited else {}
        if category_paging:
            pending_groups = {key:value for key,value in pending_groups.items()
                              if len(accepted.get(paging_entries[key][1], [])) < category_limit}
        elif category_limit and all(len(accepted.get(category, [])) >= category_limit for category in CATEGORIES):
            pending_groups = {}
        current_group = next(iter(pending_groups), None)
        current_query, current_category = paging_entries.get(current_group, (None, None))
        next_groups.update(pending_groups)
        next_groups.pop(current_group, None)
        for idx, item in enumerate(queries, 1):
            if unlimited and item != current_query:
                continue
            report(
                "search",
                f"正在检索第 {idx}/{len(queries)} 组关键词：{item}",
                query_index=idx,
                query_count=len(queries),
            )
            # arXiv 的公开 Atom API 对同一出口的连续请求容易返回 429；一轮学术
            # 搜索只用第一组（最宽泛、最接近原问题）召回预印本，其余组继续检索
            # 期刊/生医来源。这样仍保留 arXiv 覆盖，又不因六组词连续撞六次限流。
            query_sources = (
                sources & {"cnki"} if re.search(r"[\u4e00-\u9fff]", item)
                else sources - {"cnki"}
            )
            if idx != 1:
                query_sources = query_sources - {"arxiv"}
            async def search_sources():
                if unlimited:
                    position = pending_groups[current_group]
                    batch, next_position = await collect_page(manager, item, query_sources,
                        academic_source_limits, position, **({"category":current_category} if category_paging else {}))
                    if next_position:
                        next_groups[current_group] = next_position
                    return batch
                return await manager.search_all(
                    item,
                    max_per_source=20,
                    enabled_sources=query_sources,
                    include_local_catalog=False,
                    use_query_cache=False,
                    # OpenAlex 只做 DOI 精准补齐，不重新加入耗时的关键词检索。
                    # 出版商落地页仅接受明确的摘要元标签。
                    metadata_lookup_sources=candidate_metadata_sources,
                    # 学术搜索先拿本轮远程候选身份，缓存命中判断完成后再统一写库；
                    # 否则本轮刚返回的 DOI 会被误判成历史缓存。
                    persist_results=False,
                    # 学术搜索先完成六组 index 召回；逐组补摘要会对稍后被全局去重的
                    # 同一论文重复发请求，并在第六组结束前耗尽 bridge 总预算。
                    enrich_metadata=False,
                )

            journal_audit: dict[str, Any] = {"status": "not_requested", "raw_count": 0}

            async def search_journals():
                if unlimited:
                    return []  # Included, with its own cursor, by collect_page.
                # Respect the same source policy as the primary query. Chinese-only
                # CNKI queries must not silently trigger broad Crossref retrieval.
                if "crossref" not in query_sources:
                    return []
                journal_results = []
                journal_started = time.perf_counter()
                try:
                    import httpx

                    async with httpx.AsyncClient(follow_redirects=True) as client:
                        journal_results = await CrossrefSearch(client).search(
                            item,
                            rows=journal_recall_limit,
                            publication_types=("journal-article",),
                        )
                    journal_audit.update(status="ok", raw_count=len(journal_results))
                    try:
                        source_results(journal_results)
                    except Exception:
                        pass  # Preview failure does not make successful recall fail.
                    report(
                        "journal_recall",
                        f"第 {idx}/{len(queries)} 组期刊专用召回完成：{len(journal_results)} 篇",
                        query_index=idx,
                        query_count=len(queries),
                        result_count=len(journal_results),
                        elapsed_seconds=round(time.perf_counter() - journal_started, 3),
                    )
                except Exception as exc:
                    status = getattr(getattr(exc, "response", None), "status_code", None)
                    journal_audit.update(
                        status="error", error_type=type(exc).__name__,
                        http_status=status,
                    )
                    report(
                        "journal_recall",
                        f"第 {idx}/{len(queries)} 组 Crossref 期刊补充检索"
                        + ("受到限流（HTTP 429），继续检索其他来源" if status == 429 else
                           f"暂不可用：HTTP {status}" if status else f"暂不可用：{type(exc).__name__}"),
                        query_index=idx,
                        query_count=len(queries),
                        error_type=type(exc).__name__,
                        elapsed_seconds=round(time.perf_counter() - journal_started, 3),
                    )
                return journal_results

            # TaskGroup cancels the sibling on failure/cancellation; no orphan requests.
            async with asyncio.TaskGroup() as group:
                source_task = group.create_task(search_sources())
                journal_task = group.create_task(search_journals())
            results, journal_results = source_task.result(), journal_task.result()
            existing_keys = {
                (getattr(paper, "doi", "") or "").strip().lower()
                or "title:" + " ".join((getattr(paper, "title", "") or "").lower().split())
                for paper in [*all_papers, *results]
            }
            for paper in journal_results:
                key = ((getattr(paper, "doi", "") or "").strip().lower()
                       or "title:" + " ".join((getattr(paper, "title", "") or "").lower().split()))
                if key and key not in existing_keys:
                    journal_recall_keys.add(key)
            query_batch = [*results, *journal_results]

            all_papers.extend(query_batch)
            audit = results.audit_dict()
            audit["journal_recall"] = journal_audit
            if journal_audit["status"] == "error":
                status = journal_audit.get("http_status")
                audit.setdefault("warnings", []).append({
                    "code": "journal_recall_unavailable", "sources": ["crossref"],
                    "http_status": status,
                    "detail": (
                        "Crossref 期刊补充检索失败"
                        + (f"（HTTP {status}）" if status else "")
                        + "，本轮结果可能缺少部分候选。"
                    ),
                })
            audits.append(audit)
            report(
                "search",
                f"第 {idx}/{len(queries)} 组检索完成，多源 {len(results)} 篇 + 期刊补充 {len(journal_results)} 篇",
                query_index=idx,
                query_count=len(queries),
                result_count=len(results) + len(journal_results),
                multi_source_count=len(results),
                journal_recall_count=len(journal_results),
            )
        finish_stage("source_search")
        title_join_started = time.perf_counter()
        report("exact_title", "正在按原始输入执行精确标题检索")
        try:
            title_results, title_error = await exact_title_task
            normalized_input = _normalized_academic_title(query)
            for paper in title_results:
                if known_title_papers or _normalized_academic_title(str(getattr(paper, "title", "") or "")) == normalized_input:
                    key = (getattr(paper, "doi", "") or "").strip().lower()
                    if not key:
                        key = "title:" + " ".join(
                            (getattr(paper, "title", "") or "").lower().split()
                        )
                    exact_title_keys.add(key)
            all_papers.extend(title_results)
            report(
                "exact_title",
                f"原始标题检索暂不可用：{title_error}" if title_error else
                f"原始标题检索完成，找到 {len(exact_title_keys)} 篇精确同名记录",
                exact_title_matches=len(exact_title_keys),
                **({"error_type": title_error} if title_error else {}),
            )
        except Exception as exc:
            report(
                "exact_title",
                f"原始标题检索暂不可用：{type(exc).__name__}",
                error_type=type(exc).__name__,
            )
        timings["exact_title_join_wait"] = round(time.perf_counter() - title_join_started, 3)

        post_started = time.perf_counter()
        report("dedup", f"{len(queries)} 组检索已完成，开始对累计 {len(all_papers)} 条记录全局去重", raw_count=len(all_papers))
        merged_papers = manager._merge_results([], all_papers)
        if year_from is not None:
            def within_year(paper):
                try:
                    return int(str(paper.year)[:4]) >= int(year_from)
                except (TypeError, ValueError):
                    return True  # Preserve unknown years for explicit evidence review.
            merged_papers = [paper for paper in merged_papers if within_year(paper)]
        unique = {paper_identity(paper): paper for paper in merged_papers if paper_identity(paper)}
        # Index 级复用：主题查询候选来自远程，完整标题查询可直接定位库内记录。
        # 其余本地论文只按身份补字段，不添加候选。
        local_index_hits = 0
        local_index_complete_hits = 0
        local_index_hit_keys: set[str] = set()
        local_index_complete_keys: set[str] = set()
        if manager.local_index and unique:
            cached_by_key = manager.local_index.get_papers_by_identity(list(unique.values()))
            reusable_fields = (
                "authors", "abstract", "year", "venue", "url", "pdf_url",
                "fields_of_study", "subjects", "pub_type", "pub_date", "keywords",
                "issn", "eissn", "nlm_id", "journal_abbr", "impact_factor",
            )
            for key, remote_paper in unique.items():
                cached_paper = cached_by_key.get(key)
                if cached_paper is None:
                    continue
                local_index_hits += 1
                local_index_hit_keys.add(key)
                for field in reusable_fields:
                    current = getattr(remote_paper, field, None)
                    cached_value = getattr(cached_paper, field, None)
                    if field == "abstract" and cached_value and not _cached_abstract_matches_title(
                        str(getattr(remote_paper, "title", "") or ""), str(cached_value)
                    ):
                        continue
                    if not current and cached_value:
                        setattr(remote_paper, field, cached_value)
                        remote_paper.metadata_provenance[field] = (
                            cached_paper.metadata_provenance.get(field) or "local_index"
                        )
                if int(cached_paper.citations or 0) > int(remote_paper.citations or 0):
                    remote_paper.metadata_provenance["citations"] = (
                        cached_paper.metadata_provenance.get("citations") or "local_index"
                    )
                remote_paper.citations = max(
                    int(getattr(remote_paper, "citations", 0) or 0),
                    int(getattr(cached_paper, "citations", 0) or 0),
                )
                if all(
                    getattr(remote_paper, field, None)
                    for field in ("title", "authors", "abstract", "year", "venue", "url")
                ):
                    local_index_complete_hits += 1
                    local_index_complete_keys.add(key)
            report(
                "index_cache",
                (
                    f"候选 Index 本地命中 {local_index_hits}/{len(unique)} 篇，"
                    f"完整 {local_index_complete_hits} 篇、不完整 "
                    f"{local_index_hits - local_index_complete_hits} 篇；库内命中均复用，不重复补齐"
                ),
                candidate_count=len(unique),
                cache_hit_count=local_index_hits,
                complete_cache_hit_count=local_index_complete_hits,
                cache_miss_count=len(unique) - local_index_hits,
            )

        # Ordinary searches enrich newly discovered papers only. Existing records
        # retain missing fields too; absence alone must not trigger repeated lookups.
        # Fresh metadata returned by this search's sources is still merged above.
        relevance_queries = list(dict.fromkeys([query, *queries]))
        anchor_terms = _academic_core_terms(queries[0])
        mandatory_facets = _academic_boolean_facets(
            str(strategy.get("english_query") or queries[0])
        )
        mandatory_facets = _academic_comparison_facets(mandatory_facets, query)
        if search_plan:
            # Unlike legacy heuristic facets, the structured core contains only
            # required concepts. Preserve every one, including fourth/later groups.
            mandatory_facets = [[" ".join(t.lower() for t in re.findall(r"[A-Za-z][A-Za-z0-9-]*|\d+(?:\.\d+)?", term)
                                          if t.lower() not in _ACADEMIC_QUERY_STOPWORDS) for term in group]
                                for group in strategy["concept_groups"]]
        period_focus = _academic_period_focus(
            str(strategy.get("english_query") or queries[0])
        )
        period_method_terms = _academic_period_method_terms(
            str(strategy.get("english_query") or queries[0])
        )
        obvious_missing = []
        fallback_missing = []
        for key, paper in unique.items():
            if key in local_index_hit_keys:
                continue
            # 摘要用于相关度判断；作者、年份、期刊和链接用于完整 Index 展示。
            # 任一核心字段缺失都进入补齐池，不因检索源已经返回标题而假装完整。
            if all(
                getattr(paper, field, None)
                for field in ("abstract", "authors", "year", "venue", "url")
            ):
                continue
            best_relevance = max(
                compute_relevance_score(paper, candidate)
                for candidate in relevance_queries
            )
            title = str(getattr(paper, "title", "") or "").lower()
            item = (best_relevance, key, paper)
            if (
                best_relevance >= _ACADEMIC_SEARCH_MIN_RELEVANCE
                and any(_academic_term_matches(term, title) for term in anchor_terms)
            ):
                obvious_missing.append(item)
            else:
                # 低相关候选也先形成与缓存命中项相同口径的 Index，再统一评分；
                # 最终相关度门槛仍会把不合格论文淘汰，缓存不参与准入。
                fallback_missing.append(item)
        obvious_missing.sort(key=lambda item: item[0], reverse=True)
        fallback_missing.sort(key=lambda item: item[0], reverse=True)
        prioritized = [*obvious_missing, *fallback_missing]
        seen_enrichment_keys: set[str] = set()
        enrichment_targets = []
        for _, key, paper in prioritized:
            if key in seen_enrichment_keys:
                continue
            seen_enrichment_keys.add(key)
            enrichment_targets.append(paper)

        finish_stage("dedup_and_cache")
        if enrichment_targets:
            report(
                "metadata_enrichment",
                (
                    f"正在首次补全 {len(enrichment_targets)} 篇本地未收录候选的 Index"
                ),
                target_count=len(enrichment_targets),
            )
            try:
                await asyncio.wait_for(
                    manager._enrich_metadata_cross_source(
                        enrichment_targets, candidate_metadata_sources
                    ),
                    timeout=20,
                )
            except asyncio.TimeoutError:
                report(
                    "metadata_enrichment",
                    "快速元数据补全达到 20 秒上限，保留已补内容并继续排序",
                    target_count=len(enrichment_targets),
                    source_status="timeout",
                )
        finish_stage("candidate_enrichment")
        candidate_enriched_keys = {
            (str(getattr(paper, "doi", "") or "").strip().lower()
             or "title:" + " ".join(
                 str(getattr(paper, "title", "") or "").lower().split()
             ))
            for paper in enrichment_targets
        }

        report(
            "scoring",
            f"正在生成 AI 对比推荐：综合 {len(unique)} 篇候选的相关度、分区、引用与时间",
            unique_count=len(unique),
            journal_recall_unique=len(journal_recall_keys),
            journal_enriched=len(enrichment_targets),
            dedup_seconds=round(time.perf_counter() - post_started, 3),
        )
        scoring_started = time.perf_counter()

        # 每篇论文分别与原始输入及每一组扩展查询比较，取最高相关度；不能把六组
        # 拼成一个超长查询后再除以全部词数，否则真正命中某一研究角度的论文会被稀释。
        def score_unique_papers() -> list[dict[str, Any]]:
            scored = []
            for key, paper in unique.items():
                assessment = None
                if search_plan:
                    from nodes.literature.tools.bilingual_strategy import scoring_query
                    from nodes.literature.tools.academic_admission import assess_concepts
                    best_query = scoring_query(paper, strategy["concept_groups"])
                    assessment = assess_concepts(paper, strategy, _ACADEMIC_SEARCH_MIN_RELEVANCE)
                else:
                    best_query = max(relevance_queries, key=lambda candidate: compute_relevance_score(paper, candidate))
                indexed = _index_paper(paper, best_query, relevance=assessment["relevance"] if assessment else None)
                if assessment:
                    indexed["relevance_assessment"] = assessment
                indexed["exact_title_match"] = key in exact_title_keys
                scored.append(indexed)
            from shared.contrib.publication_relations import link_supplements
            link_supplements(scored)
            reviewer.submit([p for p in scored if not _academic_nonresearch_record(p)])
            for indexed in scored:
                reviewer.apply(indexed)
            return scored

        scored_papers = score_unique_papers()
        # 中文原始问题不会逐字出现在英文论文里；此时第一组英文检索词就是等价锚点。
        anchor_query = queries[0] if re.search(r"[\u4e00-\u9fff]", query) else query
        if search_plan:
            anchor_query = " ".join(group[0] for group in strategy["concept_groups"])
        papers, low_relevance_count = _select_academic_search_results(
            scored_papers,
            limit=output_limit,
            core_terms=_academic_core_terms(anchor_query),
            mandatory_facets=mandatory_facets,
            period_focus=period_focus,
            method_terms=period_method_terms,
            strict_core=bool(search_plan),
        )

        finish_stage("initial_scoring")
        # 全局补全有时间上限，可能在长候选列表尾部尚未处理到最终真正入选的论文。
        # 因此初排后只针对最终展示结果再做一次快速 DOI 补齐；不访问出版商网页/PDF，
        # 也不跨 DOI 猜测版本。摘要变化后必须重新评分，否则补回的摘要不会影响排序。
        final_missing_targets = []
        seen_final_missing: set[str] = set()
        for indexed in papers:
            if str(indexed.get("abstract") or "").strip():
                continue
            key = (str(indexed.get("doi") or "").strip().lower()
                   or "title:" + " ".join(str(indexed.get("title") or "").lower().split()))
            raw_paper = unique.get(key)
            if key in local_index_hit_keys:
                continue
            if not raw_paper or not str(getattr(raw_paper, "doi", "") or "").strip():
                continue
            if key not in seen_final_missing:
                seen_final_missing.add(key)
                final_missing_targets.append(raw_paper)
            if len(final_missing_targets) >= 50:
                break

        if final_missing_targets:
            before_filled = sum(
                bool(str(getattr(paper, "abstract", "") or "").strip())
                for paper in final_missing_targets
            )
            # 所有最终缺摘要论文都只调用一次 PubMed。若某篇此前未进入候选补齐，
            # 再补做一次 OpenAlex/OpenAIRE；已处理过的 DOI 不重复。
            final_fast_targets = list(final_missing_targets)
            final_candidate_targets = [
                paper
                for paper in final_missing_targets
                if ((str(getattr(paper, "doi", "") or "").strip().lower()
                     or "title:" + " ".join(
                         str(getattr(paper, "title", "") or "").lower().split()
                     )) not in candidate_enriched_keys)
            ]
            report(
                "final_abstract_enrichment",
                (
                    f"正在快速补齐 {len(final_fast_targets)} 篇最终缺摘要论文；"
                    f"其中 {len(final_candidate_targets)} 篇补查 OpenAlex/OpenAIRE"
                ),
                target_count=len(final_fast_targets),
                candidate_source_target_count=len(final_candidate_targets),
                enrichment_tier="fast",
            )
            fast_jobs = [
                manager._enrich_metadata_cross_source(
                    final_fast_targets, final_metadata_sources
                )
            ]
            if final_candidate_targets:
                fast_jobs.append(
                    manager._enrich_metadata_cross_source(
                        final_candidate_targets, candidate_metadata_sources
                    )
                )
            try:
                await asyncio.wait_for(asyncio.gather(*fast_jobs), timeout=15)
            except asyncio.TimeoutError:
                report(
                    "final_abstract_enrichment",
                    "最终候选快速补齐达到 15 秒上限，继续慢速兜底",
                    target_count=len(final_fast_targets),
                    source_status="timeout",
                    enrichment_tier="fast",
                )

            # 出版商落地页和 OA PDF 只服务最终展示结果；每个 DOI 在本轮仅进入一次。
            final_slow_targets = [
                paper for paper in final_missing_targets
                if not str(getattr(paper, "abstract", "") or "").strip()
            ]
            slow_status = "success"
            if final_slow_targets:
                report(
                    "final_abstract_enrichment",
                    f"正在对 {len(final_slow_targets)} 篇最终候选执行网页/PDF慢速兜底",
                    target_count=len(final_slow_targets),
                    enrichment_tier="slow",
                )
                try:
                    await asyncio.wait_for(
                        manager._enrich_metadata_cross_source(
                            final_slow_targets, slow_abstract_sources
                        ),
                        timeout=12,
                    )
                except asyncio.TimeoutError:
                    slow_status = "timeout"
            after_filled = sum(
                bool(str(getattr(paper, "abstract", "") or "").strip())
                for paper in final_missing_targets
            )
            report(
                "final_abstract_enrichment",
                (
                    f"最终候选摘要补齐完成：新增 {after_filled - before_filled} 篇"
                    + ("（慢速兜底达到 12 秒上限）" if slow_status == "timeout" else "")
                ),
                target_count=len(final_missing_targets),
                fast_target_count=len(final_fast_targets),
                candidate_source_target_count=len(final_candidate_targets),
                slow_target_count=len(final_slow_targets),
                filled_count=after_filled - before_filled,
                source_status=slow_status,
            )
            scored_papers = score_unique_papers()
            papers, low_relevance_count = _select_academic_search_results(
                scored_papers,
                limit=output_limit,
                core_terms=_academic_core_terms(anchor_query),
                mandatory_facets=mandatory_facets,
                period_focus=period_focus,
                method_terms=period_method_terms,
                strict_core=bool(search_plan),
            )
        await reviewer.finish()
        scored_papers = score_unique_papers()
        papers, low_relevance_count = _select_academic_search_results(
            scored_papers, limit=output_limit, core_terms=_academic_core_terms(anchor_query),
            mandatory_facets=mandatory_facets, period_focus=period_focus,
            method_terms=period_method_terms, strict_core=bool(search_plan),
        )
        diagnostics["evidence_review"] = reviewer.diag
        if category_limit:
            papers, accepted = apply_budget(papers, accepted, category_limit)
            if category_paging:
                next_groups = {key:value for key,value in next_groups.items()
                               if len(accepted.get(paging_entries[key][1], [])) < category_limit}
            elif all(len(accepted.get(category, [])) >= category_limit for category in CATEGORIES):
                next_groups = {}
            diagnostics["category_counts"] = {key:len(rows) for key,rows in accepted.items()}
        if manager.local_index and unique:
            manager.local_index.add_papers(list(unique.values()), query)

        finish_stage("final_enrichment_and_rescoring")
        # Search returns the Index first. The UI translates only the visible page,
        # while the shared SQLite cache still supplies translations from earlier runs.
        report("translation", "检索结果已就绪；当前页标题和摘要按需翻译", target_count=len(papers))
        if manager.local_index and hasattr(manager.local_index, "hydrate_translations"):
            manager.local_index.hydrate_translations(papers)
            manager.local_index.record_paper_translations(papers)

        finish_stage("title_translation")
        if manager.local_index and hasattr(manager.local_index, "hydrate_translations"):
            manager.local_index.hydrate_translations(papers)
            manager.local_index.record_paper_translations(papers)

        # 学术搜索只获取 Index，但仍创建统一的三要素目录；paper/figure 明确标记为
        # 本流程未尝试，交给七天一次的后台补齐任务处理。
        if papers:
            try:
                from core.paths import literature_papers_dir
                from nodes.literature.tools.archive_papers import archive_indexes_only

                await asyncio.to_thread(
                    archive_indexes_only,
                    papers,
                    str(literature_papers_dir()),
                )
            except Exception as exc:
                report("finalize", f"统一论文目录写入暂不可用：{type(exc).__name__}")

        report(
            "finalize",
            (
                f"相关性筛选与排序完成（{time.perf_counter() - scoring_started:.1f} 秒）："
                f"淘汰 {low_relevance_count} 篇低相关候选，保留 {len(papers)} 篇"
            ),
            scoring_seconds=round(time.perf_counter() - scoring_started, 3),
            candidate_count=len(scored_papers),
            excluded_low_relevance=low_relevance_count,
            result_count=len(papers),
        )
        source_counts = {}
        for paper in papers:
            source = paper["source"].split("/", 1)[0]
            source_counts[source] = source_counts.get(source, 0) + 1
        exclusion_counts: dict[str, int] = {}
        exclusion_samples: dict[str, list[dict[str, Any]]] = {}
        for candidate in scored_papers:
            reason = _academic_exclusion_reason(
                candidate, core_terms=_academic_core_terms(anchor_query),
                mandatory_facets=mandatory_facets, period_focus=period_focus,
                method_terms=period_method_terms,
                strict_core=bool(search_plan),
            )
            if reason:
                exclusion_counts[reason] = exclusion_counts.get(reason, 0) + 1
                samples = exclusion_samples.setdefault(reason, [])
                if len(samples) < 3:
                    samples.append({key: candidate.get(key) for key in ("title", "doi", "abstract", "score_breakdown")})
        diagnostics["admission"] = {
            "candidate_count": len(scored_papers), "excluded_by_reason": exclusion_counts,
            "eligible_count": len(scored_papers) - sum(exclusion_counts.values()),
            "returned_count": len(papers), "excluded_samples": exclusion_samples,
            "counting": "first_failing_gate",
            "direct_count": sum((p.get("relevance_assessment") or {}).get("tier", "direct") == "direct" for p in papers),
            "unconfirmed_count": sum((p.get("relevance_assessment") or {}).get("tier") == "unconfirmed" for p in papers),
            "background_count": sum((p.get("relevance_assessment") or {}).get("tier") == "background" for p in papers),
        }
        diagnostics["source_audits"] = audits
        diagnostics["metadata_reuse"] = {
            "cached_count": local_index_hits,
            "cached_with_missing_fields": local_index_hits - local_index_complete_hits,
            "enrichment_target_count": len(enrichment_targets),
            "final_enrichment_target_count": len(final_missing_targets),
        }
        diagnostics["selection_rules"] = {
            "retrieval_mode": strategy.get("retrieval_mode"),
            "publication_years": strategy.get("publication_years", {}),
            "study_requirements": strategy.get("study_requirements", []),
            "scope_adjustments": strategy.get("scope_adjustments", []),
            "decomposed_queries": queries, "mandatory_facets": mandatory_facets,
            "minimum_relevance": _ACADEMIC_SEARCH_MIN_RELEVANCE,
            "core_concepts_zh": strategy.get("core_concepts_zh", []),
            "background_optional_concepts": strategy.get("background_optional_concepts", []),
            "research_aspects": [a["label"] for a in strategy.get("research_aspects", [])],
        }
        warnings = _academic_search_warnings(audits)
        if low_relevance_count:
            warnings.append({
                "code": "low_relevance_candidates_excluded",
                "detail": (
                    f"已淘汰 {low_relevance_count} 篇未通过相关性或研究条件检查的候选；"
                    "各原因见 admission 诊断，结果不会补足到请求数量。"
                ),
            })
        if not remote_refresh and not papers:
            warnings.append({
                "code": "remote_search_no_match",
                "detail": "已启用的远程来源没有返回达到相关度门槛的记录。",
            })
        # 真正的“完成”由 API 在结果通过契约校验并写入 SSE done 事件后表达。
        # 此处若先声称完成，随后序列化/校验失败，界面会停在一个误导性的状态。
        report(
            "finalize",
            f"检索与排序完成，正在整理 {len(papers)} 篇结果",
            progress_percent=99,
        )
        finish_stage("finalize")
        timings["total"] = round(time.perf_counter() - started, 3)
        _record_literature_performance(
            "search", request_id=request.get("request_id"), query=query,
            timings=timings, candidate_count=len(scored_papers), returned_count=len(papers),
            index_cache_hits=local_index_hits, complete_index_cache_hits=local_index_complete_hits,
            metadata_reuse=diagnostics["metadata_reuse"],
            selection_rules=diagnostics["selection_rules"],
            admission={k: v for k, v in diagnostics["admission"].items() if k != "excluded_samples"},
            category_counts={
                category: sum(p.get("publication_category") == category for p in papers)
                for category in sorted({str(p.get("publication_category") or "") for p in papers})
            },
            source_status=[
                {"primary": {
                    source: {key: value for key, value in detail.items()
                             if key in {"status", "raw_count", "deduped_count", "elapsed_seconds", "http_status"}}
                    for source, detail in audit.get("source_diagnostics", {}).items()
                 },
                 "journal_recall": audit.get("journal_recall", {})}
                for audit in audits
            ],
        )
        return {"query": query, "search_plan": search_plan, "decomposed_queries": queries, "papers": papers, "total": len(papers), "candidate_count": len(scored_papers), "excluded_low_relevance": low_relevance_count, "minimum_relevance": _ACADEMIC_SEARCH_MIN_RELEVANCE, "required_core_terms": _academic_core_terms(queries[0]), "source_counts": source_counts, "requested_sources": sorted(sources), "attempted_sources": sorted({s for a in audits for s in a.get("attempted_sources", [])}), "unavailable_sources": sorted({s for a in audits for s in a.get("unavailable_sources", [])}), "warnings": warnings, "from_cache": all(bool(a.get("from_cache")) for a in audits) if audits else False, "search_mode": "remote", "timings": timings, "strategy_diagnostics": {k:v for k,v in diagnostics.items() if k != "started_at"}, "continuation": {"version":1,"query":query_key(query),"pagination_scope":pagination_scope,"groups":next_groups, **({"per_category_limit":category_limit,"accepted":accepted} if category_limit else {})} if next_groups else None}

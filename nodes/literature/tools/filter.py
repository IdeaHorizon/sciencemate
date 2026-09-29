"""文献质量筛选引擎"""

import math
from datetime import datetime
from typing import Optional
import re

from .config import QUALITY_WEIGHTS, CITATION_RANGES
from .search_engines import Paper


# ---------- 期刊/会议等级评分 ----------

# 常用学术期刊/会议等级（简单版，可扩展）
VENUE_SCORES = {
    # AI/ML 顶会
    "neurips": 10, "nips": 10,
    "icml": 10,
    "iclr": 10,
    "aaai": 8, "ijcai": 8,
    "acl": 9, "emnlp": 9, "naacl": 8,
    "cvpr": 10, "iccv": 10, "eccv": 8,
    "sigmod": 9, "vldb": 9,
    "osdi": 10, "sosp": 10,
    "pldi": 9, "popl": 10,
    # 综合科学期刊
    "nature": 10, "science": 10, "cell": 9,
    "pnas": 8,
    "nature communications": 8, "nat commun": 8,
    "science advances": 8,
    # AI/ML 期刊
    "jmlr": 9,
    "tpami": 9, "ieee tpami": 9,
    "machine learning": 7,
    "neural networks": 6,
    # 生物医学
    "bioinformatics": 7,
    "plos one": 5,
    "scientific reports": 5,
    # 通用
    "arxiv": 2,
}

# 可手动扩展
CUSTOM_VENUE_SCORES: dict[str, int] = {}
_venue_scores = {**VENUE_SCORES, **CUSTOM_VENUE_SCORES}


def _normalize_venue(venue: str) -> str:
    """标准化会议/期刊名"""
    if not venue:
        return ""
    v = venue.lower().strip()
    # 去掉标点和多余词
    v = v.replace(".", "").replace(",", "").replace(":", "").replace("'", "")
    v = v.replace("proceedings of the ", "").replace("international ", "")
    v = v.replace("conference on ", "").replace("journal of ", "")
    v = v.strip()
    return v


def score_venue(venue: str) -> float:
    """期刊评分：用 IF，IF=20 为满分 (0-20)"""
    if not venue:
        return 1.0
    try:
        from .jcr_ranking import lookup_if
        if_val = lookup_if(venue)
        if if_val is not None and if_val > 0:
            return min(20.0, if_val)
    except Exception:
        pass
    # IF 未收录时，用关键词匹配兜底
    v = _normalize_venue(venue)
    for key, score in _venue_scores.items():
        if key in v or v in key:
            return score
    return 3.0  # 未知期刊默认


# ---------- 核心评分函数 ----------

def score_citations(citations: int) -> float:
    """引用量归一化 (0-1)"""
    # 新论文（1年内）引用量可能为0，给一个基础分
    if citations <= 0:
        return 0.15  # 基础分，避免新论文直接被刷掉
    max_citations = CITATION_RANGES["very_high"]
    return min(1.0, math.log2(1 + citations) / math.log2(1 + max_citations))


def score_recency(year: Optional[int]) -> float:
    """时效性评分 (0-1)，越新越高"""
    if year is None:
        return 0.5  # 未知年份给中等分
    current_year = datetime.now().year
    age = current_year - year
    if age <= 1:
        return 1.0  # 当年/上一年
    elif age <= 2:
        return 0.95
    elif age <= 3:
        return 0.9
    elif age <= 5:
        return 0.8
    elif age <= 10:
        return 0.6
    elif age <= 20:
        return 0.4
    else:
        return 0.2


def score_author_influence(authors: list[str]) -> float:
    """作者影响力评分 (0-1)，暂置 0"""
    return 0.0


def _is_journal_article(paper: Paper) -> bool:
    """仅期刊文章才允许使用期刊分区和影响因子。"""
    source = str(getattr(paper, "source", "") or "").lower().split("/", 1)[0]
    if source in {"arxiv", "biorxiv", "medrxiv"}:
        return False
    if source == "pubmed":
        return True
    pub_type = str(getattr(paper, "pub_type", "") or "").lower()
    if "book" in pub_type or "proceedings" in pub_type or "conference" in pub_type:
        return False
    return pub_type in {"journal", "journal-article", "article", "article-journal"}


# ---------- 综合评分 ----------

def compute_relevance_score(paper: Paper, query: str = "") -> float:
    """标题/摘要查询相关度，归一化到 0..1。"""
    # 数字也是检索约束，尤其是日期、版本号、温度、模型代号等。旧实现把独立
    # 数字全部丢掉，`September 4` 实际只按 `September` 评分，于是 9 月 11 日
    # 的论文也能靠期刊/时间分混进来。
    terms = [
        t.lower()
        for t in re.findall(
            r"[A-Za-z][A-Za-z0-9-]{2,}|\d+(?:\.\d+)?|[\u4e00-\u9fff]{2,}",
            query or "",
        )
        if t
    ]
    if not terms:
        return 0.0
    title = (paper.title or "").lower()
    abstract = (paper.abstract or "").lower()
    matched = 0.0
    for term in terms:
        # 数字不能用普通子串匹配：查询 `4` 不应命中 `2024` 或 `14`。
        if term[0].isdigit():
            pattern = rf"(?<!\d){re.escape(term)}(?!\d)"
            in_title = re.search(pattern, title) is not None
            in_abstract = re.search(pattern, abstract) is not None
        else:
            in_title = term in title
            in_abstract = term in abstract
        if in_title:
            matched += 1.0
        elif in_abstract:
            # 仅摘要命中，低于标题命中。
            matched += 0.5
    return min(1.0, matched / len(terms))


def quality_score_breakdown(paper: Paper, relevance: float = 0.0) -> dict:
    """返回可审计的分项分数与加权贡献。"""
    cit_score = score_citations(paper.citations)
    rec_score = score_recency(paper.year)
    is_journal = _is_journal_article(paper)
    metrics = {}
    if is_journal:
        try:
            from .journal_metrics import lookup_journal_metrics
            metrics = lookup_journal_metrics(
                paper.venue or "",
                journal_abbr=getattr(paper, "journal_abbr", ""),
                issn=getattr(paper, "issn", ""),
                eissn=getattr(paper, "eissn", ""),
                nlm_id=getattr(paper, "nlm_id", ""),
            )
        except Exception:
            metrics = {}
    cas_q = metrics.get("cas_quartile")
    is_top = bool(metrics.get("cas_top"))
    if is_journal and cas_q is None:
        try:
            from .cas_ranking import lookup_cas
            cas_q, is_top = lookup_cas(paper.venue or "")
        except Exception:
            cas_q, is_top = None, False
    if cas_q is None:
        # “没有查到分区”不等于“不是期刊”。缺失分区按该维最低分处理，
        # 不能通过删除分区轴让未收录期刊反而绕开分区劣势。
        cas_score = 0.0
    else:
        cas_score = min(1.0, {1: 1.0, 2: 0.8, 3: 0.5, 4: 0.3}.get(int(cas_q), 0.35) + (0.1 if is_top else 0.0))
    if is_journal:
        weights = {"relevance": 0.70, "cas": 0.10, "citation": 0.10, "recency": 0.10}
        contributions = {"relevance": relevance * 0.70, "cas": cas_score * 0.10,
                         "citation": cit_score * 0.10, "recency": rec_score * 0.10}
    else:
        weights = {"relevance": 0.70, "citation": 0.15, "recency": 0.15}
        contributions = {"relevance": relevance * 0.70, "citation": cit_score * 0.15,
                         "recency": rec_score * 0.15}
    return {
        "is_journal": is_journal,
        "has_cas": cas_q is not None, "cas_quartile": cas_q, "cas_top": is_top,
        "cas_year": metrics.get("cas_year"),
        "impact_factor": metrics.get("impact_factor"),
        "impact_factor_year": metrics.get("impact_factor_year"),
        "jcr_quartile": metrics.get("jcr_quartile"),
        "journal_metrics_match": metrics.get("match_method"),
        "weights": weights,
        "raw": {"relevance": round(relevance, 4), "cas": round(cas_score, 4),
                "citation": round(cit_score, 4), "recency": round(rec_score, 4)},
        "contributions": {k: round(v, 4) for k, v in contributions.items()},
        "total": round(sum(contributions.values()), 4),
    }


def compute_quality_score(paper: Paper, relevance: float = 0.0) -> float:
    return quality_score_breakdown(paper, relevance).get("total", 0.0)


def filter_and_rank(
    papers: list[Paper],
    min_year: Optional[int] = None,
    min_citations: int = 0,
    top_n: Optional[int] = None,
    min_score: float = 0.0,
    query_terms: list[str] = None,
    lang_mode: str = "auto",
    chinese_query: str = "",
) -> list[Paper]:
    """筛选 + 排序
    
    排序优先级:
    1. 查询相关性 (match_count) — 论文标题/摘要匹配查询词的数量
    2. 质量分 (quality_score) — CAS分区+引用量+时效性等
    
    lang_mode: 'auto' — 英文术语去除0匹配论文
               'cn'   — 中文匹配，不过滤0匹配
               'en'   — 英文匹配，过滤0匹配
    query_terms: 英文术语列表。如果传入中文查询(如"海洋微塑料 沉降")，
    会自动通过词典翻译后再做匹配，避免中文整串无法匹配。
    """
    
    # 计算质量分
    for p in papers:
        p.score = compute_quality_score(p)
    
    # 准备匹配术语：中文查询需翻译成英文后再匹配
    match_terms = []
    full_query = ' '.join(query_terms) if query_terms else ''
    if query_terms:
        has_chinese = any(any('\u4e00' <= c <= '\u9fff' for c in t) for t in query_terms)
        if lang_mode == 'cn':
            # 中文模式：不翻译，不过滤0匹配，CNKI论文已自带相关度排序
            # match_terms 仅保留原文（用于精确匹配）和英文缩写
            for t in query_terms:
                tl = t.lower().strip()
                if tl and tl not in match_terms:
                    match_terms.append(tl)
            import re
            eng_abbr = re.findall(r'[a-zA-Z]{2,}', ' '.join(query_terms))
            for ea in eng_abbr:
                if ea.lower() not in match_terms:
                    match_terms.append(ea.lower())
        elif has_chinese:
            # 中英混合查询：保留原文里的英文词 + 中文字串本身做匹配项。
            # 不再用硬编码词典翻译——中文→英文翻译已由 SearchManager 的框架
            # LLM 翻译在检索阶段完成，这里只按现有词面算相关度。
            # 也保留原文中的英文词
            for t in query_terms:
                t_lower = t.lower()
                if not any('\u4e00' <= c <= '\u9fff' for c in t) and len(t_lower) >= 2:
                    if t_lower not in match_terms:
                        match_terms.append(t_lower)
            # 中文分词：提取查询中的中文字符作为额外匹配项
            import re
            chinese_chars = re.findall(r'[\u4e00-\u9fff]{2,}', full_query)
            for ch in chinese_chars:
                if ch not in [m for m in match_terms if '\u4e00' <= m[0] <= '\u9fff']:
                    match_terms.append(ch)
        else:
            match_terms = [t.lower() for t in query_terms]
            # 补充短语中的独立词（全匹配太严格，单匹配太宽泛，三角数加权会自动平衡）
            _seen_words = set()
            import re as _wsplit
            for t in query_terms:
                for w in _wsplit.split(r'[\s\-/]+', t):
                    wl = w.lower().strip("(),.")
                    if len(wl) >= 4 and wl not in _seen_words and wl not in ('with', 'from', 'that', 'this', 'their', 'them', 'they', 'have', 'been', 'were', 'which', 'when', 'what', 'about'):
                        _seen_words.add(wl)
            for w in sorted(_seen_words):
                if w not in match_terms:
                    match_terms.append(w)
    
    def compute_relevance(p: Paper) -> int:
        """计算论文与查询的相关性: 匹配到的术语数量（中英文混合）"""
        if not match_terms:
            return 0
        text = f"{p.title} {p.abstract or ''} {p.venue or ''}".lower()
        match_count = 0
        for term in match_terms:
            if term in text:
                match_count += 1
        return match_count
    
    # 硬筛选
    filtered = []
    for p in papers:
        if min_year and p.year and p.year < min_year:
            continue
        if p.citations < min_citations:
            continue
        if p.score < min_score:
            continue
        filtered.append(p)
    
    # 排序: 相关度×0.75 + 质量分×0.25
    # 引用量已在质量分内部（compute_quality_score 的 citation_count 因子）
    max_terms = max(1, len(match_terms))
    def _relevance_score(p):
        """按位置加权：标题x3 > 关键词x2 > 摘要x1 > 期刊x0.5"""
        title = (p.title or "").lower()
        keywords = " ".join(getattr(p, 'keywords', []) or []).lower()
        abstract = (p.abstract or "").lower()
        venue = (p.venue or "").lower()
        n = 0
        for t in match_terms:
            if t in title:
                n += 3
            elif t in keywords:
                n += 2
            elif t in abstract:
                n += 1
            elif t in venue:
                n += 0.5
        # 三角数加权：匹配越多词总分非线性飙升
        # 但用加权分而不是简单计数
        return n * (n + 1) / 2 / max_terms / 3  # 除以3归一化
    filtered.sort(key=lambda p: (
        0.75 * _relevance_score(p) + 0.25 * p.score,
        p.score
    ), reverse=True)
    
    # 当匹配词为英文（LLM 翻译）时，过滤掉完全不匹配的论文
    # 中文模式（lang_mode='cn'）跳过此过滤，允许中文论文即使不完全匹配也保留
    has_eng_terms = any(all(ord(c) < 128 for c in t) for t in match_terms) if match_terms else False
    if has_eng_terms and lang_mode != 'cn':
        # 中文论文跳过 relevance 检查（中文标题匹配不到英文字）
        def _has_cjk(text):
            return any('\u4e00' <= c <= '\u9fff' for c in (text or ''))
        filtered = [p for p in filtered
                    if _has_cjk(p.title) or _relevance_score(p) > 0]
        # 最低匹配率: 只计算英文词，中文串对英文论文永远不匹配
        eng_terms = [t for t in match_terms if all(ord(c) < 128 for c in t)]
        n_terms = len(eng_terms)
        if n_terms <= 2:
            min_match = n_terms
        elif n_terms <= 4:
            min_match = max(2, n_terms - 1)
        else:
            min_match = max(3, int(n_terms * 0.4 + 0.5))  # ≥40%, 最少3
        def _match_count(p):
            text = f"{(p.title or '').lower()} {(p.abstract or '').lower()}"
            return sum(1 for t in eng_terms if t in text)
        def _is_chinese_paper(p):
            """标题+摘要中 CJK 字符占比 > 30% 视为中文论文"""
            text = (p.title or "") + (p.abstract or "")
            if not text:
                return False
            cjk = sum(1 for c in text if '\u4e00' <= c <= '\u9fff')
            return cjk / len(text) > 0.30
        before = len(filtered)
        # 中文论文: 必须匹配至少 40% 的查询中文词
        # 中文词直接从查询里按 2-4 字滑窗切出（不再依赖硬编码词典）
        import re as _cnre
        _cn_source = chinese_query if chinese_query else full_query
        _cn_runs = _cnre.findall(r'[\u4e00-\u9fff]{2,}', _cn_source)
        cn_keywords = []
        for _run in _cn_runs:
            for _w in range(2, 5):
                for _i in range(len(_run) - _w + 1):
                    _g = _run[_i:_i + _w]
                    if _g not in cn_keywords:
                        cn_keywords.append(_g)
        cn_min_match = max(1, int(len(cn_keywords) * 0.4 + 0.5)) if cn_keywords else 1
        def _cn_match_count(p):
            """中文论文: 标题中匹配到的查询中文词数目"""
            title = p.title or ""
            return sum(1 for kw in cn_keywords if kw in title)
        filtered = [p for p in filtered
                    if p.source == 'cnki'
                    or (_is_chinese_paper(p) and _cn_match_count(p) >= cn_min_match)
                    or (not _is_chinese_paper(p) and _match_count(p) >= min_match)]
        after = len(filtered)
        if before != after:
            import logging
            logging.warning(f"最低匹配率过滤: {before}→{after} (阈值≥{min_match}/{n_terms})")
    
    if top_n:
        return filtered[:top_n]
    return filtered


def format_paper_list(papers: list[Paper]) -> str:
    """格式化为可读的文本"""
    lines = []
    for i, p in enumerate(papers, 1):
        badge = "⭐" if p.score >= 0.8 else "📍" if p.score >= 0.6 else "  "
        authors_str = ", ".join(p.authors[:5])
        if len(p.authors) > 5:
            authors_str += " et al."
        lines.append(f"{badge} **{p.title}**")
        lines.append(f"   *作者:* {authors_str}")
        lines.append(f"   *年份:* {p.year} | *引用:* {p.citations} | *来源:* {p.source} | *期刊:* {p.venue}")
        lines.append(f"   *质量分:* {p.score:.3f} | *链接:* {p.url or p.doi or 'N/A'}")
        lines.append("")
    return "\n".join(lines)

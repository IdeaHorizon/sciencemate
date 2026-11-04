"""Bilingual concept evidence and conservative background-paper admission.

No additional model call or domain dictionary: equivalent phrases come from the
same search plan. Each concept has equal weight regardless of language/word count.
"""
import html
import re


def _text(value):
    value = re.sub(r"<[^>]+>", " ", html.unescape(str(value or ""))).lower()
    return re.sub(r"[\-\u2010-\u2015]", " ", value)


def _terms(value):
    return re.findall(r"[\u3400-\u9fff]+|[a-z][a-z0-9]*|\d+(?:\.\d+)?", _text(value))


def _contains(term, text):
    if re.search(r"[\u3400-\u9fff]", term):
        return term in text
    # Whole tokens, with ordinary English plurals. AI must not match 'training'.
    alternatives = [term]
    if len(term) > 3 and term.endswith("s") and not term.endswith("ss"):
        alternatives.append(term[:-1])
    elif len(term) > 3:
        alternatives.append(term + "s")
    return bool(re.search(r"(?<![a-z0-9])(?:" + "|".join(map(re.escape, alternatives)) + r")(?![a-z0-9])", text))


def _evidence(alternatives, title, abstract):
    best_score = 0.0
    matched = title_matched = False
    for phrase in alternatives:
        terms = _terms(phrase)
        if not terms:
            continue
        in_title = [_contains(term, title) for term in terms]
        in_text = [yes or _contains(term, abstract) for yes, term in zip(in_title, terms)]
        best_score = max(best_score, sum(1 if top else .5 if body else 0
                                        for top, body in zip(in_title, in_text)) / len(terms))
        matched |= all(in_text)
        title_matched |= all(in_title)
    return best_score, matched, title_matched


def _title_support(alternatives, title):
    return max((sum(_contains(term, title) for term in terms) / len(terms)
                for phrase in alternatives if (terms := _terms(phrase))), default=0)


def _distinct_title_cues(groups, title):
    """Different core concepts need different title evidence. Abstracts may
    supply the common material/population modifier omitted by a concise title.
    A shared word ('metal' in both concepts) cannot stand in for both topics.
    """
    if len(groups) < 2:
        return False
    words = [set(t for phrase in group for t in _terms(phrase)
                 if len(t) > 2 and t not in {'the', 'and', 'for', 'with', 'from', 'into'})
             for group in groups]
    for i, own in enumerate(words):
        other = set().union(*(w for j, w in enumerate(words) if j != i))
        # Compare inflected tokens too: metal/metals remain a shared modifier.
        distinct = [t for t in own if not any(_contains(t, w) for w in other)]
        if not any(_contains(t, title) for t in distinct):
            return False
    return True


def assess_concepts(paper, strategy, minimum_relevance=.4):
    get = paper.get if isinstance(paper, dict) else lambda key, default="": getattr(paper, key, default)
    title, abstract = _text(get("title")), _text(get("abstract"))
    english = strategy.get("concept_groups") or []
    chinese = strategy.get("core_concepts_zh") or []
    if not english:
        return None
    groups = [list(dict.fromkeys([*group, *(chinese[i] if i < len(chinese) else [])]))
              for i, group in enumerate(english)]
    # With just one core, an acronym alone has no second concept to disambiguate
    # it (e.g. the same abbreviation can name a drug and a computer architecture).
    # Keep full names when available; an acronym-only user topic remains valid.
    if len(groups) == 1 and any(len(_terms(t)) > 1 for t in english[0]):
        full_names = [t for t in groups[0] if not re.fullmatch(r"[A-Z][A-Z0-9-]{1,9}", t)]
        if not _evidence(full_names, title, abstract)[1]:
            groups[0] = full_names
    labels = [chinese[i][0] if i < len(chinese) and chinese[i] else group[0]
              for i, group in enumerate(english)]
    evidence = [_evidence(group, title, abstract) for group in groups]
    missing = {i for i, (_, matched, _) in enumerate(evidence) if not matched}
    relevance = sum(item[0] for item in evidence) / len(evidence)
    title_count = sum(item[2] for item in evidence)
    distributed_title_anchor = not missing and _distinct_title_cues(groups, title)
    optional = set(strategy.get("background_optional_concepts") or [])
    excluded = any(_evidence([*concept.get("en", []), *concept.get("zh", [])], title, abstract)[1]
                   for concept in strategy.get("exclude_concepts", []))
    reason = ("explicit_exclusion" if excluded else "relevance" if relevance < minimum_relevance
              else "anchor" if not title_count and not distributed_title_anchor else "facets" if missing else None)
    background = (reason == "facets" and len(optional) == 1 and missing <= optional
                  and len(groups) - len(optional) >= 2
                  and any(item[2] for i, item in enumerate(evidence) if i not in optional))
    # A paper can address one part of a research proposal. Match each aspect
    # separately; requiring their union recreates the over-constrained query.
    aspects = strategy.get("research_aspects") or []
    matched_aspects = [aspect["label"] for aspect in aspects
                       if aspect.get("concepts") and all(
                           _evidence([*c.get("en", []), *c.get("zh", [])], title, abstract)[1]
                           for c in aspect["concepts"])]
    topic_background = reason is None and bool(aspects) and not matched_aspects
    # Without a matching research aspect, require visible title evidence for
    # each core. An incidental mention in the abstract is insufficient to
    # admit a paper about a different population or research object.
    if topic_background and not distributed_title_anchor and any(_title_support(group, title) < .5 for group in groups):
        topic_background = False
        reason = "anchor"
    background = background or topic_background
    tier = "background" if background else "direct" if reason is None else "excluded"
    return {
        "tier": tier, "relevance": round(relevance, 4),
        "coverage": round((len(groups) - len(missing)) / len(groups), 4),
        "title_coverage": round(title_count / len(groups), 4),
        "matched_concepts": [label for i, label in enumerate(labels) if i not in missing],
        "missing_concepts": [label for i, label in enumerate(labels) if i in missing],
        "matched_aspects": matched_aspects,
        "exclusion_reason": None if background else reason,
        "detail": ("背景参考：已匹配核心主题，题录尚未体现具体研究方面" if topic_background else
                   "背景参考：未覆盖情境“" + "、".join(labels[i] for i in sorted(missing))
                   + "”，已匹配其余核心研究条件" if background else
                   "直接相关：" + "、".join(matched_aspects) if reason is None and matched_aspects else
                   "标题或摘要匹配全部核心研究条件" if reason is None else "未通过核心研究条件检查"),
    }

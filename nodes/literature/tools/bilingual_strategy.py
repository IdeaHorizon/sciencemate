"""One semantic plan, compiled independently for international sources and CNKI.

The model identifies concepts and questions; code supplies operators, keeps
every core concept, bounds expansion, and never invents a domain dictionary.
"""
import json
import re
import time


PROMPT = '''Plan bilingual academic literature retrieval for the user's research question.
Return ONLY JSON with this schema:
{"retrieval_mode":"focused","core_concepts":[{"zh":["中文主题"],"en":["English topic"],"role":"subject","background_optional":false}],"exclude_concepts":[],"publication_years":null,"study_requirements":[],"search_groups":[{"label":"主题名称","additional_concepts":[]}]}

1. Choose retrieval_mode from the USER'S intent. Default focused: find studies
combining the stated subject, population, condition/outcome, method and setting.
Every stated requirement belongs in core_concepts, even without words like
"must" or "only". Do not split a method from its intended measurement/outcome.
Context/objective role labels alone NEVER make a requirement optional.
Short multi-concept input is valid: retain its concepts; length is not intent.
Use exploratory ONLY for an open research proposal/background survey or a request
for alternative research angles. Do not turn a concrete search into a proposal.
In exploratory mode, CORE IS THE RESEARCH OBJECT. Use the smallest
set of established concepts that identifies the object unambiguously. One named
architecture/mechanism is sufficient. Add its defining domain/population only
when needed to distinguish DIFFERENT objects. Every core is ANDed into EVERY
query and required in EVERY returned paper, so choose it conservatively.
A proposal "optimize architecture S to improve property R in deployment C using
method M" has S as core, and R/C/M as separate aspects. The user needs existing
work informing the proposal; papers need not have solved the proposed problem.
Do not require all papers to mention a new design goal or deployment setting.

2. ROLES: subject = object/architecture/mechanism; domain = defining application;
population = target people/species; outcome = observed condition/illness under
study; method = explicitly REQUIRED combined method; context = deployment setting;
objective = property/goal to improve. Only in exploratory mode may optional
context/objective go in aspect groups. Preserve population + condition, combined methods, numerical
conditions, explicit only/must restrictions, and explicit exclusions as mandatory.
All core concepts have background_optional=false. Never call a design objective
an outcome to make it mandatory. Policy/funding announcement years are motivation,
not publication filters, unless the user requests publication dates. Put explicit
publication bounds in publication_years={"min":2020,"max":2025} (either may be null),
NEVER in keyword concepts. Scientific numbers such as temperature remain concepts.

3. TERMS: each concept has 1-4 equivalent terms in BOTH zh and en. Use established,
short terms and common abbreviations. No Boolean operators inside terms. Do not
make a long bespoke phrase from several aspects. Alternatives must name the SAME
object, not a larger field. NEVER put the subject, its property and its mechanism
in the same alternatives array: that would retrieve any ONE instead of ALL.
Check each array as an equivalence claim: could each term replace the others
without changing the object/condition? If not, use separate core concepts.
Use atomic established terms. Avoid adding a second core that merely repeats a
qualifier already contained in another core; express relational intent with
established synonyms of that relationship, while retaining distinct restrictions.
Preserve Latin entities/numbers from the question
across the WHOLE plan (including aspects), not necessarily in every core query.
When expanding an acronym, retain its original spelling as an alternative.
For zh, choose names actually used in Chinese academic titles/abstracts, including
established alternative translations and Chinese/Latin mixed spellings of names.
Use the shortest established name that still identifies the SAME concept as the
FIRST term. For method names, prefer the established short label without a generic
"法/方法/模型" suffix when that label alone still denotes the method; this also
matches its use within a compound method name. Keep essential qualifiers intact.
例如，中文检索词可用“蒙特卡罗”“Monte Carlo”，不必只列“蒙特卡罗法”
和“蒙特卡罗方法”：论文可能写“蒙特卡罗-有限元耦合”。必须保留同一方法的
惯用不同译名；但“个人信息保护法”等名称中的“法”不可删，不能机械截词。
中文检索词会按字面匹配；“X法”不一定命中“X-Y方法”。zh 是召回用的检索词，
不是正式术语定义：对于复合方法中的名称，至少保留一个可独立识别该方法的
名称主体，不要把所有词都写成“某某法/某某方法”。
Do not spend the 1-4 alternatives on suffix-only variations of one spelling while
omitting a common alternative name. Order terms by usefulness: canonical short
name, distinct common translation/spelling, then established abbreviations.
Never shorten mechanically: retain suffixes/qualifiers that distinguish laws,
populations, mechanisms or specific variants. Do not invent synonyms, use a
broader parent concept, or substitute a different method to increase recall.
For "combine/couple/use method A with method B", A and B stay separate mandatory
concepts. The connecting verb describes their relationship; do NOT add another
core requiring the word "coupling/combination". A specifically named coupling
scheme or physical relationship is still a required concept when requested.

4. GROUPS: 1-6 distinct groups. In focused mode use one group with every required
concept in core_concepts, and no additional_concepts. Synonyms provide recall
without dropping requirements. In exploratory mode use stated research aspects. A bare topic
gets ONE core-only group. A multi-aspect proposal starts with a core-only group;
then each additional group addresses ONE aspect (normally one additional concept,
at most two genuinely inseparable concepts). Group = shared core AND that aspect,
never AND all the other groups' aspects. Preserve each requested aspect, do not
invent or duplicate them. Use concise Chinese labels. Additional and excluded
concepts have only zh/en arrays. Up to 6 cores, exclusions only if explicit.

5. STUDY REQUIREMENTS: if the user explicitly asks for a study design or validation
relationship (e.g. original randomized trials, validation at an independent institution),
add up to 4 study_requirements: {"kind":"randomized_trial|independent_validation|other", "label":"简短中文条件", "requirement":"testable English condition",
"source_quote":"exact continuous excerpt from the user's question"}.
These are evidence checks on the paper's OWN study, not keyword mentions.
Choose kind=randomized_trial only for explicitly requested randomized trials;
independent_validation only for explicitly requested independent validation.
Keep ordinary topic/method keywords in core_concepts. Do not invent a design
restriction for a general treatment/topic search. Use [] when none is stated.
Evidence checks supplement retrieval keywords; they do not replace them. Retain
an explicitly requested study design as a core concept with common short aliases.
'''


def _concept(value, *, path="concept"):
    if not isinstance(value, dict):
        raise ValueError("invalid_concept")
    result = {}
    for lang in ("zh", "en"):
        terms = value.get(lang)
        if not isinstance(terms, list) or not terms:
            raise ValueError(f"missing_bilingual_concept:{path}.{lang}; expected a nonempty array of 1-4 terms")
        if len(terms) > 4:
            raise ValueError(f"too_many_concept_terms:{path}.{lang} has {len(terms)} terms; maximum 4. "
                             "Keep source abbreviations; split different aspects into separate groups")
        normalized = []
        for term in terms:
            if not isinstance(term, str) or not term.strip() or len(term) > 120:
                raise ValueError("invalid_concept_term")
            term = " ".join(term.split())
            if re.search(r'["\r\n]|\b(?:AND|OR|NOT)\b', term):
                raise ValueError("operators_inside_concept")
            if lang == "en" and (re.search(r"[\u4e00-\u9fff]", term) or not re.search(r"[A-Za-z0-9]", term)):
                raise ValueError("not_usable_english")
            normalized.append(term)
        result[lang] = list(dict.fromkeys(normalized))
    return result


def expression(concepts, exclusions=(), *, lang="en", alternatives=True):
    conjunction, disjunction, negation = (" AND ", " OR ", " NOT ") if lang == "en" else (" * ", " + ", " - ")
    def group(concept):
        terms = concept[lang] if alternatives else concept[lang][:1]
        quoted = [f'"{term}"' for term in terms]
        return "(" + disjunction.join(quoted) + ")" if len(quoted) > 1 else quoted[0]
    query = conjunction.join(map(group, concepts))
    if exclusions:
        query += negation + "(" + disjunction.join(map(group, exclusions)) + ")"
    return query


def chinese_expression(concepts, exclusions=(), *, limit=120):
    """Fit the field budget without discarding every alternative at once.

    Keep one model-selected canonical name per required concept and ALL explicit
    exclusions. Spend the remaining budget on aliases across concepts in rounds,
    so an early concept cannot consume it all. Terms themselves are never edited.
    """
    def shared_acronyms(concept):
        # A model-confirmed abbreviation of this SAME concept is also usable in
        # Chinese indexes. Do not invent abbreviations or merge different cores.
        acronyms = [term for term in concept.get("en", []) + concept["zh"]
                    if re.fullmatch(r"[A-Z][A-Z0-9]+(?:[-/][A-Z0-9]+)*", term)]
        return {"zh": list(dict.fromkeys(concept["zh"][:1] + acronyms + concept["zh"][1:]))}

    concepts = [shared_acronyms(c) for c in concepts]
    exclusions = [shared_acronyms(c) for c in exclusions]
    full = expression(concepts, exclusions, lang="zh")
    if len(full) <= limit:
        return full
    selected = [{"zh": c["zh"][:1]} for c in concepts]
    query = expression(selected, exclusions, lang="zh")
    if len(query) > limit:
        raise ValueError("query_too_long")
    for rank in range(1, max(len(c["zh"]) for c in concepts)):
        for target, concept in zip(selected, concepts):
            if rank >= len(concept["zh"]):
                continue
            target["zh"].append(concept["zh"][rank])
            candidate = expression(selected, exclusions, lang="zh")
            if len(candidate) <= limit:
                query = candidate
            else:
                target["zh"].pop()
    return query


def recompile_chinese_queries(strategy):
    """Reuse the cached interpretation while applying current CNKI compilation.

    Compiler changes must not ask the model to reinterpret the same question.
    Match the stored English expression to its original concepts/aspect before
    replacing only its Chinese expression; never infer concepts from free text.
    """
    if not strategy.get("bilingual_queries"):
        return
    en, zh = strategy["concept_groups"], strategy["core_concepts_zh"]
    if not en or len(en) != len(zh):
        raise ValueError("cached_bilingual_concepts_mismatch")
    core = [{"en": e, "zh": z} for e, z in zip(en, zh)]
    excluded = strategy.get("exclude_concepts", [])
    alternatives = [core] + [core + a["concepts"] for a in strategy.get("research_aspects", [])]
    by_english = {expression(concepts, excluded): concepts for concepts in alternatives}
    strategy["bilingual_queries"] = [
        {**group, "zh": chinese_expression(by_english[group["en"]], excluded)}
        for group in strategy["bilingual_queries"]
    ]


def _check_source_alternatives(concept, question, path):
    """Catch independently stated input conditions collapsed into one OR group.

    Deliberately conservative: two terms may be an unexplained alias pair. Three
    disjoint literal input terms without any equivalence notation require the
    planner to check its grouping. No scientific/domain dictionary is used.
    """
    question = question.casefold()
    for terms in concept.values():
        spans = []
        for term in sorted(terms, key=len, reverse=True):
            pattern = re.escape(term.casefold())
            if re.match(r"[a-z0-9]", term, re.I):
                pattern = r"(?<![a-z0-9])" + pattern
            if re.search(r"[a-z0-9]$", term, re.I):
                pattern += r"(?![a-z0-9])"
            for match in re.finditer(pattern, question):
                if not any(match.start() < end and match.end() > start for start, end in spans):
                    spans.append(match.span())
                    break
        if len(spans) < 3:
            continue
        start, end = min(s[0] for s in spans), max(s[1] for s in spans)
        excerpt = question[max(0, start-8):end+8]
        if re.search(r"\b(?:or|aka|also called|also known as|synonyms?)\b|或者|或称|又称|亦称|别名|也叫|同义|[（(=/]", excerpt):
            continue
        raise ValueError(f"independent_terms_merged_as_synonyms:{path}; input contains "
                         "three distinct terms from one OR array. Separate independent "
                         "conditions into ANDed core concepts; keep only genuine aliases together")


def compile_plan(raw: str, question: str, *, require_roles=False) -> dict:
    from .strategy_generator import _query_semantic_issues, _short_topic_text
    match = re.search(r"```(?:json)?\s*([\s\S]*?)```", raw or "", re.I)
    data = json.loads(match.group(1) if match else raw)
    if not isinstance(data, dict):
        raise ValueError("invalid_plan")
    mode = data.get("retrieval_mode", "focused")
    if mode not in {"focused", "exploratory"}:
        raise ValueError("invalid_retrieval_mode")
    years = data.get("publication_years") or {}
    if not isinstance(years, dict):
        raise ValueError("invalid_publication_years")
    for bound in ("min", "max"):
        year = years.get(bound)
        if year is not None and (type(year) is not int or not 1000 <= year <= 2999
                                 or str(year) not in question):
            raise ValueError("publication_year_must_be_explicit")
    if years.get("min") and years.get("max") and years["min"] > years["max"]:
        raise ValueError("invalid_publication_year_range")
    requirements = data.get("study_requirements") or []
    if not isinstance(requirements, list) or len(requirements) > 4:
        raise ValueError("invalid_study_requirements")
    for requirement in requirements:
        if not isinstance(requirement, dict) or any(
            not isinstance(requirement.get(k), str) or not requirement[k].strip()
            or len(requirement[k]) > 400 for k in ("label", "requirement", "source_quote")
        ):
            raise ValueError("invalid_study_requirement")
        if requirement.get("kind", "other") not in {"randomized_trial", "independent_validation", "other"}:
            raise ValueError("invalid_study_requirement_kind")
        if requirement["source_quote"].strip().casefold() not in question.casefold():
            raise ValueError("study_requirement_must_be_explicit")
    core = data.get("core_concepts")
    if not isinstance(core, list) or not 1 <= len(core) <= 6:
        raise ValueError("missing_core_concepts")
    if require_roles and any(not isinstance(c, dict) or c.get("role") not in
                             {"subject", "domain", "population", "outcome", "method", "context", "objective"}
                             or not isinstance(c.get("background_optional"), bool) for c in core):
        raise ValueError("every_core_concept_requires_role_and_background_optional")
    core = [_concept(c, path=f"core_concepts[{i}]") for i, c in enumerate(core)]
    for i, concept in enumerate(core):
        _check_source_alternatives(concept, question, f"core_concepts[{i}]")
    background_optional = []
    for i, concept in enumerate(data["core_concepts"]):
        if concept.get("background_optional") is True:
            if concept.get("role") != "context":
                raise ValueError("only_context_can_be_background_optional")
            background_optional.append(i)
    if len(background_optional) > 1 or (background_optional and len(core) < 3):
        raise ValueError("background_requires_at_least_two_fixed_concepts")
    # A model label cannot relax an explicit restriction or numerical setting.
    if re.search(r"仅限|只检索|必须|严格限定|\bonly\b|\bmust\b", question, re.I):
        background_optional = []
    if any(re.search(r"\d", term) for i in background_optional for term in core[i]["en"] + core[i]["zh"]):
        background_optional = []
    excluded = data.get("exclude_concepts", [])
    if not isinstance(excluded, list) or len(excluded) > 6:
        raise ValueError("invalid_exclusions")
    excluded = [_concept(c, path=f"exclude_concepts[{i}]") for i, c in enumerate(excluded)]
    groups = data.get("search_groups")
    if not isinstance(groups, list) or not 1 <= len(groups) <= 6:
        raise ValueError("invalid_question_groups")
    if not all(isinstance(item, dict) for item in groups):
        raise ValueError("invalid_question_group")
    # Validate groups before interpreting their meaning. Duplicate groups and
    # repeated core concepts are harmless, including for a short query.
    for group_index, item in enumerate(groups):
        extra = item.get("additional_concepts", [])
        if not isinstance(extra, list) or len(extra) > 3:
            raise ValueError("invalid_additional_concepts")
        item["additional_concepts"] = [c for i, value in enumerate(extra)
            if (c := _concept(value, path=f"search_groups[{group_index}].additional_concepts[{i}]")) not in core]
    normalize = lambda value: re.sub(r"\s+", "", _short_topic_text(value)).casefold()
    atomic = len(core) == 1 and normalize(question) in {
        normalize(term) for term in core[0]["zh"] + core[0]["en"]}
    if atomic and any(g["additional_concepts"] for g in groups):
        raise ValueError("short_topic_was_expanded")
    # Role labels have executable meaning: a deployment setting cannot silently
    # become a mandatory condition for every recall query and admitted paper.
    # Preserve explicit/numeric restrictions and population/outcome/method cores.
    moved = []
    restricted = re.search(r"仅限|只检索|必须|严格限定|\bonly\b|\bmust\b", question, re.I)
    if require_roles and mode == "exploratory" and not atomic and not restricted:
        moved = [i for i, c in enumerate(data["core_concepts"])
                 if c.get("role") in {"context", "objective"} and not c.get("background_optional")
                 and not any(re.search(r"\d", t) for t in core[i]["en"] + core[i]["zh"])]
        if moved:
            if len(moved) == len(core):
                raise ValueError("core_needs_a_subject_not_only_context")
            contexts = [core[i] for i in moved]
            for context in contexts:
                if not any(context in [_concept(c) for c in g.get("additional_concepts", [])] for g in groups):
                    groups.append({"label": context["zh"][0], "additional_concepts": [context]})
            if len(groups) > 6:
                raise ValueError("move_context_to_aspects_within_six_groups_keep_core_only_group")
            kept = [i for i in range(len(core)) if i not in moved]
            core = [core[i] for i in kept]
            background_optional = [kept.index(i) for i in background_optional if i in kept]
    promoted = []
    if mode == "focused":
        # A model placing a required condition in another group must not turn
        # it into an optional OR branch during retrieval or admission.
        for group in groups:
            for concept in group["additional_concepts"]:
                if concept not in core:
                    core.append(concept)
                    promoted.append(concept)
        if len(core) > 6:
            raise ValueError("focused_plan_needs_at_most_six_required_concepts")
        background_optional = []
        groups = [{"label": groups[0].get("label") or "研究条件", "additional_concepts": []}]
    if len(groups) > 1 and not any(not g.get("additional_concepts") for g in groups):
        if len(groups) == 6:
            raise ValueError("multiple_questions_need_one_core_only_group_within_six")
        groups.insert(0, {"label": "核心主题", "additional_concepts": []})
    groups.sort(key=lambda g: bool(g.get("additional_concepts")))
    plan, aspects, seen = [], [], set()
    for group_index, item in enumerate(groups):
        extra = item.get("additional_concepts", [])
        if not isinstance(extra, list) or len(extra) > 3:
            raise ValueError("invalid_additional_concepts")
        extra = [_concept(c, path=f"search_groups[{group_index}].additional_concepts[{i}]")
                 for i, c in enumerate(extra)]
        # A repeated core is not an additional research aspect.
        extra = [c for c in extra if c not in core]
        concepts = core + extra
        en = expression(concepts, excluded)
        zh = chinese_expression(concepts, excluded)
        if len(en) > 2000:
            raise ValueError("query_too_long")
        if en.casefold() in seen:
            continue
        seen.add(en.casefold())
        plan.append({"label": str(item.get("label") or "研究问题")[:100], "en": en, "zh": zh})
        if extra:
            aspects.append({"label": plan[-1]["label"], "concepts": extra})
    if not plan:
        raise ValueError("empty_plan")
    evidence = " AND ".join(p["en"] for p in plan)
    if re.search(r"[\u4e00-\u9fff]", question):
        # Publication bounds are metadata, not words required in a title.
        issues = _query_semantic_issues(question, evidence + " " + " ".join(
            str(years[b]) for b in ("min", "max") if years.get(b)))
        # Structured bilingual concepts already establish usable terms. A raw
        # word-count heuristic must not reject a valid two-word topic plus dates.
        issues = [issue for issue in issues if not issue.startswith("insufficient_detail:")]
        if issues:
            raise ValueError(";".join(issues))
    return {"research_question": question, "retrieval_mode": mode,
            "publication_years": years, "study_requirements": requirements, "english_query": expression(core, excluded),
            "concept_roles": [next((original.get("role", "unknown") for original in data["core_concepts"]
                                    if _concept(original) == c), "unknown") for c in core],
            "concept_groups": [c["en"] for c in core], "core_concepts_zh": [c["zh"] for c in core],
            "background_optional_concepts": background_optional, "exclude_concepts": excluded,
            "scope_adjustments": [{"concept": data["core_concepts"][i]["zh"][0],
                                   "action": data["core_concepts"][i]["role"] + "_to_aspect"} for i in moved]
                                  + [{"concept": c["zh"][0], "action": "required_condition_to_core"} for c in promoted],
            "research_aspects": aspects,
            "bilingual_queries": plan,
            "search_queries": [{"query": p["en"], "label": p["label"], "weight": 1.0,
                                "sources": ["crossref", "openalex", "arxiv"]} for p in plan],
            "filters": {"min_citations": 0, "sort_by": "relevance"}}


def generate(question: str) -> tuple[dict, dict]:
    from . import strategy_generator as sg
    diagnostics = {"bilingual_plan_required": True, "translation_valid": False}
    correction = ""
    max_tokens, read_timeout, request_timeout = 8192, 45, 90
    planning_question = sg._semantic_anchor_source(question)
    diagnostics["proposal_administration_removed"] = planning_question != question
    anchors = sg._query_ascii_anchors(question) if re.search(r"[\u4e00-\u9fff]", question) else []
    if anchors:
        planning_question += ("\nSource spellings to preserve in the corresponding English concepts/aspects: "
                              + json.dumps(anchors, ensure_ascii=False)
                              + ". Keep the original spelling alongside any expansion. This checklist does not "
                              "make examples or aspects mandatory cores for every query.")
    planning_question += (
        "\n中文检索词检查：保留上述研究条件，补充惯用的不同译名。方法名若有可独立识别的名称主体，"
        "请将它作为 zh 的首个检索词，兼容复合方法中的写法；不要只列带‘法/方法’的正式全名。"
        "法律名称、具体变体、对象和研究限制不可机械截短，也不可用上位概念替代。"
    )
    started = time.monotonic()
    truncated_recovery = empty_recovery = False
    repaired_issues = []
    request, raw = {}, None
    # Fix distinct validation problems within one shared budget. Repeating the
    # same issue stops recovery; schema repair must not consume the only chance
    # to restore a research aspect lost during that repair.
    for attempt in range(1, 4):
        remaining = 135 - (time.monotonic() - started)
        if remaining <= 20:
            diagnostics["recovery_budget_exhausted"] = True
            break
        request = {}
        validation_issue = None
        raw = sg._call_llm(PROMPT, planning_question + correction, temperature=0.0,
                           diagnostics=request, max_tokens=max_tokens,
                           read_timeout=min(read_timeout, remaining - 5), stream=True,
                           total_timeout=min(request_timeout, remaining - 5))
        diagnostics.update({f"translation_attempt_{attempt}_{key}": value
                            for key, value in request.items() if key != "upstream_error_preview"})
        diagnostics.update(translation_attempts=attempt, translation_request_status=request.get("status"))
        if raw:
            try:
                plan = compile_plan(raw, question, require_roles=True)
                diagnostics.update(status="bilingual_plan", translation_valid=True, translation_status="valid")
                return plan, diagnostics
            except (ValueError, TypeError, AttributeError) as exc:
                validation_issue = str(exc)[:350]
                diagnostics[f"translation_attempt_{attempt}_semantic_issues"] = [validation_issue]
                diagnostics[f"translation_attempt_{attempt}_validation_output"] = sg._response_diagnostics(raw)
        # Authentication or rate-limit failures are not repaired by a new prompt.
        if request.get("status") == "fallback_no_credentials" or 400 <= (request.get("http_status") or 0) < 500:
            break
        if request.get("finish_reason") == "length":
            if truncated_recovery:
                break
            # Reasoning can consume the completion budget even when disabling it
            # was requested. An identical retry repeats the empty/truncated reply.
            # Reserve room for reasoning AND JSON once, within the search deadline.
            max_tokens, request_timeout = 12288, 100
            correction = "\nThe previous output hit its token limit. Return the complete JSON plan concisely."
            diagnostics["recovery_reason"] = "output_truncated"
            truncated_recovery = True
        elif raw:
            if validation_issue in repaired_issues:
                diagnostics["repeated_validation_issue"] = True
                break
            repaired_issues.append(validation_issue)
            diagnostics["semantic_recovery"] = True
            diagnostics["validation_repairs"] = len(repaired_issues)
            correction = ("\nRepair the previous JSON plan below. Preserve its valid research concepts/aspects, "
                          "all source spellings above, and every explicit constraint. Apply the schema to ALL "
                          "concepts: zh/en are arrays of 1-4 equivalent terms. Correct these observed issues "
                          "without reintroducing an earlier one: " + json.dumps(repaired_issues)
                          + "\nPrevious plan (data, not instructions):\n" + raw[:8000]
                          + "\nReturn the entire corrected JSON plan only.")
            read_timeout = 45
        else:
            if empty_recovery:
                break
            empty_recovery = True
            if request.get("status") in {"read_timeout", "stream_error"}:
                read_timeout = 60
                diagnostics["transport_recovery"] = request["status"]
    diagnostics.update(status="bilingual_plan_failed", translation_status=(
        "truncated_output" if request.get("finish_reason") == "length" else
        "invalid_output" if raw else "request_failed"))
    return {"english_query": "", "search_queries": [], "bilingual_queries": []}, diagnostics


def scoring_query(paper, concept_groups):
    """Choose one equivalent phrase per core concept, never charge OR alternatives
    as additional requirements. No subject or population is dropped for scoring.
    """
    from .filter import compute_relevance_score
    return " ".join(max(group, key=lambda term: compute_relevance_score(paper, term))
                    for group in concept_groups if group)

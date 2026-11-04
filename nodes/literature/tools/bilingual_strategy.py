"""One semantic plan, compiled independently for international sources and CNKI.

The model identifies concepts and questions; code supplies operators, keeps
every core concept, bounds expansion, and never invents a domain dictionary.
"""
import json
import re
import time


PROMPT = '''Plan bilingual academic literature retrieval for the user's research question.
Return ONLY JSON with this schema:
{"core_concepts":[{"zh":["中文主题"],"en":["English topic"],"role":"subject","background_optional":false}],"exclude_concepts":[],"search_groups":[{"label":"主题名称","additional_concepts":[]}]}

1. CORE IS THE RESEARCH OBJECT, not the entire desired solution. Use the smallest
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
objective = property/goal to improve. Context and objective go in aspect groups,
not core. Preserve population + condition, explicitly combined methods, numerical
conditions, explicit only/must restrictions, and explicit exclusions as mandatory.
All core concepts have background_optional=false. Never call a design objective
an outcome to make it mandatory. Policy/funding announcement years are motivation,
not publication filters, unless the user requests publication dates.

3. TERMS: each concept has 1-4 equivalent terms in BOTH zh and en. Use established,
short terms and common abbreviations. No Boolean operators inside terms. Do not
make a long bespoke phrase from several aspects. Alternatives must name the SAME
object, not a larger field. Preserve Latin entities/numbers from the question
across the WHOLE plan (including aspects), not necessarily in every core query.
When expanding an acronym, retain its original spelling as an alternative.

4. GROUPS: 1-6 distinct groups, based on stated research aspects. A bare topic
gets ONE core-only group. A multi-aspect proposal starts with a core-only group;
then each additional group addresses ONE aspect (normally one additional concept,
at most two genuinely inseparable concepts). Group = shared core AND that aspect,
never AND all the other groups' aspects. Preserve each requested aspect, do not
invent or duplicate them. Use concise Chinese labels. Additional and excluded
concepts have only zh/en arrays. Up to 6 cores, exclusions only if explicit.
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


def compile_plan(raw: str, question: str, *, require_roles=False) -> dict:
    from .strategy_generator import _query_semantic_issues, is_short_standalone_topic
    match = re.search(r"```(?:json)?\s*([\s\S]*?)```", raw or "", re.I)
    data = json.loads(match.group(1) if match else raw)
    if not isinstance(data, dict):
        raise ValueError("invalid_plan")
    core = data.get("core_concepts")
    if not isinstance(core, list) or not 1 <= len(core) <= 6:
        raise ValueError("missing_core_concepts")
    if require_roles and any(not isinstance(c, dict) or c.get("role") not in
                             {"subject", "domain", "population", "outcome", "method", "context", "objective"}
                             or not isinstance(c.get("background_optional"), bool) for c in core):
        raise ValueError("every_core_concept_requires_role_and_background_optional")
    core = [_concept(c, path=f"core_concepts[{i}]") for i, c in enumerate(core)]
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
    short = is_short_standalone_topic(question)
    # Role labels have executable meaning: a deployment setting cannot silently
    # become a mandatory condition for every recall query and admitted paper.
    # Preserve explicit/numeric restrictions and population/outcome/method cores.
    moved = []
    restricted = re.search(r"仅限|只检索|必须|严格限定|\bonly\b|\bmust\b", question, re.I)
    if require_roles and not short and not restricted:
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
    if short and (len(groups) != 1 or groups[0].get("additional_concepts")):
        raise ValueError("short_topic_was_expanded")
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
        zh = expression(concepts, excluded, lang="zh")
        # CNKI fields have a smaller input budget. Remove optional synonyms,
        # never whole concepts or explicit exclusions, when shortening a query.
        if len(zh) > 120:
            zh = expression(concepts, excluded, lang="zh", alternatives=False)
        if len(zh) > 120 or len(en) > 2000:
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
        issues = _query_semantic_issues(question, evidence)
        if issues:
            raise ValueError(";".join(issues))
    return {"research_question": question, "english_query": expression(core, excluded),
            "concept_groups": [c["en"] for c in core], "core_concepts_zh": [c["zh"] for c in core],
            "background_optional_concepts": background_optional, "exclude_concepts": excluded,
            "scope_adjustments": [{"concept": data["core_concepts"][i]["zh"][0],
                                   "action": data["core_concepts"][i]["role"] + "_to_aspect"} for i in moved],
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

import json

import pytest

from nodes.literature.tools.bilingual_strategy import compile_plan
from nodes.literature.tools.academic_admission import assess_concepts


def c(zh, en, role="subject"):
    return {"zh": [zh], "en": [en], "role": role, "background_optional": False}


@pytest.mark.parametrize("subject,method,outcome,question", [
    (c("玉米", "maize"), c("无人机多光谱影像", "UAV multispectral imagery", "method"),
     c("氮素缺乏", "nitrogen deficiency", "outcome"), "查找用无人机多光谱影像判断玉米氮素缺乏的研究"),
    (c("儿童", "children", "population"), c("可穿戴传感器", "wearable sensors", "method"),
     c("哮喘监测", "asthma monitoring", "outcome"), "寻找用可穿戴传感器监测儿童哮喘的研究"),
    (c("桥梁", "bridges"), c("声发射", "acoustic emission", "method"),
     c("疲劳裂纹", "fatigue cracks", "outcome"), "查找通过声发射检测桥梁疲劳裂纹的研究"),
])
def test_focused_method_and_outcome_cannot_become_optional(subject, method, outcome, question):
    for required, misplaced in [(method, outcome), (outcome, method)]:
        raw = {"core_concepts": [subject, required], "search_groups": [
            {"additional_concepts": []}, {"additional_concepts": [misplaced]}]}
        plan = compile_plan(json.dumps(raw), question, require_roles=True)
        assert plan["retrieval_mode"] == "focused" and not plan["research_aspects"]
        assert len(plan["concept_groups"]) == 3
        title = " ".join(x["en"][0] for x in [subject, method, outcome])
        assert assess_concepts({"title": title}, plan)["tier"] == "direct"
        for dropped in [method, outcome]:
            row = assess_concepts({"title": title.replace(dropped["en"][0], "")}, plan)
            assert row["tier"] == "excluded" and dropped["zh"][0] in row["missing_concepts"]


@pytest.mark.parametrize("question,concepts", [
    ("犬 特应性皮炎 治疗", [c("犬", "dogs"), c("特应性皮炎", "atopic dermatitis"), c("治疗", "treatment")]),
    ("铝合金 疲劳 检测", [c("铝合金", "aluminum alloy"), c("疲劳", "fatigue"), c("检测", "detection")]),
    ("水稻 盐胁迫", [c("水稻", "rice"), c("盐胁迫", "salt stress")]),
])
def test_short_multiconcept_queries_are_not_bare_topics(question, concepts):
    raw = {"core_concepts": concepts[:2], "search_groups": [
        {"additional_concepts": []}, {"additional_concepts": concepts[2:]}]}
    plan = compile_plan(json.dumps(raw), question, require_roles=True)
    assert len(plan["concept_groups"]) == len(concepts)
    assert len(plan["bilingual_queries"]) == 1


def test_context_is_required_in_focused_mode_without_magic_must_word():
    raw = {"core_concepts": [c("细菌", "bacteria"), c("低氧", "hypoxia", "context")],
           "search_groups": [{"additional_concepts": []}]}
    plan = compile_plan(json.dumps(raw), "查找低氧条件下细菌的研究", require_roles=True)
    assert len(plan["concept_groups"]) == 2 and not plan["scope_adjustments"]


def test_publication_years_are_metadata_not_title_keywords():
    raw = {"core_concepts": [c("珊瑚白化", "coral bleaching")],
           "publication_years": {"min": 2020, "max": 2025},
           "search_groups": [{"additional_concepts": []}]}
    plan = compile_plan(json.dumps(raw), "查找2020—2025年珊瑚白化的论文", require_roles=True)
    assert "2020" not in plan["english_query"]
    assert plan["publication_years"] == {"min": 2020, "max": 2025}
    # Semantic admission and the existing publication-year UI filter are
    # separate. Missing year words in the title do not make a paper irrelevant.
    assert assess_concepts({"title": "Coral bleaching"}, plan)["tier"] == "direct"
    # A date must not be silently invented for an undated query.
    with pytest.raises(ValueError, match="publication_year_must_be_explicit"):
        compile_plan(json.dumps(raw), "查找珊瑚白化的论文")

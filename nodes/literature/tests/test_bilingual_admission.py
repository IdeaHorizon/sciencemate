import json

import pytest

from nodes.literature.tools.academic_admission import assess_concepts
from nodes.literature.tools.bilingual_strategy import compile_plan
from nodes.literature.tools.strategy_generator import _query_semantic_issues
from platform_runtime import _academic_exclusion_reason, _select_academic_search_results


@pytest.fixture
def strategy():
    return {
        "concept_groups": [["generative AI", "ChatGPT", "DeepSeek"], ["civil servants"], ["digital literacy", "digital competence"]],
        "core_concepts_zh": [["生成式人工智能", "生成式AI"], ["基层公务员"], ["数字素养", "数字能力"]],
        "background_optional_concepts": [0],
    }


def test_chinese_and_english_evidence_get_equal_scores_without_translation(strategy):
    zh = assess_concepts({"title": "生成式人工智能对基层公务员数字素养的影响"}, strategy)
    en = assess_concepts({"title": "Generative AI and digital literacy of civil servants"}, strategy)
    assert zh["relevance"] == en["relevance"] == 1
    assert zh["tier"] == en["tier"] == "direct"


def test_background_keeps_population_and_outcome_and_is_not_direct_evidence(strategy):
    background = assess_concepts({"title": "基层公务员数字素养提升路径研究"}, strategy)
    assert background["tier"] == "background"
    assert background["missing_concepts"] == ["生成式人工智能"]
    assert background["relevance"] == pytest.approx(2/3, abs=.0001)
    assert assess_concepts({"title": "Generative AI and digital literacy for students"}, strategy)["tier"] == "excluded"
    assert assess_concepts({"title": "ChatGPT and civil servants performance"}, strategy)["tier"] == "excluded"
    assert assess_concepts({"title": "Training civil servants in digital competence"}, strategy)["tier"] == "background"  # 'AI' not inside 'training'.


def test_background_cannot_relax_an_unmarked_context_or_explicit_exclusion(strategy):
    p = {"title": "Assessment of digital literacy of civil servants"}
    assert assess_concepts(p, {**strategy,"background_optional_concepts": []})["tier"] == "excluded"
    excluded = {**strategy,"exclude_concepts": [{"en":["simulation"],"zh":["模拟研究"]}]}
    assert assess_concepts({"title":"生成式人工智能对基层公务员数字素养的模拟研究"}, excluded)["exclusion_reason"] == "explicit_exclusion"


def test_unrelated_notices_and_background_do_not_displace_direct_results(strategy):
    papers = []
    for title in ["基层公务员数字素养提升路径研究", "生成式人工智能对基层公务员数字素养的影响", "Editorial Board"]:
        p={"title":title,"score":1,"relevance_assessment":assess_concepts({"title":title},strategy)}
        papers.append(p)
    selected, excluded = _select_academic_search_results(papers,limit=2,strict_core=True)
    assert selected[0]["relevance_assessment"]["tier"] == "direct"
    assert selected[1]["relevance_assessment"]["tier"] == "background"
    assert excluded == 1 and _academic_exclusion_reason(papers[2]) == "nonresearch"


def test_generic_rule_preserves_medical_and_engineering_subjects():
    strategy={"concept_groups":[["urban setting"],["pediatric patients"],["asthma"]],
              "core_concepts_zh":[["城市环境"],["儿童患者"],["哮喘"]],"background_optional_concepts":[0]}
    assert assess_concepts({"title":"儿童患者哮喘研究"},strategy)["tier"] == "background"
    assert assess_concepts({"title":"城市环境成人患者哮喘研究"},strategy)["tier"] == "excluded"
    engineering={"concept_groups":[["lattice Boltzmann"],["immersed boundary"],["particle settling"]],
                 "core_concepts_zh":[["格子玻尔兹曼"],["浸入边界"],["颗粒沉降"]]}
    assert assess_concepts({"title":"格子玻尔兹曼浸入边界颗粒沉降模拟"},engineering)["tier"] == "direct"
    assert assess_concepts({"title":"格子玻尔兹曼颗粒沉降模拟"},engineering)["tier"] == "excluded"


def test_population_cannot_be_marked_optional_by_the_model():
    concept=lambda zh,en,role,optional=False:{"zh":[zh],"en":[en],"role":role,"background_optional":optional}
    raw={"core_concepts":[concept("城市环境","urban setting","context"),
                           concept("儿童患者","pediatric patients","population",True),
                           concept("哮喘","asthma","outcome")],
         "search_groups":[{"label":"研究","additional_concepts":[]}]}
    with pytest.raises(ValueError,match="only_context"):
        compile_plan(json.dumps(raw),"城市环境中儿童患者的哮喘问题")


def test_policy_year_is_not_a_retrieval_constraint_but_explicit_years_remain():
    source="2026年度国家自然科学基金将海洋工程列为优先资助方向。研究深远海风电轻量化直流输电拓扑与控制策略。"
    query="offshore wind power AND lightweight DC transmission AND converter topology AND control strategy"
    assert not any("2026" in issue for issue in _query_semantic_issues(source,query))
    for source in ["仅检索2026年以来发表的轻量化直流输电论文", "比较2026年的产业政策与2025年政策", "研究CO2在300K的扩散"]:
        assert any("missing_ascii_anchors" in issue for issue in _query_semantic_issues(source,query))


def test_research_aspects_are_matched_separately_without_dropping_subject_or_restrictions():
    c=lambda zh,en:{"zh":[zh],"en":[en]}
    strategy={"concept_groups":[["offshore wind"],["HVDC","high voltage direct current"]],
              "core_concepts_zh":[["海上风电"],["直流输电"]],
              "research_aspects":[
                  {"label":"拓扑设计","concepts":[c("换流器拓扑","converter topology")]},
                  {"label":"稳定性控制","concepts":[c("稳定性","stability"),c("控制","control")]},
              ],"exclude_concepts":[c("撤稿","retraction")]}
    topology=assess_concepts({"title":"Converter topology for offshore wind HVDC"},strategy)
    assert topology['tier']=='direct'
    assert topology['matched_aspects']==['拓扑设计']
    control=assess_concepts({"title":"Control of HVDC offshore wind for stability"},strategy)
    assert control['tier']=='direct' and control['matched_aspects']==['稳定性控制']
    overview=assess_concepts({"title":"海上风电直流输电综述"},strategy)
    assert overview['tier']=='background' and not overview['matched_aspects']
    assert assess_concepts({"title":"HVDC converter topology for urban grids"},strategy)['tier']=='excluded'
    assert assess_concepts({"title":"Retraction: HVDC offshore wind converter topology"},strategy)['tier']=='excluded'
    strict={**strategy,"concept_groups":strategy['concept_groups']+[['300 kV']],
            "core_concepts_zh":strategy['core_concepts_zh']+[['300千伏']]}
    assert assess_concepts({"title":"Converter topology for offshore wind HVDC"},strict)['tier']=='excluded'


def test_topic_only_background_needs_title_support_not_an_incidental_population_mention():
    strategy={"concept_groups":[["frontline civil servants","street-level bureaucrats"],["digital literacy","digital skills","digital competence"]],
              "research_aspects":[{"label":"生成式AI","concepts":[{"en":["generative AI"]}]}]}
    off_topic={"title":"Digital competence in teacher education", "abstract":"Teachers act as street-level bureaucrats in education policy."}
    assert assess_concepts(off_topic,strategy)['tier']=='excluded'
    relevant={"title":"Digital skills of civil servants", "abstract":"We survey civil servants as frontline professionals."}
    assert assess_concepts(relevant,strategy)['tier']=='background'


def test_single_core_needs_more_than_an_ambiguous_abbreviation():
    strategy={'concept_groups':[['dynamic heterogeneous redundancy','DHR']],
              'core_concepts_zh':[['动态异构冗余']],
              'research_aspects':[{'label':'调度','concepts':[{'en':['scheduling']}]}]}
    assert assess_concepts({'title':'DHR assay for diagnosis of granulomatous disease'},strategy)['tier']=='excluded'
    assert assess_concepts({'title':'Dynamic heterogeneous redundancy scheduling for cyber defense'},strategy)['tier']=='direct'
    assert assess_concepts({'title':'Dynamic heterogeneous redundancy architecture'},strategy)['tier']=='background'
    assert assess_concepts({'title':'DHR architecture', 'abstract':'A dynamic heterogeneous redundancy approach to scheduling'},strategy)['tier']=='direct'
    assert assess_concepts({'title':'DHR architecture'}, {'concept_groups':[['DHR']]})['tier']=='direct'


def test_concise_title_can_anchor_distinct_topics_whose_shared_material_is_in_abstract():
    strategy={'concept_groups':[['metal fatigue','fatigue of metals'],['self-healing metals']],
              'research_aspects':[{'label':'物理约束建模','concepts':[{'en':['physics-informed modeling']}]}]}
    paper={'title':'Autonomous healing of fatigue cracks via cold welding',
           'abstract':'Fatigue in metals can involve intrinsic self-healing of fatigue cracks.'}
    result=assess_concepts(paper,strategy)
    assert result['tier']=='background' and result['coverage']==1
    # Merely mentioning a shared material does not identify either phenomenon.
    assert assess_concepts({**paper,'title':'Mechanical properties of metals'},strategy)['tier']=='excluded'
    # Every core must still be established in the title+abstract evidence.
    assert assess_concepts({**paper,'abstract':'Fatigue and healing of polymer cracks'},strategy)['tier']=='excluded'

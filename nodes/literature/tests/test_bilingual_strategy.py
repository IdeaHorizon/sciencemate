import json
import pytest
from nodes.literature.tools import bilingual_strategy as bs, strategy_generator as sg
from platform_runtime import _academic_strategy_queries, run_literature_search_plan, RequestError


def concept(zh, en):
    return {'zh': [zh], 'en': [en]}


def raw(groups=1):
    return {'core_concepts': [concept('生成式人工智能', 'generative artificial intelligence'),
                             concept('基层公务员', 'grassroots civil servants'),
                             concept('数字素养', 'digital literacy')],
            'search_groups': [{'label': str(i), 'additional_concepts': [] if not i else [concept('方面'+str(i),'aspect '+str(i))]} for i in range(groups)]}


@pytest.mark.parametrize('count', [1, 2, 4, 5, 6])
def test_number_follows_questions_and_every_query_keeps_all_core_concepts(count):
    plan = bs.compile_plan(json.dumps(raw(count)), '生成式人工智能情境下基层公务员数字素养的构成、影响因素和提升机制是什么？')
    assert len(plan['bilingual_queries']) == count
    queries = _academic_strategy_queries(plan, '输入的中文自然语言段落')
    assert queries == [item['en'] for item in plan['bilingual_queries']]
    for item in plan['bilingual_queries']:
        assert '"digital literacy"' in item['en']  # Beyond the former eight-word cutoff.
        assert '"基层公务员"' in item['zh'] and '"数字素养"' in item['zh']
        assert '输入的中文' not in item['zh']
        assert item['en'].count('aspect') <= 1


def test_short_topic_has_one_group_and_no_mechanical_expansion():
    data = {'core_concepts':[concept('布拉格反射','Bragg reflection')],
            'search_groups':[{'label':'布拉格反射','additional_concepts':[]}]}
    plan = bs.compile_plan(json.dumps(data), '布拉格反射')
    assert _academic_strategy_queries(plan,'布拉格反射') == ['"Bragg reflection"']
    data['search_groups'] *= 3
    with pytest.raises(ValueError,match='short_topic_was_expanded'):
        bs.compile_plan(json.dumps(data),'布拉格反射')


def test_synonyms_and_exclusions_compile_in_both_languages():
    data = raw()
    data['core_concepts'][0]['en'].append('generative AI')
    data['exclude_concepts'] = [concept('模拟研究','simulation studies')]
    p = bs.compile_plan(json.dumps(data),'生成式人工智能情境下基层公务员数字素养研究，排除模拟研究')['bilingual_queries'][0]
    assert ' OR "generative AI"' in p['en']
    assert ' NOT ("simulation studies")' in p['en']
    assert ' - ("模拟研究")' in p['zh']


@pytest.mark.parametrize('mutate', [lambda d:d.update(search_groups=[1]),lambda d:d.update(core_concepts=[]),
                                   lambda d:d['core_concepts'][0].update(en=['仍为中文']),
                                   lambda d:d['core_concepts'][0].update(zh=['数字 AND 素养'])])
def test_invalid_plan_rejected(mutate):
    data = raw(); mutate(data)
    with pytest.raises(ValueError):bs.compile_plan(json.dumps(data),'数字素养方面的研究问题有哪些？')


def test_invalid_model_output_has_bounded_repair_not_original_paragraph_fallback(monkeypatch):
    attempts=[]
    def call(*args,diagnostics,**kwargs):
        attempts.append(args);diagnostics['status']='ok';return 'not json'
    monkeypatch.setattr(sg,'_call_llm',call)
    strategy, diagnostics = bs.generate('基层公务员数字素养研究')
    assert len(attempts)==2 and not strategy['search_queries']
    assert diagnostics['bilingual_plan_required'] and not diagnostics['translation_valid']


def test_schema_repair_can_then_restore_an_omitted_source_aspect(monkeypatch):
    question='研究县域慢病管理的数字工具与基层能力适配，包括AI辅助筛查。'
    base={'core_concepts':[{**concept('慢性病管理','chronic disease management'),
                           'role':'subject','background_optional':False}],
          'search_groups':[{'label':'核心主题','additional_concepts':[]}]}
    oversize=json.loads(json.dumps(base))
    oversize['search_groups'].append({'label':'数字工具','additional_concepts':[{
        'zh':['数字工具','数字管理工具','数字健康工具','电子健康工具','AI辅助筛查'],
        'en':['digital tool','digital management tool','digital health tool','electronic health tool','AI-assisted screening']}]})
    fixed=json.loads(json.dumps(base))
    fixed['search_groups'].append({'label':'AI辅助筛查','additional_concepts':[
        concept('AI辅助筛查','AI-assisted screening')]})
    replies=[oversize,base,fixed];calls=[]
    def call(system,user,*,diagnostics,**kwargs):
        calls.append(user)
        assert 'Source spellings to preserve' in user and '["AI"]' in user
        diagnostics.update(status='ok',finish_reason='stop')
        return json.dumps(replies[len(calls)-1])
    monkeypatch.setattr(sg,'_call_llm',call)
    plan,diag=bs.generate(question)
    assert len(calls)==3 and diag['translation_valid']
    assert 'too_many_concept_terms:' in calls[1] and 'maximum 4' in calls[1]
    assert 'missing_ascii_anchors:AI' in calls[2] and 'too_many_concept_terms:' in calls[2]
    assert 'AI-assisted screening' in plan['bilingual_queries'][1]['en']
    assert 'AI' not in plan['english_query']  # An example is not imposed on every paper.
    assert diag['validation_repairs']==2


def test_unchanged_missing_anchor_still_fails_instead_of_bypassing_validation(monkeypatch):
    calls=[]
    data={'core_concepts':[{**concept('慢病管理','chronic disease management'),
                           'role':'subject','background_optional':False}],
          'search_groups':[{'label':'主题','additional_concepts':[]}]}
    def call(*args,diagnostics,**kwargs):
        calls.append(1);diagnostics.update(status='ok',finish_reason='stop')
        return json.dumps(data)
    monkeypatch.setattr(sg,'_call_llm',call)
    plan,diag=bs.generate('慢病管理研究，包括AI辅助筛查')
    assert len(calls)==2 and not plan['search_queries']
    assert not diag['translation_valid'] and diag['repeated_validation_issue']


def test_concept_limit_reports_the_field_without_silently_dropping_the_fifth_term():
    value={'zh':['工具'], 'en':['tool','system','record','follow-up','AI-assisted screening']}
    with pytest.raises(ValueError,match=r'too_many_concept_terms:aspect.en has 5 terms; maximum 4'):
        bs._concept(value,path='aspect')


def test_english_question_does_not_turn_every_word_into_a_preserved_acronym(monkeypatch):
    def call(system,user,*,diagnostics,**kwargs):
        assert 'Source spellings to preserve' not in user
        diagnostics.update(status='ok',finish_reason='stop')
        return json.dumps({'core_concepts':[{**concept('布拉格反射','Bragg reflection'),
                            'role':'subject','background_optional':False}],
                           'search_groups':[{'label':'主题','additional_concepts':[]}]})
    monkeypatch.setattr(sg,'_call_llm',call)
    plan,diag=bs.generate('Bragg reflection')
    assert diag['translation_valid'] and plan['bilingual_queries']


@pytest.mark.parametrize('first_body', ['', '{"core_concepts":['])
def test_reasoning_exhaustion_or_partial_json_retries_with_larger_budget(monkeypatch, first_body):
    calls=[]
    data={'core_concepts':[{**concept('布拉格反射','Bragg reflection'),
                           'role':'subject','background_optional':False}],
          'search_groups':[{'label':'布拉格反射','additional_concepts':[]}]}
    def call(system,user,*,diagnostics,**kwargs):
        calls.append((user,kwargs))
        diagnostics.update(status='ok',http_status=200)
        if len(calls)==1:
            diagnostics.update(finish_reason='length',completion_tokens=4096,reasoning_tokens=4096)
            return first_body
        diagnostics.update(finish_reason='stop')
        return json.dumps(data)
    monkeypatch.setattr(sg,'_call_llm',call)
    strategy, diagnostics=bs.generate('布拉格反射')
    assert strategy['english_query']=='"Bragg reflection"'
    assert diagnostics['translation_valid'] and diagnostics['recovery_reason']=='output_truncated'
    assert [c[1]['max_tokens'] for c in calls]==[8192,12288]
    assert [c[1]['read_timeout'] for c in calls]==[45,45]
    assert all(c[1]['stream'] for c in calls)
    assert all(c[1]['total_timeout']<=100 for c in calls)
    assert 'token limit' in calls[1][0]


def test_repeated_truncation_stops_after_one_larger_retry_without_searching_reasoning(monkeypatch):
    calls=[]
    def call(*args,diagnostics,**kwargs):
        calls.append(kwargs['max_tokens'])
        diagnostics.update(status='ok',http_status=200,finish_reason='length')
        return ''
    monkeypatch.setattr(sg,'_call_llm',call)
    strategy, diagnostics=bs.generate('布拉格反射')
    assert calls==[8192,12288]
    assert not strategy['bilingual_queries']
    assert diagnostics['translation_status']=='truncated_output'
    assert not diagnostics['translation_valid']


def test_recovery_stops_when_the_planning_time_budget_is_exhausted(monkeypatch):
    ticks=iter([0,0,116])
    monkeypatch.setattr(bs.time,'monotonic',lambda:next(ticks))
    calls=[]
    def call(*args,diagnostics,**kwargs):
        calls.append(1);diagnostics.update(status='ok',finish_reason='length')
        return ''
    monkeypatch.setattr(sg,'_call_llm',call)
    plan,diag=bs.generate('布拉格反射')
    assert calls==[1] and not plan['bilingual_queries']
    assert diag['recovery_budget_exhausted'] and not diag['translation_valid']


def test_truncation_recovery_does_not_use_up_the_semantic_repair(monkeypatch):
    calls=[]
    data={'core_concepts':[{**concept('金属疲劳','metal fatigue'),
                           'role':'subject','background_optional':False}],
          'search_groups':[{'label':'主题','additional_concepts':[]}]}
    def call(system,user,*,diagnostics,**kwargs):
        calls.append(kwargs['max_tokens']);diagnostics.update(status='ok',http_status=200)
        if len(calls)==1:
            diagnostics['finish_reason']='length';return ''
        diagnostics['finish_reason']='stop'
        if len(calls)==3:
            assert 'missing_ascii_anchors:CAE' in user
            data['search_groups'].append({'label':'集成','additional_concepts':[concept('CAE集成','CAE integration')]})
        return json.dumps(data)
    monkeypatch.setattr(sg,'_call_llm',call)
    plan,diag=bs.generate('研究金属材料疲劳损伤演化的建模方法，并讨论与CAE工程软件的集成')
    assert plan['bilingual_queries'] and diag['translation_valid']
    assert calls==[8192,12288,12288]
    assert diag['recovery_reason']=='output_truncated' and diag['semantic_recovery']


@pytest.mark.parametrize('status',[401,403,429])
def test_authorization_and_rate_limits_do_not_trigger_larger_retry(monkeypatch,status):
    calls=[]
    def call(*args,diagnostics,**kwargs):
        calls.append(kwargs['max_tokens']);diagnostics.update(status='http_error',http_status=status)
        return None
    monkeypatch.setattr(sg,'_call_llm',call)
    _,diagnostics=bs.generate('布拉格反射')
    assert calls==[8192]
    assert 'recovery_reason' not in diagnostics


def test_stream_timeout_retry_uses_longer_idle_window_and_shared_deadline(monkeypatch):
    calls=[]
    def call(*args,diagnostics,**kwargs):
        calls.append(kwargs)
        diagnostics.update(status='read_timeout',timeout_phase='stream_idle')
        return None
    monkeypatch.setattr(sg,'_call_llm',call)
    plan,diag=bs.generate('布拉格反射')
    assert not plan['bilingual_queries']
    assert [c['read_timeout'] for c in calls]==[45,60]
    assert all(c['stream'] and c['total_timeout']<=90 for c in calls)
    assert diag['transport_recovery']=='read_timeout'


def test_model_and_validator_share_research_scope_without_losing_the_original_question(monkeypatch):
    question='研究布拉格反射。预期成果：SCI论文1篇。'
    data={'core_concepts':[{**concept('布拉格反射','Bragg reflection'),
                           'role':'subject','background_optional':False}],
          'search_groups':[{'label':'布拉格反射','additional_concepts':[]}]}
    def call(system,user,*,diagnostics,**kwargs):
        assert 'SCI' not in user and '1篇' not in user
        assert '布拉格反射' in user
        diagnostics.update(status='ok',finish_reason='stop')
        return json.dumps(data)
    monkeypatch.setattr(sg,'_call_llm',call)
    plan,diag=bs.generate(question)
    assert plan['research_question']==question
    assert diag['translation_valid'] and diag['proposal_administration_removed']


@pytest.mark.asyncio
async def test_plan_only_and_combined_use_same_cached_generator(monkeypatch):
    from nodes.literature.tools import academic_strategy
    plan=bs.compile_plan(json.dumps(raw(4)), '数字素养研究的构成和影响因素及其提升路径')
    monkeypatch.setattr(academic_strategy,'generate_academic_strategy',lambda q:(plan,{}))
    result=await run_literature_search_plan({'query':'数字素养研究的构成和影响因素及其提升路径'})
    assert result['search_plan']==plan['bilingual_queries']
    monkeypatch.setattr(academic_strategy,'generate_academic_strategy',lambda q:({'bilingual_queries':[]},{}))
    with pytest.raises(RequestError):await run_literature_search_plan({'query':'失败计划'})


def test_synonyms_do_not_dilute_relevance_and_late_core_is_required():
    from nodes.literature.tools.search_engines import Paper
    from nodes.literature.tools.filter import compute_relevance_score
    from platform_runtime import _paper_matches_academic_facets
    paper=Paper(title='Digital competence of street-level bureaucrats using ChatGPT',abstract='')
    groups=[['generative artificial intelligence','ChatGPT'],['grassroots civil servants','street-level bureaucrats'],['digital literacy','digital competence']]
    query=bs.scoring_query(paper,groups)
    assert 'ChatGPT' in query and 'digital competence' in query
    assert compute_relevance_score(paper,query)==1.0
    facets=[[term.lower() for term in group] for group in groups]
    assert _paper_matches_academic_facets({'title':paper.title},facets,strict_core=True)
    assert not _paper_matches_academic_facets({'title':'Street-level bureaucrats using ChatGPT'},facets,strict_core=True)
    assert not _paper_matches_academic_facets({'title':paper.title},facets+[['rural community']],strict_core=True)


def test_multiple_subquestions_also_retrieve_shared_topic():
    data=raw(4);data['search_groups']=data['search_groups'][1:]
    plan=bs.compile_plan(json.dumps(data),'生成式人工智能情境下基层公务员数字素养的构成、影响因素和提升机制是什么？')
    assert len(plan['bilingual_queries'])==4
    assert plan['bilingual_queries'][0]['en']==plan['english_query']


def test_aspects_compile_to_separate_queries_and_admission_groups():
    core=[concept('海上风电','offshore wind'),concept('直流输电','HVDC')]
    data={'core_concepts':core,'exclude_concepts':[concept('撤稿','retraction')],
          'search_groups':[{'label':'主题','additional_concepts':[]},
                           {'label':'拓扑','additional_concepts':[concept('换流器拓扑','converter topology')]},
                           {'label':'稳定性','additional_concepts':[concept('稳定性','stability')]}]}
    plan=bs.compile_plan(json.dumps(data),'海上风电直流输电的拓扑与稳定性研究，排除撤稿')
    assert len(plan['bilingual_queries'])==3
    assert [a['label'] for a in plan['research_aspects']]==['拓扑','稳定性']
    topology=plan['bilingual_queries'][1]
    assert 'converter topology' in topology['en'] and 'stability' not in topology['en']
    for group in plan['bilingual_queries']:
        assert 'offshore wind' in group['en'] and 'HVDC' in group['en']
        assert 'NOT' in group['en'] and '撤稿' in group['zh']


def test_deployment_context_is_compiled_as_an_aspect_not_a_universal_gate():
    data = {'core_concepts': [
        {**concept('动态异构冗余','dynamic heterogeneous redundancy'), 'role':'subject','background_optional':False},
        {**concept('云边端协同','cloud-edge-end collaboration'), 'role':'context','background_optional':False}],
        'search_groups':[{'label':'核心主题','additional_concepts':[]},
                         {'label':'调度','additional_concepts':[concept('自适应调度','adaptive scheduling')]}]}
    question = '研究动态异构冗余架构在云边端协同场景下如何实现低开销的自适应调度'
    plan = bs.compile_plan(json.dumps(data),question,require_roles=True)
    assert plan['concept_groups'] == [['dynamic heterogeneous redundancy']]
    assert 'cloud-edge-end' not in plan['bilingual_queries'][0]['en']
    assert any('cloud-edge-end' in q['en'] for q in plan['bilingual_queries'])
    assert len(plan['research_aspects']) == 2
    assert plan['scope_adjustments'][0]['action'] == 'context_to_aspect'
    # Explicitly limited retrieval must retain the same setting in EVERY group.
    strict = bs.compile_plan(json.dumps(data),question+'，仅限这个场景的论文',require_roles=True)
    assert len(strict['concept_groups']) == 2
    assert all('cloud-edge-end' in q['en'] for q in strict['bilingual_queries'])


def test_context_rule_never_drops_population_outcome_or_combined_methods():
    data = {'core_concepts':[
        {**concept('儿童患者','pediatric patients'),'role':'population','background_optional':False},
        {**concept('哮喘','asthma'),'role':'outcome','background_optional':False},
        {**concept('城市环境','urban environment'),'role':'context','background_optional':False}],
        'search_groups':[{'label':'儿童哮喘','additional_concepts':[]}]}
    plan=bs.compile_plan(json.dumps(data),'研究城市环境下儿童患者的哮喘及其影响因素',require_roles=True)
    assert plan['concept_groups']==[['pediatric patients'],['asthma']]
    data['core_concepts']=[{**concept('格子玻尔兹曼','lattice Boltzmann'),'role':'method','background_optional':False},
                           {**concept('浸入边界','immersed boundary'),'role':'method','background_optional':False}]
    assert len(bs.compile_plan(json.dumps(data),'用格子玻尔兹曼结合浸入边界进行模拟研究',require_roles=True)['concept_groups'])==2


def test_design_objective_is_retrieved_separately_and_numeric_constraints_remain():
    data={'core_concepts':[
        {**concept('冗余架构','redundancy architecture'),'role':'subject','background_optional':False},
        {**concept('网络韧性','network resilience'),'role':'objective','background_optional':False},
        {**concept('300 K','300 K'),'role':'context','background_optional':False}],
        'search_groups':[{'label':'架构','additional_concepts':[]}]}
    plan=bs.compile_plan(json.dumps(data),'研究冗余架构在300 K环境下如何提升网络韧性',require_roles=True)
    assert plan['concept_groups']==[['redundancy architecture'],['300 K']]
    assert plan['research_aspects'][0]['concepts'][0]['en']==['network resilience']
    assert all('300 K' in p['en'] for p in plan['bilingual_queries'])

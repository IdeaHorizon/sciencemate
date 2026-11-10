import asyncio
import copy
import json
import time
import pytest
from nodes.literature.tools.academic_evidence import EvidenceReview, validate_review
from nodes.literature.tools.academic_admission import assess_concepts
from nodes.literature.tools.bilingual_strategy import compile_plan

STRATEGY = {'research_question': 'maize nitrogen field trials', 'concept_roles': ['subject', 'outcome', 'context'],
            'concept_groups': [['maize', 'corn'], ['nitrogen status'], ['field trial']],
            'core_concepts_zh': [['玉米'], ['氮状态'], ['田间试验']]}
PAPER = {'doi': '10.example/original', 'title': 'Maize nitrogen field trials',
         'abstract': 'We diagnosed crop nitrogen deficiency and adjusted fertilizer rates in field trials.'}

def row():
    p=copy.deepcopy(PAPER);p['relevance_assessment']=assess_concepts(p,STRATEGY);return p

def review(verdict='supported'):
    return {'concepts':[{'index':i,'verdict':verdict,'quote':quote if verdict!='unknown' else ''}
                       for i,quote in enumerate(['Maize','diagnosed crop nitrogen deficiency','field trials'])],
            'requirements':[]}


def test_hallucinated_or_incomplete_evidence_never_promotes():
    bad=review();bad['concepts'][1]['quote']='Measured leaf chlorophyll concentrations'
    with pytest.raises(ValueError,match='ungrounded_quote'):validate_review(bad,PAPER,3,0)
    bad=review();bad['concepts'].pop()
    with pytest.raises(ValueError,match='incomplete_review'):validate_review(bad,PAPER,3,0)
    bad=review();bad['concepts'][1]['index']=0
    with pytest.raises(ValueError):validate_review(bad,PAPER,3,0)


def test_only_supported_review_can_rescue_missing_literal_term(tmp_path):
    p=row();assert p['relevance_assessment']['tier']=='excluded'
    r=EvidenceReview(STRATEGY,cache_dir=tmp_path,config={})
    r.checked[r.key(p)]=validate_review(review(),p,3,0);r.apply(p)
    assert p['relevance_assessment']['tier']=='direct'
    p=row();r.checked[r.key(p)]=review('unknown');r.apply(p)
    assert p['relevance_assessment']['tier']=='excluded'
    p=row();r.checked[r.key(p)]=review('contradicted');r.apply(p)
    assert p['relevance_assessment']['exclusion_reason']=='study_conditions'
    p=row();p['relevance_assessment']['exclusion_reason']='explicit_exclusion'
    r.checked[r.key(p)]=review();r.apply(p)
    assert p['relevance_assessment']['exclusion_reason']=='explicit_exclusion'


def test_study_relationship_unknown_is_not_direct_and_cache_separates_questions(tmp_path):
    s={'concept_groups':[['validation']], 'study_requirements':[{'requirement':'independent institution'}]}
    p={'title':'Model validation', 'abstract':'Randomly split patients from the same institution.'}
    p['relevance_assessment']=assess_concepts(p,s)
    r=EvidenceReview(s,cache_dir=tmp_path,config={});r.apply(p)
    assert p['relevance_assessment']['tier']=='unconfirmed'
    s2={**s,'exclude_concepts':[{'en':['retrospective'],'zh':['回顾性']}]}
    assert r.key(p)!=EvidenceReview(s2,cache_dir=tmp_path,config={}).key(p)


@pytest.mark.asyncio
async def test_cache_reused_without_model_and_metadata_change_invalidates(monkeypatch,tmp_path):
    from nodes.literature.tools import academic_evidence as module
    calls=[]
    async def complete(*args,**kwargs):
        calls.append(args)
        return {'choices':[{'message':{'content':json.dumps({'papers':[{'id':0,'concepts':[{'index':0,'verdict':'supported','quote':'diagnosed crop nitrogen deficiency'}],'requirements':[]}]})},'finish_reason':'stop'}]}
    monkeypatch.setattr(module,'request_completion',complete)
    config={'api_base':'https://invalid','api_key':'not-a-real-key','model':'test'}
    r=EvidenceReview(STRATEGY,cache_dir=tmp_path,config=config);r.submit([row()]);await r.finish(.1)
    assert r.diag['reviewed']==1
    r2=EvidenceReview(STRATEGY,cache_dir=tmp_path,config=config);r2.submit([row()]);await r2.finish(0)
    assert r2.diag['cache_hits']==1 and len(calls)==1
    changed=row();changed['abstract']='Different evidence'
    assert r2.key(changed)!=r2.key(row())
    assert all('not-a-real-key' not in p.read_text() for p in tmp_path.glob('*.json'))


@pytest.mark.asyncio
async def test_slow_model_is_cancelled_at_final_wait_budget(monkeypatch,tmp_path):
    from nodes.literature.tools import academic_evidence as module
    started=asyncio.Event();stopped=asyncio.Event()
    async def slow(*args,**kwargs):
        started.set()
        try:await asyncio.sleep(60)
        finally:stopped.set()
    monkeypatch.setattr(module,'request_completion',slow)
    r=EvidenceReview(STRATEGY,cache_dir=tmp_path,config={'api_base':'https://invalid','api_key':'x','model':'x'})
    r.submit([row()]);await started.wait();start=time.monotonic();await r.finish(.03)
    assert time.monotonic()-start<.2 and stopped.is_set()
    assert all(task.done() for task in r.tasks) and r.diag['cancelled_batches']==1


def test_plan_cannot_invent_a_requirement():
    data={'core_concepts':[{'en':['validation'],'zh':['验证']}], 'search_groups':[{'additional_concepts':[]}],
          'study_requirements':[{'label':'外部验证','requirement':'Independent institution','source_quote':'外部机构验证'}]}
    with pytest.raises(ValueError,match='study_requirement_must_be_explicit'):
        compile_plan(json.dumps(data),'验证模型')
    plan=compile_plan(json.dumps(data),'模型外部机构验证')
    assert plan['study_requirements'][0]['requirement']=='Independent institution'


def test_semantic_rescue_cannot_relax_object_population_or_method(tmp_path):
    for role in ('subject', 'population', 'method', 'unknown'):
        strategy={**STRATEGY,'concept_roles':['subject',role,'context']}
        r=EvidenceReview(strategy,cache_dir=tmp_path,config={})
        assert not r.candidate(row())


def test_original_trials_do_not_include_reviews_or_meta_analyses(tmp_path):
    s={'concept_groups':[['TAVR'],['SAVR']],
       'study_requirements':[{'kind':'randomized_trial','requirement':'Original RCT'}]}
    r=EvidenceReview(s,cache_dir=tmp_path,config={})
    p={'title':'TAVR versus SAVR: a meta-analysis of randomized controlled trials'}
    p['relevance_assessment']=assess_concepts(p,s);r.apply(p)
    assert p['relevance_assessment']['exclusion_reason']=='study_conditions'
    p={'title':'TAVR versus SAVR in a randomized controlled trial'}
    p['relevance_assessment']=assess_concepts(p,s);r.apply(p)
    assert p['relevance_assessment']['tier']=='direct'


def test_trial_protocol_is_not_confirmed_as_completed_trial(tmp_path):
    s={'concept_groups':[['TAVR'],['SAVR']],
       'study_requirements':[{'kind':'randomized_trial','requirement':'Original RCT'}]}
    p={'title':'TAVR versus SAVR: protocol for a randomized controlled trial'}
    p['relevance_assessment']=assess_concepts(p,s)
    EvidenceReview(s,cache_dir=tmp_path,config={}).apply(p)
    assert p['relevance_assessment']['tier']=='excluded'


@pytest.mark.parametrize('title,abstract', [
    ('Comment on Exercise: a randomized controlled trial', ''),
    ('Response to Comment on Exercise: a randomized controlled trial', ''),
    ('Exercise: rationale and design of a randomized trial', ''),
    ('Exercise: a randomized controlled trial', 'We will include 32 adults. This trial will assess physical activity.'),
])
def test_cached_model_verdict_cannot_promote_comment_or_future_trial(tmp_path, title, abstract):
    s={'concept_groups':[['exercise']], 'study_requirements':[{'kind':'randomized_trial','requirement':'Original RCT'}]}
    r=EvidenceReview(s,cache_dir=tmp_path,config={})
    p={'title':title,'abstract':abstract};p['relevance_assessment']=assess_concepts(p,s)
    r.checked[r.key(p)]={'concepts':[], 'requirements':[{'index':0,'verdict':'supported','quote':title}]}
    r.apply(p)
    assert p['relevance_assessment']['exclusion_reason']=='study_conditions'


def test_completed_trial_is_not_confused_with_future_followup_or_general_topic(tmp_path):
    s={'concept_groups':[['exercise']], 'study_requirements':[{'kind':'randomized_trial','requirement':'Original RCT'}]}
    p={'title':'Exercise: a randomized controlled trial', 'abstract':'We enrolled 160 adults. Participants were randomized to two groups. We will assess long-term outcomes next year.'}
    p['relevance_assessment']=assess_concepts(p,s)
    EvidenceReview(s,cache_dir=tmp_path,config={}).apply(p)
    assert p['relevance_assessment']['tier']=='direct'
    # General searches may legitimately seek protocols/reviews.
    s={'concept_groups':[['exercise']]}
    p={'title':'Exercise: protocol for a randomized trial'};p['relevance_assessment']=assess_concepts(p,s)
    EvidenceReview(s,cache_dir=tmp_path,config={}).apply(p)
    assert p['relevance_assessment']['tier']=='direct'


@pytest.mark.parametrize('quote,tier', [
    ('Data from three independent institutions were used to develop a single model.', 'unconfirmed'),
    ('Model performance was tested using a random split at three institutions.', 'unconfirmed'),
    ('The model was validated across five independent geographically distinct hospitals.', 'direct'),
    ('The model was evaluated in an external hospital.', 'direct'),
    ('模型在未参与训练的医院进行了验证。', 'direct'),
])
def test_independent_validation_quote_must_describe_validation_relationship(tmp_path, quote, tier):
    s={'concept_groups':[['prediction']], 'study_requirements':[{'kind':'independent_validation','requirement':'Validate at an institution excluded from training'}]}
    p={'title':'Prediction study','abstract':quote};p['relevance_assessment']=assess_concepts(p,s)
    r=EvidenceReview(s,cache_dir=tmp_path,config={})
    r.checked[r.key(p)]={'concepts':[], 'requirements':[{'index':0,'verdict':'supported','quote':quote}]}
    r.apply(p)
    assert p['relevance_assessment']['tier']==tier


def test_trial_design_does_not_override_wrong_intervention_in_background(tmp_path):
    s={'concept_groups':[['intervention alpha'], ['obesity']],
       'concept_roles':['subject','population'],
       'study_requirements':[{'kind':'randomized_trial','requirement':'Original RCT'}]}
    p={'title':'Intervention beta for obesity: a randomized controlled trial',
       'abstract':'Unlike intervention alpha, we studied intervention beta in patients with obesity.'}
    p['relevance_assessment']=assess_concepts(p,s)
    r=EvidenceReview(s,cache_dir=tmp_path,config={})
    assert r.concepts_to_check(p)==[s['concept_groups'][0]]
    r.apply(p)
    assert p['relevance_assessment']['tier']=='unconfirmed'
    r.checked[r.key(p)]={'concepts':[{'index':0,'verdict':'contradicted','quote':'we studied intervention beta'}],
                        'requirements':[{'index':0,'verdict':'supported','quote':'randomized controlled trial'}]}
    r.apply(p)
    assert p['relevance_assessment']['tier']=='excluded'

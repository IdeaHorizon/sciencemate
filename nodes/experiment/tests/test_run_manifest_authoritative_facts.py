"""#895 A: a manifest indexes facts, not the last command or loop verdict."""
import asyncio

import pytest

from core.state import State
from nodes.experiment.tools import finish, ledger, run_contract, safe_bash
from test_v3_finish_reports_child_obligation_effect import _operation_run


@pytest.mark.parametrize('outcome', ['success', 'failed', 'partial', 'blocked', 'cancelled'])
def test_manifest_reads_sealed_outcome_after_reopen_without_replaying_finish(tmp_path, outcome):
    state, target = _operation_run(tmp_path)
    args = {'outcome': outcome, 'summary': '任务未完成'}
    if outcome == 'success':
        written = asyncio.run(safe_bash._safe_write_file(state, path=str(target), content='ok'))
        assert written['status'] == 'success', written
        args.update(summary='已交付', deliverables=[{'path': str(target), 'obligation_id': 'bin'}])
    if outcome == 'blocked':
        args['blocker'] = {'summary': '等待输入'}
    result = asyncio.run(finish._finish_for_model(state, **args))
    assert result['status'] == 'success', result
    reopened = State.reopen('experiment', tmp_path / 'runs', state.run_id,
                            project_id='p', session_id='owner')
    reopened.hook_state['node_inputs'] = dict(state.hook_state['node_inputs'])
    assert reopened.hook_state.get('node_task_outcome') is None
    manifest = run_contract.create_run_manifest(reopened, loop_result={'status': 'completed'})
    assert manifest['task_outcome'] == outcome
    assert manifest['status'] == {'success': 'completed', 'blocked': 'blocked'}.get(outcome, 'incomplete')
    assert manifest['lifecycle']['status'] == 'completed'
    assert manifest['delivery']['closure_id'] == result['closure_id']
    assert reopened.hook_state.get('node_task_outcome') is None  # projection, not repair


def test_manifest_does_not_infer_success_or_workload_exit_from_loop_or_log(tmp_path):
    state, _ = _operation_run(tmp_path)
    logs = state.root / 'outputs/experiment/runtime/logs'
    logs.mkdir(parents=True, exist_ok=True)
    (logs / '9999_diagnostic.log').write_text('# returncode: 0\n')
    manifest = run_contract.create_run_manifest(state, loop_result={'status': 'completed'})
    assert manifest['task_outcome'] is None
    assert manifest['return_code'] is None
    assert manifest['delivery']['error_code'] == 'closure_manifest_missing'
    assert manifest['logs']


def _job(state, nonce, exit_code):
    led = ledger.open_ledger(state, write=True)
    led.append_once('intent', nonce + ':intent', nonce=nonce, scheduler='slurm')
    led.append_once('job_bound', nonce + ':bound', nonce=nonce, external_id='job-' + nonce)
    led.append_once('job_terminal', nonce + ':terminal', nonce=nonce,
                    derived_state='COMPLETED' if exit_code == 0 else 'FAILED', exit_code=exit_code)


def test_manifest_keeps_exact_job_exits_separate_from_each_other_and_task_outcome(tmp_path):
    state, _ = _operation_run(tmp_path)
    _job(state, 'one', 7)
    one = run_contract.create_run_manifest(state, status='completed')
    assert one['return_code'] == 7
    assert one['jobs'][0]['external_id'] == 'job-one'
    _job(state, 'two', 0)
    many = run_contract.create_run_manifest(state, status='completed')
    assert many['return_code'] is None
    assert [(j['external_id'], j['terminal']['exit_code']) for j in many['jobs']] == [
        ('job-one', 7), ('job-two', 0)]
    assert many['task_outcome'] is None


def test_manifest_rejects_a_drifted_delivery_instead_of_reusing_hook_outcome(tmp_path):
    state, _ = _operation_run(tmp_path)
    sealed = asyncio.run(finish._finish_for_model(state, outcome='failed', summary='真实失败'))
    raw_id = sealed['artifact_ids']['raw_results']
    raw = state.read_artifact(raw_id)
    state.save_artifact('raw_results', raw_id.split('__', 1)[1], raw['content'] + '\n',
                        amendment_reason='Exercise drift from the sealed delivery identity')
    manifest = run_contract.create_run_manifest(state, status='completed')
    assert manifest['task_outcome'] is None
    assert manifest['delivery']['passed'] is False


@pytest.mark.parametrize('job_state', ['intent', 'submitted', 'unknown', 'terminal_unbound', 'terminal_no_exit'])
def test_manifest_keeps_unobserved_or_unbound_job_exits_unknown(tmp_path, job_state):
    state, _ = _operation_run(tmp_path)
    led = ledger.open_ledger(state, write=True)
    led.append('intent', nonce='one', scheduler='slurm')
    if job_state not in {'intent', 'terminal_unbound'}:
        led.append('job_bound', nonce='one', external_id='job-one')
    if job_state == 'unknown':
        led.append('job_unknown', nonce='one', reason='scheduler unavailable')
    elif job_state.startswith('terminal_'):
        led.append('job_terminal', nonce='one', derived_state='FAILED',
                   exit_code=7 if job_state == 'terminal_unbound' else None)

    manifest = run_contract.create_run_manifest(state, status='paused')

    assert manifest['return_code'] is None
    assert manifest['task_outcome'] is None
    assert manifest['status'] == manifest['lifecycle']['status'] == 'paused'
    assert manifest['jobs'][0]['nonce'] == 'one'


def test_manifest_does_not_use_one_terminal_exit_for_a_run_with_an_open_job(tmp_path):
    state, _ = _operation_run(tmp_path)
    _job(state, 'finished', 0)
    ledger.open_ledger(state, write=True).append('intent', nonce='open', scheduler='slurm')

    manifest = run_contract.create_run_manifest(state, status='running')

    assert manifest['return_code'] is None
    assert len(manifest['jobs']) == 2
    assert manifest['jobs'][0]['terminal']['exit_code'] == 0
    assert manifest['jobs'][1]['state'] == 'intent'


def test_manifest_does_not_replace_paused_lifecycle_with_sealed_task_outcome(tmp_path):
    state, _ = _operation_run(tmp_path)
    sealed = asyncio.run(finish._finish_for_model(state, outcome='failed', summary='真实失败'))
    assert sealed['status'] == 'success', sealed

    manifest = run_contract.create_run_manifest(state, status='paused')

    assert manifest['task_outcome'] == 'failed'
    assert manifest['status'] == manifest['lifecycle']['status'] == 'paused'


def test_manifest_does_not_claim_one_job_when_ledger_tail_is_incomplete(tmp_path):
    state, _ = _operation_run(tmp_path)
    _job(state, 'finished', 0)
    path = ledger.open_ledger(state, write=False).root / 'events.jsonl'
    complete = path.read_bytes()
    path.write_bytes(complete + b'{"kind":"intent"')

    manifest = run_contract.create_run_manifest(state, status='completed')

    assert manifest['jobs'][0]['terminal']['exit_code'] == 0
    assert manifest['return_code'] is None
    assert 'incomplete tail' in manifest['jobs_error']
    assert path.read_bytes() == complete + b'{"kind":"intent"'  # read-only projection

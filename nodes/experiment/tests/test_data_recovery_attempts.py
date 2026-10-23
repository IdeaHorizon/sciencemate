"""#1096: exercise retry through dispatch, not by replacing its receipt."""
import asyncio
import json

import pytest

from nodes.experiment.tools import contract_audit as ca
from nodes.experiment.tools import execution_action_census as census
from test_blocked_operation_closure import (
    _mark_child_terminal, _record_store, _save_record, _write_child_dataset,
)
from test_v3_data_handoff_without_a_scope import _v3_state, _service_spec


def _flow(tmp_path, monkeypatch, outcomes):
    state = _v3_state(tmp_path)
    validated = asyncio.run(ca._validate_data_request_spec(state, _service_spec()))
    assert validated['status'] == 'success', validated
    sid = validated['spec_id']
    calls = []

    async def child(**kwargs):
        calls.append(kwargs)
        run_id = f'data-recovery-{len(calls)}'
        root = state.root.parent / run_id
        root.mkdir()
        (root / 'transcript.jsonl').write_text(json.dumps({
            'event': 'run_start', 'node_type': 'data', 'parent_run_id': state.run_id,
        }) + '\n')
        outcome = outcomes[len(calls)-1]
        if outcome == 'dataset':
            aid = f'dataset__recovery_{len(calls)}'
            package = tmp_path / f'package-{len(calls)}'
            package.mkdir()
            (package / 'channel.msh').write_text('mesh bytes')
            _write_child_dataset(state, run_id, aid, package)
            status = 'completed'
        else:
            aid = f'preprocessing_blocked_report__recovery_{len(calls)}'
            store, directory = _record_store(state, run_id, None)
            _save_record(store, directory, artifact_id=aid,
                         artifact_type='preprocessing_blocked_report',
                         content=json.dumps({'status': outcome, 'reason': '缺少运行环境'}),
                         produced_by_node_type='data', produced_by_run_id=run_id)
            status = 'incomplete'
        _mark_child_terminal(state, run_id, status=status, artifact_ids=[aid])
        return {'status': status, 'child_status': status, 'child_run_id': run_id,
                'child_node_type': 'data', 'all_child_artifacts': [{'id': aid}]}

    monkeypatch.setattr('shared.tools.run_node._run_node_tool', child)
    return state, sid, calls


def _dispatch(state, sid):
    return asyncio.run(ca._dispatch_data_request(state, sid, user_note='取得求解器需要的网格'))


def _record(state, sid, dispatched):
    return asyncio.run(ca._record_data_delivery_outcome(
        state, sid, data_run_id=dispatched['child_run_id'],
        blocked_report_id=dispatched['all_child_artifacts'][0]['id']))


def test_recoverable_child_then_environment_restored_consumes_new_delivery(tmp_path, monkeypatch):
    state, sid, calls = _flow(tmp_path, monkeypatch, ['externally_blocked', 'dataset'])
    first = _dispatch(state, sid)
    recorded = _record(state, sid, first)
    assert recorded['outcome'] == 'recoverable'
    second = _dispatch(state, sid)
    assert second['status'] == 'completed', second
    assert len(calls) == 2
    assert calls[0]['node_inputs'] == calls[1]['node_inputs']
    assert calls[0]['resume_run_id'] == calls[1]['resume_run_id'] == 'fresh'
    assert _dispatch(state, sid)['status'] == 'error'  # consume, do not rerun
    assert len(calls) == 2
    consumed = asyncio.run(ca._verify_dataset_consumption(
        state, second['all_child_artifacts'][0]['id'], sid))
    assert consumed['status'] == 'success', consumed
    events = [e for e in ca._events(state) if e.get('event') == 'data_request_dispatched']
    assert [e['data_dispatch_receipt']['child_run_id'] for e in events] == [
        first['child_run_id'], second['child_run_id']]
    assert ca._read_child_run_artifact(state, first['child_run_id'], first['all_child_artifacts'][0]['id'])


@pytest.mark.parametrize('status', ['needs_input', 'externally_blocked', 'fatal', 'new_status_without_evidence'])
def test_two_recoverable_attempts_do_not_invent_data_unavailability(tmp_path, monkeypatch, status):
    state, sid, calls = _flow(tmp_path, monkeypatch, [status, status])
    assert _record(state, sid, _dispatch(state, sid))['outcome'] == 'recoverable'
    second = _dispatch(state, sid)
    assert second['status'] == 'incomplete', second
    result = _record(state, sid, second)
    assert result['outcome'] == 'recoverable', result
    assert result['data_terminally_blocked'] is False
    assert result['data_status'] == status
    assert len(calls) == 2


@pytest.mark.parametrize('status', ['running', 'paused', 'unknown', None])
def test_recoverable_report_cannot_authorize_retry_without_terminal_child(tmp_path, monkeypatch, status):
    state, sid, calls = _flow(tmp_path, monkeypatch, ['needs_input', 'dataset'])
    first = _dispatch(state, sid)
    assert _record(state, sid, first)['outcome'] == 'recoverable'
    monkeypatch.setattr(ca, '_child_run_terminal_summary', lambda *_: (
        None if status is None else {'status': status, 'artifacts': first['all_child_artifacts']}))
    assert _dispatch(state, sid)['status'] == 'error'
    assert len(calls) == 1


@pytest.mark.parametrize('latest_event', ['run_start', 'paused_run_end'])
def test_reopened_child_cannot_reuse_its_old_terminal_summary(tmp_path, monkeypatch, latest_event):
    state, sid, calls = _flow(tmp_path, monkeypatch, ['needs_input', 'dataset'])
    first = _dispatch(state, sid)
    assert _record(state, sid, first)['outcome'] == 'recoverable'
    path = state.root.parent / first['child_run_id'] / 'transcript.jsonl'
    event = ({'event': 'run_start', 'node_type': 'data', 'parent_run_id': state.run_id}
             if latest_event == 'run_start' else {'event': 'run_end', 'status': 'paused'})
    with path.open('a') as handle:
        handle.write(json.dumps(event) + '\n')
    assert ca._child_run_terminal_summary(state, first['child_run_id']) is None
    assert _dispatch(state, sid)['status'] == 'error'
    assert len(calls) == 1
    # A new durable terminal restores the legal recovery path.
    _mark_child_terminal(state, first['child_run_id'], status='incomplete',
                         artifact_ids=[first['all_child_artifacts'][0]['id']])
    assert _dispatch(state, sid)['status'] == 'completed'
    assert len(calls) == 2


def test_second_attempt_reconcile_does_not_match_first_attempt_event(tmp_path, monkeypatch):
    state, sid, calls = _flow(tmp_path, monkeypatch, ['needs_input', 'dataset'])
    assert _record(state, sid, _dispatch(state, sid))['outcome'] == 'recoverable'
    real_append = state.append_transcript

    def interrupted(event, **fields):
        if event == 'data_request_dispatched':
            raise OSError('second attempt receipt event interrupted')
        return real_append(event, **fields)

    with monkeypatch.context() as patch:
        patch.setattr(state, 'append_transcript', interrupted)
        second = _dispatch(state, sid)
    assert second.get('error_code') == 'dispatch_committed_but_receipt_event_not_durable', second
    rows = [a for a in census.reduce_execution_action_census(state)['actions']
            if a['tool'] == 'dispatch_data_request']
    assert len(rows) == 2  # the first event cannot hide the second ledger receipt
    assert rows[-1]['receipt_event'] == 'input_delivery_ledger'
    reconciled = asyncio.run(ca._reconcile_data_dispatch(state, sid))
    assert reconciled.get('receipt_event_recorded') is True, reconciled
    assert reconciled['data_run_id'] == second['child_run_id']
    assert len(calls) == 2
    assert len([e for e in ca._events(state) if e.get('event') == 'data_request_dispatched']) == 2


def test_paused_attempt_missing_event_reconciles_then_retries(tmp_path, monkeypatch):
    state, sid, calls = _flow(tmp_path, monkeypatch, ['needs_input', 'dataset'])
    from shared.tools import run_node
    original_child = run_node._run_node_tool

    async def paused_child(**kwargs):
        result = await original_child(**kwargs)
        return {**result, 'status': 'pause', 'child_status': 'paused'}

    real_append = state.append_transcript

    def interrupted(event, **fields):
        if event == 'data_request_dispatched':
            raise OSError('paused receipt event interrupted')
        return real_append(event, **fields)

    with monkeypatch.context() as patch:
        patch.setattr(run_node, '_run_node_tool', paused_child)
        patch.setattr(state, 'append_transcript', interrupted)
        first = _dispatch(state, sid)
    assert first.get('error_code') == 'dispatch_committed_but_receipt_event_not_durable'
    reconciled = asyncio.run(ca._reconcile_data_dispatch(state, sid))
    assert reconciled['receipt_event_recorded'] is True
    repeated = asyncio.run(ca._reconcile_data_dispatch(state, sid))
    assert repeated.get('receipt_event_recorded') is not True
    recorded = asyncio.run(ca._record_data_delivery_outcome(
        state, sid, data_run_id=reconciled['data_run_id'],
        blocked_report_id=reconciled['blocked_report_id']))
    assert recorded['outcome'] == 'recoverable'
    second = _dispatch(state, sid)
    assert second['status'] == 'completed', second
    assert len(calls) == 2
    # Late/repeated reconciliation of the old child cannot rewrite the new one.
    state.append_transcript('data_dispatch_pause_reconciled', **reconciled)
    rows = [a for a in census.reduce_execution_action_census(state)['actions']
            if a['tool'] == 'dispatch_data_request']
    assert [a['terminal_status'] for a in rows] == ['failed', 'success']
    stale = asyncio.run(ca._record_data_delivery_outcome(
        state, sid, data_run_id=reconciled['data_run_id'],
        blocked_report_id=reconciled['blocked_report_id']))
    assert stale['status'] == 'error'
    assert _dispatch(state, sid)['status'] == 'error'
    assert len(calls) == 2

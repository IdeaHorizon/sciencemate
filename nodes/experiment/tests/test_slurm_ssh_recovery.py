"""Remote ACK recovery stays on the persisted target and existing lifecycle."""
from __future__ import annotations

import copy
import json

import pytest

from core.state import State
from nodes.experiment.tools import external_submission_recovery as recovery, slurm_ssh

TARGET = {"host": "cluster-login", "user": "remote-user", "port": 2222, "login_shell": True}
NONCE = "route-ssh-123"


def _intent():
    return {
        "scheduler": "slurm", "slurm_target": copy.deepcopy(TARGET), "remote_user": "remote-user",
        "scheduler_cluster": "alpha", "launch_host": "cluster-login", "namespace": "remote-scope",
        "submission_nonce": NONCE, "route_attempt_id": NONCE, "submitted_at": "2026-10-22T01:02:03+00:00",
        "intent_status": "prepared", "status": "success", "dry_run": False,
        "script_sha256": "a" * 64, "workdir": "/scratch/work", "output_roots": ["/scratch/work/output"],
    }


def _ok(stdout=""):
    return {"ok": True, "stdout": stdout, "stderr": "", "returncode": 0}


def _ssh(monkeypatch, *, user="remote-user", cluster="alpha", queue=None, accounting=None, fail=None):
    calls = []

    def run(target, argv, timeout=20, *, expected_identity=None):
        calls.append((copy.deepcopy(target), list(argv), timeout))
        if argv[0] in {"squeue", "sacct"}:
            assert expected_identity == {"user": "remote-user", "cluster": "alpha"}
        if argv[0] == fail:
            return {"ok": False, "stdout": "", "stderr": "connection timed out", "returncode": None}
        if argv == ["id", "-un"]:
            return _ok(user + "\n")
        if argv[0] == "scontrol":
            return _ok(f"ClusterName = {cluster}\n")
        if argv[0] == "squeue":
            return _ok(f"42|ai4s:{NONCE}" if queue is None else queue)
        if argv[0] == "sacct":
            return _ok(f"42|ai4s:{NONCE}|{cluster}" if accounting is None else accounting)
        pytest.fail(f"unexpected remote operation: {argv}")

    monkeypatch.setattr(slurm_ssh, "run", run)
    return calls


def _no_local(*args, **kwargs):
    pytest.fail("a remote intent must never fall back to the worker's scheduler")


def _state_with_intent(tmp_path):
    state = State.new("experiment", tmp_path / "runs")
    intent = _intent()
    artifact = state.save_artifact("external_submission_intent", "remote-intent", json.dumps(intent),
                                   metadata={"submission_nonce": NONCE, "scheduler": "slurm", "route_attempt_id": NONCE})
    state.append_transcript("route_step_bound", attempt_id=NONCE, tool="submit_job", applied_policy="managed_external_job")
    state.append_transcript("route_step_outcome", attempt_id=NONCE, outcome="unknown", failure_class="external_identity_reconciliation_required")
    return state, {**intent, "_artifact_id": artifact["id"]}


def test_remote_query_uses_the_frozen_target_user_and_exact_nonce(monkeypatch):
    calls = _ssh(monkeypatch, queue=f"43|ai4s:{NONCE}-suffix\n42|ai4s:{NONCE}")
    intent = _intent()
    original = copy.deepcopy(intent)
    result = recovery._query_slurm(intent, _no_local)
    assert intent == original
    assert result["query_status"] == "unique", result
    assert result["identities"][0]["job_id"] == "42"
    assert result["identities"][0]["launch_host"] == intent["launch_host"]
    assert result["identities"][0]["namespace"] == intent["namespace"]
    assert result["identities"][0]["scheduler_cluster"] == "alpha"
    assert all(t == TARGET for t, argv, timeout in calls)
    queries = [argv for target, argv, timeout in calls if argv[0] in {"squeue", "sacct"}]
    assert len(queries) == 2 and all("--user=remote-user" in argv for argv in queries)


@pytest.mark.parametrize(("overrides", "reason"), [
    ({"user": "other-user"}, "slurm_remote_user_mismatch"),
    ({"cluster": "other-cluster"}, "slurm_cluster_identity_mismatch"),
    ({"fail": "id"}, "slurm_remote_user_unavailable"),
])
def test_changed_or_unreadable_remote_identity_does_not_query_another_scope(monkeypatch, overrides, reason):
    calls = _ssh(monkeypatch, **overrides)
    result = recovery._query_slurm(_intent(), _no_local)
    assert result["query_status"] == "query_error"
    assert result["reason"] == reason
    assert all(argv[0] not in {"squeue", "sacct"} for target, argv, timeout in calls)


def test_remote_timeout_is_unknown_and_the_same_nonce_can_be_queried_again(monkeypatch):
    calls = _ssh(monkeypatch, fail="squeue")
    first = recovery._query_slurm(_intent(), _no_local)
    assert first["query_status"] == "query_error"
    assert all(target == TARGET for target, argv, timeout in calls)
    _ssh(monkeypatch)
    second = recovery._query_slurm(_intent(), _no_local)
    assert second["query_status"] == "unique"
    assert second["identities"][0]["job_id"] == "42"


def test_two_remote_jobs_with_one_nonce_are_not_arbitrarily_adopted(monkeypatch):
    _ssh(monkeypatch, queue=f"42|ai4s:{NONCE}\n43|ai4s:{NONCE}")
    result = recovery._query_slurm(_intent(), _no_local)
    assert result["query_status"] == "multiple"
    assert result["candidate_count"] == 2


def test_foreign_cluster_accounting_row_does_not_rebind_the_target(monkeypatch):
    _ssh(monkeypatch, queue="", accounting=f"42|ai4s:{NONCE}|other-cluster")
    result = recovery._query_slurm(_intent(), _no_local)
    assert result["query_status"] == "query_error"
    assert result["reason"] == "slurm_candidate_cluster_mismatch"


def test_invalid_persisted_target_never_becomes_a_local_query(monkeypatch):
    calls = _ssh(monkeypatch)
    intent = {**_intent(), "slurm_target": {}}
    result = recovery._query_slurm(intent, _no_local)
    assert result["query_status"] == "query_error"
    assert result["reason"] == "slurm_target_invalid"
    assert calls == []


def test_legacy_intent_without_target_still_uses_the_existing_local_runner():
    intent = {key: value for key, value in _intent().items() if key not in {"slurm_target", "remote_user", "launch_host", "scheduler_cluster", "namespace"}}
    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        return _ok("ClusterName = local" if argv[0] == "scontrol" else f"42|ai4s:{NONCE}")

    result = recovery._query_slurm(intent, runner)
    assert result["query_status"] == "unique"
    assert [a[0] for a in calls] == ["scontrol", "squeue", "sacct"]


def test_receipt_and_dangling_projection_preserve_remote_target(tmp_path, monkeypatch):
    state, intent = _state_with_intent(tmp_path)
    dangling = recovery.dangling_external_submission_intents(state)
    assert len(dangling) == 1
    assert dangling[0]["slurm_target"] == TARGET
    assert dangling[0]["remote_user"] == "remote-user"
    assert dangling[0]["launch_host"] == "cluster-login"
    _ssh(monkeypatch)
    query = recovery._query_slurm(intent, _no_local)
    receipt = recovery._save_reconciliation(state, intent, query)
    assert receipt["slurm_target"] == TARGET
    assert receipt["remote_user"] == "remote-user"
    assert receipt["launch_host"] == "cluster-login"
    assert receipt["namespace"] == "remote-scope"
    assert receipt["scheduler_cluster"] == "alpha"


@pytest.mark.parametrize(("field", "value"), [
    ("slurm_target", {"host": "wrong-host"}), ("remote_user", "wrong-user"),
    ("scheduler_cluster", "wrong-cluster"), ("launch_host", "wrong-host"),
])
def test_conflicting_success_receipt_cannot_switch_remote_target(monkeypatch, field, value):
    _ssh(monkeypatch)
    intent = _intent()
    receipt = {**intent, "job_id": "42", field: value}
    with pytest.raises(recovery.SubmissionRecoveryError):
        recovery._normalized_known_receipt(receipt, NONCE, intent)


def test_missing_optional_remote_receipt_metadata_can_be_restored_from_intent(monkeypatch):
    _ssh(monkeypatch)
    intent = _intent()
    receipt = {"scheduler": "slurm", "submission_nonce": NONCE, "job_id": "42", "launch_host": None}
    result = recovery._normalized_known_receipt(receipt, NONCE, intent)
    assert result["slurm_target"] == TARGET
    assert result["remote_user"] == "remote-user"
    assert result["launch_host"] == "cluster-login"
    assert result["scheduler_cluster"] == "alpha"


def test_formal_recovery_after_timeout_adopts_existing_job_and_is_idempotent(tmp_path, monkeypatch):
    state, intent = _state_with_intent(tmp_path)
    first_calls = _ssh(monkeypatch, fail="squeue")
    pending = recovery.reconcile_external_submission(state, NONCE, query_runner=_no_local)
    assert pending["status"] == "reconciliation_pending", pending
    assert pending["do_not_resubmit"] is True
    assert all(argv[0] not in {"sbatch", "scancel"} for target, argv, timeout in first_calls)
    calls = _ssh(monkeypatch)
    adopted = recovery.reconcile_external_submission(state, NONCE, query_runner=_no_local)
    assert adopted["status"] == "success", adopted
    assert adopted["resolved_identity"]["job_id"] == "42"
    assert adopted["resolved_identity"]["launch_host"] == "cluster-login"
    payload = json.loads(state.read_artifact(adopted["recovery_artifact_id"])["content"])
    assert payload["slurm_target"] == TARGET and payload["remote_user"] == "remote-user"
    count = len(calls)
    repeated = recovery.reconcile_external_submission(state, NONCE, query_runner=_no_local)
    assert repeated["status"] == "success" and repeated["already_reconciled"] is True
    assert len(calls) == count


def test_formal_recovery_with_zero_matches_keeps_original_intent_open(tmp_path, monkeypatch):
    state, intent = _state_with_intent(tmp_path)
    calls = _ssh(monkeypatch, queue="", accounting="99|ai4s:other|alpha")
    result = recovery.reconcile_external_submission(state, NONCE, query_runner=_no_local)
    assert result["status"] == "reconciliation_pending"
    assert result["do_not_resubmit"] is True
    assert recovery.dangling_external_submission_intents(state)[0]["slurm_target"] == TARGET
    assert all(argv[0] not in {"sbatch", "scancel"} for target, argv, timeout in calls)


def _v3_nonce_intent(tmp_path):
    import asyncio
    from nodes.experiment.tools import ledger, run_bootstrap
    state = State.new('experiment', tmp_path / 'runs', session_id='owner')
    state.hook_state['node_inputs'] = {
        'scope': 'operation', 'operation_category': 'scheduler_probe',
        'experiment_spec': 'Recover the authorized remote Slurm submission.',
        'prereg_assignment': {'kind': 'none', 'reason': 'operational recovery regression'},
    }
    asyncio.run(run_bootstrap.establish(state))
    intent = {**_intent(), 'route_attempt_id': None}
    state.save_artifact('external_submission_intent', 'remote-intent', json.dumps(intent))
    ledger.record(state, 'intent', nonce=NONCE, **{
        key: intent[key] for key in ('scheduler', 'slurm_target', 'remote_user', 'scheduler_cluster', 'launch_host')})
    return state, intent


@pytest.mark.parametrize('standalone', [False, True])
def test_v3_nonce_recovers_lost_ack_into_existing_ledger_without_legacy_route(tmp_path, monkeypatch, standalone):
    from nodes.experiment.tools import ledger
    if standalone:
        import sys
        import types
        from nodes.experiment import tools as node_tools
        monkeypatch.setattr(recovery, '__package__', '')
        monkeypatch.setitem(sys.modules, 'tools', node_tools)
        for name, module in vars(node_tools).items():
            if isinstance(module, types.ModuleType):
                monkeypatch.setitem(sys.modules, 'tools.' + name, module)
    state, intent = _v3_nonce_intent(tmp_path)
    assert recovery.dangling_external_submission_intents(state)[0]['submission_nonce'] == NONCE
    calls = _ssh(monkeypatch)
    result = recovery.reconcile_external_submission(state, NONCE, query_runner=_no_local)
    assert result['status'] == 'success', result
    book = ledger.open_ledger(state)
    assert ledger.project_jobs(book.records())[NONCE]['external_id'] == '42'
    assert len([row for row in book.records() if row['kind'] == 'job_reconciled']) == 1
    assert not recovery.dangling_external_submission_intents(state)
    count = len(calls)
    repeated = recovery.reconcile_external_submission(state, NONCE, query_runner=_no_local)
    assert repeated['status'] == 'success' and repeated['already_reconciled']
    assert len(calls) == count
    assert len([row for row in book.records() if row['kind'] == 'job_reconciled']) == 1
    assert all(argv[0] not in {'sbatch', 'scancel'} for target, argv, timeout in calls)


@pytest.mark.parametrize('damage', ['target', 'owner', 'bound_job'])
def test_v3_nonce_cannot_change_target_owner_or_already_bound_job(tmp_path, monkeypatch, damage):
    from nodes.experiment.tools import ledger
    state, intent = _v3_nonce_intent(tmp_path)
    if damage == 'target':
        state.save_artifact('external_submission_intent', 'remote-intent', json.dumps({
            **intent, 'slurm_target': {**TARGET, 'host': 'another-cluster'}}))
    elif damage == 'owner':
        state.session_id = 'other-session'
    else:
        ledger.record(state, 'job_bound', nonce=NONCE, scheduler='slurm', external_id='99')
    calls = _ssh(monkeypatch)
    result = recovery.reconcile_external_submission(state, NONCE, query_runner=_no_local)
    assert result['status'] != 'success' and result['do_not_resubmit'], result
    if damage != 'bound_job':
        assert not calls, 'invalid authority must be rejected before any remote query'
    assert ledger.project_jobs(ledger.open_ledger(state).records())[NONCE]['external_id'] == ('99' if damage == 'bound_job' else None)

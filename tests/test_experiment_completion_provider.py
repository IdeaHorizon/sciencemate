"""The completion provider observes C without repairing it or inventing outcomes."""

from core.completion import CompletionContext
from core.loader import load_harness
from nodes.experiment.completion_admission import experiment_completion_admission
from nodes.experiment.tests.test_060_core_completion_handoff import _manifest, _state
from nodes.experiment.tools import delivery_transaction as tx


def context(state):
    return CompletionContext(load_harness("experiment"), state, [], 1, True)


def test_missing_manifest_is_not_an_admitted_completion(tmp_path):
    state, _ = _state(tmp_path)
    result = experiment_completion_admission(context(state), None)
    assert result.disposition == "reenter"
    assert result.code == "closure_manifest_missing"
    assert result.task_outcome is None
    assert not tx.manifest_path(state).exists()


def test_core_progress_events_do_not_change_the_provider_read_set(tmp_path):
    state, _ = _state(tmp_path)
    first = experiment_completion_admission(context(state), None)
    state.append_transcript("completion_admission", code=first.code)
    state.append_transcript("model_reply", content="I should finish the task")
    second = experiment_completion_admission(context(state), None)
    assert first.fact_revision == second.fact_revision


def test_sealed_failure_is_complete_delivery_with_a_failed_task(tmp_path):
    state, ledger = _state(tmp_path)
    tx.settle_delivery(state, ledger, _manifest(state, outcome="failed"))
    before = tx.manifest_path(state).read_bytes()
    result = experiment_completion_admission(context(state), None)
    assert result.disposition == "allow"
    assert result.task_outcome == "failed"
    assert tx.manifest_path(state).read_bytes() == before
    assert not state.hook_state.get("blockers")


def test_same_error_with_a_real_new_ledger_fact_has_a_new_revision(tmp_path):
    state, ledger = _state(tmp_path)
    first = experiment_completion_admission(context(state), None)
    ledger.append("observation", action_id="a1", source="actual measured fact")
    second = experiment_completion_admission(context(state), None)
    assert first.code == second.code
    assert first.fact_revision != second.fact_revision

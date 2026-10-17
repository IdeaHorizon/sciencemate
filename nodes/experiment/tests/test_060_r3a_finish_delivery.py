"""R3-A finish vertical: canonical A/B -> one ExperimentDeliveryV1 transaction."""
from __future__ import annotations

import asyncio
import json

import pytest

from core.state import State
from nodes.experiment.tools import delivery_transaction as tx
from nodes.experiment.tools import finish, ledger, run_bootstrap


NON_SUCCESS = ("partial", "failed", "blocked", "cancelled")
ALL_OUTCOMES = ("success", *NON_SUCCESS)


def _run(awaitable):
    return asyncio.run(awaitable)


def _state(
    tmp_path,
    *,
    scope: str | None = "operation",
    prereg_assignment: dict | None = None,
    with_check: bool = False,
):
    state = State.new(
        node_type="experiment", base_dir=tmp_path / "runs",
        project_id="p", session_id="owner",
    )
    inputs = {
        "experiment_spec": "exercise the finish delivery transaction",
        "prereg_assignment": prereg_assignment or {"kind": "none", "reason": "fixture"},
        "required_outputs": ([{
            "id": "smoke", "kind": "check", "command": "true", "expect_exit": 0,
        }] if with_check else []),
    }
    if scope is not None:
        inputs["scope"] = scope
    if scope == "operation":
        inputs["operation_category"] = "environment_probe"
    state.hook_state["node_inputs"] = inputs
    established = _run(run_bootstrap.establish(state))
    assert ledger.bootstrap_phase(state) == "ready", established
    led = ledger.open_ledger(state, write=True)
    assert not led.read_only, led.reason
    return state, led


def _settled_experiment_action(led):
    led.append(
        "action_started", action_id="experiment-1", tool="safe_run_bash",
        effect_class="experiment", command="true", work_root="build",
    )
    led.append(
        "action_settled", action_id="experiment-1", state="settled", exit_code=0,
    )


def _finish_rows(led):
    return [row for row in led.records() if row.get("kind") == "finish"]


@pytest.fixture
def check_ok(monkeypatch):
    async def runner(_state, _obligation):
        return {"exit_code": 0, "stdout_tail": "ok", "stderr_tail": "", "error": None}

    monkeypatch.setattr(finish, "_check_runner", runner)


@pytest.mark.parametrize("outcome", ALL_OUTCOMES)
def test_operation_all_five_outcomes_use_one_neutral_transaction(tmp_path, check_ok, outcome):
    state, led = _state(tmp_path, with_check=True)
    _settled_experiment_action(led)
    kwargs = ({"blocker": {"blocker_id": "fixture", "summary": "blocked honestly"}}
              if outcome == "blocked" else {})

    result = _run(finish._finish(state, outcome=outcome, summary=f"operation {outcome}", **kwargs))

    assert result["status"] == "success", result
    assert result["outcome"] == outcome
    delivery = result["delivery"]
    assert delivery["schema"] == "ExperimentDeliveryV1"
    assert delivery["delivery_owner"] == "finish"
    assert delivery["scope"] == "operation" and delivery["scope_status"] == "resolved"
    assert delivery["outcome"] == outcome
    assert delivery["formal_input"] == {"status": "none", "binding": None, "error": None}
    assert delivery["execution_status"] == "attempted"
    assert delivery["result_status"] == ("delivered" if outcome == "success" else "partial")
    manifest = tx.read_manifest(state)
    assert manifest["schema"] == tx.MANIFEST_SCHEMA
    assert manifest["closure_id"] == tx.delivery_id(state)
    assert len(_finish_rows(led)) == 1
    for artifact_type, artifact_id in result["artifact_ids"].items():
        record = state.read_artifact(artifact_id)
        assert record["metadata"]["record_kind"] == "experiment_delivery"
        assert record["metadata"]["artifact_role"] == artifact_type
        assert state.artifact_head(artifact_id).frozen is True


def test_finish_discloses_scope_only_change_during_async_evaluation(tmp_path, monkeypatch):
    state, led = _state(tmp_path, with_check=True)
    _settled_experiment_action(led)

    async def runner(check_state, _obligation):
        check_state.hook_state["node_inputs"]["scope"] = "scientific"
        check_state.hook_state["node_inputs"].pop("operation_category", None)
        await run_bootstrap.establish(check_state)
        return {"exit_code": 0, "stdout_tail": "ok", "stderr_tail": "", "error": None}

    monkeypatch.setattr(finish, "_check_runner", runner)
    result = _run(finish._finish(state, outcome="success", summary="check completed"))

    final_authority = run_bootstrap.project_run_authority(led.records())
    assert final_authority["startup_blocker"]["code"] == "scope_resolution_conflict"
    assert result["status"] == "success", result
    assert result["delivery"]["scope"] == "operation"
    assert any("scope_resolution_conflict" in item
               for item in result["delivery"]["limitations"])
    assert not result["delivery"]["contradictions"]
    assert tx.read_manifest(state) is not None
    assert len(_finish_rows(led)) == 1


def test_interrupted_delivery_does_not_abandon_a_later_open_job(tmp_path, monkeypatch):
    state, led = _state(tmp_path)
    real_freeze = state.mark_frozen
    def interrupted(*args, **kwargs):
        raise OSError("interrupted finish")
    monkeypatch.setattr(state, "mark_frozen", interrupted)
    with pytest.raises(OSError, match="interrupted finish"):
        _run(finish._finish(state, outcome="failed", summary="first delivery"))
    monkeypatch.setattr(state, "mark_frozen", real_freeze)
    led.append("intent", nonce="later-job", scheduler="slurm", output_root="job-output")
    led.append("job_bound", nonce="later-job", external_id="77")
    result = _run(finish._finish(state, outcome="failed", summary="retry"))
    assert result["status"] == "live_external_effect"
    assert not _finish_rows(led)
    led.append("job_terminal", nonce="later-job", derived_state="COMPLETED", exit_code="0:0")
    recovered = _run(finish._finish(state, outcome="failed", summary="retry"))
    assert recovered["status"] == "success" and recovered["reused_manifest"] is True
    assert recovered["delivery"]["summary"] == "first delivery"
    assert len(_finish_rows(led)) == 1


def test_final_delivery_snapshot_preserves_unbound_failure_without_new_task_condition(tmp_path, check_ok, monkeypatch):
    state, led = _state(tmp_path, with_check=True)
    _settled_experiment_action(led)
    real_evaluate = finish.evaluate
    async def evaluate_then_failure(state, led, verification=None, deliverables=None):
        result = await real_evaluate(state, led, verification, deliverables)
        led.append("action_started", action_id="later", tool="safe_run_bash", effect_class="experiment")
        led.append("action_settled", action_id="later", state="settled", exit_code=1)
        return result
    monkeypatch.setattr(finish, "evaluate", evaluate_then_failure)
    result = _run(finish._finish(state, outcome="success", summary="The fixed check passed; the unbound failure remains disclosed."))
    # No current object or formal condition binds this auxiliary failure.
    # Preserve the final B snapshot, not the retired global failure veto.
    assert result["status"] == "success" and result["outcome"] == "success"
    assert not result["delivery"]["contradictions"]
    assert any("later" in item for item in result["delivery"]["limitations"])
    raw = json.loads(state.read_artifact(result["artifact_ids"]["raw_results"])["content"])
    assert any(row.get("kind") == "action_settled" and row.get("action_id") == "later"
               and row["exit_code"] == 1 for row in raw["ledger_records"])
    assert len(_finish_rows(led)) == 1 and tx.read_manifest(state) is not None


def test_fresh_finish_uses_frozen_ledger_authority_not_mutated_node_inputs(tmp_path):
    state, led = _state(tmp_path, scope="operation")
    state.hook_state["node_inputs"]["scope"] = "scientific"
    state.hook_state["node_inputs"]["prereg_assignment"] = {"kind": "pending"}

    result = _run(finish._finish(state, outcome="failed", summary="authority stays operation"))

    assert result["status"] == "success", result
    assert result["delivery"]["scope"] == "operation"
    assert result["delivery"]["formal_input"]["status"] == "none"
    assert not [row for row in led.records() if row.get("kind") == "scope_resolved"]


@pytest.mark.parametrize("outcome", NON_SUCCESS)
def test_unknown_scope_is_disclosed_without_inventing_a_completion_gap(tmp_path, outcome):
    state, _led = _state(tmp_path, scope=None)
    kwargs = ({"blocker": {"summary": "scope is still unknown"}}
              if outcome == "blocked" else {})

    result = _run(finish._finish(state, outcome=outcome, summary="unknown but honest", **kwargs))

    assert result["status"] == "success", result
    delivery = result["delivery"]
    assert delivery["scope"] == "unknown" and delivery["scope_status"] == "undecided"
    assert delivery["outcome"] == outcome
    assert delivery["formal_input"] == {"status": "none", "binding": None, "error": None}
    assert not any(gap.get("name") == "scope_undecided" for gap in delivery["gaps"])
    assert any("scope remains unknown" in item for item in delivery["limitations"])


@pytest.mark.parametrize("outcome", NON_SUCCESS)
def test_scientific_formal_input_error_is_a_typed_non_success_delivery(tmp_path, outcome):
    state, _led = _state(
        tmp_path, scope=None, prereg_assignment={"kind": "pending"},
    )
    kwargs = ({"blocker": {"summary": "formal input is pending"}}
              if outcome == "blocked" else {})

    result = _run(finish._finish(state, outcome=outcome, summary="formal input unavailable", **kwargs))

    assert result["status"] == "success", result
    delivery = result["delivery"]
    assert delivery["scope"] == "scientific" and delivery["scope_status"] == "resolved"
    assert delivery["outcome"] == outcome
    assert delivery["formal_input"]["status"] == "pending"
    assert delivery["formal_input"]["binding"] is None
    formal_error = delivery["formal_input"]["error"]
    assert formal_error["code"] == "prereg_assignment_pending"
    assert set(formal_error) == {"code", "reason"}
    assert any(gap.get("name") == "formal_input_unavailable" for gap in delivery["gaps"])


def test_scientific_valid_formal_input_uses_the_exact_frozen_binding(tmp_path):
    from core.ledger import record_version

    state = State.new(
        node_type="experiment", base_dir=tmp_path / "runs",
        project_id="p", session_id="owner",
    )
    saved = state.save_artifact(
        "pre_registration", "prereg__exact", "# prereg\n",
        metadata={"execution_mode": "scientific"},
    )
    state.mark_frozen(saved["id"])
    record = state.artifact_versions(saved["id"])[-1]
    binding = {
        "kind": "bound",
        "artifact_id": saved["id"],
        "version": record_version(record),
        "content_hash": record["content_hash"],
    }
    state.hook_state["node_inputs"] = {
        "experiment_spec": "exercise a valid scientific formal input",
        "prereg_assignment": binding,
        "required_outputs": [],
    }
    established = _run(run_bootstrap.establish(state))
    assert ledger.bootstrap_phase(state) == "ready", established

    result = _run(finish._finish(
        state, outcome="failed", summary="valid formal input, honest failed result",
    ))

    assert result["status"] == "success", result
    formal = result["delivery"]["formal_input"]
    assert formal == {
        "status": "valid",
        "binding": {key: binding[key] for key in ("artifact_id", "version", "content_hash")},
        "error": None,
    }


def test_unknown_success_still_needs_task_verification_before_manifest(tmp_path):
    state, led = _state(tmp_path, scope=None)

    result = _run(finish._finish(state, outcome="success", summary="unsupported success"))

    assert result["status"] == "success_unsupported", result
    assert any(row.get("gap") == "task_verification_missing" for row in result["gaps"]), result
    assert tx.read_manifest(state) is None
    assert _finish_rows(led) == []


def _add_open_fact(led, fact):
    if fact in {"intent", "submitted", "unknown"}:
        led.append("intent", nonce="n1", scheduler="slurm", output_root="managed")
    if fact == "submitted":
        led.append("job_bound", nonce="n1", external_id="42")
    elif fact == "unknown":
        led.append("submission_attempted", nonce="n1", scheduler="slurm")
        led.append("job_unknown", nonce="n1", reason="scheduler reply was ambiguous")
    elif fact == "action":
        led.append(
            "action_started", action_id="open-1", tool="safe_run_bash",
            effect_class="experiment", command="long-running", work_root="build",
        )


@pytest.mark.parametrize("fact", ("intent", "submitted", "unknown", "action"))
@pytest.mark.parametrize("outcome", ALL_OUTCOMES)
def test_open_or_unknown_effect_blocks_every_outcome_without_manifest(tmp_path, fact, outcome):
    state, led = _state(tmp_path, scope="operation")
    _add_open_fact(led, fact)
    kwargs = ({"blocker": {"summary": "cannot settle the effect yet"}}
              if outcome == "blocked" else {})

    result = _run(finish._finish(state, outcome=outcome, summary="must remain open", **kwargs))

    assert result["status"] == "live_external_effect", (fact, outcome, result)
    assert result["error_code"] == "terminal_closure_with_live_or_unknown_effect"
    assert tx.read_manifest(state) is None
    assert _finish_rows(led) == []


def test_existing_manifest_recovers_first_delivery_without_re_evaluation_or_new_parameters(
    tmp_path, monkeypatch,
):
    state, led = _state(tmp_path, scope="operation")
    real_mark_frozen = state.mark_frozen
    crashed = {"done": False}

    def freeze_then_raise(artifact_id, *args, **kwargs):
        record = real_mark_frozen(artifact_id, *args, **kwargs)
        if not crashed["done"]:
            crashed["done"] = True
            raise OSError("injected after first freeze")
        return record

    monkeypatch.setattr(state, "mark_frozen", freeze_then_raise)
    with pytest.raises(OSError, match="injected after first freeze"):
        _run(finish._finish(state, outcome="failed", summary="the first immutable delivery"))
    persisted = tx.read_manifest(state)
    assert persisted is not None
    assert persisted["delivery"]["outcome"] == "failed"
    monkeypatch.setattr(state, "mark_frozen", real_mark_frozen)

    async def evaluate_must_not_run(*_args, **_kwargs):
        raise AssertionError("recovery must not evaluate fresh state")

    monkeypatch.setattr(finish, "evaluate", evaluate_must_not_run)
    recovered = _run(finish._finish(
        state, outcome="not-an-outcome", summary="", unexpected_new_parameter=True,
    ))

    assert recovered["status"] == "success", recovered
    assert recovered["reused_manifest"] is True
    assert recovered["delivery"]["outcome"] == "failed"
    assert recovered["delivery"]["summary"] == "the first immutable delivery"
    assert len(_finish_rows(led)) == 1
    assert all(len(state.artifact_versions(artifact_id)) == 1
               for artifact_id in recovered["artifact_ids"].values())

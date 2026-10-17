"""Adversarial readback regressions for the ExperimentDeliveryV1 cutover.

These cases preserve the semantics of the independent d083 review without
reviving its retired ``operation_closure_input`` authority.  The persisted
transaction manifest is the delivery authority; every frozen artifact must
retain the exact metadata digest that binds it to that authority.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from core.state import State
from nodes.experiment.tools import contract_audit
from nodes.experiment.tools import delivery_transaction as tx
from nodes.experiment.tools import finish, ledger, run_bootstrap


def _run(awaitable):
    return asyncio.run(awaitable)


def _state(tmp_path: Path) -> tuple[State, ledger.Ledger]:
    state = State.new(
        node_type="experiment",
        base_dir=tmp_path / "runs",
        project_id="p",
        session_id="owner",
    )
    state.hook_state["node_inputs"] = {
        "experiment_spec": "exercise adversarial delivery readback",
        "prereg_assignment": {"kind": "none", "reason": "operation fixture"},
        "scope": "operation",
        "operation_category": "environment_probe",
        "required_outputs": [],
    }
    established = _run(run_bootstrap.establish(state))
    assert ledger.bootstrap_phase(state) == "ready", established
    led = ledger.open_ledger(state, write=True)
    assert not led.read_only, led.reason
    return state, led


def _finish_rows(led: ledger.Ledger) -> list[dict]:
    return [row for row in led.records() if row.get("kind") == "finish"]


def _ledger_bytes(state: State) -> bytes:
    path = ledger.ledger_dir(state) / "events.jsonl"
    return path.read_bytes()


def _mutate_readback_metadata(
    monkeypatch,
    state: State,
    artifact_ids: set[str],
    mutate,
) -> None:
    real_read = state.read_artifact

    def read_with_mutation(artifact_id: str):
        record = real_read(artifact_id)
        if artifact_id in artifact_ids and isinstance(record, dict):
            record = dict(record)
            metadata = dict(record.get("metadata") or {})
            mutate(metadata)
            record["metadata"] = metadata
        return record

    monkeypatch.setattr(state, "read_artifact", read_with_mutation)


@pytest.mark.parametrize("artifact_type", tx.TRIO)
@pytest.mark.parametrize("mutation", ("missing", "wrong_type"))
def test_every_frozen_artifact_requires_an_exact_delivery_envelope_digest(
    tmp_path: Path,
    monkeypatch,
    artifact_type: str,
    mutation: str,
) -> None:
    state, led = _state(tmp_path)
    first = _run(finish._finish(state, outcome="failed", summary="failed honestly"))
    assert first["status"] == "success", first
    manifest_before = tx.manifest_path(state).read_bytes()
    ledger_before = _ledger_bytes(state)
    artifact_id = first["artifact_ids"][artifact_type]

    def mutate(metadata: dict) -> None:
        if mutation == "missing":
            metadata.pop("envelope_sha256")
        else:
            metadata["envelope_sha256"] = [metadata["envelope_sha256"]]

    _mutate_readback_metadata(monkeypatch, state, {artifact_id}, mutate)

    replay = _run(finish._finish(
        state,
        outcome="success",
        summary="readback must not trust a malformed envelope",
    ))
    audit = contract_audit.audit_experiment_delivery(state)

    assert replay["status"] == "error", replay
    assert replay["error_code"] == "closure_head_mismatch", replay
    assert replay["artifact_type"] == artifact_type
    assert "metadata" in replay["mismatches"]
    assert audit["passed"] is False, audit
    assert audit["error_code"] == "closure_head_mismatch", audit
    assert audit["facts"]["artifact_type"] == artifact_type
    assert "metadata" in audit["facts"]["mismatches"]
    assert tx.manifest_path(state).read_bytes() == manifest_before
    assert _ledger_bytes(state) == ledger_before
    assert len(_finish_rows(led)) == 1


def test_three_matching_forged_success_digests_do_not_override_the_manifest(
    tmp_path: Path,
    monkeypatch,
) -> None:
    state, led = _state(tmp_path)
    first = _run(finish._finish(state, outcome="failed", summary="failed honestly"))
    assert first["status"] == "success", first
    manifest = tx.read_manifest(state)
    assert manifest is not None
    assert manifest["delivery"]["outcome"] == "failed"

    forged_delivery = dict(manifest["delivery"])
    forged_delivery.update({
        "outcome": "success",
        "execution_status": "attempted",
        "result_status": "delivered",
        "gaps": [],
        "contradictions": [],
        "blocker": None,
    })
    tx.validate_delivery(state, forged_delivery)
    forged_digest = tx.canonical_digest(forged_delivery)
    assert forged_digest != manifest["delivery_digest"]
    manifest_before = tx.manifest_path(state).read_bytes()
    ledger_before = _ledger_bytes(state)

    def forge_success(metadata: dict) -> None:
        metadata["envelope_sha256"] = forged_digest

    _mutate_readback_metadata(
        monkeypatch,
        state,
        set(first["artifact_ids"].values()),
        forge_success,
    )
    observed = {
        state.read_artifact(artifact_id)["metadata"]["envelope_sha256"]
        for artifact_id in first["artifact_ids"].values()
    }
    assert observed == {forged_digest}

    replay = _run(finish._finish(
        state,
        outcome="success",
        summary="three matching forgeries are not the manifest authority",
    ))
    audit = contract_audit.audit_experiment_delivery(state)

    assert replay["status"] == "error", replay
    assert replay["error_code"] == "closure_head_mismatch", replay
    assert replay["artifact_type"] in tx.TRIO
    assert "metadata" in replay["mismatches"]
    assert audit["passed"] is False, audit
    assert audit["error_code"] == "closure_head_mismatch", audit
    assert tx.read_manifest(state)["delivery"]["outcome"] == "failed"
    assert tx.manifest_path(state).read_bytes() == manifest_before
    assert _ledger_bytes(state) == ledger_before
    assert len(_finish_rows(led)) == 1


def test_blocked_partial_freeze_recovers_without_repeating_the_blocker(
    tmp_path: Path,
    monkeypatch,
) -> None:
    state, led = _state(tmp_path)
    blocker = {
        "blocker_id": "upstream-input-unavailable",
        "summary": "the required upstream input is unavailable",
    }
    real_mark_frozen = state.mark_frozen
    interrupted = {"done": False}

    def freeze_then_raise(artifact_id, *args, **kwargs):
        record = real_mark_frozen(artifact_id, *args, **kwargs)
        if not interrupted["done"]:
            interrupted["done"] = True
            raise OSError("injected after the first freeze")
        return record

    monkeypatch.setattr(state, "mark_frozen", freeze_then_raise)
    with pytest.raises(OSError, match="injected after the first freeze"):
        _run(finish._finish(
            state,
            outcome="blocked",
            summary="blocked before execution",
            blocker=blocker,
        ))

    manifest = tx.read_manifest(state)
    assert manifest is not None
    assert manifest["delivery"]["outcome"] == "blocked"
    assert manifest["delivery"]["blocker"] == blocker
    assert any(
        state.artifact_head(manifest["artifacts"][kind]["id"]).frozen
        for kind in tx.TRIO
    )
    assert not _finish_rows(led)
    monkeypatch.setattr(state, "mark_frozen", real_mark_frozen)

    async def evaluate_must_not_run(*_args, **_kwargs):
        raise AssertionError("manifest recovery must not evaluate fresh parameters")

    monkeypatch.setattr(finish, "evaluate", evaluate_must_not_run)
    recovered = _run(finish._finish(
        state,
        outcome="blocked",
        summary="retry deliberately omits blocker",
    ))

    assert recovered["status"] == "success", recovered
    assert recovered["reused_manifest"] is True
    assert recovered["delivery"]["outcome"] == "blocked"
    assert recovered["delivery"]["summary"] == "blocked before execution"
    assert recovered["delivery"]["blocker"] == blocker
    assert len(_finish_rows(led)) == 1
    assert all(
        state.artifact_head(artifact_id).frozen
        and len(state.artifact_versions(artifact_id)) == 1
        for artifact_id in recovered["artifact_ids"].values()
    )
    assert contract_audit.audit_experiment_delivery(state)["passed"] is True

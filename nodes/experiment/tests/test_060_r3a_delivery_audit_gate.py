"""R3-A: v3 reserved freeze gates and the read-only delivery audit."""
from __future__ import annotations

import asyncio
import json

import pytest

from core.state import State
from nodes.experiment.tools import contract_audit
from nodes.experiment.tools import delivery_transaction as tx
from nodes.experiment.tools import ledger
from shared.tools.library.artifacts_extra import _freeze_artifact


def _state(tmp_path):
    state = State.new(
        node_type="experiment",
        base_dir=tmp_path / "runs",
        project_id="p",
        session_id="owner",
    )
    led = ledger.open_ledger(state, write=True, bootstrap=True)
    ledger.mark_bootstrap_ready(state)
    return state, led


def _manifest(state):
    delivery = {
        "schema": tx.DELIVERY_SCHEMA,
        "delivery_id": tx.delivery_id(state),
        "delivery_owner": "finish",
        "scope": "operation",
        "scope_status": "resolved",
        "outcome": "failed",
        "execution_status": "attempted",
        "result_status": "unavailable",
        "summary": "the attempted operation failed honestly",
        "gaps": [],
        "contradictions": [],
        "formal_input": {"status": "none", "binding": None, "error": None},
        "limitations": [],
        "external_job_refs": [],
        "next_step": None,
        "blocker": None,
    }
    names = tx.deterministic_names(state)
    raw_id = tx.artifact_id("raw_results", names["raw_results"])
    return tx.build_manifest(
        state,
        delivery=delivery,
        contents={
            "raw_results": json.dumps(
                {"delivery": delivery, "files": []}, sort_keys=True,
            ),
            "clean_results": json.dumps(
                {"delivery": delivery, "raw_results_artifact_id": raw_id},
                sort_keys=True,
            ),
            "experiment_log": (
                "# Experiment delivery" + chr(10) * 2
                + "Failed honestly." + chr(10)
            ),
        },
    )


def _ledger_bytes(state):
    path = ledger.ledger_dir(state) / "events.jsonl"
    return path.read_bytes() if path.exists() else b""


@pytest.mark.parametrize(
    ("artifact_type", "metadata"),
    [
        ("raw_results", {"scope": "operation"}),
        ("clean_results", {"scope": "scientific"}),
        ("experiment_log", {}),
    ],
)
def test_generic_freeze_rejects_each_reserved_type_scope_neutrally(
    tmp_path, artifact_type, metadata,
):
    state, _led = _state(tmp_path)
    saved = state.save_artifact(
        artifact_type,
        f"manual_{artifact_type}",
        "not a delivery transaction",
        metadata=metadata,
    )

    result = asyncio.run(
        _freeze_artifact(state, saved["id"], "generic freeze"),
    )

    assert result["status"] == "error"
    assert result["failed_checks"] == [
        contract_audit._V3_RESERVED_FREEZE_CHECK,
    ]
    assert "finish delivery transaction" in result["reasons"][
        contract_audit._V3_RESERVED_FREEZE_CHECK
    ]
    assert state.artifact_head(saved["id"]).frozen is False


def test_private_transaction_freezes_trio_and_delivery_audit_is_read_only(tmp_path):
    state, led = _state(tmp_path)
    sealed = tx.settle_delivery(state, led, _manifest(state))
    manifest_before = tx.manifest_path(state).read_bytes()
    ledger_before = _ledger_bytes(state)

    audit = contract_audit.audit_experiment_delivery(state)

    assert audit["passed"] is True
    assert audit["status"] == "sealed"
    assert audit["artifact_ids"] == sealed["artifact_ids"]
    assert audit["artifact_identities"] == sealed["artifact_identities"]
    assert contract_audit.audit_experiment_contract(state)[
        "experiment_delivery"
    ] == audit
    assert tx.manifest_path(state).read_bytes() == manifest_before
    assert _ledger_bytes(state) == ledger_before


def test_delivery_audit_does_not_settle_a_missing_manifest(tmp_path, monkeypatch):
    state, _led = _state(tmp_path)
    monkeypatch.setattr(
        tx,
        "settle_delivery",
        lambda *_args, **_kwargs: pytest.fail(
            "audit must never settle delivery",
        ),
    )

    audit = contract_audit.audit_experiment_delivery(state)

    assert audit["passed"] is False
    assert audit["error_code"] == "closure_manifest_missing"
    assert tx.read_manifest(state) is None
    assert not [
        row for row in ledger.open_ledger(state).records()
        if row.get("kind") == "finish"
    ]
    assert all(not state.list_artifacts(kind) for kind in tx.TRIO)


def test_delivery_audit_rejects_exact_frozen_identity_drift_without_repair(
    tmp_path,
):
    state, led = _state(tmp_path)
    sealed = tx.settle_delivery(state, led, _manifest(state))
    manifest = tx.read_manifest(state)
    raw_id = sealed["artifact_ids"]["raw_results"]
    state.mark_frozen(
        raw_id,
        metadata_patch=dict(
            manifest["artifacts"]["raw_results"]["metadata"],
        ),
    )
    manifest_before = tx.manifest_path(state).read_bytes()
    ledger_before = _ledger_bytes(state)

    audit = contract_audit.audit_experiment_delivery(state)

    assert audit["passed"] is False
    assert audit["error_code"] == "closure_head_mismatch"
    assert "frozen_identity" in audit["facts"]["mismatches"]
    assert tx.manifest_path(state).read_bytes() == manifest_before
    assert _ledger_bytes(state) == ledger_before


def test_delivery_audit_rejects_manifest_digest_drift_without_repair(tmp_path):
    state, led = _state(tmp_path)
    tx.settle_delivery(state, led, _manifest(state))
    manifest = tx.read_manifest(state)
    manifest["delivery"]["summary"] = "drifted after commit"
    tx.manifest_path(state).write_text(
        json.dumps(
            manifest, ensure_ascii=False, indent=2, sort_keys=True,
        ) + chr(10),
        encoding="utf-8",
    )
    manifest_before = tx.manifest_path(state).read_bytes()
    ledger_before = _ledger_bytes(state)

    audit = contract_audit.audit_experiment_delivery(state)

    assert audit["passed"] is False
    assert audit["error_code"] == "closure_manifest_delivery_digest_mismatch"
    assert tx.manifest_path(state).read_bytes() == manifest_before
    assert _ledger_bytes(state) == ledger_before


def test_delivery_audit_rejects_a_second_same_closure_finish_without_repair(
    tmp_path,
):
    state, led = _state(tmp_path)
    tx.settle_delivery(state, led, _manifest(state))
    manifest = tx.read_manifest(state)
    led.append(
        "finish",
        idempotency_key="drifted-finish",
        closure_id=manifest["closure_id"],
        prepared_digest="drifted",
        delivery_digest="drifted",
        artifact_ids={},
        artifact_identities={},
        outcome="failed",
    )
    manifest_before = tx.manifest_path(state).read_bytes()
    ledger_before = _ledger_bytes(state)

    audit = contract_audit.audit_experiment_delivery(state)

    assert audit["passed"] is False
    assert audit["error_code"] == "closure_finish_commit_invalid"
    assert tx.manifest_path(state).read_bytes() == manifest_before
    assert _ledger_bytes(state) == ledger_before
    assert len([
        row for row in ledger.open_ledger(state).records()
        if row.get("kind") == "finish"
        and row.get("closure_id") == manifest["closure_id"]
    ]) == 2

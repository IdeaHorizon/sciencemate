"""Experiment v3 completion admission through the actual Core finalizer."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from core.agent_loop import LoopResult
from core.executor import finalize_run
from core.loader import load_harness
from core.project_bootstrap import ensure_project_worktree
from core.state import State
from nodes.experiment.tools import contract_audit
from nodes.experiment.tools import delivery_transaction as tx
from nodes.experiment.tools import ledger


def _state(
    tmp_path: Path,
    *,
    project_worktree: Path | None = None,
) -> tuple[State, ledger.Ledger]:
    state = State.new(
        node_type="experiment",
        base_dir=tmp_path / "runs",
        project_id="p",
        session_id="owner",
        project_worktree=project_worktree,
    )
    led = ledger.open_ledger(state, write=True, bootstrap=True)
    ledger.mark_bootstrap_ready(state)
    assert ledger.v3_active(state)
    return state, led


def _manifest(state: State, *, outcome: str = "success") -> dict:
    delivery = {
        "schema": tx.DELIVERY_SCHEMA,
        "delivery_id": tx.delivery_id(state),
        "delivery_owner": "finish",
        "scope": "operation",
        "scope_status": "resolved",
        "outcome": outcome,
        "execution_status": "attempted",
        "result_status": "delivered" if outcome == "success" else "unavailable",
        "summary": f"operation ended {outcome}",
        "gaps": [],
        "contradictions": [],
        "formal_input": {"status": "none", "binding": None, "error": None},
        "limitations": [],
        "external_job_refs": [],
        "next_step": None,
        "blocker": {"blocker_id": "missing-input", "summary": "upstream unavailable"}
        if outcome == "blocked" else None,
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
                "# Experiment delivery\n\n"
                f"Operation ended {outcome}.\n"
            ),
        },
    )


def _finalize(state: State) -> dict:
    result = LoopResult(
        final_text="model stopped",
        turns=1,
        tool_calls=[],
        messages=[],
        status="completed",
        terminal_cause="model_finished",
    )
    from core.bootstrap import bootstrap
    bootstrap()
    return asyncio.run(
        finalize_run(state, load_harness("experiment"), result, llm=None),
    )


def _assert_core_did_not_complete(state: State, summary: dict, code: str) -> None:
    assert summary["status"] == "suspended", summary
    assert summary["completion_admission"]["code"] == code
    assert not (state.root / "summary.json").exists()
    records = [json.loads(line) for line in state.transcript_path.read_text().splitlines()]
    assert not any(row["event"] == "run_end" for row in records)


def test_exact_sealed_success_is_a_positive_completion_control(tmp_path: Path) -> None:
    state, led = _state(tmp_path)
    tx.settle_delivery(state, led, _manifest(state, outcome="success"))

    audit = contract_audit.audit_experiment_delivery(state)
    summary = _finalize(state)

    assert audit["passed"] is True, audit
    assert audit["outcome"] == "success"
    assert summary["status"] == "completed", summary


def test_frozen_trio_without_finish_commit_must_not_complete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, led = _state(tmp_path)
    real_append_once = led.append_once

    def interrupt_finish(kind: str, key: str, **payload):
        if kind == "finish":
            raise OSError("injected post-freeze pre-finish interruption")
        return real_append_once(kind, key, **payload)

    monkeypatch.setattr(led, "append_once", interrupt_finish)
    with pytest.raises(tx.DeliveryTransactionError, match="post-freeze pre-finish") as caught:
        tx.settle_delivery(state, led, _manifest(state, outcome="failed"))
    assert caught.value.code == "closure_ledger_persist_failed"
    assert caught.value.facts["checkpoint"] == "ledger_commit"
    assert caught.value.facts["commit_visibility"] == "unknown"
    assert caught.value.facts["frozen_artifacts_preserved"] is True
    assert isinstance(caught.value.__cause__, OSError)

    audit = contract_audit.audit_experiment_delivery(state)
    assert audit["passed"] is False, audit
    assert audit["error_code"] == "closure_manifest_not_committed"
    assert set(item["type"] for item in state.list_artifacts()) == set(tx.TRIO)
    assert all(
        state.artifact_head(item["id"]).frozen
        for item in state.list_artifacts()
    )

    _assert_core_did_not_complete(state, _finalize(state), audit["error_code"])


def test_drifted_frozen_delivery_head_must_not_complete(tmp_path: Path) -> None:
    state, led = _state(tmp_path)
    sealed = tx.settle_delivery(
        state, led, _manifest(state, outcome="failed"),
    )
    manifest = tx.read_manifest(state)
    assert manifest is not None
    raw_id = sealed["artifact_ids"]["raw_results"]
    state.mark_frozen(
        raw_id,
        metadata_patch=dict(
            manifest["artifacts"]["raw_results"]["metadata"],
        ),
    )

    audit = contract_audit.audit_experiment_delivery(state)
    assert audit["passed"] is False, audit
    assert audit["error_code"] == "closure_head_mismatch"
    assert audit["facts"]["artifact_type"] == "raw_results"

    _assert_core_did_not_complete(state, _finalize(state), audit["error_code"])


def test_project_workspace_change_without_delivery_must_not_complete(
    tmp_path: Path,
) -> None:
    project = ensure_project_worktree("core-completion-handoff")
    assert project is not None
    state, _led = _state(tmp_path, project_worktree=project)
    (Path(state.workspace_root) / "note.txt").write_text(
        "work exists, but ExperimentDeliveryV1 does not\n",
        encoding="utf-8",
    )

    audit = contract_audit.audit_experiment_delivery(state)
    assert audit["passed"] is False, audit
    assert audit["error_code"] == "closure_manifest_missing"
    assert state.list_artifacts(own_only=True) == []

    summary = _finalize(state)
    assert summary["missing_required_outputs"] == []
    assert summary["delivery_empty"] is False
    _assert_core_did_not_complete(state, summary, audit["error_code"])


@pytest.mark.parametrize("outcome", ["failed", "partial", "blocked", "cancelled"])
def test_complete_delivery_does_not_claim_the_task_succeeded(tmp_path: Path, outcome: str) -> None:
    state, led = _state(tmp_path)
    tx.settle_delivery(state, led, _manifest(state, outcome=outcome))
    audit = contract_audit.audit_experiment_delivery(state)
    assert audit["passed"], audit
    summary = _finalize(state)
    assert summary["status"] == ("blocked" if outcome == "blocked" else "incomplete")
    assert summary["node_task_outcome"]["outcome"] == outcome
    assert summary["completion_admission"]["disposition"] == "allow"

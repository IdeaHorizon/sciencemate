"""A new delivery claim versions C; interrupted C is recovered, not replaced.

These exercise the real Core artifact store and the node transaction/audit.
Only persistence fault injection is mocked; no product implementation is patched.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from core import sandbox
from core.project_workspace import working_directory
from core.state import State
from nodes.experiment.tools import contract_audit
from nodes.experiment.tools import delivery_transaction as tx
from nodes.experiment.tools import finish, ledger, run_bootstrap, safe_bash


def _state(tmp_path):
    state = State.new(
        node_type="experiment", base_dir=tmp_path / "runs",
        project_id="revision-project", session_id="revision-owner",
    )
    led = ledger.open_ledger(state, write=True, bootstrap=True)
    ledger.mark_bootstrap_ready(state)
    return state, led


def _manifest(state, *, summary="initial delivery", previous=None):
    revision = 1 if previous is None else int(previous.get("revision", 1)) + 1
    identity = (tx.delivery_id(state) if previous is None
                else tx.delivery_id(state, revision=revision))
    delivery = {
        "schema": tx.DELIVERY_SCHEMA,
        "delivery_id": identity,
        "delivery_owner": "finish",
        "scope": "operation",
        "scope_status": "resolved",
        "outcome": "failed",
        "execution_status": "attempted",
        "result_status": "unavailable",
        "summary": summary,
        "gaps": [],
        "contradictions": [],
        "formal_input": {"status": "none", "binding": None, "error": None},
        "limitations": [],
        "external_job_refs": [],
        "next_step": None,
        "blocker": None,
    }
    names = tx.deterministic_names(state)
    ids = {kind: tx.artifact_id(kind, names[kind]) for kind in tx.TRIO}
    contents = {
        "raw_results": json.dumps({"delivery": delivery, "files": []}, sort_keys=True),
        "clean_results": json.dumps({
            "delivery": delivery,
            "raw_results_artifact_id": ids["raw_results"],
        }, sort_keys=True),
        "experiment_log": f"# Experiment delivery\n\n{summary}\n",
    }
    kwargs = {"previous": previous} if previous is not None else {}
    return tx.build_manifest(state, delivery=delivery, contents=contents, **kwargs)


def _first(state, led):
    result = tx.settle_delivery(state, led, _manifest(state))
    assert result["status"] == "success"
    return tx.read_manifest(state)


def _finish_rows(led):
    return [row for row in led.records() if row.get("kind") == "finish"]


def _assert_two_versions(state, led, current):
    assert current["revision"] == 2
    for kind, spec in current["artifacts"].items():
        versions = state.artifact_versions(spec["id"])
        assert [record["version"] for record in versions] == [1, 2], kind
        head = state.artifact_head(spec["id"])
        assert head.frozen and head.version == head.frozen_version == 2, kind
    rows = _finish_rows(led)
    assert len(rows) == 2
    assert len({row["closure_id"] for row in rows}) == 2


def test_revision_keeps_artifact_identity_and_archives_complete_old_claim(tmp_path):
    state, led = _state(tmp_path)
    old = _first(state, led)
    proposed = _manifest(state, summary="corrected delivery", previous=old)
    assert proposed["revision"] == 2
    pointer = proposed["supersedes"]
    assert (pointer if isinstance(pointer, str) else pointer.get("closure_id")) == old["closure_id"]

    second = tx.settle_delivery(state, led, proposed, previous=old)
    current = tx.read_manifest(state)
    assert second["status"] == "success"
    assert current["closure_id"] != old["closure_id"]
    assert current["delivery"]["summary"] == "corrected delivery"
    for kind in tx.TRIO:
        assert current["artifacts"][kind]["id"] == old["artifacts"][kind]["id"]
        assert current["artifacts"][kind]["name"] == old["artifacts"][kind]["name"]
    archive = json.loads(tx.history_manifest_path(state, 1).read_text(encoding="utf-8"))
    assert archive == old
    for kind in tx.TRIO:
        assert archive["artifacts"][kind]["content"] == old["artifacts"][kind]["content"]
    _assert_two_versions(state, led, current)
    audit = contract_audit.audit_experiment_delivery(state)
    assert audit["passed"] is True, audit
    assert audit["closure_id"] == current["closure_id"]
    assert audit["finish_seq"] == second["finish_seq"]


def test_revision_save_then_crash_recovers_v2_without_creating_v3(tmp_path, monkeypatch):
    state, led = _state(tmp_path)
    old = _first(state, led)
    proposed = _manifest(state, summary="save recovery", previous=old)
    real_save = state.save_artifact

    def save_then_crash(*args, **kwargs):
        result = real_save(*args, **kwargs)
        if args[0] == "raw_results" and result["version"] == 2:
            raise OSError("injected after revision save")
        return result

    monkeypatch.setattr(state, "save_artifact", save_then_crash)
    with pytest.raises(OSError, match="injected after revision save"):
        tx.settle_delivery(state, led, proposed, previous=old)
    assert tx.read_manifest(state)["revision"] == 2
    assert contract_audit.audit_experiment_delivery(state)["passed"] is False
    assert len(_finish_rows(led)) == 1

    monkeypatch.setattr(state, "save_artifact", real_save)
    recovered = tx.settle_delivery(state, led, None)
    assert recovered["status"] == "success"
    assert recovered["delivery"]["summary"] == "save recovery"
    _assert_two_versions(state, led, tx.read_manifest(state))
    assert contract_audit.audit_experiment_delivery(state)["passed"] is True


def test_revision_partial_freeze_is_not_sealed_and_recovery_keeps_same_claim(tmp_path, monkeypatch):
    state, led = _state(tmp_path)
    old = _first(state, led)
    proposed = _manifest(state, summary="freeze recovery", previous=old)
    raw_id = old["artifacts"]["raw_results"]["id"]
    real_freeze = state.mark_frozen

    def freeze_then_crash(artifact_id, *args, **kwargs):
        result = real_freeze(artifact_id, *args, **kwargs)
        if artifact_id == raw_id and state.artifact_head(artifact_id).version == 2:
            raise OSError("injected after revision freeze")
        return result

    monkeypatch.setattr(state, "mark_frozen", freeze_then_crash)
    with pytest.raises(OSError, match="injected after revision freeze"):
        tx.settle_delivery(state, led, proposed, previous=old)
    interrupted = tx.read_manifest(state)
    assert interrupted["revision"] == 2 and interrupted["commit"] is None
    assert contract_audit.audit_experiment_delivery(state)["passed"] is False
    assert len(_finish_rows(led)) == 1

    monkeypatch.setattr(state, "mark_frozen", real_freeze)
    recovered = tx.settle_delivery(state, led, None)
    assert recovered["status"] == "success"
    assert recovered["closure_id"] == interrupted["closure_id"]
    assert recovered["delivery"]["summary"] == "freeze recovery"
    _assert_two_versions(state, led, tx.read_manifest(state))
    assert contract_audit.audit_experiment_delivery(state)["passed"] is True


def test_stale_predecessor_cannot_replace_a_different_committed_revision(tmp_path):
    state, led = _state(tmp_path)
    old = _first(state, led)
    winning = _manifest(state, summary="accepted correction", previous=old)
    stale = _manifest(state, summary="competing correction", previous=old)
    tx.settle_delivery(state, led, winning, previous=old)
    before = tx.manifest_path(state).read_bytes()

    try:
        result = tx.settle_delivery(state, led, stale, previous=old)
    except tx.DeliveryTransactionError as exc:
        assert exc.code
    else:
        assert result["status"] == "error", result
        assert result.get("error_code"), result
    assert tx.manifest_path(state).read_bytes() == before
    assert tx.read_manifest(state)["delivery"]["summary"] == "accepted correction"
    _assert_two_versions(state, led, tx.read_manifest(state))
    assert contract_audit.audit_experiment_delivery(state)["passed"] is True


def test_plain_replay_does_not_create_an_artifact_or_delivery_revision(tmp_path):
    state, led = _state(tmp_path)
    old = _first(state, led)
    before = tx.manifest_path(state).read_bytes()
    result = tx.settle_delivery(state, led, None)
    assert result["status"] == "success" and result["reused_manifest"] is True
    assert result["closure_id"] == old["closure_id"]
    assert tx.manifest_path(state).read_bytes() == before
    assert len(_finish_rows(led)) == 1
    assert all(len(state.artifact_versions(spec["id"])) == 1
               for spec in old["artifacts"].values())
    assert contract_audit.audit_experiment_delivery(state)["passed"] is True


@pytest.mark.parametrize("selection", ["glob", "directory"])
def test_same_request_cannot_replay_success_after_selected_member_disappears(tmp_path, selection):
    """An external writer changes a known object, not an unobserved shell dependency."""
    task = "Generate and deliver the result files."
    with asyncio.Runner() as runner:
        state = State.new("experiment", tmp_path / "runs", session_id="selected-object-replay")
        state.hook_state["node_inputs"] = {"experiment_spec": task}
        established = runner.run(run_bootstrap.establish(state))
        assert ledger.bootstrap_phase(state) == "ready", established
        bundle = working_directory(state) / "runtime" / "bundle"
        target = bundle / "result.txt"
        try:
            written = runner.run(safe_bash._safe_write_file(
                state, path=str(target), content="observed result\n",
            ))
            assert written.get("status") == "success", written
            request = {
                "outcome": "success", "summary": "The required results are ready.",
                "verification": [{"requirement": task, "checks": [{
                    "kind": "glob", "pattern": str(bundle / "*.txt"), "min_count": 1,
                }]}],
            }
            if selection == "directory":
                request["deliverables"] = [{"path": str(bundle)}]
            first = runner.run(finish._finish(state, **request))
            assert first.get("status") == "success" and first.get("outcome") == "success", first
            before = tx.manifest_path(state).read_bytes()
            # Deliberate test-owned external mutation: no forged producer/event,
            # and no claim that a readonly check escaped its boundary.
            target.unlink()

            again = runner.run(finish._finish(state, **request))

            assert again.get("status") == "success_unsupported", again
            assert tx.manifest_path(state).read_bytes() == before
            assert len(_finish_rows(ledger.open_ledger(state))) == 1
        finally:
            if state.sandbox_manifest:
                sandbox.evict_state_attempt(state)


def test_third_revision_retains_history_and_cannot_hide_a_corrupt_predecessor(tmp_path):
    state, led = _state(tmp_path)
    first = _first(state, led)
    second_plan = _manifest(state, summary="second", previous=first)
    tx.settle_delivery(state, led, second_plan, previous=first)
    second = tx.read_manifest(state)
    third_plan = _manifest(state, summary="third", previous=second)
    tx.settle_delivery(state, led, third_plan, previous=second)
    third = tx.read_manifest(state)
    assert third["revision"] == 3
    assert contract_audit.audit_experiment_delivery(state)["passed"] is True
    assert json.loads(tx.history_manifest_path(state, 1).read_text()) == first
    archive = tx.history_manifest_path(state, 2)
    assert json.loads(archive.read_text()) == second
    assert all([record["version"] for record in state.artifact_versions(spec["id"])] == [1, 2, 3]
               for spec in third["artifacts"].values())

    next_plan = _manifest(state, summary="fourth", previous=third)
    before = tx.manifest_path(state).read_bytes()
    damaged = json.loads(archive.read_text())
    damaged["artifacts"]["raw_results"]["content"] += "\ncorrupted history"
    archive.write_text(json.dumps(damaged), encoding="utf-8")
    with pytest.raises(tx.DeliveryTransactionError):
        tx.settle_delivery(state, led, next_plan, previous=third)
    assert tx.manifest_path(state).read_bytes() == before
    assert len(_finish_rows(led)) == 3
    assert all(state.artifact_head(spec["id"]).version == 3
               for spec in third["artifacts"].values())

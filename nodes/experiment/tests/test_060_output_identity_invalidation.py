"""A witnessed output discontinuity cannot be repaired by replaying old stat fields.

The fixture writes real small files and uses the real action capture, canonical
B ledger, finish evaluation and closure. No OS permission/ACL changes or model
are involved. An out-of-band write represents an execution boundary that failed
to enforce a requested readonly check (Core #1334).
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from core.state import State
from nodes.experiment.tools import (
    action_boundary, capture, contract_audit, delivery_transaction, finish, ledger, run_bootstrap,
)


def _run(awaitable):
    return asyncio.run(awaitable)


def _state(tmp_path: Path):
    state = State.new("experiment", tmp_path / "runs", session_id="output-invalidation")
    target = state.root / "outputs/experiment/runtime/result.txt"
    state.hook_state["node_inputs"] = {
        "experiment_spec": "Produce result.txt and preserve its actual source.",
        "required_outputs": [{"id": "result", "kind": "file", "path": str(target)}],
    }
    established = _run(run_bootstrap.establish(state))
    assert ledger.bootstrap_phase(state) == "ready", established
    return state, target


def _produce(state, target, content=b"49\n", *, replace=False):
    handle = action_boundary.begin(
        state, tool="safe_run_bash", effect_class=action_boundary.EXPERIMENT,
        command="fixture producer", work_root=str(target.parent), output_candidates=[target],
    )
    assert isinstance(handle, action_boundary.ActionHandle), handle
    target.parent.mkdir(parents=True, exist_ok=True)
    if replace:
        temporary = target.with_suffix(".next")
        temporary.write_bytes(content)
        temporary.replace(target)
    else:
        target.write_bytes(content)
    settled = action_boundary.settle(state, handle, outcome="settled", exit_code=0)
    assert settled["status"] == "recorded", settled
    return handle.action_id


def _unobserved_rewrite(target):
    original = capture.identity(target)
    target.write_bytes(target.read_bytes())
    stat = target.stat()
    os.utime(target, ns=(stat.st_atime_ns, original["mtime_ns"] + 1_000_000_000))
    assert capture.identity(target)["sha256"] == original["sha256"]
    assert not capture.same_identity(original, capture.identity(target))[0]
    return original


def _success(state):
    return _run(finish._finish(state, outcome="success", summary="The required output is ready."))


def _invalidations(state):
    return [r for r in ledger.open_ledger(state).records()
            if r.get("kind") == "output_identity_invalidated"]


@pytest.mark.parametrize("recorded_modifier", [False, True])
def test_observed_identity_loss_survives_physical_timestamp_rollback(tmp_path, recorded_modifier):
    state, target = _state(tmp_path)
    producer = _produce(state, target)
    original = _unobserved_rewrite(target)
    unsupported = _success(state)
    assert unsupported["status"] == "success_unsupported", unsupported
    assert any(g.get("gap") == "identity_changed_since_the_action" for g in unsupported["gaps"])

    # This restores every stat/hash field the old gate compared, but it cannot
    # undo the observed break in provenance. No new output was produced.
    handle = None
    if recorded_modifier:
        handle = action_boundary.begin(
            state, tool="safe_run_bash", effect_class=action_boundary.EXPERIMENT,
            command="fixture recorded metadata restoration", output_candidates=[target],
        )
    os.utime(target, ns=(original["mtime_ns"], original["mtime_ns"]))
    if handle is not None:
        assert action_boundary.settle(state, handle, outcome="settled", exit_code=0)["status"] == "recorded"
    assert capture.same_identity(original, capture.identity(target))[0]
    retried = _success(state)
    assert retried["status"] == "success_unsupported", retried
    assert delivery_transaction.read_manifest(state) is None
    events = ledger.open_ledger(state).records()
    invalidated = [r for r in events if r.get("kind") == "output_identity_invalidated"]
    assert len(invalidated) == 1
    assert invalidated[0]["producer_action_id"] == producer


def test_recorded_modifier_before_snapshot_exposes_discontinuity_without_prior_finish(tmp_path):
    state, target = _state(tmp_path)
    producer = _produce(state, target)
    original = _unobserved_rewrite(target)
    modifier = action_boundary.begin(
        state, tool="safe_run_bash", effect_class=action_boundary.EXPERIMENT,
        command="fixture recorded metadata restoration", output_candidates=[target],
    )
    os.utime(target, ns=(original["mtime_ns"], original["mtime_ns"]))
    assert action_boundary.settle(state, modifier, outcome="settled", exit_code=0)["status"] == "recorded"
    # The modifier's actual before snapshot already proves a broken chain.
    # This is not the undetectable case where no observation saw a change.
    assert capture.same_identity(original, capture.identity(target))[0]
    result = _success(state)
    assert result["status"] == "success_unsupported", result
    assert _invalidations(state)[0]["producer_action_id"] == producer


def test_new_physical_producer_can_recover_only_the_invalidated_object(tmp_path):
    state, target = _state(tmp_path)
    old = _produce(state, target)
    untouched = target.with_name("unrelated.txt")
    other = _produce(state, untouched, b"stable\n")
    _unobserved_rewrite(target)
    assert _success(state)["status"] == "success_unsupported"
    unaffected = finish._file_evidence(state, ledger.open_ledger(state).records(), {"path": str(untouched)})
    assert unaffected["passed"] is True
    assert unaffected["evidence"]["producers"] == [other]
    replacement = _produce(state, target, replace=True)
    assert replacement != old
    recovered = _success(state)
    assert recovered["status"] == "success", recovered
    assert recovered["outcome"] == "success"
    evidence = finish._file_evidence(state, ledger.open_ledger(state).records(), {"path": str(target)})
    assert evidence["passed"] is True
    assert evidence["evidence"]["producers"] == [replacement]


def test_contiguous_same_content_modifier_still_retains_auditable_lineage(tmp_path):
    state, target = _state(tmp_path)
    original = _produce(state, target)
    old_ns = target.stat().st_mtime_ns
    handle = action_boundary.begin(
        state, tool="safe_run_bash", effect_class=action_boundary.EXPERIMENT,
        command="same-content fixture recomputation", output_candidates=[target],
    )
    target.write_bytes(b"49\n")
    os.utime(target, ns=(old_ns + 1_000_000_000, old_ns + 1_000_000_000))
    assert action_boundary.settle(state, handle, outcome="settled", exit_code=0)["status"] == "recorded"
    evidence = finish._file_evidence(state, ledger.open_ledger(state).records(), {"path": str(target)})
    assert evidence["passed"] is True
    assert evidence["evidence"]["producers"] == [original]
    assert evidence["evidence"]["identity_adjustments"] == [handle.action_id]
    assert _success(state)["status"] == "success"


def test_fresh_state_and_repeated_finish_rebuild_the_same_invalidated_generation(tmp_path):
    state, target = _state(tmp_path)
    _produce(state, target)
    original = _unobserved_rewrite(target)
    assert _success(state)["status"] == "success_unsupported"
    first = _invalidations(state)
    assert len(first) == 1
    assert _success(state)["status"] == "success_unsupported"
    assert _invalidations(state) == first
    os.utime(target, ns=(original["mtime_ns"], original["mtime_ns"]))
    reopened = State.reopen("experiment", state.root.parent, state.run_id,
                            session_id="output-invalidation")
    reopened.hook_state["node_inputs"] = dict(state.hook_state["node_inputs"])
    assert not reopened.hook_state.get("output_identity_invalidated")
    assert _success(reopened)["status"] == "success_unsupported"
    assert _invalidations(reopened) == first


def test_different_run_cannot_inherit_another_runs_invalidation(tmp_path):
    first, target = _state(tmp_path / "first")
    _produce(first, target)
    _unobserved_rewrite(target)
    assert _success(first)["status"] == "success_unsupported"
    second, independent = _state(tmp_path / "second")
    _produce(second, independent)
    assert _success(second)["status"] == "success"
    assert _invalidations(second) == []


@pytest.mark.parametrize("outcome", ["failed", "blocked", "partial", "cancelled"])
def test_invalidated_output_does_not_prevent_an_honest_terminal_delivery(tmp_path, outcome):
    state, target = _state(tmp_path)
    _produce(state, target)
    _unobserved_rewrite(target)
    assert _success(state)["status"] == "success_unsupported"
    terminal = _run(finish._finish(
        state, outcome=outcome, summary="Output source is invalidated; no success is claimed.",
        **({"blocker": {"summary": "An outside write left output provenance unavailable."}}
           if outcome == "blocked" else {}),
        next_step="Retain evidence and establish a new recorded generation if needed.",
    ))
    assert terminal["status"] == "success" and terminal["outcome"] == outcome, terminal
    assert _invalidations(state)


@pytest.mark.parametrize("effect_class", [action_boundary.EXPERIMENT, action_boundary.SUPPORT])
def test_early_finish_does_not_invalidate_an_unsettled_owned_modifier(tmp_path, effect_class):
    state, target = _state(tmp_path)
    _produce(state, target)
    before = target.stat()
    modifier = action_boundary.begin(
        state, tool="safe_run_bash", effect_class=effect_class,
        command="fixture in-flight same-content writer", output_candidates=[target],
    )
    target.write_bytes(b"49\n")
    os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000_000))
    early = _success(state)
    assert early["status"] in {"live_external_effect", "success_unsupported"}, early
    assert _invalidations(state) == []
    assert action_boundary.settle(state, modifier, outcome="settled", exit_code=0)["status"] == "recorded"
    assert _success(state)["status"] == "success"


def test_temporary_hash_unavailability_does_not_invalidate_a_producer(tmp_path, monkeypatch):
    state, target = _state(tmp_path)
    _produce(state, target)
    identity = capture.identity

    def temporarily_unreadable(path, **kwargs):
        result = identity(path, **kwargs)
        if Path(path) == target:
            result.pop("sha256", None)
            result["hash_error"] = "fixture transient read failure"
        return result

    with monkeypatch.context() as patch:
        patch.setattr(capture, "identity", temporarily_unreadable)
        assert _success(state)["status"] == "success_unsupported"
        assert _invalidations(state) == []
    assert _success(state)["status"] == "success"


def test_invalidation_append_failure_does_not_report_success(tmp_path, monkeypatch):
    state, target = _state(tmp_path)
    _produce(state, target)
    _unobserved_rewrite(target)
    original_append = ledger.Ledger.append_once

    def unavailable(self, kind, *args, **kwargs):
        if kind == "output_identity_invalidated":
            raise OSError("fixture ledger append unavailable")
        return original_append(self, kind, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(ledger.Ledger, "append_once", unavailable)
        failed = _success(state)
        assert failed["status"] == "error", failed
        assert failed["error_code"] == "output_identity_observation_not_durable"
        assert delivery_transaction.read_manifest(state) is None
    # Retry is a new observation, not an acknowledgement of the lost one.
    assert _success(state)["status"] == "success_unsupported"
    assert len(_invalidations(state)) == 1


def test_visible_invalidation_is_reconfirmed_after_durability_failure(tmp_path, monkeypatch):
    state, target = _state(tmp_path)
    _produce(state, target)
    original = _unobserved_rewrite(target)

    def unavailable(*args, **kwargs):
        raise OSError("fixture parent fsync unavailable")

    append = ledger.Ledger._append_unlocked

    def visible_without_parent_ack(self, kind, fields, **kwargs):
        if kind != "output_identity_invalidated":
            return append(self, kind, fields, **kwargs)
        append(self, kind, fields, strict_parent_fsync=False)
        raise OSError("fixture append became visible before parent durability acknowledgement")

    with monkeypatch.context() as patch:
        patch.setattr(ledger.Ledger, "_append_unlocked", visible_without_parent_ack)
        patch.setattr(ledger, "_fsync_file_and_parent", unavailable)
        first = _success(state)
        assert first["status"] == "error", first
        assert len(_invalidations(state)) == 1  # visible is not yet durable acknowledgement
        os.utime(target, ns=(original["mtime_ns"], original["mtime_ns"]))
        reopened = State.reopen("experiment", state.root.parent, state.run_id,
                                session_id="output-invalidation")
        reopened.hook_state["node_inputs"] = dict(state.hook_state["node_inputs"])
        again = _success(reopened)
        assert again["status"] == "error", again
        assert delivery_transaction.read_manifest(state) is None
    assert _success(state)["status"] == "success_unsupported"
    assert len(_invalidations(state)) == 1


def test_first_finish_witnesses_a_check_rewrite_before_metadata_is_restored(tmp_path, monkeypatch):
    state, target = _state(tmp_path)
    _produce(state, target)
    original = capture.identity(target)
    calls = []

    async def violating_readonly_check(actual_state, obligation):
        # The node's OS enforcement is owned by Core. This injected faulty
        # executor preserves the E01 variable: a supposed readonly check
        # physically rewrites the actual business output, with equal bytes.
        if not calls:
            _unobserved_rewrite(target)
        calls.append(obligation["command"])
        return {"exit_code": 0, "executed": True, "stdout_tail": "CHECK_OK", "stderr_tail": ""}

    monkeypatch.setattr(finish, "_check_runner", violating_readonly_check)
    methods = [{"requirement": "Verify the delivered output.", "checks": [
        {"kind": "check", "command": "fixture check", "expect_stdout_contains": "CHECK_OK"},
    ]}]
    first = _run(finish._finish(state, outcome="success", summary="Checked.", verification=methods))
    assert first["status"] == "success_unsupported", first
    assert _invalidations(state)
    os.utime(target, ns=(original["mtime_ns"], original["mtime_ns"]))
    retry = _run(finish._finish(state, outcome="success", summary="Checked.", verification=methods))
    assert retry["status"] == "success_unsupported", retry
    assert len(calls) == 2


@pytest.mark.parametrize("prior_finish", [False, True])
def test_sealed_success_cannot_be_replayed_after_its_generation_is_invalidated(tmp_path, prior_finish):
    state, target = _state(tmp_path)
    _produce(state, target)
    first = _success(state)
    assert first["status"] == "success"
    historical = delivery_transaction.read_manifest(state)
    original = _unobserved_rewrite(target)
    if prior_finish:
        assert _success(state)["status"] == "success_unsupported"
    modifier = action_boundary.begin(
        state, tool="safe_run_bash", effect_class=action_boundary.EXPERIMENT,
        command="fixture metadata rollback", output_candidates=[target],
    )
    os.utime(target, ns=(original["mtime_ns"], original["mtime_ns"]))
    assert action_boundary.settle(state, modifier, outcome="settled", exit_code=0)["status"] == "recorded"
    assert _success(state)["status"] == "success_unsupported"
    assert delivery_transaction.read_manifest(state) == historical
    audit = contract_audit.audit_experiment_delivery(state)
    assert audit["passed"] is True and audit["status"] == "sealed", audit
    assert audit["current_output_source"]["status"] == "invalidated"
    assert audit["current_output_source"]["findings"][0]["code"] == "delivery_source_invalidated"
    _produce(state, target, replace=True)
    recovered = _success(state)
    assert recovered["status"] == "success" and recovered["revision"] == 2, recovered
    recovered_audit = contract_audit.audit_experiment_delivery(state)
    assert recovered_audit["passed"] is True
    assert recovered_audit["current_output_source"]["status"] == "no_recorded_invalidation"
    assert _success(state)["reused_manifest"] is True


@pytest.mark.parametrize("replace,content", [(True, b"49\n"), (False, b"50\n")])
def test_broken_old_modifier_does_not_invalidate_a_later_true_producer(tmp_path, replace, content):
    state, target = _state(tmp_path)
    old = _produce(state, target)
    original = _unobserved_rewrite(target)
    modifier = action_boundary.begin(
        state, tool="safe_run_bash", effect_class=action_boundary.EXPERIMENT,
        command="fixture metadata rollback", output_candidates=[target],
    )
    os.utime(target, ns=(original["mtime_ns"], original["mtime_ns"]))
    assert action_boundary.settle(state, modifier, outcome="settled", exit_code=0)["status"] == "recorded"
    replacement = _produce(state, target, content=content, replace=replace)
    result = _success(state)
    assert result["status"] == "success", result
    assert target.read_bytes() == content
    assert _invalidations(state)[0]["producer_action_id"] == old
    evidence = finish._file_evidence(state, ledger.open_ledger(state).records(), {"path": str(target)})
    assert evidence["evidence"]["producers"] == [replacement]
    assert contract_audit.audit_experiment_delivery(state)["passed"] is True


def test_unrelated_generation_does_not_stale_a_sealed_success(tmp_path):
    state, target = _state(tmp_path)
    _produce(state, target)
    first = _success(state)
    unrelated = target.with_name("unrelated.txt")
    _produce(state, unrelated)
    _unobserved_rewrite(unrelated)
    led = ledger.open_ledger(state, write=True)
    row = finish._file_evidence(state, led.records(), {"path": str(unrelated)})
    finish._record_identity_discontinuities(led, [{"evidence": row["evidence"]}])
    assert _invalidations(state)
    again = _success(state)
    assert again["status"] == "success" and again["reused_manifest"] is True, again
    assert again["delivery_digest"] == first["delivery_digest"]
    assert contract_audit.audit_experiment_delivery(state)["passed"] is True


def test_old_c_without_pointer_requires_current_source_review_only_for_related_facts(tmp_path):
    state, target = _state(tmp_path)
    _produce(state, target)
    assert _success(state)["status"] == "success"
    historical = delivery_transaction.read_manifest(state)
    old_shape = json.loads(json.dumps(historical))
    raw = json.loads(old_shape["artifacts"]["raw_results"]["content"])
    raw.pop("ledger_pointer")
    old_shape["artifacts"]["raw_results"]["content"] = json.dumps(raw)
    assert finish.delivery_source_findings(state, old_shape, ledger.open_ledger(state).records()) == []
    _unobserved_rewrite(target)
    assert _success(state)["status"] == "success_unsupported"
    findings = finish.delivery_source_findings(state, old_shape, ledger.open_ledger(state).records())
    assert findings and findings[0]["code"] == "delivery_source_history_unavailable"
    # This fixture only queries the old shape. It never rewrites historical C.
    assert delivery_transaction.read_manifest(state) == historical

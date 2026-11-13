"""A retired object may be rejected without retiring its untouched successor.

Real files and canonical action/finish records exercise the recovery rule. The
fixture's out-of-band rename is not evidence of sandbox write capability.
"""
import os

import pytest

from core.state import State
from nodes.experiment.tools import capture, delivery_transaction, ledger
from nodes.experiment.tests.test_060_output_identity_invalidation import (
    _invalidations, _produce, _state, _success, _unobserved_rewrite,
)


@pytest.mark.parametrize("successor_bytes", [b"7\n", b"49\n"])
@pytest.mark.parametrize("reopen", [False, True])
def test_retired_object_rejection_preserves_successor_and_recovers_without_production(
    tmp_path, successor_bytes, reopen,
):
    state, target = _state(tmp_path)
    vault = tmp_path / "external-vault"
    vault.mkdir()
    old_slot, new_slot = vault / "old", vault / "successor"
    old = _produce(state, target, b"7\n")
    target.rename(old_slot)
    successor = _produce(state, target, successor_bytes)
    identity = capture.identity(target)
    target.rename(new_slot)
    old_slot.rename(target)

    rejected = _success(state)
    assert rejected["status"] == "success_unsupported", rejected
    facts = _invalidations(state)
    assert {row["producer_action_id"] for row in facts} == {old}
    assert delivery_transaction.read_manifest(state) is None

    target.rename(old_slot)
    new_slot.rename(target)
    assert capture.identity(target) == identity
    if reopen:
        previous = state
        state = State.reopen("experiment", previous.root.parent, previous.run_id,
                             session_id="output-invalidation")
        state.hook_state["node_inputs"] = dict(previous.hook_state["node_inputs"])
    recovered = _success(state)
    assert recovered["status"] == "success", recovered
    manifest = delivery_transaction.read_manifest(state)
    assert manifest is not None
    assert _invalidations(state) == facts
    before_replay = ledger.open_ledger(state).records()
    assert _success(state)["status"] == "success"
    assert delivery_transaction.read_manifest(state) == manifest
    assert ledger.open_ledger(state).records() == before_replay
    assert {r["action_id"] for r in before_replay if r.get("kind") == "action_started"} == {old, successor}


@pytest.mark.parametrize("observed_missing", [False, True])
def test_known_retired_selection_cannot_clear_successors_own_proven_break(tmp_path, observed_missing):
    state, target = _state(tmp_path)
    vault = tmp_path / "external-vault"
    vault.mkdir()
    old_slot, new_slot = vault / "old", vault / "successor"
    old = _produce(state, target, b"7\n")
    target.rename(old_slot)
    successor = _produce(state, target, b"49\n")
    identity = capture.identity(target)
    if observed_missing:
        target.rename(new_slot)
    else:
        _unobserved_rewrite(target)
    assert _success(state)["status"] == "success_unsupported"
    facts = _invalidations(state)
    assert {row["producer_action_id"] for row in facts} == {old, successor}

    if not observed_missing:
        target.rename(new_slot)
    old_slot.rename(target)
    assert _success(state)["status"] == "success_unsupported"
    assert _invalidations(state) == facts
    target.rename(old_slot)
    new_slot.rename(target)
    if not observed_missing:
        os.utime(target, ns=(identity["mtime_ns"], identity["mtime_ns"]))
    assert capture.identity(target) == identity
    assert _success(state)["status"] == "success_unsupported"
    assert _invalidations(state) == facts
    _produce(state, target, b"49\n", replace=True)
    assert _success(state)["status"] == "success"
    assert _invalidations(state) == facts

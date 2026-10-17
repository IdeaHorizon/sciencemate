"""Resume admission uses the first task, not the replacement caller dictionary.

State.reopen and the normal ready-run hook are real. Only the downloader is a
test double: changed tasks must not reach it, while unchanged preparation may.
No second call to establish() is injected after reopen to hide a missing check.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from pathlib import Path

import pytest

from core.loop_hooks import HookContext
from core.state import State
from nodes.experiment.tools import ledger, resource_fetch, run_bootstrap


def _hook(state, turn):
    return run_bootstrap._on_turn_start(HookContext(
        harness=None, state=state, messages=[], turn=turn,
    ))


def _inputs():
    return {"experiment_spec": "Acquire the approved solver source, without changing it.",
            "scope": "operation", "operation_category": "toolchain_build",
            "prereg_assignment": {"kind": "none", "reason": "source preparation"},
            "required_outputs": [{"kind": "file", "path": "runtime/source.tar.gz"}]}


def _start(tmp_path, monkeypatch, *, inputs=None):
    monkeypatch.setenv("HARNESS_SANDBOX_EGRESS_ALLOWLIST", "github.com")
    state = State.new("experiment", tmp_path / "runs", session_id="owner")
    state.hook_state["node_inputs"] = deepcopy(inputs or _inputs())
    _hook(state, 1)
    assert ledger.bootstrap_phase(state) == "ready"
    assert state.task_instance_uuid is None
    return state


def _reopen(state, inputs, *, forwarded_ids=None):
    resumed = State.reopen("experiment", state.root.parent, state.run_id, session_id="owner")
    resumed.hook_state["node_inputs"] = deepcopy(inputs)
    if forwarded_ids is not None:
        resumed.hook_state["forwarded_input_ids"] = forwarded_ids
    _hook(resumed, 6)
    assert ledger.bootstrap_phase(resumed) == "ready"
    return resumed


def _fetch(state, monkeypatch):
    calls = []

    async def download(*args, **kwargs):
        calls.append(args)
        (Path(kwargs["writable_roots"][0]) / "download").write_bytes(b"source\n")
        return "done", 0, b"{}", b""

    monkeypatch.setattr(resource_fetch, "spawn_and_wait", download)
    result = asyncio.run(resource_fetch._fetch_resource(
        state, url="https://github.com/example/solver/source.tar.gz",
    ))
    return result, calls


@pytest.mark.parametrize("change", ["task", "required", "prereg", "forwarded"])
def test_reopen_cannot_replace_node_input_task_before_preparation(tmp_path, monkeypatch, change):
    state = _start(tmp_path, monkeypatch)
    inputs = deepcopy(state.hook_state["node_inputs"])
    if change == "task":
        inputs["experiment_spec"] = "Acquire another unrelated solver."
    elif change == "required":
        inputs["required_outputs"] = []
    elif change == "prereg":
        inputs["prereg_assignment"] = {"kind": "pending"}
    resumed = _reopen(state, inputs, forwarded_ids=["a-different-input"] if change == "forwarded" else None)
    assert run_bootstrap.load_run_authority(resumed)["startup_blocker"]["code"] == "run_authority_input_changed"
    result, calls = _fetch(resumed, monkeypatch)
    assert result["status"] == "error", result
    assert result["error_code"] == "execution_intent_binding_required"
    assert result["execution_intent_binding"]["status"] in {"intent_changed", "run_authority_input_changed"}
    assert not calls


@pytest.mark.parametrize("scope_only", [False, True])
def test_same_task_reopens_without_classification_or_rebootstrap(tmp_path, monkeypatch, scope_only):
    state = _start(tmp_path, monkeypatch)
    inputs = deepcopy(state.hook_state["node_inputs"])
    if scope_only:
        inputs["scope"] = "scientific"
        inputs["operation_category"] = "other"
    resumed = _reopen(state, inputs)
    result, calls = _fetch(resumed, monkeypatch)
    assert result["status"] == "success", result
    assert len(calls) == 1


def test_same_prereg_identity_becoming_available_does_not_change_task(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_SANDBOX_EGRESS_ALLOWLIST", "github.com")
    state = State.new("experiment", tmp_path / "runs", session_id="owner")
    prereg = state.save_artifact("pre_registration", "input", "exact protocol")
    record = state.read_artifact(prereg["id"])
    inputs = _inputs()
    inputs["prereg_assignment"] = {"kind": "bound", "artifact_id": prereg["id"],
                                   "version": record["version"], "content_hash": record["content_hash"]}
    state.hook_state["node_inputs"] = deepcopy(inputs)
    _hook(state, 1)
    assert run_bootstrap.load_run_authority(state)["prereg_status"] == "invalid"
    state.mark_frozen(prereg["id"])
    resumed = _reopen(state, inputs)
    authority = run_bootstrap.load_run_authority(resumed)
    assert authority["prereg_status"] == "bound_valid"
    assert authority["formal_input_authority"] == "bound"
    assert authority["startup_blocker"] is None
    result, calls = _fetch(resumed, monkeypatch)
    assert result["status"] == "success", result
    assert len(calls) == 1


def test_rejected_resume_does_not_poison_a_later_resume_with_original_task(tmp_path, monkeypatch):
    state = _start(tmp_path, monkeypatch)
    original = deepcopy(state.hook_state["node_inputs"])
    changed = {**original, "experiment_spec": "A different task"}
    wrong = _reopen(state, changed)
    result, calls = _fetch(wrong, monkeypatch)
    assert result["status"] == "error" and not calls
    restored = _reopen(wrong, original)
    assert run_bootstrap.load_run_authority(restored)["startup_blocker"] is None
    result, calls = _fetch(restored, monkeypatch)
    assert result["status"] == "success" and len(calls) == 1
    rows = ledger.open_ledger(restored).records()
    assert any((row.get("startup_blocker") or {}).get("code") == "run_authority_input_changed"
               for row in rows)
    assert len([row for row in rows if row["kind"] == "run_bootstrap"]) == 1


def test_forwarded_material_order_and_duplicates_are_not_new_input_identity(tmp_path, monkeypatch):
    state = State.new("experiment", tmp_path / "runs", session_id="owner")
    state.hook_state.update(node_inputs=_inputs(), forwarded_input_ids=["b", "a", "a"])
    monkeypatch.setenv("HARNESS_SANDBOX_EGRESS_ALLOWLIST", "github.com")
    _hook(state, 1)
    resumed = _reopen(state, _inputs(), forwarded_ids=["a", "b"])
    result, calls = _fetch(resumed, monkeypatch)
    assert result["status"] == "success" and len(calls) == 1


def test_old_bootstrap_without_input_identity_is_unverifiable_not_rebound(tmp_path, monkeypatch):
    """Build the historical A row via its writer, not a new-input fallback."""
    original_append = ledger.Ledger.append

    def old_append(self, kind, **fields):
        if kind == "run_bootstrap":
            fields.pop("node_input_digest", None)
        return original_append(self, kind, **fields)

    with monkeypatch.context() as patch:
        patch.setattr(ledger.Ledger, "append", old_append)
        state = _start(tmp_path, patch)
    monkeypatch.setenv("HARNESS_SANDBOX_EGRESS_ALLOWLIST", "github.com")
    resumed = _reopen(state, _inputs())
    authority = run_bootstrap.load_run_authority(resumed)
    assert authority["startup_blocker"]["code"] == "run_authority_unavailable"
    assert authority.get("node_input_digest") is None
    result, calls = _fetch(resumed, monkeypatch)
    assert result["status"] == "error" and not calls
    assert result["execution_intent_binding"]["status"] == "run_authority_unavailable"
    assert len([row for row in ledger.open_ledger(resumed).records()
                if row["kind"] == "run_bootstrap"]) == 1


@pytest.mark.parametrize("change", ["drift", "restore", "refresh"])
def test_observer_reopen_does_not_write_or_claim_an_authority_update(tmp_path, monkeypatch, change):
    state = _start(tmp_path, monkeypatch)
    original = deepcopy(state.hook_state["node_inputs"])
    incoming = deepcopy(original)
    if change == "drift":
        incoming["experiment_spec"] = "A different task"
    elif change == "restore":
        state = _reopen(state, {**original, "experiment_spec": "A different task"})
        assert run_bootstrap.load_run_authority(state)["startup_blocker"] is not None
    else:
        # Start a separate scientific run, because an observer may not replace A.
        state = State.new("experiment", tmp_path / "scientific", session_id="owner")
        prereg = state.save_artifact("pre_registration", "input", "exact protocol")
        record = state.read_artifact(prereg["id"])
        incoming["prereg_assignment"] = {"kind": "bound", "artifact_id": prereg["id"],
                                         "version": record["version"], "content_hash": record["content_hash"]}
        state.hook_state["node_inputs"] = deepcopy(incoming)
        _hook(state, 1)
        state.mark_frozen(prereg["id"])
        assert run_bootstrap.load_run_authority(state)["prereg_status"] == "invalid"
    records = ledger.open_ledger(state).records()
    authority = run_bootstrap.load_run_authority(state)
    owner = (ledger.ledger_dir(state) / "owner.lock").read_bytes()
    observer = State.reopen("experiment", state.root.parent, state.run_id, session_id="observer")
    observer.hook_state["node_inputs"] = incoming
    messages = _hook(observer, 7)
    assert messages and "只读" in messages[0].content
    assert "正式输入已就绪" not in messages[0].content
    assert ledger.open_ledger(observer).records() == records
    assert run_bootstrap.load_run_authority(observer) == authority
    assert (ledger.ledger_dir(state) / "owner.lock").read_bytes() == owner

"""v3 acquisition is preparation, not the retired scope-classification ritual.

Real State/bootstrap/ledger/path/egress code is exercised. Only the downloader
result is simulated; these tests do not claim real Internet connectivity or
prove the Core subprocess sandbox.
"""
from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

import pytest

from core.state import State
from core.task_contract import PreregAssignment, TaskContractLog
from nodes.experiment.tools import action_boundary, ledger, run_bootstrap
from nodes.experiment.tools import resource_fetch as fetch


SOURCE = "https://github.com/example/solver/source.tar.gz"
CONTENT = b"source or dependency bytes\n"


def _state(tmp_path, monkeypatch, *, assignment="omitted", role=None, core=False):
    monkeypatch.setenv("HARNESS_SANDBOX_EGRESS_ALLOWLIST", "github.com")
    state = State.new("experiment", tmp_path / "runs", session_id="fetch-owner")
    inputs = {"experiment_spec": "Download the source and dependencies before building.",
              "required_outputs": []}
    if assignment == "none":
        inputs["prereg_assignment"] = {"kind": "none", "reason": "engineering preparation"}
    elif assignment == "pending":
        inputs["prereg_assignment"] = {"kind": "pending"}
    elif assignment == "invalid":
        inputs["prereg_assignment"] = {"kind": "bound", "artifact_id": "missing",
                                       "version": 1, "content_hash": "a" * 64}
    if role:
        inputs["path_roles"] = {role: {"path": str(tmp_path / role), "writable": True}}
    state.hook_state["node_inputs"] = inputs
    if core:
        state.project_root = tmp_path / "project"
        _select_task(state, assignment=None)
    established = asyncio.run(run_bootstrap.establish(state))
    assert ledger.bootstrap_phase(state) == "ready", established
    # The regression must not acquire a hidden legacy classification receipt.
    assert "experiment_execution_scope" not in state.hook_state
    return state


def _select_task(state, *, assignment=None):
    revision = TaskContractLog(state.project_root / "tasks").append(
        task_instance_uuid="fetch-task", objective="Prepare the selected source for a build",
        target_node="experiment", prereg_assignment=assignment,
        actor="_orchestrator:parent", reason="formal dispatch",
        parent_revision_digest=state.task_contract_digest or "",
    )
    state.task_instance_uuid = revision.task_instance_uuid
    state.task_contract_revision = revision.revision
    state.task_contract_digest = revision.digest
    return revision


def _downloader(monkeypatch, state):
    calls = []

    async def download(*args, **kwargs):
        calls.append((args, kwargs))
        staging = Path(kwargs["writable_roots"][0])
        assert list(staging.iterdir()) == []
        # Admission and durable B must precede the first network request.
        starts = [row for row in ledger.open_ledger(state).records()
                  if row.get("kind") == "action_started"]
        assert starts and starts[-1]["tool"] == "fetch_resource"
        assert starts[-1]["effect_class"] == "support"
        if args[0] == "curl":
            (staging / "download").write_bytes(CONTENT)
        else:
            assert args[0] == "git"
            (staging / "repository" / ".git" / "objects").mkdir(parents=True)
            (staging / "repository" / ".git" / "refs").mkdir()
            (staging / "repository" / ".git" / "HEAD").write_text("a" * 40 + "\n")
            (staging / "repository" / "source.c").write_bytes(CONTENT)
        return "done", 0, b"{}", b""

    monkeypatch.setattr(fetch, "spawn_and_wait", download)
    return calls


@pytest.mark.parametrize("assignment", ["omitted", "none", "pending", "invalid"])
@pytest.mark.parametrize("kind", ["file", "git"])
def test_preparation_does_not_require_scope_or_unconsumed_prereg(
    tmp_path, monkeypatch, assignment, kind,
):
    state = _state(tmp_path, monkeypatch, assignment=assignment)
    calls = _downloader(monkeypatch, state)
    result = asyncio.run(fetch._fetch_resource(state, url=SOURCE, kind=kind))
    assert result["status"] == "success", result
    assert len(calls) == 1
    rows = ledger.open_ledger(state).records()
    start, = [row for row in rows if row["kind"] == "action_started"]
    settled, = [row for row in rows if row["kind"] == "action_settled"]
    assert settled["action_id"] == start["action_id"] and settled["exit_code"] == 0
    observation, = [row for row in rows if row["kind"] == "observation"]
    assert observation["action_id"] == start["action_id"]
    assert str(Path(result["destination"])) in observation["identities"]
    if kind == "file":
        assert result["sha256"] == hashlib.sha256(CONTENT).hexdigest()
    assert "experiment_execution_scope" not in state.hook_state


@pytest.mark.parametrize("role", ["managed_source_root", "dependency_root"])
def test_source_and_explicit_writable_dependency_destinations_are_equivalent(
    tmp_path, monkeypatch, role,
):
    state = _state(tmp_path, monkeypatch, role=role)
    _downloader(monkeypatch, state)
    target = tmp_path / role / "package.tar.gz"
    result = asyncio.run(fetch._fetch_resource(state, url=SOURCE, destination=str(target)))
    assert result["status"] == "success", result
    assert target.read_bytes() == CONTENT


@pytest.mark.parametrize("url", ["https://ungranted.example/source.tar.gz",
                                "https://github.com.evil.example/source.tar.gz"])
def test_unknown_scope_does_not_bypass_egress_authorization(tmp_path, monkeypatch, url):
    state = _state(tmp_path, monkeypatch)
    calls = _downloader(monkeypatch, state)
    result = asyncio.run(fetch._fetch_resource(state, url=url))
    assert result["error_code"] == "network_access_required", result
    assert not calls
    assert not (fetch.experiment_output_dir(state, "runtime") / "acquired").exists()


@pytest.mark.parametrize("role", ["source_baseline_root", "dependency_root"])
def test_preparation_does_not_make_readonly_destinations_writable(tmp_path, monkeypatch, role):
    state = _state(tmp_path, monkeypatch)
    readonly = tmp_path / "readonly"
    readonly.mkdir()
    state.hook_state["path_roles"] = {role: {"path": str(readonly), "writable": False}}
    calls = _downloader(monkeypatch, state)
    result = asyncio.run(fetch._fetch_resource(state, url=SOURCE, destination=str(readonly / "source")))
    assert result["status"] == "error", result
    assert "destination 必须位于" in result["error"]
    assert not calls and list(readonly.iterdir()) == []


def test_fetch_waits_for_durable_action_before_creating_acquired_directory(tmp_path, monkeypatch):
    state = _state(tmp_path, monkeypatch)
    calls = _downloader(monkeypatch, state)
    real_record = ledger.record

    def disk_failure(check_state, kind, **fields):
        if kind == "action_started":
            raise ledger.LedgerError("simulated disk full before payload")
        return real_record(check_state, kind, **fields)

    monkeypatch.setattr(ledger, "record", disk_failure)
    result = asyncio.run(fetch._fetch_resource(state, url=SOURCE))
    assert result["error_code"] == "action_not_durably_recorded", result
    assert not calls
    assert not (fetch.experiment_output_dir(state, "runtime") / "acquired").exists()


def test_half_bootstrap_never_falls_back_to_legacy_classification(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_SANDBOX_EGRESS_ALLOWLIST", "github.com")
    state = State.new("experiment", tmp_path / "runs")
    ledger.open_ledger(state, write=True, bootstrap=True)
    assert action_boundary.v3_ready(state) and ledger.bootstrap_phase(state) == "initializing"
    calls = _downloader(monkeypatch, state)
    result = asyncio.run(fetch._fetch_resource(state, url=SOURCE))
    assert result["error_code"] == "bootstrap_incomplete", result
    assert not calls
    assert not (fetch.experiment_output_dir(state, "runtime") / "acquired").exists()


def test_ready_run_does_not_claim_unchanged_authority_when_read_is_unavailable(tmp_path, monkeypatch):
    """Fault injection: the canonical reader returns None after a read failure.

    This is distinct from an unknown scope inside an available authority. A
    ready marker alone cannot certify that the current task is still the one
    recorded for this run.
    """
    state = _state(tmp_path, monkeypatch, core=True)
    assert ledger.bootstrap_phase(state) == "ready"
    calls = _downloader(monkeypatch, state)
    monkeypatch.setattr(run_bootstrap, "load_run_authority", lambda _state: None)
    result = asyncio.run(fetch._fetch_resource(state, url=SOURCE))
    assert result["status"] == "error", result
    assert result["execution_intent_binding"]["status"] == "run_authority_unavailable"
    assert not calls
    assert not (fetch.experiment_output_dir(state, "runtime") / "acquired").exists()


def test_core_pending_assignment_allows_preparation_without_inventing_scope(tmp_path, monkeypatch):
    state = _state(tmp_path, monkeypatch, core=True)
    _downloader(monkeypatch, state)
    authority = run_bootstrap.load_run_authority(state)
    assert authority["scope"] is None and authority["prereg_status"] == "omitted"
    assert authority["task_assignment_kind"] == "pending_assignment"
    result = asyncio.run(fetch._fetch_resource(state, url=SOURCE))
    assert result["status"] == "success", result
    assert run_bootstrap.load_run_authority(state) == authority


@pytest.mark.parametrize("change", ["new_revision", "invalid_digest"])
def test_core_task_identity_cannot_change_under_a_preparation(tmp_path, monkeypatch, change):
    state = _state(tmp_path, monkeypatch, core=True)
    calls = _downloader(monkeypatch, state)
    if change == "new_revision":
        _select_task(state, assignment=PreregAssignment.none("new assignment"))
    else:
        state.task_contract_digest = "f" * 64
    result = asyncio.run(fetch._fetch_resource(state, url=SOURCE))
    assert result["error_code"] == "execution_intent_binding_required", result
    binding = result["execution_intent_binding"]
    assert binding["status"] in {"run_authority_input_changed", "task_contract_unavailable",
                                 "task_contract_identity_invalid"}, binding
    assert "classify_experiment_scope" not in str(result)
    assert not calls
    assert not (fetch.experiment_output_dir(state, "runtime") / "acquired").exists()


def test_recorded_authority_replacement_still_blocks_noncore_fetch(tmp_path, monkeypatch):
    state = _state(tmp_path, monkeypatch)
    state.hook_state["node_inputs"]["prereg_assignment"] = {"kind": "pending"}
    asyncio.run(run_bootstrap.establish(state))
    authority = run_bootstrap.load_run_authority(state)
    assert authority["startup_blocker"]["code"] == "run_authority_input_changed"
    calls = _downloader(monkeypatch, state)
    result = asyncio.run(fetch._fetch_resource(state, url=SOURCE))
    assert result["error_code"] == "execution_intent_binding_required", result
    assert result["execution_intent_binding"]["status"] == "run_authority_input_changed"
    assert not calls


def test_scope_conflict_alone_does_not_reintroduce_preparation_gate(tmp_path, monkeypatch):
    state = _state(tmp_path, monkeypatch, assignment="none")
    state.hook_state["node_inputs"]["scope"] = "operation"
    asyncio.run(run_bootstrap.establish(state))
    state.hook_state["node_inputs"]["scope"] = "scientific"
    asyncio.run(run_bootstrap.establish(state))
    authority = run_bootstrap.load_run_authority(state)
    assert authority["startup_blocker"]["code"] == "scope_resolution_conflict"
    _downloader(monkeypatch, state)
    result = asyncio.run(fetch._fetch_resource(state, url=SOURCE))
    assert result["status"] == "success", result
    assert run_bootstrap.load_run_authority(state) == authority

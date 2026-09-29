"""两条 e2e 实测出来的缺口（2026-08-21，/tmp/e2e-mesh2 run 1787281220-b844bd）：

1. data 的 blocked report 留在子 run 目录里、不会回填父 state，
   `record_data_delivery_outcome` 却只用 `state.read_artifact` 找它 —— 传对 id
   也永远读不到，agent 只能反复试错。
2. operation 的收尾契约只描述"做成了"的形状（必须有 verification）。一个
   operation 合理受阻时没有可验证的东西，于是任何 blocked 收尾都必然判不合格。
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from core.artifact_provenance import produced
from core.ledger import RecordStore, workspace_store
from core.project_workspace import _NODE_WORKSPACES
from core.state import State
from nodes.experiment.hooks import _audit_operation_log
from nodes.experiment.tools import run_contract
from nodes.experiment.tools import execution_action_census as census
from nodes.experiment.tools.contract_audit import (
    _dispatch_data_request, _reconcile_data_dispatch, _record_data_delivery_outcome,
    _validate_data_request_spec, _verify_dataset_consumption,
)


def _service_spec() -> str:
    return json.dumps({
        "request_kind": "preprocessing_service_request",
        "requesting_stage": "toolchain_build",
        "purpose": "需要一份通道网格来验证求解器能读入",
        "target_software": "meshio 5.x",
        "scientific_parameters": "not_applicable",
        "required_assets": [{"name": "channel.msh", "format": "Gmsh 4.1", "purpose": "solver mesh"}],
        "acceptance": {"file_exists": True, "schema": True, "units": True, "manifest_lineage": True},
    })


def test_managed_data_dispatch_requires_a_user_note(tmp_path: Path):
    runs = tmp_path / "runs"
    runs.mkdir()
    state = State.new("experiment", runs)
    spec = asyncio.run(_validate_data_request_spec(state, _service_spec()))

    result = asyncio.run(_dispatch_data_request(state, spec["spec_id"]))

    assert result["status"] == "error"
    assert "user_note is required" in result["error"]


def _bind_fake_worktree(state: State, project: Path) -> Path:
    """Model a parent bound to a project worktree without a git checkout.

    ``bind_project_workspace`` needs a real git worktree; these tests only need
    the two facts the record layer reads: where the worktree is and where this
    node's own records go (``experiments/``). A Data child's records land in
    ``data/`` of the same worktree ledger.
    """
    (project / _NODE_WORKSPACES["data"]).mkdir(parents=True, exist_ok=True)
    state.project_worktree = project
    state.workspace_records_dir = project / _NODE_WORKSPACES["experiment"]
    return project


def _record_store(state: State, child_run_id: str, worktree: Path | None) -> tuple[RecordStore, Path]:
    """Where a Data child's record lives: the project worktree ledger when the
    parent is bound to one, else the child's own run-local ledger."""
    if worktree is not None:
        return workspace_store(worktree), worktree / _NODE_WORKSPACES["data"]
    child_run_dir = state.root.parent / child_run_id
    return (RecordStore(child_run_dir / "artifacts", child_run_dir / "records.jsonl"),
            child_run_dir / "artifacts")


def _save_record(
    store: RecordStore, directory: Path, *, artifact_id: str, artifact_type: str,
    content: str, produced_by_node_type: str, produced_by_run_id: str,
) -> None:
    """One save row + the native file, with the producer facts on the ledger."""
    store.save(
        artifact_id=artifact_id, artifact_type=artifact_type,
        name=artifact_id.split("__", 1)[-1], content=content, metadata={},
        directory=directory, created_at="2026-09-12T00:00:00+00:00",
        provenance=produced(produced_by_node_type, produced_by_run_id),
        produced_by_node_type=produced_by_node_type, produced_by_run_id=produced_by_run_id,
        by_node=produced_by_node_type, by_run=produced_by_run_id,
    )


def _write_child_blocked_report(
    state: State, child_run_id: str, artifact_id: str, *,
    worktree: Path | None = None,
) -> None:
    """Persist the Data child provenance and its unimported blocked report."""
    child_run_dir = state.root.parent / child_run_id
    child_run_dir.mkdir(parents=True, exist_ok=True)
    (child_run_dir / "transcript.jsonl").write_text(json.dumps({
        "event": "run_start", "node_type": "data", "parent_run_id": state.run_id,
    }) + "\n", encoding="utf-8")
    store, directory = _record_store(state, child_run_id, worktree)
    _save_record(
        store, directory, artifact_id=artifact_id, artifact_type="preprocessing_blocked_report",
        content=json.dumps({
            "status": "fatal",
            "reason": "the declared source rejected every fetch attempt",
            "acquisition_attempts": [{
                "url": "https://data.example.org/declared-asset",
                "failure_type": "http_403",
                "evidence_path": "logs/fetch.stderr",
            }],
        }),
        produced_by_node_type="data", produced_by_run_id=child_run_id,
    )


def _write_child_dataset(
    state: State, child_run_id: str, artifact_id: str, package_dir: Path, *,
    worktree: Path | None = None, produced_by_run_id: str | None = None,
) -> None:
    """Persist a completed Data dataset only in the child run for pause recovery.

    ``produced_by_run_id`` re-records the same identity under another Data run:
    the producer is a ledger fact, so a relabel is a new save row.
    """
    manifest = package_dir / "manifest.json"
    manifest.write_text(json.dumps({"source": "data-child"}), encoding="utf-8")
    child_run_dir = state.root.parent / child_run_id
    child_run_dir.mkdir(parents=True, exist_ok=True)
    store, directory = _record_store(state, child_run_id, worktree)
    _save_record(
        store, directory, artifact_id=artifact_id, artifact_type="dataset",
        content=json.dumps({
            "package_dir": str(package_dir),
            "manifest_path": str(manifest),
            "lineage": {"data_run_id": child_run_id},
            "downstream_contract": {"required_assets": ["channel.msh"]},
        }),
        produced_by_node_type="data", produced_by_run_id=produced_by_run_id or child_run_id,
    )


def _mark_child_terminal(
    state: State, child_run_id: str, *, status: str, artifact_ids: list[str],
) -> None:
    """Model the durable child completion written before Core cascades resume."""
    child_run_dir = state.root.parent / child_run_id
    with (child_run_dir / "transcript.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"event": "run_end", "status": status}) + "\n")
    (child_run_dir / "summary.json").write_text(
        json.dumps({"status": status, "artifacts": [{"id": item} for item in artifact_ids]}),
        encoding="utf-8",
    )


def test_blocked_report_is_read_from_the_managed_data_child_run(tmp_path: Path, monkeypatch):
    """Only a wrapper-returned direct Data child can supply a fallback blocker."""
    runs = tmp_path / "runs"
    runs.mkdir()
    state = State.new("experiment", runs)
    _classified_operation(state)
    project = _bind_fake_worktree(state, tmp_path / "project-worktree")
    child_run_id = "1787281382-0d2459"
    report_id = "preprocessing_blocked_report__planning_contract_failure"
    spec = asyncio.run(_validate_data_request_spec(state, _service_spec()))

    async def fake_run_node(**kwargs):
        assert kwargs["node_type"] == "data"
        assert kwargs["node_inputs"] == spec["dispatch_node_inputs"]
        assert kwargs["resume_run_id"] == "fresh"
        _write_child_blocked_report(state, child_run_id, report_id, worktree=project)
        return {
            "status": "incomplete",
            "child_run_id": child_run_id,
            "child_node_type": "data",
            "child_status": "incomplete",
            "all_child_artifacts": [{"id": report_id}],
        }

    monkeypatch.setattr("shared.tools.run_node._run_node_tool", fake_run_node)
    dispatched = asyncio.run(_dispatch_data_request(
        state, spec["spec_id"], user_note="Request the declared mesh package from Data.",
    ))
    assert dispatched["spec_id"] == spec["spec_id"]
    assert dispatched["data_dispatch_receipt"]["child_run_id"] == child_run_id
    assert dispatched["data_dispatch_receipt"]["request_sha256"] == spec["request_sha256"]
    assert dispatched["data_dispatch_receipt"]["payload_sha256"] == spec["payload_sha256"]

    wrong_report = asyncio.run(_record_data_delivery_outcome(
        state, spec["spec_id"], "blocked",
        data_run_id=child_run_id,
        blocked_report_id="preprocessing_blocked_report__not_returned",
    ))
    assert wrong_report["status"] == "error"
    assert "data_blocked_report_not_returned_by_dispatch" in wrong_report["receipt_errors"]

    result = asyncio.run(_record_data_delivery_outcome(
        state, spec["spec_id"], "blocked",
        data_run_id=child_run_id, blocked_report_id=report_id))

    assert result["status"] == "success", result
    assert result["blocked_report_source"] == "managed_data_child_run"
    assert state.hook_state["input_delivery_state"][spec["spec_id"]]["data_terminally_blocked"] is True


def test_managed_data_dispatch_rejects_intent_drift_before_starting_data(
    tmp_path: Path, monkeypatch,
):
    runs = tmp_path / "runs"
    runs.mkdir()
    state = State.new("experiment", runs)
    _classified_operation(state)
    spec = asyncio.run(_validate_data_request_spec(state, _service_spec()))
    state.hook_state["node_inputs"]["experiment_focus"] = "Replace the requested mesh with a proxy."

    calls: list[dict] = []

    async def fake_run_node(**kwargs):
        calls.append(kwargs)
        return {}

    monkeypatch.setattr("shared.tools.run_node._run_node_tool", fake_run_node)
    result = asyncio.run(_dispatch_data_request(
        state, spec["spec_id"], user_note="Request the declared mesh package from Data.",
    ))

    assert result["status"] == "error"
    assert "immutable upstream-intent binding" in result["error"]
    assert result["intent_binding"]["status"] == "intent_changed"
    assert calls == []


def test_managed_data_dispatch_rejects_duplicate_active_spec(
    tmp_path: Path, monkeypatch,
):
    runs = tmp_path / "runs"
    runs.mkdir()
    state = State.new("experiment", runs)
    _classified_operation(state)
    spec = asyncio.run(_validate_data_request_spec(state, _service_spec()))
    child_run_id = "data-duplicate-child"
    report_id = "preprocessing_blocked_report__duplicate"

    calls = 0

    async def fake_run_node(**kwargs):
        nonlocal calls
        calls += 1
        _write_child_blocked_report(state, child_run_id, report_id)
        return {
            "status": "incomplete",
            "child_run_id": child_run_id,
            "child_node_type": "data",
            "child_status": "incomplete",
            "all_child_artifacts": [{"id": report_id}],
        }

    monkeypatch.setattr("shared.tools.run_node._run_node_tool", fake_run_node)
    first = asyncio.run(_dispatch_data_request(
        state, spec["spec_id"], user_note="Request the declared mesh package from Data.",
    ))
    second = asyncio.run(_dispatch_data_request(
        state, spec["spec_id"], user_note="Do not start a duplicate Data request.",
    ))

    assert first["status"] == "incomplete", first
    assert second["status"] == "error"
    assert "already has a managed Data dispatch" in second["error"]
    assert calls == 1


def test_paused_managed_data_dispatch_is_reconciled_from_one_terminal_child(
    tmp_path: Path, monkeypatch,
):
    runs = tmp_path / "runs"
    runs.mkdir()
    state = State.new("experiment", runs)
    _classified_operation(state)
    spec = asyncio.run(_validate_data_request_spec(state, _service_spec()))
    child_run_id = "data-paused-child"
    report_id = "preprocessing_blocked_report__after_pause"

    async def fake_run_node(**kwargs):
        assert kwargs["resume_run_id"] == "fresh"
        child_run_dir = state.root.parent / child_run_id
        child_run_dir.mkdir(parents=True, exist_ok=True)
        (child_run_dir / "transcript.jsonl").write_text(json.dumps({
            "event": "run_start", "node_type": "data", "parent_run_id": state.run_id,
        }) + "\n", encoding="utf-8")
        return {
            "status": "pause",
            "pause_event": {"question": "May Data access the declared public source?"},
            "child_run_id": child_run_id,
            "child_node_type": "data",
        }

    monkeypatch.setattr("shared.tools.run_node._run_node_tool", fake_run_node)
    paused = asyncio.run(_dispatch_data_request(
        state, spec["spec_id"], user_note="Request the declared mesh package from Data.",
    ))

    assert paused["status"] == "pause", paused
    assert paused["data_dispatch_receipt"]["dispatch_state"] == "paused_pending_result"
    duplicate = asyncio.run(_dispatch_data_request(
        state, spec["spec_id"], user_note="Do not duplicate the paused request.",
    ))
    assert duplicate["status"] == "error"
    assert "already has a managed Data dispatch" in duplicate["error"]

    pending = asyncio.run(_reconcile_data_dispatch(state))
    assert pending["status"] == "error"
    assert pending["error"] == "data_dispatch_paused_child_not_terminal"

    _write_child_blocked_report(state, child_run_id, report_id)
    _mark_child_terminal(state, child_run_id, status="incomplete", artifact_ids=[report_id])
    bypass = asyncio.run(_record_data_delivery_outcome(
        state, spec["spec_id"], "blocked", child_run_id, report_id,
    ))
    assert bypass["status"] == "error"
    assert "reconcile_data_dispatch" in bypass["error"]
    # P0a v4：paused 的派发在 census 里终态未知；reconcile 必须让它收敛。
    def _dispatch_terminals() -> list[str]:
        return [a["terminal_status"]
                for a in census.reduce_execution_action_census(state)["actions"]
                if a["tool"] == "dispatch_data_request"]
    assert _dispatch_terminals() == ["unknown"]
    assert "accounted_action_terminal_unknown" in (
        census.operation_execution_obligation(state)["failure_reasons"])
    reconciled = asyncio.run(_reconcile_data_dispatch(state))

    assert reconciled["status"] == "success", reconciled
    assert reconciled["data_run_id"] == child_run_id
    assert reconciled["blocked_report_id"] == report_id
    assert _dispatch_terminals() == ["failed"]          # blocked → failed, no longer unknown
    assert "accounted_action_terminal_unknown" not in (
        census.operation_execution_obligation(state)["failure_reasons"])
    assert reconciled["child_status"] == "incomplete"

    recorded = asyncio.run(_record_data_delivery_outcome(
        state, spec["spec_id"], "blocked",
        reconciled["data_run_id"], reconciled["blocked_report_id"],
    ))
    assert recorded["status"] == "success", recorded
    receipt = state.hook_state["input_delivery_state"][spec["spec_id"]][
        "data_dispatch_receipt"
    ]
    assert receipt["dispatch_state"] == "resumed_terminal"
    assert receipt["child_artifact_ids_origin"] == "direct_child_after_pause"


def test_paused_managed_data_dispatch_recovers_and_verifies_a_dataset(
    tmp_path: Path, monkeypatch,
):
    runs = tmp_path / "runs"
    runs.mkdir()
    state = State.new("experiment", runs)
    _classified_operation(state)
    project = _bind_fake_worktree(state, tmp_path / "project-worktree")
    spec = asyncio.run(_validate_data_request_spec(state, _service_spec()))
    child_run_id = "data-paused-dataset-child"
    dataset_id = "dataset__paused_mesh"

    async def fake_run_node(**kwargs):
        child_run_dir = state.root.parent / child_run_id
        child_run_dir.mkdir(parents=True, exist_ok=True)
        (child_run_dir / "transcript.jsonl").write_text(json.dumps({
            "event": "run_start", "node_type": "data", "parent_run_id": state.run_id,
        }) + "\n", encoding="utf-8")
        return {
            "status": "pause",
            "pause_event": {"question": "May Data access the declared public source?"},
            "child_run_id": child_run_id,
            "child_node_type": "data",
        }

    monkeypatch.setattr("shared.tools.run_node._run_node_tool", fake_run_node)
    assert asyncio.run(_dispatch_data_request(
        state, spec["spec_id"], user_note="Request the declared mesh package from Data.",
    ))["status"] == "pause"

    package_dir = tmp_path / "paused-data-package"
    package_dir.mkdir()
    (package_dir / "channel.msh").write_text("$MeshFormat\n4.1 0 8\n", encoding="utf-8")
    _write_child_dataset(state, child_run_id, dataset_id, package_dir, worktree=project)
    _mark_child_terminal(
        state, child_run_id, status="completed", artifact_ids=[dataset_id],
    )

    reconciled = asyncio.run(_reconcile_data_dispatch(state))
    assert reconciled["status"] == "success", reconciled
    assert reconciled["terminal_outcome"] == "dataset"
    assert reconciled["dataset_artifact_id"] == dataset_id
    assert reconciled["all_child_artifacts"] == [{"id": dataset_id}]

    verified = asyncio.run(_verify_dataset_consumption(
        state, dataset_id, spec["spec_id"],
    ))
    assert verified["status"] == "success", verified
    delivery = state.hook_state["input_delivery_state"][spec["spec_id"]]
    assert delivery["provider"] == "data"
    assert delivery["verified"] is True


def test_managed_returned_dataset_requires_its_direct_data_child_in_project_worktree(
    tmp_path: Path, monkeypatch,
):
    """A managed receipt cannot consume an arbitrary or shadowed visible dataset."""
    runs = tmp_path / "runs"
    runs.mkdir()
    state = State.new("experiment", runs)
    _classified_operation(state)
    project = _bind_fake_worktree(state, tmp_path / "project-worktree")
    spec = asyncio.run(_validate_data_request_spec(state, _service_spec()))
    child_run_id = "data-returned-worktree-child"
    dataset_id = "dataset__returned_mesh"
    package_dir = tmp_path / "returned-data-package"
    package_dir.mkdir()
    (package_dir / "channel.msh").write_text("mesh\n", encoding="utf-8")
    shadow_package_dir = tmp_path / "shadowed-experiment-package"
    shadow_package_dir.mkdir()

    async def fake_run_node(**kwargs):
        child_run_dir = state.root.parent / child_run_id
        child_run_dir.mkdir(parents=True, exist_ok=True)
        (child_run_dir / "transcript.jsonl").write_text(json.dumps({
            "event": "run_start", "node_type": "data", "parent_run_id": state.run_id,
        }) + "\n", encoding="utf-8")
        _write_child_dataset(state, child_run_id, dataset_id, package_dir, worktree=project)
        # A same-id record in Experiment ownership (this run's local ledger) is
        # not a Data deliverable; the worktree ledger's Data-produced head is.
        _save_record(
            RecordStore(state.root / "artifacts", state.root / "records.jsonl"),
            state.root / "artifacts", artifact_id=dataset_id, artifact_type="dataset",
            content=json.dumps({"package_dir": str(shadow_package_dir)}),
            produced_by_node_type="experiment", produced_by_run_id=state.run_id,
        )
        return {
            "status": "completed",
            "child_run_id": child_run_id,
            "child_node_type": "data",
            "child_status": "completed",
            "all_child_artifacts": [{"id": dataset_id}],
        }

    monkeypatch.setattr("shared.tools.run_node._run_node_tool", fake_run_node)
    dispatched = asyncio.run(_dispatch_data_request(
        state, spec["spec_id"], user_note="Request the declared mesh package from Data.",
    ))
    assert dispatched["status"] == "completed", dispatched

    visible_old = state.save_artifact("dataset", "unrelated_visible_mesh", "{}")
    wrong = asyncio.run(_verify_dataset_consumption(
        state, visible_old["id"], spec["spec_id"],
    ))
    assert wrong["status"] == "error"
    assert wrong["error"] == "dataset_artifact_id_not_returned_by_managed_data_child"
    assert not state.hook_state["input_delivery_state"][spec["spec_id"]]["verified"]

    # The producer is a ledger fact: re-record the identity under another Data run.
    _write_child_dataset(state, child_run_id, dataset_id, package_dir, worktree=project,
                         produced_by_run_id="another-data-run")
    wrong_producer = asyncio.run(_verify_dataset_consumption(
        state, dataset_id, spec["spec_id"],
    ))
    assert wrong_producer["status"] == "error"
    assert wrong_producer["error"] == "managed_data_dataset_invalid"
    assert "data_dataset_unreadable_from_managed_child" in wrong_producer["dataset_errors"]
    assert not state.hook_state["input_delivery_state"][spec["spec_id"]]["verified"]

    _write_child_dataset(state, child_run_id, dataset_id, package_dir, worktree=project)
    original_inputs = dict(state.hook_state["node_inputs"])
    state.hook_state["node_inputs"] = {
        **original_inputs, "experiment_focus": "A changed upstream objective.",
    }
    drifted = asyncio.run(_verify_dataset_consumption(
        state, dataset_id, spec["spec_id"],
    ))
    assert drifted["status"] == "error"
    assert "immutable upstream-intent binding" in drifted["error"]
    assert not state.hook_state["input_delivery_state"][spec["spec_id"]]["verified"]

    state.hook_state["node_inputs"] = original_inputs
    verified = asyncio.run(_verify_dataset_consumption(
        state, dataset_id, spec["spec_id"],
    ))
    assert verified["status"] == "success", verified


def test_paused_reconciliation_requires_summary_membership_and_uniqueness(
    tmp_path: Path, monkeypatch,
):
    runs = tmp_path / "runs"
    runs.mkdir()
    state = State.new("experiment", runs)
    _classified_operation(state)
    spec = asyncio.run(_validate_data_request_spec(state, _service_spec()))
    child_run_id = "data-paused-ambiguous-child"
    first_report = "preprocessing_blocked_report__first"
    second_report = "preprocessing_blocked_report__second"

    async def fake_run_node(**kwargs):
        child_run_dir = state.root.parent / child_run_id
        child_run_dir.mkdir(parents=True, exist_ok=True)
        (child_run_dir / "transcript.jsonl").write_text(json.dumps({
            "event": "run_start", "node_type": "data", "parent_run_id": state.run_id,
        }) + "\n", encoding="utf-8")
        return {
            "status": "pause",
            "child_run_id": child_run_id,
            "child_node_type": "data",
        }

    monkeypatch.setattr("shared.tools.run_node._run_node_tool", fake_run_node)
    assert asyncio.run(_dispatch_data_request(
        state, spec["spec_id"], user_note="Request the declared mesh package from Data.",
    ))["status"] == "pause"

    _write_child_blocked_report(state, child_run_id, first_report)
    _write_child_blocked_report(state, child_run_id, second_report)
    _mark_child_terminal(
        state, child_run_id, status="incomplete", artifact_ids=[],
    )
    unlisted = asyncio.run(_reconcile_data_dispatch(state, spec["spec_id"]))
    assert unlisted["status"] == "error"
    assert unlisted["error"] == "data_dispatch_paused_terminal_delivery_missing_or_ambiguous"
    assert unlisted["dataset_artifact_ids"] == []
    assert unlisted["blocked_report_ids"] == []

    (state.root.parent / child_run_id / "summary.json").write_text(json.dumps({
        "status": "incomplete",
        "artifacts": [{"id": first_report}, {"id": second_report}],
    }), encoding="utf-8")
    ambiguous = asyncio.run(_reconcile_data_dispatch(state, spec["spec_id"]))

    assert ambiguous["status"] == "error"
    assert ambiguous["error"] == "data_dispatch_paused_terminal_delivery_missing_or_ambiguous"
    assert ambiguous["dataset_artifact_ids"] == []
    assert ambiguous["blocked_report_ids"] == [first_report, second_report]
    bypass = asyncio.run(_record_data_delivery_outcome(
        state, spec["spec_id"], "blocked", child_run_id, first_report,
    ))
    assert bypass["status"] == "error"
    assert "reconcile_data_dispatch" in bypass["error"]


def test_unmanaged_data_report_gets_an_actionable_error(tmp_path: Path):
    """A raw run_node child id is not enough to authorize a fallback path."""
    runs = tmp_path / "runs"
    runs.mkdir()
    state = State.new("experiment", runs)
    spec = asyncio.run(_validate_data_request_spec(state, _service_spec()))

    result = asyncio.run(_record_data_delivery_outcome(
        state, spec["spec_id"], "blocked",
        data_run_id="1787281382-0d2459",
        blocked_report_id="preprocessing_blocked_report__planning_contract_failure"))

    assert result["status"] == "error"
    assert "dispatch_data_request" in result["error"]
    assert "dispatch_data_request" in result["next_step"]
    assert "managed_data_dispatch_receipt_missing" in result["receipt_errors"]


def test_child_run_reference_with_path_components_is_rejected(tmp_path: Path):
    """E-12-lite：run_id/artifact_id 带路径成分一律拒读，哪怕穿越目标真实存在。"""
    from nodes.experiment.tools.contract_audit import _read_child_run_artifact

    runs = tmp_path / "runs"
    runs.mkdir()
    state = State.new("experiment", runs)
    report_id = "preprocessing_blocked_report__planning_contract_failure"
    child_run_id = "1787281382-0d2459"
    # 穿越目标真实存在：不消毒的话 runs/../outside 是可达的。
    outside = State.new("experiment", tmp_path / "outside_runs")
    _write_child_blocked_report(outside, "outside", report_id)
    _write_child_blocked_report(state, child_run_id, report_id)

    for evil_run_id in ("../outside_runs/outside", "outside/nested", "/etc"):
        assert _read_child_run_artifact(state, evil_run_id, report_id) is None
    for evil_artifact_id in (f"../artifacts/{report_id}", "sub/dir_report", ".hidden"):
        assert _read_child_run_artifact(state, child_run_id, evil_artifact_id) is None
    # 消毒不能误伤合法引用。
    assert isinstance(_read_child_run_artifact(state, child_run_id, report_id), dict)


def test_symlinked_child_artifact_escaping_run_dir_is_rejected(tmp_path: Path):
    """E-12-lite：账本行合法，但正文文件是指向 run 外的符号链接 —— 二道防线拒。"""
    from nodes.experiment.tools.contract_audit import _read_child_run_artifact

    runs = tmp_path / "runs"
    runs.mkdir()
    state = State.new("experiment", runs)
    child_run_id = "1787281382-0d2459"
    outside = tmp_path / "secrets.json"
    outside.write_text(json.dumps({"status": "recoverable_blocked"}), encoding="utf-8")
    (runs / child_run_id).mkdir(parents=True)
    (runs / child_run_id / "transcript.jsonl").write_text(json.dumps({
        "event": "run_start", "node_type": "data", "parent_run_id": state.run_id,
    }) + "\n", encoding="utf-8")
    store, directory = _record_store(state, child_run_id, None)
    _save_record(
        store, directory, artifact_id="linked_report",
        artifact_type="preprocessing_blocked_report",
        content=json.dumps({"status": "fatal"}),
        produced_by_node_type="data", produced_by_run_id=child_run_id,
    )
    linked = directory / "linked_report.json"
    linked.unlink()
    linked.symlink_to(outside)

    assert _read_child_run_artifact(state, child_run_id, "linked_report") is None


def test_cross_project_run_is_not_reachable_any_more(tmp_path: Path):
    """E-12-lite：字符合法但非同级的 run 不再经 find_run_dir 可达。"""
    from nodes.experiment.tools.contract_audit import _read_child_run_artifact

    runs = tmp_path / "runs"
    runs.mkdir()
    state = State.new("experiment", runs)
    foreign_run_id = "1787281999-feed00"
    report_id = "preprocessing_blocked_report__planning_contract_failure"
    # 另一个"项目"的 runs 目录里放着真实产物 —— 不该被读到。
    foreign = State.new("experiment", tmp_path / "other_project" / "runs")
    _write_child_blocked_report(foreign, foreign_run_id, report_id)

    assert _read_child_run_artifact(state, foreign_run_id, report_id) is None


def _complete_operation(state: State, *, outcome: str = "success", checks=None,
                        next_step: str = "", blocker_id: str = "") -> dict:
    from nodes.experiment.tools.operation_completion import _record_operation_completion
    if checks is None:
        checks = [{"name": "operation_check", "passed": outcome == "success",
                   "evidence": {"returncode": 0 if outcome == "success" else 1}}]
    evidence = Path(state.root) / "operation_test.stdout"
    evidence.write_text("returncode=" + ("0" if outcome == "success" else "1") + "\n", encoding="utf-8")
    return asyncio.run(_record_operation_completion(
        state, task_kind="generic", objective="validate a bounded operation",
        outcome=outcome, checks=checks, artifact_paths=[str(evidence)], blocker_id=blocker_id, next_step=next_step,
    ))


def _classified_operation(state: State) -> None:
    node_inputs = state.hook_state.setdefault("node_inputs", {
        "experiment_focus": "Validate the declared bounded operation fixture.",
    })
    node_inputs.setdefault("prereg_assignment", {
        "kind": "none",
        "reason": "This operation fixture has no governing preregistration.",
    })
    asyncio.run(run_contract._classify_experiment_scope(
        state, scope="operation", operation_category="format_validation",
        reason="Validate a required input without scientific analysis.",
    ))


@pytest.mark.parametrize(
    "artifact_type",
    ["raw_results", "clean_results", "experiment_log"],
)
def test_operational_scope_rejects_manual_closure_artifact_save(
    tmp_path: Path,
    artifact_type: str,
):
    from shared.tools.builtin import _save_artifact

    state = State.new("experiment", tmp_path)
    _classified_operation(state)

    result = asyncio.run(_save_artifact(
        state,
        artifact_type=artifact_type,
        name="manual_recovery_evidence",
        content="模型自述的恢复证据",
    ))

    assert result["status"] == "error"
    assert result["failed_checks"] == ["closure_owner"]
    assert "record_operation_completion" in result["hint"]
    assert state.list_artifacts(artifact_type) == []


def test_unclassified_scope_rejects_manual_closure_artifact_save(
    tmp_path: Path,
):
    from shared.tools.builtin import _save_artifact

    state = State.new("experiment", tmp_path)
    result = asyncio.run(_save_artifact(
        state,
        artifact_type="experiment_log",
        name="premature_log",
        content="scope 尚未分类",
    ))

    assert result["status"] == "error"
    assert "classify_experiment_scope" in result["hint"]
    assert state.list_artifacts("experiment_log") == []


def test_scientific_scope_keeps_existing_result_save_path(tmp_path: Path):
    from shared.tools.builtin import _save_artifact

    state = State.new("experiment", tmp_path)
    state.hook_state["experiment_execution_scope"] = {
        "mode": "scientific",
        "category": None,
    }

    result = asyncio.run(_save_artifact(
        state,
        artifact_type="experiment_log",
        name="scientific_log",
        content="## Verdict\ninconclusive",
    ))

    assert result["status"] == "success"
    assert len(state.list_artifacts("experiment_log")) == 1


def test_blocked_operation_with_a_registered_blocker_closes_cleanly(tmp_path: Path):
    state = State.new("experiment", tmp_path)
    _classified_operation(state)
    state.hook_state["blockers"] = [{"blocker_id": "run:1", "reporting_node": "experiment", "suggested_owner": "data"}]
    completion = _complete_operation(
        state, outcome="blocked", next_step="ask data to supply the declared mesh artifact",
        checks=[], blocker_id="run:1",
    )
    audit = _audit_operation_log(state)

    assert completion["status"] == "success", completion
    assert audit["passed"] is True, audit
    assert audit["blocked_closure"] is True
    assert audit["has_verification"] is True
    assert "blocked" in audit["reason"]


def test_blocked_closure_without_registered_blocker_records_receipt(tmp_path: Path):
    """判决拆除（oc:178 删，2026-08-31）：blocked 收据本身就是 blocker 声明。

    不再要求先在 hook_state 登记 blocker 才许记 blocked 收据；收据照记、
    审计照过，登记数如实为 0。
    """
    state = State.new("experiment", tmp_path)
    _classified_operation(state)
    result = _complete_operation(
        state, outcome="blocked", next_step="ask the owner for the missing input artifact",
        checks=[{"name": "input_available", "passed": False, "evidence": {"missing": "input"}}],
    )

    assert result["status"] == "success", result
    audit = _audit_operation_log(state)
    assert audit["passed"] is True, audit
    assert audit["blocked_closure"] is True
    assert audit["n_registered_blockers"] == 0


def test_operation_completion_rejects_status_style_checks_at_input_boundary(tmp_path: Path):
    """A scheduler rejection must not be silently dropped as an invalid check."""
    from nodes.experiment.tools.operation_completion import _record_operation_completion

    state = State.new("experiment", tmp_path)
    _classified_operation(state)
    state.hook_state["blockers"] = [{"id": "run:1", "suggested_owner": "orchestrator"}]
    evidence = Path(state.root) / "scheduler_rejection.json"
    evidence.write_text("{\"error_code\": \"rejected\"}\n", encoding="utf-8")

    result = asyncio.run(_record_operation_completion(
        state, task_kind="generic", objective="record scheduler preflight rejection",
        outcome="blocked", artifact_paths=[str(evidence)],
        checks=[{"name": "submit_job", "status": "failed"}],
        next_step="redispatch with a declared absolute workdir",
    ))

    assert result["status"] == "error"
    assert result["error_code"] == "invalid_checks"
    assert "passed: false" in result["error"]
    assert not state.list_artifacts("raw_results")


def test_operation_log_cannot_close_without_frozen_result_evidence(tmp_path: Path):
    state = State.new("experiment", tmp_path)
    _classified_operation(state)
    _frozen_saved = state.save_artifact(
        "experiment_log", "operation_receipt", "## Execution\ncommand: validate input\n## Verification\ncheck: passed\n",
        metadata={"record_kind": "operation", "status": "completed"},
    )
    state.mark_frozen(_frozen_saved["id"])   # 冻结只出自账本的 freeze 行

    audit = _audit_operation_log(state)

    assert audit["passed"] is False
    assert any("raw_results" in error for error in audit["result_evidence"]["errors"])
    assert "requires exactly one current-run raw_results" in audit["reason"]


def test_closure_is_the_frozen_evidence_triplet(tmp_path: Path):
    state = State.new("experiment", tmp_path)
    _classified_operation(state)
    completion = _complete_operation(state)

    audit = _audit_operation_log(state)

    assert completion["status"] == "success", completion
    assert audit["closure_recorded"] is True
    assert audit["passed"] is True
    assert "has_status" not in audit


def test_operation_completion_coalesces_one_file_across_evidence_roles(
    tmp_path: Path,
):
    from nodes.experiment.tools.operation_completion import _record_operation_completion

    state = State.new("experiment", tmp_path)
    _classified_operation(state)
    executable = Path(state.root) / "smoke"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)

    result = asyncio.run(_record_operation_completion(
        state,
        task_kind="generic",
        objective="构建并验证同一个可执行产物",
        outcome="success",
        checks=[{
            "name": "smoke_run",
            "passed": True,
            "evidence": {"returncode": 0},
        }],
        artifact_paths=[str(executable)],
        executable_paths=[str(executable)],
    ))

    assert result["status"] == "success", result
    raw = state.read_artifact(result["raw_results_artifact_id"])
    manifest = json.loads(raw["content"])
    paths = [item["path"] for item in manifest["files"]]
    assert len(paths) == len(set(paths))
    entries = [
        item for item in manifest["files"]
        if item["path"] == str(executable.resolve())
    ]
    assert entries == [{
        **entries[0],
        "role": "operation_executable",
    }]
    assert sum(
        1 for item in result["checks"]
        if (item.get("evidence") or {}).get("path") == str(executable.resolve())
    ) == 1




def test_toolchain_build_task_kind_is_normalized_to_canonical_build(
    tmp_path: Path,
):
    from nodes.experiment.tests._mechanical_execution import (
        record_completed_local_mechanical_action,
    )
    from nodes.experiment.tools.operation_completion import (
        _record_operation_completion,
    )

    state = State.new("experiment", tmp_path)
    state.hook_state["node_inputs"] = {
        "experiment_focus": "Build and minimally run the local toolchain smoke executable.",
        "prereg_assignment": {
            "kind": "none",
            "reason": "This mechanical build has no governing preregistration.",
        },
    }
    asyncio.run(run_contract._classify_experiment_scope(
        state,
        scope="operation",
        operation_category="toolchain_build",
        reason="构建并最小运行本地程序，不产生科学结果或科学裁决。",
    ))
    executable = Path(state.root) / "toolchain-smoke"
    # P0a v4：声明的 build 产物必须映射到满足义务的 attempt 收尾时冻结的身份收据——
    # 该步骤声明它（expected_outputs）并在 attempt 内写出（produce=）。原来写在
    # attempt 之后、也没声明：夹具顺序问题，不是被钉住的选择。
    record_completed_local_mechanical_action(
        state,
        produce=lambda: executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8"),
        expected_outputs=["toolchain-smoke"],
    )
    executable.chmod(0o755)

    result = asyncio.run(_record_operation_completion(
        state,
        task_kind="toolchain_build",
        objective="构建并验证本地 toolchain smoke 程序",
        outcome="success",
        checks=[{
            "name": "minimal_run",
            "passed": True,
            "evidence": {"returncode": 0},
        }],
        executable_paths=[str(executable)],
    ))

    assert result["status"] == "success", result
    clean = json.loads(state.read_artifact(
        result["clean_results_artifact_id"])["content"])
    assert clean["task_kind"] == "build"

def test_raw_freeze_failure_is_actionable_and_same_tool_can_resume(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    import nodes.experiment.tools.operation_completion as completion_module

    state = State.new("experiment", tmp_path)
    _classified_operation(state)
    original_freeze = completion_module._freeze_artifact
    calls = {"n": 0}

    async def fail_once_on_raw(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return {
                "status": "error",
                "error": "raw_results cannot be frozen before its gates pass",
                "failed_checks": ["raw_results_manifest"],
                "reasons": {"raw_results_manifest": "injected invalid manifest"},
                "hint": "injected remediation",
            }
        return await original_freeze(*args, **kwargs)

    monkeypatch.setattr(completion_module, "_freeze_artifact", fail_once_on_raw)
    first = _complete_operation(state)

    assert first["status"] == "error"
    assert first["error_code"] == "operation_artifact_freeze_failed"
    assert first["reasons"] == {
        "raw_results_manifest": "injected invalid manifest",
    }
    assert first["hint"] == "injected remediation"
    assert first["partial_closure"]["owner"] == "record_operation_completion"
    assert "不要手工" in first["partial_closure"]["retry_guidance"]
    assert len(state.list_artifacts("raw_results")) == 1
    assert not state.list_artifacts("clean_results")

    monkeypatch.setattr(completion_module, "_freeze_artifact", original_freeze)
    resumed = _complete_operation(state)

    assert resumed["status"] == "success", resumed
    assert len(state.list_artifacts("raw_results")) == 1
    assert len(state.list_artifacts("clean_results")) == 1
    assert len(state.list_artifacts("experiment_log")) == 1


def test_legacy_owned_duplicate_raw_draft_is_rebuilt_in_same_identity(
    tmp_path: Path,
):
    import nodes.experiment.tools.operation_completion as completion_module

    state = State.new("experiment", tmp_path)
    _classified_operation(state)
    executable = Path(state.root) / "legacy-smoke"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    objective = "恢复旧版本留下的重复 raw draft"
    caller_checks = [{
        "name": "smoke_run",
        "passed": True,
        "evidence": {"returncode": 0},
    }]
    persisted_checks = [
        *caller_checks,
        completion_module._path_check(str(executable)),
        completion_module._path_check(str(executable), executable=True),
    ]
    closure_id = f"{state.run_id}:operation"
    legacy_input = {
        "task_kind": "build",
        "objective": objective,
        "outcome": "success",
        "checks": persisted_checks,
        "next_step": None,
        "summary": None,
        "blocker_id": None,
        "external_job_refs": [],
    }
    metadata = {
        "record_kind": "operation",
        "analysis_eligible": False,
        "status": "completed",
        "operation_closure_owner": "record_operation_completion",
        "operation_closure_id": closure_id,
        "operation_closure_version": 1,
        "operation_closure_input": legacy_input,
    }
    receipt = Path(state.root) / "operation_receipt.json"
    receipt.write_text(json.dumps({
        "schema_version": 2,
        "closure_id": closure_id,
        "record_kind": "operation",
        **legacy_input,
        "blocker": None,
    }), encoding="utf-8")
    duplicate = completion_module._file_entry(
        executable, "operation_output"
    )
    state.save_artifact(
        "raw_results",
        f"operation_raw_{state.run_id}",
        json.dumps({
            "record_kind": "operation",
            "closure_id": closure_id,
            "files": [
                completion_module._file_entry(
                    receipt, "operation_verification_receipt"
                ),
                duplicate,
                completion_module._file_entry(
                    executable, "operation_executable"
                ),
            ],
        }),
        metadata=metadata,
    )
    decoy = Path(state.root) / "retry-decoy"
    decoy.write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
    decoy.chmod(0o755)

    result = asyncio.run(completion_module._record_operation_completion(
        state,
        task_kind="build",
        objective=objective,
        outcome="success",
        checks=caller_checks,
        artifact_paths=[str(decoy)],
        executable_paths=[str(decoy)],
    ))

    assert result["status"] == "success", result
    raw = state.read_artifact(result["raw_results_artifact_id"])
    manifest = json.loads(raw["content"])
    assert [item["path"] for item in manifest["files"]].count(
        str(executable.resolve())
    ) == 1
    assert str(decoy.resolve()) not in {
        item["path"] for item in manifest["files"]
    }
    assert raw["version"] == 2
    assert raw["metadata"]["frozen"] is True
    closure_inputs = {
        json.dumps(
            state.read_artifact(result[key])["metadata"][
                "operation_closure_input"
            ],
            ensure_ascii=False,
            sort_keys=True,
        )
        for key in (
            "raw_results_artifact_id",
            "clean_results_artifact_id",
            "experiment_log_artifact_id",
        )
    }
    assert closure_inputs == {
        json.dumps(legacy_input, ensure_ascii=False, sort_keys=True)
    }


def test_a_successful_claim_without_evidence_paths_is_demoted_to_partial(tmp_path: Path):
    """判决拆除 O6/跨批依赖 §3（oc:138 降格，2026-08-31）。

    缺证据路径不再拒绝记收据：它成为一条 passed=False 的 check，而
    「outcome=success 但验证未通过」被机械降为 partial 并披露 —— 账本如实，
    不伪造成功，也不新造拒绝墙。
    """
    from nodes.experiment.tools.operation_completion import _record_operation_completion

    state = State.new("experiment", tmp_path)
    _classified_operation(state)
    result = asyncio.run(_record_operation_completion(
        state, task_kind="generic", objective="claim a generic operation succeeded",
        checks=[{"name": "model_claim", "passed": True, "evidence": "not a retained file"}],
    ))

    assert result["status"] == "success", result
    assert result["outcome"] == "partial"
    assert result["outcome_demoted_from"] == "success"
    assert "evidence_paths_present" in result["failed_checks"]


def test_blocked_operation_uses_registered_blocker_as_its_receipt(tmp_path: Path):
    """No pre-created evidence file is needed to close an honest blocked operation."""
    from nodes.experiment.tools.operation_completion import _record_operation_completion

    state = State.new("experiment", tmp_path)
    _classified_operation(state)
    state.hook_state["blockers"] = [{
        "blocker_id": f"{state.run_id}:1", "reporting_node": "experiment",
        "category": "environment", "summary": "scheduler workdir placeholder is unresolved",
        "requested_action": "redispatch with a declared absolute workdir",
    }]
    result = asyncio.run(_record_operation_completion(
        state, task_kind="generic", objective="record scheduler preflight rejection",
        outcome="blocked", blocker_id=f"{state.run_id}:1", checks=[],
        next_step="redispatch with a declared absolute workdir",
    ))

    assert result["status"] == "success", result
    assert len(state.list_artifacts("raw_results")) == 1
    assert len(state.list_artifacts("clean_results")) == 1
    assert len(state.list_artifacts("experiment_log")) == 1
    assert _audit_operation_log(state)["passed"] is True


def test_manual_operation_result_is_a_conflict_not_a_second_triplet(tmp_path: Path):
    from nodes.experiment.tools.operation_completion import _record_operation_completion

    state = State.new("experiment", tmp_path)
    _classified_operation(state)
    state.save_artifact("raw_results", "manual_raw", "{\"record_kind\": \"operation\", \"files\": []}")
    evidence = Path(state.root) / "operation.stdout"
    evidence.write_text("returncode=0\n", encoding="utf-8")
    result = asyncio.run(_record_operation_completion(
        state, task_kind="generic", objective="do not duplicate polluted closure",
        artifact_paths=[str(evidence)], checks=[{"name": "returncode", "passed": True, "evidence": {"returncode": 0}}],
    ))

    assert result["status"] == "error"
    assert result["error_code"] == "operation_closure_conflict"
    assert len(state.list_artifacts("raw_results")) == 1
    assert not state.list_artifacts("clean_results")
    assert not state.list_artifacts("experiment_log")


def _scientific_draft_then_operation(
    state: State, *, frozen: bool = False,
) -> str:
    """Model a mistaken scientific draft created before an operation reclassification."""
    state.hook_state["node_inputs"] = {
        "experiment_focus": "Mechanically validate a bounded software operation.",
        "prereg_assignment": {
            "kind": "none",
            "reason": "This correction fixture has no governing preregistration.",
        },
    }
    scientific = asyncio.run(run_contract._classify_experiment_scope(
        state,
        scope="scientific",
        reason="The repeated measurements were initially mistaken for scientific inference.",
    ))
    assert scientific["status"] == "success", scientific
    draft_id = state.save_artifact(
        "experiment_log",
        "mistaken_scientific_draft",
        "## Execution Status\nstatus: configure_complete\n",
    )["id"]
    if frozen:
        # 冻结的事实只出自账本的 freeze 行（C1）；save 时带 frozen 会被剥掉。
        state.mark_frozen(draft_id)
    operational = asyncio.run(run_contract._classify_experiment_scope(
        state,
        scope="operation",
        operation_category="format_validation",
        reason="The acceptance result is mechanically fixed and makes no scientific claim.",
    ))
    assert operational["status"] == "success", operational
    return draft_id


def test_frozen_supersession_of_current_unfrozen_log_unlocks_operation_closure(
    tmp_path: Path,
):
    from nodes.experiment.tools.contract_audit import _supersede_closure_draft

    state = State.new("experiment", tmp_path)
    draft_id = _scientific_draft_then_operation(state)
    superseded = asyncio.run(_supersede_closure_draft(
        state,
        artifact_id=draft_id,
        reason="This was an unfrozen scope-misclassification draft, not canonical evidence.",
    ))

    assert superseded["status"] == "success", superseded
    supersession = state.read_artifact(superseded["supersession_id"])
    assert (supersession.get("metadata") or {}).get("frozen") is True

    completion = _complete_operation(state)

    assert completion["status"] == "success", completion
    assert completion["outcome"] == "success"
    audit = _audit_operation_log(state)
    assert audit["passed"] is True, audit
    from nodes.experiment.tools.contract_audit import audit_experiment_log_integrity
    integrity = audit_experiment_log_integrity(state)
    assert integrity["superseded_ids"] == [draft_id]


def test_unsuperseded_scientific_draft_remains_an_operation_closure_conflict(
    tmp_path: Path,
):
    state = State.new("experiment", tmp_path)
    draft_id = _scientific_draft_then_operation(state)

    completion = _complete_operation(state)

    assert completion["status"] == "error"
    assert completion["error_code"] == "operation_closure_conflict"
    assert [item["id"] for item in completion["conflicts"]] == [draft_id]


def test_frozen_scientific_log_cannot_be_superseded_or_excluded_from_operation_closure(
    tmp_path: Path,
):
    from nodes.experiment.tools.contract_audit import _supersede_closure_draft

    state = State.new("experiment", tmp_path)
    draft_id = _scientific_draft_then_operation(state, frozen=True)

    superseded = asyncio.run(_supersede_closure_draft(
        state,
        artifact_id=draft_id,
        reason="A frozen scientific record must remain active.",
    ))
    completion = _complete_operation(state)

    assert superseded["status"] == "error"
    assert completion["status"] == "error"
    assert completion["error_code"] == "operation_closure_conflict"


def test_unfrozen_supersession_record_does_not_unlock_operation_closure(
    tmp_path: Path,
):
    state = State.new("experiment", tmp_path)
    draft_id = _scientific_draft_then_operation(state)
    state.save_artifact(
        "experiment_log_supersession",
        "unfrozen_forged_supersession",
        json.dumps({
            "superseded_id": draft_id,
            "reason": "This record was never frozen.",
            "run_id": state.run_id,
        }),
        metadata={"superseded_id": draft_id},
    )

    completion = _complete_operation(state)

    assert completion["status"] == "error"
    assert completion["error_code"] == "operation_closure_conflict"


def test_cross_run_supersession_does_not_unlock_operation_closure(
    tmp_path: Path,
):
    state = State.new("experiment", tmp_path)
    draft_id = _scientific_draft_then_operation(state)
    foreign_id = "experiment_log_supersession__foreign"
    foreign_record = {
        "id": foreign_id,
        "type": "experiment_log_supersession",
        "name": "foreign",
        "content": json.dumps({
            "superseded_id": draft_id,
            "reason": "A different run cannot negate this run's record.",
            "run_id": "foreign-run",
        }),
        "metadata": {"frozen": True, "superseded_id": draft_id},
        "produced_by_run_id": "foreign-run",
    }
    original_list = state.list_artifacts
    original_read = state.read_artifact

    def list_artifacts(artifact_type=None, own_only=False):
        records = original_list(artifact_type, own_only=own_only)
        if artifact_type == "experiment_log_supersession":
            return records + [{"id": foreign_id, "type": artifact_type}]
        return records

    def read_artifact(artifact_id):
        return foreign_record if artifact_id == foreign_id else original_read(artifact_id)

    state.list_artifacts = list_artifacts
    state.read_artifact = read_artifact

    completion = _complete_operation(state)

    assert completion["status"] == "error"
    assert completion["error_code"] == "operation_closure_conflict"


def test_completed_operation_closure_is_idempotent(tmp_path: Path):
    state = State.new("experiment", tmp_path)
    _classified_operation(state)
    first = _complete_operation(state)
    second = _complete_operation(state)

    assert first["status"] == "success"
    assert second["status"] == "success"
    assert second["idempotent"] is True
    assert first["experiment_log_artifact_id"] == second["experiment_log_artifact_id"]
    assert len(state.list_artifacts("raw_results")) == 1
    assert len(state.list_artifacts("clean_results")) == 1
    assert len(state.list_artifacts("experiment_log")) == 1


def test_completed_operation_closure_rejects_a_different_identity(tmp_path: Path):
    from nodes.experiment.tools.operation_completion import _record_operation_completion

    state = State.new("experiment", tmp_path)
    _classified_operation(state)
    first = _complete_operation(state)

    conflict = asyncio.run(_record_operation_completion(
        state,
        task_kind="generic",
        objective="另一个并未执行的目标",
        outcome="success",
        checks=[{
            "name": "claim",
            "passed": True,
            "evidence": {"returncode": 0},
        }],
        artifact_paths=[str(Path(state.root) / "operation_test.stdout")],
    ))

    assert first["status"] == "success"
    assert conflict["status"] == "error"
    assert conflict["error_code"] == "operation_closure_conflict"
    assert conflict["expected"] == {
        "task_kind": "generic",
        "objective": "validate a bounded operation",
        "outcome": "success",
    }


def test_partial_operation_closure_resumes_without_duplicate_artifacts(tmp_path: Path, monkeypatch):
    import nodes.experiment.tools.operation_completion as completion_module

    state = State.new("experiment", tmp_path)
    _classified_operation(state)
    original_freeze = completion_module._freeze_artifact
    calls = {"n": 0}

    async def fail_once_on_clean(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            return {"status": "error", "error": "injected clean freeze interruption"}
        return await original_freeze(*args, **kwargs)

    monkeypatch.setattr(completion_module, "_freeze_artifact", fail_once_on_clean)
    first = _complete_operation(state)
    assert first["status"] == "error"
    assert len(state.list_artifacts("raw_results")) == 1
    assert len(state.list_artifacts("clean_results")) == 1
    assert not state.list_artifacts("experiment_log")

    monkeypatch.setattr(completion_module, "_freeze_artifact", original_freeze)
    resumed = _complete_operation(state)
    assert resumed["status"] == "success", resumed
    assert len(state.list_artifacts("raw_results")) == 1
    assert len(state.list_artifacts("clean_results")) == 1
    assert len(state.list_artifacts("experiment_log")) == 1
    assert _audit_operation_log(state)["passed"] is True


def test_log_freeze_interruption_is_partial_not_false_complete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    import nodes.experiment.tools.operation_completion as completion_module

    state = State.new("experiment", tmp_path)
    _classified_operation(state)
    original_freeze = completion_module._freeze_artifact
    calls = {"n": 0}

    async def fail_once_on_log(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 3:
            return {"status": "error", "error": "injected log freeze interruption"}
        return await original_freeze(*args, **kwargs)

    monkeypatch.setattr(completion_module, "_freeze_artifact", fail_once_on_log)
    first = _complete_operation(state)
    assert first["status"] == "error"
    assert len(state.list_artifacts("raw_results")) == 1
    assert len(state.list_artifacts("clean_results")) == 1
    assert len(state.list_artifacts("experiment_log")) == 1
    log_id = state.list_artifacts("experiment_log")[0]["id"]
    assert state.read_artifact(log_id)["metadata"].get("frozen") is not True

    monkeypatch.setattr(completion_module, "_freeze_artifact", original_freeze)
    resumed = _complete_operation(state)

    assert resumed["status"] == "success", resumed
    assert resumed.get("idempotent") is not True
    assert state.read_artifact(log_id)["metadata"]["frozen"] is True
    assert _audit_operation_log(state)["passed"] is True


def _single_step_route(state: State, route_state: str, *, goal: str = "完成受管操作") -> dict:
    from nodes.experiment.tools.execution_route import (
        _declare_execution_route,
        load_canonical_route,
        step_definition_hash,
    )

    declared = asyncio.run(_declare_execution_route(state, route={
        "schema_version": 2,
        "goal": goal,
        "evidence_refs": ["https://example.invalid/operation-guide"],
        "steps": [{
            "id": "operate",
            "goal": goal,
            "after": [],
            "action": {"tool": "safe_run_bash", "program": "printf"},
            "effects": ["workspace_write"],
            "workdir_role": "run_root",
            "expected_outputs": [],
        }],
    }, amendment_reason=(
        "根据新证据修订操作路线"
        if load_canonical_route(state).get("status") == "ready" else ""
    )))
    assert declared["status"] == "success", declared
    step = load_canonical_route(state)["route"]["steps"][0]
    if route_state == "actionable":
        return declared
    attempt_id = f"attempt-{declared['route_ref']['version']}"
    state.append_transcript(
        "route_step_bound",
        route_artifact_id=declared["route_ref"]["artifact_id"],
        route_version=declared["route_ref"]["version"],
        route_content_hash=declared["route_ref"]["content_hash"],
        route_step_id=step["id"],
        step_definition_hash=step_definition_hash(step),
        attempt_id=attempt_id,
        tool="safe_run_bash",
        resolved_workdir_role="run_root",
        applied_policy="test",
    )
    if route_state == "failed":
        state.append_transcript(
            "route_step_outcome", attempt_id=attempt_id,
            outcome="failed", failure_class="returncode_nonzero",
        )
    elif route_state == "in_progress":
        state.append_transcript(
            "route_step_outcome", attempt_id=attempt_id,
            outcome="submitted", domain_receipts=["job_submission__test"],
        )
    elif route_state == "complete":
        state.append_transcript(
            "route_step_outcome", attempt_id=attempt_id, outcome="success",
            managed_tool_receipt={"status": "success", "returncode": 0},
        )
    else:
        raise AssertionError(f"unknown test route state: {route_state}")
    return declared


def test_success_completion_requires_complete_canonical_route(tmp_path: Path):
    expected_route_states = {
        "actionable": "actionable",
        "failed": "blocked",
        "in_progress": "in_progress",
        "complete": "complete",
    }
    for requested_state, expected_route_state in expected_route_states.items():
        state = State.new("experiment", tmp_path / requested_state)
        _classified_operation(state)
        _single_step_route(state, requested_state)

        completion = _complete_operation(state)

        if requested_state == "complete":
            assert completion["status"] == "success", completion
            assert any(
                item["name"] == "execution_route_complete"
                for item in completion["checks"]
            )
            assert _audit_operation_log(state)["passed"] is True
        else:
            assert completion["status"] == "error", completion
            assert completion["error_code"] == "execution_route_incomplete"
            assert completion["execution_route"]["route_state"] == expected_route_state
            assert not state.list_artifacts("raw_results")


def test_failed_and_blocked_completion_do_not_require_route_complete(tmp_path: Path):
    failed_state = State.new("experiment", tmp_path / "failed")
    _classified_operation(failed_state)
    _single_step_route(failed_state, "actionable")
    failed = _complete_operation(
        failed_state, outcome="failed",
        next_step="diagnose the retained command failure before retrying",
    )
    assert failed["status"] == "success", failed

    blocked_state = State.new("experiment", tmp_path / "blocked")
    _classified_operation(blocked_state)
    _single_step_route(blocked_state, "actionable")
    blocked_state.hook_state["blockers"] = [{
        "blocker_id": "route:blocker", "reporting_node": "experiment",
        "category": "environment", "summary": "required toolchain is unavailable",
    }]
    blocked = _complete_operation(
        blocked_state, outcome="blocked", blocker_id="route:blocker",
        checks=[], next_step="install the required toolchain and retry",
    )
    assert blocked["status"] == "success", blocked


# ── F11：一步失败就收尾 = 把整个 run 锁死（2026-09-06 平台 E2E 活体撞出）─────────
#
# 四条各自正确的规则组合成一条无解死路：
#   ① outcome=failed 跳过路线完成检查 → 直接封口 closure；
#   ② failed step 回不到 ready，唯一 re-arm 是改路线让 step_definition_hash 变；
#   ③ closure 一封口，路线内容变更即被 execution_route_sealed_by_operation_closure 拒；
#   ④ 同一 run 只有一个 closure（<run_id>:operation）且不能改判 outcome。
# ①~④ 单看都对，合起来让「一次步骤失败」不可逆地终结整个 run。判决不动（①仍不拒），
# 但事实必须进账本、代价与出口必须写进返回值。


def test_failed_closure_over_unfinished_route_is_recorded_not_refused(tmp_path: Path):
    """路线还有 ready 步骤时以 failed 收尾：照冻结（既有判决），但如实记账 + 给出口。"""
    state = State.new("experiment", tmp_path)
    _classified_operation(state)
    _single_step_route(state, "actionable")

    completion = _complete_operation(
        state, outcome="failed",
        next_step="diagnose the failed step before deciding whether to continue",
    )

    assert completion["status"] == "success", completion
    # ① 事实进账本：冻结件里有一条 passed=False 的路线 check，带未执行步骤清单。
    route_checks = [item for item in completion["checks"]
                    if item["name"] == "execution_route_complete"]
    assert route_checks, completion["checks"]
    assert route_checks[0]["passed"] is False
    assert route_checks[0]["evidence"]["ready_step_ids"] == ["operate"]
    assert "execution_route_complete" in completion["failed_checks"]
    # ② 代价与出口进返回值（BF-12）——且出口必须是节点**做得到**的事：
    #    不能指 resume_run（无 register_tool，节点调不到）。
    gap = completion["route_incomplete_at_closure"]
    assert gap["ready_step_ids"] == ["operate"]
    assert completion["retryable_in_this_run"] is False
    assert completion["node_action"] == "report_unfinished_route_and_stop"
    recovery = completion["recovery"]
    assert "resume_run" not in recovery, "出口不能指向节点调不到的工具（BF-12）"
    assert "报告" in recovery and "本 run 到此为止" in recovery
    # ③ 冻结件本身也带着这条事实（不只在返回值里）。
    log = state.read_artifact(completion["experiment_log_artifact_id"])
    frozen_checks = ((log.get("metadata") or {})
                     .get("operation_closure_input") or {}).get("checks") or []
    assert any(item.get("name") == "execution_route_complete"
               and item.get("passed") is False for item in frozen_checks)


def test_completed_route_failure_reports_no_route_gap(tmp_path: Path):
    """路线确实走完了的 failed 收尾：check 通过，且不报未完成缺口（不误报）。"""
    state = State.new("experiment", tmp_path)
    _classified_operation(state)
    _single_step_route(state, "complete")

    completion = _complete_operation(
        state, outcome="failed",
        next_step="the route ran to completion; the operation itself failed",
    )

    assert completion["status"] == "success", completion
    assert "route_incomplete_at_closure" not in completion
    route_checks = [item for item in completion["checks"]
                    if item["name"] == "execution_route_complete"]
    assert route_checks and route_checks[0]["passed"] is True


def test_sealing_over_unfinished_route_locks_the_run(tmp_path: Path):
    """钉住死锁本身：封口之后四条路全堵——这正是上面那条出口必须说实话的理由。

    本测试不主张这四条墙有错（各自都对），只锁定「代价是真的」，
    使任何一侧的放宽都必须显式改判本用例。
    """
    from nodes.experiment.tools.execution_route import _declare_execution_route

    state = State.new("experiment", tmp_path)
    _classified_operation(state)
    _single_step_route(state, "actionable")
    sealed = _complete_operation(state, outcome="failed", next_step="stop here")
    assert sealed["status"] == "success", sealed

    # ④ 同一 run 不能改判 outcome（身份三元组变了）。
    retried = _complete_operation(state, outcome="success")
    assert retried["status"] == "error"
    assert retried["error_code"] == "operation_closure_conflict"

    # ③ 封口后连路线都改不了 —— 于是 ② 的唯一 re-arm 路径也没了。
    amended = asyncio.run(_declare_execution_route(state, route={
        "schema_version": 2,
        "goal": "完成受管操作",
        "evidence_refs": ["https://example.invalid/operation-guide"],
        "steps": [{
            "id": "operate",
            "goal": "完成受管操作",
            "after": [],
            "action": {"tool": "safe_run_bash", "program": "echo"},
            "effects": ["workspace_write"],
            "workdir_role": "run_root",
            "expected_outputs": [],
        }],
    }, amendment_reason="修正入口程序后重试失败的步骤"))
    assert amended["status"] == "error"
    assert amended["error_code"] == "execution_route_sealed_by_operation_closure"


def test_completed_operation_rejects_a_new_route_version_before_write(
    tmp_path: Path,
):
    from nodes.experiment.tools.execution_route import (
        _canonical_route_artifact_id,
        _declare_execution_route,
        load_canonical_route,
    )
    from nodes.experiment.tools.operation_completion import audit_operation_completion

    state = State.new("experiment", tmp_path)
    _classified_operation(state)
    first_route = _single_step_route(state, "complete", goal="完成第一版路线")
    completion = _complete_operation(state)
    assert completion["status"] == "success", completion
    assert audit_operation_completion(state)["passed"] is True
    revised = dict(load_canonical_route(state)["route"])
    revised["goal"] = "完成修订后的路线"

    rejected = asyncio.run(_declare_execution_route(
        state,
        route=revised,
        amendment_reason="闭环后出现另一个目标",
    ))

    assert rejected["status"] == "error"
    assert rejected["error_code"] == "execution_route_sealed_by_operation_closure"
    assert [item["version"] for item in state.artifact_versions(
        _canonical_route_artifact_id(state)
    )] == [first_route["route_ref"]["version"]]
    assert audit_operation_completion(state)["passed"] is True


def test_dispatch_receipt_event_persistence_failure_is_structured_and_reconcilable(
    tmp_path: Path, monkeypatch,
):
    """P0a v4 (Codex 23 号 P0-2): the child ran and the delivery ledger is committed,
    only the census receipt event failed to persist.  The tool must not return a
    plain success (the model would dispatch a second child) and must name a real
    exit: reconcile_data_dispatch appends the missing event, idempotently."""
    runs = tmp_path / "runs"
    runs.mkdir()
    state = State.new("experiment", runs)
    _classified_operation(state)
    project = _bind_fake_worktree(state, tmp_path / "project-worktree")
    spec = asyncio.run(_validate_data_request_spec(state, _service_spec()))
    child_run_id = "data-returned-flaky-receipt-child"
    dataset_id = "dataset__flaky_receipt_mesh"
    package_dir = tmp_path / "flaky-data-package"
    package_dir.mkdir()
    (package_dir / "channel.msh").write_text("mesh\n", encoding="utf-8")
    calls = {"run_node": 0}

    async def fake_run_node(**kwargs):
        calls["run_node"] += 1
        child_run_dir = state.root.parent / child_run_id
        child_run_dir.mkdir(parents=True, exist_ok=True)
        (child_run_dir / "transcript.jsonl").write_text(json.dumps({
            "event": "run_start", "node_type": "data", "parent_run_id": state.run_id,
        }) + "\n", encoding="utf-8")
        _write_child_dataset(state, child_run_id, dataset_id, package_dir, worktree=project)
        return {
            "status": "completed", "child_run_id": child_run_id,
            "child_node_type": "data", "child_status": "completed",
            "all_child_artifacts": [{"id": dataset_id}],
        }

    monkeypatch.setattr("shared.tools.run_node._run_node_tool", fake_run_node)
    real_append = state.append_transcript
    tripped = {"count": 0}

    def flaky_append(event: str, **fields):
        if event == "data_request_dispatched" and tripped["count"] == 0:
            tripped["count"] += 1
            raise OSError("simulated transcript write failure")
        return real_append(event, **fields)

    monkeypatch.setattr(state, "append_transcript", flaky_append)
    dispatched = asyncio.run(_dispatch_data_request(
        state, spec["spec_id"], user_note="Request the declared mesh package from Data.",
    ))
    assert dispatched["status"] == "error", dispatched
    assert dispatched["error_code"] == "dispatch_committed_but_receipt_event_not_durable"
    assert dispatched["side_effect_committed"] is True
    assert dispatched["payload_must_not_rerun"] is True
    assert dispatched["next_action"]["action"] == "reconcile_data_dispatch"
    assert dispatched["next_action"]["arguments"] == {"spec_id": spec["spec_id"]}
    assert calls["run_node"] == 1
    # v5: the committed delivery ledger is the authority — the census projects the
    # returned dispatch from it even before the transcript event is back-filled.
    rows = [a for a in census.reduce_execution_action_census(state)["actions"]
            if a["tool"] == "dispatch_data_request"]
    assert [(a["terminal_status"], a["receipt_event"]) for a in rows] == [
        ("success", "input_delivery_ledger")]

    # walk the exit exactly as returned
    reconciled = asyncio.run(_reconcile_data_dispatch(state, **dispatched["next_action"]["arguments"]))
    assert reconciled["status"] == "success", reconciled
    assert reconciled["receipt_event_recorded"] is True
    assert reconciled["already_returned"] is True
    rows = [a for a in census.reduce_execution_action_census(state)["actions"]
            if a["tool"] == "dispatch_data_request"]
    assert [(a["terminal_status"], a["receipt_event"]) for a in rows] == [
        ("success", "data_request_dispatched")]        # the event row now, not the ledger fallback
    assert calls["run_node"] == 1                       # no second child
    # idempotent: once the event exists the returned dispatch is not reconcilable again
    again = asyncio.run(_reconcile_data_dispatch(state, spec_id=spec["spec_id"]))
    assert again["status"] == "error"
    assert "not waiting for pause reconciliation" in again["error"]
    # and the delivery is still consumable
    delivered = asyncio.run(_verify_dataset_consumption(state, dataset_id, spec["spec_id"]))
    assert delivered["status"] == "success", delivered


def test_paused_dispatch_whose_event_was_not_durable_is_still_accounted(
    tmp_path: Path, monkeypatch,
):
    """Adversarial review (09-21): when the child PAUSED and the data_request_dispatched
    event failed to persist, reconcile cannot record anything until the child ends —
    the census was blind and a build closure could mint success. The committed
    delivery ledger is the authority: the dispatch is projected from it (unknown)."""
    runs = tmp_path / "runs"
    runs.mkdir()
    state = State.new("experiment", runs)
    _classified_operation(state)
    spec = asyncio.run(_validate_data_request_spec(state, _service_spec()))
    child_run_id = "data-paused-flaky-child"

    async def fake_run_node(**kwargs):
        child_run_dir = state.root.parent / child_run_id
        child_run_dir.mkdir(parents=True, exist_ok=True)
        (child_run_dir / "transcript.jsonl").write_text(json.dumps({
            "event": "run_start", "node_type": "data", "parent_run_id": state.run_id,
        }) + "\n", encoding="utf-8")
        return {"status": "pause", "pause_event": {"question": "May Data access the source?"},
                "child_run_id": child_run_id, "child_node_type": "data"}

    monkeypatch.setattr("shared.tools.run_node._run_node_tool", fake_run_node)
    real_append = state.append_transcript
    tripped = {"count": 0}

    def flaky_append(event: str, **fields):
        if event == "data_request_dispatched" and tripped["count"] == 0:
            tripped["count"] += 1
            raise OSError("simulated transcript write failure")
        return real_append(event, **fields)

    monkeypatch.setattr(state, "append_transcript", flaky_append)
    dispatched = asyncio.run(_dispatch_data_request(
        state, spec["spec_id"], user_note="Request the declared mesh package from Data."))
    assert dispatched["status"] == "error", dispatched
    assert dispatched["error_code"] == "dispatch_committed_but_receipt_event_not_durable"
    monkeypatch.setattr(state, "append_transcript", real_append)

    rows = [a for a in census.reduce_execution_action_census(state)["actions"]
            if a["tool"] == "dispatch_data_request"]
    assert [(a["terminal_status"], a["receipt_event"]) for a in rows] == [
        ("unknown", "input_delivery_ledger")]
    assert "accounted_action_terminal_unknown" in (
        census.operation_execution_obligation(state)["failure_reasons"])
    # the named exit is honest here: the child is still paused, nothing to record yet
    pending = asyncio.run(_reconcile_data_dispatch(state, spec_id=spec["spec_id"]))
    assert pending["status"] == "error"
    assert pending["error"] == "data_dispatch_paused_child_not_terminal"

"""S1d-R3a: v3 reserves the delivery triplet for the finish transaction."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from core.state import State
from nodes.experiment.tools import ledger
from nodes.experiment.tools import operation_completion as completion
from shared.tools import builtin


TRIO = ("raw_results", "clean_results", "experiment_log")


def _state(tmp_path: Path, *, ready: bool = True) -> State:
    state = State.new(
        node_type="experiment",
        base_dir=tmp_path / "runs",
        project_id="project",
        session_id="owner",
    )
    opened = ledger.open_ledger(state, write=True, bootstrap=True)
    assert opened.read_only is False
    if ready:
        ledger.mark_bootstrap_ready(state)
    assert ledger.bootstrap_phase(state) == ("ready" if ready else "initializing")
    return state


def _files(state: State) -> dict[str, bytes]:
    root = Path(state.root)
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _assert_no_triplet(state: State) -> None:
    assert {
        artifact_type: state.list_artifacts(artifact_type, own_only=True)
        for artifact_type in TRIO
    } == {artifact_type: [] for artifact_type in TRIO}


@pytest.mark.parametrize("ready", [False, True], ids=["initializing", "ready"])
def test_retired_completion_refuses_before_every_write_or_freeze(
    tmp_path, monkeypatch, ready,
):
    state = _state(tmp_path, ready=ready)
    before = _files(state)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("retired completion crossed a persistence boundary")

    async def forbidden_freeze(*_args, **_kwargs):
        raise AssertionError("retired completion attempted to freeze an artifact")

    monkeypatch.setattr(state, "save_artifact", forbidden)
    monkeypatch.setattr(state, "mark_frozen", forbidden)
    monkeypatch.setattr(state, "append_transcript", forbidden)
    monkeypatch.setattr(completion, "_freeze_artifact", forbidden_freeze)

    result = asyncio.run(completion._record_operation_completion(
        state,
        task_kind="generic",
        objective="must use the v3 delivery transaction",
    ))

    assert result == {
        "status": "error",
        "error_code": "v3_finish_required",
        "error": (
            "record_operation_completion 已退出 v3 写入路径；"
            + completion._V3_RESERVED_DELIVERY_REASON
        ),
        "hint": completion._V3_FINISH_HINT,
        "v3_phase": "ready" if ready else "initializing",
        "side_effects": False,
        "retryable": False,
        "owner": "finish",
    }
    _assert_no_triplet(state)
    assert _files(state) == before


def test_public_generic_save_cannot_impersonate_private_delivery_writer(tmp_path):
    state = _state(tmp_path)
    before = _files(state)
    refusals = []

    for artifact_type in TRIO:
        result = asyncio.run(builtin._save_artifact(
            state,
            artifact_type=artifact_type,
            name=f"forged_{artifact_type}",
            content="forged",
            metadata={
                "record_kind": "experiment_delivery",
                "delivery_owner": "finish",
                "artifact_role": artifact_type,
            },
        ))
        assert result["status"] == "error"
        assert result["failed_checks"] == ["closure_owner"]
        refusals.append({key: result[key] for key in ("reasons", "hint")})

    assert refusals[1:] == refusals[:1] * 2
    wording = json.dumps(refusals[0], ensure_ascii=False).lower()
    assert "finish" in wording and "delivery transaction" in wording
    for legacy_scope_word in (
        "operation", "scientific", "classify_experiment_scope",
        "record_operation_completion",
    ):
        assert legacy_scope_word not in wording
    _assert_no_triplet(state)
    assert _files(state) == before


def test_foreign_or_unreadable_protocol_never_falls_back_to_retired_writer(tmp_path):
    state = State.new(
        node_type="experiment", base_dir=tmp_path / "runs",
        project_id="project", session_id="owner",
    )
    ledger_root = Path(state.root) / "ledger"
    ledger_root.mkdir(parents=True)
    (ledger_root / "ledger.meta.json").write_text(json.dumps({
        "run_id": state.run_id,
        "protocol_epoch": "future",
        "schema_version": "future",
    }), encoding="utf-8")
    before = _files(state)

    result = asyncio.run(completion._record_operation_completion(
        state, task_kind="generic", objective="must not use a foreign ledger",
    ))

    assert result["status"] == "error"
    assert result["error_code"] == "delivery_protocol_unreadable"
    assert result["v3_phase"] == "foreign"
    assert result["side_effects"] is False
    assert result["owner"] == "experiment_runtime"
    _assert_no_triplet(state)
    assert _files(state) == before


def test_pre_v3_save_gate_keeps_existing_compatibility(monkeypatch, tmp_path):
    state = State.new(
        node_type="experiment",
        base_dir=tmp_path / "runs",
        project_id="project",
        session_id="owner",
    )
    assert completion._v3_protocol_phase(state) == "none"

    monkeypatch.setattr(completion, "_current_execution_mode", lambda _state: "scientific")
    for artifact_type in TRIO:
        assert completion._operation_closure_artifact_save_gate(
            state, {"type": artifact_type},
        ) == {}

    monkeypatch.setattr(completion, "_current_execution_mode", lambda _state: "operational")
    refusal = completion._operation_closure_artifact_save_gate(
        state, {"type": "raw_results"},
    )
    assert "record_operation_completion" in json.dumps(refusal, ensure_ascii=False)
    _assert_no_triplet(state)

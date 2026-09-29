"""P0a v5: fixes for the 2026-09-21 adversarial review (three lenses, each finding
independently reproduced) that belong to the census / ROC / submit_job layers.
The receipt-shape findings (glob members, symlink target, directory entries,
external upper bound) are fixed by 035a's shared evaluator and pinned there.

* json_paths never reached the producer gate (P2): the same fake refused via
  artifact_paths closed green via json_paths;
* submit_job's census-failure fallback named an unregistered model tool (P2);
* the refusal text offered "close as file_delivery / generic" inside a
  toolchain_build run, where that exit is itself refused (P3).
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from nodes.experiment.tests.test_p0a_v4_producer_receipt import (
    _pending_state, _route_backed_attempt, _write_product,
)
from nodes.experiment.tools import operation_completion
from nodes.experiment.tools import resource_manager as manager


def _roc_build_json(state, product: Path) -> dict:
    return asyncio.run(operation_completion._record_operation_completion(
        state, task_kind="build", objective="build the assigned tool", outcome="success",
        checks=[{"name": "manifest", "passed": True, "evidence": {"path": str(product)}}],
        json_paths=[str(product)]))


def test_json_paths_are_attributed_like_artifact_paths(tmp_path):
    state, run_root = _pending_state(tmp_path / "rewritten")
    manifest = run_root / "out" / "manifest.json"
    _route_backed_attempt(
        state, run_root, produce=lambda: _write_product(manifest, '{"built": true}\n'),
        expected_outputs=["out/manifest.json"])
    manifest.write_text('{"built": "forged"}\n')                     # rewritten after the attempt
    refused = _roc_build_json(state, manifest)
    assert refused["status"] == "error", refused
    assert refused["error_code"] == "operation_build_artifact_not_produced_by_satisfying_attempt"
    assert refused["unattributed_paths"][0]["reason"] == "content_changed_since_attempt"

    state, run_root = _pending_state(tmp_path / "unproduced")
    _route_backed_attempt(state, run_root)
    stray = run_root / "out" / "manifest.json"
    _write_product(stray, '{"built": true}\n')                      # never produced by an attempt
    refused = _roc_build_json(state, stray)
    assert refused["error_code"] == "operation_build_artifact_not_produced_by_satisfying_attempt"
    assert refused["unattributed_paths"][0]["reason"] == "no_producer_receipt"

    state, run_root = _pending_state(tmp_path / "ok")
    manifest = run_root / "out" / "manifest.json"
    _route_backed_attempt(
        state, run_root, produce=lambda: _write_product(manifest, '{"built": true}\n'),
        expected_outputs=["out/manifest.json"])
    assert _roc_build_json(state, manifest)["status"] == "success"


def test_refusal_exit_does_not_offer_a_task_kind_the_run_cannot_close_with(tmp_path):
    state, run_root = _pending_state(tmp_path)                       # category toolchain_build
    _route_backed_attempt(state, run_root)
    fake = run_root / "bin" / "solver"
    _write_product(fake)
    refused = asyncio.run(operation_completion._record_operation_completion(
        state, task_kind="build", objective="build", outcome="success",
        checks=[{"name": "binary_present", "passed": True, "evidence": {"path": str(fake)}}],
        artifact_paths=[str(fake)]))
    assert refused["error_code"] == "operation_build_artifact_not_produced_by_satisfying_attempt"
    assert "file_delivery" not in refused["next_action"]
    assert "只兼容 build/external_job" in refused["next_action"]
    # and the exit it does name is real: blocked closes
    blocked = asyncio.run(operation_completion._record_operation_completion(
        state, task_kind="build", objective="build", outcome="blocked",
        checks=[{"name": "binary_present", "passed": False, "evidence": {"path": str(fake)}}],
        next_step="rebuild through the declared route step and retry the closure"))
    assert blocked["status"] == "success", blocked


def test_submit_job_census_failure_fallback_exit_is_runtime_owned():
    response = manager._submission_action_census_failure(
        {"status": "success", "scheduler": "slurm", "job_id": "31415"},
        {"spawn": {"status": "error", "error_code": "execution_action_spawn_observation_conflict"}},
        {"action_id": "action-" + "a" * 32},
        dry_run=False,
    )
    assert response["status"] == "submitted_needs_recovery"
    next_action = response["blocker"]["next_action"]
    assert next_action["owner"] == "experiment_runtime"
    assert next_action["action"] == "retry_missing_census_phase_only"
    assert next_action["model_callable"] is False
    assert response["model_next_action"]["action"] == "report_blocker_and_end_current_run"
    from nodes.experiment.tools import safe_bash
    assert next_action["action"] not in safe_bash._REGISTRY.tools or next_action["model_callable"] is False

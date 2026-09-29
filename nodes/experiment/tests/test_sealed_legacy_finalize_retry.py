"""034 review: a sealed legacy empty-clear run must stay idempotent under a finalize retry.

beffabbf wrote the historical success finalization *pinned* to the empty contract,
exactly like the pinned verification it projected.  ``_seal_historical_legacy_empty_clear``
appends that finalization without the pins, so the suite never exercises the real legacy shape.
"""
from __future__ import annotations

import json
from pathlib import Path

from test_external_route_projection import _legacy_external_empty_clear_state

from nodes.experiment.tools.execution_route import (
    build_route_snapshot,
    record_external_route_finalization,
)

_IDENTITY_KEYS = (
    "scheduler", "job_id", "namespace", "launch_host", "scheduler_cluster",
    "resource_uid", "submission_nonce", "process_group_id",
    "process_start_ticks", "container_runtime_id",
)


def _events(state) -> list[dict]:
    return [
        json.loads(line)
        for line in state.transcript_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _pin_historical_finalization_like_beffabbf(state) -> dict:
    events = _events(state)
    pinned = next(
        event for event in events
        if event.get("event") == "route_step_external_execution_verified"
        and event.get("supersession_basis_validated") is True
    )
    finalization = next(
        event for event in events
        if event.get("event") == "route_step_external_finalized"
    )
    finalization.update({
        "verified_output_specs": [],
        "verified_outputs": [],
        "supersession_basis_validated": True,
        "supersession_basis_expected_outputs": [],
        "supersession_step_execution_contract_hash": pinned[
            "supersession_step_execution_contract_hash"],
    })
    state.transcript_path.write_text(
        "\n".join(json.dumps(event, ensure_ascii=False) for event in events) + "\n",
        encoding="utf-8",
    )
    return finalization


def test_sealed_legacy_empty_clear_survives_a_finalization_retry(tmp_path: Path):
    state, reference = _legacy_external_empty_clear_state(
        tmp_path, pinned_success=True, historically_finalized=True)
    finalization = _pin_historical_finalization_like_beffabbf(state)
    assert build_route_snapshot(state)["route_state"] == "complete"
    finalizations_before = [
        event for event in _events(state)
        if event.get("event") == "route_step_external_finalized"
    ]

    retry = record_external_route_finalization(
        state,
        **{key: reference.get(key) for key in _IDENTITY_KEYS},
        domain_outcome="operation_completed",
        evidence_artifact_id=finalization["evidence_artifact_id"],
    )

    # beffabbf: {"status": "success", "already_projected": True}, nothing appended.
    assert retry.get("status") == "success", retry
    assert retry.get("already_projected") is True, retry
    finalizations_after = [
        event for event in _events(state)
        if event.get("event") == "route_step_external_finalized"
    ]
    assert finalizations_after == finalizations_before
    snapshot = build_route_snapshot(state)
    assert snapshot["route_state"] == "complete"
    assert snapshot["steps"]["run"]["state"] == "verified"

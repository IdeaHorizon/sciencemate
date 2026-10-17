"""Exact output identity and scientific producer-lineage regressions."""
from __future__ import annotations

import asyncio
import os
import shlex
import sys
from pathlib import Path

from core.state import State
from nodes.experiment.tools import (
    action_boundary,
    capture,
    delivery_transaction,
    finish,
    ledger,
    run_bootstrap,
    safe_bash,
)


OUTPUT_RELATIVE = "outputs/experiment/runtime/result.dat"
EXPECTED_BYTES = b"49\n"


def _state(tmp_path: Path) -> tuple[State, Path]:
    state = State.new("experiment", tmp_path / "runs")
    prereg = state.save_artifact(
        "pre_registration",
        "square",
        "square value=7",
        metadata={
            "execution_contract": {
                "version": 1,
                "scientific_params": {"value": 7},
                "runtime_params": {},
            },
        },
    )
    state.mark_frozen(prereg["id"], {"freeze_reason": "identity lineage fixture"})
    record = state.read_artifact(prereg["id"])
    state.hook_state["node_inputs"] = {
        "experiment_spec": "Measure the square of the preregistered scalar.",
        "prereg_assignment": {
            "kind": "bound",
            "artifact_id": prereg["id"],
            "version": record["version"],
            "content_hash": record["content_hash"],
        },
        "required_outputs": [
            {"id": "measurement", "kind": "file", "path": str(state.root / OUTPUT_RELATIVE)},
        ],
    }
    established = asyncio.run(run_bootstrap.establish(state))
    assert ledger.bootstrap_phase(state) == "ready", established
    return state, Path(state.root) / OUTPUT_RELATIVE


def _params(value: int) -> dict:
    return {
        "scientific_params": {"value": value},
        "runtime_params": {},
    }


def _square(state: State, target: Path, value: int) -> str:
    program = f"value = {value}; print(value * value)"
    result = asyncio.run(
        safe_bash._safe_run_bash(
            state,
            cmd=(
                f"{shlex.quote(sys.executable)} -c {shlex.quote(program)} "
                f"> {shlex.quote(str(target))}"
            ),
            timeout=20,
            execution_params=_params(value),
        )
    )
    assert result["status"] == "success", result
    starts = [
        row
        for row in ledger.open_ledger(state).records()
        if row.get("kind") == "action_started"
        and row.get("tool") == "safe_run_bash"
    ]
    return starts[-1]["action_id"]


def _force_new_mtime(path: Path, previous_mtime_ns: int) -> None:
    current = path.stat()
    os.utime(
        path,
        ns=(
            current.st_atime_ns,
            max(current.st_mtime_ns, previous_mtime_ns + 1_000_000_000),
        ),
    )


def test_same_content_rewrite_records_and_reads_back_new_physical_identity(
    tmp_path: Path,
) -> None:
    path = tmp_path / "same.dat"
    path.write_bytes(EXPECTED_BYTES)
    before = capture.snapshot([path])
    before_row = before["entries"][str(path)]

    path.write_bytes(EXPECTED_BYTES)
    _force_new_mtime(path, before_row["mtime_ns"])
    after = capture.snapshot([path])
    after_row = after["entries"][str(path)]
    difference = capture.diff(before, after)

    assert str(path) in difference["modified"]
    assert (
        difference["identity_basis"][str(path)]
        == "size+inode+mtime+sha256"
    )
    assert capture.same_identity(before_row, after_row)[0] is False
    assert capture.same_identity(after_row, capture.identity(path))[0] is True


def test_matching_then_mismatching_same_result_preserves_the_modifier_conflict(
    tmp_path: Path,
) -> None:
    state, target = _state(tmp_path)
    matching = _square(state, target, 7)
    mismatching = _square(state, target, -7)
    assert target.read_bytes() == EXPECTED_BYTES

    records = ledger.open_ledger(state).records()
    row = finish._file_evidence(
        state,
        records,
        {"path": str(target), "non_empty": True},
    )
    assert row["passed"] is True
    # Same object and content cannot prove a new content producer. The
    # original source and the later physical modifier are both relevant;
    # neither the old match nor the new timestamp can erase the conflict.
    assert row["evidence"]["producers"] == [matching]
    assert row["evidence"]["identity_adjustments"] == [mismatching]

    result = asyncio.run(
        finish._finish(
            state,
            outcome="success",
            summary="The current same-valued result reports conflicting parameters.",
        )
    )
    assert result["status"] == "success_unsupported", result
    assert any(
        finding.get("name") == "producer_parameter_claim_conflict"
        and finding.get("action") == mismatching
        for finding in result["contradictions"]
    )
    assert delivery_transaction.read_manifest(state) is None


def test_unobserved_same_content_metadata_change_cannot_reuse_old_hash(
    tmp_path: Path,
) -> None:
    state, target = _state(tmp_path)
    producer = _square(state, target, 7)
    before = target.stat()

    _force_new_mtime(target, before.st_mtime_ns)
    assert target.read_bytes() == EXPECTED_BYTES

    row = finish._file_evidence(
        state,
        ledger.open_ledger(state).records(),
        {"path": str(target), "non_empty": True},
    )
    assert row["passed"] is False
    assert row["gap"] == "identity_changed_since_the_action"
    assert row["evidence"]["producers"] == [producer]


def test_single_sided_hash_never_falls_back_to_stat_identity() -> None:
    hashed = {
        "kind": "file",
        "size": 3,
        "inode": 11,
        "mtime_ns": 17,
        "sha256": "a" * 64,
    }
    unhashed = {key: value for key, value in hashed.items() if key != "sha256"}

    assert capture.same_identity(hashed, unhashed) == (
        False,
        "hash_unavailable",
    )
    assert capture.same_identity(unhashed, hashed) == (
        False,
        "hash_unavailable",
    )


def test_overlapping_exact_identity_writers_remain_visible_but_cannot_claim_provenance(
    tmp_path: Path,
) -> None:
    state, target = _state(tmp_path)
    first = action_boundary.begin(
        state,
        tool="safe_run_bash",
        effect_class=action_boundary.EXPERIMENT,
        command="first overlapping writer",
        work_root=str(target.parent),
        execution_params=_params(7),
    )
    second = action_boundary.begin(
        state,
        tool="safe_run_bash",
        effect_class=action_boundary.EXPERIMENT,
        command="second overlapping writer",
        work_root=str(target.parent),
        execution_params=_params(-7),
    )
    assert isinstance(first, action_boundary.ActionHandle)
    assert isinstance(second, action_boundary.ActionHandle)

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(EXPECTED_BYTES)
    first_write_mtime = target.stat().st_mtime_ns
    target.write_bytes(EXPECTED_BYTES)
    _force_new_mtime(target, first_write_mtime)
    assert action_boundary.settle(
        state,
        first,
        outcome="settled",
        exit_code=0,
    )["status"] == "recorded"
    assert action_boundary.settle(
        state,
        second,
        outcome="settled",
        exit_code=0,
    )["status"] == "recorded"

    records = ledger.open_ledger(state).records()
    row = finish._file_evidence(
        state,
        records,
        {"path": str(target), "non_empty": True},
    )
    # The former expectation attributed both writers just because both end
    # snapshots saw the same identity. Overlapping observation windows cannot
    # establish which writer produced the bytes; neither may claim certainty.
    assert row["passed"] is False
    assert row["gap"] == "unattributed"
    assert row["evidence"]["producers"] == []
    ids = {first.action_id, second.action_id}
    assert {r["action_id"] for r in records if r["kind"] == "action_started"} == ids
    observed = [r for r in records if r["kind"] == "observation" and r["action_id"] in ids]
    assert len(observed) == 2
    assert all(str(target) in r["unknown"] for r in observed)
    assert all(r["overlapping_actions"][str(target)] for r in observed)

    result = asyncio.run(finish._finish(
        state, outcome="success", summary="The overlapping writers cannot establish a unique output origin.",
    ))
    assert result["status"] == "success_unsupported", result
    assert any(gap.get("gap") == "unattributed" for gap in result["gaps"]), result
    assert delivery_transaction.read_manifest(state) is None


def test_matching_metadata_touch_cannot_supersede_mismatching_content_producer(
    tmp_path: Path,
) -> None:
    state, target = _state(tmp_path)
    mismatching = _square(state, target, -7)
    original = target.read_bytes()
    stat = target.stat()
    new_mtime = stat.st_mtime_ns + 1_000_000_000
    program = (
        "import os; "
        f"os.utime({str(target)!r}, ns=({stat.st_atime_ns}, {new_mtime}))"
    )
    touched = asyncio.run(
        safe_bash._safe_run_bash(
            state,
            cmd=f"{shlex.quote(sys.executable)} -c {shlex.quote(program)}",
            timeout=20,
            execution_params=_params(7),
        )
    )
    assert touched["status"] == "success", touched
    assert target.read_bytes() == original

    result = asyncio.run(
        finish._finish(
            state,
            outcome="success",
            summary="A metadata-only action did not recompute the scientific result.",
        )
    )
    assert result["status"] == "success_unsupported", result
    assert any(
        finding.get("name") == "producer_parameter_claim_conflict"
        and finding.get("action") == mismatching
        for finding in result["contradictions"]
    )

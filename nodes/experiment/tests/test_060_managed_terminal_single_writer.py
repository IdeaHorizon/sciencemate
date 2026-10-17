"""The canonical managed-job settlement is the sole ``job_terminal`` writer."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from core.state import State
from nodes.experiment.tools import job_settlement, ledger
from nodes.experiment.tools import resource_manager as rm


def _managed_job(tmp_path: Path, *, job_id: str = "731") -> tuple[State, dict, Path]:
    state = State.new(
        node_type="experiment",
        base_dir=tmp_path / "runs",
        project_id="p",
        session_id="owner",
    )
    owner = ledger.open_ledger(state, write=True, bootstrap=True)
    ledger.mark_bootstrap_ready(state)

    output_root = tmp_path / "managed-output"
    output_root.mkdir()
    product = output_root / "result.dat"
    nonce = f"nonce-{job_id}"
    record = {
        "status": "success",
        "dry_run": False,
        "scheduler": "slurm",
        "job_id": job_id,
        "submission_nonce": nonce,
        "submitted_at": datetime.now(timezone.utc).isoformat(),
        "output_root": str(output_root),
        "output_roots": [str(output_root)],
        "health_contract": {
            "progress_paths": [],
            "completion_paths": [str(product)],
            "completion_kinds": {str(product): "file"},
            "error_patterns": [],
            "stall_after_s": 60,
        },
    }
    state.save_artifact(
        "job_submission",
        f"job_submission_{job_id}",
        json.dumps(record, ensure_ascii=False),
        metadata={"scheduler": "slurm", "dry_run": False},
    )
    owner.append("intent", nonce=nonce, scheduler="slurm", output_root=str(output_root),
                 output_roots=[str(output_root)])
    owner.append("submission_attempted", nonce=nonce, scheduler="slurm",
                 output_baseline=job_settlement.capture_submission_outputs(state, nonce))
    product.write_text("measured\n", encoding="utf-8")
    owner.append("job_bound", nonce=nonce, scheduler="slurm", external_id=job_id)
    return state, record, product


def _terminal_rows(state: State) -> list[dict]:
    return [row for row in ledger.open_ledger(state).records() if row.get("kind") == "job_terminal"]


def _transcript_events(state: State, event: str) -> list[dict]:
    if not state.transcript_path.exists():
        return []
    return [
        row
        for line in state.transcript_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
        for row in [json.loads(line)]
        if row.get("event") == event
    ]


def _terminal_health(product: Path) -> dict:
    return {
        "status": "success",
        "scheduler": "slurm",
        "job_id": "731",
        "scheduler_phase": "terminal",
        "terminal_evidence": {
            "source": "slurm_accounting",
            "allocation_state": "COMPLETED",
            "exit_code": 0,
        },
        "completion_paths": [rm._health_file_snapshot(str(product))],
        "scheduler_result": {
            "status": "success",
            "raw": {"ok": True, "stdout": "", "stderr": "", "returncode": 0},
        },
    }


def _install_slurm_probe(monkeypatch, *, job_id: str, trusted_terminal: bool, queue_stdout: str = "") -> None:
    def fake_run(argv, timeout=10):
        if argv[0] == "squeue":
            return {"ok": True, "stdout": queue_stdout, "stderr": "", "returncode": 0}
        if argv[0] == "sacct":
            if not trusted_terminal:
                return {
                    "ok": False,
                    "stdout": "",
                    "stderr": "accounting temporarily unavailable",
                    "returncode": 1,
                }
            return {
                "ok": True,
                "stdout": f"{job_id}|COMPLETED|0:0|main|node1|1|1G|2|\n",
                "stderr": "",
                "returncode": 0,
            }
        raise AssertionError(f"unexpected scheduler command: {argv}")

    monkeypatch.setattr(rm, "_run", fake_run)


def _assert_one_complete_terminal_with_output(state: State, record: dict, product: Path) -> None:
    rows = _terminal_rows(state)
    assert len(rows) == 1, rows
    row = rows[0]
    assert row.get("derived_state") == "COMPLETED"
    assert row.get("exit_code") == 0
    assert str(product) in (row.get("output_files") or [])
    assert row.get("output_observation")
    projected = ledger.project_jobs(ledger.open_ledger(state).records())[record["submission_nonce"]]
    assert projected["terminal"]["derived_state"] == "COMPLETED"
    assert projected["terminal"]["exit_code"] == 0


def test_legacy_completion_snapshot_cannot_preempt_canonical_terminal_settlement(tmp_path):
    state, record, product = _managed_job(tmp_path)
    frozen = rm.freeze_first_terminal_output_observation(
        state, record, _terminal_health(product),
    )
    settled = job_settlement.settle_terminal(
        state,
        record,
        evidence={"allocation_state": "COMPLETED", "exit_code": 0},
        scheduler_result=frozen["scheduler_result"],
        observed_by="regression",
    )

    assert frozen["terminal_output_observation"]["source"] != "unfrozen"
    assert settled.get("settled") is True and settled.get("already") is False, settled
    _assert_one_complete_terminal_with_output(state, record, product)
    assert len(_transcript_events(state, "external_job_terminal_output_observation")) == 1


@pytest.mark.parametrize("first_entry", ["health", "status"])
def test_public_terminal_entry_order_keeps_one_canonical_writer(
    tmp_path, monkeypatch, first_entry: str,
):
    state, record, product = _managed_job(tmp_path)
    _install_slurm_probe(monkeypatch, job_id=record["job_id"], trusted_terminal=True)

    if first_entry == "health":
        health = asyncio.run(rm._check_external_job_health(state, "slurm", record["job_id"]))
        initial = health
    else:
        initial = asyncio.run(rm._job_status(state, "slurm", record["job_id"]))
        health = asyncio.run(rm._check_external_job_health(state, "slurm", record["job_id"]))
    assert (initial.get("terminal_settlement") or {}).get("settled") is True, initial

    first_frozen = rm.freeze_first_terminal_output_observation(state, record, health)
    second_frozen = rm.freeze_first_terminal_output_observation(state, record, health)
    assert first_frozen["terminal_output_observation"]["source"] != "unfrozen"
    assert second_frozen["terminal_output_observation"]["source"] == "frozen_first_terminal"

    if first_entry == "health":
        repeated = asyncio.run(rm._job_status(state, "slurm", record["job_id"]))
    else:
        repeated = asyncio.run(rm._check_external_job_health(state, "slurm", record["job_id"]))
    assert (repeated.get("terminal_settlement") or {}).get("already") is True, repeated
    _assert_one_complete_terminal_with_output(state, record, product)
    assert len(_transcript_events(state, "external_job_terminal_output_observation")) == 1


@pytest.mark.parametrize(
    ("queue_stdout", "job_id"),
    [
        ("901|PENDING|0:00|(Priority)", "901"),
        ("", "902"),
    ],
    ids=["pending", "disappeared_without_accounting"],
)
def test_nonterminal_or_untrusted_absence_never_closes_the_job(
    tmp_path, monkeypatch, queue_stdout: str, job_id: str,
):
    state, record, _product = _managed_job(tmp_path, job_id=job_id)
    _install_slurm_probe(
        monkeypatch,
        job_id=job_id,
        trusted_terminal=False,
        queue_stdout=queue_stdout,
    )

    health = asyncio.run(rm._check_external_job_health(state, "slurm", job_id))
    rm.freeze_first_terminal_output_observation(state, record, health)
    asyncio.run(rm._job_status(state, "slurm", job_id))

    assert _terminal_rows(state) == []
    jobs = ledger.open_jobs(ledger.open_ledger(state).records())
    assert [job["nonce"] for job in jobs] == [record["submission_nonce"]]

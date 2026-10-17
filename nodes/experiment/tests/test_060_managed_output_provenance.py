"""Managed output ownership is the submit-intent baseline diff, not after-only existence."""
from __future__ import annotations

import copy
import hashlib
import os
from pathlib import Path
from typing import Callable

from core.state import State
from nodes.experiment.tools import job_settlement, ledger
from nodes.experiment.tools import resource_manager as rm


def _v3_state(tmp_path: Path) -> State:
    state = State.new(
        node_type="experiment",
        base_dir=tmp_path / "runs",
        project_id="p",
        session_id="owner",
    )
    ledger.open_ledger(state, write=True, bootstrap=True)
    ledger.mark_bootstrap_ready(state)
    return state


def _submit_slurm(
    state: State,
    tmp_path: Path,
    output_root: Path,
    monkeypatch,
    *,
    payload: Callable[[], None],
) -> dict:
    def fake_run(argv, timeout=20):
        assert argv[0] == "sbatch", argv
        payload()
        return {"ok": True, "stdout": "731", "stderr": "", "returncode": 0}

    monkeypatch.setattr(rm, "_run", fake_run)
    submitted = rm._submit_sync(
        runtime_root=tmp_path / "runtime",
        scheduler="slurm",
        command="./produce-results",
        job_name="provenance",
        mpi_ranks=1,
        cpus_per_rank=1,
        gpus=0,
        memory_gb=1.0,
        storage_gb=1.0,
        walltime_minutes=5,
        queue=None,
        nodelist=None,
        image=None,
        workdir=str(output_root),
        output_dir=str(output_root),
        dry_run=False,
        namespace=None,
        output_paths=[str(output_root)],
        stage_in=None,
        state=state,
        submission_nonce="managed-output-provenance",
    )
    assert submitted.get("job_id") == "731", submitted
    rows = ledger.open_ledger(state).records()
    assert [row["kind"] for row in rows if row["kind"] in {"intent", "job_bound"}] == [
        "intent",
        "job_bound",
    ]
    return submitted


def _settle(state: State, submitted: dict) -> tuple[dict, dict]:
    result = job_settlement.settle_terminal(
        state,
        submitted,
        evidence={"allocation_state": "COMPLETED", "exit_code": 0},
        scheduler_result={
            "raw": {
                "ok": True,
                "stdout": "731|COMPLETED|0:0|main|node1|1|1G|2|\n",
                "stderr": "",
                "returncode": 0,
            },
        },
        observed_by="managed-output-provenance-test",
    )
    assert result.get("settled") is True and result.get("already") is False, result
    terminal = result["record"]
    observation_id = terminal.get("output_observation")
    assert observation_id
    projected = ledger.project_output_observations(ledger.open_ledger(state).records())
    observation = next(
        row
        for row in projected
        if row.get("action_id") == observation_id
    )
    assert observation.get("source_kind") == "job_terminal"
    assert observation.get("nonce") == submitted["submission_nonce"]
    return terminal, observation


def test_preexisting_file_is_not_attributed_when_the_job_creates_a_sibling(
    tmp_path, monkeypatch,
):
    state = _v3_state(tmp_path)
    output_root = tmp_path / "outputs"
    output_root.mkdir()
    old = output_root / "preexisting.dat"
    old.write_text("belongs to an earlier producer\n", encoding="utf-8")

    new = output_root / "new-result.dat"
    submitted = _submit_slurm(
        state,
        tmp_path,
        output_root,
        monkeypatch,
        payload=lambda: new.write_text("produced by this job\n", encoding="utf-8"),
    )
    terminal, observation = _settle(state, submitted)

    assert set(terminal.get("output_files") or []) == {str(new)}
    assert set(observation.get("created") or []) == {str(new)}
    assert observation.get("modified") == []
    assert str(old) not in set(observation.get("created") or [])


def test_preexisting_file_with_changed_content_is_attributed_as_modified(
    tmp_path, monkeypatch,
):
    state = _v3_state(tmp_path)
    output_root = tmp_path / "outputs"
    output_root.mkdir()
    result_path = output_root / "result.dat"
    result_path.write_text("old content\n", encoding="utf-8")

    submitted = _submit_slurm(
        state,
        tmp_path,
        output_root,
        monkeypatch,
        payload=lambda: result_path.write_text(
            "new scientific content with a different size\n", encoding="utf-8"
        ),
    )
    terminal, observation = _settle(state, submitted)

    assert set(terminal.get("output_files") or []) == {str(result_path)}
    assert observation.get("created") == []
    assert set(observation.get("modified") or []) == {str(result_path)}


def test_same_content_touch_keeps_before_and_after_identity_in_the_window(
    tmp_path, monkeypatch,
):
    state = _v3_state(tmp_path)
    output_root = tmp_path / "outputs"
    output_root.mkdir()
    result_path = output_root / "result.dat"
    result_path.write_text("unchanged bytes\n", encoding="utf-8")
    before_stat = result_path.stat()

    def touch_payload() -> None:
        os.utime(
            result_path,
            ns=(before_stat.st_atime_ns, before_stat.st_mtime_ns + 1_000_000_000),
        )

    submitted = _submit_slurm(
        state, tmp_path, output_root, monkeypatch, payload=touch_payload,
    )
    _terminal, observation = _settle(state, submitted)

    assert observation.get("created") == []
    assert set(observation.get("modified") or []) == {str(result_path)}
    identity = observation["identities"][str(result_path)]
    assert identity.get("before_unknown") is not True
    assert identity["before"]["sha256"] == identity["after"]["sha256"]
    assert observation.get("basis") == "submission_to_terminal_observation_window"


def test_truncated_prelaunch_baseline_does_not_guess_a_later_file_was_created(
    tmp_path, monkeypatch,
):
    state = _v3_state(tmp_path)
    output_root = tmp_path / "outputs"
    output_root.mkdir()
    (output_root / "a-existing.dat").write_text("a\n", encoding="utf-8")
    (output_root / "b-existing.dat").write_text("b\n", encoding="utf-8")
    later = output_root / "z-later.dat"

    monkeypatch.setattr(job_settlement, "_MAX_OBSERVED_FILES", 1)
    submitted = _submit_slurm(
        state,
        tmp_path,
        output_root,
        monkeypatch,
        payload=lambda: later.write_text("appeared in the observation window\n", encoding="utf-8"),
    )
    marker = next(
        row
        for row in ledger.open_ledger(state).records()
        if row.get("kind") == "submission_attempted"
    )
    assert marker["output_baseline"]["snapshot"]["truncated"] is True

    monkeypatch.setattr(job_settlement, "_MAX_OBSERVED_FILES", 500)
    terminal, observation = _settle(state, submitted)

    assert observation.get("status") == "partial"
    assert observation.get("truncated") is True
    assert str(later) in set(observation.get("unknown") or [])
    assert str(later) not in set(observation.get("created") or [])
    assert str(later) not in set(observation.get("modified") or [])
    assert str(later) not in set(terminal.get("output_files") or [])


def test_terminal_append_ack_loss_reuses_the_committed_output_manifest(
    tmp_path, monkeypatch,
):
    state = _v3_state(tmp_path)
    output_root = tmp_path / "outputs"
    output_root.mkdir()
    result_path = output_root / "result.dat"
    first_bytes = b"first terminal output\n"
    submitted = _submit_slurm(
        state,
        tmp_path,
        output_root,
        monkeypatch,
        payload=lambda: result_path.write_bytes(first_bytes),
    )

    real_append_once = ledger.Ledger.append_once
    ack_lost = {"done": False}

    def append_then_lose_ack(self, kind, idempotency_key, /, **fields):
        row = real_append_once(self, kind, idempotency_key, **fields)
        if kind == "job_terminal" and not ack_lost["done"]:
            ack_lost["done"] = True
            raise OSError("injected terminal ACK loss after durable append")
        return row

    monkeypatch.setattr(ledger.Ledger, "append_once", append_then_lose_ack)
    first = job_settlement.settle_terminal(
        state,
        submitted,
        evidence={"allocation_state": "COMPLETED", "exit_code": 0},
        scheduler_result={"raw": {"stdout": "731|COMPLETED|0:0|\n"}},
        observed_by="managed-output-provenance-test",
    )
    assert first.get("settled") is False, first
    assert first.get("reason") == "ledger_write_failed", first
    assert ack_lost["done"] is True

    rows_after_lost_ack = ledger.open_ledger(state).records()
    terminals = [
        row for row in rows_after_lost_ack if row.get("kind") == "job_terminal"
    ]
    assert len(terminals) == 1
    committed = copy.deepcopy(terminals[0])
    committed_identity = committed["output_diff"]["identities"][str(result_path)]
    assert committed_identity["after"]["sha256"] == hashlib.sha256(first_bytes).hexdigest()

    result_path.write_text("content written after the lost ACK\n", encoding="utf-8")
    later = output_root / "later.dat"
    later.write_text("not part of the committed terminal window\n", encoding="utf-8")

    def fail_if_resnapshotted(*_args, **_kwargs):
        raise AssertionError("a committed job_terminal must not be re-snapshotted")

    monkeypatch.setattr(job_settlement, "_output_diff", fail_if_resnapshotted)
    replay = job_settlement.settle_terminal(
        state,
        submitted,
        evidence={"allocation_state": "COMPLETED", "exit_code": 0},
        scheduler_result={"raw": {"stdout": "731|COMPLETED|0:0|\n"}},
        observed_by="managed-output-provenance-test-retry",
    )

    assert replay.get("settled") is True, replay
    assert replay.get("already") is True, replay
    assert replay.get("record") == committed
    replay_rows = ledger.open_ledger(state).records()
    assert [row for row in replay_rows if row.get("kind") == "job_terminal"] == [
        committed
    ]
    assert not any(
        row.get("kind") in {"action_started", "observation", "action_settled"}
        for row in replay_rows
    )
    projected = ledger.project_output_observations(replay_rows)
    assert len(projected) == 1
    assert set(projected[0].get("created") or []) == {str(result_path)}
    assert str(later) not in projected[0].get("identities", {})
    assert replay["record"]["output_diff"] == committed["output_diff"]


def test_failed_job_keeps_its_nonzero_terminal_without_a_fake_local_action(
    tmp_path, monkeypatch,
):
    state = _v3_state(tmp_path)
    output_root = tmp_path / "outputs"
    output_root.mkdir()
    partial = output_root / "partial.dat"
    submitted = _submit_slurm(
        state,
        tmp_path,
        output_root,
        monkeypatch,
        payload=lambda: partial.write_text(
            "partial output before the managed job failed\n", encoding="utf-8"
        ),
    )

    result = job_settlement.settle_terminal(
        state,
        submitted,
        evidence={"allocation_state": "FAILED", "exit_code": 23},
        scheduler_result={
            "raw": {
                "ok": True,
                "stdout": "731|FAILED|23:0|main|node1|1|1G|2|\n",
                "stderr": "managed payload failed",
                "returncode": 0,
            },
        },
        observed_by="managed-output-provenance-test",
    )
    assert result.get("settled") is True and result.get("already") is False, result
    terminal = result["record"]
    assert terminal.get("derived_state") == "FAILED"
    assert terminal.get("exit_code") == 23

    rows = ledger.open_ledger(state).records()
    assert len([row for row in rows if row.get("kind") == "job_terminal"]) == 1
    assert not any(
        row.get("kind") in {"action_started", "observation", "action_settled"}
        for row in rows
    )
    assert ledger.project_actions(rows) == []
    job = ledger.project_jobs(rows)[submitted["submission_nonce"]]
    assert job["terminal"]["derived_state"] == "FAILED"
    assert job["terminal"]["exit_code"] == 23
    projected = ledger.project_output_observations(rows)
    assert len(projected) == 1
    assert projected[0].get("source_kind") == "job_terminal"
    assert set(projected[0].get("created") or []) == {str(partial)}

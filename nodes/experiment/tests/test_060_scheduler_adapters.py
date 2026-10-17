"""060 S1b：adapter 契约与"查不了不等于没有"（governing §3 第 2 层、§4 I1）。"""
from __future__ import annotations

from pathlib import Path

import pytest

from core.state import State
from nodes.experiment.tools import ledger, scheduler_adapters as sa


def _state(tmp_path: Path) -> State:
    state = State.new(node_type="experiment", base_dir=tmp_path / "runs", project_id="p", session_id="owner")
    state.hook_state["node_inputs"] = {"requested_work": "adapter"}
    ledger.open_ledger(state, write=True, bootstrap=True)
    ledger.mark_bootstrap_ready(state)
    return state


def test_remote_schedulers_never_claim_a_physical_write_wall(tmp_path):
    for name in ("slurm", "pbs", "torque"):
        caps = sa.capabilities_for(name)
        assert caps.remote_write_isolation == sa.ISOLATION_MISSING, name
        assert any("沙箱" in n or "越界" in n for n in caps.notes), name
    assert sa.capabilities_for("local").remote_write_isolation == sa.ISOLATION_ENFORCED


def test_pbs_declares_it_cannot_look_a_job_up_by_our_correlation_key(tmp_path):
    caps = sa.capabilities_for("pbs")
    assert caps.reconnect_by_correlation_key is False
    probe = sa.adapter_for("pbs").reconnect("abc123")
    assert probe["status"] == "cannot_query"
    assert "不要据此判断作业不存在" in probe["reason"]


def test_a_lost_ack_the_adapter_cannot_query_stays_unknown_and_forbids_resubmit(tmp_path):
    state = _state(tmp_path)
    out = sa.resolve_lost_ack(state, scheduler="pbs", correlation_key="nonce-1")
    assert out["decision"] == "unknown" and out["resubmit_allowed"] is False
    rows = ledger.open_ledger(state).records()
    assert [r["kind"] for r in rows] == ["job_unknown"]
    jobs = ledger.project_jobs(rows)
    assert jobs["nonce-1"]["state"] == "unknown"
    assert ledger.open_jobs(rows), "unknown 作业留在开放列表里等接管"


def _slurm_caps(**over):
    base = dict(scheduler="slurm", submit=True, query_status=True, cancel=True, terminal_facts=True,
                reconnect_by_correlation_key=True, remote_write_isolation=sa.ISOLATION_MISSING)
    base.update(over)
    return sa.AdapterCapabilities(**base)


def _fake_run(monkeypatch, *, squeue="", squeue_ok=True, sacct="", sacct_ok=True):
    def run(argv, timeout=20):
        if argv and argv[0] == "squeue":
            return {"ok": squeue_ok, "stdout": squeue, "stderr": "" if squeue_ok else "squeue: error", "returncode": 0 if squeue_ok else 1}
        return {"ok": sacct_ok, "stdout": sacct, "stderr": "" if sacct_ok else "sacct: error", "returncode": 0 if sacct_ok else 1}
    monkeypatch.setattr(sa, "_run", run)
    monkeypatch.setattr(sa.SlurmAdapter, "capabilities", lambda self: _slurm_caps())


def test_a_queued_job_is_found_by_its_correlation_key_without_any_accounting_setting(tmp_path, monkeypatch):
    """主路径是 squeue 的 %k：实测（Docker Slurm 23.11.4）它不依赖站点 accounting 配置。"""
    state = _state(tmp_path)
    _fake_run(monkeypatch,
              squeue="4242|hello|ai4s:nonce-2|RUNNING\n4243|other|ai4s:someone-else|PENDING",
              sacct="")
    out = sa.resolve_lost_ack(state, scheduler="slurm", correlation_key="nonce-2")
    assert out["decision"] == "adopted" and out["external_id"] == "4242"
    assert out["resubmit_allowed"] is False
    assert [j["job_id"] for j in out["jobs"]] == ["4242"]
    assert ledger.open_ledger(state).records()[-1]["kind"] == "job_reconciled"


def test_a_finished_job_is_found_through_accounting_and_step_rows_do_not_double_count(tmp_path, monkeypatch):
    state = _state(tmp_path)
    _fake_run(monkeypatch, squeue="",
              sacct="4242|hello|ai4s:nonce-2|COMPLETED|0:0\n4242.batch|batch|ai4s:nonce-2|COMPLETED|0:0")
    out = sa.resolve_lost_ack(state, scheduler="slurm", correlation_key="nonce-2")
    assert out["decision"] == "adopted" and [j["job_id"] for j in out["jobs"]] == ["4242"]


def test_an_empty_accounting_comment_column_is_inconclusive_not_an_absence(tmp_path, monkeypatch):
    """实测的坑：站点没设 AccountingStoreFlags=job_comment 时 sacct 的 Comment 整列为空。

    2026-09-23 在 Docker Slurm 23.11.4 上，带 --comment 提交的作业 sacct 查回来
    Comment 为空；只按 sacct 判会得出"没这个作业"，于是重复提交。空列必须判成
    查不了，而不是不存在。
    """
    state = _state(tmp_path)
    _fake_run(monkeypatch, squeue="", sacct="4242|hello||COMPLETED|0:0\n4243|other||FAILED|1:0")
    out = sa.resolve_lost_ack(state, scheduler="slurm", correlation_key="nonce-3")
    assert out["decision"] == "unknown" and out["resubmit_allowed"] is False
    assert "AccountingStoreFlags" in out["probe"]["reason"]
    assert ledger.open_ledger(state).records()[-1]["kind"] == "job_unknown"


def test_only_a_conclusive_absence_allows_a_resubmit(tmp_path, monkeypatch):
    state = _state(tmp_path)
    # accounting 的 Comment 列有内容（别的作业带 key），说明站点开了 flag：这时查不到才算没有
    _fake_run(monkeypatch, squeue="", sacct="4242|hello|ai4s:another-run|COMPLETED|0:0")
    out = sa.resolve_lost_ack(state, scheduler="slurm", correlation_key="nonce-4")
    assert out["decision"] == "absent" and out["resubmit_allowed"] is True
    assert ledger.open_ledger(state).records()[-1]["kind"] == "job_absent"


def test_a_failing_squeue_is_cannot_query_not_absent(tmp_path, monkeypatch):
    state = _state(tmp_path)
    _fake_run(monkeypatch, squeue_ok=False)
    out = sa.resolve_lost_ack(state, scheduler="slurm", correlation_key="nonce-5")
    assert out["decision"] == "unknown" and out["resubmit_allowed"] is False
    assert ledger.open_ledger(state).records()[-1]["kind"] == "job_unknown"

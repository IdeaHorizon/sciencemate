from __future__ import annotations

import json

from nodes.experiment.tools import resource_manager as rm
from nodes.experiment.tools.diagnose import DiagnoseEngine
from nodes.experiment.tools.mpi_runtime import mpi_runtime_remediation


UCX_HCOLL_LOG = """[node01:12345] Failed to receive UCX worker address
HCOLL ERROR: failed to initialize collective component
"""


def test_ucx_hcoll_failure_returns_a_single_openmpi_tcp_retry_recipe():
    remediation = mpi_runtime_remediation("mpirun -np 64 pw.x -in scf.in", UCX_HCOLL_LOG)

    assert remediation is not None
    assert remediation["status"] == "retry_recipe_ready"
    assert remediation["retry_limit"] == 1
    assert remediation["retry_command"] == (
        "mpirun --mca pml ob1 --mca btl tcp,self --mca coll_hcoll_enable 0 "
        "-np 64 pw.x -in scf.in"
    )
    assert "人工确认" in remediation["submission_policy"]


def test_ucx_only_without_openmpi_evidence_requires_manual_launcher_check():
    remediation = mpi_runtime_remediation("mpirun -np 64 pw.x -in scf.in", "UCX transport failed")

    assert remediation is not None
    assert remediation["status"] == "manual_retry_required"
    assert "retry_command" not in remediation


def test_diagnose_engine_labels_ucx_hcoll_as_mpi_runtime_failure():
    findings = DiagnoseEngine().analyze_output(UCX_HCOLL_LOG)["findings"]

    finding = next(item for item in findings if item["category"] == "mpi")
    assert finding["severity"] == "critical"
    assert any("coll_hcoll_enable" in item for item in finding["suggestions"])


def test_ucx_hcoll_retry_never_overwrites_existing_mca_settings():
    remediation = mpi_runtime_remediation(
        "mpirun --mca pml ucx -np 64 pw.x -in scf.in", UCX_HCOLL_LOG)

    assert remediation is not None
    assert remediation["status"] == "already_configured_or_conflicting"
    assert "retry_command" not in remediation


def test_health_check_exposes_mpi_remediation_from_managed_job_log(tmp_path, monkeypatch):
    log_path = tmp_path / "job.err"
    log_path.write_text(UCX_HCOLL_LOG, encoding="utf-8")
    submission = {
        "status": "success", "dry_run": False, "scheduler": "local",
        "job_id": "123", "command": "mpirun -np 64 pw.x -in scf.in",
        "output_roots": [str(tmp_path)], "scheduler_output_dir": str(tmp_path),
        "stderr_path": str(log_path), "health_contract": {},
    }

    class State:
        def list_artifacts(self, artifact_type=None):
            return ([{"id": "submission", "type": "job_submission"}]
                    if artifact_type == "job_submission" else [])

        def read_artifact(self, _artifact_id):
            return {"content": json.dumps(submission)}

    monkeypatch.setattr(rm, "_job_status_sync", lambda *_args, **_kwargs: {
        "status": "success", "raw": {"ok": True, "stdout": "NOT_RUNNING"},
    })
    health = rm.probe_external_job_health(State(), "local", "123")

    assert health["health_state"] == "failure_signal"
    assert health["mpi_runtime_remediation"]["status"] == "retry_recipe_ready"
    assert "--mca btl tcp,self" in health["mpi_runtime_remediation"]["retry_command"]

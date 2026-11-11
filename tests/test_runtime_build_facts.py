"""Worker and transcript facts identify execution code, never a later checkout."""

import hashlib
import json
from pathlib import Path

from test_platform_runtime import _read_event, _runtime_process, _send_rpc

from core.state import State


def test_worker_and_transcript_publish_the_same_execution_source(tmp_path):
    process = _runtime_process()
    try:
        _send_rpc(process, {"op": "status", "request_id": "build-status"})
        status = _read_event(process, "status")
        build = status["runtime_build"]
        assert len(build["sourceSha256"]) == 64
        assert (
            build["components"]["core/job_wait.py"]
            == hashlib.sha256(
                (Path(__file__).resolve().parents[1] / "core/job_wait.py").read_bytes()
            ).hexdigest()
        )
        assert build["codeVersion"]
        state = State.new("_orchestrator", tmp_path / "runs")
        state.append_transcript("tool_call", name="remote_job", args={"action": "wait"})
        raw = json.loads(state.transcript_path.read_text())
        assert raw["runtime_build"]["sourceSha256"] == build["sourceSha256"]
    finally:
        process.terminate()
        process.communicate(timeout=10)


def test_relocated_distribution_worker_reports_its_payload_build(tmp_path, monkeypatch):
    import os
    import subprocess
    import sys

    from scripts.package import build_server_bundle

    interface = tmp_path / "interface"
    interface.mkdir()
    (interface / "index.html").write_text("offline packaging fixture")
    monkeypatch.setattr(build_server_bundle, "DIST", tmp_path / "distribution")
    root = build_server_bundle.stage("0.5.13", interface) / "harness"
    process = subprocess.Popen(
        [sys.executable, "-m", "platform_runtime", "--serve"],
        cwd=root,
        env={**os.environ, "PYTHONPATH": str(root)},
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    try:
        _send_rpc(process, {"op": "status", "request_id": "packaged-build"})
        build = _read_event(process, "status")["runtime_build"]
        assert build["codeVersion"] == "0.5.13"
        assert (
            build["components"]["core/job_wait.py"]
            == hashlib.sha256((root / "core/job_wait.py").read_bytes()).hexdigest()
        )
    finally:
        process.terminate()
        process.communicate(timeout=10)

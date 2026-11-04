"""Real detached processes: replay, reconnect, cancellation, logs and transfers."""

import base64
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4

import pytest

AGENT = Path(__file__).resolve().parents[1] / "core" / "_ssh_agent.py.in"
pytestmark = pytest.mark.skipif(os.name != "posix", reason="Supervisor runs on the POSIX server")


@pytest.fixture
def remote(tmp_path):
    def call(action, **values):
        request = dict(action=action, project_id="test-project", resource_id="resource-1", **values)
        if action in ("submit", "exec"):
            request.setdefault("job_id", uuid4().hex)
            request["agent_source"] = AGENT.read_text()
        response = subprocess.run(
            [sys.executable, "-I", str(AGENT)],
            input=json.dumps(request),
            text=True,
            capture_output=True,
            env={**os.environ, "HOME": str(tmp_path)},
            timeout=15,
        )
        assert response.returncode == 0, response.stderr
        return json.loads(response.stdout)

    return call


def terminal(remote, job_id, timeout=10):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        job = remote("status", job_id=job_id)
        if job.get("state") not in ("launching", "running"):
            return job
        time.sleep(0.05)
    raise AssertionError("Job never reached a terminal state")


def test_reconnect_and_replay_do_not_duplicate_a_job(remote):
    key = uuid4().hex
    command = "echo one >> count.txt; sleep .3; printf done > result.txt; echo finished"
    first = remote("submit", command=command, job_id=key)
    assert first["job_id"] == key
    repeated = remote("submit", command=command, job_id=key)
    assert repeated["request_digest"] == first["request_digest"]
    final = terminal(remote, key)
    assert final["state"] == "succeeded" and final["exit_code"] == 0
    assert "finished" in final["stdout"]
    assert base64.b64decode(remote("get", path="count.txt")["content_base64"]) == b"one\n"
    assert base64.b64decode(remote("get", path="result.txt")["content_base64"]) == b"done"
    assert len(remote("jobs")["jobs"]) == 1
    conflict = remote("submit", command="echo different", job_id=key)
    assert "different command" in conflict["error"]


def test_cancel_and_timeout_terminate_managed_jobs(remote):
    job = remote("submit", command="echo started; sleep 60")
    cancelled = remote("cancel", job_id=job["job_id"])
    assert cancelled["state"] == "cancelled"
    assert terminal(remote, job["job_id"])["state"] == "cancelled"
    failed = remote("exec", command="echo failure >&2; exit 7")
    assert failed["state"] == "failed" and failed["exit_code"] == 7
    assert "failure" in failed["stderr"]
    timed_out = remote("exec", command="sleep 30", timeout=0.1)
    assert timed_out["state"] == "timed_out"


def test_binary_transfer_checksums_and_bounded_logs(remote):
    content = bytes(range(256)) * 4
    put = remote("put", path="a folder/file.bin", content_base64=base64.b64encode(content).decode())
    get = remote("get", path=put["path"])
    assert get["sha256"] == put["sha256"]
    assert base64.b64decode(get["content_base64"]) == content
    result = remote("exec", command="python3 -c \"print('x' * 3000000)\"")
    assert result["state"] == "succeeded"
    assert result["logs_truncated"] is True
    assert len(result["stdout"]) <= 32768
    assert Path(result["stdout_path"]).stat().st_size <= 2 * 1024 * 1024


def test_status_rejects_path_traversal_and_invalid_requests(remote):
    assert "Invalid job ID" in remote("status", job_id="../other-project")["error"]
    assert "Timeout" in remote("exec", command="echo no", timeout=-1)["error"]

import asyncio
import json
import subprocess
import sys

import pytest

from core.ssh_transport import SSHConnection, SSHError, SSHTransport


def test_transport_preserves_unknown_job_state_and_keeps_password_out_of_argv(monkeypatch):
    original = subprocess.Popen
    launches = []

    def launch(argv, **kwargs):
        launches.append((argv, kwargs["env"]))
        code = "import json; print(json.dumps({'state':'lost','error':'Supervisor disappeared'}))"
        return original([sys.executable, "-I", "-c", code], **kwargs)

    monkeypatch.setattr(subprocess, "Popen", launch)
    password = "password-for-test"
    result = SSHTransport(SSHConnection("node15"), password).call({"action": "status"})
    assert result["state"] == "lost"
    argv, env = launches[0]
    assert password not in json.dumps(argv)
    assert env["HF_SSH_PASSWORD"] == password
    assert "StrictHostKeyChecking=accept-new" in argv


@pytest.mark.parametrize("host", ["-oProxyCommand=evil", "host; echo bad", "user@host/path"])
def test_connection_rejects_option_and_shell_injection(host):
    with pytest.raises(ValueError):
        SSHConnection(host)


def test_transport_rejects_invalid_response_shape(monkeypatch):
    original = subprocess.Popen
    monkeypatch.setattr(
        subprocess,
        "Popen",
        lambda argv, **kwargs: original([sys.executable, "-I", "-c", 'print("null")'], **kwargs),
    )
    with pytest.raises(SSHError, match="response shape"):
        SSHTransport(SSHConnection("node15")).call({"action": "probe"})


@pytest.mark.asyncio
async def test_remote_job_status_accepts_the_job_id_from_the_receipt_only(tmp_path, monkeypatch):
    from core import remote_resources, tool_registry
    from core.bootstrap import bootstrap
    from core.state import State

    bootstrap()
    state = State.new(node_type="_orchestrator", project_id="test-project", base_dir=tmp_path)
    response_ready = asyncio.Event()
    ssh_job_id = "a" * 32
    calls = []

    async def handler(_state, action, params):
        calls.append(action)
        if action == "submit":
            await response_ready.wait()
        return {"status": "success", "job_id": ssh_job_id, "state": "running"}

    monkeypatch.setattr(remote_resources, "_handler", handler)
    execute = tool_registry.execute
    submission = asyncio.create_task(execute(
        "remote_job", state, action="submit", resource_id="test-resource",
        command="exit 0", idempotency_key="test-key", check_after_seconds=0.01,
    ))
    await asyncio.sleep(0.03)
    assert not submission.done(), "A handback tick must not fabricate another job identity"
    response_ready.set()
    receipt = await asyncio.wait_for(submission, timeout=1)
    assert receipt["job_id"] == ssh_job_id
    rejected = await execute(
        "remote_job", state, action="status", resource_id="test-resource", job_id="remote_job#1",
    )
    assert rejected["status"] == "error" and calls == ["submit"]
    result = await execute(
        "remote_job", state, action="status", resource_id="test-resource", job_id=ssh_job_id,
    )
    assert result["job_id"] == ssh_job_id
    assert calls == ["submit", "status"]

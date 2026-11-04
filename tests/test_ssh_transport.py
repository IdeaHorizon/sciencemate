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

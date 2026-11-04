"""API, ownership, encrypted credentials and Harness share the same service."""

import json
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException

from app.auth import get_current_user
from app.config import settings
from app.main import app
from app.models.project import Project
from app.models.user import User
from app.services import ssh_resources


@pytest.fixture
def local_ssh(tmp_path, monkeypatch):
    if os.name != "posix":
        pytest.skip("The remote supervisor requires a POSIX server")
    from core import ssh_transport

    remote_home = tmp_path / "remote"
    remote_home.mkdir()
    agent = Path(ssh_transport.__file__).with_name("_ssh_agent.py.in")

    class LocalTransport:
        def __init__(self, connection, password=None):
            self.password = password

        def call(self, request, timeout=40):
            request = dict(request)
            if request["action"] == "submit":
                request["agent_source"] = agent.read_text()
            result = subprocess.run(
                [sys.executable, "-I", str(agent)],
                input=json.dumps(request),
                text=True,
                capture_output=True,
                timeout=timeout,
                env={**os.environ, "HOME": str(remote_home)},
            )
            assert result.returncode == 0, result.stderr
            data = json.loads(result.stdout)
            if "error" in data:
                raise ssh_transport.SSHError(data["error"])
            return data

    monkeypatch.setattr(ssh_transport, "SSHTransport", LocalTransport)
    key = Fernet.generate_key()
    monkeypatch.setattr("app.services.credential_key.master_key", lambda: key)


async def identities(db):
    user = User(
        id=str(uuid4()),
        email=uuid4().hex + "@example.com",
        hashed_password="unused",
        display_name="SSH test",
    )
    project = Project(id=str(uuid4()), name="SSH test", owner_id=user.id)
    db.add_all([user, project])
    await db.commit()
    return user, project


@pytest.mark.asyncio
async def test_api_connect_exec_transfer_jobs_and_bridge(client, db_session, local_ssh):
    user, project = await identities(db_session)
    app.dependency_overrides[get_current_user] = lambda: user
    prefix = "/api/v1/projects/" + project.id + "/resources"
    secret = "short-password-42"
    connected = await client.post(
        prefix + "/ssh/connect", json=dict(host="test-host", password=secret)
    )
    assert connected.status_code == 200, connected.text
    resource = connected.json()["resource"]
    resource_id = resource["id"]
    assert secret not in connected.text and resource["has_secret_reference"]
    vault = ssh_resources._vault_path(resource_id)
    assert secret.encode() not in vault.read_bytes()
    assert vault.stat().st_mode & 0o077 == 0
    reconnected = await client.post(
        prefix + "/ssh/connect", json=dict(host="test-host", password=secret)
    )
    assert reconnected.status_code == 200, reconnected.text
    assert reconnected.json()["resource"]["id"] == resource_id
    listing = await client.get(prefix)
    assert listing.json()[0]["id"] == resource_id
    endpoint = prefix + "/" + resource_id + "/ssh"
    executed = await client.post(endpoint, json=dict(action="exec", command="printf hello"))
    assert executed.status_code == 200, executed.text
    assert executed.json()["stdout"] == "hello"
    failed = await client.post(endpoint, json=dict(action="exec", command="exit 9"))
    assert failed.json()["status"] == "error" and failed.json()["exit_code"] == 9
    submission = await client.post(
        endpoint, json=dict(action="submit", command="sleep 30", idempotency_key="one")
    )
    job_id = submission.json()["job_id"]
    replay = await client.post(
        endpoint, json=dict(action="submit", command="sleep 30", idempotency_key="one")
    )
    assert replay.json()["job_id"] == job_id
    from core import remote_resources
    from core.state import State

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(remote_resources, "_handler", None)
    try:
        context = {
            key: getattr(settings, key)
            for key in (
                "database_url",
                "platform_data_root",
                "runtime_tenant_id",
                "profile",
                "secret_key",
            )
        }
        ssh_resources.install_worker_bridge(context, user_id=user.id, project_id=project.id)
        # The real worker opens another DB session, so publish the registration.
        await db_session.commit()
        state = State.new(
            node_type="_orchestrator",
            project_id=project.id,
            base_dir=Path(settings.platform_data_root) / "test-runs",
        )
        result = await remote_resources.call(
            state, "cancel", resource_id=resource_id, job_id=job_id
        )
        assert result["state"] == "cancelled"
        history = await remote_resources.call(state, "jobs", resource_id=resource_id)
        assert any(job["job_id"] == job_id for job in history["jobs"])
        text = state.transcript_path.read_text()
        assert resource_id in text and secret not in text
    finally:
        monkeypatch.undo()


@pytest.mark.asyncio
async def test_credentials_cannot_be_retargeted_or_inherited(db_session, local_ssh):
    user, project = await identities(db_session)
    connected = await ssh_resources.connect(
        db_session, user, project.id, host="original", password="a-secret"
    )
    rid = connected["resource"]["id"]
    from app.models.resource import ProjectResource

    resource = await db_session.get(ProjectResource, rid)
    other = User(
        id=str(uuid4()),
        email=uuid4().hex + "@example.com",
        hashed_password="x",
        display_name="Other",
    )
    with pytest.raises(HTTPException) as forbidden:
        ssh_resources._transport(resource, other)
    assert forbidden.value.status_code == 403
    resource.config = {**resource.config, "ssh": {"host": "attacker"}}
    with pytest.raises(HTTPException) as retarget:
        ssh_resources._transport(resource, user)
    assert retarget.value.status_code == 409
    resource.config = {**resource.config, "ssh": {"host": "original"}}
    resource.secret_ref = None
    with pytest.raises(HTTPException) as removed:
        ssh_resources._transport(resource, user)
    assert removed.value.status_code == 409


@pytest.mark.asyncio
async def test_disabled_resource_stops_new_work_but_allows_cleanup(db_session, local_ssh):
    user, project = await identities(db_session)
    connected = await ssh_resources.connect(db_session, user, project.id, host="test-host")
    rid = connected["resource"]["id"]
    job = await ssh_resources.operate(
        db_session, user, project.id, rid, "submit", command="sleep 30", idempotency_key="cancel"
    )
    from app.models.resource import ProjectResource

    resource = await db_session.get(ProjectResource, rid)
    resource.is_enabled = False
    with pytest.raises(HTTPException) as disabled:
        await ssh_resources.operate(db_session, user, project.id, rid, "exec", command="echo no")
    assert disabled.value.status_code == 409
    cancelled = await ssh_resources.operate(
        db_session, user, project.id, rid, "cancel", job_id=job["job_id"]
    )
    assert cancelled["state"] == "cancelled"


def test_worker_socket_path_does_not_enter_app_settings(tmp_path):
    repo = Path(__file__).resolve().parents[3]
    context = {
        key: getattr(settings, key)
        for key in (
            "database_url",
            "platform_data_root",
            "runtime_tenant_id",
            "profile",
            "secret_key",
        )
    }
    code = """import os
from core.remote_resources import bind_worker,DELIVERY_ENV
bind_worker(user_id='user',project_id='project')
assert DELIVERY_ENV not in os.environ
assert os.environ['HARNESS_CONTROL_SOCKET'].endswith('.sock')
print('bound')
"""
    process = subprocess.run(
        [sys.executable, "-c", code],
        text=True,
        capture_output=True,
        cwd=tmp_path,
        env={
            **os.environ,
            "HARNESS_CONTROL_SOCKET": str(tmp_path / "worker.sock"),
            "HARNESS_RESOURCE_RUNTIME": json.dumps(context),
            "PYTHONPATH": os.pathsep.join((str(repo), str(repo / "platform/backend"))),
        },
    )
    assert process.returncode == 0, process.stderr
    assert process.stdout.strip() == "bound"

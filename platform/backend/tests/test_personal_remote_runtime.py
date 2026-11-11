"""Personal desktops connect over real SSH to a real, independently running backend."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import tarfile
from pathlib import Path
from uuid import uuid4

import asyncssh
import httpx
import pytest

from app.config import settings
from app.services import project_homes
from app.services import remote_servers as store
from app.services.other_homes import ThereIsNoSuchHomeError
from app.services.remote_relay import RuntimeAuthentication, destination
from app.services.remote_transport import HostKeyRequiredError, connect, provision, tunnels
from tests.test_local_runtime_api import _headers, _token, runtime_client  # noqa: F401

REPO = Path(__file__).resolve().parents[3]
TOKEN = "test-runtime-device-token"
PASSWORD = "test-ssh-password"


@pytest.fixture(autouse=True)
def private_test_credentials(monkeypatch):
    from cryptography.fernet import Fernet

    from app.services import credential_key

    key = Fernet.generate_key()
    monkeypatch.setattr(credential_key, "master_key", lambda: key)
    monkeypatch.setattr(settings, "remote_runtime_token", "")


@pytest.fixture
async def ssh_server(tmp_path):
    key = asyncssh.generate_private_key("ssh-ed25519")
    client_key = asyncssh.generate_private_key("ssh-ed25519")
    private_key = tmp_path / "id_ed25519"
    private_key.write_bytes(client_key.export_private_key())
    commands = []

    class Server(asyncssh.SSHServer):
        def begin_auth(self, username):
            return True

        def password_auth_supported(self):
            return True

        def public_key_auth_supported(self):
            return True

        def validate_password(self, username, password):
            return username == "researcher" and password == PASSWORD

        def validate_public_key(self, username, public_key):
            return username == "researcher" and public_key == client_key.convert_to_public()

        def connection_requested(self, dest_host, dest_port, orig_host, orig_port):
            return dest_host == "127.0.0.1"

    async def execute(process):
        commands.append(process.command)
        proc = await asyncio.create_subprocess_shell(
            process.command,
            cwd=tmp_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate()
        process.stdout.write(out.decode())
        process.stderr.write(err.decode())
        process.exit(proc.returncode)

    server = await asyncssh.listen(
        "127.0.0.1",
        0,
        server_host_keys=[key],
        server_factory=Server,
        process_factory=execute,
        sftp_factory=asyncssh.SFTPServer,
    )
    record = {
        "id": store.PREFIX + str(uuid4()),
        "name": "Lab server",
        "host": "127.0.0.1",
        "port": server.sockets[0].getsockname()[1],
        "username": "researcher",
        "auth_method": "key",
        "identity_file": str(private_key),
        "fingerprint": key.get_fingerprint("sha256"),
        "runtime_port": 1,
        "version": "test",
        "service_manager": "systemd",
    }
    try:
        yield record, server, commands
    finally:
        await tunnels.close()
        server.close()
        await server.wait_closed()


@pytest.fixture(scope="module")
async def real_backend(tmp_path_factory):
    root = tmp_path_factory.mktemp("independent-remote-runtime")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    env = {
        **os.environ,
        "PLATFORM_PROFILE": "personal",
        "PLATFORM_DATA_ROOT": str(root / "data"),
        "HARNESS_FRAMEWORK_HOME": str(root / "data"),
        "REMOTE_RUNTIME_TOKEN": TOKEN,
        "HARNESS_ROOT": str(REPO),
        "HARNESS_BRIDGE_ENABLED": "false",
        "DATABASE_URL": f"sqlite+aiosqlite:///{root}/data/db.sqlite",
        "STATIC_UI_ROOT": "",
    }
    log = (root / "backend.log").open("w")
    process = subprocess.Popen(
        [sys.executable, "-m", "app.launcher", "start", "--port", str(port), "--no-browser"],
        cwd=REPO / "platform/backend",
        env=env,
        stdout=log,
        stderr=log,
    )
    url = f"http://127.0.0.1:{port}"
    try:
        async with httpx.AsyncClient(trust_env=False) as client:
            for _ in range(200):
                try:
                    response = await client.get(
                        url + "/api/v1/remote-runtime", headers={"Authorization": f"Bearer {TOKEN}"}
                    )
                    if response.status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                if process.poll() is not None:
                    pytest.fail((root / "backend.log").read_text()[-6000:])
                await asyncio.sleep(0.1)
            else:
                pytest.fail((root / "backend.log").read_text()[-6000:])
        runtime = {"url": url, "port": port, "root": root, "process": process}

        async def restart():
            nonlocal process
            arguments = process.args
            process.terminate()
            await asyncio.to_thread(process.wait, 15)
            process = subprocess.Popen(
                arguments,
                cwd=REPO / "platform/backend",
                env=env,
                stdout=log,
                stderr=log,
            )
            runtime["process"] = process
            async with httpx.AsyncClient(trust_env=False) as client:
                for _ in range(200):
                    try:
                        response = await client.get(
                            url + "/api/v1/remote-runtime",
                            headers={"Authorization": f"Bearer {TOKEN}"},
                        )
                        if response.status_code == 200:
                            return
                    except httpx.HTTPError:
                        pass
                    if process.poll() is not None:
                        pytest.fail((root / "backend.log").read_text()[-6000:])
                    await asyncio.sleep(0.1)
            pytest.fail("Restarted backend did not become ready")

        runtime["restart"] = restart
        yield runtime
    finally:
        process.terminate()
        await asyncio.to_thread(process.wait, 15)
        log.close()


async def test_unknown_fingerprint_requires_confirmation_and_pins_the_accepted_key(ssh_server):
    record, _, _ = ssh_server
    unknown = {**record, "fingerprint": None}
    with pytest.raises(HostKeyRequiredError) as refused:
        await connect(unknown, {})
    assert refused.value.fingerprint == record["fingerprint"]
    connection, observed = await connect(record, {})
    assert observed == record["fingerprint"]
    connection.close()
    await connection.wait_closed()
    with pytest.raises(ThereIsNoSuchHomeError, match="指纹"):
        await connect({**record, "fingerprint": "SHA256:some-other-machine"}, {})


async def test_password_authentication_and_reconnection_require_a_password(ssh_server):
    record, _, _ = ssh_server
    password_record = {**record, "auth_method": "password", "identity_file": ""}
    with pytest.raises(ThereIsNoSuchHomeError) as expired:
        await connect(password_record, {})
    assert expired.value.status == 401
    connection, _ = await connect(password_record, {"password": PASSWORD})
    connection.close()
    await connection.wait_closed()


def test_credential_storage_is_private_and_metadata_never_contains_secrets(tmp_path):
    record = {"id": store.PREFIX + str(uuid4()), "name": "Private lab", "host": "lab"}
    secrets = {"api_token": TOKEN, "password": PASSWORD, "passphrase": "secret-phrase"}
    store.save(record, secrets, remember_secret=False)
    assert TOKEN not in json.dumps(store.all_servers())
    assert PASSWORD not in store._path(record["id"], ".enc").read_text()
    assert store._path(record["id"], ".enc").stat().st_mode & 0o777 == 0o600
    store._session_credentials.clear()
    assert store.credentials_for(record["id"]) == {"api_token": TOKEN}
    store.save(record, secrets, remember_secret=True)
    store._session_credentials.clear()
    assert store.credentials_for(record["id"]) == secrets


async def test_a_real_remote_project_uses_server_folders_and_survives_desktop_disconnect(
    ssh_server,
    real_backend,
    runtime_client,  # noqa: F811
):
    record, _, commands = ssh_server
    record["runtime_port"] = real_backend["port"]
    store.save(record, {"api_token": TOKEN}, remember_secret=False)
    client, _ = runtime_client
    token = await _token(client, "researcher@atrium.local")
    headers = _headers(token)
    listing = await client.get(
        "/api/v1/projects/folders", headers={**headers, "X-Remote-Server": record["id"]}
    )
    assert listing.status_code == 200, listing.text
    assert listing.json()["available"] is True
    chosen = real_backend["root"] / "My remote research"
    created = await client.post(
        "/api/v1/projects/",
        headers=headers,
        json={"name": "Long study", "home": record["id"], "root_folder": str(chosen)},
    )
    assert created.status_code == 201, created.text
    project = created.json()
    assert project["home"]["kind"] == "remote"
    assert project["root_folder"] == str(chosen)
    assert (chosen / "PROJECT.md").is_file()
    assert project_homes.where_this_project_lives(project["id"]) == record["id"]
    assert not (Path(settings.platform_data_root) / "repositories" / project["id"]).exists()
    info = await client.get(f"/api/v1/projects/{project['id']}", headers=headers)
    assert info.status_code == 200, info.text
    await tunnels.close()  # What closing the desktop actually does.
    assert real_backend["process"].poll() is None
    assert not commands  # A tunnel disconnect sends no remote stop or cancel commands.
    restarted_desktop = await client.get(f"/api/v1/projects/{project['id']}", headers=headers)
    assert restarted_desktop.status_code == 200
    assert restarted_desktop.json()["root_folder"] == str(chosen)


async def test_headless_runtime_requires_the_device_token(real_backend):
    async with httpx.AsyncClient(base_url=real_backend["url"], trust_env=False) as client:
        assert (await client.get("/api/v1/projects/")).status_code == 401
        assert (
            await client.get("/api/v1/remote-runtime", headers={"Authorization": "Bearer wrong"})
        ).status_code == 401
        correct = await client.get(
            "/api/v1/remote-runtime", headers={"Authorization": f"Bearer {TOKEN}"}
        )
        assert correct.status_code == 200
        assert correct.json()["personal"] is True


async def test_a_real_backend_restarts_with_a_session_in_the_chosen_remote_root(
    ssh_server,
    real_backend,
):
    record, _, _ = ssh_server
    record["runtime_port"] = real_backend["port"]
    store.save(record, {"api_token": TOKEN}, remember_secret=False)
    chosen = real_backend["root"] / "restartable-research"
    project = await store.create_project(
        record["id"], {"name": "Restart with a chosen folder", "root_folder": str(chosen)}, "en"
    )
    backend = await store.ask(
        record["id"],
        "POST",
        "/api/v1/settings/model-backends",
        json={"provider": "demo", "display_name": "Restart test", "model": "restart-test"},
    )
    session = await store.ask(
        record["id"],
        "POST",
        f"/api/v1/projects/{project['id']}/sessions",
        json={"title": "A durable session", "model_backend_id": backend["id"]},
    )
    previous_pid = real_backend["process"].pid
    await real_backend["restart"]()
    assert real_backend["process"].pid != previous_pid
    reopened = await store.ask(record["id"], "GET", f"/api/v1/projects/{project['id']}")
    sessions = await store.ask(record["id"], "GET", f"/api/v1/projects/{project['id']}/sessions")
    assert reopened["root_folder"] == str(chosen)
    assert any(item["id"] == session["id"] for item in sessions)


async def test_provisioning_uploads_and_executes_the_package_without_exposing_secrets(
    ssh_server, tmp_path, monkeypatch
):
    from app.services import remote_transport

    record, _, commands = ssh_server
    bundle = tmp_path / "runtime.tar.gz"
    installer = tmp_path / "install-runtime.sh"
    result = {
        "protocol": 1,
        "port": 18089,
        "token": TOKEN,
        "version": "test",
        "service_manager": "systemd",
    }
    import shlex

    installer.write_text(
        "#!/bin/sh\nprintf '%s\\n' "
        + shlex.quote("SCIENCEMATE_RUNTIME=" + json.dumps(result))
        + "\n"
    )
    with tarfile.open(bundle, "w:gz") as archive:
        archive.add(installer, arcname="install-runtime.sh")
    monkeypatch.setattr(remote_transport, "runtime_bundle", lambda: bundle)
    installed, secrets = await provision(record, {})
    assert installed["runtime_port"] == 18089
    assert "api_token" not in installed
    assert secrets["api_token"] == TOKEN
    assert TOKEN not in " ".join(commands)


def test_routing_never_reuses_a_local_root_or_leaks_local_settings_context():
    project_homes.this_one_lives_at("proj-remote", "remote:" + str(uuid4()))

    def scope(path, **headers):
        return {
            "path": path,
            "headers": [(key.encode(), value.encode()) for key, value in headers.items()],
        }

    assert destination(scope("/api/v1/projects/", **{"x-project": "proj-remote"})) == ""
    assert destination(scope("/api/v1/projects/folders", **{"x-project": "proj-remote"})) == ""
    assert destination(scope("/api/v1/remote-servers", **{"x-project": "proj-remote"})) == ""
    assert destination(scope("/api/v1/projects/proj-remote/sessions")).startswith("remote:")


async def test_offline_servers_keep_cached_projects_and_missing_connections_fail_closed(
    ssh_server, real_backend
):
    record, server, _ = ssh_server
    record["runtime_port"] = real_backend["port"]
    store.save(record, {"api_token": TOKEN}, remember_secret=False)
    created = await store.create_project(record["id"], {"name": "Offline research"}, "en")
    await tunnels.close()
    server.close()
    await server.wait_closed()
    projects = await store.list_projects()
    assert any(item["id"] == created["id"] and not item["home"]["reachable"] for item in projects)


async def test_runtime_authentication_does_not_protect_an_ordinary_desktop(monkeypatch):
    called = []

    async def downstream(scope, receive, send):
        called.append(True)

    wrapped = RuntimeAuthentication(downstream)
    await wrapped({"type": "http", "headers": []}, None, None)
    assert called == [True]


async def test_closing_an_observation_stream_releases_the_reader_but_keeps_ssh(ssh_server):
    from app.services.remote_relay import PersonalProjectsRelay

    record, _, commands = ssh_server
    closed = asyncio.Event()

    async def serve(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n\r\ndata: still-running\n\n"
        )
        await writer.drain()
        await reader.read()
        closed.set()
        writer.close()
        await writer.wait_closed()

    backend = await asyncio.start_server(serve, "127.0.0.1", 0)
    record["runtime_port"] = backend.sockets[0].getsockname()[1]
    store.save(record, {"api_token": TOKEN}, remember_secret=False)
    events = asyncio.Queue()
    await events.put({"type": "http.request", "body": b""})
    received = []

    async def send(message):
        received.append(message)
        if message["type"] == "http.response.body" and message.get("body"):
            await events.put({"type": "http.disconnect"})

    async def unused(scope, receive, send):
        pytest.fail("The remote project was answered locally")

    try:
        await asyncio.wait_for(
            PersonalProjectsRelay(unused)(
                {
                    "type": "http",
                    "method": "GET",
                    "path": "/api/v1/projects/observed/stream",
                    "headers": [(b"x-remote-server", record["id"].encode())],
                },
                events.get,
                send,
            ),
            3,
        )
        await asyncio.wait_for(closed.wait(), 1)
        assert tunnels.connected(record["id"])
        assert not commands
        assert any(b"still-running" in event.get("body", b"") for event in received)
    finally:
        backend.close()
        await backend.wait_closed()


async def test_connection_setup_confirms_the_host_checks_the_runtime_and_reuses_the_server(
    ssh_server,
    real_backend,
    runtime_client,  # noqa: F811
    tmp_path,
    monkeypatch,
):
    import shlex

    from app.services import remote_transport

    record, _, _ = ssh_server
    result = {
        "protocol": 1,
        "port": real_backend["port"],
        "token": TOKEN,
        "version": "test",
        "service_manager": "systemd",
    }
    installer = tmp_path / "install-runtime.sh"
    installer.write_text(
        "#!/bin/sh\nprintf '%s\\n' "
        + shlex.quote("SCIENCEMATE_RUNTIME=" + json.dumps(result))
        + "\n"
    )
    bundle = tmp_path / "runtime.tar.gz"
    with tarfile.open(bundle, "w:gz") as archive:
        archive.add(installer, arcname="install-runtime.sh")
    monkeypatch.setattr(remote_transport, "runtime_bundle", lambda: bundle)
    client, _ = runtime_client
    token = await _token(client, "researcher@atrium.local")
    headers = _headers(token)
    data = {
        key: record[key]
        for key in ("name", "host", "username", "port", "auth_method", "identity_file")
    }

    async def setup(payload):
        response = await client.post(
            "/api/v1/remote-servers/connect", headers=headers, json=payload
        )
        assert response.status_code == 202, response.text
        operation_id = response.json()["operation_id"]
        for _ in range(400):
            operation = await client.get(
                f"/api/v1/remote-servers/operations/{operation_id}", headers=headers
            )
            if operation.json()["done"]:
                return operation.json()
            await asyncio.sleep(0.02)
        pytest.fail("Runtime setup did not finish")

    unknown = await setup(data)
    assert unknown["phase"] == "host_key"
    assert store.all_servers() == []
    accepted = await setup({**data, "fingerprint": unknown["fingerprint"]})
    assert accepted["phase"] == "ready", accepted
    assert TOKEN not in json.dumps(accepted)
    again = await setup(data)
    assert again["phase"] == "ready", again
    assert again["server"]["id"] == accepted["server"]["id"]
    listed = await client.get("/api/v1/remote-servers", headers=headers)
    assert len(listed.json()["items"]) == 1
    assert TOKEN not in listed.text
    server_id = accepted["server"]["id"]
    disconnected = await client.post(
        f"/api/v1/remote-servers/{server_id}/disconnect", headers=headers
    )
    assert disconnected.status_code == 204
    with pytest.raises(ThereIsNoSuchHomeError) as disabled:
        await store.ask(server_id, "GET", "/api/v1/remote-runtime")
    assert disabled.value.status == 503
    assert real_backend["process"].poll() is None
    checked = await client.post(f"/api/v1/remote-servers/{server_id}/check", headers=headers)
    assert checked.status_code == 200, checked.text


async def test_availability_distinguishes_a_healthy_api_and_manual_disconnect(
    ssh_server,
    real_backend,
):
    from app.api.v1.remote_servers import server_availability

    record, _, commands = ssh_server
    record["runtime_port"] = real_backend["port"]
    store.save(record, {"api_token": TOKEN}, remember_secret=False)
    assert (await server_availability(record["id"], lang="en"))["state"] == "online"
    store.set_enabled(record["id"], False)
    assert (await server_availability(record["id"], lang="en"))["state"] == "disconnected"
    assert store.get(record["id"])["enabled"] is False
    assert not commands


async def test_an_api_which_accepts_tcp_but_never_answers_is_reported_unavailable(
    ssh_server,
    monkeypatch,
):
    from app.api.v1 import remote_servers as endpoint

    record, _, commands = ssh_server
    closed = asyncio.Event()

    async def stalled(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        await reader.read()
        writer.close()
        await writer.wait_closed()
        closed.set()

    backend = await asyncio.start_server(stalled, "127.0.0.1", 0)
    record["runtime_port"] = backend.sockets[0].getsockname()[1]
    store.save(record, {"api_token": TOKEN}, remember_secret=False)
    monkeypatch.setattr(endpoint, "AVAILABILITY_TIMEOUT_SECONDS", 0.15)
    try:
        status = await asyncio.wait_for(endpoint.server_availability(record["id"], lang="en"), 1)
        assert status["state"] == "unavailable"
        await asyncio.wait_for(closed.wait(), 1)
        assert tunnels.connected(record["id"])
        assert not commands
    finally:
        backend.close()
        await backend.wait_closed()


@pytest.mark.parametrize("sse", [False, True])
async def test_relay_bounds_silent_responses_but_allows_quiet_research_streams(
    ssh_server,
    monkeypatch,
    sse,
):
    from app.services import remote_relay

    record, _, commands = ssh_server

    async def serve(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        if sse:
            body = b"data: still-working\n\n"
            writer.write(
                b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\n"
                + f"Content-Length: {len(body)}\r\n\r\n".encode()
            )
            await writer.drain()
            await asyncio.sleep(0.15)
            writer.write(body)
            await writer.drain()
        else:
            await reader.read()
        writer.close()
        await writer.wait_closed()

    backend = await asyncio.start_server(serve, "127.0.0.1", 0)
    record["runtime_port"] = backend.sockets[0].getsockname()[1]
    store.save(record, {"api_token": TOKEN}, remember_secret=False)
    monkeypatch.setattr(remote_relay, "RESPONSE_HEADERS_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(remote_relay, "RESPONSE_BODY_TIMEOUT_SECONDS", 0.025)
    events = asyncio.Queue()
    await events.put({"type": "http.request", "body": b""})
    received = []

    async def send(message):
        received.append(message)

    async def unexpected(scope, receive, send):
        pytest.fail("Remote request fell through to the local backend")

    try:
        await asyncio.wait_for(
            remote_relay.PersonalProjectsRelay(unexpected)(
                {
                    "type": "http",
                    "method": "GET",
                    "path": "/api/v1/projects/example/stream",
                    "headers": [(b"x-remote-server", record["id"].encode())],
                },
                events.get,
                send,
            ),
            2,
        )
        assert received[0]["status"] == (200 if sse else 503)
        payload = b"".join(item.get("body", b"") for item in received)
        assert (b"still-working" in payload) == sse
        assert not commands
    finally:
        backend.close()
        await backend.wait_closed()

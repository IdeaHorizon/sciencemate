"""Opt-in real-host acceptance test. Uses an isolated, persistent App Server.

Run with the backend interpreter. Passwords are read from the terminal, never
from argv. No GPU workload is launched; the payload is a small stdlib CPU job.
"""

from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="node15")
    parser.add_argument("--user")
    parser.add_argument("--port", type=int)
    parser.add_argument("--jump")
    parser.add_argument("--password", action="store_true")
    parser.add_argument("--installed-runtime", action="store_true")
    parser.add_argument("--data-root", type=Path, required=True)
    args = parser.parse_args()
    password = getpass.getpass("SSH password: ") if args.password else None
    root = args.data_root.resolve()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    repository = Path(__file__).resolve().parents[1]
    backend_root = repository / "platform/backend"
    if args.installed_runtime:
        import app

        backend_root = Path(app.__file__).resolve().parent.parent
        repository = backend_root / "app/harness"
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = {
        **os.environ,
        "PLATFORM_PROFILE": "personal",
        "PLATFORM_DATA_ROOT": str(root),
        "HARNESS_FRAMEWORK_HOME": str(root),
        "HARNESS_ROOT": str(repository),
        "DATABASE_URL": "sqlite+aiosqlite:///" + str(root / "db.sqlite"),
        "HARNESS_BRIDGE_ENABLED": "false",
        "UPDATE_CHECK_ENABLED": "false",
        "PYTHONPATH": os.pathsep.join((str(repository), str(backend_root))),
    }
    log = (root / "server.log").open("ab")
    server = None
    client = httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=90, follow_redirects=True)
    report = dict(
        host=args.host,
        checks=[],
        python=sys.executable,
        harness_root=str(repository),
        backend_root=str(backend_root),
    )

    def start():
        nonlocal server
        server = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "app.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--log-level",
                "warning",
            ],
            env=env,
            cwd=backend_root,
            stdout=log,
            stderr=log,
        )
        until = time.monotonic() + 40
        while time.monotonic() < until:
            if server.poll() is not None:
                raise RuntimeError("Server startup failed; see " + str(root / "server.log"))
            try:
                if client.get("/health").status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.2)
        raise TimeoutError("App Server did not become ready")

    def stop():
        nonlocal server
        if server is not None and server.poll() is None:
            server.terminate()
            try:
                server.wait(timeout=15)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait()

    def request(method, path, data=None):
        response = client.request(method, "/api/v1" + path, json=data)
        if response.is_error:
            raise RuntimeError(f"{method} {path}: {response.text}")
        result = response.json()
        if isinstance(result, dict) and result.get("status") == "error":
            raise RuntimeError(str(result))
        return result

    def checked(label):
        report["checks"].append(label)
        print("PASS " + label, flush=True)

    try:
        start()
        user = request("GET", "/auth/me")
        project = request(
            "POST", "/projects/", {"name": "SSH acceptance " + time.strftime("%Y%m%d-%H%M%S")}
        )
        report["project_id"] = project["id"]
        prefix = "/projects/" + project["id"] + "/resources"
        # Exercise credential intake through the registered Agent tool as well
        # as the HTTP form. Secrets travel over stdin, never in argv or output.
        intake = """import asyncio,json,os,sys
from pathlib import Path
from app.config import settings
from core.remote_resources import DELIVERY_ENV,bind_worker
from core.state import State
from core.tool_registry import execute
import shared.tools.library.remote_resources
async def main():
    req=json.load(sys.stdin)
    keys=('database_url','platform_data_root','runtime_tenant_id','profile','secret_key')
    os.environ[DELIVERY_ENV]=json.dumps({key:getattr(settings,key) for key in keys})
    bind_worker(user_id=req['user_id'],project_id=req['project_id'])
    state=State.new(node_type='_orchestrator',project_id=req['project_id'],
                    base_dir=Path(req['runs']))
    result=await execute('resource',state,action='connect',**req['connection'])
    assert result['status']=='success',result
    print(json.dumps(result))
asyncio.run(main())
"""
        connected_tool = subprocess.run(
            [sys.executable, "-c", intake],
            input=json.dumps(
                dict(
                    user_id=user["id"],
                    project_id=project["id"],
                    runs=str(root / "intake-runs"),
                    connection=dict(
                        host=args.host,
                        user_name=args.user,
                        port=args.port,
                        jump=args.jump,
                        password=password,
                        name="node15",
                    ),
                )
            ),
            text=True,
            capture_output=True,
            env=env,
            cwd=repository,
            timeout=60,
        )
        assert connected_tool.returncode == 0, connected_tool.stderr
        intake_result = json.loads(connected_tool.stdout.splitlines()[-1])
        checked("Agent tool credential intake and automatic resource registration")
        connection = request(
            "POST",
            prefix + "/ssh/connect",
            dict(
                host=args.host,
                user_name=args.user,
                port=args.port,
                jump=args.jump,
                password=password,
                name="node15",
            ),
        )
        rid = connection["resource"]["id"]
        assert rid == intake_result["resource"]["id"]
        report.update(resource_id=rid, observation=connection["observation"])
        assert any(r["id"] == rid for r in request("GET", prefix))
        checked("SSH authentication, hardware probe and Resources registration")
        endpoint = prefix + "/" + rid + "/ssh"

        def remote(action, **values):
            return request("POST", endpoint, dict(action=action, **values))

        setup = remote(
            "exec",
            command="python3 -m venv --without-pip .venv && "
            ".venv/bin/python -c \"print('environment ready')\"",
            timeout=120,
        )
        assert "environment ready" in setup["stdout"]
        checked("Remote directory writes and Python environment creation")
        source = (
            'import json,time\nfrom pathlib import Path\nprint("started",flush=True)\n'
            'time.sleep(12)\nresult={"sum_squares":sum(i*i for i in range(10000))}\n'
            'Path("result.json").write_text(json.dumps(result))\nprint(json.dumps(result),flush=True)\n'
        )
        upload = remote(
            "put", path="smoke.py", content_base64=base64.b64encode(source.encode()).decode()
        )
        assert upload["sha256"] == hashlib.sha256(source.encode()).hexdigest()
        job = remote(
            "submit", command=".venv/bin/python smoke.py", timeout=60, idempotency_key="smoke-1"
        )
        replay = remote(
            "submit", command=".venv/bin/python smoke.py", timeout=60, idempotency_key="smoke-1"
        )
        assert replay["job_id"] == job["job_id"]
        report["job_id"] = job["job_id"]
        checked("File upload and idempotent detached submission")
        stop()
        start()
        assert any(r["id"] == rid for r in request("GET", prefix))
        until = time.monotonic() + 60
        while time.monotonic() < until:
            final = remote("status", job_id=job["job_id"])
            if final["state"] not in ("launching", "running"):
                break
            time.sleep(1)
        assert final["state"] == "succeeded" and final["exit_code"] == 0, final
        checked("App Server restart, credential reuse and job recovery")
        downloaded = remote("get", path="result.json")
        output = base64.b64decode(downloaded["content_base64"])
        assert hashlib.sha256(output).hexdigest() == downloaded["sha256"]
        assert json.loads(output)["sum_squares"] == 333283335000
        (root / "result.json").write_bytes(output)
        report["result"] = json.loads(output)
        report["remote_workdir"] = final["cwd"]
        checked("Result download, SHA-256 verification and expected numerical output")
        cancelled = remote(
            "submit", command="echo cancel-test; sleep 120", timeout=180, idempotency_key="cancel-1"
        )
        report["cancelled_job_id"] = cancelled["job_id"]
        assert remote("cancel", job_id=cancelled["job_id"])["state"] == "cancelled"
        checked("Managed remote process-group cancellation")
        # A fresh Harness process uses the real process-owner resource bridge,
        # as workers do, with no LLM/provider or simulated transport involved.
        worker = """import asyncio,json,os
from app.config import settings
from core.remote_resources import DELIVERY_ENV,bind_worker
from core.state import State
from core.tool_registry import execute
import shared.tools.library.remote_resources
async def main():
    req=json.load(__import__('sys').stdin)
    keys=('database_url','platform_data_root','runtime_tenant_id','profile','secret_key')
    os.environ[DELIVERY_ENV]=json.dumps({key:getattr(settings,key) for key in keys})
    bind_worker(user_id=req['user_id'],project_id=req['project_id'])
    state=State.new(node_type='_orchestrator',project_id=req['project_id'],
                    base_dir=__import__('pathlib').Path(req['runs']))
    listing=await execute('resource',state,action='list')
    assert any(r['id']==req['resource_id'] for r in listing['resources']),listing
    executed=await execute('remote_exec',state,resource_id=req['resource_id'],
                           command='printf harness-bridge-ok')
    assert executed['stdout']=='harness-bridge-ok',executed
    jobs=await execute('remote_job',state,action='list',resource_id=req['resource_id'])
    assert any(j['job_id']==req['job_id'] for j in jobs['jobs']),jobs
    local=__import__('pathlib').Path(state.root)
    payload=local/'transfer-source.bin'
    payload.write_bytes(bytes(range(256)))
    uploaded=await execute('remote_file',state,action='upload',resource_id=req['resource_id'],
        remote_path='tool-transfer.bin',local_path=str(payload))
    assert uploaded['status']=='success',uploaded
    downloaded=await execute('remote_file',state,action='download',resource_id=req['resource_id'],
        remote_path='tool-transfer.bin',local_path=str(local/'transfer-result.bin'))
    assert downloaded['status']=='success',downloaded
    assert (local/'transfer-result.bin').read_bytes()==payload.read_bytes()
    print(json.dumps({'status':'success','jobs':len(jobs['jobs'])}))
asyncio.run(main())
"""
        resumed = subprocess.run(
            [sys.executable, "-c", worker],
            input=json.dumps(
                dict(
                    user_id=user["id"],
                    project_id=project["id"],
                    resource_id=rid,
                    job_id=job["job_id"],
                    runs=str(root / "tool-runs"),
                )
            ),
            text=True,
            capture_output=True,
            env=env,
            cwd=repository,
            timeout=60,
        )
        assert resumed.returncode == 0, resumed.stderr
        report["harness_bridge"] = json.loads(resumed.stdout.splitlines()[-1])
        checked("Fresh Harness process: resource discovery, remote execution and task history")
        report["status"] = "passed"
        (root / "report.json").write_text(json.dumps(report, indent=2))
        print("Report: " + str(root / "report.json"), flush=True)
    finally:
        stop()
        client.close()
        log.close()


if __name__ == "__main__":
    main()

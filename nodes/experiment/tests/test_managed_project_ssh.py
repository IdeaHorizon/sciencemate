"""The registered non-Slurm SSH resource is available through Experiment tools."""

from __future__ import annotations

import pytest

from core import remote_resources, tool_registry
from core.state import State
from nodes.experiment.tools import ledger, resource_manager

RESOURCE_ID = "866a93ab-376c-4cca-bac6-0c054373e8c4"


@pytest.fixture
def state(tmp_path):
    state = State.new(
        node_type="experiment",
        base_dir=tmp_path / "runs",
        project_id="project",
        session_id="session",
    )
    ledger.open_ledger(state, write=True, bootstrap=True)
    ledger.mark_bootstrap_ready(state)
    return state


@pytest.mark.asyncio
async def test_registered_non_slurm_ssh_is_discoverable_through_the_tool(state, monkeypatch):
    async def bridge(request_state, action, params):
        assert request_state is state
        assert action == "list" and params == {}
        return {
            "status": "success",
            "resources": [
                {
                    "id": RESOURCE_ID,
                    "name": "Linux",
                    "provider": "ssh",
                    "resource_type": "compute",
                    "is_enabled": True,
                    "config": {"observation": {"schedulers": [], "cpu_count": 8}},
                },
            ],
        }

    monkeypatch.setattr(remote_resources, "_handler", bridge)
    monkeypatch.setattr(
        resource_manager,
        "_discover",
        lambda *_a, **_kw: {
            "status": "success",
            "available_schedulers": ["local"],
            "submittable_schedulers": ["local"],
        },
    )
    result = await tool_registry.execute("discover_resources", state, save_artifact=False)
    assert result["project_resources"][0]["resource_id"] == RESOURCE_ID
    assert result["project_resources"][0]["execution_backend"] == "ssh"
    assert result["project_resources"][0]["remote_write_isolation"] == "missing"


@pytest.fixture
def supervisor(tmp_path, monkeypatch):
    """The real remote agent and supervisor, with only the SSH wire substituted."""
    import asyncio
    import hashlib
    import json
    import sys
    from pathlib import Path

    from shared.lib import dangerous_commands

    home = tmp_path / "remote"
    home.mkdir()
    agent = Path(__file__).resolve().parents[3] / "core" / "_ssh_agent.py.in"
    runner = """
import json, sys
from pathlib import Path
agent = Path(sys.argv[1])
Path.home = classmethod(lambda cls: cls(sys.argv[2]))
scope = {"__name__": "fixture_ssh_agent"}
exec(compile(agent.read_bytes(), str(agent), "exec"), scope)
try:
    print(json.dumps(scope["main"](json.load(sys.stdin))))
except (OSError, ValueError) as error:
    print(json.dumps({"status": "error", "error": str(error)}))
"""
    calls = []
    faults = set()

    async def bridge(state, action, params):
        calls.append((action, dict(params)))
        if action == "list":
            return {
                "status": "success",
                "resources": [
                    {
                        "id": RESOURCE_ID,
                        "provider": "ssh",
                        "resource_type": "compute",
                        "name": "Linux without Slurm",
                        "is_enabled": True,
                    },
                ],
            }
        if action == "probe" and "deny_probe" in faults:
            raise PermissionError("This resource account belongs to another user")
        if "offline" in faults or (action == "get" and "drop_get" in faults):
            raise OSError("SSH disconnected")
        if action == "submit" and "drop_before_launch" in faults:
            faults.remove("drop_before_launch")
            raise OSError("SSH disconnected before delivering request")
        assert params["resource_id"] == RESOURCE_ID
        request = {"action": action, "project_id": state.project_id, **params}
        if action in {"exec", "submit"}:
            if action == "submit":
                request["agent_source"] = agent.read_text()
                request["job_id"] = hashlib.sha256(
                    f"{RESOURCE_ID}:{params['idempotency_key']}".encode()
                ).hexdigest()[:32]
            else:
                from uuid import uuid4

                request["job_id"] = uuid4().hex
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-I",
            "-c",
            runner,
            str(agent),
            str(home),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(
            process.communicate(json.dumps(request).encode()), timeout=20
        )
        assert process.returncode == 0, err.decode()
        response = json.loads(out)
        if action == "submit" and "drop_ack" in faults:
            faults.remove("drop_ack")
            raise OSError("connection lost after launch")
        if action == "get" and "wrong_bytes" in faults:
            response["content_base64"] = "d3Jvbmc="
        response.setdefault(
            "status",
            "error" if action == "exec" and response.get("state") != "succeeded" else "success",
        )
        return response

    monkeypatch.setattr(remote_resources, "_handler", bridge)
    monkeypatch.setattr(dangerous_commands, "bypass_enabled", lambda: True)
    return calls, faults


async def _submit(state, **kwargs):
    return await tool_registry.execute(
        "submit_job",
        state,
        scheduler="ssh",
        resource_id=RESOURCE_ID,
        dry_run=False,
        walltime_minutes=1,
        **kwargs,
    )


async def _status(state, handle):
    return await tool_registry.execute("job_status", state, scheduler="ssh", job_id=handle)


@pytest.mark.asyncio
async def test_real_supervisor_terminal_and_returned_file_feed_finish(state, supervisor):
    from nodes.experiment.tools import run_bootstrap

    state.hook_state["node_inputs"] = {
        "experiment_spec": "Run the staged script and check its result file.",
        "prereg_assignment": {"kind": "none", "reason": "Fixture"},
    }
    await run_bootstrap.establish(state)
    script = state.root / "produce.py"
    script.write_text("from pathlib import Path\nPath('result.txt').write_text('fresh result')\n")
    submitted = await _submit(
        state,
        command="python3 produce.py",
        output_paths=["result.txt"],
        foreground_wait_s=5,
        stage_in=[{"src": str(script), "dst": "produce.py"}],
    )
    assert submitted.get("state") == "succeeded", submitted
    assert submitted["output_collection"]["status"] == "complete", submitted
    file = submitted["output_collection"]["files"][0]
    from pathlib import Path

    assert Path(file["path"]).read_text() == "fresh result"
    rows = ledger.open_ledger(state).records()
    assert [
        r["kind"]
        for r in rows
        if r["kind"]
        in {"intent", "submission_attempted", "job_bound", "job_terminal", "job_output_collected"}
    ] == ["intent", "submission_attempted", "job_bound", "job_terminal", "job_output_collected"]
    observed = next(
        row
        for row in ledger.project_output_observations(rows)
        if row.get("local_path") == file["path"] or file["path"] in row.get("created", [])
    )
    assert observed["source_kind"] == "job_output_collected"
    closure = await tool_registry.execute(
        "finish",
        state,
        outcome="success",
        summary="Staged script produced the expected result.",
        verification=[
            {
                "requirement": "Run the staged script and check its result file.",
                "checks": [{"kind": "file", "path": file["path"]}],
            }
        ],
    )
    assert closure["status"] == "success", closure


@pytest.mark.asyncio
async def test_lost_ack_status_reconciles_same_job_and_never_replays_payload(state, supervisor):
    calls, faults = supervisor
    faults.add("drop_ack")
    args = {"command": "printf 'one\\n' >> result.txt", "output_paths": ["result.txt"]}
    submitted = await _submit(state, **args)
    assert submitted["state"] == "unknown" and submitted["do_not_resubmit"], submitted
    faults.add("offline")
    disconnected = await _status(state, submitted["job_id"])
    assert disconnected["state"] == "unknown"
    faults.remove("offline")
    faults.add("drop_get")
    # Recover from durable run records with no in-memory tool or hook state.
    from dataclasses import replace

    state = replace(state, hook_state={})
    resolved = await tool_registry.execute(
        "wait_for_external_job", state, scheduler="ssh", job_id=submitted["job_id"], max_wait_s=30
    )
    assert resolved["state"] == "succeeded", resolved
    assert resolved["output_collection"]["status"] == "partial", resolved
    faults.remove("drop_get")
    faults.add("wrong_bytes")
    retry = await _submit(state, **args)
    assert retry["state"] == "succeeded"
    assert retry["output_collection"]["status"] == "partial"
    faults.remove("wrong_bytes")
    retry = await _submit(state, **args)
    assert retry["state"] == "succeeded"
    from pathlib import Path

    assert Path(retry["output_collection"]["files"][0]["path"]).read_text() == "one\n"
    assert len([1 for action, _ in calls if action == "submit"]) == 1
    assert len([r for r in ledger.open_ledger(state).records() if r["kind"] == "job_terminal"]) == 1


@pytest.mark.asyncio
async def test_failing_pipeline_and_out_of_run_cancel(state, supervisor, tmp_path):
    calls, _ = supervisor
    submitted = await _submit(state, command="false | tail -1", foreground_wait_s=5)
    assert submitted["state"] == "failed" and submitted["exit_code"] == 1, submitted
    foreign = State.new(
        node_type="experiment",
        base_dir=tmp_path / "other",
        project_id=state.project_id,
        session_id=state.session_id,
    )
    ledger.open_ledger(foreign, write=True, bootstrap=True)
    ledger.mark_bootstrap_ready(foreign)
    denied = await tool_registry.execute(
        "cancel_job", foreign, scheduler="ssh", job_id=submitted["job_id"]
    )
    assert denied["reason"] == "ssh_job_not_owned"
    assert not any(action == "cancel" for action, _ in calls)


@pytest.mark.asyncio
async def test_valid_cancellation_and_unmanaged_background_refusal(state, supervisor):
    calls, _ = supervisor
    background = await _submit(state, command="nohup sleep 60 &")
    assert background.get("job_submitted") is False, background
    assert not any(action == "submit" for action, _ in calls)
    submitted = await _submit(state, command="sleep 30")
    assert submitted["state"] in {"running", "launching"}, submitted
    cancelled = await tool_registry.execute(
        "cancel_job", state, scheduler="ssh", job_id=submitted["job_id"]
    )
    assert cancelled["state"] == "cancelled", cancelled


def test_unknown_backend_cannot_inherit_local_isolation():
    from nodes.experiment.tools import scheduler_adapters

    assert scheduler_adapters.capabilities_for("unknown").remote_write_isolation == "unknown"
    assert scheduler_adapters.capabilities_for("ssh").remote_write_isolation == "missing"
    assert scheduler_adapters.capabilities_for("local").remote_write_isolation == "enforced"


@pytest.mark.asyncio
async def test_lost_request_can_replay_same_key_after_status_stays_unknown(state, supervisor):
    calls, faults = supervisor
    faults.add("drop_before_launch")
    submitted = await _submit(state, command="printf 'once'", foreground_wait_s=5)
    assert submitted["state"] == "unknown"
    observed = await _status(state, submitted["job_id"])
    assert observed["state"] == "unknown" and observed["safe_retry_same_request"]
    assert [action for action, _ in calls].count("submit") == 1
    retry = await _submit(state, command="printf 'once'", foreground_wait_s=5)
    assert retry["state"] == "succeeded", retry
    assert retry["submission_nonce"] == submitted["submission_nonce"]
    requests = [params for action, params in calls if action == "submit"]
    assert len(requests) == 2 and requests[0] == requests[1]
    rows = ledger.open_ledger(state).records()
    assert [
        row["kind"]
        for row in rows
        if row["kind"]
        in {"intent", "submission_attempted", "job_unknown", "job_bound", "job_terminal"}
    ] == ["intent", "submission_attempted", "job_unknown", "job_bound", "job_terminal"]


@pytest.mark.asyncio
async def test_staging_change_is_not_an_implicit_second_attempt(state, supervisor):
    calls, faults = supervisor
    faults.add("drop_before_launch")
    source = state.root / "input.txt"
    source.write_text("original")
    args = {"command": "cat input.txt", "stage_in": [{"src": str(source), "dst": "input.txt"}]}
    submitted = await _submit(state, **args)
    assert submitted["state"] == "unknown"
    source.write_text("edited")
    changed = await _submit(state, **args)
    assert changed["reason"] == "ssh_staged_inputs_changed"
    assert changed["submission_nonce"] == submitted["submission_nonce"]
    assert [action for action, _ in calls].count("submit") == 1


@pytest.mark.asyncio
async def test_concurrent_replay_does_not_stage_or_submit_twice(state, supervisor):
    import asyncio

    calls, _ = supervisor
    source = state.root / "input.txt"
    source.write_text("original")
    args = {"command": "cat input.txt", "stage_in": [{"src": str(source), "dst": "input.txt"}]}
    first, second = await asyncio.gather(_submit(state, **args), _submit(state, **args))
    assert first["job_id"] == second["job_id"]
    assert [action for action, _ in calls].count("submit") == 1
    assert [action for action, _ in calls].count("put") == 1


@pytest.mark.asyncio
async def test_unchanged_remote_output_has_no_producer(state, supervisor, tmp_path):
    import hashlib

    params = dict(
        scheduler="ssh",
        resource_id=RESOURCE_ID,
        command="printf 'no output change'",
        walltime_minutes=1,
        output_paths=["result.txt"],
    )
    plan = await tool_registry.execute("submit_job", state, dry_run=True, **params)
    scope = hashlib.sha256(state.project_id.encode()).hexdigest()[:24]
    workdir = tmp_path / "remote" / "harness-workspaces" / scope / plan["workdir"]
    workdir.mkdir(parents=True)
    (workdir / "result.txt").write_text("left from before the job")
    result = await tool_registry.execute(
        "submit_job", state, dry_run=False, foreground_wait_s=5, **params
    )
    assert result["state"] == "succeeded", result
    assert result["output_collection"]["files"] == []
    assert not any(
        row["kind"] == "job_output_collected" for row in ledger.open_ledger(state).records()
    )


@pytest.mark.asyncio
async def test_ssh_job_preserves_read_only_and_shell_boundary(state, supervisor):
    from core.side_effect_contract import declare_read_only

    calls, _ = supervisor
    escaping = await _submit(state, command="printf stray > ../outside")
    assert escaping["reason"] == "ssh_command_path_outside_job", escaping
    unmanaged = await _submit(state, command="ssh somewhere printf stray")
    assert unmanaged["job_submitted"] is False, unmanaged
    declare_read_only(state, quote="Read only.", said_by="user", declared_by_run_id=state.run_id)
    readonly = await _submit(state, command="printf stray")
    assert readonly["reason"] == "read_only"
    assert not any(action in {"put", "submit"} for action, _ in calls)


@pytest.mark.asyncio
async def test_visible_but_unauthorized_resource_does_not_open_a_job(state, supervisor):
    calls, faults = supervisor
    faults.add("deny_probe")
    result = await _submit(state, command="printf stray")
    assert result["reason"] == "ssh_resource_probe_failed", result
    assert result["job_submitted"] is False
    assert [action for action, _ in calls] == ["list", "probe"]
    assert not any(row["kind"] == "intent" for row in ledger.open_ledger(state).records())


@pytest.mark.asyncio
async def test_submit_routes_ssh_before_applying_native_resource_defaults(state, supervisor):
    calls, _ = supervisor
    common = {"command": "printf once", "resource_id": RESOURCE_ID}
    planned = await tool_registry.execute("submit_job", state, **common)
    assert planned["scheduler"] == "ssh" and planned["dry_run"] is True, planned
    assert [action for action, _ in calls] == ["list"]

    explicit_native_limits = await tool_registry.execute(
        "submit_job", state, **common, memory_gb=4.0, storage_gb=8.0
    )
    assert explicit_native_limits["reason"] == "ssh_contract_unsupported"
    assert explicit_native_limits["unsupported"] == ["memory_gb", "storage_gb"]

    conflicting_target = await tool_registry.execute(
        "submit_job", state, **common, scheduler="slurm"
    )
    assert conflicting_target["reason"] == "resource_requires_ssh"
    assert [action for action, _ in calls] == ["list"]

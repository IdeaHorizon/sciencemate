"""Normal tool/AgentLoop entry, real detached supervisor, no GPU or live service."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
from pathlib import Path

import pytest

from core import job_wait, remote_resources
from core.agent_loop import run_loop
from core.bootstrap import bootstrap
from core.harness import NodeHarness
from core.llm import LLMMessage, LLMResponse
from core.state import State
from core.tool_registry import execute

AGENT = Path(__file__).resolve().parents[1] / "core/_ssh_agent.py.in"
pytestmark = pytest.mark.skipif(os.name != "posix", reason="Real remote POSIX supervisor")


def job_id(key):
    return hashlib.sha256(("r:" + key).encode()).hexdigest()[:32]


@pytest.fixture
def bridge(tmp_path, monkeypatch):
    bootstrap()
    requests = []
    receipts = {}

    async def handler(state, action, params):
        request = {**params, "action": action, "project_id": state.project_id, "resource_id": "r"}
        if action in ("submit", "exec"):
            request["job_id"] = job_id(request.pop("idempotency_key"))
            request["agent_source"] = AGENT.read_text()
        requests.append((state.run_id, action, request.get("job_id")))
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-I",
            str(AGENT),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "HOME": str(tmp_path / "remote")},
        )
        stdout, stderr = await asyncio.wait_for(
            process.communicate(json.dumps(request).encode()), 10
        )
        assert process.returncode == 0, stderr.decode()
        result = json.loads(stdout)
        if action == "submit":
            receipts[result["job_id"]] = result
        return {**result, "status": "error" if result.get("error") else "success"}

    monkeypatch.setattr(remote_resources, "_handler", handler)
    monkeypatch.setattr(job_wait, "_POLL_BACKOFF", (0.01,))
    return requests, receipts


def reply(*calls, content=""):
    return LLMResponse(
        content=content,
        tool_calls=[
            {
                "id": f"c-{i}-{name}-{json.dumps(arguments)}",
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments)},
            }
            for i, (name, arguments) in enumerate(calls)
        ],
        finish_reason="tool_calls" if calls else "stop",
        usage={},
    )


def remote_call(action, **arguments):
    return "remote_job", {"action": action, "resource_id": "r", **arguments}


class ScriptedLLM:
    def __init__(self, *responses):
        self.responses = iter(responses)
        self.calls = 0
        self.seen = []

    async def chat(self, messages, **_):
        self.calls += 1
        self.seen = list(messages)
        return next(self.responses)


def harness():
    return NodeHarness(
        node_type="_orchestrator", system_prompt="test", tools=["remote_job"], max_turns=20
    )


def state_at(tmp_path, project="p"):
    return State.new("_orchestrator", tmp_path / "runs", project_id=project)


async def until(predicate):
    async with asyncio.timeout(10):
        while not predicate():
            await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_wait_exceeds_six_poll_windows_without_model_turns_and_preserves_parallel_jobs(
    tmp_path, bridge
):
    requests, receipts = bridge
    state = state_at(tmp_path)
    command = "echo started; while [ ! -f release ]; do sleep .01; done; echo done"
    llm = ScriptedLLM(
        reply(
            remote_call("submit", command=command, idempotency_key="a", cwd="a", timeout=10),
            remote_call("submit", command=command, idempotency_key="b", cwd="b", timeout=10),
        ),
        reply(remote_call("wait", job_id=job_id("a"), poll_interval_s=0.01)),
        reply(remote_call("wait", job_id=job_id("b"), poll_interval_s=0.01)),
        reply(content="finished"),
    )
    other = state_at(tmp_path, project="another-project")
    unrelated = await execute(
        "remote_job",
        other,
        action="submit",
        resource_id="r",
        command=command,
        idempotency_key="other",
        cwd="other",
        timeout=10,
    )
    run = asyncio.create_task(
        run_loop(harness(), state, [LLMMessage(role="user", content="go")], llm)
    )
    try:
        await until(
            lambda: len([r for r in requests if r == (state.run_id, "status", job_id("a"))]) >= 9
        )
        assert llm.calls == 2 and not run.done()
        assert set(receipts) == {job_id("a"), job_id("b"), job_id("other")}
        assert job_wait.load(state)["identity"]["job_id"] == job_id("a")
        assert job_wait.load(other) is None
        # Both jobs are already running; selecting one wait never serialized submission.
        for key in ("a", "b"):
            (Path(receipts[job_id(key)]["cwd"]) / "release").touch()
        result = await asyncio.wait_for(run, 10)
        assert result.status == "completed" and llm.calls == 4
        assert not job_wait.load(state)
        assert not any(r[0] == state.run_id and r[2] == job_id("other") for r in requests)
        assert "progress_circuit_break" not in state.transcript_path.read_text()
        assert "done" in " ".join(m.content or "" for m in llm.seen)
        still_running = await execute(
            "remote_job", other, action="status", resource_id="r", job_id=unrelated["job_id"]
        )
        assert still_running["state"] == "running"
    finally:
        for receipt in receipts.values():
            (Path(receipt["cwd"]) / "release").touch()
        if not run.done():
            run.cancel()
            await asyncio.gather(run, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command,timeout,terminal",
    [
        ("echo ok", 10, "succeeded"),
        ("echo failure >&2; exit 7", 10, "failed"),
        ("sleep 30", 0.15, "timed_out"),
        ("echo started; sleep 30", 10, "cancelled"),
    ],
)
async def test_real_terminal_resumes_once_with_original_identity(
    tmp_path, bridge, command, timeout, terminal
):
    requests, _ = bridge
    state = state_at(tmp_path)
    llm = ScriptedLLM(
        reply(remote_call("submit", command=command, timeout=timeout, idempotency_key="terminal")),
        reply(remote_call("wait", job_id=job_id("terminal"), poll_interval_s=0.01)),
        reply(content="terminal received"),
    )
    run = asyncio.create_task(
        run_loop(harness(), state, [LLMMessage(role="user", content="go")], llm)
    )
    if terminal == "cancelled":
        await until(lambda: job_wait.load(state) is not None)
        await execute(
            "remote_job", state, action="cancel", resource_id="r", job_id=job_id("terminal")
        )
    result = await asyncio.wait_for(run, 10)
    assert result.status == "completed" and llm.calls == 3
    wait = next(c["result"] for c in result.tool_calls if c["args"]["action"] == "wait")
    assert wait["state"] == terminal and wait["job_id"] == job_id("terminal")
    assert wait["project_id"] == "p" and wait["resource_id"] == "r"
    replay = await execute(
        "remote_job", state, action="wait", resource_id="r", job_id=job_id("terminal")
    )
    assert replay["state"] == terminal and replay["request_digest"] == wait["request_digest"]
    assert len([r for r in requests if r[1] == "submit"]) == 1
    job_wait.acknowledge(state)


@pytest.mark.asyncio
async def test_reopen_reattaches_selected_job_and_connection_retry_never_submits(
    tmp_path, bridge, monkeypatch
):
    requests, receipts = bridge
    state = state_at(tmp_path)
    await execute(
        "remote_job",
        state,
        action="submit",
        resource_id="r",
        cwd="restart",
        command="while [ ! -f release ]; do sleep .01; done; echo resumed",
        idempotency_key="restart",
        timeout=10,
    )
    wait = asyncio.create_task(
        execute(
            "remote_job",
            state,
            action="wait",
            resource_id="r",
            job_id=job_id("restart"),
            poll_interval_s=0.01,
        )
    )
    await until(lambda: job_wait.load(state) is not None)
    identity = job_wait.load(state)["identity"]
    # The worker disappears, while its supervised payload survives.
    wait.cancel()
    await asyncio.gather(wait, return_exceptions=True)
    reopened = State.reopen("_orchestrator", tmp_path / "runs", state.run_id, project_id="p")
    actual = remote_resources._handler
    failures = 0

    async def reconnect(st, action, params):
        nonlocal failures
        if action == "status" and failures < 2:
            failures += 1
            raise ConnectionError("controlled reconnect")
        return await actual(st, action, params)

    monkeypatch.setattr(remote_resources, "_handler", reconnect)
    llm = ScriptedLLM(reply(content="resumed"))
    run = asyncio.create_task(
        run_loop(harness(), reopened, [LLMMessage(role="user", content="continue")], llm)
    )
    try:
        await until(lambda: failures == 2)
        assert llm.calls == 0
        assert job_wait.load(reopened)["identity"] == identity
        (Path(receipts[job_id("restart")]["cwd"]) / "release").touch()
        result = await asyncio.wait_for(run, 10)
        assert result.status == "completed" and llm.calls == 1
        assert len([r for r in requests if r[1] == "submit"]) == 1
        assert any("job-wait:" in (m.content or "") for m in llm.seen)
        assert job_wait.load(reopened) is None
    finally:
        (Path(receipts[job_id("restart")]["cwd"]) / "release").touch()
        if not run.done():
            run.cancel()
            await asyncio.gather(run, return_exceptions=True)


@pytest.mark.asyncio
async def test_cancel_wait_uses_run_cancellation_without_cancelling_another_job(tmp_path, bridge):
    requests, _ = bridge
    state = state_at(tmp_path)
    receipt = await execute(
        "remote_job",
        state,
        action="submit",
        resource_id="r",
        command="sleep 30",
        idempotency_key="stop",
        timeout=10,
    )
    llm = ScriptedLLM(reply(remote_call("wait", job_id=receipt["job_id"], poll_interval_s=30)))
    run = asyncio.create_task(
        run_loop(harness(), state, [LLMMessage(role="user", content="wait")], llm)
    )
    await until(lambda: job_wait.load(state) is not None)
    state.hook_state["kill_signal"] = {"reason": "user stopped the wait"}
    result = await asyncio.wait_for(run, 2)
    assert result.status == "cancelled" and llm.calls == 1
    assert not job_wait.load(state)
    assert not any(action == "cancel" for _, action, _ in requests)
    from core.cancellation import clear

    clear(state)
    await execute("remote_job", state, action="cancel", resource_id="r", job_id=receipt["job_id"])


@pytest.mark.asyncio
async def test_unbound_or_mismatched_wait_is_not_a_progress_exemption(
    tmp_path, bridge, monkeypatch
):
    state = state_at(tmp_path)

    async def wrong_owner(st, action, params):
        return dict(
            status="success",
            project_id="wrong",
            resource_id="r",
            job_id=job_id("bad"),
            request_digest="a" * 64,
            state="running",
        )

    monkeypatch.setattr(remote_resources, "_handler", wrong_owner)

    class RepeatingLLM:
        calls = 0

        async def chat(self, messages, **_):
            self.calls += 1
            return reply(remote_call("wait", job_id=job_id("bad")))

    llm = RepeatingLLM()
    result = await run_loop(harness(), state, [LLMMessage(role="user", content="go")], llm)
    assert result.terminal_cause == "progress_circuit_break" and llm.calls < 15
    assert not job_wait.load(state)


@pytest.mark.asyncio
@pytest.mark.parametrize("with_call_id", [False, True])
async def test_checkpoint_then_crash_before_ack_delivers_exactly_once(
    tmp_path, bridge, monkeypatch, with_call_id
):
    from core.agent_loop import load_messages_checkpoint

    state = state_at(tmp_path)
    receipt = await execute(
        "remote_job",
        state,
        action="submit",
        resource_id="r",
        command="echo once",
        idempotency_key="once",
        timeout=10,
    )
    if with_call_id:
        state.hook_state["_current_tool_call_id_being_executed"] = "original-wait-call"
    job_wait.register(
        state,
        "remote_job",
        dict(action="wait", resource_id="r", job_id=receipt["job_id"]),
        {key: receipt[key] for key in ("project_id", "resource_id", "job_id", "request_digest")},
    )
    state.hook_state.pop("_current_tool_call_id_being_executed", None)
    # Cover both original-call and older-checkpoint delivery. Crash exactly
    # after resume checkpointed the notice but before it deletes the wait file.
    actual_ack = job_wait.acknowledge

    def crash(_):
        raise RuntimeError("crash before unlink")

    monkeypatch.setattr(job_wait, "acknowledge", crash)
    with pytest.raises(RuntimeError, match="before unlink"):
        await job_wait.resume(state, [LLMMessage(role="user", content="original")], turn=1)
    assert job_wait.load(state)["finished"] is True
    messages, _ = load_messages_checkpoint(state)
    assert len([m for m in messages if "job-wait:" in (m.content or "")]) == 1
    monkeypatch.setattr(job_wait, "acknowledge", actual_ack)
    reopened = State.reopen("_orchestrator", tmp_path / "runs", state.run_id, project_id="p")
    requests, _ = bridge
    before = len(requests)
    await job_wait.resume(reopened, messages, turn=2)
    assert len(requests) == before, "Already committed delivery must not even requery"
    assert len([m for m in messages if "job-wait:" in (m.content or "")]) == 1
    assert job_wait.load(reopened) is None


def test_recovery_rejects_cross_run_identity_and_mutating_continuations(tmp_path):
    state = state_at(tmp_path)
    job_wait.register(
        state,
        "remote_job",
        dict(action="wait", resource_id="r", job_id="a" * 32),
        dict(resource_id="r", job_id="a" * 32),
    )
    path = state.root / "job_wait.json"
    original = json.loads(path.read_text())
    for mutation in (
        {"run_id": "another-run"},
        {"project_id": "another-project"},
        {"arguments": {**original["arguments"], "action": "submit"}},
    ):
        path.write_text(json.dumps({**original, **mutation}))
        with pytest.raises(ValueError):
            job_wait.load(state)


@pytest.mark.asyncio
@pytest.mark.parametrize("worker_source", ["checkout", "payload"])
async def test_real_worker_death_and_new_process_resume_the_same_supervised_job(
    tmp_path, bridge, monkeypatch, worker_source,
):
    worker_root = AGENT.parent.parent
    if worker_source == "payload":
        from scripts.package import build_server_bundle

        interface = tmp_path / "interface"
        interface.mkdir()
        (interface / "index.html").write_text("offline packaging fixture")
        monkeypatch.setattr(build_server_bundle, "DIST", tmp_path / "distribution")
        worker_root = build_server_bundle.stage("0.5.13", interface) / "harness"
    _, receipts = bridge
    state = state_at(tmp_path)
    selected = await execute(
        "remote_job",
        state,
        action="submit",
        resource_id="r",
        cwd="worker-restart",
        command="while [ ! -f release ]; do sleep .01; done; echo survived-worker-death",
        idempotency_key="worker",
        timeout=10,
    )
    script = tmp_path / "wait_worker.py"
    script.write_text("""
import asyncio, json, os, sys
from pathlib import Path
from core import remote_resources
from core.agent_loop import run_loop
from core.bootstrap import bootstrap
from core.harness import NodeHarness
from core.llm import LLMMessage, LLMResponse
from core.state import State
from core.tool_registry import execute

async def main():
    base, run_id, agent, remote_home, mode, job_id = sys.argv[1:]
    bootstrap()
    state = State.reopen("_orchestrator", Path(base), run_id, project_id="p")
    async def bridge(st, action, params):
        assert action == "status", "Recovery may observe, never submit"
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-I", agent, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "HOME": remote_home})
        out, err = await proc.communicate(json.dumps(
            {**params, "action": action, "project_id": "p", "resource_id": "r"}).encode())
        assert proc.returncode == 0, err.decode()
        return {**json.loads(out), "status": "success"}
    remote_resources.install(bridge)
    if mode == "wait":
        await execute("remote_job", state, action="wait", resource_id="r", job_id=job_id,
                      poll_interval_s=0.01)
    else:
        class Model:
            calls = 0
            async def chat(self, messages, **kwargs):
                self.calls += 1
                assert "survived-worker-death" in str(messages)
                return LLMResponse(content="finished", tool_calls=[],
                                   finish_reason="stop", usage={})
        model = Model()
        result = await run_loop(NodeHarness(node_type="_orchestrator", system_prompt="test",
                                tools=["remote_job"], max_turns=5), state,
                                [LLMMessage(role="user", content="continue")], model)
        print(json.dumps({"status": result.status, "model_calls": model.calls,
                          "source_root": str(
                              Path(remote_resources.__file__).resolve().parents[1])}))
asyncio.run(main())
""")
    argv = [
        sys.executable,
        str(script),
        str(tmp_path / "runs"),
        state.run_id,
        str(AGENT),
        str(tmp_path / "remote"),
    ]
    env = {**os.environ, "PYTHONPATH": str(worker_root)}
    first = await asyncio.create_subprocess_exec(
        *argv,
        "wait",
        selected["job_id"],
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        await until(lambda: job_wait.load(state) is not None or first.returncode is not None)
        assert first.returncode is None
        original = job_wait.load(state)["identity"]
        first.kill()
        await first.communicate()
        running = await execute(
            "remote_job", state, action="status", resource_id="r", job_id=selected["job_id"]
        )
        assert running["state"] == "running"
        (Path(receipts[selected["job_id"]]["cwd"]) / "release").touch()
        second = await asyncio.create_subprocess_exec(
            *argv,
            "resume",
            selected["job_id"],
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(second.communicate(), 10)
        assert second.returncode == 0, err.decode()
        assert json.loads(out) == {
            "status": "completed", "model_calls": 1, "source_root": str(worker_root.resolve()),
        }
        assert original["job_id"] == selected["job_id"]
        assert job_wait.load(state) is None
    finally:
        (Path(receipts[selected["job_id"]]["cwd"]) / "release").touch()
        if first.returncode is None:
            first.kill()
            await first.communicate()


@pytest.mark.asyncio
async def test_permissions_are_rechecked_during_wait_and_retries_are_bounded(
    tmp_path, bridge, monkeypatch
):
    _, receipts = bridge
    state = state_at(tmp_path)
    selected = await execute(
        "remote_job",
        state,
        action="submit",
        resource_id="r",
        cwd="permission",
        command="while [ ! -f release ]; do sleep .01; done",
        idempotency_key="permission",
        timeout=10,
    )
    assert selected["next_action"]["action"] == "wait"
    actual = remote_resources._handler
    calls = 0

    class Denied(Exception):
        status_code = 403

    async def revoked(st, action, params):
        nonlocal calls
        calls += 1
        if calls > 1:
            raise Denied("resource permission revoked")
        return await actual(st, action, params)

    monkeypatch.setattr(remote_resources, "_handler", revoked)
    try:
        result = await execute(
            "remote_job",
            state,
            action="wait",
            resource_id="r",
            job_id=selected["job_id"],
            poll_interval_s=0.01,
        )
        assert result["status"] == "error" and not result["retryable"]
        assert calls == 2 and "revoked" in result["error"]
        job_wait.acknowledge(state)

        async def disconnected(st, action, params):
            nonlocal calls
            calls += 1
            raise ConnectionError("still disconnected")

        calls = 0
        monkeypatch.setattr(remote_resources, "_handler", disconnected)
        result = await execute(
            "remote_job",
            state,
            action="wait",
            resource_id="r",
            job_id=selected["job_id"],
            poll_interval_s=0.01,
        )
        assert calls == 3 and result["do_not_resubmit"]
        assert result["wait_outcome"] == "connection_unconfirmed"
    finally:
        (Path(receipts[selected["job_id"]]["cwd"]) / "release").touch()


@pytest.mark.asyncio
async def test_lost_managed_submit_receipt_reconciles_the_same_physical_job(tmp_path, bridge):
    from core.agent_loop import load_messages_checkpoint
    requests, receipts = bridge
    state = state_at(tmp_path)
    actual = remote_resources._handler

    crashed = False
    async def accepted_then_died(st, action, params):
        nonlocal crashed
        result = await actual(st, action, params)
        if action == "submit" and not crashed:
            crashed = True
            raise asyncio.CancelledError("worker died after physical accept, before receipt")
        return result

    remote_resources._handler = accepted_then_died
    llm = ScriptedLLM(reply(remote_call(
        "submit", command="echo one-physical-effect", idempotency_key="unknown", timeout=10,
    )))
    with pytest.raises(asyncio.CancelledError):
        await run_loop(harness(), state, [LLMMessage(role="user", content="go")], llm)
    messages, _ = load_messages_checkpoint(state)
    assert len(receipts) == 1 and len([r for r in requests if r[1] == "submit"]) == 1
    # Re-enter the managed adapter with the original key. This is a second API
    # attempt, and one physical supervisor identity/command execution.
    import chat
    chat._repair_message_tool_protocol(messages)
    resumed = ScriptedLLM(reply(content="original receipt reconciled"))
    await run_loop(harness(), state, messages, resumed)
    assert resumed.calls == 1 and len([r for r in requests if r[1] == "submit"]) == 2
    assert len(receipts) == 1
    assert {r[2] for r in requests if r[1] == "submit"} == {job_id("unknown")}
    assert any(job_id("unknown") in (m.content or "") for m in resumed.seen)
    from tests.test_transcript_reconstructs_requests import _rebuild_and_check
    assert _rebuild_and_check(state) == 2


@pytest.mark.asyncio
async def test_pending_wait_repairs_the_original_tool_result_before_the_next_user_message(
    tmp_path, bridge,
):
    from core.agent_loop import _persist_messages_checkpoint
    from core.conversation_store import unresolved_tool_calls

    state = state_at(tmp_path)
    receipt = await execute("remote_job", state, action="submit", resource_id="r",
                            command="echo original", idempotency_key="repair", timeout=10)
    call_id = "original-wait-call"
    state.hook_state["_current_tool_call_id_being_executed"] = call_id
    await execute("remote_job", state, action="wait", resource_id="r", job_id=receipt["job_id"])
    messages = [LLMMessage(role="assistant", tool_calls=[{
        "id": call_id, "type": "function",
        "function": {"name": "remote_job", "arguments": json.dumps({"action": "wait"})},
    }]), LLMMessage(role="user", content="system continuation transport")]
    _persist_messages_checkpoint(state, messages, turn=1)
    import chat
    chat._repair_message_tool_protocol(messages)
    assert unresolved_tool_calls(messages) == {call_id: "remote_job"}
    await job_wait.resume(state, messages, turn=2)
    assert unresolved_tool_calls(messages) == {}
    assert messages[1].role == "tool" and messages[1].tool_call_id == call_id
    assert json.loads(messages[1].content)["job_id"] == receipt["job_id"]
    assert messages[2].role == "user"


@pytest.mark.asyncio
@pytest.mark.parametrize("placeholder", [False, True])
@pytest.mark.parametrize("crash_before_ack", [False, True])
async def test_original_result_restoration_and_ack_loss_preserve_request_replay(
    tmp_path, bridge, monkeypatch, placeholder, crash_before_ack,
):
    from core.agent_loop import load_messages_checkpoint
    from tests.test_transcript_reconstructs_requests import _rebuild_and_check

    state = state_at(tmp_path)
    receipt = await execute("remote_job", state, action="submit", resource_id="r",
                            command="echo replay-once", idempotency_key="replay", timeout=10)
    call_id = "wait-for-original-replay"
    state.hook_state["_current_tool_call_id_being_executed"] = call_id
    arguments = dict(action="wait", resource_id="r", job_id=receipt["job_id"])
    job_wait.register(state, "remote_job", arguments,
                      {key: receipt[key] for key in
                       ("project_id", "resource_id", "job_id", "request_digest")})
    state.hook_state.pop("_current_tool_call_id_being_executed", None)
    messages = [LLMMessage(role="user", content="original authorized task"),
                LLMMessage(role="assistant", tool_calls=[{
                    "id": call_id, "type": "function",
                    "function": {"name": "remote_job", "arguments": json.dumps(arguments)},
                }])]
    if placeholder:
        messages.append(LLMMessage(role="tool", name="remote_job", tool_call_id=call_id,
                                   content=json.dumps({"status": "deferred", "outcome": "unknown",
                                                       "do_not_resubmit": True})))
    llm = ScriptedLLM(reply(content="original result consumed"))
    if crash_before_ack:
        actual_ack = job_wait.acknowledge
        def crash(_):
            raise RuntimeError("crash after result checkpoint")
        monkeypatch.setattr(job_wait, "acknowledge", crash)
        with pytest.raises(RuntimeError, match="after result checkpoint"):
            await run_loop(harness(), state, messages, llm)
        assert llm.calls == 0
        messages, _ = load_messages_checkpoint(state)
        requests, _ = bridge
        before = len(requests)
        monkeypatch.setattr(job_wait, "acknowledge", actual_ack)
        await run_loop(harness(), state, messages, llm)
        assert len(requests) == before, "committed result was re-observed after lost ack"
    else:
        await run_loop(harness(), state, messages, llm)
    assert _rebuild_and_check(state) == 1
    assert len([m for m in messages if m.role == "tool" and m.tool_call_id == call_id]) == 1
    assert len([r for r in bridge[0] if r[1] == "submit"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("future_name", ["read_artifact", "remote_job"])
async def test_a_blocking_wait_proves_later_calls_in_its_batch_were_not_dispatched(
    tmp_path, bridge, future_name,
):
    from core.conversation_store import unresolved_tool_calls
    from tests.test_transcript_reconstructs_requests import _rebuild_and_check

    state = state_at(tmp_path)
    receipt = await execute("remote_job", state, action="submit", resource_id="r",
                            command="echo batch-once", idempotency_key="batch", timeout=10)
    wait_id, future_id = "original-blocking-wait", "not-yet-dispatched"
    arguments = dict(action="wait", resource_id="r", job_id=receipt["job_id"])
    state.hook_state["_current_tool_call_id_being_executed"] = wait_id
    job_wait.register(state, "remote_job", arguments,
                      {key: receipt[key] for key in
                       ("project_id", "resource_id", "job_id", "request_digest")})
    state.hook_state.pop("_current_tool_call_id_being_executed", None)
    messages = [LLMMessage(role="assistant", tool_calls=[
        {"id": wait_id, "type": "function",
         "function": {"name": "remote_job", "arguments": json.dumps(arguments)}},
        {"id": future_id, "type": "function",
         "function": {"name": future_name, "arguments": "{}"}},
    ])]
    import chat
    chat._repair_message_tool_protocol(messages)
    llm = ScriptedLLM(reply(content="automatic downstream progress"))
    result = await run_loop(harness(), state, messages, llm)
    assert result.status == "completed" and llm.calls == 1
    assert not unresolved_tool_calls(messages)
    deferred = next(m for m in messages if m.tool_call_id == future_id)
    assert json.loads(deferred.content)["executed"] is False
    assert len([r for r in bridge[0] if r[1] == "submit"]) == 1
    assert _rebuild_and_check(state) == 1


@pytest.mark.asyncio
async def test_an_unmanaged_unknown_external_effect_stops_without_repeating(tmp_path, monkeypatch):
    from core import tool_registry
    from core.agent_loop import load_messages_checkpoint
    from core.conversation_store import UnconfirmedToolOutcome

    bootstrap()
    state = state_at(tmp_path)
    effect = tmp_path / "physical-starts"
    async def unsafe(*, state, **kwargs):
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-c",
            "import pathlib,sys; p=pathlib.Path(sys.argv[1]); "
            "p.write_text((p.read_text() if p.exists() else '')+'one\\n')", str(effect),
        )
        await process.wait()
        raise asyncio.CancelledError("external action accepted, receipt unknown")
    monkeypatch.setitem(tool_registry._REGISTRY.executors, "remote_exec", unsafe)
    node = NodeHarness(node_type="_orchestrator", system_prompt="test", tools=["remote_exec"], max_turns=2)
    llm = ScriptedLLM(reply(("remote_exec", {"resource_id": "r", "command": "unmanaged"})))
    with pytest.raises(asyncio.CancelledError):
        await run_loop(node, state, [LLMMessage(role="user", content="authorized action")], llm)
    messages, _ = load_messages_checkpoint(state)
    import chat
    chat._repair_message_tool_protocol(messages)
    resumed = ScriptedLLM(reply(content="must not run"))
    result = await run_loop(node, state, messages, resumed)
    assert result.status == "completed" and resumed.calls == 1
    assert effect.read_text() == "one\n"
    rejected = await execute("remote_exec", state, resource_id="r", command="resubmit")
    assert rejected["error_code"] == "unconfirmed_tool_outcome" and rejected["executed"] is False
    assert effect.read_text() == "one\n"


@pytest.mark.asyncio
async def test_an_old_result_with_a_reused_call_id_cannot_confirm_a_new_effect(tmp_path):
    from core.agent_loop import _persist_messages_checkpoint
    from core.conversation_store import UnconfirmedToolOutcome

    bootstrap()
    state = state_at(tmp_path)
    call_id = "provider-reused-id"
    state.append_transcript("tool_result", name="remote_exec", tool_call_id=call_id,
                            content=json.dumps({"status": "success", "stdout": "older operation"}))
    messages = [LLMMessage(role="assistant", tool_calls=[{
        "id": call_id, "type": "function", "function": {"name": "remote_exec",
        "arguments": json.dumps({"resource_id": "r", "command": "new unmanaged effect"})},
    }])]
    state.hook_state["_current_tool_call_id_being_executed"] = call_id
    _persist_messages_checkpoint(state, messages, turn=1)
    state.hook_state.pop("_current_tool_call_id_being_executed")
    llm = ScriptedLLM(reply(content="must not run"))
    await run_loop(harness(), state, messages, llm)
    assert llm.calls == 1
    assert state.hook_state["_unconfirmed_tool_calls"] == {call_id: "remote_exec"}

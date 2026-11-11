"""Historical receipts must be readable without replaying their effects."""
import json
import re

import pytest

import shared.tools  # noqa: F401
from core.context_view import ContextView, get
from core.llm import LLMMessage
from core.loader import load_harness
from core.state import State
from core.tool_registry import execute


@pytest.mark.asyncio
async def test_evicted_remote_submit_keeps_job_state_and_reads_without_resubmit(tmp_path, monkeypatch):
    from core import remote_resources

    calls = []

    async def handler(state, action, params):
        calls.append((action, params))
        return dict(status="success", resource_id="r", job_id="a" * 32,
                    state="running", exit_code=None, stdout_tail="long log" * 4000)

    monkeypatch.setattr(remote_resources, "_handler", handler)
    state = State.new("_orchestrator", tmp_path)
    args = dict(action="submit", resource_id="r", command="compute", idempotency_key="one")
    result = await execute("remote_job", state, **args)
    body = json.dumps(result, ensure_ascii=False)
    view = get(state)
    view.note_result(tool_name="remote_job", args=args, tool_call_id="c1", content=body,
                     turn=1, replayable=False)
    messages = [LLMMessage(role="tool", name="remote_job", tool_call_id="c1", content=body)]
    view.enforce_budget(messages, max_bytes=1)
    rendered = view.apply(messages)[0].content
    assert '"state": "running"' in rendered and '"exit_code": null' in rendered
    assert "long log" not in rendered
    ref = re.search(r'read_tool_result\(result_ref="([^"]+)"', rendered)[1]
    before = (state.root / "tool_results.jsonl").read_bytes()
    pieces, offset = [], 0
    while True:
        receipt = await execute("read_tool_result", state, result_ref=ref, offset=offset, limit=9000)
        assert receipt["historical"] is True and receipt["tool_name"] == "remote_job"
        pieces.append(receipt["content"])
        if not receipt["truncated"]:
            break
        offset = receipt["next_offset"]
    assert "".join(pieces) == body
    assert len(calls) == 1, "Reading a receipt resubmitted an effect"
    assert (state.root / "tool_results.jsonl").read_bytes() == before


def test_references_identify_exact_unicode_record_across_restart_and_reused_call_ids(tmp_path):
    state = State.new("experiment", tmp_path)
    view = get(state)
    refs = []
    for content in ("作业仍在运行", "超时：17/40", "失败详情" * 9000):
        view.note_result(tool_name="remote_job", args={"job_id": "x"}, tool_call_id="reused",
                         content=content, turn=1, replayable=False, ok=False)
        refs.append(view.entries["reused"]["result_ref"])
    restored = ContextView(state)
    assert len(set(refs)) == 3
    assert restored.read_result(refs[0])["content"] == "作业仍在运行"
    assert restored.read_result(refs[1])["content"] == "超时：17/40"
    first = restored.read_result(refs[2])
    second = restored.read_result(refs[2], offset=first["next_offset"])
    third = restored.read_result(refs[2], offset=second["next_offset"])
    assert first["content"] + second["content"] + third["content"] == "失败详情" * 9000
    original = LLMMessage(role="tool", name="remote_job", tool_call_id="reused", content="作业仍在运行")
    restored.adopt([original])
    # An incomplete inherited history cannot establish (tool, args), so it
    # must not claim another record of the same tool is its backup.
    assert restored.entries["reused"]["logged"] is False
    restored = ContextView(state)
    assistant = LLMMessage(role="assistant", tool_calls=[dict(
        id="reused", type="function", function=dict(
            name="remote_job", arguments=json.dumps({"job_id": "x"})))])
    restored.adopt([assistant, original])
    assert restored.entries["reused"]["result_ref"] == refs[0]
    # An older message with the reused ID must never be rendered using the
    # latest call's state or receipt, even if the latest entry is evicted.
    view.enforce_budget([original], max_bytes=1)
    assert view.apply([original])[0].content == "作业仍在运行"


@pytest.mark.asyncio
async def test_historical_result_read_uses_the_normal_secret_redaction_boundary(tmp_path):
    from core.secrets import register_secret

    state = State.new("experiment", tmp_path)
    view = get(state)
    secret = "receipt-test-only-secret-772caf1"
    register_secret(secret)
    view.note_result(tool_name="remote_job", args={}, tool_call_id="c1", content=secret,
                     turn=1, replayable=False)
    result = await execute("read_tool_result", state, result_ref=view.entries["c1"]["result_ref"])
    assert result["status"] == "success" and secret not in json.dumps(result)


@pytest.mark.asyncio
async def test_foreign_run_and_tampered_receipts_are_not_read(tmp_path):
    state = State.new("experiment", tmp_path / "one")
    view = get(state)
    view.note_result(tool_name="remote_job", args={}, tool_call_id="c1", content="private result",
                     turn=1, replayable=False)
    ref = view.entries["c1"]["result_ref"]
    other = State.new("experiment", tmp_path / "two")
    rejected = await execute("read_tool_result", other, result_ref=ref)
    assert rejected["status"] == "error" and "private result" not in str(rejected)
    log = state.root / "tool_results.jsonl"
    log.write_bytes(log.read_bytes().replace(b"private result", b"changed result"))
    rejected = await execute("read_tool_result", state, result_ref=ref)
    assert rejected["status"] == "error" and "integrity" in rejected["error"]


@pytest.mark.parametrize("node", ["_orchestrator", "_reviewer", "experiment", "writing", "data"])
def test_context_recovery_path_is_exposed_to_system_and_producing_nodes(node):
    assert "read_tool_result" in load_harness(node).tools

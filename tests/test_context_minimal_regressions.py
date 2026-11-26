"""Counterexamples from the context audit, using the real run store and renderer."""

import copy
import json
import shutil
from types import SimpleNamespace

import pytest

from core import context_view as cv
from core import jobs
from core import summarizer as sm
from core.context_lifecycle import target_tokens
from core.conversation_store import render_context_change
from core.harness import NodeHarness, SummarizerConfig
from core.llm import LLMClient, LLMMessage, LLMResponse, framework_notice
from core.state import State


DECISION = "方法 A 仅在高密度及当前步长失败；更小步长尚未测试。"
LEDGER = (
    "## 目标\n继续核验协议\n## 已完成\n检查记录：result.json\n"
    "## 未决与下一步\n测试更小步长\n## 决策与理由\n" + DECISION
)


class Summary:
    def __init__(self, text=LEDGER):
        self.text, self.calls = text, []

    async def chat(self, messages, **kwargs):
        self.calls.append(messages)
        return LLMResponse(content=self.text, tool_calls=[], finish_reason="stop", usage={})


def history(prefix="", n=20):
    messages = [LLMMessage(role="system", content="Research charter"),
                LLMMessage(role="user", content="Preserve the scientific protocol.")]
    for i in range(n):
        call_id = f"{prefix}call-{i}"
        messages += [
            LLMMessage(role="assistant", reasoning_content="历史推理。" * 500,
                       tool_calls=[{"id": call_id, "type": "function", "function": {
                           "name": "save_artifact", "arguments": json.dumps(
                               {"content": "原始文档。" * 500}, ensure_ascii=False)}}]),
            LLMMessage(role="tool", name="save_artifact", tool_call_id=call_id,
                       content='{"status":"success"}'),
        ]
    return messages


def harness(window=25_000, keep=6):
    return NodeHarness(node_type="literature", max_context_tokens=window,
                       max_output_tokens=4096,
                       summarizer=SummarizerConfig(keep_last_n_turns=keep))


def read_all(view, reference):
    chunks, offset = [], 0
    while True:
        page = view.read_result(reference, offset=offset)
        chunks.append(page["content"])
        if not page["truncated"]:
            return "".join(chunks)
        offset = page["next_offset"]


@pytest.mark.asyncio
async def test_archive_keeps_the_successful_ledger_and_its_scientific_conditions(tmp_path):
    state, messages, llm = State.new(node_type="literature", base_dir=tmp_path), history(), Summary()
    messages[2].content = DECISION
    result = await sm.run_summarizer(harness(), state, messages, llm, 21, sm.estimate_tokens(messages))
    assert llm.calls
    assert state.hook_state["_context_compaction"]["strategy"] == "archive"
    assert state.hook_state["_context_compaction"]["target_met"]
    assert any(DECISION in (message.content or "") for message in result)


@pytest.mark.asyncio
async def test_every_archive_remains_directly_discoverable_after_repeated_compaction(tmp_path):
    state, h, llm = State.new(node_type="literature", base_dir=tmp_path), harness(), Summary()
    messages = history()
    result = await sm.run_summarizer(h, state, messages, llm, 21, sm.estimate_tokens(messages))
    first = state.hook_state["_context_compaction"]["history_ref"]
    result = await sm.run_summarizer(h, state, result + history("next-")[2:], llm, 42, 1)
    view = cv.ContextView(state)  # no in-memory index survives this restart
    index_ref = view.history_index_ref()
    assert any(index_ref in (message.content or "") for message in result)
    index = json.loads(read_all(view, index_ref))
    assert first in {entry["result_ref"] for entry in index}
    assert json.loads(read_all(view, first))[0]["content"] == "Research charter"


@pytest.mark.asyncio
async def test_rejected_candidate_does_not_commit_its_eviction_decisions(tmp_path, monkeypatch):
    async def identity(ctx):
        return ctx.messages

    monkeypatch.setitem(sm._STRATEGIES, "audit_identity", identity)
    state = State.new(node_type="literature", base_dir=tmp_path)
    view = cv.get(state)
    calls = [{"id": name, "type": "function", "function": {
        "name": tool, "arguments": "{}"}}
        for name, tool in [("read", "audit_read"), ("human", "request_human_input")]]
    body = "a" * 1200  # a tombstone costs more tokens than this compressible result
    messages = [LLMMessage(role="system", content="charter"),
                LLMMessage(role="user", content="核心约束。" * 1000),
                LLMMessage(role="assistant", tool_calls=calls),
                LLMMessage(role="tool", name="audit_read", tool_call_id="read", content=body),
                LLMMessage(role="tool", name="request_human_input", tool_call_id="human",
                           content="continue", origin="caller")]
    view.note_result(tool_name="audit_read", args={}, tool_call_id="read", content=body,
                     turn=1, replayable=True)
    before = copy.deepcopy(view.entries), dict(view.live)
    h = harness(8000)
    h.summarizer.strategy = "audit_identity"
    result = await sm.run_summarizer(h, state, messages, None, 10, sm.estimate_tokens(messages))
    render_context_change(state, messages, result, turn=10, source="test")
    assert state.hook_state["_context_compaction"]["strategy"] == "unchanged"
    assert any(m.tool_call_id == "read" and m.content == body for m in messages)
    assert (view.entries, view.live) == before


def test_summary_input_includes_tool_parameters_and_tail_failures():
    messages = [LLMMessage(role="assistant", tool_calls=[{
        "id": "c", "function": {"name": "simulate", "arguments": "x" * 500 + DECISION}}]),
        LLMMessage(role="tool", name="simulate", tool_call_id="c",
                   content="x" * 1000 + DECISION)]
    rendered = sm._render_messages_for_compression(messages)
    assert rendered.count(DECISION) == 2


def test_current_job_snapshot_retains_actionable_identity(tmp_path, monkeypatch):
    state = State.new(node_type="analysis", base_dir=tmp_path / "runs")
    state.project_root = tmp_path / "project"
    monkeypatch.setattr(jobs, "observed_gpus", lambda: {})
    monkeypatch.setattr(jobs, "resolve_live_pids", lambda job: ([job.pid], "declared"))
    job = jobs.declare(state, purpose="Numerical convergence", pid=12345, scheduler_job_id="slurm-987")
    rendered = jobs.render_for_orchestrator(state)
    assert job.job_id in rendered
    assert "pid=12345" in rendered
    assert "scheduler_job_id=slurm-987" in rendered
    assert f"run_id={state.run_id}" in rendered


@pytest.mark.asyncio
async def test_summary_respects_its_own_known_provider_window(tmp_path):
    state, llm, h = State.new(node_type="literature", base_dir=tmp_path), Summary(), harness()
    state.hook_state[sm.PROVIDER_HARD_CAP_KEY] = 8000
    messages = history()
    ctx = sm.SummarizerContext(h, state, messages, sm.estimate_tokens(messages), llm, 21)
    assert await sm._strategy_llm(ctx) == messages
    assert ctx.summary_failure == "summary_input_exceeds_budget"
    assert not llm.calls


@pytest.mark.asyncio
async def test_failed_update_retains_the_previous_scientific_ledger(tmp_path):
    state, llm, h = State.new(node_type="literature", base_dir=tmp_path), Summary("All done."), harness()
    old = sm.build_compression_notice(1, "old", sm._COMPRESSION_SUMMARY_HEADER + "\n" + LEDGER)
    old.context_slot = None  # legacy persisted notice predating the ledger slot
    messages = history()
    messages.insert(2, old)
    result = await sm.run_summarizer(h, state, messages, llm, 21, sm.estimate_tokens(messages))
    ledgers = [m for m in result if sm._COMPRESSION_SUMMARY_HEADER in (m.content or "")]
    assert len(ledgers) == 1
    assert DECISION in ledgers[0].content
    assert state.hook_state["_context_compaction"]["target_met"]


@pytest.mark.asyncio
async def test_custom_strategy_cannot_mutate_source_messages(tmp_path, monkeypatch):
    async def corrupt(ctx):
        ctx.messages[1].content = "discard the caller's constraints"
        return ctx.messages

    monkeypatch.setitem(sm._STRATEGIES, "audit_corrupt", corrupt)
    state, messages, h = State.new(node_type="literature", base_dir=tmp_path), history(), harness()
    h.summarizer.strategy = "audit_corrupt"
    before = copy.deepcopy(messages)
    result = await sm.run_summarizer(h, state, messages, None, 21, 1)
    assert messages == before
    assert any(m.content == before[1].content for m in result)


def test_legacy_v1_receipts_remain_readable(tmp_path):
    state = State.new(node_type="literature", base_dir=tmp_path)
    old = cv.ContextView(SimpleNamespace(root=state.root))
    reference = old.archive_history(history(), turn=1)
    assert reference.startswith("v1:")
    assert json.loads(read_all(cv.ContextView(state), reference))[0]["content"] == "Research charter"


def test_committed_tool_snippet_is_not_reported_as_a_live_full_copy(tmp_path):
    state, body = State.new(node_type="literature", base_dir=tmp_path), "original full receipt"
    view = cv.get(state)
    view.note_result(tool_name="audit_read", args={}, tool_call_id="read", content=body,
                     turn=1, replayable=True)
    messages = [LLMMessage(role="tool", name="audit_read", tool_call_id="read", content=body)]
    candidate = [LLMMessage(role="tool", name="audit_read", tool_call_id="read", content="short snippet")]
    render_context_change(state, messages, candidate, turn=2, source="test")
    assert not view.is_live("audit_read", {})
    assert view.read_result(view.entries["read"]["result_ref"])["content"] == body


@pytest.mark.asyncio
async def test_scientific_snapshot_is_not_used_to_delete_a_supported_answer(monkeypatch):
    from core.llm import LLMClient, framework_notice

    conclusion = "The runtime claim failed: measured wall time was 8.85 hours against a two-hour budget."
    text = conclusion + "\nThe accuracy claim must be assessed separately."
    async def response(self, payload, **kwargs):
        return {"choices": [{"message": {"content": text}, "finish_reason": "stop"}], "usage": {}}

    monkeypatch.setenv("LLM_STREAM", "0")
    monkeypatch.setattr(LLMClient, "_post_with_retry", response)
    snapshot = framework_notice(conclusion)
    snapshot.context_slot = "research_state"
    client = LLMClient(api_key="test", model="test", base_url="https://example.invalid")
    result = await client.chat([LLMMessage(role="system", content="Scientific analyst"), snapshot,
                                LLMMessage(role="user", content="State the conclusion.")])
    assert result.content == text


@pytest.mark.asyncio
@pytest.mark.parametrize("slot", ["research_state", "continuation_ledger", "tool_findings", "scratchpad"])
@pytest.mark.parametrize("structured", [False, True])
async def test_answer_may_repeat_scientific_facts_from_a_snapshot(monkeypatch, slot, structured):
    fact = '  "tc": 2.2692871655836164,' if structured else "Recorded wall time was 8.852 hours against a two hour budget."
    content = ('{\n' + fact + '\n  "time_budget_pass": false\n}') if structured else fact + "\nThe next step is to explain the uncertainty."
    snapshot = framework_notice(fact)
    snapshot.context_slot = slot

    async def response(self, *args, **kwargs):
        return {"choices": [{"message": {"content": content}, "finish_reason": "stop"}], "usage": {}}

    monkeypatch.setenv("LLM_STREAM", "off")
    monkeypatch.setattr(LLMClient, "_post_with_retry", response)
    client = LLMClient(api_key="test", model="test", base_url="http://test")
    answer = await client.chat([LLMMessage(role="system", content="Explain the evidence."), snapshot])
    assert answer.content == content


@pytest.mark.asyncio
async def test_node_instructions_cannot_bypass_the_shared_ledger_contract(tmp_path):
    state, llm, h = State.new(node_type="literature", base_dir=tmp_path), Summary("All done."), harness(100_000)
    h.summarizer.instruction = "Preserve the project's special requirements."
    result = await sm.run_summarizer(h, state, history(), llm, 21, 1)
    assert len(llm.calls) == 2
    assert not any("All done." in (m.content or "") for m in result)
    assert state.hook_state["_context_compaction"]["reason"] == "invalid_ledger"


def test_output_reserve_participates_in_the_compression_trigger(tmp_path):
    state, h = State.new(node_type="literature", base_dir=tmp_path), harness(20_000)
    h.max_output_tokens = 12_000
    messages = [LLMMessage(role="system", content="charter"),
                LLMMessage(role="user", content="task"),
                LLMMessage(role="assistant", content="word " * 6000)]
    assert sm.estimate_tokens(messages) > target_tokens(h, state)
    assert sm.should_compress(h, messages, 20, state)[0]


def test_provider_observations_above_three_are_not_silently_clipped(tmp_path):
    state = State.new(node_type="literature", base_dir=tmp_path)
    sm.note_observed_prompt_tokens(state, local_estimate=4000, server_prompt_tokens=20_000)
    assert sm.effective_prompt_tokens(4000, state) >= 20_000


def test_offline_token_estimator_is_conservative_for_chinese(monkeypatch):
    messages = [LLMMessage(role="user", content="实验结果显示温度改变了相变位置。" * 100)]
    reference = sm.estimate_tokens(messages)
    monkeypatch.setattr(sm, "_TIKTOKEN_ENC", False)
    assert sm.estimate_tokens(messages) >= reference


def test_tool_writer_never_advertises_an_unreadable_oversized_record(tmp_path, monkeypatch):
    state, body = State.new(node_type="literature", base_dir=tmp_path), "x" * 600
    monkeypatch.setattr(cv, "_MAX_RECORD_BYTES", 200)
    view = cv.get(state)
    view.note_result(tool_name="effectful", args={}, tool_call_id="c", content=body,
                     turn=1, replayable=False)
    entry = view.entries["c"]
    assert entry["result_ref"] is None
    assert not entry["logged"]


def test_candidate_v2_receipt_survives_relocation_but_not_another_run(tmp_path, monkeypatch):
    # Pre-release v2 records remain readable; production writes stay v1 for
    # rollback compatibility with the currently deployed reader.
    monkeypatch.setattr(cv.ContextView, "_reference_version", lambda self: "v2")
    state = State.new(node_type="literature", base_dir=tmp_path / "original")
    view = cv.get(state)
    view.note_result(tool_name="probe", args={}, tool_call_id="c", content=DECISION,
                     turn=1, replayable=True)
    reference = view.entries["c"]["result_ref"]
    moved = SimpleNamespace(root=tmp_path / "moved", hook_state={}, run_id=state.run_id,
                            tenant_id=state.tenant_id, project_id=state.project_id,
                            session_id=state.session_id)
    moved_view = cv.ContextView(moved)
    shutil.copyfile(view._log(), moved_view._log())
    assert moved_view.read_result(reference)["content"] == DECISION
    moved.run_id = "a-different-run"
    with pytest.raises(ValueError):
        cv.ContextView(moved).read_result(reference)


def test_receipt_pages_revalidate_content_even_when_file_metadata_does_not_change(tmp_path, monkeypatch):
    state = State.new(node_type="literature", base_dir=tmp_path)
    view = cv.get(state)
    view.note_result(tool_name="probe", args={}, tool_call_id="c", content="abcdef" * 1000,
                     turn=1, replayable=True)
    reference = view.entries["c"]["result_ref"]
    from pathlib import Path
    log_path = view._log()
    real_stat, unchanged_stat = Path.stat, log_path.stat()

    def coarse_stat(path, *args, **kwargs):
        # Some filesystems/clock ticks give a same-size rewrite the same
        # timestamps. Integrity must depend on bytes, not metadata freshness.
        return unchanged_stat if path == log_path else real_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", coarse_stat)
    assert view.read_result(reference, limit=100)["content"] == "abcdef" * 16 + "abcd"
    assert view.read_result(reference, offset=100, limit=100)["content"] == ("abcdef" * 1000)[100:200]
    raw = view._log().read_bytes()
    corrupted = raw.replace(b"abcdef", b"abcdeg", 1)
    assert corrupted != raw and len(corrupted) == len(raw)
    view._log().write_bytes(corrupted)
    with pytest.raises(ValueError):
        # Corruption outside this page still invalidates the whole receipt.
        view.read_result(reference, offset=100, limit=100)

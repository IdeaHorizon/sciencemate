"""History is durable; the submitted working set has a verifiable budget."""

import json

import pytest

from core import summarizer as sm
from core.context_lifecycle import RequestContext
from core.context_view import ContextView
from core.conversation_store import msg_to_jsonable, unresolved_tool_calls
from core.harness import NodeHarness, SummarizerConfig
from core.llm import LLMMessage, LLMResponse, framework_notice
from core.state import State


def history(n=20, repeats=500):
    messages = [
        LLMMessage(role="system", content="Research charter"),
        LLMMessage(role="user", content="Preserve the scientific protocol."),
    ]
    for i in range(n):
        messages += [
            LLMMessage(
                role="assistant",
                reasoning_content="历史推理。" * repeats,
                tool_calls=[
                    {
                        "id": f"c{i}",
                        "type": "function",
                        "function": {
                            "name": "save_artifact",
                            "arguments": json.dumps(
                                {"content": "原始文档。" * repeats}, ensure_ascii=False
                            ),
                        },
                    }
                ],
            ),
            LLMMessage(
                role="tool",
                name="save_artifact",
                tool_call_id=f"c{i}",
                content='{"status":"success"}',
            ),
        ]
    return messages


class UnavailableSummary:
    def __init__(self, mode="empty"):
        self.budgets = []
        self.mode = mode

    async def chat(self, messages, **kwargs):
        self.budgets.append(kwargs["max_tokens"])
        if self.mode == "error":
            raise RuntimeError("offline provider failure")
        return LLMResponse(
            content="",
            tool_calls=[],
            finish_reason="length",
            reasoning_content="reasoning without a ledger",
            usage={"total_tokens": 4000, "completion_tokens_details": {"reasoning_tokens": 4000}},
        )


def harness(window=100_000):
    return NodeHarness(
        node_type="literature",
        max_context_tokens=window,
        max_output_tokens=4096,
        summarizer=SummarizerConfig(),
    )


def events(state, event):
    return [
        r
        for line in state.transcript_path.read_text().splitlines()
        if (r := json.loads(line)).get("event") == event
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["empty", "error"])
async def test_failed_summary_projects_complete_history_with_restart_safe_receipt(tmp_path, mode):
    state = State.new(node_type="literature", base_dir=tmp_path)
    messages = history()
    # Genuine steering is never truncated, counted off, or paraphrased.
    steering = LLMMessage(role="user", content="Keep every constraint. " * 400)
    messages.insert(8, steering)
    expected = [msg_to_jsonable(m) for m in messages]
    llm = UnavailableSummary(mode)
    result = await sm.run_summarizer(
        harness(), state, messages, llm, 21, sm.estimate_tokens(messages)
    )
    assert steering in result
    assert sum(m.role == "assistant" for m in result) == 3
    assert not unresolved_tool_calls(result)
    outcome = state.hook_state["_context_compaction"]
    assert outcome["strategy"] == "archive"
    assert outcome["status"] == "ready"
    assert outcome["target_met"]
    assert outcome["reason"] in {"truncated", "RuntimeError"}
    assert len(llm.budgets) == (2 if mode == "empty" else 1)
    if mode == "empty":
        assert llm.budgets[1] > llm.budgets[0]
    # A fresh object has no in-memory cache. Recover the exact pre-compaction
    # record through the same bounded reader exposed to every node.
    reader, text, offset = ContextView(state), "", 0
    while True:
        page = reader.read_result(outcome["history_ref"], offset=offset)
        text += page["content"]
        if not page["truncated"]:
            break
        offset = page["next_offset"]
    assert json.loads(text) == expected
    assert len(events(state, "context_summary_attempt")) == (2 if mode == "empty" else 1)
    assert events(state, "summarizer_compress")[-1]["strategy"] == "archive"
    assert not events(state, "session_rotation_advised")


@pytest.mark.asyncio
async def test_storage_failure_cannot_commit_a_projection(tmp_path, monkeypatch):
    state = State.new(node_type="literature", base_dir=tmp_path)
    monkeypatch.setattr(ContextView, "archive_history", lambda *a, **kw: None)
    messages, llm = history(), UnavailableSummary()
    assert await sm.run_summarizer(harness(), state, messages, llm, 20, 1) is messages
    assert not llm.budgets
    assert state.hook_state["_context_compaction"]["reason"] == "history_storage_unavailable"
    assert "_context_projection_signature" not in state.hook_state
    assert sm.should_compress(harness(), messages, 21, state)[0]


def test_archives_obey_the_readers_encoded_record_bound(tmp_path, monkeypatch):
    from core import context_view

    state = State.new(node_type="literature", base_dir=tmp_path)
    monkeypatch.setattr(context_view, "_MAX_RECORD_BYTES", 200)
    # Nested JSON escaping expands a small original document substantially.
    assert (
        ContextView(state).archive_history(
            [LLMMessage(role="assistant", content='"' * 100)], turn=1
        )
        is None
    )


def test_an_interrupted_log_tail_does_not_poison_new_archive_references(tmp_path):
    state = State.new(node_type="literature", base_dir=tmp_path)
    path = state.root / "tool_results.jsonl"
    fragment = b'{"content":"interrupted'
    path.write_bytes(fragment)
    reference = ContextView(state).archive_history(history(1), turn=1)
    assert reference
    recovered = ContextView(state).read_result(reference)
    assert json.loads(recovered["content"]) == [msg_to_jsonable(m) for m in history(1)]
    assert path.read_bytes().startswith(fragment + b"\n")


@pytest.mark.asyncio
async def test_unconfirmed_effectful_call_and_every_caller_constraint_are_retained(tmp_path):
    state = State.new(node_type="literature", base_dir=tmp_path)
    messages = history(8)
    caller_messages = [LLMMessage(role="user", content=f"constraint-{i}" * 1000) for i in range(20)]
    messages[3:3] = caller_messages
    pending = LLMMessage(
        role="assistant",
        tool_calls=[
            {
                "id": "unconfirmed",
                "type": "function",
                "function": {"name": "submit_job", "arguments": '{"job":"original"}'},
            }
        ],
    )
    messages.append(pending)
    result = await sm.run_summarizer(
        harness(window=8000), state, messages, UnavailableSummary(), 9, 1
    )
    assert all(m in result for m in caller_messages)
    assert pending in result
    assert unresolved_tool_calls(result) == {"unconfirmed": "submit_job"}
    assert state.hook_state["_context_compaction"]["status"] == "irreducible"


def test_measurement_is_frozen_and_learning_can_correct_a_conservative_prior(tmp_path):
    state = State.new(node_type="literature", base_dir=tmp_path)
    state.hook_state["_tool_schema_tokens"] = 1000
    messages, h = history(1), harness()
    request = RequestContext.measure(h, state, messages, output_tokens=4096)
    before = request.budget_tokens
    for _ in range(20):
        sm.note_observed_prompt_tokens(state, local_estimate=10000, server_prompt_tokens=7000)
    assert 0.7 <= sm.effective_calibration(state) < 1
    assert request.budget_tokens == before
    report = sm.context_window_report(
        h, state, messages, server_prompt_tokens=5000, request=request
    )
    assert report["effective_tokens"] == before
    assert report["prompt_tokens"] == 5000
    assert report["reserved_output_tokens"] == 4096
    assert report["window"] == h.max_context_tokens


def test_model_change_resets_the_observation_scope(tmp_path):
    state = State.new(node_type="literature", base_dir=tmp_path)
    client = type("Client", (), {"base_url": "endpoint", "model": "first"})()
    sm.bind_context_model(state, client)
    sm.note_observed_prompt_tokens(state, local_estimate=10000, server_prompt_tokens=7000)
    client.model = "second"
    sm.bind_context_model(state, client)
    assert "_ctx_prompt_samples" not in state.hook_state


def test_known_capacity_admission_overrides_a_non_growth_cooldown(tmp_path):
    state, messages, h = (
        State.new(node_type="literature", base_dir=tmp_path),
        history(3),
        harness(20_000),
    )
    state.hook_state[sm._LAST_COMPRESS_EST_KEY] = sm.estimate_tokens(messages)
    state.hook_state[sm._THRASH_GUARD_KEY] = 100
    state.hook_state[sm.PROVIDER_HARD_CAP_KEY] = 20_000
    assert sm.should_compress(h, messages, 20, state)[0]


@pytest.mark.asyncio
async def test_compacted_request_can_be_replayed_exactly_from_its_journal(tmp_path):
    from core.agent_loop import digest_messages, run_loop
    from core.conversation_store import apply_context_patch, jsonable_to_msg

    class MainModel:
        def spawn_silent(self):
            return UnavailableSummary()

        async def chat(self, messages, **kwargs):
            return LLMResponse(content="done", tool_calls=[], finish_reason="stop", usage={})

    state = State.new(node_type="literature", base_dir=tmp_path)
    result = await run_loop(harness(), state, history(), MainModel())
    assert result.status == "completed"
    reconstructed = []
    checked = 0
    for line in state.transcript_path.read_text().splitlines():
        record = json.loads(line)
        if record["event"] == "loop_seed":
            reconstructed = [jsonable_to_msg(m) for m in record["messages"]]
        elif record["event"] == "context_rendered":
            reconstructed = apply_context_patch(reconstructed, record["splices"])
        elif record["event"] == "llm_request":
            assert digest_messages(reconstructed)[1] == record["request_digest"]
            checked += 1
    assert checked == 1


@pytest.mark.asyncio
async def test_194_operation_history_recovers_before_the_provider_request(tmp_path):
    """图 3: old assistant reasoning/arguments must not make a 262k run fail."""
    from core.agent_loop import run_loop

    class MainModel:
        calls = 0

        def spawn_silent(self):
            return UnavailableSummary()

        async def chat(self, messages, **kwargs):
            self.calls += 1
            overflow = sm.presend_overflow(
                h, state, messages, output_tokens=kwargs["max_tokens"])[2]
            assert overflow <= 0
            return LLMResponse(content="continued", tool_calls=[], finish_reason="stop", usage={})

    state = State.new(node_type="literature", base_dir=tmp_path)
    h = harness(262144)
    h.max_output_tokens = 32768
    state.hook_state[sm.PROVIDER_HARD_CAP_KEY] = 262144
    messages = history(194, repeats=100)
    steering = LLMMessage(
        role="user", content="Keep the existing evidence; reconcile the same job.")
    messages.append(steering)
    assert sm.presend_overflow(h, state, messages, output_tokens=32768)[2] > 0
    model = MainModel()
    result = await run_loop(h, state, messages, model)
    assert result.status == "completed" and model.calls == 1
    assert steering in result.messages
    assert state.hook_state["_context_compaction"]["strategy"] == "archive"
    assert state.hook_state["_context_compaction"]["target_met"]
    assert not unresolved_tool_calls(result.messages)


@pytest.mark.asyncio
async def test_zero_recent_turns_retains_current_snapshot_slots(tmp_path):
    state, h = State.new(node_type="literature", base_dir=tmp_path), harness()
    h.summarizer.keep_last_n_turns = 0
    messages = history()
    old = framework_notice("obsolete state")
    current = framework_notice("current research decisions and pending obligations")
    old.context_slot = current.context_slot = "task_state"
    messages[3:3] = [old]
    messages.append(current)
    result = await sm.run_summarizer(h, state, messages, UnavailableSummary(), 21, 1)
    assert current in result
    assert old not in result
    assert not unresolved_tool_calls(result)


@pytest.mark.asyncio
async def test_successful_summary_cannot_replace_current_task_state_with_narrative(tmp_path):
    class ValidSummary:
        async def chat(self, messages, **kwargs):
            return LLMResponse(
                content=(
                    "## 目标\n研究\n## 已完成\n已有产物\n"
                    "## 未决与下一步\n继续核对\n## 决策与理由\n以原件为准"
                ),
                tool_calls=[],
                finish_reason="stop",
                usage={},
            )

    state, messages = State.new(node_type="literature", base_dir=tmp_path), history()
    snapshot = framework_notice("Exact latest task state; not a summary.")
    snapshot.context_slot = "task_state"
    messages.insert(4, snapshot)
    result = await sm.run_summarizer(harness(), state, messages, ValidSummary(), 21, 1)
    assert snapshot in result
    assert state.hook_state["_context_compaction"]["strategy"] == "llm"
    assert state.hook_state["_context_compaction"]["status"] == "ready"


@pytest.mark.asyncio
async def test_verbatim_tool_preferences_do_not_create_permanent_memory_exemptions(tmp_path):
    state, h, messages = State.new(node_type="literature", base_dir=tmp_path), harness(), history()
    h.summarizer.keep_tool_calls = ["save_artifact"]
    result = await sm.run_summarizer(h, state, messages, UnavailableSummary(), 21, 1)
    assert sum(m.role == "assistant" for m in result) == 3
    assert state.hook_state["_context_compaction"]["status"] == "ready"
    assert state.hook_state["_context_compaction"]["history_ref"]


@pytest.mark.asyncio
async def test_recent_window_adapts_to_capacity_without_dropping_authority(tmp_path):
    state, h, messages = (
        State.new(node_type="literature", base_dir=tmp_path),
        harness(25_000),
        history(),
    )
    h.summarizer.keep_last_n_turns = 6
    result = await sm.run_summarizer(h, state, messages, UnavailableSummary(), 21, 1)
    assert state.hook_state["_context_compaction"]["target_met"]
    assert sum(m.role == "assistant" for m in result) < 6
    assert messages[0] in result and messages[1] in result
    assert not unresolved_tool_calls(result)


@pytest.mark.asyncio
async def test_human_answers_keep_authority_even_when_transported_as_tool_receipts(tmp_path):
    state, h = State.new(node_type="literature", base_dir=tmp_path), harness()
    h.summarizer.keep_last_n_turns = 0
    question = LLMMessage(
        role="assistant",
        tool_calls=[
            {
                "id": "human-answer",
                "type": "function",
                "function": {
                    "name": "request_human_input",
                    "arguments": '{"question":"Which data?"}',
                },
            }
        ],
    )
    answer = LLMMessage(
        role="tool",
        tool_call_id="human-answer",
        name="request_human_input",
        content='{"status":"success","response":"Only cohort A"}',
        origin="caller",
    )
    messages = history()
    messages[2:2] = [question, answer]
    result = await sm.run_summarizer(h, state, messages, UnavailableSummary(), 21, 1)
    assert question in result and answer in result
    assert not unresolved_tool_calls(result)
    assert state.hook_state["_context_compaction"]["target_met"]


def test_old_answer_provenance_requires_a_recorded_control_plane_resume(tmp_path):
    from core.conversation_store import restore_messages

    state = State.new(node_type="literature", base_dir=tmp_path)
    payload = {
        "status": "success",
        "asked_by": "literature",
        "response": "Only cohort A",
        "answered_by": "person",
        "automatic": False,
    }
    raw = {
        "role": "tool",
        "tool_call_id": "answer",
        "name": "request_human_input",
        "origin": "tool",
        "content": json.dumps(payload),
    }
    assert restore_messages(state, [raw])[0].origin == "tool"
    state.append_transcript("loop_resume", pending_tool_call_id="answer")
    assert restore_messages(state, [raw])[0].origin == "caller"
    automatic = {
        **raw,
        "content": json.dumps({**payload, "answered_by": "auto", "automatic": True}),
    }
    assert restore_messages(state, [automatic])[0].origin == "tool"

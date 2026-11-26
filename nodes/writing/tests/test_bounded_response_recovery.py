"""Provider reasoning/truncation is not an empty manuscript or a role outage."""
import json
import shutil
from pathlib import Path
from unittest.mock import Mock

import pytest

from core.llm import LLMResponse
from core.state import State
from nodes.writing.tools import w_common, w_draft


@pytest.fixture
def state(tmp_path):
    fixture = Path(__file__).parent / "fixtures" / "rebuild"
    workspace = tmp_path / "workspace"
    for name in ("paper", "figures", "sources"):
        shutil.copytree(fixture / name, workspace / name)
    return State(run_id="writing-response", node_type="writing", root=tmp_path / "run",
                 project_worktree=workspace, workspace_root=workspace / "paper")


def backend(monkeypatch, replies):
    calls = []
    ledger = Mock()

    class Client:
        model = "fixture-model"
        base_url = "http://unused.invalid"
        role = "reasoning"

        async def chat(self, messages, **kwargs):
            calls.append(kwargs)
            return replies[len(calls) - 1]

    monkeypatch.setattr("core.llm.LLMClient", Client)
    monkeypatch.setattr("core.cost_ledger.record", ledger)
    return calls, ledger


def response(content=None, finish="stop", reasoning=None):
    return LLMResponse(content=content, tool_calls=[], finish_reason=finish,
                       reasoning_content=reasoning,
                       usage={"prompt_tokens": 100, "completion_tokens": 7000 if reasoning else 1,
                              "total_tokens": 7100 if reasoning else 101,
                              "completion_tokens_details": {
                                  "reasoning_tokens": 7000 if reasoning else 0}})


@pytest.mark.asyncio
@pytest.mark.parametrize("finish", ["length", "stop"])
async def test_reasoning_only_response_gets_one_larger_bounded_attempt(state, monkeypatch, finish):
    calls, ledger = backend(monkeypatch, [response(finish=finish, reasoning="private reasoning"),
                                        response("```latex\n正文\n```")])
    text = await w_common.bounded_llm(
        state, phase="draft:introduction", system="s", user="u", max_tokens=7000)
    assert text == "正文"
    assert len(calls) == ledger.call_count == 2
    assert 7000 < calls[1]["max_tokens"] <= 32768
    events = [json.loads(line) for line in state.transcript_path.read_text().splitlines()]
    attempts = [e for e in events if e.get("event") == "writing_bounded_call"]
    assert attempts[0]["finish_reason"] == finish
    assert attempts[0]["reasoning_tokens"] == 7000
    assert "private reasoning" not in state.transcript_path.read_text()


@pytest.mark.asyncio
async def test_true_empty_response_preserves_existing_section(state, monkeypatch):
    path = w_common.manuscript_dir(state) / "sections" / "introduction.tex"
    path.write_text("existing draft\n")
    calls, ledger = backend(monkeypatch, [response()])
    result = await w_draft._draft_section(state, "introduction")
    assert result["status"] == "error"
    assert result["reason"] == "writing_empty_response"
    assert result["attempts"][0]["finish_reason"] == "stop"
    assert result["model"] == "fixture-model"
    assert path.read_text() == "existing draft\n"
    assert len(calls) == ledger.call_count == 1


@pytest.mark.asyncio
async def test_truncated_text_is_never_written_as_a_complete_section(state, monkeypatch):
    path = w_common.manuscript_dir(state) / "sections" / "introduction.tex"
    path.write_text("existing draft\n")
    calls, ledger = backend(monkeypatch, [response("unfinished", "length", "thinking"),
                                        response("still unfinished", "length", "thinking")])
    result = await w_draft._revise_section(state, "introduction", "Revise")
    assert result["reason"] == "writing_truncated_response"
    assert len(result["attempts"]) == len(calls) == ledger.call_count == 2
    assert path.read_text() == "existing draft\n"


@pytest.mark.asyncio
async def test_recovered_draft_is_written_through_the_real_compose_path(state, monkeypatch):
    backend(monkeypatch, [response(finish="length", reasoning="thinking"),
                          response("\\section{引言}\n恢复后的正文。")])
    result = await w_draft._draft_section(state, "introduction")
    assert result["status"] in {"success", "success_with_issues"}
    path = w_common.manuscript_dir(state) / "sections" / "introduction.tex"
    assert "恢复后的正文" in path.read_text()


@pytest.mark.asyncio
async def test_reasoning_recovery_stops_at_the_budget_cap(state, monkeypatch):
    calls, ledger = backend(monkeypatch, [response(finish="length", reasoning="thinking"),
                                        response(finish="length", reasoning="thinking")])
    with pytest.raises(w_common.WritingResponseError) as error:
        await w_common.bounded_llm(state, phase="referee:1", system="s", user="u", max_tokens=20000)
    assert [c["max_tokens"] for c in calls] == [20000, 32768]
    assert ledger.call_count == 2
    assert error.value.result["reason"] == "writing_truncated_response"


@pytest.mark.asyncio
async def test_failed_referee_keeps_the_previous_review_round(state, monkeypatch):
    from nodes.writing.tools import w_referee

    paper = w_common.paper_dir(state)
    (paper / "pages.txt").write_text("existing rendered manuscript")
    (paper / "render_report.json").write_text('{"status":"success"}')
    state.hook_state["writing_referee_rounds"] = 1
    backend(monkeypatch, [response()])
    result = await w_referee._referee_review(state)
    assert result["reason"] == "writing_empty_response"
    assert state.hook_state["writing_referee_rounds"] == 1

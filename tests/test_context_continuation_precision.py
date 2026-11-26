"""Continuation sees the current task/state; provider markup never becomes data."""
import copy
import hashlib
import json
import subprocess

import pytest

from core import summarizer as sm
from core.harness import NodeHarness, SummarizerConfig
from core.llm import LLMClient, LLMMessage, LLMResponse, framework_notice
from core.state import State
from core.tool_call_recovery import recover_tool_calls
from nodes.analysis.tools.research_state import _read_research_state, _update_research_state


LEDGER = "## 目标\n继续研究\n## 已完成\n记录 result.json\n## 未决与下一步\n核对数据\n## 决策与理由\n保留适用条件"
LEAK = ('<｜｜DSML｜｜ calls>\n<｜｜DSML｜｜ invoke name="read_tool_result">\n'
        '<｜｜DSML｜｜ parameter name="result_ref" string="true">v2:scope:0:99:digest</｜｜DSML｜｜ parameter>\n'
        '<｜｜DSML｜｜ parameter name="offset" string="false">104000</｜｜DSML｜｜ parameter>\n'
        '<｜｜DSML｜｜ parameter name="limit" string="false">10000</｜｜DSML｜｜ parameter>\n'
        '</｜｜DSML｜｜ invoke>\n</｜｜DSML｜｜ calls>')


def snapshot(text):
    m = framework_notice(text)
    m.context_slot = "research_state"
    return m


class Summary:
    def __init__(self):
        self.requests = []

    async def chat(self, messages, **kwargs):
        self.requests.append(messages)
        return LLMResponse(content=LEDGER, tool_calls=[], finish_reason="stop", usage={})


@pytest.mark.asyncio
async def test_summary_reads_current_goal_constraints_and_latest_snapshot(tmp_path):
    messages = [LLMMessage(role="system", content="Research analyst"),
                LLMMessage(role="user", content="ORIGINAL_GOAL: separate accuracy from the two-hour budget"),
                snapshot("OUTDATED_STATE_61")]
    messages += [LLMMessage(role="assistant", content=f"Historical report {i}") for i in range(8)]
    exact = 'CURRENT_STATE_62: {"sigma": 8.712013995950031e-05, "source":"analysis_report__verified"}'
    messages += [snapshot(exact), LLMMessage(role="user", content="LATEST_CONSTRAINT: do not infer an all-stage audit")]
    original = copy.deepcopy(messages)
    llm = Summary()
    h = NodeHarness(node_type="analysis", summarizer=SummarizerConfig(keep_last_n_turns=2))
    ctx = sm.SummarizerContext(h, State.new("analysis", tmp_path), messages, sm.estimate_tokens(messages), llm, 9)
    await sm._strategy_llm(ctx)
    request = "\n".join(m.content or "" for m in llm.requests[0])
    assert "ORIGINAL_GOAL" in request
    assert "LATEST_CONSTRAINT" in request
    assert exact in request
    assert "OUTDATED_STATE_61" not in request
    assert messages == original


def test_running_summary_uses_the_newest_ledger():
    first = sm.build_compression_notice(1, "old", sm._COMPRESSION_SUMMARY_HEADER+"\n"+LEDGER+"\nOLD_PRECISION")
    last = sm.build_compression_notice(2, "current", sm._COMPRESSION_SUMMARY_HEADER+"\n"+LEDGER+"\nNEW_PRECISION")
    prior = sm._extract_prior_summary([first,last])
    assert "NEW_PRECISION" in prior and "OLD_PRECISION" not in prior


@pytest.mark.asyncio
@pytest.mark.parametrize("project_bound",[False,True])
async def test_compaction_reads_the_real_owner_without_a_pilot_injected_snapshot(tmp_path,project_bound):
    worktree = tmp_path/"project" if project_bound else None
    if worktree:
        worktree.mkdir()
        hooks = worktree/"empty-hooks"
        hooks.mkdir()
        subprocess.run(["git","init","-q",str(worktree)],check=True,capture_output=True)
        subprocess.run(["git","-C",str(worktree),"-c","user.name=ContextTest",
                        "-c","user.email=context@example.invalid","-c","commit.gpgsign=false",
                        "-c",f"core.hooksPath={hooks}","commit","--allow-empty","-qm","Initialize context fixture"],
                       check=True,capture_output=True)
    state = State.new("analysis",tmp_path/"runs",project_worktree=worktree)
    await _update_research_state(state,verdict="continue",research_question="Preserve exact recomputation",
        completed_experiments=[{"artifact_id":"analysis_report__verified","note":"sigma=8.712013995950031e-05; precision unchanged"}])
    original = await _read_research_state(state)
    llm = Summary()
    messages = [LLMMessage(role="system",content="Research"), LLMMessage(role="user",content="Continue from the current source")]
    messages += [LLMMessage(role="assistant",content="Historical explanation "*1000) for _ in range(12)]
    h = NodeHarness(node_type="analysis",max_context_tokens=18_000,max_output_tokens=4096,
                    summarizer=SummarizerConfig(strategy="llm",keep_last_n_turns=2))
    result = await sm.run_summarizer(h,state,messages,llm,21,sm.estimate_tokens(messages))
    request = "\n".join(m.content or "" for m in llm.requests[0])
    assert "8.712013995950031e-05" in request
    current = next(m for m in result if m.context_slot=="research_state")
    assert "8.712013995950031e-05" in current.content
    assert await _read_research_state(state) == original
    assert state.hook_state["_context_compaction"]["target_met"]


@pytest.mark.asyncio
async def test_disk_revision_replaces_an_old_research_snapshot_at_compaction(tmp_path):
    state = State.new("analysis",tmp_path)
    await _update_research_state(state,verdict="continue",completed_experiments=[{"note":"OLD_ESTIMATE_772"}])
    old = snapshot(json.dumps(await _read_research_state(state),ensure_ascii=False))
    await _update_research_state(state,verdict="continue",change_reason="Pinned evidence correction",
        completed_experiments=[{"artifact_id":"analysis_report__verified","note":"NEW_MEASURED_480"}])
    llm = Summary()
    messages = [LLMMessage(role="system",content="Research"),LLMMessage(role="user",content="Use current evidence"),old]
    messages += [LLMMessage(role="assistant",content="Old narrative "*1000) for _ in range(12)]
    h = NodeHarness(node_type="analysis",max_context_tokens=18_000,max_output_tokens=4096,
                    summarizer=SummarizerConfig(strategy="llm",keep_last_n_turns=2))
    result = await sm.run_summarizer(h,state,messages,llm,22,sm.estimate_tokens(messages))
    request = "\n".join(m.content or "" for m in llm.requests[0])
    assert "NEW_MEASURED_480" in request and "OLD_ESTIMATE_772" not in request
    current = [m for m in result if m.context_slot=="research_state"]
    assert len(current)==1 and "NEW_MEASURED_480" in current[0].content
    assert (await _read_research_state(state))["version"]==2


@pytest.mark.asyncio
async def test_large_research_state_uses_an_exact_versioned_reference_not_clipped_json(tmp_path):
    from core.context_lifecycle import research_state_snapshot
    from core.llm import framework_notice_body
    state = State.new("analysis",tmp_path)
    await _update_research_state(state,verdict="continue",completed_experiments=[{"note":"条件与数值。"*5000}])
    original = await _read_research_state(state)
    h = NodeHarness(node_type="analysis",max_context_tokens=18_000,max_output_tokens=4096)
    view = research_state_snapshot(h,state)
    reference = json.loads(framework_notice_body(view))
    assert reference["mode"]=="reference" and reference["version"]==1
    assert reference["artifact_id"]=="research_state__research_state"
    assert reference["content_sha256"]==hashlib.sha256(original["content"].encode()).hexdigest()
    assert "read_artifact" in reference["read"]
    assert sm.estimate_tokens([view]) < 1000
    assert await _read_research_state(state) == original


def test_recorded_double_bar_dsml_preserves_typed_arguments():
    recovered = recover_tool_calls(LEAK, [])
    assert recovered.recovered and not recovered.protocol_leak
    assert recovered.content is None
    assert len(recovered.tool_calls) == 1
    call = recovered.tool_calls[0]["function"]
    assert call["name"] == "read_tool_result"
    assert json.loads(call["arguments"]) == {"result_ref":"v2:scope:0:99:digest", "offset":104000, "limit":10000}


def provider(content=None, calls=None):
    return {"choices":[{"message":{"content":content,"tool_calls":calls or []},"finish_reason":"stop"}],"usage":{}}


@pytest.mark.asyncio
async def test_no_tools_call_retries_leaked_markup_instead_of_returning_numbers(monkeypatch):
    responses = [provider(LEAK),provider('{"status":"waiting"}')]
    requests = []
    async def post(self, payload, **kwargs):
        requests.append(payload)
        return responses[len(requests)-1]
    monkeypatch.setenv("LLM_STREAM","off")
    monkeypatch.setattr(LLMClient,"_post_with_retry",post)
    client = LLMClient(api_key="test",model="test",base_url="http://test")
    result = await client.chat([LLMMessage(role="user",content="Report the current status as JSON")],tools=None)
    assert result.content == '{"status":"waiting"}'
    assert not result.tool_calls and not result.protocol_leak
    assert len(requests) == 2 and all("tools" not in request for request in requests)


@pytest.mark.asyncio
async def test_unoffered_native_tool_calls_remain_a_bounded_protocol_failure(monkeypatch):
    import core.llm as lm
    requests = []
    async def post(self, payload, **kwargs):
        requests.append(payload)
        return provider(calls=[{"id":"unoffered","type":"function","function":{"name":"submit_job","arguments":"{}"}}])
    monkeypatch.setenv("LLM_STREAM","off")
    monkeypatch.setattr(LLMClient,"_post_with_retry",post)
    client = LLMClient(api_key="test",model="test",base_url="http://test")
    result = await client.chat([LLMMessage(role="user",content="Return a summary, no tools")],tools=None)
    assert result.protocol_leak and result.content is None and not result.tool_calls
    assert len(requests) == lm._PROTOCOL_LEAK_RETRIES+1


@pytest.mark.asyncio
async def test_old_cache_cannot_bypass_the_no_tools_contract(monkeypatch):
    from core import llm_cache
    monkeypatch.setattr(llm_cache,"should_cache",lambda temperature:True)
    monkeypatch.setattr(llm_cache,"get",lambda key:{"tool_calls":[{"function":{"name":"submit_job","arguments":"{}"}}]})
    monkeypatch.setattr(llm_cache,"put",lambda *args:None)
    monkeypatch.setenv("LLM_STREAM","off")
    async def post(self, payload, **kwargs):
        return provider("A current text-only answer")
    monkeypatch.setattr(LLMClient,"_post_with_retry",post)
    client = LLMClient(api_key="test",model="test",base_url="http://test")
    result = await client.chat([LLMMessage(role="user",content="Summarize")],tools=None)
    assert not result.tool_calls and result.content == "A current text-only answer"


@pytest.mark.asyncio
async def test_protocol_failure_is_not_cached_as_a_successful_blank_answer(monkeypatch):
    from core import llm_cache
    saved = []
    monkeypatch.setattr(llm_cache,"should_cache",lambda temperature:True)
    monkeypatch.setattr(llm_cache,"get",lambda key:None)
    monkeypatch.setattr(llm_cache,"put",lambda *args:saved.append(args))
    monkeypatch.setenv("LLM_STREAM","off")
    async def post(self, payload, **kwargs):
        return provider(LEAK)
    monkeypatch.setattr(LLMClient,"_post_with_retry",post)
    client = LLMClient(api_key="test",model="test",base_url="http://test")
    result = await client.chat([LLMMessage(role="user",content="Summarize")],tools=None)
    assert result.protocol_leak and not saved

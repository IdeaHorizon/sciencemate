"""界面上那条「当前上下文 xx / 窗口 · 到 70% 自动压缩」读的是 harness 自己报的事实。

窗口占用有两把尺：服务端实收的 prompt_tokens（事实）和框架用来判"该不该压"的
effective（本地估算 × 校准，含工具 schema）。界面若拿前者配后者的压缩线，就会
出现"显示 54% 却已经开始压缩"。所以报告里两者都给，百分比与压缩线**同尺**。

判据：
  1. 每一次 LLM 响应之后都有一条 `context_window`，且 prompt_tokens 是服务端
     那个数、effective 与 should_compress 用的是同一个函数算出来的。
  2. 分段加起来就是 effective（换算到同一单位），工具结果一出现就落在
     toolResults 里。
  3. 压过一次之后，报告带着上一次压缩的前后数字。
"""
from __future__ import annotations

import json

import pytest

import shared.tools  # noqa: F401
from core import summarizer as sm
from core.llm import LLMMessage, LLMResponse, framework_notice, opening_system_prompt


def _events(state, kind: str) -> list[dict]:
    lines = state.transcript_path.read_text(encoding="utf-8").splitlines()
    records = [json.loads(line) for line in lines if line.strip()]
    return [r for r in records if r.get("event") == kind]


def _register_echo_tool(monkeypatch):
    from core import tool_registry

    async def _echo(*, state, **kwargs):
        return {"status": "ok", "echo": "回" * 400}

    monkeypatch.setitem(tool_registry._REGISTRY.executors, "echo", _echo)
    monkeypatch.setitem(tool_registry._REGISTRY.tools, "echo",
                        tool_registry.ToolDefinition(
                            name="echo", description="e",
                            parameters_schema={"type": "object", "properties": {}},
                            replayable_read=False))


class _LLM:
    """先调 n 次工具，再收工。每次响应都带服务端 usage。"""

    def __init__(self, n_calls: int, prompt_tokens: int = 4321):
        self.n = 0
        self.n_calls = n_calls
        self.prompt_tokens = prompt_tokens

    async def chat(self, messages, **kw):
        self.n += 1
        usage = {"prompt_tokens": self.prompt_tokens + self.n,
                 "completion_tokens": 10,
                 "total_tokens": self.prompt_tokens + self.n + 10}
        if self.n > self.n_calls:
            return LLMResponse(content="done", tool_calls=[],
                               finish_reason="stop", usage=usage)
        return LLMResponse(
            content=f"第 {self.n} 次",
            tool_calls=[{"id": f"c{self.n}", "type": "function",
                         "function": {"name": "echo", "arguments": "{}"}}],
            finish_reason="tool_calls", usage=usage)


@pytest.mark.asyncio
async def test_every_llm_response_is_followed_by_a_context_report(tmp_path, monkeypatch):
    from core.agent_loop import run_loop
    from core.harness import NodeHarness, SummarizerConfig
    from core.state import State

    _register_echo_tool(monkeypatch)
    harness = NodeHarness(node_type="literature", max_turns=6, tools=["echo"],
                          summarizer=SummarizerConfig(enabled=True,
                                                      trigger_type="token_threshold",
                                                      trigger_threshold=0.7))
    harness.max_context_tokens = 100_000
    state = State.new(node_type="literature", base_dir=tmp_path, project_id="p_ctx")
    llm = _LLM(n_calls=2)

    result = await run_loop(harness, state, [], llm)
    assert result.status == "completed", result.__dict__

    responses = _events(state, "llm_response")
    reports = _events(state, "context_window")
    assert len(reports) == len(responses) == 3, (len(reports), len(responses))

    for resp, rep in zip(responses, reports):
        assert rep["turn"] == resp["turn"]
        # 1. 服务端实收就是服务端实收 —— 不是本地估算。
        assert rep["prompt_tokens"] == resp["usage"]["prompt_tokens"]
        # effective 与触发判据同一个函数（变异：把报告里的换算改成裸 est 必转红）。
        assert rep["effective_tokens"] == sm.effective_prompt_tokens(rep["est_tokens"], state)
        assert rep["effective_tokens"] > rep["est_tokens"] > 0
        assert rep["window"] == sm.effective_context_window(harness, state) == 100_000
        assert rep["configured_window"] == 100_000
        assert rep["compress_at"] == 0.7
        assert rep["emergency_at"] == sm._CONTEXT_EMERGENCY_RATIO
        # 2. 分段加起来就是 effective（每段各自取整，误差不超过段数）。
        breakdown = rep["breakdown"]
        assert set(breakdown) == {"system", "tools", "toolResults", "summary",
                                  "framework", "conversation"}
        assert abs(sum(breakdown.values()) - rep["effective_tokens"]) <= len(breakdown)
        assert rep["last_compaction"] is None

    # 第一次请求还没有工具结果；第二次起有。
    assert reports[0]["breakdown"]["toolResults"] == 0
    assert reports[1]["breakdown"]["toolResults"] > 0
    assert reports[2]["breakdown"]["toolResults"] > reports[1]["breakdown"]["toolResults"]
    assert reports[1]["n_messages"] > reports[0]["n_messages"]


def test_the_breakdown_puts_each_message_where_it_belongs(tmp_path):
    from core.harness import NodeHarness, SummarizerConfig
    from core.state import State

    harness = NodeHarness(node_type="literature", max_turns=3, tools=[],
                          summarizer=SummarizerConfig(enabled=False))
    harness.max_context_tokens = 50_000
    state = State.new(node_type="literature", base_dir=tmp_path, project_id="p_buckets")
    state.hook_state[sm._TOOL_SCHEMA_TOKENS_KEY] = 1_000
    messages = [
        opening_system_prompt("系统提示" * 50),
        LLMMessage(role="user", content="用户说的话" * 30),
        framework_notice("框架中途说的话" * 30),
        sm.build_compression_notice(3, "压缩摘要 {dropped}", "被压掉的历史" * 30),
        LLMMessage(role="assistant", content="模型回的话" * 30),
        LLMMessage(role="tool", content="工具结果" * 60, tool_call_id="c1", name="echo"),
    ]

    report = sm.context_window_report(harness, state, messages, server_prompt_tokens=777)

    calib = sm.effective_calibration(state)
    b = report["breakdown"]
    assert all(b[key] > 0 for key in ("system", "tools", "toolResults",
                                      "summary", "framework", "conversation")), b
    assert b["tools"] == int(1_000 * calib)
    assert report["prompt_tokens"] == 777
    assert report["compress_at"] is None and report["emergency_at"] is None, (
        "summarizer 关着就没有压缩线，界面不该画一条永远到不了的线")
    assert report["n_messages"] == len(messages)
    assert report["calibration"] == round(calib, 3)


@pytest.mark.asyncio
async def test_after_a_compaction_the_report_carries_its_before_and_after(
        tmp_path, monkeypatch):
    from core.agent_loop import run_loop
    from core.harness import NodeHarness, SummarizerConfig
    from core.state import State

    _register_echo_tool(monkeypatch)
    # 窗口小到第二轮必压；drop_tool_results 不需要 LLM 参与压缩。
    harness = NodeHarness(node_type="literature", max_turns=6, tools=["echo"],
                          summarizer=SummarizerConfig(enabled=True,
                                                      trigger_type="token_threshold",
                                                      trigger_threshold=0.5,
                                                      strategy="drop_tool_results",
                                                      keep_last_n_turns=0))
    harness.max_context_tokens = 900
    harness.max_output_tokens = 100
    state = State.new(node_type="literature", base_dir=tmp_path, project_id="p_compact")
    llm = _LLM(n_calls=3, prompt_tokens=300)

    await run_loop(harness, state, [], llm)

    compactions = _events(state, "summarize")
    assert compactions, "窗口 900 跑四轮工具结果居然一次都没压"
    reports = _events(state, "context_window")
    carried = [r for r in reports if r["last_compaction"] is not None]
    assert carried, "压过之后的报告没带上上一次压缩的数字"
    first = carried[0]["last_compaction"]
    assert first["turn"] == compactions[0]["turn"]
    assert first["tokens_before"] == compactions[0]["tokens_before"]
    assert first["tokens_after"] == compactions[0]["tokens_after"]

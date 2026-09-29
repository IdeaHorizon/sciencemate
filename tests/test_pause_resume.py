"""Pause/resume mechanism 测试 —— request_human_input 不再 stdin 阻塞。

覆盖：
  - 工具返回结构化 pause event（不调 input()）
  - agent_loop 检测到 pause 后返回 LoopResult(status='paused', pause_event=...)
  - pause registry 注册 ctx
  - resume_loop 用 user 回答替换 tool_result，继续 loop
  - 多级嵌套：sub-run pause → 父 run_node 工具结果冒泡 pause → 顶层 loop unwind
"""
from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.agent_loop import LoopResult, resume_loop, run_loop
from core.bootstrap import bootstrap
from core.harness import NodeHarness
from core.llm import LLMMessage, LLMResponse
from core.pause import (
    PauseEvent, PausedRunContext, clear_all,
    get_deepest_paused, get_paused_run, list_paused,
)
from core.state import State


bootstrap(force=True)


def _make_harness(node_type: str = "test_node", tools: list[str] | None = None) -> NodeHarness:
    return NodeHarness(
        node_type=node_type,
        version="0.1",
        system_prompt="test",
        rules=[],
        guidelines=[],
        skills=[],
        expected_outputs={},
        tools=tools or ["request_human_input"],
        max_turns=5,
        kb_query="_disable",
    )


def _make_state() -> State:
    td = tempfile.mkdtemp()
    return State.new(node_type="test_node", base_dir=Path(td), project_id=None)


def _llm_calls_human_input(question: str = "你想去哪里？") -> AsyncMock:
    """Mock LLM：第一轮调 request_human_input；resume 后第二轮返回最终文本。"""
    call_count = {"n": 0}

    async def fake_chat(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return LLMResponse(
                content="",
                tool_calls=[{
                    "id": "call_001",
                    "type": "function",
                    "function": {
                        "name": "request_human_input",
                        "arguments": json.dumps({"question": question}),
                    },
                }],
                finish_reason="tool_calls",
                usage={"total_tokens": 10},
            )
        return LLMResponse(
            content=f"我知道了，你想去 {kwargs.get('_response_marker', 'X')}",
            tool_calls=[],
            finish_reason="stop",
            usage={"total_tokens": 10},
        )

    return AsyncMock(side_effect=fake_chat)


# ── 基本场景：单层 pause + resume ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_request_human_input_returns_pause_not_blocks():
    """工具不阻塞 stdin，返回结构化 pause event。"""
    clear_all()
    state = _make_state()
    from core.tool_registry import execute as exec_tool
    result = await exec_tool(
        "request_human_input", state,
        question="测试问题", context="测试背景",
        options=["A", "B"],
        recommended_option_index=0,
    )
    assert result["status"] == "pause"
    assert result["pause_event"]["question"] == "测试问题"
    assert result["pause_event"]["context"] == "测试背景"
    assert result["pause_event"]["options"] == ["A", "B"]
    assert result["pause_event"]["asking_node_type"] == "test_node"


@pytest.mark.asyncio
async def test_pause_event_preserves_tool_metadata():
    """v0.4.3 regression：tool 返回的 pause_event.metadata（含 decision_package
    的 type / recommended_option_index 等）必须被 agent_loop 拷进 PauseEvent，
    否则 _ask_decision_package 永远不触发 → auto-approve 选 options[0]
    无视 reviewer 推荐。这是 dogfood 发现的真 bug。
    """
    clear_all()
    state = _make_state()
    # 用独立 tool 名避免污染真 present_decision_package 注册（被其它 test 用）
    h = _make_harness(tools=["_test_pause_with_metadata"])
    from core.tool_registry import ToolDefinition, register_tool

    async def _mock_decision_pkg(*_a, **_k):
        return {
            "status": "pause",
            "pause_event": {
                "question": "Post-node decision",
                "context": "ASCII package",
                "options": ["PROCEED", "REVISE", "ABORT", "EDIT"],
                "metadata": {
                    "type": "decision_package",
                    "recommended_option_index": 1,
                    "recommended_action": "revise",
                    "recommended_feedback": "fix the metric",
                },
            },
        }
    register_tool(ToolDefinition(
        name="_test_pause_with_metadata",
        description="mock for test", parameters_schema={"type": "object"},
        risk_level="low",
    ), _mock_decision_pkg)

    llm = MagicMock()
    async def fake_chat(*a, **k):
        return LLMResponse(
            content="",
            tool_calls=[{
                "id": "call_dp", "type": "function",
                "function": {"name": "_test_pause_with_metadata", "arguments": "{}"},
            }],
            finish_reason="tool_calls",
            usage={"total_tokens": 10},
        )
    llm.chat = AsyncMock(side_effect=fake_chat)

    messages = [LLMMessage(role="user", content="hi")]
    result = await run_loop(h, state, messages, llm)

    assert result.status == "paused"
    assert result.pause_event is not None
    md = result.pause_event.metadata
    assert md.get("type") == "decision_package", (
        f"metadata.type 没传上来；得到 {md!r}。bug 重现：agent_loop "
        f"构造 PauseEvent 时漏拷 metadata → _ask_decision_package "
        f"永远不触发，auto-approve 永远选 options[0] 无视 reviewer 推荐。"
    )
    assert md.get("recommended_option_index") == 1
    assert md.get("recommended_action") == "revise"


@pytest.mark.asyncio
async def test_agent_loop_unwinds_on_pause():
    """LLM 调 request_human_input → loop 应当返回 status='paused'，不直接给 final_text。"""
    clear_all()
    state = _make_state()
    h = _make_harness()
    llm = MagicMock()
    llm.chat = _llm_calls_human_input()

    messages = [LLMMessage(role="user", content="hi")]
    result = await run_loop(h, state, messages, llm)

    assert result.status == "paused"
    assert result.pause_event is not None
    assert result.pause_event.question == "你想去哪里？"
    assert result.pause_event.pending_tool_call_id == "call_001"
    # pause 注册到 registry
    ctx = get_paused_run(state.run_id)
    assert ctx is not None
    assert ctx.pending_tool_call_id == "call_001"


@pytest.mark.asyncio
async def test_resume_loop_continues_after_answer():
    """resume_loop 把 user 回答塞回 tool_result，再跑 loop → 这次 LLM 返回最终文本。"""
    clear_all()
    state = _make_state()
    h = _make_harness()

    # Mock LLM：第一轮调工具；第二轮看到 tool_result 后返回最终文本
    responses = [
        LLMResponse(
            content="",
            tool_calls=[{
                "id": "call_001", "type": "function",
                "function": {"name": "request_human_input",
                                "arguments": '{"question":"哪里？"}'},
            }],
            finish_reason="tool_calls",
            usage={"total_tokens": 10},
        ),
        LLMResponse(
            content="收到，你想去北京。",
            tool_calls=[],
            finish_reason="stop",
            usage={"total_tokens": 10},
        ),
    ]
    llm = MagicMock()
    llm.chat = AsyncMock(side_effect=responses)

    messages = [LLMMessage(role="user", content="hi")]
    result = await run_loop(h, state, messages, llm)
    assert result.status == "paused"

    # 用 user 回答 resume
    ctx = get_paused_run(state.run_id)
    final = await resume_loop(ctx, "北京")

    assert final.status == "completed"
    assert "北京" in final.final_text

    # tool_result 消息内容应被替换成 user 回答
    tool_msgs = [m for m in messages if m.role == "tool" and m.tool_call_id == "call_001"]
    assert len(tool_msgs) == 1
    payload = json.loads(tool_msgs[0].content)
    assert payload["status"] == "success"
    assert payload["response"] == "北京"


@pytest.mark.asyncio
async def test_pause_cleared_from_registry_after_resume():
    """resume 完成后 registry 应清空（不能让旧 pause 残留干扰新调用）。"""
    clear_all()
    state = _make_state()
    h = _make_harness()
    responses = [
        LLMResponse(content="", tool_calls=[{
            "id": "c1", "type": "function",
            "function": {"name": "request_human_input", "arguments": '{"question":"q"}'},
        }], finish_reason="tool_calls", usage={"total_tokens": 0}),
        LLMResponse(content="done", tool_calls=[], finish_reason="stop",
                       usage={"total_tokens": 0}),
    ]
    llm = MagicMock(); llm.chat = AsyncMock(side_effect=responses)
    await run_loop(h, state, [LLMMessage(role="user", content="hi")], llm)
    assert state.run_id in {c.run_id for c in list_paused()}

    ctx = get_paused_run(state.run_id)
    await resume_loop(ctx, "answer")
    assert state.run_id not in {c.run_id for c in list_paused()}


# ── 多级嵌套：sub-run pause 冒泡 ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_sub_run_pause_propagates_via_run_node(tmp_path, monkeypatch):
    """sub-node 调 request_human_input → run_node 工具返 pause 状态 → 父 loop 也 pause。

    这一关核心验证：
      - 子 run 在 paused 状态时 execute_node 不写 summary.json
      - 子 run 的 pause 事件能透过 run_node 工具结果到达父 loop
      - 父 loop 检测到 status=='pause' 也会 unwind
      - PAUSED_RUNS 里能找到子 run 的 ctx，parent_tool_call_id 已被填上
    """
    clear_all()
    parent_state = _make_state()
    parent_h = NodeHarness(
        node_type="parent", version="0.1", system_prompt="",
        rules=[], guidelines=[], skills=[], expected_outputs={},
        tools=["run_node"], callable_nodes=["test_node"], max_turns=5,
        kb_query="_disable",
    )

    # 写一个 test_node harness yaml 让 load_harness 能找到。
    #
    # **落在 tmp_path，不落在仓库的 nodes/**（2026-09-21）。原来它往真的
    # `nodes/_test_pause_child/` 写，跑完 rmtree 掉 —— 在 `-n 6` 下这是一个
    # **跨 worker 竞态**：另一个 worker 里的 `test_project_orientation.py::
    # test_every_node_harness_enables_it` 会扫 `list_harnesses()`，正好撞上这个
    # 目录已经建好、yaml 还没写（或已经被删）的那一瞬，于是
    # `FileNotFoundError: 找不到 node_type='_test_pause_child' 对应的 harness`。
    # CI 上实测红过；本机单跑永远看不到 —— 它不是这两条用例中任何一条的缺陷，
    # 是"往共享目录里写东西"本身。
    nodes_root = tmp_path / "nodes"
    monkeypatch.setattr("core.loader.NODES_DIR", nodes_root)
    nodes_dir = nodes_root / "_test_pause_child"
    nodes_dir.mkdir(parents=True, exist_ok=True)
    harness_path = nodes_dir / "harness.yaml"
    harness_path.write_text("""
node_type: _test_pause_child
version: "0.1"
system_prompt: "test child"
rules: []
guidelines: []
skills: []
tools:
  - request_human_input
max_turns: 3
context_config:
  memory_query: ""
  kb_query: "_disable"
  max_context_tokens: 60000
  max_output_tokens: 4096
  temperature: 0.3
completion_criteria:
  required_outputs: []
  quality_checks: []
expected_inputs: {}
expected_outputs: {}
handoff_policy:
  strategy: none
""", encoding="utf-8")
    parent_h.callable_nodes = ["_test_pause_child"]

    try:
        # parent LLM 调 run_node(_test_pause_child)；
        # child LLM 调 request_human_input → 触发 pause → 冒泡
        parent_llm_responses = [
            LLMResponse(content="", tool_calls=[{
                "id": "parent_call_1", "type": "function",
                "function": {"name": "run_node",
                                "arguments": json.dumps({
                                    "node_type": "_test_pause_child",
                                    # 派发前必须先跟用户说一句（schema required）
                                    "user_note": "起子节点验证 pause 冒泡",
                                    "node_inputs": {"task": "x"},
                                })},
            }], finish_reason="tool_calls", usage={"total_tokens": 0}),
        ]
        child_llm_responses = [
            LLMResponse(content="", tool_calls=[{
                "id": "child_call_1", "type": "function",
                "function": {"name": "request_human_input",
                                "arguments": json.dumps({"question": "child asks"})},
            }], finish_reason="tool_calls", usage={"total_tokens": 0}),
        ]

        all_responses = parent_llm_responses + child_llm_responses
        idx = {"n": 0}

        async def fake_chat(*args, **kwargs):
            r = all_responses[idx["n"]]
            idx["n"] += 1
            return r

        # patch LLMClient so child also uses our mock
        with patch("core.llm.LLMClient") as MockClient:
            mock_llm = MagicMock()
            mock_llm.chat = AsyncMock(side_effect=fake_chat)
            MockClient.return_value = mock_llm

            parent_messages = [LLMMessage(role="user", content="go")]
            result = await run_loop(parent_h, parent_state, parent_messages, mock_llm)

        # 父 loop 也应该 pause
        assert result.status == "paused", f"expected paused, got {result.status}"
        # registry 里应该有 child 的 ctx（叶子）
        deepest = get_deepest_paused()
        assert deepest is not None
        assert deepest.pause_event.question == "child asks"
        # parent_tool_call_id 已经被 run_node 工具填上
        assert deepest.parent_tool_call_id == "parent_call_1"
    finally:
        # 清理 test child harness
        import shutil
        shutil.rmtree(nodes_dir, ignore_errors=True)
        clear_all()

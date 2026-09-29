"""工具接口规范测试（v2.1+）。

覆盖：
  - register_tool 启动时校验（name / async / state / schema）
  - 不合规直接 raise
  - 软警告（missing **kwargs / schema mismatch）会 log 不阻塞
  - execute 的 result envelope（自动 wrap）
"""
from __future__ import annotations

import asyncio
import logging
import tempfile
from pathlib import Path

import pytest

from core.state import State
from core.tool_registry import (
    ToolDefinition, _wrap_result, execute, list_tools_for_node, register_tool,
)


def _make_state() -> State:
    return State.new(node_type="test", base_dir=Path(tempfile.mkdtemp()))


async def _guarded_fixture_executor(*, state, **_):
    return {"status": "success"}


def _register_guarded_fixture() -> None:
    register_tool(
        ToolDefinition(
            name="guarded_fixture_for_test",
            description="test-only fixture",
            parameters_schema={"type": "object", "properties": {}},
            required_runtime_capability="writing_fixture_delivery",
        ),
        _guarded_fixture_executor,
    )


# ── 启动时校验 ─────────────────────────────────────────────────────────────

def test_register_rejects_bad_name():
    """名字含空格 / 中文 / 太长 → 直接报错。"""
    async def _ok_exec(*, state, **_): return {"status": "success"}

    for bad in ["has space", "中文名", "with.dot", "a" * 65]:
        with pytest.raises(ValueError, match="不合法"):
            register_tool(
                ToolDefinition(name=bad, description="x", parameters_schema={}),
                _ok_exec,
            )


def test_register_rejects_non_async_executor():
    """同步函数 → 报错。"""
    def _sync_exec(*, state, **_): return {"status": "success"}

    with pytest.raises(ValueError, match="必须是 async"):
        register_tool(
            ToolDefinition(name="sync_tool", description="x", parameters_schema={}),
            _sync_exec,
        )


def test_register_rejects_no_state_param():
    """没 state 参数也没 **kwargs → 报错。"""
    async def _no_state(query: str = ""): return {"status": "success"}

    with pytest.raises(ValueError, match="必须接受 `state`"):
        register_tool(
            ToolDefinition(name="no_state_tool", description="x", parameters_schema={}),
            _no_state,
        )


def test_register_rejects_non_dict_schema():
    """parameters_schema 不是 dict → 报错。"""
    async def _ok(*, state, **_): return {"status": "success"}

    with pytest.raises(ValueError, match="parameters_schema 必须是 dict"):
        register_tool(
            ToolDefinition(name="bad_schema", description="x", parameters_schema="not a dict"),
            _ok,
        )


def test_register_accepts_proper_executor():
    """合规 executor 注册成功。"""
    async def _good(*, state, query: str, limit: int = 10, **_):
        return {"status": "success", "n": limit}

    register_tool(
        ToolDefinition(
            name="good_tool_for_test",
            description="test",
            parameters_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "limit": {"type": "integer"},
                },
                "required": ["query"],
            },
        ),
        _good,
    )

    # Verify it's in the registry
    from core.tool_registry import get_tool
    assert get_tool("good_tool_for_test") is not None


def test_register_warns_on_missing_kwargs(caplog):
    """没 **kwargs 兜底 → warn 但不报错。"""
    async def _no_var_kw(*, state, query: str):
        return {"status": "success"}

    with caplog.at_level(logging.WARNING):
        register_tool(
            ToolDefinition(
                name="missing_kwargs_test",
                description="x",
                parameters_schema={
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                },
            ),
            _no_var_kw,
        )
    assert any("**kwargs" in r.message for r in caplog.records)


def test_register_warns_on_schema_signature_mismatch(caplog):
    """schema 声明了 'a' 但 signature 没接 → warn。"""
    async def _sig_only_b(*, state, b: int = 1, **_):
        return {"status": "success"}

    # signature 接 b 但没 a，schema 声明 a
    with caplog.at_level(logging.WARNING):
        register_tool(
            ToolDefinition(
                name="mismatch_test",
                description="x",
                parameters_schema={
                    "type": "object",
                    "properties": {"a": {"type": "string"}},
                },
            ),
            _sig_only_b,
        )
    # 因为有 **kwargs，schema_only 不 warn；但 sig_only (b 在签名没在 schema) 会 warn
    msgs = [r.message for r in caplog.records]
    assert any("没声明" in m for m in msgs)


# ── result envelope ────────────────────────────────────────────────────────

def test_envelope_passthrough_dict_with_status():
    """工具返合规 dict → 原样透传。"""
    out = _wrap_result("t", {"status": "success", "data": 42})
    assert out == {"status": "success", "data": 42}


def test_envelope_wraps_dict_without_status(caplog):
    """工具返 dict 但没 status → 自动 wrap + warn。"""
    with caplog.at_level(logging.WARNING):
        out = _wrap_result("t", {"result": "hello"})
    assert out["status"] == "success"
    assert out["data"] == {"result": "hello"}
    assert any("status" in r.message for r in caplog.records)


def test_envelope_wraps_non_dict_as_error(caplog):
    """工具返非 dict（如 str / int）→ wrap 成 error。"""
    with caplog.at_level(logging.WARNING):
        out = _wrap_result("t", "just a string")
    assert out["status"] == "error"
    assert "non-dict" in out["error"]
    assert out["raw"] == "just a string"


@pytest.mark.asyncio
async def test_execute_catches_exception():
    """工具内部 raise → execute 捕获返 error。"""
    async def _crash(*, state, **_):
        raise RuntimeError("boom")
    register_tool(
        ToolDefinition(name="crashy_for_test", description="x", parameters_schema={}),
        _crash,
    )

    out = await execute("crashy_for_test", _make_state())
    assert out["status"] == "error"
    assert "RuntimeError" in out["error"]
    assert "boom" in out["error"]


@pytest.mark.asyncio
async def test_execute_unknown_tool_returns_error():
    out = await execute("nonexistent_tool_xyz", _make_state())
    assert out["status"] == "error"
    assert "未注册" in out["error"]


@pytest.mark.asyncio
async def test_execute_applies_envelope_to_dict_missing_status():
    """工具返 dict 缺 status → execute 出口 wrap。"""
    async def _ok_no_status(*, state, **_):
        return {"result": "computed"}
    register_tool(
        ToolDefinition(name="ok_no_status_for_test", description="x",
                        parameters_schema={}),
        _ok_no_status,
    )
    out = await execute("ok_no_status_for_test", _make_state())
    assert out["status"] == "success"
    assert out["data"] == {"result": "computed"}


def test_capability_guarded_tool_is_hidden_from_production_state(monkeypatch):
    """A whitelisted fixture tool must not appear in the production LLM schema."""
    monkeypatch.delenv("HARNESS_RUNTIME_PROFILE", raising=False)
    monkeypatch.delenv("HARNESS_TEST_CAPABILITIES", raising=False)

    _register_guarded_fixture()
    state = _make_state()
    visible = list_tools_for_node(
        "test", ["guarded_fixture_for_test"], state=state
    )
    assert visible == []


@pytest.mark.asyncio
async def test_capability_guarded_tool_cannot_execute_in_production(monkeypatch):
    monkeypatch.delenv("HARNESS_RUNTIME_PROFILE", raising=False)
    monkeypatch.delenv("HARNESS_TEST_CAPABILITIES", raising=False)
    _register_guarded_fixture()
    state = _make_state()
    out = await execute("guarded_fixture_for_test", state)
    assert out["status"] == "error"
    assert out["required_runtime_capability"] == "writing_fixture_delivery"


@pytest.mark.asyncio
async def test_explicit_test_profile_exposes_and_executes_guarded_tool(monkeypatch):
    monkeypatch.setenv("HARNESS_RUNTIME_PROFILE", "test")
    monkeypatch.setenv("HARNESS_TEST_CAPABILITIES", "writing_fixture_delivery")
    _register_guarded_fixture()
    state = _make_state()
    visible = list_tools_for_node(
        "test", ["guarded_fixture_for_test"], state=state
    )
    assert [tool.name for tool in visible] == ["guarded_fixture_for_test"]
    out = await execute("guarded_fixture_for_test", state)
    assert out["status"] == "success"


def test_unknown_or_production_profile_capabilities_fail_closed(monkeypatch):
    from core.runtime_capabilities import trusted_runtime_capabilities

    monkeypatch.setenv("HARNESS_RUNTIME_PROFILE", "production")
    monkeypatch.setenv("HARNESS_TEST_CAPABILITIES", "writing_fixture_delivery")
    assert trusted_runtime_capabilities() == frozenset()
    monkeypatch.setenv("HARNESS_RUNTIME_PROFILE", "test")
    monkeypatch.setenv("HARNESS_TEST_CAPABILITIES", "made_up_capability")
    assert trusted_runtime_capabilities() == frozenset()


def test_writing_delivery_test_mode_requires_trusted_capability():
    from core.runtime_capabilities import (
        WRITING_FIXTURE_DELIVERY_CAPABILITY,
        validate_node_runtime_inputs,
    )

    inputs = {"delivery_test_mode": "compact_full_delivery"}
    assert validate_node_runtime_inputs("writing", inputs, capabilities=())
    assert validate_node_runtime_inputs(
        "writing",
        inputs,
        capabilities=(WRITING_FIXTURE_DELIVERY_CAPABILITY,),
    ) is None


@pytest.mark.asyncio
async def test_executor_blocks_llm_authored_delivery_test_mode_before_loop(tmp_path):
    from core.executor import execute_node
    from core.harness import NodeHarness

    summary = await execute_node(
        "writing",
        state_dir=tmp_path,
        node_inputs={"delivery_test_mode": "compact_full_delivery"},
        harness_override=NodeHarness(
            node_type="writing",
            required_output_artifact_types=["manuscript"],
        ),
    )
    assert summary["status"] == "blocked_unauthorized_test_mode"
    assert summary["turns"] == 0
    assert summary["tool_call_count"] == 0
    transcript = Path(summary["state_dir"]) / "transcript.jsonl"
    assert "runtime_input_denied" in transcript.read_text(encoding="utf-8")


# ── 缺必填参数：结构化报错，不是裸 TypeError ────────────────────────────
#
# 现场（2026-08-17）：模型调 request_human_input 没给 question，拿回来的是
#   "TypeError: _request_human_input() missing 1 required positional argument: 'question'"
# 内部函数名 + 零合法取值信息 = 只能猜。


async def _needs_question(*, state, question: str, options=None, **_):
    return {"status": "success", "question": question}


def test_missing_required_arg_returns_structured_error():
    register_tool(
        ToolDefinition(
            name="needs_question_for_test",
            description="test-only",
            parameters_schema={
                "type": "object",
                "properties": {
                    "question": {"type": "string", "description": "要问的问题"},
                    "options": {"type": "array"},
                },
                "required": ["question"],
            },
        ),
        _needs_question,
    )
    result = asyncio.run(execute("needs_question_for_test", _make_state()))
    assert result["status"] == "error"
    assert result["missing_parameters"] == ["question"]
    # 报错必须说出工具名和它接受什么，不能只吐内部函数名
    assert "needs_question_for_test" in result["error"]
    assert "options" in result["error"]
    assert "TypeError" not in result["error"]
    assert result["parameters_schema"]["required"] == ["question"]


def test_supplied_required_arg_still_dispatches():
    register_tool(
        ToolDefinition(
            name="needs_question_ok_for_test",
            description="test-only",
            parameters_schema={"type": "object",
                               "properties": {"question": {"type": "string"}}},
        ),
        _needs_question,
    )
    result = asyncio.run(
        execute("needs_question_ok_for_test", _make_state(), question="真的问了"))
    assert result["status"] == "success"
    assert result["question"] == "真的问了"

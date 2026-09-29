"""空 SSE 回退：要有总预算、要留下结构化事实、要归到 provider 账上（issue #501）。

现场：一次 E2E 的五个 test 里 `_orchestrator` 分别出现 92 / 82 / 63 / 144 / 31 次
"LLM SSE ended with no content, reasoning, or tool call"。框架确实自动回退了非流式，
但：

  - 回退**另开一份完整的重试预算** → 一次逻辑调用最坏烧两倍时间，上层长时间
    没有任何状态变化；
  - 整件事只进 log.warning → transcript 上一条记录都没有，报告人只能去翻服务器
    日志把次数数出来；
  - provider 200 却连一个 choice 都没有时，`_parse_chat_response` 直接抛穿，
    run 以一个看不出成因的异常收场。
"""
from __future__ import annotations

import ast
import inspect

import pytest

from core.bootstrap import bootstrap
from core.llm import LLMClient, _parse_chat_response

bootstrap()


# ── 1. 最重的形态：一个 choice 都没有 ───────────────────────────────────────

def test_no_choices_does_not_raise():
    """此前这里 IndexError 抛穿。观察不该被坏数据打断 —— 如实返回空回合。"""
    response = _parse_chat_response({"choices": [], "usage": {"total_tokens": 7}})
    assert response.content is None
    assert response.tool_calls == []
    assert response.provider_recovery == {"reason": "no_choices"}


def test_no_choices_keeps_finish_reason_stop():
    """finish_reason 保持 stop：blank_stop 那条既有分类靠它触发，
    换个值等于把已经修好的归因静默关掉。"""
    assert _parse_chat_response({"choices": []}).finish_reason == "stop"


def test_null_choice_entry_is_also_survived():
    assert _parse_chat_response({"choices": [None]}).provider_recovery is not None


# ── 2. 恢复动作要跟着响应一起带出来 ─────────────────────────────────────────

def test_recovery_record_reaches_the_response():
    data = {
        "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
        "usage": {},
        "_provider_recovery": {"reason": "empty_sse", "still_empty": False},
    }
    assert _parse_chat_response(data).provider_recovery == {
        "reason": "empty_sse", "still_empty": False}


def test_normal_response_carries_no_recovery_noise():
    data = {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
            "usage": {}}
    assert _parse_chat_response(data).provider_recovery is None


# ── 3. 接线：一次逻辑调用只有一份时间预算 ───────────────────────────────────

def _call_kwargs(func, callee: str) -> list[set[str]]:
    tree = ast.parse(inspect.getsource(func).lstrip())
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        attr = node.func
        if isinstance(attr, ast.Attribute) and attr.attr == callee:
            out.append({kw.arg for kw in node.keywords})
    return out


def test_nonstream_fallback_shares_the_stream_deadline():
    """回退是同一次逻辑调用的后半段。另开预算 = 这次调用总时长没有上限。"""
    calls = _call_kwargs(LLMClient._stream_with_retry, "_post_with_retry")
    assert calls, "找不到回退调用 —— 接线变了，这条测试要跟着改"
    empty_sse_calls = [kw for kw in calls if "deadline" in kw]
    assert empty_sse_calls, f"回退没有沿用流式那份 deadline：{calls}"


def test_post_with_retry_accepts_a_shared_deadline():
    assert "deadline" in inspect.signature(LLMClient._post_with_retry).parameters


# ── 4. 接线：结构化事实与归因 ───────────────────────────────────────────────

def test_agent_loop_records_the_recovery_and_flags_provider_fault():
    from core import agent_loop

    source = inspect.getsource(agent_loop._run_loop_body)
    assert "llm_provider_recovery" in source
    assert "_provider_void_response" in source


def test_executor_attributes_a_void_response_to_the_provider():
    """归 infra 账才可机械重派 —— 记成节点卡死会把它锁进重复失败熔断。"""
    from core import executor, run_history
    from shared.tools import run_node

    assert "_provider_void_response" in inspect.getsource(executor.finalize_run)
    # 主类别必须已经在两张表里：一张管"算不算节点的账"，一张管"值不值得重派"
    assert executor.FAILURE_CATEGORY_PROTOCOL in run_history.EXTERNAL_FAILURE_CATEGORIES
    assert executor.FAILURE_CATEGORY_PROTOCOL in run_node._INFRA_FAILURE_CATEGORIES


# ── 5. 功能层：回退真的收到了那份 deadline ──────────────────────────────────

@pytest.mark.asyncio
async def test_fallback_receives_the_shared_deadline(monkeypatch):
    """签名对得上不等于实参传对了（PR#410 的教训）—— 真跑一次看收到什么。"""
    import core.llm as llm_mod
    from core.llm import LLMMessage
    from tests.test_llm_streaming import _FakeClient, _client

    monkeypatch.setenv("LLM_STREAM", "1")   # conftest 默认关流式
    _FakeClient.lines = ["data: [DONE]"]
    _FakeClient.status = 200
    monkeypatch.setattr(llm_mod.httpx, "AsyncClient", _FakeClient)
    seen: dict = {}

    async def fake_post(self, payload, *, timeout, max_retries, deadline=None):
        seen["deadline"] = deadline
        return {"choices": [{"message": {"content": "ok", "tool_calls": []},
                             "finish_reason": "stop"}], "usage": {}}

    monkeypatch.setattr(LLMClient, "_post_with_retry", fake_post)
    response = await _client().chat([LLMMessage(role="user", content="?")])
    assert response.content == "ok"
    assert isinstance(seen.get("deadline"), float)
    # 而且恢复记录跟着响应一起出来了
    assert response.provider_recovery["reason"] == "empty_sse"
    assert response.provider_recovery["still_empty"] is False

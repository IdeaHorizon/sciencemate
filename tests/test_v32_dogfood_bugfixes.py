"""v3.2 dogfood 暴露的 3 处 bug 修复回归测试（2026-07 v8/v9 复盘）。

均为之前记录在案、后经代码追踪确认的真实缺陷：
  1. token 熔断从未真正激活（orchestrator 顶层 state 绕开 State.new()）
  2. qc judge 看不到 v2 memory candidates（结构性误判 literature 的
     at_least_one_open_question_memory 等检查）
  3. 瞬时网络错让整个长 run 原地崩溃，无自动 resume
"""
from __future__ import annotations

import asyncio

import httpx
import pytest

from core.state import State, apply_env_tokens_limit


# ── Bug#1：token 熔断的两条构造路径都要生效 ──────────────────────────────────

def test_state_new_applies_env_tokens_limit(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_TOKENS_LIMIT", "123456")
    state = State.new(node_type="_orchestrator", base_dir=tmp_path)
    assert state.tokens_limit == 123456


def test_orchestrator_direct_construction_also_applies_env_limit(tmp_path, monkeypatch):
    """v3.2 修复的核心：chat.py 的 _make_or_load_orchestrator_state 直接调
    State(...) 构造函数（不走 State.new()），必须显式调 apply_env_tokens_limit
    才会生效 —— 这正是 v8/v9 熔断器从未触发的根因。"""
    monkeypatch.setenv("HARNESS_TOKENS_LIMIT", "654321")
    state = State(run_id="r1", node_type="_orchestrator", root=tmp_path)
    assert state.tokens_limit == 0   # 构造时还没应用
    apply_env_tokens_limit(state)
    assert state.tokens_limit == 654321


def test_chat_make_orchestrator_state_applies_limit(tmp_path, monkeypatch):
    """端到端：真正的 chat.py 入口函数必须应用 env limit。"""
    monkeypatch.setenv("HARNESS_TOKENS_LIMIT", "999000")
    import importlib
    import chat as chat_mod
    importlib.reload(chat_mod)
    state = chat_mod._make_or_load_orchestrator_state(None, tmp_path)
    assert state.tokens_limit == 999000


def test_no_env_var_means_unlimited(tmp_path, monkeypatch):
    monkeypatch.delenv("HARNESS_TOKENS_LIMIT", raising=False)
    state = State.new(node_type="_orchestrator", base_dir=tmp_path)
    assert state.tokens_limit == 0


# ── Bug#2：qc judge 必须看到 v2 memory candidates ───────────────────────────

@pytest.mark.asyncio
async def test_auto_resume_retries_transient_error_then_succeeds(tmp_path, monkeypatch):
    import scripts.run_e2e_dogfood as dogfood
    from core.harness import NodeHarness
    from core.llm import LLMMessage

    state = State.new(node_type="_orchestrator", base_dir=tmp_path)
    harness = NodeHarness(node_type="_orchestrator", system_prompt="s")
    messages = [LLMMessage(role="system", content="s")]

    calls = {"n": 0}

    async def fake_run_loop(h, s, msgs, llm):
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("simulated transient failure")
        class R:
            status = "completed"
            final_text = "ok"
            turns = 1
            tool_calls = []
        return R()

    async def fake_sleep(_):
        return None

    monkeypatch.setattr(dogfood, "run_loop", fake_run_loop)
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    result, final_messages = await dogfood.run_with_auto_resume(
        harness, state, messages, llm=None,
    )
    assert result.status == "completed"
    assert calls["n"] == 2   # 第一次失败，第二次（resume 后）成功


@pytest.mark.asyncio
async def test_auto_resume_gives_up_after_max_attempts(tmp_path, monkeypatch):
    import scripts.run_e2e_dogfood as dogfood
    from core.harness import NodeHarness
    from core.llm import LLMMessage

    state = State.new(node_type="_orchestrator", base_dir=tmp_path)
    harness = NodeHarness(node_type="_orchestrator", system_prompt="s")
    messages = [LLMMessage(role="system", content="s")]

    async def always_fails(h, s, msgs, llm):
        raise httpx.ReadTimeout("永久性网络故障（模拟）")

    async def fake_sleep(_):
        return None

    monkeypatch.setattr(dogfood, "run_loop", always_fails)
    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    with pytest.raises(httpx.ReadTimeout):
        await dogfood.run_with_auto_resume(harness, state, messages, llm=None)


@pytest.mark.asyncio
async def test_auto_resume_does_not_catch_non_transient_errors(tmp_path, monkeypatch):
    """非瞬时错误（如普通 ValueError）应该直接冒泡，不被当成"可自动恢复"。"""
    import scripts.run_e2e_dogfood as dogfood
    from core.harness import NodeHarness
    from core.llm import LLMMessage

    state = State.new(node_type="_orchestrator", base_dir=tmp_path)
    harness = NodeHarness(node_type="_orchestrator", system_prompt="s")
    messages = [LLMMessage(role="system", content="s")]

    async def raises_value_error(h, s, msgs, llm):
        raise ValueError("this is not a transient network error")

    monkeypatch.setattr(dogfood, "run_loop", raises_value_error)

    with pytest.raises(ValueError):
        await dogfood.run_with_auto_resume(harness, state, messages, llm=None)

# ↓ 部分测试已随 #627（QC 判定层整体退场，core/quality_checks.py 删除）移除：
#   它们的被测对象是该层本身（_build_state_summary）。层删了测试跟着走。
#   这批 import 断裂曾把整个 collection 挡住 —— 两个各自全绿的 PR 合并后互咬。

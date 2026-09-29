"""chat.py 对话契约层：回复兜底 / 进度描述 / 后台入流 / 中途输入不丢。

不联网、不调真实 LLM。验证 P0/P1 对话可用性修复的纯逻辑部分。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.bootstrap import bootstrap
from core.llm import FRAMEWORK_NOTICE_OPEN, LLMMessage

bootstrap()

import chat as chat_mod   # noqa: E402


# ── 回复契约兜底 ──────────────────────────────────────────────────────────────

class _FakeResult:
    def __init__(self, final_text="", tool_calls=None, status="completed"):
        self.final_text = final_text
        self.tool_calls = tool_calls or []
        self.status = status


def test_honest_fallback_summarizes_tool_calls():
    r = _FakeResult(tool_calls=[{"name": "search_kb"}, {"name": "search_kb"},
                                 {"name": "list_proposals"}])
    out = chat_mod._honest_fallback(r)
    assert "search_kb×2" in out
    assert "list_proposals" in out
    assert "空回复" not in out


def test_honest_fallback_no_tools():
    out = chat_mod._honest_fallback(_FakeResult())
    assert "没能生成回复" in out
    assert "空回复" not in out


@pytest.mark.asyncio
async def test_finalize_reply_returns_clean_text_directly():
    """final_text 非空 → 直接返回，不重问。"""
    r = _FakeResult(final_text="这是正常回复")
    out = await chat_mod._finalize_reply(r, harness=None, state=None,
                                          messages=[], llm=None)
    assert out == "这是正常回复"


@pytest.mark.asyncio
async def test_finalize_reply_retries_then_succeeds(monkeypatch):
    """final_text 空 → 追加 nudge 重问一次 → 拿到干净正文。"""
    retried = _FakeResult(final_text="重问后的回复")

    async def fake_run_loop(harness, state, messages, llm):
        return retried

    monkeypatch.setattr(chat_mod, "run_loop", fake_run_loop)
    messages: list[LLMMessage] = []
    out = await chat_mod._finalize_reply(
        _FakeResult(final_text=""), harness=object(), state=object(),
        messages=messages, llm=object(),
    )
    assert out == "重问后的回复"
    # nudge 是框架中途说话 → framework-notice（user 角色 + 归属信封），不是
    # 中段 system：那会被模型复读，也会被严格网关直接 400。
    assert any(m.role == "user" and FRAMEWORK_NOTICE_OPEN in (m.content or "")
               and "没有给用户任何可见回复" in (m.content or "")
               for m in messages)
    assert not any(m.role == "system" for m in messages)


@pytest.mark.asyncio
async def test_finalize_reply_falls_back_when_retry_also_empty(monkeypatch):
    """重问仍空 → 诚实兜底汇报 retry 轮的工具调用，不是 '(空回复)'。"""
    async def fake_run_loop(harness, state, messages, llm):
        return _FakeResult(final_text="", tool_calls=[{"name": "run_bash"}])

    monkeypatch.setattr(chat_mod, "run_loop", fake_run_loop)
    out = await chat_mod._finalize_reply(
        _FakeResult(final_text=""), harness=object(), state=object(),
        messages=[], llm=object(),
    )
    assert "run_bash" in out
    assert out != "(空回复)"


# ── 进度描述 ──────────────────────────────────────────────────────────────────

def test_describe_call_picks_node_type():
    assert chat_mod._describe_call("run_node", {"node_type": "experiment"}) == \
        "run_node(node_type=experiment)"


def test_describe_call_picks_query_and_truncates():
    long_q = "x" * 200
    out = chat_mod._describe_call("search_kb", {"query": long_q})
    assert out.startswith("search_kb(query=")
    assert "…" in out and len(out) < 120


def test_describe_call_bare_when_no_key():
    assert chat_mod._describe_call("query_project_status", {}) == "query_project_status"


def test_run_node_targets_single():
    assert chat_mod._run_node_targets("run_node", {"node_type": "experiment"}) == "experiment"


def test_run_node_targets_parallel():
    out = chat_mod._run_node_targets("run_nodes_parallel", {
        "jobs": [{"node_type": "literature"}, {"node_type": "hypothesis"}],
    })
    assert out == "literature + hypothesis"


class _FakeState:
    def __init__(self, node_type, depth):
        self.node_type = node_type
        self.depth = depth


def test_progress_labels_child_node_work(capsys):
    """子节点工具调用 → 明确标 [子节点·<type>]，让用户看出不是主对话。"""
    chat_mod._print_progress(_FakeState("experiment", 1), "run_bash", {"cmd": "ls"})
    out = capsys.readouterr().out
    assert "[子节点·experiment]" in out
    assert "run_bash" in out


def test_progress_orchestrator_run_node_header(capsys):
    """orchestrator 起子节点 → 醒目 header 标明起了谁。"""
    chat_mod._print_progress(_FakeState("_orchestrator", 0), "run_node",
                             {"node_type": "literature"})
    out = capsys.readouterr().out
    assert "起子节点 literature" in out


def test_progress_orchestrator_own_tool_plain(capsys):
    """orchestrator 自身普通工具 → 平铺一行，不加子节点标签。"""
    chat_mod._print_progress(_FakeState("_orchestrator", 0), "search_kb",
                             {"query": "LJ fluid"})
    out = capsys.readouterr().out
    assert "子节点" not in out
    assert "search_kb" in out


# ── IO 层：退格保护提示符 + 流式显示 ─────────────────────────────────────────

def test_readline_prompt_wraps_ansi_when_colored():
    """readline 版提示符：ANSI 颜色码用 \\001…\\002 包住（否则退格算错宽度、
    redisplay 从 col0 抹掉 你›）。"""
    if chat_mod._USE_COLOR:
        assert "\001" in chat_mod._USER_PROMPT_RL and "\002" in chat_mod._USER_PROMPT_RL
        # 去掉包裹标记后应还原成 display 版
        unwrapped = chat_mod._USER_PROMPT_RL.replace("\001", "").replace("\002", "")
        assert unwrapped == chat_mod._USER_PROMPT
    else:
        assert chat_mod._USER_PROMPT_RL == chat_mod._USER_PROMPT


def test_stdin_listener_passes_prompt_to_input(monkeypatch):
    """stdin 线程必须把提示符传给 input()（退格保护的关键）——不是 input("")。"""
    seen = {}

    def fake_input(prompt=""):
        seen["prompt"] = prompt
        raise EOFError            # 立即结束循环

    monkeypatch.setattr("builtins.input", fake_input)

    class _Loop:
        def call_soon_threadsafe(self, fn, *a):
            fn(*a)

    q = []

    class _Q:
        def put_nowait(self, x):
            q.append(x)

    chat_mod._stdin_listener(_Loop(), _Q())
    assert seen["prompt"] == chat_mod._USER_PROMPT_RL


def test_norm_text_folds_whitespace():
    assert chat_mod._norm_text("答案 是\n月球。") == "答案是月球。"


def test_emit_above_prompt_plain_when_not_live(capsys):
    """提示符未接管（启动阶段）→ 退化成普通 print，不发光标控制码。"""
    live = chat_mod._PROMPT_LIVE["on"]
    chat_mod._PROMPT_LIVE["on"] = False
    try:
        chat_mod._emit_above_prompt("一行消息")
        out = capsys.readouterr().out
        assert "一行消息" in out
        assert "\033[K" not in out       # 无清行控制码
    finally:
        chat_mod._PROMPT_LIVE["on"] = live


# ── 中途输入不丢：ChatState.deferred_inputs 存在 ──────────────────────────────

def test_chat_state_has_deferred_inputs():
    cs = chat_mod.ChatState()
    assert cs.deferred_inputs == []
    cs.deferred_inputs.append("顺便看下 X")
    assert cs.deferred_inputs == ["顺便看下 X"]


# ── mid-turn slash 安全子集（/bypass 等跑轮中可用；/reset /undo 仍禁）─────────

def test_midturn_slash_whitelist():
    # 档位从三条命令（/bypass、/auto_approve、/continuous）收成一条 /autonomy：
    # 三条各设一个进程全局，然后各自"顺手"把 continuous 也关掉，于是"档位现在
    # 是什么"要读齐三个全局才答得出，而它们能互相矛盾。
    assert chat_mod._midturn_slash_allowed("/autonomy") is True
    assert chat_mod._midturn_slash_allowed("/autonomy continuous") is True
    assert chat_mod._midturn_slash_allowed("/continuous on") is True
    assert chat_mod._midturn_slash_allowed("/status") is True
    assert chat_mod._midturn_slash_allowed("/answer NHC 用 3") is True
    assert chat_mod._midturn_slash_allowed("/btw 注意单位") is True
    # 有并发冲突的仍禁
    assert chat_mod._midturn_slash_allowed("/reset") is False
    assert chat_mod._midturn_slash_allowed("/undo") is False
    assert chat_mod._midturn_slash_allowed("/exit") is False
    # 前缀不误伤（/autonomyX 不是 /autonomy）
    assert chat_mod._midturn_slash_allowed("/autonomyX") is False
    assert chat_mod._midturn_slash_allowed("/continuousX") is False


@pytest.mark.asyncio
async def test_continuous_slash_toggle_and_idle_stop(tmp_path):
    """REPL 开关可持久化；idle /stop 必须能停 autonomous loop。"""
    from core import pause_driver
    from shared.lib import dangerous_commands as dc

    state = chat_mod._make_or_load_orchestrator_state(None, Path(tmp_path))
    old_auto = pause_driver.AUTO_APPROVE_ENABLED
    old_bypass = dc.bypass_enabled()
    try:
        pause_driver.set_auto_approve(False)
        dc.set_bypass_mode(False)
        await chat_mod._handle_slash("/continuous on", state, [])
        assert state.hook_state["continuous_loop"] is True
        assert state.hook_state["continuous_kick_requested"] is True
        assert pause_driver.AUTO_APPROVE_ENABLED is True
        assert dc.bypass_enabled() is True

        await chat_mod._handle_slash("/stop", state, [])
        assert state.hook_state["continuous_loop"] is False
        assert state.hook_state["continuous_phase"] == "stopped"
        # /stop 停的是**这一轮**，不是档位 —— 档位由 /autonomy 管。
        assert state.hook_state["authorized_risk_classes"] == ["*"]
    finally:
        pause_driver.set_auto_approve(old_auto)
        dc.set_bypass_mode(old_bypass)


@pytest.mark.asyncio
async def test_midturn_autonomy_change_takes_effect(tmp_path, monkeypatch):
    """mid-turn 切档 → 进程级开关立即生效（实测痛点：child 连环撞高危确认时
    这是唯一不用杀轮就能解锁的路径）。

    "立刻生效"是这条命令存在的全部理由：等下一个轮边界就等于没有 —— 一轮
    无人值守可以跑几十分钟不产生轮边界（2026-08-23 会话 e46448f0）。
    """
    from pathlib import Path
    from core import pause_driver
    from shared.lib import dangerous_commands as dc

    state = chat_mod._make_or_load_orchestrator_state(None, Path(tmp_path))
    orig = (dc.bypass_enabled(), pause_driver.AUTO_APPROVE_ENABLED)
    try:
        dc.set_bypass_mode(False)
        pause_driver.set_auto_approve(False)
        await chat_mod._handle_slash("/autonomy continuous", state, [])
        assert dc.bypass_enabled() is True
        assert pause_driver.AUTO_APPROVE_ENABLED is True
        # 反方向同样要立刻生效 —— 那是 2026-08-23 完全不生效的那个方向。
        await chat_mod._handle_slash("/autonomy assisted", state, [])
        assert dc.bypass_enabled() is False
        assert pause_driver.AUTO_APPROVE_ENABLED is False
    finally:
        dc.set_bypass_mode(orig[0])
        pause_driver.set_auto_approve(orig[1])

"""blocked 不再是终态 —— 无人值守模式唯一的终态是 complete。

现场（E2E-5b）：agent 报 blocked 说"需要 framework-owner 介入"，诊断完全正确
（16 次重试确实每次死在不同的平台问题上）。但 blocked 被做成不可逆终态：
求救没人接，停摆 12.7 小时。而判决性重放证明最后那个故障是 provider 瞬态 ——
**过几小时自己就好了**，只要"晾一会儿再试"就能爬出来。

"卡住了"描述的是暂态：此路（别的路可能通）此刻（世界会变）。
blocked = 停靠 + 按退避复查 + BLOCKED.md 呼救。人工介入能加速，但不是必需。
"""
from __future__ import annotations

import asyncio

import chat as chat_mod
from core.bootstrap import bootstrap

bootstrap()

BLOCKED_REPLY = ("16 次尝试每次死在不同的框架问题上，需要 framework-owner 介入。\n"
                 "CONTINUOUS_STATUS: blocked")


def _state(tmp_path):
    st = chat_mod._make_or_load_orchestrator_state(None, tmp_path)
    st.hook_state.update(continuous_loop=True, continuous_phase="running")
    return st


def test_blocked_parks_instead_of_stopping(tmp_path):
    """blocked → 停靠并安排复查，绝不进入终态。"""
    st = _state(tmp_path)
    prompt, delay = chat_mod._continuous_followup(
        st, BLOCKED_REPLY, reason="turn_finished")

    assert prompt is not None, "blocked 不许返回 None（None = 终态停机）"
    assert st.hook_state.get("continuous_phase") == "running", \
        "不许把 phase 置成 blocked 终态"
    assert delay >= chat_mod._BLOCKED_PROBE_BASE_S, "首次复查应在退避间隔之后"
    assert "复查" in prompt and "仍然成立" in prompt, \
        "复查提示要让它逐项核查阻塞是否还在"
    tr = st.transcript_path.read_text(encoding="utf-8")
    assert "continuous_blocked_parked" in tr
    assert "human_only_blocked" not in tr, "旧的终态事件不许再出现"


def test_sos_file_is_written_loudly(tmp_path):
    """呼救文件必须落盘 —— 让人能尽快帮，但系统不依赖人才能活。"""
    st = _state(tmp_path)
    chat_mod._continuous_followup(st, BLOCKED_REPLY, reason="turn_finished")
    md = chat_mod._blocked_md_path(st)
    assert md.exists(), "BLOCKED.md 必须存在"
    body = md.read_text(encoding="utf-8")
    assert "framework-owner" in body, "agent 的原话要进呼救记录"
    assert "自动复查" in body, "要写明系统没停、会自动复查"


def test_probe_interval_backs_off_and_caps(tmp_path):
    """复查间隔 30min 起翻倍，封顶 6h —— 停靠状态便宜且永远不死。"""
    st = _state(tmp_path)
    delays = []
    for _ in range(6):
        _, d = chat_mod._continuous_followup(st, BLOCKED_REPLY,
                                             reason="turn_finished")
        delays.append(d)
    assert delays[0] == chat_mod._BLOCKED_PROBE_BASE_S
    assert delays[1] == chat_mod._BLOCKED_PROBE_BASE_S * 2
    assert max(delays) <= chat_mod._BLOCKED_PROBE_MAX_S
    assert delays[-1] == chat_mod._BLOCKED_PROBE_MAX_S


def test_agent_sets_its_own_probe_interval(tmp_path):
    """agent 可以自定复查间隔 —— 只有它知道等的是什么。"""
    st = _state(tmp_path)
    _, d = chat_mod._continuous_followup(
        st, "等 HPC 队列。\nCONTINUOUS_STATUS: blocked check_in=7200",
        reason="turn_finished")
    assert d == 7200.0


def test_unblocking_stamps_resolution(tmp_path):
    """停靠后恢复推进 → 呼救文件盖"已解除"戳、计数清零。"""
    st = _state(tmp_path)
    chat_mod._continuous_followup(st, BLOCKED_REPLY, reason="turn_finished")
    assert st.hook_state.get("continuous_blocked_probes") == 1

    chat_mod._continuous_followup(
        st, "端点恢复了，继续推进。\nCONTINUOUS_STATUS: continue",
        reason="turn_finished")
    assert st.hook_state.get("continuous_blocked_probes") is None
    body = chat_mod._blocked_md_path(st).read_text(encoding="utf-8")
    assert "已解除" in body
    tr = st.transcript_path.read_text(encoding="utf-8")
    assert "continuous_blocked_resolved" in tr


def test_complete_is_still_terminal(tmp_path):
    """别把唯一合法的终态也改没了。"""
    st = _state(tmp_path)
    prompt, _ = chat_mod._continuous_followup(
        st, "交付物齐全。\nCONTINUOUS_STATUS: complete", reason="turn_finished")
    assert prompt is None
    assert st.hook_state.get("continuous_phase") == "complete"


def test_long_probe_delay_is_interruptible(tmp_path):
    """复查间隔以小时计 —— /stop 必须秒级生效，不能等到复查点。"""
    import time

    st = _state(tmp_path)
    cs = chat_mod.ChatState()

    async def _go():
        async def _stop_soon():
            await asyncio.sleep(0.3)
            st.hook_state["continuous_phase"] = "aborted"
        task = asyncio.ensure_future(_stop_soon())
        t0 = time.time()
        ok = await chat_mod._queue_continuous_followup(
            st, cs, BLOCKED_REPLY, reason="turn_finished")
        await task
        return ok, time.time() - t0

    ok, elapsed = asyncio.run(_go())
    assert ok is False, "/stop 后不许再排轮"
    assert elapsed < 10, f"必须秒级中断，实际 {elapsed:.1f}s（复查间隔是 1800s）"


# ── 零产出截断归 infra 账 ───────────────────────────────────────────────────

def test_zero_output_truncation_is_infra_not_node_failure():
    """连续撞满预算零产出 = provider 瞬态故障（判决性重放证明），归 infra 账。

    infra 类 run：派发层机械重派（换个时刻本身就是对瞬态故障的有效扰动），
    不进节点的卡死统计。
    """
    from shared.tools.run_node import _INFRA_FAILURE_CATEGORIES

    assert "provider_tool_call_protocol_error" in _INFRA_FAILURE_CATEGORIES
    # 归类接线：hook_state 标记 → executor 写 FAILURE_CATEGORY_PROTOCOL
    import inspect

    from core import executor
    src = inspect.getsource(executor)
    assert "_zero_output_truncation" in src, "executor 必须消费零产出标记"
    from core import agent_loop
    src2 = inspect.getsource(agent_loop)
    assert "_zero_output_truncation" in src2, "agent_loop 必须打零产出标记"

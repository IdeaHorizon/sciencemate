"""continuous 的定义是无人值守 —— 任何能让它无限期停住的东西都是 bug。

现场（E2E-5a，82 分钟死锁，靠人发现）：

    07:24:06  tool_call present_decision_package
    07:24:06  loop_pause          ← 注册了 pause
    07:24:06  continuous_followup_suppressed {"reason": "live_pause_pending"}
    （此后 82 分钟无事发生）

三条同一秒 —— 意思是**创建 pause 的那一轮已经结束了**，没有任何人在等这个
答复，auto-approve 永远不会被调用。而 continuous 有一条保护"有 pause 挂着就
别抢方向盘"（防两个控制面打架，false-PROCEED 事故的教训），于是：

    pause 没人答 → continuous 不敢动 → 永久死锁

根因：那条保护的**前提是"pause 有人管"，而注册表里根本没有这个信息**。
`list_paused()` 只答"有没有登记"。前提没被验证。

修法不是加超时兜底，是把假设变成注册表里的事实（`core.pause._DRIVEN`）。
"""
from __future__ import annotations

import asyncio

import chat as chat_mod
from core import pause as pause_mod
from core import pause_driver
from core.bootstrap import bootstrap

bootstrap()


def _pause_event(run_id: str, *, decision=True):
    meta = {"type": "decision_package", "recommended_option_index": 1} if decision else {}
    return pause_mod.PauseEvent(
        question="Post-node decision for experiment",
        options=["revise", "proceed", "redirect"],
        asking_node_type="_orchestrator",
        asking_run_id=run_id,
        pending_tool_call_id="recovered_0",
        metadata=meta,
    )


def _register(run_id: str, **kw):
    ctx = pause_mod.PausedRunContext(
        run_id=run_id, state=None, messages=[], harness=None, llm=None,
        pending_tool_call_id="recovered_0",
        pause_event=_pause_event(run_id, **kw))
    pause_mod.register_pause(ctx)
    return ctx


def setup_function():
    pause_mod.clear_all()


def teardown_function():
    pause_mod.clear_all()


# ── 注册表要能回答"有没有人管" ──────────────────────────────────────────────

def test_registry_knows_whether_anyone_is_driving():
    _register("orchestrator__p1")
    assert pause_mod.has_driver("orchestrator__p1") is False
    assert [c.run_id for c in pause_mod.undriven_pauses()] == ["orchestrator__p1"]

    pause_mod.claim_driver("orchestrator__p1")
    assert pause_mod.has_driver("orchestrator__p1") is True
    assert pause_mod.undriven_pauses() == []

    pause_mod.release_driver("orchestrator__p1")
    assert pause_mod.undriven_pauses() != []


def test_clear_pause_also_clears_driver():
    _register("r1")
    pause_mod.claim_driver("r1")
    pause_mod.clear_pause("r1")
    assert pause_mod.has_driver("r1") is False, "残留标记会让下一个同名 run 被误判有人管"


def test_resolve_pause_answer_claims_and_releases(monkeypatch):
    """驾驶员登记接在**所有前端共用的入口**上（chat 队列 / stdin 都走它）。

    只在 chat.py 那一处登记的话，run_node 前端就漏了。
    """
    _register("r2")
    monkeypatch.setattr(pause_driver, "AUTO_APPROVE_ENABLED", True)
    seen = {}

    async def _ask(pe):
        seen["driving"] = pause_mod.has_driver("r2")
        return "2"

    asyncio.run(pause_driver.resolve_pause_answer(_pause_event("r2"), _ask))
    assert seen.get("driving") is True, "等答复期间必须登记为有人管"
    assert pause_mod.has_driver("r2") is False, "答完必须撤销登记"


# ── 死锁回放：走 _queue_continuous_followup 真路径 ──────────────────────────

def _cstate(tmp_path):
    st = chat_mod._make_or_load_orchestrator_state(None, tmp_path)
    st.hook_state.update(continuous_loop=True, continuous_phase="running")
    return st


def test_driven_pause_still_suppresses(tmp_path):
    """有人管的 pause → 照旧不抢方向盘（原有保护一点不能少）。"""
    st = _cstate(tmp_path)
    cs = chat_mod.ChatState()
    _register("orchestrator__driven")
    pause_mod.claim_driver("orchestrator__driven")

    ok = asyncio.run(chat_mod._queue_continuous_followup(
        st, cs, "在等答复\nCONTINUOUS_STATUS: continue", reason="turn_finished"))
    assert ok is False, "有人管的 pause 期间不许再排一轮（两个控制面会打架）"
    tr = st.transcript_path.read_text(encoding="utf-8")
    assert "live_pause_pending" in tr


def test_orphan_pause_is_resolved_not_deadlocked(tmp_path, monkeypatch):
    """没人管的 pause → 自己用 auto-approve 答掉并继续，绝不死锁。

    走 `_queue_continuous_followup` 真路径 —— 只测 undriven_pauses() 的话，
    把 chat.py 那段接线摘掉测试照样全绿。
    """
    st = _cstate(tmp_path)
    cs = chat_mod.ChatState()
    _register("orchestrator__orphan")          # 注册但没人 claim

    drove = {}

    async def _fake_chain(*, ask_fn=None, finalize_fn=None):
        drove["answer"] = await ask_fn(_pause_event("orchestrator__orphan"))
        pause_mod.clear_pause("orchestrator__orphan")
        return ""

    monkeypatch.setattr(pause_driver, "drive_pause_chain", _fake_chain)

    ok = asyncio.run(chat_mod._queue_continuous_followup(
        st, cs, "推进中\nCONTINUOUS_STATUS: continue", reason="turn_finished"))
    assert ok is True, "孤儿 pause 不许把 continuous 卡住"
    assert drove.get("answer") == "2", "该用 auto-approve 的推荐项作答"
    tr = st.transcript_path.read_text(encoding="utf-8")
    assert "orphan_pause_detected" in tr and "orphan_pause_resolved" in tr


def test_resolution_failure_never_deadlocks(tmp_path, monkeypatch):
    """收拾孤儿失败也必须放行 —— 否则退化回今天这个死锁。"""
    st = _cstate(tmp_path)
    cs = chat_mod.ChatState()
    _register("orchestrator__boom")

    async def _boom(*, ask_fn=None, finalize_fn=None):
        raise RuntimeError("resume 炸了")

    monkeypatch.setattr(pause_driver, "drive_pause_chain", _boom)

    ok = asyncio.run(chat_mod._queue_continuous_followup(
        st, cs, "推进中\nCONTINUOUS_STATUS: continue", reason="turn_finished"))
    assert ok is True, "收拾失败也得继续跑，不能卡住"
    assert pause_mod.list_paused() == [], "注册表必须清干净，否则下一轮又被同一批卡住"
    tr = st.transcript_path.read_text(encoding="utf-8")
    assert "orphan_pause_resolve_failed" in tr


def test_mixed_driven_and_orphan_is_conservative(tmp_path):
    """一部分有人管 → 保守起见照旧不动（别在有人开车时抢方向盘）。"""
    st = _cstate(tmp_path)
    cs = chat_mod.ChatState()
    _register("orchestrator__a")
    _register("orchestrator__b")
    pause_mod.claim_driver("orchestrator__a")

    ok = asyncio.run(chat_mod._queue_continuous_followup(
        st, cs, "在等\nCONTINUOUS_STATUS: continue", reason="turn_finished"))
    assert ok is False


def test_resolution_itself_must_be_bounded(tmp_path, monkeypatch):
    """收拾孤儿的过程**自己**不许挂住 —— 否则防死锁的机制变成死锁源。

    这条是我自己踩出来的：第一版直接 `await drive_pause_chain(...)`，而 resume
    会真的跑 LLM。全套测试从 90 秒变成跑不完，就卡在这儿。
    今天已经修过三次同形状的无界 await（工具收尾 / 超时收尾 / child-wait），
    这是第四次，而且是我新写的代码。
    """
    st = _cstate(tmp_path)
    cs = chat_mod.ChatState()
    _register("orchestrator__hang")

    async def _never_returns(*, ask_fn=None, finalize_fn=None):
        await asyncio.sleep(3600)

    monkeypatch.setattr(pause_driver, "drive_pause_chain", _never_returns)
    # 常量搬进 core.pause_driver（CLI 与平台共用同一份无人值守语义），
    # 打新地址。留一个"只读别名"给旧名字反而更糟：monkeypatch 写得进、
    # 读的却是别处，界失效且看不出来。
    from core import pause_driver as _pd
    monkeypatch.setattr(_pd, "ORPHAN_PAUSE_RESOLVE_MAX_S", 0.3)

    import time as _t
    t0 = _t.time()
    ok = asyncio.run(chat_mod._queue_continuous_followup(
        st, cs, "推进中\nCONTINUOUS_STATUS: continue", reason="turn_finished"))
    elapsed = _t.time() - t0

    assert ok is True, "收拾挂住了也必须放行"
    assert elapsed < 5, f"必须在上界内收场，实际 {elapsed:.1f}s"
    assert pause_mod.list_paused() == [], "注册表必须清干净"
    tr = st.transcript_path.read_text(encoding="utf-8")
    assert "orphan_pause_resolve_failed" in tr

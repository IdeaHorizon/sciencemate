"""continuous 层的空轮驻定重放（E2E-5a 事故的 chat.py 侧接线）。

run_loop 的空轮回滚重试用尽后（status="void"），continuous 不许把它当"回答"：
没有 status 可解析、没有进度可指纹，追加任何 followup prompt 都会破坏驻定性。
正确动作 = 按退避原样重放同一请求（sentinel 逐字节等值识别，不 append 消息）。
provider 恢复的那一刻它自己爬出来 —— 不需要人，也不需要熔断。
"""
from __future__ import annotations

import pytest

import chat as chat_mod
from core.bootstrap import bootstrap

bootstrap()


@pytest.fixture(autouse=True)
def _isolate_pause_registry():
    """core.pause 的注册表是**进程级全局**，本文件有测试走真的
    `_queue_continuous_followup` —— 它一看到未被认领的 pause 就去做孤儿解析
    （上界 600s）。全量里前面某个测试漏下来的 pause 会让这里挂 10 分钟
    （实测：单跑 0.15s、全量挂死，就是这么来的）。测边界要自己划干净。"""
    from core.pause import clear_all
    clear_all()
    yield
    clear_all()


def _state(tmp_path):
    st = chat_mod._make_or_load_orchestrator_state(None, tmp_path)
    st.hook_state.update(continuous_loop=True, continuous_phase="running")
    return st


VOID_REPLY = "[近乎空响应] 模型本轮几乎没有生成内容（completion_tokens=1，prompt_tokens=174772），重试后仍如此。"


def test_void_turn_schedules_bare_replay_not_a_followup(tmp_path):
    """空轮 → sentinel 重放，不走状态机、不产生新 followup 文本。"""
    st = _state(tmp_path)
    prompt, delay = chat_mod._continuous_followup(
        st, VOID_REPLY, reason="void_turn")
    assert prompt == chat_mod._VOID_RETRY_PROMPT
    assert delay == chat_mod._VOID_FOLLOWUP_BASE_S
    tr = st.transcript_path.read_text(encoding="utf-8")
    assert "continuous_void_replay_scheduled" in tr


def test_replay_backs_off_and_caps(tmp_path):
    st = _state(tmp_path)
    delays = [chat_mod._continuous_followup(st, VOID_REPLY, reason="void_turn")[1]
              for _ in range(8)]
    assert delays[0] == chat_mod._VOID_FOLLOWUP_BASE_S
    assert delays[1] == chat_mod._VOID_FOLLOWUP_BASE_S * 2
    assert max(delays) <= chat_mod._VOID_FOLLOWUP_MAX_S
    assert delays[-1] == chat_mod._VOID_FOLLOWUP_MAX_S


def test_recovery_clears_void_counter(tmp_path):
    """恢复后计数清零 —— 下次空轮从头退避，不背旧账。"""
    st = _state(tmp_path)
    chat_mod._continuous_followup(st, VOID_REPLY, reason="void_turn")
    assert st.hook_state.get("continuous_void_rounds") == 1
    prompt, _ = chat_mod._continuous_followup(
        st, "推进中。\nCONTINUOUS_STATUS: continue", reason="turn_finished")
    assert st.hook_state.get("continuous_void_rounds") is None
    assert prompt != chat_mod._VOID_RETRY_PROMPT
    tr = st.transcript_path.read_text(encoding="utf-8")
    assert "continuous_void_replay_recovered" in tr


def test_sentinel_is_recognized_as_continuous_turn():
    """sentinel 必须带内部前缀 —— 否则会被当真实用户输入重置 continuous 状态。"""
    assert chat_mod._is_continuous_turn(chat_mod._VOID_RETRY_PROMPT)


def test_sentinel_survives_strip():
    """驱动循环对队列输入做 .strip() —— sentinel 必须逐字节存活（等值识别）。"""
    assert chat_mod._VOID_RETRY_PROMPT.strip() == chat_mod._VOID_RETRY_PROMPT


def test_void_reply_is_not_parsed_as_status(tmp_path):
    """空轮散文不含 CONTINUOUS_STATUS —— 断言它不会被误读成 blocked/complete。"""
    assert chat_mod._continuous_status(VOID_REPLY) is None


def test_sentinel_reaches_queue_undecorated(tmp_path, monkeypatch):
    """真接缝：sentinel 穿过 _queue_continuous_followup 全流程后必须逐字节
    原样入队 —— 等待注记 / check_in 提示任何一个粘上去都会把重放变回普通消息
    （本周 schema-seam 教训：内部函数测过 ≠ 接缝通了）。"""
    import asyncio

    st = _state(tmp_path)
    cs = chat_mod.ChatState()

    async def _no_wait(state):
        return 37.0                      # 模拟真等过 —— 装饰逻辑的触发条件

    monkeypatch.setattr(chat_mod, "_wait_for_child_progress", _no_wait)
    monkeypatch.setattr(chat_mod, "_VOID_FOLLOWUP_BASE_S", 0.01)
    st.hook_state["continuous_check_in_note"] = "间隔越界提醒"   # 第二个装饰源

    ok = asyncio.run(chat_mod._queue_continuous_followup(
        st, cs, VOID_REPLY, reason="void_turn"))
    assert ok is True
    queued = cs.input_queue.get_nowait()
    assert queued == chat_mod._VOID_RETRY_PROMPT, \
        f"sentinel 被装饰破坏：{queued!r}"

"""自主循环停机，用户必须**收到**，而且必须知道**为什么**。

## 现场（yuankk，2026-09-17）

从 8:30 起没有新迭代。界面上：卡片还转着，一句说明也没有。

查下来是两段各自断开的链：

1. `next_action` 把六种停机（complete / no_delta_repeat / stall_livelock /
   repeated_turn_errors / repeated_producing_failure / system_node_pingpong）
   全压成一个 `followup_declined` —— 一个**不含任何内容**的标签。真正的理由就在
   `hook_state["continuous_abort_reason"]` 里躺着，出口不取。
2. worker 老老实实写了 `unattended_loop_stopped`，平台侧**零消费方**。
   写了没人读，和没写一样，而且更难查 —— 代码里明明有 append_transcript。

「机制存在但没接到路径」，一条链上连着栽两次。
"""
from __future__ import annotations

import pytest

from core.state import State


def _stopped_action(tmp_path, *, phase: str, detail: str):
    """走真的 `next_action`：让 `_continuous_followup` 判停，看出口带出什么。"""
    import asyncio

    import chat
    from core.session_driver import next_action

    base = tmp_path / "run"
    base.mkdir()
    state = State.new(node_type="_orchestrator", base_dir=base, project_id="p1")
    chat._set_continuous_loop(state, True)
    state.hook_state["continuous_phase"] = "running"

    def _decline(st, reply, *, reason):
        st.hook_state["continuous_phase"] = phase
        st.hook_state["continuous_abort_reason"] = detail
        return None, 0.0

    original = chat._continuous_followup
    chat._continuous_followup = _decline
    try:
        return asyncio.run(next_action(state, "…", reason="turn_done",
                                       allow_child_wait=False))
    finally:
        chat._continuous_followup = original


def test_an_aborted_loop_carries_the_real_reason_out(tmp_path):
    detail = "连续输出一字不差且无新的机械 delta 可注入 —— 重试只会得到同一句话。"
    action = _stopped_action(tmp_path, phase="aborted", detail=detail)
    assert action.kind == "stop"
    assert action.reason == "loop_aborted", (
        "又压成一个不含内容的标签了 —— 上层只能说'停了'，说不出为什么"
    )
    assert action.extra["detail"] == detail


def test_a_completed_loop_is_told_apart_from_an_aborted_one(tmp_path):
    action = _stopped_action(tmp_path, phase="complete", detail="")
    assert action.reason == "research_complete", "做完和卡死不能报成同一件事"


# 平台侧那一半（"这条事件到不到得了人"）在
# platform/backend/tests/test_a_stopped_loop_reaches_the_user.py —— 它需要
# sqlalchemy，跑在后端那套依赖里。

"""一条推不动的 flow 必须**响**，而且模型必须**说得出**自己被它卡住。

## 现场（yuankk，2026-09-17，node20）

reviewer 把根因指向 `postprocess`（早已改造成 figures 服务，`post_run_flow: none`），
决策授权通过。之后：

    hook 每轮："⏳ NEXT: run_node('postprocess')"
    模型每轮：照做
    run_node：`if node_owes_post_node_flow('postprocess')` → False → 绑定整块跳过
    账本：一动不动。没有 action_attempt_count、没有 action_target_node

于是**三件事同时不成立**：flow 关不掉；空转熔断（数的是成功绑定次数）一次都没加；
40 轮后模型申报 `CONTINUOUS_STATUS: blocked`，被 `reason=pending_post_node_flow`
驳回 —— **它被卡住的那件事，恰好是禁止它说自己被卡住的那件事**。最后以
`no_delta_repeat` 静默停摆，用户界面上还写着"正在跑"。

## 两条判据

1. 计数搬到**跳不过去的那一侧**（注入，不是绑定）：不管将来冒出哪种新的关不掉
   形态，它都在 N 轮内响。
2. 驳回 `blocked` 的理由是"你还有自己推得动的东西"。一条已经证明推不动的 flow
   让这个理由不成立 —— 那时 `blocked` 就是唯一诚实的答案，必须放行。
   （`complete` 不分这一档：空转不是完成。）
"""
from __future__ import annotations

import pytest

from core import closure as _closure
from core.loader import load_harness, node_owes_post_node_flow
from core.loop_hooks import HookContext
from core.loop_hooks_builtin import _post_node_review_flow_reminder_on_turn_start
from core.state import State


def _yuankks_entry() -> dict:
    """yuankk 那条 entry 的真实形状：授权给一个服务节点，一次都没绑上过。"""
    return {
        "producing_node": "writing",
        "producing_run_id": "run-w-1",
        "artifact_ids": ["research_article__x"],
        "review_state": "done",
        "curator_state": "done",
        "decision_state": "action_authorized",
        "authorized_action": "redirect_upstream",
        "authorized_target_node": "postprocess",
        # 空转 40 轮的账面证据：这三个字段一个都没有
        # "action_attempt_count" / "action_target_node" / "action_started_at"
    }


def _orchestrator_state(tmp_path) -> State:
    base = tmp_path / "run"
    base.mkdir()
    return State.new(node_type="_orchestrator", base_dir=base, project_id="p1")


def _events(state: State) -> list[dict]:
    import json

    if not state.transcript_path.exists():
        return []
    return [json.loads(line) for line in
            state.transcript_path.read_text().splitlines() if line.strip()]


def _one_turn(state: State):
    return _post_node_review_flow_reminder_on_turn_start(HookContext(
        harness=load_harness("_orchestrator"), state=state, messages=[], turn=1))


def test_the_premise_this_target_really_cannot_close_the_flow():
    """前提：postprocess 起多少次都绑不上 —— 塌了下面全是空断言。"""
    assert node_owes_post_node_flow("postprocess") is False
    assert node_owes_post_node_flow("writing") is True


def test_the_breaker_counts_even_though_nothing_ever_binds(tmp_path):
    """熔断器管的是**下一个还没想到的形态**。

    postprocess 这一种已经在第一轮就被谓词拦掉了（见本文件最后两条）。所以这里
    故意用一个**合法**目标：授权没问题、催促没问题，可账本就是一动不动 —— 无论
    将来是哪个新缝造成的，计数都在注入侧照加，因为注入是跳不过去的。
    """
    state = _orchestrator_state(tmp_path)
    entry = _yuankks_entry() | {"authorized_target_node": "experiment"}
    state.hook_state["pending_post_node_flow"] = [entry]

    for round_no in range(1, _closure.MAX_FLOW_STALL_ROUNDS + 1):
        msgs = _one_turn(state)
        assert entry[_closure.STALL_ROUNDS_KEY] == round_no
        assert not _closure.flow_is_stalled(entry)
        assert "run_node" in msgs[0].content, "还没到熔断线，照常催"
        # 账本原样不动 —— 这正是现场的形状：绑定从没发生过
        assert "action_attempt_count" not in entry

    msgs = _one_turn(state)
    assert _closure.flow_is_stalled(entry), (
        "空转 %d 轮仍未熔断 —— 计数器又挂回绑定那一侧了吗？"
        % entry[_closure.STALL_ROUNDS_KEY]
    )
    text = msgs[0].content
    assert "别再起那个节点了" in text
    assert "report_blocker" in text and "present_decision_package" in text
    assert "blocked" in text, "得告诉它现在可以如实申报了"
    assert any(e.get("event") == "post_node_flow_stalled"
               for e in _events(state)), "没留下可查的痕迹"


def test_progress_resets_the_streak(tmp_path):
    """对照：账本真动过一下，计数归零 —— 这道闸不能把慢工判成空转。"""
    state = _orchestrator_state(tmp_path)
    entry = _yuankks_entry() | {"authorized_target_node": "experiment"}
    state.hook_state["pending_post_node_flow"] = [entry]

    for _ in range(3):
        _one_turn(state)
    assert entry[_closure.STALL_ROUNDS_KEY] == 3

    entry["action_attempt_count"] = 1        # 真绑上了一次 = 有进展
    _one_turn(state)
    assert entry[_closure.STALL_ROUNDS_KEY] == 1
    assert not _closure.flow_is_stalled(entry)


def test_the_signature_ignores_things_that_always_change(tmp_path):
    """指纹只取"推进会改变"的字段。混进时间戳/轮次，熔断器就永远不响。"""
    entry = _yuankks_entry()
    first = _closure.flow_progress_signature(entry)
    entry["action_started_at"] = "2026-09-17T08:30:00"
    entry["decision_history"] = [{"x": 1}]
    assert _closure.flow_progress_signature(entry) == first
    entry["decision_state"] = "action_in_progress"
    assert _closure.flow_progress_signature(entry) != first


# ── blocked 必须说得出口 ────────────────────────────────────────────────────

def _decide(state: State, reported: str) -> str:
    """跑真的终态门禁，看它把这次申报**当成了什么**。

    判据落在 transcript 上而不是返回值：blocked 被接受会走 `_park_blocked`（返回
    下一轮），complete 被接受会返回 None —— 两者返回值形状不同，拿返回值判会把
    "接受 complete"和"提前 return"混成一类（第一版就栽在这）。
    """
    import chat

    chat._set_continuous_loop(state, True)     # 终态门禁只在续轮模式下存在
    before = len(_events(state))
    chat._continuous_followup(state, f"没辙了\nCONTINUOUS_STATUS: {reported}",
                              reason="turn_done")
    seen = [e.get("event") for e in _events(state)[before:]]
    if "continuous_terminal_status_rejected" in seen:
        return "rejected"
    if "continuous_blocked_parked" in seen:
        return "accepted"
    if any(e.get("event") == "continuous_stopped" and e.get("reason") == "complete"
           for e in _events(state)[before:]):
        return "accepted"
    return f"neither({seen})"


@pytest.mark.parametrize("stall_rounds,expect", [
    (1, "rejected"),                                     # 还推得动 → 照旧驳回
    (_closure.MAX_FLOW_STALL_ROUNDS + 1, "accepted"),    # 已证明推不动 → 必须放行
])
def test_blocked_is_rejected_only_while_the_flow_is_still_movable(
        tmp_path, stall_rounds, expect):
    state = _orchestrator_state(tmp_path)
    entry = _yuankks_entry()
    entry[_closure.STALL_ROUNDS_KEY] = stall_rounds
    state.hook_state["pending_post_node_flow"] = [entry]
    assert _decide(state, "blocked") == expect, (
        "一条已经证明推不动的 flow 还在驳回 blocked —— "
        "「你被它卡住」和「你不许说你被它卡住」同时为真"
    )


def test_complete_is_still_rejected_by_a_stuck_flow(tmp_path):
    """空转不是完成。放宽只针对 blocked，绝不顺手把 complete 也放了。"""
    state = _orchestrator_state(tmp_path)
    entry = _yuankks_entry()
    entry[_closure.STALL_ROUNDS_KEY] = _closure.MAX_FLOW_STALL_ROUNDS + 99
    state.hook_state["pending_post_node_flow"] = [entry]
    assert _decide(state, "complete") == "rejected"


def test_a_legacy_unexecutable_authorization_is_not_re_urged(tmp_path):
    """存量 entry：2026-09-17 之前授权侧不校验，账本里躺着起了也关不掉的授权。

    催它去起那个节点，就是在**制造**那 40 轮。第一轮就得改口，不用等熔断。
    """
    state = _orchestrator_state(tmp_path)
    entry = _yuankks_entry()               # 授权目标 = postprocess（服务节点）
    state.hook_state["pending_post_node_flow"] = [entry]

    text = _one_turn(state)[0].content
    assert "这条授权执行不了" in text
    assert "NEXT: execute the authorized review action" not in text
    assert "present_decision_package" in text, "得给出唯一的出路"
    assert any(e.get("event") == "post_node_flow_authorization_is_unexecutable"
               for e in _events(state))


def test_an_executable_authorization_is_still_urged(tmp_path):
    """对照：合法授权照旧催 —— 这道闸不能顺手把正常返修也拦了。"""
    state = _orchestrator_state(tmp_path)
    entry = _yuankks_entry() | {"authorized_target_node": "experiment"}
    state.hook_state["pending_post_node_flow"] = [entry]

    text = _one_turn(state)[0].content
    assert "NEXT: execute the authorized review action" in text
    assert "run_node(node_type='experiment'" in text

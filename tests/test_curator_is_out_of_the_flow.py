"""curator 不再是 post-producing flow 的一环（wangd 2026-08-19）。

## 为什么拆掉

curator 此前是每个 producing 节点跑完必走的第 3 步，`curator_state != "done"`
就拦住下游一切派发。实测（session 976e70e1）：

    curator 跑 5 次，KB 写入 **0** 次，3 次明确 verdict=ok_no_op（扫全了零候选），
    代价 2.42M tokens —— 比真去查文献的 literature 节点（1.94M）还贵。

「每个节点跑完都有东西值得沉淀」这个前提，被数据否掉了。前提没了，围绕它建的
一整套机制（#272 的专用选项集、retry_curator 动作、整合重试上限、两道拒绝
PROCEED 的门、legacy 提醒 hook）就没有防守对象。

它们还制造了 2026-08-19 那次死循环：curator 卡在一个**不可满足**的条件上
（压缩日志扫不到）→ 菜单撤下 PROCEED → 而 UI 拿到的是另一套选项集 → 人点三次
全被静默丢弃。

## 现在

curator 是**按需调取的后台节点**。调度器判断值不值得沉淀；不值得就不起，
**没有要记账的东西**——记账等于把执行当默认、把不执行当例外，负担仍在「不做」
那一侧，而按需执行的默认就是不执行。

被调用时 #229 仍成立：跑了就得真跑过（见 test_curator_gate_is_satisfiable.py）。
"""
from __future__ import annotations

from pathlib import Path

from core.state import State
from shared.tools.library.decision_package import _NORMAL_ACTIONS, record_decision_answer


def _state(tmp: Path) -> State:
    st = State.new(node_type="_orchestrator", base_dir=tmp / "runs", project_id="p1")
    st.project_root = tmp / "proj"
    (tmp / "proj").mkdir(parents=True, exist_ok=True)
    return st


def _flow(state: State) -> dict:
    """review 已通过的 flow entry —— 注意：**没有 curator_state 这个字段了**。"""
    entry = {
        "producing_node": "literature",
        "producing_run_id": "run-lit-1",
        "artifact_ids": ["survey_report__peer_assisted"],
        "review_state": "done",
        "review_critique_artifact_id": "review_critique__x",
        "decision_state": "awaiting_human",
        "decision_options": list(_NORMAL_ACTIONS),
    }
    state.hook_state["pending_post_node_flow"] = [entry]
    return entry


_META = {"type": "decision_package", "producing_run_id": "run-lit-1"}


def test_review_done_is_enough_to_close_the_flow():
    """两步闭合：producing → reviewer → decision。中间没有 curator 那一步。"""
    from shared.tools.run_node import _unresolved_flow_reason

    entry = {
        "producing_node": "literature",
        "producing_run_id": "run-lit-1",
        "review_state": "done",
        "review_critique_artifact_id": "review_critique__x",
        "decision_state": "done",
        "accepted_action": "proceed",
    }
    assert _unresolved_flow_reason(entry) is None, (
        "review 过了、决策做了，flow 就该闭合 —— curator 不再是它的前置"
    )


def test_a_flow_entry_no_longer_carries_curator_state():
    """新建的 flow entry 不带 curator_state —— 状态不存在，就没人能读错它。"""
    from shared.tools import run_node

    src = Path(run_node.__file__).read_text(encoding="utf-8")
    body = "\n".join(
        line for line in src.splitlines() if not line.lstrip().startswith("#"))
    assert '"curator_state"' not in body, (
        "curator_state 又回到代码里了 —— 它是被删掉的流程状态，不是被跳过的一步"
    )


def test_the_decision_menu_has_no_curator_action(tmp_path):
    """`retry_curator` 不再是一个可选动作 —— 它对应的流程状态已不存在。"""
    state = _state(tmp_path)
    _flow(state)
    options = state.hook_state["pending_post_node_flow"][0]["decision_options"]

    assert "retry_curator" not in options
    assert options == list(_NORMAL_ACTIONS)


def test_menu_matches_what_the_ledger_will_accept(tmp_path):
    """菜单摆什么，判定就得认什么 —— 否则人只能反复撞墙。

    这条是从被删掉的 test_curator_gate_in_decision.py 里救出来的：它当初钉的是
    curator 那一档，而这个不变量与 curator 无关，是**所有**呈递都该成立的。
    现在由 Offer 从结构上保证（正文/UI/账本都是它的投影），这里守住行为面。
    """
    state = _state(tmp_path)
    _flow(state)
    options = state.hook_state["pending_post_node_flow"][0]["decision_options"]

    entry = record_decision_answer(state, _META, "1")
    assert entry["accepted_action"] == options[0], (
        "菜单第一项必须是判定会接受的动作"
    )
    assert not entry.get("decision_rejection")

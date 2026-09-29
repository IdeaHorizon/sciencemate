"""人在决策卡上写的纠正意见，必须跟着 REVISE 一起到达被重跑的那个节点。

实测（2026-08-30/31，本机部署跑真课题，协作档）：
课题负责人对预注册提了一条会被审稿人当场打回的方法学混淆
（对照组 1 维 normal form vs 非正规组 N=10，TPR/FPR 差异分不清是维度还是非正规性），
选 REVISE 并写明理由 —— **说了两遍**，`research_plan` 里
`S1 | construct_normal_controls | 1 维 normal form` 一字未动。

追下去：人的原话只出现在 orchestrator 的 conversation/transcript 里，
两个 hypothesis run 的 messages_checkpoint 里**零次**。
根因不是丢包，是**结构上没给人留位置**：

    feedback = entry.get("recommended_feedback") or ""      # ← 只有 reviewer 那份
    node_inputs={"mode": "revise", "reviewer_feedback": ...}

而人的 `note` 只被记进 `decision_history[-1]["note"]` —— 那是账本，没有执行路径读它。
于是节点照着 reviewer 的意见重做一遍，调度器把这一轮总结成"你的意见已被采纳"：
**报告里有，产物里没有。**

判据落在"节点收到了什么"上，不落在"账本里记没记"上 ——
记了但没人读，正是这个缺陷本身的形状。
"""
from __future__ import annotations

import pytest

from core.decision_offer import Choice, Offer


def _entry_with_note(note: str) -> dict:
    """走真的 resolve_answer + 记账路径，拿到 REVISE 被受理后的 entry。"""
    from shared.tools.library.decision_package import record_decision_answer

    class _State:
        def __init__(self):
            self.transcript = []
            # 呈递方登记的 flow entry —— record_decision_answer 按
            # producing_run_id 在这里找它要更新的那一条。
            self.hook_state = {
                "pending_post_node_flow": [{
                    "producing_run_id": "run_prod_1",
                    # 生产里登记 flow entry 写的是 `producing_node`（run_node
                    # 那一处）；`source_node_type` 是 **pause metadata** 的字段。
                    # 原来这里只写了后者，于是 REVISE 的目标解析成 None ——
                    # 一个生产造不出来的形状，藏住的正是"授权了一个不可执行动作"
                    # 这件事本身。
                    "producing_node": "hypothesis",
                    "review_state": "done",
                    "decision_options": ["proceed", "revise"],
                }],
            }

        def append_transcript(self, kind, **kw):
            self.transcript.append((kind, kw))

    offer = Offer(
        decision_id="run_x:p1",
        kind="decision_package",
        question="Post-node decision for hypothesis",
        choices=(
            Choice(id="proceed", label="PROCEED to next stage", description="接受本轮产物"),
            Choice(id="revise", label="REVISE", description="带审查反馈重跑产出节点"),
        ),
        recommended_id="proceed",
    )
    payload = offer.to_pause_payload()
    payload["metadata"] = {
        "type": "decision_package",
        "producing_run_id": "run_prod_1",
    }
    state = _State()
    return record_decision_answer(
        state, payload,
        {"offer_id": offer.offer_id, "choice_id": "revise", "note": note},
    ), state


def test_human_note_is_carried_on_the_entry_not_only_in_the_ledger():
    """人的原话必须挂在 entry 上 —— 只进 decision_history 等于没人读得到。"""
    note = "对照组必须跟非正规组同维：n=10 的正规雅可比，cond(V)=1。"
    entry, _ = _entry_with_note(note)
    assert entry.get("accepted_action") == "revise"
    assert entry.get("human_note") == note, (
        "人的纠正意见没挂到 entry 上 —— 执行路径只读 entry，"
        "留在 decision_history 里的等于没说"
    )


@pytest.mark.asyncio
async def test_revise_dispatch_carries_the_human_directive_to_the_node(monkeypatch):
    """真入口：`_execute_authorized_action` 派发重跑时，节点必须收到人的指示。

    这条才是防复发的那一条 —— entry 上挂着但派发时不带，缺陷原样活着。
    """
    from core import pause_driver

    note = "对照组改成 n=10 的正规雅可比，特征向量正交（cond(V)=1），主特征值逼近虚轴。"
    entry, state = _entry_with_note(note)
    entry["decision_state"] = "action_authorized"
    entry["authorized_target_node"] = "hypothesis"
    entry["authorized_action"] = "REVISE"
    entry["recommended_feedback"] = "reviewer: Q4 的估计器未指定。"

    seen: dict = {}

    async def _fake_run_node(state_, target, *, node_inputs, user_note=None, **kw):
        seen["target"] = target
        seen["inputs"] = node_inputs

    import shared.tools.run_node as run_node_mod
    monkeypatch.setattr(run_node_mod, "_run_node_tool", _fake_run_node)

    class _Ctx:
        pass
    ctx = _Ctx()
    ctx.state = state

    ok = await pause_driver._execute_authorized_action(ctx, entry)
    assert ok is True
    assert seen["target"] == "hypothesis"

    blob = " ".join(str(v) for v in seen["inputs"].values())
    assert "n=10 的正规雅可比" in blob, (
        "人的纠正意见没跟着 REVISE 到达节点 —— "
        "节点只会照 reviewer 的意见重做一遍"
    )
    # reviewer 那份不能被顶掉：两个来源都要在，且能分辨谁说的。
    assert "Q4 的估计器未指定" in blob
    assert seen["inputs"].get("human_directive"), "人的指示要有自己的字段，不能只混在 reviewer_feedback 里"

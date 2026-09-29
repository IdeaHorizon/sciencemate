"""post-producing flow 由**运行时**走完，调度器一轮都不花（Move 1d）。

## 改之前

flow 的三步（后为两步）由调度器逐步照抄执行：提醒 hook 每轮把下一步调用**连参数
都打印出来**，模型抄一遍，框架再用一排墙防它抄错。

    ⏳ NEXT: Step 2 — call _reviewer
    run_node(node_type='_reviewer', node_inputs={
      'artifact_id': 'pre_registration__t1', 'source_node_type': 'hypothesis',
      'producer_run_id': 'r_hyp',
    })

**框架已经能把答案连参数一起打印出来** —— 能被打印出来的东西不是判断，是手续。
代价：一个 session 32M tokens 里调度器烧掉 16.2M（50.5%），大头在这条走廊上
抄参数、撞墙、读错误、换个姿势再抄。

## 改之后

一次 `run_node(<producing>)` 内部把 review → decision 走完，返回决策 pause。
调度器只在真正要判断的地方被叫醒。
"""
from __future__ import annotations

import pytest

from shared.tools import run_node as rn


class _Rec:
    """记录运行时自己派了哪些子节点。"""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []


@pytest.fixture
def runtime(monkeypatch):
    rec = _Rec()

    async def fake_run_node(state, node_type, node_inputs=None, **kw):
        rec.calls.append((node_type, dict(node_inputs or {})))
        if node_type == "_reviewer":
            for e in state.hook_state.get("pending_post_node_flow") or []:
                if e.get("producing_run_id") == (node_inputs or {}).get("producer_run_id"):
                    e["review_state"] = "done"
                    e["review_critique_artifact_id"] = "review_critique__auto"
        return {"status": "success"}

    async def fake_present(state, **kw):
        rec.calls.append(("present_decision_package", dict(kw)))
        return {"status": "pause", "pause_event": {"question": "Post-node decision"}}

    monkeypatch.setattr(rn, "_run_node_tool", fake_run_node)
    import shared.tools.library.decision_package as dp
    monkeypatch.setattr(dp, "_present_decision_package", fake_present)
    return rec


class _State:
    def __init__(self, entry):
        self.hook_state = {"pending_post_node_flow": [entry] if entry else []}
        self.transcript: list[tuple] = []

    def append_transcript(self, kind, **kw):
        self.transcript.append((kind, kw))


def _entry(**over):
    e = {
        "producing_node": "hypothesis",
        "producing_run_id": "r_hyp",
        "artifact_ids": ["pre_registration__t1"],
        "review_state": "pending",
        "decision_state": "pending",
    }
    e.update(over)
    return e


@pytest.mark.asyncio
async def test_runtime_dispatches_the_reviewer_then_presents_the_decision(runtime):
    state = _State(_entry())

    out = await rn._run_post_producing_flow(state, "hypothesis", {"child_run_id": "r_hyp"})

    assert [c[0] for c in runtime.calls] == ["_reviewer", "present_decision_package"], (
        "运行时必须自己把两步走完 —— 调度器不该被要求抄参数"
    )
    # 参数由运行时从账本取，不靠模型传对
    assert runtime.calls[0][1]["producer_run_id"] == "r_hyp"
    assert runtime.calls[0][1]["artifact_id"] == "pre_registration__t1"
    # 返回的是决策 pause：调度器下一次醒来看到的是**人的答复**，不是"该调谁了"
    assert out["status"] == "pause"


@pytest.mark.asyncio
async def test_a_node_that_does_not_owe_the_flow_is_untouched(runtime):
    """服务节点（literature / data / postprocess）不走这条链。"""
    state = _State(_entry(producing_node="literature"))

    out = await rn._run_post_producing_flow(state, "literature", {"child_run_id": "r_hyp"})

    assert out is None and runtime.calls == []


@pytest.mark.asyncio
async def test_no_flow_entry_means_no_chain(runtime):
    """没交付就没登记 flow —— 不该凭空起 reviewer。"""
    state = _State(None)

    out = await rn._run_post_producing_flow(state, "hypothesis", {"child_run_id": "r_hyp"})

    assert out is None and runtime.calls == []


@pytest.mark.asyncio
async def test_no_artifacts_skips_review_but_still_presents(runtime):
    """没有可审产物 → 如实记 skipped，仍然呈递（别把人挡在流程外面）。"""
    state = _State(_entry(artifact_ids=[]))

    out = await rn._run_post_producing_flow(state, "hypothesis", {"child_run_id": "r_hyp"})

    assert [c[0] for c in runtime.calls] == ["present_decision_package"]
    assert state.transcript[0][0] == "post_node_review_skipped_no_artifacts"
    assert out["status"] == "pause"


@pytest.mark.asyncio
async def test_review_already_done_goes_straight_to_the_decision(runtime):
    """resume 回来时 review 已经做过 —— 不重复审。"""
    state = _State(_entry(review_state="done",
                          review_critique_artifact_id="review_critique__x"))

    await rn._run_post_producing_flow(state, "hypothesis", {"child_run_id": "r_hyp"})

    assert [c[0] for c in runtime.calls] == ["present_decision_package"]
    assert runtime.calls[0][1]["review_critique_artifact_id"] == "review_critique__x"


@pytest.mark.asyncio
async def test_the_chain_does_not_recurse_into_itself(runtime):
    """运行时派出的 `_reviewer` 跑完，不得再触发一次 flow —— 否则无限递归。

    这条链是在 `run_node` 内部串起来的，而它派出的子节点也走同一个 `run_node`。
    唯一挡住递归的是 `_owes_post_node_flow`：系统节点（`_` 前缀）永远 False。
    把它钉住 —— 哪天有人放宽那个判据，先在这里红。
    """
    state = _State(_entry(producing_node="_reviewer", producing_run_id="r_rev"))

    out = await rn._run_post_producing_flow(state, "_reviewer", {"child_run_id": "r_rev"})

    assert out is None and runtime.calls == []


@pytest.mark.asyncio
async def test_an_authorized_revision_is_not_re_reviewed(runtime):
    """人选了 REVISE 之后 flow 还开着 —— 这时不该被当成"新一轮待审"重跑一遍。"""
    state = _State(_entry(review_state="done",
                          review_critique_artifact_id="review_critique__x",
                          decision_state="action_authorized",
                          authorized_action="revise"))

    out = await rn._run_post_producing_flow(state, "hypothesis", {"child_run_id": "r_hyp"})

    assert out is None, "已授权动作在途，不该再呈递一次决策包"
    assert runtime.calls == []

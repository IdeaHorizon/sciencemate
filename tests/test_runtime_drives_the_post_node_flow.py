"""审查自动执行，结果先回调度器；决策框不能抢在调度器解释之前弹出。"""
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

    def read_artifact(self, artifact_id):
        return {"id": artifact_id, "content": "具体审查意见"}


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
async def test_runtime_returns_review_material_before_any_decision_pause(runtime):
    state = _State(_entry())

    out = await rn._run_post_producing_flow(state, "hypothesis", {"child_run_id": "r_hyp"})

    assert [c[0] for c in runtime.calls] == ["_reviewer"]
    # 参数由运行时从账本取，不靠模型传对
    assert runtime.calls[0][1]["producer_run_id"] == "r_hyp"
    assert runtime.calls[0][1]["artifact_id"] == "pre_registration__t1"
    assert "pause_event" not in out
    assert out["post_node_review"]["critique"]["content"] == "具体审查意见"
    assert state.hook_state["pending_post_node_flow"][0]["decision_state"] == "pending"


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
async def test_no_artifacts_reports_skipped_review_without_popping_a_card(runtime):
    state = _State(_entry(artifact_ids=[]))

    out = await rn._run_post_producing_flow(state, "hypothesis", {"child_run_id": "r_hyp"})

    assert runtime.calls == []
    assert state.transcript[0][0] == "post_node_review_skipped_no_artifacts"
    assert out["post_node_review"]["review_state"] == "skipped"
    assert out["post_node_review"]["critique"] is None


@pytest.mark.asyncio
async def test_review_already_done_returns_material_without_repeating_it(runtime):
    """resume 回来时 review 已经做过 —— 不重复审。"""
    state = _State(_entry(review_state="done",
                          review_critique_artifact_id="review_critique__x"))

    out = await rn._run_post_producing_flow(state, "hypothesis", {"child_run_id": "r_hyp"})

    assert runtime.calls == []
    assert out["post_node_review"]["review_critique_artifact_id"] == "review_critique__x"


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


@pytest.mark.asyncio
async def test_review_failure_is_returned_as_a_failure_not_a_research_finding(monkeypatch):
    async def failed_review(*args, **kwargs):
        return {"status": "error", "error": "review endpoint timed out"}

    monkeypatch.setattr(rn, "_run_node_tool", failed_review)
    state = _State(_entry())
    out = await rn._run_post_producing_flow(state, "hypothesis", {"child_run_id": "r_hyp"})
    assert "pause_event" not in out
    assert out["post_node_review"]["review_state"] == "failed_awaiting_human"
    assert out["post_node_review"]["critique"] is None
    assert out["post_node_review"]["review_failed_reason"] == "review endpoint timed out"


@pytest.mark.asyncio
async def test_a_reviewers_own_question_still_reaches_the_user(monkeypatch):
    paused = {"status": "pause", "pause_event": {"question": "Which supplied dataset should be reviewed?"}}

    async def paused_review(*args, **kwargs):
        return paused

    monkeypatch.setattr(rn, "_run_node_tool", paused_review)
    out = await rn._run_post_producing_flow(_State(_entry()), "hypothesis", {"child_run_id": "r_hyp"})
    assert out == paused

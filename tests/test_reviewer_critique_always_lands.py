"""reviewer 烧满轮次也必须留下审查结论 —— 而且残件不许放行下游。

被独立报过三次：#194（nidy 2026-07-28）、#301（nidy 2026-08-05）、
#395-Issue6（jicq 2026-08-11）。现象一致：`_reviewer` 打满 max_turns，
`review_critique` 一个都没产出，orchestrator 拿不到任何独立审查信号，
reviewer 被反复重派，一轮约一小时。

根因不是"没想到要落盘"，是**收尾闸够不到这条出口**：v2.1 的
`on_before_finish` 整个在 `agent_loop` 的 `if not response.tool_calls:` 里面，
只守"模型主动收手"。耗尽轮数直接跳出 for 循环，闸一次都不跑（hypothesis 的
hooks 里已经写下过同一条教训：E2E v16 烧满 40 轮，0 次拦截记录）。

本文件锁两件事：
  1. 两条出口（主动收手 / 轮次耗尽）都必须留下已经做出的判断；
  2. **兜底落的是证据，不是判决** —— 没 verdict 的残件在决策层必须落到
     「review 不可用」（retry_reviewer，动作集不含 PROCEED）。第 2 条比第 1 条
     更要紧：一份被截断的审查冒充通过的审查，比白跑一轮贵得多。
"""
from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

import pytest

import nodes._reviewer.hooks  # noqa: F401  注册 hook
import nodes._reviewer.tools  # noqa: F401  注册工具
from core.loop_hooks import HookContext, get_loop_hook
from core.state import State
from core.tool_registry import execute as execute_tool

HOOK = get_loop_hook("reviewer_critique_lands")


def _state() -> State:
    state = State.new(node_type="_reviewer", base_dir=Path(tempfile.mkdtemp()),
                      project_id="p1")
    state.save_artifact("pre_registration", "a1", "review subject")
    return state


def _call(state, **kw):
    return asyncio.run(execute_tool("compose_review_critique", state, **kw))


class _Harness:
    def __init__(self, max_turns=40):
        self.max_turns = max_turns


def _ctx(state, turn=40, max_turns=40):
    return HookContext(harness=_Harness(max_turns), state=state,
                       messages=[], turn=turn)


def _critiques(state):
    return [a for a in state.list_artifacts(own_only=True)
            if a["type"] == "review_critique"]


def _content(state, aid):
    return json.loads(state.read_artifact(aid)["content"])


def _partial_draft(state):
    """模型审到一半：收集了 concerns，但没 set_verdict、没 finalize。"""
    _call(state, action="add_concern", severity="critical",
          title="H2 没有可证伪判据",
          description="prereg 写了 H2 但没给阈值")
    _call(state, action="add_strength", text="H1 的对照组设计干净")


# ── 1. 两条出口都要留下东西 ────────────────────────────────────────────────

def test_turns_exhausted_still_lands_the_work():
    """事故本体：轮次耗尽，on_before_finish 根本没机会跑，也得落盘。"""
    st = _state()
    _partial_draft(st)
    assert not _critiques(st)

    HOOK.on_end(_ctx(st), None)          # loop 结束（max_turns 出口也走这里）

    got = _critiques(st)
    assert len(got) == 1, "轮次耗尽后审查内容全丢了 —— 正是 #194/#301/#395-6"
    body = _content(st, got[0]["id"])
    assert body["concerns"][0]["title"] == "H2 没有可证伪判据"
    assert body["strengths"]


def test_nothing_gathered_means_no_fabricated_artifact():
    """什么都没做就结束 → 不造产物。兜底是抢救，不是无中生有。"""
    st = _state()
    HOOK.on_end(_ctx(st), None)
    assert not _critiques(st)


def test_does_not_double_write_when_reviewer_finalized_properly():
    """正常走完 finalize 的 run，兜底必须闭嘴。"""
    st = _state()
    _call(st, action="set_verdict", verdict="approve", confidence=0.9,
          summary="ok")
    _call(st, action="set_recommended_action", recommended_action="proceed")
    _call(st, action="finalize", name="c1", artifact_under_review="pre_registration__a1",
          source_node_type="hypothesis")
    assert len(_critiques(st)) == 1

    HOOK.on_end(_ctx(st), None)
    assert len(_critiques(st)) == 1, "兜底重复落盘了"


# ── 2. 落证据，不落判决 ────────────────────────────────────────────────────

def test_never_invents_a_verdict():
    """红线：模型没下判断，框架绝不替它填一个。"""
    st = _state()
    _partial_draft(st)
    HOOK.on_end(_ctx(st), None)

    aid = _critiques(st)[0]["id"]
    body = _content(st, aid)
    assert body["verdict"] is None
    assert body["review_incomplete"]["verdict_is_reviewer_own"] is False
    md = st.read_artifact(aid)["metadata"]
    assert md["review_incomplete"] is True
    assert md["verdict"] is None


def test_keeps_the_verdict_the_reviewer_actually_made():
    """模型 set_verdict 过、只是没 finalize —— 那是它真下的判断，要保住。"""
    st = _state()
    _call(st, action="set_verdict", verdict="major_concerns", confidence=0.7,
          summary="两处硬伤")
    _partial_draft(st)
    HOOK.on_end(_ctx(st), None)

    body = _content(st, _critiques(st)[0]["id"])
    assert body["verdict"] == "major_concerns"
    assert body["review_incomplete"]["verdict_is_reviewer_own"] is True


# ── 3. 催 finalize：拦在还来得及的时候 ─────────────────────────────────────

def test_nudges_while_turns_remain():
    st = _state()
    _partial_draft(st)
    msgs = HOOK.on_turn_end(_ctx(st, turn=37, max_turns=40))
    assert msgs and "finalize" in msgs[0].content


def test_no_nudge_early_in_the_run():
    """轮次还宽裕时不打扰 —— 催早了是噪音。"""
    st = _state()
    _partial_draft(st)
    assert HOOK.on_turn_end(_ctx(st, turn=5, max_turns=40)) is None


def test_nudges_only_once():
    st = _state()
    _partial_draft(st)
    assert HOOK.on_turn_end(_ctx(st, turn=37, max_turns=40))
    assert HOOK.on_turn_end(_ctx(st, turn=38, max_turns=40)) is None


def test_finish_gate_blocks_a_voluntary_stop_without_critique():
    st = _state()
    _partial_draft(st)
    msgs = HOOK.on_before_finish(_ctx(st, turn=12))
    assert msgs and "没有落盘" in msgs[0].content


def test_finish_gate_silent_when_already_landed():
    st = _state()
    _call(st, action="set_verdict", verdict="approve", summary="ok")
    _call(st, action="set_recommended_action", recommended_action="proceed")
    _call(st, action="finalize", name="c1", artifact_under_review="pre_registration__a1",
          source_node_type="hypothesis")
    assert HOOK.on_before_finish(_ctx(st, turn=12)) is None


# ── 4. 决策层：残件绝不打开 PROCEED ────────────────────────────────────────

def _present(st, critique_id):
    """真的把残件喂给决策包 —— 不复刻判据，看它实际渲染出什么动作集。

    （第一版这里是本地重算 review_incomplete/verdict 的辅助函数：
    decision_package 改不改它都绿，等于自己给自己打分。变异检查抓出来的。）
    """
    from shared.tools.library.decision_package import _present_decision_package
    entry = {
        "producing_node": "hypothesis",
        "producing_run_id": "run-hyp-1",
        "artifact_ids": ["a1"],
        "review_state": "done",
        "review_critique_artifact_id": critique_id,
        "review_attempt_count": 0,
        "curator_state": "done",
        "decision_state": "pending",
    }
    st.hook_state["pending_post_node_flow"] = [entry]
    asyncio.run(_present_decision_package(
        st, source_node_type="hypothesis", producing_run_id="run-hyp-1",
        review_critique_artifact_id=critique_id))
    return entry


def test_partial_critique_never_opens_proceed():
    """红线，端到端：残件（无 verdict）渲染出的动作集里不许有 PROCEED。

    判据是 #155 的 (a) 分支 —— review 不可用 → retry_reviewer 打头。一旦这条
    松掉，残件会掉进 (b)「schema 有瑕疵」分支，选项集保持正常 5 项、PROCEED
    回来了，等于让一份被截断的审查放行下游。
    """
    st = _state()
    _partial_draft(st)
    HOOK.on_end(_ctx(st), None)
    aid = _critiques(st)[0]["id"]

    entry = _present(st, aid)

    assert "proceed" not in entry["decision_options"], \
        "被截断的审查打开了 PROCEED —— 比 reviewer 白跑一轮贵得多"
    assert entry["decision_options"][0] == "retry_reviewer"


def test_reviewer_own_verdict_is_still_consumed():
    """反向：模型真下过判断的残件照常消费，别把好料也拒了。"""
    st = _state()
    _call(st, action="set_verdict", verdict="approve", confidence=0.8, summary="x")
    _call(st, action="set_recommended_action", recommended_action="proceed")
    _partial_draft(st)
    HOOK.on_end(_ctx(st), None)
    aid = _critiques(st)[0]["id"]

    entry = _present(st, aid)

    assert "proceed" in entry["decision_options"], \
        "reviewer 自己下的 verdict 被误当成残件拒掉了"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))


# ── 落了盘 ≠ 送到了决策方（2026-08-23）───────────────────────────────────────

def test_the_rendered_package_reaches_the_platform_not_just_the_pause():
    """审查意见必须**机械送达**建 Decision 的那一层。

    现场（英国饮食 2026-08-23 02:11）：reviewer 判 major_concerns，唯一的
    critical 是一处引文方向反转 —— 论文写「斯摩莱特批评法国饮食」，而 KB 锚点
    与本文局限性一节都指向英国本土客栈；该锚点支撑 Q2 的核心论证，方向反了
    论证就塌。

    critique 落盘了、决策包也渲染了（verdict + 按 severity 排序的 concerns），
    但那份正文**只**进了 `pause_event`。平台侧建 Decision 行时读的是
    `decision_package_presented` 事件的 `prompt` 字段，取不到就落兜底文案 ——
    于是库里那一行只有「Decision required for writing」。调度器手里只有这一句，
    只能自己 read_file 去猜，派下去的修订指令里 critical 整条丢失。

    这条钉的是：渲染好的正文与结构化 concerns 必须跟着**事件**走。
    不是加门禁 —— 决定权仍在决策方，proceed 照旧可选。
    """
    import inspect

    from shared.tools.library import decision_package as dp

    src = inspect.getsource(dp._present_decision_package)
    emit = src[
        src.index('"decision_package_presented"'):
        src.index("_pause_payload = offer.to_pause_payload()")
    ]

    assert "prompt=package_text" in emit, (
        "渲染好的决策包全文必须进 decision_package_presented 事件 —— "
        "平台按它建 Decision，缺了就落兜底文案，审查意见到不了决策方"
    )
    assert "review_concerns" in emit and "severity" in emit, (
        "结构化 concerns（带 severity）必须跟着事件走，"
        "让下游不必解析正文就能知道有没有 critical"
    )
    assert "review_verdict" in emit, "verdict 是决策方分流的第一判据，必须在事件里"


def test_delivery_is_not_a_gate():
    """送达 ≠ 卡死。决定权仍然全在决策方。

    QC 层被删就是因为「一个自动判定能一票否决交付」这条路走不通
    （2026-08-22：五轮返工全花在一个判错的门上）。这次只补送达，
    不补门禁 —— 所以 PROCEED 必须仍然在动作集里。
    """
    import inspect

    from shared.tools.library import decision_package as dp

    src = inspect.getsource(dp._present_decision_package)
    emit = src[
        src.index('"decision_package_presented"'):
        src.index("_pause_payload = offer.to_pause_payload()")
    ]
    for banned in ("raise ", "return None  # blocked", "must_revise"):
        assert banned not in emit, f"送达路径上不该出现门禁语义：{banned!r}"

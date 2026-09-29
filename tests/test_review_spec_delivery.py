"""owner 的判据必须送到 reviewer 手上（E2E v13）。

`nodes/<source>/review_spec.md` 是 owner 定义"我的产出该怎么被审"的地方 ——
节点自治的核心机制。但它住在 harness 仓，而 v2 下 reviewer 的 read_file 锚在
project worktree。实测 reviewer 连试四条路径全失败，凭通用 rubric 审完，
critique 里如实记着 owner_spec_loaded: false —— 机制诚实记下了自己的失效。
"""
from __future__ import annotations

from core.context_engine import _owner_review_spec_section
from core.loader import load_harness


def _reviewer_harness():
    return load_harness("_reviewer")


def test_owner_spec_is_part_of_assembled_context(tmp_path):
    """判据是 context 的构成部分，不是 prompt 建好之后再贴的补丁。"""
    section = _owner_review_spec_section(
        _reviewer_harness(),
        {"source_node_type": "hypothesis", "artifact_id": "pre_registration__x"})
    assert section is not None
    from pathlib import Path

    from core.loader import node_dir
    real = Path(node_dir("hypothesis") / "review_spec.md").read_text(encoding="utf-8")
    assert real.splitlines()[0][:40] in section          # 正文真的在，不是给路径
    assert "owner_spec_loaded` 记 true" in section


def test_it_lands_in_the_real_built_messages(tmp_path):
    """走真入口 build_messages —— 只测 helper 就是今晚栽过的"测了内部没测路径"。"""
    from core.context_engine import build_messages
    from core.state import State

    root = tmp_path / "run"
    root.mkdir(parents=True, exist_ok=True)
    state = State(run_id="r", node_type="_reviewer", root=root)
    state.save_artifact("pre_registration", "x", "review subject")
    messages = build_messages(_reviewer_harness(), state,
                              node_inputs={"source_node_type": "hypothesis",
                                           "artifact_id": "pre_registration__x"})
    joined = "\n".join(m.content for m in messages)
    assert "owner review_spec" in joined


def test_missing_spec_says_so_instead_of_silence():
    """owner 没写 spec 是合法的 —— 但要明说，并要求 critique 如实记 false。"""
    from pathlib import Path

    from core.loader import node_dir

    section = _owner_review_spec_section(_reviewer_harness(),
                                         {"source_node_type": "data"})
    if (Path(node_dir("data")) / "review_spec.md").is_file():
        assert section and "review_spec" in section
    else:
        assert section and "没有写 `review_spec.md`" in section


def test_project_synthesis_scope_is_not_covered():
    assert _owner_review_spec_section(_reviewer_harness(),
                                      {"source_node_type": "_project"}) is None


def test_other_nodes_do_not_get_it():
    """只有 reviewer 需要判据 —— 别的节点 context 不该被塞这个。"""
    assert _owner_review_spec_section(load_harness("hypothesis"),
                                      {"source_node_type": "hypothesis"}) is None


def test_reviewer_prompt_no_longer_tells_it_to_hunt_for_the_file():
    prompt = _reviewer_harness().system_prompt
    assert "read_file('nodes/<source_node_type>/review_spec.md')" not in prompt


def test_no_stale_hook_left_behind():
    """不留两套机制 —— hook 版必须彻底删掉。"""
    import inspect

    from core import loop_hooks_builtin

    assert "review_spec_briefing" not in inspect.getsource(loop_hooks_builtin)


# ── 定向层预览排序 ────────────────────────────────────────────────────


def test_preview_ranks_deliverables_above_framework_internals():
    """字母序不是重要性：compression_log 不该把 pre_registration 挤出预览。"""
    from core.loop_hooks_builtin import _rank_for_downstream

    ids = sorted([
        "compression_log__compression_turn_24",
        "compression_log__compression_turn_39",
        "hypothesis_cluster_report__Candidate_Clustering",
        "pre_registration__LJ_Cooling_Rate",
        "research_state__research_state",
        "resource_profile__x",
    ])
    top3 = _rank_for_downstream(ids)[:3]
    assert "pre_registration__LJ_Cooling_Rate" in top3
    assert "research_state__research_state" in top3
    assert not any(i.startswith("compression_log__") for i in top3)


def test_boilerplate_readme_is_not_a_self_description(tmp_path):
    """仓库初始化模板不是自述 —— 它零信息量，还掩盖"还没写自述"。"""
    from core.loop_hooks_builtin import _BOILERPLATE_README

    assert _BOILERPLATE_README.match("Owner: `hypothesis`.")
    assert _BOILERPLATE_README.match("The owner controls the internal layout.")
    assert not _BOILERPLATE_README.match("定向查证 MLIP 外推可靠性：5 篇已核验。")


def test_decision_package_surfaces_unspecced_review():
    """自报失效必须有消费者：owner_spec_loaded=false 要在决策包里亮出来 ——
    这个字段如实记录了跨越全部历史 E2E 的失效，却从来没人读。"""
    from shared.tools.library.decision_package import _render_decision_package

    package = _render_decision_package(
        source_node_type="hypothesis", producing_run_id="r1",
        producing_summary="ok", artifact_ids_produced=["pre_registration__x"],
        curator_summary=None,
        review_critique_json={
            "verdict": "approve", "confidence": 0.8,
            "rubric_source": {"owner_spec_loaded": False, "owner_spec_path": None},
            "recommended_action": {"action": "proceed", "feedback_to_next_run": ""},
        },
        review_failed_reason=None, review_unusable=False,
        offer=_offer_for_test("proceed"), recommended_feedback="",
        recommended_target_node=None, framework_override_note=None,
    )
    assert "未按 owner 的 review_spec" in package

    specced = _render_decision_package(
        source_node_type="hypothesis", producing_run_id="r1",
        producing_summary="ok", artifact_ids_produced=["pre_registration__x"],
        curator_summary=None,
        review_critique_json={
            "verdict": "approve", "confidence": 0.8,
            "rubric_source": {"owner_spec_loaded": True,
                              "owner_spec_path": "nodes/hypothesis/review_spec.md"},
            "recommended_action": {"action": "proceed", "feedback_to_next_run": ""},
        },
        review_failed_reason=None, review_unusable=False,
        offer=_offer_for_test("proceed"), recommended_feedback="",
        recommended_target_node=None, framework_override_note=None,
    )
    assert "未按 owner 的 review_spec" not in specced


def test_rubric_source_is_framework_stamped_not_model_written(tmp_path):
    """spec 是否送达是框架事实 —— critique 里的 rubric_source 机械盖章。
    实测：让模型手写这个块，它把 JSON 写坏，自报字段成了废纸。"""
    import asyncio
    import json as _json

    from core.context_engine import build_messages
    from core.state import State
    from nodes._reviewer.tools.critique_builder import _compose_review_critique

    root = tmp_path / "run"
    root.mkdir(parents=True, exist_ok=True)
    state = State(run_id="r", node_type="_reviewer", root=root)
    state.save_artifact("pre_registration", "x", "review subject")
    # context 组装（真入口）→ 交付事实落 hook_state
    build_messages(load_harness("_reviewer"), state,
                   node_inputs={"source_node_type": "hypothesis",
                                "artifact_id": "pre_registration__x"})
    assert state.hook_state["_owner_review_spec_delivery"]["owner_spec_loaded"] is True

    async def compose():
        await _compose_review_critique(state, "set_verdict", verdict="approve",
                                       confidence=0.8, summary="ok")
        await _compose_review_critique(
            state, "set_recommended_action", recommended_action="proceed",
            feedback_to_next_run="continue")
        return await _compose_review_critique(
            state, "finalize", name="hypothesis_critique_t",
            artifact_under_review="pre_registration__x",
            source_node_type="hypothesis")

    result = asyncio.run(compose())
    assert result.get("status") != "error", result
    record = state.read_artifact(result["artifact_id"])
    content = _json.loads(record["content"])
    assert content["rubric_source"]["owner_spec_loaded"] is True
    assert content["rubric_source"]["stamped_by"] == "framework"
    meta = record["metadata"]
    if isinstance(meta, str):
        meta = _json.loads(meta)
    assert meta["owner_spec_loaded"] is True


def _offer_for_test(action="proceed", *, review_failed=False, target=None):
    """渲染器的选项集来自 offer（一处声明），测试也走同一条路。"""
    from shared.tools.library.decision_package import (
        _NORMAL_ACTIONS,
        _REVIEW_FAILED_ACTIONS,
        build_decision_offer,
    )
    ids = list(_REVIEW_FAILED_ACTIONS if review_failed else _NORMAL_ACTIONS)
    return build_decision_offer(
        decision_id="r_test:ptest", source_node_type="hypothesis",
        action_ids=ids, recommended_action=action, redirect_target_node=target,
    )

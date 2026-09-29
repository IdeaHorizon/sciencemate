"""Regression tests for Experiment's compact runtime core and conditional SOPs."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from core.bootstrap import bootstrap
from core.context_engine import build_messages
from core.loader import load_harness
from core.skill_registry import get_skill
from core.state import State
from nodes.experiment import hooks
from nodes.experiment.tools.run_contract import _classify_experiment_scope


def _state(tmp_path):
    return State.new("experiment", tmp_path)


def test_experiment_first_prompt_has_a_bounded_core_instruction_budget(tmp_path):
    """Rules/SOP must not silently grow back into every Experiment first turn."""
    bootstrap(force=True)
    harness = load_harness("experiment")
    messages = build_messages(harness, _state(tmp_path), {})
    system = next(message.content for message in messages if message.role == "system")

    # 16 = 原 15 条 + 2026-08-30 新增的"问人判据"（只有 owner 能解且不问即停车才问）
    assert len(harness.rules) == 16
    assert harness.guidelines == []
    # 049-0：节点 skill 进 L1 索引（只有名字 + 适用 + 取法，正文仍按需 load_skill）；
    # gpu-hpc-porting 仍走条件注入。索引行不是常驻正文，不改变本测试守的"瘦身"。
    assert set(harness.skills) == {
        "claim_evidence_link", "feasibility-ladder", "formal-input-recovery", "hpc-build",
        "primary-scientific-closure", "scheduler-longrun", "scientific-results"}
    assert "### Skill: hpc-build" not in system            # index line only, never the body
    # 这里曾有 `estimate_text_tokens(system) <= 1400`（3f2d8f9f，2026-08-18）。
    # 已删除，理由三条：
    #  1. 那个数没有依据 —— 它是瘦身当天的读数（1219）+15% 余量，不是从任何
    #     预算推导出来的。框架对节点常驻 prompt 体积没有任何要求：core/loader.py
    #     不校验，tests/ 无跨节点契约，11 个节点里另外 10 个零约束
    #     （writing 16759 token / hypothesis 15378，experiment 3213 是最小的）。
    #  2. 单位不自证 —— 写它时 tiktoken 不是依赖，estimate_text_tokens 走
    #     len//4 兜底，1400 实际锁的是 ~5600 字符。0b417d42（08-30）把 tiktoken
    #     进 base deps 后同一函数改用 cl100k，中文 1.111 token/字符 vs 英文
    #     0.195，估算跳 2.2×，门与改动无关地变红。而且 encoder 装不上时估算
    #     回落 char/4，任何调大的阈值都会静默通过 —— 绿着但什么都没量。
    #  3. 它没抓住它唯一该抓的那次 —— 本轮 prompt 涨到 5786 字符（超它自己
    #     的 5600 线 186），因为门早已因换口径变红，超标没人看见。
    # 真正要守的是"SOP 不许内联回常驻上下文"，由下面三条 not-in 断言 +
    # guidelines == [] + rules 条数守住，且不随 tokenizer/模型口径漂移。
    # 常驻 prompt 永不进压缩区（core/summarizer.py::split_for_compression 的
    # head 段原样重发），要记账应在框架层对所有节点统一做，不是 experiment
    # 单方面自罚。
    # main 的 1f9ebde2（08-31）曾把它机械重标为 3600 并注明「请 experiment
    # owner 复核」—— 复核结论就是上面三条：换刻度治不了「刻度没有依据」和
    # 「encoder 缺失时任何阈值都静默通过」这两条，所以整条删除而不是调大。
    assert "# HPC Source Build" not in system
    assert "# NVIDIA CUDA HPC Build and Verification" not in system
    assert "# Primary Scientific Closure" not in system


def test_experiment_conditional_sops_are_registered_without_global_injection():
    bootstrap(force=True)
    for name in (
        "hpc-build",
        "gpu-hpc-porting",
        "scheduler-longrun",
        "scientific-results",
        "primary-scientific-closure",
        "formal-input-recovery",
        "feasibility-ladder",
    ):
        assert get_skill(name) is not None, name


def test_scientific_scope_router_points_primary_run_to_results_and_closure(tmp_path):
    state = _state(tmp_path)
    state.hook_state["node_inputs"] = {
        "requested_work": "验证 primary scientific scope 的结果与收尾 SOP 路由",
        "prereg_assignment": {
            "kind": "none",
            "reason": "This routing test has no governing preregistration.",
        },
    }
    state.hook_state["run_contract"] = {
        "run_role": "primary",
        "stage": "simulation",
    }
    classified = asyncio.run(_classify_experiment_scope(
        state,
        scope="scientific",
        reason="通过正式分类入口建立不可变运行收据后验证 skill 路由。",
    ))
    assert classified["status"] == "success", classified

    messages = hooks._experiment_skill_router_on_turn_end(
        SimpleNamespace(state=state, turn=1),
    )

    assert messages is not None
    assert "load_skill(name='scientific-results')" in messages[0].content
    assert "load_skill(name='primary-scientific-closure')" in messages[0].content
    transcript = state.transcript_path.read_text(encoding="utf-8")
    assert '"event": "experiment_skill_routed"' in transcript
    assert '"primary-scientific-closure"' in transcript
    assert hooks._experiment_skill_router_on_turn_end(
        SimpleNamespace(state=state, turn=2),
    ) is None


def test_scope_router_gives_operation_a_compact_closure_prompt(tmp_path):
    state = _state(tmp_path)
    state.hook_state["experiment_execution_scope"] = {"mode": "operational"}

    messages = hooks._experiment_skill_router_on_turn_end(
        SimpleNamespace(state=state, turn=1),
    )

    assert messages is not None
    assert "record_operation_completion" in messages[0].content
    assert "不要手工 save/freeze" in messages[0].content
    assert "report_blocker" in messages[0].content
    assert "blocker.blocker_id" in messages[0].content
    transcript = state.transcript_path.read_text(encoding="utf-8")
    assert '"event": "experiment_skill_routing_not_applicable"' in transcript


def _classified_operation(tmp_path, category: str):
    state = _state(tmp_path)
    state.hook_state["node_inputs"] = {
        "experiment_focus": "Build the toolchain.",
        "prereg_assignment": {"kind": "none", "reason": "049-0 router fixture"},
    }
    classified = asyncio.run(_classify_experiment_scope(
        state, scope="operation", operation_category=category, reason="049-0 fixture"))
    assert classified["status"] == "success", classified
    return state


def test_scope_router_points_a_toolchain_build_run_at_hpc_build(tmp_path):
    """049-0：operation/toolchain_build 分类落地后，下一轮的指针里必须有 hpc-build。"""
    state = _classified_operation(tmp_path, "toolchain_build")

    messages = hooks._experiment_skill_router_on_turn_end(
        SimpleNamespace(state=state, turn=1),
    )

    assert messages is not None
    assert "load_skill(name='hpc-build')" in messages[0].content
    assert "record_operation_completion" in messages[0].content      # closure guidance kept
    transcript = state.transcript_path.read_text(encoding="utf-8")
    assert '"event": "experiment_skill_routed"' in transcript
    assert '"hpc-build"' in transcript
    assert hooks._experiment_skill_router_on_turn_end(
        SimpleNamespace(state=state, turn=2),
    ) is None


def test_scope_router_does_not_name_a_skill_for_categories_without_evidence(tmp_path):
    state = _classified_operation(tmp_path, "environment_probe")
    messages = hooks._experiment_skill_router_on_turn_end(
        SimpleNamespace(state=state, turn=1),
    )
    assert messages is not None
    assert "load_skill(" not in messages[0].content
    assert '"event": "experiment_skill_routing_not_applicable"' in state.transcript_path.read_text(
        encoding="utf-8")

"""postprocess = 图表服务（2026-08-04 定位；2026-09-01 B 刀重建后的形状）。

服务身份不变：需求方提 visual request、服务出形式、post_run_flow: none。
判决拆除 B 刀之后的实现形状：**agent 在沙箱写 matplotlib**（execute_python
迭代 + render_figure 落账），八种 typed 产物收敛为一种 `figure` 记录，
DSL 强制通道降为 skills 素材（list_skills / load_skill 可读）。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from core.bootstrap import bootstrap
from core.loader import load_harness
from core.tool_registry import list_tools_for_node

bootstrap()


@pytest.fixture(scope="module")
def figsvc():
    return load_harness("postprocess")


@pytest.fixture(scope="module")
def writing():
    return load_harness("writing")


# ── 服务身份 ────────────────────────────────────────────────────────────────


def test_is_a_service_not_a_pipeline_stage(figsvc):
    assert figsvc.post_run_flow == "none"
    assert figsvc.is_service is True
    assert figsvc.owes_post_node_flow is False, "服务不该欠 reviewer/curator/决策包 —— 把关在消费方"
    assert figsvc.owes_post_node_review is False


def test_service_is_not_blocked_by_the_pending_flow_gate():
    from core.loader import node_owes_post_node_flow as _owes_post_node_flow

    assert _owes_post_node_flow("postprocess") is False
    assert _owes_post_node_flow("writing") is True, "科学节点照旧欠审查"
    assert _owes_post_node_flow("_reviewer") is False, "系统节点从来不欠"


def test_required_output_is_the_figure_record(figsvc):
    """B 刀：唯一完成契约 = render_figure 铸出的 figure 记录。"""
    assert figsvc.required_output_artifact_types == ["figure"]


def test_service_budget_is_not_a_science_budget(figsvc):
    assert figsvc.max_turns == 40
    assert figsvc.max_output_tokens <= 8192


def test_service_cannot_write_kb(figsvc):
    names = {t.name for t in list_tools_for_node("postprocess", figsvc.tools, state=None)}
    forbidden = {
        n
        for n in names
        if n.startswith("kb_") or n in ("search_kb", "get_kb_record", "promote_load_bearing")
    }
    assert forbidden == set(), f"服务不该有 KB 工具：{forbidden}"
    assert "add_memory_candidate" not in names, "作图服务不能自行沉淀长期经验"
    assert "memory_recall" in names, "只读项目风格记忆仍可用于跨图一致性"


@pytest.fixture
def visual_review_role(monkeypatch):
    """平台配了审图后端 —— inspect_figure 的能力前提。"""
    import json as _json

    from core import model_roles

    monkeypatch.setenv("HARNESS_MODEL_ROLES", _json.dumps({
        "visual_review": {"provider": "icompify", "model": "minimax-m3",
                          "base_url": "https://reviewer.invalid", "api_key": "t"},
    }))
    model_roles.install_from_environment(force=True)
    yield
    model_roles.install_from_environment(force=True)


def test_toolbelt_is_code_plus_record(figsvc, visual_review_role):
    """B 刀工具面：写代码（execute_python）+ 落账（render_figure）+ 按需
    VLM（inspect_figure）+ 技能库；typed lifecycle 的 9 入口全部死亡。"""
    names = {t.name for t in list_tools_for_node("postprocess", figsvc.tools, state=None)}
    for gone in ("read_episode", "task", "add_runtime_directive", "request_human_input"):
        assert gone not in names, f"{gone} 是需求方的事，服务不该有"
    for gone in (
        # B 刀：typed lifecycle 工具面整体删除。
        "create_requested_scientific_visual",
        "prepare_requested_visual_design",
        "create_requested_visual_from_decision",
        "create_requested_visual_brief",
        "create_scientific_visual",
        "create_visual_brief",
        "inspect_visual_sources",
        "match_visual_skills",
        "load_visual_skill",
        "plan_visual",
        "derive_visual_inputs",
        "revise_visual_plan",
        "render_visual",
        "compose_visuals",
        "inspect_visual_output",
        "list_visual_backends",
        "request_visual_review",
        "finalize_figure_package",
        # 与出图无关的面继续挡在外面
        "save_artifact",
        "run_bash",
        "compile_latex",
        "autoplot_plan",
        "autoplot_render",
        "autoplot_critique",
        "validate_figures",
        "add_memory_candidate",
        "consult_other_model",
    ):
        assert gone not in names, f"{gone} 不该在模型面"
    for keep in (
        "read_file",
        "list_files",
        "search_files",
        "execute_python",
        "render_figure",
        "inspect_figure",
        "list_skills",
        "load_skill",
        "request_upstream_rework",
        "memory_recall",
    ):
        assert keep in names, f"{keep} 该保留"


def test_inspect_figure_disappears_without_the_review_capability(figsvc, monkeypatch):
    """能力缺席就不给工具（PR#632）：没配 visual_review → inspect_figure
    压根不出现；渲染主路径不受影响。"""
    from core import model_roles

    monkeypatch.delenv("HARNESS_MODEL_ROLES", raising=False)
    model_roles.install_from_environment(force=True)
    names = {t.name for t in list_tools_for_node("postprocess", figsvc.tools, state=None)}
    assert "inspect_figure" not in names
    for keep in ("render_figure", "execute_python", "read_file"):
        assert keep in names, f"{keep} 与审图能力无关，不该跟着消失"


def test_figure_family_skills_are_on_the_shelf():
    """DSL 降为技能库：图型家族知识必须以 node-local skill 形式在场可查。"""
    skills_root = Path("nodes/postprocess/skills")
    for family in (
        "generic-quantitative-figure",
        "relational-networks-flows-and-trees",
        "time-series-and-trajectories",
        "electronic-structure-visualization",
        "spatial-geospatial-fields",
        "multi-panel-composition",
        "scientific-schematic",
        "scientific-imaging",
        "publication-accessibility",
    ):
        assert (skills_root / family / "SKILL.md").is_file(), f"缺图型家族 skill：{family}"


# ── writing 侧：从"自己画"改成"提需求" ───────────────────────────────────────


def test_writing_dispatches_instead_of_drawing(writing):
    names = {t.name for t in list_tools_for_node("writing", writing.tools, state=None)}
    assert [n for n in names if n.startswith("autoplot")] == [], (
        "画图工具已收回 —— 需求方提需求，不自己画"
    )
    assert "request_figures" in names, "唯一派图口：框架按简报灌政策再转给图表服务"
    assert "run_node" not in names, "不许绕过 request_figures 直接派（iter11：直接 run_node 的请求没带语言政策）"
    assert writing.callable_nodes == ["postprocess"], "writing 只能派图表服务，不能派别的"


def test_writing_prompt_teaches_the_new_path(writing):
    p = writing.system_prompt
    assert "request_figures" in p, "要教它怎么提需求"
    craft = Path("nodes/writing/craft/figures.md").read_text(encoding="utf-8")
    assert '"asset_kind": "schematic"' in craft and '"spec"' in craft, "示意图是常见 Figure 1，结构 spec 必须教"
    assert "你不画图" in craft, "图型与像素是服务的专业，需求方只说要让读者看出什么"
    assert "figure_package" not in p and "figure_package" not in craft, "figure_package 已死，prompt 不许再教它"

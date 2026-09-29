"""completion gate mid-run nudge：缺 required / validate 未过时催促。"""
from __future__ import annotations

from core.artifact_capabilities import save_typed_artifact
from core.harness import NodeHarness
from core.loop_hooks import HookContext
from core.state import State
import nodes.hypothesis.hooks as hyp_hooks


def _make_ctx(tmp_path, *, turn: int = 12, max_turns: int = 40) -> HookContext:
    state = State.new(node_type="hypothesis", base_dir=tmp_path)
    harness = NodeHarness(
        node_type="hypothesis",
        max_turns=max_turns,
        required_outputs=[
            "research_state",
            "pre_registration",
            "research_plan",
            "hypothesis_innovation_report",
            "hypothesis_research_overview",
        ],
    )
    return HookContext(
        state=state,
        harness=harness,
        turn=turn,
        messages=[],
        tool_call_records=[],
    )


def _save_required(state: State, *, include_validation: dict | None = None) -> None:
    save_typed_artifact(
        state,
        artifact_type="research_state",
        name="v1",
        content="# rs",
        metadata={"version": 1, "verdict": "continue", "hypotheses": []},
    )
    for t, name in (
        ("pre_registration", "Prereg"),
        ("research_plan", "Plan"),
        ("hypothesis_innovation_report", "HIF"),
        ("hypothesis_research_overview", "Overview"),
    ):
        state.save_artifact(t, name, f"# {name}", metadata={})
    if include_validation is not None:
        state.save_artifact(
            "hypothesis_output_validation",
            "Output_Validation",
            "# validation",
            metadata=include_validation,
        )


def test_nudge_when_audits_exist_but_required_missing(tmp_path) -> None:
    ctx = _make_ctx(tmp_path, turn=12)
    ctx.state.save_artifact(
        "hypothesis_conclusion_audit", "Conclusion_Audit", "# audit", metadata={},
    )
    ctx.state.save_artifact(
        "hypothesis_innovation_report", "HIF", "# hif", metadata={},
    )
    ctx.state.save_artifact(
        "pre_registration", "Prereg", "# draft", metadata={},
    )

    note = hyp_hooks._completion_gate_debt_nudge(ctx)
    assert note is not None
    assert "完成闸未闭合" in note
    assert "research_plan" in note
    assert "hypothesis_research_overview" in note
    assert "额外 audit" in note or "cluster" in note

    msgs = hyp_hooks._missing_outputs_nudge_on_turn_start(ctx)
    assert msgs
    assert "validate_hypothesis_outputs" in msgs[0].content


def test_nudge_when_required_ok_but_validate_failed(tmp_path) -> None:
    ctx = _make_ctx(tmp_path, turn=20)
    _save_required(
        ctx.state,
        include_validation={
            "passed": False,
            "failed_checks": ["research_plan_complete"],
        },
    )

    note = hyp_hooks._completion_gate_debt_nudge(ctx)
    assert note is not None
    assert "research_plan_complete" in note
    assert "未通过" in note


def test_no_nudge_when_gate_closed(tmp_path) -> None:
    ctx = _make_ctx(tmp_path, turn=25)
    _save_required(
        ctx.state,
        include_validation={"passed": True, "failed_checks": []},
    )

    assert hyp_hooks._completion_gate_debt_nudge(ctx) is None
    assert hyp_hooks._missing_outputs_nudge_on_turn_start(ctx) is None


def test_no_nudge_too_early_without_progress(tmp_path) -> None:
    ctx = _make_ctx(tmp_path, turn=2)
    assert hyp_hooks._completion_gate_debt_nudge(ctx) is None


def test_late_run_nudge_even_without_progress(tmp_path) -> None:
    ctx = _make_ctx(tmp_path, turn=36, max_turns=40)
    note = hyp_hooks._completion_gate_debt_nudge(ctx)
    assert note is not None
    assert "只剩约" in note
    assert "incomplete" in note

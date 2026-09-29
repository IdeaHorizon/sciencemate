"""hypothesis 节点 v0.3 新工具与 HIF 扩展测试。"""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from core.state import State
from nodes.hypothesis.tools.hif_core import HIFDimensions, compute_hif
from nodes.hypothesis.tools.hypothesis_cluster import (
    cluster_by_overlap,
    overlap_ratio,
    rank_candidates,
)
from nodes.hypothesis.tools.hypothesis_evolve import _build_evolution_plan
from nodes.hypothesis.tools.research_goal import parse_research_goal


def test_overlap_ratio_identical() -> None:
    a = "NHC chain converges faster than NH on dense LJ"
    assert overlap_ratio(a, a) == 1.0


def test_cluster_merges_duplicates() -> None:
    hyps = [
        {"label": "H1", "claim_text": "Pore geometry determines Na diffusion barrier in tC40"},
        {"label": "H2", "claim_text": "Pore geometry determines sodium diffusion barrier in tC40 anode"},
        {"label": "H3", "claim_text": "Phonons at 350K gap nodal surface reducing conductivity"},
    ]
    result = cluster_by_overlap(hyps, threshold=0.55)
    assert result["n_clusters"] == 2
    assert len(result["duplicates_to_merge"]) >= 1


def test_rank_penalizes_non_representative() -> None:
    hyps = [
        {"label": "H1", "claim_text": "alpha", "hif": 70, "G": 4},
        {"label": "H2", "claim_text": "alpha duplicate", "hif": 65, "G": 4},
    ]
    cluster = cluster_by_overlap(hyps, threshold=0.55)
    ranked = rank_candidates(hyps, cluster_result=cluster)
    assert ranked[0]["label"] == "H1"


def test_hif_plausibility_reject() -> None:
    result = compute_hif(HIFDimensions(G=5, D=4, M=4, P=4, R=0, Q=1, I=4))
    assert result.plausibility_reject is True
    assert result.hif <= 24
    assert result.tier == "minimal"


def test_hif_extended_with_impact() -> None:
    legacy = compute_hif(HIFDimensions(G=4, D=4, M=4, P=4, R=0))
    extended = compute_hif(HIFDimensions(G=4, D=4, M=4, P=4, R=0, I=5))
    assert extended.hif >= legacy.hif


def test_parse_research_goal_structured() -> None:
    parsed = parse_research_goal({
        "research_goal": {
            "title": "Test goal",
            "goals": ["Q1", "Q2"],
            "constraints": ["no wet lab"],
        },
        "hypothesis_iteration": {"min_candidates": 5},
    })
    assert parsed["has_structured_goal"] is True
    assert parsed["goal"]["title"] == "Test goal"
    assert parsed["iteration"]["min_candidates"] == 5


def test_parse_research_goal_legacy_fallback() -> None:
    parsed = parse_research_goal({
        "research_question": "Does X beat Y?",
        "scope_hint": "DFT only",
    })
    assert parsed["source"] == "legacy"
    assert "Does X beat Y?" in parsed["goal"]["goals"][0]
    assert "DFT only" in parsed["goal"]["constraints"][0]


def test_parse_research_goal_merges_dialogue_user_prompt() -> None:
    parsed = parse_research_goal(
        {"research_question": "legacy q"},
        dialogue={
            "user_prompt": "为 CSV 性能数据做 paired bootstrap，禁止提及 phonon",
            "user_prompt_source": "conversation",
            "inferred_constraints": ["禁止提及 phonon", "只使用 paired bootstrap"],
            "grounding_checklist": ["align"],
        },
    )
    assert parsed["user_prompt"].startswith("为 CSV")
    assert parsed["goal"]["goals"][0].startswith("为 CSV")
    assert any("phonon" in c for c in parsed["goal"]["constraints"])
    assert "align" in parsed["checklist"]


def test_get_research_goal_loads_parent_conversation() -> None:
    import json
    from nodes.hypothesis.tools.research_goal import _get_research_goal

    base = Path(tempfile.mkdtemp())
    parent_id = "orchestrator__proj-dlg"
    parent_root = base / parent_id
    parent_root.mkdir(parents=True)
    (parent_root / "conversation.json").write_text(
        json.dumps({
            "messages": [
                {"role": "user", "content": "我们做系统性能对比"},
                {"role": "assistant", "content": "好的，先确认范围"},
                {
                    "role": "user",
                    "content": (
                        "启动 hypothesis，为 CSV 系统性能数据设计 research plan；"
                        "只使用 data_import 和 paired_bootstrap；禁止使用或提及 phonon、掺杂"
                    ),
                },
            ],
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    state = State.new(node_type="hypothesis", base_dir=base, project_id="proj-dlg")
    state.parent_run_id = parent_id
    state.hook_state["node_inputs"] = {}
    result = asyncio.run(_get_research_goal(state))
    assert result["status"] == "success"
    assert result["user_prompt"] and "CSV" in result["user_prompt"]
    assert result["dialogue_context"]["n_conversation_messages"] == 3
    cons = " ".join(result["goal"].get("constraints") or [])
    assert "phonon" in cons or "掺杂" in cons


def test_evolve_hypothesis_plan() -> None:
    plan = _build_evolution_plan(
        {"label": "H1", "claim_text": "Topology drives volume change"},
        "regime_shift",
        audit_feedback="overlap with finding",
    )
    assert plan["mode"] == "regime_shift"
    assert len(plan["evolution_actions"]) >= 2


def test_get_research_goal_tool() -> None:
    from nodes.hypothesis.tools.research_goal import _get_research_goal

    state = State.new(node_type="hypothesis", base_dir=Path(tempfile.mkdtemp()))
    state.hook_state["node_inputs"] = {
        "research_goal": {"title": "T", "goals": ["g1"]},
    }
    result = asyncio.run(_get_research_goal(state))
    assert result["status"] == "success"
    assert result["goal"]["title"] == "T"

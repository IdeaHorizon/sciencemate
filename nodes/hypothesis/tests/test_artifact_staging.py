"""audit auto-save and artifact recovery tests."""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from core import paths
from core.state import State
from nodes.hypothesis.artifact_recovery import recover_missing_artifacts
from nodes.hypothesis.tools.workflow_audit import _audit_computational_workflow
from nodes.hypothesis.tests.test_output_validator import _minimal_research_plan


def _make_state() -> State:
    return State.new(node_type="hypothesis", base_dir=Path(tempfile.mkdtemp()))


def test_audit_auto_saves_research_plan_on_pass() -> None:
    state = _make_state()
    plan = _minimal_research_plan()
    result = asyncio.run(_audit_computational_workflow(
        state, content=plan, plan_name="tC40_Research_Plan", auto_save=True,
    ))
    assert result["passed"] is True
    assert result["auto_saved"] is True
    assert result.get("artifact_id") == "research_plan__tC40_Research_Plan"
    assert state.list_artifacts("research_plan")
    # issue #166.4：draft 落点从 <run>/drafts/ 迁到 <run>/outputs/hypothesis/drafts/，
    # 目录名的真相源在 core.paths，测试不再硬编码字面量。
    assert (paths.hypothesis_drafts_dir(state) / "research_plan.md").is_file()


def test_recovery_from_audit_transcript() -> None:
    state = _make_state()
    plan = _minimal_research_plan()
    state.append_transcript(
        "tool_call",
        name="audit_computational_workflow",
        args={"content": plan, "plan_name": "Recovered_Plan", "auto_save": True},
    )
    state.append_transcript(
        "tool_result",
        name="audit_computational_workflow",
        result_preview={"passed": True, "auto_saved": False},
    )
    recovered = recover_missing_artifacts(state)
    assert len(recovered) == 1
    assert state.list_artifacts("research_plan")
    assert state.read_artifact("research_plan__Recovered_Plan")

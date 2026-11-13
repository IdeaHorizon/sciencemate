"""Exercise the public Experiment intake, handoff and bounded service routes."""
import asyncio
import subprocess

import pytest

from core.bootstrap import bootstrap
from core.loader import load_harness
from core.project_workspace import bind_project_workspace
from core.state import State
from core.tool_registry import execute, get_tool, list_tools_for_node
from nodes.experiment import hooks
from nodes.experiment.tools import run_bootstrap


@pytest.fixture
def state(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("HARNESS_FRAMEWORK_HOME", str(home))
    monkeypatch.setenv("HARNESS_FRAMEWORK_ORG_HOME", str(home / "org"))
    monkeypatch.setattr(run_bootstrap, "_scheduler_facts", lambda state: "fixture: local")
    bootstrap()
    parent = State.new("experiment", base_dir=tmp_path / "runs", project_id="routing")
    parent.hook_state["_callable_nodes"] = load_harness("experiment").callable_nodes
    return parent


def call(state, tool, **args):
    return asyncio.run(execute(tool, state, **args))


def test_revise_distinguishes_original_work_from_current_instruction(state):
    state.hook_state["node_inputs"] = {
        "experiment_spec": "R8: retry publisher full texts for twelve papers.",
        "mode": "revise", "reviewer_feedback": "Correct two reference labels.",
        "human_directive": "Metadata only. Reuse the frozen table.",
    }
    established = asyncio.run(run_bootstrap.establish(state))
    page = run_bootstrap.one_page(state, established)
    original, current = page.split("## 本轮返修范围")
    assert "R8:" in original and "R8:" not in current
    assert "Correct two reference labels." in current and "Metadata only." in current


@pytest.mark.parametrize(("product", "owner"), [
    ("survey_report", "literature"), ("dataset", "data"),
    ("manuscript", "writing"), ("review_critique", "_reviewer"),
])
def test_foreign_product_and_blocked_handoff_use_registered_owner(state, product, owner):
    summary = f"The entire assignment is to produce {product}."
    state.hook_state["node_inputs"] = {"experiment_spec": summary,
                                      "required_outputs": [{"artifact_type": product}]}
    established = asyncio.run(run_bootstrap.establish(state))
    assert f"`{product}` 由 {owner}" in run_bootstrap.one_page(state, established)
    args = dict(summary=summary, category="missing_capability", suggested_owner=owner,
                requested_action=f"Send the original assignment and references to {owner}.")
    receipt = call(state, "report_blocker", **args)["blocker"]
    assert hooks._foreign_blocker_owner({"name": "report_blocker", "args": args}) == ""
    closure = call(state, "finish", outcome="blocked", summary=summary,
                   blocker={"blocker_id": receipt["blocker_id"]}, next_step=args["requested_action"])
    assert closure["outcome"] == "blocked"
    assert state.hook_state["blockers"] == [receipt]
    assert receipt["suggested_owner"] == owner and receipt["requested_action"] == args["requested_action"]
    assert state.hook_state["node_task_outcome"]["outcome"] == "blocked"
    assert all(state.artifact_head(a).frozen for a in closure["artifact_ids"].values())
    assert not any(a["type"] == product for a in state.list_artifacts())


def test_mixed_requirements_keep_local_work_and_docs_tools(state):
    state.hook_state["node_inputs"] = {
        "experiment_spec": "Use the supplied paper to run the solver and validate the residual.",
        "required_outputs": [{"path": "residual.csv"}, {"artifact_type": "dataset"}],
    }
    page = run_bootstrap.one_page(state, asyncio.run(run_bootstrap.establish(state)))
    assert "residual.csv" in page and "`dataset` 由 data" in page
    assert not state.hook_state.get("blockers")
    tools = {t.name for t in list_tools_for_node("experiment", load_harness("experiment").tools)}
    assert {"safe_run_bash", "web_fetch", "dispatch_data_request", "request_literature_evidence"} <= tools
    assert "run_node" not in tools


def test_literature_service_delivers_original_child_artifact(state, tmp_path, monkeypatch):
    project = tmp_path / "project"
    subprocess.run(["git", "init", "-q", str(project)], check=True)
    subprocess.run(["git", "-C", str(project), "-c", "user.name=test", "-c", "user.email=test@local",
                    "commit", "-q", "--allow-empty", "-m", "fixture"], check=True)
    bind_project_workspace(state, project)
    seen = []

    async def child(**kwargs):
        seen.append(kwargs)
        out = State.new("literature", base_dir=tmp_path / "children")
        bind_project_workspace(out, project)
        out.save_artifact("literature_evidence_package", "method", "Threshold verified; sample size unknown.")
        return {"run_id": out.run_id, "node_type": "literature", "status": "completed",
                "state_dir": str(out.root), "artifacts": out.list_artifacts(), "turns": 1,
                "tokens_used": 1, "missing_required_outputs": [], "final_text_preview": "Method evidence."}

    monkeypatch.setattr("core.executor.execute_node", child)
    result = call(state, "request_literature_evidence", research_question="Which threshold for this run?")
    assert result["status"] == "success", result
    assert len(seen) == 1 and seen[0]["node_type"] == "literature"
    assert seen[0]["node_inputs"]["mode"] == "method_lookup"
    packages = [a for a in state.list_artifacts() if a["type"] == "literature_evidence_package"]
    assert len(packages) == 1
    assert "sample size unknown" in str(call(state, "read_artifact", artifact_id=packages[0]["id"]))


@pytest.mark.parametrize("override", [{"mode": "landscape"}, {"node_type": "writing"},
                                       {"research_question": ""}])
def test_invalid_service_request_cannot_dispatch(state, monkeypatch, override):
    from unittest.mock import AsyncMock
    dispatch = AsyncMock()
    monkeypatch.setattr("shared.tools.run_node._run_node_tool", dispatch)
    result = call(state, "request_literature_evidence", **{
        "research_question": "Parameter?", **override})
    assert result["status"] != "success"
    dispatch.assert_not_called()


@pytest.mark.parametrize("status", ["pause", "error"])
def test_non_success_receipt_and_unconfirmed_recovery(state, monkeypatch, status):
    from unittest.mock import AsyncMock
    dispatch = AsyncMock(return_value={"status": status, "reason": "upstream receipt"})
    monkeypatch.setattr("shared.tools.run_node._run_node_tool", dispatch)
    result = call(state, "request_literature_evidence", research_question="Range?",
                  mode="contradiction_check", forward_artifact_ids=["existing-index"])
    assert result["status"] == status and result["reason"] == "upstream receipt"
    assert dispatch.await_args.kwargs["forward_artifact_ids"] == ["existing-index"]
    recovery = get_tool("request_literature_evidence").interrupted_recovery
    assert asyncio.run(recovery({}, state=state, tool_call_id="missing")) is None
    dispatch.assert_awaited_once()

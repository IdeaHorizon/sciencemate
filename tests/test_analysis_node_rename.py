"""Upgrade old research state through the real loader, registry and ledger."""

from __future__ import annotations

import hashlib
import json

import pytest

from core import conversation_store, ledger, memory, memory_delivery, skill_library
from core.bootstrap import bootstrap
from core.directives_loader import extract_relevant_project_directives
from core.loader import list_harnesses, load_harness
from core.naming import canonical_node_fields
from core.paths import analysis_drafts_dir, prefer_existing
from core.state import State, StateContractError
from core.tool_registry import all_tool_names, execute, get_tool, list_tools_for_node


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_FRAMEWORK_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HARNESS_FRAMEWORK_PROJECTS_HOME", str(tmp_path / "projects"))


def test_one_node_and_one_tool_surface_with_old_callers(tmp_path):
    bootstrap()
    assert "analysis" in list_harnesses()
    assert "hypothesis" not in list_harnesses()
    current = load_harness("analysis")
    assert load_harness("hypothesis") == current
    assert get_tool("validate_hypothesis_outputs") is get_tool("validate_analysis_outputs")
    assert "validate_hypothesis_outputs" not in all_tool_names()
    exposed = list_tools_for_node(
        "hypothesis", ["validate_hypothesis_outputs", "validate_analysis_outputs"]
    )
    assert [tool.name for tool in exposed] == ["validate_analysis_outputs"]
    assert list_tools_for_node("writing", ["validate_hypothesis_outputs"]) == []


@pytest.mark.asyncio
async def test_old_tool_and_artifact_arguments_still_dispatch(tmp_path):
    bootstrap()
    state = State.new("hypothesis", tmp_path / "runs")
    assert state.node_type == "analysis"
    result = await execute(
        "stage_hypothesis_draft",
        state,
        artifact_type="hypothesis_research_overview",
        content="# Research overview",
    )
    assert result["status"] == "success"
    assert "analysis_research_overview" in result["staged_file"]
    assert analysis_drafts_dir(state).joinpath("analysis_research_overview.md").is_file()
    saved = await execute(
        "save_artifact",
        state,
        artifact_type="report",
        name="hypothesis_research_overview",
        content="A scientific report",
        metadata={"node_type": "hypothesis", "hypothesis_id": "H1"},
    )
    assert saved["status"] == "success"
    record = state.read_artifact(saved["id"])
    assert record["name"] == "hypothesis_research_overview"
    assert record["metadata"]["node_type"] == "hypothesis"


def test_old_artifact_continues_same_frozen_identity_and_hash_chain(tmp_path):
    state = State.new("analysis", tmp_path / "runs")
    store = state._run_store()
    old_id = "hypothesis_research_overview__hypothesis_research_overview"
    path = store.root / f"{old_id}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# Original overview")
    store._append(
        {
            "event": "save",
            "id": old_id,
            "type": "hypothesis_research_overview",
            "name": "hypothesis_research_overview",
            "path": path.name,
            "version": 1,
            "sha256": ledger.sha256_text(path.read_text()),
            "created_at": "2026-11-01T00:00:00Z",
            "produced_by_node_type": "hypothesis",
            "produced_by_run_id": "original-run",
            "provenance": {
                "kind": "produced",
                "by_node_type": "hypothesis",
                "by_run_id": "original-run",
            },
            "metadata": {},
        }
    )
    store.freeze(old_id, metadata_patch={}, by_node="hypothesis", by_run="original-run")
    original_ledger = store.ledger_path.read_bytes()
    row = state.list_artifacts("analysis_research_overview", own_only=True)[0]
    assert row["id"] == old_id
    assert row["owner_node"] == "hypothesis"  # historical producer is evidence
    with pytest.raises(StateContractError, match="amendment_reason"):
        state.save_artifact("analysis_research_overview", "analysis_research_overview", "# Updated")
    saved = state.save_artifact(
        "analysis_research_overview",
        "analysis_research_overview",
        "# Updated",
        amendment_reason="Update the research plan",
    )
    assert saved["id"] == old_id
    assert saved["version"] == 2
    assert path.read_text() == "# Updated"
    assert store.ledger_path.read_bytes().startswith(original_ledger)
    new_id = "analysis_research_overview__analysis_research_overview"
    assert store.head(new_id).artifact_id == old_id
    store.freeze(new_id, metadata_patch={}, by_node="analysis", by_run=state.run_id)
    assert store.head(old_id).frozen_version == 2
    rows = store.ledger_path.read_text().splitlines()
    for before, after in zip(rows, rows[1:]):
        assert json.loads(after)["prev_row_sha256"] == hashlib.sha256(before.encode()).hexdigest()
    assert len(store.heads()) == 1


def test_restoring_old_conversation_keeps_ids_and_source_bytes(tmp_path):
    state = State.new("analysis", tmp_path / "runs")
    old_run = "run_root::_orchestrator->hypothesis@d1"
    payload = {
        "messages": [{"role": "user", "content": "Continue research"}],
        "hook_state": {
            "pending_post_node_flow": [
                {
                    "producing_node": "hypothesis",
                    "producing_run_id": old_run,
                    "deferred_to_node": "hypothesis",
                }
            ]
        },
    }
    path = conversation_store.conversation_path(state)
    path.write_text(json.dumps(payload))
    original = path.read_bytes()
    assert conversation_store.load_conversation(state)
    entry = state.hook_state["pending_post_node_flow"][0]
    assert entry["producing_node"] == entry["deferred_to_node"] == "analysis"
    assert entry["producing_run_id"] == old_run
    assert path.read_bytes() == original
    facts = {
        "claim_type": "hypothesis",
        "hypothesis_id": "H1",
        "hypotheses": [],
        "provenance": {"node_type": "hypothesis"},
        "node_type": "hypothesis",
    }
    view = canonical_node_fields(facts)
    assert view == {**facts, "node_type": "analysis"}


def test_old_skill_assignment_and_node_instructions_remain_effective(tmp_path):
    path = skill_library.bindings_path("p")
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"bindings": [{"nodes": ["hypothesis"], "enabled": True}]}))
    original = path.read_bytes()
    config = skill_library.read_bindings("p")
    assert config["bindings"][0]["nodes"] == ["analysis"]
    assert path.read_bytes() == original
    assert skill_library.read_bindings("p")["revision"] == config["revision"]
    old = "# Project\n## 节点级指令\n### hypothesis\nKeep the budget.\n"
    assert "Keep the budget" in extract_relevant_project_directives(old, "analysis")
    both = old + "### analysis\nKeep the revised budget.\n"
    selected = extract_relevant_project_directives(both, "hypothesis")
    assert "Keep the revised budget" in selected
    assert "Keep the budget." not in selected


def test_old_memory_addresses_reach_analysis_without_rewriting_the_lesson(tmp_path):
    from types import SimpleNamespace

    worktree = tmp_path / "research"
    worktree.mkdir()
    state = SimpleNamespace(project_worktree=worktree)
    memory.append_manual(
        state,
        text="Review the original evidence.",
        section=memory.SECTION_PITFALL,
        nodes=["analysis"],
        tools=["validate_analysis_outputs"],
        run_id="old-run",
    )
    path = memory.memory_path(state)
    path.write_text(
        path.read_text()
        .replace("nodes=analysis", "nodes=hypothesis")
        .replace("tools=validate_analysis_outputs", "tools=validate_hypothesis_outputs")
    )
    old_bytes = path.read_bytes()
    assert "Review the original evidence" in memory_delivery.onboarding_slice(state, "analysis")
    assert "Review the original evidence" in memory_delivery.tool_briefing(
        state, "validate_analysis_outputs"
    )
    assert path.read_bytes() == old_bytes


def test_old_tasks_remain_assigned_to_the_same_analysis_node(tmp_path):
    from core.tasks import TaskList

    tasks = TaskList(tmp_path / "tasks")
    tasks.jsonl_path.write_text(
        json.dumps(
            {
                "id": "T01",
                "title": "Revise the original plan",
                "owner_node": "hypothesis",
                "status": "pending",
            }
        )
        + "\n"
    )
    original = tasks.jsonl_path.read_bytes()
    assert tasks.filter(owner_node="analysis")[0].id == "T01"
    assert tasks.filter(owner_node="hypothesis")[0].owner_node == "analysis"
    assert tasks.jsonl_path.read_bytes() == original
    assert tasks.start("T01", "analysis").status == "in_progress"


def test_legacy_run_draft_paths_can_be_read_without_new_writes_there(tmp_path):
    state = State.new("analysis", tmp_path / "runs")
    old = state.root / "outputs/hypothesis/drafts/overview.md"
    old.parent.mkdir(parents=True)
    old.write_text("Old draft")
    canonical = analysis_drafts_dir(state) / "overview.md"
    assert prefer_existing(state.root, canonical) == old
    assert "outputs/analysis/drafts" in canonical.as_posix()


@pytest.mark.asyncio
async def test_an_interrupted_old_draft_can_be_saved_by_its_original_relative_path(tmp_path):
    bootstrap()
    state = State.new("analysis", tmp_path / "runs")
    old = state.root / "outputs/hypothesis/drafts/hypothesis_research_overview.md"
    old.parent.mkdir(parents=True)
    old.write_text("# The interrupted research overview")
    saved = await execute(
        "save_artifact",
        state,
        artifact_type="hypothesis_research_overview",
        name="hypothesis_research_overview",
        content_from_file="drafts/hypothesis_research_overview.md",
    )
    assert saved["status"] == "success"
    assert saved["id"] == "analysis_research_overview__analysis_research_overview"
    assert state.read_artifact(saved["id"])["content"] == old.read_text()


@pytest.mark.asyncio
@pytest.mark.parametrize("saved_handle", [False, True])
async def test_old_analysis_dispatch_recovers_its_original_completed_child(
    tmp_path, monkeypatch, saved_handle
):
    import shared.tools.run_node as owner
    from core import child_dispatch

    bootstrap()
    parent = State.new("_orchestrator", tmp_path / "runs", session_id="s")
    child = State.new("analysis", parent.root.parent, session_id="s")
    child.append_transcript(
        "run_start",
        node_type="hypothesis",
        parent_run_id=parent.run_id,
        parent_dispatch_id="original-dispatch",
    )
    child.write_summary(
        {
            "run_id": child.run_id,
            "status": "completed",
            "state_dir": str(child.root),
            "artifacts": [],
        }
    )
    child.append_transcript("run_end", status="completed")
    child_bytes = child.transcript_path.read_bytes()
    args = {"node_type": "analysis"}
    payload = {
        "tool_dispatch": "original-call",
        "written_at": "2026-01-01T00:00:00Z",
        "messages": [],
    }
    if saved_handle:
        payload["child_dispatches"] = {
            "original-dispatch": {
                "dispatch_id": "original-dispatch",
                "call_id": "original-call",
                "node_type": "hypothesis",
                "child_run_id": child.run_id,
                "execution": {"node_type": "hypothesis"},
            }
        }
    parent.append_transcript(
        "subagent_call_start",
        child_node_type="hypothesis",
        parent_tool_call_id="original-call",
        dispatch_id="original-dispatch",
    )
    path = parent.root / "messages_checkpoint.json"
    path.write_text(json.dumps(payload))
    original = path.read_bytes()
    child_dispatch.checkpoint(parent)
    assert path.read_bytes() == original

    async def forbidden(**kwargs):
        pytest.fail("completed historical child must not execute again")

    monkeypatch.setattr("core.executor.execute_node", forbidden)
    monkeypatch.setattr("core.llm.LLMClient", lambda: object())
    result = await owner._recover_run_node(args, state=parent, tool_call_id="original-call")
    assert result["result"]["child_run_id"] == child.run_id
    assert result["result"]["child_status"] == "completed"
    assert child.transcript_path.read_bytes() == child_bytes
    assert {p.name for p in parent.root.parent.iterdir()} == {parent.run_id, child.run_id}

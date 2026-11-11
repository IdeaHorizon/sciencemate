"""Loaded methods must have a route within the consumer's real authority."""
import asyncio

import pytest

from core.bootstrap import bootstrap
from core.loader import load_harness
from core.skill_loader import load_all_skills
from core.state import State
from core.tool_registry import execute, list_tools_for_node


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("HARNESS_FRAMEWORK_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HARNESS_FRAMEWORK_ORG_HOME", str(tmp_path / "home" / "org"))
    bootstrap()
    load_all_skills()


@pytest.mark.parametrize("name", ["systematic_literature_search", "adversarial_evidence_search"])
def test_observation_loaded_search_has_literature_route(tmp_path, name):
    st = State.new(node_type="observation", base_dir=tmp_path)
    body = asyncio.run(execute("load_skill", st, name=name))["body_markdown"]
    assert 'node_type="literature"' in body and "user_note=" in body
    h = load_harness("observation")
    tools = {t.name for t in list_tools_for_node("observation", h.tools)}
    assert "run_node" in tools and "literature" in h.callable_nodes
    assert {"search_papers", "arxiv_search", "create_claim", "update_claim_status"}.isdisjoint(tools)


@pytest.mark.parametrize("node,name", [
    ("data", "ai4s_scientific_preprocessing"),
    ("_curator", "kb_integrity_audit"),
    ("_curator", "research_direction_exploration"),
])
def test_controlled_consumers_do_not_advertise_free_save(tmp_path, node, name):
    from core.skill_registry import get_skill
    st = State.new(node_type=node, base_dir=tmp_path)
    result = asyncio.run(execute("load_skill", st, name=name)) if node != "data" else None
    # Data uses its custom loop; its node-local skill is loaded by that controller.
    skill = get_skill(name)
    assert skill and "save_artifact" not in skill.tools_used
    assert "save_artifact(" not in skill.body_markdown
    h = load_harness(node)
    assert "save_artifact" not in {t.name for t in list_tools_for_node(node, h.tools)}
    if result:
        assert result["status"] == "success"


def test_current_owner_replaces_retired_scientific_authority():
    from pathlib import Path
    root = Path(__file__).resolve().parents[3]
    spec = (root / "nodes/_reviewer/specs/project_synthesis.md").read_text()
    assert "Analysis（hypothesis）" in spec
    assert "experiment 节点自判" not in spec
    assert "硬 gate" not in spec
    curator = (root / "nodes/_curator/harness.yaml").read_text()
    assert "命中机械 falsification_criteria 翻 hypothesis claim status" not in curator
    assert "直接 update_claim_status(to='validated'/'refuted')" not in curator


def test_observation_can_dispatch_and_consume_literature_package(tmp_path, monkeypatch):
    """Only model execution is stubbed; dispatch, scope and artifact intake are real."""
    st = State.new(node_type="observation", base_dir=tmp_path / "parent")
    from core.project_workspace import bind_project_workspace
    import subprocess
    worktree = tmp_path / "project"
    worktree.mkdir()
    subprocess.run(["git", "init", "-q", str(worktree)], check=True)
    subprocess.run(["git", "-C", str(worktree), "-c", "user.name=fixture",
                    "-c", "user.email=fixture@local", "commit", "-q", "--allow-empty", "-m", "fixture"], check=True)
    bind_project_workspace(st, worktree)
    st.hook_state["_callable_nodes"] = load_harness("observation").callable_nodes
    seen = []

    async def child(**kwargs):
        seen.append(kwargs)
        out = State.new(node_type="literature", base_dir=tmp_path / "child")
        bind_project_workspace(out, worktree)
        out.save_artifact("literature_evidence_package", "contradiction_check",
                          "Supports: source A. Contradicts: source B. Unavailable: source C.")
        return {"run_id": out.run_id, "node_type": "literature", "status": "completed",
                "state_dir": str(out.root), "artifacts": out.list_artifacts(),
                "missing_required_outputs": [], "turns": 1, "tokens_used": 1,
                "final_text_preview": "Evidence package delivered; no scientific verdict."}

    monkeypatch.setattr("core.executor.execute_node", child)
    result = asyncio.run(execute("run_node", st, node_type="literature",
        user_note="正在委派反向检索，随后核对证据。",
        node_inputs={"research_question": "A testable claim and its competing explanation",
                     "mode": "contradiction_check"}))
    assert result["status"] != "error", result
    assert seen[0]["node_inputs"]["mode"] == "contradiction_check"
    packages = [a for a in st.list_artifacts() if a["type"] == "literature_evidence_package"]
    assert len(packages) == 1
    read = asyncio.run(execute("read_artifact", st, artifact_id=packages[0]["id"]))
    assert read["status"] == "success" and "Unavailable: source C" in str(read)

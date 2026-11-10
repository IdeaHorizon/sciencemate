"""A legacy design accounting remains readable and cannot control closure."""
from types import SimpleNamespace

import pytest

from core import obligations
from core.ledger import workspace_store, write_record
from core.project_workspace import _NODE_WORKSPACES


@pytest.mark.parametrize("accounting", [None, {"n_replicas": "2/2"}])
def test_project_does_not_require_a_second_handwritten_execution_account(tmp_path, accounting):
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    plan = _NODE_WORKSPACES["hypothesis"]
    prereg = write_record(
        worktree, artifact_type="pre_registration", name="measurement",
        content="Measure with two replicates.", directory=plan,
        metadata={"expected_params": {"n_replicas": 2}},
        produced_by_node_type="hypothesis", produced_by_run_id="design",
    )
    store = workspace_store(worktree)
    store.freeze(prereg["id"], metadata_patch={}, by_node="hypothesis", by_run="design")
    research_state = write_record(
        worktree, artifact_type="research_state", name="research_state",
        content="Current research state.", directory=plan,
        metadata={"design_accounting": accounting} if accounting else {},
        produced_by_node_type="hypothesis", produced_by_run_id="design",
    )
    state = SimpleNamespace(
        project_worktree=worktree, node_type="_orchestrator", root=tmp_path / "runs" / "parent",
        project_id="fixture", run_id="parent",
    )

    assert "unaccounted_design" not in {row.kind for row in obligations.collect(state)}
    assert store.head(research_state["id"]).metadata.get("design_accounting") == accounting

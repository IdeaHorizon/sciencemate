"""An immutable task contract names the recipient before dispatch has effects."""

from pathlib import Path

import pytest

from core.bootstrap import bootstrap
from core.state import State
from core.task_contract import PreregAssignment, TaskContractLog
from shared.tools.run_node import _resolve_task_identity, _run_node_tool


@pytest.fixture
def parent(tmp_path):
    bootstrap()
    state = State.new(node_type="_orchestrator", base_dir=tmp_path / "runs")
    state.project_root = tmp_path / "project"
    state.project_root.mkdir()
    return state


def contract(parent, target, **kwargs):
    return TaskContractLog(parent.project_root / "tasks").append(
        task_instance_uuid="task-1",
        objective="Inspect existing evidence",
        target_node=target,
        prereg_assignment=PreregAssignment.none("operation"),
        actor="_orchestrator:parent",
        **kwargs,
    )


@pytest.mark.parametrize(
    "target,node",
    [
        ("_orchestrator", "experiment"),
        ("observation", "experiment"),
        ("experiment", "literature"),
        ("literature", "writing"),
    ],
)
def test_wrong_recipient_is_reported_with_an_immutable_correction(parent, target, node):
    revision = contract(parent, target)
    identity, error = _resolve_task_identity(parent, node, "task-1", revision.digest)
    assert identity is None
    assert error["error_code"] == "task_contract_target_mismatch"
    assert error["contract_target_node"] == target
    assert error["node_type"] == node
    assert error["task_contract_digest"] == revision.digest
    assert error["child_started"] is False
    assert "parent_revision_digest" in error["error"]


@pytest.mark.parametrize(
    "target,node",
    [
        ("experiment", "experiment"),
        ("hypothesis", "analysis"),
        ("", "experiment"),
    ],
)
def test_exact_recipient_alias_and_legacy_unspecified_target_are_supported(parent, target, node):
    revision = contract(parent, target)
    identity, error = _resolve_task_identity(parent, node, "task-1", revision.digest)
    assert error is None
    assert identity["task_contract_digest"] == revision.digest
    assert TaskContractLog(parent.project_root / "tasks").get("task-1", revision.digest) == revision


@pytest.mark.asyncio
@pytest.mark.parametrize("background", [False, True])
async def test_wrong_recipient_is_rejected_before_delegation_or_child_work(
    parent, monkeypatch, background
):
    revision = contract(parent, "_orchestrator")
    delegated = []
    monkeypatch.setattr(
        "core.project_workspace.note_delegated_workspace", lambda *args: delegated.append(args)
    )
    before = list((parent.project_root / "tasks").rglob("*"))
    result = await _run_node_tool(
        parent,
        "experiment",
        background=background,
        task_instance_uuid="task-1",
        task_contract_digest=revision.digest,
        node_inputs={"task": "Read the existing result"},
    )
    assert result["error_code"] == "task_contract_target_mismatch"
    assert delegated == []
    assert list((parent.project_root / "tasks").rglob("*")) == before
    assert not parent.hook_state.get("child_dispatches")


def test_correcting_the_target_preserves_old_contract_and_existing_result(parent):
    previous = contract(parent, "_orchestrator")
    evidence = Path(parent.project_root / "existing-result.txt")
    evidence.write_bytes(b"computed mean=5.0\n")
    corrected = contract(
        parent,
        "experiment",
        parent_revision_digest=previous.digest,
        reason="Correct dispatch recipient; reuse existing results",
    )
    identity, error = _resolve_task_identity(parent, "experiment", "task-1", corrected.digest)
    assert error is None
    assert identity["task_contract_revision"] == 2
    log = TaskContractLog(parent.project_root / "tasks")
    assert log.get("task-1", previous.digest) == previous
    assert evidence.read_bytes() == b"computed mean=5.0\n"

"""A correction keeps the original authority without reissuing historical work."""

from copy import deepcopy

from core.bootstrap import bootstrap
from core.context_engine import build_messages, read_first_turn_node_inputs_receipt
from core.loader import load_harness
from core.state import State


def test_metadata_revision_delivers_current_scope_separately_from_the_old_network_task(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HARNESS_FRAMEWORK_HOME", str(tmp_path / "home"))
    bootstrap()
    state = State.new("experiment", tmp_path / "runs")
    inputs = {
        "experiment_spec": "R8: retry publisher full texts and verify twelve papers.",
        "mode": "revise",
        "reviewer_feedback": 'Correct citation labels and metada'
        'ta. No network retrieval is needed.',
        "human_directive": "Only correct the existing evidence table; reuse its frozen results.",
        "prereg_artifact_id": "pre_registration__original",
    }
    before = deepcopy(inputs)
    messages = build_messages(load_harness("experiment"), state, inputs)
    text = messages[1].content
    current, historical = text.split("### 原任务与既有约束", 1)
    assert (
        "Correct citation labels" in current
        and "Only correct the existing evidence table" in current
    )
    assert "R8: retry publisher" not in current
    assert "R8: retry publisher" in historical and "pre_registration__original" in historical
    assert inputs == before
    assert read_first_turn_node_inputs_receipt(state)["values"] == before


def test_a_revision_can_explicitly_require_new_execution(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_FRAMEWORK_HOME", str(tmp_path / "home"))
    bootstrap()
    state = State.new("experiment", tmp_path / "runs")
    messages = build_messages(
        load_harness("experiment"),
        state,
        {
            "mode": "revise",
            "experiment_spec": "Run the solver.",
            "reviewer_feedback": "Rerun the solver with the corrected boundary condition.",
        },
    )
    text = messages[1].content
    current, historical = text.split("### 原任务与既有约束", 1)
    assert "Rerun the solver with the corrected boundary condition." in current
    assert "Run the solver." in historical


def test_revise_without_a_current_correction_preserves_the_original_input_rendering():
    from core.context_engine import render_node_inputs

    values = {"mode": "revise", "experiment_spec": "Continue the original task."}
    assert render_node_inputs(values) == (
        "## 节点输入\n- **mode**：revise\n- **experiment_spec**：Continue the original task."
    )

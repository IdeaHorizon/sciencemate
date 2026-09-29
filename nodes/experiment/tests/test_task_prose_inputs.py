"""Node-owned task-prose input declaration and shared selector tests."""
from __future__ import annotations

from pathlib import Path
import json

import pytest
import yaml

from core.state import State
from nodes.experiment import hooks
from nodes.experiment import task_prose_inputs as prose
from nodes.experiment.tools import resource_manager as manager


def _document(**changes):
    document = {
        "expected_inputs": {"experiment_spec": "Task body."},
        "task_prose_input_keys": ["experiment_spec"],
        "task_prose_compatibility_input_keys": ["experiment_focus"],
    }
    document.update(changes)
    return document


def _write_harness(path: Path, **changes) -> Path:
    path.write_text(
        yaml.safe_dump(_document(**changes), allow_unicode=True),
        encoding="utf-8",
    )
    return path


def test_live_contract_is_valid_and_consumed_by_both_readers():
    contract = prose.load_task_prose_input_contract()

    assert contract.canonical_keys == ("experiment_spec",)
    assert contract.compatibility_keys == ("experiment_focus",)
    assert hooks.first_task_prose_source is prose.first_task_prose_source
    assert manager.first_task_prose_source is prose.first_task_prose_source


def test_shared_selector_prefers_canonical_then_nonblank_compatibility():
    assert prose.first_task_prose_source({
        "experiment_spec": "canonical body",
        "experiment_focus": "compatibility body",
    }) == prose.TaskProseSource("experiment_spec", "canonical body")
    assert prose.first_task_prose_source({
        "experiment_spec": "   ",
        "experiment_focus": "compatibility body",
    }) == prose.TaskProseSource("experiment_focus", "compatibility body")
    assert prose.first_task_prose_source({"experiment_spec": " "}) is None
    assert prose.first_task_prose_source(None) is None



def _transcript_events(state: State) -> list[dict]:
    return [
        json.loads(line) for line in state.transcript_path.read_text(
            encoding="utf-8"
        ).splitlines()
    ]


def test_turn_one_receipt_freezes_the_selected_structured_source(tmp_path):
    state = State.new("experiment", tmp_path)
    state.hook_state["node_inputs"] = {"experiment_focus": "compatibility body"}
    state.append_transcript(
        "startup_injection_manifest", node_input_keys=["experiment_focus"],
    )
    state.append_transcript("loop_seed", messages=[])

    prose.freeze_initial_task_prose_source(state)
    state.hook_state["node_inputs"]["experiment_focus"] = "changed later"
    prose.freeze_initial_task_prose_source(state)

    receipts = [
        event for event in _transcript_events(state)
        if event["event"] == prose.TASK_PROSE_INPUT_RECEIPT_EVENT
    ]
    assert len(receipts) == 1
    receipt = receipts[0]
    assert receipt["schema_version"] == prose.TASK_PROSE_INPUT_RECEIPT_SCHEMA_VERSION
    assert receipt["source_key"] == "experiment_focus"
    assert receipt["source_text"] == "compatibility body"
    assert receipt["source_sha256"]


def test_receipt_is_not_created_after_a_second_loop_seed(tmp_path):
    state = State.new("experiment", tmp_path)
    state.hook_state["node_inputs"] = {"experiment_focus": "new input"}
    state.append_transcript(
        "startup_injection_manifest", node_input_keys=["experiment_focus"],
    )
    state.append_transcript("loop_seed", messages=[])
    state.append_transcript("loop_seed", messages=[])

    prose.freeze_initial_task_prose_source(state)

    assert not [
        event for event in _transcript_events(state)
        if event["event"] == prose.TASK_PROSE_INPUT_RECEIPT_EVENT
    ]


@pytest.mark.parametrize(
    ("changes", "match"),
    [
        ({"task_prose_input_keys": None}, "non-empty list"),
        ({"task_prose_input_keys": []}, "non-empty list"),
        ({"task_prose_input_keys": "experiment_spec"}, "non-empty list"),
        ({"task_prose_input_keys": [1]}, "only non-empty strings"),
        ({"task_prose_input_keys": ["experiment_spec", "experiment_spec"]}, "must not repeat"),
        ({"task_prose_input_keys": ["unknown"]}, "not expected_inputs"),
        ({"task_prose_compatibility_input_keys": ["experiment_spec"]}, "overlap"),
        ({"expected_inputs": {"experiment_spec": "task", "experiment_focus": "task"}}, "must be undeclared aliases"),
        ({"expected_inputs": []}, "expected_inputs must be a mapping"),
    ],
    ids=[
        "missing", "empty", "scalar", "non_string", "duplicate", "unknown_canonical",
        "overlap", "formal_compatibility", "malformed_expected_inputs",
    ],
)
def test_invalid_task_prose_declaration_fails_loudly(tmp_path, changes, match):
    path = _write_harness(tmp_path / "harness.yaml", **changes)

    with pytest.raises(prose.TaskProseInputConfigurationError, match=match):
        prose.load_task_prose_input_contract(path)

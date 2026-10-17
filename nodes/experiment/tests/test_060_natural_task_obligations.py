"""Natural task text is frozen A, not a missing internal acceptance array.

These tests cover source preservation and the model-facing connection only.
They do not claim mechanical proof of arbitrary natural-language coverage.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from core.state import State
from nodes.experiment.tools import ledger, ledger_hooks, obligations, run_bootstrap


def _state(tmp_path, inputs):
    state = State.new(
        node_type="experiment", base_dir=tmp_path / "runs",
        project_id="natural-task", session_id="owner",
    )
    state.hook_state["node_inputs"] = inputs
    ledger.open_ledger(state, write=True, bootstrap=True)
    return state


@pytest.mark.parametrize("key", ["experiment_spec", "experiment_focus", "requested_work"])
def test_natural_task_freezes_exact_text_with_source_without_a_declaring_tool(tmp_path, key):
    text = "  Build the supplied source.\nRun it three times independently; preserve every PID.\n"
    state = _state(tmp_path, {key: text})
    result = obligations.freeze(state)

    assert result["status"] == "frozen"
    assert result["task_source"] == {"source": f"node_inputs.{key}", "text": text}
    assert result["obligations"][0] == {
        "id": "task_text", "kind": "task", "text": text,
        "source": f"node_inputs.{key}", "required": True,
    }
    assert [row["artifact_type"] for row in result["obligations"][1:]] == [
        "raw_results", "clean_results", "experiment_log",
    ]
    assert obligations.task_source(state) == result["task_source"]
    assert [row["kind"] for row in ledger.open_ledger(state).records()] == ["obligations_frozen"]


def test_supplied_checks_cannot_replace_natural_task_on_first_freeze_or_reentry(tmp_path):
    text = "Run the original input three times and compare full results."
    state = _state(tmp_path, {"experiment_spec": text})
    weaker = [{"id": "shortcut", "kind": "check", "command": "true"}]
    first = obligations.freeze(state, supplied=weaker)
    before = obligations.obligations_path(state).read_bytes()
    assert first["obligations"][0]["kind"] == "task"
    assert all(row["id"] != "shortcut" for row in first["obligations"])

    state.hook_state["node_inputs"]["experiment_spec"] = "Do anything."
    second = obligations.freeze(state, supplied=weaker)
    assert second["status"] == "already_frozen"
    assert obligations.obligations_path(state).read_bytes() == before
    assert obligations.task_source(state)["text"] == text


def test_structured_requirements_remain_authoritative_and_keep_the_task_source(tmp_path):
    text = "Run three independent processes and retain the summary."
    requirement = {"id": "summary", "kind": "file", "path": "summary.json"}
    state = _state(tmp_path, {"experiment_spec": text, "required_outputs": [requirement]})
    result = obligations.freeze(state, supplied=[{"path": "unrelated.txt"}])

    assert result["status"] == "frozen"
    assert result["task_source"] == {"source": "node_inputs.experiment_spec", "text": text}
    assert result["obligations"][0] == {
        **requirement, "required": True, "source": "node_inputs.required_outputs", "non_empty": True,
    }
    assert not any(row["kind"] == "task" for row in result["obligations"])
    assert len(result["obligations"]) == 4


def test_only_the_declared_prose_keys_are_sources_and_canonical_takes_precedence(tmp_path):
    state = _state(tmp_path, {
        "experiment_spec": "canonical task", "experiment_focus": "compatibility task",
        "requested_work": "older task", "scope": "scientific",
    })
    result = obligations.freeze(state)
    assert result["task_source"] == {"source": "node_inputs.experiment_spec", "text": "canonical task"}
    assert result["obligations"][0]["text"] == "canonical task"


def test_optional_structured_rows_do_not_replace_natural_task_requirements(tmp_path):
    state = _state(tmp_path, {
        "experiment_spec": "Build the program and validate its output.",
        "required_outputs": [{"id": "timings", "path": "timings.txt", "required": False}],
    })
    result = obligations.freeze(state)
    assert result["status"] == "frozen"
    rows = result["obligations"]
    assert rows[0]["id"] == "timings" and rows[0]["required"] is False
    assert rows[1]["kind"] == "task" and rows[1]["required"] is True
    assert rows[1]["text"] == "Build the program and validate its output."


def test_optional_rows_alone_do_not_supply_a_missing_task(tmp_path):
    state = _state(tmp_path, {
        "required_outputs": [{"id": "timings", "path": "timings.txt", "required": False}],
    })
    result = obligations.freeze(state)
    assert result["status"] == "clarification_required"
    assert not obligations.obligations_path(state).exists()


@pytest.mark.parametrize("inputs", [{}, {"scope": "operation"}, {"experiment_spec": " \n "},
                                      {"experiment_spec": {"text": "not task prose"}}])
def test_missing_actual_task_and_typed_requirements_still_needs_clarification(tmp_path, inputs):
    state = _state(tmp_path, inputs)
    result = obligations.freeze(state)
    assert result["status"] == "clarification_required"
    assert not obligations.obligations_path(state).exists()
    assert obligations.task_source(state) is None


def test_structured_task_without_prose_is_supported_and_does_not_invent_prose(tmp_path):
    state = _state(tmp_path, {"required_outputs": [{"path": "actual.bin"}]})
    result = obligations.freeze(state)
    assert result["status"] == "frozen"
    assert result["task_source"] is None
    assert obligations.task_source(state) is None


def test_task_source_reader_is_read_only_and_does_not_fall_back_to_new_inputs(tmp_path):
    state = _state(tmp_path, {"experiment_spec": "original"})
    before = sorted(str(path) for path in state.root.rglob("*"))
    assert obligations.task_source(state) is None
    assert sorted(str(path) for path in state.root.rglob("*")) == before
    obligations.freeze(state)
    state.hook_state["node_inputs"]["experiment_spec"] = "replacement"
    assert obligations.task_source(state) == {"source": "node_inputs.experiment_spec", "text": "original"}


def test_one_page_and_delta_describe_evidence_not_a_missing_internal_array(tmp_path):
    text = "Build and run the supplied program three times; compare the results."
    state = State.new(node_type="experiment", base_dir=tmp_path / "runs",
                      project_id="p", session_id="owner")
    state.hook_state["node_inputs"] = {"experiment_spec": text}
    established = asyncio.run(run_bootstrap.establish(state))
    assert established["obligations"]["status"] == "frozen"
    assert ledger.bootstrap_phase(state) == "ready"
    page = run_bootstrap.one_page(state, established)
    delta = ledger_hooks.build_delta_view(state)
    assert text in page
    assert "verification" in page and "verification" in delta
    assert "无需预先声明" in page
    assert "没有点名可核" not in page
    assert "首个有副作用的动作前问一次" not in delta
    assert "task_text：检查命令" not in delta

    state.hook_state["node_inputs"]["experiment_spec"] = "silently replaced task"
    restored_page = run_bootstrap.one_page(state, established)
    assert text in restored_page
    assert "silently replaced task" not in restored_page
    saved = json.loads(obligations.obligations_path(state).read_text())
    assert saved["task_source"]["text"] == text


def test_empty_task_delta_does_not_promise_an_unwired_automatic_question(tmp_path):
    state = _state(tmp_path, {})
    delta = ledger_hooks.build_delta_view(state)
    assert "首个有副作用的动作前问一次" not in delta
    assert "任务正文" in delta

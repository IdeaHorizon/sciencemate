"""Natural-method guidance/defaults, distinct from frozen formal requirements."""
from __future__ import annotations

import asyncio
import copy
import shlex
import sys

import pytest

from core.state import State
from nodes.experiment.tools import (
    delivery_transaction,
    finish,
    ledger,
    obligations,
    run_bootstrap,
    safe_bash,
    task_verification,
)


TASK = "Create stderr.txt and retain the actual diagnostic output, even if empty."


def _run(awaitable):
    return asyncio.run(awaitable)


def _state(tmp_path, *, formal_file=False):
    state = State.new("experiment", tmp_path / "runs", project_id="guidance", session_id="owner")
    path = state.root / "outputs/experiment/runtime/stderr.txt"
    state.hook_state["node_inputs"] = {"experiment_spec": TASK}
    if formal_file:
        state.hook_state["node_inputs"]["required_outputs"] = [
            {"id": "formal_non_empty", "kind": "file", "path": str(path)},
        ]
    _run(run_bootstrap.establish(state))
    assert ledger.bootstrap_phase(state) == "ready"
    return state, path


def _command(program):
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(program)}"


def _produce(state, path, content=""):
    result = _run(safe_bash._safe_run_bash(
        state,
        cmd=_command(f"from pathlib import Path; Path({str(path)!r}).write_text({content!r})"),
        cwd=str(path.parent), timeout=20,
    ))
    assert result.get("returncode") == 0, result


def _verification(methods):
    return [{"requirement": TASK, "checks": methods}]


def _finish(state, methods):
    return _run(finish._finish(
        state, outcome="success", summary="The actual diagnostic file was preserved and checked.",
        verification=_verification(methods),
    ))


@pytest.mark.parametrize("supplied, expected", [({}, False), ({"non_empty": False}, False), ({"non_empty": True}, True)])
def test_natural_file_default_is_existence_not_a_nonempty_claim(tmp_path, supplied, expected):
    state, path = _state(tmp_path)
    method = {"kind": "file", "path": str(path), **supplied}
    original = copy.deepcopy(method)

    prepared = task_verification.prepare(state, _verification([method]))

    assert prepared["checks"][0]["non_empty"] is expected
    assert method == original


def test_real_empty_output_is_not_rejected_by_an_implicit_model_method_size_rule(tmp_path):
    state, path = _state(tmp_path)
    before_a = obligations.obligations_path(state).read_bytes()
    _produce(state, path)

    result = _finish(state, [{"kind": "file", "path": str(path)}])

    assert result.get("status") == "success", result
    assert result.get("outcome") == "success"
    assert path.stat().st_size == 0
    assert obligations.obligations_path(state).read_bytes() == before_a


def test_formal_file_default_stays_nonempty_despite_the_model_method_default(tmp_path):
    state, path = _state(tmp_path, formal_file=True)
    before_a = obligations.obligations_path(state).read_bytes()
    _produce(state, path)

    result = _finish(state, [{"kind": "file", "path": str(path)}])

    assert result.get("status") == "success_unsupported", result
    assert any(row.get("obligation") == "formal_non_empty" and row.get("gap") == "empty"
               for row in result.get("gaps", [])), result
    formal = next(row for row in obligations.frozen(state) if row["id"] == "formal_non_empty")
    assert formal["non_empty"] is True
    assert obligations.obligations_path(state).read_bytes() == before_a
    assert delivery_transaction.read_manifest(state) is None


def test_explicit_natural_nonempty_requirement_still_rejects_empty_output(tmp_path):
    state, path = _state(tmp_path)
    _produce(state, path)

    result = _finish(state, [{"kind": "file", "path": str(path), "non_empty": True}])

    assert result.get("status") == "success_unsupported", result
    assert any(row.get("gap") == "empty" for row in result.get("gaps", [])), result


@pytest.mark.parametrize("content, expected_status", [("", "success"), ("warning\n", "success_unsupported")])
def test_zero_bytes_is_a_real_content_check_not_nonempty_false(tmp_path, content, expected_status):
    state, path = _state(tmp_path)
    _produce(state, path, content)
    methods = [
        {"kind": "file", "path": str(path), "non_empty": False},
        {"kind": "check", "command": _command(
            f"from pathlib import Path; assert Path({str(path)!r}).stat().st_size == 0"
        )},
    ]

    result = _finish(state, methods)

    assert result.get("status") == expected_status, result
    if content:
        assert any(row.get("gap") == "check_failed" for row in result.get("gaps", [])), result


def test_glob_path_error_names_pattern_and_can_be_corrected_before_any_check_runs(tmp_path):
    state, path = _state(tmp_path)
    _produce(state, path, "diagnostic\n")
    before = ledger.open_ledger(state).records()
    before_a = obligations.obligations_path(state).read_bytes()
    first_check = {"kind": "check", "command": "true"}

    result = _finish(state, [first_check, {"kind": "glob", "path": str(path.parent / "*.txt")}])

    assert result.get("error_code") == "finish_parameters_invalid", result
    error = result["error"]
    assert "verification[0].checks[1]" in error
    assert "path" in error and "pattern" in error and "min_count" in error
    assert '{"kind": "glob", "pattern":' in error
    assert "retry" in error.lower()
    assert ledger.open_ledger(state).records() == before
    assert obligations.obligations_path(state).read_bytes() == before_a
    assert delivery_transaction.read_manifest(state) is None

    corrected = _finish(state, [first_check, {
        "kind": "glob", "pattern": str(path.parent / "*.txt"), "min_count": 1,
    }])
    assert corrected.get("status") == "success", corrected
    assert len([row for row in ledger.open_ledger(state).records()
                if row.get("kind") == "action_started" and row.get("tool") == "safe_run_bash"]) == 1


def test_missing_glob_pattern_error_also_shows_a_valid_shape(tmp_path):
    state, _ = _state(tmp_path)
    with pytest.raises(task_verification.VerificationError) as raised:
        task_verification.prepare(state, _verification([{"kind": "glob", "min_count": 1}]))
    assert "verification[0].checks[0]" in str(raised.value)
    assert '{"kind": "glob", "pattern":' in str(raised.value)


def test_schema_teaches_kind_specific_fields_and_the_empty_file_boundary():
    properties = task_verification.CHECK_SCHEMA["properties"]
    for name in ("kind", "path", "pattern", "min_count", "command", "cwd", "non_empty",
                 "expect_stdout_contains"):
        assert properties[name].get("description"), name
    assert properties["non_empty"].get("default") is False
    assert "零字节" in properties["non_empty"]["description"]
    assert "check" in properties["non_empty"]["description"]
    assert "只读" in properties["command"]["description"]
    assert "cwd" in properties["command"]["description"]
    assert "pattern" in properties["pattern"]["description"]
    assert "path" in properties["pattern"]["description"]
    marker_guidance = properties["expect_stdout_contains"]["description"]
    assert all(term in marker_guidance for term in ("字面", "浮点", "容差", "稳定"))


@pytest.mark.parametrize("marker, expected_status", [
    ("NUMERIC_OK", "success"),
    ("VALUE 0.3 NUMERIC_OK", "success_unsupported"),
])
def test_numeric_assertion_uses_a_stable_marker_not_float_text(tmp_path, marker, expected_status):
    state, path = _state(tmp_path)
    _produce(state, path, "diagnostic\n")
    program = (
        "import math; value = 0.1 + 0.2; "
        "assert math.isclose(value, 0.3, rel_tol=0, abs_tol=1e-12); "
        "print(f'VALUE {value} NUMERIC_OK')"
    )
    result = _finish(state, [
        {"kind": "file", "path": str(path)},
        {"kind": "check", "command": _command(program), "expect_stdout_contains": marker},
    ])

    assert result.get("status") == expected_status, result
    if expected_status == "success_unsupported":
        assert any(row.get("gap") == "check_failed" for row in result.get("gaps", [])), result

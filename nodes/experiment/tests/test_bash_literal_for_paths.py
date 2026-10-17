"""Bounded literal loops project paths without guessing dynamic shell state.

These tests only parse shell text; none of the example payloads is executed.
"""
from __future__ import annotations

import pytest

from nodes.experiment.tools import bash_semantics as bs
from nodes.experiment.tools.safe_bash import _analyze_shell_path_effects


def _effects(command):
    return _analyze_shell_path_effects(command, "/work")


def test_literal_loop_projects_each_pipeline_output():
    command = (
        'ROOT=/work; for cfg in gentle balanced brisk; do '
        'printf x | tee "$ROOT/stdout/${cfg}.jsonl"; done'
    )
    assert _effects(command) == [
        ("tee", f"/work/stdout/{cfg}.jsonl")
        for cfg in ("gentle", "balanced", "brisk")
    ]


def test_nested_literal_loops_project_cartesian_paths():
    command = (
        'ROOT=/work; for cfg in gentle balanced; do '
        'for stage in continuous resume; do '
        'printf x | tee "$ROOT/${cfg}_${stage}.txt"; done; done'
    )
    assert _effects(command) == [
        ("tee", f"/work/{cfg}_{stage}.txt")
        for cfg in ("gentle", "balanced")
        for stage in ("continuous", "resume")
    ]


def test_literal_loop_preserves_order_duplicates_and_final_binding():
    analysis = bs.analyze_bash(
        'for cfg in a a b; do touch "$cfg"; done; touch "$cfg"',
        initial_cwd="/work",
    )
    touches = [e for e in analysis.static_path_events if e.head == "touch"]
    assert [e.args for e in touches] == [("a",), ("a",), ("b",), ("b",)]
    assert all(not e.cwd.unknown and not e.dynamic_args for e in touches)


def test_loop_cwd_failure_outcomes_are_not_discarded():
    assert set(_effects('for cfg in a b; do cd "$cfg"; touch out; done')) == {
        ("touch", "/work/out"), ("touch", "/work/a/out"),
        ("touch", "/work/b/out"), ("touch", "/work/a/b/out"),
    }


def test_loop_exit_does_not_fabricate_later_iteration():
    assert _effects('for cfg in a b; do touch "$cfg"; exit; done; touch later') == [
        ("touch", "/work/a"),
    ]


@pytest.mark.parametrize("values", [
    "$CONFIGS", "$(printf a)", "*", "{a,b}", '"$CONFIGS"',
    "'a b'", '"a*"',
])
def test_nonliteral_loop_values_stay_unknown(values):
    command = f'for cfg in {values}; do printf x | tee "$cfg"; done'
    assert any(target == "<unresolved>" for _, target in _effects(command))


def test_unknown_cwd_stays_unknown_for_relative_loop_output():
    assert any(target == "<unresolved>" for _, target in _effects(
        'cd "$UNKNOWN"; for cfg in a b; do printf x | tee "$cfg"; done'
    ))


@pytest.mark.parametrize("mutation", [
    "read ROOT", "unset ROOT", "printf -v ROOT /protected",
    "builtin read ROOT", "export ROOT=/protected",
])
@pytest.mark.parametrize("in_loop", [False, True])
def test_mutating_builtin_invalidates_old_path_binding(mutation, in_loop):
    body = mutation + '; printf x | tee "$ROOT/${cfg}.txt"'
    command = 'ROOT=/work; cfg=a; '
    command += f'for cfg in a b; do {body}; done' if in_loop else body
    assert any(target == "<unresolved>" for _, target in _effects(command))


@pytest.mark.parametrize("control", ["break", "continue", "break 2", "continue 2"])
def test_unmodeled_loop_control_stays_unknown(control):
    assert any(target == "<unresolved>" for _, target in _effects(
        f'for cfg in a b; do {control}; touch "$cfg"; done; touch "$cfg"'
    ))


def test_expansion_budget_is_unknown_not_a_complete_prefix(monkeypatch):
    monkeypatch.setattr(bs, "_MAX_STATIC_COMMANDS", 5)
    command = 'for a in x y z; do for b in p q r; do touch "$a/$b"; done; done'
    analysis = bs.analyze_bash(command, initial_cwd="/work")
    assert analysis.path_unverifiable
    assert any(target == "<unresolved>" for _, target in _effects(command))


@pytest.mark.parametrize("mutation", [
    "((cfg++))", "IFS=a", "declare -n ref=ROOT; read ref",
])
def test_unmodeled_shell_state_cannot_reuse_literal_projection(mutation):
    command = f'ROOT=/work; for cfg in a b; do {mutation}; touch "$ROOT/$cfg"; done'
    assert any(target == "<unresolved>" for _, target in _effects(command))


def test_function_early_return_cannot_fabricate_later_binding():
    command = (
        'ROOT=/work; f(){ ROOT=/protected; return; ROOT=/work; }; '
        'for cfg in a b; do f; touch "$ROOT/$cfg"; done'
    )
    assert any(target == "<unresolved>" for _, target in _effects(command))


def test_unset_cannot_reuse_removed_function_body():
    command = 'f(){ touch known; }; unset f; for cfg in a b; do f "$cfg"; done'
    assert any(target == "<unresolved>" for _, target in _effects(command))


def test_static_loop_never_rebases_explicit_protected_target():
    assert _effects('ROOT=/protected; for cfg in a b; do touch "$ROOT/$cfg"; done') == [
        ("touch", "/protected/a"), ("touch", "/protected/b"),
    ]

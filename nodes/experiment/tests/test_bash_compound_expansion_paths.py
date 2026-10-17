"""Known shell expansions compose paths without weakening dynamic-path checks."""

from __future__ import annotations

from nodes.experiment.tools.safe_bash import _analyze_shell_path_effects


def _effects(command: str):
    return _analyze_shell_path_effects(command, "/work")


def test_two_known_expansions_inside_quoted_output_path():
    command = (
        "ROOT=/work; MODE=continuous; NAME=gentle; "
        'printf x | tee "$ROOT/logs/${MODE}_${NAME}.txt"'
    )
    assert _effects(command) == [("tee", "/work/logs/continuous_gentle.txt")]


def test_two_known_brace_expansions_without_simple_expansion_anchor():
    assert _effects('MODE=continuous; NAME=gentle; printf x | tee "${MODE}_${NAME}.txt"') == [
        ("tee", "/work/continuous_gentle.txt")
    ]


def test_compound_assignment_then_indirect_output_path():
    assert _effects(
        'MODE=continuous; NAME=gentle; OUT="${MODE}_${NAME}.txt"; printf x | tee "$OUT"'
    ) == [("tee", "/work/continuous_gentle.txt")]


def test_nested_literal_loop_with_compound_redirect_and_assignment():
    command = (
        "ROOT=/work; for NAME in gentle brisk; do "
        "for MODE in continuous checkpoint; do "
        'OUT="${MODE}_${NAME}.json"; '
        'STAMP=$(date -u +%s); '
        'python3 workload.py "$OUT" > "$ROOT/logs/${MODE}_${NAME}.stdout.txt" 2>&1; '
        "done; done"
    )
    assert _effects(command) == [
        ("redirect", f"/work/logs/{mode}_{name}.stdout.txt")
        for name in ("gentle", "brisk")
        for mode in ("continuous", "checkpoint")
    ]


def test_unsupported_parameter_expansion_is_still_unknown():
    assert ("tee", "<unresolved>") in _effects(
        'ROOT=/work; printf x | tee "${ROOT:-/protected}/out.txt"'
    )


def test_command_substitution_in_path_is_still_unknown():
    assert ("tee", "<unresolved>") in _effects(
        'ROOT=/work; printf x | tee "$ROOT/$(date +%s).txt"'
    )

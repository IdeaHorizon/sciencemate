"""Regression tests for structured verdict extraction from an experiment_log."""
from __future__ import annotations

from nodes.experiment.tools.repro_snapshot import extract_verdict_credibility


def test_explicit_inconclusive_wins_over_validated_prose():
    content = """## Hypothesis Verdict
verdict: inconclusive

No experimental data was produced, therefore no hypothesis can be validated
or refuted.

## Credibility
credibility: invalid
"""
    assert extract_verdict_credibility(content) == {
        "verdict": "inconclusive",
        "credibility": "invalid",
    }


def test_explicit_label_is_used_without_a_standard_section():
    content = """The run was stopped before execution.
verdict: inconclusive
The proposed method would have validated the claim if data existed.
credibility: questionable
"""
    assert extract_verdict_credibility(content) == {
        "verdict": "inconclusive",
        "credibility": "questionable",
    }


def test_unknown_explicit_verdict_does_not_fall_through_to_prose():
    content = """## Hypothesis Verdict
verdict: infeasible
The validated route was never executed.
"""
    assert "verdict" not in extract_verdict_credibility(content)


def test_provisional_is_a_supported_machine_verdict():
    assert extract_verdict_credibility(
        "## Verdict\nverdict: provisional\n"
    )["verdict"] == "provisional"

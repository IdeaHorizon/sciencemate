"""Regression tests for structured verdict extraction from an experiment_log."""
from __future__ import annotations

import errno

import pytest

from core.state import State
from nodes.experiment.tools import repro_snapshot
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


def test_overlong_prose_candidate_does_not_hide_later_repo(tmp_path, monkeypatch):
    """A slash in prompt prose is not allowed to abort later valid evidence."""
    state = State.new("experiment", tmp_path / "runs")
    valid_before = tmp_path / "valid-before"
    valid_after = tmp_path / "valid-after"
    valid_before.mkdir()
    valid_after.mkdir()
    overlong = "/" + ("prompt-prose" * 80)
    real_probe = repro_snapshot._git_root_near

    def _probe(candidate):
        if candidate in (str(valid_before), str(valid_after)):
            return candidate
        return real_probe(candidate)

    monkeypatch.setattr(repro_snapshot, "_git_root_near", _probe)

    diagnostics: list[dict] = []
    roots = repro_snapshot.discover_repo_paths(
        state,
        transcript_text=f"{valid_before} {overlong} {valid_after}",
        diagnostics=diagnostics,
    )

    assert roots == [str(valid_before), str(valid_after)]
    diagnostic, = diagnostics
    assert diagnostic["source"] == "text_fallback"
    assert diagnostic["error_type"] == "OSError"
    assert diagnostic["errno"] == errno.ENAMETOOLONG
    assert diagnostic["path_length"] == len(overlong)
    assert len(diagnostic["path_preview"]) <= 160
    assert overlong not in diagnostic["path_preview"]
    assert len(diagnostic["path_sha256"]) == 64


@pytest.mark.parametrize(
    ("failure", "error_type", "error_number"),
    [
        (PermissionError(errno.EACCES, "denied"), "PermissionError", errno.EACCES),
        (ValueError("invalid path value"), "ValueError", None),
    ],
)
def test_expected_candidate_probe_error_is_structured_and_isolated(
    tmp_path,
    monkeypatch,
    failure,
    error_type,
    error_number,
):
    state = State.new("experiment", tmp_path / "runs")
    bad = "/candidate-that-cannot-be-probed"
    good = "/later-valid-repo"
    monkeypatch.setattr(repro_snapshot, "_path_tokens", lambda _text: [bad, good])

    def _probe(candidate):
        if candidate == bad:
            raise failure
        return good

    monkeypatch.setattr(repro_snapshot, "_git_root_near", _probe)
    diagnostics: list[dict] = []

    roots = repro_snapshot.discover_repo_paths(
        state,
        transcript_text="unused",
        diagnostics=diagnostics,
    )

    assert roots == [good]
    assert diagnostics == [
        {
            "source": "text_fallback",
            "path_preview": bad,
            "path_length": len(bad),
            "path_sha256": repro_snapshot.hashlib.sha256(bad.encode()).hexdigest(),
            "error_type": error_type,
            "errno": error_number,
        }
    ]


def test_text_file_candidate_keeps_lexical_symlink_for_filter_and_export(tmp_path):
    target = tmp_path / "payload"
    target.write_text("value: 7\n", encoding="utf-8")
    alias = tmp_path / "input.yaml"
    alias.symlink_to(target)

    candidates = repro_snapshot._candidate_files_from_text(
        str(alias),
        repro_snapshot._is_config_like,
    )

    assert candidates == [alias]

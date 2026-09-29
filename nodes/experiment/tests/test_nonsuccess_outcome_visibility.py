"""Regression coverage for C8 non-success visibility at Core finalization."""
from __future__ import annotations

import pytest

from nodes.experiment.tests.probes.probe_c8_nonsuccess_visible import (
    _assert_existing_read_error_unchanged,
    _assert_nonsuccess_visible,
    _assert_success_unblocked,
)


@pytest.mark.parametrize(
    ("requested", "check_passed", "expected_effective"),
    [
        ("failed", False, "failed"),
        ("blocked", False, "blocked"),
        ("success", False, "partial"),
    ],
)
def test_honest_nonsuccess_operation_blocks_false_completed_summary(
    tmp_path, requested, check_passed, expected_effective,
) -> None:
    _assert_nonsuccess_visible(
        tmp_path,
        requested=requested,
        check_passed=check_passed,
        expected_effective=expected_effective,
    )


def test_true_success_remains_unblocked(tmp_path) -> None:
    _assert_success_unblocked(tmp_path)


def test_existing_submission_read_error_blocker_is_unchanged(tmp_path) -> None:
    _assert_existing_read_error_unchanged(tmp_path)

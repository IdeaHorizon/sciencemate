"""Regression coverage for the shared Markdown verdict-label parser."""
from __future__ import annotations

from pathlib import Path

from nodes.experiment.tests.probes.probe_verdict_label_forms import (
    _assert_content_gates,
    _assert_contract_combinations,
    _assert_negative_lines,
    _assert_save_preview_freeze,
    _assert_sediment_uses_same_forms,
)


def test_contract_audit_accepts_all_supported_verdict_label_forms() -> None:
    _assert_contract_combinations()


def test_verdict_label_parser_remains_line_anchored() -> None:
    _assert_negative_lines()


def test_verdict_content_gates_remain_strict(tmp_path: Path) -> None:
    _assert_content_gates(tmp_path)


def test_sediment_guard_uses_shared_verdict_label_forms(tmp_path: Path) -> None:
    _assert_sediment_uses_same_forms(tmp_path)


def test_markdown_verdict_survives_save_preview_freeze(tmp_path: Path) -> None:
    _assert_save_preview_freeze(tmp_path)

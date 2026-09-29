"""Probe #902: Markdown verdict labels are syntax, content gates remain strict."""
from __future__ import annotations

import asyncio
import itertools
import tempfile
from pathlib import Path

from core.state import State
from nodes.experiment.tools import sediment
from nodes.experiment.tools.contract_audit import (
    _has_explicit_inconclusive_reasoning,
    _has_provisional_execution_assessment,
    audit_verdict,
)
from shared.tools.library.artifacts_extra import _freeze_artifact


PREFIXES = ("", "- ", "* ")
LABEL_BOLD = (False, True)
VALUE_BOLD = (False, True)
COLONS = (":", "：")
LABEL_CASES = ("verdict", "Verdict")


def _label_form(
    value: str,
    *,
    prefix: str,
    label_bold: bool,
    value_bold: bool,
    colon: str,
    label: str,
) -> str:
    rendered_label = f"**{label}**" if label_bold else label
    rendered_value = f"**{value}**" if value_bold else value
    return f"{prefix}{rendered_label}{colon} {rendered_value}"


def _forms(value: str):
    for prefix, label_bold, value_bold, colon, label in itertools.product(
        PREFIXES,
        LABEL_BOLD,
        VALUE_BOLD,
        COLONS,
        LABEL_CASES,
    ):
        yield _label_form(
            value,
            prefix=prefix,
            label_bold=label_bold,
            value_bold=value_bold,
            colon=colon,
            label=label,
        )


def _formal_state(root: Path) -> State:
    state = State.new("experiment", root)
    state.hook_state["run_contract"] = {
        "run_role": "primary",
        "execution_mode": "scientific",
    }
    return state


def _provisional_content(label_line: str) -> str:
    return (
        "## Hypothesis Verdict\n"
        f"{label_line}\n"
        "measured metric: 1.25\n"
        "comparison against threshold: 1.00\n"
        "handoff: send measured result to Analysis\n\n"
        "## Methodological / Dead End\n"
        "未发现 methodological or dead_end finding。\n\n"
        "## Credibility\ncredibility: reliable\n"
    )


def _inconclusive_content(label_line: str) -> str:
    return (
        f"{label_line}\n"
        "reason: missing the required comparison data\n"
        "next_step: collect another measured sample\n"
    )


def _assert_contract_combinations() -> None:
    provisional = list(_forms("provisional"))
    inconclusive = list(_forms("inconclusive"))
    assert len(provisional) == 48
    assert len(inconclusive) == 48
    for line in provisional:
        assert _has_provisional_execution_assessment(
            _provisional_content(line)
        ), line
    for line in inconclusive:
        assert _has_explicit_inconclusive_reasoning(
            _inconclusive_content(line)
        ), line
    print("contract combinations: provisional=48/48 inconclusive=48/48")


def _assert_negative_lines() -> None:
    suffix = (
        "\nmeasured metric: 1\ncomparison against threshold: 0\n"
        "handoff to Analysis\n"
    )
    for line in (
        "不要写 **verdict**: provisional",
        "避免 verdict: provisional",
        "前面有字 verdict: provisional",
    ):
        assert not _has_provisional_execution_assessment(line + suffix), line
    print("negative anchored lines: 3/3")


def _assert_content_gates(root: Path) -> None:
    assert not _has_provisional_execution_assessment(
        "- **verdict**: provisional\ncomparison against threshold: 1\n"
        "handoff to Analysis\n"
    )
    assert not _has_explicit_inconclusive_reasoning(
        "- **verdict**: inconclusive\nreason: missing comparison data\n"
    )
    state = _formal_state(root / "metadata-only")
    state.save_artifact(
        "experiment_log",
        "metadata_only",
        "measured metric: 1\ncomparison against threshold: 0\n"
        "handoff to Analysis\n",
        metadata={"verdict": "provisional"},
    )
    assert audit_verdict(state)["passed"] is False
    print("content gates: 3/3")


def _assert_sediment_uses_same_forms(root: Path) -> None:
    matched = 0
    for index, line in enumerate(_forms("provisional")):
        state = State.new("experiment", root / f"case-{index:02d}")
        state.save_artifact(
            "experiment_log",
            "existing_verdict",
            _provisional_content(line),
        )
        result = asyncio.run(sediment._declare_inconclusive_verdict(
            state,
            reason="the current evidence cannot support a final conclusion",
            next_step="collect additional measurements before reassessment",
        ))
        assert result["status"] == "error", (line, result)
        assert "非 inconclusive verdict" in result["error"], (line, result)
        matched += 1
    assert matched == 48
    print("sediment combinations: 48/48")


def _assert_save_preview_freeze(root: Path) -> None:
    state = _formal_state(root / "e2e")
    saved = state.save_artifact(
        "experiment_log",
        "markdown_verdict",
        _provisional_content("- **verdict**： **provisional**"),
    )
    preview = asyncio.run(sediment._preview_experiment_contract(state))
    assert preview["checks"]["verdict"]["passed"] is True, preview
    # This probe owns only Markdown verdict parsing.  A lone unfrozen log is
    # intentionally not a complete scientific closure: result evidence,
    # immutable execution intent, and prereg assignment are separate gates.
    assert preview["overall_status"] == "incomplete", preview
    assert "verdict" not in preview["failed_checks"]
    frozen = asyncio.run(_freeze_artifact(
        state,
        saved["id"],
        "freeze Markdown-labelled scientific closure",
    ))
    assert frozen["status"] == "success", frozen
    record = state.read_artifact(saved["id"])
    assert (record.get("metadata") or {}).get("frozen") is True, record
    print("save-preview-freeze: PASS")


def main() -> None:
    failures: list[str] = []
    with tempfile.TemporaryDirectory(prefix="probe-verdict-labels-") as temp:
        root = Path(temp)
        cases = (
            ("contract_combinations", lambda: _assert_contract_combinations()),
            ("negative_lines", lambda: _assert_negative_lines()),
            ("content_gates", lambda: _assert_content_gates(root)),
            ("sediment_combinations", lambda: _assert_sediment_uses_same_forms(root)),
            ("save_preview_freeze", lambda: _assert_save_preview_freeze(root)),
        )
        for name, run in cases:
            try:
                run()
            except Exception as exc:
                failures.append(f"{name}: {type(exc).__name__}: {exc}")
                print(f"{name}: FAIL ({type(exc).__name__}: {exc})")
            else:
                print(f"{name}: PASS")
    assert not failures, failures
    print("probe_verdict_label_forms: PASS")


if __name__ == "__main__":
    main()

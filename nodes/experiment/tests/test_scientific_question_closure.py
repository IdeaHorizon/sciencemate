"""Strict current-run closure for frozen scientific questions."""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

from core.state import State
from nodes.experiment.tools.contract_audit import (
    audit_experiment_contract,
    audit_scientific_question_closure,
)
from nodes.experiment.tools.run_contract import _classify_experiment_scope


def _freeze(state: State, artifact_id: str) -> None:
    from shared.tools.library.artifacts_extra import _freeze_artifact

    result = asyncio.run(_freeze_artifact(
        state=state,
        artifact_id=artifact_id,
        reason="test frozen scientific evidence",
    ))
    assert result["status"] == "success", result


def _state_with_metric(
    tmp_path: Path, *, metric_status: str = "measured", include_statement: bool = False,
) -> State:
    state = State.new("experiment", tmp_path)
    prereg_id = state.save_artifact(
        "pre_registration",
        "scientific_question",
        """## Research Questions
### Q1: native result must answer the declared question
- output_kind: numeric
```yaml
- metric: native_energy
  comparison: <
  threshold: 0.02
```
""" + (
            """### Q2: native statement requires retained clean evidence
- output_kind: statement
```yaml
- id: NATIVE_EXECUTION
  statement: native solver execution was retained
```
""" if include_statement else ""
        ),
        metadata={
            "run_role": "primary",
            "execution_mode": "scientific",
            "stage": "simulation",
        },
    )["id"]
    state.mark_frozen(prereg_id)
    state.hook_state["node_inputs"] = {
        "prereg_artifact_id": prereg_id,
        "stage": "simulation",
        "experiment_focus": "Run the declared native solver and retain its actual output.",
    }
    classified = asyncio.run(_classify_experiment_scope(
        state,
        scope="scientific",
        reason="Run the frozen scientific method and retain native solver measurements.",
    ))
    assert classified["status"] == "success", classified

    raw_file = Path(state.root) / "native.stdout"
    raw_file.write_text("native_energy=0.01\n", encoding="utf-8")
    raw_hash = hashlib.sha256(raw_file.read_bytes()).hexdigest()
    raw_id = state.save_artifact(
        "raw_results",
        "native_output",
        json.dumps({"files": [{
            "path": str(raw_file), "sha256": raw_hash,
            "bytes": raw_file.stat().st_size, "role": "solver_stdout",
            "retention": "protected",
        }]}),
    )["id"]
    _freeze(state, raw_id)

    clean_id = state.save_artifact(
        "clean_results",
        "native_clean",
        json.dumps({
            "not_replayable": True,
            "status": "completed",
            "reason": "secondary scientific evidence for closure audit",
        }),
    )["id"]
    _freeze(state, clean_id)

    native_log = state.save_artifact(
        "experiment_log",
        "native_log",
        "## Execution Status\nstatus: completed\n\n## Credibility\ncredibility: reliable\n",
        metadata={
            "measured_metrics": {
                "native_energy": {
                    "status": metric_status,
                    "value": 0.01,
                    "raw_results_artifact_id": raw_id,
                },
            },
            "closure_discharges": (
                {"NATIVE_EXECUTION": {
                    "status": "discharged",
                    "artifact_id": clean_id,
                }} if include_statement else {}
            ),
        },
    )
    state.mark_frozen(native_log["id"])
    return state


def test_scientific_question_closure_requires_measured_current_raw_evidence(tmp_path: Path) -> None:
    state = _state_with_metric(tmp_path)

    audit = audit_scientific_question_closure(state)

    assert audit["passed"] is True, audit
    assert audit["status"] == "closed"
    assert audit["questions"][0]["closed"] is True


def test_scientific_question_closure_rejects_estimated_proxy_metric(tmp_path: Path) -> None:
    state = _state_with_metric(tmp_path, metric_status="estimated")

    audit = audit_scientific_question_closure(state)

    assert audit["passed"] is False
    assert audit["status"] == "unresolved"
    assert any("must be status=measured" in item["reason"] for item in audit["unresolved_items"])


def test_scientific_question_closure_rejects_raw_evidence_from_other_run(tmp_path: Path) -> None:
    state = _state_with_metric(tmp_path)
    log = state.read_artifact("experiment_log__native_log")
    # The artifact is frozen in a real run; use a fresh state to model a forged
    # current log receipt rather than mutating frozen evidence in place.
    other = _state_with_metric(tmp_path / "other")
    other_log_id = "experiment_log__native_log"
    other_log = other.read_artifact(other_log_id)
    forged = json.loads(json.dumps(other_log["metadata"]))
    forged["measured_metrics"]["native_energy"]["raw_results_artifact_id"] = "raw_results__foreign"
    # metadata 是账本事实：冻结版不能静默覆盖，走真实的修订通道 —— 带 amendment_reason
    # 落 v2（未冻结），再冻结 v2。head 就换成了伪造的收据，且仍是一份冻结 log。
    other.save_artifact("experiment_log", "native_log", other_log["content"], metadata=forged,
                        amendment_reason="model a forged current log receipt")
    other.mark_frozen(other_log_id)

    audit = audit_scientific_question_closure(other)

    assert log is not None
    assert audit["passed"] is False
    assert any("current frozen raw_results" in item["reason"] for item in audit["unresolved_items"])


def test_scientific_question_closure_revalidates_the_specific_cited_raw_artifact(tmp_path: Path) -> None:
    state = _state_with_metric(tmp_path)
    (Path(state.root) / "native.stdout").unlink()

    newer_file = Path(state.root) / "newer.stdout"
    newer_file.write_text("native_energy=0.02\n", encoding="utf-8")
    newer_id = state.save_artifact(
        "raw_results",
        "newer_output",
        json.dumps({"files": [{
            "path": str(newer_file),
            "sha256": hashlib.sha256(newer_file.read_bytes()).hexdigest(),
            "bytes": newer_file.stat().st_size,
            "role": "solver_stdout",
            "retention": "protected",
        }]}),
    )["id"]
    _freeze(state, newer_id)

    audit = audit_scientific_question_closure(state)

    assert audit["passed"] is False
    assert audit["status"] == "unresolved"
    assert any("retained bytes no longer verify" in item["reason"] for item in audit["unresolved_items"])


def test_scientific_question_closure_revalidates_the_specific_cited_clean_artifact(tmp_path: Path) -> None:
    state = _state_with_metric(tmp_path, include_statement=True)
    prior_clean_id = "clean_results__native_clean"
    prior_clean_path = state.find_artifact_path(prior_clean_id)
    assert prior_clean_path is not None
    # 盘上篡改冻结证据的正文（账本钉的 sha256 与文件从此不一致）。
    prior_clean_path.write_text("{}", encoding="utf-8")

    newer_clean_id = state.save_artifact(
        "clean_results",
        "newer_clean",
        json.dumps({
            "not_replayable": True,
            "status": "completed",
            "reason": "newer valid clean evidence must not mask the cited prior artifact",
        }),
    )["id"]
    _freeze(state, newer_clean_id)

    audit = audit_scientific_question_closure(state)

    assert audit["passed"] is False
    assert audit["status"] == "unresolved"
    assert any("cites evidence that no longer verifies" in item["reason"] for item in audit["unresolved_items"])


def test_scientific_question_closure_recovers_scope_but_rejects_missing_inputs(tmp_path: Path) -> None:
    state = _state_with_metric(tmp_path)
    state.hook_state.pop("experiment_execution_scope")
    state.hook_state.pop("node_inputs")

    closure = audit_scientific_question_closure(state)
    terminal = audit_experiment_contract(state)

    assert closure["passed"] is False
    assert closure["status"] == "execution_intent_binding_required"
    assert closure["intent_binding"]["status"] == "intent_unavailable"
    assert terminal["execution_intent_binding"]["passed"] is False
    assert terminal["execution_intent_binding"]["status"] == "intent_unavailable"


def test_secondary_scientific_subrun_keeps_intent_binding_without_full_question_closure(tmp_path: Path) -> None:
    state = State.new("experiment", tmp_path)
    prereg_id = state.save_artifact(
        "pre_registration",
        "secondary_evidence",
        "# Evidence-only scientific subrun\n",
        metadata={
            "run_role": "secondary",
            "execution_mode": "scientific",
            "stage": "simulation",
        },
    )["id"]
    state.mark_frozen(prereg_id)
    state.hook_state["node_inputs"] = {
        "prereg_artifact_id": prereg_id,
        "stage": "simulation",
        "experiment_focus": "Measure one declared intermediate quantity without claiming final closure.",
    }
    assert asyncio.run(_classify_experiment_scope(
        state,
        scope="scientific",
        reason="Produce evidence for the frozen target without closing the full study.",
    ))["status"] == "success"

    audit = audit_scientific_question_closure(state)

    assert audit["passed"] is True, audit
    assert audit["applicable"] is False
    assert audit["status"] == "nonterminal_scientific_subrun"
    assert audit["intent_binding"]["passed"] is True

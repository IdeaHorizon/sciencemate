#!/usr/bin/env python3
"""C4 的自包含坏基线探针：closure 三件套只能读取同一 active view。

该探针不依赖 pytest，也不触碰真实项目或服务。它用真实 ``State``、分类、
``supersede_closure_draft`` 和 ``record_operation_completion`` 复现：一对已经
被合法 supersede 的 raw/clean 草稿不应再阻止新的 operation closure 冻结。

默认模式应在修复前退出 1、修复后退出 0。``--legacy-active-view`` 只在临时
进程内将 active-view resolver 替换为旧的 current-run 读法；它是 mutation
check，必须退出 1，证明 probe 没有把断言改松。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
import traceback
from pathlib import Path
from typing import Any


REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from core.state import State
from nodes.experiment.tools import contract_audit
from nodes.experiment.tools.contract_audit import (
    _current_run_supersessions,
    _operation_clean_results_freeze_errors,
    _operation_goal_effect_audit,
    _operation_result_bundle,
    _supersede_closure_draft,
    active_closure_artifacts,
)
from nodes.experiment.tools.operation_completion import _record_operation_completion
from nodes.experiment.tools.run_contract import (
    _classify_experiment_scope,
    audit_execution_intent_binding,
)


TRIPLET_TYPES = ("raw_results", "clean_results", "experiment_log")


def _await(coroutine: Any) -> Any:
    return asyncio.run(coroutine)


def _classify_operation(state: State) -> None:
    state.hook_state["node_inputs"] = {
        "experiment_focus": "Verify a bounded operation closure.",
        "prereg_assignment": {
            "kind": "none",
            "reason": "This active-view probe has no governing preregistration.",
        },
    }
    result = _await(_classify_experiment_scope(
        state,
        scope="operation",
        operation_category="format_validation",
        reason="Mechanical closure verification; no scientific conclusion.",
    ))
    assert result["status"] == "success", result


def _record_completion(state: State) -> dict[str, Any]:
    evidence = Path(state.root) / "probe.stdout"
    evidence.write_text("returncode=0\n", encoding="utf-8")
    return _await(_record_operation_completion(
        state,
        task_kind="generic",
        objective="verify bounded operation closure",
        outcome="success",
        checks=[{
            "name": "returncode",
            "passed": True,
            "evidence": {"returncode": 0},
        }],
        artifact_paths=[str(evidence)],
    ))


def _seed_and_supersede_preclassification_drafts(state: State) -> dict[str, str]:
    state.hook_state["node_inputs"] = {
        "experiment_focus": "Verify a bounded operation closure.",
        "prereg_assignment": {
            "kind": "none",
            "reason": "This correction probe has no governing preregistration.",
        },
    }
    scientific = _await(_classify_experiment_scope(
        state,
        scope="scientific",
        reason="Initially misclassified as a scientific result.",
    ))
    assert scientific["status"] == "success", scientific

    raw_id = state.save_artifact(
        "raw_results", "stale_raw", json.dumps({"files": []}),
    )["id"]
    clean_id = state.save_artifact(
        "clean_results", "stale_clean", json.dumps({
            "raw_results_artifact_id": raw_id,
            "rows": [],
        }),
    )["id"]
    log_id = state.save_artifact(
        "experiment_log", "stale_log",
        "## Execution\ncommand: stale draft\nverification: stale draft\n",
    )["id"]

    operational = _await(_classify_experiment_scope(
        state,
        scope="operation",
        operation_category="toolchain_build",
        reason="It is only mechanical build verification.",
    ))
    assert operational["status"] == "success", operational

    pair = _await(_supersede_closure_draft(
        state, clean_id, reason="pre-reclassification evidence draft",
    ))
    assert pair["status"] == "success", pair
    assert pair.get("linked_superseded_id") == raw_id, pair
    log = _await(_supersede_closure_draft(
        state, log_id, reason="pre-reclassification log draft",
    ))
    assert log["status"] == "success", log
    return {
        "raw_results": raw_id,
        "clean_results": clean_id,
        "experiment_log": log_id,
    }


def _inject_superseded_drafts(state: State) -> dict[str, str]:
    raw_id = state.save_artifact(
        "raw_results", "superseded_raw", json.dumps({"files": []}),
    )["id"]
    clean_id = state.save_artifact(
        "clean_results", "superseded_clean", json.dumps({
            "raw_results_artifact_id": raw_id,
            "rows": [],
        }),
    )["id"]
    log_id = state.save_artifact(
        "experiment_log", "superseded_log",
        "## Execution\ncommand: stale draft\nverification: stale draft\n",
    )["id"]
    pair = _await(_supersede_closure_draft(
        state, clean_id, reason="fixture stale evidence pair",
    ))
    assert pair["status"] == "success", pair
    assert pair.get("linked_superseded_id") == raw_id, pair
    log = _await(_supersede_closure_draft(
        state, log_id, reason="fixture stale log",
    ))
    assert log["status"] == "success", log
    return {
        "raw_results": raw_id,
        "clean_results": clean_id,
        "experiment_log": log_id,
    }


def _forge_frozen_raw_supersession(
    state: State,
    *,
    canonical_raw_id: str,
) -> tuple[str, str]:
    """Construct the invalid ledger fact public supersession correctly rejects."""
    canonical = state.read_artifact(canonical_raw_id)
    assert isinstance(canonical, dict)
    extra = state.save_artifact(
        "raw_results",
        "forged_frozen_raw",
        str(canonical["content"]),
        metadata=dict(canonical.get("metadata") or {}),
    )
    extra_id = str(extra["id"])
    state.mark_frozen(extra_id)
    payload = json.dumps({
        "superseded_id": extra_id,
        "artifact_type": "raw_results",
        "reason": "adversarial frozen-evidence supersession fixture",
        "run_id": state.run_id,
    }, sort_keys=True)
    supersession = state.save_artifact(
        "experiment_log_supersession",
        "forged_frozen_raw_supersession",
        payload,
        metadata={
            "superseded_id": extra_id,
            "artifact_type": "raw_results",
        },
    )
    supersession_id = str(supersession["id"])
    state.mark_frozen(supersession_id)
    return extra_id, supersession_id


def _assert_consumers_see_one_active_triplet(
    state: State,
    completion: dict[str, Any],
    superseded: dict[str, str] | None,
) -> dict[str, Any]:
    expected_ids = {
        "raw_results": str(completion["raw_results_artifact_id"]),
        "clean_results": str(completion["clean_results_artifact_id"]),
        "experiment_log": str(completion["experiment_log_artifact_id"]),
    }
    active_summary: dict[str, Any] = {}
    for artifact_type in TRIPLET_TYPES:
        active, hidden = active_closure_artifacts(state, artifact_type)
        active_ids = [str(item.get("id") or "") for item in active]
        assert active_ids == [expected_ids[artifact_type]], {
            "artifact_type": artifact_type,
            "expected": expected_ids[artifact_type],
            "active": active_ids,
            "hidden": hidden,
        }
        expected_hidden = [] if superseded is None else [superseded[artifact_type]]
        assert hidden == expected_hidden, {
            "artifact_type": artifact_type,
            "expected_hidden": expected_hidden,
            "hidden": hidden,
        }
        active_summary[artifact_type] = {"active": active_ids, "hidden": hidden}

    clean_record = state.read_artifact(expected_ids["clean_results"])
    assert isinstance(clean_record, dict)
    freeze_errors = _operation_clean_results_freeze_errors(state, clean_record)
    assert freeze_errors == [], freeze_errors

    bundle = _operation_result_bundle(state)
    assert bundle["passed"] is True, bundle
    assert bundle["artifacts"]["raw_results"]["count"] == 1, bundle
    assert bundle["artifacts"]["clean_results"]["count"] == 1, bundle

    binding = audit_execution_intent_binding(state, require=False)
    goal_effect = _operation_goal_effect_audit(state, intent_binding=binding)
    assert goal_effect["passed"] is True, goal_effect
    for artifact_type in TRIPLET_TYPES:
        row = goal_effect["artifacts"][artifact_type]
        assert row.get("artifact_id") == expected_ids[artifact_type], row
        assert row.get("upstream_goal_effect") == "operational_subtask_only", row
        assert row.get("binding_status") == binding.get("status"), row

    return {
        "active": active_summary,
        "bundle": bundle,
        "goal_effect": goal_effect,
    }


def _recovery_lane(root: Path) -> dict[str, Any]:
    state = State.new("experiment", root)
    stale = _seed_and_supersede_preclassification_drafts(state)
    completion = _record_completion(state)

    raw_id = str(completion.get("raw_results_artifact_id") or "")
    raw_record = state.read_artifact(raw_id) if raw_id else None
    raw_frozen = bool(((raw_record or {}).get("metadata") or {}).get("frozen"))
    active_clean, hidden_clean = active_closure_artifacts(state, "clean_results")
    generated_clean = [
        state.read_artifact(str(item.get("id") or "")) for item in active_clean
    ]
    return {
        "completion": completion,
        "stale": stale,
        "raw_frozen": raw_frozen,
        "active_clean_ids": [str(item.get("id") or "") for item in active_clean],
        "hidden_clean_ids": hidden_clean,
        "active_clean_frozen": [
            bool(((record or {}).get("metadata") or {}).get("frozen"))
            for record in generated_clean
        ],
    }


def _consumer_lane(root: Path, *, inject: bool) -> dict[str, Any]:
    state = State.new("experiment", root)
    _classify_operation(state)
    completion = _record_completion(state)
    assert completion["status"] == "success", completion
    superseded = _inject_superseded_drafts(state) if inject else None
    report = _assert_consumers_see_one_active_triplet(
        state, completion, superseded,
    )
    return {"completion": completion, "superseded": superseded, **report}


def _frozen_evidence_lane(root: Path) -> dict[str, Any]:
    """A forged negation cannot hide a second frozen raw evidence record."""
    state = State.new("experiment", root)
    _classify_operation(state)
    completion = _record_completion(state)
    assert completion["status"] == "success", completion
    canonical_raw_id = str(completion["raw_results_artifact_id"])
    extra_raw_id, supersession_id = _forge_frozen_raw_supersession(
        state,
        canonical_raw_id=canonical_raw_id,
    )
    recognized = _current_run_supersessions(state).get(extra_raw_id)
    assert recognized and recognized["supersession_id"] == supersession_id, recognized
    assert recognized["artifact_type"] == "raw_results", recognized

    active, hidden = contract_audit.active_closure_artifacts(state, "raw_results")
    active_ids = [str(item.get("id") or "") for item in active]
    assert active_ids == [canonical_raw_id, extra_raw_id], {
        "expected": [canonical_raw_id, extra_raw_id],
        "active": active_ids,
        "hidden": hidden,
    }
    assert hidden == [], hidden
    clean_record = state.read_artifact(str(completion["clean_results_artifact_id"]))
    assert isinstance(clean_record, dict)
    errors = _operation_clean_results_freeze_errors(state, clean_record)
    assert "operation clean_results requires exactly one current-run raw_results" in errors, errors
    bundle = _operation_result_bundle(state)
    assert bundle["passed"] is False, bundle
    assert bundle["artifacts"]["raw_results"]["count"] == 2, bundle
    goal_effect = _operation_goal_effect_audit(
        state,
        intent_binding=audit_execution_intent_binding(state, require=False),
    )
    assert goal_effect["artifacts"]["raw_results"] == {
        "count": 2,
        "status": "unavailable",
    }, goal_effect
    return {
        "completion": completion,
        "canonical_raw_id": canonical_raw_id,
        "extra_raw_id": extra_raw_id,
        "supersession_id": supersession_id,
        "bundle": bundle,
        "goal_effect": goal_effect,
    }


def _legacy_active_view(state: Any, artifact_type: str) -> tuple[list[dict[str, Any]], list[str]]:
    """Mutation-only legacy resolver: old consumers counted all current artifacts."""
    return contract_audit.current_run_artifacts(state, artifact_type), []


def _unsafe_hide_frozen_superseded(
    state: Any,
    artifact_type: str,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Mutation-only resolver with only the frozen-evidence guard removed."""
    if artifact_type not in TRIPLET_TYPES:
        return contract_audit.current_run_artifacts(state, artifact_type), []
    supersessions = _current_run_supersessions(state)
    active: list[dict[str, Any]] = []
    hidden: list[str] = []
    for item in contract_audit.current_run_artifacts(state, artifact_type):
        artifact_id = str(item.get("id") or "")
        supersession = supersessions.get(artifact_id)
        if supersession and supersession.get("artifact_type") == artifact_type:
            hidden.append(artifact_id)
            continue
        active.append(item)
    return active, hidden


def _run_lane(name: str, fn: Any, failures: list[dict[str, str]]) -> dict[str, Any]:
    try:
        return fn()
    except Exception as exc:  # deliberately collect all lanes before assigning exit code
        failures.append({
            "lane": name,
            "exception": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc(),
        })
        return {"error": failures[-1]}


def main() -> int:
    parser = argparse.ArgumentParser()
    mutations = parser.add_mutually_exclusive_group()
    mutations.add_argument(
        "--legacy-active-view",
        action="store_true",
        help="mutation check: replace the resolver only in this probe process",
    )
    mutations.add_argument(
        "--unsafe-hide-frozen-superseded",
        action="store_true",
        help="mutation check: remove only the frozen-evidence guard in this process",
    )
    args = parser.parse_args()
    failures: list[dict[str, str]] = []
    reports: dict[str, Any] = {"mode": (
        "legacy" if args.legacy_active_view
        else "unsafe_hide_frozen_superseded" if args.unsafe_hide_frozen_superseded
        else "candidate"
    )}

    with tempfile.TemporaryDirectory(prefix="c4-closure-active-view-") as temp_dir:
        root = Path(temp_dir)
        reports["recovery"] = _run_lane(
            "recovery", lambda: _recovery_lane(root / "recovery"), failures,
        )

        if not args.legacy_active_view:
            recovery = reports["recovery"]
            completion = recovery.get("completion") if isinstance(recovery, dict) else None
            if not isinstance(completion, dict) or completion.get("status") != "success":
                failures.append({
                    "lane": "recovery",
                    "exception": "C4 requires a superseded draft not to block operation completion",
                    "traceback": "",
                })
            if not isinstance(recovery, dict) or recovery.get("raw_frozen") is not True:
                failures.append({
                    "lane": "recovery",
                    "exception": "the reproduction did not reach raw-results freeze",
                    "traceback": "",
                })

        original = contract_audit.active_closure_artifacts
        if args.legacy_active_view:
            contract_audit.active_closure_artifacts = _legacy_active_view
        elif args.unsafe_hide_frozen_superseded:
            contract_audit.active_closure_artifacts = _unsafe_hide_frozen_superseded
        try:
            reports["normal_control"] = _run_lane(
                "normal_control",
                lambda: _consumer_lane(root / "normal", inject=False),
                failures,
            )
            reports["superseded_control"] = _run_lane(
                "superseded_control",
                lambda: _consumer_lane(root / "superseded", inject=True),
                failures,
            )
            reports["frozen_evidence_control"] = _run_lane(
                "frozen_evidence_control",
                lambda: _frozen_evidence_lane(root / "frozen_evidence"),
                failures,
            )
        finally:
            contract_audit.active_closure_artifacts = original

    reports["failures"] = [
        {key: value for key, value in item.items() if key != "traceback"}
        for item in failures
    ]
    print(json.dumps(reports, ensure_ascii=False, indent=2, sort_keys=True))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

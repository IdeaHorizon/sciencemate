"""Action-scoped evidence of the task and inputs seen at execution entry.

This is a read-only projection, stored by the existing action/job writer. It is
neither an authorization receipt nor an inference about what arbitrary code
actually read. Tool parameter arguments remain explicitly labelled witnesses.
"""
from __future__ import annotations

import copy
from typing import Any


def capture(
    state: Any, *, execution_params: Any = None,
    input_observation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from . import ledger, run_bootstrap
    from .preflight import compare_execution_parameters
    from .run_contract import _resolve_exact_frozen_prereg

    records = ledger.open_ledger(state).records()
    authority = run_bootstrap.project_run_authority(records)
    # Admission/bootstrap own readiness. This evidence reader must not invent
    # another lifecycle gate; incomplete low-level contexts remain explicit.
    authority_available = isinstance(authority, dict)
    authority = authority if authority_available else {}
    binding = authority.get("prereg")
    context = {
        "authority_status": "recorded" if authority_available else "unavailable",
        "authority_seq": max((row["seq"] for row in records if row.get("kind") in {
            "run_bootstrap", "run_authority_refreshed", "scope_resolved", "startup_blocker_recorded",
        }), default=0),
        "task_contract": authority.get("task_contract"),
        "prereg_status": authority.get("prereg_status", "unavailable"),
        "prereg_binding": binding,
        "execution_params": execution_params,
        "parameter_source": "tool_argument_witness" if execution_params is not None else "not_observed",
        "input_verification": input_observation or {"status": "not_selected"},
    }
    if execution_params is None:
        comparison = {"applicable": False, "passed": None, "reason": "no parameter claim in this action"}
    elif not isinstance(binding, dict):
        comparison = {"applicable": False, "passed": None, "reason": "no bound prereg to compare"}
    else:
        resolved = _resolve_exact_frozen_prereg(state, **{
            "artifact_id": binding["artifact_id"], "version": binding["version"],
            "expected_content_hash": binding["content_hash"],
        })
        if not resolved.get("passed"):
            comparison = {"applicable": True, "passed": False,
                          "reason": "bound prereg unavailable at action entry", "binding_check": resolved}
        else:
            comparison = {"applicable": True,
                          **compare_execution_parameters(resolved["metadata"], execution_params)}
    context["parameter_comparison"] = comparison
    # A later mutation of caller dictionaries must not change this observation.
    return copy.deepcopy(context)

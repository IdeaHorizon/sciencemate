"""One registry must drive every scientific terminal-closure projection."""
from __future__ import annotations

import asyncio
import importlib
import json
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from core import data_provenance
from core.loop_hooks import HookContext
from core.state import State
from nodes.experiment import hooks
from nodes.experiment.tools import contract_audit, resource_manager, sediment

runtime_contract_audit = importlib.import_module("tools.contract_audit")
runtime_resource_manager = importlib.import_module("tools.resource_manager")


_TERMINAL_KEYS = {
    "verdict": "experiment_verdict_audit",
    "sediment": "experiment_sediment_audit",
    "experiment_log_integrity": "experiment_log_integrity_audit",
    "result_evidence": "experiment_result_evidence_audit",
    "citation_binding": "experiment_citation_binding_audit",
    "execution_intent_binding": "experiment_execution_intent_audit",
    "scientific_question_closure": (
        "experiment_scientific_question_closure_audit"
    ),
    "prereg_assignment": "experiment_prereg_assignment_audit",
    "data_provenance": "experiment_data_provenance_audit",
    "job_submission_records_readable": (
        "experiment_job_submission_records_readable_audit"
    ),
}
_SENTINEL_AUDIT_KEY = "sentinel_terminal_gate"
_SENTINEL_EVENT_KEY = "experiment_sentinel_terminal_audit"


def _state(tmp_path: Path, name: str) -> State:
    return State.new("experiment", tmp_path / name)


def _events(state: State) -> list[dict[str, Any]]:
    if not state.transcript_path.exists():
        return []
    return [
        json.loads(line)
        for line in state.transcript_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _audit_payload(
    *,
    failed: set[str] | frozenset[str] = frozenset(),
    include_sentinel: bool = False,
) -> dict[str, dict[str, Any]]:
    keys = [*_TERMINAL_KEYS]
    if include_sentinel:
        keys.append(_SENTINEL_AUDIT_KEY)
    payload = {
        key: {
            "passed": key not in failed,
            "applicable": True,
            "status": "failed" if key in failed else "passed",
            "reason": f"{key} {'failed' if key in failed else 'passed'}",
        }
        for key in keys
    }
    payload["verdict"]["late_declaration"] = False
    payload["sediment"]["late_declaration"] = False
    payload.update({
        # These are visible auxiliary audits, not terminal closure gates.
        "execution_record": {
            "passed": True,
            "required": False,
            "reason": "optional KB registration is not a terminal gate",
        },
        "terminal_failure_record": {
            "passed": True,
            "applicable": False,
            "reason": "no terminal failure record is required",
        },
    })
    return payload


def _patch_preview_diagnostics(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        runtime_resource_manager,
        "current_run_owed_external_workflows",
        lambda _state: [],
    )


def _preview(
    state: State,
    monkeypatch: pytest.MonkeyPatch,
    payload: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    _patch_preview_diagnostics(monkeypatch)
    monkeypatch.setattr(
        sediment, "audit_experiment_contract", lambda _state: payload,
    )
    return asyncio.run(sediment._preview_experiment_contract(state))


def _advisor(
    state: State,
    monkeypatch: pytest.MonkeyPatch,
    payload: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    monkeypatch.setattr(hooks, "_latest_experiment_log_is_frozen", lambda _state: True)
    monkeypatch.setattr(
        hooks, "audit_experiment_contract", lambda _state: payload,
    )
    hooks.sediment_closure_advisor_on_turn_end(
        HookContext(harness=None, state=state, messages=[], turn=1),
    )
    previews = [
        event for event in _events(state)
        if event.get("event") == "experiment_contract_preview"
    ]
    return previews[-1] if previews else None


def _normal_on_end(
    state: State,
    monkeypatch: pytest.MonkeyPatch,
    payload: dict[str, dict[str, Any]],
) -> SimpleNamespace:
    monkeypatch.setattr(hooks, "_is_operational_run", lambda _state: False)
    monkeypatch.setattr(
        hooks, "audit_experiment_contract", lambda _state: payload,
    )
    result = SimpleNamespace(status="completed", final_text="model claimed completion")
    hooks.experiment_contract_audit_on_end(
        HookContext(harness=None, state=state, messages=[], turn=1), result,
    )
    return result


def _exception_on_end(
    state: State,
    monkeypatch: pytest.MonkeyPatch,
) -> SimpleNamespace:
    def audit_boom(_state: State) -> dict[str, dict[str, Any]]:
        raise RuntimeError("sentinel audit failure")

    monkeypatch.setattr(hooks, "_is_operational_run", lambda _state: False)
    monkeypatch.setattr(hooks, "audit_experiment_contract", audit_boom)
    result = SimpleNamespace(status="completed", final_text="model claimed completion")
    hooks.experiment_contract_audit_on_end(
        HookContext(harness=None, state=state, messages=[], turn=1), result,
    )
    return result


def _registry() -> Mapping[str, str]:
    registry = contract_audit.TERMINAL_CLOSURE_REGISTRY
    assert isinstance(registry, Mapping)
    assert all(isinstance(key, str) and isinstance(value, str)
               for key, value in registry.items())
    return registry


def _inject_sentinel(monkeypatch: pytest.MonkeyPatch) -> None:
    for module in {contract_audit, runtime_contract_audit}:
        registry = getattr(module, "TERMINAL_CLOSURE_REGISTRY")
        monkeypatch.setattr(module, "TERMINAL_CLOSURE_REGISTRY", {
            **dict(registry),
            _SENTINEL_AUDIT_KEY: _SENTINEL_EVENT_KEY,
        })


def _terminal_event_names(state: State, event_keys: set[str]) -> set[str]:
    return {
        str(event.get("event"))
        for event in _events(state)
        if event.get("event") in event_keys
    }


def _blocked_checks(state: State) -> set[str]:
    blocked = state.hook_state.get("experiment_downstream_blocked") or {}
    return {str(item) for item in blocked.get("failed_checks") or []}


def test_terminal_closure_registry_has_one_unique_canonical_mapping() -> None:
    registry = _registry()

    assert dict(registry) == _TERMINAL_KEYS
    assert len(set(registry)) == len(registry)
    assert len(set(registry.values())) == len(registry)
    assert set(registry.values()).issubset(
        set(hooks.experiment_contract_audit.emits)
    )


def test_four_consumers_project_the_same_terminal_key_set(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected_audit_keys = set(_TERMINAL_KEYS)
    expected_event_keys = set(_TERMINAL_KEYS.values())
    payload = _audit_payload(failed=expected_audit_keys)

    preview_state = _state(tmp_path, "preview")
    preview = _preview(preview_state, monkeypatch, payload)

    advisor_state = _state(tmp_path, "advisor")
    advisor = _advisor(advisor_state, monkeypatch, payload)

    normal_state = _state(tmp_path, "normal")
    _normal_on_end(normal_state, monkeypatch, payload)

    exception_state = _state(tmp_path, "exception")
    _exception_on_end(exception_state, monkeypatch)

    observed = {
        "preview": set(preview.get("checks") or {}),
        "turn_end_advisor": set((advisor or {}).get("failed_checks") or []),
        "normal_on_end_events": _terminal_event_names(
            normal_state, expected_event_keys,
        ),
        "normal_on_end_blocker": _blocked_checks(normal_state),
        "exception_events": _terminal_event_names(
            exception_state, expected_event_keys,
        ),
        "exception_blocker": _blocked_checks(exception_state),
    }

    assert observed == {
        "preview": expected_audit_keys,
        "turn_end_advisor": expected_audit_keys,
        "normal_on_end_events": expected_event_keys,
        "normal_on_end_blocker": expected_event_keys,
        "exception_events": expected_event_keys,
        "exception_blocker": expected_event_keys,
    }
    assert "open_external_jobs" not in expected_audit_keys
    assert "experiment_terminal_closure_persistence_audit" not in expected_event_keys


@pytest.mark.parametrize(
    ("mode", "expected_passed", "expected_status"),
    [
        ("enforce", False, "failed"),
        ("warn", True, "warning"),
    ],
)
def test_data_provenance_policy_is_one_registered_audit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    expected_passed: bool,
    expected_status: str,
) -> None:
    state = _state(tmp_path, f"provenance-{mode}")
    monkeypatch.setenv("HARNESS_PROVENANCE_GATE", mode)
    monkeypatch.setattr(
        data_provenance,
        "undeclared_stale_inputs",
        lambda _state: [{"path": "/external/input.dat"}],
    )

    check = contract_audit.audit_data_provenance(state)

    assert check["passed"] is expected_passed
    assert check["status"] == expected_status
    assert check["unverified"] == [{"path": "/external/input.dat"}]


def test_data_provenance_exception_fails_closed_even_in_warn_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _state(tmp_path, "provenance-error")
    monkeypatch.setenv("HARNESS_PROVENANCE_GATE", "warn")

    def provenance_boom(_state: State) -> list[dict[str, Any]]:
        raise OSError("provenance store unavailable")

    monkeypatch.setattr(
        data_provenance, "undeclared_stale_inputs", provenance_boom,
    )

    check = contract_audit.audit_data_provenance(state)

    assert check["passed"] is False
    assert check["status"] == "audit_error"
    assert "OSError: provenance store unavailable" in check["reason"]


def test_submission_ledger_readability_is_registered_without_open_job_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _state(tmp_path, "ledger-unreadable")

    def ledger_boom(_state: State) -> list[dict[str, Any]]:
        raise resource_manager.SubmissionLedgerError("receipt body is missing")

    expected = {
        "passed": False,
        "reason": (
            "authoritative job_submission records unavailable: "
            "receipt body is missing"
        ),
    }
    for audit_module, manager_module in (
        (contract_audit, resource_manager),
        (runtime_contract_audit, runtime_resource_manager),
    ):
        monkeypatch.setattr(
            manager_module, "owed_external_job_closure_records", ledger_boom,
        )
        assert audit_module.audit_job_submission_records_readable(state) == expected
    assert "open_external_jobs" not in contract_audit.TERMINAL_CLOSURE_REGISTRY


def test_preview_does_not_requery_registered_provenance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _state(tmp_path, "preview-provenance-single-source")
    payload = _audit_payload(failed={"data_provenance"})
    payload["data_provenance"]["unverified"] = [{"path": "/stale/input"}]
    monkeypatch.setattr(
        data_provenance,
        "undeclared_stale_inputs",
        lambda _state: pytest.fail("preview must consume the registered audit"),
    )

    preview = _preview(state, monkeypatch, payload)

    assert preview["failed_checks"] == ["data_provenance"]
    assert preview["unverified"] == [{"path": "/stale/input"}]


@pytest.mark.parametrize(
    ("provenance_passed", "expected_blocked"),
    [(True, False), (False, True)],
)
def test_freeze_gate_consumes_registered_provenance_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    provenance_passed: bool,
    expected_blocked: bool,
) -> None:
    state = _state(tmp_path, f"freeze-provenance-{provenance_passed}")
    artifact_id = "experiment_log__canonical"
    payload = _audit_payload(
        failed=set() if provenance_passed else {"data_provenance"},
    )
    payload["experiment_log_integrity"].update({
        "canonical_log_id": artifact_id,
    })
    payload["data_provenance"].update({
        "status": "warning" if provenance_passed else "failed",
        "reason": "stale inputs were observed",
    })
    monkeypatch.setattr(
        contract_audit,
        "load_run_contract",
        lambda _state: {"execution_mode": "scientific"},
    )
    monkeypatch.setattr(
        contract_audit, "audit_experiment_contract", lambda _state: payload,
    )
    monkeypatch.setattr(
        contract_audit,
        "_experiment_log_correction_disclosure_failures",
        lambda _state, _record: {},
    )

    result = contract_audit._experiment_log_freeze_gate(
        state, artifact_id, {"metadata": {}},
    )

    assert ("data_provenance" in result["failures"]) is expected_blocked


def test_injected_failed_sentinel_reaches_and_fails_all_four_consumers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _inject_sentinel(monkeypatch)
    payload = _audit_payload(
        failed={_SENTINEL_AUDIT_KEY},
        include_sentinel=True,
    )

    preview_state = _state(tmp_path, "sentinel-preview")
    preview = _preview(preview_state, monkeypatch, payload)

    advisor_state = _state(tmp_path, "sentinel-advisor")
    advisor = _advisor(advisor_state, monkeypatch, payload)

    normal_state = _state(tmp_path, "sentinel-normal")
    normal_result = _normal_on_end(normal_state, monkeypatch, payload)

    exception_state = _state(tmp_path, "sentinel-exception")
    exception_result = _exception_on_end(exception_state, monkeypatch)

    preview_check = (preview.get("checks") or {}).get(_SENTINEL_AUDIT_KEY) or {}
    observed = {
        "preview": (
            _SENTINEL_AUDIT_KEY in set(preview.get("failed_checks") or [])
            and preview_check.get("passed") is False
        ),
        "turn_end_advisor": (
            _SENTINEL_AUDIT_KEY
            in set((advisor or {}).get("failed_checks") or [])
        ),
        "normal_on_end": (
            _SENTINEL_EVENT_KEY in _terminal_event_names(
                normal_state, {_SENTINEL_EVENT_KEY},
            )
            and _SENTINEL_EVENT_KEY in _blocked_checks(normal_state)
            and normal_result.status == "blocked"
        ),
        "exception_fallback": (
            _SENTINEL_EVENT_KEY in _terminal_event_names(
                exception_state, {_SENTINEL_EVENT_KEY},
            )
            and _SENTINEL_EVENT_KEY in _blocked_checks(exception_state)
            and exception_result.status == "blocked"
        ),
    }

    assert observed == {
        "preview": True,
        "turn_end_advisor": True,
        "normal_on_end": True,
        "exception_fallback": True,
    }


def test_normal_on_end_persists_result_evidence_and_citation_events(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _state(tmp_path, "normal-event-completeness")
    _normal_on_end(state, monkeypatch, _audit_payload())
    names = {str(event.get("event")) for event in _events(state)}

    assert "experiment_result_evidence_audit" in names
    assert "experiment_citation_binding_audit" in names


def test_audit_exception_fallback_includes_result_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _state(tmp_path, "exception-result-evidence")
    result = _exception_on_end(state, monkeypatch)
    names = {str(event.get("event")) for event in _events(state)}

    assert result.status == "blocked"
    assert "experiment_result_evidence_audit" in names
    assert "experiment_result_evidence_audit" in _blocked_checks(state)


# ── P0a v3 M2（Codex 复审 17 号）：sentinel 不只一种形状 ─────────────────────────
#
# 交付的 sentinel 测试只喂 status="failed", applicable=True 一种 payload；消费者若按
# status != "audit_missing" 或按 applicable 私有过滤，0 条红。这两格补上：
#   (a) 注册了但**没人供给**（reducer 的 audit_missing 兜底）→ 四处都必须当失败；
#   (b) 供给了但 applicable=False, passed=True → 四处都必须**看见**它、且都不把它当失败。

def test_registered_but_unsupplied_sentinel_fails_all_four_consumers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _inject_sentinel(monkeypatch)
    payload = _audit_payload()  # 没有 sentinel 条目 → audit_missing

    preview = _preview(_state(tmp_path, "missing-preview"), monkeypatch, payload)
    advisor = _advisor(_state(tmp_path, "missing-advisor"), monkeypatch, payload)
    normal_state = _state(tmp_path, "missing-normal")
    normal_result = _normal_on_end(normal_state, monkeypatch, payload)
    exception_state = _state(tmp_path, "missing-exception")
    exception_result = _exception_on_end(exception_state, monkeypatch)

    observed = {
        "preview": _SENTINEL_AUDIT_KEY in set(preview.get("failed_checks") or []),
        "turn_end_advisor": _SENTINEL_AUDIT_KEY in set((advisor or {}).get("failed_checks") or []),
        "normal_on_end": (
            _SENTINEL_EVENT_KEY in _blocked_checks(normal_state)
            and normal_result.status == "blocked"
        ),
        "exception_fallback": (
            _SENTINEL_EVENT_KEY in _blocked_checks(exception_state)
            and exception_result.status == "blocked"
        ),
    }
    assert observed == {
        "preview": True, "turn_end_advisor": True,
        "normal_on_end": True, "exception_fallback": True,
    }


def test_supplied_inapplicable_sentinel_is_seen_but_not_failed_by_all_four(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _inject_sentinel(monkeypatch)
    payload = _audit_payload(include_sentinel=True)
    payload[_SENTINEL_AUDIT_KEY] = {
        "passed": True, "applicable": False,
        "status": "not_applicable", "reason": "sentinel not applicable to this run",
    }

    preview = _preview(_state(tmp_path, "na-preview"), monkeypatch, payload)
    advisor = _advisor(_state(tmp_path, "na-advisor"), monkeypatch, payload)
    normal_state = _state(tmp_path, "na-normal")
    normal_result = _normal_on_end(normal_state, monkeypatch, payload)

    # 看得见：preview 的 checks 与正常 on_end 的 durable 事件里都有它
    assert _SENTINEL_AUDIT_KEY in (preview.get("checks") or {})
    assert _SENTINEL_EVENT_KEY in _terminal_event_names(normal_state, {_SENTINEL_EVENT_KEY})
    # 但没有任何一处把"不适用且通过"当失败
    assert _SENTINEL_AUDIT_KEY not in set(preview.get("failed_checks") or [])
    assert _SENTINEL_AUDIT_KEY not in set((advisor or {}).get("failed_checks") or [])
    assert _SENTINEL_EVENT_KEY not in _blocked_checks(normal_state)
    assert normal_result.status != "blocked" or _SENTINEL_EVENT_KEY not in _blocked_checks(normal_state)

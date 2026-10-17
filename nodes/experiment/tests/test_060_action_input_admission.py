"""Action-local formal-input admission at the three physical entry points."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from core.state import State
from nodes.experiment.tools import contract_audit
from nodes.experiment.tools import input_admission
from nodes.experiment.tools import input_delivery
from nodes.experiment.tools import ledger
from nodes.experiment.tools import resource_manager as rm
from nodes.experiment.tools import run_bootstrap
from nodes.experiment.tools import safe_bash


def _v3_state(tmp_path: Path) -> State:
    state = State.new(
        node_type="experiment",
        base_dir=tmp_path / "runs",
        project_id="p",
        session_id="owner",
    )
    state.hook_state["node_inputs"] = {
        "prereg_assignment": {"kind": "none", "reason": "local preparation"},
        "scope": "operation",
        "operation_category": "toolchain_build",
        "requested_work": "prepare and run a local action",
        "required_outputs": [],
    }
    asyncio.run(run_bootstrap.establish(state))
    assert ledger.bootstrap_phase(state) == "ready"
    return state


def _service_spec(asset_name: str, *, purpose: str) -> str:
    return json.dumps({
        "request_kind": "preprocessing_service_request",
        "requesting_stage": "toolchain_build",
        "purpose": purpose,
        "target_software": "test solver",
        "scientific_parameters": "not_applicable",
        "required_assets": [{
            "name": asset_name,
            "format": "text",
            "purpose": "solver input",
        }],
        "acceptance": {
            "file_exists": True,
            "schema": True,
            "units": True,
            "manifest_lineage": True,
        },
    })


def _deliver_verified_input(
    tmp_path: Path,
    state: State,
    *,
    spec_id: str,
    asset_name: str,
) -> str:
    package = tmp_path / "verified-input-package"
    package.mkdir()
    (package / asset_name).write_text("verified input\n", encoding="utf-8")
    manifest = package / "manifest.json"
    manifest.write_text(
        json.dumps({"lineage": [{"op": "prepare_input"}]}),
        encoding="utf-8",
    )
    dataset_id = state.save_artifact(
        "dataset",
        "verified-input",
        json.dumps({
            "package_dir": str(package),
            "manifest_path": str(manifest),
            "lineage": [{"op": "prepare_input"}],
            "downstream_contract": {"files": [asset_name]},
        }),
    )["id"]
    verified = asyncio.run(contract_audit._verify_dataset_consumption(
        state, dataset_id, spec_id,
    ))
    assert verified["status"] == "success", verified
    return dataset_id


def _install_audit(
    monkeypatch: pytest.MonkeyPatch,
    result: dict[str, Any] | BaseException,
) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def fake_audit(
        state: State,
        input_package_artifact_id: str | None = None,
        input_package_bindings: dict[str, str] | None = None,
        *,
        consumed_only: bool = False,
    ) -> dict[str, Any]:
        calls.append({
            "state": state,
            "input_package_artifact_id": input_package_artifact_id,
            "input_package_bindings": input_package_bindings,
            "consumed_only": consumed_only,
        })
        if isinstance(result, BaseException):
            raise result
        return dict(result)

    # The helper owns the new admission.  Patch the legacy import too so a
    # duplicate, unconditional audit cannot hide behind a local import.
    monkeypatch.setattr(
        input_admission, "audit_input_delivery_for_execution", fake_audit,
    )
    monkeypatch.setattr(
        contract_audit, "audit_input_delivery_for_execution", fake_audit,
    )
    return calls


def _forbid_physical_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def forbidden_payload(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("formal-input refusal reached payload execution")

    def forbidden_submission(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("formal-input refusal reached physical submission")

    monkeypatch.setattr(safe_bash, "_exec_and_log", forbidden_payload)
    monkeypatch.setattr(rm, "_submit_sync", forbidden_submission)


@pytest.mark.parametrize(
    ("entry", "selector", "runner"),
    [
        (
            "bash",
            {"input_package_artifact_id": "dataset__pending"},
            "safe_run_bash",
        ),
        (
            "python",
            {"input_package_bindings": {"spec-a": "dataset__pending"}},
            "safe_execute_python",
        ),
        (
            "submit",
            {"input_package_artifact_id": "dataset__pending"},
            "submit_job",
        ),
    ],
)
def test_explicitly_consumed_unverified_input_refuses_before_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    entry: str,
    selector: dict[str, Any],
    runner: str,
) -> None:
    state = _v3_state(tmp_path)
    audit = {
        "passed": False,
        "applicable": True,
        "blocking_reasons": ["input_delivery_pending"],
    }
    calls = _install_audit(monkeypatch, audit)
    _forbid_physical_execution(monkeypatch)

    if entry == "bash":
        result = asyncio.run(safe_bash._safe_run_bash(
            state, cmd="true", timeout=30, **selector,
        ))
    elif entry == "python":
        result = asyncio.run(safe_bash._safe_execute_python(
            state, code="pass", timeout=30, **selector,
        ))
    else:
        result = asyncio.run(rm._submit_job(
            state,
            command="true",
            scheduler="local",
            dry_run=False,
            **selector,
        ))

    assert result["status"] == "error"
    assert result["error_code"] == "formal_input_not_ready"
    assert result["payload_spawned"] is False
    assert result["job_submitted"] is False
    assert result["runner"] == runner
    assert result["audit"] == audit
    assert calls == [{
        "state": state,
        "input_package_artifact_id": selector.get("input_package_artifact_id"),
        "input_package_bindings": selector.get("input_package_bindings"),
        "consumed_only": True,
    }]


def test_no_selector_does_not_audit_unrelated_pending_input_or_block_preparation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _v3_state(tmp_path)
    calls = _install_audit(monkeypatch, {
        "passed": False,
        "applicable": True,
        "blocking_reasons": ["unrelated_input_pending"],
    })
    spawned: list[str] = []

    async def fake_exec(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        spawned.append("payload")
        return {
            "status": "success",
            "returncode": 0,
            "stdout": "",
            "stderr": "",
            "stdout_tail": "",
            "stderr_tail": "",
            "payload_spawned": True,
            "job_submitted": False,
        }

    monkeypatch.setattr(safe_bash, "_exec_and_log", fake_exec)
    result = asyncio.run(safe_bash._safe_run_bash(
        state, cmd="true", timeout=30,
    ))

    assert result["status"] == "success", result
    assert spawned == ["payload"]
    assert calls == []


def test_submit_dry_run_does_not_audit_even_with_a_selector(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _v3_state(tmp_path)
    calls = _install_audit(
        monkeypatch,
        AssertionError("dry-run must not audit consumed input"),
    )

    result = asyncio.run(rm._submit_job(
        state,
        command="true",
        scheduler="local",
        dry_run=True,
        input_package_artifact_id="dataset__pending",
    ))

    assert result["status"] == "success", result
    assert result["dry_run"] is True
    assert calls == []


def test_audit_exception_is_a_structured_zero_execution_refusal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _v3_state(tmp_path)
    calls = _install_audit(monkeypatch, RuntimeError("audit source unavailable"))
    _forbid_physical_execution(monkeypatch)

    result = asyncio.run(safe_bash._safe_run_bash(
        state,
        cmd="true",
        timeout=30,
        input_package_artifact_id="dataset__selected",
    ))

    assert result["status"] == "error"
    assert result["error_code"] == "formal_input_not_ready"
    assert result["payload_spawned"] is False
    assert result["job_submitted"] is False
    assert result["audit"] == {
        "passed": False,
        "applicable": True,
        "blocking_reasons": ["input_delivery_audit_unavailable"],
        "exception_type": "RuntimeError",
    }
    assert len(calls) == 1


def test_non_applicable_audit_is_recorded_as_not_performed_not_verified(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = _v3_state(tmp_path)
    events: list[tuple[str, dict[str, Any]]] = []
    audit = {"passed": True, "applicable": False, "blocking_reasons": []}
    calls = _install_audit(monkeypatch, audit)
    monkeypatch.setattr(
        state,
        "append_transcript",
        lambda event, **payload: events.append((event, payload)),
    )

    result = input_admission.check_consumed_input(
        state,
        runner="safe_run_bash",
        input_package_artifact_id="dataset__parent_forwarded",
    )

    assert result is None
    assert len(calls) == 1
    assert events == [(
        "input_delivery_preflight",
        {
            "runner": "safe_run_bash",
            "verification_status": "not_performed",
            "consumed_only": True,
            **audit,
        },
    )]


def test_real_bash_consumes_only_verified_selected_input_and_spawns(
    tmp_path: Path,
) -> None:
    state = _v3_state(tmp_path)
    pending = asyncio.run(contract_audit._validate_data_request_spec(
        state,
        _service_spec(
            "pending.txt",
            purpose="Keep an unrelated formal input pending.",
        ),
    ))
    selected = asyncio.run(contract_audit._validate_data_request_spec(
        state,
        _service_spec(
            "selected.txt",
            purpose="Provide the input consumed by this exact action.",
        ),
    ))
    dataset_id = _deliver_verified_input(
        tmp_path,
        state,
        spec_id=selected["spec_id"],
        asset_name="selected.txt",
    )

    marker = "ACTION_INPUT_ADMISSION_OK"
    result = asyncio.run(safe_bash._safe_run_bash(
        state,
        cmd=f"printf {marker}",
        timeout=30,
        input_package_artifact_id=dataset_id,
    ))
    if result.get("status") != "success" and "sandbox" in str(result).lower():
        pytest.skip(f"sandbox unavailable: {str(result)[:200]}")

    assert result["status"] == "success", result
    assert result["returncode"] == 0
    assert marker in str(result.get("stdout_tail") or result.get("stdout") or "")
    entries = input_delivery.active_input_delivery_entries(state)
    assert entries[pending["spec_id"]]["delivery"].get("verified") is not True
    assert entries[selected["spec_id"]]["delivery"]["verified"] is True
    transcript = [
        json.loads(line)
        for line in state.transcript_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    admission = [
        row for row in transcript
        if row.get("event") == "input_delivery_preflight"
        and row.get("runner") == "safe_run_bash"
        and row.get("consumed_only") is True
    ]
    assert admission
    assert admission[-1]["verification_status"] == "verified"
    assert admission[-1]["spec_ids"] == [selected["spec_id"]]
    started, = [row for row in ledger.open_ledger(state).records()
                if row.get("kind") == "action_started" and row.get("tool") == "safe_run_bash"]
    observed = started["execution_context"]["input_verification"]
    assert observed["status"] == "verified"
    assert observed["selection"]["input_package_artifact_id"] == dataset_id
    assert observed["audit"]["spec_ids"] == [selected["spec_id"]]
    assert pending["spec_id"] not in observed["audit"]["spec_ids"]

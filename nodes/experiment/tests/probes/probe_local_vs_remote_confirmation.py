#!/usr/bin/env python3
"""Self-contained acceptance probe for local versus remote job confirmation.

The probe exercises the real submit/cancel control flow with a stubbed physical
submission. It starts no workload and writes only below a temporary run root.
The pre-012 baseline exits nonzero because ordinary local submit pauses and all
local categories incorrectly claim a remote operation.
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import traceback
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from core import sandbox  # noqa: E402
from core.state import State  # noqa: E402
from nodes.experiment.tools import execution_route  # noqa: E402
from nodes.experiment.tools import resource_manager as manager  # noqa: E402
from nodes.experiment.tools.run_contract import _classify_experiment_scope  # noqa: E402
from shared.lib import dangerous_commands as danger  # noqa: E402


def _await(value: Any) -> Any:
    return asyncio.run(value)


@contextmanager
def _patches(
    changes: list[tuple[object, str, object]],
) -> Iterator[None]:
    originals: list[tuple[object, str, object]] = []
    try:
        for owner, name, replacement in changes:
            originals.append((owner, name, getattr(owner, name)))
            setattr(owner, name, replacement)
        yield
    finally:
        for owner, name, original in reversed(originals):
            setattr(owner, name, original)


def _events(state: State, event: str) -> list[dict[str, Any]]:
    if not state.transcript_path.exists():
        return []
    records = [
        json.loads(line)
        for line in state.transcript_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    return [record for record in records if record.get("event") == event]


def _planned_state(root: Path) -> State:
    state = State.new("experiment", root)
    state.hook_state["node_inputs"] = {
        "fixture": "local-vs-remote-confirmation-probe",
        "requested_work": "Verify managed local submission confirmation policy.",
    }
    prereg = state.save_artifact(
        "pre_registration",
        "plan",
        "# deterministic confirmation probe",
        metadata={
            "run_role": "primary",
            "analysis_eligible": True,
            "execution_mode": "scientific",
            "expected_params": {"case": "probe"},
        },
    )
    state.mark_frozen(prereg["id"])
    classified = _await(_classify_experiment_scope(
        state,
        scope="scientific",
        reason="A frozen probe contract exercises managed submission control flow.",
    ))
    assert classified["status"] == "success", classified
    steps = []
    for program in ("echo", "sudo", "make"):
        steps.append({
            "id": f"submit_{program}",
            "goal": f"Probe managed submission through {program}",
            "after": [],
            "action": {"tool": "submit_job", "program": program},
            "effects": [
                "workspace_write",
                "process_tree",
                "external_job",
                "scientific_execution",
            ],
            "workdir_role": "build_root" if program == "make" else "run_root",
            "expected_outputs": [],
        })
    declared = _await(execution_route._declare_execution_route(state, route={
        "schema_version": 2,
        "goal": "Probe local managed submission confirmation",
        "evidence_refs": ["probe:local-vs-remote-confirmation"],
        "steps": steps,
    }))
    assert declared["status"] == "success", declared
    return state


def _success_submit(calls: list[dict[str, Any]]) -> Callable[..., dict[str, Any]]:
    def submit(*_args: object, **kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return {
            "status": "success",
            "scheduler": "local",
            "dry_run": False,
            "job_name": "confirmation_probe",
            "job_id": f"probe-{len(calls)}",
            "submission_nonce": f"probe-nonce-{len(calls)}",
        }

    return submit


def _forbidden(label: str) -> Callable[..., Any]:
    def fail(*_args: object, **_kwargs: object) -> Any:
        raise AssertionError(f"unexpected confirmation protocol call: {label}")

    return fail


def _category_matrix() -> dict[str, Any]:
    cases = [
        ("submit", "local", None, None),
        (
            "submit",
            "local",
            "提权 (sudo)",
            "本地受管作业提交（含高危命令：提权 (sudo)）",
        ),
        ("submit", "slurm", None, "真实外部作业提交"),
        (
            "submit",
            "slurm",
            "提权 (sudo)",
            "真实外部作业提交（含高危命令：提权 (sudo)）",
        ),
        ("cancel", "local", None, "取消本地受管作业"),
        ("cancel", "slurm", None, "取消真实外部作业"),
    ]
    observed: list[dict[str, Any]] = []
    for action, scheduler_name, highrisk, expected in cases:
        actual = manager._managed_job_confirmation_category(
            action=action,
            scheduler=scheduler_name,
            highrisk_category=highrisk,
        )
        assert actual == expected, {
            "action": action,
            "scheduler": scheduler_name,
            "highrisk": highrisk,
            "expected": expected,
            "actual": actual,
        }
        observed.append({
            "action": action,
            "scheduler": scheduler_name,
            "highrisk": highrisk,
            "category": actual,
        })
    return {"cases": observed}


def _local_safe_lane(root: Path) -> dict[str, Any]:
    state = _planned_state(root)
    submissions: list[dict[str, Any]] = []
    changes = [
        (sandbox, "trusted_image_id", lambda: "sha256:probe-sandbox"),
        (manager, "_submit_sync", _success_submit(submissions)),
        (danger, "bypass_enabled", _forbidden("bypass_enabled")),
        (danger, "is_confirmed", _forbidden("is_confirmed")),
        (danger, "consume_confirmation", _forbidden("consume_confirmation")),
        (danger, "build_pause_payload", _forbidden("build_pause_payload")),
    ]
    with _patches(changes):
        result = _await(manager._submit_job(
            state=state,
            command="echo submit",
            scheduler="local",
            dry_run=False,
            execution_params={"case": "probe"},
        ))
    assert result["status"] == "success", result
    assert len(submissions) == 1, submissions
    audit = _events(state, "job_submission_confirmation_not_required")
    assert len(audit) == 1, audit
    assert audit[0]["reason"] == "local_managed_no_highrisk", audit
    assert not _events(state, "job_submission_blocked_pending_confirm")
    return {
        "status": result["status"],
        "physical_submissions": len(submissions),
        "audit_reason": audit[0]["reason"],
        "confirmation_calls": 0,
    }


def _local_highrisk_lane(root: Path) -> dict[str, Any]:
    state = _planned_state(root)
    submissions: list[dict[str, Any]] = []
    changes = [
        (sandbox, "trusted_image_id", lambda: "sha256:probe-sandbox"),
        (manager, "_submit_sync", _success_submit(submissions)),
        (danger, "bypass_enabled", lambda: False),
        (danger, "is_confirmed", lambda *_args: False),
    ]
    with _patches(changes):
        result = _await(manager._submit_job(
            state=state,
            command="sudo echo submit",
            scheduler="local",
            dry_run=False,
            execution_params={"case": "probe"},
        ))
    assert result["status"] == "pause", result
    metadata = result["pause_event"]["metadata"]
    assert metadata == {
        "type": "highrisk_confirm",
        "tool": "submit_job",
        "category": "本地受管作业提交（含高危命令：提权 (sudo)）",
    }, metadata
    assert submissions == [], submissions
    return {
        "status": result["status"],
        "category": metadata["category"],
        "physical_submissions": 0,
    }


def _bypass_and_confirmed_lane(root: Path) -> dict[str, Any]:
    bypass_state = _planned_state(root / "bypass")
    bypass_calls: list[dict[str, Any]] = []
    bypass_changes = [
        (sandbox, "trusted_image_id", lambda: "sha256:probe-sandbox"),
        (manager, "_submit_sync", _success_submit(bypass_calls)),
        (danger, "bypass_enabled", lambda: True),
        (danger, "is_confirmed", _forbidden("is_confirmed_after_bypass")),
        (danger, "consume_confirmation", _forbidden("consume_after_bypass")),
    ]
    with _patches(bypass_changes):
        bypass_result = _await(manager._submit_job(
            state=bypass_state,
            command="sudo echo submit",
            scheduler="local",
            dry_run=False,
            execution_params={"case": "probe"},
        ))
    assert bypass_result["status"] == "success", bypass_result
    assert len(bypass_calls) == 1, bypass_calls
    assert len(_events(bypass_state, "job_submission_bypassed")) == 1

    confirmed_state = _planned_state(root / "confirmed")
    confirmed_calls: list[dict[str, Any]] = []
    consumed: list[str] = []
    confirmed_changes = [
        (sandbox, "trusted_image_id", lambda: "sha256:probe-sandbox"),
        (manager, "_submit_sync", _success_submit(confirmed_calls)),
        (danger, "bypass_enabled", lambda: False),
        (danger, "is_confirmed", lambda *_args: True),
        (danger, "consume_confirmation", lambda _state, text: consumed.append(text)),
    ]
    with _patches(confirmed_changes):
        confirmed_result = _await(manager._submit_job(
            state=confirmed_state,
            command="sudo echo submit",
            scheduler="local",
            dry_run=False,
            execution_params={"case": "probe"},
        ))
    assert confirmed_result["status"] == "success", confirmed_result
    assert len(confirmed_calls) == 1, confirmed_calls
    assert len(consumed) == 1, consumed
    assert len(_events(confirmed_state, "job_submission_confirmed")) == 1
    return {
        "bypass": {
            "physical_submissions": len(bypass_calls),
            "bypass_events": 1,
        },
        "confirmed": {
            "physical_submissions": len(confirmed_calls),
            "consumed_tokens": len(consumed),
        },
    }


def _save_submission(
    state: State,
    *,
    scheduler_name: str,
    job_id: str,
) -> None:
    payload = {
        "status": "success",
        "dry_run": False,
        "scheduler": scheduler_name,
        "job_id": job_id,
        "namespace": None,
        "launch_host": None,
        "scheduler_cluster": "probe-cluster",
        "resource_uid": "probe-resource",
        "process_group_id": None,
        "process_start_ticks": None,
        "container_runtime_id": "a" * 64 if scheduler_name == "local" else None,
        "submission_nonce": f"{scheduler_name}-probe-nonce",
        "sandbox_control_dir": None,
        "output_roots": [str(state.root / "outputs")],
    }
    state.save_artifact(
        "job_submission",
        "managed_submission",
        json.dumps(payload),
    )


def _cancel_lane(root: Path) -> dict[str, Any]:
    async def unknown_end(*_args: object, **_kwargs: object) -> None:
        return None

    results: dict[str, Any] = {}
    for scheduler_name, job_id, expected_category in (
        ("local", "hf-local-probe", "取消本地受管作业"),
        ("slurm", "4242", "取消真实外部作业"),
    ):
        state = State.new("experiment", root / scheduler_name)
        _save_submission(
            state,
            scheduler_name=scheduler_name,
            job_id=job_id,
        )
        changes = [
            (manager, "_observed_job_end", unknown_end),
            (danger, "bypass_enabled", lambda: False),
            (danger, "is_confirmed", lambda *_args: False),
        ]
        with _patches(changes):
            result = _await(manager._cancel_job(
                state,
                scheduler_name,
                job_id,
                reason="probe operator requested stop",
            ))
        assert result["status"] == "pause", result
        metadata = result["pause_event"]["metadata"]
        assert metadata["category"] == expected_category, metadata
        assert state.list_artifacts("external_job_cancellation_intent") == []
        results[scheduler_name] = {
            "status": result["status"],
            "category": metadata["category"],
            "intent_before_approval": False,
        }
    return results


def _run_lane(
    name: str,
    callback: Callable[[], dict[str, Any]],
    failures: list[dict[str, str]],
) -> dict[str, Any]:
    try:
        return callback()
    except Exception as exc:
        failures.append({
            "lane": name,
            "exception": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc(),
        })
        return {"error": failures[-1]["exception"]}


def _legacy_blanket_confirmation_category(
    *, action: str, scheduler: str, highrisk_category: str | None = None,
) -> str:
    """Probe mutation that reproduces the pre-012 scheduler-blind policy."""
    del scheduler
    if action == "submit":
        if highrisk_category is None:
            return "真实外部作业提交"
        return f"真实外部作业提交（含高危命令：{highrisk_category}）"
    return "取消真实外部作业"


def main() -> int:
    arguments = sys.argv[1:]
    if arguments not in ([], ["--legacy-blanket-confirmation"]):
        print("usage: probe_local_vs_remote_confirmation.py [--legacy-blanket-confirmation]", file=sys.stderr)
        return 2
    mode = "legacy_blanket_confirmation" if arguments else "candidate"
    if arguments:
        manager._managed_job_confirmation_category = (
            _legacy_blanket_confirmation_category
        )
    failures: list[dict[str, str]] = []
    report: dict[str, Any] = {
        "probe": "local_vs_remote_confirmation",
        "mode": mode,
        "physical_workload_started": False,
    }
    with tempfile.TemporaryDirectory(
        prefix="local-vs-remote-confirmation-",
    ) as temporary:
        root = Path(temporary)
        report["category_matrix"] = _run_lane(
            "category_matrix", _category_matrix, failures,
        )
        report["local_safe"] = _run_lane(
            "local_safe",
            lambda: _local_safe_lane(root / "local-safe"),
            failures,
        )
        report["local_highrisk"] = _run_lane(
            "local_highrisk",
            lambda: _local_highrisk_lane(root / "local-highrisk"),
            failures,
        )
        report["cancel"] = _run_lane(
            "cancel",
            lambda: _cancel_lane(root / "cancel"),
            failures,
        )
        report["bypass_and_confirmed"] = _run_lane(
            "bypass_and_confirmed",
            lambda: _bypass_and_confirmed_lane(root / "confirmation-paths"),
            failures,
        )
    report["failures"] = failures
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

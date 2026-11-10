"""Project Resources SSH backend for Experiment's existing job lifecycle."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import shlex
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any
from weakref import WeakValueDictionary

from core import remote_resources
from core.secrets import is_sensitive_path
from core.side_effect_contract import promised_read_only
from shared.lib import dangerous_commands

from . import job_settlement, ledger, persistence, slurm_files
from .execution_context import capture as capture_context
from .input_admission import check_consumed_input
from .timeout_escalation import looks_backgrounded

SCHEDULER = "ssh"
_FILE_LIMIT = 8 * 1024 * 1024  # current Project Resources per-file transport limit
_TERMINAL = {
    "succeeded": "COMPLETED",
    "failed": "FAILED",
    "cancelled": "CANCELLED",
    "timed_out": "TIMEOUT",
}
_ACTIVE = {"running", "launching"}
_SUBMISSION_LOCKS: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()


async def discover(state: Any, resource_id: str | None = None) -> dict:
    listing = await remote_resources.call(state, "list")
    if listing.get("status") != "success":
        return {"project_resources": [], "project_resources_status": "unavailable"}
    resources = []
    for row in listing.get("resources", []):
        if (
            row.get("provider") != "ssh"
            or row.get("resource_type") != "compute"
            or row.get("is_enabled") is not True
            or (resource_id and row.get("id") != resource_id)
        ):
            continue
        observation = (row.get("config") or {}).get("observation") or {}
        binding = {
            key: row.get(key)
            for key in ("id", "project_id", "created_by_user_id", "endpoint", "workspace_binding")
        }
        binding["ssh"] = (row.get("config") or {}).get("ssh")
        resources.append(
            {
                "resource_id": row["id"],
                "name": row.get("name"),
                "binding_sha256": hashlib.sha256(_json(binding).encode()).hexdigest(),
                "execution_backend": SCHEDULER,
                "execution_boundary": "remote_account",
                "remote_write_isolation": "missing",
                "health_status": row.get("health_status", "unknown"),
                "observation": {
                    key: observation[key]
                    for key in (
                        "observed_at",
                        "cpu_count",
                        "memory_total_bytes",
                        "gpu_csv",
                        "schedulers",
                        "isolation",
                    )
                    if key in observation
                },
            }
        )
    return {"project_resources": resources, "project_resources_status": "observed"}


def _relative(value: str, *, directory: bool = False) -> str:
    path = PurePosixPath(value)
    if (
        not value.strip()
        or value.startswith(("~", "/"))
        or "\\" in value
        or len(value) > 200
        or any(ord(char) < 32 for char in value)
        or any(part in {"..", ".git", ".ssh", ".research", ".harness"} for part in path.parts)
        or (not directory and str(path) == ".")
    ):
        raise ValueError("expected an application-relative POSIX path without traversal")
    return str(path)


def _error(reason: str, error: str, **fields: Any) -> dict:
    return {"status": "error", "reason": reason, "error": error, "scheduler": SCHEDULER, **fields}


def _remote_mutation_refusal(state: Any) -> dict | None:
    if promised_read_only(state) is not None:
        return _error("read_only", "This run has no authority to change the remote account.")
    return None


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _output_paths(paths: list[str] | None) -> list[str]:
    # Exact files keep discovery and observations bounded by the bridge reply.
    normalized = sorted({_relative(path) for path in paths or []})
    if len(normalized) > 16:
        raise ValueError("declare at most 16 exact output files for SSH collection")
    return normalized


async def _snapshot(state: Any, request: dict) -> dict:
    paths = request["output_paths"]
    if not paths:
        return {"entries": {}, "root_coverage": [], "truncated": False}
    command = shlex.join(slurm_files._argv("project_file_snapshot", {"paths": paths}))
    result = await remote_resources.call(
        state,
        "exec",
        resource_id=request["resource_id"],
        cwd=request["cwd"],
        command=command,
        timeout=30,
    )
    if result.get("status") != "success" or result.get("exit_code") != 0:
        raise OSError("project SSH output observation unavailable")
    snapshot = json.loads(result.get("stdout") or "")
    cwd = PurePosixPath(snapshot["cwd"])
    if not cwd.is_absolute() or not str(cwd).endswith("/" + request["cwd"]):
        raise ValueError("observed working directory does not match the submitted SSH workspace")
    expected = {str(cwd / path) for path in paths}
    if (
        set(snapshot["entries"]) != expected
        or {entry["root"] for entry in snapshot["root_coverage"]} != expected
    ):
        raise ValueError("SSH file observation did not cover the exact output selection")
    return snapshot


def _intent_for(
    state: Any, job_id: str, nonce: str | None = None
) -> tuple[dict, list[dict]] | None:
    records = ledger.open_ledger(state).records()
    jobs = ledger.project_jobs(records)
    matching = [
        row
        for row in records
        if row.get("kind") == "intent"
        and row.get("scheduler") == SCHEDULER
        and (not nonce or row.get("nonce") == nonce)
        and job_id in {row["nonce"], (jobs.get(row["nonce"]) or {}).get("external_id")}
    ]
    return (matching[0], records) if len(matching) == 1 else None


def _matches(state: Any, intent: dict, result: dict, *, listing: bool = False) -> bool:
    request = intent["ssh_request"]
    return bool(
        re.fullmatch(r"[a-f0-9]{32}", str(result.get("job_id") or ""))
        and result.get("project_id") == str(state.project_id)
        and result.get("resource_id") == request["resource_id"]
        and str(result.get("cwd") or "").endswith("/" + request["cwd"])
        and result.get("command") == (request["command"][:2000] if listing else request["command"])
        and result.get("input_manifest_sha256") == request.get("input_manifest_sha256")
    )


def _unresolved(intent: dict, *, can_replay: bool = False) -> dict:
    return _error(
        "ssh_job_unconfirmed",
        "SSH job acceptance or current state is unconfirmed; query the same job.",
        state="unknown",
        do_not_resubmit=True,
        job_id=intent["nonce"],
        submission_nonce=intent["nonce"],
        resource_id=intent["ssh_request"]["resource_id"],
        next_action={"tool": "job_status", "scheduler": SCHEDULER, "job_id": intent["nonce"]},
        safe_retry_same_request=can_replay,
        **(
            {
                "retry_note": (
                    "If status stays unknown, repeat submit_job with the same job name, "
                    "resource, and inputs."
                )
            }
            if can_replay
            else {}
        ),
    )


def _bind(state: Any, intent: dict, result: dict) -> bool:
    nonce = intent["nonce"]
    led = ledger.open_ledger(state, write=True)
    job = ledger.project_jobs(led.records()).get(nonce) or {}
    if job.get("external_id"):
        return job["external_id"] == result["job_id"]
    led.append_once(
        "job_bound",
        f"ssh-job-bound:{nonce}",
        nonce=nonce,
        scheduler=SCHEDULER,
        external_id=result["job_id"],
    )
    return True


async def _find_remote(state: Any, intent: dict, records: list[dict]) -> dict | None:
    request = intent["ssh_request"]
    job = ledger.project_jobs(records).get(intent["nonce"]) or {}
    if external_id := job.get("external_id"):
        return await _find_bound_status(state, intent, external_id)
    hint = next(
        (
            row.get("ack_job_id")
            for row in records
            if row.get("kind") == "job_unknown"
            and row.get("nonce") == intent["nonce"]
            and row.get("ack_job_id")
        ),
        None,
    )
    if hint and (result := await _find_bound_status(state, intent, hint)):
        return result
    # The older bridge has no lookup by submission key. Recover only a unique
    # positive match on the intent-owned cwd. List truncation is never absence.
    result = await remote_resources.call(state, "jobs", resource_id=request["resource_id"])
    candidates = [
        job for job in result.get("jobs", []) if _matches(state, intent, job, listing=True)
    ]
    if result.get("status") != "success" or len(candidates) != 1:
        return None
    return await _find_bound_status(state, intent, candidates[0]["job_id"])


async def _find_bound_status(state: Any, intent: dict, external_id: str) -> dict | None:
    result = await remote_resources.call(
        state,
        "status",
        resource_id=intent["ssh_request"]["resource_id"],
        job_id=external_id,
    )
    if (
        result.get("status") == "success"
        and _matches(state, intent, result)
        and result["job_id"] == external_id
    ):
        return result
    return None


async def _collect(state: Any, terminal: dict, request: dict, max_bytes: int) -> dict:
    observation = slurm_files._frozen_observation(terminal)
    report: dict[str, Any] = {"status": "complete", "files": [], "unavailable": []}
    candidates = set(observation.get("created", [])) | set(observation.get("modified", []))
    if observation.get("truncated") or observation.get("unknown"):
        report["status"] = "partial"
    for path in sorted(candidates):
        identity = observation["identities"][path]["after"]
        if identity.get("kind") != "file":
            continue
        try:
            digest = identity.get("sha256")
            size = identity["size"]
            if not digest or size > min(_FILE_LIMIT, max_bytes):
                raise ValueError("file exceeds the transfer budget or has no frozen SHA-256")
            root = slurm_files._mirror_root(state, terminal["nonce"])
            suffix = re.sub(r"[^A-Za-z0-9_.-]", "_", PurePosixPath(path).name)[-60:]
            local = root / (hashlib.sha256(path.encode()).hexdigest()[:24] + "_" + suffix)
            if not local.exists() and not local.is_symlink():
                received = await remote_resources.call(
                    state, "get", resource_id=request["resource_id"], path=path
                )
                if received.get("status") != "success":
                    raise OSError("SSH output transfer failed")
                data = base64.b64decode(received["content_base64"], validate=True)
                if (
                    len(data) != size
                    or hashlib.sha256(data).hexdigest() != digest
                    or received.get("sha256") != digest
                ):
                    raise ValueError("download differs from the frozen job output")
                with tempfile.NamedTemporaryFile(dir=root, delete=False) as stream:
                    temporary = Path(stream.name)
                    try:
                        stream.write(data)
                        stream.flush()
                        os.fsync(stream.fileno())
                        os.link(temporary, local)  # never overwrite an existing mirror
                    finally:
                        temporary.unlink(missing_ok=True)
                max_bytes -= size
            persistence.fsync_directory(root)
            receipt = slurm_files._receipt(state, terminal, path, identity, local, digest)
            report["files"].append(
                {
                    "path": str(local),
                    "remote_path": path,
                    "sha256": digest,
                    "receipt_seq": receipt["seq"],
                }
            )
        except (OSError, ValueError, KeyError, ledger.LedgerError) as exc:
            report["status"] = "partial"
            report["unavailable"].append({"remote_path": path, "reason": str(exc)})
    return report


async def _observe(state: Any, intent: dict, result: dict, max_bytes: int) -> dict:
    nonce = intent["nonce"]
    request = intent["ssh_request"]
    if not _matches(state, intent, result):
        return _unresolved(intent)
    try:
        bound = _bind(state, intent, result)
    except (OSError, ledger.LedgerError):
        bound = False
    if not bound:
        return _unresolved(intent)
    physical = result.get("state")
    terminal_state = _TERMINAL.get(physical)
    if physical not in _ACTIVE and terminal_state is None:
        return _unresolved(intent)
    if not terminal_state:
        return {
            "status": "success",
            "scheduler": SCHEDULER,
            "state": physical,
            "scheduler_phase": "running",
            "job_id": result["job_id"],
            "submission_nonce": nonce,
            "resource_id": request["resource_id"],
            "stdout": result.get("stdout"),
            "stderr": result.get("stderr"),
        }
    rc = result.get("exit_code")
    failed_to_start = physical == "failed" and rc is None and not result.get("pid")
    if (
        not result.get("finished_at")
        or (type(rc) is not int and not failed_to_start)
        or (physical == "succeeded" and rc != 0)
        or (physical == "failed" and rc == 0)
    ):
        return _unresolved(intent)
    existing = job_settlement.existing_terminal(state, nonce)
    if existing is None:
        marker = next(
            row
            for row in ledger.open_ledger(state).records()
            if row.get("kind") == "submission_attempted" and row.get("nonce") == nonce
        )
        before = marker["output_baseline"]
        try:
            after = await _snapshot(state, request)
            output = {
                **slurm_files.remote_diff(before, after),
                "coordinate_space": "project_ssh",
                "baseline_seq": marker["seq"],
            }
        except (OSError, ValueError, KeyError, TypeError) as exc:
            output = {
                "created": [],
                "modified": [],
                "deleted": [],
                "unknown": [],
                "identities": {},
                "truncated": True,
                "status": "unavailable",
                "limitation": f"Terminal observed, but output identity unavailable: {exc}",
            }
        settled = job_settlement.settle_terminal(
            state,
            {
                "scheduler": SCHEDULER,
                "job_id": result["job_id"],
                "submission_nonce": nonce,
                "resource_id": request["resource_id"],
            },
            evidence={
                "source": "project_ssh_supervisor",
                "state": terminal_state,
                "exit_code": rc,
                "finished_at": result["finished_at"],
                "stdout": result.get("stdout"),
                "stderr": result.get("stderr"),
                "error": result.get("error"),
            },
            observed_by="project_ssh",
            output_observation=output,
        )
        if not settled.get("settled"):
            return {**_unresolved(intent), "terminal_settlement": settled.get("reason")}
        existing = settled["record"]
    return await _terminal_response(state, intent, existing, max_bytes)


async def _terminal_response(state: Any, intent: dict, terminal: dict, max_bytes: int) -> dict:
    request = intent["ssh_request"]
    evidence = (terminal.get("raw") or {}).get("evidence") or {}
    return {
        "status": "success",
        "scheduler": SCHEDULER,
        "scheduler_phase": "terminal",
        "state": next(
            key for key, value in _TERMINAL.items() if value == terminal["derived_state"]
        ),
        "exit_code": terminal.get("exit_code"),
        "job_id": terminal["external_id"],
        "stdout": evidence.get("stdout"),
        "stderr": evidence.get("stderr"),
        "resource_id": request["resource_id"],
        "submission_nonce": intent["nonce"],
        "terminal_settlement": {"settled": True, "seq": terminal["seq"]},
        "output_collection": await _collect(state, terminal, request, max_bytes),
    }


async def status(
    state: Any,
    job_id: str,
    nonce: str | None = None,
    *,
    max_bytes: int = 256 * 1024**2,
    cancel_reason: str | None = None,
) -> dict:
    intent, rows = _intent_for(state, job_id, nonce) or (None, [])
    if intent is None or not any(
        row.get("kind") == "submission_attempted" and row.get("nonce") == intent["nonce"]
        for row in rows
    ):
        reason, message = (
            ("ssh_job_not_owned", "No unique SSH job with this handle belongs to the current run.")
            if intent is None
            else ("ssh_job_not_submitted", "The submission did not cross the launch boundary.")
        )
        return _error(reason, message, job_id=job_id)
    if terminal := job_settlement.existing_terminal(state, intent["nonce"]):
        return await _terminal_response(state, intent, terminal, max_bytes)
    result = await _find_remote(state, intent, rows)
    if result is None:
        bound = ledger.project_jobs(rows).get(intent["nonce"], {}).get("external_id")
        return _unresolved(intent, can_replay=not bound)
    if cancel_reason is not None and result.get("state") in _ACTIVE:
        if blocked := _remote_mutation_refusal(state):
            return blocked
        identity = {"resource_id": intent["ssh_request"]["resource_id"], "job_id": result["job_id"]}
        approval = _json(
            {"operation": "cancel_external_job", "identity": identity, "reason": cancel_reason}
        )
        approval_key = hashlib.sha256(approval.encode()).hexdigest()
        cancellation_key = f"ssh-cancel:{intent['nonce']}:{approval_key}"
        requested = any(row.get("idempotency_key") == cancellation_key for row in rows)
        if not requested and not dangerous_commands.bypass_enabled():
            if not dangerous_commands.is_confirmed(state, approval):
                return dangerous_commands.build_pause_payload(
                    state,
                    tool="cancel_job",
                    text=approval,
                    category="取消真实外部作业",
                    preview=approval,
                )
            dangerous_commands.consume_confirmation(state, approval)
        try:
            if not _bind(state, intent, result):
                return _unresolved(intent)
            ledger.open_ledger(state, write=True).append_once(
                "job_cancel_requested",
                cancellation_key,
                scheduler=SCHEDULER,
                nonce=intent["nonce"],
                external_id=result["job_id"],
                resource_id=identity["resource_id"],
                reason=cancel_reason,
            )
        except (OSError, ledger.LedgerError):
            return _error(
                "ssh_cancel_not_durable", "Cancellation was not sent; repair the Experiment ledger."
            )
        result = await remote_resources.call(state, "cancel", **identity)
        if result.get("status") != "success" or result.get("job_id") != identity["job_id"]:
            return _unresolved(intent)
    return await _observe(state, intent, result, max_bytes)


async def _stage(state: Any, request: dict, stage_in: list[dict]) -> None:
    for item in stage_in:
        path = Path(item["src"])
        if is_sensitive_path(path):
            raise ValueError("protected files cannot be staged")
        with slurm_files._open_source(path) as source:
            data = source.read(_FILE_LIMIT + 1)
        digest = hashlib.sha256(data).hexdigest()
        if len(data) > _FILE_LIMIT or digest != item["sha256"]:
            raise ValueError("stage input exceeds the transfer limit or changed after approval")
        stored = await remote_resources.call(
            state,
            "put",
            resource_id=request["resource_id"],
            cwd=request["cwd"],
            path=item["dst"],
            content_base64=base64.b64encode(data).decode("ascii"),
        )
        if stored.get("status") != "success" or stored.get("sha256") != digest:
            raise OSError("SSH input staging could not be verified")


async def submit(
    state: Any,
    *,
    resource_id: str | None = None,
    command: str,
    dry_run: bool = True,
    workdir: str | None = None,
    output_paths: list[str] | None = None,
    job_name: str = "experiment_job",
    walltime_minutes: int | None = None,
    hard_deadline_s: int | None = None,
    foreground_wait_s: int | None = None,
    stage_in: list[dict] | None = None,
    execution_params: dict | None = None,
    input_package_artifact_id: str | None = None,
    input_package_bindings: dict | None = None,
    **unsupported: Any,
) -> dict:
    from . import resource_manager as rm

    if unsupported:
        return _error(
            "ssh_contract_unsupported",
            "SSH enforces walltime at the remote account boundary. "
            "Omit the unsupported options or select a backend that provides them.",
            unsupported=sorted(unsupported),
            job_submitted=False,
        )
    if blocked := rm._submission_static_validity_block(state, command):
        return {**blocked, "job_submitted": False}
    if looks_backgrounded(command):
        return _error(
            "ssh_background_payload",
            "Submit each independent foreground payload as a managed job.",
            job_submitted=False,
        )
    if blocked := _remote_mutation_refusal(state):
        return {**blocked, "job_submitted": False}
    resources = (await discover(state, resource_id))["project_resources"] if resource_id else []
    if not resources:
        reason = "ssh_resource_unavailable" if resource_id else "ssh_resource_required"
        return _error(
            reason,
            "Select an enabled SSH resource available to this project from discover_resources.",
            job_submitted=False,
        )
    try:
        subdir = _relative(workdir or ".", directory=True)
        paths = _output_paths(output_paths)
        staged = rm._validate_stage_in(state, stage_in, remote=True, max_bytes=_FILE_LIMIT)
        for item in staged:
            item["dst"] = _relative(item["dst"])
        staged = sorted(staged, key=lambda item: item["dst"])
        if set(paths) & {item["dst"] for item in staged}:
            raise ValueError("stage inputs and declared result files must be distinct")
        if len({item["dst"] for item in staged}) != len(staged):
            raise ValueError("stage destinations must be unique")
        if len(staged) > 16:
            raise ValueError("stage at most 16 files per SSH job")
        limits = (walltime_minutes * 60 if walltime_minutes is not None else None, hard_deadline_s)
        timeout = min((limit for limit in limits if limit is not None), default=86400)
        if not 0 < timeout <= 604800:
            raise ValueError("SSH walltime must be positive and no longer than seven days")
    except (OSError, ValueError) as exc:
        return _error("ssh_invalid_request", str(exc), job_submitted=False)
    if blocked := rm._ssh_submission_path_guard(
        "/job", "/job", [], command=command, confine_to_workdir=True
    ):
        return blocked
    request = {
        "resource_id": resource_id,
        "command": "set -o pipefail\n" + command,
        "timeout": timeout,
        "output_paths": paths,
    }
    binding_sha256 = resources[0]["binding_sha256"]
    confirmation = {
        "operation": "submit_external_job",
        "scheduler": SCHEDULER,
        **request,
        "job_name": job_name,
        "workdir": subdir,
        "stage_in": staged,
        "execution_params": execution_params,
        "input_package_artifact_id": input_package_artifact_id,
        "input_package_bindings": input_package_bindings,
    }
    planned_inputs = [{"src": item["src"], "dst": item["dst"]} for item in staged]
    planned = _json({**confirmation, "stage_in": planned_inputs})
    nonce = hashlib.sha256(f"{state.project_id}:{state.run_id}:{planned}".encode()).hexdigest()[:32]
    request["cwd"] = str(PurePosixPath("experiment-jobs") / nonce / subdir)
    if dry_run:
        return {
            "status": "success",
            "scheduler": SCHEDULER,
            "dry_run": True,
            "job_submitted": False,
            "resource_id": resource_id,
            "workdir": request["cwd"],
            "output_paths": paths,
            "timeout": timeout,
            "execution_boundary": "remote_account",
            "remote_write_isolation": "missing",
        }
    if ledger.bootstrap_phase(state) != "ready":
        return _error(
            "bootstrap_incomplete",
            "SSH submission requires a ready current Experiment ledger.",
            job_submitted=False,
        )
    lock_key = f"{state.root}:{nonce}"
    lock = _SUBMISSION_LOCKS.setdefault(lock_key, asyncio.Lock())
    async with lock:
        return await _submit_intent(
            state, nonce, request, confirmation, binding_sha256, foreground_wait_s
        )


async def _submit_intent(
    state: Any,
    nonce: str,
    request: dict,
    confirmation: dict,
    binding_sha256: str,
    foreground_wait_s: int | None,
) -> dict:
    from . import resource_manager as rm

    led = ledger.open_ledger(state, write=True)
    records = led.records()
    previous = ledger.project_jobs(records).get(nonce) or {}
    old_intent = next(
        (row for row in records if row.get("kind") == "intent" and row.get("nonce") == nonce), None
    )
    staged = confirmation["stage_in"]
    inputs_changed = old_intent is not None and old_intent.get("stage_in") != staged
    if inputs_changed or previous.get("state") == "absent":
        reason, message = (
            (
                "ssh_staged_inputs_changed",
                "The previous attempt bound different input bytes. Query its original handle.",
            )
            if inputs_changed
            else ("ssh_previous_attempt_closed", "The previous attempt ended before launch.")
        )
        return _error(
            reason,
            f"{message} Use a new job_name for a new attempt.",
            job_id=nonce,
            submission_nonce=nonce,
            job_submitted=False,
        )
    if any(
        row.get("kind") == "submission_attempted" and row.get("nonce") == nonce for row in records
    ):
        observed = await status(state, nonce)
        if not observed.get("safe_retry_same_request"):
            return observed
        if old_intent.get("resource_binding_sha256") != binding_sha256:
            return {
                **observed,
                "safe_retry_same_request": False,
                "reason": "ssh_resource_binding_changed",
            }
        if staged:
            try:
                evidence = await _snapshot(
                    state, {**request, "output_paths": [row["dst"] for row in staged]}
                )
            except (OSError, ValueError, KeyError, TypeError):
                return observed
            identities = evidence["entries"]
            if any(
                identities[str(PurePosixPath(evidence["cwd"]) / row["dst"])].get("sha256")
                != row["sha256"]
                for row in staged
            ):
                return {
                    **observed,
                    "safe_retry_same_request": False,
                    "reason": "ssh_remote_staged_inputs_changed",
                }
        # An explicit repeat of the same submission can deliver a request
        # that never reached the server. Reuse the stored key and exact bytes;
        # the supervisor's durable lock prevents a second accepted execution.
        return await _launch(state, led, old_intent, foreground_wait_s)
    input_observation: dict = {}
    if blocked := check_consumed_input(
        state,
        runner="submit_job",
        input_package_artifact_id=confirmation["input_package_artifact_id"],
        input_package_bindings=confirmation["input_package_bindings"],
        observation=input_observation,
    ):
        return blocked
    if not previous:
        # A list entry is visible under view_session; probe uses the existing
        # drive_session and account checks before a write intent is opened.
        probe = await remote_resources.call(state, "probe", resource_id=request["resource_id"])
        isolation = probe.get("isolation") or {}
        if (
            probe.get("status") != "success"
            or isolation.get("process_group") is not True
            or isolation.get("walltime") is not True
        ):
            return _error(
                "ssh_resource_probe_failed",
                "The current run could not reach the account or verify its supervisor capability.",
                job_submitted=False,
            )
    if not previous and not dangerous_commands.bypass_enabled():
        approval = _json(
            {
                **confirmation,
                "job_workdir": request["cwd"],
                "resource_binding_sha256": binding_sha256,
            }
        )
        if not dangerous_commands.is_confirmed(state, approval):
            category = rm._managed_job_confirmation_category(
                action="submit",
                scheduler=SCHEDULER,
                highrisk_category=None,
            )
            return dangerous_commands.build_pause_payload(
                state,
                tool="submit_job",
                text=approval,
                category=category,
                preview=(
                    f"{approval}\nSSH uses remote account rights. "
                    "The account's usual filesystem and memory permissions apply."
                ),
            )
        dangerous_commands.consume_confirmation(state, approval)
    try:
        context = capture_context(
            state,
            execution_params=confirmation["execution_params"],
            input_observation=input_observation,
        )
        # Hash the input declaration already recorded by this intent. This
        # binds verified staged bytes and protocol references, not arbitrary
        # files the payload might open at runtime.
        inputs = {
            "stage_in": staged,
            "prereg_binding": context["prereg_binding"],
            "execution_params": context["execution_params"],
            "input_verification": context["input_verification"],
        }
        request = {
            **request,
            "input_manifest_sha256": hashlib.sha256(_json(inputs).encode()).hexdigest(),
        }
        intent = led.append_once(
            "intent",
            f"ssh-job-intent:{nonce}",
            nonce=nonce,
            scheduler=SCHEDULER,
            job_name=confirmation["job_name"],
            workdir=request["cwd"],
            output_root=request["cwd"],
            ssh_request=request,
            resource_binding_sha256=binding_sha256,
            stage_in=staged,
            requested_resources={"walltime_seconds": request["timeout"]},
            execution_context=context,
        )
        baseline = await _snapshot(state, request)
        await _stage(state, request, staged)
        led.append_once(
            "submission_attempted",
            f"ssh-job-attempted:{nonce}",
            nonce=nonce,
            scheduler=SCHEDULER,
            output_baseline=baseline,
        )
    except (OSError, ValueError, KeyError, TypeError, ledger.LedgerError) as exc:
        rows = led.records()
        if any(
            row.get("kind") == "submission_attempted" and row.get("nonce") == nonce for row in rows
        ):
            return await status(state, nonce)
        if any(row.get("kind") == "intent" and row.get("nonce") == nonce for row in rows):
            led.append_once(
                "job_absent",
                f"ssh-job-absent:{nonce}",
                nonce=nonce,
                scheduler=SCHEDULER,
                reason="pre_launch_staging_or_observation_failed",
            )
        return _error(
            "ssh_pre_launch_failed", str(exc), job_submitted=False, submission_nonce=nonce
        )
    return await _launch(state, led, intent, foreground_wait_s)


async def _launch(
    state: Any, led: ledger.Ledger, intent: dict, foreground_wait_s: int | None
) -> dict:
    request = intent["ssh_request"]
    nonce = intent["nonce"]
    resource_id = request["resource_id"]
    submitted = await remote_resources.call(
        state,
        "submit",
        resource_id=resource_id,
        command=request["command"],
        cwd=request["cwd"],
        timeout=request["timeout"],
        idempotency_key=nonce,
        input_manifest_sha256=request.get("input_manifest_sha256"),
    )
    if submitted.get("status") != "success" or not _matches(state, intent, submitted):
        # A service-generated job ID after transport failure is a lookup
        # hint. Only an identity-checked status reply can bind the job.
        error = submitted.get("error")
        hint = (
            error.get("job_id")
            if isinstance(error, dict) and error.get("resource_id") == resource_id
            else None
        )
        hint = hint if isinstance(hint, str) and re.fullmatch(r"[a-f0-9]{32}", hint) else None
        job = ledger.project_jobs(led.records()).get(nonce) or {}
        if job.get("state") == "intent":
            try:
                led.append_once(
                    "job_unknown",
                    f"ssh-job-unknown:{nonce}",
                    scheduler=SCHEDULER,
                    nonce=nonce,
                    ack_job_id=hint,
                    reason="ssh_ack_unconfirmed",
                )
            except (OSError, ledger.LedgerError):
                pass  # The earlier attempted marker still keeps the effect open.
        return _unresolved(intent, can_replay=not job.get("external_id"))
    response = await _observe(state, intent, submitted, 256 * 1024**2)
    if foreground_wait_s and response.get("scheduler_phase") == "running":
        return await wait(state, response["job_id"], nonce, foreground_wait_s, None)
    return response


async def wait(
    state: Any, job_id: str, nonce: str | None, seconds: int | None, interval: int | None
) -> dict:
    initial = await status(state, job_id, nonce)
    if initial.get("scheduler_phase") != "running":
        return initial
    selected = _intent_for(state, job_id, nonce)
    if selected is None:
        return initial
    intent, _ = selected
    arguments = {key: value for key, value in dict(
        scheduler=SCHEDULER, job_id=initial["job_id"], submission_nonce=intent["nonce"],
        max_wait_s=seconds, poll_interval_s=interval).items() if value is not None}
    observed = await remote_resources.wait(
        state, intent["ssh_request"]["resource_id"], initial["job_id"],
        max_wait_s=seconds, poll_interval_s=interval,
        resume_tool="wait_for_external_job", resume_arguments=arguments)
    if observed.get("status") != "success":
        return observed
    # Physical completion is not scientific evidence. Reuse the existing
    # observation/freezing path before delivering the Experiment result.
    result = await status(state, initial["job_id"], intent["nonce"])
    if result.get("scheduler_phase") == "running":
        result["wait_outcome"] = "wait_elapsed_running"
    return result

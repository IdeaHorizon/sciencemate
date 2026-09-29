from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from core.state import State
from nodes.experiment.tools.execution_route import (
    ROUTE_V2_TOOL_SCHEMA,
    _canonical_route_artifact_id,
    _declare_execution_route,
    _local_output_repoint_witness,
    _step_definition_hash_fields,
    begin_route_step_attempt,
    build_route_snapshot,
    finish_route_step_attempt,
    resolve_execution_context,
    step_definition_hash,
)
from nodes.experiment.tools.run_contract import _classify_experiment_scope


def _state(
    tmp_path: Path,
    *,
    run_id: str | None = None,
    operation_category: str = "toolchain_build",
) -> State:
    if run_id is None:
        state = State.new(
            node_type="experiment",
            base_dir=tmp_path / "runs",
            project_id="route-project",
        )
    else:
        state = State.reopen(
            run_id=run_id,
            node_type="experiment",
            base_dir=tmp_path / "runs",
            project_id="route-project",
        )
    state.hook_state.setdefault("node_inputs", {
        "fixture": "execution_route",
        "requested_work": "验证受管执行路线的绑定、恢复与阻断语义",
        "prereg_assignment": {
            "kind": "none",
            "reason": "This operation has no governing preregistration.",
        },
    })
    if "experiment_execution_scope" not in state.hook_state:
        classified = asyncio.run(_classify_experiment_scope(
            state,
            scope="operation",
            operation_category=operation_category,
            reason="路线测试的受管构建动作必须绑定稳定的上游测试输入。",
        ))
        assert classified["status"] == "success", classified
    return state


def _route(*, goal: str = "构建并验证示例程序") -> dict:
    return {
        "schema_version": 2,
        "goal": goal,
        "evidence_refs": ["https://example.invalid/official-build-guide"],
        "steps": [
            {
                "id": "acquire",
                "goal": "取得源码",
                "after": [],
                "action": {"tool": "safe_run_bash", "program": "curl"},
                "effects": ["network_access", "workspace_write"],
                "workdir_role": "managed_source_root",
                "expected_outputs": ["src/source.tar.gz"],
            },
            {
                "id": "build",
                "goal": "使用项目入口构建",
                "after": ["acquire"],
                "action": {"tool": "submit_job", "program": "./compile"},
                "effects": ["workspace_write", "process_tree", "external_job"],
                "workdir_role": "build_root",
                "expected_outputs": ["main.exe"],
            },
            {
                "id": "smoke",
                "goal": "最小运行验证",
                "after": ["build"],
                "action": {"tool": "submit_job", "program": "./main.exe"},
                "effects": ["workspace_write", "external_job"],
                "workdir_role": "run_root",
                "expected_outputs": ["smoke.log"],
            },
        ],
    }


_STEP_IDENTITY_LEAVES = [
    "id", "goal", "after", "action.tool", "action.program",
    "action.program_sequence", "action.evidence_refs", "effects",
    "workdir_role", "expected_outputs",
]


def test_step_identity_disclosure_is_derived_from_validator_and_schema():
    step_schema = ROUTE_V2_TOOL_SCHEMA["properties"]["steps"]["items"]
    schema_leaves = {
        *set(step_schema["properties"]).difference({"action"}),
        *{
            f"action.{field}"
            for field in step_schema["properties"]["action"]["properties"]
        },
    }

    assert set(_step_definition_hash_fields()) == schema_leaves
    assert set(_step_definition_hash_fields()) == set(_STEP_IDENTITY_LEAVES)
    assert "action.workdir_role" not in schema_leaves


@pytest.mark.parametrize("field", _STEP_IDENTITY_LEAVES)
def test_every_disclosed_step_identity_leaf_changes_the_hash(field):
    step = json.loads(json.dumps(_route()["steps"][0]))
    before = step_definition_hash(step)
    replacements = {
        "id": "acquire-v2",
        "goal": "取得另一份源码",
        "after": ["prerequisite"],
        "action.tool": "safe_execute_python",
        "action.program": "wget",
        "action.program_sequence": ["curl", "tar"],
        "action.evidence_refs": ["artifact:entrypoint-proof"],
        "effects": ["workspace_write"],
        "workdir_role": "build_root",
        "expected_outputs": ["actual/source.tar.gz"],
    }
    if field.startswith("action."):
        step["action"][field.split(".", 1)[1]] = replacements[field]
    else:
        step[field] = replacements[field]
    assert step_definition_hash(step) != before


def _window_receipt(timestamp_ns: int) -> dict:
    return {
        "recorded_at": datetime.fromtimestamp(
            timestamp_ns / 1_000_000_000,
            tz=timezone.utc,
        ).isoformat(),
        "missing_expected_outputs": ["declared/source.tar.gz"],
    }


@pytest.mark.parametrize(
    "case",
    [
        "preexisting_unchanged",
        "post_terminal_create",
        "post_terminal_overwrite",
        "post_terminal_utime_rollback",
        "missing_bound_time",
        "missing_terminal_time",
        "symlink",
        "hardlink",
        "out_of_root",
        "basename_swap",
    ],
)
def test_local_output_correction_witness_rejects_unsafe_candidates(
    tmp_path, case,
):
    workdir = tmp_path / "work"
    actual_dir = workdir / "actual"
    actual_dir.mkdir(parents=True)
    candidate = actual_dir / "source.tar.gz"
    candidate.write_bytes(b"candidate\n")
    stat = candidate.stat()
    bound_ns = stat.st_ctime_ns - 1_000_000_000
    terminal_ns = stat.st_ctime_ns + 1_000_000_000
    output = "actual/source.tar.gz"

    if case == "preexisting_unchanged":
        bound_ns = max(stat.st_mtime_ns, stat.st_ctime_ns) + 1_000_000_000
        terminal_ns = bound_ns + 1_000_000_000
    elif case in {"post_terminal_create", "post_terminal_overwrite"}:
        if case == "post_terminal_overwrite":
            candidate.write_bytes(b"overwritten after terminal\n")
            stat = candidate.stat()
        terminal_ns = min(stat.st_mtime_ns, stat.st_ctime_ns) - 1_000_000_000
        bound_ns = terminal_ns - 1_000_000_000
    elif case == "post_terminal_utime_rollback":
        terminal_ns = time.time_ns()
        time.sleep(0.01)
        candidate.write_bytes(b"overwritten after terminal\n")
        rolled_back_ns = terminal_ns - 500_000_000
        os.utime(candidate, ns=(rolled_back_ns, rolled_back_ns))
        bound_ns = terminal_ns - 1_000_000_000
    elif case == "symlink":
        target = actual_dir / "target.tar.gz"
        target.write_bytes(b"target\n")
        candidate.unlink()
        candidate.symlink_to(target.name)
    elif case == "hardlink":
        os.link(candidate, actual_dir / "second-link.tar.gz")
    elif case == "out_of_root":
        outside = tmp_path / "source.tar.gz"
        outside.write_bytes(b"outside\n")
        output = "../source.tar.gz"
    elif case == "basename_swap":
        other = actual_dir / "other.tar.gz"
        other.write_bytes(b"other\n")
        output = "actual/other.tar.gz"

    previous_bound = {
        "resolved_workdir": str(workdir),
        "bound_at_ns": 0 if case == "missing_bound_time" else bound_ns,
        "expected_outputs": ["declared/source.tar.gz"],
    }
    receipt = _window_receipt(terminal_ns)
    if case == "missing_terminal_time":
        receipt["recorded_at"] = ""
    witness, violations = _local_output_repoint_witness(
        previous_bound,
        receipt,
        "attempt-negative",
        [output],
    )

    assert witness is None
    assert violations


def test_same_name_sideproduct_passes_only_the_disclosed_physical_scope(
    tmp_path,
):
    workdir = tmp_path / "work"
    candidate = workdir / "sideproduct" / "source.tar.gz"
    candidate.parent.mkdir(parents=True)
    before_ns = datetime.now(timezone.utc).timestamp() * 1_000_000_000
    time.sleep(0.01)
    candidate.write_bytes(b"same-name sideproduct\n")
    after_ns = datetime.now(timezone.utc).timestamp() * 1_000_000_000

    witness, violations = _local_output_repoint_witness(
        {"resolved_workdir": str(workdir), "bound_at_ns": int(before_ns),
         "expected_outputs": ["declared/source.tar.gz"]},
        _window_receipt(int(after_ns)),
        "attempt-sideproduct",
        ["sideproduct/source.tar.gz"],
    )

    assert violations == []
    assert witness["physical_postcondition_verified"] is True
    assert witness["witness_scope"] == "run_shared_workdir_time_window"
    assert witness["semantic_identity_independently_verified"] is False


def test_local_output_witness_rejects_path_replacement_during_stable_read(
    tmp_path, monkeypatch,
):
    from nodes.experiment.tools import execution_route as er

    workdir = tmp_path / "work"
    candidate = workdir / "actual" / "source.tar.gz"
    candidate.parent.mkdir(parents=True)
    before_ns = time.time_ns()
    time.sleep(0.01)
    candidate.write_bytes(b"original inode\n")
    terminal_ns = time.time_ns()
    replacement = candidate.with_name("replacement.tar.gz")
    replacement.write_bytes(b"replacement inode\n")
    original_fingerprint = er._local_identity_fingerprint
    replaced = False

    def replace_after_fd_stat(relative_path, identity):
        nonlocal replaced
        fingerprint = original_fingerprint(relative_path, identity)
        if not replaced:
            os.replace(replacement, candidate)
            replaced = True
        return fingerprint

    monkeypatch.setattr(
        er, "_local_identity_fingerprint", replace_after_fd_stat)
    witness, violations = er._local_output_repoint_witness(
        {"resolved_workdir": str(workdir), "bound_at_ns": before_ns,
         "expected_outputs": ["declared/source.tar.gz"]},
        _window_receipt(terminal_ns),
        "attempt-toctou",
        ["actual/source.tar.gz"],
    )

    assert witness is None
    assert any("TOCTOU" in item for item in violations)


def test_local_identity_accepts_zero_device_and_inode_without_coercion():
    from nodes.experiment.tools import execution_route as er

    record = {
        "device": 0,
        "inode": 0,
        "mode": 0o100644,
        "link_count": 1,
        "size": 0,
        "mtime_ns": 1,
        "ctime_ns": 1,
    }
    assert er._local_file_record_identity(record) == (
        0, 0, 0o100644, 1, 0, 1, 1,
    )
    for invalid in ("0", False):
        record["inode"] = invalid
        assert er._local_file_record_identity(record) is None


def test_local_output_witness_never_blocks_on_fifo_swap(
    tmp_path, monkeypatch,
):
    from nodes.experiment.tools import execution_route as er

    workdir = tmp_path / "work"
    candidate = workdir / "actual" / "source.tar.gz"
    candidate.parent.mkdir(parents=True)
    before_ns = time.time_ns()
    time.sleep(0.01)
    candidate.write_bytes(b"regular before race\n")
    terminal_ns = time.time_ns()
    original_open = er.os.open
    observed_flags = 0

    def swap_to_fifo(path, flags, *args, **kwargs):
        nonlocal observed_flags
        if os.fspath(path) != str(candidate):
            return original_open(path, flags, *args, **kwargs)
        observed_flags = flags
        candidate.unlink()
        os.mkfifo(candidate)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(er.os, "open", swap_to_fifo)
    witness, violations = er._local_output_repoint_witness(
        {"resolved_workdir": str(workdir), "bound_at_ns": before_ns,
         "expected_outputs": ["declared/source.tar.gz"]},
        _window_receipt(terminal_ns),
        "attempt-fifo-race",
        ["actual/source.tar.gz"],
    )

    assert observed_flags & os.O_NONBLOCK
    assert witness is None
    assert any("不再是普通文件" in item for item in violations)


def _corrected_local_route(
    tmp_path: Path,
    *,
    original_outputs: list[str] | None = None,
    corrected_outputs: list[str] | None = None,
) -> tuple[State, dict, dict, Path]:
    # This helper exercises correction of one acquired file.  It deliberately
    # trims away the build and smoke steps, so freezing the run as a
    # ``toolchain_build`` would misstate the immutable scope and let a
    # file-delivery fixture collide with the real-build completion guard.
    state = _state(tmp_path, operation_category="other")
    route = _route()
    route["steps"] = [route["steps"][0]]
    original_outputs = original_outputs or ["src/source.tar.gz"]
    corrected_outputs = corrected_outputs or ["actual/source.tar.gz"]
    route["steps"][0]["expected_outputs"] = original_outputs
    assert asyncio.run(_declare_execution_route(state, route=route))[
        "status"] == "success"
    workdir = state.root / "outputs" / "experiment" / "runtime" / "source"
    workdir.mkdir(parents=True)
    decision = resolve_execution_context(state, {
        "tool": "safe_run_bash",
        "program": "curl",
        "route_step_id": "acquire",
        "read_only": False,
        "observed_effects": ["network_access", "workspace_write"],
        "workdir_roles": ["managed_source_root"],
    })
    decision.update({
        "workdir_role_observed": True,
        "workdir_resolution_status": "resolved",
        "resolved_workdir": str(workdir),
    })
    binding = begin_route_step_attempt(
        state, decision, tool="safe_run_bash",
        action={"payload_digest": "c" * 64},
    )
    time.sleep(0.01)
    for relative_path in corrected_outputs:
        output = workdir / relative_path
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"same-name local output\n")
    finish_route_step_attempt(
        state, binding, result={"status": "success", "returncode": 0})
    corrected = json.loads(json.dumps(route))
    corrected["steps"][0]["expected_outputs"] = corrected_outputs
    result = asyncio.run(_declare_execution_route(
        state,
        route=corrected,
        amendment_reason="correct the path base for the original local output",
        recovery_basis={
            "attempt_id": binding["attempt_id"],
            "failure_class": "expected_output",
            "diagnosis": "the declared path base did not match the local output",
            "evidence_refs": [],
        },
    ))
    assert result["status"] == "success", result
    return state, corrected, binding, output


def test_local_correction_preserves_verified_and_repoints_missing_output(
    tmp_path,
):
    state, _corrected, binding, _output = _corrected_local_route(
        tmp_path,
        original_outputs=[
            "stable/manifest.json", "declared/source.tar.gz",
        ],
        corrected_outputs=[
            "stable/manifest.json", "actual/source.tar.gz",
        ],
    )

    snapshot = build_route_snapshot(state)
    step = snapshot["steps"]["acquire"]
    assert snapshot["route_state"] == "complete"
    assert step["state"] == "verified"
    assert step["attempt_id"] == binding["attempt_id"]
    limitations = snapshot["correction_witness_limitations"]
    assert limitations[0]["verified_outputs"] == [
        "stable/manifest.json", "actual/source.tar.gz",
    ]


@pytest.mark.parametrize(
    "mutation",
    [
        "receipt_run_id",
        "witness_attempt_id",
        "corrected_route_hash",
        "original_route_ref",
        "non_dict_file",
        "self_asserted_regular_fifo",
        "both_reuse_kinds",
        "wrong_source_event",
    ],
)
def test_local_correction_reuse_revalidates_frozen_witness_bindings(
    tmp_path, monkeypatch, mutation,
):
    from copy import deepcopy
    from nodes.experiment.tools import execution_route as er

    state, _corrected, binding, _output = _corrected_local_route(tmp_path)
    route_id = _canonical_route_artifact_id(state)
    artifact_versions = state.artifact_versions

    def tampered_versions(artifact_id):
        versions = deepcopy(artifact_versions(artifact_id))
        if artifact_id != route_id:
            return versions
        latest = max(
            (version for version in versions
             if isinstance((version.get("metadata") or {}).get(
                 "validated_recovery_receipt"), dict)),
            key=lambda version: version["version"],
        )
        receipt = latest["metadata"]["validated_recovery_receipt"]
        witness = receipt["local_output_correction_witness"]
        if mutation == "receipt_run_id":
            receipt["run_id"] = "another-run"
        elif mutation == "witness_attempt_id":
            witness["attempt_id"] = "another-attempt"
        elif mutation == "corrected_route_hash":
            witness["corrected_route_content_hash"] = "0" * 64
        elif mutation == "original_route_ref":
            witness["original_route_ref"]["content_hash"] = "0" * 64
        elif mutation == "non_dict_file":
            witness["files"].append("not-a-file-record")
        elif mutation == "both_reuse_kinds":
            receipt["external_execution_reused"] = True
        elif mutation == "wrong_source_event":
            receipt["attempt_receipt"]["source_event"] = (
                "route_step_external_execution_verified")
        else:
            file_record = witness["files"][0]
            file_record["mode"] = 0o010644
            identity = er._local_file_record_identity(file_record)
            file_record["fingerprint"] = er._local_identity_fingerprint(
                file_record["path"], identity)
        return versions

    monkeypatch.setattr(state, "artifact_versions", tampered_versions)
    snapshot = build_route_snapshot(state)

    assert snapshot["steps"]["acquire"]["state"] == "pending"
    assert snapshot["steps"]["acquire"].get("attempt_id") != binding["attempt_id"]
    assert snapshot["correction_witness_limitations"] == []


def test_correction_disclosure_fails_closed_when_lineage_cannot_be_read(
    tmp_path, monkeypatch,
):
    from nodes.experiment.tools.execution_route import (
        route_correction_witness_disclosure,
    )

    state, _corrected, _binding, _output = _corrected_local_route(tmp_path)

    def unreadable_lineage(_artifact_id):
        raise RuntimeError("ledger read failed")

    monkeypatch.setattr(state, "artifact_versions", unreadable_lineage)

    with pytest.raises(RuntimeError, match="ledger read failed"):
        route_correction_witness_disclosure(state)


def test_corrected_attempt_survives_downstream_append_and_state_reopen(
    tmp_path,
):
    state, corrected, binding, _output = _corrected_local_route(tmp_path)
    appended = json.loads(json.dumps(corrected))
    appended["steps"].append({
        "id": "inspect",
        "goal": "inspect the downloaded archive",
        "after": ["acquire"],
        "action": {"tool": "safe_run_bash", "program": "tar"},
        "effects": [],
        "workdir_role": "managed_source_root",
        "expected_outputs": [],
    })
    amended = asyncio.run(_declare_execution_route(
        state,
        route=appended,
        amendment_reason="append a downstream inspection without changing acquire",
    ))
    assert amended["status"] == "success", amended
    snapshot = build_route_snapshot(state)
    assert snapshot["steps"]["acquire"]["state"] == "verified"
    assert snapshot["steps"]["acquire"]["attempt_id"] == binding["attempt_id"]
    assert snapshot["ready_step_ids"] == ["inspect"]

    reopened = _state(tmp_path, run_id=state.run_id)
    reopened_snapshot = build_route_snapshot(reopened)
    assert reopened_snapshot["steps"]["acquire"]["state"] == "verified"
    assert reopened_snapshot["steps"]["acquire"]["attempt_id"] == binding[
        "attempt_id"]

    changed_goal = json.loads(json.dumps(appended))
    changed_goal["steps"][0]["goal"] = "use the archive for a different intent"
    changed = asyncio.run(_declare_execution_route(
        state,
        route=changed_goal,
        amendment_reason="change the completed step's execution intent",
    ))
    assert changed["status"] == "success", changed
    assert build_route_snapshot(state)["steps"]["acquire"]["state"] == "pending"


def test_same_local_correction_is_idempotent_without_another_receipt(tmp_path):
    state, corrected, binding, _output = _corrected_local_route(tmp_path)
    route_id = _canonical_route_artifact_id(state)
    versions_before = state.artifact_versions(route_id)

    repeated = asyncio.run(_declare_execution_route(state, route=corrected))

    assert repeated["status"] == "success"
    assert repeated["already_declared"] is True
    assert len(state.artifact_versions(route_id)) == len(versions_before)
    snapshot = build_route_snapshot(state)
    assert snapshot["route_state"] == "complete"
    assert snapshot["steps"]["acquire"]["attempt_id"] == binding["attempt_id"]


def test_second_local_correction_preserves_the_first_lineage_head(tmp_path):
    state = _state(tmp_path)
    route = _route()
    route["steps"] = [route["steps"][0], {
        "id": "inspect",
        "goal": "inspect the downloaded archive",
        "after": ["acquire"],
        "action": {"tool": "safe_run_bash", "program": "tar"},
        "effects": ["workspace_write"],
        "workdir_role": "managed_source_root",
        "expected_outputs": ["declared/report.json"],
    }]
    assert asyncio.run(_declare_execution_route(state, route=route))[
        "status"] == "success"
    workdir = state.root / "outputs" / "experiment" / "runtime" / "source"
    workdir.mkdir(parents=True)

    def execute_missing(step_id, program, output, digest):
        decision = resolve_execution_context(state, {
            "tool": "safe_run_bash",
            "program": program,
            "route_step_id": step_id,
            "read_only": False,
            "observed_effects": (
                ["network_access", "workspace_write"]
                if step_id == "acquire" else ["workspace_write"]
            ),
            "workdir_roles": ["managed_source_root"],
        })
        decision.update({
            "workdir_role_observed": True,
            "workdir_resolution_status": "resolved",
            "resolved_workdir": str(workdir),
        })
        binding = begin_route_step_attempt(
            state, decision, tool="safe_run_bash",
            action={"payload_digest": digest * 64},
        )
        time.sleep(0.01)
        path = workdir / output
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(step_id, encoding="utf-8")
        outcome = finish_route_step_attempt(
            state, binding, result={"status": "success", "returncode": 0})
        assert outcome["failure_class"] == "expected_outputs_missing"
        return binding

    acquire = execute_missing(
        "acquire", "curl", "actual/source.tar.gz", "a")
    corrected_a = json.loads(json.dumps(route))
    corrected_a["steps"][0]["expected_outputs"] = ["actual/source.tar.gz"]
    first = asyncio.run(_declare_execution_route(
        state,
        route=corrected_a,
        amendment_reason="correct acquire output path",
        recovery_basis={
            "attempt_id": acquire["attempt_id"],
            "failure_class": "expected_output",
            "diagnosis": "acquire used the wrong declared path base",
            "evidence_refs": [],
        },
    ))
    assert first["status"] == "success", first

    inspect = execute_missing("inspect", "tar", "actual/report.json", "b")
    corrected_b = json.loads(json.dumps(corrected_a))
    corrected_b["steps"][1]["expected_outputs"] = ["actual/report.json"]
    second = asyncio.run(_declare_execution_route(
        state,
        route=corrected_b,
        amendment_reason="correct inspect output path",
        recovery_basis={
            "attempt_id": inspect["attempt_id"],
            "failure_class": "expected_output",
            "diagnosis": "inspect used the wrong declared path base",
            "evidence_refs": [],
        },
    ))
    assert second["status"] == "success", second

    snapshot = build_route_snapshot(state)
    assert snapshot["route_state"] == "complete"
    assert snapshot["steps"]["acquire"]["attempt_id"] == acquire["attempt_id"]
    assert snapshot["steps"]["inspect"]["attempt_id"] == inspect["attempt_id"]
    assert {
        item["attempt_id"]
        for item in snapshot["correction_witness_limitations"]
    } == {acquire["attempt_id"], inspect["attempt_id"]}


def test_corrected_attempt_is_not_reused_for_a_third_output_contract(tmp_path):
    state, corrected, binding, _output = _corrected_local_route(tmp_path)
    third = json.loads(json.dumps(corrected))
    third["steps"][0]["expected_outputs"] = ["third/source.tar.gz"]
    amended = asyncio.run(_declare_execution_route(
        state,
        route=third,
        amendment_reason="declare a genuinely different third output contract",
    ))
    assert amended["status"] == "success", amended
    snapshot = build_route_snapshot(state)
    assert snapshot["steps"]["acquire"]["state"] == "pending"
    assert snapshot["steps"]["acquire"].get("attempt_id") != binding["attempt_id"]
    assert snapshot.get("correction_witness_limitations") == []


def test_operation_triplet_projects_exact_authoritative_limitation(tmp_path):
    from nodes.experiment.tools.operation_completion import (
        _record_operation_completion,
    )

    state, _corrected, _binding, output = _corrected_local_route(tmp_path)
    state.hook_state["_request_mode"] = "operation"
    expected = build_route_snapshot(state)["correction_witness_limitations"]
    completion = asyncio.run(_record_operation_completion(
        state,
        task_kind="file_delivery",
        objective="retain the corrected local output",
        outcome="success",
        checks=[{
            "name": "managed_command_returncode",
            "passed": True,
            "evidence": {"returncode": 0},
        }],
        artifact_paths=[str(output)],
    ))
    assert completion["status"] == "success", completion

    artifact_ids = [
        completion["raw_results_artifact_id"],
        completion["clean_results_artifact_id"],
        completion["experiment_log_artifact_id"],
    ]
    for artifact_id in artifact_ids:
        record = state.read_artifact(artifact_id)
        metadata = record["metadata"]
        assert metadata["route_correction_witness_limitations"] == expected
        checks = metadata["operation_closure_input"]["checks"]
        limitation = [
            item for item in checks
            if item["name"]
            == "local_exact_output_correction_witness_limitations"
        ]
        assert limitation == [{
            "name": "local_exact_output_correction_witness_limitations",
            "passed": True,
            "evidence": {"corrections": expected},
        }]
    log = state.read_artifact(completion["experiment_log_artifact_id"])[
        "content"]
    assert "run_shared_workdir_time_window" in log
    assert '"semantic_identity_independently_verified": false' in log


@pytest.mark.parametrize(
    "artifact_type", ["raw_results", "clean_results", "experiment_log"],
)
@pytest.mark.parametrize("mutation", ["omitted", "changed_source", "true"])
def test_operation_triplet_reaudit_rejects_limitation_mutation(
    tmp_path, monkeypatch, artifact_type, mutation,
):
    from nodes.experiment.tools import operation_completion as oc

    state, _corrected, _binding, output = _corrected_local_route(tmp_path)
    state.hook_state["_request_mode"] = "operation"
    call = {
        "state": state,
        "task_kind": "file_delivery",
        "objective": "retain the corrected local output",
        "outcome": "success",
        "checks": [{
            "name": "managed_command_returncode",
            "passed": True,
            "evidence": {"returncode": 0},
        }],
        "artifact_paths": [str(output)],
    }
    completion = asyncio.run(oc._record_operation_completion(**call))
    assert completion["status"] == "success", completion
    target_id = completion[f"{artifact_type}_artifact_id"]
    closure_metadata = oc._closure_metadata

    def mutated_metadata(item):
        metadata = json.loads(json.dumps(closure_metadata(item)))
        if item.get("id") != target_id:
            return metadata
        checks = metadata["operation_closure_input"]["checks"]
        limitation_check = next(
            check for check in checks
            if check["name"]
            == "local_exact_output_correction_witness_limitations"
        )
        if mutation == "omitted":
            metadata.pop("route_correction_witness_limitations")
            checks.remove(limitation_check)
        elif mutation == "changed_source":
            metadata["route_correction_witness_limitations"][0][
                "validated_recovery_receipt_source"
            ]["validated_recovery_receipt_sha256"] = "0" * 64
        else:
            limitation_check["evidence"]["corrections"][0][
                "semantic_identity_independently_verified"
            ] = True
        return metadata

    monkeypatch.setattr(oc, "_closure_metadata", mutated_metadata)
    rejected = asyncio.run(oc._record_operation_completion(**call))

    assert rejected["error_code"] == "route_correction_disclosure_mismatch"
    assert artifact_type in rejected["error"]


@pytest.mark.parametrize(
    "artifact_type", ["raw_results", "clean_results", "experiment_log"],
)
@pytest.mark.parametrize(
    "mutation",
    ["missing_metadata", "missing_named_check", "malformed_closure_input"],
)
@pytest.mark.parametrize("frozen", [True, False])
def test_partial_operation_closure_rejects_existing_disclosure_damage(
    tmp_path, monkeypatch, artifact_type, mutation, frozen,
):
    from copy import deepcopy
    from nodes.experiment.tools import operation_completion as oc

    state, _corrected, _binding, output = _corrected_local_route(tmp_path)
    state.hook_state["_request_mode"] = "operation"
    call = {
        "state": state,
        "task_kind": "file_delivery",
        "objective": "retain the corrected local output",
        "outcome": "success",
        "checks": [{
            "name": "managed_command_returncode",
            "passed": True,
            "evidence": {"returncode": 0},
        }],
        "artifact_paths": [str(output)],
    }
    completed = asyncio.run(oc._record_operation_completion(**call))
    assert completed["status"] == "success", completed
    inventory = oc._closure_state(
        state, f"{state.run_id}:operation")["inventory"]
    damaged = deepcopy(inventory[artifact_type][0])
    metadata = damaged["record"]["metadata"]
    if not frozen:
        metadata.pop("frozen", None)
        metadata.pop("frozen_at", None)
    if mutation == "missing_metadata":
        metadata.pop("route_correction_witness_limitations")
    elif mutation == "malformed_closure_input":
        metadata["operation_closure_input"] = "damaged-ledger-value"
    else:
        metadata["operation_closure_input"]["checks"] = [
            check
            for check in metadata["operation_closure_input"]["checks"]
            if check["name"]
            != "local_exact_output_correction_witness_limitations"
        ]
    partial_inventory = {
        kind: ([damaged] if kind == artifact_type else [])
        for kind in ("raw_results", "clean_results", "experiment_log")
    }
    monkeypatch.setattr(
        oc,
        "_closure_state",
        lambda _state, _closure_id: {
            "kind": "partial", "inventory": partial_inventory,
        },
    )

    rejected = asyncio.run(oc._record_operation_completion(**call))

    assert rejected["error_code"] == "route_correction_disclosure_mismatch"
    assert artifact_type in rejected["error"]


def test_operation_caller_cannot_forge_local_correction_limitation(tmp_path):
    from nodes.experiment.tools.operation_completion import (
        _record_operation_completion,
    )

    state, _corrected, _binding, output = _corrected_local_route(tmp_path)
    state.hook_state["_request_mode"] = "operation"
    forged = asyncio.run(_record_operation_completion(
        state,
        task_kind="file_delivery",
        objective="forge a stronger local provenance claim",
        outcome="success",
        checks=[{
            "name": "local_exact_output_correction_witness_limitations",
            "passed": True,
            "evidence": {
                "semantic_identity_independently_verified": True,
            },
        }],
        artifact_paths=[str(output)],
    ))

    assert forged["error_code"] == "reserved_verification_check"
    assert state.list_artifacts("experiment_log") == []


@pytest.mark.parametrize("mutation", ["omitted", "true", "changed_scope"])
def test_scientific_log_disclosure_rejects_omission_or_mutation(
    tmp_path, mutation,
):
    from nodes.experiment.tools.contract_audit import (
        _experiment_log_correction_disclosure_failures,
    )
    from nodes.experiment.tools.execution_route import (
        route_correction_witness_disclosure,
    )

    state, _corrected, _binding, _output = _corrected_local_route(tmp_path)
    disclosure = route_correction_witness_disclosure(state)
    expected = disclosure["limitations"]
    expected_check = disclosure["check"]
    metadata = {
        "route_correction_witness_limitations": json.loads(json.dumps(expected)),
        "route_correction_witness_limitation_check": json.loads(json.dumps(
            expected_check)),
    }
    if mutation == "omitted":
        metadata = {}
    elif mutation == "true":
        metadata["route_correction_witness_limitation_check"]["evidence"][
            "corrections"][0][
                "semantic_identity_independently_verified"] = True
    else:
        metadata["route_correction_witness_limitations"][0][
            "witness_scope"] = "per_attempt_provenance"
    content = json.dumps(
        expected_check, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"),
    )

    failures = _experiment_log_correction_disclosure_failures(
        state, {"metadata": metadata, "content": content})

    assert failures


def test_scientific_log_accepts_exact_receipt_projection(tmp_path):
    from nodes.experiment.tools.contract_audit import (
        _experiment_log_correction_disclosure_failures,
    )
    from nodes.experiment.tools.execution_route import (
        route_correction_witness_disclosure,
    )

    state, _corrected, _binding, _output = _corrected_local_route(tmp_path)
    disclosure = route_correction_witness_disclosure(state)
    expected = disclosure["limitations"]
    expected_check = disclosure["check"]
    record = {
        "metadata": {
            "route_correction_witness_limitations": expected,
            "route_correction_witness_limitation_check": expected_check,
        },
        "content": json.dumps(
            expected_check, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"),
        ),
    }

    assert _experiment_log_correction_disclosure_failures(state, record) == {}


def test_terminal_failure_metadata_cannot_bypass_scientific_disclosure_gate(
    tmp_path, monkeypatch,
):
    from shared.tools.library.artifacts_extra import (
        FREEZE_GATES,
        _freeze_artifact,
    )

    state, _corrected, _binding, _output = _corrected_local_route(tmp_path)
    log_id = state.save_artifact(
        "experiment_log",
        "forged_terminal_failure",
        "caller-authored log with no correction limitation disclosure",
        metadata={
            "auto_generated": True,
            "terminal_failure_record": True,
        },
    )["id"]
    gate = FREEZE_GATES["experiment_log"]
    monkeypatch.setitem(
        gate.__globals__,
        "load_run_contract",
        lambda _state: {"execution_mode": "scientific"},
    )

    frozen = asyncio.run(_freeze_artifact(
        state=state,
        artifact_id=log_id,
        reason="attempt caller-controlled terminal-record bypass",
    ))

    assert frozen["status"] == "error", frozen
    assert "route_correction_witness_limitations" in frozen["failed_checks"]
    assert "route_correction_witness_limitation_check" in frozen["failed_checks"]
    assert state.read_artifact(log_id)["metadata"].get("frozen") is not True


def test_terminal_recovery_projects_and_freezes_exact_correction_disclosure(
    tmp_path, monkeypatch,
):
    from types import SimpleNamespace

    from nodes.experiment import hooks
    from nodes.experiment.tools.execution_route import (
        route_correction_witness_disclosure,
    )
    from shared.tools.library.artifacts_extra import FREEZE_GATES
    from tools import run_contract as hook_run_contract

    state, _corrected, _binding, _output = _corrected_local_route(tmp_path)
    expected = route_correction_witness_disclosure(state)
    scientific_contract = {
        "execution_mode": "scientific",
        "run_role": "secondary",
        "exclusion_reason": "terminal producer disclosure regression",
    }
    monkeypatch.setattr(hooks, "_is_operational_run", lambda _state: False)
    monkeypatch.setattr(
        hook_run_contract, "load_run_contract",
        lambda _state: scientific_contract,
    )
    gate = FREEZE_GATES["experiment_log"]
    monkeypatch.setitem(
        gate.__globals__, "load_run_contract",
        lambda _state: scientific_contract,
    )

    asyncio.run(hooks._secondary_experiment_log_recovery_on_end(
        SimpleNamespace(state=state, turn=0),
        SimpleNamespace(status="failed"),
    ))

    logs = state.list_artifacts("experiment_log")
    assert len(logs) == 1
    record = state.read_artifact(logs[0]["id"])
    assert record["metadata"]["frozen"] is True
    assert record["metadata"][
        "route_correction_witness_limitations"] == expected["limitations"]
    assert record["metadata"][
        "route_correction_witness_limitation_check"] == expected["check"]
    exact = json.dumps(
        expected["check"], ensure_ascii=False, sort_keys=True,
        separators=(",", ":"),
    )
    assert exact in record["content"]


def test_final_scientific_audit_rechecks_frozen_correction_disclosure(
    tmp_path, monkeypatch,
):
    from nodes.experiment.tools import contract_audit as ca
    from nodes.experiment.tools.execution_route import (
        route_correction_witness_disclosure,
    )

    state, corrected, _binding, _output = _corrected_local_route(tmp_path)
    disclosure = route_correction_witness_disclosure(state)
    exact = json.dumps(
        disclosure["check"], ensure_ascii=False, sort_keys=True,
        separators=(",", ":"),
    )
    changed = json.loads(json.dumps(corrected))
    changed["steps"][0]["goal"] = "a different scientific intent"
    amended = asyncio.run(_declare_execution_route(
        state,
        route=changed,
        amendment_reason="change the corrected step intent",
    ))
    assert amended["status"] == "success", amended
    assert route_correction_witness_disclosure(state)["limitations"] == []
    saved = state.save_artifact(
        "experiment_log",
        "scientific_correction_disclosure",
        "# Result\n" + exact + "\n",
        metadata={
            "route_correction_witness_limitations": disclosure["limitations"],
            "route_correction_witness_limitation_check": disclosure["check"],
        },
    )
    state.mark_frozen(saved["id"])
    monkeypatch.setattr(
        ca, "load_run_contract",
        lambda _state: {"execution_mode": "scientific"},
    )
    monkeypatch.setattr(
        ca, "audit_experiment_log_integrity",
        lambda _state: {"passed": True, "reason": "base integrity passed"},
    )

    audit = ca.audit_experiment_contract(state)

    integrity = audit["experiment_log_integrity"]
    assert integrity["passed"] is False
    assert integrity["correction_disclosure_passed"] is False
    assert "route_correction_witness_limitations" in (
        integrity["correction_disclosure_failures"]
    )

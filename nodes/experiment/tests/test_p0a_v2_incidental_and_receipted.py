"""P0a v2: the compatibility lane accounts for what it lets through.

Three defects found by the 17 号 review of the first P0a delivery, each pinned
in both directions here:

B1  After a route-backed build step satisfied the run's real-execution
    obligation, a stray low-risk write (``cp`` after ``make``) used to be
    censused as ``ineligible`` and turned ROC(build) into
    ``operation_real_execution_required`` with no exit — while the frozen base
    closed the same sequence as success.  Such writes are now ``incidental``:
    visible, accounted, and neither satisfying nor poisoning the obligation.
    A stray write with **no** route-backed execution behind it still fails.

B2  ``safe_write_file`` never entered the census, so a pending run could write
    a fake build product, declare it, and mint a green closure.  Writes are now
    admitted as ``receipted`` support actions carrying ``write_target``; ROC(build)
    refuses a declared product that the census shows was written by the tool.
    ``fetch_resource``'s own durable receipts are projected as ``receipted``.

B3  A run started before the upgrade only has a v1 acceptance receipt, which
    cannot express typed none; its no-prereg science is refused as pending on
    purpose — the refusal now says so and names the only exit.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from core.state import State
from nodes.experiment.tests._mechanical_execution import (
    record_completed_local_mechanical_action,
)
from nodes.experiment.tools import execution_action_census as census
from nodes.experiment.tools import (
    execution_route,
    operation_completion,
    run_contract,
    safe_bash,
)


def _operation_state(tmp_path: Path, assignment: dict | None) -> State:
    state = State.new("experiment", tmp_path)
    node_inputs = {"experiment_focus": "Build and smoke-test the local toolchain."}
    if assignment is not None:
        node_inputs["prereg_assignment"] = assignment
    state.hook_state["node_inputs"] = node_inputs
    classified = asyncio.run(run_contract._classify_experiment_scope(
        state, scope="operation", operation_category="toolchain_build", reason="build only"))
    assert classified["status"] == "success", classified
    return state


def _smoke_executable(state: State) -> Path:
    return Path(state.root) / "toolchain-smoke"


def _produce_smoke_executable(state: State):
    """Materialize the build product; v3 requires this to happen *inside* the
    satisfying attempt's window, so callers pass it as ``produce=``."""
    def _write() -> None:
        executable = _smoke_executable(state)
        executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        executable.chmod(0o755)
    return _write


def _close_build(state: State, *, artifact_paths: list[str] | None = None) -> dict:
    executable = _smoke_executable(state)
    if not executable.exists():
        # No attempt produced it (e.g. the no-route case): write it now; v3 will
        # refuse it as unattributed, which is exactly what those tests expect.
        _produce_smoke_executable(state)()
    return asyncio.run(operation_completion._record_operation_completion(
        state, task_kind="toolchain_build", objective="build smoke", outcome="success",
        checks=[{"name": "minimal_run", "passed": True, "evidence": {"returncode": 0}}],
        executable_paths=[str(executable)],
        **({"artifact_paths": artifact_paths} if artifact_paths else {}),
    ))


def _kinds(state: State) -> list[tuple[str, str]]:
    obligation = census.operation_execution_obligation(state)
    return [
        (action["tool"], action["compatibility_kind"])
        for action in obligation["action_census"]["actions"]
    ]


# ── B1 ──────────────────────────────────────────────────────────────────────

def test_stray_low_risk_write_after_satisfied_route_is_incidental_and_keeps_success(tmp_path):
    state = _operation_state(tmp_path, {"kind": "none", "reason": "typed none"})
    record_completed_local_mechanical_action(
        state, produce=_produce_smoke_executable(state), expected_outputs=["toolchain-smoke"])
    workspace = Path(safe_bash.experiment_output_dir(state, "runtime", create=True))
    (workspace / "src.txt").write_text("x\n")
    result = asyncio.run(safe_bash._safe_run_bash(
        state, f"cp {workspace}/src.txt {workspace}/dst.txt", cwd=str(workspace)))
    assert result["status"] == "success", result
    obligation = census.operation_execution_obligation(state)
    assert obligation["passed"] is True, obligation["failure_reasons"]
    assert obligation["compatibility_lane"] == "route_backed_mechanical"
    assert ("safe_run_bash", "incidental") in _kinds(state)
    assert _close_build(state)["status"] == "success"


def test_stray_low_risk_write_after_satisfied_route_in_pending_run_keeps_success(tmp_path):
    state = _operation_state(tmp_path, None)  # omitted → pending compatibility lane
    record_completed_local_mechanical_action(
        state, produce=_produce_smoke_executable(state), expected_outputs=["toolchain-smoke"])
    workspace = Path(safe_bash.experiment_output_dir(state, "runtime", create=True))
    (workspace / "src.txt").write_text("x\n")
    asyncio.run(safe_bash._safe_run_bash(
        state, f"mkdir -p {workspace}/sub && cp {workspace}/src.txt {workspace}/sub/dst.txt",
        cwd=str(workspace)))
    assert _close_build(state)["status"] == "success"


def test_incidental_never_satisfies_the_obligation_on_its_own(tmp_path):
    """No route-backed execution behind it: the same stray write must still fail."""
    state = _operation_state(tmp_path, {"kind": "none", "reason": "typed none"})
    workspace = Path(safe_bash.experiment_output_dir(state, "runtime", create=True))
    (workspace / "src.txt").write_text("x\n")
    asyncio.run(safe_bash._safe_run_bash(
        state, f"cp {workspace}/src.txt {workspace}/dst.txt", cwd=str(workspace)))
    obligation = census.operation_execution_obligation(state)
    assert obligation["passed"] is False
    assert obligation["compatibility_lane"] is None
    assert obligation["real_execution_obligation_satisfied"] is False
    closed = _close_build(state)
    assert closed["status"] == "error", closed
    assert closed["error_code"] in {
        "operation_real_execution_required", "execution_route_incomplete",
    }, closed


# ── B2 ──────────────────────────────────────────────────────────────────────

def _pending_state_with_run_root(tmp_path: Path) -> tuple[State, Path]:
    state = _operation_state(tmp_path, None)
    run_root = Path(state.root) / "work" / "CTRL"
    run_root.mkdir(parents=True, exist_ok=True)
    state.hook_state["path_roles"] = {
        "experiment_root": str(run_root.parent), "run_root": str(run_root)}
    return state, run_root


def _route_backed_tar(state: State, run_root: Path, *, produce=None,
                      expected_outputs: list[str] | None = None) -> None:
    """One route-backed local step; ``produce`` runs inside the attempt and
    ``expected_outputs`` (relative to run_root) puts its products on the receipt."""
    route = {
        "schema_version": 2, "goal": "Unpack the source archive.",
        "evidence_refs": ["test:p0a-v2"],
        "steps": [{
            "id": "unpack", "goal": "Unpack the archive.", "after": [],
            "action": {"tool": "safe_run_bash", "program": "tar"},
            "effects": ["process_tree", "workspace_write"],
            "workdir_role": "run_root", "expected_outputs": list(expected_outputs or []),
        }],
    }
    declared = asyncio.run(execution_route._declare_execution_route(state, route=route))
    assert declared["status"] == "success", declared
    action = {
        "tool": "safe_run_bash", "program": "tar", "route_step_id": "unpack",
        "read_only": False, "dry_run": False,
        "observed_effects": ["process_tree", "workspace_write"],
        "workdir_roles": ["run_root"], "payload_digest": "d" * 64,
    }
    decision = dict(execution_route.resolve_execution_context(state, action))
    decision.update({
        "workdir_role_observed": True, "workdir_resolution_status": "explicit",
        "resolved_workdir": str(run_root)})
    assert execution_route.enforce_execution_route(state, action, decision) is None
    binding = execution_route.begin_route_step_attempt(
        state, decision, tool="safe_run_bash", action=action)
    token = census.begin_execution_action(state, action, decision, route_binding=binding)
    assert token["status"] == "success", token
    if produce is not None:
        produce()
    settled = census.settle_execution_action(
        state, token, payload_spawned=True, job_submitted=False,
        proof_source="test_boundary", result={"status": "success", "returncode": 0})
    assert settled["passed"], settled
    finished = execution_route.finish_route_step_attempt(
        state, binding, result={"status": "success", "returncode": 0})
    assert finished.get("outcome") == "success", finished


def test_pending_run_write_is_admitted_as_receipted_with_its_target(tmp_path):
    state, run_root = _pending_state_with_run_root(tmp_path)
    target = run_root / "notes.txt"
    written = asyncio.run(safe_bash._safe_write_file(state, str(target), "hello\n"))
    assert written["status"] == "success", written
    assert target.read_text() == "hello\n"
    actions = census.reduce_execution_action_census(state)["actions"]
    writes = [a for a in actions if a["tool"] == "safe_write_file"]
    assert len(writes) == 1, actions
    assert writes[0]["compatibility_kind"] == "receipted"
    assert writes[0]["terminal_status"] == "success"
    assert Path(writes[0]["write_target"]).resolve() == target.resolve()


def test_declared_build_product_written_by_tool_is_refused(tmp_path):
    state, run_root = _pending_state_with_run_root(tmp_path)
    _route_backed_tar(state, run_root)
    fake = run_root / "bin" / "solver"
    written = asyncio.run(safe_bash._safe_write_file(
        state, str(fake), "#!/bin/sh\necho fake\n"))
    assert written["status"] == "success", written
    closed = asyncio.run(operation_completion._record_operation_completion(
        state, task_kind="build", objective="build the assigned tool", outcome="success",
        checks=[{"name": "binary_present", "passed": True, "evidence": {"path": str(fake)}}],
        artifact_paths=[str(fake)]))
    assert closed["status"] == "error", closed
    # v4: no satisfying attempt's frozen output receipt lists the fake solver.
    assert closed["error_code"] == "operation_build_artifact_not_produced_by_satisfying_attempt"
    assert closed["unattributed_paths"][0]["reason"] == "no_producer_receipt"
    assert [Path(p["path"]).resolve() for p in closed["unattributed_paths"]] == [fake.resolve()]
    assert state.list_artifacts("raw_results") == []


def test_written_note_beside_a_real_build_product_is_fine(tmp_path):
    """The rule matches declared paths only; unrelated tool writes do not poison."""
    state, run_root = _pending_state_with_run_root(tmp_path)
    product = run_root / "bin" / "solver"

    def _produce() -> None:
        product.parent.mkdir(parents=True, exist_ok=True)
        product.write_text("#!/bin/sh\nexit 0\n")
        product.chmod(0o755)

    # v4: the real product is declared and produced by the satisfying attempt (so
    # its receipt lists it); the note written by the tool afterwards is accounted
    # but irrelevant.
    _route_backed_tar(state, run_root, produce=_produce, expected_outputs=["bin/solver"])
    note = run_root / "BUILD_NOTES.md"
    asyncio.run(safe_bash._safe_write_file(state, str(note), "notes\n"))
    closed = asyncio.run(operation_completion._record_operation_completion(
        state, task_kind="build", objective="build the assigned tool", outcome="success",
        checks=[{"name": "binary_present", "passed": True, "evidence": {"path": str(product)}}],
        artifact_paths=[str(product)]))
    assert closed["status"] == "success", closed


def test_pending_lane_admits_support_tools_and_rejects_scientific_effects(tmp_path):
    state, _run_root = _pending_state_with_run_root(tmp_path)
    for tool, policy, effects in (
        ("fetch_resource", "controlled_resource_fetch", ["network_access", "workspace_write"]),
        ("dispatch_data_request", "managed_child_dispatch", ["workspace_write"]),
        ("safe_write_file", "low_risk_effectful", ["workspace_write"]),
    ):
        block = census.pending_operation_action_block(
            state,
            {"tool": tool, "program": tool, "read_only": False, "dry_run": False,
             "observed_effects": effects},
            {"tool": tool, "decision": "route_not_required", "policy": policy,
             "effective_effects": effects},
        )
        assert block is None, (tool, block)
    scientific = census.pending_operation_action_block(
        state,
        {"tool": "fetch_resource", "program": "fetch_resource", "read_only": False,
         "dry_run": False, "observed_effects": ["scientific_execution"]},
        {"tool": "fetch_resource", "decision": "route_not_required",
         "policy": "controlled_resource_fetch",
         "effective_effects": ["scientific_execution"]},
    )
    assert scientific is not None
    assert scientific["reason"] == "scientific_execution_requires_exact_prereg_assignment"


def test_fetch_receipts_are_projected_as_receipted_actions(tmp_path):
    state, run_root = _pending_state_with_run_root(tmp_path)
    state.append_transcript(
        "resource_acquisition_completed", kind="file", url="https://example.org/x",
        destination=str(run_root / "x.tgz"), sha256="a" * 64)
    state.append_transcript(
        "resource_acquisition_failed", kind="file", url="https://example.org/y",
        destination=str(run_root / "y.tgz"), error="boom", error_code="fetch_failed")
    reduced = census.reduce_execution_action_census(state)
    projected = [a for a in reduced["actions"] if a["tool"] == "fetch_resource"]
    assert [a["terminal_status"] for a in projected] == ["success", "failed"]
    assert {a["compatibility_kind"] for a in projected} == {"receipted"}
    obligation = census.operation_execution_obligation(state)
    assert obligation["compatibility_lane"] == "route_exempt_receipted_support"
    assert obligation["real_execution_obligation_satisfied"] is False


# ── B3 ──────────────────────────────────────────────────────────────────────

def test_legacy_v1_receipt_refusal_names_the_only_exit():
    receipt = {"schema_version": 1, "receipt_digest": "d" * 64}
    payload = run_contract._pending_scientific_block_payload(
        receipt, signal_source="classification")
    assert payload["error_code"] == "prereg_assignment_required"
    assert payload["legacy_run"] is True
    assert "升级前开始" in payload["error"]
    assert "新开一个 run" in payload["error"]
    modern = run_contract._pending_scientific_block_payload(
        {"schema_version": 2}, signal_source="classification")
    assert "legacy_run" not in modern
    assert "升级前开始" not in modern["error"]


def test_accounted_action_with_unknown_terminal_fails_the_obligation(tmp_path):
    """An accounted (receipted/incidental) action is only accounted once settled.

    Mutation MH from the v2 self-review: dropping the terminal check in
    ``_accounted_failures`` turned 0 tests red, so this pins it directly.
    """
    state, run_root = _pending_state_with_run_root(tmp_path)
    _route_backed_tar(state, run_root)
    action = {
        "tool": "safe_write_file", "program": "write_file", "read_only": False,
        "dry_run": False, "observed_effects": ["workspace_write"],
        "workdir_roles": ["run_root"], "write_target": str(run_root / "late.txt"),
    }
    decision = {
        "tool": "safe_write_file", "decision": "route_not_required",
        "policy": "low_risk_effectful", "read_only": False, "dry_run": False,
        "effective_effects": ["workspace_write"],
    }
    token = census.begin_execution_action(state, action, decision)
    assert token["status"] == "success", token
    settled = census.settle_execution_action(
        state, token, payload_spawned=False, job_submitted=False,
        proof_source="test_boundary", result={"status": "no_such_status"})
    assert settled is not None
    reduced = census.reduce_execution_action_census(state)
    write = next(a for a in reduced["actions"] if a["tool"] == "safe_write_file")
    assert write["compatibility_kind"] == "receipted"
    assert write["terminal_status"] == "unknown", write
    obligation = census.operation_execution_obligation(state)
    assert obligation["passed"] is False
    assert "accounted_action_terminal_unknown" in obligation["failure_reasons"]

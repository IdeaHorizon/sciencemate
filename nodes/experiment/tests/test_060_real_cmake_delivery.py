"""Real local CMake actions through the v3 delivery boundary.

These tests deliberately avoid the LLM, UI, networking, and managed-job
paths.  The subprocess is still real: ``safe_run_bash`` configures, builds,
and runs the checked-in smoke project before ``finish`` evaluates the frozen
obligations and the read-only delivery audit checks the sealed triplet.
"""
from __future__ import annotations

import asyncio
import hashlib
import shlex
import shutil
from pathlib import Path

import pytest

from core.state import State
from nodes.experiment.tools import (
    contract_audit,
    finish,
    ledger,
    run_bootstrap,
    safe_bash,
)
from nodes.experiment.tools.path_roles import collect_path_roles


FIXTURE_SOURCE = (
    Path(__file__).resolve().parents[1] / "fixtures" / "toolchain_smoke_src"
)
SMOKE_MARKER = "HF_TOOLCHAIN_SMOKE_OK"


def _run(awaitable):
    return asyncio.run(awaitable)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _require_local_toolchain() -> None:
    missing = [tool for tool in ("cmake", "cc") if shutil.which(tool) is None]
    if missing:
        pytest.skip("local CMake smoke requires: " + ", ".join(missing))


def _roots(state: State) -> dict[str, Path]:
    return {
        role.role: Path(role.path)
        for role in collect_path_roles(state)
        if role.writable and not role.container_only
    }


def _state(tmp_path: Path, *, source_baseline: Path | None = None) -> State:
    state = State.new(
        node_type="experiment",
        base_dir=tmp_path / "runs",
        project_id="real-cmake-delivery",
        session_id="owner",
    )
    roots = _roots(state)
    binary = roots["build_root"] / "hf_toolchain_smoke"
    marker = roots["run_root"] / "smoke_stdout.txt"
    inputs = {
        "prereg_assignment": {
            "kind": "none",
            "reason": "local operation has no governing preregistration",
        },
        "scope": "operation",
        "operation_category": "toolchain_build",
        "experiment_spec": "Configure, build, and run the local CMake smoke project.",
        "required_outputs": [
            {
                "id": "smoke_binary",
                "kind": "file",
                "path": str(binary),
            },
            {
                "id": "smoke_marker_file",
                "kind": "file",
                "path": str(marker),
            },
            {
                "id": "smoke_check",
                "kind": "check",
                "command": shlex.quote(str(binary)),
                "expect_exit": 0,
                "expect_stdout_contains": SMOKE_MARKER,
            },
        ],
    }
    if source_baseline is not None:
        inputs["path_roles"] = {
            "source_baseline_root": {
                "path": str(source_baseline.resolve()),
                "writable": False,
            },
        }
    state.hook_state["node_inputs"] = inputs
    established = _run(run_bootstrap.establish(state))
    assert ledger.bootstrap_phase(state) == "ready", established
    return state


def _cmake_command(source: Path, build: Path, binary: Path, marker: Path) -> str:
    quote = lambda path: shlex.quote(str(path))
    return (
        f"cmake -S {quote(source)} -B {quote(build)}"
        f" && cmake --build {quote(build)} --parallel 1"
        f" && {quote(binary)} > {quote(marker)}"
    )


def _assert_frozen_triplet(state: State, result: dict) -> None:
    for artifact_type in ("raw_results", "clean_results", "experiment_log"):
        record = state.read_artifact(result["artifact_ids"][artifact_type])
        assert record is not None
        assert record["metadata"]["frozen"] is True


def test_real_cmake_action_finishes_and_audits_without_recompiling(tmp_path: Path):
    _require_local_toolchain()
    source = FIXTURE_SOURCE.resolve()
    assert source.is_dir()
    source_hashes_before = {
        name: _sha256(source / name) for name in ("CMakeLists.txt", "main.c")
    }
    state = _state(tmp_path, source_baseline=source)
    roots = _roots(state)
    build = roots["build_root"]
    run_root = roots["run_root"]
    binary = build / "hf_toolchain_smoke"
    marker = run_root / "smoke_stdout.txt"

    executed = _run(
        safe_bash._safe_run_bash(
            state,
            cmd=_cmake_command(source, build, binary, marker),
            cwd=str(run_root),
            timeout=120,
        )
    )

    assert executed.get("status") == "success", executed
    assert executed.get("returncode") == 0, executed
    assert binary.is_file()
    assert marker.read_text(encoding="utf-8").strip() == SMOKE_MARKER
    assert (build / "CMakeCache.txt").is_file(), "configure must have run"
    assert source_hashes_before == {
        name: _sha256(source / name) for name in source_hashes_before
    }

    led = ledger.open_ledger(state)
    rows = led.records()
    cmake_starts = [
        row for row in rows
        if row.get("kind") == "action_started"
        and row.get("tool") == "safe_run_bash"
        and "cmake -S" in str(row.get("command") or "")
    ]
    assert len(cmake_starts) == 1, cmake_starts
    action_id = cmake_starts[0]["action_id"]
    observation = next(
        row for row in rows
        if row.get("kind") == "observation" and row.get("action_id") == action_id
    )
    changed = set(observation.get("created") or []) | set(
        observation.get("modified") or []
    )
    assert {str(binary), str(marker)} <= changed
    assert (observation.get("identities") or {})[str(binary)]["after"]["kind"] == "file"
    assert (observation.get("identities") or {})[str(marker)]["after"]["kind"] == "file"

    first = _run(
        finish._finish(
            state,
            outcome="success",
            summary="CMake configure, build, and smoke run passed.",
            deliverables=[
                {"path": str(binary), "obligation_id": "smoke_binary"},
                {"path": str(marker), "obligation_id": "smoke_marker_file"},
            ],
        )
    )
    assert first["status"] == "success", first
    assert first["required_satisfied"] == "3/3"
    assert first["gaps"] == [] and first["contradictions"] == []
    _assert_frozen_triplet(state, first)
    audit = contract_audit.audit_experiment_delivery(state)
    assert audit["passed"] is True and audit["outcome"] == "success", audit

    binary_identity = (binary.stat().st_mtime_ns, _sha256(binary))
    rows_before_replay = ledger.open_ledger(state).records()
    again = _run(
        finish._finish(
            state,
            outcome="success",
            summary="CMake configure, build, and smoke run passed.",
            deliverables=[
                {"path": str(binary), "obligation_id": "smoke_binary"},
                {"path": str(marker), "obligation_id": "smoke_marker_file"},
            ],
        )
    )
    rows_after_replay = ledger.open_ledger(state).records()
    assert again["status"] == "success" and again["reused_manifest"] is True
    assert again["artifact_ids"] == first["artifact_ids"]
    assert (binary.stat().st_mtime_ns, _sha256(binary)) == binary_identity
    assert len([
        row for row in rows_after_replay
        if row.get("kind") == "action_started"
        and row.get("tool") == "safe_run_bash"
        and "cmake -S" in str(row.get("command") or "")
    ]) == 1
    assert len([row for row in rows_after_replay if row.get("kind") == "finish"]) == 1
    assert len(rows_after_replay) == len(rows_before_replay)


def test_broken_cmake_cannot_claim_success_but_can_finish_failed(tmp_path: Path):
    _require_local_toolchain()
    state = _state(tmp_path)
    roots = _roots(state)
    source = roots["managed_source_root"]
    build = roots["build_root"]
    run_root = roots["run_root"]
    binary = build / "hf_toolchain_smoke"
    marker = run_root / "smoke_stdout.txt"

    cmake_write = _run(
        safe_bash._safe_write_file(
            state,
            path=str(source / "CMakeLists.txt"),
            content=(
                "cmake_minimum_required(VERSION 3.16)\n"
                "project(hf_broken_smoke LANGUAGES C)\n"
                "add_executable(hf_toolchain_smoke main.c)\n"
            ),
        )
    )
    source_write = _run(
        safe_bash._safe_write_file(
            state,
            path=str(source / "main.c"),
            content="int main(void) { this is deliberately not valid C; }\n",
        )
    )
    assert cmake_write.get("status") == "success", cmake_write
    assert source_write.get("status") == "success", source_write

    executed = _run(
        safe_bash._safe_run_bash(
            state,
            cmd=_cmake_command(source, build, binary, marker),
            cwd=str(run_root),
            timeout=120,
        )
    )
    assert executed.get("returncode") not in (None, 0), executed
    assert not binary.exists()
    assert not marker.exists()

    unsupported = _run(
        finish._finish(
            state,
            outcome="success",
            summary="A failed compiler must not be reported as success.",
        )
    )
    assert unsupported["status"] == "success_unsupported", unsupported
    assert {gap["obligation"] for gap in unsupported["gaps"]} >= {
        "smoke_binary",
        "smoke_marker_file",
        "smoke_check",
    }
    # Missing required binary/marker and the actual failed check reject
    # success; a no-output failure need not invent a producer relationship.
    assert any(row.get("kind") == "action_settled" and row.get("exit_code") not in (None, 0)
               for row in ledger.open_ledger(state).records())
    assert not state.list_artifacts("experiment_log", own_only=True)

    failed = _run(
        finish._finish(
            state,
            outcome="failed",
            summary="CMake configured, but compilation failed on invalid C source.",
            next_step="Repair main.c and start a new build action.",
        )
    )
    assert failed["status"] == "success" and failed["outcome"] == "failed", failed
    assert any("Historical failed actions" in item for item in failed["delivery"]["limitations"])
    _assert_frozen_triplet(state, failed)
    audit = contract_audit.audit_experiment_delivery(state)
    assert audit["passed"] is True and audit["outcome"] == "failed", audit

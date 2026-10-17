"""Capture must follow the actual per-call writable boundary, not just cwd.

The Project fixture deliberately separates State.root, workspace_root and
the payload cwd.  A contains prose only: predeclared file obligations would
silently add capture candidates and hide the live CMake regression.
Payloads and negative write probes use the real sandbox.  The sole injected
fault is an unavailable pre-execution snapshot, not an execution or verdict.
"""
from __future__ import annotations

import asyncio
import shlex
import sys
from pathlib import Path

import pytest

from core import sandbox
from core.project_workspace import _git
from core.state import State
from nodes.experiment.tools import action_boundary, capture, finish, ledger, obligations, run_bootstrap, safe_bash
from nodes.experiment.tools.path_roles import collect_path_roles
from nodes.experiment.tools.subprocess_policy import bash_sandbox_roots, python_sandbox_roots


TASK = "Write the requested output containing exactly verified followed by a newline."


def _run(awaitable):
    return asyncio.run(awaitable)


@pytest.fixture
def project_state(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    _git(project, "init", "-q")
    _git(project, "-c", "user.name=Capture Test", "-c", "user.email=capture@example.test",
         "commit", "--allow-empty", "-qm", "Initialize isolated Project fixture")
    state = State.new(
        "experiment", project / ".research/runtime/runs", project_id="capture-boundary",
        session_id="owner", project_worktree=project,
    )
    state.hook_state["node_inputs"] = {
        "experiment_spec": TASK,
        "prereg_assignment": {"kind": "none", "reason": "deterministic output-capture test"},
    }
    established = _run(run_bootstrap.establish(state))
    assert ledger.bootstrap_phase(state) == "ready", established
    assert not any(row.get("kind") in {"file", "glob"} for row in obligations.frozen(state))
    assert state.root != state.workspace_root
    try:
        yield state
    finally:
        if state.sandbox_manifest:
            sandbox.evict_state_attempt(state)


def _run_root(state):
    return next(Path(role.path) for role in collect_path_roles(state) if role.role == "run_root")


def _command(program):
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(program)}"


def _execute(state, tool, program, cwd):
    if tool == "python":
        return _run(safe_bash._safe_execute_python(state, code=program, cwd=str(cwd), timeout=30))
    return _run(safe_bash._safe_run_bash(state, cmd=_command(program), cwd=str(cwd), timeout=30))


def _observations(state):
    return ledger.project_output_observations(ledger.open_ledger(state).records())


@pytest.mark.parametrize("tool,location", [
    ("bash", "workspace-parent"), ("bash", "sibling"), ("python", "parent"), ("python", "sibling"),
])
def test_authorized_output_outside_child_cwd_is_captured_without_an_input_file_obligation(
    project_state, tool, location,
):
    state = project_state
    runtime = _run_root(state)
    cwd = runtime / "child"
    cwd.mkdir()
    if location == "workspace-parent":
        target = Path(state.workspace_root) / "result.txt"
        expression = "Path.cwd().parent.parent / 'result.txt'"
    elif location == "parent":
        target = runtime / "result.txt"
        expression = "Path.cwd().parent / 'result.txt'"
    else:
        (runtime / "sibling").mkdir()
        target = runtime / "sibling/result.txt"
        expression = "Path.cwd().parent / 'sibling' / 'result.txt'"
    assert cwd != state.root and cwd != state.workspace_root
    roots, _ = (python_sandbox_roots if tool == "python" else bash_sandbox_roots)(state, str(cwd))
    assert any(target.is_relative_to(root) for root in roots), roots
    before_a = obligations.obligations_path(state).read_bytes()
    result = _execute(state, tool,
                      f"from pathlib import Path; target = {expression}; target.write_text('verified\\n')", cwd)
    assert result.get("returncode") == 0, result
    assert target.read_text() == "verified\n"
    observed = [row for row in _observations(state) if str(target) in (row.get("created") or [])]
    assert observed, "The real permitted write was omitted because it was outside cwd."
    assert observed[-1]["identities"][str(target)]["after"]["sha256"]
    assert finish._producer_rows(ledger.open_ledger(state).records(), target)
    closed = _run(finish._finish(
        state, outcome="success", summary="Output written and independently checked.",
        deliverables=[{"path": str(target)}],
        verification=[{"requirement": TASK, "checks": [
            {"kind": "file", "path": str(target)},
            {"kind": "check", "command": _command(
                f"from pathlib import Path; assert Path({str(target)!r}).read_text() == 'verified\\n'"),
             "expect_exit": 0},
        ]}],
    ))
    assert closed.get("status") == "success" and closed.get("outcome") == "success", closed
    assert obligations.obligations_path(state).read_bytes() == before_a


@pytest.mark.parametrize("tool", ["bash", "python"])
def test_capture_expansion_does_not_grant_writes_to_readonly_or_other_node_paths(project_state, tool):
    state = project_state
    project = Path(state.project_worktree)
    baseline = project / "sources/baseline.txt"
    other_node = project / "figures/other-owner.txt"
    for target in (baseline, other_node):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("must remain unchanged\n")
    # Runtime-computed paths reach the real OS boundary, rather than merely
    # obtaining an early parser refusal.  Project overlays make this a real
    # denial test even when pytest's temporary root happens to be under /tmp.
    runtime = _run_root(state)
    cwd = runtime / "child"
    cwd.mkdir()
    program = (
        "from pathlib import Path\n"
        "project = Path.cwd().parents[2]\n"
        "denied = []\n"
        "for relative in ['sources/baseline.txt', 'figures/other-owner.txt']:\n"
        "    try:\n"
        "        (project / relative).write_text('unauthorized overwrite')\n"
        "    except OSError:\n"
        "        denied.append(relative)\n"
        "assert len(denied) == 2, denied\n"
        "print('BOTH_WRITES_DENIED')\n"
    )
    result = _execute(state, tool, program, cwd)
    assert result.get("returncode") == 0, result
    assert "BOTH_WRITES_DENIED" in result.get("stdout_tail", ""), result
    assert baseline.read_text() == other_node.read_text() == "must remain unchanged\n"
    for target in (baseline, other_node):
        assert not finish._producer_rows(ledger.open_ledger(state).records(), target)


def test_combined_created_and_modified_display_limit_does_not_discard_identities(project_state):
    state = project_state
    runtime = _run_root(state)
    for number in range(100):
        (runtime / f"modified-{number:03}.txt").write_text("before")
    program = (
        "from pathlib import Path\n"
        "for n in range(150): Path(f'created-{n:03}.txt').write_text('new')\n"
        "for n in range(100): Path(f'modified-{n:03}.txt').write_text('after')\n"
    )
    result = _execute(state, "bash", program, runtime)
    assert result.get("returncode") == 0, result
    observation = _observations(state)[-1]
    assert len(observation["created"]) == 150
    assert sum(Path(p).is_file() for p in observation["modified"]) == 100
    assert str(runtime) in observation["modified"]  # Directory entries changed too.
    assert len(observation["identities"]) == len(observation["created"]) + len(observation["modified"])
    assert observation["truncated"] is False
    rows = ledger.open_ledger(state).records()
    stored = [row for row in rows if row.get("kind") == "observation"][-1]
    assert len(stored["identities"]) == 200 and stored["summary_truncated"]
    assert all(finish._file_evidence(state, rows, {"path": path})["passed"]
               for path in observation["identities"] if Path(path).is_file())


def test_unavailable_before_snapshot_does_not_invent_a_new_producer(project_state, monkeypatch):
    state = project_state
    runtime = _run_root(state)
    target = runtime / "result.txt"
    real_snapshot = capture.snapshot
    calls = 0

    def fail_before_only(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("injected pre-execution filesystem scan failure")
        return real_snapshot(*args, **kwargs)

    monkeypatch.setattr(capture, "snapshot", fail_before_only)
    result = _execute(state, "bash", "from pathlib import Path; Path('result.txt').write_text('verified\\n')", runtime)
    assert result.get("returncode") == 0, result
    assert target.read_text() == "verified\n"
    observation = _observations(state)[-1]
    assert observation["truncated"] is True
    assert str(target) not in observation["created"]
    assert str(target) in observation["unknown"]
    assert not finish._producer_rows(ledger.open_ledger(state).records(), target)
    assert finish._file_evidence(state, ledger.open_ledger(state).records(), {"path": str(target)})["passed"] is False


def test_overlapping_real_writers_do_not_double_claim_one_output_identity(project_state):
    """Two live action windows observe one shared path, not two proven producers.

    Direct Core spawn keeps the real sandbox/process path but avoids adding a
    third action wrapper around the two deliberately overlapping handles.
    """
    state = project_state
    runtime = _run_root(state)
    target = runtime / "shared.txt"
    writable, readonly = bash_sandbox_roots(state, str(runtime))
    boundary = action_boundary.observation_boundary(state, writable, readonly)
    safe_bash._ensure_hardened_attempt_manifest(state)
    handles = [action_boundary.begin(
        state, tool="safe_run_bash", command=f"writer-{number}", work_root=str(runtime),
        observation_boundary=boundary,
    ) for number in range(2)]
    assert all(isinstance(handle, action_boundary.ActionHandle) for handle in handles)

    async def run_both():
        return await asyncio.gather(*[
            safe_bash.spawn_and_wait(
                sys.executable, "-c",
                f"from pathlib import Path; Path('shared.txt').write_text('writer-{number}')",
                state=state, cwd=str(runtime), timeout=15,
                writable_roots=writable, readonly_roots=readonly,
            ) for number in range(2)
        ])

    outcomes = _run(run_both())
    for handle, (status, returncode, stdout, stderr) in zip(handles, outcomes, strict=True):
        assert status == "done" and returncode == 0, (status, returncode, stdout, stderr)
        settled = action_boundary.settle(state, handle, outcome="settled", exit_code=returncode)
        assert settled["status"] == "recorded", settled
    assert target.read_text() in {"writer-0", "writer-1"}
    rows = ledger.open_ledger(state).records()
    evidence = finish._file_evidence(state, rows, {"path": str(target)})
    assert evidence["passed"] is False, "Overlapping snapshots fabricated unambiguous provenance."
    observations = [row for row in ledger.project_output_observations(rows)
                    if row["action_id"] in {handle.action_id for handle in handles}]
    assert len(observations) == 2
    assert all(str(target) in row.get("unknown", []) for row in observations), observations
    assert not finish._producer_rows(rows, target)


@pytest.mark.parametrize("tool", ["bash", "python"])
def test_business_capture_excludes_private_scratch_and_framework_state(project_state, tool):
    state = project_state
    runtime = _run_root(state)
    cwd = runtime / "child"
    cwd.mkdir()
    internal_scratch = runtime / ".python-scratch"
    internal_scratch.mkdir(exist_ok=True)
    private_output = internal_scratch / "not-a-deliverable.txt"
    business_output = runtime / "result.txt"
    marker = state.root / "framework-marker.txt"
    marker.write_text("framework-owned metadata")
    program = (
        "from pathlib import Path\n"
        "import os\n"
        "runtime = Path.cwd().parent\n"
        "(runtime / 'result.txt').write_text('verified\\n')\n"
        "(runtime / '.python-scratch/not-a-deliverable.txt').write_text('private cache')\n"
        "private = Path(os.environ['TMPDIR']) / 'private-probe.txt'\n"
        "private.write_text('temporary data')\n"
        "assert private.read_text() == 'temporary data'\n"
        "print('PRIVATE_WRITE_OCCURRED', private)\n"
    )
    result = _execute(state, tool, program, cwd)
    assert result.get("returncode") == 0, result
    assert "PRIVATE_WRITE_OCCURRED" in result.get("stdout_tail", ""), result
    assert business_output.read_text() == "verified\n"
    assert private_output.read_text() == "private cache"
    observation = _observations(state)[-1]
    assert str(business_output) in observation["created"]
    observed_paths = set(observation["created"] + observation["modified"] + observation["unknown"])
    assert str(private_output) not in observed_paths
    assert str(private_output) not in observation["identities"]
    assert not any(Path(path).is_relative_to(state.root) for path in observed_paths)
    boundary = observation["observation_boundary"]
    assert any(internal_scratch.is_relative_to(Path(path)) for path in boundary["excluded_roots"])
    assert not any(Path(path).is_relative_to(state.root) for path in boundary["roots"])
    assert not any("hf-scratch-" in path for path in boundary["roots"])
    # Capturing an output never changes Core's approved capability manifest.
    assert marker.read_text() == "framework-owned metadata"
    assert not finish._producer_rows(ledger.open_ledger(state).records(), marker)

"""A real successful check must not bless an output tree that lost a member.

The sole fault is a test-owned external writer, scheduled after the real readonly
check and before the real directory evaluation.  The runner result, captured
identities, ledger and verdict are never fabricated.  This is not a sandbox
bypass claim: the fixture, not the model command, performs the deletion.
"""
from __future__ import annotations

import asyncio
import hashlib
import shlex
import sys
from pathlib import Path

import pytest

from core import sandbox
from core.project_workspace import _git, working_directory
from core.state import State
from nodes.experiment.tools import delivery_transaction, finish, ledger, obligations, run_bootstrap, safe_bash


def _command(program):
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(program)}"


@pytest.mark.parametrize("binding", ["project", "direct"])
@pytest.mark.parametrize("scenario", ["stable-output", "deleted-output", "deleted-input"])
def test_directory_members_observed_before_check_remain_part_of_delivery(
    tmp_path, monkeypatch, record_property, binding, scenario,
):
    input_audit = scenario == "deleted-input"
    task = ("Audit and deliver the existing input bundle; it must contain a.txt and b.txt, both exactly 49 and a newline. Do not rewrite inputs."
            if input_audit else
            "Create and deliver bundle containing a.txt and b.txt, both exactly 49 and a newline.")
    baseline = tmp_path / "provided-input"
    if input_audit:
        bundle = baseline / "bundle"
        bundle.mkdir(parents=True)
        for name in ("a.txt", "b.txt"):
            (bundle / name).write_text("49\n")
    if binding == "project":
        project = tmp_path / "project"
        project.mkdir()
        _git(project, "init", "-q")
        _git(project, "-c", "user.name=Directory Window Review", "-c",
             "user.email=window@example.test", "commit", "--allow-empty", "-qm", "fixture")
        state = State.new("experiment", project / ".research/runtime/runs",
                          project_id="directory-window", session_id="owner", project_worktree=project)
    else:
        state = State.new("experiment", tmp_path / "runs", session_id="owner")
    state.hook_state["node_inputs"] = {
        "experiment_spec": task,
        "prereg_assignment": {"kind": "none", "reason": "isolated real directory window test"},
    }
    if input_audit:
        state.hook_state["node_inputs"]["path_roles"] = {
            "source_baseline_root": {"path": str(baseline), "writable": False},
        }
    with asyncio.Runner() as runner:
        try:
            established = runner.run(run_bootstrap.establish(state))
            assert ledger.bootstrap_phase(state) == "ready", established
            assert not any(row["kind"] in {"file", "glob"} for row in obligations.frozen(state))
            workspace = working_directory(state)
            cwd = workspace / "runtime/child"
            cwd.mkdir()
            if not input_audit:
                bundle = workspace / "runtime/bundle"
                produced = runner.run(safe_bash._safe_run_bash(
                    state, cwd=str(cwd), timeout=20,
                    cmd=_command("from pathlib import Path; p = Path.cwd().parent / 'bundle'; p.mkdir(); "
                                 "(p / 'a.txt').write_text('49\\n'); (p / 'b.txt').write_text('49\\n')"),
                ))
                assert produced.get("returncode") == 0, produced
                for name in ("a.txt", "b.txt"):
                    assert finish._producer_rows(ledger.open_ledger(state).records(), bundle / name)
            else:
                assert obligations.input_reference(state, bundle)
            assert len({state.root, workspace, cwd, bundle}) == 4
            assert (bundle / "a.txt").read_text() == (bundle / "b.txt").read_text() == "49\n"
            frozen_a = obligations.obligations_path(state).read_bytes()
            actual_runner = finish._check_runner
            actual_directory_evidence = finish._directory_evidence
            window = {}

            async def check_then_external_writer(actual_state, obligation):
                result = await actual_runner(actual_state, obligation)
                assert result.get("executed") is True and result.get("exit_code") == 0, result
                assert "BOTH_MEMBERS_CHECKED" in result.get("stdout_tail", ""), result
                assert (bundle / "b.txt").read_text() == "49\n"
                window["check_passed_before_change"] = True
                if scenario != "stable-output":
                    # Only the private fixture's external writer acts here;
                    # return the actual check result completely unchanged.
                    (bundle / "b.txt").unlink()
                return result

            def observe_actual_directory_evidence(actual_state, records, path, **kwargs):
                before = kwargs["before_members"]
                assert not before["truncated"]
                assert before["entries"][str(bundle / "b.txt")]["kind"] == "file"
                window["before_snapshot_has_b"] = True
                result = actual_directory_evidence(actual_state, records, path, **kwargs)
                window["directory_assessment_passed"] = result["passed"]
                return result

            monkeypatch.setattr(finish, "_check_runner", check_then_external_writer)
            monkeypatch.setattr(finish, "_directory_evidence", observe_actual_directory_evidence)
            result = runner.run(finish._finish(
                state, outcome="success", summary="Both required bundle members were checked.",
                deliverables=[{"path": str(bundle)}],
                verification=[{"requirement": task, "checks": [{
                    "kind": "check", "cwd": "runtime", "timeout_s": 20,
                    "command": _command(f"from pathlib import Path; p = Path({str(bundle)!r}); "
                                        "assert sorted(q.name for q in p.iterdir()) == ['a.txt', 'b.txt']; "
                                        "assert (p / 'a.txt').read_bytes() == b'49\\n'; "
                                        "assert (p / 'b.txt').read_bytes() == b'49\\n'; "
                                        "print('BOTH_MEMBERS_CHECKED')"),
                    "expect_exit": 0, "expect_stdout_contains": "BOTH_MEMBERS_CHECKED",
                }]}],
            ))
            assert window["check_passed_before_change"] and window["before_snapshot_has_b"]
            assert obligations.obligations_path(state).read_bytes() == frozen_a
            assert (bundle / "a.txt").read_text() == "49\n"
            assert (bundle / "b.txt").exists() == (scenario == "stable-output")
            records = ledger.open_ledger(state).records()
            check_ids = {row["action_id"] for row in records if row.get("kind") == "action_started"
                         and row.get("tool") == "finish.check"}
            checks = [row for row in records if row.get("kind") == "action_settled"
                      and row.get("action_id") in check_ids]
            assert len(checks) == 1 and checks[0].get("executed") is True and checks[0]["exit_code"] == 0
            manifest = delivery_transaction.read_manifest(state)
            for name, value in {
                "scenario": scenario,
                "a_sha256_unchanged": hashlib.sha256(frozen_a).hexdigest(),
                **window,
                "b_exists_after_check": (bundle / "b.txt").exists(),
                "observed_finish_status": result.get("status"),
                "observed_finish_outcome": result.get("outcome"),
                "delivery_manifest_present": manifest is not None,
            }.items():
                record_property(name, value)
            if scenario == "stable-output":
                assert result.get("status") == "success" and result.get("outcome") == "success", result
                assert manifest is not None
            else:
                assert result.get("status") == "success_unsupported", result
                assert result.get("gaps"), result
                assert manifest is None
        finally:
            if state.sandbox_manifest:
                sandbox.evict_state_attempt(state)

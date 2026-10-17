"""A real check of 49 must not bless a restored, formerly successful 7.

Both file versions have genuine successful action observations.  Only the
test-owned external writer moves them, after the real readonly check returns;
no runner result, identity, ledger record or verdict is fabricated.  This is a
check-window counterexample, not a claim that the sandbox permits these moves.
"""
from __future__ import annotations

import asyncio
import hashlib
import shlex
import sys

import pytest

from core import sandbox
from core.project_workspace import _git, working_directory
from core.state import State
from nodes.experiment.tools import (
    capture, delivery_transaction, finish, ledger, obligations, run_bootstrap, safe_bash,
)


def _command(program):
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(program)}"


def _settled_checks(records):
    ids = {row["action_id"] for row in records
           if row.get("kind") == "action_started" and row.get("tool") == "finish.check"}
    return [row for row in records
            if row.get("kind") == "action_settled" and row.get("action_id") in ids]


@pytest.mark.parametrize("entry", ["deliverable-check", "file-check", "glob-before", "glob-after"])
@pytest.mark.parametrize("rollback", [False, True], ids=["stable", "rollback"])
def test_checked_object_cannot_roll_back_to_an_older_successful_producer(
    tmp_path, monkeypatch, record_property, entry, rollback,
):
    task = "Create and deliver runtime/result.txt containing exactly 49 followed by a newline."
    project = tmp_path / "project"
    project.mkdir()
    _git(project, "init", "-q")
    _git(project, "-c", "user.name=Object Window Review", "-c",
         "user.email=window@example.test", "commit", "--allow-empty", "-qm", "fixture")
    state = State.new("experiment", project / ".research/runtime/runs",
                      project_id="object-window", session_id="owner", project_worktree=project)
    state.hook_state["node_inputs"] = {
        "experiment_spec": task,
        "prereg_assignment": {"kind": "none", "reason": "isolated real object window test"},
    }
    vault = tmp_path / "fixture-only-vault"
    vault.mkdir()
    old_slot, new_slot = vault / "old-object", vault / "checked-object"
    with asyncio.Runner() as runner:
        try:
            established = runner.run(run_bootstrap.establish(state))
            assert ledger.bootstrap_phase(state) == "ready", established
            workspace = working_directory(state)
            cwd = workspace / "runtime/child"
            cwd.mkdir()
            target = workspace / "runtime/result.txt"
            assert len({state.root, workspace, cwd, vault}) == 4
            assert not vault.is_relative_to(project)
            frozen_a = obligations.obligations_path(state).read_bytes()
            assert any(row.get("text") == task for row in obligations.frozen(state))

            def produce(content):
                result = runner.run(safe_bash._safe_run_bash(
                    state, cwd=str(cwd), timeout=20,
                    cmd=_command("from pathlib import Path; "
                                 f"(Path.cwd().parent / 'result.txt').write_text({content!r})"),
                ))
                assert result.get("returncode") == 0, result
                assert target.read_text() == content
                identity = capture.identity(target)
                rows = finish._producer_rows(ledger.open_ledger(state).records(), target)
                matching = [row for row in rows if finish._identity_matches(row.get("after"), identity)[0]]
                assert matching, rows
                return identity, {row["action_id"] for row in matching}

            old_identity, old_producers = produce("7\n")
            target.rename(old_slot)
            assert capture.identity(old_slot) == old_identity
            checked_identity, checked_producers = produce("49\n")
            assert old_producers.isdisjoint(checked_producers)
            assert old_identity["inode"] != checked_identity["inode"]
            payload_records = ledger.open_ledger(state).records()
            payload_ids = {row["action_id"] for row in payload_records
                           if row.get("kind") == "action_started" and row.get("tool") == "safe_run_bash"}
            assert len(payload_ids) == 2
            payload_settled = [row for row in payload_records
                               if row.get("kind") == "action_settled" and row.get("action_id") in payload_ids]
            assert len(payload_settled) == 2 and all(row.get("exit_code") == 0 for row in payload_settled)
            actual_runner = finish._check_runner
            observed = {}

            async def check_then_external_writer(actual_state, obligation):
                result = await actual_runner(actual_state, obligation)
                assert result.get("executed") is True and result.get("exit_code") == 0, result
                assert "CHECKED_EXACT_49" in result.get("stdout_tail", ""), result
                assert target.read_bytes() == b"49\n"
                assert capture.identity(target) == checked_identity
                observed["real_check_exit_code"] = result["exit_code"]
                observed["checked_identity"] = capture.identity(target)
                if rollback:
                    # This external fixture writer has no action/producer claim.
                    # rename preserves the actual old inode, bytes and mtime.
                    target.rename(new_slot)
                    old_slot.rename(target)
                    assert capture.identity(target) == old_identity
                observed["identity_after_external_writer"] = capture.identity(target)
                return result

            monkeypatch.setattr(finish, "_check_runner", check_then_external_writer)
            check = {
                "kind": "check", "cwd": "runtime", "timeout_s": 20,
                "command": _command(f"from pathlib import Path; assert Path({str(target)!r}).read_bytes() == b'49\\n'; "
                                    "print('CHECKED_EXACT_49')"),
                "expect_exit": 0, "expect_stdout_contains": "CHECKED_EXACT_49",
            }
            file_method = {"kind": "file", "path": "runtime/result.txt"}
            glob_method = {"kind": "glob", "pattern": "runtime/*.txt", "min_count": 1}
            checks = {
                "deliverable-check": [check], "file-check": [file_method, check],
                "glob-before": [glob_method, check], "glob-after": [check, glob_method],
            }[entry]
            deliverables = [{"path": "runtime/result.txt"}] if entry == "deliverable-check" else None

            def close():
                return runner.run(finish._finish(
                    state, outcome="success", summary="The real check required result.txt to contain exactly 49.",
                    deliverables=deliverables,
                    verification=[{"requirement": task, "checks": checks}],
                ))

            result = close()
            records = ledger.open_ledger(state).records()
            settled = _settled_checks(records)
            assert len(settled) == 1 and settled[0].get("executed") is True and settled[0]["exit_code"] == 0
            assert records[:len(payload_records)] == payload_records
            assert obligations.obligations_path(state).read_bytes() == frozen_a
            assert target.read_bytes() == (b"7\n" if rollback else b"49\n")
            current_identity = capture.identity(target)
            current_producers = {row["action_id"] for row in finish._producer_rows(records, target)
                                 if finish._identity_matches(row.get("after"), current_identity)[0]}
            assert current_producers == (old_producers if rollback else checked_producers)
            manifest = delivery_transaction.read_manifest(state)
            for name, value in {
                "entry": entry, "rollback": rollback,
                "a_sha256_unchanged": hashlib.sha256(frozen_a).hexdigest(),
                "payload_count": len(payload_ids),
                "checked_content": "49\\n", "delivered_content": target.read_text(),
                "old_producers": sorted(old_producers), "checked_producers": sorted(checked_producers),
                "current_producers": sorted(current_producers),
                **observed, "observed_finish_status": result.get("status"),
                "observed_finish_outcome": result.get("outcome"),
                "delivery_manifest_present": manifest is not None,
            }.items():
                record_property(name, value)
            if not rollback:
                assert result.get("status") == "success" and result.get("outcome") == "success", result
                assert manifest is not None
                return

            assert result.get("status") == "success_unsupported", result
            assert result.get("gaps"), result
            assert manifest is None
            # This recovery path becomes reachable only after the false success
            # is fixed. Restore the already produced 49 object; do not rerun it.
            target.rename(old_slot)
            new_slot.rename(target)
            assert capture.identity(target) == checked_identity
            monkeypatch.setattr(finish, "_check_runner", actual_runner)
            recovered = close()
            assert recovered.get("status") == "success" and recovered.get("outcome") == "success", recovered
            assert delivery_transaction.read_manifest(state) is not None
            recovered_records = ledger.open_ledger(state).records()
            assert recovered_records[:len(records)] == records
            recovered_checks = _settled_checks(recovered_records)
            assert len(recovered_checks) == 2 and all(
                row.get("executed") is True and row["exit_code"] == 0 for row in recovered_checks)
            assert {row["action_id"] for row in recovered_records
                    if row.get("kind") == "action_started" and row.get("tool") == "safe_run_bash"} == payload_ids
            assert obligations.obligations_path(state).read_bytes() == frozen_a
            record_property("recovery_without_payload_rerun", True)
        finally:
            if state.sandbox_manifest:
                sandbox.evict_state_attempt(state)

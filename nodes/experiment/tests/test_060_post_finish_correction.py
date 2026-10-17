"""A committed delivery replay is not acceptance of a later correction.

The regression follows the real FVCOM shape with a tiny shell script: an
honest partial delivery, a recorded file-tool repair, a real syntax check,
then another finish. No model, external service, or FVCOM build is needed.
Preserving the old C is correct; silently acknowledging the new request
without either current evidence or an explicit recovery response is not.
"""
from __future__ import annotations

import asyncio
import json
import shlex

import pytest

from core.project_workspace import working_directory
from core.state import State
from nodes.experiment.tools import capture, delivery_transaction as tx
from nodes.experiment.tools import finish, ledger, obligations, run_bootstrap, safe_bash


TASK = "Deliver repairable.sh with valid Bash syntax."
BROKEN = "#!/bin/bash\nprintf 'ready\\n'\n&& printf 'done\\n'\n"
REPAIRED = "#!/bin/bash\nprintf 'ready\\n' && printf 'done\\n'\n"


def _state(tmp_path, runner):
    state = State.new("experiment", tmp_path / "runs", session_id="post-finish")
    state.hook_state["node_inputs"] = {
        "experiment_spec": TASK,
        "prereg_assignment": {"kind": "none", "reason": "script syntax validation"},
    }
    established = runner.run(run_bootstrap.establish(state))
    assert ledger.bootstrap_phase(state) == "ready", established
    return state, working_directory(state) / "runtime" / "repairable.sh"


def _write(state, target, content, runner):
    result = runner.run(safe_bash._safe_write_file(
        state, path=str(target), content=content,
    ))
    assert result.get("status") == "success", result
    assert target.read_text() == content
    assert finish._producer_rows(ledger.open_ledger(state).records(), target)


def _verification(target):
    return [{"requirement": TASK, "checks": [
        {"kind": "file", "path": str(target)},
        {"kind": "check", "command": f"bash -n {shlex.quote(str(target))}",
         "cwd": str(target.parent), "expect_exit": 0, "timeout_s": 20},
    ]}]


def _close(state, target, runner, *, summary):
    return runner.run(finish._finish(
        state, outcome="partial", summary=summary,
        deliverables=[{"path": str(target)}], verification=_verification(target),
        limitations=["This delivery checks syntax only, not application behavior."],
    ))


def _payload(state, result, kind):
    return json.loads(state.read_artifact(result["artifact_ids"][kind])["content"])


def _check_rows(clean):
    return clean["evaluation"]["checks"]


def _object_identities(clean, target):
    return [row["evidence"]["identity_now"] for row in _check_rows(clean)
            if row.get("evidence", {}).get("path") == str(target)
            and "identity_now" in row["evidence"]]


def _historical_contents(state, result):
    return {kind: state.read_artifact(artifact)["content"]
            for kind, artifact in result["artifact_ids"].items()}


def _assert_old_delivery_retained(state, first, old_contents):
    # A later revision may use the Core version API; freezing the first C
    # does not authorize overwriting its historical content or identity.
    for kind, artifact in first["artifact_ids"].items():
        old_version = first["artifact_identities"][kind]["version"]
        versions = state.artifact_versions(artifact)
        original = next(row for row in versions if row["version"] == old_version)
        assert original["content"] == old_contents[kind]


def test_post_finish_repair_is_not_silently_reported_as_new_delivery(tmp_path):
    with asyncio.Runner() as runner:
        state, target = _state(tmp_path, runner)
        _write(state, target, BROKEN, runner)
        first = _close(state, target, runner, summary="Script exists; syntax is still invalid.")
        assert first.get("status") == "success" and first.get("outcome") == "partial", first
        original_identity = capture.identity(target)
        original_clean = _payload(state, first, "clean_results")
        assert original_identity in _object_identities(original_clean, target)
        assert any(row.get("passed") is False
                   and row.get("evidence", {}).get("exit_code") == 2
                   for row in _check_rows(original_clean)), original_clean
        old_contents = _historical_contents(state, first)
        frozen_task = obligations.obligations_path(state).read_bytes()

        _write(state, target, REPAIRED, runner)
        current_identity = capture.identity(target)
        assert current_identity["sha256"] != original_identity["sha256"]
        check = runner.run(safe_bash._safe_run_bash(
            state, cmd=f"bash -n {shlex.quote(str(target))} && printf 'SCRIPT_SYNTAX_OK\\n'",
            cwd=str(target.parent), timeout=20,
        ))
        assert check.get("returncode") == 0, check
        correction_seq = max(row["seq"] for row in ledger.open_ledger(state).records())

        second = _close(state, target, runner, summary="Script syntax is repaired and rechecked.")
        _assert_old_delivery_retained(state, first, old_contents)
        assert obligations.obligations_path(state).read_bytes() == frozen_task

        # The agreed fix supports a genuine successor, not just a warning
        # that this correction has been discarded.
        assert second.get("status") == "success", second
        assert second["revision"] == 2 and second["reused_manifest"] is False
        current_clean = _payload(state, second, "clean_results")
        assert current_identity in _object_identities(current_clean, target), (
            "finish acknowledged a corrected delivery but its sealed object identity is stale; "
            "either include current evidence or explicitly report non-acceptance and recovery",
            {"response": second, "current_identity": current_identity,
             "sealed_identities": _object_identities(current_clean, target)},
        )
        assert current_clean["ledger_pointer"]["through_seq"] >= correction_seq
        syntax_checks = [row for row in _check_rows(current_clean)
                         if row.get("evidence", {}).get("command") == _verification(target)[0]["checks"][1]["command"]]
        assert syntax_checks and all(row["passed"] and row["evidence"]["exit_code"] == 0
                                     for row in syntax_checks), syntax_checks


def test_no_new_facts_replays_the_sealed_delivery_without_rechecking(tmp_path):
    with asyncio.Runner() as runner:
        state, target = _state(tmp_path, runner)
        _write(state, target, REPAIRED, runner)
        first = _close(state, target, runner, summary="Syntax checked; no application run claimed.")
        assert first.get("status") == "success", first
        contents = _historical_contents(state, first)
        records = ledger.open_ledger(state).records()
        again = _close(state, target, runner, summary="Syntax checked; no application run claimed.")

        assert again.get("status") == "success" and again.get("reused_manifest") is True, again
        assert again["delivery"] == first["delivery"]
        assert again["artifact_identities"] == first["artifact_identities"]
        assert ledger.open_ledger(state).records() == records
        _assert_old_delivery_retained(state, first, contents)


def test_same_request_after_file_repair_updates_c_but_unrelated_observation_does_not(tmp_path):
    with asyncio.Runner() as runner:
        state, target = _state(tmp_path, runner)
        _write(state, target, BROKEN, runner)
        first = _close(state, target, runner, summary="Current syntax evidence.")
        led = ledger.open_ledger(state, write=True)
        led.append("diagnostic_note", note="unrelated observation, no new physical action")
        before = led.records()
        replay = _close(state, target, runner, summary="Current syntax evidence.")
        assert replay["revision"] == 1 and replay["reused_manifest"] is True
        assert led.records() == before

        _write(state, target, REPAIRED, runner)
        corrected = _close(state, target, runner, summary="Current syntax evidence.")
        assert corrected["status"] == "success" and corrected["revision"] == 2, corrected
        assert corrected["artifact_ids"] == first["artifact_ids"]
        assert capture.identity(target) in _object_identities(_payload(state, corrected, "clean_results"), target)


def test_new_delivery_claim_is_not_ignored_without_new_payload_execution(tmp_path):
    with asyncio.Runner() as runner:
        state, target = _state(tmp_path, runner)
        _write(state, target, REPAIRED, runner)
        first = _close(state, target, runner, summary="Only reporting a partial result.")
        before = [row for row in ledger.open_ledger(state).records()
                  if row.get("kind") == "action_started" and row.get("tool") != "finish.check"]
        second = runner.run(finish._finish(
            state, outcome="success", summary="The requested script syntax is verified.",
            deliverables=[{"path": str(target)}], verification=_verification(target),
        ))
        assert second["status"] == "success" and second["outcome"] == "success", second
        assert second["revision"] == 2 and second["delivery_digest"] != first["delivery_digest"]
        assert before == [row for row in ledger.open_ledger(state).records()
                          if row.get("kind") == "action_started" and row.get("tool") != "finish.check"]


def test_partial_freeze_recovers_original_transaction_not_a_new_claim(tmp_path, monkeypatch):
    with asyncio.Runner() as runner:
        state, target = _state(tmp_path, runner)
        _write(state, target, BROKEN, runner)
        real_freeze = state.mark_frozen
        interrupted = False

        def freeze_then_raise(*args, **kwargs):
            nonlocal interrupted
            result = real_freeze(*args, **kwargs)
            if not interrupted:
                interrupted = True
                raise OSError("interrupted after first real freeze")
            return result

        monkeypatch.setattr(state, "mark_frozen", freeze_then_raise)
        with pytest.raises(OSError, match="first real freeze"):
            _close(state, target, runner, summary="Original partial transaction.")
        original = tx.read_manifest(state)
        assert original is not None and original["commit"] is None
        before = ledger.open_ledger(state).records()
        monkeypatch.setattr(state, "mark_frozen", real_freeze)

        recovered = runner.run(finish._finish(
            state, outcome="invalid-new-outcome", summary="", unexpected_argument=True,
        ))
        assert recovered.get("status") == "success" and recovered.get("reused_manifest") is True, recovered
        assert recovered["delivery"] == original["delivery"]
        after = ledger.open_ledger(state).records()
        assert [row for row in after if row["kind"] == "action_started"] == [
            row for row in before if row["kind"] == "action_started"]
        assert len([row for row in after if row["kind"] == "finish"]) == 1
        assert all(len(state.artifact_versions(artifact)) == 1
                   for artifact in recovered["artifact_ids"].values())


def test_observation_failure_preserves_c_and_has_a_nonexecuting_recovery(tmp_path, monkeypatch):
    with asyncio.Runner() as runner:
        state, target = _state(tmp_path, runner)
        _write(state, target, REPAIRED, runner)
        first = _close(state, target, runner, summary="Current syntax evidence.")
        before = tx.manifest_path(state).read_bytes()
        records = ledger.open_ledger(state).records()
        real_identity = capture.identity

        def unavailable(path, *args, **kwargs):
            if path == target:
                raise PermissionError("injected temporary observation failure")
            return real_identity(path, *args, **kwargs)

        with monkeypatch.context() as probe:
            probe.setattr(capture, "identity", unavailable)
            rejected = _close(state, target, runner, summary="Current syntax evidence.")
        assert rejected["error_code"] == "closure_observation_unavailable", rejected
        assert rejected["closure_id"] == first["closure_id"]
        assert rejected["side_effects"] == "none" and rejected["retryable_after_change"] is True
        assert "retry finish" in rejected["next_action"] and "do not rerun" in rejected["next_action"]
        assert tx.manifest_path(state).read_bytes() == before
        assert ledger.open_ledger(state).records() == records

        recovered = _close(state, target, runner, summary="Current syntax evidence.")
        assert recovered["reused_manifest"] is True and recovered["revision"] == 1
        assert ledger.open_ledger(state).records() == records

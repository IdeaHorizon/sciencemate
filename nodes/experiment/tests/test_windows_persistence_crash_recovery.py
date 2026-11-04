"""Real process death at delivery boundaries; unmodified Core State and storage.

Each worker is bounded, run-local, and exits at an injected checkpoint without
unwinding Python locks or finally blocks. Recovery and replay use fresh workers.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from core.state import State
from nodes.experiment.tools import delivery_transaction as tx, ledger

_OWNER = "persistence-crash-test-owner"
_EXIT = 79
_RESULT = "PERSISTENCE_RECOVERY_RESULT:"
_MODULE = "nodes.experiment.tests.test_windows_persistence_crash_recovery"


def _manifest(state):
    delivery = {
        "schema": tx.DELIVERY_SCHEMA, "delivery_id": tx.delivery_id(state),
        "delivery_owner": "finish", "scope": "operation", "scope_status": "resolved",
        "outcome": "failed", "execution_status": "attempted", "result_status": "unavailable",
        "summary": "Persistence recovery fixture; no scientific computation.",
        "gaps": [], "contradictions": [], "limitations": [], "external_job_refs": [],
        "formal_input": {"status": "none", "binding": None, "error": None},
        "next_step": None, "blocker": None,
    }
    return tx.build_manifest(
        state, delivery=delivery,
        contents={kind: f"{kind}: process recovery fixture\n" for kind in tx.TRIO},
    )


def _reopen(root):
    state = State.reopen("experiment", root.parent, root.name, session_id=_OWNER)
    led = ledger.open_ledger(state, write=True, reattach=True)
    return state, led


def _finish_rows(led):
    return [row for row in led.records() if row.get("kind") == "finish"]


def _assert_sealed(state, led, result):
    assert result["status"] == "success"
    manifest = tx.read_manifest(state)
    assert manifest["commit"] is not None
    assert set(result["artifact_ids"]) == set(tx.TRIO)
    for kind in tx.TRIO:
        spec = manifest["artifacts"][kind]
        artifact = result["artifact_ids"][kind]
        assert artifact == spec["id"]
        head = state.artifact_head(artifact)
        record = state.read_artifact(artifact)
        assert head.frozen and head.version == head.frozen_version == 1
        assert head.sha256 == head.frozen_sha256 == spec["expected_content_hash"]
        assert record["version"] == 1
        assert record["content"] == spec["content"]
        assert hashlib.sha256(record["content"].encode("utf-8")).hexdigest() == head.sha256
        assert spec["frozen"] == result["artifact_identities"][kind] == {
            "id": artifact, "version": 1, "content_hash": head.sha256,
            "frozen_at": head.frozen_at,
        }
        assert len(state.artifact_versions(artifact)) == 1
    rows = _finish_rows(led)
    assert len(rows) == 1
    assert rows[0]["closure_id"] == result["closure_id"]
    assert rows[0]["artifact_identities"] == result["artifact_identities"]
    assert rows[0]["seq"] == result["finish_seq"] == manifest["commit"]["seq"]


def _worker(action, root):
    state, led = _reopen(root)
    if action == "recover":
        result = tx.settle_delivery(state, led, None)
        _assert_sealed(state, led, result)
        print(_RESULT + json.dumps(result, sort_keys=True), flush=True)
        return

    if action == "after_manifest_replace":
        replace = tx.os.replace

        def replace_then_die(source, target):
            replace(source, target)
            if Path(target) == tx.manifest_path(state):
                os._exit(_EXIT)

        tx.os.replace = replace_then_die
    elif action == "after_first_freeze":
        freeze = state.mark_frozen

        def freeze_then_die(*args, **kwargs):
            freeze(*args, **kwargs)
            os._exit(_EXIT)

        state.mark_frozen = freeze_then_die
    elif action == "after_ledger_commit":
        append = led.append_once

        def append_then_die(*args, **kwargs):
            row = append(*args, **kwargs)
            if args[0] == "finish":
                os._exit(_EXIT)
            return row

        led.append_once = append_then_die
    else:
        raise ValueError(f"unknown crash checkpoint: {action}")
    tx.settle_delivery(state, led, _manifest(state))
    raise AssertionError(f"crash checkpoint was not reached: {action}")


def _run_worker(action, root, isolated_home):
    return subprocess.run(
        [sys.executable, "-m", _MODULE, action, str(root)],
        cwd=Path(__file__).resolve().parents[3],
        env={**os.environ, "HARNESS_FRAMEWORK_HOME": str(isolated_home)},
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
    )


def _recover(root, isolated_home):
    completed = _run_worker("recover", root, isolated_home)
    assert completed.returncode == 0, (completed.stdout, completed.stderr)
    lines = [line for line in completed.stdout.splitlines() if line.startswith(_RESULT)]
    assert len(lines) == 1, completed.stdout
    return json.loads(lines[0][len(_RESULT):])


@pytest.mark.parametrize("boundary", [
    "after_manifest_replace", "after_first_freeze", "after_ledger_commit",
])
def test_fresh_process_recovers_native_process_death_without_duplicate_delivery(
    tmp_path, monkeypatch, boundary,
):
    isolated_home = tmp_path / "harness-home"
    isolated_home.mkdir()
    monkeypatch.setenv("HARNESS_FRAMEWORK_HOME", str(isolated_home))
    state = State.new("experiment", tmp_path / "runs", session_id=_OWNER)
    led = ledger.open_ledger(state, write=True, bootstrap=True)
    ledger.mark_bootstrap_ready(state)
    expected = _manifest(state)

    crashed = _run_worker(boundary, state.root, isolated_home)
    assert crashed.returncode == _EXIT, (crashed.stdout, crashed.stderr)
    pending = tx.read_manifest(state)
    assert pending["plan_digest"] == expected["plan_digest"]
    assert pending["commit"] is None
    heads = [state.artifact_head(spec["id"]) for spec in pending["artifacts"].values()]
    if boundary == "after_manifest_replace":
        assert pending["prepared_digest"] is None
        assert all(head is None for head in heads)
        assert _finish_rows(led) == []
    elif boundary == "after_first_freeze":
        assert pending["prepared_digest"] is not None
        assert all(head is not None for head in heads)
        assert sum(head.frozen for head in heads) == 1
        assert all(spec["frozen"] is None for spec in pending["artifacts"].values())
        assert _finish_rows(led) == []
    else:
        assert all(head.frozen for head in heads)
        assert all(spec["frozen"] is not None for spec in pending["artifacts"].values())
        assert len(_finish_rows(led)) == 1

    recovered = _recover(state.root, isolated_home)
    replayed = _recover(state.root, isolated_home)
    assert recovered == replayed
    assert recovered["reused_manifest"] is True
    assert recovered["delivery_digest"] == expected["delivery_digest"]
    reopened, reopened_ledger = _reopen(state.root)
    _assert_sealed(reopened, reopened_ledger, replayed)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("worker requires checkpoint and existing run directory")
    _worker(sys.argv[1], Path(sys.argv[2]))

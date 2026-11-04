"""Real local publication/ledger boundaries with only the SSH peer substituted."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from core.state import State
from nodes.experiment.tools import capture, job_settlement, ledger, slurm_files as files


TARGET = {"host": "test-login", "user": "remote-user", "login_shell": True}
ROOT = "/scratch/test-run"


def _identity(path, content, stamp=100):
    return {"path": path, "kind": "file", "size_bytes": len(content), "size": len(content),
            "inode": stamp, "st_ino": stamp, "st_dev": 5, "st_nlink": 1,
            "mtime_ns": stamp, "ctime_ns": stamp, "sha256": hashlib.sha256(content).hexdigest()}


class Peer:
    def __init__(self):
        self.entries, self.content, self.calls = {}, {}, []
        self.offline = False
        self.cluster = "test-cluster"

    def put(self, path, content):
        self.content[path] = content
        self.entries[path] = _identity(path, content)

    def probe(self, target):
        assert target == TARGET
        return {"ok": not self.offline, "user": "remote-user", "cluster": self.cluster}

    def response(self, value):
        return {"ok": True, "returncode": 0, "stdout": json.dumps(value), "stderr": ""}

    def run(self, target, argv, timeout=20, **kwargs):
        assert target == TARGET
        action, data = argv[-2], json.loads(argv[-1])
        self.calls.append(action)
        if self.offline:
            return {"ok": False, "returncode": None, "stderr": "connection lost", "stdout": ""}
        if action == "snapshot":
            return self.response({"entries": copy.deepcopy(self.entries), "taken_at": 100,
                                  "truncated": False, "root_coverage": [{"root": r, "status": "complete"} for r in data["roots"]]})
        if action == "check":
            return self.response({"ok": True, **data})
        if action == "prepare":
            return self.response({"ok": True})
        raise AssertionError(action)

    def stream(self, target, argv, *, stdin_file=None, stdout_file=None, **kwargs):
        assert target == TARGET
        action, data = argv[-2], json.loads(argv[-1])
        self.calls.append(action)
        if action == "upload":
            content = stdin_file.read()
            assert len(content) == data["size"]
            assert hashlib.sha256(content).hexdigest() == data["sha256"]
            self.put(data["path"], content)
            return self.response({"ok": True})
        assert action == "fetch"
        if self.offline or self.entries[data["path"]] != data["identity"]:
            # Partial bytes never get published, even if a failed command
            # returned diagnostic/partial stdout through the transport.
            stdout_file.write(b"partial")
            return {"ok": False, "stderr": "remote changed or connection lost"}
        stdout_file.write(self.content[data["path"]])
        return {"ok": True, "returncode": 0}


@pytest.fixture
def managed(tmp_path, monkeypatch):
    state = State.new("experiment", tmp_path / "runs")
    owner = ledger.open_ledger(state, write=True, bootstrap=True)
    ledger.mark_bootstrap_ready(state)
    record = {"scheduler": "slurm", "job_id": "42", "submission_nonce": "test-nonce",
              "slurm_target": TARGET, "remote_user": "remote-user", "scheduler_cluster": "test-cluster",
              "output_roots": [ROOT], "scheduler_output_dir": ROOT, "workdir": ROOT}
    peer = Peer()
    monkeypatch.setattr(files.slurm_ssh, "run", peer.run)
    monkeypatch.setattr(files.slurm_ssh, "run_stream", peer.stream)
    monkeypatch.setattr(files.slurm_ssh, "probe", peer.probe)
    owner.append("intent", nonce=record["submission_nonce"], scheduler="slurm", output_root=str(tmp_path / "local-output"),
                 **{k: v for k, v in record.items() if k not in {"submission_nonce", "scheduler", "job_id"}})
    return state, record, peer


def _launch(state, record):
    owner = ledger.open_ledger(state, write=True)
    owner.append("submission_attempted", nonce=record["submission_nonce"], scheduler="slurm",
                 output_baseline=job_settlement.capture_submission_outputs(state, record["submission_nonce"]))
    owner.append("job_bound", nonce=record["submission_nonce"], scheduler="slurm", external_id="42")


def _settle(state, record):
    return job_settlement.settle_terminal(state, record, evidence={"allocation_state": "COMPLETED", "exit_code": 0})


def test_remote_result_roundtrip_keeps_original_producer_and_one_terminal(managed):
    state, record, peer = managed
    _launch(state, record)
    peer.put(ROOT + "/answer.txt", b"42\n")
    settled = _settle(state, record)
    assert settled["settled"], settled
    transfer = settled["output_transfer"]
    assert transfer["ok"], transfer
    local = Path(transfer["files"][0]["local_path"])
    assert local.read_bytes() == b"42\n"
    assert local.is_relative_to(files.experiment_output_dir(state, "runtime"))
    rows = ledger.open_ledger(state).records()
    projected = ledger.project_output_observations(rows)
    producer = [r for r in projected if str(local) in r.get("created", [])]
    assert len(producer) == 1 and producer[0]["action_id"] == "job-test-nonce"
    assert not any(ROOT + "/answer.txt" in r.get("created", []) for r in projected)
    assert not any(r["kind"] == "action_started" for r in rows)
    assert _settle(state, record)["already"]
    repeated = files.collect_outputs(state, record)
    assert repeated["ok"] and repeated["bytes_transferred"] == 0
    rows = ledger.open_ledger(state).records()
    assert sum(r["kind"] == "job_terminal" for r in rows) == 1
    assert sum(r["kind"] == "job_output_collected" for r in rows) == 1


def test_failed_transfer_can_retry_without_reobserving_or_recomputing(managed, monkeypatch):
    state, record, peer = managed
    _launch(state, record)
    peer.put(ROOT + "/answer.txt", b"42\n")
    original = files.collect_outputs
    monkeypatch.setattr(files, "collect_outputs", lambda *_a, **_k: {"ok": False, "status": "unavailable"})
    settled = _settle(state, record)
    assert settled["settled"]
    snapshot_calls = peer.calls.count("snapshot")
    monkeypatch.setattr(files, "collect_outputs", original)
    peer.offline = True
    assert not files.collect_outputs(state, record)["ok"]
    peer.offline = False
    retried = files.collect_outputs(state, record)
    assert retried["ok"], retried
    assert peer.calls.count("snapshot") == snapshot_calls
    assert sum(r["kind"] == "job_terminal" for r in ledger.open_ledger(state).records()) == 1


def test_remote_mutation_after_terminal_is_not_blessed_by_retry(managed, monkeypatch):
    state, record, peer = managed
    _launch(state, record)
    peer.put(ROOT + "/answer.txt", b"42\n")
    original = files.collect_outputs
    monkeypatch.setattr(files, "collect_outputs", lambda *_a, **_k: {})
    _settle(state, record)
    monkeypatch.setattr(files, "collect_outputs", original)
    peer.put(ROOT + "/answer.txt", b"99\n")
    result = files.collect_outputs(state, record)
    assert not result["ok"] and result["errors"]
    assert not result["files"]
    assert list(Path(result["local_root"]).iterdir()) == []


def test_explicit_larger_budget_retrieves_the_same_frozen_file(managed, monkeypatch):
    state, record, peer = managed
    _launch(state, record)
    peer.put(ROOT + "/answer.bin", b"abcde")
    original = files.collect_outputs
    monkeypatch.setattr(files, "collect_outputs", lambda *_a, **_k: {})
    _settle(state, record)
    monkeypatch.setattr(files, "collect_outputs", original)
    small = files.collect_outputs(state, record, max_bytes=4)
    assert not small["ok"] and small["retained_remote"]
    assert "fetch" not in peer.calls
    enough = files.collect_outputs(state, record, max_bytes=5)
    assert enough["ok"] and enough["bytes_transferred"] == 5


def test_preexisting_remote_file_is_not_a_result_producer(managed):
    state, record, peer = managed
    peer.put(ROOT + "/old.txt", b"old")
    _launch(state, record)
    _settle(state, record)
    projected = ledger.project_output_observations(ledger.open_ledger(state).records())
    assert not any(r.get("created") or r.get("modified") for r in projected)


def test_changed_ssh_alias_cannot_fetch_from_another_cluster(managed, monkeypatch):
    state, record, peer = managed
    _launch(state, record)
    peer.put(ROOT + "/answer.txt", b"42")
    original = files.collect_outputs
    monkeypatch.setattr(files, "collect_outputs", lambda *_a, **_k: {})
    _settle(state, record)
    monkeypatch.setattr(files, "collect_outputs", original)
    peer.cluster = "foreign-cluster"
    assert not files.collect_outputs(state, record)["ok"]
    assert "fetch" not in peer.calls


def test_existing_local_edit_is_preserved_instead_of_overwritten(managed):
    state, record, peer = managed
    _launch(state, record)
    peer.put(ROOT + "/answer.txt", b"42")
    first = _settle(state, record)["output_transfer"]
    local = Path(first["files"][0]["local_path"])
    local.write_bytes(b"edited")
    result = files.collect_outputs(state, record)
    assert not result["ok"]
    assert local.read_bytes() == b"edited"


def test_snapshot_outside_requested_roots_is_unknown(managed):
    _, _, peer = managed
    peer.put("/etc/unrelated", b"must not collect")
    result = files.snapshot(TARGET, [ROOT])
    assert result["truncated"] and result["snapshot_error"] and result["entries"] == {}


def test_remote_absence_is_case_sensitive_on_windows():
    before = {"entries": {}, "root_coverage": [{"root": "/scratch/Output", "status": "complete"}]}
    after = {"entries": {"/scratch/output/new": _identity("/scratch/output/new", b"a")}, "root_coverage": []}
    result = files.remote_diff(before, after)
    assert not result["created"] and result["unknown"] == ["/scratch/output/new"]


def test_stage_uses_posix_names_and_checks_approved_source_bytes(managed, tmp_path):
    state, record, peer = managed
    source = tmp_path / "input space.txt"
    source.write_bytes(b"input")
    script = tmp_path / "job.sh"
    script.write_bytes(b"#!/bin/bash\necho hi\n")
    stage = [{"src": str(source), "dst": "input/data.txt", "mode": "0644",
              "sha256": hashlib.sha256(b"input").hexdigest()}]
    mapped = files.remote_stage_in(stage, ROOT + "/.bootstrap")
    assert mapped[0]["src"] == ROOT + "/.bootstrap/inputs/0"
    assert stage[0]["src"] == str(source)
    source.write_bytes(b"changed")
    result = files.prepare_submission(state, TARGET, nonce="abc", job_name="test", workdir=ROOT,
                                      output_dir=ROOT, stage_in=stage, script_path=script,
                                      remote_user="remote-user", scheduler_cluster="test-cluster")
    assert not result["ok"] and result["job_submitted"] is False
    assert "upload" not in peer.calls


def test_stage_uploads_exact_script_and_input_bytes(managed, tmp_path):
    state, record, peer = managed
    source, script = tmp_path / "source", tmp_path / "job.sh"
    source.write_bytes(bytes(range(256)))
    script.write_bytes(b"#!/bin/bash\nprintf 'done\\n'\n")
    result = files.prepare_submission(state, TARGET, nonce="abc", job_name="test", workdir=ROOT,
                                      output_dir=ROOT, stage_in=[{"src": str(source), "dst": "file", "mode": "0644",
                                      "sha256": hashlib.sha256(source.read_bytes()).hexdigest()}], script_path=script,
                                      remote_user="remote-user", scheduler_cluster="test-cluster")
    assert result["ok"], result
    assert peer.content[result["remote_script_path"]] == script.read_bytes()
    assert peer.content[result["bootstrap_log_dir"] + "/inputs/0"] == source.read_bytes()


def test_scheduler_logs_use_their_separate_approved_root(managed, tmp_path):
    _, original, peer = managed
    state = State.new("experiment", tmp_path / "separate-runs")
    owner = ledger.open_ledger(state, write=True, bootstrap=True)
    ledger.mark_bootstrap_ready(state)
    record = {**original, "scheduler_output_dir": "/scratch/logs"}
    owner.append("intent", nonce=record["submission_nonce"], scheduler="slurm", output_root=str(tmp_path / "local"),
                 **{k: v for k, v in record.items() if k not in {"submission_nonce", "scheduler", "job_id"}})
    _launch(state, record)
    peer.put("/scratch/logs/job.out", b"execution log")
    result = _settle(state, record)
    assert result["output_transfer"]["ok"], result
    assert Path(result["output_transfer"]["files"][0]["local_path"]).read_bytes() == b"execution log"


def test_receipt_failure_preserves_terminal_and_recovers_large_mirror(managed, monkeypatch):
    state, record, peer = managed
    _launch(state, record)
    content = b"a" * (capture.SMALL_FILE_BYTES + 1)
    path = ROOT + "/large.bin"
    peer.put(path, content)
    peer.entries[path].pop("sha256")
    peer.entries[path]["sha256_skipped"] = "size_over_cap"
    original = files._receipt
    monkeypatch.setattr(files, "_receipt", lambda *_a, **_k: (_ for _ in ()).throw(ledger.LedgerError("disk failure")))
    result = _settle(state, record)
    assert result["settled"] and not result["output_transfer"]["ok"]
    assert len(list(Path(result["output_transfer"]["local_root"]).iterdir())) == 1
    monkeypatch.setattr(files, "_receipt", original)
    result = files.collect_outputs(state, record)
    assert result["ok"], result
    assert Path(result["files"][0]["local_path"]).read_bytes() == content
    rows = ledger.open_ledger(state).records()
    assert sum(r["kind"] == "job_terminal" for r in rows) == 1
    assert sum(r["kind"] == "job_output_collected" for r in rows) == 1


def test_concurrent_local_file_is_never_overwritten(managed, monkeypatch):
    import os
    state, record, peer = managed
    _launch(state, record)
    peer.put(ROOT + "/result", b"scientific result")
    publish_name = "rename" if os.name == "nt" else "link"
    original = getattr(files.os, publish_name)
    def publish(source, destination, *args, **kwargs):
        Path(destination).write_bytes(b"concurrent edit")
        return original(source, destination, *args, **kwargs)
    monkeypatch.setattr(files.os, publish_name, publish)
    result = _settle(state, record)["output_transfer"]
    assert not result["ok"] and result["errors"]
    assert [p.read_bytes() for p in Path(result["local_root"]).iterdir()] == [b"concurrent edit"]


def test_large_local_rewrite_before_receipt_is_not_a_verified_replica(managed, monkeypatch):
    state, record, peer = managed
    _launch(state, record)
    size = capture.SMALL_FILE_BYTES + 1
    peer.put(ROOT + "/large", b"a" * size)
    original = files._receipt
    def change_then_record(state, terminal, remote_path, remote_identity, destination, digest):
        destination.write_bytes(b"b" * size)
        return original(state, terminal, remote_path, remote_identity, destination, digest)
    monkeypatch.setattr(files, "_receipt", change_then_record)
    result = _settle(state, record)
    assert result["settled"] and not result["output_transfer"]["ok"]
    assert not any(r["kind"] == "job_output_collected" for r in ledger.open_ledger(state).records())

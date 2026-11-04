"""A returned copy keeps its original job producer and immutable terminal facts."""
from __future__ import annotations

import copy
import hashlib

import pytest

from nodes.experiment.tools import capture, finish, ledger


def _records(tmp_path, *, large=False, remote_before=None):
    path = tmp_path / "result.bin"
    path.write_bytes(b"x" * (capture.SMALL_FILE_BYTES + 1) if large else b"result")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    local = capture.identity(path)
    remote = {"kind": "file", "size": local["size"], "inode": 123, "mtime_ns": 456}
    if not large:
        remote["sha256"] = digest
    source = "/scratch/result.bin"
    nonce = "source-job"
    terminal = {
        "kind": "job_terminal", "seq": 2, "nonce": nonce, "scheduler": "slurm",
        "output_observation": f"job-{nonce}", "derived_state": "COMPLETED", "exit_code": 0,
        "output_diff": {
            "coordinate_space": "slurm_remote", "created": [source] if remote_before is None else [],
            "modified": [source] if remote_before is not None else [], "unknown": [], "truncated": False,
            "identities": {source: {"before": remote_before, "after": remote, "before_unknown": False}},
        },
    }
    collected = {
        "kind": "job_output_collected", "seq": 3, "nonce": nonce, "terminal_seq": 2,
        "remote_path": source, "remote_identity": copy.deepcopy(remote),
        "local_path": str(path), "local_identity": local, "content_sha256": digest,
        "verification_basis": "frozen_metadata_and_transfer_sha256" if large else "frozen_sha256_and_transfer_sha256",
    }
    return path, terminal, collected


def test_collection_projects_the_existing_job_without_a_new_action_or_exit(tmp_path):
    path, terminal, collected = _records(tmp_path)
    original = copy.deepcopy(terminal)
    rows = [terminal, collected]
    assert ledger.project_output_observations([terminal]) == []
    observations = ledger.project_output_observations(rows)
    assert len(observations) == 1
    assert observations[0]["source_kind"] == "job_output_collected"
    assert observations[0]["action_id"] == "job-source-job"
    producers = finish._producer_rows(rows, path)
    assert [row["action_id"] for row in producers] == ["job-source-job"]
    assert finish._identity_matches(producers[0]["after"], capture.identity(path))[0]
    assert ledger.project_actions(rows) == []
    assert terminal == original


@pytest.mark.parametrize(("field", "value"), [
    ("nonce", "another-job"), ("terminal_seq", 999), ("remote_path", "/scratch/not-observed"),
    ("content_sha256", "0" * 64), ("local_identity", {"kind": "symlink", "target": "/elsewhere"}),
    ("remote_identity", {"kind": "file", "size": 6, "inode": 999, "mtime_ns": 456}),
])
def test_collection_cannot_invent_provenance_from_a_mismatched_receipt(tmp_path, field, value):
    path, terminal, collected = _records(tmp_path)
    collected[field] = value
    assert finish._producer_rows([terminal, collected], path) == []


@pytest.mark.parametrize("mutation", ["unchanged", "unknown", "hash_error", "before_unknown", "local_hash", "local_size"])
def test_a_download_does_not_promote_unproduced_or_unverified_bytes(tmp_path, mutation):
    path, terminal, collected = _records(tmp_path)
    diff = terminal["output_diff"]
    identity = diff["identities"][collected["remote_path"]]
    if mutation == "unchanged":
        diff["created"] = []
    elif mutation == "unknown":
        diff["unknown"] = [collected["remote_path"]]
    elif mutation == "hash_error":
        identity["after"]["hash_error"] = "unreadable"
        collected["remote_identity"] = copy.deepcopy(identity["after"])
    elif mutation == "before_unknown":
        identity["before_unknown"] = True
    elif mutation == "local_hash":
        collected["local_identity"]["sha256"] = "0" * 64
    else:
        collected["local_identity"]["size"] += 1
    assert finish._producer_rows([terminal, collected], path) == []


def test_metadata_only_remote_change_is_not_promoted_by_copying(tmp_path):
    path, terminal, collected = _records(tmp_path)
    remote = terminal["output_diff"]["identities"][collected["remote_path"]]
    remote["before"] = {**remote["after"], "mtime_ns": 123}
    terminal["output_diff"]["created"] = []
    terminal["output_diff"]["modified"] = [collected["remote_path"]]
    assert finish._producer_rows([terminal, collected], path) == []


def test_same_bytes_from_a_replaced_remote_object_keep_a_job_producer(tmp_path):
    path, terminal, collected = _records(tmp_path)
    remote = terminal["output_diff"]["identities"][collected["remote_path"]]
    remote["before"] = {**remote["after"], "inode": 122}
    terminal["output_diff"]["created"] = []
    terminal["output_diff"]["modified"] = [collected["remote_path"]]
    assert finish._producers([terminal, collected], path) == ["job-source-job"]


def test_large_file_uses_frozen_metadata_and_separate_transfer_hash(tmp_path):
    path, terminal, collected = _records(tmp_path, large=True)
    assert "sha256" not in collected["local_identity"]
    producers = finish._producer_rows([terminal, collected], path)
    assert len(producers) == 1
    assert finish._identity_matches(producers[0]["after"], capture.identity(path))[0]
    bad = {**collected, "verification_basis": "hashes_look_ok"}
    assert finish._producer_rows([terminal, bad], path) == []


def test_retry_collection_can_refresh_the_mirror_without_rewriting_terminal(tmp_path):
    path, terminal, collected = _records(tmp_path)
    original = copy.deepcopy(terminal)
    replacement = tmp_path / "replacement"
    replacement.write_bytes(path.read_bytes())
    replacement.replace(path)
    retry = {**collected, "seq": 4, "local_identity": capture.identity(path)}
    current = [p for p in finish._producer_rows([terminal, collected, retry], path)
               if finish._identity_matches(p["after"], capture.identity(path))[0]]
    assert current and {p["action_id"] for p in current} == {"job-source-job"}
    assert terminal == original


def test_parameter_lineage_still_comes_from_the_original_job_intent(tmp_path):
    path, terminal, collected = _records(tmp_path)
    context = {"parameter_source": "tool_argument_witness", "parameter_comparison": {"applicable": False}}
    rows = [{"kind": "intent", "seq": 1, "nonce": "source-job", "execution_context": context}, terminal, collected]
    evaluation = {"checks": [{"required": True, "evidence": {"producers": finish._producers(rows, path)}}]}
    lineage, gaps, contradictions, limits = finish._execution_lineage_findings({"scope": "operation"}, rows, evaluation)
    assert [r["action_id"] for r in lineage] == ["job-source-job"]
    assert lineage[0]["parameter_source"] == "tool_argument_witness"
    assert not gaps and not contradictions


def test_remote_detail_loss_cannot_use_the_retained_summary_to_attribute_a_mirror(tmp_path):
    path, terminal, collected = _records(tmp_path)
    terminal["capture_record"] = str(tmp_path)
    terminal["output_diff"]["observation_ref"] = {"file": "missing", "sha256": "0" * 64, "size_bytes": 3}
    assert finish._producer_rows([terminal, collected], path) == []

"""R3-A/C: the neutral delivery triplet is one recoverable transaction."""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from core.state import State
from nodes.experiment.tools import delivery_transaction as tx, ledger


def _state(tmp_path):
    state = State.new(
        node_type="experiment", base_dir=tmp_path / "runs",
        project_id="p", session_id="owner",
    )
    led = ledger.open_ledger(state, write=True, bootstrap=True)
    ledger.mark_bootstrap_ready(state)
    return state, led


def _manifest(state, *, outcome="failed", summary="first"):
    delivery = {
        "schema": tx.DELIVERY_SCHEMA,
        "delivery_id": tx.delivery_id(state),
        "delivery_owner": "finish",
        "scope": "operation",
        "scope_status": "resolved",
        "outcome": outcome,
        "execution_status": "attempted",
        "result_status": "unavailable" if outcome != "success" else "delivered",
        "summary": summary,
        "gaps": [],
        "contradictions": [],
        "formal_input": {"status": "none", "binding": None, "error": None},
        "limitations": [],
        "external_job_refs": [],
        "next_step": None,
        "blocker": {"code": "requested_block"} if outcome == "blocked" else None,
    }
    names = tx.deterministic_names(state)
    ids = {kind: tx.artifact_id(kind, names[kind]) for kind in tx.TRIO}
    contents = {
        "raw_results": json.dumps({"delivery": delivery, "files": []}, sort_keys=True),
        "clean_results": json.dumps({
            "delivery": delivery,
            "raw_results_artifact_id": ids["raw_results"],
        }, sort_keys=True),
        "experiment_log": f"# Experiment delivery\n\n{summary}\n",
    }
    return tx.build_manifest(state, delivery=delivery, contents=contents)


def _finish_rows(led):
    return [row for row in led.records() if row.get("kind") == "finish"]


def test_one_neutral_triplet_is_frozen_and_reentry_reads_first_delivery(tmp_path):
    state, led = _state(tmp_path)
    first = tx.settle_delivery(state, led, _manifest(state, outcome="failed", summary="first"))
    assert first["delivery"]["schema"] == "ExperimentDeliveryV1"
    assert first["delivery"]["outcome"] == "failed"
    assert all("__experiment_" in value for value in first["artifact_ids"].values())
    digest = first["delivery_digest"]
    for kind, artifact in first["artifact_ids"].items():
        head = state.artifact_head(artifact)
        record = state.read_artifact(artifact)
        assert head.frozen and head.version == head.frozen_version == 1
        assert head.sha256 == head.frozen_sha256
        planned = tx.read_manifest(state)["artifacts"][kind]["metadata"]
        assert record["metadata"] == {
            **planned, "frozen": True, "frozen_at": head.frozen_at,
        }
        assert record["metadata"]["record_kind"] == "experiment_delivery"
        assert record["metadata"]["delivery_owner"] == "finish"
        assert record["metadata"]["envelope_sha256"] == digest
        assert record["metadata"]["artifact_role"] == kind
        assert "frozen_version" not in record["metadata"]
        assert "frozen_sha256" not in record["metadata"]
        assert len(state.artifact_versions(artifact)) == 1
    replay = tx.settle_delivery(
        state, led, _manifest(state, outcome="success", summary="must be ignored"))
    assert replay["reused_manifest"] is True
    assert replay["delivery"]["outcome"] == "failed"
    assert replay["delivery"]["summary"] == "first"
    assert replay["artifact_ids"] == first["artifact_ids"]
    assert len(_finish_rows(led)) == 1


def test_save_completed_then_raised_is_adopted_without_a_second_version(tmp_path, monkeypatch):
    state, led = _state(tmp_path)
    proposed = _manifest(state)
    real = state.save_artifact
    crashed = {"done": False}

    def save_then_raise(*args, **kwargs):
        saved = real(*args, **kwargs)
        if args[0] == "raw_results" and not crashed["done"]:
            crashed["done"] = True
            raise OSError("injected after save")
        return saved

    monkeypatch.setattr(state, "save_artifact", save_then_raise)
    with pytest.raises(OSError, match="injected after save"):
        tx.settle_delivery(state, led, proposed)
    monkeypatch.setattr(state, "save_artifact", real)
    recovered = tx.settle_delivery(state, led, _manifest(state, summary="ignored"))
    assert recovered["status"] == "success"
    for artifact in recovered["artifact_ids"].values():
        assert len(state.artifact_versions(artifact)) == 1
    assert len(_finish_rows(led)) == 1


def test_all_identities_are_prepared_before_the_first_freeze(tmp_path, monkeypatch):
    state, led = _state(tmp_path)
    seen = {"checked": False}

    def stop_at_first_freeze(*_args, **_kwargs):
        manifest = tx.read_manifest(state)
        assert manifest["prepared_digest"]
        assert all(manifest["artifacts"][kind]["prepared"] for kind in tx.TRIO)
        assert all(manifest["artifacts"][kind]["frozen"] is None for kind in tx.TRIO)
        seen["checked"] = True
        raise OSError("injected before first freeze")

    monkeypatch.setattr(state, "mark_frozen", stop_at_first_freeze)
    with pytest.raises(OSError, match="injected before first freeze"):
        tx.settle_delivery(state, led, _manifest(state))
    assert seen["checked"] is True
    assert not _finish_rows(led)


def test_prepared_head_drift_fails_loudly_without_commit(tmp_path, monkeypatch):
    state, led = _state(tmp_path)
    real_freeze = state.mark_frozen
    monkeypatch.setattr(
        state, "mark_frozen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("stop after prepare")),
    )
    with pytest.raises(OSError, match="stop after prepare"):
        tx.settle_delivery(state, led, _manifest(state))
    monkeypatch.setattr(state, "mark_frozen", real_freeze)
    manifest = tx.read_manifest(state)
    raw = manifest["artifacts"]["raw_results"]
    state.save_artifact(
        "raw_results", raw["name"], raw["content"] + "\nchanged",
        metadata=raw["metadata"],
    )
    with pytest.raises(tx.DeliveryTransactionError) as caught:
        tx.settle_delivery(state, led, _manifest(state))
    assert caught.value.code == "closure_head_mismatch"
    assert not _finish_rows(led)
    assert all(not (state.artifact_head(manifest["artifacts"][kind]["id"]).frozen)
               for kind in tx.TRIO)


def test_commit_succeeded_then_raised_recovers_exactly_one_finish(tmp_path, monkeypatch):
    state, led = _state(tmp_path)
    real = led.append_once
    crashed = {"done": False}

    def append_then_raise(*args, **kwargs):
        row = real(*args, **kwargs)
        if not crashed["done"]:
            crashed["done"] = True
            raise OSError("injected after commit")
        return row

    monkeypatch.setattr(led, "append_once", append_then_raise)
    with pytest.raises(OSError, match="injected after commit"):
        tx.settle_delivery(state, led, _manifest(state))
    reopened = ledger.open_ledger(state, write=True)
    recovered = tx.settle_delivery(state, reopened, _manifest(state, summary="ignored"))
    assert recovered["status"] == "success"
    assert len(_finish_rows(reopened)) == 1
    assert all(len(state.artifact_versions(artifact)) == 1
               for artifact in recovered["artifact_ids"].values())


def test_last_freeze_succeeded_then_raised_is_adopted_before_one_commit(tmp_path, monkeypatch):
    state, led = _state(tmp_path)
    real = state.mark_frozen
    crashed = {"done": False}

    def freeze_then_raise(artifact_id, *args, **kwargs):
        result = real(artifact_id, *args, **kwargs)
        if artifact_id.startswith("experiment_log__") and not crashed["done"]:
            crashed["done"] = True
            raise OSError("injected after last freeze")
        return result

    monkeypatch.setattr(state, "mark_frozen", freeze_then_raise)
    with pytest.raises(OSError, match="injected after last freeze"):
        tx.settle_delivery(state, led, _manifest(state))
    assert not _finish_rows(led)
    monkeypatch.setattr(state, "mark_frozen", real)
    recovered = tx.settle_delivery(state, led, _manifest(state, summary="ignored"))
    assert recovered["status"] == "success"
    assert len(_finish_rows(led)) == 1
    assert all(state.artifact_head(artifact).frozen
               for artifact in recovered["artifact_ids"].values())


def test_transaction_rejects_a_ledger_from_another_run_without_writing_either(tmp_path):
    state, led = _state(tmp_path / "a")
    other, other_ledger = _state(tmp_path / "b")
    with pytest.raises(tx.DeliveryTransactionError) as caught:
        tx.settle_delivery(state, other_ledger, _manifest(state))
    assert caught.value.code == "closure_ledger_mismatch"
    assert not _finish_rows(led)
    assert not _finish_rows(other_ledger)
    assert tx.read_manifest(state) is None
    assert tx.read_manifest(other) is None


def test_a_second_finish_for_the_same_closure_is_detected_not_ignored(tmp_path):
    state, led = _state(tmp_path)
    tx.settle_delivery(state, led, _manifest(state))
    led.append("finish", idempotency_key="foreign", closure_id=tx.delivery_id(state),
               prepared_digest="foreign", delivery_digest="foreign",
               artifact_ids={}, artifact_identities={}, outcome="failed")
    with pytest.raises(tx.DeliveryTransactionError) as caught:
        tx.settle_delivery(state, led, None)
    assert caught.value.code == "closure_finish_commit_invalid"
    assert len(_finish_rows(led)) == 2


@pytest.mark.parametrize("missing", [True, False])
def test_read_only_seal_verifier_requires_the_manifest_commit_checkpoint(tmp_path, missing):
    state, led = _state(tmp_path)
    tx.settle_delivery(state, led, _manifest(state))
    manifest = tx.read_manifest(state)
    if missing:
        manifest["commit"] = None
    else:
        manifest["commit"]["seq"] += 1
    with pytest.raises(tx.DeliveryTransactionError) as caught:
        tx._sealed(state, led, manifest)
    assert caught.value.code == "closure_manifest_commit_mismatch"
    assert len(_finish_rows(led)) == 1


def test_minimal_or_semantically_invalid_delivery_never_freezes(tmp_path):
    state, led = _state(tmp_path)
    minimal = {"schema": tx.DELIVERY_SCHEMA, "delivery_id": tx.delivery_id(state)}
    with pytest.raises(tx.DeliveryTransactionError) as caught:
        tx.build_manifest(state, delivery=minimal, contents={kind: "x" for kind in tx.TRIO})
    assert caught.value.code == "delivery_envelope_invalid"
    assert tx.read_manifest(state) is None and not _finish_rows(led)

    invalid = dict(_manifest(state)["delivery"])
    # Unknown is legal for success; this claim is still invalid because it
    # retains not_run/unavailable facts while claiming a successful delivery.
    invalid.update({"scope": "unknown", "scope_status": "undecided", "outcome": "success"})
    with pytest.raises(tx.DeliveryTransactionError) as caught:
        tx.build_manifest(state, delivery=invalid, contents={kind: "x" for kind in tx.TRIO})
    assert caught.value.code == "delivery_envelope_invalid"
    assert tx.read_manifest(state) is None and not _finish_rows(led)


@pytest.mark.parametrize("field,value", [
    ("operation_closure_id", "legacy"),
    ("scientific_contribution", True),
    ("analysis_eligible", False),
    ("frozen", True),
    ("freeze_reason", "foreign"),
    ("schema_version", True),
])
def test_finish_metadata_rejects_legacy_or_freeze_control_fields(tmp_path, field, value):
    state, led = _state(tmp_path)
    proposed = _manifest(state)
    proposed["artifacts"]["raw_results"]["metadata"][field] = value
    proposed["plan_digest"] = tx.canonical_digest(tx._plan_material(proposed))
    with pytest.raises(tx.DeliveryTransactionError) as caught:
        tx.settle_delivery(state, led, proposed)
    assert caught.value.code == "closure_manifest_metadata_invalid"
    assert tx.read_manifest(state) is None and not _finish_rows(led)


def test_delivery_payloads_and_envelope_are_exact_json_not_string_coercions(tmp_path):
    state, _led = _state(tmp_path)
    delivery = _manifest(state)["delivery"]
    with pytest.raises(tx.DeliveryTransactionError) as caught:
        tx.build_manifest(
            state, delivery=delivery,
            contents={"raw_results": object(), "clean_results": "x", "experiment_log": "x"})
    assert caught.value.code == "closure_payload_invalid"

    for non_json in (object(), ("tuple",), {1: "integer-key"}, float("nan"), float("inf")):
        invalid = dict(delivery)
        invalid["limitations"] = [non_json]
        with pytest.raises(tx.DeliveryTransactionError) as caught:
            tx.build_manifest(state, delivery=invalid, contents={kind: "x" for kind in tx.TRIO})
        assert caught.value.code == "canonical_json_invalid"

    with pytest.raises(tx.DeliveryTransactionError) as caught:
        tx.build_manifest(
            state, delivery=delivery,
            contents={"raw_results": "\ud800", "clean_results": "x", "experiment_log": "x"})
    assert caught.value.code == "closure_payload_invalid"


@pytest.mark.parametrize("change", [
    {"scope": "operation", "formal_input": {
        "status": "valid", "binding": {
            "artifact_id": "pre_registration__x", "version": 1,
            "content_hash": "a" * 64,
        }, "error": None,
    }},
    {"scope": "scientific", "formal_input": {
        "status": "invalid", "binding": None, "error": None,
    }},
    {"scope": "operation", "outcome": "success", "execution_status": "not_run",
     "result_status": "delivered"},
    {"scope": "operation", "outcome": "blocked", "blocker": None},
    {"scope": "scientific", "formal_input": {
        "status": "valid", "binding": {
            "artifact_id": "pre_registration__x", "version": 1,
            "content_hash": int("1" * 64),
        }, "error": None,
    }},
    {"scope": "scientific", "formal_input": {
        "status": "invalid", "binding": None,
        "error": {"code": "invalid", "reason": "bad prereg", "status": "blocked"},
    }},
])
def test_delivery_cross_field_matrix_rejects_false_or_unexplained_claims(tmp_path, change):
    state, _led = _state(tmp_path)
    delivery = dict(_manifest(state)["delivery"])
    delivery.update(change)
    with pytest.raises(tx.DeliveryTransactionError) as caught:
        tx.build_manifest(state, delivery=delivery, contents={kind: "x" for kind in tx.TRIO})
    assert caught.value.code == "delivery_envelope_invalid"


def test_manifest_and_frozen_record_metadata_drift_fail_closed(tmp_path):
    state, led = _state(tmp_path)
    proposed = _manifest(state)
    proposed["artifacts"]["raw_results"]["metadata"]["schema_version"] = 2
    proposed["plan_digest"] = tx.canonical_digest(tx._plan_material(proposed))
    with pytest.raises(tx.DeliveryTransactionError) as caught:
        tx.settle_delivery(state, led, proposed)
    assert caught.value.code == "closure_manifest_metadata_invalid"
    assert not _finish_rows(led)

    clean_state, clean_ledger = _state(tmp_path / "clean")
    sealed = tx.settle_delivery(clean_state, clean_ledger, _manifest(clean_state))
    raw_id = sealed["artifact_ids"]["raw_results"]
    clean_state.mark_frozen(raw_id, metadata_patch={"scope": "scientific"})
    with pytest.raises(tx.DeliveryTransactionError) as caught:
        tx.settle_delivery(clean_state, clean_ledger, None)
    assert caught.value.code == "closure_head_mismatch"
    assert "metadata" in caught.value.facts["mismatches"]


def test_a_second_freeze_timestamp_is_detected_as_identity_drift(tmp_path):
    state, led = _state(tmp_path)
    sealed = tx.settle_delivery(state, led, _manifest(state))
    raw_id = sealed["artifact_ids"]["raw_results"]
    state.mark_frozen(raw_id, metadata_patch=dict(
        tx.read_manifest(state)["artifacts"]["raw_results"]["metadata"]))
    with pytest.raises(tx.DeliveryTransactionError) as caught:
        tx.settle_delivery(state, led, None)
    assert caught.value.code == "closure_head_mismatch"
    assert "frozen_identity" in caught.value.facts["mismatches"]


@pytest.mark.parametrize("checkpoint", ["prepared", "frozen"])
@pytest.mark.parametrize("version", [True, 1.0])
def test_manifest_identity_versions_preserve_json_types(tmp_path, monkeypatch, checkpoint, version):
    state, led = _state(tmp_path)
    proposed = _manifest(state)
    if checkpoint == "prepared":
        real_freeze = state.mark_frozen
        monkeypatch.setattr(
            state, "mark_frozen",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("stop after prepare")),
        )
        with pytest.raises(OSError, match="stop after prepare"):
            tx.settle_delivery(state, led, proposed)
        monkeypatch.setattr(state, "mark_frozen", real_freeze)
    else:
        tx.settle_delivery(state, led, proposed)
    manifest = tx.read_manifest(state)
    manifest["artifacts"]["raw_results"][checkpoint]["version"] = version
    manifest["prepared_digest"] = tx.canonical_digest({
        kind: manifest["artifacts"][kind]["prepared"] for kind in tx.TRIO
    })
    tx.manifest_path(state).write_text(json.dumps(manifest), encoding="utf-8")
    previous_finishes = _finish_rows(led)
    with pytest.raises(tx.DeliveryTransactionError) as caught:
        tx.settle_delivery(state, led, None)
    assert caught.value.code == f"closure_manifest_{checkpoint}_invalid"
    assert _finish_rows(led) == previous_finishes


def test_manifest_replace_failure_is_structured_and_retryable(tmp_path, monkeypatch):
    state, led = _state(tmp_path)
    proposed = _manifest(state)
    real_replace = tx.os.replace
    failed = {"once": False}

    def fail_manifest_replace(source, target):
        if str(target).endswith("closure.manifest.json") and not failed["once"]:
            failed["once"] = True
            raise OSError("injected replace failure")
        return real_replace(source, target)

    monkeypatch.setattr(tx.os, "replace", fail_manifest_replace)
    with pytest.raises(tx.DeliveryTransactionError) as caught:
        tx.settle_delivery(state, led, proposed)
    assert caught.value.code == "closure_manifest_persist_failed"
    assert caught.value.facts["checkpoint"] == "replace"
    assert caught.value.facts["replace_completed"] is False
    assert not _finish_rows(led)
    monkeypatch.setattr(tx.os, "replace", real_replace)
    recovered = tx.settle_delivery(state, led, proposed)
    assert recovered["status"] == "success" and len(_finish_rows(led)) == 1


def test_post_replace_directory_sync_failure_recovers_without_duplicate_finish(tmp_path, monkeypatch):
    state, led = _state(tmp_path)
    proposed = _manifest(state)
    real_sync = tx._fsync_dir
    failed = {"once": False}

    def fail_closure_sync(path):
        if path == tx.manifest_path(state).parent and not failed["once"]:
            failed["once"] = True
            raise tx.DeliveryTransactionError(
                "closure_durability_unavailable", "injected directory sync failure", path=str(path))
        return real_sync(path)

    monkeypatch.setattr(tx, "_fsync_dir", fail_closure_sync)
    with pytest.raises(tx.DeliveryTransactionError) as caught:
        tx.settle_delivery(state, led, proposed)
    assert caught.value.code == "closure_durability_unavailable"
    assert caught.value.facts["checkpoint"] == "manifest_dir_fsync"
    assert caught.value.facts["replace_completed"] is True
    assert caught.value.facts["canonical_visible"] is True
    assert tx.read_manifest(state) is not None
    monkeypatch.setattr(tx, "_fsync_dir", real_sync)
    recovered = tx.settle_delivery(state, led, None)
    assert recovered["status"] == "success" and len(_finish_rows(led)) == 1


def test_two_finish_callers_share_one_manifest_triplet_and_commit(tmp_path):
    state, led = _state(tmp_path)
    other_state = State.reopen(
        run_id=state.run_id, node_type="experiment", base_dir=tmp_path / "runs",
        project_id="p", session_id="owner",
    )
    other_ledger = ledger.open_ledger(other_state, write=True)
    proposals = (_manifest(state, summary="first"), _manifest(other_state, summary="second"))
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(
            lambda args: tx.settle_delivery(*args),
            ((state, led, proposals[0]), (other_state, other_ledger, proposals[1])),
        ))
    assert results[0]["artifact_ids"] == results[1]["artifact_ids"]
    persisted = tx.read_manifest(state)
    assert {result["delivery_digest"] for result in results} == {persisted["delivery_digest"]}
    for artifact in results[0]["artifact_ids"].values():
        assert len(state.artifact_versions(artifact)) == 1
    rows = ledger.open_ledger(state).records()
    assert len([row for row in rows if row.get("kind") == "finish"]) == 1
    assert [row["seq"] for row in rows] == list(range(1, len(rows) + 1))

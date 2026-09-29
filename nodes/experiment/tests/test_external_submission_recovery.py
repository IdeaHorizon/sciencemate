from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from core.artifact_provenance import produced
from core.state import State
from nodes.experiment import hooks
from nodes.experiment.tools import external_submission_recovery as recovery
from nodes.experiment.tools.pbs_scheduler import reset_pbs_flavor_cache_for_tests


@pytest.fixture(autouse=True)
def _isolate_pbs_flavor_probe():
    reset_pbs_flavor_cache_for_tests()
    yield
    reset_pbs_flavor_cache_for_tests()


_RUNTIME_ID_A = "a" * 64
_IMAGE_ID = "sha256:" + "c" * 64
_CONTAINER_NAME = "hf-test-recovered-job"


def _local_inspection(runtime_id: str | None = _RUNTIME_ID_A) -> dict:
    return {
        "exists": True,
        "id": runtime_id,
        "image_id": _IMAGE_ID,
        "managed": True,
        "kind": "job",
        "name": _CONTAINER_NAME,
    }


def _save_intent(
    tmp_path: Path,
    scheduler: str,
    *,
    attempt: str = "route-0123456789abcdef",
    namespace: str | None = None,
    extra: dict | None = None,
    produced_by_run_id: str | None = None,
) -> tuple[State, str, str]:
    state = State.new("experiment", tmp_path / "runs")
    payload = {
        "status": "success",
        "intent_status": "prepared",
        "scheduler": scheduler,
        "dry_run": False,
        "job_name": "recover-me",
        "submitted_at": "2026-08-26T01:02:03+00:00",
        "submission_nonce": attempt,
        "route_attempt_id": attempt,
        "script_sha256": "a" * 64,
        "command_sha256": "b" * 64,
        "script_path": str(tmp_path / "job.sh"),
        "workdir": str(tmp_path / "run"),
        "output_roots": [str(tmp_path / "run" / "outputs")],
        "namespace": namespace,
        **(extra or {}),
    }
    artifact = state.save_artifact(
        "external_submission_intent",
        f"external_submission_intent_{state.run_id}_{attempt}",
        json.dumps(payload),
        metadata={
            "submission_nonce": attempt,
            "scheduler": scheduler,
            "route_attempt_id": attempt,
        },
        # 产出方是账本事实：指定时模拟别的 run 留下（或被改标）的 intent。
        provenance=(produced("experiment", produced_by_run_id)
                    if produced_by_run_id else None),
    )
    state.append_transcript(
        "route_step_bound",
        attempt_id=attempt,
        tool="submit_job",
        applied_policy="managed_external_job",
    )
    state.append_transcript(
        "route_step_outcome",
        attempt_id=attempt,
        outcome="unknown",
        failure_class="external_identity_reconciliation_required",
    )
    return state, attempt, str(artifact["id"])


def _append_foreign_intent(
    state: State,
    source_intent_id: str,
    *,
    scheduler: str,
    attempt: str,
    produced_by_run_id: str,
) -> str:
    source = state.read_artifact(source_intent_id)
    payload = json.loads(source["content"])
    payload.update({
        "scheduler": scheduler,
        "route_attempt_id": attempt,
        "submission_nonce": attempt,
    })
    artifact = state.save_artifact(
        "external_submission_intent",
        f"external_submission_intent_{produced_by_run_id}_{attempt}",
        json.dumps(payload),
        metadata={
            "submission_nonce": attempt,
            "scheduler": scheduler,
            "route_attempt_id": attempt,
        },
        provenance=produced("experiment", produced_by_run_id),
    )
    state.append_transcript(
        "route_step_bound", attempt_id=attempt, tool="submit_job",
        applied_policy="managed_external_job")
    state.append_transcript(
        "route_step_outcome", attempt_id=attempt, outcome="unknown",
        failure_class="external_identity_reconciliation_required")
    return str(artifact["id"])


def _ok(stdout: str = "") -> dict:
    return {"ok": True, "returncode": 0, "stdout": stdout, "stderr": ""}


def _error(message: str = "query failed") -> dict:
    return {"ok": False, "returncode": 1, "stdout": "", "stderr": message}


def _intent_base(attempt: str, tmp_path: Path) -> dict:
    return {
        "status": "success",
        "scheduler": "slurm",
        "dry_run": False,
        "job_name": "pre-submit-abort",
        "submitted_at": "2026-08-27T00:00:00+00:00",
        "submission_nonce": attempt,
        "route_attempt_id": attempt,
        "script_sha256": "a" * 64,
        "command": "echo guarded",
        "script_path": str(tmp_path / "job.sh"),
        "workdir": str(tmp_path / "run"),
        "output_roots": [str(tmp_path / "run" / "outputs")],
        "namespace": None,
    }


def test_intent_transcript_failure_is_compensated_before_submit(
    tmp_path,
    monkeypatch,
):
    from nodes.experiment.tools import resource_manager as manager

    state = State.new("experiment", tmp_path / "runs")
    attempt = "route-pre-submit-abort"
    original_append = state.append_transcript

    def fail_intent_event(event, **payload):
        if event == "external_submission_intent_persisted":
            raise OSError("transcript unavailable")
        return original_append(event, **payload)

    monkeypatch.setattr(state, "append_transcript", fail_intent_event)
    result = manager._persist_submission_intent(
        state,
        _intent_base(attempt, tmp_path),
        route_attempt_id=attempt,
    )

    assert result["status"] == "error"
    assert result["intent_status"] == "aborted_before_submit"
    assert result["submission_boundary_crossed"] is False
    artifacts = state.list_artifacts("external_submission_intent")
    assert len(artifacts) == 1
    artifact_id = artifacts[0]["id"]
    head = state.read_artifact(artifact_id)
    assert head["version"] == 2
    assert json.loads(head["content"])["intent_status"] == "aborted_before_submit"
    versions = state.artifact_versions(artifact_id)
    assert [row["version"] for row in versions] == [1, 2]
    assert json.loads(versions[0]["content"])["intent_status"] == "prepared"
    assert recovery.dangling_external_submission_intents(state) == []

    queried = []
    reconciled = recovery.reconcile_external_submission(
        state,
        attempt,
        query_runner=lambda *args, **kwargs: queried.append((args, kwargs)),
    )
    assert reconciled["status"] == "already_terminal"
    assert reconciled["reason"] == "submission_aborted_before_submit"
    assert reconciled["output_roots_released"] is True
    assert queried == []


def test_failed_abort_compensation_remains_unknown_and_reserved(
    tmp_path,
    monkeypatch,
):
    from nodes.experiment.tools import resource_manager as manager

    state = State.new("experiment", tmp_path / "runs")
    attempt = "route-pre-submit-unknown"
    original_save = state.save_artifact
    saves = 0

    def fail_second_save(*args, **kwargs):
        nonlocal saves
        saves += 1
        if saves == 2:
            raise OSError("compensation unavailable")
        return original_save(*args, **kwargs)

    monkeypatch.setattr(state, "save_artifact", fail_second_save)
    monkeypatch.setattr(
        state,
        "append_transcript",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            OSError("transcript unavailable")
        ),
    )
    result = manager._persist_submission_intent(
        state,
        _intent_base(attempt, tmp_path),
        route_attempt_id=attempt,
    )

    assert result["intent_status"] == "prepared_outcome_unknown"
    assert result["do_not_resubmit"] is True
    dangling = recovery.dangling_external_submission_intents(state)
    assert len(dangling) == 1
    assert dangling[0]["status"] == "submission_outcome_unknown"
    assert dangling[0]["do_not_resubmit"] is True


def test_pbs_parser_requires_exact_nonce_and_handles_continuations():
    text = """Job Id: 10.server
    Job_Name = a
    Variable_List = FOO=1,AI4S_SUBMISSION_NONCE=ai4s:route-exact,
        BAR=2
Job Id: 11.server
    Variable_List = AI4S_SUBMISSION_NONCE=ai4s:route-exact-suffix
"""
    rows = recovery._parse_pbs_jobs(text, "route-exact")
    assert rows == [{
        "scheduler": "pbs", "job_id": "10.server", "namespace": None,
        "launch_host": None, "scheduler_cluster": "server",
        "resource_uid": None,
    }]


def test_slurm_parser_requires_exact_comment_and_deduplicates_in_query():
    rows = recovery._parse_slurm_rows(
        "41|ai4s:route-exact\n42|ai4s:route-exact-extra\n",
        "route-exact",
        cluster="alpha",
    )
    assert [row["job_id"] for row in rows] == ["41"]


@pytest.mark.parametrize(
    ("qstat", "expected_status", "expected_count"),
    [
        (_ok(""), "zero", 0),
        (_ok("""Job Id: 1.server
    Variable_List = AI4S_SUBMISSION_NONCE=ai4s:route-0123456789abcdef
Job Id: 2.server
    Variable_List = AI4S_SUBMISSION_NONCE=ai4s:route-0123456789abcdef
"""), "multiple", 2),
        (_error(), "query_error", 0),
    ],
)
def test_zero_multiple_and_query_error_remain_non_resubmittable(
    tmp_path: Path, qstat: dict, expected_status: str, expected_count: int,
):
    state, attempt, _intent_id = _save_intent(tmp_path, "pbs")
    result = recovery.reconcile_external_submission(
        state,
        attempt,
        query_runner=lambda _argv, **_kwargs: qstat,
    )
    assert result["status"] == "reconciliation_pending"
    assert result["query_status"] == expected_status
    assert result["candidate_count"] == expected_count
    assert result["do_not_resubmit"] is True
    receipt = state.read_artifact(result["recovery_artifact_id"])
    payload = json.loads(receipt["content"])
    assert payload["status"] == "submission_outcome_unknown"
    assert payload["job_id"] is None
    assert payload["do_not_resubmit"] is True
    assert payload["reconciliation"]["query_commands"] == [
        ["qstat", "--version"], ["qstat", "-f"], ["qstat", "-x", "-f"],
    ]
    assert "stdout" not in payload["reconciliation"]


def test_pbs_pro_active_zero_without_configured_history_is_query_error(
    tmp_path: Path,
):
    state, attempt, _intent_id = _save_intent(tmp_path, "pbs")

    def runner(argv, **_kwargs):
        if argv == ["qstat", "--version"]:
            return _ok("pbs_version = 2022.1.1")
        if "-x" in argv:
            return _error("PBS is not configured to maintain job history")
        return _ok("")

    result = recovery.reconcile_external_submission(
        state, attempt, query_runner=runner,
    )
    assert result["status"] == "reconciliation_pending"
    assert result["query_status"] == "query_error"
    assert result["reason"] == "pbs_history_not_configured_after_active_zero"
    receipt = state.read_artifact(result["recovery_artifact_id"])
    payload = json.loads(receipt["content"])
    assert payload["reconciliation"]["query_commands"] == [
        ["qstat", "--version"],
        ["qstat", "-x", "-f"],
        ["qstat", "-f"],
    ]


def test_slurm_unique_identity_projects_route_and_is_idempotent(tmp_path: Path):
    state, attempt, _intent_id = _save_intent(tmp_path, "slurm")
    calls: list[list[str]] = []

    def runner(argv, **_kwargs):
        calls.append(list(argv))
        if argv[:3] == ["scontrol", "show", "config"]:
            return _ok("ClusterName = alpha\n")
        if argv[0] == "squeue":
            return _ok(f"731|ai4s:{attempt}\n")
        if argv[0] == "sacct":
            return _ok(f"731|ai4s:{attempt}|alpha\n")
        raise AssertionError(argv)

    first = recovery.reconcile_external_submission(
        state, attempt, query_runner=runner)
    assert first["status"] == "success"
    assert first["resolved_identity"] == {
        "scheduler": "slurm", "job_id": "731", "namespace": None,
        "launch_host": None, "scheduler_cluster": "alpha",
        "resource_uid": None, "process_group_id": None,
        "process_start_ticks": None, "container_runtime_id": None,
    }
    assert calls == [
        ["scontrol", "show", "config"],
        ["squeue", "-h", "-o", "%i|%k"],
        [
            "sacct", "-X", "-n", "-P", "-S", "2026-08-26T00:57:03",
            "-o", "JobIDRaw,Comment%256,Cluster",
        ],
    ]
    events = state.transcript_path.read_text(encoding="utf-8")
    assert "route_step_external_identity_resolved" in events

    second = recovery.reconcile_external_submission(
        state, attempt,
        query_runner=lambda *_args, **_kwargs: pytest.fail("幂等恢复不应再次查询 scheduler"),
    )
    assert second["status"] == "success"
    assert second["already_reconciled"] is True
    assert second["route_projection"]["already_projected"] is True


def test_recovery_receipt_uses_one_stable_artifact_identity_across_versions(tmp_path: Path):
    state, attempt, _intent_id = _save_intent(tmp_path, "pbs")
    current = {"stdout": ""}

    def runner(_argv, **_kwargs):
        return _ok(current["stdout"])

    pending = recovery.reconcile_external_submission(
        state, attempt, query_runner=runner)
    pending_outer = state.read_artifact(pending["recovery_artifact_id"])
    assert pending_outer["version"] == 1

    current["stdout"] = f"""Job Id: 991.server
    Variable_List = AI4S_SUBMISSION_NONCE=ai4s:{attempt}
"""
    resolved = recovery.reconcile_external_submission(
        state, attempt, query_runner=runner)
    assert resolved["status"] == "success"
    assert resolved["recovery_artifact_id"] == pending["recovery_artifact_id"]
    resolved_outer = state.read_artifact(resolved["recovery_artifact_id"])
    assert resolved_outer["version"] == 2


def test_identical_pending_query_does_not_create_a_new_artifact_version(tmp_path: Path):
    state, attempt, _intent_id = _save_intent(tmp_path, "pbs")
    def runner(*_args, **_kwargs):
        return _ok("")
    first = recovery.reconcile_external_submission(
        state, attempt, query_runner=runner)
    second = recovery.reconcile_external_submission(
        state, attempt, query_runner=runner)
    assert second["recovery_artifact_id"] == first["recovery_artifact_id"]
    assert state.read_artifact(first["recovery_artifact_id"])["version"] == 1


def test_kubernetes_unique_query_requires_uid_namespace_and_context(tmp_path: Path):
    state, attempt, _intent_id = _save_intent(
        tmp_path, "kubernetes", namespace="science")
    intent = recovery._intent_for_attempt(state, attempt)

    def runner(argv, **_kwargs):
        if argv[1:3] == ["config", "current-context"]:
            return _ok("cluster-a\n")
        assert argv[1:3] == ["-n", "science"]
        return _ok(json.dumps({"items": [{"metadata": {
            "name": "recover-me", "uid": "uid-123", "namespace": "science",
            "labels": {"ai4s-harness/submission-nonce": attempt},
        }}]}))

    query = recovery._query_kubernetes(intent, runner)
    assert query["query_status"] == "unique"
    assert query["identities"][0]["resource_uid"] == "uid-123"
    assert query["identities"][0]["scheduler_cluster"] == "cluster-a"


def test_local_same_name_inspection_is_candidate_not_identity_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    state, attempt, _intent_id = _save_intent(
        tmp_path,
        "local",
        extra={
            "job_id": _CONTAINER_NAME,
            "sandbox_image_id": _IMAGE_ID,
        },
    )
    inspected_names: list[str] = []

    def inspect_container(name: str) -> dict:
        inspected_names.append(name)
        return _local_inspection()

    monkeypatch.setattr("core.sandbox.inspect_container", inspect_container)
    result = recovery.reconcile_external_submission(state, attempt)

    assert result["status"] == "reconciliation_pending"
    assert result["query_status"] == "query_error"
    assert result["reason"] == "local_immutable_identity_unprovable"
    assert result["candidate_count"] == 1
    assert result["do_not_resubmit"] is True
    assert inspected_names == [_CONTAINER_NAME]
    payload = json.loads(
        state.read_artifact(result["recovery_artifact_id"])["content"]
    )
    assert payload["status"] == "submission_outcome_unknown"
    assert payload["job_id"] is None
    assert not payload.get("container_runtime_id")
    assert payload["reconciliation"]["identities"] == [{
        "scheduler": "local",
        "job_id": _CONTAINER_NAME,
        "namespace": None,
        "launch_host": None,
        "scheduler_cluster": None,
        "resource_uid": None,
        "submission_nonce": attempt,
        "process_group_id": None,
        "process_start_ticks": None,
        "container_runtime_id": _RUNTIME_ID_A,
    }]
    assert not state.list_artifacts("external_job_workflow")


@pytest.mark.parametrize("runtime_id", [None, "A" * 64, "short"])
def test_local_missing_or_invalid_runtime_id_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runtime_id: str | None,
):
    state, attempt, _intent_id = _save_intent(
        tmp_path,
        "local",
        extra={
            "job_id": _CONTAINER_NAME,
            "sandbox_image_id": _IMAGE_ID,
        },
    )
    monkeypatch.setattr(
        "core.sandbox.inspect_container",
        lambda _name: _local_inspection(runtime_id),
    )

    result = recovery.reconcile_external_submission(state, attempt)

    assert result["status"] == "reconciliation_pending"
    assert result["query_status"] == "query_error"
    assert result["reason"] == "local_container_runtime_identity_invalid"
    assert result["do_not_resubmit"] is True
    payload = json.loads(
        state.read_artifact(result["recovery_artifact_id"])["content"]
    )
    assert payload["job_id"] is None
    assert not payload.get("container_runtime_id")


def test_known_local_receipt_matching_intent_is_reused_without_inspection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    state, attempt, _intent_id = _save_intent(
        tmp_path,
        "local",
        extra={
            "job_id": _CONTAINER_NAME,
            "sandbox_image_id": _IMAGE_ID,
        },
    )
    state.save_artifact(
        "job_submission",
        "job_submission_known_local_identity",
        json.dumps({
            "status": "success",
            "scheduler": "local",
            "job_id": _CONTAINER_NAME,
            "submission_nonce": attempt,
            "route_attempt_id": attempt,
            "container_runtime_id": _RUNTIME_ID_A,
        }),
    )
    monkeypatch.setattr(
        "core.sandbox.inspect_container",
        lambda _name: pytest.fail("known framework receipt must skip discovery"),
    )

    result = recovery.reconcile_external_submission(state, attempt)

    assert result["status"] == "success"
    assert result["already_reconciled"] is True
    assert result["resolved_identity"]["job_id"] == _CONTAINER_NAME
    assert result["resolved_identity"]["container_runtime_id"] == _RUNTIME_ID_A


@pytest.mark.parametrize(
    "override",
    [
        {"scheduler": "slurm"},
        {"job_id": "hf-different-container"},
        {"submission_nonce": "route-different-attempt"},
    ],
)
def test_known_local_receipt_must_match_intent_contract(
    tmp_path: Path, override: dict,
):
    state, attempt, _intent_id = _save_intent(
        tmp_path,
        "local",
        extra={
            "job_id": _CONTAINER_NAME,
            "sandbox_image_id": _IMAGE_ID,
        },
    )
    receipt = {
        "status": "success",
        "scheduler": "local",
        "job_id": _CONTAINER_NAME,
        "submission_nonce": attempt,
        "route_attempt_id": attempt,
        "container_runtime_id": _RUNTIME_ID_A,
        **override,
    }
    state.save_artifact(
        "job_submission", "job_submission_conflicting_local_identity",
        json.dumps(receipt),
    )

    result = recovery.reconcile_external_submission(state, attempt)

    assert result["status"] == "error"
    assert result["reason"] == "submission_recovery_ledger_invalid"
    assert result["do_not_resubmit"] is True


@pytest.mark.parametrize("runtime_id", [None, "A" * 64])
def test_known_local_receipt_without_valid_runtime_id_is_ledger_error(
    tmp_path: Path, runtime_id: str | None,
):
    state, attempt, _intent_id = _save_intent(
        tmp_path,
        "local",
        extra={
            "job_id": _CONTAINER_NAME,
            "sandbox_image_id": _IMAGE_ID,
        },
    )
    state.save_artifact(
        "job_submission",
        "job_submission_invalid_local_identity",
        json.dumps({
            "status": "success",
            "scheduler": "local",
            "job_id": _CONTAINER_NAME,
            "submission_nonce": attempt,
            "route_attempt_id": attempt,
            "container_runtime_id": runtime_id,
        }),
    )

    result = recovery.reconcile_external_submission(state, attempt)

    assert result["status"] == "error"
    assert result["reason"] == "submission_recovery_ledger_invalid"
    assert result["do_not_resubmit"] is True


def test_remote_legacy_known_receipt_keeps_optional_scope_compatibility(tmp_path: Path):
    state, attempt, _intent_id = _save_intent(tmp_path, "pbs")
    artifact = state.save_artifact(
        "job_submission",
        "job_submission_remote_legacy_identity",
        json.dumps({
            "status": "success",
            "scheduler": "pbs",
            "job_id": "731.server",
            "route_attempt_id": attempt,
        }),
    )
    intent = recovery._intent_for_attempt(state, attempt)

    known = recovery._known_receipt_for_attempt(state, attempt, intent)

    assert known is not None
    assert known["_artifact_id"] == artifact["id"]
    assert known["scheduler"] == "pbs"
    assert known["job_id"] == "731.server"
    assert not known.get("submission_nonce")
    assert not known.get("scheduler_cluster")


def test_dangling_intent_reserves_outputs_and_turn_start_surfaces_recovery(tmp_path: Path):
    state, attempt, intent_id = _save_intent(tmp_path, "pbs")
    state.project_root = tmp_path / "project"

    reservations = recovery.unresolved_submission_output_reservations(state)
    assert reservations[0]["route_attempt_id"] == attempt
    assert reservations[0]["output_roots"] == [str(tmp_path / "run" / "outputs")]

    messages = hooks.external_job_reconciliation_on_turn_start(
        SimpleNamespace(state=state))
    assert messages and attempt in str(messages[0].content)
    assert state.list_artifacts("external_job_identity_recovery_workflow")
    assert any(
        item.get("reported_by") == "framework:external_job_identity_unresolved"
        and intent_id in item.get("evidence_paths", [])
        for item in state.hook_state.get("blockers") or []
    )


def test_turn_start_deduplicates_same_snapshot_but_surfaces_new_intent(tmp_path: Path):
    state, attempt, intent_id = _save_intent(tmp_path, "pbs")
    state.project_root = tmp_path / "project"
    ctx = SimpleNamespace(state=state)
    assert hooks.external_job_reconciliation_on_turn_start(ctx)
    assert hooks.external_job_reconciliation_on_turn_start(ctx) is None

    outer = state.read_artifact(intent_id)
    payload = json.loads(outer["content"])
    second = "route-fedcba9876543210"
    payload.update({"route_attempt_id": second, "submission_nonce": second})
    state.save_artifact(
        "external_submission_intent",
        f"external_submission_intent_{state.run_id}_{second}",
        json.dumps(payload),
        metadata={"route_attempt_id": second, "submission_nonce": second,
                  "scheduler": "pbs"},
    )
    state.append_transcript(
        "route_step_bound", attempt_id=second, tool="submit_job",
        applied_policy="managed_external_job")
    state.append_transcript(
        "route_step_outcome", attempt_id=second, outcome="unknown",
        failure_class="external_identity_reconciliation_required")
    messages = hooks.external_job_reconciliation_on_turn_start(ctx)
    assert messages and second in str(messages[0].content)
    assert second != attempt


def test_turn_start_records_framework_blocker_for_corrupt_intent_ledger(tmp_path: Path):
    state, _attempt, intent_id = _save_intent(tmp_path, "pbs")
    # 正文就是盘上的文件：直接把它写坏。
    state.find_artifact_path(intent_id).write_text("{broken", encoding="utf-8")

    messages = hooks.external_job_reconciliation_on_turn_start(
        SimpleNamespace(state=state))
    rendered = str(messages[0].content) if messages else ""
    assert "账本损坏" in rendered
    assert "不得向重叠 output_roots/workdir 再次 submit_job" in rendered
    assert "提交前机械重探活" not in rendered
    blockers = [
        item for item in state.hook_state.get("blockers") or []
        if item.get("reported_by")
        == "framework:external_submission_recovery_ledger_invalid"
    ]
    assert len(blockers) == 1
    assert blockers[0]["suggested_owner"] == "framework"


def test_turn_start_explains_foreign_intent_without_ledger_repair_advice(
    tmp_path: Path,
):
    state, _attempt, _intent_id = _save_intent(
        tmp_path, "local", produced_by_run_id="another-run")

    messages = hooks.external_job_reconciliation_on_turn_start(
        SimpleNamespace(state=state))
    rendered = str(messages[0].content) if messages else ""
    assert "当前格式的死亡收养证据" in rendered
    assert "历史收养记录可能使用旧探活键" in rendered
    assert "本地作业隔离层" in rendered
    assert (
        "在新的 submission intent、route attempt 与作业 launch/外部提交前重新核验"
    ) in rendered
    assert "正常受管提交的副作用前重新核验" not in rendered
    assert "若原任务本来就要执行这次新提交" in rendered
    assert "新 launch 前机械重探活" in rendered
    assert "只有取得正面死亡证据后，该 foreign intent 才不再阻断" in rendered
    assert "其他 conflicts 仍独立检查" in rendered
    assert (
        "探活 unavailable/alive 时保持拒绝，不产生新的 submission intent、"
        "route attempt 或作业 launch/外部提交"
    ) in rendered
    assert "不要仅为探活调用 submit_job" in rendered
    assert "不得向重叠 output_roots/workdir 再次 submit_job" not in rendered
    assert "已经放行" not in rendered
    assert "账本损坏" not in rendered
    assert "修复账本" not in rendered
    blockers = [
        item for item in state.hook_state.get("blockers") or []
        if item.get("reported_by")
        == "framework:external_submission_recovery_ledger_invalid"
    ]
    assert len(blockers) == 1
    assert "账本损坏" not in blockers[0]["summary"]
    assert "本地作业隔离层" in blockers[0]["requested_action"]


@pytest.mark.parametrize("first_scheduler", ["local", "slurm"])
def test_turn_start_ledger_invalid_blocker_aggregates_mixed_rows_order_independently(
    tmp_path: Path, first_scheduler: str,
):
    second_scheduler = "slurm" if first_scheduler == "local" else "local"
    state, _attempt, first_id = _save_intent(
        tmp_path, first_scheduler, produced_by_run_id="foreign-run-a")
    _append_foreign_intent(
        state, first_id,
        scheduler=second_scheduler,
        attempt="route-mixed-fedcba987654",
        produced_by_run_id="foreign-run-b",
    )

    hooks.external_job_reconciliation_on_turn_start(SimpleNamespace(state=state))
    blockers = [
        item for item in state.hook_state.get("blockers") or []
        if item.get("reported_by")
        == "framework:external_submission_recovery_ledger_invalid"
    ]
    assert len(blockers) == 1
    assert "恢复条件不同" in blockers[0]["summary"]
    assert "只有来自其他 run 且 scheduler=local 的 intent" in blockers[0]["requested_action"]
    assert "其余 ledger_invalid 行仍禁止 submit_job" in blockers[0]["requested_action"]
    assert "不要仅为探活调用 submit_job" in blockers[0]["requested_action"]


def test_turn_start_refreshes_existing_ledger_invalid_blocker_when_rows_change(
    tmp_path: Path,
):
    state, _attempt, local_id = _save_intent(
        tmp_path, "local", produced_by_run_id="foreign-run-a")
    hooks.external_job_reconciliation_on_turn_start(SimpleNamespace(state=state))
    blockers = [
        item for item in state.hook_state.get("blockers") or []
        if item.get("reported_by")
        == "framework:external_submission_recovery_ledger_invalid"
    ]
    assert len(blockers) == 1
    blocker_id = blockers[0]["blocker_id"]
    assert "恢复条件不同" not in blockers[0]["summary"]

    _append_foreign_intent(
        state, local_id,
        scheduler="slurm",
        attempt="route-transition-fedcba98",
        produced_by_run_id="foreign-run-b",
    )
    hooks.external_job_reconciliation_on_turn_start(SimpleNamespace(state=state))
    blockers = [
        item for item in state.hook_state.get("blockers") or []
        if item.get("reported_by")
        == "framework:external_submission_recovery_ledger_invalid"
    ]
    assert len(blockers) == 1
    assert blockers[0]["blocker_id"] == blocker_id
    assert "恢复条件不同" in blockers[0]["summary"]
    assert "其余 ledger_invalid 行仍禁止 submit_job" in blockers[0]["requested_action"]


def test_turn_start_resolves_only_ledger_invalid_blocker_after_valid_adoption(
    tmp_path: Path,
):
    from core.blockers import record_blocker

    state, _attempt, intent_id = _save_intent(
        tmp_path, "local", produced_by_run_id="foreign-run-a")
    hooks.external_job_reconciliation_on_turn_start(SimpleNamespace(state=state))
    record_blocker(
        state,
        summary="unrelated blocker",
        requested_action="leave this blocker alone",
        reported_by="framework:unrelated_test_blocker",
    )
    assert any(
        item.get("reported_by")
        == "framework:external_submission_recovery_ledger_invalid"
        for item in state.hook_state.get("blockers") or []
    )

    state.save_artifact(
        "external_submission_intent_adoption",
        f"external_submission_intent_adoption_{state.run_id}",
        json.dumps({
            "classification": "unknown_dead",
            "adopted_intent_artifact_id": intent_id,
            "checked_at": "2026-09-17T00:00:00+00:00",
            "liveness_probe": {
                "liveness_probe_available": True,
                "inspect": {"exists": False},
            },
        }),
    )
    assert recovery.dangling_external_submission_intents(state) == []

    assert hooks.external_job_reconciliation_on_turn_start(
        SimpleNamespace(state=state)) is None
    remaining = state.hook_state.get("blockers") or []
    assert not any(
        item.get("reported_by")
        == "framework:external_submission_recovery_ledger_invalid"
        for item in remaining
    )
    assert any(
        item.get("reported_by") == "framework:unrelated_test_blocker"
        for item in remaining
    )
    transcript = state.transcript_path.read_text(encoding="utf-8")
    assert '"event": "blocker_resolved"' in transcript
    assert '"reported_by": "framework:external_submission_recovery_ledger_invalid"' in transcript


def test_turn_start_keeps_nonlocal_foreign_intent_on_no_resubmit_path(
    tmp_path: Path,
):
    state, _attempt, _intent_id = _save_intent(
        tmp_path, "slurm", produced_by_run_id="another-run")

    messages = hooks.external_job_reconciliation_on_turn_start(
        SimpleNamespace(state=state))
    rendered = str(messages[0].content) if messages else ""
    assert "非 local intent 没有本地作业隔离层收养通道" in rendered
    assert "不得向重叠 output_roots/workdir 再次 submit_job" in rendered
    assert "提交前机械重探活" not in rendered


def test_unique_reconciliation_creates_normal_handoff_then_closes_only_recovery(
    tmp_path: Path,
):
    from core.tasks import TaskList

    state, attempt, intent_id = _save_intent(tmp_path, "pbs")
    state.project_root = tmp_path / "project"
    hooks.external_job_reconciliation_on_turn_start(SimpleNamespace(state=state))

    def runner(_argv, **_kwargs):
        return _ok(f"""Job Id: 991.server
    Variable_List = AI4S_SUBMISSION_NONCE=ai4s:{attempt}
""")

    result = recovery.reconcile_external_submission(
        state, attempt, query_runner=runner)
    assert result["status"] == "success"
    assert result["normal_workflow"]["workflow_artifact_id"]
    assert state.list_artifacts("external_job_workflow")
    tasks = TaskList(state.project_root / "tasks").list_all()
    recovery_tasks = [
        item for item in tasks
        if "external_job_identity_recovery_key=" in item.description
    ]
    normal_tasks = [item for item in tasks if "external_job_key=" in item.description]
    assert [item.status for item in recovery_tasks] == ["completed"]
    assert [item.status for item in normal_tasks] == ["pending"]
    assert not any(
        item.get("reported_by") == "framework:external_job_identity_unresolved"
        and intent_id in item.get("evidence_paths", [])
        for item in state.hook_state.get("blockers") or []
    )
    assert "external_job_identity_recovery" not in state.hook_state
    transcript = state.transcript_path.read_text(encoding="utf-8")
    assert '"event": "blocker_resolved"' in transcript


def test_task_completion_failure_keeps_identity_blocker(tmp_path: Path, monkeypatch):
    from core.tasks import TaskList

    state, attempt, intent_id = _save_intent(tmp_path, "pbs")
    state.project_root = tmp_path / "project"
    hooks.external_job_reconciliation_on_turn_start(SimpleNamespace(state=state))
    monkeypatch.setattr(
        TaskList, "complete",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("task ledger down")),
    )
    def runner(*_args, **_kwargs):
        return _ok(f"""Job Id: 991.server
    Variable_List = AI4S_SUBMISSION_NONCE=ai4s:{attempt}
""")
    result = recovery.reconcile_external_submission(
        state, attempt, query_runner=runner)
    assert result["status"] == "integration_pending"
    assert result["reason"] == "recovery_task_completion_failed"
    assert any(
        item.get("reported_by") == "framework:external_job_identity_unresolved"
        and intent_id in item.get("evidence_paths", [])
        for item in state.hook_state.get("blockers") or []
    )


def test_foreign_or_relabelled_intent_is_rejected(tmp_path: Path):
    state, attempt, _intent_id = _save_intent(
        tmp_path, "pbs", produced_by_run_id="another-run")
    result = recovery.reconcile_external_submission(
        state, attempt, query_runner=lambda *_args, **_kwargs: _ok(""))
    assert result["status"] == "error"
    assert result["reason"] == "submission_recovery_ledger_invalid"
    assert result["do_not_resubmit"] is True


def test_unknown_output_conflict_becomes_known_terminal_after_unique_reconcile(
    tmp_path: Path, monkeypatch,
):
    from nodes.experiment.tools import resource_manager as rm

    state, attempt, _intent_id = _save_intent(tmp_path, "pbs")
    output = str(tmp_path / "run" / "outputs")
    conflicts = rm._active_output_conflicts(state, [output])
    assert any(item["kind"] == "external_submission_identity_unknown"
               for item in conflicts)

    def runner(*_args, **_kwargs):
        return _ok(f"""Job Id: 991.server
    Variable_List = AI4S_SUBMISSION_NONCE=ai4s:{attempt}
""")
    assert recovery.reconcile_external_submission(
        state, attempt, query_runner=runner)["status"] == "success"
    monkeypatch.setattr(rm, "_external_job_is_active", lambda *_args, **_kwargs: False)
    assert not any(
        item.get("kind") == "external_submission_identity_unknown"
        for item in rm._active_output_conflicts(state, [output])
    )


def test_unknown_recovery_receipt_is_valid_but_blocks_open_job_projection(tmp_path: Path):
    state, attempt, _intent_id = _save_intent(tmp_path, "pbs")
    pending = recovery.reconcile_external_submission(
        state, attempt, query_runner=lambda *_args, **_kwargs: _ok(""))
    receipts = hooks._read_submission_records_strict(state)
    assert any(item["status"] == "submission_outcome_unknown" for item in receipts)
    with pytest.raises(hooks.SubmissionLedgerError, match="remains unresolved"):
        hooks._job_submission_records(state)
    assert pending["do_not_resubmit"] is True


def _call_submit_sync(
    manager, state: State, attempt: str, scheduler: str,
):
    runtime_root = manager.experiment_output_dir(
        state, "runtime", create=True,
    )
    return manager._submit_sync(
        runtime_root=runtime_root,
        scheduler=scheduler,
        command="echo hello",
        job_name="intent-terminal-test",
        mpi_ranks=1,
        cpus_per_rank=1,
        gpus=0,
        memory_gb=1.0,
        storage_gb=1.0,
        walltime_minutes=1,
        queue=None,
        nodelist=None,
        image=None,
        workdir=str(runtime_root),
        dry_run=False,
        namespace=None,
        stage_in=None,
        state=state,
        submission_nonce=attempt,
        route_attempt_id=attempt,
    )


def test_explicit_scheduler_rejection_seals_intent_and_releases_outputs(
    tmp_path, monkeypatch,
):
    from nodes.experiment.tools import resource_manager as manager

    state = State.new("experiment", tmp_path / "runs")
    attempt = "route-explicit-rejection"
    monkeypatch.setattr(
        manager,
        "_run",
        lambda *_args, **_kwargs: {
            "ok": False, "returncode": 1, "stdout": "",
            "stderr": "submission rejected",
        },
    )

    result = _call_submit_sync(manager, state, attempt, "slurm")

    assert result["status"] == "error"
    assert result["intent_status"] == "rejected_by_scheduler"
    assert result["submission_boundary_crossed"] is True
    assert result["output_roots_released"] is True
    artifacts = state.list_artifacts("external_submission_intent")
    assert len(artifacts) == 1
    head = state.read_artifact(artifacts[0]["id"])
    assert head["version"] == 2
    payload = json.loads(head["content"])
    assert payload["scheduler_acceptance"] is False
    assert recovery.dangling_external_submission_intents(state) == []
    queried = []
    reconciled = recovery.reconcile_external_submission(
        state, attempt, query_runner=lambda *_a, **_kw: queried.append(1),
    )
    assert reconciled["status"] == "already_terminal"
    assert reconciled["reason"] == "submission_rejected_by_scheduler"
    assert queried == []


def test_scheduler_rejection_terminal_write_failure_stays_reserved(
    tmp_path, monkeypatch,
):
    from nodes.experiment.tools import resource_manager as manager

    state = State.new("experiment", tmp_path / "runs")
    attempt = "route-rejection-ledger-failure"
    original_save = state.save_artifact
    saves = 0

    def fail_terminal_save(*args, **kwargs):
        nonlocal saves
        saves += 1
        if saves == 2:
            raise OSError("terminal ledger unavailable")
        return original_save(*args, **kwargs)

    monkeypatch.setattr(state, "save_artifact", fail_terminal_save)
    monkeypatch.setattr(
        manager,
        "_run",
        lambda *_args, **_kwargs: {
            "ok": False, "returncode": 1, "stdout": "", "stderr": "rejected",
        },
    )

    result = _call_submit_sync(manager, state, attempt, "slurm")

    assert result["status"] == "submission_outcome_unknown"
    assert result["do_not_resubmit"] is True
    dangling = recovery.dangling_external_submission_intents(state)
    assert len(dangling) == 1
    assert dangling[0]["route_attempt_id"] == attempt


def test_local_docker_runtime_not_found_is_sealed_before_submit(
    tmp_path, monkeypatch,
):
    from core import sandbox
    from nodes.experiment.tools import resource_manager as manager

    state = State.new("experiment", tmp_path / "runs")
    attempt = "route-local-docker-not-found"
    runtime_root = manager.experiment_output_dir(
        state, "runtime", create=True,
    ).resolve()
    cleanup_calls: list[str] = []
    launch = SimpleNamespace(
        argv=["docker", "run", "--name", "hf-test-local-not-found"],
        container_name="hf-test-local-not-found",
        control_dir=runtime_root / ".sandbox-control",
        image_id="sha256:" + "0" * 64,
        cleanup=lambda: cleanup_calls.append("cleanup"),
    )
    prepare_calls: list[tuple[list[str], dict]] = []
    run_calls: list[tuple[list[str], int]] = []

    def fake_prepare_launch(payload_argv, **kwargs):
        prepare_calls.append((list(payload_argv), dict(kwargs)))
        return launch

    def runtime_not_found(argv, *, timeout):
        run_calls.append((list(argv), timeout))
        return {
            "ok": False, "returncode": None, "stdout": "",
            "stderr": "not found",
        }

    monkeypatch.setattr(sandbox, "prepare_launch", fake_prepare_launch)
    monkeypatch.setattr(manager, "_run", runtime_not_found)

    result = _call_submit_sync(manager, state, attempt, "local")

    assert result["status"] == "error"
    assert result["intent_status"] == "aborted_before_submit"
    assert prepare_calls
    payload_argv, launch_kwargs = prepare_calls[0]
    assert payload_argv[0] == "/bin/bash"
    assert Path(payload_argv[1]).is_relative_to(runtime_root)
    assert Path(launch_kwargs["cwd"]) == runtime_root
    assert launch_kwargs["detached"] is True
    assert run_calls == [(launch.argv, 30)]
    assert cleanup_calls == ["cleanup"]

    artifacts = state.list_artifacts("external_submission_intent")
    assert len(artifacts) == 1
    head = state.read_artifact(artifacts[0]["id"])
    assert head["version"] == 2
    intent = json.loads(head["content"])
    assert intent["intent_status"] == "aborted_before_submit"
    assert intent["submission_boundary_crossed"] is False
    assert intent["terminal_reason"] == "docker_runtime_rejected_launch"
    assert recovery.dangling_external_submission_intents(state) == []

    reconciled = recovery.reconcile_external_submission(state, attempt)
    assert reconciled["status"] == "already_terminal"
    assert reconciled["reason"] == "submission_aborted_before_submit"
    assert reconciled["submission_boundary_crossed"] is False
    assert reconciled["output_roots_released"] is True


def _save_foreign_workflow_row(state: State, payload: dict) -> str:
    """写一条归属别的（已死）run 的 external_job_workflow 账本行。"""
    artifact = state.save_artifact(
        "external_job_workflow",
        f"external_job_workflow_dead-run_{payload.get('job_id')}",
        json.dumps(payload),
        metadata={"submission_nonce": payload.get("submission_nonce")},
        provenance=produced("experiment", "dead-run"),
    )
    return str(artifact["id"])


def test_matching_normal_workflow_skips_foreign_run_rows(tmp_path: Path):
    # 跨 run workflow 行是账本事实而非读取错误：按归属跳过不匹配（即便身份
    # 字段完全相同也不认领），本 run 自己的行判定不变。
    state = State.new("experiment", tmp_path / "runs")
    attempt = "route-0123456789abcdef"
    identity = {"submission_nonce": attempt, "scheduler": "pbs",
                "job_id": "991.server"}
    _save_foreign_workflow_row(state, identity)

    assert recovery._matching_normal_workflow(state, identity) is None

    own = state.save_artifact(
        "external_job_workflow",
        f"external_job_workflow_{state.run_id}_991",
        json.dumps(identity),
        metadata={"submission_nonce": attempt},
    )
    match = recovery._matching_normal_workflow(state, identity)
    assert match is not None
    assert match["_artifact_id"] == own["id"]


def test_reconcile_succeeds_despite_foreign_run_workflow_row(tmp_path: Path):
    # 上一 run 提交成功后被杀留下的 workflow 行不再把本 run 自己的 reconcile
    # 炸成 external_workflow_ledger_invalid；本 run 的 workflow 正常持久化。
    state, attempt, _intent_id = _save_intent(tmp_path, "pbs")
    state.project_root = tmp_path / "project"
    hooks.external_job_reconciliation_on_turn_start(SimpleNamespace(state=state))
    foreign_id = _save_foreign_workflow_row(state, {
        "submission_nonce": "route-deadrun0123456789ab",
        "scheduler": "pbs",
        "job_id": "731.server",
    })

    def runner(_argv, **_kwargs):
        return _ok(f"""Job Id: 991.server
    Variable_List = AI4S_SUBMISSION_NONCE=ai4s:{attempt}
""")

    result = recovery.reconcile_external_submission(
        state, attempt, query_runner=runner)
    assert result["status"] == "success"
    own_workflow_id = result["normal_workflow"]["workflow_artifact_id"]
    assert own_workflow_id
    assert own_workflow_id != foreign_id

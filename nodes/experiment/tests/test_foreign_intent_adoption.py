"""跨 run 残留 intent 的死亡证明收养通道（N-3）。

上一 run 在 submit 在飞窗口被杀时，其 prepared intent 停在非终态；它对同
project 的后继 run 可见但不归属后继 run，此前会把整个账本判成
ledger_invalid，submit/reconcile/finalize 三个出口同时锁死。收养通道只在拿到
正面死亡证据（本地作业隔离层先被证明可用 + 容器确证不存在/已退出）时放行并
写下不可变收养记录；活容器、隔离层不可用、非 local 调度器一律保持拦截
（fail-closed，inspect 的 exists=False 本身分不清“容器没了”和“隔离层不可用”）。
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from core import sandbox
from core.artifact_provenance import produced
from core.ledger import RecordStore
from core.state import State
from nodes.experiment.tools import external_submission_recovery as recovery
from nodes.experiment.tools import resource_manager as rm

_ATTEMPT = "route-foreign-0123456789abcdef"
_CONTAINER = "hf-test-dead-run-job"
_ADOPTION_TYPE = "external_submission_intent_adoption"


def _foreign_prepared_intent(
    tmp_path: Path, *, scheduler: str = "local",
) -> tuple[State, str, str]:
    state = State.new("experiment", tmp_path / "runs")
    output_root = str(tmp_path / "run" / "outputs")
    payload = {
        "status": "success",
        "intent_status": "prepared",
        "scheduler": scheduler,
        "dry_run": False,
        "job_name": "orphaned-by-kill",
        "submitted_at": "2026-08-29T01:02:03+00:00",
        "submission_nonce": _ATTEMPT,
        "route_attempt_id": _ATTEMPT,
        "script_sha256": "a" * 64,
        "command_sha256": "b" * 64,
        "script_path": str(tmp_path / "job.sh"),
        "workdir": str(tmp_path / "run"),
        "output_roots": [output_root],
        "namespace": None,
        "job_id": _CONTAINER,
    }
    artifact = state.save_artifact(
        "external_submission_intent",
        f"external_submission_intent_dead-run_{_ATTEMPT}",
        json.dumps(payload),
        metadata={"submission_nonce": _ATTEMPT, "scheduler": scheduler,
                  "route_attempt_id": _ATTEMPT},
        # 账本把 intent 记成上一个（已被杀死的）run 的产物：跨 run 残留。
        provenance=produced("experiment", "dead-run"),
    )
    return state, str(artifact["id"]), output_root


def test_dead_container_releases_outputs_and_records_adoption(
    tmp_path: Path, monkeypatch,
):
    state, intent_id, output_root = _foreign_prepared_intent(tmp_path)
    monkeypatch.setattr(
        sandbox, "availability", lambda **_: (True, "native isolation baseline ok"))
    inspected: list[str] = []

    def fake_inspect(name):
        inspected.append(name)
        return {"exists": False}

    monkeypatch.setattr(sandbox, "inspect_container", fake_inspect)

    assert rm._active_output_conflicts(state, [output_root]) == []
    assert inspected == [_CONTAINER]

    adoptions = state.list_artifacts(_ADOPTION_TYPE)
    assert len(adoptions) == 1
    record = json.loads(state.read_artifact(adoptions[0]["id"])["content"])
    assert record["classification"] == "unknown_dead"
    assert record["adopted_intent_artifact_id"] == intent_id
    assert record["intent_produced_by_run_id"] == "dead-run"
    assert record["adopted_by_run_id"] == state.run_id
    assert record["checked_at"]
    assert record["liveness_probe"]["liveness_probe_available"] is True
    assert "docker_available" not in record["liveness_probe"]
    assert record["liveness_probe"]["container_ref"] == _CONTAINER
    assert record["liveness_probe"]["inspect"]["exists"] is False
    assert record["output_roots_released"] == [output_root]
    transcript = state.transcript_path.read_text(encoding="utf-8")
    assert '"event": "external_submission_intent_adopted"' in transcript

    # 收养后 ledger 读取把该 intent 视为终态：不再 dangling，
    # 后续冲突检查直接放行且不重复探活、不写第二条收养记录。
    assert recovery.dangling_external_submission_intents(state) == []
    inspected.clear()
    assert rm._active_output_conflicts(state, [output_root]) == []
    assert inspected == []
    assert len(state.list_artifacts(_ADOPTION_TYPE)) == 1


def test_exited_container_counts_as_dead(tmp_path: Path, monkeypatch):
    state, _intent_id, output_root = _foreign_prepared_intent(tmp_path)
    monkeypatch.setattr(sandbox, "availability", lambda **_: (True, "ok"))
    monkeypatch.setattr(
        sandbox, "inspect_container",
        lambda name: {"exists": True, "running": False,
                      "status": "exited", "exit_code": 137})
    assert rm._active_output_conflicts(state, [output_root]) == []
    adoptions = state.list_artifacts(_ADOPTION_TYPE)
    assert len(adoptions) == 1
    record = json.loads(state.read_artifact(adoptions[0]["id"])["content"])
    assert record["liveness_probe"]["inspect"]["status"] == "exited"


def test_live_container_keeps_blocking_without_adoption(
    tmp_path: Path, monkeypatch,
):
    state, intent_id, output_root = _foreign_prepared_intent(tmp_path)
    monkeypatch.setattr(sandbox, "availability", lambda **_: (True, "ok"))
    monkeypatch.setattr(
        sandbox, "inspect_container",
        lambda name: {"exists": True, "running": True, "status": "running"})

    conflicts = rm._active_output_conflicts(state, [output_root])
    assert len(conflicts) == 1
    conflict = conflicts[0]
    assert conflict["kind"] == "external_submission_identity_unknown"
    assert conflict["query_status"] == "ledger_invalid"
    assert conflict["intent_artifact_id"] == intent_id
    assert conflict["do_not_resubmit"] is True
    assert "container_alive" in conflict["reason"]
    # 活 intent 的拦截语义不变：不重叠的输出根同样保持拦截。
    assert rm._active_output_conflicts(state, [str(tmp_path / "elsewhere")])
    assert state.list_artifacts(_ADOPTION_TYPE) == []


def test_liveness_probe_unavailable_keeps_blocking_and_states_probe_unavailable(
    tmp_path: Path, monkeypatch,
):
    state, _intent_id, output_root = _foreign_prepared_intent(tmp_path)
    monkeypatch.setattr(
        sandbox, "availability",
        lambda **_: (False, "native isolation backend unreachable"))

    def must_not_trust_inspect(name):
        raise AssertionError("本地作业隔离层不可用时不得依赖 inspect 结果")

    monkeypatch.setattr(sandbox, "inspect_container", must_not_trust_inspect)

    conflicts = rm._active_output_conflicts(state, [output_root])
    assert len(conflicts) == 1
    assert conflicts[0]["kind"] == "external_submission_identity_unknown"
    assert "liveness_probe_unavailable" in conflicts[0]["reason"]
    assert "本地作业隔离层不可用" in conflicts[0]["reason"]
    assert "native isolation backend unreachable" in conflicts[0]["reason"]
    assert state.list_artifacts(_ADOPTION_TYPE) == []
    rows = recovery.dangling_external_submission_intents(state)
    assert len(rows) == 1
    assert rows[0]["foreign_run"] is True
    assert rows[0]["status"] == "ledger_invalid"


def test_foreign_nonlocal_intent_is_not_adoptable_by_container_probe(
    tmp_path: Path, monkeypatch,
):
    state, _intent_id, output_root = _foreign_prepared_intent(
        tmp_path, scheduler="slurm")

    def must_not_probe(*_args, **_kwargs):
        raise AssertionError("非 local intent 不得调用本地作业隔离层探活")

    monkeypatch.setattr(sandbox, "availability", must_not_probe)
    monkeypatch.setattr(sandbox, "inspect_container", must_not_probe)

    conflicts = rm._active_output_conflicts(state, [output_root])
    assert len(conflicts) == 1
    assert "non_local_scheduler" in conflicts[0]["reason"]
    assert state.list_artifacts(_ADOPTION_TYPE) == []


def test_inspect_error_is_not_death_evidence(tmp_path: Path, monkeypatch):
    # F-5：docker_not_found/超时/OSError 时 inspect 也返回 exists=False，
    # 但带 error 字段——那是探活失败，不是正面死亡证据。
    state, _intent_id, output_root = _foreign_prepared_intent(tmp_path)
    monkeypatch.setattr(sandbox, "availability", lambda **_: (True, "ok"))
    monkeypatch.setattr(
        sandbox, "inspect_container",
        lambda name: {"exists": False, "error": "docker_not_found"})

    conflicts = rm._active_output_conflicts(state, [output_root])
    assert len(conflicts) == 1
    assert "liveness_probe_unavailable" in conflicts[0]["reason"]
    assert "docker_not_found" in conflicts[0]["reason"]
    assert state.list_artifacts(_ADOPTION_TYPE) == []


def test_death_certificate_requires_fresh_availability_probe(
    tmp_path: Path, monkeypatch,
):
    # availability 的进程级缓存不构成探活时刻的证明：必须 refresh=True，
    # 让“本地作业隔离层可用”与 inspect 结果出自同一时刻。
    state, _intent_id, output_root = _foreign_prepared_intent(tmp_path)
    refresh_calls: list[bool] = []

    def fake_availability(*, refresh: bool = False):
        refresh_calls.append(refresh)
        return (True, "ok")

    monkeypatch.setattr(sandbox, "availability", fake_availability)
    monkeypatch.setattr(sandbox, "inspect_container", lambda name: {"exists": False})

    assert rm._active_output_conflicts(state, [output_root]) == []
    assert refresh_calls == [True]


def test_adoption_record_without_inspect_evidence_is_not_trusted(
    tmp_path: Path, monkeypatch,
):
    # fail-closed：收养记录必须留有正面的容器死亡证据（inspect 的
    # exists/status）；证据整体缺失的记录不算数，intent 继续保留输出根。
    state, _intent_id, output_root = _foreign_prepared_intent(tmp_path)
    monkeypatch.setattr(sandbox, "availability", lambda **_: (True, "ok"))
    monkeypatch.setattr(sandbox, "inspect_container", lambda name: {"exists": False})
    assert rm._active_output_conflicts(state, [output_root]) == []

    adoption = state.list_artifacts(_ADOPTION_TYPE)[0]
    outer = state.read_artifact(adoption["id"])
    payload = json.loads(outer["content"])
    payload["liveness_probe"].pop("inspect")
    # 同一身份再落一版（run 本地账本）：记录本身合法，只是探活证据缺失。
    RecordStore(state.root / "artifacts", state.root / "records.jsonl").save(
        artifact_id=adoption["id"], artifact_type=_ADOPTION_TYPE, name=outer["name"],
        content=json.dumps(payload), metadata=dict(outer["metadata"]),
        directory=state.root / "artifacts", created_at=outer["created_at"],
        provenance=dict(outer["provenance"]),
        produced_by_node_type=outer["produced_by_node_type"],
        produced_by_run_id=outer["produced_by_run_id"],
        by_node="experiment", by_run=state.run_id,
    )

    assert recovery.adopted_intent_artifact_ids(state) == set()
    rows = recovery.dangling_external_submission_intents(state)
    assert len(rows) == 1
    assert rows[0]["foreign_run"] is True


def test_legacy_docker_available_adoption_record_is_not_trusted(tmp_path: Path):
    """直接改名只面向新 run；旧键没有隐式读取兜底。"""
    state, intent_id, _output_root = _foreign_prepared_intent(tmp_path)
    state.save_artifact(
        _ADOPTION_TYPE,
        f"{_ADOPTION_TYPE}_{state.run_id}_{_ATTEMPT}",
        json.dumps({
            "classification": "unknown_dead",
            "adopted_intent_artifact_id": intent_id,
            "checked_at": "2026-08-30T00:00:00+00:00",
            "liveness_probe": {
                "docker_available": True,
                "container_ref": _CONTAINER,
                "inspect": {"exists": False},
            },
        }),
    )

    assert recovery.adopted_intent_artifact_ids(state) == set()
    rows = recovery.dangling_external_submission_intents(state)
    assert len(rows) == 1
    assert rows[0]["foreign_run"] is True
    assert rows[0]["status"] == "ledger_invalid"


_OWN_ATTEMPT = "route-own-0123456789abcdef"


def test_adopted_dead_intent_does_not_lock_own_reconcile(
    tmp_path: Path, monkeypatch,
):
    # N-3 的 reconcile 出口：收养后 ledger 读取把死 intent 视为终态，
    # 当前 run 自己的中断 attempt 仍可走完恢复；死 intent 本体仍不归本 run。
    state, _intent_id, output_root = _foreign_prepared_intent(tmp_path)
    monkeypatch.setattr(sandbox, "availability", lambda **_: (True, "ok"))
    monkeypatch.setattr(sandbox, "inspect_container", lambda name: {"exists": False})
    assert rm._active_output_conflicts(state, [output_root]) == []
    assert len(state.list_artifacts(_ADOPTION_TYPE)) == 1

    own_payload = {
        "status": "success",
        "intent_status": "prepared",
        "scheduler": "slurm",
        "dry_run": False,
        "job_name": "interrupted-own-submit",
        "submitted_at": "2026-08-30T01:02:03+00:00",
        "submission_nonce": _OWN_ATTEMPT,
        "route_attempt_id": _OWN_ATTEMPT,
        "script_sha256": "a" * 64,
        "command_sha256": "b" * 64,
        "script_path": str(tmp_path / "own_job.sh"),
        "workdir": str(tmp_path / "own_run"),
        "output_roots": [str(tmp_path / "own_run" / "outputs")],
        "namespace": None,
    }
    state.save_artifact(
        "external_submission_intent",
        f"external_submission_intent_{state.run_id}_{_OWN_ATTEMPT}",
        json.dumps(own_payload),
        metadata={"submission_nonce": _OWN_ATTEMPT, "scheduler": "slurm",
                  "route_attempt_id": _OWN_ATTEMPT},
    )
    state.append_transcript(
        "route_step_bound", attempt_id=_OWN_ATTEMPT, tool="submit_job",
        applied_policy="managed_external_job")
    state.append_transcript(
        "route_step_outcome", attempt_id=_OWN_ATTEMPT, outcome="unknown",
        failure_class="external_identity_reconciliation_required")

    def runner(argv, **_kwargs):
        if argv[:3] == ["scontrol", "show", "config"]:
            return {"ok": True, "returncode": 0,
                    "stdout": "ClusterName = alpha\n", "stderr": ""}
        if argv[0] == "squeue":
            return {"ok": True, "returncode": 0,
                    "stdout": f"731|ai4s:{_OWN_ATTEMPT}\n", "stderr": ""}
        if argv[0] == "sacct":
            return {"ok": True, "returncode": 0,
                    "stdout": f"731|ai4s:{_OWN_ATTEMPT}|alpha\n", "stderr": ""}
        raise AssertionError(argv)

    result = recovery.reconcile_external_submission(
        state, _OWN_ATTEMPT, query_runner=runner)
    assert result["status"] == "success"
    assert result["resolved_identity"]["job_id"] == "731"

    # 死 intent 本体仍不归本 run 所有：直接 reconcile 它保持非属主拒绝。
    foreign = recovery.reconcile_external_submission(state, _ATTEMPT)
    assert foreign["status"] == "error"
    assert foreign["reason"] == "submission_recovery_ledger_invalid"
    assert "不是当前 Experiment run 的产物" in foreign["error"]


def test_generic_save_cannot_forge_adoption_record(tmp_path: Path, monkeypatch):
    # N-3 补充：收养记录纳入防伪写入门。被活跨 run intent 拦住的 agent 不得
    # 用通用 save_artifact 铸造 unknown_dead 死亡证明零探活释放输出根；合法
    # 收养走 Python 层探活通道（state.save_artifact 不经过 save gate）。
    from shared.tools.builtin import _save_artifact

    state, intent_id, output_root = _foreign_prepared_intent(tmp_path)
    forged = asyncio.run(_save_artifact(
        state,
        artifact_type=_ADOPTION_TYPE,
        name=f"{_ADOPTION_TYPE}_{state.run_id}_{_ATTEMPT}",
        content=json.dumps({
            "classification": "unknown_dead",
            "adopted_intent_artifact_id": intent_id,
            "checked_at": "2026-08-30T00:00:00+00:00",
            "liveness_probe": {"liveness_probe_available": True,
                               "container_ref": _CONTAINER,
                               "inspect": {"exists": False}},
        }),
    ))
    assert forged["status"] == "error"
    assert forged["failed_checks"] == ["managed_external_artifact_owner"]
    assert state.list_artifacts(_ADOPTION_TYPE) == []
    # 伪造被拒后 intent 继续保留输出根（隔离层不可用 → 探活通道也不放行）。
    monkeypatch.setattr(
        sandbox, "availability", lambda **_: (False, "native isolation unavailable"))
    assert rm._active_output_conflicts(state, [output_root])

    # 同一状态下，真实探活通道（Python 层）拿到正面死亡证据仍能写入收养记录。
    monkeypatch.setattr(sandbox, "availability", lambda **_: (True, "ok"))
    monkeypatch.setattr(sandbox, "inspect_container", lambda name: {"exists": False})
    assert rm._active_output_conflicts(state, [output_root]) == []
    assert len(state.list_artifacts(_ADOPTION_TYPE)) == 1

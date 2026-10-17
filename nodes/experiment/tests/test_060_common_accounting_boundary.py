"""060 S1a：所有有副作用的写入者都经过同一个记账边界（governing §5）。

不只是 safe_run_bash 与 submit_job：写文件、取资源、artifact 写入都要留下 effect，
否则 finish 的 provenance 有洞。提交必须"先持久化意图与 nonce、再提交"。
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from core.loop_hooks import HookContext
from core.state import State
from nodes.experiment.tools import ledger, ledger_hooks, obligations
from nodes.experiment.tools import resource_manager as rm


def _state(tmp_path: Path) -> State:
    state = State.new(node_type="experiment", base_dir=tmp_path / "runs", project_id="p", session_id="owner")
    state.hook_state["node_inputs"] = {"requested_work": "accounting boundary"}
    ledger.open_ledger(state, write=True, bootstrap=True)
    ledger.mark_bootstrap_ready(state)
    return state


class _Ctx:
    def __init__(self, state, records, turn=1):
        self.state = state
        self.tool_call_records = records
        self.turn = turn
        self.messages = []
        self.harness = None


def test_the_recorder_hook_covers_every_effectful_tool_not_just_bash_and_submit(tmp_path):
    state = _state(tmp_path)
    records = [
        {"name": "safe_write_file", "args": {"path": "/w/a.txt"}, "result": {"status": "success"}},
        {"name": "fetch_resource", "args": {"url": "https://x/y.tar", "kind": "file"},
         "result": {"status": "success", "destination": "/w/y.tar", "sha256": "ab", "size_bytes": 10}},
        {"name": "save_artifact", "args": {"artifact_type": "raw_results", "name": "r"},
         "result": {"status": "success", "id": "raw_results__r"}},
        {"name": "freeze_artifact", "args": {"artifact_id": "raw_results__r"}, "result": {"status": "success"}},
        {"name": "read_file", "args": {"path": "/w/a.txt"}, "result": {"status": "success"}},  # 只读：不记
    ]
    assert ledger_hooks.record_turn_effects(_Ctx(state, records)) == 4
    effects = [r for r in ledger.open_ledger(state).records() if r["kind"] == "effect"]
    assert [e["tool"] for e in effects] == ["safe_write_file", "fetch_resource", "save_artifact", "freeze_artifact"]
    assert {e["effect_class"] for e in effects} == {"support"}
    assert effects[1]["result"]["sha256"] == "ab"  # 结果摘要带身份，供 finish 归属


def _submit(state, tmp_path, *, dry_run=False):
    return rm._submit_sync(
        runtime_root=tmp_path / "rt", scheduler="slurm", command="echo hi", job_name="hello",
        mpi_ranks=1, cpus_per_rank=1, gpus=0, memory_gb=1.0, storage_gb=1.0, walltime_minutes=5,
        queue=None, nodelist=None, image=None, workdir=str(tmp_path / "w"), dry_run=dry_run,
        namespace=None, output_dir=str(tmp_path / "w"), stage_in=None, state=state)


def test_submission_persists_intent_and_nonce_before_the_scheduler_is_called(tmp_path, monkeypatch):
    state = _state(tmp_path)
    (tmp_path / "w").mkdir(parents=True, exist_ok=True)
    seen: dict[str, object] = {}

    def fake_run(args, timeout=10):
        # 调度器被调用的那一刻，意图必须已经在账本里
        seen["records"] = [dict(r) for r in ledger.open_ledger(state).records()]
        seen["argv"] = list(args)
        return {"ok": True, "returncode": 0, "stdout": "4242", "stderr": ""}

    monkeypatch.setattr(rm, "_run", fake_run)
    result = _submit(state, tmp_path)
    kinds_at_submit = [r["kind"] for r in seen.get("records", [])]
    assert kinds_at_submit and kinds_at_submit[0] == "intent", (kinds_at_submit, result)
    assert seen["argv"][0] == "sbatch"  # 意图先于真正的提交命令
    intent = seen["records"][0]
    assert intent["nonce"] and intent["scheduler"] == "slurm" and intent["output_root"]
    after = ledger.open_ledger(state).records()
    bound = [r for r in after if r["kind"] == "job_bound"]
    assert bound and bound[0]["external_id"] == str(result.get("job_id")) == "4242"
    assert bound[0]["nonce"] == intent["nonce"]  # 同一个 correlation key


def test_an_accepted_submission_without_a_parsable_identity_becomes_unknown_never_resubmitted(tmp_path, monkeypatch):
    state = _state(tmp_path)
    (tmp_path / "w").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(rm, "_run", lambda args, timeout=10: {
        "ok": True, "returncode": 0, "stdout": "Submitted batch job (unparsable)", "stderr": ""})
    result = _submit(state, tmp_path)
    assert result["status"] == "accepted_identity_unresolved" and result["do_not_resubmit"] is True
    rows = ledger.open_ledger(state).records()
    assert [r["kind"] for r in rows] == ["intent", "submission_attempted", "job_unknown"]
    jobs = ledger.project_jobs(rows)
    assert list(jobs.values())[0]["state"] == "unknown"
    assert ledger.open_jobs(rows), "unknown 作业必须留在开放列表里等接管"


def test_a_dry_run_writes_no_intent(tmp_path):
    state = _state(tmp_path)
    (tmp_path / "w").mkdir(parents=True, exist_ok=True)
    _submit(state, tmp_path, dry_run=True)
    assert ledger.open_ledger(state).records() == []


def test_the_delta_view_shows_facts_only_and_stays_bounded(tmp_path):
    state = _state(tmp_path)
    state.hook_state["node_inputs"]["required_outputs"] = [{"id": "exe", "kind": "file", "path": "build/hello"}]
    obligations.freeze(state)
    led = ledger.open_ledger(state, write=True, bootstrap=True)
    ledger.mark_bootstrap_ready(state)
    led.append("intent", nonce="n1", scheduler="slurm", output_root="r")
    led.append("job_bound", nonce="n1", external_id="77")
    led.append("action_started", action_id="a1", tool="safe_run_bash", command="make", effect_class="experiment")
    led.append("action_settled", action_id="a1", state="settled", exit_code=2, stderr_tail="make: *** Error 2")
    text = ledger_hooks.build_delta_view(state)
    assert "开放作业：77（submitted，slurm）" in text
    assert "exe：不在" in text
    assert "最近失败信号：a1 exit 2" in text
    assert len(text) < ledger_hooks._VIEW_MAX_CHARS
    # 视图只给事实：没有"下一步必须"之类的指令词
    assert "必须" not in text and "请先" not in text


def test_an_action_that_never_spawns_is_still_settled_not_left_open(tmp_path, monkeypatch):
    """开过的 action 一定要结。

    活体证据（preview run-v3ok-1，2026-09-23）：路线门在 spawn 前拒掉 10 次执行，
    账本里留下 10 条只有 action_started 的记录，finish 于是报了 10 条
    `action_open` 矛盾——全是账本自己制造的假矛盾，不是真的还有动作在跑。
    """
    import asyncio
    from nodes.experiment.tools import safe_bash

    state = _state(tmp_path)
    (tmp_path / "w").mkdir(parents=True, exist_ok=True)

    async def boom(*a, **kw):
        raise RuntimeError("sandbox refused before the payload ever started")

    monkeypatch.setattr(safe_bash, "spawn_and_wait", boom, raising=False)
    try:
        asyncio.run(safe_bash._exec_and_log(state, "echo hi", timeout=5, cwd=str(tmp_path / "w"),
                                            sandbox_write_targets=[str(tmp_path / "w")]))
    except Exception:
        pass
    rows = ledger.open_ledger(state).records()
    started = [r for r in rows if r["kind"] == "action_started"]
    settled = [r for r in rows if r["kind"] == "action_settled"]
    if started:  # 只有真的开了 action 才要求结算
        assert len(settled) == len(started), rows
        assert settled[0]["state"] in {"spawn_failed", "settled", "timed_out", "cancelled"}


# ── 统一 action 边界（Codex 21 号 P0-A / P0-B / P0-C）─────────────────────────

def test_a_ledger_that_cannot_record_the_start_stops_the_side_effect(tmp_path, monkeypatch):
    """P0-A：开始写不进账本就不许动手。

    先记 intent 再提交，唯一的目的就是 ACK 丢失或会话重开时不必靠猜。账本写不进去
    还照样 sbatch，等于把这条保证扔了。
    """
    from nodes.experiment.tools import action_boundary as ab

    state = _state(tmp_path)

    def boom(*a, **kw):
        raise ledger.LedgerError("disk full")

    monkeypatch.setattr(ledger, "record", boom)
    refusal = ab.begin(state, tool="safe_run_bash", command="make", work_root=str(tmp_path))
    assert isinstance(refusal, ab.BoundaryRefusal)
    result = refusal.as_result("safe_run_bash")
    assert result["error_code"] == "action_not_durably_recorded"
    assert result["payload_spawned"] is False and result["job_submitted"] is False


def test_the_scheduler_is_not_called_when_the_intent_cannot_be_persisted(tmp_path, monkeypatch):
    """P0-A 的活体形状：提交路径上账本坏了，sbatch 一次都不能被调用。"""
    state = _state(tmp_path)
    (tmp_path / "w").mkdir(parents=True, exist_ok=True)
    calls: list[list[str]] = []
    monkeypatch.setattr(rm, "_run", lambda argv, timeout=10: (calls.append(list(argv)) or
                                                              {"ok": True, "stdout": "1", "stderr": "", "returncode": 0}))

    real_record = ledger.record

    def only_intent_fails(state_arg, kind, /, **fields):
        if kind == "intent":
            raise ledger.LedgerError("disk full")
        return real_record(state_arg, kind, **fields)

    monkeypatch.setattr(ledger, "record", only_intent_fails)
    _submit(state, tmp_path)
    assert not any(argv and argv[0] == "sbatch" for argv in calls), calls


def test_the_scheduler_is_not_called_when_the_attempt_marker_cannot_be_persisted(tmp_path, monkeypatch):
    """B0：intent 成功不够；进入不可逆调用前的 marker 也必须 durable。"""
    state = _state(tmp_path)
    (tmp_path / "w").mkdir(parents=True, exist_ok=True)
    calls: list[list[str]] = []
    monkeypatch.setattr(rm, "_run", lambda argv, timeout=10: (calls.append(list(argv)) or
                                                              {"ok": True, "stdout": "1", "stderr": "", "returncode": 0}))

    real_try_record = ledger.try_record

    def only_attempt_marker_fails(state_arg, kind, /, **fields):
        if kind == "submission_attempted":
            return None
        return real_try_record(state_arg, kind, **fields)

    monkeypatch.setattr(ledger, "try_record", only_attempt_marker_fails)
    result = _submit(state, tmp_path)

    assert result["status"] == "error"
    assert result["error_code"] == "submission_attempt_not_durably_recorded"
    assert result["submission_boundary_crossed"] is False
    assert result["job_submitted"] is False and result["payload_spawned"] is False
    assert not any(argv and argv[0] == "sbatch" for argv in calls), calls
    rows = ledger.open_ledger(state).records()
    assert [row["kind"] for row in rows] == ["intent", "job_absent"], rows


def test_an_attempt_marker_exception_is_structured_and_never_calls_the_scheduler(tmp_path, monkeypatch):
    """B0：底层 I/O 异常与返回 None 同义，不能穿透成裸异常或越过物理边界。"""
    state = _state(tmp_path)
    (tmp_path / "w").mkdir(parents=True, exist_ok=True)
    calls: list[list[str]] = []
    monkeypatch.setattr(rm, "_run", lambda argv, timeout=10: (calls.append(list(argv)) or
                                                              {"ok": True, "stdout": "1", "stderr": "", "returncode": 0}))

    def marker_io_error(state_arg, kind, /, **fields):
        raise OSError("read-only filesystem")

    monkeypatch.setattr(ledger, "try_record", marker_io_error)
    result = _submit(state, tmp_path)

    assert result["status"] == "error"
    assert result["error_code"] == "submission_attempt_not_durably_recorded"
    assert result["submission_boundary_crossed"] is False
    assert result["job_submitted"] is False and result["payload_spawned"] is False
    assert not any(argv and argv[0] == "sbatch" for argv in calls), calls
    assert result["intent_closure"]["intent_left_open"] is True
    assert [row["kind"] for row in ledger.open_ledger(state).records()] == ["intent"]


def test_a_settle_that_cannot_be_recorded_is_not_a_plain_success(tmp_path, monkeypatch):
    """P0-B/A：副作用已经发生、账本没记下——不能报普通 success，也不能叫模型重跑。"""
    from nodes.experiment.tools import action_boundary as ab

    state = _state(tmp_path)
    handle = ab.begin(state, tool="safe_write_file", effect_class=ab.SUPPORT,
                      output_candidates=[tmp_path / "x.txt"])
    assert not isinstance(handle, ab.BoundaryRefusal)
    (tmp_path / "x.txt").write_text("side effect already happened")

    def boom(*a, **kw):
        raise ledger.LedgerError("disk full")

    monkeypatch.setattr(ledger, "record", boom)
    out = ab.settle(state, handle, outcome="settled", exit_code=0)
    assert out["status"] == "effect_recording_uncertain"
    assert out["side_effect_committed"] is True and out["payload_must_not_rerun"] is True
    assert out["next_action"]["model_callable"] is False


def test_the_observation_freezes_identities_not_just_path_names(tmp_path):
    """P0-B：只记"这条路径被动过"挡不住事后覆盖。"""
    from nodes.experiment.tools import action_boundary as ab

    state = _state(tmp_path)
    target = tmp_path / "out.bin"
    handle = ab.begin(state, tool="safe_run_bash", output_candidates=[target])
    target.write_bytes(b"the real product")
    assert ab.settle(state, handle, outcome="settled", exit_code=0)["status"] == "recorded"
    obs = [r for r in ledger.open_ledger(state).records() if r["kind"] == "observation"][-1]
    frozen = obs["identities"][str(target)]["after"]
    assert frozen["kind"] == "file" and frozen["sha256"]
    assert frozen["size"] == len(b"the real product")


def test_write_file_and_fetch_are_producers_the_closure_accepts(tmp_path):
    """P0-B：直接写出交付物、或下载即交付，都不该被判成无归属。"""
    from nodes.experiment.tools import action_boundary as ab

    state = _state(tmp_path)
    downloaded = tmp_path / "dataset.tar"
    downloaded.write_bytes(b"payload")
    out = ab.record_completed_effect(state, tool="fetch_resource", paths=[downloaded],
                                     command="fetch https://example.invalid/x")
    assert out["status"] == "recorded" and out["created"] == [str(downloaded)]
    obs = [r for r in ledger.open_ledger(state).records() if r["kind"] == "observation"][-1]
    row = obs["identities"][str(downloaded)]
    assert row["before_unknown"] is True and row["after"]["sha256"]  # 没看见之前的样子，如实说


def test_the_boundary_is_a_pass_through_outside_a_v3_run(tmp_path):
    """durable-or-refuse 是 v3 自己的保证，不能加到旧协议 run 上。

    2026-09-23 实测：不加这条判断时，28 条既有用例转红——它们的 run 没有 v3 账本，
    于是每一条有副作用的命令都被"记不下就不执行"拒掉。新协议不读写旧控制状态，
    也就不该给旧 run 加新门。
    """
    from nodes.experiment.tools import action_boundary as ab

    state = State.new(node_type="experiment", base_dir=tmp_path / "runs", project_id="p", session_id="s")
    state.hook_state["node_inputs"] = {"requested_work": "old protocol"}
    (Path(state.root) / "operation_evidence").mkdir(parents=True)  # v2 收尾留下的痕迹
    assert ledger.detect_epoch(state) == "old_protocol"
    handle = ab.begin(state, tool="safe_run_bash", command="make", work_root=str(tmp_path))
    assert not isinstance(handle, ab.BoundaryRefusal) and handle.recording is False
    assert ab.settle(state, handle, outcome="settled", exit_code=0) == {"status": "recorded", "noop": True}


def test_a_failure_after_the_intent_but_before_any_ack_closes_the_intent_as_absent(tmp_path, monkeypatch):
    """Codex 41 P0：intent 落盘后、sbatch 之前失败（这里让 health contract 抛错），intent 不能永远 open。"""
    state = _state(tmp_path)
    (tmp_path / "w").mkdir(parents=True, exist_ok=True)
    called = {"sbatch": 0}

    def fake_run(args, timeout=10):
        called["sbatch"] += 1
        return {"ok": True, "returncode": 0, "stdout": "4242", "stderr": ""}

    monkeypatch.setattr(rm, "_run", fake_run)
    monkeypatch.setattr(rm, "_health_contract", lambda *a, **k: (_ for _ in ()).throw(ValueError("bad health check")))
    result = _submit(state, tmp_path)
    assert result["status"] == "error" and "bad health check" in str(result.get("error")), result
    assert called["sbatch"] == 0
    rows = ledger.open_ledger(state).records()
    assert [r["kind"] for r in rows] == ["intent", "job_absent"], [r["kind"] for r in rows]
    assert ledger.open_jobs(rows) == []  # 不再是活作业
    assert list(ledger.project_jobs(rows).values())[0]["state"] == "absent"


def test_an_absent_verdict_from_the_adapter_closes_the_intent_in_the_projection(tmp_path):
    state = _state(tmp_path)
    led = ledger.open_ledger(state, write=True)
    led.append("intent", nonce="n9", scheduler="slurm", output_root="r")
    assert ledger.open_jobs(led.records()) and ledger.open_jobs(led.records())[0]["state"] == "intent"
    led.append("job_absent", nonce="n9", scheduler="slurm", reason="not in squeue nor sacct")
    assert ledger.open_jobs(led.records()) == []
    assert ledger.project_jobs(led.records())["n9"]["state"] == "absent"


def test_a_failure_after_the_scheduler_was_called_is_unknown_not_absent(tmp_path, monkeypatch):
    """Codex 45：ACK 可能已经发生时绝不能猜 absent。sbatch 被调用后抛异常 → job_unknown，留在开放列表。"""
    state = _state(tmp_path)
    (tmp_path / "w").mkdir(parents=True, exist_ok=True)
    def exploding_run(args, timeout=10):
        raise RuntimeError("connection reset after submit")
    monkeypatch.setattr(rm, "_run", exploding_run)
    with pytest.raises(RuntimeError):
        _submit(state, tmp_path)
    rows = ledger.open_ledger(state).records()
    kinds = [r["kind"] for r in rows]
    assert kinds[:2] == ["intent", "submission_attempted"], kinds
    assert "job_unknown" in kinds and "job_absent" not in kinds, kinds
    assert ledger.open_jobs(rows) and ledger.open_jobs(rows)[0]["state"] == "unknown"


def test_a_failed_intent_closure_is_disclosed_not_silent(tmp_path, monkeypatch):
    state = _state(tmp_path)
    (tmp_path / "w").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(rm, "_run", lambda args, timeout=10: {"ok": True, "returncode": 0, "stdout": "4242", "stderr": ""})
    monkeypatch.setattr(rm, "_health_contract", lambda *a, **k: (_ for _ in ()).throw(ValueError("bad health check")))
    from nodes.experiment.tools import ledger as L
    real = L.try_record
    monkeypatch.setattr(L, "try_record", lambda state_, kind, /, **f: None if kind == "job_absent" else real(state_, kind, **f))
    result = _submit(state, tmp_path)
    assert result["status"] == "error"
    assert result["intent_closure"]["status"] == "ledger_write_failed" and result["intent_closure"]["intent_left_open"] is True
    rows = ledger.open_ledger(state).records()
    assert ledger.open_jobs(rows) and ledger.open_jobs(rows)[0]["state"] == "intent"

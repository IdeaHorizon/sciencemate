"""060：协议纪元只能由 bootstrap 开启；fetch 的记账在副作用之前（Codex 25 号 P0-1/P0-2）。

三种纪元都走真实调用路径：old_protocol、fresh 未 bootstrap、v3 已 bootstrap。
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from core.state import State
from nodes.experiment.tools import action_boundary as ab, ledger, obligations, resource_fetch, run_bootstrap
from nodes.experiment.tools.path_roles import collect_path_roles


def _destination(state: State) -> str:
    """下载目的地必须落在本 run 的可写角色里（fetch 自己的准入先于账本边界生效）。"""
    for role in collect_path_roles(state):
        if role.role == "run_root":
            return str(Path(role.path) / "input.bin")
    raise AssertionError("run_root 未物化")


def _fresh(tmp_path: Path, name: str = "runs") -> State:
    state = State.new(node_type="experiment", base_dir=tmp_path / name, project_id="p", session_id="owner")
    state.hook_state["node_inputs"] = {
        "prereg_assignment": {"kind": "none", "reason": "本地任务"},
        "scope": "operation", "operation_category": "other", "requested_work": "取一份输入",
        "required_outputs": [{"id": "input", "kind": "file", "path": "outputs/experiment/runtime/input.bin"}]}
    return state


def _bootstrapped(tmp_path: Path, name: str = "runs") -> State:
    state = _fresh(tmp_path, name)
    asyncio.run(run_bootstrap.establish(state))
    assert ledger.v3_active(state)
    return state


# ── P0-1：纪元只从 bootstrap 开始 ────────────────────────────────────────────

def test_no_writer_can_open_the_v3_epoch_on_a_fresh_run(tmp_path):
    """一次 fetch 或 submit 不能把旧 harness 的 run 静默带进半个 v3。

    2026-09-23 实测（修前）：`record_completed_effect` 让 fresh run 的 epoch 从
    fresh 变成 v3 并写出了 ledger meta——没有 bootstrap、没有冻结义务、没有一页纸。
    """
    state = _fresh(tmp_path)
    assert ledger.detect_epoch(state) == "fresh" and ledger.v3_active(state) is False
    handle = ab.begin(state, tool="safe_run_bash", command="make")
    assert not isinstance(handle, ab.BoundaryRefusal) and handle.recording is False
    assert ab.settle(state, handle, outcome="settled", exit_code=0)["noop"] is True
    led = ledger.open_ledger(state, write=True)
    assert led.read_only and "only run bootstrap" in led.reason
    assert not (ledger.ledger_dir(state) / "ledger.meta.json").exists()
    assert ledger.detect_epoch(state) == "fresh"


def test_bootstrap_is_the_one_place_that_opens_the_epoch(tmp_path):
    state = _bootstrapped(tmp_path)
    assert ledger.detect_epoch(state) == ledger.PROTOCOL_EPOCH
    kinds = [r["kind"] for r in ledger.open_ledger(state).records()]
    assert kinds[0] == "run_bootstrap"


def test_an_old_protocol_run_is_left_entirely_alone(tmp_path):
    state = _fresh(tmp_path)
    (Path(state.root) / "operation_evidence").mkdir(parents=True)
    assert ledger.detect_epoch(state) == "old_protocol"
    handle = ab.begin(state, tool="safe_write_file", command="write x")
    assert handle.recording is False
    assert not (ledger.ledger_dir(state) / "ledger.meta.json").exists()


# ── P0-2：fetch 的 durable-or-refuse 在副作用之前 ────────────────────────────

def _spy_fetch(monkeypatch):
    """把最底层的 spawn 换成探针：它被调用 = 已经发生了网络副作用。"""
    calls: list[list[str]] = []

    async def spy(*argv, **kw):
        calls.append([str(a) for a in argv][:3])
        return "done", 0, b"", b""

    monkeypatch.setattr(resource_fetch, "spawn_and_wait", spy, raising=False)
    return calls


def test_a_v3_run_whose_ledger_cannot_record_never_reaches_the_network(tmp_path, monkeypatch):
    """P0-2 的核心：start 写不进账本时，底层 fetch 一次都不能发生。"""
    state = _bootstrapped(tmp_path)
    calls = _spy_fetch(monkeypatch)

    def boom(*a, **kw):
        raise ledger.LedgerError("disk full")

    monkeypatch.setattr(ledger, "record", boom)
    out = asyncio.run(resource_fetch._fetch_resource(
        state, url="https://github.com/example/repo/archive/x.tar", kind="file",
        destination=_destination(state)))
    assert out.get("error_code") == "action_not_durably_recorded", out
    assert calls == [], f"账本记不下还是发了网络请求：{calls}"


def test_a_fresh_unbootstrapped_run_fetches_exactly_as_before(tmp_path, monkeypatch):
    """未切换的 run 不被 v3 改写：既不写账本，也不把成功改成 uncertain。

    可写根要先物化，否则 fetch 在解析目的地时就失败，这一格会变成空转的绿。
    物化本身不开启纪元——只有 bootstrap 写 meta 才开启。
    """
    state = _fresh(tmp_path)
    run_bootstrap.materialize_writable_roots(state)
    # 旧 harness 的 run 会先走 v2 分类；不做这一步 fetch 会被 v2 的门拦在副作用之前，
    # 这一格就成了空转的绿。
    from nodes.experiment.tools.run_contract import _classify_experiment_scope
    assert asyncio.run(_classify_experiment_scope(
        state, scope="operation", operation_category="other",
        reason="旧协议 run 的取输入动作"))["status"] == "success"
    assert not ledger.v3_active(state)
    recorded: list[str] = []
    real_record = ledger.record
    monkeypatch.setattr(ledger, "record", lambda st, kind, /, **f: (recorded.append(kind) or real_record(st, kind, **f)))
    reached: list[str] = []

    async def spy(*argv, **kw):
        reached.append("spawn")
        raise RuntimeError("network blocked in test")

    monkeypatch.setattr(resource_fetch, "spawn_and_wait", spy, raising=False)
    out = asyncio.run(resource_fetch._fetch_resource(
        state, url="https://github.com/example/repo/archive/x.tar", kind="file",
        destination=_destination(state)))
    assert reached == ["spawn"], f"这一格必须真的走到 fetch 的副作用处，否则是空转的绿：{out}"
    assert out.get("error_code") != "action_not_durably_recorded"
    assert recorded == [], f"未 bootstrap 的 run 被写进了 v3 账本：{recorded}"
    assert not (ledger.ledger_dir(state) / "ledger.meta.json").exists()


def test_the_fetch_boundary_opens_before_the_first_spawn(tmp_path, monkeypatch):
    """顺序本身：action_started 必须在第一次 spawn 之前落盘。"""
    state = _bootstrapped(tmp_path)
    order: list[str] = []
    real_record = ledger.record

    def watch(st, kind, /, **fields):
        if kind == "action_started":
            order.append("ledger")
        return real_record(st, kind, **fields)

    async def spy(*argv, **kw):
        order.append("spawn")
        raise RuntimeError("stop here")

    monkeypatch.setattr(ledger, "record", watch)
    monkeypatch.setattr(resource_fetch, "spawn_and_wait", spy, raising=False)
    asyncio.run(resource_fetch._fetch_resource(
        state, url="https://github.com/example/repo/archive/x.tar", kind="file",
        destination=_destination(state)))
    assert order[:2] == ["ledger", "spawn"], order
    kinds = [r["kind"] for r in ledger.open_ledger(state).records()]
    assert "action_started" in kinds and "action_settled" in kinds  # 失败出口也结算了


# ── Codex 27 号：bootstrap 原子 ready；fetch 的目录物化在 begin 之后 ─────────

def test_a_half_finished_bootstrap_refuses_writers_and_does_not_fall_back_to_legacy(tmp_path, monkeypatch):
    """meta 在、ready 不在 = initializing：既不能当普通 v3 run，也不能退回旧协议透传。"""
    from nodes.experiment.tools import obligations, resource_manager as rm

    state = _fresh(tmp_path)
    monkeypatch.setattr(obligations, "freeze", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    established = asyncio.run(run_bootstrap.establish(state))
    assert established["obligations"]["status"] == "error"
    assert ledger.bootstrap_phase(state) == "initializing" and ledger.v3_active(state) is False
    kinds = [r["kind"] for r in ledger.open_ledger(state).records()]
    assert "bootstrap_incomplete" in kinds and "bootstrap_ready" not in kinds
    # 三个写入口都拒绝且不透传
    refusal = ab.begin(state, tool="safe_run_bash", command="make")
    assert isinstance(refusal, ab.BoundaryRefusal) and refusal.code == "bootstrap_incomplete"
    assert refusal.as_result("safe_run_bash")["next_action"]["action"] == "bootstrap_replays_next_turn"
    assert ab.record_completed_effect(state, tool="fetch_resource", paths=[])["error_code"] == "bootstrap_incomplete"
    calls: list = []
    monkeypatch.setattr(rm, "_run", lambda argv, timeout=10: (calls.append(argv) or {"ok": True, "stdout": "1", "stderr": "", "returncode": 0}))
    (tmp_path / "w").mkdir(exist_ok=True)
    out = rm._submit_sync(runtime_root=tmp_path / "rt", scheduler="slurm", command="echo", job_name="j",
                          mpi_ranks=1, cpus_per_rank=1, gpus=0, memory_gb=1.0, storage_gb=1.0,
                          walltime_minutes=5, queue=None, nodelist=None, image=None,
                          workdir=str(tmp_path / "w"), dry_run=False, namespace=None,
                          stage_in=None, state=state)
    assert out["error_code"] == "bootstrap_incomplete" and calls == []


def test_bootstrap_freezes_prose_and_only_absent_task_needs_clarification(tmp_path):
    state = _bootstrapped(tmp_path)
    assert ledger.bootstrap_phase(state) == "ready"
    # 自然任务原文就是 A；没有内部数组不等于没有任务。
    free = State.new(node_type="experiment", base_dir=tmp_path / "r2", project_id="p", session_id="o")
    free.hook_state["node_inputs"] = {"prereg_assignment": {"kind": "none", "reason": "x"},
                                      "scope": "operation", "operation_category": "other",
                                      "experiment_spec": "装上"}
    est = asyncio.run(run_bootstrap.establish(free))
    assert est["obligations"]["status"] == "frozen"
    assert obligations.task_source(free)["text"] == "装上"
    assert ledger.bootstrap_phase(free) == "ready"
    # 真正没有任务正文也没有结构化要求时，不能从结果倒推任务。
    absent = State.new(node_type="experiment", base_dir=tmp_path / "r3", project_id="p", session_id="o")
    absent.hook_state["node_inputs"] = {"prereg_assignment": {"kind": "none", "reason": "x"}}
    absent_est = asyncio.run(run_bootstrap.establish(absent))
    assert absent_est["obligations"]["status"] == "clarification_required"
    assert obligations.task_source(absent) is None
    assert ledger.bootstrap_phase(absent) == "ready"


def test_fetch_creates_no_directory_when_the_ledger_cannot_record_the_start(tmp_path, monkeypatch):
    """目录物化也是本 action 的副作用：账本记不下，父目录也不能出现。"""
    state = _bootstrapped(tmp_path)
    nested = Path(_destination(state)).parent / "deep" / "nested" / "input.bin"
    assert not nested.parent.exists()
    monkeypatch.setattr(ledger, "record", lambda *a, **k: (_ for _ in ()).throw(ledger.LedgerError("disk full")))
    out = asyncio.run(resource_fetch._fetch_resource(
        state, url="https://github.com/example/repo/archive/x.tar", kind="file", destination=str(nested)))
    assert out.get("error_code") == "action_not_durably_recorded", out
    assert not nested.parent.exists(), "begin 失败但目录已经被建出来了"


def test_on_the_normal_path_the_directory_appears_only_after_action_started(tmp_path, monkeypatch):
    state = _bootstrapped(tmp_path)
    nested = Path(_destination(state)).parent / "deep2" / "input.bin"
    order: list[str] = []
    real_record = ledger.record

    def watch(st, kind, /, **fields):
        if kind == "action_started":
            order.append(("ledger", nested.parent.exists()))
        return real_record(st, kind, **fields)

    async def spy(*argv, **kw):
        order.append(("spawn", nested.parent.exists()))
        raise RuntimeError("stop")

    monkeypatch.setattr(ledger, "record", watch)
    monkeypatch.setattr(resource_fetch, "spawn_and_wait", spy, raising=False)
    asyncio.run(resource_fetch._fetch_resource(
        state, url="https://github.com/example/repo/archive/x.tar", kind="file", destination=str(nested)))
    assert order[0] == ("ledger", False) and order[1] == ("spawn", True), order

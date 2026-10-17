"""060 S1d：v3 就绪的 run，写/执行/提交路径不读不写旧路线 authority（30 号任务书 §3）。

用运行时探针证明，不只从 YAML 删工具名：把 execution_route 与 census 的入口全部换成
会抛的桩，再走真实工具入口。任何一处被调用，测试就红。
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from core.state import State
from nodes.experiment.tools import execution_action_census as census
from nodes.experiment.tools import execution_route as er
from nodes.experiment.tools import ledger, resource_manager as rm, run_bootstrap, safe_bash
from nodes.experiment.tools.path_roles import collect_path_roles

ROUTE_ENTRYPOINTS = [
    (er, "resolve_execution_context"), (er, "enforce_execution_route"), (er, "route_default_workdir"),
    (er, "record_execution_route_shadow"), (er, "begin_route_step_attempt"), (er, "finish_route_step_attempt"),
    (er, "build_route_snapshot"), (er, "load_canonical_route"),
    (census, "begin_execution_action"), (census, "settle_execution_action"),
    (census, "observe_execution_action_spawn"), (census, "pending_operation_action_block"),
]


class RouteAuthorityTouched(AssertionError):
    pass


def _arm_probes(monkeypatch):
    touched: list[str] = []
    for mod, name in ROUTE_ENTRYPOINTS:
        if not hasattr(mod, name):
            continue

        def boom(*a, _n=name, **k):
            touched.append(_n)
            raise RouteAuthorityTouched(f"v3 path touched old authority: {_n}")

        monkeypatch.setattr(mod, name, boom)
    return touched


def _v3_state(tmp_path: Path) -> State:
    state = State.new(node_type="experiment", base_dir=tmp_path / "runs", project_id="p", session_id="owner")
    state.hook_state["node_inputs"] = {
        "prereg_assignment": {"kind": "none", "reason": "本地任务"},
        "scope": "operation", "operation_category": "toolchain_build",
        "requested_work": "在可写根里写一个文件并跑一条命令",
        "required_outputs": [{"id": "out", "kind": "file", "path": "outputs/experiment/runtime/out.txt"}]}
    asyncio.run(run_bootstrap.establish(state))
    assert ledger.v3_active(state)
    return state


def _run_root(state: State) -> Path:
    return Path(next(r.path for r in collect_path_roles(state) if r.role == "run_root"))


def test_write_file_on_a_v3_run_never_touches_route_or_census(tmp_path, monkeypatch):
    state = _v3_state(tmp_path)
    touched = _arm_probes(monkeypatch)
    target = _run_root(state) / "out.txt"
    out = asyncio.run(safe_bash._safe_write_file(state, path=str(target), content="hello\n"))
    assert out.get("status") == "success", out
    assert touched == []
    kinds = [r["kind"] for r in ledger.open_ledger(state).records()]
    assert "action_started" in kinds and "action_settled" in kinds  # 事实由边界记


def test_run_bash_on_a_v3_run_never_touches_route_or_census(tmp_path, monkeypatch):
    state = _v3_state(tmp_path)
    touched = _arm_probes(monkeypatch)
    root = _run_root(state)
    out = asyncio.run(safe_bash._safe_run_bash(state, cmd=f"printf v3 > {root}/out.txt", cwd=str(root), timeout=60))
    if out.get("status") != "success" and "sandbox" in str(out.get("error", "")).lower():
        pytest.skip(f"沙箱不可用：{str(out.get('error'))[:120]}")
    assert out.get("status") == "success", out
    assert touched == []
    assert (root / "out.txt").read_text() == "v3"
    obs = [r for r in ledger.open_ledger(state).records() if r["kind"] == "observation"]
    assert any(str(root / "out.txt") in (o.get("created") or []) for o in obs)


def test_submit_on_a_v3_run_never_touches_route_or_census(tmp_path, monkeypatch):
    state = _v3_state(tmp_path)
    touched = _arm_probes(monkeypatch)
    monkeypatch.setattr(rm, "_run", lambda argv, timeout=10: {"ok": True, "stdout": "4242", "stderr": "", "returncode": 0})
    out = asyncio.run(rm._submit_job(
        state, scheduler="slurm", command="echo hi", job_name="hello", workdir=str(_run_root(state)),
        mpi_ranks=1, cpus_per_rank=1, gpus=0, memory_gb=1.0, storage_gb=1.0, walltime_minutes=5))
    assert touched == [], touched
    # 工具层对远端调度器只允许 dry_run 渲染（真实提交自 ffc6c245 起硬拒，S1c-final 才开），
    # 所以这里验的是"整条 submit_job 路径不碰旧 authority"；intent→job_bound 的顺序由
    # test_060_common_accounting_boundary 在 _submit_sync 上用假调度器钉住。
    assert out.get("status") == "success" and out.get("dry_run") is True, out


def test_a_pre_v3_run_still_walks_the_old_control_plane(tmp_path, monkeypatch):
    """反证：不是 v3 就绪的 run，旧 authority 照旧被调用——短路只对 v3 生效。"""
    state = State.new(node_type="experiment", base_dir=tmp_path / "runs", project_id="p", session_id="s")
    state.hook_state["node_inputs"] = {"requested_work": "old"}
    touched = _arm_probes(monkeypatch)
    # Exercise the old node authority after satisfying the independent Core
    # file ownership boundary; do not use an out-of-run target as a proxy.
    target = Path(state.root) / "x.txt"
    # 旧路径里 resolver 的异常会被它自己的 except 吞成 resolver_error，所以看探针有没有被碰，
    # 不看有没有抛出来。
    asyncio.run(safe_bash._safe_write_file(state, path=str(target), content="x"))
    assert touched, "非 v3 run 竟然也没走旧 authority——短路范围过宽"



# ── Codex 35/36 P0-A：fetch_resource 在 v3 上不进旧 census/route authority ──────────

def test_fetch_on_a_v3_run_never_touches_route_or_census_and_still_accounts(tmp_path, monkeypatch):
    from nodes.experiment.tools import resource_fetch as fetch
    import importlib
    state = _v3_state(tmp_path)
    touched = _arm_probes(monkeypatch)
    # 无效 URL：正确的 v3 路径在联网前就拒绝；旧门零调用
    bad = asyncio.run(fetch._fetch_resource(state, url="not-a-url", destination="x"))
    assert bad.get("status") == "error", bad
    assert "OLD" not in str(bad) and "RouteAuthorityTouched" not in str(bad), bad
    assert touched == [], touched
    # 白名单域 + stub spawn（不联网）：action begin/settle 仍发生
    spawned = []
    async def fake_spawn(*args, **kwargs):
        spawned.append(kwargs)
        return ("done", 22, b"", b"curl: (6) Could not resolve host")
    modules = {fetch}
    try:  # bootstrap 之后同一文件还有第二个模块对象 tools.resource_fetch；单跑本文件时没有
        modules.add(importlib.import_module("tools.resource_fetch"))
    except ModuleNotFoundError:
        pass
    for module in modules:
        monkeypatch.setattr(module, "spawn_and_wait", fake_spawn)
    out = asyncio.run(fetch._fetch_resource(state, url="https://github.com/mom-ocean/MOM6/archive/refs/heads/main.tar.gz",
                                            destination="mom6.tar.gz"))
    assert touched == [], touched
    kinds = [r["kind"] for r in ledger.open_ledger(state).records()]
    assert "action_started" in kinds and "action_settled" in kinds, (kinds, out)
    started = [r for r in ledger.open_ledger(state).records() if r["kind"] == "action_started"]
    assert started[-1].get("tool") == "fetch_resource", started[-1]


# ── Codex 36 P1：safe_execute_python 同样零触达，且账本 tool 是它自己 ───────────

def test_execute_python_on_a_v3_run_never_touches_route_or_census(tmp_path, monkeypatch):
    state = _v3_state(tmp_path)
    touched = _arm_probes(monkeypatch)
    r = asyncio.run(safe_bash._safe_execute_python(state, code="print('HF_PY_OK')", timeout=60))
    if r.get("status") != "success" and "sandbox" in str(r.get("error") or "").lower():
        pytest.skip(str(r.get("error"))[:120])
    assert touched == [], touched
    started = [x for x in ledger.open_ledger(state).records() if x["kind"] == "action_started"]
    assert started and started[-1].get("tool") == "safe_execute_python", started

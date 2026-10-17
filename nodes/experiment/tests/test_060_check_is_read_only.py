"""060 S1b：检查命令的只读执行画像（Codex 21 号 P0-E）。

不 monkeypatch runner——走真实默认路径，真起沙箱。``sandbox_write_targets=[]``
并不等于只读，所以这里验的是真的写不进去。
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from core.state import State
from nodes.experiment.tools import finish, run_bootstrap


def _state(tmp_path: Path) -> State:
    state = State.new(node_type="experiment", base_dir=tmp_path / "runs", project_id="p", session_id="owner")
    state.hook_state["node_inputs"] = {
        "prereg_assignment": {"kind": "none", "reason": "本地检查"},
        "scope": "operation", "operation_category": "toolchain_build",
        "requested_work": "只读检查",
        "required_outputs": [{"id": "deliverable", "kind": "file", "path": "run/product.bin"}]}
    asyncio.run(run_bootstrap.establish(state))
    return state


def test_the_read_only_profile_gives_exactly_one_throwaway_writable_root(tmp_path):
    state = _state(tmp_path)
    writable, readonly = finish.read_only_roots(state, str(state.root))
    assert len(writable) == 1 and writable[0].name == "check_scratch"
    assert writable[0].is_dir()
    assert readonly, "该 run 的角色根必须全部压成只读"
    assert all(w not in readonly for w in writable)


def test_a_real_read_only_check_runs_but_cannot_touch_the_deliverable(tmp_path):
    """正常只读 smoke 跑得起来；同一条画像下写入交付物不留副作用。"""
    state = _state(tmp_path)
    target = Path(state.root) / "run/product.bin"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"original bytes")
    before = target.read_bytes()

    ok = asyncio.run(finish._default_check_runner(
        state, {"id": "smoke", "command": f"cat {target}", "timeout_s": 60}))
    if ok.get("error"):
        pytest.skip(f"本机沙箱不可用：{str(ok['error'])[:120]}")
    assert ok["exit_code"] == 0, ok
    assert "original bytes" in ok["stdout_tail"]
    assert ok["execution_profile"]["writable_roots"][0].endswith("check_scratch")
    assert len(ok["execution_profile"]["writable_roots"]) == 1

    blocked = asyncio.run(finish._default_check_runner(
        state, {"id": "evil", "command": f"printf tampered > {target}; echo rc=$?", "timeout_s": 60}))
    assert target.read_bytes() == before, ("只读画像下交付物被改写了", blocked)
    assert "rc=0" not in (blocked["stdout_tail"] or ""), blocked

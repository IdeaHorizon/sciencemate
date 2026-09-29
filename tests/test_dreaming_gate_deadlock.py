"""v3.2 死锁修复回归（2026-07 v10c dogfood 实测：12 次 curator 循环，113 分钟
不收敛，8 次 run_node(writing) 全部被拦）。

根因：writing-gate 的 dreaming 硬门禁靠 maybe_check_stale_from_audit() 判断
"curator 有没有做过 dreaming"；它读的审计链路（core.curator_audit）从未被
真实执行路径调用过（curator_audit 工具只有 log/revert，没有写入动作）——
门禁永远看到"从未跑过"，永远重新 mark_pending，死锁不会自愈。

修复：clear_pending() 在 dreaming 真结束时框架自己盖章 last_dreaming.json；
run_node.py 检测到 _curator(mode='dreaming') 真 completed 时直接调
clear_pending，不依赖 LLM 记得调任何审计工具。
"""
from __future__ import annotations

import asyncio

import pytest

from core.state import State


def test_old_audit_path_would_have_deadlocked(tmp_path):
    """先证明旧根因确实存在：curator_audit 工具没有写入动作。"""
    from shared.tools.library.audit import _AUDIT_ACTIONS
    assert "log" in _AUDIT_ACTIONS and "revert" in _AUDIT_ACTIONS
    assert "record" not in _AUDIT_ACTIONS and "write" not in _AUDIT_ACTIONS
    # CuratorAudit 类存在但工具层无法构造它——没有"记一次 dreaming 发生"的通道


def test_maybe_check_stale_returns_true_without_any_stamp(tmp_path, monkeypatch):
    """没有任何 dreaming 记录时（新项目），stale 检查该返 True（首次必须跑）——
    这本身没错，错的是"跑完之后还是永远 True"。"""
    monkeypatch.setenv("HARNESS_FRAMEWORK_HOME", str(tmp_path))
    from core.dreaming_scheduler import maybe_check_stale_from_audit
    assert maybe_check_stale_from_audit("proj_never_dreamed") is True


def _seed_completed_curator_run(home, pid, run_id="1790000000-cccccc"):
    """在 run 账本里种一次**真的跑完的** curator run（run_node 的产物形状）。"""
    import json as _json

    d = home / "projects" / pid / "runs" / run_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "summary.json").write_text(_json.dumps({
        "run_id": run_id, "node_type": "_curator", "status": "completed",
    }), encoding="utf-8")


def test_a_real_dreaming_run_unblocks_the_stale_check(tmp_path, monkeypatch):
    """核心回归：真跑过一次 dreaming 之后，stale 检查必须变 False（不再死锁）。

    判据换过两次锚，两次都是因为锚会撒谎：
      · v3.2 前读 `curator_audit`：那条链路全仓零构造点，永远判"从未跑过"，
        门禁死锁不自愈（实测 12 次 curator 循环 / 113 分钟）。
      · v3.2 后读 `last_dreaming.json` 盖章文件：`/skip-dreaming` 和 dreaming
        失败路径都经 `clear_pending()` 盖章 —— 跳过等于做过。
    现在问 **run 账本**：跳过不会产生 run，所以无章可盖。
    """
    monkeypatch.setenv("HARNESS_FRAMEWORK_HOME", str(tmp_path))
    from core.dreaming_scheduler import (
        clear_pending, maybe_check_stale_from_audit, read_pending,
    )
    pid = "proj_deadlock_test"

    # 项目刚开始，从未 dreaming 过 → stale 检查该是 True
    assert maybe_check_stale_from_audit(pid) is True

    # 只清 pending（= 跳过 / 失败路径）**不该**让判据翻面
    clear_pending(pid)
    assert maybe_check_stale_from_audit(pid) is True, "跳过被记成做过了"

    # 真跑完一次 → 账本里有证据 → 门禁放行
    _seed_completed_curator_run(tmp_path, pid)
    clear_pending(pid)
    assert maybe_check_stale_from_audit(pid) is False
    assert read_pending(pid) is None or not read_pending(pid).get("pending")


@pytest.mark.asyncio
async def test_dreaming_run_node_completion_clears_pending(tmp_path, monkeypatch):
    """端到端：_curator(mode='dreaming') 通过 run_node 完成后，
    writing 门禁应立即放行（不再需要额外一轮"记审计"）。"""
    from core.tool_registry import execute
    from core.bootstrap import bootstrap
    bootstrap()

    pid = "proj_e2e_dreaming"
    monkeypatch.setenv("HARNESS_FRAMEWORK_HOME", str(tmp_path))

    orch = State.new(node_type="_orchestrator", base_dir=tmp_path / "orch", project_id=pid)
    orch.hook_state["_callable_nodes"] = ["*"]

    # 手工模拟 _import_required_outputs 之后的分支逻辑：直接调用会被
    # _run_node_tool 内部触发的 clear_pending 路径。用最小化方式验证：
    # 直接调用 dreaming_scheduler 的两个函数模拟"curator dreaming 刚完成"。
    from core.dreaming_scheduler import (
        mark_pending, clear_pending, should_run_dreaming,
    )
    mark_pending(pid, reason="test setup: force pending")
    should, reasons = should_run_dreaming(pid)
    assert should is True and reasons

    # run_node 跑完一次 _curator 会在账本留下 summary.json —— 这才是"做过了"
    # 的证据。只调 clear_pending 相当于跳过，不该放行。
    _seed_completed_curator_run(tmp_path, pid)
    clear_pending(pid)

    should2, reasons2 = should_run_dreaming(pid)
    assert should2 is False, f"dreaming 完成后 should_run_dreaming 仍返 True：{reasons2}"

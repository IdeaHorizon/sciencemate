"""同一个节点被再次派发，必须拿到**自己那一张卡片**。

## 现场（2026-08-20，本机会话 306cd643）

observation 跑完一轮（44 actions）→ literature 补证据 → 调度器让 observation
**再检视一遍**。此后 UI 上：

- 右栏「当前」写着"当前没有节点在跑。"，而 observation 正跑着（DB 里那行
  status=running 且每分钟在更新，transcript 逐轮落盘）；
- 新一轮的 62 次工具调用，全部追加到**上一轮那张已经标"完成"的卡片**上
  （用户两张截图：同一张 observation 卡从 44 actions 涨到 62）；
- 那张新派发的 chip 是灰的、点不开。

## 根因

平台侧子 run id 是 `<parent>::_orchestrator->observation@d1` —— 标识的是**槽位**
（父 run + 节点类型 + 深度），不是这一次派发。root step id 由它派生，于是第二次
派发算出与第一次相同的 id，event_id 随之相同，`_insert_event` 按 event_id 幂等
→ 新的 `step.started` 被当成重复**静默吞掉**。

判据不是"有没有发事件"，是"**这一次派发有没有自己的身份**"。
`childRunId`（子 run transcript 所在目录名）天然带"第几次"。
"""
from __future__ import annotations

import pytest

from app.services.execution_ingest import _root_step_id


def test_two_dispatches_of_the_same_node_get_different_steps() -> None:
    """槽位 id 相同、childRunId 不同 → step id 必须不同。"""
    slot = "run_abc::_orchestrator->observation@d1"
    first = _root_step_id(slot, 1, {"childRunId": "1787206668-4b8856"})
    second = _root_step_id(slot, 1, {"childRunId": "1787208991-8997d3"})
    assert first != second, (
        "同一个节点第二次派发算出了同一个 step id —— "
        "step.started 会被幂等吞掉，UI 从此说'当前没有节点在跑'"
    )


def test_parent_and_child_never_share_a_step() -> None:
    """2026-08-11 那条回归守卫：父子共用 platform run，不能共用 step。"""
    run = "run_abc"
    parent = _root_step_id(run, 1, {})
    child = _root_step_id(run, 1, {"childRunId": "1787206668-4b8856"})
    assert parent != child


def test_same_dispatch_is_stable_across_calls() -> None:
    """同一次派发反复算必须得到同一个 id —— 否则一次运行会碎成很多张卡。"""
    state = {"childRunId": "1787208991-8997d3"}
    run = "run_abc::_orchestrator->observation@d1"
    assert _root_step_id(run, 1, state) == _root_step_id(run, 1, state)


def test_the_live_path_uses_the_shared_derivation() -> None:
    """`_ensure_root_step` 是**唯一跑得到**的那处（harness 从不发
    root_step_start，全仓 0 次）。它必须调共用算法，不许再抄一份 —— 抄件分叉
    不会报错，只会让 UI 说谎。
    """
    import inspect

    from app.services.execution_ingest import ExecutionIngestService

    source = inspect.getsource(ExecutionIngestService._ensure_root_step)
    assert "_root_step_id(" in source, "live path 必须调共用算法"
    assert "'root'" not in source and '"root"' not in source, (
        "又抄了一份 step id 算法 —— 这正是 2026-08-20 那次 UI 说谎的成因"
    )

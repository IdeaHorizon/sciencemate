"""审查后的解释交回调度器；已授权动作、手工编辑和派发失败仍由运行时处理。

解释先于弹框的控制流由 test_local_e2e_post_node_flow.py 验证。
这里保留人工选择之后的执行边界检查。
"""
from __future__ import annotations

import inspect

import pytest

import core.pause_driver as pd
from shared.tools import run_node as rn


def test_an_authorized_action_is_executed_by_the_runtime():
    """窗口二：人选了 REVISE / REDIRECT。

    运行时在拿到答复的**同一刻**直接起目标节点 —— 那是框架唯一确知"人选了什么"
    且"该起谁"的时刻。交回调度器就等于把排序权还给它。
    """
    src = inspect.getsource(pd.execute_authorized_action)
    assert "authorized_target_node" in src and "_run_node_tool" in src

    settle = inspect.getsource(pd._settle_decision_answer)
    assert "_execute_authorized_action(" in settle


def test_edit_and_dispatch_failure_both_hold_the_pause():
    """窗口三：EDIT 与「授权动作没执行成」。

    - EDIT：选项文案自己写着 *pauses for you to edit*，那它就该保持 pause。
    - 派发失败：运行时没能执行人的决定 = 框架失败，不是"轮到调度器想办法"。
      把一个不一致的状态交回去，然后再用一道墙拦它，正是这一整轮要消灭的形状。
    """
    settle = inspect.getsource(pd._settle_decision_answer)

    assert "awaiting_manual_edit" in settle, "EDIT 必须在这一层被拦下，不能 resume"
    assert "if not await _execute_authorized_action" in settle, (
        "派发失败必须保持 pause 并把失败带回给人重新裁决"
    )


def test_the_deleted_walls_stay_deleted():
    """墙不许长回来 —— 回来说明有人在补窗口的症状，而不是关窗口。"""
    src = inspect.getsource(rn)
    assert "post-producing flow 还没走完，不能起新 producing 节点" not in src
    assert "不能对 " not in src or "producer 起标准 _reviewer" not in src


def test_the_idling_breaker_is_not_collateral_damage():
    """空转熔断必须**留着** —— 运行时自动执行之后，无限重跑比以前更容易发生。

    删墙不是拆防：这道闸拦的不是"顺序错了"，是"同一件事做了 N 次还没成"。
    """
    src = inspect.getsource(rn)
    assert "_MAX_ACTION_ATTEMPTS" in src
    assert "已经起过" in src and "这是空转不是进展" in src

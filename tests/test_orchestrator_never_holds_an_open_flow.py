"""调度器不会在 flow 未闭合时握有控制权 —— 这条成立，那些墙才是多余的。

## 为什么要有这条测试

2026-08-19 删掉两道墙（「上一个 flow 没走完不许起新 producing 节点」、
「起 _reviewer 的资格门」）时，理由是"它们没有可达触发条件了"。而我在同一轮里
**两次判错**：先漏了 `action_authorized`（人选 REVISE 后运行时没接手执行），
又漏了"授权动作派发失败"。逐条推理不可靠。

所以判据不再是"我论证它不可达"，而是这条**机械不变量**：

    producing 节点交付之后，直到它的 flow 闭合（或人明确 ABORT）之前，
    控制权一次都不回到调度器手里。

墙防的是"调度器在这个窗口里乱起节点"。窗口不存在，墙就没有防守对象；窗口一旦
回来，这条测试先红 —— 那时该做的是把窗口关掉，不是把墙加回来。

## 三个窗口，逐个钉住
"""
from __future__ import annotations

import inspect

import pytest

import core.pause_driver as pd
from shared.tools import run_node as rn


def test_review_and_decision_never_return_control():
    """窗口一：review / 呈递待办。

    两步都在 `run_node(<producing>)` 内部走完，函数返回的是决策 pause ——
    调度器的下一次醒来看到的是人的答复，中间没有它的回合。
    """
    src = inspect.getsource(rn._run_post_producing_flow)
    assert "_run_node_tool(" in src and '"_reviewer"' in src, "reviewer 必须由运行时派"
    assert "_present_decision_package(" in src, "呈递必须由运行时做"

    caller = inspect.getsource(rn._run_node_tool)
    assert "_run_post_producing_flow(" in caller, (
        "这条链必须挂在派发路径上；挂不上就等于没接（机制存在但没接到路径）"
    )


def test_an_authorized_action_is_executed_by_the_runtime():
    """窗口二：人选了 REVISE / REDIRECT。

    运行时在拿到答复的**同一刻**直接起目标节点 —— 那是框架唯一确知"人选了什么"
    且"该起谁"的时刻。交回调度器就等于把排序权还给它。
    """
    src = inspect.getsource(pd._execute_authorized_action)
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

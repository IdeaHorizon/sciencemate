"""节点得知道自己还剩几轮 —— 否则它按「无限」规划，然后被切断在半途。

## 现场（E2E v23，2026-08-11）

experiment 在第 40 轮撞上 `max_turns` 被切断：14 个 LAMMPS 模拟全跑完了，
**MSD 分析一行没做**。它自己在产出说明里写着「达到 max_turns=40 截断，
未做 MSD 分析」—— 也就是说它是**事后**才知道有这个上限的。

`#398` 让这次截断对下游可见（决策层因此会推荐 REVISE 接着做），代价从
「空转三小时」降到「多绕一轮」。但更前面一步没做：**让它一开始就知道有预算**。

一个知道「还剩 8 轮」的节点可以：先把已算出的结果落成产物、把长任务拆成
两轮、或者干脆声明这一轮只做哪一段。一个不知道的节点只能一路往前，然后
在任意位置被剪断 —— 剪在哪儿全看运气。

## 为什么不是「把 40 调大」

调大只是把悬崖往后挪。真正的差别是**模型能不能规划**：预算可见时，撞上限
从"意外"变成"可以提前避开的事"。

（默认值同时从 40 提到 80：40 对 experiment 这种"提交作业 → 等 → 分析"的
节点实测不够用。这个数字是判断，不是推导出来的 —— 写在这里免得后人以为它有
什么依据。）

## 只在快没了的时候说

每轮都播报 = 噪音，模型会开始无视它（今晚 `dreaming_due` 就是这么被无视的）。
按剩余比例分档，越紧越具体。
"""
from __future__ import annotations

import textwrap

import pytest

from core.turn_budget import budget_notice


def test_it_says_nothing_while_there_is_plenty_left() -> None:
    """还早的时候闭嘴 —— 每轮播报会让这条提示失去分量。"""
    assert budget_notice(turn=1, max_turns=80) is None
    assert budget_notice(turn=30, max_turns=80) is None


def test_it_warns_at_half() -> None:
    note = budget_notice(turn=40, max_turns=80)
    assert note and "40" in note


def test_it_gets_specific_when_nearly_out() -> None:
    """快到头时要给**动作**，不是只报数字。"""
    note = budget_notice(turn=76, max_turns=80)
    assert note
    assert "4" in note                      # 还剩几轮
    assert "产物" in note or "落" in note    # 现在就把做完的落下来
    assert "切断" in note                    # 说清撞上去会怎样


def test_an_unbounded_run_never_warns() -> None:
    """`HARNESS_DEFAULT_MAX_TURNS=0` = 不设上限（长 dogfood）—— 没有预算就别提。"""
    assert budget_notice(turn=999, max_turns=0) is None
    assert budget_notice(turn=999, max_turns=None) is None


def test_bad_inputs_stay_quiet() -> None:
    """观察不能打断主流程：算不出来就不说，别抛。"""
    assert budget_notice(turn=None, max_turns=80) is None
    assert budget_notice(turn="x", max_turns="y") is None
    assert budget_notice(turn=5, max_turns=-3) is None




def test_the_hook_is_registered_and_always_on() -> None:
    """接线：光有 `budget_notice` 没人调等于没有（今天第三次踩这个坑）。

    always-on 而不是 opt-in：每个节点都有预算，漏开就是"被切断还不知道
    为什么"；而 opt-in 要逐个改节点 yaml，那 6 个 producing 节点归 owner 管。
    """
    from core import agent_loop, loop_hooks, loop_hooks_builtin  # noqa: F401

    assert "turn_budget" in agent_loop._ALWAYS_ON_HOOKS
    names = {h.name for h in loop_hooks.registered_hooks()} \
        if hasattr(loop_hooks, "registered_hooks") else None
    if names is not None:
        assert "turn_budget" in names




# ── 上限怎么算：真调 resolve_cap，不查源码 ─────────────────────────────────
#
# 同事在 04552d7 里指出过这个毛病：「复刻判据的测试等于自己给自己打分」。
# 本文件第一版有三条是 `inspect.getsource` + 正则 —— 生产代码改不改它们都绿。
# 全部改成真调用。

def test_no_budget_by_default() -> None:
    """默认无上限（2026-08-11 决定）。轮数彻底退出"防空转"。"""
    from core.turn_budget import UNLIMITED, resolve_cap

    assert resolve_cap(mode_turns=0, env_default=None) == UNLIMITED
    assert resolve_cap(mode_turns=0, env_default="") == UNLIMITED
    assert resolve_cap(mode_turns=0, env_default="0") == UNLIMITED


def test_an_explicit_cap_still_wins() -> None:
    """两个逃生口都得留着。"""
    from core.turn_budget import resolve_cap

    assert resolve_cap(mode_turns=0, env_default="120") == 120      # 环境变量
    assert resolve_cap(mode_turns=25, env_default="120") == 25      # 节点 yaml 优先


def test_a_garbage_env_value_does_not_cap_anything() -> None:
    """环境变量写错不该悄悄给所有 run 装个上限。"""
    from core.turn_budget import UNLIMITED, resolve_cap

    assert resolve_cap(mode_turns=0, env_default="abc") == UNLIMITED
    assert resolve_cap(mode_turns=0, env_default="-5") == UNLIMITED


def test_unlimited_gets_a_usable_loop_bound() -> None:
    """`range()` 要个具体的数 —— 但那个哨兵不能被当成"还剩 10 亿轮"。"""
    from core.turn_budget import budget_notice, loop_bound

    bound = loop_bound(0)
    assert bound > 10 ** 6
    assert budget_notice(turn=999, max_turns=0) is None      # 无上限时闭嘴


def test_the_hook_uses_the_effective_cap_not_the_yaml_one() -> None:
    """真跑一遍 hook：yaml 说 0（框架决定），实际生效 80，提示要按 80 算。

    取错来源这条提示就永远算不对 —— 同一个问题两个来源，今晚栽过两次。
    """
    from core.loop_hooks import HookContext
    from core.loop_hooks_builtin import _turn_budget_on_turn_start

    class _Harness:
        max_turns = 0                       # yaml 声明值：0 = "框架决定"

    class _State:
        hook_state = {"_max_turns": 80}     # 实际生效值

    ctx = HookContext(harness=_Harness(), state=_State(), messages=[], turn=78)
    out = _turn_budget_on_turn_start(ctx)
    assert out and "2" in out[0].content, "按 80 算应该报'只剩 2 轮'"

    ctx_early = HookContext(harness=_Harness(), state=_State(), messages=[], turn=3)
    assert _turn_budget_on_turn_start(ctx_early) is None, "还早的时候该闭嘴"

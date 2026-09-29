"""experiment 只能走 safe_* —— 裸 shell 不在它的工具面上。

## 这条测试的来历

2026-07-16 有过一条 runtime directive（test7-14 项目）：

    "experiment 节点：绝对不要使用 run_bash 或 execute_python（非 safe 版本）。
     所有 Python 计算用 safe_execute_python。不要 pip install、不要 subprocess、
     不要 os.system。写文件只写到 /tmp/。"

那是当年**没有机械保证时**用 prompt 求模型自觉的替代品。现在三条全部由构造
保证：裸工具不在白名单（调不到）、`safe_execute_python` 第 0 道边界硬拒
（`/bypass` 也绕不过）、写边界是进程沙箱（spawn 那一刻的墙）。

2026-08-21 记忆重建做旧 directive 认领时，wangd 判定**全部弃掉** ——
理由不是它不重要，是它已经赢了：铁律是红旗（提醒人），沙箱是拒绝
（根本做不到）。把已经硬化的东西再写成一条每轮注入的散文，只会让
reviewer 每次审查都回答一遍"不适用"。

**约束该住在能拒绝它的那一层。** 这个文件就是它现在住的地方：
防的是有人把裸 shell 加回白名单，而不是求模型别调。
"""
from __future__ import annotations

import pytest

from core.bootstrap import bootstrap
from core.loader import load_harness

bootstrap()

#: 裸执行面 —— 有它们等于绕过 safe_* 的全部审计与边界检查。
_RAW = ("run_bash", "execute_python")

# 只钉 experiment。**不推广到别的计算节点** —— 写这条时我顺手把
# data / postprocess 一起断言了，实测立刻打脸：postprocess 工具面上有裸
# `execute_python`，data / postprocess 都没有 `safe_execute_python`。
#
# 那可能是缺口，也可能是它们本来就该那样（比如出图要直接跑 matplotlib）——
# 我没查，所以不判。节点工具面归 owner，一条我没验过就写下的断言只会
# 变成误报，而误报会让人学会忽略红旗。观察记在这里，判决留给 owner。


def test_experiment_has_no_raw_shell():
    tools = set(load_harness("experiment").tools or [])
    leaked = sorted(tools & set(_RAW))
    assert not leaked, (
        f"experiment 的工具面上出现了裸执行工具 {leaked} —— "
        f"它绕过 safe_* 的边界硬拒与高危对账。要跑命令用 safe_run_bash / "
        f"safe_execute_python；确有裸执行的需求请先说明为什么 safe_* 做不到。")


def test_experiment_still_has_the_safe_path():
    """另一半：只测"没有裸的"会让"把执行能力整个删光"也通过。"""
    tools = set(load_harness("experiment").tools or [])
    assert tools & {"safe_execute_python", "safe_run_bash"}, (
        "experiment 连 safe 执行工具都没有了 —— 它还怎么跑计算？")


def test_the_boundary_deny_is_not_bypassable():
    """`pip install` / `subprocess` 这类走**边界硬拒**，不是高危确认。

    区别要紧：高危确认可以被 `/bypass` 放行，边界硬拒不行。当年那条
    directive 想要的正是后者的强度。
    """
    import inspect

    from shared.tools.library import python_exec

    src = inspect.getsource(python_exec)
    boundary_at = src.find("match_boundary_violation")
    bypass_at = src.find("bypass_enabled")
    assert boundary_at > 0, "边界检查不见了"
    assert bypass_at < 0 or boundary_at < bypass_at, (
        "边界硬拒必须在 bypass 判断**之前** —— 否则 /bypass 能绕过写边界")

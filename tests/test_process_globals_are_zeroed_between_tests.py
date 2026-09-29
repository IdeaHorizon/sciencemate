"""进程级全局必须在测试之间归零 —— 尤其是 pause 注册表。

## 现场（2026-08-21，runner 上 py-spy 抓的）

`core.pause._PAUSED_RUNS` 是**模块级 dict**，不在磁盘上 —— `tests/conftest.py`
那道 `HARNESS_FRAMEWORK_HOME` 隔离对它一点用都没有。`clear_all()` 一直存在，但
靠各个测试文件自己记得调（一张手写名单）。

漏一个的代价不是几条红，是**整片挂死**：

1. 残留 pause → `_wait_for_child_progress` 醒在 `pause_pending` 而不是本该的
   理由 → `tests/test_child_wait.py` 一次红 5 条（CI 日志里那 5 个 F 逐条对得上）；
2. `session_driver.next_action` 看到没人认领的 pause 会做孤儿解析 →
   `drive_pause_chain` → `agent_loop.resume_loop` → **单元测试里跑起真 agent
   loop**，py-spy 显示 `active+gil` 卡在 `summarizer.estimate_tokens` →
   pytest 永不退出 → CI 那一步烧掉 12m37s，容器还拆不掉。

只在 `-n 6 --dist loadfile` 下现形：一个 worker 顺序跑很多**文件**却共用一个
**进程**，泄漏跨文件传染；单独跑那个文件 25 次全绿。

## 判据

这里**故意留一个脏**（第一条），再断言下一条看不到它（第二条）。同文件内
执行顺序是确定的，所以这两条永远成对生效 —— 而且它验的是 autouse fixture
真的归了零，不是"我读了一遍 conftest 觉得它会归零"。
"""
from __future__ import annotations

import types

from core import pause as pause_mod

LEAKED = "leaked-run-from-the-previous-test"


def test_a_test_may_leave_a_pause_registered() -> None:
    """第一条：模拟"某个测试注册了 pause 但没清"。"""
    pause_mod._PAUSED_RUNS[LEAKED] = types.SimpleNamespace(
        run_id=LEAKED, parent_run_id=None, node_type="writing",
        project_id=None, question="leaked?", options=None, kind="ask",
    )
    assert LEAKED in pause_mod._PAUSED_RUNS


def test_the_next_test_does_not_inherit_it() -> None:
    """第二条：autouse fixture 必须已经把它清掉了。

    这条红 = conftest 的进程级全局归零漏了 pause 注册表，CI 会开始随机挂死
    在 `tests/test_child_wait.py`（而且现场看起来像"某几条断言 flaky"）。
    """
    assert pause_mod.list_paused() == [], (
        f"上一条测试留下的 pause 漏进来了：{list(pause_mod._PAUSED_RUNS)}。"
        "conftest 的 autouse fixture 必须调 core.pause.clear_all()。"
    )
    assert not pause_mod._ACTIVE_RUNS and not pause_mod._DRIVEN

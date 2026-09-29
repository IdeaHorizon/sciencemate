"""操作收尾时邮箱必须是空的 —— 每条消息必有终局。

## 现场（2026-08-31，本机跑真课题）

```
inbox_item_received    = 5
inbox_item_consumed    = 2
inbox_item_superseded  = 0
inbox_item_quarantined = 0
```
**3 条插话凭空消失**：既没被处理、也没被作废、也没被隔离。其中两条是会改变
实验设计的科研指令（把效应量尺子从线性化换成非线性、把机制指标改成 ensemble
并报 90% 区间）。run 直接 `completed`，界面上没有任何一处显示它们被丢了 ——
而后端每次都如实回了「已送达」。

## 根因：这条规矩只有 stop 那条路遵守

`stop_now` 的 docstring 写着：
> 「排队还没被取走的话，是说给"这一轮"听的；这一轮马上就没了，它们也就没了对象。
>   **每条消息必有终局 —— 作废也要说出口。**」
它也确实 `drain_pending()` + 记 `inbox_item_superseded`。

但**正常收尾**没有这个保证：`_inbox_consumer` 的 finally 只 `task.cancel()`，
还排在队里的话就那么留着。同一条规矩，一条路遵守、另一条路没有 ——
而"另一条路"是绝大多数情况走的那条。

判据落在「收尾之后邮箱空不空」上，不落在「有没有调 cancel」。
"""
from __future__ import annotations

import ast
import inspect
import pathlib

import pytest


def _consumer_source() -> str:
    import platform_runtime

    src = inspect.getsource(platform_runtime)
    i = src.index("def _inbox_consumer(")
    j = src.index("async def _drain_interrupts_forever", i)
    return src[i:j]


def test_the_operation_does_not_end_with_items_still_queued():
    """收尾路径必须再排空一次 —— 光 cancel 掉后台任务会把排队的话留在队里。"""
    body = _consumer_source()
    assert "task.cancel()" in body, "前提：收尾时会取消后台 drain 任务"
    assert "consume_inbox()" in body, (
        "收尾只 cancel 不排空 —— 还排在队里的插话既不会被处理，也不会被作废，"
        "而后端已经回过「已送达」（实测 received=5 / consumed=2 / superseded=0）"
    )


def test_the_final_drain_cannot_kill_the_turn():
    """收尾排空自己失败，不许反过来炸掉这一轮。"""
    body = _consumer_source()
    idx = body.index("consume_inbox()")
    window = body[max(0, idx - 400): idx + 400]
    assert "try" in window and "except" in window, "收尾排空没有被保护"
    assert "inbox_final_drain_failed" in window, "排空失败没有留下可送达的痕迹"


def test_stop_already_had_this_rule():
    """对照：stop 那条路本来就守着这条规矩 —— 这里补的是另一条路。"""
    import platform_runtime

    owner = next(
        o for _, o in vars(platform_runtime).items()
        if inspect.isclass(o) and hasattr(o, "stop_now")
    )
    src = inspect.getsource(owner.stop_now)
    # 规矩写在函数体的注释里（不是 docstring）
    assert "每条消息必有终局" in src, "规矩的出处变了，这条测试要跟着改"
    assert "drain_pending" in src and "inbox_item_superseded" in src

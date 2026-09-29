"""能跑长活的操作，**每一个**都必须有收件箱消费者 —— 这条用扫盘守，不写名单。

## 现场（2026-08-31，本机跑真课题）

人在 experiment 节点跑到一半时插话（一条方法学纠正）。账本读数：

    inbox_item_received    = 1     ← 话收进来了
    inbox_item_consumed    = 0     ← 从来没人取
    user_interrupt_received= 0     ← 待命轮没跑
    inbox_item_superseded  = 0     ← 也没被作废，就那么躺着

节点一路跑到 turn 153 / 1750 万 tokens、写完终态，**全程没见过那句话**。
而后端如实回了「已送达」。

## 根因：消费者的寿命绑在"某一种操作"上

`turn` 包了 `_inbox_consumer()`，`answer` 没包。而人答完一个 pause 之后，
节点就在**那一次 answer 里面**继续跑 —— 长活正发生在没有消费者的那段。

这与 `stop_now` docstring 记的 2026-08-24 事故**同形**（19 次停止、
19 条 received、0 条 consumed）。停止靠"根本不入队"绕开了队列，
**消息还留在队列里**，于是同一个"谁来取"的洞原样落在了消息上。

## 判据为什么是扫盘

补一个 `answer` 的消费者只修了这一个实例；下一个长跑操作照样漏，
而且漏的时候两边都不报错（后端还回 200）。所以判据是：
**凡是开了一次操作（`_operation_start`）的函数，函数体内必须出现
`_inbox_consumer(`。** 新加操作忘了包，这条当场红。
"""
from __future__ import annotations

import ast
import pathlib


def _runtime_source() -> str:
    root = pathlib.Path(__file__).resolve().parents[1]
    return (root / "platform_runtime.py").read_text(encoding="utf-8")


def _functions_that_start_an_operation(src: str) -> dict[str, ast.AST]:
    tree = ast.parse(src)
    found: dict[str, ast.AST] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for sub in ast.walk(node):
            if not isinstance(sub, ast.Call):
                continue
            fn = sub.func
            if isinstance(fn, ast.Attribute) and fn.attr == "_operation_start":
                found[node.name] = node
                break
    return found


def _mentions_inbox_consumer(fn: ast.AST) -> bool:
    for sub in ast.walk(fn):
        if isinstance(sub, ast.Call):
            f = sub.func
            if isinstance(f, ast.Attribute) and f.attr == "_inbox_consumer":
                return True
    return False


def test_the_scan_finds_the_operations_at_all():
    """扫不到东西的护栏等于没有护栏 —— 先证明判据够得着。"""
    ops = _functions_that_start_an_operation(_runtime_source())
    assert len(ops) >= 2, f"没扫到操作入口，判据失效：{sorted(ops)}"
    assert "turn" in ops and "answer" in ops, sorted(ops)


def test_every_operation_has_an_inbox_consumer():
    src = _runtime_source()
    missing = [
        name for name, fn in _functions_that_start_an_operation(src).items()
        if not _mentions_inbox_consumer(fn)
    ]
    assert not missing, (
        f"这些操作能跑长活却没有收件箱消费者：{sorted(missing)} —— "
        "这段时间里人说的话会收下、然后永远躺在队列里（received=1 / consumed=0），"
        "而后端仍回「已送达」"
    )

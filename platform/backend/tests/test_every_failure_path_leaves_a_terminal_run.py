"""任何一条失败路径都必须给 run 一个终态 —— 不是"大部分路径"。

## 现场（2026-08-10 E2E v25）

无人值守跑到 experiment 提交真实模拟，人（我）批准了一次高危操作。之后 LLM
后端返回 **HTTP 503**，harness 会话结束。

    run 状态                running
    最后一条执行事件         run.resumed（17:54）
    之后                    **一个终态事件都没有**
    实际停摆                 2 小时 25 分

`running` 明明在 `REQUIRES_LIVE_RUNTIME_STATUSES` 里 —— 这个分区的**定义**就是
"以有活进程为前提，进程没了它就是尸体"。而没有任何人核对进程还在不在。

## 根因：两条入口路径，只有一条接了记账

`execute_local_turn` 的 `except Exception` 里，`is_answer` 分支原本只在
`HarnessSessionStaleError` 时标状态，别的异常直接 raise —— 而下面那整段
fail-loud 记账（`run_end status=failed`）`is_answer` 走不到。而这次的异常是
`HarnessSessionError: LLM API HTTP 503`，不是 Stale。

**而"回答 pause"正是无人值守审批流最常走的那条路** —— 覆盖漏掉的，恰好是最
没人看着的那条。

## 这些测试为什么重写了（2026-08-12）

原来它们是这么验的：

    source = inspect.getsource(local_execution.execute_local_turn)
    branch = source[source.index("        if is_answer:") : …]
    assert 'run.status = "stale_unknown" if stale else "failed"' in branch

**对着源码字面量断言**，三个毛病：

1. 保持行为不变的重构会把它打红 —— 今天就是：`_sanitized_platform_failure(exc)`
   加了个 `run_id=` 参数，测试立刻失败，而不变量一点没变。
2. 反过来，把代码改成**字面相似但语义损坏**的样子它照样绿（比如把那行赋值
   挪进一个永远不成立的 `if` 里）。
3. 它是"复刻判据"——用测试重写一遍实现，然后确认实现等于自己。同事在
   `04552d7` 里点过这一条：**等于自己给自己打分**。

现在验的是**行为**，而且搬去了全栈脚手架所在的地方：
`test_local_runtime_api.py::test_answering_a_pause_always_leaves_a_terminal_run`
走真实 HTTP 入口 + 真实 `execute_local_turn`，只看库里那条 run 最后是什么。
实现怎么写都行，结果对就行。

本文件只留**不依赖运行时的那部分不变量**：终态分类本身，以及"用户读到的
文案里不能有异常原文"。搭第二套 fixture 去重现同一条路径，只会多一份会各自
演化的脚手架。
"""
from __future__ import annotations

from app.models.execution import REQUIRES_LIVE_RUNTIME_STATUSES, RunStatus


class _NotStale(RuntimeError):
    """非 Stale 的运行时异常 —— 正是 v25 那次 503 的形状。"""


def test_both_outcomes_are_actually_terminal_or_recoverable() -> None:
    """这两个落点必须真的走出 REQUIRES_LIVE_RUNTIME —— 否则等于没修。

    判据取自权威分类，不另写名单：分类改了这条测试自动跟着对。
    """
    for status in (RunStatus.FAILED, RunStatus.STALE_UNKNOWN):
        assert status not in REQUIRES_LIVE_RUNTIME_STATUSES
    # failed 是终态；stale_unknown 刻意不是（它可恢复），但两者都不再声称
    # "有活进程"，这正是这条修复要的。


def test_the_copy_a_user_reads_never_carries_the_raw_exception() -> None:
    """终态里存下的失败说明是**产品文案**，不是异常的 `str()`。

    2026-08-11：一条 SQLAlchemy `IntegrityError`（含整条 INSERT 和全部参数）
    直接出现在会话正文里。原因是这里存的就是 `f"…: {str(exc)}"`。
    """
    from app.services.local_execution import _sanitized_platform_failure

    record = _sanitized_platform_failure(
        _NotStale("[SQL: INSERT INTO execution_events …] token=sk-secret"),
        run_id="run_terminal_probe",
    )
    assert "INSERT INTO" not in str(record["message"])
    assert "INSERT INTO" in str(record["detail"]), "细节得留着，只是不在正文"
    assert record["reference"] == "run_terminal_probe"

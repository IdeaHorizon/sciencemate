"""停下来等人之前必须先落盘。

## 现场（2026-08-10 实测，E2E v25）

experiment 节点跑了 40 分钟、提交了真实 LAMMPS 作业、写出 7 个产物，然后停在
`submit_job` 的高危审批门上等人。两小时没人来（那一轮是无人值守）。进程一没，
源会话 worktree 里是这个样子：

    ?? experiment/artifacts/
    ?? experiment/repro/
    ?? experiment/runtime/
    最近 checkpoint: node(hypothesis): checkpoint   ← experiment 开始之前

恢复机制**工作正常** —— 它从源会话 head 开分支。但 head 停在 experiment
开始之前，于是 30 个产物只接回来 23 个。

## 为什么这是架构缺陷而不是运气不好

checkpoint 的唯一触发点是"节点跑完"（`request_completion_checkpoint`，
由 executor 的收尾路径和 platform_runtime 的完成/失败路径调用）。

而"等人"是一个**无界**等待。把未提交的工作扣在无界等待里，等于把它押在
"这个进程能活到有人回答为止"。最长、最贵、最容易被打断的那个节点
（experiment），恰恰是最不容易走到收尾路径的。

不变量：**任何无界等待之前，先落盘。**
"""
from __future__ import annotations

import inspect

from core import executor


def _pause_branch() -> str:
    """executor 里 `status == "paused"` 那一段。"""
    source = inspect.getsource(executor)
    start = source.index('if loop_result.status == "paused":')
    end = source.index('summary = await finalize_run(', start)
    return source[start:end]


def test_pause_checkpoints_before_it_parks() -> None:
    branch = _pause_branch()
    assert "request_completion_checkpoint" in branch, (
        "暂停前必须请求 checkpoint —— 否则未提交的工作押在'进程能活多久'上"
    )
    assert branch.index("request_completion_checkpoint") < branch.index('"run_paused"'), (
        "落盘要在发出 run_paused 之前：那之后这个 run 就可能随时没了"
    )


def test_checkpoint_failure_does_not_swallow_the_pause() -> None:
    """落盘失败不能反过来把这次暂停搞没。

    暂停是**已经发生**的事实（工具已经拒绝执行并要求审批）。如果 checkpoint
    抛异常就把 run 带崩，等于用一个尽力而为的保护动作，破坏一个必须发生的
    控制流 —— 那比不落盘更糟。
    """
    branch = _pause_branch()
    assert "except Exception" in branch, "checkpoint 失败必须被兜住"
    assert "workspace_checkpoint_failed" in branch, (
        "兜住了也要吵出来：静默吞掉会让'恢复丢工作'再次无人知晓"
    )


def test_completion_path_still_checkpoints() -> None:
    """收尾那次不能被这次替代 —— 暂停之后还会继续写东西。"""
    source = inspect.getsource(executor)
    assert source.count("request_completion_checkpoint") >= 2, (
        "暂停落盘是**增加**一个触发点，不是把收尾那次搬过来"
    )

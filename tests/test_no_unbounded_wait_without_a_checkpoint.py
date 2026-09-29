"""无界等待之前必须先落盘 —— 硬不变量（RFC 异步运行时 D12）。

## 账单

2026-08-10 一次无人值守 E2E 在审批门上静默挂了两小时。那次的代价：experiment
节点跑了 40 分钟、提交了真实作业、产出 7 个产物，进程一没，
`experiments/artifacts/`、`repro/`、`runtime/` 全是 untracked —— 恢复从源会话
head 开分支，而 head 还停在 experiment 开始**之前**那次 checkpoint 上。全丢。

根因是 checkpoint 只挂在"跑完"上，而最长、最贵、最容易被打断的节点恰恰是最
不容易跑完的那个。

## 为什么这条要单独写成不变量

「等人」不是"还没跑完"，是一段**无界**的等待。把未提交的工作扣在里面，等于把
它押在"这个进程能活到有人回答"上。

节点级 pause（`core/executor.py`）2026-08-11 就这么做了。而平台会话自己的
pause（`platform_runtime._operation_end`）当时写的是 `if status != "paused"`
—— 恰好把最该落盘的那一种排除在外。**同一个问题两处给出相反的答案，而没有
任何一层会报错。** 这个文件就是那道钉子。
"""
from __future__ import annotations

import io
import json
import subprocess
from pathlib import Path

import pytest

from core.llm import LLMResponse
from platform_runtime import serve_jsonl

from tests.test_platform_runtime import _FakeLLM


#: orchestrator 拥有的目录 —— 问 `core/project_workspace._NODE_WORKSPACES`，不抄。
#: 落盘只在**有脏路径**时发生，所以测试必须让它真的脏 —— 否则三条测试全是
#: 空转，而空转的绿看起来和真的一模一样。
from core.project_workspace import _NODE_WORKSPACES as _WORKSPACES

_ORCHESTRATOR_WORKSPACE = _WORKSPACES["_orchestrator"]


def _asks(question: str) -> LLMResponse:
    """一次真的 pause：`request_human_input` 是平台侧停下来等人的那条路。"""
    return LLMResponse(
        content=None,
        tool_calls=[{
            "id": "call_pause_1", "type": "function",
            "function": {
                "name": "request_human_input",
                "arguments": json.dumps(
                    {
                        "question": question,
                        "context": "需要人类确定边界",
                        "options": ["窄范围", "宽范围"],
                        "recommended_option_index": 0,
                    },
                    ensure_ascii=False,
                ),
            },
        }],
        finish_reason="tool_calls",
        usage={"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
    )


async def _turn_that_pauses(tmp_path: Path) -> list[dict]:
    """真跑一轮，让它停在一个问题上，返回它发出的全部事件。

    ⚠️ 必须绑一个**真的** Git 工作区：`request_completion_checkpoint` 在没有
    工作区时如实返回 None，什么都不发。不绑的话这三条测试全是空转 —— 而空转
    的绿看起来和真的一模一样。
    """
    worktree = tmp_path / "wt"
    worktree.mkdir(parents=True, exist_ok=True)
    for command in (
        ["git", "init", "-q"],
        ["git", "config", "user.email", "cp@test"],
        ["git", "config", "user.name", "cp"],
        ["git", "commit", "-q", "--allow-empty", "-m", "init"],
    ):
        subprocess.run(command, cwd=worktree, check=True)
    # 让 orchestrator 的工作区**真的脏**：没有脏路径就没有 checkpoint 可请求
    # （`workspace_snapshot` 如实返回 None），那样这些断言测的是空气。
    owned = worktree / _ORCHESTRATOR_WORKSPACE
    owned.mkdir(parents=True, exist_ok=True)
    (owned / "in-flight.md").write_text("干到一半的活\n", encoding="utf-8")

    llm = _FakeLLM([_asks("要用哪个数据集？")])
    requests = [
        {
            "op": "init", "request_id": "init-cp",
            "tenant_id": "tenant-test", "project_id": "project-cp",
            "session_id": "session-cp",
            "home_dir": str(tmp_path / "isolated-home"),
            "workspace_dir": str(worktree),
        },
        {"op": "turn", "request_id": "turn-cp", "message": "开始"},
    ]
    stream = io.StringIO(
        "".join(json.dumps(request, ensure_ascii=False) + "\n" for request in requests)
    )
    events: list[dict] = []
    await serve_jsonl(
        stream,
        lambda event_type, **payload: events.append({"type": event_type, **payload}),
        llm=llm,
    )
    return events


def _transcript_order(events: list[dict]) -> list[str]:
    """事件流里 transcript 记录的先后顺序（按名字）。"""
    return [
        str((event.get("event") or {}).get("event") or "")
        for event in events
        if event.get("type") == "transcript"
    ]


@pytest.mark.asyncio
async def test_pausing_for_a_human_checkpoints_first(tmp_path: Path) -> None:
    """停下来等人之前，先把干出来的活落盘。"""
    events = await _turn_that_pauses(tmp_path)
    order = _transcript_order(events)
    assert "platform_request_end" in order, f"这一轮没有正常收尾：{order}"
    assert "workspace_checkpoint_requested" in order, (
        "停在问题上却没有请求落盘 —— 未提交的工作被押在一个无界等待里"
        f"（transcript：{order}）"
    )


@pytest.mark.asyncio
async def test_the_checkpoint_happens_before_the_question_reaches_anyone(
    tmp_path: Path,
) -> None:
    """顺序要紧：落盘在 `pause_required` **之前**。

    反过来的话，人可以在事实进 Git 之前就答复，而那一瞬间进程要是没了，
    答复指向的东西不存在。
    """
    events = await _turn_that_pauses(tmp_path)
    checkpoint_at = next(
        (index for index, event in enumerate(events)
         if event.get("type") == "transcript"
         and (event.get("event") or {}).get("event") == "workspace_checkpoint_requested"),
        None,
    )
    pause_at = next(
        (index for index, event in enumerate(events) if event.get("type") == "pause_required"),
        None,
    )
    assert checkpoint_at is not None and pause_at is not None, (
        f"缺了其中一个：checkpoint={checkpoint_at} pause={pause_at}"
    )
    assert checkpoint_at < pause_at, (
        "问题先送到人手里，落盘在后 —— 中间那一瞬进程没了，答复就指向一个"
        "不存在的东西"
    )


def test_no_platform_code_branches_on_not_being_paused() -> None:
    """扫盘：平台侧不许再出现"把等人排除在外"那种分支。

    判据落在**那个比较**上，不是某个函数名：`status != "paused"` 这种写法
    正是 8-10 那次的形状。它一旦回来，上面两条行为测试要等到有人真跑出一次
    pause 才会红；这条当场红。

    走 AST 不走字符串：注释和 docstring 里要讲得了这段历史（这个文件和
    `platform_runtime` 里都写着它），而字面匹配分不出"文档里提到"和"代码里
    用了"——第一版就是这么写的，自己的注释把自己判红了。
    """
    import ast
    import inspect

    import platform_runtime as pr

    offenders: list[int] = []
    for node in ast.walk(ast.parse(inspect.getsource(pr))):
        if not isinstance(node, ast.Compare) or len(node.ops) != 1:
            continue
        if not isinstance(node.ops[0], ast.NotEq):
            continue
        left, right = node.left, node.comparators[0]
        names = {
            getattr(side, "id", None) for side in (left, right)
        }
        values = {
            side.value for side in (left, right)
            if isinstance(side, ast.Constant)
        }
        if "status" in names and "paused" in values:
            offenders.append(node.lineno)
    assert not offenders, (
        f"又出现了把 paused 排除在外的分支（行 {offenders}）—— 等人是无界的，"
        "未提交的工作不许押在它上面"
    )

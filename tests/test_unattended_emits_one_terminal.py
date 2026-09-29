"""无人值守：一次调用只有一个终止事件，且所有事件挂在同一个 request_id 上。

## 为什么这条不变量值钱

App Server 的 `_rpc_locked` 按 `request_id` 过滤事件，见到第一个 `result`
就返回（`platform/backend/app/services/harness_sessions.py`）。而 `run_unattended`
内部反复调 `turn()`，`turn()` 每轮都会走 `_operation_end` 发一个 `result`。

两件事撞在一起会产生平台最不能有的东西：**后端在第 1 轮就认为整件事做完了，
子进程却还在往下跑** —— 一个子进程两个驱动者，各自往同一个 Git worktree 写。

反过来，如果内层各用各的 request_id 来规避重复终止，后端会把内层的**进度
事件**一并过滤掉，用户在 UI 上看到的是一个几小时不动的空白。

所以只有一种正确接法：内层共用外层 request_id + 终止事件只发一次。

## 这个文件为什么重写了（2026-08-12）

原来它是**源码检视**的：`ast.parse(platform_runtime.py)` 之后
`ast.walk` 找 `emit(...)` 调用、`ast.unparse` 出来做字符串匹配。

那种写法今晚把 CI 变成了"看起来卡住"：

    FAILED tests/test_unattended_emits_one_terminal.py::
           test_inner_turns_reuse_the_outer_request_id
    SystemError: AST constructor recursion depth mismatch (before=117, after=134)
    /usr/lib/python3.11/ast.py:50

`ast.unparse` 是递归的，`run_unattended` 是个大函数，CPython 3.11 在递归深度
账目上有这个已知问题 —— 而它**跟当时剩余的递归余量有关**，所以同一份代码
时红时绿（`dcbbb0a` 红、`cb1b3b5` 红、`8dad2f6` 绿）。合并脚本看到红就拒绝
合并，我在外面看到的现象是"CI 一直不过"。

三个毛病，和今晚拆掉的其它源码检视测试是同一个（同事在 `04552d7` 点过：
复刻判据等于自己给自己打分）：

1. 它把**实现的写法**冻在测试里，保持行为不变的重构会打红它；
2. 反过来，写法相似但语义损坏照样绿（把 emit 挪进一个永不成立的分支）；
3. 它自己会因为解析器的问题而失败 —— 一个测不到被测系统的失败。

现在真跑 `serve_jsonl`，发一条 `run_unattended`，看**事件流**：
恰好一个 `result`、全部事件同一个 request_id。实现怎么写都行，结果对就行。
"""
from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from core.llm import LLMResponse
from platform_runtime import serve_jsonl

from tests.test_platform_runtime import _FakeLLM


def _done(text: str) -> LLMResponse:
    return LLMResponse(
        content=text, tool_calls=[], finish_reason="stop",
        usage={"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
    )


async def _run_unattended(tmp_path: Path, *, turns: int) -> list[dict]:
    """真跑一次无人值守，返回它发出的全部事件。"""
    llm = _FakeLLM([_done(f"第 {i + 1} 轮：继续。") for i in range(turns + 2)])
    requests = [
        {
            "op": "init",
            "request_id": "init-un",
            "tenant_id": "tenant-test",
            "project_id": "project-un",
            "session_id": "session-un",
            "home_dir": str(tmp_path / "isolated-home"),
        },
        {
            "op": "run_unattended",
            "request_id": "unattended-1",
            "message": "自己跑到做完为止",
            "max_turns": turns,
        },
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


@pytest.mark.asyncio
async def test_exactly_one_terminal_result_per_unattended_call(tmp_path: Path) -> None:
    """终止只能有一个。

    多发一个，后端就会在第 1 轮认为整件事做完并返回 —— 而子进程还在往下跑，
    同一个 Git worktree 从此有两个驱动者。
    """
    events = await _run_unattended(tmp_path, turns=3)

    terminal = [
        event for event in events
        if event["type"] == "result" and event.get("request_id") == "unattended-1"
    ]
    assert len(terminal) == 1, (
        f"`unattended-1` 收到 {len(terminal)} 个终止事件："
        f"{[e.get('data', {}).get('status') for e in terminal]}"
    )


@pytest.mark.asyncio
async def test_inner_turns_reuse_the_outer_request_id(tmp_path: Path) -> None:
    """内层不许自己造 request_id —— 造了，进度事件就被后端过滤掉。

    后端只转发与本次调用 request_id 相同的事件。内层另起一个 id，用户在 UI 上
    看到的就是一个几小时不动的空白。
    """
    events = await _run_unattended(tmp_path, turns=3)

    # 窗口 = **init 之后的一切**。
    #
    # 第一版我写的是「第一条 unattended-1 → 最后一条」—— 那是**用被测的东西
    # 自己定义窗口**，循环论证：内层真去自造 id 时，脏事件恰好落在第一条
    # `unattended-1` 之前，窗口把它们整段排除掉，测试照样绿。变异验证抓到了
    # 这一点（把内层改成 `request_id + "-inner"`，10 条脏事件，测试不红）。
    #
    # `init` 是另一次调用，它自己的 ready/transcript 本来就该带自己的 id ——
    # 那是唯一的合法例外，而且它的边界是确定的：init 的最后一条事件。
    last_init = max(
        i for i, e in enumerate(events) if e.get("request_id") == "init-un"
    )
    stray = sorted({
        event.get("request_id") for event in events[last_init + 1:]
        if event.get("request_id") not in (None, "unattended-1")
    })
    assert not stray, f"无人值守期间出现了别的 request_id：{stray}，它们会被后端过滤掉"


@pytest.mark.asyncio
async def test_the_suppression_is_reset_even_when_a_turn_raises(tmp_path: Path) -> None:
    """内层压制必须在异常路径上也复位。

    压制没复位 = 之后**每一次** turn 都不发终止事件，后端就永远等下去。
    源码检视测的是"有没有写 finally"；这里测的是**结果**：炸过一次之后，
    下一次普通 turn 照样收得到自己的 result。
    """
    llm = _FakeLLM([_done("第一轮"), _done("炸之后的一轮")])

    async def explode(*_args, **_kwargs):
        raise RuntimeError("上游炸了")

    requests = [
        {
            "op": "init",
            "request_id": "init-un2",
            "tenant_id": "tenant-test",
            "project_id": "project-un2",
            "session_id": "session-un2",
            "home_dir": str(tmp_path / "isolated-home"),
        },
        {"op": "run_unattended", "request_id": "un-boom", "message": "跑", "max_turns": 1},
        {"op": "turn", "request_id": "turn-after", "message": "再来一轮"},
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

    terminals = {
        event.get("request_id")
        for event in events
        if event["type"] in ("result", "error")
    }
    assert "turn-after" in terminals, (
        f"无人值守之后的普通 turn 没有终止事件，后端会一直等：{sorted(terminals)}"
    )


@pytest.mark.asyncio
async def test_every_dispatchable_op_is_named_in_the_error(tmp_path: Path) -> None:
    """机制写好了没接到分发表 = 平台调不到它；合法值不送到调用方 = 只能猜。

    实测（2026-08-10）：`run_unattended` 早就存在，但从未出现在 `--serve` 的
    op 分发里，也没有任何后端代码调用过它 —— 平台因此从来没有无人值守驱动，
    每一轮都要外部脚本推。这条就是防它再掉线。

    ## 判据从"扫源码"改成"真发一条非法 op 看它怎么答"（2026-08-23）

    原来这条是拿正则从源码里抠 `op == "x"` 和那句报错文案再比对。P1-1 之后
    分发表变成了两个集合、报错文案由集合**现拼** —— 于是正则什么也抠不到，
    测试红了，而被测的性质其实变得更强了（清单再也不可能和分发表分叉）。

    真发一条非法 op：既验了合法值清单送得到调用方，也验了它**确实等于**
    分发表本身。
    """
    from platform_runtime import _CONVERSATION_OPS, _MANAGEMENT_OPS, serve_jsonl

    stream = io.StringIO(
        json.dumps({"op": "no_such_op", "request_id": "bad-1"}, ensure_ascii=False) + "\n"
    )
    events: list[dict] = []
    await serve_jsonl(stream, lambda event_type, **p: events.append({"type": event_type, **p}))

    error = next(e for e in events if e["type"] == "error")
    assert error["code"] == "invalid_operation"
    missing = sorted(
        op for op in _CONVERSATION_OPS | _MANAGEMENT_OPS if op not in error["message"]
    )
    assert not missing, (
        f"这些 op 分发表里有、报错的合法值清单里没有：{missing} —— "
        "调用方只能猜（契约必须送到调用方）"
    )
    assert "run_unattended" in error["message"], "无人值守又从分发表上掉线了"

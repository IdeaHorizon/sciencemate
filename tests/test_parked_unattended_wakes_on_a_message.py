"""停靠中的无人值守被一句话叫醒 —— `min(定时器, 有人说话)`。

## 现场（wangd 2026-08-21）

一条 unattended run 报了 blocked（writing 被一道门禁挡住），于是**停靠**并按
小时退避复查：30min → 2h → **4h**。早上用户问「怎么样了？」——

- 那句话确实被取走了（drain 循环 1 秒一轮，一直在跑）；
- 但循环本身停在 `await asyncio.sleep(14400)` 上，**不可中断**。

停靠的语义从来就是「最长别叫我」，不是「必须睡够」（`chat.py` 里那句注释的
原文）。语义早就写对了，只有那一行 sleep 没照做。

顺带被拖住的还有**停止**：停止的时延上界本该由我方节拍定（1s 邮箱 + 0.5s
生成侧，PR#556），而这行 sleep 把它变成"最长等到下一个复查点"。CLI 那边的
退避是 1 秒一跳、每跳查一次 `_continuous_running`；平台这边没有 —— 同一份
策略，两个前端的 I/O 层各判各的。

## 判据

不看源码写法（`ast` 检视那套的教训就在隔壁文件里），真跑 `serve_jsonl`：
让策略层要求一段长退避，在停靠期间**从命令面**送一句话进去，看**墙钟**。

2026-08-23（P1）：投递面从文件收件箱换成了 socket 命令，所以这里也换 ——
而且换过来之后这条测试更值钱了：它现在同时验着"管理面能不能在一轮飞行中
被受理"（P1-1 的全部内容）。
"""
from __future__ import annotations

import asyncio
import io
import json
import subprocess
import time
from pathlib import Path

import pytest

from core.llm import LLMResponse
from platform_runtime import RequestSource, serve_jsonl

from tests.test_platform_runtime import _FakeLLM

#: 停靠时长。取得足够大：真睡满了测试会超时，不会"碰巧也通过"。
_PARK_S = 3600.0


class _QueuedSource(RequestSource):
    """一个可以**在跑轮中继续投喂**的命令面。

    从前这里是 `io.StringIO` —— 一次性把所有请求摆好，读完就 EOF。那种源
    表达不了本测试现在要验的事：一轮正在飞的时候，**又来了一条命令**。
    P1-1 之前它也确实表达不了，因为请求循环那时是串行的。
    """

    def __init__(self, initial: list[dict]) -> None:
        self._queue: asyncio.Queue[str | None] = asyncio.Queue()
        for request in initial:
            self._queue.put_nowait(json.dumps(request, ensure_ascii=False) + "\n")

    def send(self, request: dict) -> None:
        self._queue.put_nowait(json.dumps(request, ensure_ascii=False) + "\n")

    def eof(self) -> None:
        self._queue.put_nowait(None)

    async def readline(self, limit: int) -> str:
        line = await self._queue.get()
        return line if line is not None else ""


def _done(text: str) -> LLMResponse:
    return LLMResponse(
        content=text, tool_calls=[], finish_reason="stop",
        usage={"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
    )


async def _drive(
    tmp_path: Path, *, deposit_after_s: float | None, deposit_kind: str = "message",
) -> tuple[list[dict], float]:
    """真跑一次会停靠的无人值守；可选地在停靠期间往收件箱投一件。

    返回 (事件流, 墙钟耗时)。
    """
    from core import session_driver

    # 真的 Git 工作区：`workspace_dir` 要过 `git rev-parse --show-toplevel`，
    # 而收件箱就落在它下面。假目录会让 init 直接报 ProjectWorkspaceError ——
    # 那样测的是"init 失败"，不是停靠。
    worktree = tmp_path / "wt"
    worktree.mkdir(parents=True, exist_ok=True)
    for command in (
        ["git", "init", "-q"],
        ["git", "config", "user.email", "park@test"],
        ["git", "config", "user.name", "park"],
        ["git", "commit", "-q", "--allow-empty", "-m", "init"],
    ):
        subprocess.run(command, cwd=worktree, check=True)

    # 策略层要求一段长退避 —— 这正是 blocked 停靠的形状。这里不去真的触发
    # blocked 判定（那是策略层的事，另有测试），只固定它的**输出**：
    # 「等 _PARK_S 再问我一次」。被测的是等待这段路怎么走。
    from core.session_driver import SessionAction

    calls = {"n": 0}

    async def parked_next_action(state, reply, *, reason: str = "", **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return SessionAction(
                kind="prompt", prompt="复查：还堵着吗？",
                delay_s=_PARK_S, reason="blocked_parked",
            )
        # 醒来之后直接收尾：真策略层的判断另有测试，这里只验"醒没醒"。
        return SessionAction(kind="stop", reason="test_done")

    llm = _FakeLLM([_done(f"第 {n + 1} 轮") for n in range(6)])
    requests = [
        {
            "op": "init", "request_id": "init-p",
            "tenant_id": "tenant-test", "project_id": "project-p",
            "session_id": "session-p",
            "home_dir": str(tmp_path / "isolated-home"),
            # ⚠️ 字段名是 `workspace_dir`。写成 `project_worktree` 会被静默忽略
            # → `state.project_worktree` 是 None → drain 循环开头就 return，
            # 收件箱永远没人取。第一版就是这么写的，现象是"投递成功但没人醒"。
            "workspace_dir": str(worktree),
        },
        {
            "op": "run_unattended", "request_id": "un-p",
            "message": "自己跑", "max_turns": 2,
        },
    ]
    source = _QueuedSource(requests)
    events: list[dict] = []

    async def _deposit_later():
        await asyncio.sleep(deposit_after_s)
        if deposit_kind == "stop":
            source.send({"op": "stop", "request_id": "stop-1", "author": "tester"})
        else:
            source.send({
                "op": "interject", "request_id": "int-1",
                "text": "怎么样了？", "author": "tester", "message_id": "m-1",
            })

    import pytest as _pytest
    monkey = _pytest.MonkeyPatch()
    monkey.setattr(session_driver, "next_action", parked_next_action)
    # `run_unattended` 从 `core.session_driver` 里 import 到局部名字，patch 模块
    # 属性即可（它是函数内 import）。
    started = time.monotonic()
    try:
        def _collect(event_type, **payload):
            events.append({"type": event_type, **payload})
            # 这一趟收尾了就关命令面 —— 队列式的源不会自己 EOF（那正是它
            # 存在的理由：跑轮中还得能再投一条）。不关的话 serve 会一直等
            # 下一条命令，测试挂死在"什么都没发生"上。
            if event_type == "result" and payload.get("request_id") == "un-p":
                source.eof()

        tasks = [serve_jsonl(source, _collect, llm=llm)]
        if deposit_after_s is not None:
            tasks.append(_deposit_later())
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=60)
    finally:
        monkey.undo()
    return events, time.monotonic() - started


@pytest.mark.asyncio
async def test_a_message_wakes_the_parked_loop_instead_of_waiting_out_the_hour(
    tmp_path: Path,
) -> None:
    """8-21 回放：停靠 1 小时，2 秒后有人说话 → 必须立刻醒，不是等满。"""
    events, elapsed = await _drive(tmp_path, deposit_after_s=2.0)

    assert elapsed < 30, (
        f"停靠没被叫醒，墙钟 {elapsed:.1f}s —— 这正是 8-21 那 4 小时的形状"
    )
    woke = [
        e for e in events
        if e.get("type") == "transcript"
        and "unattended_wake_early" in json.dumps(e, ensure_ascii=False)
    ]
    assert woke, (
        "醒是醒了，但没有留下「被提前叫醒」的记录 —— 事后无从判断它是"
        "睡满了还是被叫醒的"
    )


@pytest.mark.asyncio
async def test_stop_during_park_is_not_deferred_to_the_next_probe(
    tmp_path: Path,
) -> None:
    """停止的时延上界由我方节拍定，不由停靠间隔定（PR#556 语义不许被绕过）。"""
    events, elapsed = await _drive(
        tmp_path, deposit_after_s=2.0, deposit_kind="stop")
    assert elapsed < 30, f"停止被拖到下一个复查点，墙钟 {elapsed:.1f}s"
    assert any(
        e.get("type") == "transcript"
        and "user_stop_received" in json.dumps(e, ensure_ascii=False)
        for e in events
    ), "停止信号没在停靠期间被处理"


@pytest.mark.asyncio
async def test_nobody_talking_still_parks(tmp_path: Path) -> None:
    """反向：没人说话时它**确实**在等 —— 别把「可唤醒」做成「不再停靠」。

    少了这条，把 `_park_until_woken` 改成直接 return 也能让上面两条全绿，
    而那样停靠退避就等于不存在（故障期间高速烧请求正是它要防的）。
    """
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(_drive(tmp_path, deposit_after_s=None), timeout=8)

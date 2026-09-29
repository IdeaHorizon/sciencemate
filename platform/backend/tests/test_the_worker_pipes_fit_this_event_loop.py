"""worker 的三根管道，必须是**本平台的事件循环接得上的那一种**。

## 现场（2026-09-22，同事的 Windows 机器，0.5.x 桌面版）

每一轮都失败，界面上就一句 `[WinError 6] 句柄无效` 加一个 run reference；
而「模型连接检测」是通过的 —— 那一步只是后端自己发一次 HTTP，不起 worker。

真因在 spawn：Windows 的事件循环是 proactor，它把管道**注册进 IOCP**
（`CreateIoCompletionPort(obj.fileno(), …)`，`asyncio/windows_events.py`），
要的是 overlapped 打开的管道 **HANDLE**；而 `subprocess.Popen` 的
`.stdin/.stdout/.stderr` 是架在 CRT **fd** 上的文件对象。fd 当 HANDLE 递进去，
Windows 当场 `[WinError 6]`。真机最小复现（cpython 3.12.14，proactor）：

    File "…/asyncio/windows_events.py", line 483, in recv
        self._register_with_iocp(conn)
    File "…/asyncio/windows_events.py", line 709, in _register_with_iocp
        _overlapped.CreateIoCompletionPort(obj.fileno(), self._iocp, 0, 0)
    OSError: [WinError 6] The handle is invalid

`connect_write_pipe` 那一步是**同步抛**的，所以 spawn 直接失败、整轮失败。

PR#1040（「孩子归操作系统，不归事件循环」）把 `asyncio.create_subprocess_exec`
换成裸 `subprocess.Popen`，要换掉的是**谁拥有这个孩子**；顺手丢掉的却是
`asyncio.windows_utils.Popen` 那一层 —— 在 Windows 上，管道长什么样和谁拥有
孩子是两件事，而那次只想着后者。POSIX 上两者恰好没区别，于是全绿。

## 这里钉的两条

1. **真跑一次**：走生产那个 `_spawn_child_owned_by_the_os`，往孩子的 stdin 写、
   从 stdout/stderr 读回来。两个平台都跑 —— 这正是修复前 Windows 上红、
   POSIX 上绿的那条判据，不加 `skipif`（skip 掉的闸等于没有闸）。
2. **一个出处**：交给事件循环的管道只许由 `_popen_with_pipes_this_loop_can_read`
   生产。名单会漏，扫盘不会。

> 这两条都只在**有人在 Windows 上跑测试**时才说话。发行前的 Windows 验收
> （`scripts/acceptance/personal_smoke.py --url`，它一路走到收到一条回复）
> 才是把它们跑起来的那只手 —— 0.5.x 那次没跑，于是「装完 health 200」放行了
> 一个每轮必崩的包。
"""

from __future__ import annotations

import ast
import asyncio
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from app.services.harness_sessions import (
    _popen_with_pipes_this_loop_can_read,
    _spawn_child_owned_by_the_os,
)

BACKEND_DIR = Path(__file__).resolve().parents[1]

#: 孩子：把 stdin 的每一行原样回给 stdout，并先在 stderr 上说一句。
#: 协议那头也是这个形状（按行、无缓冲），所以这条测试量的就是真东西。
CHILD = textwrap.dedent(
    """
    import sys
    sys.stderr.write("CHILD-STDERR\\n"); sys.stderr.flush()
    for line in sys.stdin:
        sys.stdout.write(line.upper()); sys.stdout.flush()
    """
)


@pytest.mark.asyncio
async def test_a_spawned_child_can_be_written_to_and_read_from(tmp_path):
    """三根管道都得真的通。Windows 上修复前死在 `connect_write_pipe`（WinError 6）。"""
    child = await _spawn_child_owned_by_the_os(
        [sys.executable, "-u", "-c", CHILD],
        cwd=str(tmp_path),
        env={**_minimal_env()},
        limit=64 * 1024,
    )
    try:
        # 行尾两个平台不一样（Windows 是 CRLF）—— 这里问的是"通不通"，不是行尾。
        first = await asyncio.wait_for(child.stderr.readline(), 20)
        assert first.strip() == b"CHILD-STDERR"
        child.stdin.write(b"ping\n")
        await child.stdin.drain()
        assert (await asyncio.wait_for(child.stdout.readline(), 20)).strip() == b"PING"
    finally:
        # 放手 = 只关我们这头；孩子读到 EOF 自己退场（两个平台同一个结果）。
        child.close_pipes()
        assert await asyncio.wait_for(child.wait(), 20) == 0


def _minimal_env() -> dict:
    """孩子只要能起一个 python —— Windows 上 `SYSTEMROOT` 缺了就起不来。"""
    keep = ("SYSTEMROOT", "PATH", "TEMP", "TMP", "PATHEXT", "COMSPEC")
    return {name: os.environ[name] for name in keep if name in os.environ}


def test_the_spawned_pipes_have_exactly_one_source() -> None:
    """扫盘：递给事件循环的管道只许来自一个地方，而那个地方分平台。

    判据不是"有没有 `windows_utils`"（那是写法），是"**谁**造的这些管道"：
    只要 `connect_read_pipe` / `connect_write_pipe` 收到的东西出自
    `_popen_with_pipes_this_loop_can_read`，平台差异就只剩它一处要回答。
    """
    module = BACKEND_DIR / "app/services/harness_sessions.py"
    tree = ast.parse(module.read_text(encoding="utf-8"))

    connectors = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        and node.func.attr in ("connect_read_pipe", "connect_write_pipe")
    ]
    assert len(connectors) == 2, (
        f"接管道的调用点从 2 个变成了 {len(connectors)} 个 —— 新的那个的管道从哪来？"
    )

    # 每个调用点的第二个实参就是那根管道。它必须是**变量**（上游那个 popen 的
    # 属性/形参），不能是在这里现造的东西 —— 现造就意味着又有了一个出处。
    pipes = [call.args[1] for call in connectors]
    assert all(isinstance(pipe, (ast.Name, ast.Attribute)) for pipe in pipes), (
        f"有人在接管道的地方现造管道：{[ast.dump(pipe)[:60] for pipe in pipes]}"
    )

    # 唯一的生产者：模块里除了它，没有第二处 `subprocess.Popen(`。
    popens = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "Popen"
    ]
    maker = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and node.name == "_popen_with_pipes_this_loop_can_read"
    )
    inside = {id(node) for node in ast.walk(maker)}
    outside = [node.lineno for node in popens if id(node) not in inside]
    assert outside == [], (
        f"{module.name} 的 {outside} 行自己起了进程 —— 管道的形状又有了第二个答案"
    )


def test_the_maker_answers_the_platform_question() -> None:
    """生产者本身必须**分平台**回答，而不是两个平台共用一条路。

    这一条是 POSIX 侧的代偿：上面那条真跑的判据在这台机器上无论如何都绿
    （POSIX 根本 import 不进 `windows_utils`），所以把"win32 那一支还在不在"
    单独钉出来。它扫的是写法，抓不住写错的 win32 分支 —— 那个只有真在
    Windows 上跑上面那条才抓得住。
    """
    source = (BACKEND_DIR / "app/services/harness_sessions.py").read_text(encoding="utf-8")
    body = source.split("def _popen_with_pipes_this_loop_can_read")[1].split("\nasync def ")[0]
    assert 'sys.platform == "win32"' in body, "生产者没有分平台 —— Windows 会拿到 fd 当 HANDLE 用"
    assert "windows_utils" in body, "Windows 那一支没有走 overlapped 管道"


def test_the_posix_path_is_still_a_plain_popen(tmp_path) -> None:
    """POSIX 上它就是 `subprocess.Popen` 本身 —— 「孩子归操作系统」没被换掉。"""
    if sys.platform == "win32":
        pytest.skip("这一条问的是 POSIX 那一支")
    popen = _popen_with_pipes_this_loop_can_read(
        [sys.executable, "-c", "pass"], cwd=str(tmp_path))
    try:
        assert type(popen) is subprocess.Popen
    finally:
        popen.kill()
        popen.wait(timeout=20)

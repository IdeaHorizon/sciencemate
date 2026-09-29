"""没人来接手的 worker 得自己收摊 —— **包括从来没人连上过的那种**。

## 病例（2026-09-07 真机）

打包自检跑完（起后端 → 建项目 → 真调一次模型 → 拿到回复 → 后端退出），留下一个
worker 进程。7 分钟后它还在：

    PID 23711  ELAPSED 07:26  TIME 0:02.15  STAT Ss
    fd 7 = 监听 socket，对端 ->(none)      ← 没有任何人连着

对照实验（起一个 worker，**从不连它**）：105 秒后仍在，CPU 0.11s。宽限期是 60 秒。

## 根因：闸只在"有人连过又走了"时武装

`watcher_gone_since()` 对"从来没人连上过"额外答 `None`，理由是握手在路上。但
`_NO_WATCHER_GRACE_S = 60` 已经比那个几百毫秒的握手窗口宽两个数量级 —— 那条特例
挡掉的不是误杀，是**真事**：后端 spawn 了 worker 却从没连上来（自检跑完就退、后端
起 worker 后自己崩了、握手前被杀），于是宽限期永远不武装，worker 堵在 `accept()`
上永远不收摊。

同一个字段还有第二份答案：`_alone_since` 在 `__init__` 里就置了 `time.monotonic()`，
它自己的注释写着"刚绑好还没人接进来也算没人看"。**一个问题两份答案，分叉时两边都
不报错。**（[[feedback-one-truth-source-per-question]]）

## 与 #835 的关系

#835 修的是"用户退出应用后 worker 还在**花钱**"。这条是同一个判据的另一半：进程
泄漏（不烧钱、不占 CPU，纯堵在 accept 上）。#835 的宽限期逻辑是对的，只是**够不
到**这条路径。
"""
from __future__ import annotations

import asyncio
import json
import time

import pytest

from platform_runtime import SocketRequestSource, TcpLoopbackRequestSource
import socket


@pytest.fixture
def source(tmp_path):
    src = (SocketRequestSource(tmp_path / "t.sock", on_connect=lambda _w: None)
           if hasattr(socket, "AF_UNIX") else TcpLoopbackRequestSource("127.0.0.1", 0, spawn_token="test", on_connect=lambda _w: None))
    yield src
    src.close()


def test_a_worker_nobody_ever_connected_to_counts_as_unwatched(source) -> None:
    """核心判据：从没人连过 → 也要报"没人看"，而且从**绑好那一刻**起算。

    以前这里答 `None`（= 有人在看），宽限期永远不武装。
    """
    alone_since = source.watcher_gone_since()

    assert alone_since is not None, (
        "从来没人连上过的 worker 报告『有人在看』—— 宽限期永远不会武装，"
        "它会堵在 accept() 上永远不收摊（真机 7 分钟、对照实验 105 秒）"
    )
    assert time.monotonic() - alone_since < 5.0, "起算点不是绑好那一刻"


def test_the_clock_starts_at_birth_not_at_first_disconnect(source, monkeypatch) -> None:
    """等一会儿，『独处多久』要跟着长 —— 判据读的是同一个时钟。"""
    first = source.watcher_gone_since()
    monkeypatch.setattr(time, "monotonic", lambda: first + 60.0)
    assert source.watcher_gone_since() == first, "起点不该动"
    assert time.monotonic() - first == 60.0


def test_who_is_watching_still_has_exactly_one_answer() -> None:
    """"有没有人在看"只许有一份答案 —— `_alone_since`。

    `_ever_connected` 当年就是第二份，删掉的理由是分叉时两边都不报错。后来
    （PR#1020）另有一个**长得很像**的闩：`_adopted_anyone`，它答的是另一个问题
    ——"有没有人来过"。一个连上又走了的后端让"有人在看"为假、"有人来过"为真，
    收摊时要说哪句话（`no_watcher` / `no_backend`）就分在这里。

    所以判据不是"不许有闩"，是**不许让它回答别人的问题**：`watcher_gone_since`
    的实现里不许出现它。
    """
    import inspect
    import pathlib

    import platform_runtime

    src = pathlib.Path(platform_runtime.__file__).read_text(encoding="utf-8")
    code = "\n".join(
        ln for ln in src.splitlines()
        if not ln.lstrip().startswith("#") and "退、后端起 worker" not in ln
    )
    assert "_ever_connected" not in code, (
        "`_ever_connected` 还在 —— 它和 `_alone_since` 是同一个问题的两份答案"
    )

    watching = inspect.getsource(
        platform_runtime._ConnectionRequestSource.watcher_gone_since
    )
    assert "_adopted_anyone" not in watching, (
        "「有没有人在看」的实现读了「有没有人来过」的闩 —— 两个问题又合成了一个"
    )

    readers = [
        name for name, member in vars(platform_runtime._ConnectionRequestSource).items()
        if callable(member) and "_adopted_anyone" in (inspect.getsource(member) or "")
    ]
    assert sorted(readers) == ["__init__", "_adopt", "was_ever_adopted"], (
        f"`_adopted_anyone` 的读写点变成了 {sorted(readers)} —— 一个闩散到三处以上，"
        "下一次就会有人拿它回答第三个问题"
    )


def test_the_grace_period_still_covers_the_handshake_window() -> None:
    """删掉特例的前提：宽限期本身就兜得住握手。

    握手是"后端 spawn 完紧接着连上来"的几百毫秒。宽限期必须比它宽得多，否则
    删掉特例就变成误杀刚出生的 worker。
    """
    from platform_runtime import _NO_WATCHER_GRACE_S

    assert _NO_WATCHER_GRACE_S >= 10.0, (
        f"宽限期只有 {_NO_WATCHER_GRACE_S}s —— 兜不住握手窗口，"
        "「从出生起算」会误杀正在被接管的 worker"
    )


# ── 从没人连上来的 worker，走的时候要说一声（2026-09-15 node20）──────────────
#
# 换代 respawn 时后端连进了上一代垂死的 backlog；新 worker 绑好之后没有任何人连它，
# 60 秒后按"没人看"收摊 —— 退出码 0、一个字没留。后端那头拿到的全部线索是
# "exited before replying (exit code 0)"，看起来像它自己崩了。它没崩，它是没人来。


@pytest.fixture
def short_socket(tmp_path):
    """macOS 的 tmp_path 接近 AF_UNIX 长度上限；socket 放短根。"""
    import shutil
    import tempfile
    from pathlib import Path

    root = Path(tempfile.mkdtemp(prefix="hnb-", dir="/tmp"))
    try:
        yield root / "w.sock"
    finally:
        shutil.rmtree(root, ignore_errors=True)


@pytest.mark.asyncio
async def test_a_worker_nobody_connected_to_says_so_on_its_way_out(short_socket, monkeypatch, capsys) -> None:
    """判据落在三样东西上：说了（事件 + stderr）、说的是"没人连"、退出码不是 0。"""
    import platform_runtime as pr

    monkeypatch.setattr(pr, "_NO_WATCHER_GRACE_S", 0.4)
    monkeypatch.setattr(pr, "_NO_WATCHER_POLL_S", 0.1)
    events: list[dict] = []
    source = pr.SocketRequestSource(short_socket, on_connect=lambda _w: None)
    try:
        reason = await asyncio.wait_for(
            pr.serve_jsonl(source, lambda kind, **payload: events.append({"type": kind, **payload})),
            timeout=10,
        )
    finally:
        source.close()

    assert reason == "no_backend", reason
    said = [e for e in events if e.get("event") == "worker.no_backend"]
    assert said, f"没有留下『没人连』的事件：{[e.get('type') for e in events]}"
    assert "no backend connected" in said[0]["detail"]
    assert "no backend connected" in capsys.readouterr().err, "stderr 上一个字都没有 —— 后端那头看不见"
    assert pr.exit_code_for_serve_reason(reason) == pr.EXIT_NO_BACKEND != 0, "退出码 0 会被当成『做完了』"


@pytest.mark.asyncio
async def test_a_worker_someone_did_connect_to_does_not_claim_nobody_came(short_socket, monkeypatch, capsys) -> None:
    """对照组：连上过又走了的，理由是 no_watcher，不许冒充『没人来过』。"""
    import platform_runtime as pr

    monkeypatch.setattr(pr, "_NO_WATCHER_GRACE_S", 0.4)
    monkeypatch.setattr(pr, "_NO_WATCHER_POLL_S", 0.1)
    events: list[dict] = []
    source = pr.SocketRequestSource(short_socket, on_connect=lambda _w: None)

    async def _visit_and_leave() -> None:
        deadline = asyncio.get_running_loop().time() + 5
        conn = None
        while conn is None and asyncio.get_running_loop().time() < deadline:
            try:
                conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                conn.connect(str(short_socket))
            except OSError:
                conn = None
                await asyncio.sleep(0.02)
        assert conn is not None, "连不上刚绑好的源"
        try:
            # 后端连上的第一件事就是握手 status —— 来过的人必然说过话。
            conn.sendall(b'{"op":"status","request_id":"hello-1"}\n')
            # 回答走的是 `emit`（这里收进 events），不是这根裸 socket：别在 socket 上
            # 阻塞读（线程里的 recv 取消不掉，会把整条测试挂死）。
            while not any(e.get("type") == "status" for e in events):
                assert asyncio.get_running_loop().time() < deadline, "status 没被处理"
                await asyncio.sleep(0.02)
        finally:
            conn.close()

    try:
        reason, _ = await asyncio.wait_for(
            asyncio.gather(
                pr.serve_jsonl(source, lambda kind, **payload: events.append({"type": kind, **payload})),
                _visit_and_leave(),
            ),
            timeout=10,
        )
    finally:
        source.close()

    assert reason == "no_watcher", reason
    assert not [e for e in events if e.get("event") == "worker.no_backend"]
    assert "no backend connected" not in capsys.readouterr().err
    assert pr.exit_code_for_serve_reason(reason) == 0


@pytest.mark.asyncio
async def test_a_backend_that_only_shook_hands_is_not_accused_of_never_coming(
    short_socket, monkeypatch, capsys
) -> None:
    """握了手就走的后端 = **来过**，哪怕它一条请求都没发。

    这一条钉的是两个修复合流时的接缝（PR#1020 × PR#1046）。#1020 判"有没有人来过"
    读的是"读到过一行请求没有"，理由写在注释里：「后端连上的第一件事是握手 status，
    所以来过必然说过」。#1046 之后那句话不再成立 —— 握手是 `{"op":"hello"}`，由传输
    面（`_ConnectionRequestSource._authenticate`）吃掉，**永远到不了读循环**。

    于是只按"说过话没有"判，会把一个连上、握了手、随后自己崩掉的后端说成"从来没人
    来过"，并以 `EXIT_NO_BACKEND` 退场 —— 归错了责任方。判据因此落在传输面记下的
    那件事上（`was_ever_adopted`）。
    """
    import platform_runtime as pr

    monkeypatch.setattr(pr, "_NO_WATCHER_GRACE_S", 0.4)
    monkeypatch.setattr(pr, "_NO_WATCHER_POLL_S", 0.1)
    events: list[dict] = []
    source = pr.SocketRequestSource(
        short_socket, on_connect=lambda _w: None, spawn_token="tok-hs"
    )

    async def _shake_hands_and_die() -> None:
        deadline = asyncio.get_running_loop().time() + 5
        conn = None
        while conn is None and asyncio.get_running_loop().time() < deadline:
            try:
                conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                conn.connect(str(short_socket))
            except OSError:
                conn = None
                await asyncio.sleep(0.02)
        assert conn is not None, "连不上刚绑好的源"
        try:
            # 生产那条路一字不差：先报身份，等 `hello_ok`，然后……什么都不发。
            conn.sendall(b'{"op":"hello","spawn_token":"tok-hs"}\n')
            answer = await asyncio.to_thread(conn.makefile("r", encoding="utf-8").readline)
            assert json.loads(answer)["type"] == "hello_ok", answer
        finally:
            conn.close()

    try:
        reason, _ = await asyncio.wait_for(
            asyncio.gather(
                pr.serve_jsonl(
                    source, lambda kind, **payload: events.append({"type": kind, **payload})
                ),
                _shake_hands_and_die(),
            ),
            timeout=10,
        )
    finally:
        source.close()

    assert reason == "no_watcher", (
        f"握过手的后端被说成『从来没人来过』（reason={reason}）—— "
        "判据读的是请求行，而握手那一行到不了读循环"
    )
    assert not [e for e in events if e.get("event") == "worker.no_backend"]
    assert "no backend connected" not in capsys.readouterr().err
    assert pr.exit_code_for_serve_reason(reason) == 0

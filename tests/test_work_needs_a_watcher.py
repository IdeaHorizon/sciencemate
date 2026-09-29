"""没有人在看着的工作不该继续花钱。

## 病例（2026-09-07 真机）

用安装包跑着一个课题，然后退出应用（`osascript -e 'quit app "Agent for Science"'`）。
壳没了、后端没了 —— **worker 还在跑**：

    92580  ELAPSED 45:53  TIME 2:31.53  %CPU 3.5   …python3 -m platform_runtime --serve
    26 秒后：TIME 2:33.27               产物从 38 件涨到 41 件

界面已经不存在。用户看不见它、停不掉它，而它在调模型、花 token。

## 这与 PR#784 是两件事

#784「后端收摊放手不杀 worker」是对的：后端**被换掉**（重启、换代）时，不该
用一个转发面的消失去销毁一次真研究 —— worker 原地等下一个后端接进来，这正是
socket 模式存在的理由。

但 worker 分不清「后端被换掉」和「用户退出了应用」，在它看来都是"读端不见了"。
分辨这两件事不必猜，只要问一句：**过了这么久，还有人来接手吗？**
没有，就是没人要这份工作了。

## 判据

- 有轮在飞 + 没人看超过宽限期 → 用**与停止按钮同一份**机械信号就地收尾
  （所以是可续跑的暂停，不是崩溃；理由写在事实流 `work_stopped_no_watcher`）；
- 短暂断开（后端重启）**不动它** —— 宽限期比重启宽得多；
- 收尾之后手上没活、仍没人看 → 收摊退出，不留一个谁也看不见的进程；
- 握手还没完成过时不算"没人看"（worker 是后端 spawn 的，那几百毫秒里没人连
  着是正常的）。
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path

import pytest

import platform_runtime as pr
from core.llm import LLMResponse
from platform_runtime import RequestSource, serve_jsonl

from tests.test_platform_runtime import _FakeLLM

#: 停靠时长。取得足够大：真睡满了测试会超时，不会"碰巧也通过"。
_PARK_S = 3600.0


# ── 传输面：socket 源知道有没有人在看 ────────────────────────────────────────


@pytest.fixture
def short_root():
    """macOS 的 tmp_path 本身就接近 AF_UNIX 上限，socket 测试要一个短根。"""
    root = Path(tempfile.mkdtemp(prefix="hwatch-", dir="/tmp"))
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _source(path, callback):
    if hasattr(socket, "AF_UNIX"):
        return pr.SocketRequestSource(path, callback), path
    source = pr.TcpLoopbackRequestSource("127.0.0.1", 0, callback, spawn_token="test")
    return source, source._server.getsockname()


def _connect(path, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if isinstance(path, tuple):
                conn = socket.create_connection(path)
                conn.sendall(b'{"op":"hello","spawn_token":"test"}\n')
            else:
                conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                conn.connect(str(path))
            return conn
        except OSError:
            time.sleep(0.02)
    raise AssertionError(f"{timeout}s 内连不上 {path}")


async def _until(predicate, timeout=5.0) -> bool:
    """等一个条件成立。让出事件循环，因为 readline 就跑在同一个循环的线程池里。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return False


def test_a_handshake_in_flight_is_not_reaped(short_root) -> None:
    """握手还在路上的 worker 不能被收摊。

    ## 这条判据原来锚错了地方

    它本来断言的是 `watcher_gone_since() is None` —— 一个**实现细节**。而
    `watcher_gone_since()` 在生产代码里只有一个消费者：watchdog 拿它跟
    `_NO_WATCHER_GRACE_S`（60 秒）比。握手是几百毫秒的事，返回"出生至今 0.3 秒"
    与返回 `None` 对那个消费者是同一个结果 —— **都不会收摊**。

    锚在 `is None` 上的代价是真的：为了满足它，源对"从来没人连上过"一律答
    `None`，于是**后端 spawn 了 worker 却从没连上来**时，宽限期永远不武装，
    worker 堵在 accept() 上永远不收摊。2026-09-07 真机：打包自检留下的 worker
    7 分 26 秒仍在；对照实验（起一个从不连它）105 秒仍在。

    现在锚在效果上：**刚出生的源，独处时长远小于宽限期**，watchdog 因此不动它。
    这既保住了这条判据要保的事，又不再要求源对"没人来过"说谎。
    """
    path = pr.control_socket_path(short_root, "p1", "s1")
    source, path = _source(path, lambda _stream: None)
    try:
        alone_since = source.watcher_gone_since()
        assert alone_since is not None, (
            "刚绑好的源答『有人在看』—— 那正是让宽限期永远不武装的那条特例"
        )
        alone_for = time.monotonic() - alone_since
        assert alone_for < pr._NO_WATCHER_GRACE_S / 10, (
            f"刚出生就已独处 {alone_for:.1f}s，逼近宽限期 "
            f"{pr._NO_WATCHER_GRACE_S}s —— 握手期的 worker 有被误杀的风险"
        )
    finally:
        source.close()


@pytest.mark.asyncio
async def test_the_source_knows_when_the_last_reader_left(short_root) -> None:
    """接上 → 有人看；断开 → 从那一刻起没人看；再接上 → 重新有人看。

    对着真 socket、真断连验 —— 这类事故只在真实收发交错里出现。

    读者用 `source.readline()`（生产里 worker 永远停在这上面）：对端走了正是
    **读**的时候发现的，不起读者，`_drop` 永远不被调到，测的就不是那条路。
    最后用一条真请求让读者自然收工，而不是从底下把 socket 抽走 —— 后者会在
    另一个线程里关掉 pytest 正用着的 fd，失败信息本身都打不出来（实测）。
    """
    path = pr.control_socket_path(short_root, "p1", "s1")
    seen: list = []
    source, path = _source(path, seen.append)
    reader = asyncio.create_task(source.readline(4096))
    conn = again = None
    try:
        conn = _connect(path)
        assert await _until(lambda: source.watcher_gone_since() is None and bool(seen))

        conn.close()
        assert await _until(lambda: source.watcher_gone_since() is not None), (
            "对端断开之后，源还认为有人在看"
        )
        left_at = source.watcher_gone_since()
        assert isinstance(left_at, float) and time.monotonic() - left_at < 10

        again = _connect(path)
        assert await _until(lambda: source.watcher_gone_since() is None), (
            "重新接上之后还认为没人看 —— 那样下一次断开的宽限期会从旧时刻算"
        )
        again.sendall(b'{"op": "status"}\n')
        assert await asyncio.wait_for(reader, timeout=5)
        again.close()
    finally:
        # 客户端那两个 socket 也要关：判据没过时上面的 assert 会直接抛出去，
        # `again` 就留在连着的状态，读者线程停在 `rfile.readline()` 上 ——
        # 而 `to_thread` 的任务 cancel 掉并不会停住那个线程，事件循环收尾时
        # 等它 join，整条测试挂死（实测挂过两次，连失败信息都打不出来）。
        for client in (conn, again):
            if client is not None:
                with contextlib.suppress(OSError):
                    client.close()
        reader.cancel()
        source.close()


# ── 决策：有轮在飞时会不会真的停 ─────────────────────────────────────────────


class _WatchedSource(RequestSource):
    """可投喂的命令面 + 一个我说了算的「有没有人在看」。

    真 socket 的连接状态由 `SocketRequestSource` 那两条测试守着；这里要验的是
    **serve_jsonl 拿这个事实做了什么**，所以把事实本身做成可控的。
    """

    def __init__(self, initial: list[dict]) -> None:
        self._queue: asyncio.Queue[str | None] = asyncio.Queue()
        for request in initial:
            self._queue.put_nowait(json.dumps(request, ensure_ascii=False) + "\n")
        self._alone_since: float | None = None
        self.closed = False

    def everyone_left(self) -> None:
        self._alone_since = time.monotonic()

    def someone_arrived(self) -> None:
        self._alone_since = None

    def watcher_gone_since(self) -> float | None:
        return self._alone_since

    def send(self, request: dict) -> None:
        self._queue.put_nowait(json.dumps(request, ensure_ascii=False) + "\n")

    def eof(self) -> None:
        self._queue.put_nowait(None)

    async def readline(self, limit: int) -> str:
        line = await self._queue.get()
        return line if line is not None else ""

    def close(self) -> None:
        self.closed = True
        self._queue.put_nowait(None)


def _done(text: str) -> LLMResponse:
    return LLMResponse(
        content=text, tool_calls=[], finish_reason="stop",
        usage={"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
    )


def _a_real_worktree(tmp_path: Path) -> Path:
    worktree = tmp_path / "wt"
    worktree.mkdir(parents=True, exist_ok=True)
    for command in (
        ["git", "init", "-q"],
        ["git", "config", "user.email", "watch@test"],
        ["git", "config", "user.name", "watch"],
        ["git", "commit", "-q", "--allow-empty", "-m", "init"],
    ):
        subprocess.run(command, cwd=worktree, check=True)
    return worktree


async def _drive(tmp_path: Path, monkeypatch, *, leave_after_s: float,
                 come_back_after_s: float | None = None,
                 stop_after_return: bool = False) -> list[dict]:
    """真跑一次会停靠的无人值守，中途让"所有人离开"。返回事件流。"""
    from core import session_driver
    from core.session_driver import SessionAction

    calls = {"n": 0}

    async def parked_next_action(state, reply, *, reason: str = "", **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return SessionAction(kind="prompt", prompt="复查：还堵着吗？",
                                 delay_s=_PARK_S, reason="blocked_parked")
        return SessionAction(kind="stop", reason="test_done")

    worktree = _a_real_worktree(tmp_path)
    llm = _FakeLLM([_done(f"第 {n + 1} 轮") for n in range(6)])
    source = _WatchedSource([
        {
            "op": "init", "request_id": "init-w",
            "tenant_id": "tenant-test", "project_id": "project-w",
            "session_id": "session-w",
            "home_dir": str(tmp_path / "isolated-home"),
            "workspace_dir": str(worktree),
        },
        {"op": "run_unattended", "request_id": "un-w", "message": "自己跑",
         "max_turns": 2},
    ])
    events: list[dict] = []

    async def _leave_and_maybe_return():
        await asyncio.sleep(leave_after_s)
        source.everyone_left()
        if come_back_after_s is not None:
            await asyncio.sleep(come_back_after_s)
            source.someone_arrived()
            if stop_after_return:
                # 对照组必须有自己的终局。停靠是 1 小时，没有这一下测试只会
                # 超时 —— 而超时和"停错了"长得一样，那样这条判据什么都没说。
                await asyncio.sleep(1.5)
                source.send({"op": "stop", "request_id": "stop-w", "author": "tester"})

    monkeypatch.setattr(session_driver, "next_action", parked_next_action)
    monkeypatch.setattr(pr, "_NO_WATCHER_GRACE_S", 1.0)
    monkeypatch.setattr(pr, "_NO_WATCHER_POLL_S", 0.1)

    def _collect(event_type, **payload):
        events.append({"type": event_type, **payload})
        if event_type == "result" and payload.get("request_id") == "un-w":
            source.eof()

    await asyncio.wait_for(
        asyncio.gather(serve_jsonl(source, _collect, llm=llm), _leave_and_maybe_return()),
        timeout=60,
    )
    return events


@pytest.mark.asyncio
async def test_a_parked_turn_stops_when_no_one_comes_back(tmp_path, monkeypatch) -> None:
    """所有人都走了、宽限期过去 → 这一轮就地收尾，理由说得出口。"""
    events = await _drive(tmp_path, monkeypatch, leave_after_s=0.3)

    told = [e for e in events if e.get("event") == "worker.no_watcher"]
    assert told, (
        "没人看着，这一轮还在跑 —— 真机上它就是这么在用户退出应用之后接着花钱的。"
        f"事件里有：{sorted({e['type'] for e in events})}"
    )
    assert "没有人在看这一轮" in told[0]["detail"]
    assert [e for e in events if e["type"] == "result" and e.get("request_id") == "un-w"], (
        "停了，但这一轮没有终局 —— 停止必须落在一个可续跑的暂停上，不是挂死"
    )


@pytest.mark.asyncio
async def test_a_backend_restart_does_not_stop_the_work(tmp_path, monkeypatch) -> None:
    """短暂断开（后端重启）不动它 —— 否则每次重启都毁掉一次真研究。

    这条是上面那条的对照：同一条路径、同样"没人看"，只是有人回来了。
    没有它，把宽限期改成 0 也能全绿。
    """
    events = await _drive(tmp_path, monkeypatch, leave_after_s=0.2, come_back_after_s=0.3,
                          stop_after_return=True)

    assert not [e for e in events if e.get("event") == "worker.no_watcher"], (
        "后端只是重启了一下（0.3s 就接回来），却把这一轮停了"
    )
    assert [e for e in events if e["type"] == "result" and e.get("request_id") == "un-w"]


@pytest.mark.asyncio
@pytest.mark.parametrize("after_turn", [False, True])
async def test_an_idle_worker_with_no_watcher_packs_up(tmp_path, monkeypatch, after_turn) -> None:
    """手上没活、也没人看 → 收摊退出，不留一个谁也看不见的进程。

    会话状态在盘上；下次后端要用时 respawn 一个接着来（续跑是全函数）。
    """
    from core import session_driver

    worktree = _a_real_worktree(tmp_path)
    source = _WatchedSource([{
        "op": "init", "request_id": "init-i",
        "tenant_id": "tenant-test", "project_id": "project-i",
        "session_id": "session-i",
        "home_dir": str(tmp_path / "isolated-home"),
        "workspace_dir": str(worktree),
    }])
    if after_turn:
        source.send({"op": "turn", "request_id": "finished-turn", "message": "完成这一轮"})
    finished = asyncio.Event()
    events: list[dict] = []

    def collect(kind, **payload):
        events.append({"type": kind, **payload})
        if kind == "result" and payload.get("request_id") == "finished-turn":
            finished.set()

    monkeypatch.setattr(pr, "_NO_WATCHER_GRACE_S", 0.5)
    monkeypatch.setattr(pr, "_NO_WATCHER_POLL_S", 0.1)
    assert session_driver is not None  # 只为说明这条路没有 patch 掉策略层

    async def _leave():
        if after_turn:
            await asyncio.wait_for(finished.wait(), timeout=15)
        else:
            await asyncio.sleep(0.3)
        source.everyone_left()

    # serve 必须**自己**返回 —— 没有人给它 EOF。
    await asyncio.wait_for(
        asyncio.gather(
            serve_jsonl(source, collect, llm=_FakeLLM([_done("hi")])),
            _leave(),
        ),
        timeout=30,
    )
    assert source.closed, "空闲 worker 在没人看之后没有收摊"


def test_the_shipped_grace_is_wider_than_a_restart() -> None:
    """出厂宽限期必须比一次后端重启宽得多。

    上面那些判据都把宽限期 patch 成 1 秒（否则每条测试要等一分钟），所以它们
    证明不了**出厂那个值**是合理的 —— 有人把它改成 0，那些测试一条都不会红。
    这条专门钉住出厂值：重启是秒级的，60 秒给了一个数量级的余量。
    """
    assert pr._NO_WATCHER_GRACE_S >= 30.0, (
        f"出厂宽限期只有 {pr._NO_WATCHER_GRACE_S}s —— 一次稍慢的后端重启就会被当成"
        "「没人要这份工作了」，把正在跑的研究停掉"
    )
    assert pr._NO_WATCHER_POLL_S <= pr._NO_WATCHER_GRACE_S / 5, "复查间隔比宽限期还粗，判据形同虚设"

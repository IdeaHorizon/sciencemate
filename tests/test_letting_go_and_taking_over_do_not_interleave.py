"""worker 的命令面上，「放开旧后端」与「接下新后端」是互斥的一个动作（#1253）。

## 现场

`platform / public-tree`（`-n 6`）两次红在同一句：

    test_a_docked_worker_survives_backend_shutdown
    AssertionError: 放开之后接不回来 —— 那放开就只是换了一种丢法

后端收摊放手（关掉连接），紧接着同一个进程去接回。worker 那头，读线程看到
旧连接的 EOF 去「放手」、accept 线程同时「接手」新连接，两边各是三段：摘/装连接、
换转发面、记"有没有人在看"，中间不持锁。交错成

    摘掉旧连接 → [装上新连接、转发面指向它、记"有人看"] → 转发面置空、记"没人看"

新后端的命令读得到，回话却进了 /dev/null（接回时那句 status 等满 10 秒）；worker
还以为没人在看，60 秒后停下研究或自行退场。机器一忙（两个 CI 同时在一台 runner
上）两个事件就会同时摆在 worker 面前 —— 本机用 SIGSTOP 饿住 worker 复现，150 次
红 16 次（修好后 150 次全绿）。

这里不靠"机器够不够忙"：把放手那一段**故意拉长**，让新后端一定落进窗口里。
判据落在可观测的后果上：转发面最后指向谁、worker 觉得有没有人在看、回话到不到得了。

## 两种传输各跑一遍

放手 / 接手 / 挂断的机制在 `_ConnectionRequestSource` 里只有一份，两种传输只差
`_make_server`。但"断线能不能叫醒另一条线程里堵着的 recv"是**操作系统**的事，
不是我们的：Windows 上 worker 的命令面是 TCP 环回（CPython 在那里没有
`socket.AF_UNIX`），只在 unix socket 上验过就等于没验 Windows。所以每条测试按本平台
**存在**的传输参数化：TCP 环回处处都跑（CI 的 Linux 也覆盖它），unix socket 只在有
`AF_UNIX` 的平台跑。两种都按生产的样子带 spawn_token、先握手再说话。
"""
from __future__ import annotations

import asyncio
import inspect
import json
import shutil
import socket
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from contextlib import contextmanager
from pathlib import Path

import pytest

import platform_runtime as pr

#: 故意拉长「放手」的上限。修好之后接手要等放手做完，所以这段会被**整段**等满；
#: 没修时接手会插进来、提前叫醒它。只是注入的延迟，不是判据。
_DRAG_S = 1.0


TOKEN = "tok-let-go"


def _recv_line(conn: socket.socket) -> bytes:
    """读一整行（逐字节，不在测试这头留缓冲 —— 后面还要在同一根连接上看 EOF）。"""
    line = b""
    while not line.endswith(b"\n"):
        chunk = conn.recv(1)
        if not chunk:
            break
        line += chunk
    return line


def _hung_up(conn: socket.socket) -> bool:
    """对面把这根连接挂了：读到 EOF，或者（对面关时还有没读完的字节）被 reset。"""
    try:
        return conn.recv(1024) == b""
    except (ConnectionResetError, ConnectionAbortedError):
        return True


class _Transport:
    """一种命令面传输：worker 那头的源怎么起、后端这头怎么连上（含身份握手）。"""

    def __init__(self, kind: str, root: Path | None) -> None:
        self.kind = kind
        self._root = root
        self._address: object = None

    def source(self, on_connect):
        if self.kind == "unix":
            path = pr.control_socket_path(self._root, "p1", "s1")
            src = pr.SocketRequestSource(path, on_connect, TOKEN)
            self._address = str(path)
        else:
            src = pr.TcpLoopbackRequestSource("127.0.0.1", 0, on_connect, TOKEN)
            self._address = src._server.getsockname()[:2]
        return src

    def connect(self, timeout: float = 5.0) -> socket.socket:
        family = socket.AF_UNIX if self.kind == "unix" else socket.AF_INET
        deadline = time.monotonic() + timeout
        while True:
            conn = socket.socket(family, socket.SOCK_STREAM)
            try:
                conn.connect(self._address)
                break
            except OSError:
                conn.close()
                if time.monotonic() >= deadline:
                    raise AssertionError(f"{timeout}s 内连不上 {self._address}") from None
                time.sleep(0.02)
        conn.settimeout(timeout)
        conn.sendall((json.dumps({"op": "hello", "spawn_token": TOKEN}) + "\n").encode())
        answer = _recv_line(conn)
        assert json.loads(answer or b"{}").get("type") == "hello_ok", f"握手没过: {answer!r}"
        return conn


@pytest.fixture(params=[
    pytest.param("unix", id="unix-socket", marks=pytest.mark.skipif(
        not hasattr(socket, "AF_UNIX"),
        reason="这个平台没有 AF_UNIX（Windows）—— worker 的命令面只有 TCP 环回",
    )),
    pytest.param("tcp", id="tcp-loopback"),
])
def transport(request):
    if request.param != "unix":
        yield _Transport(request.param, None)
        return
    # unix socket 路径有长度上限（macOS 上 pytest 的 tmp_path 已贴着它）—— 落在短路径下。
    root = Path(tempfile.mkdtemp(prefix="hlet-", dir="/tmp"))
    try:
        yield _Transport(request.param, root)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _until(predicate, timeout: float = 5.0) -> bool:
    """有界地等一个可观测的状态成立。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


@contextmanager
def _hung_up_on_exit(src, connections):
    """收尾：先挂断测试这头的连接、关掉源，线程池才收得了工。

    挂着的读者可能正堵在某根连接的 `readline` 里（没修好时正是这样）；不先放走它，
    断言一失败，线程池的退出就会永远等下去，红变成挂死。
    """
    try:
        yield
    finally:
        for conn in connections():
            if conn is not None:
                conn.close()
        src.close()


def _read_in_background(pool: ThreadPoolExecutor, src):
    """serve 循环的形状：socket 源上读者**一直**挂在当前连接上（命令面并发）。"""
    return pool.submit(asyncio.run, src.readline(65536))


def test_the_old_backends_goodbye_cannot_unhook_the_backend_that_took_over(transport):
    """旧后端的 EOF 还在处理，新后端已经接进来：转发面必须落在新连接上，worker 必须
    知道有人在看，回话必须到得了新后端。"""
    bound: list = []
    goodbye_started = threading.Event()
    someone_took_over = threading.Event()

    def on_connect(stream):
        if stream is None:
            goodbye_started.set()
            # 放手做到一半停住：新后端要是插得进来，它会在这里把我们叫醒。
            someone_took_over.wait(_DRAG_S)
        elif bound:
            someone_took_over.set()
        bound.append(stream)

    src = transport.source(on_connect)
    old = new = None
    with ThreadPoolExecutor(1) as pool, _hung_up_on_exit(src, lambda: (old, new)):
        old = transport.connect()
        assert _until(lambda: len(bound) == 1), "第一个后端没被接下"
        pending = _read_in_background(pool, src)
        old.close()                                       # 旧后端收摊
        assert goodbye_started.wait(5), "读者没看到旧连接的 EOF"
        new = transport.connect()                         # 新后端在放手途中接进来
        new.sendall(b'{"op":"status","who":"new"}\n')
        assert json.loads(pending.result(timeout=10))["who"] == "new"
        assert _until(lambda: len(bound) == 3), f"转发面换了 {len(bound)} 次，应是 接上/摘掉/接上"

        assert bound[-1] is not None, (
            "旧后端的放手把新后端的转发面摘掉了 —— 它的命令读得到，回话全进了 /dev/null"
        )
        assert src.watcher_gone_since() is None, (
            "新后端连着，worker 却记成没人在看 —— 60 秒后它会停下研究 / 自行退场"
        )
        bound[-1].write('{"type":"status"}\n')
        bound[-1].flush()
        assert _recv_line(new) == b'{"type":"status"}\n', "回话没送到接手的那个后端"


def test_the_worker_hangs_up_only_after_it_has_let_go(transport):
    """后端放手时等的回执是「worker 把连接关了」。这个回执必须**晚于**放手本身：
    对面读到 EOF 的那一刻，转发面已经摘掉、"没人在看"已经记下。"""
    bound: list = []
    hung_up = threading.Event()

    def on_connect(stream):
        if stream is None:
            # 摘转发面这一步拉长：挂断要是排在它前面，对面会在这里还没做完时就读到 EOF。
            hung_up.wait(_DRAG_S)
        bound.append(stream)

    src = transport.source(on_connect)
    conn = None
    with ThreadPoolExecutor(1) as pool, _hung_up_on_exit(src, lambda: (conn,)):
        conn = transport.connect()
        assert _until(lambda: len(bound) == 1), "后端没被接下"
        _read_in_background(pool, src)
        conn.shutdown(socket.SHUT_WR)                     # 后端放手：只关写端，等回执
        assert _hung_up(conn), "worker 没有关连接 —— 没有回执可等"
        forwarding, alone_since = bound[-1], src.watcher_gone_since()
        hung_up.set()
        assert forwarding is None, "回执早于放手：对面读到 EOF 时转发面还挂在它身上"
        assert alone_since is not None, (
            "回执早于放手：对面读到 EOF 时 worker 还没记下「没人在看」"
        )


def _a_reader_is_parked_on_a_connection() -> bool:
    """某条线程此刻正停在 `_blocking_readline` 里那句 `rfile.readline(limit)` 上。"""
    method = pr._ConnectionRequestSource._blocking_readline
    lines, start = inspect.getsourcelines(method)
    hits = [start + i for i, text in enumerate(lines) if "rfile.readline(limit)" in text]
    assert len(hits) == 1, "_blocking_readline 改写了 —— 这个探针要跟着改，否则它恒假"
    for frame in sys._current_frames().values():
        # 栈顶是 `socket.SocketIO.readinto`（读流的 C 实现回调到它），往下找。
        while frame is not None:
            if frame.f_code is method.__code__ and frame.f_lineno == hits[0]:
                return True
            frame = frame.f_back
    return False


def test_taking_over_from_a_backend_that_is_still_connected_does_not_wedge_the_worker(transport):
    """新后端接进来时旧连接还开着、一声不吭（旧后端还在优雅关机里，没走到放手）。

    读者正堵在旧连接的 `readline` 里。顶替时只关文件对象是关不掉的 —— 那个读流的
    `close()` 要等读者手里那把缓冲区锁，而读者在等一个不会说话的对面：accept 线程
    就此永远卡住，新后端的命令没人读。顶替得先**断线**。

    "断线叫醒另一条线程里堵着的 recv"是操作系统给的，所以这条在 TCP 环回上的结果
    才是 Windows 的答案。
    """
    bound: list = []
    src = transport.source(bound.append)
    old = new = None
    with ThreadPoolExecutor(1) as pool, _hung_up_on_exit(src, lambda: (old, new)):
        old = transport.connect()
        assert _until(lambda: len(bound) == 1), "第一个后端没被接下"
        pending = _read_in_background(pool, src)
        assert _until(_a_reader_is_parked_on_a_connection), (
            "读者没堵到旧连接上 —— 这条测试没摆出它要测的局面"
        )
        new = transport.connect()
        new.sendall(b'{"op":"status","who":"new"}\n')
        try:
            line = pending.result(timeout=5)
        except FutureTimeout:
            pytest.fail("新后端的命令 5 秒没人读 —— accept 线程卡在关旧连接上")
        assert json.loads(line)["who"] == "new"
        assert _hung_up(old), "被顶掉的旧连接没有被挂断"
        assert bound[-1] is not None and src.watcher_gone_since() is None

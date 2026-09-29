"""命令面走 TCP 环回（Windows 唯一的路：CPython 在 Windows 上没有 AF_UNIX）。

语义与 `SocketRequestSource` 逐字相同（断开≠结束、单连接顶替、事件继续落盘），差别
只有两处：绑 127.0.0.1:<内核挑的端口>，以及**每个连接必须先握手**——环回上任何本机
进程都连得过来，只有第一行 `{"op":"hello","spawn_token":…}` 对得上的才认。
"""
from __future__ import annotations

import asyncio
import json
import socket
import time

import pytest

import platform_runtime as pr

TOKEN = "tok-loopback"


def _raw_connect(host, port, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            conn = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            conn.connect((host, port))
            return conn
        except OSError:
            time.sleep(0.02)
    raise AssertionError(f"{timeout}s 内连不上 {host}:{port}")


def _say_hello(conn, token=TOKEN):
    conn.sendall((json.dumps({"op": "hello", "spawn_token": token}) + "\n").encode())


@pytest.fixture
def source():
    bound: list = []
    src = pr.TcpLoopbackRequestSource("127.0.0.1", 0, bound.append, TOKEN)
    host, port = src._server.getsockname()[:2]
    try:
        yield src, host, port, bound
    finally:
        src.close()


def test_binds_and_reports_a_tcp_address(source):
    src, host, port, _ = source
    assert host == "127.0.0.1" and port > 0
    assert pr._ACTUAL_CONTROL_SOCKET == f"tcp:127.0.0.1:{port}"


def test_a_line_arrives_after_a_valid_hello(source):
    src, host, port, _ = source
    conn = _raw_connect(host, port)
    _say_hello(conn)
    conn.sendall(b'{"op":"turn"}\n')
    assert asyncio.run(src.readline(65536)) == '{"op":"turn"}\n'
    conn.close()


def test_a_wrong_token_is_rejected_and_a_right_one_still_works(source):
    src, host, port, bound = source
    bad = _raw_connect(host, port)
    _say_hello(bad, token="not-the-token")
    bad.sendall(b'{"op":"turn","who":"impostor"}\n')
    time.sleep(0.3)
    assert bound == [], "认证不过的连接不该接进转发面"

    good = _raw_connect(host, port)
    _say_hello(good)
    good.sendall(b'{"op":"turn","who":"real"}\n')
    assert json.loads(asyncio.run(src.readline(65536)))["who"] == "real"
    bad.close()
    good.close()


def test_a_missing_hello_is_rejected(source):
    src, host, port, bound = source
    conn = _raw_connect(host, port)
    # 不握手，直接发命令 —— worker 把第一行当 hello 读，解析不出 op=hello → 丢弃。
    conn.sendall(b'{"op":"turn"}\n')
    time.sleep(0.3)
    assert bound == [], "没握手的连接不该接进来"
    conn.close()


def test_a_dropped_connection_is_not_the_end_of_the_session(source):
    """**断开≠结束**——TCP 上和 unix 上一样。"""
    src, host, port, bound = source
    first = _raw_connect(host, port)
    _say_hello(first)
    first.sendall(b'{"op":"turn","n":1}\n')
    assert json.loads(asyncio.run(src.readline(65536)))["n"] == 1

    async def read_next():
        pending = asyncio.create_task(src.readline(65536))
        await asyncio.sleep(0.3)
        first.close()                         # 后端死了 —— 读者正等着
        await asyncio.sleep(0.4)
        assert bound[-1] is None, "断开后转发面该摘掉"
        second = _raw_connect(host, port)
        _say_hello(second)
        second.sendall(b'{"op":"turn","n":2}\n')
        try:
            return await asyncio.wait_for(pending, timeout=5)
        finally:
            second.close()

    assert json.loads(asyncio.run(read_next()))["n"] == 2
    assert bound[-1] is not None, "接回来之后转发面该重新绑上"


def test_a_new_connection_supersedes_the_old_one(source):
    src, host, port, _ = source
    old = _raw_connect(host, port)
    _say_hello(old)
    time.sleep(0.3)
    new = _raw_connect(host, port)
    _say_hello(new)
    time.sleep(0.3)
    new.sendall(b'{"op":"turn","who":"new"}\n')
    assert json.loads(asyncio.run(src.readline(65536)))["who"] == "new"
    old.close()
    new.close()


def test_a_worker_nobody_connected_to_counts_as_unwatched(source):
    """与 unix 同一条：从绑好那一刻起就算"没人看"（宽限期靠它武装）。"""
    src, *_ = source
    assert src.watcher_gone_since() is not None

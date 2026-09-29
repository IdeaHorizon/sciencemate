"""命令面走 socket：连接断开不是会话结束（RFC 异步运行时 P0-2）。

P0-1 拆了信号连坐，P0-3 让管道断了不杀这一轮。但只要命令面还是 stdin，
后端一死 stdin 就 EOF、worker 退出 —— 当前这一轮跑完也活不到下一条命令。
这一层是最后一根钉子，判据只有一条：**断开再接回来，会话还在原地**。

对着真 socket、真线程、真断连验 —— 这些事故只在真实收发交错里出现。
"""
from __future__ import annotations

import asyncio
import io
import json
import socket
import time
from pathlib import Path

import pytest

import platform_runtime as pr
from core.worker_addressing import MAX_UNIX_SOCKET_PATH


def _connect(path, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            conn.connect(str(path))
            return conn
        except OSError:
            time.sleep(0.02)
    raise AssertionError(f"{timeout}s 内连不上 {path}")


@pytest.fixture
def short_root():
    """pytest 的 tmp_path 在 macOS 上本身就接近 AF_UNIX 上限 —— socket 测试
    需要一个真正短的根，否则测的是 pytest 的路径长度不是我们的代码。"""
    import shutil
    import tempfile

    root = Path(tempfile.mkdtemp(prefix="hsock-", dir="/tmp"))
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
def source(short_root):
    """真 socket 源 + 记录转发面变化（None = 当前没人在说话）。"""
    path = pr.control_socket_path(short_root, "p1", "s1")
    bound: list = []
    src = pr.SocketRequestSource(path, bound.append)
    try:
        yield src, path, bound
    finally:
        src.close()


def test_the_socket_path_fits_the_af_unix_limit(tmp_path):
    """AF_UNIX 有硬上限，而 session state 路径长达 284 —— 实测 bind 当场失败。

    深到放不下时**回退**而不是报错：路径只是地址（后端从注册表行读它），
    身份是 project/session。为"部署路径深了一点"让整个平台起不来，是把一件
    本可以自己解决的事变成事故。
    """
    deep = tmp_path / ("x" * 60) / ("y" * 60) / ("z" * 60)
    for root in (tmp_path, deep):
        assert len(str(pr.control_socket_path(root, "p1", "s1"))) <= MAX_UNIX_SOCKET_PATH
    # 身份来自 project/session，不来自放在哪
    assert pr.control_socket_path(tmp_path, "p1", "s1") != pr.control_socket_path(tmp_path, "p1", "s2")
    assert pr.control_socket_path(deep, "p1", "s1").name == pr.control_socket_path(
        tmp_path, "p1", "s1"
    ).name


def test_a_too_deep_root_still_binds(short_root):
    """回退不是纸面承诺 —— 真 bind 一次。"""
    deep = short_root / ("x" * 60) / ("y" * 60)
    src = pr.SocketRequestSource(pr.control_socket_path(deep, "p1", "deep"), lambda _w: None)
    try:
        _connect(pr.control_socket_path(deep, "p1", "deep")).close()
    finally:
        src.close()


def test_a_line_arrives_from_the_connection(source):
    src, path, _ = source
    conn = _connect(path)
    conn.sendall(b'{"op":"turn"}\n')
    assert asyncio.run(src.readline(65536)) == '{"op":"turn"}\n'
    conn.close()


def test_a_dropped_connection_is_not_the_end_of_the_session(source):
    """**这条是 P0-2 的全部意义。** stdio 下这里会 EOF、worker 退出。

    形态贴着真实调用：serve 循环处理完一条命令就立刻回到 readline 上 ——
    所以这里也是"读着的时候"后端死掉，而不是空转时死掉。
    """
    src, path, bound = source
    first = _connect(path)
    first.sendall(b'{"op":"turn","n":1}\n')
    assert json.loads(asyncio.run(src.readline(65536)))["n"] == 1

    async def read_next():
        # 这个 readline 跨越了"后端死掉 → 新后端接进来"的整个过程。
        pending = asyncio.create_task(src.readline(65536))
        await asyncio.sleep(0.3)
        first.close()                       # 后端死了 —— 读者正等着
        await asyncio.sleep(0.4)
        assert bound[-1] is None, "断开后转发面该摘掉（事件只落盘）"

        second = _connect(path)             # 新后端接进来
        second.sendall(b'{"op":"turn","n":2}\n')
        try:
            return await asyncio.wait_for(pending, timeout=5)
        finally:
            second.close()

    # 同一个 source、同一个会话：那次 readline 没有 EOF，它等到了新后端。
    assert json.loads(asyncio.run(read_next()))["n"] == 2
    assert bound[-1] is not None, "接回来之后转发面该重新绑上"


def test_the_forwarding_surface_follows_the_live_connection(source):
    """转发面必须跟着当前连接走 —— 事件发给"还在的那个后端"。"""
    src, path, bound = source
    first = _connect(path)
    time.sleep(0.3)
    writer = bound[-1]
    assert writer is not None
    writer.write("hello\n")
    writer.flush()
    assert first.recv(1024) == b"hello\n"
    first.close()


def test_a_new_connection_supersedes_the_old_one(source):
    """两个后端不能同时指挥一个会话：新连接顶掉旧的。
    旧的那个按定义已经不在了（后端重启后没人再用它）。"""
    src, path, bound = source
    old = _connect(path)
    time.sleep(0.3)
    new = _connect(path)
    time.sleep(0.3)

    new.sendall(b'{"op":"turn","who":"new"}\n')
    assert json.loads(asyncio.run(src.readline(65536)))["who"] == "new"
    old.close()
    new.close()


def test_close_is_the_only_real_end(source):
    """只有 close() 才返回 EOF —— 断连不是。"""
    src, path, _ = source
    conn = _connect(path)
    conn.close()
    src.close()
    assert asyncio.run(src.readline(65536)) == ""


def test_a_stale_socket_file_does_not_block_the_next_worker(short_root):
    """上一个 worker 留下的死 socket 文件不该挡住这一个。
    "谁持有这个会话"由 flock 回答 —— 一个问题一个真相源。"""
    path = pr.control_socket_path(short_root, "p1", "s1")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("我是上一个 worker 留下的尸体", encoding="utf-8")

    src = pr.SocketRequestSource(path, lambda _w: None)
    try:
        _connect(path).close()          # 能连上 = 真的绑成功了
    finally:
        src.close()


def test_stdio_path_is_unchanged(tmp_path):
    """老路必须逐字节同义：管道 EOF **就是**会话结束。"""
    src = pr.StdioRequestSource(io.StringIO('{"op":"turn"}\n'))
    assert asyncio.run(src.readline(65536)) == '{"op":"turn"}\n'
    assert asyncio.run(src.readline(65536)) == ""      # EOF

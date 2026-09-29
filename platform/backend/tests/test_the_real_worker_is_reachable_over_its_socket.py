"""后端和**真的** worker 能在 socket 上说上话（2026-08-19 node20 事故的验收）。

## 为什么已有的 spawn 测试抓不到

`test_local_runtime_api` 里那些"真起进程"的用例，起的是一个**假的**
`platform_runtime.py`（测试自己写出来的桩，读 stdin）。桩读 stdin，于是后端
回退到管道也一切正常 —— 真 worker 只读 socket 这件事，那些测试看不见。

实测代价：相对路径 `data/harness_sockets` 在后端（cwd=platform/backend）和
worker（cwd=harness 根）里指向两个不同文件，worker 绑一个、后端连另一个，
后端回退管道 → 死锁。全套测试绿，node20 上新会话永远停在"正在启动"。

## 这条测的是什么

真 `platform_runtime --serve` + 真地址推导（`_control_socket_path`）+ 真连接
（`_connect_control_socket`）+ 真 RPC 往返（`op=status`，它**在 init 之前**
也能答，正是为重连体检设计的）。

跨进程地址不一致的话，这条会直接失败 —— 而这就是那天缺的那道闸。
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config import DataRootError, settings
from app.services.harness_sessions import _connect_control_address, _control_address

HARNESS_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def short_socket_root(monkeypatch):
    """短根 —— AF_UNIX 有长度上限，用 pytest 的 tmp_path 会触发回退。"""
    root = Path(tempfile.mkdtemp(prefix="sockroot-", dir="/tmp"))
    monkeypatch.setattr(settings, "harness_root", str(HARNESS_ROOT))
    monkeypatch.setattr(settings, "harness_control_socket", True)
    monkeypatch.setattr(settings, "harness_socket_root", str(root))
    from app.services.harness_contract import _harness_root

    _harness_root.cache_clear()
    try:
        yield root
    finally:
        _harness_root.cache_clear()
        shutil.rmtree(root, ignore_errors=True)


def _a_free_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["unix", "tcp"])
async def test_backend_and_real_worker_agree_on_the_control_address(short_socket_root, transport):
    """**核心**：后端交出的地址，真 worker 真的绑在那里，并且答得出 status。

    两种传输同一条判据（RFC #848 §4.3）：unix 走 AF_UNIX（POSIX），tcp 走 127.0.0.1
    环回 + spawn-token 握手（Windows 唯一的路，POSIX 也能跑同一条来验）。tcp 这里用
    一个**具体**端口端到端验传输 + 握手；`tcp:...:0` 的注册表回读单独在
    `test_await_worker_address_reads_the_bound_port` 里测。
    """
    if transport == "unix":
        address = _control_address("proj-real", "sess-real")
        assert address is not None and Path(address).is_absolute()
    else:
        address = f"tcp:127.0.0.1:{_a_free_tcp_port()}"

    env = {
        **{k: v for k, v in os.environ.items()
           if k in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "SSL_CERT_FILE")},
        "PYTHONPATH": str(HARNESS_ROOT),
        "HARNESS_CONTROL_SOCKET": str(address),
        "HARNESS_SPAWN_TOKEN": "tok-real",
        # 真 runtime 起来要有 LLM 配置才不至于在 import 期炸；status 不用它。
        "LLM_API_KEY": "not-used-by-status",
        "LLM_BASE_URL": "http://127.0.0.1:1/v1",
        "LLM_MODEL": "fake",
    }
    # cwd 故意用 harness 根 —— 与 App Server 的 cwd（platform/backend）不同。
    # 地址要是相对的，这里就会分叉，正是那天的病根。
    proc = subprocess.Popen(
        [sys.executable, "-m", "platform_runtime", "--serve"],
        cwd=str(HARNESS_ROOT), env=env,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        channel = await _connect_control_address(
            address, spawn_token="tok-real", timeout_seconds=20,
            still_starting=lambda: proc.poll() is None,
        )
        try:
            await channel.send_line(
                json.dumps({"op": "status", "request_id": "probe-1"}) + "\n"
            )
            raw = await asyncio.wait_for(channel.read_line(), timeout=20)
            frame = json.loads(raw)
        finally:
            channel.close()
    except Exception:
        proc.kill()
        _, err = proc.communicate(timeout=10)
        pytest.fail(f"真 worker 的 socket 不可达；stderr:\n{err.decode()[-2000:]}")
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=10)

    assert frame["type"] == "status", frame
    assert frame["request_id"] == "probe-1"
    # init 之前也答得出 —— 重连体检靠的就是这条。
    assert frame["initialized"] is False
    assert frame["spawn_token"] == "tok-real"
    assert frame["protocol_version"] >= 1


def _worker_environment(address: str, token: str) -> dict[str, str]:
    return {
        **{k: v for k, v in os.environ.items()
           if k in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "SSL_CERT_FILE")},
        "PYTHONPATH": str(HARNESS_ROOT),
        "HARNESS_CONTROL_SOCKET": address,
        "HARNESS_SPAWN_TOKEN": token,
        "LLM_API_KEY": "not-used-by-status",
        "LLM_BASE_URL": "http://127.0.0.1:1/v1",
        "LLM_MODEL": "fake",
    }


@pytest.mark.asyncio
async def test_the_backend_connects_to_the_address_it_handed_out(short_socket_root, monkeypatch):
    """**#926 的验收，收成根因形状**：后端交出去的 tcp 地址就是它随后连的那个。

    以前这里测的是「后端能不能从 worker 嘴里问出内核挑的端口」——那个问题现在**不存在**了：
    地址由后端定下来（`worker_addressing.control_address` 自己挑一个空闲端口），交给
    worker 去绑。没有要问的东西，也就没有会死锁的环。

    地址走**生产那条规则**（`_control_address`），不是测试自己拼的 —— 否则这条测的
    就不是产品在做的事。
    """
    from app.services import harness_sessions as hs

    monkeypatch.setenv("HARNESS_CONTROL_TRANSPORT", "tcp")
    from app.services.harness_contract import worker_addressing
    worker_addressing.cache_clear() if hasattr(worker_addressing, "cache_clear") else None

    address = hs._control_address("proj-tcp", "sess-tcp")
    assert address is not None and address.startswith("tcp:")
    assert not address.endswith(":0"), f"交出去的地址还没落定：{address}"

    token = "tok-handed"
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "platform_runtime", "--serve",
        cwd=str(HARNESS_ROOT), env=_worker_environment(address, token),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        channel = await hs._connect_control_address(
            address, spawn_token=token, timeout_seconds=30,
            still_starting=lambda: proc.returncode is None,
        )
        try:
            await channel.send_line(
                json.dumps({"op": "status", "request_id": "probe-handed"}) + "\n"
            )
            frame = json.loads(await asyncio.wait_for(channel.read_line(), timeout=20))
        finally:
            channel.close()
    finally:
        if proc.returncode is None:
            proc.kill()
        await proc.wait()

    assert frame["type"] == "status", frame
    assert frame["spawn_token"] == token


def test_a_relative_config_never_gets_as_far_as_spawning_a_worker(monkeypatch):
    """**那天那个 bug 的病根，现在在更早一层就被拦掉。**

    原来这里真的起一个 worker，验证"相对配置 + 两个 cwd"仍然连得上 —— 靠的是
    后端把地址 resolve 成绝对再交出去。那修的是症状：resolve 用的还是 cwd，
    只不过恰好两边算出了同一个。同一个病根 8-21 在 Session worktree 上复发，
    43 个会话全部失联。

    现在相对的根在配置层就不合法，所以这个失败组合**构造不出来** —— 连
    spawn 都到不了。这条测试守的就是"到不了"。
    """
    monkeypatch.setattr(settings, "harness_root", str(HARNESS_ROOT))
    monkeypatch.setattr(settings, "harness_control_socket", True)
    monkeypatch.setattr(settings, "harness_socket_root", "data/harness_sockets")
    with pytest.raises(DataRootError):
        _control_address("proj-rel", "sess-rel")


def test_the_address_the_backend_hands_over_is_usable_from_another_cwd(short_socket_root):
    """地址是**跨进程**的契约：换个 cwd 解析出来必须还是同一个文件。"""
    address = _control_address("proj-real", "sess-real")
    assert address is not None
    original = Path.cwd()
    try:
        os.chdir(HARNESS_ROOT)                       # worker 的 cwd
        from_worker_cwd = Path(str(address)).resolve()
        os.chdir(HARNESS_ROOT / "platform" / "backend")   # 后端的 cwd
        from_backend_cwd = Path(str(address)).resolve()
    finally:
        os.chdir(original)
    assert from_worker_cwd == from_backend_cwd


@pytest.mark.asyncio
async def test_a_taken_port_fails_loudly_instead_of_hanging(short_socket_root, monkeypatch):
    """端口被抢走时必须**响亮地失败**——这是「后端定地址」那个窗口的兜底判据。

    后端挑端口和 worker 绑上之间有 ~0.2 秒。窗口本身可以接受，前提是抢占的后果是
    「当场报错、报错里带得出端口」，而不是「静默停在正在启动」——后者正是 #926 的形状。
    """
    from app.services import harness_sessions as hs

    monkeypatch.setenv("HARNESS_CONTROL_TRANSPORT", "tcp")
    address = hs._control_address("proj-taken", "sess-taken")
    assert address is not None
    _, (host, port) = hs._addressing().parse_control_address(address)

    squatter = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    squatter.bind((host, port))          # 抢在 worker 前面占住它
    squatter.listen(1)
    token = "tok-taken"
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "platform_runtime", "--serve",
        cwd=str(HARNESS_ROOT), env=_worker_environment(address, token),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
        squatter.close()

    said = stdout.decode("utf-8", "replace")
    assert "control_socket_unavailable" in said, said[-800:]
    assert str(port) in said, "报错里得说得出是哪个端口"


# ── 命令面握手：双向，且两种传输一份判据 ────────────────────────────────────
#
# 2026-09-15 现场：一条 turn 派出去，60.7 秒后 worker 以 exit 0 退场，事件流里
# 一个字都没有，用户读到「执行进程中途退出了」。真身是握手**单向**：后端写完
# hello 就当连上了，从不看对面认不认；worker 这头认不出就**静默丢掉**连接。
# 于是两边都不报错 —— 后端把 turn 写进一条马上要关的连接，真正的 worker 一直
# 没人接，60 秒宽限（`_NO_WATCHER_GRACE_S`）到点自己收摊。
#
# 而 unix 上**根本没有**这道握手（理由写的是"靠文件权限 0600"）。文件权限答的
# 是"你有权连我吗"，答不了"你是我要找的那个吗" —— 同一个 (project, session) 的
# 控制面路径是确定的，换代、respawn、reattach 都往同一条路上连。


async def _worker_on_a_socket(tmp_path, token: str):
    """真 `platform_runtime --serve`，按生产那条路构造命令面（token 从 env 来）。

    socket 不放 `tmp_path`：pytest 的临时目录带着用例全名，macOS 上直接撞
    `AF_UNIX path too long`（路径上限 ~104 字节）。
    """
    from tests._live_worker import runtime_layout, spawn_worker_async, wait_for_socket

    state_root = runtime_layout(tmp_path, "p-hs", "s-hs")
    sock = Path(tempfile.mkdtemp()) / "hs.sock"
    proc = await spawn_worker_async(state_root, sock, spawn_token=token)
    wait_for_socket(sock, timeout=15)
    return proc, sock


async def test_the_right_token_is_answered_and_the_connection_is_usable(tmp_path):
    from app.services.harness_sessions import _connect_control_address

    proc, sock = await _worker_on_a_socket(tmp_path, "tok-hs")
    try:
        channel = await _connect_control_address(str(sock), spawn_token="tok-hs")
        assert channel.usable, "握手过了却拿不到一条能用的命令面"
        channel.close()
    finally:
        proc.kill()


async def test_a_wrong_token_is_refused_at_once_instead_of_dying_60_seconds_later(tmp_path):
    """判据落在**多快**上：身份不符要当场说，不能熬成一次超时。

    以前这条路是静默的 —— 后端以为连上了，真相 60 秒后才以「执行进程中途
    退出了」的形式出现，而那时现场没有任何线索指回握手。
    """
    from app.services.harness_sessions import (
        _ControlHandshakeRejected,
        _connect_control_address,
    )

    proc, sock = await _worker_on_a_socket(tmp_path, "tok-hs")
    try:
        started = asyncio.get_running_loop().time()
        with pytest.raises(_ControlHandshakeRejected) as caught:
            await _connect_control_address(
                str(sock), spawn_token="not-the-one", timeout_seconds=5.0
            )
        elapsed = asyncio.get_running_loop().time() - started
        assert elapsed < 3.0, f"身份不符熬了 {elapsed:.1f}s —— 它该当场说"
        assert "refused our identity" in str(caught.value)
    finally:
        proc.kill()


async def test_a_backend_that_predates_the_handshake_still_connects_and_keeps_its_first_line(
    tmp_path,
):
    """换代兼容：v1 的后端在 unix 上不发 hello。

    worker 为了认身份读掉的那一行必须**还回去** —— 否则老后端的第一条命令
    凭空消失，而两边都不报错（正是这次要修的那种病的镜像）。
    """
    proc, sock = await _worker_on_a_socket(tmp_path, "tok-hs")
    try:
        reader, writer = await asyncio.open_unix_connection(str(sock))
        # 不报身份，直接发一条真请求（老后端的形状）。
        writer.write(b'{"op":"status","request_id":"req-old-backend"}\n')
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), timeout=15)
        answer = json.loads(line)
        assert answer.get("request_id") == "req-old-backend", (
            f"老后端的第一条请求被握手吃掉了：{line[:200]!r}"
        )
        writer.close()
    finally:
        proc.kill()

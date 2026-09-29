"""换代 respawn 之后，后端连上的必须是**它刚 spawn 的那个** worker，不是路径上碰巧还在的谁。

## 现场（2026-09-15 node20，session 822ee77f）

部署 d852a6f 之后，一个上一代（f82c17f）的 worker 活了下来，新后端把它接回了注册表。
用户发消息 → 指纹不同 → respawn：`op=terminate`（它答了 `terminated`）→ 紧接着 SIGTERM
→ 按"命令行还像不像 worker"判它已死 → spawn 新 worker → **立刻**连同一个地址。

可上一代还在退场：一天的进程，`exit_mm` 要卸掉几 GB 内存，这期间 /proc/pid/cmdline
已经空了（判据说"死了"），监听 socket 却要到 `exit_files` 才关。后端的连接被内核收进
**上一代**的 backlog（accept 线程早已不在），等它真退出时被 reset。新 worker 绑好之后
没有任何人连它，60 秒后按"没人看"自行收摊、退出码 0；后端在 `process.wait()` 上等
到这一刻，报出来的是 "Harness runtime process exited before replying (exit code 0)"。

## 根因

**路径不是身份。** 命令面地址由 (project, session) 算出来，每一代 worker 都绑同一个
路径。连"成功"只证明路径上有个 socket —— 可能是上一代的死文件、上一代还没退干净的
监听、或它留下的 backlog。所以连上之后要过**身份握手**（`_say_hello`，PR#1046）：
对面按自己的 spawn token 认我们，认不出就 `hello_rejected`（当场抛，重试没有意义）；
一声不吭的（上一代的 backlog 正是这样）断开重连，直到我们 spawn 的那个绑上来。

顺带两处：`terminate()` 得到 `terminated` 后先等它自己退场（它会收走自己的 socket
文件），别一转身就 SIGTERM 把它杀在半路；通道先于回答断掉时，报"通道断了、进程还
活着"，别等 60 秒后拿一个不相干的退出码当原因。
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
import time
from pathlib import Path

import pytest

from app.config import settings
from app.services.harness_sessions import (
    HarnessSessionError,
    HarnessSessionProcessError,
    _connect_control_address,
    _control_address,
    _ProjectHarnessSession,
    _SpawnedWorker,
    reattach_session,
)
from tests._live_worker import HARNESS_ROOT, WORKER_SCRIPT, declare_activity


@pytest.fixture(params=["asyncio", "uvloop"])
def event_loop_policy(request):
    """两种事件循环都跑一遍：node20 的 uvicorn 跑在 uvloop 上，本机测试默认是 asyncio。

    连接语义在两者之间有差别（uvloop 走 libuv 的 `uv_pipe_connect`）—— 事故只在
    生产的那一种上出现过，判据必须两种都覆盖。
    """
    if request.param == "uvloop":
        uvloop = pytest.importorskip("uvloop")
        return uvloop.EventLoopPolicy()
    return asyncio.DefaultEventLoopPolicy()


@pytest.fixture
def short_root(monkeypatch):
    """短根（AF_UNIX 有路径长度上限）+ 生产那条地址规则。"""
    root = Path(tempfile.mkdtemp(prefix="hrs-", dir="/tmp"))
    monkeypatch.setattr(settings, "harness_root", str(HARNESS_ROOT))
    monkeypatch.setattr(settings, "harness_control_socket", True)
    monkeypatch.setattr(settings, "harness_socket_root", str(root / "sockets"))
    monkeypatch.setattr(settings, "project_worktree_root", str(root / "wt"))
    from app.services.harness_contract import _harness_root

    _harness_root.cache_clear()
    try:
        yield root
    finally:
        _harness_root.cache_clear()
        shutil.rmtree(root, ignore_errors=True)


def _state_root(root: Path, project_id: str, session_id: str) -> Path:
    state_root = (
        root / "wt" / project_id / session_id / ".research" / "runtime" / "runs"
        / f"orchestrator__{project_id}__session__{session_id}"
    )
    state_root.mkdir(parents=True, exist_ok=True)
    return state_root


def _spawn_previous_generation(state_root: Path, sock: Path, *, token: str) -> subprocess.Popen:
    """上一代 worker：真 socket 源、真锁行、真 serve 循环（tests/_live_worker.py）。"""
    env = {**os.environ, "HARNESS_SPAWN_TOKEN": token, "HARNESS_CONTROL_SOCKET": str(sock)}
    proc = subprocess.Popen(
        [sys.executable, "-c", WORKER_SCRIPT, str(HARNESS_ROOT), str(state_root), str(sock), "--serve"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env, start_new_session=True,
    )
    assert proc.stderr is not None and "ready" in proc.stderr.readline(), f"worker 没起来 (exit={proc.poll()})"
    deadline = time.time() + 5
    while time.time() < deadline and not sock.exists():
        time.sleep(0.02)
    return proc


def _real_worker_env(address: str, token: str) -> dict[str, str]:
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


async def _spawn_real_worker(address: str, token: str) -> asyncio.subprocess.Process:
    """新一代：真 `platform_runtime --serve`，与 `_new_session` 同一种 spawn。"""
    return await asyncio.create_subprocess_exec(
        sys.executable, "-m", "platform_runtime", "--serve",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        cwd=str(HARNESS_ROOT), env=_real_worker_env(address, token), start_new_session=True,
    )


async def _status_over(channel) -> dict:
    await channel.send_line(json.dumps({"op": "status", "request_id": f"probe-{time.time_ns()}"}) + "\n")
    raw = await asyncio.wait_for(channel.read_line(), timeout=10)
    assert raw, "对面没答就把连接关了"
    return json.loads(raw)


async def _kill(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is None:
        proc.kill()
    await proc.wait()


# ── 核心判据：连上的是我们 spawn 的那个 ────────────────────────────────────


async def test_the_backend_ends_up_on_the_worker_it_spawned_not_the_dying_previous_one(short_root):
    """**事故的形状**：同一个路径上，上一代的监听还开着（accept 线程已死，backlog 照收），
    新一代正在起。后端必须连到新一代 —— 不管第一次连进了谁的 backlog。

    上一代用一个裸监听 socket 扮演：绑在生产算出的那个路径上、从不 accept；等新 worker
    重新绑好路径之后它才"退出"（关掉监听 = 内核 reset 它 backlog 里的连接）。这正是
    node20 上那个卡在 `exit_mm` 里的老进程对外表现出来的一切。
    """
    address = _control_address("proj-h", "sess-h")
    assert address is not None
    sock = Path(address)
    sock.parent.mkdir(parents=True, exist_ok=True)
    ghost = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    ghost.bind(str(sock))
    ghost.listen(1)
    ghost_inode = os.stat(sock).st_ino

    new = await _spawn_real_worker(address, "tok-new")

    async def _previous_generation_finally_exits() -> None:
        # 新 worker 把路径重新绑成了自己的 inode 之后，上一代才走完 exit_files。
        deadline = asyncio.get_running_loop().time() + 20
        while asyncio.get_running_loop().time() < deadline:
            try:
                if os.stat(sock).st_ino != ghost_inode:
                    break
            except FileNotFoundError:
                pass                      # unlink 与 bind 之间那一瞬
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.3)          # 它 backlog 里的连接在这 0.3 秒里一直挂着
        ghost.close()

    exit_task = asyncio.create_task(_previous_generation_finally_exits())
    try:
        channel = await _connect_control_address(
            address, spawn_token="tok-new", timeout_seconds=20,
            still_starting=lambda: new.returncode is None,
        )
        try:
            reply = await _status_over(channel)
        finally:
            channel.close()
        assert reply["type"] == "status" and reply["spawn_token"] == "tok-new", reply
    finally:
        await exit_task
        await _kill(new)


async def test_a_live_previous_generation_at_the_same_path_is_not_mistaken_for_ours(short_root):
    """上一代活着、答得出话，可它不是我们 spawn 的那个：spawn token 对不上就拒绝。

    以前连上就算数 —— 于是 `init` 会发给一个正在退场（或根本不归我们管）的进程。
    """
    project_id, session_id = "proj-o", "sess-o"
    address = _control_address(project_id, session_id)
    assert address is not None
    old = _spawn_previous_generation(_state_root(short_root, project_id, session_id), Path(address), token="tok-old")
    try:
        with pytest.raises(HarnessSessionError) as caught:
            await _connect_control_address(
                address, spawn_token="tok-new", timeout_seconds=1.5, still_starting=lambda: True,
            )
        assert caught.value.code == "harness_worker_unreachable"
        assert "spawn token" in str(caught.value), str(caught.value)
        # 拒绝它没有弄坏它：拿对 token 照样连得上、答得出。
        channel = await _connect_control_address(address, spawn_token="tok-old", timeout_seconds=5)
        try:
            assert (await _status_over(channel))["spawn_token"] == "tok-old"
        finally:
            channel.close()
    finally:
        old.kill()
        old.wait(timeout=10)


async def test_a_terminated_worker_takes_its_socket_file_with_it(short_root):
    """`terminate()` 得到 `terminated` 之后等它自己退场 —— 它会收走自己的 socket 文件。

    从前紧接着就 SIGTERM：退出码 -15、文件留在盘上、监听在 `exit_files` 之前还开着 ——
    下一代绑同一个路径时，后端的第一次连接就有地方可去错。
    """
    project_id, session_id = "proj-t", "sess-t"
    address = _control_address(project_id, session_id)
    assert address is not None
    sock = Path(address)
    state_root = _state_root(short_root, project_id, session_id)
    old = _spawn_previous_generation(state_root, sock, token="tok-old")
    try:
        declare_activity(state_root, pid=old.pid, spawn_token="tok-old", state="idle")
        adopted = await reattach_session(project_id, session_id, owner_user_id="u1")
        assert adopted is not None and adopted.process is None, "接回来的会话只有 pid"

        await adopted.terminate()

        assert old.wait(timeout=5) == 0, "该是体面退场（op=terminate → 自己退出），不是被 SIGTERM 掐的"
        assert not sock.exists(), "退场的 worker 没收走自己的 socket 文件"
    finally:
        if old.poll() is None:
            old.kill()
            old.wait(timeout=10)


# ── 通道先于回答断掉：说通道断了，别等一个不相干的退出码 ────────────────────


class _DeadChannel:
    usable = True

    async def send_line(self, line: str) -> None:
        return None

    async def read_line(self) -> bytes:
        return b""                        # 连上了就断：上一代 backlog 里的连接被 reset 的样子

    def close(self) -> None:
        return None


class _ProcessThatKeepsRunning:
    pid = 4242
    returncode = None

    async def wait(self) -> int:
        await asyncio.Event().wait()      # 永远不退出（新 worker 要 60 秒后才自行收摊）
        raise AssertionError("unreachable")


async def test_a_connection_that_dies_before_the_reply_is_reported_as_such(monkeypatch):
    """node20 上后端等了 60 秒，然后把新 worker 的 `exit 0` 当成失败原因报出来。

    判据落在两件事上：**多久**（有界，不等进程退出）和**说什么**（通道断了、进程还活着）。
    """
    monkeypatch.setattr(_ProjectHarnessSession, "_EXIT_CODE_WAIT_S", 0.5)

    async def _no_stderr() -> str:
        return ""

    process = _ProcessThatKeepsRunning()
    session = _ProjectHarnessSession(
        project_id="p", session_id="s", owner_user_id="u", backend_id="b",
        backend_fingerprint="fp", platform_context_hash=None,
        process=process, stderr_task=asyncio.create_task(_no_stderr()),
        provider_secrets=(), spawn_token="tok", channel=_DeadChannel(),
        worker=_SpawnedWorker(process),
    )

    async def _noop(_event: dict) -> None:
        return None

    started = time.monotonic()
    with pytest.raises(HarnessSessionProcessError) as caught:
        await session._rpc_locked(
            {"op": "status", "request_id": "probe"}, terminal_types={"status"},
            on_progress=_noop, on_protocol_event=_noop, timeout_seconds=30,
        )
    elapsed = time.monotonic() - started
    assert elapsed < 5.0, f"等了 {elapsed:.1f}s —— 还在等一个不会退出的进程"
    assert caught.value.code == "harness_worker_unreachable"
    assert caught.value.exit_code is None
    assert "still running" in str(caught.value) and "control connection closed" in str(caught.value)

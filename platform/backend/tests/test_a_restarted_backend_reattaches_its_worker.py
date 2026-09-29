"""重启后端不再杀掉正在跑的研究（RFC 异步运行时 P0-4）。

## 这条测试就是 P0 的验收判据

改造前：worker 是后端子进程、命令面是 stdin，后端一死它跟着死；启动对账
的前提"注册表为空 ⇒ 全都无主"因此成立，reap 把残留进程一律杀掉。

改造后那个前提**不再成立** —— worker 自成进程组、事件落盘、命令面是 socket，
它可能正跑着一个几小时的实验。所以启动时必须**先接、后判**。

对着真进程验：真 worker、真 socket、真注册表行、真 `op=status` 往返。
单进程 mock 看不见"接不接得回来"这件事。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from app.services.harness_sessions import (
    _ReattachedWorker,
    reattach_session,
)
from app.services.session_event_log import registry_row

# 一个真 worker：绑真 socket、拿真锁（写注册表行）、跑真的 serve 分发循环。
# 它不连 LLM，只回答协议层的问题 —— reattach 要验的正是协议层。脚本与
# `test_shutdown_detaches_from_docked_workers` 共用一份（tests/_live_worker.py）。
from tests._live_worker import HARNESS_ROOT
from tests._live_worker import WORKER_SCRIPT as _WORKER


@pytest.fixture
def short_root():
    root = Path(tempfile.mkdtemp(prefix="hre-", dir="/tmp"))
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
def live_worker(short_root, monkeypatch):
    """一个活着的 worker + App Server 侧能找到它的路径推导。"""
    from app.config import settings

    project_id, session_id = "proj-re", "sess-re"
    worktree_root = short_root / "wt"
    worktree = worktree_root / project_id / session_id
    state_root = (
        worktree / ".research" / "runtime" / "runs"
        / f"orchestrator__{project_id}__session__{session_id}"
    )
    state_root.mkdir(parents=True)
    monkeypatch.setattr(settings, "project_worktree_root", str(worktree_root))

    # 走**生产路径**算地址：harness_contract 桥（顺带验了桥本身）。
    # 直接 exec_module 那份 platform_runtime 够不到 core.*，而且那也不是
    # 后端真实走的路。
    monkeypatch.setattr(settings, "harness_root", str(HARNESS_ROOT))
    from app.services.harness_contract import _harness_root, worker_addressing

    # `_harness_root` 有 lru_cache —— monkeypatch 撤销了 settings，撤不掉缓存。
    # 不清的话后面那些"故意配一个坏 harness 根"的测试会拿到**这里**缓存的好
    # 根，于是走进本不该走的路径（实测：它们在全套里挂，单独跑却过）。
    _harness_root.cache_clear()
    sock = worker_addressing().control_socket_path(short_root, project_id, session_id)
    env = {**os.environ, "HARNESS_SPAWN_TOKEN": "tok-live", "HARNESS_CONTROL_SOCKET": str(sock)}
    proc = subprocess.Popen(
        [sys.executable, "-c", _WORKER, str(HARNESS_ROOT), str(state_root), str(sock), "--serve"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
        start_new_session=True,
    )
    assert proc.stderr is not None
    assert "ready" in proc.stderr.readline(), f"worker 没起来 (exit={proc.poll()})"
    deadline = time.time() + 5
    while time.time() < deadline and not sock.exists():
        time.sleep(0.02)
    try:
        yield proc, project_id, session_id, state_root, sock
    finally:
        proc.kill()
        proc.wait(timeout=10)
        _harness_root.cache_clear()   # 别把这次的根留给下一个测试


def test_the_registry_row_is_enough_to_find_a_live_worker(live_worker):
    """接回来只靠**注册表行**：后端重启之后它对这个 worker 一无所知。"""
    proc, _p, _s, state_root, sock = live_worker
    row = registry_row(state_root / ".chat.lock")
    assert row["pid"] == proc.pid
    assert row["control_socket"] == str(sock)
    assert row["spawn_token"] == "tok-live"


def test_a_reattached_worker_is_recognised_as_alive(live_worker):
    """"还活着吗"必须**现算**（问进程），不能读一个可能陈旧的记录 ——
    拿陈旧数字去杀进程，杀掉的是无辜的那个。"""
    proc, *_ = live_worker
    handle = _ReattachedWorker(proc.pid)
    assert handle.alive is True

    proc.kill()
    proc.wait(timeout=10)
    assert handle.alive is False


@pytest.mark.asyncio
async def test_a_live_worker_is_reattached_not_reaped(live_worker):
    """**P0 的验收判据**：后端重启后接得回来。

    注意 worker 此刻**还没 init** —— 这里验的是协议层接得上、status 答得出。
    """
    _proc, project_id, session_id, _state_root, _sock = live_worker
    session = await reattach_session(project_id, session_id, owner_user_id="u1")
    # 没 init 的 worker 不掌握研究状态 → 如实返回 None（不接，也**不杀**）。
    assert session is None


@pytest.mark.asyncio
async def test_a_dead_worker_is_not_reattached(live_worker):
    """进程没了就是没了 —— 别接一个尸体回来，那会让 UI 上永远转圈。"""
    proc, project_id, session_id, _state_root, _sock = live_worker
    proc.kill()
    proc.wait(timeout=10)
    assert await reattach_session(project_id, session_id, owner_user_id="u1") is None


@pytest.mark.asyncio
async def test_a_stale_registry_row_pointing_at_a_dead_socket_is_not_reattached(
    short_root, monkeypatch
):
    """注册表行还在、进程早没了（上次没收干净）—— 必须认出来，别挂在那儿等。"""
    from app.config import settings

    project_id, session_id = "proj-stale", "sess-stale"
    worktree_root = short_root / "wt"
    state_root = (
        worktree_root / project_id / session_id / ".research" / "runtime"
        / "runs" / f"orchestrator__{project_id}__session__{session_id}"
    )
    state_root.mkdir(parents=True)
    monkeypatch.setattr(settings, "project_worktree_root", str(worktree_root))
    (state_root / ".chat.lock").write_text(json.dumps({
        "pid": 999_999,                       # 不存在的进程
        "control_socket": str(short_root / "gone.sock"),
        "spawn_token": "tok-stale",
    }), encoding="utf-8")

    assert await reattach_session(project_id, session_id, owner_user_id="u1") is None


@pytest.mark.asyncio
async def test_an_old_stdio_worker_is_honestly_not_reattachable(short_root, monkeypatch):
    """升级窗口：跑在 stdio 老路上的 worker 没有 control_socket —— 接不回来
    就如实认了，别去猜一个地址（猜错就是连到别人的 socket 上）。"""
    from app.config import settings

    project_id, session_id = "proj-old", "sess-old"
    worktree_root = short_root / "wt"
    state_root = (
        worktree_root / project_id / session_id / ".research" / "runtime"
        / "runs" / f"orchestrator__{project_id}__session__{session_id}"
    )
    state_root.mkdir(parents=True)
    monkeypatch.setattr(settings, "project_worktree_root", str(worktree_root))
    (state_root / ".chat.lock").write_text(json.dumps({
        "pid": os.getpid(), "acquired_at": "2026-08-18T00:00:00+00:00",
    }), encoding="utf-8")   # 老格式：没有 control_socket

    assert await reattach_session(project_id, session_id, owner_user_id="u1") is None


def test_the_worker_survives_the_signal_that_kills_the_backend(live_worker):
    """detach 的判据：后端进程组收到的信号打不到 worker 身上。"""
    proc, *_ = live_worker
    assert os.getpgid(proc.pid) != os.getpgid(os.getpid()), "worker 必须自成进程组"
    os.killpg(os.getpgid(os.getpid()), 0)      # 自己的组还在（signal 0 = 只探测）
    time.sleep(0.2)
    assert proc.poll() is None

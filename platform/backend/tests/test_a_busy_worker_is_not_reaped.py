"""**跑轮中**的 worker 也接得回来（RFC 异步运行时 P0-4，2026-08-23 修）。

## 原来的验收判据验的不是那件事

`test_a_restarted_backend_reattaches_its_worker` 里那条名为"P0 的验收判据"的
测试，用的是一个**还没 init** 的 worker，并断言 `reattach_session` 返回 None。
它验的是"协议层连得上"，不是"正在做研究的 worker 活得过后端重启"。

而后者恰好是失败的：reattach 无条件发 `op=status` 等 10 秒，可 worker 的命令
循环在一轮正在跑时是堵住的（`await session.turn(...)` 一跑几小时，命令面
多路复用是 P1 的事）。于是 ——

    正在跑几小时实验的 worker → status 超时 → 接不回来 → 判无主
      → 启动对账 SIGTERM 掉它

**恢复路径恰好在它存在的那个场景里失效**：闲着的接得回来，干活的接不回来。
"部署不打断科研"这个承诺因此从来没有真正成立过，而测试全绿。

## 所以这里的 worker 必须是"命令循环堵住"的那种

真进程、真锁、真 socket、真 accept 线程 —— 只是**不读**命令。这是一轮长
research turn 在协议层的准确形状。单进程 mock 造不出这个形状：它没有那个
"listen 得到、答不上话"的中间态。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from app.services.harness_sessions import read_worker_activity, reattach_session

HARNESS_ROOT = Path(__file__).resolve().parents[3]

#: 一个**正在干活**的 worker：拿着锁、绑着 socket、自报 working，但命令循环
#: 堵死（模拟 `await session.turn(...)` 跑一小时）。它还在按心跳更新活动文件
#: —— 真 worker 的心跳挂在事件产出上，跑轮中事件最密。
_BUSY_WORKER = """
import pathlib, sys, time
sys.path.insert(0, sys.argv[1])
import platform_runtime as pr
from core.worker_activity import ActivityWriter, activity_path

state_root = pathlib.Path(sys.argv[2])
sock = pathlib.Path(sys.argv[3])
emit = pr.JsonlEmitter(sys.stdout, pr.SecretFilter([]))
with pr._project_lock(state_root):
    # 生产那条构造路 —— 命令面的身份握手从 env 拿 token。直接 new
    # SocketRequestSource 会少传 token，夹具的 worker 就不做身份校验，而线上做。
    source = pr._control_request_source(str(sock), emit.rebind_stream)
    activity = ActivityWriter(
        activity_path(state_root),
        spawn_token="tok-busy",
        command=list(sys.argv),
    )
    activity.set_state(
        "working",
        detail={"operation": "turn"},
        turn_id="turn-in-flight",
        app_binding={
            "user_id": "u-owner",
            "conversation_id": "conv-1",
            "run_id": "run-in-flight",
            "session_id": "sess-busy",
        },
    )
    sys.stderr.write("ready\\n"); sys.stderr.flush()
    while True:                      # 命令循环堵住 —— 谁也别想收到回答
        time.sleep(0.05)
        activity.touch()
"""

#: 同一个 worker，但**不自报活动**（换代期的老 worker）。
_OLD_BUSY_WORKER = _BUSY_WORKER.replace('activity.set_state(', 'None and activity.set_state(')


@pytest.fixture
def short_root():
    root = Path(tempfile.mkdtemp(prefix="hbw-", dir="/tmp"))
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _spawn(script: str, short_root: Path, monkeypatch, *, session_id: str):
    from app.config import settings
    from app.services.harness_contract import _harness_root, worker_addressing

    project_id = "proj-busy"
    worktree_root = short_root / "wt"
    worktree = worktree_root / project_id / session_id
    state_root = (
        worktree / ".research" / "runtime" / "runs"
        / f"orchestrator__{project_id}__session__{session_id}"
    )
    state_root.mkdir(parents=True)
    monkeypatch.setattr(settings, "project_worktree_root", str(worktree_root))
    monkeypatch.setattr(settings, "harness_root", str(HARNESS_ROOT))
    _harness_root.cache_clear()
    sock = worker_addressing().control_socket_path(short_root, project_id, session_id)
    env = {**os.environ, "HARNESS_SPAWN_TOKEN": "tok-busy", "HARNESS_CONTROL_SOCKET": str(sock)}
    proc = subprocess.Popen(
        # 末尾那个 `--serve` 不是摆设：`_process_is_a_runtime_worker` 靠命令行
        # 认身份，少了它这个进程在后端眼里就不是一个 runtime worker。
        [sys.executable, "-c", script, str(HARNESS_ROOT), str(state_root), str(sock), "--serve"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
        start_new_session=True,
    )
    assert proc.stderr is not None
    assert "ready" in proc.stderr.readline(), f"worker 没起来 (exit={proc.poll()})"
    deadline = time.time() + 5
    while time.time() < deadline and not sock.exists():
        time.sleep(0.02)
    return proc, project_id, session_id, state_root


@pytest.fixture
def busy_worker(short_root, monkeypatch):
    proc, *rest = _spawn(_BUSY_WORKER, short_root, monkeypatch, session_id="sess-busy")
    try:
        yield (proc, *rest)
    finally:
        proc.kill()
        proc.wait(timeout=10)
        from app.services.harness_contract import _harness_root

        _harness_root.cache_clear()


@pytest.fixture
def old_busy_worker(short_root, monkeypatch):
    proc, *rest = _spawn(_OLD_BUSY_WORKER, short_root, monkeypatch, session_id="sess-old")
    try:
        yield (proc, *rest)
    finally:
        proc.kill()
        proc.wait(timeout=10)
        from app.services.harness_contract import _harness_root

        _harness_root.cache_clear()


def test_a_busy_worker_still_says_what_it_is_doing(busy_worker):
    """自报是**落在盘上**的，所以问它不需要它有空回答。"""
    _proc, project_id, session_id, _state_root = busy_worker
    activity = read_worker_activity(project_id, session_id)
    assert activity is not None, "活动文件必须在 —— 它是整条恢复路径的地基"
    assert activity.state == "working"
    assert activity.turn_id == "turn-in-flight"
    assert activity.occupied is True


@pytest.mark.asyncio
async def test_a_worker_mid_turn_is_reattached(busy_worker):
    """**真正的 P0 验收判据**：一个答不上话的 worker，照样接得回来。"""
    _proc, project_id, session_id, _state_root = busy_worker
    session = await reattach_session(project_id, session_id, owner_user_id="u-owner")
    assert session is not None, (
        "跑轮中的 worker 没接回来 —— 启动对账下一步就会把它 SIGTERM 掉，"
        "而它正在跑的是一个几小时的实验"
    )
    assert session.reattached_status.get("from_activity") is True
    session.channel.close()


@pytest.mark.asyncio
async def test_reattach_restores_the_binding_so_the_run_still_has_an_owner(busy_worker):
    """接回来必须连**绑定**一起接。

    只接 socket 是半截的：`live_binding()` 会返回 None，
    `mark_orphaned_harness_runs` 照旧判这条 run 无主，并把刚接回来的 worker
    杀掉 —— 接回来的下一秒被自己人杀死，比接不回来更难查。
    """
    _proc, project_id, session_id, _state_root = busy_worker
    session = await reattach_session(project_id, session_id, owner_user_id="u-owner")
    assert session is not None
    assert session.binding is not None, "绑定没接回来 = 这条 run 在账面上没有主"
    assert session.binding.run_id == "run-in-flight"
    assert session.binding.user_id == "u-owner"
    session.channel.close()


@pytest.mark.asyncio
async def test_an_old_worker_without_self_report_is_honestly_not_reattached(old_busy_worker):
    """换代期的老 worker 跑轮中确实接不回来 —— 如实认了，不假装。

    这条同时是上面那条的**对照**：证明救回来的是活动自报，不是别的什么
    碰巧生效的东西。
    """
    _proc, project_id, session_id, _state_root = old_busy_worker
    assert read_worker_activity(project_id, session_id) is None
    assert await reattach_session(project_id, session_id, owner_user_id="u-owner") is None

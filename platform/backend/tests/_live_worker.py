"""一个真 worker，给"后端与 worker 分离"那一族测试共用。

绑真 socket、拿真锁（写注册表行）、跑真的 serve 分发循环、事件真落盘
（events.jsonl）。它不连 LLM，只回答协议层的问题 —— reattach / detach 要验的
正是协议层。脚本只有这一份：抄两份就会各自演化，而分叉时两边都不报错。
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from contextlib import suppress
from pathlib import Path

HARNESS_ROOT = Path(__file__).resolve().parents[3]

#: 一个真 worker：绑真 socket、拿真锁（写注册表行）、跑真的 serve 分发循环。
#: 命令行里同时有 platform_runtime 与 --serve —— App Server 的身份判据认它。
WORKER_SCRIPT = """
import asyncio, json, os, sys, pathlib, threading, time
sys.path.insert(0, sys.argv[1])
import platform_runtime as pr

state_root = pathlib.Path(sys.argv[2])
sock = pathlib.Path(sys.argv[3])
emit = pr.JsonlEmitter(sys.stdout, pr.SecretFilter([]))

def _follow_cues(cue_dir):
    # 测试的"提词器"：把 cue 文件里的事件经**真的** JsonlEmitter 发出去 ——
    # 先落 events.jsonl、再走当前 socket（同一把锁、同一份正文），和 worker 跑
    # 一轮时发 transcript / result 走的是同一条路。这里不模拟研究，只模拟
    # "worker 在某个时刻说了这句话"。
    seen = set()
    while True:
        try:
            for path in sorted(cue_dir.glob("*.json")):
                if path in seen:
                    continue
                seen.add(path)
                cue = json.loads(path.read_text(encoding="utf-8"))
                event = dict(cue.get("emit") or {})
                event_type = event.pop("type")
                emit(event_type, **event)
        except Exception as exc:  # noqa: BLE001 - 提词器坏了要看得见
            sys.stderr.write(f"cue error: {exc!r}\\n"); sys.stderr.flush()
        time.sleep(0.02)

with pr._project_lock(state_root):
    emit.attach_durable_sink(pr.session_events_path(state_root))
    # 走**生产那条构造路**（`_control_request_source`）：命令面的 spawn_token
    # 从 env 来，身份握手因此和线上一样在场。直接 new SocketRequestSource 会
    # 少传 token，于是这个夹具造出来的 worker 根本不做身份校验 —— 而线上做。
    # 2026-09-16 实测：正是这条差异让握手的缺口在测试里看不见。
    source = pr._control_request_source(str(sock), emit.rebind_stream)
    cue_dir = os.environ.get("LIVE_WORKER_CUE_DIR")
    if cue_dir:
        threading.Thread(target=_follow_cues, args=(pathlib.Path(cue_dir),), daemon=True).start()
    sys.stderr.write("ready\\n"); sys.stderr.flush()
    try:
        asyncio.run(pr.serve_jsonl(source, emit))
    finally:
        source.close()
"""


def worker_argv(state_root: Path, sock: Path) -> list[str]:
    return [
        sys.executable, "-c", WORKER_SCRIPT,
        str(HARNESS_ROOT), str(state_root), str(sock), "--serve",
    ]


def worker_env(sock: Path, *, spawn_token: str, cue_dir: Path | None = None) -> dict[str, str]:
    env = {**os.environ, "HARNESS_SPAWN_TOKEN": spawn_token, "HARNESS_CONTROL_SOCKET": str(sock)}
    if cue_dir is not None:
        env["LIVE_WORKER_CUE_DIR"] = str(cue_dir)
    return env


def cue(cue_dir: Path, **event: object) -> Path:
    """让 worker 经真 emitter 说一句话（见 WORKER_SCRIPT 的 `_follow_cues`）。

    `event` 里必须有 `type`（如 "transcript" / "result"），其余字段原样进事件。
    文件名带序号，worker 按名字顺序消费 —— 两条 cue 的先后就是事件的先后。
    """
    cue_dir.mkdir(parents=True, exist_ok=True)
    index = len(list(cue_dir.glob("*.json")))
    path = cue_dir / f"{index:04d}.json"
    path.write_text(json.dumps({"emit": event}, ensure_ascii=False), encoding="utf-8")
    return path


def runtime_layout(worktree_root: Path, project_id: str, session_id: str) -> Path:
    """App Server 侧路径推导会去找的 state_root（与 `_session_runtime_dir` 同一布局）。"""
    return (
        worktree_root / project_id / session_id / ".research" / "runtime" / "runs"
        / f"orchestrator__{project_id}__session__{session_id}"
    )


def wait_for_socket(sock: Path, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline and not Path(sock).exists():
        time.sleep(0.02)


def spawn_worker(
    state_root: Path, sock: Path, *, spawn_token: str = "tok-live", cue_dir: Path | None = None
) -> subprocess.Popen:
    """同步版：给不需要 asyncio 句柄的测试。"""
    proc = subprocess.Popen(
        worker_argv(state_root, sock),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env=worker_env(sock, spawn_token=spawn_token, cue_dir=cue_dir),
        start_new_session=True,
    )
    assert proc.stderr is not None
    line = proc.stderr.readline()
    assert "ready" in line, f"worker 没起来: {line!r} (exit={proc.poll()})"
    wait_for_socket(sock)
    return proc


async def spawn_worker_async(
    state_root: Path, sock: Path, *, spawn_token: str = "tok-live", cue_dir: Path | None = None
):
    """与 `_new_session` **同一条 spawn**：`_spawn_child_owned_by_the_os`。

    这句话以前是假的。它曾经写着"与 `_new_session` 同一种 spawn"，而底下是
    `asyncio.create_subprocess_exec` —— PR#1040 把生产那条换成 `subprocess.Popen`
    （孩子归操作系统，不归事件循环）之后，这里就分叉了，而**注释没跟着改**。

    代价是具体的：`_SpawnedWorker.release()` 调的是 `close_pipes()`，只有
    `_ChildOwnedByTheOS` 有。夹具递给它一个 `asyncio.subprocess.Process`，
    `detach` 当场 `AttributeError`，被收尾那层吞成一行
    「收尾放手：0/1 个 worker 放开了」；孩子随后会不会死，取决于 asyncio 的
    transport 什么时候被 GC —— Linux 的 CI 上会，本机 macOS 上不会。于是
    `test_a_docked_worker_survives_backend_shutdown` 在 CI 上间歇红、本机怎么跑
    都绿，被当成"幽灵容器抢 CPU 的时序抖动"查了好几轮。

    夹具按非生产的方式造被测对象，钉在它上面的判据验的就是别的东西
    （[[feedback_test_double_hides_the_unit]]）。
    """
    from app.services.harness_sessions import _spawn_child_owned_by_the_os

    proc = await _spawn_child_owned_by_the_os(
        worker_argv(state_root, sock),
        cwd=str(HARNESS_ROOT),
        env=worker_env(sock, spawn_token=spawn_token, cue_dir=cue_dir),
        limit=64 * 1024,
        start_new_session=True,
    )
    assert proc.stderr is not None
    line = await asyncio.wait_for(proc.stderr.readline(), timeout=10)
    assert b"ready" in line, f"worker 没起来: {line!r} (exit={proc.returncode})"
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 5
    while loop.time() < deadline and not Path(sock).exists():
        await asyncio.sleep(0.02)
    return proc


def declare_activity(
    state_root: Path,
    *,
    pid: int,
    spawn_token: str,
    state: str,
    app_binding: dict | None = None,
    detail: dict | None = None,
    turn_id: str = "",
) -> None:
    """替 worker 写一份活动自报（D10）—— 走契约桥用真的写入面，格式不抄。

    `turn_id` = 它正在跑的那次 RPC 的 request_id（working / parked 时才有）。
    """
    from app.services.harness_contract import worker_activity

    module = worker_activity()
    writer = module.ActivityWriter(
        module.activity_path(state_root), pid=pid, spawn_token=spawn_token
    )
    writer.set_state(state, detail=detail, app_binding=app_binding, turn_id=turn_id)


def event_types(state_root: Path) -> list[str]:
    """worker 落盘的事件类型序列（events.jsonl）。"""
    path = state_root / "events.jsonl"
    if not path.is_file():
        return []
    return [
        str(json.loads(line).get("type") or "")
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def kill_if_alive(pid: int) -> None:
    with suppress(ProcessLookupError, PermissionError):
        os.kill(pid, signal.SIGKILL)
    # 不是我们的孩子（已放手 / 被 init 收养）时 waitpid 会报 ChildProcessError —— 无妨。
    with suppress(ChildProcessError, OSError):
        os.waitpid(pid, 0)

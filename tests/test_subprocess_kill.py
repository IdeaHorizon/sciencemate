"""超时必须真能收场（E2E-5 双僵死的两条根因）。

现场：agent 传的 timeout 只有 15s / 130s —— **它没做错**。超时按时触发了，
但工具仍然 15.5 小时不返回，因为两条独立缺陷叠加：

  A. `kill_fn` 杀不到人：`os.killpg(os.getpgid(proc.pid), SIGKILL)` 在 kill
     的时刻才取 pgid，而 `bash -c "A && B &"` 的直接 bash **早退出了**
     （fork 出子 shell 跑 `&&` 串、echo 完就走）→ ProcessLookupError 被静默
     吞掉 → **什么都没杀**。
  B. 杀完的收尾是无界 await：管道写端还被后代攥着 → EOF 永远不来 →
     超时分支自己挂死。

两条缺一不可，也各自足以致命。这里两条都断言。
"""
from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import time

import pytest

from core.bootstrap import bootstrap
from shared.lib.cancellable_subprocess import group_killer

pytestmark = pytest.mark.skipif(not hasattr(os, "killpg"), reason="POSIX process-group regression; Windows Job Objects are tested by test_native_jobs")

bootstrap()


def live_group_members(pgid: int) -> list[int]:
    """进程组里**还活着**的成员 —— 僵尸不算活。

    别拿 `os.killpg(pgid, 0)` 探活：僵尸（已被 SIGKILL、但还没被父进程回收）
    仍然是进程组成员，signal 0 照样成功 → 判成"没杀干净"。谁来回收取决于
    环境：本机 macOS 上孤儿归 launchd、瞬间就没；CI 容器里 PID 1 是 job 自己
    的 shell、不收养回收孤儿，于是被杀的后台进程以 Z 态长期赖在组里。
    同一份代码"本机全绿、CI 必挂"就是这么来的（CI 红了 43 次）。

    `ps -A -o pid=,pgid=,stat=` 在 macOS / Linux 上都可用。
    """
    out = subprocess.run(["ps", "-A", "-o", "pid=,pgid=,stat="],
                         capture_output=True, text=True).stdout
    live: list[int] = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        pid, pg, stat = parts[0], parts[1], parts[2]
        if pg.isdigit() and int(pg) == pgid and not stat.startswith("Z"):
            live.append(int(pid))
    return live

# 真实形状：后台进程继承管道，直接 bash 立刻退出。就是起 vLLM 服务那条命令。
HOLDS_PIPE = "sleep 60 & echo started"
class _S:
    kill_event = None


async def _spawn(cmd: str):
    return await asyncio.create_subprocess_exec(
        "/bin/bash", "-o", "pipefail", "-c", cmd,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True)


# ── 缺陷 A：pgid 必须在 spawn 时取 ─────────────────────────────────────────

def test_late_getpgid_kills_nothing():
    """复刻原实现：kill 时才取 pgid → 直接子进程已退出 → 什么都没杀。

    这条**断言 bug 存在**，是缺陷 A 的现场证据（不是断言我们的行为）。
    """
    async def go():
        proc = await _spawn(HOLDS_PIPE)
        # `start_new_session=True` → 子进程是组长 → pgid == pid。
        #
        # 这里原来写 `os.getpgid(proc.pid)`，和被测的旧实现犯同一个错：直接
        # bash 可能已经退出并被 asyncio 的 watcher 回收，这一句就抛。16 路
        # 并发下 16 次红 3 次 —— 而它抛在**准备阶段**，跟这条测试要证的事
        # 毫无关系。
        pgid_at_spawn = proc.pid
        await asyncio.sleep(0.5)          # 直接 bash 早退出了
        late_failed = False
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except ProcessLookupError:
            late_failed = True            # ← 原代码在这里 `pass` 掉了
        # 收尾清理 —— **不能有判决权**。
        #
        # 原来这句是裸的：整组恰好都已退出时它抛 ProcessLookupError，测试就
        # 红在一个跟"晚取 pgid 杀不到人"毫无关系的原因上（CI 抓到过一次，
        # 本机全套复现不出来）。同一个文件里下面那处清理是护着的，这处漏了。
        #
        # 清理失败只说明"要清的东西已经没了"，那正是我们想要的终局。
        try:
            os.killpg(pgid_at_spawn, signal.SIGKILL)
        except ProcessLookupError:
            pass
        return late_failed

    assert asyncio.run(go()) is True, "前提：晚取 pgid 确实会 ProcessLookupError"


def test_group_killer_does_not_ask_the_os_for_the_pgid():
    """pgid 必须由构造推出，不能靠 `os.getpgid` —— 那个答案会消失。

    变异对照：把 `os.getpgid` 打成必抛。旧实现在这里退化成 `pgid=None` →
    只杀直接子进程 → 攥着管道的后代活下来，也就是缺陷 A 本身。
    """
    async def go():
        proc = await _spawn(HOLDS_PIPE)
        pgid = proc.pid
        original = os.getpgid

        def _vanished(_pid):
            raise ProcessLookupError(3, "No such process")

        os.getpgid = _vanished
        try:
            kill = group_killer(proc)
        finally:
            os.getpgid = original
        # 先等直接 bash 退出、后台 sleep 真的挂上去 —— 否则杀的是一棵还没长
        # 出来的树，旧实现也会"通过"（第一版就是这么绿的）。
        await asyncio.sleep(0.5)
        kill()
        await asyncio.sleep(0.3)
        return live_group_members(pgid)

    assert asyncio.run(go()) == [], "getpgid 不可用时整棵子树仍须被杀干净"


def test_group_killer_captures_pgid_at_spawn():
    """我们的 group_killer 在构造时就把 pgid 记下 —— 之后照样杀得到。"""
    async def go():
        proc = await _spawn(HOLDS_PIPE)
        kill = group_killer(proc)         # 此刻记下 pgid
        pgid = proc.pid                   # 组长的 pgid 就是它的 pid（同上）
        await asyncio.sleep(0.5)          # 直接 bash 退出
        kill()
        await asyncio.sleep(0.3)
        return live_group_members(pgid)   # 僵尸不算活，见函数注释

    assert asyncio.run(go()) == [], "整组必须被杀干净（含后台 sleep）"


def test_group_killer_survives_dead_process():
    """进程早没了 → kill 是 no-op，不抛。"""
    async def go():
        proc = await _spawn("echo hi")
        kill = group_killer(proc)
        await proc.wait()
        await asyncio.sleep(0.2)
        kill()                            # 不许抛
        return True

    assert asyncio.run(go()) is True


# ── 接线：框架自带的两个工具真的用上了 ────────────────────────────────────

def test_framework_tools_use_group_kill():
    """builtin.run_bash / execute_python 必须 start_new_session + group_killer。

    只测 group_killer 本身的话，把这两个调用点改回 proc.kill 测试照样全绿。
    """
    import inspect

    # 进程组 kill 现在收在 spawn_and_wait 内部（工具侧不再各写一遍）
    from shared.lib import cancellable_subprocess as cs
    from shared.tools import builtin
    from shared.tools.library import python_exec
    src = inspect.getsource(cs.spawn_and_wait)
    # 进程组的机制在 process_control（POSIX setsid / Windows Job）：起的时候用它的
    # spawn 关键字，杀的时候用 group_killer —— 两头都得接上。
    assert "group_killer(proc)" in src and "process_control.group_spawn_kwargs()" in src
    for mod, name in ((builtin, "run_bash"), (python_exec, "execute_python")):
        assert "kill_fn=proc.kill" not in inspect.getsource(mod), \
            f"{name} 还留着只杀直接子进程"


# ── 判据修正：等进程退出，不等管道 ────────────────────────────────────────
#
# 前面几条断言的是"挂死时能收场"（兜底）。这一组断言的是**根因修好了**：
# 起后台服务应当**立刻成功返回**，而不是拖到超时。

import pytest as _pytest

from shared.lib.cancellable_subprocess import spawn_and_wait

BG_SHAPES = [
    "cd /tmp && nohup sleep 120 > /dev/null 2>&1 & echo started",   # 事故原形
    "sleep 120 & echo started",                                      # 裸继承管道
    "( cd /tmp && sleep 120 ) & echo started",                       # 显式子 shell
]


@_pytest.mark.parametrize("cmd", BG_SHAPES)
def test_background_launch_returns_immediately(cmd):
    """起后台任务 = 命令本身瞬间完成，工具就该瞬间返回 success。

    老实现在这里会一直等到管道 EOF（后台进程 120 秒后才放手），实测挂 15.5h。
    """
    async def go():
        t0 = time.monotonic()
        status, rc, out, err = await spawn_and_wait(
            cmd, state=_S(), timeout=30, shell=True)
        return status, rc, out, time.monotonic() - t0

    status, rc, out, elapsed = asyncio.run(go())
    assert status == "done", f"应正常完成，实际 {status}"
    assert rc == 0
    assert b"started" in out
    assert elapsed < 3, f"应立刻返回，实际 {elapsed:.1f}s —— 又在等管道了"


def test_output_is_captured_from_file_not_pipe():
    async def go():
        return await spawn_and_wait("echo hello; echo bad >&2",
                                    state=_S(), timeout=10, shell=True)

    status, rc, out, err = asyncio.run(go())
    assert status == "done" and rc == 0
    assert b"hello" in out and b"bad" in err


def test_real_timeout_still_works():
    """真的跑太久（前台命令）仍然超时 —— 别把超时能力一起改没了。"""
    async def go():
        t0 = time.monotonic()
        status, rc, _, _ = await spawn_and_wait(
            "sleep 60", state=_S(), timeout=1, shell=True)
        return status, time.monotonic() - t0

    status, elapsed = asyncio.run(go())
    assert status == "timeout"
    assert elapsed < 10


def test_nonzero_exit_is_reported():
    async def go():
        return await spawn_and_wait("exit 3", state=_S(), timeout=10, shell=True)

    status, rc, _, _ = asyncio.run(go())
    assert status == "done" and rc == 3


def test_both_tools_wait_on_process_not_pipe():
    """两个框架工具真的换过来了（接线断言）。"""
    import inspect

    from shared.tools import builtin
    from shared.tools.library import python_exec

    for mod, name in ((builtin, "run_bash"), (python_exec, "execute_python")):
        src = inspect.getsource(mod)
        assert "spawn_and_wait(" in src, f"{name} 没改用 spawn_and_wait"
        assert "subprocess.PIPE" not in src, f"{name} 还在接管道"
        assert "race_communicate" not in src, f"{name} 还在等管道 EOF"

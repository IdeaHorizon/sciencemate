"""`shared.lib.process_control`：两个平台同一份测试，真起进程。

孙进程用 Python 自己起（不用 sh），这样 Windows 上也是同一条判据。
"""
from __future__ import annotations

import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from shared.lib import process_control as pc

ROOT = Path(__file__).resolve().parents[1]

#: 一个会起孙进程的孩子 —— 但**先等父进程把自己放进组/作业**（读一行 stdin）再起孙进程。
#: 这正是咽喉的次序：spawn → Group.of → 孩子才开始干活。Windows 的 Job 只捕获**加入之后**
#: 起的进程，所以次序必须对；POSIX 的 setsid 与次序无关，同一份测试两平台都成立。
_CHILD_WITH_GRANDCHILD = textwrap.dedent(
    """
    import subprocess, sys, time
    sys.stdin.readline()   # 等父进程：你已经在组里了，可以起孙进程了
    grandchild = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    print(grandchild.pid, flush=True)
    time.sleep(30)
    """
)


def _wait_until(predicate, timeout_s: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def test_killing_the_group_takes_the_grandchild_too():
    proc = subprocess.Popen(
        [sys.executable, "-c", _CHILD_WITH_GRANDCHILD],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, **pc.group_spawn_kwargs(),
    )
    group = pc.Group.of(proc.pid)          # 先把孩子放进组/作业
    proc.stdin.write("go\n")               # 然后才让它起孙进程 → 孙进程也在组里
    proc.stdin.flush()
    grandchild_pid = int(proc.stdout.readline().strip())
    try:
        assert pc.alive(proc.pid) and pc.alive(grandchild_pid)
        group.kill()
        proc.wait(timeout=10)
        assert _wait_until(lambda: not pc.alive(grandchild_pid)), "孙进程活过了整组 kill"
    finally:
        group.close()
        for stream in (proc.stdin, proc.stdout):
            try:
                stream.close()
            except OSError:
                pass


def test_kill_on_a_group_that_is_already_gone_is_not_an_error():
    proc = subprocess.Popen([sys.executable, "-c", "pass"], **pc.group_spawn_kwargs())
    proc.wait(timeout=10)
    group = pc.Group.of(proc.pid)  # 组长已经退出
    group.kill()
    group.close()


def test_alive_and_command_line_answer_from_the_process_not_a_record():
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        assert pc.alive(proc.pid)
        line = pc.command_line(proc.pid)
        assert line and "time.sleep(30)" in line
    finally:
        pc.kill(proc.pid)
        proc.wait(timeout=10)
    assert _wait_until(lambda: not pc.alive(proc.pid))
    assert pc.command_line(proc.pid) is None


@pytest.mark.skipif(sys.platform == "win32", reason="Windows 没有僵尸进程")
def test_a_zombie_counts_as_dead():
    """僵尸 = 已退出、只是没人收尸。它必须算**死** —— 没有 init 收割孤儿的环境（容器
    PID 1、CI）里，一个退出却成僵尸的 worker 不能被当成"还在跑"。这条在本机就能确定性
    地造出僵尸来验，不必等 CI 的容器。"""
    import os

    pid = os.fork()
    if pid == 0:  # 子进程：立刻退出，变成僵尸（父进程故意先不 waitpid）
        os._exit(0)
    try:
        assert _wait_until(lambda: not pc.alive(pid), timeout_s=5), "僵尸被当成了活着"
    finally:
        try:
            os.waitpid(pid, 0)  # 收尸，别把僵尸留给测试进程
        except ChildProcessError:
            pass


def test_terminate_and_kill_on_a_missing_pid_do_not_raise():
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(timeout=10)
    pc.terminate(proc.pid)
    pc.kill(proc.pid)


def test_process_table_contains_this_test_process():
    table = pc.process_table()
    assert table is not None
    import os

    assert any(pid == os.getpid() for pid, _ppid, _cmd in table)


def test_group_terminate_gives_grace_then_kills():
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"], **pc.group_spawn_kwargs()
    )
    group = pc.Group.of(proc.pid)
    try:
        started = time.monotonic()
        group.terminate(grace_s=2.0, still_alive=lambda: pc.alive(proc.pid) and proc.poll() is None)
        proc.wait(timeout=10)
        assert time.monotonic() - started < 5
    finally:
        group.close()


def test_watch_parent_runs_the_callback_when_the_probe_flips():
    """watch_parent 的契约：探针一旦为假就调回调、结束线程。用合成探针（不牵扯真进程）
    确定性地验循环本身 —— 两平台同一份。"""
    import threading

    ticks = iter([True, True, False])
    fired = threading.Event()
    t = pc.watch_parent(lambda: next(ticks, False), fired.set, interval=0.02)
    assert fired.wait(timeout=5), "探针翻成 False 了，watch_parent 没有触发回调"
    t.join(timeout=5)


def test_an_explicit_pid_probe_notices_that_process_dying():
    """产品实际走的路：盯住一个**显式给定**的 pid（壳把自己的 pid 交给后端）。起一个
    真进程、盯住它、杀掉它，探针要翻成 False。绕开 getppid（Windows 上经 python 启动器
    shim 时它指不对）。"""
    victim = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    probe = pc.parent_probe(pid=victim.pid)
    try:
        assert probe() is True
        pc.kill(victim.pid)
        victim.wait(timeout=10)
        assert _wait_until(lambda: probe() is False, timeout_s=10), "被盯的进程死了，探针还说它在"
    finally:
        if victim.poll() is None:
            pc.kill(victim.pid)


@pytest.mark.skipif(sys.platform == "win32", reason=(
    "Windows 的父死子亡由壳交待（壳持有后端的 Job / 把自己 pid 交给后端，见 explicit-pid "
    "探针），不靠 getppid —— 而 getppid 在 uv / venv 的 python 启动器 shim 下也测不真；"
    "POSIX 的过继才是这条 getppid 路径的战场，P2 的壳落地 Windows 那条"))
def test_the_child_dies_when_its_parent_dies(tmp_path):
    """父死子亡（getppid 路径，POSIX）：P 起孩子 C（C 用 parent_probe()+watch_parent 守着
    自己的父进程）；杀掉 P，C 必须自己退出。这是桌面壳被强杀时后端不留孤儿的判据。"""
    marker = tmp_path / "child_exited"
    child = textwrap.dedent(
        f"""
        import sys, time
        sys.path.insert(0, {str(ROOT)!r})
        from shared.lib import process_control as pc
        import os
        def bye():
            open({str(marker)!r}, "w").write("bye")
            os._exit(0)
        pc.watch_parent(pc.parent_probe(), bye, interval=0.1)
        print("watching", flush=True)
        time.sleep(60)
        """
    )
    parent = textwrap.dedent(
        f"""
        import subprocess, sys, time
        child = subprocess.Popen([sys.executable, "-c", {child!r}], stdout=subprocess.PIPE, text=True)
        print(child.pid, flush=True)
        print(child.stdout.readline().strip(), flush=True)
        time.sleep(60)
        """
    )
    p = subprocess.Popen([sys.executable, "-c", parent], stdout=subprocess.PIPE, text=True)
    child_pid = int(p.stdout.readline().strip())
    assert p.stdout.readline().strip() == "watching"
    pc.kill(p.pid)
    p.wait(timeout=10)
    assert _wait_until(marker.exists, timeout_s=10), "父进程死了，孩子没有自己退出"
    assert _wait_until(lambda: not pc.alive(child_pid), timeout_s=10)


def test_parent_probe_is_true_while_the_parent_lives():
    assert pc.parent_probe()() is True


@pytest.mark.skipif(sys.platform == "win32", reason="daemonize 在 Windows 上属于 P1 的 win32 后端")
def test_daemonize_detaches_from_the_terminal(tmp_path):
    marker = tmp_path / "daemon"
    script = textwrap.dedent(
        f"""
        import os, sys
        sys.path.insert(0, {str(ROOT)!r})
        from shared.lib import process_control as pc
        if pc.daemonize():
            open({str(marker)!r}, "w").write(f"{{os.getpid()}} {{pc.current_group_identity()}}")
            os._exit(0)
        print("parent-returned", flush=True)
        """
    )
    out = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=20)
    assert out.stdout.strip() == "parent-returned", out.stderr
    assert _wait_until(marker.exists, timeout_s=5)
    pid, identity = marker.read_text().split()
    assert identity.startswith("pgid:") and identity != f"pgid:{pid}", "孙进程不该是自己的组长"


def test_windows_daemonize_respawns_itself_without_escaping_the_job(monkeypatch):
    """Windows 上「脱开」＝重起一份自己 + 脱离控制台，**不逃壳的 Job**。

    两种「活得比谁久」要分开：`submit_job` 的容器契约要的是**活过这一轮**（Windows
    同样需要），POSIX 双 fork 顺带给的**活过整个应用**恰恰不该要 —— 壳持
    `KILL_ON_JOB_CLOSE`，关掉应用就该整棵树收摊（#835）。所以这里钉三件事：
    带 DETACHED_PROCESS、带 CREATE_NEW_PROCESS_GROUP、**不带** CREATE_BREAKAWAY_FROM_JOB
    （带了在壳的 Job 里一律 ERROR_ACCESS_DENIED，#908 真机踩过）。

    在**任何**宿主上都测得了：把 `_WINDOWS` 和那几个常量注入进来。
    """
    monkeypatch.setattr(pc, "_WINDOWS", True)
    monkeypatch.setattr(subprocess, "DETACHED_PROCESS", 0x8, raising=False)
    monkeypatch.setattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200, raising=False)
    monkeypatch.setattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0x1000000, raising=False)
    # 故意让 orig_argv[0] ≠ sys.executable（venv 就是这个样子）
    monkeypatch.setattr(sys, "orig_argv",
                        [r"C:\base\python.exe", "-I", "job.py", "start", "/tmp/x"])
    monkeypatch.delenv(pc._DETACHED_SENTINEL, raising=False)

    seen = {}

    class _Spawned:
        def __init__(self, argv, **kwargs):
            seen["argv"] = argv
            seen.update(kwargs)

    monkeypatch.setattr(pc.subprocess, "Popen", _Spawned)

    assert pc.daemonize() is False, "原进程必须拿到 False（它还要把 runtime id 打回去）"
    # 镜像必须是 `sys.executable`，**不是** orig_argv[0]：venv 里后者是 uv trampoline
    # 背后的基础解释器，拿它重起自己就把 venv 丢了，分身 ModuleNotFoundError 且
    # stdio 接了 NUL 一声不吭（2026-09-10 真机踩过）。
    assert seen["argv"][0] == sys.executable, seen["argv"]
    assert seen["argv"] == [sys.executable, "-I", "job.py", "start", "/tmp/x"], seen["argv"]
    flags = seen["creationflags"]
    assert flags & 0x8, "要 DETACHED_PROCESS：脱离控制台，原进程退出不连坐"
    assert flags & 0x200, "要 CREATE_NEW_PROCESS_GROUP：信号隔离"
    assert not (flags & 0x1000000), "**不许**逃壳的 Job（#908：一逃就 ERROR_ACCESS_DENIED）"
    assert seen["env"][pc._DETACHED_SENTINEL] == "1", "分身得认得出自己"
    # 原进程要用 stdout 把 runtime id 交回调用方，分身不能跟它抢同一个 stdout
    assert seen["stdout"] == subprocess.DEVNULL and seen["stderr"] == subprocess.DEVNULL


def test_windows_daemonize_tells_the_respawned_copy_it_is_the_detached_one(monkeypatch):
    """分身认自己靠 env 哨兵 —— 它必须拿到 True，否则会无限重起自己。"""
    monkeypatch.setattr(pc, "_WINDOWS", True)
    monkeypatch.setenv(pc._DETACHED_SENTINEL, "1")

    def _must_not_spawn(*_a, **_k):
        raise AssertionError("分身不该再起一份自己 —— 那是无限递归")

    monkeypatch.setattr(pc.subprocess, "Popen", _must_not_spawn)
    assert pc.daemonize() is True


def test_windows_detached_worker_must_not_breakaway_from_the_job(monkeypatch):
    """Windows 上起 worker **不许**带 `CREATE_BREAKAWAY_FROM_JOB`。

    2026-09-09 真机：桌面版每一轮都 `[WinError 5] 拒绝访问`，完全用不了。根因是壳
    (`platform/desktop/windows/Shell.cs`) 把后端放进一个只有 `KILL_ON_JOB_CLOSE`、
    **没有** `BREAKAWAY_OK` 的 Job；进程在这种 Job 里带 breakaway 标志起子进程，Windows
    一律 `ERROR_ACCESS_DENIED`(5)。而且就算能逃也不该逃：那个 Job 是「关掉应用＝全部收摊」
    的边界，逃出去＝关了应用 worker 还在烧钱（#835）。要的只是**信号隔离**，
    `CREATE_NEW_PROCESS_GROUP` 已经够了。

    常量在非 Windows 上不存在，所以这里注入，好让 Linux CI 也守得住这条。
    """
    monkeypatch.setattr(pc, "_WINDOWS", True)
    monkeypatch.setattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200, raising=False)
    monkeypatch.setattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0x1000000, raising=False)

    flags = pc.detached_spawn_kwargs()["creationflags"]
    assert flags & 0x200, "没给 CREATE_NEW_PROCESS_GROUP —— 后端的 Ctrl-C/SIGTERM 会连坐 worker"
    assert not (flags & 0x1000000), (
        "又把 CREATE_BREAKAWAY_FROM_JOB 加回来了 —— 壳把后端放在不许 breakaway 的 Job 里，"
        "带这个标志起 worker 会 [WinError 5] 拒绝访问，桌面版每一轮都跑不起来"
    )

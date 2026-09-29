"""进程控制原语 —— 一处回答，两个平台。

四件事：**起一组并能整组杀掉**、**认活 / 认身份**、**按 pid 收摊**、**父死子亡**；
外加给脱离终端的作业一个 :func:`daemonize`。

POSIX：新会话（``setsid``，组长的 pgid == pid）+ 信号。
Windows：Job Object（``KILL_ON_JOB_CLOSE``、不许 breakaway）+ ``TerminateProcess`` /
``TerminateJobObject``；进程身份与活性用 psutil（两平台同一份实现，不重写）。

为什么不让调用方各写各的 —— 三个在 Mac 上全绿、到 Windows 上失效且报错指向别处的坑：

* ``start_new_session=True`` 在 Windows 上被**静默忽略**：没有组，整组杀什么都杀不到；
* ``signal.SIGKILL`` 在 Windows 上**不存在**：``AttributeError`` 在杀进程那一刻炸；
* ``os.getppid()`` 在 Windows 上父进程死了**也不变**：父死子亡永远不触发，留孤儿。

运行时代码只许经这里碰进程组 / 信号 / 父进程
（``tests/test_process_control_is_the_only_process_control.py`` 扫盘钉着）。
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
import re
from functools import lru_cache
from uuid import uuid4
from collections.abc import Callable

import psutil

__all__ = [
    "Group",
    "group_spawn_kwargs",
    "detached_spawn_kwargs",
    "alive",
    "command_line",
    "process_table",
    "terminate",
    "kill",
    "parent_probe",
    "watch_parent",
    "daemonize",
]

log = logging.getLogger(__name__)

_WINDOWS = sys.platform == "win32"
#: 公开别名 —— 调用方要问"这台机器有没有进程组"时读它，别各自再判一次 sys.platform。
WINDOWS = _WINDOWS


# ── 起一组 ────────────────────────────────────────────────────────────────────


def group_spawn_kwargs() -> dict:
    """传给 ``subprocess.Popen`` / ``asyncio.create_subprocess_*`` 的关键字：子进程自成一组。

    POSIX：``setsid`` —— 子进程成为会话兼进程组组长，**pgid 恒等于 pid**（POSIX 保证，
    不必再问操作系统；问了反而有 race，见 ``group_killer``）。
    Windows：新的控制台进程组（Ctrl-C 事件不串组）；真正"整组可杀"的单位是
    :class:`Group` 里的 Job，起完之后 ``Group.of(pid)`` 把它放进去。
    """
    if _WINDOWS:
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def detached_spawn_kwargs() -> dict:
    """给要**活过后端**的孩子（platform worker）：不受后端的信号连坐。

    要的是**信号隔离**，不是"逃出 Job"——POSIX 上 `start_new_session` 让 worker 自成
    会话/进程组，后端的 Ctrl-C（打给前台进程组）和 uvicorn 关停的 SIGTERM（打给自己组）
    都连坐不到它；Windows 上做到同一件事的是 `CREATE_NEW_PROCESS_GROUP`。

    **不要再加 `CREATE_BREAKAWAY_FROM_JOB`**（2026-09-09 真机：桌面版每一轮都
    `[WinError 5] 拒绝访问`，用不了）。两个原因：

    1. **它会让每一轮都起不来。** 桌面版的壳（`platform/desktop/windows/Shell.cs`）把后端
       整棵树放进一个 `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` 的 Job，且**没给**
       `JOB_OBJECT_LIMIT_BREAKAWAY_OK`。进程在不许 breakaway 的 Job 里带这个标志起子进程，
       Windows 一律返回 `ERROR_ACCESS_DENIED`(5) —— 后端起 worker 那步当场失败。
       从源码跑时没有壳、进程不在 Job 里，这个标志"无害"，所以一路没被发现；装完自检也只
       验到 `/health/ready` 200、**从没真发过一条消息**，worker 那步一次都没走过。
    2. **就算能 breakaway 也不该 breakaway。** 壳那个 Job 就是"关掉应用＝全部收摊"的边界。
       逃出去意味着关了应用 worker 还在跑、还在烧钱 —— 正是 [[project_work_needs_a_watcher]]
       (#835) 修过的事。留在 Job 里语义才对：**后端重启它照活**（Job 不因成员退出而关闭），
       **关掉应用它跟着死**。
    """
    if _WINDOWS:
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


class Group:
    """一个可整组杀掉的进程集合，包括孙进程。

    用法（咽喉、build guard）::

        proc = await asyncio.create_subprocess_exec(*argv, **group_spawn_kwargs())
        group = Group.of(proc.pid)
        ...
        group.kill()

    POSIX：``of(pid)`` 只记下 pgid（== pid，前提是用 :func:`group_spawn_kwargs` 起的）；
    ``kill`` = ``killpg(SIGKILL)``。
    Windows：``of(pid)`` 建一个 Job（``KILL_ON_JOB_CLOSE``、不许 breakaway）并把 pid 放进去，
    此后它起的一切都在 Job 里；``kill`` = ``TerminateJobObject``；本对象被回收时 Job 句柄
    关闭 → 整组死（这也是「我们死了孩子也死」的保证）。spawn 到 ``of`` 之间那一小段
    窗口里子进程起的孙进程不在 Job 里 —— 模型命令走 ``_win32_exec.py`` 启动器（它自己
    先进 Job 再起载荷）把这个窗口关掉；框架自己的命令（git、latexmk）窗口可忽略。
    """

    __slots__ = ("_pgid", "_job", "_name")

    def __init__(self, *, pgid: int | None = None, job: int | None = None, name: str | None = None) -> None:
        self._pgid = pgid
        self._job = job
        self._name = name

    @classmethod
    def of(cls, pid: int) -> "Group":
        if _WINDOWS:
            job = _win32.create_job(kill_on_close=True)
            _win32.assign(job, pid)
            return cls(job=job)
        return cls(pgid=int(pid))

    @classmethod
    def from_identity(cls, identity: str) -> "Group":
        """从事实账里的字符串（:meth:`identity` / :func:`current_group_identity`）接回一组。
        ``pgid:<n>`` 只在 POSIX 上有意义；Windows 的命名 Job（``job:<name>``）在 P1。"""
        kind, _, value = str(identity).partition(":")
        if kind == "pgid" and value.isdigit() and not _WINDOWS:
            return cls(pgid=int(value))
        if kind == "job" and _WINDOWS and re.fullmatch(r"Local\\ScienceMate-job-[0-9a-f]{32}", value):
            handle = _win32.open_job(value)
            if handle is not None:
                return cls(job=handle, name=value)
        raise ValueError(f"cannot attach to process group {identity!r} on {sys.platform}")

    def identity(self) -> str:
        """给事实账（record.json）用的字符串：``pgid:<n>``。Windows 的命名 Job 在 P1。"""
        if self._pgid is not None:
            return f"pgid:{self._pgid}"
        return f"job:{self._name}" if self._name else "job:anonymous"

    def kill(self) -> bool:
        """整组立刻死。组已经没了 = 目的已达成，也算 True；只有真错误（权限之类）返回 False。"""
        if _WINDOWS:
            if self._job is not None:
                _win32.terminate_job(self._job)
            return True
        import signal

        try:
            os.killpg(self._pgid, signal.SIGKILL)
            return True
        except ProcessLookupError:
            return True
        except OSError as exc:
            log.warning("killpg(%s) failed: %s", self._pgid, exc)
            return False

    def terminate(self, *, grace_s: float, still_alive: Callable[[], bool]) -> None:
        """先礼后兵：POSIX 发 SIGTERM，最多等 ``grace_s`` 秒看 ``still_alive``，还在就 SIGKILL。
        Windows 没有优雅信号，直接整组终止（优雅停止走协议层的命令，不走信号）。"""
        if _WINDOWS:
            self.kill()
            return
        import signal

        try:
            os.killpg(self._pgid, signal.SIGTERM)
        except ProcessLookupError:
            return
        except OSError as exc:
            log.warning("killpg(%s, SIGTERM) failed: %s", self._pgid, exc)
            return
        deadline = time.monotonic() + grace_s
        while time.monotonic() < deadline and still_alive():
            time.sleep(0.05)
        if still_alive():
            self.kill()

    def close(self) -> None:
        """Windows：关 Job 句柄（KILL_ON_JOB_CLOSE → 组里还活着的一起死）。POSIX 无事。"""
        if _WINDOWS and self._job is not None:
            _win32.close(self._job)
            self._job = None


# ── 认活 / 认身份 ────────────────────────────────────────────────────────────


def alive(pid: int) -> bool:
    """进程**还在跑**吗。僵尸算**死**：它已经退出，只是父进程还没 waitpid 收走。

    旧写法 ``os.kill(pid, 0)`` 对僵尸返回成功（僵尸还占着 pid 表项）—— 于是一个退出了
    却没人收尸的 worker / 作业会被当成"还活着"。没有 init 收割孤儿的环境（容器 PID 1、
    CI）里这尤其要命。这里把僵尸判成死，是修那个潜伏的错，不是新语义。Windows 没有僵尸，
    ``status()`` 给别的值 → 照常算活。
    """
    try:
        proc = psutil.Process(int(pid))
        return proc.status() != psutil.STATUS_ZOMBIE
    except (psutil.NoSuchProcess, psutil.ZombieProcess):
        return False
    except (psutil.AccessDenied, psutil.Error):
        # 拿不到状态但进程确实存在（别人的进程）：退回"存在即活"，别把它误判成死。
        return psutil.pid_exists(int(pid))
    except (ValueError, OverflowError):
        return False


#: 身份读不到时的取值。**不是 False** —— "读不到"和"不是同一个进程"必须分得开：
#: 前者要 fail closed（不判死、也不发信号），后者要明确拒绝发信号。
UNKNOWN_IDENTITY = None


def birth_identity(pid: int) -> str | None:
    """进程的**出生身份**：号 + 它是什么时候出生的。读不到返回 None。

    裸 pid / pgid 不是身份，是**号**，而号会被复用。复用之后：
      · `stop` 会向占号的无关进程组发 SIGTERM，必要时 SIGKILL；
      · `inspect` 把占号的进程当成原作业，于是一个早就结束的作业报 running；
      · 作业因此走不出去（取消报 `local_job_stop_unconfirmed`，收尾报
        `finalized_needs_cleanup`）。

    主机重启会把这个概率放大：旧记录还在、状态停在 running，而 PID 计数从头开始。

    Linux 上取 `/proc/<pid>/stat` 的 **starttime**（自开机以来的时钟滴答），
    其它平台取 psutil 的 `create_time()`。

    为什么 Linux 单拎出来：`create_time()` 在 Linux 上是 `boot_time + starttime/HZ`，
    而 `boot_time` 读的是 `/proc/stat` 的 `btime` —— **NTP 一调时它就会跟着变**。
    于是同一个进程在两个时刻被两个进程各算一次，会得到两个不同的"出生时刻"，
    身份当场对不上。CI（容器里跑 NTP）上实测：作业刚起来就被 `inspect` 判成
    `pid_reused`，`status` 报 dead，测试红。starttime 是相对开机的滴答数，不经过
    btime，正是为这件事存在的那个量。

    读不到就返回 None（`UNKNOWN_IDENTITY`），调用方 fail closed。
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return UNKNOWN_IDENTITY
    if sys.platform.startswith("linux"):
        try:
            with open(f"/proc/{pid}/stat", "rb") as fh:
                raw = fh.read().decode("utf-8", "replace")
            # 第 2 个字段是 comm，**可能含空格和右括号** —— 按最后一个 ')' 切。
            fields = raw[raw.rindex(")") + 2:].split()
            return f"ticks:{fields[19]}"          # starttime = 全表第 22 个字段
        except (OSError, ValueError, IndexError):
            return UNKNOWN_IDENTITY
    try:
        return f"start:{psutil.Process(pid).create_time():.3f}"
    except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.Error,
            ValueError, OverflowError):
        return UNKNOWN_IDENTITY


def identity_matches(pid: int, recorded: str | None) -> bool | None:
    """现在占着这个号的，还是当初那个进程吗。

    三个答案，**不能压成两个**：
      · True  —— 是它；
      · False —— 号被复用了（或者进程已经不在）：**不要发信号**；
      · None  —— 读不到身份，或者记录里本来就没有身份（升级前起的作业）：
        既不判死也不发信号，由调用方 fail closed。
    """
    if not recorded:
        return None
    current = birth_identity(pid)
    if current is UNKNOWN_IDENTITY:
        # 进程不在了也会走到这里。分得开：不在 = 肯定不是它。
        return False if not psutil.pid_exists(int(pid)) else None
    # 两边不是同一种刻度（升级前后换过口径、或跨平台搬过账本）→ **不比**。
    # 拿滴答数和秒去比会得出"不是同一个进程"，而那是个假答案。
    if recorded.split(":", 1)[0] != current.split(":", 1)[0]:
        return None
    return current == recorded


# ── 收尸：谁负责（#1085 B）──────────────────────────────────────────────────
#
# 受管本地作业的 supervisor 是**双 fork 出来的孤儿**（`daemonize()`）：它的直接
# 父进程当场 `_exit`，它被 PID 1 收养。这是有意的 —— 后端收摊、换代、重启都不该
# 毁掉正在跑的研究（PR#784 / #1040：孩子归 OS 不归事件循环）。
#
# 代价是框架里**没有任何进程能对它 waitpid**。所以收尸的 owner 只能是 PID 1，
# 也就是部署方：容器要带会收尸的 init（`--init` / tini / s6 / systemd）。仓库里
# 已有先例：`deploy/imagegen-service/compose.yaml` 写着 `init: true`。
#
# 把它写成一个具名常量而不是留在注释里，是因为 #1085 B 问的正是"owner 是谁"——
# 一个没有 owner 的义务会被每一方都当成别人的事。
ORPHAN_REAPING_OWNER = "deployment_pid1"


@lru_cache(maxsize=1)
def orphans_get_reaped(timeout_s: float = 2.0) -> bool:
    """这台机器的 PID 1 会收割孤儿僵尸吗 —— **探一次，不猜**（结果缓存）。

    没有收割者时（没带 `--init` 的容器、CI），每个本地作业至少留下 1 个僵尸
    （supervisor），被取消的作业留下 2 个。僵尸占 PID 表和 pids 配额，还会把
    进程组撑着，于是按组判活的检查在那类机器上要么红要么只能跳过。

    做法：让一个 `sh` 起一个后台 `sleep` 并报出它的 pid，然后收割 `sh` 自己 ——
    那个 `sleep` 就孤儿化给了 PID 1。它退出之后**直接看它是不是僵尸**。

    为什么不看"进程组还在不在"：那条路要 `killpg(pgid, 0)`，而号一旦被别的用户
    的组复用就是 `EPERM` —— 一个诊断探针因此在收集阶段抛异常（实测），而且
    "看不了"和"还在"被压成了同一个答案。问僵尸状态是直接问要问的那件事。

    Windows 没有僵尸这回事，直接 True。
    """
    if _WINDOWS:
        return True
    try:
        probe = subprocess.run(
            ["/bin/sh", "-c", "sleep 0.05 </dev/null >/dev/null 2>&1 & echo $!"],
            capture_output=True, text=True, timeout=float(timeout_s), check=False,
            start_new_session=True)
        orphan = int((probe.stdout or "").strip() or 0)
    except Exception:
        return False
    if orphan <= 0:
        return False
    deadline = time.monotonic() + float(timeout_s)
    while time.monotonic() < deadline:
        try:
            status = psutil.Process(orphan).status()
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            # 进程表里已经没有它了 = 被收走了。（ZombieProcess 是 psutil 在
            # **自己的子进程**上抛的，这里的孤儿不属于我们，不会走到。）
            return True
        except psutil.Error:
            return False        # 看不了就别说"有收割者"——保守方向是报缺口
        if status == psutil.STATUS_ZOMBIE:
            return False        # 退出了还挂在那儿 —— 没人收尸
        time.sleep(0.05)
    return False


def command_line(pid: int) -> str | None:
    """进程的命令行（空格连接）。没了 / 看不了 → None，绝不猜。"""
    try:
        parts = psutil.Process(int(pid)).cmdline()
    except (psutil.NoSuchProcess, psutil.ZombieProcess, psutil.AccessDenied, ValueError):
        return None
    return " ".join(parts) if parts else None


def process_table() -> list[tuple[int, int, str]] | None:
    """(pid, ppid, cmdline) 全表。查不到返回 None。"""
    rows: list[tuple[int, int, str]] = []
    try:
        for proc in psutil.process_iter(["pid", "ppid", "cmdline"]):
            info = proc.info
            cmd = info.get("cmdline") or []
            rows.append((int(info["pid"]), int(info.get("ppid") or 0), " ".join(cmd)))
    except psutil.Error:
        return None
    return rows


# ── 按 pid 收摊 ──────────────────────────────────────────────────────────────


def terminate(pid: int) -> bool:
    """请它退出。POSIX：SIGTERM；Windows：``TerminateProcess``（没有优雅一说）。
    进程已经没了也算 True；只有被拒（不是我们的进程）返回 False。"""
    try:
        psutil.Process(int(pid)).terminate()
        return True
    except (psutil.NoSuchProcess, psutil.ZombieProcess):
        return True
    except psutil.AccessDenied as exc:
        log.warning("terminate(%s) denied: %s", pid, exc)
        return False


def kill(pid: int) -> bool:
    """让它立刻死。POSIX：SIGKILL；Windows：``TerminateProcess``。
    进程已经没了也算 True；只有被拒返回 False。"""
    try:
        psutil.Process(int(pid)).kill()
        return True
    except (psutil.NoSuchProcess, psutil.ZombieProcess):
        return True
    except psutil.AccessDenied as exc:
        log.warning("kill(%s) denied: %s", pid, exc)
        return False


# ── 父死子亡 ─────────────────────────────────────────────────────────────────


def parent_probe(pid: int | None = None) -> Callable[[], bool]:
    """返回「那个进程还在吗」的探针，起在**它还活着**的时候。

    ``pid`` 显式给定时，盯住那个具体进程 —— 这是**产品实际走的路**：桌面壳（一个真
    进程，没有解释器 shim）起后端时把**自己的 pid** 交给后端，后端盯住它。Windows 上
    拿它的句柄等（句柄在手 pid 不会被复用）；POSIX 上问它在不在。

    不给 ``pid`` 时退回"我的父进程还在吗"：POSIX 看 ``getppid()`` 变没变（父死被过继、
    号会变）。**Windows 的 ``getppid()`` 不可靠**——父不过继号不变，且经 venv / uv 的
    python 启动器时它指向的是 shim 而不是真正的父；所以 Windows 上应当走显式 ``pid``
    这条路（由壳提供），无 pid 的 Windows 分支只作尽力而为。
    """
    if pid is not None:
        if _WINDOWS:
            handle = _win32.open_for_wait(int(pid))
            if handle is None:
                return lambda: False
            return lambda: not _win32.has_exited(handle)
        target = int(pid)
        return lambda: alive(target)
    if _WINDOWS:
        handle = _win32.open_for_wait(os.getppid())
        if handle is None:  # 起探针时父进程已经没了
            return lambda: False
        return lambda: not _win32.has_exited(handle)
    original = os.getppid()
    return lambda: os.getppid() == original


def watch_parent(
    parent_alive: Callable[[], bool],
    on_death: Callable[[], None],
    *,
    interval: float = 1.0,
) -> threading.Thread:
    """后台线程：``parent_alive()`` 一旦为假就调 ``on_death`` 并结束。

    轮询而不是等一根管道 EOF：管道那套要父子两边都配合，而这条路必须在**父进程
    崩掉**时也成立 —— 那时候没有人还能配合。
    """

    def watch() -> None:
        while parent_alive():
            time.sleep(interval)
        on_death()

    thread = threading.Thread(target=watch, daemon=True, name="parent-watch")
    thread.start()
    return thread


# ── 脱离终端 ─────────────────────────────────────────────────────────────────


#: Windows 的 detached 分身靠它认出自己。POSIX 上 fork 之后「我是谁」由返回值直接给出，
#: Windows 没有 fork，只能重新起一份自己 —— 那份怎么知道自己是分身？用一个 env 哨兵，
#: 与 `platform_env.ensure_utf8_mode` 的 re-exec 同一套做法（那条已在真机上跑了一个月）。
_DETACHED_SENTINEL = "_HF_DETACHED_JOB"


def daemonize() -> bool:
    """让调用方**活过这一轮**：返回 True＝你已经是那个脱开的进程了，False＝你是原进程。

    ## 两种「活得比谁久」不是一回事

    `submit_job scheduler=local` 的容器契约要的是**活过发起它的那一轮**：起一个 detached
    的东西、拿回一个不可变 runtime id、之后按名字 inspect / stop。POSIX 双 fork 顺带还给了
    第二种——**活过整个应用**（自成会话、脱离终端）。

    这两种在桌面版上必须分开看。2026-09-10 我一度判「Windows 桌面版不需要 detached job，
    所以别实现」——**那是把两者混为一谈**：第一种在 Windows 上同样需要（不然 experiment
    的正路就没了），第二种恰恰**不该要**（壳持 `KILL_ON_JOB_CLOSE` 的 Job，关掉应用就该
    整棵树收摊，#835 的「没人看着的工作不该继续花钱」）。

    ## 所以 Windows 的实现是「重起一份自己 + 脱离控制台」，**不逃 Job**

    没有 fork，就用 `sys.orig_argv` 重新起一份自己（同 `ensure_utf8_mode` 的 re-exec），
    带 `DETACHED_PROCESS`（脱离控制台，原进程退出不连坐）+ `CREATE_NEW_PROCESS_GROUP`
    （信号隔离），**不带** `CREATE_BREAKAWAY_FROM_JOB` —— 带了在壳的 Job 里一律
    `ERROR_ACCESS_DENIED`(5)（#908 真机踩过），而且就算能逃也不该逃。

    分身自己的 stdio 接 NUL：原进程要用 stdout 把 runtime id 交回调用方，两份进程共用
    一个 stdout 会把那行搅乱。

    起不来就**如实抛**（不假装脱开了），调用方看得见真错误。
    """
    if _WINDOWS:
        if os.environ.get(_DETACHED_SENTINEL) == "1":
            return True
        argv = list(getattr(sys, "orig_argv", None) or [])
        if not argv:
            raise RuntimeError("daemonize(): 拿不到 sys.orig_argv，重起不了一份自己")
        # ⚠️ **镜像用 `sys.executable`，不是 `orig_argv[0]`。** venv 里这两个不是一回事：
        # `orig_argv[0]` 是 uv trampoline 背后的**基础解释器**，`sys.executable` 才是
        # venv 那个。拿前者重起自己 = 把 venv 丢了，分身当场
        # `ModuleNotFoundError: No module named 'psutil'` 而且 stdio 接了 NUL、**一声不吭**
        # （2026-09-10 真机实测；装好的应用里两者恰好相同，所以只在开发形态上炸）。
        # `ensure_utf8_mode` 没这个问题是因为 `os.execv(sys.executable, argv)` 把镜像与
        # argv 分开传 —— 这里用 Popen，argv[0] 就是镜像，必须自己接对。
        environment = {**os.environ, _DETACHED_SENTINEL: "1"}
        subprocess.Popen(
            [sys.executable, *argv[1:]],
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            creationflags=(subprocess.DETACHED_PROCESS
                           | subprocess.CREATE_NEW_PROCESS_GROUP),
        )
        return False
    pid = os.fork()
    if pid > 0:
        os.waitpid(pid, 0)
        return False
    os.setsid()
    if os.fork() > 0:
        os._exit(0)
    return True


_OWNED_JOB: Group | None = None


def current_group_identity() -> str:
    """A detached supervisor's killable identity, established before children spawn."""
    global _OWNED_JOB
    if _WINDOWS:
        if _OWNED_JOB is None:
            name = "Local\\ScienceMate-job-" + uuid4().hex
            handle = _win32.create_job(kill_on_close=True, name=name)
            if not _win32.assign(handle, os.getpid()):
                _win32.close(handle)
                raise RuntimeError("Cannot supervise this detached process with a Windows Job Object")
            _OWNED_JOB = Group(job=handle, name=name)
        return _OWNED_JOB.identity()
    return f"pgid:{os.getpgid(0)}"


# ── Windows 的那几个 Win32 调用 ─────────────────────────────────────────────
#
# 只用 ctypes，不引 pywin32。64 位下每个 HANDLE 参数 / 返回值都要显式 c_void_p，
# 否则被当成 32 位 int 截断 —— 报的是 ERROR_INVALID_HANDLE，指不回这里。


if _WINDOWS:  # pragma: no cover - 由 tests/test_process_control.py 在真机上覆盖
    import ctypes
    from ctypes import wintypes

    class _Win32:
        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
        JobObjectExtendedLimitInformation = 9
        PROCESS_SET_QUOTA = 0x0100
        PROCESS_TERMINATE = 0x0001
        SYNCHRONIZE = 0x00100000
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        WAIT_TIMEOUT = 0x102
        STILL_ACTIVE = 259

        class _IoCounters(ctypes.Structure):
            _fields_ = [(n, ctypes.c_ulonglong) for n in "abcdef"]

        class _Basic(ctypes.Structure):
            _fields_ = [
                ("per_process_user_time", ctypes.c_longlong),
                ("per_job_user_time", ctypes.c_longlong),
                ("limit_flags", wintypes.DWORD),
                ("min_ws", ctypes.c_size_t),
                ("max_ws", ctypes.c_size_t),
                ("active_process_limit", wintypes.DWORD),
                ("affinity", ctypes.c_size_t),
                ("priority_class", wintypes.DWORD),
                ("scheduling_class", wintypes.DWORD),
            ]

        def __init__(self) -> None:
            V = ctypes.c_void_p
            k = ctypes.WinDLL("kernel32", use_last_error=True)
            k.CreateJobObjectW.restype = V
            k.CreateJobObjectW.argtypes = [V, wintypes.LPCWSTR]
            k.OpenJobObjectW.restype = V
            k.OpenJobObjectW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
            k.SetInformationJobObject.argtypes = [V, ctypes.c_int, V, wintypes.DWORD]
            k.AssignProcessToJobObject.argtypes = [V, V]
            k.TerminateJobObject.argtypes = [V, wintypes.UINT]
            k.OpenProcess.restype = V
            k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            k.WaitForSingleObject.argtypes = [V, wintypes.DWORD]
            k.WaitForSingleObject.restype = wintypes.DWORD
            k.GetExitCodeProcess.argtypes = [V, ctypes.POINTER(wintypes.DWORD)]
            k.CloseHandle.argtypes = [V]
            self.k = k

            class _Ext(ctypes.Structure):
                _fields_ = [
                    ("basic", _Win32._Basic),
                    ("io", _Win32._IoCounters),
                    ("process_memory_limit", ctypes.c_size_t),
                    ("job_memory_limit", ctypes.c_size_t),
                    ("peak_process_memory", ctypes.c_size_t),
                    ("peak_job_memory", ctypes.c_size_t),
                ]

            self._Ext = _Ext

        def open_job(self, name: str) -> int | None:
            return self.k.OpenJobObjectW(0x0008, False, name) or None

        def create_job(self, *, kill_on_close: bool, name: str | None = None) -> int:
            job = self.k.CreateJobObjectW(None, name)
            if not job:
                raise OSError(ctypes.get_last_error(), "CreateJobObject failed")
            if kill_on_close:
                info = self._Ext()
                info.basic.limit_flags = self.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
                if not self.k.SetInformationJobObject(
                    job, self.JobObjectExtendedLimitInformation, ctypes.byref(info), ctypes.sizeof(info)
                ):
                    raise OSError(ctypes.get_last_error(), "SetInformationJobObject failed")
            return job

        def assign(self, job: int, pid: int) -> bool:
            """把 pid 放进 job；返回捕获成功没有。**从不 raise**：捕获不了就退化成"这一组
            没在 job 里"（`Group.kill` 因此变 best-effort，和 POSIX 上 killpg 吞错误一个路子）
            —— 在 spawn 之后建组这条路上，宁可少一层保护也不该当场崩。

            进程已经没了不算错（OpenProcess 失败，或 assign 失败但退出码显示它已退出）：
            空 job 的 kill 是 no-op。只有"进程还活着却捕不进 job"才是意外，记一条 warning。
            """
            access = self.PROCESS_SET_QUOTA | self.PROCESS_TERMINATE | self.PROCESS_QUERY_LIMITED_INFORMATION
            proc = self.k.OpenProcess(access, False, int(pid))
            if not proc:
                return False
            try:
                if self.k.AssignProcessToJobObject(job, proc):
                    return True
                err = ctypes.get_last_error()
                code = wintypes.DWORD()
                got = self.k.GetExitCodeProcess(proc, ctypes.byref(code))
                if got and code.value == self.STILL_ACTIVE:
                    log.warning("AssignProcessToJobObject(%s) failed but the process is alive: err=%s", pid, err)
                return False
            finally:
                self.k.CloseHandle(proc)

        def terminate_job(self, job: int) -> None:
            self.k.TerminateJobObject(job, 137)

        def close(self, handle: int) -> None:
            self.k.CloseHandle(handle)

        def open_for_wait(self, pid: int) -> int | None:
            handle = self.k.OpenProcess(
                self.SYNCHRONIZE | self.PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid)
            )
            return handle or None

        def has_exited(self, handle: int) -> bool:
            return self.k.WaitForSingleObject(handle, 0) != self.WAIT_TIMEOUT

    _win32 = _Win32()

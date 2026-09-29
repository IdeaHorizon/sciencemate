"""统一的可取消子进程入口 —— 模型命令进操作系统的唯一咽喉。

模型进程只允许通过 ``spawn_and_wait`` 进入某个隔离后端（``core.isolation``），
后端由 ``HARNESS_EXECUTOR`` 选择；这里不知道也不该知道后端是 Docker、seatbelt
还是 cgroup。等待与 ``state.kill_event`` 竞争，取消或超时走后端的 ``terminate``，
宿主侧进程组也会被回收。``writable_roots=None`` 只供参数完全由框架控制的内部命令。

「唯一」由 ``tests/test_model_commands_reach_the_os_through_one_throat.py`` 扫盘钉着：
运行时 import 面上任何 argv 非字面量的子进程调用都得登记在 ``framework_exemptions.yaml``。
"""
from __future__ import annotations

import asyncio
import dataclasses
import logging
import math
import os
import shutil as _shutil
from datetime import datetime as _dt, timezone as _tz
from pathlib import Path as _Path
from collections.abc import Callable
from typing import Any
import sys

from shared.lib import process_control

log = logging.getLogger(__name__)

_DRAIN_GRACE_S = 5.0


def group_killer(proc: asyncio.subprocess.Process) -> Callable[[], None]:
    """构造"杀掉整个进程组"的 kill_fn。

    必须配 `start_new_session=True` 使用（`test_builtin_bash_uses_group_kill`
    机械地钉着这一条）。那意味着子进程 `setsid()` 成了会话兼进程组组长，于是
    **它的 pgid 恒等于它的 pid** —— POSIX 保证，不需要问操作系统。

    这里原来是 `os.getpgid(proc.pid)`，取不到就退化成只杀直接子进程。两个问题：

      1. **有 race**：`bash -c "A && B &"` 的直接 bash 转眼就退出，asyncio 的
         watcher 线程随即回收它 —— spawn 与这一句之间只要被调度器插一下，
         getpgid 就抛。实测：16 路并发下必现（`tests/test_subprocess_kill.py`
         同款写法 16 次里红 3 次）。
      2. **退化路径正是它要修的那个 bug**：取不到 pgid 意味着直接子进程已经
         没了，此时 `proc.kill()` 什么都杀不到，真正攥着管道的后代活得好好的
         —— 那就是缺陷 A（E2E-5 那次 15.5 小时不返回）原封不动地回来了。

    别问一个答案会消失的问题。pid 是我们自己拿在手上的，不会消失。

    机制在 ``shared.lib.process_control.Group``（POSIX 进程组 / Windows Job）；这里只是
    把「整组」绑到这个 proc 上。
    """
    group = process_control.Group.of(proc.pid)

    def _kill() -> None:
        if group.kill():
            return          # 整组死了，或整组本来就没了 = 目的已达成
        # 因权限之类的真错误失败时，杀直接子进程聊胜于无。
        try:
            proc.kill()
        except (ProcessLookupError, OSError):
            pass

    return _kill


# 输出落文件并等直接进程退出，绝不拿管道 EOF 当完成判据。
_TAIL_READ_CAP = 256 * 1024
"""从输出文件尾部最多读多少 —— 跑几小时的作业日志可能上 GB，别全读进内存。"""


def _read_tail(path: str, cap: int = _TAIL_READ_CAP) -> bytes:
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - cap))
            return f.read()
    except OSError:
        return b""


def _preserve_large_output(path: str, dest_dir: Any, label: str) -> str | None:
    """输出超过尾部读取上限时，把**完整**输出文件保留下来并返回新路径。

    完整输出本来就已经写在临时文件里了 —— 旧行为是只读尾部 256KB 然后把文件
    删掉，于是开头那段（求解器的参数校验错误、网格读取失败往往就在那里）被
    永久丢弃，模型只能反复重跑去猜。这里改成：大输出不删，挪到 run 目录下。
    """
    try:
        if os.path.getsize(path) <= _TAIL_READ_CAP:
            return None
        d = _Path(dest_dir) / ".harness" / "tool_output"
        d.mkdir(parents=True, exist_ok=True)
        stamp = _dt.now(_tz.utc).strftime("%Y%m%dT%H%M%S%f")
        dest = d / f"{label}-{stamp}.txt"
        _shutil.move(path, dest)
        return str(dest)
    except Exception:
        return None



def the_interpreter_for_model_code() -> str:
    """交给模型代码跑的那个 Python —— **这个问题只在这里回答一次**。

    答案永远是"跑着 harness 的这一个"（`sys.executable`）：模型写的科学代码要
    用的 numpy / matplotlib / ase，与 harness 自己用的是同一套依赖，装在同一份
    运行时里。源码 checkout、wheel 安装、`.app` 里随包分发 —— 三种形态下这句话
    都成立，而任何**绝对路径**都只在其中一种下成立。

    ## 为什么不是 `/usr/bin/python3`（2026-09-06 真机实测）

    Docker 年代它是对的：容器镜像里的 `/usr/bin/python3` 就是那份装好科学栈的
    解释器。删掉 Docker、改成原生执行之后，macOS 上它是 **Xcode 的 python
    3.9，没有 numpy** —— 实测现场是模型接连跑四条命令满硬盘找 numpy：

        ls -l /usr/bin/python3 /opt/homebrew/bin/python3; which python3
        find /usr/local/lib /opt/homebrew/lib /Library/Frameworks -name numpy -type d
        === any venv in project ===

    沙箱本身没问题（四条命令全 returncode 0）。缺的只是"用哪个 Python"这个
    答案，而它一直就在进程自己身上。
    """
    return sys.executable


async def spawn_and_wait(
    *args: str,
    state: Any,
    timeout: float,
    cwd: str | None = None,
    shell: bool = False,
    writable_roots: "list[Any] | None" = None,
    readonly_roots: "list[Any] | None" = None,
    spill_dir: Any = None,
    spill: "dict | None" = None,
    sandbox_limits: Any = None,
    network_access: bool = False,
    sandbox_environment: "dict[str, str] | None" = None,
    sandbox_home: str = "host",
) -> tuple[str, int | None, bytes, bytes]:
    """起子进程并等它**退出**。返回 (status, returncode, stdout, stderr)。

    status ∈ {"done", "cancelled", "timeout", "spawn_failed"}。
    支持 `state.kill_event` 抢占；这里等进程退出，不等管道 EOF。

    ``spawn_failed`` = **载荷一次都没跑**，returncode 恒为 None。三个来源：后端拒绝派发
    （契约错）、操作系统起不了最外层包装、以及**墙的最内层（我们自己的启动器）exec 载荷
    失败** —— 后者以普通非零码退出，只在 stderr 留一行约定标记
    （``core.isolation.launcher_refusal``）；不读那一行，"latexmk 不在 PATH"和"latexmk
    跑完返回 1"在这里长得一模一样，下游只能把前者当稿子的错（2026-09-15 node20 复现）。

    `writable_roots` 非 None = 这是模型控制的子进程。它只能通过
    ``core.isolation.select_backend()`` 交出的后端进入操作系统；后端不可用或
    契约无效时直接 ``spawn_failed``，这里没有绕开后端的路。None 只供参数完全
    由框架控制的内部进程。``sandbox_home`` 是 ``CommandSpec.home``：``host``（默认，用户的
    程序看见用户的家）或 ``own``（平台自带的程序住墙给的家）。
    """
    import tempfile

    sandbox_launch = None
    if writable_roots is not None:
        from core import isolation as _isolation
        from core import sandbox as _sandbox

        from shared.lib.shell import posix_shell

        inner = [posix_shell(), "-c", args[0]] if shell else list(args)
        if not inner:
            return "spawn_failed", None, b"", b"empty sandbox command"
        sandbox_cwd = cwd
        if sandbox_cwd is None:
            first = _Path(writable_roots[0]).expanduser().resolve(strict=True)
            sandbox_cwd = str(first if first.is_dir() else first.parent)
        limits = sandbox_limits or _sandbox.SandboxLimits()
        limits = dataclasses.replace(limits, walltime_seconds=max(1, math.ceil(timeout)))
        try:
            backend = _isolation.select_backend()
            spec = _isolation.CommandSpec(
                argv=tuple(inner),
                cwd=str(sandbox_cwd),
                writable_roots=tuple(_Path(p) for p in writable_roots),
                readonly_roots=tuple(_Path(p) for p in (readonly_roots or ())),
                limits=limits,
                environment=sandbox_environment,
                home=sandbox_home,
                # Only framework-built acquisition commands set this; regular
                # model commands are permanently networkless.
                network_access=bool(network_access),
            )
            # 这条 run 跑在什么墙后面 —— 第一条命令之前记一次，进 transcript。
            # 记账层永不杀轮：记不上吵一声，命令照起（墙在 prepare 里，不在账本里）。
            try:
                _isolation.record_enforcement_once(state, backend)
            except Exception as exc:
                log.warning("isolation enforcement record failed: %s", exc)
            sandbox_launch = backend.prepare(spec, state=state)
            # 有些边界是**逐条命令**才知道守没守住（典型：可写根内部的只读洞在纯放行
            # 清单后端上超了规则预算）。后端把它挂在 launch 上，这里如实写进 transcript
            # —— 沉默的话，账面看起来和"守住了"一模一样。同样是记账层，同样永不杀轮。
            for invariant, why in tuple(getattr(sandbox_launch, "unmet", ()) or ()):
                try:
                    state.append_transcript("isolation_gap", invariant=invariant, reason=why)
                except Exception as exc:
                    log.warning("isolation gap record failed: %s", exc)
        except Exception as exc:
            log.error("isolation backend rejected launch: %s", exc)
            return "spawn_failed", None, b"", str(exc).encode("utf-8", errors="replace")
        # 后端说了算：argv 它给，cwd / env 它给就用它的（原生后端把 cwd 和脱敏后的
        # 环境放在 Launch 上；镜像后端把 cwd 装进容器请求里，这里就是 None）。
        args, shell = tuple(sandbox_launch.argv), False
        cwd = getattr(sandbox_launch, "cwd", None)
        spawn_env = getattr(sandbox_launch, "env", None)
    else:
        spawn_env = None

    out_f = tempfile.NamedTemporaryFile(prefix="hf_out_", suffix=".log", delete=False)
    err_f = tempfile.NamedTemporaryFile(prefix="hf_err_", suffix=".log", delete=False)
    out_path, err_path = out_f.name, err_f.name
    try:
        spawn = (asyncio.create_subprocess_shell(args[0], cwd=cwd, env=spawn_env,
                                                 stdout=out_f, stderr=err_f,
                                                 stdin=asyncio.subprocess.DEVNULL,
                                                 **process_control.group_spawn_kwargs())
                 if shell else
                 asyncio.create_subprocess_exec(*args, cwd=cwd, env=spawn_env,
                                                stdout=out_f, stderr=err_f,
                                                stdin=asyncio.subprocess.DEVNULL,
                                                **process_control.group_spawn_kwargs()))
        try:
            proc = await spawn
        except Exception as e:
            return "spawn_failed", None, b"", str(e).encode()

        kill = group_killer(proc)
        wait_task = asyncio.ensure_future(proc.wait())
        kill_event = getattr(state, "kill_event", None)
        kill_task = (asyncio.ensure_future(kill_event.wait())
                     if kill_event is not None else None)
        tasks = [wait_task] + ([kill_task] if kill_task is not None else [])

        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            done, _ = await asyncio.wait(tasks, timeout=max(0.0, deadline - loop.time()),
                                         return_when=asyncio.FIRST_COMPLETED)
            # kill_event 绑在别的 event loop 上（同一个 state 跨两次 asyncio.run）时，
            # `Event.wait()` 立刻以 RuntimeError 结束 —— 那不是"有人按了停止"，是
            # 没有可用的停止信号。原来这里把它当成取消，把一条健康命令 SIGKILL 成
            # rc=-9（writing 的双次编译真撞了）。信号坏了就当没有信号，吵一声。
            if (kill_task is not None and kill_task in done
                    and not kill_task.cancelled() and kill_task.exception() is not None):
                log.warning("kill_event is unusable (%s); running without a cancel signal",
                            kill_task.exception())
                tasks, kill_task = [wait_task], None
                if wait_task in done:
                    break
                continue
            break
        if wait_task in done:
            status = "done"
            # 命令结束后整棵进程树死（I3 的 GROUP_KILL）：`nohup x &` 留下的后代不能
            # 成为下一条命令的隐藏状态。镜像后端在容器里已经清过一遍，这里对它的
            # 宿主侧 docker 客户端组是空操作；原生后端就靠这一下。
            if sandbox_launch is not None:
                kill()
        else:
            status = "cancelled" if (kill_task is not None
                                     and kill_task in done) else "timeout"
            sandbox_cancel_requested = False
            if sandbox_launch is not None:
                try:
                    outcome = await asyncio.to_thread(sandbox_launch.terminate)
                    # False = 后端这边没有可停的东西（原生后端没 cgroup 时），
                    # 别等 drain，直接杀进程组。
                    sandbox_cancel_requested = outcome is not False
                except Exception as exc:
                    log.error("failed to terminate launch: %s", exc)
            # The host client is the only observer that waits for PID 1 to
            # finish killing/reaping the payload.  Killing it immediately
            # after a successful cancel request lets the caller return while
            # the Attempt is still transiently busy, so pause/end eviction can
            # leak its cgroup reservation.  Let the supervisor publish the
            # terminal result first; the process-group kill remains the bounded
            # fallback when cancellation itself failed or the client wedges.
            if not sandbox_cancel_requested:
                kill()
            try:
                await asyncio.wait_for(asyncio.shield(wait_task),
                                       timeout=_DRAIN_GRACE_S)
            except Exception:
                kill()
                if not wait_task.done():
                    wait_task.cancel()
        if kill_task is not None and not kill_task.done():
            kill_task.cancel()
        # Windows cannot rename a file while our capture handles still own it.
        # The process has settled; close them before preserving complete output.
        out_f.close()
        err_f.close()
        _out_b, _err_b = _read_tail(out_path), _read_tail(err_path)
        returncode = proc.returncode
        if status == "done" and sandbox_launch is not None:
            # 墙的最内层是我们自己的启动器（landlock / _exec.py / win32）：它 exec 载荷
            # 失败时以普通非零码退出、并在 stderr 打一行约定标记。那次载荷一次都没跑，
            # 退出码是启动器的、不是命令的 —— 按 spawn_failed 交出去，下游才分得开
            # 「缺工具链」和「命令自己失败」。
            from core import isolation as _isolation

            if _isolation.launcher_refusal(_err_b) is not None:
                status, returncode = "spawn_failed", None
        if spill is not None and spill_dir is not None:
            _op = _preserve_large_output(out_path, spill_dir, "stdout")
            _ep = _preserve_large_output(err_path, spill_dir, "stderr")
            if _op:
                spill["stdout_path"] = _op
            if _ep:
                spill["stderr_path"] = _ep
        return status, returncode, _out_b, _err_b
    finally:
        if sandbox_launch is not None:
            sandbox_launch.cleanup()
        for f, p in ((out_f, out_path), (err_f, err_path)):
            try:
                f.close()
            except Exception:
                pass
            try:
                os.unlink(p)
            except OSError:
                pass

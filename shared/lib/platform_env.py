"""子进程要能正常起来、以及**能守住墙**所必需的操作系统级环境变量 —— 一处回答。

Windows 的坑：我们对交给子进程的环境做白名单过滤（worker 的 env）、或用 `env -i`
起一个干净环境（模型的 python 子进程），两者都会把 `SYSTEMROOT` 之类剥掉。而 Windows
上**没有 `SYSTEMROOT` 的进程连 ws2_32 / 加密 DLL 都加载不了** —— 症状是 socket / DNS /
TLS 在别处炸，报错指不回这里（最典型的"Mac 全绿、Windows 全红且指向别处"）。

这些变量本身不是秘密，是"这台机器上任何进程都得有"的底座（值来自机器，不含用户数据、
不含凭据）。worker 的 env 和模型 python 子进程的 env 都从这里取同一份，不各抄一份。
POSIX 上这些变量基本都不存在于环境里，于是过滤出来是空集 —— 对 POSIX 无副作用。

第二件同族的事：**文本编码**。Windows 解释器的默认文本编码是 ``cp1252``（``locale``），
而产品到处写读中文（transcript / memory / node README / LaTeX）。不显式指定
``encoding=`` 的 ``open()`` / ``print()`` 在 Windows 上会 ``UnicodeEncodeError`` 崩、或把
utf-8 字节按 cp1252 读成乱码 —— ``hf doctor`` 头一句 print 就栽（真机实测）。架构解是
**Python UTF-8 模式**（``PYTHONUTF8=1`` / ``-X utf8``）：一个杠杆把整进程的 stdout 与
``open()`` 默认编码都扳成 utf-8，不用逐处补 ``encoding=``。它只在解释器**启动时**读，
所以外部启动的入口（CLI/后端）没开就 :func:`ensure_utf8_mode` re-exec 一次；框架自己
spawn 的子进程走 :func:`utf8_mode_env` 从 env 带上开关、直接起在 utf-8 模式。
"""
from __future__ import annotations

import os
import sys
import subprocess
from collections.abc import Mapping

__all__ = [
    "LINUX_SESSION_ENV",
    "WINDOWS_SYSTEM_ENV",
    "ensure_utf8_mode",
    "passthrough_names",
    "system_env_passthrough",
    "utf8_mode_env",
]

#: Windows 上进程正常运行所需的系统变量。
#:  - SYSTEMROOT / SYSTEMDRIVE / WINDIR：DLL 搜索根，缺了网络/加密 DLL 加载失败。
#:  - COMSPEC / PATHEXT：解释器与可执行后缀解析。
#:  - TEMP / TMP：临时目录（Windows 上程序默认写这里）。
#:  - USERPROFILE / LOCALAPPDATA / APPDATA / PROGRAMDATA：标准数据根。
#:  - NUMBER_OF_PROCESSORS / PROCESSOR_ARCHITECTURE：偶有库据此选路。
WINDOWS_SYSTEM_ENV = frozenset({
    "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR",
    "COMSPEC", "PATHEXT",
    "TEMP", "TMP",
    "USERPROFILE", "LOCALAPPDATA", "APPDATA", "PROGRAMDATA",
    "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE",
})


#: Linux 上连**用户级 systemd 会话**所需的两个变量。
#:  - XDG_RUNTIME_DIR：`/run/user/<uid>`，用户 session bus 与私有 socket 都在这儿。
#:  - DBUS_SESSION_BUS_ADDRESS：session bus 的地址。
#:
#: 为什么它们和 Windows 的 SYSTEMROOT 是同一类东西：缺了它们，
#: `systemd-run --user --scope -p MemoryMax=... -p TasksMax=...` 连不上 user session，
#: 于是 `core/isolation/linux.py::_probe_systemd()` 的行为探针必然失败，后端如实报告
#: mem_cap / pids_cap / cpu_cap 缺失 —— **记账是诚实的，缺的是能力本身**。
#:
#: 2026-09-07 实测（#849）：后端进程 environ 里两个都在，worker 进程里**数量 = 0**，
#: 因为父进程构造子环境时用的是白名单、而白名单里没有它们。后果是 UI 路径下一个吃
#: 资源的脚本可以不受任何 cgroup 约束地跑 —— 实测 188 进程（超 pids=128 限额 47%）、
#: 内存 +2GB、全程零干预；同一载荷在 CLI 下 1.4–2.4 秒内就被拦住。
#: 更该警惕的是模型看不出区别：它把「防线不存在」读成了「资源有余量」，并据此建议把
#: 规模放大到 1500 分片。守卫是否生效根本不在它的可观测面内。
#:
#: 值来自机器、不含用户数据、不含凭据 —— 和 WINDOWS_SYSTEM_ENV 同一条纪律。
#: 非 Linux 上这两个名字不存在于环境里，于是过滤出来是空集，逐字无副作用。
LINUX_SESSION_ENV = frozenset({
    "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS",
})


def passthrough_names() -> frozenset[str]:
    """必须放行给子进程的系统变量名集合。

    **一处回答**：worker 的 env、后端启动探针的 env、模型 python 子进程的 env 都从这里
    取同一份。分开各写一份名单的代价已经付过一次 —— #849 里那两份白名单漏的是同样的
    东西，而症状出现在第三个地方（沙箱没有资源墙），报错指不回这里。
    """
    return WINDOWS_SYSTEM_ENV | LINUX_SESSION_ENV


def system_env_passthrough(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """从给定环境里挑出这些系统变量（**存在的才挑**）。默认读 ``os.environ``。

    POSIX 上这些名字基本都不在环境里 → 返回空 dict，调用方逻辑因此对 POSIX 是无操作。
    """
    src = os.environ if environ is None else environ
    return {name: src[name] for name in passthrough_names() if name in src}


# ── UTF-8 模式：Windows 上把整进程的默认文本编码扳成 utf-8 ────────────────────────

_UTF8_REEXEC_SENTINEL = "_HF_UTF8_REEXEC"


def utf8_mode_env() -> dict[str, str]:
    """给框架 spawn 的 Python 子进程强制 ``PYTHONUTF8=1``（Windows 上让它们直接起在
    UTF-8 模式 —— stdout/stderr 与 ``open()`` 默认编码都是 utf-8）。POSIX 上返回空 dict，
    调用方逻辑对 POSIX 因此是无操作。子进程从 env 继承这个开关即可，不必自己 re-exec。"""
    return {"PYTHONUTF8": "1"} if sys.platform == "win32" else {}


def _reconfigure_std_streams() -> None:
    """把 stdout/stderr 掰成 utf-8 —— re-exec 起不来时的降级：至少保证能打印
    （改不了 ``open()`` 的默认编码，那只有真正的 UTF-8 模式才行）。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):  # 已被接管/不可重配 —— 尽力而为
            pass


def _in_utf8_mode() -> bool:
    return bool(sys.flags.utf8_mode)


def ensure_utf8_mode() -> None:
    """Windows：保证本解释器跑在 UTF-8 模式，没开就带 ``PYTHONUTF8=1`` **原样 re-exec 一次**。

    在**外部启动的入口**（CLI ``hf``、后端 launcher）的 ``main()`` 最前面调。框架自己 spawn
    的 worker / 模型命令走 :func:`utf8_mode_env` 从 env 带上开关、直接起在 utf-8 模式，那条
    路不经过这里。

    无操作的情形：POSIX（utf-8 本就是默认）、已在 UTF-8 模式（含被本函数 re-exec 后的新
    进程 —— ``PYTHONUTF8=1`` 已生效，``sys.flags.utf8_mode`` 为真，直接返回，不会循环）。
    re-exec 起不来（冻结/嵌入式解释器没有可用的 ``sys.orig_argv``，或 ``execv`` 失败）→ 降级
    只把 stdout/stderr 掰成 utf-8，保证至少能打印。
    """
    if sys.platform != "win32" or _in_utf8_mode():
        return
    if os.environ.get(_UTF8_REEXEC_SENTINEL) == "1":
        # 已经带 PYTHONUTF8=1 re-exec 过一次却仍不在 utf-8 模式（罕见：显式 -X utf8=0
        # 覆盖）—— 别再 re-exec 成死循环，降级只修打印。
        _reconfigure_std_streams()
        return
    os.environ["PYTHONUTF8"] = "1"
    os.environ[_UTF8_REEXEC_SENTINEL] = "1"
    argv = list(getattr(sys, "orig_argv", None) or [])
    if not argv:  # 没有原始 argv 可复现这次启动（冻结解释器）—— 降级
        _reconfigure_std_streams()
        return
    try:
        # Windows CRT execv does not preserve Python argv quoting and does not
        # provide POSIX same-process replacement. Wait for the real child using
        # subprocess quoting so -c scripts, spaces and non-ASCII args stay intact.
        code = subprocess.call([sys.executable, *argv[1:]])
        raise SystemExit(code)
    except OSError:
        _reconfigure_std_streams()

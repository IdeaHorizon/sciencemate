"""OS 启动的第一个 shell 可执行文件 —— 一处回答。

框架把模型命令包进一层 shell 再交给隔离后端启动：`/bin/sh -c <命令>`（框架包裹）
或 `/bin/bash -o pipefail -c <命令>`（模型命令，要 pipefail）。**Windows 上没有
`/bin/bash`**：模型的 shell 是随包 Git for Windows 的 MSYS2 `bash.exe` —— `bash_semantics`
那道语义闸、`env -i /bin/bash -o pipefail` 那份契约、模型沉淀的 run_bash 经验，全都据此
成立（RFC #848 §4.7）。

**只有 OS 启动的第一个 shell 可执行文件（某个 argv 的 argv[0]）要走这里。** 命令字符串
**里面**出现的 `/bin/bash`、`/usr/bin/env` 由那个 shell（在 Windows 上就是 MSYS bash）
自己解析，不用改。远端集群作业（k8s 模板里的 `/bin/bash`）跑在集群的 Linux 上，也不走这里。
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

__all__ = ["posix_shell", "bash_shell", "msys_runtime_bin"]

_WINDOWS = sys.platform == "win32"


def bash_shell() -> str:
    """模型命令跑的那个 shell（bash，支持 `-o pipefail`）。POSIX：`/bin/bash`；
    Windows：随包 MSYS2 `bash.exe`。"""
    return _windows_bash() if _WINDOWS else "/bin/bash"


def posix_shell() -> str:
    """框架包裹用的最小 shell。POSIX：`/bin/sh`；Windows：同一个 MSYS `bash.exe`
    （bash 是 sh 的超集，框架包裹的那点 `cd`/`exec`/`-c` 在 bash 下语义一致）。"""
    return _windows_bash() if _WINDOWS else "/bin/sh"


def _windows_bash() -> str:  # pragma: no cover - 由 Windows 真机与 tests 的注入覆盖
    """随包 Git for Windows 的 MSYS2 bash。

    次序：显式 `HARNESS_BASH` 覆盖 → 随包布局 `<python 前缀>/../git/usr/bin/bash.exe`
    （打包 P2）→ 从 PATH 上的 `git.exe` 反推它自带的 MSYS bash → Git for Windows 标准
    安装位置。

    **绝不回落到 `shutil.which("bash")`**：装了 WSL 的机器上，PATH 上第一个 `bash.exe`
    是 WindowsApps 里的 **WSL 启动器**，它会把模型命令整个丢进 WSL2 的 Ubuntu（`uname -o`
    变 GNU/Linux）—— 而 RFC #848 明确拒绝 WSL 路线，模型必须跑在受我们沙箱控制的原生
    MSYS bash 里。真机实测（desktop-9el2944）坐实：`which("bash")` 命中的正是 WSL。
    找不到受管 MSYS Bash 就显式失败，绝不让 Windows PATH 接管并转入 WSL。
    """
    override = os.environ.get("HARNESS_BASH", "").strip()
    if override:
        return override

    candidates: list[Path] = [Path(sys.prefix).parent / "git" / "usr" / "bin" / "bash.exe"]
    git = shutil.which("git")
    if git:
        # Git for Windows：<git>/cmd/git.exe 或 <git>/bin/git.exe → <git>/usr/bin/bash.exe
        git_root = Path(git).resolve().parent.parent
        candidates.append(git_root / "usr" / "bin" / "bash.exe")
    for base in (os.environ.get("PROGRAMFILES"), os.environ.get("PROGRAMFILES(X86)"),
                 os.environ.get("LOCALAPPDATA")):
        if base:
            candidates.append(Path(base) / "Git" / "usr" / "bin" / "bash.exe")
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    raise FileNotFoundError("Git for Windows Bash is unavailable; repair the installation or set HARNESS_BASH")


def msys_runtime_bin(program: str) -> str | None:
    r"""``program`` 是 MSYS 程序就返回它所在的目录（MSYS 的 ``/usr/bin``），否则 None。POSIX 恒 None。

    判据是**机械的**：和 ``msys-2.0.dll`` 同目录 —— Windows 加载器先在程序自己的目录找 DLL，
    所以这个目录里的程序（Git for Windows 的 ``usr/bin/bash.exe``、``sh.exe``……）跑的都是
    这一份 MSYS 运行时。不按名字猜：随包布局、系统 Git、``HARNESS_BASH`` 指过去的 MSYS2
    都一样认；WSL 的 ``bash.exe``（System32 / WindowsApps）旁边没有它，不认。

    原生进程起 MSYS 程序要替它做两件 login shell 才会做、我们直接起 ``bash.exe`` 就没人做
    的事（调用方：``core.isolation.win32``）：

    * 它自己的 ``/usr/bin`` 放上 PATH（#1203）。``mkdir``、``cut``、``find``、``sort`` 都在
      那里；Git for Windows 只把 ``cmd``/``bin`` 放上全局 PATH，于是本地作业脚本
      ``mkdir: command not found`` 退 127。只给这个子进程放、不放全局：放全局最前面，后端里
      按 PATH 找 ``find``/``sort`` 的都拿到 MSYS 版；放全局最后面，bash 里的 ``find`` 先撞上
      System32 的 ``FIND.EXE``。这正是 Git Bash 的 ``/etc/profile`` 做的事（``/usr/bin`` 排在
      继承来的 Windows PATH 之前）。
    * 命令行按 Cygwin 的规则加引号（#1204），不是 MSVC 的 ``list2cmdline``：MSYS 运行时自己
      把 Windows 命令行切回 argv，引号里的 ``\\`` 折成 ``\``、不带引号的 ``*``/``{a,b}``/``~``
      被展开、``'`` 当引号吃掉、``@文件`` 被换成文件内容。
    """
    if not _WINDOWS or not program:
        return None
    path = Path(program)
    if not path.is_absolute():
        found = shutil.which(program)
        if not found:
            return None
        path = Path(found)
    folder = path.parent
    return str(folder) if (folder / "msys-2.0.dll").is_file() else None


def resolve_bash_executable(name: str) -> str | None:
    """Resolve a Bash external command; Windows PATHEXT does not govern MSYS.

    MSYS accepts executable scripts without an extension. Its /bin and /usr/bin
    refer to the bundled runtime, not the Windows system drive.
    """
    if not _WINDOWS:
        return shutil.which(name)
    if not name or name.startswith('-'):
        return None
    value = name.replace('\\', '/')
    if value.startswith(('/usr/bin/', '/bin/')):
        candidate = Path(bash_shell()).resolve().parent / value.rsplit('/', 1)[-1]
        directories = [candidate.parent]
        leaf = candidate.name
    elif '/' in value:
        candidate = Path(name)
        if not candidate.is_absolute():
            return None
        directories = [candidate.parent]
        leaf = candidate.name
    else:
        directories = [Path(entry) for entry in os.get_exec_path()]
        leaf = name
    for folder in directories:
        for suffix in ('', '.exe') if not leaf.lower().endswith('.exe') else ('',):
            candidate = folder / (leaf + suffix)
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate.absolute())
    return None

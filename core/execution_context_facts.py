"""模型这一轮跑在什么执行上下文里 —— 平台的一等事实，不该由模型探测。

## 为什么需要它（2026-10-29 lujy 诊断包，#1263）

Windows 0.5.8 个人版上，用户问「你能连接本机 ubuntu 子系统吗」。experiment 节点在
受管上下文（随包 MSYS2 bash，受限完整性级别）里跑了两趟共 11 条 ``wsl.exe`` 探针，
九条 stdout 逐字节相同（UTF-16LE 的「拒绝访问。/ Wsl/E_ACCESSDENIED」），两趟 reviewer
各审一遍，整段对话约 1200 万 token，最后得出的结论是一个**在进程起来之前就确定**的事实：
受限完整性级别的进程够不着 WSL 服务，这是写边界的设计结果，不是故障。

同一趟里模型还自己摸出了另外三件确定性的事：Windows 原生程序的输出是 CP936 或 UTF-16LE
（它用 ``od`` 和 ``iconv`` 一条条试）、MSYS 会把 ``/groups`` 这种 Windows 风格选项改写成路径
（它试到 ``MSYS2_ARG_CONV_EXCL='*'`` 才通）、随包的 MSYS 是精简版。

``RFC_WINDOWS_NATIVE_20260907.md`` §4.7 当时就写了「platform_profile 必须多告诉模型五件事：
os、shell 路径、python 路径、multiprocessing 是 spawn、路径分隔符」——一直没落地；
``shared/lib/hostinfo.py`` 只报 CPU / 内存 / 磁盘 / OS 描述。这里把那五件事，加上隔离后端
守住与没守住的不变量，以及后端特有的「别去碰」的事实，机械生成成开局说明里的一段。

## 为什么是机械生成而不是写进 prompt

写进 prompt 的话每个平台一份、后端一改就过期；这里的每一行都从现有的真相源读出来，
不另存一份：``shared.lib.hostinfo.os_description``、``core.isolation.enforcement_snapshot``
（行为探针探出来的不变量，不是按功能名猜的）、``shared.lib.shell.bash_shell``、
``sys.executable``。后端特有的事实按后端名查表，表里只放**由该后端的墙决定**的事
（见 ``_BACKEND_NOTES``），不放会随部署变化的东西。

## 边界

这里只说「这一轮的本机执行上下文」。执行目标（WSL / ssh）落地后，这一段按目标各出一份
（``RFC_WINDOWS_EXECUTION_TARGETS_20261030.md`` D6）。
"""
from __future__ import annotations

import os
import sys
from typing import Any

__all__ = ["gather", "render_section"]

#: 后端特有、由墙决定、模型靠探测要花很多轮才能发现的事实。按后端名查表。
#: 只写**确定性的**事（这台后端的墙保证如此），不写「可能」「一般」。
_BACKEND_NOTES: dict[str, tuple[str, ...]] = {
    "win32": (
        "⛔ `wsl.exe`（以及任何要进 WSL 服务的调用）从这个上下文**不可达**：命令跑在受限"
        "完整性级别下，这是写边界的设计结果，不是故障。不要探测、不要重试、不要换 "
        "`cmd.exe` / `powershell.exe` 再试一遍。用户要在 WSL 里跑的任务，如实回答当前版本"
        "没有为这个项目选 WSL 执行目标（项目设置里可选），可走的路是用户在设置里选一个 "
        "WSL 发行版，或自己在 WSL 里跑；不要替用户做环境诊断。",
        "Windows 原生程序（`whoami.exe` / `cmd.exe` / `powershell.exe`）写出的 CP936 / UTF-16LE "
        "平台已转成 UTF-8 交给你，不用 `od` / `iconv` 试编码；只有**重定向进文件**的原始字节仍是"
        "原编码，读那种文件才需要 `iconv -f CP936 -t UTF-8`（或 `-f UTF-16LE`）。",
        "模型 shell 是随包 Git for Windows 的 MSYS2 bash：传 Windows 风格选项（如 `/groups`、"
        "`/c`）前加 `MSYS2_ARG_CONV_EXCL='*'`，否则会被改写成路径；MSYS 是精简版，缺 `rev` "
        "这类命令；`python3` 可用（随包别名）；multiprocessing 是 spawn，脚本要有 "
        "`if __name__ == \"__main__\":` 守卫。",
    ),
    "darwin": (
        "seatbelt 守住写边界与断网（连 localhost 都不通）；内存 / 进程数没有硬墙，账上已记。",
    ),
    "wsl": (
        "命令跑在 **WSL 发行版（Linux）里**，不是 Windows：GNU bash、发行版自己的 `python3` 与 "
        "apt / conda 装的工具链；平台自带的 Windows Python 与它的依赖在这里**不存在**，"
        "要用的包在发行版里装。",
        "路径用 Linux 写法：Windows 的 `C:\\x\\y` 在这里是 `/mnt/c/x/y`。优先写相对工作目录的"
        "路径；不要写 `C:\\` 形式。",
        "工作目录在 Windows 盘上（`/mnt/<盘>/…`），大量小文件读写比 Linux 原生目录慢一个"
        "量级；中间文件放 `$TMPDIR`（本条命令私有，结束即删），结果写回工作目录。",
        "Windows 程序（`cmd.exe`、`powershell.exe`、任何 `.exe`）在这里**用不了**：墙把 WSL "
        "interop 关了，这是写边界的一部分，不要绕。",
    ),
}


def gather(*, snapshot: dict[str, Any] | None = None, platform: str | None = None,
           shell: str | None = None, python: str | None = None,
           os_text: str | None = None, target: dict[str, Any] | None = None,
           gpu: str | None = None, job_tcp: str | None = None) -> dict[str, Any]:
    """收集事实。参数全是测试注入口；生产路径一个都不传，各自从真相源读。

    任何一处读不到都**不抛**：这段是观察层，它消失或报错都不能拦住主流程 —— 但读不到
    要写成「未知」交出去，不能静默省掉那一行（沉默会被读成「没有这回事」）。
    """
    facts: dict[str, Any] = {"platform": platform or sys.platform}
    # 测试注入了 snapshot 而没注入 target 时，不去问真机的执行目标。
    snapshot_given = snapshot is not None

    if os_text is None:
        try:
            from shared.lib.hostinfo import os_description

            os_text = os_description()
        except Exception:
            os_text = "未知"
    facts["os"] = os_text

    if snapshot is None:
        try:
            from core.isolation import enforcement_snapshot

            snapshot = enforcement_snapshot()
        except Exception as exc:  # pragma: no cover - 观察层兜底
            snapshot = {"backend": None, "enforced": [], "missing_for_unattended": [],
                        "missing_for_attended": [],
                        "unavailable_reason": f"{exc.__class__.__name__}: {exc}"}
    facts["backend"] = snapshot.get("backend")
    facts["enforced"] = list(snapshot.get("enforced") or [])
    facts["missing_for_unattended"] = list(snapshot.get("missing_for_unattended") or [])
    facts["missing_for_attended"] = list(snapshot.get("missing_for_attended") or [])
    facts["unavailable_reason"] = snapshot.get("unavailable_reason")

    if shell is None:
        try:
            from shared.lib.shell import bash_shell

            shell = bash_shell()
        except Exception as exc:
            shell = f"（找不到受管 shell：{exc}）"
    facts["shell"] = shell
    facts["python"] = python or sys.executable
    facts["path_sep"] = "\\" if facts["platform"] == "win32" else os.sep
    if target is None and snapshot_given is False:
        try:
            from core.isolation import target_facts

            target = target_facts()
        except Exception as exc:  # pragma: no cover - 观察层兜底
            target = {"target": "?", "error": f"{exc.__class__.__name__}: {exc}"}
    if target:
        # 执行目标不是本机：系统、shell、python、分隔符都换成那一边的。
        facts["target"] = target.get("target")
        facts["target_error"] = target.get("error")
        if not facts["target_error"] and target.get("write_boundary") is False:
            # 到得了发行版、但墙立不起来（最常见：没装 bubblewrap）—— 与到不了同一种结局。
            facts["target_error"] = (target.get("reason")
                                     or "the write boundary cannot be enforced there")
        if not target.get("error"):
            distro = target.get("distro") or target.get("distro_name") or "WSL"
            facts["os"] = f"{distro}（WSL2，内核 {target.get('kernel') or '未知'}），宿主是 Windows"
            facts["shell"] = "/bin/bash"
            facts["python"] = f"python3（发行版自带，{target.get('python') or '版本未知'}）"
            facts["path_sep"] = "/"
            facts["mount_prefix"] = target.get("mount_prefix")
    if gpu is None and snapshot_given is False:
        # GPU（#893）：看得见几张、能不能分、要不要先问人 —— 与 discover_resources 和
        # submit_job 的拒绝读同一个 gpu_capability。机器上没有卡就不写这一行（每个 run 都付
        # 的字，不付给不存在的东西）；查的时候炸了要写出来。
        try:
            from core.sandbox import gpu_capability

            cap = gpu_capability()
            gpu = cap.sentence() if cap.visible else ""
        except Exception as exc:  # pragma: no cover - 观察层兜底
            gpu = f"GPU：查不出来（{exc.__class__.__name__}: {exc}）"
    facts["gpu"] = gpu or ""
    if job_tcp is None and snapshot_given is False:
        # 本机多进程通信（#1166）：MPI 作业要不要换路，开跑前就该知道，而不是批准后在 MPI_Init 上撞。
        try:
            from core.sandbox import job_tcp_capability

            job_tcp = job_tcp_capability().sentence()
        except Exception as exc:  # pragma: no cover - 观察层兜底
            job_tcp = f"本机多进程通信（MPI 回环 TCP）：查不出来（{exc.__class__.__name__}: {exc}）"
    facts["job_tcp"] = job_tcp or ""
    return facts


def _join(names: list[str]) -> str:
    return ", ".join(f"`{n}`" for n in names) if names else "—"


def render_section(facts: dict[str, Any] | None = None) -> str:
    """开局说明里的「这一轮跑在什么里」那一段。

    每一行都是事实，不是建议。后端特有的「别去碰」写成祈使句，因为那正是模型靠探测
    要花最多轮才能发现、而我们在进程起来之前就知道的事。
    """
    f = facts if facts is not None else gather()
    lines = ["## ⚙️ 这一轮的执行上下文（机械生成，不是猜的）", f"- 系统：{f['os']}"]
    if f.get("target"):
        if f.get("target_error"):
            lines.append(
                f"- 执行目标：`{f['target']}` —— **到不了**（{f['target_error']}）。命令会以 "
                "spawn_failed 结束，不会悄悄改在本机跑；如实告诉用户，修好之前别反复重试。")
        else:
            lines.append(f"- 执行目标：`{f['target']}`（项目设置里选的，不是本机 Windows）")
    backend = f.get("backend")
    if backend:
        lines.append(
            f"- 隔离后端：`{backend}`；守住：{_join(f['enforced'])}；"
            f"无人值守还缺：{_join(f['missing_for_unattended'])}；"
            f"人在场还缺：{_join(f['missing_for_attended'])}"
        )
    else:
        reason = f.get("unavailable_reason") or "原因未知"
        lines.append(f"- 隔离后端：**起不来**（{reason}）；命令不在墙后面跑，账上如实记。")
    if f.get("gpu"):
        lines.append(f"- {f['gpu']}")
    if f.get("job_tcp"):
        lines.append(f"- {f['job_tcp']}")
    lines.append(
        f"- 模型 shell：`{f['shell']}`；python：`{f['python']}`；路径分隔符 `{f['path_sep']}`"
    )
    lines.append("- 写只限自己的作用域（run 根与私有 scratch）；其余目录可读不可写。")
    if not f.get("target_error"):
        for note in _BACKEND_NOTES.get(str(backend or ""), ()):
            lines.append(f"- {note}")
    return "\n".join(lines)

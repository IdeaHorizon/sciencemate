#!/usr/bin/env python3
"""原生 detached 作业：把「本地作业 = 一个容器」的契约用一个脱离终端的进程组实现。

experiment 节点的 ``submit_job scheduler=local`` 按容器模型工作：起一个 detached 的
东西、拿回一个不可变 runtime id、之后按名字 inspect / stop。Docker 没有了（PR C），
契约留着 —— 这里给它一个真身：

    python -I _native_job.py start <control_dir>   # 脱离终端起作业，stdout 打印 runtime id
    python -I _native_job.py run   <control_dir>   # （start 内部用）作业监护进程

``control_dir/launch.json`` 由 ``core.sandbox.prepare_launch(detached=True)`` 写好：
argv（已经是原生后端包过的命令：seatbelt / bwrap / systemd-run）、cwd、env、
stdout / stderr 落点。``record.json`` 是作业的事实账：pid / group / runtime_id /
status / exit_code。``core.sandbox.inspect_container`` 读它。

独立脚本，不 import core：它脱离终端后活得比调用方久。脱离终端、整组收摊这些
**进程控制机制**在 ``shared.lib.process_control``（POSIX 双 fork + 进程组；Windows 的
命名 Job 属于 win32 后端）—— 本脚本以 ``-I`` 起，仓库根不在 sys.path 上，自己追加。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.append(str(Path(__file__).resolve().parents[2]))

from shared.lib import process_control  # noqa: E402
from shared.lib.json_file import read_object, write_object  # noqa: E402


def _run(control_dir: Path) -> int:
    """作业监护进程：起真正的命令、等它退出、把退出码记进 record.json。"""
    launch = json.loads((control_dir / "launch.json").read_text(encoding="utf-8"))
    record_path = control_dir / "record.json"
    record = read_object(record_path)
    process_control.current_group_identity()  # Join the Job before any child can spawn.
    stdout = open(launch["stdout_path"], "ab")
    stderr = open(launch["stderr_path"], "ab")
    try:
        proc = subprocess.Popen(
            launch["argv"],
            cwd=launch["cwd"],
            env=launch["env"],
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
        )
    except OSError as exc:
        stdout.close()
        stderr.close()
        record.update({"status": "dead", "exit_code": 127, "error": f"{type(exc).__name__}: {exc}",
                       "ended_at": time.time()})
        write_object(record_path, record)
        return 127
    stdout.close()
    stderr.close()
    record.update({"child_pid": proc.pid, "status": "running"})
    write_object(record_path, record)
    code = proc.wait()
    record.update({"status": "exited", "exit_code": code, "ended_at": time.time()})
    write_object(record_path, record)
    # 命令结束后整棵进程树死：后台残留不能活过作业本身。请整组退出（含本进程 ——
    # 它本来就要返回了），不等待。
    try:
        group = process_control.Group.from_identity(process_control.current_group_identity())
    except ValueError:
        pass
    else:
        group.terminate(grace_s=0, still_alive=lambda: False)
    return code


def _start(control_dir: Path) -> int:
    """脱离终端起作业：孙进程跑 ``run``；原进程等孙进程登记后打印 runtime id 并返回。"""
    record_path = control_dir / "record.json"
    record = read_object(record_path)
    if not process_control.daemonize():
        # 等孙进程把自己的 pid 写进 record（最多 5 秒），再把 runtime id 交回调用方。
        deadline = time.time() + 5
        while time.time() < deadline:
            current = read_object(record_path)
            if current.get("pid"):
                break
            time.sleep(0.05)
        if not current.get("pid"):
            raise RuntimeError("Detached job did not establish a supervisor; no payload was accepted")
        print(record["runtime_id"], flush=True)
        return 0
    # 组号靠 supervisor 的出生身份间接锚住（#1085 A）。
    #
    # `current_group_identity()` 记的是双 fork 中间那一代的 pid，而那一代当场就
    # `_exit` 了 —— 组号本身没有任何东西能锚住。但**只要 supervisor 还活着，它就
    # 是这个组的成员，组就不会空、组号就不会被重新分配**。所以发信号之前核
    # supervisor 的出生身份，等价于核了组号：身份对得上 ⇒ 组还是原来那个组。
    #
    # 试过让 supervisor 自己当组长（`os.setpgid(0, 0)`，issue 里提的"最小做法"）：
    # 本机全绿，**CI（linux）上作业起不来** —— `inspect` 等不到 running，
    # experiment 的两条取消用例跟着红。没有 Linux 机器当场查，而这条本来就只是
    # 锦上添花：上面那个论证已经让组号不需要自己的锚。所以不做，留在这里说明为什么。
    record.update({"pid": os.getpid(), "group": process_control.current_group_identity(),
                   # 出生身份：号会被复用，出生时刻不会（#1085 A）。
                   "birth_identity": process_control.birth_identity(os.getpid()),
                   # 收尸归谁、这台机器满不满足那个前提（#1085 B）。记下来是为了
                   # 事后审计读得到，而不是靠"部署时应该带了 init"这个假设。
                   "reaping_owner": process_control.ORPHAN_REAPING_OWNER,
                   "status": "starting", "started_at": time.time()})
    write_object(record_path, record)
    devnull = os.open(os.devnull, os.O_RDWR)
    for fd in (0, 1, 2):
        os.dup2(devnull, fd)
    os._exit(_run(control_dir))


def main(argv: list[str]) -> int:
    if len(argv) != 3 or argv[1] not in {"start", "run"}:
        print("usage: _native_job.py start|run <control_dir>", file=sys.stderr)
        return 64
    control_dir = Path(argv[2]).resolve()
    return _start(control_dir) if argv[1] == "start" else _run(control_dir)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

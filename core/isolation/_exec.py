#!/usr/bin/env python3
"""exec 垫片：载荷**起没起来**这件事，由 exec 它的那一层说出来。

独立脚本，不 import core（它跑在墙内，越少越好）。用法：

    python -I _exec.py -- cmd …

为什么要有它：bwrap / sandbox-exec 自己 ``execvp`` 目标命令失败时，只会打一行自己方言的
stderr、然后以一个**普通的非零码**退出（bwrap 1、sandbox-exec 71）。从咽喉看，这和
"命令起来了、跑完了、自己返回非零"一模一样 —— 于是「这台机器没有 latexmk」被下游读成
「稿子编译失败」，模型被叫去改一份完全正确的稿子（2026-09-09 bwrap 三轮；2026-09-15
node20 上 Landlock 启动器同病）。

所以每条隔离链的**最内层**都是我们自己的代码：这里 exec 载荷，exec 不成就按
``core.isolation.LAUNCHER_ERROR_MARKER`` 的约定说一句
``HARNESS_ISOLATION_ERROR exec_failed:<argv0>:<errno>:<strerror>`` 并以 127 退出；咽喉
（``spawn_and_wait``）读到那一行就把这次报成 ``spawn_failed``，下游据此分「缺工具链」
与「稿子有错」。Landlock 启动器（``_landlock_exec.py``）自己 exec、说同一句话；win32
启动器（``_win32_exec.py``）CreateProcess 失败也走同一个标记。
"""

from __future__ import annotations

import errno
import os
import sys

#: 与 ``core.isolation.LAUNCHER_ERROR_MARKER`` 逐字相同。本文件 ``-I`` 跑在墙内、不能 import
#: core，所以各写一份字面量；一致性由 tests/test_launcher_says_the_payload_never_started.py
#: **真起一次**钉住（不是比字符串）。
MARKER = "HARNESS_ISOLATION_ERROR"


def exec_or_report(command: list[str]) -> int:
    """``execvp`` 成功就不再回来；失败时把原因按约定打到 stderr，返回 127。"""
    try:
        os.execvp(command[0], command)
    except OSError as exc:
        code = errno.errorcode.get(exc.errno, str(exc.errno))
        print(f"{MARKER} exec_failed:{command[0]}:{code}:{exc.strerror}",
              file=sys.stderr, flush=True)
        return 127
    return 127  # pragma: no cover - execvp 成功就不会回到这里


def main(argv: list[str]) -> int:
    if len(argv) < 3 or argv[1] != "--":
        print("usage: _exec.py -- cmd ...", file=sys.stderr)
        return 64
    return exec_or_report(argv[2:])


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

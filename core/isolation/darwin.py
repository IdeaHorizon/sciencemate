"""darwin 后端：seatbelt（``sandbox-exec``）。

守到的：I1 写边界（含 ``.git``）、I2 断网（``(deny network*)`` 连 localhost 都不通）、
墙钟与整组清扫（咽喉给的）。**守不到**：内存 / 进程数 / 磁盘硬墙 —— macOS 没有
cgroup，``RLIMIT_NPROC`` 是按用户计的、设低了用户自己的进程都 fork 不出来。这正是
RFC §3.5 「人在场档」的最低集合；无人值守要不要接受，由 ``HARNESS_ENFORCEMENT_POLICY``
定，记账里写得明明白白。

Apple 把 sandbox-exec 标了 deprecated，但系统自己仍在用，Claude Code 也在用。
可用性靠**行为探针**：真起一次、真写一次、真连一次，都被拒才算有。
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

from . import CommandSpec, Invariant
from ._native import (
    NativeLaunch,
    WriteLayers,
    exec_shim_argv,
    payload_environment,
    private_scratch,
    run_ok,
    write_layers,
)

SANDBOX_EXEC = "/usr/bin/sandbox-exec"

_PROBE_CONNECT = (
    "import socket, sys\n"
    "s = socket.socket(); s.settimeout(3)\n"
    "try:\n"
    "    s.connect(('127.0.0.1', 1))\n"
    "except PermissionError:\n"
    "    sys.exit(0)\n"
    "except OSError:\n"
    "    sys.exit(3)\n"
    "sys.exit(4)\n"
)


def _rule(path: Path, verb: str) -> str:
    quoted = str(path).replace("\\", "\\\\").replace('"', '\\"')
    kind = "literal" if path.is_file() else "subpath"
    return f'({verb} file-write* ({kind} "{quoted}"))'


def seatbelt_profile(layers: WriteLayers, *, network: bool) -> str:
    """规则后写的赢 —— 按 ``layers.ordered``（深度升序）发射，于是"最具体的声明赢"。

    这里此前发的是 broad → readonly → priority：一个住在只读根里的可写根会排在只读
    规则**之后**，把它内部声明的只读洞重新 allow 回来（#899-A，darwin 与 linux 同病，
    WP-21 的 macOS 安装路径走的就是这条）。深度序天然处理任意层数的嵌套。
    """
    rules = [
        "(version 1)",
        "(allow default)",
        "(deny file-write*)",
        '(allow file-write* (subpath "/dev"))',
        *(_rule(path, "allow" if mode == "rw" else "deny") for path, mode in layers.ordered),
        *(_rule(path, "deny") for path in layers.git),
    ]
    if not network:
        # ``network*`` 覆盖 ``network-outbound``，而 path-based 的 AF_UNIX connect 就是
        # network-outbound —— 所以宿主控制面 socket（docker.sock 之类）在 darwin 上随
        # 这一条一起被拒（#845 在 linux 原生后端上报的那个洞，seatbelt 这边没有）。
        # 判据落在 test_isolation_carveout.py 的真起进程探针上，不靠这条注释。
        rules.append("(deny network*)")
    return "".join(rules)


def _probe() -> tuple[frozenset[Invariant], str]:
    if sys.platform != "darwin":
        return frozenset(), f"not darwin: {sys.platform}"
    if not os.access(SANDBOX_EXEC, os.X_OK):
        return frozenset(), "sandbox-exec not found"
    with tempfile.TemporaryDirectory(prefix="hf-seatbelt-probe-") as tmp:
        target = Path(tmp) / "probe"
        deny_all = "(version 1)(allow default)(deny file-write*)(deny network*)"
        wrote = run_ok([SANDBOX_EXEC, "-p", deny_all, "/bin/sh", "-c",
                        f"echo x > '{target}'"])
        if wrote or target.exists():
            return frozenset(), "sandbox-exec did not deny a write"
        allow_here = ("(version 1)(allow default)(deny file-write*)"
                      f'(allow file-write* (subpath "{Path(tmp).resolve()}"))')
        if not run_ok([SANDBOX_EXEC, "-p", allow_here, "/bin/sh", "-c",
                       f"echo x > '{Path(tmp).resolve() / 'ok'}'"]):
            return frozenset(), "sandbox-exec denied an allowed write"
        if not run_ok([SANDBOX_EXEC, "-p", deny_all, sys.executable, "-I", "-B", "-c",
                       _PROBE_CONNECT]):
            return frozenset(), "sandbox-exec did not deny a network connect"
    return (
        frozenset({
            Invariant.WRITE_BOUNDARY,
            Invariant.GIT_UNWRITABLE,
            Invariant.NET_DENY,
            # seatbelt 有 deny 原语（``(deny file-write* (subpath ...))``），后写的规则赢
            # —— 按深度序发射就能兑现"可写根内部的只读洞"，任意层数。行为判据见
            # tests/test_isolation_carveout.py（真起进程写一次，rc 必须非 0）。
            Invariant.READONLY_CARVEOUT,
            Invariant.WALLTIME,
            Invariant.GROUP_KILL,
        }),
        "",
    )


class DarwinBackend:
    name = "darwin"

    def __init__(self) -> None:
        self._caps: frozenset[Invariant] | None = None
        self._reason = ""

    def capabilities(self) -> frozenset[Invariant]:
        if self._caps is None:
            self._caps, self._reason = _probe()
        return self._caps

    @property
    def unix_socket_reachable(self) -> bool:
        """seatbelt 的 ``(deny network*)`` 覆盖 ``network-outbound``，而 path-based 的
        AF_UNIX ``connect`` 就是 network-outbound —— 所以宿主控制面 socket 在 darwin 上
        随断网一起被拒。#845 报的是 linux 原生后端上的洞，这边没有。

        这是**实测**结论（``_probe`` 的 ``_PROBE_CONNECT`` 只证明 TCP；unix socket 那条
        由 tests/test_isolation_carveout.py 真起一次 socket server + connect 证明），
        不是从 "network* 听起来管所有网络" 推出来的。"""
        return False

    @property
    def unavailable_reason(self) -> str:
        self.capabilities()
        return self._reason

    def prepare(self, spec: CommandSpec, *, state: Any) -> NativeLaunch:
        del state
        # 私有 scratch：seatbelt 没有 namespace，共享 /tmp 只能靠"不 allow"挡住写 ——
        # 但那样命令就没地方写临时文件了。给它一个自己的，TMPDIR 指过去（#872）。
        scratch_dir = private_scratch()
        layers = write_layers(spec.writable_roots, spec.readonly_roots, scratch_dir=scratch_dir)
        profile = seatbelt_profile(layers, network=spec.network_access)
        exe = shutil.which("sandbox-exec") or SANDBOX_EXEC
        # 最内层是我们的垫片：sandbox-exec 自己 execvp 载荷失败只会
        # `sandbox-exec: execvp() of 'x' failed: …` + rc 71（本机实测），从咽喉看和"命令
        # 跑完返回 71"一模一样；垫片 exec 不成会按约定说一句，咽喉据此报 spawn_failed。
        return NativeLaunch(
            argv=[exe, "-p", profile, *exec_shim_argv(), *spec.argv],
            cwd=spec.cwd,
            env=payload_environment(spec.environment, scratch_dir=scratch_dir, home=spec.home),
            scratch_dir=scratch_dir,
        )

"""win32 后端：受限完整性级别（Low IL）令牌守写边界，Job Object 守资源与整组可杀。

Windows 上没有 seatbelt、没有 Landlock、没有 cgroup。守同一批不变量的**免管理员**原生
原语是（RFC #848 §11 真机验证过）：

* **Low IL 主令牌**：把载荷令牌降到 Low（降自己不需要管理员）。Low-IL 进程写不了默认
  （Medium）标签的对象 —— 也就是整块盘。可写根用 ``icacls /setintegritylevel (OI)(CI)L``
  打 Low 标签**punch 出洞**。→ WRITE_BOUNDARY。
* **可写根下的 ``.git`` 打 Medium 标签**：Medium ≥ Low，Low-IL 写不进；给对象打不高于
  自己令牌的标签不需要特权（打 High 才要 SeRelabelPrivilege=管理员，非管理员打 High 会
  静默失败 —— git_unwritable 一度探针为 false 就栽在这）。→ GIT_UNWRITABLE。
* **Job Object**：JOB_MEMORY / PROCESS_MEMORY（MEM_CAP）、ACTIVE_PROCESS（PIDS_CAP）、
  KILL_ON_JOB_CLOSE（启动器持 job 句柄，被杀即整树亡 —— GROUP_KILL 的第二重保险）。

墙钟与整组清扫由咽喉给（和 darwin / linux 一样）。**守不到**：NET_DENY —— Windows 上没有
免管理员、不改系统状态的断网原语（防火墙规则要管理员，WFP/网络命名空间同理），Low-IL 仍能
连 localhost / 外网。守不到就**记在账上**（``isolation_enforcement`` 事件），由自主档按
``HARNESS_ENFORCEMENT_POLICY`` 决定够不够进无人值守，不假装（[[feedback_absent_check_looks_like_passed_check]]）。

与 darwin / linux 不同：seatbelt / bwrap / landlock 是"把 argv[0] 换成沙箱工具再 exec"；
Windows 不能给自己降 IL 再 exec（要用**新令牌**起进程），所以走一个独立启动器
``_win32_exec.py``：咽喉起 ``python _win32_exec.py run '<spec>' -- cmd``，它施加隔离、
``CreateProcessAsUser`` 起载荷、等它退出、透传退出码。形状照抄 linux.py 的 Landlock 路径
（``python _landlock_exec.py '<roots>' -- cmd``）。

可写面是**纯放行**模型（和 Landlock 一样没有"放行父目录再拒子目录"的共享层）：不能去给
共享的系统 TEMP / 用户缓存打 Low 标签（会污染别的进程在那儿的新文件，且非管理员未必有权）。
所以只给命令自己的根 + 一个私有 scratch 打 Low，worktree 其余部分因为没打标签而天然（Medium）
拒写。

能力靠**行为探针**：``python _win32_exec.py probe`` 真降令牌、真打标签、真写一次、真装满一个
job，把实测结果按 JSON 报回来 —— 不是按功能名猜的承诺。探针在**子进程**里跑（照抄 linux
探 landlock 的做法），这样本模块在非 Windows 上 import 也不会去碰只有 Windows 才有的
``ctypes.wintypes``。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from . import CommandSpec, Invariant
from ._native import (
    NativeLaunch,
    payload_environment,
    private_scratch,
    write_layers,
)

_WIN32_EXEC = Path(__file__).with_name("_win32_exec.py")

# 探针 JSON 字段 → 不变量。这四条纯由 win32 机制供给，只能实测；walltime / group_kill
# 由咽喉另行保证（见模块 docstring），不在此表。
_PROBE_INVARIANTS = {
    "write_boundary": Invariant.WRITE_BOUNDARY,
    "git_unwritable": Invariant.GIT_UNWRITABLE,
    "readonly_carveout": Invariant.READONLY_CARVEOUT,
    "mem_cap": Invariant.MEM_CAP,
    "pids_cap": Invariant.PIDS_CAP,
}


def _probe() -> tuple[frozenset[Invariant], str, bool]:
    """(守到的不变量, 守不到的原因, 墙给的家接不接得住)。"""
    if sys.platform != "win32":
        return frozenset(), f"not win32: {sys.platform}", False
    if not _WIN32_EXEC.exists():
        return frozenset(), "win32 launcher missing", False
    try:
        out = subprocess.run(
            [sys.executable, "-I", "-B", str(_WIN32_EXEC), "probe"],
            capture_output=True, text=True, timeout=60, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return frozenset(), f"win32 probe failed to run: {exc}", False
    try:
        reported = json.loads((out.stdout or "").strip() or "{}")
    except ValueError:
        detail = (out.stderr or "").strip()[:200]
        return frozenset(), f"win32 probe returned no JSON: {detail}", False
    # 咽喉给的两条（和 darwin / linux 一致）：墙钟 + 整组清扫。Windows 上整组清扫由
    # process_control 的 Job(KILL_ON_JOB_CLOSE) 保证，启动器自己的 job 是第二重。
    caps = {Invariant.WALLTIME, Invariant.GROUP_KILL}
    for field, invariant in _PROBE_INVARIANTS.items():
        if reported.get(field) is True:
            caps.add(invariant)
    home = reported.get("payload_home") is True
    if Invariant.WRITE_BOUNDARY not in caps:
        missing = [f for f in _PROBE_INVARIANTS if not reported.get(f)]
        return frozenset(), ("win32 write boundary not enforced here "
                             f"(probe missing: {', '.join(missing) or 'unknown'})"), home
    return frozenset(caps), "", home


class Win32Backend:
    name = "win32"

    def __init__(self) -> None:
        self._caps: frozenset[Invariant] | None = None
        self._reason = ""
        self._home = False

    def capabilities(self) -> frozenset[Invariant]:
        if self._caps is None:
            self._caps, self._reason, self._home = _probe()
        return self._caps

    @property
    def payload_home_scope(self) -> str:
        """墙给的家（``_native.payload_home``）在这台机器上兑现到哪一步。

        ``container``：换掉 ``USERPROFILE`` 之后系统接口给出的 LocalAppData 跟着进了墙给
        的家（探针真起载荷问过、写过）。``env_only``：只有看环境变量的程序进得了家，走
        Known Folder 的程序仍拿到真家、写不进去 —— 组策略把文件夹重定向成绝对路径的
        机器就是这样。缺口默认可见，不默认消失。
        """
        self.capabilities()
        return "container" if self._home else "env_only"

    @property
    def unix_socket_reachable(self) -> bool:
        """Windows 上没有免管理员的断网原语（NET_DENY 本来就守不到，记在账上），
        AF_UNIX socket 同理够得到。缺口默认可见（#845 的同族）。"""
        return True

    @property
    def unavailable_reason(self) -> str:
        self.capabilities()
        return self._reason

    def prepare(self, spec: CommandSpec, *, state: Any) -> NativeLaunch:
        del state
        scratch_dir = private_scratch()
        layers = write_layers(spec.writable_roots, spec.readonly_roots, scratch_dir=scratch_dir)
        limits = spec.limits
        # 可写面用 ``layers.writable``（声明的根 + 私有 scratch + harness 自己的缓存
        # 子目录）。共享 /tmp 与整棵用户缓存已经不在里面了 —— 收窄发生在 write_layers，
        # 每个后端同一份答案（#872）。
        #
        # 只读洞不必靠补集覆盖：Windows 的 deny 原语已经存在（重打 Medium 标签，git
        # 那条就是这么做的），所以这里把 carve-out 原样交给启动器按深度序打标签。
        carve_outs = sorted(layers.carve_outs(), key=lambda item: len(item.parts))
        payload_spec = {
            "writable": [str(p) for p in layers.writable],
            "readonly": [str(p) for p in carve_outs],
            "git": [str(p) for p in layers.git],
            "memory_bytes": int(getattr(limits, "memory_bytes", 0) or 0),
            "pids": int(getattr(limits, "pids", 0) or 0),
        }
        argv = [sys.executable, "-I", "-B", str(_WIN32_EXEC), "run", json.dumps(payload_spec),
                "--", *spec.argv]
        return NativeLaunch(
            argv=argv,
            cwd=spec.cwd,
            env=payload_environment(spec.environment, scratch_dir=scratch_dir, home=spec.home),
            scratch_dir=scratch_dir,
        )

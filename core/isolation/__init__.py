"""执行器分档：模型命令进操作系统之前的那道墙，按「不变量」而不是按「机制」定义。

背景（docs/RFC_EXECUTOR_TIERS_20260904.md）：此前唯一的实现是 Docker 容器，
而且它的长相（只读 rootfs / 显式卷 / 断网 / cgroup / PID 1 监督）被抬成了所有执行
方式的准入契约——于是 Slurm 真实提交被硬拒、个人机器没装 Docker 连后端都起不来。

这里把两件事拆开：

* **不变量**（:class:`Invariant`）是义务：写边界、网络与数据不同框、一条命令拖不垮
  宿主、环境有记录。它们不随执行器变。
* **后端**（:class:`Backend`）是实现：每个后端用宿主最便宜的原生机制守这些义务，
  并用行为探针报告自己**实际**守到了哪几条（:meth:`Backend.capabilities`）。守不到
  的那条不是拒绝启动，而是记进 attempt 账本（``isolation_enforcement`` 事件），
  由自主档决定够不够进无人值守。

调用方只认一个入口：``shared/lib/cancellable_subprocess.spawn_and_wait``。它通过
:func:`select_backend` 拿后端，其余一概不知道 Docker 是什么。

后端：``darwin``（seatbelt）、``linux``（Landlock / bwrap + systemd cgroup）。``auto`` 就是
本平台的原生后端，守不住写边界就拒绝并说明修法 —— 唯一保留的拒绝就是 I1。Docker 镜像
后端已于 PR C 整体删除（wangd 09-04：node20 也切原生）。
"""

from __future__ import annotations

import os
import sys
import weakref
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

EXECUTOR_ENV = "HARNESS_EXECUTOR"
"""选择后端的唯一开关。``auto`` 按宿主挑最便宜的原生后端；显式名字钉死。"""

POLICY_ENV = "HARNESS_ENFORCEMENT_POLICY"
"""``strict``：宿主守不到无人值守最低集合就拒绝进无人值守档（服务器默认）。
``personal``：允许进，但记账并让 UI 标「弱资源墙」（个人机器默认）。"""


class IsolationContractError(ValueError):
    """调用方把契约用错了（非法后端名、非法策略名……）。报错里列合法取值。"""


class Invariant(str, Enum):
    """四条义务拆成可逐条探测、逐条记账的原子项。

    I1 写边界 = WRITE_BOUNDARY + GIT_UNWRITABLE（+ READ_BOUNDARY / READONLY_CARVEOUT 是加强）
       口径进记账 ``write_boundary_scope``：``content_and_metadata``（bwrap / seatbelt /
       win32）| ``content_only``（Landlock —— 内核没有管元数据的访问位，可写根之外
       任意自有文件的 chmod / utime / setxattr 照样改得动，写内容与截断仍挡住）|
       ``none``。同一个词在两种后端下含义不同，账上就得是两个词（#1095，与
       ``net_scope`` 同形）。
       READONLY_CARVEOUT = "调用方声明的、**嵌套在某个可写根内部**的只读根，交给后端之后
       真的写不进去吗"。它需要 deny 原语（bwrap ``--ro-bind`` / seatbelt ``deny`` /
       win32 重打 Medium 标签）；纯放行清单（Landlock）只能靠补集覆盖，兑不兑现得起来
       是逐条命令的事，那条挂在 ``NativeLaunch.unmet``。#899-D 请求这一项的理由是：
       没有它，``EnforcementRecord`` **结构上就报告不了这个缺口**，每个节点只能自己
       镜像一遍后端的绑定次序去猜 —— 而镜像本身就是下一个会悄悄过期的假设。
       判定不必再猜：``WriteLayers.grants_write(path)`` 是公开的。
    I2 网络与数据不同框 = NET_DENY（取物段是 NET_ALLOWLIST）。口径进记账
       ``net_scope``：``all``（bwrap/seatbelt 整个网络）| ``tcp``（Landlock ABI 4 只管
       TCP bind/connect）| ``none``。一个 Linux 上没有 bwrap 的机器守到的是 tcp，不是 all。
    I3 拖不垮宿主 = MEM_CAP + PIDS_CAP + CPU_CAP + WALLTIME + GROUP_KILL
       DISK_CAP 曾经也在这里，**已删**：容器年代由 ``--storage-opt`` 提供，Docker 拆除
       之后没有任何原生后端能给（linux 的 per-scope 磁盘配额要先给文件系统配 quota，
       darwin / win32 根本没有）。零供给方的义务不是"暂时缺"，是**一条谁都满足不了的
       最低要求** —— 它让每台原生机器的 ``missing_for_unattended`` 永远非空，于是共享
       执行器上无人值守被永久拒绝，而给出的补救（"让管理员补上资源限制"）根本够不着。
       哪天真有后端能给，再连着供给一起加回来（#798）。
       CPU_CAP 是反过来的一例：linux 的 ``systemd-run --user --scope -p CPUQuota=`` 一直
       在真的限 CPU，词表里却**没有这个名字**，于是 ``sandbox_contract.cpus`` 没有任何
       东西能对上 —— 模型把它读成契约、据此判断"单核争抢"，差点砍掉一个正在多核跑的
       作业（#841）。有机制就得有名字，否则记账问不出这件事。
    I4 环境有记录 = ENV_PINNED（只有镜像后端能承诺「一致」，其余后端只记录）
    """

    WRITE_BOUNDARY = "write_boundary"
    GIT_UNWRITABLE = "git_unwritable"
    READ_BOUNDARY = "read_boundary"
    READONLY_CARVEOUT = "readonly_carveout"
    NET_DENY = "net_deny"
    NET_ALLOWLIST = "net_allowlist"
    MEM_CAP = "mem_cap"
    PIDS_CAP = "pids_cap"
    CPU_CAP = "cpu_cap"
    WALLTIME = "walltime"
    GROUP_KILL = "group_kill"
    ENV_PINNED = "env_pinned"


UNRECOVERABLE: frozenset[Invariant] = frozenset({
    Invariant.WRITE_BOUNDARY,
    Invariant.GIT_UNWRITABLE,
    Invariant.NET_DENY,
})
"""违反了就回不来的那类（数据没了、历史分叉、外泄）。每一档都硬性，起不来就拒绝执行。"""

UNATTENDED_MINIMUM: frozenset[Invariant] = UNRECOVERABLE | {
    Invariant.GROUP_KILL,
    Invariant.MEM_CAP,
    Invariant.PIDS_CAP,
    Invariant.WALLTIME,
}
"""无人值守档的最低集合：没人在键盘前，能踩刹车的那几条必须齐。

**只放"没有它就会毁掉东西"的**：内存与进程数管的是把宿主拖垮，墙钟与整组清扫管的
是停得下来。CPU 配额不在内 —— 一条吃满 CPU 的命令是慢，不是毁；把它写进最低集合会
让 macOS / Windows 全体失去无人值守，换来的只是"更慢"这一种后果被提前拦下。
DISK_CAP 也不在内，而且整条已从词表删除（见 :class:`Invariant`）。
"""

ATTENDED_MINIMUM: frozenset[Invariant] = UNRECOVERABLE | {
    Invariant.GROUP_KILL,
    Invariant.WALLTIME,
}
"""人在场档的最低集合：跑飞了人会按停止，只要保证按得死、写不出去。"""

_POLICIES = ("strict", "personal")

LAUNCHER_ERROR_MARKER = "HARNESS_ISOLATION_ERROR"
"""墙的最内层（我们自己的启动器：``_landlock_exec.py`` / ``_exec.py`` / ``_win32_exec.py``）
说「载荷没起来」时打到 stderr 的那一行的开头：``HARNESS_ISOLATION_ERROR <kind>:<detail>``。

为什么要有这一行：bwrap / sandbox-exec / Landlock 启动器自己 exec 载荷失败时，都只是以一个
**普通的非零码**退出（bwrap 1、sandbox-exec 71、启动器 traceback）。从咽喉看，这和"命令
起来了、跑完了、自己返回非零"一模一样 —— 于是「这台机器没有 latexmk」被下游读成「稿子
编译失败」，模型被叫去改一份完全正确的稿子（2026-09-09 bwrap；2026-09-15 node20 Landlock）。

所以每条链的最内层都是我们的代码，它 exec 不成就说这一句，咽喉据此报 ``spawn_failed``
（:func:`launcher_refusal`）。启动器 ``-I`` 跑在墙内、不 import core，所以三处各写一份
字面量；一致性由 tests/test_launcher_says_the_payload_never_started.py **真起一次**钉住。
"""


def launcher_refusal(stderr: bytes | str) -> str | None:
    """启动器说「载荷没起来」的那一行；没有这一行 → None。

    只认**行首**的标记。伪造这一行的只能是载荷自己 —— 骗到的也只是它自己这一次的归属，
    所以不必再防。
    """
    text = stderr.decode("utf-8", errors="replace") if isinstance(stderr, bytes) else stderr
    prefix = LAUNCHER_ERROR_MARKER + " "
    for line in text.splitlines():
        if line.startswith(prefix):
            return line.rstrip()
    return None


def enforcement_policy() -> str:
    raw = os.environ.get(POLICY_ENV, "").strip().lower()
    if not raw:
        return "personal"
    if raw not in _POLICIES:
        raise IsolationContractError(
            f"{POLICY_ENV}={raw!r} is not a policy; valid values: {', '.join(_POLICIES)}"
        )
    return raw


#: ``CommandSpec.home`` 的合法取值：``host`` 用户的家（只读可见），``own`` 墙给的家。
HOMES = ("host", "own")


@dataclass(frozen=True)
class CommandSpec:
    """一条模型命令进后端之前被说清楚的全部事实。

    ``limits`` 是 :class:`core.sandbox.SandboxLimits`（本 PR 不搬它）；``network_access``
    只允许框架拼的取物命令为 True，普通模型命令永远 False。

    ``home`` —— 这个程序住哪个家（:data:`HOMES`）：

    * ``"host"``（默认）：程序是**用户的**（模型代码、用户自己装的工具）。它看见用户真实
      的家 —— elan 按 ``~/.elan`` 找 Lean、conda / juliaup / R 用户库同理，「本机就是环境」。
      写照旧只限声明的根、私有 tmp 与持久缓存。
    * ``"own"``：程序是**平台的**、自成一体（随包 tectonic 与它调的 biber）。墙给它一个
      干净、完整、按本平台原生词汇布置的家（``_native.payload_home``），缓存区持久 ——
      它在每台机器上的行为都一样，不取决于用户的家里恰好有什么。2026-09-23 那台干净
      Windows 上 tectonic 5 秒 ``os error 5``、而开发机上从没露面，就是它依赖了用户家里
      「恰好已经存在」的一个目录。
    """

    argv: tuple[str, ...]
    cwd: str
    writable_roots: tuple[Path, ...]
    readonly_roots: tuple[Path, ...] = ()
    limits: Any = None
    environment: Mapping[str, str] | None = None
    network_access: bool = False
    home: str = "host"

    def __post_init__(self) -> None:
        if not self.argv or any(not isinstance(part, str) or "\x00" in part for part in self.argv):
            raise IsolationContractError("argv must be a non-empty sequence of NUL-free strings")
        if not self.writable_roots:
            raise IsolationContractError("a model command needs at least one writable root")
        if self.home not in HOMES:
            raise IsolationContractError(
                f"home={self.home!r} is not a home; valid values: {', '.join(HOMES)}")


@runtime_checkable
class Launch(Protocol):
    """后端交回来的可起进程：``argv`` 由 spawn_and_wait 起，取消走 ``terminate``。

    可选属性：``cwd`` / ``env``（原生后端用，咽喉原样应用）。``terminate`` 返回 False
    表示后端这边没有可停的东西，咽喉立刻杀进程组；返回 None / True 表示后端会把
    进程带下来，咽喉等一小段 drain。
    """

    argv: list[str]

    def terminate(self, *, remove: bool = ...) -> Any: ...

    def cleanup(self) -> None: ...


@runtime_checkable
class Backend(Protocol):
    name: str

    def capabilities(self) -> frozenset[Invariant]:
        """行为探针探出来的事实，不是按功能名猜的承诺。空集 = 这台机器上起不来。"""
        ...

    def prepare(self, spec: CommandSpec, *, state: Any) -> Launch: ...


@dataclass(frozen=True)
class EnforcementRecord:
    """一个 attempt 实际跑在什么墙后面。进 transcript，也是 UI「弱资源墙」标签的来源。"""

    backend: str
    policy: str
    enforced: tuple[str, ...]
    missing_for_unattended: tuple[str, ...]
    missing_for_attended: tuple[str, ...]
    extras: dict[str, Any] = field(default_factory=dict)

    def as_event(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "policy": self.policy,
            "enforced": list(self.enforced),
            "missing_for_unattended": list(self.missing_for_unattended),
            "missing_for_attended": list(self.missing_for_attended),
            **self.extras,
        }


def _orphan_reaping_fact() -> dict[str, Any]:
    from shared.lib import process_control

    try:
        reaped = process_control.orphans_get_reaped()
    except Exception:
        reaped = None
    return {"owner": process_control.ORPHAN_REAPING_OWNER, "pid1_reaps": reaped}


def enforcement_record(backend: Backend) -> EnforcementRecord:
    caps = backend.capabilities()
    # NET_DENY 的口径随账走：后端自己报（linux 的 all/tcp/none）；没报的按"守到=all、
    # 没守到=none"—— 别让一个只断 TCP 的机器在账上长得和整个断网一样。
    net_scope = getattr(backend, "net_scope", None)
    if not isinstance(net_scope, str):
        net_scope = "all" if Invariant.NET_DENY in caps else "none"
    # path-based AF_UNIX socket 是文件系统对象：Landlock 的 FS 规则族不管 connect(2)，
    # network namespace 也不管它。写边界与断网两条账看起来盖住了控制面 socket，其实
    # 没有（#845）。后端答不上来就按"够得到"记 —— 缺口默认可见，不默认消失。
    reachable = getattr(backend, "unix_socket_reachable", True)
    if not isinstance(reachable, (bool, str)):
        reachable = True
    # WRITE_BOUNDARY 的口径，和 net_scope 同理（#1095）。
    #
    # Landlock 的访问位里**没有、也不可能有**管元数据的那一类：内核文档明写
    # chmod/chown/utime/setxattr 这些"文件相关动作"限制不了。于是同一个
    # `write_boundary` 在 bwrap 下含内容+元数据，在 Landlock 下只含内容与目录项 ——
    # 可写根之外任意自有文件的权限位、时间戳、xattr 照样改得动（实测 chmod 0644→0600
    # rc=0）。能力声明比兑现的宽，而 strict 策略、UI 与节点从账上看不出差别。
    #
    # 后端答不上来时按"窄"记：把没兑现的当成兑现了，正是这条要修的东西。
    write_scope = getattr(backend, "write_boundary_scope", None)
    if not isinstance(write_scope, str):
        write_scope = (
            "content_and_metadata" if Invariant.WRITE_BOUNDARY in caps else "none")
    return EnforcementRecord(
        backend=backend.name,
        policy=enforcement_policy(),
        enforced=tuple(sorted(c.value for c in caps)),
        missing_for_unattended=tuple(sorted(c.value for c in UNATTENDED_MINIMUM - caps)),
        missing_for_attended=tuple(sorted(c.value for c in ATTENDED_MINIMUM - caps)),
        extras={
            "net_scope": net_scope,
            "write_boundary_scope": write_scope,
            # 这台机器的 PID 1 会不会收割孤儿僵尸（#1085 B）。受管作业的
            # supervisor 是双 fork 出来的孤儿，框架里没有进程能对它 waitpid，
            # 所以收尸的 owner 是部署方（带 init 的 PID 1）。**探一次记下来**，
            # 不靠"部署时应该带了 init"这个假设 —— 假设不会在账上留下缺口。
            "orphan_reaping": _orphan_reaping_fact(),
            "unix_socket_reachable": reachable,
            "pids_event_source": getattr(backend, "pids_event_source", None),
            # 墙给的家兑现到哪一步（``_native.payload_home``）。POSIX 上家就是环境变量
            # 指过去的那个目录，没有别的间接层；Windows 要看 Known Folder 跟不跟着走，
            # 由后端的探针答。
            "payload_home": getattr(backend, "payload_home_scope", "container"),
        },
    )


# ── 后端选择 ──────────────────────────────────────────────────────────────────

_BACKEND_NAMES = ("auto", "darwin", "linux", "win32")
_cache: dict[str, Backend] = {}
_auto_cache: str | None = None


def _instantiate(name: str) -> Backend:
    backend = _cache.get(name)
    if backend is not None:
        return backend
    if name == "darwin":
        from .darwin import DarwinBackend

        backend = DarwinBackend()
    elif name == "linux":
        from .linux import LinuxBackend

        backend = LinuxBackend()
    elif name == "win32":
        from .win32 import Win32Backend

        backend = Win32Backend()
    else:  # pragma: no cover - guarded by _BACKEND_NAMES
        raise IsolationContractError(f"no implementation for backend {name!r}")
    _cache[name] = backend
    return backend


def native_backend_name() -> str | None:
    """这台机器对应的原生后端名；没有对应实现（如 win32）时为 None。"""
    if sys.platform == "darwin":
        return "darwin"
    if sys.platform.startswith("linux"):
        return "linux"
    if sys.platform == "win32":
        return "win32"
    return None


_NATIVE_REMEDY = {
    "darwin": "macOS needs /usr/bin/sandbox-exec (ships with the OS)",
    "linux": "run a kernel >= 5.13 with Landlock enabled (and a seccomp policy that does "
             "not block it); kernel >= 6.7 (Landlock ABI 4) also gives net_deny without "
             "bubblewrap or user namespaces",
    "win32": "Windows enforces the write boundary with a Low-integrity token via "
             "_win32_exec.py; needs a 64-bit CPython with ctypes (bundled). Run "
             "`python -I core/isolation/_win32_exec.py probe` to see which invariant fails",
}


def _resolve_auto() -> str:
    """``auto`` = 本平台的原生后端，没有别的。

    原生后端守不住写边界就拒绝（唯一保留的拒绝 = I1），报错里说怎么修。没有别的兜底：
    Docker 镜像后端已删（wangd 2026-09-04）。
    """
    global _auto_cache
    if _auto_cache is not None:
        return _auto_cache
    native = native_backend_name()
    if native is None:
        raise IsolationContractError(
            f"no native isolation backend for {sys.platform} "
            "(darwin / linux / win32 are the implemented platforms)"
        )
    backend = _instantiate(native)
    if Invariant.WRITE_BOUNDARY in backend.capabilities():
        _auto_cache = native
        return native
    why = getattr(backend, "unavailable_reason", "") or "unavailable"
    raise IsolationContractError(
        f"the {native} backend cannot enforce the write boundary (I1) on this host: {why}. "
        f"Fix: {_NATIVE_REMEDY[native]}"
    )


def select_backend(name: str | None = None) -> Backend:
    """按 ``HARNESS_EXECUTOR``（或显式 ``name``）交出后端实例；非法名字把合法值念出来。"""
    raw = (name if name is not None else os.environ.get(EXECUTOR_ENV, "auto")).strip().lower()
    if raw not in _BACKEND_NAMES:
        raise IsolationContractError(
            f"{EXECUTOR_ENV}={raw!r} is not a backend; valid values: {', '.join(_BACKEND_NAMES)}"
        )
    resolved = _resolve_auto() if raw == "auto" else raw
    return _instantiate(resolved)


def enforcement_snapshot() -> dict:
    """这台机器守得住哪几条不变量 —— **只读报告，不起任何进程**。

    给 `research-platform doctor` 这类"说清楚现状"的地方用。它们需要的是一个
    答案，不是一个后端实例；直接调 `select_backend()` 会撞上"只有咽喉能拿后端"
    那道闸 —— 而那道闸拦得对：拿到后端的下一步通常就是起进程。

    守不住也是一个答案（`backend: None` + 原因），不是一次失败。
    """
    try:
        backend = select_backend()
    except IsolationContractError as exc:
        return {
            "backend": None,
            "enforced": [],
            "missing_for_unattended": sorted(c.value for c in UNATTENDED_MINIMUM),
            "missing_for_attended": sorted(c.value for c in ATTENDED_MINIMUM),
            "unavailable_reason": str(exc),
        }
    return enforcement_record(backend).as_event()


def attempt_capability(backend: Backend | None = None) -> dict[str, str]:
    """一个 attempt 出生时冻进 manifest 的「谁来守」。

    原生后端冻的是后端名。``core.sandbox._local_manifest`` 与平台的
    ``local_execution._freeze_attempt_sandbox_manifest`` 都从这里拿，不各写一份。
    """
    chosen = backend or select_backend()
    return {"backend": chosen.name}


def _reset_for_tests() -> None:
    global _auto_cache
    _cache.clear()
    _auto_cache = None
    _recorded_ids.clear()


# ── 记账：每个 state（= 每个 run 进程内）只记一次 ────────────────────────────────

_recorded_ids: set[int] = set()
"""按 ``id(state)`` 记，不用 WeakSet：``core.state.State`` 是 eq=True 的 dataclass，
不可 hash，塞进 WeakSet 会炸（test_shell_probe_only 真跑抓到的）。对象死了由
``weakref.finalize`` 把 id 撤掉，免得 id 被复用后误判「已记过」；不可 weakref 的
对象（测试替身）就留着 id，进程内够用。"""


def record_enforcement_once(state: Any, backend: Backend) -> EnforcementRecord | None:
    """把「这条 run 跑在什么墙后面」写进 transcript，一条 run 一次。

    返回写下的记录；已经记过则返回 None。
    """
    key = id(state)
    if key in _recorded_ids:
        return None
    record = enforcement_record(backend)
    append = getattr(state, "append_transcript", None)
    if callable(append):
        append("isolation_enforcement", **record.as_event())
    _recorded_ids.add(key)
    try:
        weakref.finalize(state, _recorded_ids.discard, key)
    except TypeError:
        pass
    return record


__all__ = [
    "ATTENDED_MINIMUM",
    "Backend",
    "CommandSpec",
    "EXECUTOR_ENV",
    "EnforcementRecord",
    "HOMES",
    "Invariant",
    "IsolationContractError",
    "LAUNCHER_ERROR_MARKER",
    "Launch",
    "POLICY_ENV",
    "UNATTENDED_MINIMUM",
    "UNRECOVERABLE",
    "enforcement_policy",
    "enforcement_record",
    "enforcement_snapshot",
    "launcher_refusal",
    "native_backend_name",
    "record_enforcement_once",
    "select_backend",
]

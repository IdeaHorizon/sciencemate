#!/usr/bin/env python3
"""Landlock 写边界启动器：把自己关进只许写这几个根的规则集，然后 exec 目标命令。

独立脚本，不 import core（它跑在墙内，越少越好）。用法：

    python -I _landlock_exec.py abi                        # 内核 Landlock ABI（0 = 不可用）
    python -I _landlock_exec.py [--network deny|allow] '<roots json>' -- cmd …

Landlock 不需要 user namespace（bwrap 需要），内核 ≥ 5.13 的普通用户就能用 ——
所以它是 CI runner、node20、HPC 作业里的写墙主力。两条边，各有各的答案：

* **它是纯放行清单**：放行了一个根就放行了它下面的一切，挡不住根下面的 ``.git``。
  所以 ``.git`` 那条不靠它挡，靠**结构判据**：可写根里不许含 gitdir 或 worktree
  指针文件，含了就拒绝派发（``linux.py::LinuxBackend.prepare``）。守不住就不装守住。
* **ABI ≥ 4（内核 ≥ 6.7）管 TCP**：声明 ``handled_access_net`` 且不加任何 net 规则 =
  全部 TCP bind/connect 拒绝，不需要 userns。口径要如实：**只管 TCP**，UDP / ICMP /
  unix socket 不在内（bwrap ``--unshare-net`` 是整个网络命名空间）。记账里叫
  ``net_scope: tcp``。ABI < 4 的内核只传第一个字段，否则老内核 E2BIG。

2026-09-08 node20 真机：bwrap 装了但被 ``apparmor_restrict_unprivileged_userns=1``
挡住建不出 namespace，而 Landlock ABI 4 在 —— 这条路就是为它开的（issue #797）。

代码搬自 deploy/sandbox/sandbox_payload.py（PR C 删那边）。
"""

from __future__ import annotations

import ctypes
import errno
import json
import os
import stat
import sys

SYS_LANDLOCK_CREATE_RULESET = 444
SYS_LANDLOCK_ADD_RULE = 445
SYS_LANDLOCK_RESTRICT_SELF = 446
LANDLOCK_CREATE_RULESET_VERSION = 1
LANDLOCK_RULE_PATH_BENEATH = 1
WRITE_FILE = 1 << 1
REMOVE_DIR = 1 << 4
REMOVE_FILE = 1 << 5
MAKE_CHAR = 1 << 6
MAKE_DIR = 1 << 7
MAKE_REG = 1 << 8
MAKE_SOCK = 1 << 9
MAKE_FIFO = 1 << 10
MAKE_BLOCK = 1 << 11
MAKE_SYM = 1 << 12
REFER = 1 << 13
TRUNCATE = 1 << 14
PR_SET_NO_NEW_PRIVS = 38
# Landlock ABI 4：网络访问位（include/uapi/linux/landlock.h）
NET_BIND_TCP = 1 << 0
NET_CONNECT_TCP = 1 << 1
#: 网络规则要求的最低 ABI。
NET_ABI = 4

# ── seccomp：Landlock 只管 TCP，UDP / ICMP / DNS 由它补（#1180）────────────────
#
# Landlock ABI 4 的网络规则只覆盖 TCP bind/connect；UDP、raw socket 不在内，内核低于 6.7
# 时连 TCP 都没有。seccomp 过滤器在 ``socket(2)`` 这一步按地址族拒掉 AF_INET / AF_INET6
# （返回 EPERM）：没有 inet socket 就没有 TCP、没有 UDP、没有 DNS 查询。不需要 userns，
# 不需要 root，只要 NO_NEW_PRIVS（上面已经设了）。
#
# **故意不碰 AF_UNIX**：python multiprocessing、X11、本机 IPC 都靠它，而且它不是出网；
# 账上 ``unix_socket_reachable`` 照旧如实为 True。也不碰 AF_NETLINK：glibc 的 getaddrinfo
# 列接口要用它，拦了会把一类本地程序打坏，而它本身不通外网。
#
# 过滤器按**本机架构**写死（seccomp_data.arch 对不上就放行 —— 别的 ABI 这里不懂，不装懂）。
# 支持 x86_64 与 aarch64；别的架构不装过滤器，由 linux.py 的探针如实探出 ``net_scope: tcp``。
PR_SET_SECCOMP = 22
SECCOMP_MODE_FILTER = 2
SECCOMP_RET_ALLOW = 0x7FFF0000
SECCOMP_RET_ERRNO = 0x00050000
AF_INET = 2
AF_INET6 = 10
#: machine → (AUDIT_ARCH_*, __NR_socket)。
_SECCOMP_ARCH = {
    "x86_64": (0xC000003E, 41),
    "aarch64": (0xC00000B7, 198),
}
_BPF_LD_W_ABS = 0x20
_BPF_JEQ_K = 0x15
_BPF_RET_K = 0x06


class SockFilter(ctypes.Structure):
    _fields_ = [("code", ctypes.c_uint16), ("jt", ctypes.c_uint8),
                ("jf", ctypes.c_uint8), ("k", ctypes.c_uint32)]


class SockFprog(ctypes.Structure):
    _fields_ = [("len", ctypes.c_ushort), ("filter", ctypes.POINTER(SockFilter))]


def inet_socket_filter(machine: str) -> list[tuple[int, int, int, int]] | None:
    """禁建 AF_INET / AF_INET6 socket 的 BPF 程序（code, jt, jf, k）；架构不认识就 None。

    seccomp_data 的布局：nr@0、arch@4、args[0] 低 32 位@16（小端）。跳转是相对偏移：
    下一条 = pc + 1 + jt/jf。
    """
    arch = _SECCOMP_ARCH.get(machine)
    if arch is None:
        return None
    audit_arch, nr_socket = arch
    return [
        (_BPF_LD_W_ABS, 0, 0, 4),                 # 0: A = arch
        (_BPF_JEQ_K, 0, 5, audit_arch),           # 1: 不是本机 ABI → 7 ALLOW
        (_BPF_LD_W_ABS, 0, 0, 0),                 # 2: A = nr
        (_BPF_JEQ_K, 0, 3, nr_socket),            # 3: 不是 socket(2) → 7 ALLOW
        (_BPF_LD_W_ABS, 0, 0, 16),                # 4: A = args[0]（domain）
        (_BPF_JEQ_K, 2, 0, AF_INET),              # 5: AF_INET → 8 EPERM
        (_BPF_JEQ_K, 1, 0, AF_INET6),             # 6: AF_INET6 → 8 EPERM
        (_BPF_RET_K, 0, 0, SECCOMP_RET_ALLOW),    # 7
        (_BPF_RET_K, 0, 0, SECCOMP_RET_ERRNO | errno.EPERM),  # 8
    ]


def deny_inet_sockets(libc: ctypes.CDLL) -> bool:
    """装上过滤器；架构不认识或内核拒绝（EINVAL：没有 seccomp filter）返回 False，不抛。

    返回值只给调用方记账用；真相由 linux.py 的探针决定（它在 ``--network deny`` 下真的去建
    一个 UDP socket，建得出就是 ``tcp`` 口径、建不出才是 ``inet``）。
    """
    import platform

    program = inet_socket_filter(platform.machine())
    if program is None:
        return False
    insns = (SockFilter * len(program))(*[SockFilter(*insn) for insn in program])
    prog = SockFprog(len(program), insns)
    return libc.prctl(PR_SET_SECCOMP, SECCOMP_MODE_FILTER, ctypes.byref(prog), 0, 0) == 0


class RulesetAttr(ctypes.Structure):
    """ABI 1-3 的结构：只有文件系统一个字段。老内核按 size 判，多传一个字段就 E2BIG。"""

    _fields_ = [("handled_access_fs", ctypes.c_uint64)]


class RulesetAttrV4(ctypes.Structure):
    """ABI ≥ 4：多一个 ``handled_access_net``。声明了它、又不加任何 net 规则，就是全拒。"""

    _fields_ = [("handled_access_fs", ctypes.c_uint64), ("handled_access_net", ctypes.c_uint64)]


class PathBeneathAttr(ctypes.Structure):
    _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]


def landlock_abi() -> int:
    if not sys.platform.startswith("linux"):
        return 0
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        abi = libc.syscall(
            SYS_LANDLOCK_CREATE_RULESET, ctypes.c_void_p(), 0, LANDLOCK_CREATE_RULESET_VERSION
        )
    except OSError:
        return 0
    return max(0, int(abi))


def restrict_writes_to(roots: list[str], *, network: bool = False) -> None:
    """把自己关进「只许写这几个根」的规则集；``network=False`` 时再断网：ABI ≥ 4 用 Landlock
    拒 TCP bind/connect，再加一层 seccomp 禁建 AF_INET / AF_INET6 socket（UDP / DNS / 老内核
    上的 TCP 一起断，#1180）。

    ``network=True`` 只给框架拼的取物命令（``CommandSpec.network_access``），普通模型命令
    永远 False。这里不假装：守到哪一层由 ``linux.py`` 的探针真连一次、真建一个 UDP socket
    探出来，记账 ``net_scope`` 是 ``inet`` / ``tcp`` / ``none``。
    """
    libc = ctypes.CDLL(None, use_errno=True)
    abi = libc.syscall(
        SYS_LANDLOCK_CREATE_RULESET, ctypes.c_void_p(), 0, LANDLOCK_CREATE_RULESET_VERSION
    )
    if abi < 1:
        raise OSError(ctypes.get_errno(), "landlock_unavailable")
    handled = (WRITE_FILE | REMOVE_DIR | REMOVE_FILE | MAKE_CHAR | MAKE_DIR | MAKE_REG
               | MAKE_SOCK | MAKE_FIFO | MAKE_BLOCK | MAKE_SYM)
    if abi >= 2:
        handled |= REFER
    if abi >= 3:
        handled |= TRUNCATE
    if abi >= NET_ABI and not network:
        # 声明网络位、不加任何 net 规则 = 全部 TCP bind/connect 拒绝。判据 C：删掉这一行
        # 的 handled_access_net，`test_landlock_denies_tcp_without_bwrap` 必须转红。
        attr: ctypes.Structure = RulesetAttrV4(handled, NET_BIND_TCP | NET_CONNECT_TCP)
    else:
        attr = RulesetAttr(handled)
    ruleset_fd = libc.syscall(
        SYS_LANDLOCK_CREATE_RULESET, ctypes.byref(attr), ctypes.sizeof(attr), 0
    )
    if ruleset_fd < 0:
        raise OSError(ctypes.get_errno(), "landlock_create_ruleset_failed")
    try:
        # 普通文件只接受这四位，其余一律 EINVAL(22)。
        #
        # 2026-09-15 在 Linux CI 上被判据抓到两次：补集覆盖会枚举出**文件**路径
        # （放行兄弟条目时文件和目录一起出来），整条 landlock_add_rule 因此失败，
        # 现场只有一句 `landlock_add_rule_failed:<某个 .py>`，而失败会把整套隔离
        # 打掉 —— 于是连"可写根里已存在的文件"都写不进去，症状离病根很远。
        #
        # 第一次我写成"减掉目录专属那几位"，**漏了 REFER**（它也是目录语义：
        # 跨目录 link/rename），CI 照样 EINVAL。改成**取交集**：列出文件**能**接受
        # 的，其余全掩掉。减黑名单会漏，取白名单不会 —— 这一位的代价是整套隔离。
        # 这套 ruleset 只声明**写侧**的位（读和执行不在 handled 里，见上面），
        # 所以普通文件能接受的就这两位。EXECUTE / READ_FILE 本模块没定义 ——
        # 写进来会是 NameError，而那会在真机上把整套隔离打掉。
        file_ok = WRITE_FILE | TRUNCATE
        for root in roots:
            fd = os.open(root, os.O_PATH | os.O_CLOEXEC)
            try:
                try:
                    is_dir = stat.S_ISDIR(os.stat(fd).st_mode)
                except OSError:
                    is_dir = True          # 看不出来就按目录发，行为与旧版一致
                rights = handled if is_dir else (handled & file_ok)
                rule = PathBeneathAttr(rights, fd)
                if libc.syscall(SYS_LANDLOCK_ADD_RULE, ruleset_fd, LANDLOCK_RULE_PATH_BENEATH,
                                ctypes.byref(rule), 0) != 0:
                    raise OSError(ctypes.get_errno(), f"landlock_add_rule_failed:{root}")
            finally:
                os.close(fd)
        if libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
            raise OSError(ctypes.get_errno(), "no_new_privs_failed")
        if libc.syscall(SYS_LANDLOCK_RESTRICT_SELF, ruleset_fd, 0) != 0:
            raise OSError(ctypes.get_errno(), "landlock_restrict_self_failed")
    finally:
        os.close(ruleset_fd)
    if not network:
        # Landlock 之外再补 seccomp：建不出 AF_INET / AF_INET6 socket 就没有 UDP、DNS、
        # 老内核上的 TCP（#1180）。装不上不抛 —— 探针会如实探出口径是 tcp 还是 inet。
        # 判据 D：删掉这一行，`test_net_deny_covers_udp_without_bwrap` 必须转红。
        deny_inet_sockets(libc)


def main(argv: list[str]) -> int:
    if len(argv) == 2 and argv[1] == "abi":
        print(landlock_abi(), flush=True)
        return 0
    network = False
    rest = argv[1:]
    if rest and rest[0] == "--network":
        # 显式两个词，不用布尔旗：`--network` 后面少写一个词就是用法错误，而不是
        # 静默落到某个默认值上。默认（不给）= deny，与 CommandSpec 的默认一致。
        if len(rest) < 2 or rest[1] not in ("allow", "deny"):
            print("usage: _landlock_exec.py [--network allow|deny] '<roots json>' -- cmd ...",
                  file=sys.stderr)
            return 64
        network = rest[1] == "allow"
        rest = rest[2:]
    try:
        roots = json.loads(rest[0])
        assert rest[1] == "--" and len(rest) > 2
        command = rest[2:]
    except (IndexError, ValueError, AssertionError):
        print("usage: _landlock_exec.py [--network allow|deny] '<roots json>' -- cmd ...",
              file=sys.stderr)
        return 64
    try:
        restrict_writes_to([str(r) for r in roots], network=network)
    except OSError as exc:
        print(f"HARNESS_ISOLATION_ERROR landlock_setup_failed:{exc}", file=sys.stderr, flush=True)
        return 127
    # exec 不成（argv0 不在 PATH、shebang 指的解释器不在、ENOEXEC……）要**说出来**。
    # 不说的话这里以 Python traceback + 非零码退出，咽喉看到的和"命令自己失败了"
    # 一模一样，于是「这台机器没有 latexmk」被读成「稿子编译失败」—— 2026-09-15 node20
    # 复现：PATH 缺 ~/.local/bin，模型被叫去改一份完全正确的稿子。这一行的格式与
    # _exec.py / core.isolation.LAUNCHER_ERROR_MARKER 逐字相同，咽喉据此报 spawn_failed；
    # 一致性由 tests/test_launcher_says_the_payload_never_started.py 真起一次钉住。
    try:
        os.execvp(command[0], command)
    except OSError as exc:
        code = errno.errorcode.get(exc.errno, str(exc.errno))
        print(f"HARNESS_ISOLATION_ERROR exec_failed:{command[0]}:{code}:{exc.strerror}",
              file=sys.stderr, flush=True)
        return 127
    return 127  # pragma: no cover - execvp 成功就不会回到这里


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

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
    """把自己关进「只许写这几个根」的规则集；``network=False`` 且 ABI ≥ 4 时再断 TCP。

    ``network=True`` 只给框架拼的取物命令（``CommandSpec.network_access``），普通模型命令
    永远 False。ABI < 4 的内核**没有**网络规则可用 —— 这里不假装，调用方按
    ``landlock_abi()`` 记账（``linux.py`` 只在 abi ≥ 4 时声明 NET_DENY）。
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

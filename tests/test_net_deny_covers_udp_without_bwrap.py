"""Landlock 只管 TCP；UDP / DNS / 老内核上的 TCP 由 seccomp 补（#1180）。

## 为什么

没有 bwrap 的 Linux（node20：AppArmor 限制非特权 userns）只能走 Landlock。Landlock ABI ≥ 4
只能拦 TCP 的 bind/connect，UDP、raw socket 不在覆盖范围内；内核低于 6.7 时连 TCP 都没有。
一个 DNS 查询就是一个 UDP 包 —— 「断网」漏着 UDP 不叫断网。

## 做法

启动器在 Landlock 之后再装一层 seccomp 过滤器：``socket(2)`` 的地址族是 AF_INET / AF_INET6
就返回 EPERM。不需要 userns、不需要 root。**故意不碰 AF_UNIX**（multiprocessing、本机 IPC）
和 AF_NETLINK（glibc 列接口）。口径由 ``linux.py`` 的探针真建一个 UDP socket 探出来：
建不出 → ``net_scope: inet``；建得出 → ``tcp``。

## 判据 D（变异）

删掉 ``_landlock_exec.py`` ``restrict_writes_to`` 末尾的 ``deny_inet_sockets(libc)`` 那一行
→ 本文件里 UDP 相关的用例全红，而 ``test_landlock_denies_tcp_without_bwrap`` 照旧绿。
"""
from __future__ import annotations

import platform
import subprocess
import sys

import pytest

from core import isolation
from core.isolation import Invariant
from core.isolation._landlock_exec import _SECCOMP_ARCH, inet_socket_filter
from core.isolation.linux import _LANDLOCK_EXEC, LinuxBackend, _landlock_argv

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Landlock / seccomp 是 Linux 的")


def _abi() -> int:
    out = subprocess.run([sys.executable, "-I", str(_LANDLOCK_EXEC), "abi"],
                         capture_output=True, text=True, timeout=10, check=False)
    return int((out.stdout or "0").strip() or 0)


_ABI = _abi() if sys.platform.startswith("linux") else 0
_needs_landlock = pytest.mark.skipif(_ABI < 1, reason=f"这个内核没有 Landlock（abi {_ABI}）")
_needs_known_arch = pytest.mark.skipif(
    platform.machine() not in _SECCOMP_ARCH,
    reason=f"seccomp 过滤器只为 {sorted(_SECCOMP_ARCH)} 写了，这是 {platform.machine()}")

#: 建一个 UDP socket：被拒 → 0，建成 → 4，其它 → 3。
_UDP = (
    "import errno, socket, sys\n"
    "try:\n    socket.socket(socket.AF_INET, socket.SOCK_DGRAM).close()\n"
    "except PermissionError:\n    sys.exit(0)\n"
    "except OSError as e:\n    sys.exit(0 if e.errno in (errno.EACCES, errno.EPERM) else 3)\n"
    "sys.exit(4)\n"
)
_UDP6 = _UDP.replace("socket.AF_INET,", "socket.AF_INET6,")
#: unix socket 对（multiprocessing 的管道）必须照常建得出：拦错了地址族会把一类本地程序打坏。
_UNIX = ("import socket, sys\n"
         "a, b = socket.socketpair(); a.sendall(b'x'); assert b.recv(1) == b'x'\n"
         "sys.exit(4)\n")
#: DNS 查询也是 UDP：getaddrinfo 要么被拒，要么解析失败，不许成功发出去。
_DNS = ("import socket, sys\n"
        "try:\n    socket.getaddrinfo('example.com', 80)\n"
        "except (PermissionError, socket.gaierror):\n    sys.exit(0)\n"
        "sys.exit(4)\n")


def _rc(argv: list[str]) -> int:
    return subprocess.run(argv, capture_output=True, timeout=20, check=False).returncode


def test_the_filter_program_is_what_the_kernel_will_read():
    """BPF 程序的形状：本机架构不对放行、不是 socket(2) 放行、AF_INET/AF_INET6 → EPERM、其余放行。
    跳转偏移手算一遍钉住 —— 一个偏移错就是整台机器要么断不了网要么什么都建不出来。"""
    prog = inet_socket_filter("x86_64")
    assert prog is not None and len(prog) == 9
    allow, eperm = 7, 8

    def target(idx: int, taken: bool) -> int:
        code, jt, jf, _k = prog[idx]
        assert code == 0x15, idx
        return idx + 1 + (jt if taken else jf)

    assert target(1, taken=False) == allow, "别的 ABI 要放行"
    assert target(3, taken=False) == allow, "不是 socket(2) 要放行"
    assert target(5, taken=True) == eperm and target(5, taken=False) == 6
    assert target(6, taken=True) == eperm and target(6, taken=False) == allow
    assert prog[allow] == (0x06, 0, 0, 0x7FFF0000)
    assert prog[eperm][3] == (0x00050000 | 1), "EPERM 而不是 KILL：载荷要能把错报出来"
    assert prog[1][3] == 0xC000003E and prog[3][3] == 41
    assert inet_socket_filter("aarch64")[3][3] == 198
    assert inet_socket_filter("riscv64") is None, "不认识的架构不装过滤器，由探针如实报 tcp"


@_needs_landlock
@_needs_known_arch
def test_deny_blocks_udp_and_dns_but_not_unix_sockets(tmp_path):
    roots = [str(tmp_path), "/dev"]
    py = [sys.executable, "-I", "-B", "-c"]
    # 对照：allow 下 UDP / UDP6 建得出。
    assert _rc([*_landlock_argv(roots, network=True), *py, _UDP]) == 4, "对照组 UDP 都建不出来，探针无效"
    assert _rc([*_landlock_argv(roots, network=False), *py, _UDP]) == 0, "deny 下 UDP socket 还建得出来"
    assert _rc([*_landlock_argv(roots, network=False), *py, _UDP6]) == 0, "deny 下 UDP6 socket 还建得出来"
    assert _rc([*_landlock_argv(roots, network=False), *py, _DNS]) == 0, "deny 下 DNS 查询发出去了"
    assert _rc([*_landlock_argv(roots, network=False), *py, _UNIX]) == 4, "deny 下 unix socketpair 被误伤"


@_needs_landlock
@_needs_known_arch
def test_the_backend_reports_the_inet_scope_it_proved(monkeypatch):
    backend = LinuxBackend()
    backend._probe()
    if not backend._landlock:
        pytest.skip(f"这台机器 Landlock 写墙不可用：{backend.unavailable_reason}")
    monkeypatch.setattr(backend, "_bwrap", None)      # 装作没有 bwrap —— node20 的样子
    assert backend._landlock_net, backend.unavailable_reason
    assert Invariant.NET_DENY in backend.capabilities()
    assert backend.net_scope == "inet", (
        f"探针应证明两层都在；现在是 {backend.net_scope!r}（{backend.unavailable_reason}）")
    record = isolation.enforcement_record(backend).as_event()
    assert record["net_scope"] == "inet" and "net_deny" in record["enforced"]
    assert record["unix_socket_reachable"] is True, "unix socket 没拦，账上就得如实写通"


@_needs_landlock
@_needs_known_arch
def test_a_model_command_cannot_send_udp_through_the_backend(tmp_path, monkeypatch):
    """走真入口 spawn_and_wait。"""
    import asyncio

    from shared.lib.cancellable_subprocess import spawn_and_wait

    monkeypatch.setenv(isolation.EXECUTOR_ENV, "linux")
    isolation._reset_for_tests()
    backend = isolation.select_backend("linux")
    backend._probe()
    if not backend._landlock:
        pytest.skip(f"这台机器 Landlock 写墙不可用：{backend.unavailable_reason}")
    monkeypatch.setattr(backend, "_bwrap", None)

    class _State:
        events: list = []
        kill_event = None

        def append_transcript(self, event_type, **payload):
            self.events.append({"event": event_type, **payload})

    mine = tmp_path / "mine"
    mine.mkdir()
    state = _State()
    try:
        status, rc, _out, err = asyncio.run(spawn_and_wait(
            sys.executable, "-I", "-c", _UDP, state=state, timeout=30,
            cwd=str(mine), writable_roots=[mine], readonly_roots=[]))
        assert status == "done", err
        assert rc == 0, f"模型命令建出了 UDP socket（rc={rc}）: {err!r}"
        record = [e for e in state.events if e["event"] == "isolation_enforcement"][0]
        assert record["net_scope"] == "inet"
    finally:
        isolation._reset_for_tests()

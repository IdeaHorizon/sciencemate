"""Landlock ABI 4 断 TCP —— 不靠 bwrap、不靠 user namespace（issue #797，WP-01）。

## 为什么要这条

2026-09-08 node20 真机：bubblewrap 装了，但 Ubuntu 24.04 的
``kernel.apparmor_restrict_unprivileged_userns=1`` 让它建不出 namespace
（``setting up uid map: Permission denied``）。而内核 6.8 的 Landlock ABI 4 在 —— 用一个
独立探针证明过：声明 ``handled_access_net`` 且不加任何 net 规则，TCP connect 就是
``EACCES``，不需要 root、不改 sysctl。代码里当时**没有**这一位（``_landlock_exec.py``
的 ruleset 只有文件系统一个字段），于是 ``capabilities()`` 只在 bwrap 在场时才给
NET_DENY —— 机器上有、内核给了、我们自己没接。

## 口径

只管 **TCP bind/connect**。UDP / ICMP / unix socket 不在内，bwrap ``--unshare-net``
才是整个网络命名空间。所以记账里叫 ``net_scope: tcp``，UI 原样带出，不许圆。

## 判据 C（变异）

删掉 ``_landlock_exec.py`` 里 ``RulesetAttrV4(handled, NET_BIND_TCP | NET_CONNECT_TCP)``
那一行的网络位（换回 ``RulesetAttr(handled)``）→ 本文件全红。
"""
from __future__ import annotations

import socket
import subprocess
import sys

import pytest

from core import isolation
from core.isolation import Invariant
from core.isolation.linux import _LANDLOCK_EXEC, LinuxBackend, _landlock_argv

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Landlock 是 Linux 的")


def _abi() -> int:
    out = subprocess.run([sys.executable, "-I", str(_LANDLOCK_EXEC), "abi"],
                         capture_output=True, text=True, timeout=10, check=False)
    return int((out.stdout or "0").strip() or 0)


_ABI = _abi() if sys.platform.startswith("linux") else 0
_needs_abi4 = pytest.mark.skipif(
    _ABI < 4, reason=f"Landlock ABI {_ABI} < 4：这个内核没有网络规则（需要 ≥ 6.7）"
)

#: 连本机一个**真在听**的端口：被拒 → 0，连上 → 4，其它 → 3。
_PROBE = (
    "import errno, socket, sys\n"
    "s = socket.socket(); s.settimeout(3)\n"
    "try:\n    s.connect(('127.0.0.1', int(sys.argv[1])))\n"
    "except PermissionError:\n    sys.exit(0)\n"
    "except OSError as e:\n"
    "    sys.exit(0 if e.errno in (errno.EACCES, errno.EPERM) else 3)\n"
    "sys.exit(4)\n"
)


@pytest.fixture()
def listening_port():
    """对照组：端口必须真的有人听。没人听时不加沙箱也 ECONNREFUSED，分不出真假。"""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    try:
        yield listener.getsockname()[1]
    finally:
        listener.close()


def _run(argv: list[str]) -> int:
    return subprocess.run(argv, capture_output=True, timeout=20, check=False).returncode


@_needs_abi4
def test_launcher_denies_tcp_connect_and_the_control_still_connects(tmp_path, listening_port):
    roots = [str(tmp_path.resolve()), "/dev"]
    probe = [sys.executable, "-I", "-c", _PROBE, str(listening_port)]
    assert _run([*_landlock_argv(roots, network=True), *probe]) == 4, (
        "对照组先失败了：--network allow 都连不上，说明监听器或探针本身坏了"
    )
    rc = _run([*_landlock_argv(roots, network=False), *probe])
    assert rc == 0, f"--network deny 之下 connect 没被拒（rc={rc}）—— 网络位没接上"


@_needs_abi4
def test_launcher_without_the_flag_defaults_to_deny(tmp_path, listening_port):
    """不给 --network 就是 deny，与 CommandSpec.network_access 的默认一致。"""
    roots = [str(tmp_path.resolve()), "/dev"]
    argv = [sys.executable, "-I", str(_LANDLOCK_EXEC), __import__("json").dumps(roots), "--",
            sys.executable, "-I", "-c", _PROBE, str(listening_port)]
    assert _run(argv) == 0


def test_launcher_refuses_a_dangling_network_flag():
    out = subprocess.run([sys.executable, "-I", str(_LANDLOCK_EXEC), "--network"],
                         capture_output=True, text=True, timeout=10, check=False)
    assert out.returncode == 64
    assert "--network allow|deny" in out.stderr


@_needs_abi4
def test_backend_declares_net_deny_with_tcp_scope_without_bwrap(monkeypatch):
    backend = LinuxBackend()
    backend._probe()
    if not backend._landlock:
        pytest.skip(f"这台机器 Landlock 写墙不可用：{backend.unavailable_reason}")
    if not backend._landlock_net:
        pytest.fail(f"ABI {_ABI} ≥ 4 却没探到网络拒绝：{backend.unavailable_reason}")
    # 装作没有 bwrap —— 那正是 node20 的样子
    monkeypatch.setattr(backend, "_bwrap", None)
    caps = backend.capabilities()
    assert Invariant.NET_DENY in caps
    assert Invariant.WRITE_BOUNDARY in caps
    assert backend.net_scope == "tcp"
    record = isolation.enforcement_record(backend).as_event()
    assert record["net_scope"] == "tcp", "只断 TCP 的机器在账上不许长得和整个断网一样"
    assert "net_deny" in record["enforced"]


@_needs_abi4
def test_a_model_command_cannot_connect_through_the_backend(tmp_path, listening_port, monkeypatch):
    """走真入口 spawn_and_wait：装作没有 bwrap，模型命令的 connect 必须被拒。"""
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
    status, rc, _out, err = asyncio.run(spawn_and_wait(
        sys.executable, "-I", "-c", _PROBE, str(listening_port),
        state=state, timeout=30, cwd=str(mine), writable_roots=[mine], readonly_roots=[]))
    try:
        assert status == "done", err
        assert rc == 0, f"connect 没被拒（rc={rc}）: {err!r}"
        record = [e for e in state.events if e["event"] == "isolation_enforcement"][0]
        assert record["net_scope"] == "tcp"
    finally:
        isolation._reset_for_tests()


@_needs_abi4
def test_acquisition_commands_keep_the_network(tmp_path, listening_port, monkeypatch):
    """框架拼的取物命令 network_access=True 必须还连得上 —— 断网不许误伤白名单取物段。"""
    import asyncio

    from shared.lib.cancellable_subprocess import spawn_and_wait

    monkeypatch.setenv(isolation.EXECUTOR_ENV, "linux")
    isolation._reset_for_tests()
    backend = isolation.select_backend("linux")
    backend._probe()
    if not backend._landlock:
        pytest.skip(backend.unavailable_reason)
    monkeypatch.setattr(backend, "_bwrap", None)

    class _State:
        events: list = []
        kill_event = None

        def append_transcript(self, *_a, **_k):
            pass

    mine = tmp_path / "mine"
    mine.mkdir()
    status, rc, _out, err = asyncio.run(spawn_and_wait(
        sys.executable, "-I", "-c", _PROBE, str(listening_port),
        state=_State(), timeout=30, cwd=str(mine), writable_roots=[mine], readonly_roots=[],
        network_access=True))
    try:
        assert status == "done", err
        assert rc == 4, f"取物命令被断网了（rc={rc}）: {err!r}"
    finally:
        isolation._reset_for_tests()

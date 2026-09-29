"""命令面地址的词表：scheme 解析 / 传输默认 / **交出去的地址一定是具体的**。"""
from __future__ import annotations

import ast
import io
import json
from pathlib import Path

import pytest

from core import worker_addressing as wa

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_bare_path_parses_as_unix_for_backward_compat():
    # 注册表里的老地址、POSIX 上经环境变量交给 worker 的裸路径都不带 scheme。
    transport, endpoint = wa.parse_control_address("/tmp/harness/s-abc.sock")
    assert transport == wa.TRANSPORT_UNIX
    assert endpoint == Path("/tmp/harness/s-abc.sock")


def test_unix_scheme_parses():
    transport, endpoint = wa.parse_control_address("unix:/tmp/x.sock")
    assert transport == wa.TRANSPORT_UNIX and endpoint == Path("/tmp/x.sock")


def test_tcp_scheme_parses_host_and_port():
    transport, endpoint = wa.parse_control_address("tcp:127.0.0.1:54321")
    assert transport == wa.TRANSPORT_TCP and endpoint == ("127.0.0.1", 54321)


def test_control_address_is_a_bare_path_for_unix(tmp_path):
    address = wa.control_address(tmp_path, "p1", "s1", transport=wa.TRANSPORT_UNIX)
    # 裸路径（不带 `unix:` 前缀）—— 老 worker 把它当路径读。
    assert not address.startswith("unix:")
    assert Path(address).is_absolute()


def test_default_transport_is_tcp_on_windows(monkeypatch):
    monkeypatch.delenv("HARNESS_CONTROL_TRANSPORT", raising=False)
    monkeypatch.setattr(wa.sys, "platform", "win32")
    assert wa.default_transport() == wa.TRANSPORT_TCP


def test_default_transport_is_unix_on_posix(monkeypatch):
    monkeypatch.delenv("HARNESS_CONTROL_TRANSPORT", raising=False)
    monkeypatch.setattr(wa.sys, "platform", "linux")
    assert wa.default_transport() == wa.TRANSPORT_UNIX


def test_env_forces_the_transport(monkeypatch):
    monkeypatch.setenv("HARNESS_CONTROL_TRANSPORT", "tcp")
    monkeypatch.setattr(wa.sys, "platform", "linux")
    assert wa.default_transport() == wa.TRANSPORT_TCP


# ── 绑好之后怎么把真实地址报回去 ────────────────────────────────────────────


def _string_constants(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }


# ── 交出去的地址必须是**具体**的 ────────────────────────────────────────────


def test_the_tcp_address_carries_a_concrete_port():
    """**#926 那个死结的根因判据。**

    交 `tcp:...:0` 就等于说「端口只有 worker 知道」，于是必须再发明一条通道把它问
    回来 —— 而那条通道（注册表行）要等第一个请求才存在，第一个请求要等连接。
    Windows 上 `tcp` 是唯一传输，这个环每次都死锁。

    所以判据不落在「有没有汇报机制」上，落在**源头**：后端交出去的地址就是具体的。
    """
    address = wa.control_address("/anything", "p1", "s1", transport=wa.TRANSPORT_TCP)
    transport, endpoint = wa.parse_control_address(address)
    assert transport == wa.TRANSPORT_TCP
    host, port = endpoint
    assert host == wa.TCP_LOOPBACK_HOST
    assert port > 0, f"端口必须是具体的，拿到 {address}"


def test_two_sessions_do_not_get_the_same_port():
    """端口是问内核要的，不是算出来的 —— 两个会话不能撞在一起。"""
    a = wa.control_address("/anything", "p1", "s1", transport=wa.TRANSPORT_TCP)
    b = wa.control_address("/anything", "p2", "s2", transport=wa.TRANSPORT_TCP)
    assert a != b


def test_the_picked_port_is_actually_bindable():
    """挑完就得能绑上 —— 否则「后端定地址」这条规则从第一步就不成立。"""
    import socket

    port = wa.pick_loopback_port()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind((wa.TCP_LOOPBACK_HOST, port))   # 抢不到就直接抛
        server.listen(1)
        assert server.getsockname()[1] == port


def test_nobody_hands_out_an_unresolved_address_any_more():
    """机械闸：`:0`（让内核挑）这个形状不许再出现在交地址的那条路上。

    它一旦回来，「谁告诉谁端口」就又需要一条通道，而那条通道是 #926 的死结本身。
    """
    source = (REPO_ROOT / "core" / "worker_addressing.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    handed = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "control_address"
    ]
    assert handed, "control_address 不见了？"
    literals = {
        n.value for n in ast.walk(handed[0])
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
    }
    assert not any(v.endswith(":0") for v in literals), literals

"""模型 python 子进程在 Windows 上要能起来：`env -i` 保留系统变量、审计钩子不炸。

两条都是"Mac 全绿、Windows 全红且指向别处"的坑：`env -i` 剥掉 SYSTEMROOT → python.exe
加载不了网络/加密 DLL；审计钩子引用不存在的 `socket.AF_UNIX` → 每次 socket 以 AttributeError
炸而不是给出该给的 PermissionError。
"""
from __future__ import annotations

import socket
import subprocess
import sys

import pytest

from nodes.experiment.tools import safe_bash as sb
from shared.lib import platform_env


def test_the_whitelist_consumes_the_shared_windows_system_list():
    # 一处回答：模型 python 子进程的白名单必须包含那份 Windows 系统变量名单，不各写一份。
    assert platform_env.WINDOWS_SYSTEM_ENV <= sb._SAFE_PYTHON_CHILD_ENV


#: 用户会话总线：Core 造墙那一层（systemd-run --user）需要，模型代码不需要。
_SESSION_BUS_ENV = ("XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS")


def test_model_children_never_get_the_user_session_bus(monkeypatch):
    """#849 节点侧：宿主 AF_UNIX 可达（#845）修好之前，总线地址不交给模型子进程。

    判据落在注入结果上，不落在名单上：即便 platform_env 以后把会话变量加进透传名单，
    模型子进程的 `env -i` 赋值里也不得出现它们。
    """
    for name in _SESSION_BUS_ENV:
        assert name not in sb._SAFE_PYTHON_CHILD_ENV
    monkeypatch.setattr(sb, "_system_env_passthrough", lambda: {
        "SYSTEMROOT": r"C:\Windows",
        "XDG_RUNTIME_DIR": "/run/user/1000",
        "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1000/bus",
    })
    command = sb._command_with_safe_child_env("python foo.py", {"PYTHONPATH": "/x"})
    assert "SYSTEMROOT=" in command
    for name in _SESSION_BUS_ENV:
        assert f"{name}=" not in command, command


def test_env_i_reinjects_windows_system_vars(monkeypatch):
    """`env -i` 起干净环境，Windows 系统变量要从 os.environ 补回来。"""
    monkeypatch.setenv("SYSTEMROOT", r"C:\Windows")
    monkeypatch.setenv("COMSPEC", r"C:\Windows\System32\cmd.exe")
    command = sb._command_with_safe_child_env("python foo.py", {"PYTHONPATH": "/x"})
    assert "SYSTEMROOT=C:\\Windows" in command
    assert "COMSPEC=" in command
    assert "PYTHONPATH=/x" in command
    # 仍然是 env -i /bin/bash 的契约，没被破坏。
    assert "env" in command and "-i" in command and "/bin/bash" in command


def test_env_i_is_a_noop_on_posix(monkeypatch):
    for name in platform_env.passthrough_names():
        monkeypatch.delenv(name, raising=False)
    command = sb._command_with_safe_child_env("python foo.py", {"PYTHONPATH": "/x"})
    assert "SYSTEMROOT" not in command


def test_caller_value_wins_over_the_system_default(monkeypatch):
    monkeypatch.setenv("TEMP", r"C:\Windows\Temp")
    command = sb._command_with_safe_child_env("python foo.py", {"TEMP": "/run/local/tmp", "PYTHONPATH": "/x"})
    assert "TEMP=/run/local/tmp" in command
    assert r"TEMP=C:\Windows\Temp" not in command


def test_audit_hook_blocks_all_sockets_when_af_unix_is_absent():
    """模拟 Windows（socket 没有 AF_UNIX）：审计钩子必须给 PermissionError，不能以
    AttributeError 炸。这条同时钉住那处 getattr —— 改回 `_hf_socket.AF_UNIX` 就红。"""
    payload = (
        "import socket\n"
        "try:\n"
        "    socket.socket(socket.AF_INET)\n"
        "    print('NOT_BLOCKED')\n"
        "except PermissionError:\n"
        "    print('BLOCKED')\n"
    )
    # 先把 AF_UNIX 删掉（socket 已缓存进 sys.modules），preamble 里的 getattr 就回落 None。
    # 模拟：删掉 AF_UNIX（Windows 上本就没有，容忍）。socket 已缓存进 sys.modules，
    # preamble 里的 getattr 就回落 None。
    strip = "import socket\ntry:\n del socket.AF_UNIX\nexcept AttributeError:\n pass\n"
    code = strip + sb._with_no_child_process_audit(payload)
    result = subprocess.run(
        [sys.executable, "-c", code], stdin=subprocess.DEVNULL,
        capture_output=True, text=True, timeout=10, check=False,
    )
    assert "BLOCKED" in result.stdout, result.stderr
    assert "AttributeError" not in result.stderr, "审计钩子不该因缺 AF_UNIX 而崩"


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="平台没有 AF_UNIX（Windows）")
def test_audit_hook_still_allows_af_unix_where_it_exists():
    """POSIX 上 AF_UNIX 在 → 行为不变（放行 AF_UNIX）。用一对真 socketpair 验，避开
    AF_UNIX 路径长度上限。"""
    payload = (
        "import socket\n"
        "a, b = socket.socketpair(socket.AF_UNIX)\n"   # AF_UNIX 通过审计
        "a.close(); b.close()\n"
        "print('OK')\n"
    )
    code = sb._with_no_child_process_audit(payload)
    result = subprocess.run(
        [sys.executable, "-c", code], stdin=subprocess.DEVNULL,
        capture_output=True, text=True, timeout=10, check=False,
    )
    assert result.returncode == 0 and "OK" in result.stdout, result.stderr

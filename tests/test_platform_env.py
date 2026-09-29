"""`shared.lib.platform_env`：子进程要能起来所必需的系统变量，一处回答。"""
from __future__ import annotations

from shared.lib import platform_env


def test_passthrough_names_cover_the_windows_dll_roots():
    names = platform_env.passthrough_names()
    # 缺了这几个，Windows 上 python.exe 连网络/加密 DLL 都加载不了。
    for essential in ("SYSTEMROOT", "SYSTEMDRIVE", "COMSPEC", "PATHEXT", "TEMP", "TMP"):
        assert essential in names, essential


def test_passthrough_picks_only_present_vars():
    env = {"SYSTEMROOT": r"C:\Windows", "PATH": "/x", "IRRELEVANT": "y"}
    out = platform_env.system_env_passthrough(env)
    assert out == {"SYSTEMROOT": r"C:\Windows"}


def test_passthrough_is_empty_on_a_posix_environ():
    # 典型 POSIX 环境里这些名字都不在 → 空 dict（对 POSIX 无副作用）。
    env = {"PATH": "/usr/bin", "HOME": "/home/x", "LANG": "C"}
    assert platform_env.system_env_passthrough(env) == {}


def test_values_are_taken_verbatim():
    env = {"COMSPEC": r"C:\Windows\System32\cmd.exe"}
    assert platform_env.system_env_passthrough(env)["COMSPEC"] == r"C:\Windows\System32\cmd.exe"

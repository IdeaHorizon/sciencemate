"""本人的目录，本人什么时候都进得去 —— 不取决于建它那一刻是不是以管理员身份在跑（#1263 诊断包）。

lujy 10-29 的诊断包：``home_marker_unreadable``，``harness-state\\users\\<uid>\\.platform-tenant-identity.json``
本人读不了、owner 都读不出来（``GetNamedSecurityInfo failed: 5``）；同一个数据根里 ``payload``、一个工作区的
owner 是 ``BUILTIN\\Administrators`` —— 这个应用以管理员身份跑过。

机制：CPython 3.12.4 起，Windows 上 ``mkdir(mode=0o700)``（以及 ``tempfile.mkdtemp``）给的是受保护的
``D:P(SY)(BA)(OW)`` —— 没有用户本人的 SID，只认 owner。提权时建的 owner 是 Administrators，之后本人
未提权就被关在外面。11-04 在 9800x3d 上用同一个未提权令牌做 AccessCheck 复现过：owner=本人可读，
owner=Administrators 拒绝；按继承建的两种都可读。
"""
from __future__ import annotations

import ast
import os
import stat
import sys
from pathlib import Path

import pytest

from shared.lib.private_files import mkdir_private

ROOT = Path(__file__).resolve().parents[1]

#: 只在 Linux 上跑的代码：mode 就是 POSIX 权限位，没有 Windows 那层意思。
_POSIX_ONLY = {"core/isolation/_wsl_entry.py": "跑在 WSL 发行版里（Linux）"}
_SCANNED = ("core", "shared", "platform_runtime.py", "platform/backend/app")


@pytest.mark.skipif(os.name == "nt", reason="POSIX 权限位")
def test_posix_keeps_owner_only(tmp_path) -> None:
    target = tmp_path / "a" / "home"
    mkdir_private(target)
    assert stat.S_IMODE(target.stat().st_mode) == 0o700
    target.chmod(0o755)
    mkdir_private(target)                      # 已存在也收紧（原来那处 chmod 的作用）
    assert stat.S_IMODE(target.stat().st_mode) == 0o700
    with pytest.raises(FileExistsError):
        mkdir_private(target, exist_ok=False)


def _offending_calls(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        values = [*node.args, *(k.value for k in node.keywords)]
        modes = [v for v in values if isinstance(v, ast.Constant) and v.value == 0o700]
        if name in {"mkdir", "makedirs"} and modes:
            found.append(f"{path.relative_to(ROOT)}:{node.lineno} {name}(…0o700…)")
        if name == "mkdtemp" and any(k.arg == "dir" for k in node.keywords):
            found.append(f"{path.relative_to(ROOT)}:{node.lineno} mkdtemp(dir=…)")
    return found


def test_nothing_builds_lasting_directories_with_mode_0o700() -> None:
    """扫写法：``mkdir/makedirs(…0o700…)`` 与「在持久位置 mkdtemp(dir=…)」都要走 mkdir_private。

    mkdtemp 不带 dir= 的（%TEMP% 里一次性的东西）不算：它们活不到下一次运行。
    """
    offending = []
    for entry in _SCANNED:
        base = ROOT / entry
        files = [base] if base.is_file() else sorted(base.rglob("*.py"))
        for path in files:
            rel = str(path.relative_to(ROOT)).replace(os.sep, "/")
            if "/tests/" in f"/{rel}" or rel in _POSIX_ONLY or rel == "shared/lib/private_files.py":
                continue
            offending += _offending_calls(path)
    assert not offending, (
        "这些地方按 0o700 建目录：Windows 上那是只认 owner 的受保护 DACL，以管理员身份跑过一次，"
        "之后正常运行就进不去（home_marker_unreadable）。改用 shared.lib.private_files.mkdir_private：\n  "
        + "\n  ".join(offending))


# ── Windows：同一个未提权令牌，owner 换成 Administrators 还进不进得去 ─────────────


def _access_as_me(path: Path, owner: str | None) -> bool:  # pragma: no cover - 只在 Windows 上跑
    import ctypes
    import re
    from ctypes import wintypes as w

    adv = ctypes.WinDLL("advapi32", use_last_error=True)
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.GetCurrentProcess.restype = w.HANDLE
    adv.OpenProcessToken.argtypes = [w.HANDLE, w.DWORD, ctypes.POINTER(w.HANDLE)]
    adv.DuplicateToken.argtypes = [w.HANDLE, ctypes.c_int, ctypes.POINTER(w.HANDLE)]
    adv.GetNamedSecurityInfoW.argtypes = [w.LPCWSTR, ctypes.c_int, w.DWORD, ctypes.c_void_p, ctypes.c_void_p,
                                          ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    adv.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [
        ctypes.c_void_p, w.DWORD, w.DWORD, ctypes.POINTER(ctypes.c_wchar_p), ctypes.c_void_p]
    adv.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        w.LPCWSTR, w.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
    adv.AccessCheck.argtypes = [ctypes.c_void_p, w.HANDLE, w.DWORD, ctypes.c_void_p, ctypes.c_void_p,
                                ctypes.POINTER(w.DWORD), ctypes.POINTER(w.DWORD), ctypes.POINTER(w.BOOL)]
    sd = ctypes.c_void_p()
    assert adv.GetNamedSecurityInfoW(str(path), 1, 1 | 2 | 4, None, None, None, None, ctypes.byref(sd)) == 0
    text = ctypes.c_wchar_p()
    assert adv.ConvertSecurityDescriptorToStringSecurityDescriptorW(sd, 1, 1 | 2 | 4, ctypes.byref(text), None)
    sddl = text.value if owner is None else re.sub(r"^O:[^:]+?(?=G:)", f"O:{owner}", text.value)
    built = ctypes.c_void_p()
    assert adv.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(built), None)
    token, imp = w.HANDLE(), w.HANDLE()
    assert adv.OpenProcessToken(k32.GetCurrentProcess(), 0x0002 | 0x0008, ctypes.byref(token))
    assert adv.DuplicateToken(token, 2, ctypes.byref(imp))

    class Mapping(ctypes.Structure):
        _fields_ = [("r", w.DWORD), ("w", w.DWORD), ("x", w.DWORD), ("a", w.DWORD)]

    mapping = Mapping(0x120089, 0x120116, 0x1200A0, 0x1F01FF)
    privileges = (ctypes.c_byte * 256)()
    size, granted, status = w.DWORD(256), w.DWORD(), w.BOOL()
    assert adv.AccessCheck(built, imp, 0x120089 | 0x00020000, ctypes.byref(mapping), privileges,
                           ctypes.byref(size), ctypes.byref(granted), ctypes.byref(status))
    return bool(status.value)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows DACL")
def test_a_private_dir_built_while_elevated_still_opens_for_me(tmp_path) -> None:
    base = Path(os.path.expandvars(r"%LOCALAPPDATA%")) / f"hf-private-dir-{os.getpid()}"
    base.mkdir()
    try:
        ours = base / "home"
        mkdir_private(ours)
        raw = base / "raw"
        os.mkdir(raw, 0o700)
        if not _access_as_me(raw, owner="BA"):
            assert _access_as_me(raw, owner=None), "对照：本人建的 0o700 本人进得去"
        else:
            pytest.skip("这台机器上 mkdir(0o700) 不是只认 owner 的 DACL（Python 早于 3.12.4？）")
        assert _access_as_me(ours, owner=None)
        assert _access_as_me(ours, owner="BA"), (
            "以管理员身份建的（owner = Administrators）本人未提权进不去 —— 就是 home_marker_unreadable")
    finally:
        import shutil

        shutil.rmtree(base, ignore_errors=True)

"""`shared.lib.shell`：OS 启动的第一个 shell 可执行文件，一处回答。"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from shared.lib import shell


def test_posix_returns_the_literals_unchanged():
    if sys.platform == "win32":
        pytest.skip("POSIX 断言")
    assert shell.posix_shell() == "/bin/sh"
    assert shell.bash_shell() == "/bin/bash"


def test_windows_uses_msys_bash(monkeypatch):
    # 不在 Windows 上也要能验解析逻辑：强制走 Windows 分支 + HARNESS_BASH 覆盖。
    monkeypatch.setattr(shell, "_WINDOWS", True)
    monkeypatch.setenv("HARNESS_BASH", r"C:\Program Files\Git\usr\bin\bash.exe")
    assert shell.bash_shell() == r"C:\Program Files\Git\usr\bin\bash.exe"
    # posix 包裹在 Windows 上是同一个 MSYS bash（bash 是 sh 的超集）。
    assert shell.posix_shell() == r"C:\Program Files\Git\usr\bin\bash.exe"


def test_windows_prefers_the_bundled_git_bash(monkeypatch, tmp_path):
    monkeypatch.setattr(shell, "_WINDOWS", True)
    monkeypatch.delenv("HARNESS_BASH", raising=False)
    # 造一个"随包布局"：<sys.prefix>/../git/usr/bin/bash.exe
    prefix = tmp_path / "python"
    prefix.mkdir()
    bash = tmp_path / "git" / "usr" / "bin" / "bash.exe"
    bash.parent.mkdir(parents=True)
    bash.write_text("", encoding="utf-8")
    monkeypatch.setattr(shell.sys, "prefix", str(prefix))
    assert shell.bash_shell() == str(bash)


def test_windows_derives_bash_from_git_on_path(monkeypatch, tmp_path):
    """从 PATH 上的 git.exe 反推它自带的 MSYS bash（<git>/cmd/git.exe → <git>/usr/bin/bash.exe）。"""
    monkeypatch.setattr(shell, "_WINDOWS", True)
    monkeypatch.delenv("HARNESS_BASH", raising=False)
    # 随包布局的探测点 = <sys.prefix>/../git —— 让它落在一个**没有** git 的目录下，
    # 免得和下面的 Git 安装在（大小写不敏感的）文件系统上撞车。
    monkeypatch.setattr(shell.sys, "prefix", str(tmp_path / "runtime" / "python"))
    git_root = tmp_path / "install" / "Git"
    (git_root / "cmd").mkdir(parents=True)
    git_exe = git_root / "cmd" / "git.exe"
    git_exe.write_text("", encoding="utf-8")
    bash = git_root / "usr" / "bin" / "bash.exe"
    bash.parent.mkdir(parents=True)
    bash.write_text("", encoding="utf-8")
    monkeypatch.setattr(shell.shutil, "which", lambda name: str(git_exe) if name == "git" else None)
    # macOS 的 tmp 是 /var→/private/var 软链，_windows_bash 内部 resolve 了 git 路径 → 比较也 resolve
    assert Path(shell.bash_shell()).resolve() == bash.resolve()


def test_windows_never_falls_back_to_which_bash(monkeypatch, tmp_path):
    """装了 WSL 的机器上 which('bash') 是 WSL 启动器 —— 绝不能回落到它（会把模型丢进 WSL2）。
    找不到 Git bash 时必须显式失败，不能让 Windows PATH 选择 WSL。"""
    monkeypatch.setattr(shell, "_WINDOWS", True)
    monkeypatch.delenv("HARNESS_BASH", raising=False)
    monkeypatch.setattr(shell.sys, "prefix", str(tmp_path / "python"))
    monkeypatch.delenv("PROGRAMFILES", raising=False)
    monkeypatch.delenv("PROGRAMFILES(X86)", raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)

    def _which(name):
        if name == "git":
            return None
        if name == "bash":
            return r"C:\Users\x\AppData\Local\Microsoft\WindowsApps\bash.exe"  # WSL 存根
        return None

    monkeypatch.setattr(shell.shutil, "which", _which)
    with pytest.raises(FileNotFoundError, match="Git for Windows Bash"):
        shell.bash_shell()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX sandbox 行为")
def test_the_throat_launches_through_the_seam(tmp_path):
    """接缝真的接上了：一条 shell 命令经沙箱路径（writable_roots）跑起来、把文件写出。
    posix_shell() 若返回坏路径，这条命令根本起不来 —— 文件不会出现。"""
    import subprocess

    from shared.lib.cancellable_subprocess import spawn_and_wait

    worktree = tmp_path
    mine = worktree / "mine"
    mine.mkdir()
    subprocess.run(["git", "init", "-q", str(worktree)], check=True)

    class _State:
        kill_event = None
        events: list = []

        def record_event(self, *a, **k):
            pass

    target = mine / "seam.txt"
    status, rc, _out, err = asyncio.run(spawn_and_wait(
        f"echo seam-ok > '{target}'", state=_State(), timeout=30, shell=True,
        cwd=str(mine), writable_roots=[mine], readonly_roots=[worktree],
    ))
    assert (status, rc) == ("done", 0), err
    assert target.read_text() == "seam-ok\n"


@pytest.mark.skipif(sys.platform != "win32", reason="MSYS command lookup on Windows")
def test_extensionless_script_lookup_matches_the_real_model_shell(tmp_path, monkeypatch):
    import os
    import subprocess
    script = tmp_path / "hf-lookup-probe"
    script.write_text("#!/bin/sh\nprintf 'actual-script'\n", encoding="utf-8", newline="\n")
    bash = shell.bash_shell()
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    resolved = shell.resolve_bash_executable(script.name)
    assert resolved is not None and Path(resolved).samefile(script)
    completed = subprocess.run([bash, "-c", script.name],
                               capture_output=True, text=True, timeout=10)
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout == "actual-script"

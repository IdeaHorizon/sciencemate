"""git 子进程的输出按 UTF-8 解码、消息钉在 C locale —— 不随这台机器的代码页变。

2026-10-29，lujy 的会话诊断包（中文 Windows 11，ScienceMate 0.5.8）：git 把本地化消息按
控制台代码页 CP936 写出来，`subprocess.run(text=True)` 按本机 locale 解码，Windows 上读管道
的线程在 0xb0 上 UnicodeDecodeError 死掉、那条流还成 **None**，`_git` 在 `.strip()` 上
AttributeError —— GET …/sessions/<id>/changes 连续 500 了 36 次
（session_changes._read_session_patch → project_repository.diff → _git）。

这里不依赖这台机器的 locale：**假 git 直接吐 CP936 字节**。修之前这条在 UTF-8 locale 的
Mac/Linux 上同样红（POSIX 的 communicate 在主线程解码，直接抛 UnicodeDecodeError）。
同一条教训打包器那边踩过一次（`scripts/package/git_tracked.py`）：git 写的路径永远是 UTF-8
字节，和 locale 无关 —— 会随 locale 变的只有消息，所以消息钉成 C，解码钉成 UTF-8。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from app.services import project_repository
from app.services.project_repository import GitProjectRepository, ProjectRepositoryError

#: 「安」的 CP936 编码。作为 UTF-8 它是非法序列（0xb0 不是任何合法起始字节的后继）。
CP936_BYTES = b"\xb0\xb2"


def _repository_whose_git(tmp_path: Path, body: str) -> GitProjectRepository:
    """一个 `GitProjectRepository`，它的 git 是一段 Python：拿到 argv，往两条流写**字节**。"""
    script = tmp_path / "fake_git.py"
    script.write_text("import os, sys\n" + body + "\n", encoding="utf-8")
    if sys.platform == "win32":
        launcher = tmp_path / "git.cmd"
        launcher.write_text(f'@"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
    else:
        launcher = tmp_path / "git"
        launcher.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n',
                            encoding="utf-8")
        launcher.chmod(0o755)
    repository = GitProjectRepository(tmp_path / "repositories", tmp_path / "worktrees")
    repository.git = str(launcher)
    return repository


def test_git_output_that_is_not_utf8_comes_back_as_a_string(tmp_path: Path) -> None:
    """stdout 里夹着 CP936 字节：`_git` 还是返回 str，坏字节换成 U+FFFD，不抛、不 None。"""
    repository = _repository_whose_git(
        tmp_path,
        "sys.stdout.buffer.write(b'M\\tpaper.tex\\n' + " + repr(CP936_BYTES) + " + b'\\n')",
    )
    out = repository._git(tmp_path, "diff", "--name-status")
    assert isinstance(out, str)
    assert out == "M\tpaper.tex\n��"


def test_the_exit_code_channel_decodes_the_same_way(tmp_path: Path) -> None:
    """`_git_exit_code` 走同一个出口 —— 退出码照给，stdout 是 str。"""
    repository = _repository_whose_git(
        tmp_path, "sys.stdout.buffer.write(" + repr(CP936_BYTES) + "); sys.exit(1)"
    )
    code, out = repository._git_exit_code(tmp_path, "merge-base", "--is-ancestor", "a", "b")
    assert (code, out) == (1, "��")


def test_a_localized_failure_message_still_becomes_a_readable_error(tmp_path: Path) -> None:
    """stderr 里的 CP936 消息：报的是 ProjectRepositoryError（带那段话），不是解码炸在半路。"""
    repository = _repository_whose_git(
        tmp_path,
        "sys.stderr.buffer.write(b'fatal: ' + " + repr(CP936_BYTES) + " + b'\\n'); sys.exit(128)",
    )
    with pytest.raises(ProjectRepositoryError, match=r"failed \(128\): fatal: ��"):
        repository._git(tmp_path, "diff")


def test_git_is_told_to_speak_c_locale_and_the_callers_env_still_arrives(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """进程的 locale 是中文也好，git 看到的是 LC_ALL=LANG=C；调用方按次传的 env 不丢。"""
    monkeypatch.setenv("LC_ALL", "zh_CN.GB2312")
    monkeypatch.setenv("LANG", "zh_CN.GB2312")
    repository = _repository_whose_git(
        tmp_path,
        "print(os.environ.get('LC_ALL'), os.environ.get('LANG'), "
        "os.environ.get('GIT_AUTHOR_NAME'), os.environ.get('PATH') is not None)",
    )
    out = repository._git(tmp_path, "commit", env={"GIT_AUTHOR_NAME": "platform"})
    assert out == "C C platform True"


def test_a_stream_the_reader_thread_lost_reads_as_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CPython（Windows）读管道线程死掉时把那条流还成 None —— `_git` 当它是空串，两条路都是。"""
    repository = GitProjectRepository(tmp_path / "repositories", tmp_path / "worktrees")
    outcomes = iter([0, 128])

    def lost(argv, **kwargs):  # noqa: ANN001 — subprocess.run 的签名
        return subprocess.CompletedProcess(argv, next(outcomes), None, None)

    monkeypatch.setattr(project_repository.subprocess, "run", lost)
    assert repository._git(tmp_path, "rev-parse", "HEAD") == ""
    with pytest.raises(ProjectRepositoryError, match=r"git rev-parse HEAD failed \(128\): $"):
        repository._git(tmp_path, "rev-parse", "HEAD")

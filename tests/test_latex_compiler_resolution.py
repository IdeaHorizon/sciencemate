"""compile_latex 的编译器选择：latexmk 优先，缺则 tectonic（Windows 无 LaTeX 的架构答案）。

真机缘起（09-08 非管理员 Windows）：Windows 上没有任何 LaTeX（pdflatex/xelatex/latexmk
全无），产品跑到 writing 出 PDF 那步就没工具可用。tectonic 单文件、非管理员、自带按需取包 +
多轮 + BibTeX、bundle 版本锁定。

它往哪儿写缓存不归这里管：tectonic 自成一体，住墙给的家（``CommandSpec.home="own"``，
``core.isolation._native.payload_home``），按本平台约定写进那个家的缓存区（链到持久缓存）。
所以 tectonic 这一支**不带任何专用环境变量** —— 曾经的 ``TECTONIC_CACHE_DIR`` 只盖住包缓存、
没盖住 formats，干净的 Windows 上每编一次 os error 5。latexmk 是用户的 TeX Live，住用户的家。

这套测试钉编译器选择与命令拼装（跨平台，mock shutil.which / _run_process）；缓存跨命令
持久由 tests/test_the_wall_gives_a_home.py 真起进程钉。
"""

from __future__ import annotations

import pytest

from shared.tools.library import latex


# ── 选择 ─────────────────────────────────────────────────────────────────────

def test_resolve_prefers_latexmk(monkeypatch):
    monkeypatch.setattr(latex.shutil, "which",
                        lambda n: "/usr/bin/latexmk" if n == "latexmk" else None)
    assert latex._resolve_compiler() == ("latexmk", "/usr/bin/latexmk")


def test_resolve_falls_back_to_tectonic_when_no_latexmk(monkeypatch):
    monkeypatch.setattr(latex.shutil, "which",
                        lambda n: r"C:\afs\tectonic.exe" if n == "tectonic" else None)
    assert latex._resolve_compiler() == ("tectonic", r"C:\afs\tectonic.exe")


def test_resolve_defaults_to_latexmk_when_neither_on_path(monkeypatch):
    # 两者都不在 PATH：照常拼 latexmk 命令交给 spawn，**不在这里下结论**。
    # 控制面的 PATH 只是对沙箱环境的猜测；"这台机器到底有没有 LaTeX"由真正起
    # 进程的那一层回答（spawn 的 status=spawn_failed → _launch_failure 现算）。
    monkeypatch.setattr(latex.shutil, "which", lambda n: None)
    assert latex._resolve_compiler() == ("latexmk", "latexmk")


# ── 命令拼装 ─────────────────────────────────────────────────────────────────

def _capture_run_process(monkeypatch):
    captured: dict = {}

    async def fake(state, command, cwd, timeout, *, home="host", network=False):
        captured["command"] = command
        captured["cwd"] = cwd
        captured["home"] = home
        captured["network"] = network
        return "done", 0, b"", b""

    monkeypatch.setattr(latex, "_run_process", fake)
    return captured


@pytest.mark.asyncio
async def test_latexmk_command_unchanged_when_present(monkeypatch, tmp_path):
    monkeypatch.setattr(latex.shutil, "which",
                        lambda n: "/usr/bin/latexmk" if n == "latexmk" else None)
    cap = _capture_run_process(monkeypatch)
    run = await latex.run_tex(
        None, tmp_path, "paper.tex", "xelatex", 60)
    assert run.returncode == 0 and run.compiler == "latexmk"
    assert cap["command"] == [
        "/usr/bin/latexmk", "-xelatex", "-interaction=nonstopmode",
        "-halt-on-error", "-file-line-error", "paper.tex",
    ]
    assert cap["home"] == "host", "系统 TeX Live 是用户的程序：要看见用户的家（TEXMFHOME 里的个人宏包）"
    assert cap["network"] is False, "稿子是模型写的：编译默认断网"


@pytest.mark.asyncio
async def test_tectonic_command_keeps_logs_and_writes_into_compile_dir(monkeypatch, tmp_path):
    # 只有 tectonic：命令要 --keep-logs（版式审计读 .log）+ --outdir compile_dir。
    monkeypatch.setattr(latex.shutil, "which",
                        lambda n: "tectonic" if n == "tectonic" else None)
    cap = _capture_run_process(monkeypatch)
    compile_dir = tmp_path / "cd"
    compile_dir.mkdir()
    run = await latex.run_tex(
        None, compile_dir, "paper.tex", "xelatex", 60)
    assert run.returncode == 0 and run.compiler == "tectonic"
    assert cap["command"] == [
        "tectonic", "--keep-logs", "--outdir", str(compile_dir), "paper.tex",
    ]
    assert not (compile_dir / ".tectonic-cache").exists()
    assert cap["home"] == "own", (
        "tectonic 自成一体：住墙给的家。照用用户的家，干净 Windows 上它的 formats 目录建不出来")
    assert cap["network"] is False, "稿子是模型写的：编译默认断网（宏包由框架自己的样本取）"


@pytest.mark.asyncio
async def test_neither_on_path_still_builds_latexmk_command(monkeypatch, tmp_path):
    # 两者都不在 PATH：仍拼 latexmk 命令并走 _run_process。真机上由 spawn 报
    # not-found，归属在 _launch_failure 里定 —— 判决属于知道真相的那一层。
    monkeypatch.setattr(latex.shutil, "which", lambda n: None)
    cap = _capture_run_process(monkeypatch)
    await latex.run_tex(None, tmp_path, "paper.tex", "pdflatex", 60)
    assert cap["command"][0] == "latexmk"

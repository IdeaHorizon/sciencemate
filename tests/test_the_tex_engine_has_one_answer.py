"""「这台机器用哪个 TeX、怎么跑它」只有一个答案：``shared.tools.library.latex``。

缘起（2026-09-23）：稿件走 ``latex.run_tex``（latexmk，缺则随包 tectonic；tectonic 住墙给的
家），出图的 tikz 后端却自己 ``which("xelatex") or which("lualatex")``、在墙外起它，量字宽的
那一道又自己 ``which("xelatex")``、在咽喉之外 ``subprocess.run``。于是只有随包 tectonic 的
Windows 桌面上，**默认**的示意图后端一张图都出不来（"no xelatex/lualatex on the minting
host"），而 PDF 实测说这台机器「能出 PDF」—— 同一个问题两个答案，谁也不报错。

这里钉三件事：

1. 只有 tectonic 的机器上，示意图与量字宽都由 tectonic 来做，而且**进墙、住它自己的家**
   （``home="own"``；可写的只有那一次的工作目录）。两边都有时两边都是 latexmk、用户的家。
2. 一个 TeX 都没有时，出图如实说「没有 TeX」（``toolchain_missing``、点名 matplotlib），
   量字宽如实退回估算 —— 不是「没有 xelatex」这句只对一半机器成立的话。
3. 运行时代码里，除了 ``latex.py`` 没有别处自己按名字找 TeX 引擎（扫 AST，不列名单）。
"""

from __future__ import annotations

import ast
import asyncio
import json
import os
import stat
import sys
from pathlib import Path

import pytest

from shared.lib import cancellable_subprocess
from shared.tools.library import latex

REPO = Path(__file__).resolve().parents[1]
TEX_ENGINES = frozenset({"latexmk", "tectonic", "xelatex", "lualatex", "pdflatex"})
ANSWER = "shared/tools/library/latex.py"

#: 一页最小的 PDF —— 替身编译器交回的「编出来的图」。
TINY_PDF = (
    b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 20 20]>>endobj\n"
    b"trailer<</Root 1 0 R>>\n%%EOF\n"
)


def _only_on_path(monkeypatch, **found: str) -> None:
    """PATH 上只有 ``found`` 里的那几个编译器（名字 → 解析到的路径）。"""
    monkeypatch.setattr(latex.shutil, "which", lambda name, *a, **k: found.get(name))


def _spy_on_the_throat(monkeypatch, *, write) -> list[tuple[tuple[str, ...], dict]]:
    """把唯一咽喉换成记录员：记下 argv 与墙的参数，由 ``write(cwd)`` 伪造编译产物。"""
    seen: list[tuple[tuple[str, ...], dict]] = []

    async def spawn(*argv, **kwargs):
        seen.append((argv, kwargs))
        write(Path(kwargs["cwd"]))
        return "done", 0, b"", b""

    monkeypatch.setattr(cancellable_subprocess, "spawn_and_wait", spawn)
    return seen


def _write_tikz_work(base: Path) -> Path:
    work = base / "_contract_tikz"
    work.mkdir(parents=True)
    (work / "figure.tex").write_text("\\documentclass{standalone}\\begin{document}x\\end{document}\n")
    (work / "manifest.json").write_text(json.dumps({"outputs": ["fig.pdf"], "dpi": 100}))
    return work


def _render(base: Path) -> dict:
    from nodes.postprocess.tools.figure import _finish_tikz_render

    return asyncio.run(_finish_tikz_render(None, base, timeout=60))


def _measure() -> tuple[dict | None, str]:
    from nodes.postprocess import text_metrics
    from nodes.postprocess.diagram_compiler import _tex_escape

    text_metrics._MEMO.clear()
    return text_metrics.measure_with_tex(
        [("交换机 A", 9.4), ("RTX PRO 6000", 8.0)], r"\usepackage{fontspec}", escape=_tex_escape)


def _answer_every_width(cwd: Path) -> None:
    """替身排版器：给测量文档里每个 AFSMW 槽位回一个宽度（写进 --keep-logs 的那份 .log）。"""
    slots = [line.split("AFSMW ", 1)[1].split(" ", 1)[0]
             for line in (cwd / "measure.tex").read_text(encoding="utf-8").splitlines()
             if "\\typeout{AFSMW " in line]
    (cwd / "measure.log").write_text("".join(f"AFSMW {s} 12.5pt\n" for s in slots))


# ── 1. 只有 tectonic：两条路都用它，进墙、住自己的家 ───────────────────────────────

def test_with_only_tectonic_a_tikz_figure_is_compiled_by_tectonic_in_its_own_home(
        monkeypatch, tmp_path):
    _only_on_path(monkeypatch, tectonic="/opt/afs/tectonic")
    work = _write_tikz_work(tmp_path)
    seen = _spy_on_the_throat(monkeypatch, write=lambda cwd: (cwd / "figure.pdf").write_bytes(TINY_PDF))

    finish = _render(tmp_path)

    assert finish["status"] == "success", finish
    assert finish["compiler"] == "tectonic"
    assert (tmp_path / "fig.pdf").read_bytes() == TINY_PDF
    (argv, wall), = seen
    assert argv[0] == "/opt/afs/tectonic", "只有 tectonic 的机器上，示意图就该由它来编"
    assert wall["sandbox_home"] == "own", "tectonic 自成一体：在哪儿跑都住墙给的家"
    assert [Path(p) for p in wall["writable_roots"]] == [work], (
        "图的编译进墙：可写的只有这一次的 _contract_tikz/")


def test_with_only_tectonic_the_text_is_measured_by_tectonic_in_its_own_home(monkeypatch):
    _only_on_path(monkeypatch, tectonic="/opt/afs/tectonic")
    seen = _spy_on_the_throat(monkeypatch, write=_answer_every_width)

    table, why = _measure()

    assert table == {("RTX PRO 6000", 8.0): 12.5, ("交换机 A", 9.4): 12.5}, why
    assert why == "measured by tectonic", "量字宽的排版器必须是出图的那个排版器"
    (argv, wall), = seen
    assert argv[0] == "/opt/afs/tectonic"
    assert wall["sandbox_home"] == "own"
    assert len(wall["writable_roots"]) == 1 and Path(wall["writable_roots"][0]) == Path(wall["cwd"])


def test_with_both_the_figure_and_the_manuscript_use_latexmk_in_the_users_home(
        monkeypatch, tmp_path):
    _only_on_path(monkeypatch, latexmk="/usr/bin/latexmk", tectonic="/opt/afs/tectonic")
    _write_tikz_work(tmp_path)
    seen = _spy_on_the_throat(monkeypatch, write=lambda cwd: (cwd / "figure.pdf").write_bytes(TINY_PDF))

    assert _render(tmp_path)["compiler"] == "latexmk"
    manuscript = tmp_path / "paper"
    manuscript.mkdir()
    asyncio.run(latex.run_tex(None, manuscript, "paper.tex", "xelatex", 60))

    (figure_argv, figure_wall), (paper_argv, paper_wall) = seen
    assert figure_argv[:2] == paper_argv[:2] == ("/usr/bin/latexmk", "-xelatex")
    assert figure_wall["sandbox_home"] == paper_wall["sandbox_home"] == "host"


# ── 2. 一个 TeX 都没有：说「没有 TeX」，不说「没有 xelatex」 ──────────────────────

def test_with_no_tex_the_figure_says_the_toolchain_is_missing_and_names_matplotlib(
        monkeypatch, tmp_path):
    _only_on_path(monkeypatch)
    _write_tikz_work(tmp_path)

    async def spawn(*argv, **kwargs):
        return "spawn_failed", None, b"", b"No such file or directory: 'latexmk'"

    monkeypatch.setattr(cancellable_subprocess, "spawn_and_wait", spawn)
    finish = _render(tmp_path)

    assert finish["status"] == "error" and finish["error_code"] == "toolchain_missing", finish
    assert "latexmk" in finish["error"] and "tectonic" in finish["error"]
    assert "backend='matplotlib'" in finish["error"]


def test_with_no_tex_the_widths_are_estimated_and_the_record_says_why(monkeypatch):
    _only_on_path(monkeypatch)
    seen = _spy_on_the_throat(monkeypatch, write=_answer_every_width)

    table, why = _measure()

    assert table is None and not seen, "没有 TeX 就别起进程"
    assert why.startswith("no TeX engine on this host") and "estimated" in why


# ── 3. 除了 latex.py，没有别处自己按名字找 TeX ───────────────────────────────────

SCAN_ROOTS = ("core", "shared", "nodes", "platform/backend/app", "platform_runtime.py",
              "run_node.py", "chat.py")
EXCLUDED_PARTS = frozenset({"tests", "fixtures", "project_templates", "__pycache__", ".venv",
                            "node_modules", "docs", "skills", "scripts"})


def _runtime_files() -> list[Path]:
    files: list[Path] = []
    for root in SCAN_ROOTS:
        base = REPO / root
        if base.is_file():
            files.append(base)
        elif base.is_dir():
            files += [p for p in sorted(base.rglob("*.py"))
                      if not EXCLUDED_PARTS & set(p.relative_to(REPO).parts)
                      and not p.name.startswith("test_")]
    return files


def _asks_for_a_tex_engine(call: ast.Call) -> str | None:
    func = call.func
    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
    if name != "which" or not call.args:
        return None
    arg = call.args[0]
    if isinstance(arg, ast.Constant) and arg.value in TEX_ENGINES:
        return arg.value
    return None


def test_only_the_latex_tool_decides_which_tex_engine_this_machine_has():
    offenders = []
    for path in _runtime_files():
        rel = path.relative_to(REPO).as_posix()
        if rel == ANSWER:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call) and (engine := _asks_for_a_tex_engine(node)):
                offenders.append(f"{rel}:{node.lineno} which({engine!r})")
    assert not offenders, (
        "这些地方自己按名字找 TeX 引擎 —— 那是「这台机器用哪个 TeX」的第二个答案。"
        "问 shared.tools.library.latex（_resolve_compiler / no_tex_engine / run_tex）：\n"
        + "\n".join(offenders))


def test_the_scan_sees_the_old_way_of_asking():
    """变异对照：上面那条扫描认得出被删掉的那种写法。"""
    tree = ast.parse('engine = shutil.which("xelatex") or shutil.which("lualatex")')
    found = [_asks_for_a_tex_engine(n) for n in ast.walk(tree) if isinstance(n, ast.Call)]
    assert sorted(filter(None, found)) == ["lualatex", "xelatex"]


# ── 真过墙：替身 tectonic 看见的家不是用户的家 ─────────────────────────────────────

def _wall_ready() -> bool:
    from core import isolation

    name = isolation.native_backend_name()
    if name is None:
        return False
    try:
        backend = isolation.select_backend(name)
    except isolation.IsolationContractError:
        return False
    return isolation.Invariant.WRITE_BOUNDARY in backend.capabilities()


@pytest.mark.skipif(sys.platform == "win32",
                    reason="替身编译器是 sh 脚本；Windows 上由真 tectonic 验（见 PR 说明）")
def test_a_tikz_figure_really_crosses_the_wall_into_tectonics_own_home(monkeypatch, tmp_path):
    from core import isolation

    name = isolation.native_backend_name()
    if name is not None:
        monkeypatch.setenv(isolation.EXECUTOR_ENV, name)
    isolation._reset_for_tests()
    try:
        if not _wall_ready():
            pytest.skip("本机的原生后端守不住写边界")
        bindir = tmp_path / "bin"
        bindir.mkdir()
        fake = bindir / "tectonic"
        # argv: --keep-logs --outdir <dir> figure.tex
        fake.write_text(
            "#!/bin/sh\n"
            'printf "%s" "$HOME" > "$3/seen_home"\n'
            'printf "%%PDF-1.4 fake" > "$3/figure.pdf"\n')
        fake.chmod(fake.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        monkeypatch.setenv("PATH", os.pathsep.join([str(bindir), "/usr/bin", "/bin"]))
        if latex.shutil.which("latexmk"):
            pytest.skip("这台机器的 /usr/bin 里有 latexmk —— 轮不到 tectonic")
        workspace = tmp_path / "ws"
        work = _write_tikz_work(workspace)

        finish = _render(workspace)

        assert finish["status"] == "success" and finish["compiler"] == "tectonic", finish
        seen_home = (work / "seen_home").read_text()
        assert seen_home and Path(seen_home) != Path.home(), (
            f"tectonic 在墙里看见的是用户的家 {seen_home!r}：干净机器上它的 formats 建不出来")
    finally:
        isolation._reset_for_tests()

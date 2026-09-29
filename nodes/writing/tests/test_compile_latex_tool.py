from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from core import paths
from core.sandbox import availability
from core.project_workspace import working_directory
from core.state import State
from core.tool_registry import execute
from shared.lib.latex_layout import audit_latex_log
from shared.tools.library.latex import PROVENANCE_NAME, _compile_latex

RENDERER_TEMPLATE = Path(__file__).parents[1] / "renderers" / "zh_article" / "main.tex.tmpl"
_SANDBOX_AVAILABLE = availability()[0]

MANUSCRIPT_BODY = "本节的正文只应该存在于源工程里，不该出现在工作区观测事件中。"


def _write_fake_executable(path: Path, body: str) -> None:
    path.write_text("#!/bin/sh\nset -eu\n" + body, encoding="utf-8")
    path.chmod(0o755)


def _fake_tex_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    fail: bool = False,
    invalid_pdf: bool = False,
) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for engine in ("pdflatex", "xelatex", "lualatex"):
        _write_fake_executable(bin_dir / engine, "exit 0\n")
    if fail:
        latexmk_body = "exit 2\n"
    elif invalid_pdf:
        latexmk_body = """
last=""
for arg in "$@"; do
    last="$arg"
done
stem=${last%.tex}
printf 'not a pdf\n' > "${stem}.pdf"
"""
    else:
        latexmk_body = """
last=""
for arg in "$@"; do
    last="$arg"
done
stem=${last%.tex}
printf '%%PDF-1.4\n%% harness test\n' > "${stem}.pdf"
"""
    _write_fake_executable(bin_dir / "latexmk", latexmk_body)

    async def _fake_run_process(_state: State, command: list[str], cwd: Path, timeout: float,
                                *, home: str = "host", network: bool = False):
        executable = bin_dir / Path(command[0]).name
        completed = subprocess.run(
            [str(executable), *command[1:]],
            cwd=cwd,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        return "done", completed.returncode, completed.stdout, completed.stderr

    monkeypatch.setattr("shared.tools.library.latex._run_process", _fake_run_process)
    return bin_dir


def test_layout_audit_rejects_node20_table_failure_signature() -> None:
    audit = audit_latex_log(
        "Package tabularx Warning: X Columns too narrow (table too wide)\n"
        "Overfull \\hbox (432.80186pt too wide) in alignment at lines 146--146\n"
        "LaTeX Warning: Float too large for page by 84.95325pt on input line 147.\n"
    )

    assert audit["passed"] is False
    assert {finding["category"] for finding in audit["hard_failures"]} == {
        "tabularx_table_too_wide",
        "overfull_alignment",
        "float_too_large_for_page",
    }
    assert audit["max_overflow_pt"] == pytest.approx(432.80186)


def test_layout_audit_keeps_tiny_prose_overflow_non_blocking() -> None:
    audit = audit_latex_log(
        "Overfull \\hbox (1.25pt too wide) in paragraph at lines 20--21\n"
    )

    assert audit["passed"] is True
    assert audit["hard_failures"] == []
    assert audit["warnings"][0]["category"] == "overfull_hbox"


@pytest.mark.asyncio
async def test_compile_latex_promotes_layout_failed_pdf_with_honest_audit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """判决拆除（verdicts_shared latex:514，批 3w 在 writing 侧消费）：版面
    审计失败不再销毁 PDF、不再拒绝——看不见图/稿的科学家连调试都做不了。
    PDF 照常晋升，layout_audit.passed=false 如实随返回值与编译收据走，
    validate_writing_manuscript 读收据时以 warning 披露给 referee。
    """
    bin_dir = _fake_tex_path(tmp_path, monkeypatch)
    _write_fake_executable(
        bin_dir / "latexmk",
        r'''
last=""
for arg in "$@"; do
    last="$arg"
done
stem=${last%.tex}
printf '%%PDF-1.4\n%% harness test\n' > "${stem}.pdf"
printf 'Package tabularx Warning: X Columns too narrow (table too wide)\n' > "${stem}.log"
''',
    )
    state = State.new(node_type="writing", base_dir=tmp_path)

    result = await _compile_latex(
        state=state,
        tex_source="\\documentclass{article}\\begin{document}x\\end{document}",
        output_name="wide_table",
    )

    assert result["status"] == "success"
    assert result["layout_audit"]["passed"] is False
    assert Path(result["pdf_path"]).exists()
    receipts = state.list_artifacts("latex_build_receipt")
    assert receipts, "layout 失败的编译也要有收据——身份与充分性分账"
    receipt = state.read_artifact(receipts[-1]["id"])
    assert receipt is not None
    payload = json.loads(receipt["content"])
    assert payload["build_record"]["layout_audit"]["passed"] is False


@pytest.mark.asyncio
async def test_compile_latex_single_file_returns_fresh_pdf(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_tex_path(tmp_path, monkeypatch)
    state = State.new(node_type="writing", base_dir=tmp_path)

    result = await _compile_latex(
        state=state,
        tex_source="\\documentclass{article}\\begin{document}Hello\\end{document}",
        output_name="paper",
    )

    assert result["status"] == "success"
    assert result["engine"] == "pdflatex"
    assert result["source_mode"] == "single_file"
    assert Path(result["pdf_path"]).read_bytes().startswith(b"%PDF-")
    assert len(result["pdf_sha256"]) == 64
    assert result["latex_build_receipt_id"].startswith("latex_build_receipt__")
    assert Path(result["log_path"]).exists()


@pytest.mark.asyncio
async def test_compile_latex_project_mode_copies_full_source_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_tex_path(tmp_path, monkeypatch)
    state = State.new(node_type="writing", base_dir=tmp_path)
    project = working_directory(state) / "manuscript"
    (project / "sections").mkdir(parents=True)
    (project / "main.tex").write_text(
        "\\documentclass{article}\\begin{document}\\input{sections/results}\\end{document}",
        encoding="utf-8",
    )
    (project / "sections" / "results.tex").write_text("Results.", encoding="utf-8")

    result = await _compile_latex(
        state=state,
        source_dir="manuscript",
        main_tex="main.tex",
        output_name="submission",
    )

    assert result["status"] == "success"
    assert result["source_mode"] == "project"
    copied_section = Path(result["workdir"]) / "sections" / "results.tex"
    assert copied_section.read_text(encoding="utf-8") == "Results."


@pytest.mark.asyncio
async def test_compile_latex_project_mode_does_not_follow_symlinks_outside_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_tex_path(tmp_path, monkeypatch)
    state = State.new(node_type="writing", base_dir=tmp_path)
    project = working_directory(state) / "manuscript"
    project.mkdir()
    (project / "main.tex").write_text(
        "\\documentclass{article}\\begin{document}Safe project.\\end{document}",
        encoding="utf-8",
    )
    outside = tmp_path / "outside_secret.tex"
    outside.write_text("outside secret should not enter latex_build", encoding="utf-8")
    try:
        (project / "linked_secret.tex").symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"symlinks are not available in this environment: {exc}")

    result = await _compile_latex(
        state=state,
        source_dir="manuscript",
        main_tex="main.tex",
        output_name="submission",
    )

    linked_copy = Path(result["workdir"]) / "linked_secret.tex"
    assert result["status"] == "success"
    assert "linked_secret.tex" in result["skipped_project_paths"]
    assert not linked_copy.exists()
    assert "outside secret should not enter latex_build" not in "\n".join(
        path.read_text(encoding="utf-8", errors="ignore")
        for path in Path(result["workdir"]).rglob("*.tex")
    )


@pytest.mark.asyncio
async def test_compile_latex_auto_selects_xelatex_for_cjk(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_tex_path(tmp_path, monkeypatch)
    state = State.new(node_type="writing", base_dir=tmp_path)

    result = await _compile_latex(
        state=state,
        tex_source="\\documentclass{article}\\begin{document}中文\\end{document}",
    )

    assert result["status"] == "success"
    assert result["engine"] == "xelatex"
    assert "-xelatex" in result["commands"][0]


@pytest.mark.asyncio
async def test_compile_latex_does_not_accept_stale_pdf_after_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_tex_path(tmp_path, monkeypatch, fail=True)
    state = State.new(node_type="writing", base_dir=tmp_path)
    stale_dir = paths.latex_build_dir(state, output_name="paper")
    stale_dir.mkdir(parents=True)
    stale_pdf = stale_dir / "paper.pdf"
    stale_pdf.write_bytes(b"%PDF-1.4\nstale\n")

    result = await _compile_latex(
        state=state,
        tex_source="\\documentclass{article}\\begin{document}Broken\\end{document}",
        output_name="paper",
    )

    assert result["status"] == "error"
    assert result["returncode"] == 2
    assert not stale_pdf.exists()


@pytest.mark.asyncio
async def test_compile_latex_rejects_non_pdf_output_after_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_tex_path(tmp_path, monkeypatch, invalid_pdf=True)
    state = State.new(node_type="writing", base_dir=tmp_path)

    result = await _compile_latex(
        state=state,
        tex_source="\\documentclass{article}\\begin{document}Broken\\end{document}",
        output_name="paper",
    )

    assert result["status"] == "error"
    assert "不是有效 PDF" in result["error"]
    assert Path(result["pdf_path"]).read_bytes() == b"not a pdf\n"


@pytest.mark.asyncio
async def test_compile_latex_rejects_unsafe_paths(tmp_path: Path) -> None:
    state = State.new(node_type="writing", base_dir=tmp_path)

    bad_output = await _compile_latex(
        state=state,
        tex_source="x",
        output_name="../paper",
    )
    outside_project = await _compile_latex(
        state=state,
        source_dir=str(tmp_path),
        main_tex="main.tex",
    )

    assert bad_output["status"] == "error"
    assert outside_project["status"] == "error"


@pytest.mark.asyncio
async def test_compile_latex_rejects_overlapping_source_without_deleting_it(
    tmp_path: Path,
) -> None:
    state = State.new(node_type="writing", base_dir=tmp_path)
    project = paths.latex_build_dir(state, output_name="paper") / "source"
    project.mkdir(parents=True)
    main_tex = project / "main.tex"
    main_tex.write_text("\\documentclass{article}", encoding="utf-8")

    result = await _compile_latex(
        state=state,
        source_dir=str(project),
        output_name="paper",
    )

    # source 在 build 目录**内部** —— 这是真正退化的一种（构建前会被清空），
    # 仍然拒绝，且不许删掉用户的源文件。
    assert result["status"] == "error"
    assert "latex_build" in result["error"]
    assert main_tex.exists()


@pytest.mark.asyncio
async def test_source_dir_may_contain_the_build_dir(tmp_path: Path,
                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    """source **包含** build 目录是合法布局，不许拒绝。

    latex_build 落进节点自己的 Git 目录之后，writing 的 .tex 就和它同级 ——
    "整个 writing 当 source_dir"这个自然写法会让 source 包含 workdir。
    E2E v22 实测：writing 连撞 4 次"不能与 latex_build 重叠"，论文改不动。
    拒绝的本意是防自我递归复制，跳过构建目录同样安全。
    """
    _fake_tex_path(tmp_path, monkeypatch)
    state = State.new(node_type="writing", base_dir=tmp_path)
    root = working_directory(state)          # source 就是节点工作目录本身
    (root / "main.tex").write_text(
        "\\documentclass{article}\\begin{document}x\\end{document}", encoding="utf-8")
    # 先造一个已存在的构建目录，确保它落在 source 里面
    stale = paths.latex_build_dir(state, output_name="paper")
    stale.mkdir(parents=True, exist_ok=True)
    (stale / "leftover.tex").write_text("old", encoding="utf-8")

    result = await _compile_latex(state=state, source_dir=str(root),
                                  main_tex="main.tex", output_name="paper")

    assert result["status"] == "success", result
    workdir = Path(result["workdir"])
    assert not (workdir / "latex_build").exists(), "构建目录不许被拷进自己"


def _real_tex_stack_available(*commands: str) -> bool:
    return all(shutil.which(command) for command in commands)


def _tex_compiler_available() -> bool:
    """真的有 LaTeX 编译器吗 —— 名单问 compile_latex 自己那一份，不另抄一张。

    2026-09-17：这两条 smoke 的 skip 条件里**只有** sandbox 和 pdftoppm，没有它们
    真正需要的编译器。CI 一直没装 poppler，于是它们一直"skip"，看起来无害；
    给 CI 装上 poppler-utils（writing 的交付页判据要 pdftotext）的当天，它们立刻
    以"这台机器上没有 LaTeX 工具链"转红 —— **跳过的理由和真正的前提对不上，
    这种 skip 掩盖的不是它自己，是别人**。
    """
    from shared.tools.library.latex import _COMPILERS

    return any(shutil.which(name) for name in _COMPILERS)


def _assert_pdf_renders(pdf_path: Path, output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    prefix = output_dir / "page"
    subprocess.run(
        ["pdftoppm", "-f", "1", "-singlefile", "-png", str(pdf_path), str(prefix)],
        check=True,
        capture_output=True,
    )
    rendered = prefix.with_suffix(".png")
    assert rendered.exists()
    assert rendered.stat().st_size > 0


@pytest.mark.skipif(
    not _SANDBOX_AVAILABLE
    or not _tex_compiler_available()
    or not _real_tex_stack_available("pdftoppm"),
    reason=(
        "real TeX smoke test requires the sandbox image, a host LaTeX compiler and "
        "the pdftoppm renderer"
    ),
)
@pytest.mark.asyncio
async def test_compile_latex_real_sci_project_with_references(
    tmp_path: Path,
) -> None:
    state = State.new(node_type="writing", base_dir=tmp_path)
    project = working_directory(state) / "sci_project"
    (project / "sections").mkdir(parents=True)
    # 用 writing 渲染器的真模板（ctexart + biblatex/biber）装配一个最小工程：这就是 render_manuscript 交给编译器的形状
    main = (RENDERER_TEMPLATE.read_text(encoding="utf-8")
            .replace("<<TITLE>>", "Harness LaTeX 冒烟测试")
            .replace("<<AUTHORS>>", "Harness Framework")
            .replace("<<SECTIONS>>", "\\input{sections/introduction}\n\\input{sections/methods}")
            .replace("<<APPENDIX>>", ""))
    (project / "main.tex").write_text(main, encoding="utf-8")
    (project / "sections" / "abstract.tex").write_text("一次确定性的编译冒烟测试。\n", encoding="utf-8")
    (project / "sections" / "introduction.tex").write_text(
        "\\section{引言}\\label{sec:intro}本工程引用 \\cite{lamport1994}，并指向第~\\ref{sec:methods}~节。\n",
        encoding="utf-8",
    )
    (project / "sections" / "methods.tex").write_text(
        "\\section{方法}\\label{sec:methods}渲染器用 latexmk 跑 xelatex 与 biber。\n",
        encoding="utf-8",
    )
    (project / "refs.bib").write_text(
        "@book{lamport1994,title={LaTeX: A Document Preparation System},"
        "author={Lamport, Leslie},year={1994},publisher={Addison-Wesley}}\n",
        encoding="utf-8",
    )

    result = await _compile_latex(
        state=state,
        source_dir="sci_project",
        main_tex="main.tex",
        output_name="sci_smoke",
        engine="xelatex",
    )

    assert result["status"] == "success"
    pdf_path = Path(result["pdf_path"])
    assert pdf_path.exists()
    log_text = Path(result["log_path"]).read_text(encoding="utf-8")
    assert "undefined references" not in log_text.lower()
    assert "undefined citations" not in log_text.lower()
    _assert_pdf_renders(pdf_path, state.root / "rendered_sci")


@pytest.mark.skipif(
    not _SANDBOX_AVAILABLE
    or not _tex_compiler_available()
    or not _real_tex_stack_available("pdftoppm"),
    reason=(
        "real CJK smoke test requires the sandbox image, a host LaTeX compiler and "
        "the pdftoppm renderer"
    ),
)
@pytest.mark.asyncio
async def test_compile_latex_real_cjk_document(tmp_path: Path) -> None:
    state = State.new(node_type="writing", base_dir=tmp_path)
    result = await _compile_latex(
        state=state,
        tex_source=(
            "\\documentclass{ctexart}"
            "\\begin{document}Harness 中文 PDF 编译与渲染测试。\\end{document}"
        ),
        output_name="cjk_smoke",
        engine="auto",
    )

    assert result["status"] == "success"
    assert result["engine"] == "xelatex"
    _assert_pdf_renders(Path(result["pdf_path"]), state.root / "rendered_cjk")


# ══════════════════════════════════════════════════════════════════════════
# 绑真 Project worktree —— 编译现场不许进工作区
#
# 上面那批测试全部跑在**未绑定** state 上。未绑定时产物和现场都落在 run 根
# 底下，"有没有污染 Git 工作区"这个问题在那儿根本不存在 —— 所以 2026-08-19
# 那次事故（一次 compile_latex 造出 18 个新文件、64 KB diff 正文，顶爆协议
# 单帧上限，杀死一条跑了 63 分钟的 run）它们一条都看不见，全绿。
#
# 判据必须问在**观测事件**上，而不是问在"目录里有什么"：事件才是那条会被
# 送进协议、入库、进 checkpoint 的东西。
# ══════════════════════════════════════════════════════════════════════════


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _worktree(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.name", "Test Platform")
    _git(root, "config", "user.email", "platform@example.test")
    (root / ".gitignore").write_text(".research/cache/\n", encoding="utf-8")
    for directory in ("experiment", "postprocess", "writing"):
        (root / directory).mkdir()
        (root / directory / "README.md").write_text(f"# {directory}\n", encoding="utf-8")
    _git(root, "add", "--all")
    _git(root, "commit", "-m", "Initialize Project")
    return root


def _bound_state(tmp_path: Path) -> tuple[State, Path]:
    project = _worktree(tmp_path)
    state = State.new(
        "writing",
        tmp_path / "runtime",
        project_id="project-1",
        project_worktree=project,
    )
    return state, project


async def _manuscript(
    state: State, project: Path, *, entries: tuple[str, ...] = ("main.tex",)
) -> Path:
    """一份最小的 structured project：入口 + 分节正文 + 参考文献。

    **每个文件都走 `write_file` 落盘**，不用 `Path.write_text` 抄近路。工作区
    观测是"这一次工具调用改了什么"的口径，绕过工具直接写盘的文件会被记在
    **下一次**工具调用头上 —— 那样本文件里所有"编译事件里有没有源码正文"的
    判据都会因为搭台方式而假红/假绿，与被测代码无关。
    """
    async def _put(relative: str, content: str) -> None:
        result = await execute("write_file", state, path=relative, content=content)
        assert result["status"] == "success", result

    source = project / "paper" / "sci_manuscript"
    await _put("sci_manuscript/body/results.tex", f"\\section{{Results}}{MANUSCRIPT_BODY}\n")
    await _put("sci_manuscript/refs.bib", "@book{a,title={A},author={B},year={2026}}\n")
    for index, entry in enumerate(entries):
        await _put(
            f"sci_manuscript/{entry}",
            "\\documentclass{article}"
            f"\\def\\linenumbers{{{'true' if index == 0 else 'false'}}}"
            "\\begin{document}\\input{body/results}\\end{document}\n",
        )
    return source


def _workspace_events(state: State) -> list[dict]:
    lines = state.transcript_path.read_text(encoding="utf-8").splitlines()
    return [
        event
        for event in (json.loads(line) for line in lines)
        if event.get("event") == "workspace_changed"
    ]


@pytest.mark.asyncio
async def test_compiling_does_not_push_the_manuscript_source_through_the_event_stream(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """一次编译的观测事件里不许出现稿件正文，也不许出现 TeX 中间件。

    这是 2026-08-19 事故的判据本身：那条 68 KB 的 `workspace_changed` 事件
    之所以存在，是因为构建目录（= 一整份源码副本 + .aux/.log/.pdf）就落在
    被观测的节点目录里。
    """
    _fake_tex_path(tmp_path, monkeypatch)
    state, project = _bound_state(tmp_path)
    await _manuscript(state, project)

    result = await execute(
        "compile_latex",
        state,
        source_dir="sci_manuscript",
        main_tex="main.tex",
        output_name="paper_review",
    )
    assert result["status"] == "success", result

    changed = _workspace_events(state)
    assert changed, "编译产出了 PDF，观测事件不该一条都没有"
    latest = changed[-1]
    assert latest["tool_name"] == "compile_latex"
    assert MANUSCRIPT_BODY not in latest["patch"], (
        "稿件正文被编译动作重新灌进观测事件 —— 构建目录又回到工作区里了"
    )
    assert not [p for p in latest["paths"] if p.endswith((".tex", ".bib", ".aux", ".log"))], (
        f"编译中间件进了工作区: {latest['paths']}"
    )
    # 交付面：只有 PDF 和出处，没有别的。
    outdir = Path(result["output_dir"])
    assert sorted(p.name for p in outdir.iterdir()) == [PROVENANCE_NAME, "main.pdf"]


@pytest.mark.asyncio
async def test_build_scratch_lives_outside_the_node_git_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """现场在 run 缓存里，产物在节点 Git 目录里 —— 结构上就是两个地方。

    这一条一旦回归，源码副本会再次落进被观测、会 checkpoint 的目录，
    上面那条 diff 判据也会跟着一起红。这里单独钉一次结构，是为了让回归
    报错指向**原因**而不是症状。
    """
    _fake_tex_path(tmp_path, monkeypatch)
    state, project = _bound_state(tmp_path)
    await _manuscript(state, project)

    result = await _compile_latex(
        state=state,
        source_dir="sci_manuscript",
        main_tex="main.tex",
        output_name="paper_review",
    )

    assert result["status"] == "success", result
    node_dir = (project / "paper").resolve()
    scratch = Path(result["workdir"]).resolve()
    assert not scratch.is_relative_to(node_dir), f"编译现场落在了节点 Git 目录里: {scratch}"
    assert scratch.is_relative_to(Path(state.root).resolve())
    assert (scratch / "body" / "results.tex").is_file(), "现场仍然要有完整源码副本"
    assert Path(result["pdf_path"]).resolve().is_relative_to(node_dir)


@pytest.mark.asyncio
async def test_provenance_records_which_entry_tex_produced_this_pdf(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """review / clean 之分靠 build.json，不靠 PDF 旁边那份源码副本。

    副本被拿掉之后，"这份 PDF 是从哪个入口编的"必须还答得出来 —— 否则
    下游（inspect_sci_run 的行号校验）就只能改成信任文件名。记的是入口
    路径 + 哈希，比读副本更强：副本会漂，哈希对不上就是对不上。
    """
    _fake_tex_path(tmp_path, monkeypatch)
    state, project = _bound_state(tmp_path)
    source = await _manuscript(state, project, entries=("main.tex", "main_clean.tex"))

    for entry, output_name in (("main.tex", "p_review"), ("main_clean.tex", "p_clean")):
        result = await _compile_latex(
            state=state, source_dir="sci_manuscript",
            main_tex=entry, output_name=output_name,
        )
        assert result["status"] == "success", result
        record = json.loads(Path(result["build_provenance_path"]).read_text(encoding="utf-8"))
        assert record["main_tex"] == entry
        import hashlib

        assert record["main_tex_sha256"] == hashlib.sha256(
            (source / entry).read_bytes()
        ).hexdigest()

    review = json.loads(
        (paths.writing_latex_build_dir(state, "p_review") / PROVENANCE_NAME)
        .read_text(encoding="utf-8")
    )
    clean = json.loads(
        (paths.writing_latex_build_dir(state, "p_clean") / PROVENANCE_NAME)
        .read_text(encoding="utf-8")
    )
    assert review["main_tex_sha256"] != clean["main_tex_sha256"]
    # 同一棵源码树的两次编译：树哈希必须一致，区别只在入口。
    assert review["source_tree_sha256"] == clean["source_tree_sha256"]


@pytest.mark.asyncio
async def test_recompiling_does_not_accumulate_previous_outputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """连编两次，产物目录不许越长越大，观测 diff 也不许。"""
    _fake_tex_path(tmp_path, monkeypatch)
    state, project = _bound_state(tmp_path)
    await _manuscript(state, project)

    first = await execute(
        "compile_latex", state, source_dir="sci_manuscript",
        main_tex="main.tex", output_name="paper_review",
    )
    revised = await execute(
        "write_file", state, path="sci_manuscript/body/results.tex",
        content=f"\\section{{Results}}{MANUSCRIPT_BODY} 又改了一版。\n",
    )
    assert revised["status"] == "success", revised
    second = await execute(
        "compile_latex", state, source_dir="sci_manuscript",
        main_tex="main.tex", output_name="paper_review",
    )

    assert first["status"] == "success" and second["status"] == "success"
    outdir = Path(second["output_dir"])
    assert sorted(p.name for p in outdir.iterdir()) == [PROVENANCE_NAME, "main.pdf"]
    # 第二次编译只碰了 PDF/build.json 和新 minted 的 typed build receipt（原生
    # 文件 `paper/latex_build_receipt__<...>.json`，账本在 .research/ledger）；
    # 上一次的源码副本没有留在里面等着被当成"这一次的改动"重报一遍。
    touched = _workspace_events(state)[-1]["paths"]
    assert all(
        p.startswith("paper/latex_build/paper_review/")
        or p.startswith("paper/latex_build_receipt__paper_review_")
        for p in touched
    ), touched
    assert len(touched) <= 2, touched

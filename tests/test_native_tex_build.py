"""A TeX engine is useful without latexmk; its dependencies remain explicit."""

from pathlib import Path

import pytest

from shared.tools.library import latex


@pytest.mark.parametrize("engine", ["xelatex", "pdflatex", "lualatex"])
def test_native_engine_is_recognized_without_a_build_manager(monkeypatch, engine):
    monkeypatch.setattr(
        latex.shutil, "which", lambda name, **_: f"/tex/{name}" if name == engine else None
    )
    assert latex._resolve_compiler(engine) == (engine, f"/tex/{engine}")
    assert not latex.no_tex_engine()


def test_a_missing_selected_engine_keeps_its_own_identity(monkeypatch):
    monkeypatch.setattr(
        latex.shutil, "which", lambda name, **_: "/tex/xelatex" if name == "xelatex" else None
    )
    assert latex._resolve_compiler("lualatex") == ("lualatex", "lualatex")
    assert not latex.no_tex_engine()


@pytest.mark.asyncio
@pytest.mark.parametrize("bibliography", [None, "bibtex", "biber"])
async def test_native_build_manages_passes_and_preserves_the_selected_bibliography(
    monkeypatch, tmp_path, bibliography
):
    programs = {"xelatex", bibliography}
    monkeypatch.setattr(
        latex.shutil, "which", lambda name, **_: f"/tex/{name}" if name in programs else None
    )
    calls = []
    source = tmp_path / "paper.tex"
    source.write_text("original manuscript")

    async def run(state, command, cwd, timeout, **kwargs):
        calls.append((command, kwargs))
        if Path(command[0]).name == "xelatex":
            (cwd / "paper.aux").write_text(
                r"\bibdata{refs}" if bibliography == "bibtex" else "stable refs"
            )
            if bibliography == "biber":
                (cwd / "paper.bcf").write_text("bibliography control")
            (cwd / "paper.pdf").write_bytes(b"%PDF-1.4\n")
        return "done", 0, b"", b""

    monkeypatch.setattr(latex, "_run_process", run)
    result = await latex.run_tex(None, tmp_path, "paper.tex", "xelatex", 60)
    assert result.status == "done" and result.returncode == 0
    names = [Path(command[0]).name for command, _ in calls]
    assert names == ["xelatex", *([bibliography] if bibliography else []), "xelatex", "xelatex"]
    assert source.read_text() == "original manuscript"
    assert all(not options["network"] for _, options in calls)
    assert all(
        "-no-shell-escape" in command for command, _ in calls if Path(command[0]).name == "xelatex"
    )


def test_missing_bibliography_program_is_not_reported_as_no_latex(monkeypatch):
    monkeypatch.setattr(
        latex.shutil, "which", lambda name, **_: "/tex/xelatex" if name == "xelatex" else None
    )
    run = latex.CompileRun(
        "spawn_failed",
        None,
        [["xelatex", "paper.tex"], ["biber", "paper"]],
        b"",
        b"exec_failed:biber:ENOENT",
        "xelatex",
        "/tex/xelatex",
    )
    result = latex._launch_failure(run, run.stderr.decode(), {})
    assert result["missing_program"] == "biber"
    assert result["available_engines"] == ["xelatex"]
    assert "没有 LaTeX 工具链" not in result["error"]

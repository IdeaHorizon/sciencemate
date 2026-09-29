"""「这台机器能不能出 PDF」是实测出来的，编译失败的归属也由实测来定。

缘起（2026-09-23，一台干净的 Windows）：随包 tectonic 每次 5 秒 ``os error 5``，日志里
一句 TeX 报错都没有；随包里也没有模板要的 biber。平台却按「编译器文件在」说能出 PDF，
编译层把失败当成稿子的错（「改那一节再 render_manuscript」），writing 改了一份没毛病的
稿子，调度器又花半小时自己排查平台。

三件事钉在这里：

1. 失败却没有 TeX 报错时，框架拿一份**已知能编过**的样本走同一条路再编一次：它也编不过
   → 这台机器的事（toolchain_missing），编得过 → 稿子的事。
2. 样本要真的代表平台模板的需求：模板用到的文档类、宏包、biblatex 选项，样本里都要有。
3. 宏包从哪来（2026-09-24，Mac 随包 tectonic）：墙里断网，tectonic 一个包都取不到。只有
   框架**自己写死**的样本能联网取包；断网编译失败而 TeX 一句没说（取包撞墙）时，唯一的
   编译路径自己去取、再断网重编 —— 模型的稿子永远断网。
"""

from __future__ import annotations

import ast
import asyncio
import re
import subprocess
from pathlib import Path

import pytest

from core import isolation
from core import tool_errors as _errs
from core.state import State
from shared.lib import pdf_toolchain
from shared.tools.library import latex

RENDERERS = Path(__file__).resolve().parents[1] / "nodes" / "writing" / "renderers"


@pytest.fixture(autouse=True)
def _own_data_root(monkeypatch, tmp_path):
    """实测记录写进 tmp，别碰真数据根。"""
    monkeypatch.setenv("HARNESS_FRAMEWORK_HOME", str(tmp_path / "afs"))


def _with_latexmk(monkeypatch):
    monkeypatch.setattr(latex.shutil, "which",
                        lambda n: "/usr/bin/latexmk" if n == "latexmk" else None)


def _every_compile_fails_like_the_windows_box(monkeypatch):
    """那台 Windows 的原样：编译器起来了，5 秒退出 1，stderr 两行 os error 5，没有 .log。"""
    async def fake(state, command, cwd, timeout, **_):
        return "done", 1, b"", b"error: Access is denied. (os error 5)\ncaused by: Access is denied. (os error 5)\n"

    monkeypatch.setattr(latex, "_run_process", fake)


async def _compile(tmp_path: Path) -> dict:
    state = State.new(node_type="writing", base_dir=tmp_path)
    return await latex._compile_latex(
        state=state, tex_source="\\documentclass{article}\\begin{document}x\\end{document}",
        output_name="paper",
    )


# ── 1. 失败归属 ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_the_windows_box_is_the_machines_fault_not_the_manuscripts(monkeypatch, tmp_path):
    _with_latexmk(monkeypatch)
    _every_compile_fails_like_the_windows_box(monkeypatch)
    result = await _compile(tmp_path / "run")

    assert result["error_code"] == _errs.TOOLCHAIN_MISSING, result
    assert "不是这份稿件的问题" in result["error"]
    assert "os error 5" in result["error"], "实测的原话要带给读的人"
    assert "改源码" not in result["recovery"] and "改那一节" not in result["recovery"]
    assert result["toolchain"]["works"] is False
    # 同一份观测进了记录 —— 下一次拼系统提示时调度器开工前就知道。
    assert pdf_toolchain.read_record()["works"] is False
    assert "实测**不可用**" in pdf_toolchain.describe_for_prompt()


@pytest.mark.asyncio
async def test_no_tex_error_but_the_known_good_sample_compiles_is_the_manuscripts(
        monkeypatch, tmp_path):
    _with_latexmk(monkeypatch)
    _every_compile_fails_like_the_windows_box(monkeypatch)

    async def the_sample_compiles(state=None, *, timeout=900):
        return {"works": True, "status": "works", "reason": ""}

    monkeypatch.setattr(pdf_toolchain, "measure", the_sample_compiles)
    result = await _compile(tmp_path / "run")

    assert result["error"] == "LaTeX 编译失败"
    assert result.get("error_code") is None, "稿子的错由 _wrap_result 记成 rejected"
    assert "改源码" in result["recovery"]


@pytest.mark.asyncio
async def test_a_tex_error_goes_straight_to_the_manuscript_without_remeasuring(
        monkeypatch, tmp_path):
    _with_latexmk(monkeypatch)

    async def fake(state, command, cwd, timeout, **_):
        return "done", 1, b"./main.tex:12: Undefined control sequence.\n", b""

    monkeypatch.setattr(latex, "_run_process", fake)
    calls = []

    async def should_not_run(state=None, *, timeout=900):
        calls.append(1)
        return {"works": False}

    monkeypatch.setattr(pdf_toolchain, "measure", should_not_run)
    result = await _compile(tmp_path / "run")

    assert calls == [], "编译器已经指出了稿子哪里错，不该再去重测机器"
    assert result["latex_errors"] == ["./main.tex:12: Undefined control sequence."]
    assert result.get("error_code") is None


def test_tex_errors_reads_biber_too():
    blg = "INFO - This is Biber 2.17\nERROR - BibTeX subsystem: refs.bib, line 3, syntax error\n"
    assert latex.tex_errors(blg) == ["ERROR - BibTeX subsystem: refs.bib, line 3, syntax error"]


# ── 2. 记录：什么时候算当前答案 ────────────────────────────────────────────────

def test_describe_says_unknown_works_and_broken_differently():
    assert "还没实测" in pdf_toolchain.describe(None)
    works = {"works": True, "identity": {"compiler": "tectonic", "biber": "C:/biber.exe"}}
    assert "实测可用（tectonic + biber）" in pdf_toolchain.describe(works)
    broken = {"works": False, "reason": "error: program not found",
              "identity": {"compiler": "tectonic", "biber": None}}
    line = pdf_toolchain.describe(broken)
    assert "实测**不可用**（tectonic（没有 biber））" in line and "program not found" in line


@pytest.mark.parametrize("stored, same_identity, remeasured", [
    ({"works": True}, True, False),    # 编过、工具没换：用它
    ({"works": True}, False, True),    # 工具换了（升级、装了 biber）：重测
    ({"works": False}, True, True),    # 编不过的每次都重测：也许已经修好了
    (None, True, True),                # 从没测过
])
def test_ensure_measured_reuses_only_a_passing_record_for_the_same_tools(
        monkeypatch, stored, same_identity, remeasured):
    now = {"compiler": "tectonic", "binary": "t", "binary_fingerprint": [1, 2]}
    if stored is not None:
        stored = {**stored, "identity": now if same_identity else {**now, "binary_fingerprint": [9, 9]}}
    monkeypatch.setattr(pdf_toolchain, "read_record", lambda: stored)
    monkeypatch.setattr(pdf_toolchain, "identity", lambda: now)
    calls = []

    async def fake_measure(state=None, *, timeout=900):
        calls.append(1)
        return {"works": True, "identity": now}

    monkeypatch.setattr(pdf_toolchain, "measure", fake_measure)
    asyncio.run(pdf_toolchain.ensure_measured())
    assert bool(calls) is remeasured


# ── 3. 样本 = 平台模板的需求 ─────────────────────────────────────────────────

def _needs(tex: str) -> dict:
    documentclass = re.search(r"\\documentclass(?:\[[^\]]*\])?\{([^}]+)\}", tex)
    packages: set[str] = set()
    options: dict[str, str] = {}
    for opts, names in re.findall(r"\\usepackage(?:\[([^\]]*)\])?\{([^}]+)\}", tex):
        for name in names.split(","):
            packages.add(name.strip())
            if opts:
                options[name.strip()] = opts
    return {"class": documentclass.group(1) if documentclass else None,
            "packages": packages, "biblatex": options.get("biblatex")}


def test_the_sample_needs_everything_the_platforms_templates_need():
    templates = sorted(RENDERERS.glob("*/main.tex.tmpl"))
    assert templates, f"没找到平台模板：{RENDERERS}"
    sample = _needs(pdf_toolchain.PROBE_TEX)
    for template in templates:
        need = _needs(template.read_text(encoding="utf-8"))
        assert need["class"] == sample["class"], f"{template}: 文档类对不上"
        missing = need["packages"] - sample["packages"]
        assert not missing, f"{template} 用到而实测样本没有的宏包：{sorted(missing)}"
        assert need["biblatex"] == sample["biblatex"], (
            f"{template} 的 biblatex 选项（后端 / 样式）和样本不同 —— 实测证明不了它能编")


def test_the_sample_figure_is_a_real_pdf():
    blob = pdf_toolchain._tiny_pdf()
    assert blob.startswith(b"%PDF-") and blob.rstrip().endswith(b"%%EOF")
    xref = int(blob.rsplit(b"startxref\n", 1)[1].split(b"\n", 1)[0])
    assert blob[xref:].startswith(b"xref"), "startxref 指错了位置，xdvipdfmx 会拒收"


# ── 4. 实测走的是模型的稿子走的那一条路 ────────────────────────────────────────

def _native_wall_or_skip(monkeypatch) -> None:
    name = isolation.native_backend_name()
    if name is None:
        pytest.skip("no native backend here")
    monkeypatch.setenv(isolation.EXECUTOR_ENV, name)
    isolation._reset_for_tests()
    if isolation.Invariant.WRITE_BOUNDARY not in isolation.select_backend(name).capabilities():
        isolation._reset_for_tests()
        pytest.skip(f"native backend {name} cannot enforce the write boundary here")


class _State:
    kill_event = None

    def __init__(self) -> None:
        self.events: list[str] = []

    def append_transcript(self, event_type: str, **payload) -> None:
        self.events.append(event_type)


def test_the_measurement_goes_through_the_wall(monkeypatch):
    """开发机上从没露面的那个缺陷，就是因为验证绕开了墙。实测必须过墙。"""
    _native_wall_or_skip(monkeypatch)
    state = _State()
    record = asyncio.run(pdf_toolchain.measure(state, timeout=600))
    assert "isolation_enforcement" in state.events, "实测没走墙 —— 它证明不了模型的稿子能编"
    assert record["status"] in {"works", "broken", "absent"}
    assert record["works"] is (record["status"] == "works")
    assert pdf_toolchain.read_record() == record


# ── 5. 宏包从哪来：只有框架自己的样本联网，缺包由唯一的编译路径自己补 ─────────────

ROOT = Path(__file__).resolve().parents[1]


def _with_tectonic(monkeypatch):
    monkeypatch.setattr(latex.shutil, "which",
                        lambda n: "/opt/tectonic" if n == "tectonic" else None)


class _Compiles:
    """假的 ``_run_process``：记下每次的文档名与网络开关；按剧本决定成败。"""

    def __init__(self, offline_fails: int, *, offline_stdout: bytes = b"",
                 offline_log: str | None = None, online_fails: bool = False) -> None:
        self.calls: list[tuple[str, bool]] = []
        self.offline_fails = offline_fails
        self.offline_stdout = offline_stdout
        self.offline_log = offline_log
        self.online_fails = online_fails

    async def __call__(self, state, command, cwd, timeout, *, home="host", network=False):
        tex_name = command[-1]
        self.calls.append((tex_name, network))
        if network:
            if self.online_fails:
                return "done", 1, b"", b"error: failed to lookup address information: nodename nor servname provided"
            return "done", 0, b"", b""
        if self.offline_fails > 0:
            self.offline_fails -= 1
            if self.offline_log is not None:
                (Path(cwd) / Path(tex_name).with_suffix(".log")).write_text(self.offline_log, encoding="utf-8")
            # 2026-09-24 本机空缓存的原样：取 bundle 撞墙，排版没开始，没有 .log。
            return "done", 1, self.offline_stdout, (
                b"error: error sending request for url (https://relay.fullyjustified.net/default_bundle_v33.tar)"
                b": error trying to connect: tcp connect error: Operation not permitted (os error 1)\n")
        return "done", 0, b"", b""


def _run_tex(tmp_path: Path, name: str = "paper.tex") -> latex.CompileRun:
    return asyncio.run(latex.run_tex(None, tmp_path, name, "xelatex", 60))


def test_an_empty_cache_is_filled_by_the_frameworks_samples_then_recompiled_offline(
        monkeypatch, tmp_path):
    _with_tectonic(monkeypatch)
    fake = _Compiles(offline_fails=1)
    monkeypatch.setattr(latex, "_run_process", fake)

    run = _run_tex(tmp_path)

    assert fake.calls == [
        ("paper.tex", False),        # 模型的稿子：断网，撞墙
        ("probe.tex", True),         # 框架的样本：联网取包
        ("probe-tikz.tex", True),
        ("paper.tex", False),        # 同一份稿子：仍然断网，用刚取进来的包
    ]
    assert (run.status, run.returncode) == ("done", 0)


def test_a_tex_complaint_is_the_manuscripts_and_fetches_nothing(monkeypatch, tmp_path):
    _with_tectonic(monkeypatch)
    fake = _Compiles(offline_fails=1, offline_log="! Undefined control sequence.\nl.12 \\foo\n")
    monkeypatch.setattr(latex, "_run_process", fake)

    run = _run_tex(tmp_path)

    assert fake.calls == [("paper.tex", False)], "TeX 已经说了稿子哪里错：不是缓存的事，不取包"
    assert run.returncode == 1


def test_a_stale_log_from_an_earlier_round_does_not_count_as_this_rounds_complaint(
        monkeypatch, tmp_path):
    """工程目录整棵拷进现场，上一轮的 paper.log 跟着进来；这一次在排版前就撞了墙。"""
    _with_tectonic(monkeypatch)
    (tmp_path / "paper.log").write_text("! LaTeX Error: File `old.sty' not found.\n", encoding="utf-8")
    fake = _Compiles(offline_fails=1)
    monkeypatch.setattr(latex, "_run_process", fake)

    _run_tex(tmp_path)

    assert ("probe.tex", True) in fake.calls, "旧日志里的错不是这一次的：该取包"


def test_latexmk_never_fetches(monkeypatch, tmp_path):
    _with_latexmk(monkeypatch)
    fake = _Compiles(offline_fails=1)
    monkeypatch.setattr(latex, "_run_process", fake)

    _run_tex(tmp_path)

    assert fake.calls == [("paper.tex", False)], "系统 TeX Live 自己带全套包，框架不替它联网"


def test_when_fetching_fails_its_own_words_reach_the_attribution(monkeypatch, tmp_path):
    """机器没网：重编照样撞墙，但读归属的人要看到「取包没成功」和它的原因，而不是只看到墙。"""
    _with_tectonic(monkeypatch)
    fake = _Compiles(offline_fails=2, online_fails=True)
    monkeypatch.setattr(latex, "_run_process", fake)

    run = _run_tex(tmp_path)

    assert run.returncode == 1
    said = run.stderr.decode("utf-8")
    assert "联网取宏包没成功" in said and "nodename nor servname" in said, said


def test_the_measurement_fetches_through_the_same_path(monkeypatch, tmp_path):
    """实测不另开一条取包的路：它断网编样本，缺包由 run_tex 自己补。"""
    _with_tectonic(monkeypatch)
    fake = _Compiles(offline_fails=1)

    async def compiles_and_writes_the_pdf(state, command, cwd, timeout, **kw):
        result = await fake(state, command, cwd, timeout, **kw)
        if result[1] == 0 and not kw.get("network"):
            (Path(cwd) / "probe.pdf").write_bytes(pdf_toolchain._tiny_pdf())
        return result

    monkeypatch.setattr(latex, "_run_process", compiles_and_writes_the_pdf)
    record = asyncio.run(pdf_toolchain.measure())

    assert record["works"] is True, record
    assert fake.calls[0] == ("probe.tex", False) and fake.calls[-1] == ("probe.tex", False)
    assert [network for _, network in fake.calls].count(True) == 2


def _network_keyword(call: ast.Call) -> ast.expr | None:
    for keyword in call.keywords:
        if keyword.arg == "network":
            return keyword.value
    return None


def _called_name(call: ast.Call) -> str:
    func = call.func
    return func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")


def _repo_python_files() -> list[Path]:
    """仓库里的 .py：git 跟踪的 + 还没提交但没被忽略的。

    不按盘扫：打过一次包，``dist/ScienceMate.app`` 里就躺着一整份 harness 的拷贝，闸会去
    判它（2026-09-24 本机正是这样红的）—— 判据随这台机器上碰巧有什么而变。
    """
    listed = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "-co", "--exclude-standard", "-z", "*.py"],
        capture_output=True, check=True).stdout.decode("utf-8", errors="replace")
    files = [ROOT / name for name in listed.split("\0") if name and (ROOT / name).is_file()]
    assert files, "git ls-files 一个 .py 都没列出来 —— 扫描语料是空的，这条闸什么也没证明"
    return files


def test_only_the_frameworks_own_samples_compile_with_the_network():
    """``network=True`` 只许出现在取包那一处；latex.py 自己只许原样往下传。

    稿子是模型写的：让它在墙里联网，``\\input{把数据编进文件名}`` 就是一条外带通道。
    """
    allowed = {
        ROOT / "shared" / "lib" / "pdf_toolchain.py": "取包：框架写死的样本",
        ROOT / "shared" / "tools" / "library" / "latex.py": "run_tex 把自己的参数往下传",
    }
    found: list[str] = []
    for path in _repo_python_files():
        rel = path.relative_to(ROOT)
        if "tests" in rel.parts:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and _called_name(node) in {"run_tex", "_run_process"}):
                continue
            value = _network_keyword(node)
            if value is None or (isinstance(value, ast.Constant) and value.value is False):
                continue
            if path not in allowed:
                found.append(f"{rel}:{node.lineno}")
            elif path.name == "latex.py" and not (isinstance(value, ast.Name) and value.id == "network"):
                found.append(f"{rel}:{node.lineno}（latex.py 里只许原样传 network）")
    assert not found, "这些 TeX 编译打开了网络：" + "、".join(found)


def _tikz_needs(tex: str) -> dict:
    need = _needs(tex)
    need["libraries"] = {name.strip() for group in re.findall(r"\\usetikzlibrary\{([^}]+)\}", tex)
                         for name in group.split(",")}
    return need


def test_the_figure_sample_needs_everything_the_figure_backend_needs(monkeypatch):
    """出图（tikz 后端 + 它的 CJK 前言，量字宽用同一份）要的包，取包的样本里都要有 ——
    缺一个，断网的墙里那张图就编不出来，而 PDF 实测照样说「能」。"""
    from nodes.postprocess import diagram_compiler

    sample = _tikz_needs(pdf_toolchain.PROBE_TIKZ_TEX)
    for fonts in ([], ["Songti SC"]):     # 扫不到 CJK 字体 / 扫到了：两种前言
        monkeypatch.setattr("nodes.postprocess.fonts.cjk_families", lambda f=fonts: f)
        doc = diagram_compiler._TIKZ_DOC % {"cjk": diagram_compiler._cjk_preamble(), "body": ""}
        need = _tikz_needs(doc)
        assert need["class"] == sample["class"]
        assert not need["packages"] - sample["packages"], sorted(need["packages"] - sample["packages"])
        assert not need["libraries"] - sample["libraries"], sorted(need["libraries"] - sample["libraries"])

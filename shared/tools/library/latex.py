"""Compile single-file or project-style LaTeX sources in the trusted image.

编译器按这个顺序挑（``_resolve_compiler``）：**latexmk** 在则用它（Linux 镜像自带全套
TeX Live，逐字不变）；缺则 **tectonic**（单文件、自带按需取包 + 多轮 + BibTeX/Biber、
bundle 版本锁定 —— Windows 上没有任何 LaTeX，随包 tectonic 是 RFC §? 的架构答案，且比
"host 上碰巧装了什么 TeX"更可复现）。两者都没有才判"没有 LaTeX 工具链"。这不是"host
依赖的偶然回退"：tectonic 是**故意随产品分发**的那一个。

**失败分四类，各归各的下一步**（2026-09-09 修：此前四类压成一句"LaTeX 编译失败"，
界面统一建议"去查你的源码"，于是缺工具链那次模型连查三轮完全正确的稿子）：

  编译器跑完拒绝源码  → rejected          模型照 TeX 报错改稿再来
  两个编译器都不在场  → toolchain_missing 人去装 TeX，模型改稿无用
  失败却没有 TeX 报错，且已知能编过的样本在同一条路上也编不过
                      → toolchain_missing 这台机器出不了 PDF（shared/lib/pdf_toolchain 实测）
  有编译器但起不来    → command_failed    平台侧执行环境故障
  人按了停止          → 取消，不是失败

判据由**知道真相的那一层**给：`_run_process` 原样交回 spawn 的 status，
`_launch_failure` 在失败当时现算"哪些编译器不在 PATH"。

issue #107：这是 writing 节点原本的增强版实现（single-file + structured
project、CJK 引擎自动选择、PDF 校验、路径逃逸防护）迁移上来的，取代了旧版
只支持单文件的极简实现。writing 不再同名覆盖这个工具——白名单可用，全节点
可用（跟被替换前的旧版一样是框架级默认工具，不限定 allowed_node_types）。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import shutil
from pathlib import Path
from typing import Any, NamedTuple

from core import tool_errors as _errs
from core.state import State
from core.tool_registry import ToolDefinition, register_tool
from shared.lib.latex_layout import audit_latex_log

_VALID_OUTPUT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_CJK_TEXT = re.compile(
    "[\u2e80-\u2eff\u3000-\u303f\u3040-\u30ff\u31f0-\u31ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]"
)
_ENGINES = {"auto", "pdflatex", "xelatex", "lualatex"}
_LATEXMK_FLAGS = {
    "pdflatex": "-pdf",
    "xelatex": "-xelatex",
    "lualatex": "-lualatex",
}


def _error(message: str, **details: Any) -> dict[str, Any]:
    return {"status": "error", "error": message, **details}


#: TeX / biber 自己报错的那几种行。writing 的出稿报告与失败归属读的是同一份。
_TEX_ERROR_LINES = tuple(re.compile(p, re.M) for p in (
    r"^! (.+)$",
    r"^(\S+\.tex:\d+: .+)$",
    r"^(.*LaTeX Error: .+)$",
    r"^(.*Emergency stop.*)$",
    r"^(.*Runaway argument.*)$",
    r"^(.*Undefined control sequence.*)$",
    r"^(ERROR - .+)$",           # biber 的 .blg
))


def tex_errors(text: str, *, limit: int = 10) -> list[str]:
    """日志里 TeX / biber **自己**说出来的错（去重、保序）。

    有这些行 = 编译器读了稿子并指出了哪里不对，下一步在写稿的人手里。一行都没有而
    编译又失败了，就不能直接当成稿子的错 —— 见 ``_compile_latex`` 的失败归属。
    """
    found: list[str] = []
    for pattern in _TEX_ERROR_LINES:
        found.extend(match.group(1).strip() for match in pattern.finditer(text or ""))
    return list(dict.fromkeys(line for line in found if line))[:limit]


def _log_stamps(compile_dir: Path, tex_name: str) -> dict[Path, int]:
    """``<stem>.log`` / ``.blg`` 此刻的修改戳（不存在的不列）。"""
    stem = compile_dir / Path(tex_name).stem
    stamps: dict[Path, int] = {}
    for path in (stem.with_suffix(".log"), stem.with_suffix(".blg")):
        try:
            stamps[path] = path.stat().st_mtime_ns
        except OSError:
            continue
    return stamps


def _tex_complaints(before: dict[Path, int], after: dict[Path, int],
                    stdout_parts: list[bytes]) -> list[str]:
    """**这一次**编译里 TeX / biber 说出来的错：只读这次写过的日志（戳变了或新出现的）。

    工程目录整棵拷进现场，上一轮留下的 ``main.log`` 会跟着进来；这次编译若在排版开始前
    就失败（取包撞墙），那份旧日志原封不动 —— 读它就会把上一轮的错当成这一轮的。
    """
    logs = [path.read_text(encoding="utf-8", errors="replace")
            for path, stamp in after.items() if before.get(path) != stamp]
    logs += [part.decode("utf-8", errors="replace") for part in stdout_parts]
    return tex_errors("\n".join(logs))


def _safe_output_name(raw: str) -> str | None:
    if raw in {".", ".."} or not _VALID_OUTPUT_NAME.fullmatch(raw):
        return None
    return raw


def _safe_main_tex(raw: str) -> Path | None:
    path = Path(raw)
    if path.is_absolute() or ".." in path.parts or path.suffix.lower() != ".tex":
        return None
    return path


def _resolve_source_dir(state: State, raw: str) -> tuple[Path | None, str | None]:
    """把 source_dir 解析成绝对路径，边界交给统一的作用域解析器。

    以前这里自己要求"必须位于 state.root 之内"。`state.root` 是 run 缓存目录，
    而 manuscript 工程属于 writing 节点在 Project worktree 里的目录 —— 于是这道
    自制边界会把节点自己的稿子判成越界。边界只有一个真相源
    `resolve_tool_path`（worktree 内可读、只能写自己目录），这里只负责把"编译源
    不能是整棵树/必须是目录"这两条**本工具自己的**约束说清楚。
    """
    from core.project_workspace import resolve_tool_path

    try:
        path = resolve_tool_path(state, raw)
    except Exception as exc:
        return None, f"source_dir 越界或无法解析: {exc}"
    for boundary in (getattr(state, "project_worktree", None), state.root):
        if boundary is not None and path == Path(boundary).resolve():
            return None, "source_dir 不能是整个工作区根目录"
    if not path.is_dir():
        return None, f"source_dir 不存在或不是目录: {path}"
    return path, None


def _paths_overlap(first: Path, second: Path) -> bool:
    for candidate, parent in ((first, second), (second, first)):
        try:
            candidate.relative_to(parent)
        except ValueError:
            continue
        return True
    return False


def _path_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _reset_workdir(workdir: Path) -> None:
    if workdir.is_symlink() or workdir.is_file():
        workdir.unlink()
    elif workdir.exists():
        shutil.rmtree(workdir)


def _copy_project_tree(source: Path, workdir: Path,
                       exclude_root: Path | None = None) -> list[str]:
    """把工程树拷进编译现场。**产物目录和现场本身永远跳过。**

    现场已经搬回 run 缓存，所以 source 不会再包含 workdir。但 source 仍然会
    包含**产物**目录（`<worktree>/writing/latex_build/`，上一次编译提升上去的
    PDF 就在里面）—— 那些 PDF 不是编译输入，跟着拷进现场只会让每一轮都背上
    前一轮的产物。`exclude_root` 跳的就是它。

    "整个 writing 当 source_dir"是完全合法的布局，不许因此拒绝
    （E2E v22 实测：writing 连撞 4 次"source_dir 不能与 latex_build 重叠"，
    论文改不动）。跳过就够安全了 —— 拒绝的本意是防"把自己拷进自己"的无限
    递归，跳过同样达到目的，还不堵死合法布局。
    """
    source_root = source.resolve()
    # 跳整个 **latex_build 根**，不只是本次的输出子目录 —— 后者的父目录仍在
    # source 里，照样会被拷进去（第一版就漏在这：只跳 workdir，`latex_build/`
    # 本身照拷不误）。
    excluded = [workdir.resolve()]
    if exclude_root is not None:
        excluded.append(exclude_root.resolve())
    skipped: list[str] = []
    workdir.mkdir(parents=True)
    for path in sorted(source.rglob("*")):
        resolved_early = path.resolve()
        if any(resolved_early == ex or _path_within(resolved_early, ex) for ex in excluded):
            continue                     # 构建目录：不拷自己
        rel = path.relative_to(source)
        if path.is_symlink():
            skipped.append(str(rel))
            continue
        resolved = path.resolve()
        if not _path_within(resolved, source_root):
            skipped.append(str(rel))
            continue
        dest = workdir / rel
        if path.is_dir():
            dest.mkdir(parents=True, exist_ok=True)
        elif path.is_file():
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, dest)
    return skipped


PROVENANCE_NAME = "build.json"
_FINGERPRINT_SUFFIXES = {".tex", ".bib", ".cls", ".sty", ".bst"}


def _display(state: State, path: Path | None) -> str | None:
    if path is None:
        return None
    from core import paths

    return paths.display_relpath(state, path)


def _source_fingerprint(
    source: Path | None, tex_path: Path, source_text: str
) -> dict[str, Any]:
    """把"这份 PDF 是从什么源编出来的"压成**定长**的几个字段。

    不列文件清单：清单会随稿件规模增长，而 build.json 要进 Git、要进工作区
    观测事件 —— 一个会长大的字段就是把刚拆掉的那颗雷重新埋回去。

    定长仍然够用：`main_tex` + 它的哈希回答"这份 PDF 的入口是哪个 .tex"
    （review / clean 之分就在这一条上），`source_tree_sha256` 回答"整棵源码
    树后来动过没有"。要具体差异，去 Project Git 里比 —— 那才是源码的真相源。
    """
    entry_sha = hashlib.sha256(tex_path.read_bytes()).hexdigest()
    if source is None:
        return {
            "main_tex_sha256": entry_sha,
            "source_tree_sha256": hashlib.sha256(source_text.encode("utf-8")).hexdigest(),
            "source_file_count": 1,
        }
    digest = hashlib.sha256()
    count = 0
    for path in sorted(source.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        if path.suffix.lower() not in _FINGERPRINT_SUFFIXES:
            continue
        try:
            body = path.read_bytes()
        except OSError:
            continue
        digest.update(str(path.relative_to(source)).encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(body).digest())
        count += 1
    return {
        "main_tex_sha256": entry_sha,
        "source_tree_sha256": digest.hexdigest(),
        "source_file_count": count,
    }


def _promote(outdir: Path, pdf_path: Path, provenance: dict[str, Any]) -> tuple[Path, Path]:
    """把交付物从编译现场提升到产物目录。**只搬 PDF，外加一份出处。**

    "只搬这两个"是本函数的全部意义 —— 现场里那份源码副本和 TeX 中间件
    （.aux/.log/.out/.bbl）留在 run 缓存里就好：它们可由源码重算，没有人
    在下一个 session 需要它们，而它们进了 Git 目录就会变成每次编译一份
    64 KB 的观测 diff（2026-08-19 那次把整条 run 杀掉的就是它）。
    """
    outdir.mkdir(parents=True, exist_ok=True)
    delivered = outdir / pdf_path.name
    shutil.copy2(pdf_path, delivered)
    record = outdir / PROVENANCE_NAME
    record.write_text(
        json.dumps(provenance, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return delivered, record


def _read_project_tex(project_dir: Path) -> str:
    chunks: list[str] = []
    total = 0
    for path in sorted(project_dir.rglob("*.tex")):
        if total >= 5_000_000:
            break
        try:
            text = path.read_text(encoding="utf-8", errors="replace")[:2_000_000]
        except OSError:
            continue
        chunks.append(text)
        total += len(text)
    return "\n".join(chunks)


def _is_pdf_file(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            return handle.read(5) == b"%PDF-"
    except OSError:
        return False


def _select_engine(requested: str, source_text: str) -> tuple[str | None, str | None]:
    if requested not in _ENGINES:
        return None, f"engine 必须是以下之一: {', '.join(sorted(_ENGINES))}"

    if requested != "auto":
        return requested, None

    contains_cjk = bool(_CJK_TEXT.search(source_text))
    # Engine availability belongs to the trusted sandbox image contract, not
    # the host Python environment running the control plane.
    return ("xelatex" if contains_cjk else "pdflatex"), None


#: 我们认得的编译器，**按优先级**。`_resolve_compiler` 和"这台机器有没有 LaTeX"
#: 这两个问题共用这一份名单 —— 分成两份就会出现"选的时候认 3 个、判缺失时只认 2 个"。
_COMPILERS = ("latexmk", "tectonic")


def _resolve_compiler() -> tuple[str, str]:
    """挑编译器 ``(kind, argv0)``：优先 ``latexmk``（全套 TeX，逐字不变）；缺则 ``tectonic``
    （单文件、自带按需取包 + 多轮 + BibTeX，Windows 无 LaTeX 时随包的那一个）。

    这里只回答**先试哪个**，不回答"这台机器到底有没有 LaTeX"。后者由真正起进程的那
    一层给（`_run_process` 的 status），因为控制面的 ``PATH`` 只是对沙箱环境的猜测 ——
    今天两者共享文件系统，明天换个隔离后端就未必。两者都不在 PATH 时照常拼 latexmk
    命令交给 spawn：让**权威的那一层**去说 not-found，而不是在这里替它下结论。

    ``argv0`` 用**解析到的路径**（在时）或裸名。"""
    for name in _COMPILERS:
        found = shutil.which(name)
        if found:
            return name, found
    return _COMPILERS[0], _COMPILERS[0]


def absent_compilers() -> list[str]:
    """``_COMPILERS`` 里 PATH 上找不到的那几个（保序）。全在这里 = 这台机器没有 TeX。

    **现算**，不缓存：装好 texlive / 放上 tectonic 之后，下一次调用就该自己好起来。"""
    return [name for name in _COMPILERS if shutil.which(name) is None]


def no_tex_engine() -> bool:
    """这台机器上一个我们认得的编译器都没有。「有没有 TeX」只有这一个问法 —— 出图、
    量字宽、测试的 skip 条件都问它，不各自 ``which("xelatex")``。"""
    return len(absent_compilers()) == len(_COMPILERS)


def _launch_failure(
    run: "CompileRun", stderr_text: str, common: dict[str, Any]
) -> dict[str, Any]:
    """编译器**没能启动**。这里回答唯一有用的问题：接下来该谁动手？

    `spawn_failed` 有两个来源，下一步完全不同，所以必须在这里分开 ——
    分不开的代价实测过：两者都显示成"LaTeX 编译失败 / Review the document
    source"，而那句话对两者**都是错的**。

      · 两个编译器都不在 PATH  → 这台机器缺东西，**人**去装（模型改稿无用）
      · 有编译器但进程起不来   → 沙箱/隔离后端的问题，**我们**去查

    判据是**现算**的（每次失败重新 `which` 一遍），不是开局采样的快照：装好
    texlive 之后下一次调用就该自己好起来，不该被上一次的记录关在门外。
    """
    absent = absent_compilers()
    tail = stderr_text[-1500:]
    if len(absent) == len(_COMPILERS):
        return _error(
            "这台机器上没有 LaTeX 工具链："
            + "、".join(_COMPILERS)
            + " 都不在 PATH 上，编译器进程没能启动。"
            "**这与稿件内容无关**——改 LaTeX 源码不会让它变好，重试也不会。"
            "把这一条当作环境 blocker 上报，别继续尝试编译。",
            error_code=_errs.TOOLCHAIN_MISSING,
            recovery="需要在运行 harness 的机器上装 LaTeX："
                     "Linux `apt install texlive-full latexmk`，"
                     "或把 tectonic 放进 PATH。装好后重新编译即可。",
            missing_compilers=list(_COMPILERS),
            stderr_tail=tail,
            **common,
        )
    return _error(
        f"编译器 {run.argv0!r} 没能启动（隔离后端/沙箱问题，不是稿件问题）："
        + (tail.strip().splitlines() or [""])[-1][:300],
        error_code=_errs.COMMAND_FAILED,
        recovery="这是平台侧的执行环境故障，改稿件无用；请把 stderr_tail 交给平台维护者。",
        available_compilers=[n for n in _COMPILERS if n not in absent],
        stderr_tail=tail,
        **common,
    )


async def _run_process(
    state: State,
    command: list[str],
    cwd: Path,
    timeout: float,
    *,
    home: str = "host",
    network: bool = False,
) -> tuple[str, int | None, bytes, bytes]:
    """起编译进程，**原样交回 spawn 的 status**（done / cancelled / timeout / spawn_failed）。

    以前这里把 status 压成两个信号：timeout→`(None, …, True)`、spawn_failed→**退出码
    127**、其余（含 cancelled）→退出码原样。压平之后下游只剩"退出码非零"一种事实，
    于是三件完全不同的事在界面上是同一句话：

      · 稿子里有 TeX 错误        → 模型改源码就能过
      · 这台机器没装 latexmk     → 模型改多少次都没用，要人去装
      · 人按了停止               → 谁都没错

    2026-09-09 实测第二种：三轮 `bwrap: execvp latexmk: No such file or directory`
    全部显示成"LaTeX 编译失败 / Review the document source"，模型三轮都在查一份
    完全正确的稿子。**归属只能在知道真相的那一层定**，而知道的就是这里。

    2026-09-15 node20 又见同一病、另一个长相：Landlock 启动器自己起来了，`os.execvp
    latexmk` 才 ENOENT —— 启动器以 traceback + 非零码退出，咽喉把它记成 done/rc≠0，
    这里原样交出去仍是「编译失败」。bwrap / sandbox-exec 自己 execvp 失败也是同一形状
    （普通非零码，09-09 那三轮其实从来不是 spawn_failed）。所以「载荷没起来」这句话
    改由**墙的最内层**（我们自己的启动器）说：stderr 一行 `HARNESS_ISOLATION_ERROR
    exec_failed:…`，咽喉读到即报 spawn_failed（`core.isolation.launcher_refusal`）。

    走唯一咽喉：后端由它选、kill_event / 超时 / 整组清扫 / 记账都在那里。
    以前这里自己 prepare_attempt_command + create_subprocess_exec，是三处
    「已在墙内但绕开咽喉」之一（PR C 收掉）。
    """
    from core.sandbox import SandboxLimits
    from shared.lib.cancellable_subprocess import spawn_and_wait

    status, returncode, stdout, stderr = await spawn_and_wait(
        *command,
        state=state,
        timeout=max(1.0, float(timeout)),
        cwd=str(cwd),
        writable_roots=[cwd],
        sandbox_home=home,
        network_access=network,
        sandbox_limits=SandboxLimits(
            memory_bytes=4 * 1024**3,
            cpus=2,
            pids=128,
            walltime_seconds=max(1, int(timeout)),
            storage_bytes=4 * 1024**3,
            output_bytes=16 * 1024**2,
        ),
    )
    return status, returncode, stdout, stderr


def _remaining_timeout(deadline: float) -> float:
    return max(0.1, deadline - asyncio.get_running_loop().time())


class CompileRun(NamedTuple):
    """一次编译跑完之后的**全部事实**。判决在 `_compile_latex` 里下，这里只记录。

    以前这是个 6 元组，最后一位 `sequence_error` **恒为 None** —— 那是更早以前
    "latexmk 失败就退回逐轮 pdflatex+bibtex" 那套多命令脚手架的残骸；脚手架删了，
    槽位留着，`if sequence_error:` 于是成了一条永不执行的死路。留着空槽比没有更糟：
    读代码的人以为"起不来"已经有人处理了。具名字段就没有藏得住的空槽。
    """

    status: str                    # spawn 的原话：done / cancelled / timeout / spawn_failed
    returncode: int | None
    commands: list[list[str]]
    stdout: bytes
    stderr: bytes
    compiler: str                  # "latexmk" / "tectonic"
    argv0: str


async def run_tex(
    state: State | None,
    compile_dir: Path,
    tex_name: str,
    engine: str,
    timeout: float,
    *,
    network: bool = False,
) -> CompileRun:
    """把 ``compile_dir/tex_name`` 编成 PDF —— **框架的每一次 TeX 编译都走这里。**

    调用方：稿件（``compile_latex``）、PDF 实测（``shared.lib.pdf_toolchain``）、示意图的
    tikz 后端（``nodes/postprocess/tools/figure.py::_finish_tikz_render``）和它排版前的
    量字宽道次（``nodes/postprocess/text_metrics``）。「用哪个编译器、跑在哪堵墙后、住哪个
    家」只在这里答一次：写死 ``xelatex`` 的出图路径曾经是第二个答案 —— 只有随包 tectonic
    的 Windows 上示意图一张都出不来，而 PDF 实测说「能」。

    墙：可写的只有 ``compile_dir``（``_run_process`` 的 ``writable_roots``）。家：latexmk 是
    用户的 TeX Live（``host``），tectonic 是平台自带的（``own``），见下面分支里的注释。
    ``state`` 为 None = 框架自己发起、不属于任何 run 的编译：没有停止按钮，也不记 transcript。

    网：**默认断网**（墙在 macOS / Linux 上真断，Windows 上断不了、记在账上）。tectonic 按
    需联网取宏包，而稿子是模型写的 —— 让它在墙里联网，``\\input{把数据编进文件名}`` 就是
    一条外带通道（「网络与数据不同框」）。所以 ``network=True`` 只给框架**自己写死**的样本
    （``shared.lib.pdf_toolchain.acquire_packages`` 取宏包进缓存），模型的稿子永远断网、用
    缓存里的包；没有别的调用方打开它由 ``tests/test_pdf_toolchain_is_measured.py`` 扫盘钉住。
    缓存里缺包（第一次编、或缓存被清）由这里自己补：断网的 tectonic 失败而 TeX 一句没说，
    就先取包、再断网重编一次 —— 不管第一次编的是哪个调用方，都走同一条路。
    """
    deadline = asyncio.get_running_loop().time() + timeout
    commands: list[list[str]] = []
    stdout_parts: list[bytes] = []
    stderr_parts: list[bytes] = []

    async def run(command: list[str], *, home: str = "host") -> tuple[str, int | None]:
        # network 只从 run_tex 的参数来，不在分支里另给。
        commands.append(command)
        status, returncode, stdout, stderr = await _run_process(
            state,
            command,
            compile_dir,
            _remaining_timeout(deadline),
            home=home,
            network=network,
        )
        stdout_parts.append(stdout)
        stderr_parts.append(stderr)
        return status, returncode

    kind, binary = _resolve_compiler()
    if kind == "latexmk":
        command = [
            binary,
            _LATEXMK_FLAGS[engine],
            "-interaction=nonstopmode",
            "-halt-on-error",
            "-file-line-error",
            tex_name,
        ]
        status, returncode = await run(command)
    else:  # tectonic：自己按需取包 + 多轮 + BibTeX/Biber，单条命令即可。
        # 它自成一体（宏包按需取、biber 随包），不需要用户的家里有任何东西 —— 所以住墙给的
        # 家（home="own"）。它的包缓存、格式文件、字体缓存按本平台约定写进「用户缓存目录」
        # （Windows ``%LOCALAPPDATA%``、macOS ``~/Library/Caches``、Linux XDG），墙给的家
        # 把那一块链到持久缓存。曾经给它指 ``TECTONIC_CACHE_DIR``、让它照用用户的家：那只
        # 盖住了包缓存、没盖住 formats，干净的 Windows 上每编一次 5 秒 os error 5。
        # latexmk 走系统的 TeX Live，是用户的程序（home="host"：个人宏包在 TEXMFHOME 里）。
        # --keep-logs 让它落 <stem>.log，版式审计要读。engine（xelatex/pdflatex）对 tectonic
        # 无意义：它就是 XeTeX（xelatex 的超集）。
        command = [binary, "--keep-logs", "--outdir", str(compile_dir), tex_name]
        before = _log_stamps(compile_dir, tex_name)
        status, returncode = await run(command, home="own")
        if (not network and status == "done" and returncode != 0
                and not _tex_complaints(before, _log_stamps(compile_dir, tex_name), stdout_parts)):
            # 断网的 tectonic 失败了，TeX 却一句没说：它要的包不在缓存里（这台机器第一次编、
            # 或缓存被清过）—— 取包那一步撞了墙，排版根本没开始。框架用自己写死的样本联网
            # 把平台要的包取进缓存，再断网重编这一次。取包不占调用方的编译预算（它是在准备
            # 环境，不是在编这份稿子）；取不到的原话放在 stderr 最前面，归属判据读得到。
            from shared.lib import pdf_toolchain

            failed = await pdf_toolchain.acquire_packages(state)
            if failed:
                stderr_parts.append(f"error: 联网取宏包没成功：{failed}".encode("utf-8"))
            deadline = asyncio.get_running_loop().time() + timeout
            status, returncode = await run(command, home="own")
    return CompileRun(
        status=status,
        returncode=returncode,
        commands=commands,
        stdout=b"\n".join(stdout_parts),
        stderr=b"\n".join(stderr_parts),
        compiler=kind,
        argv0=binary,
    )


async def _compile_latex(
    state: State,
    tex_source: str | None = None,
    output_name: str = "paper",
    timeout: int = 180,
    bibtex_source: str | None = None,
    source_dir: str | None = None,
    main_tex: str = "main.tex",
    engine: str = "auto",
    **_: Any,
) -> dict[str, Any]:
    """Compile either tex_source or a project directory into a fresh PDF."""
    safe_output_name = _safe_output_name(output_name)
    if not safe_output_name:
        return _error("output_name 只能包含字母、数字、点、下划线和连字符，且不能是路径")

    has_source = bool(tex_source and tex_source.strip())
    has_project = bool(source_dir and source_dir.strip())
    if has_source == has_project:
        return _error("必须且只能提供 tex_source 或 source_dir 其中之一")

    from core import paths

    # 现场（run 缓存，随便清空）和产物（节点 Git 目录，只放 PDF + build.json）
    # 是两个目录 —— 见 paths.latex_build_dir 的 docstring。
    workdir = paths.latex_scratch_dir(state, output_name=safe_output_name)
    outdir = paths.latex_build_dir(state, output_name=safe_output_name)
    source: Path | None = None
    safe_main_tex: Path | None = None
    skipped_project_paths: list[str] = []

    if has_project:
        source, source_error = _resolve_source_dir(state, source_dir or "")
        if source_error:
            return _error(source_error)
        safe_main_tex = _safe_main_tex(main_tex)
        if not safe_main_tex:
            return _error("main_tex 必须是 source_dir 内的相对 .tex 路径，且不能包含 '..'")
        assert source is not None
        # 只拒**真正退化**的：source 落在这一轮会被清空的两个目录里的任何一个
        # （现场、产物）。source **包含**产物目录是合法的常见布局，
        # 交给 _copy_project_tree 跳过即可。
        source_resolved = source.resolve()
        for wiped in (workdir, outdir):
            wiped_resolved = wiped.resolve()
            if source_resolved == wiped_resolved or _path_within(source_resolved, wiped_resolved):
                return _error(
                    "source_dir 不能是 latex_build 输出目录本身或它的子目录"
                    "（构建前会被清空）。请把稿件源放在 latex_build 之外，"
                    f"例如 {source_resolved.parent.name}/ 下的工程目录。"
                )

    # 两个都先清空。产物目录也必须清 —— 否则一次失败的编译会把上一轮的 PDF
    # 留在原地冒充这一轮的交付物（下游只按路径取件，认不出它是旧的）。
    _reset_workdir(workdir)
    _reset_workdir(outdir)

    if has_project:
        assert source is not None
        assert safe_main_tex is not None
        skipped_project_paths = _copy_project_tree(
            source, workdir, exclude_root=paths.latex_build_dir(state))
        tex_path = workdir / safe_main_tex
        if not tex_path.is_file():
            return _error(f"main_tex 不存在: {safe_main_tex}")
        source_text = _read_project_tex(workdir)
        source_mode = "project"
    else:
        workdir.mkdir(parents=True)
        tex_path = workdir / f"{safe_output_name}.tex"
        tex_path.write_text(tex_source or "", encoding="utf-8")
        source_text = tex_source or ""
        source_mode = "single_file"

    if bibtex_source:
        (tex_path.parent / f"{tex_path.stem}.bib").write_text(
            bibtex_source,
            encoding="utf-8",
        )

    selected_engine, engine_error = _select_engine(engine, source_text)
    if engine_error:
        return _error(engine_error, requested_engine=engine)
    assert selected_engine is not None

    compile_dir = tex_path.parent
    run = await run_tex(
        state,
        compile_dir,
        tex_path.name,
        selected_engine,
        timeout,
    )
    returncode = run.returncode
    commands = run.commands

    decoded_stdout = run.stdout.decode("utf-8", errors="replace")
    decoded_stderr = run.stderr.decode("utf-8", errors="replace")
    build_log_path = compile_dir / f"{tex_path.stem}.harness-build.log"
    command_text = "\n".join(" ".join(command) for command in commands)
    build_log_path.write_text(
        f"commands:\n{command_text}\n\nstdout:\n{decoded_stdout}\n\nstderr:\n{decoded_stderr}\n",
        encoding="utf-8",
    )
    final_log_path = tex_path.with_suffix(".log")
    result_log_path = final_log_path if final_log_path.exists() else build_log_path
    final_log_text = result_log_path.read_text(encoding="utf-8", errors="replace")
    layout_audit = audit_latex_log(final_log_text)

    common = {
        "engine": selected_engine,
        "source_mode": source_mode,
        # workdir = 编译现场（run 缓存，日志在这儿，失败时来这里看）；
        # output_dir = 交付目录（节点 Git 目录，成功后只有 PDF + build.json）。
        "workdir": str(workdir),
        "output_dir": str(outdir),
        "log_path": str(result_log_path),
        "build_log_path": str(build_log_path),
        "commands": commands,
        "returncode": returncode,
        "skipped_project_paths": skipped_project_paths,
        "layout_audit": layout_audit,
    }
    if run.status == "spawn_failed":
        return _launch_failure(run, decoded_stderr, common)
    if run.status == "timeout":
        return {
            "status": "timeout",
            "timeout_s": timeout,
            **common,
        }
    if run.status == "cancelled":
        # 人按了停止。这不是稿子的问题、也不是我们的 bug —— 压成"编译失败"会让
        # 模型回头去改一份没毛病的稿子（这正是压平 status 的代价）。
        return _error(
            "编译被取消（有人停止了这次运行）",
            error_code=_errs.REJECTED,
            recovery="重新发起编译即可；本次没有产出 PDF。",
            **common,
        )

    pdf_path = tex_path.with_suffix(".pdf")
    if returncode != 0:
        blg = tex_path.with_suffix(".blg")
        errors = tex_errors("\n".join((
            final_log_text,
            blg.read_text(encoding="utf-8", errors="replace") if blg.is_file() else "",
            decoded_stdout,
        )))
        if not errors:
            # 编译器起来了、失败了，却**一句 TeX 报错都没留下**。这不能直接当成稿子的错：
            # 2026-09-23 干净 Windows 上 tectonic 每次 5 秒 `os error 5`，日志是空的，
            # 旧代码照样说「改那一节再编」，writing 就去改了一份没毛病的稿子，调度器又
            # 花半小时自己排查平台。判据在这里现算：拿一份**已知能编过**的样本走同一条路
            # 再编一次 —— 它也编不过，就是这台机器的事。
            from shared.lib import pdf_toolchain

            verdict = await pdf_toolchain.measure(state)
            if not verdict.get("works"):
                return _error(
                    "这台机器现在出不了 PDF —— 不是这份稿件的问题。框架拿一份已知能编过的"
                    "样本在同一条路上重编了一次，也失败了："
                    + (verdict.get("reason") or "没有留下原因"),
                    error_code=_errs.TOOLCHAIN_MISSING,
                    recovery=(
                        "别改稿、别重试、别自己排查平台（那不在你的权限里，也修不好）："
                        "把这一条原样作为环境 blocker 上报。稿件源码完好，在 workdir，"
                        "可以先把源码交付出去。"
                    ),
                    toolchain=verdict,
                    stdout_tail=decoded_stdout[-3000:],
                    stderr_tail=decoded_stderr[-1500:],
                    **common,
                )
        # 编译器读了稿子、拒绝了它（或者样本在同一条路上编得过）：下一步在写稿的人手里。
        # 不自带 code：由 _wrap_result 记成 rejected（ReAct 的正常一步）。
        return _error(
            "LaTeX 编译失败",
            latex_errors=errors,
            stdout_tail=decoded_stdout[-3000:],
            stderr_tail=decoded_stderr[-1500:],
            recovery=(
                "按 latex_errors 里的 <文件>:<行> 改源码里那一段，然后重新编译。" if errors else
                "编译器拒绝了这份源码但没留下 TeX 报错行（同一条路上已知能编过的样本编得过，"
                "所以是稿件的事）：看 stderr_tail 与 log_path，改源码后重新编译。"
            ),
            **common,
        )
    if not pdf_path.is_file() or pdf_path.stat().st_size <= 0:
        return _error(
            "LaTeX 命令返回成功，但本轮未生成有效 PDF",
            stdout_tail=decoded_stdout[-3000:],
            stderr_tail=decoded_stderr[-1500:],
            **common,
        )
    if not _is_pdf_file(pdf_path):
        return _error(
            "LaTeX 命令返回成功，但生成文件不是有效 PDF",
            stdout_tail=decoded_stdout[-3000:],
            stderr_tail=decoded_stderr[-1500:],
            pdf_path=str(pdf_path),
            **common,
        )

    # 判决拆除（verdicts_shared latex:514）：版面审计失败**不再销毁 PDF、不再拒绝**。
    # 审计结果本来就如实随返回值走（layout_audit 在 common 里）——检测一直诚实，
    # 旧拒绝分支只多做了"毁尸"一件事：看不见版面的科学家连调试都做不了（S5），
    # 而"这版没过版面审计"这句话由 layout_audit.passed=false 如实承载（S2）。
    pdf_bytes = pdf_path.read_bytes()
    build_record = {
        "schema_version": 2,
        "output_name": safe_output_name,
        "engine": selected_engine,
        "source_mode": source_mode,
        "main_tex": str(safe_main_tex) if safe_main_tex else tex_path.name,
        "source_dir": _display(state, source),
        "commands": commands,
        "returncode": returncode,
        "pdf_sha256": hashlib.sha256(pdf_bytes).hexdigest(),
        "pdf_size_bytes": len(pdf_bytes),
        "layout_audit": layout_audit,
        **_source_fingerprint(source, tex_path, source_text),
    }
    delivered_pdf, provenance_path = _promote(outdir, pdf_path, build_record)
    build_receipt_id: str | None = None
    if state.node_type == "writing":
        from core.artifact_capabilities import save_typed_artifact

        receipt_payload = {
            "artifact_type": "latex_build_receipt",
            "schema_version": 1,
            "pdf_path": str(delivered_pdf),
            "pdf_sha256": build_record["pdf_sha256"],
            "build_provenance_path": str(provenance_path),
            "build_record": build_record,
        }
        saved_receipt = save_typed_artifact(
            state,
            artifact_type="latex_build_receipt",
            name=(
                f"{safe_output_name}_{build_record['pdf_sha256'][:12]}_"
                f"{build_record['source_tree_sha256'][:12]}"
            ),
            content=json.dumps(receipt_payload, ensure_ascii=False, indent=2),
            metadata={
                "pdf_sha256": build_record["pdf_sha256"],
                "layout_audit_passed": bool(layout_audit.get("passed")),
                "output_name": safe_output_name,
            },
        )
        build_receipt_id = saved_receipt["id"]
    return {
        "status": "success",
        "pdf_path": str(delivered_pdf),
        "build_provenance_path": str(provenance_path),
        "latex_build_receipt_id": build_receipt_id,
        "scratch_pdf_path": str(pdf_path),
        "size_bytes": len(pdf_bytes),
        "pdf_sha256": hashlib.sha256(pdf_bytes).hexdigest(),
        "log_tail": decoded_stdout[-2000:],
        **common,
    }


register_tool(
    ToolDefinition(
        name="compile_latex",
        description=(
            "把单文件 LaTeX 或完整 LaTeX 项目编译成全新的 PDF。提供 tex_source，"
            "或提供位于当前 run 目录内的 source_dir + main_tex；两种模式二选一。"
            "engine 可选 auto、pdflatex、xelatex、lualatex；auto 检测到 CJK 时优先"
            "XeLaTeX/LuaLaTeX。优先使用 latexmk，缺失时改用 tectonic（它自带按需取包、"
            "多轮编译和 BibTeX/Biber；此时 engine 参数无意义——tectonic 就是 XeTeX）。"
            "两者都不在这台机器上时返回 error_code=toolchain_missing，那是环境缺失、"
            "不是稿件问题，重试和改源码都无效。编译失败却没有 TeX 报错时，框架会用一份"
            "已知能编过的样本在同一条路上重编：样本也编不过同样返回 toolchain_missing"
            "（附 toolchain 实测记录）。返回 PDF、构建日志、实际引擎和 SHA-256。"
            "TeX 返回 0 后仍会审计最终日志；表格/浮动体越界、alignment 溢出、"
            "大幅盒子溢出或未解析引用会失败，失败的 PDF 不会被提升。"
            "编译在 run 缓存里进行，成功后只把 PDF 和一份 build.json（编译出处）"
            "提升到 latex_build/<output_name>/ —— 源码副本和 .aux/.log 等中间件"
            "**不**进工作区，需要时按返回的 workdir/log_path 去 run 缓存里读。"
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "tex_source": {"type": "string", "description": "完整单文件 .tex 源码。"},
                "source_dir": {
                    "type": "string",
                    "description": "LaTeX 项目目录；相对路径从当前 run 目录解析。",
                },
                "main_tex": {
                    "type": "string",
                    "default": "main.tex",
                    "description": "项目模式入口 .tex，相对于 source_dir。",
                },
                "output_name": {
                    "type": "string",
                    "default": "paper",
                    "description": "隔离构建目录名；单文件模式下也作为 PDF 文件名。",
                },
                "engine": {
                    "type": "string",
                    "enum": ["auto", "pdflatex", "xelatex", "lualatex"],
                    "default": "auto",
                },
                "timeout": {
                    "type": "integer",
                    "default": 180,
                    "minimum": 10,
                    "maximum": 1800,
                },
                "bibtex_source": {
                    "type": "string",
                    "description": "可选 .bib 内容，写入入口 tex 同目录的同名 .bib 文件。",
                },
            },
        },
        risk_level="medium",
    ),
    _compile_latex,
)

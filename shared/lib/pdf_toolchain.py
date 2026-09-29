"""这台机器能不能出平台要的 PDF —— **实测**出来，不按「编译器文件在不在」猜。

## 为什么要实测（2026-09-23，一台干净的 Windows）

平台说「能出 PDF」的依据曾经是 tectonic.exe 这个文件存在（doctor、打包自检都这么判）。
那台机器上文件在，可它每次编译 5 秒就 ``os error 5``；随包里也根本没有 zh_article
模板要的 biber。writing 照着「ctexart + biblatex/biber」写了 45 分钟，最后一步才撞上，
调度器又花 30 分钟自己排查平台、最后用 matplotlib 手排了一份预览。开发机上一切正常 ——
缺的那个目录早就在，biber 是系统 TeX Live 带的。

所以「能不能」只有一种答法：拿一份**已知能编过**的样本，走**模型的稿子走的同一条路**
（同一个编译器选择、同一堵墙、墙给的同一个家）真编一次。编过了就是能；编不过，原因
就是这次实测的原话。

## 同一份观测，三处消费

    measure() ──→ toolchain/pdf.json（最近一次实测的记录，带时间与工具身份）
                   ├─→ 系统提示里的平台资源登记（调度器 / writing 开工前就知道）
                   ├─→ doctor（读记录）与打包自检（装完在安装位置真编一次）
                   └─← 编译失败而日志里没有 TeX 报错时，latex.py 当场重测一次，据此
                       判「稿子的错」还是「这台机器的事」

记录不是配置：它记的是「什么时候、用哪套工具、编没编过」。工具身份（编译器与 biber 的
路径和指纹、墙的后端）变了，记录就不再被当作当前的答案，下一次 ``ensure_measured``
（写论文开工时）重新实测。

## 宏包从哪来（随包 tectonic）

墙里断网（macOS / Linux 真断），而 tectonic 的宏包是按需联网取的。取包只在框架**自己
写死**的样本上联网（:func:`acquire_packages`：这里的两份样本 = 平台模板 + 出图前言要的
全部宏包），模型的稿子永远断网、只用缓存。触发点在唯一的编译路径上（``latex.run_tex``：
断网编译失败而 TeX 一句没说 = 取包撞墙 → 取包 → 重编一次），所以不管第一次编的是
稿子、示意图还是量字宽，也不管缓存是不是被清过，都是同一条路。

## 样本 = 平台模板的需求

样本的文档类、宏包与 biblatex 选项照 ``nodes/writing/renderers/*/main.tex.tmpl`` 写；
两边的对齐由 ``tests/test_pdf_toolchain_is_measured.py`` 机械钉住 —— 模板加了一个宏包而
样本没跟上，那条测试就红，而不是让实测对一个模板用不到的子集说「能」。
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core import paths

#: 样本的 TeX 源 —— 与平台模板同一套文档类 / 宏包 / biblatex 选项（对齐见模块 docstring）。
PROBE_TEX = r"""\documentclass[12pt,a4paper]{ctexart}
\usepackage[margin=2.5cm]{geometry}
\usepackage{booktabs,graphicx,caption,subcaption,amsmath,amssymb,array,multirow,longtable,float,adjustbox}
\usepackage[backend=biber,style=gb7714-2015,gbnamefmt=lowercase,sorting=none,url=false,doi=true]{biblatex}
\addbibresource{probe.bib}
\usepackage[colorlinks=true,linkcolor=blue,citecolor=blue,urlcolor=blue]{hyperref}
\begin{document}
\section{实测}
中文正文与公式 $\int_0^1 e^{x}\,\mathrm{d}x = e - 1$，引用\cite{probe}。
\begin{figure}[H]\centering\includegraphics[width=1cm]{probe-figure.pdf}\caption{示意}\end{figure}
\printbibliography[title=参考文献]
\end{document}
"""

PROBE_BIB = """@book{probe,
  author = {张三 and 李四},
  title = {数值分析},
  publisher = {高等教育出版社},
  year = {2020},
}
"""

#: 出图（tikz 后端，``nodes/postprocess/diagram_compiler._TIKZ_DOC`` + 它的 CJK 前言；排版前
#: 的量字宽用同一份前言）要的宏包。只用来**取宏包**（:func:`acquire_packages`），不参与
#: 结论：tectonic 在断网的墙里只认缓存，出图要的包也得先取进来（对齐同样由测试钉住）。
PROBE_TIKZ_TEX = r"""\documentclass[border=6pt]{standalone}
\usepackage{fontspec}
\usepackage{tikz}
\usetikzlibrary{positioning,fit,backgrounds,arrows.meta}
\usepackage{ctex}
\begin{document}
\begin{tikzpicture}\node[draw] (a) {示意 A}; \node[draw,right=of a] (b) {B};
\draw[-{Stealth}] (a) -- (b);\end{tikzpicture}
\end{document}
"""

#: 平台的 PDF 能力在提示里叫什么 —— 模型据此判断哪些交付受它影响。
WHAT = "PDF 排版（中文正文 + GB/T 7714 参考文献 + 插图）"


def _tiny_pdf() -> bytes:
    """一页 20×20pt 的 PDF，当样本里的插图（论文的图就是 PDF，走 xdvipdfmx 嵌入）。"""
    content = b"0 0 1 rg 2 2 16 16 re f"
    bodies = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 20 20] /Contents 4 0 R /Resources << >> >>",
        b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(bodies, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(bodies) + 1)
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(bodies) + 1, xref)
    return bytes(out)


def record_path() -> Path:
    return paths.toolchain_facts_dir() / "pdf.json"


def read_record() -> dict[str, Any] | None:
    try:
        record = json.loads(record_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) else None


def _write_record(record: dict[str, Any]) -> None:
    target = record_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    scratch = target.with_suffix(".json.tmp")
    scratch.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(scratch, target)


def _fingerprint(binary: str | None) -> list[int] | None:
    if not binary:
        return None
    try:
        info = Path(binary).stat()
    except OSError:
        return None
    return [info.st_size, info.st_mtime_ns]


def _backend_name() -> str:
    # 只读快照：拿后端实例是咽喉一家的事（test_model_commands_reach_the_os_through_one_throat）。
    from core import isolation

    return isolation.enforcement_snapshot().get("backend") or "none"


def identity() -> dict[str, Any]:
    """这一次会用哪套工具 —— 记录是不是当前答案，就看它和这个对不对得上。"""
    from shared.tools.library import latex

    kind, binary = latex._resolve_compiler()
    biber = shutil.which("biber")
    return {
        "compiler": kind,
        "binary": binary,
        "binary_fingerprint": _fingerprint(shutil.which(binary) or binary),
        "biber": biber,
        "biber_fingerprint": _fingerprint(biber),
        "backend": _backend_name(),
    }


def _reason(run: Any, compile_dir: Path, timeout: int, tex_name: str = "probe.tex") -> str:
    from shared.tools.library import latex

    if run.status == "timeout":
        return (f"实测 {timeout} 秒没编完 —— 第一次编译要联网取宏包，多半是网络太慢或断了")
    if run.status == "cancelled":
        return "实测被停止了"
    stderr = run.stderr.decode("utf-8", errors="replace")
    stdout = run.stdout.decode("utf-8", errors="replace")
    if run.status == "spawn_failed":
        last = [line for line in stderr.splitlines() if line.strip()]
        return "编译器没能启动：" + (last[-1][:300] if last else run.argv0)
    said = [line.strip() for line in (stderr + "\n" + stdout).splitlines()
            if line.strip().lower().startswith("error:")]
    stem = compile_dir / tex_name
    logs = "\n".join(
        path.read_text(encoding="utf-8", errors="replace")
        for path in (stem.with_suffix(".log"), stem.with_suffix(".blg")) if path.is_file())
    said += latex.tex_errors(logs)
    if not said:
        said = [line.strip() for line in (stderr + "\n" + stdout).splitlines() if line.strip()][-3:]
    return "；".join(dict.fromkeys(said))[:600] or f"编译器退出码 {run.returncode}，没有留下原因"


def _write_samples(work: Path) -> None:
    (work / "probe.tex").write_text(PROBE_TEX, encoding="utf-8")
    (work / "probe.bib").write_text(PROBE_BIB, encoding="utf-8")
    (work / "probe-figure.pdf").write_bytes(_tiny_pdf())
    (work / "probe-tikz.tex").write_text(PROBE_TIKZ_TEX, encoding="utf-8")


#: 取宏包的预算。第一次要把平台用到的包全取下来（2026-09-24 本机，空缓存：约 4 分钟、
#: 47 MB）；它不占调用方的编译预算 —— 那是在准备环境，不是在编那份稿子。
ACQUIRE_TIMEOUT_S = 900


async def acquire_packages(state: Any | None) -> str:
    """tectonic：用平台**自己写死**的两份样本联网编一次，把平台要的宏包取进持久缓存。

    调用方只有一个：``latex.run_tex`` —— 断网的 tectonic 编译失败而 TeX 一句没说（取包
    撞了墙）时，它调这里，再断网重编一次。这是框架拼的取物命令（``CommandSpec.network_access``
    留给的正是这一类）：文档里没有一个字来自模型；之后模型的稿子、框架的示意图与量字宽，
    都在断网的墙里用这里取进来的包。macOS / Linux 的墙真断网，不取，随包 tectonic 一个包
    都拿不到（2026-09-24 本机：``tcp connect error: Operation not permitted``）。

    返回取包失败的原话（都编过了为空）。
    """
    from shared.tools.library import latex

    root = Path(tempfile.mkdtemp(prefix="hf-pdf-acquire-")).resolve()
    said: list[str] = []
    try:
        for name in ("probe.tex", "probe-tikz.tex"):
            work = root / name.removesuffix(".tex")
            work.mkdir()
            _write_samples(work)
            run = await latex.run_tex(state, work, name, "xelatex", ACQUIRE_TIMEOUT_S, network=True)
            if not (run.status == "done" and run.returncode == 0):
                said.append(f"{name}: {_reason(run, work, ACQUIRE_TIMEOUT_S, name)}")
    finally:
        shutil.rmtree(root, ignore_errors=True)
    return "；".join(said)


async def measure(state: Any | None = None, *, timeout: int = 900) -> dict[str, Any]:
    """真编一次样本，写下记录并返回它。

    走 ``latex.run_tex`` —— 模型的稿子走的那一条：同一个编译器选择、同一堵墙、墙给的
    同一个家、**同样断网**（缓存里缺包时它自己先取，见 :func:`acquire_packages`）。给了
    ``state`` 就用它（停止按钮对实测同样有效）；没给就是框架自己发起的实测。
    """
    from shared.tools.library import latex

    ident = identity()
    started = time.monotonic()
    work = Path(tempfile.mkdtemp(prefix="hf-pdf-probe-")).resolve()
    try:
        _write_samples(work)
        absent = latex.absent_compilers()
        run = await latex.run_tex(state, work, "probe.tex", "xelatex", timeout)
        pdf = work / "probe.pdf"
        works = (run.status == "done" and run.returncode == 0 and pdf.is_file()
                 and latex._is_pdf_file(pdf))
        if works:
            status, reason = "works", ""
        elif len(absent) == len(latex._COMPILERS):
            status = "absent"
            reason = "这台机器上没有 LaTeX：" + "、".join(latex._COMPILERS) + " 都不在"
        else:
            status, reason = "broken", _reason(run, work, timeout)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    record = {
        "schema_version": 1,
        "works": works,
        "status": status,
        "reason": reason,
        "measured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "seconds": round(time.monotonic() - started, 1),
        "identity": ident,
    }
    _write_record(record)
    return record


async def ensure_measured(state: Any | None = None, *, timeout: int = 900) -> dict[str, Any]:
    """记录编过、且工具身份没变，就用它；否则重新实测（编不过的也重测：也许已经修好了）。"""
    record = read_record()
    if record and record.get("works") and record.get("identity") == identity():
        return record
    return await measure(state, timeout=timeout)


def describe(record: dict[str, Any] | None) -> str:
    """给系统提示与 doctor 的一行。不带时间戳：答案不变，这一行就不变。"""
    if not record:
        return (f"{WHAT}：这台机器上还没实测过（刚装好或工具链刚换过）；"
                "写论文开工时（write_brief）会实测一次，编译失败时也会。")
    ident = record.get("identity") or {}
    tools = ident.get("compiler") or "?"
    if ident.get("compiler") == "tectonic":
        tools += " + biber" if ident.get("biber") else "（没有 biber）"
    if record.get("works"):
        return f"{WHAT}：实测可用（{tools}）。"
    return (f"{WHAT}：实测**不可用**（{tools}）—— {record.get('reason') or '原因没留下'}。"
            "这不是稿件能解决的问题：需要 PDF 的交付（论文、报告）开工前先告诉用户，"
            "别写到最后一步才发现；可以先交付 LaTeX 源码。")


def describe_for_prompt() -> str:
    return describe(read_record())


def main(argv: list[str]) -> int:
    """``python -m shared.lib.pdf_toolchain {ensure|measure|show}`` —— 打印记录（JSON）。

    给人在命令行上查 / 重测用。平台自己**不在启动时**实测：个人版刚装好不该在用户开口
    之前联网（RFC X8），第一次实测在用户要 PDF 的时候（write_brief）。编过返回 0，编不过返回 2。
    """
    action = argv[1] if len(argv) > 1 else "show"
    if action == "show":
        print(json.dumps(read_record(), ensure_ascii=False))
        return 0
    if action not in {"ensure", "measure"}:
        print("usage: python -m shared.lib.pdf_toolchain {ensure|measure|show}", file=sys.stderr)
        return 64
    run = ensure_measured if action == "ensure" else measure
    record = asyncio.run(run())
    print(json.dumps(record, ensure_ascii=False))
    return 0 if record.get("works") else 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

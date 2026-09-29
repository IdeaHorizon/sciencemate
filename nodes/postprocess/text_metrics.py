"""字宽由**排版器**回答，不由布局自己估。

布局一直用经验系数估字宽（CJK 1.0 em / 拉丁 0.56 em），而真正排版的是
xelatex。两者不一致时，所有几何判据校验的都是那份估算 —— 「0 交叉 0 贴线」
的真实含义只是「我们的估算自洽」，不是纸上干净。

2026-09-17 实测（calibration-reference）：GPU 副标题 `RTX PRO 6000` 估 49.9pt、
纸上量出来 73.8pt（每字 0.92 em 而不是 0.56 em），两头压在盒线上 —— 而
`graze` / `collinear_overlaps` / `content_fill` 一条都没响，因为它们问的是
几何，几何问的是估算，估算就是那个错的人。

这正是图合同当初要治的病的同构复发：当年是「要求在文本 / 检查在像素 / 中间
不对账」，现在是「几何在估算 / 排版在渲染器 / 中间不对账」。修法一样：把两边
接到同一个真相源上 —— 让排版器量，布局用量出来的数。

量不到时（本机没有 TeX、或这次走 matplotlib 后端）退回估算，但退回这件事
会被如实记进几何里（`text_metrics.source`）。缺席不许长得像测过。

**量字宽的排版器 = 出图的排版器。** 两者都经 ``latex.run_tex``：同一个编译器（latexmk，
缺则随包 tectonic）、同一堵墙、同一个家。这里曾经自己 ``which("xelatex")``、在咽喉之外
``subprocess.run``：只有 tectonic 的机器上量不了、也画不了；两个都有 xelatex 的机器上，
量的和画的也可能是两套字体解析（系统 TeX Live 对 tectonic 的 bundle）。
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import tempfile
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
from pathlib import Path
from typing import Iterable

#: (文字, 字号) → 实测宽度（pt）。布局期间由 `measured()` 装上。
_TABLE: ContextVar[dict[tuple[str, float], float] | None] = ContextVar(
    "figure_text_metrics", default=None
)

#: 同一份 preamble + 同一批字串只量一次。测量道次要跑一次 TeX，八个基准
#: 各量一次就是八次 —— 这个进程内备忘把重复的那些省掉。
_MEMO: dict[str, dict[tuple[str, float], float]] = {}

_MEASURE_RE = re.compile(r"^AFSMW (\d+) ([0-9.]+)pt", re.M)

#: 单次测量道次的上限。量不完不算失败 —— 退回估算并说出来，
#: 但不允许它把一次渲染拖到分钟级。
MEASURE_TIMEOUT_S = 40.0


def lookup(text: str, font_size: float) -> float | None:
    """布局问一段文字有多宽。装了实测表就答实测值，否则答 None（调用方退回估算）。"""

    table = _TABLE.get()
    if not table:
        return None
    return table.get((str(text or ""), round(float(font_size), 2)))


class measured:
    """`with measured(table):` —— 这段里的字宽查询走实测表。"""

    def __init__(self, table: dict[tuple[str, float], float] | None) -> None:
        self._table = table
        self._token = None

    def __enter__(self) -> "measured":
        self._token = _TABLE.set(self._table or None)
        return self

    def __exit__(self, *_exc: object) -> None:
        if self._token is not None:
            _TABLE.reset(self._token)


def _compile_blocking(work: Path, timeout: float):
    """同步地跑一次 ``latex.run_tex``。

    布局是同步代码（declare / render / 印刷改法都从同步函数里调它），编译是异步的；而
    declare_figure 本身跑在事件循环里，``asyncio.run`` 不能在同一线程里嵌套 —— 所以在
    一个自己的线程里起一个循环跑完它。调用方阻塞的时长与原来的 ``subprocess.run`` 相同
    （≤ ``MEASURE_TIMEOUT_S``）。没有 run：量字宽不属于哪条 run，也就没有停止按钮。
    """

    from shared.tools.library import latex

    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="tex-measure") as pool:
        return pool.submit(
            asyncio.run, latex.run_tex(None, work, "measure.tex", "xelatex", timeout)
        ).result()


def measure_with_tex(
    items: Iterable[tuple[str, float]],
    preamble: str,
    *,
    escape,
) -> tuple[dict[tuple[str, float], float] | None, str]:
    """编一次测量文档，把每段文字的真实排版宽度量回来 —— 用出图的那个排版器。

    返回 `(表, 来源说明)`。量不成时表是 None，说明里写清楚为什么 —— 调用方把
    它记进几何，让「没量」和「量过且一致」在账上长得不一样。

    每段文字量两次（粗体与正常）取**大**的那个：查表时布局并不知道这段字将来
    是不是 `\\bfseries`（18 个调用点没有这个参数），而估宽了盒子略大是无害的，
    估窄了会挤 —— 与原估算器同一个安全方向。
    """

    from shared.tools.library import latex

    wanted = sorted({(str(text or ""), round(float(size), 2)) for text, size in items})
    wanted = [(text, size) for text, size in wanted if text.strip() and size > 0]
    if not wanted:
        return {}, "nothing to measure"

    if latex.no_tex_engine():
        return None, (
            "no TeX engine on this host (" + " / ".join(latex.absent_compilers())
            + " not found); box sizes are estimated, not measured"
        )
    compiler, binary = latex._resolve_compiler()

    key = hashlib.sha256(
        (binary + "\n--\n" + preamble + "\n--\n" + repr(wanted)).encode("utf-8")
    ).hexdigest()
    if key in _MEMO:
        return dict(_MEMO[key]), f"measured by {compiler} (cached)"

    body = [r"\newlength{\afsmw}", r"\begin{document}"]
    for index, (text, size) in enumerate(wanted):
        escaped = escape(text)
        for variant, series in ((0, r"\bfseries "), (1, "")):
            body.append(
                rf"\settowidth{{\afsmw}}{{\fontsize{{{size}}}{{{size * 1.2}}}"
                rf"\selectfont {series}{escaped}}}"
            )
            body.append(rf"\typeout{{AFSMW {index * 2 + variant} \the\afsmw}}")
    body.append(r"\null")
    body.append(r"\end{document}")

    document = "\n".join(
        [r"\documentclass[border=6pt]{standalone}", preamble, *body]
    )

    with tempfile.TemporaryDirectory(prefix="afs-measure-") as tmp:
        work = Path(tmp).resolve()
        (work / "measure.tex").write_text(document, encoding="utf-8")
        run = _compile_blocking(work, MEASURE_TIMEOUT_S)
        if run.status == "timeout":
            return None, (
                f"the {compiler} measuring pass did not finish in {MEASURE_TIMEOUT_S:.0f}s; "
                "box sizes are estimated, not measured"
            )
        log = (run.stdout + b"\n" + run.stderr).decode("utf-8", errors="replace")
        try:
            log += (work / "measure.log").read_text(encoding="utf-8", errors="replace")
        except OSError:
            pass

    found: dict[int, float] = {
        int(slot): float(width) for slot, width in _MEASURE_RE.findall(log)
    }
    if len(found) < len(wanted) * 2:
        # 半份实测比没有实测更危险：一部分盒子按真宽、一部分按估算，谁挤谁不挤
        # 说不清。要么整份，要么如实退回。
        return None, (
            f"the {compiler} measuring pass returned {len(found)}/{len(wanted) * 2} widths "
            f"({run.status}, rc={run.returncode}; it probably failed to compile); "
            "box sizes are estimated, not measured"
        )

    table = {
        (text, size): max(found[index * 2], found[index * 2 + 1])
        for index, (text, size) in enumerate(wanted)
    }
    _MEMO[key] = dict(table)
    return table, f"measured by {compiler}"

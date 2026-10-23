"""SVG → PNG 渲染（SVG 轨的渲染引擎，不直接面对 agent）。

## 选型（阶段 0 spike 定案，2026-10-15）

cairosvg：纯 wheel、run-local requirements 机制装（沙箱下载 → 离线安装两段
已内建，见 `shared.tools.library.python_exec`），系统 `libcairo.so.2` 在场，
实测 CJK / µ / 六属性标记全过；run-local 目录里的 pillow 还顺带绕过了宿主
环境的坏 PIL。备选链（svglib / resvg-py / rsvg-convert）未启用。

## 尺寸纪律

废弃 AutoFigure 的 1333×750 强拉伸：渲染宽度由绑定版心（`MEDIA`）按
300DPI 折算（84mm ≈ 992px，170mm ≈ 2008px），SVG 内部坐标系不变——
viewBox 是逻辑坐标，物理尺寸只决定栅格化分辨率。

## 试渲染

`trial_render` = 机械审计（`svg_audit`，进程内，便宜）+ 沙箱栅格化，
返回**轻量审计摘要**给 agent 在 mint 前迭代（方案 A 改进 C9：试渲染不占
渲染预算，铸造预算只在 `render_illustration` 里计）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .contracts import VisualContractError
from .figure_contract import MEDIA

#: 栅格化分辨率（印刷 300DPI）。
RENDER_DPI = 300

#: 沙箱里要装的渲染器（spike 实测版本；放宽到兼容范围）。
RENDER_REQUIREMENTS = ["cairosvg>=2.7,<3"]


def render_width_px(medium: str | None) -> int:
    """绑定版心 → 栅格化宽度（px）。未绑定按 double_column（默认版心）。"""

    spec = MEDIA.get(medium or "", MEDIA["double_column"])
    return max(1, round(spec["w"] / 25.4 * RENDER_DPI))


def _render_script(svg_bytes: bytes, out_path: Path, width_px: int) -> str:
    """沙箱里跑的那一段。SVG 以字节字面量嵌入 —— repr() 保一切转义。

    模型的字只在这段脚本的**数据**里，argv/代码由框架拼（与 render_figure
    的 TikZ 咽喉同一姿态）。

    渲染后接字形探针：cairosvg 对每个 text 元素只取 font-family 列表的
    **第一个**字体交给 cairo（text.py:54-66），不做逐字回退 —— 解析到的
    字体缺哪个字，哪个字就是豆腐块。探针用同一条 select_font_face 路径
    逐字查 text_to_glyphs 的 glyph index（0 = .notdef），这是渲染器自身的
    真相，不是字体栈的估计（2026-10-20 实测：同一个 ᵀ，regular 豆腐、
    bold 正常 —— fc-match 的答案与 cairo 实际解析不一致，只能问 cairo）。

    `background_color="white"`：SVG 声明的 width/height 物理比与 viewBox 比
    稍有出入时（0.19% 就够），cairosvg 按 preserveAspectRatio 默认行为把
    画面 letterbox 居中，边缘留下**透明**细带（2026-10-20 e2e 实测：170mm×
    200mm + viewBox 482×566，300DPI 下上下各 ~2px 透明）。科学图的底色本来
    就是白 —— 填白兜底，审计侧另有 SVG-ASPECT finding 点名叫作者对齐比率。
    """
    return (
        "import cairosvg\n"
        f"cairosvg.svg2png(bytestring={svg_bytes!r}, "
        f"write_to={str(out_path)!r}, output_width={int(width_px)}, "
        'background_color="white")\n'
        f"print({str(out_path)!r})\n"
        + _GLYPH_PROBE
        + f"\n_run_glyph_probe({svg_bytes!r})\n"
    )


#: 沙箱字形探针（跟在栅格化之后跑，结果打 TOFU_DETECTED 标记行上 stdout）。
_GLYPH_PROBE = '''
import json as _json
import xml.etree.ElementTree as _ET
import cairocffi as _cairo

_SVG_NS = "{http://www.w3.org/2000/svg}"
def _strip_ns(tag):
    return tag[len(_SVG_NS):] if tag.startswith(_SVG_NS) else tag

def _run_glyph_probe(svg_bytes):
    surface = _cairo.ImageSurface(_cairo.FORMAT_ARGB32, 1, 1)
    ctx = _cairo.Context(surface)
    bad = {}

    def walk(el, fam, weight, style):
        fam = el.get("font-family") or fam
        weight = el.get("font-weight") or weight
        style = el.get("font-style") or style
        if _strip_ns(el.tag) == "text":
            first = (fam or "sans-serif").split(",")[0].strip().strip(chr(34)).strip(chr(39)).strip()
            bold = str(weight or "").lower() in ("bold", "600", "700", "800", "900")
            w = _cairo.FONT_WEIGHT_BOLD if bold else _cairo.FONT_WEIGHT_NORMAL
            s = (_cairo.FONT_SLANT_ITALIC
                 if str(style or "").lower() in ("italic", "oblique")
                 else _cairo.FONT_SLANT_NORMAL)
            ctx.select_font_face(first, s, w)
            scaled = ctx.get_scaled_font()
            text = "".join(el.itertext())
            for ch in sorted({c for c in text if ord(c) > 0x7E}):
                glyphs, _clusters, _backwards = scaled.text_to_glyphs(0, 0, ch, True)
                if not glyphs or glyphs[0][0] == 0:
                    bad[ch] = f"{first}/{'bold' if bold else 'regular'}"
        for child in el:
            walk(child, fam, weight, style)

    walk(_ET.fromstring(svg_bytes.decode("utf-8")), None, None, None)
    if bad:
        print("TOFU_DETECTED " + _json.dumps(bad, ensure_ascii=False))
'''


def _extract_tofu(stdout_text: str) -> dict[str, str]:
    """从渲染脚本 stdout 里捡 TOFU_DETECTED 标记行（{字符: 解析到的字体/weight}）。"""
    for line in (stdout_text or "").splitlines():
        if line.startswith("TOFU_DETECTED "):
            try:
                import json as _json

                payload = _json.loads(line[len("TOFU_DETECTED "):])
                return payload if isinstance(payload, dict) else {}
            except Exception:
                return {}
    return {}


async def render_svg_png(
    state: Any,
    svg_text: str,
    out_path: Path,
    *,
    medium: str | None = None,
    timeout: int = 300,
) -> dict[str, Any]:
    """把 SVG 栅格化成 PNG（沙箱执行）。返回执行信封 + 文件事实。"""

    from shared.tools.library import python_exec

    out_path.parent.mkdir(parents=True, exist_ok=True)
    width_px = render_width_px(medium)
    execution = await python_exec._execute_python(
        state,
        code=_render_script(svg_text.encode("utf-8"), out_path, width_px),
        timeout=timeout,
        requirements=list(RENDER_REQUIREMENTS),
    )
    if execution.get("status") != "success":
        return {
            "status": "error",
            "error": execution.get("error") or "cairosvg render did not exit cleanly",
            "execution": execution,
            "width_px": width_px,
        }
    if not out_path.is_file() or out_path.stat().st_size == 0:
        return {
            "status": "error",
            "error": f"cairosvg exited cleanly but {out_path.name} was not produced",
            "execution": execution,
            "width_px": width_px,
        }
    result: dict[str, Any] = {
        "status": "success",
        "png_path": str(out_path),
        "png_bytes": out_path.stat().st_size,
        "width_px": width_px,
        "execution": execution,
    }
    tofu = _extract_tofu(execution.get("stdout_tail") or "")
    if tofu:
        # 字形缺失不是渲染失败（像素出来了），是内容判决 —— 交给铸造门拒，
        # 不走环境通道（环境通道不扣预算；选错字是作者的内容问题，该扣）。
        result["tofu"] = tofu
    return result


async def trial_render(
    state: Any,
    svg_text: str,
    out_path: Path,
    *,
    contract: dict[str, Any] | None = None,
    policy: dict[str, Any] | None = None,
    medium: str | None = None,
    enforce_print: bool = False,
    timeout: int = 300,
) -> dict[str, Any]:
    """试渲染：机械审计 + 栅格化 + 轻量摘要。**不占铸造预算**，mint 前迭代用。

    摘要刻意压扁（计数不是清单）——它是给 agent 的方向感，详单在 findings 里。
    """

    from .svg_audit import audit_svg

    try:
        audit = audit_svg(
            svg_text,
            contract=contract,
            policy=policy,
            medium=medium,
            enforce_print=enforce_print,
        )
    except VisualContractError as exc:
        return {"status": "error", "error": str(exc), "error_code": "audit_rejected"}

    render = await render_svg_png(state, svg_text, out_path, medium=medium, timeout=timeout)
    if render.get("status") != "success":
        return {**render, "audit": audit}

    report = {
        "status": "success",
        "png_path": render["png_path"],
        "png_bytes": render["png_bytes"],
        "width_px": render["width_px"],
        "observed": audit["observed"],
        "audit_summary": audit["summary"],
        "audit_findings": audit["findings"],
        "note": "试渲染：没有铸任何记录、没占渲染预算。定稿后用 "
        "render_illustration 铸造。",
    }
    if render.get("tofu"):
        # 渲染器实测有字符成了豆腐块 —— 试渲染阶段就摆出来，别等铸造门拒。
        report["tofu"] = render["tofu"]
        report["audit_findings"] = [
            *report["audit_findings"],
            {
                "collector": "SVG-GLYPH",
                "message": (
                    "characters that render as tofu boxes with the fonts cairo "
                    "actually resolves (first font-family entry only, no per-glyph "
                    "fallback): "
                    + "; ".join(f"{ch!r} via {where}" for ch, where in list(render["tofu"].items())[:8])
                ),
            },
        ]
    return report

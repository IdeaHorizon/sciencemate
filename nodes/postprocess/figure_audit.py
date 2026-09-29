"""图像级机械审计 —— 渲染方式无关的恒跑证人（判决拆除 B 刀）。

按「检查存留三门槛」清洗后只剩五类检查，全部满足：机械事实 + 文本模型自己
看不见 + 对账本真实性或可复现性有后果：

1. **文件有效性**：交付文件必须真的解析得开（PNG/PDF/SVG 头与像素）；
2. **空白渲染**：整张图像素全同 = 渲染静默失败（rc=0 也可能全白）；
3. **文字出界/碰撞**：最终尺寸下的 text box 几何（savefig 时刻在沙箱内测量）；
4. **分辨率**：publication 用途的位图 DPI 低于出版下限；
5. **字体豆腐块**：matplotlib 的 Glyph missing / findfont 信号（由
   execute_python 的 envelope 抬出，这里只归账）。

全部结果落 findings —— 没有任何拒绝分支；改不改归 agent，终审归 referee。

## 沙箱内几何审计的做法

agent 在沙箱里写任意 matplotlib 代码；工具在代码前注入一段**自足**的
preamble（不 import 本仓库 —— 沙箱里未必挂载得到），monkeypatch
``Figure.savefig``：每次保存时用当次 renderer 量所有可见 text artist 的
window extent，判出界与两两碰撞，连同 canvas 尺寸/DPI 写进 sidecar JSON。
代码不用 matplotlib（比如直接写 SVG）时 sidecar 自然为空 —— 审计记
``text_geometry: not_attached``（缺席事实，不是 warning）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

#: publication 用途位图的出版下限（Nature/PLOS 均要求 ≥300 DPI 位图）。
PUBLICATION_MIN_RASTER_DPI = 300

_RASTER_SUFFIXES = {".png", ".jpg", ".jpeg", ".tif", ".tiff"}


def audit_preamble(sidecar_path: str) -> str:
    """自足的沙箱内几何审计 preamble。

    只依赖标准库 + matplotlib 自身；任何一步失败都吞掉（审计崩了不许拖死
    渲染），但 attach 失败的事实会体现在 sidecar 缺条目上。
    """
    return (
        "import atexit as _fa_atexit, json as _fa_json\n"
        f"_FA_SIDECAR = {sidecar_path!r}\n"
        "_fa_records = []\n"
        "def _fa_candidates(fig):\n"
        "    # 只量**真的会画出来**的文字：findobj(Text) 会捞到视野外被裁剪的\n"
        "    # tick label（matplotlib 不渲染它们，但对象存在），把它们当出界=假阳性。\n"
        "    out = list(fig.texts)\n"
        "    for ax in fig.axes:\n"
        "        out.append(ax.title)\n"
        "        out.extend(ax.texts)\n"
        "        if not ax.axison:\n"
        "            continue\n"
        "        out.extend([ax.xaxis.label, ax.yaxis.label,\n"
        "                    ax.xaxis.get_offset_text(), ax.yaxis.get_offset_text()])\n"
        "        x_min, x_max = sorted(float(v) for v in ax.get_xlim())\n"
        "        y_min, y_max = sorted(float(v) for v in ax.get_ylim())\n"
        "        out.extend(lab for loc, lab in zip(ax.get_xticks(), ax.get_xticklabels())\n"
        "                   if x_min <= float(loc) <= x_max)\n"
        "        out.extend(lab for loc, lab in zip(ax.get_yticks(), ax.get_yticklabels())\n"
        "                   if y_min <= float(loc) <= y_max)\n"
        "        legend = ax.get_legend()\n"
        "        if legend is not None:\n"
        "            out.append(legend.get_title())\n"
        "            out.extend(legend.get_texts())\n"
        "    seen, unique = set(), []\n"
        "    for artist in out:\n"
        "        if artist is not None and id(artist) not in seen:\n"
        "            seen.add(id(artist)); unique.append(artist)\n"
        "    return unique\n"
        "def _fa_audit(fig, fname):\n"
        "    rec = {'output': str(fname)}\n"
        "    try:\n"
        "        fig.canvas.draw()\n"
        "        fig.canvas.draw()  # constrained layout 首次 draw 后才定型\n"
        "        renderer = getattr(fig.canvas, 'get_renderer', lambda: None)()\n"
        "        w_px, h_px = fig.canvas.get_width_height()\n"
        "        boxes = []\n"
        "        sizes = []\n"
        "        for artist in _fa_candidates(fig):\n"
        "            try:\n"
        "                if not artist.get_visible() or not str(artist.get_text()).strip():\n"
        "                    continue\n"
        "                bb = artist.get_window_extent(renderer=renderer)\n"
        "                if bb.width <= 0 or bb.height <= 0:\n"
        "                    continue\n"
        "                boxes.append((bb.x0, bb.y0, bb.x1, bb.y1,\n"
        "                              str(artist.get_text())[:60]))\n"
        # 字号跟着每段文字一起记：数据图的印刷字号判据（合同的 medium × figsize）
        # 读的就是这个 —— 不然「刻度 4pt」只有肉眼看得见。
        "                sizes.append((float(artist.get_fontsize()),\n"
        "                              str(artist.get_text())[:60]))\n"
        "            except Exception:\n"
        "                continue\n"
        "        tol = 1.0\n"
        "        outside = [b[4] for b in boxes\n"
        "                   if b[0] < -tol or b[1] < -tol\n"
        "                   or b[2] > w_px + tol or b[3] > h_px + tol]\n"
        "        collisions = []\n"
        "        for i in range(len(boxes)):\n"
        "            for j in range(i + 1, len(boxes)):\n"
        "                a, b = boxes[i], boxes[j]\n"
        "                ox = min(a[2], b[2]) - max(a[0], b[0])\n"
        "                oy = min(a[3], b[3]) - max(a[1], b[1])\n"
        "                if ox > 1.0 and oy > 1.0:\n"
        "                    collisions.append([a[4], b[4]])\n"
        "        rec['text_geometry'] = {\n"
        "            'attached': True,\n"
        "            'canvas_px': [int(w_px), int(h_px)],\n"
        "            'dpi': float(fig.dpi),\n"
        "            'size_inches': [float(v) for v in fig.get_size_inches()],\n"
        "            'text_count': len(boxes),\n"
        "            'outside_count': len(outside),\n"
        "            'outside_samples': outside[:10],\n"
        "            'collision_count': len(collisions),\n"
        "            'collision_samples': collisions[:10],\n"
        "            'min_font_pt': min((s for s, _t in sizes), default=None),\n"
        "            'min_font_sample': next((t for s, t in sorted(sizes, key=lambda p: p[0])), None),\n"
        "            'texts': [b[4] for b in boxes][:200],\n"
        "        }\n"
        "    except Exception as exc:\n"
        "        rec['text_geometry'] = {'attached': False, 'error': str(exc)[:200]}\n"
        # ── 对象模型（合同核查的读数端）────────────────────────────────
        # 「查对象模型不查像素」：面板数、每个面板的序列数、坐标轴刻度类型，
        # 全部从 matplotlib 自己的对象树上读，不靠看图也不靠模型判断。这是
        # asserted 家族（统计图）能被机械核对的全部依据。
        "    try:\n"
        "        from matplotlib.colors import to_hex as _fa_hex\n"
        "        w_px, h_px = fig.canvas.get_width_height()\n"
        "        renderer = getattr(fig.canvas, 'get_renderer', lambda: None)()\n"
        "        def _fa_legend(owner, legend):\n"
        "            entry = {'owner': owner,\n"
        "                     'labels': [str(t.get_text()) for t in legend.get_texts()]}\n"
        "            try:\n"
        "                bb = legend.get_window_extent(renderer=renderer)\n"
        "                entry['inside'] = bool(bb.x0 >= -1.0 and bb.y0 >= -1.0\n"
        "                                       and bb.x1 <= w_px + 1.0 and bb.y1 <= h_px + 1.0)\n"
        "            except Exception:\n"
        "                entry['inside'] = None\n"
        "            return entry\n"
        "        legends = [_fa_legend('figure', lg) for lg in getattr(fig, 'legends', []) or []]\n"
        "        colours = []\n"
        "        labeled = []\n"
        "        panels = []\n"
        "        for ax in fig.axes:\n"
        "            if str(ax.get_label() or '') == '<colorbar>':\n"
        "                continue  # colorbar 不是面板\n"
        "            containers = len(getattr(ax, 'containers', []) or [])\n"
        "            if ax.get_legend() is not None:\n"
        "                legends.append(_fa_legend('axes', ax.get_legend()))\n"
        "            try:\n"
        "                labeled.extend(str(l) for l in ax.get_legend_handles_labels()[1])\n"
        "            except Exception:\n"
        "                pass\n"
        # 序列颜色：线 / 柱 / 无色标映射的集合。有 array（cmap 映射）的集合与
        # 图像不算 —— 那是数据在选色，不是作者。
        "            try:\n"
        "                for ln in ax.lines:\n"
        "                    if ln.get_visible() and len(ln.get_xdata()) > 0:\n"
        "                        colours.append(_fa_hex(ln.get_color()))\n"
        "                for c in getattr(ax, 'containers', []) or []:\n"
        "                    patches = [p for p in getattr(c, 'patches', []) or [] if p.get_visible()]\n"
        "                    if patches:\n"
        "                        colours.append(_fa_hex(patches[0].get_facecolor()))\n"
        "                for col in ax.collections:\n"
        "                    if not col.get_visible() or col.get_array() is not None:\n"
        "                        continue\n"
        "                    fc = col.get_facecolor()\n"
        "                    if len(fc):\n"
        "                        colours.append(_fa_hex(fc[0]))\n"
        "            except Exception:\n"
        "                pass\n"
        "            panels.append({\n"
        "                'label': str(ax.get_label() or ''),\n"
        "                'title': str(ax.get_title() or ''),\n"
        "                'xlabel': str(ax.get_xlabel() or ''),\n"
        "                'ylabel': str(ax.get_ylabel() or ''),\n"
        "                'line_count': len([ln for ln in ax.lines\n"
        "                                   if ln.get_visible()\n"
        "                                   and len(ln.get_xdata()) > 0]),\n"
        "                'collection_count': len([c for c in ax.collections\n"
        "                                         if c.get_visible()]),\n"
        "                'container_count': containers,\n"
        "                'image_count': len(ax.images),\n"
        "                'legend_labels': [str(t.get_text()) for t in\n"
        "                                  (ax.get_legend().get_texts()\n"
        "                                   if ax.get_legend() is not None else [])],\n"
        "                'xscale': str(ax.get_xscale()),\n"
        "                'yscale': str(ax.get_yscale()),\n"
        "                'has_errorbars': any(getattr(c, 'has_xerr', False)\n"
        "                                     or getattr(c, 'has_yerr', False)\n"
        "                                     for c in getattr(ax, 'containers', []) or []),\n"
        "            })\n"
        "        rec['object_model'] = {'attached': True, 'panels': panels,\n"
        "                               'legends': legends,\n"
        "                               'labeled_series': labeled,\n"
        "                               'series_colors': sorted(set(colours)),\n"
        "                               'size_pt': [float(v) * 72.0 for v in fig.get_size_inches()]}\n"
        "    except Exception as exc:\n"
        "        rec['object_model'] = {'attached': False, 'error': str(exc)[:200]}\n"
        "    _fa_records.append(rec)\n"
        "try:\n"
        "    from matplotlib.figure import Figure as _FA_Figure\n"
        "    _fa_orig_savefig = _FA_Figure.savefig\n"
        "    def _fa_savefig(self, fname, *args, **kwargs):\n"
        "        result = _fa_orig_savefig(self, fname, *args, **kwargs)\n"
        "        try:\n"
        "            _fa_audit(self, fname)\n"
        # 分辨率要记**这次保存**用的 dpi，不是 fig.dpi（默认 100）：iter11 的代码
        # 明明 savefig(dpi=300)，审计却四次报「100 DPI」—— 报的是一个没人用的数。
        "            try:\n"
        "                import matplotlib as _fa_mpl\n"
        "                _d = kwargs.get('dpi', _fa_mpl.rcParams.get('savefig.dpi', 'figure'))\n"
        "                _fa_records[-1]['text_geometry']['dpi'] = float(self.dpi if _d in (None, 'figure') else _d)\n"
        "            except Exception:\n"
        "                pass\n"
        "        except Exception:\n"
        "            pass\n"
        "        return result\n"
        "    _FA_Figure.savefig = _fa_savefig\n"
        "except Exception:\n"
        "    pass\n"
        "def _fa_flush():\n"
        "    try:\n"
        "        with open(_FA_SIDECAR, 'w') as fh:\n"
        "            _fa_json.dump(_fa_records, fh)\n"
        "    except Exception:\n"
        "        pass\n"
        "_fa_atexit.register(_fa_flush)\n"
    )


def read_sidecar(sidecar: Path) -> list[dict[str, Any]]:
    try:
        payload = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return [item for item in payload if isinstance(item, dict)] if isinstance(payload, list) else []


def _validate_image_file(path: Path) -> tuple[dict[str, Any], str | None]:
    """(facts, problem) —— facts 恒有，problem 仅在文件解析不开/空白时给。"""
    suffix = path.suffix.lower()
    facts: dict[str, Any] = {"format": suffix.lstrip("."), "size_bytes": path.stat().st_size}
    if facts["size_bytes"] == 0:
        return facts, "file is empty (0 bytes)"
    if suffix in _RASTER_SUFFIXES:
        try:
            from PIL import Image

            with Image.open(path) as image:
                image.load()
                facts["pixel_size"] = [int(image.width), int(image.height)]
                extrema = image.convert("L").getextrema()
                facts["luminance_extrema"] = [int(extrema[0]), int(extrema[1])]
                if extrema[0] == extrema[1]:
                    return facts, "raster image is entirely one value (blank render)"
        except ImportError:
            facts["pixel_validation"] = "unavailable (PIL not installed)"
        except Exception as exc:  # noqa: BLE001 —— 解析失败正是要报告的事实
            return facts, f"raster file failed to parse: {exc}"
    elif suffix == ".pdf":
        head = path.open("rb").read(5)
        if head != b"%PDF-":
            return facts, "file does not start with a PDF header"
    elif suffix == ".svg":
        head = path.open("rb").read(4096).lstrip()
        if b"<svg" not in head and b"<?xml" not in head:
            return facts, "file does not look like SVG/XML"
    elif suffix == ".eps":
        head = path.open("rb").read(4)
        if head not in (b"%!PS", b"\xc5\xd0\xd3\xc6"):
            return facts, "file does not start with an EPS header"
    return facts, None


def audit_figure_outputs(
    *,
    output_paths: dict[str, Path],
    sidecar_records: list[dict[str, Any]],
    publication_grade: bool,
    glyph_warning: str | None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """恒跑图像级审计：返回 (findings, audit_facts)。

    audit_facts 恒被写进 figure 记录（``ran: True`` + 每文件事实）——
    「审计缺席」与「审计跑了没发现问题」必须可区分（没执行 ≈ 执行了没效果
    是同一类病，判据落在记录形状上）。
    """
    findings: list[dict[str, Any]] = []
    per_file: dict[str, dict[str, Any]] = {}
    geometry_by_name = {Path(str(rec.get("output"))).name: rec for rec in sidecar_records}

    for fmt, path in output_paths.items():
        facts, problem = _validate_image_file(path)
        per_file[fmt] = facts
        if problem:
            findings.append(
                {
                    "collector": "OB-FILE-VALIDITY",
                    "file": path.name,
                    "message": problem,
                }
            )
        geometry = (geometry_by_name.get(path.name) or {}).get("text_geometry")
        if isinstance(geometry, dict) and geometry.get("attached"):
            facts["text_geometry"] = geometry
            if geometry.get("outside_count"):
                findings.append(
                    {
                        "collector": "OB-TEXT-BOUNDS",
                        "file": path.name,
                        "message": (
                            f"{geometry['outside_count']} text element(s) extend past the "
                            "canvas at final size"
                        ),
                        "samples": geometry.get("outside_samples") or [],
                    }
                )
            if geometry.get("collision_count"):
                findings.append(
                    {
                        "collector": "OB-TEXT-COLLISION",
                        "file": path.name,
                        "message": (
                            f"{geometry['collision_count']} pair(s) of text elements overlap "
                            "at final size"
                        ),
                        "samples": geometry.get("collision_samples") or [],
                    }
                )
            dpi = geometry.get("dpi")
            if (
                publication_grade
                and path.suffix.lower() in _RASTER_SUFFIXES
                and isinstance(dpi, int | float)
                and dpi < PUBLICATION_MIN_RASTER_DPI
            ):
                findings.append(
                    {
                        "collector": "OB-RESOLUTION",
                        "file": path.name,
                        "message": (
                            f"raster output saved at {dpi:g} DPI; publication venues "
                            f"require >= {PUBLICATION_MIN_RASTER_DPI} DPI"
                        ),
                    }
                )
        else:
            facts["text_geometry"] = {"attached": False}

    if glyph_warning:
        findings.append(
            {
                "collector": "OB-GLYPH",
                "message": glyph_warning,
            }
        )

    audit_facts = {
        "ran": True,
        "kind": "image_mechanical_audit",
        "files": per_file,
        "text_geometry_attached": any(
            isinstance(f.get("text_geometry"), dict) and f["text_geometry"].get("attached")
            for f in per_file.values()
        ),
    }
    return findings, audit_facts


def observed_object_model(sidecar_records: list[dict[str, Any]]) -> dict[str, Any]:
    """sidecar → 合同断言求值用的读数。

    「查对象模型不查像素」的读数端：asserted 家族（统计图）的图是 agent 写
    代码画的，所以必须真的去读 matplotlib 的对象树，而不是相信声明。取最后
    一次 savefig 的快照 —— 那才是交付的那张图。

    代码没用 matplotlib（例如直接写 SVG）时自然没有 object_model：返回
    ``attached=False``，由调用方如实记「对象模型未采到」而不是判失败。
    """

    snapshot: dict[str, Any] | None = None
    geometry: dict[str, Any] | None = None
    for record in sidecar_records:
        model = record.get("object_model")
        if isinstance(model, dict) and model.get("attached"):
            snapshot = model
            text = record.get("text_geometry")
            geometry = text if isinstance(text, dict) and text.get("attached") else None
    if snapshot is None:
        return {"attached": False}
    panels = [item for item in snapshot.get("panels") or [] if isinstance(item, dict)]
    # 序列 = 读者在图例里分得开的东西。有标签的艺术家在场时按**不同标签的个数**
    # 数（4 面板 × 4 组 × 6 根堆叠柱是 192 个 container、2 条序列 —— iter11 就是
    # 被 192 拒了四次，模型只好把 series 从合同里删掉）；一个标签都没有时退回
    # 数艺术家（两条没标签的 ax.plot 仍然是两条序列）。
    labeled = [str(item).strip() for item in snapshot.get("labeled_series") or [] if str(item).strip()]
    series_total = (
        len(set(labeled))
        if labeled
        else sum(
            int(panel.get("line_count") or 0)
            + int(panel.get("collection_count") or 0)
            + int(panel.get("container_count") or 0)
            for panel in panels
        )
    )
    # 轴刻度：单面板直接给；多面板只在全体一致时给一个值，否则留 None ——
    # 「多个面板各不相同」不该被折算成某一个面板的值（两个视图两个问题）。
    axis_scales: dict[str, Any] = {}
    for axis, key in (("x", "xscale"), ("y", "yscale")):
        values = {str(panel.get(key)) for panel in panels if panel.get(key)}
        axis_scales[axis] = values.pop() if len(values) == 1 else None
    return {
        "attached": True,
        "panel_count": len(panels),
        "series_count": series_total,
        "axis_scales": axis_scales,
        "panels": panels,
        "legends": [item for item in snapshot.get("legends") or [] if isinstance(item, dict)],
        "labeled_series": sorted(set(labeled)),
        "series_colors": list(snapshot.get("series_colors") or []),
        "size_pt": list(snapshot.get("size_pt") or []),
        "min_font_pt": (geometry or {}).get("min_font_pt"),
        "min_font_sample": (geometry or {}).get("min_font_sample"),
        "texts": list((geometry or {}).get("texts") or []),
    }

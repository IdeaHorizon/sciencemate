---
name: generic-quantitative-figure
description: Select and construct truthful general-purpose, diagnostic, genomic, enrichment, and event-based scientific charts from already-clean or explicitly display-ready quantitative tables when no narrower domain skill is a better match.
---

# Generic Quantitative Figure

## Choose by visual question

- Trend over an ordered variable: line, optionally with observed markers.
- Relationship between continuous variables: scatter; use a line only when the order or model has upstream meaning.
- Comparison of discrete categories: points, intervals, or bars when magnitude from a meaningful baseline is the message.
- Distribution: histogram, box, or violin; include raw observations when feasible.
- Two-dimensional matrix: heatmap with a declared matrix contract, color-scale meaning, labeled coordinates, missing-cell encoding, and a scientifically meaningful center only when explicitly supplied.
- Diagnostic/domain coordinates: use the native MA, Manhattan, QQ, residual, enrichment-dot,
  event-raster, or density-ridge grammar only when its named upstream semantic contract is present.

Avoid a bar chart for continuous trends, a line for unordered categories, a pie chart for fine comparison, and unlabelled error bars. Preserve rather than summarize existing row-level data unless the supplied artifact already contains aggregate fields.

## Construction

Specify x/y/color/shape/facet bindings, units, scale types, category order, uncertainty fields, and legend order in the plan. Use a restrained accessible palette, exact physical size, and vector output where appropriate. Inspect the rendered result for clipping, occlusion, density, and misleading scale behavior.

For a vague request, a diagnostic/statistical plot, or a named specialist chart, load
`references/chart-selection.md` before planning. It gives role requirements and stop conditions;
it does not imply that the active backend implements every listed form.

## 语义自查清单（渲染后逐条过）

- **基线诚实**：bar/area 类从 0 开始，否则在轴标签/caption 显式披露截断基线；scatter/line 的非零轴限是常态，不用假装从 0。
- **尺度披露**：log 轴必须由轴标签与刻度自明；双轴图要有强理由并双侧着色对应。
- 上游给了不确定性（区间/std/CI）就画出来并在图例声明语义；上游没给绝不自己算。
- 分组身份用颜色**加**冗余编码（marker/线型）；色板色盲安全；禁用 rainbow/jet。
- 过绘制（点云糊成一团）用透明度/密度表达处理，绝不删点、不抖动科学坐标。

## 代码起手式（可选便利，不是通道）

`nodes.postprocess.figure_helpers` 提供期刊栏宽（mm）、Okabe-Ito 色盲安全
色板、CJK 字体栈与 `apply_publication_defaults(plt)`；毫米转 figsize 用
`figsize_mm(width_mm, height_mm)`。

## 嵌套参数扫描网格（来自 2026-09-01 真实 E2E 教训，原 PR#753）

参数扫描的 `clean_results` 常是 N 维嵌套 dict（`results[组][κ][σ][T][指标]`），
不是表。旧管线曾因「requires a table-like source」三连拒让整篇论文交不出图。
现在：**你自己在绘图代码里无损展平**（不聚合、不剔除、不插补）：

```python
def flatten(node, keys=()):
    if all(not isinstance(v, dict) for v in node.values()):
        yield {**{f"level_{i}": k for i, k in enumerate(keys)}, **node}
    else:
        for k, v in node.items():
            yield from flatten(v, keys + (k,))
df = pd.DataFrame(flatten(nested))
```
层名（组/κ/σ/T）按上游 metadata 命名列；参差不齐的层如实报告，别猜。

---
name: publication-accessibility
description: Apply publication-grade layout, typography, export, color-accessibility, and legibility constraints to scientific visual assets.
---

# Publication Accessibility

## Art direction

Prefer restrained, information-dense design: clear hierarchy, consistent spacing, deliberate typography, aligned panels, light structural guides, and minimal ornamental ink. “Premium” means precise and coherent, not glossy decoration.

## Accessibility and export

- Use colorblind-safe palettes and redundant encodings such as line style, marker, label, or texture when identity matters.
- Maintain readable type, strokes, symbols, and annotations at the final physical size.
- Avoid rainbow palettes for ordered data unless the domain convention explicitly requires one and the limitations are documented.
- Export exact requested dimensions. Do not use automatic tight cropping that changes physical size.
- Prefer vector PDF/SVG for charts and schematics; use lossless raster output for scientific images.
- Verify contrast, clipping, panel labels, legend order, and grayscale distinguishability.

Journal-specific rules are request inputs. Do not assume a venue template that was not supplied.

## 代码便利

色盲安全色板与出版默认（字号/线宽/DPI/字体栈）在
`nodes.postprocess.figure_helpers`（`COLORBLIND_SAFE` /
`apply_publication_defaults`），普通库函数，可用可不用。

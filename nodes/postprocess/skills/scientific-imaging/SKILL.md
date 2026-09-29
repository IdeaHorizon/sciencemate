---
name: scientific-imaging
description: Produce microscopy, medical, astronomy, remote-sensing, and other scientific image panels with immutable raw data and explicit display-transform provenance.
---

# Scientific Imaging

Keep source pixels immutable. Record every crop, rotation, flip, channel selection, lookup table, contrast window, gamma, and resize as an image-transform artifact. Distinguish display transforms from scientific preprocessing.

Preserve bit depth and dynamic-range semantics until export. Never burn in a scale bar unless pixel size and units are supplied. Use identical windows and color limits for panels intended for quantitative comparison. Mark saturation, masks, and missing pixels honestly.

Annotations may identify supplied structures but must not obscure evidence. Use lossless raster output for pixel data; use vector overlays when possible. A generative image model is prohibited for evidence-bearing scientific images.

Load `references/image-display-contracts.md` for microscopy, radiology, astronomy,
multichannel, inset, montage, or cross-panel intensity-window decisions.

## 语义自查清单（渲染后逐条过）

- 16-bit/float 图像必须显式声明 display window（vmin/vmax）；window 只影响显示，在 caption/findings 里记录截断比例，原始位深与文件 hash 不变。
- label/mask/segmentation 是分类数据：用 categorical LUT，绝不用连续 colormap 或连续 window。
- 有物理标定（μm/px 等）就画 scale bar 并标注；没有标定不要编。
- 多 panel 图像显示参数（window/gamma/colormap）跨 panel 一致，不一致时逐 panel 标注。
- 亮暗区域的可见结构丢失（过曝/欠曝）要么调 window 修掉，要么在 caption 里承认。

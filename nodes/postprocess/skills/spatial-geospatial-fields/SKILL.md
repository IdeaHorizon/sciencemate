---
name: spatial-geospatial-fields
description: Plan maps and scalar or vector field visualizations with explicit coordinates, projection, normalization, masks, and domain-appropriate spatial conventions.
---

# Spatial and Geospatial Fields

Require coordinate semantics, units, extent, orientation, and—when geographic—a coordinate reference system or projection. Do not treat row and column indices as physical coordinates unless declared.

Choose sequential color for ordered magnitude, diverging color around a scientifically meaningful center, cyclic color for phase or direction, and categorical color only for classes. State normalization and shared limits when comparing panels. Preserve masks and missing regions distinctly from zero.

Maps must not imply unsupported precision or distort area/distance without noting the projection tradeoff. Vector fields need scale and sampling semantics. Contours, interpolation, regridding, and derived gradients require upstream results or an explicitly lineage-recorded display-only operation that does not alter scientific interpretation.

Load `references/fields-and-maps.md` for projection, scalar/vector field, contour,
phase/cyclic, mask, orientation, and shared-normalization decisions.

The native `xarray_cartopy` path accepts NetCDF/Zarr geophysical grids only with
explicit latitude/longitude or projected x/y bindings, field units, calendar,
projection, and time/vertical/member selections. It uses exact source cells;
interpolation, smoothing, and regridding remain false and are mechanically audited.
Curvilinear WRF-style coordinates may carry a time dimension; the declared selector
is applied identically to the scalar field, latitude, longitude, and any colocated
vector components. Staggered U/V grids must be destaggered upstream with lineage.
Display-only arrow thinning is allowed only through an explicit integer stride and
the retained count is written to the render manifest.

## 语义自查清单（渲染后逐条过）

- 字段单位、坐标系（CRS/晶格）、时间步来自上游 metadata；transient 数据必须显式选 time index/value，多 block 数据必须显式选 block —— 缺了就 request_upstream_rework，不猜。
- 相机参数/切片位置/等值面阈值写在渲染代码里（可复现），caption 说明选择理由。
- **矢量诚实**：平面切片/流线/箭头可导出 true SVG/PDF；体渲染、粒子云、非平面三维场是位图 —— 交付高 DPI PNG/TIFF，不要用矢量后缀包装位图。
- colorbar 有单位与范围；发散场用发散色板且中心对准物理零点；禁用 rainbow/jet。

# Publication layout system

Start from final physical size. Allocate panel area by information density, not equal rectangles by
default. Define outer margins, gutters, alignment anchors, panel order and shared guides in millimetres.

- Align comparable plot areas, baselines and colorbars rather than image bounding boxes.
- Use shared axes/legends only when scales and semantics truly match.
- Keep panel labels in stable top-left anchors, outside evidence where possible, and follow the active
  venue case/size rule.
- Reserve a dedicated label band when a label would otherwise cover marks, microscopy pixels,
  annotations, legends or axes. Measure its type size in points at the final physical dimensions.
- Normalize font family, optical text size, stroke weight and semantic colors across heterogeneous
  backends.
- Preserve each panel aspect ratio when it has scientific meaning. Never stretch microscopy, maps or
  molecular scenes to fill a cell.
- Compose once in SVG coordinates measured against the final millimetre canvas. Inline SVG components
  with isolated ID namespaces; embed raster panels losslessly. Derive PNG/TIFF/PDF/EPS from that same
  layout source so panel geometry cannot drift between formats.
- Record vector and raster component counts. An editable wrapper does not make embedded raster evidence
  vector, and a publication package must not claim otherwise.
- Inspect every panel at final size, then inspect reading order, whitespace balance and cross-panel
  consistency on the composition. A changed panel invalidates the composition review.

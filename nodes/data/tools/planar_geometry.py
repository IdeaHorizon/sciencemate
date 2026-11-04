"""Native construction and independent checks for an explicit planar primitive.

Coordinates and mesh sizes share the caller's consistent length unit. Supplied
CAD and arbitrary shapes are deliberately outside this primitive's contract.
"""
from __future__ import annotations

import math
from typing import Any


def circular_plate_geometry(bindings: dict[str, Any]) -> dict[str, Any] | None:
    geometry = bindings.get("geometry")
    if not isinstance(geometry, dict) or geometry.get("type") != "rectangle_with_circular_hole":
        return None
    def number(*keys):
        value = next((geometry[key] for key in keys if key in geometry), None)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f"Planar geometry needs a finite numeric {keys[0]} in consistent units")
        return float(value)
    length, width = number("length_x", "length"), number("width_y", "width")
    radius = number("hole_radius")
    center = geometry.get("hole_center")
    if (not isinstance(center, (list, tuple)) or len(center) != 2
            or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in center)):
        raise ValueError("Planar geometry needs an explicit numeric XY hole_center")
    cx, cy = center
    if not (radius > 0 and radius < cx < length-radius and radius < cy < width-radius):
        raise ValueError("The circular hole must lie strictly inside the positive rectangle")
    return dict(length=length, width=width, radius=radius, cx=cx, cy=cy)


def render_planar_geometry(bindings: dict[str, Any]) -> str | None:
    shape = circular_plate_geometry(bindings)
    if shape is None:
        return None
    if bindings.get("mesh_dimension", 2) != 2:
        return None
    if set(bindings["geometry"]) - {"type", "length_x", "length", "width_y", "width", "hole_radius", "hole_center", "thickness", "unit", "units", "length_unit"}:
        return None
    length, width, radius, cx, cy = (shape[k] for k in ("length", "width", "radius", "cx", "cy"))
    mesh = bindings.get("mesh") or {}
    # The primitive accepts only these sizing controls; unknown constraints
    # remain with the general geometry writer instead of being silently lost.
    if set(mesh) - {"characteristic_length", "minimum_length", "element_order", "element_type", "refinement", "refinement_near_hole", "thickness"}:
        return None
    far = mesh.get("characteristic_length", min(length, width)/15)
    near = mesh.get("minimum_length", min(radius/10, far/4) if mesh.get("refinement_near_hole", True) else far)
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v <= 0 for v in (near, far)) or near > far:
        raise ValueError("Planar sizing requires 0 < minimum_length <= characteristic_length")
    names = bindings.get("named_regions") or []
    regions = {"left": "left", "left_edge": "left", "right": "right", "right_edge": "right",
               "bottom": "bottom", "bottom_edge": "bottom", "top": "top", "top_edge": "top",
               "hole": "hole", "hole_edge": "hole"}
    if any(name not in regions for name in names):
        return None  # Do not guess which boundary an unfamiliar name denotes.
    names = list(dict.fromkeys(names))
    if len({regions[name] for name in names}) != len(names):
        return None
    names += [name for name in ("left_edge", "right_edge", "bottom_edge", "top_edge", "hole")
              if regions[name] not in {regions[item] for item in names}]
    eps = max(length, width)*1e-7
    source = f'''// Native rectangle minus circular disk; all lengths use caller units.
SetFactory("OpenCASCADE");
Rectangle(1) = {{0,0,0,{length},{width}}};
Disk(2) = {{{cx},{cy},0,{radius},{radius}}};
domain[] = BooleanDifference{{ Surface{{1}}; Delete; }}{{ Surface{{2}}; Delete; }};
Physical Surface("plate") = {{domain[]}};
left[] = Curve In BoundingBox{{{-eps},{-eps},{-eps},{eps},{width+eps},{eps}}};
right[] = Curve In BoundingBox{{{length-eps},{-eps},{-eps},{length+eps},{width+eps},{eps}}};
bottom[] = Curve In BoundingBox{{{-eps},{-eps},{-eps},{length+eps},{eps},{eps}}};
top[] = Curve In BoundingBox{{{-eps},{width-eps},{-eps},{length+eps},{width+eps},{eps}}};
hole[] = Curve In BoundingBox{{{cx-radius-eps},{cy-radius-eps},{-eps},{cx+radius+eps},{cy+radius+eps},{eps}}};
'''
    source += "\n".join(f'Physical Curve("{name}") = {{{regions[name]}[]}};' for name in names)
    source += f'''
Field[1] = Distance;
Field[1].CurvesList = {{hole[]}};
Field[1].Sampling = 200;
Field[2] = Threshold;
Field[2].InField = 1;
Field[2].SizeMin = {near};
Field[2].SizeMax = {far};
Field[2].DistMin = {radius/4};
Field[2].DistMax = {3*radius};
Background Field = 2;
Mesh.MeshSizeMin = {near};
Mesh.MeshSizeMax = {far};
Mesh.MeshSizeExtendFromBoundary = 0;
Mesh.ElementOrder = {int(mesh.get("element_order", 1))};
'''
    return source


def check_planar_geometry(nodes, cells, bindings):
    """Check the materialized corner topology, not the generating script.

    Linear chordal boundaries approximate a circle: use radius/edge-count-aware
    area tolerance, not the false assertion that every edge lies on the circle.
    """
    shape = circular_plate_geometry(bindings)
    if shape is None:
        return None
    length, width, radius, cx, cy = (shape[k] for k in ("length", "width", "radius", "cx", "cy"))
    tolerance = max(length, width)*1e-7
    edges: dict[tuple[int, int], int] = {}
    used = {n for cell in cells for n in cell}
    for cell in cells:
        for a, b in zip(cell, cell[1:]+cell[:1]):
            key = tuple(sorted((a, b)))
            edges[key] = edges.get(key, 0)+1
    # Area uses absolute cell areas so valid opposite orientations cannot cancel.
    area = sum(abs(sum(nodes[a][0]*nodes[b][1]-nodes[b][0]*nodes[a][1]
                       for a, b in zip(cell, cell[1:]+cell[:1]))/2) for cell in cells)
    hole_edges = [edge for edge, count in edges.items() if count == 1
                  and all(abs(math.hypot(nodes[n][0]-cx, nodes[n][1]-cy)-radius) <= tolerance for n in edge)]
    inside = sum(math.hypot(nodes[n][0]-cx, nodes[n][1]-cy) < radius-tolerance for n in used)
    expected_area = length*width-math.pi*radius*radius
    angles = [2*math.asin(min(1.0, math.dist(nodes[a][:2], nodes[b][:2])/(2*radius))) for a, b in hole_edges]
    area_tolerance = expected_area*1e-6 + sum(radius**2*(angle-math.sin(angle))/2 for angle in angles)
    bounds = [[min(nodes[n][axis] for n in used), max(nodes[n][axis] for n in used)] for axis in range(2)] if used else []
    checks = {
        "rectangle_extent": bool(bounds) and all(abs(a-b) <= tolerance for pair, want in zip(bounds, ([0, length], [0, width])) for a, b in zip(pair, want)),
        "circular_hole_boundary": len(hole_edges) >= 3 and abs(sum(angles)-2*math.pi) < 1e-5,
        "hole_excludes_material_nodes": inside == 0,
        "material_area": abs(area-expected_area) <= area_tolerance,
    }
    bands: dict[str, list[float]] = {"near_hole": [], "far_field": []}
    for cell in cells:
        distance = math.hypot(sum(nodes[n][0] for n in cell)/len(cell)-cx,
                              sum(nodes[n][1] for n in cell)/len(cell)-cy)
        band = "near_hole" if distance <= 2*radius else "far_field" if distance >= 4*radius else ""
        if band:
            bands[band].append(max(math.dist(nodes[a][:2], nodes[b][:2]) for a, b in zip(cell, cell[1:]+cell[:1])))
    sizing = {name+"_mean_max_edge": sum(values)/len(values) if values else None for name, values in bands.items()}
    return {"status": "pass" if all(checks.values()) else "fail", "checks": checks,
            "observed": {"bounds": bounds, "hole_boundary_edges": len(hole_edges), "nodes_inside_hole": inside,
                         "material_area": area, "expected_area": expected_area, "area_tolerance": area_tolerance,
                         **sizing}}

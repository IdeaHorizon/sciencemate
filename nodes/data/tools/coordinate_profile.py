"""Coordinate-profile utilities for public/downloaded geometry data.

This module is intentionally generic: it converts one or more 2D coordinate
files into a single closed profile that downstream Gmsh profile meshing can
consume.  It is useful for blade, hydrofoil, strut, rib, or other section
profiles distributed as separate pressure/suction or upper/lower surface files.
"""
from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any

from core import paths
from core.state import State


def _parse_jsonish_list(value: Any) -> list[str]:
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value if str(item).strip()]
    text = str(value or "").strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
        if isinstance(parsed, (list, tuple)):
            return [str(item) for item in parsed if str(item).strip()]
    except json.JSONDecodeError:
        pass
    return [part.strip() for part in re.split(r"[\n,;]+", text) if part.strip()]


def _read_xy_text(text: str) -> list[tuple[float, float]]:
    head = text[:2000]
    if re.search(r"\bTITLE\s*=", head, flags=re.I) and re.search(r"\bVARIABLES\s*=", head, flags=re.I):
        variables = re.findall(r'"([^"]+)"', head)
        if len(variables) > 3:
            raise ValueError(
                "file appears to be Tecplot field/boundary-condition data, not a closed x-y profile"
            )
    points: list[tuple[float, float]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", "//", "%")):
            continue
        parts = re.split(r"[\s,;]+", stripped)
        if len(parts) < 2:
            continue
        try:
            points.append((float(parts[0]), float(parts[1])))
        except ValueError:
            continue
    cleaned: list[tuple[float, float]] = []
    for x, y in points:
        if not cleaned or math.hypot(x - cleaned[-1][0], y - cleaned[-1][1]) > 1e-12:
            cleaned.append((x, y))
    return cleaned


def _read_xy_file(path: Path) -> list[tuple[float, float]]:
    return _read_xy_text(path.read_text(encoding="utf-8", errors="ignore"))


def _read_xy_value(value: Any) -> list[tuple[float, float]]:
    if value in (None, "", [], {}):
        return []
    if isinstance(value, (list, tuple)):
        points: list[tuple[float, float]] = []
        for item in value:
            if isinstance(item, dict) and "x" in item and "y" in item:
                points.append((float(item["x"]), float(item["y"])))
            elif isinstance(item, (list, tuple)) and len(item) >= 2:
                points.append((float(item[0]), float(item[1])))
        return points
    text = str(value).strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, (list, tuple)):
        return _read_xy_value(parsed)
    return _read_xy_text(text)


def _dist(a: tuple[float, float], b: tuple[float, float]) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _append_best_oriented(
    chain: list[tuple[float, float]],
    segment: list[tuple[float, float]],
) -> list[tuple[float, float]]:
    candidates = [
        segment,
        list(reversed(segment)),
    ]
    best = min(candidates, key=lambda pts: _dist(chain[-1], pts[0]))
    if _dist(chain[-1], best[0]) < 1e-10:
        return chain + best[1:]
    return chain + best


def _order_segments(segments: list[list[tuple[float, float]]]) -> list[tuple[float, float]]:
    if not segments:
        return []
    remaining = [list(seg) for seg in segments]
    # Start with the segment whose first point is furthest downstream; this
    # often yields TE -> LE -> TE ordering for profile sections.
    start_idx = max(range(len(remaining)), key=lambda i: max(pt[0] for pt in remaining[i]))
    chain = remaining.pop(start_idx)
    if chain[0][0] < chain[-1][0]:
        chain = list(reversed(chain))
    while remaining:
        idx = min(range(len(remaining)), key=lambda i: min(_dist(chain[-1], remaining[i][0]), _dist(chain[-1], remaining[i][-1])))
        segment = remaining.pop(idx)
        chain = _append_best_oriented(chain, segment)
    if len(chain) >= 2 and _dist(chain[0], chain[-1]) < 1e-10:
        chain = chain[:-1]
    return chain


def _profile_quality(points: list[tuple[float, float]]) -> dict[str, Any]:
    if not points:
        return {"point_count": 0, "closed_gap": None}
    min_x = min(x for x, _ in points)
    max_x = max(x for x, _ in points)
    min_y = min(y for _, y in points)
    max_y = max(y for _, y in points)
    chord = max_x - min_x
    thickness = max_y - min_y
    return {
        "point_count": len(points),
        "min_x": min_x,
        "max_x": max_x,
        "min_y": min_y,
        "max_y": max_y,
        "chord": chord,
        "thickness": thickness,
        "thickness_to_chord": thickness / chord if chord > 0 else None,
        "closed_gap": _dist(points[0], points[-1]) if len(points) >= 2 else None,
    }


async def build_coordinate_profile(
    *,
    state: State,
    coordinate_files: Any = None,
    coordinate_text: Any = "",
    coordinate_arrays: Any = None,
    output_name: str = "closed_profile.dat",
    profile_name: str = "downloaded_profile",
    **_: Any,
) -> dict[str, Any]:
    coordinate_paths = [Path(item).expanduser() for item in _parse_jsonish_list(coordinate_files)]
    missing = [str(path) for path in coordinate_paths if not path.exists()]
    if missing:
        return {"status": "error", "error": "coordinate file(s) do not exist", "missing": missing}

    segments: list[list[tuple[float, float]]] = []
    source_reports: list[dict[str, Any]] = []
    for path in coordinate_paths:
        try:
            points = _read_xy_file(path)
        except ValueError as exc:
            return {
                "status": "needs_geometry_processing",
                "message": str(exc),
                "source_file": str(path),
                "recommended_tools": ["data_web_download", "prepare_scientific_mesh"],
                "next_action": (
                    "该文件不是二维闭合 profile。若已有 IGES/STEP/GEO/MSH 几何文件，"
                    "应改用 prepare_scientific_mesh(mesh_type='geometry_file_gmsh')；"
                    "若只有场数据，请继续公开检索真实几何/CAD/profile 文件。"
                ),
            }
        source_reports.append({"path": str(path), "point_count": len(points)})
        if len(points) < 2:
            return {"status": "error", "error": f"coordinate file has fewer than 2 points: {path}"}
        segments.append(points)

    for source_name, raw_value in (
        ("coordinate_text", coordinate_text),
        ("coordinate_arrays", coordinate_arrays),
    ):
        if raw_value in (None, "", [], {}):
            continue
        try:
            points = _read_xy_value(raw_value)
        except (TypeError, ValueError, KeyError) as exc:
            return {"status": "error", "error": f"invalid {source_name}: {exc}"}
        source_reports.append({"source": source_name, "point_count": len(points)})
        if len(points) < 2:
            return {"status": "error", "error": f"{source_name} has fewer than 2 points"}
        segments.append(points)

    if not segments:
        return {
            "status": "error",
            "error": "coordinate_files, coordinate_text, or coordinate_arrays is required",
        }

    profile = _order_segments(segments)
    # 判决拆除（cp:205 删，2026-08-31）：≥20 点是任意审美阈值；真 validity 由
    # 相邻检查承担（每段 <2 点在上方拒绝、零弦长在下方拒绝）。
    closed_profile = profile + [profile[0]]
    quality = _profile_quality(closed_profile)
    if not quality.get("chord") or quality["chord"] <= 0:
        return {"status": "error", "error": "combined profile has zero chord", "quality": quality}

    workspace = getattr(state, "workspace_root", None) or state.root
    out_dir = paths.data_workspace_dir(
        Path(str(workspace)).expanduser().resolve(),
        "derived_geometry",
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    safe_name = re.sub(r"[^A-Za-z0-9._+-]+", "_", output_name or "closed_profile.dat").strip("._")
    if not safe_name:
        safe_name = "closed_profile.dat"
    if Path(safe_name).suffix.lower() not in {".dat", ".txt", ".csv"}:
        safe_name += ".dat"
    out_path = out_dir / safe_name
    with out_path.open("w", encoding="utf-8") as handle:
        handle.write(f"# {profile_name}\n")
        handle.write("# Combined 2D profile from coordinate_files; columns: x y\n")
        for x, y in closed_profile:
            handle.write(f"{x:.12g} {y:.12g}\n")

    return {
        "status": "success",
        "profile_name": profile_name,
        "coordinate_profile_path": str(out_path),
        "airfoil_dat_path": str(out_path),
        "source_files": [str(path) for path in coordinate_paths],
        "source_reports": source_reports,
        "quality": quality,
        "recommended_next_call": {
            "tool": "prepare_scientific_mesh",
            "operation": "generate",
            "mesh_type": "coordinate_profile_gmsh",
            "parameters": {
                "coordinate_profile_path": str(out_path),
                "airfoil_dat_path": str(out_path),
                "airfoil_name": profile_name,
                "convert_to_openfoam": True,
                "write_tecplot": True,
                "quality_preset": "robust",
            },
        },
    }

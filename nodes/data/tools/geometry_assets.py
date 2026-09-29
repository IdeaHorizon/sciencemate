"""Shared discovery and selection for local/downloaded geometry assets."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from core.state import State

from .human_input_utils import is_placeholder_value


COORDINATE_PROFILE_SUFFIXES = {".dat", ".txt", ".xy"}
CAD_SOLID_SUFFIXES = {".step", ".stp", ".iges", ".igs", ".brep"}
GEOMETRY_ASSET_SUFFIXES = COORDINATE_PROFILE_SUFFIXES | CAD_SOLID_SUFFIXES | {
    ".geo", ".msh", ".stl",
    ".cas", ".cgns", ".foam", ".vtk", ".vtu", ".med", ".unv",
    ".exo", ".ex2", ".e",
}
GEOMETRY_FILE_KEYS = (
    "geometry_file", "geometry_path", "cad_file", "stl_file", "step_file",
    "geo_file", "msh_file",
)
COORDINATE_PROFILE_KEYS = (
    "coordinate_profile_path", "profile_dat_path", "blade_profile_path", "airfoil_dat_path",
)
GEOMETRY_INPUT_KEYS = GEOMETRY_FILE_KEYS + COORDINATE_PROFILE_KEYS + (
    "airfoil_coordinate_text", "airfoil_coordinates",
)
_PARAMETER_GROUPS = ("geometry", "flow", "domain", "mesh", "mesh_density", "outputs")


def canonical_condition_region(name: Any) -> str:
    """Recover a physical region from a model-flattened condition key."""
    region = re.sub(r"[^a-z0-9]+", "_", str(name or "").casefold()).strip("_")
    if region in {
        "direction", "magnitude", "value", "unit", "pressure", "traction",
        "stress", "load", "force", "pressure_mpa", "traction_mpa", "stress_mpa",
    }:
        return ""
    return re.sub(
        r"_(?:pressure|traction|stress|load|force)(?:_(?:pa|kpa|mpa|gpa|n|kn|mn))?$",
        "",
        region,
    )


def canonical_parameter_bindings(value: Any) -> dict[str, Any]:
    """Normalize model-chosen group names without changing bound values."""
    if not isinstance(value, dict):
        return {}
    aliases = {
        "boundary": "boundary_conditions",
        "boundaries": "boundary_conditions",
        "constraint": "boundary_conditions",
        "constraints": "boundary_conditions",
        "load": "loads",
        "traction": "loads",
        "tractions": "loads",
    }
    normalized: dict[str, Any] = {}
    for raw_key, item in value.items():
        token = re.sub(r"[^a-z0-9]+", "_", str(raw_key).casefold()).strip("_")
        key = aliases.get(token, token)
        if isinstance(item, dict) and isinstance(normalized.get(key), dict):
            normalized[key] = {**normalized[key], **item}
        else:
            normalized[key] = item
    return normalized


def flatten_parameter_groups(
    params: dict[str, Any],
    extra_groups: tuple[str, ...] = (),
) -> dict[str, Any]:
    flattened = dict(params)
    for group in (*_PARAMETER_GROUPS, *extra_groups):
        value = flattened.get(group)
        if isinstance(value, dict):
            for key, nested_value in value.items():
                flattened.setdefault(key, nested_value)
    return flattened


def canonical_mesh_controls(params: dict[str, Any], supported: Any = None) -> dict[str, Any]:
    """Reuse mesh-stage vocabulary at the actual adapter boundary as well."""
    effective = dict(params)
    aliases = {
        "far_field": ("farfield_radius", "far_field_radius", "farfield", "domain_radius"),
        "airfoil_name": ("airfoil", "profile_name"),
        "reynolds_number": ("reynolds", "re", "Re"),
        "upstream_length": ("domain_upstream_length",),
        "downstream_length": ("domain_downstream_length",),
        "characteristic_length": ("target_size", "target_element_size"),
        "minimum_length": ("min_size", "minimum_element_size", "min_element_size"),
        "element_order": ("order",),
        "solver_mesh_format": ("output_format", "mesh_format"),
        "output_version": ("output_format_version", "msh_file_version"),
    }
    for target, sources in aliases.items():
        if supported is not None and target not in supported:
            continue
        if effective.get(target) not in (None, "", [], {}):
            continue
        value = next((params[key] for key in sources if params.get(key) not in (None, "", [], {})), None)
        if value is not None:
            effective[target] = value
    return effective


def mesh_visualization_requested(params: dict[str, Any], formats: Any = ()) -> bool:
    """Choose companion visualization by delivery format, preserving opt-outs."""
    params = canonical_mesh_controls(flatten_parameter_groups(params))
    if params.get("write_tecplot") is not None:
        return str(params["write_tecplot"]).strip().lower() not in {"false", "0", "no", "off", ""}
    if str(params.get("convert_to_openfoam") or "").lower() in {"true", "1"}:
        return True
    labels = [str(value or "").lower() for value in (
        params.get("solver_mesh_format"), params.get("mesh_format"), *formats,
    )]
    if any("tecplot" in label for label in labels):
        return True
    if params.get("solver_mesh_format"):
        # An intermediate MSH does not require a companion when the final
        # delivery is a directly viewable solver deck or another format.
        labels = [str(params["solver_mesh_format"]).lower()]
    return any(re.search(r"\bmsh\s*\d*(?:\.\d+)?\b", label) for label in labels)


def geometry_file_from_params(params: dict[str, Any]) -> str:
    params = flatten_parameter_groups(params)
    for key in GEOMETRY_FILE_KEYS:
        value = params.get(key)
        if value not in (None, "") and not is_placeholder_value(value):
            path = existing_geometry_like_path(value)
            if path:
                return path
    return ""


def existing_geometry_like_path(path: Any) -> str:
    value = str(path or "").strip()
    if not value:
        return ""
    candidate = Path(value).expanduser()
    if candidate.is_file() and candidate.suffix.lower() in GEOMETRY_ASSET_SUFFIXES:
        return str(candidate.resolve())
    return ""


def has_explicit_geometry_input(params: dict[str, Any]) -> bool:
    if geometry_file_from_params(params):
        return True
    if any(existing_geometry_like_path(params.get(key)) for key in COORDINATE_PROFILE_KEYS):
        return True
    return any(
        params.get(key) not in (None, "", [], {}) and not is_placeholder_value(params.get(key))
        for key in ("airfoil_coordinate_text", "airfoil_coordinates")
    )


def _asset_token(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def downloaded_geometry_assets_from_state(state: State) -> list[dict[str, Any]]:
    assets: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add_asset(item: Any, source: str = "downloaded_geometry_asset") -> None:
        if not isinstance(item, dict):
            return
        path = existing_geometry_like_path(
            item.get("preferred_geometry_file")
            or item.get("path")
            or item.get("saved_path")
            or item.get("local_path")
        )
        if not path or path in seen:
            return
        seen.add(path)
        assets.append({
            "path": path,
            "downloaded_path": str(item.get("downloaded_path") or item.get("saved_path") or path),
            "url": item.get("url") or (item.get("source_result") or {}).get("url"),
            "sha256": item.get("sha256"),
            "extension": Path(path).suffix.lower(),
            "source": item.get("source") or source,
        })

    hook_state = getattr(state, "hook_state", {})
    if isinstance(hook_state, dict):
        add_asset(hook_state.get("scientific_mesh_pending_asset_evaluation"))
        for item in hook_state.get("_data_downloaded_geometry_assets") or []:
            add_asset(item)

    download_dirs = [Path(str(state.root)) / ".data_node_work" / "downloads"]
    for download_dir in download_dirs:
        index_path = download_dir / "download_index.json"
        try:
            index = json.loads(index_path.read_text(encoding="utf-8")) if index_path.is_file() else {}
        except (OSError, json.JSONDecodeError):
            index = {}
        if isinstance(index, dict):
            for item in index.values():
                add_asset(item)
        if download_dir.is_dir():
            for path in sorted(download_dir.rglob("*")):
                add_asset({"path": str(path)}, source="run_local_downloads_scan")
    return assets


def _add_evidence_assets(value: Any, assets: list[dict[str, Any]]) -> None:
    seen = {str(item.get("path")) for item in assets}

    def visit(item: Any) -> None:
        if isinstance(item, list):
            for nested in item:
                visit(nested)
            return
        if not isinstance(item, dict):
            return
        for key in ("auto_downloaded_geometry", "download", "download_result"):
            nested = item.get(key)
            if not isinstance(nested, dict):
                continue
            path = existing_geometry_like_path(
                nested.get("preferred_geometry_file")
                or nested.get("path")
                or nested.get("saved_path")
                or nested.get("local_path")
            )
            if path and path not in seen:
                seen.add(path)
                assets.append({
                    "path": path,
                    "downloaded_path": str(nested.get("saved_path") or path),
                    "url": nested.get("url") or (nested.get("source_result") or {}).get("url"),
                    "sha256": nested.get("sha256"),
                    "extension": Path(path).suffix.lower(),
                    "source": "reference_evidence_download",
                })
        for key in ("result", "evidence", "reference_evidence"):
            if isinstance(item.get(key), (dict, list)):
                visit(item[key])

    visit(value)


def apply_downloaded_geometry_assets_to_params(
    state: State,
    spec: str,
    params: dict[str, Any],
) -> None:
    """Select one unambiguous, intent-matching asset without changing geometry role."""
    if has_explicit_geometry_input(params):
        return
    assets = downloaded_geometry_assets_from_state(state)
    _add_evidence_assets(params.get("reference_evidence"), assets)
    analysis = params.get("requirement_analysis")
    if isinstance(analysis, dict):
        _add_evidence_assets(analysis.get("reference_evidence"), assets)
    if not assets:
        return

    profile_token = _asset_token(
        params.get("airfoil_name") or params.get("airfoil") or params.get("profile_name")
        or params.get("blade_name") or params.get("geometry_name")
    )
    profile_intent = bool(
        profile_token
        or re.search(
            r"\b(airfoil|aerofoil|blade|profile|cascade)\b|翼型|机翼|叶片|叶栅",
            spec or "",
            flags=re.I,
        )
    )
    coordinate_assets = [item for item in assets if item.get("extension") in COORDINATE_PROFILE_SUFFIXES]
    non_coordinate_assets = [item for item in assets if item not in coordinate_assets]
    candidates = coordinate_assets if profile_intent else non_coordinate_assets
    if profile_intent and profile_token:
        matched = [
            item for item in candidates
            if profile_token in _asset_token(Path(str(item["path"])).stem)
            or profile_token in _asset_token(item.get("url"))
        ]
        asset = matched[0] if len(matched) == 1 else (candidates[0] if len(candidates) == 1 else None)
    else:
        asset = candidates[0] if len(candidates) == 1 else None
    if not asset:
        return

    path = str(asset["path"])
    if Path(path).suffix.lower() in COORDINATE_PROFILE_SUFFIXES:
        params.setdefault("coordinate_profile_path", path)
        params.setdefault("profile_dat_path", path)
        if re.search(r"\b(airfoil|aerofoil|profile|naca)\b|翼型|机翼", spec, flags=re.I):
            params.setdefault("airfoil_dat_path", path)
    else:
        params.setdefault("geometry_file", path)
    trace = params.get("source_trace")
    if not isinstance(trace, list):
        trace = [] if trace in (None, "") else [trace]
        params["source_trace"] = trace
    trace.append({
        "source": "downloaded_geometry_asset",
        "path": path,
        "url": asset.get("url"),
        "sha256": asset.get("sha256"),
        "decision": "use_existing_download_before_human_input",
    })

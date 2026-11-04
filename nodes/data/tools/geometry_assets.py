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


def canonical_condition_region(name: Any, controls: Any = None, known_regions=()) -> str:
    """Recover a physical region from a model-flattened condition key."""
    region = re.sub(r"[^a-z0-9]+", "_", str(name or "").casefold()).strip("_")
    # Mesh physical groups are authoritative identities, including names such
    # as LOAD. A scalar quantity/default is different from a region assignment.
    if any(re.sub(r"[^a-z0-9]+", "_", str(item).casefold()).strip("_") == region
           for item in known_regions):
        return region
    assignment = (isinstance(controls, dict) and bool(controls.keys() - {"value", "unit", "units"})) or (
        isinstance(controls, str) and bool(re.search(r"(?:\bon\s+[xyz]\s*=|[+-][xyz]\b)", controls, re.I)))
    if region in {"pressure", "traction", "stress", "load", "force"} and assignment:
        return region
    if region in {
        "direction", "magnitude", "value", "unit", "units", "pressure", "traction",
        "stress", "load", "force", "pressure_mpa", "traction_mpa", "stress_mpa",
    }:
        return ""
    # Aggregate checks describe a load, not an extra physical group. Preserve
    # them in the bindings for review; never create a second loaded boundary.
    if re.search(r"(?:^|_)(?:resultant|total_force)(?:_(?:n|kn|mn))?$", region):
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
        "boundary_condition": "boundary_conditions",
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
        region = re.fullmatch(r"(.+?)_(?:boundary_)?(condition|constraint|load|traction)", token)
        if region and token not in aliases:
            key = "boundary_conditions" if region[2] in {"condition", "constraint"} else "loads"
            item = {region[1]: item}
        if isinstance(item, dict) and isinstance(normalized.get(key), dict):
            normalized[key] = {**normalized[key], **item}
        else:
            normalized[key] = item
    return normalized


def parameter_binding_value(group: dict[str, Any], *names: str, default: Any = None) -> Any:
    """Read a runtime value; leave its authoritative unit/condition record intact."""
    values = {
        re.sub(r"[^a-z0-9]+", "", str(key).casefold()): value
        for key, value in group.items()
    }
    value = default
    for name in names:
        token = re.sub(r"[^a-z0-9]+", "", name.casefold())
        if token in values and values[token] not in (None, ""):
            value = values[token]
            break
    # Unwrap only a quantity envelope, not a condition such as
    # {type: 'dirichlet', value: 400, unit: 'K'} or an arbitrary model object.
    # Magnitudes stay in the supplied consistent units; this is no SI conversion.
    if isinstance(value, dict) and "value" in value and not value.keys() - {"value", "unit", "units"}:
        return value["value"]
    return value


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
        "required_far_field": ("far_field_distance_min", "farfield_distance_min", "minimum_far_field_distance"),
        "airfoil_name": ("airfoil", "profile_name"),
        "reynolds_number": ("reynolds", "re", "Re"),
        "upstream_length": ("domain_upstream_length",),
        "downstream_length": ("domain_downstream_length",),
        "characteristic_length": ("target_size", "target_element_size"),
        "minimum_length": ("min_size", "minimum_size", "minimum_element_size", "min_element_size"),
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


def mesh_visualization_requested(
    params: dict[str, Any], formats: Any = (), *, request: dict[str, Any] | None = None,
) -> bool:
    """Use caller choices, then delivery-format defaults; inferred flags are not opt-outs."""
    from nodes.data.planning.request_contract import caller_request_text

    analysis = params.get("requirement_analysis") or {}
    request = request or params.get("preprocessing_request") or analysis.get("preprocessing_request") or {}
    params = canonical_mesh_controls(flatten_parameter_groups(params))
    choice = params.get("write_tecplot") if not request else None
    text = caller_request_text(request or params, preprocessing_request=request)
    flag = re.search(r"\bwrite_tecplot\s*[:=]\s*(true|false|0|1)\b", text, re.I)
    if flag:
        choice = flag.group(1)
    elif re.search(r"(?:不需要|无需|不要|不用|不生成|不输出|省略|跳过|\b(?:without|no|omit|skip|disable)\b)"
                   r"[^\n。；;,.]{0,24}(?:可视化|tecplot|visualization)", text, re.I):
        choice = False
    elif re.search(r"可视化|\b(?:tecplot|visualization)\b", text, re.I):
        choice = True
    elif request:
        inputs = request.get("inputs") or []
        sources = [request.get("authority_snapshot"), request,
                   *(inputs if isinstance(inputs, list) else [inputs]),
                   *(request.get("requested_assets") or [])]
        for source in sources:
            if not isinstance(source, dict):
                continue
            controls = flatten_parameter_groups(source, ("parameters", "parameter_bindings"))
            controls = flatten_parameter_groups(controls)
            if controls.get("write_tecplot") is not None:
                choice = controls["write_tecplot"]
                break
    if choice is not None:
        return str(choice).strip().lower() not in {"false", "0", "no", "off", ""}
    if str(params.get("convert_to_openfoam") or "").lower() in {"true", "1"}:
        return True
    labels = [str(value or "").lower() for value in (
        params.get("solver_mesh_format"), params.get("mesh_format"), *formats,
    )]
    if any("openfoam" in label or "polymesh" in label for label in labels):
        return True
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


def coordinate_profile_from_params(params: dict[str, Any]) -> str:
    """Recognize coordinate input even when supplied through a generic file key."""
    params = flatten_parameter_groups(params)
    for key in (*COORDINATE_PROFILE_KEYS, *GEOMETRY_FILE_KEYS):
        path = existing_geometry_like_path(params.get(key))
        if path and Path(path).suffix.lower() in COORDINATE_PROFILE_SUFFIXES:
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
    if coordinate_profile_from_params(params):
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

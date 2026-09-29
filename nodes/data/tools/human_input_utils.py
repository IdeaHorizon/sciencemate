"""Helpers for deterministic human-in-the-loop resume handling in data tools."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from core.state import State
from nodes.data.pipeline_contract import needs_input_result
from nodes.data.planning.request_contract import caller_request_text
_PLACEHOLDER_RE = re.compile(
    r"^\s*(?:"
    r"PENDING_USER_INPUT|requested_but_not_provided|TODO|TBD|NONE|NULL|"
    r"/absolute/path/to/.*|请替换为实际路径"
    r")\s*$",
    flags=re.IGNORECASE,
)

_AIRFOIL_PATH_FIELD_RE = re.compile(
    r"(?:^|[\s,{])(?:airfoil_dat_path|airfoil_path|geometry_file|geometry_path)\s*[:=]\s*"
    r"(?P<quote>[\"']?)(?P<path>[^\"'\s,}]+)(?P=quote)",
    flags=re.IGNORECASE,
)

_AIRFOIL_TEXT_FIELD_RE = re.compile(
    r"(?:^|[\s,{])(?:airfoil_coordinate_text|airfoil_coordinates)\s*[:=]\s*(?P<value>.+)$",
    flags=re.IGNORECASE | re.DOTALL,
)

_GEOMETRY_PATH_FIELD_RE = re.compile(
    r"(?:^|[\s,{])(?:"
    r"geometry_file|geometry_path|cad_file|stl_file|step_file|geo_file|msh_file|"
    r"airfoil_dat_path|blade_profile_path|profile_dat_path"
    r")\s*[:=]\s*(?P<quote>[\"']?)(?P<path>[^\"'\s,}]+)(?P=quote)",
    flags=re.IGNORECASE,
)

_PUBLIC_REFERENCE_REQUEST_RE = re.compile(
    r"(?:"
    r"网络|网上|联网|公开|文献|资料|数据库|搜索|检索|查找|寻找|"
    r"internet|web|online|public|literature|reference|search|find"
    r")",
    flags=re.IGNORECASE,
)

_AIRFOIL_OR_NACA_INTENT_RE = re.compile(
    r"(?:\bNACA\s*[-_ ]?\d{4}\b|\bairfoil\b|\baerofoil\b|\bwing\b|翼型|机翼)",
    flags=re.IGNORECASE,
)

_NON_AIRFOIL_GEOMETRY_INTENT_RE = re.compile(
    r"(?:"
    r"\bcylinder\b|\bcircular\s+cylinder\b|\bsphere\b|\bpipe\b|\bduct\b|"
    r"\bchannel\b|\bcavity\b|\bbackward[- ]?facing\s+step\b|\bnozzle\b|"
    r"\bcascade\b|\bturbomachinery\b|\bturbine\b|\bcompressor\b|\bvane\b|"
    r"圆柱|球体|管道|管流|槽道|方腔|后台阶|喷管|叶栅|叶排|涡轮|透平|压气机"
    r")",
    flags=re.IGNORECASE,
)


def pause_for_input(
    question: str,
    context: str = "",
    options: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the shared resumable input result used by every data producer."""
    details = dict(metadata or {})
    return needs_input_result(
        question=question,
        context=context,
        options=options,
        missing_fields=details.pop("required_fields", []),
        metadata=details,
    )

_MESH_DENSITY_FIELDS = {
    "n_surface": int,
    "target_cell_count": int,
    "target_cells": int,
    "cell_budget": int,
    "h_airfoil": float,
    "h_cylinder": float,
    "h_farfield": float,
    "boundary_layer_first": float,
    "boundary_layer_thickness": float,
    "boundary_layer_ratio": float,
    "target_y_plus": float,
    "target_streamwise_delta_x_plus": float,
}
_RECOMMENDED_MESH_DENSITY = {
    "n_surface": 401,
    "h_airfoil": 0.004,
    "h_farfield": 0.8,
    "boundary_layer_first": 0.0001,
    "boundary_layer_thickness": 0.04,
    "boundary_layer_ratio": 1.12,
    "quality_preset": "robust",
}

_MESH_DENSITY_KEYS = tuple(_MESH_DENSITY_FIELDS)


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on", "是", "确认"}
    return False


def has_mesh_density_parameters(params: dict[str, Any] | None) -> bool:
    """Return whether any executable mesh-density value was supplied."""
    return bool(
        isinstance(params, dict)
        and any(params.get(key) is not None for key in _MESH_DENSITY_KEYS)
    )


def mesh_density_confirmed(params: dict[str, Any] | None) -> bool:
    """Read the common confirmation flag used by all mesh generators."""
    if not isinstance(params, dict):
        return False
    return _as_bool(
        params.get("mesh_density_confirmed")
        or params.get("confirm_mesh_density")
        or params.get("user_confirmed_mesh_density")
    )


def apply_recommended_mesh_density(
    params: dict[str, Any],
    *,
    source: str = "data_default_recommended",
) -> dict[str, Any]:
    """Apply one shared conservative density preset and record its provenance."""
    for key, value in _RECOMMENDED_MESH_DENSITY.items():
        params.setdefault(key, value)
    params["mesh_density_confirmed"] = True
    params.setdefault("mesh_density_confirmation_source", source)
    return params


def mesh_density_requested(text: Any) -> bool:
    """Detect a request for mesh resolution without tying it to one discipline."""
    lower = str(text or "").lower()
    return any(keyword in lower for keyword in (
        "加密", "更密", "细网格", "密网格", "网格量", "网格数量", "周向", "径向",
        "第一层", "首层", "近壁", "壁面", "边界层", "y+", "yplus",
        "n_surface", "h_airfoil", "h_farfield", "boundary_layer_first",
        "boundary_layer_ratio", "mesh density", "mesh refinement", "near wall",
        "first cell", "cell size",
    ))


def mesh_density_requires_confirmation(params: dict[str, Any], text: Any) -> bool:
    """Only explicit custom/confirmation requests should create a HITL pause."""
    if not isinstance(params, dict):
        return False
    if params.get("require_mesh_density_confirmation") is True or params.get(
        "mesh_density_confirmation_required"
    ) is True:
        return True
    return bool(re.search(
        r"(?:请|需要|必须|must|need).{0,8}(?:确认|自定义|confirm|custom).{0,8}"
        r"(?:网格|加密|密度|mesh|refinement|density)",
        str(text or ""),
        flags=re.I,
    ))


def is_placeholder_value(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        text = value.strip()
        return not text or bool(_PLACEHOLDER_RE.match(text))
    if isinstance(value, (list, tuple, dict)):
        return len(value) == 0
    return False


def apply_mesh_density_from_text(params: dict[str, Any], text: Any) -> dict[str, Any]:
    """Merge density values and confirmations written in a caller spec.

    Service plans commonly preserve the caller request as ``spec`` while
    putting executable values in ``parameters``.  Treating a textual
    ``mesh_density_confirmed=true`` as prose made a resumed request look
    unconfirmed and caused the same human-input pause on every new run.
    Explicit values or a clear recommendation choice are deterministic input,
    not a new scientific decision.
    """
    if not isinstance(params, dict):
        return params
    value = caller_request_text(text)
    if not value:
        return params
    updates: dict[str, Any] = {}
    for field, converter in _MESH_DENSITY_FIELDS.items():
        match = re.search(
            rf"(?:[\"']?{re.escape(field)}[\"']?)\s*[:=：]\s*"
            r"[\"']?([-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?)[\"']?",
            value,
            flags=re.I,
        )
        if not match:
            continue
        try:
            updates[field] = converter(float(match.group(1)))
        except (TypeError, ValueError, OverflowError):
            continue
    explicit_confirmation = bool(re.search(
        r"[\"']?(?:mesh_density_confirmed|confirm_mesh_density|user_confirmed_mesh_density)[\"']?"
        r"\s*[:=：]\s*(?:true|1|yes|confirmed|确认|已确认)|"
        r"(?:使用|采用|按|用).{0,8}(?:合理|推荐|默认).{0,8}(?:加密|网格|密度|参数|值)|"
        r"(?:recommended|default)\s+(?:mesh\s+)?(?:density|parameters?)",
        value,
        flags=re.I,
    ))
    if not updates and not explicit_confirmation:
        return params
    params.update(updates)
    # A request to use "reasonable/default" density authorizes the selected
    # producer's defaults; it does not select one cross-domain numeric preset.
    # Concrete defaults belong to the mesh adapter because a suitable first
    # layer height or surface resolution is geometry- and solver-dependent.
    params["mesh_density_confirmed"] = True
    params.setdefault(
        "mesh_density_confirmation_source",
        "caller_spec" if explicit_confirmation else "caller_spec_values",
    )
    return params


def original_user_request_text_from_state(state: State) -> str:
    """Read intent only from the locked request envelope, never transcripts."""
    request = state.hook_state.get("_data_current_request")
    if not isinstance(request, dict):
        return ""
    return caller_request_text(request, preprocessing_request=request)


def extract_naca_code(*values: Any) -> str | None:
    """Return an explicit 4/5-digit NACA designation from structured or text input."""
    for value in values:
        if value is None:
            continue
        if isinstance(value, dict):
            value = value.get("naca_code") or value.get("code")
            if value is None:
                continue
        text = str(value).strip()
        direct = re.fullmatch(r"\d{4,5}", text)
        labelled = re.search(r"\bNACA\s*[-_ ]?(\d{4,5})\b", text, flags=re.I)
        if direct or labelled:
            return direct.group(0) if direct else labelled.group(1)
    return None


def looks_like_airfoil_or_naca_request(text: Any, params: dict[str, Any] | None = None) -> bool:
    if isinstance(text, str) and _AIRFOIL_OR_NACA_INTENT_RE.search(text):
        return True
    params = params or {}
    for key in ("naca_code", "airfoil_name", "airfoil", "airfoil_dat_path"):
        value = params.get(key)
        if value and not is_placeholder_value(value):
            return True
    geometry = params.get("geometry")
    if isinstance(geometry, dict):
        if str(geometry.get("type") or "").strip().lower() == "naca":
            return True
        for key in ("naca_code", "airfoil_name", "airfoil_dat_path"):
            if geometry.get(key) and not is_placeholder_value(geometry.get(key)):
                return True
    elif isinstance(geometry, str) and _AIRFOIL_OR_NACA_INTENT_RE.search(geometry):
        return True
    return False


def surrogate_geometry_explicitly_approved(params: dict[str, Any] | None = None) -> bool:
    params = params or {}
    return (
        _as_bool(params.get("surrogate_geometry_approved"))
        and _as_bool(params.get("nonphysical_approximation"))
        and not is_placeholder_value(params.get("surrogate_geometry"))
    )


def original_intent_allows_airfoil_mesh(
    state: State,
    current_text: str = "",
    params: dict[str, Any] | None = None,
) -> bool:
    """Return whether generating an airfoil/NACA mesh preserves user intent."""
    params = params or {}
    if surrogate_geometry_explicitly_approved(params):
        return True
    original = original_user_request_text_from_state(state)
    if original:
        if looks_like_airfoil_or_naca_request(original, {}):
            return True
        if _NON_AIRFOIL_GEOMETRY_INTENT_RE.search(original):
            return False
        return looks_like_airfoil_or_naca_request(current_text, params)
    return looks_like_airfoil_or_naca_request(current_text, params)


def looks_like_public_reference_request(text: Any) -> bool:
    if not isinstance(text, str):
        return False
    value = text.strip()
    if not value or is_placeholder_value(value):
        return False
    for match in _PUBLIC_REFERENCE_REQUEST_RE.finditer(value):
        prefix = value[max(0, match.start() - 40):match.start()]
        if re.search(
            r"(?:不需要|无需|不要|禁止|拒绝|不使用|不依赖|"
            r"do\s+not|don't|not|no|without)"
            r"[^。；;,，\n]{0,28}$",
            prefix,
            flags=re.IGNORECASE,
        ):
            continue
        return True
    return False


def looks_like_airfoil_path(text: str) -> bool:
    value = extract_airfoil_path_from_text(text) or text.strip()
    if is_placeholder_value(value):
        return False
    suffixes = (".dat", ".txt", ".csv", ".coord", ".coords")
    if value.startswith(("~", "/")) and len(value) > 1:
        return True
    if Path(value).suffix.lower() in suffixes:
        return True
    return False


def looks_like_geometry_path(text: Any) -> bool:
    if not isinstance(text, str):
        return False
    value = extract_geometry_path_from_text(text) or text.strip()
    if is_placeholder_value(value):
        return False
    suffixes = (
        ".dat",
        ".txt",
        ".csv",
        ".coord",
        ".coords",
        ".geo",
        ".msh",
        ".step",
        ".stp",
        ".iges",
        ".igs",
        ".stl",
        ".brep",
    )
    if value.startswith(("~", "/")) and len(value) > 1:
        return True
    return Path(value).suffix.lower() in suffixes


def extract_geometry_path_from_text(text: Any) -> str | None:
    if not isinstance(text, str):
        return None
    value = text.strip()
    if is_placeholder_value(value):
        return None
    match = _GEOMETRY_PATH_FIELD_RE.search(value)
    if match:
        return match.group("path").strip()
    return None


def extract_airfoil_path_from_text(text: Any) -> str | None:
    if not isinstance(text, str):
        return None
    value = text.strip()
    if is_placeholder_value(value):
        return None
    match = _AIRFOIL_PATH_FIELD_RE.search(value)
    if match:
        return match.group("path").strip()
    return None


def extract_airfoil_coordinate_text_from_text(text: Any) -> str | None:
    if not isinstance(text, str):
        return None
    value = text.strip()
    if is_placeholder_value(value):
        return None
    match = _AIRFOIL_TEXT_FIELD_RE.search(value)
    if match:
        candidate = match.group("value").strip()
        return candidate if candidate and not is_placeholder_value(candidate) else None
    return None


def looks_like_airfoil_coordinate_text(value: Any) -> bool:
    if is_placeholder_value(value):
        return False
    if isinstance(value, (list, tuple)):
        return len(value) >= 3
    text = str(value).strip()
    if not text:
        return False
    try:
        parsed = json.loads(text)
    except Exception:
        parsed = None
    if isinstance(parsed, (list, tuple)):
        return len(parsed) >= 3
    count = 0
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", "//")):
            continue
        nums = re.findall(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][-+]?\d+)?", stripped)
        if len(nums) >= 2:
            count += 1
        if count >= 3:
            return True
    return False


def has_valid_airfoil_geometry(params: dict[str, Any]) -> bool:
    path = params.get("airfoil_dat_path")
    if path and not is_placeholder_value(path):
        return True
    for key in ("airfoil_dat_path", "airfoil_coordinate_text", "airfoil_coordinates", "geometry_file", "geometry_path"):
        if extract_airfoil_path_from_text(params.get(key)):
            return True
    text = params.get("airfoil_coordinate_text")
    extracted_text = extract_airfoil_coordinate_text_from_text(text)
    if looks_like_airfoil_coordinate_text(extracted_text):
        return True
    if looks_like_airfoil_coordinate_text(text):
        return True
    coords = params.get("airfoil_coordinates")
    if looks_like_airfoil_coordinate_text(coords):
        return True
    return False


def _apply_geometry_path(params: dict[str, Any], path: str) -> None:
    suffix = Path(path).suffix.lower()
    if suffix in {".dat", ".txt", ".csv", ".coord", ".coords"}:
        params.setdefault("coordinate_profile_path", path)
        params.setdefault("blade_profile_path", path)
        params.setdefault("airfoil_dat_path", path)
    else:
        params.setdefault("geometry_file", path)


def _normalise_embedded_airfoil_geometry_fields(params: dict[str, Any]) -> None:
    for key in ("airfoil_dat_path", "geometry_file", "geometry_path", "airfoil_coordinate_text"):
        extracted_path = extract_airfoil_path_from_text(params.get(key))
        if extracted_path:
            if key == "airfoil_dat_path" or is_placeholder_value(params.get("airfoil_dat_path")):
                params.pop("airfoil_dat_path", None)
            params.setdefault("airfoil_dat_path", extracted_path)
            return
    extracted_text = extract_airfoil_coordinate_text_from_text(params.get("airfoil_coordinate_text"))
    if extracted_text:
        params["airfoil_coordinate_text"] = extracted_text

"""CFD adapter boundary for the data node.

The caller/model selects a registered mesh type and supplies geometry through
the shared preprocessing contract. This module validates that selection and
delegates generation without loading a library of case-specific profiles.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from core.state import State
from .geometry_assets import COORDINATE_PROFILE_KEYS, GEOMETRY_FILE_KEYS, flatten_parameter_groups
from .human_input_utils import (
    caller_request_text,
    extract_naca_code as _extract_naca_code,
    has_valid_airfoil_geometry,
    is_placeholder_value,
    looks_like_public_reference_request,
    original_intent_allows_airfoil_mesh,
    original_user_request_text_from_state,
    pause_for_input as _pause_for_input,
    surrogate_geometry_explicitly_approved,
)
from .mesh_iteration_advisor import (
    CANONICAL_CFD_MESH_INTENT_PATTERNS,
    explicit_mesh_contract as _match_case,
    _resolve_mesh_iteration_inputs,
    geometry_parameters_need_reference_resolution,
    looks_like_turbomachinery_blade,
    public_geometry_reference_request,
)
from .mesh_generator import _generate_computational_mesh, _safe_case_dir_under_state


# The workspace contains packages for every scientific discipline.  CFD is a
# routing specialization, not the identity of the directory that owns shared
# hypotheses, audit records, and stage assets.
_PREPROCESSING_WORKSPACE_DIR = "mesh"


def openfoam_required_files_from_context(text: str) -> list[str]:
    names = [
        "0/U", "0/p", "constant/transportProperties",
        "constant/turbulenceProperties", "system/controlDict",
        "system/fvSchemes", "system/fvSolution",
    ]
    if re.search(r"\b(mesh|grid|blockMesh|snappyHexMesh|Gmsh|polyMesh)\b|网格", text, flags=re.I):
        names.insert(0, "system/blockMeshDict")
        names.append("constant/polyMesh")
    if re.search(r"\b(decomposePar|parallel|并行)\b", text, flags=re.I):
        names.append("system/decomposeParDict")
    return names


def merge_adapter_required_files(existing: list[dict[str, Any]], names: list[str]) -> list[dict[str, Any]]:
    result = list(existing)
    seen = {
        str(item.get("name_or_role") or item.get("id") or "").strip().lower()
        for item in result if isinstance(item, dict)
    }
    for name in names:
        key = name.strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        result.append({
            "id": re.sub(r"[^a-z0-9]+", "_", key).strip("_") or "required_file",
            "name_or_role": name,
            "format": "OpenFOAM dictionary or mesh asset",
            "reason": "Required by the registered CFD adapter.",
            "evidence": [{"source_type": "adapter", "detail": f"CFD adapter requires {name}."}],
            "confidence": 0.85,
            "consequences_if_missing": "The CFD preprocessing case cannot be prepared or validated.",
        })
    return result


def _default_preprocessing_workspace(state: State) -> str:
    """Return the run-local, discipline-neutral preprocessing workspace."""
    return str(Path(str(state.root)) / ".data_node_work" / _PREPROCESSING_WORKSPACE_DIR)


def _json_dumps(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True)


def _parse_json_object(raw: str) -> tuple[dict[str, Any], str | None]:
    if not raw:
        return {}, None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        return {}, f"parameters is not valid JSON: {exc}"
    if not isinstance(parsed, dict):
        return {}, "parameters must be a JSON object"
    return parsed, None


_CAD_GEOMETRY_FIELDS = GEOMETRY_FILE_KEYS
_PROFILE_GEOMETRY_FIELDS = COORDINATE_PROFILE_KEYS


def _needs_custom_geometry(case: dict[str, Any], params: dict[str, Any]) -> bool:
    if case.get("geometry_mode") != "user_file":
        return False
    return not bool(_provided_geometry_file(params) or _provided_profile_geometry(params))


def _provided_geometry_file(params: dict[str, Any]) -> str:
    for key in _CAD_GEOMETRY_FIELDS:
        value = params.get(key)
        if value and not is_placeholder_value(value):
            return str(value)
    return ""


def _provided_profile_geometry(params: dict[str, Any]) -> str:
    for key in _PROFILE_GEOMETRY_FIELDS:
        value = params.get(key)
        if value and not is_placeholder_value(value):
            return str(value)
    return ""


def _profile_mesh_type(spec: str, params: dict[str, Any]) -> str:
    requested = str(params.get("mesh_type") or "").strip().lower()
    if requested in {"coordinate_profile_gmsh", "coordinate_profile_cascade_gmsh"}:
        return requested
    if looks_like_turbomachinery_blade(spec, params) or _has_turbomachinery_cascade_domain(params):
        return "coordinate_profile_cascade_gmsh"
    return "coordinate_profile_gmsh"


async def _dispatch_profile_geometry(
    state: State,
    spec: str,
    params: dict[str, Any],
    case: dict[str, Any] | None,
    case_dir: str,
    generate_mesh: bool,
) -> dict[str, Any]:
    profile_path = _provided_profile_geometry(params)
    merged = {**((case or {}).get("defaults") or {}), **params}
    merged.setdefault("coordinate_profile_path", profile_path)
    mesh_type = _profile_mesh_type(spec, merged)
    merged["mesh_type"] = mesh_type
    if not generate_mesh:
        return {
            "status": "success",
            "case_type": (case or {}).get("case_type") or merged.get("case_type"),
            "mesh_type": mesh_type,
            "parameters": merged,
            "case_template": case,
            "geometry_priority": "coordinate_profile",
        }
    if not case_dir:
        case_dir = _default_preprocessing_workspace(state)
    case_dir = _safe_case_dir_under_state(state, case_dir, default_name=_PREPROCESSING_WORKSPACE_DIR)
    result = await _generate_computational_mesh(
        state,
        discipline="cfd",
        mesh_type=mesh_type,
        geometry=spec,
        case_dir=case_dir,
        parameters=_json_dumps(merged),
    )
    result.setdefault("case_type", (case or {}).get("case_type") or merged.get("case_type"))
    result.setdefault("case_template_source", (case or {}).get("_source"))
    result.setdefault("geometry_priority", "coordinate_profile")
    return result


def _canonical_case_matches_original_intent(
    state: State,
    case_type: str,
    mesh_type: str,
    intent_text: str,
    params: dict[str, Any],
) -> bool:
    if surrogate_geometry_explicitly_approved(params):
        return True
    pattern = (
        CANONICAL_CFD_MESH_INTENT_PATTERNS.get(str(case_type or "").strip().lower())
        or CANONICAL_CFD_MESH_INTENT_PATTERNS.get(str(mesh_type or "").strip().lower())
    )
    if not pattern:
        return True
    original = original_user_request_text_from_state(state)
    source_text = original or intent_text
    if looks_like_turbomachinery_blade(source_text or "", params):
        return False
    return bool(re.search(pattern, source_text or "", flags=re.IGNORECASE))


def _has_turbomachinery_cascade_domain(params: dict[str, Any]) -> bool:
    pitch_ok = any(params.get(key) not in (None, "", [], {}) for key in ("pitch", "pitch_chord_ratio", "blade_pitch"))
    inlet_outlet_ok = any(
        params.get(key) not in (None, "", [], {})
        for key in ("inlet_length", "outlet_length", "axial_chord", "upstream_length", "downstream_length")
    )
    periodic_ok = any(
        params.get(key) not in (None, "", [], {})
        for key in ("periodic_boundary_pairing", "periodic_patches", "cascade_periodic", "pitchwise_periodic")
    )
    return pitch_ok and inlet_outlet_ok and periodic_ok


def _extract_airfoil_name(spec: str, params: dict[str, Any]) -> str | None:
    if _extract_naca_code(
        spec,
        params.get("naca_code"),
        params.get("airfoil"),
        params.get("geometry"),
    ):
        return None
    generic_names = {
        "airfoil", "aerofoil", "profile", "the airfoil", "the aerofoil",
        "cfd airfoil", "cfd aerofoil",
        "airfoil profile", "aerofoil profile", "coordinate profile",
    }
    for key in ("airfoil_name", "airfoil", "geometry"):
        value = params.get(key)
        if isinstance(value, dict):
            if str(value.get("type") or "").lower() == "naca" or _extract_naca_code(value):
                continue
            value = value.get("airfoil_name") or value.get("name")
        if isinstance(value, str) and value.strip() and not _extract_naca_code(value):
            if value.strip().lower() in generic_names:
                continue
            if looks_like_turbomachinery_blade(value, params):
                continue
            return value.strip()
    profile_code = r"(?:[A-Z]{1,4}\s*[-_]?\s*\d{2,4}[A-Z0-9]{0,3}|DU\s*\d{2}\s*W\s*\d{2,4}|NLF\s*\d{3,5})"
    for keyword in re.finditer(r"\b(?:airfoil|aerofoil|profile)\b|翼型", spec, flags=re.IGNORECASE):
        window = spec[max(0, keyword.start() - 100): keyword.end() + 60]
        m = re.search(rf"\b({profile_code})\b", window, flags=re.IGNORECASE)
        if m and not _extract_naca_code(m.group(1)):
            candidate = re.sub(r"[\s_-]+", "", m.group(1)).upper()
            if not re.fullmatch(r"V\d{2,5}", candidate, flags=re.I):
                return candidate
    m = re.search(r"\b([A-Za-z][A-Za-z0-9_.-]*\s*[-_ ]?airfoil)\b", spec, flags=re.IGNORECASE)
    if m and not _extract_naca_code(m.group(1)):
        candidate = re.sub(r"\s+", " ", m.group(1)).strip()
        if candidate.lower() in generic_names:
            return None
        compact = re.sub(r"[^A-Za-z0-9]+", "", m.group(1))
        if re.search(r"\d{5,}", compact) or re.search(r"[A-Za-z]{5,}\d{4}airfoil$", compact, flags=re.I):
            return None
        return candidate
    return None


def _case_prompt(
    case: dict[str, Any],
    params: dict[str, Any],
    reason: str,
    input_kind: str = "cfd_case_parameters",
) -> dict[str, Any]:
    defaults = case.get("defaults") or {}
    template = {
        "case_type": case.get("case_type"),
        "mesh_type": case.get("mesh_type"),
        **defaults,
        **{k: v for k, v in params.items() if k not in {"case_type", "mesh_type"}},
    }
    guidance = case.get("required_guidance") or []
    customer_message = "\n".join([
        f"已识别为 CFD 标准算例：{case.get('case_type')}。",
        reason,
        "",
        "请在任务 prompt 或 parameters JSON 中补充/确认参数。",
        "关键说明：",
        *[f"- {item}" for item in guidance],
        "",
        "参数模板：",
        _json_dumps(template),
    ])
    payload = _pause_for_input(
        question="请补充该 CFD 网格算例所需的几何文件、边界命名或关键参数。",
        context=customer_message,
        metadata={
            "input_kind": input_kind,
            "case_type": case.get("case_type"),
            "mesh_type": case.get("mesh_type"),
            "parameter_template": template,
            "required_fields": guidance,
            "resume_instruction": (
                "把用户回答合并到 parameters；如果回答是文件路径，优先作为 geometry_file/"
                "airfoil_dat_path/cad_file，随后重新调用 prepare_scientific_mesh。"
            ),
        },
    )
    payload.update({
        "case_type": case.get("case_type"),
        "mesh_type": case.get("mesh_type"),
        "customer_message": customer_message,
        "required_fields": guidance,
        "parameter_template": template,
        "case_template": case,
    })
    return payload


async def _prepare_cfd_mesh_case(
    state: State,
    spec: str = "",
    case_type: str = "",
    case_dir: str = "",
    parameters: str = "",
    generate_mesh: bool = True,
    **extra: Any,
) -> dict[str, Any]:
    spec = caller_request_text(spec) or str(spec or "")
    params, parse_error = _parse_json_object(parameters)
    if parse_error:
        return {"status": "error", "error": parse_error}
    params = flatten_parameter_groups(params)
    for key, value in extra.items():
        if key not in params and value is not None:
            params[key] = value
    if not spec:
        spec = " ".join(
            str(params.get(key) or "")
            for key in ("case_type", "mesh_type", "airfoil_name", "geometry", "airfoil")
        ).strip()
    original_spec = original_user_request_text_from_state(state)
    intent_spec = "\n".join(part for part in (original_spec, spec) if part)
    requested_case = str(case_type or params.get("case_type") or params.get("mesh_type") or "").strip().lower()
    # Recovery/reference plans may carry the generic ``public_geometry``
    # template even after the caller supplied a complete analytic NACA
    # definition.  Treat the explicit parametric geometry as authoritative so
    # the generic reference template cannot reopen an unnecessary web search.
    naca_code = _extract_naca_code(
        spec,
        params.get("naca_code"),
        params.get("airfoil"),
        params.get("geometry"),
    )
    if (
        naca_code
        and requested_case in {"", "public_geometry", "generic_gmsh", "custom_geometry"}
        and original_intent_allows_airfoil_mesh(state, intent_spec or spec, params)
    ):
        requested_case = "airfoil"
        case_type = "airfoil"
        params["case_type"] = "airfoil"
        params["mesh_type"] = "airfoil_gmsh"
    if (
        requested_case in {"airfoil", "airfoil_gmsh", "airfoil_ogrid", "naca", "naca_airfoil"}
        or params.get("naca_code") not in (None, "", [], {})
    ) and not original_intent_allows_airfoil_mesh(state, spec, params):
        return public_geometry_reference_request(intent_spec, params, case_type="public_geometry")
    if not _canonical_case_matches_original_intent(state, requested_case, requested_case, intent_spec, params):
        return public_geometry_reference_request(intent_spec, params, case_type="public_geometry")
    if (
        looks_like_turbomachinery_blade(intent_spec, params)
        and str(params.get("isolated_blade_approximation_confirmed") or "").lower() not in {"1", "true", "yes"}
        and not _has_turbomachinery_cascade_domain(params)
    ):
        return public_geometry_reference_request(intent_spec, params, case_type="public_geometry")

    # A local/public coordinate profile is already a concrete geometry input.
    # Route it before template-specific airfoil/NACA checks so a downloaded
    # .dat/.txt/.xy profile cannot be mistaken for missing user geometry.
    if _provided_profile_geometry(params):
        return await _dispatch_profile_geometry(
            state=state,
            spec=intent_spec or spec,
            params=params,
            case=_match_case(spec, params, case_type=case_type),
            case_dir=case_dir,
            generate_mesh=generate_mesh,
        )

    case = _match_case(spec, params, case_type=case_type)
    if case is None:
        advisor_result = await _resolve_mesh_iteration_inputs(
            state,
            spec=spec,
            case_type=case_type,
            parameters=_json_dumps(params),
        )
        if advisor_result.get("status") in {"needs_input", "needs_reference_search", "needs_geometry_processing", "error"}:
            return advisor_result
        if advisor_result.get("status") == "success":
            params.update(advisor_result.get("resolved_parameters") or {})
            if _provided_profile_geometry(params):
                return await _dispatch_profile_geometry(
                    state=state,
                    spec=intent_spec or spec,
                    params=params,
                    case=None,
                    case_dir=case_dir,
                    generate_mesh=generate_mesh,
                )
        customer_message = (
            "当前请求尚未选择可执行的 mesh_type，也没有提供可识别的几何文件。\n"
            "请由规划模型根据原始需求选择已注册网格适配器，或补充 geometry_file "
            "（.step/.stp/.iges/.stl/.brep/.geo）和边界命名。"
        )
        payload = _pause_for_input(
            question="请补充 CFD 算例类型或几何文件路径/边界命名。",
            context=customer_message,
            metadata={
                "input_kind": "cfd_case_identification",
                "resume_instruction": (
                    "把用户回答解析为 mesh_type 或 geometry_file/cad_file，随后重新调用 "
                    "prepare_scientific_mesh。"
                ),
            },
        )
        payload.update({
            "customer_message": (
                "当前请求尚未选择可执行的 mesh_type，也没有提供可识别的几何文件。\n"
                "请由规划模型选择已注册网格适配器，或补充 geometry_file 和边界命名。"
            ),
        })
        return payload

    provided_geometry = _provided_geometry_file(params)
    if provided_geometry and geometry_parameters_need_reference_resolution(params):
        advisor_result = await _resolve_mesh_iteration_inputs(
            state,
            spec=spec,
            case_type=str(case.get("case_type") or case_type),
            parameters=_json_dumps(params),
        )
        if advisor_result.get("status") in {"needs_input", "needs_reference_search", "needs_geometry_processing", "error"}:
            return advisor_result
        if advisor_result.get("status") == "success":
            params.update(advisor_result.get("resolved_parameters") or {})
            provided_geometry = _provided_geometry_file(params)

    if _provided_profile_geometry(params):
        return await _dispatch_profile_geometry(
            state=state,
            spec=intent_spec or spec,
            params={**(case.get("defaults") or {}), **params},
            case=case,
            case_dir=case_dir,
            generate_mesh=generate_mesh,
        )

    if provided_geometry:
        merged = {**(case.get("defaults") or {}), **params}
        merged["geometry_file"] = provided_geometry
        merged["mesh_type"] = "geometry_file_gmsh"
        if not generate_mesh:
            return {
                "status": "success",
                "case_type": case.get("case_type"),
                "mesh_type": "geometry_file_gmsh",
                "parameters": merged,
                "case_template": case,
                "geometry_priority": "user_provided_geometry_file",
            }
        if not case_dir:
            case_dir = _default_preprocessing_workspace(state)
        case_dir = _safe_case_dir_under_state(state, case_dir, default_name=_PREPROCESSING_WORKSPACE_DIR)
        result = await _generate_computational_mesh(
            state,
            discipline="cfd",
            mesh_type="geometry_file_gmsh",
            geometry=spec,
            case_dir=case_dir,
            parameters=_json_dumps(merged),
        )
        result.setdefault("case_type", case.get("case_type"))
        result.setdefault("case_template_source", case.get("_source"))
        result.setdefault("geometry_priority", "user_provided_geometry_file")
        return result

    if case.get("geometry_mode") in {"public_reference_or_user_file", "public_reference_parametric"}:
        if case.get("geometry_mode") == "public_reference_or_user_file":
            return public_geometry_reference_request(spec, params, case_type="public_geometry")
        advisor_result = await _resolve_mesh_iteration_inputs(
            state,
            spec=spec,
            case_type=str(case.get("case_type") or case_type),
            parameters=_json_dumps(params),
        )
        if advisor_result.get("status") in {"success", "needs_input", "needs_reference_search", "needs_geometry_processing", "error"}:
            return advisor_result

    if _needs_custom_geometry(case, params):
        return public_geometry_reference_request(spec, params, case_type="public_geometry")

    defaults = case.get("defaults") or {}
    merged_params = {**defaults, **params}
    explicit_mesh_type = str(merged_params.get("mesh_type") or "").strip().lower()
    mesh_type = (
        explicit_mesh_type
        if explicit_mesh_type in {"airfoil_gmsh", "airfoil_ogrid"}
        else str(case.get("mesh_type") or explicit_mesh_type or "").strip()
    )
    # Domain shape and mesh family are separate contracts. A circular/O-shaped
    # farfield does not authorize replacing an unstructured Gmsh request with
    # the structured O-grid adapter; only an explicit mesh_type does.
    if not mesh_type or mesh_type == "custom_geometry":
        return _case_prompt(case, merged_params, "该算例模板尚未绑定可自动生成的网格生成器。")
    if not _canonical_case_matches_original_intent(
        state,
        str(case.get("case_type") or ""),
        mesh_type,
        intent_spec,
        merged_params,
    ):
        return public_geometry_reference_request(intent_spec, merged_params, case_type="public_geometry")

    if mesh_type in {"airfoil_gmsh", "airfoil_ogrid"}:
        if not original_intent_allows_airfoil_mesh(state, spec, merged_params):
            return public_geometry_reference_request(intent_spec, merged_params, case_type="public_geometry")
        for key in ("airfoil_dat_path", "airfoil_coordinate_text", "airfoil_coordinates"):
            if key in merged_params and is_placeholder_value(merged_params.get(key)):
                merged_params.pop(key, None)
        naca_code = _extract_naca_code(
            spec,
            merged_params.get("naca_code"),
            merged_params.get("airfoil"),
            merged_params.get("geometry"),
        )
        if naca_code:
            merged_params["naca_code"] = naca_code
        airfoil_name = _extract_airfoil_name(spec, merged_params)
        if airfoil_name:
            merged_params.setdefault("airfoil_name", airfoil_name)
        if (
            not has_valid_airfoil_geometry(merged_params)
            and (
                looks_like_turbomachinery_blade(intent_spec, merged_params)
                # A concrete NACA code is already a complete local geometry
                # contract. Ignore wrapper/KB words such as ``search`` in
                # that case; they must not reroute the request to acquisition.
                or (looks_like_public_reference_request(spec) and not naca_code)
            )
        ):
            return public_geometry_reference_request(intent_spec, merged_params, case_type="public_geometry")
        if airfoil_name and not naca_code and not has_valid_airfoil_geometry(merged_params):
            return _case_prompt(
                case,
                merged_params,
                "该请求指定了非 NACA 翼型，必须提供翼型坐标或几何文件。",
                input_kind="airfoil_geometry",
            )
        if not naca_code and not has_valid_airfoil_geometry(merged_params):
            return _case_prompt(
                case,
                merged_params,
                "该请求走翼型/NACA 网格路径，但缺少明确 naca_code 或真实坐标文件；data 节点不会默认回退到 NACA0012。",
                input_kind="airfoil_geometry",
            )
    if mesh_type == "airfoil_ogrid":
        if not naca_code:
            return _case_prompt(
                case,
                merged_params,
                "airfoil_ogrid 需要明确 naca_code；data 节点不会默认回退到 NACA0012。",
                input_kind="airfoil_geometry",
            )

    if not generate_mesh:
        if case_dir:
            case_dir = _safe_case_dir_under_state(state, case_dir, default_name=_PREPROCESSING_WORKSPACE_DIR)
        return {
            "status": "success",
            "case_type": case.get("case_type"),
            "mesh_type": mesh_type,
            "parameters": merged_params,
            "case_dir": case_dir,
            "case_template": case,
        }

    if not case_dir:
        case_dir = _default_preprocessing_workspace(state)
    case_dir = _safe_case_dir_under_state(state, case_dir, default_name=_PREPROCESSING_WORKSPACE_DIR)

    result = await _generate_computational_mesh(
        state,
        discipline="cfd",
        mesh_type=mesh_type,
        geometry=spec,
        case_dir=case_dir,
        parameters=_json_dumps(merged_params),
    )
    result.setdefault("case_type", case.get("case_type"))
    result.setdefault("case_template_source", case.get("_source"))
    return result

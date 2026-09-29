"""Decision support for mesh self-iteration and human input routing.

The tool in this module does not generate meshes.  It classifies missing or
ambiguous information during mesh setup/review into three buckets:

* can be resolved from local case profiles or public benchmark references;
* should be searched in literature/reference tools before asking the user;
* must be supplied or confirmed by the user because guessing would change the
  actual geometry, boundary semantics, or scientific intent.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from core.state import State
from nodes.data.pipeline_contract import needs_input_result
from .geometry_assets import GEOMETRY_INPUT_KEYS, flatten_parameter_groups
from .human_input_utils import (
    apply_mesh_density_from_text,
    has_mesh_density_parameters,
    mesh_density_requested,
    mesh_density_requires_confirmation,
    original_intent_allows_airfoil_mesh,
    original_user_request_text_from_state,
    surrogate_geometry_explicitly_approved,
)


_PRIVATE_GEOMETRY_FIELDS = GEOMETRY_INPUT_KEYS

_BOUNDARY_FIELDS = (
    "boundary_map",
    "patch_map",
    "inlet_patch",
    "outlet_patch",
    "wall_patches",
    "farfield_patch",
)

_FLOW_FIELDS = (
    "reynolds_number",
    "mach_number",
    "velocity",
    "inlet_velocity",
    "density",
    "viscosity",
    "kinematic_viscosity",
)

_UNIT_FIELDS = ("unit_system", "length_unit", "scale")

_GEOMETRY_INCOMPLETE_FLAGS = (
    "geometry_parameters_incomplete",
    "domain_parameters_incomplete",
    "boundary_parameters_incomplete",
    "mesh_parameters_incomplete",
    "needs_domain_parameters",
    "needs_boundary_parameters",
    "needs_unit_confirmation",
)

_FAST_MESH_REFERENCE_SEARCH_POLICY = {
    "preferred_external_tools": ["web_search", "web_download"],
    "max_queries_first_pass": 4,
    "web_search_defaults": {
        "limit": 5,
        "provider": "auto",
        "stop_after_first_success": True,
    },
    "download_only": [
        "direct geometry/coordinate/CAD/mesh/archive files",
        "repository or dataset pages with likely downloadable mesh-useful files",
    ],
    "do_not_download": ["ordinary papers", "abstract pages", "generic documentation pages"],
    "fallback_after_first_pass": ["approved_targeted_data_web_search"],
}

_AIRFOIL_CASE_VALUES = {
    "airfoil",
    "airfoil_ogrid",
    "airfoil_gmsh",
    "naca",
    "naca_airfoil",
    "wing",
    "aerofoil",
}

_AIRFOIL_SURROGATE_FIELDS = (
    "naca_code",
    "airfoil",
    "airfoil_name",
    "h_airfoil",
    "n_surface",
)

_TURBOMACHINERY_PATTERN = (
    r"\b(?:turbine|compressor|cascade|turbomachinery|vane)\b|"
    r"(?:涡轮|透平|压气机|叶栅)"
)

CANONICAL_CFD_MESH_INTENT_PATTERNS: dict[str, str] = {
    "airfoil_gmsh": r"\b(?:airfoil|aerofoil|naca\s*[-_ ]?\d{4,5})\b|(?:翼型|机翼)",
    "cylinder_flow": r"\b(?:cylinder|circular\s+cylinder)\b|(?:圆柱|圆柱绕流)",
    "cylinder_gmsh": r"\b(?:cylinder|circular\s+cylinder)\b|(?:圆柱|圆柱绕流)",
    "sphere": r"\bsphere\b|(?:球体|球绕流)",
    "pipe_flow": r"\b(?:pipe|pipe\s+flow)\b|(?:管道|管流)",
    "converging_diverging_nozzle": r"\bnozzle\b|(?:喷管|收敛[- ]?扩张)",
    "structured_rect": r"\b(?:structured_rect|rectangle|rectangular|channel|cavity)\b|(?:方腔|槽道|矩形)",
}


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


def _text_blob(spec: str, params: dict[str, Any], case_type: str = "") -> str:
    """Return scientific mesh intent without replaying recovery/search output.

    Caller-declared stages are the active task scope. Tool outputs can quote
    previous classifier errors; those texts are execution history, not the
    active mesh domain.
    """
    analysis = params.get("requirement_analysis")
    scoped_analysis: dict[str, Any] = {}
    use_scoped_spec = False
    if isinstance(analysis, dict):
        stages = [
            item for item in analysis.get("calculation_stages") or []
            if isinstance(item, dict)
        ]
        use_scoped_spec = bool(stages)
        if stages:
            active_roles = {
                re.sub(r"[^a-z0-9]+", "", str(role).lower())
                for stage in stages
                for role in stage.get("required_file_roles") or []
            }
            required_files = []
            for item in analysis.get("required_files") or []:
                if not isinstance(item, dict):
                    continue
                aliases = {
                    re.sub(r"[^a-z0-9]+", "", str(item.get(key) or "").lower())
                    for key in ("id", "name_or_role")
                } - {""}
                if not active_roles or any(
                    left == right or left in right or right in left
                    for left in active_roles for right in aliases
                ):
                    required_files.append(item)
            scoped_analysis = {
                "calculation_type": analysis.get("calculation_type"),
                "calculation_stages": stages,
                "required_files": required_files,
                "assumptions": analysis.get("assumptions") or [],
            }
    stable_params = {
        key: value for key, value in params.items()
        if key not in {
            "reference_evidence", "reference_notes", "search_queries", "search_policy",
            "recent_tool_results", "review_feedback", "requirement_analysis", "source_trace",
        }
    }
    payload = {
        "spec": "" if use_scoped_spec else spec,
        "case_type": case_type,
        "parameters": stable_params,
        "active_request_scope": scoped_analysis,
    }
    return _json_dumps(payload).lower()


def explicit_mesh_contract(
    spec: str,
    params: dict[str, Any],
    case_type: str = "",
) -> dict[str, Any] | None:
    """Resolve an explicit adapter or an unambiguous parametric mesh intent.

    Runtime routing does not depend on case profiles.  The adapter remains the
    sole owner of its defaults; this function only selects an already
    registered geometry family when the caller names it unambiguously.
    """
    mesh_type = str(
        params.get("mesh_type")
        or case_type
        or params.get("case_type")
        or ""
    ).strip()
    text = _text_blob(spec, params, case_type)
    matches = [
        candidate
        for candidate, pattern in CANONICAL_CFD_MESH_INTENT_PATTERNS.items()
        if re.search(pattern, text, flags=re.I)
    ]
    if not mesh_type or (
        matches and mesh_type.lower() not in CANONICAL_CFD_MESH_INTENT_PATTERNS
    ):
        inferred = ""
        if re.search(r"\bgmsh\b|\bunstructured\b|非结构|\.msh\b|\.geo\b", text, flags=re.I):
            inferred = next(
                (candidate for candidate in matches if candidate.endswith("_gmsh")),
                "",
            )
        mesh_type = inferred or (matches[0] if matches else mesh_type)
    if not mesh_type:
        return None
    return {
        "case_type": str(params.get("case_type") or case_type or mesh_type).strip(),
        "mesh_type": mesh_type,
        "defaults": {},
    }


def _profile_is_airfoil_like(profile: dict[str, Any] | None) -> bool:
    if not profile:
        return False
    values = {
        str(profile.get("case_type") or "").strip().lower(),
        str(profile.get("mesh_type") or "").strip().lower(),
        str(profile.get("case_family") or "").strip().lower(),
    }
    values.update(str(alias).strip().lower() for alias in profile.get("aliases", []) if alias)
    return bool(values & _AIRFOIL_CASE_VALUES)


def _geometry_value_is_naca_or_airfoil(value: Any) -> bool:
    if isinstance(value, dict):
        geometry_type = str(value.get("type") or "").strip().lower()
        if geometry_type in {"naca", "airfoil", "aerofoil", "wing"}:
            return True
        return any(value.get(key) not in (None, "", [], {}) for key in ("naca_code", "airfoil_name"))
    if isinstance(value, str):
        return bool(re.search(r"(\bnaca\b|\bairfoil\b|\baerofoil\b|\bwing\b|翼型|机翼)", value, re.IGNORECASE))
    return False


def _attempts_airfoil_resolution(
    case_type: str,
    params: dict[str, Any],
    local_case: dict[str, Any] | None = None,
    public_case: dict[str, Any] | None = None,
) -> bool:
    values = {
        str(case_type or "").strip().lower(),
        str(params.get("case_type") or "").strip().lower(),
        str(params.get("mesh_type") or "").strip().lower(),
    }
    if values & _AIRFOIL_CASE_VALUES:
        return True
    if _profile_is_airfoil_like(local_case) or _profile_is_airfoil_like(public_case):
        return True
    if any(params.get(field) not in (None, "", [], {}) for field in ("naca_code", "airfoil")):
        return True
    return _geometry_value_is_naca_or_airfoil(params.get("geometry"))


def _scrub_disallowed_airfoil_surrogate_params(params: dict[str, Any]) -> dict[str, Any]:
    """Remove model-invented airfoil/NACA surrogate fields from a non-airfoil task.

    If a path was provided through an airfoil-specific field, keep it as a
    generic geometry hint so the public-reference/HITL flow can still use it.
    """
    cleaned = dict(params)
    if cleaned.get("airfoil_dat_path") and not _has_any(cleaned, ("geometry_file", "geometry_path")):
        cleaned["geometry_file"] = cleaned.get("airfoil_dat_path")
    for field in _AIRFOIL_SURROGATE_FIELDS:
        cleaned.pop(field, None)
    if str(cleaned.get("mesh_type") or "").strip().lower() in _AIRFOIL_CASE_VALUES:
        cleaned["mesh_type"] = "generic_gmsh"
    if str(cleaned.get("case_type") or "").strip().lower() in _AIRFOIL_CASE_VALUES:
        cleaned["case_type"] = "public_geometry"
    geometry = cleaned.get("geometry")
    if _geometry_value_is_naca_or_airfoil(geometry):
        cleaned.pop("geometry", None)
    return cleaned


def _disallowed_airfoil_resolution_request(
    state: State,
    spec: str,
    case_type: str,
    params: dict[str, Any],
    local_case: dict[str, Any] | None = None,
    public_case: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    if not _attempts_airfoil_resolution(case_type, params, local_case, public_case):
        return None
    current_text = "\n".join(
        text
        for text in (
            spec,
            case_type,
            str(params.get("case_type") or ""),
            str(params.get("mesh_type") or ""),
        )
        if text
    )
    if original_intent_allows_airfoil_mesh(state, current_text=current_text, params=params):
        return None

    original_spec = original_user_request_text_from_state(state)
    intent_spec = "\n".join(text for text in (original_spec, spec) if text)
    cleaned = _scrub_disallowed_airfoil_surrogate_params(params)
    result = public_geometry_reference_request(intent_spec or spec, cleaned, case_type=str(cleaned.get("case_type") or ""))
    result["message"] = (
        "原始任务不是机翼/翼型/NACA 网格，当前参数却尝试解析为 airfoil/NACA。"
        "节点已拒绝跨几何角色替代，并转入同类公开几何/参数检索流程。"
    )
    result["blocked_surrogate"] = {
        "reason": "airfoil_or_naca_surrogate_not_allowed_by_original_intent",
        "attempted_case_type": case_type or params.get("case_type"),
        "attempted_mesh_type": params.get("mesh_type"),
        "attempted_local_profile": (local_case or {}).get("case_type"),
        "attempted_public_profile": (public_case or {}).get("case_type"),
    }
    result.setdefault("source_trace", []).append({
        "source": "original_intent_guard",
        "action": "rejected_airfoil_or_naca_surrogate",
    })
    return result


def _disallowed_canonical_resolution_request(
    state: State,
    spec: str,
    case_type: str,
    params: dict[str, Any],
    local_case: dict[str, Any] | None = None,
    public_case: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    if surrogate_geometry_explicitly_approved(params):
        return None
    values = [
        str(case_type or "").strip().lower(),
        str(params.get("case_type") or "").strip().lower(),
        str(params.get("mesh_type") or "").strip().lower(),
        str((local_case or {}).get("case_type") or "").strip().lower(),
        str((local_case or {}).get("mesh_type") or "").strip().lower(),
        str((public_case or {}).get("case_type") or "").strip().lower(),
        str((public_case or {}).get("mesh_type") or "").strip().lower(),
    ]
    matched_value = next((value for value in values if value in CANONICAL_CFD_MESH_INTENT_PATTERNS), "")
    if not matched_value:
        return None
    original = original_user_request_text_from_state(state)
    source_text = original or spec
    if looks_like_turbomachinery_blade(source_text or "", params):
        matched_pattern = False
    else:
        matched_pattern = bool(
            re.search(CANONICAL_CFD_MESH_INTENT_PATTERNS[matched_value], source_text or "", flags=re.IGNORECASE)
        )
    if matched_pattern:
        return None

    cleaned = dict(params)
    cleaned["case_type"] = "public_geometry"
    cleaned["mesh_type"] = "generic_gmsh"
    result = public_geometry_reference_request("\n".join(part for part in (original, spec) if part), cleaned, case_type="public_geometry")
    result["message"] = (
        "原始任务不是该常用参数化几何，当前参数却尝试解析为 "
        f"{matched_value}。节点已拒绝跨几何角色替代，并转入同类公开几何/参数检索流程。"
    )
    result["blocked_surrogate"] = {
        "reason": "canonical_geometry_surrogate_not_allowed_by_original_intent",
        "attempted_geometry": matched_value,
        "attempted_case_type": case_type or params.get("case_type"),
        "attempted_mesh_type": params.get("mesh_type"),
    }
    result.setdefault("source_trace", []).append({
        "source": "original_intent_guard",
        "action": "rejected_canonical_geometry_surrogate",
    })
    return result


def _public_search_queries(spec: str, params: dict[str, Any], profile: dict[str, Any] | None) -> list[str]:
    topology_queries = (
        (
            _TURBOMACHINERY_PATTERN,
            [
                "public turbine cascade blade profile coordinates dat",
                "low pressure turbine cascade geometry coordinates dataset download",
                "turbomachinery cascade blade coordinates mesh benchmark",
                "turbine vane cascade profile coordinates repository",
            ],
        ),
        (
            r"\b(?:rotating|rotation|sliding\s+mesh|mrf|ami|impeller|fan|propeller|rotor)\b|"
            r"(?:旋转|滑移网格|叶轮|风扇|螺旋桨|动静)",
            [
                "public rotating machinery CFD geometry mesh dataset",
                "impeller fan propeller CFD geometry CAD mesh download",
                "MRF AMI rotating region OpenFOAM benchmark geometry",
                "rotor stator interface mesh benchmark geometry file",
            ],
        ),
        (
            r"\b(?:internal\s+flow|passage|duct|diffuser|combustor|manifold|heat\s+exchanger|valve|nozzle)\b|"
            r"(?:内流|流道|通道|扩压器|燃烧室|歧管|换热器|阀|喷管)",
            [
                "public internal flow geometry CAD mesh dataset",
                "duct diffuser nozzle valve CFD benchmark geometry mesh",
                "internal passage OpenFOAM mesh geometry download",
                "heat exchanger manifold CFD geometry mesh file",
            ],
        ),
        (
            r"\b(?:multi[- ]?region|cht|conjugate\s+heat\s+transfer|fluid[- ]?solid|fsi)\b|"
            r"(?:多区域|共轭传热|流固|固流|多物理)",
            [
                "public multi region CHT CFD geometry mesh benchmark",
                "conjugate heat transfer OpenFOAM case geometry mesh",
                "fluid solid interface CFD mesh dataset download",
                "multi region CFD CAD geometry mesh file",
            ],
        ),
        (
            r"\b(?:free\s+surface|vof|multiphase|two[- ]?phase|waterline|wave\s+tank)\b|"
            r"(?:自由液面|多相|两相|波浪|水面)",
            [
                "public free surface VOF CFD geometry mesh benchmark",
                "wave tank CFD geometry mesh dataset download",
                "multiphase OpenFOAM case geometry mesh file",
                "two phase flow benchmark domain boundary mesh parameters",
            ],
        ),
        (
            r"\bcylinder\b|(?:圆柱|圆柱绕流)",
            [
                "flow around cylinder CFD benchmark domain mesh parameters",
                "cylinder wake OpenFOAM mesh geometry benchmark",
            ],
        ),
        (
            r"\b(?:lid\s+driven\s+cavity|cavity|backward\s+facing\s+step|backstep|channel)\b|"
            r"(?:槽道|方腔|后台阶|后向台阶)",
            [
                "canonical CFD benchmark geometry domain mesh parameters",
                "backward facing step cavity channel OpenFOAM mesh benchmark",
            ],
        ),
    )

    def compact_hint(value: Any) -> str:
        text = " ".join(str(value or "").split())
        text = re.sub(r"##.*", " ", text)
        text = re.sub(r"(manifest|OpenFOAM|Tecplot|system|0/U|0/p|前处理包|其他参数你可以自己决定)", " ", text, flags=re.I)
        text = " ".join(text.split())
        return text[:120]

    specific_name = compact_hint(params.get("geometry") or params.get("profile_name") or params.get("airfoil_name"))
    if not specific_name or specific_name.lower() in {"public_geometry", "generic_gmsh", "cascade", "turbine_cascade"}:
        specific_name = ""
    base = specific_name or compact_hint(spec) or str((profile or {}).get("case_type") or "geometry").strip()
    raw_queries = list((profile or {}).get("search_queries") or [])
    text_blob = _text_blob(spec, params, str((profile or {}).get("case_type") or ""))
    matched_topology_queries: list[str] = []
    for pattern, queries_for_topology in topology_queries:
        if re.search(pattern, text_blob, flags=re.IGNORECASE):
            matched_topology_queries = list(queries_for_topology)
            break
    if specific_name:
        raw_queries.extend([
            f"{specific_name} geometry coordinates CAD mesh file download",
            f"{specific_name} CFD benchmark domain boundary mesh parameters",
            f"{specific_name} repository dataset geometry mesh",
        ])
    raw_queries.extend(matched_topology_queries)
    raw_queries.extend([
        f"{base} geometry coordinates mesh file download",
        f"{base} benchmark domain boundary mesh parameters",
        "public CFD benchmark geometry coordinates CAD mesh dataset",
    ])
    seen: set[str] = set()
    queries: list[str] = []
    for query in raw_queries:
        cleaned = " ".join(str(query).split())
        cleaned = re.sub(
            r"(manifest|OpenFOAM|Tecplot|system files?|0/U|0/p|前处理包|可复现|其他参数你可以自己决定)",
            " ",
            cleaned,
            flags=re.I,
        )
        cleaned = " ".join(cleaned.split())[:160]
        if cleaned and cleaned.lower() not in seen:
            seen.add(cleaned.lower())
            queries.append(cleaned)
        if len(queries) >= 4:
            break
    return queries


def looks_like_turbomachinery_blade(spec: str, params: dict[str, Any] | None = None) -> bool:
    params = params or {}
    text = _text_blob(spec, params)
    return bool(
        re.search(
            _TURBOMACHINERY_PATTERN,
            text,
            flags=re.IGNORECASE,
        )
        and not re.search(r"(wing|机翼)", text, flags=re.IGNORECASE)
    )


def public_geometry_reference_request(
    spec: str,
    params: dict[str, Any] | None = None,
    case_type: str = "",
) -> dict[str, Any]:
    params = params or {}
    profile = {
        "case_type": case_type or params.get("case_type") or "public_geometry",
        "mesh_type": params.get("mesh_type") or "generic_gmsh",
        "public_reference_hint": "public geometry/profile parameters with citation",
        "suggested_parameters": {},
    }
    resolved = dict(profile.get("suggested_parameters") or {})
    resolved.update({k: v for k, v in params.items() if v not in (None, "", [], {})})
    resolved.setdefault("case_type", profile.get("case_type"))
    resolved.setdefault("mesh_type", profile.get("mesh_type"))
    has_local_geometry = _has_any(resolved, _PRIVATE_GEOMETRY_FIELDS)
    if has_local_geometry:
        message = (
            "已收到几何文件或几何线索，但缺少外围计算域、边界语义、单位/尺度、"
            "工况或网格目标等配套参数。请先检索公开 benchmark、论文、算例说明或几何文件附带文档；"
            "不要重复询问同一个几何文件。"
        )
        next_action = (
            "首轮只调用 search_kb/data_web_search 检索公开域尺寸、边界条件和可下载几何/网格文件；"
            "只对直接几何/坐标/CAD/mesh/archive 文件或高相关数据仓库页面调用 data_web_download；"
            "单位/尺度和网格设置；将候选来源和摘要写入 reference_notes 后再次调用 "
                "prepare_scientific_mesh(operation='resolve')。只有首轮没有可用文件/参数时，才在 approved plan 的 "
                "targeted_search_requests 中继续调用 data_web_search；若仍无法找到可靠来源，再通过 human-in-the-loop "
            "询问用户确认缺失字段。"
        )
    else:
        message = (
            "用户没有本地几何文件。请在已锁定的任务范围内先检索公开来源，"
            "不要重复询问几何文件；找到坐标/参数后必须记录来源并用于后续网格生成。"
        )
        next_action = (
            "首轮只调用 search_kb/data_web_search 检索公开几何坐标、CAD 或 mesh 文件；"
            "只对直接几何/坐标/CAD/mesh/archive 文件或高相关数据仓库页面调用 data_web_download；"
            "将候选来源、坐标摘要和引用放入 reference_notes 后再次调用 "
            "prepare_scientific_mesh(operation='resolve')。只有首轮没有可用文件/参数时，才在 approved plan 的 "
            "targeted_search_requests 中继续调用 data_web_search；若无法找到可复现坐标，再通过 human-in-the-loop "
            "请用户确认采用哪个公开来源或提供本地几何。"
        )
    return {
        "status": "needs_reference_search",
        "reference_request": {
            "id": f"geometry_reference_{profile.get('case_type') or case_type or 'public'}",
            "missing": "public geometry or mesh reference",
            "query": (_public_search_queries(spec, params, profile) or ["authoritative public geometry reference"])[0],
            "tool": "data_web_search",
            "web_search_allowed": True,
            "search_mode": "official_geometry_reference",
            "asset_kind": "geometry_or_mesh",
            "asset_role": "official_file_reference",
            "workflow_capability": "geometry_acquisition",
            # Public geometry is a model-selected dependency.  It is not an
            # exact-file contract unless the caller supplied a filename,
            # revision, or locator.  Keep the provenance/review gates, but
            # allow the search tool to select a direct geometry asset.
            "requires_exact_file": False,
            "auto_discover_downloads": True,
            "max_discovery_pages": 1,
        },
        "message": message,
        "case_type": profile.get("case_type"),
        "case_family": profile.get("case_family"),
        "mesh_type": profile.get("mesh_type"),
        "resolved_parameters": resolved,
        "search_queries": _public_search_queries(spec, params, profile),
        "recommended_tools": ["search_kb", "data_web_search", "data_web_download"],
        "fallback_tools": ["approved_targeted_data_web_search"],
        "search_policy": dict(_FAST_MESH_REFERENCE_SEARCH_POLICY),
        "next_action": next_action,
        "source_trace": [
            {
                "source": "public_reference_requested_by_user",
                "case_type": profile.get("case_type"),
                "case_family": profile.get("case_family"),
                "hint": profile.get("public_reference_hint"),
            }
        ],
    }


def _has_any(params: dict[str, Any], fields: tuple[str, ...] | list[str]) -> bool:
    return any(params.get(field) not in (None, "", [], {}) for field in fields)


def _as_true(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on", "确认", "需要"}


_SPECIAL_DOMAIN_TOPOLOGIES: tuple[dict[str, Any], ...] = (
    {
        "topology": "periodic_or_repeating_passage",
        "description": "周期/重复通道或叶排/阵列类流动",
        "pattern": r"(\b(?:periodic|cyclic|pitchwise|cascade|blade row|stator|rotor row|annular passage)\b|重复|周期|叶栅|叶排|环形通道)",
        "fields": (
            ("periodic_boundary_pairing", "periodic_patches", "cascade_periodic", "pitchwise_periodic"),
            ("pitch", "pitch_chord_ratio", "blade_pitch", "periodic_pitch", "passage_pitch"),
            ("inlet_length", "outlet_length", "upstream_length", "downstream_length", "fore_domain_length", "aft_domain_length", "inlet_extent", "outlet_extent"),
        ),
    },
    {
        "topology": "rotating_or_sliding_region",
        "description": "旋转机械、滑移网格、MRF/AMI 类流动",
        "pattern": r"(\b(?:rotating|rotation|sliding mesh|mrf|ami|impeller|fan|propeller|rotor)\b|旋转|滑移网格|叶轮|风扇|螺旋桨|动静)",
        "fields": (
            ("rotating_region", "mrf_zone", "ami_interface", "sliding_interface", "rotor_region"),
            ("rotation_axis", "angular_velocity", "rpm"),
            ("stationary_region", "interface_patches", "rotor_stator_interface"),
        ),
    },
    {
        "topology": "internal_passage_or_device",
        "description": "内部通道、装置或有入口/出口的真实流道",
        "pattern": r"(\b(?:internal flow|passage|duct|diffuser|combustor|manifold|heat exchanger|valve)\b|\bnozzle.{0,40}geometry\b|内流|流道|通道|扩压器|燃烧室|歧管|换热器|阀|真实喷管)",
        "fields": (
            ("inlet_patch", "inlet_patches", "inlet_name"),
            ("outlet_patch", "outlet_patches", "outlet_name"),
            ("wall_patches", "wall_patch", "solid_walls", "no_slip_walls"),
        ),
    },
    {
        "topology": "multi_region_or_conjugate",
        "description": "多区域/共轭传热/流固或多物理耦合",
        "pattern": r"(\b(?:multi[- ]?region|cht|conjugate heat transfer|fluid[- ]?solid|fsi)\b|多区域|共轭传热|流固|固流|多物理)",
        "fields": (
            ("regions", "region_map", "fluid_region", "solid_region"),
            ("interfaces", "interface_map", "coupled_interfaces"),
            ("materials", "material_map", "solid_materials"),
        ),
    },
    {
        "topology": "free_surface_or_multiphase",
        "description": "自由液面/多相/VOF 类流动",
        "pattern": r"(\b(?:free surface|vof|multiphase|two[- ]?phase|waterline|wave tank)\b|自由液面|多相|两相|波浪|水面)",
        "fields": (
            ("phase_boundary", "free_surface_region", "initial_water_level", "interface_location"),
            ("atmosphere_patch", "pressure_outlet_patch", "open_boundary_patch"),
            ("domain_height", "domain_depth", "tank_length", "tank_height"),
        ),
    },
)


def _missing_field_groups(params: dict[str, Any], groups: tuple[tuple[str, ...], ...]) -> list[str]:
    missing: list[str] = []
    for group in groups:
        if not _has_any(params, group):
            missing.append(" 或 ".join(group))
    return missing


def _periodic_text_is_domain_topology(text: str) -> bool:
    """Distinguish periodic/cyclic boundaries from temporal periodic forcing.

    Scientific plans often mention periodic perturbations, excitation, sampling,
    or signals.  Those are time/frequency semantics and must not force a
    repeating-passage mesh unless the text also carries domain/boundary cues.
    """
    if re.search(
        r"(cascade|blade row|stator|rotor row|annular passage|pitchwise|cyclic\s+patch|"
        r"periodic\s+(boundary|patch|pairing|domain|passage|mesh)|"
        r"(boundary|patch|pairing|domain|passage|mesh|pitch)\s+periodic|"
        r"叶栅|叶排|环形通道|周期边界|周期.{0,8}(边界|通道|配对|节距)|"
        r"(边界|通道|配对|节距).{0,8}周期)",
        text,
        flags=re.IGNORECASE,
    ):
        return True
    if re.search(
        r"(periodic|周期).{0,24}(perturbation|forcing|excitation|disturbance|signal|"
        r"sampling|oscillation|frequency|wall\s+blowing|suction|扰动|激励|信号|采样|振荡|频率|吹吸)",
        text,
        flags=re.IGNORECASE,
    ):
        return False
    return False


def detect_special_domain_requirements(
    spec: str,
    params: dict[str, Any] | None = None,
    case_type: str = "",
    include_complete: bool = False,
) -> dict[str, Any] | None:
    """Detect non-farfield topologies that need domain/boundary semantics.

    This is intentionally role/topology based, not case-name based.  It blocks
    using isolated-airfoil C-domain/profile farfield meshes for periodic,
    rotating, internal, multiregion, or free-surface CFD requests unless the
    caller has supplied the topology-defining fields or explicitly approved a
    non-physical surrogate.
    """
    params = params or {}
    if surrogate_geometry_explicitly_approved(params):
        return None
    text = _text_blob(spec, params, case_type)
    for rule in _SPECIAL_DOMAIN_TOPOLOGIES:
        if not re.search(str(rule["pattern"]), text, flags=re.IGNORECASE):
            continue
        if str(rule.get("topology")) == "periodic_or_repeating_passage" and not _periodic_text_is_domain_topology(text):
            continue
        missing = _missing_field_groups(params, rule["fields"])
        if not missing and not include_complete:
            return None
        topology = str(rule["topology"])
        description = str(rule["description"])
        base = str(params.get("case_type") or params.get("geometry") or spec or topology)
        return {
            "topology": topology,
            "description": description,
            "missing_fields": missing,
            "complete": not missing,
            "message": (
                f"该请求属于{description}，计算域/边界拓扑不能用孤立机翼 C-domain 或普通 farfield profile 替代。"
                "请先公开检索对应 benchmark/论文/几何说明补齐这些域参数；检索不到再进入 human-in-the-loop。"
            ),
            "search_queries": [
                f"{base} CFD computational domain boundary conditions {topology}",
                f"{base} mesh domain parameters benchmark",
                f"{base} OpenFOAM boundary patches mesh",
            ],
        }
    return None


def special_domain_reference_request(
    spec: str,
    params: dict[str, Any],
    requirement: dict[str, Any],
) -> dict[str, Any]:
    return {
        "status": "needs_reference_search",
        "reference_request": {
            "id": f"special_domain_reference_{requirement.get('topology') or 'domain'}",
            "missing": ", ".join(requirement.get("missing_fields") or []) or "special-domain reference",
            "query": (requirement.get("search_queries") or ["authoritative special-domain reference"])[0],
            "tool": "data_web_search",
            "web_search_allowed": True,
            "search_mode": "official_geometry_reference",
            "asset_kind": "geometry_or_mesh",
            "asset_role": "official_file_reference",
            "workflow_capability": "geometry_acquisition",
            "requires_exact_file": False,
            "auto_discover_downloads": True,
            "max_discovery_pages": 1,
        },
        "message": requirement.get("message"),
        "case_type": str(params.get("case_type") or "special_domain_cfd"),
        "topology": requirement.get("topology"),
        "missing_fields": requirement.get("missing_fields") or [],
        "resolved_parameters": dict(params),
        "search_queries": (requirement.get("search_queries") or [])[: _FAST_MESH_REFERENCE_SEARCH_POLICY["max_queries_first_pass"]],
        "recommended_tools": ["data_web_search", "data_web_download", "prepare_scientific_mesh"],
        "fallback_tools": list(_FAST_MESH_REFERENCE_SEARCH_POLICY["fallback_after_first_pass"]),
        "search_policy": dict(_FAST_MESH_REFERENCE_SEARCH_POLICY),
        "next_action": (
            "首轮只检索同一物理拓扑的公开几何/计算域/边界条件资料；只下载几何/坐标/CAD/mesh/archive "
            "或高相关数据仓库页面。如果只能找到通用网页或无可复现参数，"
            "把检索摘要写入 reference_notes 后再次调用 prepare_scientific_mesh(operation='resolve')，由工具触发 HITL。"
        ),
        "do_not_suggest": [
            "不要建议改用 NACA/机翼 C-domain",
            "不要建议改用圆柱/方腔/槽道等不同物理拓扑",
            "不要保存 pending 或不合格 dataset artifact",
        ],
    }


def geometry_parameters_need_reference_resolution(params: dict[str, Any]) -> bool:
    """Return True when geometry exists but supporting mesh parameters are incomplete.

    The unified mesh service should run its resolve operation first, so public sources
    get a chance to provide domain, boundary, unit, and mesh-target parameters
    before human-in-the-loop is used.
    """
    geometry_path = ""
    for key in _PRIVATE_GEOMETRY_FIELDS:
        value = params.get(key)
        if isinstance(value, str) and value.strip():
            geometry_path = value.strip()
            break
    representation = str(params.get("geometry_representation") or "").strip().lower()
    suffix = Path(geometry_path).suffix.lower() if geometry_path else ""
    if suffix in {".cas", ".cgns", ".foam"} or (
        suffix == ".msh"
        and representation in {"reference_computational_mesh", "computational_mesh", "fluid_domain_mesh"}
    ):
        return False
    if not _has_any(params, _PRIVATE_GEOMETRY_FIELDS):
        return False
    if any(_as_true(params.get(flag)) for flag in _GEOMETRY_INCOMPLETE_FLAGS):
        return True
    if _as_true(params.get("require_boundary_confirmation")) and not _has_any(params, _BOUNDARY_FIELDS):
        return True
    if _as_true(params.get("require_unit_confirmation")) and not _has_any(params, _UNIT_FIELDS):
        return True
    return False


def _looks_like_private_or_custom_geometry(spec: str, params: dict[str, Any]) -> bool:
    if _has_any(params, _PRIVATE_GEOMETRY_FIELDS):
        return True
    text = _text_blob(spec, params)
    return bool(re.search(r"\b(step|stp|iges|igs|stl|brep|cad|自定义|复杂几何|真实几何)\b", text))


def _reference_notes_indicate_search_failure(reference_notes: str) -> bool:
    text = str(reference_notes or "").strip().lower()
    if not text:
        return False
    return bool(
        re.search(
            r"(未找到|没有找到|找不到|无可靠|不可靠|检索失败|搜索失败|"
            r"没有可下载|无可下载|未返回可下载|未能.*下载|下载失败|无可用|无.*坐标|"
            r"未获得.*坐标|未能.*获取.*坐标|未能.*获得.*坐标|未获取.*几何|"
            r"只有通用|通用网页|无关结果|"
            r"限流|429|rate.?limit|rate limited|http 429|"
            r"no reliable|not found|no geometry|no coordinates|no downloadable|download failed|"
            r"no usable|generic only|generic pages|search failed|insufficient|not downloadable|"
            r"unavailable|failed or unavailable)",
            text,
            flags=re.IGNORECASE,
        )
    )


def _reference_notes_lack_usable_geometry(reference_notes: str) -> bool:
    """Return True when search found hints but no reproducible geometry asset.

    Search snippets often identify a benchmark page, but a mesh generator needs
    concrete coordinates/CAD/mesh files or enough numeric geometry parameters.
    Treat "source found, coordinates still unavailable" as a HITL condition
    instead of allowing public defaults to be promoted to a completed mesh plan.
    """
    text = str(reference_notes or "").strip().lower()
    if not text:
        return False
    return bool(
        re.search(
            r"(无法自动获取|无法获取|不能自动获取|需要进一步下载|需要.*解析|"
            r"真实几何应.*获取|真实.*坐标.*未|未获得.*可复现|无具体\s*profile|没有具体\s*profile|"
            r"未指定具体叶片|未指定具体.*profile|缺少.*profile|缺少.*坐标|"
            r"no specific profile|no concrete profile|no usable coordinates|"
            r"coordinates.*not.*retriev|geometry.*not.*retriev|"
            r"need.*download|must.*download|source.*only|hint.*only)",
            text,
            flags=re.IGNORECASE,
        )
    )


def _reference_notes_need_coordinate_profile_build(reference_notes: str) -> bool:
    text = str(reference_notes or "").strip().lower()
    if not text:
        return False
    return bool(
        re.search(
            r"(separate surface|separate.*points|pressure side|suction side|upper.*lower|"
            r"分开.*坐标|两面.*数据|压力面|吸力面|需要.*合并|requiring combination|"
            r"combine.*profile|closed profile)",
            text,
            flags=re.IGNORECASE,
        )
    )


def _geometry_policy_requires_user_file(params: dict[str, Any], reference_notes: str = "") -> bool:
    """Return True when the flow explicitly requires a real geometry asset.

    This is intentionally generic: it applies to any complex/public geometry,
    not only turbomachinery.  Numeric flow/domain assumptions can be defaulted,
    but a missing geometry file cannot be converted into a different physical
    shape unless the user explicitly approves a nonphysical surrogate.
    """
    policy = str(params.get("geometry_source_policy") or params.get("geometry_policy") or "").strip().lower()
    if policy in {
        "must_provide_file",
        "user_provided_file",
        "requires_geometry_file",
        "geometry_file_required",
        "must_provide_geometry",
    }:
        return True
    text = " ".join(str(value or "") for value in (reference_notes, params.get("geometry_status"), params.get("mesh_status")))
    return bool(
        re.search(
            r"(必须.*提供.*几何|需要.*几何文件|缺少.*几何文件|geometry file required|"
            r"must provide.*geometry|requires.*geometry file)",
            text,
            flags=re.IGNORECASE,
        )
    )


def _review_issues_missing_turbomachinery_domain(review_issues: str) -> list[str]:
    text = str(review_issues or "")
    missing: list[str] = []
    if re.search(r"(missing_required_has_pitch|has_pitch|pitch)", text, flags=re.IGNORECASE):
        missing.append("pitch / pitch_chord_ratio / blade_pitch")
    if re.search(r"(missing_required_has_inlet_outlet_extent|inlet_outlet|upstream|downstream|fore_domain|aft_domain)", text, flags=re.IGNORECASE):
        missing.append("inlet/outlet extent")
    if re.search(r"(missing_required_has_periodic_pairing|periodic|cyclic|pitchwise)", text, flags=re.IGNORECASE):
        missing.append("periodic boundary pairing")
    return missing


def _build_pause_payload(
    question: str,
    context: str,
    required_fields: list[dict[str, str]],
    example_reply: str,
    metadata: dict[str, Any],
) -> dict[str, Any]:
    full_context = "\n".join([
        context,
        "",
        "请补充或确认以下信息：",
        *[f"- {item['field']}: {item['why']}" for item in required_fields],
        "",
        "可直接回复示例：",
        example_reply,
    ])
    result = needs_input_result(
        question=question,
        context=full_context,
        missing_fields=[item.get("field", "") for item in required_fields],
        metadata={
            "input_kind": "mesh_iteration_decision",
            "example_reply": example_reply,
            "resume_instruction": (
                "Merge the supplied geometry, boundary, unit, physical-condition, or quality values into "
                "the same preprocessing request, then resume the affected step."
            ),
            **metadata,
        },
    )
    result.update({
        "customer_message": full_context,
        "required_fields": required_fields,
        "example_reply": example_reply,
    })
    return result


def _surrogate_geometry_warning() -> str:
    return (
        "不得建议使用与原任务几何角色或流动拓扑不一致的替代几何。"
        "只有用户明确设置 surrogate_geometry_approved=true，并同时给出 surrogate_geometry、"
        "surrogate_reason 和 nonphysical_approximation=true 时，才允许生成非物理替代/可视化网格；"
        "否则只能要求同类几何文件、同类公开来源或候选来源确认。"
    )


def _build_missing_parameter_reference_request(
    spec: str,
    params: dict[str, Any],
    missing_fields: list[dict[str, str]],
    case_profile: dict[str, Any] | None,
    public_profile: dict[str, Any] | None,
    case_type: str = "",
) -> dict[str, Any]:
    case_label = str(
        (case_profile or public_profile or {}).get("case_type")
        or case_type
        or params.get("case_type")
        or "custom_geometry"
    )
    field_names = [str(item.get("field") or "") for item in missing_fields]
    geometry_name = (
        params.get("geometry_file")
        or params.get("geometry_path")
        or params.get("cad_file")
        or params.get("airfoil_dat_path")
        or params.get("blade_profile_path")
        or spec
    )
    base_queries = list((public_profile or {}).get("search_queries") or [])
    search_terms = " ".join(field_names) or "domain boundary mesh parameters"
    queries = [
        *base_queries,
        f"{geometry_name} CFD mesh domain boundary conditions parameters",
        f"{geometry_name} geometry mesh {search_terms}",
        f"{spec} benchmark mesh domain boundary parameters",
    ]
    seen: set[str] = set()
    deduped_queries: list[str] = []
    for query in queries:
        cleaned = " ".join(str(query).split())
        if cleaned and cleaned.lower() not in seen:
            seen.add(cleaned.lower())
            deduped_queries.append(cleaned)

    resolved = dict(params)
    if case_profile:
        resolved = {**(case_profile.get("defaults") or {}), **resolved}
        resolved.setdefault("case_type", case_profile.get("case_type"))
        resolved.setdefault("mesh_type", case_profile.get("mesh_type"))
    if public_profile:
        for key, value in (public_profile.get("suggested_parameters") or {}).items():
            resolved.setdefault(key, value)
        resolved.setdefault("case_type", public_profile.get("case_type"))
        resolved.setdefault("case_family", public_profile.get("case_family"))
        resolved.setdefault("mesh_type", public_profile.get("mesh_type"))

    return {
        "status": "needs_reference_search",
        "message": (
            "已收到几何文件或几何线索，但网格生成还缺少域尺寸、边界语义、单位、工况或网格目标等参数。"
            "请先检索公开 benchmark、论文、算例说明或几何文件附带文档来补齐这些参数；"
            "不要直接重复询问用户。若检索不到可靠来源，再进入 human-in-the-loop。"
        ),
        "case_type": case_label,
        "case_family": (public_profile or {}).get("case_family"),
        "resolved_parameters": resolved,
        "missing_fields": missing_fields,
        "search_queries": deduped_queries[: _FAST_MESH_REFERENCE_SEARCH_POLICY["max_queries_first_pass"]],
        "recommended_tools": ["search_kb", "data_web_search", "data_web_download"],
        "fallback_tools": list(_FAST_MESH_REFERENCE_SEARCH_POLICY["fallback_after_first_pass"]),
        "search_policy": dict(_FAST_MESH_REFERENCE_SEARCH_POLICY),
        "next_action": (
            "首轮按 search_queries 检索公开资料，只下载直接几何/坐标/CAD/mesh/archive 文件或高相关数据仓库页面。"
            "把候选域尺寸、边界条件、单位/尺度、网格目标和来源摘要写入 reference_notes 后再次调用 "
            "prepare_scientific_mesh(operation='resolve')。"
            "如果 reference_notes 表明没有可靠来源，再向用户说明缺少哪些字段以及为什么必须确认。"
        ),
        "source_trace": [
            {
                "source": "reference_search_required_for_incomplete_geometry_parameters",
                "case_type": case_label,
                "geometry_hint": str(geometry_name),
                "missing_fields": field_names,
            }
        ],
    }


def _required_user_fields(
    spec: str,
    params: dict[str, Any],
    case_profile: dict[str, Any] | None,
    public_profile: dict[str, Any] | None,
) -> list[dict[str, str]]:
    fields: list[dict[str, str]] = []
    if _looks_like_private_or_custom_geometry(spec, params) and not _has_any(params, _PRIVATE_GEOMETRY_FIELDS):
        fields.append({
            "field": "geometry_file 或几何坐标/尺寸参数",
            "why": "真实/复杂几何无法从公开资料可靠推断，猜测会改变计算对象。",
        })
    if case_profile and case_profile.get("geometry_mode") == "user_file" and not _has_any(params, _PRIVATE_GEOMETRY_FIELDS):
        fields.append({
            "field": "geometry_file",
            "why": "该模板声明几何必须由用户提供，支持 .step/.stp/.iges/.stl/.brep/.geo。",
        })
    require_boundary_confirmation = str(params.get("require_boundary_confirmation") or "").lower() in {"1", "true", "yes"}
    require_unit_confirmation = str(params.get("require_unit_confirmation") or "").lower() in {"1", "true", "yes"}
    if (
        require_boundary_confirmation
        and _has_any(params, _PRIVATE_GEOMETRY_FIELDS)
        and not _has_any(params, _BOUNDARY_FIELDS)
    ):
        fields.append({
            "field": "boundary_map 或 patch_map",
            "why": "边界语义决定 inlet/outlet/wall/farfield，节点不能仅凭 CAD patch 名称安全猜测。",
        })
    if (
        require_unit_confirmation
        and not _has_any(params, _UNIT_FIELDS)
        and _has_any(params, _PRIVATE_GEOMETRY_FIELDS)
    ):
        fields.append({
            "field": "unit_system 或 length_unit/scale",
            "why": "CAD/坐标文件常缺单位；单位错误会导致网格尺寸和流动无量纲数错误。",
        })
    require_flow_confirmation = str(params.get("require_flow_confirmation") or "").lower() in {
        "1", "true", "yes"
    }
    if require_flow_confirmation and not _has_any(params, _FLOW_FIELDS):
        fields.append({
            "field": "流动工况",
            "why": "用户明确要求确认流动工况；Re/Ma/入口速度/流体属性会影响网格和求解器输入。",
        })
    density_text = _text_blob(spec, params)
    if (
        mesh_density_requested(density_text)
        and mesh_density_requires_confirmation(params, spec)
        and not has_mesh_density_parameters(params)
    ):
        fields.append({
            "field": "网格加密目标",
            "why": "用户提出加密但未给数值，需要确认使用推荐参数还是指定 cell_budget/target_y_plus/first cell height。",
        })
    return fields


def _apply_defaultable_flow_conditions(
    resolved: dict[str, Any],
    spec: str,
    reference_notes: str,
) -> list[dict[str, Any]]:
    """Resolve missing operating conditions without unnecessary HITL.

    Flow conditions are preprocessing defaults, not geometry identity.  They
    should be recorded as assumptions unless the user explicitly requests
    confirmation via require_flow_confirmation=true.
    """
    text = "\n".join(part for part in (spec, reference_notes) if part)
    defaults: dict[str, Any] = {}

    reynolds_match = re.search(
        r"(?:reynolds(?:\s+number)?|\bRe)\s*(?:=|:|≈|~|is)?\s*([0-9]+(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?)",
        text,
        flags=re.IGNORECASE,
    )
    mach_match = re.search(
        r"(?:exit\s+|inlet\s+|design\s+)?(?:mach(?:\s+number)?|\bMa)\s*(?:=|:|≈|~|is)?\s*([0-9]+(?:\.[0-9]+)?)",
        text,
        flags=re.IGNORECASE,
    )
    if reynolds_match:
        defaults["reynolds_number"] = float(reynolds_match.group(1))
    elif re.search(r"(high[- ]?reynolds|高雷诺)", text, flags=re.IGNORECASE):
        defaults["reynolds_number"] = 500000.0

    if mach_match:
        defaults["mach_number"] = float(mach_match.group(1))
    elif re.search(r"(compressible|可压)", text, flags=re.IGNORECASE):
        defaults["mach_number"] = 0.2

    if not defaults and not _has_any(resolved, _FLOW_FIELDS) and re.search(
        r"(cfd|rans|openfoam|流动|绕流)", text, flags=re.IGNORECASE
    ):
        defaults["reynolds_number"] = 500000.0
        defaults["mach_number"] = 0.2

    if not defaults:
        return []

    assumptions = resolved.get("assumptions")
    if not isinstance(assumptions, list):
        assumptions = [] if assumptions in (None, "") else [str(assumptions)]
        resolved["assumptions"] = assumptions
    applied: list[dict[str, Any]] = []
    for key, value in defaults.items():
        if key in resolved:
            continue
        resolved[key] = value
        source = "reference_notes" if re.search(str(value), reference_notes or "") else "regime_default"
        applied.append({"parameter": key, "value": value, "source": source})
        assumptions.append(
            f"{key}={value:g} was selected by the preprocessing node because the user did not require explicit operating-condition confirmation."
        )
    resolved.setdefault("flow_conditions_confirmed", False)
    resolved.setdefault("flow_condition_policy", "automatic_default_with_manifest_assumption")
    return applied


async def _resolve_mesh_iteration_inputs(
    state: State,
    spec: str = "",
    case_type: str = "",
    parameters: str = "",
    review_issues: str = "",
    reference_notes: str = "",
    allow_public_defaults: bool = True,
    require_user_confirmation: bool = True,
    **extra: Any,
) -> dict[str, Any]:
    params, parse_error = _parse_json_object(parameters)
    if parse_error:
        return {"status": "error", "error": parse_error}
    params = flatten_parameter_groups(params, ("boundaries",))
    for key, value in extra.items():
        if key not in params and value is not None:
            params[key] = value
    apply_mesh_density_from_text(params, spec)

    public_case = None
    local_case = explicit_mesh_contract(spec, params, case_type=case_type)
    disallowed_airfoil = _disallowed_airfoil_resolution_request(
        state=state,
        spec=spec,
        case_type=case_type,
        params=params,
        local_case=local_case,
        public_case=public_case,
    )
    if disallowed_airfoil:
        return disallowed_airfoil
    disallowed_canonical = _disallowed_canonical_resolution_request(
        state=state,
        spec=spec,
        case_type=case_type,
        params=params,
        local_case=local_case,
        public_case=public_case,
    )
    if disallowed_canonical:
        return disallowed_canonical

    special_domain = detect_special_domain_requirements(spec, params, case_type=case_type)
    if special_domain:
        if not reference_notes:
            return special_domain_reference_request(spec, params, special_domain)
        if _reference_notes_indicate_search_failure(reference_notes) or _reference_notes_lack_usable_geometry(reference_notes):
            return _build_pause_payload(
                question="请补充该 CFD 算例的特殊计算域/边界拓扑参数。",
                context=(
                    f"该任务属于{special_domain.get('description')}。节点已尝试公开检索，但没有获得足够可靠的"
                    "可复现域参数。不能用孤立机翼 C-domain、圆柱或矩形标准算例替代。"
                ),
                required_fields=[
                    {"field": field, "why": "该字段决定计算域/边界拓扑，猜测会改变物理问题。"}
                    for field in (special_domain.get("missing_fields") or [])
                ],
                example_reply=(
                    "请提供同一物理拓扑的几何/网格文件路径，或补充上述边界/域参数；"
                    "如果只需要非物理可视化替代，请显式给出 surrogate_geometry_approved=true。"
                ),
                metadata={
                    "case_type": case_type or params.get("case_type") or "special_domain_cfd",
                    "topology": special_domain.get("topology"),
                    "parameters": params,
                    "reference_notes": reference_notes[:1000],
                },
            )

    missing_cascade_fields = _review_issues_missing_turbomachinery_domain(review_issues)
    if missing_cascade_fields:
        if not reference_notes:
            base = str(
                params.get("profile_name")
                or params.get("geometry")
                or params.get("case_type")
                or spec
                or "turbomachinery cascade"
            )
            return {
                "status": "needs_reference_search",
                "message": (
                    "网格语义审核表明该任务是叶栅/周期通道流动，但缺少外围通道域或周期边界参数。"
                    "请先检索公开 benchmark/论文/算例说明补齐这些参数。"
                ),
                "case_type": "turbomachinery_cascade",
                "missing_fields": missing_cascade_fields,
                "resolved_parameters": params,
                "search_queries": [
                    f"{base} cascade pitch inlet outlet periodic boundary geometry",
                    f"{base} computational domain periodic boundary mesh parameters",
                    f"{base} benchmark cascade geometry coordinates pitch",
                ][: _FAST_MESH_REFERENCE_SEARCH_POLICY["max_queries_first_pass"]],
                "recommended_tools": ["data_web_search", "data_web_download", "prepare_scientific_mesh"],
                "fallback_tools": list(_FAST_MESH_REFERENCE_SEARCH_POLICY["fallback_after_first_pass"]),
                "search_policy": dict(_FAST_MESH_REFERENCE_SEARCH_POLICY),
                "next_action": (
                    "首轮检索缺失的 pitch、进出口范围、周期边界；只下载几何/坐标/CAD/mesh/archive "
                    "或高相关数据仓库页面。把来源和是否找到可复现参数写入 "
                    "reference_notes 后再次调用 prepare_scientific_mesh(operation='resolve')。"
                ),
            }
        if _reference_notes_indicate_search_failure(reference_notes) or _reference_notes_lack_usable_geometry(reference_notes):
            return _build_pause_payload(
                question="请确认叶栅外围计算域和周期边界参数。",
                context=(
                    "网格审核发现当前叶片 profile 网格缺少叶栅/周期通道所需的外围区域参数。"
                    "节点已尝试公开检索，但没有找到足够可靠的可复现参数。"
                    "这些参数决定计算域拓扑，不能用孤立机翼 C-domain 替代。"
                ),
                required_fields=[
                    {"field": field, "why": "叶栅通道网格必须具备该参数，否则外围区域不是涡轮叶片计算域。"}
                    for field in missing_cascade_fields
                ],
                example_reply=(
                    "pitch_chord_ratio=0.85\n"
                    "upstream_length=1.0, downstream_length=2.0\n"
                    "cascade_periodic=true, periodic_boundary_pairing=upper:lower"
                ),
                metadata={
                    "case_type": "turbomachinery_cascade",
                    "review_issues": review_issues,
                    "parameters": params,
                    "reference_notes": reference_notes[:1000],
                },
            )

    if (
        _geometry_policy_requires_user_file(params, reference_notes)
        and not _has_any(params, _PRIVATE_GEOMETRY_FIELDS)
        and not surrogate_geometry_explicitly_approved(params)
    ):
        case_label = str((public_case or local_case or {}).get("case_type") or case_type or params.get("case_type") or "public_geometry")
        return _build_pause_payload(
            question="请提供可复现的几何文件，或确认同类公开几何下载来源。",
            context=(
                f"当前识别为 {case_label}。参数或检索摘要已经说明该任务必须有真实几何文件，"
                "但当前没有 geometry_file/cad_file/stl_file/step_file/geo_file/msh_file。"
                "流动参数、网格密度和边界层参数可以由节点默认，但几何形状不能凭空替换。\n"
                + _surrogate_geometry_warning()
            ),
            required_fields=[
                {
                    "field": "geometry_file 或可下载的同类公开几何 URL",
                    "why": "真实几何决定物理问题和网格区域；缺少它时无法生成合格的 OpenFOAM/Tecplot 网格。",
                }
            ],
            example_reply=(
                "geometry_file=/absolute/path/to/geometry.step\n"
                "或：candidate_geometry_source=https://example.org/case/geometry.dat\n"
                "或：surrogate_geometry_approved=true, surrogate_geometry=<指定替代几何>, "
                "surrogate_reason=<原因>, nonphysical_approximation=true"
            ),
            metadata={
                "case_type": case_label,
                "review_issues": review_issues,
                "parameters": params,
                "reference_notes": reference_notes[:1000],
                "do_not_suggest": [
                    "不要建议用不同几何角色/不同流动拓扑的标准几何替代用户指定几何",
                    "不要把替代几何作为验证算例，除非用户明确 surrogate_geometry_approved=true",
                ],
            },
        )

    user_fields = _required_user_fields(spec, params, local_case, public_case)

    if user_fields and require_user_confirmation:
        if _has_any(params, _PRIVATE_GEOMETRY_FIELDS):
            if not reference_notes:
                return _build_missing_parameter_reference_request(
                    spec=spec,
                    params=params,
                    missing_fields=user_fields,
                    case_profile=local_case,
                    public_profile=public_case,
                    case_type=case_type,
                )
            if _reference_notes_indicate_search_failure(reference_notes):
                case_label = (
                    str((local_case or public_case or {}).get("case_type") or case_type or params.get("case_type") or "未确定算例")
                )
                return _build_pause_payload(
                    question="公开检索没有补齐几何相关网格参数，请确认缺失信息。",
                    context=(
                        f"当前识别为 {case_label}。节点已优先尝试从公开资料补齐参数，"
                        "但检索结果不足以可靠确定这些会影响几何、边界语义、单位或网格目标的信息。\n"
                        + _surrogate_geometry_warning()
                    ),
                    required_fields=user_fields,
                    example_reply="geometry_file=/absolute/path/to/model.step\nboundary_map={\"inlet\":\"inlet\",\"outlet\":\"outlet\",\"walls\":[\"wall\"]}\nlength_unit=m",
                    metadata={
                        "case_type": case_label,
                        "review_issues": review_issues,
                        "parameters": params,
                        "reference_notes": reference_notes[:1000],
                        "do_not_suggest": [
                            "不要建议用不同几何角色/不同流动拓扑的标准几何替代用户指定几何",
                            "不要建议用翼型/NACA 替代非翼型、内部通道、周期通道、级联或真实 CAD 几何",
                            "不要把替代几何作为验证算例，除非用户明确 surrogate_geometry_approved=true",
                        ],
                    },
                )
            params["reference_notes"] = reference_notes[:1000]
            user_fields = []
        if not user_fields:
            pass
        else:
            case_label = (
                str((local_case or public_case or {}).get("case_type") or case_type or params.get("case_type") or "未确定算例")
            )
            example_parts = []
            if any("geometry" in item["field"] or "几何" in item["field"] for item in user_fields):
                example_parts.append("geometry_file=/absolute/path/to/model.step")
            if any("boundary" in item["field"] or "patch" in item["field"] for item in user_fields):
                example_parts.append('boundary_map={"inlet":"inlet","outlet":"outlet","walls":["wall"]}')
            if any("unit" in item["field"] or "单位" in item["field"] for item in user_fields):
                example_parts.append("length_unit=m")
            if any("流动" in item["field"] for item in user_fields):
                example_parts.append("reynolds_number=500000")
            if any("网格" in item["field"] for item in user_fields):
                example_parts.append("使用推荐加密参数")
            return _build_pause_payload(
                question="请补充网格自迭代必须由您确认的信息。",
                context=(
                    f"当前识别为 {case_label}。这些信息会改变几何、边界语义、单位或网格目标，"
                    "不能通过公开资料安全替代。"
                ),
                required_fields=user_fields,
                example_reply="\n".join(example_parts) or "请给出 geometry_file、boundary_map、单位和流动工况。",
                metadata={
                    "case_type": case_label,
                    "review_issues": review_issues,
                    "parameters": params,
                },
            )

    source_trace: list[dict[str, Any]] = []
    resolved = dict(params)
    if local_case:
        defaults = local_case.get("defaults") or {}
        resolved = {**defaults, **resolved}
        resolved.setdefault("case_type", local_case.get("case_type"))
        resolved.setdefault("mesh_type", local_case.get("mesh_type"))
        source_trace.append({
            "source": "local_case_profile",
            "path": local_case.get("_source"),
            "case_type": local_case.get("case_type"),
        })

    if public_case and allow_public_defaults:
        for key, value in (public_case.get("suggested_parameters") or {}).items():
            if key == "mesh_type" and resolved.get("mesh_type") in {"public_geometry", "generic_gmsh", "", None}:
                resolved["mesh_type"] = value
            else:
                resolved.setdefault(key, value)
        resolved.setdefault("case_type", public_case.get("case_type"))
        if resolved.get("mesh_type") in {"public_geometry", "", None}:
            resolved["mesh_type"] = public_case.get("mesh_type")
        source_trace.append({
            "source": "public_benchmark_profile_hint",
            "case_type": public_case.get("case_type"),
            "case_family": public_case.get("case_family"),
            "hint": public_case.get("public_reference_hint"),
        })

    flow_defaults = _apply_defaultable_flow_conditions(resolved, spec, reference_notes)
    if flow_defaults:
        source_trace.append({
            "source": "automatic_flow_condition_resolution",
            "parameters": flow_defaults,
            "requires_human_input": False,
        })

    disallowed_resolved_airfoil = _disallowed_airfoil_resolution_request(
        state=state,
        spec=spec,
        case_type=case_type,
        params=resolved,
        local_case=local_case,
        public_case=public_case,
    )
    if disallowed_resolved_airfoil:
        return disallowed_resolved_airfoil
    disallowed_resolved_canonical = _disallowed_canonical_resolution_request(
        state=state,
        spec=spec,
        case_type=case_type,
        params=resolved,
        local_case=local_case,
        public_case=public_case,
    )
    if disallowed_resolved_canonical:
        return disallowed_resolved_canonical

    # 判决拆除三波（mia:1514 降格，2026-09-02）：「像公开 benchmark 且没写
    # reference_notes 就必须先检索」是顺序仪式（参数已 resolved 也拦）。改为用
    # resolved/默认参数继续，如实挂 public_benchmark_params_unverified 义务
    # （resolved_parameters + source_trace + 顶层 obligations + transcript）；
    # 检索线索照给，模型想核实随时可走。
    public_benchmark_unverified: dict[str, Any] | None = None
    if public_case and not reference_notes:
        public_benchmark_unverified = {
            "kind": "public_benchmark_params_unverified",
            "case_type": public_case.get("case_type"),
            "case_family": public_case.get("case_family"),
            "message": (
                "该算例像公开 CFD benchmark，但没有 reference_notes：标准域尺寸/边界条件"
                "取自本地 profile 与公开参考默认值，未经检索核实。"
            ),
            "clear_by": (
                "用 search_kb/data_web_search 检索下列 query，把摘要、来源、"
                "download.saved_path/sha256 写入 reference_notes 后再次调用 "
                "prepare_scientific_mesh(operation='resolve')。"
            ),
            "search_queries": _public_search_queries(spec, params, public_case),
            "recommended_tools": ["search_kb", "data_web_search", "data_web_download"],
            "search_policy": dict(_FAST_MESH_REFERENCE_SEARCH_POLICY),
        }
        resolved["public_benchmark_params_unverified"] = True
        source_trace.append({
            "source": "public_benchmark_params_unverified",
            "case_type": public_case.get("case_type"),
            "case_family": public_case.get("case_family"),
            "requires_human_input": False,
        })
        try:
            state.append_transcript(
                "mesh_public_benchmark_params_unverified",
                case_type=public_case.get("case_type"),
                case_family=public_case.get("case_family"),
            )
        except Exception:
            pass

    if public_case and reference_notes:
        source_trace.append({
            "source": "reference_notes",
            "summary": reference_notes[:1000],
        })
        if _reference_notes_need_coordinate_profile_build(reference_notes):
            return {
                "status": "needs_geometry_processing",
                "message": (
                    "公开几何坐标已找到，但当前是多个二维坐标段/上下表面/压力面和吸力面，"
                    "必须先合成为闭合 profile，不能退回 NACA 或占位几何。"
                ),
                "recommended_tools": ["prepare_scientific_mesh"],
                "next_action": (
                    "调用 prepare_scientific_mesh(operation='build_profile', coordinate_files=[...]) 合成闭合 profile；"
                    "然后调用 prepare_scientific_mesh(operation='generate', mesh_type='coordinate_profile_gmsh', "
                    "parameters 中传 coordinate_profile_path, convert_to_openfoam=true, write_tecplot=true)。"
                ),
                "resolved_parameters": resolved,
                "source_trace": source_trace,
            }
        if (
            _reference_notes_indicate_search_failure(reference_notes)
            or _reference_notes_lack_usable_geometry(reference_notes)
        ):
            return _build_pause_payload(
                question="公开检索没有找到可复现的几何参数，请确认是否能提供几何文件。",
                context=(
                    "节点已优先尝试使用公开资料检索几何参数，但当前检索结果不足以可靠生成网格。"
                    "如果只找到了 benchmark 页面、论文或数据库入口，但没有可直接使用的坐标/CAD/网格文件，"
                    "仍不能生成合格网格。继续自迭代需要用户提供本地几何文件，"
                    "或明确确认某个同类公开来源中可下载的具体几何文件。\n"
                    + _surrogate_geometry_warning()
                ),
                required_fields=[
                    {
                        "field": "geometry_file 或同类 candidate_geometry_source",
                        "why": "没有可复现几何坐标/参数时，节点无法保证生成的是用户指定几何；跨类别替代会改变物理问题。",
                    }
                ],
                example_reply=(
                    "geometry_file=/absolute/path/to/geometry.step\n"
                    "或：确认采用同类候选来源 <source-name/url> 的几何参数\n"
                    "或：surrogate_geometry_approved=true, surrogate_geometry=<同类/指定替代几何>, "
                    "surrogate_reason=<原因>, nonphysical_approximation=true"
                ),
                metadata={
                    "case_type": public_case.get("case_type"),
                    "case_family": public_case.get("case_family"),
                    "review_issues": review_issues,
                    "parameters": resolved,
                    "reference_notes": reference_notes[:1000],
                    "do_not_suggest": [
                        "不要建议用不同几何角色/不同流动拓扑的标准几何替代用户指定几何",
                        "不要建议用翼型/NACA 替代非翼型、内部通道、周期通道、级联或真实 CAD 几何",
                        "不要把替代几何作为验证算例，除非用户明确 surrogate_geometry_approved=true",
                    ],
                },
            )

    unresolved_public_fields = []
    if public_case:
        for field in public_case.get("needs_user_if_missing", []):
            normalized = str(field)
            if "reynolds" in normalized and _has_any(resolved, ("reynolds_number",)):
                continue
            if "y_plus" in normalized and _has_any(resolved, ("target_y_plus", "boundary_layer_first")):
                continue
            if "scale" in normalized and _has_any(resolved, ("scale", "length_unit", "step_height", "plate_length")):
                continue
            unresolved_public_fields.append(normalized)
    if unresolved_public_fields and require_user_confirmation:
        required = [
            {
                "field": field,
                "why": "公开 benchmark 可能有多种变体；该字段需要用户确认采用哪个物理工况或尺度。",
            }
            for field in unresolved_public_fields
        ]
        return _build_pause_payload(
            question="请确认公开 benchmark 的具体变体参数。",
            context="已找到可公开参考的算例类型，但仍有会影响网格和物理问题的关键变体需要确认。",
            required_fields=required,
            example_reply="reynolds_number=1000\nstep_height=0.01 m\n使用推荐网格质量门",
            metadata={
                "case_type": public_case.get("case_type"),
                "case_family": public_case.get("case_family"),
                "review_issues": review_issues,
                "parameters": resolved,
            },
        )

    suggestions = []
    issues_text = review_issues.lower()
    if "aspect" in issues_text or "长宽比" in issues_text:
        suggestions.append("降低边界层增长率或适度增大第一层网格距离，再局部加密核心区域。")
    if "farfield" in issues_text or "远场" in issues_text:
        suggestions.append("增大远场边界或下游长度，并保持出口到尾流区的缓冲距离。")
    if "tecplot" in issues_text or ".dat" in issues_text:
        suggestions.append("重新执行 Tecplot 导出，并在复核中检查 surface/volume dat 非空。")
    if "openfoam" in issues_text or "polymesh" in issues_text:
        suggestions.append("重新执行 OpenFOAM 转换，并检查 constant/polyMesh/boundary 与 system/controlDict。")

    result = {
        "status": "success",
        "resolved_parameters": resolved,
        "source_trace": source_trace,
        "mesh_iteration_guidance": suggestions,
        "requires_human_input": False,
        "confidence": 0.78 if source_trace else 0.45,
        "notes": (
            "已尽量使用本地 case profile/公开参考线索补齐可自动处理的信息。"
            "几何、边界语义、单位和用户目标仍不得擅自猜测。"
        ),
    }
    if public_benchmark_unverified:
        result["obligations"] = [public_benchmark_unverified]
        result["public_benchmark_params_unverified"] = True
        result["confidence"] = min(float(result["confidence"]), 0.45)
        result["notes"] += " 公开 benchmark 参数未经检索核实（见 obligations）。"
    return result

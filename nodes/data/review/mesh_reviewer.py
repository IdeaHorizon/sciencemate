"""Compact semantic review for generated meshes."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


_ROLE_RULES: dict[str, dict[str, Any]] = {
    "generic_geometry": {"quality": {"min_2d_elements": 1}},
    "airfoil_external": {
        "quality": {
            "min_2d_elements": 1000, "min_far_field": 10, "min_wake_length": 15,
            "min_surface_points": 121, "max_boundary_layer_ratio": 1.25,
        },
        "required_features": [
            "farfield_extent_ok", "wake_extent_ok", "surface_resolution_ok",
            "boundary_layer_growth_ok",
        ],
    },
    "cylinder_external": {
        "quality": {"min_2d_elements": 1000},
        "recommended_features": ["has_cylinder_wall", "has_wake_extent"],
    },
    "internal_rectangular_flow": {
        "quality": {"min_2d_elements": 100},
        "recommended_features": ["has_wall_boundary"],
    },
    "turbomachinery_cascade": {
        "quality": {"min_2d_elements": 1000},
        "reject_if": ["isolated_airfoil_domain"],
        "required_features": [
            "has_pitch", "has_inlet_outlet_extent", "has_periodic_pairing",
            "domain_shape_verified", "domain_extent_reasonable", "periodic_pitch_consistent",
        ],
        "recommended_features": ["has_blade_wall", "has_inlet", "has_outlet"],
    },
    "periodic_or_repeating_passage": {
        "quality": {"min_2d_elements": 1000},
        "reject_if": ["isolated_airfoil_domain"],
        "required_features": [
            "has_pitch", "has_inlet_outlet_extent", "has_periodic_pairing",
            "domain_shape_verified", "domain_extent_reasonable", "periodic_pitch_consistent",
        ],
        "recommended_features": ["has_inlet", "has_outlet"],
    },
    "rotating_or_sliding_region": {
        "quality": {"min_2d_elements": 1000},
        "reject_if": ["isolated_airfoil_domain"],
        "required_features": ["has_rotating_region", "has_rotation_definition", "has_interface_map"],
    },
    "internal_passage_or_device": {
        "quality": {"min_2d_elements": 500},
        "reject_if": ["isolated_airfoil_domain"],
        "required_features": ["has_inlet", "has_outlet", "has_wall_boundary"],
    },
    "multi_region_or_conjugate": {
        "quality": {"min_2d_elements": 500},
        "reject_if": ["isolated_airfoil_domain"],
        "required_features": ["has_region_map", "has_interface_map"],
    },
    "free_surface_or_multiphase": {
        "quality": {"min_2d_elements": 500},
        "reject_if": ["isolated_airfoil_domain"],
        "required_features": ["has_phase_domain", "has_open_boundary"],
    },
}


def _text_blob(*values: Any) -> str:
    return "\n".join(
        json.dumps(value, ensure_ascii=False, sort_keys=True)
        if isinstance(value, (dict, list, tuple)) else str(value)
        for value in values if value is not None
    ).lower()


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return default if value in (None, "", [], {}) else float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return default if value in (None, "", [], {}) else int(float(value))
    except (TypeError, ValueError):
        return default


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value or "").strip().lower()
    if text in {"true", "1", "yes", "on"}:
        return True
    if text in {"false", "0", "no", "off", "none", "null"}:
        return False
    return default


def infer_mesh_intent(result: dict[str, Any], requested: dict[str, Any]) -> dict[str, Any]:
    mesh_type = str(result.get("mesh_type") or requested.get("mesh_type") or "").strip()
    text = _text_blob(result, requested)
    case_family = str(requested.get("case_family") or result.get("case_family") or "").strip().lower()
    profile_kind = str(requested.get("profile_kind") or result.get("profile_kind") or "").strip().lower()
    case_type = str(requested.get("case_type") or result.get("case_type") or "").strip().lower()
    role, topology = "generic_geometry", "unknown"
    if (
        mesh_type == "geometry_file_gmsh"
        and str(requested.get("discipline") or result.get("discipline") or "").lower() != "cfd"
        and not _as_bool(requested.get("convert_to_openfoam"))
        and not (result.get("openfoam") or {}).get("requested")
    ):
        # Object names and requirements such as "no duplicate nodes" do not
        # turn a solid mesh into a periodic fluid passage. CFD role rules only
        # apply when that physical adapter or its solver output was requested.
        text = ""
    if (
        "turbomachinery_blade_section" in {case_family, profile_kind}
        or "turbomachinery_cascade" in {case_family, profile_kind, case_type}
        or re.search(r"\b(?:turbine|compressor|cascade|turbomachinery|blade|vane)\b|涡轮|透平|压气机|叶片|叶栅", text)
    ):
        role, topology = "turbomachinery_cascade", "internal_periodic_passage"
    elif re.search(r"\b(?:periodic|cyclic|pitchwise|blade row|annular passage)\b|重复|周期|叶排|环形通道", text):
        role, topology = "periodic_or_repeating_passage", "internal_periodic_passage"
    elif mesh_type in {"airfoil_gmsh", "airfoil_ogrid"} or re.search(r"\b(?:airfoil|wing|naca)\b|翼型|机翼", text):
        role, topology = "airfoil_external", "external_farfield"
    elif mesh_type in {"cylinder_flow", "cylinder_gmsh"} or re.search(r"\bcylinder\b|圆柱", text):
        role, topology = "cylinder_external", "external_wake"
    elif re.search(r"\b(?:rotating|sliding mesh|mrf|ami|impeller|fan|propeller|rotor)\b|旋转|滑移网格|叶轮|风扇|螺旋桨", text):
        role, topology = "rotating_or_sliding_region", "rotating_interface"
    elif re.search(r"\b(?:multi[- ]?region|cht|conjugate heat transfer|fluid[- ]?solid|fsi)\b|多区域|共轭传热|流固|多物理", text):
        role, topology = "multi_region_or_conjugate", "multi_region_coupled"
    elif re.search(r"\b(?:free surface|vof|multiphase|two[- ]?phase|wave tank)\b|自由液面|多相|两相|波浪|水面", text):
        role, topology = "free_surface_or_multiphase", "free_surface_multiphase"
    elif re.search(r"\b(?:internal flow|passage|duct|diffuser|combustor|manifold|heat exchanger|valve)\b|内流|流道|通道|扩压器|燃烧室|歧管|换热器|阀", text):
        role, topology = "internal_passage_or_device", "internal_inlet_outlet"
    elif mesh_type == "structured_rect":
        role, topology = "internal_rectangular_flow", "internal_rectangular"
    return {
        "geometry_role": role, "flow_topology": topology, "mesh_type": mesh_type,
        "case_type": case_type, "case_family": case_family, "profile_kind": profile_kind,
    }


def _boundary_names(poly_dir: Any) -> list[str]:
    path = Path(str(poly_dir or "")) / "boundary"
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    return [
        line.strip() for index, line in enumerate(lines[:-1])
        if re.match(r"^[A-Za-z_][A-Za-z0-9_./-]*$", line.strip())
        and lines[index + 1].strip() == "{"
        and line.strip() != "FoamFile"
    ]


def extract_mesh_features(
    result: dict[str, Any],
    requested: dict[str, Any],
    intent: dict[str, Any],
) -> dict[str, Any]:
    grid = result.get("grid_params") or {}
    patches = _boundary_names(result.get("polyMesh_dir"))
    patch_text = " ".join(patches).lower()

    def has(*names: str) -> bool:
        return any(
            requested.get(name) not in (None, "", [], {})
            or grid.get(name) not in (None, "", [], {})
            for name in names
        )

    role = intent.get("geometry_role")
    mesh_type = str(result.get("mesh_type") or requested.get("mesh_type") or "")
    requested_pitch = _as_float(grid.get("pitch", requested.get("pitch")))
    actual_pitch = _as_float(grid.get("actual_pitch", requested.get("actual_pitch")))
    pitch_error = (
        abs(actual_pitch - requested_pitch) / max(abs(requested_pitch), 1e-12)
        if requested_pitch > 0 else 0.0
    )
    domain_shape = str(grid.get("domain_shape") or requested.get("domain_shape") or "").lower()
    reference_domain = domain_shape in {"reference_mesh", "reference_polygon", "segmented_periodic_passage"}
    far_field = _as_float(grid.get("far_field", requested.get("far_field")))
    wake_length = _as_float(grid.get("wake_length", requested.get("wake_length")))
    domain_type = str(grid.get("domain_type") or requested.get("domain_type") or "").lower()
    return {
        "patches": patches,
        "has_inlet": "inlet" in patch_text,
        "has_outlet": "outlet" in patch_text,
        "has_farfield": "farfield" in patch_text,
        "has_wall_boundary": any(token in patch_text for token in ("wall", "airfoil", "blade", "cylinder")),
        "has_blade_wall": "blade" in patch_text or "airfoil" in patch_text,
        "has_cylinder_wall": "cylinder" in patch_text,
        "has_periodic_pairing": has("periodic_boundary_pairing", "periodic_patches")
        or _as_bool(requested.get("cascade_periodic"))
        or _as_bool(requested.get("pitchwise_periodic"))
        or "periodic" in patch_text or "cyclic" in patch_text,
        "has_pitch": has("pitch", "pitch_chord_ratio", "blade_pitch"),
        "has_inlet_outlet_extent": has(
            "inlet_length", "outlet_length", "axial_chord", "upstream_length",
            "downstream_length", "fore_domain_length", "aft_domain_length",
            "inlet_extent", "outlet_extent",
        ) or (reference_domain and "inlet" in patch_text and "outlet" in patch_text),
        "has_wake_extent": has("wake_length", "downstream_length"),
        "has_rotating_region": has("rotating_region", "mrf_zone", "ami_interface", "sliding_interface", "rotor_region"),
        "has_rotation_definition": has("rotation_axis", "angular_velocity", "rpm"),
        "has_region_map": has("regions", "region_map", "fluid_region", "solid_region"),
        "has_interface_map": has("interfaces", "interface_map", "coupled_interfaces", "interface_patches"),
        "has_phase_domain": has("phase_boundary", "free_surface_region", "initial_water_level", "interface_location"),
        "has_open_boundary": has("atmosphere_patch", "pressure_outlet_patch", "open_boundary_patch")
        or any(token in patch_text for token in ("atmosphere", "pressureoutlet", "open")),
        "farfield_extent_ok": far_field >= 10.0,
        "wake_extent_ok": domain_type != "c" or wake_length >= max(15.0, far_field),
        "surface_resolution_ok": _as_int(grid.get("n_surface", requested.get("n_surface"))) >= 121,
        "boundary_layer_growth_ok": _as_float(
            grid.get("boundary_layer_ratio", requested.get("boundary_layer_ratio")), 99.0
        ) <= 1.25,
        "isolated_airfoil_domain": role == "turbomachinery_cascade"
        and mesh_type == "airfoil_gmsh"
        and not _as_bool(requested.get("isolated_blade_approximation_confirmed")),
        "domain_shape_verified": _as_bool(grid.get("domain_shape_verified", requested.get("domain_shape_verified"))),
        "domain_extent_reasonable": (
            0 < _as_float(grid.get("domain_x_extent_chords", requested.get("domain_x_extent_chords")))
            <= _as_float(requested.get("max_domain_x_extent_chords"), 16.0)
        ),
        "periodic_pitch_consistent": requested_pitch <= 0
        or pitch_error <= _as_float(requested.get("max_pitch_relative_error"), 0.05),
        "domain_shape": domain_shape,
        "pitch_relative_error": pitch_error,
    }


def semantic_mesh_review(result: dict[str, Any], requested: dict[str, Any]) -> dict[str, Any]:
    intent = infer_mesh_intent(result, requested)
    features = extract_mesh_features(result, requested, intent)
    role = str(intent["geometry_role"])
    rule = _ROLE_RULES.get(role, _ROLE_RULES["generic_geometry"])
    checks: dict[str, bool] = {}
    issues: list[dict[str, Any]] = []

    def check(feature: str, expected: bool, severity: str, prefix: str) -> None:
        value = bool(features.get(feature))
        ok = value is expected
        checks[f"{prefix}_{feature}"] = ok
        if not ok:
            issues.append({
                "code": f"{prefix}_{feature}",
                "severity": severity,
                "message": f"Mesh role={role} failed semantic feature {feature}.",
                "recommendation": "Use geometry, domain boundaries, and metadata appropriate to the inferred physical role.",
            })

    for feature in rule.get("reject_if") or []:
        check(feature, False, "critical", "semantic_reject")
    for feature in rule.get("required_features") or []:
        check(feature, True, "critical", "missing_required")
    for feature in rule.get("recommended_features") or []:
        check(feature, True, "minor", "missing_recommended")
    return {
        "intent": intent,
        "features": features,
        "checks": checks,
        "issues": issues,
        "quality_overrides": dict(rule.get("quality") or {}),
        "rule_source": "nodes/data/review/mesh_reviewer.py",
        "rule_role": role,
    }

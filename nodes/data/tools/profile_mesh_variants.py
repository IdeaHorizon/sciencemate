"""Generate only the geometry variants explicitly declared by a CFD plan."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from core.state import State

from .geometry_assets import (
    COORDINATE_PROFILE_KEYS,
    COORDINATE_PROFILE_SUFFIXES,
    existing_geometry_like_path,
)


def _slug(value: str, fallback: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("_") or fallback


def _naca_code(value: Any, *, allow_bare: bool = False) -> str:
    pattern = r"\s*(?:NACA\s*[-_ ]?)?(\d{4})\s*" if allow_bare else r"\s*NACA\s*[-_ ]?(\d{4})\s*"
    match = re.fullmatch(pattern, str(value or ""), flags=re.I)
    return match.group(1) if match else ""


def _declared_stage_values(
    calculation_stages: list[dict[str, Any]],
) -> list[Any]:
    """Read profile variants from declared stage parameters and sweeps only."""
    values: list[Any] = []

    def add(value: Any) -> None:
        if isinstance(value, list):
            values.extend(value)
        elif value not in (None, ""):
            values.append(value)

    for stage in calculation_stages:
        parameters = dict(stage.get("parameters") or {})
        for key in ("geometry_variant", "naca_code", *COORDINATE_PROFILE_KEYS):
            value = parameters.get(key)
            add(f"NACA {value}" if key == "naca_code" and re.fullmatch(r"\d{4}", str(value or "")) else value)

        sweep = stage.get("sweep") or parameters.get("sweep") or {}
        dimensions = sweep.get("dimensions") if isinstance(sweep, dict) else []
        if not dimensions and isinstance(sweep, dict) and sweep.get("parameter"):
            dimensions = [sweep]
        for dimension in dimensions or []:
            if not isinstance(dimension, dict):
                continue
            parameter = str(dimension.get("parameter") or "").strip().casefold()
            if parameter in {"geometry_variant", "naca_code", *COORDINATE_PROFILE_KEYS}:
                values = dimension.get("values")
                if parameter == "naca_code" and isinstance(values, list):
                    values = [f"NACA {value}" if re.fullmatch(r"\d{4}", str(value or "")) else value for value in values]
                add(values)
    return values


def cfd_profile_mesh_variants(
    *,
    calculation_stages: list[dict[str, Any]],
    params: dict[str, Any],
    mesh_generation_result: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Return plan-declared NACA or coordinate-profile variants, excluding the main mesh."""
    main_naca = _naca_code(
        (mesh_generation_result or {}).get("naca_code") or params.get("naca_code"),
        allow_bare=True,
    )
    main_paths = {
        str(value)
        for source in (params, mesh_generation_result or {})
        for key in COORDINATE_PROFILE_KEYS
        if (value := source.get(key))
    }
    variants: list[dict[str, Any]] = []
    seen: set[str] = set()
    for value in _declared_stage_values(calculation_stages):
        code = _naca_code(value)
        if code and code != main_naca:
            variant = {
                "variant_id": f"naca{code}",
                "role": "declared_profile_variant",
                "profile_name": f"NACA {code}",
                "mesh_type": "airfoil_gmsh",
                "naca_code": code,
                "source": "declared_stage_geometry_variant",
            }
        else:
            path = existing_geometry_like_path(value)
            if not path or Path(path).suffix.lower() not in COORDINATE_PROFILE_SUFFIXES or path in main_paths:
                continue
            name = re.sub(r"[^A-Za-z0-9_.-]+", "", Path(path).stem).upper() or "coordinate_profile"
            variant = {
                "variant_id": _slug(name.lower(), "coordinate_profile"),
                "role": "declared_profile_variant",
                "profile_name": name,
                "mesh_type": "coordinate_profile_gmsh",
                "coordinate_profile_path": path,
                "source": "declared_stage_geometry_variant",
            }
        key = str(variant["variant_id"])
        if key not in seen:
            seen.add(key)
            variants.append(variant)
    return variants


async def generate_cfd_profile_mesh_variants(
    *,
    state: State,
    selected_discipline: str,
    params: dict[str, Any],
    package_dir: Path,
    mesh_generation_result: dict[str, Any] | None,
    calculation_stages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if selected_discipline != "cfd":
        return []
    variants = cfd_profile_mesh_variants(
        calculation_stages=calculation_stages,
        params=params,
        mesh_generation_result=mesh_generation_result,
    )
    if not variants:
        return []

    from .scientific_mesh import prepare_scientific_mesh

    base_keys = {
        "angle_of_attack", "aoa", "aoa_deg", "alpha_deg", "reynolds_number", "mach_number",
        "domain_type", "far_field", "wake_length", "span", "n_surface", "h_airfoil",
        "h_farfield", "boundary_layer_first", "boundary_layer_thickness",
        "boundary_layer_ratio", "quality_preset", "gmsh_binary", "gmsh_to_foam_cmd",
        "timeout", "convert_to_openfoam", "write_tecplot",
    }
    results: list[dict[str, Any]] = []
    for variant in variants:
        variant_id = _slug(str(variant.get("variant_id") or "profile_variant"), "profile_variant")
        variant_params = {key: params[key] for key in base_keys if key in params}
        variant_params.update({
            "mesh_type": variant["mesh_type"],
            "quality_preset": variant_params.get("quality_preset", "robust"),
            "convert_to_openfoam": variant_params.get("convert_to_openfoam", True),
            "write_tecplot": variant_params.get("write_tecplot", True),
            "case_type": "airfoil",
            "variant_role": variant.get("role"),
            "variant_source": variant.get("source"),
        })
        if variant["mesh_type"] == "airfoil_gmsh":
            variant_params["naca_code"] = variant["naca_code"]
        else:
            variant_params.update({
                "coordinate_profile_path": variant["coordinate_profile_path"],
                "profile_dat_path": variant["coordinate_profile_path"],
                "profile_name": variant.get("profile_name"),
            })
        case_dir = package_dir / "mesh_variants" / variant_id
        try:
            result = await prepare_scientific_mesh(
                state=state,
                spec=(
                    f"Generate mesh variant {variant.get('profile_name') or variant_id}. "
                    "Use only the explicit geometry parameters in this variant call."
                ),
                discipline="cfd",
                mesh_type=str(variant["mesh_type"]),
                case_dir=str(case_dir),
                parameters=json.dumps(variant_params, ensure_ascii=False),
                operation="prepare",
                generate_mesh=True,
            )
        except Exception as exc:
            result = {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
        results.append({
            **variant,
            "case_dir": str(case_dir.resolve()),
            "status": result.get("status"),
            "mesh_review_status": ((result.get("mesh_review") or {}).get("status")),
            "deliverable_valid": result.get("deliverable_valid", result.get("status") == "success"),
            "result": result,
        })
    return results

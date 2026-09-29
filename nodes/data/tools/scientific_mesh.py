"""Unified, cross-discipline computational mesh workflow.

This is the only mesh tool that should be exposed by the data harness.  It
keeps the established CFD router, geometry resolver, profile builder, and mesh
generators as internal implementation layers while presenting one stable API
to the model.
"""
from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any

from core.state import State
from core.tool_registry import ToolDefinition, register_tool
from nodes.data.pipeline_contract import revision_contract
from nodes.data.planning.store import witness_plan_approval

from .discipline_identifier import identify_discipline
from .geometry_assets import (
    COORDINATE_PROFILE_SUFFIXES as _COORDINATE_PROFILE_SUFFIXES,
    apply_downloaded_geometry_assets_to_params as _apply_downloaded_geometry_assets_to_params,
    canonical_parameter_bindings,
    flatten_parameter_groups,
    geometry_file_from_params as _geometry_file,
)
from .human_input_utils import caller_request_text


_CONTINUUM_DISCIPLINES = {"cfd", "csm", "cem", "heat_transfer", "multiphysics"}
_REFERENCE_COMPUTATIONAL_MESH_SUFFIXES = {
    ".cas", ".cgns", ".foam", ".msh", ".vtk", ".vtu",
    ".med", ".unv", ".exo", ".ex2", ".e",
}
MESH_OPERATIONS = frozenset({"prepare", "resolve", "build_profile", "generate", "convert"})


def normalize_mesh_operation(value: Any) -> str:
    """Normalize planner vocabulary to the single mesh service contract."""
    operation = str(value or "prepare").strip().lower()
    aliases = {
        "mesh_generation": "prepare",
        "generate_mesh": "generate",
        "convert_gmsh_to_openfoam": "convert",
        "gmsh_to_openfoam": "convert",
    }
    return aliases.get(operation, operation)


def _mesh_flag(value: Any, default: bool) -> bool:
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _resolve_mesh_path(state: State, value: Any) -> Path | None:
    candidate = Path(str(value or "")).expanduser()
    if candidate.is_file():
        return candidate.resolve()
    if not candidate.is_absolute():
        roots = [Path(str(state.root))]
        workspace_root = getattr(state, "workspace_root", None)
        if workspace_root is not None:
            roots.insert(0, Path(str(workspace_root)))
        for root in roots:
            local = (root / candidate).resolve()
            if local.is_file():
                return local
    return None


def _canonical_mesh_discipline(
    declared: str,
    detected: dict[str, Any],
    params: dict[str, Any],
) -> str:
    """Map a descriptive scientific label to a registered mesh adapter."""
    from .geometry_assets import CAD_SOLID_SUFFIXES

    cad_input = Path(_geometry_file(params)).suffix.lower() in CAD_SOLID_SUFFIXES
    supported = {*_CONTINUUM_DISCIPLINES, "md"}
    if cad_input:
        supported.discard("md")
    raw = str(declared or "").strip().lower()
    analysis = params.get("requirement_analysis")
    analysis_discipline = (
        analysis.get("discipline") if isinstance(analysis, dict) else None
    )
    adapter = (
        str(analysis_discipline.get("adapter_route") or "").strip().lower()
        if isinstance(analysis_discipline, dict) else ""
    )
    if adapter in supported:
        return adapter
    if raw in supported:
        return raw
    aliases = {
        "computational fluid dynamics": "cfd",
        "fluid dynamics": "cfd",
        "finite element": "csm",
        "structural mechanics": "csm",
        "electromagnetic": "cem",
        "molecular dynamics": "md",
        "heat transfer": "heat_transfer",
    }
    for label, canonical in aliases.items():
        if canonical in supported and re.search(rf"(?<![a-z]){re.escape(label)}(?![a-z])", raw):
            return canonical
    for canonical in supported:
        if re.search(rf"(?<![a-z0-9]){re.escape(canonical)}(?![a-z0-9])", raw):
            return canonical
    if cad_input:
        # CAD is a continuum representation, not evidence of atomic structure.
        # An unspecified discipline must not override this explicit input type.
        return "multiphysics"
    detected_name = str(detected.get("primary_discipline") or "").strip().lower()
    if detected_name in supported:
        return detected_name
    return raw or "multiphysics"


def _parse_parameters(raw: str | dict[str, Any] | None) -> tuple[dict[str, Any], str | None]:
    if raw in (None, ""):
        return {}, None
    if isinstance(raw, dict):
        return dict(raw), None
    try:
        value = json.loads(str(raw))
    except json.JSONDecodeError as exc:
        return {}, f"parameters is not valid JSON: {exc}"
    if not isinstance(value, dict):
        return {}, "parameters must be a JSON object"
    return value, None


def _is_reference_computational_mesh(params: dict[str, Any]) -> bool:
    path = _geometry_file(params)
    if not path:
        return False
    suffix = Path(path).suffix.lower()
    representation = str(params.get("geometry_representation") or "").strip().lower()
    if suffix in _REFERENCE_COMPUTATIONAL_MESH_SUFFIXES - {".msh"}:
        return True
    return suffix == ".msh" and representation in {
        "reference_computational_mesh",
        "computational_mesh",
        "fluid_domain_mesh",
    }


def _generic_review(
    result: dict[str, Any],
    discipline: str,
    requested: dict[str, Any] | None = None,
) -> dict[str, Any]:
    existing = result.get("mesh_review")
    if isinstance(existing, dict) and existing.get("status"):
        return existing

    quality = result.get("quality") or {}
    if result.get("mesh_file") and quality:
        from .mesh_generator import _review_gmsh_mesh

        return _review_gmsh_mesh(
            result,
            {**dict(requested or {}), "discipline": discipline},
        )

    written = [Path(str(item)).expanduser() for item in result.get("written_files") or []]
    existing_files = [str(path) for path in written if path.exists() and path.is_file()]
    entity_count = (
        result.get("n_cells")
        or result.get("n_atoms")
        or result.get("n_points")
        or quality.get("n_elements")
        or quality.get("n_3d_elements")
        or quality.get("n_2d_elements")
    )
    checks = [
        {
            "name": "mesh_or_structure_files_exist",
            "status": "pass" if existing_files else "fail",
            "evidence": existing_files,
        },
        {
            "name": "positive_entity_count",
            "status": "pass" if isinstance(entity_count, (int, float)) and entity_count > 0 else "fail",
            "evidence": entity_count,
        },
    ]
    if quality:
        invalid = quality.get("invalid_elements", quality.get("n_invalid", 0))
        checks.append({
            "name": "no_reported_invalid_elements",
            "status": "pass" if invalid in (None, 0, 0.0) else "fail",
            "evidence": quality,
        })
    failed = [item for item in checks if item["status"] == "fail"]
    diagnostic_context = {
        "requested_dimension": (result.get("grid_params") or {}).get("mesh_dimension"),
        "observed_entity_counts": {
            key: quality.get(key)
            for key in ("n_2d_elements", "n_3d_elements", "n_elements")
            if key in quality
        },
        "geometry_file": result.get("geometry_file"),
        "generator_output_tail": str(result.get("gmsh_stdout_tail") or "")[-1200:],
    }
    return {
        "status": "fail" if failed else "pass",
        "scope": "cross_discipline_minimum_readiness",
        "discipline": discipline,
        "checks": checks,
        "issues": [
            {
                "severity": "critical",
                "code": item["name"],
                "message": f"Universal mesh readiness check failed: {item['name']}",
                "evidence": item["evidence"],
                "diagnostic_context": diagnostic_context,
                "required_change": (
                    "Revise the owning producer or its generated input so the materialized asset "
                    "contains entities of the requested dimension, then rerun this review."
                ),
            }
            for item in failed
        ],
    }


def _invalidate_failed_delivery(result: dict[str, Any]) -> dict[str, Any]:
    """Expose failed mesh assets as diagnostics only, never as deliverables."""
    review = result.get("mesh_review") or {}
    failed = result.get("status") in {"error", "needs_revision", "review_failed"} or review.get("status") == "fail"
    if not failed:
        if result.get("status") == "success":
            result.setdefault("deliverable_valid", True)
        return result
    result["deliverable_valid"] = False
    diagnostic_assets = list(result.get("diagnostic_assets") or [])
    for key in (
        "geo_file", "mesh_file", "quality_report", "tecplot_file", "tecplot_surface_file",
        "tecplot_volume_file", "polyMesh_dir",
    ):
        value = result.get(key)
        if value and value not in diagnostic_assets:
            diagnostic_assets.append(value)
        result[key] = None
    for value in result.get("tecplot_files") or []:
        if value not in diagnostic_assets:
            diagnostic_assets.append(value)
    result["tecplot_files"] = []
    result["diagnostic_assets"] = diagnostic_assets
    return result


def _normalize_public_result(value: Any) -> Any:
    """Normalize nested adapter mappings without changing scientific arrays."""
    if isinstance(value, dict):
        return {key: _normalize_public_result(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_normalize_public_result(item) for item in value]
    return value


def _control_receipt(
    requested: dict[str, Any],
    result: dict[str, Any],
) -> dict[str, Any]:
    """Report whether observable caller controls reached the producer output."""
    observed = result.get("grid_params") if isinstance(result.get("grid_params"), dict) else {}

    def matches(expected: Any, actual: Any) -> bool:
        if isinstance(expected, dict) and isinstance(actual, dict):
            return all(key in actual and matches(value, actual[key]) for key, value in expected.items())
        if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
            return math.isclose(float(expected), float(actual), rel_tol=1e-9, abs_tol=1e-12)
        return expected == actual

    applied = {
        key: observed[key]
        for key, value in requested.items()
        if key in observed and matches(value, observed[key])
    }
    mismatched = {
        key: {"requested": value, "observed": observed[key]}
        for key, value in requested.items()
        if key in observed and not matches(value, observed[key])
    }
    return {
        "status": "fail" if mismatched else "pass",
        "requested": requested,
        "observed": observed,
        "applied": applied,
        "mismatched": mismatched,
        "unobserved": sorted(key for key in requested if key not in observed),
    }


def _resolve_unfinished_geometry_state(
    state: State,
    result: dict[str, Any],
    params: dict[str, Any],
    discipline: str,
    spec: str,
) -> dict[str, Any]:
    """Expose one generic reference transition; the pipeline owns retry and resume."""
    status = str(result.get("status") or "")
    if status not in {"needs_geometry_processing", "needs_reference_search"}:
        return result

    queries = [
        " ".join(str(item).split())
        for item in result.get("search_queries") or []
        if str(item).strip()
    ]
    if not queries:
        queries = [
            " ".join(part for part in (
                discipline,
                spec[:180],
                "public geometry mesh coordinates scientific asset",
            ) if part)
        ]
    reference_request = (
        dict(result.get("reference_request"))
        if isinstance(result.get("reference_request"), dict)
        else {
            "id": "scientific_mesh_reference",
            "missing": (
                result.get("message")
                or result.get("error")
                or "public geometry or mesh evidence"
            ),
            "query": queries[0],
            "tool": "data_web_search",
            "web_search_allowed": True,
            "search_mode": "generic",
            "asset_kind": "geometry_or_mesh",
            "asset_role": "external_asset",
            "workflow_capability": "geometry_acquisition",
            "requires_exact_file": False,
            "auto_discover_downloads": True,
            "max_discovery_pages": 2,
        }
    )
    return {
        **result,
        "status": "needs_reference_search",
        "reference_request": reference_request,
        "search_queries": queries,
        "next_action": (
            "Execute the approved public discovery request. Materialize a selected result only "
            "through data_web_download, then retry this same mesh step with the verified local asset."
        ),
    }


async def prepare_scientific_mesh(
    *,
    state: State,
    spec: str = "",
    discipline: str = "",
    mesh_type: str = "",
    case_dir: str = "",
    parameters: str | dict[str, Any] = "",
    operation: str = "prepare",
    coordinate_files: Any = None,
    generate_mesh: bool = True,
    review_issues: str = "",
    reference_notes: str = "",
    **extra: Any,
) -> dict[str, Any]:
    """Resolve, generate, and review a mesh/particle structure for any discipline."""
    # Planner service payloads may contain a JSON-wrapped caller request plus
    # KB/status reminders.  Only the caller spec is allowed to influence mesh
    # routing; reminders must not turn a local analytic geometry into a public
    # reference search.
    spec = caller_request_text(spec) or str(spec or "")
    params, error = _parse_parameters(parameters)
    if error:
        return {"status": "error", "error": error}
    for key, value in extra.items():
        if value is not None and key not in params:
            params[key] = value
    # Reuse the package builder's existing caller-control extractor at the
    # actual mesh entry point. Previously these values were applied only when
    # packaging, after the mesh had already been generated and reviewed.
    from .scientific_preprocessor import (
        _extract_airfoil_mesh_density_numbers,
        _extract_numbers,
    )

    caller_controls = {
        **_extract_numbers(spec),
        **_extract_airfoil_mesh_density_numbers(spec),
    }
    # Optional evidence is request-driven.  A structured validator receipt and
    # a native mesher source are the compact defaults; retain bulky raw output
    # or add a wrapper only when the caller/model explicitly asks for them.
    if re.search(r"check\s*mesh.{0,12}(?:原始|完整|输出|日志|raw|full|log)", spec, flags=re.I):
        caller_controls["retain_validation_log"] = True
    if re.search(
        r"(?:一键|可执行|shell|bash|python).{0,16}(?:脚本|script)|"
        r"(?:脚本|script).{0,16}(?:一键|可执行|shell|bash|python)",
        spec,
        flags=re.I,
    ):
        caller_controls["require_executable_reproduction_script"] = True
    target_cells = int(caller_controls.get("target_cell_count") or 0)
    if target_cells > 0 and "timeout" not in caller_controls:
        # Preserve the established 120 s guard for ordinary jobs, while
        # scaling the existing execution budget for explicitly large meshes.
        caller_controls["timeout"] = min(
            3600,
            max(120, int(120 * max(1.0, target_cells / 100_000) ** 0.5)),
        )
    if "boundary_layer_first" in caller_controls:
        caller_controls["lock_near_wall_topology"] = True
    # The caller text remains authoritative even when an Analyst contract is
    # present. Analyst bindings add structured implementation detail below;
    # they must not erase explicit dimensions from the original request.
    params = {**params, **caller_controls}
    if reference_notes:
        params.setdefault("reference_notes", reference_notes)
    if review_issues:
        params["review_issues"] = review_issues
    pending_asset = (
        state.hook_state.get("scientific_mesh_pending_asset_evaluation")
        if isinstance(state.hook_state, dict)
        else None
    )
    if isinstance(pending_asset, dict):
        pending_path = str(pending_asset.get("path") or "").strip()
        pending_suffix = Path(pending_path).suffix.lower() if pending_path else ""
        if pending_path and pending_suffix in _COORDINATE_PROFILE_SUFFIXES:
            params.setdefault("coordinate_profile_path", pending_path)
            params.setdefault("profile_dat_path", pending_path)
            if re.search(r"\b(airfoil|aerofoil|profile|naca)\b|翼型", spec, flags=re.I):
                params.setdefault("airfoil_dat_path", pending_path)
        elif pending_path and not _geometry_file(params):
            params["geometry_file"] = pending_path
        if pending_path:
            source_trace = params.get("source_trace")
            if not isinstance(source_trace, list):
                source_trace = [] if source_trace in (None, "") else [source_trace]
                params["source_trace"] = source_trace
            source_trace.append({
                "source": "pending_downloaded_asset",
                "path": pending_path,
                "url": pending_asset.get("url"),
                "sha256": pending_asset.get("sha256"),
                "decision": "evaluate_before_additional_search",
            })
        state.hook_state.pop("scientific_mesh_pending_asset_evaluation", None)
    _apply_downloaded_geometry_assets_to_params(state, spec, params)

    # Apply the caller-declared mesh-stage contract at the single meshing entry
    # point. The package builder may reuse this result later, so waiting until
    # packaging would leave a valid-looking mesh generated from defaults.
    requirement_analysis = params.get("requirement_analysis")
    if isinstance(requirement_analysis, dict):
        # Requirement models may choose harmless group aliases (for example
        # ``boundary`` vs ``boundary_conditions``). Normalize once at the
        # producer boundary and pass the same locked values to every mesher.
        for required_file in requirement_analysis.get("required_files") or []:
            if not isinstance(required_file, dict):
                continue
            for group, values in canonical_parameter_bindings(
                required_file.get("parameter_bindings")
            ).items():
                if isinstance(values, dict):
                    current = params.get(group)
                    params[group] = {
                        **values,
                        **(current if isinstance(current, dict) else {}),
                    }
                else:
                    params.setdefault(group, values)
        params = flatten_parameter_groups(params)
        boundary_contract = [
            item for item in requirement_analysis.get("boundary_contract") or []
            if isinstance(item, dict)
            and str(item.get("role") or "").strip()
            and str(item.get("name") or "").strip()
        ]
        if boundary_contract:
            declared_role_names = {
                str(item["role"]): str(item["name"])
                for item in boundary_contract
            }
            params["boundary_role_names"] = declared_role_names
            if "boundary_role_names" in caller_controls:
                caller_controls["boundary_role_names"] = declared_role_names
            declared_boundary_map = {
                str(item["name"]): str(item.get("type") or "patch")
                for item in boundary_contract
            }
            params["boundary_map"] = declared_boundary_map
            params["expected_boundaries"] = [
                name for name, entity_type in declared_boundary_map.items()
                if entity_type.strip().lower() not in {
                    "internal", "volume", "region", "cellzone", "cell_zone",
                }
            ]
            if "expected_boundaries" in caller_controls:
                caller_controls["expected_boundaries"] = params["expected_boundaries"]
        calculation_stages = requirement_analysis.get("calculation_stages") or []
        if isinstance(calculation_stages, list) and calculation_stages:
            from .scientific_preprocessor import _apply_mesh_stage_requirements

            params = _apply_mesh_stage_requirements(
                params,
                [item for item in calculation_stages if isinstance(item, dict)],
            )

    # 判决拆除三波（sm:461 → schema，2026-09-02）：合法 operation 只在注册
    # schema 的 enum 里声明一次，派发口核一次；这里只做别名归一，不再二审。
    # 仓库内直接调用方（profile_mesh_variants / scientific_preprocessor）只传
    # 字面量 "prepare"。
    operation = normalize_mesh_operation(operation)

    detected = identify_discipline(
        file_path=_geometry_file(params) or None,
        text_sample=spec,
        metadata=params or None,
    )
    selected = _canonical_mesh_discipline(discipline, detected, params)
    from .geometry_assets import mesh_visualization_requested

    params.setdefault("write_tecplot", mesh_visualization_requested({
        "convert_to_openfoam": selected == "cfd", **params,
    }))

    reference_mesh = _is_reference_computational_mesh(params)
    if reference_mesh:
        params["geometry_representation"] = "reference_computational_mesh"
        params["computational_domain_complete"] = True
        params["domain_shape_verified"] = True
        params.setdefault("mesh_type", "geometry_file_gmsh")
        params.setdefault("remesh_reference_with_gmsh", True)
        source_trace = params.get("source_trace")
        if not isinstance(source_trace, list):
            source_trace = [] if source_trace in (None, "") else [source_trace]
            params["source_trace"] = source_trace
        if not any(
            isinstance(item, dict) and item.get("source") == "reference_computational_mesh"
            for item in source_trace
        ):
            source_trace.append({
                "source": "reference_computational_mesh",
                "path": _geometry_file(params),
                "decision": "extract_complete_domain_boundaries_then_regenerate_cells_with_gmsh",
            })

    coordinate_text = (
        params.get("coordinate_profile_text")
        or params.get("airfoil_coordinate_text")
        or params.get("profile_coordinate_text")
        or ""
    )
    coordinate_arrays = (
        params.get("coordinate_profile_coordinates")
        or params.get("airfoil_coordinates")
        or params.get("profile_coordinates")
    )
    inline_profile_supplied = coordinate_text not in (None, "") or coordinate_arrays not in (None, "", [], {})
    requested_profile_mesh = str(mesh_type or params.get("mesh_type") or "").strip().lower() in {
        "coordinate_profile_gmsh",
        "coordinate_profile_cascade_gmsh",
    }
    existing_profile_path = (
        params.get("coordinate_profile_path")
        or params.get("profile_dat_path")
        or params.get("airfoil_dat_path")
        or ""
    )
    coordinate_file_inputs = coordinate_files
    if coordinate_file_inputs in (None, "") and requested_profile_mesh and existing_profile_path:
        coordinate_file_inputs = [str(existing_profile_path)]
    should_build_profile = (
        coordinate_file_inputs not in (None, "")
        or operation == "build_profile"
        or (
            inline_profile_supplied
            and requested_profile_mesh
        )
    )
    if should_build_profile:
        from .coordinate_profile import build_coordinate_profile

        profile_result = await build_coordinate_profile(
            state=state,
            coordinate_files=coordinate_file_inputs,
            coordinate_text=coordinate_text,
            coordinate_arrays=coordinate_arrays,
            output_name=str(params.get("profile_output_name") or "closed_profile.dat"),
            profile_name=str(
                params.get("profile_name")
                or params.get("airfoil_name")
                or "derived_profile"
            ),
        )
        if profile_result.get("status") != "success" or operation == "build_profile":
            profile_result["workflow_tool"] = "prepare_scientific_mesh"
            return _normalize_public_result(profile_result)
        params["coordinate_profile_path"] = profile_result["coordinate_profile_path"]
        params.setdefault("profile_name", profile_result.get("profile_name"))
        source_trace = params.get("source_trace")
        if not isinstance(source_trace, list):
            source_trace = [] if source_trace in (None, "") else [source_trace]
            params["source_trace"] = source_trace
        source_trace.append({
            "source": "coordinate_profile_normalization",
            "files": profile_result.get("source_files"),
            "inline_coordinates": bool(inline_profile_supplied),
            "derived_geometry": profile_result.get("coordinate_profile_path"),
        })

    if operation == "resolve":
        from .mesh_iteration_advisor import _resolve_mesh_iteration_inputs

        result = await _resolve_mesh_iteration_inputs(
            state,
            spec=spec,
            case_type=str(params.get("case_type") or mesh_type or ""),
            parameters=json.dumps(params, ensure_ascii=False),
            review_issues=review_issues,
            reference_notes=reference_notes,
        )
        result.setdefault("discipline", selected)
        result.setdefault("workflow_tool", "prepare_scientific_mesh")
        return _normalize_public_result(result)

    if operation == "convert":
        source = _resolve_mesh_path(
            state,
            _geometry_file(params)
            or (coordinate_files[0] if isinstance(coordinate_files, (list, tuple)) and coordinate_files else coordinate_files),
        )
        if source is None:
            return _normalize_public_result({
                "status": "needs_input",
                "reason": "mesh_conversion_source_missing",
                "required_fields": ["geometry_file or coordinate_files containing an existing .msh/.geo asset"],
                "workflow_tool": "prepare_scientific_mesh",
            })
        from .mesh_generator import _safe_case_dir_under_state, generate_geometry_file_gmsh_mesh

        case_root = _safe_case_dir_under_state(
            state,
            case_dir or params.get("case_dir") or "",
            default_name="mesh_conversion",
        )
        reserved = {
            "geometry_file", "case_dir", "convert_to_openfoam", "write_tecplot",
            "mesh_dimension", "characteristic_length", "gmsh_binary", "gmsh_to_foam_cmd",
            "timeout",
        }
        result = generate_geometry_file_gmsh_mesh(
            geometry_file=str(source),
            case_dir=case_root,
            convert_to_openfoam=_mesh_flag(params.get("convert_to_openfoam"), True),
            write_tecplot=_mesh_flag(params.get("write_tecplot"), True),
            mesh_dimension=params.get("mesh_dimension") or params.get("dimension"),
            characteristic_length=params.get("characteristic_length"),
            gmsh_binary=str(params.get("gmsh_binary") or "gmsh"),
            gmsh_to_foam_cmd=str(params.get("gmsh_to_foam_cmd") or "openfoam gmshToFoam"),
            timeout=int(params.get("timeout") or 180),
            **{key: value for key, value in params.items() if key not in reserved},
        )
        result["discipline"] = selected
        result["workflow_tool"] = "prepare_scientific_mesh"
        if result.get("status") != "error" and generate_mesh:
            review = _generic_review(result, selected, params)
            result["mesh_review"] = review
            if review.get("status") != "pass":
                result["status"] = "error"
                result["error"] = "Converted mesh failed universal readiness review."
        return _normalize_public_result(_invalidate_failed_delivery(result))

    if operation == "prepare" and selected == "cfd" and not reference_mesh:
        from .cfd_case_router import _prepare_cfd_mesh_case

        result = await _prepare_cfd_mesh_case(
            state,
            spec=spec,
            case_type=str(params.get("case_type") or mesh_type or ""),
            case_dir=case_dir,
            parameters=json.dumps(params, ensure_ascii=False),
            generate_mesh=generate_mesh,
            review_issues=review_issues,
        )
    else:
        from .mesh_generator import _generate_computational_mesh

        generator_discipline = selected
        if selected == "unknown" and _geometry_file(params):
            # Reuse the internal generic-continuum adapter without changing the
            # task's scientific discipline or inventing solver semantics.
            generator_discipline = "multiphysics"
            params["adapter_role"] = "generic_continuum_geometry_meshing"
            params["declared_discipline"] = "unknown"
        elif selected == "unknown" and not _geometry_file(params):
            result = {
                "status": "needs_reference_search",
                "reason": "unknown_discipline_mesh_requires_explicit_geometry_or_domain",
                "required_fields": [
                    "geometry/domain asset compatible with the requested spatial discretization",
                    "boundary or region semantics required by the downstream consumer",
                    "units or scale when not encoded in the asset",
                ],
                "search_queries": [
                    " ".join(part for part in (
                        spec[:180],
                        "authoritative geometry computational domain dataset direct download",
                    ) if part)
                ],
            }
            result.setdefault("discipline", selected)
            result.setdefault("discipline_detection", detected)
            result.setdefault("workflow_tool", "prepare_scientific_mesh")
            return _normalize_public_result(
                _resolve_unfinished_geometry_state(state, result, params, selected, spec)
            )
        if selected in _CONTINUUM_DISCIPLINES and _geometry_file(params):
            mesh_type = "geometry_file_gmsh"
            params.setdefault("convert_to_openfoam", selected == "cfd")
        elif selected == "unknown" and _geometry_file(params):
            mesh_type = "geometry_file_gmsh"
            params.setdefault("convert_to_openfoam", False)
        result = await _generate_computational_mesh(
            state,
            discipline=generator_discipline,
            mesh_type=mesh_type,
            geometry=spec,
            case_dir=case_dir,
            parameters=json.dumps(params, ensure_ascii=False),
        )

    result["discipline"] = selected
    if selected == "unknown" and _geometry_file(params):
        result.setdefault("adapter_role", "generic_continuum_geometry_meshing")
    result.setdefault("discipline_detection", detected)
    result.setdefault("workflow_tool", "prepare_scientific_mesh")
    result = _resolve_unfinished_geometry_state(state, result, params, selected, spec)
    if result.get("status") == "success":
        analysis = params.get("requirement_analysis")
        analysis = analysis if isinstance(analysis, dict) else {}
        result.setdefault("parameter_basis", {
            "request_controls": caller_controls,
            "assumptions": analysis.get("assumptions") or [],
            "unresolved_facts": analysis.get("unresolved_facts") or [],
        })
        receipt = _control_receipt(caller_controls, result)
        result["control_receipt"] = receipt
        if receipt["status"] != "pass":
            issues = [{
                "code": "caller_control_not_applied",
                "severity": "critical",
                "message": f"Explicit caller control {name!r} was not applied.",
                "required_change": (
                    "Regenerate the same asset using the locked caller value; do not alter authority."
                ),
                "control": name,
                **values,
            } for name, values in receipt["mismatched"].items()]
            result["status"] = "needs_revision"
            result["error"] = (
                "The producer did not apply one or more explicit caller controls: "
                + ", ".join(receipt["mismatched"])
            )
            result["revision_contract"] = revision_contract(issues, scope="asset")
    if result.get("status") == "success" and generate_mesh:
        review = _generic_review(result, selected, params)
        result["mesh_review"] = review
        if review.get("status") != "pass":
            result["status"] = "needs_revision"
            result["error"] = "Generated mesh/structure failed universal readiness review."
            result["revision_contract"] = revision_contract(
                review.get("issues") or [], scope="asset"
            )
    return _normalize_public_result(_invalidate_failed_delivery(result))


async def _gated_prepare_scientific_mesh(state: State, **kwargs: Any) -> dict[str, Any]:
    operation = normalize_mesh_operation(kwargs.get("operation"))
    generate_mesh = bool(kwargs.get("generate_mesh", True))
    stamp: dict[str, Any] = {}
    if operation in {"prepare", "generate", "convert"} and generate_mesh:
        # 判决拆除 O9（随根 store:468 降格，2026-08-31）：评审不再是通行许可；
        # 未评审执行照跑，产物打 plan_approval_status:unapproved。
        stamp = witness_plan_approval(state, "prepare_scientific_mesh")
    result = await prepare_scientific_mesh(state=state, **kwargs)
    if isinstance(result, dict) and stamp:
        result.setdefault("plan_approval_status", stamp["plan_approval_status"])
    return result


# 深层网格生成器里那些「传错就被拒」的参数，声明只有一份（在 mesh_generator
# 里，校验器就长在那）。这里把同一份声明挂到模型唯一看得见的入口上 ——
# register_tool 会把它渲染进 description，模型在**调用前**就读得到。
from .mesh_generator import MESH_PARAMETER_CONTRACT  # noqa: E402

register_tool(
    ToolDefinition(
        name="prepare_scientific_mesh",
        description=(
            "跨学科网格总工具，也是 data 节点唯一公开网格入口。自动识别 CFD、结构力学、"
            "计算电磁、传热、分子动力学或多物理场，完成缺参分流、二维坐标合成、参数化/"
            "Gmsh 网格或粒子构型生成、质量审核和自迭代续跑。CFD 只是内部路由分支。"
            "默认 operation=prepare；仅需缺参分析用 resolve，仅合并坐标用 build_profile，"
            "绕过高层路由直接生成时用 generate；已有 Gmsh 资产转 OpenFOAM 用 convert。"
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "spec": {"type": "string", "default": "", "description": "用户原始网格/结构需求。"},
                "discipline": {"type": "string", "default": "", "description": "可选学科；留空自动识别。"},
                "mesh_type": {"type": "string", "default": "", "description": "可选网格类型；留空按学科和几何路由。"},
                "case_dir": {"type": "string", "default": "", "description": "可选输出目录，最终限制在当前 state.root。"},
                "parameters": {"description": "JSON 对象或 JSON 字符串，包含几何、尺度、边界、密度和输出参数。"},
                "operation": {
                    "type": "string",
                    "enum": ["prepare", "resolve", "build_profile", "generate", "convert"],
                    "default": "prepare",
                },
                "coordinate_files": {"description": "可选二维坐标文件列表；提供后自动合并为闭合 profile。"},
                "generate_mesh": {"type": "boolean", "default": True},
                "review_issues": {"type": "string", "default": ""},
                "reference_notes": {"type": "string", "default": ""},
                "retain_validation_log": {
                    "type": "boolean",
                    "description": "仅在调用方或规划模型要求完整验证器原始输出时启用。",
                },
                "require_executable_reproduction_script": {
                    "type": "boolean",
                    "description": "仅在明确要求一键/可执行复现包装脚本时启用；原生 mesher 输入默认已可复现。",
                },
            },
            "additionalProperties": True,
        },
        allowed_node_types=["data"],
        risk_level="low",
        content_contract=MESH_PARAMETER_CONTRACT,
    ),
    _gated_prepare_scientific_mesh,
)

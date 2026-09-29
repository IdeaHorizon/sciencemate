"""scientific_preprocessor -- build simulation-ready preprocessing packages.

This tool sits above the lower-level discipline identifier and mesh generator.
It turns a natural language scientific computing request into a reproducible
input package: manifest, solver input decks, optional mesh/structure assets,
and a data-driven model training scaffold when tabular data is available.
"""
from __future__ import annotations

import csv
import copy
from fractions import Fraction
import hashlib
from itertools import product
import json
import math
import os
import re
import shutil
import sys
import tarfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from core import paths
from core.state import State
from nodes.data.pipeline_contract import merge_parameter_updates, revision_contract
from nodes.data.planning.schemas import canonical_workflow_capability, normalize_asset_contract
from nodes.data.planning.store import approved_plan, witness_plan_approval
from nodes.data.planning.request_contract import (
    build_preprocessing_work_order,
    normalize_preprocessing_request,
)
from nodes.data.progress import emit_progress
from nodes.data.stage_semantics import (
    canonical_stage_parameter_key,
    dependency_artifact_ready,
    execution_kind as stage_execution_kind,
    extract_explicit_stage_parameters,
    is_xfoil_stage,
    produces_runtime_result,
    requests_parameter_inheritance,
    stage_family,
    stage_kind,
    stage_text,
    structure_source_role,
    structure_variant,
)

from .discipline_identifier import identify_discipline
from .cfd_case_router import _extract_airfoil_name
from .geometry_assets import (
    apply_downloaded_geometry_assets_to_params as _apply_downloaded_geometry_assets_to_params,
    canonical_condition_region,
    canonical_parameter_bindings,
    flatten_parameter_groups,
    geometry_file_from_params,
)
from .human_input_utils import (
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
    explicit_mesh_contract as _match_cfd_case_profile,
    _resolve_mesh_iteration_inputs,
    detect_special_domain_requirements,
    geometry_parameters_need_reference_resolution,
    looks_like_turbomachinery_blade,
    public_geometry_reference_request,
    special_domain_reference_request,
)
from .scientific_assets import (
    downloaded_dataset_candidates,
    inspect_scientific_asset_path,
)
from .package_publisher import (
    discard_incomplete_delivery as _discard_incomplete_preprocessing_delivery,
    prepare_package_staging,
    remap_published_paths,
    stage_runtime_mesh_asset as _stage_runtime_mesh_asset,
)


DISCIPLINE_DEFAULTS: dict[str, dict[str, Any]] = {
    "cfd": {
        "solver_family": "OpenFOAM",
        "mesh_type": "generic_gmsh",
        "quality_gates": [
            "mesh has positive cells and named boundary patches",
            "velocity, pressure, viscosity, and time controls are explicit",
            "Re/Ma regime is stated or marked unknown",
        ],
    },
    "heat_transfer": {
        "solver_family": "OpenFOAM",
        "mesh_type": "generic_gmsh",
        "quality_gates": [
            "thermal properties required by the declared steady/transient formulation are explicit",
            "temperature and flux/insulation boundaries match the request; do not invent a source",
            "mesh resolves declared thermal gradients near walls and interfaces",
        ],
    },
    "md": {
        "solver_family": "LAMMPS",
        "mesh_type": "atomistic_structure",
        "quality_gates": [
            "atom count, box bounds, mass, unit style, and boundary style are explicit",
            "thermostat/barostat choice is declared",
            "time step and run length are stated",
        ],
    },
    "csm": {
        "solver_family": "Abaqus/CalculiX",
        "mesh_type": "generic_gmsh",
        "quality_gates": [
            "units, material law, constraints, and load regions are explicit",
            "element family and mesh density are stated",
            "small/large deformation assumption is stated",
        ],
    },
    "cem": {
        "solver_family": "openEMS/HFSS-style",
        "mesh_type": "generic_gmsh",
        "quality_gates": [
            "frequency band, excitations, material permittivity/permeability are explicit",
            "boundary condition type is stated",
            "mesh resolution respects wavelength guidance",
        ],
    },
    "electronic_structure": {
        "solver_family": "VASP-style DFT",
        "mesh_type": "atomistic_structure",
        "quality_gates": [
            "INCAR declares method, cutoff, convergence, and relaxation/static settings",
            "KPOINTS declares a reproducible reciprocal-space sampling policy",
            "POSCAR is present only when authoritative lattice and coordinates are available",
            "POTCAR is assembled from an authorized local pseudopotential library in POSCAR species order",
        ],
    },
    "multiphysics": {
        "solver_family": "coupled workflow",
        "mesh_type": "generic_gmsh",
        "quality_gates": [
            "participating physics and exchanged fields are explicit",
            "interface mapping and time coupling strategy are stated",
            "each region has a compatible mesh/input contract",
        ],
    },
    "unknown": {
        "solver_family": "external downstream consumer",
        "mesh_type": "none",
        "quality_gates": [
            "input assets are readable and structurally characterized",
            "unknown semantics are explicit assumptions rather than inferred defaults",
            "the downstream file and parameter contract is recorded",
        ],
    },
}

def _canonical_cfd_mesh_matches_original_intent(
    state: State,
    mesh_type: str,
    intent_text: str,
    params: dict[str, Any],
) -> bool:
    if surrogate_geometry_explicitly_approved(params):
        return True
    pattern = CANONICAL_CFD_MESH_INTENT_PATTERNS.get(str(mesh_type or "").strip().lower())
    if not pattern:
        return True
    original = original_user_request_text_from_state(state)
    source_text = original or intent_text
    if looks_like_turbomachinery_blade(source_text or "", params):
        return False
    return bool(re.search(pattern, source_text or "", flags=re.IGNORECASE))


def _slugify(text: str, fallback: str = "scientific_case") -> str:
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", text.strip())[:80].strip("_")
    return slug or fallback


def _json_dumps(data: Any) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True)


def _canonical_package_discipline(discipline: str, detected: dict[str, Any], spec: str, params: dict[str, Any]) -> str:
    raw = (discipline or detected.get("primary_discipline") or "").strip().lower()
    # The caller scope is authoritative. A package call may still
    # contain broad words such as ``surface``/``grid`` or a stale adapter hint;
    # those terms cannot promote an atmospheric/data task to the CFD adapter.
    analysis = params.get("requirement_analysis")
    scope = analysis.get("task_scope") if isinstance(analysis, dict) else None
    declared_scope = ""
    declared_route = ""
    if isinstance(scope, dict):
        scope_discipline = scope.get("discipline")
        declared_scope = str(
            scope_discipline.get("primary")
            if isinstance(scope_discipline, dict)
            else scope_discipline or ""
        ).strip().lower()
        allowed = {str(item).strip().lower() for item in scope.get("allowed_capabilities") or []}
        excluded = {str(item).strip().lower() for item in scope.get("excluded_capabilities") or []}
        analysis_discipline = analysis.get("discipline") if isinstance(analysis, dict) else None
        if isinstance(analysis_discipline, dict):
            declared_route = str(
                analysis_discipline.get("adapter_route")
                or analysis_discipline.get("primary")
                or ""
            ).strip().lower()
        if declared_route in DISCIPLINE_DEFAULTS and declared_route != "unknown":
            return declared_route
        # A caller-declared domain always wins over adapter keyword
        # detection.  This also covers new domains and prevents unrelated
        # VASP/CFD terms in evidence or prose from selecting another adapter.
        if declared_scope and declared_scope not in DISCIPLINE_DEFAULTS and declared_route not in {"", "generic", "unknown"}:
            return declared_route
        if declared_scope and declared_scope not in DISCIPLINE_DEFAULTS:
            return declared_scope
        if declared_scope and raw and raw != declared_scope and declared_scope in DISCIPLINE_DEFAULTS:
            return declared_scope
        if declared_scope and (
            "mesh_generation" not in allowed
            or "geometry_acquisition" in excluded
            or "mesh_generation" in excluded
        ) and raw in {"cfd", "computational_fluid_dynamics", "openfoam", "fluid_dynamics"}:
            return declared_scope
        if declared_scope and raw in {"", "unknown", "unspecified"}:
            return declared_scope
    text = " ".join([
        raw,
        spec,
        json.dumps(params, ensure_ascii=False, default=str),
    ])
    if raw in {
        "materials", "material", "materials_science", "computational_materials_science",
        "electronic_structure", "dft", "quantum_espresso", "qe", "cp2k", "vasp",
    }:
        return "electronic_structure"
    if re.search(
        r"\b(VASP|DFT|INCAR|KPOINTS|POSCAR|POTCAR|PAW|CP2K|Quantum\s+ESPRESSO|pw\.x|QE|赝势|电子结构|第一性原理)\b",
        text,
        flags=re.I,
    ):
        return "electronic_structure"
    for canonical in DISCIPLINE_DEFAULTS:
        if canonical == "unknown":
            continue
        if re.search(rf"(?<![a-z0-9]){re.escape(canonical)}(?![a-z0-9])", raw):
            return canonical
    analysis = params.get("requirement_analysis")
    analysis_discipline = analysis.get("discipline") if isinstance(analysis, dict) else None
    adapter = (
        str(analysis_discipline.get("adapter_route") or "").strip().lower()
        if isinstance(analysis_discipline, dict) else ""
    )
    if adapter in DISCIPLINE_DEFAULTS:
        return adapter
    detected_name = str(detected.get("primary_discipline") or "").strip().lower()
    # Detection may fill an omitted/unspecified discipline, including the
    # common planner fallback ``unknown``.  A genuinely named new discipline
    # remains untouched; only a registered adapter inferred with evidence is
    # selected here.
    if raw in {"", "unknown", "unspecified", "未确定", "未知"} and detected_name in DISCIPLINE_DEFAULTS:
        return detected_name
    return raw


def _package_discipline_identity(
    discipline: str,
    detected: dict[str, Any],
    spec: str,
    params: dict[str, Any],
) -> tuple[str, str, str]:
    """Separate scientific identity from the internal generation adapter."""
    declared = str(
        params.get("declared_discipline")
        or discipline
        or detected.get("primary_discipline")
        or "unknown"
    ).strip()
    canonical = _canonical_package_discipline(discipline, detected, spec, params)
    if canonical in DISCIPLINE_DEFAULTS:
        adapter = canonical
        reported = declared or canonical
    else:
        # A new discipline remains itself in the artifact. The generic adapter
        # is only an implementation route and must not relabel the science.
        adapter = "unknown"
        reported = declared or canonical or "unknown"
    if declared.lower() not in {"", "unknown", "unspecified", "未确定", "未知"}:
        params["declared_discipline"] = declared
    return reported, adapter, declared


def _explicit_required_file_names(params: dict[str, Any]) -> set[str]:
    names: set[str] = set()
    analysis = params.get("requirement_analysis")
    if isinstance(analysis, dict):
        for item in analysis.get("required_files") or []:
            if not isinstance(item, dict):
                continue
            for key in ("id", "name_or_role", "format"):
                value = str(item.get(key) or "").strip()
                if value:
                    names.add(value.lower())
    for value in _as_list(params.get("required_files")):
        if isinstance(value, str) and value.strip():
            names.add(value.strip().lower())
    return names


def _request_bound_needs_solver_templates(params: dict[str, Any]) -> bool:
    """Return whether a caller-scoped request actually names solver inputs.

    A request-bound mesh is an asset delivery, not a request for a runnable CFD
    case.  Keeping this decision next to the existing template selector makes
    the package builder follow the normalized asset contract instead of an
    internal ``write_domain_templates`` hint from a fallback plan.
    """
    analysis = params.get("requirement_analysis")
    if not isinstance(analysis, dict):
        return True
    request = analysis.get("preprocessing_request")
    if not isinstance(request, dict):
        return True
    if request.get("review_profile") != "request_bound":
        return True
    assets = request.get("requested_assets")
    if not isinstance(assets, list) or not assets:
        assets = analysis.get("required_files") or []
    for item in assets:
        if not isinstance(item, dict):
            item = {"name_or_role": item}
        contract = normalize_asset_contract(item)
        representation = str(contract.get("representation") or "").casefold()
        role_text = " ".join(
            str(item.get(key) or "")
            for key in (
                "id", "asset_id", "name_or_role", "filename", "format",
                "scientific_role", "purpose", "reason", "content_constraints",
            )
        ).casefold()
        composite_solver_asset = bool(re.search(
            r"simulation[_ -]?case|solver[_ -]?input|input[_ -]?deck|complete[_ -]?.*input",
            role_text,
            flags=re.I,
        ))
        if representation == "simulation_case" or composite_solver_asset:
            return True
        if representation == "mesh" or re.search(
            r"mesh|checkmesh|quality|audit|visualization|polyMesh|网格|质量|审计",
            role_text,
            flags=re.I,
        ):
            continue
        capability = canonical_workflow_capability(item)
        if capability in {"configuration_generation", "preprocessing_script_generation"}:
            return True
        if re.search(
            r"(?:^|/)(?:0/(?:u|p)|controlDict|fvSchemes|fvSolution|transportProperties|"
            r"incar|kpoints|poscar|potcar|namelist|.*\.nml|.*\.cfg|.*\.conf)$",
            role_text,
            flags=re.I,
        ):
            return True
    return False


def _should_write_discipline_templates(selected_discipline: str, params: dict[str, Any]) -> bool:
    if params.get("domain_specific_generation_deferred") is True:
        return False
    if not _request_bound_needs_solver_templates(params):
        return False
    explicit = str(params.get("write_domain_templates") or params.get("generate_solver_templates") or "").lower()
    if explicit in {"1", "true", "yes"}:
        return True
    required_names = " ".join(sorted(_explicit_required_file_names(params)))
    if selected_discipline == "electronic_structure":
        return bool(
            required_names
            or params.get("material_input_files")
            or params.get("solver_input_files")
        )
    if selected_discipline == "md":
        return bool(re.search(r"\b(lammps|in\.lammps|lammps\.data)\b", required_names, flags=re.I))
    if selected_discipline in {"cfd", "heat_transfer"}:
        return bool(re.search(r"\b(controlDict|fvSchemes|fvSolution|transportProperties|0/U|0/p|openfoam)\b", required_names, flags=re.I)) or (
            selected_discipline == "heat_transfer" and bool(re.search(r"\binp\b|abaqus|calculix", required_names, flags=re.I))
        )
    if selected_discipline in {"csm", "cem", "multiphysics"}:
        return bool(required_names)
    return False


def _explicit_mesh_assets_requested(spec: str, selected_discipline: str, params: dict[str, Any]) -> bool:
    if params.get("domain_specific_generation_deferred") is True:
        return False
    explicit = str(params.get("generate_mesh_assets") or params.get("generate_mesh") or "").strip().lower()
    if explicit in {"1", "true", "yes"}:
        return True
    if explicit in {"0", "false", "no"}:
        return False
    required_names = " ".join(sorted(_explicit_required_file_names(params)))
    text = " ".join([
        spec,
        required_names,
        str(params.get("mesh_type") or ""),
        str(params.get("case_type") or ""),
    ])
    if selected_discipline == "electronic_structure":
        return False
    return bool(re.search(
        r"\b(computational mesh|finite element mesh|volume mesh|surface mesh|polyMesh|gmsh|mesh asset|mesh file|\.msh)\b|网格",
        text,
        flags=re.I,
    ))


def _material_solver(spec: str, params: dict[str, Any]) -> str:
    text = " ".join(str(params.get(key) or "") for key in (
        "simulation_software", "software", "solver", "solver_family", "application",
    ))
    text = f"{text}\n{spec}".lower()
    if "quantum espresso" in text or re.search(r"\bqe\b|pw\.x", text):
        return "quantum_espresso"
    if "cp2k" in text:
        return "cp2k"
    if "vasp" in text or re.search(r"\b(incar|poscar|potcar|kpoints)\b", text):
        return "vasp"
    return "generic_materials"


def _material_defaults(base: dict[str, Any], solver: str) -> dict[str, Any]:
    defaults = {**base, "quality_gates": list(base.get("quality_gates") or [])}
    if solver == "quantum_espresso":
        defaults.update({
            "solver_family": "Quantum ESPRESSO",
            "quality_gates": [
                "pw.x namelists and cards are syntactically complete",
                "ATOMIC_SPECIES and ATOMIC_POSITIONS mappings are consistent",
                "ecutwfc and reciprocal-space sampling are explicit",
            ],
        })
    elif solver == "cp2k":
        defaults.update({
            "solver_family": "CP2K",
            "quality_gates": [
                "GLOBAL, FORCE_EVAL, DFT, SUBSYS, CELL, and COORD sections are complete",
                "every coordinate species has a KIND basis and potential mapping",
                "calculation type and numerical settings are explicit",
            ],
        })
    elif solver == "generic_materials":
        defaults.update({
            "solver_family": "materials simulation software",
            "quality_gates": [
                "structure species and coordinates are resolved",
                "solver input files satisfy the approved plan contract",
                "basis or pseudopotential mappings are explicit when required",
            ],
        })
    return defaults


def _provided_material_files(params: dict[str, Any]) -> dict[str, str]:
    raw = params.get("material_input_files") or params.get("solver_input_files") or {}
    if not isinstance(raw, dict):
        return {}
    return {
        str(name): str(content)
        for name, content in raw.items()
        if str(name).strip() and str(content).strip()
    }


def _stable_id(prefix: str, value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    return f"{prefix}:{digest}"


def _as_list(value: Any) -> list[Any]:
    if value is None or value == "":
        return []
    return value if isinstance(value, list) else [value]


def _build_universal_manifest_contract(
    *,
    state: State,
    spec: str,
    data_path: str,
    selected_discipline: str,
    params: dict[str, Any],
    defaults: dict[str, Any],
    dataset_profile: dict[str, Any],
    mesh_type: str,
    mesh_generation_result: dict[str, Any] | None,
    package_dir: Path,
    written_file_names: list[str],
) -> dict[str, Any]:
    """Build the universal data-readiness contract without inventing semantics."""
    generated_at = datetime.now(timezone.utc).isoformat()
    mesh_result = mesh_generation_result or {}
    mesh_review = mesh_result.get("mesh_review") or {}

    declared_kind = str(params.get("data_model_kind") or "").strip().lower()
    allowed_kinds = {
        "table", "time_series", "tensor", "spatial_field", "mesh",
        "particle_structure", "graph", "spectrum", "text_log",
        "simulation_case", "multimodal_bundle", "unknown",
    }
    if declared_kind in allowed_kinds:
        data_kind = declared_kind
        modalities = [str(item) for item in _as_list(params.get("modalities"))]
    elif str(dataset_profile.get("data_model_kind") or "") in allowed_kinds:
        data_kind = str(dataset_profile["data_model_kind"])
        modalities = [data_kind]
    elif data_path and Path(data_path).suffix.lower() in {".csv", ".tsv"}:
        data_kind = "table"
        modalities = ["tabular_data"]
    elif selected_discipline == "md":
        data_kind = "particle_structure"
        modalities = ["particle_structure", "simulation_case"]
    elif mesh_generation_result:
        data_kind = "mesh"
        modalities = ["mesh", "simulation_case"]
    else:
        data_kind = "simulation_case"
        modalities = ["simulation_case"]

    source_inputs: list[dict[str, Any]] = [
        {
            "kind": "user_specification",
            "stable_id": _stable_id("spec", spec),
        }
    ]
    if data_path:
        source_inputs.append({
            "kind": "data_file",
            "path": str(Path(data_path).expanduser()),
            "exists": bool(dataset_profile.get("exists")),
            "format": dataset_profile.get("format"),
            "asset_kind": dataset_profile.get("asset_kind"),
            "sha256": dataset_profile.get("sha256"),
        })
    for key in (
        "geometry_file", "cad_file", "stl_file", "step_file", "geo_file",
        "msh_file", "coordinate_profile_path", "profile_dat_path", "airfoil_dat_path",
    ):
        if params.get(key):
            source_inputs.append({"kind": key, "path": str(params[key])})
    for item in _as_list(params.get("source_trace")):
        source_inputs.append({"kind": "reference", "value": item})

    semantic_control_keys = {
        "mesh_type", "case_type", "quality_preset", "convert_to_openfoam",
        "write_tecplot", "timeout", "seed", "source_trace",
    }
    quantities = [
        {
            "name": key,
            "value": value,
            "unit": params.get(f"{key}_unit"),
        }
        for key, value in sorted(params.items())
        if key not in semantic_control_keys
        and not key.endswith("_unit")
        and isinstance(value, (int, float))
        and not isinstance(value, bool)
    ]

    assumptions: list[str] = list(_as_list(params.get("assumptions")))
    if quantities and any(item["unit"] is None for item in quantities):
        assumptions.append("One or more numeric quantities have no user-declared unit; no unit was inferred.")
    if not params.get("coordinate_frames") and not params.get("coordinate_frame"):
        assumptions.append("Coordinate frame was not provided; downstream consumers must confirm it when relevant.")
    if data_kind in {"time_series", "tensor", "spatial_field", "spectrum", "graph"} and not params.get("axes"):
        assumptions.append(f"Axis semantics for the {data_kind} asset were not explicitly provided.")
    if data_kind in {"time_series", "spatial_field", "spectrum"} and not params.get("units"):
        assumptions.append(f"Variable units for the {data_kind} asset were not explicitly provided.")
    if not params.get("protocol_or_method"):
        assumptions.append("Acquisition or preparation protocol was not provided.")
    if not params.get("reference_standards"):
        assumptions.append("Reference standard was not provided.")
    if not params.get("instrument"):
        assumptions.append("Instrument and calibration context were not provided or are not applicable to this generated input package.")

    quality_gates: list[dict[str, Any]] = [
        {
            "name": "package_directory_created",
            "status": "pass" if package_dir.is_dir() else "fail",
            "evidence": str(package_dir),
        },
        {
            "name": "source_readable",
            "status": "pass" if not data_path or bool(dataset_profile.get("exists")) else "fail",
            "evidence": dataset_profile if data_path else "No external data file was required.",
        },
    ]
    if data_path:
        quality_gates.extend(
            dict(gate)
            for gate in dataset_profile.get("quality_gates") or []
            if isinstance(gate, dict)
        )
    if mesh_generation_result:
        review_status = str(mesh_review.get("status") or "").lower()
        quality_gates.append({
            "name": "mesh_generation_and_review",
            "status": "pass" if mesh_result.get("status") == "success" and review_status == "pass" else "fail",
            "evidence": {
                "generation_status": mesh_result.get("status"),
                "mesh_review_status": mesh_review.get("status"),
                "mesh_review_report": mesh_result.get("mesh_review_report"),
            },
        })
    lineage = [
        {
            "input": "node_inputs.spec",
            "op": "characterize_and_prepare_scientific_data",
            "params": {
                "discipline": selected_discipline,
                "mesh_type": mesh_type,
                "parameter_stable_id": _stable_id("params", params),
            },
            "output": str(package_dir),
            "timestamp": generated_at,
            "stable_id": _stable_id("package", {
                "run_id": state.run_id,
                "package_dir": str(package_dir),
                "files": written_file_names,
            }),
        }
    ]
    if mesh_generation_result:
        lineage.append({
            "input": source_inputs,
            "op": "generate_and_review_mesh",
            "params": {"mesh_type": mesh_type},
            "output": mesh_result.get("written_files") or str(package_dir),
            "timestamp": generated_at,
            "stable_id": _stable_id("mesh", mesh_result.get("written_files") or mesh_result),
        })

    known_limitations: list[str] = []
    if assumptions:
        known_limitations.append("Unresolved semantics are listed in assumptions and require downstream confirmation.")
    if not mesh_generation_result and data_kind in {"mesh", "simulation_case"}:
        known_limitations.append("No mesh asset was generated in this preprocessing call.")

    expected_files = sorted(set(written_file_names))
    return {
        "objective": spec.strip(),
        "discipline": {
            "declared_or_detected": params.get("declared_discipline") or selected_discipline,
            "adapter_route": (
                "generic"
                if selected_discipline == "unknown"
                and str(params.get("declared_discipline") or "").lower() not in {"", "unknown"}
                else selected_discipline
            ),
        },
        "data_model": {
            "kind": data_kind,
            "description": (
                f"Scientific preprocessing package for "
                f"{params.get('declared_discipline') or selected_discipline}."
            ),
            "modalities": modalities,
        },
        "source": {
            "inputs": source_inputs,
            "owner": params.get("source_owner"),
            "license": params.get("source_license"),
        },
        "semantics": {
            "quantities": quantities,
            "units": _as_list(params.get("units")),
            "axes": _as_list(params.get("axes")),
            "coordinate_frames": _as_list(params.get("coordinate_frames") or params.get("coordinate_frame")),
            "topology": params.get("topology") or mesh_review.get("mesh_intent"),
        },
        "measurement_context": {
            "instrument": params.get("instrument"),
            "calibration": _as_list(params.get("calibration")),
            "reference_standards": _as_list(params.get("reference_standards")),
            "protocol_or_method": params.get("protocol_or_method"),
            "acquisition_settings": params.get("acquisition_settings") or {},
            "processing_defaults": params.get("processing_defaults") or {},
        },
        "quality_gates": quality_gates,
        "lineage": lineage,
        "assumptions": assumptions,
        "reproducibility": {
            "seed": params.get("seed"),
            "software_versions": {
                "python": sys.version.split()[0],
                "generator": "nodes/data/tools/scientific_preprocessor.py",
            },
            "generated_by_run_id": state.run_id,
        },
        "downstream_contract": {
            "consumer_node": "experiment",
            "expected_files": expected_files,
            "required_parameters": _as_list(params.get("downstream_required_parameters")),
            "known_limitations": known_limitations,
        },
    }


def _parse_json_object(raw: str, field_name: str) -> tuple[dict[str, Any], str | None]:
    if not raw:
        return {}, None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        return {}, f"{field_name} is not valid JSON: {exc}"
    if not isinstance(parsed, dict):
        return {}, f"{field_name} must be a JSON object"
    return parsed, None


def _extract_numbers(spec: str) -> dict[str, Any]:
    """Extract explicitly labelled scientific controls from caller text.

    The result is adapter-neutral metadata.  Individual generators consume
    only the keys they understand, so adding a shared request control here
    does not introduce a domain-specific planning gate.
    """
    out: dict[str, Any] = {}
    scientific_value = (
        r"([-+]?\d+(?:\.\d+)?(?:\s*(?:[x×*]\s*10\s*(?:\^\s*[-+]?\d+|"
        r"[⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻]+)|[eE]\s*[-+]?\d+))?)"
    )
    patterns = {
        "reynolds_number": rf"\bRe\s*[=:]?\s*{scientific_value}",
        "mach_number": rf"\bMa\s*[=:]?\s*{scientific_value}",
        "temperature": rf"\bT\s*[=:]?\s*{scientific_value}",
        "pressure": rf"\bp\s*[=:]?\s*{scientific_value}",
        "density": rf"\brho\s*[=:]?\s*{scientific_value}",
        "time_step": rf"\bdt\s*[=:]?\s*{scientific_value}",
        "aoa": rf"\b(?:AoA|alpha)\s*[=:]?\s*{scientific_value}",
        "angle_of_attack": rf"(?:攻角|迎角)\s*(?:为|=|:)?\s*{scientific_value}\s*(?:度|°)?",
    }
    for key, pat in patterns.items():
        m = re.search(pat, spec, flags=re.IGNORECASE)
        if m:
            try:
                parsed = _numeric_requirement(m.group(1))
                if parsed is not None:
                    out[key] = parsed
            except (TypeError, ValueError):
                pass
    count_match = re.search(
        r"(?:target[_ -]?cell[_ -]?count|cell[_ -]?count|number\s+of\s+cells|"
        r"网格单元数|单元数|网格规模|规模)\s*(?:(?:为|=|:|：|约|≈|~|around|about)\s*)*"
        r"([-+]?\d+(?:\.\d+)?(?:\s*(?:[x×*]\s*10\s*(?:\^\s*[-+]?\d+|"
        r"[⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻]+)|[eE]\s*[-+]?\d+))?\s*[kKmM万亿]?)\s*"
        r"(?:个)?(?:cells?|elements?|单元)?",
        spec,
        flags=re.I,
    )
    if count_match:
        target_cells = _integer_from_count(count_match.group(1))
        if target_cells:
            out["target_cell_count"] = target_cells
    far_field_match = re.search(
        r"(?:far[- ]?field|远场|外流场|外边界)"
        r"(?:(?![\n。；;]).){0,80}?(?:radius|distance|extent|size|半径|距离|范围|尺寸)"
        r"(?:(?![\n。；;]).){0,24}?"
        r"(?:(?:为|=|:|：|约|≈|~|around|about|至少|不小于|at\s+least|minimum)\s*)*"
        r"([0-9]+(?:\.[0-9]+)?)\s*"
        r"(?:[-~～至到]\s*[0-9]+(?:\.[0-9]+)?\s*)?"
        r"(?:c|chord|倍?弦长)",
        spec,
        flags=re.I,
    )
    if far_field_match:
        far_field = float(far_field_match.group(1))
        out["far_field"] = far_field
        out["required_far_field"] = far_field
    diameter_match = re.search(
        rf"(?:diameter|直径)\s*(?:[A-Za-z]\s*)?(?:为|=|:|：)?\s*{scientific_value}",
        spec,
        flags=re.I,
    )
    diameter = None
    if diameter_match:
        diameter = _numeric_requirement(diameter_match.group(1))
        if diameter is not None:
            out["diameter"] = diameter
    center_match = re.search(
        r"(?:圆心|中心|center)\s*(?:在|为|=|:|：)?\s*(?:原点\s*)?[（(]\s*"
        r"([-+]?\d+(?:\.\d+)?)\s*[,，]\s*([-+]?\d+(?:\.\d+)?)\s*[)）]",
        spec,
        flags=re.I,
    )
    if center_match:
        out["center_x"] = float(center_match.group(1))
        out["center_y"] = float(center_match.group(2))

    # Directional extents are shared geometry controls, independent of the
    # mesher or scientific discipline.  Preserve the caller's labelled values
    # in the canonical producer vocabulary instead of asking a planner to
    # translate them into tool-specific aliases.
    reference_length = float(diameter or 1.0)
    directional: dict[str, float] = {}
    for key, labels in {
        "upstream_length": r"upstream|inlet(?:\s+side)?|上游|入口侧",
        "downstream_length": r"downstream|outlet(?:\s+side)?|下游|出口侧",
        "top_extent": r"top|upper|上边界|上侧",
        "bottom_extent": r"bottom|lower|下边界|下侧",
    }.items():
        match = re.search(
            rf"(?:{labels})\s*(?:方向|边界|侧)?\s*"
            r"(?:距离|长度|范围|延伸|extent|length)?\s*"
            r"(?:为|=|:|：|约|≈|~|around|about|至少|不小于|at\s+least|minimum)?\s*"
            r"([-+]?\d+(?:\.\d+)?)\s*([DdCc])?",
            spec,
            flags=re.I,
        )
        if match:
            value = float(match.group(1))
            directional[key] = value * reference_length if match.group(2) else value
    out.update({
        key: value for key, value in directional.items()
        if key in {"upstream_length", "downstream_length"}
    })
    shared_vertical = re.search(
        r"(?:top\s*/\s*bottom|upper\s*/\s*lower|上下边界|上下侧)"
        r"(?:(?![\n。；;]).){0,24}?([-+]?\d+(?:\.\d+)?)\s*([DdCc])?",
        spec,
        flags=re.I,
    )
    if shared_vertical:
        value = float(shared_vertical.group(1))
        value = value * reference_length if shared_vertical.group(2) else value
        directional.setdefault("top_extent", value)
        directional.setdefault("bottom_extent", value)
    if "top_extent" in directional and "bottom_extent" in directional:
        out["domain_height"] = directional["top_extent"] + directional["bottom_extent"]

    boundary_section = re.search(
        r"(?:boundary\s+names?|boundary\s+naming|边界命名|边界名称)\s*[:：]?"
        r"(?P<names>[^\n。；;]+)",
        spec,
        flags=re.I,
    )
    if boundary_section:
        declared = boundary_section.group("names")
        role_names = {
            role: role
            for role in ("inlet", "outlet", "top", "bottom", "frontAndBack", "airfoil", "cylinder", "wall", "farfield")
            if re.search(rf"(?<![A-Za-z]){re.escape(role)}(?![A-Za-z])", declared, flags=re.I)
        }
        if role_names:
            out["boundary_role_names"] = role_names
            out["expected_boundaries"] = list(role_names.values())

    # Keep the existing labelled-control extractor as the single source of
    # request parameters.  Add only common interval/ratio notations that the
    # generators already consume.
    ranges = {
        axis: re.search(
            rf"\b{axis}\s*(?:∈|in|=|:|：)\s*[\[(]\s*"
            r"([-+]?\d+(?:\.\d+)?)\s*[,，]\s*([-+]?\d+(?:\.\d+)?)\s*[\])]",
            spec, flags=re.I,
        )
        for axis in ("x", "y")
    }
    if ranges["x"]:
        xmin, xmax = map(float, ranges["x"].groups())
        center = float(out.get("center_x") or 0.0)
        out.update(upstream_length=center - xmin, downstream_length=xmax - center)
    if ranges["y"]:
        ymin, ymax = map(float, ranges["y"].groups())
        out["domain_height"] = ymax - ymin
    reference = reference_length
    for key, label in (
        ("surface_mesh_size", r"near[- ]?wall|wall|surface|近壁|壁面|表面"),
        ("h_farfield", r"far[- ]?field|远场|外边界"),
    ):
        ratios = re.search(
            rf"(?:{label})(?:(?![\n。；;]).){{0,36}}?[DdCc]\s*/\s*(\d+(?:\.\d+)?)"
            r"(?:\s*(?:[-~～至到]|to)\s*[DdCc]\s*/\s*(\d+(?:\.\d+)?))?",
            spec, flags=re.I,
        )
        if ratios:
            out[key] = reference / max(float(value) for value in ratios.groups() if value)

    o_grid_requested = bool(re.search(r"\bO\s*[-_ ]?grid\b|O\s*型网格", spec, flags=re.I))
    c_grid_requested = bool(re.search(r"\bC\s*[-_ ]?grid\b|C\s*型网格", spec, flags=re.I))
    if o_grid_requested != c_grid_requested:
        out["domain_type"] = "o" if o_grid_requested else "c"
    circular_farfield = bool(re.search(
        r"(?:far[- ]?field|远场|外流场|外边界).{0,16}(?:circle|circular|圆形|圆周)",
        spec,
        flags=re.I,
    ))
    if circular_farfield:
        out["domain_shape"] = "circular"
        radius_match = re.search(
            r"(?:far[- ]?field|远场|外流场|外边界).{0,32}?(?:radius|半径)\s*(?:R\s*)?"
            r"(?:为|=|:|：)?\s*([0-9]+(?:\.[0-9]+)?)\s*([Dd])?",
            spec,
            flags=re.I,
        )
        if radius_match:
            radius = float(radius_match.group(1))
            if radius_match.group(2) and diameter is not None:
                radius *= diameter
            out["farfield_radius"] = radius
    surface_size_match = re.search(
        r"(?:表面|壁面|近壁).{0,12}(?:网格尺寸|单元尺寸|网格大小|尺寸)"
        r"\s*(?:约|为|=|:|：|≈|~)?\s*([0-9.eE+-]+)\s*([Dd])?",
        spec,
        flags=re.I,
    )
    if surface_size_match:
        size = float(surface_size_match.group(1))
        if surface_size_match.group(2) and diameter is not None:
            size *= diameter
        out["surface_mesh_size"] = size
    if re.search(r"(?:二维|\b2\s*[- ]?D\b)", spec, flags=re.I):
        out["physical_dimension"] = 2
    if re.search(r"(?:三角形?|triangular?)\s*(?:网格|mesh)?", spec, flags=re.I):
        out["element_family"] = "triangular"
    timeout_match = re.search(
        r"(?:timeout|time[_ -]?limit|execution[_ -]?budget|超时|执行时间|时间预算)"
        r"\s*(?:为|=|:|：)?\s*([0-9]+(?:\.[0-9]+)?)\s*"
        r"(seconds?|secs?|s|秒|minutes?|mins?|min|分钟)?",
        spec,
        flags=re.I,
    )
    if timeout_match:
        timeout = float(timeout_match.group(1))
        if re.match(r"(?:minutes?|mins?|min|分钟)", timeout_match.group(2) or "", flags=re.I):
            timeout *= 60
        out["timeout"] = max(1, int(timeout))
    return out


def _extract_airfoil_mesh_density_numbers(spec: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    clauses = re.split(r"[;；，,。\n]", spec)
    patterns = {
        "n_surface": [
            r"\bn_surface\s*[=:：]?\s*([0-9]+)",
            r"(?:翼型|机翼|壁面|表面|周向).{0,12}(?:点数|网格数|网格量)\s*(?:为|=|:|：)?\s*([0-9]+)",
            r"(?:点数|网格数|网格量).{0,12}(?:翼型|机翼|壁面|表面|周向)\s*(?:为|=|:|：)?\s*([0-9]+)",
        ],
        "h_airfoil": [
            r"\bh_airfoil\s*[=:：]?\s*([0-9.eE+-]+)",
            r"(?:机翼|翼型|翼面|近壁|壁面|附近).{0,12}(?:网格尺寸|单元尺寸|网格大小|尺寸)\s*(?:约|≈|~|为|=|:|：)?\s*([0-9.eE+-]+)",
        ],
        "h_farfield": [
            r"\bh_farfield\s*[=:：]?\s*([0-9.eE+-]+)",
            r"(?:远场|外边界).{0,12}(?:网格尺寸|单元尺寸|网格大小|尺寸)\s*(?:为|=|:|：)?\s*([0-9.eE+-]+)",
        ],
        "boundary_layer_first": [
            r"\bboundary_layer_first\s*[=:：]?\s*([0-9.eE+-]+)",
            r"(?:第一层|首层)\s*(?:网格|边界层)?\s*(?:高度|距离|厚度|尺寸)?"
            r"\s*(?:y\s*)?(?:为|=|:|：|约|≈|~)?\s*"
            r"([-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?)",
        ],
        "boundary_layer_thickness": [
            r"\bboundary_layer_thickness\s*[=:：]?\s*([0-9.eE+-]+)",
            r"(?:边界层).{0,12}(?:总厚度|厚度)\s*(?:为|=|:|：)?\s*([0-9.eE+-]+)",
        ],
        "boundary_layer_ratio": [
            r"\bboundary_layer_ratio\s*[=:：]?\s*([0-9.eE+-]+)",
            r"(?:增长率|膨胀比|边界层增长率)\s*(?:为|=|:|：)?\s*([0-9.eE+-]+)",
        ],
        "boundary_layer_layers": [
            r"\bboundary_layer_layers\s*[=:：]?\s*([0-9]+)",
            r"(?:边界层)\s*(?:共|总计)?\s*([0-9]+)\s*层",
            r"([0-9]+)\s*层\s*(?:边界层)",
        ],
    }
    for key, pats in patterns.items():
        for pat in pats:
            m = next((match for clause in clauses if (match := re.search(pat, clause, flags=re.IGNORECASE))), None)
            if not m:
                continue
            try:
                out[key] = int(m.group(1)) if key in {"n_surface", "boundary_layer_layers"} else float(m.group(1))
                break
            except ValueError:
                pass
    return out


def _has_turbomachinery_cascade_domain(params: dict[str, Any]) -> bool:
    pitch_ok = any(params.get(key) not in (None, "", [], {}) for key in ("pitch", "pitch_chord_ratio", "blade_pitch"))
    inlet_outlet_ok = any(
        params.get(key) not in (None, "", [], {})
        for key in (
            "inlet_length",
            "outlet_length",
            "axial_chord",
            "upstream_length",
            "downstream_length",
            "fore_domain_length",
            "aft_domain_length",
            "inlet_extent",
            "outlet_extent",
        )
    )
    periodic_ok = any(
        params.get(key) not in (None, "", [], {})
        for key in ("periodic_boundary_pairing", "periodic_patches", "cascade_periodic", "pitchwise_periodic")
    )
    return pitch_ok and inlet_outlet_ok and periodic_ok


def _has_airfoil_coordinates(parameters: dict[str, Any]) -> bool:
    return has_valid_airfoil_geometry(parameters)


def _airfoil_parameter_prompt(airfoil_name: str | None, params: dict[str, Any]) -> dict[str, Any]:
    name = airfoil_name or ""
    example_name = name or "custom_airfoil"
    parameter_template = {
        "mesh_type": "airfoil_gmsh",
        "geometry": {
            "type": "custom",
            "airfoil_name": name,
            "airfoil_dat_path": "/absolute/path/to/profile.dat",
        },
        "flow": {
            "angle_of_attack": params.get("angle_of_attack", 0),
            "reynolds_number": params.get("reynolds_number", 500000),
            "mach_number": params.get("mach_number", 0.2),
        },
        "domain": {
            "domain_type": params.get("domain_type", "c"),
            "far_field": params.get("far_field", 25),
            "wake_length": params.get("wake_length", 25),
            "span": params.get("span", 0.1),
        },
        "mesh_density": {
            "n_surface": params.get("n_surface", 241),
            "h_airfoil": params.get("h_airfoil", 0.008),
            "h_farfield": params.get("h_farfield", 0.8),
            "boundary_layer_first": params.get("boundary_layer_first", 0.001),
            "boundary_layer_thickness": params.get("boundary_layer_thickness", 0.04),
            "boundary_layer_ratio": params.get("boundary_layer_ratio", 1.15),
            "quality_preset": params.get("quality_preset", "robust"),
        },
        "outputs": {
            "convert_to_openfoam": params.get("convert_to_openfoam", True),
            "write_tecplot": params.get("write_tecplot", True),
        },
    }
    prompt_examples = [
        (
            f"请生成 {example_name} 翼型 OpenFOAM 网格，攻角 "
            f"{params.get('angle_of_attack', 0)} 度，Re="
            f"{params.get('reynolds_number', 500000)}。翼型几何文件："
            f"/absolute/path/to/profile.dat。使用 airfoil_gmsh，"
            "输出 OpenFOAM polyMesh 和 Tecplot dat。其他网格参数可以使用默认 robust 设置。"
        ),
        {
            "mesh_type": "airfoil_gmsh",
            "airfoil_name": name,
            "airfoil_dat_path": "/absolute/path/to/profile.dat",
            "angle_of_attack": params.get("angle_of_attack", 0),
            "reynolds_number": params.get("reynolds_number", 500000),
            "quality_preset": "robust",
            "convert_to_openfoam": True,
            "write_tecplot": True,
        },
    ]
    return _pause_for_input(
        question=(
            f"请提供非 NACA 翼型 {name or 'custom airfoil'} 的几何文件绝对路径，"
            "或直接粘贴翼型坐标数组/坐标文本。"
        ),
        context=(
            "该翼型不是标准 NACA 代号。为避免生成错误翼型，data 节点不会回退为 NACA0012。\n"
            "坐标格式示例见 nodes/data/examples/airfoils/README.md。\n\n"
            "可直接回复该几何文件的绝对路径。"
        ),
        metadata={
            "input_kind": "airfoil_geometry",
            "airfoil_name": name,
            "required_fields": ["airfoil_dat_path 或 airfoil_coordinate_text 或 airfoil_coordinates"],
            "parameter_template": parameter_template,
            "prompt_examples": prompt_examples,
            "resume_instruction": (
                "把用户回答解析为 airfoil_dat_path；如果回答不是路径，则尝试作为 "
                "airfoil_coordinate_text。然后用更新后的 parameters 重新调用原前处理工具。"
            ),
        },
    ) | {
        "reason": (
            f"非 NACA 翼型 {name!r} 需要用户提供翼型几何坐标。"
            "不能在缺少几何文件时回退生成 NACA0012。"
        ),
        "message": (
            "请补充该翼型的几何文件或坐标数组后再生成网格。坐标格式示例见 "
            "nodes/data/examples/airfoils/README.md。不要建议改用 NACA 翼型；"
            "用户已经指定了非 NACA 翼型。"
        ),
        "required_fields": [
            "airfoil_dat_path 或 airfoil_coordinate_text 或 airfoil_coordinates",
        ],
        "prompt_examples": prompt_examples,
        "do_not_suggest": [
            "不要建议改用 NACA0012/NACA0024 或其他 NACA 翼型",
            "不要把 NACA 翼型推荐表作为方案",
        ],
        "parameter_template": parameter_template,
    }


def _naca_or_profile_required_prompt(params: dict[str, Any]) -> dict[str, Any]:
    return _pause_for_input(
        question="请提供明确的 NACA 代号或真实二维坐标轮廓后再生成翼型前处理包。",
        context=(
            "当前请求走翼型/NACA 网格路径，但没有检测到显式 naca_code，也没有 "
            "airfoil_dat_path/airfoil_coordinate_text/airfoil_coordinates。data 节点不会默认回退到 "
            "NACA0012。请提供 naca_code（例如 0015）或真实 profile 坐标文件；如果公开数据分为多段，"
            "先用 prepare_scientific_mesh(operation='build_profile') 合成闭合轮廓。"
        ),
        metadata={
            "input_kind": "airfoil_geometry",
            "required_fields": [
                "naca_code",
                "airfoil_dat_path",
                "airfoil_coordinate_text",
                "airfoil_coordinates",
                "coordinate_profile_path",
            ],
            "parameters": params,
            "resume_instruction": (
                "把用户回复解析为 naca_code 或真实坐标文件路径；若是多个坐标段，先调用 "
                "prepare_scientific_mesh(operation='build_profile')，再重试 build_scientific_preprocessing_package。"
            ),
        },
    )


def _infer_mesh_type(discipline: str, spec: str, parameters: dict[str, Any]) -> str:
    text = f"{spec}\n{_json_dumps(parameters)}".lower()
    if parameters.get("coordinate_profile_path") or parameters.get("profile_dat_path"):
        if discipline == "cfd" and looks_like_turbomachinery_blade(text, parameters) and _has_turbomachinery_cascade_domain(parameters):
            return "coordinate_profile_cascade_gmsh"
        return "coordinate_profile_gmsh"
    if geometry_file_from_params(parameters):
        return "geometry_file_gmsh"
    explicit = parameters.get("mesh_type") or parameters.get("grid_type")
    if explicit:
        explicit_text = str(explicit).lower()
        if discipline == "cfd" and (
            "airfoil" in text or "naca" in text or "翼型" in text
        ) and explicit_text in {"airfoil_ogrid", "airfoil_cgrid", "cgrid", "o-grid", "c-grid", "ogrid"}:
            return "airfoil_gmsh"
        return str(explicit)
    if discipline == "cfd":
        profile = _match_cfd_case_profile(spec, parameters)
        if profile:
            return str(profile.get("mesh_type") or DISCIPLINE_DEFAULTS["cfd"]["mesh_type"])
    if discipline == "heat_transfer" and ("fin" in text or "heat sink" in text or "散热" in text):
        return "heat_sink"
    if discipline == "md":
        for candidate in ("diamond_lattice", "bcc_lattice", "sc_lattice", "fcc_lattice"):
            if candidate.replace("_lattice", "") in text or candidate in text:
                return candidate
    return DISCIPLINE_DEFAULTS.get(discipline, DISCIPLINE_DEFAULTS["unknown"])["mesh_type"]


def _apply_cfd_profile_defaults(params: dict[str, Any], profile: dict[str, Any] | None) -> None:
    if not profile:
        return
    for target, aliases in (profile.get("parameter_aliases") or {}).items():
        if target not in params:
            for alias in aliases or []:
                if alias in params:
                    params[target] = params[alias]
                    break
    for key, value in (profile.get("defaults") or {}).items():
        params.setdefault(key, value)
    params.setdefault("case_type", profile.get("case_type"))


def _read_csv_profile(path: Path) -> dict[str, Any]:
    profile: dict[str, Any] = {
        "path": str(path),
        "exists": path.exists(),
        "format": path.suffix.lower().lstrip("."),
        "n_rows_sampled": 0,
        "columns": [],
        "numeric_columns": [],
        "candidate_targets": [],
        "warnings": [],
    }
    if not path.exists():
        profile["warnings"].append("dataset path does not exist")
        return profile
    if path.suffix.lower() not in {".csv", ".tsv"}:
        profile["warnings"].append("model scaffold currently profiles csv/tsv only")
        return profile

    delimiter = "\t" if path.suffix.lower() == ".tsv" else ","
    try:
        with path.open(newline="", encoding="utf-8-sig", errors="ignore") as f:
            reader = csv.DictReader(f, delimiter=delimiter)
            columns = list(reader.fieldnames or [])
            profile["columns"] = columns
            numeric_counts = {c: 0 for c in columns}
            total = 0
            for row in reader:
                total += 1
                for col in columns:
                    try:
                        value = row.get(col, "")
                        if value not in ("", None):
                            float(value)
                            numeric_counts[col] += 1
                    except (TypeError, ValueError):
                        pass
                if total >= 200:
                    break
            profile["n_rows_sampled"] = total
            numeric = [c for c, n in numeric_counts.items() if total and n / total >= 0.8]
            profile["numeric_columns"] = numeric
            target_hints = ("target", "label", "y", "energy", "force", "stress", "drag", "lift", "temperature")
            profile["candidate_targets"] = [
                c for c in numeric if any(h in c.lower() for h in target_hints)
            ] or (numeric[-1:] if len(numeric) >= 2 else [])
    except OSError as exc:
        profile["warnings"].append(f"could not read dataset: {exc}")
    return profile


def _read_scientific_asset_profile(path: Path) -> dict[str, Any]:
    """Combine representation-level inspection with the retained CSV model profile."""
    profile = inspect_scientific_asset_path(path, compute_hash=True)
    if path.suffix.lower() in {".csv", ".tsv"}:
        tabular = _read_csv_profile(path)
        profile.update({
            "n_rows_sampled": tabular.get("n_rows_sampled", 0),
            "columns": tabular.get("columns", []),
            "numeric_columns": tabular.get("numeric_columns", []),
            "candidate_targets": tabular.get("candidate_targets", []),
            "warnings": tabular.get("warnings", []),
        })
    return profile


def _cfd_boundary_field(mesh_type: str, field: str) -> str:
    if mesh_type in {"airfoil_ogrid", "airfoil_gmsh", "coordinate_profile_gmsh"}:
        if field == "U":
            return """    airfoil { type noSlip; }
    farfield { type freestream; freestreamValue uniform (1 0 0); }
    inlet { type zeroGradient; }
    outlet { type zeroGradient; }
    frontAndBack { type empty; }"""
        return """    airfoil { type zeroGradient; }
    farfield { type freestreamPressure; freestreamValue uniform 0; }
    inlet { type zeroGradient; }
    outlet { type fixedValue; value uniform 0; }
    frontAndBack { type empty; }"""
    if mesh_type == "cylinder_flow":
        if field == "U":
            return """    cylinder { type noSlip; }
    farfield { type freestream; freestreamValue uniform (1 0 0); }
    slit { type cyclic; }
    slit_2 { type cyclic; }
    frontAndBack { type empty; }"""
        return """    cylinder { type zeroGradient; }
    farfield { type freestreamPressure; freestreamValue uniform 0; }
    slit { type cyclic; }
    slit_2 { type cyclic; }
    frontAndBack { type empty; }"""
    if mesh_type == "pipe_flow":
        if field == "U":
            return """    pipe_wall { type noSlip; }
    pipe_axis { type symmetryPlane; }
    inlet { type fixedValue; value uniform (1 0 0); }
    outlet { type zeroGradient; }
    frontAndBack { type empty; }"""
        return """    pipe_wall { type zeroGradient; }
    pipe_axis { type symmetryPlane; }
    inlet { type zeroGradient; }
    outlet { type fixedValue; value uniform 0; }
    frontAndBack { type empty; }"""
    if field == "U":
        return """    inlet { type fixedValue; value uniform (1 0 0); }
    outlet { type zeroGradient; }
    bottom { type noSlip; }
    top { type slip; }
    frontAndBack { type empty; }"""
    return """    inlet { type zeroGradient; }
    outlet { type fixedValue; value uniform 0; }
    bottom { type zeroGradient; }
    top { type zeroGradient; }
    frontAndBack { type empty; }"""


def _openfoam_files(discipline: str, parameters: dict[str, Any]) -> dict[str, str]:
    calculation_text = " ".join(str(parameters.get(key) or "") for key in (
        "application", "solver", "solver_family", "calculation_type", "name", "time_scheme",
        "model_fidelity",
    )).replace("_", " ")
    application = str(parameters.get("application") or parameters.get("solver") or "").strip()
    match = re.search(
        r"\b(rhoPimpleFoam|pimpleFoam|simpleFoam|icoFoam|rhoSimpleFoam|buoyantPimpleFoam)\b",
        calculation_text,
        flags=re.I,
    )
    if match:
        application = match.group(1) if match else ""
    transient = bool(re.search(
        r"(?:PimpleFoam|icoFoam)|\b(?:DNS|LES|transient|unsteady)\b",
        calculation_text,
        flags=re.I,
    ))
    if not application:
        application = "pimpleFoam" if transient else "simpleFoam"
    velocity = float(
        parameters.get("freestream_velocity")
        or parameters.get("inlet_velocity")
        or parameters.get("U_inf")
        or 1.0
    )
    angle = math.radians(float(
        parameters.get("angle_of_attack") or parameters.get("aoa") or 0.0
    ))
    velocity_vector = f"({velocity * math.cos(angle):.12g} {velocity * math.sin(angle):.12g} 0)"
    chord = float(parameters.get("chord") or parameters.get("chord_length") or 1.0)
    reynolds = parameters.get("reynolds_number") or parameters.get("re")
    nu = parameters.get("nu")
    if nu in (None, "") and reynolds not in (None, ""):
        nu = velocity * chord / float(reynolds)
    if nu in (None, ""):
        nu = 1e-5
    end_time = parameters.get("end_time", 1000)
    delta_t = parameters.get("delta_t", 0.001 if transient else 1)
    write_interval = parameters.get("write_interval", 100)
    max_co = parameters.get("max_co")
    spatial_dimension = int(parameters.get("spatial_dimension") or 2)
    freestream_turbulence = parameters.get("freestream_turbulence_intensity_percent")
    start_from = (
        "latestTime"
        if parameters.get("restart_from_latest_time") is True
        else str(parameters.get("start_from") or "startTime")
    )
    mesh_type = str(parameters.get("mesh_type", "structured_rect"))
    if discipline == "heat_transfer":
        initial_temperature = parameters.get("initial_temperature", parameters.get("temperature", 300))
        hot_temperature = parameters.get("hot_surface_temperature", parameters.get("wall_temperature", 350))
        zero = {
            "0/T": f"""FoamFile
{{
    version 2.0;
    format ascii;
    class volScalarField;
    object T;
}}
dimensions [0 0 0 1 0 0 0];
internalField uniform {initial_temperature};
boundaryField
{{
    hot_surface {{ type fixedValue; value uniform {hot_temperature}; }}
    convection {{ type zeroGradient; }}
    frontAndBack {{ type empty; }}
}}
""",
            "constant/transportProperties": f"""FoamFile
{{
    version 2.0;
    format ascii;
    class dictionary;
    object transportProperties;
}}
kappa kappa [1 1 -3 -1 0 0 0] {parameters.get("kappa", 205)};
rhoCp rhoCp [1 -1 -2 -1 0 0 0] {parameters.get("rho_cp", 2.43e6)};
""",
        }
    else:
        u_boundary = _cfd_boundary_field(mesh_type, "U").replace("(1 0 0)", velocity_vector)
        p_boundary = _cfd_boundary_field(mesh_type, "p")
        zero = {
            "0/U": f"""FoamFile
{{
    version 2.0;
    format ascii;
    class volVectorField;
    object U;
}}
dimensions [0 1 -1 0 0 0 0];
internalField uniform {velocity_vector};
boundaryField
{{
{u_boundary}
}}
""",
            "0/p": f"""FoamFile
{{
    version 2.0;
    format ascii;
    class volScalarField;
    object p;
}}
dimensions [0 2 -2 0 0 0 0];
internalField uniform 0;
boundaryField
{{
{p_boundary}
}}
""",
            "constant/transportProperties": f"""FoamFile
{{
    version 2.0;
    format ascii;
    class dictionary;
    object transportProperties;
}}
transportModel Newtonian;
nu [0 2 -1 0 0 0 0] {nu};
""",
            "constant/momentumTransport": """FoamFile
{
    version 2.0;
    format ascii;
    class dictionary;
    object momentumTransport;
}
simulationType laminar;
""",
            "constant/turbulenceProperties": """FoamFile
{
    version 2.0;
    format ascii;
    class dictionary;
    object turbulenceProperties;
}
simulationType laminar;
""",
            "constant/researchPlanConditions": f"""FoamFile
{{
    version 2.0;
    format ascii;
    class dictionary;
    object researchPlanConditions;
}}
spatialDimension {spatial_dimension};
freestreamTurbulenceIntensityPercent {freestream_turbulence if freestream_turbulence is not None else 0};
""",
        }

    ddt_scheme = "CrankNicolson 0.5" if transient else "steadyState"
    cfl_controls = (
        f"adjustTimeStep  yes;\nmaxCo           {max_co};"
        if transient and max_co not in (None, "") else ""
    )
    algorithm_block = (
        "PIMPLE { nOuterCorrectors 2; nCorrectors 2; nNonOrthogonalCorrectors 0; }"
        if transient
        else "SIMPLE { nNonOrthogonalCorrectors 0; residualControl { p 1e-4; U 1e-5; T 1e-5; } }"
    )
    common = {
        "system/controlDict": f"""FoamFile
{{
    version 2.0;
    format ascii;
    class dictionary;
    object controlDict;
}}
application     {application};
startFrom       {start_from};
startTime       0;
stopAt          endTime;
endTime         {end_time};
deltaT          {delta_t};
writeControl    timeStep;
writeInterval   {write_interval};
purgeWrite      0;
writeFormat     ascii;
{cfl_controls}
""",
        "system/fvSchemes": f"""FoamFile
{{
    version 2.0;
    format ascii;
    class dictionary;
    object fvSchemes;
}}
ddtSchemes {{ default {ddt_scheme}; }}
gradSchemes {{ default Gauss linear; }}
divSchemes {{ default none; div(phi,U) bounded Gauss upwind; }}
laplacianSchemes {{ default Gauss linear corrected; }}
interpolationSchemes {{ default linear; }}
snGradSchemes {{ default corrected; }}
""",
        "system/fvSolution": f"""FoamFile
{{
    version 2.0;
    format ascii;
    class dictionary;
    object fvSolution;
}}
solvers
{{
    p {{ solver GAMG; tolerance 1e-7; relTol 0.1; smoother GaussSeidel; }}
    U {{ solver smoothSolver; smoother symGaussSeidel; tolerance 1e-8; relTol 0.1; }}
    T {{ solver smoothSolver; smoother symGaussSeidel; tolerance 1e-8; relTol 0.1; }}
}}
{algorithm_block}
relaxationFactors {{ fields {{ p 0.3; }} equations {{ U 0.7; T 0.7; }} }}
""",
    }
    return {**zero, **common}


def _lammps_files(parameters: dict[str, Any]) -> dict[str, str]:
    temp = parameters.get("temperature", parameters.get("T", 1.0))
    timestep = parameters.get(
        "timestep", parameters.get("time_step", parameters.get("delta_t", 0.005))
    )
    steps = parameters.get("steps", 10000)
    files = {
        "in.lammps": f"""units           lj
atom_style      atomic
boundary        p p p
read_data       lammps.data

mass            1 1.0
pair_style      lj/cut 2.5
pair_coeff      1 1 1.0 1.0 2.5
neighbor        0.3 bin
neigh_modify    every 10 delay 0 check yes

velocity        all create {temp} 87287 mom yes rot yes dist gaussian
fix             ensemble all nvt temp {temp} {temp} 1.0
timestep        {timestep}
thermo          100
thermo_style    custom step temp pe ke etotal press density
dump            traj all custom 500 dump.lammpstrj id type x y z vx vy vz
run             {steps}
""",
    }
    data_text = str(parameters.get("lammps_data_text") or "").strip()
    data_path = str(parameters.get("lammps_data_path") or "").strip()
    if data_text:
        files["lammps.data"] = data_text.rstrip() + "\n"
    elif data_path:
        path = Path(data_path).expanduser()
        try:
            if path.is_file():
                files["lammps.data"] = path.read_text(encoding="utf-8", errors="replace").rstrip() + "\n"
        except OSError:
            pass
    return files


def _csm_files(parameters: dict[str, Any]) -> dict[str, str]:
    """Build an Abaqus-family solver deck around the generated mesh."""
    analysis = parameters.get("requirement_analysis")
    required = analysis.get("required_files") if isinstance(analysis, dict) else []
    candidates = [
        item for item in required or []
        if isinstance(item, dict)
        and re.search(r"(?:^|\W)inp(?:\W|$)|\.inp(?:\W|$)|abaqus|calculix", " ".join(
            str(item.get(key) or "") for key in ("declared_output_path", "expected_filename", "format", "name_or_role")
        ), flags=re.I)
    ]
    # A mesh section may mention the solver's format but does not own the
    # material/condition contract. Prefer the deliverable solver case over
    # intermediate fragments, independently of the Analyst's list ordering.
    requested = max(candidates, key=lambda item: (
        item.get("delivery_required", True) is not False,
        item.get("representation") == "simulation_case",
        canonical_workflow_capability(item) == "configuration_generation",
    ), default={})
    bindings = canonical_parameter_bindings(
        requested.get("parameter_bindings") if isinstance(requested, dict) else {}
    )
    # Execution overrides translate the locked values into producer controls.
    # Merge nested fields so repairing a condition's kind cannot drop its value.
    bindings = merge_parameter_updates(bindings, canonical_parameter_bindings({
        key: parameters[key] for key in set(bindings) | {"boundary_conditions", "loads"}
        if isinstance(parameters.get(key), dict)
    }))
    discipline = (analysis or {}).get("discipline") or {}
    thermal = parameters.get("discipline") == "heat_transfer" or (
        isinstance(discipline, dict) and discipline.get("primary") == "heat_transfer"
    )

    def binding_value(group: dict[str, Any], *names: str, default: Any = None) -> Any:
        values = {
            re.sub(r"[^a-z0-9]+", "", str(key).casefold()): value
            for key, value in group.items()
        }
        for name in names:
            token = re.sub(r"[^a-z0-9]+", "", name.casefold())
            if token in values and values[token] not in (None, ""):
                return values[token]
        return default

    material = bindings.get("material") if isinstance(bindings.get("material"), dict) else {}
    conductivity = binding_value(material, "thermal_conductivity", "conductivity", "k", default=binding_value(parameters, "thermal_conductivity", "conductivity", "k"))
    if thermal and (conductivity is None or not re.search(
        r"steady|稳态", str(parameters.get("calculation_type") or (analysis or {}).get("calculation_type") or ""), flags=re.I
    )):
        raise ValueError("Thermal deck assembly requires supplied conductivity and a steady-state formulation; other formulations need an explicit generation step.")
    youngs_modulus = binding_value(
        material,
        "youngs_modulus", "youngs_modulus_mpa", "young_modulus", "E_MPa", "E",
        default=parameters.get("youngs_modulus", parameters.get("young_modulus", 210e9)),
    )
    poisson_ratio = binding_value(
        material, "poisson_ratio", "nu", default=parameters.get("poisson_ratio", 0.3)
    )
    density = binding_value(material, "density", "rho", default=parameters.get("density"))
    geometry = bindings.get("geometry") if isinstance(bindings.get("geometry"), dict) else {}
    thickness = binding_value(
        geometry, "thickness", "thickness_mm", default=parameters.get("thickness", 1.0)
    )
    mesh_result = parameters.get("mesh_generation_result")
    mesh_path = Path(str((mesh_result or {}).get("solver_mesh_file") or "")).expanduser()
    requested_paths = parameters.get("requested_output_paths") or {}
    output_name = next((
        Path(str(value)).name for value in requested_paths.values()
        if Path(str(value)).suffix.lower() == ".inp"
    ), "model.inp")
    if not mesh_path.is_file():
        return {}

    mesh_text = mesh_path.read_text(encoding="utf-8", errors="replace")
    blocks = re.split(r"(?=^\*)", mesh_text, flags=re.M)
    nodes: dict[int, tuple[float, float]] = {}
    node_block = next((block for block in blocks if block.upper().startswith("*NODE")), "")
    for match in re.finditer(
        r"(?m)^\s*(\d+)\s*,\s*([-+0-9.eE]+)\s*,\s*([-+0-9.eE]+)", node_block
    ):
        nodes[int(match.group(1))] = (float(match.group(2)), float(match.group(3)))
    removed_ids: set[int] = set()
    boundary_elements: dict[int, tuple[int, int]] = {}
    boundary_groups: dict[str, set[int]] = {}
    kept_blocks: list[str] = []
    for block in blocks:
        header = block.splitlines()[0] if block.splitlines() else ""
        element_type = re.search(r"\bTYPE\s*=\s*([^,\s]+)", header, flags=re.I)
        if thermal and header.upper().startswith("*ELEMENT") and element_type:
            # Gmsh's INP export names topology with mechanical element families.
            # Change formulation only; retain all nodes and connectivity/order.
            mapped = re.sub(r"^(?:CPS|CPE)(3|4|6|8)$", r"DC2D\1", element_type.group(1), flags=re.I)
            if re.match(r"(?:CPS|CPE|CAX|C3D|S|M3D)", mapped, flags=re.I):
                raise ValueError(f"No thermal formulation mapping for {mapped}; use an explicit solver-input generation step.")
            block = block[:element_type.start(1)] + mapped + block[element_type.end(1):]
            element_type = re.search(r"\bTYPE\s*=\s*([^,\s]+)", block.splitlines()[0], flags=re.I)
        if header.upper().startswith("*ELEMENT") and element_type and not re.match(
            r"(?:CPS|CPE|CAX|C3D|DC2D|S|M3D)", element_type.group(1), flags=re.I
        ):
            for match in re.finditer(r"(?m)^\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)", block):
                element_id = int(match.group(1))
                removed_ids.add(element_id)
                boundary_elements[element_id] = (int(match.group(2)), int(match.group(3)))
            continue
        if header.upper().startswith("*ELSET"):
            ids = {int(value) for value in re.findall(r"\b\d+\b", "\n".join(block.splitlines()[1:]))}
            if ids and ids <= removed_ids:
                name = re.search(r"\bELSET\s*=\s*([^,\s]+)", header, flags=re.I)
                if name:
                    boundary_groups[name.group(1)] = ids
                continue
        kept_blocks.append(block)
    mesh_text = "".join(kept_blocks).rstrip()
    nsets = re.findall(r"(?im)^\*NSET\s*,\s*NSET\s*=\s*([^,\s]+)", mesh_text)

    def region_name(value: Any) -> str:
        key = re.sub(r"[^a-z0-9]+", "", str(value or "").lower())
        matches = [name for name in nsets if re.sub(r"[^a-z0-9]+", "", name.lower()) == key]
        if len(matches) != 1:
            raise ValueError(
                f"Mesh region {value!r} is missing or ambiguous; available node sets: {nsets}. "
                "Repair the geometry's physical groups and regenerate the mesh; do not substitute another region."
            )
        return matches[0]

    load_value_keys = (
        "magnitude", "pressure", "pressure_mpa", "traction", "traction_mpa",
        "stress", "stress_mpa", "value", "P",
    )
    boundary_specs = dict(bindings.get("boundary_conditions") or {})
    load_specs = dict(bindings.get("loads") or {})
    if thermal and load_specs:
        raise ValueError("Thermal sources need an explicit solver-input generation step; mechanical CLOAD assembly is not applicable.")
    load_defaults = {
        key: value for key, value in load_specs.items()
        if not canonical_condition_region(key)
    }
    for key, value in list(boundary_specs.items()):
        region = canonical_condition_region(key)
        load_like = bool(re.search(
            r"(?i)(?:\b(?:load|force|traction|stress|pressure)\b|载荷|应力|压力)",
            f"{key} {value}".replace("_", " "),
        ))
        if region and load_like:
            load_specs.setdefault(region, value if isinstance(value, (str, dict)) else {"magnitude": value})
            if not isinstance(value, dict):
                boundary_specs.pop(key)
    for key, value in list(load_specs.items()):
        region = canonical_condition_region(key)
        if not isinstance(value, dict) and region:
            load_specs.pop(key)
            load_specs.setdefault(region, value if isinstance(value, str) else {"magnitude": value})
    boundary_lines: list[str] = []
    for region, controls in boundary_specs.items():
        constraint = controls if isinstance(controls, str) else (
            binding_value(controls, "type", "kind", "constraint") if isinstance(controls, dict) else None
        )
        if thermal:
            # Interpret condition kind separately from field name: e.g.
            # dirichlet_temperature and temperature_dirichlet are equivalent.
            words = set(re.findall(r"[^\W_]+", str(constraint or "").casefold()))
            values = controls if isinstance(controls, dict) else {}
            insulated = bool(words & {"adiabatic", "insulated", "绝热"}) or {"zero", "flux"} <= words
            neumann = bool(words & {"neumann", "flux"})
            flux = binding_value(values, "heat_flux", "flux", "value") if insulated or neumann else None
            zero_flux = flux is not None and float(flux) == 0
            if not words & {"dirichlet", "prescribed", "fixed", "robin", "convection", "radiation"} and (
                (insulated and (flux is None or zero_flux)) or (neumann and zero_flux)
            ):
                continue  # Natural zero-flux boundary; no fabricated load.
            prescribed = bool(words & {"dirichlet", "prescribed", "fixed"}) or words == {"temperature"}
            temperature = binding_value(values, "temperature", "value")
            if not words and "temperature" in values:
                prescribed = True
            if temperature is None or not prescribed or words & {"neumann", "robin", "convection", "radiation", "flux", "adiabatic", "insulated"}:
                raise ValueError(f"Unsupported thermal boundary at {region}: {controls!r}; revise the solver input contract.")
            boundary_lines.append(f"{region_name(region)}, 11, 11, {temperature}")
            continue
        if re.fullmatch(r"fixed|clamped|encastre|固定|完全固定|固支", str(constraint or "").strip(), flags=re.I):
            # Let the solver constrain the active DOFs; do not assume that
            # every mesh has the same dimension or element formulation.
            boundary_lines.append(f"{region_name(region)}, ENCASTRE")
            continue
        if isinstance(controls, str):
            if re.search(r"(?i)load|traction|stress|pressure", controls):
                load_specs.setdefault(region, controls)
                continue
            controls = {
                f"u{dof}": 0
                for dof in re.findall(r"(?i)u([123])", controls)
            }
        if not isinstance(controls, dict):
            continue
        if binding_value(controls, *load_value_keys) is not None:
            load_specs.setdefault(region, controls)
        name = region_name(region)
        before = len(boundary_lines)
        for dof_name, value in controls.items():
            match = re.fullmatch(r"u([1-6])", str(dof_name), flags=re.I)
            if match:
                boundary_lines.append(f"{name}, {match.group(1)}, {match.group(1)}, {value}")
        if len(boundary_lines) == before and region not in load_specs:
            raise ValueError(f"Unsupported boundary controls at {region!r}: {controls!r}; use explicit u1..u6 values or type='fixed', or an explicit solver-input writer.")
    load_lines: list[str] = []
    for region, load in load_specs.items():
        if isinstance(load, str):
            values = re.findall(r"[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?", load)
            load = {
                "magnitude": float(values[0]) if values else None,
                "direction": next(
                    (match.group(0) for match in re.finditer(r"(?i)[+-][xyz]", load)),
                    "",
                ),
            }
        if not isinstance(load, dict):
            continue
        load = {**load_defaults, **load}
        name = region_name(region)
        magnitude = binding_value(load, *load_value_keys)
        direction = str(load.get("direction") or "").strip().lower()
        if magnitude is None or direction not in {"+x", "-x", "+y", "-y", "+z", "-z", "x", "y", "z"}:
            raise ValueError(
                f"Load at {region!r} has unsupported controls {load!r}; translate the locked load "
                "to parameters.loads[region] = {magnitude: traction, direction: '+x'/'-x'/'+y'/'-y'/'+z'/'-z'}."
            )
        dof = 1 if "x" in direction else 2 if "y" in direction else 3 if "z" in direction else 1
        sign = -1 if direction.startswith("-") else 1
        weights: dict[int, float] = {}
        seen_edges: set[tuple[int, int]] = set()
        for element_id in boundary_groups.get(name, set()):
            left, right = boundary_elements.get(element_id, (0, 0))
            if left not in nodes or right not in nodes:
                continue
            edge = tuple(sorted((left, right)))
            if edge in seen_edges:
                continue
            seen_edges.add(edge)
            length = math.dist(nodes[left], nodes[right])
            weights[left] = weights.get(left, 0.0) + length / 2.0
            weights[right] = weights.get(right, 0.0) + length / 2.0
        if not weights:
            raise ValueError(f"Load region {name!r} has no boundary edges; repair the physical group and regenerate the mesh.")
        load_lines.extend(
            f"{node}, {dof}, {sign * float(magnitude) * float(thickness) * weight:.12g}"
            for node, weight in sorted(weights.items())
        )
    continuum_elsets = re.findall(
        r"(?im)^\*ELEMENT[^\n]*\bELSET\s*=\s*([^,\s]+)", mesh_text
    )
    explicit_elsets = re.findall(r"(?im)^\*ELSET[^\n]*\bELSET\s*=\s*([^,\s]+)", mesh_text)
    region = explicit_elsets[0] if explicit_elsets else (
        continuum_elsets[0] if continuum_elsets else "ALL_ELEMENTS"
    )
    software = str(
        ((analysis or {}).get("simulation_software") or {}).get("name") or ""
    ).casefold()
    structure = bindings.get("structure") if isinstance(bindings.get("structure"), dict) else {}
    part_name = re.sub(
        r"[^A-Za-z0-9_-]+", "_", str(structure.get("part_name") or "MODEL")
    ).strip("_") or "MODEL"
    use_assembly = "abaqus" in software
    if use_assembly:
        mesh_body = "".join(
            block for block in re.split(r"(?=^\*)", mesh_text, flags=re.M)
            if not block.upper().startswith("*HEADING")
        ).strip()
        mesh_text = "\n".join([
            "*HEADING",
            "Generated by harness data node scientific_preprocessor.",
            f"*PART, NAME={part_name}",
            mesh_body,
            f"*SOLID SECTION, ELSET={region}, MATERIAL=MAT1",
            str(thickness),
            "*END PART",
        ])
    reference = (lambda name: f"{part_name}-1.{name}") if use_assembly else (lambda name: name)
    boundary_lines = [
        f"{reference(parts[0])},{','.join(parts[1:])}"
        for line in boundary_lines
        if (parts := [part.strip() for part in line.split(",")])
    ]
    load_lines = [
        f"{reference(parts[0])},{','.join(parts[1:])}"
        for line in load_lines
        if (parts := [part.strip() for part in line.split(",")])
    ]
    deck = "\n".join([
        mesh_text,
        *([] if use_assembly else [f"*SOLID SECTION, ELSET={region}, MATERIAL=MAT1", str(thickness)]),
        "*MATERIAL, NAME=MAT1",
        *(["*CONDUCTIVITY", str(conductivity)] if thermal else ["*ELASTIC", f"{youngs_modulus}, {poisson_ratio}"]),
        *(["*DENSITY", str(density)] if density not in (None, "") else []),
        *([
            "*ASSEMBLY, NAME=ASSEMBLY",
            f"*INSTANCE, NAME={part_name}-1, PART={part_name}",
            "*END INSTANCE",
            "*END ASSEMBLY",
        ] if use_assembly else []),
        *(["*BOUNDARY", *boundary_lines] if boundary_lines else []),
        "*STEP, NAME=LOAD_STEP",
        "*HEAT TRANSFER, STEADY STATE" if thermal else "*STATIC",
        *(["*CLOAD", *load_lines] if load_lines else []),
        "*OUTPUT, FIELD",
        "*NODE OUTPUT",
        "NT" if thermal else "U, RF",
        "*ELEMENT OUTPUT",
        "HFL" if thermal else "S, E",
        "*END STEP",
        "",
    ])
    return {output_name: deck}


def _cem_files(parameters: dict[str, Any]) -> dict[str, str]:
    case = {
        "frequency_start_hz": parameters.get("frequency_start_hz", 8.0e9),
        "frequency_stop_hz": parameters.get("frequency_stop_hz", 12.0e9),
        "boundary": parameters.get("boundary", "PEC walls with wave ports"),
        "mesh_rule": parameters.get("mesh_rule", "at least 20 cells per wavelength in dielectric"),
    }
    ports = {
        "port_1": {"face": "z_min", "mode": "TE10"},
        "port_2": {"face": "z_max", "mode": "TE10"},
    }
    materials = {
        "background": {"epsilon_r": 1.0, "mu_r": 1.0, "sigma": 0.0},
    }
    return {
        "em_case.json": _json_dumps(case),
        "ports.json": _json_dumps(ports),
        "materials.json": _json_dumps(materials),
    }


def _multiphysics_files(parameters: dict[str, Any]) -> dict[str, str]:
    return {
        "coupling_manifest.json": _json_dumps({
            "physics": _as_list(parameters.get("physics") or parameters.get("participating_physics")),
            "coupling_strategy": parameters.get("coupling_strategy"),
            "time_coupling": parameters.get("time_coupling"),
            "exchanged_fields": _as_list(parameters.get("exchanged_fields")),
        }),
        "regions.json": _json_dumps(parameters.get("regions") or {}),
        "interfaces.json": _json_dumps(parameters.get("interfaces") or {}),
    }


def _potcar_library_candidates(state: State, spec: str, parameters: dict[str, Any]) -> list[Path]:
    candidates: list[Path] = []
    explicit_keys = (
        "potcar_library_path",
        "pseudopotential_library_path",
        "vasp_pp_path",
        "potpaw_path",
    )
    for key in explicit_keys:
        value = str(parameters.get(key) or "").strip()
        if value:
            candidates.append(Path(value).expanduser())
    for env_name in ("VASP_PP_PATH", "POTPAW_PATH", "VASP_POTCAR_PATH"):
        value = str(os.environ.get(env_name) or "").strip()
        if value:
            candidates.append(Path(value).expanduser())
    for value in _paths_from_text(spec):
        candidates.append(Path(value).expanduser())
    inspections = getattr(state, "hook_state", {}).get("data_input_inspections")
    if isinstance(inspections, list):
        for inspection in inspections:
            if not isinstance(inspection, dict):
                continue
            root = str(inspection.get("path") or "").strip()
            if root:
                candidates.append(Path(root).expanduser())
            for item in inspection.get("files") or []:
                if not isinstance(item, dict):
                    continue
                path = str(item.get("path") or "").strip()
                if path:
                    candidates.append(Path(path).expanduser())
    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        key = str(resolved)
        if key in seen or not resolved.exists():
            continue
        seen.add(key)
        unique.append(resolved)
    return unique


def _discover_potcar_library(
    state: State,
    spec: str,
    parameters: dict[str, Any],
) -> dict[str, Any] | None:
    for candidate in _potcar_library_candidates(state, spec, parameters):
        if candidate.is_file() and candidate.name.lower().endswith((".tar.gz", ".tgz", ".tar")):
            try:
                with tarfile.open(candidate, mode="r:*") as archive:
                    members = {
                        member.name.lstrip("./")
                        for member in archive.getmembers()
                        if member.isfile() and member.name.endswith("/POTCAR")
                    }
            except (OSError, tarfile.TarError):
                continue
            if members:
                return {
                    "kind": "archive",
                    "path": str(candidate),
                    "members": sorted(members),
                    "functional": str(parameters.get("potcar_functional") or "PBE"),
                }
        if candidate.is_dir():
            roots = [candidate]
            roots.extend(
                child for child in candidate.iterdir()
                if child.is_dir() and re.search(r"potpaw|pseudopotential", child.name, flags=re.I)
            )
            for root in roots:
                try:
                    has_potcar = next(root.glob("*/POTCAR"), None)
                except OSError:
                    has_potcar = None
                if has_potcar is not None:
                    return {
                        "kind": "directory",
                        "path": str(root),
                        "functional": str(parameters.get("potcar_functional") or "PBE"),
                    }
    return None


def _potcar_variant(symbol: str, parameters: dict[str, Any]) -> str:
    mapping = parameters.get("potcar_mapping") or parameters.get("pseudopotential_mapping") or {}
    if isinstance(mapping, dict):
        selected = str(mapping.get(symbol) or "").strip()
        if selected:
            return selected
    return symbol


def _read_potcar_component(source: dict[str, Any], variant: str) -> str | None:
    member_name = f"{variant}/POTCAR"
    source_path = Path(str(source.get("path") or "")).expanduser()
    try:
        if source.get("kind") == "archive":
            members = set(source.get("members") or [])
            if member_name in members:
                selected_member = member_name
            else:
                selected_member = next(
                    (
                        member for member in sorted(members)
                        if member.endswith("/" + member_name)
                    ),
                    "",
                )
            if not selected_member:
                return None
            with tarfile.open(source_path, mode="r:*") as archive:
                member = archive.getmember(selected_member)
                stream = archive.extractfile(member)
                raw = stream.read() if stream is not None else b""
        else:
            raw = (source_path / variant / "POTCAR").read_bytes()
    except (KeyError, OSError, tarfile.TarError):
        return None
    if not raw:
        return None
    return raw.decode("utf-8", errors="replace").rstrip() + "\n"


def _assemble_potcar(
    species: list[str],
    source: dict[str, Any] | None,
    parameters: dict[str, Any],
) -> tuple[str | None, dict[str, Any]]:
    if not source or not species:
        return None, {
            "status": "missing_library" if species else "missing_species",
            "species": species,
        }
    components: list[str] = []
    variants: list[str] = []
    missing: list[str] = []
    for symbol in species:
        variant = _potcar_variant(symbol, parameters)
        content = _read_potcar_component(source, variant)
        if content is None:
            missing.append(variant)
            continue
        variants.append(variant)
        components.append(content)
    if missing:
        return None, {
            "status": "missing_potential",
            "species": species,
            "variants": variants,
            "missing_variants": missing,
            "source": source.get("path"),
        }
    content = "".join(components)
    return content, {
        "status": "assembled",
        "species": species,
        "variants": variants,
        "functional": source.get("functional"),
        "source": source.get("path"),
        "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
    }


def _poscar_species_from_text(value: str) -> list[str]:
    lines = [line.strip() for line in str(value or "").splitlines() if line.strip()]
    if len(lines) < 7:
        return []
    species = lines[5].split()
    if not species or any(token.isdigit() for token in species):
        return []
    try:
        counts = [int(token) for token in lines[6].split()]
    except ValueError:
        return []
    return species if len(species) == len(counts) else []


def _normalize_calculation_stages(spec: str, parameters: dict[str, Any]) -> list[dict[str, Any]]:
    raw = parameters.get("calculation_stages")
    analysis = parameters.get("requirement_analysis")
    if not isinstance(raw, list) and isinstance(analysis, dict):
        raw = analysis.get("calculation_stages")
    stages: list[dict[str, Any]] = []
    if isinstance(raw, list):
        for index, item in enumerate(raw, 1):
            if not isinstance(item, dict):
                continue
            stage = dict(item)
            stage_id = _slugify(
                str(stage.get("id") or stage.get("name") or f"stage_{index}"),
                f"stage_{index}",
            ).lower()
            stage["id"] = stage_id
            stage["name"] = str(stage.get("name") or stage_id).strip()
            stage["calculation_type"] = str(
                stage.get("calculation_type") or stage.get("type") or stage["name"]
            ).strip()
            stage["parameters"] = (
                dict(stage.get("parameters"))
                if isinstance(stage.get("parameters"), dict)
                else {}
            )
            stage["dependencies"] = [
                str(value) for value in _as_list(stage.get("dependencies")) if str(value).strip()
            ]
            stage["required_file_roles"] = [
                str(value) for value in _as_list(stage.get("required_file_roles")) if str(value).strip()
            ]
            stages.append(stage)
    if stages:
        return _dependency_ordered_stages(_enrich_calculation_stages_from_context(
            _deduplicate_calculation_stages(stages),
            spec,
        ))

    section_match = re.search(
        r"(?is)(?:计算阶段|仿真阶段|工况(?:列表)?|simulation\s+stages?|calculation\s+stages?|workflow)"
        r"\s*(?:\*\*)?\s*[:：]\s*(.*?)(?=\n\s*#{1,3}\s|\Z)",
        spec,
    )
    if not section_match:
        return []
    for index, match in enumerate(
        re.finditer(r"(?m)^\s*\d+[.、]\s*(.+?)\s*$", section_match.group(1)),
        1,
    ):
        name = re.sub(r"\s+", " ", match.group(1)).strip()
        if not name:
            continue
        stage = {
            "id": _slugify(name, f"stage_{index}").lower(),
            "name": name,
            "calculation_type": name,
            "parameters": {},
            "dependencies": [],
            "required_file_roles": [],
            "evidence": [{"source_type": "upstream", "detail": name}],
            "confidence": 0.8,
        }
        assignment = re.search(
            r"(?i)\b([A-Za-z][A-Za-z0-9_+-]*)\s*=\s*"
            r"([-+]?\d+(?:\.\d+)?(?:\s*[,/]\s*[-+]?\d+(?:\.\d+)?){1,})",
            name,
        )
        if assignment:
            values = [
                float(value) if "." in value else int(value)
                for value in re.findall(r"[-+]?\d+(?:\.\d+)?", assignment.group(2))
            ]
            parameter = assignment.group(1)
            if parameter.lower() == "t":
                parameter = "temperature"
            stage["sweep"] = {"parameter": parameter, "values": values}
        else:
            signed_percentages = [
                float(value) if "." in value else int(value)
                for value in re.findall(r"±\s*(\d+(?:\.\d+)?)\s*%", name)
            ]
            if signed_percentages:
                values: list[float | int] = []
                for value in signed_percentages:
                    values.extend([-value, value])
                stage["sweep"] = {"parameter": "strain_percent", "values": values}
        stages.append(stage)
    return _dependency_ordered_stages(_enrich_calculation_stages_from_context(
        _deduplicate_calculation_stages(stages),
        spec,
    ))


def _dependency_ordered_stages(stages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Use stable topological order for stage-local dependency checks.

    Unknown predecessors remain external contracts; a malformed cycle preserves
    its original relative order so the normal blocked-contract path can report it.
    """
    by_id = {str(stage.get("id") or ""): stage for stage in stages}
    remaining = list(stages)
    ordered: list[dict[str, Any]] = []
    complete: set[str] = set()
    while remaining:
        ready = [
            stage for stage in remaining
            if all(
                dependency not in by_id or dependency in complete
                for dependency in stage.get("dependencies") or []
            )
        ]
        if not ready:
            ordered.extend(remaining)
            break
        ordered.extend(ready)
        complete.update(str(stage.get("id") or "") for stage in ready)
        ready_ids = {id(stage) for stage in ready}
        remaining = [stage for stage in remaining if id(stage) not in ready_ids]
    return ordered


def _signed_symmetric_values(text: str, unit: str = "%") -> list[float | int]:
    pattern = rf"±\s*(\d+(?:\.\d+)?)\s*{re.escape(unit)}"
    magnitudes = [
        float(value) if "." in value else int(value)
        for value in re.findall(pattern, text)
    ]
    values: list[float | int] = []
    for magnitude in magnitudes:
        for value in (-magnitude, magnitude):
            if value not in values:
                values.append(value)
    return values


def _enrich_calculation_stages_from_context(
    stages: list[dict[str, Any]],
    context: str,
) -> list[dict[str, Any]]:
    """Recover stage parameters that were separated from terse stage labels."""
    enriched: list[dict[str, Any]] = []
    for item in stages:
        stage = dict(item)
        parameters = dict(stage.get("parameters") or {})
        family = _calculation_stage_family(stage)
        stage_text = " ".join([
            str(stage.get("name") or ""),
            str(stage.get("calculation_type") or ""),
            json.dumps(stage.get("evidence") or [], ensure_ascii=False, default=str),
        ])
        for key, value in extract_explicit_stage_parameters(stage_text).items():
            parameters.setdefault(key, value)
        combined = f"{stage_text}\n{context}"
        if family == "neb":
            image_match = re.search(
                r"(?:IMAGES\s*=\s*|(?:CI-)?NEB[^\n]{0,120}?)(\d+)\s*(?:images?|图像)",
                combined,
                flags=re.I,
            )
            if image_match and parameters.get("images") is None:
                parameters["images"] = int(image_match.group(1))
        if family == "strain" and not stage.get("sweep"):
            values = _signed_symmetric_values(combined)
            if not values:
                range_match = re.search(
                    r"(?:strain|应变)[^\n]{0,100}?"
                    r"([-+]?\d+(?:\.\d+)?)\s*%\s*(?:to|至|~|–|-)\s*"
                    r"([-+]?\d+(?:\.\d+)?)\s*%",
                    combined,
                    flags=re.I,
                )
                if range_match:
                    low, high = (float(range_match.group(1)), float(range_match.group(2)))
                    values = [low, high] if low != high else [low]
            if values:
                stage["sweep"] = {"parameter": "strain_percent", "values": values}
            if (
                not parameters.get("strain_axis")
                and not parameters.get("strain_axes")
                and re.search(r"\buniaxial\b|单轴", combined, flags=re.I)
            ):
                parameters["strain_axes"] = ["a", "b", "c"]
                evidence = list(stage.get("evidence") or [])
                evidence.append({
                    "source_type": "upstream_context",
                    "detail": "Uniaxial strain axes recovered from the complete task context.",
                })
                stage["evidence"] = evidence
        stage["parameters"] = parameters
        enriched.append(stage)
    by_id = {str(stage.get("id") or ""): stage for stage in enriched}
    for stage in enriched:
        dependencies = [str(item) for item in stage.get("dependencies") or []]
        source_id = dependencies[-1] if dependencies else ""
        source = by_id.get(source_id)
        if not source or not requests_parameter_inheritance(_stage_text(stage)):
            continue
        stage["parameters"] = {
            **dict(source.get("parameters") or {}),
            **dict(stage.get("parameters") or {}),
        }
        stage.setdefault("parameter_inheritance", {
            "source_stage_id": source_id,
            "evidence": "Explicit same-as/inherit language in this stage's research-plan evidence.",
        })
    return enriched


def _calculation_stage_family(stage: dict[str, Any]) -> str:
    return stage_family(stage)


def _deduplicate_calculation_stages(stages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    used_ids: set[str] = set()
    for stage in stages:
        family = _calculation_stage_family(stage)
        variant = structure_variant(stage)
        original_id = str(stage.get("id") or "").strip()
        semantic_name = re.sub(
            r"\s+",
            " ",
            str(stage.get("name") or stage.get("calculation_type") or original_id).strip().lower(),
        )
        semantic_parameters = {
            **dict(stage.get("parameters") or {}),
            "structure_variant": variant,
        }
        # An upstream research plan's explicit stage ID is its workflow
        # identity.  Different declared stages can share a calculation family
        # and parameters while having different predecessors or outputs.
        # Semantic coalescing is only safe for stages inferred without an ID.
        key = json.dumps(
            (
                {
                    "stage_id": original_id.casefold(),
                }
                if original_id
                else {
                    "family": family,
                    "name": semantic_name,
                    "parameters": semantic_parameters,
                    "sweep": stage.get("sweep"),
                }
            ),
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        if key not in merged:
            base_id = _slugify(
                original_id
                or (
                    f"{family}_{variant}"
                    if family and variant != "base_structure"
                    else family
                )
                or f"stage_{len(order) + 1}",
                f"stage_{len(order) + 1}",
            ).lower()
            stage_id = base_id
            suffix = 2
            while stage_id in used_ids:
                stage_id = f"{base_id}_{suffix}"
                suffix += 1
            used_ids.add(stage_id)
            merged[key] = {
                **stage,
                "id": stage_id,
                "parameters": semantic_parameters,
            }
            order.append(key)
            continue
        current = merged[key]
        current_parameters = dict(current.get("parameters") or {})
        current_parameters.update(stage.get("parameters") or {})
        current["parameters"] = current_parameters
        current["explicit_parameter_keys"] = sorted(set(
            list(current.get("explicit_parameter_keys") or [])
            + list(stage.get("explicit_parameter_keys") or [])
        ))
        for field in ("required_file_roles", "dependencies", "evidence"):
            values = list(current.get(field) or [])
            for item in stage.get(field) or []:
                if item not in values:
                    values.append(item)
            current[field] = values
        if not current.get("sweep") and stage.get("sweep"):
            current["sweep"] = stage["sweep"]
        if not current.get("input_files") and stage.get("input_files"):
            current["input_files"] = stage["input_files"]
    return [merged[key] for key in order]


def _expand_stage_sweeps(stages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    expanded: list[dict[str, Any]] = []
    for stage in stages:
        parameters = dict(stage.get("parameters") or {})
        sweep = stage.get("sweep") or parameters.pop("sweep", None)
        if not isinstance(sweep, dict):
            expanded.append({**stage, "parameters": parameters})
            continue
        dimensions = [
            item for item in sweep.get("dimensions") or []
            if isinstance(item, dict)
            and str(item.get("parameter") or "").strip()
            and isinstance(item.get("values"), list)
            and item.get("values")
        ]
        if not dimensions:
            parameter = str(sweep.get("parameter") or "").strip()
            values = sweep.get("values")
            if parameter and isinstance(values, list) and values:
                dimensions = [{"parameter": parameter, "values": values}]
        if not dimensions:
            expanded.append({**stage, "parameters": parameters})
            continue
        dimension_names = [str(item["parameter"]).strip() for item in dimensions]
        value_sets = [list(item["values"]) for item in dimensions]
        explicit_case_count = math.prod(len(values) for values in value_sets)
        parameter_space = stage.get("parameter_space") or {}
        unresolved_ranges = [
            item for item in parameter_space.get("unresolved_ranges") or []
            if isinstance(item, dict)
        ]
        expected_case_count = parameter_space.get("expected_case_count")
        try:
            expected_case_count = int(expected_case_count) if expected_case_count is not None else None
        except (TypeError, ValueError):
            expected_case_count = None
        validation_status = "pass"
        if unresolved_ranges:
            validation_status = "needs_sampling_design"
        elif expected_case_count is not None and expected_case_count != explicit_case_count:
            validation_status = "case_count_mismatch"
        parameter_space_validation = {
            "status": validation_status,
            "dimension_names": dimension_names,
            "dimension_sizes": [len(values) for values in value_sets],
            "explicit_case_count": explicit_case_count,
            "expected_case_count": expected_case_count,
            "unresolved_ranges": unresolved_ranges,
            "policy": "expand explicit values only; never invent continuous-range sample points",
        }
        stage_text = " ".join([
            str(stage.get("name") or ""),
            str(stage.get("calculation_type") or ""),
            json.dumps(stage.get("evidence") or [], ensure_ascii=False, default=str),
        ])
        strain_axes = parameters.get("strain_axes")
        if (
            dimension_names == ["strain_percent"]
            and not parameters.get("strain_axis")
            and re.search(r"\buniaxial\b|单轴", stage_text, flags=re.I)
        ):
            strain_axes = strain_axes if isinstance(strain_axes, list) else ["a", "b", "c"]
        sweep_case_index = 0
        for combination in product(*value_sets):
            condition = dict(zip(dimension_names, combination))
            axes = strain_axes if isinstance(strain_axes, list) and strain_axes else [None]
            for axis in axes:
                sweep_case_index += 1
                suffix_parts = [f"{key}_{value}" for key, value in condition.items()]
                condition_parameters = {**parameters, **condition}
                sweep_value: dict[str, Any] = {"parameters": condition}
                if len(condition) == 1:
                    sweep_value.update({
                        "parameter": dimension_names[0],
                        "value": combination[0],
                    })
                if axis is not None:
                    condition_parameters["strain_axis"] = str(axis)
                    suffix_parts.append(f"axis_{axis}")
                    sweep_value["strain_axis"] = str(axis)
                suffix = _slugify("__".join(suffix_parts), "condition").lower()
                condition_label = ", ".join(
                    [
                        *(f"{key}={value}" for key, value in condition.items()),
                        *([f"strain_axis={axis}"] if axis is not None else []),
                    ]
                )
                expanded.append({
                    **stage,
                    "id": f"{stage['id']}__{suffix}",
                    "name": f"{stage.get('name') or stage['id']} [{condition_label}]",
                    "parameters": condition_parameters,
                    "sweep_parent": stage["id"],
                    "sweep_case_id": f"case_{sweep_case_index:03d}",
                    "sweep_case_index": sweep_case_index,
                    "sweep_value": sweep_value,
                    "parameter_space_validation": parameter_space_validation,
                })
    return expanded


def _stage_output_prefix(root: str, stage: dict[str, Any]) -> str:
    """Place every expanded condition below its owning research-plan step."""
    stage_id = _slugify(str(stage.get("id") or "stage"), "stage").lower()
    parent_id = _slugify(str(stage.get("sweep_parent") or ""), "").lower()
    if not parent_id:
        return f"{root}/{stage_id}"
    case_id = _slugify(
        str(stage.get("sweep_case_id") or ""),
        "",
    ).lower()
    if not case_id:
        marker = f"{parent_id}__"
        case_id = stage_id[len(marker):] if stage_id.startswith(marker) else stage_id
    return f"{root}/{parent_id}/cases/{case_id or 'case'}"


def _write_sweep_case_indexes(
    files: dict[str, str],
    records: list[dict[str, Any]],
    root: str,
) -> None:
    """Write one compact condition index per swept step."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        parent = str(record.get("sweep_parent") or "").strip()
        if parent:
            groups.setdefault(parent, []).append(record)
    for parent, items in groups.items():
        parent_id = _slugify(parent, "stage").lower()
        ordered = sorted(
            items,
            key=lambda item: (
                int(item.get("sweep_case_index") or 0),
                str(item.get("id") or ""),
            ),
        )
        files[f"{root}/{parent_id}/case_index.json"] = _json_dumps({
            "stage_id": parent,
            "layout": "cases/<case_id>",
            "case_count": len(ordered),
            "cases": [{
                "case_id": item.get("sweep_case_id"),
                "record_id": item.get("id"),
                "parameters": (item.get("sweep_value") or {}).get("parameters") or {},
                "status": item.get("status"),
                "case_root": item.get("case_root"),
            } for item in ordered],
        })


def _vasp_stage_parameters(stage: dict[str, Any]) -> dict[str, Any]:
    values = dict(stage.get("parameters") or {})
    calculation = str(stage.get("calculation_type") or stage.get("name") or "").lower()
    text = _stage_text(stage)
    incar = dict(values.get("incar") or values.get("incar_parameters") or {})
    if re.search(r"\bneb\b|nudged elastic|扩散势垒|迁移路径", calculation):
        incar.setdefault("IBRION", 3)
        incar.setdefault("POTIM", 0)
        incar.setdefault("IMAGES", values.get("images", 7))
        incar.setdefault("SPRING", values.get("spring", -5.0))
    elif re.search(r"\baimd\b|molecular dynamics|分子动力学|nvt|nve", calculation):
        incar.setdefault("IBRION", 0)
        incar.setdefault("MDALGO", values.get("mdalgo", 2))
        incar.setdefault("POTIM", values.get("timestep_fs", values.get("potim", 1.0)))
        incar.setdefault("NSW", values.get("md_steps", values.get("nsw", 5000)))
        incar.setdefault("ISIF", values.get("isif", 2))
        if re.search(r"\bnvt\b|nos[eé].hoover|恒温", calculation, flags=re.I):
            incar.setdefault("SMASS", values.get("smass", 0))
        temperature = values.get("temperature")
        if temperature is None:
            sweep_value = stage.get("sweep_value") or {}
            if str(sweep_value.get("parameter") or "").lower() in {"t", "temperature"}:
                temperature = sweep_value.get("value")
        if temperature is None:
            match = re.search(r"([-+]?\d+(?:\.\d+)?)\s*K\b", text, flags=re.I)
            if match:
                raw_temperature = float(match.group(1))
                temperature = int(raw_temperature) if raw_temperature.is_integer() else raw_temperature
        if temperature is not None:
            incar.setdefault("TEBEG", temperature)
            incar.setdefault("TEEND", temperature)
    elif re.search(r"\bdfpt\b|phonon|声子|response", calculation):
        incar.setdefault("IBRION", 8)
        incar.setdefault("NSW", 1)
    elif re.search(r"static|single.point|dos|band|静态|能带|态密度", calculation):
        incar.setdefault("IBRION", -1)
        incar.setdefault("NSW", 0)
    elif re.search(r"\bbader\b", calculation):
        incar.setdefault("IBRION", -1)
        incar.setdefault("NSW", 0)
        incar.setdefault("LCHARG", True)
        incar.setdefault("LAECHG", True)
    elif re.search(r"relax|optimi[sz]|弛豫|结构优化", calculation):
        incar.setdefault("IBRION", 2)
        incar.setdefault("NSW", values.get("nsw", 200))
        incar.setdefault("ISIF", values.get("isif", 3))
    return {**values, "incar_parameters": incar, "calculation_type": stage.get("calculation_type")}


def _conservative_predecessor_incar(
    stage: dict[str, Any],
    stage_parameters: dict[str, Any],
    structure_generation: dict[str, Any] | None,
) -> dict[str, Any]:
    """Build stable relaxation controls for generated candidate structures."""
    precursor_incar = dict(stage_parameters.get("incar_parameters") or {})
    for key in (
        "IBRION", "NSW", "ISIF", "MDALGO", "POTIM", "TEBEG", "TEEND",
        "SMASS", "IMAGES", "SPRING",
    ):
        precursor_incar.pop(key, None)

    candidate_structure = bool(
        structure_generation
        and structure_generation.get("confidence") in {"heuristic", "provisional"}
    )
    modified_structure = structure_variant(stage) != "base_structure"
    if candidate_structure or modified_structure:
        return {
            **precursor_incar,
            "IBRION": int(stage_parameters.get("relaxation_ibrion") or 1),
            "NSW": int(stage_parameters.get("relaxation_steps") or 120),
            "ISIF": int(stage_parameters.get("relaxation_isif") or 2),
            "POTIM": float(stage_parameters.get("relaxation_potim") or 0.3),
            "ALGO": stage_parameters.get("relaxation_algo") or "Normal",
            "NELM": int(stage_parameters.get("relaxation_nelm") or 160),
            "ISYM": int(stage_parameters.get("relaxation_isym") or 0),
            "LREAL": stage_parameters.get("relaxation_lreal", False),
            "LASPH": stage_parameters.get("relaxation_lasph", True),
            "ADDGRID": stage_parameters.get("relaxation_addgrid", True),
        }
    return {
        **precursor_incar,
        "IBRION": int(stage_parameters.get("relaxation_ibrion") or 2),
        "NSW": int(stage_parameters.get("relaxation_steps") or 200),
        "ISIF": int(stage_parameters.get("relaxation_isif") or 3),
    }


def _poscar_structure_layout(value: str) -> dict[str, Any] | None:
    lines = str(value or "").splitlines()
    if len(lines) < 8:
        return None
    try:
        lattice = [[float(token) for token in lines[index].split()[:3]] for index in range(2, 5)]
        counts = [int(token) for token in lines[6].split()]
    except (ValueError, IndexError):
        return None
    coordinate_mode_index = 7
    if lines[coordinate_mode_index].strip().lower().startswith("s"):
        coordinate_mode_index += 1
    if (
        coordinate_mode_index >= len(lines)
        or not lines[coordinate_mode_index].strip().lower().startswith("d")
    ):
        return None
    coordinate_start = coordinate_mode_index + 1
    atom_count = sum(counts)
    if len(lines) < coordinate_start + atom_count:
        return None
    try:
        coordinates = [
            [float(token) for token in lines[coordinate_start + index].split()[:3]]
            for index in range(atom_count)
        ]
    except (ValueError, IndexError):
        return None
    suffixes = [
        lines[coordinate_start + index].split()[3:]
        for index in range(atom_count)
    ]
    return {
        "lines": lines,
        "lattice": lattice,
        "counts": counts,
        "coordinate_mode_index": coordinate_mode_index,
        "coordinate_start": coordinate_start,
        "coordinates": coordinates,
        "suffixes": suffixes,
    }


def _supercell_from_stage(stage: dict[str, Any]) -> tuple[int, int, int] | None:
    parameters = stage.get("parameters") or {}
    raw = parameters.get("supercell") or parameters.get("supercell_matrix")
    if isinstance(raw, (list, tuple)) and len(raw) == 3:
        try:
            factors = tuple(int(value) for value in raw)
        except (TypeError, ValueError):
            factors = ()
        if len(factors) == 3 and all(value > 0 for value in factors):
            return factors
    text = " ".join([
        str(stage.get("name") or ""),
        str(stage.get("calculation_type") or ""),
        json.dumps(stage.get("evidence") or [], ensure_ascii=False, default=str),
    ])
    match = re.search(r"(\d+)\s*[x×]\s*(\d+)\s*[x×]\s*(\d+)\s*supercell", text, flags=re.I)
    if match:
        return tuple(int(match.group(index)) for index in range(1, 4))
    return None


def _strain_from_stage(stage: dict[str, Any]) -> tuple[int, float] | None:
    parameters = stage.get("parameters") or {}
    value = parameters.get("strain_percent")
    if value is None:
        return None
    axis = str(parameters.get("strain_axis") or parameters.get("axis") or "").strip().lower()
    if not axis:
        text = " ".join([
            str(stage.get("name") or ""),
            str(stage.get("calculation_type") or ""),
            json.dumps(stage.get("evidence") or [], ensure_ascii=False, default=str),
        ])
        match = re.search(r"\b([abcxyz])[- ]?axis\b|沿\s*([abcxyz])\s*轴", text, flags=re.I)
        axis = next((item for item in (match.groups() if match else ()) if item), "").lower()
    axis_index = {"a": 0, "x": 0, "b": 1, "y": 1, "c": 2, "z": 2}.get(axis)
    if axis_index is None:
        return None
    try:
        return axis_index, float(value)
    except (TypeError, ValueError):
        return None


def _transform_poscar_for_stage(poscar: str, stage: dict[str, Any]) -> str | None:
    layout = _poscar_structure_layout(poscar)
    if layout is None:
        return None
    lines = list(layout["lines"])
    supercell = _supercell_from_stage(stage)
    if supercell:
        for axis, factor in enumerate(supercell):
            lines[2 + axis] = "  ".join(
                f"{value * factor:.16f}" for value in layout["lattice"][axis]
            )
        lines[6] = " ".join(str(count * (supercell[0] * supercell[1] * supercell[2])) for count in layout["counts"])
        transformed: list[str] = []
        offset = 0
        for count in layout["counts"]:
            for atom_index in range(offset, offset + count):
                coordinate = layout["coordinates"][atom_index]
                suffix = layout["suffixes"][atom_index]
                for i in range(supercell[0]):
                    for j in range(supercell[1]):
                        for k in range(supercell[2]):
                            values = (
                                (coordinate[0] + i) / supercell[0],
                                (coordinate[1] + j) / supercell[1],
                                (coordinate[2] + k) / supercell[2],
                            )
                            transformed.append(
                                "  ".join(f"{value:.16f}" for value in values)
                                + (f"  {' '.join(suffix)}" if suffix else "")
                            )
            offset += count
        lines = lines[:layout["coordinate_start"]] + transformed
    strain = _strain_from_stage(stage)
    if strain:
        axis, percent = strain
        lattice = [float(token) for token in lines[2 + axis].split()[:3]]
        lines[2 + axis] = "  ".join(f"{value * (1.0 + percent / 100.0):.16f}" for value in lattice)
    if not supercell and not strain:
        return None
    return "\n".join(lines).rstrip() + "\n"


_CHEMICAL_SYMBOLS = {
    "H", "He", "Li", "Be", "B", "C", "N", "O", "F", "Ne", "Na", "Mg", "Al", "Si",
    "P", "S", "Cl", "Ar", "K", "Ca", "Sc", "Ti", "V", "Cr", "Mn", "Fe", "Co", "Ni",
    "Cu", "Zn", "Ga", "Ge", "As", "Se", "Br", "Kr", "Rb", "Sr", "Y", "Zr", "Nb", "Mo",
    "Tc", "Ru", "Rh", "Pd", "Ag", "Cd", "In", "Sn", "Sb", "Te", "I", "Xe", "Cs", "Ba",
    "La", "Ce", "Pr", "Nd", "Pm", "Sm", "Eu", "Gd", "Tb", "Dy", "Ho", "Er", "Tm", "Yb",
    "Lu", "Hf", "Ta", "W", "Re", "Os", "Ir", "Pt", "Au", "Hg", "Tl", "Pb", "Bi", "Po",
    "At", "Rn",
}

_COMMON_MOBILE_ION_NAMES = {
    "hydrogen": "H",
    "lithium": "Li",
    "sodium": "Na",
    "potassium": "K",
    "magnesium": "Mg",
    "calcium": "Ca",
    "aluminum": "Al",
    "aluminium": "Al",
    "zinc": "Zn",
    "钠": "Na",
    "锂": "Li",
    "钾": "K",
    "镁": "Mg",
    "钙": "Ca",
    "铝": "Al",
    "锌": "Zn",
}

_MOBILE_ION_PRIORITY = ("Na", "Li", "K", "Mg", "Ca", "Al", "Zn", "H")


def _strip_citation_author_symbols(text: str) -> str:
    """Remove common author-citation fragments that look like element symbols."""
    return re.sub(
        r"\b([A-Z][a-z]?)\s+et\s+al\.?",
        " ",
        str(text or ""),
        flags=re.I,
    )


def _mobile_species_candidates(text: str, host_species: list[str]) -> list[str]:
    clean = _strip_citation_author_symbols(text)
    found: list[str] = []

    def add(symbol: str) -> None:
        if symbol in _CHEMICAL_SYMBOLS and symbol not in host_species and symbol not in found:
            found.append(symbol)

    for name, symbol in _COMMON_MOBILE_ION_NAMES.items():
        if re.search(rf"(?<![A-Za-z]){re.escape(name)}(?:\s*[-+]?ion|离子|[⁺+\-])?", clean, flags=re.I):
            add(symbol)
    for symbol in _MOBILE_ION_PRIORITY:
        if re.search(
            rf"(?<![A-Za-z]){re.escape(symbol)}\s*(?:[⁺+\-]|ion\b|离子|adsorption|吸附|intercalat|嵌入|插层|迁移|migration)",
            clean,
            flags=re.I,
        ):
            add(symbol)
    contextual = [
        token
        for token in re.findall(
            r"(?<![A-Za-z])([A-Z][a-z]?)"
            r"(?=\s*(?:[⁺+\-]|ion\b|离子|adsorption|吸附|intercalat|嵌入|插层|迁移|migration))",
            clean,
            flags=re.I,
        )
        if token in _CHEMICAL_SYMBOLS and token not in host_species
    ]
    for token in contextual:
        add(token)
    return sorted(found, key=lambda item: _MOBILE_ION_PRIORITY.index(item) if item in _MOBILE_ION_PRIORITY else 999)


def _stage_inserted_species(
    stage: dict[str, Any],
    host_species: list[str],
    global_context: str = "",
) -> str:
    parameters = stage.get("parameters") or {}
    explicit = str(
        parameters.get("inserted_species")
        or parameters.get("adsorbate")
        or parameters.get("guest_species")
        or ""
    ).strip()
    if explicit in _CHEMICAL_SYMBOLS:
        return explicit
    text = " ".join([
        str(stage.get("name") or ""),
        str(stage.get("calculation_type") or ""),
        json.dumps(stage.get("evidence") or [], ensure_ascii=False, default=str),
    ])
    mobile_candidates = _mobile_species_candidates(f"{text}\n{global_context}", host_species)
    if mobile_candidates:
        return mobile_candidates[0]
    clean_text = _strip_citation_author_symbols(text)
    candidates = [
        token for token in re.findall(r"(?<![A-Za-z])([A-Z][a-z]?)(?:[⁺+\-]|(?=\d|[^A-Za-z]|$))", clean_text)
        if token in _CHEMICAL_SYMBOLS and token not in host_species
    ]
    if candidates:
        return candidates[0]
    return ""


def _fractional_cartesian(
    fractional: list[float] | tuple[float, float, float],
    lattice: list[list[float]],
) -> tuple[float, float, float]:
    return tuple(
        sum(float(fractional[index]) * lattice[index][axis] for index in range(3))
        for axis in range(3)
    )


def _periodic_distance(
    first: list[float] | tuple[float, float, float],
    second: list[float] | tuple[float, float, float],
    lattice: list[list[float]],
) -> float:
    delta = [first[index] - second[index] for index in range(3)]
    delta = [value - round(value) for value in delta]
    cart = _fractional_cartesian(delta, lattice)
    return math.sqrt(sum(value * value for value in cart))


def _void_candidate_reports(poscar: str, count: int = 8) -> list[dict[str, Any]]:
    layout = _poscar_structure_layout(poscar)
    if layout is None:
        return []
    candidates: list[tuple[float, list[float], int]] = []
    grid = 8
    for i in range(grid):
        for j in range(grid):
            for k in range(grid):
                point = [(i + 0.5) / grid, (j + 0.5) / grid, (k + 0.5) / grid]
                distances = [
                    _periodic_distance(point, coordinate, layout["lattice"])
                    for coordinate in layout["coordinates"]
                ]
                clearance = min(distances)
                nearest_index = distances.index(clearance)
                candidates.append((clearance, point, nearest_index))
    selected: list[dict[str, Any]] = []
    for clearance, point, nearest_index in sorted(candidates, key=lambda item: item[0], reverse=True):
        if all(
            _periodic_distance(point, other["fractional_position"], layout["lattice"]) >= 1.2
            for other in selected
        ):
            selected.append({
                "fractional_position": point,
                "minimum_host_distance_angstrom": round(clearance, 6),
                "nearest_host_atom_index": nearest_index + 1,
                "geometry_status": "candidate" if clearance >= 1.2 else "too_close",
            })
        if len(selected) >= count:
            break
    return selected


def _candidate_sets(
    reports: list[dict[str, Any]],
    insert_count: int,
    candidate_count: int,
) -> list[list[dict[str, Any]]]:
    if insert_count <= 0:
        return []
    sets: list[list[dict[str, Any]]] = []
    for start in range(max(1, min(candidate_count, len(reports)))):
        selected = reports[start:start + insert_count]
        if len(selected) < insert_count:
            break
        sets.append(selected)
    return sets


def _candidate_geometry_summary(poscar: str, inserted_species: str = "") -> dict[str, Any]:
    layout = _poscar_structure_layout(poscar)
    if layout is None:
        return {"valid": False, "reason": "invalid POSCAR layout"}
    species = layout["lines"][5].split()
    labels: list[str] = []
    for symbol, count in zip(species, layout["counts"]):
        labels.extend([symbol] * count)
    min_pair: tuple[float, int, int] | None = None
    min_inserted_host: tuple[float, int, int] | None = None
    for i, first in enumerate(layout["coordinates"]):
        for j in range(i + 1, len(layout["coordinates"])):
            distance = _periodic_distance(first, layout["coordinates"][j], layout["lattice"])
            if min_pair is None or distance < min_pair[0]:
                min_pair = (distance, i + 1, j + 1)
            if inserted_species and labels[i] != labels[j] and inserted_species in {labels[i], labels[j]}:
                if min_inserted_host is None or distance < min_inserted_host[0]:
                    min_inserted_host = (distance, i + 1, j + 1)
    hard_collision = bool(min_pair and min_pair[0] < 0.75)
    return {
        "valid": not hard_collision,
        "minimum_pair_distance_angstrom": round(min_pair[0], 6) if min_pair else None,
        "minimum_pair_atom_indices": list(min_pair[1:]) if min_pair else [],
        "minimum_inserted_host_distance_angstrom": round(min_inserted_host[0], 6) if min_inserted_host else None,
        "minimum_inserted_host_atom_indices": list(min_inserted_host[1:]) if min_inserted_host else [],
        "hard_collision": hard_collision,
    }


def _append_poscar_species(poscar: str, species: str, positions: list[list[float]]) -> str | None:
    layout = _poscar_structure_layout(poscar)
    if layout is None or not species or not positions:
        return None
    lines = list(layout["lines"])
    host_species = lines[5].split()
    counts = list(layout["counts"])
    coordinate_lines = lines[
        layout["coordinate_start"]:layout["coordinate_start"] + sum(counts)
    ]
    new_lines = [
        "  ".join(f"{value % 1.0:.16f}" for value in position)
        for position in positions
    ]
    if species in host_species:
        species_index = host_species.index(species)
        insert_at = sum(counts[:species_index + 1])
        coordinate_lines[insert_at:insert_at] = new_lines
        counts[species_index] += len(new_lines)
    else:
        host_species.append(species)
        counts.append(len(new_lines))
        coordinate_lines.extend(new_lines)
    lines[5] = " ".join(host_species)
    lines[6] = " ".join(str(value) for value in counts)
    lines = lines[:layout["coordinate_start"]] + coordinate_lines
    return "\n".join(lines).rstrip() + "\n"


def _composition_supercell(value: Any) -> tuple[tuple[int, int, int], int] | None:
    try:
        fraction = Fraction(float(value)).limit_denominator(8)
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    if fraction <= 0:
        return None
    denominator = fraction.denominator
    factors = [1, 1, 1]
    divisor = 2
    remaining = denominator
    axis = 0
    while divisor * divisor <= remaining:
        while remaining % divisor == 0:
            factors[axis % 3] *= divisor
            remaining //= divisor
            axis += 1
        divisor += 1
    if remaining > 1:
        factors[axis % 3] *= remaining
    return (factors[0], factors[1], factors[2]), fraction.numerator


def _perturb_poscar(poscar: str, amplitude_fractional: float = 0.01) -> str | None:
    layout = _poscar_structure_layout(poscar)
    if layout is None:
        return None
    lines = list(layout["lines"])
    coordinates: list[str] = []
    for index, coordinate in enumerate(layout["coordinates"]):
        direction = (
            math.sin((index + 1) * 1.618),
            math.sin((index + 1) * 2.414),
            math.sin((index + 1) * 3.142),
        )
        shifted = [
            (coordinate[axis] + amplitude_fractional * direction[axis]) % 1.0
            for axis in range(3)
        ]
        suffix = layout["suffixes"][index]
        coordinates.append(
            "  ".join(f"{value:.16f}" for value in shifted)
            + (f"  {' '.join(suffix)}" if suffix else "")
        )
    lines[0] = f"{lines[0]} heuristic symmetry-broken candidate"
    lines = lines[:layout["coordinate_start"]] + coordinates
    return "\n".join(lines).rstrip() + "\n"


def _heuristic_stage_structures(
    base_poscar: str,
    stage: dict[str, Any],
    global_context: str = "",
) -> tuple[dict[str, str], dict[str, Any] | None]:
    layout = _poscar_structure_layout(base_poscar)
    if layout is None:
        return {}, None
    text = " ".join([
        str(stage.get("name") or ""),
        str(stage.get("calculation_type") or ""),
        json.dumps(stage.get("evidence") or [], ensure_ascii=False, default=str),
    ])
    host_species = layout["lines"][5].split()
    inserted_species = _stage_inserted_species(stage, host_species, global_context)
    parameters = stage.get("parameters") or {}
    try:
        candidate_count = max(1, min(8, int(parameters.get("candidate_count") or parameters.get("site_count") or 4)))
    except (TypeError, ValueError):
        candidate_count = 4
    void_reports = _void_candidate_reports(base_poscar, count=max(12, candidate_count))
    voids = [list(item["fractional_position"]) for item in void_reports]
    if re.search(r"\bneb\b|endpoint|migration path|扩散路径|迁移路径|端点", text, flags=re.I):
        if not inserted_species or len(voids) < 2:
            return {}, None
        images = int((stage.get("parameters") or {}).get("images") or 7)
        files: dict[str, str] = {}
        start, end = voids[0], voids[1]
        delta = [end[index] - start[index] for index in range(3)]
        delta = [value - round(value) for value in delta]
        for image in range(images + 2):
            fraction = image / (images + 1)
            position = [[(start[axis] + fraction * delta[axis]) % 1.0 for axis in range(3)]]
            candidate = _append_poscar_species(base_poscar, inserted_species, position)
            if candidate:
                files[f"{image:02d}/POSCAR"] = candidate
        if files:
            geometry = [
                _candidate_geometry_summary(content, inserted_species)
                for name, content in sorted(files.items())
                if name.endswith("/POSCAR")
            ]
            return files, {
                "method": "periodic_void_search_and_minimum_image_interpolation",
                "confidence": "heuristic",
                "inserted_species": inserted_species,
                "image_count": len(files),
                "geometry_checks": geometry,
                "requires_relaxation": True,
            }
    if re.search(r"adsorption|吸附", text, flags=re.I):
        if inserted_species and void_reports:
            files: dict[str, str] = {}
            reports: list[dict[str, Any]] = []
            for index, report in enumerate(void_reports[:candidate_count], 1):
                candidate = _append_poscar_species(
                    base_poscar,
                    inserted_species,
                    [list(report["fractional_position"])],
                )
                if not candidate:
                    continue
                geometry = _candidate_geometry_summary(candidate, inserted_species)
                if not geometry.get("valid"):
                    continue
                candidate_report = {
                    **report,
                    "candidate_id": f"site_{index:02d}",
                    "geometry": geometry,
                    "selected_as_primary": index == 1,
                }
                reports.append(candidate_report)
                files[f"candidates/site_{index:02d}/POSCAR"] = candidate
                if index == 1:
                    files["POSCAR"] = candidate
            if files.get("POSCAR"):
                files["candidate_structures.json"] = _json_dumps({
                    "structure_role": "adsorption_candidate_sites",
                    "inserted_species": inserted_species,
                    "selection_policy": "rank by maximum periodic host clearance; validate by short conservative relaxation",
                    "candidates": reports,
                })
                return files, {
                    "method": "periodic_maximum_clearance_void_site",
                    "confidence": "heuristic",
                    "inserted_species": inserted_species,
                    "candidate_site_count": len(reports),
                    "primary_candidate_id": "site_01",
                    "geometry_checks": reports[0].get("geometry") if reports else {},
                    "requires_relaxation": True,
                }
    if re.search(r"intercalat|insertion|嵌入|插层", text, flags=re.I):
        if not inserted_species:
            return {}, None
        composition = parameters.get("x")
        composition_plan = _composition_supercell(composition)
        working = base_poscar
        insert_count = 1
        supercell = _supercell_from_stage(stage) or (1, 1, 1)
        if composition_plan:
            supercell, insert_count = composition_plan
        if supercell != (1, 1, 1):
            working = _transform_poscar_for_stage(
                base_poscar,
                {**stage, "parameters": {**(stage.get("parameters") or {}), "supercell": supercell}},
            ) or base_poscar
        working_reports = _void_candidate_reports(working, count=max(insert_count + candidate_count, 12))
        files: dict[str, str] = {}
        reports: list[dict[str, Any]] = []
        for index, candidate_set in enumerate(
            _candidate_sets(working_reports, insert_count, candidate_count),
            1,
        ):
            positions = [list(item["fractional_position"]) for item in candidate_set]
            candidate = _append_poscar_species(working, inserted_species, positions)
            if not candidate:
                continue
            geometry = _candidate_geometry_summary(candidate, inserted_species)
            if not geometry.get("valid"):
                continue
            candidate_report = {
                "candidate_id": f"site_{index:02d}",
                "inserted_positions": candidate_set,
                "geometry": geometry,
                "selected_as_primary": index == 1,
            }
            reports.append(candidate_report)
            files[f"candidates/site_{index:02d}/POSCAR"] = candidate
            if index == 1:
                files["POSCAR"] = candidate
        if files.get("POSCAR"):
            files["candidate_structures.json"] = _json_dumps({
                "structure_role": "intercalation_candidate_sites",
                "inserted_species": inserted_species,
                "insert_count": insert_count,
                "supercell": list(supercell),
                "selection_policy": "rank by maximum periodic host clearance; validate by short conservative relaxation",
                "candidates": reports,
            })
            return files, {
                "method": "stoichiometric_supercell_and_periodic_void_placement",
                "confidence": "heuristic",
                "inserted_species": inserted_species,
                "insert_count": insert_count,
                "supercell": list(supercell),
                "candidate_site_count": len(reports),
                "primary_candidate_id": "site_01",
                "geometry_checks": reports[0].get("geometry") if reports else {},
                "requires_relaxation": True,
            }
    if re.search(r"control structure|symmetry.break|distort|控制结构|对照结构|对称性破", text, flags=re.I):
        candidate = _perturb_poscar(base_poscar)
        if candidate:
            return {"POSCAR": candidate}, {
                "method": "deterministic_small_symmetry_breaking_displacement",
                "confidence": "heuristic",
                "requires_relaxation": True,
                "requires_property_verification": True,
            }
    return {}, None


def _stage_text(stage: dict[str, Any]) -> str:
    return stage_text(stage)


def _stage_execution_kind(stage: dict[str, Any]) -> str:
    return stage_execution_kind(stage)


def _stage_kind(stage: dict[str, Any]) -> str:
    return stage_kind(stage)


def _structure_source_role(stage: dict[str, Any]) -> str:
    return structure_source_role(stage)


def _upstream_structure_contract(
    stage: dict[str, Any],
    previous_stages: list[dict[str, Any]],
    target_path: str,
) -> dict[str, Any] | None:
    text = _stage_text(stage)
    execution_kind = _stage_execution_kind(stage)
    source_role = _structure_source_role(stage)
    explicit_dependencies = [
        str(value).strip().casefold()
        for value in stage.get("dependencies") or []
        if str(value).strip()
    ]
    previous_by_id = {
        str(item.get("id") or "").strip().casefold(): item
        for item in previous_stages
        if str(item.get("id") or "").strip()
    }
    def produces_runtime_structure(value: dict[str, Any]) -> bool:
        family = _calculation_stage_family(value)
        value_text = _stage_text(value)
        return bool(
            family in {
                "relaxation",
                "intercalation",
                "adsorption",
                "strain",
                "neb",
                "control_structure",
                "static",
                "aimd",
                "phonon",
                "dfpt",
                "band_structure",
                "dos",
            }
            or re.search(
                r"\b(relax|optimi[sz]|minimi[sz]|equilibrat|anneal|"
                r"geometry|structure)\b.*\b(output|result|final|converged)\b|"
                r"弛豫|优化|收敛结构|稳定结构|最终结构",
                value_text,
                flags=re.I,
            )
        )

    runtime_structure_sources = [
        dep_id
        for dep_id in explicit_dependencies
        if produces_runtime_structure(previous_by_id.get(dep_id, {}))
    ]
    if execution_kind == "solver_input" and runtime_structure_sources:
        return {
            "status": "deferred_dependency",
            "target": target_path,
            "source_stages": runtime_structure_sources,
            "source_artifact_role": "converged_structure",
            "source_file_patterns": [
                "converged_structure.*",
                "final_structure.*",
                "optimized_structure.*",
                "relaxed_structure.*",
                "CONTCAR",
                "*/CONTCAR",
            ],
            "transform": "promote_converged_structure_to_stage_input",
            "validation": [
                "source calculation reached the approved convergence criteria",
                "source structure is readable and structurally valid",
                "species order and atom count are preserved",
            ],
            "execution_kind": "solver_input",
        }
    if execution_kind == "data_derivation" and source_role == "trajectory":
        sources = explicit_dependencies or [
            str(item.get("id"))
            for item in previous_stages
            if _calculation_stage_family(item) == "aimd"
        ]
        selection_count_match = re.search(
            r"(?:extract|select|sample|提取|选择|采样)\s*(?:≥|>=|at least|至少)?\s*(\d+)",
            text,
            flags=re.I,
        )
        minimum_per_source = int(selection_count_match.group(1)) if selection_count_match else None
        return {
            "status": "deferred_dependency",
            "target": target_path.rsplit("/", 1)[0] + "/derived_structures/{source_stage}/item_*/POSCAR",
            "source_stages": sources,
            "source_artifact_role": "trajectory",
            "source_file_patterns": ["XDATCAR", "trajectory.*", "*.traj"],
            "transform": "extract_representative_structures",
            "selection_policy": "approved research-plan selection criterion",
            "minimum_outputs_per_source": minimum_per_source,
            "validation": [
                "source simulation completed and trajectory is readable",
                "selected structures satisfy the approved decorrelation/selection criterion",
                "species order and atom count are preserved",
            ],
            "execution_kind": "data_derivation",
        }
    if execution_kind == "solver_input" and source_role == "trajectory":
        sources = explicit_dependencies or [
            str(item.get("id"))
            for item in previous_stages
            if _stage_execution_kind(item) == "data_derivation"
            and _structure_source_role(item) == "trajectory"
        ]
        return {
            "status": "deferred_dependency",
            "target": target_path,
            "source_stages": sources,
            "source_artifact_role": "derived_structure_collection",
            "source_file_patterns": ["derived_structures/*/item_*/POSCAR"],
            "transform": "instantiate_one_calculation_directory_per_derived_structure",
            "validation": [
                "snapshot POSCAR is structurally valid",
                "species order matches POTCAR",
                "the source snapshot identifier is recorded in lineage",
            ],
            "execution_kind": "solver_input",
        }
    return None


def _stable_structure_required(
    stage: dict[str, Any],
    structure_generation: dict[str, Any] | None,
) -> bool:
    parameters = stage.get("parameters") or {}
    if parameters.get("structure_is_relaxed") is True:
        return False
    family = _calculation_stage_family(stage)
    text = _stage_text(stage)
    return bool(
        family == "aimd"
        or (
            family in {"dfpt", "band_structure", "static", "bader"}
            and re.search(r"equilibrium|stable|relaxed|optimized|平衡|稳定|弛豫后|优化后", text, flags=re.I)
        )
        or (
            structure_generation
            and structure_generation.get("requires_relaxation") is True
            and structure_generation.get("confidence") in {"heuristic", "provisional"}
        )
    )


def _simulation_stage_generator(
    discipline: str,
    parameters: dict[str, Any],
) -> dict[str, str]:
    solver = parameters.get("simulation_software") or ((parameters.get("requirement_analysis") or {}).get("simulation_software") or {})
    if discipline == "heat_transfer" and (
        re.search(r"abaqus|calculix", str(solver), flags=re.I)
        or any(Path(str(path)).suffix.lower() == ".inp"
               for path in (parameters.get("requested_output_paths") or {}).values())
    ):
        return _csm_files({**parameters, "discipline": discipline})
    if discipline in {"cfd", "heat_transfer"}:
        return _openfoam_files(discipline, parameters)
    if discipline == "md":
        return _lammps_files(parameters)
    if discipline == "csm":
        return _csm_files(parameters)
    if discipline == "cem":
        return _cem_files(parameters)
    if discipline == "multiphysics":
        return _multiphysics_files(parameters)
    return {}


class _TrackedStageParameters(dict[str, Any]):
    """Dictionary that records which plan controls an adapter actually reads."""

    def __init__(self, values: dict[str, Any]) -> None:
        super().__init__(values)
        self.accessed_keys: set[str] = set()

    def _record(self, key: Any) -> None:
        canonical = canonical_stage_parameter_key(str(key))
        if canonical:
            self.accessed_keys.add(canonical)

    def get(self, key: Any, default: Any = None) -> Any:
        self._record(key)
        return super().get(key, default)

    def __getitem__(self, key: Any) -> Any:
        self._record(key)
        return super().__getitem__(key)

    def __contains__(self, key: object) -> bool:
        self._record(key)
        return super().__contains__(key)

    def setdefault(self, key: Any, default: Any = None) -> Any:
        self._record(key)
        return super().setdefault(key, default)


def _generate_with_parameter_trace(
    generator: Any,
    parameters: dict[str, Any],
    *args: Any,
) -> tuple[dict[str, str], set[str]]:
    tracked = _TrackedStageParameters(parameters)
    generated = generator(tracked, *args)
    return generated, tracked.accessed_keys


def _generate_simulation_stage_with_trace(
    discipline: str,
    parameters: dict[str, Any],
) -> tuple[dict[str, str], set[str]]:
    tracked = _TrackedStageParameters(parameters)
    generated = _simulation_stage_generator(discipline, tracked)
    return generated, tracked.accessed_keys


def _xfoil_preprocessing_files(
    parameters: dict[str, Any],
) -> tuple[dict[str, str], set[str], list[str]]:
    """Build an XFOIL input deck without running the downstream calculation."""
    consumed: set[str] = set()
    missing: list[str] = []

    profile_content = ""
    profile_source = ""
    for key in ("coordinate_profile_path", "profile_dat_path", "airfoil_dat_path"):
        value = parameters.get(key)
        if value in (None, ""):
            continue
        consumed.add(canonical_stage_parameter_key(key))
        path = Path(str(value)).expanduser()
        if path.is_file():
            profile_content = path.read_text(encoding="utf-8", errors="replace")
            profile_source = str(path.resolve())
            break

    airfoil = str(parameters.get("airfoil") or parameters.get("airfoil_name") or "").strip()
    naca_code = str(parameters.get("naca_code") or "").strip()
    naca_match = re.search(r"(?i)NACA\s*([0-9]{4,5})", airfoil)
    if not naca_code and naca_match:
        naca_code = naca_match.group(1)
        consumed.add(canonical_stage_parameter_key("airfoil"))
    if naca_code:
        consumed.add(canonical_stage_parameter_key("naca_code"))

    if profile_content:
        geometry_command = "LOAD airfoil.dat"
    elif re.fullmatch(r"[0-9]{4,5}", naca_code):
        geometry_command = f"NACA {naca_code}"
    else:
        geometry_command = ""
        missing.append("coordinate profile for the declared XFOIL geometry")

    reynolds_key = (
        "reynolds_numbers"
        if parameters.get("reynolds_numbers") not in (None, "")
        else "reynolds_number"
    )
    reynolds_value = parameters.get(reynolds_key)
    if isinstance(reynolds_value, (list, tuple)):
        reynolds = [float(value) for value in reynolds_value]
    else:
        reynolds = [float(reynolds_value)] if reynolds_value not in (None, "") else []
    if reynolds:
        consumed.add(canonical_stage_parameter_key(reynolds_key))
    if not reynolds:
        missing.append("Reynolds number(s) for the declared viscous XFOIL stage")

    alpha_range_key = (
        "alpha_range_deg"
        if parameters.get("alpha_range_deg") not in (None, "")
        else "alpha_range"
    )
    alpha_range = parameters.get(alpha_range_key)
    alpha_values: list[float] = []
    if isinstance(alpha_range, dict):
        alpha_start = alpha_range.get("min")
        alpha_stop = alpha_range.get("max")
        alpha_step = alpha_range.get("step")
        consumed.add(canonical_stage_parameter_key(alpha_range_key))
    elif isinstance(alpha_range, (list, tuple)) and alpha_range:
        alpha_values = [float(value) for value in alpha_range]
        alpha_start = alpha_values[0]
        alpha_stop = alpha_values[-1]
        alpha_step = (
            alpha_values[1] - alpha_values[0]
            if len(alpha_values) > 1 else None
        )
        consumed.add(canonical_stage_parameter_key(alpha_range_key))
    elif isinstance(alpha_range, str):
        normalized_range = alpha_range.replace("°", " ")
        range_match = re.search(
            r"([-+0-9.eE]+)\s*(?:to|至|[-–~])\s*([-+0-9.eE]+)"
            r"\s*,?\s*(?:step|步长)\s*([-+0-9.eE]+)",
            normalized_range,
            flags=re.I,
        )
        if range_match:
            alpha_start, alpha_stop, alpha_step = (
                float(range_match.group(1)),
                float(range_match.group(2)),
                float(range_match.group(3)),
            )
            consumed.add(canonical_stage_parameter_key(alpha_range_key))
        else:
            alpha_start = parameters.get("angle_of_attack_start")
            alpha_stop = parameters.get("angle_of_attack_end")
            alpha_step = parameters.get("angle_of_attack_step")
    else:
        alpha_start = parameters.get("angle_of_attack_start")
        alpha_stop = parameters.get("angle_of_attack_end")
        alpha_step = parameters.get("angle_of_attack_step")
        for key, value in (
            ("angle_of_attack_start", alpha_start),
            ("angle_of_attack_end", alpha_stop),
            ("angle_of_attack_step", alpha_step),
        ):
            if value not in (None, ""):
                consumed.add(canonical_stage_parameter_key(key))
    if not alpha_values and any(value in (None, "") for value in (alpha_start, alpha_stop, alpha_step)):
        missing.append("explicit angle-of-attack start, end, and step for XFOIL")

    ncrit = parameters.get("ncrit", parameters.get("n_crit"))
    if ncrit not in (None, ""):
        consumed.add(canonical_stage_parameter_key(
            "ncrit" if parameters.get("ncrit") not in (None, "") else "n_crit"
        ))
    if missing:
        return {}, consumed, missing

    commands = [geometry_command, "PANE"]
    polar_files: list[str] = []
    for reynolds_number in reynolds:
        label = f"{reynolds_number:g}".replace("+", "").replace(".", "p")
        polar_name = f"polar_re_{label}.dat"
        polar_files.append(polar_name)
        commands.extend([
            "OPER",
            f"VISC {reynolds_number:g}",
            *(["VPAR", f"N {float(ncrit):g}", ""] if ncrit not in (None, "") else []),
            "ITER 250",
            "PACC",
            polar_name,
            "",
            *(
                [f"ALFA {value:g}" for value in alpha_values]
                if alpha_values
                else [f"ASEQ {float(alpha_start):g} {float(alpha_stop):g} {float(alpha_step):g}"]
            ),
            "PACC",
            "",
        ])
    commands.append("QUIT")
    case_contract = {
        "tool": "XFOIL",
        "execution_kind": "preprocessing_input",
        "geometry_source": profile_source or f"analytic_naca:{naca_code}",
        "reynolds_numbers": reynolds,
        "angle_of_attack_range_deg": {
            "start": float(alpha_start),
            "stop": float(alpha_stop),
            "step": float(alpha_step) if alpha_step not in (None, "") else None,
            "values": alpha_values or None,
        },
        "ncrit": float(ncrit) if ncrit not in (None, "") else None,
        "model_fidelity": parameters.get("model_fidelity"),
        "expected_outputs": polar_files,
        "run_policy": "execute in the downstream experiment node; data node only prepares inputs",
    }
    files = {
        "xfoil.in": "\n".join(commands) + "\n",
        "run_xfoil.sh": "#!/bin/sh\nset -eu\nxfoil < xfoil.in\n",
        "xfoil_case.json": _json_dumps(case_contract),
    }
    if profile_content:
        files["airfoil.dat"] = profile_content.rstrip() + "\n"
    return files, consumed, []


def _normalise_stage_runtime_parameters(parameters: dict[str, Any]) -> dict[str, Any]:
    """Map explicit research-plan aliases to generator-neutral controls."""
    normalized = dict(parameters)
    # Apply the shared canonicalisation first.  Research plans commonly use
    # domain notation such as ``Re`` or ``CFL``; matching aliases only by the
    # original spelling made their propagation depend on capitalization.
    for key, value in tuple(normalized.items()):
        canonical_key = canonical_stage_parameter_key(str(key))
        if canonical_key and canonical_key not in normalized:
            normalized[canonical_key] = value
    aliases = {
        "angle_of_attack_deg": "angle_of_attack",
        "alpha_deg": "angle_of_attack",
        "re": "reynolds_number",
        "cfl_max": "max_co",
        "timestep": "delta_t",
        "time_step": "delta_t",
        "energy_cutoff": "encut",
        "kpoint_mesh": "kmesh",
        "kpoints": "kmesh",
        "young_modulus": "youngs_modulus",
    }
    for source, target in aliases.items():
        if normalized.get(target) in (None, "") and normalized.get(source) not in (None, ""):
            normalized[target] = normalized[source]
    if normalized.get("end_time") in (None, ""):
        for source in (
            "total_simulation_time_tUinf_over_c",
            "extended_simulation_time_tUinf_over_c",
            "simulation_time",
            "run_time",
        ):
            if normalized.get(source) not in (None, ""):
                normalized["end_time"] = normalized[source]
                normalized["end_time_source"] = source
                break
    return normalized


def _inherit_reusable_mesh_provenance(
    parameters: dict[str, Any],
    mesh_result: dict[str, Any] | None,
) -> dict[str, Any]:
    """Reuse the coordinate profile that produced an accepted mesh."""
    effective = dict(parameters or {})
    coordinate_path = (mesh_result or {}).get("coordinate_profile_path")
    if effective.get("coordinate_profile_path") in (None, "", [], {}) and coordinate_path:
        effective["coordinate_profile_path"] = coordinate_path
    return effective


def _approved_stage_parameters(
    package_parameters: dict[str, Any],
    stage: dict[str, Any],
    *,
    normalize_runtime: bool = True,
) -> dict[str, Any]:
    """Merge the approved plan's stage scope into adapter parameters.

    The helper is discipline-neutral: a stage can describe a mesh, a solver
    deck, a structure, a table transformation, or another scientific asset.
    Keeping the same merge rule for generation and local repair prevents a
    repair from silently falling back to package defaults.
    """
    effective = {
        **dict(package_parameters or {}),
        **dict(stage.get("parameters") or {}),
    }
    # A stage's explicit alias (for example ``Re`` or ``alpha_deg``) is more
    # specific than a package-level canonical default.  Promote it before
    # normalisation so the adapter and the parameter contract see the same
    # stage-local value, including a sweep's declared value list.
    stage_parameters = dict(stage.get("parameters") or {})
    for key, value in stage_parameters.items():
        canonical_key = canonical_stage_parameter_key(str(key))
        if (
            canonical_key
            and canonical_key != key
            and canonical_key not in stage_parameters
            and value not in (None, "")
        ):
            effective[canonical_key] = value
    for key in ("calculation_type", "name", "solver", "application"):
        value = stage.get(key)
        if value not in (None, ""):
            effective[key] = value
    return _normalise_stage_runtime_parameters(effective) if normalize_runtime else effective


def _stage_parameter_contract(
    stage: dict[str, Any],
    effective_parameters: dict[str, Any],
    *,
    consumed_parameters: set[str] | list[str] | tuple[str, ...] | None = None,
    parameter_evidence: dict[str, dict[str, Any]] | None = None,
    generated_files: dict[str, str] | None = None,
    require_consumption: bool = False,
) -> dict[str, Any]:
    """Record merge, adapter-consumption and direct output evidence per parameter."""
    sweep_values = {
        canonical_stage_parameter_key(str(key)): value
        for key, value in ((stage.get("sweep_value") or {}).get("parameters") or {}).items()
        if canonical_stage_parameter_key(str(key))
    }
    declared = {
        key: value for key, value in dict(stage.get("parameters") or {}).items()
        if value not in (None, "", [], {}) and not is_placeholder_value(value)
    }
    # Expanded sweep cases retain the parent stage's value lists for
    # traceability, while the adapter receives the selected scalar in
    # ``sweep_value``.  A per-case contract must validate that selected value,
    # not reject the case because its parent design space was a list.
    declared = {
        key: sweep_values.get(canonical_stage_parameter_key(str(key)), value)
        for key, value in declared.items()
    }
    aliases = {
        "angle_of_attack_deg": "angle_of_attack",
        "alpha_deg": "angle_of_attack",
        "re": "reynolds_number",
        "cfl_max": "max_co",
        "time_step": "delta_t",
        "total_simulation_time_tUinf_over_c": "end_time",
        "extended_simulation_time_tUinf_over_c": "end_time",
    }
    checks: list[dict[str, Any]] = []
    consumption_checks: list[dict[str, Any]] = []
    consumed = {
        canonical_stage_parameter_key(str(key))
        for key in (consumed_parameters or [])
        if canonical_stage_parameter_key(str(key))
    }
    workflow_evidence = {
        canonical_stage_parameter_key(str(key)): value
        for key, value in (parameter_evidence or {}).items()
        if canonical_stage_parameter_key(str(key)) and isinstance(value, dict)
    }
    explicit_keys = {
        canonical_stage_parameter_key(str(key))
        for key in stage.get("explicit_parameter_keys") or declared
        if canonical_stage_parameter_key(str(key))
    }
    if not stage.get("explicit_parameter_keys") and declared.get("structure_variant") == "base_structure":
        explicit_keys.discard("structure_variant")

    def json_contains(value: Any, key: str, expected: Any) -> bool:
        if isinstance(value, dict):
            for nested_key, nested_value in value.items():
                if canonical_stage_parameter_key(str(nested_key)) == key and equivalent(expected, nested_value):
                    return True
                if json_contains(nested_value, key, expected):
                    return True
        elif isinstance(value, list):
            return any(json_contains(item, key, expected) for item in value)
        return False

    def direct_output_paths(key: str, expected: Any) -> list[str]:
        paths: list[str] = []
        for name, content in (generated_files or {}).items():
            text_content = str(content or "")
            if not text_content.strip():
                continue
            lower_name = str(name).lower()
            if lower_name.endswith((".json", ".yaml", ".yml")):
                try:
                    parsed = yaml.safe_load(content)
                except yaml.YAMLError:
                    parsed = None
                if json_contains(parsed, key, expected):
                    paths.append(str(name))
                    continue
            if text_contains_parameter(text_content, lower_name, key, expected):
                paths.append(str(name))
        return paths

    def equivalent(expected: Any, actual: Any) -> bool:
        if actual in (None, "", [], {}):
            return False
        if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
            return math.isclose(float(expected), float(actual), rel_tol=1e-12, abs_tol=1e-12)
        if isinstance(expected, str) and isinstance(actual, (int, float)):
            match = re.fullmatch(
                r"\s*(?:<=|>=|≤|≥|<|>)?\s*([-+]?[0-9]+(?:\.[0-9]+)?)\s*%?\s*",
                expected,
            )
            if match:
                return math.isclose(
                    float(match.group(1)), float(actual), rel_tol=1e-12, abs_tol=1e-12
                )
        if isinstance(expected, str) and isinstance(actual, str):
            return expected.strip() == actual.strip()
        return expected == actual

    def numeric_tokens(value: Any) -> list[float]:
        values: list[float] = []
        for match in re.findall(r"[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?", str(value)):
            try:
                values.append(float(match))
            except ValueError:
                pass
        return values

    def text_contains_parameter(content: str, filename: str, key: str, expected: Any) -> bool:
        canonical = canonical_stage_parameter_key(key)
        if not canonical:
            return False
        expected_numbers = numeric_tokens(expected)
        key_pattern = re.escape(canonical).replace("_", r"[_\s-]*")
        for match in re.finditer(
            rf"(?im)^\s*{key_pattern}\s*(?:=|:)\s*(.+?)\s*$",
            content,
        ):
            value_text = match.group(1).strip()
            if equivalent(expected, value_text):
                return True
            actual_numbers = numeric_tokens(value_text)
            if expected_numbers and actual_numbers and any(
                math.isclose(left, right, rel_tol=1e-9, abs_tol=1e-9)
                for left in expected_numbers
                for right in actual_numbers
            ):
                return True
        if filename.endswith(("poscar", "contcar")) or Path(filename).name.upper() in {"POSCAR", "CONTCAR"}:
            if canonical in {"a", "b", "c", "lattice", "lattice_constant", "cell"}:
                content_numbers = numeric_tokens("\n".join(content.splitlines()[1:5]))
                return bool(
                    expected_numbers
                    and content_numbers
                    and any(
                        math.isclose(left, right, rel_tol=1e-4, abs_tol=1e-4)
                        for left in expected_numbers
                        for right in content_numbers
                    )
                )
        return False

    for key, value in declared.items():
        canonical_key = canonical_stage_parameter_key(key)
        effective_key = aliases.get(key, aliases.get(canonical_key, canonical_key))
        actual = effective_parameters.get(effective_key)
        checks.append({
            "declared_key": key,
            "effective_key": effective_key,
            "declared_value": value,
            "effective_value": actual,
            "status": "pass" if equivalent(value, actual) else "fail",
        })
        if canonical_key not in explicit_keys:
            continue
        output_paths = direct_output_paths(effective_key, actual)
        consumed_by_adapter = effective_key in consumed or canonical_key in consumed
        workflow_consumed = workflow_evidence.get(effective_key) or workflow_evidence.get(canonical_key)
        consumption_checks.append({
            "declared_key": key,
            "effective_key": effective_key,
            "status": "pass" if consumed_by_adapter or output_paths or workflow_consumed else "fail",
            "evidence": (
                {"kind": "adapter_parameter_read", "key": effective_key}
                if consumed_by_adapter
                else (
                    {"kind": "direct_output_value", "paths": output_paths}
                    if output_paths else workflow_consumed
                )
            ),
        })
    propagation_passed = all(item["status"] == "pass" for item in checks)
    consumption_passed = (
        all(item["status"] == "pass" for item in consumption_checks)
        if require_consumption else True
    )
    return {
        "status": "pass" if propagation_passed and consumption_passed else "fail",
        "stage_id": stage.get("id"),
        "calculation_type": stage.get("calculation_type"),
        "declared_parameters": declared,
        "effective_parameters": {
            item["effective_key"]: item["effective_value"] for item in checks
        },
        "propagation_checks": checks,
        "consumption_required": require_consumption,
        "consumed_parameter_keys": sorted(consumed),
        "consumption_checks": consumption_checks,
        "validation_scope": (
            "explicit research-plan parameters must reach the effective stage scope and be read by "
            "the selected adapter or be directly evidenced in a supplied structured output; "
            "adapter-specific output checks are then performed by the package reviewer"
        ),
    }


def _normalised_role(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def _reference_evidence_has_local_asset(parameters: dict[str, Any], aliases: set[str]) -> bool:
    evidence = parameters.get("reference_evidence") or []

    def existing_path(value: Any) -> bool:
        if isinstance(value, list):
            return any(existing_path(item) for item in value)
        if not isinstance(value, dict):
            return False
        for key in (
            "preferred_geometry_file", "saved_path", "downloaded_path", "local_path", "path",
        ):
            raw = value.get(key)
            if raw:
                try:
                    if Path(str(raw)).expanduser().is_file():
                        return True
                except OSError:
                    pass
        return any(existing_path(item) for item in value.values() if isinstance(item, (dict, list)))

    for entry in _as_list(evidence):
        if not isinstance(entry, dict):
            continue
        labels = {
            _normalised_role(entry.get("asset_id")),
            _normalised_role(entry.get("asset_role")),
        } - {""}
        if labels and aliases and not any(
            left == right or left in right or right in left
            for left in labels for right in aliases
        ):
            continue
        if existing_path(entry.get("result") if "result" in entry else entry):
            return True
    return False


def _stage_missing_external_assets(stage: dict[str, Any], parameters: dict[str, Any]) -> list[str]:
    analysis = parameters.get("requirement_analysis")
    if not isinstance(analysis, dict):
        return []
    stage_roles = {
        _normalised_role(value)
        for value in stage.get("required_file_roles") or []
        if _normalised_role(value)
    }
    if not stage_roles:
        return []
    missing: list[str] = []
    for item in analysis.get("required_files") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name_or_role") or item.get("id") or "").strip()
        aliases = {
            _normalised_role(item.get("id")),
            _normalised_role(item.get("name_or_role")),
        } - {""}
        if not aliases or not any(
            left == right or left in right or right in left
            for left in stage_roles for right in aliases
        ):
            continue
        text = " ".join(str(item.get(key) or "") for key in (
            "id", "name_or_role", "asset_role", "format", "reason",
        ))
        # Spatial geometry/mesh is owned by the preceding reviewed mesh step.
        # This check covers reusable non-mesh data that must exist as a file.
        if not re.search(
            r"\b(dataset|data table|lookup table|material propert(?:y|ies)|experimental data|"
            r"reference curve|spectrum|force[- ]?field|basis set|parameter database)\b|"
            r"数据集|数据表|物性|实验数据|参考曲线|光谱|力场|基组|参数库",
            text,
            flags=re.I,
        ):
            continue
        explicit_paths = [
            parameters.get(str(item.get("id") or "")),
            parameters.get(f"{item.get('id')}_path"),
        ]
        available = any(
            value and Path(str(value)).expanduser().is_file()
            for value in explicit_paths
        ) or _reference_evidence_has_local_asset(parameters, aliases)
        if not available:
            missing.append(f"data_asset:{name}")
    return missing


def _stage_declared_missing_inputs(stage: dict[str, Any], parameters: dict[str, Any]) -> list[str]:
    missing = [
        str(item).strip()
        for item in _as_list(stage.get("missing_inputs"))
        if str(item).strip()
    ]
    required_parameters = [
        str(item).strip()
        for item in [
            *_as_list(stage.get("required_parameters")),
            *_as_list((stage.get("parameters") or {}).get("required_parameters")),
        ]
        if str(item).strip()
    ]
    for name in required_parameters:
        value = parameters.get(name)
        if value in (None, "", [], {}) or is_placeholder_value(value):
            missing.append(name)
    for name, value in (stage.get("parameters") or {}).items():
        if name == "required_parameters":
            continue
        if is_placeholder_value(value):
            missing.append(str(name))
    missing.extend(_stage_missing_external_assets(stage, parameters))
    return list(dict.fromkeys(missing))


def _mesh_asset_roles(discipline: str, mesh_type: str) -> list[str]:
    """Describe generated mesh outputs by role, without prescribing a mesher."""
    roles = ["geometry_coordinates", "native_mesh", "mesh_quality_report"]
    if discipline == "cfd":
        roles.append("openfoam_poly_mesh")
    if "gmsh" in str(mesh_type or "").lower():
        # A Gmsh-backed stage has a .geo/.msh provenance asset, not blockMeshDict.
        roles.append("gmsh_geometry_script")
    return roles


_SUPERSCRIPT_NUMBER_MAP = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻", "0123456789+-")


def _normalize_scientific_number_text(value: Any) -> str:
    text = str(value).replace("，", ",")
    text = re.sub(
        r"10([⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻]+)",
        lambda match: "10^" + match.group(1).translate(_SUPERSCRIPT_NUMBER_MAP),
        text,
    )
    text = re.sub(r"(?<=\d)[,_](?=\d)", "", text)
    return re.sub(r"(?<=\d)\s+(?=\d{3}(?:\D|$))", "", text)


def _integer_from_count(value: Any) -> int | None:
    """Parse common cell-count notation without truncating grouped numbers."""
    if isinstance(value, bool) or value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return max(1, int(value))
    text = _normalize_scientific_number_text(value)
    match = re.search(
        r"([-+]?\d+(?:\.\d+)?)\s*"
        r"(?:(?:[x×*]\s*10\s*\^?\s*([-+]?\d+))|(?:[eE]\s*([-+]?\d+)))?\s*"
        r"([kKmM万亿]?)",
        text,
    )
    if not match:
        return None
    try:
        number = float(match.group(1))
        exponent = match.group(2) or match.group(3)
        if exponent is not None:
            number *= 10 ** int(exponent)
        suffix = match.group(4)
        multiplier = {
            "k": 1_000,
            "m": 1_000_000,
            "万": 10_000,
            "亿": 100_000_000,
        }.get(suffix.lower(), 1)
        return max(1, int(number * multiplier))
    except (OverflowError, ValueError):
        return None


def _numeric_requirement(value: Any) -> float | None:
    """Read grouped/scientific plan values, including ``60,000`` and ``6×10⁴``."""
    if isinstance(value, bool) or value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = _normalize_scientific_number_text(value)
    match = re.search(
        r"([-+]?\d+(?:\.\d+)?)\s*"
        r"(?:(?:[x×*]\s*10\s*\^?\s*([-+]?\d+))|(?:[eE]\s*([-+]?\d+)))?",
        text,
    )
    try:
        if not match:
            return None
        number = float(match.group(1))
        exponent = match.group(2) or match.group(3)
        return number * (10 ** int(exponent)) if exponent is not None else number
    except (OverflowError, ValueError):
        return None


def _stage_parameter_value(stages: list[dict[str, Any]], *keys: str) -> Any:
    """Find an explicitly declared parameter without inventing a default."""
    for stage in stages:
        values = stage.get("parameters") or {}
        if not isinstance(values, dict):
            continue
        for key in keys:
            value = values.get(key)
            if value not in (None, "", [], {}) and not is_placeholder_value(value):
                return value
    return None


def _stage_text_numeric_values(
    stages: list[dict[str, Any]],
    patterns: tuple[str, ...],
) -> list[float]:
    """Extract only explicitly labelled numeric requirements from plan prose."""
    values: list[float] = []
    for stage in stages:
        text = _stage_text(stage)
        for pattern in patterns:
            for match in re.finditer(pattern, text, flags=re.I):
                value = _numeric_requirement(match.group(1))
                if value is not None:
                    values.append(float(value))
    return values


def _apply_mesh_stage_requirements(
    parameters: dict[str, Any],
    stages: list[dict[str, Any]],
) -> dict[str, Any]:
    """Promote explicit mesh-stage requirements into mesher controls.

    This is intentionally semantic rather than case-name based.  It handles
    count, domain extent and wall-unit requirements wherever a research plan
    declares them, and keeps the original declaration as an auditable contract.
    """
    convergence_stages = [
        item for item in stages if _stage_kind(item) == "mesh_convergence"
    ]
    stage = next(
        (item for item in stages if _stage_kind(item) == "mesh_generation"),
        None,
    ) or next(
        iter(convergence_stages),
        None,
    )
    if not stage:
        return dict(parameters)
    # Hypothesis selection is no longer a Data concern. All caller-scoped
    # stages are relevant; the mesh-generation stage still anchors ordinary
    # requests while convergence stages share the declared controls.
    relevant_stages = list(stages)
    declared = dict(stage.get("parameters") or {})
    effective = dict(parameters)
    # Preserve every explicit mesh-stage control, irrespective of discipline.
    # Generic aliases below promote common research-plan vocabulary to the
    # stable mesher contract while explicit caller values keep precedence.
    for key, value in declared.items():
        if value not in (None, "", [], {}) and not is_placeholder_value(value):
            effective.setdefault(str(key), copy.deepcopy(value))
    from .geometry_assets import canonical_mesh_controls

    effective = canonical_mesh_controls(effective)

    angle_of_attack = _stage_parameter_value(
        relevant_stages,
        "angle_of_attack", "angle_of_attack_deg", "alpha", "alpha_deg", "aoa",
    )
    if angle_of_attack not in (None, "", [], {}):
        effective.setdefault("angle_of_attack", angle_of_attack)
    reynolds_number = _stage_parameter_value(
        relevant_stages, "reynolds_number", "reynolds", "re", "Re"
    )
    if reynolds_number not in (None, "", [], {}):
        effective.setdefault("reynolds_number", reynolds_number)
    contract: dict[str, Any] = {
        "stage_id": stage.get("id"),
        "declared": declared,
        "applied": {},
        "wall_unit_reference": None,
    }

    def plan_values(*keys: str) -> list[Any]:
        values: list[Any] = []
        # Cross-stage resolution constraints are needed only when constructing
        # a declared convergence reference. Ordinary one-mesh workflows retain
        # their original mesh-stage parameter scope.
        requirement_stages = relevant_stages if convergence_stages else [stage]
        for item in requirement_stages:
            item_parameters = item.get("parameters") or {}
            if not isinstance(item_parameters, dict):
                continue
            for key in keys:
                value = item_parameters.get(key)
                if value not in (None, "", [], {}) and not is_placeholder_value(value):
                    values.append(value)
        return values

    target_cells = next((
        _integer_from_count(declared.get(key))
        for key in ("target_cell_count", "target_cells", "cell_count_2d", "cell_count", "element_count")
        if _integer_from_count(declared.get(key))
    ), None)
    if not target_cells:
        for convergence_stage in convergence_stages:
            levels = _mesh_convergence_levels(
                convergence_stage,
                _integer_from_count(effective.get("target_cell_count")),
            )
            if not levels:
                continue
            reference_level = _mesh_convergence_reference_level(
                convergence_stage,
                levels,
                _integer_from_count(effective.get("target_cell_count")),
            )
            target_cells = next((
                _integer_from_count(level.get("target_cell_count"))
                for level in levels
                if str(level.get("id") or "").casefold() == reference_level.casefold()
            ), None)
            if target_cells:
                contract["reference_level"] = reference_level
                break
    if target_cells:
        effective["target_cell_count"] = target_cells
        contract["applied"]["target_cell_count"] = target_cells

    far_field_values = [
        value for value in (
            _numeric_requirement(raw)
            for raw in plan_values(
                "far_field", "farfield", "far_field_radius", "farfield_radius", "domain_radius"
            )
        )
        if value is not None
    ]
    far_field_values.extend(_stage_text_numeric_values(
        relevant_stages,
        (
            r"(?:far[- ]?field|远场)(?:\s*(?:radius|distance|extent|半径|距离|范围))?"
            r"\s*(?:=|:|≈|~|为)?\s*([0-9]+(?:\.[0-9]+)?)\s*(?:c|chord|弦长)?",
        ),
    ))
    required_far_field = max(far_field_values) if far_field_values else None
    if required_far_field is not None:
        current_far_field = _numeric_requirement(effective.get("far_field")) or 0.0
        effective["far_field"] = max(current_far_field, required_far_field)
        effective["required_far_field"] = required_far_field
        contract["applied"]["required_far_field"] = required_far_field

    y_plus_values = [
        value for value in (
            _numeric_requirement(raw)
            for raw in plan_values(
                "wall_y_plus", "target_y_plus", "y_plus", "yplus", "y_plus_max",
                "wall_y_plus_max",
            )
        )
        if value is not None
    ]
    y_plus_values.extend(_stage_text_numeric_values(
        relevant_stages,
        (
            r"(?:\by\s*\+|\byplus\b|壁面\s*y\s*\+)\s*(?:<=|≤|<|=|:|≈|~)\s*"
            r"([0-9]+(?:\.[0-9]+)?)",
        ),
    ))
    dx_plus_values = [
        value for value in (
            _numeric_requirement(raw)
            for raw in plan_values(
                "streamwise_delta_x_plus", "streamwise_dx_plus", "delta_x_plus",
                "target_delta_x_plus", "dx_plus",
                "delta_x_plus_max", "streamwise_dx_plus_max",
            )
        )
        if value is not None
    ]
    dx_plus_values.extend(_stage_text_numeric_values(
        relevant_stages,
        (
            r"(?:Δ|delta)\s*x\s*(?:\+|⁺|plus)\s*(?:<=|≤|<|=|:|≈|~)\s*"
            r"([0-9]+(?:\.[0-9]+)?)",
        ),
    ))
    target_y_plus = min(y_plus_values) if y_plus_values else None
    target_dx_plus = min(dx_plus_values) if dx_plus_values else None
    if target_y_plus is not None:
        effective["target_wall_y_plus"] = target_y_plus
        effective.setdefault("near_wall_refinement_mode", "boundary_layer")
        effective["lock_near_wall_topology"] = True
        contract["applied"]["target_wall_y_plus"] = target_y_plus
        contract["applied"]["near_wall_refinement_mode"] = effective["near_wall_refinement_mode"]
        contract["applied"]["lock_near_wall_topology"] = True
    if target_dx_plus is not None:
        effective["target_streamwise_delta_x_plus"] = target_dx_plus
        contract["applied"]["target_streamwise_delta_x_plus"] = target_dx_plus

    # A pre-run wall-unit requirement needs a declared reference model.  For a
    # plan that explicitly declares laminar/DNS flow and Reynolds number, use a
    # conservative documented Blasius reference only to size the initial mesh;
    # the post-run wall field remains the final y+ authority.
    reynolds = _numeric_requirement(
        effective.get("reynolds_number")
        or effective.get("re")
        or _stage_parameter_value(stages, "reynolds_number", "re", "Re")
    )
    if (target_y_plus is not None or target_dx_plus is not None) and reynolds and reynolds > 0:
        velocity = _numeric_requirement(
            effective.get("freestream_velocity") or effective.get("inlet_velocity") or effective.get("U_inf")
        ) or 1.0
        chord = _numeric_requirement(effective.get("chord") or effective.get("chord_length")) or 1.0
        nu = _numeric_requirement(effective.get("nu")) or velocity * chord / reynolds
        cf = 1.328 / math.sqrt(reynolds)
        u_tau = velocity * math.sqrt(cf / 2.0)
        viscous_length = nu / max(u_tau, 1e-12)
        reference = {
            "model": "laminar_blasius_reference_for_pre_run_mesh_sizing",
            "reynolds_number": reynolds,
            "reference_velocity": velocity,
            "reference_length": chord,
            "nu": nu,
            "estimated_u_tau": u_tau,
            "viscous_length": viscous_length,
            "post_run_validation_required": True,
        }
        effective["wall_unit_reference"] = reference
        contract["wall_unit_reference"] = reference
        if target_y_plus is not None:
            first_height = max(1e-8, target_y_plus * viscous_length * 0.9)
            existing = _numeric_requirement(effective.get("boundary_layer_first"))
            effective["boundary_layer_first"] = min(existing, first_height) if existing else first_height
            contract["applied"]["boundary_layer_first"] = effective["boundary_layer_first"]
        if target_dx_plus is not None:
            streamwise_size = max(1e-8, target_dx_plus * viscous_length * 0.9)
            existing = _numeric_requirement(effective.get("h_airfoil"))
            effective["h_airfoil"] = min(existing, streamwise_size) if existing else streamwise_size
            contract["applied"]["h_airfoil"] = effective["h_airfoil"]
    elif target_y_plus is not None or target_dx_plus is not None:
        contract["wall_unit_reference"] = {
            "status": "unverified",
            "reason": "no explicit Reynolds number or wall-unit reference was available for pre-run sizing",
        }

    effective["mesh_plan_contract"] = contract
    return effective


def _mesh_convergence_levels(
    stage: dict[str, Any],
    reference_target_count: int | None = None,
) -> list[dict[str, Any]]:
    """Read an explicit, mesher-neutral mesh-convergence level contract."""
    parameters = dict(stage.get("parameters") or {})
    explicit = parameters.get("mesh_levels") or parameters.get("mesh_convergence_levels")
    levels: list[dict[str, Any]] = []
    if isinstance(explicit, list):
        for index, item in enumerate(explicit, 1):
            if not isinstance(item, dict):
                continue
            name = _slugify(str(item.get("id") or item.get("name") or f"level_{index}"), f"level_{index}")
            target = item.get("target_cell_count") or item.get("target_cells")
            target_value = _integer_from_count(target)
            levels.append({"id": name, "target_cell_count": target_value, "parameters": dict(item)})
    if levels:
        return levels

    for key, value in parameters.items():
        match = re.fullmatch(r"([A-Za-z][A-Za-z0-9_-]*)_(?:cell|cells|elements)", str(key))
        if not match:
            continue
        target = _integer_from_count(value)
        if target:
            levels.append({
                "id": _slugify(match.group(1), "level"),
                "target_cell_count": target,
                "parameters": {str(key): value},
            })
    if levels:
        return sorted(levels, key=lambda item: int(item.get("target_cell_count") or 0))

    # Research plans often declare counts in prose/evidence rather than a
    # machine-shaped parameters object (for example 粗网格≈10万，中≈20万，细≈40万).
    # Extract only values explicitly attached to a level and an assignment-like
    # marker, so refinement ratios such as 1x/1.5x/2x are not mistaken for cells.
    stage_contract_text = _stage_text(stage)
    aliases = {
        "coarse": r"(?:coarse|粗(?:网格)?)",
        "medium": r"(?:medium|中(?:等)?(?:网格)?)",
        "fine": r"(?:fine|细(?:网格)?)",
    }
    for level_id, label_pattern in aliases.items():
        match = re.search(
            rf"{label_pattern}\s*(?:mesh|grid|网格)?\s*(?:≈|约|=|＝|~)\s*"
            r"([0-9][0-9,._]*(?:\.[0-9]+)?\s*(?:[kKmM万亿])?)",
            stage_contract_text,
            flags=re.I,
        )
        target_value = _integer_from_count(match.group(1)) if match else None
        if target_value:
            levels.append({
                "id": level_id,
                "target_cell_count": target_value,
                "parameters": {"source": "stage_evidence"},
            })
    if levels:
        return sorted(levels, key=lambda item: int(item.get("target_cell_count") or 0))

    target = parameters.get("target_cell_count")
    target_value = _integer_from_count(target) or _integer_from_count(reference_target_count)
    if target_value:
        requested_level_names = [
            _slugify(str(item), "level")
            for item in explicit or parameters.get("levels") or []
            if not isinstance(item, dict) and str(item).strip()
        ]
        if requested_level_names and set(requested_level_names) >= {"coarse", "medium", "fine"}:
            requested_level_names = ["coarse", "medium", "fine"]
        elif not requested_level_names:
            requested_level_names = ["coarse", "medium", "fine"]
        factors = {"coarse": 0.5, "medium": 1.0, "fine": 2.0}
        return [
            {
                "id": level_id,
                "target_cell_count": max(1, round(target_value * factors.get(level_id, 1.0))),
                "parameters": {"source": "reference_target_count"},
            }
            for level_id in requested_level_names
        ]
    return []


def _gate_guidance_files(stage: dict[str, Any]) -> dict[str, str]:
    """Emit a result-neutral decision contract for a research-plan gate."""
    gate = dict((stage.get("parameters") or {}).get("gate") or {})
    gate_id = str(stage.get("id") or "gate")
    contract = {
        "schema_version": "1.0",
        "stage_id": gate_id,
        "role": "downstream_gate_evaluator_guidance",
        "source_stage_ids": gate.get("source_stage_ids") or stage.get("dependencies") or [],
        "condition": gate.get("condition"),
        "branches": {
            "pass": {
                "instruction": gate.get("pass_action"),
                "next_stage_ids": gate.get("pass_stage_ids") or [],
            },
            "fail": {
                "instruction": gate.get("fallback_action"),
                "next_stage_ids": gate.get("fallback_stage_ids") or [],
            },
        },
        "evaluation_policy": [
            "Run and quality-check every source stage before evaluating this gate.",
            "Use actual experiment/simulation outputs; the data node does not evaluate scientific results.",
            "Record the measured evidence, decision, and selected branch in gate_result.json.",
            "Call the data node with the selected next stage and register gate_result.json as this stage artifact.",
        ],
        "result_artifact": {
            "filename": "gate_result.json",
            "required_fields": [
                "stage_id", "status", "decision", "evidence", "evaluated_at", "selected_next_stage_ids",
            ],
            "allowed_decisions": ["pass", "fail", "indeterminate"],
        },
        "source_evidence": gate.get("evidence"),
    }
    result_schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "required": contract["result_artifact"]["required_fields"],
        "properties": {
            "stage_id": {"const": gate_id},
            "status": {"enum": ["completed", "blocked"]},
            "decision": {"enum": ["pass", "fail", "indeterminate"]},
            "evidence": {"type": ["object", "array"]},
            "evaluated_at": {"type": "string"},
            "selected_next_stage_ids": {
                "type": "array", "items": {"type": "string"},
            },
        },
    }
    markdown = "\n".join([
        f"# Gate {gate_id}",
        "",
        f"- Source stages: {', '.join(contract['source_stage_ids']) or 'not declared'}",
        f"- Condition: {contract['condition'] or 'not declared'}",
        f"- Pass: {contract['branches']['pass']['instruction'] or 'not declared'}",
        f"- Fail/fallback: {contract['branches']['fail']['instruction'] or 'not declared'}",
        "",
        "After the source calculation finishes, evaluate the declared condition from its real outputs,",
        "write `gate_result.json` against `gate_result.schema.json`, and invoke the data node for only",
        "the selected next stage IDs. Do not treat this guidance directory as a solver case.",
        "",
    ])
    return {
        "gate_guidance.json": _json_dumps(contract),
        "gate_result.schema.json": _json_dumps(result_schema),
        "GATE_INSTRUCTIONS.md": markdown,
    }


def _simulation_stage_files(
    discipline: str,
    stages: list[dict[str, Any]],
    parameters: dict[str, Any],
    *,
    mesh_available: bool,
    mesh_convergence_variants: dict[str, list[dict[str, Any]]] | None = None,
) -> tuple[dict[str, str], list[dict[str, Any]]]:
    """Generate independent solver cases and pending contracts per research stage."""
    files: dict[str, str] = {}
    records: list[dict[str, Any]] = []
    root = "stages"
    prior_records: dict[str, dict[str, Any]] = {}
    prior_record_groups: dict[str, list[dict[str, Any]]] = {}
    target_stage_ids = {
        str(value).strip().lower()
        for value in _as_list(
            parameters.get("target_stage_ids") or parameters.get("resume_stage_ids")
        )
        if str(value).strip()
    }
    for stage in _expand_stage_sweeps(stages):
        stage_id = _slugify(str(stage.get("id") or "stage"), "stage").lower()
        sweep_parent = str(stage.get("sweep_parent") or "").strip().lower()
        if target_stage_ids and stage_id not in target_stage_ids and sweep_parent not in target_stage_ids:
            continue
        prefix = _stage_output_prefix(root, stage)
        merged = _approved_stage_parameters(parameters, stage)
        text = _stage_text(stage)
        stage_kind = _stage_kind(stage)
        execution_kind = _stage_execution_kind(stage)
        missing_inputs = _stage_declared_missing_inputs(stage, merged)
        parameter_space_validation = stage.get("parameter_space_validation") or {}
        if parameter_space_validation.get("status") in {
            "needs_sampling_design", "case_count_mismatch",
        }:
            unresolved = [
                str(item.get("parameter") or "range")
                for item in parameter_space_validation.get("unresolved_ranges") or []
                if isinstance(item, dict)
            ]
            missing_inputs.append(
                "parameter-space design: "
                f"explicit={parameter_space_validation.get('explicit_case_count')}, "
                f"expected={parameter_space_validation.get('expected_case_count')}, "
                f"unresolved_ranges={','.join(unresolved) or 'none'}"
            )
        dependency_missing_inputs: list[str] = []
        dependency_requirements: list[dict[str, Any]] = []
        dependency_artifacts = {
            **(
                parameters.get("dependency_artifacts")
                if isinstance(parameters.get("dependency_artifacts"), dict)
                else {}
            ),
            **(
                (stage.get("parameters") or {}).get("dependency_artifacts")
                if isinstance((stage.get("parameters") or {}).get("dependency_artifacts"), dict)
                else {}
            ),
        }
        for dependency in [str(item) for item in stage.get("dependencies") or []]:
            artifact = dependency_artifacts.get(dependency)
            if dependency_artifact_ready(artifact):
                continue
            dependency_records = [
                item for item in [prior_records.get(dependency)]
                if isinstance(item, dict)
            ] or list(prior_record_groups.get(dependency) or [])
            dependency_statuses = sorted({
                str(item.get("status") or "not_ready") for item in dependency_records
            })
            dependency_outputs = list(dict.fromkeys(
                str(output)
                for item in dependency_records
                for output in item.get("expected_outputs") or []
                if str(output).strip()
            ))
            if not dependency_records:
                dependency_missing_inputs.append(f"stage_dependency:{dependency}")
                dependency_state = "stage_record_not_available"
            elif any(item.get("status") != "ready" for item in dependency_records):
                dependency_missing_inputs.append(
                    f"stage_dependency:{dependency}:{','.join(dependency_statuses) or 'not_ready'}"
                )
                dependency_state = "predecessor_not_ready"
            elif any(
                (
                    str(item.get("execution_kind") or "") in {"solver_input", "data_derivation"}
                    or str(item.get("stage_kind") or _stage_kind(item)) == "gate_evaluator"
                )
                and str(item.get("stage_kind") or _stage_kind(item)) not in {
                    "mesh_generation", "mesh_convergence",
                }
                for item in dependency_records
            ):
                dependency_missing_inputs.append(f"result_artifact:{dependency}")
                dependency_state = "result_artifact_required"
            else:
                dependency_state = "artifact_registration_required"
            dependency_requirements.append({
                "dependency_stage_id": dependency,
                "current_state": dependency_state,
                "predecessor_statuses": dependency_statuses,
                "expected_output_roles": dependency_outputs or [
                    "completed predecessor result artifact"
                ],
                "registration_key": f"dependency_artifacts.{dependency}",
                "accepted_artifact": {
                    "status": "ready",
                    "path": "/absolute/path/to/completed/result/file_or_directory",
                    "quality_status": "pass",
                    "required_files": [],
                },
                "validation": [
                    "path exists and is readable as a file or directory",
                    "status declares the predecessor calculation complete",
                    "quality_status is pass and any required_files exist",
                ],
            })
        missing_inputs.extend(dependency_missing_inputs)
        pending_reason = ""
        # Generation state is stage-local.  Without this reset a derivation or
        # blocked stage can inherit the previous stage's solver deck and look
        # complete even though none of those files belong to it.
        generated: dict[str, str] = {}
        solver_files: dict[str, str] = {}
        consumed_parameters: set[str] = set()
        actual_roles = list(stage.get("required_file_roles") or [])
        variant_case_roots: list[str] = []
        if stage_kind == "mesh_generation" and mesh_available:
            actual_roles = _mesh_asset_roles(discipline, str(merged.get("mesh_type") or ""))
            generated = {
                "runtime_assets.json": _json_dumps({
                    "status": "ready",
                    "asset_role": "mesh",
                    "ownership": "stage_local_generated_asset",
                    "validation": "generated asset and universal readiness review passed",
                    "provided_file_roles": actual_roles,
                })
            }
        elif stage_kind == "mesh_generation":
            generated = {}
            pending_reason = "reviewed_mesh_not_available"
            missing_inputs.append("reviewed computational mesh")
        elif stage_kind == "mesh_convergence":
            variants = (mesh_convergence_variants or {}).get(stage_id, [])
            expected_levels = _mesh_convergence_levels(stage)
            family_validation = (
                variants[0].get("family_validation")
                if variants else {}
            ) or {}
            family_validation_status = (
                family_validation.get("status") if family_validation else "pass"
            )
            valid_variants = [
                item for item in variants
                if item.get("status") == "success" and item.get("deliverable_valid") is not False
                and item.get("mesh_review_status") != "fail"
            ]
            convergence_complete = bool(
                expected_levels
                and len(valid_variants) == len(expected_levels)
                and family_validation_status == "pass"
            )
            actual_roles = ["mesh_convergence_variants", "mesh_quality_reports", "native_mesh"]
            if discipline == "cfd":
                actual_roles.append("openfoam_poly_mesh")
            generated = {}
            if convergence_complete:
                generated["mesh_convergence.json"] = _json_dumps({
                    "status": "ready",
                    "asset_role": "mesh_convergence_variants",
                    "levels": [{
                        key: value for key, value in item.items()
                        if key not in {"result", "family_validation"}
                    } for item in variants],
                    "family_validation": family_validation,
                    "reference_level": family_validation.get("reference_level"),
                    "downstream_default_level": family_validation.get("downstream_default_level"),
                    "generation_policy": family_validation.get("generation_policy"),
                    "convergence_decision": family_validation.get("convergence_decision"),
                    "comparison_contract": family_validation.get("comparison_contract"),
                    "validation": (
                        "The reference mesh is generated first. Coarser and finer levels must derive from "
                        "that mesh family, retain geometry/domain/near-wall invariants, and pass review."
                    ),
                })
            # A convergence study is only useful when each accepted mesh
            # level can be run with the same solver contract.  The mesh
            # stage remains the asset owner, while accepted level directories
            # are independently runnable whenever an adapter is registered.
            for item in valid_variants:
                level_id = _slugify(str(item.get("id") or "level"), "level").lower()
                level_parameters = {
                    **merged,
                    "target_cell_count": item.get("selected_target_cell_count")
                    or item.get("target_cell_count"),
                    "actual_cell_count": item.get("actual_cell_count"),
                    "mesh_convergence_level": level_id,
                }
                level_solver_files, level_consumed = _generate_simulation_stage_with_trace(
                    discipline, level_parameters
                )
                if not level_solver_files:
                    continue
                level_prefix = f"mesh_variants/{level_id}"
                generated.update({
                    f"{level_prefix}/{name}": content
                    for name, content in level_solver_files.items()
                })
                generated[f"{level_prefix}/runtime_assets.json"] = _json_dumps({
                    "status": "ready",
                    "asset_role": "mesh_convergence_solver_case",
                    "mesh_level": level_id,
                    "mesh_role": item.get("role"),
                    "downstream_default": item.get("downstream_default", False),
                    "target_cell_count": item.get("target_cell_count"),
                    "selected_target_cell_count": item.get("selected_target_cell_count"),
                    "actual_cell_count": item.get("actual_cell_count"),
                    "validation": (
                        "mesh quality and convergence-family reviews passed before solver input was emitted"
                    ),
                })
                generated[f"{level_prefix}/parameter_contract.json"] = _json_dumps(
                    _stage_parameter_contract(
                        stage,
                        level_parameters,
                        consumed_parameters=level_consumed,
                        generated_files=level_solver_files,
                    )
                )
                generated[f"{level_prefix}/case.foam"] = "// OpenFOAM case marker\n"
                variant_case_roots.append(f"{prefix}/{level_prefix}")
            if variant_case_roots:
                actual_roles.append("solver_input_deck_per_convergence_level")
            if not convergence_complete:
                pending_reason = "mesh_convergence_variants_not_available"
                missing_inputs.append("reviewed mesh-convergence variants")
        elif stage_kind == "gate_evaluator":
            actual_roles = ["gate_guidance", "gate_result_contract"]
            generated = _gate_guidance_files(stage)
        elif execution_kind == "data_derivation" or re.search(
            r"\b(post[- ]?process|analyse results?|analyze results?|extract results?|"
            r"compute (?:coefficients?|metrics?))\b|后处理|结果分析|提取结果",
            text,
            flags=re.I,
        ):
            pending_reason = "requires_downstream_result_artifact"
            missing_inputs.append("downstream simulation/result artifact")
        elif discipline in {"cfd", "heat_transfer"} and is_xfoil_stage(stage):
            generated, consumed_parameters, xfoil_missing = _xfoil_preprocessing_files(merged)
            # XFOIL is a solver-input stage too, but its deck is not an
            # OpenFOAM deck.  Keeping it in the stage file contract makes the
            # planner, package layout and reviewer agree on one definition of
            # a complete pre-analysis case.
            solver_files = dict(generated)
            if xfoil_missing:
                pending_reason = "declared_preprocessor_inputs_missing"
                missing_inputs.extend(xfoil_missing)

        else:
            generated = {}
        if execution_kind == "solver_input" and not generated and not is_xfoil_stage(stage):
            solver_files, consumed_parameters = _generate_simulation_stage_with_trace(
                discipline, merged
            )
            generated = dict(solver_files)
            if not solver_files and not missing_inputs:
                pending_reason = "solver_input_adapter_not_available"
                missing_inputs.append("registered solver-input adapter for this stage")
        if stage.get("entry_gates"):
            generated["entry_gate_guidance.json"] = _json_dumps({
                "stage_id": stage_id,
                "policy": "Evaluate every entry gate from completed predecessor results before running this stage.",
                "gates": stage.get("entry_gates"),
            })
        if dependency_missing_inputs and not pending_reason:
            pending_reason = "requires_stage_dependency_artifact"

        parameter_contract = _stage_parameter_contract(
            stage,
            merged,
            consumed_parameters=consumed_parameters,
            parameter_evidence={
                str(key): {
                    "kind": "parameter_space_condition_identity",
                    "value": value,
                }
                for key, value in (
                    (stage.get("sweep_value") or {}).get("parameters") or {}
                ).items()
            },
            generated_files=solver_files,
            require_consumption=bool(solver_files) and execution_kind == "solver_input",
        )
        if solver_files and parameter_contract["status"] != "pass":
            unconsumed = [
                str(item.get("declared_key") or "")
                for item in parameter_contract.get("consumption_checks") or []
                if item.get("status") != "pass"
            ]
            if unconsumed:
                missing_inputs.append(
                    "unconsumed research-plan parameters: " + ", ".join(unconsumed)
                )
                if not pending_reason:
                    pending_reason = "plan_parameters_not_consumed_by_adapter"

        if missing_inputs:
            deferred = (
                execution_kind == "data_derivation"
                or pending_reason == "requires_downstream_result_artifact"
                or bool(dependency_missing_inputs)
            )
            contract = {
                "status": "deferred_dependency" if deferred else "blocked_missing_inputs",
                "stage_id": stage_id,
                "calculation_type": stage.get("calculation_type"),
                "reason": pending_reason or "declared_stage_inputs_missing",
                "missing_inputs": list(dict.fromkeys(missing_inputs)),
                "available_parameters": {
                    key: value for key, value in (stage.get("parameters") or {}).items()
                    if value not in (None, "", [], {}) and not is_placeholder_value(value)
                },
                "planned_solver_files": sorted(
                    solver_files.keys()
                ) if execution_kind == "solver_input" else [],
                "parameter_space": parameter_space_validation or None,
                "dependency_requirements": dependency_requirements,
                "resume": {
                    "action": "invoke the data node for this target stage after registering completed predecessor artifacts",
                    "preserve_ready_stages": True,
                    "search_policy": "search only the named reusable data asset; use HITL for user-owned values",
                    "callable_node": "data",
                    "request": {
                        "mode": "resume_dependent_stages",
                        "target_stage_ids": [str(stage.get("sweep_parent") or stage_id)],
                        "dependency_artifacts": {
                            item["dependency_stage_id"]: item["accepted_artifact"]
                            for item in dependency_requirements
                        },
                    },
                },
            }
            if solver_files:
                generated["parameter_contract.json"] = _json_dumps(parameter_contract)
            # Keep a stage-local recovery contract as well as the consolidated
            # handoff.  The local file makes every planned step visible and
            # independently reviewable; the grouped handoff remains the batch
            # resume interface for downstream nodes.
            if stage_kind != "gate_evaluator":
                generated["input.pending.json"] = _json_dumps(contract)
            status = contract["status"]
        else:
            if mesh_available:
                generated["runtime_assets.json"] = _json_dumps({
                    "status": "ready",
                    "asset_role": "mesh",
                    "ownership": "stage_local_generated_asset",
                    "validation": "generated asset quality gate must pass before this case is executed",
                })
            generated["parameter_contract.json"] = _json_dumps(parameter_contract)
            status = "ready"

        generated_files = []
        for name, content in generated.items():
            path = f"{prefix}/{name}"
            files[path] = content
            generated_files.append(path)
        records.append({
            "id": stage_id,
            "name": stage.get("name"),
            "calculation_type": stage.get("calculation_type"),
            "parameters": stage.get("parameters") or {},
            "explicit_parameter_keys": stage.get("explicit_parameter_keys") or [],
            "effective_parameters": merged,
            "dependencies": stage.get("dependencies") or [],
            "dependency_requirements": dependency_requirements,
            "expected_outputs": stage.get("expected_outputs") or [],
            "required_file_roles": actual_roles,
            "declared_file_roles": stage.get("required_file_roles") or [],
            "generated_files": sorted(generated_files),
            "expected_solver_files": sorted(
                solver_files.keys()
            ) if execution_kind == "solver_input" else [],
            "case_root": prefix,
            "variant_case_roots": variant_case_roots,
            "status": status,
            "missing_inputs": list(dict.fromkeys(missing_inputs)),
            "execution_kind": execution_kind,
            "stage_kind": stage_kind,
            "entry_gates": stage.get("entry_gates") or [],
            "sweep_parent": stage.get("sweep_parent"),
            "sweep_case_id": stage.get("sweep_case_id"),
            "sweep_case_index": stage.get("sweep_case_index"),
            "sweep_value": stage.get("sweep_value"),
            "parameter_space_validation": parameter_space_validation or None,
            "asset_variant_id": _slugify(
                str((stage.get("parameters") or {}).get("geometry_variant") or ""),
                "",
            ).lower() or None,
        })
        prior_records[stage_id] = records[-1]
        if stage.get("sweep_parent"):
            prior_record_groups.setdefault(str(stage["sweep_parent"]), []).append(records[-1])
    _write_sweep_case_indexes(files, records, root)
    return files, records


def _material_stage_files(
    solver: str,
    stages: list[dict[str, Any]],
    parameters: dict[str, Any],
    shared_files: dict[str, str],
    task_context: str = "",
    potcar_source: dict[str, Any] | None = None,
) -> tuple[dict[str, str], list[dict[str, Any]]]:
    files: dict[str, str] = {}
    records: list[dict[str, Any]] = []
    stage_root = "stages"
    expanded_stages = _expand_stage_sweeps(stages)
    accepted_stages: list[dict[str, Any]] = []
    for stage in expanded_stages:
        prefix = _stage_output_prefix(stage_root, stage)
        stage_parameters = _approved_stage_parameters(parameters, stage)
        generated: dict[str, str] = {}
        consumed_parameters: set[str] = set()
        missing_inputs: list[str] = []
        structure_generation: dict[str, Any] | None = None
        structure_contract: dict[str, Any] | None = None
        if solver == "vasp":
            stage_parameters = {**stage_parameters, **_vasp_stage_parameters(stage)}
            generated, consumed_parameters = _generate_with_parameter_trace(
                _vasp_files, stage_parameters, potcar_source
            )
            supplied = stage.get("input_files") or stage.get("files") or {}
            supplied_has_poscar = isinstance(supplied, dict) and "POSCAR" in supplied
            if isinstance(supplied, dict):
                generated.update({
                    str(name): str(content)
                    for name, content in supplied.items()
                    if str(name).strip() and str(content).strip()
                })
            generated.pop("VASP_INPUTS_README.md", None)
            stage_text = _stage_text(stage)
            evidence_text = json.dumps(stage.get("evidence") or [], ensure_ascii=False, default=str)
            bader_site_structure = bool(
                re.search(r"\bbader\b", stage_text, flags=re.I)
                and re.search(r"adsorption|intercalat|吸附|嵌入|插层", evidence_text, flags=re.I)
            )
            distinct_structure = (
                structure_variant(stage) != "base_structure"
                or bool(re.search(
                r"\b(supercell|adsorption|intercalat(?:e|ed|ing|ion)?|composition|strain|distort|"
                r"control structure|neb|endpoint|initial state|final state|defect|snapshot|trajectory|slab|"
                r"surface (?:model|structure|geometry)|interface (?:model|structure|geometry))\b|"
                r"超胞|吸附|嵌入|插层|组分|应变|畸变|控制结构|端点|初态|终态|缺陷|表面|界面",
                stage_text,
                flags=re.I,
                ))
                or bader_site_structure
            )
            placement_or_endpoint_required = bool(re.search(
                r"\b(adsorption|intercalat(?:e|ed|ing|ion)?|control structure|neb|endpoint|"
                r"initial state|final state|defect|snapshot|trajectory)\b|"
                r"吸附|嵌入|插层|控制结构|端点|初态|终态|缺陷|快照|轨迹",
                stage_text,
                flags=re.I,
            )) or bader_site_structure
            sweep_value = stage.get("sweep_value") or {}
            zero_composition_reference = (
                str(sweep_value.get("parameter") or "").lower()
                in {"x", "composition", "concentration"}
                and sweep_value.get("value") in {0, 0.0, "0", "0.0"}
            )
            if zero_composition_reference:
                distinct_structure = False
            if distinct_structure and not supplied_has_poscar:
                generated.pop("POSCAR", None)
            transformed_poscar = (
                _transform_poscar_for_stage(shared_files.get("POSCAR", ""), stage)
                if not supplied_has_poscar and "POSCAR" in shared_files
                else None
            )
            if transformed_poscar:
                generated["POSCAR"] = transformed_poscar
                if not placement_or_endpoint_required:
                    distinct_structure = False
                    structure_generation = {
                        "method": "deterministic_lattice_or_supercell_transform",
                        "confidence": "derived",
                        "requires_relaxation": bool(_strain_from_stage(stage)),
                    }
            if distinct_structure and not supplied_has_poscar and "POSCAR" in shared_files:
                heuristic_files, heuristic_provenance = _heuristic_stage_structures(
                    shared_files["POSCAR"],
                    stage,
                    task_context,
                )
                if heuristic_files:
                    generated.update(heuristic_files)
                    structure_generation = heuristic_provenance
                    distinct_structure = False
            if distinct_structure and not supplied_has_poscar:
                generated.pop("POSCAR", None)
                if stage.get("parameters", {}).get("strain_percent") is not None:
                    missing_inputs.append("strain_axis and stage-specific POSCAR")
                elif re.search(r"snapshot|trajectory|快照|轨迹", stage_text, flags=re.I):
                    missing_inputs.append("AIMD snapshot structure from downstream simulation output")
                elif re.search(r"\bneb\b|endpoint|initial state|final state|端点|初态|终态", stage_text, flags=re.I):
                    missing_inputs.append("NEB endpoint structures/POSCAR images")
                elif re.search(r"adsorption|吸附", stage_text, flags=re.I):
                    missing_inputs.append("adsorption site coordinates/POSCAR")
                elif re.search(r"intercalat|composition|concentration|嵌入|插层|组分|浓度", stage_text, flags=re.I):
                    missing_inputs.append("inserted-species coordinates/POSCAR")
                else:
                    missing_inputs.append("stage-specific structure/POSCAR")
            elif (
                "POSCAR" not in generated
                and "POSCAR" in shared_files
                and structure_generation is None
            ):
                generated["POSCAR"] = shared_files["POSCAR"]
            structure_contract = _upstream_structure_contract(
                stage,
                accepted_stages,
                f"{prefix}/POSCAR",
            )
            if structure_contract:
                generated.pop("POSCAR", None)
                contract_name = (
                    "derivation.pending.json"
                    if structure_contract.get("execution_kind") == "data_derivation"
                    else "structure.pending.json"
                )
                generated[contract_name] = _json_dumps(structure_contract)
                missing_inputs = [
                    item for item in missing_inputs
                    if not re.search(r"snapshot|trajectory|快照|轨迹", item, flags=re.I)
                ]
            elif (
                "POSCAR" in generated
                and _stable_structure_required(stage, structure_generation)
                and _calculation_stage_family(stage) != "relaxation"
            ):
                candidate_poscar = generated.pop("POSCAR")
                relaxation_parameters = {
                    **stage_parameters,
                    "calculation_type": "structure relaxation prerequisite",
                    "poscar_text": candidate_poscar,
                    "incar_parameters": _conservative_predecessor_incar(
                        stage,
                        stage_parameters,
                        structure_generation,
                    ),
                }
                precursor = _vasp_files(relaxation_parameters, potcar_source)
                precursor.pop("VASP_INPUTS_README.md", None)
                for name, content in precursor.items():
                    generated[f"predecessor/{name}"] = content
                structure_contract = {
                    "status": "deferred_dependency",
                    "target": f"{prefix}/POSCAR",
                    "source_stages": [f"{stage['id']}__predecessor"],
                    "source_artifact_role": "converged_structure",
                    "source_file_patterns": [
                        "predecessor/converged_structure.*",
                        "predecessor/final_structure.*",
                        "predecessor/optimized_structure.*",
                        "predecessor/relaxed_structure.*",
                        "predecessor/CONTCAR",
                    ],
                    "transform": "promote_converged_structure_to_stage_input",
                    "validation": [
                        "predecessor structure-generation run reached the approved convergence criteria",
                        "source structure is readable and structurally valid",
                        "species order and atom count are preserved",
                    ],
                    "execution_kind": "solver_input",
                }
                generated["structure.pending.json"] = _json_dumps(structure_contract)
            if structure_contract and structure_contract.get("execution_kind") == "data_derivation":
                for solver_file in ("INCAR", "KPOINTS", "POTCAR"):
                    generated.pop(solver_file, None)
            generated_poscar = next(
                (
                    content for name, content in generated.items()
                    if Path(name).name.upper() == "POSCAR"
                ),
                shared_files.get("POSCAR", "") if structure_contract else "",
            )
            generated_species = _poscar_species_from_text(generated_poscar)
            if (
                generated_species
                and not (
                    structure_contract
                    and structure_contract.get("execution_kind") == "data_derivation"
                )
            ):
                potcar, potcar_provenance = _assemble_potcar(
                    generated_species,
                    potcar_source,
                    stage_parameters,
                )
                if potcar is not None:
                    generated["POTCAR"] = potcar
                else:
                    generated.pop("POTCAR", None)
                    missing_inputs.append(
                        "authorized pseudopotential library entries: "
                        + ", ".join(
                            potcar_provenance.get("missing_variants")
                            or generated_species
                        )
                    )
        else:
            supplied = (
                stage.get("input_files")
                or stage.get("files")
                or stage_parameters.get("input_files")
                or stage_parameters.get("material_input_files")
                or {}
            )
            if isinstance(supplied, dict):
                generated = {
                    str(name): str(content)
                    for name, content in supplied.items()
                    if str(name).strip() and str(content).strip()
                }
            if not generated:
                missing_inputs.append("stage-specific solver input files")
        generated = {
            name: content for name, content in generated.items()
            if Path(name).name.lower() not in {"readme", "readme.md", "vasp_inputs_readme.md"}
        }
        parameter_contract = _stage_parameter_contract(
            stage,
            stage_parameters,
            consumed_parameters=consumed_parameters,
            generated_files=generated,
            require_consumption=bool(generated),
        )
        if generated and parameter_contract["status"] != "pass":
            unconsumed = [
                str(item.get("declared_key") or "")
                for item in parameter_contract.get("consumption_checks") or []
                if item.get("status") != "pass"
            ]
            if unconsumed:
                missing_inputs.append(
                    "unconsumed research-plan parameters: " + ", ".join(unconsumed)
                )
        if not missing_inputs and not structure_contract:
            generated["parameter_contract.json"] = _json_dumps(parameter_contract)
        for name, content in generated.items():
            files[f"{prefix}/{name}"] = content
        deferred_dependency = bool(structure_contract)
        candidate_generated = bool(
            not missing_inputs and not deferred_dependency
            and structure_generation
            and structure_generation.get("confidence") in {"heuristic", "provisional"}
        )
        records.append({
            "id": stage["id"],
            "name": stage.get("name"),
            "calculation_type": stage.get("calculation_type"),
            "parameters": stage.get("parameters") or {},
            "explicit_parameter_keys": stage.get("explicit_parameter_keys") or [],
            "effective_parameters": stage_parameters,
            "dependencies": stage.get("dependencies") or [],
            "required_file_roles": stage.get("required_file_roles") or [],
            "generated_files": sorted(f"{prefix}/{name}" for name in generated),
            "status": (
                "deferred_dependency"
                if deferred_dependency
                else ("blocked" if missing_inputs else ("candidate_generated" if candidate_generated else "ready"))
            ),
            "missing_inputs": missing_inputs,
            "structure_generation": structure_generation,
            "structure_contract": structure_contract,
            "execution_kind": (
                structure_contract.get("execution_kind")
                if structure_contract
                else "solver_input"
            ),
            "sweep_parent": stage.get("sweep_parent"),
            "sweep_case_id": stage.get("sweep_case_id"),
            "sweep_case_index": stage.get("sweep_case_index"),
            "sweep_value": stage.get("sweep_value"),
            "case_root": prefix,
        })
        accepted_stages.append(stage)
    _write_sweep_case_indexes(files, records, stage_root)
    return files, records


def _stage_record_root(record: dict[str, Any]) -> str:
    for name in record.get("generated_files") or []:
        path = str(name)
        for suffix in (
            "/INCAR", "/KPOINTS", "/POTCAR", "/POSCAR",
            "/structure.pending.json", "/derivation.pending.json",
        ):
            if path.endswith(suffix):
                return path[: -len(suffix)]
        if path.endswith("/predecessor/INCAR"):
            return path[: -len("/predecessor/INCAR")]
    stage_id = str(record.get("id") or "stage").strip()
    sweep_parent = str(record.get("sweep_parent") or "").strip()
    sweep_case_id = str(record.get("sweep_case_id") or "").strip()
    root = "stages"
    if sweep_parent:
        return (
            f"{root}/{_slugify(sweep_parent, 'stage').lower()}/cases/"
            f"{_slugify(sweep_case_id or stage_id, 'case').lower()}"
        )
    return f"{root}/{_slugify(stage_id, 'stage')}"


def _record_generated_file(record: dict[str, Any], name: str) -> None:
    generated = [str(item) for item in record.get("generated_files") or []]
    if name not in generated:
        generated.append(name)
        record["generated_files"] = sorted(generated)


def _first_valid_poscar_for_root(files: dict[str, str], root: str, base_poscar: str = "") -> tuple[str, str]:
    candidates = [
        f"{root}/POSCAR",
        f"{root}/predecessor/POSCAR",
    ]
    candidates.extend(
        name for name in sorted(files)
        if name.startswith(root + "/") and Path(name).name.upper() == "POSCAR"
    )
    for name in candidates:
        content = str(files.get(name) or "").strip()
        if content and _poscar_species_from_text(content):
            return content.rstrip() + "\n", name
    if str(base_poscar or "").strip() and _poscar_species_from_text(base_poscar):
        return str(base_poscar).rstrip() + "\n", "base_structure/POSCAR"
    return "", ""


def _stage_missing_structure_terms(record: dict[str, Any]) -> list[str]:
    return [
        str(item)
        for item in record.get("missing_inputs") or []
        if re.search(r"POSCAR|structure|coordinates|endpoint|snapshot|trajectory|结构|坐标|端点|快照|轨迹", str(item), re.I)
    ]


def _refresh_workflow_manifest(
    files: dict[str, str],
    defaults: dict[str, Any],
    stage_records: list[dict[str, Any]],
    deferred_stage_resume: dict[str, Any] | None = None,
) -> None:
    if not stage_records:
        return
    manifest = {
        "solver": defaults["solver_family"],
        "stage_count": len(stage_records),
        "ready_stage_count": sum(item.get("status") == "ready" for item in stage_records),
        "candidate_generated_stage_count": sum(
            item.get("status") == "candidate_generated" for item in stage_records
        ),
        "deferred_dependency_stage_count": sum(
            item.get("status") == "deferred_dependency" for item in stage_records
        ),
        "blocked_stage_count": sum(
            str(item.get("status") or "").startswith("blocked")
            for item in stage_records
        ),
        "stages": stage_records,
    }
    if deferred_stage_resume and deferred_stage_resume.get("requests"):
        manifest["deferred_stage_resume"] = deferred_stage_resume
    files["workflow_manifest.json"] = _json_dumps(manifest)


def _augment_deferred_stage_resumes(
    files: dict[str, str],
    stage_records: list[dict[str, Any]],
    *,
    package_dir: Path,
) -> dict[str, Any]:
    """Create a node-callable resume contract for every unresolved dependency."""
    by_id = {
        str(record.get("id") or "").casefold(): record
        for record in stage_records
        if str(record.get("id") or "")
    }
    by_parent: dict[str, list[dict[str, Any]]] = {}
    for record in stage_records:
        parent = str(record.get("sweep_parent") or "")
        if parent:
            by_parent.setdefault(parent, []).append(record)

    grouped: dict[str, dict[str, Any]] = {}
    for record in stage_records:
        dependencies = [
            str(item).strip().casefold()
            for item in record.get("dependencies") or []
            if str(item).strip()
        ]
        if not dependencies:
            continue
        effective = record.get("effective_parameters") or {}
        supplied = effective.get("dependency_artifacts") if isinstance(effective, dict) else {}
        supplied = supplied if isinstance(supplied, dict) else {}
        requirements = list(record.get("dependency_requirements") or [])
        known_requirement_ids = {
            str(item.get("dependency_stage_id") or "")
            for item in requirements if isinstance(item, dict)
        }
        missing_dependency_terms: list[str] = []
        for dependency in dependencies:
            if dependency_artifact_ready(supplied.get(dependency)):
                continue
            candidates = [by_id[dependency]] if dependency in by_id else list(by_parent.get(dependency) or [])
            if candidates and all(item.get("status") == "ready" for item in candidates):
                produces_result = any(produces_runtime_result(item) for item in candidates)
                if not produces_result:
                    continue
                current_state = "result_artifact_required"
                missing_term = f"result_artifact:{dependency}"
            elif candidates:
                current_state = "predecessor_not_ready"
                statuses = sorted({str(item.get("status") or "not_ready") for item in candidates})
                missing_term = f"stage_dependency:{dependency}:{','.join(statuses)}"
            else:
                current_state = "stage_record_not_available"
                missing_term = f"stage_dependency:{dependency}"
            missing_dependency_terms.append(missing_term)
            if dependency not in known_requirement_ids:
                expected_outputs = list(dict.fromkeys(
                    str(output)
                    for item in candidates
                    for output in item.get("expected_outputs") or []
                    if str(output).strip()
                ))
                requirements.append({
                    "dependency_stage_id": dependency,
                    "current_state": current_state,
                    "predecessor_statuses": sorted({
                        str(item.get("status") or "not_ready") for item in candidates
                    }),
                    "expected_output_roles": expected_outputs or [
                        "completed predecessor result artifact"
                    ],
                    "registration_key": f"dependency_artifacts.{dependency}",
                    "accepted_artifact": {
                        "status": "ready",
                        "path": "/absolute/path/to/completed/result/file_or_directory",
                        "quality_status": "pass",
                        "required_files": [],
                    },
                    "validation": [
                        "path exists and is readable as a file or directory",
                        "status declares the predecessor calculation complete",
                        "quality_status is pass and any required_files exist",
                    ],
                })
        # ``dependency_requirements`` also records already available asset
        # provenance.  Only unresolved dependencies need a deferred handoff;
        # otherwise a ready mesh/convergence stage is incorrectly downgraded
        # merely because its provenance was recorded.
        if not missing_dependency_terms:
            continue

        record["dependency_requirements"] = requirements
        record["missing_inputs"] = list(dict.fromkeys([
            *(record.get("missing_inputs") or []),
            *missing_dependency_terms,
        ]))
        if record.get("status") in {"ready", "candidate_generated"}:
            record["status"] = "deferred_dependency"
        target_id = str(record.get("sweep_parent") or record.get("id") or "")
        request = {
            "mode": "resume_dependent_stages",
            "source_manifest_path": str((package_dir / "manifest.json").resolve()),
            "target_stage_ids": [target_id],
            "dependency_artifacts": {
                item["dependency_stage_id"]: item["accepted_artifact"]
                for item in requirements
                if isinstance(item, dict) and item.get("dependency_stage_id")
            },
        }
        record["resume_request"] = request
        case_root = _stage_case_root(record)
        # Keep the stage-local pending contract. It explains the exact blocker
        # beside that stage; the workflow manifest also aggregates all resume
        # requests for downstream orchestration.

        group = grouped.setdefault(target_id, {
            "target_stage_ids": [target_id],
            "stage_case_ids": [],
            "remaining_blockers": [],
            "dependency_artifacts": {},
        })
        group["stage_case_ids"].append(str(record.get("id") or ""))
        group["remaining_blockers"].extend(record.get("missing_inputs") or [])
        group["dependency_artifacts"].update(request["dependency_artifacts"])

    requests = []
    for target_id, group in grouped.items():
        requests.append({
            "callable_node": "data",
            "when": "after every listed predecessor artifact passes its downstream quality gate",
            "request": {
                "mode": "resume_dependent_stages",
                "source_manifest_path": str((package_dir / "manifest.json").resolve()),
                "target_stage_ids": [target_id],
                "dependency_artifacts": group["dependency_artifacts"],
            },
            "stage_case_ids": sorted(set(group["stage_case_ids"])),
            "remaining_blockers": sorted(set(group["remaining_blockers"])),
        })
    return {
        "schema_version": "1.0",
        "status": "resume_available" if requests else "no_deferred_dependencies",
        "source_manifest_path": str((package_dir / "manifest.json").resolve()),
        "execution_policy": (
            "The downstream node runs ready cases, registers completed result artifacts, then calls "
            "the data node with the matching request. target_stage_ids limits regeneration to the "
            "newly unlocked stage family. Existing ready cases remain authoritative."
        ),
        "requests": requests,
    }


def _stage_case_root(record: dict[str, Any]) -> str:
    explicit = str(record.get("case_root") or "").strip()
    if explicit:
        return explicit
    generated = [str(path) for path in record.get("generated_files") or [] if str(path).strip()]
    if not generated:
        return ""
    return str(Path(generated[0]).parent)


def _repair_vasp_stage_inputs(
    files: dict[str, str],
    stage_records: list[dict[str, Any]],
    *,
    base_poscar: str,
    potcar_source: dict[str, Any] | None,
    parameters: dict[str, Any],
    target_stage_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Repair deterministic, local VASP package defects found by review.

    This does not invent a new research plan. It only fills missing per-stage
    input decks from already generated structures, predecessor structures, or
    the shared base structure, then reassembles POTCAR from the authorized local
    library when available.
    """
    repairs: list[dict[str, Any]] = []
    for record in stage_records:
        if not isinstance(record, dict):
            continue
        if target_stage_ids is not None and str(record.get("id") or "") not in target_stage_ids:
            continue
        root = _stage_record_root(record)
        execution_kind = str(record.get("execution_kind") or "solver_input")
        if execution_kind == "data_derivation":
            continue
        stage_parameters = {
            **parameters,
            **dict(record.get("effective_parameters") or {}),
        }
        if not record.get("effective_parameters"):
            stage_parameters = _approved_stage_parameters(
                parameters,
                {
                    "name": record.get("name"),
                    "calculation_type": record.get("calculation_type"),
                    "parameters": record.get("parameters") or {},
                },
                normalize_runtime=False,
            )
        stage_parameters = {
            **stage_parameters,
            **_vasp_stage_parameters({
                "id": record.get("id"),
                "name": record.get("name"),
                "calculation_type": record.get("calculation_type"),
                "parameters": record.get("parameters") or {},
                "sweep_value": record.get("sweep_value"),
            }),
        }
        template, consumed_parameters = _generate_with_parameter_trace(
            _vasp_files, stage_parameters, potcar_source
        )
        template.pop("VASP_INPUTS_README.md", None)
        for solver_name in ("INCAR", "KPOINTS"):
            target = f"{root}/{solver_name}"
            if not str(files.get(target) or "").strip() and str(template.get(solver_name) or "").strip():
                files[target] = template[solver_name]
                _record_generated_file(record, target)
                repairs.append({
                    "stage_id": record.get("id"),
                    "action": f"generated_missing_{solver_name.lower()}",
                    "file": target,
                    "source": "stage_parameters_and_conservative_defaults",
                })

        poscar_name = f"{root}/POSCAR"
        if (
            not str(files.get(poscar_name) or "").strip()
            and str(record.get("status") or "") != "deferred_dependency"
        ):
            source_poscar, source_name = _first_valid_poscar_for_root(files, root, base_poscar)
            if source_poscar:
                files[poscar_name] = source_poscar
                _record_generated_file(record, poscar_name)
                missing_structure = _stage_missing_structure_terms(record)
                record["missing_inputs"] = [
                    item for item in record.get("missing_inputs") or []
                    if item not in missing_structure
                ]
                record["structure_generation"] = {
                    **(record.get("structure_generation") or {}),
                    "method": "review_guided_local_structure_completion",
                    "source": source_name,
                    "confidence": (
                        "derived" if source_name != "base_structure/POSCAR" else "provisional"
                    ),
                    "requires_relaxation": bool(
                        record.get("structure_contract")
                        or re.search(r"aimd|molecular dynamics|neb|snapshot|trajectory", " ".join([
                            str(record.get("name") or ""),
                            str(record.get("calculation_type") or ""),
                        ]), flags=re.I)
                    ),
                }
                if record.get("structure_contract"):
                    record["prior_structure_contract"] = record.pop("structure_contract")
                for pending_name in ("structure.pending.json", "derivation.pending.json"):
                    files.pop(f"{root}/{pending_name}", None)
                    record["generated_files"] = [
                        name for name in record.get("generated_files") or []
                        if name != f"{root}/{pending_name}"
                    ]
                if record.get("status") in {"blocked", "deferred_dependency"}:
                    record["status"] = "candidate_generated"
                repairs.append({
                    "stage_id": record.get("id"),
                    "action": "generated_missing_poscar",
                    "file": poscar_name,
                    "source": source_name,
                })

        structure_poscar, structure_source = _first_valid_poscar_for_root(files, root, base_poscar)
        species = _poscar_species_from_text(structure_poscar)
        if species:
            potcar, provenance = _assemble_potcar(species, potcar_source, stage_parameters)
            potcar_name = f"{root}/POTCAR"
            if potcar is not None and files.get(potcar_name) != potcar:
                files[potcar_name] = potcar
                _record_generated_file(record, potcar_name)
                repairs.append({
                    "stage_id": record.get("id"),
                    "action": "assembled_potcar",
                    "file": potcar_name,
                    "species": species,
                    "source": provenance.get("source"),
                    "structure_source": structure_source,
                })
            elif potcar is None:
                missing = ", ".join(provenance.get("missing_variants") or species)
                if missing:
                    missing_text = f"authorized pseudopotential library entries: {missing}"
                    if missing_text not in record.get("missing_inputs", []):
                        record.setdefault("missing_inputs", []).append(missing_text)
        if not record.get("missing_inputs") and record.get("status") == "blocked":
            record["status"] = "candidate_generated" if record.get("structure_generation") else "ready"
        if not record.get("missing_inputs") and not record.get("structure_contract"):
            stage_descriptor = {
                "id": record.get("id"),
                "calculation_type": record.get("calculation_type"),
                "parameters": record.get("parameters") or {},
                "explicit_parameter_keys": record.get("explicit_parameter_keys") or [],
            }
            contract_name = f"{root}/parameter_contract.json"
            local_files = {
                path[len(root) + 1:]: content
                for path, content in files.items()
                if path.startswith(root + "/") and not path.endswith("parameter_contract.json")
            }
            contract_payload = _json_dumps(
                _stage_parameter_contract(
                    stage_descriptor,
                    stage_parameters,
                    consumed_parameters=consumed_parameters,
                    generated_files=local_files,
                    require_consumption=True,
                )
            )
            if files.get(contract_name) != contract_payload:
                files[contract_name] = contract_payload
                repairs.append({
                    "stage_id": record.get("id"),
                    "action": "refresh_stage_parameter_contract",
                    "file": contract_name,
                    "scope": "local_stage_only",
                })
            _record_generated_file(record, contract_name)
    return repairs


def _repair_preprocessing_package(
    selected_discipline: str,
    material_solver: str,
    files: dict[str, str],
    stage_records: list[dict[str, Any]],
    *,
    base_poscar: str,
    potcar_source: dict[str, Any] | None,
    parameters: dict[str, Any],
    defaults: dict[str, Any],
    review: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    repaired_stage_ids = {
        str(item.get("stage_id") or "")
        for item in (review or {}).get("issues") or []
        if str(item.get("stage_id") or "")
    }
    # Common/package-level contract issues identify the stage by its package
    # path rather than a reviewer-provided stage_id.
    for item in (review or {}).get("issues") or []:
        file_name = str(item.get("file") or "")
        for record in stage_records:
            root = _stage_case_root(record).rstrip("/")
            if root and file_name.startswith(root + "/"):
                repaired_stage_ids.add(str(record.get("id") or ""))
    if selected_discipline == "electronic_structure" and material_solver == "vasp":
        repairs = _repair_vasp_stage_inputs(
            files,
            stage_records,
            base_poscar=base_poscar,
            potcar_source=potcar_source,
            parameters=parameters,
            target_stage_ids=repaired_stage_ids or None,
        )
        if repairs:
            _refresh_workflow_manifest(files, defaults, stage_records)
        return repairs
    repairs: list[dict[str, Any]] = []
    for record in stage_records:
        stage_id = str(record.get("id") or "")
        if not stage_id or stage_id not in repaired_stage_ids:
            continue
        if record.get("execution_kind") != "solver_input":
            continue
        root = str(record.get("case_root") or "").rstrip("/")
        if not root:
            continue
        contract_issue = any(
            item.get("code") == "stage_parameter_contract_invalid"
            and (
                str(item.get("stage_id") or "") == stage_id
                or str(item.get("file") or "").startswith(root + "/")
            )
            for item in (review or {}).get("issues") or []
        )
        effective = _normalise_stage_runtime_parameters({
            **parameters,
            **dict(record.get("effective_parameters") or {}),
        })
        if not record.get("effective_parameters"):
            effective = _approved_stage_parameters(
                parameters,
                {
                    "name": record.get("name"),
                    "calculation_type": record.get("calculation_type"),
                    "parameters": record.get("parameters") or {},
                },
        )
        regenerated, consumed_parameters = _generate_simulation_stage_with_trace(
            selected_discipline, effective
        )
        if not regenerated:
            # Some disciplines use user-provided or previously transformed
            # stage assets rather than a registered text-deck generator. A
            # contract-only review failure is still deterministically
            # repairable without changing those assets or revisiting planning.
            has_stage_assets = any(path.startswith(root + "/") for path in files)
            if contract_issue and has_stage_assets:
                stage_descriptor = {
                    "id": stage_id,
                    "calculation_type": record.get("calculation_type"),
                    "parameters": record.get("parameters") or {},
                    "explicit_parameter_keys": record.get("explicit_parameter_keys") or [],
                }
                contract_path = f"{root}/parameter_contract.json"
                local_assets = {
                    path[len(root) + 1:]: content
                    for path, content in files.items()
                    if path.startswith(root + "/") and not path.endswith("parameter_contract.json")
                }
                contract = _stage_parameter_contract(
                    stage_descriptor,
                    effective,
                    generated_files=local_assets,
                    require_consumption=True,
                )
                if contract["status"] == "pass":
                    contract_payload = _json_dumps(contract)
                    changed = files.get(contract_path) != contract_payload
                    files[contract_path] = contract_payload
                    _record_generated_file(record, contract_path)
                    if changed:
                        repairs.append({
                            "stage_id": stage_id,
                            "action": "refresh_stage_parameter_contract",
                            "scope": "local_stage_only",
                        })
            continue
        before = {
            path: content for path, content in files.items()
            if path.startswith(root + "/")
        }
        for path in list(files):
            relative = path[len(root) + 1:] if path.startswith(root + "/") else ""
            if relative.startswith(("0/", "system/")) or relative in {
                "in.lammps", "lammps.data", "model.inp", "em_case.json", "ports.json",
                "materials.json", "loads.json", "regions.json", "interfaces.json", "coupling_manifest.json",
                "parameter_contract.json",
            }:
                files.pop(path)
        for name, content in regenerated.items():
            files[f"{root}/{name}"] = content
        stage_descriptor = {
            "id": stage_id,
            "calculation_type": record.get("calculation_type"),
            "parameters": record.get("parameters") or {},
            "explicit_parameter_keys": record.get("explicit_parameter_keys") or [],
        }
        files[f"{root}/parameter_contract.json"] = _json_dumps(
            _stage_parameter_contract(
                stage_descriptor,
                effective,
                consumed_parameters=consumed_parameters,
                generated_files=regenerated,
                require_consumption=True,
            )
        )
        _record_generated_file(record, f"{root}/parameter_contract.json")
        after = {
            path: content for path, content in files.items()
            if path.startswith(root + "/")
        }
        if after != before:
            repairs.append({
                "stage_id": stage_id,
                "action": "regenerate_stage_input_deck_from_approved_parameters",
                "scope": "local_stage_only",
            })
    if repairs:
        _refresh_workflow_manifest(files, defaults, stage_records)
    return repairs



def _vasp_files(
    parameters: dict[str, Any],
    potcar_source: dict[str, Any] | None = None,
) -> dict[str, str]:
    calculation = str(parameters.get("calculation_type") or parameters.get("calculation") or "").lower()
    is_relax = bool(re.search(r"relax|optimi[sz]e|geometry|结构优化|弛豫", calculation, flags=re.I))
    is_aimd = bool(re.search(r"\baimd\b|molecular dynamics|分子动力学|\bnvt\b|\bnve\b", calculation, flags=re.I))
    is_neb = bool(re.search(r"\bneb\b|nudged elastic|扩散势垒|迁移路径", calculation, flags=re.I))
    encut = int(float(parameters.get(
        "encut",
        parameters.get("energy_cutoff", parameters.get("ENCUT", 520)),
    )))
    ediff = parameters.get("ediff", parameters.get("EDIFF", "1E-6"))
    ediffg = parameters.get("ediffg", parameters.get("EDIFFG", "-0.02" if is_relax else ""))
    ismear = parameters.get("ismear", parameters.get("ISMEAR", 0))
    sigma = parameters.get("sigma", parameters.get("SIGMA", 0.05))
    exchange_correlation = str(
        parameters.get("exchange_correlation")
        or parameters.get("xc_functional")
        or parameters.get("gga")
        or "PBE"
    ).strip()
    gga_value = "PE" if exchange_correlation.upper() in {"PBE", "GGA-PBE"} else exchange_correlation
    kmesh = parameters.get("kmesh") or parameters.get("kpoints") or parameters.get("kpoint_mesh") or [3, 3, 3]
    if isinstance(kmesh, str):
        values = [int(v) for v in re.findall(r"\d+", kmesh)[:3]]
        kmesh = values if len(values) == 3 else [3, 3, 3]
    if not isinstance(kmesh, list) or len(kmesh) != 3:
        kmesh = [3, 3, 3]
    species = (
        parameters.get("structure_species")
        or parameters.get("species")
        or parameters.get("elements")
        or parameters.get("potcar_symbols")
        or []
    )
    species = [str(item).strip() for item in _as_list(species) if str(item).strip()]

    incar_lines = [
        "SYSTEM = generated_by_data_node",
        f"ENCUT = {encut}",
        f"EDIFF = {ediff}",
        f"ISMEAR = {ismear}",
        f"SIGMA = {sigma}",
        f"GGA = {gga_value}",
        "PREC = Accurate",
        "LREAL = Auto",
    ]
    if is_aimd:
        temperature = parameters.get("temperature")
        md_steps = parameters.get("md_steps", parameters.get("steps", parameters.get("nsw", 5000)))
        timestep_fs = parameters.get("timestep_fs", parameters.get("potim", 1.0))
        mdalgo = parameters.get("mdalgo", 2)
        smass = parameters.get("smass")
        incar_lines.extend([
            "IBRION = 0",
            f"NSW = {md_steps}",
            f"POTIM = {timestep_fs}",
            f"MDALGO = {mdalgo}",
        ])
        if smass not in (None, ""):
            incar_lines.append(f"SMASS = {smass}")
        if temperature not in (None, ""):
            incar_lines.extend([f"TEBEG = {temperature}", f"TEEND = {temperature}"])
    elif is_neb:
        images = parameters.get("images", 7)
        spring = parameters.get("spring", -5.0)
        incar_lines.extend([
            "IBRION = 3",
            "POTIM = 0",
            f"IMAGES = {images}",
            f"SPRING = {spring}",
        ])
    elif is_relax:
        incar_lines.extend([
            "IBRION = 2",
            "NSW = 200",
            "ISIF = 3",
        ])
        if ediffg:
            incar_lines.append(f"EDIFFG = {ediffg}")
    else:
        incar_lines.extend([
            "IBRION = -1",
            "NSW = 0",
        ])
    for key, value in sorted((parameters.get("incar_parameters") or {}).items()):
        normalized = str(key).strip().upper()
        if not normalized:
            continue
        incar_lines = [
            line for line in incar_lines
            if not re.match(rf"^\s*{re.escape(normalized)}\s*=", line, flags=re.I)
        ]
        if isinstance(value, bool):
            value = ".TRUE." if value else ".FALSE."
        incar_lines.append(f"{normalized} = {value}")
    files = {
        "INCAR": "\n".join(incar_lines) + "\n",
        "KPOINTS": "\n".join([
            "Automatic mesh generated by data node",
            "0",
            "Gamma",
            " ".join(str(int(v)) for v in kmesh),
            "0 0 0",
            "",
        ]),
        "VASP_INPUTS_README.md": "\n".join([
            "# VASP preprocessing package",
            "",
            "- `INCAR` and `KPOINTS` are generated from local/upstream parameters and conservative defaults.",
            "- `POTCAR` is assembled only from an authorized local pseudopotential library.",
            "- `POSCAR` is included only when a validated structure file or complete crystallographic evidence is available.",
            "",
        ]),
    }
    poscar_text = str(parameters.get("poscar_text") or "").strip()
    structure_file = str(parameters.get("structure_file") or parameters.get("poscar_path") or "").strip()
    if poscar_text:
        files["POSCAR"] = poscar_text.rstrip() + "\n"
    elif structure_file:
        path = Path(structure_file).expanduser()
        try:
            if path.is_file():
                files["POSCAR"] = path.read_text(encoding="utf-8", errors="replace").rstrip() + "\n"
        except OSError:
            pass
    resolved_species = _poscar_species_from_text(files.get("POSCAR", "")) or species
    potcar, _ = _assemble_potcar(resolved_species, potcar_source, parameters)
    if potcar is not None:
        files["POTCAR"] = potcar
    return files


def _paths_from_text(value: str) -> list[str]:
    paths: list[str] = []
    for raw_path in re.findall(
        r"(?<![\w:])(?:~/(?:[^\s,;，；。/]+/)*[^\s,;，；。]+|"
        r"/(?:Users|home|tmp|private|var|opt|data|workspace|mnt)/[^\n\r\t,;，；。]+)",
        value or "",
    ):
        cleaned = raw_path.strip().strip("'\"`()[]{}<>：:")
        candidate = Path(cleaned).expanduser()
        if candidate.exists() and str(candidate) not in paths:
            paths.append(str(candidate))
    return paths


def _mesh_result_cell_count(result: dict[str, Any] | None) -> int | None:
    if not isinstance(result, dict):
        return None
    for key in ("n_cells", "cell_count", "n_3d_elements", "n_2d_elements"):
        try:
            value = int(result.get(key) or 0)
        except (TypeError, ValueError):
            value = 0
        if value > 0:
            return value
    report = result.get("quality_report")
    if report:
        try:
            data = json.loads(Path(str(report)).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
        for key in ("n_3d_elements", "n_2d_elements", "n_cells"):
            try:
                value = int(data.get(key) or 0)
            except (TypeError, ValueError):
                value = 0
            if value > 0:
                return value
    return None


def _scaled_mesh_density_parameters(
    parameters: dict[str, Any],
    *,
    target_cell_count: int | None,
    reference_cell_count: int | None,
    invariant_controls: set[str] | None = None,
) -> dict[str, Any]:
    """Scale common mesh-density controls without changing physical geometry."""
    scaled = dict(parameters)
    if not target_cell_count or not reference_cell_count:
        return scaled
    invariant_names = {str(name).casefold() for name in (invariant_controls or set())}
    factor = max(0.25, min(4.0, math.sqrt(target_cell_count / reference_cell_count)))
    ratio_value = scaled.get("boundary_layer_ratio")
    if (
        isinstance(ratio_value, (int, float))
        and not isinstance(ratio_value, bool)
        and "boundary_layer_ratio" not in invariant_names
        and float(ratio_value) > 1.0
    ):
        # Keep first-cell height and total layer thickness fixed while changing
        # radial resolution.  A smaller growth ratio creates more layers; a
        # larger ratio creates fewer layers.
        scaled["boundary_layer_ratio"] = max(
            1.03,
            min(1.24, 1.0 + (float(ratio_value) - 1.0) / factor),
        )
    for key, value in list(scaled.items()):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        normalized = str(key).lower()
        if normalized in invariant_names:
            continue
        if normalized == "boundary_layer_ratio":
            continue
        if (
            normalized.startswith("n_")
            or normalized.endswith(("_divisions", "_segments", "_layers"))
        ):
            scaled[key] = max(2, int(round(value * factor)))
        elif (
            normalized.startswith("h_")
            or normalized.endswith(("_cell_size", "_element_size", "_refinement_size"))
            or normalized == "boundary_layer_first"
        ):
            scaled[key] = float(value) / factor
    return scaled


def _mesh_convergence_family_parameters(
    parameters: dict[str, Any],
    mesh_generation_result: dict[str, Any],
    *,
    authoritative_controls: set[str] | None = None,
) -> tuple[dict[str, Any], set[str]]:
    """Freeze non-density mesh choices for a convergence family.

    Convergence compares discretisation, not different geometry, domains, or
    wall treatments. The returned invariant set is adapter-neutral and only
    covers controls that the original mesh actually declared.
    """
    family = dict(parameters)
    authoritative_names = {
        str(name).casefold() for name in (authoritative_controls or set())
    }
    if mesh_generation_result.get("mesh_type"):
        family["mesh_type"] = mesh_generation_result["mesh_type"]
    actual = mesh_generation_result.get("grid_params") or {}
    grid_invariants: dict[str, Any] = {}
    if isinstance(actual, dict):
        for key, value in actual.items():
            normalized = str(key).lower()
            if value in (None, "", [], {}):
                continue
            is_density_control = (
                normalized.startswith(("n_", "h_"))
                or normalized.endswith(("_divisions", "_segments", "_layers", "_cell_size", "_element_size", "_refinement_size"))
                or normalized in {"target_streamwise_delta_x_plus", "boundary_layer_ratio"}
            )
            excluded = normalized in {
                "mesh_family_contract", "target_cell_count", "quality_adjustments",
                "gmsh_binary", "gmsh_to_foam_cmd", "convert_to_openfoam", "write_tecplot",
            }
            if not excluded and not (
                normalized in authoritative_names
                and family.get(key) not in (None, "", [], {})
            ):
                family[key] = value
            if not is_density_control and not excluded:
                grid_invariants[key] = family.get(key, value)
    invariant_controls = {
        key for key in (
            "boundary_layer_first", "first_layer_height", "wall_first_cell_height",
            "near_wall_refinement_mode", "boundary_layer_thickness",
        )
        if family.get(key) not in (None, "", [], {})
    }
    family["mesh_family_contract"] = {
        # ``mesh_type`` is an adapter/router label and may legitimately differ
        # from the research-plan description (for example a C-domain request
        # implemented by a coordinate-profile Gmsh adapter).  Family identity
        # is instead enforced through measurable geometry, domain and
        # near-wall invariants below.
        "grid_invariants": grid_invariants,
        "invariant_controls": sorted(invariant_controls),
    }
    family["lock_near_wall_topology"] = bool(invariant_controls)
    return family, invariant_controls


def _quality_bounded_refinement_target(
    previous_valid_cells: int | None,
    requested_target_cells: int | None,
) -> int | None:
    """Choose one conservative refinement target after a quality-gate failure.

    The target remains between the last accepted density and the requested
    density.  This is deliberately geometry-agnostic: it is only a bounded
    retry policy for mesher density controls, never a geometry repair policy.
    """
    if not previous_valid_cells or not requested_target_cells:
        return None
    if requested_target_cells <= previous_valid_cells:
        return None
    span = requested_target_cells - previous_valid_cells
    # One bounded midpoint-biased retry keeps a failed fine level from turning
    # into an unbounded meshing loop while still providing meaningful refinement.
    candidate = previous_valid_cells + int(round(span * 0.55))
    return min(requested_target_cells - 1, max(previous_valid_cells + 1, candidate))


def _mesh_variant_is_valid(result: dict[str, Any]) -> bool:
    """Apply the same deliverability gate used by stage contracts."""
    return (
        result.get("status") == "success"
        and result.get("deliverable_valid", True) is not False
        and ((result.get("mesh_review") or {}).get("status")) != "fail"
    )


def _mesh_convergence_reference_level(
    stage: dict[str, Any],
    levels: list[dict[str, Any]],
    reference_cells: int | None,
) -> str:
    """Select the production mesh that anchors a convergence family."""
    parameters = dict(stage.get("parameters") or {})
    explicit = str(
        parameters.get("reference_level")
        or parameters.get("baseline_level")
        or parameters.get("downstream_default_level")
        or ""
    ).strip().casefold()
    level_ids = {
        str(level.get("id") or "").strip().casefold(): str(level.get("id") or "")
        for level in levels
    }
    if explicit in level_ids:
        return level_ids[explicit]
    for preferred in ("medium", "reference", "baseline"):
        if preferred in level_ids:
            return level_ids[preferred]
    comparable = [
        level for level in levels if _integer_from_count(level.get("target_cell_count"))
    ]
    if comparable:
        if reference_cells:
            selected = min(
                comparable,
                key=lambda level: abs(
                    int(level.get("target_cell_count") or 0) - reference_cells
                ),
            )
        else:
            selected = sorted(
                comparable,
                key=lambda level: int(level.get("target_cell_count") or 0),
            )[len(comparable) // 2]
        return str(selected.get("id") or "")
    return str(levels[0].get("id") or "")


def _validate_mesh_convergence_family(
    variants: list[dict[str, Any]],
    reference_level: str,
) -> dict[str, Any]:
    """Require a single comparable family with density increasing by level."""
    valid = [item for item in variants if _mesh_variant_is_valid(item.get("result") or {})]
    ordered = sorted(
        valid,
        key=lambda item: int(item.get("target_cell_count") or 0),
    )
    counts = [int(item.get("actual_cell_count") or 0) for item in ordered]
    strictly_increasing = bool(counts) and all(
        left < right for left, right in zip(counts, counts[1:])
    )
    density_control_checks: dict[str, Any] = {}

    def control_values(name: str) -> list[float] | None:
        values: list[float] = []
        present = 0
        for item in ordered:
            value = ((item.get("result") or {}).get("grid_params") or {}).get(name)
            if value not in (None, ""):
                present += 1
                try:
                    values.append(float(value))
                except (TypeError, ValueError):
                    return None
        if present == 0:
            return []
        return values if present == len(ordered) else None

    for name, direction in (
        ("n_surface", "increasing"),
        ("boundary_layer_layers", "increasing"),
        ("boundary_layer_ratio", "decreasing"),
        ("h_airfoil", "decreasing"),
    ):
        values = control_values(name)
        if values == []:
            continue
        monotonic = bool(values) and all(
            left < right if direction == "increasing" else left > right
            for left, right in zip(values, values[1:])
        )
        density_control_checks[name] = {
            "status": "pass" if monotonic else "fail",
            "direction_with_refinement": direction,
            "values_in_target_order": values,
        }
    density_controls_valid = all(
        item.get("status") == "pass"
        for item in density_control_checks.values()
    )
    reference = next(
        (
            item for item in variants
            if str(item.get("id") or "").casefold() == str(reference_level).casefold()
        ),
        None,
    )
    status = (
        "pass"
        if len(valid) == len(variants)
        and strictly_increasing
        and density_controls_valid
        and reference is not None
        and _mesh_variant_is_valid(reference.get("result") or {})
        else "fail"
    )
    return {
        "status": status,
        "reference_level": reference_level,
        "downstream_default_level": reference_level,
        "generation_policy": "reference_first_then_derive_coarser_and_finer",
        "actual_cell_counts_in_target_order": counts,
        "strictly_increasing_cell_counts": strictly_increasing,
        "density_control_checks": density_control_checks,
        "density_controls_valid": density_controls_valid,
        "convergence_decision": "requires downstream simulation comparison",
        "comparison_contract": (
            "Run identical physics and numerics on the reference and finer meshes; "
            "accept the reference mesh only when declared observables differ within the research-plan tolerance."
        ),
    }


def _reference_mesh_from_convergence_variants(
    variants_by_stage: dict[str, list[dict[str, Any]]],
) -> dict[str, Any] | None:
    """Return an isolated copy of the accepted downstream-default mesh."""
    for variants in variants_by_stage.values():
        reference = next(
            (
                item for item in variants
                if item.get("downstream_default") is True
                and item.get("deliverable_valid") is not False
                and _mesh_variant_is_valid(item.get("result") or {})
            ),
            None,
        )
        if reference:
            result = copy.deepcopy(reference.get("result") or {})
            result["mesh_convergence_role"] = "reference"
            result["mesh_convergence_level"] = reference.get("id")
            result["downstream_default"] = True
            return result
    return None


async def _generate_mesh_convergence_variants(
    *,
    state: State,
    spec: str,
    selected_discipline: str,
    params: dict[str, Any],
    package_dir: Path,
    calculation_stages: list[dict[str, Any]],
    mesh_generation_result: dict[str, Any] | None,
) -> dict[str, list[dict[str, Any]]]:
    """Generate declared refinement levels for any supported mesher-backed stage."""
    if not isinstance(mesh_generation_result, dict) or mesh_generation_result.get("status") != "success":
        return {}
    params = _inherit_reusable_mesh_provenance(params, mesh_generation_result)
    base_reference_cells = _mesh_result_cell_count(mesh_generation_result)
    plan_contract = params.get("mesh_plan_contract") or {}
    authoritative_controls = set((plan_contract.get("applied") or {}).keys())
    base_family_parameters, base_invariant_controls = _mesh_convergence_family_parameters(
        params,
        mesh_generation_result,
        authoritative_controls=authoritative_controls,
    )
    results_by_stage: dict[str, list[dict[str, Any]]] = {}
    from .scientific_mesh import prepare_scientific_mesh

    for stage in calculation_stages:
        if _stage_kind(stage) != "mesh_convergence":
            continue
        levels = _mesh_convergence_levels(
            stage,
            _integer_from_count(params.get("target_cell_count")) or base_reference_cells,
        )
        if not levels:
            continue
        reference_level = _mesh_convergence_reference_level(
            stage,
            levels,
            base_reference_cells,
        )
        reference_target_cells = next(
            (
                _integer_from_count(level.get("target_cell_count"))
                for level in levels
                if str(level.get("id") or "") == reference_level
            ),
            None,
        )
        generation_levels = sorted(
            levels,
            key=lambda level: (
                str(level.get("id") or "") != reference_level,
                int(level.get("target_cell_count") or 0),
            ),
        )
        stage_id = _slugify(str(stage.get("id") or "mesh_convergence"), "mesh_convergence").lower()
        stage_root = package_dir / "stages" / stage_id
        stage_results_by_id: dict[str, dict[str, Any]] = {}
        reference_cells = base_reference_cells
        family_parameters = dict(base_family_parameters)
        invariant_controls = set(base_invariant_controls)
        reference_ready = False
        for generation_index, level in enumerate(generation_levels, 1):
            level_id = _slugify(str(level.get("id") or "level"), "level").lower()
            requested_target_cells = _integer_from_count(level.get("target_cell_count"))
            case_dir = stage_root / "mesh_variants" / level_id
            is_reference = str(level.get("id") or "") == reference_level
            if not is_reference and not reference_ready:
                stage_results_by_id[level_id] = {
                    "id": level_id,
                    "role": "derived",
                    "downstream_default": False,
                    "target_cell_count": requested_target_cells,
                    "actual_cell_count": None,
                    "case_dir": str(case_dir.resolve()),
                    "status": "blocked_reference_mesh_invalid",
                    "deliverable_valid": False,
                    "mesh_review_status": "fail",
                    "result": {
                        "status": "error",
                        "deliverable_valid": False,
                        "error": "The reference mesh failed; derived convergence levels were not generated.",
                    },
                }
                continue
            candidate_targets = [requested_target_cells]
            candidate_attempts: list[dict[str, Any]] = []
            result: dict[str, Any] = {}
            selected_target_cells = requested_target_cells

            # The first attempt honors the requested density.  If its quality
            # review rejects the mesh, make at most one lower-density retry
            # between this level and the most recent accepted level.
            for attempt_index in range(2):
                candidate_target_cells = candidate_targets[-1]
                variant_params = _scaled_mesh_density_parameters(
                    {**family_parameters, **dict(stage.get("parameters") or {})},
                    target_cell_count=candidate_target_cells,
                    reference_cell_count=reference_cells,
                    invariant_controls=invariant_controls,
                )
                variant_params.update(dict(level.get("parameters") or {}))
                for invariant in invariant_controls:
                    if invariant in family_parameters:
                        variant_params[invariant] = family_parameters[invariant]
                relaxed_reference_constraints: list[str] = []
                if (
                    not is_reference
                    and requested_target_cells
                    and reference_target_cells
                    and requested_target_cells < reference_target_cells
                ):
                    # The reference mesh must satisfy the production-plan
                    # resolution. A deliberately coarser comparison mesh may
                    # relax tangential/global density limits, while geometry,
                    # domain and explicit first-layer controls stay fixed.
                    for key in ("target_streamwise_delta_x_plus",):
                        if variant_params.pop(key, None) is not None:
                            relaxed_reference_constraints.append(key)
                variant_params.update({
                    "mesh_type": family_parameters.get("mesh_type") or params.get("mesh_type"),
                    "mesh_convergence_level": level_id,
                    "mesh_convergence_reference_level": reference_level,
                    "mesh_convergence_role": "reference" if is_reference else "derived",
                    "target_cell_count": candidate_target_cells,
                    "convert_to_openfoam": variant_params.get("convert_to_openfoam", True),
                    "write_tecplot": variant_params.get("write_tecplot", True),
                })
                try:
                    result = await prepare_scientific_mesh(
                        state=state,
                        spec=(
                            f"Generate the {level_id} mesh-convergence level for the declared scientific case. "
                            "Keep geometry and physical domain fixed; change only mesh-density controls."
                        ),
                        discipline=selected_discipline,
                        mesh_type=str(variant_params.get("mesh_type") or ""),
                        case_dir=str(case_dir),
                        parameters=_json_dumps({
                            key: value for key, value in variant_params.items()
                            if key not in {"requirement_analysis", "calculation_stages"}
                        }),
                        operation="prepare",
                        generate_mesh=True,
                    )
                except Exception as exc:
                    result = {"status": "error", "error": f"{type(exc).__name__}: {exc}"}

                actual_cell_count = _mesh_result_cell_count(result)
                candidate_attempts.append({
                    "attempt": attempt_index + 1,
                    "target_cell_count": candidate_target_cells,
                    "actual_cell_count": actual_cell_count,
                    "status": result.get("status"),
                    "deliverable_valid": result.get(
                        "deliverable_valid", result.get("status") == "success"
                    ),
                    "mesh_review_status": ((result.get("mesh_review") or {}).get("status")),
                    "case_dir": str(case_dir.resolve()),
                })
                selected_target_cells = candidate_target_cells
                if _mesh_variant_is_valid(result):
                    break

                fallback_target = (
                    None
                    if is_reference
                    else _quality_bounded_refinement_target(
                        reference_cells,
                        requested_target_cells,
                    )
                )
                if attempt_index or not fallback_target or fallback_target == candidate_target_cells:
                    break
                candidate_targets.append(fallback_target)

            if result.get("case_dir"):
                result = _organize_mesh_convergence_variant_files(
                    package_dir,
                    stage_id,
                    level_id,
                    result,
                ) or result
            actual_cell_count = _mesh_result_cell_count(result)
            variant_record = {
                "id": level_id,
                "role": "reference" if is_reference else "derived",
                "downstream_default": is_reference,
                "generation_order": generation_index,
                "target_cell_count": requested_target_cells,
                "selected_target_cell_count": selected_target_cells,
                "actual_cell_count": actual_cell_count,
                "target_relaxed_for_quality": (
                    selected_target_cells != requested_target_cells
                    and _mesh_variant_is_valid(result)
                ),
                "reference_constraints_relaxed_for_coarse_level": relaxed_reference_constraints,
                "quality_bounded_retry": {
                    "policy": "one_conservative_refinement_retry",
                    "attempts": candidate_attempts,
                },
                "case_dir": str(case_dir.resolve()),
                "asset_locations": result.get("convergence_asset_locations") or {},
                "status": result.get("status"),
                "deliverable_valid": result.get("deliverable_valid", result.get("status") == "success"),
                "mesh_review_status": ((result.get("mesh_review") or {}).get("status")),
                "result": result,
            }
            stage_results_by_id[level_id] = variant_record
            if is_reference and _mesh_variant_is_valid(result):
                reference_ready = True
                reference_cells = actual_cell_count or reference_cells
                family_parameters, invariant_controls = _mesh_convergence_family_parameters(
                    variant_params,
                    result,
                )
        stage_results = [
            stage_results_by_id[_slugify(str(level.get("id") or "level"), "level").lower()]
            for level in levels
        ]
        family_validation = _validate_mesh_convergence_family(stage_results, reference_level)
        for item in stage_results:
            item["family_validation"] = family_validation
            if family_validation["status"] != "pass":
                item["deliverable_valid"] = False
        results_by_stage[stage_id] = stage_results
    return results_by_stage


def _local_reference_inputs(spec: str, params: dict[str, Any]) -> tuple[list[str], list[dict[str, Any]]]:
    source_paths: list[str] = []
    reference_evidence: list[dict[str, Any]] = []

    def add_path(value: Any) -> None:
        path = str(value or "").strip()
        if path and Path(path).expanduser().exists() and path not in source_paths:
            source_paths.append(path)

    for key in ("source_paths", "reference_paths", "local_reference_paths"):
        for value in _as_list(params.get(key)):
            add_path(value)
    for key in ("reference_evidence", "evidence"):
        for item in _as_list(params.get(key)):
            if isinstance(item, dict):
                reference_evidence.append(item)
                for path_key in ("path", "file_path", "local_path"):
                    add_path(item.get(path_key))
    analysis = params.get("requirement_analysis")
    if isinstance(analysis, dict):
        for item in analysis.get("evidence") or []:
            if isinstance(item, dict):
                reference_evidence.append(item)
                for path_key in ("path", "file_path", "local_path"):
                    add_path(item.get(path_key))
        for file_item in analysis.get("required_files") or []:
            if not isinstance(file_item, dict):
                continue
            for item in file_item.get("evidence") or []:
                if isinstance(item, dict):
                    reference_evidence.append(item)
                    for path_key in ("path", "file_path", "local_path"):
                        add_path(item.get(path_key))
    for path in _paths_from_text(spec):
        add_path(path)
    if re.search(
        r"\b(space\s*group|wyckoff|wickoff|fractional|lattice|POSCAR)\b|"
        r"空间群|晶格|分数坐标|原子坐标|位点",
        spec,
        flags=re.I,
    ):
        reference_evidence.append({
            "source_type": "task_context_crystallographic_excerpt",
            "text": spec,
        })
    return source_paths, reference_evidence


async def _recover_poscar_from_local_references(
    state: State,
    package_dir: Path,
    spec: str,
    params: dict[str, Any],
) -> dict[str, Any] | None:
    emit_progress(state, "poscar_recovery", "checking local/reference evidence")
    source_paths, reference_evidence = _local_reference_inputs(spec, params)
    inspected_inputs = getattr(state, "hook_state", {}).get("data_input_inspections")
    has_inspected_inputs = isinstance(inspected_inputs, list) and bool(inspected_inputs)
    if not source_paths and not reference_evidence and not has_inspected_inputs:
        emit_progress(state, "poscar_recovery_skip", "no local/reference evidence")
        return None
    try:
        from .atomic_structure_recovery import (
            _inspect_structure_file,
            _write_candidate_poscar,
            recover_atomic_structure,
        )
    except Exception as exc:
        return {"status": "skipped", "reason": f"atomic structure recovery unavailable: {type(exc).__name__}: {exc}"}

    structure_species = params.get("structure_species") or []
    composition = str(
        params.get("structure_composition")
        or params.get("composition")
        or " ".join(str(item) for item in _as_list(structure_species))
    ).strip()
    expected = params.get("expected_atom_count") or params.get("atom_count") or params.get("natoms")
    try:
        expected_atom_count = int(expected) if expected else None
    except (TypeError, ValueError):
        expected_atom_count = None
    structure_name = str(
        params.get("structure_name")
        or params.get("material")
        or params.get("system")
        or params.get("case_name")
        or "local_reference_structure"
    )
    assessed = await recover_atomic_structure(
        state,
        structure_name=structure_name,
        composition=composition,
        expected_atom_count=expected_atom_count,
        source_paths=source_paths,
        reference_evidence=reference_evidence,
        operation="assess",
        output_name="POSCAR",
    )
    if assessed.get("status") != "ready_to_reconstruct":
        emit_progress(
            state,
            "poscar_recovery_done",
            str(assessed.get("status") or "unknown"),
        )
        return assessed
    evidence = assessed.get("crystallographic_evidence") or {}
    candidate = package_dir / "POSCAR"
    _write_candidate_poscar(candidate, structure_name, evidence)
    validation = _inspect_structure_file(candidate, expected_atom_count)
    emit_progress(
        state,
        "poscar_recovery_done",
        "success" if validation.get("valid") else "review_failed",
        atom_count=validation.get("atom_count"),
    )
    result = {
        "status": "success" if validation.get("valid") else "review_failed",
        "candidate_path": str(candidate),
        "validation": validation,
        "crystallographic_evidence": evidence,
    }
    if not validation.get("valid"):
        issues = [{
            "code": "structure_validation_failed",
            "file": str(candidate),
            "message": str(validation.get("error") or validation.get("reason") or "Generated structure is not simulation-ready."),
            "required_change": "Repair the generated structure using the locked crystallographic evidence.",
        }]
        result["revision_contract"] = revision_contract(issues, scope="asset")
    return result


def _training_script() -> str:
    return '''"""Train a baseline scientific surrogate model from a CSV/TSV table.

The script prefers scikit-learn when available and falls back to NumPy ridge
regression.

Outputs (model_metrics.json / model_coefficients.json) are written to an
**explicit** directory, never to the process cwd: by default next to this script
(i.e. inside the preprocessing package), overridable with --out-dir. Relying on
cwd made the landing spot depend on who launched the script (harness-framework
issue #166.4).
"""
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path


def load_table(path: Path, target: str | None):
    delimiter = "\\t" if path.suffix.lower() == ".tsv" else ","
    with path.open(newline="", encoding="utf-8-sig", errors="ignore") as f:
        rows = list(csv.DictReader(f, delimiter=delimiter))
    if not rows:
        raise SystemExit("empty table")
    columns = list(rows[0].keys())
    numeric = []
    for col in columns:
        ok = 0
        for row in rows:
            try:
                float(row[col])
                ok += 1
            except (TypeError, ValueError):
                pass
        if ok == len(rows):
            numeric.append(col)
    if len(numeric) < 2:
        raise SystemExit("need at least two fully numeric columns")
    if target is None:
        target = numeric[-1]
    features = [c for c in numeric if c != target]
    x = [[float(row[c]) for c in features] for row in rows]
    y = [float(row[target]) for row in rows]
    return features, target, x, y


OUT_DIR = Path(__file__).resolve().parent


def _parse_argv(argv):
    args = list(argv)
    out_dir = OUT_DIR
    if "--out-dir" in args:
        i = args.index("--out-dir")
        try:
            out_dir = Path(args[i + 1]).expanduser().resolve()
        except IndexError:
            raise SystemExit("--out-dir requires a directory path")
        del args[i:i + 2]
    return args, out_dir


def main():
    argv, out_dir = _parse_argv(sys.argv[1:])
    if not argv:
        raise SystemExit(
            "usage: python train_model.py DATA.csv [target_column] [--out-dir DIR]"
        )
    path = Path(argv[0])
    target = argv[1] if len(argv) > 1 else None
    features, target, x, y = load_table(path, target)
    try:
        from sklearn.ensemble import RandomForestRegressor
        from sklearn.metrics import mean_absolute_error, r2_score
        from sklearn.model_selection import train_test_split
        x_train, x_test, y_train, y_test = train_test_split(x, y, test_size=0.2, random_state=17)
        model = RandomForestRegressor(n_estimators=200, random_state=17, min_samples_leaf=2)
        model.fit(x_train, y_train)
        pred = model.predict(x_test)
        metrics = {
            "model": "RandomForestRegressor",
            "target": target,
            "features": features,
            "mae": float(mean_absolute_error(y_test, pred)),
            "r2": float(r2_score(y_test, pred)) if len(set(y_test)) > 1 else None,
            "n_train": len(x_train),
            "n_test": len(x_test),
        }
        coeffs = {"feature_importances": dict(zip(features, map(float, model.feature_importances_)))}
    except Exception as exc:
        import numpy as np
        X = np.asarray(x, dtype=float)
        Y = np.asarray(y, dtype=float)
        X = np.column_stack([np.ones(len(X)), X])
        lam = 1e-8
        beta = np.linalg.solve(X.T @ X + lam * np.eye(X.shape[1]), X.T @ Y)
        pred = X @ beta
        mae = float(np.mean(np.abs(pred - Y)))
        ss_res = float(np.sum((Y - pred) ** 2))
        ss_tot = float(np.sum((Y - np.mean(Y)) ** 2))
        metrics = {
            "model": "numpy_ridge_regression",
            "fallback_reason": str(exc),
            "target": target,
            "features": features,
            "mae_train": mae,
            "r2_train": 1.0 - ss_res / ss_tot if ss_tot else None,
            "n_train": len(Y),
        }
        coeffs = {"intercept": float(beta[0]), "coefficients": dict(zip(features, map(float, beta[1:])))}
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "model_metrics.json").write_text(json.dumps(metrics, indent=2), encoding="utf-8")
    (out_dir / "model_coefficients.json").write_text(
        json.dumps(coeffs, indent=2), encoding="utf-8"
    )
    print(json.dumps({"output_dir": str(out_dir), **metrics}, indent=2))


if __name__ == "__main__":
    main()
'''


def _write_package_files(package_dir: Path, files: dict[str, str]) -> list[str]:
    written: list[str] = []
    for rel, content in files.items():
        path = package_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        written.append(str(path))
    return written


def _detect_existing_cfd_mesh_assets(case_dir: Any) -> dict[str, Any] | None:
    """Validate only an explicitly supplied case; never scan a run implicitly."""
    if not case_dir:
        return None
    return _detect_existing_cfd_mesh_assets_at_root(Path(str(case_dir)).expanduser())


def _detect_existing_cfd_mesh_assets_at_root(root: Path) -> dict[str, Any] | None:
    from .mesh_generator import validate_openfoam_polymesh, validate_tecplot_against_polymesh

    if root.name == "polyMesh" and root.parent.name == "constant":
        root = root.parent.parent
    package_root = root
    for candidate in (root, *root.parents):
        if (candidate / "manifest.json").is_file() or (candidate / "audit" / "mesh_delivery.json").is_file():
            package_root = candidate
            break
    poly_dirs = [package_root / "constant" / "polyMesh"]
    surface_candidates = sorted({
        *package_root.glob("*_tecplot_surface.dat"),
        *package_root.glob("mesh_surface.dat"),
        *(package_root / "visualization").glob("*_tecplot_surface.dat"),
        *(package_root / "visualization").glob("mesh_surface.dat"),
    })
    volume_candidates = sorted({
        *package_root.glob("*_tecplot_volume.dat"),
        *package_root.glob("mesh_volume.dat"),
        *(package_root / "visualization").glob("*_tecplot_volume.dat"),
        *(package_root / "visualization").glob("mesh_volume.dat"),
    })
    quality = package_root / "audit" / "mesh_quality.json"
    if not quality.exists():
        quality = package_root / "mesh_quality.json"
    review_path = package_root / "audit" / "mesh_review.json"
    if not review_path.exists():
        review_path = package_root / "mesh_review.json"
    review: dict[str, Any] = {}
    if review_path.exists():
        try:
            review = json.loads(review_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            review = {}
    if review.get("status") == "fail":
        return None
    for poly_dir in poly_dirs:
        core_mesh_files = [poly_dir / name for name in ("points", "faces", "owner", "neighbour", "boundary")]
        if not all(path.is_file() and path.stat().st_size > 0 for path in core_mesh_files):
            continue
        openfoam_validation = validate_openfoam_polymesh(poly_dir)
        valid_surface_candidates = [
            path for path in surface_candidates
            if validate_tecplot_against_polymesh(poly_dir, path).get("status") == "pass"
        ]
        if openfoam_validation.get("status") != "pass" or not valid_surface_candidates:
            continue
        case_root = poly_dir.parent.parent
        control = case_root / "system" / "controlDict"
        written = [str(path) for path in core_mesh_files]
        if control.is_file() and control.stat().st_size > 0:
            written.append(str(control))
        written.extend(str(path) for path in valid_surface_candidates[:1])
        written.extend(str(path) for path in volume_candidates[:1])
        if quality.exists():
            written.append(str(quality))
        if review_path.exists():
            written.append(str(review_path))
        return {
            "status": "success",
            "source": "existing_case_dir",
            "case_dir": str(package_root.resolve()),
            "openfoam_case_dir": str(case_root.resolve()),
            "polyMesh_dir": str(poly_dir.resolve()),
            "polyMesh_files": {path.name: str(path.resolve()) for path in core_mesh_files},
            "controlDict": str(control.resolve()) if control.is_file() else "",
            "tecplot_file": str(valid_surface_candidates[0].resolve()),
            "tecplot_volume_file": str(volume_candidates[0].resolve()) if volume_candidates else "",
            "quality_report": str(quality.resolve()) if quality.exists() else "",
            "mesh_review_report": str(review_path.resolve()) if review_path.exists() else "",
            "mesh_review": review,
            "deliverable_valid": review.get("status") in {None, "pass"},
            "openfoam_validation": openfoam_validation,
            "written_files": written,
        }
    return None


def _replace_written_path(assets: dict[str, Any], old: Path, new: Path) -> None:
    old_value = str(old.resolve())
    new_value = str(new.resolve())
    for key in ("written_files", "tecplot_files"):
        written = [str(path) for path in assets.get(key) or []]
        assets[key] = [new_value if str(Path(path).resolve()) == old_value else path for path in written]
    if new_value not in assets["written_files"]:
        assets["written_files"].append(new_value)


def _organize_mesh_reproducibility_files(package_dir: Path, assets: dict[str, Any] | None) -> dict[str, Any] | None:
    """Move optional mesher source files out of the delivery root.

    The root remains an immediately usable mesh delivery.  Native mesher files
    stay available for reproducibility but live under a stable, non-case folder.
    """
    if not assets:
        return assets
    reproducibility_dir = package_dir / "reproducibility" / "mesh"
    runtime_file = assets.get("solver_mesh_file") or (
        assets.get("mesh_file") if not assets.get("polyMesh_dir") else None
    )
    primary = Path(str(runtime_file)).resolve() if runtime_file else None
    candidates: list[tuple[str | None, Path]] = []
    for key in ("geo_file", "mesh_file", "generator_script"):
        if assets.get(key):
            candidates.append((key, Path(str(assets[key])).expanduser()))
    # Existing-mesh discovery reconstructs delivery paths from polyMesh.  Scan
    # root source files as well, so their provenance is not lost on reuse.
    candidates.extend((None, path) for pattern in ("*.geo", "*.msh", "*.py") for path in package_dir.glob(pattern))
    seen: set[Path] = set()
    for key, source in candidates:
        try:
            source = source.resolve()
        except OSError:
            continue
        if source in seen:
            continue
        seen.add(source)
        if not source.is_file():
            continue
        try:
            relative = source.relative_to(package_dir.resolve())
        except ValueError:
            relative = None
        if relative is not None and source.parent != package_dir.resolve() and source.parent != reproducibility_dir.resolve():
            continue
        # A standalone requested mesh is the runtime delivery, not provenance.
        target_root = package_dir if source == primary else reproducibility_dir
        target_root.mkdir(parents=True, exist_ok=True)
        target = target_root / source.name
        if source != target:
            if target.exists():
                target.unlink()
            if relative is None:
                shutil.copy2(source, target)
            else:
                shutil.move(str(source), str(target))
        if key:
            assets[key] = str(target.resolve())
        for alias in ("mesh_file", "solver_mesh_file"):
            if assets.get(alias) and Path(str(assets[alias])).resolve() == source:
                assets[alias] = str(target.resolve())
        _replace_written_path(assets, source, target)
    return assets


def _remove_superseded_mesh_assets(
    package_dir: Path,
    superseded: dict[str, Any] | None,
    selected: dict[str, Any] | None,
) -> list[str]:
    """Remove only preliminary assets replaced by a reviewed reference mesh."""
    if not superseded or not selected:
        return []
    package_root = package_dir.resolve()
    selected_paths: set[Path] = set()
    for key in (
        "polyMesh_dir", "geo_file", "mesh_file", "tecplot_file",
        "tecplot_volume_file", "quality_report", "mesh_review_report",
    ):
        raw = selected.get(key)
        if raw:
            try:
                selected_paths.add(Path(str(raw)).expanduser().resolve())
            except OSError:
                pass

    removed: list[str] = []
    for key in (
        "tecplot_file", "tecplot_volume_file", "quality_report",
        "mesh_review_report", "geo_file", "mesh_file", "polyMesh_dir",
    ):
        raw = superseded.get(key)
        if not raw:
            continue
        try:
            path = Path(str(raw)).expanduser().resolve()
            path.relative_to(package_root)
        except (OSError, ValueError):
            continue
        if path in selected_paths or not path.exists():
            continue
        if path.is_dir():
            shutil.rmtree(path)
        elif path.is_file():
            path.unlink()
        else:
            continue
        removed.append(str(path))
        parent = path.parent
        while parent != package_root and parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()
            parent = parent.parent
    return removed


def _organize_mesh_convergence_variant_files(
    package_dir: Path,
    stage_id: str,
    level_id: str,
    assets: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Keep convergence cases runnable while grouping non-runtime assets by role."""
    if not assets:
        return assets
    package_root = package_dir.resolve()
    role_roots = {
        "visualization": package_dir / "visualization" / "mesh_convergence" / stage_id / level_id,
        "audit": package_dir / "audit" / "mesh_convergence" / stage_id / level_id,
        "reproducibility": package_dir / "reproducibility" / "mesh_convergence" / stage_id / level_id,
    }
    locations: dict[str, list[str]] = {
        "runtime_case": [str(Path(str(assets.get("case_dir") or "")).resolve())],
        "visualization": [],
        "audit": [],
        "reproducibility": [],
    }

    def inside_package(path: Path) -> Path | None:
        try:
            resolved = path.expanduser().resolve()
            resolved.relative_to(package_root)
            return resolved
        except (OSError, ValueError):
            return None

    def move_asset(key: str, role: str, name: str | None = None, *, copy_only: bool = False) -> None:
        raw = assets.get(key)
        source = inside_package(Path(str(raw))) if raw else None
        if not source or not source.is_file():
            return
        target_root = role_roots[role]
        target_root.mkdir(parents=True, exist_ok=True)
        target = (target_root / (name or source.name)).resolve()
        if source != target:
            if target.exists():
                target.unlink()
            if copy_only:
                shutil.copy2(source, target)
                value = str(target)
                written = [str(path) for path in assets.get("written_files") or []]
                if value not in written:
                    written.append(value)
                assets["written_files"] = written
            else:
                shutil.move(str(source), str(target))
                for asset_key, value in list(assets.items()):
                    if not isinstance(value, (str, Path)):
                        continue
                    try:
                        if Path(str(value)).expanduser().resolve() == source:
                            assets[asset_key] = str(target)
                    except OSError:
                        continue
                _replace_written_path(assets, source, target)
        locations[role].append(str(target))

    move_asset("tecplot_file", "visualization", "mesh_surface.dat")
    move_asset("tecplot_volume_file", "visualization", "mesh_volume.dat")
    move_asset("quality_report", "audit", "mesh_quality.json")
    move_asset("mesh_review_report", "audit", "mesh_review.json")
    move_asset("check_mesh_log", "audit", "checkMesh.log")

    has_independent_runtime_mesh = bool(
        assets.get("polyMesh_dir") and Path(str(assets.get("polyMesh_dir"))).is_dir()
    )
    move_asset("geo_file", "reproducibility")
    move_asset(
        "mesh_file",
        "reproducibility",
        copy_only=not has_independent_runtime_mesh,
    )
    move_asset("generator_script", "reproducibility")

    attempts = Path(str(assets.get("mesh_attempts_dir") or assets.get("case_dir") or ""))
    if not assets.get("mesh_attempts_dir"):
        attempts = attempts / "mesh_attempts"
    attempts = inside_package(attempts)
    if attempts and attempts.is_dir():
        target = role_roots["audit"] / "iterations"
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            shutil.rmtree(target)
        source_root = attempts.resolve()
        shutil.move(str(attempts), str(target))
        updated: list[str] = []
        for raw in assets.get("written_files") or []:
            path = Path(str(raw))
            try:
                relative = path.resolve().relative_to(source_root)
            except (OSError, ValueError):
                updated.append(str(raw))
            else:
                updated.append(str((target / relative).resolve()))
        assets["written_files"] = updated
        assets["mesh_attempts_dir"] = str(target.resolve())
        locations["audit"].append(str(target.resolve()))

    case_dir = inside_package(Path(str(assets.get("case_dir") or "")))
    if case_dir and case_dir.is_dir():
        local_audit = case_dir / "audit"
        if local_audit.is_dir():
            target = role_roots["audit"] / "diagnostics"
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                shutil.rmtree(target)
            source_root = local_audit.resolve()
            shutil.move(str(local_audit), str(target))
            updated: list[str] = []
            for raw in assets.get("written_files") or []:
                path = Path(str(raw))
                try:
                    relative = path.resolve().relative_to(source_root)
                except (OSError, ValueError):
                    updated.append(str(raw))
                else:
                    updated.append(str((target / relative).resolve()))
            assets["written_files"] = updated
            locations["audit"].append(str(target.resolve()))
    assets["convergence_asset_locations"] = locations
    return assets


def _stage_has_solver_input(record: dict[str, Any]) -> bool:
    if str(record.get("execution_kind") or "solver_input") != "solver_input":
        return False
    for raw in record.get("generated_files") or []:
        name = str(raw)
        if name.endswith((
            "/runtime_assets.json", "/mesh_reference.json", "/mesh_convergence.json", "/input.pending.json",
        )):
            continue
        return True
    return False


def _mesh_owner_and_consumers(
    package_dir: Path,
    stage_records: list[dict[str, Any]],
) -> tuple[Path, list[Path]] | None:
    mesh_stage = next(
        (record for record in stage_records if any(str(path).endswith(("/runtime_assets.json", "/mesh_reference.json")) for path in record.get("generated_files") or [])),
        None,
    ) or next((record for record in stage_records if _stage_has_solver_input(record)), None)
    if mesh_stage is None:
        return None
    owner = package_dir / _stage_case_root(mesh_stage)
    consumers = [owner]
    for record in stage_records:
        if record.get("status") not in {
            "ready", "candidate_generated", "deferred_dependency",
        }:
            continue
        variant_roots = [
            str(value).strip()
            for value in record.get("variant_case_roots") or []
            if str(value).strip()
        ]
        if variant_roots:
            consumers.extend(package_dir / root for root in variant_roots)
        elif _stage_has_solver_input(record):
            consumers.append(package_dir / _stage_case_root(record))
    return owner, list(dict.fromkeys(consumers))


def _runtime_case_asset_candidates(package_dir: Path, assets: dict[str, Any]) -> list[Path]:
    """Find runtime input assets without tying package layout to a discipline."""
    suffixes = {".msh", ".mesh", ".med", ".unv", ".cgns", ".bdf", ".nas", ".node", ".ele", ".vtu", ".vtk", ".xdmf", ".data"}
    candidates: list[Path] = []
    for key in ("runtime_assets", "runtime_input_files", "mesh_runtime_files", "mesh_file", "structure_file", "data_file"):
        value = assets.get(key)
        values = value if isinstance(value, list) else [value]
        for raw in values:
            if raw:
                candidates.append(Path(str(raw)))
    for raw in assets.get("written_files") or []:
        path = Path(str(raw))
        if path.suffix.lower() in suffixes:
            candidates.append(path)
    unique: list[Path] = []
    for path in candidates:
        try:
            resolved = path.expanduser().resolve()
            resolved.relative_to(package_dir.resolve())
        except (OSError, ValueError):
            continue
        if resolved.is_file() and resolved not in unique:
            unique.append(resolved)
    return unique


def _materialize_stage_local_meshes(
    package_dir: Path,
    assets: dict[str, Any] | None,
    stage_records: list[dict[str, Any]],
) -> list[Path]:
    """Make every runnable stage self-contained using mesh asset roles, not discipline names."""
    if not assets or not stage_records:
        return []
    placement = _mesh_owner_and_consumers(package_dir, stage_records)
    if placement is None:
        return []
    owner_root, stage_roots = placement
    delivered: list[Path] = []
    raw_poly_mesh = assets.get("polyMesh_dir")
    source = Path(str(raw_poly_mesh)).expanduser() if raw_poly_mesh else None
    has_openfoam_runtime_mesh = bool(source and source.is_dir())
    native_mesh_source: Path | None = None
    if assets.get("mesh_file"):
        try:
            native_mesh_source = Path(str(assets["mesh_file"])).expanduser().resolve()
        except OSError:
            pass
    if has_openfoam_runtime_mesh:
        diagnostic_set = source / "sets" / "twoInternalFacesCells"
        if diagnostic_set.is_dir():
            shutil.rmtree(diagnostic_set)
        elif diagnostic_set.is_file():
            diagnostic_set.unlink()
        owner_poly = owner_root / "constant" / "polyMesh"
        owner_poly.parent.mkdir(parents=True, exist_ok=True)
        if source.resolve() != owner_poly.resolve():
            source_root = source.resolve()
            if owner_poly.exists():
                shutil.rmtree(owner_poly)
            root_generated_poly = (package_dir / "constant" / "polyMesh").resolve()
            if source_root == root_generated_poly:
                shutil.move(str(source), str(owner_poly))
                assets["written_files"] = [
                    str((owner_poly / path.resolve().relative_to(source_root)).resolve())
                    if source_root in path.resolve().parents else str(raw)
                    for raw in assets.get("written_files") or []
                    for path in [Path(str(raw))]
                ]
            else:
                # A convergence reference remains independently runnable in
                # its own variant directory; consumers receive copies.
                shutil.copytree(source, owner_poly)
        assets["polyMesh_dir"] = str(owner_poly.resolve())
        assets["openfoam_case_dir"] = str(owner_root.resolve())
        for case_root in stage_roots:
            case_poly = case_root / "constant" / "polyMesh"
            if case_root != owner_root and not case_poly.exists():
                case_poly.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(owner_poly, case_poly)
            marker = case_root / "case.foam"
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text("// OpenFOAM case marker\n", encoding="utf-8")
            delivered.append(marker)
            delivered.extend(path for path in (case_poly / name for name in ("points", "faces", "owner", "neighbour", "boundary")) if path.is_file())
        assets["openfoam_case_dirs"] = [str(path.resolve()) for path in stage_roots]

    # Native runtime inputs are copied into each runnable case.
    # Preserve their root-relative name where possible; otherwise use mesh/.
    for source_file in _runtime_case_asset_candidates(package_dir, assets):
        # Once an OpenFOAM polyMesh has been materialized, the mesher exchange
        # file is provenance rather than a runtime dependency.  Keep its single
        # reproducibility copy instead of duplicating a large .msh in every case.
        if has_openfoam_runtime_mesh and native_mesh_source is not None and source_file == native_mesh_source:
            continue
        try:
            relative = source_file.relative_to(package_dir.resolve())
        except ValueError:
            relative = Path("mesh") / source_file.name
        if relative.parts and relative.parts[0] == "reproducibility":
            relative = Path("mesh") / source_file.name
        for case_root in stage_roots:
            target = case_root / relative
            if target.resolve() == source_file.resolve():
                delivered.append(target)
                continue
            if not target.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source_file, target)
            delivered.append(target)
    root_constant = package_dir / "constant"
    if root_constant.is_dir() and not any(root_constant.iterdir()):
        root_constant.rmdir()
    for record in stage_records:
        root = package_dir / _stage_case_root(record)
        try:
            root_relative = root.resolve().relative_to(package_dir.resolve())
        except ValueError:
            continue
        for path in delivered:
            try:
                relative = path.resolve().relative_to(package_dir.resolve())
            except ValueError:
                continue
            if relative == root_relative or root_relative in relative.parents:
                _record_generated_file(record, str(relative))
    assets.setdefault("written_files", []).extend(str(path.resolve()) for path in delivered)
    return delivered


def _asset_variant_token(value: Any) -> str:
    """Normalize a plan asset identity without assuming a scientific domain."""
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def _asset_variant_aliases(
    assets: dict[str, Any] | None,
    metadata: dict[str, Any] | None = None,
) -> set[str]:
    aliases: set[str] = set()
    sources = [assets or {}, metadata or {}]
    for source in sources:
        for key in (
            "variant_id", "asset_variant_id", "profile_name", "geometry_name",
            "airfoil_name", "naca_code", "coordinate_profile_path",
            "profile_dat_path", "airfoil_dat_path", "geometry_file",
            "geometry_path", "source_path",
        ):
            raw = source.get(key)
            if not raw:
                continue
            values = [raw, Path(str(raw)).stem] if "path" in key or "file" in key else [raw]
            for value in values:
                token = _asset_variant_token(value)
                if token:
                    aliases.add(token)
                    if key == "naca_code" and token.isdigit():
                        aliases.add(f"naca{token}")
    return aliases


def _materialize_stage_runtime_asset_variants(
    package_dir: Path,
    main_assets: dict[str, Any] | None,
    variant_results: list[dict[str, Any]],
    stage_records: list[dict[str, Any]],
    *,
    main_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Route generated assets to stages by declared identity.

    A stage requesting another geometry/model must never silently inherit the
    main runtime asset.  The same identity-based routing can be reused for
    structures, meshes, models, and other parameter-space variants.
    """
    if not stage_records:
        return {"delivered": [], "unresolved": []}

    main_aliases = _asset_variant_aliases(main_assets, main_metadata)
    available: list[tuple[set[str], dict[str, Any], dict[str, Any]]] = []
    for variant in variant_results or []:
        result = variant.get("result") if isinstance(variant.get("result"), dict) else {}
        if (
            variant.get("status") != "success"
            or result.get("status") != "success"
            or result.get("deliverable_valid", True) is False
        ):
            continue
        aliases = _asset_variant_aliases(result, variant)
        if aliases:
            available.append((aliases, result, variant))

    main_records: list[dict[str, Any]] = []
    variant_groups: dict[int, list[dict[str, Any]]] = {}
    unresolved: list[dict[str, str]] = []
    for record in stage_records:
        requested = _asset_variant_token(record.get("asset_variant_id"))
        if not requested or requested in main_aliases:
            main_records.append(record)
            continue
        matched_index = next(
            (index for index, (aliases, _, _) in enumerate(available) if requested in aliases),
            None,
        )
        if matched_index is not None:
            variant_groups.setdefault(matched_index, []).append(record)
            continue
        missing = f"runtime asset variant: {record.get('asset_variant_id')}"
        record["missing_inputs"] = list(dict.fromkeys([
            *(record.get("missing_inputs") or []),
            missing,
        ]))
        record["status"] = "blocked_missing_inputs"
        unresolved.append({
            "stage_id": str(record.get("id") or ""),
            "asset_variant_id": str(record.get("asset_variant_id") or ""),
        })

    delivered: list[Path] = []
    if main_records and main_assets:
        delivered.extend(_materialize_stage_local_meshes(package_dir, main_assets, main_records))
    for index, records in variant_groups.items():
        _, assets, _ = available[index]
        delivered.extend(_materialize_stage_local_meshes(package_dir, assets, records))
    return {
        "delivered": [str(path.resolve()) for path in delivered],
        "unresolved": unresolved,
        "main_aliases": sorted(main_aliases),
        "variant_group_count": len(variant_groups),
    }


def _organize_mesh_auxiliary_files(package_dir: Path, assets: dict[str, Any] | None) -> dict[str, Any] | None:
    if not assets:
        return assets
    audit_dir = package_dir / "audit"
    names = {
        "quality_report": ("audit", "mesh_quality.json"),
        "mesh_review_report": ("audit", "mesh_review.json"),
        "check_mesh_log": ("audit", "checkMesh.log"),
        "tecplot_file": ("visualization", "mesh_surface.dat"),
        "tecplot_volume_file": ("visualization", "mesh_volume.dat"),
    }
    for key, (folder, name) in names.items():
        source = Path(str(assets.get(key) or package_dir / name))
        if not source.is_file():
            continue
        target = package_dir / folder / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.resolve() != target.resolve():
            if target.exists():
                target.unlink()
            if source.parent.resolve() == package_dir.resolve():
                shutil.move(str(source), str(target))
            else:
                # Preserve stage-local quality evidence for a convergence
                # reference while publishing a canonical audit copy.
                shutil.copy2(source, target)
        _replace_written_path(assets, source, target)
        assets[key] = str(target.resolve())
        if key == "tecplot_file":
            assets["tecplot_surface_file"] = assets[key]
    attempts = package_dir / "mesh_attempts"
    if attempts.is_dir():
        audit_dir.mkdir(parents=True, exist_ok=True)
        target = audit_dir / "mesh_attempts"
        if target.exists():
            shutil.rmtree(target)
        shutil.move(str(attempts), str(target))
        updated: list[str] = []
        for raw in assets.get("written_files") or []:
            path = Path(str(raw))
            try:
                relative = path.resolve().relative_to(attempts.resolve())
            except ValueError:
                updated.append(str(raw))
            else:
                updated.append(str((target / relative).resolve()))
        assets["written_files"] = updated
        assets["mesh_attempts_dir"] = str(target.resolve())
    return assets


def _write_mesh_delivery_index(package_dir: Path, assets: dict[str, Any] | None) -> dict[str, Any] | None:
    if not assets:
        return None
    poly_dir = Path(str(assets.get("polyMesh_dir") or ""))
    surface = Path(str(assets.get("tecplot_file") or ""))
    if not poly_dir.exists() or not surface.is_file() or surface.stat().st_size <= 0:
        return None

    canonical_surface = surface
    canonical_volume = Path(str(assets.get("tecplot_volume_file") or package_dir / "visualization" / "mesh_volume.dat"))
    core_files = {
        name: poly_dir / name
        for name in ("points", "faces", "owner", "neighbour", "boundary")
    }
    openfoam_case_dir = Path(str(assets.get("openfoam_case_dir") or poly_dir.parent.parent)).resolve()
    foam_marker = openfoam_case_dir / "case.foam"
    foam_marker.write_text("// OpenFOAM case marker\n", encoding="utf-8")
    mesh_review = assets.get("mesh_review") or {}
    grid_params = assets.get("grid_params") or {}
    actual_cell_count = (
        (assets.get("openfoam_validation") or {}).get("n_cells")
        or _mesh_result_cell_count(assets)
    )
    index = {
        "status": "ready",
        "openfoam_case_dir": str(openfoam_case_dir),
        "openfoam_case_dirs": assets.get("openfoam_case_dirs") or [str(openfoam_case_dir)],
        "polyMesh_dir": str(poly_dir.resolve()),
        "polyMesh_files": {
            name: {"path": str(path.resolve()), "bytes": path.stat().st_size}
            for name, path in core_files.items()
        },
        "tecplot_surface": {
            "path": str(canonical_surface.resolve()),
            "bytes": canonical_surface.stat().st_size,
        },
        "openfoam_marker": str(foam_marker.resolve()),
        "mesh_review_status": mesh_review.get("status"),
        "mesh_selection": {
            "mode": (
                "mesh_convergence_reference"
                if assets.get("mesh_convergence_role") == "reference"
                else "single_mesh"
            ),
            "level": assets.get("mesh_convergence_level"),
            "downstream_default": bool(assets.get("downstream_default", False)),
            "target_cell_count": grid_params.get("target_cell_count"),
            "actual_cell_count": actual_cell_count,
            "research_plan_review_status": mesh_review.get("status"),
            "plan_contract": mesh_review.get("plan_contract") or {},
        },
        "grid_parameters": grid_params,
        "parameter_basis": assets.get("parameter_basis") or {},
        "openfoam_validation": assets.get("openfoam_validation"),
        "deliverable_valid": bool(assets.get("deliverable_valid", True)),
    }
    if canonical_volume.is_file():
        index["tecplot_volume"] = {
            "path": str(canonical_volume.resolve()),
            "bytes": canonical_volume.stat().st_size,
        }
    audit_dir = package_dir / "audit"
    audit_dir.mkdir(parents=True, exist_ok=True)
    index_path = audit_dir / "mesh_delivery.json"
    index_path.write_text(_json_dumps(index), encoding="utf-8")
    assets["tecplot_file"] = str(canonical_surface.resolve())
    assets["mesh_delivery_index"] = str(index_path.resolve())
    assets["openfoam_marker"] = str(foam_marker.resolve())
    for path in (canonical_surface, canonical_volume, foam_marker, index_path, *core_files.values()):
        if not path.exists():
            continue
        value = str(path.resolve())
        if value not in assets["written_files"]:
            assets["written_files"].append(value)
    return index


async def _build_scientific_preprocessing_package(
    state: State,
    spec: str,
    discipline: str = "",
    data_path: str = "",
    output_name: str = "",
    parameters: str = "",
    generate_model_assets: bool = False,
    generate_mesh_assets: bool | None = None,
    **extra: Any,
) -> dict:
    emit_progress(state, "package", "building scientific preprocessing package")
    if not spec.strip():
        return {
            "status": "error",
            "error": "spec is required. Provide the scientific task, geometry/data, target solver, and expected downstream use.",
        }

    params, parse_error = _parse_json_object(parameters, "parameters")
    if parse_error:
        return {"status": "error", "error": parse_error}
    params = flatten_parameter_groups(params)
    for key, value in extra.items():
        if key not in params and value is not None:
            params[key] = value
    if not data_path:
        downloaded_datasets = downloaded_dataset_candidates(state)
        expected_kind = str(params.get("data_model_kind") or "").strip().lower()
        if expected_kind:
            matching = [
                item for item in downloaded_datasets
                if str(item.get("data_model_kind") or "").lower() == expected_kind
            ]
            if len(matching) == 1:
                downloaded_datasets = matching
        if len(downloaded_datasets) == 1:
            selected_dataset = downloaded_datasets[0]
            data_path = str(selected_dataset["path"])
            trace = params.setdefault("source_trace", [])
            if not isinstance(trace, list):
                trace = [trace]
                params["source_trace"] = trace
            trace.append({
                "source": "approved_public_download",
                "path": data_path,
                "url": selected_dataset.get("url"),
                "sha256": selected_dataset.get("sha256"),
                "decision": "use_unambiguous_downloaded_dataset",
            })
    if params.get("domain_specific_generation_deferred") is True:
        generate_mesh_assets = False
        generate_model_assets = False
    _apply_downloaded_geometry_assets_to_params(state, spec, params)
    original_spec = original_user_request_text_from_state(state)
    intent_spec = "\n".join(part for part in (original_spec, spec) if part)
    if not isinstance(params.get("requirement_analysis"), dict):
        params.update({k: v for k, v in _extract_numbers(spec).items() if k not in params})
        params.update({k: v for k, v in _extract_airfoil_mesh_density_numbers(spec).items() if k not in params})
    naca_code = _extract_naca_code(
        spec,
        params.get("naca_code"),
        params.get("airfoil"),
        params.get("geometry"),
        params.get("case_name"),
        output_name,
    )
    if naca_code:
        params["naca_code"] = naca_code
    airfoil_name = _extract_airfoil_name(spec, params)
    if airfoil_name:
        params.setdefault("airfoil_name", airfoil_name)
    if "angle_of_attack" not in params:
        for alias in ("aoa", "aoa_deg", "alpha_deg", "alpha"):
            if alias in params:
                params["angle_of_attack"] = params[alias]
                break

    detected = identify_discipline(
        file_path=data_path or None,
        text_sample=spec,
        metadata=params or None,
    )
    reported_discipline, selected_discipline, declared_discipline = _package_discipline_identity(
        discipline,
        detected,
        spec,
        params,
    )
    if selected_discipline == "electronic_structure" and "generate_model_assets" not in params:
        generate_model_assets = False
    material_solver = (
        _material_solver(spec, params)
        if selected_discipline == "electronic_structure"
        else ""
    )
    defaults = (
        _material_defaults(DISCIPLINE_DEFAULTS[selected_discipline], material_solver)
        if selected_discipline == "electronic_structure"
        else DISCIPLINE_DEFAULTS[selected_discipline]
    )
    if selected_discipline == "electronic_structure":
        params.setdefault("simulation_software", defaults["solver_family"])
    calculation_stages = _normalize_calculation_stages(spec, params)
    requirement_analysis = params.get("requirement_analysis")
    requirement_analysis = requirement_analysis if isinstance(requirement_analysis, dict) else {}
    declared_solver = (requirement_analysis.get("simulation_software") or {}).get("name")
    if declared_solver and str(declared_solver).lower() not in {"unknown", "unspecified"}:
        defaults = {**defaults, "solver_family": declared_solver}
    preprocessing_request = requirement_analysis.get("preprocessing_request")
    if not isinstance(preprocessing_request, dict) or not preprocessing_request:
        preprocessing_request = normalize_preprocessing_request({
            "user_request": spec,
            "required_files": params.get("required_files") or [],
        })
    preprocessing_work_order = requirement_analysis.get("preprocessing_work_order")
    if not isinstance(preprocessing_work_order, dict) or not preprocessing_work_order:
        preprocessing_work_order = build_preprocessing_work_order(
            preprocessing_request,
            requirement_analysis,
        )
    review_profile = str(preprocessing_request.get("review_profile") or "request_bound")
    # The caller-declared stage scope is the source of truth for generated mesh
    # controls. This occurs before any mesher/router call, so no designer/critic
    # cycle is needed for an ordinary local repair.
    params = _apply_mesh_stage_requirements(
        params,
        calculation_stages,
    )
    emit_progress(
        state,
        "package_discipline",
        selected_discipline,
        solver=defaults["solver_family"],
    )
    if generate_mesh_assets is None:
        generate_mesh_assets = _explicit_mesh_assets_requested(spec, selected_discipline, params)
    else:
        generate_mesh_assets = bool(generate_mesh_assets)
    cfd_profile = _match_cfd_case_profile(spec, params) if selected_discipline == "cfd" else None
    mesh_type = _infer_mesh_type(selected_discipline, spec, params)
    provided_mesh_result = params.get("mesh_generation_result")
    if isinstance(provided_mesh_result, dict):
        # Execution bookkeeping belongs to the checkpoint, not to consumers
        # or recursively embedded package manifests.
        provided_mesh_result = {key: value for key, value in provided_mesh_result.items()
                                if key not in {"effective_arguments", "repair_history"}}
        params["mesh_generation_result"] = provided_mesh_result
    if isinstance(provided_mesh_result, dict) and provided_mesh_result.get("status") == "success":
        params.setdefault(
            "case_dir",
            provided_mesh_result.get("case_dir")
            or provided_mesh_result.get("package_dir"),
        )
    existing_mesh_assets = None
    if (
        selected_discipline == "cfd"
        and isinstance(provided_mesh_result, dict)
        and provided_mesh_result.get("status") == "success"
        and provided_mesh_result.get("deliverable_valid", True) is not False
    ):
        existing_mesh_assets = provided_mesh_result
    elif selected_discipline == "cfd":
        existing_mesh_assets = _detect_existing_cfd_mesh_assets(params.get("case_dir"))
    if existing_mesh_assets:
        params["case_dir"] = existing_mesh_assets["case_dir"]
        params.setdefault("computational_domain_complete", True)
        params.setdefault("domain_shape_verified", True)
        params.setdefault("geometry_representation", "reference_computational_mesh")
        reviewed_mesh_type = str((existing_mesh_assets.get("mesh_review") or {}).get("mesh_type") or "").strip()
        if not reviewed_mesh_type:
            reviewed_mesh_type = str(existing_mesh_assets.get("mesh_type") or "").strip()
        if reviewed_mesh_type:
            mesh_type = reviewed_mesh_type
    special_domain = (
        detect_special_domain_requirements(
            intent_spec,
            params,
            case_type=str(params.get("case_type") or mesh_type or ""),
            include_complete=True,
        )
        if selected_discipline == "cfd"
        else None
    )
    if not existing_mesh_assets and special_domain and mesh_type in {
        "airfoil_gmsh",
        "airfoil_ogrid",
        "coordinate_profile_gmsh",
        "structured_rect",
        "cylinder_flow",
        "cylinder_gmsh",
        "sphere",
        "pipe_flow",
        "converging_diverging_nozzle",
    }:
        if not special_domain.get("missing_fields"):
            special_domain = {
                **special_domain,
                "missing_fields": ["适配该特殊拓扑的 geometry_file/mesh_type 或专用网格生成路径"],
            }
        return special_domain_reference_request(intent_spec, params, special_domain)
    if (
        selected_discipline == "cfd"
        and not existing_mesh_assets
        and mesh_type in {"airfoil_gmsh", "airfoil_ogrid"}
        and not original_intent_allows_airfoil_mesh(state, spec, params)
    ):
        return public_geometry_reference_request(intent_spec, params, case_type="public_geometry")
    if (
        selected_discipline == "cfd"
        and not existing_mesh_assets
        and not _canonical_cfd_mesh_matches_original_intent(state, mesh_type, intent_spec, params)
    ):
        return public_geometry_reference_request(intent_spec, params, case_type="public_geometry")
    if selected_discipline == "cfd" and not existing_mesh_assets and geometry_parameters_need_reference_resolution(params):
        advisor_result = await _resolve_mesh_iteration_inputs(
            state,
            spec=spec,
            case_type=str(params.get("case_type") or mesh_type or ""),
            parameters=_json_dumps(params),
        )
        if advisor_result.get("status") in {"needs_input", "needs_reference_search", "needs_geometry_processing", "error"}:
            return advisor_result
        if advisor_result.get("status") == "success":
            params.update(advisor_result.get("resolved_parameters") or {})
            mesh_type = _infer_mesh_type(selected_discipline, spec, params)
    if selected_discipline == "cfd":
        _apply_cfd_profile_defaults(params, cfd_profile)
        if not existing_mesh_assets and mesh_type in {"public_geometry", "custom_geometry", "generic_gmsh"} and not params.get("geometry_file"):
            return public_geometry_reference_request(spec, params, case_type=str((cfd_profile or {}).get("case_type") or mesh_type))
    if (
        selected_discipline == "cfd"
        and mesh_type == "airfoil_gmsh"
        and looks_like_turbomachinery_blade(intent_spec, params)
        and not str(params.get("isolated_blade_approximation_confirmed") or "").lower() in {"1", "true", "yes"}
        and not _has_turbomachinery_cascade_domain(params)
    ):
        return public_geometry_reference_request(intent_spec, params, case_type="public_geometry")
    if (
        selected_discipline == "cfd"
        and mesh_type == "coordinate_profile_gmsh"
        and looks_like_turbomachinery_blade(intent_spec, params)
        and not _has_turbomachinery_cascade_domain(params)
    ):
        return public_geometry_reference_request(intent_spec, params, case_type="public_geometry")
    if selected_discipline == "cfd" and mesh_type == "coordinate_profile_gmsh":
        special_profile_domain = detect_special_domain_requirements(
            intent_spec,
            params,
            case_type=str(params.get("case_type") or "coordinate_profile_gmsh"),
            include_complete=True,
        )
        if special_profile_domain:
            if not special_profile_domain.get("missing_fields"):
                special_profile_domain = {
                    **special_profile_domain,
                    "missing_fields": ["适配该特殊拓扑的 profile 通道/周期域生成器"],
                }
            return special_domain_reference_request(intent_spec, params, special_profile_domain)
    if selected_discipline == "cfd" and mesh_type in {"airfoil_gmsh", "airfoil_ogrid"}:
        if not original_intent_allows_airfoil_mesh(state, spec, params):
            return public_geometry_reference_request(intent_spec, params, case_type="public_geometry")
        params.setdefault("mesh_type", mesh_type)
        params.setdefault("quality_preset", "robust")
        params.setdefault("convert_to_openfoam", True)
        params.setdefault("write_tecplot", True)
        for key in ("airfoil_dat_path", "airfoil_coordinate_text", "airfoil_coordinates"):
            if key in params and is_placeholder_value(params.get(key)):
                params.pop(key, None)
        if (
            not _has_airfoil_coordinates(params)
            and not existing_mesh_assets
            and (
                looks_like_turbomachinery_blade(intent_spec, params)
                or looks_like_public_reference_request(spec)
            )
        ):
            return public_geometry_reference_request(intent_spec, params, case_type="public_geometry")
        if airfoil_name and not naca_code and not _has_airfoil_coordinates(params) and not existing_mesh_assets:
            return _airfoil_parameter_prompt(airfoil_name, params)
        if not naca_code and not _has_airfoil_coordinates(params) and not existing_mesh_assets:
            return _naca_or_profile_required_prompt(params)

    case_name = _slugify(output_name or params.get("case_name", "") or spec)
    package_dir, final_package_dir = prepare_package_staging(
        state,
        str(preprocessing_request.get("delivery_name") or ""),
    )
    mesh_delivery: dict[str, Any] | None = None

    dataset_profile: dict[str, Any] = {}
    if data_path:
        source_data_path = Path(data_path).expanduser().resolve()
        data_workspace = Path(
            str(getattr(state, "workspace_root", None) or state.root)
        ).expanduser().resolve()
        download_roots = [
            (Path(str(state.root)) / ".data_node_work" / "downloads").resolve(),
            (data_workspace / "data_preprocessing" / "public_downloads").resolve(),
        ]
        if (
            source_data_path.is_file()
            and any(root in source_data_path.parents for root in download_roots)
            and package_dir.resolve() not in source_data_path.parents
            and inspect_scientific_asset_path(source_data_path).get("asset_kind") == "dataset"
        ):
            packaged_data_dir = package_dir / "data"
            packaged_data_dir.mkdir(parents=True, exist_ok=True)
            packaged_data_path = packaged_data_dir / source_data_path.name
            shutil.copy2(source_data_path, packaged_data_path)
            data_path = str(packaged_data_path.resolve())
            params["packaged_data_relative_path"] = str(packaged_data_path.relative_to(package_dir))
        dataset_profile = _read_scientific_asset_profile(Path(data_path).expanduser())

    files: dict[str, str] = {}
    input_params = {**params, "mesh_type": mesh_type}
    potcar_source = (
        _discover_potcar_library(state, spec, params)
        if selected_discipline == "electronic_structure" and material_solver == "vasp"
        else None
    )
    potcar_provenance: dict[str, Any] | None = None
    if potcar_source:
        emit_progress(
            state,
            "potcar_library",
            "authorized local pseudopotential library found",
            kind=potcar_source.get("kind"),
            path=potcar_source.get("path"),
        )
    write_discipline_templates = _should_write_discipline_templates(selected_discipline, params)
    request_bound_asset_only = (
        review_profile == "request_bound"
        and not _request_bound_needs_solver_templates(params)
    )
    poscar_recovery_result: dict[str, Any] | None = None
    if write_discipline_templates and (
        not calculation_stages or selected_discipline == "electronic_structure"
    ):
        if selected_discipline == "electronic_structure":
            if material_solver == "vasp":
                files.update(_vasp_files({
                    **params,
                    "calculation_type": params.get("calculation_type") or spec,
                }, potcar_source))
            else:
                files.update(_provided_material_files(params))
        else:
            files.update(_simulation_stage_generator(selected_discipline, input_params))
    if not write_discipline_templates and selected_discipline == "electronic_structure":
        if material_solver == "vasp":
            files.update(_vasp_files({
                **params,
                "calculation_type": params.get("calculation_type") or spec,
            }, potcar_source))
            if not (params.get("poscar_text") or params.get("structure_file") or params.get("poscar_path")):
                files.pop("POSCAR", None)
        else:
            files.update(_provided_material_files(params))
    elif (
        not write_discipline_templates
        and selected_discipline != "electronic_structure"
        and not calculation_stages
        and not request_bound_asset_only
    ):
        files["coupling_manifest.json"] = _json_dumps({
            "status": "available_inputs_and_blocked_contract",
            "discipline": reported_discipline,
            "message": "No domain-specific solver template was written because the approved plan did not request one explicitly.",
            "required_files": sorted(_explicit_required_file_names(params)),
            "unresolved_facts": (params.get("requirement_analysis") or {}).get("unresolved_facts", [])
            if isinstance(params.get("requirement_analysis"), dict) else [],
        })
    if selected_discipline == "electronic_structure" and material_solver == "vasp" and "POSCAR" not in files:
        poscar_recovery_result = await _recover_poscar_from_local_references(state, package_dir, spec, params)
        if poscar_recovery_result and poscar_recovery_result.get("status") == "success":
            try:
                files["POSCAR"] = Path(poscar_recovery_result["candidate_path"]).read_text(encoding="utf-8")
            except OSError:
                pass
    if selected_discipline == "electronic_structure" and material_solver == "vasp" and "POSCAR" in files:
        poscar_species = _poscar_species_from_text(files["POSCAR"])
        if poscar_species:
            potcar, potcar_provenance = _assemble_potcar(
                poscar_species,
                potcar_source,
                params,
            )
            if potcar is not None:
                files["POTCAR"] = potcar
            else:
                files.pop("POTCAR", None)
    stage_records: list[dict[str, Any]] = []
    base_material_poscar = files.get("POSCAR", "")
    if selected_discipline == "electronic_structure" and calculation_stages:
        stage_files, stage_records = _material_stage_files(
            material_solver,
            calculation_stages,
            params,
            files,
            spec,
            potcar_source,
        )
        files.update(stage_files)
        _refresh_workflow_manifest(files, defaults, stage_records)
        for root_input in (
            "INCAR", "KPOINTS", "POSCAR", "POTCAR", "VASP_INPUTS_README.md",
        ):
            files.pop(root_input, None)
            root_path = package_dir / root_input
            if root_path.is_file():
                root_path.unlink()
    model_plan = {
        "enabled": bool(generate_model_assets),
        "purpose": "surrogate/regression model with scientific feature audit",
        "data_path": data_path,
        "target_column": params.get("target_column") or (dataset_profile.get("candidate_targets") or [None])[0],
        "feature_policy": "use numeric columns only; preserve units and dimensional meaning in metadata",
        "validation": "holdout split when scikit-learn is available; otherwise training diagnostics only",
        "scientific_checks": [
            "record units for every feature and target",
            "check extrapolation bounds against training ranges",
            "report feature importance or coefficients with physical interpretation",
            "do not treat high R2 as mechanistic validation",
        ],
    }
    if generate_model_assets:
        files["model/train_model.py"] = _training_script()
        files["model/model_plan.json"] = _json_dumps(model_plan)

    mesh_generation_result: dict[str, Any] | None = (
        dict(provided_mesh_result)
        if isinstance(provided_mesh_result, dict)
        else None
    )
    mesh_variant_results: list[dict[str, Any]] = []
    if mesh_generation_result is None and existing_mesh_assets:
        mesh_generation_result = existing_mesh_assets
    elif mesh_generation_result is None and generate_mesh_assets:
        try:
            from .scientific_mesh import prepare_scientific_mesh

            mesh_generation_result = await prepare_scientific_mesh(
                state=state,
                spec=spec,
                discipline=selected_discipline,
                mesh_type=mesh_type,
                case_dir="",
                parameters=_json_dumps(params),
                operation="prepare",
                generate_mesh=True,
            )
        except Exception as exc:
            mesh_generation_result = {
                "status": "error",
                "error": f"{type(exc).__name__}: {exc}",
            }
        if (
            selected_discipline == "cfd"
            and mesh_generation_result
            and mesh_generation_result.get("status") == "success"
        ):
            from .profile_mesh_variants import generate_cfd_profile_mesh_variants

            mesh_variant_results = await generate_cfd_profile_mesh_variants(
                state=state,
                selected_discipline=selected_discipline,
                params=params,
                package_dir=package_dir,
                mesh_generation_result=mesh_generation_result,
                calculation_stages=calculation_stages,
            )

    if mesh_generation_result and mesh_generation_result.get("status") == "success":
        mesh_generation_result = _stage_runtime_mesh_asset(mesh_generation_result, package_dir)
        mesh_generation_result = _organize_mesh_reproducibility_files(package_dir, mesh_generation_result)
        params = _inherit_reusable_mesh_provenance(params, mesh_generation_result)
        # A caller may explicitly request the analytic profile alongside the
        # mesh.  Materialize that declared file from the same NACA parameters
        # used by the mesher; do not infer or add it for mesh-only requests.
        for asset in requirement_analysis.get("required_files") or []:
            if not isinstance(asset, dict) or str(normalize_asset_contract(asset).get("representation") or "") != "coordinate_profile":
                continue
            raw_name = str(
                asset.get("expected_filename")
                or asset.get("declared_output_path")
                or asset.get("name_or_role")
                or ""
            ).replace("\\", "/")
            relative = Path(raw_name)
            if not raw_name or relative.is_absolute() or ".." in relative.parts:
                continue
            target = package_dir / relative
            if target.exists():
                continue
            code = str(params.get("naca_code") or "").strip()
            if not re.fullmatch(r"\d{4,5}", code):
                continue
            from .mesh_generator import naca_4digit_closed_loop

            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(
                "\n".join(f"{x:.10g} {y:.10g}" for x, y in naca_4digit_closed_loop(code)) + "\n",
                encoding="utf-8",
            )
    preliminary_mesh_generation_result = mesh_generation_result

    mesh_convergence_variant_results: dict[str, list[dict[str, Any]]] = {}
    if (
        mesh_generation_result
        and mesh_generation_result.get("status") == "success"
        and calculation_stages
    ):
        mesh_convergence_variant_results = await _generate_mesh_convergence_variants(
            state=state,
            spec=intent_spec,
            selected_discipline=selected_discipline,
            params=params,
            package_dir=package_dir,
            calculation_stages=calculation_stages,
            mesh_generation_result=mesh_generation_result,
        )
        convergence_reference_mesh = _reference_mesh_from_convergence_variants(
            mesh_convergence_variant_results
        )
        if convergence_reference_mesh:
            _remove_superseded_mesh_assets(
                package_dir,
                preliminary_mesh_generation_result,
                convergence_reference_mesh,
            )
            mesh_generation_result = convergence_reference_mesh

    if mesh_generation_result and (
        mesh_generation_result.get("status") != "success"
        or mesh_generation_result.get("deliverable_valid") is False
        or ((mesh_generation_result.get("mesh_review") or {}).get("status") == "fail")
    ):
        if package_dir.exists() and not any(package_dir.iterdir()):
            package_dir.rmdir()
        return {
            "status": mesh_generation_result.get("status") or "error",
            "error": mesh_generation_result.get("error", "mesh generation/review failed"),
            "mesh_generation_result": mesh_generation_result,
            "message": (
                "网格生成后复核未通过，data 节点没有保存成功 dataset。"
                "请查看 mesh_review/review_iterations 了解失败原因。"
            ),
        }

    if selected_discipline != "electronic_structure" and calculation_stages:
        simulation_stage_files, simulation_stage_records = _simulation_stage_files(
            selected_discipline,
            calculation_stages,
            {**params, "mesh_type": mesh_type},
            mesh_available=bool(
                mesh_generation_result
                and mesh_generation_result.get("status") == "success"
            ),
            mesh_convergence_variants=mesh_convergence_variant_results,
        )
        files.update(simulation_stage_files)
        stage_records = simulation_stage_records
        _refresh_workflow_manifest(
            files,
            defaults,
            stage_records,
        )

    # Runtime assets must exist inside every runnable stage before package
    # review.  Reviewing first made metadata-only mesh stages appear ready and
    # also meant a later review failure prevented polyMesh delivery entirely.
    if mesh_generation_result and mesh_generation_result.get("status") == "success":
        asset_routing = _materialize_stage_runtime_asset_variants(
            package_dir,
            mesh_generation_result,
            mesh_variant_results,
            stage_records,
            main_metadata=params,
        )
        if asset_routing.get("unresolved"):
            files["audit/runtime_asset_routing.json"] = _json_dumps(asset_routing)
        mesh_generation_result = _organize_mesh_auxiliary_files(package_dir, mesh_generation_result)
        if mesh_generation_result.get("polyMesh_dir") and mesh_generation_result.get("tecplot_file"):
            mesh_delivery = _write_mesh_delivery_index(package_dir, mesh_generation_result)
        if stage_records:
            _refresh_workflow_manifest(
                files,
                defaults,
                stage_records,
            )
    deferred_stage_resume = _augment_deferred_stage_resumes(
        files,
        stage_records,
        package_dir=package_dir,
    )
    _refresh_workflow_manifest(
        files,
        defaults,
        stage_records,
        deferred_stage_resume,
    )

    review_context = {
        **params,
        "calculation_stages": calculation_stages,
        "stage_records": stage_records,
        "package_dir": str(package_dir.resolve()),
    }
    # Review belongs to execute_preprocessing_plan.  A regenerated package may
    # receive that executor's bounded RevisionContract; apply only those cited
    # changes here, then return another staged candidate for the same review.
    repair_actions: list[dict[str, Any]] = []
    revision_feedback = params.get("revision_contract")
    revision_issues = (
        revision_feedback.get("issues")
        if isinstance(revision_feedback, dict)
        else revision_feedback
    )
    revision_issues = [
        item for item in (revision_issues or []) if isinstance(item, dict)
    ]
    if revision_issues:
        repairs = _repair_preprocessing_package(
            selected_discipline,
            material_solver,
            files,
            stage_records,
            base_poscar=base_material_poscar,
            potcar_source=potcar_source,
            parameters=params,
            defaults=defaults,
            review={"status": "fail", "issues": revision_issues},
        )
        repair_actions.extend(repairs)
        emit_progress(
            state,
            "package_revision_applied",
            "executor review feedback applied to staged assets",
            repair_count=len(repairs),
        )

    staging_package_dir_abs = package_dir.resolve()
    mesh_written_files = [
        str(Path(path).resolve().relative_to(staging_package_dir_abs))
        for path in (mesh_generation_result or {}).get("written_files", [])
        if (
            Path(path).is_absolute()
            and Path(path).exists()
            and Path(path).resolve().is_relative_to(staging_package_dir_abs)
        )
    ]
    for variant in mesh_variant_results:
        result = variant.get("result") or {}
        for path in result.get("written_files") or []:
            path_obj = Path(str(path))
            if path_obj.is_absolute() and path_obj.exists():
                try:
                    mesh_written_files.append(str(path_obj.resolve().relative_to(staging_package_dir_abs)))
                except ValueError:
                    continue
    for variants in mesh_convergence_variant_results.values():
        for variant in variants:
            result = variant.get("result") or {}
            for path in result.get("written_files") or []:
                path_obj = Path(str(path))
                if path_obj.is_absolute() and path_obj.exists():
                    try:
                        mesh_written_files.append(str(path_obj.resolve().relative_to(staging_package_dir_abs)))
                    except ValueError:
                        continue
    package_dir_abs = final_package_dir.resolve()
    data_path = remap_published_paths(data_path, staging_package_dir_abs, package_dir_abs)
    mesh_generation_result = remap_published_paths(
        mesh_generation_result, staging_package_dir_abs, package_dir_abs
    )
    mesh_variant_results = remap_published_paths(
        mesh_variant_results, staging_package_dir_abs, package_dir_abs
    )
    mesh_convergence_variant_results = remap_published_paths(
        mesh_convergence_variant_results, staging_package_dir_abs, package_dir_abs
    )

    extra_written_names = ["README.md"]
    if params.get("packaged_data_relative_path"):
        extra_written_names.append(str(params["packaged_data_relative_path"]))
    if mesh_delivery:
        extra_written_names.extend([
            "visualization/mesh_surface.dat",
            "visualization/mesh_volume.dat",
            "audit/mesh_delivery.json",
        ])
    package_written_file_names = sorted(set([
        *files.keys(), *mesh_written_files, *extra_written_names,
    ]))
    ready_stage_cases = [
        case_root
        for stage in stage_records
        if stage.get("status") in {"ready", "candidate_generated"}
        or stage.get("variant_case_roots")
        for case_root in (
            stage.get("variant_case_roots") or [_stage_case_root(stage)]
        )
        if case_root
    ]
    blocked_stage_cases = [
        {
            "stage_id": stage.get("id"),
            "case_root": _stage_case_root(stage),
            "status": stage.get("status"),
            "missing_inputs": stage.get("missing_inputs") or [],
            "dependency_requirements": stage.get("dependency_requirements") or [],
            "resume_request": stage.get("resume_request"),
            "pending_contract": next(
                (
                    path for path in stage.get("generated_files") or []
                    if str(path).endswith("/input.pending.json")
                ),
                None,
            ),
        }
        for stage in stage_records
        if str(stage.get("status") or "").startswith("blocked")
        or stage.get("status") == "deferred_dependency"
    ]
    universal_contract = _build_universal_manifest_contract(
        state=state,
        spec=spec,
        data_path=data_path,
        selected_discipline=selected_discipline,
        params=params,
        defaults=defaults,
        dataset_profile=dataset_profile,
        mesh_type=mesh_type,
        mesh_generation_result=mesh_generation_result,
        # The final publish directory is created after review.  Validate the
        # directory that actually contains the staged package, then remap its
        # recorded paths to the published location below.
        package_dir=staging_package_dir_abs,
        written_file_names=package_written_file_names,
    )
    universal_contract = remap_published_paths(
        universal_contract, staging_package_dir_abs, package_dir_abs
    )
    if deferred_stage_resume.get("requests"):
        universal_contract["downstream_contract"].update({
            "deferred_stage_resume": "workflow_manifest.json#deferred_stage_resume",
            "resume_callable_node": "data",
            "resume_request_count": len(deferred_stage_resume["requests"]),
        })
    if mesh_variant_results:
        universal_contract["quality_gates"].append({
            "name": "explicit_profile_mesh_variants",
            "status": (
                "pass"
                if all(
                    item.get("status") == "success"
                    and str(item.get("mesh_review_status") or "").lower() == "pass"
                    and item.get("deliverable_valid", True) is not False
                    for item in mesh_variant_results
                )
                else "fail"
            ),
            "evidence": [
                {
                    "variant_id": item.get("variant_id"),
                    "profile_name": item.get("profile_name"),
                    "mesh_type": item.get("mesh_type"),
                    "case_dir": item.get("case_dir"),
                    "status": item.get("status"),
                    "mesh_review_status": item.get("mesh_review_status"),
                    "tecplot_file": (item.get("result") or {}).get("tecplot_file"),
                    "polyMesh_dir": (item.get("result") or {}).get("polyMesh_dir"),
                }
                for item in mesh_variant_results
            ],
        })
    if mesh_convergence_variant_results:
        all_convergence_variants = [
            item for variants in mesh_convergence_variant_results.values() for item in variants
        ]
        universal_contract["quality_gates"].append({
            "name": "mesh_convergence_variants",
            "status": (
                "pass"
                if all(
                    item.get("status") == "success"
                    and item.get("deliverable_valid", True) is not False
                    and str(item.get("mesh_review_status") or "").lower() == "pass"
                    for item in all_convergence_variants
                )
                else "fail"
            ),
            "evidence": {
                stage_id: [{
                    "id": item.get("id"),
                    "role": item.get("role"),
                    "downstream_default": item.get("downstream_default", False),
                    "target_cell_count": item.get("target_cell_count"),
                    "selected_target_cell_count": item.get("selected_target_cell_count"),
                    "actual_cell_count": item.get("actual_cell_count"),
                    "target_relaxed_for_quality": item.get("target_relaxed_for_quality", False),
                    "case_dir": item.get("case_dir"),
                    "status": item.get("status"),
                    "mesh_review_status": item.get("mesh_review_status"),
                } for item in variants]
                for stage_id, variants in mesh_convergence_variant_results.items()
            },
        })
    manifest = {
        **universal_contract,
        "case_name": case_name,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "generated_by": "nodes/data/tools/scientific_preprocessor.py",
        "spec": spec,
        "discipline": reported_discipline,
        "discipline_adapter": (
            "generic" if selected_discipline == "unknown" and reported_discipline != "unknown"
            else selected_discipline
        ),
        "declared_discipline": declared_discipline,
        "discipline_detection": detected,
        "solver_family": defaults["solver_family"],
        "mesh": {
            "mesh_type": mesh_type,
            "parameters": {key: value for key, value in params.items() if key not in {
                "mesh_generation_result", "requirement_analysis", "preprocessing_request",
                "preprocessing_work_order", "review_issues", "revision_contract", "reference_evidence",
            }},
            "generation_tool": "prepare_scientific_mesh",
            "generation_status": (
                mesh_generation_result.get("status", "success")
                if mesh_generation_result
                else "not_run"
            ),
            "generated_assets": mesh_generation_result or {},
            "explicit_profile_variants": [
                {
                    "variant_id": item.get("variant_id"),
                    "profile_name": item.get("profile_name"),
                    "mesh_type": item.get("mesh_type"),
                    "case_dir": item.get("case_dir"),
                    "status": item.get("status"),
                    "mesh_review_status": item.get("mesh_review_status"),
                    "deliverable_valid": item.get("deliverable_valid"),
                    "tecplot_file": (item.get("result") or {}).get("tecplot_file"),
                    "tecplot_volume_file": (item.get("result") or {}).get("tecplot_volume_file"),
                    "polyMesh_dir": (item.get("result") or {}).get("polyMesh_dir"),
                    "source": item.get("source"),
                    "role": item.get("role"),
                }
                for item in mesh_variant_results
            ],
            "mesh_convergence_variants": {
                stage_id: [{
                    "id": item.get("id"),
                    "role": item.get("role"),
                    "downstream_default": item.get("downstream_default", False),
                    "target_cell_count": item.get("target_cell_count"),
                    "selected_target_cell_count": item.get("selected_target_cell_count"),
                    "actual_cell_count": item.get("actual_cell_count"),
                    "target_relaxed_for_quality": item.get("target_relaxed_for_quality", False),
                    "case_dir": item.get("case_dir"),
                    "status": item.get("status"),
                    "mesh_review_status": item.get("mesh_review_status"),
                    "deliverable_valid": item.get("deliverable_valid"),
                    "polyMesh_dir": (item.get("result") or {}).get("polyMesh_dir"),
                } for item in variants]
                for stage_id, variants in mesh_convergence_variant_results.items()
            },
        },
        "input_deck": {
            "written_files": package_written_file_names,
            "ready_case_roots": ready_stage_cases,
            "incomplete_cases": blocked_stage_cases,
        },
        "dataset_profile": dataset_profile,
        "data_driven_model": model_plan,
        "local_structure_recovery": poscar_recovery_result,
        "pseudopotential_source": (
            {
                "kind": potcar_source.get("kind"),
                "path": potcar_source.get("path"),
                "functional": potcar_source.get("functional"),
                "base_structure_assembly": potcar_provenance,
                "authorization": "user_provided_local_library",
            }
            if potcar_source else {
                "status": "not_found",
                "authorization": "required_for_real_potcar_generation",
            }
        ),
        "request_id": preprocessing_request.get("request_id"),
        "request_spec_hash": preprocessing_request.get("request_spec_hash"),
        "review_profile": review_profile,
        "preprocessing_request": preprocessing_request,
        "preprocessing_work_order": preprocessing_work_order,
        "calculation_stages": stage_records,
        "discipline_quality_requirements": defaults["quality_gates"],
        "handoff_to_experiment": {
            "package_dir": str(package_dir_abs),
            "artifact_type": "dataset",
            "ready_case_roots": ready_stage_cases,
            "incomplete_cases": blocked_stage_cases,
            "deferred_stage_resume": (
                {
                    "contract": "workflow_manifest.json#deferred_stage_resume",
                    "callable_node": "data",
                    "request_count": len(deferred_stage_resume.get("requests") or []),
                }
                if deferred_stage_resume.get("requests") else None
            ),
            "execution_policy": (
                "Run only cases listed in ready_case_roots. Each incomplete case is isolated and "
                "may be resumed after satisfying its input.pending.json contract."
            ),
            "must_verify_before_run": [
                *([
                    "mesh generation status is success",
                    "input deck boundary patch names match generated mesh",
                ] if generate_mesh_assets or mesh_generation_result else []),
                "all dimensional quantities have units",
            ],
        },
    }
    readiness_lines = [
        "This directory is a reproducible preprocessing package for the experiment node.",
    ]
    if mesh_generation_result and mesh_generation_result.get("status") == "success":
        readiness_lines.append("Mesh assets were generated or discovered and indexed in this directory.")
    elif generate_mesh_assets:
        readiness_lines.append("Mesh generation was requested but did not produce a successful mesh asset.")
    if selected_discipline == "electronic_structure":
        readiness_lines.append(
            "Materials-simulation input files are generated only from explicit local/upstream parameters."
        )
        readiness_lines.append(f"Detected materials solver adapter: {material_solver}.")
        if material_solver == "vasp":
            if "POSCAR" in files:
                readiness_lines.append("POSCAR was generated from local structure text/file evidence.")
            else:
                readiness_lines.append("POSCAR is absent because no complete local structure evidence was available.")
    if generate_model_assets:
        readiness_lines.append("For data-driven models, inspect `model/model_plan.json` and run the training script with an explicit data file.")
    if stage_records:
        readiness_lines.append(
            f"Research-plan stages: {len(ready_stage_cases)} ready, "
            f"{len(blocked_stage_cases)} incomplete or deferred."
        )
        readiness_lines.append(
            "Run only the case roots listed in `manifest.json` under "
            "`handoff_to_experiment.ready_case_roots`."
        )
    files["README.md"] = "\n".join([
        f"# {case_name}",
        "",
        f"- Discipline: `{reported_discipline}`",
        *([
            "- Internal adapter: `generic`",
        ] if selected_discipline == "unknown" and reported_discipline != "unknown" else []),
        f"- Solver family: `{defaults['solver_family']}`",
        *([f"- Mesh type: `{mesh_type}`"] if generate_mesh_assets or mesh_generation_result else []),
        *([f"- NACA code: `{params['naca_code']}`"] if params.get("naca_code") else []),
        *([f"- Angle of attack: `{params['angle_of_attack']} deg`"] if params.get("angle_of_attack") is not None else []),
        "",
        *readiness_lines,
        "",
    ])

    _write_package_files(package_dir, files)
    delivery_files: list[dict[str, Any]] = []
    for path in package_dir.rglob("*"):
        if not path.is_file():
            continue
        relative = str(path.relative_to(package_dir))
        if relative in {"manifest.json", "audit/preprocessing_review.json"}:
            continue
        delivery_files.append({
            "id": relative,
            "asset_id": None,
            "path": relative,
            "size": path.stat().st_size,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        })
    staged_files = [path for path in package_dir.rglob("*") if path.is_file()]
    n_written = len(staged_files)
    size_bytes = sum(path.stat().st_size for path in staged_files)

    delivery_fields = {
        "discipline": reported_discipline,
        "discipline_adapter": (
            "generic" if selected_discipline == "unknown" and reported_discipline != "unknown"
            else selected_discipline
        ),
        "mesh_type": mesh_type,
        "solver_family": defaults["solver_family"],
        "data_path": data_path,
        "model_assets": bool(generate_model_assets),
        "mesh_assets": bool(mesh_generation_result and mesh_generation_result.get("status") != "error"),
        "mesh_generation_status": (mesh_generation_result or {}).get("status") if mesh_generation_result else "not_run",
        "mesh_review_status": ((mesh_generation_result or {}).get("mesh_review") or {}).get("status"),
        "polyMesh_dir": (mesh_generation_result or {}).get("polyMesh_dir"),
        "tecplot_surface": (mesh_generation_result or {}).get("tecplot_file"),
        "tecplot_volume": (mesh_generation_result or {}).get("tecplot_volume_file"),
        "mesh_delivery_index": (mesh_generation_result or {}).get("mesh_delivery_index"),
        "mesh_variants": [
            {
                "variant_id": item.get("variant_id"),
                "profile_name": item.get("profile_name"),
                "mesh_type": item.get("mesh_type"),
                "case_dir": item.get("case_dir"),
                "status": item.get("status"),
                "mesh_review_status": item.get("mesh_review_status"),
                "tecplot_file": (item.get("result") or {}).get("tecplot_file"),
                "polyMesh_dir": (item.get("result") or {}).get("polyMesh_dir"),
            }
            for item in mesh_variant_results
        ],
        "local_structure_recovery": poscar_recovery_result,
        "request_id": preprocessing_request.get("request_id"),
        "request_spec_hash": preprocessing_request.get("request_spec_hash"),
        "review_profile": review_profile,
        "calculation_stages": stage_records,
    }
    delivery_metadata = {
            "discipline": reported_discipline,
            "discipline_adapter": (
                "generic" if selected_discipline == "unknown" and reported_discipline != "unknown"
                else selected_discipline
            ),
            "mesh_type": mesh_type,
            "solver_family": defaults["solver_family"],
            "data_model": universal_contract["data_model"],
            "source": universal_contract["source"],
            "semantics": universal_contract["semantics"],
            "measurement_context": universal_contract["measurement_context"],
            "reproducibility": universal_contract["reproducibility"],
            "downstream_contract": universal_contract["downstream_contract"],
            "quality_gates": universal_contract["quality_gates"],
            "quality_gate_count": len(universal_contract["quality_gates"]),
            "assumptions": universal_contract["assumptions"],
            "assumption_count": len(universal_contract["assumptions"]),
            "lineage_count": len(universal_contract["lineage"]),
            "mesh_review_status": ((mesh_generation_result or {}).get("mesh_review") or {}).get("status"),
            "request_id": preprocessing_request.get("request_id"),
            "request_spec_hash": preprocessing_request.get("request_spec_hash"),
            "review_profile": review_profile,
            "deliverable_valid": (mesh_generation_result or {}).get("deliverable_valid", True),
            "polyMesh_dir": (mesh_generation_result or {}).get("polyMesh_dir"),
            "tecplot_surface": (mesh_generation_result or {}).get("tecplot_file"),
            "mesh_delivery_index": (mesh_generation_result or {}).get("mesh_delivery_index"),
            "mesh_variant_count": len(mesh_variant_results),
            "mesh_convergence_variant_count": sum(
                len(items) for items in mesh_convergence_variant_results.values()
            ),
    }
    emit_progress(
        state,
        "package_candidate_ready",
        str(staging_package_dir_abs),
        file_count=n_written,
        size_bytes=size_bytes,
    )
    return {
        "status": "success",
        "case_name": case_name,
        "package_dir": str(package_dir_abs),
        "discipline": reported_discipline,
        "discipline_adapter": (
            "generic" if selected_discipline == "unknown" and reported_discipline != "unknown"
            else selected_discipline
        ),
        "confidence": detected.get("confidence"),
        "mesh_type": mesh_type,
        "solver_family": defaults["solver_family"],
        "written_files": [str(path.resolve()) for path in staged_files],
        "mesh_generation_result": mesh_generation_result,
        "n_written_files": n_written,
        "size_bytes": size_bytes,
        "dataset_artifact_saved": False,
        "delivery_candidate": {
            "cleanup_workspaces": True,
            "staging_dir": str(staging_package_dir_abs),
            "final_dir": str(package_dir_abs),
            "artifact_name": f"{case_name}_preprocessing_contract",
            "discipline": selected_discipline,
            "preprocessing_request": preprocessing_request,
            "preprocessing_work_order": preprocessing_work_order,
            "review_profile": review_profile,
            "review_context": review_context,
            "manifest": manifest,
            "delivery_files": delivery_files,
            "delivery_fields": delivery_fields,
            "metadata": delivery_metadata,
            "repair_actions": repair_actions,
        },
    }


def _merge_approved_package_step_arguments(
    state: State,
    kwargs: dict[str, Any],
) -> dict[str, Any]:
    """Preserve approved structured package arguments on manual tool calls."""
    def coerce_parameters(value: Any, field_name: str) -> dict[str, Any]:
        if isinstance(value, dict):
            return dict(value)
        parsed, _ = _parse_json_object(str(value or ""), field_name)
        return parsed

    plan = approved_plan(state)
    if not isinstance(plan, dict):
        return kwargs
    approved_args: dict[str, Any] = {}
    for step in plan.get("generation_steps") or []:
        if not isinstance(step, dict):
            continue
        if step.get("tool_name") != "build_scientific_preprocessing_package":
            continue
        candidate = step.get("tool_arguments") or {}
        if isinstance(candidate, dict):
            approved_args = dict(candidate)
        break
    if not approved_args:
        return kwargs

    current = dict(kwargs)
    approved_params = coerce_parameters(approved_args.get("parameters"), "approved.parameters")
    current_params = coerce_parameters(current.get("parameters"), "parameters")
    current_analysis = current_params.get("requirement_analysis") if isinstance(current_params, dict) else None
    current_stages = (
        current_analysis.get("calculation_stages")
        if isinstance(current_analysis, dict)
        else None
    )
    approved_analysis = approved_params.get("requirement_analysis") if isinstance(approved_params, dict) else None
    approved_stages = (
        approved_analysis.get("calculation_stages")
        if isinstance(approved_analysis, dict)
        else None
    )
    needs_approved_params = bool(approved_stages and not current_stages)
    if needs_approved_params:
        merged_params = {**approved_params, **current_params}
        merged_params["requirement_analysis"] = approved_analysis
        if approved_params.get("reference_evidence") and not current_params.get("reference_evidence"):
            merged_params["reference_evidence"] = approved_params.get("reference_evidence")
        current["parameters"] = json.dumps(merged_params, ensure_ascii=False)
        state.append_transcript(
            "approved_package_arguments_restored",
            reason="manual package call lacked approved calculation_stages",
            approved_stage_count=len(approved_stages or []),
        )

    for key in (
        "spec",
        "discipline",
        "reference_evidence",
        "mesh_generation_result",
    ):
        if key not in current or current.get(key) in (None, "", [], {}):
            if approved_args.get(key) not in (None, "", [], {}):
                current[key] = approved_args.get(key)
    return current


async def _gated_build_scientific_preprocessing_package(
    state: State,
    **kwargs: Any,
) -> dict[str, Any]:
    # 判决拆除 O9（随根 store:468 降格，2026-08-31）：评审不再是通行许可，
    # 未评审执行照跑并打 plan_approval_status:unapproved。
    approval_stamp = witness_plan_approval(state, "build_scientific_preprocessing_package")
    # 判决拆除（sp:8575 删，2026-08-31）：「plan 必须含本步骤」是审批链重复
    # 抄件（同规则第 3 处抄写）；唯一登记点在 executor dispatch（plan_deviation）。
    kwargs = _merge_approved_package_step_arguments(state, kwargs)
    try:
        result = await _build_scientific_preprocessing_package(state=state, **kwargs)
    except Exception:
        _discard_incomplete_preprocessing_delivery(state)
        raise
    if isinstance(result, dict):
        result.setdefault("plan_approval_status", approval_stamp["plan_approval_status"])
    return result

"""Unified review entrypoint for every preprocessing discipline."""
from __future__ import annotations

import json
import re
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from nodes.data.pipeline_contract import revision_contract
from nodes.data.planning.schemas import (
    blocked_placeholder_markers,
)
from nodes.data.stage_semantics import dependency_artifact_ready, produces_runtime_result


def _common_review(files: dict[str, str], requested: dict[str, Any]) -> dict[str, Any]:
    checks: dict[str, Any] = {}
    issues: list[dict[str, Any]] = []
    for name, content in files.items():
        path = PurePosixPath(str(name))
        safe_path = not path.is_absolute() and ".." not in path.parts
        checks[f"safe_path:{name}"] = safe_path
        if not safe_path:
            issues.append({
                "code": "unsafe_output_path",
                "severity": "critical",
                "file": name,
                "message": f"Generated path {name} escapes the package directory.",
                "recommendation": "Use a package-relative output path.",
                "repairable": False,
            })
        if not str(content or "").strip():
            issues.append({
                "code": "empty_generated_file",
                "severity": "critical",
                "file": name,
                "message": f"Generated file {name} is empty.",
                "recommendation": "Regenerate it or remove it from required deliverables.",
                "repairable": False,
            })
            continue
        placeholders = blocked_placeholder_markers(content)
        # Package path safety is enforced above. An absolute path inside file
        # content can be producer provenance (for example a mesher source
        # comment), so it is not a package-wide placeholder. Producers remain
        # responsible for validating executable runtime dependencies.
        placeholders = [
            marker for marker in placeholders
            if marker != "environment_absolute_path"
        ]
        checks[f"placeholder_free:{name}"] = not placeholders
        if placeholders:
            issues.append({
                "code": "placeholder_or_stub_content",
                "severity": "critical",
                "file": name,
                "message": (
                    f"Generated file {name} still contains placeholder or non-executable "
                    f"runtime content: {', '.join(placeholders)}."
                ),
                "recommendation": (
                    "Regenerate only this artifact with final runnable content or an actionable "
                    "external-data acquisition document."
                ),
                "repairable": True,
            })
        suffix = path.suffix.lower()
        parsed: Any = None
        try:
            if suffix == ".json":
                parsed = json.loads(content)
            elif suffix in {".yaml", ".yml"}:
                parsed = yaml.safe_load(content)
            else:
                continue
            if not isinstance(parsed, (dict, list)):
                raise ValueError("top-level value must be a mapping or sequence")
            checks[f"structured_format:{name}"] = True
        except (json.JSONDecodeError, yaml.YAMLError, ValueError) as exc:
            checks[f"structured_format:{name}"] = False
            issues.append({
                "code": "invalid_structured_file",
                "severity": "critical",
                "file": name,
                "message": f"{name} is not valid structured data: {exc}",
                "recommendation": "Regenerate syntactically valid structured content.",
                "repairable": False,
            })
        if path.name == "parameter_contract.json":
            valid_contract = bool(
                isinstance(parsed, dict)
                and parsed.get("status") == "pass"
                and all(
                    item.get("status") == "pass"
                    for item in parsed.get("propagation_checks") or []
                )
                and (
                    not parsed.get("consumption_required")
                    or all(
                        item.get("status") == "pass"
                        for item in parsed.get("consumption_checks") or []
                    )
                )
            )
            checks[f"stage_parameter_contract:{name}"] = valid_contract
            if not valid_contract:
                issues.append({
                    "code": "stage_parameter_contract_invalid",
                    "severity": "critical",
                    "file": name,
                    "message": (
                        "A stage did not propagate and consume every explicit research-plan parameter "
                        "in its selected generator."
                    ),
                    "recommendation": (
                        "Regenerate only the failing stage with its declared parameters merged into "
                        "and consumed by the discipline adapter."
                    ),
                    "repairable": True,
                })
    return {"checks": checks, "issues": issues}


def _scientific_asset_review(files: dict[str, str], requested: dict[str, Any]) -> dict[str, Any]:
    """Run registered structure and mesh validators on declared delivery files."""
    package_dir_raw = str(requested.get("package_dir") or "").strip()
    if not package_dir_raw:
        return {"checks": {}, "issues": []}
    package_dir = Path(package_dir_raw).expanduser().resolve()
    candidates = [
        name for name in files
        if PurePosixPath(name).name.upper() in {"POSCAR", "CONTCAR"}
        or PurePosixPath(name).suffix.lower() in {".cif", ".vasp", ".poscar", ".contcar"}
    ]
    checks: dict[str, Any] = {}
    issues: list[dict[str, Any]] = []
    if candidates:
        from nodes.data.tools.atomic_structure_recovery import _inspect_structure_file
    for name in candidates:
        path = (package_dir / Path(name)).resolve()
        try:
            path.relative_to(package_dir)
            result = _inspect_structure_file(path, None)
        except (OSError, ValueError) as exc:
            result = {"valid": False, "error": f"{type(exc).__name__}: {exc}"}
        valid = bool(result.get("valid"))
        checks[f"structure_format:{name}"] = valid
        if not valid:
            issues.append({
                "code": "invalid_structure_asset",
                "severity": "critical",
                "file": name,
                "message": result.get("error") or result.get("geometry_validation_error") or (
                    "The delivered structure file failed syntax or geometry validation."
                ),
                "recommendation": "Regenerate only this structure asset from the locked authority.",
                "repairable": True,
            })
    from nodes.data.tools.scientific_assets import inspect_scientific_asset_path
    for name in files:
        path = (package_dir / Path(name)).resolve()
        try:
            path.relative_to(package_dir)
        except ValueError:
            continue
        profile = inspect_scientific_asset_path(path)
        mesh_gates = [
            gate for gate in profile.get("quality_gates") or []
            if isinstance(gate, dict) and str(gate.get("name") or "").startswith("mesh_")
        ]
        if not mesh_gates:
            continue
        metrics = profile.get("metadata") if isinstance(profile.get("metadata"), dict) else {}
        failed = [str(gate.get("name")) for gate in mesh_gates if gate.get("status") != "pass"]
        for gate in mesh_gates:
            checks[f"{name}:{gate.get('name')}"] = gate.get("status") == "pass"
        checks[f"mesh_topology:{name}"] = {"satisfied": not failed, "metrics": metrics}
        if failed:
            issues.append({
                "code": "invalid_mesh_topology",
                "severity": "critical",
                "file": name,
                "message": "Mesh topology validation failed: " + ", ".join(failed),
                "recommendation": (
                    "Regenerate the same mesh asset with unique connectivity, defined nodes, "
                    "positive-area elements, manifold edges, and closed requested boundaries."
                ),
                "diagnostic_context": metrics,
                "repairable": True,
            })
    return {"checks": checks, "issues": issues}


def _requested_stage_conformance_review(requested: dict[str, Any]) -> dict[str, Any]:
    """Ensure generated stage records cannot drift from the caller's scope."""
    stages = [item for item in requested.get("calculation_stages") or [] if isinstance(item, dict)]
    records = [item for item in requested.get("stage_records") or [] if isinstance(item, dict)]
    if not stages:
        return {"checks": {}, "issues": []}

    checks: dict[str, Any] = {}
    issues: list[dict[str, Any]] = []
    package_dir_raw = str(requested.get("package_dir") or "").strip()
    package_dir = Path(package_dir_raw) if package_dir_raw else None
    generated_content = requested.get("_generated_files") or {}

    def file_available(name: str) -> bool:
        content = generated_content.get(name)
        if content is not None:
            return bool(str(content).strip()) or name.endswith("case.foam")
        if package_dir is None:
            return False
        try:
            path = (package_dir / name).resolve()
            path.relative_to(package_dir.resolve())
        except (OSError, ValueError):
            return False
        return path.is_file() and (path.stat().st_size > 0 or path.name == "case.foam")
    planned: dict[str, dict[str, Any]] = {}
    for stage in stages:
        stage_id = str(stage.get("id") or "").strip()
        if stage_id:
            planned[stage_id] = stage

    covered: set[str] = set()
    records_by_id = {
        str(item.get("id") or "").strip().casefold(): item
        for item in records
        if str(item.get("id") or "").strip()
    }
    for record in records:
        record_id = str(record.get("id") or "").strip()
        parent_id = str(record.get("sweep_parent") or "").strip()
        plan_id = record_id if record_id in planned else parent_id
        if plan_id in planned:
            covered.add(plan_id)
        else:
            issues.append({
                "code": "stage_not_in_request_scope",
                "severity": "critical",
                "stage_id": record_id,
                "file": str(record.get("case_root") or ""),
                "message": f"Generated stage {record_id!r} is not declared by the caller's approved scope.",
                "recommendation": "Remove the undeclared stage and regenerate only the requested scope.",
                "repairable": False,
            })
        status = str(record.get("status") or "").strip()
        plan_stage = planned.get(plan_id) or {}
        dependency_artifacts = (
            (record.get("effective_parameters") or {}).get("dependency_artifacts")
            if isinstance(record.get("effective_parameters"), dict)
            else {}
        )
        dependency_artifacts = dependency_artifacts if isinstance(dependency_artifacts, dict) else {}
        unsatisfied_dependencies: list[str] = []
        for dependency in [
            str(item).strip() for item in plan_stage.get("dependencies") or [] if str(item).strip()
        ]:
            artifact = dependency_artifacts.get(dependency)
            if dependency_artifact_ready(artifact):
                continue
            dependency_record = records_by_id.get(dependency.casefold())
            if dependency_record is None:
                unsatisfied_dependencies.append(f"{dependency}:missing_stage_record")
            elif dependency_record.get("status") != "ready":
                unsatisfied_dependencies.append(
                    f"{dependency}:{dependency_record.get('status') or 'not_ready'}"
                )
            elif produces_runtime_result(dependency_record):
                unsatisfied_dependencies.append(f"{dependency}:result_artifact_missing")
        dependency_state_valid = status != "ready" or not unsatisfied_dependencies
        checks[f"stage_dependencies_satisfied:{record_id}"] = dependency_state_valid
        if not dependency_state_valid:
            issues.append({
                "code": "stage_dependency_state_inconsistent",
                "severity": "critical",
                "stage_id": record_id,
                "file": str(record.get("case_root") or ""),
                "message": (
                    f"Stage {record_id!r} is marked ready although dependencies are unavailable: "
                    f"{', '.join(unsatisfied_dependencies)}."
                ),
                "recommendation": (
                    "Keep the generated candidate inputs, mark the stage deferred_dependency, "
                    "and record the exact predecessor artifact needed for resume."
                ),
                "repairable": True,
            })
        generated_files = [str(item) for item in record.get("generated_files") or [] if str(item).strip()]
        missing_inputs = [str(item) for item in record.get("missing_inputs") or [] if str(item).strip()]
        missing_generated_files = [name for name in generated_files if not file_available(name)]
        expected_solver_files = [
            str(item) for item in record.get("expected_solver_files") or [] if str(item).strip()
        ]
        case_root = str(record.get("case_root") or "").strip()
        missing_solver_files = [
            f"{case_root}/{name}" if case_root else name
            for name in expected_solver_files
            if not file_available(f"{case_root}/{name}" if case_root else name)
        ]
        roles = {
            re.sub(r"[^a-z0-9]+", "_", str(role).lower()).strip("_")
            for role in record.get("required_file_roles") or []
        }
        missing_mesh_files: list[str] = []
        mesh_roots = [
            str(value).strip()
            for value in record.get("variant_case_roots") or []
            if str(value).strip()
        ] or ([case_root] if case_root else [])
        if "openfoam_poly_mesh" in roles:
            missing_mesh_files = [
                f"{root}/constant/polyMesh/{name}"
                for root in mesh_roots
                for name in ("points", "faces", "owner", "neighbour", "boundary")
                if not file_available(f"{root}/constant/polyMesh/{name}")
            ]
        complete = not missing_generated_files and not missing_solver_files and not missing_mesh_files
        ready_valid = status == "ready" and bool(generated_files) and not missing_inputs and complete
        candidate_valid = status == "candidate_generated" and bool(generated_files) and not missing_inputs and complete
        quarantined_valid = status != "ready" and bool(missing_inputs)
        checks[f"stage_delivery_state:{record_id}"] = ready_valid or candidate_valid or quarantined_valid
        checks[f"stage_generated_files_exist:{record_id}"] = not missing_generated_files
        if expected_solver_files:
            checks[f"stage_solver_deck_complete:{record_id}"] = not missing_solver_files
        if "openfoam_poly_mesh" in roles:
            checks[f"stage_runtime_mesh_complete:{record_id}"] = not missing_mesh_files
        for code, missing, label in (
            ("stage_declared_file_missing", missing_generated_files, "declared generated file"),
            ("stage_solver_deck_incomplete", missing_solver_files, "required solver input"),
            ("stage_runtime_mesh_incomplete", missing_mesh_files, "runtime mesh component"),
        ):
            if not missing or status not in {"ready", "candidate_generated"}:
                continue
            issues.append({
                "code": code,
                "severity": "critical",
                "stage_id": record_id,
                "file": missing[0],
                "message": f"Stage {record_id!r} is marked {status} but lacks {label}s: {', '.join(missing)}.",
                "recommendation": "Regenerate only this stage and rerun deterministic review before delivery.",
                "repairable": True,
            })
        if not (ready_valid or candidate_valid or quarantined_valid):
            issues.append({
                "code": "stage_delivery_state_inconsistent",
                "severity": "critical",
                "stage_id": record_id,
                "file": str(record.get("case_root") or ""),
                "message": (
                    f"Stage {record_id!r} is neither a complete ready case nor an explicitly "
                    "quarantined incomplete case."
                ),
                "recommendation": (
                    "Generate and review all required inputs, or mark the stage incomplete with "
                    "an explicit missing-input resume contract."
                ),
                "repairable": False,
            })

    missing_stage_ids = sorted(set(planned) - covered)
    checks["requested_stage_coverage"] = not missing_stage_ids
    for stage_id in missing_stage_ids:
        issues.append({
            "code": "requested_stage_missing",
            "severity": "critical",
            "stage_id": stage_id,
            "file": "",
            "message": f"Requested stage {stage_id!r} has no generated or blocked stage record.",
            "recommendation": "Generate that stage or emit its explicit resumable input contract.",
            "repairable": False,
        })
    ready_records = [
        record for record in records
        if record.get("status") in {"ready", "candidate_generated"}
        and record.get("generated_files")
    ]
    allow_contract_only = bool(requested.get("allow_contract_only_delivery"))
    checks["at_least_one_ready_stage"] = bool(ready_records) or allow_contract_only
    if records and not ready_records and not allow_contract_only:
        issues.append({
            "code": "no_ready_stage_deliverable",
            "severity": "critical",
            "file": "workflow_manifest.json",
            "message": (
                "The package contains requested stage records but no complete runnable or "
                "otherwise usable stage deliverable. Pending contracts alone are not a dataset delivery."
            ),
            "recommendation": (
                "Repair the earliest blocked generation stage and review it before saving a dataset; "
                "otherwise emit a recoverable blocked report."
            ),
            "repairable": False,
        })
    return {"checks": checks, "issues": issues}


def matching_asset_paths(
    requirement: dict[str, Any], paths: set[str], bound_paths: list[str] | None = None,
) -> list[str]:
    """Resolve a declared location or a unique filename, never guess between duplicates."""
    aliases = {
        str(PurePosixPath(str(requirement[key]))).rstrip("/")
        for key in ("declared_output_path", "expected_filename", "filename", "path")
        if str(requirement.get(key) or "").strip()
    }
    matches = {path for path in paths if any(
        path == alias or path.startswith(f"{alias}/") for alias in aliases
    )}
    if not matches:
        # A filename does not prescribe the producer's internal directory.
        # Explicit directory paths still require exact relative locations.
        candidates = {path for path in paths if any(
            len(PurePosixPath(alias).parts) == 1 and PurePosixPath(path).name == alias
            for alias in aliases
        )}
        if len(candidates) == 1:
            matches.update(candidates)
    if requirement.get("output_path_is_explicit") is False:
        matches.update(path for path in bound_paths or [] if path in paths)
    return sorted(matches)


def _required_asset_review(files: dict[str, str], requested: dict[str, Any]) -> dict[str, Any]:
    """Verify the approved deliverables by stable asset identity."""
    requirements = [
        item for item in requested.get("required_deliverables") or []
        if isinstance(item, dict)
        and item.get("required", True) is not False
        and item.get("delivery_required", True) is not False
    ]
    if not requirements:
        return {"checks": {}, "issues": []}
    delivered_paths = {
        str(PurePosixPath(name))
        for name, content in files.items()
        if str(content).strip()
    }
    delivered_by_asset: dict[str, list[str]] = {}
    for item in requested.get("delivery_files") or []:
        if not isinstance(item, dict):
            continue
        path = str(PurePosixPath(str(item.get("path") or "")))
        asset_ids = [item.get("asset_id"), *(item.get("asset_ids") or [])]
        for asset_id in asset_ids:
            if asset_id and path in delivered_paths:
                delivered_by_asset.setdefault(str(asset_id), []).append(path)

    checks: dict[str, Any] = {}
    issues: list[dict[str, Any]] = []
    for index, requirement in enumerate(requirements):
        requirement_id = str(requirement.get("id") or f"required_asset_{index + 1}").strip()
        aliases = {
            str(PurePosixPath(str(requirement.get(key)))).rstrip("/")
            for key in ("declared_output_path", "expected_filename", "filename", "path")
            if str(requirement.get(key) or "").strip()
        }
        matches = matching_asset_paths(requirement, delivered_paths, delivered_by_asset.get(requirement_id))
        satisfied = bool(matches)
        checks[f"required_asset:{requirement_id}"] = {
            "satisfied": satisfied,
            "matches": sorted(matches)[:5],
        }
        if not satisfied:
            target = next(iter(sorted(aliases)), requirement_id)
            issues.append({
                "code": "required_asset_unfulfilled",
                "severity": "critical",
                "file": target,
                "deliverable_id": requirement_id,
                "message": f"Approved delivery asset {requirement_id!r} is not present in the package.",
                "recommendation": "Regenerate the owning asset without changing the locked request.",
                "repairable": True,
            })
        else:
            from nodes.data.tools.geometry_assets import canonical_mesh_controls, flatten_parameter_groups
            from nodes.data.tools.scientific_assets import validate_format_version

            bindings = canonical_mesh_controls(flatten_parameter_groups(requirement.get("parameter_bindings") or {}))
            declared_format = bindings.get("solver_mesh_format") or requirement.get("format") or ""
            declared_version = bindings.get("output_version") or ""
            errors = {name: validate_format_version(files[name], declared_format, declared_version) for name in matches}
            # An asset can include an internal representation and a final
            # export. Require a matching delivery, not conversion of both.
            if errors and all(errors.values()):
                name = sorted(errors)[0]
                checks[f"required_asset:{requirement_id}"]["satisfied"] = False
                issues.append({
                    "code": "format_version_mismatch", "severity": "critical", "file": name,
                    "deliverable_id": requirement_id, "message": errors[name],
                    "recommendation": "Export the existing asset in the declared format/version without changing its scientific content.",
                    "repairable": True,
                })
    return {"checks": checks, "issues": issues}


def review_preprocessing_package(
    discipline: str,
    files: dict[str, str],
    requested: dict[str, Any] | None = None,
) -> dict[str, Any]:
    normalized = str(discipline or "").strip().lower()
    context = {
        **(requested or {}),
        "_review_discipline": normalized,
        "_generated_files": files,
    }
    common = _common_review(files, context)
    scientific_assets = _scientific_asset_review(files, context)
    stage_conformance = _requested_stage_conformance_review(context)
    required_assets = _required_asset_review(files, context)
    # Domain semantics are reviewed once against the locked authority by the
    # model-assisted authority reviewer.  This function intentionally retains
    # only deterministic checks that are broadly reusable across disciplines.
    domain_review = {
        "review_type": "deterministic_preprocessing_package",
        "status": "pass",
        "checks": {},
        "issues": [],
        "repairs": [],
    }
    issues = [
        *(common.get("issues") or []),
        *(scientific_assets.get("issues") or []),
        *(stage_conformance.get("issues") or []),
        *(required_assets.get("issues") or []),
        *(domain_review.get("issues") or []),
    ]
    failed = any(item.get("severity") == "critical" for item in issues)
    return {
        **domain_review,
        "status": "fail" if failed else "pass",
        "discipline": normalized or discipline,
        "issues": issues,
        "common_checks": {
            **(common.get("checks") or {}),
            **(scientific_assets.get("checks") or {}),
            **(stage_conformance.get("checks") or {}),
            **(required_assets.get("checks") or {}),
            "generated_files_nonempty": not any(
                item.get("code") == "empty_generated_file" for item in issues
            ),
            "deterministic_review_complete": True,
        },
        **({"revision_contract": revision_contract(issues)} if failed else {}),
    }

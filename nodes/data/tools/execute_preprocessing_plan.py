"""Execute an approved preprocessing plan as a dependency-ordered DAG."""
from __future__ import annotations

import asyncio
import json
import re
import hashlib
import shutil
from datetime import datetime, timezone
from pathlib import Path
from pathlib import PurePosixPath
from typing import Any

from core.state import State
from core.llm import LLMMessage
from core.tool_registry import ToolDefinition, execute, get_tool, register_tool
from shared.lib.json_file import write_object
from nodes.data.pipeline_contract import (
    RESUMABLE_WRITER_FAILURES,
    merge_parameter_updates,
    pipeline_outcome,
    review_progress_improved,
    revision_progress,
    revision_contract,
    revision_issue_identity,
    with_pipeline_outcome,
)
from nodes.data.planning.store import (
    PlanningStore,
    canonical_hash,
    plan_authorizes_tool,
)
from nodes.data.planning.request_contract import caller_request_text
from nodes.data.planning.schemas import (
    LOCAL_REUSE_STRATEGIES,
    _step_capability_contract_error,
    _step_workflow_capability,
    is_pipeline_infrastructure_step,
    mesh_dependency_issues,
    tool_allowed_in_plan_kind,
)
from nodes.data.progress import emit_progress
from nodes.data.review.package_reviewer import review_preprocessing_package, _required_asset_review, matching_asset_paths
from nodes.data.review.gate_registry import build_review_receipt, write_review_receipt
from nodes.data.review.authority_reviewer import (
    _extract_object,
    _file_evidence,
    combine_reviews,
    review_delivery_against_authority,
)

from .preprocessing_capabilities import (
    execute_preprocessing_python,
    generate_preprocessing_artifact,
)
from .scientific_assets import remember_downloaded_scientific_asset
from .geometry_assets import canonical_parameter_bindings
from .package_publisher import (
    artifact_delivery_path as _artifact_delivery_path,
    cleanup_generated_workspaces,
    package_paths,
    prepare_package_staging,
    publish_dataset_delivery,
    remap_published_paths,
    save_failed_review,
)


def _structured_failure_issues(result: dict[str, Any]) -> list[dict[str, Any]]:
    """Collect producer/reviewer diagnostics without knowing their discipline."""
    candidates: list[Any] = []
    for key in ("validation_diagnostics", "issues", "review_issues"):
        candidates.extend(result.get(key) or [])
    for key in ("review", "mesh_review", "quality_review", "review_report"):
        container = result.get(key)
        if isinstance(container, dict):
            candidates.extend(container.get("issues") or [])
    contract = result.get("revision_contract")
    if isinstance(contract, dict):
        candidates.extend(contract.get("issues") or [])
    context = result.get("repair_context") if isinstance(result.get("repair_context"), dict) else None
    issues: list[dict[str, Any]] = []
    seen: set[tuple[tuple[str, ...], str]] = set()
    for item in candidates:
        if not isinstance(item, dict):
            continue
        key = (revision_issue_identity(item), str(item.get("message") or ""))
        if key in seen:
            continue
        seen.add(key)
        issue = dict(item)
        if context and not isinstance(issue.get("diagnostic_context"), dict):
            issue["diagnostic_context"] = context
        issues.append(issue)
    return issues


def _failure_signature(result: dict[str, Any]) -> str:
    error = str(result.get("error") or result.get("blocked_reason") or "")
    # Traceback line numbers/source excerpts change after an edit, even when
    # the consumer is still failing for exactly the same reason.
    exceptions = re.findall(r"(?m)(?:^|: )([\w.]*(?:Error|Exception):[^\n]+)", error)
    if exceptions:
        if exceptions[-1].startswith(("SyntaxError:", "IndentationError:", "TabError:")):
            # Different offending source text is a new diagnostic even when
            # Python's generic syntax-error wording is identical.
            return canonical_hash(re.sub(r'File "[^"]+", line \d+', 'File <source>', error))
        return canonical_hash(exceptions[-1].strip())
    return revision_progress(
        _structured_failure_issues(result),
        error=error,
    )["diagnostic_signature"]


def _step_artifact_paths(step: dict[str, Any], result: dict[str, Any]) -> dict[str, str]:
    """Resolve logical outputs once for dependency transport and publication."""
    paths = dict(result.get("generated_artifacts") or {})
    output_ids = step.get("outputs") or []
    primary = next((result[key] for key in (
        "saved_path", "polyMesh_dir", "solver_mesh_file", "mesh_file", "structure_file", "data_file",
        "preferred_geometry_file", "path",
    ) if result.get(key)), None)
    # Never assign one native file to several independently declared assets.
    if primary and len(output_ids) == 1:
        paths.setdefault(str(output_ids[0]), str(primary))
    return paths


def _result_material_paths(result: dict[str, Any], *, include_missing: bool = False) -> list[Path]:
    """Return materialized paths declared by one producer result.

    Reviewers cite package-relative files while generation tools report their
    run-local source paths. Keeping this translation at the executor boundary
    lets every domain use the same producer-repair loop.
    """
    values: list[Any] = [*(result.get("written_files") or [])]
    variants = [*(result.get("profile_variants") or []),
                *(item for group in (result.get("convergence_variants") or {}).values() for item in group)]
    for variant in variants:
        values.extend(_result_material_paths(variant.get("result", variant), include_missing=include_missing))
    generated = result.get("generated_artifacts")
    if isinstance(generated, dict):
        values.extend(generated.values())
    if result.get("saved_path"):
        values.append(result["saved_path"])
    for key, value in result.items():
        if value in (None, "", [], {}) or not isinstance(value, (str, Path)):
            continue
        if key in {"path", "saved_path", "preferred_geometry_file"} or key.endswith(
            ("_file", "_path", "_dir")
        ):
            # Only explicit output receipts above promise existence. Other
            # path fields can be future publication destinations or metadata.
            if not include_missing or Path(value).expanduser().exists():
                values.append(value)
    paths: list[Path] = []
    seen: set[str] = set()
    for value in values:
        try:
            path = Path(str(value)).expanduser().resolve()
        except (OSError, RuntimeError, ValueError):
            continue
        key = str(path)
        if key not in seen and (include_missing or path.exists()):
            seen.add(key)
            paths.append(path)
    return paths


def _result_material_fingerprints(
    result: dict[str, Any], *, issues: list[dict[str, Any]] | None = None,
) -> dict[str, str]:
    """Ignore incidental receipts, but include a report explicitly under repair."""
    declared = {str(Path(path).expanduser().resolve())
                for path in (result.get("generated_artifacts") or {}).values() if path}
    targets = [str(issue.get("source_file") or issue.get("file") or issue.get("path") or "")
               for issue in issues or []]
    return {
        str(path): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in _result_material_paths(result)
        if path.is_file()
        and (str(path) in declared or any(target and (
            path.as_posix() == target or path.as_posix().endswith("/" + target.lstrip("/"))
            or path.name == PurePosixPath(target).name
        ) for target in targets) or (
            "audit" not in {part.casefold() for part in path.parts}
            and path.suffix.casefold() != ".log"
            and "review" not in path.name.casefold()
            and path.name.casefold() != "manifest.json"
        ))
    }


def _result_material_evidence(result: dict[str, Any]) -> dict[str, Any]:
    """Bounded, explicitly sampled producer evidence shared by repair/planning."""
    evidence = {}
    paths = sorted((path for path in _result_material_paths(result) if path.is_file()),
                   key=lambda path: path.stat().st_size)
    for path in paths[:4]:
        size = path.stat().st_size
        with path.open("rb") as stream:
            sample = stream.read(12000)
            if size > 12000:
                stream.seek(size - 6000)
                sample = sample[:6000] + b"\n[... sampled; middle omitted ...]\n" + stream.read(6000)
        evidence[str(path)] = {
            "content": "[binary content omitted]" if b"\x00" in sample else sample.decode("utf-8", errors="replace"),
            "size_bytes": size,
            "sampled": size > 12000 or b"\x00" in sample,
        }
    return evidence


def _review_issue_producer(
    issue: dict[str, Any],
    results: list[dict[str, Any]],
    *,
    file_hashes: dict[Path, str] | None = None,
) -> str:
    """Resolve a causal repair source, or otherwise the observed file's writer."""
    # The caller may share hashes within one immutable review inventory. Do
    # not retain them across regeneration, when the same paths can change.
    file_hashes = {} if file_hashes is None else file_hashes

    def content_hash(path: Path) -> str:
        if path not in file_hashes:
            file_hashes[path] = hashlib.sha256(path.read_bytes()).hexdigest()
        return file_hashes[path]

    # Review's source_file means the source that must change, not mere lineage.
    # An unresolved source must not suppress the observed file's real owner.
    if issue.get("source_file"):
        owner = _review_issue_producer({"file": issue["source_file"]}, results, file_hashes=file_hashes)
        if owner:
            return owner
    cited = str(issue.get("file") or issue.get("path") or "").strip().replace("\\", "/")
    if not cited:
        context = issue.get("diagnostic_context")
        owners = {
            _review_issue_producer({"file": str(path)}, results, file_hashes=file_hashes)
            for path in _result_material_paths(context if isinstance(context, dict) else {})
        } - {""}
        return next(iter(owners)) if len(owners) == 1 else ""
    cited_parts = PurePosixPath(cited).parts
    reviewed_hash = None
    for record in results:
        candidate = (record.get("result") or {}).get("delivery_candidate") or {}
        root = candidate.get("staging_dir")
        if root:
            path = (Path(root) / cited).resolve()
            if path.is_relative_to(Path(root).resolve()) and path.is_file():
                reviewed_hash = content_hash(path)
                break
    matches: list[tuple[int, int, int, int, str]] = []
    for record in results:
        if not isinstance(record, dict):
            continue
        result = record.get("result") if isinstance(record.get("result"), dict) else {}
        # An unchanged copy belongs to the upstream producer. A transformed
        # solver deck belongs to its writer, not a same-named mesh export.
        assembler_penalty = 1 if isinstance(result.get("delivery_candidate"), dict) else 0
        produced = set(_result_material_paths({
            key: result.get(key) for key in ("written_files", "generated_artifacts", "saved_path")
        }))
        for path in _result_material_paths(result):
            parts = PurePosixPath(path.as_posix()).parts
            common = 0
            for left, right in zip(reversed(cited_parts), reversed(parts)):
                if left.casefold() != right.casefold():
                    break
                common += 1
            if common:
                same_content = int(reviewed_hash is not None and path.is_file()
                                   and content_hash(path) == reviewed_hash)
                # Referencing an upstream file (e.g. geometry_file) does not
                # make this consumer its producer. Break exact-path ties by
                # actual output ownership rather than lexical step-id order.
                matches.append((same_content, -assembler_penalty, common, int(path in produced), str(record.get("step_id") or "")))
    return max(matches)[4] if matches else ""


def _causal_review_producer(issue: dict, plan: dict, results: list[dict], owner: str = "") -> str:
    """A proposed repair must affect the observed asset through the current DAG."""
    owner = owner or _review_issue_producer({key: value for key, value in issue.items()
                                           if key not in {"source_file", "step_id"}}, results)
    steps = {str(step["id"]): step for step in plan.get("generation_steps") or []}
    ancestors, pending = set(), [owner] if owner else []
    while pending:
        current = pending.pop()
        if current not in ancestors:
            ancestors.add(current)
            pending.extend(steps.get(current, {}).get("dependencies") or [])
    source_owner = (_review_issue_producer({"file": issue["source_file"]}, results)
                    if issue.get("source_file") else "")
    if source_owner not in ancestors:
        issue.pop("source_file", None)
    declared = str(issue.get("step_id") or "")
    return next((candidate for candidate in (declared, source_owner, owner) if candidate in ancestors), "")


def _review_producer_map(
    plan: dict[str, Any],
    results: list[dict[str, Any]],
    delivery_files: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Expose actual writers so review can distinguish evidence from repair targets."""
    steps = {str(step.get("id") or ""): step for step in plan.get("generation_steps") or []
             if isinstance(step, dict) and step.get("id")}
    producers: list[dict[str, Any]] = []
    for record in results:
        step_id = str(record.get("step_id") or "")
        step = steps.get(step_id)
        if step is None:
            continue
        tool_name = str(step.get("tool_name") or "")
        definition = get_tool(tool_name)
        repair_field = _tool_repair_field(definition)
        result = record.get("result") if isinstance(record.get("result"), dict) else {}
        # Input references (geometry_file, case_dir, etc.) do not establish
        # ownership. Only the producer's explicit output receipts do.
        materialized = _result_material_paths({
            "written_files": result.get("written_files") or [],
            "generated_artifacts": _step_artifact_paths(step, result),
            "saved_path": result.get("saved_path"),
        })
        if not materialized:
            continue
        native_measurements = dict(result.get("quality") or {}) if isinstance(result.get("quality"), dict) else {}
        native_mesh_check = (result.get("mesh_review") or {}).get("openfoam_check_mesh") or {}
        if native_mesh_check:
            # Pass the native observation scope through, not only the boolean
            # mesh_review.checks summary. Older checkpoints retain the raw log
            # and can expose the same facts without rerunning mesh generation.
            from .mesh_generator import _parse_check_mesh_observations

            native_measurements["openfoam_check_mesh"] = {
                "scope": "OpenFOAM checkMesh -allTopology -allGeometry; only reported observations",
                **{key: native_mesh_check[key] for key in (
                    "status", "returncode", "failed_checks", "mesh_ok_marker", "warnings", "measurement_sources",
                    "severely_non_orthogonal_faces", "under_determined_cells", "low_interpolation_weight_faces",
                ) if key in native_mesh_check},
                "observations": native_mesh_check.get("observations") or _parse_check_mesh_observations(
                    str(native_mesh_check.get("output_tail") or "")),
            }
        producers.append({
            "step_id": step_id,
            "tool_name": tool_name,
            "outputs": list(step.get("outputs") or []),
            "dependencies": list(step.get("dependencies") or []),
            "repair_field": repair_field,
            "derived_report_files": result.get("derived_report_files") or [],
            "report_rebuild": _report_rebuild_contract(tool_name, result),
            # Effective producer controls are distinct from output measurements.
            # Keep both available so review need not infer frozen controls
            # from an independently authored reproducibility report.
            "executed_parameters": result.get("grid_params") or {},
            "native_execution": {key: result[key] for key in
                                 ("gmsh_cmd", "solver_export", "geometry_file", "copied_geometry_file", "mesh_file", "solver_mesh_file", "edit_source")
                                 if result.get(key)},
            "native_reviews": {key: {"status": value.get("status"), "scope": value.get("scope") or "producer integrity checks",
                                      "checks": value.get("checks") or {}}
                               for key in ("mesh_review", "quality_review", "validation_receipt")
                               if isinstance((value := result.get(key)), dict) and value.get("checks")},
            # Evidence size is the review reader's responsibility. Dropping a
            # measurement here forces the reviewer to recreate it from scratch.
            "native_measurements": native_measurements,
            "materialized_files": sorted(str(path) for path in materialized),
            "staged_files": [],
        })
    by_id = {producer["step_id"]: producer for producer in producers}
    file_hashes: dict[Path, str] = {}
    if delivery_files is not None:
        for record in results:
            staging = ((record.get("result") or {}).get("delivery_candidate") or {}).get("staging_dir")
            if not staging:
                continue
            root = Path(staging).expanduser().resolve()
            for item in delivery_files:
                path = (root / str(item.get("path") or "")).resolve()
                if path.is_relative_to(root) and item.get("sha256"):
                    file_hashes[path] = str(item["sha256"])
    if delivery_files is None:
        delivery_files = [
            item for record in results
            for item in ((record.get("result") or {}).get("delivery_candidate") or {}).get("delivery_files") or []
            if isinstance(item, dict)
        ]
    for item in delivery_files:
        path = str(item.get("path") or "")
        owner = (_review_issue_producer({"file": path}, results, file_hashes=file_hashes)
                 if path else "")
        if owner in by_id and path not in by_id[owner]["staged_files"]:
            by_id[owner]["staged_files"].append(path)
    # Retired scripts from a route revision are not causal repair targets.
    active, pending = set(), [item["step_id"] for item in producers if item["staged_files"]]
    while pending:
        step_id = pending.pop()
        if step_id not in active:
            active.add(step_id)
            pending.extend(steps.get(step_id, {}).get("dependencies") or [])
    return [item for item in producers if item["step_id"] in active] if active else producers


def _review_progress(review: dict[str, Any], files: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Describe reviewer progress without coupling retries to a fixed count."""
    issues = [item for item in review.get("issues") or [] if isinstance(item, dict)]
    passed_checks = sorted(
        str(name) for name, value in (review.get("common_checks") or {}).items()
        if value is True
        or isinstance(value, dict) and (
            value.get("satisfied") is True or value.get("status") == "pass"
        )
    )
    return revision_progress(issues, passed_checks=passed_checks, materials={
        str(item["path"]): item["sha256"] for item in files or []
        if item.get("path") and item.get("sha256")
    })


def _bind_delivery_assets(
    plan: dict[str, Any],
    delivery_files: list[dict[str, Any]],
    bindings: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Bind staged paths to the plan's immutable logical asset IDs."""
    contracts = [
        item for item in plan.get("required_deliverables") or []
        if isinstance(item, dict) and str(item.get("id") or "").strip()
        and item.get("delivery_required") is not False
    ]
    intermediates = {str(item["id"]) for item in plan.get("required_deliverables") or []
                     if isinstance(item, dict) and item.get("id") and item.get("delivery_required") is False}
    paths = {str(PurePosixPath(str(item.get("path") or ""))) for item in delivery_files}
    resolved = {
        str(contract["id"]): matching_asset_paths(contract, paths, [
            path for binding in bindings or [] if isinstance(binding, dict)
            and binding.get("asset_id") == contract["id"]
            for path in binding.get("paths") or [] if isinstance(path, str)
        ], plan=plan)
        for contract in contracts
    }
    for delivered in delivery_files:
        path = str(PurePosixPath(str(delivered.get("path") or "")))
        matches = [value for value in delivered.get("asset_ids") or [] if isinstance(value, str) and value not in intermediates]
        primary = str(delivered.get("asset_id") or "")
        if primary and primary not in intermediates:
            matches.append(primary)
        matches.extend(asset_id for asset_id, matched in resolved.items() if path in matched)
        if matches:
            delivered["asset_ids"] = sorted(set(matches))
            delivered["asset_id"] = primary if primary in matches else delivered["asset_ids"][0]
    return delivery_files


async def _review_and_publish_candidate(
    state: State,
    plan: dict[str, Any],
    candidate: dict[str, Any],
    results: list[dict[str, Any]],
) -> dict[str, Any]:
    """Own the only review, manifest finalization, and publication boundary."""
    preprocessing_request = (
        candidate.get("preprocessing_request")
        if isinstance(candidate.get("preprocessing_request"), dict)
        else plan.get("preprocessing_request") or {}
    )
    staging = Path(str(candidate.get("staging_dir") or "")).expanduser().resolve()
    final = Path(str(candidate.get("final_dir") or "")).expanduser().resolve()
    expected_staging, expected_final = package_paths(
        state,
        str(preprocessing_request.get("delivery_name") or ""),
    )
    allowed_finals = {expected_final.resolve()}
    if preprocessing_request.get("delivery_name", "preprocessing") in {"", "preprocessing"}:
        # Older writer resumes assembled before restoring the analysis and
        # used this default name. It is still scoped to this workspace/run.
        allowed_finals.add(expected_final.with_name(f"preprocessing__{state.run_id}").resolve())
    if staging != expected_staging.resolve() or final not in allowed_finals:
        raise ValueError("Delivery candidate is outside the canonical data package paths.")
    if not staging.is_dir():
        raise ValueError("Delivery candidate staging directory is unavailable.")

    delivery_files = _bind_delivery_assets(plan, [
        dict(item) for item in candidate.get("delivery_files") or []
        if isinstance(item, dict) and str(item.get("path") or "").strip()
    ])
    if not delivery_files:
        raise ValueError("Delivery candidate contains no declared assets.")
    asset_by_path = {
        str(item.get("path") or ""): str(item.get("asset_id") or item.get("id") or "")
        for item in delivery_files
        if str(item.get("path") or "")
    }

    def attach_deliverable_ids(review: dict[str, Any]) -> None:
        for issue in review.get("issues") or []:
            if not isinstance(issue, dict) or str(issue.get("deliverable_id") or "").strip():
                continue
            path = str(issue.get("file") or issue.get("path") or "")
            if asset_by_path.get(path):
                issue["deliverable_id"] = asset_by_path[path]
        if review.get("status") != "pass":
            review["revision_contract"] = revision_contract(review.get("issues") or [])

    review_files: dict[str, str] = {}
    for item in delivery_files:
        relative = PurePosixPath(str(item["path"]))
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"Delivery file escapes staging: {relative}")
        path = (staging / Path(str(relative))).resolve()
        path.relative_to(staging)
        if not path.is_file():
            raise ValueError(f"Declared delivery file is missing: {relative}")
        item["size"] = path.stat().st_size
        item["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        with path.open("rb") as handle:
            binary = b"\x00" in handle.read(8192)
        review_files[str(relative)] = (
            json.dumps({"binary_asset": str(relative), "size_bytes": item["size"], "sha256": item["sha256"]})
            if binary else path.read_text(encoding="utf-8", errors="replace")
        )

    # Publication can assemble a candidate without a dedicated assembly step.
    # Keep that stage's own input receipt in the existing execution checkpoint
    # so a later review does not need already-cleaned download intermediates.
    checkpoint_path = state.root / "planning" / "execution_checkpoint.json"
    if checkpoint_path.is_file():
        checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        if checkpoint.get("plan_hash") == plan.get("plan_hash"):
            checkpoint["delivery_candidate"] = {**candidate, "delivery_files": delivery_files}
            checkpoint.pop("pending_delivery_rebuild", None)
            write_object(checkpoint_path, json.loads(json.dumps(checkpoint, default=str)))

    work_order = (
        candidate.get("preprocessing_work_order")
        if isinstance(candidate.get("preprocessing_work_order"), dict)
        else plan.get("preprocessing_work_order") or {}
    )
    review_profile = str(
        candidate.get("review_profile")
        or preprocessing_request.get("review_profile")
        or plan.get("review_profile")
        or "plan_bound"
    )
    review_context = {
        key: value for key, value in dict(candidate.get("review_context") or {}).items()
        if key not in {"required_files", "plan_required_files"}
    }
    review_context.update({
        "package_dir": str(staging),
        "delivery_destination": str(final),
        "delivery_files": delivery_files,
        "required_deliverables": plan.get("required_deliverables") or [],
        "source_assets": (plan.get("requirement_analysis") or {}).get("required_files") or [],
        "preprocessing_request": preprocessing_request,
        "generation_steps": plan.get("generation_steps") or [],
        "review_profile": review_profile,
    })
    deterministic_review = review_preprocessing_package(
        str(candidate.get("discipline") or (plan.get("discipline") or {}).get("primary") or "unknown"),
        review_files,
        review_context,
    )
    # The assembler's bound mesh input owns report transcription checks. Do not
    # make the model rediscover copied parameter/count fields across many pages.
    from .scientific_preprocessor import mesh_report_consistency

    for record in results:
        result = record.get("result") or {}
        source = result.get("mesh_generation_result")
        if (isinstance(source, dict) and source
                and (result.get("delivery_candidate") or {}).get("staging_dir") == str(staging)):
            consistency = mesh_report_consistency(review_files, source)
            deterministic_review.setdefault("common_checks", {}).update(consistency["checks"])
            deterministic_review.setdefault("issues", []).extend(consistency["issues"])
            if consistency["issues"]:
                deterministic_review["status"] = "fail"
    attach_deliverable_ids(deterministic_review)
    unavailable = [issue for issue in deterministic_review.get("issues") or []
                   if issue.get("code") == "native_validator_unavailable"]
    if unavailable:
        report = save_failed_review(state, staging, deterministic_review)
        return {"status": "externally_blocked", "failure_category": "environment_required",
                "retry_target": "authority_review", "review_report": str(report),
                "required_dependencies": sorted({dep for issue in unavailable for dep in issue["required_dependencies"]}),
                "error": " ".join(issue["message"] + " " + issue["recommendation"] for issue in unavailable)}
    # Identity resolution is part of semantic review, not mesh regeneration.
    # Still stop here for native validation, safety or actual format failures.
    if deterministic_review.get("status") != "pass" and any(
        issue.get("code") != "required_asset_unfulfilled"
        or str(issue.get("deliverable_id") or "").startswith("caller:")
        for issue in deterministic_review.get("issues") or []
    ):
        report = save_failed_review(state, staging, deterministic_review)
        return {
            "status": "review_failed",
            "review_report": str(report),
            "revision_contract": deterministic_review.get("revision_contract"),
            "review_progress": _review_progress(deterministic_review, delivery_files),
            "message": "Staged assets failed the common delivery review.",
        }
    producer_evidence: dict[str, Any] = {}
    producer_map = _review_producer_map(plan, results, delivery_files)
    active_producers = {item["step_id"] for item in producer_map}
    # Both edit baselines and reused scientific inputs are review evidence.
    # A local input has no download-producer result; omitting it here makes
    # conservation/provenance checks impossible even though execution read it.
    source_assets = {str(item.get("id")): item for item in
                     (plan.get("requirement_analysis") or {}).get("required_files") or []}
    for item in source_assets.values():
        baseline = source_assets.get(str(item.get("edit_source"))) or item
        source = (baseline.get("local_match") or {}).get("path") or (baseline.get("local_match") or {}).get("saved_path")
        if source and Path(source).is_file():
            producer_evidence[source] = {"size_bytes": Path(source).stat().st_size}
    # Planning/repair summaries deliberately sample four small files. Review
    # needs the actual source inventory, not that unrelated sampling policy.
    for record in results:
        if record.get("step_id") not in active_producers:
            continue
        for path in _result_material_paths(record.get("result") or {}):
            if path.is_file():
                producer_evidence[str(path)] = {"size_bytes": path.stat().st_size}
        # Adapter receipts expose parameter transport and execution details;
        # activation still needs native evidence.
        receipt = (record.get("result") or {}).get("control_receipt")
        if isinstance(receipt, dict) and receipt:
            producer_evidence[f"{record.get('step_id')}:control_receipt"] = json.dumps(receipt, ensure_ascii=False)
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8")) if checkpoint_path.is_file() else {}
    if checkpoint.get("plan_hash") == plan.get("plan_hash") and checkpoint.get("authority_review_state"):
        state.hook_state.setdefault("_data_authority_review", checkpoint["authority_review_state"])
    try:
        authority_review = await review_delivery_against_authority(
            state,
            files=review_files,
            preprocessing_request=preprocessing_request,
            work_order=work_order,
            plan_context={
                **plan,
                "delivery_destination": str(final),
                "producer_map": producer_map,
            },
            deterministic_review=deterministic_review,
            package_dir=staging,
            producer_evidence=producer_evidence,
        )
    finally:
        # Persist the current review window even if the call is cancelled.
        if checkpoint.get("plan_hash") == plan.get("plan_hash") and state.hook_state.get("_data_authority_review"):
            checkpoint["authority_review_state"] = state.hook_state["_data_authority_review"]
            write_object(checkpoint_path, json.loads(json.dumps(checkpoint, default=str)))
    if authority_review.get("status") == "needs_input":
        return authority_review
    if authority_review.get("status") == "error":
        return {
            "status": "externally_blocked",
            "outcome": "externally_blocked",
            "failure_category": str(
                authority_review.get("failure_category") or "review_protocol_error"
            ),
            "error": authority_review.get("summary") or "Authority review protocol failed.",
            "review": authority_review,
            "required_dependencies": authority_review.get("required_dependencies") or [],
            "review_report": str(save_failed_review(state, staging, authority_review)),
            "retry_target": "authority_review",
        }
    delivery_files = _bind_delivery_assets(plan, delivery_files, authority_review.get("asset_bindings"))
    review_context["delivery_files"] = delivery_files
    resolved_assets = _required_asset_review(review_files, review_context)
    deterministic_review["common_checks"].update(resolved_assets["checks"])
    deterministic_review["issues"] = [
        issue for issue in deterministic_review.get("issues") or []
        if issue.get("code") != "required_asset_unfulfilled"
    ] + resolved_assets["issues"]
    deterministic_review["status"] = "fail" if any(
        issue.get("severity") == "critical" for issue in deterministic_review["issues"]
    ) else "pass"
    deterministic_review.pop("revision_contract", None)
    asset_by_path.update({item["path"]: str(item.get("asset_id") or "") for item in delivery_files})
    attach_deliverable_ids(deterministic_review)
    review = combine_reviews(deterministic_review, authority_review)
    attach_deliverable_ids(review)
    if review.get("status") != "pass":
        report = save_failed_review(state, staging, review)
        return {
            "status": "review_failed",
            "review_report": str(report),
            "revision_contract": review.get("revision_contract"),
            "review_progress": _review_progress(review, delivery_files),
            "message": "Staged assets do not satisfy the locked preprocessing request.",
        }

    manifest = dict(candidate.get("manifest") or {})
    manifest.update({
        "request_id": preprocessing_request.get("request_id"),
        "request_spec_hash": preprocessing_request.get("request_spec_hash"),
        "review_profile": review_profile,
        "preprocessing_request": preprocessing_request,
        "preprocessing_work_order": work_order,
    })
    data_model = manifest.get("data_model") if isinstance(manifest.get("data_model"), dict) else {}
    manifest["data_model"] = {**data_model, "files": delivery_files}
    gates = [dict(item) for item in manifest.get("quality_gates") or [] if isinstance(item, dict)]
    gates.append({
        "name": "preprocessing_package_review",
        "status": "pass",
        "evidence": review.get("common_checks") or {},
    })
    manifest["quality_gates"] = gates
    receipt = build_review_receipt(
        review_profile=review_profile,
        preprocessing_request=preprocessing_request,
        work_order=work_order,
        package_review=deterministic_review,
        authority_review=authority_review,
        package_dir=staging,
        delivery_files=delivery_files,
    )
    if receipt.get("status") != "pass":
        report = save_failed_review(state, staging, receipt)
        return {
            "status": "review_failed",
            "review_report": str(report),
            "revision_contract": receipt.get("revision_contract"),
            "review_progress": _review_progress(receipt, delivery_files),
            "message": "The authority-specific review receipt is incomplete.",
        }
    manifest["quality_gates"].extend(receipt.get("quality_gates") or [])
    manifest["review_receipt"] = receipt
    manifest["preprocessing_review"] = review
    manifest["warnings"] = authority_review.get("warnings") or []
    # 判决拆除 O9：未评审 plan 的产物打 plan_approval_status:unapproved。
    manifest.setdefault("plan_approval_status", str(
        getattr(state, "hook_state", {}).get("_plan_approval_status") or "approved"))
    # A caller's manifest is a reviewed asset, not the framework's mutable
    # delivery receipt. Preserve its content and hash just like any other file.
    manifest_relative = "audit/delivery_manifest.json" if any(
        item.get("path") == "manifest.json" for item in delivery_files
    ) else "manifest.json"
    manifest = remap_published_paths(manifest, staging, final)
    (staging / manifest_relative).parent.mkdir(parents=True, exist_ok=True)
    (staging / manifest_relative).write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    write_review_receipt(staging, remap_published_paths(receipt, staging, final))
    content = {
        "artifact_kind": "data_preprocessing_delivery",
        "delivery_complete": True,
        "manifest_contract": {"complete": True, "required_fields": sorted(manifest)},
        **manifest,
        **dict(candidate.get("delivery_fields") or {}),
        "files": delivery_files,
        "package_dir": str(final),
        "manifest_path": str(final / manifest_relative),
        "review_receipt": receipt,
        "preprocessing_review": review,
        "workspace_exports": [
            {"package_path": item["path"], "destination": str(PurePosixPath(asset["declared_output_path"]) / PurePosixPath(item["path"]).relative_to(item["asset_root"]))
                 if item.get("asset_root") else asset["declared_output_path"], "sha256": item["sha256"],
             "edit_baseline": (source_assets.get(str(asset.get("edit_source"))) or {}).get("local_match") or {}}
            for asset in plan.get("required_deliverables") or []
            if asset.get("output_path_is_explicit") is True and getattr(state, "workspace_root", None)
            and PurePosixPath(str(asset.get("declared_output_path") or "")).parts[:1]
                == (Path(state.workspace_root).name,)
            for item in delivery_files if str(item.get("asset_id")) == str(asset.get("id"))
        ],
    }
    publication = publish_dataset_delivery(
        state,
        staging=staging,
        final=final,
        artifact_name=str(candidate.get("artifact_name") or "preprocessing_bundle"),
        content=content,
        metadata={
            **dict(candidate.get("metadata") or {}),
            "deliverable_valid": True,
            "preprocessing_review_status": "pass",
            "package_dir": str(final),
            "manifest_path": str(final / manifest_relative),
            "request_id": preprocessing_request.get("request_id"),
            "request_spec_hash": preprocessing_request.get("request_spec_hash"),
            "review_profile": review_profile,
        },
    )
    if candidate.get("cleanup_workspaces") is True:
        cleanup_generated_workspaces(state)
    return {
        "status": "success",
        "artifact_id": publication["artifact"].get("id"),
        "package_dir": publication["package_dir"],
        "published_files": publication["written_files"],
        "warnings": authority_review.get("warnings") or [],
    }


async def _publish_plan_delivery(
    state: State,
    plan: dict[str, Any],
    results: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Materialize all approved outputs before the single delivery review."""
    for record in reversed(results):
        result = record.get("result") if isinstance(record, dict) else None
        candidate = result.get("delivery_candidate") if isinstance(result, dict) else None
        if isinstance(candidate, dict):
            return await _publish_generic_artifact_delivery(state, plan, results, candidate)
    return await _publish_generic_artifact_delivery(state, plan, results)


async def _publish_generic_artifact_delivery(
    state: State,
    plan: dict[str, Any],
    results: list[dict[str, Any]],
    candidate: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Publish successful per-file generation into the stage-aware dataset contract."""
    def local_materialization(item: Any) -> bool:
        """Use one existing publisher path for local and verified downloaded files."""
        if not isinstance(item, dict):
            return False
        if str(item.get("source_strategy") or "").strip().lower() in LOCAL_REUSE_STRATEGIES:
            return True
        local_match = item.get("local_match") if isinstance(item.get("local_match"), dict) else {}
        return bool(
            str(local_match.get("path") or local_match.get("saved_path") or "").strip()
            and str(item.get("reference_status") or "").strip().lower()
            in {"resolved", "downloaded_verified"}
        )

    step_by_id = {
        str(step["id"]): step for step in plan.get("generation_steps") or []
        if isinstance(step, dict) and step.get("id")
    }
    has_generated_artifacts = any(
        _step_artifact_paths(step_by_id.get(str(record.get("step_id"))) or {}, record.get("result") or {})
        for record in results
    )
    has_local_reuse = any(
        local_materialization(item)
        for item in plan.get("required_deliverables") or []
    )
    if not has_generated_artifacts and not has_local_reuse:
        if candidate is not None:
            return await _review_and_publish_candidate(state, plan, candidate, results)
        return None
    analysis = plan.get("requirement_analysis") if isinstance(plan.get("requirement_analysis"), dict) else {}
    preprocessing_request = plan.get("preprocessing_request") if isinstance(plan.get("preprocessing_request"), dict) else {}
    work_order = plan.get("preprocessing_work_order") if isinstance(plan.get("preprocessing_work_order"), dict) else {}
    if candidate is not None:
        staging, final = package_paths(state, str(preprocessing_request.get("delivery_name") or ""))
        if (Path(candidate["staging_dir"]).resolve(), Path(candidate["final_dir"]).resolve()) != (
            staging.resolve(), final.resolve()
        ):
            raise ValueError("Delivery candidate is outside the canonical data package paths.")
    else:
        staging, final = prepare_package_staging(
            state, str(preprocessing_request.get("delivery_name") or ""),
        )
    review_profile = str(plan.get("review_profile") or preprocessing_request.get("review_profile") or "plan_bound")
    def slug(value: str, fallback: str) -> str:
        text = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value or "").strip()).strip("._-")
        return text or fallback

    declared_stages = [
        str(stage.get("id") or "").strip()
        for stage in analysis.get("calculation_stages") or []
        if isinstance(stage, dict) and str(stage.get("id") or "").strip()
    ]
    deliverable_by_id = {
        str(item.get("id") or ""): item
        for item in plan.get("required_deliverables") or []
        if isinstance(item, dict) and str(item.get("id") or "")
    }

    def source_for_output(record: dict[str, Any], output_id: str) -> tuple[Path, dict[str, Any]] | None:
        result_payload = record.get("result") or {}
        provenance = result_payload.get("artifact_provenance") or {}
        raw_path = _step_artifact_paths(step_by_id.get(str(record.get("step_id"))) or {}, result_payload).get(output_id)
        if not raw_path:
            return None
        source = Path(str(raw_path)).expanduser().resolve()
        if not source.is_file() and not source.is_dir():
            raise ValueError(f"Generated artifact {output_id} is missing.")
        return source, provenance.get(str(output_id)) or {
            "source": "approved_generation_step",
            "step_id": record.get("step_id"),
        }

    files = _bind_delivery_assets(plan, [dict(item) for item in (candidate or {}).get("delivery_files") or []])
    staged_ids = {
        str(asset_id) for item in files
        for asset_id in [item.get("id"), item.get("asset_id"), *(item.get("asset_ids") or [])]
        if asset_id
    }
    published_target_owners = {str(item["path"]): str(item.get("asset_id") or item.get("id") or item["path"]) for item in files}

    def reserve_target(relative_path: PurePosixPath, output_id: str) -> Path:
        target = (staging / Path(str(relative_path))).resolve()
        target.relative_to(staging.resolve())
        target_key = str(target.relative_to(staging.resolve()))
        existing_owner = published_target_owners.get(target_key)
        if existing_owner is not None and existing_owner != output_id:
            raise ValueError(
                "Approved plan assigns multiple deliverables to one downstream file: "
                f"{existing_owner} and {output_id} -> {target_key}"
            )
        published_target_owners[target_key] = output_id
        target.parent.mkdir(parents=True, exist_ok=True)
        return target

    def sha256_file(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def runtime_dependencies(value: Any) -> list[str]:
        values = value if isinstance(value, (list, tuple, set)) else [value]
        result: list[str] = []
        seen: set[str] = set()
        for item in values:
            text = str(item or "").strip()
            key = re.split(r"[<>=!~;\s\[]", text, maxsplit=1)[0].replace("_", "-").casefold()
            if text and key not in seen:
                seen.add(key)
                result.append(text)
        return result

    # A local-reuse contract has already been matched against a real input by
    # the planner.  Materialize it here rather than pretending it was written
    # by a generation step.  Large assets remain in place and get a stable
    # stage-local reference unless the contract explicitly requires the file
    # itself at a delivery path.
    copy_limit_bytes = 64 * 1024 * 1024
    for deliverable in plan.get("required_deliverables") or []:
        if (not local_materialization(deliverable)
                or deliverable.get("delivery_required") is False
                or str(deliverable.get("id")) in staged_ids):
            continue
        local_match = deliverable.get("local_match") if isinstance(deliverable.get("local_match"), dict) else {}
        from .input_inspection import _resolve
        source = _resolve(state, str(local_match.get("path") or local_match.get("saved_path") or deliverable.get("declared_output_path") or ""))
        if not source.is_file():
            return {
                "status": "review_failed",
                "revision_contract": revision_contract([{
                    "code": "local_source_binding_unavailable",
                    "deliverable_id": str(deliverable.get("id") or ""),
                    "message": f"Locally reused asset is unavailable: {source}",
                    "required_change": "Reconcile local_match with inspected inputs and verified path aliases; do not regenerate or substitute the original source.",
                }], scope="plan"),
            }
        source = source.resolve()
        expected_hash = str(local_match.get("sha256") or deliverable.get("sha256") or "").strip().lower()
        actual_hash = ""
        if source.stat().st_size <= copy_limit_bytes or expected_hash:
            actual_hash = sha256_file(source)
        if expected_hash and actual_hash != expected_hash:
            raise ValueError(f"Locally reused asset hash mismatch: {source}")
        stage_id = str(deliverable.get("stage_id") or "").strip()
        deliverable_id = str(deliverable.get("id") or source.name)
        relative_path = _artifact_delivery_path(plan, {"stage_id": stage_id}, deliverable_id, source)
        materialization_source = (
            "local_reuse"
            if str(deliverable.get("source_strategy") or "").strip().lower() == "local_reuse"
            else "verified_reference_download"
        )
        provenance = {
            "source": materialization_source,
            "original_path": str(source),
            "match_basis": list(local_match.get("match_basis") or []),
            "inventory_provenance": local_match.get("provenance"),
        }
        if source.stat().st_size <= copy_limit_bytes or deliverable.get("output_path_is_explicit") is True:
            target = reserve_target(relative_path, deliverable_id)
            if target != source:
                shutil.copy2(source, target)
            copied_hash = actual_hash or sha256_file(target)
            files.append({
                "id": deliverable_id,
                "path": str(target.relative_to(staging)),
                "step_id": "publisher_local_reuse",
                "stage_id": stage_id,
                "size": target.stat().st_size,
                "sha256": copied_hash,
                "runtime_dependencies": [],
                "provenance": {**provenance, "materialization": "copied_local_asset"},
            })
        else:
            reference = reserve_target(
                relative_path.parent / f"{slug(deliverable_id, 'local_asset')}.local_reference.json",
                deliverable_id,
            )
            identity = {
                "size_bytes": source.stat().st_size,
                "mtime_ns": source.stat().st_mtime_ns,
                "sha256": actual_hash or None,
            }
            reference.write_text(json.dumps({
                "mode": "referenced_local_asset",
                "path": str(source),
                "identity": identity,
                "format": deliverable.get("format") or local_match.get("format"),
                "downstream_read_contract": "Read the verified source path directly; do not duplicate this large asset into the preprocessing package.",
            }, indent=2, ensure_ascii=False), encoding="utf-8")
            files.append({
                "id": deliverable_id,
                "path": str(reference.relative_to(staging)),
                "step_id": "publisher_local_reuse",
                "stage_id": stage_id,
                "size": reference.stat().st_size,
                "sha256": sha256_file(reference),
                "runtime_dependencies": [],
                "provenance": {**provenance, "materialization": "referenced_local_asset", "source_identity": identity},
            })
        staged_ids.add(deliverable_id)
    for record in results:
        step_id = str(record.get("step_id") or "")
        step = step_by_id.get(step_id) or {}
        output_ids = [str(item) for item in step.get("outputs") or []]
        for output_id in output_ids:
            if (output_id in staged_ids
                    or (deliverable_by_id.get(output_id) or {}).get("delivery_required") is False):
                continue
            source_record = source_for_output(record, output_id)
            if source_record is None:
                continue
            source, provenance = source_record
            bundle = source if source.is_dir() else None
            root_relative = _artifact_delivery_path(plan, step, output_id, source)
            members = sorted(path for path in source.rglob("*") if path.is_file()) if bundle else [source]
            if not members:
                raise ValueError(f"Generated directory asset {output_id} is empty.")
            for source in members:
                if bundle and not source.resolve().is_relative_to(bundle):
                    raise ValueError(f"Directory asset {output_id} contains a file outside its source tree.")
                expected_hash = "" if bundle else str(provenance.get("sha256") or "").strip().lower()
                actual_hash = sha256_file(source)
                if expected_hash and actual_hash != expected_hash:
                    raise ValueError(
                        "Generated artifact changed after its executor validation; "
                        f"refusing to publish {output_id} from shared or mutated staging path {source}."
                    )
                # Assembly may preserve a native tool's filename. Bind its verified
                # bytes to the logical output instead of regenerating or duplicating
                # an already staged asset. Explicit caller paths remain mandatory.
                equivalent = [str(item["path"]) for item in files
                              if item.get("sha256") == actual_hash
                              and (staging / item["path"]).is_file()
                              and sha256_file(staging / item["path"]) == actual_hash]
                if not bundle and len(equivalent) == 1 and matching_asset_paths(
                    deliverable_by_id.get(output_id) or {}, set(equivalent), equivalent,
                    plan=plan,
                ):
                    _bind_delivery_assets(plan, files, [{"asset_id": output_id, "paths": equivalent}])
                    staged_ids.add(output_id)
                    continue
                relative_path = root_relative / source.relative_to(bundle).as_posix() if bundle else root_relative
                target = reserve_target(relative_path, str(output_id))
                target.write_bytes(source.read_bytes())
                step_spec = (
                    (step.get("tool_arguments") or {}).get("artifact_spec")
                    if isinstance(step.get("tool_arguments"), dict)
                    else {}
                )
                declared_runtime_dependencies = provenance.get("runtime_dependencies")
                if not declared_runtime_dependencies:
                    result_payload = record.get("result") or {}
                    declared_runtime_dependencies = result_payload.get("runtime_dependencies")
                if not declared_runtime_dependencies and isinstance(step_spec, dict):
                    declared_runtime_dependencies = step_spec.get("runtime_dependencies")
                # The producer path is private staging state. The stable path for
                # downstream consumers is the package-relative path above.
                published_provenance = {
                    key: value for key, value in provenance.items() if key != "path"
                }
                stage_id = str(step.get("stage_id") or (deliverable_by_id.get(output_id) or {}).get("stage_id") or "").strip()
                files.append({
                    "id": str(output_id),
                    **({"asset_root": str(root_relative)} if bundle else {}),
                    "path": str(target.relative_to(staging)),
                    "step_id": step_id,
                    "stage_id": stage_id,
                    "size": target.stat().st_size,
                    "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
                    "runtime_dependencies": runtime_dependencies(
                        declared_runtime_dependencies
                    ),
                    "provenance": published_provenance,
                })
    if not files:
        raise ValueError("No generated artifacts were available for package publication.")
    if candidate is not None:
        return await _review_and_publish_candidate(state, plan, {**candidate, "delivery_files": files}, results)
    delivered_by_stage = {
        stage_id: [item["path"] for item in files if item.get("stage_id") == stage_id]
        for stage_id in declared_stages
    }
    stage_contracts = {
        str(item.get("stage_id") or ""): item
        for item in analysis.get("stage_input_contracts") or []
        if isinstance(item, dict) and str(item.get("stage_id") or "")
    }
    publishable_deliverable_ids = {
        str(item.get("id") or "")
        for item in plan.get("required_deliverables") or []
        if isinstance(item, dict)
        and str(item.get("id") or "")
        and item.get("required", True)
        and item.get("delivery_required") is not False
        and str(item.get("fulfillment_kind") or "") != "runtime_output"
    }
    delivered_ids = {str(item.get("id") or "") for item in files}
    def stage_record(stage_id: str) -> dict[str, Any]:
        contract = stage_contracts.get(stage_id) or {}
        launch_ids = [
            str(item)
            for key in ("local_parameter_ids", "external_input_ids")
            for item in contract.get(key) or []
            if str(item) in publishable_deliverable_ids
        ]
        missing_local = [item for item in launch_ids if item not in delivered_ids]
        upstream = [str(item) for item in contract.get("upstream_runtime_stage_ids") or [] if str(item)]
        generated = delivered_by_stage.get(stage_id) or []
        if missing_local:
            status = "blocked_missing_inputs"
            missing = [f"local_parameter:{item}" for item in missing_local]
        elif not generated and upstream:
            status = "awaiting_upstream_runtime_inputs"
            missing = [f"upstream_runtime_stage:{item}" for item in upstream]
        elif generated:
            status = "candidate_generated"
            missing = []
        else:
            status = "blocked_missing_inputs"
            missing = ["No stage-local parameter or external-input contract was generated."]
        return {
            "id": stage_id,
            "status": status,
            "execution_kind": "asset_generation",
            "generated_files": generated,
            "missing_inputs": missing,
            "input_contract": contract,
            "case_root": str(Path("stages") / slug(stage_id, "stage")),
        }
    stage_records = [
        stage_record(stage_id)
        for stage_id in declared_stages
    ]
    package_runtime_dependencies = sorted(runtime_dependencies([
        dependency
        for item in files
        for dependency in runtime_dependencies(item.get("runtime_dependencies"))
    ]), key=str.casefold)
    package_runtime_dependencies_by_stage = {
        stage_id: sorted(runtime_dependencies([
            dependency
            for item in files
            if str(item.get("stage_id") or "") == stage_id
            for dependency in runtime_dependencies(item.get("runtime_dependencies"))
        ]), key=str.casefold)
        for stage_id in declared_stages
    }
    manifest = {
        "objective": plan.get("task_summary") or "Preprocessing artifact delivery",
        "data_model": {"kind": "preprocessing_artifact_bundle", "files": files},
        "runtime_dependencies": package_runtime_dependencies,
        "runtime_dependencies_by_stage": package_runtime_dependencies_by_stage,
        "source": {
            "kind": "approved_preprocessing_work_order",
            "plan_id": plan.get("plan_id"),
            "work_order_id": work_order.get("work_order_id"),
        },
        "semantics": "Each requested file was materialized from the locked authority contract and reviewed before publication.",
        "measurement_context": {},
        "plan_approval_status": str(
            getattr(state, "hook_state", {}).get("_plan_approval_status") or "approved"),
        "quality_gates": [
            {"name": "per_file_generation", "status": "pass", "evidence": files},
        ],
        "lineage": [
            {"op": str((item.get("provenance") or {}).get("materialization") or "generate_preprocessing_artifact"), "output": item}
            for item in files
        ],
        "assumptions": plan.get("assumptions") or [],
        "reproducibility": {
            "plan_hash": plan.get("plan_hash"),
            "workspace_layout": "<declared_file>" if review_profile == "request_bound" or not declared_stages else "stages/<stage>/<declared_file>",
            "tool_versions": {"data_preprocessing_executor": "1"},
        },
        "downstream_contract": {"artifact_type": "dataset", "files": [item["path"] for item in files]},
        "request_id": preprocessing_request.get("request_id"),
        "request_spec_hash": preprocessing_request.get("request_spec_hash"),
        "review_profile": review_profile,
        "preprocessing_request": preprocessing_request,
        "preprocessing_work_order": work_order,
        "calculation_stages": analysis.get("calculation_stages") or [],
        "stage_records": stage_records,
    }
    return await _review_and_publish_candidate(state, plan, {
        "staging_dir": str(staging),
        "final_dir": str(final),
        "artifact_name": "generic_preprocessing_bundle",
        "discipline": str((plan.get("discipline") or {}).get("primary") or "unknown"),
        "preprocessing_request": preprocessing_request,
        "preprocessing_work_order": work_order,
        "review_profile": review_profile,
        "review_context": {
            "calculation_stages": analysis.get("calculation_stages") or [],
            "stage_input_contracts": analysis.get("stage_input_contracts") or [],
            "stage_records": stage_records,
            "allow_contract_only_delivery": True,
        },
        "manifest": manifest,
        "delivery_files": files,
        "metadata": {"runtime_dependencies": package_runtime_dependencies},
    }, results)


_FORBIDDEN = {
    "execute_preprocessing_plan",
    "run_preprocessing_planning_loop",
    "design_preprocessing_plan",
    "critique_preprocessing_plan",
}

# These statuses are control-flow transitions, not generic execution errors.
# The pipeline driver consumes them and either approves the single reference
# request or terminates recoverably.  Keeping the distinction here prevents a
# failed generation step from being silently re-planned as another generation
# plan.
_PIPELINE_TRANSITION_STATUSES = {
    "needs_reference_search",
    "needs_geometry_processing",
    "needs_revision",
    "needs_input",
    "externally_blocked",
}
_PLAN_CONTRACT_FAILURE_STATUSES = {
    "not_authorized_by_plan",
    "plan_contract_error",
    "execution_contract_error",
}

_MESH_USEFUL_SUFFIXES = {
    ".dat", ".txt", ".xy",
    ".geo", ".msh", ".stl", ".step", ".stp", ".iges", ".igs", ".brep",
    ".cas", ".cgns", ".foam", ".vtk", ".vtu", ".med", ".unv", ".exo", ".ex2", ".e",
}

_TRANSIENT_ERROR_PATTERN = re.compile(
    r"(?i)(?:\b(?:timeout|\w*Timeout(?:Error|Exception)?)\b|timed out|readerror|connecterror|connection reset|"
    r"remoteprotocolerror|peer closed|incomplete chunked|server disconnected|"
    r"temporar(?:y|ily)|http\s*(?:408|425|429|5\d\d)|"
    r"rate.?limit|service unavailable|bad gateway|gateway timeout)"
)
_INTERNAL_CONTRACT_ERROR_PATTERN = re.compile(
    r"(?i)^(?:TypeError|AttributeError|KeyError|IndexError|AssertionError|"
    r"UnboundLocalError|NotImplementedError):"
)


def _execution_failure_category(result: dict[str, Any]) -> str:
    """Classify tool failures by recovery policy, independent of discipline."""
    if result.get("failure_category") in {
        "framework_report_error", "repair_unsupported", "repair_exhausted", "review_protocol_error",
    }:
        return result["failure_category"]
    if result.get("status") == "externally_blocked":
        return ("environment_required" if result.get("failure_category") == "environment_required"
                or (result.get("resume_contract") or {}).get("kind") == "environment_required"
                else str(result.get("failure_category") or "external_dependency_unavailable"))
    if any(result.get(key) for key in (
        "parameter_violations", "missing_parameters", "argument_shape_mismatches",
    )):
        return "plan_contract_error"
    if str(result.get("status_class") or "").strip().lower() in {
        "transient",
        "provider_unavailable",
        "rate_limited",
    }:
        return "transient_execution_error"
    if str(result.get("status_class") or "").strip().lower() in {
        "permanent", "size_limit", "not_downloadable_asset", "unrecognized_scientific_asset",
    }:
        return "deterministic_execution_error"
    status = str(result.get("status") or "").strip().lower()
    if status == "needs_input":
        return "human_input_required"
    if status in {"needs_reference_search", "needs_search"}:
        return "reference_asset_required"
    if status == "review_failed":
        return "generated_asset_review_failed"
    if status in _PLAN_CONTRACT_FAILURE_STATUSES:
        return "plan_contract_error"
    if status == "blocked":
        return "deterministic_execution_error"
    if status != "error":
        return "none"
    error = str(result.get("error") or "").strip()
    if re.search(r"(?i)plan_contract_error|not_authorized_by_plan", error):
        return "plan_contract_error"
    if _TRANSIENT_ERROR_PATTERN.search(error):
        return "transient_execution_error"
    if _INTERNAL_CONTRACT_ERROR_PATTERN.search(error):
        return "internal_tool_contract_error"
    if re.search(r"(?i)(?:not authorized|not allowed|requires an explicit gated adapter)", error):
        return "plan_authorization_error"
    if re.search(r"(?i)(?:not registered|command not found|no such executable)", error):
        return "environment_required"
    return "deterministic_execution_error"


def _revision_contract_from_failure(
    step_id: str,
    tool_name: str,
    result: dict[str, Any],
) -> dict[str, Any] | None:
    """Return a bounded one-step revision contract for actionable failures.

    Recovery is classified from the outcome, never from a discipline or tool
    name.  Human/scientific input, external evidence and unavailable runtimes
    use their dedicated transitions; every other diagnosed local failure may
    amend only its owning step while the request authority remains locked.
    """
    if str(result.get("status") or "").strip().lower() not in {
        "error", "blocked", "needs_revision", "review_failed", "needs_download_selection",
    }:
        return None
    diagnostics = _structured_failure_issues(result)
    category = _execution_failure_category(result)
    if category in {
        "environment_required",
        "human_input_required", "reference_asset_required",
        "framework_report_error", "review_protocol_error",
    }:
        return None
    declared = pipeline_outcome(result).get("revision_contract")
    if isinstance(declared, dict) and declared.get("scope") == "plan" and declared.get("issues"):
        return declared
    if category == "transient_execution_error" and (
        not result.get("url") or int(result.get("execution_attempts") or 0) < 2
        or result.get("repair_history") or result.get("local_repair_exhausted")
    ):
        return None
    issues = [
        {
            **item,
            "step_id": item.get("step_id") or step_id,
            "tool_name": item.get("tool_name") or tool_name,
            "failure_category": item.get("failure_category") or category,
        }
        for item in diagnostics[:8]
    ]
    parameter_errors = [
        str(item) for key in ("parameter_violations", "missing_parameters", "argument_shape_mismatches")
        for item in result.get(key) or []
    ]
    error = "; ".join(parameter_errors) or str(
        result.get("error")
        or result.get("message")
        or result.get("blocked_reason")
        or result.get("stderr_tail")
        or result.get("stdout_tail")
        or ""
    ).strip()
    if not error and not issues:
        return None
    if error and not issues:
        issues.append({
            "code": category,
            "severity": "critical",
            "step_id": step_id,
            "tool_name": tool_name,
            "message": error,
            "diagnostic_context": {**(result.get("repair_context") or {}), **{key: result[key] for key in (
                "url", "http_status", "status_class", "response_excerpt",
                "expected_bytes", "max_bytes", "bytes_received",
            ) if result.get(key) not in (None, "", [], {})}},
            "required_change": (
                "Correct only this step from the locked work order and diagnostic. Preserve all "
                "authoritative scientific values; request input if a new value is required."
            ),
        })
    return revision_contract(issues, scope=("plan" if result.get("local_repair_exhausted")
                             or category in {"repair_unsupported", "repair_exhausted"} else "step"),
                             affected_ids=[step_id])


def _tool_repair_field(definition: ToolDefinition | None) -> str:
    # A parameter schema is not a repair interface. Preserve declared adapters;
    # do not ask a model to rewrite arbitrary tool calls after a failure.
    properties = (
        definition.parameters_schema.get("properties") or {}
        if definition is not None and isinstance(definition.parameters_schema, dict)
        else {}
    )
    return next(
        (name for name in ("repair_feedback", "review_issues", "revision_contract", "code") if name in properties),
        "",
    )


def _report_rebuild_contract(tool_name: str, result: dict[str, Any]) -> dict[str, Any]:
    contract = result.get("report_rebuild") or {}
    if not contract and tool_name == "build_scientific_preprocessing_package":
        # Backward compatibility uses the same producer declaration as a new
        # result, never the reviewer's claim that a file is editable.
        from .scientific_preprocessor import package_report_rebuild_contract

        contract = package_report_rebuild_contract(result.get("delivery_candidate") or {})
    return contract if contract.get("strategy") == "reinvoke_preserving_dependencies" else {}


def _report_rebuild_covers(result: dict[str, Any], contract: dict[str, Any], feedback: list[dict[str, Any]]) -> bool:
    root = (result.get("delivery_candidate") or {}).get("staging_dir")
    if not root or not feedback:
        return False
    root = Path(root).resolve()
    declared = {path.resolve() for path in _result_material_paths(result) if path.is_file()}
    allowed = {(root / name).resolve() for name in contract.get("files") or []
               if isinstance(name, str) and not Path(name).is_absolute() and ".." not in Path(name).parts}
    allowed = {path for path in allowed if path.is_relative_to(root) and path in declared}
    return bool(allowed) and all(
        (root / str(issue.get("source_file") or issue.get("file") or "")).resolve() in allowed
        for issue in feedback)


def _report_preserved_assets(step: dict[str, Any], outputs: dict[str, Any]) -> dict[str, str]:
    previous = outputs.get(str(step.get("id"))) or {}
    protected = {str(path): hashlib.sha256(path.read_bytes()).hexdigest()
                 for dependency in step.get("dependencies") or []
                 for path in _result_material_paths(outputs.get(str(dependency)) or {}) if path.is_file()}
    candidate = previous.get("delivery_candidate") or {}
    root = Path(candidate["staging_dir"]).resolve()
    rebuilt = set(_report_rebuild_contract(str(step.get("tool_name")), previous).get("files") or [])
    for item in candidate.get("delivery_files") or []:
        name = str(item.get("path") or "")
        path = (root / name).resolve()
        if name not in rebuilt and path.is_relative_to(root) and path.is_file():
            protected[str(path)] = hashlib.sha256(path.read_bytes()).hexdigest()
    return protected


def _effective_step_arguments(step: dict[str, Any], outputs: dict[str, Any]) -> dict[str, Any]:
    """Keep repaired controls while resolving dependency references afresh."""
    previous = outputs.get(str(step.get("id") or "")) or {}
    return {**dict(step.get("tool_arguments") or {}), **previous.get("effective_arguments", {})}


def _restore_tool_result(state: State, tool_name: str, result: dict[str, Any]) -> dict[str, Any]:
    """Resolve the registry's context-view pointer for internal execution only."""
    if result.get("oversized") is not True:
        return result
    from core.bounded_output import OVERSIZED_DIRNAME

    try:
        path = Path(str(result.get("saved_to") or "")).resolve(strict=True)
        path.relative_to(state.root.resolve() / OVERSIZED_DIRNAME)
        blob = path.read_bytes()
        digest = hashlib.sha256(blob).hexdigest()[:12]
        if path.name != f"{tool_name}__{digest}.json":
            raise ValueError("Stored tool result identity does not match its reference")
        restored = json.loads(blob)
        if not isinstance(restored, dict) or restored.get("oversized") is True:
            raise ValueError("Stored tool result is not a complete result object")
        return restored
    except (OSError, ValueError) as exc:
        return {"status": "externally_blocked", "failure_category": "environment_required",
                "error": f"Cannot restore the complete {tool_name} result: {exc}",
                "saved_to": result.get("saved_to")}


async def _invoke_planned_tool(
    state: State,
    tool_name: str,
    definition: ToolDefinition,
    arguments: dict[str, Any],
    *,
    plan_id: str | None = None,
    plan_kind: str = "preprocessing_generation",
    step_id: str = "",
) -> dict[str, Any]:
    """Execute a plan tool with bounded retries only for transient failures."""
    async def invoke_once(call_arguments: dict[str, Any]) -> dict[str, Any]:
        previous_context = state.hook_state.get("_active_preprocessing_step")
        state.hook_state["_active_preprocessing_step"] = {
            "plan_id": plan_id,
            "plan_kind": plan_kind,
            "step_id": step_id,
            "tool_name": tool_name,
            "tool_arguments": call_arguments,
        }
        try:
            try:
                return await _invoke_authorized_tool(call_arguments)
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                return {
                    "status": "error",
                    "error": error,
                    **(
                        {"status_class": "transient"}
                        if _TRANSIENT_ERROR_PATTERN.search(error) else {}
                    ),
                }
        finally:
            if previous_context is None:
                state.hook_state.pop("_active_preprocessing_step", None)
            else:
                state.hook_state["_active_preprocessing_step"] = previous_context

    async def _invoke_authorized_tool(call_arguments: dict[str, Any]) -> dict[str, Any]:
        if tool_name == "generate_preprocessing_artifact":
            return await generate_preprocessing_artifact(state=state, **call_arguments)
        if tool_name in {"execute_python", "execute_preprocessing_python"}:
            return await execute_preprocessing_python(state=state, **call_arguments)
        if definition.risk_level == "high":
            return {
                "status": "error",
                "error": f"High-risk tool {tool_name!r} requires an explicit gated adapter.",
            }
        return _restore_tool_result(state, tool_name, await execute(tool_name, state, **call_arguments))

    result = await invoke_once(arguments)
    category = _execution_failure_category(result)
    if tool_name == "generate_preprocessing_artifact" and category in {
        "transient_execution_error", "model_service_timeout",
    }:
        # The writer already bounds its response and transport recovery. An
        # outer replay would silently grant the same request a new deadline.
        return {**result, "status": "externally_blocked",
                "failure_category": "model_service_timeout" if category == "model_service_timeout" else "model_service_unavailable"}
    attempts = 1
    # An unchanged call has made no material progress just because its error
    # text (byte counts, timing, request IDs) changed. Give it one retry only.
    while (attempts < 2 and not result.get("retry_exhausted")
           and _execution_failure_category(result) == "transient_execution_error"):
        from .web_search import http_retry_delay

        delay = http_retry_delay(result)
        if delay > float(arguments.get("timeout_seconds") or 600):
            break  # Never shorten a server cooldown to fit this execution budget.
        attempts += 1
        emit_progress(
            state,
            "execute_step_retry",
            tool_name,
            attempt=attempts,
            failure_category=_execution_failure_category(result),
            error=str(result.get("error") or "")[:400],
            retry_delay_seconds=delay,
        )
        while delay > 0:
            interval = min(delay, 30.0)
            await asyncio.sleep(interval)
            delay -= interval
            if delay > 0:
                emit_progress(state, "execute_step_retry_wait", tool_name, remaining_seconds=delay)
        result = await invoke_once(arguments)
    if (result.get("status_class") == "rate_limited"
            or (tool_name == "data_web_download" and _execution_failure_category(result) == "transient_execution_error")):
        # Exhausted transport recovery is not a bad asset contract. Preserve
        # the source and diagnosis instead of asking the model to rewrite URLs.
        rate_limited = result.get("status_class") == "rate_limited"
        result = {**result, "status": "externally_blocked",
                  "message": ("Remote service is rate limited; retry after its cooldown, keeping the source request unchanged."
                              if rate_limited else "Download transport recovery exhausted; retry when the remote service or connection recovers, keeping the source request unchanged."),
                  "resume_contract": {"kind": "rate_limit" if rate_limited else "remote_download",
                                      "url": result.get("url"),
                                      "retry_after": result.get("retry_after", "")}}
    # Semantic repair belongs to the DAG executor, not this invocation layer.
    if attempts > 1:
        result = {**result, "execution_attempts": attempts}
    return result


def _remember_downloaded_geometry_asset(state: State, result: Any) -> None:
    """Persist usable downloaded geometry/mesh paths for the next generation plan.

    Reference-search plans often execute before the final package plan.  The
    downloaded file must survive replanning, otherwise the final plan can loop
    back into another search even though a usable asset was already acquired.
    """
    if not isinstance(result, dict):
        return

    candidates: list[dict[str, Any]] = []
    downloaded = result.get("auto_downloaded_geometry")
    if isinstance(downloaded, dict):
        candidates.append(downloaded)
    if result.get("status") == "success" and (result.get("saved_path") or result.get("preferred_geometry_file")):
        candidates.append(result)
    for value in result.values():
        if isinstance(value, dict):
            nested = value.get("auto_downloaded_geometry")
            if isinstance(nested, dict):
                candidates.append(nested)

    for item in candidates:
        declared_kind = str(item.get("asset_kind") or "").strip().lower()
        if declared_kind and declared_kind not in {"geometry", "geometry_or_mesh", "mesh", "structure"}:
            continue
        path = (
            item.get("preferred_geometry_file")
            or item.get("saved_path")
            or item.get("path")
            or item.get("local_path")
        )
        path_text = str(path or "").strip()
        if not path_text:
            continue
        candidate = Path(path_text).expanduser()
        if not candidate.is_file() or candidate.suffix.lower() not in _MESH_USEFUL_SUFFIXES:
            continue
        state.hook_state["scientific_mesh_pending_asset_evaluation"] = {
            "path": str(candidate),
            "downloaded_path": str(item.get("saved_path") or candidate),
            "url": item.get("url") or (item.get("source_result") or {}).get("url"),
            "sha256": item.get("sha256"),
            "extension": candidate.suffix.lower(),
            "source": "approved_reference_search",
        }
        assets = state.hook_state.setdefault("_data_downloaded_geometry_assets", [])
        if isinstance(assets, list) and not any(str(a.get("path")) == str(candidate) for a in assets if isinstance(a, dict)):
            assets.append(dict(state.hook_state["scientific_mesh_pending_asset_evaluation"]))
        break


def _mentions_mesh(value: Any) -> bool:
    text = json.dumps(value, ensure_ascii=False, default=str) if not isinstance(value, str) else value
    return bool(
        text
        and (
            "computational_mesh" in text
            or "finite element mesh" in text.lower()
            or "polyMesh" in text
            or "mesh asset" in text.lower()
            or "网格" in text
        )
    )


def _plan_step_requires_mesh(plan: dict[str, Any], step: dict[str, Any]) -> bool:
    output_ids = {str(item) for item in step.get("outputs") or []}
    for deliverable in plan.get("required_deliverables") or []:
        if not isinstance(deliverable, dict):
            continue
        deliverable_id = str(deliverable.get("id") or "")
        if deliverable_id and output_ids and deliverable_id not in output_ids:
            continue
        if _mentions_mesh(deliverable):
            return True
    return _mentions_mesh({
        "action": step.get("action"),
        "tool_capability": step.get("tool_capability"),
        "outputs": step.get("outputs"),
        "verification": step.get("verification"),
    })


def _apply_plan_argument_defaults(
    plan: dict[str, Any],
    step: dict[str, Any],
    arguments: dict[str, Any],
    outputs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if step.get("tool_name") in {"generate_preprocessing_artifact", "execute_preprocessing_python", "execute_python"}:
        request = plan.get("preprocessing_request") or {}
        if step.get("tool_name") == "generate_preprocessing_artifact":
            delivery_paths = {
                str(Path(path).resolve()): str(_artifact_delivery_path(plan, producer, asset_id, Path(path)))
                for producer in plan.get("generation_steps") or []
                for asset_id, path in _step_artifact_paths(
                    producer, (outputs or {}).get(str(producer.get("id"))) or {},
                ).items()
                if any(item.get("id") == asset_id and item.get("delivery_required") is not False
                        for item in plan.get("required_deliverables") or [])
            }
            # Assembly can preserve native filenames instead of the proposed
            # path. Use the same byte identity as publication, not a guessed
            # filename or an asset ID that a downstream file cannot open.
            explicit_sources = {str(Path(path).resolve())
                                for producer in plan.get("generation_steps") or []
                                for asset_id, path in _step_artifact_paths(
                                    producer, (outputs or {}).get(str(producer.get("id"))) or {},
                                ).items()
                                if any(item.get("id") == asset_id and item.get("output_path_is_explicit") is True
                                       for item in plan.get("required_deliverables") or [])}
            for source in delivery_paths:
                source_path = Path(source)
                if not source_path.is_file() or source in explicit_sources:
                    continue
                digest = hashlib.sha256()
                with source_path.open("rb") as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(chunk)
                source_hash = digest.hexdigest()
                staged = {str(item["path"]) for result in (outputs or {}).values()
                          for item in (result.get("delivery_candidate") or {}).get("delivery_files") or []
                          if item.get("path") and item.get("sha256") == source_hash}
                if len(staged) == 1:
                    delivery_paths[source] = staged.pop()
            consumers = []
            for consumer in plan.get("generation_steps") or []:
                consumer_args = consumer.get("tool_arguments") or {}
                if consumer_args.get("script_asset") not in (step.get("outputs") or []):
                    continue
                bound = (_apply_plan_argument_defaults(plan, consumer, consumer_args, outputs)
                         if consumer.get("tool_name") in {"execute_preprocessing_python", "execute_python"}
                         else consumer_args)
                consumers.append({
                    "tool_name": consumer.get("tool_name"), "script_asset": consumer_args.get("script_asset"),
                    "inputs": consumer.get("inputs"), "output_paths": consumer_args.get("output_paths"),
                    "materialized_input_files": bound.get("input_files", {}),
                    "input_metadata": bound.get("input_metadata", {}),
                })
            arguments = {**arguments, "artifact_spec": {
                **(arguments.get("artifact_spec") or {}),
                "runtime_dependencies": (arguments.get("artifact_spec") or {}).get("runtime_dependencies")
                                        or step.get("runtime_dependencies") or [],
                "original_request": caller_request_text(request, preprocessing_request=request),
                "delivery_paths": delivery_paths,
                "execution_consumers": consumers,
                "native_mesh_executions": [{key: result[key] for key in
                    ("gmsh_cmd", "solver_export", "geometry_file", "copied_geometry_file", "mesh_file")
                    if key in result} for result in (outputs or {}).values()
                    if result.get("status") == "success" and result.get("gmsh_cmd")],
                "related_artifact_evidence": _file_evidence({
                    delivery_paths.get(path, path): evidence
                    for peer_id, result in (outputs or {}).items()
                    if peer_id != str(step.get("id") or "") and result.get("status") == "success"
                    for path, evidence in _result_material_evidence(result).items()
                }, total_limit=12000, line_numbers=True),
            }}
        input_ids = set(step.get("inputs") or []) | set(arguments.get("input_files") or {})
        if step.get("tool_name") == "generate_preprocessing_artifact" and arguments["artifact_spec"].get("edit_source"):
            input_ids.add(arguments["artifact_spec"]["edit_source"])
        sources = {
            str(item["id"]): str((item.get("local_match") or {}).get("path")
                                 or (item.get("local_match") or {}).get("saved_path")
                                 or item.get("requested_path")
                                 or item.get("declared_output_path") or "")
            for item in [*((plan.get("requirement_analysis") or {}).get("required_files") or []),
                         *(plan.get("required_deliverables") or [])]
            if item.get("id") in input_ids
            and str(item.get("source_strategy") or "").lower() in LOCAL_REUSE_STRATEGIES
        }
        metadata = {}
        for dependency in step.get("dependencies") or []:
            result = (outputs or {}).get(str(dependency)) or {}
            producer = next((item for item in plan.get("generation_steps") or []
                             if item.get("id") == dependency), {})
            for key, path in _step_artifact_paths(producer, result).items():
                if key not in input_ids:
                    continue
                sources[key] = path
                provenance = (result.get("artifact_provenance") or {}).get(key) or {}
                if not provenance and str(path) == str(result.get("saved_path")):
                    provenance = result
                metadata[key] = {field: provenance[field] for field in (
                    "url", "sha256", "bytes", "size_bytes", "content_type", "acquired_at",
                ) if field in provenance}
            # Expose native verification receipts as dependency evidence, so
            # report writers can reuse computed checks rather than rerun them.
            sources.update({f"{dependency}.{key}": str(path) for key, path in result.items()
                            if key.endswith("_report") and isinstance(path, str) and Path(path).is_file()})
        if step.get("tool_name") == "generate_preprocessing_artifact":
            return {**arguments, "input_files": sources,
                    "artifact_spec": {**arguments["artifact_spec"], "input_metadata": metadata}}
        return {**arguments, "input_files": sources, "input_metadata": metadata}
    is_package_step = (
        step.get("tool_name") == "build_scientific_preprocessing_package"
        or "preprocessing_package" in str(step.get("tool_capability") or "")
    )
    is_mesh_step = step.get("tool_name") == "prepare_scientific_mesh"
    if not is_package_step and not is_mesh_step:
        return arguments
    adjusted = dict(arguments)
    dependency_outputs = [
        (outputs or {}).get(str(dependency))
        for dependency in step.get("dependencies") or []
    ]
    reviewed_mesh = next((
        result for result in dependency_outputs
        if isinstance(result, dict)
        and result.get("status") == "success"
        and (
            result.get("polyMesh_dir")
            or result.get("mesh_file")
            or result.get("structure_file")
            or result.get("data_model_kind") == "mesh"
        )
    ), None)
    raw_parameters = adjusted.get("parameters")
    try:
        parameters = json.loads(raw_parameters) if isinstance(raw_parameters, str) and raw_parameters.strip() else raw_parameters
    except json.JSONDecodeError:
        return arguments  # Let the tool report invalid input to the existing repair loop.
    if parameters is None or parameters == "":
        parameters = {}
    if not isinstance(parameters, dict):
        return arguments
    parameters = dict(parameters)
    plan_analysis = plan.get("requirement_analysis")
    if isinstance(plan_analysis, dict):
        parameters["requirement_analysis"] = dict(plan_analysis)
        parameters["calculation_stages"] = list(plan_analysis.get("calculation_stages") or [])
    adjusted["parameters"] = json.dumps(parameters, ensure_ascii=False)
    if is_mesh_step:
        from .scientific_mesh import normalize_mesh_operation

        operation = normalize_mesh_operation(adjusted.get("operation"))
        adjusted["operation"] = operation
        geometry_source = next((
            str(path)
            for result in dependency_outputs
            if isinstance(result, dict) and result.get("status") == "success"
            for path in (
                list((result.get("generated_artifacts") or {}).values())
                if isinstance(result.get("generated_artifacts"), dict) else []
            )
            if Path(str(path)).suffix.lower() in _MESH_USEFUL_SUFFIXES
            and Path(str(path)).is_file()
        ), "")
        if geometry_source:
            geometry_suffix = Path(geometry_source).suffix.lower()
            if geometry_suffix in {".dat", ".txt", ".xy"}:
                adjusted["coordinate_files"] = [geometry_source]
            else:
                parameters["geometry_file"] = geometry_source
                # ``coordinate_files`` is exclusively a point-profile input.
                # A revised plan can retain the producer's declared relative
                # output here even though the dependency already supplied an
                # absolute CAD/Gmsh path.  Let dependency resolution own the
                # transport path instead of feeding the stale value to the
                # coordinate-profile parser.
                adjusted.pop("coordinate_files", None)
            adjusted["parameters"] = json.dumps(parameters, ensure_ascii=False)
        if operation == "convert" and reviewed_mesh:
            source = (
                reviewed_mesh.get("mesh_file")
                or reviewed_mesh.get("geometry_file")
                or reviewed_mesh.get("preferred_geometry_file")
            )
            if source:
                parameters["geometry_file"] = source
                adjusted["parameters"] = json.dumps(parameters, ensure_ascii=False)
                adjusted.setdefault("case_dir", reviewed_mesh.get("case_dir") or "")
                adjusted["coordinate_files"] = [source]
        return adjusted
    if reviewed_mesh is not None:
        adjusted.setdefault("mesh_generation_result", reviewed_mesh)
        adjusted["generate_mesh_assets"] = False
    if "generate_mesh_assets" not in adjusted:
        adjusted["generate_mesh_assets"] = _plan_step_requires_mesh(plan, step)
    if "generate_model_assets" not in adjusted:
        adjusted["generate_model_assets"] = _mentions_mesh(step) is False and "target_column" in json.dumps(adjusted, default=str)
    return adjusted


def _reuse_reviewed_mesh_conversion(
    step: dict[str, Any],
    arguments: dict[str, Any],
    outputs: dict[str, Any],
) -> dict[str, Any] | None:
    """Reuse a reviewed OpenFOAM export instead of converting it twice."""
    if step.get("tool_name") != "prepare_scientific_mesh":
        return None
    from .scientific_mesh import normalize_mesh_operation

    if normalize_mesh_operation(arguments.get("operation")) != "convert":
        return None
    for dependency in step.get("dependencies") or []:
        result = outputs.get(str(dependency))
        if not isinstance(result, dict) or result.get("status") != "success":
            continue
        if not result.get("polyMesh_dir"):
            continue
        return {
            **result,
            "workflow_tool": "prepare_scientific_mesh",
            "reused_existing_conversion": True,
            "conversion_source_step": str(dependency),
        }
    return None


async def _repair_step_from_feedback(
    state: State,
    plan: dict[str, Any],
    step: dict[str, Any],
    outputs: dict[str, Any],
    feedback: list[dict[str, Any]],
    *,
    plan_id: str,
    plan_kind: str,
) -> tuple[dict[str, Any], bool]:
    """Regenerate one approved producer without changing its work order."""
    step_id = str(step.get("id") or "")
    tool_name = str(step.get("tool_name") or "")
    previous = outputs.get(step_id) or {}
    if previous.get("pending_repair"):
        previous = {**previous, "status": previous["pending_repair"].get("status", "needs_revision")}
    if (previous.get("revision_contract") or {}).get("scope") == "plan":
        return {**previous, "status": "needs_revision", "local_repair_exhausted": True}, False
    definition = get_tool(tool_name)
    repair_field = _tool_repair_field(definition)
    if definition is None or not repair_field:
        return {
            **(outputs.get(step_id) or {}), "status": "needs_revision",
            "failure_category": "repair_unsupported",
            "issues": feedback,
            "revision_contract": revision_contract(feedback, scope="step", affected_ids=[step_id]),
            "local_repair_exhausted": True,
        }, False
    effective_arguments = _effective_step_arguments(step, outputs)
    arguments = _resolve_refs(effective_arguments, outputs)
    arguments = _apply_plan_argument_defaults(plan, step, arguments, outputs)
    if arguments.get("script_asset"):
        return {**previous, "status": "needs_revision", "issues": feedback,
                "revision_contract": revision_contract(feedback, scope="plan", affected_ids=[step_id]),
                "local_repair_exhausted": True,
                "error": "The bound script needs a writable producer in the DAG; do not replace it with inline code."}, False
    report_only = bool(feedback) and all(issue.get("repair_kind") == "derived_report" for issue in feedback)
    report_rebuild = report_only and _report_rebuild_covers(
        previous, _report_rebuild_contract(tool_name, previous), feedback)
    if report_only and not report_rebuild and repair_field not in {"repair_feedback", "code"}:
        return {**previous, "status": "needs_revision", "issues": feedback,
                "failure_category": "framework_report_error", "local_repair_exhausted": True,
                "revision_contract": revision_contract(feedback, scope="asset", affected_ids=[step_id]),
                "error": "This producer has no declared rebuild capability for the cited computed report. "
                         "Preserve assets and repair the report implementation; parameter changes cannot fix its claims."}, False
    history = [
        {key: attempt[key] for key in ("action", "issue_ids", "parameter_updates", "arguments_hash", "before", "after", "status", "changed", "repair_change") if key in attempt}
        for attempt in (previous.get("repair_history") or [])[-4:]
    ]
    emit_progress(state, "execute_step_repair", step_id, tool=tool_name,
                  reason="derived_report_rebuild" if report_rebuild else "review_feedback")
    protected_inputs = {}
    if report_rebuild:
        protected_inputs = (previous.get("pending_repair") or {}).get("protected_inputs")
        if protected_inputs is None:
            protected_inputs = _report_preserved_assets(step, outputs)
    # Text producers interpret feedback themselves. Parameter-driven tools
    # need an executable parameter change, not just an attached issue string.
    updates: dict[str, Any] = {}
    source_repair = repair_field == "code"
    if not report_rebuild and (source_repair or ("parameters" in arguments and definition.content_contract)):
        client = state.hook_state.get("_data_planning_llm_client")
        if client is None:
            return {"status": "externally_blocked", "error": "Parameter repair model unavailable"}, False
        parameters = {"code": arguments["code"]} if source_repair else arguments["parameters"]
        parameters = json.loads(parameters) if isinstance(parameters, str) else dict(parameters or {})
        observed = (previous.get("repair_context") or {}).get("observed_parameters") or previous.get("grid_params") or previous.get("parameters") or {}
        observed = observed if isinstance(observed, dict) else {}
        allowed = (set(parameters) | set(observed) | set(definition.content_contract or {})) - {
            "requirement_analysis", "preprocessing_request", "preprocessing_work_order",
            "spec", "source_trace", "review_issues", "revision_contract",
        }
        from .preprocessing_capabilities import _call_producer_model

        response = await asyncio.wait_for(_call_producer_model(state, [
            LLMMessage(role="system", content=(
                "Repair this producer's parameters against its locked original request and review. "
                "Translate authoritative values to the controls actually used by the producer, "
                "using observed_parameters to identify defaults that ignored the request. "
                "Follow the producer_contract for accepted nested shapes and values, not just key names. "
                "Do not change scientific requirements, source identities, output contracts, tools, or unrelated parameters. "
                "Source context is implementation guidance, not new scientific authority: an alternate data "
                "release or stream is not an equivalent endpoint unless the locked request permits it. "
                "Return only JSON {\"parameter_updates\":{...}} using allowed_keys. "
                "Updates merge recursively: omitted keys are preserved. To remove a generated control "
                "group that contradicts the request, set that nested object to {} explicitly; do not "
                "merely omit it or add a new type beside incompatible old fields. Preserve unrelated "
                "groups and authoritative values. Observed/default controls and analyst_inference bindings "
                "are not user requirements; correct them against the locked request when necessary. "
                "If no supported parameter change can repair the defect, return an empty object."
                " For code repair, return parameter_updates.code with complete corrected Python. "
                "Use the observed execution environment, keep input_files/output_paths and scientific semantics, "
                "read inputs through input_files rather than guessed filenames. Reuse upstream verification "
                "receipts for report generation; do not replace computed facts with a guessed format parser. "
                "do not install packages or move network acquisition into Python. If changing the DAG or "
                "environment is necessary, return no update and explain the missing capability in reason. "
                "The executor will send a diagnosed capability gap to the route planner."
            )),
            LLMMessage(role="user", content=json.dumps({
                "request": caller_request_text(
                    plan.get("preprocessing_request") or {},
                    preprocessing_request=plan.get("preprocessing_request") or {},
                ),
                "parameters": {key: value for key, value in parameters.items() if key in allowed},
                "parameter_bindings": [{"asset_id": item.get("id"),
                                        "source": item.get("parameter_binding_source"),
                                        "values": canonical_parameter_bindings(item.get("parameter_bindings"))}
                                       for item in (parameters.get("requirement_analysis") or {}).get("required_files") or []],
                "producer_contract": definition.content_contract or definition.description,
                "observed_parameters": {key: value for key, value in observed.items() if key in allowed},
                "allowed_keys": sorted(allowed), "issues": feedback,
                "execution_evidence": previous.get("repair_context") or {},
                "input_files": arguments.get("input_files"),
                "input_evidence": _file_evidence({
                    path: evidence["content"]
                    for dependency in step.get("dependencies") or []
                    for path, evidence in _result_material_evidence(outputs.get(str(dependency)) or {}).items()
                }, total_limit=12000) if source_repair else {},
                "output_paths": arguments.get("output_paths"),
                "previous_attempts": history[-4:],
            }, ensure_ascii=False, default=str)),
        ], max_tokens=32768, temperature=0.1, max_retries=1),
            timeout=getattr(client, "timeout", 300))
        state.tokens_used += int((response.usage or {}).get("total_tokens") or 0)
        state.append_transcript(
            "producer_repair_response", step_id=step_id,
            finish_reason=response.finish_reason, content_chars=len(response.content or ""),
            completion_tokens=(response.usage or {}).get("completion_tokens"),
        )
        try:
            if response.finish_reason == "length" or not str(response.content or "").strip():
                raise ValueError("truncated or empty model response")
            proposal = _extract_object(response.content)
            updates = proposal.get("parameter_updates", {})
        except ValueError as exc:
            return {**previous, "status": "externally_blocked", "issues": feedback,
                    "failure_category": "model_response_truncated" if response.finish_reason == "length" else "model_response_invalid",
                    "error": f"Producer repair returned no complete JSON response; preserve assets and resume this repair, not plan redesign. {exc}"}, False
        if isinstance(updates, dict) and tool_name == "prepare_scientific_mesh":
            from .geometry_assets import canonical_mesh_controls, flatten_parameter_groups

            # Normalize the delta before merging: an alias must override the
            # previous canonical value, not be shadowed by it on the next call.
            updates = canonical_mesh_controls(flatten_parameter_groups(updates))
        if not isinstance(updates, dict) or not set(updates) <= allowed:
            return {**previous, "status": "needs_revision", "issues": feedback,
                    "failure_category": "repair_unsupported", "local_repair_exhausted": True,
                    "revision_contract": revision_contract(feedback, scope="plan", affected_ids=[step_id]),
                    "error": "Proposed repair exceeds this producer's controls; revise the affected producer. "
                             + str(proposal.get("reason") or "")[:1000]}, False
        if not updates:
            return {**(outputs.get(step_id) or {}), "status": "needs_revision", "issues": feedback,
                    "failure_category": "repair_unsupported",
                    "revision_contract": revision_contract(feedback, scope="asset", affected_ids=[step_id]),
                    "local_repair_exhausted": True,
                    "error": "Review requires a producer change beyond the supported parameter controls. "
                             + str(proposal.get("reason") or "")[:1000]}, False
        updated_parameters = merge_parameter_updates(parameters, updates)
        if canonical_hash(updated_parameters) == canonical_hash(parameters):
            return {**previous, "status": "needs_revision", "issues": feedback,
                    "local_repair_exhausted": True,
                    "error": "Parameter repair proposed no executable change; unchanged tool invocation skipped."}, False
        if source_repair:
            code = updated_parameters.get("code")
            if not isinstance(code, str) or not code.strip():
                return {**previous, "status": "needs_revision", "local_repair_exhausted": True}, False
            arguments["code"] = effective_arguments["code"] = code
        else:
            arguments["parameters"] = (
                json.dumps(updated_parameters, ensure_ascii=False)
                if isinstance(arguments["parameters"], str) else updated_parameters
            )
            # Persist only the parameter delta; never freeze resolved dependency paths.
            raw_parameters = effective_arguments.get("parameters") or {}
            raw_parameters = json.loads(raw_parameters) if isinstance(raw_parameters, str) else dict(raw_parameters)
            effective_arguments["parameters"] = (
                json.dumps(merge_parameter_updates(raw_parameters, updates), ensure_ascii=False)
                if isinstance(arguments["parameters"], str) else merge_parameter_updates(raw_parameters, updates)
            )
    repair_value: Any = arguments["code"] if source_repair else feedback
    if repair_field == "repair_feedback" and history:
        repair_value = [*feedback, {
            "repair_attempt": len(history) + 1,
            "previous_attempts": [{key: item[key] for key in ("issue_ids", "changed", "status", "repair_change", "before", "after") if key in item} for item in history[-3:]],
            "required_change": "The consumer still rejects this artifact. Do not revert to an earlier failed construction or only reformat it. Replace the invalid implementation using supported syntax while preserving the caller's requirements.",
        }]
    if repair_field == "revision_contract":
        repair_value = revision_contract(feedback, scope="asset", affected_ids=[step_id])
    elif repair_field == "review_issues":
        repair_value = json.dumps(feedback, ensure_ascii=False)
    pending = previous.get("pending_repair") or {}
    before = (pending["before"] if "before" in pending
              else _result_material_fingerprints(previous, issues=feedback))
    revised = await _invoke_planned_tool(
        state,
        tool_name,
        definition,
        {**arguments, repair_field: repair_value},
        plan_id=plan_id,
        plan_kind=plan_kind,
        step_id=step_id,
    )
    if report_rebuild:
        changed_inputs = [path for path, digest in protected_inputs.items()
                          if not Path(path).is_file() or hashlib.sha256(Path(path).read_bytes()).hexdigest() != digest]
        if changed_inputs:
            return {**revised, "status": "needs_revision", "failure_category": "framework_report_error",
                    "local_repair_exhausted": True, "issues": feedback,
                    "revision_contract": revision_contract(feedback, scope="asset", affected_ids=[step_id]),
                    "error": "Report rebuild changed preserved inputs or unrelated delivery files; publication is blocked.",
                    "changed_inputs": changed_inputs}, False
    after = _result_material_fingerprints(revised, issues=feedback)
    accepted = {"success"}
    if plan_kind == "reference_evidence_only":
        # Discovery metadata is progress, not a delivered asset. The reference
        # ledger still requires verified local bytes for material inputs.
        accepted.add("needs_download_selection")
        before, after = [fingerprints or {
            "reference": canonical_hash({key: value[key] for key in (
                "url", "download_links", "results", "metadata_excerpt", "page_excerpt",
            ) if value.get(key)})
        } for value, fingerprints in ((previous, before), (revised, after))]
    changed = revised.get("status") in accepted and bool(after) and after != before and all(
        after not in (attempt.get("before"), attempt.get("after")) for attempt in history
    )
    revised["effective_arguments"] = effective_arguments
    revised["repair_history"] = [*history[-3:], {
        "action": "rebuild_derived_reports" if report_rebuild else "repair_producer",
        "issue_ids": [revision_issue_identity(item) for item in feedback],
        "parameter_updates": {"code_hash": canonical_hash(updates)} if source_repair else updates,
        "arguments_hash": canonical_hash(arguments),
        "before": before, "after": after, "status": revised.get("status"),
        "changed": changed,
        **({"repair_change": revised["repair_change"]} if revised.get("repair_change") else {}),
    }]
    if revised.get("status") == "success" and not changed:
        revised.update(status="needs_revision", issues=feedback, local_repair_exhausted=True, error=(
            "The producer returned unchanged or previously rejected artifacts. Use the current diagnostic "
            "and rejected changes to choose a different valid implementation; reverting to an earlier "
            "failed version is not repair progress."
        ))
    return revised, changed


def _replace_step_result(
    results: list[dict[str, Any]],
    outputs: dict[str, Any],
    step_id: str,
    result: dict[str, Any],
) -> None:
    outputs[step_id] = result
    for record in results:
        if str(record.get("step_id") or "") == step_id:
            record.update({
                "status": result.get("status", "error"),
                "result": result,
                "completed_at": datetime.now(timezone.utc).isoformat(),
            })
            return


def _save_execution_checkpoint(
    state: State, plan: dict[str, Any], results: list[dict[str, Any]],
    outputs: dict[str, Any], repair_budget: dict[str, int],
) -> dict[str, Any]:
    """Commit step state atomically, retaining the review evidence for this plan."""
    path = state.root / "planning" / "execution_checkpoint.json"
    checkpoint = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    plan_hash = canonical_hash(plan)
    if checkpoint.get("plan_hash") != plan_hash:
        checkpoint = {}
    records = {str(item["step_id"]): item for item in results}
    # The currently executing step may not yet be appended to results.
    for step in plan.get("generation_steps") or []:
        step_id = str(step["id"])
        if step_id in outputs:
            records[step_id] = {**records.get(step_id, {}), "step_id": step_id,
                                "tool_name": step.get("tool_name"),
                                "status": outputs[step_id].get("status"), "result": outputs[step_id]}
    checkpoint.update(plan_hash=plan_hash, steps=list(records.values()),
                      outputs=outputs, repair_budget=repair_budget)
    if any(result.get("pending_repair") or result.get("status") == "blocked_dependency"
           for result in outputs.values()):
        checkpoint.pop("delivery_candidate", None)
        checkpoint["pending_delivery_rebuild"] = True
    write_object(path, json.loads(json.dumps(checkpoint, ensure_ascii=False, default=str)))
    return checkpoint


async def _repair_affected_steps(
    state: State,
    plan: dict[str, Any],
    steps: list[dict[str, Any]],
    results: list[dict[str, Any]],
    outputs: dict[str, Any],
    repairs: dict[str, list[dict[str, Any]]],
    *,
    plan_id: str,
    plan_kind: str,
    repair_budget: dict[str, int],
) -> str:
    """Repair owners and rebuild their transitive consumers in DAG order.

    Return the first unsuccessful step. Invalidate consumers before writing so
    a failed repair can never leave an old downstream success reusable.
    """
    # Executable input definitions belong to their existing text writer;
    # rebuild consumers instead of trying to express source edits as CLI flags.
    routed: dict[str, list[dict[str, Any]]] = {}
    for step_id, issues in repairs.items():
        step = next(item for item in steps if str(item["id"]) == step_id)
        script = _effective_step_arguments(step, outputs).get("script_asset")
        owners = [str(item["id"]) for item in steps if script and script in (item.get("outputs") or [])
                  and item["id"] in (step.get("dependencies") or [])
                  and item.get("tool_name") == "generate_preprocessing_artifact"]
        geometry = str((outputs.get(step_id) or {}).get("geometry_file") or "")
        if (not owners and step.get("tool_name") == "prepare_scientific_mesh"
                and Path(geometry).suffix.lower() == ".geo"
                and issues and all(issue.get("repair_kind") == "producer_controls" for issue in issues)):
            owners = [str(item["id"]) for item in steps
                      if item["id"] in (step.get("dependencies") or [])
                      and item.get("tool_name") == "generate_preprocessing_artifact"
                      and geometry in _step_artifact_paths(item, outputs.get(str(item["id"])) or {}).values()]
        owner = owners[0] if len(owners) == 1 else step_id
        routed.setdefault(owner, []).extend({**issue, "step_id": owner,
                                            "consumer_step_id": step_id} for issue in issues)
    repairs = routed

    def persist():
        return _save_execution_checkpoint(state, plan, results, outputs, repair_budget)

    affected = set(repairs)
    for step in steps:  # already topologically ordered by the executor
        if set(map(str, step.get("dependencies") or [])) & affected:
            affected.add(str(step["id"]))
    # Nested rebuilds must not discard another outstanding repair on a consumer.
    for step_id in affected - repairs.keys():
        pending = (outputs.get(step_id) or {}).get("pending_repair")
        if pending:
            repairs[step_id] = pending["issues"]
    for step_id in {
        str(step["id"]) for step in steps
        if str(step["id"]) not in repairs and set(map(str, step.get("dependencies") or [])) & affected
    }:
        previous = outputs.get(step_id) or {}
        _replace_step_result(results, outputs, step_id, {
            **previous, "status": "blocked_dependency",
            "error": previous.get("error") or "Upstream assets changed; this consumer must be regenerated.",
            **({"repair_failure_signature": _failure_signature(previous)}
               if previous.get("status") in {"error", "needs_revision"} else {}),
        })
    for step_id, feedback in repairs.items():
        previous = outputs.get(step_id) or {}
        pending = previous.get("pending_repair") or {
            "issues": feedback, "status": previous.get("status"),
            "before": _result_material_fingerprints(previous, issues=feedback),
        }
        step = next(item for item in steps if str(item.get("id")) == step_id)
        if (feedback and all(item.get("repair_kind") == "derived_report" for item in feedback)
                and _report_rebuild_covers(previous, _report_rebuild_contract(str(step.get("tool_name")), previous), feedback)
                and "protected_inputs" not in pending):
            # Persist before invoking the producer, so interruption cannot
            # silently accept modified inputs as the next attempt's baseline.
            pending["protected_inputs"] = _report_preserved_assets(step, outputs)
        _replace_step_result(results, outputs, step_id, {
            **previous, "status": "repair_pending", "pending_repair": pending,
        })
    persist()
    for step in steps:
        step_id = str(step["id"])
        if step_id not in affected:
            continue
        if step_id in repairs:
            feedback = repairs[step_id]
            seen_failures = {_failure_signature(outputs.get(step_id) or {})}
            while True:
                if repair_budget["remaining"] <= 0:
                    _replace_step_result(results, outputs, step_id, {
                        **(outputs.get(step_id) or {}), "status": "needs_revision",
                        "failure_category": "repair_exhausted", "local_repair_exhausted": True,
                        "issues": feedback,
                        "revision_contract": revision_contract(feedback, scope="asset", affected_ids=[step_id]),
                        "error": "Local repair budget exhausted without resolving the findings. "
                                 "Revise the affected production method using these diagnostics; preserve the requested outputs.",
                    })
                    outputs[step_id].pop("pending_repair", None)
                    persist()
                    return step_id
                repair_budget["remaining"] -= 1
                persist()  # Persist invalidation and charge before any model/tool side effect.
                try:
                    previous = outputs.get(step_id) or {}
                    previous = {**previous, "status": previous["pending_repair"].get("status", "needs_revision")}
                    revised, changed = await _repair_step_from_feedback(
                        state, plan, step, outputs, feedback,
                        plan_id=plan_id, plan_kind=plan_kind,
                    )
                except Exception as exc:
                    revised = {**(outputs.get(step_id) or {}), "status": "error",
                               "error": f"{type(exc).__name__}: {exc}", "issues": feedback}
                    revised["failure_category"] = _execution_failure_category(revised)
                    changed = False
                if (revised.get("local_repair_exhausted")
                        and _execution_failure_category(previous) == "transient_execution_error"):
                    # No supported alternate endpoint was found. Retain the
                    # real outage diagnostic, not an invented asset defect.
                    revised = {**previous, "local_repair_exhausted": True,
                               "repair_error": revised.get("error")}
                revised.pop("pending_repair", None)
                contract = _revision_contract_from_failure(step_id, str(step.get("tool_name") or ""), revised)
                # Compare the normalized diagnostic, not the addition of
                # ownership/category metadata on the next iteration.
                signature = _failure_signature({**revised, "issues": contract["issues"]} if contract else revised)
                if (changed or not contract or contract.get("scope") == "plan" or revised.get("local_repair_exhausted")
                        or signature in seen_failures):
                    break
                seen_failures.add(signature)
                # A corrected control can expose a different defect in an
                # upstream asset. Re-resolve ownership before asking this
                # producer to repair it with the wrong controls again.
                dependencies = {str(item) for item in step.get("dependencies") or []}
                upstream = [item for item in results if item.get("step_id") in dependencies]
                owners = {_review_issue_producer(issue, upstream) for issue in contract["issues"]}
                if owners and "" not in owners:
                    _replace_step_result(results, outputs, step_id, revised)
                    failed = await _repair_affected_steps(
                        state, plan, steps[:steps.index(step) + 1], results, outputs,
                        {owner: [issue for issue in contract["issues"]
                                 if _review_issue_producer(issue, upstream) == owner] for owner in owners},
                        plan_id=plan_id, plan_kind=plan_kind,
                        repair_budget=repair_budget,
                    )
                    if failed:
                        return failed
                    revised, changed = outputs[step_id], True
                    break
                feedback = _structured_failure_issues({"issues": contract["issues"]})
                _replace_step_result(results, outputs, step_id, {
                    **revised, "status": "repair_pending",
                    "pending_repair": {"issues": feedback, "status": revised.get("status"),
                                       "before": _result_material_fingerprints(revised, issues=feedback)},
                })
            if not changed:
                # Exhausted controls require an alternative producer, while
                # the locked delivery requirements remain unchanged.
                revised = revised or {**(outputs.get(step_id) or {}), "status": "needs_revision"}
                if revised.get("status") != "externally_blocked" and (
                    revised.get("status") == "success" or _revision_contract_from_failure(
                        step_id, str(step.get("tool_name") or ""), revised
                    )
                ):
                    revised = {
                        **revised, "status": "needs_revision",
                        "revision_contract": (contract if contract and contract.get("scope") == "plan" else
                                              revision_contract((contract or {}).get("issues") or feedback, scope="asset", affected_ids=[step_id])),
                        "local_repair_exhausted": True,
                    }
                _replace_step_result(results, outputs, step_id, revised)
                persist()
                return step_id
        else:
            arguments = _resolve_refs(_effective_step_arguments(step, outputs), outputs)
            arguments = _apply_plan_argument_defaults(plan, step, arguments, outputs)
            definition = get_tool(str(step.get("tool_name") or ""))
            if definition is None:
                revised = {"status": "error", "error": "Dependent producer is not registered."}
            else:
                previous = outputs.get(step_id) or {}
                emit_progress(state, "execute_step_rebuild", step_id, reason="upstream_asset_changed")
                revised = await _invoke_planned_tool(
                    state, str(step.get("tool_name") or ""), definition, arguments,
                    plan_id=plan_id, plan_kind=plan_kind, step_id=step_id,
                )
                for key in ("effective_arguments", "repair_history"):
                    if key in previous:
                        revised[key] = previous[key]
            contract = _revision_contract_from_failure(step_id, str(step.get("tool_name") or ""), revised)
            if contract:
                revised = {**revised, "status": "needs_revision", "revision_contract": contract}
                if previous.get("repair_failure_signature") == _failure_signature(revised):
                    revised.update(local_repair_exhausted=True,
                                   repair_error="The rebuilt consumer repeats the same failure after its producer changed.")
                if contract.get("scope") != "plan" and not revised.get("local_repair_exhausted"):
                    _replace_step_result(results, outputs, step_id, revised)
                    dependencies = {str(item) for item in step.get("dependencies") or []}
                    upstream = [item for item in results if item.get("step_id") in dependencies]
                    next_repairs: dict[str, list[dict[str, Any]]] = {}
                    for issue in contract["issues"]:
                        owner = _review_issue_producer(issue, upstream) or step_id
                        next_repairs.setdefault(owner, []).append(issue)
                    failed = await _repair_affected_steps(
                        state, plan, steps[:steps.index(step) + 1], results, outputs, next_repairs,
                        plan_id=plan_id, plan_kind=plan_kind, repair_budget=repair_budget,
                    )
                    if failed:
                        return failed
                    revised = outputs[step_id]
        _replace_step_result(results, outputs, step_id, revised)
        persist()
        if revised.get("status") != "success":
            return step_id
    return ""


def _ordered_steps(steps: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], str | None]:
    by_id = {str(step.get("id")): step for step in steps if isinstance(step, dict) and step.get("id")}
    pending = dict(by_id)
    completed: set[str] = set()
    ordered: list[dict[str, Any]] = []
    while pending:
        ready = [
            step_id for step_id, step in pending.items()
            if set(str(v) for v in step.get("dependencies") or []) <= completed
        ]
        if not ready:
            return [], "generation_steps contain a cycle or missing dependency"
        for step_id in sorted(ready):
            ordered.append(pending.pop(step_id))
            completed.add(step_id)
    return ordered, None


def _failed_step_dependencies(
    step: dict[str, Any], outputs: dict[str, Any]
) -> list[str]:
    """Return dependencies that did not produce a successful usable output."""
    failed: list[str] = []
    for dependency in step.get("dependencies") or []:
        dependency_id = str(dependency)
        result = outputs.get(dependency_id)
        if not isinstance(result, dict) or result.get("status") != "success":
            failed.append(dependency_id)
    return failed


def _scope_contract_error(plan: dict[str, Any], step: dict[str, Any]) -> str:
    plan_kind = str(plan.get("plan_kind") or "preprocessing_generation")
    if not tool_allowed_in_plan_kind(step.get("tool_name"), plan_kind):
        return (
            f"plan_contract_error: tool {step.get('tool_name') or '?'} "
            f"is not allowed in {plan_kind}"
        )
    scope = plan.get("task_scope") if isinstance(plan.get("task_scope"), dict) else {}
    allowed = {str(item).strip().lower() for item in scope.get("allowed_capabilities") or []}
    excluded = {str(item).strip().lower() for item in scope.get("excluded_capabilities") or []}
    capability = _step_workflow_capability(step)
    capability_error = _step_capability_contract_error(step)
    if not scope:
        return "plan_contract_error: task_scope is required before execution"
    if capability_error:
        return f"plan_contract_error: step {step.get('id') or '?'} {capability_error}"
    if capability == "unclassified":
        return f"plan_contract_error: step {step.get('id') or '?'} has no workflow_capability"
    if not is_pipeline_infrastructure_step(step) and (
        capability not in allowed or capability in excluded
    ):
        return (
            f"plan_contract_error: workflow_capability {capability!r} for step "
            f"{step.get('id') or '?'} is outside the approved task scope"
        )
    return ""


def _is_reference_evidence_only_plan(plan: dict[str, Any]) -> bool:
    if plan.get("plan_kind"):
        return plan["plan_kind"] == "reference_evidence_only"
    deliverables = [item for item in plan.get("required_deliverables") or [] if isinstance(item, dict)]
    if deliverables and all(str(item.get("type") or "") == "reference_evidence" for item in deliverables):
        return True
    steps = [item for item in plan.get("generation_steps") or [] if isinstance(item, dict)]
    return bool(steps) and all(
        tool_allowed_in_plan_kind(item.get("tool_name"), "reference_evidence_only")
        for item in steps
    )


def _clear_reference_plan_approval(state: State, plan_id: str, plan_hash: str) -> None:
    PlanningStore(state).clear_approval("reference_evidence_only")
    state.append_transcript(
        "preprocessing_reference_plan_approval_cleared",
        plan_id=plan_id,
        plan_hash=plan_hash,
        reason="reference_evidence_only_plan_completed",
    )


def _resolve_refs(value: Any, outputs: dict[str, Any]) -> Any:
    """Resolve structured {"$ref": "step_id.key.path"} arguments."""
    if isinstance(value, list):
        return [_resolve_refs(item, outputs) for item in value]
    if isinstance(value, dict):
        if set(value) == {"$ref"}:
            parts = str(value["$ref"]).split(".")
            current: Any = outputs.get(parts[0])
            if current is None:
                raise ValueError(f"Unknown step output reference: {value['$ref']}")
            for part in parts[1:]:
                if not isinstance(current, dict) or part not in current:
                    raise ValueError(f"Unknown step output reference: {value['$ref']}")
                current = current[part]
            return current
        return {key: _resolve_refs(item, outputs) for key, item in value.items()}
    return value


async def execute_preprocessing_plan(
    state: State,
    stop_on_error: bool = True,
    plan_id: str = "",
    plan_kind: str = "preprocessing_generation",
    resume_target: str = "",
    **_: Any,
) -> dict[str, Any]:
    store = PlanningStore(state)
    requested_kind = str(plan_kind or "preprocessing_generation")
    # 判决拆除三波（epp:1406 → schema，2026-09-02）：plan_kind 的合法值只在注册
    # schema 的 enum 里声明一次，派发口核一次；data_agent_loop 直接调用时只传字面量。
    # 判决拆除 O9（epp:1356/1365 随根 store:468 降格，2026-08-31）：
    # Designer/Critic 评审照跑照记，但不再是通行许可 —— 未评审/非当前批准的
    # plan 照执行，本次执行与其产物打 plan_approval_status:unapproved。
    plan_approval_status = "approved"
    if plan_id:
        record = store.get_plan(str(plan_id))
        if not record:
            return {"status": "error", "error": f"Unknown plan_id {plan_id!r}."}
        actual_kind = str((record.get("plan") or {}).get("plan_kind") or "preprocessing_generation")
        if actual_kind != requested_kind:
            return {
                "status": "error",
                "error": f"plan_id {plan_id!r} is {actual_kind}, not requested {requested_kind}.",
            }
        status = store.approval_status(actual_kind)
        if not status.get("approved") or str(status.get("approved_plan_id") or "") != str(plan_id):
            plan_approval_status = "unapproved"
            state.append_transcript(
                "preprocessing_unreviewed_execution",
                operation="execute_preprocessing_plan",
                plan_id=str(plan_id),
                reason=f"plan {plan_id!r} is not the current approved {actual_kind} plan",
                planning_status=status,
            )
        plan = record.get("plan")
    else:
        status = store.approval_status(requested_kind)
        if status.get("approved"):
            record = store.get_plan(status.get("approved_plan_id"))
        else:
            record = store.latest_plan(requested_kind)
            plan_approval_status = "unapproved"
            state.append_transcript(
                "preprocessing_unreviewed_execution",
                operation="execute_preprocessing_plan",
                plan_id=str((record or {}).get("plan_id") or ""),
                reason=f"no current approved {requested_kind} plan; executing latest plan",
                planning_status=status,
            )
        plan = record.get("plan") if record else None
    if not isinstance(plan, dict):
        # 保留（epp:1372，C 类）：plan 记录读不出/不存在 = 现实自己拒绝。
        return {"status": "error", "error": "No readable preprocessing plan record exists to execute."}
    # Every execution path (including writer recovery) uses the saved plan's
    # naming context. Fresh hook_state must not change the delivery destination.
    state.hook_state["_data_latest_requirement_analysis"] = plan.get("requirement_analysis") or {}
    dependency_issues = mesh_dependency_issues(plan)
    if dependency_issues:
        # A missing producer is a DAG defect, not an editable consumer parameter.
        # This also protects saved plans created before dependency validation.
        return with_pipeline_outcome({
            "status": "needs_revision", "plan_id": str(record.get("plan_id") or plan_id or ""),
            "failure_category": "plan_contract_error", "n_executed": 0,
            "error": dependency_issues[0]["message"],
            "revision_contract": revision_contract(dependency_issues, scope="plan"),
        })
    state.hook_state["_plan_approval_status"] = plan_approval_status
    steps, error = _ordered_steps(plan.get("generation_steps") or [])
    if error:
        return {"status": "error", "error": error}
    emit_progress(
        state,
        "execute_plan",
        "starting approved preprocessing plan",
        steps=len(steps),
    )

    latest = store.latest_plan(
        "reference_evidence_only" if _is_reference_evidence_only_plan(plan) else "preprocessing_generation"
    ) or {}
    plan_hash = canonical_hash(plan)
    checkpoint_path = state.root / "planning" / "execution_checkpoint.json"
    stage_resume = {
        "kind": "pipeline_stage",
        "resume_run_id": state.run_id,
        "plan_id": str(plan_id or latest.get("plan_id") or ""),
        "plan_hash": plan_hash,
        "execution_checkpoint": str(checkpoint_path),
        "preserve_request_spec_hash": str((plan.get("preprocessing_request") or {}).get("request_spec_hash") or ""),
    }

    def stage_resume_blocked(message: str) -> dict[str, Any]:
        return with_pipeline_outcome({
            "status": "externally_blocked", "failure_category": "stage_resume_unavailable",
            "error": message, "plan_id": stage_resume["plan_id"],
            "retry_target": resume_target, "execution_checkpoint": str(checkpoint_path),
            "resume_contract": {**stage_resume, "retry_target": resume_target},
        })

    checkpoint: dict[str, Any] = {}
    if checkpoint_path.exists():
        try:
            checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            checkpoint = {}
    if resume_target and (
        resume_target not in {"authority_review", "publication", "artifact_writer"}
        or plan_approval_status != "approved" or checkpoint.get("plan_hash") != plan_hash
    ):
        return stage_resume_blocked(
            "The requested pipeline stage requires its unchanged approved plan and matching execution checkpoint. "
            "Restore that checkpoint or request a separate generation attempt; no assets were regenerated."
        )
    if checkpoint.get("plan_hash") != plan_hash:
        prior_checkpoint = checkpoint
        checkpoint = {"plan_hash": plan_hash, "steps": [], "outputs": {}}
        revision = store.load_generation_revision()
        if (
            str(prior_checkpoint.get("plan_hash") or "")
            == str(revision.get("base_plan_hash") or "")
        ):
            base_record = store.get_plan(str(revision.get("base_plan_id") or "")) or {}
            base_plan = base_record.get("plan") if isinstance(base_record.get("plan"), dict) else {}
            base_steps = {
                str(item.get("id") or ""): item
                for item in base_plan.get("generation_steps") or []
                if isinstance(item, dict) and str(item.get("id") or "")
            }
            current_steps = {
                str(item.get("id") or ""): item
                for item in steps if isinstance(item, dict) and str(item.get("id") or "")
            }

            # A targeted package review identifies the owner deliverable, not
            # necessarily the generation step.  Resolve both namespaces
            # before rebasing the checkpoint; otherwise a revised plan can
            # preserve every old success and immediately reproduce the same
            # review result.
            revision_feedback = revision.get("feedback") if isinstance(revision, dict) else {}
            if isinstance(revision_feedback, dict):
                revision_feedback = revision_feedback.get("feedback") or revision_feedback.get("issues") or []
            affected_step_ids: set[str] = set()
            unmapped_feedback: list[dict[str, Any]] = []
            for item in revision_feedback if isinstance(revision_feedback, list) else []:
                if not isinstance(item, dict):
                    continue
                direct = str(item.get("step_id") or "").strip()
                deliverable_id = str(item.get("deliverable_id") or "").strip()
                if direct:
                    affected_step_ids.add(direct)
                if deliverable_id:
                    affected_step_ids.update(
                        str(step.get("id") or "")
                        for step in current_steps.values()
                        if deliverable_id in {str(value) for value in step.get("outputs") or []}
                    )
                if item.get("repairable") is not False and not direct and not deliverable_id:
                    unmapped_feedback.append(item)
            if unmapped_feedback:
                # Missing ownership cannot justify reusing old successes, but
                # it does not prove the revised work order cannot succeed.
                affected_step_ids.update(current_steps)
            def reusable(record: dict[str, Any]) -> bool:
                step_id = str(record.get("step_id") or "")
                if (
                    record.get("status") != "success"
                    or step_id not in base_steps
                    or step_id not in current_steps
                    or canonical_hash(base_steps[step_id]) != canonical_hash(current_steps[step_id])
                    or step_id in affected_step_ids
                ):
                    return False
                result = record.get("result") or {}
                material_paths = _result_material_paths(result, include_missing=True)
                return bool(material_paths) and all(
                    path.exists()
                    for path in material_paths
                )

            prior_records = {
                str(item.get("step_id") or ""): item
                for item in prior_checkpoint.get("steps") or [] if isinstance(item, dict)
            }
            preserved_ids: set[str] = set()
            # Reuse in DAG order: unchanged consumers are still stale when
            # an input changed, disappeared, or was rejected by the reviewer.
            for step in steps:
                step_id = str(step["id"])
                record = prior_records.get(step_id) or {}
                if reusable(record) and set(map(str, step.get("dependencies") or [])) <= preserved_ids:
                    checkpoint["steps"].append(record)
                    preserved_ids.add(step_id)
            checkpoint["outputs"] = {
                key: value for key, value in (prior_checkpoint.get("outputs") or {}).items()
                if str(key) in preserved_ids
            }
            if preserved_ids:
                emit_progress(
                    state,
                    "execute_checkpoint_rebased",
                    "reusing unchanged successful steps after targeted plan revision",
                    preserved_steps=len(preserved_ids),
                )
        # A rebased checkpoint must describe the new plan even when every
        # step is skipped.  Leaving the old hash on disk causes each retry to
        # rebase the same plan and re-enter the planning loop indefinitely.
        write_object(checkpoint_path, json.loads(json.dumps(checkpoint, default=str)))
    for record in checkpoint.get("steps") or []:
        result = _restore_tool_result(state, str(record.get("tool_name") or ""), record.get("result") or {})
        record.update(result=result, status=result.get("status"))
        checkpoint.setdefault("outputs", {})[str(record.get("step_id") or "")] = result
    if resume_target == "artifact_writer":
        interrupted = [record for record in checkpoint.get("steps") or []
                       if record.get("status") == "externally_blocked"
                       and record.get("tool_name") == "generate_preprocessing_artifact"
                       and (record.get("result") or {}).get("failure_category") in RESUMABLE_WRITER_FAILURES]
        if len(interrupted) != 1:
            return stage_resume_blocked("Writer recovery requires exactly one saved model/reference-service interruption.")
        record = interrupted[0]
        record["status"] = record["result"]["status"] = "retry_pending"
        emit_progress(state, "execute_stage_resume", "artifact_writer", step_id=record["step_id"])
        resume_target = ""
    # Failed/cancelled runs discard their private downloads. A saved success
    # is not reusable once its materialized input (or a producer output) is gone.
    invalidated = {
        str(record.get("step_id")) for record in checkpoint.get("steps") or []
        if any(not path.exists() for path in _result_material_paths(
            record.get("result") or {}, include_missing=True,
        ))
    }
    for step in steps:
        if set(map(str, step.get("dependencies") or [])) & invalidated:
            invalidated.add(str(step["id"]))
    recovering_repair = bool(checkpoint.get("pending_delivery_rebuild")) or any(
        item.get("pending_repair") for item in (checkpoint.get("outputs") or {}).values()
    )
    if recovering_repair:
        # A review-only resume cannot reuse a candidate whose producers were
        # interrupted during repair. Finish the saved local work first.
        resume_target = ""
        emit_progress(state, "execute_repair_resume", "continuing saved repair and dependent rebuilds")
    resumed_candidate = None
    if resume_target:
        completed = {str(record.get("step_id")) for record in checkpoint.get("steps") or []
                     if record.get("status") == "success"}
        resumed_candidate = checkpoint.get("delivery_candidate") or next((
            (record.get("result") or {}).get("delivery_candidate")
            for record in reversed(checkpoint.get("steps") or [])
            if (record.get("result") or {}).get("delivery_candidate")
        ), None)
        staging = Path(str((resumed_candidate or {}).get("staging_dir") or ""))
        files = (resumed_candidate or {}).get("delivery_files") or []
        if (completed != {str(step["id"]) for step in steps} or not files
                or any(not (staging / str(item.get("path") or "")).is_file() for item in files)):
            return stage_resume_blocked(
                "The saved pipeline stage cannot resume because its successful execution receipt or staged delivery "
                "files are unavailable. Restore that candidate; no generation was started."
            )
        # Only this stage's candidate is needed to resume review. Missing
        # historical downloads must not trigger regeneration of completed work;
        # the reviewer can still report missing source evidence if it needs it.
        invalidated.clear()
        emit_progress(state, "execute_stage_resume", resume_target,
                      plan_id=stage_resume["plan_id"], preserved_steps=len(completed))
    if invalidated:
        for record in checkpoint.get("steps") or []:
            result = record.get("result") or {}
            if str(record.get("step_id")) in invalidated and not result.get("pending_repair"):
                result = {**result, "status": "blocked_dependency"}
                record.update(status=result["status"], result=result)
                checkpoint["outputs"][str(record["step_id"])] = result
        checkpoint.pop("delivery_candidate", None)
        checkpoint["pending_delivery_rebuild"] = True
        write_object(checkpoint_path, json.loads(json.dumps(checkpoint, default=str)))
    for record in checkpoint.get("steps") or []:
        result = record.get("result") or {}
        if result.get("status") == "externally_blocked":
            return with_pipeline_outcome({**result, "plan_id": latest.get("plan_id"),
                                          "failed_step": record.get("step_id")})
    prior_failures = [
        item for item in (checkpoint.get("steps") or [])
        if isinstance(item, dict) and item.get("status") in {"review_failed", "error"}
    ]
    if prior_failures:
        latest_failure = prior_failures[-1]
        result = latest_failure.get("result") or {}
        if latest_failure.get("status") == "error":
            contract = result.get("revision_contract") or _revision_contract_from_failure(
                str(latest_failure.get("step_id") or ""),
                str(latest_failure.get("tool_name") or ""),
                result,
            )
            # 保留·升 A（epp:1586，2026-08-31，措辞照 te:402 范本）：确定性重放
            # 抑制 = 算力熔断，不是可行性判定。
            return with_pipeline_outcome({
                "status": "needs_revision" if contract else "error",
                "error": (
                    "⛔ 算力熔断：同一份 plan 的同一步已确定性失败过，逐字节相同的重放只会再烧一轮。"
                    "两个出口（任选其一即可放行）：① 按 revision_contract 修订该步后重新执行；"
                    "② request_human_input 说明卡点请人裁决。"
                    "注意：这是执行方式的限制，不是「此事不可行」的判定。"
                ),
                "failure_category": _execution_failure_category(result),
                "plan_id": latest.get("plan_id"),
                "plan_hash": plan_hash,
                "failed_step": latest_failure.get("step_id"),
                "revision_contract": contract,
                "execution_checkpoint": str(checkpoint_path),
            })
        contract = result.get("revision_contract")
        emit_progress(
            state,
            "execute_plan_skip",
            "unchanged plan already failed review",
            step_id=latest_failure.get("step_id"),
        )
        # 保留·升 A（epp:1609，2026-08-31，措辞照 te:402 范本）：零变化重放抑制
        # = 算力熔断；出口随 O9 根降格后真实可走（评审不再是通行许可）。
        return with_pipeline_outcome({
            "status": "needs_revision",
            "error": (
                "⛔ 算力熔断：这份未变的 plan 已经败过同一场 review，原样重跑只会复现同一结果。"
                "两个出口（任选其一即可放行）：① 按 revision_contract 修订 plan 后重新执行"
                "（评审照跑照记，但已不是通行许可）；② request_human_input 请人裁决。"
                "注意：这是执行方式的限制，不是「此事不可行」的判定。"
            ),
            "plan_id": latest.get("plan_id"),
            "plan_hash": plan_hash,
            "failed_step": latest_failure.get("step_id"),
            "revision_contract": contract,
            "execution_checkpoint": str(checkpoint_path),
        })
    repair_budget = dict(checkpoint.get("repair_budget") or {"remaining": max(8, 3 * len(steps))})
    results = [
        item for item in (checkpoint.get("steps") or [])
        if isinstance(item, dict) and item.get("status") == "success"
    ]
    outputs = dict(checkpoint.get("outputs") or {})
    reference_plan = _is_reference_evidence_only_plan(plan)
    reference_evidence: list[dict[str, Any]] = []
    completed_ids = {
        str(item.get("step_id")) for item in results
        if isinstance(item, dict) and item.get("status") == "success"
    }
    for step in steps:
        step_id = str(step.get("id"))
        if step_id in completed_ids:
            emit_progress(state, "execute_step_skip", step_id, reason="checkpoint_success")
            continue
        tool_name = str(step.get("tool_name") or "").strip()
        definition: ToolDefinition | None = None
        emit_progress(state, "execute_step", step_id, tool=tool_name)
        result: dict[str, Any] | None = None
        failed_dependencies = _failed_step_dependencies(step, outputs)
        if failed_dependencies:
            result = {
                "status": "blocked_dependency",
                "error": (
                    "Approved-plan dependencies did not complete successfully: "
                    + ", ".join(failed_dependencies)
                ),
                "failed_dependencies": failed_dependencies,
            }
            tool_name = ""
        try:
            arguments = (
                {}
                if result is not None
                else _resolve_refs(_effective_step_arguments(step, outputs), outputs)
            )
            if result is None:
                arguments = _apply_plan_argument_defaults(plan, step, arguments, outputs)
        except ValueError as exc:
            arguments = {}
            result = {"status": "needs_revision", "failure_category": "plan_contract_error",
                      "error": str(exc), "revision_contract": revision_contract([{
                          "code": "invalid_step_arguments", "step_id": step_id, "tool_name": tool_name,
                          "message": str(exc), "required_change": "Correct this step's input references or argument contract; preserve successful producers.",
                      }], scope="plan")}
        step_plan_deviations: list[dict[str, Any]] = []
        if result is None:
            scope_error = _scope_contract_error(plan, {**step, "tool_arguments": arguments})
            if scope_error:
                # 判决拆除 O8（epp:1663 降格，2026-08-31）：plan 是承诺不是牢笼
                # —— 超 scope 照跑，偏离在唯一登记点（executor dispatch）记账。
                step_plan_deviations.append({
                    "kind": "scope_contract", "step_id": step_id, "detail": scope_error})
                state.append_transcript(
                    "plan_deviation", kind="scope_contract",
                    step_id=step_id, tool_name=tool_name, detail=scope_error)
        if result is None and (not tool_name or tool_name in _FORBIDDEN):
            result = {"status": "error", "error": f"Invalid execution tool {tool_name!r}"}
        elif result is None:
            if not plan_authorizes_tool(plan, tool_name):
                # 判决拆除 O8（epp:1668 降格，2026-08-31）：工具未被 plan 点名
                # 只说明「某文档没点过名」——照跑，偏离记账（假墙：拿掉它，
                # 下方节点能力边界/风险闸一条不动）。
                step_plan_deviations.append({
                    "kind": "tool_not_in_plan", "step_id": step_id, "tool_name": tool_name})
                state.append_transcript(
                    "plan_deviation", kind="tool_not_in_plan",
                    step_id=step_id, tool_name=tool_name)
            definition = get_tool(tool_name)
            if definition is None:
                result = {"status": "error", "error": f"Tool {tool_name!r} is not registered."}
            elif definition.allowed_node_types is not None and "data" not in definition.allowed_node_types:
                # 保留·升 B（epp:1674，2026-08-31）：allowed_node_types 是节点
                # 能力边界（写不跨节点），不是审批 —— 真墙，保留。
                result = {"status": "error", "error": f"Tool {tool_name!r} is not allowed for data."}
            else:
                pending = (outputs.get(step_id) or {}).get("pending_repair")
                if pending:
                    failed = await _repair_affected_steps(
                        state, plan, steps[:steps.index(step) + 1], results, outputs,
                        {step_id: pending["issues"]}, plan_id=str(latest.get("plan_id") or ""),
                        plan_kind=requested_kind, repair_budget=repair_budget,
                    )
                    result = outputs[failed or step_id]
                else:
                    result = _reuse_reviewed_mesh_conversion(step, arguments, outputs)
                if result is None:
                    result = await _invoke_planned_tool(
                        state,
                        tool_name,
                        definition,
                        arguments,
                        plan_id=str(latest.get("plan_id") or ""),
                        plan_kind=requested_kind,
                        step_id=step_id,
                    )
        targeted_revision = (None if reference_plan and result.get("status") == "needs_download_selection"
                             else _revision_contract_from_failure(step_id, tool_name, result))
        # Route diagnosed input defects to their writer; otherwise repair the
        # failing step itself. Use the same DAG repair as publication review.
        seen_consumer_failures: set[str] = set()
        local_dependencies = [
            candidate
            for dependency in step.get("dependencies") or []
            for candidate in steps
            if str(candidate.get("id") or "") == str(dependency)
            and _tool_repair_field(get_tool(str(candidate.get("tool_name") or "")))
        ]
        while (targeted_revision and targeted_revision.get("scope") != "plan"
               and not result.get("local_repair_exhausted")):
            failure_signature = _failure_signature(result)
            if not failure_signature or failure_signature in seen_consumer_failures:
                result = {**result, "local_repair_exhausted": True}
                targeted_revision = revision_contract(targeted_revision["issues"], scope="plan",
                                                      affected_ids=targeted_revision.get("affected_ids"))
                break
            seen_consumer_failures.add(failure_signature)
            dependency_records = [item for item in results if item.get("step_id") in {
                str(candidate.get("id") or "") for candidate in local_dependencies
            }]
            repairs: dict[str, list[dict[str, Any]]] = {}
            for issue in targeted_revision.get("issues") or []:
                owner = _review_issue_producer(issue, dependency_records)
                if not owner:
                    declared = str(issue.get("step_id") or "")
                    owner = declared if declared in {str(item["id"]) for item in local_dependencies} else step_id
                owner_step = next(item for item in steps if str(item["id"]) == owner)
                repairs.setdefault(owner, []).append({
                    **issue, "step_id": owner, "tool_name": owner_step.get("tool_name"), "consumer_step_id": step_id,
                })
            if not repairs or definition is None:
                break
            outputs[step_id] = result
            failed_repair = await _repair_affected_steps(
                state, plan, steps[:steps.index(step) + 1], results, outputs, repairs,
                plan_id=str(latest.get("plan_id") or ""),
                plan_kind=requested_kind,
                repair_budget=repair_budget,
            )
            if failed_repair and failed_repair != step_id:
                failed_result = outputs[failed_repair]
                original_error = result.get("error")
                result = {
                    **(outputs.get(step_id) or {}),
                    **{key: value for key, value in failed_result.items() if key in {
                        "status", "error", "failure_category", "revision_contract", "local_repair_exhausted",
                        "resume_contract", "missing_fields", "question", "reference_request",
                        "required_dependencies", "repair_context",
                    }},
                }
                if original_error:
                    result.update(error=original_error, repair_error=failed_result.get("error"))
            else:
                result = outputs.get(step_id) or result
            targeted_revision = (None if reference_plan and result.get("status") == "needs_download_selection"
                                 else _revision_contract_from_failure(step_id, tool_name, result))
            if result.get("local_repair_exhausted"):
                targeted_revision = _revision_contract_from_failure(step_id, tool_name, result)
                break
        if targeted_revision is not None:
            # An asset failure does not invalidate an otherwise executable DAG.
            result = {
                **result,
                "status": "needs_revision",
                "failure_category": ("plan_contract_error" if targeted_revision.get("scope") == "plan"
                                     else result.get("failure_category") or "deterministic_execution_error"),
                "revision_contract": targeted_revision,
            }
        record = {
            "step_id": step_id,
            "tool_name": tool_name,
            "asset_role": str(arguments.get("asset_role") or "").strip().lower(),
            "status": result.get("status", "error"),
            "result": result,
            "completed_at": datetime.now(timezone.utc).isoformat(),
            **({"plan_deviations": step_plan_deviations} if step_plan_deviations else {}),
        }
        # Reference failures are evidence for the planner to classify; they
        # do not prove that any source bytes were acquired.
        _remember_downloaded_geometry_asset(state, result)
        remember_downloaded_scientific_asset(state, result)
        results.append(record)
        outputs[step_id] = result
        if reference_plan and tool_name in {"data_web_search", "data_web_download"}:
            from .preprocessing_planner import record_reference_execution_evidence

            reference_evidence = record_reference_execution_evidence(state, plan, [record])
        documentary_reference_error = bool(
            reference_plan
            and tool_name in {"data_web_search", "data_web_download"}
            and record["status"] == "error"
            and record.get("asset_role") == "external_dataset_reference"
        )
        if record["status"] == "review_failed":
            contract = result.get("revision_contract")
            review_issues = list((contract or {}).get("issues") or [])
            state.append_transcript(
                "preprocessing_review_failed",
                step_id=step_id,
                tool_name=tool_name,
                feedback=review_issues,
                review_report=result.get("review_report"),
            )
            repairable_feedback = [
                item for item in review_issues
                if isinstance(item, dict) and item.get("repairable") is not False
            ]
            if review_issues and not repairable_feedback:
                # Reviewer availability/schema failures cannot be corrected by
                # changing scientific assets. The bounded review retry has
                # already run, so stop without sending an unchanged product
                # through Designer again.
                record["status"] = "externally_blocked"
                result["status"] = "externally_blocked"
                result["failure_category"] = "review_protocol_error"
            else:
                # A substantive, repairable mismatch remains a bounded
                # generation-revision transition driven by Reviewer evidence.
                record["status"] = "needs_revision"
                result["failure_category"] = "generated_asset_review_failed"
        elif record["status"] in _PIPELINE_TRANSITION_STATUSES | _PLAN_CONTRACT_FAILURE_STATUSES | {"error", "blocked"} and not documentary_reference_error:
            if record["status"] in _PIPELINE_TRANSITION_STATUSES:
                # A transition consumes the current generation approval.  It
                # must be converted by the pipeline driver into a reference
                # plan; replaying this generation plan would repeat the same
                # failing package step indefinitely.
                result["failure_category"] = _execution_failure_category(result)
            elif record["status"] in _PLAN_CONTRACT_FAILURE_STATUSES:
                result["failure_category"] = "plan_contract_error"
            else:
                result["failure_category"] = _execution_failure_category(result)
            # Only a structural revision consumes approval. Content defects,
            # report bugs and service outages do not change the approved DAG.
            if pipeline_outcome(result)["kind"] == "revise_plan":
                store.invalidate_plan(
                    requested_kind,
                    str(latest.get("plan_id") or ""),
                    reason=result.get("failure_category") or "execution_failed",
                )
        checkpoint = _save_execution_checkpoint(state, plan, results, outputs, repair_budget)
        state.append_transcript(
            "preprocessing_plan_step",
            step_id=step_id,
            tool_name=tool_name,
            status=record["status"],
        )
        emit_progress(
            state,
            "execute_step_done",
            step_id,
            tool=tool_name,
            status=record["status"],
        )
        accepted_step_statuses = (
            {"success", "deferred_dependency", "needs_download_selection"} if reference_plan else {"success"}
        )
        # Let the reference ledger distinguish an endpoint needing correction
        # from a transport outage. Neither is a completed dataset delivery.
        if (
            reference_plan
            and tool_name in {"data_web_search", "data_web_download"}
            and str(arguments.get("asset_role") or "").strip().lower() == "external_dataset_reference"
        ):
            accepted_step_statuses = accepted_step_statuses | {"error"}
        if stop_on_error and record["status"] not in accepted_step_statuses:
            break

    def _reference_step_accepted(item: dict[str, Any]) -> bool:
        accepted = {"success", "deferred_dependency", "needs_download_selection"}
        return item.get("status") in accepted or (
            reference_plan
            and item.get("status") == "error"
            and str(item.get("tool_name") or "") in {"data_web_search", "data_web_download"}
            and str(item.get("asset_role") or "").strip().lower()
            == "external_dataset_reference"
        )

    successful = len(results) == len(steps) and all(
        _reference_step_accepted(item) if reference_plan else item.get("status") == "success"
        for item in results
    )
    publication = None
    if successful and not reference_plan:
        try:
            review_feedback = (revision_contract(
                store.load_generation_revision().get("feedback")
            ) or {}).get("issues", [])
            review_plan = {**plan, "plan_id": latest.get("plan_id"), "plan_hash": plan_hash,
                           "review_feedback": review_feedback}
            if resumed_candidate is not None:
                publication = await _review_and_publish_candidate(state, review_plan, resumed_candidate, results)
            else:
                publication = await _publish_plan_delivery(state, review_plan, results)
            if isinstance(publication, dict) and publication.get("outcome") == "retry_step":
                successful = False
            observed_review_states: set[str] = set()
            best_review_progress: dict[str, Any] | None = None
            while isinstance(publication, dict) and publication.get("status") == "review_failed":
                contract = publication.get("revision_contract")
                review_issues = [
                    item for item in (contract or {}).get("issues") or []
                    if isinstance(item, dict)
                ]
                current_progress = publication.get("review_progress") or {}
                progress_signature = str(
                    current_progress.get("signature")
                    or (contract or {}).get("signature")
                    or ""
                )
                if not review_issues or not progress_signature:
                    publication.update(status="externally_blocked", outcome="externally_blocked",
                                       failure_category="review_protocol_error", retry_target="authority_review",
                                       error="Review failed without actionable findings; preserve assets and repair the review protocol.")
                    break
                steps_by_id = {
                    str(item.get("id") or ""): item
                    for item in steps if isinstance(item, dict) and str(item.get("id") or "")
                }
                step_by_output: dict[str, str] = {}
                for candidate in steps:
                    if not isinstance(candidate, dict):
                        continue
                    candidate_id = str(candidate.get("id") or "").strip()
                    if not candidate_id:
                        continue
                    for output_id in candidate.get("outputs") or []:
                        key = str(output_id or "").strip()
                        if key:
                            step_by_output.setdefault(key, candidate_id)
                deliverable_by_file = {
                    PurePosixPath(str(value)).name: str(item.get("id") or "")
                    for item in plan.get("required_deliverables") or []
                    if isinstance(item, dict) and str(item.get("id") or "")
                    for value in (
                        item.get("expected_filename"),
                        item.get("filename"),
                        item.get("path"),
                        item.get("declared_output_path"),
                    )
                    if str(value or "").strip()
                }
                required_ids = {str(item.get("id")) for item in plan.get("required_deliverables") or []}
                for item in review_issues:
                    asset_id = str(item.get("deliverable_id") or deliverable_by_file.get(
                        PurePosixPath(str(item.get("file") or "")).name, ""
                    ))
                    if asset_id:
                        item["deliverable_id"] = asset_id
                    output_id = asset_id.removeprefix("caller:")
                    owner = step_by_output.get(output_id) or step_by_output.get(output_id.lower())
                    if item.get("repair_kind") == "plan_structure" or (not owner and (
                        asset_id in required_ids or (asset_id.startswith("caller:")
                        and item.get("code") == "required_asset_unfulfilled")
                    )):
                        item["repair_kind"] = "plan_structure"
                        item.pop("step_id", None)
                        item.pop("source_file", None)
                        continue
                    producer = _causal_review_producer(item, plan, results, owner or "")
                    item.pop("step_id", None)
                    if producer:
                        item["step_id"] = producer
                    elif not item.get("step_id"):
                        if owner:
                            item["step_id"] = owner
                contract = revision_contract(review_issues)
                publication["revision_contract"] = contract
                if contract["scope"] == "plan":
                    publication["message"] = "A required delivery has no production step; revise the plan before repairing files."
                    break
                unowned = [item for item in review_issues
                           if (item.get("file") or item.get("deliverable_id")) and not item.get("step_id")]
                if unowned:
                    publication["revision_contract"] = revision_contract(review_issues, scope="plan")
                    publication["failure_category"] = "repair_unsupported"
                    publication["message"] = "The reviewed file has no identifiable repair owner; preserve its evidence and restore ownership metadata."
                    break
                if progress_signature in observed_review_states or not review_progress_improved(current_progress, best_review_progress):
                    publication["message"] = "Local repair did not resolve findings; revise the affected generation method."
                    publication["revision_contract"] = revision_contract(review_issues, scope="plan")
                    break
                best_review_progress = current_progress
                observed_review_states.add(progress_signature)
                cited_step_ids = list(dict.fromkeys(
                    str(item.get("step_id") or "")
                    for item in review_issues
                    if item.get("repairable") is not False and str(item.get("step_id") or "")
                ))
                repair_calls: dict[str, list[dict[str, Any]]] = {}
                for step_id in cited_step_ids:
                    step = steps_by_id.get(step_id)
                    definition = get_tool(str((step or {}).get("tool_name") or ""))
                    repair_field = _tool_repair_field(definition)
                    if not step or definition is None or not repair_field:
                        continue
                    step_feedback = [
                        item for item in review_issues
                        if str(item.get("step_id") or "") == step_id
                        and item.get("repairable") is not False
                    ]
                    if step_feedback:
                        repair_calls[step_id] = step_feedback
                if not repair_calls:
                    publication["failure_category"] = "repair_unsupported"
                    publication["message"] = "The cited producer has no supported repair action; replace that producer while preserving the requested outputs."
                    break
                failed_repair = await _repair_affected_steps(
                    state, plan, steps, results, outputs, repair_calls,
                    plan_id=str(latest.get("plan_id") or ""), plan_kind=requested_kind,
                    repair_budget=repair_budget,
                )
                checkpoint = _save_execution_checkpoint(state, plan, results, outputs, repair_budget)
                if failed_repair:
                    failed_result = outputs[failed_repair]
                    publication["revision_contract"] = pipeline_outcome(failed_result).get("revision_contract") or contract
                    publication["message"] = failed_result.get("error") or "The affected producer requires a local route revision."
                    publication["failure_category"] = _execution_failure_category(failed_result)
                    failed_outcome = pipeline_outcome(failed_result)["kind"]
                    if failed_outcome in {"retry_step", "needs_input", "externally_blocked"}:
                        publication = {**failed_result, "outcome": failed_outcome}
                        successful = False
                    break
                emit_progress(
                    state,
                    "asset_review_repair",
                    "reviewed assets regenerated directly; rerunning package review",
                    steps=len(repair_calls),
                )
                publication = await _publish_plan_delivery(
                    state,
                    {**plan, "plan_id": latest.get("plan_id"), "plan_hash": plan_hash,
                     "review_feedback": review_issues},
                    results,
                )
            successful = bool(isinstance(publication, dict) and publication.get("status") == "success"
                              and publication.get("artifact_id"))
            if publication is None or (publication.get("status") == "success" and not publication.get("artifact_id")):
                publication = {"status": "review_failed", "message": "No staged delivery candidate was produced.",
                               "revision_contract": revision_contract([{
                                   "code": "missing_delivery_candidate",
                                   "message": "Successful generation steps did not expose a publishable asset or package.",
                                   "required_change": "Restore the owning producer's delivery contract without regenerating valid dependencies.",
                               }], scope="plan")}
            if isinstance(publication, dict) and publication.get("status") == "review_failed":
                successful = False
                if pipeline_outcome(publication)["kind"] == "revise_plan":
                    store.invalidate_plan(requested_kind, str(latest.get("plan_id") or ""),
                                          reason="delivery_structure_requires_revision")
        except Exception as exc:
            successful = False
            error = f"Generic package publication failed: {type(exc).__name__}: {exc}"
            category = _execution_failure_category({"status": "error", "error": error})
            publication = {
                "status": "retryable_error" if category == "transient_execution_error" else "error",
                "error": error,
                **(
                    {
                        "outcome": "retry_step",
                        "failure_category": category,
                        "retry_target": "publication",
                    }
                    if category == "transient_execution_error" else {}
                ),
            }
    transition_record = next(
        (
            item for item in reversed(results)
            if isinstance(item, dict)
            and str(item.get("status") or "") in _PIPELINE_TRANSITION_STATUSES
        ),
        None,
    )
    contract_failure_record = next(
        (
            item for item in reversed(results)
            if isinstance(item, dict)
            and (
                str(item.get("status") or "") in _PLAN_CONTRACT_FAILURE_STATUSES
                or str((item.get("result") or {}).get("failure_category") or "") == "plan_contract_error"
            )
        ),
        None,
    )
    if transition_record is not None:
        status_value = str(transition_record.get("status"))
    elif contract_failure_record is not None:
        status_value = "needs_revision"
    elif isinstance(publication, dict) and publication.get("status") == "review_failed":
        # Preserve the actual repair scope; the controller must not reinterpret
        # an exhausted content repair as a plan defect.
        status_value = "needs_revision"
    else:
        status_value = "success" if successful else "error"
    reference_only_done = successful and reference_plan
    if reference_only_done:
        status_value = "needs_final_generation_plan"
    payload = {
        "status": status_value,
        "plan_id": latest.get("plan_id"),
        "plan_approval_status": plan_approval_status,
        "n_steps": len(steps),
        "n_executed": len(results),
        "steps": results,
        "outputs": outputs,
        "repair_budget": repair_budget,
    }
    failed_record = next((item for item in reversed(results)
                          if item.get("status") not in {"success", "deferred_dependency"}), None)
    if not successful and failed_record:
        failure = failed_record.get("result") or {}
        if failure.get("status") == "pause":
            pause = failure.get("pause_event") or {}
            payload.update(status="needs_input", question=pause.get("question") or failure.get("question") or failure.get("message")
                           or failure.get("reason") or "Execution requires caller approval.",
                           context="\n".join(str(value) for value in (failure.get("approval_contract"), pause.get("context")) if value),
                           resume_contract={**stage_resume, "step_id": failed_record.get("step_id")})
        payload.update({
            "failed_step": failed_record.get("step_id"),
            "failed_tool": failed_record.get("tool_name"),
            "failure_category": _execution_failure_category(failure),
            "error": str(failure.get("error") or failure.get("message") or failure.get("blocked_reason") or "")[:4000],
            "revision_contract": pipeline_outcome(failure).get("revision_contract"),
            "failure_details": {key: failure[key] for key in (
                "error_code", "status_class", "http_status", "url", "parameter_violations",
                "missing_parameters", "argument_shape_mismatches", "execution_attempts",
                "required_dependencies", "repair_context",
                "response_excerpt", "retry_after", "bytes_received", "expected_bytes",
                "transfer_attempts", "retry_exhausted",
            ) if failure.get(key)},
        })
    if transition_record is not None:
        transition_result = transition_record.get("result") or {}
        payload["failed_step"] = transition_record.get("step_id")
        if transition_result.get("revision_contract"):
            payload["revision_contract"] = transition_result.get("revision_contract")
        payload["transition"] = {
            "status": transition_record.get("status"),
            "step_id": transition_record.get("step_id"),
            "tool_name": transition_record.get("tool_name"),
            "reference_request": transition_result.get("reference_request"),
            "search_queries": transition_result.get("search_queries") or [],
            "missing_fields": transition_result.get("missing_fields") or [],
            "resume_contract": transition_result.get("resume_contract"),
            "reason": transition_result.get("message") or transition_result.get("error"),
        }
        if transition_result.get("resume_contract"):
            payload["resume_contract"] = transition_result.get("resume_contract")
    if (payload.get("status") == "externally_blocked"
            and payload.get("failed_tool") == "generate_preprocessing_artifact"
            and payload.get("failure_category") in RESUMABLE_WRITER_FAILURES):
        payload.update(retry_target="artifact_writer", resume_contract={
            **stage_resume, "retry_target": "artifact_writer", "step_id": payload["failed_step"],
        })
    if contract_failure_record is not None:
        payload["failed_step"] = contract_failure_record.get("step_id")
        payload["failure_category"] = "plan_contract_error"
        contract_result = contract_failure_record.get("result") or {}
        payload["revision_contract"] = contract_result.get("revision_contract") or revision_contract([{
            "code": "plan_contract_error",
            "step_id": contract_failure_record.get("step_id"),
            "message": contract_result.get("error") or "The approved step violates its execution contract.",
            "required_change": "Amend only the affected step contract while preserving request authority.",
        }], scope="plan")
    if publication is not None:
        payload["publication"] = publication
        if publication.get("status") != "success":
            payload["error"] = publication.get("error") or publication.get("message") or publication.get("question") or "Delivery publication did not complete."
            if publication.get("failure_category"):
                payload["failure_category"] = publication["failure_category"]
        if isinstance(publication, dict) and publication.get("outcome") in {"retry_step", "needs_input", "externally_blocked"}:
            payload.update({key: publication[key] for key in ("resume_contract", "retry_target", "missing_fields", "question", "context", "options") if key in publication})
            payload["status"] = publication["status"]
            payload["outcome"] = publication["outcome"]
            payload["failure_category"] = str(
                publication.get("failure_category") or _execution_failure_category(publication)
            )
            if publication.get("outcome") == "externally_blocked" and publication.get("retry_target"):
                payload["execution_checkpoint"] = str(checkpoint_path)
                payload["resume_contract"] = {**stage_resume, "retry_target": publication["retry_target"]}
        if isinstance(publication, dict) and publication.get("revision_contract") and transition_record is None:
            payload["revision_contract"] = publication.get("revision_contract")
    if reference_only_done:
        # Evidence was persisted after each step.  Replanning receives the
        # exact executor record, including no_result/discovered_candidate.
        payload["reference_evidence"] = reference_evidence
        _clear_reference_plan_approval(
            state,
            str(latest.get("plan_id") or ""),
            str(latest.get("plan_hash") or plan_hash),
        )
    payload = with_pipeline_outcome(payload)
    payload["review_progress"] = (publication or {}).get("review_progress") or revision_progress(
        (payload.get("revision_contract") or {}).get("issues") or [],
        materials={step_id: _result_material_fingerprints(result) for step_id, result in outputs.items()},
        error=str((transition_record or {}).get("result", {}).get("error") or ""),
    )
    path = state.root / "planning" / "execution_result.json"
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    payload["path"] = str(path)
    emit_progress(
        state,
        "execute_plan_done",
        payload["status"],
        executed=len(results),
        steps=len(steps),
    )
    return payload


register_tool(
    ToolDefinition(
        name="execute_preprocessing_plan",
        description=(
            "Execute the current approved preprocessing plan in dependency order using the caller-provided "
            "environment. It dispatches only authorized tools, records every step, and stops on failure."
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "stop_on_error": {"type": "boolean", "default": True},
                "plan_id": {"type": "string", "description": "Exact approved plan identity to execute."},
                "plan_kind": {
                    "type": "string",
                    "enum": ["preprocessing_generation", "reference_evidence_only"],
                    "default": "preprocessing_generation",
                },
                "resume_target": {
                    "type": "string", "enum": ["authority_review", "publication", "artifact_writer"],
                    "description": "Explicit recovery after the stage service is restored: resume review/publication, or the interrupted artifact writer. Preserve successful producers and the approved plan.",
                },
            },
        },
        allowed_node_types=["data"],
        risk_level="high",
    ),
    execute_preprocessing_plan,
)

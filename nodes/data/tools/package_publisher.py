"""Single publishing boundary for data-node preprocessing packages.

Generators and reviewers work under ``.data_node_work``.  Only this module may
promote a reviewed staging tree into the visible ``data_preprocessing``
delivery directory.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from pathlib import Path, PurePosixPath
from typing import Any

from core import paths
from core.state import State
from nodes.data.delivery_contract import (
    artifact_name_for_attempt, outcome_fields,
)
from nodes.data.review.gate_registry import quality_gates_complete
from nodes.data.planning.schemas import explicit_output_path


WORK_ROOT_NAME = ".data_node_work"
STAGING_NAME = "package_staging"
DELIVERY_ROOT_NAME = "data_preprocessing"


def artifact_delivery_path(
    plan: dict[str, Any], step: dict[str, Any], output_id: str, source: Path,
) -> PurePosixPath:
    """One consumer-facing path for writer, materializer and review bindings."""
    arguments = step.get("tool_arguments") if isinstance(step.get("tool_arguments"), dict) else {}
    deliverable = next((item for item in plan.get("required_deliverables") or []
                        if str(item.get("id")) == str(output_id)), {})
    # Step paths are private work locations; the delivery contract owns layout.
    raw = str(deliverable.get("declared_output_path") or "").strip()
    # Also reconcile saved plans whose old normalizer marked an alternate
    # spelling explicit without replacing the logical-ID-derived path.
    caller_path = (explicit_output_path([deliverable], plan.get("preprocessing_request") or {})
                   if deliverable.get("output_path_is_explicit") is True else "")
    raw = caller_path or raw
    explicit_path = bool(raw and deliverable.get("output_path_is_explicit") is True)
    step_raw = ((arguments.get("output_paths") or {}).get(output_id)
                if isinstance(arguments.get("output_paths"), dict) else None)
    raw_name = PurePosixPath(raw).name if raw else ""
    role_aliases = {
        re.sub(r"[^a-z0-9]+", "", str(deliverable.get(key) or "").lower())
        for key in ("name", "name_or_role", "type", "scientific_role")
        if str(deliverable.get(key) or "").strip()
    }
    raw_alias = re.sub(r"[^a-z0-9]+", "", raw_name.lower())
    step_name = PurePosixPath(str(step_raw or source.name)).name
    if not explicit_path and step_name and (not raw or (
        raw_alias in role_aliases
        and not re.search(r"\.[A-Za-z0-9][A-Za-z0-9._-]*$", raw_name)
        and (bool(PurePosixPath(step_name).suffix) or source.is_dir())
    )):
        raw = step_name
    path = PurePosixPath(str(raw or source.name))
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"Output path for {output_id} escapes the package stage: {raw!r}")
    parts = tuple(part for part in path.parts if part not in {".", ""})
    while not explicit_path and parts and parts[0].lower() in {"outputs", "generated", "config", "scripts", "inputs"}:
        parts = parts[1:]
    if not parts:
        raise ValueError(f"Output path for {output_id} is empty.")
    path = PurePosixPath(*parts)
    profile = plan.get("review_profile") or (plan.get("preprocessing_request") or {}).get("review_profile") or "plan_bound"
    stage = re.sub(r"[^A-Za-z0-9._-]+", "_", str(step.get("stage_id") or deliverable.get("stage_id") or "").strip()).strip("._-")
    if profile != "request_bound" and stage and stage != "package":
        path = PurePosixPath("stages", stage) / path
    return path


def package_paths(
    state: State,
    delivery_name: str = "",
) -> tuple[Path, Path]:
    root = Path(str(state.root)).expanduser().resolve()
    # ``state.root`` is run-local cache.  With a bound project, the data node's
    # workspace is the durable output anchor shared by the other nodes; keep
    # the staging tree run-local and promote only the reviewed package there.
    delivery_root = Path(
        str(getattr(state, "workspace_root", None) or root)
    ).expanduser().resolve()
    if not delivery_name or delivery_name == "preprocessing":
        # A readable directory is presentation metadata, not a mutation of the
        # caller's signed request. Explicit caller names still take precedence.
        analysis = state.hook_state.get("_data_latest_requirement_analysis") or {}
        discipline = analysis.get("discipline") if isinstance(analysis.get("discipline"), dict) else {}
        software = analysis.get("simulation_software") if isinstance(analysis.get("simulation_software"), dict) else {}
        delivery_name = "_".join(filter(None, (
            str(discipline.get("primary") or "").strip(),
            str(analysis.get("calculation_type") or "").strip(),
            str(software.get("name") or software.get("primary") or "").strip(),
        )))
    safe_delivery_name = "".join(
        char if char.isalnum() or char in {"-", "_"} else "_"
        for char in str(delivery_name or "preprocessing").strip()
    ).strip("_")[:72]
    namespace = f"{safe_delivery_name or 'preprocessing'}__{state.run_id}"
    # final 目录必须走 core.paths 权威 helper（中央契约
    # test_node_calls_paths_helper 把关）—— 命名空间是本节点的需求，"包落在
    # workspace 的哪一层"是布局，布局只有 core.paths 一个真相源。
    final = paths.data_package_dir(delivery_root, namespace)
    return root / WORK_ROOT_NAME / STAGING_NAME, final


def prepare_package_staging(
    state: State,
    delivery_name: str = "",
) -> tuple[Path, Path]:
    staging, final = package_paths(state, delivery_name)
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True, exist_ok=True)
    return staging, final


def stage_runtime_mesh_asset(
    assets: dict[str, Any] | None,
    staging: Path,
) -> dict[str, Any] | None:
    """Import only a reviewed runtime mesh, never an entire generator case."""
    if not assets:
        return None
    raw_poly_mesh = assets.get("polyMesh_dir")
    if not raw_poly_mesh:
        return assets
    source_poly = Path(str(raw_poly_mesh)).expanduser()
    if not source_poly.is_dir():
        return assets
    source_poly = source_poly.resolve()
    target_poly = (staging / "constant" / "polyMesh").resolve()
    if source_poly == target_poly:
        return assets
    target_poly.parent.mkdir(parents=True, exist_ok=True)
    if target_poly.exists():
        shutil.rmtree(target_poly)
    shutil.copytree(source_poly, target_poly)
    assets["original_case_dir"] = str(source_poly.parent.parent)
    assets["polyMesh_dir"] = str(target_poly)
    assets["case_dir"] = str(staging.resolve())
    assets["openfoam_case_dir"] = str(staging.resolve())
    assets["source"] = "staged_reviewed_runtime_mesh"
    written = [str(path) for path in assets.get("written_files") or []]
    for path in target_poly.rglob("*"):
        value = str(path.resolve())
        if path.is_file() and value not in written:
            written.append(value)
    assets["written_files"] = written
    return assets


def remap_published_paths(value: Any, staging: Path, final: Path) -> Any:
    """Return a copy whose run-local staging paths point at final delivery."""
    old = str(staging.expanduser().resolve())
    new = str(final.expanduser().resolve())
    if isinstance(value, dict):
        return {key: remap_published_paths(item, staging, final) for key, item in value.items()}
    if isinstance(value, list):
        return [remap_published_paths(item, staging, final) for item in value]
    if isinstance(value, tuple):
        return tuple(remap_published_paths(item, staging, final) for item in value)
    if isinstance(value, Path):
        return Path(str(value).replace(old, new))
    if isinstance(value, str):
        return value.replace(old, new)
    return value


def publish_package(staging: Path, final: Path) -> list[str]:
    """Atomically promote the staging tree already validated by the publisher."""
    staging = staging.expanduser().resolve()
    final = final.expanduser().resolve()
    final.parent.mkdir(parents=True, exist_ok=True)
    backup = final.with_name(f".{final.name}.previous")
    if backup.exists():
        shutil.rmtree(backup)
    if final.exists():
        os.replace(final, backup)
    try:
        os.replace(staging, final)
    except Exception:
        if backup.exists() and not final.exists():
            os.replace(backup, final)
        raise
    if backup.exists():
        shutil.rmtree(backup)
    return [str(path.resolve()) for path in final.rglob("*") if path.is_file()]


def publish_dataset_delivery(
    state: State,
    *,
    staging: Path,
    final: Path,
    artifact_name: str,
    content: dict[str, Any],
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Publish one reviewed package and save its canonical dataset artifact."""
    request_id = str(content.get("request_id") or "").strip()
    request_hash = str(content.get("request_spec_hash") or "").strip()
    review_profile = str(content.get("review_profile") or "").strip()
    review_receipt = content.get("review_receipt")
    if (
        not request_id
        or len(request_hash) != 64
        or review_profile not in {"plan_bound", "request_bound"}
        or not isinstance(review_receipt, dict)
        or review_receipt.get("status") != "pass"
    ):
        raise ValueError("Refusing to publish without a passed authority-specific review receipt")
    published_content = remap_published_paths(content, staging, final)
    published_metadata = remap_published_paths(dict(metadata or {}), staging, final)
    delivery_files = published_content.get("files")
    if not isinstance(delivery_files, list):
        data_model = published_content.get("data_model")
        delivery_files = data_model.get("files") if isinstance(data_model, dict) else []
    delivery_files = delivery_files if isinstance(delivery_files, list) else []
    kind = (review_receipt.get("authority_review") or {}).get("delivery_kind")
    if not isinstance(kind, str) or kind not in {"dataset_materialized", "partial_dataset", "schema_only", "acquisition_plan"}:
        raise ValueError("Refusing publication without a reviewed delivery_kind")
    if not delivery_files or not published_content.get("lineage"):
        raise ValueError("Publication requires materialized files and source lineage")
    if (review_receipt.get("request_id") != request_id
            or review_receipt.get("request_spec_hash") != request_hash
            or review_receipt.get("review_profile") != review_profile):
        raise ValueError("Review receipt does not belong to this request")
    if not quality_gates_complete(review_receipt.get("quality_gates"), review_profile):
        raise ValueError("Publication requires passed requirement, quality and integrity gates")
    if not quality_gates_complete(published_content.get("quality_gates"), review_profile):
        raise ValueError("Published content contains incomplete quality gates")
    for item in delivery_files:
        path = (staging / str(item.get("path") or "")).resolve()
        if not path.is_relative_to(staging.resolve()) or not path.is_file():
            raise ValueError("Delivery asset is missing or outside the package")
        data = path.read_bytes()
        if not data or hashlib.sha256(data).hexdigest() != item.get("sha256"):
            raise ValueError("Delivery asset changed after review")
    expected_request = state.hook_state.get("_data_current_request")
    if isinstance(expected_request, dict) and expected_request:
        from nodes.data.planning.request_contract import preprocessing_request_fingerprint
        actual_request = content.get("preprocessing_request") or {}
        if preprocessing_request_fingerprint(actual_request) != preprocessing_request_fingerprint(expected_request):
            raise ValueError("Reviewed package belongs to a different bound request")
    fields = outcome_fields(
        kind,
        missing=([] if kind == "dataset_materialized" else
                 [{"reason": "Delivery is not a complete materialized dataset", "delivery_kind": kind}]),
    )
    published_content.update(fields)
    published_metadata.update(fields)
    manifest_path = Path(published_content.get("manifest_path") or final / "manifest.json")
    manifest_relative = manifest_path.resolve().relative_to(final.resolve())
    staged_manifest = staging / manifest_relative
    if not staged_manifest.resolve().is_relative_to(staging.resolve()):
        raise ValueError("Delivery manifest escapes staging")
    if any((staging / str(item.get("path") or "")).resolve() == staged_manifest.resolve()
           for item in delivery_files):
        raise ValueError("Framework delivery manifest cannot overwrite a reviewed asset")
    manifest = json.loads(staged_manifest.read_text(encoding="utf-8"))
    if not quality_gates_complete(manifest.get("quality_gates"), review_profile):
        raise ValueError("Delivery manifest contains incomplete quality gates")
    manifest.update(fields)
    staged_manifest.write_text(json.dumps(manifest, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    exports = []
    workspace = getattr(state, "workspace_root", None)
    for entry in published_content.get("workspace_exports") or []:
        if not workspace:
            raise ValueError("A project-relative export requires a bound Data workspace")
        root = Path(workspace).resolve()
        relative = PurePosixPath(entry["destination"])
        if relative.is_absolute() or ".." in relative.parts or not relative.parts or relative.parts[0] != root.name:
            raise ValueError("Project export must stay within the Data workspace: " + str(relative))
        target = root.joinpath(*relative.parts[1:]).resolve()
        source = (staging / entry["package_path"]).resolve()
        if not target.is_relative_to(root) or target == root or not source.is_relative_to(staging.resolve()):
            raise ValueError("Export path escapes its authorized workspace")
        data = source.read_bytes()
        if hashlib.sha256(data).hexdigest() != entry["sha256"]:
            raise ValueError("Export source changed after review")
        existing_hash = hashlib.sha256(target.read_bytes()).hexdigest() if target.is_file() else None
        baseline = entry.get("edit_baseline") or {}
        expected = {entry["sha256"]}
        if baseline.get("path") and Path(baseline["path"]).resolve() == target and baseline.get("sha256"):
            expected.add(baseline["sha256"])
        if target.exists() and (not target.is_file() or existing_hash not in expected):
            raise ValueError("Export would overwrite an unrelated existing file: " + str(target))
        exports.append({**entry, "path": str(target), "source": str(final / entry["package_path"])})
    # Validate every destination before writing aliases. Keep staging intact
    # until exports succeed, so an export I/O failure cannot force regeneration.
    for entry in exports:
        target = Path(entry["path"])
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + "." + state.run_id + ".tmp")
        try:
            shutil.copyfile(staging / entry["package_path"], temporary)
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
        if hashlib.sha256(target.read_bytes()).hexdigest() != entry["sha256"]:
            raise ValueError("Export bytes do not match the reviewed asset")
    published_content["workspace_exports"] = exports
    if exports:
        manifest["workspace_exports"] = exports
        staged_manifest.write_text(json.dumps(manifest, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    written_files = publish_package(staging, final)
    # Promotion preserves reviewed bytes and their hashes. Only framework-owned
    # metadata is remapped; caller files must never be rewritten after review.
    published_content.update({
        "artifact_kind": "data_preprocessing_delivery",
        "payload_kind": (
            "single_asset" if len(delivery_files) == 1
            else "asset_bundle" if delivery_files
            else "dataset"
        ),
        "files": delivery_files,
        "package_dir": str(final.resolve()),
        "manifest_path": published_content.get("manifest_path") or str((final / "manifest.json").resolve()),
    })
    published_metadata.update({
        "artifact_kind": "data_preprocessing_delivery",
        "package_dir": str(final.resolve()),
        "manifest_path": published_content["manifest_path"],
    })
    artifact = state.save_artifact(
        "dataset" if kind == "dataset_materialized" else "preprocessing_document",
        artifact_name_for_attempt(state, artifact_name),
        json.dumps(published_content, indent=2, ensure_ascii=False, default=str),
        metadata=published_metadata,
    )
    return {
        "artifact": artifact,
        "content": published_content,
        "written_files": written_files,
        "package_dir": str(final.resolve()),
        "manifest_path": published_content["manifest_path"],
    }


def save_failed_review(
    state: State,
    staging: Path,
    package_review: dict[str, Any],
) -> Path:
    """Keep the failed candidate available for inspection and targeted repair."""
    audit_root = Path(
        str(getattr(state, "workspace_root", None) or state.root)
    ).expanduser().resolve()
    audit_dir = audit_root / "audit" / Path(state.root).name
    audit_dir.mkdir(parents=True, exist_ok=True)
    content = json.dumps({**package_review, "run_id": state.run_id, "staging_dir": str(staging)}, indent=2, ensure_ascii=False, default=str)
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:12]
    report = audit_dir / f"preprocessing_review_{digest}.json"
    report.write_text(content, encoding="utf-8")
    return report


def cleanup_generated_workspaces(state: State, *, downloads_only: bool = False) -> list[str]:
    root = Path(str(state.root)).expanduser().resolve()
    removed: list[str] = []
    work_root = root / WORK_ROOT_NAME
    if downloads_only:
        work_root = work_root / "downloads"
    if work_root.is_symlink() or work_root.parent.is_symlink():
        return removed
    if work_root.is_dir():
        shutil.rmtree(work_root)
        removed.append(str(work_root))
    if downloads_only:
        return removed
    data_root = Path(
        str(getattr(state, "workspace_root", None) or root)
    ).expanduser().resolve() / DELIVERY_ROOT_NAME
    if data_root.is_dir():
        for metadata in data_root.rglob(".DS_Store"):
            try:
                metadata.unlink()
            except OSError:
                pass
    return removed


def discard_incomplete_delivery(state: State) -> list[str]:
    """Discard only run-local staging; never touch an earlier published request."""
    staging = Path(str(state.root)).expanduser().resolve() / WORK_ROOT_NAME / STAGING_NAME
    if not staging.is_dir():
        return []
    shutil.rmtree(staging)
    return [str(staging)]

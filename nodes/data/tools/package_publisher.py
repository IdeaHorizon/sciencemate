"""Single publishing boundary for data-node preprocessing packages.

Generators and reviewers work under ``.data_node_work``.  Only this module may
promote a reviewed staging tree into the visible ``data_preprocessing``
delivery directory.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path
from typing import Any

from core import paths
from core.state import State


WORK_ROOT_NAME = ".data_node_work"
STAGING_NAME = "package_staging"
DELIVERY_ROOT_NAME = "data_preprocessing"


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


def _rewrite_published_text_paths(root: Path, staging: Path, final: Path) -> None:
    old = str(staging.resolve())
    new = str(final.resolve())
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in {".json", ".md", ".yaml", ".yml", ".txt"}:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if old in text:
            path.write_text(text.replace(old, new), encoding="utf-8")


def publish_package(staging: Path, final: Path) -> list[str]:
    """Atomically promote one reviewed staging tree into final delivery.

    判决拆除 O10（pp:112 降格，2026-08-31，呈裁④定案）：缺 manifest.json 不再
    拒晋升 —— 照发，包内落一份 audit/manifest_missing.json 见证（产物如实写明
    哪些检查没过），下游照读照报。**显式接受的风险**：.previous 备份只有一层，
    降格会提高「不完整包顶掉上一版完整包」的频率；owner 拍板不加深备份层数，
    以 manifest 见证 + 下游 review 承担质量闭环。
    """
    staging = staging.expanduser().resolve()
    final = final.expanduser().resolve()
    if not (staging / "manifest.json").is_file():
        witness_dir = staging / "audit"
        witness_dir.mkdir(parents=True, exist_ok=True)
        (witness_dir / "manifest_missing.json").write_text(json.dumps({
            "check": "manifest_present",
            "passed": False,
            "reason": "package was published without manifest.json; downstream consumers must not assume a manifest",
        }, indent=2, ensure_ascii=False), encoding="utf-8")
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
    _rewrite_published_text_paths(final, staging, final)
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
    written_files = publish_package(staging, final)
    published_content = remap_published_paths(content, staging, final)
    published_metadata = remap_published_paths(dict(metadata or {}), staging, final)
    delivery_files = published_content.get("files")
    if not isinstance(delivery_files, list):
        data_model = published_content.get("data_model")
        delivery_files = data_model.get("files") if isinstance(data_model, dict) else []
    delivery_files = delivery_files if isinstance(delivery_files, list) else []
    # ``publish_package`` rewrites staging paths embedded in text files after
    # the atomic move.  That changes their bytes (notably
    # ``audit/mesh_delivery.json``), so refresh the already-declared file
    # records against the published tree before saving the dataset artifact.
    published_root = final.expanduser().resolve()
    for record in delivery_files:
        if not isinstance(record, dict):
            continue
        relative = str(record.get("path") or "").strip()
        if not relative:
            continue
        published_path = (published_root / relative).resolve()
        try:
            published_path.relative_to(published_root)
        except ValueError:
            continue
        if published_path.is_file():
            record["size"] = published_path.stat().st_size
            record["sha256"] = hashlib.sha256(published_path.read_bytes()).hexdigest()
    published_content.update({
        "artifact_kind": "data_preprocessing_delivery",
        "payload_kind": (
            "single_asset" if len(delivery_files) == 1
            else "asset_bundle" if delivery_files
            else "dataset"
        ),
        "files": delivery_files,
        "delivery_complete": True,
        "package_dir": str(final.resolve()),
        "manifest_path": str((final / "manifest.json").resolve()),
    })
    published_metadata.update({
        "artifact_kind": "data_preprocessing_delivery",
        "delivery_complete": True,
        "package_dir": str(final.resolve()),
        "manifest_path": str((final / "manifest.json").resolve()),
    })
    artifact = state.save_artifact(
        "dataset",
        artifact_name,
        json.dumps(published_content, indent=2, ensure_ascii=False, default=str),
        metadata=published_metadata,
    )
    return {
        "artifact": artifact,
        "content": published_content,
        "written_files": written_files,
        "package_dir": str(final.resolve()),
        "manifest_path": str((final / "manifest.json").resolve()),
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


def cleanup_generated_workspaces(state: State) -> list[str]:
    root = Path(str(state.root)).expanduser().resolve()
    removed: list[str] = []
    work_root = root / WORK_ROOT_NAME
    if work_root.is_dir():
        shutil.rmtree(work_root)
        removed.append(str(work_root))
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

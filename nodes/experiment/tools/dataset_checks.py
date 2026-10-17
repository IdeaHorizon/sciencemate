"""Pure, receiver-side structural checks for a dataset package.

This module does not select an artifact, authorize its use, mutate run state, or
decide whether the package is scientifically suitable for a task.  Callers own
those decisions.  The helper only inspects the supplied artifact record and the
filesystem paths that record declares.
"""
from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

_ALIASES = {
    "package_path": ("package_path", "package_dir", "data_path", "path"),
    "manifest": ("manifest", "manifest_path"),
    "lineage": ("lineage",),
    "downstream_contract": ("downstream_contract",),
}

_LIMITATIONS = [
    "selection_and_authorization_not_evaluated",
    "manifest_json_shape_only",
    "lineage_and_downstream_contract_presence_only",
    "file_content_hashes_not_recomputed",
    "scientific_applicability_not_evaluated",
]


def _identity(record: Any) -> dict[str, Any]:
    if not isinstance(record, Mapping):
        return {}
    fields = {"artifact_id", "id", "type", "version", "content_hash"}
    return {
        key: value
        for key, value in record.items()
        if key in fields or str(key).startswith("produced_by")
    }


def _result(
    *,
    passed: bool,
    missing: list[str],
    undeclared: list[str],
    missing_assets: list[str],
    package_dir: str | None,
    manifest_path: str | None,
    artifact_identity: dict[str, Any],
) -> dict[str, Any]:
    return {
        "passed": passed,
        "missing_dataset_fields": missing,
        "undeclared_requested_assets": undeclared,
        "missing_requested_assets": missing_assets,
        "package_dir": package_dir,
        "manifest_path": manifest_path,
        "artifact_identity": artifact_identity,
        "limitations": list(_LIMITATIONS),
    }


def inspect_dataset_package(
    record: Any,
    *,
    required_assets: Iterable[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Inspect one already-selected dataset artifact and its package structure.

    ``required_assets`` is an optional, caller-owned list of validated asset
    declarations.  It narrows this mechanical presence check; it does not make
    this function the owner of task fit.  Legacy record aliases and staged
    package layouts match the existing Experiment dataset consumer.
    """
    artifact_identity = _identity(record)
    if record is None:
        return _result(
            passed=False,
            missing=["dataset_record_missing"],
            undeclared=[],
            missing_assets=[],
            package_dir=None,
            manifest_path=None,
            artifact_identity=artifact_identity,
        )
    if not isinstance(record, Mapping):
        return _result(
            passed=False,
            missing=["dataset_record_invalid"],
            undeclared=[],
            missing_assets=[],
            package_dir=None,
            manifest_path=None,
            artifact_identity=artifact_identity,
        )
    if record.get("type") != "dataset":
        return _result(
            passed=False,
            missing=["dataset_type_invalid"],
            undeclared=[],
            missing_assets=[],
            package_dir=None,
            manifest_path=None,
            artifact_identity=artifact_identity,
        )

    metadata = record.get("metadata")
    view = dict(metadata) if isinstance(metadata, Mapping) else {}
    try:
        content = json.loads(record.get("content") or "{}")
        if isinstance(content, dict):
            for key, value in content.items():
                view.setdefault(key, value)
    except (TypeError, json.JSONDecodeError):
        pass

    missing = [
        field
        for field, aliases in _ALIASES.items()
        if not any(view.get(alias) not in (None, "", [], {}) for alias in aliases)
    ]

    package_value = next(
        (view.get(alias) for alias in _ALIASES["package_path"] if view.get(alias)),
        None,
    )
    package_path: Path | None = None
    package_dir: str | None = None
    if not isinstance(package_value, str):
        missing.append("package_path_not_usable")
    else:
        candidate = Path(package_value).expanduser()
        if not candidate.is_dir():
            missing.append("package_path_not_found_or_not_directory")
        else:
            package_path = candidate.resolve()
            package_dir = str(package_path)

    manifest_value = next(
        (view.get(alias) for alias in _ALIASES["manifest"] if view.get(alias)),
        None,
    )
    manifest_path: str | None = None
    manifest_candidate = (
        Path(manifest_value).expanduser() if isinstance(manifest_value, str) else None
    )
    if manifest_candidate is None or not manifest_candidate.is_file():
        missing.append("manifest_path_not_found")
    else:
        # Preserve the existing consumer's observable alias value.  Unlike the
        # package directory, the legacy delivery record did not canonicalize
        # this path before persisting it.
        manifest_path = str(manifest_value)
        try:
            manifest = json.loads(manifest_candidate.read_text(encoding="utf-8"))
            if not isinstance(manifest, dict):
                missing.append("manifest_not_an_object")
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            missing.append("manifest_not_readable_json")

    contract = view.get("downstream_contract")
    if isinstance(contract, str):
        try:
            contract = json.loads(contract)
        except json.JSONDecodeError:
            pass
    contract_text = (
        json.dumps(contract, ensure_ascii=False, sort_keys=True)
        if isinstance(contract, dict | list)
        else str(contract or "")
    )

    asset_names: list[str] = []
    for asset in required_assets:
        name = asset.get("name") if isinstance(asset, Mapping) else None
        if isinstance(name, str) and name:
            asset_names.append(name)
        else:
            missing.append("required_asset_invalid")

    undeclared = [name for name in asset_names if name not in contract_text]
    missing_assets: list[str] = []
    if package_path is not None:
        for name in asset_names:
            direct = package_path / name
            # Preserve the existing consumer's staged-directory compatibility:
            # a requested basename may live below a declared stage directory.
            found = direct.is_file() or any(
                candidate.is_file()
                for candidate in package_path.rglob(Path(name).name)
            )
            if not found:
                missing_assets.append(name)

    return _result(
        passed=not missing and not undeclared and not missing_assets,
        missing=missing,
        undeclared=undeclared,
        missing_assets=missing_assets,
        package_dir=package_dir,
        manifest_path=manifest_path,
        artifact_identity=artifact_identity,
    )

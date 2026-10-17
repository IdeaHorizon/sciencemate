"""Pure dataset-package inspection shared by current and parent inputs."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from nodes.experiment.tools.dataset_checks import inspect_dataset_package


def _record(package_dir: Path, manifest_path: Path, **overrides) -> dict:
    record = {
        "id": "dataset__package",
        "type": "dataset",
        "version": 3,
        "content_hash": "a" * 64,
        "produced_by_run_id": "data-child",
        "produced_by_node_type": "data",
        "content": "{}",
        "metadata": {
            "package_path": str(package_dir),
            "manifest": str(manifest_path),
            "lineage": {"source": "fixture"},
            "downstream_contract": {"files": ["input.dat"]},
        },
    }
    record.update(overrides)
    return record


def test_valid_dataset_package_reports_structure_and_exact_artifact_identity(
    tmp_path: Path,
) -> None:
    package_dir = tmp_path / "package"
    package_dir.mkdir()
    (package_dir / "input.dat").write_text("payload\n", encoding="utf-8")
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps({"files": []}), encoding="utf-8")
    record = _record(package_dir, manifest_path)

    result = inspect_dataset_package(
        record,
        required_assets=({"name": "input.dat"},),
    )

    assert result["passed"] is True, result
    assert result["missing_dataset_fields"] == []
    assert result["undeclared_requested_assets"] == []
    assert result["missing_requested_assets"] == []
    assert result["package_dir"] == str(package_dir.resolve())
    assert result["manifest_path"] == str(manifest_path.resolve())
    assert result["artifact_identity"] == {
        "id": "dataset__package",
        "type": "dataset",
        "version": 3,
        "content_hash": "a" * 64,
        "produced_by_run_id": "data-child",
        "produced_by_node_type": "data",
    }
    assert "file_content_hashes_not_recomputed" in result["limitations"]
    assert "scientific_applicability_not_evaluated" in result["limitations"]


def test_supplied_artifact_id_is_preserved_without_inventing_id(
    tmp_path: Path,
) -> None:
    package_dir = tmp_path / "package"
    package_dir.mkdir()
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text("{}", encoding="utf-8")
    record = _record(package_dir, manifest_path)
    record["artifact_id"] = record.pop("id")

    result = inspect_dataset_package(record)

    assert result["passed"] is True, result
    assert result["artifact_identity"]["artifact_id"] == "dataset__package"
    assert "id" not in result["artifact_identity"]


@pytest.mark.parametrize(
    ("record", "reason"),
    [
        (None, "dataset_record_missing"),
        ({"id": "report__wrong", "type": "report"}, "dataset_type_invalid"),
    ],
)
def test_missing_record_or_wrong_type_fails_without_state(record, reason) -> None:
    result = inspect_dataset_package(record)

    assert result["passed"] is False
    assert result["missing_dataset_fields"] == [reason]
    assert result["package_dir"] is None
    assert result["manifest_path"] is None


def test_missing_package_directory_and_bad_manifest_are_reported(
    tmp_path: Path,
) -> None:
    missing_dir = tmp_path / "missing-package"
    bad_manifest = tmp_path / "manifest.json"
    bad_manifest.write_text("{not-json", encoding="utf-8")

    result = inspect_dataset_package(_record(missing_dir, bad_manifest))

    assert result["passed"] is False
    assert "package_path_not_found_or_not_directory" in result["missing_dataset_fields"]
    assert "manifest_not_readable_json" in result["missing_dataset_fields"]
    assert result["package_dir"] is None
    assert result["manifest_path"] == str(bad_manifest.resolve())


def test_required_assets_report_contract_and_filesystem_gaps(
    tmp_path: Path,
) -> None:
    package_dir = tmp_path / "package"
    package_dir.mkdir()
    (package_dir / "present.dat").write_text("present\n", encoding="utf-8")
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text("{}", encoding="utf-8")
    record = _record(package_dir, manifest_path)
    record["metadata"]["downstream_contract"] = {"files": ["present.dat"]}

    result = inspect_dataset_package(
        record,
        required_assets=(
            {"name": "present.dat"},
            {"name": "missing.dat"},
        ),
    )

    assert result["passed"] is False
    assert result["undeclared_requested_assets"] == ["missing.dat"]
    assert result["missing_requested_assets"] == ["missing.dat"]


def test_metadata_and_content_aliases_preserve_staged_package_layout(
    tmp_path: Path,
) -> None:
    package_dir = tmp_path / "package"
    staged = package_dir / "stage-one"
    staged.mkdir(parents=True)
    (staged / "mesh.msh").write_text("mesh\n", encoding="utf-8")
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text("{}", encoding="utf-8")
    record = {
        "id": "dataset__aliases",
        "type": "dataset",
        "metadata": {"data_path": str(package_dir)},
        "content": json.dumps({
            "manifest_path": str(manifest_path),
            "lineage": {"source": "content-alias"},
            "downstream_contract": json.dumps({"files": ["mesh.msh"]}),
        }),
    }

    result = inspect_dataset_package(
        record,
        required_assets=({"name": "mesh.msh"},),
    )

    assert result["passed"] is True, result
    assert result["package_dir"] == str(package_dir.resolve())
    assert result["manifest_path"] == str(manifest_path.resolve())


def test_parent_package_without_task_assets_still_requires_common_structure(
    tmp_path: Path,
) -> None:
    package_dir = tmp_path / "package"
    package_dir.mkdir()
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text("[]", encoding="utf-8")
    record = _record(package_dir, manifest_path)

    result = inspect_dataset_package(record)

    assert result["passed"] is False
    assert result["undeclared_requested_assets"] == []
    assert result["missing_requested_assets"] == []
    assert result["missing_dataset_fields"] == ["manifest_not_an_object"]
    assert "scientific_applicability_not_evaluated" in result["limitations"]


def test_parent_package_without_task_assets_still_requires_lineage_and_contract(
    tmp_path: Path,
) -> None:
    package_dir = tmp_path / "package"
    package_dir.mkdir()
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text("{}", encoding="utf-8")
    record = _record(package_dir, manifest_path)
    record["metadata"].pop("lineage")
    record["metadata"].pop("downstream_contract")

    result = inspect_dataset_package(record)

    assert result["passed"] is False
    assert result["missing_dataset_fields"] == [
        "lineage",
        "downstream_contract",
    ]
    assert result["undeclared_requested_assets"] == []
    assert result["missing_requested_assets"] == []

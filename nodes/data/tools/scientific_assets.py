"""Content-oriented scientific asset classification and minimum readiness checks.

The registry is deliberately organized by data representation and file format,
not by scientific discipline.  A NetCDF adapter therefore serves weather,
ocean, geoscience, heat-transfer, and any future spatial-field workflow.
"""
from __future__ import annotations

import csv
import hashlib
import json
import re
import math
import mimetypes
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from core.state import State
_FORMAT_SPECS: tuple[dict[str, Any], ...] = (
    {
        "format": "csv", "suffixes": (".csv",), "asset_kind": "dataset",
        "data_model_kind": "table", "adapter": "tabular",
    },
    {
        "format": "tsv", "suffixes": (".tsv",), "asset_kind": "dataset",
        "data_model_kind": "table", "adapter": "tabular",
    },
    {
        "format": "json", "suffixes": (".json", ".jsonl", ".ndjson"),
        "asset_kind": "dataset", "data_model_kind": "unknown", "adapter": "json",
    },
    {
        "format": "structured_text", "suffixes": (".xml", ".yaml", ".yml", ".toml"),
        "asset_kind": "parameters", "data_model_kind": "unknown", "adapter": "structured_text",
    },
    {
        "format": "plain_text", "suffixes": (".txt", ".log", ".md"),
        "asset_kind": "dataset", "data_model_kind": "text_log", "adapter": "text",
    },
    {
        "format": "coordinate_text", "suffixes": (".dat", ".xy"),
        "asset_kind": "dataset", "data_model_kind": "table", "adapter": "text",
    },
    {
        "format": "netcdf", "suffixes": (".nc", ".nc4", ".cdf"),
        "asset_kind": "dataset", "data_model_kind": "spatial_field", "adapter": "netcdf",
    },
    {
        "format": "grib", "suffixes": (".grib", ".grb", ".grib2", ".grb2"),
        "asset_kind": "dataset", "data_model_kind": "spatial_field", "adapter": "grib",
    },
    {
        "format": "hdf5", "suffixes": (".h5", ".hdf5", ".hdf"),
        "asset_kind": "dataset", "data_model_kind": "tensor", "adapter": "hdf5",
    },
    {
        "format": "parquet", "suffixes": (".parquet",),
        "asset_kind": "dataset", "data_model_kind": "table", "adapter": "parquet",
    },
    {
        "format": "arrow", "suffixes": (".feather", ".arrow"),
        "asset_kind": "dataset", "data_model_kind": "table", "adapter": "arrow",
    },
    {
        "format": "numpy", "suffixes": (".npy", ".npz"),
        "asset_kind": "dataset", "data_model_kind": "tensor", "adapter": "numpy",
    },
    {
        "format": "matlab", "suffixes": (".mat",),
        "asset_kind": "dataset", "data_model_kind": "tensor", "adapter": "matlab",
    },
    {
        "format": "image", "suffixes": (".png", ".jpg", ".jpeg", ".bmp", ".webp"),
        "asset_kind": "dataset", "data_model_kind": "tensor", "adapter": "image",
    },
    {
        "format": "raster_field", "suffixes": (".tif", ".tiff", ".geotiff"),
        "asset_kind": "dataset", "data_model_kind": "spatial_field", "adapter": "raster",
    },
    {
        "format": "audio", "suffixes": (".wav", ".flac", ".mp3", ".ogg"),
        "asset_kind": "dataset", "data_model_kind": "time_series", "adapter": "audio",
    },
    {
        "format": "video", "suffixes": (".mp4", ".mov", ".avi", ".mkv"),
        "asset_kind": "dataset", "data_model_kind": "tensor", "adapter": "video",
    },
    {
        "format": "geospatial_vector", "suffixes": (".geojson", ".shp", ".gpkg"),
        "asset_kind": "dataset", "data_model_kind": "spatial_field", "adapter": "geospatial_vector",
    },
    {
        "format": "sqlite", "suffixes": (".sqlite", ".sqlite3", ".db"),
        "asset_kind": "dataset", "data_model_kind": "table", "adapter": "sqlite",
    },
    {
        "format": "ml_record", "suffixes": (".tfrecord", ".record", ".safetensors"),
        "asset_kind": "dataset", "data_model_kind": "tensor", "adapter": "ml_record",
    },
    {
        "format": "unsafe_serialized_object", "suffixes": (".pkl", ".pickle", ".pt", ".pth"),
        "asset_kind": "dataset", "data_model_kind": "unknown", "adapter": "unsafe_serialized_object",
        "safe_to_deserialize": False,
    },
    {
        "format": "fits", "suffixes": (".fits", ".fit", ".fts"),
        "asset_kind": "dataset", "data_model_kind": "tensor", "adapter": "fits",
    },
    {
        "format": "graph", "suffixes": (".graphml", ".gexf", ".gml", ".edgelist"),
        "asset_kind": "dataset", "data_model_kind": "graph", "adapter": "graph",
    },
    {
        "format": "spectrum", "suffixes": (".jdx", ".dx", ".spc"),
        "asset_kind": "dataset", "data_model_kind": "spectrum", "adapter": "spectrum",
    },
    {
        "format": "atomic_structure",
        "suffixes": (".cif", ".vasp", ".poscar", ".contcar", ".xyz", ".pdb", ".mol", ".sdf"),
        "asset_kind": "structure", "data_model_kind": "particle_structure", "adapter": "structure",
    },
    {
        "format": "mesh",
        "suffixes": (
            ".geo", ".msh", ".stl", ".step", ".stp", ".iges", ".igs", ".brep",
            ".obj", ".ply", ".vtk", ".vtu", ".cgns", ".foam", ".cas", ".med",
            ".unv", ".exo", ".ex2", ".e", ".su2", ".bdf", ".nas", ".inp",
        ),
        "asset_kind": "geometry_or_mesh", "data_model_kind": "mesh", "adapter": "mesh",
    },
    {
        "format": "archive", "suffixes": (".zip", ".tar", ".tgz", ".gz", ".bz2", ".xz"),
        "asset_kind": "archive", "data_model_kind": "multimodal_bundle", "adapter": "archive",
    },
    {
        "format": "document", "suffixes": (".pdf",),
        "asset_kind": "document", "data_model_kind": "text_log", "adapter": "document",
    },
)

FORMAT_BY_SUFFIX: dict[str, dict[str, Any]] = {
    suffix: spec for spec in _FORMAT_SPECS for suffix in spec["suffixes"]
}
SCIENTIFIC_ASSET_SUFFIXES = frozenset(FORMAT_BY_SUFFIX)
DATASET_SUFFIXES = frozenset(
    suffix for suffix, spec in FORMAT_BY_SUFFIX.items() if spec["asset_kind"] == "dataset"
)
GEOMETRY_OR_STRUCTURE_SUFFIXES = frozenset(
    suffix
    for suffix, spec in FORMAT_BY_SUFFIX.items()
    if spec["asset_kind"] in {"geometry_or_mesh", "structure"}
)
PARAMETER_SUFFIXES = frozenset(
    suffix for suffix, spec in FORMAT_BY_SUFFIX.items() if spec["asset_kind"] == "parameters"
)
ARCHIVE_SUFFIXES = frozenset(
    suffix for suffix, spec in FORMAT_BY_SUFFIX.items() if spec["asset_kind"] == "archive"
)

_SIGNATURES: tuple[tuple[str, bytes, dict[str, Any]], ...] = (
    ("hdf5", b"\x89HDF\r\n\x1a\n", FORMAT_BY_SUFFIX[".h5"]),
    ("netcdf", b"CDF\x01", FORMAT_BY_SUFFIX[".nc"]),
    ("netcdf", b"CDF\x02", FORMAT_BY_SUFFIX[".nc"]),
    ("netcdf", b"CDF\x05", FORMAT_BY_SUFFIX[".nc"]),
    ("numpy", b"\x93NUMPY", FORMAT_BY_SUFFIX[".npy"]),
    ("parquet", b"PAR1", FORMAT_BY_SUFFIX[".parquet"]),
    ("zip", b"PK\x03\x04", FORMAT_BY_SUFFIX[".zip"]),
    ("gzip", b"\x1f\x8b", FORMAT_BY_SUFFIX[".gz"]),
    ("fits", b"SIMPLE  =", FORMAT_BY_SUFFIX[".fits"]),
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _signature_spec(head: bytes, tail: bytes) -> dict[str, Any] | None:
    for _name, signature, spec in _SIGNATURES:
        if head.startswith(signature):
            return spec
    if len(tail) >= 4 and tail.endswith(b"PAR1"):
        return FORMAT_BY_SUFFIX[".parquet"]
    if head[:4] in {b"GRIB"}:
        return FORMAT_BY_SUFFIX[".grib"]
    return None


def infer_scientific_suffix(data: bytes, content_type: str = "") -> str:
    """Infer a conservative suffix from a bounded payload signature/MIME type."""
    head = data[:512]
    tail = data[-512:] if len(data) > 512 else data
    spec = _signature_spec(head, tail)
    if spec:
        return str(spec["suffixes"][0])
    mime = str(content_type or "").split(";", 1)[0].strip().lower()
    return {
        "application/x-netcdf": ".nc",
        "application/netcdf": ".nc",
        "application/x-hdf5": ".h5",
        "application/vnd.apache.parquet": ".parquet",
        "application/zip": ".zip",
        "application/gzip": ".gz",
        "text/csv": ".csv",
        "application/json": ".json",
    }.get(mime, "")


def _directory_spec(path: Path) -> dict[str, Any]:
    if path.suffix.lower() == ".zarr" or any((path / marker).exists() for marker in (".zgroup", ".zarray", "zarr.json")):
        return {
            "format": "zarr", "asset_kind": "dataset",
            "data_model_kind": "tensor", "adapter": "zarr",
        }
    if (path / "constant" / "polyMesh").is_dir() or (path / "system" / "controlDict").is_file():
        return {
            "format": "openfoam_case", "asset_kind": "simulation_case",
            "data_model_kind": "simulation_case", "adapter": "simulation_case",
        }
    if (path / "POSCAR").is_file() or (path / "INCAR").is_file():
        return {
            "format": "electronic_structure_case", "asset_kind": "simulation_case",
            "data_model_kind": "simulation_case", "adapter": "simulation_case",
        }
    return {
        "format": "directory_bundle", "asset_kind": "bundle",
        "data_model_kind": "multimodal_bundle", "adapter": "directory",
    }


def _tabular_checks(path: Path, delimiter: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    gates: list[dict[str, Any]] = []
    metadata: dict[str, Any] = {}
    try:
        with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as handle:
            reader = csv.reader(handle, delimiter=delimiter)
            rows = []
            for index, row in enumerate(reader):
                rows.append(row)
                if index >= 1000:
                    break
        width = len(rows[0]) if rows else 0
        consistent = bool(rows) and all(len(row) == width for row in rows)
        data_rows = rows[1:] if len(rows) > 1 else []
        missing_values = sum(
            1 for row in data_rows for value in row if not str(value).strip()
        )
        duplicate_rows = len(data_rows) - len({tuple(row) for row in data_rows})
        nonfinite_numeric = 0
        for row in data_rows:
            for value in row:
                try:
                    parsed = float(value)
                except (TypeError, ValueError):
                    continue
                if not math.isfinite(parsed):
                    nonfinite_numeric += 1
        metadata.update({
            "sampled_rows": len(rows),
            "column_count": width,
            "header": rows[0][:100] if rows else [],
            "sample_truncated": len(rows) >= 1001,
            "missing_values_sampled": missing_values,
            "duplicate_rows_sampled": duplicate_rows,
            "nonfinite_numeric_values_sampled": nonfinite_numeric,
        })
        gates.extend([
            {"name": "tabular_rows_present", "status": "pass" if rows else "fail", "evidence": len(rows)},
            {"name": "tabular_column_count_positive", "status": "pass" if width > 0 else "fail", "evidence": width},
            {"name": "tabular_row_width_consistent", "status": "pass" if consistent else "fail", "evidence": "first 1001 rows"},
            {"name": "tabular_missing_values_checked", "status": "pass", "evidence": missing_values},
            {"name": "tabular_duplicate_rows_checked", "status": "pass", "evidence": duplicate_rows},
            {
                "name": "tabular_numeric_values_finite",
                "status": "pass" if nonfinite_numeric == 0 else "fail",
                "evidence": nonfinite_numeric,
            },
        ])
    except OSError as exc:
        gates.append({"name": "tabular_parse", "status": "fail", "evidence": f"{type(exc).__name__}: {exc}"})
    return gates, metadata


def surface_topology_metrics(cells: list[list[int]]) -> dict[str, Any]:
    """Compute representation-independent topology facts for surface cells."""
    cell_edges: list[list[tuple[int, int]]] = []
    edge_counts: Counter[tuple[int, int]] = Counter()
    for cell in cells:
        edges = [
            tuple(sorted((node, cell[(index + 1) % len(cell)])))
            for index, node in enumerate(cell)
        ]
        cell_edges.append(edges)
        edge_counts.update(edges)

    boundary_edges = [edge for edge, count in edge_counts.items() if count == 1]
    adjacency: dict[int, set[int]] = defaultdict(set)
    for left, right in boundary_edges:
        adjacency[left].add(right)
        adjacency[right].add(left)
    open_nodes = [node for node, neighbours in adjacency.items() if len(neighbours) != 2]
    unseen = set(adjacency)
    boundary_components = 0
    while unseen:
        boundary_components += 1
        stack = [unseen.pop()]
        while stack:
            for neighbour in adjacency[stack.pop()]:
                if neighbour in unseen:
                    unseen.remove(neighbour)
                    stack.append(neighbour)

    parents = list(range(len(cell_edges)))

    def find(cell: int) -> int:
        while parents[cell] != cell:
            parents[cell] = parents[parents[cell]]
            cell = parents[cell]
        return cell

    edge_owner: dict[tuple[int, int], int] = {}
    for cell_id, edges in enumerate(cell_edges):
        for edge in edges:
            owner = edge_owner.setdefault(edge, cell_id)
            left, right = find(cell_id), find(owner)
            if left != right:
                parents[right] = left
    nonmanifold = sum(count > 2 for count in edge_counts.values())
    boundary_closed = bool(boundary_edges) and not open_nodes
    return {
        "n_unique_surface_edges": len(edge_counts),
        "n_boundary_edges": len(boundary_edges),
        "n_boundary_components": boundary_components,
        "n_closed_boundary_loops": boundary_components if boundary_closed else 0,
        "n_open_or_nonmanifold_boundary_nodes": len(open_nodes),
        "n_nonmanifold_surface_edges": nonmanifold,
        "n_surface_components": len({find(cell) for cell in range(len(cell_edges))}) if cell_edges else 0,
        "boundary_loops_closed": boundary_closed,
        "surface_shell_closed": bool(edge_counts) and not boundary_edges and not nonmanifold,
    }


def validate_format_version(content: str, requested_format: str, requested_version: str = "") -> str | None:
    """Check declared serialization versions against headers, not model claims.

    Unknown formats remain owned by their parser/semantic reviewer. A major
    version request accepts its minor revisions; an exact version stays exact.
    """
    label = str(requested_format or "").strip().lower()
    for family, header in (
        ("msh", r"\A\s*\$MeshFormat\s+([0-9.]+)\s"),
        ("vtk", r"\A\s*# vtk DataFile Version\s+([0-9.]+)"),
    ):
        declared = re.fullmatch(r"(?:gmsh[\s_-]*)?" + family + r"[\s_-]*([0-9.]+)?", label)
        if not declared:
            continue
        expected = str(requested_version or declared.group(1) or "").strip()
        if not re.fullmatch(r"\d+(?:\.\d+)*", expected):
            return None
        observed = re.search(header, content[:512], flags=re.I)
        version = observed.group(1) if observed else "unrecognized header"
        if version != expected and not version.startswith(expected + "."):
            return f"Requested {family} version {expected}, but the materialized file header is {version}."
    return None


def inspect_scientific_asset_path(path: str | Path, *, compute_hash: bool = False) -> dict[str, Any]:
    """Classify one local asset using suffix plus bounded content signatures."""
    candidate = Path(path).expanduser().resolve()
    result: dict[str, Any] = {
        "path": str(candidate),
        "exists": candidate.exists(),
        "readable": False,
        "format": "unknown",
        "asset_kind": "unknown",
        "data_model_kind": "unknown",
        "adapter": "generic_binary",
        "quality_gates": [],
    }
    if not candidate.exists():
        result["quality_gates"].append({"name": "asset_exists", "status": "fail", "evidence": str(candidate)})
        return result
    result["quality_gates"].append({"name": "asset_exists", "status": "pass", "evidence": str(candidate)})
    if candidate.is_dir():
        result.update(_directory_spec(candidate))
        try:
            entries = sum(1 for _ in candidate.iterdir())
            result.update({"readable": True, "entry_count": entries})
            result["quality_gates"].append({"name": "directory_readable", "status": "pass", "evidence": entries})
        except OSError as exc:
            result["quality_gates"].append({"name": "directory_readable", "status": "fail", "evidence": str(exc)})
        return result
    if not candidate.is_file():
        result["quality_gates"].append({"name": "regular_file", "status": "fail", "evidence": str(candidate)})
        return result
    try:
        size = candidate.stat().st_size
        with candidate.open("rb") as handle:
            head = handle.read(512)
            if size > 512:
                handle.seek(max(0, size - 512))
                tail = handle.read(512)
            else:
                tail = head
    except OSError as exc:
        result["quality_gates"].append({"name": "file_readable", "status": "fail", "evidence": str(exc)})
        return result

    suffix = candidate.suffix.lower()
    suffix_spec = FORMAT_BY_SUFFIX.get(suffix)
    signature_spec = _signature_spec(head, tail)
    container_compatible = bool(
        suffix_spec
        and signature_spec
        and (
            (
                signature_spec["format"] == "hdf5"
                and suffix_spec["format"] in {"netcdf", "matlab"}
            )
            or (
                signature_spec["format"] == "archive"
                and suffix_spec["format"] == "numpy"
                and suffix == ".npz"
            )
        )
    )
    selected = suffix_spec if container_compatible else (signature_spec or suffix_spec)
    if selected:
        result.update({key: selected[key] for key in ("format", "asset_kind", "data_model_kind", "adapter")})
        if selected.get("safe_to_deserialize") is False:
            result["safe_to_deserialize"] = False
    result.update({
        "readable": True,
        "size_bytes": size,
        "suffix": suffix,
        "mime_type": mimetypes.guess_type(candidate.name)[0],
        "classification_confidence": (
            "suffix_and_container_signature"
            if container_compatible
            else ("content_signature" if signature_spec else ("suffix" if suffix_spec else "unknown"))
        ),
    })
    result["quality_gates"].extend([
        {"name": "file_readable", "status": "pass", "evidence": str(candidate)},
        {"name": "file_nonempty", "status": "pass" if size > 0 else "fail", "evidence": size},
        {
            "name": "scientific_format_recognized",
            "status": "pass" if selected else "fail",
            "evidence": result["format"],
        },
    ])
    if suffix_spec and signature_spec and suffix_spec["format"] != signature_spec["format"] and not container_compatible:
        result["quality_gates"].append({
            "name": "suffix_matches_content",
            "status": "fail",
            "evidence": {"suffix_format": suffix_spec["format"], "content_format": signature_spec["format"]},
        })
    if result["adapter"] == "tabular" and size > 0:
        gates, metadata = _tabular_checks(candidate, "\t" if suffix == ".tsv" else ",")
        result["quality_gates"].extend(gates)
        result["metadata"] = metadata
    elif result["adapter"] == "json" and size > 0 and suffix == ".json":
        try:
            parsed = json.loads(candidate.read_text(encoding="utf-8"))
            result["metadata"] = {
                "json_type": type(parsed).__name__,
                "keys": list(parsed)[:100] if isinstance(parsed, dict) else [],
            }
            result["quality_gates"].append({"name": "json_parse", "status": "pass", "evidence": result["metadata"]["json_type"]})
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            result["quality_gates"].append({"name": "json_parse", "status": "fail", "evidence": f"{type(exc).__name__}: {exc}"})
    elif result["adapter"] == "unsafe_serialized_object":
        result["quality_gates"].append({
            "name": "safe_deserialization",
            "status": "fail",
            "evidence": "Object serialization may execute code; convert in an isolated trusted environment before parsing.",
        })
    if compute_hash and size > 0:
        result["sha256"] = _sha256(candidate)
    return result


def remember_downloaded_scientific_asset(state: State, result: Any) -> None:
    """Persist all approved downloaded assets, not only geometry files."""
    if not isinstance(result, dict):
        return
    candidates: list[dict[str, Any]] = []
    if result.get("status") == "success" and result.get("saved_path"):
        candidates.append(result)
    for key in ("auto_downloaded_asset", "auto_downloaded_geometry", "download_result"):
        value = result.get(key)
        if isinstance(value, dict):
            candidates.append(value)
    for value in result.values():
        if isinstance(value, dict):
            for key in ("auto_downloaded_asset", "auto_downloaded_geometry", "download_result"):
                nested = value.get(key)
                if isinstance(nested, dict):
                    candidates.append(nested)

    hook_state = getattr(state, "hook_state", None)
    if not isinstance(hook_state, dict):
        return
    assets = hook_state.setdefault("_data_downloaded_scientific_assets", [])
    if not isinstance(assets, list):
        return
    for item in candidates:
        path_text = str(
            item.get("preferred_asset_file")
            or item.get("saved_path")
            or item.get("path")
            or item.get("local_path")
            or ""
        ).strip()
        if not path_text:
            continue
        candidate = Path(path_text).expanduser()
        if not candidate.exists():
            continue
        profile = inspect_scientific_asset_path(candidate)
        record = {
            "path": str(candidate.resolve()),
            "url": item.get("url") or (item.get("source_result") or {}).get("url"),
            "sha256": item.get("sha256"),
            "format": profile.get("format"),
            "asset_kind": profile.get("asset_kind"),
            "data_model_kind": profile.get("data_model_kind"),
            "source": "approved_public_download",
        }
        if not any(str(existing.get("path")) == record["path"] for existing in assets if isinstance(existing, dict)):
            assets.append(record)


def downloaded_dataset_candidates(state: State) -> list[dict[str, Any]]:
    hook_state = getattr(state, "hook_state", None)
    if not isinstance(hook_state, dict):
        return []
    return [
        dict(item)
        for item in hook_state.get("_data_downloaded_scientific_assets") or []
        if isinstance(item, dict) and item.get("asset_kind") == "dataset" and Path(str(item.get("path") or "")).is_file()
    ]

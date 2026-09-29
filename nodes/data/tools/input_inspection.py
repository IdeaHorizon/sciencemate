"""Low-risk input discovery available before preprocessing-plan approval."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from core.state import State
from nodes.data.progress import emit_progress

from .scientific_assets import inspect_scientific_asset_path

_TEXT_SUFFIXES = {
    # Generic text and structured data.
    ".cfg", ".csv", ".dat", ".ini", ".json", ".md", ".toml", ".tsv",
    ".txt", ".xml", ".yaml", ".yml",
    # Electronic structure, quantum chemistry, and atomistic simulation.
    ".cell", ".cif", ".com", ".gjf", ".in", ".inp", ".lammps", ".mol",
    ".pdb", ".poscar", ".psf", ".pw", ".vasp", ".xyz",
    # Mesh, CFD, finite element, heat transfer, and multiphysics text inputs.
    ".bdf", ".cas", ".foam", ".geo", ".jou", ".key", ".msh", ".nas",
    ".sif", ".su2", ".unv",
    # Electromagnetics, chemistry mechanisms, controls, and scripts.
    ".cir", ".ctl", ".fdf", ".jsonnet", ".mechanism", ".net", ".py",
    ".sh", ".yaml.in",
}

_TEXT_BASENAMES = {
    "controlDict", "INCAR", "KPOINTS", "POSCAR", "POTCAR", "README",
    "config", "fvSchemes", "fvSolution",
}


def _resolve(state: State, path: str) -> Path:
    """Resolve explicit inputs using the tool workspace and recorded anchors.

    Absolute caller paths may be outside the worktree. Relative paths never
    depend on the worker's process cwd.
    """
    from core import paths
    from core.project_workspace import working_directory

    candidate = Path(path).expanduser()
    if candidate.is_absolute():
        return candidate.resolve()
    anchors = [working_directory(state), *paths.display_anchors(state)]
    for anchor in anchors:
        resolved = (Path(anchor) / candidate).resolve()
        if resolved.exists():
            return resolved
    return (Path(anchors[0]) / candidate).resolve()


def _alternate_existing_path(root: Path) -> Path | None:
    """Recover an omitted directory only when the nearby exact name is unique.

    Never climb to an ancestor or scan a home/root directory. Bound discovery
    itself (not just its results), and refuse ambiguous or incomplete scans.
    """
    parent = root.parent
    if not parent.is_dir() or parent == Path.home() or len(parent.parts) < 3:
        return None
    matches: set[Path] = set()
    visited = 0
    for directory, dirs, files in os.walk(parent, followlinks=False):
        visited += len(dirs) + len(files)
        if visited > 1000:
            return None
        folder = Path(directory)
        if root.name in files or root.name in dirs:
            candidate = (folder / root.name).resolve()
            if candidate.is_relative_to(parent.resolve()):
                matches.add(candidate)
        dirs[:] = [name for name in dirs if not name.startswith(".")
                   and not (folder / name).is_symlink()]
        if len(folder.relative_to(parent).parts) >= 2:
            dirs.clear()
    return next(iter(matches)) if len(matches) == 1 else None


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(parsed, maximum))


def _preview(path: Path, max_preview_bytes: int) -> dict[str, Any]:
    raw = path.read_bytes()[:max_preview_bytes]
    text = raw.decode("utf-8", errors="replace")
    result: dict[str, Any] = {
        "preview": text,
        "preview_truncated": path.stat().st_size > len(raw),
    }
    if path.suffix.lower() == ".json":
        try:
            parsed = json.loads(path.read_text(encoding="utf-8"))
            result["json_type"] = type(parsed).__name__
            if isinstance(parsed, dict):
                result["json_keys"] = list(parsed)[:100]
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            result["json_error"] = f"{type(exc).__name__}: {exc}"
    return result


async def inspect_input_path(
    state: State,
    path: str,
    recursive: bool = True,
    max_depth: int = 3,
    max_files: int = 40,
    include_previews: bool = True,
    max_preview_bytes: int = 2_048,
    **_: Any,
) -> dict[str, Any]:
    """List a path and optionally preview bounded text files without executing code."""
    root = _resolve(state, path)
    emit_progress(state, "inspect_input", str(root))
    if not root.exists():
        alternate = _alternate_existing_path(root)
        if alternate is None:
            emit_progress(state, "inspect_input_missing", str(root))
            return {"status": "error", "error": f"Input path does not exist: {root}"}
        state.append_transcript(
            "input_path_alias_resolved",
            requested_path=str(root),
            resolved_path=str(alternate),
        )
        emit_progress(state, "inspect_input_alias", str(alternate), requested=str(root))
        root = alternate

    max_depth = _bounded_int(max_depth, 3, 0, 10)
    max_files = _bounded_int(max_files, 40, 1, 1000)
    max_preview_bytes = _bounded_int(max_preview_bytes, 2_048, 256, 65_536)

    if root.is_file():
        candidates = [root]
    else:
        iterator = root.rglob("*") if recursive else root.glob("*")
        candidates = []
        for candidate in sorted(iterator):
            try:
                is_file = candidate.is_file()
            except OSError as exc:
                state.append_transcript(
                    "input_path_entry_skipped",
                    path=str(candidate),
                    error=f"{type(exc).__name__}: {exc}",
                )
                continue
            if not is_file:
                continue
            if len(candidate.relative_to(root).parts) > max_depth:
                continue
            candidates.append(candidate)
            if len(candidates) >= max_files:
                break

    files: list[dict[str, Any]] = []
    for candidate in candidates:
        stat = candidate.stat()
        entry: dict[str, Any] = {
            "path": str(candidate),
            "relative_path": candidate.name if root.is_file() else str(candidate.relative_to(root)),
            "size_bytes": stat.st_size,
            "suffix": candidate.suffix.lower(),
        }
        asset_profile = inspect_scientific_asset_path(candidate)
        entry["scientific_asset"] = {
            key: asset_profile.get(key)
            for key in (
                "format", "asset_kind", "data_model_kind", "adapter",
                "classification_confidence", "quality_gates",
            )
        }
        if include_previews and (
            candidate.suffix.lower() in _TEXT_SUFFIXES or candidate.name in _TEXT_BASENAMES
        ):
            try:
                entry.update(_preview(candidate, max_preview_bytes))
            except OSError as exc:
                entry["preview_error"] = f"{type(exc).__name__}: {exc}"
        files.append(entry)

    result = {
        "status": "success",
        "path": str(root),
        "requested_path": str(_resolve(state, path)),
        "path_type": "file" if root.is_file() else "directory",
        "file_count": len(files),
        "truncated": root.is_dir() and len(files) >= max_files,
        "files": files,
        "planning_gate_required": False,
        "scientific_asset": inspect_scientific_asset_path(root),
    }

    hook_state = getattr(state, "hook_state", None)
    if isinstance(hook_state, dict):
        inspections = hook_state.setdefault("data_input_inspections", [])
        if isinstance(inspections, list):
            inspections.append({
                "path": result["path"],
                "requested_path": result["requested_path"],
                "path_type": result["path_type"],
                "files": files,
            })
            del inspections[:-8]

    state.append_transcript(
        "input_path_inspected",
        path=str(root),
        path_type="file" if root.is_file() else "directory",
        file_count=len(files),
    )
    emit_progress(
        state,
        "inspect_input_done",
        str(root),
        file_count=len(files),
        path_type="file" if root.is_file() else "directory",
    )
    return result

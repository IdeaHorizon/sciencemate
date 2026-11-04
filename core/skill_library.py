"""Imported skill bundles: immutable revisions, personal libraries, project assignments.

The UI and workers use these same files. Importing never enables a skill in a
project. Old revisions remain available for pinned projects and resumed runs.
"""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

import yaml

from core.paths import projects_root
from core.skill_loader import load_skill_from_folder
from shared.lib.filelock import exclusive
from shared.lib.private_files import mkdir_private

MAX_FILES = 256
MAX_BUNDLE_BYTES = 8 * 1024 * 1024
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_SKILL_BYTES = 256 * 1024
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
_REVISION = re.compile(r"[0-9a-f]{64}")


class SkillLibraryError(ValueError):
    """A stable error code; the API supplies the message in the screen language."""


def _identifier(value: str, pattern=_ID) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise SkillLibraryError("invalid_identifier")
    return value


def library_root(projects_home: Path | None = None) -> Path:
    return (projects_home or projects_root()).parent / "skill-library"


def _personal(owner_id: str, projects_home: Path | None = None) -> Path:
    return library_root(projects_home) / _identifier(owner_id)


def bundle_path(owner_id: str, name: str, revision: str, projects_home: Path | None = None) -> Path:
    return (
        _personal(owner_id, projects_home)
        / _identifier(name, _NAME)
        / _identifier(revision, _REVISION)
    )


def _read(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SkillLibraryError("unreadable_configuration") from exc


def _write(path: Path, data: Any) -> None:
    mkdir_private(path.parent)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, delete=False, prefix=".skills-"
    ) as stream:
        scratch = Path(stream.name)
        try:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        except BaseException:
            scratch.unlink(missing_ok=True)
            raise
    try:
        scratch.replace(path)
    finally:
        scratch.unlink(missing_ok=True)


def safe_asset_path(value: str) -> str:
    path = PurePosixPath(value)
    reserved = {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }
    if (
        not value
        or len(value) > 240
        or "\\" in value
        or ":" in value
        or any(ord(c) < 32 or c in '<>"|?*' for c in value)
        or path.is_absolute()
        or any(p in ("", ".", "..") for p in value.split("/"))
        or any(
            p.startswith(".")
            or p == "__MACOSX"
            or p.endswith((".", " "))
            or p.split(".")[0].upper() in reserved
            for p in path.parts
        )
    ):
        raise SkillLibraryError("unsafe_path")
    return path.as_posix()


def bundle_digest(files: dict[str, bytes]) -> str:
    digest = hashlib.sha256()
    for name, content in sorted(files.items()):
        digest.update(name.encode("utf-8") + b"\x00")
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def validate_bundle(files: dict[str, bytes]) -> dict:
    if (
        not files
        or len(files) > MAX_FILES
        or sum(map(len, files.values())) > MAX_BUNDLE_BYTES
        or any(len(content) > MAX_FILE_BYTES for content in files.values())
    ):
        raise SkillLibraryError("bundle_too_large")
    for name in files:
        safe_asset_path(name)
    raw = files.get("SKILL.md", b"")
    if not raw or len(raw) > MAX_SKILL_BYTES:
        raise SkillLibraryError("invalid_skill")
    try:
        text = raw.decode("utf-8-sig")
        match = re.match(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*\r?\n(.*)\Z", text, re.S)
        if match is None:
            raise SkillLibraryError("invalid_skill")
        frontmatter = yaml.safe_load(match[1])
        if not isinstance(frontmatter, dict) or not match[2].strip():
            raise SkillLibraryError("invalid_skill")
        name = _identifier(frontmatter.get("name"), _NAME)
        safe_asset_path(name)
        description = frontmatter.get("description")
        if not isinstance(description, str) or not description.strip():
            raise SkillLibraryError("invalid_skill")
        tools = frontmatter.get("tools_used") or []
        if isinstance(tools, str):
            tools = [tools]
        if not isinstance(tools, list) or not all(isinstance(t, str) for t in tools):
            raise SkillLibraryError("invalid_skill")
    except (UnicodeError, yaml.YAMLError, TypeError) as exc:
        raise SkillLibraryError("invalid_skill") from exc
    return {
        "name": name,
        "description": description.strip(),
        "tools_used": tools,
        "revision": bundle_digest(files),
        "files": sorted(files),
    }


def list_library(owner_id: str, *, projects_home: Path | None = None) -> list[dict]:
    root = _personal(owner_id, projects_home)
    return [
        record
        for path in sorted(root.glob("*/record.json"))
        if isinstance(record := _read(path, None), dict)
    ]


def read_record(owner_id: str, name: str, *, projects_home: Path | None = None) -> dict:
    record = _read(
        _personal(owner_id, projects_home) / _identifier(name, _NAME) / "record.json", None
    )
    if not isinstance(record, dict):
        raise SkillLibraryError("skill_not_found")
    return record


def import_bundle(
    owner_id: str, files: dict[str, bytes], *, projects_home: Path | None = None
) -> dict:
    metadata = validate_bundle(files)
    root = _personal(owner_id, projects_home)
    mkdir_private(root)
    with exclusive(root / ".lock"):
        name, revision = metadata["name"], metadata["revision"]
        target = bundle_path(owner_id, name, revision, projects_home)
        if not target.exists():
            mkdir_private(target.parent)
            with tempfile.TemporaryDirectory(dir=target.parent, prefix=".import-") as temp:
                staged = Path(temp) / "bundle"
                mkdir_private(staged)
                for rel, content in files.items():
                    dest = staged / rel
                    mkdir_private(dest.parent)
                    dest.write_bytes(content)
                staged.rename(target)
        path = root / name / "record.json"
        previous = _read(path, {})
        record = {
            **metadata,
            "owner_id": owner_id,
            "enabled": previous.get("enabled", True),
            "revisions": sorted(set(previous.get("revisions", [])) | {revision}),
            "revision_details": {**previous.get("revision_details", {}), revision: metadata},
        }
        _write(path, record)
        return record


def set_library_enabled(
    owner_id: str, name: str, enabled: bool, *, projects_home: Path | None = None
) -> dict:
    root = _personal(owner_id, projects_home)
    with exclusive(root / ".lock"):
        record = read_record(owner_id, name, projects_home=projects_home)
        record["enabled"] = enabled
        _write(root / name / "record.json", record)
        return record


def read_bundle(
    owner_id: str, name: str, revision: str, *, projects_home: Path | None = None
) -> dict[str, bytes]:
    root = bundle_path(owner_id, name, revision, projects_home)
    if not root.is_dir() or root.is_symlink():
        raise SkillLibraryError("skill_not_found")
    files: dict[str, bytes] = {}
    for path in root.rglob("*"):
        if path.is_symlink():
            raise SkillLibraryError("unsafe_path")
        if path.is_file():
            files[safe_asset_path(path.relative_to(root).as_posix())] = path.read_bytes()
    metadata = validate_bundle(files)
    if metadata["revision"] != revision or metadata["name"] != name:
        raise SkillLibraryError("revision_mismatch")
    return files


def bindings_path(project_id: str, projects_home: Path | None = None) -> Path:
    return (projects_home or projects_root()) / _identifier(project_id) / "skills.json"


def read_bindings(project_id: str, *, projects_home: Path | None = None) -> dict:
    value = _read(bindings_path(project_id, projects_home), {"bindings": []})
    if not isinstance(value, dict) or not isinstance(value.get("bindings"), list):
        raise SkillLibraryError("unreadable_configuration")
    revision = hashlib.sha256(json.dumps(value["bindings"], sort_keys=True).encode()).hexdigest()
    return {"bindings": value["bindings"], "revision": revision}


def write_bindings(
    project_id: str,
    bindings: list[dict],
    expected_revision: str,
    *,
    projects_home: Path | None = None,
) -> dict:
    path = bindings_path(project_id, projects_home)
    mkdir_private(path.parent)
    with exclusive(path.with_suffix(".lock")):
        if read_bindings(project_id, projects_home=projects_home)["revision"] != expected_revision:
            raise SkillLibraryError("configuration_changed")
        _write(path, {"bindings": bindings})
    return read_bindings(project_id, projects_home=projects_home)


def snapshot_skills(state: Any, node_type: str) -> list:
    """Freeze assignments, library availability and content at this run's first use.

    No global registry mutation: concurrent projects (or two versions of the
    same skill) cannot change one another. The receipt survives worker restarts.
    """
    if not getattr(state, "project_id", None):
        return []
    if getattr(state, "node_type", node_type) != node_type and not getattr(
        state, "node_type", ""
    ).startswith("_"):
        return []
    cache_key = "_library_skill_snapshot"
    if cache_key not in state.hook_state:
        path = Path(state.root) / "skill_snapshot.json"
        with exclusive(path.with_suffix(".lock")):
            snapshot = _read(path, None)
            if snapshot is None:
                config = read_bindings(str(state.project_id))
                snapshot = []
                for binding in config["bindings"]:
                    if not binding["enabled"]:
                        continue
                    if (
                        not state.node_type.startswith("_")
                        and state.node_type not in binding["nodes"]
                    ):
                        continue
                    owner, name, revision = (binding[k] for k in ("owner_id", "name", "revision"))
                    record = read_record(owner, name)
                    if not record["enabled"]:
                        continue
                    files = read_bundle(owner, name, revision)
                    # A run owns its asset bytes, too: no later edit can change
                    # what load_skill returns halfway through this run.
                    folder = Path(state.root) / "skill_assets" / name
                    for rel, content in files.items():
                        target = folder / rel
                        mkdir_private(target.parent)
                        target.write_bytes(content)
                    snapshot.append(
                        {**binding, "source_dir": folder.relative_to(state.root).as_posix()}
                    )
                _write(path, snapshot)
                state.append_transcript(
                    "skill_snapshot",
                    revision=config["revision"],
                    skills=[{k: b[k] for k in ("name", "revision", "nodes")} for b in snapshot],
                )
            state.hook_state[cache_key] = snapshot
    out = []
    for binding in state.hook_state[cache_key]:
        if node_type not in binding["nodes"]:
            continue
        folder = Path(state.root) / safe_asset_path(binding["source_dir"])
        skill = load_skill_from_folder(folder, origin="library")
        if skill is None:
            raise SkillLibraryError("skill_not_found")
        skill.assets = sorted(
            p.relative_to(folder).as_posix()
            for p in folder.rglob("*")
            if p.is_file() and p.name != "SKILL.md"
        )
        skill.revision = binding["revision"]
        out.append(skill)
    return out

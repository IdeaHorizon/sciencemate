"""UI adapter to the harness-owned skill library; no edition-specific imports."""

from __future__ import annotations

import io
import stat
import zipfile
from pathlib import PurePosixPath

import yaml

from app.config import the_projects_home
from app.services.harness_imports import ensure_harness_importable, harness_module, harness_root


def library():
    return harness_module("core.skill_library")


def nodes() -> list[dict]:
    ensure_harness_importable()
    from core.loader import _with_always_on_tools, list_harnesses, node_dir

    result = []
    for name in list_harnesses():
        raw = yaml.safe_load((node_dir(name) / "harness.yaml").read_text(encoding="utf-8"))
        result.append(
            {
                "id": name,
                "tools": _with_always_on_tools(raw.get("tools") or [], name, raw.get("skills")),
            }
        )
    return result


def _known_names() -> set[str]:
    from core.skill_loader import load_skill_from_folder

    root = harness_root()
    folders = [
        *(root / "shared" / "skills").glob("*/SKILL.md"),
        *(root / "nodes").glob("*/skills/*/SKILL.md"),
    ]
    return {
        skill.name for path in folders if (skill := load_skill_from_folder(path.parent)) is not None
    }


def normalize_upload(files: dict[str, bytes]) -> dict[str, bytes]:
    lib = library()
    files = {
        name: data
        for name, data in files.items()
        if "__MACOSX" not in name.split("/") and PurePosixPath(name).name != ".DS_Store"
    }
    for name in files:
        lib.safe_asset_path(name)
    roots = [PurePosixPath(name).parent for name in files if PurePosixPath(name).name == "SKILL.md"]
    if len(roots) != 1:
        raise lib.SkillLibraryError("one_skill_required")
    normalized = {}
    seen = set()
    for name, content in files.items():
        try:
            relative = PurePosixPath(name).relative_to(roots[0]).as_posix()
        except ValueError as exc:
            raise lib.SkillLibraryError("one_skill_required") from exc
        lib.safe_asset_path(relative)
        if relative.casefold() in seen:
            raise lib.SkillLibraryError("unsafe_path")
        seen.add(relative.casefold())
        normalized[relative] = content
    for name in normalized:
        if any(
            parent.as_posix().casefold() in seen
            for parent in PurePosixPath(name).parents
            if parent.as_posix() != "."
        ):
            raise lib.SkillLibraryError("unsafe_path")
    return normalized


def unpack_zip(content: bytes) -> dict[str, bytes]:
    lib = library()
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            infos = archive.infolist()
            if len(infos) > lib.MAX_FILES * 2:
                raise lib.SkillLibraryError("bundle_too_large")
            files = {}
            total = 0
            seen = set()
            for info in infos:
                name = info.filename.rstrip("/")
                if "__MACOSX" in name.split("/") or PurePosixPath(name).name == ".DS_Store":
                    continue
                lib.safe_asset_path(name)
                if stat.S_ISLNK(info.external_attr >> 16) or info.flag_bits & 1:
                    raise lib.SkillLibraryError("unsafe_path")
                if info.is_dir():
                    continue
                if name.casefold() in seen:
                    raise lib.SkillLibraryError("unsafe_path")
                seen.add(name.casefold())
                total += info.file_size
                if info.file_size > lib.MAX_FILE_BYTES or total > lib.MAX_BUNDLE_BYTES:
                    raise lib.SkillLibraryError("bundle_too_large")
                with archive.open(info) as stream:
                    files[name] = stream.read(lib.MAX_FILE_BYTES + 1)
            return normalize_upload(files)
    except (zipfile.BadZipFile, RuntimeError, EOFError, NotImplementedError) as exc:
        raise lib.SkillLibraryError("invalid_archive") from exc


def import_files(owner_id: str, files: dict[str, bytes]) -> dict:
    lib = library()
    normalized = normalize_upload(files)
    metadata = lib.validate_bundle(normalized)
    if metadata["name"] in _known_names():
        raise lib.SkillLibraryError("reserved_name")
    return lib.import_bundle(owner_id, normalized, projects_home=the_projects_home())


def preview(
    owner_id: str, name: str, revision: str | None = None, asset: str | None = None
) -> dict:
    lib = library()
    home = the_projects_home()
    record = lib.read_record(owner_id, name, projects_home=home)
    revision = revision or record["revision"]
    files = lib.read_bundle(owner_id, name, revision, projects_home=home)
    metadata = lib.validate_bundle(files)
    if asset:
        lib.safe_asset_path(asset)
        if asset not in files:
            raise lib.SkillLibraryError("skill_not_found")
        raw = files[asset]
    else:
        raw = files["SKILL.md"]
    try:
        # Validate the full file first; a preview limit may split a UTF-8
        # character without making a text attachment a binary file.
        raw.decode("utf-8-sig")
        content = raw[: 128 * 1024].decode("utf-8-sig", errors="ignore")
        binary = "\x00" in content
    except UnicodeError:
        content, binary = "", True
    return {
        **record,
        **metadata,
        "content": "" if binary else content,
        "asset": asset or "SKILL.md",
        "binary": binary,
        "truncated": len(raw) > 128 * 1024,
    }


def project_skills(project_id: str, owner_id: str) -> dict:
    lib, home = library(), the_projects_home()
    config = lib.read_bindings(project_id, projects_home=home)
    available = {(owner_id, r["name"]): r for r in lib.list_library(owner_id, projects_home=home)}
    for binding in config["bindings"]:
        key = (binding["owner_id"], binding["name"])
        if key not in available:
            record = lib.read_record(*key, projects_home=home)
            granted = {
                b["revision"] for b in config["bindings"] if (b["owner_id"], b["name"]) == key
            }
            # Sharing a revision with a project does not share the author's
            # private future revisions with its other members.
            metadata = record["revision_details"][binding["revision"]]
            available[key] = {
                **record,
                **metadata,
                "revisions": sorted(granted),
                "revision_details": {rev: record["revision_details"][rev] for rev in granted},
            }
    return {**config, "skills": list(available.values()), "nodes": nodes()}


def save_project_skills(
    project_id: str, owner_id: str, bindings: list[dict], revision: str
) -> dict:
    lib, home = library(), the_projects_home()
    old = lib.read_bindings(project_id, projects_home=home)
    authorized = {(b["owner_id"], b["name"], b["revision"]) for b in old["bindings"]}
    known_nodes = {node["id"] for node in nodes()}
    seen = set()
    for binding in bindings:
        key = (binding["owner_id"], binding["name"], binding["revision"])
        if key[0] != owner_id and key not in authorized:
            raise lib.SkillLibraryError("skill_not_found")
        record = lib.read_record(*key[:2], projects_home=home)
        if binding["name"] in seen:
            raise lib.SkillLibraryError("duplicate_skill")
        seen.add(binding["name"])
        if (
            not binding["nodes"]
            or not set(binding["nodes"]).issubset(known_nodes)
            or len(binding["nodes"]) != len(set(binding["nodes"]))
        ):
            raise lib.SkillLibraryError("invalid_nodes")
        if not record["enabled"] and binding["enabled"]:
            # A disabled library may still be kept, or disabled/removed, in a project.
            if binding not in old["bindings"]:
                raise lib.SkillLibraryError("skill_disabled")
        lib.read_bundle(*key, projects_home=home)
    lib.write_bindings(project_id, bindings, revision, projects_home=home)
    return project_skills(project_id, owner_id)

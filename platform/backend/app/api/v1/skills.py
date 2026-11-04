"""Personal skill management and project/node assignments, in both editions."""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.auth import get_current_user
from app.config import the_projects_home
from app.database import get_db
from app.models.user import User
from app.services import skills
from app.services.sessions import require_capability
from app.services.user_interface import Language, language_on_screen, say

router = APIRouter()

_ERRORS = {
    "invalid_identifier": {
        "zh": "技能名称或标识无效。",
        "en": "The skill name or identifier is invalid.",
    },
    "invalid_skill": {
        "zh": "SKILL.md 需要有效的名称、用途说明和正文。",
        "en": "SKILL.md needs a valid name, description and body.",
    },
    "bundle_too_large": {
        "zh": "技能最多包含 256 个文件、共 8 MB；单个文件不超过 2 MB，SKILL.md 不超过 256 KB。",
        "en": "A skill can contain up to 256 files and 8 MB total; "
        "each file must be under 2 MB and SKILL.md under 256 KB.",
    },
    "unsafe_path": {
        "zh": "技能中存在不安全、重复或不兼容的文件路径。",
        "en": "The skill contains unsafe, duplicate or incompatible file paths.",
    },
    "one_skill_required": {
        "zh": "每次导入一个技能文件夹，其中应包含一份 SKILL.md。",
        "en": "Import one skill folder containing exactly one SKILL.md.",
    },
    "invalid_archive": {
        "zh": "无法读取 ZIP，请检查文件是否完整且未加密。",
        "en": "The ZIP could not be read. Check that it is complete and unencrypted.",
    },
    "reserved_name": {
        "zh": "名称与平台内置技能重复，请修改名称后导入。",
        "en": "This name is used by a built-in skill. Rename the imported skill.",
    },
    "skill_not_found": {
        "zh": "找不到这份技能或所选版本。",
        "en": "This skill or selected revision could not be found.",
    },
    "revision_mismatch": {
        "zh": "技能文件与保存的版本不一致，请重新导入。",
        "en": "The skill files no longer match the saved revision. Import them again.",
    },
    "configuration_changed": {
        "zh": "技能配置已被修改，请刷新后重试。",
        "en": "The skill configuration has changed. Refresh and try again.",
    },
    "unreadable_configuration": {
        "zh": "无法读取保存的技能配置。",
        "en": "The saved skill configuration could not be read.",
    },
    "duplicate_skill": {
        "zh": "一个项目中每个技能只能配置一次。",
        "en": "Configure each skill only once per project.",
    },
    "invalid_nodes": {
        "zh": "请为技能选择至少一个有效的研究节点。",
        "en": "Select at least one available research node for each skill.",
    },
    "skill_disabled": {
        "zh": "该技能在技能库中已停用，请先启用后再分配。",
        "en": "This skill is disabled in the library. Enable it before assigning it.",
    },
}


async def _call(fn, *args, lang: str, **kwargs):
    try:
        return await run_in_threadpool(fn, *args, **kwargs)
    except skills.library().SkillLibraryError as exc:
        code = str(exc)
        status = (
            404 if code == "skill_not_found" else 409 if code == "configuration_changed" else 400
        )
        raise HTTPException(
            status_code=status,
            detail={
                "code": code,
                "message": say(
                    _ERRORS.get(
                        code,
                        {
                            "zh": "技能操作未能完成。",
                            "en": "The skill operation could not be completed.",
                        },
                    ),
                    lang,
                ),
            },
        ) from exc


class Availability(BaseModel):
    enabled: bool


class Binding(BaseModel):
    owner_id: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=64)
    revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    nodes: list[str] = Field(min_length=1, max_length=32)
    enabled: bool = True


class ProjectSkillsWrite(BaseModel):
    revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    bindings: list[Binding] = Field(max_length=100)


@router.get("/settings/skills")
async def list_skills(
    user: User = Depends(get_current_user), lang: Language = Depends(language_on_screen)
) -> dict:
    records = await _call(
        skills.library().list_library, str(user.id), projects_home=the_projects_home(), lang=lang
    )
    return {"skills": records, "nodes": await _call(skills.nodes, lang=lang)}


async def _import(files: list[UploadFile], user: User, lang: str) -> dict:
    lib = skills.library()
    try:
        if len(files) > lib.MAX_FILES:
            raise lib.SkillLibraryError("bundle_too_large")
        contents, total = {}, 0
        for file in files:
            content = await file.read(lib.MAX_BUNDLE_BYTES + 1)
            total += len(content)
            if total > lib.MAX_BUNDLE_BYTES:
                raise lib.SkillLibraryError("bundle_too_large")
            name = file.filename or ""
            if name in contents:
                raise lib.SkillLibraryError("unsafe_path")
            contents[name] = content
        if len(contents) == 1 and next(iter(contents)).lower().endswith(".zip"):
            contents = await _call(skills.unpack_zip, next(iter(contents.values())), lang=lang)
        return await _call(skills.import_files, str(user.id), contents, lang=lang)
    except lib.SkillLibraryError as exc:
        code = str(exc)
        raise HTTPException(
            status_code=400, detail={"code": code, "message": say(_ERRORS[code], lang)}
        ) from exc
    finally:
        for file in files:
            await file.close()


@router.post("/settings/skills/import", status_code=201)
async def import_skill(
    files: list[UploadFile] = File(...),
    user: User = Depends(get_current_user),
    lang: Language = Depends(language_on_screen),
) -> dict:
    return await _import(files, user, lang)


@router.get("/settings/skills/{name}")
async def preview_skill(
    name: str,
    revision: str | None = None,
    asset: str | None = None,
    user: User = Depends(get_current_user),
    lang: Language = Depends(language_on_screen),
) -> dict:
    return await _call(skills.preview, str(user.id), name, revision, asset, lang=lang)


@router.patch("/settings/skills/{name}")
async def set_availability(
    name: str,
    data: Availability,
    user: User = Depends(get_current_user),
    lang: Language = Depends(language_on_screen),
) -> dict:
    return await _call(
        skills.library().set_library_enabled,
        str(user.id),
        name,
        data.enabled,
        projects_home=the_projects_home(),
        lang=lang,
    )


@router.get("/projects/{project_id}/skills")
async def get_project_skills(
    project_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    lang: Language = Depends(language_on_screen),
) -> dict:
    project = await require_capability(db, user, project_id, "view_session")
    return await _call(skills.project_skills, str(project.id), str(user.id), lang=lang)


@router.put("/projects/{project_id}/skills")
async def put_project_skills(
    project_id: str,
    data: ProjectSkillsWrite,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    lang: Language = Depends(language_on_screen),
) -> dict:
    project = await require_capability(db, user, project_id, "manage_settings")
    return await _call(
        skills.save_project_skills,
        str(project.id),
        str(user.id),
        [b.model_dump() for b in data.bindings],
        data.revision,
        lang=lang,
    )


@router.post("/projects/{project_id}/skills/import", status_code=201)
async def import_for_project(
    project_id: str,
    files: list[UploadFile] = File(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    lang: Language = Depends(language_on_screen),
) -> dict:
    await require_capability(db, user, project_id, "manage_settings")
    return await _import(files, user, lang)


@router.get("/projects/{project_id}/skills/preview")
async def preview_for_project(
    project_id: str,
    name: str,
    owner_id: str,
    revision: str,
    asset: str | None = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    lang: Language = Depends(language_on_screen),
) -> dict:
    project = await require_capability(db, user, project_id, "view_session")
    config = await _call(
        skills.library().read_bindings,
        str(project.id),
        projects_home=the_projects_home(),
        lang=lang,
    )
    if owner_id != str(user.id) and not any(
        b["owner_id"] == owner_id and b["name"] == name and b["revision"] == revision
        for b in config["bindings"]
    ):
        raise HTTPException(status_code=404, detail=say(_ERRORS["skill_not_found"], lang))
    result = await _call(skills.preview, owner_id, name, revision, asset, lang=lang)
    if owner_id != str(user.id):
        result["revisions"] = [revision]
        result["revision_details"] = {revision: result["revision_details"][revision]}
    return result

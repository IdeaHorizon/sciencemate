"""Personal Research Settings persistence, snapshots, and prompt context."""

import hashlib
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.research_settings import UserResearchSettings
from app.models.user import User
from app.schemas.research_settings import ResearchSettingsWrite

DEFAULT_RESPONSE_LANGUAGE = "auto"
DEFAULT_CITATION_STYLE = "author_year"
DEFAULT_EVIDENCE_STANDARD = "balanced"
INSTRUCTION_SCOPES = {"all", "literature", "experiments", "writing", "review"}


async def get_research_settings_record(
    db: AsyncSession, *, user_id: str, for_update: bool = False
) -> UserResearchSettings | None:
    query = select(UserResearchSettings).where(
        UserResearchSettings.tenant_id == settings.runtime_tenant_id,
        UserResearchSettings.user_id == user_id,
    )
    if for_update:
        query = query.with_for_update()
    return await db.scalar(query)


def _normalized_instructions(raw: list | None) -> list[dict]:
    normalized: list[dict] = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        scope = str(item.get("scope") or "all")
        normalized.append(
            {
                "id": str(item.get("id") or uuid4()),
                "title": str(item.get("title") or "Research instruction").strip()[:120],
                "scope": scope if scope in INSTRUCTION_SCOPES else "all",
                "instruction": str(item.get("instruction") or "").strip()[:2_000],
                "enabled": bool(item.get("enabled", True)),
            }
        )
    return normalized


def _memory_enabled(user: User) -> bool:
    preferences = user.preferences if isinstance(user.preferences, dict) else {}
    research = preferences.get("research")
    if not isinstance(research, dict):
        return True
    value = research.get("memory_enabled", True)
    return value if isinstance(value, bool) else True


def _effective_layers(
    user: User,
    instructions: list[dict],
    *,
    memory_enabled: bool,
) -> list[dict]:
    """助手实际会读到的指令层 —— 这里只答「个人」那一层。

    harness 读的指令只有两层（`core/directives_loader.py`）：个人层（`PROFILE.md` +
    平台渲染的 `RESEARCH_SETTINGS.md`）和项目层（项目仓库里的 `PROJECT.md`，在项目设置
    里改）。机构 / 研究组那两层在 RFC X2 删表时就没了（见 `services/instructions.py`
    「组织层删掉了」）：没有文件、没有写入口、没有注入。这里以前照旧报着那两层，
    `instruction_count` 永远是 0，界面于是永远显示「未设置」，还说它们「由管理员维护」。
    """
    enabled_count = sum(1 for item in instructions if item["enabled"])
    return [
        {
            "kind": "personal",
            "name": user.display_name,
            "editable": True,
            "instruction_count": len(instructions),
            "summary": (
                f"{enabled_count} of {len(instructions)} personal instructions enabled."
                if memory_enabled
                else "Personal instruction memory is disabled for new Sessions."
            ),
        }
    ]


def research_settings_payload(user: User, record: UserResearchSettings | None) -> dict:
    instructions = _normalized_instructions(record.instructions if record else [])
    memory_enabled = _memory_enabled(user)
    return {
        "response_language": (record.response_language if record else DEFAULT_RESPONSE_LANGUAGE),
        "citation_style": record.citation_style if record else DEFAULT_CITATION_STYLE,
        "evidence_standard": (record.evidence_standard if record else DEFAULT_EVIDENCE_STANDARD),
        "memory_enabled": memory_enabled,
        "instructions": instructions,
        "effective_layers": _effective_layers(user, instructions, memory_enabled=memory_enabled),
        "updated_at": record.updated_at if record else None,
    }


async def effective_research_settings(db: AsyncSession, *, user: User) -> dict:
    record = await get_research_settings_record(db, user_id=user.id)
    # 指令的"第几版"随三张指令表一起没了（RFC X2）：指令现在是文件，版本是 git。
    return research_settings_payload(user, record)


async def replace_research_settings(
    db: AsyncSession, *, user: User, data: ResearchSettingsWrite
) -> UserResearchSettings:
    record = await get_research_settings_record(db, user_id=user.id, for_update=True)
    instructions = [
        {
            "id": item.id or str(uuid4()),
            "title": item.title.strip(),
            "scope": item.scope,
            "instruction": item.instruction.strip(),
            "enabled": item.enabled,
        }
        for item in data.instructions
    ]
    if record is None:
        record = UserResearchSettings(
            tenant_id=settings.runtime_tenant_id,
            user_id=user.id,
            version=1,
        )
        db.add(record)
    else:
        record.version += 1
    record.response_language = data.response_language
    record.citation_style = data.citation_style
    record.evidence_standard = data.evidence_standard
    record.instructions = instructions
    preferences = dict(user.preferences) if isinstance(user.preferences, dict) else {}
    research_preferences = (
        dict(preferences["research"]) if isinstance(preferences.get("research"), dict) else {}
    )
    research_preferences["memory_enabled"] = data.memory_enabled
    preferences["research"] = research_preferences
    user.preferences = preferences
    await db.flush()
    await db.refresh(record)
    return record


def research_settings_snapshot(payload: dict) -> dict:
    personal = {
        "schema_version": 1,
        "response_language": payload["response_language"],
        "citation_style": payload["citation_style"],
        "evidence_standard": payload["evidence_standard"],
        "memory_enabled": payload.get("memory_enabled", True),
        "instructions": (payload["instructions"] if payload.get("memory_enabled", True) else []),
        "settings_updated_at": (
            payload["updated_at"].isoformat()
            if isinstance(payload.get("updated_at"), datetime)
            else None
        ),
    }
    snapshot = {
        **personal,
        "snapshot_id": str(uuid4()),
        "captured_at": datetime.now(UTC).isoformat(),
    }
    snapshot["context_hash"] = hashlib.sha256(
        compile_research_context(snapshot).encode()
    ).hexdigest()
    return snapshot


def research_settings_snapshot_ref(snapshot: dict | None) -> dict | None:
    """Return the non-secret audit reference safe for ordinary project APIs."""
    if not isinstance(snapshot, dict):
        return None
    snapshot_id = snapshot.get("snapshot_id")
    context_hash = snapshot.get("context_hash")
    if not isinstance(snapshot_id, str) or not isinstance(context_hash, str):
        return None
    return {
        "snapshot_id": snapshot_id,
        "context_hash": context_hash,
    }


def compile_research_context(snapshot: dict | None) -> str:
    if not isinstance(snapshot, dict):
        return ""
    language = snapshot.get("response_language", DEFAULT_RESPONSE_LANGUAGE)
    citation = snapshot.get("citation_style", DEFAULT_CITATION_STYLE)
    evidence = snapshot.get("evidence_standard", DEFAULT_EVIDENCE_STANDARD)
    lines = [
        "## Personal Research Settings",
        f"- Response language: {language}",
        f"- Citation style: {citation}",
        f"- Evidence standard: {evidence}",
    ]
    if snapshot.get("memory_enabled", True) is False:
        return "\n".join(lines)
    enabled = [
        item
        for item in snapshot.get("instructions", [])
        if isinstance(item, dict) and item.get("enabled")
    ]
    if enabled:
        lines.extend(("", "### Active Personal Research Instructions"))
        for item in enabled:
            lines.append(
                f"- [{item.get('scope', 'all')}] {item.get('title', 'Instruction')}: "
                f"{item.get('instruction', '')}"
            )
    return "\n".join(lines)

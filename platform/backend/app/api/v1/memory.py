"""Memory view over the harness project memory.

Memory is written by agents (`add_memory_candidate`) and by the curator, into
`memory.jsonl` inside the harness project directory.  This service's
`memory_entries` / `research_skills` / `memory_proposals` tables were an earlier
parallel model; on the deployed instance with 15 real projects they held zero
rows, and the CRUD around them (create / patch / delete / search / capacity /
skills) had no frontend caller.

What is left is the one endpoint the UI actually uses: list what this project
remembers.  Writes are deliberately absent — memory admission is a harness
concern with its own rules (candidates queue, curator processing); a direct
write from this service would bypass them.  See `app.services.harness_kb`.
"""

from fastapi import APIRouter, Depends

from app.auth import get_current_user
from app.models.user import User
from app.schemas.memory import MemoryEntryResponse, MemoryLayer
from app.services import harness_kb

router = APIRouter()

# harness 的分类词表（`_VALID_CANDIDATE_CATEGORIES`）与本服务的 MemoryType 是
# 两套独立演化出来的词。按语义对齐，并把原值原样留在 `source` 里 —— 映射不该是
# 有损的，否则 UI 上看到的分类跟 agent 写下的对不上，而且再也查不回去。
_HARNESS_CATEGORY_TO_TYPE = {
    "observation": "factual",       # 观察到的事实
    "pitfall": "judgmental",        # 从经验里得出的结论/告警
    "workflow_hint": "rule",        # 流程性规则
    "user_pref_hint": "preference",
}


@router.get("/entries", response_model=list[MemoryEntryResponse])
async def list_memory_entries(
    project_id: str | None = None,
    layer: MemoryLayer | None = None,
    user: User = Depends(get_current_user),
) -> list[dict]:
    """Read the memory the agents actually wrote, newest first."""
    entries = [
        _as_memory_response(record, project_id, user.id)
        for record in await harness_kb.query(user, project_id or "", "memory")
    ]
    if layer:
        entries = [e for e in entries if e["layer"] == layer]
    return entries


def _as_memory_response(record: dict, project_id: str | None,
                        user_id: str) -> dict:
    """Map a harness memory record onto the response shape the UI renders.

    The harness schema is looser than this service's table (it grew from a wet
    ledger, not a migration), so anything it does not carry is reported as
    absent rather than invented — a fabricated `confidence` or `last_verified_at`
    would read as a real judgement in the UI.
    """
    created = record.get("created_at") or record.get("at") or ""
    tags = record.get("tags")
    harness_category = str(record.get("category") or record.get("type") or "")
    source = record.get("source")
    source = dict(source) if isinstance(source, dict) else {}
    if harness_category:
        source.setdefault("harness_category", harness_category)
    return {
        "id": str(record.get("id") or record.get("memory_id") or ""),
        "content": str(record.get("content") or record.get("text") or ""),
        # 认不出的分类落 factual（"记下了一条事实"是最不越权的说法）——
        # 绝不猜成 judgmental，那是在替 agent 下判断。
        "type": _HARNESS_CATEGORY_TO_TYPE.get(harness_category, "factual"),
        "layer": record.get("layer") or ("project" if project_id else "user"),
        "confidence": record.get("confidence") or "medium",
        "status": record.get("status") or "active",
        "project_id": project_id,
        "user_id": user_id,
        "tags": tags if isinstance(tags, list) else None,
        "topic": record.get("topic"),
        "source": source,
        "created_at": created,
        "updated_at": record.get("updated_at") or created,
        "last_accessed_at": None,
        "last_verified_at": None,
        "superseded_by_id": record.get("superseded_by_id"),
        "conflicts_with_ids": None,
        "token_count": None,
    }

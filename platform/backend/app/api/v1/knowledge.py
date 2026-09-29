"""Knowledge search over the harness KB.

This router used to own a whole `KBEntry` + `Chunk` CRUD surface — the backend's
own PDF-ingest domain, built before `platform_runtime` bridged execution onto
the harness.  All of it read and wrote tables that, on the deployed instance
with 15 real projects, held **zero rows**: agents sediment knowledge through
`create_claim` / `kb_ingest` into harness JSONL, never into this database.

The ingest half (`/library/*`, `kb_ingestion`) is retired outright — it had an
API but no UI and was never used.  What remains is the one thing users actually
need: searching what the agents learned.  See `app.services.harness_kb`.
"""

from fastapi import APIRouter, Depends

from app.auth import get_current_user
from app.models.user import User
from app.schemas.knowledge import KBSearchRequest, KBSearchResult
from app.services import harness_kb

router = APIRouter()


@router.post("/search", response_model=list[KBSearchResult])
async def search_kb(
    data: KBSearchRequest,
    user: User = Depends(get_current_user),
) -> list[dict]:
    """Search the knowledge the agents actually sedimented.

    Substring search, deliberately not semantic: the embedding index belongs to
    the harness (`core/kb_vector_index`) and is built by a local model inside the
    harness interpreter.  Reaching it from this process would mean a second
    model load or a cross-process call — a separate decision from "stop reading
    an empty table".  Ranking is therefore explainable (earliest match first)
    rather than a relevance score this service did not compute.
    """
    hits: list[dict] = []
    # claims / concepts / chunks 三类都搜 —— 用户问"知识库里有没有提到 X"，
    # 不该因为它被记成 concept 而不是 claim 就搜不到。
    for entity in ("claims", "concepts", "chunks"):
        for record in await harness_kb.query(
            user, data.project_id or "", entity,
            search=data.query, limit=data.top_k,
        ):
            hits.append(_as_search_result(entity, record))
            if len(hits) >= data.top_k:
                return hits
    return hits


def _as_search_result(entity: str, record: dict) -> dict:
    """Map one harness KB record onto the response shape the UI renders.

    `score` is a constant 1.0: this is a lexical hit, and dressing the match
    offset up as a relevance judgement would claim precision that was never
    computed.  `chunk.section` carries which entity matched so the UI can tell
    a claim from a concept from a paper chunk.
    """
    text = str(
        record.get("text")
        or record.get("claim_text")
        or record.get("description")
        or record.get("canonical_name")
        or ""
    )
    return {
        "chunk": {
            "id": str(record.get("id") or ""),
            "kb_entry_id": None,
            "artifact_id": record.get("origin_artifact_id"),
            "text": text,
            "chunk_index": 0,
            "section": entity,
            "page_number": None,
            "token_count": None,
            "citability": "medium",
        },
        "source_title": str(
            record.get("canonical_name") or record.get("source") or entity
        ),
        "source_ref": record.get("source"),
        "quality_tier": None,
        "score": 1.0,
        "artifact_id": record.get("origin_artifact_id"),
    }

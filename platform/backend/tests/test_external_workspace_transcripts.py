"""The real wrapper reader restores large workspace indexes before side effects."""
from __future__ import annotations

import json

import pytest
from core.workspace_transcript import MAX_TRANSCRIPT_RECORD_BYTES, encode_workspace_record
from sqlalchemy import select

from app.models.execution import ExecutionEvent
from app.services.execution_ingest import ExecutionIngestService
from app.services.harness_transcript_ingest import ingest_transcript_wrapper, resolve_wrapper

from .test_execution_foundation import AT, _context, _runtime_transcript, execution_db  # noqa: F401


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["workspace_changed", "workspace_checkpoint_requested"])
async def test_real_wrapper_restores_the_whole_index_and_replay_is_idempotent(
    execution_db, kind,  # noqa: F811
):
    path = _runtime_transcript("large-workspace/transcript.jsonl", [])
    paths = [f"experiments/{'目录' * 35}/{i}.py" for i in range(30000)]
    record = {
        "event": kind, "at": AT, "paths": paths, "files_changed": len(paths),
        "node_type": "experiment", "tool_name": "load_skill", "additions": 30000,
        "deletions": 0, "patch": "+small\n", "patch_truncated": True,
        "file_stats": [{"path": paths[0], "additions": 1, "deletions": 0, "status": "added"}],
    }
    line = encode_workspace_record(record, path)
    assert len(line) <= MAX_TRANSCRIPT_RECORD_BYTES
    path.write_bytes(line)
    event = {"transcript_path": str(path), "byte_start": 0, "byte_end": len(line)}
    resolved = resolve_wrapper(event, project_id="project-tenant-a", session_id="session-a")
    assert resolved[2] == len(line)
    states = {}
    service = ExecutionIngestService()
    for _ in range(2):
        native = await ingest_transcript_wrapper(
            execution_db, service=service, context=_context(), event=event, harness_states=states,
        )
        await execution_db.commit()
        # local_execution receives this exact return for its checkpoint side effect.
        assert native["paths"] == paths
        assert native["files_changed"] == 30000
    stored = list((await execution_db.scalars(select(ExecutionEvent))).all())
    if kind == "workspace_changed":
        cards = [item for item in stored if item.kind == "workspace.changed"]
        assert len(cards) == 1
        assert cards[0].payload["filesChanged"] == 30000
        assert cards[0].payload["files"][0]["path"] == paths[0]
        assert cards[0].payload["patch"] == "+small\n"


@pytest.mark.asyncio
async def test_a_missing_detail_page_fails_explicitly_before_any_projection(
    execution_db,  # noqa: F811
):
    path = _runtime_transcript("missing-workspace/transcript.jsonl", [])
    line = encode_workspace_record({
        "event": "workspace_changed", "at": AT,
        "paths": ["experiments/" + "目录" * 35 + f"/{i}" for i in range(30000)],
        "files_changed": 30000,
    }, path)
    path.write_bytes(line)
    detail = json.loads(line)["workspace_details"]
    (path.parent / detail["path"]).unlink()
    with pytest.raises(RuntimeError, match="Invalid external workspace record.*missing"):
        await ingest_transcript_wrapper(
            execution_db, service=ExecutionIngestService(), context=_context(),
            event={"transcript_path": str(path), "byte_start": 0, "byte_end": len(line)},
            harness_states={},
        )
    assert list((await execution_db.scalars(select(ExecutionEvent))).all()) == []

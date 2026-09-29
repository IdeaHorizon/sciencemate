"""User-facing projection of Harness transport records."""

from app.services.harness_progress import protocol_user_progress


def test_internal_transport_lifecycle_is_not_user_progress() -> None:
    for kind in ("started", "transcript", "child_event", "ready", "result", "error"):
        assert protocol_user_progress({"type": kind, "request_id": "request-1"}) is None


def test_internal_tool_progress_is_not_user_progress() -> None:
    for event in (
        {"type": "progress", "tool_name": "write_scratchpad"},
        {"type": "progress", "tool_name": "platform_heartbeat"},
        {"type": "progress", "tool_name": "hook_before_tool"},
        {"type": "progress", "tool_name": "internal_cache_sync"},
        {"type": "progress", "tool_name": "arxiv_search", "internal_only": True},
        {"type": "progress", "tool_name": "arxiv_search", "source": "platform"},
    ):
        assert protocol_user_progress(event) is None


def test_explicit_progress_is_human_readable_and_has_stable_identity() -> None:
    first = protocol_user_progress(
        {
            "type": "progress",
            "request_id": "request-1",
            "tool_name": "semantic_scholar_search",
            "message": "Searching papers",
        }
    )
    updated = protocol_user_progress(
        {
            "type": "progress",
            "request_id": "request-1",
            "tool_name": "semantic_scholar_search",
            "message": "Reviewing results",
        }
    )

    assert first is not None
    assert updated is not None
    assert first["id"] == updated["id"]
    assert first["event"] == "tool.progress"
    assert first["label"] == "Using Semantic scholar search"
    assert first["detail"] == "Searching papers"
    assert updated["detail"] == "Reviewing results"


def test_wait_and_pause_records_have_distinct_user_facing_events() -> None:
    waiting = protocol_user_progress(
        {
            "type": "background_wait",
            "request_id": "request-1",
            "job_id": "job-1",
            "detail": "Waiting for scheduler allocation",
        }
    )
    paused = protocol_user_progress(
        {
            "type": "pause_required",
            "request_id": "request-1",
            "pause_id": "pause-1",
            "question": "Which dataset should be authoritative?",
        }
    )

    assert waiting is not None
    assert waiting["event"] == "run.waiting_compute"
    assert waiting["detail"] == "Waiting for scheduler allocation"
    assert paused is not None
    assert paused["event"] == "run.paused"
    assert paused["label"] == "Needs input"
    assert paused["status"] == "waiting_human"

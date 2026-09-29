"""Backend Foundation coverage for durable ingest, authority, redaction, and reads."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.api.v1.execution import get_runtime_tenant_id, router
from app.auth import get_current_user
from app.config import settings
from app.database import Base, get_db
from app.models.execution import (
    EXECUTION_TABLES,
    AttemptStatus,
    Command,
    Decision,
    DecisionStatus,
    ExecutionEvent,
    Run,
    RunAttempt,
    RunStatus,
    SessionProjection,
)
from app.schemas.execution import RunUsageResponse
from app.services.execution_ingest import (
    LOCAL_RECORD_IDENTITY_PREFIX,
    DecisionAuthorityRequiredError,
    DecisionAuthoritySnapshot,
    DecisionResponseRejectedError,
    ExecutionIngestService,
    IdempotencyConflictError,
    IngestContext,
    IngestError,
    TranscriptParseError,
    UnsupportedDecisionActionSetError,
)

AT = "2026-08-03T01:02:03+00:00"


@pytest_asyncio.fixture
async def execution_db() -> AsyncSession:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: Base.metadata.create_all(
                sync_connection, tables=EXECUTION_TABLES
            )
        )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: Base.metadata.drop_all(
                sync_connection, tables=reversed(EXECUTION_TABLES)
            )
        )
    await engine.dispose()


def _context(
    *,
    tenant: str = "tenant-a",
    session: str = "session-a",
    run: str = "run-a",
    authority: DecisionAuthoritySnapshot | None = None,
    actor: str | None = None,
) -> IngestContext:
    return IngestContext(
        tenant_id=tenant,
        workspace_id=f"workspace-{tenant}",
        project_id=f"project-{tenant}",
        session_id=session,
        run_id=run,
        actor_user_id=actor,
        decision_authority=authority,
    )


def _raw_line(raw: dict) -> bytes:
    return json.dumps(raw, ensure_ascii=False, sort_keys=True).encode()


def _runtime_transcript(relative: str, records: list[dict]) -> Path:
    """把一份 transcript 写进**生产会认的**运行态根下。

    `resolve_wrapper` 只接受 `_approved_runtime_root()` 里的路径 —— 随手一个
    tmp_path 在生产会被当成越界读拒掉。测试写在别处，就等于绕开了这道边界。
    conftest 已把 `settings.platform_data_root` 钉在 tmp 下，这里派生即可。
    """
    from app.config import data_root

    path = data_root("state") / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"".join(_raw_line(record) + b"\n" for record in records))
    return path


async def ingest_transcript_like_production(
    db: AsyncSession,
    *,
    service: ExecutionIngestService,
    context: IngestContext,
    path: Path,
    harness_states: dict[str, dict],
    from_offset: int = 0,
) -> int:
    """按 worker 真实发包的样子摄取一份 transcript，返回读到的字节位置。

    **摄取只有一条路**：worker 在 socket 上逐行发包装事件，平台逐条
    `ingest_transcript_wrapper`；`replay_missed_events` 从 events.jsonl 补齐
    时喂的是同一形状的包装事件。一条路，两个驱动者。

    此前这些用例走的是 `ExecutionIngestService.ingest_transcript_file` —— 那个
    方法零生产调用方，而且它自己维护一本生产从不写的 checkpoint。断言落在
    没人走的那条路上，于是 `owningTranscript` / `dispatchKey` 两个缺陷在生产
    里活着、CI 里全绿（见 032 migration）。
    """
    from app.services.harness_transcript_ingest import ingest_transcript_wrapper

    offset = from_offset
    blob = path.read_bytes()
    for line in blob[from_offset:].splitlines(keepends=True):
        if not line.endswith(b"\n"):
            break  # 半行 —— worker 还没写完，它不会为它发包装事件
        start, offset = offset, offset + len(line)
        if not line.strip():
            continue
        await ingest_transcript_wrapper(
            db,
            service=service,
            context=context,
            event={
                "type": "transcript",
                "transcript_path": str(path),
                "byte_start": start,
                "byte_end": offset,
            },
            harness_states=harness_states,
        )
    return offset


async def _ingest(
    service: ExecutionIngestService,
    db: AsyncSession,
    context: IngestContext,
    raw: dict,
    *,
    offset: int,
    state: dict | None = None,
    file_identity: str = "fixture-file",
):
    return await service.ingest_raw_record(
        db,
        context=context,
        file_identity=file_identity,
        byte_offset=offset,
        raw_line=_raw_line(raw),
        raw=raw,
        adapter_state=state if state is not None else {},
    )


def test_every_execution_table_carries_full_identity() -> None:
    required = {"tenant_id", "workspace_id", "project_id", "session_id"}
    for table in EXECUTION_TABLES:
        assert required.issubset(table.columns.keys()), table.name


def test_runtime_tenant_is_server_owned_and_rejects_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "runtime_tenant_id", "")
    with pytest.raises(HTTPException) as error:
        get_runtime_tenant_id()
    assert error.value.status_code == 503
    assert "local-tenant" not in str(error.value.detail)


def test_decision_authority_subjects_are_unique() -> None:
    with pytest.raises(ValueError, match="must be unique"):
        DecisionAuthoritySnapshot(
            authority_type="named_users",
            authority_subjects=("reviewer-a", "reviewer-a"),
            required_approval_count=1,
            action_set_version="normal-v1",
            policy_snapshot_id="policy-a",
        )
    for subjects, action_set_version, policy_snapshot_id in (
        (("",), "normal-v1", "policy-a"),
        (("   ",), "normal-v1", "policy-a"),
        (("reviewer-a",), "  ", "policy-a"),
        (("reviewer-a",), "normal-v1", "  "),
    ):
        with pytest.raises(ValueError):
            DecisionAuthoritySnapshot(
                authority_type="named_users",
                authority_subjects=subjects,
                required_approval_count=1,
                action_set_version=action_set_version,
                policy_snapshot_id=policy_snapshot_id,
            )
    for cost in (-0.01, float("nan"), float("inf")):
        with pytest.raises(ValidationError):
            RunUsageResponse(
                prompt_tokens=0,
                completion_tokens=0,
                total_tokens=0,
                cost=cost,
                coverage="partial",
            )


@pytest.mark.asyncio
async def test_ingest_redacts_before_persistence_and_audits_truncation(
    execution_db: AsyncSession, tmp_path: Path
) -> None:
    service = ExecutionIngestService()
    context = _context()
    nested: dict = {"leaf": "safe"}
    for index in range(15):
        nested = {f"level{index}": nested}
    secrets = {
        "apiKey": "api-key-do-not-store",
        "OPENAI_API_KEY": "openai-key-do-not-store",
        "anthropicApiKey": "anthropic-key-do-not-store",
        "accessToken": "access-token-do-not-store",
        "privateKey": "private-key-do-not-store",
        "databaseUrl": "postgresql://admin:password@database.internal/research",
        "totalTokens": 77,
        "path": "/Users/researcher/private/results/raw.csv",
        "long": "x" * 4_100,
        "many": list(range(205)),
        "nested": nested,
    }
    records = [
        {"event": "run_start", "at": AT, "node_type": "analysis"},
        {"event": "tool_call", "at": AT, "turn": 1, "name": "execute", "args": secrets},
        {
            "event": "tool_result",
            "at": AT,
            "turn": 1,
            "status": "completed",
            "result_preview": {"accessToken": "result-token-do-not-store", "count": 1},
        },
        {"event": "hook_noise", "at": AT, "message": "z" * 4_100},
    ]
    complete = b"".join(_raw_line(record) + b"\n" for record in records)
    transcript = _runtime_transcript("runs/orch__redaction/transcript.jsonl", records)
    # 尾巴上追一条**没写完**的记录：worker 还在往下写。
    with transcript.open("ab") as tail:
        tail.write(b'{"event":"tool_call"')

    consumed = await ingest_transcript_like_production(
        execution_db, service=service, context=context, path=transcript, harness_states={}
    )
    # 半行不摄取 —— 它还不是一条记录。生产里 worker 根本不会为它发包装事件；
    # 这里的口径要和那件事一致，否则测试比生产宽松。
    assert consumed == len(complete)

    events = list(
        (await execution_db.execute(select(ExecutionEvent).order_by(ExecutionEvent.sequence)))
        .scalars()
        .all()
    )
    persisted = json.dumps(
        [{"source": event.source, "payload": event.payload} for event in events],
        ensure_ascii=False,
        default=str,
    )
    for secret in (
        "api-key-do-not-store",
        "openai-key-do-not-store",
        "anthropic-key-do-not-store",
        "access-token-do-not-store",
        "private-key-do-not-store",
        "password@database.internal",
        "result-token-do-not-store",
        str(transcript.resolve()),
        "/Users/researcher/private/results/raw.csv",
    ):
        assert secret not in persisted

    tool_started = next(event for event in events if event.kind == "tool.started")
    assert tool_started.payload["arguments"]["apiKey"] == "[REDACTED]"
    assert tool_started.payload["arguments"]["OPENAI_API_KEY"] == "[REDACTED]"
    assert tool_started.payload["arguments"]["anthropicApiKey"] == "[REDACTED]"
    assert tool_started.payload["arguments"]["accessToken"] == "[REDACTED]"
    assert tool_started.payload["arguments"]["privateKey"] == "[REDACTED]"
    assert tool_started.payload["arguments"]["databaseUrl"] == "[REDACTED]"
    assert tool_started.payload["arguments"]["totalTokens"] == 77
    assert tool_started.source["fileRef"].startswith("file_")
    assert "file_path" not in tool_started.source

    warnings = [event for event in events if event.kind == "redaction.warning"]
    codes = {code for event in warnings for code in event.payload["codes"]}
    assert {
        "sensitive_field",
        "path_minimized",
        "string_truncated",
        "array_truncated",
        "maximum_depth",
    }.issubset(codes)
    assert all("codes" in event.payload for event in warnings)
    assert any(
        event.origin == "raw_transcript"
        and event.source["rawEvent"] == "hook_noise"
        and "string_truncated" in event.payload["codes"]
        for event in warnings
    )


@pytest.mark.asyncio
async def test_replay_does_not_advance_sequence_usage_or_retry(
    execution_db: AsyncSession, tmp_path: Path
) -> None:
    service = ExecutionIngestService()
    context = _context()
    records = [
        {"event": "run_start", "at": AT, "node_type": "survey"},
        {
            "event": "llm_response",
            "at": AT,
            "usage_is_cumulative": True,
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "total_tokens": 120,
                "cost": "0.10",
                "currency": "USD",
                "coverage": "complete",
            },
        },
        {"event": "llm_retry_scheduled", "at": AT, "attempt": 2, "max_attempts": 3},
        {
            "event": "llm_response",
            "at": AT,
            "usage_is_cumulative": True,
            "usage": {
                "prompt_tokens": 150,
                "completion_tokens": 30,
                "total_tokens": 180,
                "cost": "0.15",
                "currency": "USD",
                "coverage": "complete",
            },
        },
        {"event": "run_end", "at": AT, "status": "completed_with_warning"},
    ]
    transcript = _runtime_transcript("runs/orch__usage/transcript.jsonl", records)
    states: dict[str, dict] = {}

    consumed = await ingest_transcript_like_production(
        execution_db, service=service, context=context, path=transcript, harness_states=states
    )
    run = await execution_db.get(Run, context.run_id)
    session = await execution_db.get(SessionProjection, context.session_id)
    assert run is not None and session is not None
    assert (run.prompt_tokens, run.completion_tokens, run.total_tokens) == (150, 30, 180)
    assert run.cost == Decimal("0.150000")
    assert run.retry_count == 1
    assert run.status == "completed_with_warning"
    usage_events = list(
        (
            await execution_db.execute(
                select(ExecutionEvent)
                .where(ExecutionEvent.kind == "usage.updated")
                .order_by(ExecutionEvent.sequence)
            )
        )
        .scalars()
        .all()
    )
    assert [event.payload["cost"] for event in usage_events] == [0.1, 0.05]
    assert all(type(event.payload["cost"]) is float for event in usage_events)
    sequence_before = session.next_sequence

    # 从头再摄一遍。幂等不靠"记得读到哪了"，靠 raw 行的唯一键
    # (tenant, run, file_identity, byte_offset, raw_line_hash) —— 补齐路径重放
    # 整个文件时依赖的正是这条，所以这里就从 0 再来一次。
    await ingest_transcript_like_production(
        execution_db, service=service, context=context, path=transcript,
        harness_states={}, from_offset=0,
    )
    await execution_db.refresh(run)
    await execution_db.refresh(session)
    assert session.next_sequence == sequence_before
    assert (run.prompt_tokens, run.completion_tokens, run.total_tokens) == (150, 30, 180)
    assert run.cost == Decimal("0.150000")
    assert run.retry_count == 1

    appended = {
        "event": "llm_response",
        "at": AT,
        "usage_is_cumulative": True,
        "usage": {
            "prompt_tokens": 175,
            "completion_tokens": 35,
            "total_tokens": 210,
            "cost": "0.18",
            "currency": "USD",
            "coverage": "complete",
        },
    }
    with transcript.open("ab") as output:
        output.write(_raw_line(appended) + b"\n")
    await ingest_transcript_like_production(
        execution_db, service=service, context=context, path=transcript,
        harness_states=states, from_offset=consumed,
    )
    await execution_db.refresh(run)
    assert (run.prompt_tokens, run.completion_tokens, run.total_tokens) == (175, 35, 210)
    assert run.cost == Decimal("0.180000")
    appended_usage = await execution_db.scalar(
        select(ExecutionEvent)
        .where(ExecutionEvent.kind == "usage.updated")
        .order_by(ExecutionEvent.sequence.desc())
    )
    assert appended_usage is not None
    assert appended_usage.payload["cost"] == 0.03
    assert type(appended_usage.payload["cost"]) is float


@pytest.mark.asyncio
async def test_timestamp_decisions_and_commands_fail_closed(
    execution_db: AsyncSession,
) -> None:
    service = ExecutionIngestService()
    for suffix, raw in (
        ("invalid", {"event": "run_start", "at": "not-a-time"}),
        ("missing", {"event": "run_start"}),
        ("naive", {"event": "run_start", "at": "2026-08-03T01:02:03"}),
    ):
        malformed_context = _context(session=f"session-{suffix}", run=f"run-{suffix}")
        with pytest.raises(TranscriptParseError):
            await _ingest(
                service,
                execution_db,
                malformed_context,
                raw,
                offset=0,
            )
        assert await execution_db.get(SessionProjection, malformed_context.session_id) is None
        assert await execution_db.get(Run, malformed_context.run_id) is None

    context = _context(actor="initiator")

    human = await _ingest(
        service,
        execution_db,
        context,
        {"event": "human_input_requested", "at": AT, "question": "Which material?", "n_options": 3},
        offset=10,
    )
    assert human.event is not None and human.event.kind == "run.paused"
    assert human.event.payload["detailsUnavailable"] is True
    assert await execution_db.scalar(select(Decision)) is None
    session = await execution_db.get(SessionProjection, context.session_id)
    assert session is not None

    # A genuinely unknown action set still fails closed.
    before_ambiguous = session.next_sequence
    with pytest.raises(UnsupportedDecisionActionSetError):
        await _ingest(
            service,
            execution_db,
            context,
            {
                "event": "decision_package_presented",
                "at": AT,
                "producing_run_id": "producer-b",
                "review_failed": True,
                "decision_options": ["launch_missiles"],
            },
            offset=21,
        )
    assert session.next_sequence == before_ambiguous

    normal_decision = {
        "event": "decision_package_presented",
        "at": AT,
        "producing_run_id": "producer-a",
        "source_node_type": "analysis",
        "recommended_action": "proceed",
        "review_failed": False,
        "context": {"apiKey": "decision-secret-do-not-store", "summary": "ready"},
    }
    with pytest.raises(DecisionAuthorityRequiredError):
        await _ingest(
            service,
            execution_db,
            context,
            normal_decision,
            offset=30,
        )
    assert session.next_sequence == before_ambiguous

    authority = DecisionAuthoritySnapshot(
        authority_type="named_users",
        authority_subjects=("reviewer-a", "reviewer-b"),
        required_approval_count=2,
        action_set_version="normal-v1",
        policy_snapshot_id="policy-snapshot-a",
    )
    authorized_context = _context(authority=authority, actor="initiator")

    # Review-failed Decision is a modeled action set, not an ingest failure.
    review_failed_decision = await _ingest(
        service,
        execution_db,
        authorized_context,
        {
            "event": "decision_package_presented",
            "at": AT,
            "producing_run_id": "producer-review-failed",
            "source_node_type": "experiment",
            "review_failed": True,
            "decision_options": [
                "retry_reviewer", "revise", "redirect_upstream", "abort", "edit",
            ],
        },
        offset=22,
    )
    assert review_failed_decision.event is not None
    assert review_failed_decision.event.kind == "decision.required"
    rf_choice_ids = [c["choiceId"] for c in review_failed_decision.event.payload["choices"]]
    assert rf_choice_ids == ["retry_reviewer", "revise", "redirect_upstream", "abort", "edit"]
    assert "proceed" not in rf_choice_ids
    assert review_failed_decision.event.payload["reviewFailed"] is True

    outcome = await _ingest(
        service,
        execution_db,
        authorized_context,
        normal_decision,
        offset=30,
    )
    assert outcome.event is not None
    # 不钉 id 的**字符串形态**，钉不变量：这次 run 里恰好一个 decision，且
    # 它的 id 里带着 run 作用域。2026-08-10 起 decision id 按平台 run 作用域化
    # —— 恢复出来的会话会复用同一个 harness producing run，id 不带 run_id 就
    # 会撞上不可变判据，整轮死（见 test_decision_id_is_scoped_to_one_run）。
    decision = await execution_db.scalar(
        select(Decision).where(Decision.id.like("%producer-a"))
    )
    assert decision is not None
    assert authorized_context.run_id in decision.id
    assert [choice["choiceId"] for choice in decision.choices] == [
        "proceed",
        "revise",
        "redirect_upstream",
        "abort",
        "edit",
    ]
    assert decision.context["apiKey"] == "[REDACTED]"

    sequence_before_mutation = session.next_sequence
    changed_expiry = DecisionAuthoritySnapshot(
        authority_type="named_users",
        authority_subjects=("reviewer-a", "reviewer-b"),
        required_approval_count=2,
        action_set_version="normal-v1",
        policy_snapshot_id="policy-snapshot-a",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    # 不可变快照守卫仍然拒收变异 —— 但裁决权只到"这条记录不落成正常事件"
    # 为止：拒收落成 record.rejected 见证事件，冻结的 Decision 一个字不变，
    # 摄取调用**不再抛异常**（2026-08-17 之前它抛穿 answer()，整轮被判死）。
    mutated_by_authority = await _ingest(
        service,
        execution_db,
        _context(authority=changed_expiry, actor="initiator"),
        normal_decision,
        offset=40,
    )
    assert mutated_by_authority.event is not None
    assert mutated_by_authority.event.kind == "record.rejected"
    assert "immutable snapshot mutation" in str(mutated_by_authority.event.payload["reason"])
    mutated_by_content = await _ingest(
        service,
        execution_db,
        authorized_context,
        {**normal_decision, "prompt": "A changed re-presentation"},
        offset=50,
    )
    assert mutated_by_content.event is not None
    assert mutated_by_content.event.kind == "record.rejected"
    frozen_decision = await execution_db.scalar(
        select(Decision).where(Decision.id == decision.id)
    )
    assert frozen_decision is not None
    assert frozen_decision.prompt == decision.prompt
    # 见证事件本身要占序号 —— 它是真事件，不是日志。
    assert session.next_sequence == sequence_before_mutation + 2

    with pytest.raises(DecisionResponseRejectedError):
        await service.respond_to_decision(
            execution_db,
            tenant_id="tenant-a",
            decision_id=decision.id,
            actor_user_id="outsider",
            choice_id="proceed",
            has_decision_respond_capability=True,
        )
    await service.respond_to_decision(
        execution_db,
        tenant_id="tenant-a",
        decision_id=decision.id,
        actor_user_id="reviewer-a",
        choice_id="proceed",
        expected_accepted_response_count=0,
        has_decision_respond_capability=True,
    )
    assert decision.status == DecisionStatus.PARTIALLY_APPROVED
    with pytest.raises(DecisionResponseRejectedError, match="changed after") as stale:
        await service.respond_to_decision(
            execution_db,
            tenant_id="tenant-a",
            decision_id=decision.id,
            actor_user_id="reviewer-b",
            choice_id="proceed",
            expected_accepted_response_count=0,
            has_decision_respond_capability=True,
        )
    assert stale.value.code == "decision_response_stale"
    assert len(decision.accepted_responses) == 1
    assert decision.status == DecisionStatus.PARTIALLY_APPROVED
    await service.respond_to_decision(
        execution_db,
        tenant_id="tenant-a",
        decision_id=decision.id,
        actor_user_id="reviewer-b",
        choice_id="proceed",
        expected_accepted_response_count=1,
        has_decision_respond_capability=True,
    )
    assert decision.status == DecisionStatus.RESOLVED

    command_context = _context(actor="initiator")
    command = await service.create_command(
        execution_db,
        context=command_context,
        kind="run.pause",
        idempotency_key="pause-once",
        payload={"apiKey": "command-payload-secret", "reason": "review"},
    )
    replayed = await service.create_command(
        execution_db,
        context=command_context,
        kind="run.pause",
        idempotency_key="pause-once",
        payload={"apiKey": "command-payload-secret", "reason": "review"},
    )
    assert replayed.id == command.id
    with pytest.raises(IdempotencyConflictError):
        await service.create_command(
            execution_db,
            context=command_context,
            kind="run.cancel",
            idempotency_key="pause-once",
            payload={"reason": "different"},
        )
    await service.complete_command(
        execution_db,
        tenant_id="tenant-a",
        command_id=command.id,
        result={"accessToken": "command-result-secret"},
        error={"privateKey": "command-error-secret"},
    )
    persisted_command = json.dumps(
        {"payload": command.payload, "result": command.result, "error": command.error}
    )
    for secret in (
        "command-payload-secret",
        "command-result-secret",
        "command-error-secret",
    ):
        assert secret not in persisted_command
    # 「这条命令失败了」的事实就是 error 在场 —— 不再另存一个状态词。
    assert command.error is not None


@pytest.mark.asyncio
async def test_unmatched_tool_result_never_creates_orphan_root(
    execution_db: AsyncSession,
) -> None:
    service = ExecutionIngestService()
    context = _context(session="session-truncated", run="run-truncated")
    outcome = await _ingest(
        service,
        execution_db,
        context,
        {
            "event": "tool_result",
            "at": AT,
            "turn": 9,
            "status": "completed",
            "result_preview": {"status": "completed"},
        },
        offset=0,
        state={},
    )
    session = await execution_db.get(SessionProjection, context.session_id)
    events = list(
        (
            await execution_db.execute(
                select(ExecutionEvent).where(ExecutionEvent.session_id == context.session_id)
            )
        )
        .scalars()
        .all()
    )
    assert outcome.event is None
    assert session is not None and session.next_sequence == 0
    assert events == []


@pytest.mark.asyncio
async def test_background_interleaving_never_claims_tool_step_association(
    execution_db: AsyncSession,
) -> None:
    service = ExecutionIngestService()
    context = _context()
    state: dict = {}
    records = [
        {"event": "run_start", "at": AT, "node_type": "orchestrator"},
        {"event": "tool_call", "at": AT, "turn": 1, "name": "run_node", "args": {}},
        {
            "event": "subagent_call_start",
            "at": AT,
            "child_node_type": "survey",
            "child_depth": 1,
            "background": True,
            "forwarded_artifacts": [],
            "autoresolved": None,
            "node_inputs_preview": "{}",
        },
        {
            "event": "tool_result",
            "at": AT,
            "turn": 1,
            "status": "completed",
            "result_preview": {"status": "started"},
        },
        {"event": "tool_call", "at": AT, "turn": 2, "name": "run_node", "args": {}},
        {
            "event": "subagent_call_start",
            "at": AT,
            "child_node_type": "writing",
            "child_depth": 1,
            "background": False,
            "forwarded_artifacts": [],
            "autoresolved": None,
            "node_inputs_preview": "{}",
        },
        {
            "event": "subagent_call_end",
            "at": AT,
            "child_node_type": "survey",
            "child_run_id": "child-survey",
            "child_status": "completed",
        },
        {
            "event": "subagent_call_end",
            "at": AT,
            "child_node_type": "writing",
            "child_run_id": "child-writing",
            "child_status": "completed",
        },
        {
            "event": "tool_result",
            "at": AT,
            "turn": 2,
            "status": "completed",
            "result_preview": {"status": "completed"},
        },
    ]
    for offset, raw in enumerate(records):
        await _ingest(
            service,
            execution_db,
            context,
            raw,
            offset=offset * 100,
            state=state,
        )

    events = list(
        (await execution_db.execute(select(ExecutionEvent).order_by(ExecutionEvent.sequence)))
        .scalars()
        .all()
    )
    # 本测试守的不变量：**父节点自己的工具调用，不许因为此刻有子节点在飞就被
    # 算到子节点那一步。** 这条继续成立，而且现在更强 —— 归属按 transcript 文件
    # 走（每个子 run 有自己的文件、自己的 step），不再依赖"当前前台是谁"的推断。
    #
    # 2026-08-10 起父节点**不再**为子节点声明 step：派发那一刻它不知道子 run 的
    # id（child_run_id 来自子节点跑完后的 summary），发出来的 step 必然与子
    # transcript 自己声明的那个对不上，结果是同一节点在 Trace 上出现两次、且父侧
    # 那个永远空着。所以下面删掉了对"父侧 step.started/completed"的断言 ——
    # 那是旧实现的产物，不是本测试要守的东西。
    step_starts = [event for event in events if event.kind == "step.started"]
    assert not [e for e in step_starts if e.payload.get("title") in {"writing", "survey"}], (
        "子节点的 step 只能由它自己的 transcript 声明，父侧不许再发一个"
    )
    tools = [event for event in events if event.kind == "tool.started"]
    assert len(tools) == 2
    # 两次 run_node 都是**调度器自己**调的，必须落在同一步（它的根 step）上
    assert tools[0].payload["stepId"] == tools[1].payload["stepId"]
    assert tools[0].payload["toolCallId"] != tools[1].payload["toolCallId"]
    root_start = next(
        event for event in events
        if event.kind == "step.started" and event.payload["title"] == "orchestrator activity"
    )
    assert tools[0].payload["stepId"] == root_start.payload["stepId"]


@pytest.mark.asyncio
async def test_read_api_is_exclusive_and_tenant_fail_closed(
    execution_db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = ExecutionIngestService()
    for index, cost in enumerate((-0.01, "not-a-number", "NaN", "Infinity")):
        rejected_context = _context(
            session=f"session-rejected-cost-{index}",
            run=f"run-rejected-cost-{index}",
        )
        with pytest.raises(IngestError, match="finite non-negative"):
            await _ingest(
                service,
                execution_db,
                rejected_context,
                {
                    "event": "llm_response",
                    "at": AT,
                    "usage": {
                        "prompt_tokens": 50,
                        "completion_tokens": 10,
                        "total_tokens": 60,
                        "cost": cost,
                    },
                },
                offset=0,
            )
        assert await execution_db.get(SessionProjection, rejected_context.session_id) is None
        assert await execution_db.get(Run, rejected_context.run_id) is None
        rejected_events = list(
            (
                await execution_db.execute(
                    select(ExecutionEvent).where(
                        ExecutionEvent.session_id == rejected_context.session_id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert rejected_events == []

    authority = DecisionAuthoritySnapshot(
        authority_type="initiating_user",
        authority_subjects=("reviewer-a",),
        required_approval_count=1,
        action_set_version="normal-v1",
        policy_snapshot_id="policy-a",
    )
    context = _context(authority=authority)
    state: dict = {}
    records = [
        {"event": "run_start", "at": AT, "node_type": "analysis"},
        {
            "event": "llm_response",
            "at": AT,
            "usage": {
                "prompt_tokens": 12,
                "completion_tokens": 3,
                "total_tokens": 15,
                "cost": "0.02",
                "currency": "USD",
                "coverage": "complete",
            },
        },
        {"event": "llm_retry_scheduled", "at": AT, "attempt": 2, "max_attempts": 2},
        {
            "event": "decision_package_presented",
            "at": AT,
            "producing_run_id": "producer-api",
            "source_node_type": "analysis",
            "recommended_action": "proceed",
            "review_failed": False,
        },
    ]
    # 最后一条（producer-api 的呈递）留到过期授权那条之后再摄：一条 run 同一时刻
    # 只有一张活卡，新呈递落地会退役同一条 run 上更早的 pending 卡（2026-09-09）。
    # 这条测试要看的是"过期的不算当前、活的那张能按 id 取"，所以活的那张最后落地。
    for offset, raw in enumerate(records[:-1]):
        await _ingest(
            service,
            execution_db,
            context,
            raw,
            offset=offset * 100,
            state=state,
        )
    foreign = _context(tenant="tenant-b", session="session-b", run="run-b")
    await _ingest(
        service,
        execution_db,
        foreign,
        {"event": "run_start", "at": AT, "node_type": "survey"},
        offset=0,
    )
    expired_authority = DecisionAuthoritySnapshot(
        authority_type="initiating_user",
        authority_subjects=("reviewer-a",),
        required_approval_count=1,
        action_set_version="normal-v1",
        policy_snapshot_id="policy-expired",
        expires_at=datetime.now(UTC) - timedelta(minutes=1),
    )
    await _ingest(
        service,
        execution_db,
        _context(authority=expired_authority),
        {
            "event": "decision_package_presented",
            "at": AT,
            "producing_run_id": "producer-expired",
            "source_node_type": "analysis",
            "recommended_action": "proceed",
            "review_failed": False,
        },
        offset=500,
        state=state,
    )
    await _ingest(
        service,
        execution_db,
        context,
        records[-1],
        offset=(len(records) - 1) * 100 + 1000,
        state=state,
    )
    fractional_cost_context = _context(
        session="session-fractional-cost",
        run="run-fractional-cost",
    )
    await _ingest(
        service,
        execution_db,
        fractional_cost_context,
        {"event": "run_start", "at": AT, "node_type": "analysis"},
        offset=0,
    )
    await _ingest(
        service,
        execution_db,
        fractional_cost_context,
        {
            "event": "llm_response",
            "at": AT,
            "usage": {
                "prompt_tokens": 20,
                "completion_tokens": 4,
                "total_tokens": 24,
                "cost": "0.041",
                "currency": "USD",
                "coverage": "complete",
            },
        },
        offset=100,
    )
    fractional_cost_event = await execution_db.scalar(
        select(ExecutionEvent).where(
            ExecutionEvent.session_id == fractional_cost_context.session_id,
            ExecutionEvent.kind == "usage.updated",
        )
    )
    assert fractional_cost_event is not None
    assert fractional_cost_event.payload["cost"] == 0.041
    assert type(fractional_cost_event.payload["cost"]) is float

    api = FastAPI()
    api.include_router(router, prefix="/api/v1")

    async def override_db():
        yield execution_db

    async def override_user():
        return SimpleNamespace(id="reviewer-a")

    api.dependency_overrides[get_db] = override_db
    api.dependency_overrides[get_current_user] = override_user
    api.dependency_overrides[get_runtime_tenant_id] = lambda: "tenant-a"
    async with AsyncClient(transport=ASGITransport(app=api), base_url="http://test") as client:
        runs = (await client.get("/api/v1/runs", headers={"X-Tenant-ID": "tenant-b"})).json()
        runs_by_id = {item["id"]: item for item in runs["items"]}
        assert set(runs_by_id) == {"run-a", "run-fractional-cost"}
        assert runs_by_id["run-a"]["usage"] == {
            "promptTokens": 12,
            "completionTokens": 3,
            "totalTokens": 15,
            "cost": 0.02,
            "currency": "USD",
            "coverage": "complete",
        }
        assert runs_by_id["run-a"]["retryCount"] == 1
        assert isinstance(runs_by_id["run-a"]["usage"]["cost"], float)

        zero_usage_context = _context(session="session-zero", run="run-zero")
        await _ingest(
            service,
            execution_db,
            zero_usage_context,
            {"event": "run_start", "at": AT, "node_type": "survey"},
            offset=0,
        )
        await _ingest(
            service,
            execution_db,
            zero_usage_context,
            {
                "event": "llm_response",
                "at": AT,
                "usage": {
                    "prompt_tokens": 3,
                    "completion_tokens": 1,
                    "total_tokens": 4,
                },
            },
            offset=100,
        )
        zero_usage = (await client.get("/api/v1/runs/run-zero")).json()["run"]["usage"]
        assert "cost" in zero_usage and zero_usage["cost"] is None

        negative_context = _context(session="session-negative", run="run-negative")
        negative_state: dict = {}
        await _ingest(
            service,
            execution_db,
            negative_context,
            {"event": "run_start", "at": AT, "node_type": "survey"},
            offset=0,
            state=negative_state,
        )
        negative_session = await execution_db.get(SessionProjection, negative_context.session_id)
        negative_run = await execution_db.get(Run, negative_context.run_id)
        assert negative_session is not None and negative_run is not None
        sequence_before_invalid_cost = negative_session.next_sequence
        for cost in (-0.01, "not-a-number", "NaN", "Infinity"):
            with pytest.raises(TranscriptParseError, match="finite non-negative"):
                await _ingest(
                    service,
                    execution_db,
                    negative_context,
                    {
                        "event": "llm_response",
                        "at": AT,
                        "usage": {
                            "prompt_tokens": 50,
                            "completion_tokens": 10,
                            "total_tokens": 60,
                            "cost": cost,
                        },
                    },
                    offset=100,
                    state=negative_state,
                )
        assert negative_session.next_sequence == sequence_before_invalid_cost
        assert (
            negative_run.prompt_tokens,
            negative_run.completion_tokens,
            negative_run.total_tokens,
            negative_run.cost,
        ) == (0, 0, 0, None)
        negative_usage_events = list(
            (
                await execution_db.execute(
                    select(ExecutionEvent).where(
                        ExecutionEvent.session_id == negative_context.session_id,
                        ExecutionEvent.kind == "usage.updated",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert negative_usage_events == []
        negative_usage = (await client.get("/api/v1/runs/run-negative")).json()["run"]["usage"]
        assert negative_usage["cost"] is None
        assert negative_usage["totalTokens"] == 0

        detail = (await client.get("/api/v1/runs/run-a")).json()
        assert detail["run"]["id"] == "run-a"
        assert detail["attempts"][0]["attemptNo"] == 1
        expected_run_events = list(
            (
                await execution_db.execute(
                    select(ExecutionEvent).where(
                        ExecutionEvent.tenant_id == "tenant-a",
                        ExecutionEvent.run_id == "run-a",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert detail["eventCount"] == len(expected_run_events)
        assert detail["eventCount"] > 0
        assert (await client.get("/api/v1/runs/run-b")).status_code == 404

        secondary_context = _context(session="session-a", run="run-a-secondary")
        await _ingest(
            service,
            execution_db,
            secondary_context,
            {"event": "run_start", "at": AT, "node_type": "analysis"},
            offset=0,
            file_identity="secondary-run-file",
        )
        filtered_page = (
            await client.get(
                "/api/v1/sessions/session-a/events",
                params={"runId": "run-a"},
            )
        ).json()
        assert len(filtered_page["items"]) == detail["eventCount"]
        assert {item["runId"] for item in filtered_page["items"]} == {"run-a"}
        assert (
            await client.get(
                "/api/v1/sessions/session-a/events",
                params={"runId": "run-a-secondary"},
            )
        ).json()["items"][0]["runId"] == "run-a-secondary"
        cross_session = await client.get(
            "/api/v1/sessions/session-a/events",
            params={"runId": "run-fractional-cost"},
        )
        assert cross_session.status_code == 404
        assert cross_session.json() == {"code": "not_found", "message": "Run not found."}
        assert (
            await client.get(
                "/api/v1/sessions/session-a/events",
                params={"runId": "run-b"},
            )
        ).status_code == 404
        assert (
            await client.get(
                "/api/v1/sessions/session-a/events",
                params={"runId": "run-missing"},
            )
        ).status_code == 404
        assert (
            await client.get(
                "/api/v1/sessions/session-a/events",
                params={"runId": ""},
            )
        ).status_code == 422

        page = (
            await client.get(
                "/api/v1/sessions/session-a/events",
                params={"afterSequence": 1, "limit": 1},
            )
        ).json()
        assert page["items"][0]["sequence"] == 2
        assert page["afterSequence"] == 1
        assert page["nextAfterSequence"] == 2
        assert page["hasMore"] is True
        assert page["items"][0]["kind"] == "usage.updated"
        assert page["items"][0]["payload"]["cost"] == 0.02
        assert type(page["items"][0]["payload"]["cost"]) is float
        fractional_cost_page = (
            await client.get(
                "/api/v1/sessions/session-fractional-cost/events",
                params={"afterSequence": 1},
            )
        ).json()
        assert fractional_cost_page["items"][0]["payload"]["cost"] == 0.041
        assert type(fractional_cost_page["items"][0]["payload"]["cost"]) is float
        zero_cost_page = (
            await client.get(
                "/api/v1/sessions/session-zero/events",
                params={"afterSequence": 1},
            )
        ).json()
        assert zero_cost_page["items"][0]["payload"]["cost"] is None
        empty = (
            await client.get(
                "/api/v1/sessions/session-a/events",
                params={"afterSequence": 999},
            )
        ).json()
        assert empty == {
            "items": [],
            "afterSequence": 999,
            "nextAfterSequence": 999,
            "hasMore": False,
        }
        assert (await client.get("/api/v1/sessions/session-b/events")).status_code == 404

        current = (await client.get("/api/v1/sessions/session-a/decisions/current")).json()
        assert len(current["items"]) == 1
        decision_id = current["items"][0]["id"]
        # 同上：钉"带 run 作用域"，不钉具体字符串。
        assert "producer-api" in decision_id and "run-a" in decision_id
        assert current["items"][0]["authority"]["policySnapshotId"] == "policy-a"
        assert (await client.get(f"/api/v1/decisions/{decision_id}")).status_code == 200
        assert (await client.get("/api/v1/sessions/session-b/decisions/current")).status_code == 404
        assert (await client.get("/api/v1/decisions/foreign")).status_code == 404

        invalid = await client.get(
            "/api/v1/sessions/session-a/events", params={"afterSequence": -1}
        )
        assert invalid.status_code == 422
        assert invalid.json() == {
            "code": "invalid_request",
            "message": "Request validation failed.",
        }

        monkeypatch.setattr(settings, "debug", False)
        api.dependency_overrides.pop(get_current_user)
        unauthenticated = await client.get("/api/v1/runs")
        assert unauthenticated.status_code == 401
        assert unauthenticated.json() == {
            "code": "unauthorized",
            "message": "Invalid authentication credentials",
        }

        api.dependency_overrides[get_current_user] = override_user
        api.dependency_overrides.pop(get_runtime_tenant_id)
        monkeypatch.setattr(settings, "runtime_tenant_id", "")
        unavailable = await client.get("/api/v1/runs")
        assert unavailable.status_code == 503
        assert unavailable.json() == {
            "code": "service_unavailable",
            "message": "Execution tenant is not configured.",
        }

    with pytest.raises(IntegrityError):
        async with execution_db.begin_nested():
            negative_run.cost = Decimal("-0.01")
            await execution_db.flush()

    commands = list((await execution_db.execute(select(Command))).scalars().all())
    assert commands == []


@pytest.mark.asyncio
async def test_child_node_terminal_never_closes_the_parent_command_attempt(
    execution_db: AsyncSession,
) -> None:
    """一个用户命令 = 一个 Run；harness 在命令内跑节点树。

    实测事故（2026-08-06 node20 EXP-01）：子节点 experiment 以 incomplete 收尾，
    ingest 把父命令的 attempt 关成 failed；随后 decision 把 run 翻回
    waiting_human。于是 runs 说"等你回答、可恢复"，run_attempts 说"我早死了"，
    恢复校验要求 attempt=RUNNING → 人工答复永远被拒，post-node 决策后 run 永久
    卡死。新拓扑每轮 Analysis↔Experiment 都要过 post-node 决策，这条必须成立。
    """
    service = ExecutionIngestService()
    authority = DecisionAuthoritySnapshot(
        authority_type="initiating_user",
        authority_subjects=("initiator",),
        required_approval_count=1,
        action_set_version="harness-normal-v1",
        policy_snapshot_id="policy-nested",
    )
    context = _context(session="session-nested", run="run-nested",
                       authority=authority, actor="initiator")
    state: dict = {}
    offset = 0

    async def ingest(raw: dict):
        nonlocal offset
        offset += 100
        return await _ingest(service, execution_db, context, raw,
                             offset=offset, state=state)

    # 命令自己的 run（orchestrator）
    await ingest({"event": "run_start", "at": AT, "node_type": "project_chat"})
    # 子节点：experiment 跑完但 incomplete
    await ingest({"event": "run_start", "at": AT, "node_type": "experiment"})
    child_end = await ingest({"event": "run_end", "at": AT, "status": "incomplete"})
    assert child_end.event is not None
    assert child_end.event.payload["owningRun"] is False

    run = await execution_db.get(Run, context.run_id)
    assert run is not None
    assert run.status == RunStatus.RUNNING.value, "子节点 incomplete 不得改父命令状态"
    attempt = await execution_db.scalar(
        select(RunAttempt).where(RunAttempt.run_id == context.run_id)
    )
    assert attempt is not None
    assert attempt.status == AttemptStatus.RUNNING.value, "子节点终态不得关闭父 attempt"

    # 子节点 _reviewer 完成，同样不得终结父命令
    await ingest({"event": "run_start", "at": AT, "node_type": "_reviewer"})
    await ingest({"event": "run_end", "at": AT, "status": "completed"})
    await execution_db.refresh(run)
    await execution_db.refresh(attempt)
    assert run.status == RunStatus.RUNNING.value
    assert attempt.status == AttemptStatus.RUNNING.value

    # post-node 决策：run 进入 waiting_human，attempt 仍然 RUNNING → 可恢复
    await ingest({
        "event": "decision_package_presented", "at": AT,
        "producing_run_id": "producer-nested", "source_node_type": "experiment",
        "review_failed": True,
        "decision_options": ["retry_reviewer", "revise", "redirect_upstream",
                             "abort", "edit"],
    })
    await execution_db.refresh(run)
    await execution_db.refresh(attempt)
    assert run.status == RunStatus.WAITING_HUMAN.value
    assert attempt.status == AttemptStatus.RUNNING.value, (
        "waiting_human 的 run 必须还有活着的 attempt，否则恢复校验永远拒绝答复"
    )

    # 命令自己收尾时才真正关闭 attempt
    own_end = await ingest({"event": "run_end", "at": AT, "status": "completed"})
    assert own_end.event is not None
    assert own_end.event.payload["owningRun"] is True
    await execution_db.refresh(run)
    await execution_db.refresh(attempt)
    assert run.status == RunStatus.COMPLETED.value
    assert attempt.status != AttemptStatus.RUNNING.value


@pytest.mark.asyncio
async def test_child_transcript_file_never_owns_the_parent_run(
    execution_db: AsyncSession,
) -> None:
    """子节点 transcript 不得自称 owning —— 走**生产那条**摄取路验。

    单文件内的深度计数看不见跨文件嵌套（实测：experiment 子 run 的 run_start
    在它自己的文件里深度=1，被误判成 owning）。归属必须按文件定：一个
    platform Run 的第一份 transcript 才是这条命令自己的。

    这个用例此前跑在 `ingest_transcript_file` 上 —— 一条零生产调用方的路。
    它在那条路上一直绿，而生产里这道闸恒真：实测 2026-08-25 session 2220d882,
    10 个 run（含孙节点）`owningRun` 全是 true。**判据没错，测的路错了。**
    """
    service = ExecutionIngestService()
    context = _context(session="session-files", run="run-files")
    states: dict[str, dict] = {}

    parent = _runtime_transcript(
        "runs/orchestrator__p__session__files/transcript.jsonl",
        [{"event": "run_start", "at": AT, "node_type": "project_chat"}],
    )
    await ingest_transcript_like_production(
        execution_db, service=service, context=context, path=parent, harness_states=states
    )

    child = _runtime_transcript(
        "runs/1786331823-experiment/transcript.jsonl",
        [
            {"event": "run_start", "at": AT, "node_type": "experiment"},
            {"event": "run_end", "at": AT, "status": "incomplete"},
        ],
    )
    await ingest_transcript_like_production(
        execution_db, service=service, context=context, path=child, harness_states=states
    )

    kinds = {
        (e.kind, (e.payload or {}).get("nodeType")): (e.payload or {}).get("owningRun")
        for e in (
            await execution_db.scalars(
                select(ExecutionEvent).where(ExecutionEvent.run_id == context.run_id)
            )
        ).all()
    }
    assert kinds[("run.started", "project_chat")] is True
    assert kinds[("run.started", "experiment")] is False, "子节点 transcript 不得自称 owning"

    run = await execution_db.get(Run, context.run_id)
    assert run is not None
    assert run.status == RunStatus.RUNNING.value
    attempt = await execution_db.scalar(
        select(RunAttempt).where(RunAttempt.run_id == context.run_id)
    )
    assert attempt is not None
    assert attempt.status == AttemptStatus.RUNNING.value


@pytest.mark.asyncio
async def test_platform_own_records_do_not_make_the_parent_a_latecomer(
    execution_db: AsyncSession,
) -> None:
    """平台自己写的记录不算"这个 run 已经收过 transcript 了"。

    归属判据现在数 `execution_events`。而在第一条 transcript 之前，
    `execute_local_turn` 已经用 `LOCAL_RECORD_IDENTITY_PREFIX` 打头的身份写过
    run_start / session_message。把它们数进去，父 transcript 自己就成了后来者
    —— 闸从"恒真"翻成"恒假"，两个方向都错。
    """
    service = ExecutionIngestService()
    context = _context(session="session-local-first", run="run-local-first")

    # 平台自己的记录先落库（生产顺序：run_start 在 worker 出声之前）
    await service.ingest_raw_record(
        execution_db,
        context=context,
        file_identity=f"{LOCAL_RECORD_IDENTITY_PREFIX}{context.run_id}:op",
        byte_offset=0,
        raw_line=_raw_line({"event": "run_start", "at": AT, "node_type": "project_chat"}),
        raw={"event": "run_start", "at": AT, "node_type": "project_chat"},
        adapter_state={},
    )

    parent = _runtime_transcript(
        "runs/orchestrator__p__session__local/transcript.jsonl",
        [{"event": "run_start", "at": AT, "node_type": "project_chat"}],
    )
    await ingest_transcript_like_production(
        execution_db, service=service, context=context, path=parent, harness_states={}
    )

    owning = [
        (e.payload or {}).get("owningRun")
        for e in (
            await execution_db.scalars(
                select(ExecutionEvent).where(
                    ExecutionEvent.run_id == context.run_id,
                    ExecutionEvent.kind == "run.started",
                )
            )
        ).all()
    ]
    assert True in owning, "父 transcript 被平台自记的记录挤成了后来者"


def test_live_ownership_is_decided_per_run_not_per_turn() -> None:
    """归属判据必须是 **run 级**的 —— 2026-08-19 事故的根因 C。

    `_harness_adapter_state` 此前自己算归属：`owningTranscript = not states`。
    而 `states` 是 `execute_local_turn` 的局部变量、**每 turn 清零**，于是它
    实际回答的是"本轮第一个出声的文件"。第一轮两者恰好相等；续跑落进子节点时
    不相等 —— 子节点成了"本轮第一个"，它的 `run_end` 关掉了父 run 的 attempt，
    两条生命周期永久分叉，之后所有人工答复被判 stale（实测：25 条一样的
    ERROR / 3 天）。

    这里只守"函数如实带走传进来的归属"这一件事。归属**算得对不对**由
    `test_child_transcript_file_never_owns_the_parent_run` 在真实摄取路上验 ——
    从前那条断言是 `inspect.getsource()` 两条路径的字符串比对，而它保的是
    "两份实现别分叉"。现在只有一份实现，没有可分叉的对象了。
    """
    import inspect

    from app.services.local_execution import _harness_adapter_state

    # 带走传进来的归属，不自己算
    states: dict[str, dict] = {}
    assert _harness_adapter_state(states, "dev:ino-a", True)["owningTranscript"] is True
    assert _harness_adapter_state(states, "dev:ino-b", False)["owningTranscript"] is False
    # 同一文件重复取回同一份 state（深度计数要跨事件累积）
    states["dev:ino-a"]["runDepth"] = 1
    assert _harness_adapter_state(states, "dev:ino-a", False)["runDepth"] == 1, (
        "同一文件必须复用同一份 adapter_state"
    )

    # 非 owning 的文件必须带上"这一次派发"的身份，否则 `_root_step_id` 回落到
    # 槽位级 id，同一节点重派的 step.started 被幂等吞掉（实测 7 次派发 1 张卡）。
    assert states["dev:ino-b"]["dispatchKey"] == "dev:ino-b"
    assert "dispatchKey" not in states["dev:ino-a"], "owning 的那份不是「某一次派发」"

    # 老的 turn 级判据不许回来
    assert '"owningTranscript": not states' not in inspect.getsource(_harness_adapter_state)


@pytest.mark.asyncio
async def test_reported_blockers_reach_the_user_facing_event_stream(
    execution_db: AsyncSession,
) -> None:
    """节点报的阻塞必须投影成用户可见事件 —— 否则 UI 上只看到 run 莫名 incomplete。

    report_blocker 是 v2.1 架构的一等机制（节点只报事实+证据+需求，调度器
    ReAct 决定怎么解）。实测（2026-08-07 E2E）：postprocess 用它如实报了
    missing_input 且说得很具体，但平台完全不投影这个事件 —— 用户看不到任何
    线索，调用方也没消费，最后整条 run 被 cancel。
    """
    service = ExecutionIngestService()
    context = _context(session="session-blocker", run="run-blocker")
    state: dict = {}

    await service.ingest_raw_record(
        execution_db, context=context, file_identity="f-blocker", byte_offset=0,
        raw_line=b"{}", adapter_state=state,
        raw={"event": "run_start", "at": AT, "node_type": "project_chat"},
    )
    outcome = await service.ingest_raw_record(
        execution_db, context=context, file_identity="f-blocker", byte_offset=100,
        raw_line=b"{}", adapter_state=state,
        raw={
            "event": "blocker_reported", "at": AT,
            "blocker_id": "run-x:1", "reporting_node": "postprocess",
            "category": "missing_input",
            "summary": "节点输入缺少 visual_requests 字段",
            "requested_action": "用 visual_requests 重发请求",
            "evidence_paths": ["figures/README.md"],
            "retryable_after_change": True,
        },
    )
    assert outcome.event is not None
    assert outcome.event.kind == "run.blocked"
    assert outcome.event.visibility == "summary"
    payload = outcome.event.payload
    assert payload["reportingNode"] == "postprocess"
    assert payload["category"] == "missing_input"
    assert "visual_requests" in payload["summary"]
    assert payload["retryableAfterChange"] is True


async def test_run_end_failure_category_reaches_the_event_payload(
    execution_db: AsyncSession,
) -> None:
    """失败**原因**随失败事实一起过河 —— 没有它，前端对着 run.failed 只能说
    一句笼统的"失败"（2026-08-20 实测：模型服务读超时打死 hypothesis，
    failure_category 明明白白写在 run_end 里，UI 上只剩"失败 · 31 actions"）。
    """
    service = ExecutionIngestService()
    authority = DecisionAuthoritySnapshot(
        authority_type="initiating_user",
        authority_subjects=("initiator",),
        required_approval_count=1,
        action_set_version="harness-normal-v1",
        policy_snapshot_id="policy-failcat",
    )
    context = _context(session="session-failcat", run="run-failcat",
                       authority=authority, actor="initiator")
    state: dict = {}

    await _ingest(service, execution_db, context,
                  {"event": "run_start", "at": AT, "node_type": "hypothesis"},
                  offset=100, state=state)
    ended = await _ingest(
        service, execution_db, context,
        {"event": "run_end", "at": AT, "status": "error",
         "failure_category": "provider_unavailable",
         "failure_subcategory": "ReadTimeout"},
        offset=200, state=state)
    assert ended.event is not None
    assert ended.event.payload["failureCategory"] == "provider_unavailable"
    assert ended.event.payload["failureSubcategory"] == "ReadTimeout"

    # 没有失败字段的 run_end 不该长出这两个键 —— 缺席就是缺席
    await _ingest(service, execution_db, context,
                  {"event": "run_start", "at": AT, "node_type": "literature"},
                  offset=300, state=state)
    clean = await _ingest(service, execution_db, context,
                          {"event": "run_end", "at": AT, "status": "completed"},
                          offset=400, state=state)
    assert clean.event is not None
    assert "failureCategory" not in clean.event.payload


@pytest.mark.asyncio
async def test_tool_failure_keeps_its_reason_and_who_is_at_fault(
    execution_db: AsyncSession,
) -> None:
    """工具失败必须带着**原因**和**是谁的锅**落到事件里。

    三条回归，都对应过真实事故：

      1. 结果被截断成字符串时仍判得出失败 —— 上一版只对 dict 判 status，
         于是 harness 那边超 500 字节就整体 dumps 的失败全落成
         `tool.completed`，界面显示为成功（本机库 659 条）。
      2. `error_code` 原样透传 —— 它是 harness 在失败发生那一层盖的章
         （rejected = 框架按设计说不 / tool_exception = 我们的代码崩了）。
         上一版硬编码 "tool_error"，唯一的分类信息在这一跳丢掉。
      3. 子进程工具把失败写在 returncode/stderr_tail 里、envelope 没有
         `error` 时，合成一句人看得懂的 —— 否则只剩 "Tool execution failed"。
    """
    service = ExecutionIngestService()
    cases = [
        (
            {"status": "error", "error_code": "tool_exception", "error": "KeyError: 'turns'",
             "traceback_tail": ["  File \"shared/tools/run_node.py\", line 2776, in _run_node",
                                "    \"child_turns\": summary[\"turns\"],",
                                "KeyError: 'turns'"]},
            "tool_exception",
            "KeyError: 'turns'",
        ),
        (
            '{"status": "error", "error_code": "rejected", "error": "⛔ 探查专用 shell"'
            ', "cmd": "cp -r a b"}...[truncated]',
            "rejected",
            "⛔ 探查专用 shell",
        ),
        (
            {"status": "error", "returncode": 2, "stderr_tail": "lammps: no such file"},
            "tool_error",
            "命令退出码 2：lammps: no such file",
        ),
    ]
    for index, (preview, expected_code, expected_message) in enumerate(cases):
        context = _context(session=f"session-fail-{index}", run=f"run-fail-{index}")
        state: dict = {}
        for offset, record in enumerate([
            {"event": "run_start", "at": AT, "node_type": "orchestrator"},
            {"event": "tool_call", "at": AT, "turn": 1, "name": "run_bash", "args": {}},
            {"event": "tool_result", "at": AT, "turn": 1, "result_preview": preview},
        ]):
            await _ingest(service, execution_db, context, record, offset=offset, state=state)
        events = list(
            (
                await execution_db.execute(
                    select(ExecutionEvent).where(ExecutionEvent.session_id == context.session_id)
                )
            )
            .scalars()
            .all()
        )
        failed = [event for event in events if event.kind == "tool.failed"]
        assert len(failed) == 1, f"{preview!r} 没被判成失败"
        assert failed[0].payload["errorCode"] == expected_code
        assert failed[0].payload["errorMessage"].startswith(expected_message)
        # 崩了的那一类要带出事地点；驳回那一类没有 traceback 可言。
        if expected_code == "tool_exception":
            assert "run_node.py" in " ".join(failed[0].payload["errorTracebackTail"])
        else:
            assert "errorTracebackTail" not in failed[0].payload


@pytest.mark.asyncio
async def test_re_dispatching_the_same_node_gets_its_own_card(
    execution_db: AsyncSession,
) -> None:
    """同一个节点被再次派发 → 右栏一张新卡，不是往旧卡上追加。

    实测现场（wangd 2026-08-21）：observation 被派 3 次、_reviewer 4 次，右栏
    每种只有一张卡；新一轮的工具全部追加到上一次那张已标"完成"的卡上，而
    「当前」恒空 —— UI 说"当前没有节点在跑"而它正跑着。

    病根是 step id 取自**目录名**，而 harness 重新派发时复用同一个 run 目录。
    判据换成 transcript 的文件身份：新的一次派发是新文件，同一趟续跑是同一份
    文件 ——「这一次」和「这一趟」的分界本来就画在文件上。
    """
    from app.services.execution_ingest import _root_step_id

    run_id = "run_x::_orchestrator->observation@d1"
    first = _root_step_id(run_id, 1, {"dispatchKey": "file_aaa"})
    second = _root_step_id(run_id, 1, {"dispatchKey": "file_bbb"})
    assert first != second, "两次派发算出同一个 step id —— 第二次会被幂等吞掉"

    # 同一趟续跑：同一份文件 → 同一张卡。
    assert _root_step_id(run_id, 1, {"dispatchKey": "file_aaa"}) == first

    # 老 checkpoint 只有 childRunId（字段换名前存的）—— 读侧必须继续认它，
    # 否则平台重启后在飞的 run 会换一张卡片，同一趟裂成两半。
    legacy = _root_step_id(run_id, 1, {"childRunId": "1787296172-abc"})
    assert legacy == _root_step_id(run_id, 1, {"childRunId": "1787296172-abc"})
    assert legacy not in {first, second}

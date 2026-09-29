"""决策行只是运行时呈递的投影：一条 run 一次只有一张活卡，运行时说答了就是答了。

## 现场（2026-09-09 node20，run_24c909df）

三张 post-node 决策卡在库里全是 `pending`：

- 09:38 那张被替人点掉（运行时记了 `decision_answer_recorded`），但平台只在自己
  的 `accepted_responses` 计数够数时才肯改状态 —— 计数是 0，行永远 pending；
- observation 重跑后 09:47 的新卡是**另一个** producing run，旧卡按
  producingRunId 作用域没被顶掉；
- 前端于是一直把旧卡当活卡递给人，人点下去答的是一个早已不存在的 pause，
  运行时拒（offer_superseded），人看到的是空气泡加同一张卡。

两条规则，都是运行时的结构事实，不是名单：新呈递落地 = 同一条 run 上更早的
呈递不再可答；运行时给了终局 = 行有了终局（账面不许否决现场）。
"""
from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import StaticPool, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database import Base
from app.models.execution import Decision, DecisionStatus
from app.services.execution_ingest import ExecutionIngestService


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all, tables=[Decision.__table__])
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        yield session
    await engine.dispose()


def _decision(decision_id: str, *, producing_run_id: str, offer_id: str) -> Decision:
    return Decision(
        id=decision_id,
        tenant_id="t",
        workspace_id="w",
        project_id="p",
        session_id="s",
        run_id="run_x",
        attempt_no=1,
        subtype="post_node",
        prompt="Post-node decision",
        context={"producingRunId": producing_run_id, "offerId": offer_id},
        choices=[{"choiceId": "proceed", "label": "Continue"}],
        recommended_choice_id="proceed",
        authority_type="user",
        authority_subjects=["u"],
        required_approval_count=1,
        action_set_version="v1",
        policy_snapshot_id="policy",
        expires_at=None,
        status=DecisionStatus.PENDING,
    )


@pytest.mark.asyncio
async def test_a_new_presentation_retires_every_earlier_pending_card_on_the_run(db) -> None:
    old = _decision("d_old", producing_run_id="1788917217-bfe3f6", offer_id="a:p:o1")
    new = _decision("d_new", producing_run_id="1788918046-679c79", offer_id="b:p:o2")
    db.add_all([old, new])
    await db.flush()

    await ExecutionIngestService()._supersede_prior_presentations(db, decision=new)

    rows = {row.id: row for row in (await db.scalars(select(Decision))).all()}
    assert rows["d_old"].status == DecisionStatus.SUPERSEDED.value, (
        "不同 producing run 的旧卡也必须退役 —— 一条 run 同一时刻只有一张活卡"
    )
    assert rows["d_old"].context["supersededByDecisionId"] == "d_new"
    assert rows["d_new"].status == DecisionStatus.PENDING


@pytest.mark.asyncio
async def test_the_runtime_terminal_resolves_the_row_without_platform_bookkeeping(db) -> None:
    card = _decision("d1", producing_run_id="x", offer_id="x:p:o")
    db.add(card)
    await db.flush()
    assert card.accepted_responses in (None, [])

    event = SimpleNamespace(
        payload={"decisionId": "d1", "selectedChoiceId": "proceed", "terminal": True},
        occurred_at=datetime.now(UTC),
    )
    context = SimpleNamespace(tenant_id="t")
    await ExecutionIngestService()._observe_decision_resolution(db, event=event, context=context)

    assert card.status == DecisionStatus.RESOLVED
    assert card.selected_choice_id == "proceed"
    assert card.resolved_at is not None


@pytest.mark.asyncio
async def test_a_non_terminal_resolution_keeps_the_card_open(db) -> None:
    card = _decision("d1", producing_run_id="x", offer_id="x:p:o")
    db.add(card)
    await db.flush()
    event = SimpleNamespace(
        payload={"decisionId": "d1", "selectedChoiceId": "edit", "terminal": False},
        occurred_at=datetime.now(UTC),
    )
    await ExecutionIngestService()._observe_decision_resolution(
        db, event=event, context=SimpleNamespace(tenant_id="t")
    )
    assert card.status == DecisionStatus.PENDING


def test_the_presentation_identity_travels_into_the_decision_row() -> None:
    """`decision_package_presented` 带着 offer_id；投影不许把它丢掉。"""
    from app.services.execution_ingest import IngestContext, TranscriptAdapter

    context = IngestContext(
        tenant_id="t", workspace_id="w", project_id="p", session_id="s", run_id="run_x",
    )
    draft = TranscriptAdapter().adapt(
        {
            "event": "decision_package_presented",
            "at": datetime.now(UTC).isoformat(),
            "producing_run_id": "1788917217-bfe3f6",
            "decision_id": "1788917217-bfe3f6:p6e729f0e",
            "offer_id": "1788917217-bfe3f6:p6e729f0e:o41e10000",
            "prompt": "Post-node decision for observation",
            "recommended_action": "proceed",
        },
        context=context,
        adapter_state={},
        event_id="evt-1",
    )
    assert draft is not None and draft.kind == "decision.required"
    assert draft.payload["context"]["offerId"] == "1788917217-bfe3f6:p6e729f0e:o41e10000"

"""重呈递 = 新决定落地，旧呈递退场 —— 不是"篡改"，也不留幽灵。

## 现场（2026-08-17）

orchestrator 呈递 decision package → 框架判 curator 未完成 → 重跑 curator
四次 → **重新呈递**。旧实现里两次呈递派生出同一个 Decision.id，第二次条款
已变（带上了第四次 curator 的状态）→ 不可变快照守卫判"篡改"→ IngestError
抛穿 answer() → 整轮判死；用户重发又撞 resume 绑定校验 —— session 永久卡死。

## 修后的契约（本文件钉住的三件事）

1. 每次呈递带自己的 decision_id（harness 生成）→ 两次呈递是两个 Decision 行。
2. 新呈递落地时，同一个决定点（producingRunId 相同）的旧 pending 呈递被标
   `superseded`，不再出现在"当前可答"集合里。
3. 不同决定点（别的 producing run）的 pending 决定不受影响 —— 作用域别扩大。
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

from app.models.execution import Decision, DecisionStatus
from app.services.execution_ingest import (
    DecisionAuthoritySnapshot,
    ExecutionIngestService,
)

from .test_execution_foundation import AT, _context, _ingest, execution_db  # noqa: F401

pytestmark = pytest.mark.asyncio


def _presented(producing_run_id: str, presentation: str, *, prompt_suffix: str = "") -> dict:
    return {
        "event": "decision_package_presented",
        "at": AT,
        "producing_run_id": producing_run_id,
        "decision_id": f"{producing_run_id}:p{presentation}",
        "source_node_type": "analysis",
        "recommended_action": "proceed",
        "review_failed": False,
        "context": {"summary": f"ready{prompt_suffix}"},
    }


def _authority() -> DecisionAuthoritySnapshot:
    return DecisionAuthoritySnapshot(
        authority_type="initiating_user",
        authority_subjects=("initiator",),
        required_approval_count=1,
        action_set_version="normal-v1",
        policy_snapshot_id="policy-snapshot-a",
    )


async def test_a_represented_package_is_a_new_decision_and_retires_the_old_one(
    execution_db,  # noqa: F811
) -> None:
    service = ExecutionIngestService()
    context = _context(authority=_authority(), actor="initiator")

    first = await _ingest(
        service, execution_db, context, _presented("producer-a", "11111111"), offset=10
    )
    assert first.event is not None and first.event.kind == "decision.required"

    # 另一个决定点的呈递：它落地那一刻 a 就不再可答了（run 上只有一个 pause）。
    bystander = await _ingest(
        service, execution_db, context, _presented("producer-z", "zzzzzzzz"), offset=20
    )
    assert bystander.event is not None and bystander.event.kind == "decision.required"

    # 同一个决定点的重呈递（条款已变）—— 不是篡改，是新决定。
    second = await _ingest(
        service,
        execution_db,
        context,
        _presented("producer-a", "22222222", prompt_suffix=" (curator rerun)"),
        offset=30,
    )
    assert second.event is not None and second.event.kind == "decision.required"

    decisions = (await execution_db.scalars(select(Decision))).all()
    by_id = {d.id: d for d in decisions}
    assert len(decisions) == 3

    first_id = str(first.event.payload["decisionId"])
    second_id = str(second.event.payload["decisionId"])
    bystander_id = str(bystander.event.payload["decisionId"])
    assert first_id != second_id

    # 呈递顺序 a → z → a'：z 落地时 a 退场，a' 落地时 z 退场；活卡永远只有最新那张。
    assert by_id[first_id].status == DecisionStatus.SUPERSEDED
    assert by_id[first_id].context["supersededByDecisionId"] == bystander_id
    assert by_id[bystander_id].status == DecisionStatus.SUPERSEDED
    assert by_id[bystander_id].context["supersededByDecisionId"] == second_id
    assert by_id[second_id].status == DecisionStatus.PENDING


async def test_replaying_the_same_presentation_does_not_supersede_itself(
    execution_db,  # noqa: F811
) -> None:
    """幂等重放（同一字节位置）不得制造第二个决定，也不得动第一个的状态。"""
    service = ExecutionIngestService()
    context = _context(authority=_authority(), actor="initiator")
    raw = _presented("producer-a", "11111111")

    first = await _ingest(service, execution_db, context, raw, offset=10)
    replay = await _ingest(service, execution_db, context, raw, offset=10)
    assert replay.duplicate is True

    decisions = (await execution_db.scalars(select(Decision))).all()
    assert len(decisions) == 1
    assert decisions[0].status == DecisionStatus.PENDING
    assert first.event is not None


async def test_ingest_never_spans_a_nested_transaction(execution_db) -> None:  # noqa: F811
    """摄取一条记录不许跨调用持有 SAVEPOINT —— 成功和拒收两条路都要干净。

    2026-08-17 事故：为了"拒收时回滚到记录开始前"，把严格路径包进了
    `db.begin_nested()`。SSE 观察者断开后这一轮转入后台继续跑，请求级 session
    随断开被拆掉，而 savepoint 正好跨在那上面 ——

        OperationalError: no such savepoint: sa_savepoint_1

    摄取当场抛错、run 永远到不了 completed（三次挂一次）。防的那件事其实不会
    发生：所有 RecordRejectedError 都在任何 db.add 之前抛。这条测试锁住"不许
    再加回来"，而不是锁某段实现长什么样。
    """
    service = ExecutionIngestService()
    context = _context(authority=_authority(), actor="initiator")

    ok = await _ingest(
        service, execution_db, context, _presented("producer-a", "11111111"), offset=10
    )
    assert ok.event is not None
    assert execution_db.in_nested_transaction() is False, "成功路径留下了未关闭的 SAVEPOINT"

    # 拒收路径：同一决定点、同一 decision_id、条款已变 → 不可变快照守卫拒收。
    rejected = await _ingest(
        service,
        execution_db,
        context,
        {**_presented("producer-a", "11111111"), "prompt": "changed after freeze"},
        offset=20,
    )
    assert rejected.event is not None and rejected.event.kind == "record.rejected"
    assert execution_db.in_nested_transaction() is False, "拒收路径留下了未关闭的 SAVEPOINT"

    # 拒收之后照常还能继续摄取（turn 活着）。
    after = await _ingest(
        service, execution_db, context, _presented("producer-b", "33333333"), offset=30
    )
    assert after.event is not None and after.event.kind == "decision.required"

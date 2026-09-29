"""一个正在跑的 run，没有「挂起的询问」可丢。

## 病例（2026-09-07 真机第五轮）

那一轮我给会话发了 7 条东西：一次决策选项（PROCEED + 附言）、三次高危批准、
一次插话、一次拒绝（附现场证据）、一次再批准。**七条条条生效** —— 模型按选项
走了、作业跑了、取消执行了、被拒绝的那次没执行。

而这七次里，每一次流的第一条事件都是：

    {"type":"progress","event":"pause.abandoned",
     "detail":"之前那个待回答的问题随运行时一起丢了，已作废。…"}

什么都没丢。这句话的作者在代码里写着「静默作废一个用户正看着的问题 = 他以为
自己答上了，其实没有」—— 意图完全正确，触发条件却错了，于是它变成一句**吓人
的假话**，而且说在用户最需要相信这套东西的时刻。

## 真因：拿错了词表

`assert_conversation_runtime_available` 的 docstring 是
"Fail with a recoverable conflict when a live pause was lost" —— 它问的是**挂起
的那个询问**还在不在。但候选集取的是 `REQUIRES_LIVE_RUNTIME_STATUSES`，那个集合
回答的是**另一个问题**：谁以有活进程为前提。`RUNNING` 在里面。

于是顶层 run 一边正常跑着，函数一边走到最后那句无条件 `raise`：

    if (run.status == WAITING_HUMAN and binding and …):   # 只有这一条会 return
        return
    if run.status == WAITING_HUMAN:
        await mark_orphaned_harness_runs(db)
    raise HarnessSessionStaleError(...)                    # 其余一律抛

`running` 既不满足 return 的条件、也不进 orphan 分支，直接抛。

「谁以有活进程为前提」和「谁停在等人上」是两个问题；进程死活由 `run_liveness`
现算（D11），不在这儿判。
"""
from __future__ import annotations

import uuid

import pytest

from app.models.execution import (
    PARKED_WAITING_FOR_HUMAN,
    REQUIRES_LIVE_RUNTIME_STATUSES,
    Run,
    RunStatus,
)
from app.services.harness_sessions import (
    HarnessSessionStaleError,
    assert_conversation_runtime_available,
)


def _a_run(session_id: str, project_id: str, status: RunStatus) -> Run:
    return Run(
        id=f"run_{uuid.uuid4().hex}",
        tenant_id="t",
        workspace_id="w",
        project_id=project_id,
        session_id=session_id,
        node_type="project_chat",
        status=status.value,
        prompt_tokens=0,
        completion_tokens=0,
        total_tokens=0,
        usage_coverage="partial",
        retry_count=0,
        summary={"executionKernel": "formal_harness"},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status",
    sorted(REQUIRES_LIVE_RUNTIME_STATUSES - PARKED_WAITING_FOR_HUMAN, key=lambda s: s.value),
)
async def test_a_run_that_is_not_parked_is_not_a_lost_pause(db_session, status) -> None:
    """在跑 / 排队 / 派发中 / 重试中 —— 都没有挂起的询问，都不该报「丢了」。"""
    session_id, project_id = str(uuid.uuid4()), str(uuid.uuid4())
    db_session.add(_a_run(session_id, project_id, status))
    await db_session.flush()

    await assert_conversation_runtime_available(
        db_session, project_id=project_id, conversation_id=session_id, user_id="u1")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status", sorted(PARKED_WAITING_FOR_HUMAN, key=lambda s: s.value))
async def test_a_parked_pause_with_no_live_binding_still_reports_lost(db_session, status) -> None:
    """反过来那一半必须还在：停在等人上、而绑定没了，就是真丢了。

    没有这条，把上面那个判据放宽成"永远别抛"也能全绿 —— 而那会让「你以为答上了
    其实没有」重新变成静默失败。
    """
    session_id, project_id = str(uuid.uuid4()), str(uuid.uuid4())
    db_session.add(_a_run(session_id, project_id, status))
    await db_session.flush()

    with pytest.raises(HarnessSessionStaleError):
        await assert_conversation_runtime_available(
            db_session, project_id=project_id, conversation_id=session_id, user_id="u1")


def test_the_two_vocabularies_are_not_the_same_question() -> None:
    """「以有活进程为前提」严格宽于「停在等人上」—— 差集非空才有这条 bug 的空间。

    这条钉住的是**为什么**不能拿前者当后者用：哪天两个集合真的相等了，上面那些
    参数化用例会变成空跑，而这条会红。
    """
    assert PARKED_WAITING_FOR_HUMAN < REQUIRES_LIVE_RUNTIME_STATUSES
    assert REQUIRES_LIVE_RUNTIME_STATUSES - PARKED_WAITING_FOR_HUMAN

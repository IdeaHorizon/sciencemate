"""恢复资格按"现在还能不能接着跑"判，不按"当初怎么死的"判。

原判据要求 staleReason == 'app_server_restart' —— 只认一种死法。harness 子
进程被 kill / OOM 时标签对不上，于是明明恢复不了的 session 被拒绝恢复：
聊天端点已经 409 并把 recover 链接指给用户，用户点了却得到
session_not_recoverable（E2E v13 实测：9 个 MD 全跑完、数据在磁盘上，
卡在这里进不去汇总）。同一个事实两处各判各的，必然对不上。

本文件曾经全是 inspect.getsource 的**文本断言**，结果：判据函数的 import
写错了模块（local_execution 而非 harness_sessions），运行时必 ImportError，
而这些文本断言**全绿** —— 测的是"源码里写了这个词"，不是"这条路走得通"。
现在一律走真入口 recover_stale_session，用真 DB 对象。
"""

import uuid

import pytest
from fastapi import HTTPException

from app.models.execution import Run, SessionProjection
from app.models.project import Project
from app.models.user import User
from app.services import sessions as sessions_service
from app.services.harness_sessions import HarnessSessionStaleError

_OMIT = object()

TENANT = "t1"


async def _fixture(db, *, stale_reason: str, execution_kernel="formal_harness"):
    """一个 stale 的 formal_harness session；**故意不给 git 基线**。

    资格门放行后紧接着就是 recovery_context_incomplete 检查 —— 用它当哨兵，
    可以在不建 Git 仓的前提下区分"被资格门拦下"和"资格门放行了"。
    """
    user = User(
        id=str(uuid.uuid4()),
        email=f"u-{uuid.uuid4().hex[:6]}",
        hashed_password="x",
        display_name="u",
        role="researcher",
        institution_id="ieit",
    )
    project = Project(
        id=str(uuid.uuid4()),
        name="p",
        owner_id=user.id,
    )
    session_id = str(uuid.uuid4())
    projection = SessionProjection(
        tenant_id=TENANT,
        workspace_id="w",
        project_id=str(project.id),
        session_id=session_id,
        initiating_user_id=user.id,
        title="s",
        created_by_user_id=user.id,
        lifecycle_status="active",
    )
    run = Run(
        id=str(uuid.uuid4()),
        tenant_id=TENANT,
        workspace_id="w",
        project_id=str(project.id),
        session_id=session_id,
        status="stale_unknown",
        summary={
            **({} if execution_kernel is _OMIT else {"executionKernel": execution_kernel}),
            "staleReason": stale_reason,
            "resumable": False,
        },
    )
    db.add_all([user, project, projection, run])
    await db.flush()
    return user, project, session_id


async def _recover(db, user, project, session_id):
    return await sessions_service.recover_stale_session(
        db, user=user, project=project, source_session_id=session_id
    )


def _detail_code(exc: HTTPException) -> str:
    return exc.detail["code"] if isinstance(exc.detail, dict) else str(exc.detail)


@pytest.mark.asyncio
async def test_runtime_lost_with_unlabelled_death_is_recoverable(db_session, monkeypatch):
    """死因标签不是 app_server_restart，但运行时确实接不上 → 必须放行。

    这就是 E2E v13 卡住的现场：harness 子进程没了，标签对不上，用户被
    409 指过来又被 409 挡回去。
    """
    user, project, session_id = await _fixture(db_session, stale_reason="harness_process_gone")

    async def probe(*args, **kwargs):
        raise HarnessSessionStaleError("harness runtime is gone")

    monkeypatch.setattr(
        "app.services.harness_sessions.assert_conversation_runtime_available", probe
    )

    with pytest.raises(HTTPException) as caught:
        await _recover(db_session, user, project, session_id)
    # 被资格门放行了，栽在下一道（缺 base revision）——这正是我们要的证据
    assert _detail_code(caught.value) == "recovery_context_incomplete"


@pytest.mark.asyncio
async def test_live_runtime_is_not_recoverable(db_session, monkeypatch):
    """还能接着跑 → 不给恢复（否则平白开出一条分叉）。"""
    user, project, session_id = await _fixture(db_session, stale_reason="harness_process_gone")

    async def probe(*args, **kwargs):
        return None  # 活着

    monkeypatch.setattr(
        "app.services.harness_sessions.assert_conversation_runtime_available", probe
    )

    with pytest.raises(HTTPException) as caught:
        await _recover(db_session, user, project, session_id)
    assert _detail_code(caught.value) == "session_not_recoverable"


@pytest.mark.asyncio
async def test_legacy_app_server_restart_still_recoverable_without_probe(db_session, monkeypatch):
    """老路径不依赖活性探测：探测说"活着"也照旧认 app_server_restart 标记。"""
    user, project, session_id = await _fixture(db_session, stale_reason="app_server_restart")

    async def probe(*args, **kwargs):
        return None

    monkeypatch.setattr(
        "app.services.harness_sessions.assert_conversation_runtime_available", probe
    )

    with pytest.raises(HTTPException) as caught:
        await _recover(db_session, user, project, session_id)
    assert _detail_code(caught.value) == "recovery_context_incomplete"


@pytest.mark.asyncio
async def test_probe_blowing_up_is_fail_closed(db_session, monkeypatch):
    """判据自身出错（含 import 写错这种）→ 不放行，而不是默认放行。"""
    user, project, session_id = await _fixture(db_session, stale_reason="harness_process_gone")

    async def probe(*args, **kwargs):
        raise RuntimeError("probe itself is broken")

    monkeypatch.setattr(
        "app.services.harness_sessions.assert_conversation_runtime_available", probe
    )

    with pytest.raises(HTTPException) as caught:
        await _recover(db_session, user, project, session_id)
    assert _detail_code(caught.value) == "session_not_recoverable"


@pytest.mark.asyncio
async def test_a_run_without_execution_kernel_is_still_recoverable(db_session, monkeypatch):
    """`executionKernel` 没写过 ≠ 不是本平台跑的。

    这个字段在很多 run 上根本没被写进 summary。恢复判据原来要求它 **等于**
    "formal_harness"，于是那些 run 永远通不过 —— 恢复入口对它们等于不存在。

    2026-08-22 实测：一条 writing run 的 worker 死在 `running` 上，平台连续
    见证了 6 次（summary.runtimeWitness 6 条，条条 app_server_restart /
    observedStatus=running），6 次都没能按自己的见证放行恢复，只因为这个字段
    是 None。同时 cancel 那一侧正确地拒绝伪造它够不到的运行时状态 —— 两道闸
    各自都对，合起来把 run 永久焊死。

    `mark_orphaned_harness_runs` 早就把同一个 bug 修成了"只用来排除别的内核"。
    这条测试钉住两边同款。
    """
    user, project, session_id = await _fixture(
        db_session, stale_reason="app_server_restart", execution_kernel=_OMIT
    )

    async def probe(*args, **kwargs):
        return None  # 探针说"接得上" —— 老路径不依赖它

    monkeypatch.setattr(
        "app.services.harness_sessions.assert_conversation_runtime_available", probe
    )

    with pytest.raises(HTTPException) as caught:
        await _recover(db_session, user, project, session_id)
    # 走到 recovery_context_incomplete 就说明**恢复资格这道门过了**
    # （这个 fixture 故意没给 git 基线）。要是资格没过，
    # 报的会是 session_not_recoverable。
    assert _detail_code(caught.value) == "recovery_context_incomplete"


@pytest.mark.asyncio
async def test_another_kernel_is_still_excluded(db_session, monkeypatch):
    """放宽不等于放弃：明确标着别的内核的 run 仍然不归这条路管。"""
    user, project, session_id = await _fixture(
        db_session, stale_reason="app_server_restart", execution_kernel="local_demo"
    )

    async def probe(*args, **kwargs):
        return None

    monkeypatch.setattr(
        "app.services.harness_sessions.assert_conversation_runtime_available", probe
    )

    with pytest.raises(HTTPException) as caught:
        await _recover(db_session, user, project, session_id)
    assert _detail_code(caught.value) == "session_not_recoverable"

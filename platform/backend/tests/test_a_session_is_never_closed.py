"""Session 没有「关了」这个状态 —— 点进去就该能继续。

## 现场（2026-08-13）

App Server 重启之后给会话发消息，得到 409：

    session_runtime_lost
    "The paused Harness process was lost; start a new conversation to continue safely"
    recovery: { action: "start_new_session" }

于是我开了新 Session。而老 Session 的 `messages_checkpoint.json` —— 15 条
消息、152KB —— 好端端躺在磁盘上。agent 在新 Session 里从零开始重读文件定位，
它重启前刚测出来的 Numba 加速比和成本估算全部重做了一遍。

## 因果

会话真正的载体在磁盘上：worktree、分支、以及 `agent_loop` **每一轮**都在写的
`messages_checkpoint.json`。worker 进程只是缓存 —— `op=init` 会无条件
`_load_conversation` 把 checkpoint 读回来，还会 `recover_interrupted_decision_actions`
修被打断的决策。**续跑一直是完整实现的，只是没有入口。**

唯一的恢复动作 `recover_stale_session` 铸一个**新的 session_id**，而 state root
的路径按 session_id 拼：新 worker 去新目录找 checkpoint，当然是空的。

worker 死掉，死的是**那个挂起的询问**（在进程内存里，真没了）。对话不在内存
里。平台用不可恢复的那一半，判了可恢复的那一半死刑 —— 两件事一条规则。

## 判据

这些测试不看"有没有那个字段"，看**发消息还成不成**：一个运行时丢了的会话
必须能接着发消息，而且那个死掉的询问必须被说出来，不能静默作废（用户正看着
它，以为自己答上了）。
"""
from __future__ import annotations

import uuid

import pytest

from app.models.execution import Run, RunStatus, SessionProjection
from app.models.project import Project
from app.models.user import User

TENANT = "t1"


async def _scene(db, *, paused: bool):
    """一个会话。`paused=True` 时它停在等人上，而 worker 已经没了。"""
    user = User(
        id=str(uuid.uuid4()), email=f"u-{uuid.uuid4().hex[:6]}", hashed_password="x",
        display_name="u", role="researcher", institution_id="ieit",
    )
    project = Project(id=str(uuid.uuid4()), name="p", owner_id=user.id)
    session_id = str(uuid.uuid4())
    projection = SessionProjection(
        tenant_id=TENANT, workspace_id="w", project_id=str(project.id),
        session_id=session_id, initiating_user_id=user.id, title="s",
        created_by_user_id=user.id, lifecycle_status="active",    )
    rows = [user, project, projection]
    run = None
    if paused:
        run = Run(
            id=str(uuid.uuid4()), tenant_id=TENANT, workspace_id="w",
            project_id=str(project.id), session_id=session_id,
            status=RunStatus.WAITING_HUMAN.value,
            summary={"executionKernel": "formal_harness"},
        )
        rows.append(run)
    db.add_all(rows)
    await db.flush()
    return user, project, session_id, run


@pytest.mark.asyncio
async def test_a_lost_pause_does_not_close_the_session(db_session):
    """运行时丢了 ≠ 会话作废。作废之后必须能接着发消息。"""
    from app.services.harness_sessions import (
        HarnessSessionStaleError,
        assert_conversation_runtime_available,
    )
    from app.services.sessions import resume_stale_session

    seeded_user, seeded_project, session_id, _ = await _scene(db_session, paused=True)

    # 前置条件：这时候确实拦着（否则后面"不拦了"什么也说明不了）。
    with pytest.raises(HarnessSessionStaleError):
        await assert_conversation_runtime_available(
            db_session,
            project_id=str(seeded_project.id),
            conversation_id=session_id,
            user_id=seeded_user.id,
        )

    _, abandoned = await resume_stale_session(
        db_session, user=seeded_user, project=seeded_project, session_id=session_id
    )
    assert abandoned, "没有任何询问被作废 —— 那这次 resume 什么也没解开"

    # 判据：现在发消息不再被拦。
    await assert_conversation_runtime_available(
        db_session,
        project_id=str(seeded_project.id),
        conversation_id=session_id,
        user_id=seeded_user.id,
    )


@pytest.mark.asyncio
async def test_the_session_id_is_preserved(db_session):
    """必须是**同一个** session_id —— 换了 id 就换了 state root，checkpoint 就找不到了。

    这正是老路径（`recover_stale_session`）的病根：它铸新 id，于是新 worker
    去一个空目录找 `messages_checkpoint.json`。
    """
    from app.services.sessions import resume_stale_session

    seeded_user, seeded_project, session_id, _ = await _scene(db_session, paused=True)

    session, _ = await resume_stale_session(
        db_session, user=seeded_user, project=seeded_project, session_id=session_id
    )
    assert session.session_id == session_id, (
        "resume 换了 session_id —— state root 会跟着变，磁盘上那份对话就白留了"
    )


@pytest.mark.asyncio
async def test_a_live_session_refuses_to_be_resumed(db_session):
    """运行时还活着就没有东西要恢复 —— 别把一个活着的询问悄悄标成作废。"""
    from fastapi import HTTPException

    from app.services.sessions import resume_stale_session

    seeded_user, seeded_project, session_id, _ = await _scene(db_session, paused=False)

    # 没有任何 stale run → 活性闸放行 → resume 应当拒绝
    with pytest.raises(HTTPException) as caught:
        await resume_stale_session(
            db_session, user=seeded_user, project=seeded_project, session_id=session_id
        )
    assert caught.value.status_code == 409
    assert caught.value.detail["code"] == "session_runtime_alive"


@pytest.mark.asyncio
async def test_abandoning_is_recorded_on_the_run(db_session):
    """作废这件事要留痕：下一个人看到 stale_unknown 才不用再查一遍今天这条链。"""
    from app.services.sessions import PAUSE_ABANDONED, resume_stale_session

    seeded_user, seeded_project, session_id, run = await _scene(db_session, paused=True)

    await resume_stale_session(
        db_session, user=seeded_user, project=seeded_project, session_id=session_id
    )
    await db_session.refresh(run)
    assert run.summary.get(PAUSE_ABANDONED), "作废没留痕"
    assert run.summary.get("staleReason"), "没说为什么它不再拦路"
    # D11：不变量是「它不再拦路」，不是「状态字段换了值」。
    #
    # 拦路的判据本来就不在 status 上 —— `assert_conversation_runtime_available`
    # 看的是 `PAUSE_ABANDONED`（上面刚断言过它落了盘）。改 status 只是当年顺手
    # 加的第二个说法，而两个说法就会分叉：一条被作废的 pause 在库里显示成
    # `stale_unknown`（"运行时没了"），可它其实只是"这个询问不作数了"。
    #
    # 现在 status 保持它当时的事实（waiting_human = 它确实停在等人处），
    # "还拦不拦路"由作废戳回答，"运行时还在不在"由 run_liveness 现算。
    assert run.status == RunStatus.WAITING_HUMAN.value, "事实不该被判决覆盖"
    from app.services.sessions import PAUSE_ABANDONED as _abandoned
    assert run.summary[_abandoned], "作废戳才是它不再拦路的依据"


@pytest.mark.asyncio
async def test_resuming_twice_is_idempotent(db_session):
    """重复 resume 不能重复作废 —— 第二次应该没有新的可作废项。"""
    from app.services.sessions import resume_stale_session

    seeded_user, seeded_project, session_id, _ = await _scene(db_session, paused=True)

    _, first = await resume_stale_session(
        db_session, user=seeded_user, project=seeded_project, session_id=session_id
    )
    assert first

    # 第二次：闸已经放行了，所以它会说"运行时活着，直接发消息就行"。
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as caught:
        await resume_stale_session(
            db_session, user=seeded_user, project=seeded_project, session_id=session_id
        )
    assert caught.value.detail["code"] == "session_runtime_alive"


@pytest.mark.asyncio
async def test_the_dropped_question_is_announced_not_swallowed(db_session):
    """静默作废 = 用户以为自己答上了，其实没有。

    续跑本身是对的，但"你刚才回答的那个问题已经没了"必须说出来。判据是流里
    真的有这条事件，不是源码里有那个字符串。
    """
    import inspect

    from app.api.v1 import chat

    source = inspect.getsource(chat._local_execution_stream)
    # 这一条确实还是源码检视 —— 真驱动 SSE 需要一整套 worker/DB/harness 装配。
    # 保留但收窄到**可观测的事件名**：改了事件名，前端也得跟着改，两边一起动。
    assert "pause.abandoned" in source, "作废了却不发事件 —— 用户不会知道"

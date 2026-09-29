"""子节点的事件要记在**它自己**的 run 名下，不是一股脑挂在顶层。

## 现场（wangd 2026-08-11 试用）

> 「它现在所有的这个显示的子节点的这些进程…它只在最下面显示。这 literature
>   都结束了，然后开始 hypothesis 了，它还是在下面显示一大坨。」

UI 上是一坨 `Research activity — 124 recorded actions`，literature 的、
_reviewer 的、hypothesis 的动作全糊在一起，看不出哪个属于谁、谁先谁后。

## 不是没摄取，是没归属

实测（会话 bc1c7343）：

    不同 transcript 文件：6 个   ← 子节点的**都进来了**
    不同 run_id：       1 个    ← 但全记在顶层那条 run 上

而子节点 transcript 的**第一条** `run_start` 身份是全的：

    node_type: literature
    depth: 1
    sub_run_id: _orchestrator->literature@d1
    parent_run_id: orchestrator__<project>__session__<session>

**后续事件不带**（只有 tenant/session）—— 所以归属信息只在每个文件的第一行，
而摄取器是按 `file_identity` 逐行走的，adapter_state 本来就按文件保存。
把第一行的身份记进去，同一文件的后续事件就都能归位。

UI 分组、跑完收起、一行结论 —— 全都建立在这个归属上。库里分不开，前端再怎么
渲染也分不开。
"""
from __future__ import annotations

import json

import pytest
from sqlalchemy import select



def _run_start(node_type: str, sub_run_id: str, parent: str) -> dict:
    return {
        "event": "run_start",
        "at": "2026-08-11T10:00:00+00:00",
        "node_type": node_type,
        "depth": 1,
        "sub_run_id": sub_run_id,
        "parent_run_id": parent,
        "tenant_id": "t",
        "session_id": "s-attr",
    }


def test_the_first_line_carries_the_childs_identity() -> None:
    """从 run_start 认出"这份 transcript 是谁的"。"""
    from app.services.execution_ingest import child_run_identity

    ident = child_run_identity(_run_start("literature", "_orchestrator->literature@d1", "orc-1"))
    assert ident is not None
    assert ident.node_type == "literature"
    assert ident.run_id == "_orchestrator->literature@d1"
    assert ident.parent_run_id == "orc-1"


def test_a_top_level_transcript_has_no_child_identity() -> None:
    """顶层自己的 transcript 不该被当成子节点（没有 depth / parent）。"""
    from app.services.execution_ingest import child_run_identity

    assert child_run_identity({"event": "run_start", "node_type": "_orchestrator"}) is None
    assert child_run_identity({"event": "platform_session_start"}) is None


def test_a_later_event_without_identity_inherits_from_the_file() -> None:
    """后续事件只带 tenant/session —— 身份从同一文件的第一行继承。

    这正是"归属信息只在第一行"这个事实的处理方式：adapter_state 按文件存，
    记一次，同文件后续都能归位。
    """
    from app.services.execution_ingest import child_run_identity, remember_child_identity

    state: dict = {}
    remember_child_identity(state, _run_start("literature", "lit-1", "orc-1"))

    later = {"event": "tool_call", "tenant_id": "t", "session_id": "s-attr"}
    assert child_run_identity(later) is None          # 事件自己没有
    ident = remember_child_identity(state, later)     # 但文件记得
    assert ident is not None and ident.run_id == "lit-1"


def test_two_files_do_not_leak_into_each_other() -> None:
    """两个子节点各有各的 adapter_state —— 串了就等于分组全错。"""
    from app.services.execution_ingest import remember_child_identity

    lit: dict = {}
    hyp: dict = {}
    remember_child_identity(lit, _run_start("literature", "lit-1", "orc-1"))
    remember_child_identity(hyp, _run_start("hypothesis", "hyp-1", "orc-1"))

    assert remember_child_identity(lit, {"event": "tool_call"}).run_id == "lit-1"
    assert remember_child_identity(hyp, {"event": "tool_call"}).run_id == "hyp-1"


@pytest.mark.asyncio
async def test_child_events_land_under_the_child_run(db_session) -> None:
    """接线：走真实摄取，库里子节点的事件带自己的 run_id 和 parent_run_id。

    没有这一条，前端永远分不了组 —— 这是整条链的根。
    """
    from app.models.execution import ExecutionEvent
    from app.services.execution_ingest import ExecutionIngestService, IngestContext

    service = ExecutionIngestService()
    ctx = IngestContext(
        tenant_id="t", workspace_id="w", project_id="p",
        session_id="s-attr", run_id="orc-1",
        actor_user_id=None, decision_authority=None,
    )
    state: dict = {}
    for offset, raw in enumerate([
        _run_start("literature", "lit-1", "orc-1"),
        {"event": "tool_call", "at": "2026-08-11T10:00:01+00:00",
         "name": "search_papers", "args": {}, "tool_call_id": "c1"},
    ]):
        await service.ingest_raw_record(
            db_session, context=ctx, file_identity="lit-file", byte_offset=offset * 100,
            raw_line=json.dumps(raw, ensure_ascii=False).encode(),
            raw=raw, adapter_state=state,
        )

    rows = (await db_session.execute(
        select(ExecutionEvent.run_id, ExecutionEvent.parent_run_id)
        .where(ExecutionEvent.session_id == "s-attr")
    )).all()
    assert rows, "一条都没落库"
    # id 用父 run 限定过（`<父>/<子>`）—— harness 的 sub_run_id 只在会话内唯一，
    # 平台的 Run.id 要全局唯一，见 `test_two_sessions_can_each_run_the_same_node`。
    assert any(r.run_id == "orc-1::lit-1" for r in rows), (
        f"子节点事件没归到自己名下：{[(r.run_id, r.parent_run_id) for r in rows]}"
    )
    assert any(r.parent_run_id == "orc-1" for r in rows), "父子关系没记上"


@pytest.mark.asyncio
async def test_the_parent_is_the_platform_run_not_the_harness_orchestrator_name(db_session) -> None:
    """父 run 取**平台**这一侧的 id —— transcript 里那个是另一个标识空间。

    ## 现场（2026-08-12）

    子节点事件的 `parent_run_id` 存的是 harness 自己的编排器名：

        orchestrator__467e24b5-…__session__bc1c7343-…

    而平台的 run id 长这样：`run_357d167bcae7408f…`。两个空间名字都像 id，
    含义不同，**分叉时不报错** —— 直到有人拿它做连接：

        GET /sessions/{id}/events?runId=run_357d…&includeChildren=true
        → `parent_run_id == run_357d…` 永远不成立 → 一个子节点都取不到

    UI 上的症状是：改了半天，会话里还是只有编排器自己那一坨 197 条动作。
    """
    import json

    from app.models.execution import ExecutionEvent
    from app.services.execution_ingest import ExecutionIngestService, IngestContext

    service = ExecutionIngestService()
    ctx = IngestContext(
        tenant_id="t", workspace_id="w", project_id="p",
        session_id="s-parent-space", run_id="run_platform_side",
        actor_user_id=None, decision_authority=None,
    )
    state: dict = {}
    raw = {
        "event": "run_start", "at": "2026-08-11T10:00:00+00:00",
        "node_type": "literature", "depth": 1,
        "sub_run_id": "_orchestrator->literature@d1",
        # harness 自己的父名 —— 平台侧不存在这个 id
        "parent_run_id": "orchestrator__proj__session__sess",
        "tenant_id": "t", "session_id": "s-parent-space",
    }
    await service.ingest_raw_record(
        db_session, context=ctx, file_identity="lit", byte_offset=0,
        raw_line=json.dumps(raw, ensure_ascii=False).encode(),
        raw=raw, adapter_state=state,
    )

    rows = (await db_session.execute(
        select(ExecutionEvent.run_id, ExecutionEvent.parent_run_id)
        .where(ExecutionEvent.session_id == "s-parent-space")
    )).all()
    assert rows
    assert all(r.parent_run_id == "run_platform_side" for r in rows), (
        f"父 run 用了 harness 的名字，平台侧连不上：{[(r.run_id, r.parent_run_id) for r in rows]}"
    )
    assert not any("orchestrator__" in (r.parent_run_id or "") for r in rows)


@pytest.mark.asyncio
async def test_two_sessions_can_each_run_the_same_node(db_session) -> None:
    """同一个节点在两个会话里各跑一次，不能撞 id。

    ## 现场（2026-08-12，恢复路径上炸的）

    harness 的 `sub_run_id`（`_orchestrator->_curator@d1`）是它在**本次调用树
    里的位置** —— 会话内唯一。平台的 `Run.id` 要求全局唯一。直接拿来用：

        会话 A 跑 curator → Run 行 `_orchestrator->_curator@d1` 绑在会话 A
        会话 B 跑 curator → 同名 → IngestError: Run identity conflicts…

    而这恰好发生在**恢复**路径上：打断 → recover 开新会话 → 重跑同一批节点 →
    第一条 run 当场失败。恢复机制形同虚设。

    今晚第三次「两个标识空间被当成一个」，这次是我自己引入的。
    """
    import json

    from app.models.execution import ExecutionEvent
    from app.services.execution_ingest import ExecutionIngestService, IngestContext

    service = ExecutionIngestService()
    raw = {
        "event": "run_start", "at": "2026-08-12T10:00:00+00:00",
        "node_type": "_curator", "depth": 1,
        "sub_run_id": "_orchestrator->_curator@d1",
        "parent_run_id": "orchestrator__p__session__whatever",
    }
    for session_id, run_id in (("s-first", "run_first"), ("s-second", "run_second")):
        ctx = IngestContext(
            tenant_id="t", workspace_id="w", project_id="p",
            session_id=session_id, run_id=run_id,
            actor_user_id=None, decision_authority=None,
        )
        await service.ingest_raw_record(
            db_session, context=ctx, file_identity=f"{session_id}-curator", byte_offset=0,
            raw_line=json.dumps(raw, ensure_ascii=False).encode(),
            raw=raw, adapter_state={},
        )

    rows = (await db_session.execute(
        select(ExecutionEvent.session_id, ExecutionEvent.run_id)
        .where(ExecutionEvent.session_id.in_(["s-first", "s-second"]))
    )).all()
    by_session = {r.session_id: r.run_id for r in rows}
    assert len(by_session) == 2, f"两个会话都得落库：{by_session}"
    assert by_session["s-first"] != by_session["s-second"], (
        f"两个会话的同名子节点用了同一个 run id：{by_session}"
    )
    for run_id in by_session.values():
        assert "_curator" in run_id, f"id 里得看得出是谁：{run_id}"


@pytest.mark.asyncio
async def test_a_finished_child_run_is_recorded_as_finished(db_session) -> None:
    """子 run 跑完了，它**自己那一行**就得写上终态。

    ## 现场（2026-08-31，本机跑真课题）

    experiment 节点跑到 turn 153、1760 万 tokens、产物全部 frozen，
    `run.completed{"missingRequiredOutputs": []}` 也发出来了 —— 但它自己那条
    Run 行永远停在 `queued`，连 RunAttempt 行都没建出来。随后被清扫判成
    `stale_unknown`，界面显示「Interrupted / Did not finish」。
    用户看到的是"这趟白跑了"，磁盘上却是一次完整成功的实验。

    ## 根因：一条规则管了两件事

    `owningRun` 闸原来是整条 `return`：非 owning 的生命周期事件一个字都不写。
    它要防的是「子节点的终态关掉**父** attempt」（真实事故），但顺手把
    「子 run 自己那一行的终态」也扔了。两件事被同一个判据管着，
    保住了其中一件，另一件就悄悄丢了。

    判据落在**子行自己的状态**上，同时 `test_child_lifecycle_does_not_close_the_parent`
    （下一条）钉住父不许被碰 —— 两条一起才算说清楚。
    """
    from app.models.execution import Run, RunAttempt
    from app.services.execution_ingest import ExecutionIngestService, IngestContext

    service = ExecutionIngestService()
    ctx = IngestContext(
        tenant_id="t", workspace_id="w", project_id="p",
        session_id="s-fin", run_id="orc-fin",
        actor_user_id=None, decision_authority=None,
    )
    async def ingest(raw: dict, offset: int, state: dict, fid: str) -> None:
        await service.ingest_raw_record(
            db_session, context=ctx, file_identity=fid, byte_offset=offset * 100,
            raw_line=json.dumps(raw, ensure_ascii=False).encode(),
            raw=raw, adapter_state=state,
        )

    # 生产顺序：这条命令自己的 transcript 先落 —— 它才是 owning transcript。
    # 之后子节点自己的文件进来时 `owningRun=False`，正是现场的那条路。
    own_state: dict = {}
    await ingest({"event": "run_start", "at": "2026-08-11T10:00:00+00:00",
                  "node_type": "project_chat", "session_id": "s-fin"},
                 0, own_state, "own-file")

    # `owningTranscript` 由上一层（harness_transcript_ingest）按事实表判定：
    # 一个 platform Run 的**第一份** transcript 才是这条命令自己的。
    # 子节点自己的文件拿到 False —— 这里照生产那层的取值传进来，
    # 否则测试走的是 owning 分支，与现场不是同一条路。
    child_state: dict = {"owningTranscript": False}
    start = _run_start("experiment", "exp-1", "orc-fin")
    start["session_id"] = "s-fin"
    await ingest(start, 0, child_state, "exp-file")
    end = {"event": "run_end", "at": "2026-08-11T11:00:00+00:00", "status": "completed"}
    await ingest(end, 1, child_state, "exp-file")

    child = await db_session.get(Run, "orc-fin::exp-1")
    assert child is not None, "子 run 行没建出来"
    assert child.status == "completed", (
        f"子 run 跑完了却还写着 {child.status!r} —— "
        "界面会把一次成功的实验显示成 Did not finish"
    )
    assert child.ended_at is not None, "终态没有落时间"

    attempt = await db_session.scalar(
        select(RunAttempt).where(RunAttempt.run_id == "orc-fin::exp-1")
    )
    assert attempt is not None, "子 run 连 attempt 行都没有"
    assert attempt.status == "completed"


@pytest.mark.asyncio
async def test_child_lifecycle_does_not_close_the_parent(db_session) -> None:
    """但子 run 的终态**绝不许**关掉父命令 —— 这是上一版闸存在的理由，不能丢。

    实测事故（注释原文）：experiment incomplete 把父 attempt 关成 failed，
    之后 decision 又把 run 翻回 waiting_human，durable 状态自相矛盾 →
    人工答复永远被拒，post-node 决策后 run 永久卡死。
    """
    from app.models.execution import Run, RunAttempt
    from app.services.execution_ingest import ExecutionIngestService, IngestContext

    service = ExecutionIngestService()
    ctx = IngestContext(
        tenant_id="t", workspace_id="w", project_id="p",
        session_id="s-par", run_id="orc-par",
        actor_user_id=None, decision_authority=None,
    )

    async def ingest(raw: dict, offset: int, state: dict, fid: str) -> None:
        await service.ingest_raw_record(
            db_session, context=ctx, file_identity=fid, byte_offset=offset * 100,
            raw_line=json.dumps(raw, ensure_ascii=False).encode(),
            raw=raw, adapter_state=state,
        )

    own_state: dict = {}
    await ingest({"event": "run_start", "at": "2026-08-11T10:00:00+00:00",
                  "node_type": "project_chat", "session_id": "s-par"}, 0, own_state, "own-file")

    child_state: dict = {"owningTranscript": False}
    start = _run_start("experiment", "exp-par", "orc-par")
    start["session_id"] = "s-par"
    await ingest(start, 0, child_state, "child-file")
    await ingest({"event": "run_end", "at": "2026-08-11T10:30:00+00:00",
                  "status": "incomplete"}, 1, child_state, "child-file")

    parent = await db_session.get(Run, "orc-par")
    assert parent is not None
    assert parent.status == "running", (
        f"子节点 incomplete 把父命令改成了 {parent.status!r} —— "
        "这正是那次「post-node 决策后 run 永久卡死」的事故"
    )
    parent_attempt = await db_session.scalar(
        select(RunAttempt).where(RunAttempt.run_id == "orc-par")
    )
    assert parent_attempt is not None
    assert parent_attempt.status == "running", "子节点终态关掉了父 attempt"

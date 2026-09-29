"""接回来的会话，在飞那一轮在**新后端**收尾（issue #785，P0-4 另一半）。

## 这里验的是什么

#784 让后端收摊放手不杀 worker，新后端按注册表把它接回来。可接回只恢复了
"它在为谁跑"；它正在跑的那一轮 —— 事件、终止 result、pause 呈递、run 终态 ——
在新后端里没有人接：`_read_forever` 只把事件交给会话面观众，而接回来的会话
没有观众；启动时的 replay 按设计只补 transcript、不补 result。于是部署时正
跑着的研究活了，但那一轮的账永远记不上。

现在接回落在 `_adopt_locked` 一处，钩子把那一轮交给 `execute_local_turn(rejoin=…)`：
同一份收尾。四种形状：

  1. 在飞 → 跑完：终止 result 从新 socket 来，run completed、助手消息落库、command 收尾。
  2. 在飞 → 停下问人：run waiting_human，`paused_binding` 认得它，答复闸能找到 pause。
  3. 断连窗口里跑完了（接回时 worker 已 idle）：result 只在 events.jsonl 里，从盘上收尾。
  4. 接回之后 worker 死了：一句干净的 stale 终态，不是 `None.wait()` 的 AttributeError。

## 走真入口

真 worker 进程（真 socket、真注册表行、真 events.jsonl、真 JsonlEmitter）、真
`mark_orphaned_harness_runs(reap_workers=True)`（lifespan 启动段调的那一个）、
真 `execute_local_turn` 收尾（demo 后端、真 sqlite）。worker 不连 LLM：它"说的话"
由测试经它自己的 emitter 提词（`tests/_live_worker.cue`），走的是 worker 跑一轮时
发事件的同一条路。
"""
from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.models.execution import (
    AttemptStatus,
    Command,
    ExecutionEvent,
    Run,
    RunAttempt,
    RunStatus,
    SessionMessage,
    SessionProjection,
)
from app.models.user import User
from app.services.harness_sessions import (
    _control_address as _control_socket_path,
    _session_runtime_dir,
    harness_session_manager,
    mark_orphaned_harness_runs,
)
from app.services.local_execution import (
    REJOIN_KIND,
    detached_execution_count,
    rejoin_adopted_session,
)
from tests._live_worker import cue, declare_activity, kill_if_alive, spawn_worker
from tests.test_local_runtime_api import _headers, _token, runtime_client  # noqa: F401 - fixture

RESEARCHER = "researcher@atrium.local"
TOKEN = "tok-rejoin"


@dataclass
class Seeded:
    project_id: str
    session_id: str
    run_id: str
    command_id: str
    user_id: str
    state_root: Path
    cue_dir: Path
    proc: object

    def binding(self) -> dict:
        return {
            "user_id": self.user_id,
            "conversation_id": self.session_id,
            "run_id": self.run_id,
            "session_id": self.session_id,
        }


async def _wait_until(predicate: Callable[[], bool], *, timeout: float, what: str) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"等了 {timeout}s 仍未 {what}")


def _events(state_root: Path) -> list[dict]:
    path = state_root / "events.jsonl"
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _completed_result(seeded: Seeded, request_id: str, text: str) -> dict:
    return {
        "request_id": request_id,
        "run_id": "harness-run-1",
        "project_id": seeded.project_id,
        "status": "completed",
        "final_text": text,
        "transcript_path": str(seeded.state_root / "transcript.jsonl"),
        "artifact_paths": [],
        "pause_event": None,
        "pause_id": "",
        "pause_pending_path": None,
        "tokens_used": 12,
        "tokens_used_delta": 12,
    }


def _paused_result(seeded: Seeded, request_id: str) -> dict:
    (seeded.state_root / "pause_pending.json").write_text("{}", encoding="utf-8")
    return {
        **_completed_result(seeded, request_id, ""),
        "status": "paused",
        "pause_event": {
            "question": "Post-node decision for hypothesis",
            "context": "NODE COMPLETED: hypothesis",
            "options": ["PROCEED", "REVISE"],
            "pending_tool_call_id": "pause-rejoin-1",
            "asking_run_id": "harness-run-1",
            "metadata": {"type": "decision"},
        },
        "pause_id": "pause-rejoin-1",
        "pause_pending_path": str(seeded.state_root / "pause_pending.json"),
    }


@pytest.fixture
def adoption_wired(monkeypatch):
    """与 main.lifespan 同一根接线；注册表从空开始（"新后端"）。"""
    monkeypatch.setattr(harness_session_manager, "_sessions", {})
    harness_session_manager.set_adoption_handler(rejoin_adopted_session)
    yield
    harness_session_manager.set_adoption_handler(None)


@pytest.fixture
async def seeded(runtime_client, tmp_path):
    """一个真会话（走 API 建，带指令快照与 worktree）+ 一条 running 的顶层 run +
    上一个后端替它建的 command + 一个真 worker（它正是在跑这条 run 的那个）。"""
    client, factory = runtime_client
    token = await _token(client, RESEARCHER)
    project_id = (await client.get("/api/v1/projects/", headers=_headers(token))).json()[0]["id"]
    created = await client.post(
        f"/api/v1/projects/{project_id}/sessions",
        headers=_headers(token),
        json={"title": "接回在飞那一轮"},
    )
    assert created.status_code == 201, created.text
    session_id = created.json()["id"]

    run_id = f"run_{uuid4().hex}"
    async with factory() as db:
        user = await db.scalar(select(User).where(User.email == RESEARCHER))
        conversation = await db.get(SessionProjection, session_id)
        assert user is not None and conversation is not None
        db.add(Run(
            id=run_id, tenant_id=conversation.tenant_id, workspace_id=conversation.workspace_id,
            project_id=project_id, session_id=session_id, parent_run_id=None,
            status=RunStatus.RUNNING.value, summary={},
        ))
        db.add(RunAttempt(
            tenant_id=conversation.tenant_id, workspace_id=conversation.workspace_id,
            project_id=project_id, session_id=session_id, run_id=run_id, attempt_no=1,
            status=AttemptStatus.RUNNING.value,
        ))
        command = Command(
            tenant_id=conversation.tenant_id, workspace_id=conversation.workspace_id,
            project_id=project_id, session_id=session_id, run_id=run_id,
            actor_user_id=user.id, kind="run.start",
            idempotency_key=f"chat:{session_id}:seed", payload={"message": "跑一轮"},
        )
        db.add(command)
        await db.commit()
        user_id, command_id = user.id, command.id

    state_root = _session_runtime_dir(project_id, session_id)
    assert state_root is not None, "会话 worktree 不在 —— API 建会话没落地"
    state_root.mkdir(parents=True, exist_ok=True)
    (state_root / "transcript.jsonl").write_text("", encoding="utf-8")
    sock = _control_socket_path(project_id, session_id)
    assert sock is not None, "socket 命令面没开"
    cue_dir = tmp_path / "cues"
    proc = spawn_worker(state_root, sock, spawn_token=TOKEN, cue_dir=cue_dir)
    item = Seeded(project_id, session_id, run_id, command_id, user_id, state_root, cue_dir, proc)
    try:
        yield item, factory
    finally:
        kill_if_alive(proc.pid)


async def _adopt_through_the_real_entry(factory) -> int:
    async with factory() as db:
        stale = await mark_orphaned_harness_runs(db, reap_workers=True)
        await db.commit()
    return stale


async def _rejoin_is_waiting(seeded: Seeded, request_id: str) -> None:
    """等到新后端把观众登记好（`_pending` 里有那个 request_id）再让 worker 开口。

    生产里事件早于登记也不丢（socket 缓冲着，读者一起来就读到），这里等一下
    只是让"断连窗口那段由 replay 补、之后那段实时到达"两条路各走各的。
    """
    def _registered() -> bool:
        session = harness_session_manager._sessions.get(
            harness_session_manager._key(seeded.project_id, seeded.session_id))
        return session is not None and request_id in session._pending
    await _wait_until(_registered, timeout=15, what="登记对在飞那一轮的等待")


@pytest.mark.asyncio
async def test_an_inflight_turn_is_settled_by_the_backend_that_adopted_it(
    adoption_wired, seeded,
):
    """形状 1：部署时正跑着 → 新后端接回 → worker 跑完 → 账记在新后端。"""
    item, factory = seeded
    declare_activity(
        item.state_root, pid=item.proc.pid, spawn_token=TOKEN, state="working",
        turn_id="turn-rejoin-1", app_binding=item.binding(), detail={"step": "experiment"},
    )

    stale = await _adopt_through_the_real_entry(factory)
    assert stale == 0, "接回来的 run 被启动对账判死了"
    live = harness_session_manager.live_binding(item.project_id, item.session_id)
    assert live is not None and live.run_id == item.run_id
    assert detached_execution_count(REJOIN_KIND) == 1, "接回之后没有人去接在飞那一轮"
    # 接回期间这个会话是"占用"的（新消息走插话，与它由上一个后端发起时一样）。
    await _rejoin_is_waiting(item, "turn-rejoin-1")
    assert harness_session_manager.is_occupied(item.project_id, item.session_id)

    # 真 worker 的 `_operation_end` 顺序：先把活动自报翻回 idle，再发终止 result。
    declare_activity(item.state_root, pid=item.proc.pid, spawn_token=TOKEN, state="idle")
    cue(item.cue_dir, type="result", request_id="turn-rejoin-1",
        data=_completed_result(item, "turn-rejoin-1", "整轮跑完了"))
    await _wait_until(lambda: detached_execution_count(REJOIN_KIND) == 0, timeout=30,
                      what="接回的那一轮收尾")

    async with factory() as db:
        run = await db.get(Run, item.run_id)
        assert run is not None and run.status == "completed", run.status
        assert run.summary["assistantMessage"] == "整轮跑完了"
        assert run.summary["harnessRunId"] == "harness-run-1"
        replies = (await db.execute(
            select(SessionMessage).where(
                SessionMessage.run_id == item.run_id, SessionMessage.role == "assistant")
        )).scalars().all()
        assert [m.content for m in replies] == ["整轮跑完了"]
        command = await db.get(Command, item.command_id)
        assert command is not None and command.result == {
            "runId": item.run_id, "artifactId": None, "status": "completed"}
        rejoined = await db.scalar(select(ExecutionEvent).where(
            ExecutionEvent.session_id == item.session_id, ExecutionEvent.kind == "run.rejoined"))
        assert rejoined is not None and rejoined.payload["source"] == "live_socket"
    assert harness_session_manager.paused_binding(item.project_id, item.session_id) is None
    assert not harness_session_manager.is_occupied(item.project_id, item.session_id)


@pytest.mark.asyncio
async def test_an_inflight_turn_that_pauses_is_answerable_in_the_new_backend(
    adoption_wired, seeded,
):
    """形状 2：接回之后才停下问人 —— pause 记在新后端，答复闸找得到它。"""
    item, factory = seeded
    declare_activity(
        item.state_root, pid=item.proc.pid, spawn_token=TOKEN, state="working",
        turn_id="turn-rejoin-2", app_binding=item.binding(),
    )
    assert await _adopt_through_the_real_entry(factory) == 0
    await _rejoin_is_waiting(item, "turn-rejoin-2")

    # 真 worker 停在问题上时先自报 waiting_human（带 pause_id），再发 paused 的 result。
    declare_activity(
        item.state_root, pid=item.proc.pid, spawn_token=TOKEN, state="waiting_human",
        app_binding=item.binding(), detail={"question": "Post-node decision", "pause_id": "pause-rejoin-1"},
    )
    cue(item.cue_dir, type="result", request_id="turn-rejoin-2",
        data=_paused_result(item, "turn-rejoin-2"))
    await _wait_until(lambda: detached_execution_count(REJOIN_KIND) == 0, timeout=30,
                      what="接回的那一轮停下")

    async with factory() as db:
        run = await db.get(Run, item.run_id)
        assert run is not None and run.status == "waiting_human", run.status
        assert run.summary["pause"]["question"] == "Post-node decision for hypothesis"
    paused = harness_session_manager.paused_binding(item.project_id, item.session_id)
    assert paused is not None and paused.run_id == item.run_id, "答复闸找不到这个 pause"
    session = harness_session_manager._sessions[
        harness_session_manager._key(item.project_id, item.session_id)]
    assert session.pause_id == "pause-rejoin-1"
    assert session.inflight_request_id == "", "停下之后不该还有'在飞'的一轮"


@pytest.mark.asyncio
async def test_a_turn_that_finished_while_nobody_listened_is_settled_from_disk(
    adoption_wired, seeded,
):
    """形状 3：worker 在断连窗口里跑完了、已经 idle。result 只在 events.jsonl 里。"""
    item, factory = seeded
    # 上一个后端还在时它是 working……然后后端没了、它跑完了、回到 idle
    # （idle 会把 binding 清掉 —— 那正是这一类必须按会话找 run 的原因）。
    declare_activity(
        item.state_root, pid=item.proc.pid, spawn_token=TOKEN, state="working",
        turn_id="turn-gone-1", app_binding=item.binding(),
    )
    cue(item.cue_dir, type="result", request_id="turn-gone-1",
        data=_completed_result(item, "turn-gone-1", "没人听的时候跑完的"))
    await _wait_until(
        lambda: any(e.get("type") == "result" for e in _events(item.state_root)),
        timeout=10, what="result 落盘")
    declare_activity(item.state_root, pid=item.proc.pid, spawn_token=TOKEN, state="idle")

    assert await _adopt_through_the_real_entry(factory) == 0, "跑完了的 run 被判成孤儿"
    await _wait_until(lambda: detached_execution_count(REJOIN_KIND) == 0, timeout=30,
                      what="从盘上收尾")

    async with factory() as db:
        run = await db.get(Run, item.run_id)
        assert run is not None and run.status == "completed", run.status
        assert run.summary["assistantMessage"] == "没人听的时候跑完的"
        rejoined = await db.scalar(select(ExecutionEvent).where(
            ExecutionEvent.session_id == item.session_id, ExecutionEvent.kind == "run.rejoined"))
        assert rejoined is not None and rejoined.payload["source"] == "events_file"
    # 顺带：接回来的空闲 worker 现在认得这条 run 是它的（下一次接回 / 答复闸都靠它）。
    live = harness_session_manager.live_binding(item.project_id, item.session_id)
    assert live is not None and live.run_id == item.run_id


@pytest.mark.asyncio
async def test_a_result_from_before_this_run_is_not_mistaken_for_it(adoption_wired, seeded):
    """形状 3 的反例：盘上那条 result 属于**上一轮**（比这条 run 建行还早）——
    不许拿它给这条 run 收尾。"""
    item, factory = seeded
    stale_result = {
        **_completed_result(item, "turn-old", "上一轮的"),
    }
    # 手写一条"比 run 早"的 result 进事件文件（cue 走 emitter 会盖当前时间戳）。
    (item.state_root / "events.jsonl").write_text(
        json.dumps({"type": "result", "at": "2000-01-01T00:00:00+00:00",
                    "request_id": "turn-old", "data": stale_result}) + "\n",
        encoding="utf-8",
    )
    declare_activity(item.state_root, pid=item.proc.pid, spawn_token=TOKEN, state="idle")
    await _adopt_through_the_real_entry(factory)
    await asyncio.sleep(0.5)
    assert detached_execution_count(REJOIN_KIND) == 0
    async with factory() as db:
        run = await db.get(Run, item.run_id)
        assert run is not None and run.status == "running", "拿上一轮的 result 给这条 run 收了尾"


@pytest.mark.asyncio
async def test_a_worker_that_dies_after_rejoin_ends_the_turn_loudly(adoption_wired, seeded):
    """形状 4：接回之后 worker 没了 —— 那一轮以 stale 终态收尾并留下失败记录，
    不是死在 `self.process.wait()` 的 AttributeError 上（接回来的会话没有句柄）。"""
    item, factory = seeded
    declare_activity(
        item.state_root, pid=item.proc.pid, spawn_token=TOKEN, state="working",
        turn_id="turn-rejoin-4", app_binding=item.binding(),
    )
    assert await _adopt_through_the_real_entry(factory) == 0
    await _rejoin_is_waiting(item, "turn-rejoin-4")

    kill_if_alive(item.proc.pid)
    await _wait_until(lambda: detached_execution_count(REJOIN_KIND) == 0, timeout=30,
                      what="接回的那一轮因 worker 死亡而结束")

    async with factory() as db:
        run = await db.get(Run, item.run_id)
        assert run is not None
        # 「运行时丢了」是可恢复的那一类（stale_unknown / harness_process_lost），
        # 不是「这次研究失败了」。接回来的会话没有进程句柄，EOF 若照旧走
        # `self.process.wait()` 就是 AttributeError → failed + "平台内部错误"——
        # 对着一件没发生的失败给建议。
        assert run.status == "stale_unknown", (run.status, run.summary.get("failure"))
        assert run.summary.get("staleReason") == "harness_process_lost", run.summary
        assert run.summary.get("failure"), "死得无声无息：没有失败记录"
        assert "NoneType" not in json.dumps(run.summary, ensure_ascii=False), (
            "接回来的会话没有句柄，EOF 时去 wait 一个 None 了")


@pytest.mark.asyncio
async def test_the_real_lifespan_wires_the_adoption_handler(monkeypatch, db_engine):
    """机制存在但没接到路径 = 不存在。生产接线在 main.lifespan —— 走真的 lifespan
    看钩子在不在，而不是相信上面几条自己 set 的测试。"""
    from app.config import settings
    from app.main import app, lifespan
    from app.services import harness_sessions, lifecycle

    monkeypatch.setattr(settings, "local_demo_mode", True)
    monkeypatch.setattr(settings, "harness_bridge_enabled", False)
    monkeypatch.setattr(settings, "feed_collector_enabled", False)
    monkeypatch.setattr(settings, "literature_harvester_enabled", False)

    monkeypatch.setattr(harness_session_manager, "_sessions", {})
    lifecycle.reset_for_tests()
    try:
        async with lifespan(app):
            assert harness_session_manager._adoption_handler is rejoin_adopted_session, (
                "lifespan 没把接回钩子挂上 —— 接回来的 worker 那一轮又没人记账了"
            )
    finally:
        lifecycle.reset_for_tests()
        harness_session_manager.set_adoption_handler(None)

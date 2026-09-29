"""「这条 run 无主了」是随时可算的事实，不是"后端重启"这个仪式的副产品。

## 现场（E2E v23，2026-08-11）

    01:03:58  run_732df7a3… (project_chat) 开始
    02:30:56  最后一次更新
    02:34:55  边界守卫误判 → 工具报错
    03:37:14  reviewer 重试跑了 10 分钟
    之后      驱动它的 harness 子进程没了

    12:10（约 1 小时 40 分钟后）DB 里它的状态：**running**

后端一直活着，所以启动时的那次对账没机会跑。于是这条 run 永远"正在跑"：
UI 上看着在干活，会话级锁占着，没有任何东西会发现。

## 判据本来就和「启动」无关

`mark_orphaned_harness_runs` 自己的注释写得很清楚：

    现在唯一的判据就是本函数的语义：这个状态需要活体进程，而注册表里没有
    它 → 无主。

但它调的是 `paused_binding()`，那个函数要求 **`session.paused`**：

    if session and session.alive and session.paused:

启动时注册表是空的，所以这个多出来的条件不显形。可它意味着这个函数**只能
在启动时调** —— 随时调就会把一个正在干活的 running run 判成尸体。

`paused` 回答的是"它停下来等人了吗"，和"它还有没有活体进程"是两个问题。
（同一个毛病在这个函数上出现过一次：以前还要求 `resumable is True`，而
"能不能续"和"要不要回收尸体"方向正好相反。注释里记着。）

## 改法

判据改成 `live_binding()`：**alive 且 binding 指向这条 run**，不问 paused。
这样它在任何时刻求值都是对的 —— 于是可以在读会话状态时现算，而不是等下次
重启。
"""
from __future__ import annotations

import pytest

from app.services.run_liveness import runtime_lost
from app.models.execution import RunStatus
from app.services.harness_sessions import AppRunBinding, harness_session_manager


class _FakeProcess:
    def __init__(self, returncode: int | None) -> None:
        self.returncode = returncode


class _FakeSession:
    """只带判据用得上的字段。"""

    def __init__(self, *, alive: bool, paused: bool, run_id: str | None) -> None:
        self.closed = not alive
        self.process = _FakeProcess(None if alive else 0)
        self.paused = paused
        self.binding = (
            AppRunBinding(user_id="u", conversation_id="c", run_id=run_id, session_id="s")
            if run_id else None
        )

    @property
    def alive(self) -> bool:
        return not self.closed and self.process.returncode is None


@pytest.fixture()
def registry(monkeypatch):
    """把注册表换成可控的假表，别碰真子进程。"""
    store: dict[str, _FakeSession] = {}
    monkeypatch.setattr(harness_session_manager, "_sessions", store)
    return store


def _key(project_id: str, session_id: str) -> str:
    return harness_session_manager._key(project_id, session_id)


def test_a_running_run_with_a_live_runtime_is_not_orphaned(registry) -> None:
    """正在干活的 run 必须认出来 —— 这是随时求值的前提。

    今天的 `paused_binding()` 在这里返回 None（因为 paused=False），
    于是任何"随时对账"的实现都会把它当尸体回收掉。
    """
    registry[_key("p", "s")] = _FakeSession(alive=True, paused=False, run_id="run-1")

    binding = harness_session_manager.live_binding("p", "s")
    assert binding is not None and binding.run_id == "run-1"


def test_a_paused_run_with_a_live_runtime_is_not_orphaned(registry) -> None:
    """等人的那种照旧认得出（原来的能力不能丢）。"""
    registry[_key("p", "s")] = _FakeSession(alive=True, paused=True, run_id="run-1")

    binding = harness_session_manager.live_binding("p", "s")
    assert binding is not None and binding.run_id == "run-1"


def test_a_dead_process_has_no_live_binding(registry) -> None:
    """进程没了 = 无主，不管它死之前是 running 还是 waiting。"""
    registry[_key("p", "s")] = _FakeSession(alive=False, paused=False, run_id="run-1")

    assert harness_session_manager.live_binding("p", "s") is None


def test_nothing_in_the_registry_has_no_live_binding(registry) -> None:
    """后端刚重启的情形 —— 原来的行为一字不变。"""
    assert harness_session_manager.live_binding("p", "s") is None


def test_paused_binding_still_answers_its_own_question(registry) -> None:
    """`paused_binding` 不改语义：它回答的是"停下来等人了吗"。

    两个问题分开问 —— 这条测试防的是"顺手把它也放宽了"。
    """
    registry[_key("p", "s")] = _FakeSession(alive=True, paused=False, run_id="run-1")
    assert harness_session_manager.paused_binding("p", "s") is None

    registry[_key("p", "s")] = _FakeSession(alive=True, paused=True, run_id="run-1")
    assert harness_session_manager.paused_binding("p", "s") is not None


@pytest.mark.asyncio
async def test_reconcile_leaves_a_live_running_run_alone(db_session, registry) -> None:
    """端到端：注册表里有活体 → 对账不动它。

    没有这条，"随时对账"就是一颗定时炸弹。
    """
    from app.services.harness_sessions import mark_orphaned_harness_runs

    run = await _insert_run(db_session, run_id="run-live", status=RunStatus.RUNNING)
    registry[_key(run.project_id, run.session_id)] = _FakeSession(
        alive=True, paused=False, run_id="run-live"
    )

    changed = await mark_orphaned_harness_runs(db_session)
    await db_session.refresh(run)

    assert changed == 0
    assert run.status == RunStatus.RUNNING


@pytest.mark.asyncio
async def test_reconcile_reaps_a_running_run_with_no_runtime(db_session, registry) -> None:
    """真实现场：running，但注册表里什么都没有 → 回收。"""
    from app.services.harness_sessions import mark_orphaned_harness_runs

    run = await _insert_run(db_session, run_id="run-dead", status=RunStatus.RUNNING)

    changed = await mark_orphaned_harness_runs(db_session)
    await db_session.refresh(run)

    assert changed == 1
    # D11：判决不落盘 —— status 由投影器独占，平台只留见证。
    # 「运行时没了吗」改成现算（`run_liveness.runtime_lost`）。
    assert run.status == RunStatus.RUNNING, "平台不该改写事实字段"
    assert run.summary["runtimeWitness"][-1]["reason"] == "app_server_restart"
    assert runtime_lost(run, has_live_binding=False) is True
    assert run.summary.get("staleFromStatus") == RunStatus.RUNNING


async def _insert_run(db_session, *, run_id: str, status: str):
    from datetime import UTC, datetime, timedelta

    from app.models.execution import Run

    run = Run(
        id=run_id,
        tenant_id="local-tenant",
        workspace_id="w",
        project_id="p",
        session_id="s",
        node_type="project_chat",
        status=status,
        summary={},
        # 种子必须过了出生宽限：刚出生的 run 按新不变量**不该**被判无主
        # （2026-08-24 出生竞态），这里测的是"真无主的会被处理"。
        created_at=datetime.now(UTC) - timedelta(hours=1),
    )
    db_session.add(run)
    await db_session.flush()
    return run


@pytest.mark.asyncio
async def test_reading_the_session_reaps_it(db_session, registry, monkeypatch) -> None:
    """走真实的读路径 `session_response()` —— 谎是在这一层讲出去的。

    「机制存在但没接到路径」：判据修对了，但如果只有 lifespan 调它，
    2026-08-11 那条 run 照样会 `running` 一小时四十分钟。
    """
    from app.services import sessions as svc

    run = await _insert_run(db_session, run_id="run-orphan", status=RunStatus.RUNNING)
    # 注册表空的 = 没有活体进程

    await svc._reap_orphaned_runs_of(
        db_session, project_id=run.project_id, session_id=run.session_id
    )
    await db_session.refresh(run)

    # D11：判决不落盘 —— status 由投影器独占，平台只留见证。
    # 「运行时没了吗」改成现算（`run_liveness.runtime_lost`）。
    assert run.status == RunStatus.RUNNING, "平台不该改写事实字段"
    assert run.summary["runtimeWitness"][-1]["reason"] == "app_server_restart"
    assert runtime_lost(run, has_live_binding=False) is True


@pytest.mark.asyncio
async def test_reading_the_session_does_not_touch_a_live_run(db_session, registry) -> None:
    """同一条路径下，活着的 run 一个字都不能动。"""
    from app.services import sessions as svc

    run = await _insert_run(db_session, run_id="run-alive", status=RunStatus.RUNNING)
    registry[_key(run.project_id, run.session_id)] = _FakeSession(
        alive=True, paused=False, run_id="run-alive"
    )

    await svc._reap_orphaned_runs_of(
        db_session, project_id=run.project_id, session_id=run.session_id
    )
    await db_session.refresh(run)

    assert run.status == RunStatus.RUNNING


@pytest.mark.asyncio
async def test_a_reconcile_failure_never_breaks_the_read(db_session, monkeypatch) -> None:
    """对账是观察，不是主流程 —— 它挂了也不能让一次 GET 挂掉。

    退化行为是"回到旧的：等下次重启"，不是 500。
    """
    from app.services import sessions as svc

    monkeypatch.setattr(
        "app.services.harness_sessions.mark_orphaned_harness_runs",
        _boom,
    )
    await _insert_run(db_session, run_id="run-x", status=RunStatus.RUNNING)
    await svc._reap_orphaned_runs_of(db_session, project_id="p", session_id="s")


async def _boom(*args, **kwargs):
    raise RuntimeError("registry exploded")

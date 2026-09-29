"""「它还在跑」这句话必须现算之后再讲出去。

## 现场（2026-08-23）

D11 把"运行时没了"从判决改成读时现算 —— 判决不再写进 `run.status`。对的。
但**没人把现算接到读的那一端**：三个对外投影读的都还是 `run.status` 原值。

    顶部徽章 / 输入框模式   sessions.session_response 的 executionState
    /runs 列表             execution._run_response          → 前端 runStatusById
    右栏「研究进程」        前端 projectRunActivity 的 parentStatus（取自上面那条）

当天库里 8 条 run 显示「运行中」，心跳最老的停了 1 天 9 小时；而同一个会话的
停止按钮回 409「Nothing is running in this Session right now」—— 同一个问题两
个真相源，方向相反各错一边。

代价不止难看：UI 认为在跑 → 输入框只给"插话" → 插话排进一个没有 worker 的
run → 没人消费。用户被锁在一个既停不掉也续不了的会话里（8-22 17:52 那条插话
就是这么丢的）。

右栏其实**早就写好了**"被打断"的纠偏（`INTERRUPTED_PARENT_STATUSES`），只是
`parentStatus ?? recordedTerminalStatus` 让说谎的那个优先。修在源头 = 三处一起好。
"""
from __future__ import annotations

import ast
import inspect
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.config import settings
from app.models.execution import (
    ASSERTS_ACTIVE_WORK,
    PARKED_WAITING_FOR_HUMAN,
    REQUIRES_LIVE_RUNTIME_STATUSES,
    Run,
    RunAttempt,
    RunStatus,
    SessionProjection,
)
from app.services import run_liveness

NOW = datetime(2026, 8, 23, 12, 0, tzinfo=UTC)


def _run(status: str, *, run_id: str = "r1", parent: str | None = None) -> Run:
    return Run(
        id=run_id, tenant_id=settings.runtime_tenant_id, workspace_id="w",
        project_id="p", session_id="s", parent_run_id=parent,
        status=status, summary={},
    )


def _attempt(*, ended: bool, lease_offset_minutes: int = -60) -> RunAttempt:
    return RunAttempt(
        id="a1", tenant_id=settings.runtime_tenant_id, workspace_id="w",
        project_id="p", session_id="s", run_id="r1", attempt_no=1,
        status="stale_unknown" if ended else "running",
        lease_until=NOW + timedelta(minutes=lease_offset_minutes),
        ended_at=NOW - timedelta(hours=3) if ended else None,
    )


# ── 判据 ────────────────────────────────────────────────────────────────────

def test_a_run_that_claims_to_be_working_with_a_finished_attempt_is_corrected() -> None:
    """这就是那 8 条僵尸的形状：说自己在跑，而承载它的那一趟早结束了。"""
    assert run_liveness.observed_status(
        _run(RunStatus.RUNNING.value), attempt=_attempt(ended=True),
        now=NOW, has_live_binding=False,
    ) == RunStatus.STALE_UNKNOWN.value


def test_retrying_is_corrected_too() -> None:
    """`retrying` 同样断言"它在动" —— 现场那条 writing 就停在这个状态。"""
    assert run_liveness.observed_status(
        _run(RunStatus.RETRYING.value), attempt=_attempt(ended=True),
        now=NOW, has_live_binding=False,
    ) == RunStatus.STALE_UNKNOWN.value


@pytest.mark.parametrize("status", sorted(s.value for s in PARKED_WAITING_FOR_HUMAN))
def test_waiting_for_a_human_is_never_corrected(status: str) -> None:
    """「在等你回答」在没有 worker 时**仍然成立** —— 回答会把它重新拉起来。

    这两个状态和上面那些一样"需要活进程"，输入条件也完全一样（attempt 结束、
    注册表为空）—— 库里现在就有 5 条 waiting_human + 3 条 waiting_permission
    正是这个形状。把它们也改判成 stale_unknown，等于把一条能续的会话说成故障。
    分界线是**说错了的代价**，不是"需不需要活进程"。
    """
    assert run_liveness.observed_status(
        _run(status), attempt=_attempt(ended=True), now=NOW, has_live_binding=False,
    ) == status


def test_a_freshly_dispatched_run_is_hands_off_even_with_an_empty_registry() -> None:
    """挡 8-21 那次事故的重演：一条刚起 9 秒、注册表还没登记的 run 被判死，
    UI 停止轮询五分钟，而它的子节点一路跑完了。后端重启后注册表本来就是空的 ——
    只按"注册表里没有它"改判，等于把每一条正在跑的 run 都判一次死。

    ⚠️ 这条原来用的是 `_attempt(ended=False)`，而那个 fixture 的租约默认**一小时
    前就过期了** —— 也就是说它声称在防"刚出生"，构造的却是"僵尸"（见下一条）。
    真正挡住刚出生那一幕的是**租约**：派发时就写下 lease_until = now + 180s。
    触发用例要用真卡住过的那个输入，否则断言落在另一条路上
    （[[feedback_right_verdict_wrong_path]]）。
    """
    assert run_liveness.observed_status(
        _run(RunStatus.RUNNING.value),
        attempt=_attempt(ended=False, lease_offset_minutes=+2),  # 刚派发，租约新鲜
        now=NOW, has_live_binding=False,
    ) == RunStatus.RUNNING.value


def test_a_killed_worker_that_never_closed_its_attempt_is_still_reported_lost() -> None:
    """worker 被 SIGKILL：attempt 从来没被关掉，租约早已过期。

    这正是 D11 要根治的那一类僵尸 —— 一条永远显示「运行中」、停止按钮却回
    409 的 run。旧判据里的 `attempt.ended_at is not None` 合取把它挡在门外：
    "那一趟结束了" 需要 worker 主动写一笔，而 worker 被杀时恰恰写不了。
    """
    assert run_liveness.observed_status(
        _run(RunStatus.RUNNING.value),
        attempt=_attempt(ended=False),  # 租约 60 分钟前过期，attempt 没关
        now=NOW, has_live_binding=False,
    ) == RunStatus.STALE_UNKNOWN.value


def test_no_attempt_at_all_means_hands_off() -> None:
    """刚 queued、还没派出去 —— 没有证据就不下结论。"""
    assert run_liveness.observed_status(
        _run(RunStatus.QUEUED.value), attempt=None, now=NOW, has_live_binding=False,
    ) == RunStatus.QUEUED.value


def test_a_live_binding_always_wins() -> None:
    """注册表里明确有它 → 一定不改判（恒不误杀优先于恒不漏报）。"""
    assert run_liveness.observed_status(
        _run(RunStatus.RUNNING.value), attempt=_attempt(ended=True),
        now=NOW, has_live_binding=True,
    ) == RunStatus.RUNNING.value


def test_terminal_runs_are_passed_through_untouched() -> None:
    for status in (RunStatus.COMPLETED.value, RunStatus.FAILED.value):
        assert run_liveness.observed_status(
            _run(status), attempt=_attempt(ended=True), now=NOW, has_live_binding=False,
        ) == status


def test_the_two_classes_are_derived_not_a_fourth_hand_written_list() -> None:
    """名单一多就会各自演化，而分叉时两边都不报错（同 UNFINISHED 的理由）。"""
    assert ASSERTS_ACTIVE_WORK == REQUIRES_LIVE_RUNTIME_STATUSES - PARKED_WAITING_FOR_HUMAN
    assert not (ASSERTS_ACTIVE_WORK & PARKED_WAITING_FOR_HUMAN)
    assert ASSERTS_ACTIVE_WORK | PARKED_WAITING_FOR_HUMAN == REQUIRES_LIVE_RUNTIME_STATUSES


# ── 接线：走真入口，不查名字 ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_the_session_projection_tells_the_truth(db_session, monkeypatch) -> None:
    """真入口 `session_response()` 的 executionState 必须是现算的那个。

    不 monkeypatch `observed_status_map` —— 替身遮住的就是这条线本身。只把
    注册表按死成"空"（真实后端重启之后它就是空的）。
    """
    from app.models.project import Project
    from app.models.user import User
    from app.services import sessions as sessions_svc

    user = User(email="live@example.test", hashed_password="x", display_name="T")
    db_session.add(user)
    await db_session.flush()
    project = Project(name="p-live", owner_id=user.id)
    db_session.add(project)
    await db_session.flush()
    session = SessionProjection(
        tenant_id=settings.runtime_tenant_id, workspace_id="w",
        project_id=str(project.id), session_id="s-live", initiating_user_id=user.id,
    )
    db_session.add(session)
    await db_session.flush()

    run = Run(
        id="run_live", tenant_id=settings.runtime_tenant_id, workspace_id="w",
        project_id=str(project.id), session_id="s-live", parent_run_id=None,
        status=RunStatus.RUNNING.value, summary={},
    )
    db_session.add(run)
    await db_session.flush()
    db_session.add(RunAttempt(
        id="att_live", tenant_id=settings.runtime_tenant_id, workspace_id="w",
        project_id=str(project.id), session_id="s-live", run_id="run_live",
        attempt_no=1, status="stale_unknown",
        lease_until=datetime.now(UTC) - timedelta(hours=3),
        ended_at=datetime.now(UTC) - timedelta(hours=3),
    ))
    await db_session.flush()

    from app.services.harness_sessions import harness_session_manager

    monkeypatch.setattr(harness_session_manager, "live_binding", lambda *a, **k: None)
    monkeypatch.setattr(sessions_svc, "_reap_orphaned_runs_of", lambda *a, **k: _noop())

    payload = await sessions_svc.session_response(
        db_session, user=user, project=project, session=session,
    )
    assert payload["executionState"] == RunStatus.STALE_UNKNOWN.value, (
        "会话顶部徽章仍然在讲库里那句已经不成立的话 —— "
        "停止按钮同一时刻会回 409「没有当前轮可停」"
    )

    # 同一次现算的成品：前端只读这一个，所以它必须和上面那句一致。
    view = payload["executionView"]
    assert view["phase"] == "interrupted"
    assert view["canStop"] is False, (
        "运行时已经没了还给停止按钮 —— 这正是「按钮亮着按下去 409」那一幕"
    )
    # 项目 owner 一定能驱动。这条断言的价值在它会怎么坏：`may_drive` 若拿
    # 内部能力名去比对 api_names=True 的**别名**列表（drive_session vs drive），
    # 结果恒为假 —— 所有人的输入框都灰着，而没有任何一处报错。
    # 2026-08-27 我就是这么写错的，靠部署起来点一遍才照出来。
    assert view["answer"]["via"] == "composer", (
        "会话没在等人、这个人又是项目 owner，输入框却是灰的"
    )


async def _noop():
    return None


def test_both_run_projections_pass_the_computed_map_in() -> None:
    """`/runs` 的两个出口都必须把现算结果传进 `_run_response`。

    查的是**调用点的实参**（AST），不是文件里出没出现过这个名字 —— 撤掉调用
    留下 import，grep 一样命中。
    """
    from app.api.v1 import execution as execution_api

    tree = ast.parse(Path(inspect.getfile(execution_api)).read_text(encoding="utf-8"))
    calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_run_response"
    ]
    assert calls, "`_run_response` 一次都没被调用？那这个测试盯错了地方"
    for call in calls:
        assert len(call.args) >= 2, (
            f"第 {call.lineno} 行的 `_run_response` 只传了 run —— "
            "不传现算结果就是原样透传库里那句可能早就不成立的断言"
        )


@pytest.mark.asyncio
async def test_a_child_run_is_judged_by_its_parents_binding(db_session, monkeypatch) -> None:
    """子 run 的死活看**父 run** 的 binding —— 注册表只登记顶层那条。

    拿子 run 自己的 id 去比对，每一条子 run 都会被判成没主的（2026-08-12
    的孤儿扫描就是这么把正在干活的 curator 杀掉的）。
    """
    parent = _run(RunStatus.RUNNING.value, run_id="run_p")
    child = _run(RunStatus.RUNNING.value, run_id="run_p::child@d1", parent="run_p")
    db_session.add_all([parent, child])
    await db_session.flush()

    class _Binding:
        run_id = "run_p"

    from app.services.harness_sessions import harness_session_manager

    monkeypatch.setattr(harness_session_manager, "live_binding", lambda *a, **k: _Binding())
    observed = await run_liveness.observed_status_map(db_session, [parent, child])
    assert observed["run_p::child@d1"] == RunStatus.RUNNING.value, (
        "父 run 还有活进程，子 run 就还活着"
    )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))


@pytest.mark.asyncio
async def test_a_child_is_never_more_alive_than_its_parent(db_session, monkeypatch) -> None:
    """父 run 收场了，子 run 就不可能还在动。

    2026-08-27 现场（会话 ac17ccb7）：根 run 05:48 就 failed 了，五条子 run 的
    status 永远停在 `queued` —— 摄取层**有意**不让子节点的生命周期事件驱动
    Run.status（否则子节点终态会关掉父命令的 attempt）。于是右栏五张卡片各自
    算下来都是「在跑」，永远转圈，而它们等的那个调度器早就没了。

    子 run 从来没有自己的 attempt（派发的是父 run 的进程），所以「没有 attempt
    就不下结论」那条永远救着它们 —— 父的终态才是这里唯一的证据。
    """
    from app.services import run_liveness as liveness

    parent = Run(
        id="run_parent", tenant_id=settings.runtime_tenant_id, workspace_id="w",
        project_id="p", session_id="s", parent_run_id=None,
        status=RunStatus.FAILED.value, summary={},
    )
    child = Run(
        id="run_parent::_orchestrator->literature@d1",
        tenant_id=settings.runtime_tenant_id, workspace_id="w",
        project_id="p", session_id="s", parent_run_id="run_parent",
        status=RunStatus.QUEUED.value, summary={},
    )
    db_session.add_all([parent, child])
    await db_session.flush()

    from app.services.harness_sessions import harness_session_manager

    monkeypatch.setattr(harness_session_manager, "live_binding", lambda *a, **k: None)

    observed = await liveness.observed_status_map(db_session, [parent, child])
    assert observed[parent.id] == RunStatus.FAILED.value
    assert observed[child.id] == RunStatus.STALE_UNKNOWN.value, (
        "父 run 已经 failed，子 run 却报「在跑」—— 右栏那张卡片会永远转圈"
    )


@pytest.mark.asyncio
async def test_a_docked_worker_does_not_keep_a_finished_parents_child_alive(
    db_session, monkeypatch
) -> None:
    """父 run 已经收场时，注册表里那条 binding **不再是证据**。

    2026-09-16 现场（yuankk，session 74576823）：父 run 13:47 因 HTTP 400 failed，
    worker 没走 —— 它停靠在 socket 上等**下一轮**，binding 还在注册表里。于是
    `_parent_is_over` 为真、`_has_live_binding` 也为真，两个条件 AND 起来纠偏
    被跳过：右栏「postprocess 进行中 · 40 actions」一直转，聊天区同时写着
    「这一轮没能完成」，同屏两个互相矛盾的说法。

    两个条件在答同一个问题（「还有没有东西在驱动这条子 run」），不一致时过期
    的那个赢了。父的终态是充分证据：子 run 跑在父 run 的进程里，父结束了它就
    不可能还在动。

    ⚠️ 上一条测试把 `live_binding` 打成 None —— 那正好绕开了这个缺陷。判据要
    覆盖**有停靠 worker**的那一半，否则这个 bug 在测试里根本不显形。
    """
    from app.services import run_liveness as liveness
    from app.services.harness_sessions import harness_session_manager

    parent = Run(
        id="run_d43f5d70", tenant_id=settings.runtime_tenant_id, workspace_id="w",
        project_id="p", session_id="s", parent_run_id=None,
        status=RunStatus.FAILED.value, summary={},
    )
    child = Run(
        id="run_d43f5d70::_orchestrator->postprocess@d1",
        tenant_id=settings.runtime_tenant_id, workspace_id="w",
        project_id="p", session_id="s", parent_run_id="run_d43f5d70",
        status=RunStatus.RUNNING.value, summary={},
    )
    db_session.add_all([parent, child])
    await db_session.flush()

    class _DockedWorkerBinding:
        run_id = "run_d43f5d70"        # 停靠着等下一轮，binding 仍指着已死的父 run

    monkeypatch.setattr(
        harness_session_manager, "live_binding", lambda *a, **k: _DockedWorkerBinding()
    )

    observed = await liveness.observed_status_map(db_session, [parent, child])

    assert observed[parent.id] == RunStatus.FAILED.value
    assert observed[child.id] == RunStatus.STALE_UNKNOWN.value, (
        "父 run 已 failed，却因为 worker 还停靠着就把子 run 报成「在跑」"
    )

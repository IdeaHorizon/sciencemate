"""ExecutionView：一个会话此刻的局面只有一个答案。

这些判据全部落在**效果**上（三态是什么、按钮给不给、输入框开不开），不落在
文案上 —— 断言文案还在等于没断言（2026-08-24 那次「接入课题」跳得走但没人读
参数、测试一直绿）。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.models.execution import (
    ASSERTS_ACTIVE_WORK,
    AWAITING_HUMAN_RUN_STATUSES,
    Run,
    RunStatus,
    TERMINAL_RUN_STATUSES,
)
from app.services import execution_view as view


def _run(status: str, *, run_id: str = "run-1", started=None, summary=None) -> Run:
    run = Run()
    run.id = run_id
    run.status = status
    run.started_at = started or datetime(2026, 8, 27, 5, 0, tzinfo=UTC)
    run.created_at = run.started_at
    run.summary = summary
    return run


# ── 分类的完备性：新增状态而不分类，这里立刻红 ────────────────────────────


def test_every_run_status_lands_in_exactly_one_phase() -> None:
    """13 个状态值每一个都要有归宿，且只有一个。

    这条的价值在它会怎么坏：谁加了第 14 个状态而不管这里，测试当场红 ——
    而不是让那个新值悄悄落进 `alive` 兜底，在 UI 上转一个永不停止的圈。
    """
    for status in RunStatus:
        phase = view.phase_of(status.value)
        assert phase in {view.PHASE_ALIVE, view.PHASE_ENDED, view.PHASE_INTERRUPTED}

    ended = {s.value for s in RunStatus if view.phase_of(s.value) == view.PHASE_ENDED}
    assert ended == {s.value for s in TERMINAL_RUN_STATUSES}

    interrupted = {
        s.value for s in RunStatus if view.phase_of(s.value) == view.PHASE_INTERRUPTED
    }
    assert interrupted == {RunStatus.STALE_UNKNOWN.value}


def test_every_terminal_status_has_an_outcome() -> None:
    """终态必须能说出「是哪一种结局」。少一个映射，用户看到的是空白结论。"""
    for status in TERMINAL_RUN_STATUSES:
        built = view.build(_run(status.value), observed_status=status.value)
        assert built["outcome"] is not None, status
        assert built["label"] != "Finished", status  # 兜底文案说明漏了映射


def test_active_statuses_are_alive_and_offer_a_stop_button() -> None:
    for status in ASSERTS_ACTIVE_WORK:
        built = view.build(
            _run(status.value), observed_status=status.value, live_runtime=True
        )
        assert built["phase"] == view.PHASE_ALIVE, status
        assert built["waitingOn"] is None, status
        assert built["canStop"] is True, status
        assert view.answer_affordance(
            phase=built["phase"], waiting=built["waitingOn"], pause=None, may_drive=True
        )["via"] == view.VIA_COMPOSER, status


def test_waiting_for_a_person_hands_the_entry_to_the_card_and_hands_over_the_card() -> None:
    for status in AWAITING_HUMAN_RUN_STATUSES:
        built = view.build_session_view(
            _run(status.value),
            observed_status=status.value,
            pause={"runId": "run-1", "prompt": "选哪个方向？", "options": ["A", "B"]},
            live_runtime=True,
        )
        assert built["phase"] == view.PHASE_ALIVE, status
        assert built["waitingOn"]["kind"] in {"human", "permission"}, status
        # 答案要从那张卡片走 —— 而"那张卡片"就在同一个字段里，不在别处。
        assert built["answer"]["via"] == view.VIA_PAUSE, status
        assert built["answer"]["pause"]["options"] == ["A", "B"], status


# ── 本次事故的回放：一个答案，不再自相矛盾 ─────────────────────────────────


def test_the_incident_session_reports_ended_not_queued() -> None:
    """2026-08-27 现场：根 run 已 failed，五条子 run 停在 queued 且更新时间更新。

    旧路径（前端按 updatedAt 在全部 run 里取最新）会拿到子 run 的 `queued`，
    于是「Queued + 转圈 + 一个按下去 409 的停止按钮」三个说法同屏并存。
    view 只认当前这一轮的现算状态，所以这里只可能有一个答案。
    """
    root = _run(RunStatus.FAILED.value, run_id="run_59aa96")
    built = view.build(
        root,
        observed_status=RunStatus.FAILED.value,
        failure={"title": "这一轮的执行进程中途退出了"},
        live_runtime=False,
    )
    assert built["phase"] == view.PHASE_ENDED
    assert built["outcome"] == "failed"
    assert built["canStop"] is False          # 没有活体运行时 → 不给按钮
    assert built["error"] is not None         # 失败原因要摆出来


def test_a_status_that_says_running_without_a_runtime_never_offers_a_stop_button() -> None:
    """按钮的可见性只问「现在有没有活体运行时」，不看状态长相。

    这是 `canStop` 与 `/stop` 共用一个谓词的意义：库里那一行说 running，
    而进程早没了 —— 按钮不该亮。
    """
    built = view.build(
        _run(RunStatus.RUNNING.value), observed_status=RunStatus.RUNNING.value,
        live_runtime=False,
    )
    assert built["canStop"] is False


def test_an_interrupted_run_is_not_dressed_up_as_completed() -> None:
    built = view.build(
        _run(RunStatus.STALE_UNKNOWN.value),
        observed_status=RunStatus.STALE_UNKNOWN.value,
        failure={"title": "worker lost"},
    )
    assert built["phase"] == view.PHASE_INTERRUPTED
    assert built["outcome"] is None           # 它没有结局 —— 不许捏一个出来
    assert built["error"] is not None


def test_a_question_survives_the_death_of_the_process_that_asked_it() -> None:
    """一个还没人回答的问题，不会因为问它的进程死了就不存在了。

    run 被部署重启掐掉 → 转 stale_unknown，而 summary.pause 里的 question /
    context / options 一样不少。「在等什么」与「还活着吗」是两个问题：
    waitingOn 回答前者（所以照样有），phase 回答后者（所以答不了）。
    丢掉前者，用户就会在记录里看见平台问「是否批准这个高危作业」，底下
    既没有可点的东西，也没有一句话说明它已经问不成了。
    """
    built = view.build(
        _run(RunStatus.STALE_UNKNOWN.value),
        observed_status=RunStatus.STALE_UNKNOWN.value,
        was_waiting_on=RunStatus.WAITING_PERMISSION.value,
    )
    assert built["phase"] == view.PHASE_INTERRUPTED
    assert built["waitingOn"]["kind"] == "permission"   # 问题还在
    # 但只能另起一轮，不能作答：运行时没了 → 入口回到输入框。
    assert view.answer_affordance(
        phase=built["phase"], waiting=built["waitingOn"],
        pause={"prompt": "批准吗"}, may_drive=True,
    )["via"] == view.VIA_COMPOSER


def test_a_clean_finish_does_not_surface_a_stale_failure_record() -> None:
    """干净收尾的 run，summary 里留着上一次重试的失败见证也不该当结论摆出来。"""
    built = view.build(
        _run(RunStatus.COMPLETED.value),
        observed_status=RunStatus.COMPLETED.value,
        failure={"title": "一次早就重试成功的失败"},
    )
    assert built["outcome"] == "ok"
    assert built["error"] is None


def test_a_session_that_never_ran_is_ready_not_running() -> None:
    built = view.build_session_view(None, observed_status=None, pause=None)
    assert built["phase"] == view.PHASE_ENDED
    assert built["outcome"] is None
    assert built["canStop"] is False
    assert built["answer"]["via"] == view.VIA_COMPOSER
    assert built["runId"] is None


def test_a_viewer_who_cannot_drive_gets_neither_button_nor_composer() -> None:
    built = view.build_session_view(
        _run(RunStatus.RUNNING.value),
        observed_status=RunStatus.RUNNING.value,
        pause=None,
        live_runtime=True,
        may_drive=False,
        readonly_reason="Archived sessions are read-only.",
    )
    assert built["canStop"] is False
    assert built["answer"]["via"] == view.VIA_NONE
    # 拒绝要说得出原因 —— 一个灰着的输入框不解释自己，人只会以为是坏了。
    assert built["answer"]["reason"]


# ── 结构判据：停止按钮与 /stop 端点必须是同一个谓词 ─────────────────────────


def test_the_stop_endpoint_asks_the_same_predicate_the_button_does() -> None:
    """不是「两边写法一样」，是**同一个函数**。

    结构判据而不是行为判据：行为判据要起一个真进程才能分辨，而这条缺陷的形状
    正是「两边各自演化」——它在任何单一时刻的行为都可能一致。
    """
    import ast
    import inspect

    from app.api.v1 import sessions as sessions_api

    source = inspect.getsource(sessions_api.stop_session_turn)
    tree = ast.parse(source.lstrip())
    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "has_live_runtime" in called, (
        "/stop 必须调用 execution_view.has_live_runtime —— 抄一份 live_binding "
        "判断就是又一个会各自演化的真相源"
    )


# ── 防复发：判决不许再回到 run.summary ────────────────────────────────────────


def test_no_verdict_about_the_future_is_written_into_run_summary() -> None:
    """`run.summary` 里不许再出现 `resumable` 这类**关于未来的判决**。

    它曾经是一条会自我加固的谎：账面说"这个暂停续不上了" → 答复闸据此拒答
    → 拒答时再往账面盖一次"续不上" —— 2026-08-24 用户被无限拒答就是这么来的。
    「能不能续」由现场回答（binding 在不在、租约新不新鲜），现算，不落盘。

    判据扫的是**赋值目标**（`X.summary = {...}` 的字面量里有没有这个键），
    不是"文件里有没有出现 resumable" —— 后者会把响应字典和事件载荷里那些
    合法的同名字段一起误伤（它们是当时的事实，不是存起来的判决）。
    """
    import ast
    import pathlib

    offenders: list[str] = []
    root = pathlib.Path(__file__).resolve().parents[1] / "app"
    for path in root.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        if "summary" not in source:
            continue
        for node in ast.walk(ast.parse(source)):
            if not isinstance(node, ast.Assign):
                continue
            targets = [
                t for t in node.targets
                if isinstance(t, ast.Attribute) and t.attr == "summary"
            ]
            if not targets or not isinstance(node.value, ast.Dict):
                continue
            for key in node.value.keys:
                if isinstance(key, ast.Constant) and key.value == "resumable":
                    offenders.append(f"{path.name}:{node.lineno}")
    assert not offenders, (
        f"这些地方又把「能不能续」这条判决写进了 run.summary：{offenders}"
    )

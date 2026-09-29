"""活动状态取自**真实进展**，而且 `unknown` 不是判决（RFC D10 活动维）。

## D10 定的两条硬要求

    心跳**必须由干活的那个循环自己发**（与真实进展耦合 —— 独立心跳线程会
    制造"看起来活着的僵尸"）；沉默衰减为 `unknown`。
    `unknown` 只呈现、不判决：杀不杀由人或显式机械政策定。

## 为什么这两条要单独钉

**第一条**：一个卡死在 `kevent` 上的 worker，进程还在、锁还攥着、独立心跳线程
照发 —— 所有"它还活着"的信号都成立，而它什么都没在做。真实进展是唯一不会被
这种僵尸满足的信号：没有产出就没有事件，没有事件租约自己过期。

所以心跳挂在投影器上（`execution_ingest._touch_activity_lease`）：一条事件被
投影 = worker 真的走完了一步。这不是"顺手放这儿"，是判据本身的要求。

**第二条**：2026-08-21 那条 run 被判"没主了"时其实正跑着，UI 据此说「这一轮
没跑完」并停止轮询五分钟。教训不是"判得不够准"，是**这个位置根本不该下判决**
—— 注册表是进程内缓存，重启即空，它天然分不清"死了"和"刚起来还没登记"。

于是本模块最强的结论只能是 `unknown`。这个测试断言 `activity()` 的取值域里
**没有** `dead` 之类的东西 —— 不是靠约定，是靠没有那个返回值。
"""
from __future__ import annotations

import inspect
import pathlib
from datetime import UTC, datetime, timedelta

from app.models.execution import Run, RunAttempt, RunStatus
from app.services import run_liveness
from app.services.run_liveness import activity, runtime_lost

NOW = datetime(2026, 8, 21, 12, 0, tzinfo=UTC)


def _run(status: str = RunStatus.RUNNING.value) -> Run:
    return Run(id="run_x", status=status, summary={})


def _attempt(lease_until: datetime | None) -> RunAttempt:
    return RunAttempt(id="a1", run_id="run_x", attempt_no=1, lease_until=lease_until)


def test_a_fresh_lease_means_working_even_with_an_empty_registry() -> None:
    """租约没过期 = 它刚刚还在动，哪怕后端刚重启、注册表是空的。

    这正是 2026-08-21 那次误判的形状：新后端启动、注册表为空，于是一条正在
    推进的 run 被判成无主。租约跨进程存活，补上的就是这一格。
    """
    run = _run()
    attempt = _attempt(NOW + timedelta(seconds=60))
    assert activity(run, attempt=attempt, now=NOW, has_live_binding=False) == "working"
    assert runtime_lost(run, has_live_binding=False, attempt=attempt, now=NOW) is False


def test_silence_decays_to_unknown_not_to_dead() -> None:
    """租约过期只说"不知道"。"""
    run = _run()
    expired = _attempt(NOW - timedelta(seconds=1))
    assert activity(run, attempt=expired, now=NOW, has_live_binding=False) == "unknown"


def test_the_vocabulary_has_no_word_for_dead() -> None:
    """取值域里没有"死了"——不是靠约定，是靠没有那个返回值。

    源码级判据：本模块不许出现宣告死亡的字面量。一旦有人加回来，这条当场红。
    """
    source = inspect.getsource(run_liveness)
    for verdict in ('"dead"', "'dead'", '"terminated"', '"killed"'):
        assert verdict not in source, (
            f"run_liveness 里出现了 {verdict} —— 这个模块最强的结论只能是 unknown"
        )
    assert {activity(_run(), attempt=_attempt(t), now=NOW, has_live_binding=False)
            for t in (None, NOW - timedelta(days=1), NOW + timedelta(hours=1))} <= {
        "working", "unknown", "idle"}


def test_a_status_that_needs_no_runtime_is_idle_not_unknown() -> None:
    """已经收尾的 run 不该被问"运行时还在不在" —— 它本来就不需要。"""
    done = _run(RunStatus.COMPLETED.value)
    assert activity(done, attempt=None, now=NOW, has_live_binding=False) == "idle"
    assert runtime_lost(done, has_live_binding=False, attempt=None, now=NOW) is False


def test_the_heartbeat_is_written_where_real_progress_is_projected() -> None:
    """心跳必须挂在投影器上，不能是独立定时器。

    判据取自**调用位置**而不是函数名：`_touch_activity_lease` 必须被
    `_apply_projection`（每条 worker 事件都流过的那个函数）调用。
    有人把它挪进定时任务，这条就红 —— 那正是 D10 点名要避免的"僵尸心跳"。
    """
    from app.services import execution_ingest

    projection = inspect.getsource(execution_ingest.ExecutionIngestService._apply_projection)
    assert "_touch_activity_lease" in projection, (
        "心跳没接在投影器上 —— 与真实进展解耦的心跳会让卡死的 worker 看起来一直活着"
    )
    touch = inspect.getsource(execution_ingest.ExecutionIngestService._touch_activity_lease)
    assert "lease_until" in touch and "heartbeat_at" in touch


def test_a_naive_timestamp_from_the_database_does_not_blow_up_the_projector() -> None:
    """库里读回的时间可能不带时区 —— 比较之前必须归一化。

    第一版没归一化，`(now - last)` 当场 `TypeError: can't subtract offset-naive
    and offset-aware datetimes`。而这个异常发生在**投影路径**上，被外层记成
    "这一轮执行失败" —— 一条只是被重启打断的 run 因此被盖成 `failed`，
    正是「重启打断不能落成研究失败的终态」那条不变量要防的东西。

    加一个心跳，顺手把恢复语义弄坏了：**新机制接在旧路径上时，要问的不是
    "我这段对不对"，是"我抛出的异常会被谁接住、记成什么"**。
    """
    run = _run()
    naive = _attempt(NOW.replace(tzinfo=None) + timedelta(seconds=60))
    assert activity(run, attempt=naive, now=NOW, has_live_binding=False) == "working"
    stale_naive = _attempt(NOW.replace(tzinfo=None) - timedelta(seconds=60))
    assert activity(run, attempt=stale_naive, now=NOW, has_live_binding=False) == "unknown"


def test_parking_is_reported_by_the_worker_not_guessed_by_the_platform() -> None:
    """停靠中的 worker 自报"我睡到几点"，平台据此延长租约（RFC D10）。

    ## 为什么必须由 worker 说

    停靠中它什么都不产出，从平台看就是**沉默** —— 而沉默按租约会衰减成
    `unknown`。2026-08-21 现场：unattended 停靠在 4 小时的复查间隔上，用户问
    「怎么样了？」，读到的是「平台内部错误」。

    "我要睡到 T、因为 R" 是 worker 独有的事实，平台猜不出来也不该猜。
    D10 给活动维定的取值里本来就有 `parked(until, why)`。

    ## 它是事实，不是豁免权

    自报延长的是平台的**耐心**：worker 说睡到 T，租约跟到 T（加一个周期的宽限
    让它醒来后有时间发第一条事件）。真到点了还没动静，照样衰减为 `unknown`
    —— 自报改变的是何时开始怀疑，不是"永远别怀疑"。
    """
    import inspect

    from app.services import execution_ingest

    # worker 侧：停靠时必须落 transcript（平台 tail 的那个通道），不是 RPC
    # 事件流 —— 停靠发生在 RPC 早已返回之后，那头没人在听。
    runtime = pathlib.Path(__file__).resolve().parents[3] / "platform_runtime.py"
    park_source = runtime.read_text(encoding="utf-8")
    assert '"worker_parked"' in park_source, "worker 停靠时没有自报"
    parked_call = park_source.split('"worker_parked"')[0][-400:]
    assert "append_transcript" in parked_call, (
        "worker_parked 发到了 RPC 事件流上 —— 停靠时那头没人在听，等于没发"
    )

    # 平台侧：认这条事实并延长租约
    touch = inspect.getsource(execution_ingest.ExecutionIngestService._touch_activity_lease)
    assert "run.parked" in touch and "untilEpoch" in touch, "平台没认 worker 自报的停靠"
    assert "max(lease" in touch, "自报的时间没有真正延长租约"


def test_a_parked_worker_still_decays_once_its_own_deadline_passes() -> None:
    """自报的是耐心不是豁免：到点没动静，照样 unknown。"""
    run = _run()
    overdue = _attempt(NOW - timedelta(seconds=1))   # 它说睡到这个点，过了
    assert activity(run, attempt=overdue, now=NOW, has_live_binding=False) == "unknown"

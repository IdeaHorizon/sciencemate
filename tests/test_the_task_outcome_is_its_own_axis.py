"""收尾健不健康，和交代的活干成没干成，是两个问题（#1086）。

`summary["status"]` 一条轴此前同时回答这两件事。于是 experiment 的 operation run
收尾完整、审计通过、任务却是 failed / partial / blocked 时，它显示 `completed`，
summary 除标识外和真 success **逐字段相同** —— 上游据此登记 post-node flow、
自动派 reviewer、把它算成已闭环。

节点唯一能让 status 不是 completed 的办法是写一条 blocker，一写就是 `blocked`，
而 `blocked` 在 Core 里指「中途停靠、没走到评估」。四种局面压成一个词。
"""
from __future__ import annotations

import pytest

from core.task_outcome import OUTCOMES, record_task_outcome, status_floor, task_outcome


class _State:
    def __init__(self):
        self.hook_state: dict = {}
        self.transcript: list = []

    def append_transcript(self, kind, **kw):
        self.transcript.append((kind, kw))


def test_an_unreported_outcome_is_not_success() -> None:
    """没人回答过这个问题 ≠ 答案是成功。"""
    assert task_outcome(_State()) is None
    assert status_floor(None, "completed") == "completed"


def test_the_vocabulary_is_closed() -> None:
    st = _State()
    with pytest.raises(ValueError) as e:
        record_task_outcome(st, "mostly_ok")
    assert "合法值" in str(e.value)
    for word in ("success", "partial", "failed", "blocked", "cancelled"):
        assert word in OUTCOMES


def test_recording_is_auditable_and_last_write_wins() -> None:
    """收尾过程里结论可能被降格 —— 改判本身要留痕。"""
    st = _State()
    record_task_outcome(st, "success", reported_by="node:experiment")
    record_task_outcome(st, "partial", detail="声明 success，被机械降格")
    assert task_outcome(st)["outcome"] == "partial"
    events = [kw for kind, kw in st.transcript if kind == "node_task_outcome_recorded"]
    assert len(events) == 2
    assert events[1]["previous"] == "success", "改判过程看不出来，等于只留了最后一句"


def test_a_failed_task_never_shows_as_completed() -> None:
    """过渡期不变量：消费方还在拿 status=completed 当「任务成功」。"""
    assert status_floor("failed", "completed") == "incomplete"
    assert status_floor("partial", "completed") == "incomplete"
    assert status_floor("blocked", "completed") == "blocked"
    assert status_floor("cancelled", "completed") == "incomplete"


def test_a_successful_task_cannot_repair_a_broken_closure() -> None:
    """只往下压，不往上抬 —— 否则第二条轴变成了第一条轴的赦免通道。"""
    assert status_floor("success", "incomplete") == "incomplete"
    assert status_floor("success", "blocked") == "blocked"
    assert status_floor("failed", "blocked") == "blocked"


def test_a_failed_task_does_not_become_a_blocker() -> None:
    """任务失败不写 blocker —— 写了会落局面快照，把重派挡在派发闸外。

    派发闸拒绝重派的理由是「同样的输入必然得到同样的结论」，而作业瞬时失败
    这类 failed 根本不满足那个前提。
    """
    st = _State()
    record_task_outcome(st, "failed", detail="作业退出 1")
    assert not st.hook_state.get("blockers")


# ── 走真入口 finalize_run ────────────────────────────────────────────────
#
# 上面那些只验纯函数。缺陷活在 summary 里，所以判据要落在**最终那份 summary**
# 上：两条轴各自如实、互不覆盖。


async def _finalize(state, harness):
    from core.agent_loop import LoopResult
    from core.executor import finalize_run

    lr = LoopResult(final_text="", turns=1, tool_calls=[], messages=[])
    return await finalize_run(state, harness, lr, llm=None)


@pytest.mark.asyncio
async def test_final_summary_keeps_closure_status_and_task_outcome_separate(tmp_path):
    """#1086 验收 2：收尾状态与任务结局在 summary 里各自如实、互不覆盖。"""
    from core.blockers import record_blocker
    from core.harness import NodeHarness
    from core.state import State

    harness = NodeHarness(node_type="experiment")

    # ① 收尾健康 + 任务 failed —— 以前这一格显示 completed，和真成功逐字段相同
    st = State.new(node_type="experiment", base_dir=tmp_path / "a", project_id="p")
    record_task_outcome(st, "failed", detail="作业退出 1，证据已冻结")
    summary = await _finalize(st, harness)
    assert summary["node_task_outcome"]["outcome"] == "failed"
    assert summary["status"] != "completed", (
        "任务没做成却显示 completed —— 上游会据此登记审查义务、派 reviewer、算闭环")
    assert not summary["blockers"], "任务失败被写成了 blocker，局面快照会挡住重派"

    # ② 收尾故障 + 任务 success —— 第二条轴不能赦免第一条
    st = State.new(node_type="experiment", base_dir=tmp_path / "b", project_id="p")
    record_blocker(st, summary="作业提交记录读不出", category="environment")
    record_task_outcome(st, "success")
    summary = await _finalize(st, harness)
    assert summary["node_task_outcome"]["outcome"] == "success"
    assert summary["status"] == "blocked", summary["status"]

    # ③ 节点没报告 → None，**不是** success
    st = State.new(node_type="experiment", base_dir=tmp_path / "c", project_id="p")
    summary = await _finalize(st, harness)
    assert summary["node_task_outcome"] is None
    assert summary["status"] == "completed", "没报告不该被当成失败"


@pytest.mark.asyncio
async def test_the_outcome_reaches_the_platform(tmp_path):
    """summary 里有还不够：平台那张 payload 是按名点收的，不点名就到不了界面。"""
    from pathlib import Path

    ingest = Path(__file__).resolve().parents[1] / (
        "platform/backend/app/services/execution_ingest.py")
    body = ingest.read_text(encoding="utf-8")
    assert "node_task_outcome" in body and "taskOutcome" in body, (
        "任务结局停在 run_end 里，做决定的是读事件的那一方")

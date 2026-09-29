"""reviewer 重试上限不能被一次进程重启清零。

## 现场（E2E v23，2026-08-11）

    04:21:55  decision_package_presented  review_retry_capped: **true**
              选项集里因此出现了 PROCEED（#202 的设计：封顶后必须给出路）
    04:47     我重启后端（部署守卫修复）
    05:29:23  decision_package_presented  review_retry_capped: **false**
              选项集退回 [retry_reviewer, revise, redirect_upstream, abort, edit]
              —— 没有 PROCEED，自动批准选 1 = 再审一遍

计数存在 `state.hook_state["pending_post_node_flow"]` 里，那是**进程内存**。

## 同一个毛病往下挪了一层

这段代码自己的注释记着上一次教训：

    为什么现有熔断全都够不着：整个循环发生在**一个 orchestrator run 内部的
    pause/resume** 里……而乒乓 / 停滞 / 重复失败熔断都在 _continuous_followup
    —— 那是**轮之间**执行的。……所以上限必须钉在**授权这一刻**。

时机改对了，**耐久性没改**。而进程被换掉的场合恰恰是崩溃 / 挂起 / 部署修复
—— 正是最需要熔断器记事的时候。

## 判据要从耐久事实现算

授权/封顶事件**自带权威计数**，而且写进 orchestrator 的 transcript —— 那个文件
**按会话存、跨 run 追加**（实测同一份文件里同时有 03:24 和 05:29 的事件）：

    reviewer_retry_authorized_by_human   attempt_number: 2
    reviewer_retry_capped                attempts: 2, cap: 2

**读数字，不数条数**：实测这份 transcript 里各只有一条，数条数得 1（差一次才
封顶），读数字得 2（正确）。

内存计数仍然认 —— 取两者的较大值：transcript 读不动时不至于把上限清零，
而重启之后由磁盘补回来。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from shared.tools.library import decision_package as dp


class _State:
    def __init__(self, transcript: Path) -> None:
        self.transcript_path = transcript
        self.node_type = "_orchestrator"
        self.run_id = "orc-1"
        self.hook_state: dict = {}

    def append_transcript(self, event: str, **fields) -> None:
        with self.transcript_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"event": event, **fields}, ensure_ascii=False) + "\n")


@pytest.fixture()
def state(tmp_path: Path) -> _State:
    path = tmp_path / "transcript.jsonl"
    path.write_text("", encoding="utf-8")
    return _State(path)


def test_the_authoritative_number_is_read_not_recounted(state) -> None:
    """读事件自带的计数，别数条数。

    真实 transcript（2026-08-11）里只有**一条** `reviewer_retry_authorized_by_human`，
    但它写着 `attempt_number: 2`。数条数会得到 1 —— 熔断器差一次才封顶，
    于是重启后又能多转一圈。
    """
    state.append_transcript("reviewer_retry_authorized_by_human",
                            producing_run_id="run-A", attempt_number=2)

    assert dp._durable_retry_attempts(state, "run-A") == 2


def test_the_capped_event_also_carries_the_number(state) -> None:
    state.append_transcript("reviewer_retry_capped",
                            producing_run_id="run-A", attempts=2, cap=2)
    assert dp._durable_retry_attempts(state, "run-A") == 2


def test_another_producers_retries_do_not_count(state) -> None:
    """上限是**按 producer run** 算的，别把别人的账记到这里。"""
    state.append_transcript("reviewer_retry_authorized_by_human",
                            producing_run_id="run-B", attempt_number=9)
    state.append_transcript("reviewer_retry_authorized_by_human",
                            producing_run_id="run-A", attempt_number=1)

    assert dp._durable_retry_attempts(state, "run-A") == 1


def test_the_count_survives_a_lost_hook_state(state) -> None:
    """真实现场：进程换了，hook_state 空了，transcript 还在。"""
    state.append_transcript("reviewer_retry_authorized_by_human",
                            producing_run_id="run-A", attempt_number=2)
    state.hook_state.clear()                      # ← 重启

    entry = {"producing_run_id": "run-A", "review_attempt_count": 0}
    assert dp._effective_retry_attempts(state, entry) >= dp._RETRY_REVIEWER_MAX, (
        "重启把熔断器清零了 —— 无限重试循环会从头再来一遍"
    )


def test_memory_still_wins_when_it_is_higher(state) -> None:
    """transcript 读不动 / 没写全时，不能反过来把上限调低。取较大值。"""
    entry = {"producing_run_id": "run-A", "review_attempt_count": 5}
    assert dp._effective_retry_attempts(state, entry) == 5


def test_an_unreadable_transcript_falls_back_to_memory(tmp_path: Path) -> None:
    """观察不能打断主流程：读不了就退回内存计数，不抛。"""
    st = _State(tmp_path / "does-not-exist.jsonl")
    entry = {"producing_run_id": "run-A", "review_attempt_count": 1}
    assert dp._effective_retry_attempts(st, entry) == 1


def test_a_fresh_producer_starts_at_zero(state) -> None:
    """没有历史就是 0 —— 别把"读不到"当成"到上限了"，那会反过来堵死正常流程。"""
    assert dp._durable_retry_attempts(state, "run-新") == 0

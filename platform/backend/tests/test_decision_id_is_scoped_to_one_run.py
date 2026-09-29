"""一个 decision 属于**一次平台 run** —— 否则恢复出来的会话必然死在审批上。

## 现场（2026-08-10 E2E v25-r）

恢复出来的会话跑到 experiment 提交模拟、停在高危审批那一刻，后端抛：

    IngestError: Decision immutable snapshot mutation rejected

run 挂了 44 分钟。

## 根因

`decision_id` 原来只派生自 harness 侧的 `producing_run_id`。而那个 id
**跨平台会话恢复是延续的** —— 恢复出来的会话继承同一个 Git 工作区，
`.research` 里的 run 目录还在，harness 会用回同一个 producing run。

于是恢复之后：

    同一个 decision_id  ＋  新的 session_id / run_id
      → _validate_existing_decision 的 frozen 元组（含 session_id / run_id）
        对不上 → IngestError → 整轮死

**在恢复出来的会话里，任何一次审批 pause 都必然杀掉这一轮。** 而"恢复 +
审批"正是无人值守长任务最常走的组合。

## 病在 id，不在守卫

守卫是对的：一个决定的条款不许在人回答之前变。但 session/run 不是"条款"，
是"这是哪一次的决定" —— 两次不同的 run 问同一个问题，本来就是两个决定，
人是**为这一次**批的。
"""
from __future__ import annotations

from app.services.execution_ingest import TranscriptAdapter

RAW = {"event": "decision_package_presented", "producing_run_id": "1786341126-96f87e"}


def _id(run_id: str, raw: dict | None = None) -> str:
    return TranscriptAdapter._decision_id(raw or RAW, "evt-0123456789", run_id=run_id)


def test_two_runs_asking_the_same_thing_get_two_decisions() -> None:
    """这是整条修复的落点：恢复换了 run，就该是新决定。"""
    assert _id("run_aaa") != _id("run_bbb")


def test_the_same_run_is_stable_across_reingest() -> None:
    """幂等仍然成立 —— transcript 重放不能造出第二个决定。"""
    assert _id("run_aaa") == _id("run_aaa")


def test_an_explicit_harness_id_is_scoped_too() -> None:
    """harness 自己给的 id 同样不能跨 run 复用 —— 否则同一个洞换个入口再来。"""
    raw = {**RAW, "decision_id": "d-custom"}
    assert _id("run_aaa", raw) != _id("run_bbb", raw)


def test_a_missing_producing_run_still_yields_a_scoped_id() -> None:
    assert _id("run_aaa", {"event": "x"}) != _id("run_bbb", {"event": "x"})


def test_two_presentations_of_the_same_producer_are_two_decisions() -> None:
    """同一轮里重呈递（curator 重跑后条款已变）也该是新决定（2026-08-17）。

    harness 现在每次呈递都生成独立的 `decision_id`（`{producing_run_id}:p<hex>`），
    同一个平台 run 内的两次呈递因此拿到两个 Decision —— 不再撞不可变快照守卫。
    """
    first = _id("run_aaa", {**RAW, "decision_id": f"{RAW['producing_run_id']}:p11111111"})
    second = _id("run_aaa", {**RAW, "decision_id": f"{RAW['producing_run_id']}:p22222222"})
    assert first != second


def test_the_id_fits_the_column() -> None:
    """`Decision.id` 是 String(128)。

    先量再改 —— 同一晚栽过一次：`alembic_version.version_num` 是 varchar(32)，
    而我起了个 34 字符的 revision 名，DDL 跑完、记版本号回滚，症状出现在
    几层之外。
    """
    from app.models.execution import Decision

    limit = Decision.__table__.c.id.type.length
    assert limit == 128
    longest = _id("run_" + "f" * 32, {**RAW, "decision_id": "d" * 40})
    assert len(longest) <= limit

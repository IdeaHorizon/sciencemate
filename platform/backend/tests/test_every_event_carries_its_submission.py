"""每条事件都带着「它在回应哪一次提交」（RFC 异步运行时 P1-5 / 附录 S1）。

## 借鉴与病根

    S1 | Codex SQ/EQ | 事件端到端带 submission id，回答锚回提问
       | P1-5（**渲染错位类 bug 的类级解**）

此前每种事件各自手工带一个锚字段（`replies_to_message_id` / `message_id` /
`repliesToMessageId`…）。谁忘了带，谁就渲染错位 —— 而错位不报错，它只是**看起来
像另一句话的回答**。2026-08-21 一晚上手工修过两次同款：

- 决策呈递把回答渲染在提问上面；
- 右栏箭头点第一次派发，跳到第三张同类卡。

逐个补是名单式护栏（[[护栏要扫盘，不要写名单]]）：新事件默认漏。

## 修法：盖在两个唯一出口上

    worker  `State.append_transcript`  —— 每条 transcript 记录的唯一出口
    平台     `_insert_event`            —— 每条 ExecutionEvent 的唯一出口

一处盖全。新增事件类型不用记得带，因为它根本没有"带不带"这个选择。

## 判据是扫盘

下面第二条测试直接扫 `_insert_event` 的**全部调用点**：漏一个就红。这比断言
"某几种事件带了"强 —— 后者恰恰是名单。
"""
from __future__ import annotations

import inspect
import re

import pytest


def test_the_transcript_exit_stamps_every_record(tmp_path) -> None:
    """worker 侧：盖在 `append_transcript` 上，不逐个事件类型记。"""
    import json
    import sys

    sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[3]))
    from core.state import State

    state = State.new(node_type="writing", base_dir=tmp_path)
    state.submission_id = "turn-abc123"
    state.append_transcript("tool_call", name="save_artifact")
    state.append_transcript("llm_response", usage={"total_tokens": 10})

    records = [json.loads(line) for line in
               state.transcript_path.read_text(encoding="utf-8").strip().splitlines()]
    stamped = [r for r in records if r.get("submission_id") == "turn-abc123"]
    assert len(stamped) == len(records), "有 transcript 记录没被盖上 submission id"


def test_no_insert_event_call_site_forgets_the_submission_id() -> None:
    """平台侧：**扫全部调用点**，漏一个就红。

    这是这条不变量的真正护栏。断言"某几种事件带了"没用 —— 那正是名单，
    而名单的失效方式是"新加的那个不在名单里"，且失效时**静默**。
    """
    from app.services import execution_ingest

    source = inspect.getsource(execution_ingest)
    calls = [m.start() for m in re.finditer(r"await self\._insert_event\(", source)]
    assert len(calls) >= 5, f"只找到 {len(calls)} 个调用点，正则大概率失配了"

    #: 允许不带的调用点，**每一个都要写明理由**。
    #:
    #: 例外必须点名 —— 沉默地放过等于名单式护栏又回来了。判据取函数名而不是
    #: 行号：行号会随无关改动漂移，一漂就得有人回来"修测试"，修着修着就把
    #: 真缺口一起修没了。
    EXEMPT = {
        # 拒收见证：这条事件说的是"有一份 transcript 被拒了"，它不属于某一次
        # 提交 —— 那份文件可能根本没有对应的用户输入（对账、回放、外部投递）。
        "_record_rejection_witness",
        # 脱敏警告：挂在**另一条事件**上的旁注（`sourceEventId`），归属由它锚的
        # 那条事件回答，自己再带一份就是两个答案。
        "_insert_redaction_warning",
    }

    def enclosing_function(offset: int) -> str:
        head = source[:offset]
        matches = re.findall(r"\n    (?:async )?def ([a-zA-Z_0-9]+)\(", head)
        return matches[-1] if matches else ""

    missing = []
    for start in calls:
        window = source[start:start + 900].split("\n        )")[0]
        if "adapter_state_submission_id" in window:
            continue
        owner = enclosing_function(start)
        if owner in EXEMPT:
            continue
        missing.append(f"{owner}（第 {source[:start].count(chr(10)) + 1} 行）")
    assert not missing, (
        f"这些 _insert_event 调用点没带 submission id：{missing} —— "
        "它们产出的事件锚不回提问，UI 只能靠顺序猜。"
        "真不该带的话，把函数名加进 EXEMPT 并写明理由。"
    )


def test_the_exemptions_still_exist() -> None:
    """豁免名单不许留着已经不存在的函数名。

    否则它会慢慢变成一张"曾经"的清单：函数改了名，豁免继续生效，而那个新名字
    的调用点从此不受任何约束 —— 护栏静默失效的经典形状。
    """
    from app.services import execution_ingest

    source = inspect.getsource(execution_ingest)
    for name in ("_record_rejection_witness", "_insert_redaction_warning"):
        assert f"def {name}(" in source, (
            f"豁免名单里的 {name} 已经不存在 —— 清理它，否则它在替一个不存在的"
            "东西挡着，而真正该被查的调用点可能正叫这个名字"
        )


def test_a_new_submission_replaces_the_previous_one() -> None:
    """插话是**新的一次**提交 —— 不换 id 的话它引出的回答会锚回上一句。

    「回答渲染在提问上面」就是这么来的：归属信息不是缺失，是**指向了错的那一次**。
    """
    import pathlib

    runtime = pathlib.Path(__file__).resolve().parents[3] / "platform_runtime.py"
    source = runtime.read_text(encoding="utf-8")
    # turn / run_unattended：本轮的 request_id
    assert source.count("self.state.submission_id = request_id") >= 2, (
        "turn 和 run_unattended 都要盖 —— 漏一个，那一趟的事件全锚不回去"
    )
    # 插话：换成那条消息自己的 id
    assert "self.state.submission_id = item.message_id" in source, (
        "插话没换 submission id —— 它引出的回答会锚回上一句提问"
    )


@pytest.mark.asyncio
async def test_the_projector_copies_it_onto_the_event(db_session) -> None:
    """端到端最后一跳：transcript 上的 id 出现在事件 payload 里。"""
    from datetime import UTC, datetime

    from sqlalchemy import select

    from app.models.execution import EventVisibility, SessionProjection
    from app.services.execution_ingest import (
        EventDraft, ExecutionIngestService, IngestContext,
    )

    db_session.add(SessionProjection(
        tenant_id="t", workspace_id="w", project_id="p", session_id="s-sub",
        next_sequence=0))
    await db_session.flush()
    session = await db_session.scalar(
        select(SessionProjection).where(SessionProjection.session_id == "s-sub"))

    service = ExecutionIngestService()
    context = IngestContext(
        tenant_id="t", workspace_id="w", project_id="p",
        session_id="s-sub", run_id="run-sub", attempt_no=1)
    event = await service._insert_event(
        db_session,
        session=session,
        context=context,
        event_id="ev-sub-1",
        draft=EventDraft("agent.message", EventVisibility.STANDARD,
                         {"text": "回答"}, datetime.now(UTC)),
        origin="raw_transcript",
        source={"rawEvent": "llm_response", "fileRef": "f", "byteOffset": 0},
        adapter_state_submission_id="turn-xyz",
    )
    assert event.payload["submissionId"] == "turn-xyz"

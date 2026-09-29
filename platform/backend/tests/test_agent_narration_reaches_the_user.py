"""agent 每轮写的说明必须到得了用户面前 —— 现在被摄取器扔了。

## 现场（wangd 2026-08-11 试用）

> 「现在这个 UI 它每一步具体在干啥，我感觉看的一头雾水，它没有告诉这个用户，
>   我现在干了啥？」

UI 上显示的是一串工具名：

    Search literature — "English breakfast" OR "Christmas pudding"
    Organize research evidence
    List research outputs

而模型每一轮**都写了人话**，就在 transcript 里躺着（literature 那个 run 写了
28 段）：

    [第 3 轮] 前两轮宽泛查询的结果不太理想——大量不相关论文。我需要调整策略，
              使用更精准的查询词。
    [第 6 轮] 我注意到一个关键问题：search_papers 的通用搜索对人文社科类话题
              匹配效果很差…但有几篇有价值的线索出现了：Bleasdale et al. 2019…

用户想问的正是「它为什么突然搜圣诞布丁」，而答案就在第 3 轮那段里。

## 为什么没到

摄取器**读过**这些事件，只是只取 usage：

    if raw_event == "llm_response" and isinstance(raw.get("usage"), dict):
        payload = self._usage_payload(...)      # 只要 token 数
        return EventDraft("usage.updated", ...)  # 正文丢掉

所以整条 SSE 流里只有 tool.progress / workspace.changed / run.started ——
一条带正文的都没有。**信息在系统里，只是没送到需要它的那一方**，而这次那一方
是用户。

## 这一条只做「送达」，不做加工

不总结、不改写、不判断该不该显示 —— 原样落成一条 `agent.message`，
由前端决定怎么渲染。加工是另一件事，且一旦开始加工就会有人问"它替我省略了什么"。
"""
from __future__ import annotations

import json

import pytest
from sqlalchemy import select


def _draft(service, raw: dict):
    """走真实的 raw → EventDraft 转换，不复刻判据。"""
    return service._draft_for_raw(raw, {}) if hasattr(service, "_draft_for_raw") else None


def test_a_response_with_text_becomes_a_visible_message() -> None:
    from app.services.execution_ingest import ExecutionIngestService

    raw = {
        "event": "llm_response",
        "at": "2026-08-11T10:00:00+00:00",
        "turn": 3,
        "content": "前两轮宽泛查询的结果不太理想——大量不相关论文。我需要调整策略。",
        "usage": {"prompt_tokens": 100, "completion_tokens": 20},
    }
    draft = ExecutionIngestService.narration_draft(raw)
    assert draft is not None, "有正文的回复必须落成一条事件"
    assert draft.kind == "agent.message"
    assert "宽泛查询" in draft.payload["text"]
    assert draft.payload["turn"] == 3


def test_usage_is_still_recorded_separately() -> None:
    """记账那一半不能丢 —— 两件事，两条事件。"""
    from app.services.execution_ingest import ExecutionIngestService

    raw = {"event": "llm_response", "at": "2026-08-11T10:00:00+00:00",
           "usage": {"prompt_tokens": 1}, "content": "x" * 50}
    assert ExecutionIngestService.narration_draft(raw) is not None
    # usage.updated 那条由原有分支产出，这里只断言新分支没有把它顶掉
    assert isinstance(raw.get("usage"), dict)


def test_a_toolcall_only_turn_produces_nothing() -> None:
    """本轮只调工具没写字 —— 别造一条空消息刷屏。"""
    from app.services.execution_ingest import ExecutionIngestService

    for empty in ("", "   ", None):
        raw = {"event": "llm_response", "content": empty, "usage": {"prompt_tokens": 1}}
        assert ExecutionIngestService.narration_draft(raw) is None


def test_a_trivial_ack_is_not_worth_a_card() -> None:
    """「好的」「收到」这种不值一条 —— 但判据是长度，不是关键词名单。

    关键词名单对没见过的说法默认漏过（护栏要扫盘不要写名单）。
    """
    from app.services.execution_ingest import ExecutionIngestService

    assert ExecutionIngestService.narration_draft(
        {"event": "llm_response", "at": "2026-08-11T10:00:00+00:00",
         "content": "好的"}) is None


def test_it_does_not_summarize_or_rewrite() -> None:
    """原样送达。一旦开始加工，就会有人问"它替我省略了什么"。"""
    from app.services.execution_ingest import ExecutionIngestService

    text = "我注意到一个关键问题：search_papers 对人文社科匹配很差。" * 3
    draft = ExecutionIngestService.narration_draft(
        {"event": "llm_response", "at": "2026-08-11T10:00:00+00:00", "content": text})
    assert draft.payload["text"] == text


def test_only_llm_response_carries_narration() -> None:
    """别把别的事件误认成叙述。"""
    from app.services.execution_ingest import ExecutionIngestService

    assert ExecutionIngestService.narration_draft(
        {"event": "tool_call", "content": "这是工具参数不是叙述" * 3}) is None


def test_a_missing_timestamp_does_not_break_ingestion() -> None:
    """这条是「观察」——时间戳缺了就跳过，不能把整条摄取打断。

    可选查询放进包住主流程的 try 里，它一抛就顶掉真正的失败 —— 这个坑
    `project_autonomy_policy` 上付过一次学费。
    """
    from app.services.execution_ingest import ExecutionIngestService

    for bad in (None, "", "not-a-time", "2026-08-11T10:00:00"):   # 最后一个缺时区
        raw = {"event": "llm_response", "at": bad, "content": "有正文的一段说明文字"}
        assert ExecutionIngestService.narration_draft(raw) is None


@pytest.mark.asyncio
async def test_it_actually_lands_in_the_event_log(db_session) -> None:
    """接线：走真实的 `ingest_raw_record`，看库里真出现 `agent.message`。

    「函数写了没人调」今天已经踩了三次 —— 新加的东西自己也得受这一问。
    """
    from app.models.execution import ExecutionEvent
    from app.services.execution_ingest import ExecutionIngestService, IngestContext

    service = ExecutionIngestService()
    ctx = IngestContext(
        tenant_id="t", workspace_id="w", project_id="p",
        session_id="s-narration", run_id="r-narration",
        actor_user_id=None, decision_authority=None,
    )
    raw = {
        "event": "llm_response",
        "at": "2026-08-11T10:00:00+00:00",
        "turn": 3,
        "content": "前两轮宽泛查询结果不理想，换更精准的查询词。",
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }
    await service.ingest_raw_record(
        db_session, context=ctx, file_identity="f", byte_offset=0,
        raw_line=json.dumps(raw, ensure_ascii=False).encode(),
        raw=raw, adapter_state={},
    )
    kinds = (await db_session.execute(
        select(ExecutionEvent.kind).where(ExecutionEvent.run_id == "r-narration")
    )).scalars().all()

    assert "agent.message" in kinds, f"叙述没落库，只有 {kinds}"
    assert "usage.updated" in kinds, "记账那条不能被顶掉——两件事两条事件"


def test_it_reads_the_authoritative_full_text_not_the_preview() -> None:
    """`content` 是权威正文。两个字段都在时，取完整的那个。

    实测发现的坑：transcript 里根本没有 `content` 字段，只有
    `content_preview=(response.content or "")[:500]` —— 硬截 500 字、不留标记。
    于是"完整的 500 字"和"被砍掉一半"分不开，信息在**源头**就没了。
    `core/agent_loop.py` 现在同时写 `content`（全文）。
    """
    from app.services.execution_ingest import ExecutionIngestService

    full = "第一段。" * 200                       # 远超 500
    draft = ExecutionIngestService.narration_draft({
        "event": "llm_response", "at": "2026-08-11T10:00:00+00:00",
        "content": full, "content_preview": full[:500],
    })
    assert draft.payload["text"] == full, "取了截断的那份"
    assert draft.payload["previewOnly"] is False


def test_an_old_transcript_with_only_a_preview_says_so() -> None:
    """只有 preview 的老 transcript 照样送达，但**标明这是预览**。

    退回去读 preview 的时候我们无从判断它完不完整 —— 那就如实说不知道，
    不要猜。猜错的两个方向都难看：把半截话当完整发言，或给完整发言加假省略号。
    """
    from app.services.execution_ingest import ExecutionIngestService

    draft = ExecutionIngestService.narration_draft({
        "event": "llm_response", "at": "2026-08-11T10:00:00+00:00",
        "content_preview": "好的，这是第 1 轮 Analysis。让我按照完整工作流开始。",
    })
    assert draft is not None, "老 transcript 的叙述被整条丢掉了"
    assert draft.payload["previewOnly"] is True


def test_the_agent_loop_records_the_whole_thing() -> None:
    """接线：`core/agent_loop.py` 真的写了 `content` 全文。

    这一条对着**真实源码的行为**验 —— 没有它，上面两条都只是在测一个
    从来没有人产出的字段（今天已经在 `content` 这个名字上栽过一次）。
    """
    import ast
    from pathlib import Path

    source = (Path(__file__).resolve().parents[3] / "core" / "agent_loop.py").read_text("utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        args = [a for a in node.args if isinstance(a, ast.Constant)]
        if not any(a.value == "llm_response" for a in args):
            continue
        names = {kw.arg for kw in node.keywords}
        assert "content" in names, f"llm_response 只写了 {sorted(names)}，没有完整正文"
        return
    raise AssertionError("没找到写 llm_response 的地方 —— 判据锚点丢了")


@pytest.mark.asyncio
async def test_a_derived_origin_without_sources_is_refused_at_write_time(db_session) -> None:
    """`adapter_derived` 必须说得出派生自哪些事件 —— 写入时就查。

    ## 现场（2026-08-12，查了很久）

    叙述事件我给了 `origin=adapter_derived`，而它其实是从 raw 行**直接读出来的**
    （和同一行产出的 `usage.updated` 一样），没有源事件、也就没有 `derivedFrom`。

    这条不变量**浏览器端解析器一直在执行**：

        Event 3: adapter_derived requires non-empty source.derivedFrom

    但写入端不管。后果不是"这条事件不显示"，而是整页 `eventsQuery` 抛异常 ——
    那一轮的执行记录一片空白，而且从 UI 上完全看不出是为什么。

    契约只在出口检查 = 让错误在离制造它的人最远的地方爆炸。
    """
    from app.models.execution import EventOrigin
    from app.services.execution_ingest import (
        EventDraft,
        ExecutionIngestService,
        IngestContext,
        IngestError,
    )

    service = ExecutionIngestService()
    ctx = IngestContext(
        tenant_id="t", workspace_id="w", project_id="p",
        session_id="s-origin", run_id="r-origin",
        actor_user_id=None, decision_authority=None,
    )
    session, _ = await service._lock_scope(db_session, ctx)
    with pytest.raises(IngestError) as caught:
        await service._insert_event(
            db_session, session=session, context=ctx, event_id="e-origin",
            draft=EventDraft("agent.message", "standard", {"text": "x"}, None),
            origin=EventOrigin.ADAPTER_DERIVED,
            source={"rawEvent": "llm_response"},     # ← 没有 derivedFrom
        )
    assert "derivedFrom" in str(caught.value)


@pytest.mark.asyncio
async def test_the_narration_declares_the_origin_it_really_has(db_session) -> None:
    """接线：真实摄取产出的叙述事件，origin 是 `raw_transcript`。

    它和同一行产出的 `usage.updated` 是同一类东西 —— 直接从 transcript 读的。
    """
    import json

    from app.models.execution import ExecutionEvent
    from app.services.execution_ingest import ExecutionIngestService, IngestContext

    service = ExecutionIngestService()
    ctx = IngestContext(
        tenant_id="t", workspace_id="w", project_id="p",
        session_id="s-origin2", run_id="r-origin2",
        actor_user_id=None, decision_authority=None,
    )
    raw = {
        "event": "llm_response", "at": "2026-08-11T10:00:00+00:00", "turn": 1,
        "content": "换更精准的查询词，理由写在这里。", "usage": {"prompt_tokens": 1},
    }
    await service.ingest_raw_record(
        db_session, context=ctx, file_identity="f", byte_offset=0,
        raw_line=json.dumps(raw, ensure_ascii=False).encode(),
        raw=raw, adapter_state={},
    )
    origins = dict(
        (await db_session.execute(
            select(ExecutionEvent.kind, ExecutionEvent.origin)
            .where(ExecutionEvent.session_id == "s-origin2")
        )).all()
    )
    assert origins.get("agent.message") == origins.get("usage.updated"), (
        f"叙述和记账来自同一行，origin 却不同：{origins}"
    )


def test_an_interrupt_reply_becomes_a_visible_message() -> None:
    """打断决策轮的答复也必须送达用户。

    2026-08-17 实测：用户在 hypothesis 跑着时问"跑的怎么样了"，决策轮查了
    进度、生成了回答 —— 然后只打印在 CLI stdout。平台用户什么都看不到，
    插话像对着空气说话。
    """
    from app.services.execution_ingest import ExecutionIngestService

    raw = {
        "event": "interrupt_reply",
        "at": "2026-08-17T14:00:00+00:00",
        "text": "[orchestrator → run_x] 最近进展：\n   第 4 轮检索中，已入索引 17 篇",
    }
    draft = ExecutionIngestService.narration_draft(raw)
    assert draft is not None
    # 待命轮的答复是**调度器在对话里说话**，不是节点独白 —— 左栏 2026-08-18
    # 起只留对话级内容，映射成 agent.message 会让它整段消失。
    assert draft.kind == "orchestrator.said"
    assert "已入索引 17 篇" in draft.payload["text"]


def test_a_short_interrupt_reply_is_still_delivered() -> None:
    """短确认不过最短长度门 —— 这里沉默的代价是"用户以为没人理他"。"""
    from app.services.execution_ingest import ExecutionIngestService

    draft = ExecutionIngestService.narration_draft({
        "event": "interrupt_reply",
        "at": "2026-08-17T14:00:00+00:00",
        "text": "✓ 已注入",
    })
    assert draft is not None and draft.payload["text"] == "✓ 已注入"
    assert draft.kind == "orchestrator.said"


def test_an_interrupt_reply_carries_its_anchor() -> None:
    """答复声明自己在回答哪条消息 —— 没有锚，UI 只能把它挂进 run 的活动窗口，
    渲染在提问**上面**（2026-08-18 实测）。"""
    from app.services.execution_ingest import ExecutionIngestService

    draft = ExecutionIngestService.narration_draft({
        "event": "interrupt_reply",
        "at": "2026-08-18T08:09:44+00:00",
        "text": "正在跑，没卡住。",
        "replies_to_message_id": "msg-80112a58",
    })
    assert draft is not None
    assert draft.payload["repliesToMessageId"] == "msg-80112a58"
    # 老事件 / CLI 投递没有锚 → 空字符串，保持原渲染位置，不能是 None。
    bare = ExecutionIngestService.narration_draft({
        "event": "interrupt_reply",
        "at": "2026-08-18T08:09:44+00:00",
        "text": "正在跑。",
    })
    assert bare is not None and bare.payload["repliesToMessageId"] == ""


def test_an_interrupt_ack_becomes_a_persistent_receipt() -> None:
    """插话送达回执必须是持久事件。

    2026-08-18 实测：回执发在 progress 通道 —— 不落库、_progress_id 按
    request 算，子节点的下一条工具进度立刻把它顶掉。用户按下回车后 2 分钟
    黑屏，看到的仍是 "Analyzing tool results"。
    """
    from app.services.execution_ingest import ExecutionIngestService

    draft = ExecutionIngestService.narration_draft({
        "event": "user_interrupt_received",
        "at": "2026-08-18T08:07:41+00:00",
        "text": "跑的怎么样了？",
        "message_id": "msg-80112a58",
        "author": "user-1",
    })
    assert draft is not None
    assert draft.kind == "interrupt.acknowledged"
    assert draft.payload["repliesToMessageId"] == "msg-80112a58"
    assert draft.payload["deferred"] is False


def test_a_deferred_interrupt_still_gets_a_receipt() -> None:
    """没有活跃子节点时那句写死的「（收到 ——…）」降级成机械回执：
    框架别替调度器编台词，措辞归显示层（wangd 2026-08-18）。"""
    from app.services.execution_ingest import ExecutionIngestService

    draft = ExecutionIngestService.narration_draft({
        "event": "interrupt_deferred",
        "at": "2026-08-18T08:07:41+00:00",
        "replies_to_message_id": "msg-80112a58",
    })
    assert draft is not None
    assert draft.kind == "interrupt.acknowledged"
    assert draft.payload["deferred"] is True
    assert draft.payload["repliesToMessageId"] == "msg-80112a58"


def test_an_empty_interrupt_reply_produces_nothing() -> None:
    from app.services.execution_ingest import ExecutionIngestService

    assert ExecutionIngestService.narration_draft({
        "event": "interrupt_reply", "at": "2026-08-17T14:00:00+00:00", "text": "  ",
    }) is None

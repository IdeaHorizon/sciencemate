"""v3.3：同 run 重复只读调用的记忆层 + 压缩保任务状态。

回归依据（E2E 实测事故 2026-07-26）：一个 curator run 用 78 个不同 query 发了
51,937 次 search_kb（ATLAS 一词 7,409 次，空结果 0 次），14,844 次 LLM 调用烧
22.7 亿 token，write_scratchpad/save_artifact/create_claim 全为 0。机理是 149 次
上下文压缩把工具结果裁掉，agent 每次压缩后"忘了"查过，重跑同一份 checklist。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import shared.tools  # noqa: F401  # 注册真实工具：判据现在来自注册表声明
from core import tool_call_cache as tc
from core.state import State


class _FakeState:
    """只需要 hook_state 的最小 state。"""

    def __init__(self) -> None:
        self.hook_state: dict = {}


def _big_result(n_items: int = 40) -> dict:
    return {
        "status": "success",
        "total_matched": 3,
        "items": [{"text": "x" * 900} for _ in range(n_items)],
    }


# ── 基本语义 ────────────────────────────────────────────────────────────────

def test_first_call_is_miss():
    s = _FakeState()
    assert tc.lookup(s, "search_kb", {"query": "ATLAS"}) is None


def test_a_repeat_gets_a_pointer_to_what_is_already_in_context():
    """⚠️ 判据已更新（工作集重构）：重复请求拿到的是**指针**，不是副本。

    原判据是"软阈值之前原样返回结果"。工作集接管后，原文就在上文逐字摆着，
    再发一份（哪怕无损）也是浪费；本层的职责收窄成指路 + 记账。
    """
    s = _FakeState()
    res = {"status": "success", "total_matched": 3}
    tc.store(s, "search_kb", {"query": "ATLAS"}, res, turn=1)
    second = tc.lookup(s, "search_kb", {"query": "ATLAS"}, is_live=True)
    assert second is not None
    assert second.get("already_in_context") is True
    assert second.get("status") == "success", "status 必须继承产生方的章"
    assert "total_matched" not in second, "又把内容重发了一遍"


def test_a_repeat_never_costs_a_second_copy_of_a_big_result():
    """⚠️ 判据已更新：大结果的重复请求也只回指针，体积与结果大小无关。

    原判据是"到软阈值后返回**紧凑版**"。紧凑版是框架执笔的有损转述，占着
    tool result 的位 —— 它正是 2026-08-21 那 105 次"把 error 说成 success"
    的载体。工作集保证原文在上文至多一份，于是这里连紧凑版都不需要了。
    """
    s = _FakeState()
    tc.store(s, "search_kb", {"query": "ATLAS"}, _big_result(), turn=1)
    third = None
    for _ in range(3):
        third = tc.lookup(s, "search_kb", {"query": "ATLAS"}, is_live=True)
    assert third["already_in_context"] is True
    assert len(str(third)) < len(str(_big_result())) / 5
    assert "items" not in str(third), "把大结果又抄了一遍"


def test_hard_threshold_escalates_to_error_directive():
    """病态重复必须升级成 error + 明确的改策略指引（而不是继续喂结果）。"""
    s = _FakeState()
    tc.store(s, "search_kb", {"query": "ATLAS"}, {"status": "success"}, turn=1)
    last = None
    for _ in range(tc.HARD_REPEAT + 5):
        last = tc.lookup(s, "search_kb", {"query": "ATLAS"})
    assert last["status"] == "error"
    assert last["repeat_count"] >= tc.HARD_REPEAT
    # 指引必须告诉它"把结论落盘再继续"，否则 agent 无从改变行为
    assert "write_scratchpad" in last["error"] or "save_artifact" in last["error"]


# ── 正确性：写操作必须让缓存失效（否则会返回过期数据）────────────────────────

def test_mutation_invalidates_cache():
    """先查空 → 写入 → 再查，绝不能命中旧的空结果。"""
    s = _FakeState()
    tc.store(s, "search_kb", {"query": "X"}, {"status": "success", "total_matched": 0}, turn=1)
    assert tc.lookup(s, "search_kb", {"query": "X"}) is not None
    # 非白名单工具（写）执行 → 代次 +1 → 全部失效
    tc.store(s, "create_claim", {"claim_text": "new"}, {"status": "success"}, turn=2)
    assert tc.lookup(s, "search_kb", {"query": "X"}) is None


def test_non_cacheable_tool_never_cached():
    s = _FakeState()
    tc.store(s, "run_bash", {"cmd": "ls"}, {"status": "success"}, turn=1)
    assert tc.lookup(s, "run_bash", {"cmd": "ls"}) is None


def test_different_args_are_different_entries():
    s = _FakeState()
    tc.store(s, "search_kb", {"query": "A"}, {"status": "success", "n": 1}, turn=1)
    assert tc.lookup(s, "search_kb", {"query": "B"}) is None


def test_args_key_is_order_insensitive():
    s = _FakeState()
    tc.store(s, "search_kb", {"a": 1, "b": 2}, {"status": "success"}, turn=1)
    assert tc.lookup(s, "search_kb", {"b": 2, "a": 1}) is not None


# ── 压缩保任务状态：findings digest ─────────────────────────────────────────

def test_findings_digest_lists_established_results():
    """台账要列出查过的条目，但**不许冠名"已确立的结论"**（2026-08-21 判据更新）。

    原断言是 `"勿重复查询" in d`。那句话固化了一个与 summarizer 正面矛盾的行为：
    summarizer 清除旧 tool result 时写的是"用同样参数重调即可取回"，台账却在
    劝阻重调。两条机制都是为同一场事故（51,937 次 search_kb）建的，方向却相反。

    现在台账只声明自己是**截断后的摘要**，把"要不要重调"的判断留给模型。
    """
    s = _FakeState()
    tc.store(s, "search_kb", {"query": "ATLAS"}, {"status": "success", "total_matched": 3}, turn=1)
    tc.store(s, "search_kb", {"query": "Qodara"}, {"status": "success", "total_matched": 0}, turn=1)
    d = tc.findings_digest(s)
    assert d is not None
    assert "ATLAS" in d and "Qodara" in d
    assert "已确立" not in d, "把截断后的摘要冠名成了「已确立的结论」"
    assert "摘要" in d, "没有声明这些行只是摘要"


def test_findings_digest_empty_when_nothing_cached():
    assert tc.findings_digest(_FakeState()) is None


def test_findings_digest_drops_stale_generation():
    """写操作后旧结论不再被声称为"已确立"。"""
    s = _FakeState()
    tc.store(s, "search_kb", {"query": "ATLAS"}, {"status": "success"}, turn=1)
    tc.store(s, "create_claim", {"x": 1}, {"status": "success"}, turn=2)
    assert tc.findings_digest(s) is None


@pytest.mark.asyncio
async def test_summarizer_injects_digest_after_compression(tmp_path: Path):
    """压缩后必须把结论台账注回上下文 —— 这是"压缩把进度裁掉"的直接修复。"""
    from core.harness import NodeHarness, SummarizerConfig
    from core.llm import LLMMessage
    from core.summarizer import run_summarizer

    state = State.new(node_type="literature", base_dir=tmp_path, project_id="p_dig")
    tc.store(state, "search_kb", {"query": "ATLAS"},
             {"status": "success", "total_matched": 3}, turn=1)

    harness = NodeHarness(
        node_type="test", max_context_tokens=4000,
        summarizer=SummarizerConfig(enabled=True, trigger_threshold=0.5,
                                     strategy="truncate", keep_last_n_turns=1),
    )
    msgs = [
        LLMMessage(role="system", content="sys"),
        LLMMessage(role="user", content="q"),
        *[LLMMessage(role="assistant", content="filler " * 200) for _ in range(6)],
    ]
    out = await run_summarizer(harness, state, msgs, llm=None, turn=3,
                                estimated_tokens=99999)
    tail = out[-1]
    # 判据看内容不看角色：台账现在以 framework-notice（user + 归属信封）注回 ——
    # 中段的 system 消息模型归属不了（PR#462 实测复述率 13/14）。
    assert tail.role != "system", "框架还在中段用 system 角色说话"
    assert "ATLAS" in (tail.content or ""), "压缩结果里应含已查过的条目台账"


# ── 接线：agent_loop 分发路径确实走缓存（最易"静默没生效"的一环）──────────

@pytest.mark.asyncio
async def test_agent_loop_dispatch_uses_cache(tmp_path: Path, monkeypatch):
    """同一 turn 内重复调同一只读工具：真实工具只应被执行 1 次，其余走缓存。"""
    from core import tool_registry
    from core.agent_loop import run_loop
    from core.harness import NodeHarness, SummarizerConfig
    from core.llm import LLMResponse

    calls: list[dict] = []

    async def _fake_search_kb(*, state, **kwargs):
        calls.append(kwargs)
        return {"status": "success", "total_matched": 1, "items": ["x" * 500]}

    monkeypatch.setitem(tool_registry._REGISTRY.executors, "search_kb", _fake_search_kb)
    monkeypatch.setitem(
        tool_registry._REGISTRY.tools, "search_kb",
        tool_registry.ToolDefinition(
            name="search_kb", description="d",
            parameters_schema={"type": "object", "properties": {}},
            # 判据来自声明：真实的 search_kb 就是这么声明的，假的也必须一致，
            # 否则测的是一个生产中不存在的工具。
            replayable_read=True,
        ),
    )

    def _tc(i: int) -> dict:
        return {"id": f"c{i}", "type": "function",
                "function": {"name": "search_kb", "arguments": '{"query": "ATLAS"}'}}

    class _FakeLLM:
        def __init__(self) -> None:
            self.n = 0

        async def chat(self, messages, **kw):
            self.n += 1
            if self.n == 1:
                # 一轮里连发 6 次完全相同的调用（复刻 checklist 循环）
                return LLMResponse(content=None, tool_calls=[_tc(i) for i in range(6)],
                                    finish_reason="tool_calls", usage={})
            return LLMResponse(content="done", tool_calls=[],
                                finish_reason="stop", usage={})

    harness = NodeHarness(
        node_type="literature", max_turns=3, tools=["search_kb"],
        summarizer=SummarizerConfig(enabled=False),
    )
    state = State.new(node_type="literature", base_dir=tmp_path, project_id="p_wire")
    await run_loop(harness, state, [], _FakeLLM())

    assert len(calls) == 1, f"真实工具应只执行 1 次（其余命中缓存），实际 {len(calls)} 次"
    hits = [json.loads(line)["event"] for line in
            state.transcript_path.read_text(encoding="utf-8").splitlines()
            if '"tool_call_cache_hit"' in line]
    assert len(hits) == 5, f"应有 5 次缓存命中事件，实际 {len(hits)}"


# ── 判据来自声明，不是名单（2026-08-13）────────────────────────────────────


def test_the_judgment_comes_from_the_declaration_not_from_a_list() -> None:
    """凡是自己声明了 replayable_read 的工具，都必须被覆盖 —— 一个不漏。

    这里原本是一份硬编码的 17 项白名单，和 `ToolDefinition.replayable_read`
    是同一个问题的第二份答案。两份已经双向分叉、且两边都不报错。
    """
    import shared.tools  # noqa: F401  # 触发注册
    from core.tool_registry import all_tool_names, get_tool

    declared = {
        name for name in all_tool_names()
        if getattr(get_tool(name), "replayable_read", False)
    }
    assert declared, "注册表里一个 replayable_read 都没有，说明扫的地方不对"
    assert declared <= tc.cacheable_tools()


def test_read_file_is_covered() -> None:
    """v26 实测：hypothesis 连读同一个 research_plan.md **815 次**，本该在第 30
    次触发的病态循环告警一次都没响 —— 因为那份名单是 KB/artifact 时代写的，
    workspace-first 之后成为主力读工具的 read_file 从来没被加进去，而它**早就
    在自己的 ToolDefinition 上声明了 replayable_read=True**。代价 117M tokens。
    """
    import shared.tools  # noqa: F401

    covered = tc.cacheable_tools()
    for name in ("read_file", "list_files", "search_files", "read_own_prior_attempt"):
        assert name in covered, f"{name} 声明了纯读却没被覆盖"


def test_a_newly_declared_tool_is_covered_without_touching_this_module() -> None:
    """真正的不变量：**新东西默认被覆盖**。

    名单式护栏的病根不是"漏了哪一个"，而是"新增的默认漏过"。所以判据不能是
    "名单里有没有它"，而必须是"它自己声明了没有"。
    """
    from core.tool_registry import ToolDefinition, register_tool

    async def _noop(state, **kwargs):
        return {"status": "success"}

    register_tool(
        ToolDefinition(
            name="a_brand_new_read_tool",
            description="test-only",
            parameters_schema={"type": "object", "properties": {}},
            replayable_read=True,
        ),
        _noop,
    )
    assert "a_brand_new_read_tool" in tc.cacheable_tools()


def test_a_write_tool_is_never_cacheable() -> None:
    """containment 没放松：没声明纯读的工具一律不缓存（宁漏不错）。"""
    import shared.tools  # noqa: F401

    covered = tc.cacheable_tools()
    for name in ("save_artifact", "write_file", "create_claim", "run_node"):
        assert name not in covered

"""病态重复熔断必须**改变控制流**，不能只是换一段文案。

回归依据（2026-08-21 实测）：`HARD_REPEAT=30` 触发后返回 `status="error"`，
模型又发了 34 次完全相同的调用，一共 64 次 —— 报错不改变任何东西，模型可以
无限忽略它。

    一个可以被无限忽略的熔断器不是熔断器。

本文件钉两件事：

  1. 阈值降到 8（≤10）—— 真成本是每次重复都把整个上下文重发一遍，30 次 =
     30 个满上下文轮次白烧。
  2. 无视 error 继续重复 → 做**结构性**的事：登记 blocker + 请求 loop 收口。
     判据故意不写"error 文案里有没有某个词"，而写"hook_state 里有没有那两
     样东西" —— 前者换个措辞就悄悄失效，后者是控制流本身。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import shared.tools  # noqa: F401  # 注册真实工具（判据来自注册表声明）
from core import tool_call_cache as tc
from core.state import State


class _State:
    """带 hook_state / 身份 / transcript 的最小 state。"""

    def __init__(self) -> None:
        self.hook_state: dict = {}
        self.run_id = "run_test"
        self.node_type = "curator"
        self.events: list[tuple[str, dict]] = []

    def append_transcript(self, event: str, **fields) -> None:
        self.events.append((event, fields))


def _repeat(state, n: int, tool: str = "search_kb", args: dict | None = None):
    """先真执行一次登记，再重复 lookup n-1 次，返回最后一次的 envelope。"""
    args = args if args is not None else {"query": "ATLAS"}
    tc.store(state, tool, args, {"status": "success", "total_matched": 3}, turn=1)
    last = None
    for _ in range(n - 1):
        last = tc.lookup(state, tool, args)
    return last


# ── (a) 阈值 ────────────────────────────────────────────────────────────────

def test_hard_threshold_is_low_enough_to_matter() -> None:
    """30 次相同调用 = 30 个满上下文模型轮次。降到 8-10 档。"""
    assert tc.SOFT_REPEAT < tc.HARD_REPEAT <= 10
    assert tc.HARD_REPEAT < tc.BREAK_REPEAT, "停机档必须严于报错档"


def test_error_directive_still_arrives_before_the_break() -> None:
    """HARD..BREAK 之间是"给自纠机会"的窗口，必须先报错、且预告要停机。"""
    s = _State()
    env = _repeat(s, tc.HARD_REPEAT)
    assert env["status"] == "error"
    assert env["repeat_count"] == tc.HARD_REPEAT
    assert env.get("run_terminated") is not True, "报错档不该直接停机"
    assert "blockers" not in s.hook_state
    assert "_loop_terminal" not in s.hook_state
    # 预告是"控制流会变"，不是又一句"请注意" —— 合法取值必须送到调用方手上。
    assert env["escalates_at_repeat"] == tc.BREAK_REPEAT


# ── (b) 无视 error 之后必须发生结构性的事 ──────────────────────────────────

def test_ignoring_the_error_terminates_the_run() -> None:
    s = _State()
    env = _repeat(s, tc.BREAK_REPEAT)

    # 1. 返回值明说本 run 已终止
    assert env["status"] == "error"
    assert env["run_terminated"] is True

    # 2. 登记了一条真 blocker —— executor 读 hook_state["blockers"] 判终态，
    #    orchestrator 的派发闸读它决定要不要重派。形状与 report_blocker 同源。
    blockers = s.hook_state["blockers"]
    assert len(blockers) == 1
    assert blockers[0]["category"] in {"missing_capability", "other"}
    assert blockers[0]["reported_by"] == "framework:tool_call_cache"
    assert blockers[0]["requested_action"], "重派方必须拿到'改什么才值得再跑'"

    # 3. 通过既有的 _loop_terminal 通道请求 loop 收口（owner hook 用的同一条）
    terminal = s.hook_state["_loop_terminal"]
    assert terminal["status"] == "failed"
    assert terminal["requested_by"] == "tool_call_cache"

    assert any(e == "tool_call_repeat_circuit_break" for e, _ in s.events)


def test_the_read_findings_survive_the_break() -> None:
    """停机前把本 run 已确立的只读结论带进 blocker 正文。

    这些结论只活在 hook_state 的缓存里，run 一结束就没了 —— 不带走等于这一趟
    读的全白读，下一趟还要再读一遍。
    """
    s = _State()
    tc.store(s, "search_kb", {"query": "SPECIAL_MARKER"},
             {"status": "success", "total_matched": 7}, turn=1)
    _repeat(s, tc.BREAK_REPEAT, args={"query": "ATLAS"})
    summary = s.hook_state["blockers"][0]["summary"]
    assert "SPECIAL_MARKER" in summary


def test_the_break_is_recorded_once_not_every_call() -> None:
    """撞线后继续重复不再重复记账，但 envelope 一直是终止态。"""
    s = _State()
    _repeat(s, tc.BREAK_REPEAT + 8)
    assert len(s.hook_state["blockers"]) == 1
    assert len([e for e, _ in s.events if e == "tool_call_repeat_circuit_break"]) == 1


# ── (c) "连续"由构造保证：中途真做了事，计数自己归零 ────────────────────────

def test_a_real_write_in_between_resets_everything() -> None:
    """写操作 bump 代次 → 整份缓存失效 → 不该被判成病态重复。

    这就是为什么不需要另立一个"连续次数"计数器：entry 的 hits 还能涨到 12，
    本身就证明这期间一次写操作都没有。
    """
    s = _State()
    for _ in range(2):
        tc.store(s, "search_kb", {"query": "ATLAS"},
                 {"status": "success"}, turn=1)
        for _ in range(tc.BREAK_REPEAT - 2):
            tc.lookup(s, "search_kb", {"query": "ATLAS"})
        # 非纯读工具执行 → generation += 1
        tc.store(s, "create_claim", {"claim_text": "progress"},
                 {"status": "success"}, turn=2)
        assert tc.lookup(s, "search_kb", {"query": "ATLAS"}) is None

    assert "blockers" not in s.hook_state
    assert "_loop_terminal" not in s.hook_state


# ── (d) 接线：agent_loop 真的会停 ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_agent_loop_actually_stops(tmp_path: Path, monkeypatch) -> None:
    """最重要的一条：机制存在 ≠ 接到了路径上。

    让 LLM **无限**重复同一个只读调用（每轮一次，永不停）。没有熔断就会一直
    跑到 max_turns；接上了就必须在 BREAK 附近终止，且 run 以 blocker 收尾。
    """
    from core import tool_registry
    from core.agent_loop import run_loop
    from core.harness import NodeHarness, SummarizerConfig
    from core.llm import LLMResponse

    calls: list[dict] = []

    async def _fake_search_kb(*, state, **kwargs):
        calls.append(kwargs)
        return {"status": "success", "total_matched": 1}

    monkeypatch.setitem(tool_registry._REGISTRY.executors, "search_kb", _fake_search_kb)
    monkeypatch.setitem(
        tool_registry._REGISTRY.tools, "search_kb",
        tool_registry.ToolDefinition(
            name="search_kb", description="d",
            parameters_schema={"type": "object", "properties": {}},
            replayable_read=True,
        ),
    )

    class _StubbornLLM:
        """永远发同一个调用，永远不收手 —— 就是实测里那 64 次的行为。"""

        def __init__(self) -> None:
            self.n = 0

        async def chat(self, messages, **kw):
            self.n += 1
            return LLMResponse(
                content=f"attempt {self.n}",   # 正文每轮不同 → 绕开进展熔断的复读判据
                tool_calls=[{
                    "id": f"c{self.n}", "type": "function",
                    "function": {"name": "search_kb",
                                 "arguments": '{"query": "ATLAS"}'},
                }],
                finish_reason="tool_calls", usage={},
            )

    harness = NodeHarness(
        node_type="curator", max_turns=200, tools=["search_kb"],
        summarizer=SummarizerConfig(enabled=False),
    )
    state = State.new(node_type="curator", base_dir=tmp_path, project_id="p_break")
    llm = _StubbornLLM()
    result = await run_loop(harness, state, [], llm)

    assert result.status == "failed", f"应被熔断终止，实际 {result.status}"
    assert llm.n <= tc.BREAK_REPEAT + 2, (
        f"熔断没接到路径上：模型跑了 {llm.n} 轮才停")
    assert len(calls) == 1, "真实工具只该执行一次，其余全部命中缓存"

    blockers = state.hook_state.get("blockers") or []
    assert len(blockers) == 1, "本 run 必须以一条 blocker 收尾"
    assert blockers[0]["reported_by"] == "framework:tool_call_cache"

    events = [json.loads(line)["event"]
              for line in state.transcript_path.read_text(encoding="utf-8").splitlines()]
    assert "tool_call_repeat_circuit_break" in events

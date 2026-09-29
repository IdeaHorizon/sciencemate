"""并行工具派发（#736）：安全前缀合批并发，记账路径与串行逐字节同形。

判据是**结构性**的（区间重叠 / 先后序），不是墙钟阈值 —— 串行执行下
B.start > A.end 恒成立，与机器快慢无关（判据不许依赖运行环境）。
"""
from __future__ import annotations

import asyncio
import json
import time

import pytest

from core.agent_loop import run_loop
from core.bootstrap import bootstrap
from core.harness import NodeHarness
from core.llm import LLMMessage, LLMResponse
from core.state import State
from core.tool_registry import ToolDefinition, register_tool

#: {tool_key: (start, end)} —— 每次执行记录真实起止时刻
_SPANS: dict[str, tuple[float, float]] = {}


@pytest.fixture(autouse=True)
def _setup():
    bootstrap()

    async def _read(state, key: str = "", **kw):
        t0 = time.monotonic()
        await asyncio.sleep(0.1)
        _SPANS[f"read:{key}"] = (t0, time.monotonic())
        return {"status": "success", "key": key}

    async def _write(state, key: str = "", **kw):
        t0 = time.monotonic()
        await asyncio.sleep(0.1)
        _SPANS[f"write:{key}"] = (t0, time.monotonic())
        return {"status": "success", "key": key}

    for name, fn, replayable in (("probe_read", _read, True),
                                 ("probe_write", _write, False)):
        try:
            register_tool(ToolDefinition(
                name=name, description="test probe",
                parameters_schema={"type": "object", "properties": {
                    "key": {"type": "string"}}},
                replayable_read=replayable,
            ), fn)
        except Exception:
            pass       # 已注册（同进程多测试）
    _SPANS.clear()
    yield


def _call(name, key, cid):
    return {"id": cid, "type": "function",
            "function": {"name": name,
                         "arguments": json.dumps({"key": key})}}


class _OneTurn:
    """第一轮发出给定 calls，第二轮收尾。"""

    def __init__(self, calls):
        self.calls = calls
        self.turn = 0

    async def chat(self, messages, **kw):
        self.turn += 1
        if self.turn == 1:
            return LLMResponse(content="干活", tool_calls=self.calls,
                               finish_reason="tool_calls", usage={})
        return LLMResponse(content="做完了。", tool_calls=[],
                           finish_reason="stop", usage={})


def _harness():
    return NodeHarness(node_type="literature", system_prompt="t",
                       tools=["probe_read", "probe_write"], max_turns=8)


def _events(state, name):
    lines = [json.loads(ln) for ln in
             state.transcript_path.read_text(encoding="utf-8").splitlines()
             if ln.strip()]
    return [e for e in lines if e.get("event") == name]


def _overlapping(a, b):
    return _SPANS[a][0] < _SPANS[b][1] and _SPANS[b][0] < _SPANS[a][1]


@pytest.mark.asyncio
async def test_consecutive_reads_run_concurrently(tmp_path):
    state = State.new("literature", tmp_path / "r1", project_id="p")
    calls = [_call("probe_read", k, f"c{k}") for k in ("a", "b", "d")]
    result = await run_loop(_harness(), state,
                            [LLMMessage(role="user", content="go")],
                            _OneTurn(calls))
    assert result.status == "completed"

    ev = _events(state, "parallel_tool_dispatch")
    assert len(ev) == 1 and ev[0]["count"] == 3

    # 结构性并发判据：串行下 b.start > a.end 恒成立；并发下三对区间两两重叠
    assert _overlapping("read:a", "read:b")
    assert _overlapping("read:b", "read:d")

    # 记账顺序与串行同形：tool 消息按原 call 顺序、id 一一对应
    tool_msgs = [m for m in result.messages if getattr(m, "role", "") == "tool"]
    assert [m.tool_call_id for m in tool_msgs] == ["ca", "cb", "cd"]
    # 每条结果内容对得上自己的 args（不是三份同一个结果）
    for m, k in zip(tool_msgs, ("a", "b", "d")):
        assert json.loads(m.content)["key"] == k


@pytest.mark.asyncio
async def test_write_barrier_stops_the_prefix(tmp_path):
    """写调用之后的读保持惰性串行 —— read-after-write 顺序不许打破。"""
    state = State.new("literature", tmp_path / "r2", project_id="p")
    calls = [_call("probe_write", "w", "cw"),
             _call("probe_read", "x", "cx"),
             _call("probe_read", "y", "cy")]
    result = await run_loop(_harness(), state,
                            [LLMMessage(role="user", content="go")],
                            _OneTurn(calls))
    assert result.status == "completed"
    assert not _events(state, "parallel_tool_dispatch")   # 前缀长 0，无并发
    # 读严格发生在写完成之后
    assert _SPANS["read:x"][0] >= _SPANS["write:w"][1]
    assert _SPANS["read:y"][0] >= _SPANS["read:x"][1]     # 且彼此也串行


@pytest.mark.asyncio
async def test_reads_before_write_parallelize_write_stays_after(tmp_path):
    state = State.new("literature", tmp_path / "r3", project_id="p")
    calls = [_call("probe_read", "m", "cm"),
             _call("probe_read", "n", "cn"),
             _call("probe_write", "z", "cz")]
    result = await run_loop(_harness(), state,
                            [LLMMessage(role="user", content="go")],
                            _OneTurn(calls))
    assert result.status == "completed"
    ev = _events(state, "parallel_tool_dispatch")
    assert len(ev) == 1 and ev[0]["count"] == 2           # 只有读前缀
    assert _overlapping("read:m", "read:n")
    assert _SPANS["write:z"][0] >= _SPANS["read:m"][1]    # 写永远在后
    tool_msgs = [m for m in result.messages if getattr(m, "role", "") == "tool"]
    assert [m.tool_call_id for m in tool_msgs] == ["cm", "cn", "cz"]


@pytest.mark.asyncio
async def test_kill_switch_restores_pure_serial(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_PARALLEL_TOOLS", "off")
    state = State.new("literature", tmp_path / "r4", project_id="p")
    calls = [_call("probe_read", "s1", "c1"), _call("probe_read", "s2", "c2")]
    result = await run_loop(_harness(), state,
                            [LLMMessage(role="user", content="go")],
                            _OneTurn(calls))
    assert result.status == "completed"
    assert not _events(state, "parallel_tool_dispatch")
    assert not _overlapping("read:s1", "read:s2")


@pytest.mark.asyncio
async def test_single_read_does_not_bother_with_gather(tmp_path):
    state = State.new("literature", tmp_path / "r5", project_id="p")
    calls = [_call("probe_read", "solo", "c1")]
    await run_loop(_harness(), state,
                   [LLMMessage(role="user", content="go")], _OneTurn(calls))
    assert not _events(state, "parallel_tool_dispatch")


@pytest.mark.asyncio
async def test_duplicate_reads_still_hit_cache_not_amplified(tmp_path):
    """同参重复读必须仍走缓存（51,937 次 search_kb 的教训）——并行化不许把
    "1 次真跑 + N 次缓存"放大成 N 次真跑。"""
    state = State.new("literature", tmp_path / "r6", project_id="p")
    calls = [_call("probe_read", "dup", f"c{i}") for i in range(4)]
    result = await run_loop(_harness(), state,
                            [LLMMessage(role="user", content="go")],
                            _OneTurn(calls))
    assert result.status == "completed"
    # 真实执行次数靠 span 记录数判不出（同 key 覆盖）——直接数 transcript：
    cache_hits = _events(state, "tool_call_cache_hit")
    assert len(cache_hits) == 3, f"4 次同参读应 1 真跑 + 3 缓存，实际缓存 {len(cache_hits)}"

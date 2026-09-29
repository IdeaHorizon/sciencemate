"""Summarizer 单元 smoke test。不联网，不调真实 LLM。

用法：python -m tests.test_summarizer
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.harness import NodeHarness, SummarizerConfig
from core.llm import LLMMessage, LLMResponse
from core.state import State
from core.summarizer import (
    estimate_tokens,
    register_strategy,
    run_summarizer,
    should_compress,
    split_for_compression,
)


# ── fake LLM：record calls，返回固定文本 ─────────────────────────────────────

class FakeLLM:
    def __init__(self) -> None:
        self.calls: list[list[LLMMessage]] = []

    async def chat(self, messages, **kw):
        self.calls.append(messages)
        return LLMResponse(
            content="（这是 fake LLM 生成的压缩摘要：agent 跑了 X，得到了 Y。）",
            tool_calls=[],
            finish_reason="stop",
            usage={"total_tokens": 100},
        )


# ── 构造 messages：1 system + 1 user + 5 assistant/tool 轮 ──────────────────

def make_messages() -> list[LLMMessage]:
    msgs: list[LLMMessage] = [
        LLMMessage(role="system", content="system prompt: 你是研究 agent。" + ("a" * 500)),
        LLMMessage(role="user", content="research question: 解决 X 问题。" + ("b" * 500)),
    ]
    for i in range(1, 6):
        msgs.append(LLMMessage(
            role="assistant",
            content=f"assistant turn {i} 推理：" + ("c" * 800),
            tool_calls=[{
                "id": f"call_{i}",
                "type": "function",
                "function": {"name": "save_artifact" if i == 3 else "search_memory",
                              "arguments": f'{{"q": "step_{i}"}}'},
            }],
        ))
        msgs.append(LLMMessage(
            role="tool",
            tool_call_id=f"call_{i}",
            name="save_artifact" if i == 3 else "search_memory",
            content=(f"tool result {i}: " + ("d" * 1500)),
        ))
    return msgs


# ── tests ────────────────────────────────────────────────────────────────────

def test_estimate_tokens():
    msgs = make_messages()
    est = estimate_tokens(msgs)
    assert est > 1000, f"expected >1000, got {est}"
    print(f"  ✓ estimate_tokens: {est} tokens")


def test_should_compress():
    harness = NodeHarness(
        node_type="test", max_context_tokens=4000,
        summarizer=SummarizerConfig(enabled=True, trigger_threshold=0.5, strategy="truncate"),
    )
    msgs = make_messages()
    trigger, est = should_compress(harness, msgs, turn=2)
    assert trigger, f"expected trigger=True (est={est}, threshold=2000)"
    print(f"  ✓ should_compress=True at est={est}, threshold=2000")

    # disabled → 不触发
    harness2 = NodeHarness(
        node_type="test", summarizer=SummarizerConfig(enabled=False),
    )
    trigger2, _ = should_compress(harness2, msgs, turn=2)
    assert not trigger2
    print("  ✓ disabled summarizer 不触发")


def test_split_for_compression():
    msgs = make_messages()
    head, middle, tail = split_for_compression(msgs, keep_last_n_turns=2)
    # 头部应包含 system + 第一条 user
    assert head[0].role == "system"
    assert head[1].role == "user"
    assert len(head) == 2
    # 尾部应包含最后 2 个 assistant 起始的轮 → 4 条消息
    assistant_in_tail = sum(1 for m in tail if m.role == "assistant")
    assert assistant_in_tail == 2
    # 中间段应包含前 3 个 assistant 轮 → 6 条消息
    assistant_in_middle = sum(1 for m in middle if m.role == "assistant")
    assert assistant_in_middle == 3
    print(f"  ✓ split: head={len(head)} middle={len(middle)} tail={len(tail)}")


def test_truncate_strategy():
    from core.summarizer import _strategy_truncate, SummarizerContext

    harness = NodeHarness(
        node_type="test", max_context_tokens=4000,
        summarizer=SummarizerConfig(
            enabled=True, strategy="truncate",
            keep_last_n_turns=2, keep_tool_calls=["save_artifact"],
        ),
    )
    state = State(run_id="r1", node_type="test", root=Path("/tmp"))
    msgs = make_messages()
    ctx = SummarizerContext(
        harness=harness, state=state, messages=msgs,
        estimated_tokens=estimate_tokens(msgs), llm=FakeLLM(),  # type: ignore
    )
    new_msgs = asyncio.run(_strategy_truncate(ctx))
    assert len(new_msgs) < len(msgs), f"truncate 应该缩短 messages: before={len(msgs)} after={len(new_msgs)}"
    # 该有的 save_artifact 调用保留下来
    artifact_calls = [
        m for m in new_msgs
        if m.role == "assistant" and m.tool_calls
        and any((tc.get("function") or {}).get("name") == "save_artifact" for tc in m.tool_calls)
    ]
    assert len(artifact_calls) >= 1, "save_artifact tool call 应该被保留"
    print(f"  ✓ truncate: {len(msgs)} → {len(new_msgs)} messages，save_artifact 保留")


def test_drop_tool_results_strategy():
    from core.summarizer import _strategy_drop_tool_results, SummarizerContext

    harness = NodeHarness(
        node_type="test", max_context_tokens=4000,
        summarizer=SummarizerConfig(
            enabled=True, strategy="drop_tool_results",
            keep_last_n_turns=2, keep_tool_calls=["save_artifact"],
        ),
    )
    state = State(run_id="r1", node_type="test", root=Path("/tmp"))
    msgs = make_messages()
    ctx = SummarizerContext(
        harness=harness, state=state, messages=msgs,
        estimated_tokens=estimate_tokens(msgs), llm=FakeLLM(),  # type: ignore
    )
    new_msgs = asyncio.run(_strategy_drop_tool_results(ctx))
    # 总条数不变（不丢消息），但中段 tool result 被截短
    tool_msgs_in_middle = [
        m for m in new_msgs if m.role == "tool" and m.name != "save_artifact"
    ]
    short_count = sum(1 for m in tool_msgs_in_middle if len(m.content or "") <= 250)
    assert short_count >= 1
    print(f"  ✓ drop_tool_results: {short_count} tool result 被截短到 ≤250 字符")


def test_llm_strategy_with_fake_llm():
    from core.summarizer import _strategy_llm, SummarizerContext

    harness = NodeHarness(
        node_type="test", max_context_tokens=4000,
        summarizer=SummarizerConfig(
            enabled=True, strategy="llm",
            keep_last_n_turns=2, keep_tool_calls=["save_artifact"],
            instruction="自定义压缩指引：把所有 search_memory 结果丢掉。",
        ),
    )
    state = State(run_id="r1", node_type="test", root=Path("/tmp"))
    msgs = make_messages()
    fake = FakeLLM()
    ctx = SummarizerContext(
        harness=harness, state=state, messages=msgs,
        estimated_tokens=estimate_tokens(msgs), llm=fake,  # type: ignore
    )
    new_msgs = asyncio.run(_strategy_llm(ctx))
    assert len(fake.calls) == 1, "应该调用了一次 LLM"
    assert len(new_msgs) < len(msgs)
    # 检查压缩 system message 已经被注入
    # 判据按 marker 找，不按角色 —— 这条 notice 的角色是 framework-notice
    # （user + 归属信封），中段的 system 消息模型归属不了（PR#462 实测）。
    notice = [m for m in new_msgs if "历史压缩" in (m.content or "")]
    assert len(notice) >= 1, "应该有一条 '历史压缩' 的 system message"
    # 自定义 instruction 被传给了 LLM
    instruction_msg = fake.calls[0][0]
    assert "自定义压缩指引" in (instruction_msg.content or "")
    print(f"  ✓ llm strategy: {len(msgs)} → {len(new_msgs)}; 自定义 instruction 被使用")


def test_run_summarizer_dispatch():
    """整体 dispatch：harness.summarizer.strategy = 'truncate' 应该路由到 truncate 策略。"""
    harness = NodeHarness(
        node_type="test", max_context_tokens=4000,
        summarizer=SummarizerConfig(
            enabled=True, strategy="truncate", keep_last_n_turns=2,
        ),
    )
    state = State(run_id="r1", node_type="test", root=Path("/tmp"))
    msgs = make_messages()
    new_msgs = asyncio.run(run_summarizer(
        harness, state, msgs, FakeLLM(), turn=1,  # type: ignore
        estimated_tokens=estimate_tokens(msgs),
    ))
    assert len(new_msgs) < len(msgs)
    print("  ✓ run_summarizer 正确 dispatch 到 strategy=truncate")


def test_never_strategy():
    """trigger_type=never 不触发。"""
    harness = NodeHarness(
        node_type="test", max_context_tokens=10,   # 极低，但 never 不触发
        summarizer=SummarizerConfig(enabled=True, trigger_type="never"),
    )
    msgs = make_messages()
    trigger, _ = should_compress(harness, msgs, turn=2)
    assert not trigger
    print("  ✓ trigger_type=never 不触发")


if __name__ == "__main__":
    print("== Summarizer smoke tests ==")
    tests = [
        test_estimate_tokens,
        test_should_compress,
        test_split_for_compression,
        test_truncate_strategy,
        test_drop_tool_results_strategy,
        test_llm_strategy_with_fake_llm,
        test_run_summarizer_dispatch,
        test_never_strategy,
    ]
    for t in tests:
        print(f"- {t.__name__}")
        t()
    print("\nALL PASS ✓")


# ─────────────────────────────────────────────────────────────────────────────
# v1.6 改进测试
# ─────────────────────────────────────────────────────────────────────────────


def test_estimate_tokens_uses_tiktoken_when_available():
    """v1.6 G: tiktoken 可用时应精算（vs char/4）。"""
    from core.llm import LLMMessage
    from core.summarizer import estimate_tokens, _get_encoder

    msgs = [LLMMessage(role="user", content="hello 你好 world")]
    n = estimate_tokens(msgs)

    if _get_encoder() is not None:
        # tiktoken 精算：英文 hello/world 各 1，中文你/好 各 1，加 4 overhead
        # 总共约 6-10，char/4 仅约 4
        assert n >= 5, f"tiktoken 估算应 >= 5, got {n}"
    else:
        # fallback char/4
        assert n == len("hello 你好 world") // 4


def test_extract_keep_tool_pairs_preserves_all_tool_results():
    """v1.6 E: assistant.tool_calls=[A,B] 中只 A 在 keep，**仍要保 A 和 B 的
    tool result**（OpenAI API 硬契约 —— 否则报 tool_call_id 不匹配）。"""
    from core.llm import LLMMessage
    from core.summarizer import extract_keep_tool_pairs

    middle = [
        LLMMessage(
            role="assistant",
            content=None,
            tool_calls=[
                {"function": {"name": "save_artifact", "arguments": "{}"}, "id": "call_A"},
                {"function": {"name": "list_artifacts", "arguments": "{}"}, "id": "call_B"},
            ],
        ),
        LLMMessage(role="tool", name="save_artifact", tool_call_id="call_A", content="ok"),
        LLMMessage(role="tool", name="list_artifacts", tool_call_id="call_B", content="3 items"),
    ]
    out = extract_keep_tool_pairs(middle, keep_tool_calls=["save_artifact"])
    # 应该 3 条全保（assistant + 两条 tool result，pair 完整）
    assert len(out) == 3, f"应保全 pair (3 条), got {len(out)}"
    assert out[0].role == "assistant"
    tool_names = {m.name for m in out if m.role == "tool"}
    assert tool_names == {"save_artifact", "list_artifacts"}, (
        f"两条 tool result 都该在, got {tool_names}"
    )


def test_extract_prior_summary_finds_compression_notice():
    """v1.6 H: 检测 head 里的"上次压缩 notice"，抽出摘要文本。"""
    from core.llm import LLMMessage
    from core.summarizer import _extract_prior_summary

    head = [
        LLMMessage(role="system", content="initial system"),
        LLMMessage(role="user", content="task"),
        LLMMessage(role="system", content=(
            "📦 历史压缩（turn 8）：把中间 30 条 messages 压成下面这段："
            "\n\n## 历史压缩摘要\nagent 在 turn 2-7 调研了 A / B 两种方法,"
            " 选了 A 因为 X。\n\n（5 条 keep_tool_calls 相关 messages 已原样保留在下方。）"
        )),
    ]
    summary = _extract_prior_summary(head)
    assert summary is not None
    assert "agent 在 turn 2-7" in summary
    assert "选了 A 因为 X" in summary
    # 不应含"（X 条 keep...）"括号说明
    assert "keep_tool_calls" not in summary


def test_extract_prior_summary_returns_none_for_clean_head():
    from core.llm import LLMMessage
    from core.summarizer import _extract_prior_summary
    head = [
        LLMMessage(role="system", content="initial"),
        LLMMessage(role="user", content="task"),
    ]
    assert _extract_prior_summary(head) is None


def test_llm_instruction_forbids_phantom_ids():
    """v1.6 C: 默认 LLM instruction 必须明确禁止编 id。"""
    from core.summarizer import _DEFAULT_LLM_INSTRUCTION
    assert ("不许编造" in _DEFAULT_LLM_INSTRUCTION or
            "原样" in _DEFAULT_LLM_INSTRUCTION)
    # 关键 id 类型都提到
    assert "claim_" in _DEFAULT_LLM_INSTRUCTION
    assert "chunk_" in _DEFAULT_LLM_INSTRUCTION


@pytest.mark.asyncio
async def test_run_summarizer_writes_transcript_event(tmp_path):
    """v1.6 A: run_summarizer 应在 transcript 写 tokens_before / after / ratio。"""
    import json
    from core.harness import NodeHarness, SummarizerConfig
    from core.llm import LLMMessage
    from core.state import State
    from core.summarizer import run_summarizer

    state = State.new(node_type="test", base_dir=tmp_path, project_id="p")
    harness = NodeHarness(
        node_type="test",
        max_context_tokens=10000,
        summarizer=SummarizerConfig(strategy="truncate", keep_last_n_turns=1),
    )
    msgs = [
        LLMMessage(role="system", content="sys"),
        LLMMessage(role="user", content="hi"),
        LLMMessage(role="assistant", content="a1"),
        LLMMessage(role="assistant", content="a2"),
        LLMMessage(role="assistant", content="a3"),
    ]
    await run_summarizer(harness, state, msgs, llm=FakeLLM(), turn=5,
                         estimated_tokens=8500)
    lines = state.transcript_path.read_text(encoding="utf-8").splitlines()
    events = [json.loads(l) for l in lines if l.strip()]
    compress_evs = [e for e in events if e.get("event") == "summarizer_compress"]
    assert compress_evs, "应有 summarizer_compress 事件"
    e = compress_evs[0]
    assert e["tokens_before"] == 8500
    assert "tokens_after" in e
    assert "compression_ratio" in e
    assert e["strategy"] == "truncate"


@pytest.mark.asyncio
async def test_run_summarizer_secondary_protection(tmp_path):
    """v1.6 F: 压缩后仍 > 90%×max_context，应该跑 drop_tool_results 二次兜底。"""
    import json
    from core.harness import NodeHarness, SummarizerConfig
    from core.llm import LLMMessage
    from core.state import State
    from core.summarizer import run_summarizer

    state = State.new(node_type="test", base_dir=tmp_path, project_id="p")
    # max_context=100，touch threshold 0.9 → secondary 触发当 after > 90
    harness = NodeHarness(
        node_type="test",
        max_context_tokens=100,
        summarizer=SummarizerConfig(strategy="truncate", keep_last_n_turns=10),
    )
    # 让原 messages 不被压（keep_last=10，超过 turn 数）→ tokens_after ≈ before
    big_content = "x" * 1000  # 长 content 让 estimate 高
    msgs = [
        LLMMessage(role="system", content=big_content),
        LLMMessage(role="user", content=big_content),
        LLMMessage(role="assistant", content=big_content),
    ]
    await run_summarizer(harness, state, msgs, llm=FakeLLM(), turn=5,
                         estimated_tokens=1000)
    lines = state.transcript_path.read_text(encoding="utf-8").splitlines()
    events = [json.loads(l) for l in lines if l.strip()]
    secondary = [e for e in events if e.get("event") == "summarizer_compress_secondary"]
    # max_context=100, after >> 90 → 应触发二次保护
    assert secondary, "二次保护应触发"


# ── scratchpad hook 改进测试 ─────────────────────────────────────────────


def test_scratchpad_hook_turn_1_empty_injects_guidance(tmp_path):
    """turn 1 + 白板空 → 注入开场引导。"""
    from core.harness import NodeHarness
    from core.loop_hooks import HookContext
    from core.loop_hooks_builtin import _scratchpad_on_turn_start
    from core.state import State

    state = State.new(node_type="literature", base_dir=tmp_path, project_id="p")
    assert state.scratchpad == ""  # 空
    ctx = HookContext(
        harness=NodeHarness(node_type="literature"),
        state=state, messages=[], turn=1,
    )
    out = _scratchpad_on_turn_start(ctx)
    assert out is not None
    assert len(out) == 1
    content = out[0].content
    # 含第一性原理与覆盖语义
    assert "第一性原理" in content
    assert "整块替换" in content
    assert "write_scratchpad" in content


def test_scratchpad_hook_with_notes_injects_notes(tmp_path):
    """有板子 → 注入板子（不重复引导）。"""
    from core.harness import NodeHarness
    from core.loop_hooks import HookContext
    from core.loop_hooks_builtin import _scratchpad_on_turn_start
    from core.state import State

    state = State.new(node_type="literature", base_dir=tmp_path, project_id="p")
    state.scratchpad = "试过 X，不 work，因为 Y\n决定走 Z 路径"

    ctx = HookContext(
        harness=NodeHarness(node_type="literature"),
        state=state, messages=[], turn=5,
    )
    out = _scratchpad_on_turn_start(ctx)
    assert out is not None
    content = out[0].content
    assert "试过 X" in content
    assert "决定走 Z" in content
    # 不重复 first-principles 引导
    assert "第一性原理" not in content


def test_scratchpad_hook_turn_10_empty_reminds(tmp_path):
    """v1.6: 第 10/20/30 轮仍空 → 轻量提醒（不每轮 spam）。

    2026-07-09（P0-4）：intro 改按 state 级 flag 触发一次（不再按 turn==1），真实
    loop 里 turn 1 已消费 intro flag。这里先置 flag，再验证 turn 10 提醒。"""
    from core.harness import NodeHarness
    from core.loop_hooks import HookContext
    from core.loop_hooks_builtin import (
        _SCRATCHPAD_INTRO_KEY, _scratchpad_on_turn_start,
    )
    from core.state import State

    state = State.new(node_type="literature", base_dir=tmp_path, project_id="p")
    state.hook_state[_SCRATCHPAD_INTRO_KEY] = True    # intro 已在 turn 1 发过
    ctx10 = HookContext(harness=NodeHarness(node_type="literature"),
                         state=state, messages=[], turn=10)
    out = _scratchpad_on_turn_start(ctx10)
    assert out is not None
    assert "提醒" in out[0].content


def test_scratchpad_hook_mid_turn_empty_silent(tmp_path):
    """v1.6: 第 2/3/4/...9 轮空 → 不 spam（返 None）。

    2026-07-09（P0-4）：intro 已在 turn 1 消费掉 flag，中间轮才 silent。"""
    from core.harness import NodeHarness
    from core.loop_hooks import HookContext
    from core.loop_hooks_builtin import (
        _SCRATCHPAD_INTRO_KEY, _scratchpad_on_turn_start,
    )
    from core.state import State

    state = State.new(node_type="literature", base_dir=tmp_path, project_id="p")
    state.hook_state[_SCRATCHPAD_INTRO_KEY] = True    # intro 已在 turn 1 发过
    for t in [2, 3, 7, 11, 15]:
        ctx = HookContext(harness=NodeHarness(node_type="literature"),
                          state=state, messages=[], turn=t)
        out = _scratchpad_on_turn_start(ctx)
        assert out is None, f"turn {t} 应 silent, got {out}"


# ── 8 节点 yaml 启用 scratchpad ─────────────────────────────────────────────


@pytest.mark.parametrize("node_name", [
    "literature", "hypothesis", "data", "experiment",
    "postprocess", "writing", "_orchestrator",
])
def test_node_has_scratchpad_enabled(node_name):
    """v1.6 D: 8 节点都启 scratchpad（tool + hook）。

    scratchpad 是**跨轮**工作笔记：工具挂在工具面上、hook 每轮注入一次。换掉
    框架 loop 的节点两样都没有（没有轮，也没有 hook 触发点 —— 见
    `core/executor.py` 的 custom 分支），声明了也是死配置。判据用
    `runs_the_framework_loop()` 现算，不写节点名。
    """
    from core.custom_loop import runs_the_framework_loop
    from core.loader import load_harness

    if not runs_the_framework_loop(node_name):
        pytest.skip(f"{node_name} 用 custom loop：没有轮循环，scratchpad 无处落地")
    h = load_harness(node_name)
    assert "write_scratchpad" in h.tools, (
        f"{node_name} tools 应含 write_scratchpad"
    )
    assert "scratchpad" in h.loop_hooks, (
        f"{node_name} loop_hooks 应含 scratchpad"
    )


# ── v3.3: context 溢出安全（校准 + emergency 绕过 thrash 冷却）────────────────

def test_should_compress_calibration_triggers_earlier(tmp_path, monkeypatch):
    """校准系数让触发提前 —— 补 tiktoken 对 DeepSeek 等后端的系统性低估。
    raw est < threshold 但 est*calib > threshold 时应触发（旧行为漏掉）。"""
    import core.summarizer as sm
    msgs = make_messages()
    E = estimate_tokens(msgs)
    window = int(E * 2.3)   # threshold=window*0.5=E*1.15，落在 E 与 E*1.3 之间
    harness = NodeHarness(
        node_type="test", max_context_tokens=window,
        summarizer=SummarizerConfig(enabled=True, trigger_threshold=0.5, strategy="truncate"),
    )
    monkeypatch.setattr(sm, "_CONTEXT_CALIBRATION", 1.0)
    trig_no, _ = sm.should_compress(harness, msgs, turn=2)
    assert not trig_no, "无校准：raw est < threshold 不应触发"
    monkeypatch.setattr(sm, "_CONTEXT_CALIBRATION", 1.3)
    trig_yes, _ = sm.should_compress(harness, msgs, turn=2)
    assert trig_yes, "校准后 eff=est*1.3 > threshold 应触发"


def test_emergency_compress_bypasses_thrash_guard(tmp_path, monkeypatch):
    """逼近真实窗口时，emergency 压缩必须无视 thrash 冷却 —— 否则下一轮撞 400
    死锁（atomic-agents E2E 事故：压缩节省 <5% 触发冷却，逼近上限却不再压）。"""
    import core.summarizer as sm
    from core.summarizer import _THRASH_GUARD_KEY
    msgs = make_messages()
    E = estimate_tokens(msgs)
    monkeypatch.setattr(sm, "_CONTEXT_CALIBRATION", 1.3)
    monkeypatch.setattr(sm, "_CONTEXT_EMERGENCY_RATIO", 0.9)
    state = State.new(node_type="test", base_dir=tmp_path)
    state.hook_state[_THRASH_GUARD_KEY] = 10   # 激活冷却：turn<=10 正常路径应 False

    # window 小 → emergency(0.9*window=0.9E) <= eff(1.3E) → 无视 guard 强制压
    h_small = NodeHarness(
        node_type="test", max_context_tokens=int(E),
        summarizer=SummarizerConfig(enabled=True, trigger_threshold=0.5, strategy="truncate"),
    )
    trig, _ = sm.should_compress(h_small, msgs, turn=3, state=state)
    assert trig, "逼近窗口应无视 thrash guard 强制压缩"

    # window 大 → eff 远低于 emergency 且低于 threshold → guard 生效 → 不压
    h_big = NodeHarness(
        node_type="test", max_context_tokens=int(E * 100),
        summarizer=SummarizerConfig(enabled=True, trigger_threshold=0.5, strategy="truncate"),
    )
    trig2, _ = sm.should_compress(h_big, msgs, turn=3, state=state)
    assert not trig2, "远离窗口时 thrash guard 应生效（不压，避免无效重跑）"

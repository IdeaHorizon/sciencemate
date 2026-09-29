"""截断恢复（#223）+ tool_call arguments 上线前合法性收口（#224）。

## #223（jicq 实测，run 1785307479-d15500）
experiment 节点第 9 轮把 hook 注入的内部控制状态（plan_reminder / last_build /
engineering_control …）复读到 16384 token 上限被砍，`tool_calls` 因此为空 →
`if not response.tool_calls` 把它当成"模型决定收工"，节点直接 incomplete 结束。
一整轮上限的 token 花在吐内部状态上，真正的下一步动作从未发生。
原代码只留了一条 warning + transcript 事件就往下走。

## #224（jicq 实测，run 1785290848-77b927）
writing 节点某次请求 HTTP 400：`Unterminated string starting at: line 1
column 139`。看着像"我们发了非法 body"，但 httpx 的 `json=payload` 从 dict
序列化、body 必然合法。真病在一层里面：`tool_calls[].function.arguments`
**本身就是 JSON 字符串**，服务端会解析它。provider 截断 args 后（#184 签名），
坏 JSON 进了 assistant 历史，**此后每次请求都带着它** → 每次都 400，无法自愈。
"""
from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

import pytest

from core.agent_loop import run_loop
from core.harness import NodeHarness
from core.llm import (
    LLMMessage,
    LLMResponse,
    _msg_to_dict,
    sanitize_tool_calls_for_wire,
)
from core.state import State
from core.tool_registry import ToolDefinition, register_tool

# ── #224：arguments 合法性收口 ──────────────────────────────────────────────

_TRUNCATED_ARGS = '{"content": "## Verdict\\napprove_with_revi'


def test_truncated_args_reproduce_the_400_error():
    """先证明病因：body 合法，但 arguments 自身解析报的正是 jicq 那个错。"""
    d = _msg_to_dict(LLMMessage(role="assistant", content=None, tool_calls=[
        {"id": "c1", "type": "function",
         "function": {"name": "save_artifact", "arguments": _TRUNCATED_ARGS}}]))
    json.loads(json.dumps({"messages": [d]}))          # 整个 body 合法
    # 清洗后 arguments 必须可解析（否则服务端 400）
    json.loads(d["tool_calls"][0]["function"]["arguments"])


def test_sanitize_replaces_bad_args_with_valid_placeholder():
    clean, repaired = sanitize_tool_calls_for_wire([
        {"id": "c1", "type": "function",
         "function": {"name": "save_artifact", "arguments": _TRUNCATED_ARGS}}])
    assert repaired == ["save_artifact"]
    payload = json.loads(clean[0]["function"]["arguments"])
    assert "_framework_note" in payload
    assert payload["original_length"] == len(_TRUNCATED_ARGS)


def test_sanitize_leaves_valid_args_untouched():
    good = '{"a": 1, "b": "x"}'
    clean, repaired = sanitize_tool_calls_for_wire([
        {"id": "c1", "type": "function", "function": {"name": "t", "arguments": good}}])
    assert repaired == []
    assert clean[0]["function"]["arguments"] == good


def test_empty_arguments_leave_as_the_one_canonical_form():
    """这条原来断言的是 `repaired == []`，注释「空串按 {} 处理；缺字段不动」——
    "处理"只发生在判据里（`json.loads(raw or "{}")`），发出去的仍是 `""`。它把
    一个分叉钉成了设计（2026-09-16 yuankk：严格网关对 `""` 回 400，每轮复发）。

    现在的判据：空 / 缺 / None / 纯空白，出去**都是** `"{}"` —— 不算"修复"
    （没有东西坏），是唯一的规范形。
    """
    clean, repaired = sanitize_tool_calls_for_wire([
        {"id": "c1", "type": "function", "function": {"name": "t", "arguments": ""}},
        {"id": "c2", "type": "function", "function": {"name": "t2"}},
        {"id": "c3", "type": "function", "function": {"name": "t3", "arguments": "   "}},
        {"id": "c4", "type": "function", "function": {"name": "t4", "arguments": None}},
    ])
    assert repaired == [], "空参数不是坏参数，不该触发 #224 的占位与告警"
    assert [c["function"]["arguments"] for c in clean] == ["{}"] * 4
    assert sanitize_tool_calls_for_wire(None) == ([], [])


# ── 一个值一种形状：入口规范化 + 出口不变量 ─────────────────────────────────

_ARGUMENT_SHAPES = {
    "empty": "", "blank": "   ", "none": None, "missing": ..., "dict": {"path": "a"},
    "truncated": _TRUNCATED_ARGS, "list": "[]", "valid": '{"path": "a"}',
}


def _call(shape):
    fn = {"name": "read_file"}
    if shape is not ...:
        fn["arguments"] = shape
    return {"id": "c", "type": "function", "function": fn}


@pytest.mark.parametrize("name,shape", list(_ARGUMENT_SHAPES.items()))
def test_whatever_leaves_msg_to_dict_is_valid_json_on_the_wire(name, shape):
    """性质，不是名单：不论 arguments 进来是什么形状，出去的每个都能 json.loads。

    yuankk 撞的就是 `empty`：`read_file` 的 arguments 为 `""`，落进
    messages_checkpoint.json，续跑每轮重放、每轮 400。
    """
    d = _msg_to_dict(LLMMessage(role="assistant", content=None, tool_calls=[_call(shape)]))
    wire = d["tool_calls"][0]["function"]["arguments"]
    assert isinstance(wire, str)
    json.loads(wire)                                   # 不抛即通过
    json.loads(json.dumps({"messages": [d]}))          # 整个 body 也合法


@pytest.mark.parametrize("name,shape", [(k, v) for k, v in _ARGUMENT_SHAPES.items()
                                        if k in ("empty", "blank", "none", "missing", "dict")])
def test_the_response_parser_never_hands_the_framework_an_empty_arguments(name, shape):
    """入口：`_parse_chat_response` 是流式 / 非流式两条传输路的汇合点。空参数在
    这里就变成 "{}"，**新**历史与 checkpoint 从此只见一种形状。`agent_loop` 里那几处
    `or "{}"` 仍留着：磁盘上还有规范化之前写的 checkpoint（yuankk 那条就是），
    重放进执行侧时要靠它们兜 —— 与出口那道网同一个理由。"""
    from core.llm import _parse_chat_response

    data = {"choices": [{"message": {"role": "assistant", "content": None,
                                     "tool_calls": [_call(shape)]},
                         "finish_reason": "tool_calls"}], "usage": {}}
    resp = _parse_chat_response(data)
    args = resp.tool_calls[0]["function"]["arguments"]
    assert isinstance(args, str) and json.loads(args) == (shape if isinstance(shape, dict) else {})


def test_the_stream_slot_shape_is_canonicalised_too():
    """流式收尾产出的 slot 长这样：id 可能是 None、arguments 从 "" 累加。一个
    工具调用一个 delta 都没带参数时，slot 就停在 ""。"""
    from core.llm import _parse_chat_response

    slot = {"id": None, "type": "function", "function": {"name": "list_artifacts", "arguments": ""}}
    data = {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [slot]},
                         "finish_reason": "tool_calls"}], "usage": {}, "_stream_aborted": False}
    assert _parse_chat_response(data).tool_calls[0]["function"]["arguments"] == "{}"


def test_the_exact_message_that_bricked_yuankks_session_now_ships_clean():
    """真样本：messages_checkpoint.json 第 46 条的形状。"""
    poisoned = LLMMessage(role="assistant", content=None, tool_calls=[
        {"id": "call_x", "type": "function", "function": {"name": "read_file", "arguments": ""}}])
    d = _msg_to_dict(poisoned)
    assert d["tool_calls"][0]["function"]["arguments"] == "{}"


def test_every_message_in_history_is_wire_safe():
    """核心回归：坏 args 一旦进历史，**每一次**后续请求都带着它 —— 序列化
    收口必须保证任何一条历史消息出去都是可解析的。"""
    history = [
        LLMMessage(role="user", content="go"),
        LLMMessage(role="assistant", content=None, tool_calls=[
            {"id": "c1", "type": "function",
             "function": {"name": "save_artifact", "arguments": _TRUNCATED_ARGS}}]),
        LLMMessage(role="tool", tool_call_id="c1", name="save_artifact",
                   content='{"status":"error"}'),
        LLMMessage(role="user", content="继续"),
    ]
    for m in history:
        d = _msg_to_dict(m)
        for tc in (d.get("tool_calls") or []):
            json.loads(tc["function"]["arguments"])     # 不抛即通过


# ── #223：截断恢复 ─────────────────────────────────────────────────────────


def _state() -> State:
    return State.new(node_type="experiment", base_dir=Path(tempfile.mkdtemp()))


@pytest.fixture(autouse=True)
def _echo_tool():
    async def _echo(state, **kw):
        return {"status": "success", "echo": kw}
    try:
        register_tool(ToolDefinition(
            name="echo_tool", description="t",
            parameters_schema={"type": "object", "properties": {}},
            handler=_echo, node_types=["experiment"]))
    except Exception:
        pass


def _harness() -> NodeHarness:
    return NodeHarness(node_type="experiment", system_prompt="sys",
                       tools=["echo_tool"], max_turns=20)


_LEAKED = ("Do not jump to source-tree fix before case-local options are "
           "exhausted. - plan_reminder: true - turn_count: 8 - last_build: ok "
           "- engineering_control: phase_locked")


class _TruncatingThenRecovers:
    """第一轮复读内部控制状态被截断（无 tool_calls），收到强约束后正常调工具。"""

    def __init__(self, n_truncated=1):
        self.calls = 0
        self.n_truncated = n_truncated
        self.saw_recovery_instruction = False
        self.budgets: list = []          # 每次调用实际用的 max_tokens

    async def chat(self, messages, *, tools=None, **kw):
        self.calls += 1
        # ⚠️ 判据扫**内容**，不扫角色。原来这里限定 `m.role == "system"`，
        # 于是框架把这条注入改成 framework_notice（user 角色 + 归属信封）之后，
        # 检测静默失效、测试照样全绿 —— 与 PR#462 记过的两处是同一类接缝：
        # 以角色为判据的检测，会在角色变更时无声地不再检测任何东西。
        if any("撞到 token 上限" in (m.content or "") for m in messages):
            self.saw_recovery_instruction = True
        self.budgets.append(kw.get("max_tokens"))
        if self.calls <= self.n_truncated:
            return LLMResponse(content=_LEAKED, tool_calls=[],
                               finish_reason="length",
                               usage={"completion_tokens": 16384})
        return LLMResponse(content="收到，改用短回复。", tool_calls=[],
                           finish_reason="stop", usage={"completion_tokens": 12})


def test_truncated_response_does_not_end_node():
    """核心回归：截断 + 无 tool_calls 不再被当成"模型收工"直接结束。"""
    st = _state()
    llm = _TruncatingThenRecovers()
    result = asyncio.run(run_loop(_harness(), st,
                                  [LLMMessage(role="user", content="go")], llm))

    assert llm.calls >= 2, "截断后必须重试，不能一轮就结束"
    assert llm.saw_recovery_instruction, "必须注入强约束（别复读控制文本）"
    # **重试必须带机械增量**：只改提示词 = 同样的输入配同样的上限，必然同样的
    # 结果（E2E-5b 实测三轮 completion_tokens 精确等于 12000、零产出）。
    assert len(llm.budgets) >= 2
    assert llm.budgets[1] > llm.budgets[0], \
        f"重试要抬输出预算，实际 {llm.budgets[0]} → {llm.budgets[1]}"
    assert result.final_text == "收到，改用短回复。"      # 恢复后的正常收尾
    assert _LEAKED not in (result.final_text or "")      # 泄漏内容不进最终文本

    events = [json.loads(x) for x in
              st.transcript_path.read_text(encoding="utf-8").splitlines() if x.strip()]
    assert [e for e in events if e.get("event") == "llm_truncated"]
    assert [e for e in events
            if e.get("event") == "llm_truncation_recovery_injected"]


def test_truncation_retries_are_bounded(monkeypatch):
    """一直截断 → 有界（不能无限烧），用尽后留 exhausted 事件并终止。"""
    monkeypatch.setenv("HARNESS_TRUNCATION_RETRIES", "2")
    st = _state()
    llm = _TruncatingThenRecovers(n_truncated=99)     # 永远截断
    result = asyncio.run(run_loop(_harness(), st,
                                  [LLMMessage(role="user", content="go")], llm))

    assert llm.calls <= 6, f"重试没收敛，调了 {llm.calls} 次"
    assert result.turns >= 1
    events = [json.loads(x) for x in
              st.transcript_path.read_text(encoding="utf-8").splitlines() if x.strip()]
    assert [e for e in events
            if e.get("event") == "llm_truncation_recovery_exhausted"]


def test_truncated_garbage_not_added_to_history():
    """截断的复读垃圾不该进消息历史（否则下一轮继续污染上下文）。"""
    st = _state()
    msgs = [LLMMessage(role="user", content="go")]
    asyncio.run(run_loop(_harness(), st, msgs, _TruncatingThenRecovers()))
    assert not any(_LEAKED in (m.content or "") for m in msgs
                   if m.role == "assistant")


class _TruncatedWithToolCall:
    """截断但**发出了** tool_call —— 这种照常执行工具，不该触发恢复重试。"""

    def __init__(self):
        self.calls = 0

    async def chat(self, messages, *, tools=None, **kw):
        self.calls += 1
        if self.calls == 1:
            return LLMResponse(
                content="部分文本", finish_reason="length",
                tool_calls=[{"id": "c1", "type": "function",
                             "function": {"name": "echo_tool", "arguments": '{"x":1}'}}],
                usage={"completion_tokens": 16384})
        return LLMResponse(content="完成。", tool_calls=[],
                           finish_reason="stop", usage={"completion_tokens": 5})


def test_truncated_but_has_tool_call_proceeds_normally():
    """不误伤：截断了但有 tool_call 说明动作发出去了 —— 照常执行。"""
    st = _state()
    llm = _TruncatedWithToolCall()
    result = asyncio.run(run_loop(_harness(), st,
                                  [LLMMessage(role="user", content="go")], llm))
    assert result.final_text == "完成。"
    events = [json.loads(x) for x in
              st.transcript_path.read_text(encoding="utf-8").splitlines() if x.strip()]
    assert not [e for e in events
                if e.get("event") == "llm_truncation_recovery_injected"]


class _NeverTruncates:
    def __init__(self):
        self.calls = 0

    async def chat(self, messages, *, tools=None, **kw):
        self.calls += 1
        return LLMResponse(content="一次就好。", tool_calls=[],
                           finish_reason="stop", usage={"completion_tokens": 6})


def test_normal_run_unaffected():
    st = _state()
    llm = _NeverTruncates()
    result = asyncio.run(run_loop(_harness(), st,
                                  [LLMMessage(role="user", content="go")], llm))
    assert llm.calls == 1 and result.final_text == "一次就好。"


# ── E2E-5b 回放：预算烧光但零产出 ───────────────────────────────────────────

class _BurnsBudgetProducingNothing:
    """复刻现场：completion_tokens 精确等于上限，content=0，tool_calls=0。

    E2E-5b turn 18/19/20 的真实 usage：
        {"completion_tokens": 12000, "reasoning_tokens": 1540}  content=""
    三轮一模一样 —— 因为每次重试都用同一个 12000 上限，同样的输入必然同样的
    结果。框架注了两次"这轮只做一件事"的提示词，一次也没起作用。
    """

    def __init__(self, *, succeed_above: int):
        self.succeed_above = succeed_above
        self.budgets: list = []

    async def chat(self, messages, *, tools=None, **kw):
        budget = kw.get("max_tokens")
        self.budgets.append(budget)
        if budget is not None and budget > self.succeed_above:
            return LLMResponse(content="终于写完了。", tool_calls=[],
                               finish_reason="stop",
                               usage={"completion_tokens": 900})
        # 预算烧光、什么也没吐出来
        return LLMResponse(content="", tool_calls=[], finish_reason="length",
                           usage={"completion_tokens": budget,
                                  "reasoning_tokens": 1540})


def test_replays_e2e5b_zero_output_truncation():
    """撞顶且零产出时，抬预算才是真增量 —— 光换措辞救不回来。"""
    st = _state()
    # 照抄 writing 节点的真实配置（nodes/writing/harness.yaml: 12000）
    h = _harness()
    h.max_output_tokens = 12000
    llm = _BurnsBudgetProducingNothing(succeed_above=12000)
    result = asyncio.run(run_loop(h, st,
                                  [LLMMessage(role="user", content="写手稿")], llm))

    assert llm.budgets[0] == 12000, f"第一轮该用节点配置的 12000，实际 {llm.budgets[0]}"
    assert llm.budgets[1] > llm.budgets[0], "第一次重试就该抬预算"
    assert result.final_text == "终于写完了。", \
        f"抬了预算就该写得完，实际 budgets={llm.budgets}"

    events = [json.loads(x) for x in
              st.transcript_path.read_text(encoding="utf-8").splitlines() if x.strip()]
    trunc = [e for e in events if e.get("event") == "llm_truncated"]
    assert trunc and trunc[0].get("content_chars") == 0, "要记下这轮零产出"
    assert trunc[0].get("n_tool_calls") == 0
    inj = [e for e in events if e.get("event") == "llm_truncation_recovery_injected"]
    assert inj and inj[0]["budget_after"] > inj[0]["budget_before"], \
        "留痕必须能看出预算抬了多少"


def test_budget_escalation_is_capped():
    """绝不无限抬 —— 天花板挡着。"""
    import core.agent_loop as al

    st = _state()
    llm = _BurnsBudgetProducingNothing(succeed_above=10**9)   # 永远写不完
    h = _harness()
    h.max_output_tokens = 12000
    orig = al._TRUNCATION_BUDGET_CEILING
    al._TRUNCATION_BUDGET_CEILING = 20000
    try:
        asyncio.run(run_loop(h, st, [LLMMessage(role="user", content="x")], llm))
    finally:
        al._TRUNCATION_BUDGET_CEILING = orig
    assert max(b for b in llm.budgets if b) <= 20000, f"超天花板了：{llm.budgets}"

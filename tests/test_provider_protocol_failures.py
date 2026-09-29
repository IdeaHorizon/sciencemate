"""provider tool-call 协议失败：三种签名分类 + args 有界重发 + 熔断（issue #184）。

背景（qinp 2026-07 带证据 zip 的实测）：#106 只认了**一种**协议失败签名
（DSML 残片 + tool_calls 为空），另外两种全部漏网，被误呈成"节点质量/prompt
问题"，把排查方向带偏：

  - `blank_stop`：glm-5.1 reviewer run `1785036378-5104c6` 跑 11 轮后空转，
    finish_reason=stop、tool_calls=[]、content 空，`missing_required_outputs
    =["review_critique"]`。旧分类器因为文本里没有 DSML marker 返回 None。
  - `malformed_tool_args`：一份 7.7KB 的 review_critique，报
    `参数 JSON 解析失败：Expecting ',' delimiter: line 1 column 7711 (char 7710)`。
    旧分类器第一行 `if loop_result.tool_calls: return None` 提前返回，完全
    命不中；模型收到干巴巴的 error 后 turn 14 直接空 stop，不自我纠正。
  - 无熔断：`_PROTOCOL_LEAK_RETRIES=2` 只在单次 call 内重试。
    `HARNESS_DEFAULT_MAX_TURNS=0` 下实测空转 ~40 分钟、~5300 次失败调用，
    conversation.json 涨到 1101 条消息 / 138K token → 项目无法 resume。

全部离线确定性：构造 mock LoopResult / result dict，不联网、不真调 LLM。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from core.agent_loop import LoopResult
from core.bootstrap import bootstrap
from core.executor import (
    _classify_incomplete_failure,
    _classify_incomplete_failure_detail,
    finalize_run,
)
from core.harness import NodeHarness
from core.llm import LLMClient
from core.pause import clear_all
from core.state import State
from core.tool_call_recovery import (
    MALFORMED_ARGS_ERROR_PREFIX,
    PROTOCOL_BREAKER_MARKER,
    MalformedArgsRepair,
    ProtocolFailureBreaker,
    is_malformed_args_error,
    malformed_args_excerpt,
    protocol_failure_signature,
)


@pytest.fixture(autouse=True)
def _setup():
    bootstrap()
    yield
    clear_all()


_DSML_FRAGMENT = (
    "</parameter>\n</｜DSML｜parameter>\n</｜DSML｜invoke>\n</｜DSML｜tool_calls>"
)
# 实测原文（qinp 证据 zip）
_REAL_PARSE_ERROR = "Expecting ',' delimiter: line 1 column 7711 (char 7710)"


def _malformed_record(name: str = "save_artifact") -> dict:
    """agent_loop 在 args 解析失败时记进 all_tool_calls 的那种记录。"""
    return {
        "name": name,
        "args": {},
        "result": {"status": "error",
                   "error": f"{MALFORMED_ARGS_ERROR_PREFIX}{_REAL_PARSE_ERROR}"},
    }


def _ok_record(name: str = "save_artifact") -> dict:
    return {"name": name, "args": {"type": "x"},
            "result": {"status": "ok", "artifact_id": "a1"}}


# ══════════════════════════════════════════════════════════════════════════
# 验收 A：三种签名正确分类 + 正常 run 绝不误分类
# ══════════════════════════════════════════════════════════════════════════

def test_dsml_leak_subcategory():
    """#106 的老签名：主类别必须原样保留（下游 run_node.py 在 == 比较它）。"""
    lr = LoopResult(final_text=_DSML_FRAGMENT, turns=6, tool_calls=[])
    cat, sub = _classify_incomplete_failure_detail(
        lr, missing_required_outputs=["manuscript"])
    assert cat == "provider_tool_call_protocol_error"
    assert sub == "dsml_leak"


def test_surfaced_markup_leak_text_is_dsml_leak():
    """agent_loop 在 leak 重试耗尽后写的兜底文案里**没有** DSML marker ——
    旧分类器因此漏掉了这条已被明确识别过的协议故障。"""
    lr = LoopResult(
        final_text="[provider tool-call 协议错误] 模型本轮尝试调用工具，但当前 LLM "
                   "后端把 tool-call markup 当普通文本返回、未结构化。",
        turns=4, tool_calls=[],
    )
    assert _classify_incomplete_failure_detail(
        lr, missing_required_outputs=["review_critique"]) == (
        "provider_tool_call_protocol_error", "dsml_leak")


def test_blank_stop_classified():
    """glm-5.1 reviewer 实测：11 轮、零工具调用、空 content、必需产出缺失。"""
    lr = LoopResult(final_text="", turns=11, tool_calls=[])
    assert _classify_incomplete_failure_detail(
        lr, missing_required_outputs=["review_critique"]) == (
        "provider_tool_call_protocol_error", "blank_stop")


def test_blank_stop_needs_missing_outputs():
    """没告诉分类器"必需产出缺没缺"时不猜（保持老单参签名的行为不变）。"""
    lr = LoopResult(final_text="", turns=1, tool_calls=[])
    assert _classify_incomplete_failure(lr) is None


def test_near_empty_response_text_is_blank_stop():
    lr = LoopResult(
        final_text="[近乎空响应] 模型本轮几乎没有生成内容（completion_tokens=1）。",
        turns=9, tool_calls=[],
    )
    assert _classify_incomplete_failure_detail(
        lr, missing_required_outputs=["review_critique"])[1] == "blank_stop"


def test_malformed_tool_args_classified():
    """核心回归：tool_calls 非空**不再**提前返回 None —— 否则这条永远命不中。"""
    lr = LoopResult(final_text="", turns=13,
                    tool_calls=[_ok_record("search_kb"), _malformed_record()])
    assert _classify_incomplete_failure_detail(
        lr, missing_required_outputs=["review_critique"]) == (
        "provider_tool_call_protocol_error", "malformed_tool_args")


def test_circuit_break_marker_classified():
    """熔断停机的 run 不能伪装成一次普通的"没产出" incomplete。"""
    lr = LoopResult(
        final_text=f"{PROTOCOL_BREAKER_MARKER} 已连续 5 次同类协议失败。",
        turns=5, tool_calls=[_ok_record()],
    )
    assert _classify_incomplete_failure_detail(
        lr, missing_required_outputs=["manuscript"]) == (
        "provider_tool_call_protocol_error", "circuit_break")


# ── 误分类防线（验收硬要求）────────────────────────────────────────────────

def test_normal_blocked_run_not_misclassified():
    """普通材料不足的 blocked run：有正文、零调用 → 不是协议故障。"""
    lr = LoopResult(
        final_text="缺少必要的 upstream 材料，无法继续，请补充 xxx 数据后重跑。",
        turns=2, tool_calls=[],
    )
    assert _classify_incomplete_failure_detail(
        lr, missing_required_outputs=["manuscript"]) == (None, None)


def test_worked_but_no_output_not_misclassified():
    """"本 run 成功调过工具、只是最后没产出" = 普通 incomplete，绝不能误报。"""
    lr = LoopResult(final_text="我查完了资料但没能写出正式稿。", turns=20,
                    tool_calls=[_ok_record("search_kb"), _ok_record("read_kb")])
    assert _classify_incomplete_failure_detail(
        lr, missing_required_outputs=["manuscript"]) == (None, None)


def test_self_corrected_malformed_args_not_misclassified():
    """中途 args 坏过一次、随后成功干活 = 模型自我纠正了 → 不算协议故障。
    判据是**最后一次**调用是否终结在 args 解析失败上。"""
    lr = LoopResult(final_text="写完了大纲，但正稿没来得及。", turns=8,
                    tool_calls=[_malformed_record(), _ok_record()])
    assert _classify_incomplete_failure_detail(
        lr, missing_required_outputs=["manuscript"]) == (None, None)


def test_tool_business_error_not_misclassified():
    """工具**执行了**、只是自己返回业务 error → 正常失败，不是协议失败。"""
    lr = LoopResult(final_text="", turns=5, tool_calls=[{
        "name": "save_artifact", "args": {"type": "x"},
        "result": {"status": "error", "error": "artifact type 不在白名单内"},
    }])
    assert _classify_incomplete_failure_detail(
        lr, missing_required_outputs=["manuscript"]) == (None, None)


def test_missing_empty_means_qc_failure_not_protocol():
    """产出齐了、只是 QC 挂了 → 节点质量问题，永远不该归到 provider 头上。"""
    lr = LoopResult(final_text="", turns=13, tool_calls=[_malformed_record()])
    assert _classify_incomplete_failure_detail(
        lr, missing_required_outputs=[]) == (None, None)


# ── finalize_run 集成：子类写进 summary.json，主类别保持兼容 ────────────────

def _harness(required: list[str]) -> NodeHarness:
    return NodeHarness(node_type="writing", system_prompt="t", tools=[],
                       max_turns=5, required_outputs=required)


@pytest.mark.asyncio
@pytest.mark.parametrize("lr,expect_sub", [
    (LoopResult(final_text=_DSML_FRAGMENT, turns=6, tool_calls=[]), "dsml_leak"),
    (LoopResult(final_text="", turns=11, tool_calls=[]), "blank_stop"),
    (LoopResult(final_text="", turns=13, tool_calls=[_malformed_record()]),
     "malformed_tool_args"),
])
async def test_finalize_run_writes_subcategory(tmp_path: Path, lr, expect_sub):
    state = State.new(node_type="writing", base_dir=tmp_path)
    summary = await finalize_run(state, _harness(["manuscript"]), lr, LLMClient(
        api_key="k", model="m", base_url="http://x"))
    assert summary["status"] == "incomplete"
    # 主类别不变 —— run_node.py:410 与既有测试都在 == 比它
    assert summary["failure_category"] == "provider_tool_call_protocol_error"
    assert summary["failure_subcategory"] == expect_sub
    # 真落盘（下游读的是 summary.json，不是内存 dict）
    on_disk = json.loads((state.root / "summary.json").read_text(encoding="utf-8"))
    assert on_disk["failure_subcategory"] == expect_sub


@pytest.mark.asyncio
async def test_finalize_run_normal_incomplete_has_no_subcategory(tmp_path: Path):
    state = State.new(node_type="writing", base_dir=tmp_path)
    lr = LoopResult(final_text="材料不足，无法完成写作。", turns=2,
                    tool_calls=[_ok_record("search_kb")])
    summary = await finalize_run(state, _harness(["manuscript"]), lr, LLMClient(
        api_key="k", model="m", base_url="http://x"))
    assert summary["failure_category"] is None
    assert summary["failure_subcategory"] is None


# ══════════════════════════════════════════════════════════════════════════
# 验收 B：malformed args 有界重发（一次成功即完成，超界才判失败）
# ══════════════════════════════════════════════════════════════════════════

def test_is_malformed_args_error_discriminates():
    assert is_malformed_args_error(
        {"status": "error", "error": f"{MALFORMED_ARGS_ERROR_PREFIX}{_REAL_PARSE_ERROR}"})
    assert is_malformed_args_error({"status": "error", "malformed_args": True})
    # 工具业务错误 / 成功结果 / 非 dict 都不是
    assert not is_malformed_args_error({"status": "error", "error": "文件不存在"})
    assert not is_malformed_args_error({"status": "ok"})
    assert not is_malformed_args_error("参数 JSON 解析失败：x")


def test_excerpt_points_at_the_break():
    """7.7KB 单行 JSON 只报 "column 7711" 等于没说 —— 必须把断点附近原文回喂。"""
    raw = '{"body": "' + "x" * 7700 + '" "type": "review_critique"}'
    err = None
    try:
        json.loads(raw)
    except json.JSONDecodeError as e:
        err = e
    assert err is not None
    ex = malformed_args_excerpt(raw, err, window=20)
    assert "⟪HERE⟫" in ex
    assert len(ex) < 100          # 不是把 7.7KB 原样回喂
    assert '"type"' in ex         # 断点附近的真实上下文在里面


def test_bounded_resend_success_clears_counter():
    """一次成功即完成：计数清零，不把历史失败累计到下次。"""
    r = MalformedArgsRepair(max_retries=2)
    first = r.feedback(tool_name="save_artifact", raw_args='{"a": 1', error="boom")
    assert first["retryable"] is True and first["attempt"] == 1
    r.note_success("save_artifact")
    assert not r.exhausted("save_artifact")
    again = r.feedback(tool_name="save_artifact", raw_args='{"a": 1', error="boom")
    assert again["attempt"] == 1, "成功之后应从头计，不继承之前的失败"


def test_bounded_resend_exhausts_and_switches_strategy():
    r = MalformedArgsRepair(max_retries=2)
    outs = [r.feedback(tool_name="save_artifact", raw_args='{"a": 1', error="boom")
            for _ in range(3)]
    assert [o["retryable"] for o in outs] == [True, True, False]
    assert r.exhausted("save_artifact")
    # 超界后不再让模型原样重试，而是要求换策略
    assert "不要再原样重发" in outs[-1]["hint"]
    assert "拆" in outs[-1]["hint"]
    # error 前缀始终保持，executor 的分类才认得出
    assert all(o["error"].startswith(MALFORMED_ARGS_ERROR_PREFIX) for o in outs)


def test_bounded_resend_counts_per_tool():
    """两个不同工具各坏一次 ≠ 同一个工具连坏两次。"""
    r = MalformedArgsRepair(max_retries=1)
    a = r.feedback(tool_name="save_artifact", raw_args="{", error="e")
    b = r.feedback(tool_name="write_kb", raw_args="{", error="e")
    assert a["attempt"] == 1 and b["attempt"] == 1
    assert a["retryable"] and b["retryable"]


# ══════════════════════════════════════════════════════════════════════════
# 验收 D：熔断在阈值处停止（且与 max_turns 无关）
# ══════════════════════════════════════════════════════════════════════════

def test_signature_healthy_turn_is_none():
    assert protocol_failure_signature(has_tool_calls=True) is None
    assert protocol_failure_signature(content="这是正常的最终回答。") is None


def test_signature_kinds():
    assert protocol_failure_signature(
        malformed_args_tools=["save_artifact"]) == "malformed_args:save_artifact"
    assert protocol_failure_signature(
        protocol_leak=True, leak_kind="empty") == "provider_leak:empty"
    assert protocol_failure_signature(
        protocol_leak=True, leak_kind="markup") == "provider_leak:markup"
    assert protocol_failure_signature(content="  ") == "blank_stop"
    # 有 markup leak 但同时恢复出了调用 = provider 还活着，不算失败轮
    assert protocol_failure_signature(protocol_leak=True, has_tool_calls=True) is None


def test_breaker_warns_then_aborts_at_threshold():
    b = ProtocolFailureBreaker(warn_at=3, abort_at=5, abort_any_at=99)
    decisions = [b.record("blank_stop") for _ in range(5)]
    assert [d.should_warn for d in decisions] == [False, False, True, True, False]
    assert [d.should_abort for d in decisions] == [False] * 4 + [True]
    final = decisions[-1]
    assert PROTOCOL_BREAKER_MARKER in final.diagnosis
    assert "provider" in final.diagnosis          # 明确 provider 归因
    assert "max_turns" in final.diagnosis         # 明说与轮数上限无关


def test_breaker_healthy_turn_breaks_streak():
    """中间成功一轮 = provider 还能干活，偶发失败不该攒成熔断。"""
    b = ProtocolFailureBreaker(warn_at=3, abort_at=3, abort_any_at=99)
    b.record("blank_stop")
    b.record("blank_stop")
    assert b.record(None).should_abort is False
    d = b.record("blank_stop")
    assert d.streak == 1 and not d.should_abort


def test_breaker_different_signature_resets_same_kind_streak():
    b = ProtocolFailureBreaker(warn_at=3, abort_at=3, abort_any_at=99)
    b.record("blank_stop")
    b.record("blank_stop")
    d = b.record("provider_leak:markup")
    assert d.streak == 1 and not d.should_abort
    assert d.any_streak == 3, "换了签名，但连续协议失败的总数要继续算"


def test_breaker_any_streak_catches_alternating_signatures():
    """交替签名不能绕过熔断 —— provider 坏了就是坏了。"""
    b = ProtocolFailureBreaker(warn_at=99, abort_at=99, abort_any_at=4)
    sigs = ["blank_stop", "provider_leak:markup", "blank_stop", "provider_leak:empty"]
    decisions = [b.record(s) for s in sigs]
    assert [d.should_abort for d in decisions] == [False, False, False, True]


def test_breaker_is_independent_of_max_turns():
    """HARNESS_DEFAULT_MAX_TURNS=0（无限轮）实测烧了 ~5300 次失败调用。
    熔断只数连续失败，不读任何轮数上限配置 —— 这里断言它在第 5 次就停。"""
    b = ProtocolFailureBreaker(warn_at=3, abort_at=5, abort_any_at=99)
    fired_at = None
    for i in range(1, 1000):
        if b.record("malformed_args:save_artifact").should_abort:
            fired_at = i
            break
    assert fired_at == 5


# ── E2E-4：认出工具名但参数全丢 ─────────────────────────────────────────────

_LT = chr(60)


def _inv(name, params, *, ns=""):
    """拼一段 invoke markup。**不在源码里写字面标签** —— 否则读到这个文件的
    agent 会把它当成真的工具调用（写这条测试时实测踩到）。"""
    o, c = _LT, chr(62)
    body = "".join(f"{o}{ns}parameter name=\"{k}\"{c}{v}{o}/{ns}parameter{c}"
                   for k, v in params.items())
    return f"{o}{ns}invoke name=\"{name}\"{c}{body}{o}/{ns}invoke{c}"


def test_namespaced_invoke_markup_is_parsed():
    """带 XML 命名空间前缀的 invoke/parameter 也要认（E2E-4 实测的后端方言）。"""
    from core.tool_call_recovery import recover_tool_calls

    for ns in ("", "antml:", "tool:"):
        res = recover_tool_calls(
            "先建任务。\n" + _inv("task", {"action": "create", "title": "P1"}, ns=ns),
            [])
        assert len(res.tool_calls) == 1, f"ns={ns!r}"
        fn = res.tool_calls[0]["function"]
        assert fn["name"] == "task"
        import json as _j
        assert _j.loads(fn["arguments"]) == {"action": "create", "title": "P1"}
        assert not res.protocol_leak


def test_unparsed_params_never_dispatch_an_empty_call():
    """认出工具名、参数一个没解析出来 → **不派发**，判 protocol_leak 重试。

    E2E-4 现场：orchestrator 第 2 轮起，所有工具调用都被恢复成
    `{"name": "task", "arguments": "{}"}`，下游报
    `_task() missing 1 required positional argument: 'action'`。模型据此判定
    "框架的 XML 解析坏了 —— 这不是我能从 agent 侧修复的问题"，整个 run 在
    literature 都没跑起来时就 blocked。**派发空参数调用比不恢复更糟。**
    """
    from core.tool_call_recovery import recover_tool_calls

    o, c = _LT, chr(62)
    # body 非空（有内容），但参数写成了框架不认的形状
    weird = (f"{o}invoke name=\"task\"{c}"
             f"{o}args{c}" + '{"action": "create"}' + f"{o}/args{c}"
             f"{o}/invoke{c}")
    res = recover_tool_calls("现在从 Phase 1 开始。\n" + weird, [])
    assert res.tool_calls == [], "宁可不恢复，也不能派发空参数调用"
    assert res.protocol_leak is True, "要判协议失败才会触发重试"
    assert res.unparsed_tool == "task"
    assert "args" in (res.unparsed_body_preview or ""), "要留原文供排查方言"


def test_genuinely_argumentless_call_still_works():
    """body 为空才是真的无参调用（kb_overview 这类），不能被误判。"""
    from core.tool_call_recovery import recover_tool_calls

    o, c = _LT, chr(62)
    res = recover_tool_calls(
        "看一眼 KB。\n" + f"{o}invoke name=\"kb_overview\"{c}{o}/invoke{c}", [])
    assert len(res.tool_calls) == 1
    assert res.tool_calls[0]["function"]["name"] == "kb_overview"
    assert res.protocol_leak is False


# ── E2E-4 二次实测：不闭合的 DSML 变体 + 自增计数击穿 hash ──────────────────

def _dsml(tag, attrs=""):
    """拼一个 DSML 开标签。不写字面标签 —— 会被读到此文件的 agent 当真调用。"""
    return chr(60) + "｜DSML｜" + tag + attrs + chr(62)


def test_unclosed_dsml_with_type_attrs_is_recovered():
    """真实后端方言：全不闭合 + string 类型属性。照抄 E2E-4 chat.log 的形态。

    旧 regex 要求闭合标签 → 一个都匹不上 → 内容当叙述文本放行 → 模型看到调用
    没执行，自增计数重试 23 轮。而它想发的 node_inputs 是完全正确的（含
    reused_inputs 声明指引）—— 生产路线的决策对了，死在传输层。
    """
    import json as _j

    from core.tool_call_recovery import recover_tool_calls

    content = (
        "现在立即正确调起 data 节点。\n"
        + _dsml('invoke name="run_node"') + "\n"
        + _dsml('parameter name="node_type" string="true"') + "data\n"
        + _dsml('parameter name="node_inputs" string="false"')
        + _j.dumps({"research_question": "Collect tau-bench trajectories",
                    "context": "declare metadata.reused_inputs"},
                   ensure_ascii=False)
    )
    res = recover_tool_calls(content, [])
    assert len(res.tool_calls) == 1
    fn = res.tool_calls[0]["function"]
    assert fn["name"] == "run_node"
    args = _j.loads(fn["arguments"])
    assert args["node_type"] == "data"
    # string="false" = 方言自述的 JSON：必须解析成 dict，不是塞一个 JSON 字符串
    assert isinstance(args["node_inputs"], dict)
    assert args["node_inputs"]["research_question"] == "Collect tau-bench trajectories"
    # markup 必须被清洗掉，叙述保留
    assert "DSML" not in (res.content or "")


def test_unclosed_json_param_tolerates_trailing_prose():
    """不闭合形态下最后一个参数的值一路延伸到文本末尾 —— raw_decode 吃掉前缀
    合法 JSON，尾随叙述不搅坏参数。"""
    import json as _j

    from core.tool_call_recovery import recover_tool_calls

    content = (
        _dsml('invoke name="task"') + "\n"
        + _dsml('parameter name="payload" string="false"')
        + '{"action": "create"}\n\n以上是本轮计划。'
    )
    res = recover_tool_calls(content, [])
    args = _j.loads(res.tool_calls[0]["function"]["arguments"])
    assert args["payload"] == {"action": "create"}


def test_well_formed_closed_markup_still_works():
    """规范闭合形态（既有后端）不能被扫描重写破坏。"""
    import json as _j

    from core.tool_call_recovery import recover_tool_calls

    o, c = chr(60), chr(62)
    content = (
        o + 'invoke name="search_kb"' + c
        + o + 'parameter name="query"' + c + "verifiability"
        + o + "/parameter" + c + o + "/invoke" + c
    )
    res = recover_tool_calls(content, [])
    args = _j.loads(res.tool_calls[0]["function"]["arguments"])
    assert args == {"query": "verifiability"}


def test_two_unclosed_invokes_split_correctly():
    """相邻两个不闭合 invoke：前一个的 body 止于后一个的开标签。"""
    import json as _j

    from core.tool_call_recovery import recover_tool_calls

    content = (
        _dsml('invoke name="kb_overview"') + "\n"
        + _dsml('invoke name="list_artifacts"') + "\n"
    )
    res = recover_tool_calls(content, [])
    names = [t["function"]["name"] for t in res.tool_calls]
    assert names == ["kb_overview", "list_artifacts"]
    for t in res.tool_calls:
        assert _j.loads(t["function"]["arguments"]) == {}

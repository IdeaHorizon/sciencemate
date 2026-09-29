"""工作集：工具结果在 context 里至多一份活副本，原文全保真落盘。

RFC_CONTEXT_AS_A_RENDERED_VIEW 的承重墙测试。判据全部围绕一条不变量：

    任意一个 (tool, canonical_args)，在 context 里**至多一份活副本**。

以及它带来的三条推论：重读免费且完整 / 重读不膨胀 context / 循环判据变精确。
"""
from __future__ import annotations

import json
import pathlib

import pytest

import shared.tools  # noqa: F401
from core import context_view as cv
from core.llm import LLMMessage


class _FakeState:
    """替身的属性名必须与真 State 一致 —— 否则它遮住的正是要测的东西。

    第一版这里写的是 `run_dir` / `base_dir`，真 State 两个都没有。实现照着替身
    写，于是 run log 一次都没落过盘，而这一整片测试全绿。
    `test_the_run_log_lands_with_a_real_state` 是那道护栏。
    """

    def __init__(self, tmp_path):
        self.hook_state: dict = {}
        self.root = tmp_path


def _tool_msg(tcid: str, content: str) -> LLMMessage:
    return LLMMessage(role="tool", tool_call_id=tcid, name="read_file", content=content)


# ── 不变量：至多一份活副本 ──────────────────────────────────────────────

def test_a_second_read_of_the_same_key_tombstones_the_first(tmp_path):
    """读第二次，第一份**立刻**变墓碑 —— 不等预算不够。

    这是"重读不膨胀 context"的机械保证：读一百次和读一次占一样的空间，
    所以根本不需要靠"惩罚重复"来控体积。
    """
    s = _FakeState(tmp_path)
    view = cv.get(s)
    msgs = [_tool_msg("c1", "X" * 5000), _tool_msg("c2", "X" * 5000)]
    view.note_result(tool_name="read_file", args={"p": "a"}, tool_call_id="c1",
                     content=msgs[0].content, turn=1, replayable=True)
    view.note_result(tool_name="read_file", args={"p": "a"}, tool_call_id="c2",
                     content=msgs[1].content, turn=2, replayable=True)
    msgs = view.apply(msgs)

    assert msgs[0].content.startswith(cv.TOMBSTONE_PREFIX), "第一份没变墓碑"
    assert msgs[1].content == "X" * 5000, "最新那份不该被动"
    assert view.stats()["live_entries"] == 1


def test_different_args_are_independent_copies(tmp_path):
    s = _FakeState(tmp_path)
    view = cv.get(s)
    msgs = [_tool_msg("c1", "A"), _tool_msg("c2", "B")]
    view.note_result(tool_name="read_file", args={"p": "a"}, tool_call_id="c1",
                     content="A", turn=1, replayable=True)
    view.note_result(tool_name="read_file", args={"p": "b"}, tool_call_id="c2",
                     content="B", turn=2, replayable=True)
    msgs = view.apply(msgs)
    assert msgs[0].content == "A" and msgs[1].content == "B"


def test_rendering_never_changes_message_count_or_pairing(tmp_path):
    """渲染只动 content。条数和 tool_call_id 是 API 硬约束，不许碰。

    这也是为什么驱逐用墓碑而不是删消息：删一条就得改上一条 assistant 的
    tool_calls，那才是真的在编辑历史。
    """
    s = _FakeState(tmp_path)
    view = cv.get(s)
    msgs = [_tool_msg("c1", "A"), _tool_msg("c2", "B")]
    ids_before = [m.tool_call_id for m in msgs]
    for i, (tcid, c) in enumerate((("c1", "A"), ("c2", "B")), start=1):
        view.note_result(tool_name="read_file", args={"p": "same"}, tool_call_id=tcid,
                         content=c, turn=i, replayable=True)
    msgs = view.apply(msgs)
    assert len(msgs) == 2
    assert [m.tool_call_id for m in msgs] == ids_before
    assert all(m.role == "tool" for m in msgs)


# ── 推论：重读免费且完整 ────────────────────────────────────────────────

def test_the_original_survives_eviction_on_disk(tmp_path):
    """驱逐不丢信息：原文在 run log 里，能原样取回。"""
    s = _FakeState(tmp_path)
    view = cv.get(s)
    body = "原文" * 3000
    msgs = [_tool_msg("c1", body)]
    view.note_result(tool_name="read_file", args={"p": "a"}, tool_call_id="c1",
                     content=body, turn=1, replayable=True)
    view.enforce_budget(msgs, max_bytes=10)
    msgs = view.apply(msgs)
    assert msgs[0].content.startswith(cv.TOMBSTONE_PREFIX)
    assert view.recover("read_file", {"p": "a"}) == body, "原文没能从 run log 取回"


def test_the_tombstone_says_how_to_get_it_back(tmp_path):
    s = _FakeState(tmp_path)
    view = cv.get(s)
    msgs = [_tool_msg("c1", "X" * 100)]
    view.note_result(tool_name="read_file", args={"p": "a"}, tool_call_id="c1",
                     content="X" * 100, turn=1, replayable=True)
    view.enforce_budget(msgs, max_bytes=1)
    msgs = view.apply(msgs)
    body = msgs[0].content
    assert "没有丢" in body, "没说清楚原文还在"
    assert "不算重复" in body, "没说清楚重新取回是合法的（这正是旧实现的谎）"


def test_a_side_effecting_tool_is_never_told_to_replay_itself(tmp_path):
    """⚠️ 正确性，不是文案偏好。

    "用同样参数再调一次就能取回"对 read_file 是真话，对 run_bash / save_artifact
    是**危险的错误建议** —— 照做会真的再执行一次。
    """
    s = _FakeState(tmp_path)
    view = cv.get(s)
    # 走"同 key 新副本挤旧副本"这条路 —— 有副作用的工具不会被**预算**驱逐
    # （fail-closed），但连着跑两次同样的命令，旧那份照样得让位。
    msgs = [LLMMessage(role="tool", tool_call_id="c1", name="run_bash", content="Y" * 100),
            LLMMessage(role="tool", tool_call_id="c2", name="run_bash", content="Z" * 100)]
    for i, tcid in enumerate(("c1", "c2"), start=1):
        view.note_result(tool_name="run_bash", args={"cmd": "make all"}, tool_call_id=tcid,
                         content=("Y" if i == 1 else "Z") * 100, turn=i, replayable=False)
    msgs = view.apply(msgs)
    body = msgs[0].content
    assert body.startswith(cv.TOMBSTONE_PREFIX), "旧副本没让位"
    assert "有副作用" in body and "不要为了看结果而重跑" in body
    assert "不算重复调用" not in body, "对有副作用的工具建议了重放"


def test_a_failed_result_is_never_promised_a_free_recovery(tmp_path):
    """失败结果的墓碑**不许**说"重新取回不算重复"。

    那句话对成功副本是真的（恢复路径 lookup 放行且不计数），对失败是假的：
    失败没有可复用的副本，重调必然重新执行、必然重新计数，多来几次就熔断。
    **骗它去撞墙，比不说话糟得多。**

    实测抓到过：连读同一个不存在的文件，前几条全变成"重新取回不算重复"的
    墓碑 —— 而每一次重调其实都在把它推近熔断。
    """
    s = _FakeState(tmp_path)
    view = cv.get(s)
    msgs = [_tool_msg("c1", "E1"), _tool_msg("c2", "E2")]
    for i, tcid in enumerate(("c1", "c2"), start=1):
        view.note_result(tool_name="read_file", args={"p": "missing"}, tool_call_id=tcid,
                         content=f"E{i}", turn=i, replayable=True, ok=False)
    msgs = view.apply(msgs)
    tomb = msgs[0].content
    assert tomb.startswith(cv.TOMBSTONE_PREFIX)
    assert "不算重复" not in tomb, "对失败结果许诺了免费恢复"
    assert "会被计入重复" in tomb, "没告诉它重调是要计数的"


def test_eviction_eligibility_is_retrievability(tmp_path):
    """驱逐资格 = 可取回性（issue #710 重推），不再是可重放性。

    步 0 之后每条结果的原文都落 run log —— 驱逐是无损的（墓碑指原文），与它
    可不可重放、成没成功无关。旧豁免让 run_node 的 blocked 结果（单条 25k 字符、
    每次重试 +1 条）享受无限累积权，直到网关 400 杀 run。

    驱逐顺序仍保旧不变量的精神：可重放的先走，落盘的失败结果最后走
    （失败正文是自纠依据，LRU 保住最近一条）。
    """
    s = _FakeState(tmp_path)
    view = cv.get(s)
    msgs = [LLMMessage(role="tool", tool_call_id="c1", name="run_bash", content="B" * 500),
            _tool_msg("c2", "R" * 500),
            LLMMessage(role="tool", tool_call_id="c3", name="run_bash", content="F" * 500)]
    view.note_result(tool_name="run_bash", args={"cmd": "x"}, tool_call_id="c1",
                     content="B" * 500, turn=1, replayable=False)
    view.note_result(tool_name="read_file", args={"p": "a"}, tool_call_id="c2",
                     content="R" * 500, turn=2, replayable=True)
    view.note_result(tool_name="run_bash", args={"cmd": "y"}, tool_call_id="c3",
                     content="F" * 500, turn=3, replayable=False, ok=False)
    # 预算装得下一条：可重放的最先走，然后是落盘的成功结果；失败的最后。
    view.enforce_budget(msgs, max_bytes=520)
    msgs = view.apply(msgs)
    assert msgs[1].content.startswith(cv.TOMBSTONE_PREFIX), "可重调的该最先被驱逐"
    assert msgs[0].content.startswith(cv.TOMBSTONE_PREFIX), \
        "落了盘的非 replayable 结果现在是可驱逐的 —— 原文在 run log 里没有丢"
    assert not msgs[2].content.startswith(cv.TOMBSTONE_PREFIX), \
        "落盘的失败结果最后才走：预算够留一条时留的该是它"
    # 墓碑必须如实指出原文在哪，且仍然不许建议重放有副作用的工具。
    assert "tool_results.jsonl" in msgs[0].content
    assert "不要为了看结果而重跑" in msgs[0].content


def test_a_result_nowhere_retrievable_is_never_evicted(tmp_path):
    """哪儿都取不回的结果（没落盘、不可重放、无 compactor）仍然 fail-closed。

    这是旧豁免里**唯一站得住**的那部分：驱逐它是真丢。构造法 = state 没有
    run 根（log 落不了盘）+ 认领来路不明的历史消息。
    """
    s = _FakeState(tmp_path)
    view = cv.get(s)
    view._log_path = None
    view.state = type("S", (), {"root": None, "transcript_path": None,
                                "hook_state": {}})()
    orphan = LLMMessage(role="tool", tool_call_id="cx", name="mystery_tool",
                        content="X" * 5000)
    msgs = [orphan]
    view.adopt(msgs, replayable_of=lambda name: False)
    assert view.entries["cx"].get("logged") is False
    view.enforce_budget(msgs, max_bytes=1)
    msgs = view.apply(msgs)
    assert not msgs[0].content.startswith(cv.TOMBSTONE_PREFIX), \
        "驱逐了一个真的取不回来的结果"


# ── 推论：循环判据变精确 ────────────────────────────────────────────────

def test_a_live_copy_means_it_is_really_looping(tmp_path):
    s = _FakeState(tmp_path)
    view = cv.get(s)
    view.note_result(tool_name="read_file", args={"p": "a"}, tool_call_id="c1",
                     content="A", turn=1, replayable=True)
    assert view.is_live("read_file", {"p": "a"}) is True
    assert view.is_live("read_file", {"p": "other"}) is False


def test_a_recovered_read_is_not_a_repeat(tmp_path):
    """副本被驱逐之后再调 = 框架自己规定的恢复路径，不是病。

    旧实现把它算作重复，于是 summarizer 的"重调即可取回"和 tool_call_cache 的
    "重调是循环"正面打架 —— 模型照框架说的做，反被惩罚到停机。
    """
    s = _FakeState(tmp_path)
    view = cv.get(s)
    msgs = [_tool_msg("c1", "X" * 500)]
    view.note_result(tool_name="read_file", args={"p": "a"}, tool_call_id="c1",
                     content="X" * 500, turn=1, replayable=True)
    view.enforce_budget(msgs, max_bytes=1)
    msgs = view.apply(msgs)
    assert view.is_live("read_file", {"p": "a"}) is False, \
        "被驱逐之后仍被判成'答案就在眼前'"


# ── 真跑 agent_loop：两条路都要对 ──────────────────────────────────────

def _fake_llm(tool_name, args, n_calls):
    from core.llm import LLMResponse

    class _LLM:
        def __init__(self): self.n = 0
        async def chat(self, messages, **kw):
            self.n += 1
            if self.n > n_calls:
                return LLMResponse(content="done", tool_calls=[],
                                   finish_reason="stop", usage={})
            return LLMResponse(
                content=f"第 {self.n} 次",
                tool_calls=[{"id": f"c{self.n}", "type": "function",
                             "function": {"name": tool_name,
                                          "arguments": json.dumps(args)}}],
                finish_reason="tool_calls", usage={})
    return _LLM()


@pytest.mark.asyncio
async def test_repeated_big_reads_do_not_grow_the_context(tmp_path, monkeypatch):
    """连读同一个大文件 8 次，context 里始终只有一份完整副本。"""
    from core import tool_registry
    from core.agent_loop import run_loop
    from core.harness import NodeHarness, SummarizerConfig
    from core.state import State

    async def _big(*, state, **kwargs):
        return {"status": "success", "content": "大" * 4000}

    monkeypatch.setitem(tool_registry._REGISTRY.executors, "read_file", _big)
    monkeypatch.setitem(tool_registry._REGISTRY.tools, "read_file",
                        tool_registry.ToolDefinition(
                            name="read_file", description="d",
                            parameters_schema={"type": "object", "properties": {}},
                            replayable_read=True))

    harness = NodeHarness(node_type="literature", max_turns=30, tools=["read_file"],
                          summarizer=SummarizerConfig(enabled=False))
    state = State.new(node_type="literature", base_dir=tmp_path, project_id="p_big")
    result = await run_loop(harness, state, [], _fake_llm("read_file", {"p": "a"}, 8))

    bodies = [m.content or "" for m in result.messages if m.role == "tool"]
    full = [c for c in bodies if c.count("大") > 1000]
    assert len(bodies) == 8, f"应有 8 条 tool 消息，实际 {len(bodies)}"
    assert len(full) == 1, f"应只剩一份完整副本，实际 {len(full)} 份"

    # 不变量的直接表达：读 8 次的 context 占用 ≈ 读 1 次。
    # （这里其余 7 条是"结果已在上文"的短指针 —— 预算够时不会触发驱逐，
    #   所以不该断言它们是墓碑：那是预算不够时才走的另一条路。）
    total = sum(len(c) for c in bodies)
    assert total < len(full[0]) * 2, (
        f"context 随重读膨胀了：8 次共 {total} 字符，单份 {len(full[0])} 字符")


@pytest.mark.asyncio
async def test_a_failing_loop_still_breaks(tmp_path, monkeypatch):
    """失败结果一直是 live（体积小、不会被驱逐）→ 重复判据照常成立 → 照常熔断。

    这是新判据最容易引入的回归：把"重复"收窄到 live 之后，2026-08-21 那种
    连撞 64 次"找不到文件"的循环**必须仍然停得下来**。
    """
    from core import tool_call_cache as tc
    from core import tool_registry
    from core.agent_loop import run_loop
    from core.harness import NodeHarness, SummarizerConfig
    from core.state import State

    async def _missing(*, state, **kwargs):
        return {"status": "error", "error": "找不到文件：/wt/x.md"}

    monkeypatch.setitem(tool_registry._REGISTRY.executors, "read_file", _missing)
    monkeypatch.setitem(tool_registry._REGISTRY.tools, "read_file",
                        tool_registry.ToolDefinition(
                            name="read_file", description="d",
                            parameters_schema={"type": "object", "properties": {}},
                            replayable_read=True))

    harness = NodeHarness(node_type="literature", max_turns=200, tools=["read_file"],
                          summarizer=SummarizerConfig(enabled=False))
    state = State.new(node_type="literature", base_dir=tmp_path, project_id="p_fail")
    llm = _fake_llm("read_file", {"p": "/wt/x.md"}, 100)
    result = await run_loop(harness, state, [], llm)

    assert result.status == "failed", f"失败循环没有熔断，实际 {result.status}"
    assert llm.n <= tc.BREAK_REPEAT + 2, f"跑了 {llm.n} 轮才停"
    assert len(state.hook_state.get("blockers", [])) == 1


@pytest.mark.asyncio
async def test_evicted_then_reread_comes_back_whole_and_is_not_punished(tmp_path, monkeypatch):
    """**RFC 的核心承诺，也是旧架构做不到的那件事。**

    副本被驱逐 → 模型按墓碑指示重新调用 → 完整取回，工具真的执行了，且这次
    重读**不计入重复**、不会把它推向熔断。

    旧架构下这条路是死的：summarizer 清除时写着"用同样参数重调即可取回"，
    而 tool_call_cache 把重调计成重复，第 3 次起返回 400 字截断版、第 8 次报错、
    第 12 次登记 blocker 停机。模型完全照框架说的做，被框架一路惩罚到停机。
    """
    from core import agent_loop as al
    from core import tool_call_cache as tc
    from core import tool_registry
    from core.agent_loop import run_loop
    from core.harness import NodeHarness, SummarizerConfig
    from core.state import State

    # 预算压到"装不下一份结果"：把份额上限压到地板（推导预算取
    # min(现算, 上限) 再 max(4096 字节地板)），4096 字符远小于一份结果
    # （约 12k 字符）。预算 owner 现在是 summarizer.derived_tool_budget_bytes。
    from core import summarizer as _sm
    monkeypatch.setattr(_sm, "WORKING_SET_SHARE_CAP", 0.001)

    executions = []

    async def _big(*, state, **kwargs):
        executions.append(1)
        return {"status": "success", "content": "原文" * 2000}

    monkeypatch.setitem(tool_registry._REGISTRY.executors, "read_file", _big)
    monkeypatch.setitem(tool_registry._REGISTRY.tools, "read_file",
                        tool_registry.ToolDefinition(
                            name="read_file", description="d",
                            parameters_schema={"type": "object", "properties": {}},
                            replayable_read=True))

    harness = NodeHarness(node_type="literature", max_turns=40, tools=["read_file"],
                          summarizer=SummarizerConfig(enabled=False))
    state = State.new(node_type="literature", base_dir=tmp_path, project_id="p_rec")
    n_reads = tc.BREAK_REPEAT + 6            # 远超旧架构的停机线
    llm = _fake_llm("read_file", {"p": "a"}, n_reads)
    result = await run_loop(harness, state, [], llm)

    # 1. 没被熔断 —— 恢复不是病
    assert result.status != "failed", (
        f"按墓碑指示重新取回，却被判成循环并停机（{result.final_text[:200]}）")
    assert "blockers" not in state.hook_state

    # 2. 被驱逐后的重读**真的执行了**（不是拿缓存糊弄）。
    #    最近一轮保护（issue #710）让"上一轮刚返回的副本"保持 live —— 对着
    #    live 副本的重读拿指针不重新执行，这是恢复语义的另一半，不是糊弄。
    #    于是执行次数是"驱逐了几次就执行几次"：约每两轮一次，且必须 > 1
    #    （一次都不执行 = 旧的截断/惩罚路径回来了）。
    assert len(executions) >= n_reads // 2, (
        f"驱逐后重读没有真执行：{len(executions)}/{n_reads}")
    assert len(executions) < n_reads, (
        "live 副本的重读也重新执行了 —— 最近一轮保护没有生效")

    # 3. 每次真执行交给模型的都是**完整**原文，不是截断版。
    #    判据落在 run log 上：它记的就是当时放进 tool 消息的那串字符，而且是
    #    权威（context 里的副本随时可能被驱逐成墓碑 —— 本例预算极小，连最后
    #    一份也会被驱逐，所以在 messages 上找完整原文是找不到的）。
    import json as _json
    log = state.root / "tool_results.jsonl"
    assert log.exists(), "run log 没落盘"
    recs = [_json.loads(l) for l in log.read_text(encoding="utf-8").splitlines()]
    assert len(recs) == len(executions), f"run log 少记了：{len(recs)}/{len(executions)}"
    for r in recs:
        assert r["content"].count("原文") >= 2000, "交给模型的不是完整原文"
        assert "截断" not in r["content"], "内容被截断了"

    # 4. context 仍然没膨胀：驱逐后留的是墓碑，不是副本
    bodies = [m.content or "" for m in result.messages if m.role == "tool"]
    total = sum(len(c) for c in bodies)
    one = len(recs[0]["content"])
    assert total < one * 3, f"context 随重读膨胀：{total} 字符（单份 {one}）"
    assert sum(1 for c in bodies if c.startswith(cv.TOMBSTONE_PREFIX)) >= len(executions) - 1, \
        "驱逐掉的没有留下墓碑"


def test_the_run_log_lands_with_a_real_state(tmp_path):
    """用**真 State** 验证落盘 —— 替身遮蔽的护栏。

    工作集的全部正确性都压在"原文确实落到了磁盘"上：驱逐之所以不丢信息，
    墓碑之所以敢说"原文没有丢"，全靠这一条。而它恰恰是最容易被替身遮住的 ——
    实现读了两个 State 根本没有的属性，`getattr(..., None)` 一路静默，
    11 条单元测试全绿。
    """
    from core.state import State

    state = State.new(node_type="literature", base_dir=tmp_path, project_id="p_log")
    view = cv.get(state)
    view.note_result(tool_name="read_file", args={"p": "a"}, tool_call_id="c1",
                     content="真原文" * 500, turn=1, replayable=True)

    logs = list(pathlib.Path(state.root).glob("tool_results.jsonl"))
    assert logs, f"run log 没落盘（state.root={state.root}）"
    assert view.recover("read_file", {"p": "a"}) == "真原文" * 500


# ── 与现场对账：脱节的代价是死循环，不是少省点空间 ──────────────────────────

def test_a_copy_removed_by_someone_else_stops_counting_as_live(tmp_path):
    """别人把那条消息拿走了，工作集必须**现算**出它已经不在场。

    脱节的后果不是少省点空间，是**死循环**：`is_live` 说"完整答案正摆在眼前"，
    于是 lookup 回一个"往上翻"的指针，而那条消息已经被摘要吃掉 —— 模型翻不到，
    只能再问，再拿到同一个指针。框架把它锁死在一个它无法满足的指令上。
    """
    s = _FakeState(tmp_path)
    view = cv.get(s)
    msgs = [_tool_msg("c1", "A" * 500)]
    view.note_result(tool_name="read_file", args={"p": "a"}, tool_call_id="c1",
                     content="A" * 500, turn=1, replayable=True)
    assert view.is_live("read_file", {"p": "a"}) is True

    # 模拟 summarizer 把中间段整体换成一条摘要：那条 tool 消息没了
    view.sync([LLMMessage(role="system", content="……摘要……")])
    assert view.is_live("read_file", {"p": "a"}) is False, \
        "消息已经不在场，工作集还认为「答案就在眼前」"


def test_sync_does_not_evict_what_is_still_there(tmp_path):
    """对账只销不在场的，不许误伤还在的（否则每轮都白白丢副本）。"""
    s = _FakeState(tmp_path)
    view = cv.get(s)
    msgs = [_tool_msg("c1", "A" * 500)]
    view.note_result(tool_name="read_file", args={"p": "a"}, tool_call_id="c1",
                     content="A" * 500, turn=1, replayable=True)
    assert view.sync(msgs) == 0
    assert view.is_live("read_file", {"p": "a"}) is True


@pytest.mark.asyncio
async def test_compression_that_drops_tool_messages_does_not_strand_the_pointer(
        tmp_path, monkeypatch):
    """端到端：压缩把 tool 消息换掉之后，模型再问同一个 key 必须真执行。

    这是"指针指向墓碑"那条死循环的护栏 —— 它跨了两个模块（summarizer 换消息、
    工作集记 live），任何一边单独看都是对的。
    """
    from core import agent_loop as al
    from core import summarizer as _sm
    from core import tool_registry
    from core.agent_loop import run_loop
    from core.harness import NodeHarness, SummarizerConfig
    from core.state import State

    executions = []

    async def _read(*, state, **kwargs):
        executions.append(1)
        return {"status": "success", "content": "内容" * 50}

    monkeypatch.setitem(tool_registry._REGISTRY.executors, "read_file", _read)
    monkeypatch.setitem(tool_registry._REGISTRY.tools, "read_file",
                        tool_registry.ToolDefinition(
                            name="read_file", description="d",
                            parameters_schema={"type": "object", "properties": {}},
                            replayable_read=True))

    # 每轮都"压缩"：把所有 tool 消息扔掉，换成一条摘要 —— summarizer 的极端形态
    async def _nuking_strategy(ctx):
        return [m for m in ctx.messages if getattr(m, "role", None) != "tool"] + [
            LLMMessage(role="system", content="……历史摘要……")]

    monkeypatch.setattr(_sm, "should_compress",
                        lambda h, m, t, state=None: (True, 1))
    monkeypatch.setattr(_sm, "run_summarizer",
                        lambda h, st, m, llm, t, est: _nuking_strategy(
                            type("C", (), {"messages": m})()))

    harness = NodeHarness(node_type="literature", max_turns=12, tools=["read_file"],
                          summarizer=SummarizerConfig(enabled=True))
    state = State.new(node_type="literature", base_dir=tmp_path, project_id="p_strand")
    llm = _fake_llm("read_file", {"p": "a"}, 6)
    result = await run_loop(harness, state, [], llm)

    assert result.status != "failed", f"被误判成循环并停机：{result.final_text[:200]}"
    assert len(executions) == 6, (
        f"压缩吃掉副本后，模型再问却没真执行（{len(executions)}/6）—— "
        f"它拿到的是一个指向已消失内容的指针")

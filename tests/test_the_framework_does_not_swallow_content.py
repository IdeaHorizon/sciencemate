"""框架不许吞内容、不许伪造、不许只见证不判决。

## 事故来源

2026-08-21 本机 E2E，run `run_c5359f57f8d74ff59bb70c9881553034::_orchestrator->_curator@d1`：
curator 去读一个它自己编出来的
`.research/orchestration/artifacts/curator_dreaming_report.md`（当时那个目录是**空的**），
**同一个 key 从第 30 次一路数到第 64 次**，run 最后还是 `status: completed`。

从执行事件里逐条取证，四个独立缺陷，全部在框架侧：

  1. 第 3～29 次重复，缓存层把 `{"status":"error"}` 包进自制的 `{"status":"success"}`
     信封返回 —— 这一条 run **105 次**。模型问"成了吗"，平台答"成了"。
  2. 失败结果进了结果缓存，于是 `findings_digest` 把 `找不到文件：…` 以
     "本 run 已确立的查询结论"的名义注回压缩后的上下文。
  3. 第 30 次起返回 error directive，然后 31、32 … 64 —— 熔断只是一条日志。
     **这一条已由 `fed96b41`（HARD 30→8、新增 BREAK=12 真停机）修复**，本文件
     不再重复实现，只守住"失败循环也必须数得到、熔断得了"这条边界 —— 因为
     v3.4 让失败不再进结果缓存，若计数还挂在缓存条目上，那 64 次一次都数不到。
  4. `read_file` 的报错在最需要信息时退化成零信息：空目录静默跳过、找不到同名
     静默跳过、读到目录只回一句"不是文件"（那一条 run 撞了 18 次，每次一整轮）。

这些测试是这四条的回归判据。每一条都能通过**变异对应实现**变红。
"""
from __future__ import annotations

import ast
import pathlib

import pytest

import shared.tools  # noqa: F401  # 注册真实工具：判据来自注册表声明
from core import tool_call_cache as tc


class _FakeState:
    def __init__(self) -> None:
        self.hook_state: dict = {}


_MISSING = {"status": "error", "error": "找不到文件：/wt/.research/orchestration/artifacts/x.md"}


# ── ① 平台不许对模型撒谎 ────────────────────────────────────────────────────

def test_a_cached_failure_is_never_reported_as_success():
    """事故原文：105 次 `{"cached": true, "result": {"status": "error"…}}`，外层
    却盖着 `"status": "success"`。status 是**产生结果那一层**盖的章，缓存层无权改判。
    """
    s = _FakeState()
    for i in range(tc.HARD_REPEAT - 2):
        out = tc.store(s, "read_file", {"path": "/wt/x.md"}, dict(_MISSING), turn=i)
        assert out.get("status") == "error", f"第 {i+1} 次：失败被改判成了 {out.get('status')!r}"
        hit = tc.lookup(s, "read_file", {"path": "/wt/x.md"})
        if hit is not None:
            assert hit.get("status") != "success", "缓存层把失败说成了成功"


def test_a_failure_is_re_executed_not_answered_from_cache():
    """失败不进结果缓存 —— 本层无权替一个失败的调用作答。

    `replayable_read` 的语义是"重调能取回同样结果"，一句"找不到文件"字面满足它，
    但它不是**结论**，是"这条路走不通"。两者性质不同，不能共用一个格子。
    """
    s = _FakeState()
    tc.store(s, "read_file", {"path": "/wt/x.md"}, dict(_MISSING), turn=1)
    assert tc.lookup(s, "read_file", {"path": "/wt/x.md"}) is None, \
        "失败结果被当成可复用的结论缓存了"


def test_a_failure_never_enters_the_findings_digest():
    """`找不到文件` 不许以"已确立的结论"的名义被注回压缩后的上下文。"""
    s = _FakeState()
    tc.store(s, "read_file", {"path": "/wt/x.md"}, dict(_MISSING), turn=1)
    assert tc.findings_digest(s) is None


def test_the_digest_does_not_call_a_truncated_summary_a_conclusion():
    """台账每行都是**截断后的摘要**，不能冠名"结论"，且必须明说重调不算重复 ——
    否则它与 summarizer 写在占位符里的"用同样参数重调即可取回"正面矛盾。
    """
    s = _FakeState()
    tc.store(s, "search_kb", {"query": "ATLAS"},
             {"status": "success", "total_matched": 3}, turn=1)
    d = tc.findings_digest(s)
    assert d and "ATLAS" in d
    # 判据是"有没有冠名成已确立的结论"，不是"出不出现结论二字"——
    # 标题里那句"不是结论"正是要的免责，按字面扫会把它自己判红。
    assert "已确立" not in d, "把截断后的摘要冠名成了「已确立的结论」"
    assert "不是结论" in d, "没有声明这些行只是摘要"
    # ⚠️ 这里**不**断言"告诉模型重调取原文不算重复"——当前架构下重调确实会被
    # 计数、够多次照样熔断，写那句话等于第二句假话。summarizer 的"重调即可取回"
    # 与本层的重复计数仍然矛盾，只靠 COMPACT_MIN_CHARS 缓解了一半；根治见
    # docs/RFC_CONTEXT_AS_A_RENDERED_VIEW.md。


def test_the_pointer_never_stamps_a_status_the_producer_did_not_stamp():
    """紧凑化只准砍体积，不准盖章 —— 第二道防线，独立于"失败不进缓存"。

    第一版这条判据是靠"失败结果不许被报成成功"来写的，但新实现里失败**根本
    进不了结果缓存**，那条路对失败不可达 —— 变异掉 `out["status"] = "success"`
    测试照样全绿。判据必须直接打在紧凑化这一步上：拿一个**产生方没盖章**的大
    结果，紧凑之后也不许凭空多出一个 status。
    """
    s = _FakeState()
    big = {"total_matched": 3, "items": [{"text": "x" * 900} for _ in range(40)]}
    assert "status" not in big
    tc.store(s, "search_kb", {"query": "ATLAS"}, big, turn=1)
    out = tc.lookup(s, "search_kb", {"query": "ATLAS"}, is_live=True)
    assert out is not None and out.get("already_in_context") is True
    assert "status" not in out, f"本层凭空盖了章：status={out.get('status')!r}"


def test_a_live_repeat_gets_a_pointer_not_a_lossy_copy():
    """⚠️ 判据已更新（工作集重构）。

    原判据是"小结果重调即**原样返回**"。那是在旧架构里能做到的最好情况 ——
    但仍然是本层在替模型作答。工作集接管之后，原文就在上文逐字摆着，本层
    该做的是**指路**，一份副本都不该再发：有损的不行，无损的也是浪费。

    `already_in_context` 就是那个指针；`status` 仍然继承产生方盖的章。
    """
    s = _FakeState()
    res = {"status": "success", "total_matched": 3, "items": ["a", "b"]}
    tc.store(s, "search_kb", {"query": "ATLAS"}, res, turn=1)
    for _ in range(tc.HARD_REPEAT - 2):
        hit = tc.lookup(s, "search_kb", {"query": "ATLAS"}, is_live=True)
        assert hit is not None
        assert hit.get("already_in_context") is True, "还在发副本"
        assert "items" not in hit, "又把内容重发了一遍"
        assert hit.get("status") == "success", "status 没继承产生方的章"


# ── ② 护栏必须给得出可执行的出口 ────────────────────────────────────────────

def test_a_repeated_failure_carries_the_repeat_fact_to_the_model():
    """失败要真跑（本层不替它作答），但"你已经这样失败过 N 次"必须送达。"""
    s = _FakeState()
    out = None
    for i in range(tc.SOFT_REPEAT + 1):
        if tc.lookup(s, "read_file", {"path": "/wt/x.md"}) is None:
            out = tc.store(s, "read_file", {"path": "/wt/x.md"}, dict(_MISSING), turn=i)
    assert out is not None and "repeat_note" in out, "重复事实没送到模型面前"
    assert out["status"] == "error", "附加提示不许改动产生方盖的章"


# ── ③ 拆开台账与缓存之后，失败循环仍然必须熔断 ──────────────────────────────

def _drive_failing_loop(s, n: int):
    """把同一个失败调用重复 n 次，返回最后一个交给模型的 envelope。"""
    last = None
    for i in range(n):
        hit = tc.lookup(s, "read_file", {"path": "/wt/x.md"})
        last = hit if hit is not None else tc.store(
            s, "read_file", {"path": "/wt/x.md"}, dict(_MISSING), turn=i)
    return last


def test_a_failing_loop_is_still_counted_and_still_breaks():
    """v3.4 最容易引入的回归：失败不进结果缓存了，如果计数还挂在缓存条目的
    `hits` 上，2026-08-21 那条连撞 64 次"找不到文件"的循环就**一次都数不到**，
    熔断永远不会触发。计数必须走台账。
    """
    s = _FakeState()
    env = _drive_failing_loop(s, tc.BREAK_REPEAT)
    assert env["status"] == "error"
    assert env.get("run_terminated") is True, "失败循环没有熔断 —— 计数丢了"
    assert len(s.hook_state["blockers"]) == 1
    assert s.hook_state["_loop_terminal"]["status"] == "failed"


def test_the_hard_directive_for_a_failing_loop_offers_a_usable_exit():
    """那 64 次全是 `找不到文件`，而旧文案只会说"把已确认的结论写进产物"——
    模型手里根本没有结论可落。**给不出可执行出口的护栏等于没有护栏。**
    """
    s = _FakeState()
    env = _drive_failing_loop(s, tc.HARD_REPEAT)
    assert env["status"] == "error" and env.get("repeat_kind") == "failing"
    assert env.get("run_terminated") is not True, "报错档不该直接停机"
    body = env["error"]
    assert "list_files" in body, "没告诉它怎么看清目录里到底有什么"
    assert "走不通" in body or "不存在" in body
    assert "save_artifact" not in body, "对着失败循环建议落盘 —— 它没有东西可落"


def test_the_succeeding_loop_directive_still_says_write_it_down():
    """成功结果的循环（51,937 次 search_kb 那种）出口不变：落盘再继续。"""
    s = _FakeState()
    tc.store(s, "search_kb", {"query": "ATLAS"}, {"status": "success"}, turn=1)
    last = None
    for _ in range(tc.HARD_REPEAT - 1):
        last = tc.lookup(s, "search_kb", {"query": "ATLAS"})
    assert last["status"] == "error" and last.get("repeat_kind") == "succeeding"
    assert "write_scratchpad" in last["error"] or "save_artifact" in last["error"]


def test_the_blocker_body_never_passes_a_failure_off_as_a_finding():
    """`_break()` 把 `findings_digest` 写进 blocker 正文交给 orchestrator。
    失败若混在里面，"读不到"就变成了派发决策依据的"事实"——这是本次事故里
    传得最远的一条谎。
    """
    s = _FakeState()
    tc.store(s, "search_kb", {"query": "REAL_FINDING"},
             {"status": "success", "total_matched": 3}, turn=0)
    _drive_failing_loop(s, tc.BREAK_REPEAT)
    summary = s.hook_state["blockers"][0]["summary"]
    assert "REAL_FINDING" in summary, "真结论没被带走"
    assert "找不到文件" not in summary.split("📒")[-1], \
        "失败报错混进了「查过的条目」台账"
    action = s.hook_state["blockers"][0]["requested_action"]
    assert "存不存在" in action or "不存在" in action, \
        "失败循环的重派建议还在说「换更具体的查询」"


@pytest.mark.asyncio
async def test_the_repeat_fact_actually_reaches_the_llm(tmp_path, monkeypatch):
    """接线测试：`store()` 返回了提示 ≠ 提示到了模型面前。

    第一版只断言 `tc.store(...)` 的返回值里有 `repeat_note`，于是把 agent_loop
    那行改成 `_ = _tool_cache.store(...)`（丢弃返回值）测试照样全绿 —— 测的是
    我以为的送达，不是真的送达。判据必须落在**喂给 LLM 的那条 tool 消息**上。
    """
    from core import tool_registry
    from core.agent_loop import run_loop
    from core.harness import NodeHarness, SummarizerConfig
    from core.llm import LLMResponse
    from core.state import State

    async def _always_missing(*, state, **kwargs):
        return {"status": "error", "error": "找不到文件：/wt/x.md"}

    monkeypatch.setitem(tool_registry._REGISTRY.executors, "read_file", _always_missing)
    monkeypatch.setitem(
        tool_registry._REGISTRY.tools, "read_file",
        tool_registry.ToolDefinition(
            name="read_file", description="d",
            parameters_schema={"type": "object", "properties": {}},
            replayable_read=True,
        ),
    )

    class _StubbornLLM:
        def __init__(self): self.n = 0
        async def chat(self, messages, **kw):
            self.n += 1
            if self.n > tc.SOFT_REPEAT + 2:
                return LLMResponse(content="stop", tool_calls=[],
                                   finish_reason="stop", usage={})
            return LLMResponse(
                content=f"第 {self.n} 次尝试",
                tool_calls=[{"id": f"c{self.n}", "type": "function",
                             "function": {"name": "read_file",
                                          "arguments": '{"path": "/wt/x.md"}'}}],
                finish_reason="tool_calls", usage={})

    harness = NodeHarness(node_type="literature", max_turns=20, tools=["read_file"],
                          summarizer=SummarizerConfig(enabled=False))
    state = State.new(node_type="literature", base_dir=tmp_path, project_id="p_wire2")
    result = await run_loop(harness, state, [], _StubbornLLM())

    tool_msgs = [m.content or "" for m in result.messages if m.role == "tool"]
    assert tool_msgs, "没有 tool 消息，测试没跑到路径上"
    assert any("repeat_note" in c for c in tool_msgs), \
        "重复事实没有出现在喂给 LLM 的 tool 消息里 —— 返回值被丢在了半路"
    # 墓碑本身不是 error（它是"这条已移出上下文"的说明），所以判据落在
    # **还带着内容的那些**消息上：它们一条都不许被改判成成功。
    from core.context_view import TOMBSTONE_PREFIX
    bodies = [c for c in tool_msgs if not c.startswith(TOMBSTONE_PREFIX)]
    assert bodies, "全是墓碑，没测到内容"
    assert all('"status": "error"' in c or "'status': 'error'" in c
               for c in bodies), "失败在送达途中被改判了"


# ── ④ 报错必须列出正确答案 ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_reading_a_missing_file_in_an_empty_dir_says_the_dir_is_empty(tmp_path):
    """事故的点火点：目标目录是空的，旧实现 `if entries:` 静默跳过，模型拿到
    光秃秃一句"找不到文件"。"该目录存在且为空"是能一次排除整棵子树的事实。
    """
    from core.state import State
    from shared.tools.builtin import _read_file

    (tmp_path / "artifacts").mkdir()
    state = State.new(node_type="literature", base_dir=tmp_path, project_id="p_empty")
    state.workspace_root = tmp_path
    out = await _read_file(state, path=str(tmp_path / "artifacts" / "curator_dreaming_report.md"))
    assert out["status"] == "error"
    assert "空" in out["error"], f"空目录被静默跳过了：{out['error']!r}"


@pytest.mark.asyncio
async def test_reading_a_directory_returns_its_listing(tmp_path):
    """框架此刻已经知道答案（就是个目录，一次 iterdir 的事），不许扔掉让模型
    再花一整轮去猜子项名字 —— 事故里这条撞了 18 次。
    """
    from core.state import State
    from shared.tools.builtin import _read_file

    d = tmp_path / "orchestration"
    d.mkdir()
    (d / "artifacts").mkdir()
    (d / "sessions").mkdir()
    state = State.new(node_type="literature", base_dir=tmp_path, project_id="p_dir")
    state.workspace_root = tmp_path
    out = await _read_file(state, path=str(d))
    assert out["status"] == "error", "读目录确实没读成文件，不许盖成 success"
    assert "artifacts/" in out["error"] and "sessions/" in out["error"], \
        f"报错没有把清单给足：{out['error']!r}"


@pytest.mark.asyncio
async def test_a_name_that_exists_nowhere_is_said_out_loud(tmp_path):
    """"我查了整棵树，没有"和"我没查"对模型是两件事，旧实现把前者渲染成了后者。"""
    from core.state import State
    from shared.tools.builtin import _read_file

    (tmp_path / "real.md").write_text("x", encoding="utf-8")
    state = State.new(node_type="literature", base_dir=tmp_path, project_id="p_none")
    state.workspace_root = tmp_path
    state.project_worktree = tmp_path
    out = await _read_file(state, path=str(tmp_path / "sub" / "invented_name.md"))
    assert "没有任何" in out["error"], f"整棵树都没有这个名字，却没说出来：{out['error']!r}"


# ── ⑤ 框架中途发声必须走信封 —— 扫盘，不写名单 ──────────────────────────────

def test_no_module_speaks_to_the_model_as_system_mid_conversation():
    """PR#462 实测：会话**中段**的 `role="system"` → 13/14 的回复以复述注入文本
    开头、输出膨胀 3×；换成 `framework_notice`（user + 归属信封）归零。

    ## 判据扫的是「这件事」，不是某种写法（2026-08-22 收紧）

    第一版只扫 `messages.append(LLMMessage(role="system", …))`。它漏掉了
    `summarizer` 里三处 —— 那里用的是 `new_msgs = [*new_msgs, LLMMessage(...)]`，
    列表拼接不是 append，于是压缩后注回的台账、换届建议、压缩 notice 全程走的
    都是那条实测无效的路径，而护栏一声不吭。**边界画在调用形式上，就只挡得住
    那一种写法。**

    现在的判据：`core/` 与 `shared/` 下**任何**裸的 `LLMMessage(role="system")`
    都算违规。合法用途（某次 LLM 调用的开篇 system prompt）必须显式走
    `core.llm.system_prompt()` —— 把合法的那条路命名出来，剩下的一律违规，
    新写的代码默认被覆盖。

    豁免仅两处，都有结构性理由：
      - `core/llm.py`：`system_prompt` / `framework_notice` 的定义处本身。
      - `core/loop_hooks_builtin.py`：hook 的**返回值**，全部流经
        `loop_hooks._as_framework_notices` 这个咽喉统一转成 framework-notice。
    """
    root = pathlib.Path(__file__).resolve().parents[1]
    exempt = {"core/llm.py", "core/loop_hooks_builtin.py", "core/loop_hooks.py"}
    offenders: list[str] = []
    for f in sorted((root / "core").rglob("*.py")) + sorted((root / "shared").rglob("*.py")):
        rel = str(f.relative_to(root))
        if rel in exempt:
            continue
        try:
            tree = ast.parse(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        for n in ast.walk(tree):
            if not (isinstance(n, ast.Call) and getattr(n.func, "id", None) == "LLMMessage"):
                continue
            for kw in n.keywords:
                if (kw.arg == "role" and isinstance(kw.value, ast.Constant)
                        and kw.value.value == "system"):
                    offenders.append(f"{rel}:{n.lineno}")
    assert not offenders, (
        "这些地方裸构造了 system 角色消息。会话中段对模型说话必须走 "
        "framework_notice()；某次 LLM 调用的开篇 system prompt 走 system_prompt()：\n  "
        + "\n  ".join(offenders))


def test_the_compression_notice_is_found_by_marker_not_by_role():
    """压缩 notice 的定位判据不许依赖角色。

    它的角色刚从 system 改成 framework-notice。若 `_is_compression_notice` /
    `_extract_prior_summary` 还按 `role == "system"` 过滤，后果是**静默**的：
    running summary 断链（每次压缩从零开始，摘要碎成一段段）、旧 notice 摘不掉
    （一轮一轮堆在 head 里）。两者都不报错。
    """
    from core import summarizer as sm

    notice = sm.build_compression_notice(3, "test", "## 历史压缩摘要\nX")
    assert notice.role != "system", "还在用 system 角色中段说话"
    assert sm._is_compression_notice(notice), "换了角色就认不出自己写的 notice 了"
    assert sm._extract_prior_summary([notice]) is not None, "running summary 断链"


"""进展熔断：按"这一轮有没有发生什么"停机。

真实事故回放（2026-08-13，v26 的 hypothesis）：模型连续 591 轮输出**一字不差**
的同一句话，每轮只调 write_scratchpad / read_file，1482 轮 / 117M tokens / 5h40m。
三道现有防线全部穿过：

  - 协议熔断      → 全程协议正常
  - 重复调用缓存  → write_scratchpad 每次参数都不同；read_file 不在白名单里
  - 空轮回滚      → 每轮都有 tool_calls，不算空轮

所以判据不能是"调了哪个工具"，只能是"有没有发生变化"。
"""
from __future__ import annotations

from core.progress_breaker import ProgressBreaker, response_signature
from core.state import State


def _state(tmp_path):
    return State.new("hypothesis", tmp_path / "rt", project_id="p")


def _call(name, args):
    return [{"type": "function", "function": {"name": name, "arguments": args}}]


def test_identical_replies_with_no_durable_change_abort(tmp_path):
    """现场原话：'I need to break the scratchpad loop.' —— 说了 591 遍。"""
    state = _state(tmp_path)
    breaker = ProgressBreaker()
    said = "I need to break the scratchpad loop."
    calls = _call("read_file", '{"path": "/x/research_plan.md"}')

    aborted_at = None
    for turn in range(1, 40):
        d = breaker.record(state, response_signature(said, calls), turn=turn)
        if d.should_abort:
            aborted_at = turn
            break
    assert aborted_at is not None and aborted_at <= 8, "591 轮那种复读必须早早停掉"
    assert "逐字节相同" in d.diagnosis
    # 诊断要指向真病根，否则模型/人只会原样重跑
    assert "没有对应的工具" in d.diagnosis


def test_warning_comes_before_the_kill(tmp_path):
    state = _state(tmp_path)
    breaker = ProgressBreaker()
    warned = False
    for turn in range(1, 20):
        d = breaker.record(state, response_signature("同一句话", _call("read_file", "{}")),
                           turn=turn)
        if d.should_warn:
            warned = True
            assert "换一个动作" in d.diagnosis
        if d.should_abort:
            break
    assert warned, "先给一次可行动的警告，再停机"


def test_different_replies_never_abort(tmp_path):
    """正常干活：每轮说的做的都不一样 —— 一次都不该被打断。"""
    state = _state(tmp_path)
    breaker = ProgressBreaker()
    for turn in range(1, 200):
        d = breaker.record(
            state,
            response_signature(f"第 {turn} 步：读第 {turn} 个文件",
                               _call("read_file", f'{{"p":{turn}}}')),
            turn=turn,
        )
        assert not d.should_abort


def test_same_words_but_real_work_landed_is_not_a_loop(tmp_path):
    """说同样的话但确实在落盘（比如批量跑模拟）→ 不是原地转。"""
    state = _state(tmp_path)
    breaker = ProgressBreaker()
    for turn in range(1, 30):
        state.hook_state["_project_workspace_fingerprint"] = f"sha-{turn}"
        d = breaker.record(state, response_signature("继续跑下一个温度点",
                                                     _call("safe_run_bash", "{}")), turn=turn)
        assert not d.should_abort


def test_revising_the_board_counts_as_progress(tmp_path):
    """改写白板 = 模型在推进工作状态，不算原地转。"""
    from core import whiteboard

    state = _state(tmp_path)
    breaker = ProgressBreaker()
    for turn in range(1, 30):
        whiteboard.write(state, f"当前在第 {turn} 步", turn=turn)
        d = breaker.record(state, response_signature("继续", _call("write_scratchpad", "{}")),
                           turn=turn)
        assert not d.should_abort


def test_stall_only_notices_never_kills(tmp_path):
    """连着几十轮不落盘可能完全正常（读文献、翻目录）→ 只送事实，不判决。

    证据与判决分开：误停一条正常 run，比多说一句话贵得多。
    """
    state = _state(tmp_path)
    breaker = ProgressBreaker()
    notices = 0
    for turn in range(1, 120):
        d = breaker.record(
            state,
            response_signature(f"读第 {turn} 篇", _call("read_file", f'{{"i":{turn}}}')),
            turn=turn,
        )
        assert not d.should_abort
        if d.should_warn:
            notices += 1
            assert "如果这是有意的" in d.diagnosis
    assert 1 <= notices <= 10, "提示要稀疏，不能每轮 spam"


def test_signature_includes_tool_arguments(tmp_path):
    """只比工具名会把"读了不同的文件"误判成复读。"""
    a = response_signature("读文件", _call("read_file", '{"path": "a.md"}'))
    b = response_signature("读文件", _call("read_file", '{"path": "b.md"}'))
    assert a != b


def test_artifact_written_counts_as_progress_without_a_worktree(tmp_path):
    """没绑 worktree（CLI / 单机）时，run-local 产物是唯一的落盘信号。"""
    state = _state(tmp_path)
    assert not state.project_worktree
    breaker = ProgressBreaker()
    for turn in range(1, 30):
        state.save_artifact("note", f"n{turn}", "body")
        d = breaker.record(state, response_signature("存一个", _call("save_artifact", "{}")),
                           turn=turn)
        assert not d.should_abort


def test_thresholds_are_configurable(monkeypatch, tmp_path):
    monkeypatch.setenv("HARNESS_REPEAT_ABORT", "3")
    state = _state(tmp_path)
    breaker = ProgressBreaker()
    hits = [breaker.record(state, response_signature("x", None), turn=t).should_abort
            for t in range(1, 6)]
    assert any(hits)

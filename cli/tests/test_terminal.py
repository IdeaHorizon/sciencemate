"""Terminal UI regression tests maintained with the CLI."""

import io
import sys

from cli import terminal as chat_mod


def test_legacy_node_and_tool_names_render_as_analysis(monkeypatch):
    from types import SimpleNamespace

    assert chat_mod._run_node_targets("run_node", {"node_type": "hypothesis"}) == "analysis"
    assert chat_mod._describe_call("validate_hypothesis_outputs", {}) == "validate_analysis_outputs"
    assert chat_mod._describe_call("run_node", {"node_type": "hypothesis"}) == (
        "run_node(node_type=analysis)"
    )
    assert chat_mod._describe_call("save_artifact", {"name": "hypothesis"}) == (
        "save_artifact(name=hypothesis)"
    )
    lines = []
    monkeypatch.setattr(chat_mod, "_emit_above_prompt", lines.append)
    chat_mod._print_pause_prompt(SimpleNamespace(
        asking_node_type="hypothesis", question="Continue?", context=None, options=[]))
    assert "来自节点：analysis" in lines[0]


class _FakeRealStream:
    """录制 write() 调用，冒充真实 sys.stderr 给 _PromptSafeStderr 包。"""

    def __init__(self):
        self.writes: list[str] = []

    def write(self, s: str) -> int:
        self.writes.append(s)
        return len(s)

    def flush(self) -> None:
        pass

    def isatty(self) -> bool:
        return True


def test_prompt_safe_stderr_buffers_until_full_line():
    """半行不触发清行/重画；凑齐一整行才落地，且落地内容干净无残留。

    复现场景（issue：终端花屏）：node hook 直接 print(msg, file=sys.stderr,
    flush=True) 会把消息拆成多次 write（内容 + 换行符），如果不按行缓冲，
    半行也会触发一次清行+重画，视觉上提示符被反复顶出来。
    """
    chat_mod._PROMPT_LIVE["on"] = True
    chat_mod._IS_TTY = True
    # 清行/重画本身写真实 sys.stdout（跟 _emit_above_prompt 同款协议）——
    # 测试只关心 _PromptSafeStderr 有没有正确按行缓冲、转发给被包的流，
    # 不关心提示符重画的具体 ANSI 输出，摘掉避免污染 pytest 自己的输出。
    saved_clear = chat_mod._clear_input_line
    saved_redraw = chat_mod._redraw_prompt
    chat_mod._clear_input_line = lambda: None
    chat_mod._redraw_prompt = lambda: None
    try:
        real = _FakeRealStream()
        wrapped = chat_mod._PromptSafeStderr(real)

        # print() 典型地拆成两次 write：消息体 + 换行
        wrapped.write("[experiment 14:46:02] tool_done: safe_execute_python")
        assert real.writes == [], "半行不该提前落地"
        wrapped.write("\n")
        assert real.writes == ["[experiment 14:46:02] tool_done: safe_execute_python\n"]

        wrapped.write("[experiment 14:46:27] llm_tool_calls: safe_execute_python\n")
        assert real.writes[-1] == "[experiment 14:46:27] llm_tool_calls: safe_execute_python\n"
        assert len(real.writes) == 2, "两条完整行 → 恰好两次真实落地写"
    finally:
        chat_mod._clear_input_line = saved_clear
        chat_mod._redraw_prompt = saved_redraw
        chat_mod._PROMPT_LIVE["on"] = False


def test_emit_above_prompt_flushes_when_not_tty(monkeypatch):
    """管道下 stdout 是块缓冲的：不 flush 的话进度提示要等一轮跑完才吐出来 ——
    用户看到的就是"完全没反馈"。"""
    from cli import terminal as chat_mod

    flushed: list[bool] = []

    class _Spy(io.StringIO):
        def isatty(self):
            return False

        def flush(self):
            flushed.append(True)
            super().flush()

    spy = _Spy()
    monkeypatch.setattr(chat_mod, "_IS_TTY", False)
    monkeypatch.setitem(chat_mod._PROMPT_LIVE, "on", False)
    monkeypatch.setattr(sys, "stdout", spy)
    chat_mod._emit_above_prompt("⏳ 处理中…")

    assert "⏳ 处理中…" in spy.getvalue()
    assert flushed, "非 tty 下必须 flush"


def test_reasoning_display_writes_and_flushes_when_not_tty(monkeypatch):
    """非 tty 下思考指示同样要即时可见。"""
    from cli import terminal as chat_mod

    flushed: list[bool] = []

    class _Spy(io.StringIO):
        def isatty(self):
            return False

        def flush(self):
            flushed.append(True)
            super().flush()

    spy = _Spy()
    monkeypatch.setattr(chat_mod, "_IS_TTY", False)
    monkeypatch.setattr(chat_mod, "_REASON_MODE", "brief")
    monkeypatch.setattr(
        chat_mod, "_REASON_STATE", {"active": False, "chars": 0, "t0": 0.0, "last_tick": 0.0}
    )
    monkeypatch.setattr(sys, "stdout", spy)

    chat_mod._reasoning_display("思考内容")
    chat_mod._reasoning_display(None)

    out = spy.getvalue()
    assert "思考中" in out, f"思考阶段没有任何指示：{out!r}"
    assert out.endswith("\n"), "推理段必须以换行收尾，别跟正文挤一行"
    assert flushed


def test_reasoning_display_off_mode_silent(monkeypatch):
    from cli import terminal as chat_mod

    spy = io.StringIO()
    monkeypatch.setattr(chat_mod, "_REASON_MODE", "off")
    monkeypatch.setattr(
        chat_mod, "_REASON_STATE", {"active": False, "chars": 0, "t0": 0.0, "last_tick": 0.0}
    )
    monkeypatch.setattr(sys, "stdout", spy)
    chat_mod._reasoning_display("x")
    chat_mod._reasoning_display(None)
    assert spy.getvalue() == ""


def test_reasoning_display_full_mode_streams_text(monkeypatch):
    from cli import terminal as chat_mod

    spy = io.StringIO()
    monkeypatch.setattr(chat_mod, "_IS_TTY", False)
    monkeypatch.setattr(chat_mod, "_REASON_MODE", "full")
    monkeypatch.setattr(
        chat_mod, "_REASON_STATE", {"active": False, "chars": 0, "t0": 0.0, "last_tick": 0.0}
    )
    monkeypatch.setattr(sys, "stdout", spy)
    chat_mod._reasoning_display("推理正文")
    chat_mod._reasoning_display(None)
    assert "推理正文" in spy.getvalue()


def test_prompt_safe_stderr_passthrough_when_prompt_not_live():
    """提示符还没接管（启动阶段）/非 tty 时，直接透传，不缓冲。"""
    chat_mod._PROMPT_LIVE["on"] = False
    real = _FakeRealStream()
    wrapped = chat_mod._PromptSafeStderr(real)
    wrapped.write("early startup log\n")
    assert real.writes == ["early startup log\n"]

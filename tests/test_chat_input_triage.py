"""输入分诊前门 + panic + /undo + 会话锁 + 崩溃遗留扫描（R0 批次）。

四条输入健壮性不变量的机械保障：
  1. 任何单条输入不能永久损坏 session（超长输入落盘，修毒丸 400 死循环）
  2. 任何用户意图不能静默丢失（多意图提示；deferred 已有覆盖）
  3. 任何不可逆变更必须有确认或补偿路径（artifact 覆盖历史 + /undo）
  4. 用户任何时刻有体面退出路径（/stop panic）
不联网。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.bootstrap import bootstrap
from core.llm import FRAMEWORK_NOTICE_OPEN, LLMMessage
from core.state import State

bootstrap()

import chat as chat_mod   # noqa: E402


def _state(tmp_path: Path, project_id="p_triage") -> State:
    return State.new(node_type="_orchestrator", base_dir=tmp_path,
                       project_id=project_id)


# ── 1) 毒丸修复：超长输入落盘 ────────────────────────────────────────────────

def test_oversize_input_diverted_to_file(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("LLM_CONTEXT_WINDOW", "40000")   # 上限 = 10000 chars
    state = _state(tmp_path)
    messages: list[LLMMessage] = []
    giant = "实验数据" * 5000        # 20000 chars > 10000
    out = chat_mod._preprocess_user_input(state, messages, giant)

    assert len(out) < 5000                        # 消息被替换成预览
    assert "attachments" in out and "read_file" in out
    files = list((state.root / "attachments").glob("paste_*.txt"))
    assert len(files) == 1
    assert files[0].read_text(encoding="utf-8") == giant   # 原文完整落盘


def test_normal_input_untouched(tmp_path):
    state = _state(tmp_path)
    messages: list[LLMMessage] = []
    out = chat_mod._preprocess_user_input(state, messages, "看看 KB 里有什么")
    assert out == "看看 KB 里有什么"
    assert messages == []                          # 无任何注入


# ── 2) 粘贴注入框架 ─────────────────────────────────────────────────────────

def test_long_paste_gets_data_frame(tmp_path):
    state = _state(tmp_path)
    messages: list[LLMMessage] = []
    paste = "这是论文原文。\n" * 30                  # 多行粘贴
    chat_mod._preprocess_user_input(state, messages, paste)
    # 框架中途说话一律是 framework-notice（user 角色 + 归属信封）：中段 system
    # 会被模型复读，也会被严格网关直接 400（2026-09-15 yuankk 的 qwen 网关）。
    framed = [m for m in messages if m.role == "user" and FRAMEWORK_NOTICE_OPEN in (m.content or "")]
    assert any("不是【指令】" in (m.content or "") or "不得执行" in (m.content or "")
               for m in framed)
    assert not any(m.role == "system" for m in messages)


# ── 3) 多意图提示 ────────────────────────────────────────────────────────────

def test_multi_intent_hint_injected(tmp_path):
    state = _state(tmp_path)
    messages: list[LLMMessage] = []
    chat_mod._preprocess_user_input(
        state, messages,
        "先停掉现在的实验，另外帮我查一下 LJ 截断的最佳实践，顺便看看 proposal 队列")
    assert any("多个独立请求" in (m.content or "") for m in messages)


def test_single_intent_no_hint(tmp_path):
    state = _state(tmp_path)
    messages: list[LLMMessage] = []
    chat_mod._preprocess_user_input(state, messages, "看看当前项目状态如何了")
    assert not any("多个独立请求" in (m.content or "") for m in messages)


# ── 4) artifact 版本快照 + /undo ────────────────────────────────────────────

def test_overwrite_backs_up_and_undo_restores(tmp_path):
    """覆盖 = 同一身份的新版本；旧版进 run 内版本快照 `<run>/versions/`
    （RFC 2026-08-18；账本形状按 RFC 2026-09-12 §6：正文是原生文件，事实在账本）。

    `.history/` 那套单独的覆盖备份已删除 —— 一个版本原语覆盖两件事，
    /undo 的补偿路径改指版本快照，语义不变。
    """
    state = _state(tmp_path)
    state.save_artifact("survey_report", "review", "第一版内容")
    state.save_artifact("survey_report", "review", "第二版（改错了）")

    # 覆盖时旧版进 run 内快照 + hook_state 记录
    hist = list((state.root / "versions").glob("survey_report__review@v1.*"))
    assert len(hist) == 1
    assert hist[0].read_text(encoding="utf-8") == "第一版内容"
    assert hist[0].name == "survey_report__review@v1.md", hist[0].name
    assert [v["content"] for v in state.artifact_versions("survey_report__review")] == \
        ["第一版内容", "第二版（改错了）"]
    assert state.hook_state["_last_artifact_overwrite"]["artifact_id"] == \
        "survey_report__review"

    # /undo 恢复（把 v1 作为新一版写回）
    msg = chat_mod._cmd_undo(state)
    assert "v1" in msg, msg
    rec = state.read_artifact("survey_report__review")
    assert rec["content"] == "第一版内容"


def test_undo_without_history_explains(tmp_path):
    state = _state(tmp_path)
    msg = chat_mod._cmd_undo(state)
    assert "没有可撤销" in msg


def test_frozen_artifact_not_undoable(tmp_path):
    state = _state(tmp_path)
    state.save_artifact("note", "n1", "v1")
    state.save_artifact("note", "n1", "v2")
    # 冻结当前版（账本一行，文件不动）
    state.mark_frozen("note__n1", {})
    msg = chat_mod._cmd_undo(state)
    assert "frozen" in msg or "冻结" in msg
    assert state.read_artifact("note__n1")["content"] == "v2"   # 没被回滚
    assert state.find_artifact_path("note__n1").read_text(encoding="utf-8") == "v2"


def test_history_not_visible_in_list_artifacts(tmp_path):
    state = _state(tmp_path)
    state.save_artifact("note", "n2", "v1")
    state.save_artifact("note", "n2", "v2")
    ids = [a["id"] for a in state.list_artifacts()]
    assert ids == ["note__n2"]                     # .history 不泄漏进列表


# ── 5) panic：/stop 给 orchestrator + child 写 kill_signal ──────────────────

def test_panic_stop_signals_orchestrator_and_children(tmp_path, capsys):
    from core.pause import ActiveRunInfo, register_active, unregister_active

    state = _state(tmp_path)
    child = State.new(node_type="experiment", base_dir=tmp_path, project_id="p_triage")
    register_active(ActiveRunInfo(run_id=child.run_id, node_type="experiment",
                                    state=child))
    try:
        cs = chat_mod.ChatState()
        chat_mod._do_panic_stop(state, cs)
        assert state.hook_state["kill_signal"]["requested_by"] == "user_panic"
        assert child.hook_state["kill_signal"]["requested_by"] == "user_panic"
        assert cs.panic.is_set()
    finally:
        unregister_active(child.run_id)


# ── 6) 会话锁 ────────────────────────────────────────────────────────────────

def test_session_lock_rejects_second_instance(tmp_path):
    state = _state(tmp_path)
    assert chat_mod._acquire_session_lock(state) is True
    # 同进程第二次拿（新 fd）应失败 —— flock 排他
    saved = chat_mod._SESSION_LOCK_HANDLE
    try:
        assert chat_mod._acquire_session_lock(state) is False
    finally:
        chat_mod._SESSION_LOCK_HANDLE = saved
        if saved is not None:
            saved.close()
            chat_mod._SESSION_LOCK_HANDLE = None


# ── 7) 崩溃遗留 run 扫描 ─────────────────────────────────────────────────────

def test_scan_orphaned_runs(tmp_path):
    base = tmp_path / "runs"
    # 正常完结的 run（有 summary）→ 不算
    ok = base / "run_ok"; ok.mkdir(parents=True)
    (ok / "messages_checkpoint.json").write_text("{}")
    (ok / "summary.json").write_text("{}")
    # 跑一半死了（有 checkpoint 无 summary）
    half = base / "run_half"; half.mkdir()
    (half / "messages_checkpoint.json").write_text("{}")
    # 死在 pause 等答复
    paused = base / "run_paused"; paused.mkdir()
    (paused / "pause_pending.json").write_text("{}")
    # 当前 run 自己 → 不算
    cur = base / "orchestrator__me"; cur.mkdir()
    (cur / "messages_checkpoint.json").write_text("{}")

    orphans = chat_mod._scan_orphaned_runs(base, "orchestrator__me")
    kinds = {o["run_id"]: o["kind"] for o in orphans}
    assert kinds == {"run_half": "interrupted", "run_paused": "paused_orphan"}


# ── 多行粘贴合并（2026-07-12：修 input() 逐行读把粘贴按 \n 切断的 bug）────────

def _make_reader(remaining):
    """把一串"后续行"做成 read_next + more_available（模拟 stdin 缓冲）。"""
    buf = list(remaining)
    def more_available():
        return bool(buf)
    def read_next():
        return buf.pop(0) if buf else None
    return read_next, more_available


def test_multiline_paste_coalesced_into_one_input():
    """粘贴的多行（首行 + 3 续行）合并成单条、用 \\n 连接 —— 不再逐行切断。"""
    read_next, more = _make_reader(["第二行", "第三行", "末行"])
    out = chat_mod._coalesce_pasted_lines("第一行", read_next, more)
    assert out == "第一行\n第二行\n第三行\n末行"


def test_single_line_input_not_altered():
    """普通单行输入：没有续行缓冲 → 原样返回，不加换行。"""
    read_next, more = _make_reader([])
    out = chat_mod._coalesce_pasted_lines("就一行", read_next, more)
    assert out == "就一行"


def test_coalesce_stops_at_eof():
    """续行读到 EOF（read_next 抛 EOFError）→ 停在已读到的行，不炸。"""
    def more_available():
        return True
    def read_next():
        raise EOFError
    out = chat_mod._coalesce_pasted_lines("头行", read_next, more_available)
    assert out == "头行"


def test_coalesce_stops_when_read_next_returns_none():
    calls = {"n": 0}
    def more_available():
        return True                      # 一直说"有"，靠 None 停
    def read_next():
        calls["n"] += 1
        return "续" if calls["n"] == 1 else None
    out = chat_mod._coalesce_pasted_lines("头", read_next, more_available)
    assert out == "头\n续"


def test_coalesce_strips_bracketed_paste_markers():
    """老 readline/终端把 bracketed-paste 转义序列当字面量灌进来 → 剥掉。"""
    read_next, more = _make_reader(["中间", "尾行\x1b[201~"])
    out = chat_mod._coalesce_pasted_lines("\x1b[200~首行", read_next, more)
    assert "\x1b[200~" not in out and "\x1b[201~" not in out
    assert out == "首行\n中间\n尾行"


def test_stdin_more_buffered_safe_when_not_selectable(monkeypatch):
    """stdin 不可 select（如测试环境的假对象）→ 返 False，不炸、退化逐行。"""
    import io
    monkeypatch.setattr(chat_mod.sys, "stdin", io.StringIO("x"))
    # StringIO 没有 fileno 可 select → 异常被吞 → False
    assert chat_mod._stdin_more_buffered(timeout=0.0) is False


# ── 终端 termios 复原（2026-07-13：修 qinp 报告 /exit / Ctrl-C 后不回显 bug）──
# PTY 级回归：造一对 pty，模拟 readline 把 TTY 切成 raw（-icanon -echo），
# 验证 _restore_terminal_state 能把 canon+echo 复原回来。

def test_restore_terminal_state_reenables_canon_and_echo():
    import os as _os
    pty = pytest.importorskip("pty")
    termios = pytest.importorskip("termios")

    master, slave = pty.openpty()
    try:
        sane = termios.tcgetattr(slave)                 # 初始：canon+echo 都在
        raw = termios.tcgetattr(slave)
        raw[3] &= ~(termios.ICANON | termios.ECHO)      # lflag：关 canon + echo（模拟 readline raw 态）
        termios.tcsetattr(slave, termios.TCSANOW, raw)
        broken = termios.tcgetattr(slave)
        assert not (broken[3] & termios.ICANON)         # 确认现在确实坏了
        assert not (broken[3] & termios.ECHO)

        chat_mod._restore_terminal_state((slave, sane))  # ← 被测：复原

        fixed = termios.tcgetattr(slave)
        assert fixed[3] & termios.ICANON                # canonical 回来了
        assert fixed[3] & termios.ECHO                  # echo 回来了
    finally:
        _os.close(master)
        _os.close(slave)


def test_restore_terminal_state_none_is_noop():
    """saved=None（非 tty / Windows）→ 不炸、无操作。"""
    chat_mod._restore_terminal_state(None)              # 不抛异常即通过


def test_save_terminal_state_returns_none_when_not_a_tty(monkeypatch):
    """stdin 不是 tty（pytest 环境 / 管道）→ 返 None，优雅降级。"""
    import io
    monkeypatch.setattr(chat_mod.sys, "stdin", io.StringIO("x"))
    assert chat_mod._save_terminal_state() is None


def test_save_then_restore_roundtrip_on_pty():
    """在真 pty 上 save→改 raw→restore，关心的本地模式位（canon/echo/signals）复原。

    只比 lflag 里的关键位（ICANON/ECHO/ECHOE/ISIG）——bug 就出在这些位；pty 驱动
    在 tcsetattr 后重读 lflag 会置一些高位（EXTPROC 之类），比整段 lflag 全等太苛刻。
    """
    import os as _os
    pty = pytest.importorskip("pty")
    termios = pytest.importorskip("termios")

    mask = termios.ICANON | termios.ECHO | termios.ECHOE | termios.ISIG
    master, slave = pty.openpty()
    try:
        original = termios.tcgetattr(slave)
        raw = termios.tcgetattr(slave)
        raw[3] &= ~(termios.ICANON | termios.ECHO)
        termios.tcsetattr(slave, termios.TCSANOW, raw)
        assert (termios.tcgetattr(slave)[3] & mask) != (original[3] & mask)   # 确认被改坏
        chat_mod._restore_terminal_state((slave, original))
        assert (termios.tcgetattr(slave)[3] & mask) == (original[3] & mask)   # 关键位复原
    finally:
        _os.close(master)
        _os.close(slave)


# ── #141: listener 收到 /exit /quit 后停止读取（不再进下一次 input() 持有 raw TTY）──

def _run_listener_with_scripted_input(lines):
    """同步跑 _stdin_listener（用假 loop/queue + 脚本 input），返回
    (queued_messages, input_call_count)。/exit 或 EOF 会让它 return，不会死循环。"""
    calls = {"n": 0}
    seq = list(lines)

    def fake_input(prompt=""):
        calls["n"] += 1
        if not seq:
            raise EOFError
        return seq.pop(0)

    class _FakeLoop:
        def call_soon_threadsafe(self, fn, arg):
            fn(arg)                         # 同步执行，不需要真事件循环

    class _FakeQueue:
        def __init__(self):
            self.items = []
        def put_nowait(self, x):
            self.items.append(x)

    q = _FakeQueue()
    import builtins
    orig_input = builtins.input
    orig_more = chat_mod._stdin_more_buffered
    builtins.input = fake_input
    chat_mod._stdin_more_buffered = lambda *a, **k: False   # 不合并，逐行
    try:
        chat_mod._stdin_listener(_FakeLoop(), q)           # /exit / EOF → return
    finally:
        builtins.input = orig_input
        chat_mod._stdin_more_buffered = orig_more
    return q.items, calls["n"]


def test_listener_stops_after_exit_command():
    msgs, n_calls = _run_listener_with_scripted_input(["/exit"])
    assert msgs == ["/exit"]
    assert n_calls == 1               # 只读了一次，没进下一次 input()


def test_listener_stops_after_quit_command():
    msgs, n_calls = _run_listener_with_scripted_input(["/quit"])
    assert msgs == ["/quit"]
    assert n_calls == 1


def test_listener_keeps_reading_after_normal_message():
    # 普通消息后继续读，第二条是 /exit 才停 → 共 2 次 input()
    msgs, n_calls = _run_listener_with_scripted_input(["你好", "/exit"])
    assert msgs == ["你好", "/exit"]
    assert n_calls == 2


def test_listener_eof_queues_exit_and_stops():
    msgs, n_calls = _run_listener_with_scripted_input([])   # 立即 EOF
    assert msgs == ["/exit"]
    assert n_calls == 1


def test_pty_subprocess_exit_restores_termios():
    """PTY 集成（qinp 要求形态）：子进程模拟 readline 切 raw，退出后 slave 的
    ICANON/ECHO 复原。用 chat 的 save/restore + atexit（跟 _main 一致）。"""
    import os as _os, sys as _sys
    pty = pytest.importorskip("pty")
    termios = pytest.importorskip("termios")
    import subprocess

    child = (
        "import sys, os, termios, atexit\n"
        "sys.path.insert(0, %r)\n"
        "import chat as c\n"
        "saved = c._save_terminal_state()\n"
        "atexit.register(c._restore_terminal_state, saved)\n"
        "fd = sys.stdin.fileno()\n"
        "a = termios.tcgetattr(fd); a[3] &= ~(termios.ICANON | termios.ECHO)\n"
        "termios.tcsetattr(fd, termios.TCSANOW, a)\n"
        "sys.exit(0)\n"
    ) % str(Path(chat_mod.__file__).parent)

    master, slave = pty.openpty()
    try:
        before = termios.tcgetattr(slave)
        p = subprocess.Popen([_sys.executable, "-c", child],
                             stdin=slave, stdout=slave, stderr=subprocess.DEVNULL)
        p.wait(timeout=30)
        after = termios.tcgetattr(slave)
        assert after[3] & termios.ICANON
        assert after[3] & termios.ECHO
        assert (after[3] & termios.ICANON) == (before[3] & termios.ICANON)
    finally:
        _os.close(master)
        _os.close(slave)


# ── #140: bracketed paste 集成（PTY）—— 多行粘贴整段进 buffer，末次 Enter 才提交 ──

def test_bracketed_paste_coalesces_multiline_into_one_input():
    """真 PTY：发 ESC[200~ 多行 ESC[201~ + 单独 Enter，readline 应把整段多行作为
    **一个** input() 返回（内部换行不触发 accept-line）。libedit 后端不支持 → skip。"""
    import os as _os, sys as _sys, time as _time
    pty = pytest.importorskip("pty")
    import readline as _rl
    if "libedit" in (getattr(_rl, "__doc__", "") or "").lower():
        pytest.skip("libedit 后端不支持 enable-bracketed-paste")

    child = (
        "import sys, readline\n"
        "try: readline.parse_and_bind('set enable-bracketed-paste on')\n"
        "except Exception: pass\n"
        "sys.stderr.write('GOT:' + repr(input('')) + '\\n')\n"
    )
    master, slave = pty.openpty()
    pid = _os.fork()
    if pid == 0:
        _os.close(master)
        _os.dup2(slave, 0); _os.dup2(slave, 1); _os.dup2(slave, 2)
        _os.execv(_sys.executable, [_sys.executable, "-c", child])
    _os.close(slave)
    _time.sleep(0.5)
    _os.write(master, b"\x1b[200~alpha\nbeta\ngamma\x1b[201~")
    _time.sleep(0.2)
    _os.write(master, b"\r")                          # 末次 Enter 才提交
    out = b""
    try:
        while b"GOT:" not in out:
            d = _os.read(master, 4096)
            if not d:
                break
            out += d
    except OSError:
        pass
    _os.waitpid(pid, 0)
    _os.close(master)
    text = out.decode(errors="replace")
    assert "GOT:'alpha\\nbeta\\ngamma'" in text, f"未整段合并：{text!r}"

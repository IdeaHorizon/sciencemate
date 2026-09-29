"""repair_ledger 的错误摘要取抬头行，不靠英文词表（#625）。

E2E v28 实拍：safe_bash 的 scope guard 报的是中文——

    ⛔ 可执行目标不存在（exec_preflight 事前拦截，命令未执行）。
    目标：./run.sh
    解析为：/…/experiment/run.sh
    （解析 cwd：/…/experiment）
    常见原因：① 没有先 `cd <运行目录>`…

原实现按 `error|fatal|failed|cannot|undefined|missing` 挑行，一行都匹配不上，
落到 `splitlines()[-1]` 兜底，账本里只剩 `（解析 cwd：…）`；component 也认不出
→ "unknown" → 永远 open，而 run 其实成功了。
"""
from __future__ import annotations

from nodes.experiment.hooks import _short_error

_GUARD = (
    "⛔ 可执行目标不存在（exec_preflight 事前拦截，命令未执行）。\n"
    "目标：./run.sh\n"
    "解析为：/w/experiment/run.sh\n"
    "（解析 cwd：/w/experiment）\n"
    "常见原因：① 没有先 `cd <运行目录>`；② 路径拼错。\n"
    "命令片段：./run.sh --case a"
)


def test_chinese_guard_error_keeps_its_header_line():
    got = _short_error({"stderr_tail": _GUARD})
    assert got.startswith("⛔ 可执行目标不存在"), got      # 抬头行打头，不是最后那行
    assert "目标：./run.sh" in got                          # 错误的对象也在
    assert len(got) <= 220


def test_english_error_still_yields_its_first_line():
    got = _short_error({"stderr_tail": "make: *** [all] Error 1\nmake: Leaving directory"})
    assert got.startswith("make: *** [all] Error 1")


def test_error_field_fallback_and_empty():
    assert _short_error({"error": "工具层错误：参数不合法\n\n改法：…"}) == "工具层错误：参数不合法"
    assert _short_error({}) == "(no stderr/stdout tail)"

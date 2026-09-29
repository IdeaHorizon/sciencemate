"""失败事件要带上工具自己写的"下一步该干什么"。

显示层（`payload-view.ts` → `tool-presentation.ts`）一直在读 `error.recovery`，
但这一跳以前只输出 `errorCode` / `errorMessage` —— **读端有、写端零**。于是每
一类失败都退回按工具类型猜的兜底句，而对 compile_latex 那句兜底是 "Review the
document source"。2026-09-09 现场：稿子一个字没错，缺的是 latexmk，界面照那句
话说了三遍，模型据此查了三轮源码。

配套：`tests/test_recovery_reaches_the_person.py`（harness 侧的截断与字段名对齐）。
"""
from __future__ import annotations

from app.services.execution_ingest import _tool_failure_fields


def _failure(**extra) -> dict:
    return {
        "status": "error",
        "error_code": "toolchain_missing",
        "error": "这台机器上没有 LaTeX 工具链",
        "recovery": "在跑 harness 的机器上装 texlive，或把 tectonic 放进 PATH。",
        **extra,
    }


def test_the_event_carries_the_next_step() -> None:
    fields = _tool_failure_fields({}, _failure())
    assert fields["errorCode"] == "toolchain_missing"
    assert "texlive" in fields["errorRecovery"], \
        "事件里没带 recovery —— 界面只能退回按工具类型猜的兜底文案"


def test_recovery_survives_when_the_body_was_truncated() -> None:
    """harness 超 500 字节会把结果压成 envelope + `_body_truncated`。recovery 在
    envelope 白名单里，所以这一跳照样拿得到 —— 编译失败的返回值必然走这条路。"""
    truncated = _failure(_body_truncated='{"stdout_tail": "xxx"...[truncated]')
    assert "texlive" in _tool_failure_fields({}, truncated)["errorRecovery"]


def test_a_tool_that_says_nothing_adds_no_empty_field() -> None:
    """没写 recovery 的工具照旧走兜底，别塞一个空串把兜底顶掉。"""
    assert "errorRecovery" not in _tool_failure_fields(
        {}, {"status": "error", "error": "boom", "recovery": "   "})
    assert "errorRecovery" not in _tool_failure_fields(
        {}, {"status": "error", "error": "boom"})


def test_recovery_is_read_from_the_raw_event_too() -> None:
    """结果被截成**字符串**时（老 transcript 回放），recovery 只可能在 raw 上。"""
    fields = _tool_failure_fields(
        {"recovery": "装 texlive"}, '{"status": "error", "error": "x"...[truncated]')
    assert fields["errorRecovery"] == "装 texlive"

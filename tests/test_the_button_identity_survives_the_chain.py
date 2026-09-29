"""人点的那个按钮的身份，必须原样穿过整条 pause 链。

## 病例（2026-08-22，E2E v27）

决策卡片反复回来，点了像没反应。事件流里是 `decision_answer_rejected /
unrecognized_answer`，`answer_preview` 里是我写的那段中文附言 —— 而
`legal_choice_ids` 是 `[proceed, revise, redirect_upstream, abort, edit]`。
也就是说：**结构化的 `choice_id` 根本没到判定层**，只剩附言文本去撞合法集。

链路两端都是对的：前端 `HumanInputPrompt` 把 `{offer_id, choice_id}` 与附言
分开发；`platform_runtime.one_answer` 也正确构造了那个 dict；后端
`resolve_answer` 拿到 dict 就做集合成员判定。断在中间：

    core/pause_driver.resolve_pause_answer   →  (await manual_ask(...) or "").strip()
    core/pause_driver._normalize_decision_override → (answer or "").strip()

`dict` 没有 `.strip()`，于是当场 `AttributeError`；异常被
`_settle_decision_answer` 的 `except Exception` 兜住 → 记成"答复没被受理" →
原样重呈递。人看到的就是「点了没反应，卡片又回来了」。

**只选选项不写附言时反而能过** —— 压出来的字符串恰好等于选项 label，被兼容层
的精确匹配救了。所以这个 bug 只在"既选选项又写附言"时显形，最难被测试覆盖到
的那种组合。

## 判据

`AskFn` 的返回值有两种合法形态（str / dict），整条链必须两种都能透传。
凡是无条件 `.strip()` 的地方，都是在悄悄抹掉按钮身份 —— 抹掉之后没人报错，
只是退化成拿文案去撞合法集（2026-08-19 静默丢弃事故的同一个引擎）。
"""
from __future__ import annotations

import asyncio
import importlib
import os

import pytest

from core.pause import PauseEvent

STRUCTURED = {"offer_id": "of_1", "choice_id": "proceed", "note": "附言：直接出 PDF"}


def _decision_pause() -> PauseEvent:
    return PauseEvent.from_payload({
        "question": "Post-node decision for hypothesis",
        "options": ["PROCEED to next stage", "REVISE (re-run source_node)"],
        "metadata": {"type": "decision_package", "recommended_option_index": 0},
        "pending_tool_call_id": "p1",
    })


def _reloaded_driver(monkeypatch, *, auto_approve: str):
    """auto-approve 的开关在 import 时求值，所以要重载模块。"""
    monkeypatch.setenv("HARNESS_AUTO_APPROVE", auto_approve)
    import core.pause_driver as pd

    return importlib.reload(pd)


@pytest.mark.parametrize("auto_approve", ["0", "1"])
def test_a_structured_answer_survives(monkeypatch, auto_approve: str) -> None:
    """两条分支都要透传 —— 人点按钮的路径不该因为 auto-approve 开没开而不同。"""
    pd = _reloaded_driver(monkeypatch, auto_approve=auto_approve)

    async def ask(_event):
        return dict(STRUCTURED)

    out = asyncio.run(pd.resolve_pause_answer(_decision_pause(), ask))

    assert isinstance(out, dict), (
        f"按钮身份被压成了 {type(out).__name__} —— 判定层只能拿文案去撞合法集，"
        f"选项集一变就静默丢弃人的授权"
    )
    assert out["choice_id"] == "proceed"
    assert out["note"] == STRUCTURED["note"], "附言不该在传递中丢掉"


def test_plain_text_answers_still_work(monkeypatch) -> None:
    """兼容层不能被顺手改坏：stdin / 老前端回传的仍是纯文本。"""
    pd = _reloaded_driver(monkeypatch, auto_approve="0")

    async def ask(_event):
        return "  1  "

    out = asyncio.run(pd.resolve_pause_answer(_decision_pause(), ask))
    assert out == "1", "纯文本答复要照旧去空白后透传"


def test_skip_override_still_maps_to_abort(monkeypatch) -> None:
    """`skip` 这个特殊改写只对文本生效，且必须还在。"""
    pd = _reloaded_driver(monkeypatch, auto_approve="0")
    options = ["PROCEED to next stage", "ABORT pipeline"]

    assert pd._normalize_decision_override("skip", options) == "2"
    # 结构化答复没有可归一化的东西，原样返回（而不是炸在 .strip() 上）。
    assert pd._normalize_decision_override(dict(STRUCTURED), options) == STRUCTURED

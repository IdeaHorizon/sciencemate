"""状态词跟着这个人的界面语言走。

「界面语言」这个开关 2026-09-15 之前只驱动资讯流，切了工作区一个字都不变。
我第一版的处理是把开关删掉 —— wangd：「还应该保留这个按钮，只是让它真的有用
才行啊。你删了按钮这不是糊弄吗」。

界面上的字有一部分不在前端：执行状态（已完成 / 正在跑 / 等你回答）是后端
`execution_view` 算好交出去的**成品**。它要是不跟着语言走，英文界面上就会
冒出一句中文，而前端那边怎么翻都没用。

判据落在**交出去的那个字**上，不落在"有没有传 lang"这种写法上。
"""
from __future__ import annotations

import pytest

from app.services import execution_view


@pytest.mark.parametrize(
    "phase,outcome,waiting,zh,en",
    [
        ("ended", "ok", None, "已完成", "Completed"),
        ("ended", "failed", None, "失败", "Failed"),
        ("interrupted", None, None, "中断", "Interrupted"),
        ("alive", None, None, "正在跑", "Running"),
        ("alive", None, {"kind": "human"}, "等你回答", "Needs your answer"),
        ("alive", None, {"kind": "permission"}, "等你授权", "Needs your approval"),
        ("alive", None, {"kind": "compute"}, "等算力", "Waiting for compute"),
    ],
)
def test_every_status_word_has_both_languages(phase, outcome, waiting, zh, en) -> None:
    assert execution_view._label(phase, outcome, waiting, "zh") == zh
    assert execution_view._label(phase, outcome, waiting, "en") == en


def test_an_unknown_language_falls_back_to_chinese_instead_of_blowing_up() -> None:
    """少一种翻译不该让一个 API 响应拼不出来 —— 退回中文，但别报错。"""
    assert execution_view._label("ended", "ok", None, "fr") == "已完成"


def test_the_default_is_chinese_so_a_caller_that_forgets_still_gets_words() -> None:
    # 忘了传 lang 的调用点会得到中文，而不是一个 key 或者空串。
    assert execution_view._label("alive", None, None) == "正在跑"

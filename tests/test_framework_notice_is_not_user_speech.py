"""框架注入的提示不是用户发言 —— 任何问"用户说了什么"的地方都不许把它算进去。

背景：为了不让模型复述注入内容，框架提示走的是 **role="user"** 信封
（`loop_hooks._as_framework_notices`，PR#462）。信封正文明写
「（研究框架自动注入的状态与提示，不是用户发言）」——
**散文说不是，结构说是**。于是每个按 `role == "user"` 取用户原话的消费者都会捡到它。

2026-08-31 本机跑真课题实测到的后果：hypothesis 节点把框架的 curator 维护提醒
（`💤 dreaming_due`、"Mode 2 是 KB 长期质量的保证"）解析成了**用户的 locked_definitions**，
definition_fidelity 于是报「prereg 缺失用户类别: dreaming_due / Mode 2」，
把一条与课题毫无关系的框架提示要求写进科研预注册的 definition_lock 段。

判别器 `is_framework_notice` 早就存在 —— 缺的是**接线**。这里锁住框架侧两处；
`nodes/{hypothesis,experiment,data}` 里还有 4 处同形，属各自 owner。
"""
from __future__ import annotations

from core.llm import LLMMessage, framework_notice


def _mixed_messages() -> list[LLMMessage]:
    return [
        LLMMessage(role="system", content="sys"),
        LLMMessage(role="user", content="我要研究非正规动力学下的 EWS 假阳性。"),
        framework_notice("💤 dreaming_due —— Mode 2 是 KB 长期质量的保证。"),
        LLMMessage(role="assistant", content="收到。"),
        LLMMessage(role="user", content="对照组必须跟非正规组同维。"),
    ]


def test_user_utterances_exclude_framework_notices():
    """宪法的防伪依据里混进框架提示 = 模型能把框架的话洗成用户立的铁律。"""
    messages = _mixed_messages()
    # 复刻 agent_loop 里那一句的判据（同一个过滤条件）
    from core.llm import is_framework_notice

    utterances = [
        m.content for m in messages
        if getattr(m, "role", "") == "user"
        and (m.content or "").strip()
        and not is_framework_notice(m)
    ]
    assert len(utterances) == 2, "只有两句是人说的"
    joined = " ".join(utterances)
    assert "dreaming_due" not in joined, (
        "框架自动提示被算成了用户原话 —— memory_write 会拿它当引文核对通过"
    )
    assert "非正规动力学" in joined and "同维" in joined


def test_agent_loop_actually_applies_that_filter():
    """走真代码：agent_loop 里那一处必须真的过滤，不是测试自己复刻了一遍。"""
    import inspect

    from core import agent_loop

    src = inspect.getsource(agent_loop)
    marker = '_user_utterances'
    idx = src.index(marker)
    window = src[idx: idx + 400]
    assert "is_framework_notice" in window, (
        "agent_loop 组装 _user_utterances 时没有排除 framework-notice"
    )


def test_summary_does_not_label_a_notice_as_user():
    """摘要会被反复喂回模型；把框架的话标成 [user] 会一路传下去。"""
    from core.summarizer import _render_messages_for_compression as render  # type: ignore[attr-defined]

    text = render(_mixed_messages())
    assert "[framework-notice]:" in text, "框架提示在摘要里仍被标成用户发言"
    assert "[user]: 💤" not in text

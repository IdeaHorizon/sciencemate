"""上一个会话说的话，不是这一轮用户交代的事（#974）。

## 现场

同一 project 下两个独立会话：A 里用户写了「交给 Experiment 节点完成」；B 里用户只给
了一个自然业务任务，全文没有 `Experiment` 这个词。B 的父 project_chat 却说：

> 用户明确点名「交给 Experiment 节点完成」——虽然这是上一轮消息的表述……

然后据此创建 Experiment child。

## 根因

`research_intake.json` 落在 **project_root**，被逐字注入同项目的**每一个**会话，
而每一段都不带来源会话。于是 A 里的那句话在 B 的上下文里读起来和当前用户的话
一模一样 —— 模型没有任何办法分辨，它不是在编，是在读框架递给它的东西。

新会话因此不是隔离的决策上下文：旧的节点点名、授权、资源上限都可能被续用。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from core.research_intake import record_intake, render_intake_section


def test_another_sessions_text_is_labelled_as_such(tmp_path: Path) -> None:
    record_intake(tmp_path, "把这项只读环境审查明确交给 Experiment 节点完成",
                  source="user", session_id="session-A")

    section = render_intake_section(tmp_path, current_session_id="session-B")

    assert "另一个会话" in section, f"上一个会话的话读起来像当前用户刚说的：\n{section}"
    assert "不构成本轮授权" in section
    assert "内部路由指令" in section, "验收 3：节点点名不能从另一 session 继承"


def test_this_sessions_text_is_not_demoted(tmp_path: Path) -> None:
    """对照：本会话自己的原话照旧是权威 —— 这条修复不能把它也推远。"""
    record_intake(tmp_path, "准备 CPython 3.12.10 离线审查材料",
                  source="user", session_id="session-B")

    section = render_intake_section(tmp_path, current_session_id="session-B")

    assert "本会话" in section
    assert "另一个会话" not in section
    assert "不构成本轮授权" not in section, "本会话自己的话被当成了项目背景"


def test_amendments_carry_their_own_origin(tmp_path: Path) -> None:
    """后续指令与决策附言各有各的来源 —— 不能跟着原始输入一起算。"""
    record_intake(tmp_path, "研究英国饮食声誉", source="user", session_id="session-A")
    record_intake(tmp_path, "这一步交给 Experiment 节点", source="user",
                  session_id="session-A")
    record_intake(tmp_path, "顺便看看近三年的数据", source="user",
                  session_id="session-B")

    section = render_intake_section(tmp_path, current_session_id="session-B")

    lines = section.splitlines()
    a_line = next(l for l in lines if "#1" in l)
    b_line = next(l for l in lines if "#2" in l)
    assert "另一个会话" in a_line, a_line
    assert "本会话" in b_line and "另一个会话" not in b_line, b_line


def test_a_legacy_record_is_not_claimed_as_this_session(tmp_path: Path) -> None:
    """老记录没有来源会话 —— 如实说不知道，不冒充本会话的。"""
    record_intake(tmp_path, "老项目的原始输入", source="user")

    section = render_intake_section(tmp_path, current_session_id="session-B")
    assert "来源会话不详" in section
    assert "不构成本轮授权" in section


def test_without_a_session_identity_nothing_changes(tmp_path: Path) -> None:
    """CLI / 教学版没有会话概念 —— 分不出来就别加一段吓人的警告。"""
    record_intake(tmp_path, "研究英国饮食声誉", source="user")

    section = render_intake_section(tmp_path)
    assert "不构成本轮授权" not in section
    assert "研究英国饮食声誉" in section


def test_the_context_engine_passes_the_current_session(tmp_path: Path) -> None:
    """标签要真的到得了节点：context_engine 得把这一轮的会话传下去。"""
    import inspect

    import core.context_engine as ce

    src = inspect.getsource(ce)
    at = src.index("render_intake_section(")
    assert "current_session_id" in src[at:at + 300], (
        "渲染时不知道这一轮是哪个会话 —— 那就永远标不出来源")

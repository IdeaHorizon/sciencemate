"""同一个暂停有两条事件，要取**信息最全**的那条，不是最新的那条。

## 现场（2026-08-11 实测）

一次高危审批在库里落了两条 `run.paused`，相差 55 毫秒：

    00:53:30.185  bare  {"reason": "waiting_human", "prompt": "…"}
    00:53:30.240  rich  {"reason": "waiting_permission",
                         "context": "工具：submit_job / 命中类别：真实外部作业提交
                                     command=/opt/homebrew/bin/lmp_serial …",
                         "options": ["批准执行", "拒绝"]}

取"最新一条"是在**赌 ingest 顺序**。读早了就拿到空的 —— 我实测拿到过
`选项: []` / `提问节点: None`，也就是一个**没有命令、没有选项**的审批框。

而 #370 整件事就是为了让人**看得见在批什么**。看不见内容的审批等于没有审批。

## 判据

在**同一个未答复的暂停**内（往回扫到遇上 `run.resumed` 为止）挑 payload
字段最多的那条。不会把上一次已经答过的问题翻出来。
"""
from __future__ import annotations

import inspect

from app.services import sessions


def _source() -> str:
    return inspect.getsource(sessions._pending_approval)


def test_it_does_not_just_take_the_latest() -> None:
    source = _source()
    assert ".limit(1)" not in source, "取最新一条 = 赌 ingest 顺序"
    assert "max(" in source and "open_pauses" in source


def test_it_stops_at_the_last_resume() -> None:
    """只在同一个未答复的暂停内挑 —— 否则会把上次答过的问题再问一遍。"""
    source = _source()
    assert 'if item.kind != "run.paused":' in source
    assert "break" in source


def test_the_verbatim_command_is_what_matters() -> None:
    """`context` 是逐字待执行内容。它丢了，审批框就退化成确定/取消。"""
    source = _source()
    assert '"context": payload.get("context")' in source

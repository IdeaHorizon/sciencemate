"""Autonomous 必须同时意味着"自己往下推"，不只是"自动批准"。

实测（2026-08-10）：`run_unattended` 在 harness 侧早就写好，但既没进 `--serve`
的 op 分发表，后端也从来没调过——**平台从来没有过无人值守驱动**。UI 上的
Autonomous 只接通了 `HARNESS_AUTO_APPROVE`（自动批准），没接通续轮。于是
"点一次跑一夜"做不到，每一轮都要外部脚本推；而那个外挂脚本按 DB 缓存态判断，
误标过正在干活的 run（2026-08-09 实测）。

这组测试钉住接线本身，不测 harness 内部的循环逻辑（那在 harness 仓
tests/test_unattended_emits_one_terminal.py）。
"""
from __future__ import annotations

import inspect

from app.services import harness_sessions


def _session_turn_source() -> str:
    return inspect.getsource(harness_sessions._ProjectHarnessSession.turn)


def _manager_turn_source() -> str:
    return inspect.getsource(harness_sessions.HarnessSessionManager.turn)


def test_autonomous_is_forwarded_as_unattended() -> None:
    """manager 收到的 autonomous 必须一路走到会话级 turn。

    以前 `autonomous` 只到 `_get_or_create`（用来设子进程环境变量），
    `session.turn(...)` 那一行完全看不到它 —— 半截线。
    """
    source = _manager_turn_source()
    assert "unattended=autonomous" in source, (
        "autonomous 必须透传成 unattended，否则 UI 上的自主模式只是自动批准"
    )


def test_unattended_uses_the_run_unattended_op() -> None:
    source = _session_turn_source()
    assert '"op": "run_unattended"' in source
    assert '"op": "turn"' in source, "非自主路径必须保持原样，别把交互式也改掉"


def test_unattended_carries_a_turn_bound() -> None:
    """有界是这个循环能存在的前提：为了'能自己跑完'写的循环，自己变成
    无限循环就全白做。"""
    source = _session_turn_source()
    assert "max_turns" in source
    signature = inspect.signature(harness_sessions._ProjectHarnessSession.turn)
    assert signature.parameters["max_turns"].default == 200


def test_interactive_path_is_unchanged_by_default() -> None:
    """默认必须还是单轮：交互式聊天不该突然变成会自己跑一夜的东西。"""
    signature = inspect.signature(harness_sessions._ProjectHarnessSession.turn)
    assert signature.parameters["unattended"].default is False

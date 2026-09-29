"""一条 run 死了，服务端必须留得下追查的线索。

## 现场（2026-08-18）

一条 experiment run 把英国饮食课题的全部裁决做完（239 万 tokens），在最后一步
失败。用户看到的是兜底文案「这一轮没能完成 / 平台在记录这次运行时撞上了内部
错误」，而服务端日志里**一个字都没有** —— `local_execution` 这个模块当时连
logger 都没有。事后翻日志、翻事件表、翻 run 记录，都定位不到是哪一步炸的。

文案表里那句「完整技术细节挂在下面那个 reference 上」因此是空头支票：
reference 就是 run_id，可日志里根本没有以它为线索的任何一行。

## 判据

翻译成人话是给**用户**的，技术真相要同时留给运维 —— 这是两个受众，不是一件
事的两种说法。所以这里验的不是"文案对不对"（那有别的测试），而是**失败时到底
有没有落日志、日志里有没有 run_id**。
"""
from __future__ import annotations

import logging

from app.services.local_execution import _sanitized_platform_failure


class _Boom(RuntimeError):
    pass


def test_an_unexpected_failure_is_logged_with_its_run_id(caplog):
    with caplog.at_level(logging.ERROR, logger="app.services.local_execution"):
        record = _sanitized_platform_failure(_Boom("inner detail"), run_id="run_abc123")

    assert record["reference"] == "run_abc123"
    logged = [r for r in caplog.records if r.name == "app.services.local_execution"]
    assert logged, "失败没有落任何服务端日志 —— 这条 run 事后无从追查"
    text = " ".join(r.getMessage() for r in logged)
    assert "run_abc123" in text, "日志里没有 run_id，reference 对不上任何一行"
    assert "_Boom" in text, "日志里没有异常类型"
    assert any(r.exc_info for r in logged), "没有栈，只知道炸了、不知道在哪炸"


def test_the_user_facing_copy_still_never_leaks_the_exception_text(caplog):
    """留日志不等于把异常原文倒给用户 —— 那是这个函数原本就守住的边界。"""
    with caplog.at_level(logging.ERROR, logger="app.services.local_execution"):
        record = _sanitized_platform_failure(_Boom("SELECT * FROM secrets"), run_id="run_x")

    assert "SELECT * FROM secrets" not in record["title"]
    assert "SELECT * FROM secrets" not in record["body"]
    assert "SELECT * FROM secrets" not in record["message"]


def test_our_own_restart_is_recorded_without_a_stack(caplog):
    """自己重启打断的不是故障：留一行可检索的记录，但别刷栈。

    否则每次部署都会在日志里堆一片红色的假故障，真故障反而被淹掉。
    """
    from app.services.harness_sessions import HarnessSessionStaleError

    del HarnessSessionStaleError  # 只是确认模块可导入，不参与判据

    class _Shutdown(RuntimeError):
        pass

    import app.services.local_execution as le

    original = le.interrupted_by_our_own_shutdown
    le.interrupted_by_our_own_shutdown = lambda exc: True
    try:
        with caplog.at_level(logging.INFO, logger="app.services.local_execution"):
            record = le._sanitized_platform_failure(_Shutdown("-15"), run_id="run_restart")
    finally:
        le.interrupted_by_our_own_shutdown = original

    assert record["code"] == "app_server_restarted"
    logged = [r for r in caplog.records if r.name == "app.services.local_execution"]
    assert logged, "重启打断也要留一行，否则没人知道这轮为什么停了"
    assert "run_restart" in " ".join(r.getMessage() for r in logged)
    assert not any(r.exc_info for r in logged), "重启不是故障，不该刷栈"

"""被平台重启打断的 run，其原因必须到达用户可见的 `failure` —— 现算叠加，不落盘。

## 现场（wangd 2026-08-24）

node20 重部署把一条 hypothesis run 的 worker SIGKILL 了。后端在启动见证里明明
写下了 `staleReason="app_server_restart"`（`mark_orphaned_harness_runs`），但用户
在 UI 上看到的是"这一轮没跑完 / Status unknown"——因为前端读的是另一个字段
`summary.failure`，而见证只写了 `staleReason`。同一个问题两个字段、从不相遇，于是
掉进通用兜底。

## 判据

读路径（`_run_response`）现算出 `stale_unknown` 的**同一刻**，就该把见证里的
`app_server_restart` 翻成那条早已写好的 `app_server_restarted` 文案，叠加进
`summary.failure`。不落盘（D11：见证可持久化、判决不可以），所以只在现算为
stale 时叠加；run 一旦续上（observed 变回 running），叠加自然消失。
"""
from __future__ import annotations

from types import SimpleNamespace

from app.api.v1.execution import _observed_summary
from app.models.execution import RunStatus

STALE = RunStatus.STALE_UNKNOWN.value


def _run(summary):
    return SimpleNamespace(id="run_x", summary=summary)


def test_restart_witness_becomes_user_facing_failure_when_stale():
    run = _run({"staleReason": "app_server_restart", "resumable": False})
    out = _observed_summary(run, STALE)
    assert isinstance(out, dict)
    failure = out.get("failure")
    assert isinstance(failure, dict), "现算 stale + 重启见证 → 必须叠加 failure"
    assert failure.get("code") == "app_server_restarted"
    # 文案取自 run_failures 的表，不是这里现编 —— 单一真相源。
    assert "重启" in failure.get("title", "")
    assert failure.get("recovery"), "要告诉用户怎么接着跑"
    # 见证本身原样保留：叠加是补一层说法，不是替换事实。
    assert out.get("staleReason") == "app_server_restart"


def test_no_overlay_when_not_stale():
    # 关键变异：run 还在跑（或已 running 续上），绝不能显示"被重启打断"。
    run = _run({"staleReason": "app_server_restart"})
    out = _observed_summary(run, RunStatus.RUNNING.value)
    assert out == {"staleReason": "app_server_restart"}
    assert "failure" not in out


def test_no_overlay_for_other_stale_reasons():
    # stale 但不是重启造成的 —— 这里不越俎代庖编一个具体原因。
    run = _run({"staleReason": "something_else"})
    out = _observed_summary(run, STALE)
    assert "failure" not in out


def test_existing_failure_is_not_clobbered():
    # 进程内那条路（优雅关闭当场 describe）已写了更权威的 failure，别盖。
    original = {"code": "upstream_rejected", "title": "模型服务拒绝了这次调用"}
    run = _run({"staleReason": "app_server_restart", "failure": original})
    out = _observed_summary(run, STALE)
    assert out["failure"] is original


def test_non_dict_summary_passes_through():
    run = _run(None)
    assert _observed_summary(run, STALE) is None

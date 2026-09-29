"""守卫拦截成功 ≠ 这次研究失败了。

## 现场（2026-08-13 E2E，writing 派图测试）

一个 run 走完了全部流程：curator dreaming → project_synthesis(ready_to_write)
→ writing（产出 5 页 PDF + 自派 postprocess 出两张图）→ 下游 reviewer 给
APPROVE 4/5 → curator integration → `present_decision_package` → 正常 pause
等人决策。

账本上是这样收尾的：

    05:48:50.951  run.paused    {"reason": "waiting_human"}      ← 正确终态
    05:48:50.998  step.started  "project_chat activity"          ← 47ms 后
    05:48:51.001  step.failed   "…platform hit an internal error…"
    05:48:51.007  run.failed

harness 侧一点错没有：pause 带合法 `pending_tool_call_id`，`turn` 如实返回
`status: "paused"`，transcript 到此为止。之后有别的东西又推了一次 turn，被
`platform_runtime` 的守卫按 `RequestError("pause_pending", …)` 拦下 —— **拦截
成功**。代价却是整个已产出、已过审的 run 被判死。

## 根因不在文案表

`platform_runtime` 把 `code` / `error_type` / `details` 都发上了线，而
`_rpc_locked` 只取 `message`。`RequestError` 自称 "a stable, caller-actionable
request validation failure"，那个稳定 code 在边界上被丢掉了 —— 于是
`run_failures._classify`（它第一件事就是读 `exc.code`）手里只剩一句英文散文，
只能选兜底那条"平台内部错误 / 请重新发送"。

三样全错：不是内部错误（是流程重入），活儿没丢（已产出且过审），重发只会重跑。

## 这些测试守的是什么

1. 契约要送到调用方：harness 声明的 code 必须活着穿过 RPC 边界。
2. `pause_pending` 不是失败：终态是它本来就在的 `waiting_human`。
3. 别把两条路一起堵死：还能答复的 pause，`resumable` 必须为真 —— 否则
   answer 被判 stale、turn 被判 pause_pending（见
   `_validate_resume_binding` 里那段 2026-08-09 的普查记录）。
"""
from __future__ import annotations

from pathlib import Path

from app.models.execution import RunStatus
from app.services import local_execution
from app.services import run_failures
from app.services.harness_sessions import (
    HarnessSessionError,
    HarnessSessionProcessError,
)
from app.services.local_execution import terminal_state_for


def _pause_pending() -> HarnessSessionError:
    """A rejection exactly as the boundary now constructs it."""
    return HarnessSessionError(
        "answer the pending pause before starting another turn",
        code="pause_pending",
        error_type="RequestError",
        details={"request_id": "turn-07ef6a0f"},
    )


def test_harness_error_code_survives_the_rpc_boundary() -> None:
    exc = _pause_pending()
    assert exc.code == "pause_pending"
    assert exc.error_type == "RequestError"
    assert exc.details == {"request_id": "turn-07ef6a0f"}


def test_a_bare_error_keeps_its_class_identity() -> None:
    """构造函数不许把子类自带的 code 冲成空。"""
    assert HarnessSessionProcessError("gone", exit_code=-15).code == "harness_process_exited"
    assert HarnessSessionError("no code declared").code == ""


def test_pause_rejection_is_not_a_failed_run() -> None:
    status, stale_reason = terminal_state_for(_pause_pending())
    assert status == RunStatus.WAITING_HUMAN.value
    assert stale_reason is None


def test_other_harness_errors_still_fail() -> None:
    """只豁免这一个 code —— 别的 RPC 故障照旧是失败。"""
    status, _ = terminal_state_for(HarnessSessionError("boom", code="invalid_request"))
    assert status == RunStatus.FAILED.value


def test_pause_rejection_gets_its_own_user_copy() -> None:
    failure = run_failures.describe(_pause_pending(), reference="run_x").as_record()
    assert failure["code"] == "pause_pending"
    # 正文不得再声称"内部错误"，也不得建议重发。
    generic = run_failures.describe(Exception("anything"), reference="run_x").as_record()
    assert failure["title"] != generic["title"]
    assert failure["body"] != generic["body"]
    assert "again" not in failure["recovery"].lower()
    assert failure["retryable"] is False


def test_the_recording_paths_derive_resumable_instead_of_hardcoding_false() -> None:
    """`resumable` / attempt 关闭 / step 结论都必须跟着那处唯一的终态判断走。

    写死 False 会造出 `_validate_resume_binding` 里那个自相矛盾的状态：run 停在
    waiting_*，answer 因 "not safely resumable" 被拒，turn 因 pause_pending 被拒
    —— 两条路同时堵死。这里守的是"别再写死"，所以按源码断言：异常收尾那两段
    必须出现从终态推导的判据。

    2026-08-27 更新：`resumable` **不再写进 run.summary**（那是一条关于未来的
    落盘判决，账面判决会自我加固）。still_answerable 仍然存在，但只作为这一趟
    收尾里的现算局部量，决定要不要关 attempt —— 判据因此从"有没有写对"改成
    "有没有还在写"。
    """
    source = Path(local_execution.__file__).read_text(encoding="utf-8")
    # 异常收尾块从 `terminal_state_for(exc)` 开始，到函数结束。
    tail = source.split("terminal_status, stale_reason = terminal_state_for(exc)", 1)
    assert len(tail) == 2, "异常收尾块的锚点变了，这个测试需要跟着改"
    body = tail[1]
    summary_writes = [
        chunk for chunk in body.split("run.summary = {")[1:]
    ]
    for chunk in summary_writes:
        assert '"resumable"' not in chunk.split("}")[0], (
            "run.summary 又开始存 resumable 了 —— 那是关于未来的判决，不是事实"
        )
    assert "still_answerable = terminal_status" in body, "现算的可答复判断没了"
    assert "if attempt and not still_answerable:" in body, "还能答复的 pause 不该关掉 attempt"
    assert 'if step_failed else "paused"' in body, "step 结论又写死成 failed 了"

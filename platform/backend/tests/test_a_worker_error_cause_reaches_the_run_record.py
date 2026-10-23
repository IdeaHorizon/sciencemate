"""窄单 E #1220：worker 错误事件的 `cause`（底层异常原文）穿过 RPC 边界进 run 记录。

worker 那一半在框架侧 `tests/test_a_worker_error_carries_its_cause.py`。这里守后端两跳：
1. 控制通道的读者把事件里的 `cause` 交给 `HarnessSessionError`（和 `code` 同一次构造）；
2. `run_failures.describe` 把它放进失败记录的 `cause`，过同一道清洗、同一个上限，只进技术细节。
"""
from __future__ import annotations

import ast
from pathlib import Path

from app.services import run_failures
from app.services.harness_sessions import HarnessSessionError

HARNESS_SESSIONS = Path(__file__).resolve().parents[1] / "app" / "services" / "harness_sessions.py"


def test_the_failure_record_keeps_the_cause_out_of_the_prose() -> None:
    exc = HarnessSessionError(
        "cannot read the state directory",
        code="state_dir_unavailable",
        error_type="RequestError",
        cause=r"PermissionError: [WinError 5] 拒绝访问。: 'C:\\Users\\x\\afs\\project-worktrees\\a\\b\\.research\\runtime\\runs'",
    )
    record = run_failures.describe(exc, reference="run-1").as_record()
    assert "WinError 5" in record["cause"] and "runtime" in record["cause"]
    assert "WinError" not in record["body"] and "WinError" not in record["title"]


def test_no_cause_means_no_field() -> None:
    record = run_failures.describe(HarnessSessionError("x", code="runtime_error")).as_record()
    assert "cause" not in record


def test_a_huge_cause_is_capped() -> None:
    record = run_failures.describe(HarnessSessionError("x", code="runtime_error", cause="y" * 100_000)).as_record()
    assert len(record["cause"]) < 100_000


def _calls_building_the_error_from_an_event(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "HarnessSessionError":
            kws = {kw.arg: ast.unparse(kw.value) for kw in node.keywords if kw.arg}
            if "event.get(\"code\")" in kws.get("code", "") or "event.get('code')" in kws.get("code", ""):
                yield kws


def test_the_reader_hands_the_event_cause_to_the_error() -> None:
    tree = ast.parse(HARNESS_SESSIONS.read_text(encoding="utf-8"))
    sites = list(_calls_building_the_error_from_an_event(tree))
    assert sites, "找不到从 error 事件构造 HarnessSessionError 的那一处 —— 读者改了写法，这条要跟着改"
    for kws in sites:
        assert "cause" in kws and "event.get(\"cause\")" in kws["cause"].replace("'", '"'), kws

"""窄单 E #1220：worker 的错误事件带底层异常原文（`cause`）。

lujy 10-15 的现场里，用户手里只有「这一轮没能完成」和一个 reference；`[WinError 5]` 与路径
只在 worker 的 stderr 里。错误事件的 `message` 是我们自己的话，`cause` 是实际发生了什么。

- 意外异常：`cause` 是它的类型 + 原文（errno + 路径；OSError 的 repr 会丢路径，所以不用 repr）。
- `RequestError` 链着底层异常（`raise … from exc`）：`cause` 是被链着的那个。
- `RequestError` 什么也没链着：没有底层原因，事件里就没有 `cause`，不编一个。
"""
from __future__ import annotations

import io
import json

import pytest

from core.pause import clear_all
from platform_runtime import RequestError, _cause_of, serve_jsonl


@pytest.fixture(autouse=True)
def _clear():
    clear_all()
    yield
    clear_all()


def test_an_unexpected_exception_is_its_own_cause():
    exc = PermissionError(13, "Access is denied", r"C:\data\harness-state\users\u\.platform-tenant-identity.json")
    cause = _cause_of(exc)
    assert "PermissionError" in cause and "Access is denied" in cause and ".platform-tenant-identity.json" in cause


def test_a_request_error_reports_what_it_was_raised_from():
    try:
        try:
            raise OSError(5, "拒绝访问。", r"C:\data\runs")
        except OSError as inner:
            raise RequestError("state_dir_unavailable", "cannot open the state directory") from inner
    except RequestError as exc:
        cause = _cause_of(exc)
    assert cause.startswith("OSError: ") and "拒绝访问" in cause and "data" in cause and "runs" in cause  # 路径按 repr 引号（反斜杠成双）


def test_a_bare_request_error_has_no_cause():
    assert _cause_of(RequestError("invalid_message", "message must be a non-empty string")) == ""
    assert _cause_of(None) == ""


def test_a_huge_cause_is_capped():
    assert len(_cause_of(RuntimeError("x" * 50_000))) < 2100


class _DeniedLLM:
    stream_display = None

    async def chat(self, messages, **kwargs):
        raise PermissionError(13, "Access is denied", "/data/project-worktrees/p/s/.research/runtime/runs")


@pytest.mark.asyncio
async def test_the_error_event_on_the_wire_carries_the_cause(tmp_path):
    requests = [
        {"op": "init", "request_id": "init-cause", "tenant_id": "tenant-test",
         "project_id": "project-cause", "session_id": "session-cause",
         "home_dir": str(tmp_path / "isolated-home")},
        {"op": "turn", "request_id": "bad-turn", "message": "   "},
        {"op": "turn", "request_id": "denied-turn", "message": "开始"},
    ]
    events: list[dict] = []
    await serve_jsonl(
        io.StringIO("".join(json.dumps(r) + "\n" for r in requests)),
        lambda event_type, **payload: events.append({"type": event_type, **payload}),
        llm=_DeniedLLM(),
    )
    errors = {e["request_id"]: e for e in events if e["type"] == "error"}
    assert errors["bad-turn"]["code"] == "invalid_message"
    assert "cause" not in errors["bad-turn"], "没链着底层异常的 RequestError 不许编一个 cause"
    denied = errors["denied-turn"]
    assert "PermissionError" in denied["cause"] and "runtime/runs" in denied["cause"], denied

"""Executor catch-all: run_loop 抛异常时仍写 status='error' summary.json。

同事反馈：6/11 那次 long-run 撞到 LLM provider RemoteProtocolError，
core/llm.py 的 retry 已经在 v2.x 加了但 3 次都失败，异常冒泡出 execute_node，
**summary.json 永远不写**，事后只能靠 transcript 拼现场。

这条 test gate executor 现在会捕获异常 → 写 status='error' summary →
re-raise（保留旧的 exception 语义，run_node.py 仍非零退出）。
"""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from core.bootstrap import bootstrap
from core.executor import execute_node
from core.harness import NodeHarness
from core.llm import LLMResponse
from core.pause import clear_all, list_active_runs


@pytest.fixture(autouse=True)
def _setup():
    bootstrap()
    yield
    clear_all()


def _minimal_harness() -> NodeHarness:
    return NodeHarness(
        node_type="literature",
        system_prompt="test",
        tools=[],
        max_turns=5,
        required_outputs=[],
        required_output_artifact_types=[],
    )


class _AlwaysFailLLM:
    """每次 chat 都抛指定 exception。"""
    def __init__(self, exc: Exception) -> None:
        self.exc = exc
        self.calls = 0

    async def chat(self, messages, **kw):
        self.calls += 1
        raise self.exc


@pytest.mark.asyncio
async def test_remote_protocol_error_writes_error_summary(
    tmp_path: Path, monkeypatch,
):
    """LLM 一直抛 RemoteProtocolError → executor 写 summary.status='error' + re-raise。"""
    harness = _minimal_harness()
    from core import executor as exec_mod
    monkeypatch.setattr(exec_mod, "load_harness",
                          lambda node_type, nodes_dir=None: harness)

    exc = httpx.RemoteProtocolError("Server disconnected without sending a response.")
    llm = _AlwaysFailLLM(exc)

    with pytest.raises(httpx.RemoteProtocolError):
        await execute_node(
            node_type="literature",
            state_dir=tmp_path,
            llm=llm,
        )

    # summary.json 写好了
    runs = list(tmp_path.glob("*/summary.json"))
    assert len(runs) == 1, f"expected 1 summary, got {runs}"
    sm = json.loads(runs[0].read_text(encoding="utf-8"))
    assert sm["status"] == "error"
    assert sm["error_type"] == "RemoteProtocolError"
    assert "disconnected" in sm["error_message"]
    assert "Traceback" in sm["error_traceback"]
    assert sm["node_type"] == "literature"

    # active registry 已清
    assert sm["run_id"] not in [r.run_id for r in list_active_runs()]


@pytest.mark.asyncio
async def test_runtime_error_writes_error_summary(tmp_path: Path, monkeypatch):
    """generic RuntimeError（如 LLM retry 耗尽后的 wrapped error）也走 error 路径。"""
    harness = _minimal_harness()
    from core import executor as exec_mod
    monkeypatch.setattr(exec_mod, "load_harness",
                          lambda node_type, nodes_dir=None: harness)

    llm = _AlwaysFailLLM(RuntimeError("LLM API HTTP 503: (after 3 retries)"))

    with pytest.raises(RuntimeError):
        await execute_node(
            node_type="literature",
            state_dir=tmp_path,
            llm=llm,
        )

    sm_path = next(tmp_path.glob("*/summary.json"))
    sm = json.loads(sm_path.read_text(encoding="utf-8"))
    assert sm["status"] == "error"
    assert sm["error_type"] == "RuntimeError"
    assert "503" in sm["error_message"]


@pytest.mark.asyncio
async def test_error_summary_has_transcript_run_end(tmp_path: Path, monkeypatch):
    """transcript 应有 run_end event status=error，便于事后 grep。"""
    harness = _minimal_harness()
    from core import executor as exec_mod
    monkeypatch.setattr(exec_mod, "load_harness",
                          lambda node_type, nodes_dir=None: harness)

    llm = _AlwaysFailLLM(httpx.ConnectError("connection refused"))

    with pytest.raises(httpx.ConnectError):
        await execute_node(
            node_type="literature",
            state_dir=tmp_path,
            llm=llm,
        )

    tx = next(tmp_path.glob("*/transcript.jsonl"))
    events = [json.loads(line) for line in tx.read_text().splitlines() if line.strip()]
    end_events = [e for e in events if e.get("event") == "run_end"]
    assert len(end_events) == 1
    assert end_events[0]["status"] == "error"
    assert end_events[0]["error_type"] == "ConnectError"
    # traceback 被故意排除（避免 transcript 噪音）
    assert "error_traceback" not in end_events[0]


@pytest.mark.asyncio
async def test_cancelled_error_NOT_caught(tmp_path: Path, monkeypatch):
    """asyncio.CancelledError 不写 summary，向上冒，让 cancellation 语义照常传。"""
    import asyncio
    harness = _minimal_harness()
    from core import executor as exec_mod
    monkeypatch.setattr(exec_mod, "load_harness",
                          lambda node_type, nodes_dir=None: harness)

    llm = _AlwaysFailLLM(asyncio.CancelledError())

    # CancelledError 是 BaseException 不是 Exception，应该绕过 except 直接冒
    with pytest.raises(asyncio.CancelledError):
        await execute_node(
            node_type="literature",
            state_dir=tmp_path,
            llm=llm,
        )

    # summary.json 不应该被写出来
    summaries = list(tmp_path.glob("*/summary.json"))
    assert summaries == [], f"CancelledError 不应写 error summary，但发现: {summaries}"


# ── provider 挂了不是节点失败（issue #480）──────────────────────────────────

@pytest.mark.asyncio
async def test_provider_outage_is_categorised_as_external(tmp_path: Path, monkeypatch):
    """ReadError 打穿重试预算 → summary 说得出"是 provider 挂了"。

    现场（jicq 流体力学多轮 E2E）：Literature / Hypothesis 因 ReadError /
    RemoteProtocolError / ReadTimeout 没产出任何产物，而框架只留下一段
    traceback —— 用户无法区分"模型服务暂时失败"和"研究内容本身不合格"。
    """
    from core import executor as exec_mod
    monkeypatch.setattr(exec_mod, "load_harness",
                          lambda node_type, nodes_dir=None: _minimal_harness())
    monkeypatch.setenv("LLM_BASE_URL", "https://provider.example/v1")
    monkeypatch.setenv("LLM_MODEL", "glm-5.1")

    with pytest.raises(httpx.ReadError):
        await execute_node(node_type="literature", state_dir=tmp_path,
                            llm=_AlwaysFailLLM(httpx.ReadError("stream truncated")))

    sm = json.loads(next(tmp_path.glob("*/summary.json")).read_text(encoding="utf-8"))
    assert sm["failure_category"] == "provider_unavailable", sm.get("failure_category")
    assert sm["failure_subcategory"] == "ReadError"
    assert "ReadError" in (sm.get("provider_error") or "")
    assert sm["provider_base_url"] == "https://provider.example/v1"
    assert sm["provider_model"] == "glm-5.1"
    assert "has_checkpoint" in sm, "有没有可续跑的 checkpoint 必须说清楚"


@pytest.mark.asyncio
async def test_a_node_side_crash_is_not_blamed_on_the_provider(tmp_path: Path, monkeypatch):
    """别把什么都推给 provider：普通异常不许染上 provider_unavailable。"""
    from core import executor as exec_mod
    monkeypatch.setattr(exec_mod, "load_harness",
                          lambda node_type, nodes_dir=None: _minimal_harness())

    with pytest.raises(ValueError):
        await execute_node(node_type="literature", state_dir=tmp_path,
                            llm=_AlwaysFailLLM(ValueError("节点自己的 bug")))

    sm = json.loads(next(tmp_path.glob("*/summary.json")).read_text(encoding="utf-8"))
    assert sm["failure_category"] is None
    assert sm.get("provider_error") is None


def test_provider_outage_does_not_count_as_a_node_failure() -> None:
    """provider 挂了不进节点的卡死统计 —— 否则熔断会去解一个不存在的问题。"""
    from core.run_history import EXTERNAL_FAILURE_CATEGORIES

    assert "provider_unavailable" in EXTERNAL_FAILURE_CATEGORIES


def test_the_transient_predicate_shares_the_retry_policy_list() -> None:
    """判据与重试策略共用同一份名单，不另开抄件。"""
    from core.llm import LLMHTTPError, is_transient_provider_error

    assert is_transient_provider_error(httpx.ReadError("x"))
    assert is_transient_provider_error(httpx.RemoteProtocolError("x"))
    assert is_transient_provider_error(httpx.ReadTimeout("x"))
    assert is_transient_provider_error(LLMHTTPError(429, "concurrency limit"))
    assert is_transient_provider_error(LLMHTTPError(503, "backend restarting"))
    # 4xx（除 429）是我们自己的请求有问题，重试白费，也不该记成 provider 挂了
    assert not is_transient_provider_error(LLMHTTPError(401, "bad key"))
    assert not is_transient_provider_error(ValueError("boom"))


@pytest.mark.asyncio
async def test_the_rate_limit_reason_survives_into_the_summary(tmp_path: Path, monkeypatch):
    """429 的原因要落盘，不能只活在一行日志里（issue #490）。

    只看到 "429" 时没人分得清限的是 TPM、RPM 还是并发位 —— 而这三种的处置
    完全不同（降并发 / 降频 / 砍 max_tokens）。
    """
    from core import executor as exec_mod
    from core.llm import LLMHTTPError
    monkeypatch.setattr(exec_mod, "load_harness",
                          lambda node_type, nodes_dir=None: _minimal_harness())

    exc = LLMHTTPError(
        429, "LLM API HTTP 429: busy",
        body='{"error": {"type": "rate_limit_exceeded", "code": "concurrency"}}',
        retry_after="45", model="glm-5.1")

    with pytest.raises(LLMHTTPError):
        await execute_node(node_type="literature", state_dir=tmp_path,
                            llm=_AlwaysFailLLM(exc))

    sm = json.loads(next(tmp_path.glob("*/summary.json")).read_text(encoding="utf-8"))
    detail = sm["provider_error_detail"]
    assert detail["status"] == 429
    assert detail["error_type"] == "rate_limit_exceeded"
    assert detail["error_code"] == "concurrency"
    assert detail["retry_after"] == "45"
    assert detail["model"] == "glm-5.1"

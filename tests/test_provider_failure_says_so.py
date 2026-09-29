"""模型服务的锅要写着模型服务的名字过河 —— 三段链路各自守一条。

现场（2026-08-20，积算网关 ReadTimeout，会话 4adbea62）：hypothesis 干了
34 个工具调用、122 万 token 的真活，被上游读超时打死。此后三层各错一步：
worker 兜底把异常标成 ``runtime_error``（文案表认不出 → "平台内部错误"）；
run_node 的精简返回按名点收、类别码没过投影（→ 调度器自己造句，造出
"被框架错误打断"）；ingest 的 run_end payload 不带 failure_category
（→ UI 只会说一句笼统的"失败"）。平台是唯一没做错事的一方，锅全在它头上。
不联网。
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import httpx
import pytest

from core.state import State


def _state() -> State:
    return State.new(node_type="hypothesis", base_dir=Path(tempfile.mkdtemp()))


def test_error_code_for_declares_provider_transience() -> None:
    """worker 兜底：provider 瞬态故障声明 upstream_unavailable，别的照旧。"""
    from platform_runtime import _error_code_for

    assert _error_code_for(httpx.ReadTimeout("read timed out")) == "upstream_unavailable"
    assert _error_code_for(httpx.ConnectError("refused")) == "upstream_unavailable"
    assert _error_code_for(ValueError("a real bug")) == "runtime_error"

    class _WithStatus(Exception):
        status = 503

    assert _error_code_for(_WithStatus("upstream 503")) == "upstream_unavailable"


@pytest.mark.asyncio
async def test_provider_failure_summary_carries_fixed_attribution() -> None:
    """run_node 给调度器的结果必须带定死的归因句 —— 只给类别码等于逼模型造句。"""
    from shared.tools.run_node import _execute_with_infra_retry_inner

    async def fake_execute_node(**_kw):
        return {
            "status": "error",
            "run_id": "child-1",
            "failure_category": "provider_unavailable",
            "failure_subcategory": "ReadTimeout",
            # 有产出 → 机械重试不接手（重跑可能覆盖成果），一次就返回
            "produced_artifact_types": ["pre_registration"],
            "turns": 20,
            "tool_call_count": 34,
        }

    summary = await _execute_with_infra_retry_inner(
        _state(), "hypothesis", {}, fake_execute_node)
    human = summary.get("failure_human") or ""
    assert "模型服务" in human, "归因句必须点名模型服务"
    assert "不是研究框架" in human, "必须明说不是框架的错 —— 实测模型会反着说"
    assert "ReadTimeout" in human, "子类别要在句子里，别让人再去翻字段"


@pytest.mark.asyncio
async def test_non_provider_failure_gets_no_attribution_sentence() -> None:
    """别的失败不硬贴这句话 —— 说错归因和不说一样糟。"""
    from shared.tools.run_node import _execute_with_infra_retry_inner

    async def fake_execute_node(**_kw):
        return {
            "status": "incomplete",
            "run_id": "child-2",
            "failure_category": "orchestration_not_closed",
            "produced_artifact_types": [],
            "turns": 5,
            "tool_call_count": 3,
        }

    summary = await _execute_with_infra_retry_inner(
        _state(), "hypothesis", {}, fake_execute_node)
    assert "failure_human" not in summary

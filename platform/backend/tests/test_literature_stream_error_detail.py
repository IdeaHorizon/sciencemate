"""Streamed errors use the UI language and retain the bridge cause in logs."""

import json
from unittest.mock import AsyncMock

import pytest


@pytest.mark.asyncio
async def test_search_stream_preserves_bridge_failure_reason(monkeypatch, caplog):
    from app.api.v1 import literature
    from app.schemas.literature import LiteratureSearchIn

    monkeypatch.setattr(literature, "select_effective_backend", AsyncMock(return_value=object()))
    monkeypatch.setattr(literature, "_school_search", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        literature.bridge,
        "call",
        AsyncMock(side_effect=literature.bridge.BridgeUnavailable("未能生成有效的英文检索词")),
    )

    response = await literature.search_literature_stream(
        LiteratureSearchIn(query="布拉格反射", limit=20),
        user=object(),
        db=AsyncMock(),
    )
    events = [
        json.loads(chunk.removeprefix("data: "))
        async for chunk in response.body_iterator
        if chunk.startswith("data: ")
    ]

    assert events[-1]["type"] == "error"
    assert "无法处理检索结果" in events[-1]["message"]
    assert "未能生成有效的英文检索词" in caplog.text

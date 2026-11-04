"""自建端点不鉴权时，资讯 / 文献检索那条单次调用也要够得着它。

现场（2026-11-04）：设置里加一条「本机 / 自建网关」连接、key 留空，探针通过、状态 ready、
主推理角色绑上了 —— 会话能用，文献检索却回「无法处理检索结果」。日志：
`BridgeUnavailable: model backend has no usable credentials`。会话那条路（harness_sessions）
在 PR#1019 已经按「谁提供端点」判（`credential_is_optional`），bridge 这条还在要 key。

判据钉在行为上：真起一个子进程，看它收到的环境 —— 没有 key 也发得出去，key 是空串
（不是 "None"）；没填 base_url 的官方托管端点没有 key 照旧拒。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


def _fake_harness(root: Path) -> None:
    (root / "core").mkdir(parents=True, exist_ok=True)
    (root / "core" / "llm.py").write_text("", encoding="utf-8")
    (root / "platform_runtime.py").write_text(
        "import json, os, sys\n"
        "sys.stdin.readline()\n"
        "sys.stdout.write(json.dumps({'type': 'probe_result', 'key': os.environ.get('LLM_API_KEY'),\n"
        "                             'base': os.environ.get('LLM_BASE_URL')}) + '\\n')\n"
        "sys.stdout.flush()\n",
        encoding="utf-8",
    )


class _Backend:
    def __init__(self, base_url: str | None) -> None:
        self.provider = "local"
        self.model = "stub-model"
        self.base_url = base_url
        self.api_key_encrypted = None
        self.api_key_ref = None
        self.last_probe_ok = True


@pytest.fixture
def harness(tmp_path, monkeypatch):
    from app.config import settings

    _fake_harness(tmp_path)
    monkeypatch.setattr(settings, "harness_bridge_enabled", True)
    monkeypatch.setattr(settings, "harness_root", str(tmp_path))
    monkeypatch.setattr(settings, "harness_python", sys.executable)
    return tmp_path


@pytest.mark.asyncio
async def test_a_self_hosted_endpoint_without_a_key_is_reached(harness, monkeypatch) -> None:
    from app.services.feed import bridge

    monkeypatch.setattr(bridge, "resolved_api_key", lambda backend: None)
    monkeypatch.setattr(bridge, "_provider_base_url", lambda backend: backend.base_url)
    result = await bridge.call(op="probe", payload={}, result_type="probe_result",
                               backend=_Backend("http://127.0.0.1:18495/v1"))
    assert result["base"] == "http://127.0.0.1:18495/v1"
    assert result["key"] == "", f"没有 key 时传给子进程的应是空串，实际是 {result['key']!r}"


@pytest.mark.asyncio
async def test_a_hosted_endpoint_without_a_key_is_still_refused(harness, monkeypatch) -> None:
    from app.services.feed import bridge

    monkeypatch.setattr(bridge, "resolved_api_key", lambda backend: None)
    # 没填 base_url = 用厂商官方地址；key 是我们唯一的身份，没有就不发。
    monkeypatch.setattr(bridge, "_provider_base_url", lambda backend: "https://api.deepseek.com/v1")
    with pytest.raises(bridge.BridgeUnavailable, match="no usable credentials"):
        await bridge.call(op="probe", payload={}, result_type="probe_result", backend=_Backend(None))

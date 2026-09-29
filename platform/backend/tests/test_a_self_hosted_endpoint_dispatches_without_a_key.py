""""没有 API key"不等于"这条连接没配好" —— 派发的两道闸都得认这件事。

现场（2026-09-15，yuankk）：`local` + `http://10.128.7.30:8000/v1` 的 vLLM 不鉴权，
key 留空。从 node20 实测那台服务器：`/v1/models` 带不带 key 都是 200 + JSON，
平台那张 1×1 图探针也 200。可平台这一侧三处各自把它判死：

- `backend_status()` → `missing_credentials`（见 test_credential_health.py）
- `_role_delivery_payload` 一句 `continue` 把这个角色从下发清单里**静默**抹掉
- `_child_environment` 直接 `raise HarnessSessionError` —— worker 根本起不来

判据落在后两处：自建端点（用户自己填了 base_url）没有 key 照样发得出去；托管
端点（我们用 provider 默认地址）没有 key 仍然拦下 —— 那时 key 是唯一的身份。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.models.model_backend import ModelBackendConfig
from app.services import harness_sessions as hs
from app.services.model_role_catalog import REASONING_ROLE


def _backend(**kw) -> ModelBackendConfig:
    defaults = dict(
        id="b1", display_name="Qwen3.8-27B", provider="local", model="qwen3.8-27b",
        base_url="http://10.128.7.30:8000/v1", scope_kind="personal", scope_id="u1",
        roles=[REASONING_ROLE], default_for_roles=[], is_enabled=True,
        credential_source="none", encrypted_api_key=None,
    )
    defaults.update(kw)
    return ModelBackendConfig(**defaults)


@pytest.fixture(autouse=True)
def _no_stored_key(monkeypatch):
    monkeypatch.setattr(hs, "resolved_api_key", lambda config: None)


def test_a_self_hosted_role_is_delivered_with_an_empty_key():
    payload = json.loads(hs._role_delivery_payload({REASONING_ROLE: _backend()}))

    assert REASONING_ROLE in payload, "自建端点的角色不许被静默抹掉"
    assert payload[REASONING_ROLE]["base_url"] == "http://10.128.7.30:8000/v1"
    assert payload[REASONING_ROLE]["api_key"] == "", "缺席就是空串，不是 None（worker 那边要能 json 解）"


def test_a_hosted_provider_without_a_key_is_still_dropped():
    """托管端点没有 key = 我们没有任何办法证明自己是谁。拦下是对的。"""
    hosted = _backend(provider="deepseek", base_url=None)

    assert json.loads(hs._role_delivery_payload({REASONING_ROLE: hosted})) == {}


def test_the_worker_starts_for_a_self_hosted_endpoint_and_not_for_a_hosted_one(monkeypatch, tmp_path):
    monkeypatch.setattr(hs, "harness_subprocess_env", lambda root, passthrough=(): {})

    env = hs._child_environment(Path(tmp_path), {REASONING_ROLE: _backend()})
    assert env["LLM_BASE_URL"] == "http://10.128.7.30:8000/v1"
    assert env["LLM_API_KEY"] == ""
    assert json.loads(env["HARNESS_MODEL_ROLES"])[REASONING_ROLE]["model"] == "qwen3.8-27b"

    with pytest.raises(hs.HarnessSessionError, match="no usable credential"):
        hs._child_environment(Path(tmp_path), {REASONING_ROLE: _backend(provider="deepseek", base_url=None)})

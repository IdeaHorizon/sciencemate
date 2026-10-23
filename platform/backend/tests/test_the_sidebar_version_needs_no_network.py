"""侧栏底部的版本号读 `GET /version`：只读本机标记，不碰网络（wangd 10-19「为啥我看不到版本号」）。

`GET /update` 每次都去更新源查一遍，离线时要等超时 —— 侧栏每次渲染都问它就太贵了。
两处说的版本出自同一个函数（`describe_installed`），不会分叉。
"""
from __future__ import annotations

import pytest

from app.services import release_feed
from app.services import self_update as su
from tests.test_local_runtime_api import _headers, _token, runtime_client  # noqa: F401


@pytest.mark.asyncio
async def test_the_version_is_answered_without_touching_the_network(runtime_client, monkeypatch, tmp_path) -> None:
    from app.config import settings

    def no_network(*args, **kwargs):
        raise AssertionError("GET /version 去拉了更新源")

    for name in ("get_json", "get_bytes", "stream"):
        monkeypatch.setattr(release_feed, name, no_network)
    monkeypatch.setattr(settings, "platform_data_root", str(tmp_path))
    seen = {}

    def describe(data_root, harness_dir):
        seen["called"] = True
        return "0.5.7", "payload"

    monkeypatch.setattr(su, "describe_installed", describe)
    client, _ = runtime_client
    token = await _token(client, "researcher@atrium.local")
    response = await client.get("/api/v1/version", headers=_headers(token))
    assert response.status_code == 200, response.text
    assert response.json() == {"version": "0.5.7", "installed_from": "payload"}
    assert seen.get("called"), "版本号不是从 describe_installed 来的 —— 和关于页会分叉"

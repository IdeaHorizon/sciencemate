"""把 runtime_client 那台后端装配成组织档（profile=org）—— 核心测试与专业版测试共用的夹具。

档位（personal / org）是核心的事：组织档的鉴权、成员关系、可见范围都在核心里；专业版加的是
组织服务器的**功能**（组织页、请柬、连接、自升级）。所以这个夹具住在核心，两边的测试都从这里拿。
"""
import pytest

from app.config import settings
from tests.test_local_runtime_api import _headers, _token, runtime_client  # noqa: F401


@pytest.fixture
def an_organisation_server(runtime_client, monkeypatch):  # noqa: F811
    monkeypatch.setattr(settings, "profile", "org")
    return runtime_client


async def _as(client, email: str) -> dict:
    return _headers(await _token(client, email))

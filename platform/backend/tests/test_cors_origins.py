"""换个端口跑前端不该变成"Failed to fetch"。

CORS 允许来源原先是写死的两条（localhost:3000 / 127.0.0.1:3000）。而
docs/ui-deployment.md 明写自部署"按需改端口" —— 改了之后预检 400，前端只
显示一句没有信息量的 "Failed to fetch"，没有任何线索指向 CORS。写名单的
护栏就是这样：新东西默认漏过（这里是默认拒绝），而且不吭声。

规则：显式配了就用配的；没配且是本地自部署（local_demo_mode/debug）就
**扫盘放行任意 loopback 端口**；否则保持原来的保守默认。
"""

import importlib

import pytest
from fastapi.middleware.cors import CORSMiddleware


def _cors_layer(monkeypatch, **overrides):
    from app.config import settings as app_settings

    for key, value in overrides.items():
        monkeypatch.setattr(app_settings, key, value)
    main = importlib.reload(importlib.import_module("app.main"))
    for layer in main.app.user_middleware:
        if layer.cls is CORSMiddleware:
            return layer.kwargs
    raise AssertionError("CORS middleware is not installed")


@pytest.fixture(autouse=True)
def _restore_app_module():
    """本文件 reload app.main；跑完还原，免得污染同进程的其它测试。"""
    yield
    importlib.reload(importlib.import_module("app.main"))


def test_local_deploy_accepts_any_loopback_port(monkeypatch):
    kwargs = _cors_layer(monkeypatch, cors_allow_origins="", local_demo_mode=True, debug=False)
    import re

    pattern = re.compile(kwargs["allow_origin_regex"])
    assert pattern.fullmatch("http://127.0.0.1:18092")
    assert pattern.fullmatch("http://localhost:3000")
    assert pattern.fullmatch("http://localhost")
    # 扫盘限于 loopback —— 不是"什么都放行"
    assert not pattern.fullmatch("http://evil.example.com")
    assert not pattern.fullmatch("http://192.0.2.20:18080")


def test_explicit_list_wins(monkeypatch):
    kwargs = _cors_layer(
        monkeypatch,
        cors_allow_origins="http://192.0.2.20:18080, https://platform.example.edu",
        local_demo_mode=True,
        debug=True,
    )
    assert kwargs["allow_origins"] == [
        "http://192.0.2.20:18080",
        "https://platform.example.edu",
    ]
    assert "allow_origin_regex" not in kwargs


def test_unconfigured_production_keeps_the_conservative_default(monkeypatch):
    kwargs = _cors_layer(monkeypatch, cors_allow_origins="", local_demo_mode=False, debug=False)
    assert kwargs["allow_origins"] == ["http://localhost:3000", "http://127.0.0.1:3000"]
    assert "allow_origin_regex" not in kwargs

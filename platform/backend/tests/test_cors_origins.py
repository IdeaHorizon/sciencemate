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
    """本文件 reload app.main；跑完**原样**放回，免得污染同进程的其它测试。

    原来的「还原」是再 reload 一次 —— 可 reload 是在同一个模块对象上重跑一遍，
    `app.main.app` 换成了又一个**新**实例。别的测试文件在导入时拿到的是最早那个
    app（夹具在它上面装 `dependency_overrides[get_db]`、请求也发给它），而在调用
    时才 `from app.main import app` 的辅助函数拿到的是新的、overrides 为空的那个，
    于是退回全局库 —— `no such table: users`，红在 test_project_blockers /
    test_project_file_preview 里，只在 xdist 恰好把本文件分到它们前面时出现
    （2026-10-09 #1197 连红两次，本机全绿）。所以存下命名空间，跑完整个放回。
    """
    module = importlib.import_module("app.main")
    saved = dict(module.__dict__)
    yield
    module.__dict__.clear()
    module.__dict__.update(saved)


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

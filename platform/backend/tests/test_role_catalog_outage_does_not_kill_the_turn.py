"""模型角色目录读不到 → 只是"辅助角色都缺"，不是"平台不能说话"。

现场根据：2026-08-22 的 postprocess 事故里，wangd 对"少一个能力就整条链停摆"
的原话是「没 VLM 就不做呀，你这反正图有了，你先用上呀」「少点什么东西它就
进行不下去了，Agent 应该很灵活才对」。

`resolve_role_bindings` 在每一次对话开头解析全部角色。它原来直接让
ModelRoleCatalogError 冒上去，于是 HARNESS_ROOT 少配一行 = 一句话都说不了
（实测：13 条会话测试连带全红）。但主推理模型的解析**根本不经过目录**
（调用方对 reasoning 有 effective_backend 兜底），目录只决定辅助角色 ——
而"辅助角色缺席"在这套设计里本来就是**局面**：节点拿到 absence_note、
按缺能力那条路降级。

吵在该吵的地方：日志 + 『设置 → 模型』那条路径照旧抛（那才是给人看配置的
地方）。这个文件钉的就是这条分工。
"""

import logging

import pytest

from app.config import settings as app_settings
from app.models.model_backend import ModelBackendConfig, UserModelBackendPreference
from app.models.user import User
from app.services import model_backends
from app.services.model_role_catalog import ModelRoleCatalogError


@pytest.fixture(autouse=True)
def _harness_on(monkeypatch):
    monkeypatch.setattr(app_settings, "harness_bridge_enabled", True)


def _backend(name, roles):
    return ModelBackendConfig(
        id=name, display_name=name, provider="deepseek", model="m",
        base_url="http://x/v1", scope_kind="institution", scope_id="ieit",
        roles=list(roles), default_for_roles=list(roles), is_enabled=True,
        credential_source="encrypted", encrypted_api_key="k",
    )


def _user():
    return User(id="u1", email="u", hashed_password="x", display_name="u",
                role="researcher", institution_id="ieit")


class _DB:
    async def get(self, model, key):
        return None if model is UserModelBackendPreference else None

    async def scalar(self, _stmt):
        return None


def _visible(monkeypatch, items):
    async def fake_list(db, user):
        return list(items)

    monkeypatch.setattr("app.services.model_backends.list_visible_backends", fake_list)


@pytest.mark.asyncio
async def test_unreadable_catalog_yields_no_bindings_instead_of_raising(
    monkeypatch, caplog,
):
    """目录读不到 → 返回空绑定（调用方照旧给 reasoning 兜底），并且吵在日志里。"""
    def _boom():
        raise ModelRoleCatalogError("HARNESS_ROOT 未配置")

    monkeypatch.setattr("app.services.model_backends.catalog", _boom)
    _visible(monkeypatch, [_backend("b", ["reasoning"])])

    with caplog.at_level(logging.ERROR):
        bindings = await model_backends.resolve_role_bindings(_DB(), _user())

    assert bindings == {}, "目录坏了不该顺手把主模型也判没"
    assert any("模型角色目录读不到" in r.getMessage() for r in caplog.records), \
        "静默降级等于把部署缺陷藏起来 —— 必须留下 ERROR"


@pytest.mark.asyncio
async def test_readable_catalog_still_binds_every_declared_role(monkeypatch):
    """变异对照：目录好的时候，每个角色照样各自解析（别把降级写成常态）。"""
    class _Spec:
        def __init__(self, rid):
            self.id = rid

    monkeypatch.setattr(
        "app.services.model_backends.catalog",
        lambda: (_Spec("reasoning"), _Spec("visual_review")),
    )
    _visible(monkeypatch, [
        _backend("text", ["reasoning"]),
        _backend("vlm", ["visual_review"]),
    ])

    bindings = await model_backends.resolve_role_bindings(_DB(), _user())

    assert set(bindings) == {"reasoning", "visual_review"}
    assert bindings["visual_review"].id == "vlm"

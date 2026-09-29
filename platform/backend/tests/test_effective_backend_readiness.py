"""默认模型后端必须是**真的能用**的那个，而且 UI 和运行时得说同一句话。

现场（node20 交付实测）：机构默认指向一个 credential_source=environment 但
key 已失效的公网后端 —— 同事一进来发消息就 401，而同一机构里有一个 ready
的自建后端就在旁边。"被标成默认"不等于"能用"。

更根上的问题是这条规则有**两份实现**：services/model_backends.effective_backend
（运行时真取哪个）和 api/v1/settings._effective_id（UI 给谁打 default 徽章）。
两份各判各的，徽章指着 A、运行时用着 B。本文件测的是收敛之后的那一处。
"""

import pytest

from app.api.v1.settings import _effective_id
from app.config import settings as app_settings
from app.models.model_backend import ModelBackendConfig, UserModelBackendPreference
from app.models.user import User
from app.services.model_backends import select_effective_backend


@pytest.fixture(autouse=True)
def _harness_on(monkeypatch):
    """backend_status 在 bridge 关掉时对**所有**后端都返回 harness_disabled，
    那样这组测试测的就不是 readiness 而是开关本身。"""
    monkeypatch.setattr(app_settings, "harness_bridge_enabled", True)


def _backend(name, *, source="encrypted", key="k", default=False, enabled=True):
    return ModelBackendConfig(
        id=name,
        display_name=name,
        provider="deepseek",
        model="m",
        base_url="http://x/v1",
        scope_kind="institution",
        scope_id="ieit",
        roles=["reasoning"],
        default_for_roles=["reasoning"] if default else [],
        is_enabled=enabled,
        credential_source=source,
        encrypted_api_key=key,
    )


def _user():
    return User(
        id="u1", email="u", hashed_password="x", display_name="u",
        role="researcher", institution_id="ieit",
    )


class _DB:
    """只回答这两个问题：有没有用户偏好 / 偏好指向的后端是哪个。"""

    def __init__(self, preference=None, by_id=None):
        self._preference = preference
        self._by_id = by_id or {}

    async def get(self, model, key):
        return self._preference if model is UserModelBackendPreference else None

    async def scalar(self, _stmt):
        return self._preference and self._by_id.get(self._preference.backend_id)


def _visible(monkeypatch, items):
    async def fake_list(db, user):
        return list(items)

    # 两个模块各自 import 了这个名字；一起换掉，测的才是同一条路径
    monkeypatch.setattr("app.services.model_backends.list_visible_backends", fake_list)
    monkeypatch.setattr("app.api.v1.settings.list_visible_backends", fake_list)


@pytest.mark.asyncio
async def test_ready_backend_wins_over_unusable_default(monkeypatch):
    """默认后端缺凭据 → 选可用的那个，而不是把用户丢给必然失败的默认。"""
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setattr(app_settings, "deepseek_api_key", "")
    broken = _backend("broken-default", source="environment", key=None, default=True)
    good = _backend("self-hosted")
    _visible(monkeypatch, [broken, good])

    selected = await select_effective_backend(_DB(), _user())
    assert selected.id == "self-hosted"


@pytest.mark.asyncio
async def test_ready_default_is_still_preferred(monkeypatch):
    """既是默认又可用 → 照旧选它（别把正常语义修没了）。"""
    good_default = _backend("good-default", default=True)
    other = _backend("other")
    _visible(monkeypatch, [good_default, other])

    selected = await select_effective_backend(_DB(), _user())
    assert selected.id == "good-default"


@pytest.mark.asyncio
async def test_ui_badge_matches_what_the_runtime_would_pick(monkeypatch):
    """UI 的 default 徽章和运行时的选择是同一处推导出来的。

    这条覆盖的正是老 _effective_id 的分歧：它把**用户偏好**无条件当默认
    返回，哪怕那个后端根本不 ready —— 运行时早就绕开它了。
    """
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setattr(app_settings, "deepseek_api_key", "")
    broken = _backend("broken-pref", source="environment", key=None)
    good = _backend("self-hosted", default=True)
    db = _DB(
        preference=UserModelBackendPreference(
            user_id="u1", role="reasoning", backend_id="broken-pref"
        ),
        by_id={"broken-pref": broken, "self-hosted": good},
    )
    _visible(monkeypatch, [broken, good])
    user = _user()

    runtime_choice = await select_effective_backend(db, user)
    ui_badge = await _effective_id(db, user)
    assert runtime_choice.id == "self-hosted"
    assert ui_badge == runtime_choice.id


@pytest.mark.asyncio
async def test_usable_preference_still_wins(monkeypatch):
    """偏好本身能用时依旧压过 scope 默认 —— 用户的显式选择不该被"修"掉。"""
    preferred = _backend("preferred")
    scope_default = _backend("scope-default", default=True)
    db = _DB(
        preference=UserModelBackendPreference(
            user_id="u1", role="reasoning", backend_id="preferred"
        ),
        by_id={"preferred": preferred, "scope-default": scope_default},
    )
    _visible(monkeypatch, [preferred, scope_default])

    selected = await select_effective_backend(db, _user())
    assert selected.id == "preferred"


@pytest.mark.asyncio
async def test_nothing_usable_reports_none_rather_than_lying(monkeypatch):
    """全都不可用 → 返回 None（端点自己决定报 503），不硬凑一个出来。"""
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setattr(app_settings, "deepseek_api_key", "")
    _visible(monkeypatch, [_backend("broken", source="environment", key=None, default=True)])

    assert await select_effective_backend(_DB(), _user()) is None

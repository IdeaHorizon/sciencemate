""""key 字段填了"是声明，"provider 认这把 key"才是观测。

node20 交付实测：机构默认后端的 key 填得好好的但早已失效，平台一路把它
显示为 ready 并选成默认 —— 每位同事一进来发消息就 401
"Authentication Fails, Your api key: ****724d is invalid"。

设计要点：
- 落库的是**观测**（探过没、结论、provider 原话），status 每次现算。
  判决落盘必然随规则进化作废。
- last_probe_ok 是**三态**：None 不知道 / True 认 / False 明确拒绝。
  只有 False 降级 —— 超时和 5xx 不算拒绝，provider 临时抽风不该让一个
  好后端被永久标坏。
"""

import pytest

from app.config import settings as app_settings
from app.models.model_backend import ModelBackendConfig
from app.services import model_backends as mb


@pytest.fixture(autouse=True)
def _harness_on(monkeypatch):
    monkeypatch.setattr(app_settings, "harness_bridge_enabled", True)


def _backend(**kw):
    defaults = dict(
        id="b1", display_name="b", provider="openai_compatible", model="m",
        base_url="https://api.example.com/v1", scope_kind="institution", scope_id="ieit",
        roles=["reasoning"], default_for_roles=[], is_enabled=True,
        credential_source="encrypted", encrypted_api_key="cipher",
    )
    defaults.update(kw)
    return ModelBackendConfig(**defaults)


class _Response:
    def __init__(self, status_code, *, content_type="application/json", body=None):
        self.status_code = status_code
        self.headers = {"content-type": content_type}
        self._body = {"error": {"message": "nope"}} if body is None else body

    @property
    def is_success(self):
        return 200 <= self.status_code < 300

    def json(self):
        if self._body is _NOT_JSON:
            raise ValueError("not json")
        return self._body


_NOT_JSON = object()


def _patch_http(monkeypatch, *, response=None, raises=None):
    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None):
            if raises:
                raise raises
            return response

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    monkeypatch.setattr(mb, "resolved_api_key", lambda config: "sk-test")


# ── status 现算 ───────────────────────────────────────────────────────────


def test_rejected_credential_is_not_ready():
    assert mb.backend_status(_backend(last_probe_ok=False)) == "credentials_rejected"


def test_never_probed_stays_ready():
    """没探过 ≠ 坏了。默认不能把所有存量后端一夜之间打成不可用。"""
    assert mb.backend_status(_backend(last_probe_ok=None)) == "ready"


def test_probe_ok_is_ready():
    assert mb.backend_status(_backend(last_probe_ok=True)) == "ready"


def test_missing_credentials_still_wins_over_probe():
    """连 key 都没有时，报"缺凭据"比报"被拒绝"更准确。"""
    config = _backend(credential_source="encrypted", encrypted_api_key=None, last_probe_ok=False)
    assert mb.backend_status(config) == "missing_credentials"


# ── 探测本身：只有明确拒绝才算坏 ────────────────────────────────────────


@pytest.mark.asyncio
async def test_401_is_a_definitive_rejection(monkeypatch):
    _patch_http(monkeypatch, response=_Response(401))
    ok, detail = await mb.probe_backend_credential(_backend())
    assert ok is False
    assert "401" in detail


@pytest.mark.asyncio
async def test_200_is_acceptance(monkeypatch):
    _patch_http(monkeypatch, response=_Response(200))
    ok, _ = await mb.probe_backend_credential(_backend())
    assert ok is True


@pytest.mark.asyncio
async def test_provider_5xx_is_inconclusive_not_rejection(monkeypatch):
    """provider 抽风不该把一个好后端永久标坏。"""
    _patch_http(monkeypatch, response=_Response(503))
    ok, _ = await mb.probe_backend_credential(_backend())
    assert ok is None


@pytest.mark.asyncio
async def test_network_failure_is_inconclusive(monkeypatch):
    """探不到（断网/超时）= 不知道。今晚本机就够不着实验室网。"""
    _patch_http(monkeypatch, raises=OSError("no route to host"))
    ok, detail = await mb.probe_backend_credential(_backend())
    assert ok is None
    assert "could not reach" in detail


@pytest.mark.asyncio
async def test_provider_without_probe_endpoint_is_inconclusive(monkeypatch):
    _patch_http(monkeypatch, response=_Response(200))
    ok, _ = await mb.probe_backend_credential(_backend(base_url=None))
    assert ok is None


# ── 与选择逻辑联通：被拒绝的默认不该赢过可用的后端 ───────────────────


@pytest.mark.asyncio
async def test_rejected_default_loses_to_a_working_backend(monkeypatch):
    """node20 现场的机械回放：默认被 401 过，旁边有个能用的。"""
    rejected_default = _backend(
        id="rejected-default", default_for_roles=["reasoning"], last_probe_ok=False
    )
    working = _backend(id="working", last_probe_ok=True)

    async def fake_list(db, user):
        return [rejected_default, working]

    monkeypatch.setattr(mb, "list_visible_backends", fake_list)

    from app.models.user import User

    user = User(id="u1", email="u", hashed_password="x", display_name="u",
                role="researcher", institution_id="ieit")

    class _DB:
        async def get(self, *a, **k):
            return None

    selected = await mb.select_effective_backend(_DB(), user)
    assert selected.id == "working"


@pytest.mark.asyncio
async def test_captive_portal_401_is_not_a_provider_rejection(monkeypatch):
    """强制门户 / 公司代理也回 401，但回的是 HTML 登录页。

    把那种当"key 失效"，代理后面的同事所有后端会被永久标坏 —— 而真正的
    provider（OpenAI 兼容）一律回 JSON 错误体。判据用这个。
    """
    _patch_http(
        monkeypatch,
        response=_Response(401, content_type="text/html", body=_NOT_JSON),
    )
    ok, detail = await mb.probe_backend_credential(_backend())
    assert ok is None
    assert "non-API responder" in detail


@pytest.mark.asyncio
async def test_json_401_without_content_type_still_counts(monkeypatch):
    """没有 content-type 但 body 是 JSON —— 照样认（别把真拒绝漏掉）。"""
    _patch_http(monkeypatch, response=_Response(401, content_type="", body={"code": "INVALID_API_KEY"}))
    ok, _ = await mb.probe_backend_credential(_backend())
    assert ok is False


# ── 自建端点：没有 key 是正常形态，不是"没配好" ─────────────────────────
#
# 现场（2026-09-15，yuankk）：`local` + `http://10.128.7.30:8000/v1` 的 vLLM
# 不鉴权，key 留空 → 状态 `missing_credentials`、探针连包都没发（界面上"未判定"）、
# 真要跑时 worker 起不来。那台服务器一直好好的 —— 从 node20 打 `/v1/models`
# 带不带 key 都是 200 + JSON，1×1 图探针也 200。


def _selfhosted(**kw):
    return _backend(provider="local", base_url="http://10.128.7.30:8000/v1",
                    credential_source="none", encrypted_api_key=None, **kw)


def test_a_self_hosted_endpoint_needs_no_key_but_hosted_ones_do():
    """判据是**谁提供端点**，不是 provider 标签。

    `openai_compatible` 既可能是自建 vLLM 也可能是某家托管服务 —— 同一个标签
    两种答案，名单在这件事上是错的量具。
    """
    assert mb.credential_is_optional(_selfhosted()) is True
    assert mb.credential_is_optional(_backend(provider="deepseek", base_url=None)) is False
    assert mb.credential_is_optional(_backend(provider="local", base_url="   ")) is False


def test_a_self_hosted_endpoint_is_ready_only_after_it_served_us_without_a_key():
    """"它不鉴权"是**观测**，不是我们替它声明的。

    没探过之前照旧说 `missing_credentials` —— 否则等于把"没填 key"一律说成
    "能用"，把这个坑从一头翻到另一头。
    """
    assert mb.backend_status(_selfhosted(last_probe_ok=None)) == "missing_credentials"
    assert mb.backend_status(_selfhosted(last_probe_ok=True)) == "ready"
    # 托管 provider 没有 key：探针再说什么都不该把它说成能用。
    hosted = _backend(provider="deepseek", base_url=None,
                      credential_source="none", encrypted_api_key=None, last_probe_ok=True)
    assert mb.backend_status(hosted) == "missing_credentials"


@pytest.mark.asyncio
async def test_a_keyless_probe_sends_no_authorization_header(monkeypatch):
    """没有 key 就**不发**那个头，而不是发一个空的。

    `Bearer `（空值）在一部分网关上会被判成"给了一把坏 key"而回 401 —— 那个
    401 指的是假因：端点本来根本不要鉴权。
    """
    seen: list[dict | None] = []

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None):
            seen.append(headers)
            return _Response(200, body={"object": "list", "data": []})

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    monkeypatch.setattr(mb, "resolved_api_key", lambda config: None)

    ok, detail = await mb.probe_backend_credential(_selfhosted())

    assert seen == [{}], f"不该带 Authorization 头：{seen}"
    assert ok is True
    assert "unauthenticated" in detail


@pytest.mark.asyncio
async def test_an_endpoint_that_demands_a_key_says_so_instead_of_rejecting_one(monkeypatch):
    """没给 key 而被拒 = "这个端点要鉴权"，不是"这把 key 坏了"。

    判 False 会让界面说 "Credential rejected" —— 而这里根本没有凭据可拒。
    """
    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None):
            return _Response(401)

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    monkeypatch.setattr(mb, "resolved_api_key", lambda config: None)

    ok, detail = await mb.probe_backend_credential(_selfhosted())

    assert ok is None, "不是 False —— 没有凭据就谈不上被拒"
    assert "requires a credential" in detail
    assert mb.backend_status(_selfhosted(last_probe_ok=ok)) == "missing_credentials"

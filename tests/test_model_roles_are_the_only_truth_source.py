"""模型角色：交付通道、凭据去向、缺角色的说法。

这些测试钉的是**接缝**，不是函数返回值：凭据有没有离开 env、缺角色时报错
指不指得到能解决的地方、坏配置会不会被静默当成"没配置"。
"""

from __future__ import annotations

import json

import pytest

from core import model_roles, runtime_secrets


@pytest.fixture(autouse=True)
def _clean_process_state(monkeypatch):
    """每个用例都从"什么都没装"开始 —— 角色状态是进程级的。"""
    monkeypatch.delenv("HARNESS_MODEL_ROLES", raising=False)
    for name in ("LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL", "LLM_CONTEXT_WINDOW"):
        monkeypatch.delenv(name, raising=False)
    runtime_secrets._SECRETS.clear()
    model_roles._bindings.clear()
    model_roles._installed = False
    model_roles._delivery_error = None
    yield
    runtime_secrets._SECRETS.clear()
    model_roles._bindings.clear()
    model_roles._installed = False
    model_roles._delivery_error = None


def _channel(**roles) -> str:
    return json.dumps(roles)


def test_the_credential_leaves_the_environment(monkeypatch):
    """通道读完就该从 env 消失。

    工具子进程继承 env（execute_python / run_bash 都是 subprocess）。这条
    不成立，模型可控的代码就能读到审图凭据 —— 2026-08-22 之前的真实状态，
    成因是透传白名单认识那个变量名而擦除名单不认识。
    """
    monkeypatch.setenv(
        "HARNESS_MODEL_ROLES",
        _channel(
            visual_review={
                "provider": "openai_compatible",
                "model": "some-vlm",
                "base_url": "https://vendor.example/v1",
                "api_key": "sk-vision-secret",
            }
        ),
    )
    model_roles.install_from_environment()

    import os

    assert "HARNESS_MODEL_ROLES" not in os.environ
    assert "sk-vision-secret" not in json.dumps(dict(os.environ))
    # 但进程内仍然拿得到 —— 不是丢了，是搬了家。
    assert model_roles.require("visual_review").api_key == "sk-vision-secret"


def test_a_binding_never_prints_its_key():
    """binding 被日志/异常打印出来时不该顺手带出凭据。"""
    binding = model_roles.RoleBinding(
        role="visual_review", provider="p", model="m", base_url="https://x/v1"
    )
    runtime_secrets.install(binding.secret_name, "sk-should-not-appear")
    assert "sk-should-not-appear" not in repr(binding)
    assert "api_key" not in binding.to_public_dict()
    assert binding.to_public_dict()["api_key_set"] is True


def test_missing_role_points_at_something_the_user_can_actually_do(monkeypatch):
    """报错必须指向能被解决的那件事。

    从前审图缺凭据时的说法是 "missing environment variable ICOMPIFY_API_KEY"
    —— 而平台路径上用户根本没有能填这个变量的地方，照着它去翻 .env 是白费
    时间。现在必须点名角色、给出设置入口，并带上目录里的出路。
    """
    monkeypatch.setenv(
        "HARNESS_MODEL_ROLES",
        _channel(
            reasoning={
                "provider": "deepseek",
                "model": "m",
                "base_url": "https://x/v1",
                "api_key": "k",
            }
        ),
    )
    with pytest.raises(model_roles.ModelRoleUnavailable) as caught:
        model_roles.require("visual_review")
    message = str(caught.value)
    assert "visual_review" in message
    assert "设置" in message
    # 出路必须带出来，而不是只说"不可用"。审图这个槽的出路 2026-08-23 变了：
    # 从"你显式降级 quick"变成"工具面上没有它、finalize 自动按 draft 交付"，
    # 所以判据钉在**说清楚会发生什么**上，不钉某个词。
    assert "draft" in message and "工具面" in message
    assert "ICOMPIFY_API_KEY" not in message


def test_an_unknown_role_lists_the_legal_values():
    """合法取值必须随报错交出来 —— 别让调用方猜一个名字。"""
    with pytest.raises(model_roles.ModelRoleError) as caught:
        model_roles.resolve("hallucinated_role")
    assert "visual_review" in str(caught.value)
    assert "reasoning" in str(caught.value)


def test_a_broken_channel_is_loud_and_does_not_fall_back(monkeypatch):
    """坏配置不许退回 LLM_* 合成。

    静默退回 = 人在设置页对着一条正确的记录找不出毛病。宁可全场失败得
    吵一点。
    """
    monkeypatch.setenv("HARNESS_MODEL_ROLES", "{not json")
    monkeypatch.setenv("LLM_API_KEY", "sk-cli")
    monkeypatch.setenv("LLM_BASE_URL", "https://cli.example/v1")
    monkeypatch.setenv("LLM_MODEL", "cli-model")

    assert model_roles.resolve("reasoning") is None
    assert model_roles.delivery_error()
    section = "\n".join(model_roles.render_role_section())
    assert "⚠️" in section


def test_the_cli_entry_still_works_without_a_channel(monkeypatch):
    """没有通道时从 LLM_* 合成 reasoning —— 一个入口，不是第二个真相源。"""
    monkeypatch.setenv("LLM_API_KEY", "sk-cli")
    monkeypatch.setenv("LLM_BASE_URL", "https://cli.example/v1")
    monkeypatch.setenv("LLM_MODEL", "cli-model")
    monkeypatch.setenv("LLM_CONTEXT_WINDOW", "200000")

    binding = model_roles.require("reasoning")
    assert binding.model == "cli-model"
    assert binding.context_window_tokens == 200000
    assert binding.api_key == "sk-cli"


def test_the_injected_section_lists_missing_roles_too(monkeypatch):
    """缺的角色也要出现在开局注入里，并带着出路。

    只列可用的 = 节点开工之后才发现缺口。实测代价：postprocess 渲染完三张
    publication 图，才在 finalize 处知道审图不可用。
    """
    monkeypatch.setenv(
        "HARNESS_MODEL_ROLES",
        _channel(
            reasoning={
                "provider": "deepseek",
                "model": "ds",
                "base_url": "https://x/v1",
                "api_key": "k",
            }
        ),
    )
    section = "\n".join(model_roles.render_role_section())
    assert "visual_review" in section
    assert "未配置" in section
    assert "draft" in section, "缺口要连同后果一起说，不能只说缺"


def test_a_role_the_harness_does_not_know_is_ignored(monkeypatch):
    """平台发来目录之外的角色 = 两边版本不同步。忽略，不当成支持。"""
    monkeypatch.setenv(
        "HARNESS_MODEL_ROLES",
        _channel(
            from_a_newer_platform={
                "provider": "p",
                "model": "m",
                "base_url": "https://x/v1",
                "api_key": "k",
            }
        ),
    )
    model_roles.install_from_environment()
    assert model_roles.bound_roles() == {}


def test_a_half_configured_role_counts_as_unconfigured(monkeypatch):
    """缺地址的绑定不是"配了一半"，是没配。

    半成品会让消费方走进"有绑定但调不通"的分支，而那条分支的报错指向网络
    或 provider —— 指错方向比不报错更费时间。
    """
    monkeypatch.setenv(
        "HARNESS_MODEL_ROLES",
        _channel(
            visual_review={
                "provider": "p",
                "model": "m",
                "base_url": "",
                "api_key": "k",
            }
        ),
    )
    assert model_roles.resolve("visual_review") is None


def test_a_self_hosted_endpoint_without_a_key_is_configured(monkeypatch):
    """没有 key 的自建端点是**配好了**的：平台只在端点确实不要 key 时这样发（探过）。

    2026-09-24 真跑：组织提供了一个不鉴权的自建模型，平台照发、这里照丢 —— 组织项目一句话都
    跑不了，报的是「当前没有可用后端」。`LLMClient` 早在 09-15 就不拿 key 当门槛了。
    """
    monkeypatch.setenv(
        "HARNESS_MODEL_ROLES",
        _channel(
            reasoning={
                "provider": "openai_compatible",
                "model": "smoke",
                "base_url": "http://gpu-box:8000/v1",
                "api_key": "",
            }
        ),
    )
    binding = model_roles.resolve("reasoning")
    assert binding is not None, "不鉴权的自建端点被当成没配 —— 会话一开就「没有可用后端」"
    assert binding.base_url.startswith("http://gpu-box:8000") and binding.api_key == ""

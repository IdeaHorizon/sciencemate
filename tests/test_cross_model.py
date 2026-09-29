"""cross-model 工具测试：list_alternative_models + consult_other_model。

不真打外部 LLM API —— monkeypatch LLMClient.chat 让单测自洽。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from core import model_roles
from core.llm_providers import (
    ProviderSpec,
    _parse_one,
    _role_providers,
    get_provider,
    list_providers,
)
from core.state import State

# ── ProviderSpec 解析 ───────────────────────────────────────────────────────


def test_parse_one_valid():
    item = {
        "name": "claude", "model": "claude-opus-4-7",
        "base_url": "https://api.anthropic.com",
        "api_key_env": "ANTHROPIC_API_KEY",
        "description": "second opinion",
    }
    spec = _parse_one(0, item)
    assert spec is not None
    assert spec.name == "claude"
    assert spec.base_url == "https://api.anthropic.com"     # /v1 后缀已被去掉？不，这里没 /v1
    assert spec.description == "second opinion"


def test_parse_one_strips_trailing_slash_in_base_url():
    item = {
        "name": "c", "model": "m",
        "base_url": "https://api.example.com/",  # trailing slash
        "api_key_env": "X_API_KEY",
    }
    spec = _parse_one(0, item)
    assert spec.base_url == "https://api.example.com"


def test_parse_one_missing_required_returns_none(caplog):
    item = {"name": "c", "base_url": "x", "api_key_env": "Y"}    # 缺 model
    spec = _parse_one(0, item)
    assert spec is None
    assert any("model" in r.message for r in caplog.records)


def test_parse_one_wrong_type_returns_none(caplog):
    item = {
        "name": 123, "model": "m", "base_url": "x", "api_key_env": "Y",
    }
    spec = _parse_one(0, item)
    assert spec is None


def test_parse_one_not_dict_returns_none(caplog):
    spec = _parse_one(0, "not a dict")
    assert spec is None


# ── 主模型（reasoning 角色）提取 ────────────────────────────────────────────
#
# 2026-08-22：这一节原来测的是 `_primary_provider_from_env()` —— 从 LLM_* 直接
# 派生一个叫 "primary" 的 provider。模型角色进场后它没了：provider 列表与平台
# 配置读同一份 `model_roles`，主模型只是 `reasoning` 这个普通角色。
# 断言跟着换的是**名字**（角色 id，不再是模型名），问的还是同三件事：
# 齐了给一条 / 缺一就没有 / base_url 规范化。


def test_reasoning_role_from_env_complete(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "sk-test")
    monkeypatch.setenv("LLM_BASE_URL", "https://api.example.com")
    monkeypatch.setenv("LLM_MODEL", "test-model-x")
    provs = _role_providers()
    assert len(provs) == 1
    p = provs[0]
    assert p.name == model_roles.REASONING_ROLE
    assert p.model == "test-model-x"
    # 凭据键名是 runtime_secrets 里的键，不是环境变量名 —— 平台路径上
    # os.environ 里根本没有它。
    assert p.api_key_env == model_roles.require("reasoning").secret_name


def test_reasoning_role_missing_key_yields_no_provider(monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.setenv("LLM_BASE_URL", "https://x")
    monkeypatch.setenv("LLM_MODEL", "m")
    assert _role_providers() == []


def test_reasoning_base_url_strips_v1_suffix(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "k")
    monkeypatch.setenv("LLM_BASE_URL", "https://api.example.com/v1")
    monkeypatch.setenv("LLM_MODEL", "m")
    assert _role_providers()[0].base_url == "https://api.example.com"


# ── list_providers / get_provider ──────────────────────────────────────────


def test_list_providers_empty_env(monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)
    monkeypatch.delenv("LLM_PROVIDERS_JSON", raising=False)
    assert list_providers() == []


def test_list_providers_only_primary(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "k")
    monkeypatch.setenv("LLM_BASE_URL", "https://api.x.com")
    monkeypatch.setenv("LLM_MODEL", "main-model")
    monkeypatch.delenv("LLM_PROVIDERS_JSON", raising=False)
    provs = list_providers()
    assert len(provs) == 1
    assert provs[0].name == model_roles.REASONING_ROLE
    assert provs[0].model == "main-model"


def test_list_providers_primary_plus_extra(monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "k")
    monkeypatch.setenv("LLM_BASE_URL", "https://api.x.com")
    monkeypatch.setenv("LLM_MODEL", "main-model")
    monkeypatch.setenv("LLM_PROVIDERS_JSON", json.dumps([
        {"name": "claude", "model": "claude-opus", "base_url": "https://a.c",
         "api_key_env": "ANTHROPIC_API_KEY"},
        {"name": "gpt-4o", "model": "gpt-4o", "base_url": "https://o.ai",
         "api_key_env": "OPENAI_API_KEY"},
    ]))
    provs = list_providers()
    names = [p.name for p in provs]
    assert names == [model_roles.REASONING_ROLE, "claude", "gpt-4o"]


def test_list_providers_skips_duplicate_name(monkeypatch, caplog):
    monkeypatch.setenv("LLM_API_KEY", "k")
    monkeypatch.setenv("LLM_BASE_URL", "https://x")
    monkeypatch.setenv("LLM_MODEL", "main")
    monkeypatch.setenv("LLM_PROVIDERS_JSON", json.dumps([
        # 跟主模型那条重名 —— 主模型现在叫角色 id（reasoning），不叫模型名
        {"name": "reasoning", "model": "m", "base_url": "https://other",
         "api_key_env": "OTHER_KEY"},
    ]))
    provs = list_providers()
    assert len(provs) == 1
    assert any("重名" in r.message for r in caplog.records)


def test_list_providers_malformed_json_returns_primary(monkeypatch, caplog):
    monkeypatch.setenv("LLM_API_KEY", "k")
    monkeypatch.setenv("LLM_BASE_URL", "https://x")
    monkeypatch.setenv("LLM_MODEL", "main")
    monkeypatch.setenv("LLM_PROVIDERS_JSON", "[{this is broken json}")
    provs = list_providers()
    assert len(provs) == 1
    assert provs[0].name == model_roles.REASONING_ROLE
    assert any("不是合法 JSON" in r.message for r in caplog.records)


def test_list_providers_not_array_returns_primary(monkeypatch, caplog):
    monkeypatch.setenv("LLM_API_KEY", "k")
    monkeypatch.setenv("LLM_BASE_URL", "https://x")
    monkeypatch.setenv("LLM_MODEL", "main")
    monkeypatch.setenv("LLM_PROVIDERS_JSON", '{"not": "array"}')
    provs = list_providers()
    assert len(provs) == 1
    assert any("必须是 JSON 数组" in r.message for r in caplog.records)


def test_list_providers_one_bad_entry_keeps_others(monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.setenv("LLM_PROVIDERS_JSON", json.dumps([
        {"name": "good", "model": "m", "base_url": "https://x",
         "api_key_env": "Y"},
        {"name": "bad"},  # 缺 model/base_url/api_key_env
        {"name": "good2", "model": "m2", "base_url": "https://y",
         "api_key_env": "Z"},
    ]))
    provs = list_providers()
    names = [p.name for p in provs]
    assert names == ["good", "good2"]


def test_get_provider_finds_by_name(monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.setenv("LLM_PROVIDERS_JSON", json.dumps([
        {"name": "claude", "model": "m", "base_url": "https://a",
         "api_key_env": "Y"},
    ]))
    p = get_provider("claude")
    assert p is not None
    assert p.name == "claude"
    assert get_provider("not-exists") is None
    assert get_provider("") is None


def test_provider_to_summary_includes_api_key_set(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-real")
    spec = ProviderSpec("c", "m", "https://x", "ANTHROPIC_API_KEY", "desc")
    d = spec.to_summary_dict(include_status=True)
    assert d["api_key_set"] is True
    # 不应该有 api_key 字段（安全）
    assert "api_key" not in d


def test_provider_to_summary_api_key_not_set(monkeypatch):
    monkeypatch.delenv("MISSING_KEY", raising=False)
    spec = ProviderSpec("c", "m", "https://x", "MISSING_KEY")
    d = spec.to_summary_dict()
    assert d["api_key_set"] is False


def _make_state(tmp_path: Path) -> State:
    return State.new(node_type="literature", base_dir=tmp_path,
                       project_id="p_cross_model")


# ── consult_other_model 工具 ────────────────────────────────────────────────


def test_consult_tool_description_lists_providers():
    """v1.5 (revised): description 启动时嵌入 provider 列表，LLM 一眼可见
    不需要单独的 list 工具。"""
    from core.bootstrap import bootstrap
    bootstrap()
    from core.tool_registry import get_tool
    t = get_tool("consult_other_model")
    assert t is not None
    # description 应该提到 "provider" 或 "注册"
    assert ("provider" in t.description.lower()
            or "注册" in t.description)


def test_consult_tool_description_rebuilt_via_helper(monkeypatch):
    """直接调 _build_description() 验证它读 env + 嵌入注册项。"""
    from shared.tools.library.cross_model import _build_description
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.setenv("LLM_PROVIDERS_JSON", json.dumps([
        {"name": "alpha", "model": "m-a", "base_url": "https://x",
         "api_key_env": "A_KEY",
         "description": "alpha test provider"},
    ]))
    desc = _build_description()
    assert "alpha" in desc
    assert "alpha test provider" in desc


def test_consult_tool_description_when_no_providers(monkeypatch):
    from shared.tools.library.cross_model import _build_description
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)
    monkeypatch.delenv("LLM_PROVIDERS_JSON", raising=False)
    desc = _build_description()
    assert "没有注册" in desc or "没有" in desc


@pytest.mark.asyncio
async def test_consult_missing_model_name(tmp_path):
    from core.bootstrap import bootstrap
    bootstrap()
    from core.tool_registry import execute as execute_tool
    state = _make_state(tmp_path)
    res = await execute_tool(
        "consult_other_model", state,
        model_name="", prompt="hi", reason="testing",
    )
    assert res["status"] == "error"
    assert "model_name" in res["error"]


@pytest.mark.asyncio
async def test_consult_missing_prompt(tmp_path):
    from core.bootstrap import bootstrap
    bootstrap()
    from core.tool_registry import execute as execute_tool
    state = _make_state(tmp_path)
    res = await execute_tool(
        "consult_other_model", state,
        model_name="x", prompt="", reason="testing this thing",
    )
    assert res["status"] == "error"
    assert "prompt" in res["error"]


@pytest.mark.asyncio
async def test_consult_short_reason_rejected(tmp_path):
    from core.bootstrap import bootstrap
    bootstrap()
    from core.tool_registry import execute as execute_tool
    state = _make_state(tmp_path)
    # 判决拆除：字数闸删——短理由放行（原样记账），空理由仍拒（语义必需）。
    res = await execute_tool(
        "consult_other_model", state,
        model_name="x", prompt="some prompt", reason="   ",
    )
    assert res["status"] == "error"


@pytest.mark.asyncio
async def test_consult_unknown_model_returns_list(tmp_path, monkeypatch):
    from core.bootstrap import bootstrap
    bootstrap()
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.setenv("LLM_PROVIDERS_JSON", json.dumps([
        {"name": "alpha", "model": "m", "base_url": "https://x",
         "api_key_env": "A_KEY"},
    ]))
    from core.tool_registry import execute as execute_tool
    state = _make_state(tmp_path)
    res = await execute_tool(
        "consult_other_model", state,
        model_name="not_registered", prompt="hello",
        reason="testing unknown model behavior",
    )
    assert res["status"] == "error"
    assert "未知" in res["error"]
    assert "available_models" in res
    assert "alpha" in res["available_models"]


@pytest.mark.asyncio
async def test_consult_missing_api_key(tmp_path, monkeypatch):
    from core.bootstrap import bootstrap
    bootstrap()
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("FAKE_PROVIDER_KEY", raising=False)
    monkeypatch.setenv("LLM_PROVIDERS_JSON", json.dumps([
        {"name": "fakep", "model": "m", "base_url": "https://x",
         "api_key_env": "FAKE_PROVIDER_KEY"},
    ]))
    from core.tool_registry import execute as execute_tool
    state = _make_state(tmp_path)
    res = await execute_tool(
        "consult_other_model", state,
        model_name="fakep", prompt="hello",
        reason="testing missing api key",
    )
    assert res["status"] == "error"
    assert "FAKE_PROVIDER_KEY" in res["error"]


@pytest.mark.asyncio
async def test_consult_success_with_mocked_chat(tmp_path, monkeypatch):
    from core.bootstrap import bootstrap
    bootstrap()
    # 注册一个 fake provider + fake key
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.setenv("FAKE_KEY", "sk-fake-real")
    monkeypatch.setenv("LLM_PROVIDERS_JSON", json.dumps([
        {"name": "fakep", "model": "fake-model", "base_url": "https://api.fake",
         "api_key_env": "FAKE_KEY"},
    ]))

    # patch LLMClient.chat 返 fake response
    from core import llm as llm_mod
    from core.llm import LLMResponse

    async def fake_chat(self, messages, *, tools=None, max_tokens=4096,
                         temperature=0.5, timeout=None, _no_cache=False):
        return LLMResponse(
            content="fake answer from second model",
            tool_calls=[],
            finish_reason="stop",
            usage={"prompt_tokens": 10, "completion_tokens": 8, "total_tokens": 18},
            reasoning_content=None,
        )
    monkeypatch.setattr(llm_mod.LLMClient, "chat", fake_chat)

    from core.tool_registry import execute as execute_tool
    state = _make_state(tmp_path)
    res = await execute_tool(
        "consult_other_model", state,
        model_name="fakep", prompt="What is X?",
        reason="cross-checking main model's claim about X",
    )
    assert res["status"] == "success"
    assert res["model_used"] == "fakep"
    assert res["content"] == "fake answer from second model"
    assert res["finish_reason"] == "stop"
    assert res["usage"]["completion_tokens"] == 8
    assert "reason_logged" in res
    assert "cross-check" in res["reason_logged"]


# ── yaml integration：10 个节点工具白名单含两 tools ──────────────────────────


@pytest.mark.parametrize("node_name", [
    "literature", "hypothesis", "data", "experiment",
    "writing",
    "_orchestrator", "_curator", "_reviewer",
])
def test_node_has_consult_tool(node_name):
    """工具白名单只对**会把工具面交给模型**的节点有意义。

    换掉了框架 loop 的节点（`core/executor.py` 直接 dispatch custom loop）没有
    轮循环、不构造 tool schema、内部按函数名直接 import 调用 —— 它的
    `tools:` 列表谁都不读。在那儿写一行 `consult_other_model` 不会让任何模型
    多出一个"第二意见"能力，只会让这条测试变绿。判据用 `runs_the_framework_loop()`
    现算，不写节点名：换回框架 loop 的那天，这条当天重新对它生效。
    """
    from core.custom_loop import runs_the_framework_loop
    from core.loader import load_harness

    if not runs_the_framework_loop(node_name):
        pytest.skip(f"{node_name} 用 custom loop：没有工具面，白名单不被任何人读")
    h = load_harness(node_name)
    assert "consult_other_model" in h.tools, (
        f"{node_name} 工具白名单应含 consult_other_model（v1.5）"
    )


def test_postprocess_uses_only_its_fixed_visual_reviewer_backend():
    """Scientific Visualization cannot bypass its calibrated reviewer contract."""
    from core.loader import load_harness

    harness = load_harness("postprocess")
    assert "consult_other_model" not in harness.tools
    assert "render_figure" in harness.tools
    assert "request_visual_review" not in harness.tools

"""LLM_PROVIDERS_JSON 解析器 —— 加载注册的备用 LLM provider 列表。

`.env` 里 `LLM_PROVIDERS_JSON` 是 JSON 字符串（数组），每个元素：

    {
      "name":         "claude-opus-4-7",          # 必填，调用时用的 ID
      "model":        "claude-opus-4-7",          # 必填，传给 provider 的模型名
      "base_url":     "https://api.anthropic.com", # 必填，OpenAI-compatible API
      "api_key_env":  "ANTHROPIC_API_KEY",         # 必填，从该 env var 取 key
                                                    # （**不**把 key 直接放 JSON 里）
      "description":  "Anthropic 旗舰..."          # 可选，给 LLM 看的说明
    }

设计原则：
  - api key 走单独 env var（API_KEY 跟 provider 元数据分离，安全 + 易换）
  - 加载延迟到第一次访问（启动时若 env 缺失不阻塞）
  - schema 校验严：缺字段 / 错类型 → 该条目报 invalid（不静默吞）；其它条
    目仍可用（向前兼容）
  - 跟主 LLMClient 平行：当前主模型（LLM_API_KEY/LLM_BASE_URL/LLM_MODEL）
    自动作为 "primary" 加入列表头部，name 取 LLM_MODEL；调用 primary 等价
    于走主链路（不绕 ）。

跟 cross-model 工具的关系：
  - `list_alternative_models()` 工具调本模块的 `list_providers()`
  - `consult_other_model(model_name=...)` 工具调本模块的 `get_provider(name)`
    + 构 LLMClient 真打 API
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Any

from .runtime_secrets import get as _runtime_secret

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProviderSpec:
    """一个注册的 LLM provider。"""
    name: str
    model: str
    base_url: str
    api_key_env: str
    description: str = ""

    def to_summary_dict(self, include_status: bool = True) -> dict[str, Any]:
        """给 LLM 看的精简 dict（不含 api_key 自身）。"""
        out: dict[str, Any] = {
            "name": self.name,
            "model": self.model,
            "base_url": self.base_url,
            "api_key_env": self.api_key_env,
        }
        if self.description:
            out["description"] = self.description
        if include_status:
            out["api_key_set"] = bool(
                os.getenv(self.api_key_env) or _runtime_secret(self.api_key_env)
            )
        return out


def _parse_one(idx: int, item: Any) -> ProviderSpec | None:
    """校验 + 解析 1 条 provider entry；invalid 返 None + warn。"""
    if not isinstance(item, dict):
        log.warning(
            "LLM_PROVIDERS_JSON[%d]: 必须是 dict, got %s",
            idx, type(item).__name__,
        )
        return None

    required = ("name", "model", "base_url", "api_key_env")
    missing = [k for k in required if not item.get(k)]
    if missing:
        log.warning(
            "LLM_PROVIDERS_JSON[%d]: 缺字段 %s, item=%r", idx, missing, item,
        )
        return None

    for k in required:
        if not isinstance(item[k], str):
            log.warning(
                "LLM_PROVIDERS_JSON[%d].%s: 必须是 string, got %s",
                idx, k, type(item[k]).__name__,
            )
            return None

    desc = item.get("description") or ""
    if not isinstance(desc, str):
        log.warning(
            "LLM_PROVIDERS_JSON[%d].description: 必须是 string, got %s",
            idx, type(desc).__name__,
        )
        desc = ""

    return ProviderSpec(
        name=item["name"].strip(),
        model=item["model"].strip(),
        base_url=item["base_url"].rstrip("/"),
        api_key_env=item["api_key_env"].strip(),
        description=desc.strip(),
    )


def _role_providers() -> list[ProviderSpec]:
    """已绑定的**模型角色**，每个角色一条 provider。

    此前这里只从 LLM_* env 派生一个 "primary"，于是 `list_alternative_models`
    看到的世界与平台真正配置的世界是两份。现在两者读同一个 `model_roles`：
    角色即 provider，reasoning 在头部。

    `api_key_env` 保留字段名以兼容既有消费方，但值是 runtime_secrets 里的
    键名 —— 平台路径上凭据从不落在 os.environ 里。
    """
    from . import model_roles

    bound = model_roles.bound_roles()
    ordered = sorted(bound.values(), key=lambda b: (b.role != model_roles.REASONING_ROLE, b.role))
    out: list[ProviderSpec] = []
    for binding in ordered:
        role_spec = model_roles.spec(binding.role)
        out.append(
            ProviderSpec(
                name=binding.role,
                model=binding.model,
                base_url=binding.base_url,
                api_key_env=binding.secret_name,
                description=(role_spec.title if role_spec else binding.role),
            )
        )
    return out


def list_providers() -> list[ProviderSpec]:
    """返回所有可用 provider（primary 在头）。

    每次调用都重新读 env，让 .env 改后立即生效（不缓存）。
    """
    out: list[ProviderSpec] = _role_providers()

    raw = os.getenv("LLM_PROVIDERS_JSON", "").strip()
    if not raw:
        return out

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        log.warning(
            "LLM_PROVIDERS_JSON 不是合法 JSON：%s。除 primary 外其它 "
            "provider 不可用。", e,
        )
        return out

    if not isinstance(parsed, list):
        log.warning(
            "LLM_PROVIDERS_JSON 必须是 JSON 数组, got %s。除 primary 外其它 "
            "provider 不可用。", type(parsed).__name__,
        )
        return out

    seen_names = {p.name for p in out}
    for idx, item in enumerate(parsed):
        spec = _parse_one(idx, item)
        if spec is None:
            continue
        if spec.name in seen_names:
            log.warning(
                "LLM_PROVIDERS_JSON[%d]: name=%r 跟 primary 或前面的 "
                "provider 重名 —— 跳过", idx, spec.name,
            )
            continue
        out.append(spec)
        seen_names.add(spec.name)

    return out


def get_provider(name: str) -> ProviderSpec | None:
    """按 name 找 provider，找不到返 None。"""
    if not name:
        return None
    for p in list_providers():
        if p.name == name:
            return p
    return None

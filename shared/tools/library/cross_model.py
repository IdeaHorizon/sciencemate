"""cross-model 工具 —— 让当前 agent 调其它注册 LLM 拿"第二意见"。

单工具 `consult_other_model(model_name, prompt, reason)`：

  - 可用 provider 列表在 description 里**启动时嵌入**（从 .env LLM_PROVIDERS_JSON
    + 主模型 LLM_MODEL 派生）。LLM 一眼能看，不需要先调"列表工具"
  - model_name 错时 error response 含 `available_models`，agent 也能动态发现
  - 必填 reason（非空，说清为什么）强制可审计，防 token 滥用

用例：
  - 选实验方法 / 写 manuscript 关键段时，让另一个家族的模型（reasoning vs
    fast、大 vs 小）拿一个独立判断
  - 长 context 主模型推理慢时，让小模型先草拟，再把草稿喂主模型 refine
  - cross-check：主模型给出强结论时，问另一家是否同意（防 model-family bias）

跟主 agent_loop 的区别：
  - 不消耗主 agent 的 turn 预算（独立 LLM call，结果作为 tool_result 返回）
  - 不写 KB / artifact / memory（只返文本）—— agent 自己决定怎么用答复
"""
from __future__ import annotations

import os
import time
from typing import Any

from core.llm import LLMClient, LLMMessage, opening_system_prompt
from core.llm_providers import list_providers, get_provider
from core.state import State
from core.tool_registry import ToolDefinition, register_tool


def _build_description() -> str:
    """启动时构造 tool description —— 把当前注册的 provider 列表嵌进去，
    LLM 看一眼就知道有哪些可选。"""
    base = (
        "**向另一个注册 LLM provider 打 1 次 call 拿独立第二意见**。"
        "不消耗当前 agent turn 预算（独立 LLM call，结果回 tool_result）。\n\n"
        "**何时调**：\n"
        "  - 关键决策时想 cross-check 主模型的结论是否 family-biased\n"
        "  - 主模型推理慢时让小模型快速草拟\n"
        "  - 写关键段（manuscript / claim）想要不同模型 phrasing 参考\n\n"
        "**何时不调**（避免浪费 token）：\n"
        "  - 微观决策 / 工具调用细节 → 主模型自己想\n"
        "  - 信息查询（'X 是啥'）→ search_kb / 工具去查\n"
        "  - 已经在 search_kb / memory_recall 拿到答案 → 别再问\n\n"
        "**3 个必填参数**：\n"
        "  - model_name: 下面列出的 provider name 之一\n"
        "  - prompt:     给目标模型的具体问题\n"
        "  - reason:     非空，说清为啥选这个模型（强制可审计 防滥用）\n\n"
    )

    provs = list_providers()
    if not provs:
        # 没注册任何 provider —— 工具仍上架但调不通；description 说明
        return base + (
            "**当前没有注册的 provider**（.env 缺 LLM_API_KEY/LLM_BASE_URL/"
            "LLM_MODEL，且没设 LLM_PROVIDERS_JSON）。framework 启动后调本工具会返"
            " status=error。让 user / framework owner 配 .env。"
        )

    lines = ["**当前注册的 provider**（启动时从 .env 读，列表稳定）："]
    for p in provs:
        # name + (model) + description + key 状态
        line = f"  - `{p.name}`"
        if p.model != p.name:
            line += f" (model={p.model})"
        if p.description:
            line += f" —— {p.description}"
        key_set = bool(os.getenv(p.api_key_env))
        if not key_set:
            line += f"  [⚠️ {p.api_key_env} 未设，调用会失败]"
        lines.append(line)

    return base + "\n".join(lines)


async def _consult_other_model(
    state: State,
    model_name: str,
    prompt: str,
    reason: str,
    system_prompt: str | None = None,
    max_tokens: int = 4096,
    temperature: float = 0.5,
    **_: Any,
) -> dict:
    """向指定注册 provider 打 1 次 LLM call，返其答复。

    Args:
      model_name: 必填。从 description 里列出的 provider name 选。
      prompt:     必填。给目标模型的 user message。
      reason:     必填。**为啥选这个模型 / 为啥需要 cross-check**。强制写理由
                  防滥用 token；transcript 里记录便于事后审计。
      system_prompt: 可选。默认 "second opinion research assistant"。
      max_tokens: 默认 4096。
      temperature: 默认 0.5（cross-check 偏 deterministic）。

    Returns:
      success → {status, model_used, content, reasoning_content?, usage,
                   finish_reason, elapsed_s, reason_logged}
      error   → {status, error, available_models? (when model_name unknown)}
    """
    # model_name / prompt / reason 的必填与非空由 parameters_schema 声明
    # （required + minLength:1），派发口核一次，这里不再手写。
    provider = get_provider(model_name)
    if provider is None:
        return {
            "status": "error",
            "error": f"未知 model_name={model_name!r}。",
            "available_models": [p.name for p in list_providers()],
            "hint": "工具 description 列出了所有可用 provider name。",
        }

    api_key = os.getenv(provider.api_key_env, "").strip()
    if not api_key:
        return {
            "status": "error",
            "error": (
                f"provider {model_name!r} 的 API key 未设 "
                f"(env var {provider.api_key_env} 是空)。"
            ),
        }

    # 用 LLMClient 直接构 + 调（不用全局单例 —— 每次新建避免 state pollution）
    client = LLMClient(
        api_key=api_key,
        base_url=provider.base_url,
        model=provider.model,
    )

    sys_msg = system_prompt or (
        "You are a research assistant providing an independent second opinion. "
        "Be concise, specific, and flag any disagreement with the framing you "
        "received explicitly."
    )

    messages = [
        opening_system_prompt(sys_msg),
        LLMMessage(role="user", content=prompt),
    ]

    state.append_transcript(
        "consult_other_model_call",
        model_name=model_name,
        reason=reason[:300],
        prompt_preview=prompt[:200],
        sys_prompt_preview=sys_msg[:200],
    )

    t0 = time.monotonic()
    try:
        resp = await client.chat(
            messages,
            tools=None,
            max_tokens=int(max_tokens),
            temperature=float(temperature),
        )
    except Exception as e:
        elapsed = time.monotonic() - t0
        state.append_transcript(
            "consult_other_model_error",
            model_name=model_name,
            error_type=type(e).__name__,
            error_msg=str(e)[:300],
            elapsed_s=round(elapsed, 2),
        )
        return {
            "status": "error",
            "error": f"{type(e).__name__}: {str(e)[:500]}",
            "model_name": model_name,
            "elapsed_s": round(elapsed, 2),
        }
    elapsed = time.monotonic() - t0

    out: dict[str, Any] = {
        "status": "success",
        "model_used": provider.name,
        "content": resp.content,
        "finish_reason": resp.finish_reason,
        "usage": resp.usage or {},
        "elapsed_s": round(elapsed, 2),
        "reason_logged": reason[:200],
    }
    if resp.reasoning_content:
        out["reasoning_content"] = resp.reasoning_content

    state.append_transcript(
        "consult_other_model_result",
        model_name=model_name,
        finish_reason=resp.finish_reason,
        content_preview=(resp.content or "")[:200],
        usage=resp.usage or {},
        elapsed_s=round(elapsed, 2),
    )

    return out


register_tool(
    ToolDefinition(
        name="consult_other_model",
        description=_build_description(),
        parameters_schema={
            "type": "object",
            "properties": {
                "model_name": {
                    "type": "string", "minLength": 1,
                    "description": "目标 provider 的 name（看 description 里列出的）。"
                },
                "prompt": {
                    "type": "string", "minLength": 1,
                    "description": "给目标模型的具体问题 / cross-check 内容。"
                },
                "reason": {
                    "type": "string", "minLength": 1,
                    "description": "非空，说清为啥选这个模型 / 为啥需要 cross-check（可审计 防滥用）。"
                },
                "system_prompt": {
                    "type": "string",
                    "description": (
                        "可选 system message。默认 \"second opinion research assistant\"。"
                    ),
                },
                "max_tokens": {
                    "type": "integer", "default": 4096,
                    "minimum": 64, "maximum": 32768,
                },
                "temperature": {
                    "type": "number", "default": 0.5,
                    "minimum": 0.0, "maximum": 2.0,
                },
            },
            "required": ["model_name", "prompt", "reason"],
        },
        risk_level="low",
    ),
    _consult_other_model,
)

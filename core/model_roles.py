"""模型角色 —— "谁来干这件事" 的唯一真相源。

## 为什么有这个模块

在此之前，"平台上有哪些模型、各自干什么、凭据在哪"这一个问题有**九份抄件**：
DB 里的 model_backend_configs、Python 与 TypeScript 各一份 provider 词表、
provider→settings 字段映射、bridge 的 env 透传白名单、worker 的凭据擦除名单、
LLM_PROVIDERS_JSON、节点里的 reviewer/backend.yaml，以及把同一份默认值又写
一遍的 ReviewerConfig dataclass。

代价不是"重复"这种审美问题，是两处真实故障：

  1. **审图能力对用户完全不可见。** UI 上"选模型"只选主模型；审图 VLM 的
     provider/model/base_url 写死在节点里，key 只从一个写死的环境变量名读。
     想换、想注册、想知道它是谁 —— 都没有路径。缺了它，postprocess 渲染完
     三张 publication 图之后才在 finalize 处撞墙。
  2. **凭据泄漏面。** 透传白名单认识 ICOMPIFY_API_KEY，擦除名单不认识它。
     于是它进了 worker 的 os.environ 并留在那里，而 postprocess 有
     execute_python（subprocess，继承 env）。两张写死的名单各自演化，裂缝
     就是洞。

## 这里的模型

**角色（role）** = 一个能力槽，由**消费方**定义：某个节点或框架子系统需要
"一个能干某件事的模型"。目录在 `shared/model_roles.yaml`，平台按 HARNESS_ROOT
读同一份 —— 不是同步两份，是只有一份。

**绑定（binding）** = 平台为某个角色解析出的具体后端。整个进程只有**一条**
交付通道：`HARNESS_MODEL_ROLES`（JSON）。凭据随通道进来，进程启动时立刻搬进
runtime_secrets 并把通道变量从 env 抹掉 —— 要擦什么是**从通道扫出来**的，
不是另写一张名单。加一个角色不需要任何名单跟着改。

`reasoning`（主推理模型）就是一个普通角色，没有特权。LLM_API_KEY/LLM_BASE_URL/
LLM_MODEL 这组老环境变量只是 **CLI / 开发入口**的合成来源：没有通道时由它们
合成出一个 reasoning 绑定。两个入口在这里汇成一份内存事实，下游只认这一份。

## 失败方向（机制接缝五问过一遍）

  - **目录文件读不到** → 抛。空目录 = "一个角色都没有" = 所有 require 都失败
    但原因指向"没配置"，把 harness 自身残缺伪装成用户没设置。
  - **通道 JSON 坏了** → 吵：log warning + 注入段里显式 ⚠️，且**不**退回
    LLM_* 合成。坏掉的配置比没有配置更该被看见；静默退回会让人在设置页里
    对着一条正确的记录找不出毛病。
  - **角色没配** → `resolve()` 返 None、`require()` 抛带出路的异常。这是
    **局面**，不是错误 —— 消费方据此降级（见 absence_note），而不是重试。
  - **解除路径**：没有。角色可用性来自平台配置，模型不能给自己授权
    （与 core/runtime_capabilities 同一条原则：能力是进程属主的配置，不是
    节点输入）。
  - **主路径够得着**：节点走 require()，人走设置页，开局注入走
    render_role_section() —— 三者读的是同一次解析结果。
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from .runtime_secrets import get as _runtime_secret
from .runtime_secrets import install as _install_secret

log = logging.getLogger(__name__)

#: 平台 → worker 的唯一一条角色交付通道。
DELIVERY_ENV = "HARNESS_MODEL_ROLES"

#: 角色目录（平台按 HARNESS_ROOT 读同一个相对路径）。
CATALOG_RELATIVE_PATH = "shared/model_roles.yaml"

REASONING_ROLE = "reasoning"

#: 通道解析失败时记在这里，供注入段显式报出来（吞掉 = 用户看不见配置坏了）。
_delivery_error: str | None = None


class ModelRoleError(RuntimeError):
    """目录本身有问题 —— harness 残缺，不是用户没配置。"""


class ModelRoleUnavailable(RuntimeError):
    """这个角色此刻没有可用后端。消息里必须带**出路**，不只是"不可用"。"""

    def __init__(self, role_id: str, *, reason: str = "", absence_note: str = "") -> None:
        self.role_id = role_id
        self.reason = reason
        self.absence_note = absence_note
        parts = [f"模型角色 {role_id!r} 当前没有可用后端。"]
        if reason:
            parts.append(f"原因：{reason}")
        if absence_note:
            parts.append(f"后果与出路：{absence_note}")
        parts.append(
            f"这是确定性的配置缺失，重试无意义 —— 在平台『设置 → 模型』里给 "
            f"{role_id!r} 指派一条可用连接，或按上面的出路降级。"
        )
        super().__init__("\n".join(parts))


@dataclass(frozen=True)
class RoleSpec:
    """目录里的一条：这个槽是什么，缺了怎么办。"""

    id: str
    title: str
    description: str = ""
    modality: str = "text"
    required: bool = False
    absence_note: str = ""

    @property
    def needs_vision(self) -> bool:
        return self.modality == "vision"


@dataclass(frozen=True)
class RoleBinding:
    """平台为一个角色解析出的具体后端。

    `api_key` 不进 dataclass —— 它活在 runtime_secrets 里，按需取。这样一个
    被日志/repr/异常打印出来的 binding 不会顺手把凭据带出去。
    """

    role: str
    provider: str
    model: str
    base_url: str
    display_name: str = ""
    context_window_tokens: int | None = None

    @property
    def secret_name(self) -> str:
        return f"MODEL_ROLE_{self.role.upper()}_API_KEY"

    @property
    def api_key(self) -> str:
        return _runtime_secret(self.secret_name)

    def to_public_dict(self) -> dict[str, Any]:
        """给人/给模型看的形态。永远不含 key 本身。"""
        return {
            "role": self.role,
            "provider": self.provider,
            "model": self.model,
            "base_url": self.base_url,
            "display_name": self.display_name,
            "context_window_tokens": self.context_window_tokens,
            "api_key_set": bool(self.api_key),
        }


# ── 目录 ────────────────────────────────────────────────────────────────────


def catalog_path() -> Path:
    override = os.getenv("HARNESS_MODEL_ROLES_CATALOG")
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[1] / CATALOG_RELATIVE_PATH


@lru_cache(maxsize=1)
def _load_catalog(path_str: str) -> tuple[RoleSpec, ...]:
    path = Path(path_str)
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ModelRoleError(f"模型角色目录读不到：{path} —— {exc}") from exc
    except yaml.YAMLError as exc:
        raise ModelRoleError(f"模型角色目录不是合法 YAML：{path} —— {exc}") from exc
    if not isinstance(raw, dict) or not isinstance(raw.get("roles"), list):
        raise ModelRoleError(f"模型角色目录缺 roles 列表：{path}")
    specs: list[RoleSpec] = []
    seen: set[str] = set()
    for item in raw["roles"]:
        if not isinstance(item, dict) or not str(item.get("id") or "").strip():
            raise ModelRoleError(f"模型角色目录里有一条缺 id：{item!r}")
        role_id = str(item["id"]).strip()
        if role_id in seen:
            raise ModelRoleError(f"模型角色目录里 id 重复：{role_id!r}")
        seen.add(role_id)
        modality = str(item.get("modality") or "text").strip()
        if modality not in {"text", "vision"}:
            raise ModelRoleError(
                f"模型角色 {role_id!r} 的 modality={modality!r} 不认识（只有 text / vision）"
            )
        specs.append(
            RoleSpec(
                id=role_id,
                title=str(item.get("title") or role_id).strip(),
                description=" ".join(str(item.get("description") or "").split()),
                modality=modality,
                required=bool(item.get("required")),
                absence_note=" ".join(str(item.get("absence_note") or "").split()),
            )
        )
    return tuple(specs)


def catalog() -> tuple[RoleSpec, ...]:
    return _load_catalog(str(catalog_path()))


def spec(role_id: str) -> RoleSpec | None:
    return next((item for item in catalog() if item.id == role_id), None)


def require_spec(role_id: str) -> RoleSpec:
    found = spec(role_id)
    if found is None:
        known = ", ".join(item.id for item in catalog()) or "（空）"
        raise ModelRoleError(
            f"未知模型角色 {role_id!r}。合法取值：{known}。"
            f"新增角色请改 {CATALOG_RELATIVE_PATH}，不要在调用处编一个名字。"
        )
    return found


# ── 交付通道 ────────────────────────────────────────────────────────────────


def _parse_delivery(raw: str) -> tuple[dict[str, RoleBinding], dict[str, str], str | None]:
    """解析通道 → (bindings, 各角色的 key, 解析错误)。"""
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        return {}, {}, f"{DELIVERY_ENV} 不是合法 JSON：{exc}"
    if not isinstance(payload, dict):
        return {}, {}, f"{DELIVERY_ENV} 必须是 JSON object，得到 {type(payload).__name__}"

    bindings: dict[str, RoleBinding] = {}
    secrets: dict[str, str] = {}
    for role_id, item in payload.items():
        if not isinstance(item, dict):
            log.warning("%s[%s] 不是 object —— 跳过", DELIVERY_ENV, role_id)
            continue
        # 目录之外的角色一律忽略：平台发来一个 harness 不认识的槽，说明两边
        # 版本不同步，静默当成"支持"比忽略更危险。
        if spec(role_id) is None:
            log.warning(
                "%s 里的角色 %r 不在目录中（平台与 harness 版本不同步？）—— 忽略",
                DELIVERY_ENV, role_id,
            )
            continue
        model = str(item.get("model") or "").strip()
        base_url = str(item.get("base_url") or "").strip().rstrip("/")
        api_key = str(item.get("api_key") or "")
        # 缺模型名或地址才是"没配"。**没有 key 不算**：自建端点（vLLM / SGLang / Ollama）默认
        # 不鉴权，平台只在端点确实不要 key 时才这样发（它探过：`credential_is_optional` 且探针
        # 通过）。`LLMClient` 在 2026-09-15 就不再拿 key 当门槛了，这里漏了 —— 于是平台照发、
        # 这里照丢，一台跑得好好的自建端点在任何会话里都「当前没有可用后端」
        # （2026-09-24 真跑：组织提供的自建模型，组织项目一句话都跑不了）。
        if not (model and base_url):
            missing = [name for name, value in (("model", model), ("base_url", base_url)) if not value]
            log.warning("角色 %r 的绑定缺 %s —— 视为未配置", role_id, "/".join(missing))
            continue
        window = item.get("context_window_tokens")
        binding = RoleBinding(
            role=role_id,
            provider=str(item.get("provider") or "").strip(),
            model=model,
            base_url=base_url,
            display_name=str(item.get("display_name") or "").strip(),
            context_window_tokens=int(window) if isinstance(window, int) and window > 0 else None,
        )
        bindings[role_id] = binding
        secrets[binding.secret_name] = api_key
    return bindings, secrets, None


def _synthesize_reasoning_from_env() -> tuple[dict[str, RoleBinding], dict[str, str]]:
    """CLI / 开发入口：从 LLM_* 合成一个 reasoning 绑定。

    这**不是**第二个真相源 —— 它只在没有交付通道时把老环境变量抬进同一个
    内存结构，下游一律只读这个结构。平台路径永远走通道。
    """
    api_key = (os.getenv("LLM_API_KEY", "") or _runtime_secret("LLM_API_KEY")).strip()
    base_url = os.getenv("LLM_BASE_URL", "").strip().rstrip("/")
    # /v1 后缀在这里就削掉：下游一律自己拼 /v1/chat/completions。老的
    # `_primary_provider_from_env` 削过，收进角色层时丢了 —— LLMClient 自己
    # 也削一次所以看不出病，但那就是同一个规范化有两处实现，迟早分叉。
    if base_url.endswith("/v1"):
        base_url = base_url[:-3]
    model = os.getenv("LLM_MODEL", "").strip()
    if not (api_key and base_url and model):
        return {}, {}
    window = os.getenv("LLM_CONTEXT_WINDOW", "").strip()
    binding = RoleBinding(
        role=REASONING_ROLE,
        provider=os.getenv("LLM_PROVIDER", "").strip() or "openai_compatible",
        model=model,
        base_url=base_url,
        display_name=model,
        context_window_tokens=int(window) if window.isdigit() and int(window) > 0 else None,
    )
    return {REASONING_ROLE: binding}, {binding.secret_name: api_key}


_bindings: dict[str, RoleBinding] = {}
_installed = False
#: 通道来了没有。通道是**一次性**的（读完就把 env 抹掉），所以它一旦装上就是
#: 这个进程的终局；而 LLM_* 合成没有这个性质，见 `_ensure_installed`。
_from_channel = False
#: 上一次 LLM_* 合成读到的四元组，用来避免每次 resolve 都重装一遍 secret。
_env_signature: tuple[str, ...] | None = None


def install_from_environment(*, force: bool = False) -> None:
    """把交付通道读进进程，凭据搬进 runtime_secrets，并把通道从 env 抹掉。

    抹掉这一步是**擦除面**：要擦什么从通道扫出来（就是通道本身这一个变量），
    不是另写一张"哪些 env 是密钥"的名单。加角色不需要任何名单跟着改 ——
    此前那张名单漏掉审图 key，正是这类洞的来源。
    """
    global _installed, _from_channel, _delivery_error, _env_signature
    if _installed and not force:
        return
    raw = os.environ.pop(DELIVERY_ENV, "")
    if raw.strip():
        bindings, secrets, error = _parse_delivery(raw)
        _delivery_error = error
        _from_channel = True
        if error:
            # 坏掉的配置不退回 LLM_* 合成：静默退回 = 人在设置页对着一条
            # 正确的记录找不出毛病。宁可全场失败得吵一点。
            log.error("%s", error)
            bindings, secrets = {}, {}
    else:
        _delivery_error = None
        _from_channel = False
        bindings, secrets = _synthesize_reasoning_from_env()
        _env_signature = _env_fingerprint()

    for name, value in secrets.items():
        _install_secret(name, value)
    _bindings.clear()
    _bindings.update(bindings)
    _installed = True


def _env_fingerprint() -> tuple[str, ...]:
    return tuple(
        os.getenv(name, "")
        for name in ("LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL", "LLM_PROVIDER",
                     "LLM_CONTEXT_WINDOW")
    )


def _ensure_installed() -> None:
    """装一次；但 **LLM_* 合成那条路不冻结**。

    通道是一次性的（读完 env 就抹掉），装上即终局 —— 那条路缓存是对的。
    LLM_* 不是：它一直躺在 env 里，而"谁先碰到角色"决定了缓存内容。第一次
    触碰若发生在 `load_dotenv()` 之前（import 期的探活、CLI 起步顺序、测试
    进程里前一个用例），这个进程就永久记住"reasoning 没有后端"，而 .env 里
    明明写着 —— 报错还会理直气壮地叫人去设置页配一条它已经配好的连接。

    这正是[[同一函数两个采样时刻＝两个真相源]]那个形状：老的 `list_providers`
    文档里写着"每次调用都重新读 env，让 .env 改后立即生效（不缓存）"，收进
    角色层时把这条性质丢了。所以这里按**指纹**重算：env 没变就什么都不做。
    """
    global _installed
    if not _installed:
        install_from_environment()
        return
    if _from_channel:
        return
    if _env_fingerprint() != _env_signature:
        install_from_environment(force=True)


# ── 消费面 ──────────────────────────────────────────────────────────────────


def resolve(role_id: str) -> RoleBinding | None:
    """这个角色现在由谁来干。没配 → None（这是局面，不是错误）。"""
    require_spec(role_id)
    _ensure_installed()
    return _bindings.get(role_id)


def available(role_id: str) -> bool:
    return resolve(role_id) is not None


def require(role_id: str) -> RoleBinding:
    """拿绑定，没有就抛一个**带出路**的异常。"""
    binding = resolve(role_id)
    if binding is not None:
        return binding
    role_spec = require_spec(role_id)
    raise ModelRoleUnavailable(
        role_id,
        reason=_delivery_error or "",
        absence_note=role_spec.absence_note,
    )


def bound_roles() -> dict[str, RoleBinding]:
    _ensure_installed()
    return dict(_bindings)


def delivery_error() -> str | None:
    _ensure_installed()
    return _delivery_error


def render_role_section() -> list[str]:
    """注入 system prompt 的"模型角色"段。

    **缺失的角色也要列出来**，并把 absence_note 一字不改地带上。只列可用的
    等于让节点在开工之后才发现缺口 —— 那正是这次要消灭的成本：postprocess
    渲染完三张 publication 图，才在 finalize 处知道审图不可用。
    """
    _ensure_installed()
    lines: list[str] = []
    if _delivery_error:
        lines.append(f"⚠️ 模型角色配置解析失败（{_delivery_error}）—— 本次全部角色视为不可用。")
    try:
        specs = catalog()
    except ModelRoleError as exc:
        return lines + [f"⚠️ 模型角色目录不可用：{exc}"]
    lines.append("模型角色（框架按平台配置解析，非声明值）：")
    for item in specs:
        binding = _bindings.get(item.id)
        if binding is not None:
            lines.append(
                f"- ✅ {item.id}（{item.title}）：{binding.display_name or binding.model}"
                f"，model={binding.model}"
            )
        else:
            note = f" 出路：{item.absence_note}" if item.absence_note else ""
            lines.append(f"- ❌ {item.id}（{item.title}）：**未配置**。{note}")
    return lines

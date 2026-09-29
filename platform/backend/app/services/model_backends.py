"""Governance-aware model backend catalog and secret handling."""

import base64
import hashlib
import logging
from datetime import UTC, datetime
from uuid import UUID

from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.model_backend import ModelBackendConfig, UserModelBackendPreference
from app.models.user import User, UserRole
from app.services.model_role_catalog import (
    REASONING_ROLE,
    ModelRoleCatalogError,
    catalog,
)

log = logging.getLogger(__name__)

ENV_KEY_FIELDS = {
    "anthropic": "anthropic_api_key",
    "deepseek": "deepseek_api_key",
    "kimi": "kimi_api_key",
    "openai": "openai_api_key",
}
HARNESS_COMPATIBLE_PROVIDERS = frozenset(
    {"deepseek", "kimi", "openai", "openai_compatible", "local"}
)


def _fernet() -> Fernet:
    """当前主密钥。它从哪儿来、为什么不再是源码里那个常量，见
    `services/credential_key.py`。"""
    from app.services.credential_key import master_key

    return Fernet(master_key())


def _the_legacy_fernet() -> Fernet:
    """出厂常量派生的那把 —— **只用于解开存量**，任何新写入都不许用它。"""
    from app.services.credential_key import the_published_constant_key

    return Fernet(the_published_constant_key())


def encrypt_api_key(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()


def decrypt_api_key(value: str) -> str:
    """先试当前密钥，再试出厂密钥。

    换密钥不能让已经存着的凭据变成"解不开" —— 那对用户就是"我的 key 没了"。
    存量由启动时的 `rekey_legacy_credentials()` 换成当前密钥；这里的回退是为了
    换完之前的那一小段时间，以及换失败时不至于让人用不了。
    """
    try:
        return _fernet().decrypt(value.encode()).decode()
    except InvalidToken:
        pass
    try:
        return _the_legacy_fernet().decrypt(value.encode()).decode()
    except InvalidToken as exc:
        raise RuntimeError("Stored model credential cannot be decrypted") from exc


def was_encrypted_with_the_published_constant(value: str) -> bool:
    """这条密文是不是还用着源码里那个常量。"""
    try:
        _fernet().decrypt(value.encode())
        return False
    except InvalidToken:
        pass
    try:
        _the_legacy_fernet().decrypt(value.encode())
        return True
    except InvalidToken:
        return False


async def rekey_legacy_credentials(db) -> int:
    """把还用出厂密钥的凭据重新加密成当前密钥，返回换掉的条数。

    启动时跑一次。不跑的话那些弱密文会一直躺在库里 —— **"以后新写的安全了"
    不是修复**，用户已经存进去的那把才是要保护的东西。
    """
    from sqlalchemy import select

    from app.models.model_backend import ModelBackendConfig

    rows = (
        await db.scalars(
            select(ModelBackendConfig).where(
                ModelBackendConfig.encrypted_api_key.is_not(None)
            )
        )
    ).all()
    rekeyed = 0
    for row in rows:
        blob = row.encrypted_api_key
        if not blob or not was_encrypted_with_the_published_constant(blob):
            continue
        row.encrypted_api_key = encrypt_api_key(
            _the_legacy_fernet().decrypt(blob.encode()).decode()
        )
        rekeyed += 1
    if rekeyed:
        await db.flush()
    return rekeyed


def environment_api_key(provider: str) -> str | None:
    field = ENV_KEY_FIELDS.get(provider)
    value = getattr(settings, field, "") if field else ""
    return value or None


def backend_has_key(config: ModelBackendConfig) -> bool:
    if config.provider == "demo":
        return False
    if config.credential_source == "environment":
        return environment_api_key(config.provider) is not None
    return bool(config.encrypted_api_key)


def credential_is_optional(config: ModelBackendConfig) -> bool:
    """这条连接可以没有 API key 吗？

    判据不是 provider 标签，是**谁提供端点**。用户自己填了 `base_url` = 他指着
    自己的服务器；那台服务器要不要鉴权是它自己的事，而且是**能观测的**（探针
    不带 Authorization 头问一次就知道）。用官方托管端点（没填 base_url）时，
    key 是我们唯一的身份，必需。

    为什么不按 provider 名单判：`openai_compatible` 既可能是自建 vLLM，也可能
    是某家托管服务 —— 同一个标签两种答案，名单在这件事上是错的量具。

    现场（2026-09-15，yuankk）：`local` + `http://10.128.7.30:8000/v1` 的 vLLM
    不鉴权，key 留空 → 状态 `missing_credentials`、探针连包都不发（"未判定"）、
    真要跑时 worker 起不来。那台服务器一直好好的。
    """
    return bool((config.base_url or "").strip())


def backend_status(config: ModelBackendConfig) -> str:
    if not config.is_enabled:
        return "disabled"
    if config.provider == "demo":
        return "ready"
    if not settings.harness_bridge_enabled:
        return "harness_disabled"
    if config.provider not in HARNESS_COMPATIBLE_PROVIDERS:
        return "unsupported_by_harness"
    if not backend_has_key(config) and not (
        credential_is_optional(config) and config.last_probe_ok is True
    ):
        # "没有 key"不等于"没配好"：自建端点大多不鉴权。但"它不鉴权"是**观测**
        # 而不是我们替它声明的 —— 探针拿不带 Authorization 头的请求问过、端点
        # 收了（last_probe_ok is True），才算数。没探明白之前不装作能用。
        return "missing_credentials"
    # "key 字段填了"是**声明**，"provider 认这把 key"才是**观测**。node20
    # 实测：机构默认后端的 key 填得好好的但已失效，平台一路显示 ready 并
    # 把它选成默认，同事一发消息就 401。只有明确的拒绝才降级——探不出结论
    # （网络不通、超时、provider 没有可探端点）保持 ready，不拿"不知道"当"坏了"。
    if config.last_probe_ok is False:
        return "credentials_rejected"
    return "ready"


def _visible_clause(user: User):
    clauses = [
        and_(
            ModelBackendConfig.scope_kind == "institution",
            ModelBackendConfig.scope_id == user.institution_id,
        ),
        and_(
            ModelBackendConfig.scope_kind == "personal",
            ModelBackendConfig.scope_id == user.id,
        ),
    ]
    # 「课题组」这一档随 group_admin 一起退役（UserRole 的说明）。组织服务器上
    # 从来没有建出过 group 档的后端：只有 group_admin 能建，而他们都没有组。
    return or_(*clauses)


async def list_visible_backends(db: AsyncSession, user: User) -> list[ModelBackendConfig]:
    result = await db.execute(
        select(ModelBackendConfig)
        .where(_visible_clause(user))
        .order_by(ModelBackendConfig.scope_kind, ModelBackendConfig.display_name)
    )
    return list(result.scalars().all())


async def get_visible_backend(db: AsyncSession, user: User, backend_id: str) -> ModelBackendConfig:
    # id 列是 UUID：一个形状不对的字符串在**驱动层**就抛 DataError，还没走到
    # 下面那句 404 —— 于是"这个后端不存在"这件事，会因为不存在的方式不同而
    # 一会儿 404、一会儿 500。对调用方来说是同一件事，就得是同一个答案。
    try:
        UUID(str(backend_id))
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(status_code=404, detail="Model backend not found") from None
    config = await db.scalar(
        select(ModelBackendConfig).where(
            ModelBackendConfig.id == backend_id,
            _visible_clause(user),
        )
    )
    if not config:
        raise HTTPException(status_code=404, detail="Model backend not found")
    return config


#: 端点没填时，唯一键把它折成的值。见 `ModelBackendConfig.__table_args__`：
#: DB 侧是 `coalesce(base_url, '')`，这里必须是同一个折法，否则 Python 查得到的
#: 冲突和 DB 拦得住的冲突是两个集合 —— 那就是"先说没冲突再报 500"。
def endpoint_identity(base_url: str | None) -> str:
    return base_url or ""


async def find_conflicting_backend(
    db: AsyncSession,
    *,
    scope_kind: str,
    scope_id: str,
    provider: str,
    model: str,
    base_url: str | None,
    exclude_id: str | None = None,
) -> ModelBackendConfig | None:
    """已经占着这个 (scope, provider, model, 端点) 的那条连接，如果有的话。

    存在的理由是**报错要说得出人话**：唯一键撞了之后，"已经有一条了"是个
    没用的答案 —— 人要知道的是哪一条、它挂在哪个端点上，才知道自己是想改
    那条还是真的要新建一条。所以冲突发生时得把那行捞出来。

    `exclude_id` 给编辑用：一条连接不跟自己冲突。
    """
    clauses = [
        ModelBackendConfig.scope_kind == scope_kind,
        ModelBackendConfig.scope_id == scope_id,
        ModelBackendConfig.provider == provider,
        ModelBackendConfig.model == model,
        func.coalesce(ModelBackendConfig.base_url, "") == endpoint_identity(base_url),
    ]
    if exclude_id is not None:
        clauses.append(ModelBackendConfig.id != exclude_id)
    return await db.scalar(select(ModelBackendConfig).where(*clauses))


def managed_scope(user: User) -> tuple[str, str]:
    if user.role == UserRole.INSTITUTION_ADMIN.value:
        return "institution", user.institution_id
    return "personal", user.id


def can_edit_backend(user: User, config: ModelBackendConfig) -> bool:
    # 组织提供的那一份（`services/organisation_models`）归组织管理员，这台桌面上的人改不了。
    if config.provided_by_connection:
        return False
    return (config.scope_kind, config.scope_id) == managed_scope(user)


async def select_effective_backend(
    db: AsyncSession, user: User, *, role: str = REASONING_ROLE, require_ready: bool = True
) -> ModelBackendConfig | None:
    """"这个用户的这个**角色**实际会用哪个后端" —— **唯一**推导处。

    这条规则原本有两份实现：本文件（运行时真取哪个）和 settings.py 的
    `_effective_id`（UI 给哪个打 default 徽章）。两份各判各的，于是徽章
    指着 A、运行时用着 B。node20 交付实测把这个分歧顶了出来：机构默认
    指向一个 key 已失效的后端，UI 照旧把它显示成默认。

    选择顺序：该角色的用户显式偏好 → 同 scope 优先级里"既是该角色默认又
    能用" → 任何被授权服务该角色且能用的。`require_ready=True` 时"能用"=
    backend_status()=="ready"；一个必然失败的默认不该赢过旁边一个可用的。

    2026-08-22 加上 `role`：此前这个函数只能回答一个问题（主模型是谁），
    于是节点里的辅助模型（审图 VLM）只能自己写死 provider + 读写死的环境
    变量名 —— 对用户完全不可见，也没法换。角色化之后，"这个能力槽有没有
    人填"变成平台能机械回答的问题，可以在派发时就回答。
    """

    def usable(item: ModelBackendConfig) -> bool:
        if role not in (item.roles or []):
            return False
        return not require_ready or backend_status(item) == "ready"

    preference = await db.get(UserModelBackendPreference, (user.id, role))
    if preference:
        selected = await db.scalar(
            select(ModelBackendConfig).where(
                ModelBackendConfig.id == preference.backend_id,
                _visible_clause(user),
            )
        )
        if selected and usable(selected):
            return selected

    candidates = [item for item in await list_visible_backends(db, user) if usable(item)]
    # 自己的连接先于组织的（组那一档随 group_admin 退役了，`_visible_clause` 不再列它）。
    precedence = {"personal": 0, "institution": 1}

    def is_role_default(item: ModelBackendConfig) -> bool:
        return role in (item.default_for_roles or [])

    candidates.sort(key=lambda item: (precedence[item.scope_kind], not is_role_default(item)))
    return next((item for item in candidates if is_role_default(item)), None) or next(
        iter(candidates), None
    )


async def effective_backend(
    db: AsyncSession, user: User, *, role: str = REASONING_ROLE, require_ready: bool = True
) -> ModelBackendConfig:
    selected = await select_effective_backend(db, user, role=role, require_ready=require_ready)
    if not selected:
        raise HTTPException(status_code=503, detail=nobody_can_answer(role))
    return selected


def nobody_can_answer(role: str) -> str:
    """没有能用的模型时给人看的那句话 —— 说清缺什么、谁能补、去哪补。

    曾经是一句 `No ready model backend is available for role 'reasoning'`。在组织服务器
    上这句话尤其误导：组织项目跑在**服务器**的模型上（和成员本机配的模型无关），而一台
    刚装好、刚建好组织的服务器上一个模型都没有 —— 成员从桌面建了组织项目，第一句话就撞
    上它，却读不出"这要组织管理员去配"（`RFC_ORGANISATION_PAGE_20260923` E 批）。
    """
    from app import assembly

    what = "主模型" if role == REASONING_ROLE else f"「{role}」这个角色的模型"
    if assembly.profile_name() == "org":
        return (f"这个组织还没有能用的{what} —— 组织项目跑在组织服务器上，用的是组织的模型。"
                "请组织管理员在「组织 → 设置 → 模型」里加一个。")
    return f"还没有能用的{what} —— 去「设置 → 模型与密钥」配一个。"


async def resolve_role_bindings(
    db: AsyncSession, user: User
) -> dict[str, ModelBackendConfig]:
    """一次解析出**全部**角色的绑定 —— bridge 建 worker 环境时用这一份。

    按目录**扫**，不写名单：加一个角色只改 shared/model_roles.yaml，这里
    自动跟上。此前 bridge 是一张写死的 env 透传白名单，新角色靠"记得去加
    一行"，而漏掉的那次直接造成审图 key 既没被擦出子进程环境、也没进脱敏面。
    """
    bindings: dict[str, ModelBackendConfig] = {}
    try:
        specs = catalog()
    except ModelRoleCatalogError as exc:
        # 目录读不到是**部署缺陷**，但它不该把会话一起判死：主推理模型的解析
        # 根本不经过目录（调用方对 reasoning 有 effective_backend 兜底），
        # 硬抛的结果是"HARNESS_ROOT 少配一行 → 一句话都说不了"。
        #
        # 缺角色本来就是这套设计里的**局面**：节点拿到 absence_note、按缺能力
        # 那条路降级。目录读不到只是"所有辅助角色都缺"，形状是一样的。
        # 吵在该吵的地方：日志 + 『设置 → 模型』那条路径照旧抛（它就是给人看
        # 配置的地方），而不是吵在每一次对话上。
        log.error("模型角色目录读不到，本次只解析主推理模型：%s", exc)
        return bindings
    for spec in specs:
        selected = await select_effective_backend(db, user, role=spec.id)
        if selected is not None:
            bindings[spec.id] = selected
    return bindings


async def set_effective_default(
    db: AsyncSession, user: User, config: ModelBackendConfig, *, role: str = REASONING_ROLE
) -> None:
    if role not in (config.roles or []):
        raise HTTPException(
            status_code=409,
            detail=f"Backend is not authorized for role {role!r}",
        )
    if backend_status(config) != "ready":
        raise HTTPException(status_code=409, detail="Model backend is not ready")
    if can_edit_backend(user, config) and user.role != UserRole.RESEARCHER.value:
        # 同 scope 内该角色的默认是唯一的：先把别人的这一项摘掉，再给自己戴上。
        # 只动**这个角色**，别人的角色默认不受影响。
        siblings = await db.scalars(
            select(ModelBackendConfig).where(
                ModelBackendConfig.scope_kind == config.scope_kind,
                ModelBackendConfig.scope_id == config.scope_id,
                ModelBackendConfig.id != config.id,
            )
        )
        for sibling in siblings:
            if role in (sibling.default_for_roles or []):
                sibling.default_for_roles = [
                    item for item in sibling.default_for_roles if item != role
                ]
        if role not in (config.default_for_roles or []):
            config.default_for_roles = [*(config.default_for_roles or []), role]
    preference = await db.get(UserModelBackendPreference, (user.id, role))
    if preference:
        preference.backend_id = config.id
    else:
        db.add(
            UserModelBackendPreference(user_id=user.id, role=role, backend_id=config.id)
        )
    await db.flush()


_PROBE_TIMEOUT_SECONDS = 8.0


def _probe_url(config: ModelBackendConfig) -> str | None:
    """能探的才探。探不了返回 None —— 结论是"不知道"，不是"坏了"。"""
    base = (config.base_url or "").rstrip("/")
    if not base:
        return None
    if config.provider in {"deepseek", "kimi", "openai", "openai_compatible", "local"}:
        return f"{base}/models"
    return None


def _auth_headers(key: str | None) -> dict[str, str]:
    """没有 key 就**不发** Authorization 头，而不是发一个空的。

    `Bearer `（空值）在一部分网关上会被判成"给了一把坏 key"而回 401 —— 那个
    401 指的是假因。缺席就让它缺席，端点才能如实回答"我要不要鉴权"。
    """
    return {"Authorization": f"Bearer {key}"} if key else {}


def _looks_like_api_response(response) -> bool:
    """回应的是不是那个 API 本身（而不是中间的门户/代理）。"""
    content_type = ""
    try:
        content_type = (response.headers.get("content-type") or "").lower()
    except Exception:
        pass
    if "json" in content_type:
        return True
    try:  # 没有 content-type 也认：只要 body 是 JSON
        response.json()
        return True
    except Exception:
        return False


async def probe_backend_credential(config: ModelBackendConfig) -> tuple[bool | None, str]:
    """拿真 key 问 provider 一句，返回 (ok, detail)。

    ok=True  provider 认这把 key
    ok=False provider **明确拒绝**（401/403）—— 唯一会让 status 降级的情况
    ok=None  探不出结论（没有可探端点 / 网络不通 / 超时 / 5xx）

    刻意不把 5xx、超时算作拒绝：provider 临时抽风不该让一个好后端被永久
    标坏，而 401 是稳定事实。
    """
    url = _probe_url(config)
    if not url:
        return None, "no probe endpoint for this provider"
    key = resolved_api_key(config)
    if not key and not credential_is_optional(config):
        return None, "no resolvable credential"

    import httpx

    try:
        async with httpx.AsyncClient(timeout=_PROBE_TIMEOUT_SECONDS) as client:
            response = await client.get(url, headers=_auth_headers(key))
    except Exception as exc:  # 网络层面的失败 = 不知道
        return None, f"probe could not reach provider: {type(exc).__name__}"

    if response.status_code in (401, 403):
        # 只有**确实是 provider 在拒绝**才算数。强制门户 / 公司代理也会回
        # 401，但回的是 HTML 登录页——把那种当成"key 失效"，会让代理后面的
        # 同事所有后端被永久标坏。判据：OpenAI 兼容 API 一律回 JSON 错误体。
        if not _looks_like_api_response(response):
            return None, "HTTP 401/403 from a non-API responder (proxy or captive portal?)"
        if not key:
            # 没给 key 而被拒 —— 这是"这个端点要鉴权"，不是"这把 key 坏了"。
            # 判 False 会让界面说 "Credential rejected"，而这里根本没有凭据可拒。
            return None, (
                f"endpoint requires a credential (HTTP {response.status_code}) "
                "—— 这个端点需要 API key"
            )
        return False, f"provider rejected the credential (HTTP {response.status_code})"
    if response.status_code >= 500:
        return None, f"provider error (HTTP {response.status_code})"
    if response.is_success:
        return True, (
            "provider accepted the credential" if key
            else "endpoint served an unauthenticated request"
        )
    return None, f"inconclusive (HTTP {response.status_code})"


#: 探视觉能力用的 1×1 透明 PNG。够小到不产生实际推理成本，够真到能让
#: 不支持图像的端点明确拒绝。
_ONE_PIXEL_PNG_DATA_URL = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
    "YPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)

#: 端点说"我不认图"的稳定说法。命中才判 False —— 其它一律"不知道"。
_VISION_REFUSAL_MARKERS = (
    "image_url",
    "image input",
    "does not support image",
    "not support vision",
    "unsupported content type",
    "invalid content type",
    "multimodal",
    "vision",
)


def serves_vision_role(config: ModelBackendConfig) -> bool:
    """这条连接被指派给了任何一个需要视觉的角色吗。

    按目录**扫**（modality == vision），不写死角色名 —— 以后加一个需要看图
    的角色，这里自动跟上。
    """
    try:
        vision_roles = {item.id for item in catalog() if item.needs_vision}
    except Exception:  # 目录读不到时不假装知道，按"不需要视觉"处理并让目录错误自己浮上去
        return False
    return bool(vision_roles & set(config.roles or []))


async def probe_vision_capability(config: ModelBackendConfig) -> tuple[bool | None, str]:
    """给它一张 1×1 图，看它收不收。返回 (ok, detail)，三态同凭据探针。

    为什么要真探：**"这个模型认不认图"是能观测的**。靠模型名去猜（名字里
    有 vl / vision / gpt-4o…）在换一家 provider 时立刻失效，而失效的方式是
    静默的 —— 用户指派完看到一个绿灯，直到某个节点渲染完图才发现审不了。

    只有端点**明确说不支持图像**才判 False。超时、5xx、看不懂的 400 一律
    None：临时抽风和"我不认图"是两件事，混在一起会把一个好后端永久标坏。
    """
    base = (config.base_url or "").rstrip("/")
    if not base:
        return None, "no base_url to probe"
    if base.endswith("/v1"):
        base = base[:-3]
    key = resolved_api_key(config)
    if not key and not credential_is_optional(config):
        return None, "no resolvable credential"

    import httpx

    payload = {
        "model": config.model,
        "max_tokens": 1,
        "temperature": 0,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "ok?"},
                    {"type": "image_url", "image_url": {"url": _ONE_PIXEL_PNG_DATA_URL}},
                ],
            }
        ],
    }
    try:
        async with httpx.AsyncClient(timeout=_PROBE_TIMEOUT_SECONDS) as client:
            response = await client.post(
                f"{base}/v1/chat/completions",
                headers=_auth_headers(key),
                json=payload,
            )
    except Exception as exc:
        return None, f"vision probe could not reach provider: {type(exc).__name__}"

    if response.is_success:
        return True, "provider accepted an image message"
    if response.status_code in (400, 415, 422):
        body = (response.text or "")[:2000].lower()
        if any(marker in body for marker in _VISION_REFUSAL_MARKERS):
            return False, (
                f"provider rejected an image message (HTTP {response.status_code}) "
                "— 这个模型不接受图像输入"
            )
        return None, f"inconclusive rejection (HTTP {response.status_code})"
    return None, f"inconclusive (HTTP {response.status_code})"


async def record_backend_probe(db: AsyncSession, config: ModelBackendConfig) -> bool | None:
    """探一次并把**观测**落库（不落判决）。返回本次凭据结论。

    被指派给视觉角色的连接**多探一次图**。顺序有意为之：凭据没过就不探图
    （一把坏 key 探什么都是 401，探出来的"不认图"是假的）。
    """
    ok, detail = await probe_backend_credential(config)
    config.last_probe_at = datetime.now(UTC)
    config.last_probe_ok = ok
    config.last_probe_detail = detail[:500]
    if ok is not False and serves_vision_role(config):
        vision_ok, vision_detail = await probe_vision_capability(config)
        config.last_vision_ok = vision_ok
        config.last_vision_detail = vision_detail[:500]
    elif not serves_vision_role(config):
        # 不再服务视觉角色了，旧观测就作废 —— 留着它等于把一个跟当前指派
        # 无关的事实继续展示成现状。
        config.last_vision_ok = None
        config.last_vision_detail = None
    await db.flush()
    return ok


def resolved_api_key(config: ModelBackendConfig) -> str | None:
    if config.credential_source == "environment":
        return environment_api_key(config.provider)
    if config.encrypted_api_key:
        return decrypt_api_key(config.encrypted_api_key)
    return None


# ── 这条模型是谁提供的 ─────────────────────────────────────────────────────

#: 发行可以补充来源（专业版：组织提供的模型带着组织名）。核心只有「这台机器自己的」。
PROVENANCE_EXTENSIONS: list = []


def where_it_comes_from(config: ModelBackendConfig) -> dict | None:
    """列表上的「来源」。空 = 这台机器自己的连接。"""
    for ask in PROVENANCE_EXTENSIONS:
        got = ask(config)
        if got:
            return got
    return None


# ── 别处提供的模型 ─────────────────────────────────────────────────────────

#: 列模型 / 角色之前先做的事（专业版：把组织提供的模型对进本机这张表）。
BEFORE_LISTING_EXTENSIONS: list = []
#: 给某个角色挑了一条连接之后还要告诉谁（专业版：挑的是组织的模型，就告诉那台服务器）。
AFTER_ROLE_CHOICE_EXTENSIONS: list = []


async def bring_in_what_others_provide(db, user) -> None:
    for hook in BEFORE_LISTING_EXTENSIONS:
        await hook(db, user)


async def tell_the_provider_about_the_choice(config: ModelBackendConfig, role: str) -> None:
    for hook in AFTER_ROLE_CHOICE_EXTENSIONS:
        await hook(config, role)

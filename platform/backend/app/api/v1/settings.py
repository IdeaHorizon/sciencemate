"""Authenticated settings backed by durable user and execution facts."""

from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.database import get_db
from app.models.execution import Run, SessionProjection
from app.models.model_backend import ModelBackendConfig, UserModelBackendPreference
from app.models.user import User, UserRole
from app.schemas.research_settings import ResearchSettingsOut, ResearchSettingsWrite
from app.schemas.settings import (
    SettingsModel,
    InterfaceSettings,
    NotificationSettings,
    NotificationSettingsOut,
    UsageSummary,
)
from app.services.user_interface import effective_interface
from app.services.model_backends import (
    bring_in_what_others_provide,
    tell_the_provider_about_the_choice,
    where_it_comes_from,
    backend_has_key,
    backend_status,
    can_edit_backend,
    encrypt_api_key,
    find_conflicting_backend,
    get_visible_backend,
    list_visible_backends,
    managed_scope,
    record_backend_probe,
    select_effective_backend,
    serves_vision_role,
    set_effective_default,
)
from app.services.model_role_catalog import (
    REASONING_ROLE,
    ModelRoleCatalogError,
    catalog as model_role_catalog,
    role_ids as model_role_ids,
    spec as model_role_spec,
)
from app.services.research_settings import (
    effective_research_settings,
    replace_research_settings,
)

router = APIRouter()


_DEFAULT_INTERFACE = InterfaceSettings()
_DEFAULT_NOTIFICATIONS = NotificationSettings()


#: 界面偏好的解释只有一处 —— `services/user_interface`。资讯流也要读它
#: （按语言出文案），两边各实现一遍就是两个会分叉的答案。
_effective_interface = effective_interface


@router.get("/interface", response_model=InterfaceSettings)
async def get_interface_settings(user: User = Depends(get_current_user)) -> InterfaceSettings:
    return _effective_interface(user)


@router.put("/interface", response_model=InterfaceSettings)
async def put_interface_settings(
    data: InterfaceSettings,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> InterfaceSettings:
    preferences = dict(user.preferences) if isinstance(user.preferences, dict) else {}
    preferences["interface"] = data.model_dump()
    user.preferences = preferences
    await db.flush()
    return data


def _effective_notifications(user: User) -> NotificationSettingsOut:
    normalized = _DEFAULT_NOTIFICATIONS.model_dump()
    preferences = user.preferences if isinstance(user.preferences, dict) else {}
    stored = preferences.get("notifications")
    if isinstance(stored, dict):
        for field in normalized:
            value = stored.get(field)
            if isinstance(value, bool):
                normalized[field] = value
    return NotificationSettingsOut(**normalized)


@router.get("/notifications", response_model=NotificationSettingsOut)
async def get_notification_settings(
    user: User = Depends(get_current_user),
) -> NotificationSettingsOut:
    """Return in-app event visibility; outbound delivery is not advertised."""
    return _effective_notifications(user)


@router.put("/notifications", response_model=NotificationSettingsOut)
async def put_notification_settings(
    data: NotificationSettings,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> NotificationSettingsOut:
    preferences = dict(user.preferences) if isinstance(user.preferences, dict) else {}
    preferences["notifications"] = data.model_dump()
    user.preferences = preferences
    await db.flush()
    return NotificationSettingsOut(**data.model_dump())


def _aware(value: datetime) -> datetime:
    """把落库取回来的时间戳统一成 aware。

    列是 `DateTime(timezone=True)`，但**取回来是不是 aware 取决于驱动**：
    Postgres 给 aware，SQLite 给 naive。拿 naive 去和 `datetime.now(UTC)` 相减
    是 `TypeError: can't subtract offset-naive and offset-aware datetimes`，
    也就是一个只在某些部署上才炸的 500。写这个函数前它已经被手抄过一遍。
    """
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _utc_date(value: datetime) -> date:
    return _aware(value).astimezone(UTC).date()


def _streaks(active_dates: set[date], today: date) -> tuple[int, int]:
    ordered = sorted(day for day in active_dates if day <= today)
    longest = 0
    running = 0
    previous: date | None = None
    for day in ordered:
        running = running + 1 if previous and day == previous + timedelta(days=1) else 1
        longest = max(longest, running)
        previous = day

    current = 0
    cursor = today
    while cursor in active_dates:
        current += 1
        cursor -= timedelta(days=1)
    return current, longest


@router.get("/usage", response_model=UsageSummary)
async def get_usage_settings(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Aggregate only Runs attached to Sessions initiated or created by this user."""
    owned_session_ids = set(
        (
            await db.scalars(
                select(SessionProjection.session_id).where(
                    or_(
                        SessionProjection.created_by_user_id == user.id,
                        SessionProjection.initiating_user_id == user.id,
                    )
                )
            )
        ).all()
    )
    runs = (
        list((await db.scalars(select(Run).where(Run.session_id.in_(owned_session_ids)))).all())
        if owned_session_ids
        else []
    )

    known_cost_runs = [run for run in runs if run.cost is not None]
    known_cost = sum(
        (run.cost for run in known_cost_runs if run.cost is not None),
        Decimal("0"),
    )
    currencies = {
        run.cost_currency.upper() if run.cost_currency else None for run in known_cost_runs
    }
    cost_currency = (
        next(iter(currencies)) if len(currencies) == 1 and None not in currencies else None
    )

    today = datetime.now(UTC).date()
    daily_start = today - timedelta(days=364)
    active_dates: set[date] = set()
    daily_totals: dict[date, list[int]] = defaultdict(lambda: [0, 0])
    for run in runs:
        run_date = _utc_date(run.created_at)
        if run_date <= today:
            active_dates.add(run_date)
        if daily_start <= run_date <= today:
            daily_totals[run_date][0] += int(run.total_tokens or 0)
            daily_totals[run_date][1] += 1
    current_streak, longest_streak = _streaks(active_dates, today)

    return {
        "session_count": len(owned_session_ids),
        "run_count": len(runs),
        "prompt_tokens": sum(int(run.prompt_tokens or 0) for run in runs),
        "completion_tokens": sum(int(run.completion_tokens or 0) for run in runs),
        "total_tokens": sum(int(run.total_tokens or 0) for run in runs),
        "retry_count": sum(int(run.retry_count or 0) for run in runs),
        "known_cost": float(known_cost),
        "cost_currency": cost_currency,
        "cost_known_runs": len(known_cost_runs),
        "cost_unknown_runs": len(runs) - len(known_cost_runs),
        "active_days": len(active_dates),
        "current_streak": current_streak,
        "longest_streak": longest_streak,
        "daily": [
            {
                "date": day,
                "total_tokens": daily_totals[day][0],
                "run_count": daily_totals[day][1],
            }
            for day in sorted(daily_totals)
        ],
    }






@router.get("/research", response_model=ResearchSettingsOut)
async def get_personal_research_settings(
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> dict:
    """Return only the authenticated user's effective personal research defaults."""
    return await effective_research_settings(db, user=user)


@router.put("/research", response_model=ResearchSettingsOut)
async def put_personal_research_settings(
    data: ResearchSettingsWrite,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Atomically replace the authenticated user's editable personal layer."""
    await replace_research_settings(db, user=user, data=data)
    return await effective_research_settings(db, user=user)


class ModelBackendWrite(BaseModel):
    provider: str = Field(min_length=1, max_length=64)
    # 这条连接被授权服务哪些**模型角色**。留空 = reasoning（绝大多数连接
    # 就是主模型）。合法取值来自 harness 的角色目录 —— 见 GET /model-roles，
    # 前端渲染的就是那份，不在任何一侧另存词表。
    roles: list[str] | None = None
    display_name: str = Field(min_length=1, max_length=200)
    model: str = Field(min_length=1, max_length=200)
    base_url: str | None = Field(default=None, max_length=1000)
    api_key: str | None = Field(default=None, min_length=1)
    credential_source: Literal["none", "environment", "encrypted"] | None = None
    # 上下文窗口 = 这个模型能吃多少 token。留空 = 用平台默认。
    # 它决定摘要器什么时候压缩（阈值是窗口的 70%），填错的代价是要么频繁
    # 压缩丢上下文，要么撞上 provider 的硬上限。
    context_window_tokens: int | None = Field(default=None, ge=8000, le=2_000_000)



class ModelBackendUpdate(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=200)
    roles: list[str] | None = None
    model: str | None = Field(default=None, min_length=1, max_length=200)
    base_url: str | None = Field(default=None, max_length=1000)
    api_key: str | None = Field(default=None, min_length=1)
    is_enabled: bool | None = None
    # 上下文窗口 = 这个模型能吃多少 token。留空 = 用平台默认。
    # 它决定摘要器什么时候压缩（阈值是窗口的 70%），填错的代价是要么频繁
    # 压缩丢上下文，要么撞上 provider 的硬上限。
    context_window_tokens: int | None = Field(default=None, ge=8000, le=2_000_000)



def _validated_roles(requested: list[str] | None) -> list[str]:
    """合法角色**由目录说了算**，且不合法要当场报错并把合法取值列出来。

    「合法取值只在运行时报错」这条路走过一次了：模型/人得靠猜。词表进
    schema、报错列清单，是同一件事的两半。
    """
    if requested is None:
        return [REASONING_ROLE]
    try:
        known = model_role_ids()
    except ModelRoleCatalogError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    unknown = [item for item in requested if item not in known]
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown model roles {unknown}; valid roles are {sorted(known)}",
        )
    # 去重保序：同一个角色写两遍不是错误，但存两份就会在别处被数两次。
    return list(dict.fromkeys(requested))


def _response(config: ModelBackendConfig, user: User, effective_id: str | None) -> dict:
    return {
        "id": config.id,
        "roles": list(config.roles or []),
        "default_for_roles": list(config.default_for_roles or []),
        # 视觉能力是**独立观测**，跟凭据健康分开交出去 —— 合成一个 status
        # 会让"这个模型不认图"显示成"凭据被拒"，人就去换 key 了。
        "serves_vision_role": serves_vision_role(config),
        "last_vision_ok": config.last_vision_ok,
        "last_vision_detail": config.last_vision_detail,
        "provider": config.provider,
        "display_name": config.display_name,
        "model": config.model,
        "base_url": config.base_url,
        "context_window_tokens": config.context_window_tokens,
        "status": backend_status(config),
        "is_default": config.id == effective_id,
        "scope": {"kind": config.scope_kind, "id": config.scope_id},
        "has_api_key": backend_has_key(config),
        "editable": can_edit_backend(user, config),
        # 这一条是哪个组织提供的（空 = 这台机器自己的）。界面据此写「来源：某某组织」。
        "provided_by": where_it_comes_from(config),
        # 下面四个是**观测本身**，不是由它派生的那个 status。
        #
        # 从前只投影 status，于是 UI 上一句 "Ready" 背后是"上一次写操作那天
        # 探到的结果"—— 探针只在新建/改动/设默认时跑，之后这个字段就再也不
        # 变了。用户看到的是一个像"当前事实"的快照，而它可能是几周前的。
        # 2026-08-21 wangd 的原话是「感觉很多都不能用，放着有啥用」——正是
        # 这个。判决（status）留着，但证据必须一起交出去，让人看得见它多旧。
        "is_enabled": config.is_enabled,
        "last_probe_at": config.last_probe_at,
        "last_probe_ok": config.last_probe_ok,
        "last_probe_detail": config.last_probe_detail,
    }


async def _effective_id(
    db: AsyncSession, user: User, *, role: str = REASONING_ROLE
) -> str | None:
    """UI 的 default 徽章 = 运行时真会选的那个。同一条规则，一处推导。"""
    selected = await select_effective_backend(db, user, role=role)
    return selected.id if selected else None


@router.get("/model-roles")
async def get_model_roles(
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> dict:
    """角色目录 + 每个角色**当下真会用哪条连接**。

    目录的权威在 harness（$HARNESS_ROOT/shared/model_roles.yaml）。前端渲染
    这份返回，不自己枚举角色 —— provider 词表当年在 Python 和 TypeScript 各
    活了一份、靠一条测试钉着相等，那条路不再走第二遍。

    目录读不到就 503 并说清是 HARNESS_ROOT 的事。返回空列表长得像"这套部署
    没有任何角色"，会让人在设置页里找一个根本没渲染出来的开关。
    """
    try:
        specs = model_role_catalog()
    except ModelRoleCatalogError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    # 组织提供的模型也是候选 —— 先对一次表（桌面上才有这回事）。
    await bring_in_what_others_provide(db, user)
    out = []
    for item in specs:
        selected = await select_effective_backend(db, user, role=item.id)
        out.append(
            {
                **item.to_public_dict(),
                "bound_backend_id": selected.id if selected else None,
                "bound_display_name": selected.display_name if selected else None,
                "bound_model": selected.model if selected else None,
                # 缺角色的**后果**在 absence_note / absence_impact 里（前者给
                # 节点、后者给人），这里只说在不在。
                "available": selected is not None,
            }
        )
    return {"roles": out}


@router.post("/model-backends/{backend_id}/roles/{role}/default")
async def make_model_backend_role_default(
    backend_id: str,
    role: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """把某条连接指派为**这个角色**的默认。

    与主模型设默认是同一条规则、同一处实现 —— 主模型只是 role=reasoning。
    """
    if model_role_spec(role) is None:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown model role {role!r}; valid roles are {sorted(model_role_ids())}",
        )
    config = await get_visible_backend(db, user, backend_id)
    # 设默认是全场最高风险的一次声明，先重探再判 —— 不吃可能过期的旧观测。
    await record_backend_probe(db, config)
    await set_effective_default(db, user, config, role=role)
    # 挑的是别处提供的那一条（组织的模型）：那一处也要知道 —— 专业版在钩子里告诉服务器。
    await tell_the_provider_about_the_choice(config, role)
    return {"status": "updated", "backend_id": config.id, "role": role}


@router.get("/model-backends")
async def get_model_backends(
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> list[dict]:
    # 组织提供的模型出现在这里、标着来源（桌面上才有这回事；组织服务器上立即返回）。
    await bring_in_what_others_provide(db, user)
    effective_id = await _effective_id(db, user)
    return [
        _response(config, user, effective_id) for config in await list_visible_backends(db, user)
    ]


def _not_yours_to_change(config: ModelBackendConfig) -> HTTPException:
    source = where_it_comes_from(config)
    if source:
        return HTTPException(status_code=403, detail=(
            f"「{config.display_name}」是「{source['organisation_name']}」提供的，由那个组织的管理员维护。"
            "不想用它，就在模型角色里换一条。"))
    return HTTPException(status_code=403, detail="Not allowed to modify this backend scope")


def _endpoint_taken(existing: ModelBackendConfig) -> HTTPException:
    """撞了唯一键要说的话。

    在此之前这里什么都不说：`IntegrityError` 一路冒到顶，FastAPI 交回
    **500 Internal Server Error、正文一个字没有**。人看到 500 只会以为平台
    坏了，而事实是他填的东西跟一条已经存在的连接重了 —— 一个他自己三秒就能
    解决的问题，却没有任何线索。

    所以这条 409 必须点名**是哪一条**：display_name 让他在列表里找得到它，
    base_url 让他判断自己是想改那条、还是真要新建一条指向别处的。
    """
    at = existing.base_url or "the provider's default endpoint"
    return HTTPException(
        status_code=409,
        detail=(
            f"This scope already has a connection to {existing.provider}/{existing.model} "
            f"at {at} — “{existing.display_name}”. Edit that connection, or point this "
            "one at a different endpoint."
        ),
    )


async def _endpoint_clash(
    db: AsyncSession,
    *,
    scope_kind: str,
    scope_id: str,
    provider: str,
    model: str,
    base_url: str | None,
    exclude_id: str | None = None,
) -> HTTPException | None:
    """写入撞了唯一键之后：查出到底是谁占着。查不到就返回 None。

    **写之前不预查一遍**是有意的。预查用的是 Python 侧同款 `coalesce`，它和
    唯一键是两个各自演化的答案 —— 实测过一次：把唯一键写成 `(..., base_url)`
    （NULL 不参与唯一性比较，等于对所有没填端点的连接取消了约束）之后，因为
    预查自己就把重复挡下来了，**接口层的测试一条都没红**。一个问题两个真相
    源，分叉的时候没有人会知道。所以判"重不重"只由库说了算，这里只负责在它
    说重了之后，把那条占位的记录捞出来讲成人话。

    查不到就让原本的 `IntegrityError` 原样冒出去。别的约束（scope_kind /
    credential_source 的 CHECK）撞了也说成"重复连接"，会把人支去改一个根本
    没问题的字段 —— 产生失败的那层才有资格盖章。
    """
    raced = await find_conflicting_backend(
        db,
        scope_kind=scope_kind,
        scope_id=scope_id,
        provider=provider,
        model=model,
        base_url=base_url,
        exclude_id=exclude_id,
    )
    return _endpoint_taken(raced) if raced is not None else None


@router.post("/model-backends", status_code=201)
async def create_model_backend(
    data: ModelBackendWrite,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    scope_kind, scope_id = managed_scope(user)
    source = data.credential_source or ("encrypted" if data.api_key else "none")
    if data.provider == "demo":
        source = "none"
    if source == "encrypted" and not data.api_key:
        raise HTTPException(status_code=422, detail="api_key is required for encrypted credentials")
    identity = dict(
        scope_kind=scope_kind,
        scope_id=scope_id,
        provider=data.provider,
        model=data.model,
        base_url=data.base_url,
    )
    config = ModelBackendConfig(
        **identity,
        display_name=data.display_name,
        context_window_tokens=data.context_window_tokens,
        roles=_validated_roles(data.roles),
        credential_source=source,
        encrypted_api_key=encrypt_api_key(data.api_key) if data.api_key else None,
        created_by_user_id=user.id,
    )
    try:
        # `db.add` 必须在 savepoint **里面**：撞键之后 savepoint 回滚，而留在
        # session 里的待插入对象会在请求收尾那次 commit 被再插一遍——一个
        # 说得清楚的 409 又变回 500。（feed.py 的 engagement 就是这么栽过一次。）
        async with db.begin_nested():
            db.add(config)
            await db.flush()
    except IntegrityError as exc:
        clash = await _endpoint_clash(db, **identity)
        if clash is None:
            raise
        raise clash from exc
    # 声明的时候就去问 provider 认不认这把 key。不在这里探，就只能等某位
    # 同事发消息时以 401 的形式发现——node20 交付实测就是这么发现的。
    await record_backend_probe(db, config)
    return _response(config, user, await _effective_id(db, user))


@router.put("/model-backends/{backend_id}")
async def update_model_backend(
    backend_id: str,
    data: ModelBackendUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    config = await get_visible_backend(db, user, backend_id)
    if not can_edit_backend(user, config):
        raise _not_yours_to_change(config)
    # 撞不撞车，问的是**这次写完之后长什么样**：改端点、改模型名都能把这条
    # 连接挪到另一条已经占着的身份上。`None` = 这个字段不改。
    identity = dict(
        scope_kind=config.scope_kind,
        scope_id=config.scope_id,
        provider=config.provider,
        model=config.model if data.model is None else data.model,
        base_url=config.base_url if data.base_url is None else data.base_url,
    )
    # `config.id` 先取出来：撞键之后 savepoint 回滚会把这个对象**过期**，
    # 到 except 里再读属性就是一次同步 IO（`MissingGreenlet`）—— 兜底自己
    # 变成另一个 500。（这行不是防御性代码，是真跑出来的。）
    backend_id_value = config.id
    roles_changed = False
    try:
        # 赋值也在 savepoint 里：撞键之后回滚要把这条连接原样退回去，别在
        # session 里留一份改了一半的它。
        async with db.begin_nested():
            for field in ("display_name", "model", "base_url", "is_enabled",
                          "context_window_tokens"):
                value = getattr(data, field)
                if value is not None:
                    setattr(config, field, value)
            if data.roles is not None:
                new_roles = _validated_roles(data.roles)
                roles_changed = set(new_roles) != set(config.roles or [])
                config.roles = new_roles
                # 撤销授权就把对应的默认一起摘掉 —— 留着一个指向未授权角色的默认，
                # 是一条"存在但永远选不中"的记录，下次读它的人得自己推断这件事。
                config.default_for_roles = [
                    item for item in (config.default_for_roles or []) if item in new_roles
                ]
            if data.api_key is not None:
                config.encrypted_api_key = encrypt_api_key(data.api_key)
                config.credential_source = "encrypted"
            await db.flush()
    except IntegrityError as exc:
        clash = await _endpoint_clash(db, **identity, exclude_id=backend_id_value)
        if clash is None:
            raise
        raise clash from exc
    credential_changed = data.api_key is not None or data.base_url is not None
    if credential_changed or roles_changed:
        # 换了 key / 换了地址 = 旧观测作废。**改了角色也要重探** —— 刚被
        # 指派给视觉角色的连接，这一刻才第一次需要回答"它认不认图"。
        await record_backend_probe(db, config)
    return _response(config, user, await _effective_id(db, user))


@router.post("/model-backends/{backend_id}/default")
async def make_model_backend_default(
    backend_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    config = await get_visible_backend(db, user, backend_id)
    # 把一个后端设成默认是全场最高风险的一次声明——机构默认坏掉 = 全所有人
    # 一进来就 401。所以这里**先重探再判 ready**，不吃可能过期的旧观测。
    await record_backend_probe(db, config)
    await set_effective_default(db, user, config)
    return {"status": "updated", "backend_id": config.id}


#: 同一个后端两次真探之间的最小间隔。低于它就直接把上次的观测原样交回。
#:
#: 为什么要有：探活是拿**存着的凭证**去打 provider 的真实端点。这个按钮对
#: 所有看得见该连接的人开放（研究员最需要知道的恰恰是"管理员配的那条还活着
#: 吗"），所以必须有个东西挡住"一屋子人一起点"变成对 provider 的定频压测。
#: 用冷却而不是权限，是因为按权限挡等于把这个能力还给了不需要它的人。
PROBE_COOLDOWN = timedelta(seconds=60)


@router.post("/model-backends/{backend_id}/probe")
async def probe_model_backend(
    backend_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """现在去问一次 provider，别拿上次那张快照糊弄人。

    看得见就能探 —— 不要求 editable。研究员改不了机构那条连接，但"它现在还
    能不能用"正是他要判断的事；只让管理员探，等于让所有人继续对着陈旧的
    ready 猜。
    """
    config = await get_visible_backend(db, user, backend_id)
    fresh = True
    if config.last_probe_at is not None:
        age = datetime.now(UTC) - _aware(config.last_probe_at)
        if age < PROBE_COOLDOWN:
            fresh = False
    if fresh:
        await record_backend_probe(db, config)
    return {
        **_response(config, user, await _effective_id(db, user)),
        # 告诉调用方这次到底有没有真去打：省略它，UI 就只能假装每次都是新的。
        "probed_now": fresh,
        "cooldown_seconds": int(PROBE_COOLDOWN.total_seconds()),
    }


@router.delete("/model-backends/{backend_id}", status_code=204)
async def delete_model_backend(
    backend_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    config = await get_visible_backend(db, user, backend_id)
    if not can_edit_backend(user, config):
        raise _not_yours_to_change(config)
    if config.default_for_roles:
        raise HTTPException(
            status_code=409,
            detail=(
                "Cannot delete a backend that is the scope default for roles "
                f"{sorted(config.default_for_roles)}"
            ),
        )
    await db.delete(config)
    await db.flush()

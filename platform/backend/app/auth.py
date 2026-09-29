"""Authentication — JWT tokens, password hashing, FastAPI dependency."""

import hashlib
import hmac
import logging
import secrets
from datetime import UTC, datetime, timedelta

import bcrypt
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
import jwt
from jwt import InvalidTokenError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.authentication import token_digest
from app.database import get_db

logger = logging.getLogger(__name__)
from app.models.authentication import RevokedAccessToken
from app.models.user import User

ALGORITHM = "HS256"

oauth2_scheme = OAuth2PasswordBearer(
    tokenUrl=f"{settings.api_v1_prefix}/auth/login",
    auto_error=False,
)


def hash_password(password: str) -> str:
    encoded = password.encode("utf-8")
    if len(encoded) > 72:
        raise ValueError("Password exceeds bcrypt's 72-byte limit")
    return bcrypt.hashpw(encoded, bcrypt.gensalt()).decode("ascii")


def verify_password(plain: str, hashed: str) -> bool:
    try:
        encoded = plain.encode("utf-8")
        return len(encoded) <= 72 and bcrypt.checkpw(encoded, hashed.encode("ascii"))
    except (ValueError, UnicodeError):
        return False


def credential_fingerprint(user: User) -> str:
    """这把凭据的指纹 —— 换一次密码换一个值。

    改密码之后旧会话要断，靠的就是它：token 里带着签它那一刻的指纹，
    `get_current_user` 拿它和用户**现在**那把凭据比。bcrypt 每次 hash 都用新
    的盐，所以"改成同一个密码"也会换指纹。

    用 HMAC 而不是裸 sha256：token 是**持有者读得到**的，裸摘要等于把一个与
    密码哈希一一对应的值发出去。加上服务端密钥之后，它在密钥之外没有含义。
    """
    return hmac.new(
        settings.secret_key.encode("utf-8"),
        user.hashed_password.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def how_long_a_token_lives(held: bool) -> timedelta:
    """这张 token 活多久 —— 由**谁保管它**决定，不由签它的端点决定。

    浏览器里的一张和桌面加密存着的一张，是两种东西：前者的风险是"这台电脑还
    有别人用"，后者的风险是"这台机器整个被拿走了"。同一个数字服务不了两种。

    三个入口（登录 / 注册后登录 / 建立组织）都经过这里，所以"被保管的凭据活
    多久"在服务器上只有一个答案。多写一处就会分叉，而分叉的那天没有人会发现
    ——两种都能用，只是有一种莫名其妙地每天掉线。
    """
    if held:
        return timedelta(days=settings.held_credential_expire_days)
    return timedelta(minutes=settings.access_token_expire_minutes)


def create_access_token(user: User, expires_delta: timedelta | None = None) -> str:
    """签一张 token：它归谁、活到什么时候、以及**是哪一把凭据签的**。

    没有 `iat`。从前有，而且是必填的 —— 因为"改密码之前发的 token 不算数"
    那道闸是拿 `iat` 和 `password_changed_at` 比时刻。那套判据整个删了
    （见 `get_current_user` 里 `cred` 那一段），`iat` 也就没有任何读者，
    留着只会把墙钟重新放回准入路径上：PyJWT 默认还要验 `iat`
    （`int(iat) > now` 就拒，leeway 为 0）。

    `jti` 让"同一个人、同一秒、连着登两次"拿到两张**不同**的 token。没有它，
    两次的载荷会逐字相同（`exp` 是整秒），于是撤销一张就撤销了另一张 ——
    登出一个标签页把另一个也踢下线，而且没有任何地方会报错。
    """
    expire = datetime.now(UTC) + (
        expires_delta or timedelta(minutes=settings.access_token_expire_minutes)
    )
    return jwt.encode(
        {
            "sub": user.id,
            "exp": expire,
            "cred": credential_fingerprint(user),
            "jti": secrets.token_urlsafe(24),
        },
        settings.secret_key,
        algorithm=ALGORITHM,
    )


#: 一张「只够调模型」的通行证在 token 里带的那个标记。
MODEL_PASS = "models"


def create_model_pass(user: User) -> str:
    """签一张只够调这个组织模型的通行证 —— 给成员的桌面，让他本机的项目也能用组织的模型。

    为什么不直接把账号的 token 交给调模型的那个进程：那张能做这个人在组织里能做的一切
    （读项目、改密码、发请柬）。调模型的进程只该拿到调模型的能力 —— 通行证泄露的最坏
    情况是"有人拿他的名义调了一阵组织的模型"，而不是"有人成了他"。

    它和账号 token 同一把凭据指纹：他改了密码、被停用，通行证当场作废。活多久按被保管的
    凭据算（桌面加密存着它）。`get_current_user` 拒收它（见 `purpose` 那一段）。
    """
    return jwt.encode(
        {
            "sub": user.id,
            "exp": datetime.now(UTC) + how_long_a_token_lives(True),
            "cred": credential_fingerprint(user),
            "jti": secrets.token_urlsafe(24),
            "purpose": MODEL_PASS,
        },
        settings.secret_key,
        algorithm=ALGORITHM,
    )


async def user_holding_a_model_pass(token: str, db: AsyncSession) -> User:
    """模型网关的准入：这张是不是一张还算数的**模型通行证**，它归谁。

    和 `get_current_user` 同一套判据（签名、撤销表、账号在不在、凭据指纹），只多一条：
    必须是通行证。账号 token 也不收 —— 网关只认为它签的那种。
    """
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=[ALGORITHM],
                             options={"require": ["sub", "exp", "cred"]})
    except InvalidTokenError as exc:
        raise _refuse("model pass did not decode", error=type(exc).__name__) from exc
    if payload.get("purpose") != MODEL_PASS:
        raise _refuse("not a model pass")
    if await db.get(RevokedAccessToken, token_digest(token)) is not None:
        raise _refuse("model pass was revoked", sub=payload["sub"])
    user = await db.scalar(select(User).where(User.id == payload["sub"]))
    if user is None or not user.is_active:
        raise _refuse("no such active user", sub=payload["sub"])
    if not hmac.compare_digest(str(payload["cred"]), credential_fingerprint(user)):
        raise _refuse("model pass minted under a superseded credential", sub=payload["sub"])
    return user


async def implicit_local_user(db: AsyncSession = Depends(get_db)) -> User:
    """个人档的「当前用户」：这台机器的主人。

    软件装在自己的电脑上，账号这件事从头到尾不存在 —— 带不带 token 都一样。
    装配层用它**覆盖** `get_current_user` 这个依赖（`app.dependency_overrides`），
    而不是在这里加一个模块级开关：开关是进程全局的，一条测试打开它，同一个
    worker 里后面每条测试都跟着变（2026-09-05 实测：10 条测试的"项目列表是空的"
    就是这么来的）。依赖覆盖挂在 app 上，谁装的谁负责，测试收尾一并清掉。
    """
    return await _get_or_create_local_user(db)


async def get_current_user(
    token: str | None = Depends(oauth2_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    """FastAPI dependency: extract and validate current user from JWT."""
    if not token and settings.debug:
        return await _get_or_create_local_user(db)

    if not token:
        raise _refuse("no bearer token on the request")

    try:
        # `require` 把"这张 token 没说自己是哪一把凭据签的"从一个可选字段变成
        # 一道拒绝：缺了 `cred`，下面那道闸就无从判断，只能 fail closed，而
        # **为什么**被拒会一路沉默到用户那里。
        #
        # **墙钟不在准入路径上。** 这里唯一和时间有关的是 `exp`，而时钟往回走
        # 只会让 token 多活一会儿，不会让有效的失效。从前不是这样：token 带
        # `iat`，PyJWT 默认拿它判"生效了没有"（`int(iat) > now` 就拒，leeway
        # 为 0，`int()` 还是截断），于是墙钟往回挪**几十毫秒**就能把刚签出去的
        # token 判成伪造 —— #836 / #933 那个"CI 上间歇性 401、本机从未复现"就是
        # 它（run 3086：两个独立 worker 进程在相隔约两秒内各判错一次，方向还
        # 相反；两个进程连库都是各自内存里的，共用的只有墙钟）。
        payload = jwt.decode(
            token,
            settings.secret_key,
            algorithms=[ALGORITHM],
            options={"require": ["sub", "exp", "cred"]},
        )
        user_id: str | None = payload.get("sub")
        if user_id is None:
            raise _refuse("token carries no subject")
    except InvalidTokenError as exc:
        raise _refuse("token did not decode", error=type(exc).__name__) from exc

    # 模型通行证（`create_model_pass`）只够调模型。拿它当账号用 = 调模型的进程里漏出去
    # 的那一张，成了"这个人"。
    if payload.get("purpose"):
        raise _refuse("a model pass cannot act as the account", sub=payload.get("sub"))

    # 签名有效不等于还算数：登出会把这一张记进撤销表。这道闸必须读库 —— 答
    # `/auth/me` 的可能是另一个 worker（组织档是另一台机器），进程内的集合对
    # 它不存在。
    if await db.get(RevokedAccessToken, token_digest(token)) is not None:
        raise _refuse("token was revoked by a logout", sub=user_id)

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise _refuse("no such user", sub=user_id)
    if not user.is_active:
        raise _refuse("account is not active", sub=user_id)

    # 改密码要把**别的**会话踢下线。问的是"签这张 token 的那把凭据，还是现在
    # 这把吗"—— 一个身份比较，不是两个时刻的先后。
    #
    # 从前问的是后者：`iat > password_changed_at`。两个数来自**同一口不可靠的
    # 井**，墙钟往回挪一下，分界就落在旧 token 的签发时刻之前，于是被偷走的那
    # 个会话活了下来（#836 的形状）。而那一侧**不能**靠加宽容度修：留一秒空窗
    # 的轮换不算轮换。所以时钟整个从这个问题里拿掉了。
    if not hmac.compare_digest(str(payload["cred"]), credential_fingerprint(user)):
        raise _refuse("minted under a superseded credential", sub=user_id)
    return user


def _refuse(reason: str, **facts: object) -> HTTPException:
    """401 不能只是一个数字。

    2026-08 起这套鉴权在 CI 上间歇性判错三次被立案（#836 / #933），三次都只能
    归档成"说不清"—— 因为判决点不说话。中间补过一处诊断，用的是 `logger.info`：
    **那一行在 CI 的失败报告里一次都没出现过**。pytest 没有配 `log_level`，
    root logger 就停在 WARNING；而把等级调到 INFO 的 `setup_logging` 只在
    lifespan 里跑，测试走 `ASGITransport`，根本不进 lifespan。等级要落在读的人
    真的读得到的那一层，否则"有诊断"和"没诊断"在报告里长得一模一样。

    记的是判据的输入，不是凭据：token 本身、密钥、密码一个字都不进日志。
    """
    if facts:
        logger.warning(
            "authentication refused: %s (%s)",
            reason,
            " ".join(f"{key}={value}" for key, value in facts.items()),
        )
    else:
        logger.warning("authentication refused: %s", reason)
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid authentication credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )


async def _get_or_create_local_user(db: AsyncSession) -> User:
    """这台机器的那一个人。

    个人档里它就是用户本人（没有别人）；DEBUG 模式下它是开发者。两种情况是
    同一件事：**这个进程服务于一个人，不需要他证明自己是谁。**
    """
    dev_email = "dev@research-platform.local"
    result = await db.execute(select(User).where(User.email == dev_email))
    user = result.scalar_one_or_none()
    if user:
        return user

    user = User(
        email=dev_email,
        # Random, and never surfaced: this account is reached without a password
        # at all under DEBUG, so a fixed literal would only be a published
        # credential that still works over /auth/login if DEBUG ever ships on.
        hashed_password=hash_password(secrets.token_urlsafe(32)),
        display_name="Dev Researcher",
    )
    db.add(user)
    await db.flush()
    return user

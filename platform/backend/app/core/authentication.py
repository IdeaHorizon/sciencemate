"""认证状态的写侧：把一张 access token 作废。"""
from datetime import UTC, datetime
from hashlib import sha256

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.authentication import RevokedAccessToken


def token_digest(token: str) -> str:
    """撤销表里的身份。存散列而不是 token 本身 —— 见模型的 docstring。"""
    return sha256(token.encode("utf-8")).hexdigest()


def _insert(db: AsyncSession, model):
    """按方言取 `insert`：`on_conflict_do_nothing` 不在方言中立的那一层。"""
    if db.get_bind().dialect.name == "sqlite":
        from sqlalchemy.dialects.sqlite import insert
    else:
        from sqlalchemy.dialects.postgresql import insert
    return insert(model)


async def revoke_access_token(db: AsyncSession, token: str, expires_at: datetime) -> None:
    """记下这一张不算数了，直到它自己过期。

    顺手清掉已经过期的行：过期之后签名本身就拦得住，留着只是让表无限长。清理
    挂在写路径上而不是一条定时任务上 —— 定时任务是第二套需要有人看着的东西，
    而这张表只在有人登出时才长。

    `on_conflict_do_nothing`：同一张 token 登出两次是正常操作（重试、两个标签
    页），不该变成 500。
    """
    await db.execute(
        delete(RevokedAccessToken).where(RevokedAccessToken.expires_at <= datetime.now(UTC))
    )
    statement = _insert(db, RevokedAccessToken).values(
        token_hash=token_digest(token), expires_at=expires_at
    )
    await db.execute(
        statement.on_conflict_do_nothing(index_elements=[RevokedAccessToken.token_hash])
    )

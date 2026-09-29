"""邀请码 —— 组织服务器上「谁能进来」的唯一答案。

## 为什么需要它

`/register` 此前完全开放：任何能连到这台服务器的人都能建号，落到默认机构和默认组。
而**没有任何路径能改一个人的角色**（全仓 app/scripts/deploy 零写入点），所以
node20 上那 1 个 institution_admin 和 1 个 group_admin 是直接塞进库的。

两件事合起来就是：门开着，而管门的人不存在。

## 一次一码

邀请码带着「进来之后是谁」——角色、机构、组。用掉即作废（`accepted_at`），过期即
无效。不做「一码多人」：那等于把一个链接变成一扇长期敞开的门，而它什么时候被转发
出去没有人知道。

码本身只存**哈希**。数据库被看到的时候，里面不该有一把还能用的钥匙 —— 同密码。
"""
from datetime import datetime
from uuid import uuid4

from sqlalchemy import String, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base, UTCDateTime


class Invitation(Base):
    """一张请柬：谁发的、进来是什么身份、有没有被用掉。"""

    __tablename__ = "invitations"

    id: Mapped[str] = mapped_column(
        Uuid(as_uuid=False), primary_key=True, default=lambda: str(uuid4())
    )
    #: 邀请码的 sha256。原文只在发出去那一次出现过。
    code_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    #: 用掉它的人进来是什么角色（`UserRole`）。
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    institution_id: Mapped[str] = mapped_column(String(64), nullable=False)
    institution_name: Mapped[str] = mapped_column(String(200), nullable=False)
    group_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    group_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    #: 只给这个邮箱用；空 = 谁拿到谁能用（仍然是一次性的）。
    email: Mapped[str | None] = mapped_column(String(320), nullable=True)

    created_by_user_id: Mapped[str] = mapped_column(Uuid(as_uuid=False), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    accepted_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    accepted_by_user_id: Mapped[str | None] = mapped_column(
        Uuid(as_uuid=False), nullable=True
    )

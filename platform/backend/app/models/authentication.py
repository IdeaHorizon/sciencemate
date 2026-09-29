"""每个 API 进程都要看得见的认证状态。

JWT 是自证的：签名对、没过期，它就有效 —— 服务端不需要记住任何东西，也因此
**说不出"这一张不算了"**。登出从前只是让浏览器把 token 扔掉，被复制走的那一份
照样能用到过期为止。撤销是一张表：它得在库里，因为 `/auth/me` 可能由另一个
worker（组织档是另一台机器）来答，进程内的集合对它不存在。

只存散列：这张表要是被读走，里面不能有任何一张还能用的 token。
"""
from datetime import datetime

from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base, UTCDateTime


class RevokedAccessToken(Base):
    __tablename__ = "revoked_access_tokens"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    #: 这张 token 自己的 `exp`。过了它，签名就已经拦得住了，这一行可以删 ——
    #: 索引是为了让清理走索引扫描，而不是全表。
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), index=True)

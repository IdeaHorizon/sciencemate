"""认证一次请求要读到的所有表。

几条测试自己建 schema（只建用得上的那几张 —— 默认 conftest 的全量 create_all
对它们太重）。那些名单里都有 `users`，因为它们服务的是**已登录**的请求。可
"登录这件事要读哪些表"不止 users 一张：撤销名单也在这条路上，而名单式的
fixture 不会因为多了一张表就报错 —— 它报的是 `no such table`，出现在一条跟
认证毫无关系的测试里（2026-09-14 实测 83 红）。

所以这里只有一处：认证长出新表时改这一行，六个 fixture 跟着对。
"""
from app.models.authentication import RevokedAccessToken
from app.models.user import User

AUTHENTICATION_TABLES = [User.__table__, RevokedAccessToken.__table__]

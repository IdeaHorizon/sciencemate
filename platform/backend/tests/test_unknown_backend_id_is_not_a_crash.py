""""这个后端不存在"只能有一个答案，不管它是怎么个不存在法。

现场（2026-08-13，node20 打真实请求）：PATCH 会话模型时塞一个不是 UUID 的
id，返回 500 —— 因为 id 列是 UUID，形状不对的字符串在 asyncpg 那层就抛
DataError，压根走不到下面那句 404。同一件事（后端不存在）因为不存在的方式
不同而给出两种答案，其中一种还说成是平台自己崩了。
"""

import pytest
from fastapi import HTTPException

from app.models.user import User
from app.services.model_backends import get_visible_backend


class _DB:
    """真实现场里这一步根本到不了 DB —— 到了就说明校验没拦住。"""

    async def scalar(self, _statement):
        raise AssertionError("malformed id must not reach the database")


def _user() -> User:
    return User(
        id="u1", email="u", hashed_password="x", display_name="u",
        role="researcher", institution_id="ieit",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["not-a-real-backend", "", "  ", "123", "z" * 40])
async def test_a_malformed_backend_id_is_404_not_500(bad):
    with pytest.raises(HTTPException) as caught:
        await get_visible_backend(_DB(), _user(), bad)
    assert caught.value.status_code == 404

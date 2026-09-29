"""「我是谁」—— 每种发行都要答的那一句。

个人版界面启动时就问它（隐式本机用户）；组织服务器上答的是 token 对应的账号。
登录、注册、登出、改口令是专业版的（`app/pro/api/auth.py`），个人版没有登录这回事。
"""
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_user
from app.database import get_db
from app.models.user import User
from app.policies import governance_scope_for, permissions_for
from app.schemas.auth import UserInfo, UserProfileUpdate

router = APIRouter()


def _user_info(user: User) -> dict:
    return {
        "id": user.id,
        "email": user.email,
        "display_name": user.display_name,
        "is_active": user.is_active,
        "role": user.role,
        "institution_id": user.institution_id,
        "institution_name": user.institution_name or "",
        # 管理员刚重置过他的密码 —— 界面据此把他挡在改密码那一步上。
        "must_change_password": bool(getattr(user, "must_change_password", False)),
        "governance_scope": governance_scope_for(user),
        "permissions": permissions_for(user),
    }

@router.get("/me", response_model=UserInfo)
async def get_me(user: User = Depends(get_current_user)) -> dict:
    """Return current authenticated user."""
    return _user_info(user)

@router.patch("/me", response_model=UserInfo)
async def update_me(
    data: UserProfileUpdate,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Update the sole editable account profile field."""
    user.display_name = data.display_name
    await db.flush()
    return _user_info(user)

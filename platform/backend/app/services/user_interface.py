"""这个人的界面偏好 —— 包括他要用哪种语言看平台。

## 为什么单独一个模块

`_effective_interface` 原先长在 `api/v1/settings.py` 里，只有设置页用得着。
资讯流要按语言出文案，就也需要它 —— 而"再实现一遍"意味着两处对同一份
偏好各有一套解释，分叉时不报错（设置页显示英文、资讯流还给中文）。

所以搬到服务层，两边都调它。settings 端点保留原有行为，只是不再自己实现。
"""

from __future__ import annotations

from typing import Literal

from pydantic import ValidationError

from app.models.user import User
from app.schemas.settings import InterfaceSettings

Language = Literal["zh", "en"]

_DEFAULTS = InterfaceSettings()


def effective_interface(user: User) -> InterfaceSettings:
    """把存下来的偏好读成一份**每个字段都在**的设置。

    逐字段验证而不是整份验证：偏好是长期累积的 JSON，某个历史字段变成
    非法值时，整份验证会把这个人的**全部**偏好一起打回默认 —— 一次
    改名就能让所有人的主题、密度、落地页同时复位。
    """
    normalized = _DEFAULTS.model_dump()
    # `getattr` 而不是 `user.preferences`：这个函数现在也被执行状态那条链调用，
    # 而那条链上有不带偏好的用户对象（测试替身、以及只取了几列的投影）。
    # 读不到偏好只意味着"按默认来"，不该让一个 API 响应整个垮掉。
    raw = getattr(user, "preferences", None)
    preferences = raw if isinstance(raw, dict) else {}
    stored = preferences.get("interface")
    if not isinstance(stored, dict):
        return InterfaceSettings.model_validate(normalized)
    for field in normalized:
        if field not in stored:
            continue
        candidate = {**normalized, field: stored[field]}
        try:
            validated = InterfaceSettings.model_validate(candidate)
        except ValidationError:
            continue
        normalized[field] = getattr(validated, field)
    return InterfaceSettings.model_validate(normalized)


def language_for(user: User | None) -> Language:
    """这个人要用哪种语言看界面。

    没有用户（后台采集任务、健康探针）时给默认语言 —— 那些场合的文案
    不面向某个具体的人。
    """
    if user is None:
        return _DEFAULTS.language
    return effective_interface(user).language

"""这个人的界面偏好 —— 包括他要用哪种语言看平台。

## 为什么单独一个模块

`_effective_interface` 原先长在 `api/v1/settings.py` 里，只有设置页用得着。
资讯流要按语言出文案，就也需要它 —— 而"再实现一遍"意味着两处对同一份
偏好各有一套解释，分叉时不报错（设置页显示英文、资讯流还给中文）。

所以搬到服务层，两边都调它。settings 端点保留原有行为，只是不再自己实现。
"""

from __future__ import annotations

from typing import Literal

from fastapi import Request
from pydantic import ValidationError

from app.models.user import User
from app.schemas.settings import InterfaceSettings
from app.services.say import SaidToAPerson, in_both_languages, said_of, say  # noqa: F401 —— 端点从这里拿，见 services/say.py

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


#: 前端每一问都带着这一屏正在显示的语言（`lib/api.ts` 的 `fetchWithAuth`）。
SCREEN_LANGUAGE_HEADER = "x-interface-language"


def language_on_screen(request: Request) -> Language:
    """这一问的回答（尤其是报错）该用哪种语言说：发问那一屏正显示的那种。

    为什么不是 `language_for(user)`：一半的端点在那一刻没有用户 —— 注册、登录前、
    桌面本地的组织连接、组织服务器的模型通行证。而报错是说给**正看着这一屏的人**
    听的，前端知道那一屏是什么语言（登录前点过的也算），每一问都带上。转交到组织
    服务器时这个头原样过去，那边也按桌面这一屏说话，而不是按服务器上那份账号的偏好。

    没带（脚本、worker、旧前端）就是默认语言。
    """
    return language_said_by(request.headers.get(SCREEN_LANGUAGE_HEADER))


def language_said_by(header_value: str | None) -> Language:
    """`X-Interface-Language` 那个头的值读成一种语言 —— 中间件（拿不到 `Request` 的那几层：
    请求体闸、转交给组织服务器的 relay）和端点读的是同一条规则。"""
    value = (header_value or "").strip().lower()
    return value if value in ("zh", "en") else _DEFAULTS.language  # type: ignore[return-value]

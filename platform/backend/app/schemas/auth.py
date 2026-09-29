"""Authentication request/response schemas."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator


class UserRegister(BaseModel):
    email: str
    password: str
    display_name: str
    #: 组织服务器默认要它（`settings.registration_mode == "invite"`）。个人档没有
    #: 注册这回事；`open` 模式下忽略。
    invite_code: str | None = None


class UserLogin(BaseModel):
    email: str
    password: str
    #: 进哪个组织。一台服务器上住着好几个 —— 不说就只有"这台只有一个组织"时才不歧义。
    organisation: str | None = None
    #: 拿到的这张要不要**保管**起来（桌面把它加密存进本机钥匙串）。
    #:
    #: 由客户端说，因为只有它知道自己是什么：一个共享电脑上的浏览器标签页，还是
    #: 一台个人机器上的应用。服务器判断不了，猜一个就必然对另一种是错的。
    remember_this_device: bool = False


class UserProfileUpdate(BaseModel):
    display_name: str = Field(min_length=1, max_length=100)

    model_config = ConfigDict(extra="forbid")

    @field_validator("display_name", mode="before")
    @classmethod
    def normalize_display_name(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        normalized = value.strip()
        if not normalized:
            raise ValueError("display_name must not be blank")
        return normalized


class ChangePasswordRequest(BaseModel):
    """Length and byte-ceiling checks deliberately live in
    app.pro.password_policy, not here: the endpoint reports every violation at
    once and the UI renders that same list. A `min_length` on this field would
    be a second, weaker copy of the rule that short-circuits the real one."""

    current_password: str
    new_password: str

    model_config = ConfigDict(extra="forbid")


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"


class PasswordChangeResponse(TokenResponse):
    """A rotation invalidates every token issued before it — including the one
    that made the request — so the caller gets a replacement and stays signed in
    on this device while its other sessions end."""

    changed_at: datetime


class PasswordCandidate(BaseModel):
    """一个还没被用来注册的密码 —— 只为问一句「它行不行」。

    `email` / `display_name` 可给可不给：给了才检查得了「密码里不许有你的邮箱名」
    那一条，而建立组织那个表单这两样都在手边。
    """

    password: str
    email: str | None = None
    display_name: str | None = None


class PasswordPolicyInfo(BaseModel):
    min_length: int
    max_bytes: int
    rules: list[str]


class GovernanceScope(BaseModel):
    kind: str
    id: str
    name: str


class UserInfo(BaseModel):
    id: str
    email: str
    display_name: str
    is_active: bool
    role: str
    #: 这个账号属于**哪个组织**。2026-09-22 起账号按组织唯一（同一台服务器上住着
    #: 好几个组织，同一个人在几个里各有一个账号是正常的），所以"你是谁"答不完整
    #: 除非把这个说出来。
    #:
    #: 凭邀请码注册时更是唯一的答案来源：码上带着身份，注册的人事先并不知道自己
    #: 落进了哪个组织 —— 不报这一个，桌面就无从知道刚建的连接属于谁。
    institution_id: str
    institution_name: str = ""
    governance_scope: GovernanceScope
    permissions: list[str]

    model_config = {"from_attributes": True}
    #: 管理员刚重置过密码，他手上是一次性口令。
    must_change_password: bool = False

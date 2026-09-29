"""这台 App Server 提供哪些能力 —— 前端据此渲染。

## 为什么是一个不鉴权的端点

个人档里没有登录这一步，所以前端在拿到任何身份之前就得知道「这里要不要登录」。
把它藏在鉴权后面，就成了一个先有鸡还是先有蛋的问题。

返回的内容里没有任何私密的东西：它只说这台服务器**装配成了什么形态**，
和"谁在用"无关。
"""
from __future__ import annotations

from fastapi import APIRouter

from app import assembly

router = APIRouter()

#: 「这台服务器认哪几种登录」—— 由有登录这回事的发行登记（专业版：`services/sign_in`）。
SIGN_IN_METHODS_EXTENSIONS: list = []


@router.get("/capabilities")
async def get_capabilities() -> dict:
    """能力集合 + 档位。前端按它决定画什么，不按档位名字判。

    档位名字也交出去，但**只作为显示**（状态栏里说清楚这是本机还是组织）。
    界面上任何"要不要画"的判断都必须落在 features 上 —— 名字会变，能力不会。
    """
    payload: dict = {
        "profile": assembly.profile_name(),
        "features": sorted(assembly.capabilities()),
    }
    # 发行只作为显示（关于页写"个人版 / 专业版"），而且**只有桌面安装才有这个问题**。
    #
    # 组织服务器上它没有意义：那台机器不是谁的桌面，也不从发行的更新源自更新。
    # 此前它照答不误，答的是回落值 `personal` —— 一台专业版的服务器自报"个人版"，
    # 而那是**一句假话**（2026-09-16 真装一台服务器时看见的）。答不出的问题就不答。
    if assembly.profile_name() == "personal":
        payload["edition"] = assembly.edition_name()
    # 凭据主密钥**实际**存在哪。向导要对用户说一句关于 key 的话，而那句话的真假
    # 取决于这次安装到底落在钥匙串还是退回了文件 —— 事实在后端，界面不许猜。
    # `where_the_key_lives()` 早就存在（doctor 在用），只是从来没有接口把它送到
    # 界面手里，于是文案只能说一句正确但含糊的话。
    #
    # **只在个人档交出。** 这个接口不需要登录就能读：个人档只监听 127.0.0.1，读
    # 得到它的就是本人；组织档是网络可达的，把"这份安装退回了较弱的存法"告诉一个
    # 还没登录的人，是白送一条侦察情报。组织档的答案恒为 operator，本来也没有第二
    # 种可能，界面用兜底那句就够。
    if assembly.profile_name() == "personal":
        payload["credentialKeyStorage"] = _where_the_credential_key_lives()
    # 这台服务器允许哪几种登录方式。今天恒为 ["password"]，但登录页要按**这个**
    # 画，不许写死一张表单 —— 接了单位的账号系统之后（LDAP / 企业微信 / 学校
    # OAuth），多出来的按钮是从这里长出来的，不是再改一次登录页。
    #
    # 只在有登录这回事的服务器上交出：个人档没有门，答它等于凭空多一个概念。
    if "auth" in assembly.capabilities():
        payload["signInMethods"] = [method for ask in SIGN_IN_METHODS_EXTENSIONS for method in ask()]
    return payload


def _where_the_credential_key_lives() -> str:
    """给界面看的一个词：keychain / file / operator。算不出来就说 unknown。

    全程吞异常：能力接口是首屏第一个请求，绝不能因为一个说明性字段而挂掉。
    """
    try:
        from app.services.credential_key import where_the_key_lives

        answer = where_the_key_lives()
    except Exception:
        return "unknown"
    if "keychain" in answer.lower():
        return "keychain"
    if "operator" in answer.lower():
        return "operator"
    if "0600" in answer:
        return "file"
    return "unknown"

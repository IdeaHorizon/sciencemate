"""资讯流里**由后端产生**的文案（分区标题、说不出内容时的原因、报错正文）。

## 为什么后端也要有一张表

前端能翻译的只有前端自己写的字。分区标题、"这台部署关掉了外部抓取"这类句子
是后端算出来的 —— 它们随数据走（有没有 for-you 分区取决于匹配结果），不可能
在前端按 key 猜出来。所以后端产生的字在后端翻，前端产生的字在前端翻：
**每一句只有一处定义**，两边不是彼此的抄件。

## 缺翻译时给什么

给中文原文，不给 key。一个 `feed.section.for_you` 出现在界面上，对用户来说
就是坏了；一句没翻的中文只是没翻。
"""

from __future__ import annotations

#: key → {语言: 文案}。加一句就在这里加一行，不要在端点里写字面量。
COPY: dict[str, dict[str, str]] = {
    "empty.collection_disabled": {
        "zh": "外部内容采集已关闭，科研资讯只显示组织内部内容和用户分享的链接。",
        "en": "External collection is turned off. The feed shows only internal posts and "
              "shared links.",
    },
    "empty.scheduler_off": {
        "zh": "自动采集已关闭，暂时不会有新内容。",
        "en": "Automatic collection is turned off, so no new items will arrive.",
    },
    "empty.first_round": {
        "zh": "内容池还是空的 —— 第一轮采集通常在服务启动后几分钟内完成。",
        "en": "The pool is still empty — the first collection round usually finishes a "
              "few minutes after startup.",
    },
    # 个人档**从不**起后台采集（assembly.background_collection_enabled 只在 org
    # 档为真）。在它上面说"第一轮几分钟内完成"是一句永远兑现不了的话：等多久
    # 都不会有内容，而界面一直在暗示再等等。空态要说清楚这台机器上到底会不会
    # 有东西进来，以及此刻你能做什么。
    # 以前还说「或连接组织服务器查看组织采集的内容」—— 连了组织也看不到：资讯请求从不转给组织服务器
    # （没有 X-Organisation，也不是项目级路径），桌面的资讯永远是本机这一份（11-04 核过）。
    "empty.no_collector_here": {
        "zh": "这台电脑不会自动采集资讯。你可以在「搜索」中检索文献，或分享一条链接。",
        "en": "This computer does not collect items automatically. You can search for papers "
              "under Search, or share a link.",
    },
    "error.vocabulary_filter": {
        "zh": "暂时无法按学科筛选，请稍后重试。",
        "en": "Filtering by field is unavailable right now. Try again later.",
    },
    "error.vocabulary_interests": {
        "zh": "暂时无法修改订阅，请稍后重试。",
        "en": "Subscriptions can't be changed right now. Try again later.",
    },
    "error.vocabulary_catalog": {
        "zh": "无法加载学科列表，请稍后重试。",
        "en": "Could not load the list of fields. Try again later.",
    },
    "error.unknown_domains": {
        "zh": "无法识别这些学科：{domains}",
        "en": "Unrecognized fields: {domains}",
    },
    "error.paper_has_no_identity": {
        "zh": "这篇文献没有 DOI 或链接，无法收藏。",
        "en": "This paper has no DOI or link, so it cannot be saved.",
    },
    "error.link_has_no_identity": {
        "zh": "无法添加这个链接，请换一个链接再试。",
        "en": "This link cannot be added. Try a different link.",
    },
    "curation.no_model": {
        "zh": "还没有可用的资讯挖掘模型。请在「设置 → 模型与密钥」中，为一个模型连接勾选"
              "「资讯挖掘模型」角色，再打开此开关。",
        "en": "No feed curation model is available yet. Under Settings → Models & credentials, "
              "tick the “Feed curation model” role on a connection, then turn this on.",
    },
}


def t(key: str, lang: str = "zh", **fields: str) -> str:
    """按语言取一句文案。`{name}` 占位符用关键字参数填。"""
    entry = COPY.get(key)
    if entry is None:
        # 缺 key 是代码错误，不该让用户看见一个 key —— 但也不该让整个请求
        # 垮掉。返回 key 本身，日志里就能一眼看到是哪句忘了加。
        return key
    text = entry.get(lang) or entry.get("zh") or key
    return text.format(**fields) if fields else text

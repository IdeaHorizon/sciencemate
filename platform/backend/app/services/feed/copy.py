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
        "zh": "这台部署关掉了外部抓取，资讯流只会显示组织内部内容与用户分享。",
        "en": "External collection is switched off on this deployment; the feed shows "
              "only internal posts and shared links.",
    },
    "empty.scheduler_off": {
        "zh": "采集调度已关闭，暂时不会有新内容进来。",
        "en": "The collection scheduler is stopped, so nothing new is arriving.",
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
    "empty.no_collector_here": {
        "zh": "这台机器不在后台爬网，所以内容池是空的。你可以自己分享一条，"
              "或者连上组织服务器，用那边采到的。",
        "en": "This machine does not crawl in the background, so the pool is empty. "
              "Share a link yourself, or connect an organisation server and read what "
              "it collects.",
    },
    "error.vocabulary_filter": {
        "zh": "域词表暂时不可用，无法按方向筛选（这是平台侧故障，与内容无关）",
        "en": "The domain vocabulary is unavailable, so filtering by field is off right "
              "now (a platform fault, not a content one)",
    },
    "error.vocabulary_interests": {
        "zh": "域词表暂时不可用，现在改不了关注方向（平台侧故障）",
        "en": "The domain vocabulary is unavailable, so interests cannot be changed right "
              "now (a platform fault)",
    },
    "error.vocabulary_catalog": {
        "zh": "域词表暂时不可用（平台侧故障）—— 不展示一份猜出来的分类表",
        "en": "The domain vocabulary is unavailable (a platform fault) — no guessed "
              "taxonomy will be shown",
    },
    "error.unknown_domains": {
        "zh": "这些方向不在词表里：{domains}",
        "en": "These fields are not in the vocabulary: {domains}",
    },
    "error.link_has_no_identity": {
        "zh": "这个链接算不出稳定身份，收不了",
        "en": "This link has no stable identity, so it cannot be accepted",
    },
    "curation.no_model": {
        "zh": "还没有可用的资讯挖掘模型。到 设置 → 模型 里给一个连接勾上"
              "「资讯挖掘模型」角色，再回来打开这个开关。",
        "en": "No model is available for feed curation yet. Give a connection the "
              "“Feed curation model” role under Settings → Models, then come back and "
              "turn this on.",
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

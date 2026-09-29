"""一条资讯的身份 —— 同一件事从几个源进来，必须收敛成同一个键。

## 为什么身份必须是机械算出来的

同一篇论文会从 arXiv 的分类流、某个期刊 RSS、和 HuggingFace 的精选流三处
同时进来。没有统一身份的后果有两层：

1. 用户在同一天的日报里看见同一篇三次；
2. **热度算不出来**。"这篇正在被广泛讨论"这个判断，机械形态就是"几个互相
   独立的渠道提到了同一个键"。键要是不收敛，多源提及看起来就只是三条各不
   相干的内容 —— 注意力信号在落库那一刻就被丢掉了。

## 优先级：DOI > arXiv ID > 归一化 URL

前两个是**出版界已经定好的**标识符，全球唯一且稳定。URL 只是兜底：同一份
东西可以有无数个 URL（带 utm 参数的、带 www 的、http 的、末尾多个斜杠的），
所以用它之前要先归一化。

## 为什么不做标题模糊匹配

试过在脑子里推演：标题相似度合并能多抓一些，但**合错的代价不对称**。两条
真不同的工作被并成一条，用户永远看不到被吞掉的那条，而且没有任何症状 ——
它就是不在那里。宁可偶尔重复显示。

预印本和正式发表版**故意不合并**：它们本来就是两份不同的产物（版本、同行
评议状态、可引用性都不同），科研语境里把它们当成一件事是错的。
"""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlsplit, urlunsplit

#: arXiv 新版编号：`2401.01234` / `2401.01234v3`（2007 年 4 月起）。
_ARXIV_NEW = re.compile(r"\b(\d{4}\.\d{4,5})(v\d+)?\b")

#: arXiv 旧版编号：`cond-mat/0701001`。
_ARXIV_OLD = re.compile(r"\b([a-z-]+(?:\.[A-Za-z-]+)?)/(\d{7})(v\d+)?\b")

#: DOI：`10.` 开头，注册机构号，斜杠，后缀。末尾的标点不算 DOI 的一部分
#: （HTML 里 DOI 常以句号结尾）。
_DOI = re.compile(r"\b(10\.\d{4,9}/[^\s\"'<>]+?)(?=[.,;)\]]*(?:\s|$))", re.IGNORECASE)

#: 只用于追踪、不影响指向哪个资源的查询参数。
_TRACKING_PARAMS = frozenset(
    {
        "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
        "ref", "referrer", "source", "src", "fbclid", "gclid", "mc_cid", "mc_eid",
        "error", "code", "rss", "sap-outbound-id", "WT.ec_id", "wt.ec_id",
    }
)


def arxiv_id(text: str) -> str | None:
    """从任意文本（URL、guid、摘要）里摘出 arXiv 编号，**去掉版本后缀**。

    去版本不是省事：`2401.01234v1` 和 `v2` 是同一篇论文的两稿。留着版本号
    就等于每次作者更新都在日报里多出一条"新论文"。
    """
    match = _ARXIV_NEW.search(text or "")
    if match:
        return match.group(1)
    match = _ARXIV_OLD.search(text or "")
    if match:
        return f"{match.group(1)}/{match.group(2)}"
    return None


def doi(text: str) -> str | None:
    """从任意文本里摘出 DOI，小写化。

    DOI 规范说前缀大小写不敏感、后缀敏感，但注册实践里大小写混用普遍，
    而"同一个 DOI 因为大小写被当成两条"比"两个仅大小写不同的 DOI 被合并"
    常见得多。统一小写。
    """
    match = _DOI.search(text or "")
    if not match:
        return None
    return match.group(1).lower().rstrip(".,;")


def normalize_url(url: str) -> str:
    """URL 归一化：去掉不影响"指向哪个资源"的那些差异。"""
    raw = (url or "").strip()
    if not raw:
        return ""
    parts = urlsplit(raw)
    scheme = (parts.scheme or "https").lower()
    host = parts.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    # 同一份资源的 http/https 两个地址是同一份资源。统一到 https 之后，
    # 键就不会因为源里写了哪个协议而分叉。
    if scheme in ("http", "https"):
        scheme = "https"
    path = parts.path.rstrip("/") or "/"
    kept = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if k not in _TRACKING_PARAMS]
    query = "&".join(f"{k}={v}" for k, v in sorted(kept))
    # fragment 一律丢：`#abstract` 和 `#references` 是同一个页面。
    return urlunsplit((scheme, host, path, query, ""))


def canonical_key(*, url: str = "", guid: str = "", text: str = "") -> str:
    """一条资讯的稳定身份。

    `text` 给的是摘要/正文这类可能内嵌标识符的自由文本 —— 很多期刊 RSS 的
    链接指向自家落地页，DOI 只出现在描述里。

    返回空字符串表示**算不出身份**。调用方必须把这种条目丢掉而不是造一个
    键：造出来的键下次算还是不一样，同一条内容会天天作为"新内容"重新出现。
    """
    haystack = " ".join(p for p in (url, guid, text) if p)
    found_doi = doi(haystack)
    if found_doi:
        return f"doi:{found_doi}"
    found_arxiv = arxiv_id(haystack)
    if found_arxiv:
        return f"arxiv:{found_arxiv}"
    normalized = normalize_url(url) or normalize_url(guid)
    if normalized and normalized != "https:///":
        return f"url:{normalized}"
    return ""

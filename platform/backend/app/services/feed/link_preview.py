"""用户投喂一个链接，平台把它做成一张卡 —— 顺带堵死 SSRF。

## 为什么这个功能值得有

X / 公众号 / 知乎这些封闭生态没有可用的开放接口（贵、限流、或者干脆反爬），
硬爬既不稳也不体面。但科研信息确实有一大半先在那些地方发生。

转化办法不是更强的爬虫，是**把它变成社区功能**：用户看到一条好东西，贴个
链接进来，平台抓标题和摘要做成卡片，署"由某某分享"。发原创贴很难，转一条
链接加一句话人人肯干 —— 冷启动期的用户内容大概率全从这来。

## 为什么这里必须有一道网络护栏

这是整个平台上**唯一**一处"用户给一个地址，服务器就去访问它"的地方。不设
防的话，任何一个登录用户都能让 App Server 去打：

  - `http://169.254.169.254/…`（云厂商的元数据服务，取得起实例凭据）
  - `http://127.0.0.1:18081/…`（平台自己的后端）
  - 内网里任何一台机器

而返回内容会被当成"卡片摘要"显示给他看。这不是理论风险，是 SSRF 的教科书
形态。

所以：**先解析域名拿到 IP，逐个判是不是公网地址，再决定发不发这个请求。**
重定向必须一跳一判 —— 只判第一个 URL 而让 httpx 自动跟跳，等于没判：
攻击者给一个公网地址，让它 302 到 169.254.169.254。
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import socket
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

from app.services.feed.collectors import (
    FETCH_TIMEOUT_SECONDS,
    USER_AGENT,
    _clean_text,
    _summary,
)

logger = logging.getLogger(__name__)

#: 最多跟几跳重定向。每一跳都要重新过地址检查。
MAX_REDIRECTS = 5

#: 抓取上限。做一张卡只需要 <head>，正文再大也没用。
MAX_PREVIEW_BYTES = 2 * 1024 * 1024

_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_META_RE = re.compile(
    r"""<meta\s+[^>]*?(?:name|property)\s*=\s*["'](?P<key>[^"']+)["'][^>]*?"""
    r"""content\s*=\s*["'](?P<value>[^"']*)["']""",
    re.IGNORECASE,
)
_META_REVERSED_RE = re.compile(
    r"""<meta\s+[^>]*?content\s*=\s*["'](?P<value>[^"']*)["'][^>]*?"""
    r"""(?:name|property)\s*=\s*["'](?P<key>[^"']+)["']""",
    re.IGNORECASE,
)


class LinkRejected(ValueError):
    """这个链接不能抓。消息会**原样给到用户**，所以要说人话。"""


@dataclass(slots=True)
class LinkPreview:
    url: str
    title: str
    summary: str
    venue: str


def _is_public_address(raw: str) -> bool:
    try:
        address = ipaddress.ip_address(raw)
    except ValueError:
        return False
    # `is_global` 一个属性就覆盖了私网/回环/链路本地/保留/多播的全部情况。
    # 自己列网段是名单式护栏 —— 少写一个就是一个洞，而且不会有任何症状。
    return address.is_global


async def _assert_reachable_and_public(url: str) -> None:
    """域名解析出来的**每一个** IP 都得是公网地址。

    要判全部而不是第一个：一个域名可以同时解析出一个公网 A 记录和一个内网
    AAAA 记录，而实际连哪个由系统决定 —— 只判一个等于让攻击者去挑。
    """
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise LinkRejected("只支持 http/https 链接")
    host = parts.hostname
    if not host:
        raise LinkRejected("链接里没有主机名")
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(
            host, parts.port or (443 if parts.scheme == "https" else 80),
            proto=socket.IPPROTO_TCP,
        )
    except socket.gaierror as exc:
        raise LinkRejected(f"解析不了这个域名：{host}") from exc
    if not infos:
        raise LinkRejected(f"解析不了这个域名：{host}")
    for info in infos:
        address = info[4][0]
        if not _is_public_address(str(address)):
            raise LinkRejected(
                "这个地址指向内网或本机，平台不会去访问它。"
                "请贴一个公开可访问的链接。"
            )


async def fetch_preview(url: str) -> LinkPreview:
    """抓一个用户给的链接，做成卡片素材。"""
    current = (url or "").strip()
    if not current:
        raise LinkRejected("链接为空")

    async with httpx.AsyncClient(
        timeout=FETCH_TIMEOUT_SECONDS,
        # 自动跟跳会绕过地址检查（公网地址 302 到内网），所以关掉，手工跟。
        follow_redirects=False,
        headers={"User-Agent": USER_AGENT},
    ) as client:
        for _ in range(MAX_REDIRECTS + 1):
            await _assert_reachable_and_public(current)
            try:
                response = await client.get(current)
            except httpx.HTTPError as exc:
                raise LinkRejected(f"打不开这个链接：{type(exc).__name__}") from exc
            if response.is_redirect:
                location = response.headers.get("location")
                if not location:
                    raise LinkRejected("这个链接跳转到了一个空地址")
                current = str(httpx.URL(current).join(location))
                continue
            if response.status_code >= 400:
                raise LinkRejected(f"这个链接返回 HTTP {response.status_code}")
            return _extract(current, response)
    raise LinkRejected("这个链接跳转太多次了")


def _extract(url: str, response: httpx.Response) -> LinkPreview:
    body = response.content[:MAX_PREVIEW_BYTES]
    try:
        html = body.decode(response.encoding or "utf-8", errors="replace")
    except (LookupError, TypeError):
        html = body.decode("utf-8", errors="replace")

    meta: dict[str, str] = {}
    for pattern in (_META_RE, _META_REVERSED_RE):
        for match in pattern.finditer(html):
            key = match.group("key").strip().lower()
            meta.setdefault(key, match.group("value"))

    title = _clean_text(
        meta.get("og:title") or meta.get("twitter:title") or _first_title(html)
    )
    if not title:
        raise LinkRejected("这个页面没有标题，做不成卡片")
    description = (
        meta.get("og:description")
        or meta.get("twitter:description")
        or meta.get("description")
        or ""
    )
    venue = _clean_text(meta.get("og:site_name") or "") or (urlsplit(url).hostname or "")
    return LinkPreview(
        url=url,
        title=title[:500],
        summary=_summary(description),
        venue=venue[:200],
    )


def _first_title(html: str) -> str:
    match = _TITLE_RE.search(html)
    return match.group(1) if match else ""

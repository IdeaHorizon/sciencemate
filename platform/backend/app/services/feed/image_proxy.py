"""条目配图由**后端代取**，不让浏览器直连出版商。

## 为什么不直接把外链交给 `<img src>`

那等于每次有人打开资讯流首页，浏览器就替他向 aps.org / phys.org /
quantamagazine.org 各发一次请求，带上他的 IP、User-Agent 和 Referer
（`https://<平台>/feed`）。出版商由此拿到一份"哪个机构的谁、在什么时间、
在读哪些条目"的日志 —— 而用户并没有点开那篇文章。

平台在别处已经定过同一件事（PR#642 摆图进对话：图片源只许工作区路径，
外部 URL 渲染即外泄）。资讯流是外部内容，但泄露的机制一模一样，所以结论
也一样：代理。

## 为什么接口收 item_id 而不是 URL

收 URL 的代理就是一个 SSRF 装置：任何登录用户都能让 App Server 去打内网。
收 `item_id` 之后，要取的地址来自**我们自己的库**（采集时从 feed 里抓到的），
用户无法指定，SSRF 面直接是零 —— 不需要再写一遍地址校验。

（对照 `link_preview`：那里用户**必须**能给任意 URL，所以那条路上有一整套
逐跳解析 + 公网地址校验。两条路的对手不同，防法也就不同。）

## 缓存

同一张图会被很多用户、很多次刷新请求。进程内缓存一份字节，容量按条目数
封顶；超了按最久未用淘汰。这不是性能优化，是**礼貌**：不缓存的话我们会
把每次刷新都转成一次对出版商的请求。
"""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict

import httpx

from app.services.feed.collectors import FETCH_TIMEOUT_SECONDS, USER_AGENT

logger = logging.getLogger(__name__)

#: 单张图的上限。卡片上是缩略图，超过这个尺寸的不是缩略图。
MAX_IMAGE_BYTES = 3 * 1024 * 1024

#: 缓存几张。按条目算，不按字节 —— 一屏十几张，几百张足够覆盖翻几页。
CACHE_CAPACITY = 400

#: 只认这些类型。`image/*` 里 svg 单独排除：SVG 能内嵌脚本，
#: 而它会以我们自己的 origin 被加载。
ALLOWED_CONTENT_TYPES = (
    "image/png", "image/jpeg", "image/gif", "image/webp", "image/avif",
)


class ImageUnavailable(RuntimeError):
    """这张图取不回来。调用方回 404 —— 卡片按无图渲染，不是错误。"""


_cache: OrderedDict[str, tuple[bytes, str]] = OrderedDict()
_lock = asyncio.Lock()


async def fetch(url: str) -> tuple[bytes, str]:
    """取一张图，返回 (字节, content-type)。带进程内 LRU。"""
    async with _lock:
        hit = _cache.get(url)
        if hit is not None:
            _cache.move_to_end(url)
            return hit

    try:
        async with httpx.AsyncClient(
            timeout=FETCH_TIMEOUT_SECONDS,
            follow_redirects=True,
            # 不带 Referer：我们代取时也没有理由告诉对方是谁在看。
            headers={"User-Agent": USER_AGENT, "Accept": "image/*"},
        ) as client:
            response = await client.get(url)
    except httpx.HTTPError as exc:
        raise ImageUnavailable(f"{type(exc).__name__}: {exc}") from exc

    if response.status_code >= 400:
        raise ImageUnavailable(f"HTTP {response.status_code}")

    content_type = (response.headers.get("content-type") or "").split(";")[0].strip().lower()
    if content_type not in ALLOWED_CONTENT_TYPES:
        # 类型不对就不返回。把任意字节按 image/* 发给浏览器，等于让出版商
        # 决定在我们的 origin 下加载什么。
        raise ImageUnavailable(f"unexpected content-type {content_type!r}")

    body = response.content
    if len(body) > MAX_IMAGE_BYTES:
        raise ImageUnavailable(f"image is {len(body)} bytes, over the cap")

    async with _lock:
        _cache[url] = (body, content_type)
        _cache.move_to_end(url)
        while len(_cache) > CACHE_CAPACITY:
            _cache.popitem(last=False)
    return body, content_type


def cache_size() -> int:
    """给测试与状态端点用。"""
    return len(_cache)


def clear_cache() -> None:
    _cache.clear()

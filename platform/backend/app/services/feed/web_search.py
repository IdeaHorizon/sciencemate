"""Feed web discovery using the shared HTTP client and its normal fallback policy."""
from __future__ import annotations
import os
import re
from html import unescape
from urllib.parse import quote_plus
import httpx
from typing import Any
from shared.tools.web import _http_get, _strip_html, _ddg_search, log

_BAIDU_HEADING_RE = re.compile(
    r'<h3\b[^>]*class="[^"]*\bt\b[^"]*"[^>]*>(.*?)</h3>', re.S | re.I,
)
_BAIDU_LINK_RE = re.compile(r'<a\b[^>]*href="([^"]+)"[^>]*>(.*?)</a>', re.S | re.I)


def _parse_baidu_results(html: str, limit: int) -> list[dict[str, str]]:
    results: list[dict[str, str]] = []
    for heading in _BAIDU_HEADING_RE.findall(html):
        match = _BAIDU_LINK_RE.search(heading)
        if match is None:
            continue
        title = _strip_html(match.group(2))
        url = unescape(match.group(1))
        if title and url:
            results.append({"title": title, "url": url, "snippet": ""})
            if len(results) >= limit:
                break
    return results


async def _baidu_search(query: str, limit: int) -> dict:
    """百度 HTML 后端。url 是 baidu.com/link?url=... click-tracking 形式，
    web_fetch follow_redirects 后能拿真页面。"""
    url = f"https://www.baidu.com/s?wd={quote_plus(query)}"
    try:
        r = await _http_get(url)
    except httpx.HTTPError as e:
        return {"status": "error", "error": f"{type(e).__name__}: {e}",
                "backend": "baidu"}

    if r.status_code >= 400:
        return {"status": "error", "error": f"HTTP {r.status_code}",
                "backend": "baidu"}

    results = _parse_baidu_results(r.text, limit)

    if not results:
        return {"status": "error",
                "error": "no results parsed (possibly rate-limited or DOM changed)",
                "backend": "baidu"}

    return {"status": "success", "backend": "baidu", "query": query,
            "results": results, "total": len(results)}


# ── web_search 主入口（两级 fallback）─────────────────────────────────────────

async def search_feed_web(state: Any, query: str, limit: int = 10,
                       **_: Any) -> dict:
    q = (query or "").strip()      # 非空 = schema minLength:1，派发口核
    limit = max(1, min(int(limit or 10), 30))

    backend = (os.getenv("WEB_SEARCH_BACKEND") or "auto").strip().lower()

    if backend == "ddg":
        return await _ddg_search(q, limit)
    if backend == "baidu":
        return await _baidu_search(q, limit)

    # auto: 先 DDG，失败/连不通切百度
    r1 = await _ddg_search(q, limit)
    if r1.get("status") == "success":
        return r1
    log.info("DDG failed (%s), falling back to baidu", r1.get("error"))
    r2 = await _baidu_search(q, limit)
    if r2.get("status") == "success":
        r2["ddg_fallback_reason"] = r1.get("error")
        return r2
    return {
        "status": "error",
        "error": "all backends failed",
        "ddg_error": r1.get("error"),
        "baidu_error": r2.get("error"),
    }

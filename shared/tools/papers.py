"""学术文献搜索工具：Semantic Scholar + arXiv。

- Semantic Scholar：如设 `S2_API_KEY` env var，自动 attach `x-api-key` header
  （免费匿名 endpoint 速率极低；带 key 后限额 50–100× 提升）。
- arXiv：模块级 asyncio.Lock + 3 秒最小间隔，符合 arXiv "every 3 seconds at most"
  policy；防止多个 child agent 并发起请求被 ban。
- 通用 429/503 retry：读 `Retry-After` header，无则 exponential backoff w/ jitter，
  最多 3 次。彻底失败后返结构化 error envelope（含 status_class= rate_limited
  / transient / permanent），告诉 agent 这类失败该如何处置（参考 v1.1
  framework system_prompt prefix 的元认知约定）。

进程内查询缓存：同一节点 / 同一 run 周期内，LLM 往往会反复用近似 query
搜同一个主题。缓存 (query, limit, year_from / sort_by) → result，TTL 1 小时，
最多 256 条 entry（LRU 淘汰）。命中时返回的结果会带 `cached: True`，方便
LLM 自检是否在原地打转。
"""
from __future__ import annotations

import asyncio
import os
import random
import time
import xml.etree.ElementTree as ET
from collections import OrderedDict
from typing import Any

import httpx

from core.state import State
from core.tool_registry import ToolDefinition, register_tool


# ── 通用 retry helper ─────────────────────────────────────────────────────────

_RETRY_STATUSES = (429, 503)
_MAX_RETRIES = 3
_DEFAULT_BACKOFF_BASE = 2.0   # 2/4/8s base，jitter ± 25%


def _classify_status(code: int) -> str:
    if code in (429,):
        return "rate_limited"
    if 500 <= code < 600:
        return "transient"
    if 400 <= code < 500:
        return "permanent"
    return "transient"


def _compute_backoff(attempt: int, retry_after_header: str | None) -> float:
    """优先 Retry-After（秒数 or HTTP-date 简化处理为 5s）；否则 exponential + jitter。"""
    if retry_after_header:
        try:
            return max(0.5, float(retry_after_header))
        except (TypeError, ValueError):
            return 5.0
    base = _DEFAULT_BACKOFF_BASE ** attempt        # 2, 4, 8
    jitter = base * 0.25 * (2 * random.random() - 1)
    return max(0.5, base + jitter)


async def _http_get_with_retry(
    url: str,
    *,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 30.0,
    follow_redirects: bool = False,
    max_retries: int = _MAX_RETRIES,
) -> httpx.Response:
    """GET 含 429/503 backoff。彻底失败抛最后一次 HTTPStatusError。

    单次调用最多 ~14 秒等待（2+4+8）；调用方应保证整体 timeout 容纳得下。
    """
    last_exc: Exception | None = None
    async with httpx.AsyncClient(timeout=timeout,
                                  follow_redirects=follow_redirects) as client:
        for attempt in range(max_retries + 1):     # 1 次首发 + max_retries 次重试
            try:
                resp = await client.get(url, params=params, headers=headers)
                if resp.status_code in _RETRY_STATUSES and attempt < max_retries:
                    sleep_s = _compute_backoff(
                        attempt, resp.headers.get("Retry-After"))
                    await asyncio.sleep(sleep_s)
                    continue
                resp.raise_for_status()
                return resp
            except httpx.HTTPStatusError as e:
                last_exc = e
                if e.response.status_code in _RETRY_STATUSES \
                        and attempt < max_retries:
                    sleep_s = _compute_backoff(
                        attempt, e.response.headers.get("Retry-After"))
                    await asyncio.sleep(sleep_s)
                    continue
                raise
            except (httpx.TimeoutException, httpx.RequestError) as e:
                last_exc = e
                if attempt < max_retries:
                    await asyncio.sleep(_compute_backoff(attempt, None))
                    continue
                raise
    # unreachable
    if last_exc:
        raise last_exc
    raise RuntimeError("retry loop ended without response or exception")


# ── 进程内查询缓存（LRU + TTL） ────────────────────────────────────────────

_CACHE_TTL_SEC = 60 * 60   # 1 小时
_CACHE_MAX = 256

# key=(source, normalized_query_tuple) → (expire_at, value, hit_count)
_CACHE: "OrderedDict[tuple, tuple[float, dict, int]]" = OrderedDict()


def _cache_key(source: str, query: str, **params: Any) -> tuple:
    """规范化 query（小写 + 去多余空格）+ 排序的 params 元组。"""
    q_norm = " ".join((query or "").lower().split())
    param_items = tuple(sorted((k, v) for k, v in params.items() if v is not None))
    return (source, q_norm, param_items)


def _cache_get(key: tuple) -> dict | None:
    entry = _CACHE.get(key)
    if entry is None:
        return None
    expire_at, value, hits = entry
    if time.monotonic() > expire_at:
        _CACHE.pop(key, None)
        return None
    # LRU：访问命中 → 移到尾部 + hit_count++
    _CACHE.move_to_end(key)
    _CACHE[key] = (expire_at, value, hits + 1)
    out = dict(value)
    out["cached"] = True
    out["cache_hits_for_this_query"] = hits + 1
    return out


def _cache_put(key: tuple, value: dict) -> None:
    # 只缓存成功结果
    if (value or {}).get("status") != "success":
        return
    if len(_CACHE) >= _CACHE_MAX:
        _CACHE.popitem(last=False)
    _CACHE[key] = (time.monotonic() + _CACHE_TTL_SEC, value, 0)
    _CACHE.move_to_end(key)


def _cache_reset() -> None:
    """供测试用：清缓存。生产代码请勿调用。"""
    _CACHE.clear()


# ── Semantic Scholar ────────────────────────────────────────────────────────
#
# 这里原来有一个 `semantic_scholar_search` 工具（S2 单源检索）+ 它自己的一套
# S2 HTTP 客户端。两处问题：
#
#   1. literature 同时拿到它和 `search_papers`（五源并行、单源挂掉自动降级），
#      于是模型有机会把整条调研绑死在 S2 一家的可用性上 —— node20 实测 8 次
#      检索挂了 3 次，全是 S2 的 429（issue #326）。
#   2. `nodes/literature/tools/search_engines.py::SemanticScholarSearch` 才是
#      多源工具在用的 S2 客户端；这里这份是**第二套独立实现**。
#
# 需要只搜 S2 时：`search_papers(include_s2=True, 其余 include_* 全 False)`，
# `year_from` 也已接到多源工具上，能力一条不少。

# ── arXiv ────────────────────────────────────────────────────────────────────

_ARXIV_URL = "https://export.arxiv.org/api/query"

# arXiv 官方政策：≤ 1 req per 3 s，burst not tolerated。
# 模块级 lock + 上次发起时间，跨多个并发 child agent 共享同一 process budget。
_ARXIV_MIN_INTERVAL_SEC = 3.0
_arxiv_lock = asyncio.Lock()
_arxiv_last_call: float = 0.0


async def _arxiv_throttle() -> None:
    """串行化 arxiv 调用，保证两次发起间隔 ≥ 3 秒。"""
    global _arxiv_last_call
    async with _arxiv_lock:
        elapsed = time.monotonic() - _arxiv_last_call
        if elapsed < _ARXIV_MIN_INTERVAL_SEC:
            await asyncio.sleep(_ARXIV_MIN_INTERVAL_SEC - elapsed)
        _arxiv_last_call = time.monotonic()


async def _arxiv_search(
    state: State,
    query: str,
    max_results: int = 10,
    sort_by: str = "relevance",
    **_: Any,
) -> dict:
    # query 非空 = schema required + minLength:1，派发口核
    cache_key = _cache_key("arxiv", query,
                              max_results=min(int(max_results), 50),
                              sort_by=sort_by)
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    sort_map = {"relevance": "relevance",
                "lastUpdatedDate": "lastUpdatedDate",
                "submittedDate": "submittedDate"}
    params = {
        "search_query": f"all:{query}",
        "start": 0,
        "max_results": min(int(max_results), 50),
        "sortBy": sort_map.get(sort_by, "relevance"),
        "sortOrder": "descending",
    }

    await _arxiv_throttle()        # 3s 最小间隔 + 跨 child 共享
    try:
        resp = await _http_get_with_retry(
            _ARXIV_URL, params=params, timeout=30.0, follow_redirects=True)
    except httpx.HTTPStatusError as e:
        code = e.response.status_code
        cls = _classify_status(code)
        guidance = {
            "rate_limited": (
                "arXiv 持续 rate-limited（已 retry 3 次 + 3s 间隔仍被拦）。"
                "**不要再 retry**；改用 KB 现有 chunk + search_papers，"
                "或 memory_note(text=..., category='pitfall')。"
            ),
            "permanent": "请求本身有问题。检查 query 后换法。",
            "transient": "服务器侧暂时问题。建议优先用 KB。",
        }.get(cls, "")
        return {"status": "error", "status_class": cls,
                "error": f"arXiv HTTP {code}",
                "guidance": guidance}
    except httpx.TimeoutException:
        return {"status": "error", "status_class": "transient",
                "error": "arXiv 超时",
                "guidance": "网络层超时；改用 search_papers 或先用 KB。"}
    except Exception as e:
        return {"status": "error", "status_class": "transient",
                "error": f"{type(e).__name__}: {e}"}

    try:
        root = ET.fromstring(resp.text)
    except ET.ParseError as e:
        return {"status": "error", "error": f"arXiv XML 解析失败：{e}"}

    ns = {"atom": "http://www.w3.org/2005/Atom"}
    papers = []
    for entry in root.findall("atom:entry", ns):
        title = (entry.findtext("atom:title", "", ns) or "").strip().replace("\n", " ")
        abstract = (entry.findtext("atom:summary", "", ns) or "").strip()[:600]
        arxiv_id_full = entry.findtext("atom:id", "", ns) or ""
        arxiv_id = (arxiv_id_full.split("/abs/")[-1]
                    if "/abs/" in arxiv_id_full else arxiv_id_full)
        published = (entry.findtext("atom:published", "", ns) or "")[:10]
        authors = [
            (a.findtext("atom:name", "", ns) or "").strip()
            for a in entry.findall("atom:author", ns)
        ][:5]
        categories = [c.get("term", "") for c in entry.findall("atom:category", ns)]
        papers.append({
            "arxiv_id": arxiv_id,
            "title": title,
            "abstract": abstract,
            "authors": authors,
            "published": published,
            "categories": categories,
        })

    result = {"status": "success", "source": "arxiv",
               "papers": papers, "total": len(papers)}
    _cache_put(cache_key, result)
    return result


register_tool(
    ToolDefinition(
        name="arxiv_search",
        description=(
            "在 arXiv 搜预印本。"
            "最多 50 条，含 arxiv_id / title / abstract / authors / published / categories。"
            "适用于：找最新的（尚未正式发表的）工作、按 cs/physics/q-bio 等分类筛选。"
            "sort_by: relevance（默认）/ lastUpdatedDate / submittedDate。"
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "minLength": 1, "description": "自由文本查询。"},
                "max_results": {"type": "integer", "default": 10, "minimum": 1, "maximum": 50},
                "sort_by": {
                    "type": "string",
                    "enum": ["relevance", "lastUpdatedDate", "submittedDate"],
                    "default": "relevance",
                },
            },
            "required": ["query"],
        },
        risk_level="low",
    ),
    _arxiv_search,
)

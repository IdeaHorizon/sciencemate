"""Optional translation providers for academic search index fields.

The query strategy is intentionally outside this module and still uses the
reasoning model. Only English paper titles and abstracts enter this client.
"""

from __future__ import annotations

import asyncio
import os
import re
import threading
import time

import httpx


MAX_SEGMENT_CHARS = 4500
_WEB_SERVICES = ("bing", "lara", "alibaba")
_WEB_TRANSLATION_LOCKS = {service: threading.Lock() for service in _WEB_SERVICES}


def _valid_translation(source: str, value: object) -> str | None:
    if not isinstance(value, str):
        return None
    translated = value.strip()
    if not re.search(r"[\u4e00-\u9fff]", translated):
        return None
    # A one-line summary of a long abstract must not enter the full-text cache.
    if len(translated) < max(2, int(len(source) * 0.15)):
        return None
    if len(translated) > max(100, len(source) * 3):
        return None
    return translated


def configured_provider() -> str:
    provider = os.getenv("ACADEMIC_INDEX_TRANSLATOR", "llm").strip().lower()
    if provider not in {"llm", "local", "translators"}:
        raise ValueError("ACADEMIC_INDEX_TRANSLATOR must be llm, local or translators")
    return provider


def _segments(text: str) -> list[str]:
    """Keep each request inside the local model's context without losing text."""
    if len(text) <= MAX_SEGMENT_CHARS:
        return [text]
    pieces: list[str] = []
    remaining = text
    while len(remaining) > MAX_SEGMENT_CHARS:
        cut = remaining.rfind(" ", 0, MAX_SEGMENT_CHARS + 1)
        if cut < MAX_SEGMENT_CHARS // 2:
            cut = MAX_SEGMENT_CHARS
        pieces.append(remaining[:cut].strip())
        remaining = remaining[cut:].strip()
    if remaining:
        pieces.append(remaining)
    return pieces


async def _local_parts(parts: list[str]) -> dict[int, str]:
    """Call a local llama.cpp/Ollama OpenAI-compatible server on any OS."""
    endpoint = os.getenv(
        "ACADEMIC_LOCAL_TRANSLATOR_URL", "http://127.0.0.1:28082/v1/chat/completions"
    ).strip()
    model = os.getenv("ACADEMIC_LOCAL_TRANSLATOR_MODEL", "hy-mt2").strip()
    semaphore = asyncio.Semaphore(2)
    result: dict[int, str] = {}

    async with httpx.AsyncClient(timeout=90) as client:
        async def send(index: int, part: str) -> None:
            async with semaphore:
                try:
                    response = await client.post(endpoint, json={
                        "model": model,
                        "messages": [{"role": "user", "content": (
                            "将以下英文学术论文文本忠实翻译为简体中文。只输出译文，"
                            "保留公式、数字、缩写、化学式和引用标记，不要增删结论：\n\n" + part
                        )}],
                        "temperature": 0.0,
                        "max_tokens": min(4096, max(256, len(part) * 2)),
                    })
                    response.raise_for_status()
                    choices = response.json().get("choices") or []
                    value = choices[0]["message"]["content"] if choices else None
                    translated = _valid_translation(part, value)
                    if translated:
                        result[index] = translated
                except (httpx.HTTPError, ValueError, TypeError, KeyError, IndexError, AttributeError):
                    return

        await asyncio.gather(*(send(index, part) for index, part in enumerate(parts)))
    return result


def _web_translate(part: str, service: str) -> str | None:
    """Run the optional synchronous library outside the event loop."""
    # Importing translators otherwise makes a network request to discover the
    # machine's region. Set it explicitly before import, including in workers.
    os.environ["translators_default_region"] = (
        os.getenv("ACADEMIC_TRANSLATORS_REGION")
        or os.getenv("translators_default_region")
        or "CN"
    )
    import translators

    # The installed library declares a per-provider input limit. Inputs above
    # it are rejected before HTTP anyway; do not queue them behind valid work.
    server = getattr(getattr(translators, "translate_text_with_sync", None), "__self__", None)
    method = getattr(server, "translators_dict", {}).get(service)
    provider = getattr(method, "__self__", None)
    input_limit = getattr(provider, "input_limit", None)
    if isinstance(input_limit, (int, float)) and len(part.strip()) > input_limit:
        return None

    # Hold the lock in the thread: cancelling the awaiting coroutine must not
    # release a session while its synchronous HTTP request is still running.
    with _WEB_TRANSLATION_LOCKS[service]:
        return translators.translate_text(
            part, translator=service, from_language="en", to_language="zh",
            timeout=8, if_print_warning=False,
        )


async def _web_parts(parts: list[str], diagnostics: dict | None = None) -> dict[int, str]:
    """Try Bing, then Lara and Alibaba for failure or invalid response.

    Each website retains its own serialized session. Independent parts can
    advance to fallback providers while another part uses the first provider.
    """
    result: dict[int, str] = {}
    pending = asyncio.Semaphore(3)
    source_stats: dict[str, dict[str, int]] = {}

    async def send(index: int, part: str) -> None:
        async with pending:
            for service in _WEB_SERVICES:
                stats = source_stats.setdefault(service, {"attempts": 0, "translated": 0, "failed": 0})
                stats["attempts"] += 1
                try:
                    value = await asyncio.to_thread(_web_translate, part, service)
                    translated = _valid_translation(part, value)
                    if translated:
                        result[index] = translated
                        stats["translated"] += 1
                        return
                    stats["failed"] += 1
                except ModuleNotFoundError as exc:
                    if exc.name == "translators":
                        raise RuntimeError(
                            "Optional translators package is missing; install translators==6.0.4"
                        ) from exc
                    raise
                except Exception:
                    # The next source can still translate this particular text.
                    stats["failed"] += 1
                    continue
    try:
        await asyncio.gather(*(send(index, part) for index, part in enumerate(parts)))
    finally:
        if diagnostics is not None:
            diagnostics["sources"] = source_stats
    return result


async def translate_index_texts(texts: list[str], diagnostics: dict | None = None) -> dict[str, str]:
    """Translate unique strings; return only complete, validated translations."""
    provider = configured_provider()
    if provider == "llm":
        raise RuntimeError("Dedicated translation provider is not configured")
    unique = list(dict.fromkeys(text for text in texts if text.strip()))
    parts: list[str] = []
    indexes: dict[str, list[int]] = {}
    for text in unique:
        indexes[text] = []
        for part in _segments(text):
            indexes[text].append(len(parts))
            parts.append(part)
    started = time.perf_counter()
    try:
        if provider == "local":
            translated = await _local_parts(parts)
        else:
            translated = await (_web_parts(parts) if diagnostics is None
                                else _web_parts(parts, diagnostics=diagnostics))
    finally:
        if diagnostics is not None:
            diagnostics.update({
                "provider": provider, "texts": len(unique), "segments": len(parts),
                "elapsed_seconds": round(time.perf_counter() - started, 3),
            })
    return {
        text: " ".join(translated[index] for index in positions)
        for text, positions in indexes.items()
        if all(index in translated for index in positions)
    }

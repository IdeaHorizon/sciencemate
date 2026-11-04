"""Dedicated index translation must be cached and never call the reasoning model."""

import httpx
import pytest
from unittest.mock import AsyncMock

import platform_runtime as runtime
from nodes.literature.tools import index_translation, local_index


def test_short_summary_cannot_enter_full_abstract_cache():
    source = "A detailed scholarly abstract about a controlled experiment. " * 30
    assert index_translation._valid_translation(source, "本文研究了这个问题。") is None


def test_scientific_jats_scripts_survive_text_cleaning():
    source = "<jats:p>V <jats:sub>OC</jats:sub> = 3.00 V; 9.0 mA cm <jats:sup>−2</jats:sup>.</jats:p>"
    assert runtime._clean_literature_text(source) == "V_OC = 3.00 V; 9.0 mA cm^−2."


@pytest.mark.asyncio
async def test_local_provider_uses_openai_compatible_api(monkeypatch):
    monkeypatch.setenv("ACADEMIC_INDEX_TRANSLATOR", "local")
    monkeypatch.setenv("ACADEMIC_LOCAL_TRANSLATOR_URL", "http://127.0.0.1:28082/v1/chat/completions")
    requests = []

    def handler(request):
        requests.append(request)
        assert request.url.path == "/v1/chat/completions"
        assert b"Literature" in request.content
        return httpx.Response(200, json={"choices": [{"message": {"content": "文献翻译"}}]})

    client_class = httpx.AsyncClient
    monkeypatch.setattr(index_translation.httpx, "AsyncClient", lambda **kwargs: client_class(transport=httpx.MockTransport(handler)))
    result = await index_translation.translate_index_texts(["Literature translation", "Literature translation"])
    assert result == {"Literature translation": "文献翻译"}
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_long_abstract_is_not_cached_when_a_segment_fails(monkeypatch):
    monkeypatch.setenv("ACADEMIC_INDEX_TRANSLATOR", "local")
    text = "Scientific abstract " * 400
    async def partial(parts):
        assert len(parts) > 1
        assert all(len(part) <= index_translation.MAX_SEGMENT_CHARS for part in parts)
        return {0: "科研摘要"}
    monkeypatch.setattr(index_translation, "_local_parts", partial)
    assert await index_translation.translate_index_texts([text]) == {}


@pytest.mark.asyncio
async def test_translators_tries_bing_then_lara_only_for_failure(monkeypatch):
    monkeypatch.setenv("ACADEMIC_INDEX_TRANSLATOR", "translators")
    calls = []

    def translate(part, service):
        calls.append((part, service))
        if part == "A scholarly title" and service == "bing":
            return "学术标题"
        if service == "bing":
            raise RuntimeError("temporary Bing failure")
        return "这是一个关于实验结果的完整摘要。"

    monkeypatch.setattr(index_translation, "_web_translate", translate)
    result = await index_translation.translate_index_texts([
        "A scholarly title", "A detailed abstract about experimental results.",
    ])
    assert result == {
        "A scholarly title": "学术标题",
        "A detailed abstract about experimental results.": "这是一个关于实验结果的完整摘要。",
    }
    assert [service for part, service in calls if part == "A scholarly title"] == ["bing"]
    assert [service for part, service in calls if part != "A scholarly title"] == ["bing", "lara"]


@pytest.mark.asyncio
async def test_translators_discards_invalid_translation_without_partial_cache(monkeypatch):
    monkeypatch.setenv("ACADEMIC_INDEX_TRANSLATOR", "translators")
    text = "A complete scientific abstract about measured outcomes. " * 100
    calls = []

    def translate(part, service):
        calls.append(service)
        if len(part) > 4000:
            return "本文研究了问题。"
        return "这是一个完整的科研摘要译文，讨论了测量结果与研究结论。" * 20

    monkeypatch.setattr(index_translation, "_web_translate", translate)
    assert await index_translation.translate_index_texts([text]) == {}
    assert all(service in calls for service in ("bing", "lara", "alibaba"))


@pytest.mark.asyncio
async def test_translators_uses_alibaba_after_two_invalid_sources(monkeypatch):
    monkeypatch.setenv("ACADEMIC_INDEX_TRANSLATOR", "translators")
    calls = []

    def translate(part, service):
        calls.append(service)
        return "English unchanged" if service != "alibaba" else "中文译文"

    monkeypatch.setattr(index_translation, "_web_translate", translate)
    assert await index_translation.translate_index_texts(["English source"]) == {
        "English source": "中文译文"
    }
    assert calls == ["bing", "lara", "alibaba"]


@pytest.mark.asyncio
async def test_local_provider_reuses_sqlite_and_skips_llm(monkeypatch, tmp_path):
    from core.llm import LLMClient

    index = local_index.LocalPaperIndex(str(tmp_path / "catalog.sqlite3"))
    monkeypatch.setenv("ACADEMIC_INDEX_TRANSLATOR", "local")
    monkeypatch.setattr(local_index, "get_index", lambda: index)
    calls = []

    async def translate(parts):
        calls.append(parts)
        return {index: "粒子沉降" if "Particle" in part else "这是关于粒子沉降的完整摘要。"
                for index, part in enumerate(parts)}

    monkeypatch.setattr(index_translation, "_local_parts", translate)
    llm = AsyncMock(side_effect=AssertionError("index translation must not call the reasoning model"))
    monkeypatch.setattr(LLMClient, "chat", llm)
    payload = {"papers": [{"key": "a", "title": "Particle sedimentation",
                            "abstract": "A complete abstract about particle sedimentation."}],
               "generate_summaries": False, "combine_translation": True}
    first = await runtime.run_literature_translate_page(payload)
    second = await runtime.run_literature_translate_page(payload)
    assert first["translations"][0]["title_zh"] == "粒子沉降"
    assert first["translations"][0]["abstract_zh"] == "这是关于粒子沉降的完整摘要。"
    assert len(calls) == 2
    assert second["diagnostics"]["titles"]["status"] == "cache_hit"
    assert second["diagnostics"]["abstracts"]["status"] == "cache_hit"
    llm.assert_not_called()


@pytest.mark.asyncio
async def test_translators_provider_reuses_index_cache_without_llm(monkeypatch, tmp_path):
    from core.llm import LLMClient

    index = local_index.LocalPaperIndex(str(tmp_path / "catalog.sqlite3"))
    monkeypatch.setenv("ACADEMIC_INDEX_TRANSLATOR", "translators")
    monkeypatch.setattr(local_index, "get_index", lambda: index)
    calls = []

    async def translate(parts, **kwargs):
        calls.extend(parts)
        return {i: "粒子沉降" if "Particle" in part else "这是关于粒子沉降的完整摘要。"
                for i, part in enumerate(parts)}

    monkeypatch.setattr(index_translation, "_web_parts", translate)
    llm = AsyncMock(side_effect=AssertionError("website translation must not call the reasoning model"))
    monkeypatch.setattr(LLMClient, "chat", llm)
    payload = {"papers": [{"key": "a", "title": "Particle sedimentation",
                            "abstract": "A complete abstract about particle sedimentation."}],
               "generate_summaries": False, "combine_translation": True}
    first = await runtime.run_literature_translate_page(payload)
    second = await runtime.run_literature_translate_page(payload)
    assert first["translations"][0]["title_zh"] == "粒子沉降"
    assert first["translations"][0]["abstract_zh"] == "这是关于粒子沉降的完整摘要。"
    assert len(calls) == 2
    assert second["diagnostics"]["titles"]["status"] == "cache_hit"
    assert second["diagnostics"]["abstracts"]["status"] == "cache_hit"
    llm.assert_not_called()


@pytest.mark.asyncio
async def test_web_fallback_can_run_while_another_part_uses_primary(monkeypatch):
    import sys
    import threading
    from types import SimpleNamespace

    fallback_started = threading.Event()
    primary_completed = threading.Event()

    def translate(part, *, translator, **kwargs):
        if part == "First title" and translator == "bing":
            raise RuntimeError("primary temporarily unavailable")
        if translator == "lara":
            fallback_started.set()
            assert primary_completed.wait(2)
        elif part == "Second title":
            assert fallback_started.wait(2)
            primary_completed.set()
        return "完整的中文标题"

    monkeypatch.setitem(sys.modules, "translators", SimpleNamespace(translate_text=translate))
    diagnostics = {}
    result = await index_translation._web_parts(
        ["First title", "Second title"], diagnostics=diagnostics,
    )
    assert result == {0: "完整的中文标题", 1: "完整的中文标题"}
    assert diagnostics["sources"]["bing"]["translated"] == 1
    assert diagnostics["sources"]["lara"]["translated"] == 1


@pytest.mark.asyncio
async def test_one_web_provider_never_shares_its_session_concurrently(monkeypatch):
    import asyncio
    import sys
    import threading
    import time
    from types import SimpleNamespace

    active = peak = 0
    counter_lock = threading.Lock()

    def translate(*args, **kwargs):
        nonlocal active, peak
        with counter_lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.01)
        with counter_lock:
            active -= 1
        return "完整的中文译文"

    monkeypatch.setitem(sys.modules, "translators", SimpleNamespace(translate_text=translate))
    titles, abstracts = await asyncio.gather(
        index_translation._web_parts(["First title", "Second title"]),
        index_translation._web_parts(["First abstract", "Second abstract"]),
    )
    assert len(titles) == len(abstracts) == 2
    assert peak == 1


def test_provider_length_limit_skips_only_inputs_the_library_would_reject(monkeypatch):
    import sys
    from types import SimpleNamespace

    calls = []

    class Provider:
        input_limit = 10

        def translate(self, part, **kwargs):
            calls.append(part)
            return "完整中文译文"

    class Server:
        def __init__(self):
            self.translators_dict = {"bing": Provider().translate}

        def translate_text(self, part, translator="bing", **kwargs):
            return self.translators_dict[translator](part, **kwargs)

    server = Server()
    monkeypatch.setitem(sys.modules, "translators", SimpleNamespace(
        translate_text_with_sync=server.translate_text,
        translate_text=server.translate_text,
    ))
    assert index_translation._web_translate("x" * 11, "bing") is None
    assert calls == []
    assert index_translation._web_translate("x" * 10, "bing") == "完整中文译文"
    assert calls == ["x" * 10]

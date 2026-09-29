"""来源白名单回归（issue #230：CNKI-only 检索返回已禁用的 CrossRef 结果）。

实测事故：`include_crossref=false` + 其余国际源全关（白名单只剩 cnki）时，
search_papers 仍返回 total=20、首条 source=crossref。三条根因路径：

  1. `enabled_sources or {默认全开}` —— 显式传空集合被 `or` 判 falsy → 静默全开，
     "把所有源关掉" 反而等于 "全开"；
  2. **本地 SQLite 索引**（papers.db，跨来源跨课题的历史沉淀）的命中结果直接
     merge 进最终返回，白名单当时只传给了 _search_remote → 这是本 issue 的主因；
  3. 被污染的结果又按 CNKI-only 的 cache_key 落盘 → 只修首次路径，旧缓存照吐脏数据。

本文件把这三条路径 + 兜底防线全部钉住，并全部**离线**跑：
远程用 fake（monkeypatch `_search_remote` / `_safe_search`），CNKI 用 fake 类，
本地索引既测 fake 也测真 sqlite（临时 db），不发一个真实请求。
"""
from __future__ import annotations

import hashlib
import pickle

import pytest

from nodes.literature.tools.search_engines import (
    DEFAULT_ENABLED_SOURCES,
    KNOWN_SOURCES,
    Paper,
    SearchManager,
    SearchResults,
    canonical_source,
    filter_papers_by_sources,
    is_source_allowed,
)


# ─────────────────────────────────────────────────────────────────────────────
# 测试替身
# ─────────────────────────────────────────────────────────────────────────────

def make_paper(source: str, title: str, doi: str = "") -> Paper:
    """造一篇论文。

    venue / abstract **必须给全**：search_all 对「有 DOI 但缺 venue/abstract」的论文
    会去 api.crossref.org 回填 —— 测试要保持离线，就不能留这个坑。
    """
    return Paper(
        title=title, doi=doi, source=source,
        venue="Some Journal", abstract="x" * 100, year=2024,
    )


class FakeIndex:
    """假的本地索引：只关心「它会把历史沉淀的论文吐给 search_all」这件事。"""

    def __init__(self, papers: list[Paper]):
        self._papers = list(papers)
        self.added: list[Paper] = []

    def search(self, query: str, limit: int = 200, min_year=None) -> list[Paper]:
        return list(self._papers)

    def search_by_terms(self, terms, limit: int = 200, min_match: int = 2) -> list[Paper]:
        return []

    def add_papers(self, papers, search_query: str = ""):
        self.added.extend(papers)
        return len(papers), 0

    def cache_search_results(self, query, papers, ttl_days: int = 30):
        pass


class FakeCnki:
    """假 CnkiSearch：ready 与否、返回什么都由测试指定。"""

    def __init__(self, papers=None, ready=True, raise_on_search=False):
        self._papers = papers or []
        self._ready = ready
        self._raise = raise_on_search

    @property
    def is_ready(self) -> bool:
        return self._ready

    async def search(self, keyword: str, max_results: int = 50):
        if self._raise:
            raise RuntimeError("cnki boom")
        return list(self._papers)


def patch_cnki(monkeypatch, fake: FakeCnki):
    """_search_cnki 里 `from .cnki_search import CnkiSearch` 是调用时导入，
    所以打模块属性即可生效。"""
    import nodes.literature.tools.cnki_search as cnki_mod
    monkeypatch.setattr(cnki_mod, "CnkiSearch", lambda *a, **k: fake)


def make_manager(local_papers=None, remote_papers=None):
    """构造一个完全离线的 SearchManager（本地索引替身 + 远程替身）。"""
    mgr = SearchManager()
    mgr.local_index = FakeIndex(local_papers or [])
    calls: list[set] = []

    # 签名必须逐字跟上 `SearchManager._search_remote` —— 替身不是随便一个能被
    # 调用的东西，它站在被测实现的位置上。**故意不写 `**kwargs`**：吞掉任意新
    # 参数的替身会让下一次契约漂移悄无声息地通过，而这次 CI 红正是因为它没吞
    # （生产侧 `_search_remote(..., date_from=date_from)` 是无条件传的）。
    # 这里只接住 date_from、不断言它 —— 本文件的被测面是来源白名单，日期窗口
    # 的判据该由加这个参数的人在 nodes/literature 侧自己写。
    async def _fake_remote(query, max_per_source, s2_api_key, enabled_sources=None,
                           date_from=None):
        calls.append(set(enabled_sources or []))
        return list(remote_papers or []), {
            "attempted": set(enabled_sources or []) - {"cnki"},
            "unavailable": set(),
            "warnings": [],
        }

    mgr._search_remote = _fake_remote  # type: ignore[method-assign]
    mgr.remote_calls = calls  # type: ignore[attr-defined]
    return mgr


@pytest.mark.asyncio
async def test_the_remote_double_still_stands_where_the_real_method_stands():
    """替身的签名必须能接住真方法的每一个参数 —— 否则这一整片测的是别的东西。

    2026-08-28 实测：`_search_remote` 多了一个 `date_from`（生产侧无条件按关键字
    传），本文件 13 条应声转红，而现场是一片 `status` / `total` 断言失败 ——
    真因（替身把 TypeError 抛在断言之前）要人自己从堆栈里读出来。

    判据从**真方法的签名**推，不是列一张参数名单：以后再多什么参数，这里先红，
    且消息直接点名是哪个参数漂了。修法永远是把替身补齐，不是给替身加
    `**kwargs` —— 那等于把这道闸永久关掉。
    """
    import inspect

    real = inspect.signature(SearchManager._search_remote)
    fake = inspect.signature(make_manager()._search_remote)
    # real 的第一个参数是 self，替身是普通函数没有它。
    expected = [n for n in list(real.parameters)[1:]]
    missing = [n for n in expected if n not in fake.parameters]
    assert not missing, (
        f"tests 里的 _search_remote 替身接不住真方法的参数：{missing}\n"
        f"  真方法：{expected}\n"
        f"  替身  ：{list(fake.parameters)}\n"
        "把缺的参数补进替身（带默认值）。别加 **kwargs —— 那会让下一次漂移无声通过。"
    )


def cache_key_for(query: str, max_per_source: int, lang_mode: str, sources: set[str]) -> str:
    """复刻 search_all 的 cache_key 规则（同时把这份契约钉在测试里）。"""
    sources_key = ",".join(sorted(sources))
    return hashlib.md5(
        f"{query.lower().strip()}:{max_per_source}:{lang_mode}:{sources_key}".encode()
    ).hexdigest()


def warning_codes(results) -> set[str]:
    return {w.get("code") for w in results.warnings}


# ─────────────────────────────────────────────────────────────────────────────
# 纯函数：来源归一 + 过滤
# ─────────────────────────────────────────────────────────────────────────────

def test_canonical_source_strips_cnki_subdb():
    # CNKI 写进 Paper.source 的是 `cnki/学术期刊` 这种带子库后缀的值：
    # 不归一就会把所有 CNKI 结果误杀 —— 白名单反而变成 "CNKI-only 返回零结果"
    assert canonical_source("cnki/学术期刊") == "cnki"
    assert canonical_source("CNKI/博士论文") == "cnki"
    assert canonical_source("crossref") == "crossref"
    assert canonical_source("") == ""
    assert canonical_source(None) == ""

    assert is_source_allowed("cnki/学术期刊", {"cnki"}) is True
    assert is_source_allowed("crossref", {"cnki"}) is False


def test_unknown_provenance_is_rejected_fail_closed():
    # 本地索引里可能有历史遗留的无 source 记录：无法证明来自允许的源就不算允许
    kept, dropped = filter_papers_by_sources(
        [make_paper("", "no source"), make_paper("cnki/学术期刊", "中文文献")],
        {"cnki"},
    )
    assert [p.title for p in kept] == ["中文文献"]
    assert dropped == {"<empty>": 1}


def test_search_results_is_still_a_list():
    # 向后兼容：现有调用方（app.py / search_papers.py）拿它当 list 用
    r = SearchResults([make_paper("arxiv", "a")], requested_sources=["arxiv"])
    assert isinstance(r, list) and len(r) == 1
    assert r[0].title == "a"
    assert r.returned_sources == ["arxiv"]
    assert r.audit_dict()["requested_sources"] == ["arxiv"]


# ─────────────────────────────────────────────────────────────────────────────
# 验收 1：本地索引含 crossref + 白名单={cnki} → 结果里不能有 crossref
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_local_index_crossref_excluded_when_only_cnki_enabled():
    mgr = make_manager(local_papers=[
        make_paper("crossref", "Crossref paper", doi="10.1000/aaa"),
        make_paper("arxiv", "Arxiv paper", doi="10.1000/bbb"),
    ])
    results = await mgr.search_all("machine learning potential", enabled_sources={"cnki"})

    assert list(results) == [], "CNKI-only 检索不得返回本地索引里的 crossref/arxiv 沉淀"
    assert results.returned_sources == []
    assert results.requested_sources == ["cnki"]
    # 白名单外的记录被丢弃这件事要可见（不是静默吞掉）
    assert "disabled_source_filtered" in warning_codes(results)


@pytest.mark.asyncio
async def test_local_index_crossref_excluded_but_cnki_kept():
    """混合场景：本地有 crossref，CNKI 真有结果 → 只留 CNKI。"""
    cnki_paper = make_paper("cnki/学术期刊", "机器学习势函数综述")
    mgr = make_manager(local_papers=[make_paper("crossref", "Crossref paper", doi="10.1000/aaa")])
    with pytest.MonkeyPatch.context() as mp:
        patch_cnki(mp, FakeCnki([cnki_paper]))
        results = await mgr.search_all("机器学习势函数", enabled_sources={"cnki"})

    assert [p.title for p in results] == ["机器学习势函数综述"]
    assert results.returned_sources == ["cnki"]
    assert "cnki" in results.attempted_sources


@pytest.mark.asyncio
async def test_real_local_index_crossref_excluded(tmp_path):
    """用**真 sqlite 索引**复现主因：同一 query 之前用全源搜过，crossref 记录
    躺在 papers.db 里，随后的 CNKI-only 调用把它捞出来当结果返回。"""
    from nodes.literature.tools.local_index import LocalPaperIndex

    query = "machine learning interatomic potential"
    index = LocalPaperIndex(db_path=str(tmp_path / "papers.db"))
    index.add_papers([
        make_paper("crossref", "Crossref MLIP review", doi="10.1000/ccc"),
        make_paper("openalex", "OpenAlex MLIP benchmark", doi="10.1000/ddd"),
        make_paper("cnki/学术期刊", "machine learning 中文综述"),
    ], query)
    assert len(index.search(query, limit=50)) == 3, "前置条件：索引里三条都能被搜到"

    mgr = make_manager()
    mgr.local_index = index
    results = await mgr.search_all(query, enabled_sources={"cnki"})

    assert results.returned_sources in ([], ["cnki"])
    assert all(canonical_source(p.source) == "cnki" for p in results)
    assert "crossref" not in [canonical_source(p.source) for p in results]


# ─────────────────────────────────────────────────────────────────────────────
# 验收 2：空集合不得回退成默认全开
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_empty_whitelist_returns_zero_and_never_reenables_defaults():
    mgr = make_manager(
        local_papers=[make_paper("crossref", "Crossref paper", doi="10.1000/aaa")],
        remote_papers=[make_paper("arxiv", "Arxiv paper", doi="10.1000/bbb")],
    )
    results = await mgr.search_all("anything", enabled_sources=set())

    assert list(results) == []
    assert results.requested_sources == []
    assert warning_codes(results) == {"empty_source_whitelist"}
    assert mgr.remote_calls == [], "空白名单不该发起任何远程检索"


@pytest.mark.asyncio
async def test_none_whitelist_means_all_sources():
    """None 才是「用默认全部来源」—— 与空集合区分开。"""
    mgr = make_manager(remote_papers=[make_paper("arxiv", "Arxiv paper", doi="10.1000/bbb")])
    results = await mgr.search_all("anything", enabled_sources=None)

    assert [p.title for p in results] == ["Arxiv paper"]
    assert mgr.remote_calls == [set(DEFAULT_ENABLED_SOURCES)]
    assert set(results.requested_sources) == set(KNOWN_SOURCES)


@pytest.mark.asyncio
async def test_unknown_source_name_is_reported_not_silently_ignored():
    mgr = make_manager(remote_papers=[make_paper("arxiv", "Arxiv paper", doi="10.1000/bbb")])
    results = await mgr.search_all("anything", enabled_sources={"arxiv", "scoopus"})

    assert mgr.remote_calls == [{"arxiv"}]
    assert "unknown_source_requested" in warning_codes(results)


# ─────────────────────────────────────────────────────────────────────────────
# 验收 3：混合白名单只返允许源（含远程侧）
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_mixed_whitelist_only_queries_and_returns_allowed_sources(monkeypatch):
    """不打 _search_remote，改打 _safe_search：验证远程侧真的只给白名单内的源建任务。"""
    log_name_to_source = {
        "arXiv": "arxiv", "S2": "semantic_scholar", "CrossRef": "crossref",
        "OpenAlex": "openalex", "GoogleScholar": "google_scholar",
    }
    queried: list[str] = []

    async def fake_safe_search(self, engine, query, limit, name, timeout_sec=0):
        queried.append(name)
        src = log_name_to_source[name]
        return [make_paper(src, f"{src} paper", doi=f"10.1000/{src}")]

    monkeypatch.setattr(SearchManager, "_safe_search", fake_safe_search)

    mgr = SearchManager()
    mgr.local_index = FakeIndex([make_paper("crossref", "local crossref", doi="10.1000/local")])
    results = await mgr.search_all("some query", enabled_sources={"arxiv", "openalex"})

    assert sorted(queried) == ["OpenAlex", "arXiv"]
    assert results.returned_sources == ["arxiv", "openalex"]
    assert set(results.attempted_sources) == {"arxiv", "openalex"}


@pytest.mark.asyncio
async def test_engine_returning_wrong_source_is_filtered(monkeypatch):
    """变异测试：某个引擎（笔误/复制粘贴）吐出 crossref 的记录 → 出口必须拦住。
    这条钉的是兜底防线本身，而不是某条已知路径。"""
    async def fake_safe_search(self, engine, query, limit, name, timeout_sec=0):
        return [make_paper("crossref", "sneaky crossref", doi="10.1000/sneaky")]

    monkeypatch.setattr(SearchManager, "_safe_search", fake_safe_search)

    mgr = SearchManager()
    mgr.local_index = None
    results = await mgr.search_all("some query", enabled_sources={"arxiv"})

    assert list(results) == []
    assert results.returned_sources == []


@pytest.mark.asyncio
async def test_final_guard_catches_unfiltered_new_path():
    """模拟「以后新增取数路径忘了过滤」：让 _search_remote 直接吐禁用源。
    出口兜底要把它拦下并留下 machine-readable 记录（fail-closed + fail-loud）。"""
    mgr = make_manager(remote_papers=[make_paper("crossref", "leaked", doi="10.1000/leak")])
    results = await mgr.search_all("some query", enabled_sources={"arxiv"})

    assert list(results) == []
    assert "disabled_source_filtered" in warning_codes(results)


# ─────────────────────────────────────────────────────────────────────────────
# 验收 4：旧脏缓存不能继续吐
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_stale_polluted_cache_is_invalidated_and_researched():
    query = "machine learning potential"
    mgr = make_manager(remote_papers=[])
    key = cache_key_for(query, 50, "auto", {"cnki"})
    path = mgr._cache_path(key)
    with open(path, "wb") as f:
        # 白名单修好之前落盘的脏缓存：CNKI-only 的 key 下存着 crossref
        pickle.dump([make_paper("crossref", "cached crossref", doi="10.1000/aaa")], f)

    with pytest.MonkeyPatch.context() as mp:
        patch_cnki(mp, FakeCnki([make_paper("cnki/学术期刊", "重搜到的中文文献")]))
        results = await mgr.search_all(query, enabled_sources={"cnki"})

    # query 是英文 → CNKI 不搜 → 干净结果就是零篇（关键是不能是缓存里的 crossref）
    assert results.returned_sources == [], "脏缓存作废后不得再吐 crossref"
    assert list(results) == []
    # 脏文件必须被替换掉：不能留在盘上等下次再命中
    if path.exists():
        with open(path, "rb") as f:
            assert [canonical_source(p.source) for p in pickle.load(f)] == []

    # 第二次调用（缓存已干净/已删）同样不得出现 crossref
    with pytest.MonkeyPatch.context() as mp:
        patch_cnki(mp, FakeCnki([]))
        again = await mgr.search_all(query, enabled_sources={"cnki"})
    assert again.returned_sources == []


@pytest.mark.asyncio
async def test_clean_cache_still_hits():
    query = "机器学习势函数"
    mgr = make_manager()
    key = cache_key_for(query, 50, "auto", {"cnki"})
    with open(mgr._cache_path(key), "wb") as f:
        pickle.dump([make_paper("cnki/学术期刊", "缓存的中文文献")], f)

    results = await mgr.search_all(query, enabled_sources={"cnki"})
    assert [p.title for p in results] == ["缓存的中文文献"]
    assert results.from_cache is True


# ─────────────────────────────────────────────────────────────────────────────
# 验收 5：CNKI 不可用 → 零结果 + 机器可读 warning，绝不用禁用源顶替
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cnki_not_configured_returns_zero_with_machine_readable_warning():
    mgr = make_manager(local_papers=[
        make_paper("crossref", "Crossref paper", doi="10.1000/aaa"),  # 顶替的诱惑
    ])
    with pytest.MonkeyPatch.context() as mp:
        patch_cnki(mp, FakeCnki(ready=False))
        results = await mgr.search_all("机器学习势函数", enabled_sources={"cnki"})

    assert list(results) == []
    assert results.unavailable_sources == ["cnki"]
    unavailable = [w for w in results.warnings if w["code"] == "source_unavailable"]
    assert unavailable and unavailable[0]["sources"] == ["cnki"]
    assert unavailable[0]["reason"] == "not_configured"


@pytest.mark.asyncio
async def test_cnki_error_reported_as_unavailable():
    mgr = make_manager()
    with pytest.MonkeyPatch.context() as mp:
        patch_cnki(mp, FakeCnki(raise_on_search=True))
        results = await mgr.search_all("机器学习势函数", enabled_sources={"cnki"})

    assert list(results) == []
    assert results.unavailable_sources == ["cnki"]
    assert [w["reason"] for w in results.warnings if w["code"] == "source_unavailable"] == ["error"]


@pytest.mark.asyncio
async def test_cnki_lang_mode_cn_unavailable_and_whitelist_excluded():
    # lang_mode='cn' 且 cnki 不在白名单 → 明确「没有可搜的源」，不是悄悄搜英文源
    mgr = make_manager(local_papers=[make_paper("crossref", "Crossref paper", doi="10.1000/aaa")])
    results = await mgr.search_all("机器学习", lang_mode="cn", enabled_sources={"crossref"})
    assert list(results) == []
    assert "no_source_for_lang_mode" in warning_codes(results)
    assert mgr.remote_calls == []

    # lang_mode='cn' + CNKI 没配 cookie → 零结果 + warning
    mgr2 = make_manager(local_papers=[make_paper("crossref", "Crossref paper", doi="10.1000/aaa")])
    with pytest.MonkeyPatch.context() as mp:
        patch_cnki(mp, FakeCnki(ready=False))
        results2 = await mgr2.search_all("机器学习", lang_mode="cn", enabled_sources={"cnki"})
    assert list(results2) == []
    assert results2.unavailable_sources == ["cnki"]


@pytest.mark.asyncio
async def test_english_query_with_cnki_only_says_why_it_is_empty():
    """CNKI-only + 英文 query（正是 issue #230 的实测调用形态）：
    结果必须是零篇，且说清是「query 不含中文所以没搜 CNKI」，而不是静默 0 篇 ——
    更不是拿 crossref 顶上。"""
    mgr = make_manager(local_papers=[make_paper("crossref", "Crossref paper", doi="10.1000/aaa")])
    results = await mgr.search_all("machine learning potential", enabled_sources={"cnki"})

    assert list(results) == []
    assert results.unavailable_sources == ["cnki"]
    assert "source_not_attempted" in warning_codes(results)


# ─────────────────────────────────────────────────────────────────────────────
# 工具层（LLM 看到的返回）
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_tool_rejects_all_sources_disabled():
    from nodes.literature.tools.search_papers import _search_papers

    out = await _search_papers(
        None, query="anything",
        include_arxiv=False, include_s2=False, include_openalex=False,
        include_crossref=False, include_cnki=False,
    )
    assert out["status"] == "error"
    assert "至少" in out["error"]


@pytest.mark.asyncio
async def test_tool_reports_source_audit(monkeypatch):
    """issue #230 的实测复现：include_crossref=false + 其它国际源 false + cnki 默认 true，
    本地索引里有 crossref 沉淀 → 过去返回 total=20/首条 crossref。"""
    import nodes.literature.tools.search_papers as sp

    def factory(*a, **k):
        return make_manager(local_papers=[
            make_paper("crossref", "Crossref paper", doi="10.1000/aaa"),
            make_paper("arxiv", "Arxiv paper", doi="10.1000/bbb"),
        ])

    monkeypatch.setattr(sp, "SearchManager", factory)

    out = await sp._search_papers(
        None, query="machine learning potential",
        include_arxiv=False, include_s2=False, include_openalex=False,
        include_crossref=False,  # 显式禁用
    )
    assert out["status"] == "success"
    assert out["total"] == 0
    assert out["papers"] == []
    assert out["requested_sources"] == ["cnki"]
    assert out["returned_sources"] == []
    assert out["unavailable_sources"] == ["cnki"]
    assert any(w["code"] == "source_not_attempted" for w in out["warnings"])


@pytest.mark.asyncio
async def test_tool_keeps_allowed_sources_and_reports_them(monkeypatch):
    import nodes.literature.tools.search_papers as sp

    def factory(*a, **k):
        return make_manager(
            local_papers=[make_paper("crossref", "Crossref paper", doi="10.1000/aaa")],
            remote_papers=[make_paper("arxiv", "Arxiv paper", doi="10.1000/bbb")],
        )

    monkeypatch.setattr(sp, "SearchManager", factory)

    out = await sp._search_papers(
        None, query="machine learning potential",
        include_s2=False, include_openalex=False, include_crossref=False, include_cnki=False,
    )
    assert out["total"] == 1
    assert out["papers"][0]["source"] == "arxiv"
    assert out["returned_sources"] == ["arxiv"]
    assert out["requested_sources"] == ["arxiv"]


# ── #230 复核残留（qinp 2026-07-30）：禁用源不得被元数据回填偷偷请求 ────────
#
# 原行为：venue/abstract 回填无条件直连 api.crossref.org，**crossref 被显式禁用
# 时也照发**。它只给已允许的论文补元数据、不引入新论文，所以不是结果泄漏 ——
# 但审计的 attempted_sources 不记这些真实发生的请求，"禁了 crossref 却在打
# crossref"，审计与事实不符就失去了意义。
# （上面 make_paper 的 docstring 正是旁证：这批测试当初靠"把 venue/abstract 填满"
#   才绕开回填保持离线 —— 说明那条网络路径一直是敞开的。）


def _paper_missing_metadata(source: str = "arxiv") -> Paper:
    """有 DOI 但缺 venue/abstract —— 正好会触发回填的那种。"""
    return Paper(title="需要回填的论文", doi="10.1000/example", source=source,
                 venue=None, abstract=None, year=2024)


@pytest.mark.asyncio
async def test_crossref_enrichment_skipped_when_disabled(monkeypatch):
    """crossref 不在白名单 → 回填一个请求都不发，并留机器可读 warning。"""
    import nodes.literature.tools.search_engines as se

    class _BoomClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, *a, **kw):
            raise AssertionError(f"crossref 已禁用，不该发起请求：{url}")

    monkeypatch.setattr(se.httpx, "AsyncClient", _BoomClient)
    mgr = make_manager(remote_papers=[_paper_missing_metadata("arxiv")])
    monkeypatch.setattr(mgr, "_load_cached_results", lambda *a, **kw: None)
    monkeypatch.setattr(mgr, "_save_cached_results", lambda *a, **kw: None)

    res = await mgr.search_all("q", enabled_sources={"arxiv"})

    audit = res.audit_dict()
    assert "crossref" not in audit["attempted_sources"]      # 审计不撒谎
    assert "enrichment_skipped" in warning_codes(res)        # 如实告知代价
    assert [p.source for p in res] == ["arxiv"]              # 结果不受影响

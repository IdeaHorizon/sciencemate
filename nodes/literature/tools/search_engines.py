"""多源学术搜索引擎 — 四源并行 + LLM 翻译 + venue: 语法"""

import asyncio
import re
import html
from html.parser import HTMLParser
import xml.etree.ElementTree as ET
import json
import os
import time
import hashlib
import pickle
import sqlite3
from datetime import datetime, UTC
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse
from typing import Callable, Optional
from dataclasses import dataclass, field, asdict
import httpx

from .config import SEARCH_ENGINES, DEFAULT_RESULTS_PER_SOURCE, PER_SOURCE_LIMITS


@dataclass
class Paper:
    """论文数据结构"""
    title: str = ""
    authors: list = field(default_factory=list)
    year: Optional[int] = None
    venue: str = ""
    doi: str = ""
    source: str = ""
    citations: int = 0
    score: float = 0.0
    metadata_provenance: dict = field(default_factory=dict)
    url: str = ""
    abstract: str = ""
    keywords: list = field(default_factory=list)
    fields_of_study: list = field(default_factory=list)
    pub_type: str = "journal-article"
    pub_date: str = ""
    pdf_url: str = ""
    is_oa: bool = False
    oa_url: str = ""
    arxiv_categories: list = field(default_factory=list)
    subjects: list = field(default_factory=list)
    issn: str = ""
    eissn: str = ""
    nlm_id: str = ""
    journal_abbr: str = ""
    impact_factor: Optional[float] = None

    def to_dict(self):
        return asdict(self)


# ========================================================================
# 来源白名单（issue #230）
# ========================================================================
# 实测事故：`include_crossref=false` + 其余国际源全关（白名单只剩 cnki）时，
# search_all 仍返回 total=20、首条 source=crossref。三处根因：
#   1. `enabled_sources or {默认全开}` —— 调用方显式传空集合时被 `or` 判为 falsy，
#      静默把 6 个源全部重新打开（"关掉所有源" 反而等于 "全开"）；
#   2. 本地 SQLite 索引（历史检索沉淀，里面存着 crossref/arxiv 记录）的命中结果
#      直接 _merge_results 进最终返回 —— 白名单当时只传给了 _search_remote；
#   3. 被污染的结果又按 CNKI-only 的 cache_key 落盘，只修首次路径的话，
#      旧缓存下次照样吐 crossref。
#
# 所以来源判定收敛到这里一处，并在**最终返回前再兜一次**（fail-closed）：
# 以后新增任何取数路径（新引擎 / 新本地库 / 新缓存层），即使忘了过滤，
# 也不会把禁用源漏出去，且会打印 loud warning 指出漏源路径存在。
KNOWN_SOURCES = frozenset({
    "arxiv", "biorxiv", "medrxiv", "semantic_scholar", "pubmed", "crossref", "openalex", "google_scholar", "cnki",
})

# 白名单缺省值（**仅** enabled_sources is None 时使用；空集合 ≠ 缺省）
DEFAULT_ENABLED_SOURCES = KNOWN_SOURCES


def _as_list(value):
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _openaire_abstract(payload: dict, expected_doi: str) -> str:
    """读取与 DOI 精确对应的 OpenAIRE description，拒绝相邻搜索结果。"""
    results = (((payload or {}).get("response") or {}).get("results") or {}).get("result")
    for row in _as_list(results):
        try:
            result = row["metadata"]["oaf:entity"]["oaf:result"]
        except (KeyError, TypeError):
            continue
        pids = _as_list(result.get("pid"))
        doi_values = {
            str(pid.get("$") or "").strip().lower()
            for pid in pids if isinstance(pid, dict)
            if str(pid.get("@classid") or "").lower() == "doi"
        }
        if expected_doi.strip().lower() not in doi_values:
            continue
        descriptions = []
        for item in _as_list(result.get("description")):
            value = item.get("$") if isinstance(item, dict) else item
            text = " ".join(html.unescape(str(value or "")).split())
            text = re.sub(r"^abstract\s*[:.\-–—]*\s*", "", text, flags=re.I)
            if 100 <= len(text) <= 20_000:
                descriptions.append(text)
        return max(descriptions, key=len, default="")
    return ""


class _SearchResultLinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        values = {str(key).lower(): str(value or "") for key, value in attrs}
        if "result__a" in values.get("class", "") and values.get("href"):
            self.links.append(values["href"])


def _public_pdf_candidates(search_html: str) -> list[str]:
    parser = _SearchResultLinkParser()
    try:
        parser.feed(search_html or "")
    except Exception:
        return []
    candidates = []
    blocked = ("sci-hub", "researchgate.net", "academia.edu")
    for raw in parser.links:
        url = raw
        if raw.startswith("//"):
            url = "https:" + raw
        parsed = urlparse(url)
        if parsed.hostname and parsed.hostname.endswith("duckduckgo.com"):
            url = unquote((parse_qs(parsed.query).get("uddg") or [""])[0])
            parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if parsed.scheme != "https" or not host or any(x in host for x in blocked):
            continue
        if not parsed.path.lower().endswith(".pdf"):
            continue
        if url not in candidates:
            candidates.append(url)
    return candidates[:2]


def _direct_pdf_candidates(page_html: str) -> list[str]:
    """从 DOI/出版社中转页提取明确写出的公开 PDF URL。"""
    candidates: list[str] = []
    blocked = ("sci-hub", "researchgate.net", "academia.edu")
    for raw in re.findall(
        r"""https?://[^\s<>"']+?\.pdf(?:[?#][^\s<>"']*)?""",
        html.unescape(page_html or ""),
        flags=re.I,
    ):
        url = raw.rstrip(".,;:)")
        host = (urlparse(url).hostname or "").lower()
        if host and not any(value in host for value in blocked) and url not in candidates:
            candidates.append(url)
    return candidates[:2]


def _pdf_original_abstract(pdf_bytes: bytes, *, doi: str, title: str) -> str:
    """从已验证为同一论文的前两页提取作者原始 Abstract。"""
    if not pdf_bytes.startswith(b"%PDF-") or len(pdf_bytes) > 20 * 1024 * 1024:
        return ""
    try:
        import pymupdf
        document = pymupdf.open(stream=pdf_bytes, filetype="pdf")
        text = "\n".join(
            document[index].get_text()
            for index in range(min(2, document.page_count))
        )
        document.close()
    except Exception:
        return ""
    compact = " ".join(text.split()).lower()
    doi_match = doi.lower() in compact
    title_terms = [
        term for term in re.findall(r"[a-z0-9]+", title.lower())
        if len(term) >= 4 and term not in {"with", "from", "under", "using", "versus", "their"}
    ]
    title_coverage = (
        sum(term in compact for term in set(title_terms)) / len(set(title_terms))
        if title_terms else 0.0
    )
    if not doi_match and title_coverage < 0.7:
        return ""
    match = re.search(
        r"(?is)\babstract\s*[.:\-–—]*\s*(.+?)"
        r"(?=\s+(?:key\s*words?|keywords?|for\s+citation|introduction|1\.?\s+introduction)\b)",
        text,
    )
    if not match:
        return ""
    abstract = " ".join(match.group(1).split())
    return abstract if 100 <= len(abstract) <= 8_000 else ""


async def _discover_pdf_abstract(client: httpx.AsyncClient, paper: Paper) -> tuple[str, str]:
    """最后兜底：精确题名发现公开PDF，核对DOI/题名后只读前两页。"""
    title = str(paper.title or "").strip()
    if not title:
        return "", ""
    known = [str(paper.pdf_url or ""), str(paper.oa_url or "")]
    urls = [url for url in known if url]
    if not urls:
        # DOI 落地页有时是中转页，正文会明确列出出版社公开 PDF。
        # 先走 DOI，既快又可核验；没有链接才用通用网页搜索兜底。
        if paper.doi:
            try:
                landing = await client.get(
                    f"https://doi.org/{paper.doi}",
                    headers={"User-Agent": "Mozilla/5.0"},
                    timeout=5,
                    follow_redirects=True,
                )
                if landing.status_code == 200:
                    urls = _direct_pdf_candidates(landing.text)
            except Exception:
                pass
        if not urls:
            response = await client.get(
                "https://html.duckduckgo.com/html/",
                params={"q": f'"{title}" filetype:pdf'},
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=5,
                follow_redirects=True,
            )
            if response.status_code != 200:
                return "", ""
            urls = _public_pdf_candidates(response.text)
    for url in list(dict.fromkeys(urls))[:2]:
        try:
            pdf = await client.get(
                url, headers={"User-Agent": "Mozilla/5.0"},
                timeout=10, follow_redirects=True,
            )
            if pdf.status_code != 200:
                continue
            abstract = _pdf_original_abstract(pdf.content, doi=paper.doi, title=title)
            if abstract:
                return abstract, str(pdf.url)
        except Exception:
            continue
    return "", ""


class _CitationAbstractParser(HTMLParser):
    """只读取出版商页面明确声明的摘要元标签，拒绝普通页面描述。"""

    _NAMES = {
        "citation_abstract", "dc.description", "dcterms.abstract",
        "prism.abstract", "eprints.abstract",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.candidates: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "meta":
            return
        values = {str(key).lower(): str(value or "") for key, value in attrs}
        name = (values.get("name") or values.get("property") or "").lower().strip()
        content = " ".join(values.get("content", "").split())
        if name in self._NAMES and len(content) >= 100:
            self.candidates.append(content)


def _publisher_landing_abstract(page_html: str) -> str:
    parser = _CitationAbstractParser()
    try:
        parser.feed(page_html or "")
    except Exception:
        return ""
    return max(parser.candidates, key=len, default="")


def canonical_source(raw: str) -> str:
    """把 Paper.source 归一成白名单里的 canonical 源 id。

    为啥需要归一：CNKI 写进 Paper.source 的是 `cnki/学术期刊` 这种**带子库后缀**的值
    （cnki_search.py: `source=f"cnki/{db_label}"`），直接拿 `p.source in enabled_sources`
    判断会把所有 CNKI 结果误杀 —— 白名单反而变成"CNKI-only 返回零结果"。
    统一取 '/' 前一段并小写，其余源本来就是 canonical 名。
    """
    s = (raw or "").strip().lower()
    if not s:
        return ""
    return s.split("/", 1)[0].strip()


def is_source_allowed(paper_source: str, enabled_sources) -> bool:
    """单篇论文的来源是否在白名单内。

    来源为空 / 无法归一的记录一律**拒绝**（fail-closed）：本地索引里可能有历史
    遗留的无 source 记录，无法证明它来自允许的源，就不能算允许。
    """
    canon = canonical_source(paper_source)
    if not canon:
        return False
    return canon in enabled_sources


def filter_papers_by_sources(papers: list, enabled_sources) -> tuple[list, dict]:
    """按白名单过滤论文，返回 (保留列表, {被拒来源: 条数})。

    第二个返回值用于审计/告警：非空说明某条取数路径没做过滤（issue #230 的形态）。
    """
    kept = []
    dropped: dict[str, int] = {}
    for p in papers:
        if is_source_allowed(getattr(p, "source", ""), enabled_sources):
            kept.append(p)
        else:
            label = canonical_source(getattr(p, "source", "")) or "<empty>"
            dropped[label] = dropped.get(label, 0) + 1
    return kept, dropped


class SearchResults(list):
    """search_all 的返回值：**本体仍是 list[Paper]**，额外挂来源审计信息。

    为啥用 list 子类而不是换成 dict/dataclass：现有调用方（nodes/literature/app.py
    的 `papers = loop.run_until_complete(mgr.search_all(...))`、search_papers.py 的
    `for p in papers` / `papers.sort()` / `papers[:n]`）都把返回值当列表用，换返回
    类型会全线 break。list 子类让 len()/迭代/排序/切片全部照旧，想审计的调用方
    多读几个属性即可。

    注意：切片（`papers[:20]`）返回的是**普通 list**，审计信息会丢 —— 需要审计的
    调用方要在切片前先取 `audit_dict()`。
    """

    def __init__(
        self,
        papers=(),
        *,
        requested_sources=None,
        attempted_sources=None,
        unavailable_sources=None,
        warnings=None,
        from_cache: bool = False,
        raw_source_counts=None,
        deduped_source_counts=None,
        source_diagnostics=None,
    ):
        super().__init__(papers)
        self.requested_sources = sorted(requested_sources or [])
        self.attempted_sources = sorted(attempted_sources or [])
        self.unavailable_sources = sorted(unavailable_sources or [])
        self.warnings = list(warnings or [])
        self.from_cache = from_cache
        self.raw_source_counts = dict(raw_source_counts or {})
        self.deduped_source_counts = dict(deduped_source_counts or {})
        self.source_diagnostics = dict(source_diagnostics or {})

    @property
    def returned_sources(self) -> list[str]:
        """实际返回论文的来源集合 —— 从列表内容现算，不可能与真实结果对不上。"""
        return sorted({canonical_source(getattr(p, "source", "")) or "<empty>" for p in self})

    def audit_dict(self) -> dict:
        return {
            "requested_sources": list(self.requested_sources),
            "attempted_sources": list(self.attempted_sources),
            "unavailable_sources": list(self.unavailable_sources),
            "returned_sources": self.returned_sources,
            "warnings": list(self.warnings),
            "from_cache": self.from_cache,
            "raw_source_counts": dict(self.raw_source_counts),
            "deduped_source_counts": dict(self.deduped_source_counts),
            "source_diagnostics": dict(self.source_diagnostics),
        }


# ---------- arXiv ----------
class ArxivSearch:
    def __init__(self, client: httpx.AsyncClient):
        self.client = client
        self.cfg = SEARCH_ENGINES["arxiv"]

    async def search(self, query: str, max_results: int = DEFAULT_RESULTS_PER_SOURCE, date_from: str | None = None) -> list[Paper]:
        if not self.cfg["enabled"]:
            return []
        # 上层策略生成器已经把研究问题拆成多组检索词。这里必须把收到的每一组
        # 当作一个完整查询，只访问一次 arXiv；再次按词切分会把 6 组查询膨胀成
        # 十几次网络请求，并因请求间隔而平白增加数十秒延迟。
        params = {
            "search_query": " ".join(query.strip().split()),
            "start": 0,
            "max_results": max_results,
            "sortBy": "relevance",
            "sortOrder": "descending",
        }
        timeout_s = float(os.environ.get("HARNESS_ARXIV_TIMEOUT_SECONDS", "45"))
        resp = await self.client.get(
            self.cfg["base_url"],
            params=params,
            timeout=timeout_s,
            follow_redirects=True,
        )
        resp.raise_for_status()
        return self._parse(resp.text)[:max_results]

    def _parse(self, xml_text: str) -> list[Paper]:
        ns = {"a": "http://www.w3.org/2005/Atom"}
        root = ET.fromstring(xml_text)
        papers = []
        for entry in root.findall("a:entry", ns):
            title_el = entry.find("a:title", ns)
            title = title_el.text.strip().replace("\n", " ") if title_el is not None and title_el.text else ""
            summary = entry.find("a:summary", ns)
            abstract = summary.text.strip().replace("\n", " ") if summary is not None and summary.text else ""
            published = entry.find("a:published", ns)
            year = int(published.text[:4]) if published is not None and published.text else None
            pub_date = published.text.strip() if published is not None and published.text else ""
            link_el = entry.find("a:id", ns)
            url = link_el.text.strip() if link_el is not None and link_el.text else ""
            doi = ""
            for link in entry.findall("a:link", ns):
                if link.get("title") == "doi":
                    doi = link.get("href", "")
                    break
            papers.append(Paper(
                title=title, abstract=abstract, year=year, url=url,
                doi=doi, source="arxiv", venue="arXiv Preprint",
                authors=[a.text for a in entry.findall("a:author/a:name", ns) if a.text],
                pub_date=pub_date,
            ))
        return papers


# ---------- bioRxiv / medRxiv（Europe PMC 关键词索引） ----------
class EuropePmcPreprintSearch:
    """通过Europe PMC检索指定预印本服务器，不抓取网页。"""

    def __init__(self, client: httpx.AsyncClient, server: str):
        normalized = server.strip().lower()
        if normalized not in {"biorxiv", "medrxiv"}:
            raise ValueError(f"unsupported preprint server: {server}")
        self.client = client
        self.server = normalized
        self.display_server = "bioRxiv" if normalized == "biorxiv" else "medRxiv"
        self.cfg = SEARCH_ENGINES[normalized]

    @staticmethod
    def _plain_text(value: str) -> str:
        without_tags = re.sub(r"<[^>]+>", " ", value or "")
        return " ".join(html.unescape(without_tags).split())

    async def search(
        self,
        query: str,
        limit: int = DEFAULT_RESULTS_PER_SOURCE,
        date_from: str | None = None,
    ) -> list[Paper]:
        if not self.cfg["enabled"]:
            return []
        clean_query = " ".join(query.replace('"', " ").split())
        clauses = [
            "SRC:PPR",
            f'PUBLISHER:"{self.display_server}"',
            f"({clean_query})",
        ]
        if date_from:
            today = datetime.now(UTC).date().isoformat()
            clauses.append(f"FIRST_PDATE:[{date_from} TO {today}]")
        response = await self.client.get(
            self.cfg["base_url"],
            params={
                "query": " AND ".join(clauses),
                "format": "json",
                "resultType": "core",
                "pageSize": min(max(1, int(limit)), 100),
            },
            timeout=15,
        )
        response.raise_for_status()
        return self._parse(response.json())[:limit]

    def _parse(self, data: dict) -> list[Paper]:
        papers = []
        for item in (data.get("resultList") or {}).get("result", []):
            details = item.get("bookOrReportDetails") or {}
            publisher = str(details.get("publisher") or "")
            if publisher.lower() != self.server:
                continue
            title = self._plain_text(str(item.get("title") or ""))
            if not title:
                continue
            author_list = (item.get("authorList") or {}).get("author", [])
            authors = []
            for author in author_list:
                name = (
                    author.get("fullName")
                    or " ".join(filter(None, [
                        author.get("firstName", ""),
                        author.get("lastName", ""),
                    ]))
                )
                if str(name).strip():
                    authors.append(str(name).strip())
            if not authors and item.get("authorString"):
                authors = [
                    part.strip()
                    for part in str(item["authorString"]).split(",")
                    if part.strip()
                ]
            doi = str(item.get("doi") or "").strip()
            pub_date = str(
                item.get("firstPublicationDate")
                or item.get("firstIndexDate")
                or ""
            ).strip()
            try:
                year = int(pub_date[:4]) if pub_date else None
            except ValueError:
                year = None
            abstract = self._plain_text(str(item.get("abstractText") or ""))
            keywords = [
                self._plain_text(str(keyword))
                for keyword in ((item.get("keywordList") or {}).get("keyword", []) or [])
                if self._plain_text(str(keyword))
            ]
            url = (
                f"https://doi.org/{doi}"
                if doi
                else f"https://europepmc.org/article/PPR/{item.get('id', '')}"
            )
            papers.append(Paper(
                title=title,
                authors=authors,
                year=year,
                venue=self.display_server,
                doi=doi,
                source=self.server,
                citations=int(item.get("citedByCount") or 0),
                url=url,
                abstract=abstract,
                keywords=keywords,
                fields_of_study=[self.display_server],
                pub_type="posted-content",
                pub_date=pub_date,
                metadata_provenance={
                    field: self.server
                    for field, value in {
                        "title": title,
                        "authors": authors,
                        "year": year,
                        "venue": self.display_server,
                        "doi": doi,
                        "url": url,
                        "abstract": abstract,
                    }.items()
                    if value
                },
            ))
        return papers


# ---------- PubMed ----------
class PubMedSearch:
    """PubMed E-utilities 检索；NCBI API Key 可选。"""

    _rate_lock: asyncio.Lock | None = None
    _last_request_at = 0.0

    def __init__(self, client: httpx.AsyncClient, api_key: str = ""):
        self.client = client
        self.cfg = SEARCH_ENGINES["pubmed"]
        self.api_key = api_key or os.environ.get("NCBI_API_KEY", "")
        self.tool = os.environ.get("NCBI_TOOL", "harness_framework")
        self.email = os.environ.get("NCBI_EMAIL", "")

    @classmethod
    async def _wait_for_request_slot(cls, min_interval: float) -> None:
        loop = asyncio.get_running_loop()
        if cls._rate_lock is None or getattr(cls._rate_lock, "_loop", loop) not in (None, loop):
            cls._rate_lock = asyncio.Lock()
            cls._last_request_at = 0.0
        async with cls._rate_lock:
            now = time.monotonic()
            wait = min_interval - (now - cls._last_request_at)
            if wait > 0:
                await asyncio.sleep(wait)
            cls._last_request_at = time.monotonic()

    async def _get(self, endpoint: str, params: dict) -> httpx.Response:
        params = dict(params)
        params["tool"] = self.tool
        if self.api_key:
            params["api_key"] = self.api_key
        if self.email:
            params["email"] = self.email
        # NCBI: 无 Key 不超过 3 req/s；有 Key 默认不超过 10 req/s。
        min_interval = 0.36 if not self.api_key else 0.11
        for attempt in range(2):
            await self._wait_for_request_slot(min_interval)
            response = await self.client.get(
                f"{self.cfg['base_url']}/{endpoint}",
                params=params,
                timeout=15,
            )
            if response.status_code != 429:
                response.raise_for_status()
                return response
            retry_after = response.headers.get("Retry-After")
            await asyncio.sleep(
                float(retry_after) if retry_after else min_interval * (attempt + 2)
            )
        response.raise_for_status()
        return response

    async def search(
        self,
        query: str,
        limit: int = DEFAULT_RESULTS_PER_SOURCE,
        date_from: str | None = None,
    ) -> list[Paper]:
        if not self.cfg["enabled"]:
            return []
        core_query = " ".join(query.strip().split())
        if core_query.lower().startswith("issn:"):
            issn = core_query[5:].split(";", 1)[0].strip()
            core_query = f"{issn}[issn]" if issn else core_query
        if date_from:
            today = datetime.now(UTC).date().strftime("%Y/%m/%d")
            start = date_from.replace("-", "/")
            core_query = (
                f"({core_query}) AND ({start}[Date - Publication] : "
                f"{today}[Date - Publication])"
            )

        search_response = await self._get(
            "esearch.fcgi",
            {
                "db": "pubmed",
                "term": core_query,
                "retmode": "json",
                "retmax": min(max(1, int(limit)), 100),
                "sort": "relevance",
            },
        )
        ids = search_response.json().get("esearchresult", {}).get("idlist", [])
        if not ids:
            return []

        fetch_response = await self._get(
            "efetch.fcgi",
            {
                "db": "pubmed",
                "id": ",".join(ids),
                "retmode": "xml",
            },
        )
        return self._parse(fetch_response.text)[:limit]

    @staticmethod
    def _element_text(element) -> str:
        if element is None:
            return ""
        return html.unescape("".join(element.itertext())).strip()

    @staticmethod
    def _publication_date(article) -> tuple[Optional[int], str]:
        date_node = article.find(".//Article/Journal/JournalIssue/PubDate")
        if date_node is None:
            date_node = article.find(".//Article/ArticleDate")
        if date_node is None:
            return None, ""
        year_text = PubMedSearch._element_text(date_node.find("Year"))
        month_text = PubMedSearch._element_text(date_node.find("Month"))
        day_text = PubMedSearch._element_text(date_node.find("Day"))
        medline = PubMedSearch._element_text(date_node.find("MedlineDate"))
        if not year_text and medline:
            match = re.search(r"\\b(18|19|20)\\d{2}\\b", medline)
            year_text = match.group(0) if match else ""
        try:
            year = int(year_text) if year_text else None
        except ValueError:
            year = None
        if not year:
            return None, medline
        month_map = {
            "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
            "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
        }
        if month_text:
            try:
                month = int(month_text)
            except ValueError:
                month = month_map.get(month_text[:3].lower())
        else:
            month = None
        try:
            day = int(day_text) if day_text else None
        except ValueError:
            day = None
        parts = [str(year)]
        if month:
            parts.append(f"{month:02d}")
            if day:
                parts.append(f"{day:02d}")
        return year, "-".join(parts)

    def _parse(self, xml_text: str) -> list[Paper]:
        root = ET.fromstring(xml_text)
        papers = []
        for record in root.findall(".//PubmedArticle"):
            citation = record.find("MedlineCitation")
            article = citation.find("Article") if citation is not None else None
            if article is None:
                continue
            pmid = self._element_text(citation.find("PMID"))
            title = self._element_text(article.find("ArticleTitle"))
            if not title:
                continue

            authors = []
            for author in article.findall(".//AuthorList/Author"):
                collective = self._element_text(author.find("CollectiveName"))
                name = " ".join(filter(None, [
                    self._element_text(author.find("ForeName")),
                    self._element_text(author.find("LastName")),
                ]))
                if collective or name:
                    authors.append(collective or name)

            abstract_parts = []
            for part in article.findall(".//Abstract/AbstractText"):
                text = self._element_text(part)
                if not text:
                    continue
                label = (part.get("Label") or "").strip()
                abstract_parts.append(f"{label}: {text}" if label else text)
            abstract = "\n".join(abstract_parts)

            doi = ""
            for article_id in record.findall(".//PubmedData/ArticleIdList/ArticleId"):
                if (article_id.get("IdType") or "").lower() == "doi":
                    doi = self._element_text(article_id)
                    break
            if not doi:
                for eid in article.findall(".//ELocationID"):
                    if (eid.get("EIdType") or "").lower() == "doi":
                        doi = self._element_text(eid)
                        break

            year, pub_date = self._publication_date(record)
            venue = self._element_text(article.find(".//Journal/Title"))
            journal_abbr = self._element_text(article.find(".//Journal/ISOAbbreviation"))
            if not venue:
                venue = journal_abbr
            journal_issn = article.find(".//Journal/ISSN")
            raw_issn = self._element_text(journal_issn)
            issn_type = (journal_issn.get("IssnType") if journal_issn is not None else "") or ""
            linking_issn = self._element_text(record.find(".//MedlineJournalInfo/ISSNLinking"))
            issn = raw_issn if issn_type.lower() != "electronic" else linking_issn
            eissn = raw_issn if issn_type.lower() == "electronic" else ""
            if not issn:
                issn = linking_issn
            nlm_id = self._element_text(record.find(".//MedlineJournalInfo/NlmUniqueID"))
            mesh = [
                self._element_text(node)
                for node in citation.findall(".//MeshHeading/DescriptorName")
                if self._element_text(node)
            ]
            keywords = [
                self._element_text(node)
                for node in citation.findall(".//KeywordList/Keyword")
                if self._element_text(node)
            ]
            papers.append(Paper(
                title=title,
                authors=authors,
                year=year,
                venue=venue,
                doi=doi,
                source="pubmed",
                citations=0,
                url=f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/" if pmid else "",
                abstract=abstract,
                keywords=keywords,
                fields_of_study=mesh,
                pub_type="journal-article",
                pub_date=pub_date,
                issn=issn, eissn=eissn, nlm_id=nlm_id, journal_abbr=journal_abbr,
                metadata_provenance={
                    field: "pubmed"
                    for field, value in {
                        "title": title,
                        "authors": authors,
                        "year": year,
                        "venue": venue,
                        "doi": doi,
                        "url": pmid,
                        "abstract": abstract,
                    }.items()
                    if value
                },
            ))
        return papers


# ---------- Semantic Scholar ----------
class SemanticScholarSearch:
    # Semantic Scholar applies a cumulative limit of 1 request/second across
    # all endpoints. Keep a small safety margin and share the limiter across
    # all instances in this process (search rounds create fresh instances).
    _min_request_interval = max(
        3.0, float(os.environ.get("SEMANTIC_SCHOLAR_MIN_INTERVAL", "3.0"))
    )
    _rate_lock: asyncio.Lock | None = None
    _last_request_at = 0.0

    def __init__(self, client: httpx.AsyncClient, api_key: str = ""):
        self.client = client
        self.cfg = SEARCH_ENGINES["semantic_scholar"]
        self.api_key = api_key
        self.headers = {"User-Agent": "SurveyHarness/1.0"}
        if api_key:
            self.headers["x-api-key"] = api_key

    @classmethod
    async def _wait_for_request_slot(cls) -> None:
        """Serialize S2 requests and leave >1 second between wire calls."""
        loop = asyncio.get_running_loop()
        # Reset defensively when isolated node runs create a new event loop.
        if cls._rate_lock is None or getattr(cls._rate_lock, "_loop", loop) not in (None, loop):
            cls._rate_lock = asyncio.Lock()
            cls._last_request_at = 0.0
        async with cls._rate_lock:
            now = time.monotonic()
            wait = cls._min_request_interval - (now - cls._last_request_at)
            if wait > 0:
                await asyncio.sleep(wait)
            cls._last_request_at = time.monotonic()

    async def _rate_limited_search(self, params: dict) -> httpx.Response:
        base_delay = self._min_request_interval
        for attempt in range(2):
            await self._wait_for_request_slot()
            resp = await self.client.get(
                f"{self.cfg['base_url']}/paper/search", params=params,
                headers=self.headers, timeout=15,
            )
            if resp.status_code == 429:
                retry_after = resp.headers.get("Retry-After")
                wait = float(retry_after) if retry_after else base_delay * (2 ** attempt)
                await asyncio.sleep(wait)
                continue
            return resp
        return resp

    async def search(self, query: str, limit: int = DEFAULT_RESULTS_PER_SOURCE, date_from: str | None = None) -> list[Paper]:
        if not self.cfg["enabled"]:
            return []
        fields = (
            "title,authors,year,citationCount,externalIds,venue,url,abstract,"
            "publicationVenue,publicationDate"
        )
        params = {"query": query, "limit": min(limit, 50), "fields": fields, "sort": "relevance"}
        if date_from:
            params["publicationDateOrYear"] = date_from + ":"
        try:
            resp = await self._rate_limited_search(params)
            if resp.status_code == 429:
                raise RuntimeError("Semantic Scholar API rate limited (HTTP 429)")
            resp.raise_for_status()
            return self._parse(resp.json())
        except Exception:
            # 交给 SearchManager._safe_search 记录 error/timeout，不能把失败伪装成零匹配。
            raise

    def _parse(self, data: dict) -> list[Paper]:
        papers = []
        for item in data.get("data", []):
            authors = [a.get("name", "") for a in item.get("authors", []) if a.get("name")]
            external_ids = item.get("externalIds", {}) or {}
            doi = (external_ids.get("DOI") or "")
            arxiv_id = external_ids.get("ArXiv", "")
            venue = item.get("venue", "") or ""
            if not venue:
                pub_venue = item.get("publicationVenue") or {}
                venue = pub_venue.get("name", "")
            if not venue:
                # S2 does not have publisher field; keep as is
                pass
            papers.append(Paper(
                title=(item.get("title") or ""), authors=authors, year=item.get("year"),
                venue=venue, doi=doi, source="semantic_scholar",
                citations=item.get("citationCount", 0), url=item.get("url", ""),
                abstract=item.get("abstract") or "",
                keywords=[arxiv_id] if arxiv_id else [],
                pub_date=item.get("publicationDate") or "",
            ))
        return papers


# ---------- CrossRef ----------
class CrossrefSearch:
    def __init__(self, client: httpx.AsyncClient):
        self.client = client
        self.cfg = SEARCH_ENGINES["crossref"]

    async def search_title(self, title: str, rows: int = 20) -> list[Paper]:
        """按原始题名检索；供学术搜索的精确标题通道使用。"""
        if not self.cfg["enabled"] or not title.strip():
            return []
        params = {
            "query.title": title.strip(),
            "rows": max(1, min(int(rows), 50)),
            "sort": "relevance",
            "order": "desc",
            "filter": (
                "type:journal-article,type:proceedings-article,"
                "type:book-chapter,type:book"
            ),
        }
        headers = {"User-Agent": "SurveyHarness/1.0"}
        # 标题通道和随后多源检索可能相邻；尊重 Retry-After，避免一次短暂
        # 429 让本来可精确命中的老文献消失。
        for attempt in range(2):
            resp = await self.client.get(
                self.cfg["base_url"],
                params=params,
                headers=headers,
                timeout=30,
            )
            if resp.status_code != 429:
                resp.raise_for_status()
                return self._parse(resp.json())
            retry_after = resp.headers.get("Retry-After")
            await asyncio.sleep(
                float(retry_after) if retry_after else 1.5 * (attempt + 1)
            )
        resp.raise_for_status()
        return []

    async def search(
        self,
        query: str,
        rows: int = DEFAULT_RESULTS_PER_SOURCE,
        date_from: str | None = None,
        publication_types: tuple[str, ...] | None = None,
        title_only: bool = False,
    ) -> list[Paper]:
        if title_only:
            return await self.search_title(query, rows=rows)
        if not self.cfg["enabled"]:
            return []
        # 一期刊可能同时有纸质版和电子版 ISSN；分别查询后合并，避免漏文献。
        if query.strip().lower().startswith("issn:") and ";" in query[5:]:
            merged = []
            for value in query.strip()[5:].split(";"):
                value = value.strip()
                if value:
                    merged.extend(await self.search("issn:" + value, rows=rows, date_from=date_from, publication_types=publication_types))
            unique = {}
            for paper in merged:
                key = (paper.doi or "").lower() or paper.title.lower()
                unique[key] = paper
            return list(unique.values())
        # Crossref 的 query 是相关性检索，不是布尔 AND；保留完整查询，
        # 避免旧实现截掉 salinity/turbulence 等后半段研究条件。
        core_query = query.strip()
        allowed_types = publication_types or (
            "journal-article", "proceedings-article", "book-chapter", "book"
        )
        type_filter = ",".join(f"type:{value}" for value in allowed_types)
        params = {"query": core_query, "rows": rows, "sort": "relevance",
                  "order": "desc", "filter": type_filter}
        # harvester uses issn:<print;electronic> for venue-scoped collection.
        # Crossref must receive this as an ISSN filter, not as free-text query.
        if core_query.lower().startswith("issn:"):
            issn = core_query[5:].split(";", 1)[0].strip()
            core_query = ""
            params["query"] = core_query
            if issn:
                params["filter"] += ",issn:" + issn
        if date_from:
            params["filter"] += ",from-pub-date:" + date_from
            # 远程层同时限制上界，避免把出版社未来排期返回给后台。
            params["filter"] += ",until-pub-date:" + datetime.now(UTC).date().isoformat()
        headers = {"User-Agent": "SurveyHarness/1.0"}
        try:
            resp = await self.client.get(self.cfg["base_url"], params=params, headers=headers, timeout=30)
            resp.raise_for_status()
            return self._parse(resp.json())
        except Exception:
            # 让 SearchManager 记录 error/timeout，而不是把 API 故障伪装成零结果。
            raise

    def _parse(self, data: dict) -> list[Paper]:
        papers = []
        for item in data.get("message", {}).get("items", []):
            authors = []
            for a in item.get("author", []):
                name = " ".join(filter(None, [a.get("given", ""), a.get("family", "")]))
                if name.strip():
                    authors.append(name.strip())
            doi = item.get("DOI", "")
            pub_type = item.get("type", "journal-article")
            pub_date = ""
            for dp in ["published-print", "published-online", "created"]:
                dp_data = item.get("date-parts", item.get(dp))
                if isinstance(dp_data, dict) and dp_data.get("date-parts"):
                    dp_data = dp_data["date-parts"]
                if dp_data and isinstance(dp_data, list) and dp_data[0]:
                    pub_date = "-".join(str(x) for x in dp_data[0])
                    break
            # CrossRef 不同 publication type 的 venue 来源不同
            venue = ""
            container_title = item.get("container-title", [])
            if container_title and container_title[0]:
                venue = container_title[0]
            else:
                # proceedings / proceedings-article / book-chapter 等可能没有 container-title
                event = item.get("event", {}) or {}
                if event:
                    venue = event.get("name", "")
                if not venue:
                    # publisher 兜底：Copernicus EGU abstracts, MDPI preprints 等
                    pub_name = item.get("publisher") or ""
                    if pub_name and "copernicus" in pub_name.lower() and (item.get("DOI") or "").lower().find("egu") >= 0:
                        venue = "EGU General Assembly"
                    elif pub_name:
                        venue = pub_name
            issn = ""
            eissn = ""
            for identifier in item.get("issn-type", []) or []:
                value = str(identifier.get("value") or "")
                kind = str(identifier.get("type") or "").lower()
                if kind == "electronic":
                    eissn = value
                elif kind == "print":
                    issn = value
            fallback_issns = list(item.get("ISSN", []) or [])
            if not issn and fallback_issns:
                issn = str(fallback_issns[0])
            if not eissn and len(fallback_issns) > 1:
                eissn = str(fallback_issns[1])
            papers.append(Paper(
                title=html.unescape("".join(item.get("title", []))),
                authors=authors,
                year=item.get("published-print", {}).get("date-parts", [[None]])[0][0]
                      or item.get("created", {}).get("date-parts", [[None]])[0][0],
                venue=venue,
                doi=doi, source="crossref",
                citations=item.get("is-referenced-by-count", 0),
                url=item.get("URL", ""),
                abstract=item.get("abstract") or "",
                pub_type=pub_type, pub_date=pub_date, issn=issn, eissn=eissn,
            ))
        return papers


# ---------- OpenAlex ----------
class OpenAlexSearch:
    def __init__(self, client: httpx.AsyncClient):
        self.client = client
        self.cfg = SEARCH_ENGINES["openalex"]

    async def search(self, query: str, per_page: int = DEFAULT_RESULTS_PER_SOURCE, date_from: str | None = None) -> list[Paper]:
        if not self.cfg["enabled"]:
            return []
        if query.strip().lower().startswith("issn:") and ";" in query[5:]:
            merged = []
            for value in query.strip()[5:].split(";"):
                value = value.strip()
                if value:
                    merged.extend(await self.search("issn:" + value, per_page=per_page, date_from=date_from))
            unique = {}
            for paper in merged:
                key = (paper.doi or "").lower() or paper.title.lower()
                unique[key] = paper
            return list(unique.values())
        core_query = query.strip()
        # OpenAlex 的 `search=` 全文检索是 Premium 计费功能（每请求 $0.001，需余额）；
        # 免费/余额为 0 的 key 会被 429 "Insufficient budget" 拒绝。改用免费的
        # `filter=title_and_abstract.search`：带引号做短语精确匹配（AND 分词会严重跑偏，
        # 如 synthetic+control+method 会混入 8 万篇无关论文），并按被引降序——
        # 既避开计费，又把被引最高的奠基论文排进候选（文献调研要抓的开山作）。
        phrase = '"' + core_query.replace('"', '') + '"'
        params = {"filter": f"title_and_abstract.search:{phrase}",
                  "per-page": min(per_page, 200), "sort": "cited_by_count:desc"}
        # OpenAlex supports source ISSN filtering; use it for journal-scoped harvests.
        if core_query.lower().startswith("issn:"):
            issn = core_query[5:].split(";", 1)[0].strip()
            # ISSN 采集按期刊与日期取最新内容，与关键词检索共用 sort/filter 槽位。
            params["sort"] = "publication_date:desc"
            if issn:
                params["filter"] = "primary_location.source.issn:" + issn
            else:
                params.pop("filter", None)
        if date_from:
            date_filter = "from_publication_date:" + date_from
            date_filter += ",to_publication_date:" + datetime.now(UTC).date().isoformat()
            params["filter"] = (params.get("filter", "") + "," + date_filter).lstrip(",")
        api_key = os.environ.get("OPENALEX_API_KEY")
        if api_key:
            params["api_key"] = api_key
        headers = {"User-Agent": "SurveyHarness/1.0", "Accept": "application/json"}
        try:
            resp = await self.client.get(self.cfg["base_url"], params=params, headers=headers, timeout=30)
            resp.raise_for_status()
            return self._parse(resp.json())
        except Exception:
            raise

    def _parse(self, data: dict) -> list[Paper]:
        papers = []
        for item in data.get("results", []):
            authors = [a.get("author", {}).get("display_name", "") for a in item.get("authorships", []) if a.get("author")]
            pub_date = item.get("publication_date", "")
            year = int(pub_date[:4]) if pub_date else item.get("publication_year")
            venue = ""
            # OpenAlex 新版 API: host_venue 已废弃，期刊名在 primary_location.source.display_name
            primary_loc = item.get("primary_location") or {}
            source_record = {}
            if primary_loc:
                source_record = primary_loc.get("source") or {}
                venue = source_record.get("display_name", "")
                # 兜底：source 为 None 时（如会议论文），用 raw_source_name
                if not venue:
                    venue = primary_loc.get("raw_source_name", "")
            # 兜底：旧版 host_venue
            if not venue:
                host = item.get("host_venue", {}) or {}
                venue = host.get("display_name", "") if host else ""
            if not venue:
                # 兜底：用 publisher 名
                venue = (item.get("publisher") or "")
            doi = item.get("doi", "") or ""
            if doi.startswith("https://doi.org/"):
                doi = doi[16:]
            # OpenAlex 对无 DOI 的论文分配 hash 标识符，不是真实 DOI
            if doi.startswith("hash:") or not doi:
                doi = ""
            pdf_url = ""
            # OpenAlex 返回 open_access.oa_url (最佳 OA 链接)
            oa = item.get("open_access", {}) or {}
            if oa.get("oa_url"):
                pdf_url = oa["oa_url"]
            # 其次 best_oa_location.pdf_url
            if not pdf_url:
                best_loc = item.get("best_oa_location", {}) or {}
                pdf_url = best_loc.get("pdf_url", "") or ""
            # 兜底: locations 数组
            if not pdf_url:
                for loc in item.get("locations", []) or []:
                    p = loc.get("pdf_url", "")
                    if p:
                        pdf_url = p
                        break
            inv_index = item.get("abstract_inverted_index")
            abstract_text = ""
            if inv_index and isinstance(inv_index, dict):
                # 将 inverted index 转回文本
                word_positions = [(pos, word) for word, positions in inv_index.items() for pos in (positions or [])]
                word_positions.sort(key=lambda x: x[0])
                abstract_text = " ".join(word for _, word in word_positions)
            # OA 状态
            oa_info = item.get("open_access", {}) or {}
            paper_is_oa = oa_info.get("is_oa", False)
            paper_oa_url = oa_info.get("oa_url", "") or ""
            papers.append(Paper(
                title=(item.get("title") or ""), authors=authors, year=year,
                venue=venue, doi=doi, source="openalex",
                citations=item.get("cited_by_count", 0),
                url=item.get("id", ""), abstract=abstract_text,
                keywords=[], pub_date=pub_date,
                is_oa=paper_is_oa, oa_url=paper_oa_url,
                issn=str(source_record.get("issn_l") or ((source_record.get("issn") or [""])[0]) or ""),
                eissn=str(((source_record.get("issn") or ["", ""])[1:2] or [""])[0]),
            ))
            if pdf_url:
                papers[-1].pdf_url = pdf_url
        return papers



# ---------- Google Scholar (镜像代理 scholar.lanfanshu.cn) ----------
_GS_BASE = "https://scholar.lanfanshu.cn"


class GoogleScholarSearch:
    """Google Scholar 镜像搜索（scholar.lanfanshu.cn）"""

    def __init__(self, client: httpx.AsyncClient):
        self.client = client
        self.cfg = SEARCH_ENGINES.get("google_scholar", {"enabled": True})

    async def search(self, query: str, max_results: int = DEFAULT_RESULTS_PER_SOURCE) -> list[Paper]:
        if not self.cfg.get("enabled", True):
            return []
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }
        params = {"hl": "en", "q": query}
        try:
            resp = await self.client.get(
                f"{_GS_BASE}/scholar", params=params, headers=headers, timeout=15, follow_redirects=True
            )
            if resp.status_code != 200:
                print(f"  [GoogleScholar] HTTP {resp.status_code}")
                return []
            return self._parse(resp.text)
        except Exception as e:
            print(f"  [GoogleScholar] 请求异常: {e}")
            return []

    def _parse(self, html_text: str) -> list[Paper]:
        from bs4 import BeautifulSoup
        papers = []
        soup = BeautifulSoup(html_text, "html.parser")

        for entry in soup.select(".gs_ri"):
            try:
                title_el = entry.select_one("h3.gs_rt a")
                if not title_el:
                    title_el = entry.select_one("h3.gs_rt")
                if not title_el:
                    continue
                title = title_el.get_text(strip=True)
                url = title_el.get("href", "")

                # 去掉 [HTML]、[PDF] 等前缀
                for prefix in ["[HTML][HTML] ", "[HTML] ", "[PDF] ", "[CITATION] ", "[BOOK] "]:
                    if title.startswith(prefix):
                        title = title[len(prefix):]
                        break

                # 作者/期刊/年份
                a_el = entry.select_one(".gs_a")
                authors = []
                venue = ""
                year = None
                if a_el:
                    a_text = a_el.get_text(strip=True)
                    # 格式示例: "Q Li, G Yang, Y Huang - International Journal of …, 2024 - Elsevier"
                    # 或包含省略号: "H Zhang, A Yu, W Zhong... - Powder Technology, 2015 - Elsevier"
                    # 年份可能在整段的任何位置（被 … 截断时不在 parts[1]）
                    year_match = re.search(r"\b(20\d{2})\b", a_text)
                    if year_match:
                        year = int(year_match.group(1))
                    parts = a_text.split(" - ")
                    if len(parts) >= 2:
                        authors = [a.strip() for a in parts[0].split(",") if a.strip() and not a.strip().startswith("…")]
                        venue_part = parts[1].strip()
                        # 去掉年份、publisher 只留期刊名
                        venue = re.sub(r",\s*20\d{2}.*", "", venue_part).strip()
                        if not venue or venue == parts[1].strip():
                            venue = venue_part.split(",")[0].strip()
                    elif len(parts) >= 1:
                        authors = [a.strip() for a in parts[0].split(",") if a.strip() and not a.strip().startswith("…")]

                # 摘要片段
                snippet_el = entry.select_one(".gs_rs")
                abstract = snippet_el.get_text(strip=True) if snippet_el else ""

                # 父容器 HTML（用于提取引用次数和 DOI）
                parent_html = str(entry.parent) if entry.parent else ""

                # 引用次数
                citations = 0
                cite_match = re.search(r"被引用次数：(\d+)", parent_html)
                if cite_match:
                    citations = int(cite_match.group(1))

                # DOI (从父容器链接或全文提取)
                doi = ""
                # 提取所有链接中的 DOI 标识
                doi_hrefs = re.findall(
                    r'href="https?://[^"]*?(10\.\d{4,}/[^"\s/?&]+)',
                    parent_html
                )
                if doi_hrefs:
                    candidates = set()
                    for d in doi_hrefs:
                        # 去掉 /pdf /abstract /full 等后缀
                        clean = re.sub(r'/(?:pdf|abstract|full|article|x-references|htm|xml)$', '', d)
                        candidates.add(clean)
                    # 优先选有效 DOI
                    valid_dois = [c for c in candidates if re.match(r'^10\.\d{4,}/', c)]
                    if valid_dois:
                        doi = valid_dois[0]
                if not doi:
                    # 全文兜底
                    all_text = parent_html
                    doi_match = re.search(r"(10\.\d{4,}/[^\s\"<>]+)", all_text)
                    if doi_match:
                        candidate = doi_match.group(1).rstrip(".,;")
                        # 验证有效 DOI 格式
                        if re.match(r'^10\.\d{4,}/[a-zA-Z0-9._-]+$', candidate) and '/' in candidate.split('/', 1)[1]:
                            doi = candidate

                p = Paper(
                    title=title, authors=authors, year=year,
                    venue=venue, doi=doi, source="google_scholar",
                    citations=citations, url=url, abstract=abstract,
                )
                papers.append(p)
            except Exception as e:
                continue

        print(f"  [GoogleScholar] {len(papers)} 篇")
        return papers[:DEFAULT_RESULTS_PER_SOURCE]


# ========================================================================
# SearchManager — 统一搜索入口
# ========================================================================
class SearchManager:
    def __init__(
        self,
        sem_scholar_key: str = "",
        progress_callback: Callable[[dict], None] | None = None,
        arxiv_timeout_seconds: float = 15.0,
        per_source_limits: dict[str, int] | None = None,
    ):
        try:
            from .local_index import LocalPaperIndex
            self.local_index = LocalPaperIndex()
        except Exception:
            self.local_index = None
        self._s2_key = sem_scholar_key or os.environ.get("SEMANTIC_SCHOLAR_API_KEY", "")
        self._progress_callback = progress_callback
        self._arxiv_timeout_seconds = max(0.1, float(arxiv_timeout_seconds))
        # None 保留 literature/E2E 的全局来源上限；呈现层学术搜索可以传入
        # 独立上限，避免为了调 UI 的候选规模而改变节点本身的检索行为。
        self._per_source_limits = (
            {canonical_source(k): max(1, int(v)) for k, v in per_source_limits.items()}
            if per_source_limits is not None
            else None
        )
        from core.paths import home as _hf_home
        self._cache_dir = _hf_home() / "literature" / "search_cache"
        self._cache_dir.mkdir(parents=True, exist_ok=True)

    def _emit_progress(self, event: dict) -> None:
        if self._progress_callback is None:
            return
        try:
            self._progress_callback(event)
        except Exception:
            # 进度展示不能影响实际检索。
            pass

    def _cache_path(self, key: str) -> Path:
        return self._cache_dir / f"{key}.pkl"

    def _load_cached_results(self, key: str):
        path = self._cache_path(key)
        if path.exists():
            try:
                with open(path, 'rb') as f:
                    return pickle.load(f)
            except Exception:
                return None
        return None

    def _save_cached_results(self, key: str, papers: list):
        path = self._cache_path(key)
        try:
            with open(path, 'wb') as f:
                # 只落**普通 list**：缓存格式与历史文件保持一致（存量 pkl 就是 list），
                # 审计信息按调用现算，不进缓存（缓存跨调用复用，审计是本次调用的事实）。
                pickle.dump(list(papers), f)
        except Exception:
            pass

    def _drop_cached_results(self, key: str) -> None:
        """作废某个 cache_key 的缓存文件。

        issue #230：白名单修好之前写下的缓存里混着禁用源的记录，只在读取时过滤
        不够 —— 那份脏文件会一直存在、每次都要过滤，且过滤后可能少于真实可得结果
        （例：CNKI-only 的 key 下存的全是 crossref，过滤完变成 0 篇，而真做一次
        CNKI 检索本来有结果）。所以检测到污染直接删文件 + 走一次干净检索。
        """
        try:
            self._cache_path(key).unlink(missing_ok=True)
        except Exception:
            pass

    def _search_shared_catalog(self, query: str, limit: int = 200) -> list[Paper]:
        """Search the shared cross-project catalog used by academic search and feed."""
        try:
            from core.paths import literature_papers_dir
            db_path = literature_papers_dir() / "literature_catalog.sqlite3"
        except Exception:
            from core.paths import home as _root  # 「根在哪」一处回答（含 Windows 分支）
            db_path = _root() / "literature" / "papers" / "literature_catalog.sqlite3"
        if not db_path.is_file():
            return []
        terms = [t for t in re.findall(r"[\w\u4e00-\u9fff]+", str(query).lower()) if len(t) >= 2]
        if not terms:
            return []
        clauses = " AND ".join(["(lower(title) LIKE ? OR lower(metadata_json) LIKE ?)"] * len(terms))
        params = []
        for term in terms:
            like = "%" + term + "%"; params.extend([like, like])
        try:
            with sqlite3.connect(db_path, timeout=5) as conn:
                rows = conn.execute("SELECT doi,title,year,source,metadata_json FROM papers WHERE " + clauses + " ORDER BY updated_at DESC LIMIT ?", (*params, max(1, int(limit)))).fetchall()
        except sqlite3.Error:
            return []
        out=[]
        for doi,title,year,source,raw in rows:
            try: meta=json.loads(raw or "{}") if isinstance(raw,str) else {}
            except (TypeError,ValueError): meta={}
            out.append(Paper(title=str(title or meta.get("title") or ""), authors=meta.get("authors") or [], year=year, venue=str(meta.get("venue") or ""), doi=str(doi or ""), source=str(source or meta.get("source") or ""), citations=int(meta.get("citations") or 0), url=str(meta.get("url") or ""), abstract=str(meta.get("abstract") or ""), keywords=meta.get("keywords") or [], pub_type=str(meta.get("pub_type") or "journal-article"), pub_date=str(meta.get("pub_date") or meta.get("published_at") or ""), pdf_url=str(meta.get("pdf_url") or ""), is_oa=bool(meta.get("is_oa")), oa_url=str(meta.get("oa_url") or ""), subjects=meta.get("subjects") or [], arxiv_categories=meta.get("arxiv_categories") or []))
        return out

    async def search_local(self, query: str, limit: int = 200) -> "SearchResults":
        """本地离线检索入口；优先查询共享 literature catalog。"""
        papers = self._search_shared_catalog(query, limit=limit)
        if papers:
            counts = {}
            for paper in papers:
                src = canonical_source(getattr(paper, "source", "")) or "<empty>"
                counts[src] = counts.get(src, 0) + 1
            return SearchResults(papers, requested_sources=sorted(counts), attempted_sources=[], warnings=[], from_cache=True, raw_source_counts=counts, deduped_source_counts=counts, source_diagnostics={src: {"status": "local_catalog", "raw_count": n, "deduped_count": n} for src,n in counts.items()})
        if not self.local_index:
            return SearchResults([], warnings=[{"code": "local_index_unavailable", "detail": "本地论文索引不可用"}])
        papers = self.local_index.search_local(query, limit=limit)
        counts = {}
        for paper in papers:
            source = canonical_source(getattr(paper, "source", "")) or "<empty>"
            counts[source] = counts.get(source, 0) + 1
        return SearchResults(papers, requested_sources=sorted(counts),
                             attempted_sources=[], warnings=[], from_cache=True,
                             raw_source_counts=counts, deduped_source_counts=counts,
                             source_diagnostics={src: {"status": "local", "raw_count": n, "deduped_count": n}
                                                 for src, n in counts.items()})

    async def search_all(
        self,
        query: str,
        max_per_source: int = DEFAULT_RESULTS_PER_SOURCE,
        s2_api_key: str = "",
        lang_mode: str = "auto",
        enabled_sources: set[str] | None = None,
        date_from: str | None = None,
        include_local_catalog: bool = True,
        use_query_cache: bool = True,
        metadata_lookup_sources: set[str] | None = None,
        enrich_metadata: bool = True,
        persist_results: bool = True,
        crossref_query_mode: str = "full",
    ) -> "SearchResults":
        """多源检索，本地优先，远程兜底

        支持中英文查询，支持 venue:Nature 语法

        lang_mode: 'auto' — 中文自动翻译后搜索英文源 + CNKI
                   'cn'   — 仅中文源（CNKI），不搜英文
                   'en'   — 仅英文源
        enabled_sources: 来源白名单：arxiv / biorxiv / medrxiv /
                         semantic_scholar / pubmed / crossref / openalex /
                         google_scholar / cnki
                         - None  → 用 DEFAULT_ENABLED_SOURCES（全开）
                         - 空集合 → **零结果**（不再回退全开，见 issue #230）

        返回 SearchResults（list[Paper] 的子类，向后兼容），额外带
        requested_sources / attempted_sources / unavailable_sources /
        returned_sources / warnings 供调用方审计与展示。
        """
        query_normalized = query.lower().strip()
        # 白名单归一：**只有 None 才用缺省全开**。
        # 老写法 `set(enabled_sources or {...})` 把空集合（"一个源都别搜"）当 falsy →
        # 静默全开，是 issue #230 的根因之一。
        if enabled_sources is None:
            enabled_sources = set(DEFAULT_ENABLED_SOURCES)
        else:
            enabled_sources = {canonical_source(s) for s in enabled_sources if canonical_source(s)}

        warnings: list[dict] = []
        requested_sources = sorted(enabled_sources)

        unknown = sorted(enabled_sources - KNOWN_SOURCES)
        if unknown:
            # 不静默忽略：拼错源名会让人以为搜了，其实一个都没搜
            warnings.append({
                "code": "unknown_source_requested",
                "sources": unknown,
                "detail": f"未知来源名 {unknown}，已忽略；已知来源：{sorted(KNOWN_SOURCES)}",
            })
            enabled_sources = enabled_sources & set(KNOWN_SOURCES)

        if not enabled_sources:
            # fail-closed：显式"什么都不启用"就返回零结果，绝不回退到默认全开
            print("  [SearchManager] 来源白名单为空 → 返回零结果（不回退默认源）")
            warnings.append({
                "code": "empty_source_whitelist",
                "sources": [],
                "detail": "enabled_sources 为空集合：未检索任何来源。传 None 才表示使用默认全部来源。",
            })
            return SearchResults(
                [], requested_sources=requested_sources, warnings=warnings,
            )

        # 检查缓存（中文转英文后也命中缓存）
        sources_key = ",".join(sorted(enabled_sources))
        cache_material = f"{query_normalized}:{max_per_source}:{lang_mode}:{sources_key}"
        if date_from:
            cache_material += f":{date_from}"
        cache_key = hashlib.md5(cache_material.encode()).hexdigest()
        cached = self._load_cached_results(cache_key) if use_query_cache else None
        if cached:
            # 缓存也要过白名单：白名单 bug 修好之前落盘的缓存里混着禁用源的记录
            # （CNKI-only 的 key 下存着 crossref 结果），不校验就等于 bug 没修。
            kept, dropped = filter_papers_by_sources(cached, enabled_sources)
            if dropped:
                print(f"  [SearchManager] ⚠️ 缓存含白名单外来源 {dropped} → 作废该缓存，重新检索")
                self._drop_cached_results(cache_key)
            else:
                print(f"  [SearchManager] 缓存命中: {len(kept)} 篇")
                cached_counts = {}
                for paper in kept:
                    src = canonical_source(getattr(paper, "source", "")) or "<empty>"
                    cached_counts[src] = cached_counts.get(src, 0) + 1
                return SearchResults(
                    kept,
                    requested_sources=requested_sources,
                    warnings=warnings,
                    from_cache=True,
                    raw_source_counts=dict(cached_counts),
                    deduped_source_counts=dict(cached_counts),
                    source_diagnostics={src: {"status": "cached", "raw_count": n,
                                              "deduped_count": n}
                                        for src, n in cached_counts.items()},
                )

        # 解析 venue: 过滤器
        venue_filter = None
        _vf_match = re.search(r'(venue|journal):([a-zA-Z0-9\s&.-]+?)(?:\s+\w+:|$)', query_normalized)
        if _vf_match:
            venue_filter = _vf_match.group(2).strip()
            query_normalized = re.sub(r'(venue|journal):[a-zA-Z0-9\s&.-]+', '', query_normalized).strip()
        
        def _apply_venue_filter(papers: list) -> list:
            if not venue_filter:
                return papers
            vf = venue_filter.lower()
            return [p for p in papers if p.venue and re.search(r'\b' + re.escape(vf) + r'\b', p.venue.lower())]
        
        attempted_sources: set[str] = set()
        unavailable_sources: set[str] = set()

        # --- 中文模式：仅 CNKI，不搜英文源 ---
        if lang_mode == 'cn':
            if "cnki" not in enabled_sources:
                warnings.append({
                    "code": "no_source_for_lang_mode",
                    "sources": [],
                    "detail": "lang_mode='cn' 只搜 CNKI，但 cnki 不在白名单内：未检索任何来源。",
                })
                return self._finalize_results(
                    [], enabled_sources,
                    requested_sources=requested_sources,
                    attempted_sources=attempted_sources,
                    unavailable_sources=sorted(enabled_sources),
                    warnings=warnings,
                )
            cnki_papers, cnki_warns, cnki_ok = await self._search_cnki(query, max_per_source)
            warnings.extend(cnki_warns)
            attempted_sources.add("cnki")
            if not cnki_ok:
                unavailable_sources.add("cnki")
            return self._finalize_results(
                _apply_venue_filter(cnki_papers), enabled_sources,
                requested_sources=requested_sources,
                attempted_sources=attempted_sources,
                unavailable_sources=unavailable_sources,
                warnings=warnings,
            )

        # --- 非中文模式：可选本地索引 + 远程搜索 ---
        # E2E/历史调用默认保留本地索引兼容路径；学术搜索传 False，避免把整个
        # catalog 当作检索结果，只复用上面命中的 query cache，未命中再访问远程源。
        local_results = []
        if include_local_catalog and self.local_index:
            local_results = self.local_index.search(query_normalized, limit=max_per_source * 10)

        # 语义匹配
        semantic_results = []
        if include_local_catalog and self.local_index and len(local_results) < 20:
            english_terms = self._extract_english_terms(query_normalized)
            if english_terms:
                semantic_results = self.local_index.search_by_terms(english_terms, limit=max_per_source * 10)

        local_combined = self._merge_results(local_results, semantic_results) if semantic_results else local_results

        # 本地索引也必须过白名单（issue #230 主因）：papers.db 是**跨来源跨课题**的历史
        # 沉淀，同一个 query 之前用全源搜过，crossref/arxiv 记录就躺在里面；这条路径
        # 原来完全没接白名单，于是 CNKI-only 调用照样吐 crossref。
        dropped_before_exit: dict = {}
        if local_combined:
            local_combined, local_dropped = filter_papers_by_sources(local_combined, enabled_sources)
            # 定期采集带 date_from 时，本地历史索引也必须遵守同一窗口；
            # 否则远程结果虽已按日期过滤，旧论文仍会在合并阶段混入。
            if date_from:
                try:
                    from datetime import date as _date
                    _cutoff = _date.fromisoformat(date_from)
                    def _recent_local(p):
                        raw = str(getattr(p, "pub_date", "") or "").strip()
                        try:
                            return bool(raw) and _date.fromisoformat(raw[:10]) >= _cutoff
                        except ValueError:
                            return False
                    local_combined = [p for p in local_combined if _recent_local(p)]
                except ValueError:
                    pass
            if local_dropped:
                print(f"  [SearchManager] 本地索引按白名单过滤掉 {sum(local_dropped.values())} 篇: {local_dropped}")
                for _src, _n in local_dropped.items():
                    dropped_before_exit[_src] = dropped_before_exit.get(_src, 0) + _n

        # --- 远程搜索 ---
        # 拿到什么 query 就搜什么，不在工具内部做任何翻译。跨语言覆盖由调用方
        # （agent，本身就是 LLM）决策：想搜国际库就传英文词，想搜 CNKI 就传中文，
        # 要两边都覆盖就分两次调用。工具不替 agent 偷偷 LLM 翻译。
        #
        # crossref 检索模式走**实例状态**，不往 `_search_remote` 的签名里加参数：
        # `tests/test_source_whitelist.py` 按真方法的签名推替身该接住哪些参数
        # （`test_the_remote_double_still_stands_where_the_real_method_stands`），
        # 签名一长，框架层的测试替身就得跟着改 —— 而那个文件不在文献节点的提交
        # 边界内，等于让本节点去改别人的测试。模式是"这次检索怎么查 crossref"，
        # 本来就是管理器这一次调用的状态，放实例上语义也对。
        self._crossref_query_mode = crossref_query_mode
        remote_kwargs = {"date_from": date_from}
        remote_results, remote_audit = await self._search_remote(
            query, max_per_source, s2_api_key or self._s2_key, enabled_sources,
            **remote_kwargs,
        )
        attempted_sources |= remote_audit["attempted"]
        unavailable_sources |= remote_audit["unavailable"]
        warnings.extend(remote_audit["warnings"])
        if "crossref" not in enabled_sources and any(
            getattr(p, "doi", None) and not getattr(p, "venue", None)
            for p in remote_results
        ):
            warnings.append({
                "code": "enrichment_skipped",
                "sources": ["crossref"],
                "detail": "Crossref 不在白名单内，缺失 venue 的元数据未做 DOI 回填。",
            })

        # --- CNKI 中文搜索（仅当查询含中文时） ---
        if "cnki" in enabled_sources:
            if self._has_chinese(query):
                cnki_papers, cnki_warns, cnki_ok = await self._search_cnki(query, max_per_source)
                warnings.extend(cnki_warns)
                attempted_sources.add("cnki")
                if not cnki_ok:
                    unavailable_sources.add("cnki")
                if cnki_papers:
                    # 去重合并（CNKI 论文无 DOI，用标题去重）
                    remote_results = self._merge_results(remote_results, cnki_papers)
                    print(f"  [SearchManager] CNKI 补充 {len(cnki_papers)} 篇")
            else:
                # 显式记录"要了 CNKI 但没搜"：否则 CNKI-only + 英文 query 会静默返回
                # 零结果，调用方（LLM）看不出是查询语言不对还是真没文献。
                unavailable_sources.add("cnki")
                warnings.append({
                    "code": "source_not_attempted",
                    "sources": ["cnki"],
                    "detail": "query 不含中文，跳过 CNKI（CNKI 是中文库，本工具不代做翻译）。",
                })

        # 合并
        final_results = self._merge_results(remote_results, local_combined)

        # 检索来源与元数据补齐来源是两个契约。默认保持严格白名单；呈现层可显式
        # 开启低成本 DOI 精准补齐，而不把该来源加入关键词检索和来源数量统计。
        enrichment_sources = (
            enabled_sources
            if metadata_lookup_sources is None
            else {canonical_source(src) if src != "publisher_landing" else src
                  for src in metadata_lookup_sources}
        )
        if enrich_metadata:
            await self._enrich_metadata_cross_source(final_results, enrichment_sources)

        # 引用量回填：跳过（用户要求去掉，避免 S2 查询延迟）

        # 落盘/落库前先过白名单：脏数据一旦进了 papers.db / pkl 缓存，就会在**以后**
        # 的调用里再冒出来（issue #230 就是这么从"一次泄漏"变成"持续泄漏"的）。
        final_results, final_dropped = filter_papers_by_sources(final_results, enabled_sources)
        for _src, _n in final_dropped.items():
            dropped_before_exit[_src] = dropped_before_exit.get(_src, 0) + _n

        if persist_results and self.local_index and final_results:
            self.local_index.add_papers(final_results, query_normalized)
            if use_query_cache:
                self.local_index.cache_search_results(query_normalized, final_results)

        # 文件查询缓存只服务明确允许复用答案的调用；学术搜索每次重新检索。
        if use_query_cache:
            self._save_cached_results(cache_key, final_results)

        return self._finalize_results(
            _apply_venue_filter(final_results), enabled_sources,
            requested_sources=requested_sources,
            attempted_sources=attempted_sources,
            unavailable_sources=unavailable_sources,
            warnings=warnings,
            pre_dropped=dropped_before_exit,
            raw_source_counts=remote_audit.get("raw_source_counts", {}),
            deduped_source_counts=remote_audit.get("deduped_source_counts", {}),
            source_diagnostics=remote_audit.get("source_diagnostics", {}),
        )

    # ---------- 白名单兜底 / CNKI 取数 ----------
    def _finalize_results(
        self,
        papers: list,
        enabled_sources,
        *,
        requested_sources: list[str],
        attempted_sources,
        unavailable_sources,
        warnings: list[dict],
        from_cache: bool = False,
        pre_dropped: dict | None = None,
        raw_source_counts=None,
        deduped_source_counts=None,
        source_diagnostics=None,
    ) -> "SearchResults":
        """所有 return 的最后一道关：再过一次白名单（fail-closed 兜底）。

        为啥要"多余"地再过一次：本 issue 的教训是白名单只接了远程一条路径，
        本地索引 / 缓存两条路径各自绕过去了。这里做成"出口唯一 + 出口校验"，
        以后再加取数路径（新引擎、PDF 导入、别的库），漏接过滤最坏也只是**少结果**，
        不会把禁用源当合法结果吐出去；同时打印 loud warning 把漏源路径暴露出来，
        而不是静静吞掉（fail-loud）。
        """
        kept, dropped = filter_papers_by_sources(papers, enabled_sources)
        leaked = dict(pre_dropped or {})
        for k, v in dropped.items():
            leaked[k] = leaked.get(k, 0) + v
        if dropped:
            print(
                f"  [SearchManager] ⚠️ 出口白名单拦下 {sum(dropped.values())} 篇禁用源结果 {dropped}"
                f" —— 说明有取数路径没接白名单，请修那条路径（issue #230 形态）"
            )
        if leaked:
            warnings = warnings + [{
                "code": "disabled_source_filtered",
                "sources": sorted(leaked),
                "detail": f"已丢弃白名单外来源的记录（多为本地索引/缓存历史沉淀）：{leaked}",
            }]
        return SearchResults(
            kept,
            requested_sources=requested_sources,
            attempted_sources=attempted_sources,
            unavailable_sources=unavailable_sources,
            warnings=warnings,
            from_cache=from_cache,
            raw_source_counts=raw_source_counts,
            deduped_source_counts=deduped_source_counts,
            source_diagnostics=source_diagnostics,
        )

    async def _search_cnki(self, query: str, max_per_source: int) -> tuple[list, list[dict], bool]:
        """CNKI 检索，返回 (papers, warnings, available)。

        available=False 表示 CNKI 这个源本次**不可用**（没配 cookie / 异常），
        调用方据此写进 unavailable_sources：CNKI 不可用时必须是"零结果 + 明确 warning"，
        绝不能拿别的（尤其是被禁用的）源顶替 —— 那等于悄悄换了检索范围。
        """
        warnings: list[dict] = []
        try:
            from .cnki_search import CnkiSearch
            cnki = CnkiSearch()
            if not cnki.is_ready:
                print("  [SearchManager] ⚠️ CNKI 未配置 cookie → 零结果（不用其它源顶替）")
                warnings.append({
                    "code": "source_unavailable",
                    "sources": ["cnki"],
                    "reason": "not_configured",
                    "detail": "CNKI 未配置有效 cookie（缺 LID），本次未检索 CNKI；结果为零篇而非用其它来源替代。",
                })
                return [], warnings, False
            papers = await cnki.search(query, max_results=max_per_source)
            print(f"  [SearchManager] CNKI {len(papers)} 篇")
            return papers, warnings, True
        except Exception as e:
            print(f"  [SearchManager] CNKI 异常: {e}")
            warnings.append({
                "code": "source_unavailable",
                "sources": ["cnki"],
                "reason": "error",
                "detail": f"CNKI 检索异常：{type(e).__name__}: {str(e)[:200]}",
            })
            return [], warnings, False

    # ---------- 辅助方法 ----------
    def _extract_english_terms(self, query: str) -> list[str]:
        words = re.findall(r'[a-zA-Z]{2,}', query)
        return list(set(w.lower() for w in words if len(w) >= 2))

    def _has_chinese(self, query: str) -> bool:
        return bool(re.search(r'[\u4e00-\u9fff]', query))

    def _merge_results(self, results1: list, results2: list) -> list:
        seen = {}
        merged = []
        for p in results1 + results2:
            # 使用 Unicode 兼容的 key：保留所有字母数字（含中文）
            title_key = re.sub(r'[^\w]', '', (p.title or "").lower())[:60]
            doi_key = re.sub(r'[^\w]', '', (p.doi or "").lower())[:40]
            key = doi_key if doi_key else title_key
            if not key:
                continue
            if key not in seen:
                seen[key] = len(merged)
                merged.append(p)
            else:
                existing = merged[seen[key]]
                # 远程结果经常只有标题/DOI；SQLite 中的历史记录可能已有完整
                # 摘要、作者和链接。逐字段合并，避免把完整本地记录丢掉后，
                # 又对同一篇论文发起跨源元数据补齐请求。
                scalar_fields = (
                    "abstract", "venue", "year", "url", "citations", "pdf_url",
                    "oa_url", "pub_date", "pub_type",
                )
                for field_name in scalar_fields:
                    current = getattr(existing, field_name, None)
                    incoming = getattr(p, field_name, None)
                    if not current and incoming:
                        setattr(existing, field_name, incoming)
                list_fields = ("authors", "keywords", "fields_of_study", "subjects")
                for field_name in list_fields:
                    current = getattr(existing, field_name, None)
                    incoming = getattr(p, field_name, None)
                    if not current and incoming:
                        setattr(existing, field_name, list(incoming))
                if getattr(p, "metadata_provenance", None):
                    existing.metadata_provenance.update(p.metadata_provenance)
        return merged

    async def _safe_search(
        self, engine, query: str, limit: int, name: str,
        timeout_sec: int = 0, date_from: str | None = None,
        title_only: bool = False,
    ) -> list:
        t0 = time.time()
        diag = {"status": "failed", "raw_count": 0, "elapsed_seconds": None}
        self._emit_progress({
            "stage": "source",
            "detail": f"{name} 开始检索",
            "source": name,
            "source_status": "started",
        })
        try:
            if title_only:
                coro = engine.search(query, limit, date_from=date_from, title_only=True)
            else:
                coro = engine.search(query, limit, date_from=date_from)
            if timeout_sec > 0:
                results = await asyncio.wait_for(coro, timeout=timeout_sec)
            else:
                results = await coro
            elapsed = round(time.time() - t0, 2)
            diag.update({"status": "ok", "raw_count": len(results), "elapsed_seconds": elapsed})
            if elapsed > 5:
                print(f"  [Timing] ⚠️ {name}: {elapsed}s ({len(results)}篇) — 慢源")
            else:
                print(f"  [Timing] {name}: {elapsed}s ({len(results)}篇)")
            self._last_search_diagnostics[name] = diag
            self._emit_progress({
                "stage": "source",
                "detail": f"{name} 完成：{len(results)} 篇，耗时 {elapsed:.2f} 秒",
                "source": name,
                "source_status": "ok",
                "result_count": len(results),
                "elapsed_seconds": elapsed,
            })
            return results
        except asyncio.TimeoutError:
            diag.update({"status": "timeout", "error": f"超过 {timeout_sec}s"})
            self._last_search_diagnostics[name] = diag
            print(f"  [Timing] {name}: >{timeout_sec}s (超时跳过)")
            self._emit_progress({
                "stage": "source",
                "detail": f"{name} 超时：等待超过 {timeout_sec} 秒，已跳过",
                "source": name,
                "source_status": "timeout",
                "elapsed_seconds": timeout_sec,
            })
            return []
        except Exception as e:
            response = getattr(e, "response", None)
            if response is not None:
                body = (getattr(response, "text", "") or "").replace("\n", " ")[:300]
                detail = f"HTTP {response.status_code}: {body}"
                retry_after = response.headers.get("retry-after")
                if retry_after:
                    detail += f" (Retry-After: {retry_after})"
            else:
                detail = f"{type(e).__name__}: {str(e)[:200]}"
            elapsed = round(time.time() - t0, 2)
            diag.update({"status": "error", "error": detail, "elapsed_seconds": elapsed})
            self._last_search_diagnostics[name] = diag
            print(f"  [Timing] {name}: {elapsed}s (失败: {detail})")
            self._emit_progress({
                "stage": "source",
                "detail": f"{name} 失败，耗时 {elapsed:.2f} 秒：{detail}",
                "source": name,
                "source_status": "error",
                "elapsed_seconds": elapsed,
            })
            return []

    async def _search_remote(
        self,
        query: str,
        max_per_source: int,
        s2_api_key: str,
        enabled_sources: set[str] | None = None,
        date_from: str | None = None,
    ) -> tuple[list, dict]:
        """远程多源检索。返回 (deduped_papers, audit)。

        audit = {"attempted": set[str], "unavailable": set[str], "warnings": list[dict]}
        —— attempted 是本次真的发过请求的源；unavailable 是白名单里要了、但引擎在
        config 里被关掉（或压根没有实现）而没发请求的源。上层据此如实报告，
        避免"要了 A 却拿了 B"。

        crossref 的检索模式从 `self._crossref_query_mode` 读（`search_all` 在调用
        前写入）—— 刻意不做成参数：这个签名是框架层测试替身合同的基准面，加参数
        会让边界外的测试跟着漂。缺省按 "full" 处理，直接调本方法的旧调用方行为不变。
        """
        crossref_query_mode = getattr(self, "_crossref_query_mode", "full")
        from .config import S2_FALLBACK_CROSSREF_MULTIPLIER
        results = []
        s2_failed = False
        # 每次远程检索独立记录各引擎的请求状态，避免把“失败”伪装成“零匹配”。
        self._last_search_diagnostics = {}
        attempted: set[str] = set()
        unavailable: set[str] = set()
        audit_warnings: list[dict] = []
        limits = httpx.Limits(max_connections=30, max_keepalive_connections=10)
        # 与 search_all 一致：只有 None 才用缺省；空集合就是"一个远程源都不搜"。
        # （老写法 `enabled_sources or {...}` 会把空集合当 falsy → 全开，见 issue #230）
        if enabled_sources is None:
            enabled_sources = set(DEFAULT_ENABLED_SOURCES)
        else:
            enabled_sources = {canonical_source(s) for s in enabled_sources if canonical_source(s)}
        async with httpx.AsyncClient(limits=limits, timeout=httpx.Timeout(30.0)) as client:
            source_limits = self._per_source_limits or PER_SOURCE_LIMITS
            arxiv_n = source_limits.get("arxiv", max_per_source)
            biorxiv_n = source_limits.get("biorxiv", max_per_source)
            medrxiv_n = source_limits.get("medrxiv", max_per_source)
            s2_n = source_limits.get("semantic_scholar", max_per_source)
            pubmed_n = source_limits.get("pubmed", max_per_source)
            cr_n = source_limits.get("crossref", max_per_source)
            oa_n = source_limits.get("openalex", max_per_source)
            arxiv = ArxivSearch(client)
            biorxiv = EuropePmcPreprintSearch(client, "biorxiv")
            medrxiv = EuropePmcPreprintSearch(client, "medrxiv")
            s2 = SemanticScholarSearch(client, s2_api_key)
            pubmed = PubMedSearch(client)
            crossref = CrossrefSearch(client)
            openalex = OpenAlexSearch(client)
            google_scholar = GoogleScholarSearch(client)
            
            # (canonical 源名, 引擎, limit, 日志名, 超时秒)
            remote_specs = [
                ("arxiv", arxiv, arxiv_n, "arXiv", self._arxiv_timeout_seconds),
                ("biorxiv", biorxiv, biorxiv_n, "bioRxiv", 15),
                ("medrxiv", medrxiv, medrxiv_n, "medRxiv", 15),
                ("semantic_scholar", s2, s2_n, "S2", 0),
                ("pubmed", pubmed, pubmed_n, "PubMed", 20),
                ("crossref", crossref, cr_n, "CrossRef", 0),
                ("openalex", openalex, oa_n, "OpenAlex", 0),
                ("google_scholar", google_scholar, arxiv_n, "GoogleScholar", 15),
            ]
            task_specs = []
            for name, engine, limit_n, log_name, tmo in remote_specs:
                if name not in enabled_sources:
                    continue
                if not SEARCH_ENGINES.get(name, {}).get("enabled", False):
                    # 白名单要了这个源，但它在 config 里被关掉（如 pubmed 缺 API key）。
                    # 如实记为 unavailable，而不是让上层以为搜过了。
                    unavailable.add(name)
                    audit_warnings.append({
                        "code": "source_unavailable",
                        "sources": [name],
                        "reason": "disabled_in_config",
                        "detail": f"{name} 在 config.SEARCH_ENGINES 中 enabled=False，本次未检索。",
                    })
                    continue
                attempted.add(name)
                safe_kwargs = {"timeout_sec": tmo}
                if date_from is not None:
                    safe_kwargs["date_from"] = date_from
                if name == "crossref" and crossref_query_mode == "title":
                    safe_kwargs["title_only"] = True
                task_specs.append((name, self._safe_search(engine, query, limit_n, log_name, **safe_kwargs)))
            if not task_specs:
                return [], {"attempted": attempted, "unavailable": unavailable, "warnings": audit_warnings,
                            "source_diagnostics": dict(self._last_search_diagnostics),
                            "raw_source_counts": {}, "deduped_source_counts": {}}

            all_results = await asyncio.gather(*[coro for _, coro in task_specs], return_exceptions=True)
            by_source = dict(zip([name for name, _ in task_specs], all_results))
            # 定向查证的 dual 模式自动补一条 Crossref 题名召回；普通 full 模式
            # 只有一条主题查询，避免改变学术搜索和 E2E 的默认行为。
            if crossref_query_mode == "dual" and "crossref" in enabled_sources:
                title_result = await self._safe_search(
                    crossref, query, cr_n, "CrossRef-title", timeout_sec=0,
                    date_from=date_from, title_only=True,
                )
                for paper in title_result:
                    paper.metadata_provenance["crossref_query_mode"] = "title"
                by_source["crossref_title"] = title_result
            s2_result = by_source.get("semantic_scholar", [])
            if isinstance(s2_result, Exception) or (isinstance(s2_result, list) and len(s2_result) == 0):
                s2_failed = True
            
            for r in all_results:
                if isinstance(r, list):
                    results.extend(r)
            if isinstance(by_source.get("crossref_title"), list):
                results.extend(by_source["crossref_title"])
            
            seen = {}  # key -> index in deduped
            deduped = []
            for p in results:
                title_key = re.sub(r'[^a-zA-Z0-9]', '', (p.title or "").lower())[:60]
                doi_key = re.sub(r'[^a-zA-Z0-9]', '', (p.doi or "").lower())[:40]
                key = doi_key if doi_key else title_key
                if not key:
                    continue
                if key not in seen:
                    seen[key] = len(deduped)
                    deduped.append(p)
                else:
                    # 合并字段：保留有值的版本
                    existing = deduped[seen[key]]
                    if not existing.venue and p.venue:
                        existing.venue = p.venue
                    if not existing.abstract and p.abstract:
                        existing.abstract = p.abstract
                    if not existing.year and p.year:
                        existing.year = p.year
            
            # 去重后回填：有 DOI 但无 venue 的论文，尝试从 CrossRef 查
            #
            # #230 复核残留（qinp 2026-07-30）：外层 search_all 的回填已按白名单
            # gate 住了，但**这条内层路径**当时漏了 —— crossref 禁用时它照发
            # `_fill_venue_by_doi`，审计里 attempted_sources=["arxiv"] 却真打了
            # api.crossref.org，审计与事实不符。同一个 gate 必须两处都有。
            _enrich_ok = is_source_allowed("crossref", enabled_sources)
            if not _enrich_ok and any(p.doi and not p.venue for p in deduped):
                audit_warnings.append({
                    "code": "enrichment_skipped",
                    "sources": ["crossref"],
                    "detail": "Crossref 不在白名单内，缺失 venue 的元数据未做 DOI 回填。",
                })
            fill_tasks = []
            if _enrich_ok:
                for p in deduped:
                    if p.doi and not p.venue:
                        fill_tasks.append(self._fill_venue_by_doi(p))
            if fill_tasks:
                await asyncio.gather(*fill_tasks)
                attempted.add("crossref")   # 真打了就如实记进审计
            
            # S2 挂了的补偿检索：**只能用白名单内的源**。原来无条件拿 crossref +
            # openalex 补偿，等于给被禁用的源发真实请求、并把它们的结果写进 papers.db
            # —— 下次别的调用从本地索引又把这些禁用源捞出来（issue #230 的二级传播路径）。
            if s2_failed and "semantic_scholar" in enabled_sources:
                comp_engines = {}
                if "crossref" in enabled_sources:
                    comp_engines["crossref"] = crossref
                if "openalex" in enabled_sources:
                    comp_engines["openalex"] = openalex
                if comp_engines:
                    asyncio.create_task(
                        self._run_compensation(comp_engines, query, cr_n, oa_n)
                    )

            print(f"  [SearchManager] 远程检索: {len(results)} 篇, 去重后: {len(deduped)} 篇")
            # 出口再兜一层：引擎万一给错 source（笔误 / 新引擎复制粘贴），也不放过去
            deduped, _remote_dropped = filter_papers_by_sources(deduped, enabled_sources)
            if _remote_dropped:
                print(f"  [SearchManager] ⚠️ 远程结果含白名单外来源，已丢弃: {_remote_dropped}")
            source_counts = {}
            for p in deduped:
                src = canonical_source(getattr(p, "source", "")) or "<empty>"
                source_counts[src] = source_counts.get(src, 0) + 1
            # 统一为 canonical source 名，并明确区分 no_matches 与请求失败。
            name_to_source = {"arXiv": "arxiv", "bioRxiv": "biorxiv",
                              "medRxiv": "medrxiv", "S2": "semantic_scholar",
                              "PubMed": "pubmed", "CrossRef": "crossref",
                              "OpenAlex": "openalex",
                              "GoogleScholar": "google_scholar"}
            diagnostics = {}
            for name, diag in self._last_search_diagnostics.items():
                src = name_to_source.get(name, canonical_source(name))
                diagnostics[src] = dict(diag)
                if diag.get("status") == "ok" and diag.get("raw_count", 0) == 0:
                    audit_warnings.append({"code": "source_no_matches", "sources": [src],
                                           "detail": "请求成功但返回零篇；不是请求异常。"})
                elif diag.get("status") in {"error", "timeout"}:
                    audit_warnings.append({"code": "source_request_failed", "sources": [src],
                                           "reason": diag.get("status"),
                                           "detail": diag.get("error", "")})
            for src, count in source_counts.items():
                diagnostics.setdefault(src, {}).update({"deduped_count": count})
            return deduped, {
                "attempted": attempted,
                "unavailable": unavailable,
                "warnings": audit_warnings,
                "source_diagnostics": diagnostics,
                "raw_source_counts": {src: d.get("raw_count", 0) for src, d in diagnostics.items()},
                "deduped_source_counts": source_counts,
            }

    async def _enrich_metadata_cross_source(self, papers: list[Paper], enabled_sources: set[str]) -> None:
        """按 DOI 从 Crossref/OpenAlex/PubMed/OpenAIRE/S2 互补缺失元数据。"""
        targets = [p for p in papers if p.doi and any(
            not getattr(p, f, "") for f in ("abstract", "authors", "venue", "year", "url")
        )]
        if not targets:
            return
        enrichment_started = time.monotonic()
        self._emit_progress({
            "stage": "metadata_enrichment",
            "detail": f"开始跨源补齐 {len(targets)} 篇论文的元数据",
            "completed_count": 0,
            "target_count": len(targets),
            "elapsed_seconds": 0.0,
        })
        async with httpx.AsyncClient(timeout=10) as client:
            # 不同论文之间原本已经并行；这里再让单篇论文的快速元数据源并行。
            # 各源使用独立并发阈值，既消除串行等待，也避免短时间请求洪峰。
            source_semaphores = {
                "crossref": asyncio.Semaphore(6),
                "openalex": asyncio.Semaphore(10),
                "pubmed": asyncio.Semaphore(3),
                "openaire": asyncio.Semaphore(10),
                "semantic_scholar": asyncio.Semaphore(1),
            }

            async def fetch_fast_source(source: str, doi: str) -> dict | None:
                try:
                    async with source_semaphores[source]:
                        if source == "crossref":
                            r = await client.get(f"https://api.crossref.org/works/{doi}")
                            if r.status_code != 200:
                                return None
                            item = r.json().get("message", {})
                            return {
                                "abstract": item.get("abstract", ""),
                                "authors": [
                                    f"{a.get('given', '')} {a.get('family', '')}".strip()
                                    for a in item.get("author", [])
                                ],
                                "venue": (item.get("container-title") or [""])[0]
                                or item.get("publisher", ""),
                                "year": (
                                    (
                                        item.get("published-print")
                                        or item.get("published-online")
                                        or item.get("issued")
                                        or {}
                                    ).get("date-parts")
                                    or [[None]]
                                )[0][0],
                                "url": item.get("URL", ""),
                            }
                        if source == "openalex":
                            r = await client.get(
                                f"https://api.openalex.org/works/https://doi.org/{doi}"
                            )
                            if r.status_code != 200:
                                return None
                            item = r.json()
                            words = [
                                (position, word)
                                for word, positions in (
                                    item.get("abstract_inverted_index") or {}
                                ).items()
                                for position in positions
                            ]
                            primary = item.get("primary_location") or {}
                            return {
                                "abstract": " ".join(
                                    word for _, word in sorted(words)
                                ),
                                "authors": [
                                    a.get("author", {}).get("display_name", "")
                                    for a in item.get("authorships", [])
                                ],
                                "venue": (primary.get("source") or {}).get(
                                    "display_name", ""
                                ),
                                "year": item.get("publication_year"),
                                "url": item.get("doi") or item.get("id", ""),
                            }
                        if source == "pubmed":
                            matches = await PubMedSearch(client).search(
                                f"{doi}[doi]", limit=1
                            )
                            if not matches:
                                return None
                            match = matches[0]
                            return {
                                "abstract": match.abstract,
                                "authors": match.authors,
                                "venue": match.venue,
                                "year": match.year,
                                "url": match.url,
                            }
                        if source == "openaire":
                            response = await client.get(
                                "https://api.openaire.eu/search/publications",
                                params={"doi": doi, "format": "json", "size": 3},
                                timeout=8,
                            )
                            if response.status_code != 200:
                                return None
                            return {
                                "abstract": _openaire_abstract(
                                    response.json(), doi
                                )
                            }

                        await SemanticScholarSearch._wait_for_request_slot()
                        r = await client.get(
                            f"https://api.semanticscholar.org/graph/v1/paper/DOI:{doi}",
                            params={
                                "fields": "title,authors,year,venue,abstract,url"
                            },
                            headers={
                                "x-api-key": os.environ.get(
                                    "SEMANTIC_SCHOLAR_API_KEY", ""
                                )
                            },
                        )
                        if r.status_code != 200:
                            return None
                        item = r.json()
                        return {
                            "abstract": item.get("abstract", ""),
                            "authors": [
                                a.get("name", "") for a in item.get("authors", [])
                            ],
                            "venue": item.get("venue", ""),
                            "year": item.get("year"),
                            "url": item.get("url", ""),
                        }
                except Exception:
                    return None

            async def one(p: Paper) -> None:
                doi = p.doi.strip()
                fields = ("abstract", "authors", "venue", "year", "url")

                def missing() -> set[str]:
                    return {f for f in fields if not getattr(p, f, "")}

                # 快速来源并发请求，但按固定优先级合并，网络返回顺序不会改变
                # 最终 index。只填缺失字段，永不覆盖检索源已给出的元数据。
                fast_sources = [
                    source
                    for source in (
                        "crossref",
                        "openalex",
                        "pubmed",
                        "openaire",
                        "semantic_scholar",
                    )
                    if source in enabled_sources
                ]
                fast_results = await asyncio.gather(
                    *(fetch_fast_source(source, doi) for source in fast_sources)
                )
                for source, data in zip(fast_sources, fast_results):
                    if not data:
                        continue
                    for field, value in data.items():
                        if value and not getattr(p, field, ""):
                            setattr(p, field, value)
                            p.metadata_provenance[field] = source

                # DOI 落地页仅作最后的官方元数据补齐；普通 description 不算摘要，
                # 只接受 citation_abstract / DC / PRISM 等明确摘要标签。
                if "publisher_landing" in enabled_sources and not p.abstract:
                    try:
                        # 落地页是最慢、最不稳定的兜底。httpx 的 timeout 是分阶段
                        # 超时；外层 wait_for 才能保证整次跳转+读取的墙钟时间不超过5秒。
                        response = await asyncio.wait_for(
                            client.get(
                                f"https://doi.org/{doi}",
                                follow_redirects=True,
                                timeout=5,
                                headers={
                                    "User-Agent": (
                                        "Mozilla/5.0 (X11; Linux x86_64) "
                                        "AppleWebKit/537.36 Chrome/124 Safari/537.36"
                                    ),
                                    "Accept": "text/html,application/xhtml+xml",
                                },
                            ),
                            timeout=5,
                        )
                        if response.status_code == 200:
                            abstract = _publisher_landing_abstract(response.text)
                            if abstract:
                                p.abstract = abstract
                                p.metadata_provenance["abstract"] = "publisher_landing"
                    except Exception:
                        pass

                if "oa_pdf_discovery" in enabled_sources and not p.abstract:
                    try:
                        abstract, pdf_url = await asyncio.wait_for(
                            _discover_pdf_abstract(client, p), timeout=5
                        )
                        if abstract:
                            p.abstract = abstract
                            p.metadata_provenance["abstract"] = "oa_pdf"
                            if pdf_url and not p.oa_url:
                                p.oa_url = pdf_url
                            p.is_oa = True
                    except Exception:
                        pass
            # 单篇或整批补全失败不能拖住整次检索。
            default_paper_timeout = "30" if "oa_pdf_discovery" in enabled_sources else "20"
            per_paper_timeout = float(os.environ.get("LITERATURE_METADATA_PAPER_TIMEOUT", default_paper_timeout))
            overall_timeout = float(os.environ.get("LITERATURE_METADATA_TIMEOUT", "120"))

            completed_count = 0

            async def guarded(p: Paper) -> None:
                nonlocal completed_count
                try:
                    await asyncio.wait_for(one(p), timeout=per_paper_timeout)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    pass
                except Exception:
                    pass
                finally:
                    completed_count += 1
                    if completed_count == len(targets) or completed_count % 10 == 0:
                        elapsed = round(time.monotonic() - enrichment_started, 2)
                        self._emit_progress({
                            "stage": "metadata_enrichment",
                            "detail": (
                                f"跨源元数据补齐 {completed_count}/{len(targets)}，"
                                f"已耗时 {elapsed:.2f} 秒"
                            ),
                            "completed_count": completed_count,
                            "target_count": len(targets),
                            "elapsed_seconds": elapsed,
                        })

            try:
                await asyncio.wait_for(
                    asyncio.gather(*(guarded(p) for p in targets)),
                    timeout=overall_timeout,
                )
            except asyncio.TimeoutError:
                # 保留已经补齐的部分，继续评分和落盘。
                elapsed = round(time.monotonic() - enrichment_started, 2)
                self._emit_progress({
                    "stage": "metadata_enrichment",
                    "detail": (
                        f"跨源元数据补齐达到 {overall_timeout:.0f} 秒上限："
                        f"已完成 {completed_count}/{len(targets)}，继续后续流程"
                    ),
                    "completed_count": completed_count,
                    "target_count": len(targets),
                    "elapsed_seconds": elapsed,
                    "source_status": "timeout",
                })
                return

    async def _fill_venue_by_doi(self, p) -> None:
        """有 DOI 但无 venue 的论文，从 CrossRef DOI 回填 venue"""
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(f"https://api.crossref.org/works/{p.doi}")
                if resp.status_code == 200:
                    item = resp.json()["message"]
                    ct = item.get("container-title", [])
                    if ct and ct[0]:
                        p.venue = ct[0]
                    else:
                        event = item.get("event", {}) or {}
                        if event:
                            p.venue = event.get("name", "")
                        else:
                            pub = item.get("publisher") or ""
                            if pub:
                                p.venue = pub
        except Exception:
            pass

    def _generate_query_variants_simple(self, query: str) -> list[str]:
        """简单查询变体生成（替代不小心被删除的 _generate_query_variants）"""
        words = query.split()
        if len(words) <= 3:
            return [query]
        return [query, " ".join(words[:3])] if len(words) > 3 else [query]

    async def _run_compensation(self, comp_engines: dict, query, cr_n, oa_n):
        """S2 失败时的后台补偿检索（结果只沉淀进本地索引，不进本次返回）。

        comp_engines 由调用方按白名单过滤后传入（{'crossref': engine, ...}）：
        补偿不是"绕过白名单去别的源捞"的后门 —— 禁用的源既不发请求，也不许写进 papers.db。
        """
        try:
            from .config import S2_FALLBACK_CROSSREF_MULTIPLIER
            variants = self._generate_query_variants_simple(query)
            for variant in variants[:2]:
                comp_tasks = []
                if "crossref" in comp_engines:
                    comp_tasks.append(self._safe_search(
                        comp_engines["crossref"], variant,
                        cr_n * S2_FALLBACK_CROSSREF_MULTIPLIER // 2, "CrossRef-comp",
                    ))
                if "openalex" in comp_engines:
                    comp_tasks.append(self._safe_search(
                        comp_engines["openalex"], variant, oa_n // 2, "OpenAlex-comp",
                    ))
                if not comp_tasks:
                    return
                comp_results = await asyncio.gather(*comp_tasks, return_exceptions=True)
                for r in comp_results:
                    if isinstance(r, list) and self.local_index:
                        allowed, _ = filter_papers_by_sources(r, set(comp_engines))
                        if allowed:
                            self.local_index.add_papers(allowed, query)
                break
        except Exception as e:
            print(f"  [Compensation] 补偿搜索失败: {e}")

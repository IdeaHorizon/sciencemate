"""从外部源抓资讯 —— 只搬运，不创作。

## provenance-first

每个采集器返回的 `CollectedItem` 都必须带 `url`（出处）和一个算得出来的
`canonical_key`。摘要一律是**从抓到的原文截取**，没有任何一步让模型"根据
印象写一条新闻"。

理由不是洁癖：平台的全部价值建立在可审计上。一条没有出处的资讯，读者无从
分辨它是真有这件事、还是某一层的幻觉 —— 而在一个科研工具里，这种东西会被
当成事实带进研究。

## 为什么不引 feedparser

RSS/Atom 的解析用标准库就够（下面 120 行）。而外部依赖在这个仓库有实际
代价：CI 依赖烘在基础镜像里，加一个包要重建镜像，否则每个 job 现装。

## XML 是敌意输入

源在我们控制之外。`xml.etree` 对 billion-laughs 类的实体炸弹是脆弱的
（见 Python 文档的 XML 漏洞表），所以解析前先机械拒绝带 DTD/ENTITY 声明的
文档 —— 正常的 feed 不需要它们。
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import quote_plus
from xml.etree import ElementTree

import httpx

from app.services.feed import canonical

logger = logging.getLogger(__name__)

#: 单个源一次响应的上限。真实 feed 都在几百 KB 量级；超过这个数不是 feed，
#: 是别的东西。
MAX_RESPONSE_BYTES = 8 * 1024 * 1024

#: 单次抓取的超时。源慢不该拖住整轮采集。
FETCH_TIMEOUT_SECONDS = 30.0

#: 摘要截断长度。卡片上本来也放不下更多，而整段摘要会把库撑大。
SUMMARY_MAX_CHARS = 1200

#: 自报家门。匿名爬别人的接口既不礼貌，出问题时对方也找不到人。
USER_AGENT = (
    "IEIT-Research-Platform-Feed/1.0 "
    "(+research feed aggregator; contact: platform administrator)"
)

_ATOM = "{http://www.w3.org/2005/Atom}"
_ARXIV_NS = "{http://arxiv.org/schemas/atom}"
#: RSS 1.0（RDF）—— Nature / Science / APS 这批期刊用的就是它，不是 RSS 2.0。
_RDF = "{http://www.w3.org/1999/02/22-rdf-syntax-ns#}"
_RSS1 = "{http://purl.org/rss/1.0/}"
_DC = "{http://purl.org/dc/elements/1.1/}"
_PRISM = "{http://prismstandard.org/namespaces/basic/2.0/}"
_CONTENT = "{http://purl.org/rss/1.0/modules/content/}"

# 可信编辑型科研资讯。它们不是期刊论文，不能走期刊分区准入；
# 同时只允许明确列出的编辑源进入 bignews 探索位，避免任意 RSS 伪装。
TRUSTED_SCIENCE_NEWS_VENUES = frozenset({
    "Quanta Magazine", "Phys.org", "ScienceDaily", "MIT Technology Review",
})

#: 带 DTD 或实体声明的 XML 一律拒绝（实体炸弹的载体）。
_DTD_MARKER = re.compile(rb"<!\s*(DOCTYPE|ENTITY)", re.IGNORECASE)

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


class CollectorError(RuntimeError):
    """这个源这一轮没抓成 —— 由调度器计入连败。"""


@dataclass(slots=True)
class CollectedItem:
    """一条抓到的原料。字段全部来自源，没有一处是推断出来的。"""

    canonical_key: str
    kind: str
    title: str
    url: str
    summary: str = ""
    authors: list[str] = field(default_factory=list)
    venue: str = ""
    published_at: datetime | None = None
    domains: list[str] = field(default_factory=list)
    image_url: str = ""
    extra: dict = field(default_factory=dict)


def _clean_text(raw: str | None) -> str:
    """去标签、压空白。RSS 的 description 里常带 HTML。"""
    if not raw:
        return ""
    return _WS_RE.sub(" ", _TAG_RE.sub(" ", raw)).strip()


def _summary(raw: str | None) -> str:
    text = _clean_text(raw)
    if len(text) <= SUMMARY_MAX_CHARS:
        return text
    # 截在词边界上，末尾留省略号，别把一个词劈开。
    return text[:SUMMARY_MAX_CHARS].rsplit(" ", 1)[0] + "…"


def _parse_datetime(raw: str | None) -> datetime | None:
    """接受 ISO8601 与 RFC822 两种写法 —— Atom 用前者，RSS 2.0 用后者。

    解析不出来返回 None，**不拿"现在"顶替**：把抓取时刻当成发表时刻，会让
    一篇三年前的文章在排序里表现得像今天刚发的。
    """
    text = (raw or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except ValueError:
        pass
    try:
        parsed = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if parsed is None:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _parse_xml(raw: bytes) -> ElementTree.Element:
    if _DTD_MARKER.search(raw[:4096]):
        raise CollectorError("feed declares a DTD or entity; refusing to parse")
    try:
        return ElementTree.fromstring(raw)
    except ElementTree.ParseError as exc:
        raise CollectorError(f"malformed XML: {exc}") from exc


async def _fetch(client: httpx.AsyncClient, url: str) -> bytes:
    try:
        response = await client.get(url)
    except httpx.HTTPError as exc:
        raise CollectorError(f"{type(exc).__name__}: {exc}") from exc
    if response.status_code >= 400:
        raise CollectorError(f"HTTP {response.status_code} from {url}")
    body = response.content
    if len(body) > MAX_RESPONSE_BYTES:
        raise CollectorError(f"response is {len(body)} bytes, over the {MAX_RESPONSE_BYTES} cap")
    return body


#: 各家 feed 放图的四种地方。顺序即优先级：显式的缩略图字段比正文里第一张
#: `<img>` 可靠（正文里第一张常常是社交图标或者出版商 logo）。
_MEDIA_NS = "{http://search.yahoo.com/mrss/}"
_IMAGE_EXT = re.compile(r"\.(png|jpe?g|gif|webp|avif)(\?|$)", re.IGNORECASE)
_INLINE_IMG = re.compile(r"<img[^>]+src=[\"']([^\"']+)[\"']", re.IGNORECASE)


def _extract_image(node: ElementTree.Element, body: str = "") -> str:
    """从一个 feed 条目里找配图。找不到返回空字符串 —— **不编一张**。

    没有图是常态（Nature 系、arXiv、bioRxiv 全都没有），所以返回空是正常路径，
    不是失败。给一条没图的内容配一张"通用科学插画"是在伪造信息。

    ## ⚠️ 不要写 `(node.find(...) or {}).get("url")`

    第一版就是那么写的，对 PRX（正文内联 `<img>`）有效，对 Phys.org 与 Quanta
    **全部返回空** —— 而它们恰恰是图片覆盖率最高的两个源。

    原因是 ElementTree 的一个陷阱：**Element 的真值取决于它有没有子节点**
    （`__bool__` 返回 `len(self) != 0`）。`<media:thumbnail url="…"/>` 是自闭合
    的、零子节点，于是 `element or {}` 求值成 `{}`，`.get("url")` 恒为 None。

    写法看着完全合理，语法、类型、单测全过 —— 只有喂真 feed 才会发现一整类源
    的图全丢了。所以这里一律显式判 `is not None`。
    """
    for tag in (f"{_MEDIA_NS}thumbnail", f"{_MEDIA_NS}content", "enclosure"):
        element = node.find(tag)
        if element is None:
            continue
        url = (element.get("url") or "").strip()
        if url.startswith(("http://", "https://")):
            return url

    # 正文里的第一张图。要求扩展名像图片 —— 追踪像素与 1x1 计数器通常没有。
    match = _INLINE_IMG.search(body or "")
    if match:
        url = match.group(1).strip()
        if url.startswith("//"):
            url = "https:" + url
        if url.startswith(("http://", "https://")) and _IMAGE_EXT.search(url):
            return url
    return ""


#: 落地页 `og:image` 的两种写法（属性在前 / content 在前）。
_OG_IMAGE = (
    re.compile(r"""<meta[^>]+(?:property|name)=["']og:image["'][^>]*content=["']([^"']+)""", re.I),
    re.compile(r"""<meta[^>]+content=["']([^"']+)["'][^>]*(?:property|name)=["']og:image["']""", re.I),
)

#: 落地页只读前面这些字节 —— `og:image` 一定在 `<head>` 里，而整页可能几 MB。
_OG_SCAN_BYTES = 200_000


async def fetch_og_image(client: httpx.AsyncClient, url: str) -> str:
    """从条目的落地页里取 `og:image`。取不到返回空字符串。

    ## 为什么这是**按源开启**的，不是所有源都做

    打真源查过（2026-08-23），两家的结果正好相反：

      - `nature.com/articles/…` 的 og:image 是**文章里的真实配图**
        （`media.springernature.com/…art%3A10.1038%2F…`）；
      - `arxiv.org/abs/…` 的 og:image 是 **arXiv 自己的 logo**。

    对 arXiv 做这件事，等于给每一篇论文配上同一张 arXiv logo —— 那不是"有图"，
    是拿装饰冒充内容，比留空更糟。而"og:image 是真图还是台标"是**出版商的
    属性**，检测不出来（同一张 logo 要看过好几条才认得出）。所以交给源上的
    `enrich_image` 显式声明，谁开谁负责。

    ## 为什么只对新条目做

    每条一次 HTTP。一个 8 条的期刊源每 12 小时全量重抓一遍，就是每天 16 次
    对出版商的无谓请求，而其中 15 次拿回的是同一批已经存过的图。
    """
    try:
        response = await client.get(url)
    except httpx.HTTPError:
        return ""
    if response.status_code >= 400:
        return ""
    head = response.content[:_OG_SCAN_BYTES].decode("utf-8", errors="replace")
    for pattern in _OG_IMAGE:
        match = pattern.search(head)
        if match:
            candidate = match.group(1).strip()
            if candidate.startswith("//"):
                candidate = "https:" + candidate
            if candidate.startswith(("http://", "https://")):
                return candidate
    return ""


def make_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=FETCH_TIMEOUT_SECONDS,
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT},
    )


# ── arXiv ────────────────────────────────────────────────────────────────────


async def collect_arxiv(client: httpx.AsyncClient, config: dict) -> list[CollectedItem]:
    """arXiv 官方 Atom API，按分类取最新。

    分类直接来自域注册表的骨架词表，所以抓回来的条目自带的分类**就是**我们
    用来送达的域 —— 两边不需要任何映射表（这正是当初骨架抄 arXiv 的收益）。
    """
    category = str(config.get("category") or "").strip()
    if not category:
        raise CollectorError("arxiv source needs a `category`")
    max_results = min(int(config.get("max_results") or 30), 100)
    url = (
        "http://export.arxiv.org/api/query"
        f"?search_query=cat:{category}"
        f"&sortBy=submittedDate&sortOrder=descending&max_results={max_results}"
    )
    return _arxiv_entries(_parse_xml(await _fetch(client, url)))


def _arxiv_entries(root: ElementTree.Element) -> list[CollectedItem]:
    """arXiv Atom 的条目解析 —— **按分类浏览**与**按检索词搜**共用这一份。

    两条路径拿回来的是同一种文档。抄两份解析就是两个会各自演化的答案，
    而分叉时两边都照常返回结果，没有任何一层会报错。
    """
    items: list[CollectedItem] = []
    for entry in root.findall(f"{_ATOM}entry"):
        entry_id = (entry.findtext(f"{_ATOM}id") or "").strip()
        title = _clean_text(entry.findtext(f"{_ATOM}title"))
        if not entry_id or not title:
            continue
        key = canonical.canonical_key(url=entry_id, text=entry_id)
        if not key:
            continue
        # 摘要页而不是 API 的 id URL：用户点开要看的是论文页面。
        abs_url = entry_id.replace("http://", "https://")
        domains = [
            term
            for element in entry.findall(f"{_ATOM}category")
            if (term := (element.get("term") or "").strip())
        ]
        primary = entry.find(f"{_ARXIV_NS}primary_category")
        if primary is not None and (term := (primary.get("term") or "").strip()):
            # 主分类排第一 —— 送达时它是这条内容"属于"哪儿。
            domains = [term] + [d for d in domains if d != term]
        items.append(
            CollectedItem(
                canonical_key=key,
                kind="paper",
                title=title,
                url=abs_url,
                summary=_summary(entry.findtext(f"{_ATOM}summary")),
                authors=[
                    name
                    for author in entry.findall(f"{_ATOM}author")
                    if (name := _clean_text(author.findtext(f"{_ATOM}name")))
                ],
                venue="arXiv",
                published_at=_parse_datetime(entry.findtext(f"{_ATOM}published")),
                domains=domains,
            )
        )
    return items


async def search_arxiv(
    client: httpx.AsyncClient, *, query: str, max_results: int = 15
) -> list[CollectedItem]:
    """按检索词在 arXiv 里搜 —— 自动挖掘"定向采集"的那一半。

    与 `collect_arxiv` 问的问题不同：那个问"这个分类今天发了什么"（浏览），
    这个问"哪些论文在讲这件事"（检索）。一个课题真正相关的工作经常发在他
    没订的分类里，只有检索捞得到。

    **检索由平台执行，不是给模型一个联网工具。** 模型只把课题描述翻成检索词
    （`op=feed_curate_profile`），发请求、限速、去重、落库都在这一层 ——
    这样出处始终攥在平台手里，也不给模型开一条出网的口子。
    """
    terms = " ".join((query or "").split())[:120]
    if not terms:
        raise CollectorError("search_arxiv needs a non-empty query")
    # 各词 **AND**，不是整串当词组、也不是默认的 OR。三种都打真源比过
    # （2026-08-22，检索词 "finite size scaling Ising"）：
    #
    #   all:"finite size scaling Ising"  → 0 条。没有论文会逐字出现这一整串。
    #   all:finite size scaling Ising    → 4 条，但头一条是《4DAnyone: Create
    #                                      Anyone in 4D from a Monocular Video》
    #                                      —— OR 命中某个常见词就算数，没用。
    #   all:finite AND all:size AND …    → 4 条，全部是 criticality / scaling
    #                                      方向的真相关工作。
    #
    # 光看文档推演会选错：我第一版按"加引号=词组更准"写，结果一条都搜不到。
    words = [w for w in terms.replace('"', " ").split() if w]
    if not words:
        raise CollectorError("search_arxiv needs at least one term")
    expression = " AND ".join(f"all:{word}" for word in words[:8])
    url = (
        "http://export.arxiv.org/api/query"
        f"?search_query={quote_plus(expression)}"
        "&sortBy=submittedDate&sortOrder=descending"
        f"&max_results={min(max_results, 50)}"
    )
    return _arxiv_entries(_parse_xml(await _fetch(client, url)))


# ── RSS / Atom ───────────────────────────────────────────────────────────────


async def collect_rss(client: httpx.AsyncClient, config: dict) -> list[CollectedItem]:
    """期刊 TOC —— RSS 2.0 / Atom / RSS 1.0(RDF) 三种都认。

    三种都要，是打真源打出来的：Nature、Nature Materials、Nature Machine
    Intelligence、Science、PRL **五个全是 RSS 1.0(RDF)**。只支持 RSS 2.0 和
    Atom 的话，顶刊这一路一条都进不来 —— 而在单元测试里喂一个自己造的 RSS
    2.0 样本，五条全绿。
    """
    url = str(config.get("url") or "").strip()
    if not url:
        raise CollectorError("rss source needs a `url`")
    venue = str(config.get("venue") or "").strip()
    root = _parse_xml(await _fetch(client, url))

    if root.tag == f"{_RDF}RDF":  # RSS 1.0
        items = _rdf_items(root, venue)
    else:
        channel = root.find("channel")
        if channel is not None:  # RSS 2.0
            items = _rss2_items(channel, venue)
        elif root.tag == f"{_ATOM}feed":  # Atom
            items = _atom_items(root, venue)
        else:
            raise CollectorError(
                f"{url} is not RSS 2.0, Atom, or RSS 1.0 (root tag {root.tag!r})"
            )

    content_kind = str(config.get("content_kind") or "").strip().lower()
    if content_kind == "news" or venue in TRUSTED_SCIENCE_NEWS_VENUES:
        for item in items:
            item.kind = "news"
            item.extra = {
                **item.extra,
                "feed_acquisition_routes": ["bignews"],
                "editorial_authority": 0.85,
            }
    return items


def _rdf_items(root: ElementTree.Element, venue: str) -> list[CollectedItem]:
    """RSS 1.0：`item` 是 RDF 根的直接子节点，不在 `channel` 里面。

    这批 feed 常常带 `prism:doi` —— 一个**已经解析好**的 DOI，比从正文里正则
    抠可靠得多。有它就用它。
    """
    items: list[CollectedItem] = []
    for node in root.findall(f"{_RSS1}item"):
        title = _clean_text(node.findtext(f"{_RSS1}title"))
        link = (node.findtext(f"{_RSS1}link") or node.get(f"{_RDF}about") or "").strip()
        body = (
            node.findtext(f"{_CONTENT}encoded")
            or node.findtext(f"{_RSS1}description")
            or ""
        )
        if not title:
            continue
        prism_doi = (node.findtext(f"{_PRISM}doi") or "").strip()
        key = canonical.canonical_key(url=link, guid=prism_doi, text=body)
        if not key:
            continue
        items.append(
            CollectedItem(
                canonical_key=key,
                kind="paper",
                title=title,
                url=link,
                summary=_summary(body),
                # dc:creator 在这些 feed 里是**多个同名元素**，一个作者一个。
                # `findtext` 只取第一个 —— 那会把合著者集体抹掉。
                authors=[
                    name
                    for element in node.findall(f"{_DC}creator")
                    if (name := _clean_text(element.text))
                ],
                venue=venue or _clean_text(node.findtext(f"{_PRISM}publicationName")),
                image_url=_extract_image(node, body),
                published_at=_parse_datetime(
                    node.findtext(f"{_DC}date") or node.findtext(f"{_PRISM}publicationDate")
                ),
            )
        )
    return items


def _rss2_items(channel: ElementTree.Element, venue: str) -> list[CollectedItem]:
    items: list[CollectedItem] = []
    for node in channel.findall("item"):
        title = _clean_text(node.findtext("title"))
        link = (node.findtext("link") or "").strip()
        guid = (node.findtext("guid") or "").strip()
        description = node.findtext("description") or ""
        if not title:
            continue
        # DOI 常常只出现在描述里，不在链接上 —— 所以正文一起参与算身份。
        key = canonical.canonical_key(url=link, guid=guid, text=description)
        if not key:
            continue
        items.append(
            CollectedItem(
                canonical_key=key,
                kind="paper",
                title=title,
                url=link or guid,
                summary=_summary(description),
                authors=_rss_authors(node),
                venue=venue,
                image_url=_extract_image(node, description),
                published_at=_parse_datetime(node.findtext("pubDate")),
            )
        )
    return items


def _rss_authors(node: ElementTree.Element) -> list[str]:
    raw = node.findtext("{http://purl.org/dc/elements/1.1/}creator") or node.findtext("author")
    cleaned = _clean_text(raw)
    if not cleaned:
        return []
    return [part.strip() for part in re.split(r";|,(?=\s*[A-Z])", cleaned) if part.strip()]


def _atom_items(feed: ElementTree.Element, venue: str) -> list[CollectedItem]:
    items: list[CollectedItem] = []
    for entry in feed.findall(f"{_ATOM}entry"):
        title = _clean_text(entry.findtext(f"{_ATOM}title"))
        link_element = entry.find(f"{_ATOM}link[@rel='alternate']")
        if link_element is None:
            link_element = entry.find(f"{_ATOM}link")
        link = (link_element.get("href") if link_element is not None else "") or ""
        entry_id = (entry.findtext(f"{_ATOM}id") or "").strip()
        body = entry.findtext(f"{_ATOM}summary") or entry.findtext(f"{_ATOM}content") or ""
        if not title:
            continue
        key = canonical.canonical_key(url=link, guid=entry_id, text=body)
        if not key:
            continue
        items.append(
            CollectedItem(
                canonical_key=key,
                kind="paper",
                title=title,
                url=link or entry_id,
                summary=_summary(body),
                authors=[
                    name
                    for author in entry.findall(f"{_ATOM}author")
                    if (name := _clean_text(author.findtext(f"{_ATOM}name")))
                ],
                venue=venue,
                image_url=_extract_image(entry, body),
                published_at=_parse_datetime(
                    entry.findtext(f"{_ATOM}published") or entry.findtext(f"{_ATOM}updated")
                ),
            )
        )
    return items


# ── HuggingFace Daily Papers ─────────────────────────────────────────────────


async def collect_hf_daily_papers(
    client: httpx.AsyncClient, config: dict
) -> list[CollectedItem]:
    """HuggingFace 的每日精选。

    它是 T1 源里唯一**自带注意力信号**的：`upvotes` 是真人投的票。这个数不
    参与身份，只作为热度证据存进 `extra`，排序层可以用它，但一条内容是什么
    仍然由它的一手出处决定。
    """
    url = str(config.get("url") or "https://huggingface.co/api/daily_papers").strip()
    raw = await _fetch(client, url)
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CollectorError(f"daily papers response is not JSON: {exc}") from exc
    if not isinstance(payload, list):
        raise CollectorError("daily papers response is not a JSON array")

    items: list[CollectedItem] = []
    for record in payload:
        if not isinstance(record, dict):
            continue
        paper = record.get("paper") if isinstance(record.get("paper"), dict) else record
        arxiv = str(paper.get("id") or "").strip()
        title = _clean_text(str(paper.get("title") or record.get("title") or ""))
        if not arxiv or not title:
            continue
        key = canonical.canonical_key(url=f"https://arxiv.org/abs/{arxiv}", text=arxiv)
        if not key:
            continue
        authors = [
            name
            for author in (paper.get("authors") or [])
            if isinstance(author, dict) and (name := _clean_text(str(author.get("name") or "")))
        ]
        upvotes = paper.get("upvotes")
        items.append(
            CollectedItem(
                canonical_key=key,
                kind="paper",
                title=title,
                url=f"https://arxiv.org/abs/{arxiv}",
                summary=_summary(str(paper.get("summary") or "")),
                authors=authors,
                venue="arXiv",
                published_at=_parse_datetime(
                    str(paper.get("publishedAt") or record.get("publishedAt") or "")
                ),
                image_url=str(paper.get("thumbnail") or record.get("thumbnail") or "").strip(),
                extra={"upvotes": upvotes} if isinstance(upvotes, int) else {},
            )
        )
    return items


# ── 会议 / 期刊截稿日 ─────────────────────────────────────────────────────────


async def collect_conference_deadlines(
    client: httpx.AsyncClient, config: dict
) -> list[CollectedItem]:
    """截稿日历。

    读一个 JSON 数组，每项至少要有 `name` / `deadline` / `link`。这个形状是
    社区 deadline 站（ccfddl / ai-deadlines 一系）导出的通用形状；换源只要
    换 URL，不改代码。

    已经过期的条目在这里就丢掉：一条过了期的截稿提醒不是内容，是噪音。
    """
    url = str(config.get("url") or "").strip()
    if not url:
        raise CollectorError("conference_deadlines source needs a `url`")
    raw = await _fetch(client, url)
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CollectorError(f"deadlines response is not JSON: {exc}") from exc
    if not isinstance(payload, list):
        raise CollectorError("deadlines response is not a JSON array")

    now = datetime.now(UTC)
    items: list[CollectedItem] = []
    for record in payload:
        if not isinstance(record, dict):
            continue
        name = _clean_text(str(record.get("name") or record.get("title") or ""))
        link = str(record.get("link") or record.get("url") or "").strip()
        deadline = _parse_datetime(str(record.get("deadline") or ""))
        if not name or not link or deadline is None or deadline < now:
            continue
        key = canonical.canonical_key(url=link, guid=f"{name}-{deadline.date().isoformat()}")
        if not key:
            continue
        items.append(
            CollectedItem(
                canonical_key=f"{key}#deadline-{deadline.date().isoformat()}",
                kind="deadline",
                title=name,
                url=link,
                summary=_clean_text(str(record.get("note") or record.get("description") or "")),
                venue=_clean_text(str(record.get("sub") or record.get("venue") or "")),
                published_at=deadline,
                extra={"deadline_at": deadline.isoformat()},
            )
        )
    return items


#: 源种类 → 采集器。新增一种源改这里一行，调度器不用动。
COLLECTORS = {
    "arxiv": collect_arxiv,
    "rss": collect_rss,
    "hf_daily_papers": collect_hf_daily_papers,
    "conference_deadlines": collect_conference_deadlines,
}


async def collect(
    client: httpx.AsyncClient, *, kind: str, config: dict
) -> list[CollectedItem]:
    collector = COLLECTORS.get(kind)
    if collector is None:
        raise CollectorError(f"no collector for source kind {kind!r}")
    return await collector(client, config)


def spine_domains(candidates: Iterable[str]) -> list[str]:
    """把源给的分类过成**词表里真有的**那些，顺序不变、去重。

    过滤不是挑剔：域是送达地址，一个词表外的字符串谁也匹配不上，留着只会
    让"这条属于哪儿"这个问题有两个不同的答案。
    """
    from app.services.feed.domains import is_known_domain

    out: list[str] = []
    for candidate in candidates:
        slug = (candidate or "").strip()
        if slug and slug not in out and is_known_domain(slug):
            out.append(slug)
    return out

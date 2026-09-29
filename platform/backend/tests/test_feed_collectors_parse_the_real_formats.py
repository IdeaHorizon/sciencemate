"""采集器必须认真实源的格式，不是我造的样本的格式。

## 这个文件存在的理由

第一版只支持 RSS 2.0 和 Atom。喂一个自造的 RSS 2.0 样本，测试全绿。真打了一次
源才发现：**Nature / Nature Materials / Nature Machine Intelligence / Science /
PRL 五个全是 RSS 1.0(RDF)** —— 顶刊那一路一条都进不来。

所以下面的样本是从真实响应里截下来的（结构逐字保留，只删了条目数量），而不是
按规范写出来的。测试要证明的是"这个格式我们认得"，而按规范自己写一份样本，
证明的是"我认得我自己写的东西"。
"""
from __future__ import annotations

import pytest

from app.services.feed import collectors

# ── 真实响应的结构，取自 https://www.nature.com/nmat.rss ──────────────────
NATURE_RDF = b"""<?xml version="1.0" encoding="UTF-8"?>
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
         xmlns:prism="http://prismstandard.org/namespaces/basic/2.0/"
         xmlns:dc="http://purl.org/dc/elements/1.1/"
         xmlns:content="http://purl.org/rss/1.0/modules/content/"
         xmlns="http://purl.org/rss/1.0/">
    <channel rdf:about="http://feeds.nature.com/nmat/rss/current">
        <title>Nature Materials</title>
    </channel>
    <item rdf:about="https://www.nature.com/articles/s41563-026-02716-1">
        <title><![CDATA[Reticular chemistry mimics enzyme pockets]]></title>
        <link>https://www.nature.com/articles/s41563-026-02716-1</link>
        <content:encoded><![CDATA[<p>Nature Materials, Published online: 19 August 2026;
            <a href="x">doi:10.1038/s41563-026-02716-1</a></p>Two metal-organic frameworks.]]></content:encoded>
        <dc:creator>Himan Dev Singh</dc:creator><dc:creator>Wendy L. Queen</dc:creator>
        <dc:date>2026-08-19</dc:date>
        <prism:doi>10.1038/s41563-026-02716-1</prism:doi>
    </item>
</rdf:RDF>
"""

ARXIV_ATOM = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
  <entry>
    <id>http://arxiv.org/abs/2608.20337v1</id>
    <published>2026-08-20T17:59:57Z</published>
    <title>Information on trajectories</title>
    <summary>  Accounting for information flow on the path space.
</summary>
    <author><name>Akshay Balsubramani</name></author>
    <arxiv:primary_category term="math.PR"/>
    <category term="cs.LG"/>
    <category term="math.PR"/>
  </entry>
</feed>
"""

RSS2 = b"""<?xml version="1.0"?>
<rss version="2.0"><channel><title>Some blog</title>
  <item>
    <title>A release</title>
    <link>https://example.org/releases/1</link>
    <description>&lt;p&gt;Version 2 is out.&lt;/p&gt;</description>
    <pubDate>Mon, 18 Aug 2026 09:00:00 GMT</pubDate>
  </item>
</channel></rss>
"""


class _Response:
    def __init__(self, content: bytes) -> None:
        self.content = content
        self.status_code = 200


class _Client:
    """只回一个固定响应 —— 采集器不该在测试里出网。"""

    def __init__(self, content: bytes) -> None:
        self._content = content
        self.requested: list[str] = []

    async def get(self, url: str) -> _Response:
        self.requested.append(url)
        return _Response(self._content)


@pytest.mark.asyncio
async def test_rss1_rdf_is_parsed_because_the_top_journals_use_it() -> None:
    items = await collectors.collect_rss(
        _Client(NATURE_RDF), {"url": "https://www.nature.com/nmat.rss", "venue": "Nature Materials"}
    )
    assert len(items) == 1
    item = items[0]
    assert item.title == "Reticular chemistry mimics enzyme pockets"
    assert item.url == "https://www.nature.com/articles/s41563-026-02716-1"
    # prism:doi 是**已经解析好**的 DOI，比从正文里正则抠可靠。
    assert item.canonical_key == "doi:10.1038/s41563-026-02716-1"
    # dc:creator 是多个同名元素；findtext 只取第一个，那会抹掉所有合著者。
    assert item.authors == ["Himan Dev Singh", "Wendy L. Queen"]
    assert item.published_at is not None and item.published_at.year == 2026
    # 摘要里不许残留 HTML 标签。
    assert "<" not in (item.summary or "")


@pytest.mark.asyncio
async def test_arxiv_primary_category_leads_the_domain_list() -> None:
    """主分类排第一 —— 送达时它是这条内容"属于"哪儿。"""
    client = _Client(ARXIV_ATOM)
    items = await collectors.collect_arxiv(client, {"category": "cs.LG", "max_results": 5})
    assert len(items) == 1
    assert items[0].domains[0] == "math.PR"
    assert set(items[0].domains) == {"math.PR", "cs.LG"}
    assert items[0].canonical_key == "arxiv:2608.20337"
    # 摘要页链接，不是 API 的 id URL，而且升到 https。
    assert items[0].url.startswith("https://arxiv.org/abs/")
    assert "cat:cs.LG" in client.requested[0]


@pytest.mark.asyncio
async def test_rss2_still_works() -> None:
    items = await collectors.collect_rss(_Client(RSS2), {"url": "https://example.org/feed"})
    assert len(items) == 1
    assert items[0].title == "A release"
    assert items[0].summary == "Version 2 is out."
    assert items[0].published_at is not None



@pytest.mark.asyncio
async def test_trusted_science_news_is_not_misclassified_as_a_journal_paper() -> None:
    items = await collectors.collect_rss(
        _Client(RSS2),
        {"url": "https://example.org/feed", "venue": "Quanta Magazine"},
    )
    assert len(items) == 1
    assert items[0].kind == "news"
    assert items[0].extra["feed_acquisition_routes"] == ["bignews"]


@pytest.mark.asyncio
async def test_an_entity_bomb_is_refused_before_parsing() -> None:
    """源在我们控制之外，而 `xml.etree` 对实体炸弹是脆弱的。

    正常的 feed 不需要 DTD，所以带 DTD/ENTITY 声明的一律先拒绝再说 ——
    在解析**之前**，不是在解析崩溃之后。
    """
    bomb = (
        b'<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol">'
        b'<!ENTITY lol2 "&lol;&lol;&lol;">]><rss><channel>'
        b"<item><title>&lol2;</title></item></channel></rss>"
    )
    with pytest.raises(collectors.CollectorError, match="DTD|entity"):
        await collectors.collect_rss(_Client(bomb), {"url": "https://evil.example/feed"})


@pytest.mark.asyncio
async def test_an_unknown_root_element_is_a_loud_failure() -> None:
    """源换了格式必须吵 —— 静默返回空的话，症状是这个源慢慢就没内容了，
    而没人知道为什么。"""
    with pytest.raises(collectors.CollectorError, match="not RSS 2.0"):
        await collectors.collect_rss(
            _Client(b"<html><body>we moved</body></html>"), {"url": "https://example.org/feed"}
        )


def test_a_missing_date_is_not_silently_replaced_with_now() -> None:
    """把抓取时刻当成发表时刻，会让一篇三年前的文章在排序里表现得像今天刚发的。"""
    assert collectors._parse_datetime(None) is None
    assert collectors._parse_datetime("") is None
    assert collectors._parse_datetime("sometime last week") is None
    # ISO 与 RFC822 两种写法都要认（Atom 用前者，RSS 2.0 用后者）。
    assert collectors._parse_datetime("2026-08-20T17:59:57Z") is not None
    assert collectors._parse_datetime("Mon, 18 Aug 2026 09:00:00 GMT") is not None

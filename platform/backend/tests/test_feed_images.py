"""每一条资讯都给得出图 —— 三级供给不许有漏网的那一格。

这一层的失败方式很特别：**它不报错**。取不到图时接口从前返回 404，前端把它
当"这条没有配图"正常渲染，于是一屏灰底占位就是"正常工作"。所以判据不能是
"没有 500"，得是"每一条都真的拿到了图，而且是从哪一级拿到的"。
"""
from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.auth import get_current_user
from app.database import get_db
from app.main import app
from app.models.feed import FeedItem, FeedVisibility
from app.models.user import User
from app.services.feed import thumbnail

_ME = "aaaa1111-1111-4111-8111-111111111111"

# 三条内容分别落在三级供给上（id 里的字母是必须的，见 test_feed_endpoints 的注释）。
_WITH_FEED_IMAGE = "dddd0001-0000-4000-8000-000000000001"
_ARXIV = "dddd0002-0000-4000-8000-000000000002"
_NO_PDF = "dddd0003-0000-4000-8000-000000000003"


@pytest_asyncio.fixture
async def image_client(db_session):
    db_session.add(User(id=_ME, email="me@lab.test", display_name="Me", hashed_password="x",
                        institution_id="inst-a", group_id="group-a"))
    db_session.add_all([
        FeedItem(id=_WITH_FEED_IMAGE, canonical_key="url:https://aps.test/a", kind="paper",
                 title="出版商自己配了图", url="https://aps.test/a",
                 image_url="https://aps.test/key-image.png",
                 authors=[], domains=["cond-mat"], extra={}, venue="Physical Review Letters",
                 visibility=FeedVisibility.PLATFORM),
        FeedItem(id=_ARXIV, canonical_key="arxiv:2401.00001", kind="paper",
                 title="预印本，feed 不带图但论文里有", url="https://arxiv.org/abs/2401.00001",
                 authors=[], domains=["cs.LG"], extra={}, venue="arXiv",
                 visibility=FeedVisibility.PLATFORM),
        FeedItem(id=_NO_PDF, canonical_key="url:https://news.test/x", kind="news",
                 title="一条谁也裁不出图的新闻", url="https://news.test/x",
                 authors=[], domains=[], extra={}, venue=None,
                 visibility=FeedVisibility.PLATFORM),
    ])
    await db_session.flush()

    async def override_get_db():
        yield db_session

    async def override_current_user() -> User:
        return await db_session.get(User, _ME)

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_current_user
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://feed") as c:
            yield c
    finally:
        app.dependency_overrides.clear()




async def _drain_background_thumbnails() -> None:
    """等后台补图任务收工（2026-09-01 契约：图片接口立即返回，真图后台补缓存）。"""
    import asyncio as _aio
    for _ in range(200):
        if not thumbnail._INFLIGHT:
            return
        await _aio.sleep(0)
    raise AssertionError(f"后台补图任务没有收工：{thumbnail._INFLIGHT}")


@pytest.mark.asyncio
async def test_every_item_resolves_to_an_image_even_when_every_real_source_fails(
    image_client, monkeypatch
) -> None:
    """把外网整个打掉，三条内容仍然各自拿到一张图。

    这条测试**故意让前两级全部失败** —— 出版商的图取不回来、PDF 也裁不出来。
    真实部署里这两件事经常发生（404、付费墙、超时），而那正是"所有的都得有图"
    这个要求唯一会被违反的时刻。前两级顺利时谁都是绿的，说明不了什么。
    """
    async def no_publisher_image(url: str):
        from app.services.feed.image_proxy import ImageUnavailable
        raise ImageUnavailable("publisher said no")

    async def no_figure(pdf_url: str):
        return None

    monkeypatch.setattr("app.api.v1.feed.fetch_image", no_publisher_image)
    monkeypatch.setattr(thumbnail, "figure_from_pdf", no_figure)

    for item_id in (_WITH_FEED_IMAGE, _ARXIV, _NO_PDF):
        response = await image_client.get(f"/api/v1/feed/items/{item_id}/image")
        assert response.status_code == 200, f"{item_id} 没有图：{response.text[:200]}"
        assert response.content, f"{item_id} 返回了 0 字节"
        # 兜底那一级是生成封面，明确标注自己是生成的排版卡，不冒充科学插图。
        assert response.headers["content-type"].startswith("image/svg+xml")
        assert b"<svg" in response.content


@pytest.mark.asyncio
async def test_each_tier_is_actually_reached_not_just_the_last_one(
    image_client, monkeypatch
) -> None:
    """三级各走一次 —— 只断言"有图"分不清"三级都在工作"和"永远只有兜底"。

    如果哪天前两级悄悄坏掉（比如 `fetch_image` 的异常类型变了、被上层吞成
    静默失败），全站会退化成清一色的生成封面：版面看起来完好，实际上一张真图
    都没有了，而"每条都有图"的断言依然全绿。
    """
    async def publisher_image(url: str):
        assert url == "https://aps.test/key-image.png"
        return b"\x89PNG\r\n\x1a\nfake", "image/png"

    async def figure(pdf_url: str):
        # 到得了这里就说明 arXiv 的 abs 地址被正确翻成了 PDF 地址。
        assert pdf_url == "https://arxiv.org/pdf/2401.00001"
        return b"\x89PNG\r\n\x1a\nfigure"

    monkeypatch.setattr("app.api.v1.feed.fetch_image", publisher_image)
    monkeypatch.setattr(thumbnail, "figure_from_pdf", figure)

    first = await image_client.get(f"/api/v1/feed/items/{_WITH_FEED_IMAGE}/image")
    assert first.content == b"\x89PNG\r\n\x1a\nfake", "① feed 自带的图没被用上"

    # ② 2026-09-01 契约：cache-miss 首次立即回生成封面（不再让用户陪着下载 PDF——
    # 实测排队 394 秒占满浏览器连接把 UI 点死），真图后台补缓存，下次即真图。
    second_now = await image_client.get(f"/api/v1/feed/items/{_ARXIV}/image")
    assert b"<svg" in second_now.content, "cache-miss 首次应立即回生成封面"
    await _drain_background_thumbnails()
    second = await image_client.get(f"/api/v1/feed/items/{_ARXIV}/image")
    assert second.content == b"\x89PNG\r\n\x1a\nfigure", "② PDF 插图没被后台补进缓存"

    third = await image_client.get(f"/api/v1/feed/items/{_NO_PDF}/image")
    assert b"<svg" in third.content, "③ 兜底封面没被用上"


@pytest.mark.asyncio
async def test_a_paper_with_no_figure_is_not_re_downloaded_on_every_view(
    image_client, monkeypatch
) -> None:
    """裁不出图的论文只下载一次 PDF。

    缓存只记住成功的话，一篇纯理论文章会在**每次**有人刷到这张卡时重下一遍
    PDF —— 上限、并发闸、礼貌全都白设，而症状只是"首页有点慢"。
    """
    calls: list[str] = []

    async def figure(pdf_url: str):
        calls.append(pdf_url)
        return None

    monkeypatch.setattr(thumbnail, "figure_from_pdf", figure)

    for _ in range(3):
        assert (await image_client.get(f"/api/v1/feed/items/{_ARXIV}/image")).status_code == 200
        await _drain_background_thumbnails()
    assert calls == ["https://arxiv.org/pdf/2401.00001"], f"重下了 {len(calls)} 次"


@pytest.mark.asyncio
async def test_a_derived_figure_is_served_from_disk_the_second_time(
    image_client, monkeypatch
) -> None:
    """裁出来的图落盘，重启也不用重做（对 arXiv 也礼貌）。"""
    calls: list[str] = []

    async def figure(pdf_url: str):
        calls.append(pdf_url)
        return b"\x89PNG\r\n\x1a\nfigure"

    monkeypatch.setattr(thumbnail, "figure_from_pdf", figure)

    first = await image_client.get(f"/api/v1/feed/items/{_ARXIV}/image")
    assert b"<svg" in first.content, "cache-miss 首次应立即回生成封面（后台补真图）"
    await _drain_background_thumbnails()
    assert thumbnail.cache_path(_ARXIV).is_file()

    second = await image_client.get(f"/api/v1/feed/items/{_ARXIV}/image")
    assert second.content == b"\x89PNG\r\n\x1a\nfigure"
    assert len(calls) == 1, "第二次又去下了一遍 PDF"


def test_a_pdf_address_is_never_guessed() -> None:
    """猜不出 PDF 地址就说猜不出。

    给别家出版商拼一个 `/pdf` 过去，拿回来的是登录页或 404 页 —— 然后我们
    会把一张登录截图当成"这篇论文的插图"摆在首页上。
    """
    assert thumbnail.pdf_url_for("https://arxiv.org/abs/2401.01234v3") == \
        "https://arxiv.org/pdf/2401.01234"
    assert thumbnail.pdf_url_for(
        "https://www.biorxiv.org/content/10.1101/2026.08.01.123456v1"
    ) == "https://www.biorxiv.org/content/10.1101/2026.08.01.123456v1.full.pdf"

    for opaque in (
        "https://www.nature.com/articles/s41586-026-00001-x",
        "https://www.science.org/doi/10.1126/science.abc1234",
        "https://phys.org/news/2026-08-something.html",
        "",
        None,
    ):
        assert thumbnail.pdf_url_for(opaque) is None, f"给 {opaque} 猜了一个 PDF 地址"


def test_the_generated_cover_claims_nothing_it_cannot_back_up() -> None:
    """兜底封面只放标题与来源 —— 它不冒充任何一张科学插图。

    配一张"好看的科学图"是最容易的做法，也是**拿装饰冒充内容**：读者会以为
    那是这项工作的图。排版卡不会被误认，它只负责让版面完整。
    """
    cover = thumbnail.generated_cover(
        title="Neural quantum states <in> \"condensed matter\" & beyond",
        venue="arXiv", domain="cond-mat.str-el",
    ).decode()
    assert cover.startswith("<svg")
    # 标题里的尖括号和引号必须转义，否则一个带 < 的标题就能把 SVG 撑坏
    # （这张图是要进浏览器的，破损的 SVG 就是又一格空白）。
    assert "<in>" not in cover and '"condensed matter"' not in cover
    assert "&lt;in&gt;" in cover and "&amp;" in cover
    # 同域配色稳定：一眼能认出"这是同一类"。
    assert thumbnail.generated_cover(title="A", venue="x", domain="cond-mat.str-el") \
        != thumbnail.generated_cover(title="B", venue="x", domain="cond-mat.str-el")
    palette_of = lambda d: thumbnail.generated_cover(title="T", venue="v", domain=d)
    assert palette_of("cs.LG") == palette_of("cs.LG")


@pytest.mark.asyncio
async def test_a_paper_without_figures_gets_a_cover_not_a_page_of_text(
    image_client, monkeypatch
) -> None:
    """裁不出图时**不许**退化成"渲染正文首页"。

    第一版那么做了，实测是一张 300KB 的整页正文缩略图：字小到读不了，
    在版面上看着像加载坏了 —— 而读者会以为那就是这篇论文的图。真跑一次、
    把图打开看一眼才发现；`status_code == 200` 对它一路全绿。

    判据钉在**产物的形状**上：没有插图的论文拿到的必须是生成封面（SVG），
    不是一张 PNG。
    """
    async def no_figure(pdf_url: str):
        return None

    monkeypatch.setattr(thumbnail, "figure_from_pdf", no_figure)

    response = await image_client.get(f"/api/v1/feed/items/{_ARXIV}/image")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/svg+xml"), \
        "没有插图的论文被渲染成了一页正文"


def test_a_page_of_body_text_is_never_mistaken_for_a_figure() -> None:
    """铺满整页的"图"是页眉线 + 表格框的并集，不是插图。

    上面那条测试挡的是**产物的形状**，这条挡的是**产生它的判断** —— 没有
    这道面积闸，`figure_from_pdf` 会把整页正文当成一张图裁出来，而它交出的
    确实是一张 PNG，形状那条测试看不出问题。
    """
    assert thumbnail.MAX_FIGURE_PAGE_FRACTION < 1.0
    page_area = 612.0 * 792.0
    whole_page = (612.0 * 0.98) * (792.0 * 0.98)
    assert whole_page / page_area > thumbnail.MAX_FIGURE_PAGE_FRACTION, \
        "整页大小的包围盒没有被这道闸拦住"
    a_real_figure = 468.0 * 124.0   # 实测样本：2608.21349 里裁出来的那张
    assert a_real_figure / page_area < thumbnail.MAX_FIGURE_PAGE_FRACTION, \
        "这道闸把真实插图也拦掉了"

import hashlib
import json
from types import SimpleNamespace

import httpx
import pytest
from PIL import Image

from app.api.v1 import feed
from app.services.feed import image_proxy
from nodes.literature.tools.literature_catalog import upsert_paper
from shared.contrib import paper_image_policy as policy


@pytest.mark.asyncio
async def test_proxy_rejects_known_ui_url_before_reading_memory_cache():
    url = "https://openalex.org/brand-assets/openalex-mark-square.png"
    image_proxy._cache[url] = (b"cached bad image", "image/png")
    try:
        with pytest.raises(image_proxy.ImageUnavailable, match="website_brand_asset"):
            await image_proxy.fetch(url)
    finally:
        image_proxy.clear_cache()


@pytest.mark.asyncio
async def test_proxy_rejects_known_image_bytes_on_new_host(monkeypatch):
    content = b"confirmed shared UI bitmap"
    monkeypatch.setitem(policy.KNOWN_UI_IMAGE_HASHES, hashlib.sha256(content).hexdigest(),
                        "website_brand_asset")
    real_client = httpx.AsyncClient
    monkeypatch.setattr(image_proxy.httpx, "AsyncClient", lambda **kw: real_client(
        transport=httpx.MockTransport(lambda req: httpx.Response(
            200, content=content, headers={"content-type": "image/png"},
        )), **kw,
    ))
    with pytest.raises(image_proxy.ImageUnavailable, match="website_brand_asset"):
        await image_proxy.fetch("https://publisher.test/renamed-asset.png")


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked", [True, False])
async def test_local_catalog_filters_ui_asset_but_keeps_real_figures(
    tmp_path, monkeypatch, blocked,
):
    figure = tmp_path / "paper" / "figure.png"
    figure.parent.mkdir()
    Image.new("RGB", (300, 180), "red").save(figure)
    upsert_paper(tmp_path, {
        "doi": "10.1/check", "title": "Real paper",
        "image": {
            "path": str(figure), "status": "available",
            "url": ("https://captcha.perfdrive.com/captcha-public/images/ss_captcha.png"
                    if blocked else "https://publisher.test/fig1.png"),
        },
    })
    monkeypatch.setattr(feed, "literature_papers_root", lambda: tmp_path)
    item = SimpleNamespace(extra={"doi": "10.1/check"}, url="https://doi.org/10.1/check")
    assert feed._local_literature_figure(item) == (None if blocked else figure)


@pytest.mark.asyncio
async def test_rejected_catalog_and_remote_image_fall_back_to_generated_cover(tmp_path, monkeypatch):
    monkeypatch.setattr(feed, "literature_papers_root", lambda: tmp_path)
    item = SimpleNamespace(
        id="item", extra={}, url="https://doi.org/10.1/check", title="Real paper",
        image_url="https://openalex.org/brand-assets/openalex-mark-square.png",
        venue="Journal", domains=[], source_id=None,
    )

    class DB:
        async def scalar(self, statement):
            return item

    monkeypatch.setattr(feed, "_visible_clause", lambda user: True)
    response = await feed.get_item_image("item", user=SimpleNamespace(), db=DB())
    assert response.media_type == "image/svg+xml"
    assert b"Real paper" in response.body

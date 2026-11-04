import json
import time

import httpx
import pytest

from nodes.literature.tools import paper_images as images
from shared.contrib import paper_image_policy as policy


CAPTCHA = "https://captcha.perfdrive.com/captcha-public/images/ss_captcha.png"
BRAND = "https://openalex.org/brand-assets/openalex-mark-square.png"
PNG = b"\x89PNG\r\n\x1a\n" + b"test" * 100


@pytest.mark.parametrize("url", [
    CAPTCHA, BRAND, "https://publisher.test/images/loading.gif",
    "https://publisher.test/assets/spinner.png",
    "https://publisher.test/cdn-cgi/challenge-platform/picture.png",
])
def test_ui_assets_rejected_even_as_explicit_or_cached_images(tmp_path, monkeypatch, url):
    requested = []
    real_client = httpx.Client

    def handle(request):
        requested.append(str(request.url))
        return httpx.Response(200, text="<html><title>Article</title></html>",
                              headers={"content-type": "text/html"})

    monkeypatch.setattr(images.httpx, "Client",
                        lambda **kw: real_client(transport=httpx.MockTransport(handle), **kw))
    path = tmp_path / "image_urls.json"
    path.write_text(json.dumps({"10.1/test": {"url": url, "ts": time.time()}}))
    result = images.fetch_paper_image_url(
        {"doi": "10.1/test", "image_url": url, "url": "https://publisher.test/article"},
        str(tmp_path),
    )
    assert result["status"] == "unavailable"
    assert url not in requested
    assert "10.1/test" not in json.loads(path.read_text())
    materialized = images.materialize_image_url({"url": url}, str(tmp_path), {})
    assert materialized["status"] == "unavailable"
    assert not (tmp_path / "images").exists()


@pytest.mark.parametrize("html", [
    '<title>Just a moment...</title><meta property="og:image" content="/image.png">',
    '<html><img src="https://captcha.perfdrive.com/captcha-public/images/ss_captcha.png"></html>',
])
def test_http_200_verification_page_has_no_eligible_figures(html):
    assert policy.challenge_page(html)
    assert images._image_url_from_html(html, "https://publisher.test/article") == ""


def test_article_context_and_lazy_figure_win_over_brand_metadata():
    html = f'''<meta content="{BRAND}" property="og:image">
    <img src="/random-ui.png">
    <figure><img src="/loading.gif" data-src="/paper-gr1.png" alt="Figure 1"></figure>'''
    assert images._image_urls_from_html(html, "https://publisher.test/article") == [
        "https://publisher.test/paper-gr1.png",
    ]


def test_scientific_fingerprint_or_captcha_research_is_not_rejected():
    html = '''<title>Fingerprint classification for CAPTCHA research</title>
    <meta name="citation_title" content="Fingerprint classification">
    <figure><img src="/papers/fig1.png"></figure>'''
    assert not policy.challenge_page(html)
    assert images._image_url_from_html(html, "https://publisher.test/article").endswith("/fig1.png")
    assert not policy.image_url_rejection_reason(
        "https://publisher.test/photos/Live-Event-Default-NO-LOGO.jpg",
    )


def test_failed_first_image_does_not_hide_next_article_figure(tmp_path, monkeypatch):
    real_client = httpx.Client

    def handle(request):
        if request.url.path == "/article":
            return httpx.Response(200, headers={"content-type": "text/html"}, text=
                                  '<figure><img src="/fig1.png"><img src="/fig2.png"></figure>')
        if request.url.path == "/fig1.png":
            return httpx.Response(404)
        return httpx.Response(200, headers={"content-type": "image/png"}, content=PNG)

    monkeypatch.setattr(images.httpx, "Client",
                        lambda **kw: real_client(transport=httpx.MockTransport(handle), **kw))
    result = images.fetch_paper_image_url(
        {"doi": "10.1/test", "url": "https://publisher.test/article"}, str(tmp_path),
    )
    assert result["url"] == "https://publisher.test/fig2.png"


def test_openalex_record_page_is_not_used_as_image_source(tmp_path, monkeypatch):
    requested = []
    real_client = httpx.Client

    def handle(request):
        requested.append(str(request.url))
        return httpx.Response(403)

    monkeypatch.setattr(images.httpx, "Client",
                        lambda **kw: real_client(transport=httpx.MockTransport(handle), **kw))
    result = images.fetch_paper_image_url(
        {"doi": "10.1/test", "source": "openalex", "url": "https://openalex.org/W123"},
        str(tmp_path),
    )
    assert requested == ["https://doi.org/10.1/test"]
    assert result["status"] == "unavailable"


def test_redirect_to_verification_image_is_rejected(tmp_path, monkeypatch):
    real_client = httpx.Client

    def handle(request):
        if request.url.host == "publisher.test":
            return httpx.Response(302, headers={"location": CAPTCHA})
        return httpx.Response(200, headers={"content-type": "image/png"}, content=PNG)

    monkeypatch.setattr(images.httpx, "Client",
                        lambda **kw: real_client(transport=httpx.MockTransport(handle), **kw))
    result = images.materialize_image_url(
        {"url": "https://publisher.test/image"}, str(tmp_path), {},
    )
    assert result["status"] == "unavailable"
    assert result["reason"] == "captcha_or_anti_bot"


def test_bad_archived_figure_does_not_short_circuit_reacquisition(tmp_path, monkeypatch):
    from nodes.literature.tools import archive_papers as archive
    root = tmp_path / "papers"
    paper = {"doi": "10.1/test", "title": "Real research", "index_status": "acquired",
             "image": {"status": "available", "url": CAPTCHA, "path": "/old.png"}}
    layout = archive._article_layout(str(root), paper)
    archive._write_article_index(paper, layout["index"])
    attempts = []

    def unavailable(record, output_dir):
        attempts.append(record["doi"])
        return {"status": "unavailable", "reason": "captcha_or_anti_bot"}

    monkeypatch.setattr(archive, "fetch_paper_image_url", unavailable)
    result = archive.archive_index_and_figure(paper, str(root))
    assert attempts == ["10.1/test"]
    assert result["image"]["fallback"] == "title_card"
    assert result["figure_status"] == "failed"

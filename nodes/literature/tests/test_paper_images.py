from __future__ import annotations

import io
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from nodes.literature.tools.paper_images import extract_paper_image, extract_representative_image, fetch_paper_image_url, materialize_image_url


def _make_pdf(path):
    fitz = pytest.importorskip("fitz")
    image = Image.new("RGB", (900, 420), "white")
    # A few coloured blocks make the deterministic visual scorer prefer it.
    draw = ImageDraw.Draw(image)
    for box, colour in [((40, 100, 260, 300), "#4c78a8"), ((320, 100, 540, 300), "#f58518"), ((600, 100, 820, 300), "#54a24b")]:
        draw.rectangle(box, fill=colour)
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    page.insert_text((50, 50), "Figure 1. Proposed architecture and model framework")
    page.insert_image(fitz.Rect(40, 100, 555, 380), stream=buf.getvalue())
    doc.save(path)
    doc.close()


def test_extract_representative_image_records_provenance(tmp_path):
    pytest.importorskip("fitz")
    pdf = tmp_path / "paper.pdf"
    _make_pdf(pdf)
    result = extract_representative_image(str(pdf), str(tmp_path / "cache"), {"doi": "10.1234/example"})
    assert result["status"] == "extracted"
    assert result["source"] == "pdf"
    assert result["page"] == 1
    assert result["path"].endswith("_representative.png")
    assert len(result["sha256"]) == 64
    assert (tmp_path / "cache" / "images").is_dir()


def test_missing_pdf_is_non_blocking(tmp_path):
    result = extract_paper_image({"title": "missing"}, str(tmp_path))
    assert result["status"] == "unavailable"
    assert result["reason"] == "no_local_pdf"


def test_invalid_pdf_is_non_blocking(tmp_path):
    pytest.importorskip("fitz")
    pdf = tmp_path / "bad.pdf"
    pdf.write_bytes(b"not a pdf")
    result = extract_representative_image(str(pdf), str(tmp_path / "cache"), {})
    assert result["status"] == "unavailable"
    assert result["reason"] == "invalid_pdf_header"


class _FakeResponse:
    status_code = 200
    headers = {"content-type": "image/png"}
    content = b"\x89PNG\r\n" + b"x" * 300
    url = "https://cdn.example/figure.png"
    text = ""


class _FakeClient:
    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def get(self, url, **kwargs):
        return _FakeResponse()


def test_remote_image_url_is_cached_and_audited(tmp_path, monkeypatch):
    import nodes.literature.tools.paper_images as module
    monkeypatch.setattr(module.httpx, "Client", _FakeClient)
    result = fetch_paper_image_url(
        {"doi": "10.1234/example", "image_url": "https://cdn.example/figure.png"},
        str(tmp_path),
    )
    assert result["status"] == "available"
    assert result["source"] == "metadata"
    assert (tmp_path / "image_urls.json").exists()


def test_first_page_small_publisher_logo_is_rejected(tmp_path):
    fitz = pytest.importorskip("fitz")
    pdf = tmp_path / "logo.pdf"
    image = Image.new("RGB", (260, 100), "white")
    draw = ImageDraw.Draw(image)
    draw.text((10, 40), "PUBLISHER LOGO", fill="black")
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    doc = fitz.open()
    page = doc.new_page()
    page.insert_image(fitz.Rect(40, 40, 300, 140), stream=buf.getvalue())
    doc.save(pdf)
    doc.close()
    result = extract_representative_image(str(pdf), str(tmp_path / "cache"), {"doi": "10.1234/logo"})
    assert result["status"] == "unavailable"
    assert result["reason"] == "no_eligible_embedded_figure"


def test_remote_image_is_materialized_locally(tmp_path, monkeypatch):
    import nodes.literature.tools.paper_images as module
    monkeypatch.setattr(module.httpx, "Client", _FakeClient)
    result = materialize_image_url(
        {"status": "available", "url": "https://cdn.example/figure.png", "source": "metadata", "method": "direct_url"},
        str(tmp_path),
        {"doi": "10.1234/local"},
    )
    assert result["status"] == "available"
    assert result["path"].endswith("_remote.png")
    assert Path(result["path"]).read_bytes().startswith(b"\x89PNG")
    assert result["url"].startswith("https://")


def test_a_title_card_does_not_permanently_disable_real_image_retries(
    tmp_path, monkeypatch
):
    import nodes.literature.tools.archive_papers as archive

    attempts = []

    def unavailable(record, index_dir):
        attempts.append(record["doi"])
        return {
            "status": "unavailable",
            "reason": "publisher_temporarily_unavailable",
            "method": "publisher_landing",
        }

    def title_card(record, article_dir, **kwargs):
        path = Path(article_dir) / "figure" / "paper_title_card.png"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"title-card")
        return {"status": "available", "path": str(path), "source": "generated"}

    monkeypatch.setattr(archive, "fetch_paper_image_url", unavailable)
    monkeypatch.setattr(archive, "generate_title_card", title_card)

    paper = {
        "doi": "10.1234/retry-image",
        "title": "Retry real image",
        "url": "https://doi.org/10.1234/retry-image",
    }
    first = archive.archive_index_and_figure(paper, str(tmp_path / "papers"))
    second = archive.archive_index_and_figure(paper, str(tmp_path / "papers"))

    assert first["image"]["fallback"] == "title_card"
    assert second["image"]["fallback"] == "title_card"
    assert attempts == ["10.1234/retry-image", "10.1234/retry-image"]

"""Extract an auditable representative image from an already cached paper PDF.

This module is downstream of the existing full-text strategy. It never searches
for a paper and never changes whether a PDF is considered downloaded.
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import time
from pathlib import Path
from typing import Any

import httpx

IMAGE_SCHEMA_VERSION = 1
_MODEL_TERMS = ("graphical abstract", "architecture", "framework", "model overview", "pipeline", "schematic", "system overview", "structure", "流程图", "架构", "框架", "模型结构", "系统结构")
_RESULT_TERMS = ("accuracy", "precision", "recall", "f1", "auc", "ablation", "experimental results", "performance comparison", "baseline", "结果", "对比实验", "消融实验")


def _paper_image_id(paper: dict[str, Any]) -> str:
    identity = "|".join(str(paper.get(key) or "").strip().lower() for key in ("doi", "url", "title"))
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]


def _caption_score(page_text: str, image_index: int, image_count: int = 1) -> tuple[int, str]:
    text = " ".join((page_text or "").lower().split())
    # 不再默认奖励首页第一张图：很多 PDF 首页第一张图是出版社 logo。
    score = 0
    score += sum(10 for term in _MODEL_TERMS if term in text)
    score -= sum(12 for term in _RESULT_TERMS if term in text)
    if image_count != 1:
        return score, ""
    match = re.search(r"\b(?:figure|fig\.?|图)\s*([0-9]+[a-z]?)", text, re.I)
    return score, (f"Figure {match.group(1)}" if match else "")


def _visual_score(image: Any) -> dict[str, Any]:
    width, height = image.size
    aspect = width / height if height else 0.0
    score = 15
    if aspect > 1.5:
        score += 25
    elif aspect > 1.2:
        score += 15
    elif 0 < aspect < 0.8:
        score -= 15
    try:
        colours = image.convert("RGB").getcolors(maxcolors=50_000)
        colour_count = len(colours) if colours is not None else 50_001
    except Exception:
        colour_count = 0
    if 5 < colour_count < 500:
        score += 20
    elif colour_count > 5_000:
        score -= 15
    if width < 200 or height < 100:
        score -= 30
    if width >= 400 and height >= 200:
        score += 10
    return {"score": score, "width": width, "height": height, "aspect_ratio": round(aspect, 4)}


def _unavailable(reason: str) -> dict[str, Any]:
    return {"schema_version": IMAGE_SCHEMA_VERSION, "status": "unavailable", "path": "", "url": "", "source": "", "method": "", "page": None, "figure_label": "", "score": None, "width": None, "height": None, "sha256": "", "reason": reason}


def generate_title_card(paper: dict[str, Any], output_dir: str, *, image_subdir: str = "images") -> dict[str, Any]:
    """Generate an explicit placeholder image when no paper image is available.

    This is not treated as a paper figure: the provenance is marked as
    ``generated/title_card`` so consumers can distinguish it from remote or
    PDF-derived images.
    """
    try:
        from PIL import Image, ImageDraw, ImageFont
    except Exception as exc:  # pragma: no cover
        return _unavailable(f"title_card_dependency_missing:{type(exc).__name__}")
    title = " ".join(str(paper.get("title") or "Untitled paper").split()).strip() or "Untitled paper"
    width, height = 1600, 900
    image = Image.new("RGB", (width, height), (28, 39, 58))
    draw = ImageDraw.Draw(image)
    # Keep the card legible without requiring an image-generation model.
    draw.rectangle((70, 70, width - 70, height - 70), outline=(111, 190, 170), width=4)
    font_candidates = (
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    )
    font = None
    for candidate in font_candidates:
        try:
            font = ImageFont.truetype(candidate, 58)
            break
        except Exception:
            continue
    if font is None:
        font = ImageFont.load_default()
    # Wrap by rendered width so both Latin and CJK titles remain inside card.
    lines: list[str] = []
    current = ""
    for char in title:
        trial = current + char
        if current and draw.textlength(trial, font=font) > width - 240:
            lines.append(current)
            current = char
        else:
            current = trial
    if current:
        lines.append(current)
    lines = lines[:8]
    text = "\n".join(lines)
    bbox = draw.multiline_textbbox((0, 0), text, font=font, spacing=18, align="center")
    x = (width - (bbox[2] - bbox[0])) / 2
    y = (height - (bbox[3] - bbox[1])) / 2
    draw.multiline_text((x, y), text, font=font, fill=(242, 247, 250), spacing=18, align="center")
    target_dir = Path(output_dir) / image_subdir
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{_paper_image_id(paper)}_title_card.png"
    try:
        image.save(target, format="PNG", optimize=True)
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
    except Exception as exc:
        return _unavailable(f"title_card_write_failed:{type(exc).__name__}")
    return {
        "schema_version": IMAGE_SCHEMA_VERSION,
        "status": "generated",
        "path": str(target.resolve()),
        "url": "",
        "source": "generated",
        "method": "title_card",
        "page": None,
        "figure_label": "",
        "score": None,
        "width": width,
        "height": height,
        "sha256": digest,
        "reason": "no_remote_or_pdf_image",
    }


def _arxiv_id(paper: dict[str, Any]) -> str:
    for value in (paper.get("arxiv_id", ""), paper.get("url", ""), paper.get("doi", "")):
        match = re.search(r"(?:arxiv\.org/(?:abs|pdf|html)/|10\.48550/arxiv\.)(\d{4}\.\d{4,5})(?:v\d+)?", str(value), re.I)
        if match:
            return match.group(1)
    return ""


def _image_url_from_html(html: str, base_url: str) -> str:
    """从论文 landing page 找图片，排除站点 logo 和图标。"""
    from urllib.parse import urljoin
    patterns = [
        r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)',
        r'<meta[^>]+name=["\']twitter:image["\'][^>]+content=["\']([^"\']+)',
        r'<meta[^>]+name=["\']citation_image["\'][^>]+content=["\']([^"\']+)',
        r'<img[^>]+(?:src|data-src)=["\']([^"\']+)["\']',
    ]
    blocked = ("logo", "icon", "avatar", "favicon", "sprite", "cookie", "placeholder", "cover", "journal-cover")
    for pattern in patterns:
        for match in re.finditer(pattern, html, re.I):
            url = urljoin(base_url, match.group(1).strip())
            if url.startswith(("http://", "https://")) and not any(x in url.lower() for x in blocked):
                return url
    return ""


def _is_image_response(response: httpx.Response) -> bool:
    content_type = (response.headers.get("content-type") or "").lower()
    magic = response.content[:12]
    return "image/" in content_type or magic.startswith((b"\x89PNG", b"\xff\xd8\xff", b"GIF8", b"RIFF"))


def _response_failure_reason(response: httpx.Response) -> str:
    """Return an auditable category for a failed HTTP/image response."""
    status = int(response.status_code)
    body = response.content[:8192].lower()
    anti_bot = (b"cloudflare" in body or b"captcha" in body or b"challenge" in body
                or b"just a moment" in body or b"cf-chl" in body)
    if status in (401, 403) and anti_bot:
        return "captcha_or_anti_bot"
    if status == 403:
        return "http_403"
    if status == 429:
        return "http_429"
    if status >= 500:
        return "http_5xx"
    if status >= 400:
        return "http_error"
    if not _is_image_response(response) or len(response.content) < 256:
        return "invalid_image_response"
    return ""


def _verify_image_url_detailed(client: httpx.Client, url: str) -> tuple[bool, str]:
    try:
        response = client.get(url, headers={"Range": "bytes=0-4095"})
    except httpx.TimeoutException:
        return False, "network_timeout"
    except httpx.HTTPError:
        return False, "network_error"
    return (True, "") if not _response_failure_reason(response) else (False, _response_failure_reason(response))


def _verify_image_url(client: httpx.Client, url: str) -> bool:
    ok, _ = _verify_image_url_detailed(client, url)
    return ok


def _cached_image_urls(output_dir: str) -> tuple[Path, dict[str, Any]]:
    path = Path(output_dir) / "image_urls.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return path, data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return path, {}


def _publisher_page_candidates(paper: dict[str, Any], doi: str) -> list[tuple[str, str]]:
    """为常见出版社补充不依赖 DOI 重定向的文章页候选。"""
    if not doi:
        return []
    venue = str(paper.get("venue") or paper.get("journal") or "").lower()
    encoded = doi.strip()
    candidates: list[tuple[str, str]] = []
    if doi.startswith("10.1103/"):
        aps_journal = "prl" if "physical review letters" in venue else "pre" if "physical review e" in venue else "prfluids" if "physical review fluids" in venue else "pre"
        candidates.extend([
            (f"https://journals.aps.org/{aps_journal}/article/{encoded}", "aps_landing"),
            (f"https://journals.aps.org/{aps_journal}/article/{encoded}/figures/1/medium", "aps_figure"),
        ])
    elif doi.startswith("10.1063/"):
        # AIP 的 DOI 页面可通过 doi.org 跳转；部分期刊也接受 /doi/路径。
        candidates.extend([
            (f"https://pubs.aip.org/doi/{encoded}", "aip_landing"),
            (f"https://pubs.aip.org/aip/{encoded}", "aip_landing"),
        ])
    elif doi.startswith("10.1088/"):
        candidates.extend([
            (f"https://iopscience.iop.org/article/{encoded}", "iop_landing"),
            (f"https://iopscience.iop.org/article/{encoded}/figure/1", "iop_figure"),
        ])
    elif doi.startswith("10.1080/"):
        candidates.append((f"https://www.tandfonline.com/doi/full/{encoded}", "taylorfrancis_landing"))
    elif doi.startswith("10.1093/"):
        candidates.append((f"https://academic.oup.com/article/doi/{encoded}", "oxford_landing"))
    elif doi.startswith("10.1177/"):
        candidates.append((f"https://journals.sagepub.com/doi/full/{encoded}", "sage_landing"))
    return candidates


def fetch_paper_image_url(paper: dict[str, Any], output_dir: str, *, timeout: float = 6.0, ttl: float = 30 * 86400) -> dict[str, Any]:
    """按网页/CDN策略寻找图片 URL；不下载 PDF，也不改变全文状态。"""
    identity = str(paper.get("doi") or paper.get("url") or paper.get("title") or "").strip().lower()
    if not identity:
        return {"status": "unavailable", "url": "", "source": "remote", "method": "none", "reason": "paper_identity_missing"}
    cache_path, cache = _cached_image_urls(output_dir)
    cached = cache.get(identity)
    cached_url = str(cached.get("url", "")) if isinstance(cached, dict) else ""
    cached_blocked = ("cover" in cached_url.lower() or "journal-cover" in cached_url.lower())
    if isinstance(cached, dict) and cached_url and not cached_blocked and time.time() - float(cached.get("ts", 0)) < ttl:
        return {"status": "available", "url": cached_url, "source": cached.get("source", "remote_cache"), "method": "cache", "reason": ""}

    candidates: list[tuple[str, str]] = []
    explicit = str(paper.get("image_url") or "").strip()
    if explicit.startswith(("http://", "https://")):
        candidates.append((explicit, "metadata"))
    arxiv = _arxiv_id(paper)
    if arxiv:
        html_url = f"https://arxiv.org/html/{arxiv}"
        candidates.append((html_url, "arxiv_html"))
        for suffix in ("x1.png", "x2.png", "x3.png"):
            candidates.append((f"{html_url}/{suffix}", "arxiv_html"))
    # 已知出版商 CDN：只构造候选 URL，仍必须通过图片魔数/content-type 校验。
    doi = str(paper.get("doi") or "").strip().lower()
    crossref_message: dict[str, Any] = {}
    if doi.startswith(("10.1016/", "10.3389/", "10.1007/", "10.1186/", "10.3390/")):
        try:
            with httpx.Client(timeout=timeout, headers={"User-Agent": "harness-literature-image/1.0"}) as metadata_client:
                metadata_response = metadata_client.get(f"https://api.crossref.org/works/{doi}")
                if metadata_response.status_code < 400:
                    crossref_message = metadata_response.json().get("message", {}) or {}
        except Exception:
            crossref_message = {}
    if doi.startswith("10.1016/"):
        for pii in crossref_message.get("alternative-id", []) or []:
            pii_text = str(pii).strip()
            if pii_text.startswith("S"):
                # Crossref 对老 Elsevier 记录可能返回 S0301-9322(97)88594-1，
                # 而 CDN 使用去掉标点后的 PII（S0301932297885941）。
                normalized_pii = re.sub(r"[^A-Za-z0-9]", "", pii_text)
                # 不只猜 graphical abstract 的 *_lrg 文件；正文图通常是 gr1.jpg。
                for suffix in ("gr1", "gr2", "gr3", "ga1", "fx1", "ga1_lrg", "gr1_lrg", "fx1_lrg"):
                    candidates.append((f"https://ars.els-cdn.com/content/image/1-s2.0-{normalized_pii}-{suffix}.jpg", "elsevier_cdn"))
    elif doi.startswith("10.3389/"):
        parts = doi.split("/")[-1].split(".")
        if len(parts) >= 2:
            journal, article_id = parts[0], parts[-1]
            volume = str(crossref_message.get("volume") or "")
            for stem in ([f"{journal}-{volume}-{article_id}"] if volume else []) + [f"{journal}-{article_id}"]:
                for figure in ("g001", "g002", "g003"):
                    candidates.append((f"https://www.frontiersin.org/files/Articles/{article_id}/xml-images/{stem}-{figure}.webp", "frontiers_cdn"))
    elif doi.startswith(("10.1007/", "10.1186/")):
        page = str(crossref_message.get("page") or "").split("-")[0] or str(crossref_message.get("article-number") or "")
        if page:
            suffix = doi.split("/")[-1].replace("/", "%2F")
            journal = suffix.split("-")[0]
            year = str(paper.get("year") or crossref_message.get("published", {}).get("date-parts", [[""]])[0][0] or "")
            candidates.append((f"https://media.springernature.com/lw685/springer-static/image/art%3A{doi.replace('/', '%2F')}/MediaObjects/{journal}_{year}_{page}_Fig1_HTML.png", "springer_cdn"))
    elif doi.startswith("10.3390/"):
        volume = str(crossref_message.get("volume") or "")
        page = str(crossref_message.get("page") or "").split("-")[0]
        if volume and page:
            journal = str((crossref_message.get("container-title") or [""])[0]).lower().replace(" ", "-")
            if journal:
                candidates.append((f"https://pub.mdpi-res.com/{journal}/{journal}-{volume.zfill(2)}-{page.zfill(5)}/article_deploy/html/images/{journal}-{volume.zfill(2)}-{page.zfill(5)}-g001.png", "mdpi_cdn"))

    landing = str(paper.get("url") or "").strip()
    doi_value = str(paper.get("doi") or "").strip()
    # OpenAlex 的 url 通常只是 openalex.org/W... 记录页，不是出版社论文页。
    # 有 DOI 时优先走 DOI 跳转，让出版社 landing page 暴露图注/og:image；
    # OpenAlex 页面仍保留为最后兜底，避免丢掉没有 DOI 的记录。
    is_openalex_record = str(paper.get("source") or "").lower() == "openalex" or "openalex.org/" in landing.lower()
    if doi_value and (is_openalex_record or not landing):
        candidates.append((f"https://doi.org/{doi_value}", "doi_landing_page"))
    candidates.extend(_publisher_page_candidates(paper, doi_value.lower()))
    if landing.startswith(("http://", "https://")):
        candidates.append((landing, "landing_page"))

    failure_reasons: list[str] = []
    try:
        with httpx.Client(follow_redirects=True, timeout=timeout, headers={"User-Agent": "harness-literature-image/1.0"}) as client:
            for url, method in candidates:
                try:
                    is_html = method in {"arxiv_html", "landing_page", "doi_landing_page", "aps_landing", "aip_landing", "iop_landing", "taylorfrancis_landing", "oxford_landing", "sage_landing"} and not url.lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".gif"))
                    if is_html:
                        response = client.get(url)
                        reason = _response_failure_reason(response) if response.status_code >= 400 else ""
                        if reason:
                            failure_reasons.append(reason)
                            continue
                        if "text/html" not in (response.headers.get("content-type") or ""):
                            failure_reasons.append("invalid_landing_response")
                            continue
                        image_url = _image_url_from_html(response.text[:2_000_000], str(response.url))
                        if not image_url:
                            failure_reasons.append("no_image_metadata")
                            continue
                        verified, reason = _verify_image_url_detailed(client, image_url)
                        if not verified:
                            failure_reasons.append(reason)
                            continue
                        result = {"status": "available", "url": image_url, "source": "arxiv_html" if method == "arxiv_html" else "landing_page", "method": "html_meta_or_img", "reason": ""}
                        cache[identity] = {"url": image_url, "source": result["source"], "ts": time.time()}
                        cache_path.parent.mkdir(parents=True, exist_ok=True)
                        cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
                        return result
                    verified, reason = _verify_image_url_detailed(client, url)
                    if not verified:
                        failure_reasons.append(reason)
                        continue
                    result = {"status": "available", "url": url, "source": method, "method": "direct_url", "reason": ""}
                    cache[identity] = {"url": url, "source": method, "ts": time.time()}
                    cache_path.parent.mkdir(parents=True, exist_ok=True)
                    cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
                    return result
                except httpx.TimeoutException:
                    failure_reasons.append("network_timeout")
                except httpx.HTTPError:
                    failure_reasons.append("network_error")
                except Exception:
                    failure_reasons.append("network_error")
    except httpx.TimeoutException:
        failure_reasons.append("network_timeout")
    except httpx.HTTPError:
        failure_reasons.append("network_error")
    if not candidates:
        primary = "publisher_rule_miss"
    elif failure_reasons:
        priority = ("captcha_or_anti_bot", "http_403", "http_429", "network_timeout", "network_error", "http_5xx", "http_error", "invalid_image_response", "no_image_metadata", "invalid_landing_response")
        primary = next((item for item in priority if item in failure_reasons), failure_reasons[-1])
    else:
        primary = "publisher_rule_miss"
    return {"status": "unavailable", "url": "", "source": "remote", "method": "tried_html_and_direct", "reason": primary, "failure_reasons": sorted(set(failure_reasons))}



def materialize_image_url(remote: dict[str, Any], output_dir: str, paper: dict[str, Any], *, timeout: float = 12.0, image_subdir: str = "images") -> dict[str, Any]:
    """把已验证的远程图片下载到本地图片目录，并保留原 URL。"""
    url = str(remote.get("url") or "").strip()
    if not url:
        return {"status": "unavailable", "reason": "remote_url_missing", "url": url}
    identity = "|".join(str(paper.get(key) or "").strip().lower() for key in ("doi", "url", "title"))
    image_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]
    try:
        with httpx.Client(follow_redirects=True, timeout=timeout, headers={"User-Agent": "harness-literature-image/1.0"}) as client:
            response = client.get(url)
        if response.status_code >= 400 or not _is_image_response(response) or len(response.content) < 256:
            return {"status": "unavailable", "reason": f"download_invalid:{response.status_code}", "url": url}
        content_type = (response.headers.get("content-type") or "").lower()
        suffix = ".jpg" if "jpeg" in content_type or response.content[:3] == b"\xff\xd8\xff" else ".webp" if "webp" in content_type else ".gif" if "gif" in content_type else ".png"
        target_dir = Path(output_dir) / image_subdir
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"{image_id}_remote{suffix}"
        target.write_bytes(response.content)
        digest = hashlib.sha256(response.content).hexdigest()
        return {"status": "available", "path": str(target.resolve()), "url": url, "source": remote.get("source", "remote"), "method": remote.get("method", ""), "sha256": digest, "bytes": len(response.content), "reason": ""}
    except Exception as exc:
        return {"status": "unavailable", "reason": f"download_failed:{type(exc).__name__}", "url": url}


def extract_representative_image(pdf_path: str, output_dir: str, paper: dict[str, Any], *, max_pages: int | None = None, min_score: int = 20, deadline: float | None = None, image_subdir: str = "images") -> dict[str, Any]:
    """Extract the best embedded figure from the PDF.

    By default all pages are scanned. ``max_pages`` remains available for
    callers that deliberately want a bounded probe; the archive path still
    has its overall deadline so long papers cannot run indefinitely.
    """
    path = Path(pdf_path)
    if not path.exists():
        return _unavailable("pdf_missing")
    try:
        with path.open("rb") as stream:
            if stream.read(5) != b"%PDF-":
                return _unavailable("invalid_pdf_header")
    except OSError:
        return _unavailable("pdf_unreadable")
    try:
        import fitz
        from PIL import Image
    except Exception as exc:  # pragma: no cover
        return _unavailable(f"image_dependency_missing:{type(exc).__name__}")
    try:
        document = fitz.open(str(path))
    except Exception as exc:
        return _unavailable(f"pdf_open_failed:{type(exc).__name__}")
    candidates: list[dict[str, Any]] = []
    try:
        page_limit = len(document) if max_pages is None else min(max_pages, len(document))
        for page_number in range(page_limit):
            if deadline is not None and time.monotonic() >= deadline:
                break
            page = document[page_number]
            page_text = page.get_text("text") or ""
            image_infos = page.get_images(full=True)
            for image_index, image_info in enumerate(image_infos):
                try:
                    if deadline is not None and time.monotonic() >= deadline:
                        break
                    raw = document.extract_image(image_info[0])
                    image = Image.open(io.BytesIO(raw["image"])).convert("RGB")
                    visual = _visual_score(image)
                    caption, label = _caption_score(page_text, image_index, len(image_infos))
                    # 首页小图且没有 Figure/Fig/图注或模型关键词，通常是出版社 logo、期刊标志或装饰图。
                    # 大尺寸 graphical abstract 仍允许保留；正文页正常按视觉+图注评分。
                    frontmatter = page_number == 0 and image.width < 400 and image.height < 300
                    has_figure_context = bool(label) or any(term in page_text.lower() for term in _MODEL_TERMS)
                    if frontmatter and not has_figure_context:
                        continue
                    total = visual["score"] + caption
                    if total >= min_score:
                        candidates.append({"image": image.copy(), "page": page_number + 1, "figure_label": label, "score": total, "width": visual["width"], "height": visual["height"]})
                except Exception:
                    continue
    finally:
        document.close()
    if not candidates:
        return _unavailable("no_eligible_embedded_figure")
    best = max(candidates, key=lambda item: item["score"])
    target_dir = Path(output_dir) / image_subdir
    target_dir.mkdir(parents=True, exist_ok=True)
    image_path = target_dir / f"{_paper_image_id(paper)}_representative.png"
    try:
        best["image"].save(image_path, format="PNG", optimize=True)
        digest = hashlib.sha256(image_path.read_bytes()).hexdigest()
    except Exception as exc:
        return _unavailable(f"image_write_failed:{type(exc).__name__}")
    return {"schema_version": IMAGE_SCHEMA_VERSION, "status": "extracted", "path": str(image_path.resolve()), "source": "pdf", "page": best["page"], "figure_label": best["figure_label"], "score": best["score"], "width": best["width"], "height": best["height"], "sha256": digest, "reason": ""}


def extract_paper_image(paper: dict[str, Any], output_dir: str, *, max_pages: int | None = None, deadline: float | None = None, image_subdir: str = "images") -> dict[str, Any]:
    """Return image metadata for one archived paper without affecting full text."""
    pdf_path = str(paper.get("pdf_path") or "").strip()
    if not pdf_path:
        return _unavailable("no_local_pdf")
    return extract_representative_image(pdf_path, output_dir, paper, max_pages=max_pages, deadline=deadline, image_subdir=image_subdir)

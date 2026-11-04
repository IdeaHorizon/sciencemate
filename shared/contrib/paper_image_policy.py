"""Reject known website UI assets at collection, cache and serving boundaries."""
from __future__ import annotations

import hashlib
import re
from urllib.parse import unquote, urlsplit


# Confirmed cached website assets, not perceptual similarity to scientific plots.
KNOWN_UI_IMAGE_HASHES = {
    "308a47a0cf2eb75fe02b6c66e8e86e1f7cfb1e730282570f318b1b4929558cb2": "captcha_or_anti_bot",
    "655ba6113c02073ef097cb5944d0675ec22a15eec9dd9ab98d22bd1c22474c41": "website_brand_asset",
}


def image_url_rejection_reason(url: str) -> str:
    parts = urlsplit(str(url or ""))
    host = (parts.hostname or "").lower()
    path = unquote(parts.path).lower()
    if any(host == domain or host.endswith("." + domain) for domain in (
        "perfdrive.com", "hcaptcha.com", "recaptcha.net",
    )) or re.search(r"(?:^|[/_.-])(?:captcha|recaptcha|hcaptcha)(?:[/_.-]|$)", path):
        return "captcha_or_anti_bot"
    if "/cdn-cgi/challenge-platform/" in path:
        return "captcha_or_anti_bot"
    if any(token in path for token in (
        "/brand-assets/", "/branding/", "/site-assets/", "openalex-mark",
    )):
        return "website_brand_asset"
    if re.search(
        r"(?:^|[/_.-])(?:logo|favicon|avatar|sprite|placeholder|spinner|loading|"
        r"loader|cookie|icon|journal-cover)(?:[/_.-]|$)",
        re.sub(r"no[-_]logo", "", path),
    ):
        return "website_ui_asset"
    return ""


def image_record_rejection_reason(image: dict) -> str:
    if not isinstance(image, dict):
        return ""
    return image_url_rejection_reason(image.get("url", "")) or KNOWN_UI_IMAGE_HASHES.get(
        str(image.get("sha256") or ""), "",
    )


def image_bytes_rejection_reason(content: bytes) -> str:
    return KNOWN_UI_IMAGE_HASHES.get(hashlib.sha256(content).hexdigest(), "")


def challenge_page(html: str) -> bool:
    """Check challenge-page identity, not incidental CAPTCHA mentions in a paper."""
    title = re.search(r"<title\b[^>]*>(.*?)</title\s*>", html, re.I | re.S)
    if title and re.search(
        r"just a moment|access denied|robot check|human verification|"
        r"verify you are human|checking your browser|request rejected|"
        r"pardon our interruption|security check",
        title.group(1), re.I,
    ):
        return True
    return bool(re.search(
        r"""(?:src|action)=["'][^"']*(?:captcha\.perfdrive\.com|/cdn-cgi/challenge-platform/)""",
        html, re.I,
    ) and not re.search(r"""name=["']citation_(?:title|doi)["']""", html, re.I))

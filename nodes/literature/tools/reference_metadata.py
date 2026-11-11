"""Public reference facts for consumers; no scientific or writing verdict.

DOI content negotiation works across registration agencies. arXiv metadata uses
the same parser as Literature search. A reachable URL alone proves no title,
authorship, or support for a scientific claim.
"""
from __future__ import annotations

import html
import re
import unicodedata
from difflib import SequenceMatcher
from urllib.parse import quote, urlsplit
from xml.etree.ElementTree import ParseError

import httpx
from shared.contrib.literature_identity import normalize_doi, normalize_title


def _text(value: str) -> str:
    value = html.unescape(value)
    value = re.sub(r"\\[a-zA-Z]+", "", value)
    return "".join(c.casefold() for c in unicodedata.normalize("NFKD", value)
                   if c.isalnum())


def _title(value: str) -> str:
    plain = html.unescape(re.sub(r"<[^>]*>", " ", value))
    plain = re.sub(r"\\[a-zA-Z]+", "", plain).replace("{", "").replace("}", "")
    return normalize_title(plain)


def _authors(value: str) -> list[str]:
    """BibTeX's 'and' separator does not split a braced organization name."""
    parts, start, depth = [], 0, 0
    for match in re.finditer(r"[{}]|\s+and\s+", value):
        token = match.group()
        if token == "{":
            depth += 1
        elif token == "}":
            depth -= 1
        elif not depth:
            parts.append(value[start:match.start()].strip())
            start = match.end()
    return parts + [value[start:].strip()]


def compare_reference_metadata(fields: dict, metadata: dict) -> dict:
    """One conservative identity comparison for Writing and Literature audit.

    Exact normalized titles match. The existing Literature distance threshold
    detects clearly unrelated titles; close variants stay unknown. Incomplete
    or abbreviated author lists never acquire a complete-authorship stamp.
    """
    comparisons = {}
    if fields.get("title"):
        supplied, actual = _title(fields["title"]), _title(metadata.get("title") or "")
        comparisons["title"] = ("unavailable" if not actual else "match" if supplied == actual else
                                "mismatch" if SequenceMatcher(None, supplied, actual).ratio() < 0.78
                                else "unavailable")
    if fields.get("author"):
        supplied = _authors(fields["author"])
        actual = metadata.get("families") or []
        partial = bool(supplied and supplied[-1] == "others")
        if partial:
            supplied.pop()
        author_checks = []
        for written, family in zip(supplied, actual):
            written = written.split(",")[0] if "," in written else written
            w, f = _text(written), _text(family)
            author_checks.append("unavailable" if not (w and f) else "match" if w.endswith(f) else
                                 "unavailable" if f.endswith(w) else "mismatch")
        if "mismatch" in author_checks:
            comparisons["author"] = "mismatch"
        elif partial or len(supplied) != len(actual) or not author_checks or "unavailable" in author_checks:
            comparisons["author"] = "unavailable"
        else:
            comparisons["author"] = "match"
    status = ("mismatch" if "mismatch" in comparisons.values() else
              "unavailable" if not comparisons or "unavailable" in comparisons.values() else "resolved")
    return {"status": status, "metadata": metadata, "comparisons": comparisons}


async def resolve_reference(kind: str, identifier: str, fields: dict) -> dict:
    """Return resolved/unavailable/invalid/mismatch plus the observed evidence.

    Missing metadata stays unknown. Nothing here decides whether a draft may be
    delivered, or whether a publication supports the sentence that cites it.
    """
    fact = {"status": "unavailable", "kind": kind, "identifier": identifier,
            "scope": "identifier" if kind == "url" else "bibliographic_identity"}
    if not identifier:
        return {**fact, "status": "invalid", "reason": "missing identifier"}
    if kind == "doi":
        identifier = normalize_doi(identifier)
        if not re.fullmatch(r"10\.\d{4,9}/\S+", identifier):
            return {**fact, "status": "invalid", "reason": "malformed DOI"}
        url = "https://doi.org/" + quote(identifier, safe="/")
    elif kind == "arxiv":
        url = "https://export.arxiv.org/api/query?id_list=" + quote(identifier, safe="")
    elif kind == "url":
        try:
            parts = urlsplit(identifier)
            if parts.scheme not in ("http", "https") or not parts.netloc:
                raise ValueError("unsupported URL")
            httpx.URL(identifier)
        except (ValueError, httpx.InvalidURL):
            return {**fact, "status": "invalid", "reason": "malformed URL"}
        url = identifier
    else:
        return {**fact, "status": "invalid", "reason": "unsupported or malformed identifier"}
    fact["url"] = url
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=12.0) as client:
            headers = {"User-Agent": "harness-literature/1.0"}
            if kind == "doi":
                headers["Accept"] = "application/vnd.citationstyles.csl+json"
            response = await client.get(url, headers=headers)
            fact["http_status"] = response.status_code
            fact["response_url"] = str(response.url)
            if response.status_code in (404, 410):
                # A publisher's broken landing page is not an absent DOI.
                invalid = kind != "doi" or response.url.host in ("doi.org", "dx.doi.org")
                return {**fact, "status": "invalid" if invalid else "unavailable"}
            if not 200 <= response.status_code < 300:
                return fact
            if kind == "url":
                return {**fact, "status": "resolved", "metadata_comparison": "not_performed"}
            if kind == "doi":
                item = response.json()
                metadata = {"title": item.get("title") or "",
                            "families": [a.get("family") or a.get("literal") or "" for a in item.get("author", [])],
                            "doi": item.get("DOI", "")}
                if metadata["doi"] and normalize_doi(metadata["doi"]) != identifier:
                    return {**fact, "status": "mismatch", "metadata": metadata,
                            "comparisons": {"doi": "mismatch"}}
            else:
                from .search_engines import ArxivSearch
                papers = ArxivSearch(client)._parse(response.text)
                papers = [p for p in papers if p.url.rstrip("/").rsplit("/abs/", 1)[-1] == identifier
                          or re.sub(r"v\d+$", "", p.url.rsplit("/abs/", 1)[-1]) == identifier]
                if not papers:
                    return {**fact, "reason": "no matching arXiv metadata"}
                paper = papers[0]
                metadata = {"title": paper.title,
                            "families": [a.split()[-1] for a in paper.authors]}
            return {**fact, **compare_reference_metadata(fields, metadata)}
    except (httpx.HTTPError, ValueError, TypeError, AttributeError, KeyError, IndexError, ParseError) as exc:
        return {**fact, "reason": type(exc).__name__}

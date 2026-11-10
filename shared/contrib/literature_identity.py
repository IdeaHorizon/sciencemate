"""Conservative publication identities shared by retrieval and federation."""
from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping


def normalize_doi(value: object) -> str:
    text = str(value or "").strip()
    text = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", text, flags=re.I)
    text = re.sub(r"^doi\s*:\s*", "", text, flags=re.I).strip().casefold()
    return "" if text.startswith("hash:") else text


def normalize_title(value: object) -> str:
    # Keep Chinese, complete suffixes and scientific punctuation (e.g. C++/C#).
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    # Publishers differ on word-joining hyphens; keep numeric signs and charges.
    text = re.sub(r"(?<=[^\W\d_])[-‐‑‒–—](?=[^\W\d_])", " ", text)
    return " ".join(text.split())


def _get(record, key):
    return record.get(key) if isinstance(record, Mapping) else getattr(record, key, None)


def paper_identity(record) -> str:
    doi = normalize_doi(_get(record, "doi"))
    title = normalize_title(_get(record, "title"))
    return doi or ("title:" + title if title else "")


class PublicationIdentityIndex:
    """Match DOI first; title/year only if it identifies one compatible record.

    Two explicit different DOIs are never merged just because titles agree.
    Ambiguous title-only hits stay separate rather than choosing an arbitrary DOI.
    """
    def __init__(self):
        self.dois = {}
        self.titles = {}

    def find(self, record):
        doi = normalize_doi(_get(record, "doi"))
        if doi and doi in self.dois:
            return self.dois[doi]
        title = normalize_title(_get(record, "title"))
        key = (title, str(_get(record, "year") or ""))
        matches = [p for p in self.titles.get(key, [])
                   if not doi or not normalize_doi(_get(p, "doi"))
                   or normalize_doi(_get(p, "doi")) == doi]
        return matches[0] if title and len(matches) == 1 else None

    def remember(self, record, *, alias=None):
        for value in (record, alias):
            if value is None:
                continue
            doi = normalize_doi(_get(value, "doi"))
            if doi:
                self.dois[doi] = record
            title = normalize_title(_get(value, "title"))
            if title:
                key = (title, str(_get(value, "year") or ""))
                rows = self.titles.setdefault(key, [])
                if not any(p is record for p in rows):
                    rows.append(record)

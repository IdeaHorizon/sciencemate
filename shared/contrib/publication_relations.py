"""Recognize supporting files without treating studies about them as files."""
import re
from collections.abc import Mapping


def supplement_relation(paper) -> dict | None:
    get = paper.get if isinstance(paper, Mapping) else lambda k, default=None: getattr(paper, k, default)
    provenance = get("metadata_provenance") or {}
    relation = provenance.get("relations") or {}
    parents = relation.get("is-supplement-to") or []
    parent = next((str(p.get("id") or "") for p in parents
                   if isinstance(p, dict) and str(p.get("id-type") or "").lower() == "doi"), "")
    title = str(get("title") or "")
    prefix = re.match(r"^(?:supplement(?:al|ary) (?:material|data|information)|supporting information)\s+(?:for|to)\s*[:：]?\s*(.+)", title, re.I)
    kind = str(get("pub_type") or provenance.get("publication_type") or "").lower()
    if not parent and not prefix and kind not in {"supplementary-material", "supplementary material"}:
        return None
    doi = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", str(get("doi") or ""), flags=re.I)
    if not parent and prefix and doi.lower().endswith(".supp"):
        parent = doi[:-5]
    return {"relation": "is-supplement-to", "parent_doi": parent,
            "parent_title": prefix.group(1).strip() if prefix else ""}


def link_supplements(papers: list[dict]) -> None:
    """Keep the original article's supporting-file links in its metadata."""
    originals = {str(p.get("doi") or "").lower(): p for p in papers if p.get("doi") and not supplement_relation(p)}
    for paper in papers:
        relation = supplement_relation(paper)
        if not relation:
            continue
        paper.setdefault("metadata_provenance", {})["supplement_relation"] = relation
        original = originals.get(relation["parent_doi"].lower())
        if original is not None:
            supporting = original.setdefault("metadata_provenance", {}).setdefault("supplements", [])
            item = {k: paper.get(k) for k in ("doi", "title", "url")}
            if item not in supporting:
                supporting.append(item)

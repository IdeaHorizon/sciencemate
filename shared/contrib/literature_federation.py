"""Pure result federation shared by platform and literature adapters.

Preserve source order; private catalogues do not acquire invented public scores.
"""
from __future__ import annotations
from .literature_provenance import selected_metadata_provenance
from .literature_identity import PublicationIdentityIndex


def merge_channels(*channels: list[dict], limit: int | None = 200) -> list[dict]:
    """Match the search UI's DOI-or-title/year dedup and round-robin order."""
    papers, identities = [], PublicationIdentityIndex()
    for row in range(max((len(c) for c in channels), default=0)):
        for channel in channels:
            if row >= len(channel):
                continue
            item = channel[row]
            existing = identities.find(item)
            if existing is not None:
                previous = dict(existing)
                for field in ("doi", "abstract", "abstract_zh", "title_zh", "venue", "url", "school_fulltext_url"):
                    if not existing.get(field) and item.get(field):
                        existing[field] = item[field]
                existing["metadata_provenance"] = selected_metadata_provenance(previous, item, existing)
                existing["school_holdings"] = list(dict.fromkeys([*(existing.get("school_holdings") or []), *(item.get("school_holdings") or [])]))
                identities.remember(existing, alias=item)
            else:
                copy = dict(item, federated_rank=len(papers))
                papers.append(copy)
                identities.remember(copy)
    return papers[:limit]


def merge_school_papers(result: dict, school_papers: list[dict], *, limit: int | None) -> dict:
    """Interleave school-only hits without inventing a comparable quality score."""
    merged = dict(result)
    public_papers = [dict(paper) for paper in result.get("papers") or []]
    identities = PublicationIdentityIndex()
    for paper in public_papers:
        identities.remember(paper)
    school_only = []
    added = 0
    for incoming in school_papers:
        incoming = dict(incoming)
        current = identities.find(incoming)
        if current is None:
            school_only.append(incoming)
            identities.remember(incoming)
            added += 1
            continue
        previous = dict(current)
        current["school_holdings"] = list(
            dict.fromkeys([*(current.get("school_holdings") or []), *incoming["school_holdings"]])
        )
        if incoming["school_fulltext_url"]:
            current["school_fulltext_url"] = incoming["school_fulltext_url"]
        for field in ("doi", "abstract", "authors", "venue", "year", "pub_date", "url"):
            if not current.get(field) and incoming.get(field):
                current[field] = incoming[field]
        current["metadata_provenance"] = selected_metadata_provenance(previous, incoming, current)
        identities.remember(current, alias=incoming)
    # Each source keeps its own order. Give a school-only hit one slot after
    # every four public hits, so a full public result does not hide the school
    # catalogue. When either source runs out, fill the remaining slots from
    # the other. The federated position is presentation order, not a quality
    # score comparable across two different search systems.
    papers = []
    public_index = school_index = public_since_school = 0
    cap = len(public_papers) + len(school_only) if limit is None else max(1, int(limit))
    while len(papers) < cap and (public_index < len(public_papers) or school_index < len(school_only)):
        if school_index < len(school_only) and (
            public_index >= len(public_papers) or public_since_school >= 4
        ):
            papers.append(school_only[school_index])
            school_index += 1
            public_since_school = 0
        else:
            papers.append(public_papers[public_index])
            public_index += 1
            public_since_school += 1
    if school_papers:
        for position, paper in enumerate(papers):
            paper["federated_rank"] = position
    merged["papers"] = papers
    merged["total"] = len(merged["papers"])
    merged["candidate_count"] = (
        int(result.get("candidate_count") or len(result.get("papers") or [])) + added
    )
    counts = dict(result.get("source_counts") or {})
    if school_papers:
        counts["school_library"] = len(school_papers)
    merged["source_counts"] = counts
    return merged

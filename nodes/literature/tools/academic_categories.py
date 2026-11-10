"""Independent result budgets and provider filters for search display categories."""
from shared.contrib.literature_identity import PublicationIdentityIndex

CATEGORIES = ("期刊文章", "中文期刊", "学位论文", "预印本", "书籍", "会议文章", "其他")
CROSSREF_TYPES = {
    "期刊文章": ("journal-article",), "中文期刊": ("journal-article",),
    "学位论文": ("dissertation",), "预印本": ("posted-content",),
    "书籍": ("book", "book-chapter", "monograph", "edited-book", "reference-book"),
    "会议文章": ("proceedings-article",),
    "其他": ("report", "dataset", "standard", "other"),
}
OPENALEX_TYPES = {
    "期刊文章":"article|review|letter|editorial", "中文期刊":"article|review|letter|editorial",
    "学位论文":"dissertation", "预印本":"preprint", "书籍":"book|book-chapter",
    "会议文章":"conference-paper|conference-abstract", "其他":"dataset|report|standard|other|paratext|reference-entry",
}


def sources_for_category(sources, category):
    if category is None:
        return sources
    compatible = {"crossref", "openalex"}
    if category in {"期刊文章", "中文期刊"}:
        compatible.add("pubmed")
    if category == "期刊文章":
        compatible.update(("semantic_scholar", "google_scholar"))
    if category == "预印本":
        compatible.update(("arxiv", "biorxiv", "medrxiv"))
    if category == "中文期刊":
        # Crossref has no language filter; Chinese hits from its journal lane
        # still count here. CNKI and OpenAlex provide direct Chinese recall.
        compatible.discard("crossref")
    return set(sources) & compatible


def apply_budget(papers, accepted, limit):
    """Count unique admitted records, across pages and all query groups."""
    identity = PublicationIdentityIndex()
    records = {category: list(accepted.get(category, [])) for category in CATEGORIES}
    for category, rows in records.items():
        for row in rows:
            identity.remember(row)
    output = []
    for paper in papers:
        category = paper.get("publication_category", "其他")
        if category not in records:
            category = "其他"
        existing = identity.find(paper)
        if existing is not None:
            for key in ("doi", "title", "year"):
                if not existing.get(key) and paper.get(key):
                    existing[key] = paper[key]
            identity.remember(existing, alias=paper)
            output.append(paper)  # Metadata updates do not consume a new place.
            continue
        if len(records[category]) >= limit:
            continue
        record = {key:paper.get(key) for key in ("doi", "title", "year")}
        records[category].append(record)
        identity.remember(record)
        output.append(paper)
    return output, records


def read_budget(continuation, limit):
    accepted = (continuation or {}).get("accepted", {})
    if not isinstance(accepted, dict) or set(accepted) - set(CATEGORIES):
        raise ValueError("Invalid category budget")
    if continuation and continuation.get("per_category_limit") != limit:
        raise ValueError("Changed category budget")
    for rows in accepted.values():
        if not isinstance(rows, list) or len(rows) > limit:
            raise ValueError("Invalid category count")
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("title"), str) or len(row["title"]) > 2000:
                raise ValueError("Invalid retained identity")
            if row.get("doi") is not None and (not isinstance(row["doi"], str) or len(row["doi"]) > 500):
                raise ValueError("Invalid retained DOI")
    return accepted

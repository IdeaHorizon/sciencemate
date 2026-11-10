"""Search display categories, using original metadata rather than translations."""
from collections.abc import Mapping
import re
from shared.contrib.publication_relations import supplement_relation


def published_journal_metadata(paper):
    """Positive publication evidence, independent of the retrieval origin.

    Paper.pub_type has a journal default, so it alone cannot upgrade a preprint.
    """
    get = paper.get if isinstance(paper, Mapping) else lambda key, default=None: getattr(paper, key, default)
    provenance = get("metadata_provenance") or {}
    source = str(get("source", "") or "").lower().split("/", 1)[0]
    kind = str(provenance.get("publication_type") or get("pub_type") or "").lower()
    if kind not in {"journal", "journal-article", "article", "article-journal", "review", "letter", "期刊"}:
        return None
    collection = str(provenance.get("source_collection") or "").strip()
    if provenance.get("source_type") == "journal" and collection:
        return {"venue": collection, "pub_type": kind}
    if source in {"crossref", "pubmed"} and get("venue") and provenance.get("publication_type"):
        return {"venue": get("venue"), "pub_type": kind}
    return None


def publication_category(paper) -> str:
    get = paper.get if isinstance(paper, Mapping) else lambda key, default=None: getattr(paper, key, default)
    source = str(get("source", "") or "").lower().split("/", 1)[0]
    provenance = get("metadata_provenance") or {}
    kind = str(get("document_type") or provenance.get("publication_type", get("pub_type")) or "").lower()
    if supplement_relation(paper):
        return "其他"
    if any(word in kind for word in ("thesis", "dissertation", "硕士", "博士", "学位")):
        return "学位论文"
    if (source in {"arxiv", "biorxiv", "medrxiv"} or kind in {"preprint", "posted-content"}) and not published_journal_metadata(paper):
        return "预印本"
    if any(word in kind for word in ("book", "书籍", "图书", "专著")) or kind == "monograph":
        return "书籍"
    if any(word in kind for word in ("proceedings", "conference", "会议")):
        return "会议文章"
    journal_kinds = {"journal", "journal-article", "article", "article-journal", "review", "letter", "期刊"}
    if source == "openalex" and provenance.get("source_type") == "repository" and kind in journal_kinds:
        # OpenAlex's generic 'article' can include repository dissertations.
        # A named thesis collection is evidence; a university/repository name
        # alone is not. Keep uncertain records out of the journal tab.
        collection = str(provenance.get("source_collection") or "")
        if re.search(r"\b(?:theses|dissertations|etds?)\b|学位论文", collection, re.I):
            return "学位论文"
        if not provenance.get("journal_source_present") and not get("issn") and not get("eissn"):
            return "其他"
    if source == "pubmed" or kind in journal_kinds:
        provenance = get("metadata_provenance") or {}
        language = str(get("language") or provenance.get("language") or "").lower().replace("_", "-")
        if language:
            chinese = language.split("-", 1)[0] in {"zh", "zho", "chi", "chinese", "中文", "汉语"}
        else:
            # CNKI/Crossref may omit language. Use the original title only;
            # title_zh is a generated translation and must never affect the tab.
            title = str(get("title", "") or "")
            chinese = bool(re.search(r"[\u3400-\u9fff]", title)) and not bool(re.search(r"[\u3040-\u30ff]", title))
        return "中文期刊" if chinese else "期刊文章"
    return "其他"

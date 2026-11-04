"""Search display categories, using original metadata rather than translations."""
from collections.abc import Mapping
import re


def publication_category(paper) -> str:
    get = paper.get if isinstance(paper, Mapping) else lambda key, default=None: getattr(paper, key, default)
    source = str(get("source", "") or "").lower().split("/", 1)[0]
    kind = str(get("document_type") or get("pub_type") or "").lower()
    if any(word in kind for word in ("thesis", "dissertation", "硕士", "博士", "学位")):
        return "学位论文"
    if source in {"arxiv", "biorxiv", "medrxiv"} or kind in {"preprint", "posted-content"}:
        return "预印本"
    if "book" in kind or kind in {"书籍", "图书"}:
        return "书籍"
    if any(word in kind for word in ("proceedings", "conference", "会议")):
        return "会议文章"
    if source == "pubmed" or kind in {"journal", "journal-article", "article", "article-journal", "期刊"}:
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

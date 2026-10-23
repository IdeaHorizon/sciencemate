"""期刊只是检索入口；较窄的二级学科还需论文主题证据。"""
from __future__ import annotations

import csv
import html
import json
import os
import re
import unicodedata
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def journal_display_name(value: object) -> str:
    text = str(value or "")
    # Some sources escape an already escaped name. Bound decoding to avoid
    # unbounded work on malformed upstream metadata.
    for _ in range(4):
        decoded = html.unescape(text)
        if decoded == text:
            break
        text = decoded
    return " ".join(text.split())


def journal_key(value: object) -> str:
    text = journal_display_name(value).lower()
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"^the\b[\s\W_]*", "", text)
    return re.sub(r"[^a-z0-9]", "", text)


@lru_cache(maxsize=4)
def _rules(path: str, modified: int) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))["rules"]


def rules() -> dict:
    path = Path(__file__).with_name("journal_domain_scope_rules.json")
    return _rules(str(path), path.stat().st_mtime_ns)


def scope_matches(code: str, venue: str, title: str, abstract: str = "") -> bool:
    rule = rules().get(code)
    if not rule:
        return True
    key = journal_key(venue)
    if key in {journal_key(v) for v in rule.get("direct_journals", [])}:
        return True
    limited = rule.get("only_journals")
    if limited is not None and key not in {journal_key(v) for v in limited}:
        return True
    # 刊名不作为论文主题证据；去掉 HTML 标签避免匹配摘要中的标记。
    text = html.unescape(re.sub(r"<[^>]+>", " ", f"{title} {abstract}"))
    return all(re.search(pattern, text, re.I) for pattern in rule["all_patterns"])


@lru_cache(maxsize=4)
def _mapping(path: str, modified: int) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    with Path(path).open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle, delimiter="\t"):
            if row.get("分类状态") in {"rejected", "scope_pending", "identity_verified"}:
                continue
            result.setdefault(journal_key(row.get("journal_name")), set()).add(
                row["二级学科代码"]
            )
    return result


def current_domains(venue: str, codes, title: str, abstract: str = "") -> tuple[str, ...]:
    """重算旧映射产物的送达学科，防止删除的关系靠旧标签继续推荐。"""
    path = Path(os.environ.get("HARNESS_JOURNAL_DOMAIN_MAP") or ROOT / "docs" / "xiaohongshu_journal_map.tsv")
    mapped = _mapping(str(path), path.stat().st_mtime_ns).get(journal_key(venue), set())
    return tuple(dict.fromkeys(
        str(code) for code in codes
        if str(code) in mapped and scope_matches(str(code), venue, title, abstract)
    ))

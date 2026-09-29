"""Project the shared literature catalog into the feed read model.

The per-paper ``index.json`` files and their SQLite catalog remain the paper
fact source.  ``feed_items`` only carries the small, rebuildable projection the
feed needs for stable identities, visibility and engagement foreign keys.
"""

from __future__ import annotations

import html
import json
import os
import re
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.feed import FeedItem, FeedItemKind, FeedVisibility


def literature_papers_root() -> Path:
    """Return the same shared paper root used by the literature node.

    The platform used to derive this path from ``PLATFORM_DATA_ROOT`` while the
    literature node derives it from ``core.paths``. That split made harvesting
    succeed while every projection round saw another (usually empty) catalog.
    """
    configured = os.environ.get("HARNESS_LITERATURE_HOME", "").strip()
    if configured:
        literature_home = Path(configured).expanduser()
        return literature_home.resolve() / "papers"

    from core import paths as harness_paths

    return harness_paths.literature_papers_dir().resolve()


def publication_datetime(raw: object, year: object = None) -> datetime | None:
    """Parse source dates without treating the harvest time as publication time.

    Crossref commonly emits unpadded values such as ``2026-8-5``.  Month/year
    precision is kept conservative by using the first day of that period.
    """
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=UTC)
    text = " ".join(str(raw or "").strip().split())
    if text:
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        except ValueError:
            pass
        date_part = text.split("T", 1)[0].replace("/", "-").replace(".", "-")
        parts = date_part.split("-")
        try:
            if len(parts) >= 3 and all(part.isdigit() for part in parts[:3]):
                return datetime(int(parts[0]), int(parts[1]), int(parts[2]), tzinfo=UTC)
            if len(parts) >= 2 and all(part.isdigit() for part in parts[:2]):
                return datetime(int(parts[0]), int(parts[1]), 1, tzinfo=UTC)
            if len(parts) == 1 and parts[0].isdigit():
                return datetime(int(parts[0]), 1, 1, tzinfo=UTC)
        except ValueError:
            pass
    try:
        return datetime(int(year), 1, 1, tzinfo=UTC)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True, slots=True)
class ProjectionReport:
    seen: int = 0
    stored: int = 0
    duplicates: int = 0
    rejected: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "seen": self.seen,
            "stored": self.stored,
            "duplicates": self.duplicates,
            "rejected": self.rejected,
        }


def _metadata(raw: object) -> dict:
    try:
        parsed = json.loads(str(raw or "{}"))
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _canonical_key(catalog_key: str, metadata: dict) -> str:
    rss_key = str(metadata.get("rss_canonical_key") or "").strip()
    if rss_key:
        return rss_key
    normalized = catalog_key.strip().lower()
    return normalized if normalized.startswith("url:") else f"doi:{normalized}"


def _plain_text(value: object) -> str:
    """Remove JATS/HTML wrappers and normalize source line wrapping."""
    without_tags = re.sub(r"<[^>]+>", " ", str(value or ""))
    return " ".join(html.unescape(without_tags).split())


def _authors(value: object) -> list[str]:
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    if not isinstance(value, list):
        return []
    result = []
    for author in value:
        if isinstance(author, dict):
            name = f"{author.get('given', '')} {author.get('family', '')}".strip()
        else:
            name = str(author).strip()
        if name:
            result.append(name)
    return result


async def project_literature_catalog(db: AsyncSession, *, limit: int = 50) -> ProjectionReport:
    """Refresh a bounded batch of feed rows from the shared catalog."""
    catalog = literature_papers_root() / "literature_catalog.sqlite3"
    if not catalog.is_file():
        return ProjectionReport()
    try:
        with sqlite3.connect(catalog, timeout=5) as conn:
            rows = conn.execute(
                """SELECT doi,title,source,year,article_dir,metadata_json,
                          published_at,updated_at
                     FROM papers ORDER BY updated_at DESC LIMIT ?""",
                (max(1, int(limit)),),
            ).fetchall()
    except sqlite3.Error:
        return ProjectionReport()
    if not rows:
        return ProjectionReport()

    from nodes.literature.tools.journal_policy import JournalDecision, decide

    prepared: list[tuple] = []
    for row in rows:
        catalog_key, title, source, year, article_dir, raw, published_at, updated_at = row
        metadata = _metadata(raw)
        canonical_key = _canonical_key(str(catalog_key or ""), metadata)
        if not canonical_key or not str(title or "").strip():
            continue
        routes = {
            str(route) for route in (metadata.get("feed_acquisition_routes") or [])
            if str(route)
        }
        routed_domains = tuple(dict.fromkeys(
            str(code) for code in (metadata.get("second_level_domains") or [])
            if str(code)
        ))
        # 课题路线（project_profile / web）**和用户直接订阅的路线**（scholar，
        # 以及明确标了 subscription_requested 的期刊订阅）都不走期刊映射判定：
        # 它们由用户点名的检索请求产生，靠 profile_user_ids + 检索相关度准入，
        # 不属于「按离线期刊映射表扫出来的本领域动态」，也不该被
        # journal_policy.decide 以「不是期刊文章」拒掉。
        #
        # scholar 路线此前落进下面的 else 分支，于是「订阅了学者」采集回来的东西
        # 全部被投影丢弃 —— 症状是订阅学者永远没有内容，而采集日志显示"archived
        # N item(s)"（2026-09-21 实测：库里有 7 条 route=scholar，feed_items 里 0 条）。
        #
        # journal 路线的例外条件仍然成立：只有喂了 journal_harvest_verified 的
        # 期刊映射产物才免检，普通 journal 记录照旧过 decide。
        if (
            "project_profile" in routes
            or "web" in routes
            or "scholar" in routes
            or metadata.get("subscription_requested") is True
            or ("journal" in routes and metadata.get("journal_harvest_verified") is True)
        ):
            decision = JournalDecision(
                True,
                str(metadata.get("classification_mode") or "feed_acquisition_route"),
                routed_domains,
            )
        else:
            decision = decide(
                venue=str(metadata.get("venue") or ""),
                source=str(metadata.get("source") or source or ""),
                pub_type=str(metadata.get("pub_type") or ""),
                title=str(title or ""),
            )
        prepared.append(
            (
                row,
                metadata,
                canonical_key,
                decision,
                publication_datetime(
                    published_at
                    or metadata.get("pub_date")
                    or metadata.get("publication_date")
                    or metadata.get("published_at"),
                    year,
                ),
            )
        )

    keys = [entry[2] for entry in prepared]
    existing = {}
    if keys:
        existing = {
            item.canonical_key: item
            for item in (await db.execute(select(FeedItem).where(FeedItem.canonical_key.in_(keys))))
            .scalars()
            .all()
        }

    stored = duplicates = rejected = 0
    for entry in prepared:
        row, metadata, canonical_key, decision, published = entry
        catalog_key, title, _source, _year, article_dir, _raw, _published, updated_at = row
        item = existing.get(canonical_key)
        if not decision.accepted:
            rejected += 1
            if item is not None and item.kind == FeedItemKind.PAPER:
                item.extra = {
                    **(item.extra or {}),
                    "literature_catalog": True,
                    "literature_eligible": False,
                    "catalog_key": str(catalog_key).lower(),
                }
            continue

        # The policy decision, not stale metadata copied into the catalog, owns
        # the delivery address.  This is what makes a corrected mapping take
        # effect on the next projection round.
        domains = list(dict.fromkeys(str(value) for value in decision.domains if value))
        real_doi = "" if str(catalog_key).startswith("url:") else str(catalog_key).lower()
        url = str(metadata.get("url") or "").strip()
        if not url and real_doi:
            url = f"https://doi.org/{real_doi}"
        extra = {
            **(item.extra if item is not None and isinstance(item.extra, dict) else {}),
            "literature_catalog": True,
            "literature_eligible": True,
            "doi": real_doi,
            "catalog_key": str(catalog_key).lower(),
            "article_dir": str(article_dir or ""),
            "classification_mode": decision.reason,
            "feed_acquisition_routes": sorted(
                str(route) for route in (metadata.get("feed_acquisition_routes") or [])
                if str(route)
            ),
            "profile_user_ids": list(metadata.get("profile_user_ids") or []),
            "cas_quartile": metadata.get("cas_quartile"),
            "jcr_quartile": metadata.get("jcr_quartile"),
            "citations": int(metadata.get("citations") or 0),
            "source": str(metadata.get("source") or _source or ""),
            "discovery_query": str(metadata.get("discovery_query") or ""),
            "profile_query_relevance": metadata.get("profile_query_relevance"),
            "web_search_backend": str(metadata.get("web_search_backend") or ""),
            "web_authority": metadata.get("web_authority"),
            "web_traceability": metadata.get("web_traceability"),
            "discovered_at": str(metadata.get("discovered_at") or ""),
            # 保留来源真实日期精度。YYYY-MM 在目录中表示“本月发表、日未知”，
            # 不能在投影后被误当成当月 1 日并被七天候选窗口提前淘汰。
            "publication_date_precision": str(
                metadata.get("publication_date_precision") or ""
            ),
        }
        # web 路线是「资讯/新闻」通道，不是论文：网页发现的新闻、博客、行业
        # 动态不该套「论文」徽章。project_profile / journal 才是 paper 形态。
        routes = {
            str(route) for route in (metadata.get("feed_acquisition_routes") or [])
            if str(route)
        }
        kind = FeedItemKind.NEWS if "web" in routes else FeedItemKind.PAPER
        values = {
            "kind": kind,
            "title": _plain_text(title)[:2000],
            "url": url or None,
            "summary": _plain_text(metadata.get("abstract"))[:1200] or None,
            "authors": _authors(metadata.get("authors"))[:50],
            "venue": str(metadata.get("venue") or "")[:200] or None,
            "published_at": published,
            "domains": domains,
            "visibility": FeedVisibility.PLATFORM,
            "extra": extra,
        }
        if item is None:
            item = FeedItem(
                id=str(uuid.uuid5(uuid.NAMESPACE_URL, "literature:" + str(catalog_key).lower())),
                canonical_key=canonical_key,
                **values,
            )
            try:
                if updated_at:
                    item.created_at = datetime.fromtimestamp(float(updated_at), tz=UTC)
            except (TypeError, ValueError, OSError):
                pass
            db.add(item)
            existing[canonical_key] = item
            stored += 1
        else:
            for field, value in values.items():
                setattr(item, field, value)
            duplicates += 1

    await db.flush()
    return ProjectionReport(seen=len(rows), stored=stored, duplicates=duplicates, rejected=rejected)

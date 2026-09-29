"""The literature catalog/feed boundary keeps facts singular and IDs actionable."""

from __future__ import annotations

import base64
from datetime import UTC, datetime

import pytest
from httpx import ASGITransport, AsyncClient
from nodes.literature.tools.journal_policy import reset_cache
from nodes.literature.tools.literature_catalog import upsert_paper
from sqlalchemy import select

from app.auth import get_current_user
from app.database import get_db
from app.main import app
from app.models.feed import FeedEngagement, FeedItem
from app.models.user import User
from app.services.feed.literature_projection import (
    project_literature_catalog,
    publication_datetime,
)
from app.services.literature_harvester import (
    _journal_targets,
    _mapping_confidence_rank,
    _normal_journal,
    _recent_publication,
)

#: 一张**真的** 320×180 JPEG（base64）。为什么要内嵌见 `_write_fixture_jpeg`。
_FIXTURE_JPEG_B64 = (
    "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAoHBwgHBgoICAgLCgoLDhgQDg0NDh0VFhEYIx8lJCIfIiEmKzcvJik0KSEiMEEx"
    "NDk7Pj4+JS5ESUM8SDc9Pjv/2wBDAQoLCw4NDhwQEBw7KCIoOzs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7Ozs7"
    "Ozs7Ozs7Ozs7Ozs7Ozv/wAARCAC0AUADASIAAhEBAxEB/8QAHwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAA"
    "AgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIhMUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6"
    "Q0RFRkdISUpTVFVWV1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXG"
    "x8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEBAQEBAQAAAAAAAAECAwQFBgcICQoL/8QAtREA"
    "AgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAVYnLRChYkNOEl8RcYGRomJygpKjU2Nzg5"
    "OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOEhYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPE"
    "xcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq8vP09fb3+Pn6/9oADAMBAAIRAxEAPwCWiiivlT6UKKKKACiiigAooooAKKKKACii"
    "igAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACii"
    "igAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACii"
    "igAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACii"
    "igAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACii"
    "igAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACii"
    "igAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACii"
    "igAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACii"
    "igAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACii"
    "igAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACii"
    "igAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACii"
    "igAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACii"
    "igAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACii"
    "igAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACiiigAooooAKKKKACii"
    "igD/2Q=="
)


def _write_fixture_jpeg(path: Path) -> None:
    """写一张真的、够大的 JPEG 到 `path`。

    feed 端点用 Pillow 校验本地插图（`_valid_figure`），但那里把 Pillow 当**可选**
    依赖 —— 缺失时它放宽（见该函数的 ImportError 分支），也就是说 CI 环境里没有
    Pillow。测试自己 `from PIL import Image` 会让整个 job 在**收集阶段**就红
    （实测 CI：1636 passed / 1 error）。所以图直接内嵌：有没有 Pillow 都能跑，
    而验的仍是同一件事 ——「有真图就原样吐出，不回退到生成封面」。
    """
    path.write_bytes(base64.b64decode(_FIXTURE_JPEG_B64))


_ME = "aaaa1111-1111-4111-8111-111111111111"


def test_source_dates_keep_real_precision_instead_of_becoming_harvest_time() -> None:
    assert publication_datetime("2026-8-5") == datetime(2026, 8, 5, tzinfo=UTC)
    assert publication_datetime("2026/08") == datetime(2026, 8, 1, tzinfo=UTC)
    assert publication_datetime("", 2024) == datetime(2024, 1, 1, tzinfo=UTC)
    assert publication_datetime("not-a-date") is None


def test_feed_recency_respects_source_date_precision() -> None:
    now = datetime(2026, 9, 10, 12, tzinfo=UTC)
    cutoff = datetime(2026, 9, 3, 12, tzinfo=UTC)

    assert _recent_publication(
        "2026-09-08", 2026, now=now, cutoff=cutoff
    )[1:] == (True, "day")
    assert _recent_publication(
        "2026-09", 2026, now=now, cutoff=cutoff
    )[1:] == (True, "month")
    assert _recent_publication(
        "2026-08", 2026, now=now, cutoff=cutoff
    )[1:] == (False, "month")
    assert _recent_publication(
        "2026", 2026, now=now, cutoff=cutoff
    )[1:] == (False, "year")


def test_journal_name_normalization_accepts_an_optional_leading_article() -> None:
    assert _normal_journal("The British Accounting Review") == _normal_journal(
        "British Accounting Review"
    )
    assert _normal_journal("Theory and Decision") == "theoryanddecision"


def test_explicit_domain_mapping_beats_high_quartile_substring_noise(
    tmp_path, monkeypatch
) -> None:
    """映射证据决定归属，分区只在同等级证据内排序。"""
    mapping = tmp_path / "journal-map.tsv"
    header = (
        "journal_name\tcas_quartile\ttop\topen_access\trss_status\tissn\tpublisher"
        "\tdomains\t一级学科代码\t一级学科\t二级学科代码\t二级学科"
        "\t分类依据\t分类状态\t分类置信度\n"
    )
    noisy = "".join(
        f"Sports Reports {index}\t1\t是\t\t\t\t\t\t0815\t水利工程\t081505"
        "\t港口、海岸及近海工程\tjournal_title_anchor:port|sport"
        "\tspeculative\tmedium\n"
        for index in range(10)
    )
    explicit = (
        "COASTAL ENGINEERING\t2\t否\t\t\t0378-3839\tElsevier\t\t0815"
        "\t水利工程\t081505\t港口、海岸及近海工程"
        "\tjournal_title_water_relaxed:coastal engineering|port engineering"
        "\tspeculative\tlow-medium\n"
    )
    mapping.write_text(header + noisy + explicit, encoding="utf-8")
    monkeypatch.setenv("HARNESS_JOURNAL_DOMAIN_MAP", str(mapping))

    targets = _journal_targets({"081505"})

    assert "issn:0378-3839" in targets
    assert len(targets) == 1
    assert all("Sports Reports" not in target["journal"] for target in targets.values())


def test_mapping_confidence_is_ordered_before_journal_quality() -> None:
    assert _mapping_confidence_rank("high") < _mapping_confidence_rank("medium")
    assert _mapping_confidence_rank("medium") < _mapping_confidence_rank("low")


def test_hydrology_mapping_contains_core_hydrology_journals(monkeypatch) -> None:
    monkeypatch.delenv("HARNESS_JOURNAL_DOMAIN_MAP", raising=False)
    journals = {
        str(target["journal"]).casefold()
        for target in _journal_targets({"081501"}).values()
    }
    assert "journal of hydrology" in journals
    assert "water resources research" in journals
    assert "hydrological processes" in journals
    assert "hydrology and earth system sciences" in journals


def test_atmospheric_physics_mapping_contains_core_journals(monkeypatch) -> None:
    monkeypatch.delenv("HARNESS_JOURNAL_DOMAIN_MAP", raising=False)
    journals = {
        str(target["journal"]).casefold()
        for target in _journal_targets({"070602"}).values()
    }
    assert "atmospheric research" in journals
    assert "journal of the atmospheric sciences" in journals


@pytest.mark.asyncio
async def test_projected_paper_can_be_saved_and_serves_its_real_mime_type(
    db_session, tmp_path, monkeypatch
) -> None:
    literature_home = tmp_path / "literature"
    papers_root = literature_home / "papers"
    article = papers_root / "10.1234__actionable"
    figure = article / "figure" / "result.jpg"
    figure.parent.mkdir(parents=True)
    # 造一张**真的**、够大的 JPEG。feed 端点用 Pillow 校验本地插图（`_valid_figure`）：
    # 只有 magic bytes 的假 JPEG 打不开，会被判无效、回退到生成的 SVG 封面 —— 那样
    # 这个测试就验不到「有真图就原样吐出」这件事了。
    _write_fixture_jpeg(figure)

    mapping = tmp_path / "journal-map.tsv"
    mapping.write_text(
        "journal_name\t二级学科代码\t分类依据\t分类状态\n"
        "A Carefully Reviewed Journal\t070207\tmanual_source_verified\tmanually_verified\n"
        "A Guessed Journal\t010108\toffline_strict_title_anchor\tverified\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HARNESS_LITERATURE_HOME", str(literature_home))
    monkeypatch.setenv("HARNESS_JOURNAL_DOMAIN_MAP", str(mapping))
    reset_cache()

    now = datetime.now(UTC)
    upsert_paper(
        papers_root,
        {
            "doi": "10.1234/actionable",
            "title": "A stable paper card",
            "source": "crossref",
            "year": now.year,
            "venue": "A Carefully Reviewed Journal",
            "pub_type": "journal-article",
            "pub_date": now.date().isoformat(),
            "publication_date_precision": "day",
            "url": "https://doi.org/10.1234/actionable",
            "authors": ["Ada Example"],
            "abstract": "Source-provided abstract.",
            # Deliberately stale: the reviewed decision must own delivery.
            "second_level_domains": ["010108"],
            "article_dir": str(article),
            "image": {"status": "available", "path": str(figure), "source": "publisher"},
        },
    )
    upsert_paper(
        papers_root,
        {
            "doi": "10.1234/guessed",
            "title": "A guessed classification",
            "source": "crossref",
            "year": now.year,
            "venue": "A Guessed Journal",
            "pub_type": "journal-article",
            "pub_date": now.date().isoformat(),
            "url": "https://doi.org/10.1234/guessed",
            "article_dir": str(papers_root / "10.1234__guessed"),
        },
    )

    report = await project_literature_catalog(db_session, limit=20)
    assert report.seen == 2
    assert report.stored == 1
    assert report.rejected == 1

    item = await db_session.scalar(
        select(FeedItem).where(FeedItem.canonical_key == "doi:10.1234/actionable")
    )
    assert item is not None
    assert isinstance(item.id, str)
    assert item.domains == ["070207"]
    assert item.extra["publication_date_precision"] == "day"
    assert (
        await db_session.scalar(
            select(FeedItem).where(FeedItem.canonical_key == "doi:10.1234/guessed")
        )
        is None
    )

    user = User(
        id=_ME,
        email="reader@example.test",
        display_name="Reader",
        hashed_password="x",
        institution_id="inst-a",
        group_id="group-a",
    )
    db_session.add(user)
    await db_session.flush()

    async def override_get_db():
        yield db_session

    async def override_current_user() -> User:
        return user

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_current_user
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://feed") as client:
            saved = await client.post(
                f"/api/v1/feed/items/{item.id}/engagement", json={"action": "save"}
            )
            assert saved.status_code == 204, saved.text
            detail = await client.get(f"/api/v1/feed/items/{item.id}")
            assert detail.status_code == 200
            assert detail.json()["saved"] is True
            image = await client.get(f"/api/v1/feed/items/{item.id}/image")
            assert image.status_code == 200
            assert image.headers["content-type"].startswith("image/jpeg")
            assert image.content == figure.read_bytes()
    finally:
        app.dependency_overrides.clear()
        reset_cache()

    engagement = await db_session.scalar(
        select(FeedEngagement).where(FeedEngagement.item_id == item.id)
    )
    assert engagement is not None


@pytest.mark.asyncio
async def test_project_profile_items_enter_the_candidate_pool_without_a_domain(
    db_session,
) -> None:
    """project_profile / web 论文不标学科 domains（见 discovery._targeted_search），
    但用户订阅了 6 位学科时，candidate_items 的 domains 预过滤不能把它们整批滤掉
    —— 它们要凭 extra.profile_user_ids 指向这个用户进候选池。"""
    from datetime import timedelta

    from app.services.feed.profile import Profile
    from app.services.feed.ranking import candidate_items

    user_id = "aaaa1111-1111-4111-8111-111111111111"
    project_paper = FeedItem(
        id="bbbb2222-2222-4222-8222-222222222222",
        canonical_key="doi:10.1234/vehicle-dyn",
        kind="paper",
        title="Vehicle dynamics modeling",
        url="https://doi.org/10.1234/vehicle-dyn",
        summary="steer-by-wire chassis control and vehicle dynamics",
        authors=[],
        domains=[],  # project_profile 论文不标学科
        published_at=datetime.now(UTC) - timedelta(days=1),
        extra={
            "feed_acquisition_routes": ["project_profile"],
            "profile_user_ids": [user_id],
            "profile_query_relevance": 0.9,
        },
    )
    db_session.add(project_paper)
    await db_session.flush()

    profile = Profile(user_id=user_id, domains=("060105",))
    candidates = await candidate_items(db_session, profile=profile)

    assert any(str(c.id) == "bbbb2222-2222-4222-8222-222222222222" for c in candidates)


@pytest.mark.asyncio
async def test_web_routed_items_are_projected_not_rejected_as_non_journal(
    db_session, tmp_path, monkeypatch
) -> None:
    """web 路线（课题画像的公开网页发现）不经 journal_policy.decide 的
    「不是期刊文章」判定 —— 它的 pub_type=web-resource 会被 decide 误拒，
    导致网页搜索结果永远进不了 feed。"""
    literature_home = tmp_path / "literature"
    papers_root = literature_home / "papers"
    mapping = tmp_path / "journal-map.tsv"
    mapping.write_text(
        "journal_name\t二级学科代码\t分类依据\t分类状态\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HARNESS_LITERATURE_HOME", str(literature_home))
    monkeypatch.setenv("HARNESS_JOURNAL_DOMAIN_MAP", str(mapping))
    reset_cache()

    now = datetime.now(UTC)
    upsert_paper(
        papers_root,
        {
            "doi": "10.1234/web-resource",
            "title": "A steer-by-wire overview from the open web",
            "source": "web",
            "year": now.year,
            "venue": "example.com",
            "pub_type": "web-resource",
            "pub_date": now.date().isoformat(),
            "url": "https://example.com/steer-by-wire",
            "abstract": "Open-web overview of steer-by-wire fault-tolerant control.",
            "feed_acquisition_routes": ["web"],
            "profile_user_ids": [_ME],
            "second_level_domains": [],
            "classification_mode": "project_profile_web_search",
            "discovery_query": "steer-by-wire fault-tolerant control",
            "article_dir": str(papers_root / "10.1234__web-resource"),
        },
    )

    report = await project_literature_catalog(db_session, limit=20)
    assert report.stored == 1
    assert report.rejected == 0

    item = await db_session.scalar(
        select(FeedItem).where(FeedItem.canonical_key == "doi:10.1234/web-resource")
    )
    assert item is not None
    assert "web" in item.extra["feed_acquisition_routes"]
    assert item.domains == []
    assert item.kind == "news"  # 网页发现是「资讯」，不是「论文」
    reset_cache()

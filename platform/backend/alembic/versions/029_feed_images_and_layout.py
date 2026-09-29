"""资讯条目的图片，以及更多来源。

## 为什么图片是一列而不是"用的时候现抓"

现抓意味着渲染一屏 12 张卡就要发 12 个出站请求，而且每次刷新都重来。图片地址
在采集那一刻就在 feed 里（APS 的 key image、Phys.org 的 media:thumbnail），
采集时顺手存下来是零成本的。

## 为什么可以为空，而且很多条就是空

打真源查过（2026-08-23）：APS 系（PRL/PRX/PRB）和 Phys.org / Quanta /
MIT Tech Review 带图，而 **Nature 系、Science、arXiv 一个图片字段都没有**。
所以"有图"是少数派，界面必须把无图当成正常态，不是缺陷态。

Revision ID: 029_feed_images_and_layout
Revises: 028_delivery_block_evidence
"""

import sqlalchemy as sa
from alembic import op

revision = "029_feed_images_and_layout"
down_revision = "028_delivery_block_evidence"
branch_labels = None
depends_on = None

#: 打真源验过能用的新源（2026-08-23）。死掉的不收：Science Advances 返回
#: HTTP 410、Chemistry World 返回 404 —— 播一个 404 的源进去，只会让"源健康"
#: 那张表上永远挂着一条红的。
_NEW_SOURCES: tuple[dict, ...] = (
    # 预印本：物理之外的半边天。bioRxiv 用的也是 RSS 1.0(RDF)。
    {
        "kind": "rss", "name": "bioRxiv — 最新预印本",
        "config": {"url": "https://connect.biorxiv.org/biorxiv_xml.php?subject=all",
                   "venue": "bioRxiv"},
        "domains": ["q-bio.BM"], "poll_interval_seconds": 21600,
    },
    # APS 系：图最全（PRX 实测 100 条里 97 条带 key image）。
    {
        "kind": "rss", "name": "Physical Review X",
        "config": {"url": "http://feeds.aps.org/rss/recent/prx.xml", "venue": "Physical Review X"},
        "domains": [], "poll_interval_seconds": 43200,
    },
    {
        "kind": "rss", "name": "Physical Review B",
        "config": {"url": "http://feeds.aps.org/rss/recent/prb.xml", "venue": "Physical Review B"},
        "domains": ["cond-mat.mtrl-sci"], "poll_interval_seconds": 43200,
    },
    # Nature 子刊：无图，但内容权重高。
    {
        "kind": "rss", "name": "Nature Physics",
        "config": {"url": "https://www.nature.com/nphys.rss", "venue": "Nature Physics", "enrich_image": True},
        "domains": [], "poll_interval_seconds": 43200,
    },
    {
        "kind": "rss", "name": "Nature Chemistry",
        "config": {"url": "https://www.nature.com/nchem.rss", "venue": "Nature Chemistry", "enrich_image": True},
        "domains": ["physics.chem-ph"], "poll_interval_seconds": 43200,
    },
    {
        "kind": "rss", "name": "Nature Communications",
        "config": {"url": "https://www.nature.com/ncomms.rss", "venue": "Nature Communications", "enrich_image": True},
        "domains": [], "poll_interval_seconds": 43200,
    },
    # 科学新闻/科普：这一类是"有意思的东西"，也是图片覆盖率最高的一类。
    {
        "kind": "rss", "name": "Quanta Magazine",
        "config": {"url": "https://api.quantamagazine.org/feed/", "venue": "Quanta Magazine"},
        "domains": [], "poll_interval_seconds": 43200,
    },
    {
        "kind": "rss", "name": "Phys.org — 物理",
        "config": {"url": "https://phys.org/rss-feed/physics-news/", "venue": "Phys.org"},
        "domains": [], "poll_interval_seconds": 43200,
    },
    {
        "kind": "rss", "name": "ScienceDaily — 物质与能量",
        "config": {"url": "https://www.sciencedaily.com/rss/matter_energy/physics.xml",
                   "venue": "ScienceDaily"},
        "domains": [], "poll_interval_seconds": 43200,
    },
    {
        "kind": "rss", "name": "MIT Technology Review",
        "config": {"url": "https://www.technologyreview.com/feed/", "venue": "MIT Technology Review"},
        "domains": ["cs.LG"], "poll_interval_seconds": 43200,
    },
)


def upgrade() -> None:
    op.add_column("feed_items", sa.Column("image_url", sa.Text(), nullable=True))
    _seed_new_sources()
    _enable_image_enrichment_on_existing_sources()


def _seed_new_sources() -> None:
    """播种新源；已存在同 (kind, name) 的跳过（与 025 同一个幂等写法）。"""
    import json
    import uuid

    from sqlalchemy.dialects.postgresql import JSONB, UUID

    json_type = sa.JSON().with_variant(JSONB(), "postgresql")
    bind = op.get_bind()
    existing = {
        (row[0], row[1])
        for row in bind.execute(sa.text("SELECT kind, name FROM feed_sources")).fetchall()
    }
    is_postgres = bind.dialect.name == "postgresql"
    rows = []
    for source in _NEW_SOURCES:
        if (source["kind"], source["name"]) in existing:
            continue
        rows.append({
            "id": str(uuid.uuid4()),
            "kind": source["kind"],
            "name": source["name"],
            "config": source["config"] if is_postgres else json.dumps(source["config"]),
            "domains": source["domains"] if is_postgres else json.dumps(source["domains"]),
            "poll_interval_seconds": source["poll_interval_seconds"],
        })
    if not rows:
        return
    table = sa.table(
        "feed_sources",
        sa.column("id", UUID(as_uuid=False)),
        sa.column("kind", sa.String),
        sa.column("name", sa.String),
        sa.column("config", json_type),
        sa.column("domains", json_type),
        sa.column("poll_interval_seconds", sa.Integer),
    )
    op.bulk_insert(table, rows)


def downgrade() -> None:
    op.drop_column("feed_items", "image_url")
    names = [s["name"] for s in _NEW_SOURCES]
    op.get_bind().execute(
        sa.text("DELETE FROM feed_sources WHERE name = ANY(:names)").bindparams(
            sa.bindparam("names", value=names, expanding=False)
        )
        if op.get_bind().dialect.name == "postgresql"
        else sa.text("DELETE FROM feed_sources WHERE name IN :names").bindparams(
            sa.bindparam("names", value=tuple(names), expanding=True)
        )
    )


def _enable_image_enrichment_on_existing_sources() -> None:
    """给**已经存在**的 Nature/Science 源打开落地页补图。

    它们是 025 播的，config 里没有这个开关。打真源验过（2026-08-23）：
    nature.com 文章页的 og:image 是文章里的真实配图，而 arXiv 的是它自己的
    logo —— 所以只给这几个开，arXiv 一律不开。
    """
    import json

    bind = op.get_bind()
    rows = bind.execute(
        sa.text("SELECT id, config FROM feed_sources WHERE kind = 'rss'")
    ).fetchall()
    for row in rows:
        config = row[1]
        if isinstance(config, str):
            config = json.loads(config)
        if not isinstance(config, dict):
            continue
        url = str(config.get("url") or "")
        if "nature.com" not in url and "science.org" not in url:
            continue
        if config.get("enrich_image"):
            continue
        config["enrich_image"] = True
        bind.execute(
            sa.text("UPDATE feed_sources SET config = :config WHERE id = :id").bindparams(
                sa.bindparam("config", value=json.dumps(config), type_=sa.Text()),
                sa.bindparam("id", value=row[0]),
            )
            if bind.dialect.name != "postgresql"
            else sa.text(
                "UPDATE feed_sources SET config = CAST(:config AS JSONB) WHERE id = :id"
            ).bindparams(
                sa.bindparam("config", value=json.dumps(config)),
                sa.bindparam("id", value=row[0]),
            )
        )

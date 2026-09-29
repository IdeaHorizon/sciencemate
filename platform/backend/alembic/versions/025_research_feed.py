"""科研资讯流：源 / 条目 / 互动 / 每日选摘。

平台此前没有信息摄入层，用户获取领域动态靠平台之外的一堆渠道，而那些渠道
没有一个知道他手头正在做什么研究 —— 这里知道（projects.research_domain、
org 的域注册表、KB 沉淀的方向），所以推荐信号从第一天起就是真实科研上下文，
不靠冷启动问卷慢慢学。

四张表的边界见 `app/models/feed.py` 的模块 docstring，其中两条要点：

- 资讯是**有时效的信息**，不是经过准入的知识资产，所以落 Postgres 而不是
  harness 的 JSONL —— 这不违反"知识归 harness"的纪律，恰恰因为它不是知识。
- 没有复用 `watchlists` / `dreaming_jobs`（007 号迁移建的，至今零使用）：
  前者是用户级关注对象、后者是配在源之上的作业，而一个源该多久拉一次是这个
  源自己的出版节奏。两张表都原样保留给它们本来的用途。

`feed_sources` 顺带在这里播种：源清单是这套东西的"内容供给从哪来"，属于
schema 的一部分而不是运维配置。播种用 ON CONFLICT DO NOTHING 语义（先查后插），
所以重复执行、以及此前手工加过同名源的库，都不会炸。

Revision ID: 025_research_feed
Revises: 024_model_context_window
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "025_research_feed"
down_revision = "024_model_context_window"
branch_labels = None
depends_on = None

#: JSONB 在 Postgres，JSON 在别处 —— 与模型层 `_JSON` 同一个写法。
_JSON = sa.JSON().with_variant(JSONB(), "postgresql")

#: 开箱即用的源。**只放机器可靠、条款允许、且不需要密钥的 T1 源**：
#: 冷启动时一条内容都没有的 feed 比没有 feed 更糟，而要用户先去配十个 RSS
#: 才能看见第一条，等于把冷启动难题转嫁给了用户。
#:
#: 社交/社区源（HN、Reddit、Bluesky）是 M2 的**热度信号**，不作为内容源 ——
#: 一条内容的身份始终锚在一手出处上。
_SEED_SOURCES: tuple[dict, ...] = (
    # arXiv：平台主战场的几个分类。一天一更，六小时一拉足够。
    {
        "kind": "arxiv",
        "name": "arXiv cond-mat.mtrl-sci",
        "config": {"category": "cond-mat.mtrl-sci", "max_results": 40},
        "domains": ["cond-mat.mtrl-sci"],
        "poll_interval_seconds": 21600,
    },
    {
        "kind": "arxiv",
        "name": "arXiv cond-mat.stat-mech",
        "config": {"category": "cond-mat.stat-mech", "max_results": 40},
        "domains": ["cond-mat.stat-mech"],
        "poll_interval_seconds": 21600,
    },
    {
        "kind": "arxiv",
        "name": "arXiv physics.comp-ph",
        "config": {"category": "physics.comp-ph", "max_results": 40},
        "domains": ["physics.comp-ph"],
        "poll_interval_seconds": 21600,
    },
    {
        "kind": "arxiv",
        "name": "arXiv physics.chem-ph",
        "config": {"category": "physics.chem-ph", "max_results": 40},
        "domains": ["physics.chem-ph"],
        "poll_interval_seconds": 21600,
    },
    {
        "kind": "arxiv",
        "name": "arXiv cs.LG",
        "config": {"category": "cs.LG", "max_results": 40},
        "domains": ["cs.LG"],
        "poll_interval_seconds": 21600,
    },
    {
        "kind": "arxiv",
        "name": "arXiv stat.ML",
        "config": {"category": "stat.ML", "max_results": 40},
        "domains": ["stat.ML"],
        "poll_interval_seconds": 21600,
    },
    {
        "kind": "arxiv",
        "name": "arXiv quant-ph",
        "config": {"category": "quant-ph", "max_results": 40},
        "domains": ["quant-ph"],
        "poll_interval_seconds": 21600,
    },
    {
        "kind": "arxiv",
        "name": "arXiv q-bio.BM",
        "config": {"category": "q-bio.BM", "max_results": 30},
        "domains": ["q-bio.BM"],
        "poll_interval_seconds": 21600,
    },
    # 顶刊 RSS：综合刊横跨多个域，条目自带的分类拿不到，只能按刊物给一组
    # 宽域，再由排序层按标题/摘要细化。
    {
        "kind": "rss",
        "name": "Nature — latest research",
        "config": {"url": "https://www.nature.com/nature.rss", "venue": "Nature"},
        "domains": [],
        "poll_interval_seconds": 43200,
    },
    {
        "kind": "rss",
        "name": "Science — current issue",
        "config": {
            "url": "https://www.science.org/rss/news_current.xml",
            "venue": "Science",
        },
        "domains": [],
        "poll_interval_seconds": 43200,
    },
    {
        "kind": "rss",
        "name": "Nature Materials",
        "config": {
            "url": "https://www.nature.com/nmat.rss",
            "venue": "Nature Materials",
        },
        "domains": ["cond-mat.mtrl-sci"],
        "poll_interval_seconds": 43200,
    },
    {
        "kind": "rss",
        "name": "Nature Machine Intelligence",
        "config": {
            "url": "https://www.nature.com/natmachintell.rss",
            "venue": "Nature Machine Intelligence",
        },
        "domains": ["cs.LG"],
        "poll_interval_seconds": 43200,
    },
    {
        "kind": "rss",
        "name": "Physical Review Letters",
        "config": {
            "url": "http://feeds.aps.org/rss/recent/prl.xml",
            "venue": "Physical Review Letters",
        },
        "domains": [],
        "poll_interval_seconds": 43200,
    },
    # HuggingFace Daily Papers：本身就是一份"今天被讨论最多的论文"精选流，
    # 有稳定 API。它是唯一一个把注意力信号直接给出来的 T1 源。
    {
        "kind": "hf_daily_papers",
        "name": "HuggingFace Daily Papers",
        "config": {"url": "https://huggingface.co/api/daily_papers"},
        "domains": ["cs.LG"],
        "poll_interval_seconds": 21600,
    },
)


def upgrade() -> None:
    op.create_table(
        "feed_sources",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column("kind", sa.String(50), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("config", _JSON, nullable=False),
        sa.Column("domains", _JSON, nullable=False),
        sa.Column(
            "poll_interval_seconds", sa.Integer, nullable=False, server_default="21600"
        ),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default="true"),
        sa.Column("last_polled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_item_count", sa.Integer, nullable=True),
        sa.Column("last_error", sa.Text, nullable=True),
        sa.Column("consecutive_failures", sa.Integer, nullable=False, server_default="0"),
        sa.Column(
            "max_consecutive_failures", sa.Integer, nullable=False, server_default="5"
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("kind", "name", name="uq_feed_sources_kind_name"),
    )
    op.create_index(
        "ix_feed_sources_active_polled", "feed_sources", ["is_active", "last_polled_at"]
    )

    op.create_table(
        "feed_items",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column("canonical_key", sa.String(255), nullable=False, unique=True),
        sa.Column("kind", sa.String(30), nullable=False),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("url", sa.Text, nullable=True),
        sa.Column("summary", sa.Text, nullable=True),
        sa.Column("authors", _JSON, nullable=False),
        sa.Column("venue", sa.String(200), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True, index=True),
        sa.Column("domains", _JSON, nullable=False),
        sa.Column(
            "source_id",
            UUID(as_uuid=False),
            sa.ForeignKey("feed_sources.id", ondelete="SET NULL"),
            nullable=True,
            index=True,
        ),
        sa.Column(
            "author_user_id",
            UUID(as_uuid=False),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
            index=True,
        ),
        sa.Column("organization_id", sa.String(64), nullable=True, index=True),
        sa.Column("visibility", sa.String(20), nullable=False, server_default="platform"),
        sa.Column("extra", _JSON, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), index=True
        ),
    )
    op.create_index("ix_feed_items_kind_published", "feed_items", ["kind", "published_at"])
    op.create_index(
        "ix_feed_items_visibility_org", "feed_items", ["visibility", "organization_id"]
    )

    op.create_table(
        "feed_engagements",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column(
            "user_id",
            UUID(as_uuid=False),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "item_id",
            UUID(as_uuid=False),
            sa.ForeignKey("feed_items.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("action", sa.String(20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("user_id", "item_id", "action", name="uq_feed_engagement_once"),
    )
    op.create_index(
        "ix_feed_engagements_user_action", "feed_engagements", ["user_id", "action"]
    )

    op.create_table(
        "feed_daily_picks",
        sa.Column(
            "user_id",
            UUID(as_uuid=False),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("pick_date", sa.Date, primary_key=True),
        sa.Column("picks", _JSON, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )

    _seed_sources()


def _seed_sources() -> None:
    """播种默认源；已存在同 (kind, name) 的跳过。

    先查后插而不是 `INSERT ... ON CONFLICT`：这个迁移要能在 SQLite 上跑
    （后端测试用 SQLite），而两种方言的 upsert 语法不通用。源的条数是十几条，
    一次全表查的代价可以忽略。
    """
    import json
    import uuid

    bind = op.get_bind()
    existing = {
        (row[0], row[1])
        for row in bind.execute(sa.text("SELECT kind, name FROM feed_sources")).fetchall()
    }
    is_postgres = bind.dialect.name == "postgresql"
    rows = []
    for source in _SEED_SOURCES:
        if (source["kind"], source["name"]) in existing:
            continue
        rows.append(
            {
                "id": str(uuid.uuid4()),
                "kind": source["kind"],
                "name": source["name"],
                # SQLite 的 JSON 列吃字符串，Postgres 的 JSONB 由驱动自己序列化。
                "config": source["config"] if is_postgres else json.dumps(source["config"]),
                "domains": source["domains"] if is_postgres else json.dumps(source["domains"]),
                "poll_interval_seconds": source["poll_interval_seconds"],
            }
        )
    if not rows:
        return
    table = sa.table(
        "feed_sources",
        sa.column("id", UUID(as_uuid=False)),
        sa.column("kind", sa.String),
        sa.column("name", sa.String),
        sa.column("config", _JSON),
        sa.column("domains", _JSON),
        sa.column("poll_interval_seconds", sa.Integer),
    )
    op.bulk_insert(table, rows)


def downgrade() -> None:
    op.drop_table("feed_daily_picks")
    op.drop_index("ix_feed_engagements_user_action", table_name="feed_engagements")
    op.drop_table("feed_engagements")
    op.drop_index("ix_feed_items_visibility_org", table_name="feed_items")
    op.drop_index("ix_feed_items_kind_published", table_name="feed_items")
    op.drop_table("feed_items")
    op.drop_index("ix_feed_sources_active_polled", table_name="feed_sources")
    op.drop_table("feed_sources")

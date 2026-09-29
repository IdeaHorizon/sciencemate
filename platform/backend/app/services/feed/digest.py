"""域周报 —— agent 相对 RSS 阅读器的那点增量。

## 为什么这一层值得存在

原料层（论文卡、动态卡、日历卡）任何一个 RSS 阅读器都能做。加工层才是
agent 的活：把一周里散落的 N 条缩成"这个方向最近在发生什么"，并且每一句都
指得回具体哪一条。

## 谁付这次模型调用

生成一份周报要过一次模型，而凭据在这个平台上是**属人**的。用的是
`feed_curation` 角色（见 `shared/model_roles.yaml`）—— 不回退到主推理模型：
那是用户为做研究选的，不是授权给后台挖资讯的。

触发有两条路，都落成同一条 `FeedItem(kind=digest)`、全平台共享、一个域一周
只生成一次：

  - **按需**：谁先打开这个域的周报谁触发（第一个读者多等十几秒）；
  - **主动**：开了自动挖掘的用户，由采集循环替他预生成（见 `scheduler`）。

第二条正是"平台服务身份不存在"那个坑的解：用户显式打开开关并指名模型，
就补上了缺失的那个"人"—— 同意与凭据都齐了。

## provenance

`extra.covers_item_ids` 记下这份周报是从哪些条目缩出来的。模型只看得到调用
方给的那批材料，提示词要求逐条标 [序号]，所以正文里的每个指代都能落回一条
有出处的真实条目。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.feed import FeedItem, FeedItemKind, FeedVisibility
from app.models.user import User
from app.services.feed import bridge, domains as domain_service
from app.services.feed.bridge import BridgeUnavailable
from app.services.feed.ranking import recent_order

logger = logging.getLogger(__name__)

#: 一个域多久出一份周报。
DIGEST_PERIOD_DAYS = 7

#: 少于这么多条新内容就不出周报 —— 三条内容缩成一段话，不如直接看那三条。
MIN_ITEMS_FOR_DIGEST = 6

#: 一份周报最多喂几条材料进去。
MAX_ITEMS_PER_DIGEST = 25

#: 超时与凭据都在 `feed.bridge` 里定 —— 这一层不再自己拿主意。
class DigestUnavailable(RuntimeError):
    """这份周报现在生成不了。**不是错误页**：调用方照常返回原料，
    只是这次没有加工层。"""


def digest_key(domain: str, period_start: datetime) -> str:
    """周报的身份：域 + 周期起点。同一域同一周只会有一份。"""
    return f"digest:{domain}:{period_start.date().isoformat()}"


def period_start(now: datetime | None = None) -> datetime:
    """本期起点 —— 按自然周对齐（周一）。

    对齐到自然周而不是"从现在往前推七天"：后者会让每次请求都落在一个略微
    不同的窗口上，于是同一个域今天生成一份、明天又生成一份，缓存等于没有。
    """
    moment = (now or datetime.now(UTC)).astimezone(UTC)
    midnight = moment.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight - timedelta(days=midnight.weekday())


async def existing_digest(db: AsyncSession, *, domain: str) -> FeedItem | None:
    return await db.scalar(
        select(FeedItem).where(FeedItem.canonical_key == digest_key(domain, period_start()))
    )


async def source_items(db: AsyncSession, *, domain: str) -> list[FeedItem]:
    """这一期该被缩进周报的原料。

    按 `ancestors` 匹配，所以 `cond-mat` 的周报会把 `cond-mat.mtrl-sci` 的
    内容算进来 —— 一个大类的周报理应覆盖它下面的分类。
    """
    start = period_start()
    candidates = list(
        (
            await db.execute(
                select(FeedItem)
                .where(
                    FeedItem.kind == FeedItemKind.PAPER,
                    FeedItem.visibility == FeedVisibility.PLATFORM,
                    FeedItem.created_at >= start,
                )
                # 时效排序统一走 ranking.recent_order()：无日期条目按入库时间参与
                # 竞争，而不是被 NULLS LAST 一律垫到底、撞上 limit 就整批消失。
                .order_by(recent_order())
                .limit(400)
            )
        )
        .scalars()
        .all()
    )
    matched: list[FeedItem] = []
    for item in candidates:
        for slug in item.domains or ():
            if domain in domain_service.ancestors(str(slug)):
                matched.append(item)
                break
        if len(matched) >= MAX_ITEMS_PER_DIGEST:
            break
    return matched


async def ensure_digest(db: AsyncSession, *, domain: str, user: User) -> FeedItem | None:
    """拿这个域本期的周报；没有就生成一份。

    生成失败**不抛给调用方**：周报是锦上添花，它没生成出来时资讯流本身照常
    工作。但失败要留日志 —— 一个静默不生成的加工层，和一个"这个域这周确实
    没什么内容"的加工层，从外面看一模一样。
    """
    cached = await existing_digest(db, domain=domain)
    if cached is not None:
        return cached

    items = await source_items(db, domain=domain)
    if len(items) < MIN_ITEMS_FOR_DIGEST:
        return None

    try:
        text = await _ask_harness_for_digest(domain=domain, items=items, user=user, db=db)
    except (DigestUnavailable, BridgeUnavailable) as exc:
        logger.info("Weekly digest for %s not generated: %s", domain, exc)
        return None
    except Exception:
        logger.warning("Weekly digest for %s failed", domain, exc_info=True)
        return None
    if not text:
        return None

    start = period_start()
    record = FeedItem(
        canonical_key=digest_key(domain, start),
        kind=FeedItemKind.DIGEST,
        title=f"{domain_service.label(domain)}·本周动向",
        # 周报没有外部出处 —— 它就是在这儿生成的。`url` 因此为空，而
        # provenance 由 `covers_item_ids` 承担：正文里每个 [n] 都落回这批
        # 真实条目中的一条。
        url=None,
        summary=text,
        authors=[],
        venue="平台生成",
        published_at=datetime.now(UTC),
        domains=[domain],
        visibility=FeedVisibility.PLATFORM,
        extra={
            "covers_item_ids": [item.id for item in items],
            "period_start": start.isoformat(),
            "window_days": DIGEST_PERIOD_DAYS,
            "generated_by_user_id": user.id,
        },
    )
    db.add(record)
    await db.flush()
    return record


async def _ask_harness_for_digest(
    *, domain: str, items: list[FeedItem], user: User, db: AsyncSession
) -> str:
    """`op=feed_digest` —— 经 `feed.bridge`，与挖掘同一条路。

    模型调用不在 App Server 做：provider 分支、重试、超时、密钥解析只存在于
    `core.llm.LLMClient`。而 spawn 子进程那段也只有 `feed.bridge` 一份 ——
    抄第二份的话，超时怎么定、凭据怎么擦、失败怎么归因就有了两个会各自演化
    的答案，且分叉时两边都不报错。
    """
    backend = await bridge.curation_backend(db, user)
    if backend is None:
        raise DigestUnavailable(
            "没有配资讯挖掘模型 —— 周报由 feed_curation 角色生成"
        )
    event = await bridge.call(
        op="feed_digest",
        result_type="feed_digest_result",
        backend=backend,
        payload={
            "domain_label": domain_service.label(domain),
            "items": [
                {
                    "title": item.title,
                    "summary": (item.summary or "")[:600],
                    "venue": item.venue or "",
                }
                for item in items
            ],
        },
    )
    text = event.get("digest")
    return text.strip() if isinstance(text, str) else ""

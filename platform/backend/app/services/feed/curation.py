"""自动挖掘：让 agent 读你的课题，推断你该关注什么、该检索什么。

## 它补的是哪个洞

排序侧一直是自动的（画像每次现算，跟着 Project 走）。但"你到底属于哪些学科
方向"这一半此前只有一个很笨的英文标签子串匹配 —— 拿 155 个 arXiv 标签去课题
文字里找字面出现。实测一个写着 "Scientific AI" 的课题**一个都匹配不上**。
这一层就是把那一半换成模型来做。

## 三条纪律

1. **explicit > inferred**：用户手选的方向永远优先，推断出来的只是补充。
   删掉一个推断项会被记住（`rejected`），下次不再推给他 —— 否则他每天都要
   删同一个，而系统看起来像没听见。
2. **推断结果必须看得见**：接口把 explicit / inferred 分开返回，界面标
   "从你的课题推断"。一个替你决定你能看到什么的黑箱，比没有这个功能更糟。
3. **不是每次请求都算**：按课题文本的指纹判断有没有实质变化。改一个错别字
   不该烧一次模型调用，而加一个新课题应该立刻重算。

## 为什么开关默认关

它花的是用户自己的模型预算。默认替他花钱这件事，无论多小都不该由我们决定。
而且角色没配时它根本不可用（见 `shared/model_roles.yaml` 的 absence_note）。
"""

from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import bindparam, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.project import Project, ProjectStatus
from app.models.user import User
from app.services.feed import bridge, domains as domain_service
from app.services.feed.bridge import BridgeUnavailable
from app.services.harness_contract import HarnessContractUnavailable

logger = logging.getLogger(__name__)

#: 每个活跃课题最多配几个论文/资讯检索词。这是 prompt 约束 + 解析层截断的
#: 双重上限：词要真正属于"这个"课题，而不是一个课题吐出五六个词挤掉别人。
PER_PROJECT_QUERY_CAP = 2

#: 合并后全局检索词上限。对齐 `discovery.MAX_QUERIES_PER_USER`（检索侧每轮
#: 最多几组）—— floor（每个活跃课题至少 1 词）在这个上限之内才成立。课题数
#: 超过它时，按活跃度优先保留，最不活跃的课题可能拿不到词（有界的退化）。
MAX_MERGED_QUERIES = 5


def stored(user: User) -> dict:
    preferences = user.preferences if isinstance(user.preferences, dict) else {}
    feed = preferences.get("feed")
    return feed if isinstance(feed, dict) else {}


def is_enabled(user: User) -> bool:
    """自动挖掘开着吗。**默认关** —— 它花的是用户自己的模型预算。"""
    return bool(stored(user).get("auto_curation"))


def inferred_domains(user: User) -> tuple[str, ...]:
    raw = stored(user).get("inferred_domains")
    return tuple(str(d) for d in raw) if isinstance(raw, list) else ()


def rejected_domains(user: User) -> frozenset[str]:
    """他从推断结果里删掉过的。记住它，别下次再推一遍。"""
    raw = stored(user).get("rejected_domains")
    return frozenset(str(d) for d in raw) if isinstance(raw, list) else frozenset()


def inferred_queries(user: User) -> tuple[str, ...]:
    raw = stored(user).get("inferred_queries")
    return tuple(str(q) for q in raw) if isinstance(raw, list) else ()


def effective_domains(user: User) -> tuple[str, ...]:
    """真正用于送达匹配的方向：手选的 ∪（推断的 − 删掉的）。

    手选的排在前面，且不会被推断结果挤掉 —— 顺序本身也是一种表达。
    """
    from app.services.feed.profile import stored_domains

    out: list[str] = list(stored_domains(user))
    rejected = rejected_domains(user)
    for slug in inferred_domains(user):
        if slug not in out and slug not in rejected:
            out.append(slug)
    return tuple(out)


def project_fingerprint(projects: list[Project]) -> str:
    """课题文本的指纹 —— 用来判断"有没有实质变化，值不值得重算"。

    只算进真正会影响推断的三个字段。改标题里的一个错别字确实会让指纹变，
    这是可接受的：它便宜，而反过来（漏掉一次真变化）会让推荐一直停在旧课题上。
    """
    digest = hashlib.sha256()
    for project in sorted(projects, key=lambda p: p.id):
        for part in (project.name, project.research_domain, project.description):
            digest.update((part or "").encode("utf-8"))
            digest.update(b"\x1f")
    return digest.hexdigest()[:32]


def needs_refresh(user: User, fingerprint: str) -> bool:
    if not is_enabled(user):
        return False
    return stored(user).get("curated_fingerprint") != fingerprint


async def user_projects(db: AsyncSession, user: User) -> list[Project]:
    """用户在推进中的课题 —— **只看 ACTIVE**。

    暂停（PAUSED）、完成（COMPLETED）、归档（ARCHIVED）的课题一律不参与
    关键词推断：它们不该继续抢占那 3 个检索词配额，更不该继续往他的资讯流里
    推内容。否则一个已经放下的旧课题会一直压着新课题 —— 这正是「用户改了
    订阅方向，却还看到旧课题内容」的根源：课题的生命周期此前根本没接进这条
    管线（`where owner_id` 只按人过滤，不管课题还做不做）。
    """
    rows = await db.execute(
        select(Project)
        .where(Project.owner_id == user.id, Project.status == ProjectStatus.ACTIVE)
        .order_by(Project.updated_at.desc())
    )
    return list(rows.scalars().all())


async def project_user_inputs(db: AsyncSession, project_id: str, *, limit: int = 40) -> list[str]:
    """该项目下用户的**原始输入**文字（只看 user 消息，不看模型回复）。

    ``project.name`` / ``research_domain`` 是用户创建课题时的一句概述，往往太粗；
    而他在 research session 里真正敲下的问题才是最具体的意图。关键词据此提炼，
    比从概述里猜更贴他实际在做的事。

    取**最近**（按消息时间倒序）而不是最早：一个做久了的课题，他当下的诉求在
    最早那几条里根本看不到 —— 拿「最早的 40 条」去提炼，提炼的是过时的意图。
    返回时翻正成时间先后顺序，供下游 prompt 里「按时间先后」呈现。

    session 记录为空时返回空列表 —— 调用方回退到 name/domain 文本。
    """
    rows = await db.execute(
        text(
            "SELECT sm.content FROM session_messages sm "
            "JOIN sessions s ON s.session_id = sm.session_id "
            "WHERE s.project_id = :project_id AND sm.role = 'user' "
            "ORDER BY sm.created_at DESC, sm.sequence DESC LIMIT :limit"
        ),
        {"project_id": project_id, "limit": limit},
    )
    recent: list[str] = []
    for content in rows.scalars().all():
        value = " ".join(str(content or "").split())
        if value and value not in recent:
            recent.append(value)
    # SQL 取回的是「最新在前」，翻正成时间先后（最旧在前）。
    recent.reverse()
    return recent


def _coerce_datetime(value: object) -> datetime | None:
    """``MAX(created_at)`` 走 ``text()`` 在 SQLite 返回字符串、在 Postgres 返回 datetime。

    统一成 datetime；naive 值补 UTC —— 写入时区被 strip 的那部分本机都是按 UTC
    写的，读回后补回 UTC 才不会让活跃度排序时 naive/aware 混用。
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    try:
        parsed = datetime.fromisoformat(str(value).strip())
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


async def projects_last_active(
    db: AsyncSession, project_ids: list[str]
) -> dict[str, datetime | None]:
    """每个课题最近的用户消息时间 —— 课题真实活跃度，不用 ``projects.updated_at``。

    ``updated_at`` 任何字段改动（改个标题也算）都会 bump，不代表「还在做」；
    真实活跃只看他最近什么时候在这个课题下说过话。谁更该优先配满检索词，由
    这个时间决定：刚动过的在前，从不说话的沉底。
    """
    if not project_ids:
        return {}
    rows = await db.execute(
        text(
            "SELECT s.project_id, MAX(sm.created_at) AS last_active "
            "FROM session_messages sm "
            "JOIN sessions s ON s.session_id = sm.session_id "
            "WHERE s.project_id IN :ids AND sm.role = 'user' "
            "GROUP BY s.project_id"
        ).bindparams(bindparam("ids", expanding=True)),
        {"ids": project_ids},
    )
    found = {str(r.project_id): _coerce_datetime(r.last_active) for r in rows}
    return {pid: found.get(pid) for pid in project_ids}


def _merge_project_queries(
    per_project: list[dict[str, list[str]]],
    last_active: list[datetime | None],
    *,
    field: str,
    cap: int,
) -> list[str]:
    """把每个课题各自的检索词合并成一份全局列表。

    round-robin：先保证每个课题至少 1 词（floor），有富余再按活跃度给更活跃的
    课题补第 2 个词。活跃度只决定「谁先被补齐」，不决定「谁被挤到零词」——
    挤到零词只会发生在课题数超过全局上限的时候，那时沉底的（最不活跃）先让位。
    """
    def _active_score(active_at: datetime | None) -> float:
        # 有活跃时间的往前排（负数越大越靠前）；从未活跃的沉底。
        return -(active_at.timestamp()) if active_at is not None else 0.0

    order = sorted(
        range(len(per_project)),
        key=lambda i: (last_active[i] is None, _active_score(last_active[i])),
    )
    merged: list[str] = []
    max_slots = max((len(p.get(field) or []) for p in per_project), default=0)
    for round_idx in range(max_slots):
        for i in order:
            words = per_project[i].get(field) or []
            if round_idx < len(words):
                word = words[round_idx]
                if word and word not in merged:
                    merged.append(word)
                    if len(merged) >= cap:
                        return merged
    return merged


async def curate(
    db: AsyncSession,
    *,
    user: User,
    force: bool = False,
    allow_disabled: bool = False,
    selected_domains: list[str] | None = None,
) -> dict[str, Any] | None:
    """跑一次挖掘。返回这次推断的结果；没跑（不需要/不可用）返回 None。

    失败**不抛给调用方**：挖掘是锦上添花，它没跑成时资讯流照常工作。但要留
    日志，并且把失败原因写进用户偏好里 —— 一个静默不工作的开关，和一个
    "确实没什么可推断的"的开关，从界面上看一模一样。
    """
    if not is_enabled(user) and not allow_disabled:
        return None

    projects = await user_projects(db, user)
    if not projects:
        # 一个课题都没有时没什么可推断的。这不是失败，别记成失败。
        return None

    # 每个课题的真实活跃度（最近用户消息时间），喂给 prompt 让模型感知、并在
    # 合并时决定「谁先被补齐」。不用 projects.updated_at（改标题也会 bump）。
    project_ids = [p.id for p in projects]
    last_active_map = await projects_last_active(db, project_ids)

    fingerprint = project_fingerprint(projects)
    if not force and not needs_refresh(user, fingerprint):
        return None

    backend = await bridge.curation_backend(db, user)
    if backend is None:
        _remember(user, error="没有配资讯挖掘模型（设置 → 模型 → 资讯挖掘模型）")
        await db.flush()
        return None

    try:
        candidates = list(domain_service.domain_registry().spine_categories())
    except HarnessContractUnavailable as exc:
        _remember(user, error=f"域词表暂时不可用（平台侧）：{exc}")
        await db.flush()
        return None

    project_payloads: list[dict[str, Any]] = []
    for project in projects:
        active_at = last_active_map.get(project.id)
        project_payloads.append(
            {
                "name": project.name,
                "research_domain": project.research_domain,
                "description": project.description,
                "user_inputs": await project_user_inputs(db, project.id),
                "last_active": active_at.isoformat() if active_at else None,
            }
        )

    try:
        event = await bridge.call(
            op="feed_curate_profile",
            result_type="feed_curate_profile_result",
            backend=backend,
            payload={
                "projects": project_payloads,
                "candidate_domains": candidates,
                "selected_domains": [
                    f"{code}: {domain_service.label(code)}"
                    for code in dict.fromkeys(selected_domains or [])
                ],
            },
        )
    except BridgeUnavailable as exc:
        logger.info("Feed curation unavailable for %s: %s", user.id, exc)
        _remember(user, error=str(exc))
        await db.flush()
        return None
    except Exception as exc:  # noqa: BLE001 - 挖掘失败绝不该影响资讯流本身
        logger.warning("Feed curation failed for %s", user.id, exc_info=True)
        _remember(user, error=f"{type(exc).__name__}: {exc}")
        await db.flush()
        return None

    # event 里现在是 {domains, projects}：projects 是每个课题一组词。合并成全局
    # queries / news_queries（下游 discovery 仍按全局列表检索）。round-robin
    # 保证每个课题至少 1 词，活跃度决定谁先被补满。
    per_project: list[dict[str, list[str]]] = []
    raw_projects = event.get("projects")
    if isinstance(raw_projects, list):
        for entry in raw_projects[: len(projects)]:
            entry = entry if isinstance(entry, dict) else {}
            per_project.append(
                {
                    "queries": [str(q).strip() for q in (entry.get("queries") or []) if str(q).strip()],
                    "news_queries": [str(q).strip() for q in (entry.get("news_queries") or []) if str(q).strip()],
                }
            )
    # 对齐到课题数：parse 层缺失的课题补空词（floor 轮到它时跳过）。
    while len(per_project) < len(projects):
        per_project.append({"queries": [], "news_queries": []})

    last_active_list = [last_active_map.get(p.id) for p in projects]
    queries = _merge_project_queries(
        per_project, last_active_list, field="queries", cap=MAX_MERGED_QUERIES
    )
    news_queries = _merge_project_queries(
        per_project, last_active_list, field="news_queries", cap=MAX_MERGED_QUERIES
    )

    result = {
        "domains": [str(d) for d in (event.get("domains") or [])],
        "queries": queries,
        "news_queries": news_queries,
    }
    _remember(
        user,
        fingerprint=fingerprint,
        domains=result["domains"],
        queries=result["queries"],
        news_queries=result["news_queries"],
        model=f"{backend.display_name} · {backend.model}",
    )
    await db.flush()
    logger.info(
        "Feed curation for %s: %d domain(s), %d query(ies), %d news_query(ies)",
        user.id, len(result["domains"]), len(result["queries"]), len(result["news_queries"]),
    )
    return result


def _remember(
    user: User,
    *,
    fingerprint: str | None = None,
    domains: list[str] | None = None,
    queries: list[str] | None = None,
    news_queries: list[str] | None = None,
    model: str | None = None,
    error: str | None = None,
) -> None:
    """把这次挖掘的结果（或失败）写回用户偏好。

    失败也写：`curated_error` 是界面上"开关开着但没工作"唯一能说清原因的地方。
    成功时清掉它，否则一条早就修好的错误会一直挂在那儿。
    """
    preferences = dict(user.preferences) if isinstance(user.preferences, dict) else {}
    feed = dict(preferences.get("feed") or {})
    feed["curated_at"] = datetime.now(UTC).isoformat()
    if error is not None:
        feed["curated_error"] = error[:500]
    else:
        feed.pop("curated_error", None)
    if fingerprint is not None:
        feed["curated_fingerprint"] = fingerprint
    if domains is not None:
        feed["inferred_domains"] = domains
    if queries is not None:
        feed["inferred_queries"] = queries
    if news_queries is not None:
        feed["inferred_news_queries"] = news_queries
    if model is not None:
        feed["curated_by_model"] = model
    preferences["feed"] = feed
    user.preferences = preferences

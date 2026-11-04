"""User-scoped CNKI indexes, checkpoints and leases for managed/local collectors."""
from __future__ import annotations

import hashlib
import re
import secrets
import unicodedata
from datetime import UTC, datetime, timedelta
from uuid import NAMESPACE_URL, uuid5

from fastapi import HTTPException
from sqlalchemy import func, select, update

from app.models.cnki import CnkiDevice, CnkiJob, CnkiJobPaper, CnkiPaper
from app.models.feed import FeedItem
from app.models.user import User
from app.schemas.literature import LiteraturePaperOut, LiteratureSearchOut
from app.services.say import say


def now():
    return datetime.now(UTC)


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


async def lock_user(db, user_id: str):
    """Serialize this user's browser writes, including different jobs/devices.

    SQLite ignores FOR UPDATE; a no-op write acquires its transaction lock.
    The request's get_db transaction holds the lock through commit.
    """
    if db.get_bind().dialect.name == "sqlite":
        await db.execute(update(User).where(User.id == user_id).values(id=User.id))
    else:
        await db.execute(select(User.id).where(User.id == user_id).with_for_update())


def identity(paper: dict) -> str:
    # CNKI detail URLs contain session-specific encrypted IDs. Never use these
    # as the persistent key, or repeat searches create duplicate records.
    title = re.sub(r"\s+", "", unicodedata.normalize("NFKC", paper["title"])).casefold()
    return digest(title + "\0" + str(paper.get("year") or str(paper.get("pub_date") or "")[:4]))


def device_view(device: CnkiDevice) -> dict:
    online = bool(device.last_seen_at and now() - device.last_seen_at < timedelta(seconds=100))
    return {"id": device.id, "name": device.name, "online": online and not device.revoked_at,
            "state": "revoked" if device.revoked_at else (device.last_report or {}).get("state", "idle") if online else "offline",
            "last_seen_at": device.last_seen_at, "detail": (device.last_report or {}).get("detail", "")}


def job_state(job: CnkiJob) -> str:
    if not job.enabled:
        return "cancelled"
    if job.last_completed_at and (job.checkpoint or {}).get("finished"):
        return "completed"
    if job.lease_until and job.lease_until > now():
        return "working"
    return (job.last_report or {}).get("state", "queued") if (job.last_report or {}).get("state") in {
        "waiting_for_verification", "browser_closed", "paused", "error"
    } else "queued"


async def job_view(db, job: CnkiJob, *, papers=False) -> dict:
    count = await db.scalar(select(func.count()).select_from(CnkiJobPaper).where(CnkiJobPaper.job_id == job.id))
    result = {"id": job.id, "query": job.query, "category": job.category, "recurring": job.recurring,
              "enabled": job.enabled, "state": job_state(job), "count": count,
              "page": (job.checkpoint or {}).get("page", 1), "total": (job.checkpoint or {}).get("total", 0),
              "detail": (job.last_report or {}).get("detail", ""), "last_completed_at": job.last_completed_at,
              "executor": (job.checkpoint or {}).get("executor", "server")}
    if papers:
        rows = (await db.scalars(select(CnkiPaper).join(CnkiJobPaper, CnkiJobPaper.paper_id == CnkiPaper.id)
                                .where(CnkiJobPaper.job_id == job.id, CnkiPaper.user_id == job.user_id)
                                .order_by(CnkiPaper.metadata_json["pub_date"].as_string().desc(),
                                          CnkiPaper.observed_at.desc(), CnkiPaper.id).limit(job.result_limit or 200))).all()
        result["result"] = LiteratureSearchOut(
            query=job.query, papers=[as_literature(p.metadata_json) for p in rows], total=len(rows),
            candidate_count=count, source_counts={"cnki": count}, requested_sources=["cnki"],
            attempted_sources=["cnki"], search_mode="cnki_" + result["executor"] + "_browser", from_cache=job_state(job)=="completed",
        ).model_dump()
    return result


def as_literature(p: dict) -> LiteraturePaperOut:
    from app.services.harness_imports import harness_module
    category = harness_module("shared.contrib.publication_categories").publication_category(
        {"document_type":"期刊", **p, "source":"cnki"})
    return LiteraturePaperOut(
        title=p["title"], title_zh=p["title"], authors=p.get("authors", []), venue=p.get("venue"),
        abstract=p.get("abstract"), abstract_zh=p.get("abstract"), doi=p.get("doi"), url=p.get("url"),
        year=p.get("year"), pub_date=p.get("pub_date"), source="cnki", citations=p.get("citations", 0),
        publication_category=category, index_completeness={"abstract":bool(p.get("abstract")),
        "abstract_complete":bool(p.get("abstract")) and not p.get("abstract_truncated", False)},
        metadata_provenance={"provider":"cnki", "transport":p.get("transport", "user_browser"),
                             "keywords":p.get("keywords",[]), "metadata_error":p.get("metadata_error", "")},
    )


async def claim_job(db, device: CnkiDevice, *, executor="local") -> dict | None:
    moment = now()
    if await db.scalar(select(CnkiJob.id).where(CnkiJob.user_id == device.user_id,
            CnkiJob.enabled.is_(True), CnkiJob.lease_until > moment).limit(1)):
        return None
    statement = select(CnkiJob).where(CnkiJob.user_id == device.user_id, CnkiJob.enabled.is_(True)).order_by(CnkiJob.recurring, CnkiJob.created_at)
    if db.get_bind().dialect.name == "postgresql":
        statement = statement.with_for_update(skip_locked=True)
    jobs = list((await db.scalars(statement)).all())
    if executor == "server":
        # Collect every queued query's indexes before spending time on abstracts.
        jobs.sort(key=lambda job: (job.checkpoint or {}).get("phase") == "metadata"
                  and not (job.checkpoint or {}).get("finished"))
    for job in jobs:
        if (job.checkpoint or {}).get("executor", "server") != executor:
            continue
        if executor == "server":
            state = (job.last_report or {}).get("state")
            if state == "paused":
                continue
            delay = 30 if state == "waiting_for_verification" else 300 if state == "error" else 0
            if delay and job.last_report_at and moment-job.last_report_at < timedelta(seconds=delay):
                continue
        if job.lease_until and job.lease_until > moment:
            continue
        if job.last_completed_at and (not job.recurring or moment-job.last_completed_at < timedelta(hours=24)):
            continue
        checkpoint = dict(job.checkpoint or {})
        if checkpoint.get("finished"):
            checkpoint = {"executor": executor}
        since = (moment-timedelta(days=365)).date().isoformat() if job.recurring else ""
        if job.recurring and job.last_completed_at:
            since = max(since, (job.last_completed_at-timedelta(days=31)).date().isoformat())
        checkpoint.setdefault("page", 1)
        checkpoint.setdefault("since", since)
        checkpoint.setdefault("accepted", [])
        job.checkpoint = checkpoint
        lease = secrets.token_urlsafe(32)
        job.lease_device_id, job.lease_hash = device.id, digest(lease)
        job.lease_until = moment + timedelta(seconds=90)
        job.last_report_at, job.last_report = moment, {"state":"working"}
        await db.flush()
        return {"id":job.id, "query":job.query, "field":job.field, "category":job.category,
                "page":checkpoint["page"], "accepted":checkpoint["accepted"], "since":checkpoint["since"],
                "phase":checkpoint.get("phase", "index"), "total":checkpoint.get("total", 0),
                "limit":job.result_limit, "lease":lease}
    return None


async def leased_job(db, device: CnkiDevice, job_id: str, lease: str, *, lang: str = "zh") -> CnkiJob:
    job = await db.scalar(select(CnkiJob).where(CnkiJob.id==job_id, CnkiJob.user_id==device.user_id).with_for_update())
    if (not job or not job.enabled or job.lease_device_id != device.id or not job.lease_hash
            or not secrets.compare_digest(job.lease_hash, digest(lease))
            or not job.lease_until or job.lease_until < now()):
        raise HTTPException(409, say({"zh": "任务已暂停或已由新的采集会话接续", "en": "The task is paused or has been taken over by a new collection session."}, lang))
    return job


async def receive_batch(db, device, job, batch, *, lang: str = "zh"):
    checkpoint = dict(job.checkpoint or {})
    expected = checkpoint.get("page", 1)
    if batch.page < expected:
        return {"accepted":True, "page":expected, "replayed":True}
    if batch.page != expected:
        raise HTTPException(409, say({"zh": "请从上次确认的页继续", "en": "Continue from the last acknowledged page."}, lang))
    moment = now()
    accepted = set(checkpoint.get("accepted", []))
    for value in batch.papers:
        incoming = value.model_dump()
        incoming["transport"] = "server_browser" if (job.checkpoint or {}).get("executor", "server") == "server" else "user_browser"
        key = identity(incoming)
        paper_id = str(uuid5(NAMESPACE_URL, device.user_id+"/cnki/"+key))
        paper = await db.get(CnkiPaper, paper_id)
        if paper is None:
            paper = CnkiPaper(id=paper_id, user_id=device.user_id, identity=key, metadata_json={}, observed_at=moment)
            db.add(paper)
        merged = dict(paper.metadata_json or {})
        for name, item in incoming.items():
            if item or name not in merged:
                merged[name] = item
        if incoming.get("abstract"):
            merged["abstract_truncated"] = incoming["abstract_truncated"]
        if incoming.get("metadata_attempted"):
            merged["metadata_error"] = incoming.get("metadata_error", "")
        paper.metadata_json = merged
        if incoming.get("metadata_attempted"):
            paper.observed_at = moment
        if not await db.get(CnkiJobPaper, (job.id, paper_id)):
            db.add(CnkiJobPaper(job_id=job.id, paper_id=paper_id))
        await db.flush()
        if merged.get("metadata_attempted"):
            accepted.add(incoming["title"])
        if job.recurring:
            await sync_feed_paper(db, paper)
    checkpoint.update(total=batch.total, accepted=sorted(accepted))
    if batch.page_complete:
        checkpoint.update(page=batch.page+1, accepted=[])
    if batch.finished:
        checkpoint["finished"] = True
        job.last_completed_at = moment
        job.lease_until = moment
    else:
        job.lease_until = moment + timedelta(seconds=90)
    job.checkpoint = checkpoint
    job.last_report_at, job.last_report = moment, {"state":"completed" if batch.finished else "working"}
    device.last_seen_at, device.last_report = moment, {"state":"working"}
    await db.flush()
    return {"accepted":True, "page":checkpoint["page"]}


async def sync_feed_paper(db, paper: CnkiPaper):
    from app.services.feed.literature_projection import publication_datetime
    from app.services.feed.pool_cache import clear
    from app.services.feed.ranking import is_journal_notice
    jobs = (await db.scalars(select(CnkiJob).join(CnkiJobPaper, CnkiJobPaper.job_id==CnkiJob.id).where(
        CnkiJobPaper.paper_id==paper.id, CnkiJob.user_id==paper.user_id, CnkiJob.enabled.is_(True), CnkiJob.recurring.is_(True)))).all()
    key = "cnki-user:"+paper.id
    item = await db.scalar(select(FeedItem).where(FeedItem.canonical_key==key))
    p = paper.metadata_json
    if not item:
        item = FeedItem(id=paper.id, canonical_key=key, kind="paper", title=p["title"], authors=[], domains=[], extra={})
        db.add(item)
    item.title, item.url = p["title"], p["url"]
    item.summary, item.authors, item.venue = p.get("abstract", ""), p.get("authors", []), str(p.get("venue", ""))[:200]
    item.author_user_id = paper.user_id
    item.published_at = publication_datetime(p.get("pub_date"), p.get("year"))
    item.extra = {"cnki_local":True, "cnki_categories":sorted({j.category for j in jobs}),
                  "cnki_queries":[j.query for j in jobs], "doi":p.get("doi", ""),
                  "feed_acquisition_routes":["web"], "index_only":True,
                  "title_zh":p["title"], "abstract_zh":p.get("abstract", ""),
                  "literature_preparation":"ready" if p.get("metadata_attempted") else "pending",
                  "abstract_lookup_completed":bool(p.get("metadata_attempted"))}
    item.visibility = "private" if jobs and p.get("metadata_attempted") and not is_journal_notice(item) else "withdrawn"
    clear()

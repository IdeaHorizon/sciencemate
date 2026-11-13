"""User-scoped CNKI indexes, checkpoints and leases for managed/local collectors."""
from __future__ import annotations

from app.services.say import say

import hashlib
import base64
import json
import re
import secrets
import unicodedata
from datetime import UTC, datetime, timedelta
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from fastapi import HTTPException
from sqlalchemy import and_, func, or_, select, update

from app.models.cnki import CnkiDevice, CnkiJob, CnkiJobPaper, CnkiPaper
from app.models.feed import FeedItem
from app.models.user import User
from app.schemas.literature import LiteraturePaperOut, LiteratureSearchOut


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
    if (job.checkpoint or {}).get("reset_session"):
        return "queued"
    if job.lease_until and job.lease_until > now():
        return "working"
    return (job.last_report or {}).get("state", "queued") if (job.last_report or {}).get("state") in {
        "waiting_for_verification", "browser_closed", "paused", "error"
    } else "queued"


async def job_view(db, job: CnkiJob, *, papers=False, page_size: int | None = None, cursor: str | None = None) -> dict:
    # All paper writes take the same user lock. Taking it before a page watermark
    # prevents an in-flight transaction from committing an older unseen update.
    if papers and page_size is not None:
        await lock_user(db, job.user_id)
    count = await db.scalar(select(func.count()).select_from(CnkiJobPaper).where(CnkiJobPaper.job_id == job.id))
    result = {"id": job.id, "query": job.query, "category": job.category, "recurring": job.recurring,
              "enabled": job.enabled, "state": job_state(job), "count": count,
              "phase": (job.checkpoint or {}).get("phase", "index"),
              "page": (job.checkpoint or {}).get("page", 1), "total": (job.checkpoint or {}).get("total", 0),
              "detail": (job.last_report or {}).get("detail", ""), "last_completed_at": job.last_completed_at,
              "error_code": (job.last_report or {}).get("code"),
              "executor": (job.checkpoint or {}).get("executor", "server"),
              "document_kind": (job.checkpoint or {}).get("document_kind")}
    # Existing installations stored the denial only in the human-readable
    # receipt. Keep the recovery action available after upgrading those jobs.
    if (result["state"] == "paused" and not result["error_code"]
            and result["executor"] == "server" and "HTTP 403" in result["detail"]):
        result["error_code"] = "access_denied"
    if (job.checkpoint or {}).get("reset_session"):
        result.update(detail="正在备份旧会话并建立新的知网采集会话", error_code=None)
    if papers:
        statement = select(CnkiPaper).join(CnkiJobPaper, CnkiJobPaper.paper_id == CnkiPaper.id).where(
            CnkiJobPaper.job_id == job.id, CnkiPaper.user_id == job.user_id)
        if page_size is not None:
            after = after_id = None
            through = now()
            if cursor:
                try:
                    if len(cursor) > 2048:
                        raise ValueError()
                    owner_job, after_raw, after_id, through_raw = json.loads(base64.urlsafe_b64decode(cursor))
                    if owner_job != job.id or (after_id is not None and not isinstance(after_id, str)):
                        raise ValueError()
                    if after_id is not None:
                        UUID(after_id)
                    after = datetime.fromisoformat(after_raw) if after_raw else None
                    through = datetime.fromisoformat(through_raw) if through_raw else through
                    if (after and after.tzinfo is None) or through.tzinfo is None:
                        raise ValueError()
                    if (after_id and not after) or (after and after > through) or through > now():
                        raise ValueError()
                except (ValueError, TypeError, json.JSONDecodeError) as exc:
                    raise HTTPException(422, "Invalid CNKI result cursor") from exc
            if after:
                condition = CnkiPaper.updated_at > after
                if after_id:
                    condition = or_(condition, and_(CnkiPaper.updated_at == after, CnkiPaper.id > after_id))
                statement = statement.where(condition)
            statement = statement.where(CnkiPaper.updated_at <= through).order_by(CnkiPaper.updated_at, CnkiPaper.id)
            rows = list((await db.scalars(statement.limit(page_size + 1))).all())
            more = len(rows) > page_size
            rows = rows[:page_size]
            next_position = [job.id, rows[-1].updated_at.isoformat(), rows[-1].id, through.isoformat()] if more else [job.id, through.isoformat(), None, None]
            result.update(paper_ids=[p.id for p in rows], has_more=more,
                          next_cursor=base64.urlsafe_b64encode(json.dumps(next_position).encode()).decode())
        else:
            statement = statement.order_by(CnkiPaper.metadata_json["pub_date"].as_string().desc(),
                                           CnkiPaper.observed_at.desc(), CnkiPaper.id)
            if job.result_limit is not None:
                statement = statement.limit(job.result_limit)
            rows = list((await db.scalars(statement)).all())
        blocked = result["state"] in {"waiting_for_verification", "browser_closed", "paused", "error", "cancelled"}
        result["result"] = LiteratureSearchOut(
            query=job.query, papers=[as_literature(p.metadata_json) for p in rows], total=len(rows),
            candidate_count=count, source_counts={"cnki": count}, requested_sources=["cnki"],
            attempted_sources=["cnki"], search_mode="cnki_" + result["executor"] + "_browser", from_cache=job_state(job)=="completed",
            unavailable_sources=["cnki"] if blocked else [],
            warnings=[{"code": "source_incomplete", "sources": ["cnki"], "reason": result["state"],
                       "detail": result["detail"] or "知网本轮采集尚未完成，已取得的题录仍可使用。"}] if blocked else [],
            strategy_diagnostics={"cnki": {"state": result["state"], "page": result["page"],
                                           "count": count, "phase": (job.checkpoint or {}).get("phase", "index")}},
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


async def claim_job(db, device: CnkiDevice, *, executor="local", server_slots=1) -> dict | None:
    moment = now()
    active = list((await db.scalars(select(CnkiJob).where(CnkiJob.user_id == device.user_id,
            CnkiJob.enabled.is_(True), CnkiJob.lease_until > moment))).all())
    if active and (executor != "server" or len(active) >= min(server_slots, 2)
                   or any(j.lease_device_id != device.id or (j.checkpoint or {}).get("executor", "server") != "server" for j in active)):
        return None
    statement = select(CnkiJob).where(CnkiJob.user_id == device.user_id, CnkiJob.enabled.is_(True)).order_by(CnkiJob.recurring, CnkiJob.created_at)
    if db.get_bind().dialect.name == "postgresql":
        statement = statement.with_for_update(skip_locked=True)
    jobs = list((await db.scalars(statement)).all())
    if executor == "server":
        # A reset belongs to the shared browser, not one category's tab. The
        # coordinator must quiesce both lanes before any new claim for this user.
        if any((job.checkpoint or {}).get("reset_session") for job in jobs):
            return None
        # Collect every queued query's indexes before spending time on abstracts.
        jobs.sort(key=lambda job: (
            (job.checkpoint or {}).get("phase") == "metadata"
            and not (job.checkpoint or {}).get("finished"),
            (job.last_report or {}).get("state") in {"error", "waiting_for_verification"},
        ))
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
                "recover_session": bool(checkpoint.get("recover_session")),
                "check_session": bool(checkpoint.get("check_session")),
                "resume_verification": bool(checkpoint.get("resume_verification")),
                "document_kind": checkpoint.get("document_kind"),
                "page":checkpoint["page"], "accepted":checkpoint["accepted"], "since":checkpoint["since"],
                "phase":checkpoint.get("phase", "index"), "total":checkpoint.get("total", 0),
                "limit":job.result_limit, "lease":lease}
    return None


async def request_session_reset(db, user_id, job_ids, *, lang="zh"):
    """Explicit user action only; polls, retries and recurring jobs never call it."""
    await lock_user(db, user_id)
    jobs = list((await db.scalars(select(CnkiJob).where(CnkiJob.user_id == user_id))).all())
    selected = [job for job in jobs if job.id in job_ids and job.enabled]
    if len(selected) != len(job_ids):
        raise HTTPException(404, say({"zh": "采集任务不存在", "en": "Collection task not found."}, lang))
    if any((job.checkpoint or {}).get("executor", "server") != "server" for job in selected):
        raise HTTPException(422, say({"zh": "此操作仅适用于平台管理的知网浏览器", "en": "Only the managed CNKI browser can be reset."}, lang))
    selected = [job for job in selected if job_state(job) != "completed"]
    if not selected:
        raise HTTPException(409, say({"zh": "这些检索已经完成，无需重建会话", "en": "These searches are complete; a session reset is unnecessary."}, lang))
    request_id = next(((job.checkpoint or {}).get("reset_session") for job in jobs
                       if job.enabled and (job.checkpoint or {}).get("reset_session")), None)
    if not request_id:
        moment = now()
        attempts = [(job.checkpoint or {}).get("session_reset_at") for job in jobs]
        if any(stamp and moment-datetime.fromisoformat(stamp) < timedelta(seconds=60) for stamp in attempts):
            raise HTTPException(429, say({"zh": "刚刚已重建过知网会话，请先检查新窗口，至少一分钟后再试", "en": "The CNKI session was just reset. Check the new window and wait at least one minute before retrying."}, lang))
        request_id = str(uuid4())
    for job in selected:
        job.checkpoint = {**(job.checkpoint or {}), "reset_session": request_id}
    return {"state": "queued", "request_id": request_id}


async def leased_job(db, device: CnkiDevice, job_id: str, lease: str, *, lang: str = "zh") -> CnkiJob:
    job = await db.scalar(select(CnkiJob).where(CnkiJob.id==job_id, CnkiJob.user_id==device.user_id).with_for_update())
    if (not job or not job.enabled or job.lease_device_id != device.id or not job.lease_hash
            or not secrets.compare_digest(job.lease_hash, digest(lease))
            or not job.lease_until or job.lease_until < now()):
        raise HTTPException(409, say({"zh": "任务已暂停或已由新的采集会话接续", "en": "This task is paused or has been taken over by another collection session."}, lang))
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
    count = await db.scalar(select(func.count()).select_from(CnkiJobPaper).where(CnkiJobPaper.job_id==job.id))
    for value in batch.papers:
        incoming = value.model_dump()
        kind = checkpoint.get("document_kind")
        if kind:
            expected_category = {"journal":"中文期刊", "thesis":"学位论文", "conference":"会议文章", "book":"书籍", "other":"其他"}[kind]
            if as_literature(incoming).publication_category != expected_category:
                continue
        incoming["transport"] = "server_browser" if (job.checkpoint or {}).get("executor", "server") == "server" else "user_browser"
        key = identity(incoming)
        paper_id = str(uuid5(NAMESPACE_URL, device.user_id+"/cnki/"+key))
        link = await db.get(CnkiJobPaper, (job.id, paper_id))
        if kind and job.result_limit is not None and not link and count >= job.result_limit:
            continue
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
        paper.updated_at = moment
        if incoming.get("metadata_attempted"):
            paper.observed_at = moment
        if not link:
            # These mappers have no ORM relationship to order their inserts.
            # PostgreSQL checks the FK immediately: persist the index before
            # linking it to a job, including the first uncached result page.
            await db.flush([paper])
            db.add(CnkiJobPaper(job_id=job.id, paper_id=paper_id))
            count += 1
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
    return {"accepted":True, "page":checkpoint["page"], "count":count}


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


async def create_job(db, user, payload, *, page_size: int | None = None) -> dict:
    """Shared job reuse/retry policy for the UI and authenticated E2E source access."""
    await lock_user(db, user.id)
    if not payload.recurring:
        previous = await db.scalar(select(CnkiJob).where(CnkiJob.user_id==user.id, CnkiJob.query==payload.query,
            CnkiJob.recurring.is_(False), CnkiJob.enabled.is_(True), CnkiJob.category==payload.category,
            CnkiJob.result_limit==payload.limit,
            (CnkiJob.checkpoint["document_kind"].as_string()==payload.document_kind
             if payload.document_kind else CnkiJob.checkpoint["document_kind"].as_string().is_(None)),
            ).order_by(CnkiJob.created_at.desc()).limit(1))
        if previous and (previous.checkpoint or {}).get("executor", "server") == payload.executor and (not previous.last_completed_at or now()-previous.last_completed_at < timedelta(hours=24)):
            # An explicit new search is a retry request, unlike background polling.
            # Preserve accumulated papers/checkpoints and any active lease.
            if job_state(previous) in {"paused", "error", "browser_closed"}:
                previous.checkpoint = {**(previous.checkpoint or {}), "check_session": payload.executor == "server"}
                if job_state(previous) == "paused":
                    previous.checkpoint = {**(previous.checkpoint or {}), "recover_session": True}
                previous.last_report = {"state": "queued"}
                previous.last_report_at = now()
            return await job_view(db,previous,papers=True,page_size=page_size)
    else:
        previous = await db.scalar(select(CnkiJob).where(CnkiJob.user_id==user.id, CnkiJob.query==payload.query,
            CnkiJob.recurring.is_(True), CnkiJob.enabled.is_(True), CnkiJob.category==payload.category).limit(1))
        if previous and (previous.checkpoint or {}).get("executor", "server") == payload.executor:
            return await job_view(db,previous,papers=True,page_size=page_size)
    job = CnkiJob(user_id=user.id, query=payload.query, category=payload.category,
                  field={"journal":"LY","scholar":"AU"}.get(payload.category,"SU"),
                  recurring=payload.recurring, result_limit=None if payload.recurring else payload.limit,
                  created_at=now(), checkpoint={"executor":payload.executor,
                                               **({"check_session":True} if payload.executor == "server" and not payload.recurring else {}),
                                               **({"document_kind":payload.document_kind} if payload.document_kind else {})})
    db.add(job)
    await db.flush()
    return await job_view(db,job,papers=True,page_size=page_size)

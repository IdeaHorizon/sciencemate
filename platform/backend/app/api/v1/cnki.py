"""Pair a local browser and exchange metadata-only, user-scoped CNKI jobs."""
from __future__ import annotations

import hmac
import io
import secrets
import zipfile
from datetime import timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select, update

from app.auth import credential_fingerprint, get_current_user
from app.database import get_db
from app.models.cnki import CnkiDevice, CnkiJob, CnkiJobPaper, CnkiPaper
from app.models.user import User
from app.schemas.cnki import BatchIn, HeartbeatIn, IndexIn, JobIn, PairIn, ReportIn, SessionResetIn
from app.services import cnki as service
from app.services.user_interface import Language, language_on_screen, say

router = APIRouter()
bearer = HTTPBearer(auto_error=False)


async def browser_device(auth: HTTPAuthorizationCredentials | None = Depends(bearer), db=Depends(get_db), lang: Language = Depends(language_on_screen)):
    if auth is None:
        raise HTTPException(401, say({"zh": "请重新连接本机采集扩展", "en": "Reconnect the local collection extension."}, lang))
    device = await db.scalar(select(CnkiDevice).where(CnkiDevice.token_hash==service.digest(auth.credentials), CnkiDevice.revoked_at.is_(None)))
    user = await db.get(User, device.user_id) if device else None
    if not device or not user or not user.is_active or not hmac.compare_digest(device.credential_fingerprint, credential_fingerprint(user)):
        raise HTTPException(401, say({"zh": "本机采集连接已失效，请重新连接", "en": "The local collection connection has expired. Reconnect it."}, lang))
    await service.lock_user(db, device.user_id)
    await db.refresh(device)
    if device.revoked_at:
        raise HTTPException(401, say({"zh": "本机采集连接已失效，请重新连接", "en": "The local collection connection has expired. Reconnect it."}, lang))
    return device


@router.post("/pairing")
async def pairing(user=Depends(get_current_user), db=Depends(get_db)):
    code = secrets.token_urlsafe(24)
    device = CnkiDevice(user_id=user.id, pair_hash=service.digest(code), pair_expires_at=service.now()+timedelta(minutes=10),
                        created_at=service.now(), credential_fingerprint=credential_fingerprint(user))
    db.add(device)
    await db.flush()
    return {"code":code, "expires_at":device.pair_expires_at}


@router.post("/pair")
async def pair(payload: PairIn, db=Depends(get_db), lang: Language = Depends(language_on_screen)):
    token = secrets.token_urlsafe(40)
    device_id = await db.scalar(update(CnkiDevice).where(
        CnkiDevice.pair_hash==service.digest(payload.code), CnkiDevice.claimed_at.is_(None),
        CnkiDevice.revoked_at.is_(None), CnkiDevice.pair_expires_at > service.now(),
    ).values(token_hash=service.digest(token), pair_hash=None, claimed_at=service.now(), name=payload.name).returning(CnkiDevice.id))
    if not device_id:
        raise HTTPException(400, say({"zh": "连接码无效或已过期，请在平台重新生成", "en": "The pairing code is invalid or expired. Generate a new code in the platform."}, lang))
    return {"token":token, "device_id":device_id}


@router.get("/status")
async def status(include_jobs: bool = True, user=Depends(get_current_user), db=Depends(get_db)):
    from app.services.cnki_browser import availability
    devices = (await db.scalars(select(CnkiDevice).where(CnkiDevice.user_id==user.id, CnkiDevice.claimed_at.is_not(None)))).all()
    jobs = (await db.scalars(select(CnkiJob).where(CnkiJob.user_id==user.id).order_by(CnkiJob.created_at.desc()).limit(100))).all() if include_jobs else []
    return {"collector": availability(), "devices":[service.device_view(d) for d in devices], "jobs":[await service.job_view(db,j) for j in jobs]}


@router.delete("/devices/{device_id}")
async def revoke(device_id: str, user=Depends(get_current_user), db=Depends(get_db), lang: Language = Depends(language_on_screen)):
    await service.lock_user(db, user.id)
    device = await db.scalar(select(CnkiDevice).where(CnkiDevice.id==device_id, CnkiDevice.user_id==user.id))
    if not device:
        raise HTTPException(404, say({"zh": "连接不存在", "en": "The connection does not exist."}, lang))
    device.revoked_at = service.now()
    return {"ok":True}


@router.post("/jobs")
async def create_job(payload: JobIn, page_size: int | None = Query(default=None, ge=1, le=500), user=Depends(get_current_user), db=Depends(get_db)):
    return await service.create_job(db, user, payload, page_size=page_size)


@router.post("/session/reset")
async def reset_session(payload: SessionResetIn, user=Depends(get_current_user), db=Depends(get_db), lang: Language = Depends(language_on_screen)):
    return await service.request_session_reset(db, user.id, {str(value) for value in payload.job_ids}, lang=lang)


@router.post("/jobs/{job_id}/retry")
async def retry(job_id: str, user=Depends(get_current_user), db=Depends(get_db), lang: Language = Depends(language_on_screen)):
    await service.lock_user(db, user.id)
    job = await db.scalar(select(CnkiJob).where(CnkiJob.id==job_id, CnkiJob.user_id==user.id).with_for_update())
    if not job or not job.enabled:
        raise HTTPException(404, say({"zh": "采集任务不存在", "en": "The collection task does not exist."}, lang))
    if service.job_state(job) not in {"working", "completed"}:
        if service.job_state(job) == "paused":
            job.checkpoint = {**(job.checkpoint or {}), "recover_session": True}
        job.last_report, job.last_report_at = {"state":"queued"}, service.now()
    return await service.job_view(db,job,papers=True)


@router.get("/jobs/{job_id}")
async def get_job(job_id: str, page_size: int | None = Query(default=None, ge=1, le=500), cursor: str | None = Query(default=None, max_length=2048), user=Depends(get_current_user), db=Depends(get_db), lang: Language = Depends(language_on_screen)):
    job = await db.scalar(select(CnkiJob).where(CnkiJob.id==job_id, CnkiJob.user_id==user.id))
    if not job:
        raise HTTPException(404, say({"zh": "采集任务不存在", "en": "The collection task does not exist."}, lang))
    return await service.job_view(db,job,papers=True,page_size=page_size,cursor=cursor)


@router.delete("/jobs/{job_id}")
async def cancel(job_id: str, user=Depends(get_current_user), db=Depends(get_db), lang: Language = Depends(language_on_screen)):
    await service.lock_user(db, user.id)
    job = await db.scalar(select(CnkiJob).where(CnkiJob.id==job_id, CnkiJob.user_id==user.id).with_for_update())
    if not job:
        raise HTTPException(404, say({"zh": "采集任务不存在", "en": "The collection task does not exist."}, lang))
    job.enabled, job.lease_until = False, service.now()
    await db.flush()
    if job.recurring:
        for paper in (await db.scalars(select(CnkiPaper).join(CnkiJobPaper, CnkiJobPaper.paper_id==CnkiPaper.id).where(CnkiJobPaper.job_id==job.id))).all():
            await service.sync_feed_paper(db,paper)
    return {"ok":True}


@router.post("/worker/poll")
async def poll(payload: HeartbeatIn, device=Depends(browser_device), db=Depends(get_db)):
    device.last_seen_at, device.last_report = service.now(), payload.model_dump()
    job = await service.claim_job(db,device) if payload.state=="idle" else None
    return {"job":job}


@router.post("/worker/cache")
async def cache(papers: list[IndexIn], device=Depends(browser_device), db=Depends(get_db), lang: Language = Depends(language_on_screen)):
    if len(papers)>20:
        raise HTTPException(422, say({"zh": "每批最多 20 篇", "en": "Each batch can contain at most 20 papers."}, lang))
    keys = [service.identity(p.model_dump()) for p in papers]
    rows = (await db.scalars(select(CnkiPaper).where(CnkiPaper.user_id==device.user_id, CnkiPaper.identity.in_(keys)))).all()
    return {"papers":[p.metadata_json for p in rows if p.metadata_json.get("metadata_attempted")
            and not p.metadata_json.get("abstract_truncated")
            and (p.metadata_json.get("abstract") or service.now()-p.observed_at < timedelta(hours=24))]}


@router.post("/worker/jobs/{job_id}/batch")
async def batch(job_id: str, payload: BatchIn, device=Depends(browser_device), db=Depends(get_db), lang: Language = Depends(language_on_screen)):
    job = await service.leased_job(db,device,job_id,payload.lease,lang=lang)
    return await service.receive_batch(db,device,job,payload,lang=lang)


@router.post("/worker/jobs/{job_id}/report")
async def report(job_id: str, payload: ReportIn, device=Depends(browser_device), db=Depends(get_db), lang: Language = Depends(language_on_screen)):
    job = await service.leased_job(db,device,job_id,payload.lease,lang=lang)
    state = payload.model_dump(exclude={"lease"})
    job.last_report_at, job.last_report = service.now(), state
    job.lease_until = service.now()+timedelta(seconds=90) if payload.state=="working" else service.now()
    device.last_seen_at, device.last_report = service.now(), state
    return {"ok":True}


@router.get("/extension.zip")
async def extension_bundle():
    root = Path(__file__).resolve().parents[2]/"assets"/"cnki-extension"
    output = io.BytesIO()
    with zipfile.ZipFile(output,"w",zipfile.ZIP_DEFLATED) as archive:
        for name in ("manifest.json","background.js","cnki-page.js","platform-bridge.js","popup.html","popup.js","popup.css","README.txt"):
            archive.write(root/name, "sciencemate-cnki/"+name)
    return Response(output.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition":'attachment; filename="sciencemate-cnki.zip"'})

"""Durable managed CNKI queue: leases, private cache, partial batches, daily refresh."""
from __future__ import annotations

import asyncio
import logging
import re
import time
import unicodedata
from datetime import timedelta
from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import select

from app.config import settings, the_data_root
from app.database import get_session_factory
from app.models.cnki import CnkiJob, CnkiPaper, CnkiJobPaper
from app.models.user import User
from app.schemas.cnki import BatchIn, IndexIn
from app.services import cnki as store
from app.services.cnki_browser import Browser, CollectionError

logger = logging.getLogger(__name__)
MESSAGES = {
    "waiting_for_verification": "知网要求人工验证，平台已保留进度；请在运行平台的电脑上完成知网验证（远程部署请联系管理员），之后自动继续。",
    "rate_limited": "知网暂时限制请求，已保留进度，稍后自动重试。",
    "access_denied": "知网拒绝当前访问（HTTP 403），已暂停该会话的检索和摘要请求；已取得的题录仍可使用。可在运行平台的电脑上检查知网访问，再点击“重试知网”。",
    "session_paused": "知网会话因上次 HTTP 403 暂停，当前请求未发送到知网；这不代表知网刚刚再次拒绝。已取得的题录仍可使用，稍后重新搜索或点击“重试知网”可检查恢复情况。",
    "virtual_display_unavailable": "后台浏览器需要虚拟桌面组件，请管理员安装 Xvfb 与 xvfb-run。",
    "browser_not_installed": "采集服务器未找到 Chrome 或 Edge，请联系管理员。",
    "browser_start_failed": "知网采集浏览器启动失败，请管理员检查浏览器运行环境。",
    "browser_profile_in_use": "旧知网采集资料仍被占用或无法移动，请关闭平台打开的采集浏览器后再重建会话；已获取的文献保留。",
    "browser_profile_backup_failed": "知网采集资料备份未完成，已停止重建会话；请检查本机资料目录的访问权限和可用空间。",
    "browser_activation_failed": "知网检索控件未能正常操作，已保留进度；请检查当前知网页面。",
    "browser_activation_moved": "知网页面变化导致操作未完成，已保留进度，稍后自动重试。",
    "metadata_title_mismatch": "知网返回的题录与标题不一致，已暂停本次补齐。",
    "result_pages_changed": "知网结果分页发生变化，已保留获取到的题录，请重新检索。",
    "result_type_mismatch": "知网返回的文献类型与当前类别不一致，已停止翻页并保留进度，等待修正分类识别。",
    "results_unavailable": "知网暂未提供检索结果，已保留进度，稍后重试；这不代表已确认没有匹配文献。",
    "cnki_timeout": "知网检索页面响应超时，已保留进度，稍后自动重试；可先查看其他来源结果。",
    "TimeoutError": "知网浏览器操作超时，已保留进度，稍后自动重试；可先查看其他来源结果。",
    "search_http_417": "知网题录接口拒绝本次请求（HTTP 417），该来源尚未完成，已保留进度；可先查看其他来源结果。",
    "verification_result_pending": "正在等待知网载入验证后的结果，已保留进度，不会重复提交查询。",
}


def clean(row: dict) -> dict:
    value = {k:v for k,v in row.items() if k in IndexIn.model_fields}
    if row.get("exportId"):
        value["export_id"] = row["exportId"]
    return value


def matching_export(row: dict, value: dict) -> bool:
    def title(text):
        return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text or "")).casefold()
    original, exported = title(row.get("title")), title(value.get("title"))
    if original == exported and original:
        return True
    # CNKI's thesis export can omit the subtitle after an em dash. Accept that
    # precise variation only with matching authors AND publication year.
    main = re.split(r"——|—|--", original, maxsplit=1)[0]
    return (main != original and len(main) >= 16 and exported == main
            and bool(row.get("year")) and row["year"] == value.get("year")
            and bool({title(a) for a in row.get("authors", []) if title(a)}
                     & {title(a) for a in value.get("authors", []) if title(a)}))


class Collector:
    def __init__(self, sessions=None, browser=None):
        self.sessions = sessions
        self.browser = browser
        self.task = None

    def start(self):
        if settings.cnki_server_enabled and (self.task is None or self.task.done()):
            self.task = asyncio.create_task(self.run(), name="cnki-collector")

    async def stop(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None

    async def claim(self, user_id=None, *, server_slots=1):
        async with self.sessions.begin() as db:
            ids = (await db.scalars(select(CnkiJob.user_id).join(User, User.id==CnkiJob.user_id)
                .where(CnkiJob.enabled.is_(True), User.is_active.is_(True))
                .order_by(CnkiJob.last_report_at.asc().nullsfirst(), CnkiJob.created_at))).all()
        for owner in dict.fromkeys(ids):
            if user_id is not None and owner != user_id:
                continue
            async with self.sessions.begin() as db:
                await store.lock_user(db, owner)
                user = await db.get(User, owner)
                if not user or not user.is_active:
                    continue
                device = SimpleNamespace(id=str(uuid5(NAMESPACE_URL, owner+"/cnki-server")), user_id=owner)
                job = await store.claim_job(db, device, executor="server", server_slots=server_slots)
                if job:
                    return device, job
        return None

    async def report(self, device, task, state="working", detail="", *, metrics=None, code=None):
        async with self.sessions.begin() as db:
            await store.lock_user(db, device.user_id)
            job = await store.leased_job(db, device, task["id"], task["lease"])
            job.last_report_at, job.last_report = store.now(), {"state":state, "detail":detail}
            if code:
                job.last_report = {**job.last_report, "code": code}
            job.lease_until = store.now()+timedelta(seconds=90) if state=="working" else store.now()
            if metrics:
                job.checkpoint = {**job.checkpoint, **metrics}

    async def batch(self, device, task, **values):
        async with self.sessions.begin() as db:
            await store.lock_user(db, device.user_id)
            job = await store.leased_job(db, device, task["id"], task["lease"])
            return await store.receive_batch(db, device, job, BatchIn(lease=task["lease"], page=task["page"], **values))

    async def cache(self, user_id, rows):
        keys = [store.identity(p) for p in rows]
        async with self.sessions() as db:
            papers = (await db.scalars(select(CnkiPaper).where(CnkiPaper.user_id==user_id, CnkiPaper.identity.in_(keys)))).all()
            return {p.identity:clean(p.metadata_json) for p in papers if p.metadata_json.get("metadata_attempted")
                    and not p.metadata_json.get("abstract_truncated")
                    and (p.metadata_json.get("abstract") or store.now()-p.observed_at < timedelta(hours=24))}

    async def metadata(self, row, cached):
        if cached:
            return {**clean(row), **cached, "url":row["url"],
                    "export_id":row.get("export_id") or row.get("exportId", "")}
        paper = clean(row)
        try:
            value = await self.browser.execute("export", {"exportId":row.get("export_id") or row.get("exportId", "")})
            if not matching_export(row, value):
                raise CollectionError("metadata_title_mismatch")
            for key, item in clean(value).items():
                if item or key not in paper:
                    paper[key] = item
            paper["title"] = row["title"]  # Keep the full title and stable index key.
            if paper.get("abstract_truncated") or not paper.get("abstract"):
                detail = await self.browser.full_abstract(row["url"])
                title = re.sub(r"\s*(网络首发|附视频)\s*$", "", detail.get("title") or "").strip()
                if title == row["title"] and len(detail.get("abstract", "")) > len(paper.get("abstract", "")):
                    paper.update(abstract=detail["abstract"], abstract_truncated=detail.get("abstract_truncated", False))
        except CollectionError as exc:
            if str(exc) in {"waiting_for_verification", "rate_limited", "access_denied", "session_paused"}:
                raise
            # Retain the original index, never merge a different paper's export.
            # A bad export must not restart the browser or the whole query.
            paper["metadata_error"] = str(exc)
        except TimeoutError:
            paper["metadata_error"] = "metadata_timeout"
        paper["metadata_attempted"] = True
        return paper

    async def finish_index(self, device, task, elapsed):
        async with self.sessions.begin() as db:
            await store.lock_user(db, device.user_id)
            job = await store.leased_job(db, device, task["id"], task["lease"])
            checkpoint = dict(job.checkpoint or {})
            checkpoint.update(phase="metadata", index_seconds=round(elapsed, 3))
            job.checkpoint = checkpoint
            job.lease_until = store.now()
            job.last_report_at, job.last_report = store.now(), {
                "state":"queued", "detail":"中文题录已获取，等待读取知网摘要"}

    async def enrich(self, device, task):
        started = time.monotonic()
        async with self.sessions() as db:
            rows = [clean(p.metadata_json) for p in (await db.scalars(select(CnkiPaper)
                .join(CnkiJobPaper, CnkiJobPaper.paper_id==CnkiPaper.id)
                .where(CnkiJobPaper.job_id==task["id"], CnkiPaper.user_id==device.user_id))).all()]
        cached = await self.cache(device.user_id, rows)
        pending = [row for row in rows if store.identity(row) not in cached]
        if pending:
            await self.browser.ensure(device.user_id)
            await self.browser.prepare_metadata()
        for i in range(0, len(pending), 2):
            await self.report(device, task, detail=f"中文题录已获取，正在读取知网摘要 {i+1}–{min(i+2,len(pending))}/{len(pending)} 篇")
            replies = await asyncio.gather(*(self.metadata(row, None) for row in pending[i:i+2]), return_exceptions=True)
            await self.batch(device, task, papers=[p for p in replies if isinstance(p,dict)], total=task["total"])
            for result in replies:
                if isinstance(result, BaseException):
                    raise result
        async with self.sessions.begin() as db:
            await store.lock_user(db, device.user_id)
            job = await store.leased_job(db, device, task["id"], task["lease"])
            job.checkpoint = {**job.checkpoint, "metadata_seconds":round(time.monotonic()-started, 3)}
        await self.batch(device, task, papers=[], total=task["total"], finished=True)

    async def collect(self, device, task):
        if task.get("phase") == "metadata":
            return await self.enrich(device, task)
        started = time.monotonic()
        # Older checkpoints may already have consumed this request's page
        # budget. Finish without reopening the browser and replaying those pages.
        if task["limit"] is not None and (task["page"]-1)*20 >= task["limit"]:
            await self.finish_index(device, task, time.monotonic()-started)
            return
        await self.report(device, task, detail="正在连接知网")
        await self.browser.ensure(device.user_id)
        await self.report(device, task, detail="正在向知网提交检索并等待题录")
        options = {"resume_verification": task.get("resume_verification", False)}
        if task.get("document_kind"):
            options["document_kind"] = task["document_kind"]
        state = await self.browser.search(task["query"], task["field"], **options)
        await self.report(device, task, metrics={"resume_verification": False})
        while state["page"] < task["page"] and state["hasNext"]:
            await self.report(device, task, detail="正在恢复已保存的分页进度")
            state = await self.browser.execute("next")
        if state["page"] != task["page"]:
            raise CollectionError("result_pages_changed")
        while True:
            if not state["rows"] and not state.get("emptyConfirmed"):
                raise CollectionError("unrecognized_results_page")
            remaining = 20 if task["limit"] is None else max(0, task["limit"]-(task["page"]-1)*20)
            wanted = state["rows"][:remaining]
            def older(row):
                date = row.get("pub_date", "")
                return bool(task["since"] and re.match(r"\d{4}-\d{2}-\d{2}", date) and date[:10] < task["since"])
            within = [row for row in wanted if not older(row)]
            category = {"journal":"中文期刊", "thesis":"学位论文", "conference":"会议文章", "book":"书籍"}.get(task.get("document_kind"))
            if category and within and not any(store.as_literature(row).publication_category == category for row in within):
                raise CollectionError("result_type_mismatch")
            # A single search has a bounded recall budget, not a target to fill
            # after filtering. In particular, CNKI's "other" tab is CROSSDB:
            # do not traverse its entire mixed catalogue to find 200 survivors.
            finished = (not state["hasNext"] or not wanted or
                        (task["limit"] is not None and task["page"]*20 >= task["limit"]) or
                        (state.get("dateSorted") and bool(state["rows"]) and all(older(r) for r in state["rows"])))
            # Page acknowledgement follows durable index storage, independent
            # of slower metadata exports. Restart resumes at the next page.
            receipt = await self.batch(device, task, papers=[clean(row) for row in within], total=state["total"], page_complete=not finished)
            if task.get("document_kind") and task["limit"] is not None and receipt["count"] >= task["limit"]:
                finished = True
            if finished:
                await self.finish_index(device, task, time.monotonic()-started)
                return
            task = {**task, "page":task["page"]+1, "accepted":[]}
            state = await self.browser.execute("next")

    async def process(self, device, task):
        started = time.monotonic()
        async def heartbeat():
            while True:
                await asyncio.sleep(20)
                # Renewal must preserve the current progress text.
                async with self.sessions.begin() as db:
                    await store.lock_user(db, device.user_id)
                    job = await store.leased_job(db, device, task["id"], task["lease"])
                    job.lease_until = store.now()+timedelta(seconds=90)
        pulse = asyncio.create_task(heartbeat())
        try:
            if task.get("recover_session") or task.get("check_session"):
                # Consume once per explicit retry. A second denial pauses again;
                # it must not turn into an unattended browser restart loop.
                await self.report(device, task, detail="正在检查知网会话", metrics={"recover_session": False, "check_session": False})
                await self.browser.ensure(device.user_id)
                if task.get("recover_session"):
                    await self.browser.recover_session()
                else:
                    await self.browser.check_session()
            await self.collect(device, task)
        except asyncio.CancelledError:
            try:
                await self.report(device, task, "queued", "采集进度已保存，等待继续")
            except Exception:
                pass  # Another worker may already own an expired lease.
            raise
        except Exception as exc:
            code = str(exc) if isinstance(exc, CollectionError) else type(exc).__name__
            waiting = code in {"waiting_for_verification", "verification_result_pending", "search_http_417"}
            state = "waiting_for_verification" if waiting else "paused" if code in {"access_denied", "session_paused", "result_type_mismatch"} else "error"
            try:
                await self.report(device, task, state, MESSAGES.get(code, "知网采集暂时失败，已保留进度，稍后自动重试。"),
                                  code=code,
                                  metrics={f"{task.get('phase', 'index')}_seconds":round(time.monotonic()-started, 3),
                                           **({"resume_verification": True} if waiting and task.get("phase", "index") == "index" else {})})
            except Exception:
                pass  # Cancelled/expired leases must not be overwritten.
            logger.info("CNKI collection job=%s kind=%s phase=%s state=%s code=%s endpoint=%s http_status=%s",
                        task["id"], task.get("document_kind"), task.get("phase", "index"), state, code,
                        getattr(exc, "endpoint", None), getattr(exc, "http_status", None))
            if code in {"browser_start_failed", "browser_start_timeout", "browser_command_failed", "ConnectionClosedError", "ConnectError"}:
                await self.browser.close()
        finally:
            pulse.cancel()
            await asyncio.gather(pulse, return_exceptions=True)

    async def reset_requested_session(self, active):
        """One manual reset for all categories, serialized by the worker lock."""
        owner = next((owner for owner, _ in active.values()), None)
        async with self.sessions.begin() as db:
            statement = select(CnkiJob).join(User, User.id == CnkiJob.user_id).where(
                CnkiJob.enabled.is_(True), User.is_active.is_(True),
                CnkiJob.checkpoint["reset_session"].as_string().is_not(None),
            ).order_by(CnkiJob.created_at)
            if owner is not None:
                statement = statement.where(CnkiJob.user_id == owner)
            request = await db.scalar(statement.limit(1))
            if request is None:
                return False
            owner, request_id = request.user_id, request.checkpoint["reset_session"]
        # Cancellation reports preserve acknowledged pages and stored papers.
        # A lane must never close the shared browser under its sibling's request.
        for _, work in active.values():
            work.cancel()
        await asyncio.gather(*(work for _, work in active.values()), return_exceptions=True)
        active.clear()
        code = None
        async with self.sessions.begin() as db:
            await store.lock_user(db, owner)
            jobs = list((await db.scalars(select(CnkiJob).where(CnkiJob.user_id == owner))).all())
            selected = [job for job in jobs if (job.checkpoint or {}).get("reset_session") == request_id]
            needed = any(job.enabled and store.job_state(job) != "completed" for job in selected)
        if needed:
            # Browser shutdown can take seconds. Do not hold the database lock
            # while waiting; new requests join the same persisted reset ID.
            try:
                await self.browser.reset_session(owner, request_id)
            except Exception as exc:
                code = str(exc) if isinstance(exc, CollectionError) else "browser_profile_backup_failed"
        async with self.sessions.begin() as db:
            await store.lock_user(db, owner)
            jobs = list((await db.scalars(select(CnkiJob).where(CnkiJob.user_id == owner))).all())
            selected = [job for job in jobs if (job.checkpoint or {}).get("reset_session") == request_id]
            if needed:
                moment = store.now()
                for job in jobs:
                    if (job.checkpoint or {}).get("executor", "server") != "server":
                        continue
                    if not code:
                        job.checkpoint = {**(job.checkpoint or {}), "resume_verification": False,
                                          "recover_session": False, "check_session": False}
                    if job in selected:
                        job.checkpoint = {**job.checkpoint, "session_reset_at": moment.isoformat()}
                        job.lease_until, job.lease_hash = moment, None
                        job.last_report_at = moment
                        job.last_report = {"state": "paused" if code else "queued", "code": code,
                            "detail": MESSAGES.get(code, "旧会话已备份，正在使用新会话继续检索；如知网要求验证，请在采集窗口完成。")}
                logger.info("CNKI session reset user=%s request=%s code=%s", owner, request_id, code or "ok")
            for job in selected:
                job.checkpoint = {key: value for key, value in job.checkpoint.items() if key != "reset_session"}
        return True

    async def run(self):
        from app.services.project_repository import _the_file_lock
        lock = None
        self.sessions = self.sessions or get_session_factory()
        self.browser = self.browser or Browser()
        active = {}
        try:
            while True:
                try:
                    if lock is None:
                        lock = _the_file_lock().try_hold(the_data_root()/"cnki/worker.lock")
                    if lock:
                        for slot, (_, work) in list(active.items()):
                            if work.done():
                                await asyncio.gather(work, return_exceptions=True)
                                del active[slot]
                        if await self.reset_requested_session(active):
                            continue
                        owner = next((owner for owner, _ in active.values()), None)
                        if len(active) < 2:
                            claimed = await self.claim(owner, server_slots=2)
                            if claimed:
                                device, task = claimed
                                slot = next(s for s in range(2) if s not in active)
                                child = Collector(self.sessions, self.browser.lane(slot))
                                active[slot] = (device.user_id, asyncio.create_task(child.process(device, task)))
                                continue
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning("CNKI queue unavailable: %s", type(exc).__name__)
                if active:
                    await asyncio.wait([work for _, work in active.values()], timeout=3,
                                       return_when=asyncio.FIRST_COMPLETED)
                else:
                    await asyncio.sleep(3)
        finally:
            for _, work in active.values():
                work.cancel()
            await asyncio.gather(*(work for _, work in active.values()), return_exceptions=True)
            await self.browser.close()
            if lock:
                lock.close()


cnki_collector = Collector()

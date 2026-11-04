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
    "access_denied": "知网检索页拒绝访问（HTTP 403），已保留采集进度。再次搜索或点击“重试知网”可重新启动采集；如浏览器出现验证，请完成验证。",
    "virtual_display_unavailable": "后台浏览器需要虚拟桌面组件，请管理员安装 Xvfb 与 xvfb-run。",
    "browser_not_installed": "采集服务器未找到 Chrome 或 Edge，请联系管理员。",
    "browser_start_failed": "知网采集浏览器启动失败，请管理员检查浏览器运行环境。",
    "metadata_title_mismatch": "知网返回的题录与标题不一致，已暂停本次补齐。",
    "result_pages_changed": "知网结果分页发生变化，已保留获取到的题录，请重新检索。",
    "results_unavailable": "知网返回“暂无数据，请稍后重试”，本组尚未取得题录，已保留进度；这不代表已确认没有匹配文献。",
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

    async def claim(self):
        async with self.sessions.begin() as db:
            ids = (await db.scalars(select(CnkiJob.user_id).join(User, User.id==CnkiJob.user_id)
                .where(CnkiJob.enabled.is_(True), User.is_active.is_(True))
                .order_by(CnkiJob.last_report_at.asc().nullsfirst(), CnkiJob.created_at))).all()
        for user_id in dict.fromkeys(ids):
            async with self.sessions.begin() as db:
                await store.lock_user(db, user_id)
                user = await db.get(User, user_id)
                if not user or not user.is_active:
                    continue
                device = SimpleNamespace(id=str(uuid5(NAMESPACE_URL, user_id+"/cnki-server")), user_id=user_id)
                job = await store.claim_job(db, device, executor="server")
                if job:
                    return device, job
        return None

    async def report(self, device, task, state="working", detail="", *, metrics=None):
        async with self.sessions.begin() as db:
            await store.lock_user(db, device.user_id)
            job = await store.leased_job(db, device, task["id"], task["lease"])
            job.last_report_at, job.last_report = store.now(), {"state":state, "detail":detail}
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
            if str(exc) in {"waiting_for_verification", "rate_limited", "access_denied"}:
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
                "state":"queued", "detail":"题录已入库，摘要等待补齐"}

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
            await self.report(device, task, detail=f"题录已入库，正在补齐摘要 {i+1}–{min(i+2,len(pending))}/{len(pending)} 篇")
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
        await self.report(device, task, detail="正在连接知网")
        await self.browser.ensure(device.user_id)
        state = await self.browser.search(task["query"], task["field"])
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
            finished = (not state["hasNext"] or not wanted or
                        (task["limit"] is not None and task["page"]*20 >= task["limit"]) or
                        (state.get("dateSorted") and bool(state["rows"]) and all(older(r) for r in state["rows"])))
            # Page acknowledgement follows durable index storage, independent
            # of slower metadata exports. Restart resumes at the next page.
            await self.batch(device, task, papers=[clean(row) for row in within], total=state["total"], page_complete=not finished)
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
            await self.collect(device, task)
        except asyncio.CancelledError:
            try:
                await self.report(device, task, "queued", "采集进度已保存，等待继续")
            except Exception:
                pass  # Another worker may already own an expired lease.
            raise
        except Exception as exc:
            code = str(exc) if isinstance(exc, CollectionError) else type(exc).__name__
            state = "waiting_for_verification" if code=="waiting_for_verification" else "paused" if code=="access_denied" else "error"
            try:
                await self.report(device, task, state, MESSAGES.get(code, "知网采集暂时失败，已保留进度，稍后自动重试。"),
                                  metrics={f"{task.get('phase', 'index')}_seconds":round(time.monotonic()-started, 3)})
            except Exception:
                pass  # Cancelled/expired leases must not be overwritten.
            logger.info("CNKI collection state=%s code=%s", state, code)
            if code in {"browser_start_failed", "browser_start_timeout", "browser_command_failed", "ConnectionClosedError", "ConnectError"}:
                await self.browser.close()
        finally:
            pulse.cancel()
            await asyncio.gather(pulse, return_exceptions=True)

    async def run(self):
        from app.services.project_repository import _the_file_lock
        lock = None
        self.sessions = self.sessions or get_session_factory()
        self.browser = self.browser or Browser()
        try:
            while True:
                try:
                    if lock is None:
                        lock = _the_file_lock().try_hold(the_data_root()/"cnki/worker.lock")
                    if lock:
                        task = await self.claim()
                        if task:
                            await self.process(*task)
                            continue
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.warning("CNKI queue unavailable: %s", type(exc).__name__)
                await asyncio.sleep(3)
        finally:
            await self.browser.close()
            if lock:
                lock.close()


cnki_collector = Collector()

"""Metadata-only browser collection: ownership, leases, resume and feed admission.

Uses an isolated SQLite file and ASGI transport; never contacts CNKI or the
running platform database. Run with --noconftest to avoid integration fixtures.
"""
import asyncio
import io
import zipfile
from datetime import timedelta
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI, Header, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.v1.cnki import router
from app.auth import get_current_user
from app.database import Base, get_db
from app.models.cnki import CnkiDevice, CnkiJob, CnkiJobPaper, CnkiPaper
from app.models.feed import FeedItem, FeedSource
from app.models.user import User
from app.services import cnki as service


@pytest.fixture
async def rig(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path}/cnki.db")
    tables = [User.__table__, FeedSource.__table__, FeedItem.__table__,
              CnkiDevice.__table__, CnkiJob.__table__, CnkiPaper.__table__, CnkiJobPaper.__table__]
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=tables))
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    users = [User(id=str(uuid4()), email=f"u{i}@test.invalid", hashed_password=f"test-hash-{i}",
                  display_name=f"User {i}", is_active=True) for i in range(2)]
    async with sessions.begin() as db:
        db.add_all(users)
    async def database():
        async with sessions.begin() as db:
            yield db
    async def current(x_test_user: str | None = Header(None)):
        if x_test_user not in {u.id for u in users}:
            raise HTTPException(401)
        return next(u for u in users if u.id == x_test_user)
    app = FastAPI()
    app.include_router(router, prefix="/cnki")
    app.dependency_overrides[get_db] = database
    app.dependency_overrides[get_current_user] = current
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        yield client, sessions, users
    await engine.dispose()


def owner(user):
    return {"X-Test-User": user.id}


async def pair(client, user):
    response = await client.post("/cnki/pairing", headers=owner(user))
    code = response.json()["code"]
    paired = await client.post("/cnki/pair", json={"code":code})
    assert paired.status_code == 200
    data = paired.json()
    return {"Authorization":"Bearer "+data["token"]}, data["device_id"], code


async def job(client, user, recurring=False, category="domain", query="格子玻尔兹曼"):
    response = await client.post("/cnki/jobs", headers=owner(user), json={
        "query":query, "recurring":recurring, "category":category, "limit":20, "executor":"local"})
    assert response.status_code == 200, response.text
    return response.json()


async def claim(client, headers):
    response = await client.post("/cnki/worker/poll", headers=headers, json={"state":"idle"})
    assert response.status_code == 200, response.text
    return response.json()["job"]


def paper(**changes):
    return {"title":"格子玻尔兹曼方法研究", "year":2026, "pub_date":"2026-10-20",
            "url":"https://kns.cnki.net/kcms2/article/abstract?v=example", "venue":"计算力学学报",
            "authors":["测试作者"], **changes}


async def upload(client, headers, task, papers, **fields):
    response = await client.post(f"/cnki/worker/jobs/{task['id']}/batch", headers=headers,
        json={"lease":task["lease"],"page":task["page"],"papers":papers,**fields})
    assert response.status_code == 200, response.text
    return response.json()


async def test_uncached_indexes_are_inserted_before_job_links_with_foreign_keys_enabled(rig):
    from types import SimpleNamespace
    from sqlalchemy import text
    from app.schemas.cnki import BatchIn
    client, sessions, users = rig
    created = await job(client, users[0])
    async with sessions.begin() as db:
        await db.execute(text("PRAGMA foreign_keys=ON"))
        assert await db.scalar(text("PRAGMA foreign_keys")) == 1
        row = await db.get(CnkiJob, created["id"])
        row.checkpoint = {**row.checkpoint, "page": 1}
        result = await service.receive_batch(db, SimpleNamespace(user_id=users[0].id), row,
            BatchIn(lease="x"*32, page=1, papers=[paper(),paper(title="未缓存的新论文")]))
        assert result["count"] == 2
        assert await db.scalar(select(func.count()).select_from(CnkiJobPaper)) == 2


async def test_pairing_is_atomic_single_use_and_scoped(rig):
    client, sessions, users = rig
    code = (await client.post("/cnki/pairing", headers=owner(users[0]))).json()["code"]
    replies = await asyncio.gather(*(client.post("/cnki/pair",json={"code":code}) for _ in range(2)))
    assert sorted(r.status_code for r in replies) == [200,400]
    token = next(r.json()["token"] for r in replies if r.status_code==200)
    assert (await client.get("/cnki/status",headers={"Authorization":"Bearer "+token})).status_code==401
    async with sessions() as db:
        device = await db.scalar(select(CnkiDevice))
        assert device.pair_hash is None and device.token_hash != token


@pytest.mark.parametrize("invalidate", ["revoke", "password", "inactive", "expired_code"])
async def test_invalid_credentials_cannot_claim(rig, invalidate):
    client,sessions,users=rig
    headers,device_id,_=await pair(client,users[0])
    if invalidate=="expired_code":
        code=(await client.post("/cnki/pairing",headers=owner(users[0]))).json()["code"]
        async with sessions.begin() as db:
            device=await db.scalar(select(CnkiDevice).where(CnkiDevice.pair_hash==service.digest(code)))
            device.pair_expires_at=service.now()-timedelta(seconds=1)
        assert (await client.post("/cnki/pair",json={"code":code})).status_code==400
        return
    if invalidate=="revoke":
        assert (await client.delete(f"/cnki/devices/{device_id}",headers=owner(users[1]))).status_code==404
        assert (await client.delete(f"/cnki/devices/{device_id}",headers=owner(users[0]))).status_code==200
    else:
        async with sessions.begin() as db:
            user=await db.get(User,users[0].id)
            if invalidate=="password": user.hashed_password="new-password-hash"
            else: user.is_active=False
    assert (await client.post("/cnki/worker/poll",headers=headers,json={})).status_code==401


async def test_two_devices_do_not_claim_parallel_jobs_and_resume_checkpoint(rig):
    client,sessions,users=rig
    first,_,_=await pair(client,users[0]); second,_,_=await pair(client,users[0])
    await job(client,users[0]); await job(client,users[0],query="另一项")
    tasks=await asyncio.gather(claim(client,first),claim(client,second))
    assert sum(t is not None for t in tasks)==1
    task=next(t for t in tasks if t); worker=first if tasks[0] else second
    other=second if tasks[0] else first
    await upload(client,worker,task,[paper(abstract="已补齐摘要",metadata_attempted=True)])
    # Browser death releases neither data nor acknowledged titles.
    async with sessions.begin() as db:
        row=await db.get(CnkiJob,task["id"]); row.lease_until=service.now()-timedelta(seconds=1)
    resumed=await claim(client,other)
    assert resumed["id"]==task["id"] and resumed["page"]==1
    assert resumed["accepted"]==[paper()["title"]]
    stale=await client.post(f"/cnki/worker/jobs/{task['id']}/batch",headers=worker,
                           json={"lease":task["lease"],"page":1,"papers":[]})
    assert stale.status_code==409
    await upload(client,other,resumed,[paper()],page_complete=True)
    replay=await upload(client,other,resumed,[paper()])
    assert replay["replayed"]
    async with sessions() as db:
        assert await db.scalar(select(func.count()).select_from(CnkiPaper))==1
        assert (await db.scalar(select(CnkiPaper))).metadata_json["abstract"]=="已补齐摘要"


async def test_cross_user_cache_and_job_privacy(rig):
    client,_,users=rig
    first,_,_=await pair(client,users[0]); other,_,_=await pair(client,users[1])
    created=await job(client,users[0]); task=await claim(client,first)
    await upload(client,first,task,[paper(abstract="私有题录",metadata_attempted=True)],finished=True)
    assert (await client.get(f"/cnki/jobs/{created['id']}",headers=owner(users[1]))).status_code==404
    assert await claim(client,other) is None
    assert (await client.post("/cnki/worker/cache",headers=other,json=[paper()])).json()["papers"]==[]
    assert (await client.post("/cnki/worker/cache",headers=first,json=[paper()])).json()["papers"][0]["abstract"]=="私有题录"
    result=(await client.get(f"/cnki/jobs/{created['id']}",headers=owner(users[0]))).json()["result"]
    assert result["papers"][0]["title_zh"]==paper()["title"]
    assert result["papers"][0]["publication_category"]=="中文期刊"


async def test_saved_indexes_return_chinese_journal_and_thesis_categories(rig):
    client,_,users=rig
    headers,_,_=await pair(client,users[0])
    created=await job(client,users[0]); task=await claim(client,headers)
    await upload(client,headers,task,[
        paper(title="DeepSeek赋能基层公务员数字素养的机遇与路径",document_type="期刊",metadata_attempted=True),
        paper(title="生成式人工智能使用对基层公务员创新行为的影响研究——基于C市的实证",document_type="硕士",metadata_attempted=True),
    ],finished=True)
    result=(await client.get(f"/cnki/jobs/{created['id']}",headers=owner(users[0]))).json()
    assert result["state"]=="completed" and result["count"]==2
    assert {p["publication_category"] for p in result["result"]["papers"]}=={"中文期刊","学位论文"}
    assert await claim(client,headers) is None  # Reclassification needs no new harvest.


async def test_daily_schedule_24h_overlap_and_unlimited_subscription(rig, monkeypatch):
    client,sessions,users=rig
    headers,_,_=await pair(client,users[0])
    await job(client,users[0],recurring=True,category="journal")
    base=service.now(); task=await claim(client,headers)
    assert task["field"]=="LY" and task["limit"] is None
    assert task["since"]==(base-timedelta(days=365)).date().isoformat()
    await upload(client,headers,task,[],finished=True,page_complete=True)
    assert await claim(client,headers) is None
    monkeypatch.setattr(service,"now",lambda:base+timedelta(hours=25))
    next_task=await claim(client,headers)
    assert next_task["page"]==1 and next_task["accepted"]==[]
    assert next_task["since"]==(base-timedelta(days=31)).date().isoformat()


async def test_feed_ready_gate_categories_window_and_unsubscribe(rig):
    from app.api.v1.feed import _visible_clause, _to_out
    from app.services.feed.ranking import publication_window_clause
    client,sessions,users=rig
    headers,_,_=await pair(client,users[0])
    j=await job(client,users[0],recurring=True,category="project")
    task=await claim(client,headers)
    record=paper(pub_date=(service.now()-timedelta(days=100)).date().isoformat())
    await upload(client,headers,task,[record])
    async with sessions() as db:
        assert (await db.scalar(select(FeedItem))).visibility=="withdrawn"
    await upload(client,headers,task,[{**record,"metadata_attempted":True,"abstract":"中文摘要"}],finished=True)
    async with sessions() as db:
        item=await db.scalar(select(FeedItem).where(_visible_clause(users[0]),publication_window_clause(service.now())))
        assert item and item.visibility=="private" and item.extra["cnki_categories"]==["project"]
        assert _to_out(item).abstract_zh=="中文摘要"
        assert await db.scalar(select(FeedItem).where(_visible_clause(users[1]))) is None
    assert (await client.delete(f"/cnki/jobs/{j['id']}",headers=owner(users[0]))).status_code==200
    async with sessions() as db:
        assert (await db.scalar(select(FeedItem))).visibility=="withdrawn"


async def test_notice_never_recommended_and_no_negative_cache_forever(rig, monkeypatch):
    client,sessions,users=rig
    headers,_,_=await pair(client,users[0])
    await job(client,users[0],recurring=True)
    task=await claim(client,headers)
    record=paper(title="Editorial Board",metadata_attempted=True)
    await upload(client,headers,task,[record],finished=True)
    async with sessions() as db:
        assert (await db.scalar(select(FeedItem))).visibility=="withdrawn"
    current=service.now()
    monkeypatch.setattr(service,"now",lambda:current+timedelta(hours=25))
    assert (await client.post("/cnki/worker/cache",headers=headers,json=[record])).json()["papers"]==[]


@pytest.mark.parametrize("change",[{"cookies":"never upload"},{"url":"https://example.com/detail"},{"url":"https://kns.cnki.net/download/file.pdf"}])
async def test_ingest_whitelist_rejects_credentials_and_non_index_urls(rig,change):
    client,_,users=rig
    headers,_,_=await pair(client,users[0])
    await job(client,users[0]); task=await claim(client,headers)
    response=await client.post(f"/cnki/worker/jobs/{task['id']}/batch",headers=headers,
        json={"lease":task["lease"],"page":1,"papers":[paper(**change)]})
    assert response.status_code==422


async def test_extension_download_is_static_and_cookie_permission_free(rig):
    client,_,_=rig
    response=await client.get("/cnki/extension.zip")
    assert response.status_code==200
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert len(archive.namelist())==8
        manifest=archive.read("sciencemate-cnki/manifest.json")
        assert b'"cookies"' not in manifest and b'"debugger"' not in manifest



async def test_lightweight_status_does_not_load_historical_job_papers(rig, monkeypatch):
    client, sessions, users = rig
    await pair(client, users[0]);await job(client,users[0])
    async def unexpected(*args,**kwargs):
        pytest.fail('lightweight status must not load old job records')
    monkeypatch.setattr(service,'job_view',unexpected)
    response=await client.get('/cnki/status?include_jobs=false',headers=owner(users[0]))
    assert response.status_code==200 and len(response.json()['devices'])==1
    assert response.json()['jobs']==[]


class ManagedBrowser:
    def __init__(self, *, challenge=False, unknown=False):
        self.challenge, self.unknown = challenge, unknown
        self.exports = 0
        self.page = 1
        self.owner = None
        self.searches = 0
        self.closes = 0

    async def ensure(self, user_id):
        self.owner = user_id

    async def check_session(self):
        pass

    def snapshot(self):
        start = (self.page-1)*20
        rows = [paper(title=f"中文研究 {i}", exportId=str(i)) for i in range(start,min(start+20,23))]
        return {"rows":[] if self.unknown else rows, "page":self.page,
                "hasNext":self.page==1, "total":23, "dateSorted":True}

    async def search(self, query, field, *, resume_verification=False):
        from app.services.cnki_browser import CollectionError
        if self.challenge:
            raise CollectionError("waiting_for_verification")
        self.searches += 1
        self.page = 1
        return self.snapshot()

    async def prepare_metadata(self):
        pass

    async def execute(self, action, args=None):
        if action=="next":
            self.page += 1
            return self.snapshot()
        assert action=="export"
        self.exports += 1
        return {"title":f"中文研究 {args['exportId']}", "abstract":"真实来源摘要占位", "metadata_attempted":True}

    async def close(self):
        self.closes += 1


async def drain_managed(worker):
    for _ in range(100):
        task = await worker.claim()
        if task is None:
            return
        await worker.process(*task)
    raise AssertionError("collector did not become idle")


async def managed_job(client, user, query="中文研究", limit=23):
    response = await client.post('/cnki/jobs',headers=owner(user),json={"query":query,"limit":limit})
    assert response.status_code==200
    assert response.json()["executor"]=="server"
    return response.json()


async def test_managed_queue_without_pairing_paginates_and_reuses_private_cache(rig):
    from app.services.cnki_worker import Collector
    client,sessions,users=rig
    browser=ManagedBrowser(); worker=Collector(sessions,browser)
    created=await managed_job(client,users[0])
    await worker.process(*(await worker.claim()))
    index=(await client.get('/cnki/jobs/'+created['id'],headers=owner(users[0]))).json()
    assert index['count']==23 and index['state']=='queued' and browser.exports==0
    await drain_managed(worker)
    result=(await client.get('/cnki/jobs/'+created['id'],headers=owner(users[0]))).json()
    assert result['state']=='completed' and result['count']==23 and browser.exports==23
    assert result['result']['search_mode']=='cnki_server_browser'
    assert result['result']['papers'][0]['metadata_provenance']['transport']=='server_browser'
    assert await worker.claim() is None
    # A different query returning the same indexes must reuse metadata.
    await managed_job(client,users[0],query="相关研究")
    await drain_managed(worker)
    assert browser.exports==23
    # Another user's cache is isolated even for identical paper identities.
    await managed_job(client,users[1])
    await drain_managed(worker)
    assert browser.exports==46 and browser.owner==users[1].id


async def test_metadata_403_stops_after_inflight_pair_and_does_not_cache_missing_abstracts(rig):
    from app.services.cnki_worker import Collector
    from app.services.cnki_browser import CollectionError
    client, sessions, users = rig
    class DeniedBrowser(ManagedBrowser):
        async def execute(self, action, args=None):
            if action == "export":
                self.exports += 1
                raise CollectionError("access_denied", endpoint="/dm8/API/GetExport", http_status=403)
            return await super().execute(action,args)
    browser = DeniedBrowser();worker = Collector(sessions,browser)
    created = await managed_job(client,users[0])
    await worker.process(*(await worker.claim()))
    await worker.process(*(await worker.claim()))
    result = (await client.get('/cnki/jobs/'+created['id'],headers=owner(users[0]))).json()
    assert result['state']=='paused' and result['count']==23
    assert browser.exports==2 and await worker.claim() is None
    async with sessions() as db:
        papers=(await db.scalars(select(CnkiPaper))).all()
        assert not any(p.metadata_json.get('metadata_attempted') for p in papers)


async def test_shared_browser_stops_other_lanes_after_a_metadata_denial(monkeypatch,tmp_path):
    from app.services import cnki_browser
    browser=cnki_browser.Browser(tmp_path);calls=[]
    first={'webSocketDebuggerUrl':'first'};second={'webSocketDebuggerUrl':'second'}
    async def command(ws,method,**kwargs):
        calls.append(ws)
        return {'result':{'value':{'error':'access_denied','endpoint':'/dm8/API/GetExport','httpStatus':403}}}
    monkeypatch.setattr(cnki_browser,'command',command)
    with pytest.raises(cnki_browser.CollectionError) as caught:
        await browser.execute('export',{'exportId':'one'},tab=first)
    assert caught.value.http_status==403 and caught.value.endpoint=='/dm8/API/GetExport'
    with pytest.raises(cnki_browser.CollectionError,match='session_paused'):
        await browser.execute('export',{'exportId':'two'},tab=second)
    with pytest.raises(cnki_browser.CollectionError,match='session_paused'):
        await browser.navigate(second,cnki_browser.SEARCH_URL)
    assert calls==['first']


async def test_search_navigation_and_exports_share_pacing_without_serializing_responses(monkeypatch, tmp_path):
    from app.services import cnki_browser
    monkeypatch.setattr(cnki_browser.settings, 'cnki_request_interval_seconds', .03)
    browser = cnki_browser.Browser(tmp_path)
    first, second = browser.lane(0), browser.lane(1)
    first.tab = {'webSocketDebuggerUrl': 'first'}
    second.tab = {'webSocketDebuggerUrl': 'second'}
    export_started, finish_export = asyncio.Event(), asyncio.Event()
    starts = []
    async def command(ws, method, **kwargs):
        expression = kwargs.get('expression', '')
        if method == 'Page.navigate':
            starts.append(('navigate', cnki_browser.time.monotonic()))
            return {}
        if expression == 'performance.timeOrigin':
            return {'result': {'value': 1}}
        if expression.startswith('({host:'):
            return {'result': {'value': {'host': 'www.cnki.net', 'path': '/', 'ready': 'complete', 'origin': 2}}}
        if 'cnkiPage("export",' in expression:
            starts.append(('export', cnki_browser.time.monotonic()))
            export_started.set()
            await finish_export.wait()
            return {'result': {'value': {'abstract': '摘要'}}}
        if 'cnkiPage("next",' in expression:
            return {'result': {'value': {'activation': {'x': 20, 'y': 30}}}}
        assert 'cnkiPage("wait",' in expression
        return {'result': {'value': {'page': 2, 'rows': [paper()]}}}
    async def activate(*args):
        starts.append(('next', cnki_browser.time.monotonic()))
    monkeypatch.setattr(cnki_browser, 'command', command)
    monkeypatch.setattr(cnki_browser, 'activate_control', activate)
    running = asyncio.create_task(first.execute('export', {'exportId': 'one'}))
    try:
        await asyncio.wait_for(export_started.wait(), 1)
        assert (await asyncio.wait_for(second.execute('next'), 1))['page'] == 2
        await asyncio.wait_for(browser.navigate(second.tab, cnki_browser.HOME_URL), 1)
        assert not running.done(), 'a slow export must not block the other category'
        assert [action for action, _ in starts] == ['export', 'next', 'navigate']
        assert all(right[1]-left[1] >= .025 for left, right in zip(starts, starts[1:]))
    finally:
        finish_export.set()
        await running


async def test_pending_request_stops_on_sibling_denial_and_state_reads_remain_available(monkeypatch, tmp_path):
    from app.services import cnki_browser
    monkeypatch.setattr(cnki_browser.settings, 'cnki_request_interval_seconds', .03)
    browser = cnki_browser.Browser(tmp_path)
    first = {'webSocketDebuggerUrl': 'first'}
    second = {'webSocketDebuggerUrl': 'second'}
    entered, deny = asyncio.Event(), asyncio.Event()
    calls = []
    async def command(ws, method, **kwargs):
        calls.append(ws)
        if 'cnkiPage("state",' in kwargs['expression']:
            return {'result': {'value': {'captcha': False}}}
        assert ws == 'first', 'queued export must not be dispatched after 403'
        entered.set()
        await deny.wait()
        return {'result': {'value': {'error': 'access_denied', 'httpStatus': 403}}}
    monkeypatch.setattr(cnki_browser, 'command', command)
    running = asyncio.create_task(browser.execute('export', tab=first))
    queued = None
    try:
        await asyncio.wait_for(entered.wait(), 1)
        queued = asyncio.create_task(browser.execute('export', tab=second))
        await asyncio.sleep(0)
        assert browser.request_lock.locked()
        deny.set()
        for task, code in ((running, 'access_denied'), (queued, 'session_paused')):
            with pytest.raises(cnki_browser.CollectionError, match=code):
                await asyncio.wait_for(task, 1)
        assert calls == ['first']
        assert not (await browser.execute('state', tab=second))['captcha']
        assert calls == ['first', 'second']
    finally:
        deny.set()
        for task in (running, queued):
            if task is not None and not task.done():
                task.cancel()
        await asyncio.gather(*(task for task in (running, queued) if task is not None), return_exceptions=True)


async def test_wrong_category_page_pauses_instead_of_scanning_until_exhaustion(rig):
    from app.services.cnki_worker import Collector
    client,sessions,users=rig
    created=(await client.post('/cnki/jobs',headers=owner(users[0]),json={
        'query':'图书主题','limit':200,'document_kind':'book'})).json()
    class WrongCategory(ManagedBrowser):
        async def search(self,query,field,**kwargs):return await super().search(query,field)
    browser=WrongCategory();worker=Collector(sessions,browser)
    await worker.process(*(await worker.claim()))
    result=(await client.get('/cnki/jobs/'+created['id'],headers=owner(users[0]))).json()
    assert result['state']=='paused' and result['count']==0 and result['page']==1
    assert browser.page==1 and await worker.claim() is None


async def test_managed_captcha_keeps_job_pending_and_retry_is_owner_scoped(rig):
    from app.services.cnki_worker import Collector
    client,sessions,users=rig
    browser=ManagedBrowser(challenge=True); worker=Collector(sessions,browser)
    created=await managed_job(client,users[0],limit=2)
    await worker.process(*(await worker.claim()))
    result=(await client.get('/cnki/jobs/'+created['id'],headers=owner(users[0]))).json()
    assert result['state']=='waiting_for_verification' and not result['last_completed_at']
    assert await worker.claim() is None  # No challenge retry storm.
    assert (await client.post('/cnki/jobs/'+created['id']+'/retry',headers=owner(users[1]))).status_code==404
    assert (await client.post('/cnki/jobs/'+created['id']+'/retry',headers=owner(users[0]))).status_code==200
    browser.challenge=False
    await drain_managed(worker)
    result=(await client.get('/cnki/jobs/'+created['id'],headers=owner(users[0]))).json()
    assert result['state']=='completed' and result['count']==2


async def test_unrecognized_page_is_error_not_zero_results(rig):
    from app.services.cnki_worker import Collector
    client,sessions,users=rig
    created=await managed_job(client,users[0])
    worker=Collector(sessions,ManagedBrowser(unknown=True))
    await worker.process(*(await worker.claim()))
    result=(await client.get('/cnki/jobs/'+created['id'],headers=owner(users[0]))).json()
    assert result['state']=='error' and result['last_completed_at'] is None


async def test_managed_and_extension_cannot_claim_each_others_jobs(rig):
    from app.services.cnki_worker import Collector
    client,sessions,users=rig
    local,_,_=await pair(client,users[0])
    created=await managed_job(client,users[0])
    assert await claim(client,local) is None
    worker=Collector(sessions,ManagedBrowser())
    device,task=await worker.claim()
    assert task['id']==created['id']
    await job(client,users[0],query="本机兼容任务")
    assert await claim(client,local) is None  # Active managed lease blocks overlap.
    await worker.process(device,task)
    await drain_managed(worker)
    assert await worker.claim() is None
    assert await claim(client,local) is not None


async def test_all_query_indexes_precede_exports_and_restart_resumes_metadata(rig):
    from app.services.cnki_worker import Collector
    client,sessions,users=rig
    browser=ManagedBrowser(); worker=Collector(sessions,browser)
    first=await managed_job(client,users[0],query="第一组",limit=2)
    second=await managed_job(client,users[0],query="第二组",limit=2)
    for expected in (first,second):
        device,task=await worker.claim()
        assert task['id']==expected['id'] and task['phase']=='index'
        indexed=(await client.get('/cnki/jobs/'+expected['id'],headers=owner(users[0]))).json()
        assert indexed['phase']=='index'
        await worker.process(device,task)
        indexed=(await client.get('/cnki/jobs/'+expected['id'],headers=owner(users[0]))).json()
        assert indexed['phase']=='metadata' and indexed['state']=='queued'
        # Progress survives an empty transient report after an incremental batch.
        async with sessions.begin() as db:
            stored=await db.get(CnkiJob,expected['id'])
            stored.last_report={}
        indexed=(await client.get('/cnki/jobs/'+expected['id'],headers=owner(users[0]))).json()
        assert indexed['phase']=='metadata' and not indexed['detail']
    assert browser.exports==0 and browser.searches==2
    # A new worker completes pending exports from durable indexes without
    # searching the two groups again. Export IDs stay out of public results.
    resumed=ManagedBrowser(); worker=Collector(sessions,resumed)
    await drain_managed(worker)
    assert resumed.searches==0 and resumed.exports==2
    for created in (first,second):
        result=(await client.get('/cnki/jobs/'+created['id'],headers=owner(users[0]))).json()
        assert result['state']=='completed' and result['count']==2
        assert all('export_id' not in p for p in result['result']['papers'])


async def test_wrong_export_keeps_original_index_and_does_not_restart_browser(rig):
    from app.services.cnki_worker import Collector
    client,sessions,users=rig
    class Mismatched(ManagedBrowser):
        async def execute(self, action, args=None):
            value=await super().execute(action,args)
            if action=='export' and args['exportId']=='0':
                value.update(title='完全不相干的题名',abstract='不能并入的摘要')
            return value
    browser=Mismatched(); worker=Collector(sessions,browser)
    created=await managed_job(client,users[0],limit=2)
    await drain_managed(worker)
    result=(await client.get('/cnki/jobs/'+created['id'],headers=owner(users[0]))).json()
    assert result['state']=='completed' and result['count']==2
    assert browser.closes==0 and browser.searches==1
    rows={p['title']:p for p in result['result']['papers']}
    assert not rows['中文研究 0']['abstract']
    assert rows['中文研究 0']['metadata_provenance']['metadata_error']=='metadata_title_mismatch'
    assert rows['中文研究 1']['abstract']


async def test_access_denied_pauses_without_restarting_or_auto_retry(rig):
    from app.services.cnki_worker import Collector
    from app.services.cnki_browser import CollectionError
    client,sessions,users=rig
    class Denied(ManagedBrowser):
        async def search(self, query, field, *, resume_verification=False):
            raise CollectionError('access_denied')
    browser=Denied(); worker=Collector(sessions,browser)
    created=await managed_job(client,users[0],limit=2)
    await drain_managed(worker)
    result=(await client.get('/cnki/jobs/'+created['id'],headers=owner(users[0]))).json()
    assert result['state']=='paused' and '403' in result['detail']
    assert result['result']['unavailable_sources'] == ['cnki']
    assert result['result']['warnings'][0]['reason'] == 'paused'
    assert result['result']['strategy_diagnostics']['cnki']['state'] == 'paused'
    assert browser.closes==0 and await worker.claim() is None


async def test_new_query_checks_old_denial_once_but_daily_jobs_and_polling_do_not(rig):
    from app.services.cnki_worker import Collector
    from app.services.cnki_browser import CollectionError
    client, sessions, users = rig
    class Paused(ManagedBrowser):
        denied = True
        checks = 0
        async def check_session(self):
            self.checks += 1
            self.denied = False
        async def search(self, query, field, **kwargs):
            if self.denied:
                raise CollectionError('session_paused')
            return await super().search(query, field, **kwargs)
    browser = Paused()
    worker = Collector(sessions, browser)
    daily = (await client.post('/cnki/jobs', headers=owner(users[0]), json={
        'query': '定时订阅', 'recurring': True})).json()
    await drain_managed(worker)
    paused = (await client.get('/cnki/jobs/'+daily['id'], headers=owner(users[0]))).json()
    assert paused['state'] == 'paused' and '当前请求未发送' in paused['detail']
    assert browser.checks == 0 and browser.denied
    assert await worker.claim() is None
    fresh = await managed_job(client, users[0], query='两天后主动提交的不同问题', limit=2)
    await drain_managed(worker)
    result = (await client.get('/cnki/jobs/'+fresh['id'], headers=owner(users[0]))).json()
    assert result['state'] == 'completed' and result['count'] == 2
    assert browser.checks == 1 and not browser.denied
    async with sessions() as db:
        assert (await db.get(CnkiJob, fresh['id'])).checkpoint['check_session'] is False
    assert (await client.get('/cnki/jobs/'+daily['id'], headers=owner(users[0]))).json()['state'] == 'paused'


async def test_session_pause_during_metadata_does_not_cache_a_missing_abstract(rig):
    from app.services.cnki_worker import Collector
    from app.services.cnki_browser import CollectionError
    client, sessions, users = rig
    class Paused(ManagedBrowser):
        async def execute(self, action, args=None):
            if action == 'export':
                raise CollectionError('session_paused')
            return await super().execute(action, args)
    created = await managed_job(client, users[0], limit=2)
    worker = Collector(sessions, Paused())
    await drain_managed(worker)
    result = (await client.get('/cnki/jobs/'+created['id'], headers=owner(users[0]))).json()
    assert result['state'] == 'paused' and result['count'] == 2
    async with sessions() as db:
        rows = (await db.scalars(select(CnkiPaper))).all()
        assert not any(row.metadata_json.get('metadata_attempted') for row in rows)


async def test_cancelled_worker_releases_lease_and_preserves_checkpoint(rig):
    from app.services.cnki_worker import Collector
    client,sessions,users=rig
    entered=asyncio.Event()
    class Slow(ManagedBrowser):
        async def search(self, query, field, *, resume_verification=False):
            entered.set()
            await asyncio.Event().wait()
    worker=Collector(sessions,Slow())
    created=await managed_job(client,users[0],limit=2)
    running=asyncio.create_task(worker.process(*(await worker.claim())))
    await entered.wait();running.cancel()
    with pytest.raises(asyncio.CancelledError):await running
    _,task=await worker.claim()
    assert task['id']==created['id'] and task['page']==1


async def test_browser_navigation_reports_http_403(monkeypatch,tmp_path):
    from app.services import cnki_browser
    replies=[{'result':{'value':1}}, {'errorText':'net::ERR_HTTP_RESPONSE_CODE_FAILURE'},
             {'result':{'value':{'host':'chromewebdata','error':'HTTP ERROR 403','ready':'complete','origin':2}}}]
    async def command(*args,**kwargs):return replies.pop(0)
    monkeypatch.setattr(cnki_browser,'command',command)
    with pytest.raises(cnki_browser.CollectionError,match='access_denied'):
        await cnki_browser.Browser(tmp_path).navigate({'webSocketDebuggerUrl':'unused'},cnki_browser.SEARCH_URL)


def test_thesis_subtitle_requires_same_author_and_year():
    from app.services.cnki_worker import matching_export
    row={'title':'生成式人工智能使用对基层公务员创新行为的影响研究——基于某市的实证',
         'year':2025,'authors':['测试作者']}
    value={'title':row['title'].split('——')[0],'year':2025,'authors':['测试作者']}
    assert matching_export(row,value)
    assert not matching_export(row,{**value,'authors':['另一作者']})
    assert not matching_export(row,{**value,'year':2024})
    assert not matching_export(row,{**value,'title':value['title'][:-4]})


async def test_subtitle_export_preserves_full_index_identity():
    from app.services.cnki_worker import Collector
    row=paper(title='生成式人工智能使用对基层公务员创新行为的影响研究——基于某市的实证')
    class Browser:
        async def execute(self,*args):
            return {**row,'title':row['title'].split('——')[0],'abstract':'完整研究摘要'}
    output=await Collector(browser=Browser()).metadata(row,None)
    assert output['title']==row['title'] and output['abstract']=='完整研究摘要'
    assert service.identity(output)==service.identity(row)


async def test_explicit_search_requeues_paused_job_without_losing_progress(rig):
    client, sessions, users = rig
    created = await managed_job(client, users[0], limit=2)
    async with sessions.begin() as db:
        job = await db.get(CnkiJob, created["id"])
        job.last_report = {"state": "paused", "detail": "HTTP 403"}
        job.checkpoint = {"executor": "server", "page": 3}
    # Reading a paused job must not trigger background retries.
    assert (await client.get('/cnki/jobs/'+created['id'], headers=owner(users[0]))).json()['state'] == 'paused'
    retried = await managed_job(client, users[0], limit=2)
    assert retried['id'] == created['id']
    assert retried['state'] == 'queued'
    async with sessions() as db:
        job = await db.get(CnkiJob, created['id'])
        assert job.checkpoint['page'] == 3
        assert job.checkpoint['recover_session'] is True


async def test_explicit_retry_recovers_session_once_and_preserves_indexes(rig):
    from app.services.cnki_worker import Collector
    from app.services.cnki_browser import CollectionError
    client, sessions, users = rig
    class Denied(ManagedBrowser):
        recoveries = 0
        async def recover_session(self):
            self.recoveries += 1
        async def search(self, query, field, *, resume_verification=False):
            raise CollectionError('access_denied')
    browser = Denied()
    worker = Collector(sessions, browser)
    created = await managed_job(client, users[0], limit=2)
    await drain_managed(worker)
    assert browser.recoveries == 0
    await client.post('/cnki/jobs/'+created['id']+'/retry', headers=owner(users[0]))
    await drain_managed(worker)
    result = (await client.get('/cnki/jobs/'+created['id'], headers=owner(users[0]))).json()
    assert browser.recoveries == 1 and result['state'] == 'paused'
    assert await worker.claim() is None
    async with sessions() as db:
        job = await db.get(CnkiJob, created['id'])
        assert job.checkpoint['recover_session'] is False


async def test_session_recovery_keeps_manual_challenge_in_place(monkeypatch, tmp_path):
    from app.services.cnki_browser import Browser, CollectionError
    browser = Browser(tmp_path)
    browser.tab = {'id': 'search'}
    calls = []
    async def execute(*args, **kwargs):
        return {'captcha': True}
    async def navigate(*args):
        calls.append(args)
    monkeypatch.setattr(browser, 'execute', execute)
    monkeypatch.setattr(browser, 'navigate', navigate)
    with pytest.raises(CollectionError, match='waiting_for_verification'):
        await browser.recover_session()
    assert not calls


@pytest.mark.parametrize('denied_again', [False, True])
async def test_category_retries_share_one_session_recovery(monkeypatch, tmp_path, denied_again):
    from app.services.cnki_browser import Browser, CollectionError
    browser = Browser(tmp_path)
    browser.tab = {'id': 'search'}
    browser.denied = True
    calls = []
    async def execute(*args, **kwargs):
        return {'captcha': False}
    async def navigate(*args):
        calls.append(args)
        await asyncio.sleep(0)
        if denied_again:
            browser.denied = True
            raise CollectionError('access_denied')
    monkeypatch.setattr(browser, 'execute', execute)
    monkeypatch.setattr(browser, 'navigate', navigate)
    outcomes = await asyncio.gather(*(browser.recover_session() for _ in range(5)), return_exceptions=True)
    assert len(calls) == 1
    assert browser.denied is denied_again
    if denied_again:
        assert all(isinstance(result, CollectionError) for result in outcomes)
        assert [str(result) for result in outcomes] == ['access_denied'] + ['session_paused'] * 4
    else:
        assert outcomes == [None] * 5


async def test_new_search_recovers_old_denial_but_not_a_fresh_sibling_403(monkeypatch, tmp_path):
    from app.services import cnki_browser
    browser = cnki_browser.Browser(tmp_path)
    browser.tab = {'webSocketDebuggerUrl': 'first'}
    calls = []
    async def command(*args, **kwargs):
        return {'result': {'value': {'error': 'access_denied', 'httpStatus': 403}}}
    async def execute(*args, **kwargs):
        return {'captcha': False}
    async def navigate(tab, url):
        calls.append(url)
    monkeypatch.setattr(cnki_browser, 'command', command)
    with pytest.raises(cnki_browser.CollectionError, match='access_denied'):
        await browser.execute('export')
    assert browser.denied_at is not None
    monkeypatch.setattr(browser, 'execute', execute)
    monkeypatch.setattr(browser, 'navigate', navigate)
    for _ in range(4):
        with pytest.raises(cnki_browser.CollectionError, match='session_paused'):
            await browser.check_session()
    assert calls == [] and browser.denied
    # Passage of time does not resume anything. A later explicit search checks once.
    browser.denied_at -= 2*24*60*60
    assert calls == [] and browser.denied
    await browser.check_session()
    assert calls == [cnki_browser.HOME_URL] and not browser.denied
    await browser.check_session()
    assert len(calls) == 1, 'healthy sessions must not be reloaded by each new query'


async def test_closed_denied_tab_can_be_reopened_without_network_until_explicit_retry(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from app.services.cnki_browser import Browser, HOME_URL
    browser = Browser(tmp_path)
    browser.user_id = str(uuid4())
    browser.process = SimpleNamespace(returncode=None)
    browser.tab = {'id': 'closed'}
    browser.denied = True
    calls = []
    class Client:
        async def get(self, path):
            return httpx.Response(200, json=[])
    browser.client = Client()
    async def new_tab():
        return {'id': 'replacement'}
    async def navigate(tab, url):
        calls.append((tab, url))
    async def execute(*args, **kwargs):
        return {'captcha': False}
    monkeypatch.setattr(browser, 'new_tab', new_tab)
    monkeypatch.setattr(browser, 'navigate', navigate)
    monkeypatch.setattr(browser, 'execute', execute)
    await browser.ensure(browser.user_id)
    assert browser.tab['id'] == 'replacement' and browser.denied and not calls
    await browser.recover_session()
    assert calls == [(browser.tab, HOME_URL)] and not browser.denied


async def test_closed_tab_recovers_without_restarting_verified_browser(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from app.services.cnki_browser import Browser, HOME_URL
    browser = Browser(tmp_path)
    browser.user_id = str(uuid4())
    browser.process = SimpleNamespace(returncode=None)
    browser.tab = {'id': 'closed'}
    browser.detail_tab = {'id': 'closed-detail'}
    calls = []
    class Client:
        async def get(self, path):
            return httpx.Response(200, json=[])
    browser.client = Client()
    async def new_tab():
        return {'id': 'replacement'}
    async def navigate(tab, url):
        calls.append((tab, url))
    monkeypatch.setattr(browser, 'new_tab', new_tab)
    monkeypatch.setattr(browser, 'navigate', navigate)
    await browser.ensure(browser.user_id)
    assert browser.tab['id'] == 'replacement' and browser.detail_tab is None
    assert calls == [(browser.tab, HOME_URL)]


async def test_browser_accepts_public_homepage_but_rejects_other_hosts(monkeypatch, tmp_path):
    from app.services import cnki_browser
    browser = cnki_browser.Browser(tmp_path)
    replies = [{'result': {'value': 1}}, {}, {'result': {'value': {
        'host': 'www.cnki.net', 'path': '/', 'ready': 'complete', 'origin': 2}}}]
    async def command(*args, **kwargs): return replies.pop(0)
    monkeypatch.setattr(cnki_browser, 'command', command)
    await browser.navigate({'webSocketDebuggerUrl': 'unused'}, cnki_browser.HOME_URL)
    for url in ['https://example.org/', 'https://www.cnki.net/arbitrary', 'http://www.cnki.net/']:
        with pytest.raises(cnki_browser.CollectionError, match='invalid_cnki_url'):
            await browser.navigate({'webSocketDebuggerUrl': 'unused'}, url)


async def test_verification_is_reported_without_focusing_browser(monkeypatch, tmp_path):
    from app.services import cnki_browser
    calls = []
    async def command(ws, method, **params):
        calls.append(method)
        return {"result": {"value": {"error": "waiting_for_verification"}}}
    monkeypatch.setattr(cnki_browser, 'command', command)
    browser = cnki_browser.Browser(tmp_path)
    browser.tab = {"webSocketDebuggerUrl": "unused"}
    with pytest.raises(cnki_browser.CollectionError, match="waiting_for_verification"):
        await browser.execute("search", {"query": "test"})
    assert calls == ["Runtime.evaluate"]

async def test_new_index_job_precedes_due_failed_retry(rig):
    client, sessions, users = rig
    old = await managed_job(client, users[0], query='旧任务失败重试')
    async with sessions.begin() as db:
        job = await db.get(CnkiJob, old['id'])
        job.last_report = {'state': 'error', 'detail': 'timeout'}
        job.last_report_at = service.now() - timedelta(minutes=6)
    fresh = await managed_job(client, users[0], query='当前新检索')
    from app.services.cnki_worker import Collector
    worker = Collector(sessions, ManagedBrowser())
    device, task = await worker.claim()
    assert task['id'] == fresh['id']
    # Active leases still prevent simultaneous queries against the same tab.
    assert await worker.claim() is None
    await worker.process(device, task)
    device, task = await worker.claim()
    assert task['id'] == old['id']

async def test_verified_results_are_read_without_submitting_again(monkeypatch, tmp_path):
    from app.services import cnki_browser
    browser = cnki_browser.Browser(tmp_path)
    browser.tab = {'webSocketDebuggerUrl': 'unused'}
    calls = []
    state = {'query': '原检索词', 'field': 'SU', 'rows': [paper()], 'page': 1, 'total': 1}
    async def execute(action, *args, **kwargs):
        calls.append(action)
        assert action == 'state', 'verification recovery must not resubmit'
        return state
    monkeypatch.setattr(browser, 'execute', execute)
    assert await browser.search('原检索词', 'SU', resume_verification=True) == state
    assert calls == ['state', 'state']


async def test_resume_never_imports_results_for_a_different_query(monkeypatch, tmp_path):
    from app.services import cnki_browser
    browser = cnki_browser.Browser(tmp_path)
    browser.tab = {'webSocketDebuggerUrl': 'unused'}
    calls = []
    async def execute(action, *args, **kwargs):
        calls.append(action)
        return {'query': 'other', 'field': 'SU', 'rows': [paper()]} if action == 'state' else {'rows': []}
    async def command(*args, **kwargs):
        return {'result': {'value': {'host': 'kns.cnki.net', 'ready': True}}}
    monkeypatch.setattr(browser, 'execute', execute)
    monkeypatch.setattr(cnki_browser, 'command', command)
    result = await browser.search('original', 'SU', resume_verification=True)
    assert result['rows'] == [] and calls[-1] == 'search'


async def test_complete_total_library_result_proves_empty_categories_without_querying_them(monkeypatch, tmp_path):
    from app.services import cnki_browser
    browser=cnki_browser.Browser(tmp_path);browser.tab={'webSocketDebuggerUrl':'unused'}
    total={'query':'LBM','field':'SU','resource':'CROSSDB','page':1,'total':2,'hasNext':False,
           'rows':[paper(title='期刊研究',document_type='期刊'),paper(title='学位研究',document_type='硕士')]}
    calls=[]
    async def execute(action,*args,**kwargs):
        calls.append(action)
        assert action in {'state','search'}, 'known empty categories must not issue resource clicks'
        return {'resource':'CROSSDB'} if action=='state' else total
    async def command(*args,**kwargs):
        return {'result':{'value':{'host':'kns.cnki.net','ready':True}}}
    monkeypatch.setattr(browser,'execute',execute);monkeypatch.setattr(cnki_browser,'command',command)
    for kind in ('conference','book','other'):
        result=await browser.search('LBM','SU',document_kind=kind)
        assert result['emptyConfirmed'] and result['rows']==[] and result['total']==0
        assert result['emptyEvidence']=='complete_total_library_result'
    assert calls.count('search')==1
    assert browser.empty_category('other query','SU','book') is None
    browser.complete_result=(browser.complete_result[0],browser.complete_result[1]-61,total)
    assert browser.empty_category('LBM','SU','book') is None


@pytest.mark.parametrize('total,has_next',[(20,True),(20,False)])
async def test_partial_total_library_page_cannot_prove_a_category_empty(monkeypatch,tmp_path,total,has_next):
    from app.services import cnki_browser
    browser=cnki_browser.Browser(tmp_path);browser.tab={'webSocketDebuggerUrl':'unused'}
    calls=[]
    async def execute(action,args=None,**kwargs):
        calls.append(action)
        if action=='state':return {'resource':'CROSSDB'}
        if action=='search':return {'query':'LBM','field':'SU','resource':'CROSSDB','rows':[paper()], 'total':total,'hasNext':has_next}
        assert action=='resource'
        raise cnki_browser.CollectionError('results_unavailable')
    async def command(*args,**kwargs):return {'result':{'value':{'host':'kns.cnki.net','ready':True}}}
    monkeypatch.setattr(browser,'execute',execute);monkeypatch.setattr(cnki_browser,'command',command)
    with pytest.raises(cnki_browser.CollectionError,match='results_unavailable'):
        await browser.search('LBM','SU',document_kind='book')
    assert calls[-1]=='resource' and browser.complete_result is None


async def test_verification_resume_marker_survives_queue_retry(rig):
    from app.services.cnki_worker import Collector
    client, sessions, users = rig
    await managed_job(client, users[0])
    worker = Collector(sessions, ManagedBrowser(challenge=True))
    await worker.process(*(await worker.claim()))
    async with sessions.begin() as db:
        job = (await db.scalars(select(CnkiJob))).one()
        assert job.checkpoint['resume_verification'] is True
        job.last_report_at = service.now() - timedelta(minutes=1)
    _, task = await worker.claim()
    assert task['resume_verification'] is True


async def test_native_control_is_confined_to_tab_protocol(monkeypatch):
    import json
    from app.services import cnki_browser
    class Socket:
        def __init__(self):
            self.messages = asyncio.Queue()
            self.sent = []
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def send(self, message):
            value = json.loads(message)
            self.sent.append(value)
            await self.messages.put(json.dumps({'id': value['id'], 'result': {}}))
        async def recv(self): return await self.messages.get()
    socket = Socket()
    monkeypatch.setattr(cnki_browser.websockets, 'connect', lambda *args, **kwargs: socket)
    await cnki_browser.activate_control('ws://127.0.0.1:12345/page', {'x': 25, 'y': 35})
    assert [m['method'] for m in socket.sent] == ['Input.dispatchMouseEvent'] * 3
    assert [m['params']['type'] for m in socket.sent] == ['mouseMoved', 'mousePressed', 'mouseReleased']
    assert all(m['params']['x'] == 25 and m['params']['y'] == 35 for m in socket.sent)


@pytest.mark.parametrize('point', [{'x': float('nan'), 'y': 1}, {'x': -1, 'y': 1}])
async def test_invalid_control_coordinates_never_reach_browser(point, monkeypatch):
    from app.services import cnki_browser
    def forbidden(*args, **kwargs): raise AssertionError('invalid control must not connect')
    monkeypatch.setattr(cnki_browser.websockets, 'connect', forbidden)
    with pytest.raises(cnki_browser.CollectionError, match='browser_activation_failed'):
        await cnki_browser.activate_control('ws://127.0.0.1:12345/page', point)


async def test_native_action_runs_after_preparation_before_result_wait(monkeypatch, tmp_path):
    from app.services import cnki_browser
    browser = cnki_browser.Browser(tmp_path)
    browser.tab = {'webSocketDebuggerUrl': 'ws://127.0.0.1:12345/page'}
    calls = []
    async def page(action, args=None, **kwargs):
        calls.append(action)
        if action == 'search':
            assert args['prepareOnly'] is True
            return {'activation': {'x': 20, 'y': 30}}
        assert action == 'wait'
        return {'rows': [paper()], 'dateSorted': True}
    async def activate(*args): calls.append('activate')
    monkeypatch.setattr(browser, '_page', page)
    monkeypatch.setattr(cnki_browser, 'activate_control', activate)
    result = await browser.execute('search', {'query': 'LBM', 'field': 'SU'})
    assert result['rows']
    assert calls == ['search', 'activate', 'wait']


async def test_displaced_control_reacquires_coordinates_before_retry(monkeypatch, tmp_path):
    from app.services import cnki_browser
    browser = cnki_browser.Browser(tmp_path)
    browser.tab = {'webSocketDebuggerUrl': 'ws://127.0.0.1:12345/page'}
    prepared, positions, calls = [], [], []
    async def page(action, args=None, **kwargs):
        calls.append(action)
        if action == 'next':
            prepared.append(1)
            return {'activation': {'x': 20 * len(prepared), 'y': 30}}
        if action == 'wait':
            if len(prepared) == 1:
                raise cnki_browser.CollectionError('browser_activation_moved')
            return {'page': 2, 'rows': [paper()]}
        assert action == 'cancel'
        return {}
    async def activate(_, point): positions.append(point['x'])
    monkeypatch.setattr(browser, '_page', page)
    monkeypatch.setattr(cnki_browser, 'activate_control', activate)
    assert (await browser.execute('next'))['page'] == 2
    assert positions == [20, 40]
    assert calls == ['next', 'wait', 'cancel', 'next', 'wait']


@pytest.mark.parametrize('error,attempts', [
    ('browser_activation_moved', 3), ('cnki_timeout', 1),
    ('waiting_for_verification', 1), ('access_denied', 1), ('results_unavailable', 1),
])
async def test_control_retry_is_bounded_and_never_replays_submitted_queries(error, attempts, monkeypatch, tmp_path):
    from app.services import cnki_browser
    browser = cnki_browser.Browser(tmp_path)
    browser.tab = {'webSocketDebuggerUrl': 'ws://127.0.0.1:12345/page'}
    prepared, cancelled, activated = [], [], []
    async def page(action, args=None, **kwargs):
        if action == 'search':
            prepared.append(1)
            return {'activation': {'x': 20, 'y': 30}}
        if action == 'wait': raise cnki_browser.CollectionError(error)
        assert action == 'cancel'
        cancelled.append(1)
        return {}
    async def activate(*args): activated.append(1)
    monkeypatch.setattr(browser, '_page', page)
    monkeypatch.setattr(cnki_browser, 'activate_control', activate)
    with pytest.raises(cnki_browser.CollectionError, match=error):
        await browser.execute('search', {'query': 'LBM'})
    assert len(prepared) == len(activated) == len(cancelled) == attempts


async def test_unlimited_collection_crosses_200_and_completed_pages_are_all_readable(rig):
    from app.services.cnki_worker import Collector
    client,sessions,users=rig
    class LargeBrowser(ManagedBrowser):
        def snapshot(self):
            start=(self.page-1)*20
            return {'page':self.page,'total':245,'hasNext':self.page<13,'dateSorted':True,
                    'rows':[paper(title=f'中文研究 {i}',exportId=str(i)) for i in range(start,min(start+20,245))]}
    browser=LargeBrowser();worker=Collector(sessions,browser)
    created=await managed_job(client,users[0],limit=None)
    task=await worker.claim();assert task[1]['limit'] is None
    await worker.process(*task)
    assert browser.page==13
    # The index is available without waiting for all abstracts.
    full=(await client.get('/cnki/jobs/'+created['id'],headers=owner(users[0]))).json()
    assert full['count']==245 and len(full['result']['papers'])==245
    cursor=None;ids=set();sizes=[]
    while True:
        params={'page_size':70}
        if cursor:params['cursor']=cursor
        response=await client.get('/cnki/jobs/'+created['id'],headers=owner(users[0]),params=params)
        assert response.status_code==200,response.text
        data=response.json();sizes.append(len(data['paper_ids']))
        assert not ids.intersection(data['paper_ids']);ids.update(data['paper_ids'])
        cursor=data['next_cursor']
        if not data['has_more']:break
    assert sizes==[70,70,70,35] and len(ids)==245
    unchanged=(await client.get('/cnki/jobs/'+created['id'],headers=owner(users[0]),params={'page_size':70,'cursor':cursor})).json()
    assert unchanged['result']['papers']==[] and unchanged['count']==245
    assert (await client.get('/cnki/jobs/'+created['id'],headers=owner(users[1]),params={'page_size':70})).status_code==404


async def test_incremental_cursor_delivers_updates_after_a_snapshot_without_duplication(rig):
    client,_,users=rig
    headers,_,_=await pair(client,users[0]);created=await job(client,users[0]);task=await claim(client,headers)
    rows=[paper(title=f'研究{i}') for i in range(3)]
    await upload(client,headers,task,rows,total=3)
    first=(await client.get('/cnki/jobs/'+created['id'],headers=owner(users[0]),params={'page_size':2})).json()
    changed=next(r for r in rows if r['title']==first['result']['papers'][0]['title'])
    await upload(client,headers,task,[{**changed,'abstract':'新增的摘要','metadata_attempted':True}],total=3)
    second=(await client.get('/cnki/jobs/'+created['id'],headers=owner(users[0]),params={'page_size':2,'cursor':first['next_cursor']})).json()
    assert len(second['paper_ids'])==1 and not second['has_more']
    update=(await client.get('/cnki/jobs/'+created['id'],headers=owner(users[0]),params={'page_size':2,'cursor':second['next_cursor']})).json()
    assert update['paper_ids']==[first['paper_ids'][0]]
    assert update['result']['papers'][0]['abstract']=='新增的摘要'
    assert (await client.get('/cnki/jobs/'+created['id'],headers=owner(users[0]),params={'page_size':2,'cursor':'not-a-cursor'})).status_code==422


async def test_unlimited_request_never_reuses_a_capped_completed_job(rig):
    client,sessions,users=rig
    old=await managed_job(client,users[0],limit=200)
    async with sessions.begin() as db:
        stored=await db.get(CnkiJob,old['id']);stored.last_completed_at=service.now()
        stored.checkpoint={**stored.checkpoint,'finished':True,'page':10}
    response=await client.post('/cnki/jobs?page_size=200',headers=owner(users[0]),json={'query':'中文研究'})
    assert response.status_code==200,response.text
    new=response.json();assert new['id']!=old['id'] and new['state']=='queued'
    async with sessions() as db:
        assert (await db.get(CnkiJob,new['id'])).result_limit is None
        assert (await db.get(CnkiJob,old['id'])).result_limit==200


async def test_categories_do_not_reuse_mixed_cache_or_each_others_jobs(rig):
    client,_,users=rig
    mixed=await managed_job(client,users[0],limit=200)
    jobs=[]
    for kind in ('journal','thesis','journal'):
        reply=await client.post('/cnki/jobs',headers=owner(users[0]),json={
            'query':'中文研究','limit':200,'document_kind':kind})
        assert reply.status_code==200,reply.text
        jobs.append(reply.json())
    assert jobs[0]['id']==jobs[2]['id']
    assert len({mixed['id'],jobs[0]['id'],jobs[1]['id']})==3
    assert [j['document_kind'] for j in jobs]==['journal','thesis','journal']


async def test_server_can_lease_two_categories_but_never_a_third_or_another_device(rig):
    from app.services.cnki_worker import Collector
    client,sessions,users=rig
    for kind in ('journal','thesis','book'):
        assert (await client.post('/cnki/jobs',headers=owner(users[0]),json={
            'query':'中文研究','limit':200,'document_kind':kind})).status_code==200
    worker=Collector(sessions,ManagedBrowser())
    first=await worker.claim(server_slots=2)
    second=await worker.claim(users[0].id,server_slots=2)
    assert {first[1]['document_kind'],second[1]['document_kind']}=={'journal','thesis'}
    assert await worker.claim(users[0].id,server_slots=2) is None
    headers,_,_=await pair(client,users[0])
    assert (await client.post('/cnki/worker/poll',headers=headers,json={'state':'idle'})).json()['job'] is None


async def test_category_quota_counts_unique_matching_indexes_and_keeps_metadata_updates(rig):
    from app.services.cnki_worker import Collector
    client,sessions,users=rig
    created=(await client.post('/cnki/jobs',headers=owner(users[0]),json={
        'query':'中文研究','limit':200,'document_kind':'thesis'})).json()
    worker=Collector(sessions,ManagedBrowser())
    device,task=await worker.claim()
    # Journal records and duplicates cannot use a thesis slot.
    for n in range(12):
        rows=[paper(title=f'学位研究 {i}',document_type='硕士') for i in range(n*20,(n+1)*20)]
        await worker.batch(device,task,papers=rows,total=500,page_complete=True)
        task={**task,'page':task['page']+1}
    await worker.batch(device,task,papers=[paper(title='学位研究 0',document_type='硕士',abstract='补齐摘要',metadata_attempted=True),paper(title='期刊干扰',document_type='期刊')],total=500)
    response=(await client.get('/cnki/jobs/'+created['id'],headers=owner(users[0]))).json()
    assert response['count']==len(response['result']['papers'])==200
    assert {p['publication_category'] for p in response['result']['papers']}=={'学位论文'}
    assert next(p for p in response['result']['papers'] if p['title']=='学位研究 0')['abstract']=='补齐摘要'


async def test_collector_runs_two_lanes_together_and_cleans_up_on_stop(rig,monkeypatch,tmp_path):
    from types import SimpleNamespace
    from app.services.cnki_worker import Collector
    from app.services import project_repository
    client,sessions,users=rig
    for kind in ('journal','thesis','book'):
        await client.post('/cnki/jobs',headers=owner(users[0]),json={'query':'中文研究','limit':200,'document_kind':kind})
    started=set();both=asyncio.Event();closed=[]
    class Lane(ManagedBrowser):
        async def search(self,query,field,**kwargs):
            started.add(kwargs['document_kind'])
            if len(started)==2:both.set()
            await asyncio.Event().wait()
    class Root:
        def lane(self,slot):return Lane()
        async def close(self):closed.append(True)
    monkeypatch.setattr(project_repository,'_the_file_lock',lambda:SimpleNamespace(try_hold=lambda path:SimpleNamespace(close=lambda:None)))
    worker=Collector(sessions,Root());worker.task=asyncio.create_task(worker.run())
    try:
        await asyncio.wait_for(both.wait(),3)
        assert started=={'journal','thesis'}
    finally:
        await worker.stop()
    assert closed==[True]

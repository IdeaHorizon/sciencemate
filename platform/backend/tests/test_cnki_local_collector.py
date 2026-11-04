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

    def snapshot(self):
        start = (self.page-1)*20
        rows = [paper(title=f"中文研究 {i}", exportId=str(i)) for i in range(start,min(start+20,23))]
        return {"rows":[] if self.unknown else rows, "page":self.page,
                "hasNext":self.page==1, "total":23, "dateSorted":True}

    async def search(self, query, field):
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
        await worker.process(device,task)
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
        async def search(self, query, field):
            raise CollectionError('access_denied')
    browser=Denied(); worker=Collector(sessions,browser)
    created=await managed_job(client,users[0],limit=2)
    await drain_managed(worker)
    result=(await client.get('/cnki/jobs/'+created['id'],headers=owner(users[0]))).json()
    assert result['state']=='paused' and '403' in result['detail']
    assert browser.closes==0 and await worker.claim() is None


async def test_cancelled_worker_releases_lease_and_preserves_checkpoint(rig):
    from app.services.cnki_worker import Collector
    client,sessions,users=rig
    entered=asyncio.Event()
    class Slow(ManagedBrowser):
        async def search(self, query, field):
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

"""端到端：三个知识接口返回的必须是 harness 库里的真实记录。

单元测试锁的是适配器读得对（`test_ui_reads_the_kb_agents_write.py`）；这里锁的是
**接口真的换过去了** —— 否则适配器写完了、端点还在查空表，两边各自绿。

覆盖前端实际调用的全部三个：`/knowledge/search`、`/memory/entries`、`/kb/concepts`。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.auth import get_current_user
from app.config import settings
from app.database import get_db
from app.main import app
from app.models.user import User

# DB 侧 project_id 是 UUID 列，harness 侧是目录名 —— 两边必须同值，
# 所以统一用一个合法 UUID。
_PROJECT = "11111111-1111-4111-8111-111111111111"


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in records),
                    encoding="utf-8")


@pytest_asyncio.fixture
async def kb_client(db_session, tmp_path, monkeypatch):
    """真 harness 布局 + 打过认证桩的 client。"""
    user = User(id="u-kb-1", email="kb@example.com", display_name="KB Tester",
                hashed_password="x")
    state_root = tmp_path / "harness_runtime"
    home = state_root / "users" / user.id
    _write_jsonl(home / "projects" / _PROJECT / "kb_concepts.jsonl", [
        {"id": "concept_ka", "canonical_name": "KA 模型", "concept_type": "method",
         "description": "二元 Lennard-Jones 混合体系", "aliases": ["Kob-Andersen"],
         "sources": ["doi:10.1000/a", "doi:10.1000/b"], "claim_ids": ["claim_tg"]},
    ])
    _write_jsonl(home / "projects" / _PROJECT / "kb_claims.jsonl", [
        {"id": "claim_tg", "claim_text": "冷却速率降低一个量级，Tg 下降约 2%",
         "claim_type": "empirical", "confidence": 0.8, "status": "supported",
         "concept_ids": ["concept_ka"], "created_at": "2026-08-05T00:00:00Z"},
        {"id": "syn_1", "claim_text": "低温段方法学综述\n三份实验共同支持…",
         "claim_type": "synthesis", "confidence": 0.6,
         "created_at": "2026-08-08T00:00:00Z"},
    ])
    _write_jsonl(home / "projects" / _PROJECT / "kb_proposals.jsonl", [
        {"id": "prop_1", "content": "把 KA 模型升到 org 层", "status": "pending",
         "created_at": "2026-08-09T00:00:00Z"},
        {"id": "prop_done", "content": "已处理的", "status": "accepted",
         "created_at": "2026-08-02T00:00:00Z"},
    ])
    _write_jsonl(home / "projects" / _PROJECT / "memory.jsonl", [
        {"id": "m_old", "content": "早一条", "category": "observation",
         "created_at": "2026-08-01T00:00:00Z"},
        {"id": "m_new", "content": "低温段要用更长弛豫", "category": "pitfall",
         "created_at": "2026-08-09T00:00:00Z"},
        {"id": "m_odd", "content": "分类是新词", "category": "brand_new_category",
         "created_at": "2026-07-01T00:00:00Z"},
    ])
    monkeypatch.setattr(settings, "harness_state_root", str(state_root))

    # 桥是子进程，单测里不起它：把 harness_kb.query 打成直接读同一批 fixture 文件。
    # 端到端「桥真的能答」由 tests/test_kb_bridge_query.py 用真进程验。
    from app.services import harness_kb

    async def _fake_query(_user, project_id, entity, *, search="", limit=50, offset=0):
        base = home / "projects" / project_id
        name = {"memory": "memory.jsonl", "proposals": "kb_proposals.jsonl"}.get(
            entity, f"kb_{entity}.jsonl")
        records = []
        for path in (base / name, home / "org" / name):
            if not path.is_file():
                continue
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    records.append(json.loads(line))
        if entity == "memory":
            records.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
        if entity == "proposals":
            records = [r for r in records if r.get("status") == "pending"]
        if search:
            needle = search.lower()
            records = [r for r in records
                       if needle in json.dumps(r, ensure_ascii=False).lower()]
        return records[offset:offset + limit]

    monkeypatch.setattr(harness_kb, "query", _fake_query)

    async def override_get_db():
        yield db_session

    async def override_current_user() -> User:
        return user

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_current_user
    try:
        async with AsyncClient(transport=ASGITransport(app=app),
                                base_url="http://kb-test") as client:
            yield client
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_knowledge_search_returns_harness_records(kb_client):
    """事故本体：以前这里查的是空表，永远返回 []。"""
    resp = await kb_client.post("/api/v1/knowledge/search",
                                 json={"query": "冷却速率", "project_id": _PROJECT})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body, "搜不到 —— 端点还在读那张没人写的表"
    assert any("Tg 下降" in item["chunk"]["text"] for item in body)


@pytest.mark.asyncio
async def test_knowledge_search_misses_are_empty_not_errors(kb_client):
    resp = await kb_client.post("/api/v1/knowledge/search",
                                 json={"query": "完全不相干", "project_id": _PROJECT})
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_memory_entries_return_harness_memory_newest_first(kb_client):
    resp = await kb_client.get(f"/api/v1/memory/entries?project_id={_PROJECT}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [e["id"] for e in body] == ["m_new", "m_old", "m_odd"]
    assert body[0]["content"] == "低温段要用更长弛豫"


@pytest.mark.asyncio
async def test_memory_category_maps_without_losing_the_original(kb_client):
    """两套分类词表是各自演化出来的 —— 映射不能有损。"""
    body = (await kb_client.get(f"/api/v1/memory/entries?project_id={_PROJECT}")).json()
    by_id = {e["id"]: e for e in body}
    assert by_id["m_new"]["type"] == "judgmental"      # pitfall = 从经验得出的结论
    assert by_id["m_old"]["type"] == "factual"         # observation = 观察到的事实
    # 原值原样留在 source 里，UI 上的分类永远查得回 agent 写的是什么
    assert by_id["m_new"]["source"]["harness_category"] == "pitfall"


@pytest.mark.asyncio
async def test_unknown_category_does_not_become_a_judgement(kb_client):
    """认不出的分类落 factual —— 猜成 judgmental 等于替 agent 下判断。"""
    body = (await kb_client.get(f"/api/v1/memory/entries?project_id={_PROJECT}")).json()
    odd = next(e for e in body if e["id"] == "m_odd")
    assert odd["type"] == "factual"
    assert odd["source"]["harness_category"] == "brand_new_category"


@pytest.mark.asyncio
async def test_concepts_come_from_harness_with_real_counts(kb_client):
    resp = await kb_client.get(f"/api/v1/kb/concepts?project_id={_PROJECT}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body) == 1
    concept = body[0]
    assert concept["canonical_name"] == "KA 模型"
    assert concept["aliases"] == ["Kob-Andersen"]
    # 计数来自真实记录，不是那张空表里的 0
    assert concept["source_count"] == 2
    assert concept["claim_count"] == 1


@pytest.mark.asyncio
async def test_concept_query_filters_on_real_fields(kb_client):
    hit = await kb_client.get(f"/api/v1/kb/concepts?project_id={_PROJECT}&query=Kob-Andersen")
    assert [c["id"] for c in hit.json()] == ["concept_ka"], "别名没被搜到"
    miss = await kb_client.get(f"/api/v1/kb/concepts?project_id={_PROJECT}&query=不存在")
    assert miss.json() == []


@pytest.mark.asyncio
async def test_claims_come_from_harness(kb_client):
    body = (await kb_client.get(f"/api/v1/kb/claims?project_id={_PROJECT}")).json()
    assert [c["id"] for c in body] == ["claim_tg"], "synthesis 不该混进 claim 列表"
    assert body[0]["is_verified"] is True          # status=supported
    assert body[0]["confidence"] == "high"         # 0.8 → 分桶，不是 "0.8"


@pytest.mark.asyncio
async def test_syntheses_are_claims_with_that_type(kb_client):
    """harness 把 synthesis 折进 claims 用 claim_type 区分 —— 不是另一个库。"""
    body = (await kb_client.get(f"/api/v1/kb/syntheses?project_id={_PROJECT}")).json()
    assert [s["id"] for s in body] == ["syn_1"]
    assert body[0]["title"] == "低温段方法学综述"   # 正文首行，不编标题


@pytest.mark.asyncio
async def test_proposals_come_from_the_curator_queue(kb_client):
    body = (await kb_client.get(f"/api/v1/kb/proposals?project_id={_PROJECT}")).json()
    assert [p["id"] for p in body] == ["prop_1"], "默认只看 pending"


@pytest.mark.asyncio
async def test_concept_claims_link_through(kb_client):
    body = (await kb_client.get(
        f"/api/v1/kb/concepts/concept_ka/claims?project_id={_PROJECT}")).json()
    assert [c["id"] for c in body] == ["claim_tg"]


@pytest.mark.asyncio
async def test_fake_write_buttons_are_gone(kb_client):
    """写空表的那几个假按钮已下线。

    保留的是 proposal 的批准/驳回 —— 它们现在走桥调 harness 的 resolve_proposal，
    有真实副作用和 reasoning 审计。其余几个当初写的是没人读的表，删掉而不是
    重新指向 harness：claim 状态在那边由机械权威规则管（update_claim_status 要
    research_state 背书），从这里开侧门等于绕过它们。
    """
    for url in (
        "/api/v1/kb/claims/claim_tg/verify",
        "/api/v1/kb/syntheses/syn_1/review",
        "/api/v1/memory/entries",
    ):
        resp = await kb_client.post(url, json={})
        assert resp.status_code in (404, 405), f"{url} 还活着 → {resp.status_code}"


@pytest.mark.asyncio
async def test_proposal_actions_go_through_the_harness(kb_client, monkeypatch):
    """批准/驳回必须落到 harness 的 resolve_proposal，不是本地翻状态。"""
    from app.services import harness_kb

    seen = {}

    async def _fake_resolve(_user, project_id, proposal_id, *, decision, reasoning):
        seen.update(project_id=project_id, proposal_id=proposal_id,
                    decision=decision, reasoning=reasoning)
        return {"proposal_id": proposal_id, "decision": decision}

    monkeypatch.setattr(harness_kb, "resolve_proposal", _fake_resolve)
    resp = await kb_client.post(
        f"/api/v1/kb/proposals/prop_1/approve?project_id={_PROJECT}"
        "&reasoning=确认该概念跨项目复用")
    assert resp.status_code == 200, resp.text
    assert seen["decision"] == "accepted"
    assert seen["proposal_id"] == "prop_1"
    assert len(seen["reasoning"]) >= 5


@pytest.mark.asyncio
async def test_reasoning_is_required_for_a_decision(kb_client):
    """理由是队列可审计的凭据 —— 缺了要当场拒，而不是记一条没来由的决定。"""
    resp = await kb_client.post(
        f"/api/v1/kb/proposals/prop_1/approve?project_id={_PROJECT}&reasoning=x")
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_project_without_sediment_is_empty_not_500(kb_client):
    """还没沉淀过的项目：空视图，不是报错。"""
    for url in ("/api/v1/kb/concepts?project_id=22222222-2222-4222-8222-222222222222",
                "/api/v1/memory/entries?project_id=22222222-2222-4222-8222-222222222222"):
        resp = await kb_client.get(url)
        assert resp.status_code == 200, f"{url} → {resp.status_code} {resp.text}"
        assert resp.json() == []

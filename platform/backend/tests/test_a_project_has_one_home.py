"""项目层一个项目一份（`docs/RFC_PROJECT_HOME_20260924.md`）—— 平台这一侧：说出来、合过来。

- 平台**告诉**每个会话和每一问项目层在哪（`config.the_projects_home`），不让 worker 从 home 推。
- 升级时把按人分存的旧项目层（`state/users/<uid>/projects/<项目>`）合成一份：抄不搬、按 id 并、
  作业行记上是谁、历史 run 硬链接、幂等。旧目录一个字节都不动。
"""
from __future__ import annotations

import ast
import json
import os
import uuid
from pathlib import Path

import pytest

from app.services.one_home_per_project import merge_the_per_person_copies

ALICE = uuid.UUID("11111111-2222-3333-4444-555555555555")
BOB = uuid.UUID("66666666-7777-8888-9999-000000000000")
PROJECT = uuid.UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")


def _jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records), encoding="utf-8")


def _read(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _two_people_one_project(state: Path) -> tuple[Path, Path]:
    """同一个项目在两个人的 home 里各一份 —— 连拼法都不一样（带连字符 / 32 位 hex）。"""
    alice = state / "users" / str(ALICE) / "projects" / str(PROJECT)
    bob = state / "users" / BOB.hex / "projects" / PROJECT.hex
    shared = {"id": "claim_same", "claim_text": "两人都写下的一句", "updated_at": "2026-09-20T00:00:00Z"}
    _jsonl(alice / "kb_claims.jsonl", [
        shared,
        {"id": "claim_alice", "claim_text": "只有 A 知道", "updated_at": "2026-09-21T00:00:00Z"},
        {"id": "claim_touched", "claim_text": "被改过的", "status": "open", "updated_at": "2026-09-21T00:00:00Z"},
    ])
    _jsonl(bob / "kb_claims.jsonl", [
        shared,
        {"id": "claim_bob", "claim_text": "只有 B 知道", "updated_at": "2026-09-22T00:00:00Z"},
        {"id": "claim_touched", "claim_text": "被改过的", "status": "validated", "updated_at": "2026-09-23T00:00:00Z"},
    ])
    _jsonl(alice / "memory.jsonl", [{"id": "mem_a", "text": "A 记下的"}])
    _jsonl(bob / "jobs.jsonl", [
        {"_op": "declare", "job_id": "job_1", "purpose": "B 起的扫描", "started_at": 1.0},
        {"_op": "update", "job_id": "job_1", "status": "done"},
    ])
    (alice / "runs" / "run_a").mkdir(parents=True)
    (alice / "runs" / "run_a" / "transcript.jsonl").write_text("{}\n", encoding="utf-8")
    (alice / "PROJECT_MANIFEST.md").write_text("派生的", encoding="utf-8")
    # 项目层底下还有名单外的东西：任务合同、花费账、单文档的状态 —— 升级时一样不能丢。
    _jsonl(bob / "tasks" / "tasks.jsonl", [{"id": "task_1", "title": "B 建的任务"}])
    _jsonl(alice / ".harness" / "cost_ledger.jsonl", [{"id": "cost_1", "usd": 0.3}])
    (alice / "research_intake.json").write_text('{"original_text": "A 说的"}', encoding="utf-8")
    (bob / "research_intake.json").write_text('{"original_text": "B 说的"}', encoding="utf-8")
    return alice, bob


def test_two_copies_of_a_project_become_one(tmp_path: Path) -> None:
    state, projects = tmp_path / "state", tmp_path / "state" / "projects"
    alice, bob = _two_people_one_project(state)
    before = {p: p.read_bytes() for p in (alice / "kb_claims.jsonl", bob / "kb_claims.jsonl")}

    outcome = merge_the_per_person_copies(state, projects)

    one = projects / str(PROJECT)
    claims = {r["id"]: r for r in _read(one / "kb_claims.jsonl")}
    assert set(claims) == {"claim_same", "claim_alice", "claim_bob", "claim_touched"}, "两份没并成一份"
    assert claims["claim_touched"]["status"] == "validated", "同 id 不同内容该留 updated_at 新的那条"
    assert outcome.conflicts, "同 id 不同内容要记一笔"
    assert [r["id"] for r in _read(one / "memory.jsonl")] == ["mem_a"]
    declared = [r for r in _read(one / "jobs.jsonl") if r.get("_op") == "declare"]
    assert [r.get("by_user_id") for r in declared] == [str(BOB)], "账本合成一份之后认不出是谁跑的了"
    transcript = one / "runs" / "run_a" / "transcript.jsonl"
    assert transcript.exists() and os.stat(transcript).st_ino == os.stat(
        alice / "runs" / "run_a" / "transcript.jsonl").st_ino, "历史 run 该硬链接过去（两边路径都在、不多占盘）"
    assert not (one / "PROJECT_MANIFEST.md").exists(), "派生的东西不该抄"
    assert [r["id"] for r in _read(one / "tasks" / "tasks.jsonl")] == ["task_1"], "名单外的目录在升级时丢了"
    assert [r["id"] for r in _read(one / ".harness" / "cost_ledger.jsonl")] == ["cost_1"]
    assert json.loads((one / "research_intake.json").read_text(encoding="utf-8"))["original_text"] == "A 说的"
    assert any("research_intake.json" in c for c in outcome.conflicts), "两份不一样的单文档要记一笔"
    assert not (projects / PROJECT.hex).exists(), "同一个项目的两种拼法没合成一个"
    assert {p: p.read_bytes() for p in before} == before, "源目录被动过 —— 说好是抄不是搬"


def test_merging_again_changes_nothing_but_picks_up_what_came_later(tmp_path: Path) -> None:
    """停靠的 worker 跨升级还活着，旧位置可能又多几行 —— 下次启动只把新的合过来。"""
    state, projects = tmp_path / "state", tmp_path / "state" / "projects"
    alice, _bob = _two_people_one_project(state)
    merge_the_per_person_copies(state, projects)
    target = projects / str(PROJECT) / "kb_claims.jsonl"
    settled = target.read_bytes()

    again = merge_the_per_person_copies(state, projects)
    assert again.projects == 0 and target.read_bytes() == settled, "第二次启动又动了一遍"

    with (alice / "kb_claims.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"id": "claim_late", "claim_text": "升级后才写下的"}, ensure_ascii=False) + "\n")
    later = merge_the_per_person_copies(state, projects)
    assert later.projects == 1
    assert "claim_late" in {r["id"] for r in _read(target)}

    # 只在子目录里多了一行（顶层什么都没变）—— 也得认出来。
    with (alice / ".harness" / "cost_ledger.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"id": "cost_late", "usd": 0.1}) + "\n")
    merge_the_per_person_copies(state, projects)
    assert "cost_late" in {r["id"] for r in _read(projects / str(PROJECT) / ".harness" / "cost_ledger.jsonl")}, (
        "子目录里后来写下的没合过来 —— 指纹只看了顶层")


@pytest.mark.asyncio
async def test_the_backend_merges_before_any_worker_starts(tmp_path: Path, monkeypatch) -> None:
    from app import main
    from app.config import data_root, settings, the_projects_home

    monkeypatch.setattr(settings, "platform_data_root", str(tmp_path / "data"))
    _two_people_one_project(data_root("state"))

    await main._one_home_per_project()

    assert (the_projects_home() / str(PROJECT) / "kb_claims.jsonl").exists()
    source = (Path(main.__file__).read_text(encoding="utf-8"))
    body = source[source.index("async def lifespan("):]
    assert body.index("await _one_home_per_project()") < body.index("set_adoption_handler"), (
        "合并要在任何 worker 被接回之前")


@pytest.mark.asyncio
async def test_every_knowledge_question_says_where_the_project_lives(monkeypatch) -> None:
    from app.config import the_projects_home
    from app.services import harness_bridge_once, harness_kb

    asked: list[dict] = []

    async def ask_once(request, **_kw):
        asked.append(request)
        return {"type": "kb_query_result", "records": []}

    monkeypatch.setattr(harness_bridge_once, "ask_once", ask_once)

    class _User:
        id = "u1"
        institution_id = "inst"

    await harness_kb.query(_User(), "p1", "claims")

    assert asked and asked[0]["projects_home"] == str(the_projects_home())


def test_the_session_tells_the_worker_where_the_project_lives() -> None:
    """起 worker 的那一处，给的是 `the_projects_home()`（读调用本身，不是字符串）。"""
    source = (Path(__file__).resolve().parents[1] / "app/services/harness_sessions.py").read_text(encoding="utf-8")
    calls = [node for node in ast.walk(ast.parse(source))
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
             and node.func.attr == "initialize"]
    told = [kw.value for call in calls for kw in call.keywords if kw.arg == "projects_home"]
    assert told and all(isinstance(v, ast.Call) and ast.unparse(v.func) == "the_projects_home" for v in told)

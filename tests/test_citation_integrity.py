"""v0.9: citation integrity 测试。

覆盖：
  - find_phantom_citations 纯 Python 检查
  - validate_artifact_citations 端到端
  - citation_integrity_check hook on_turn_end 自动扫
  - quality_checks._build_state_summary 把 citation_validation 事件 surface
  - reviewer yaml 含 v0.9 rule
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from core.bootstrap import bootstrap
from core.state import State
from shared.lib.citation_integrity import (
    find_phantom_citations,
    validate_artifact_citations,
)


def _make_state(tmp_path: Path, project_id: str = "p_cit") -> State:
    return State.new(node_type="writing", base_dir=tmp_path,
                       project_id=project_id)


# ─────────────────────────────────────────────────────────────────────────────
# find_phantom_citations
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_find_phantom_finds_fake_claim_id(tmp_path):
    bootstrap()
    state = _make_state(tmp_path)
    from core.tool_registry import execute as execute_tool
    # 造一个真的 claim
    c = await execute_tool("create_concept", state,
                              canonical_name="X", concept_type="method",
                              description="test concept")
    cl = await execute_tool("create_claim", state,
                              claim_text="real claim", claim_type="empirical",
                              confidence=0.6, concept_ids=[c["id"]],
                              sources=["doi:10/a"])
    real_id = cl["id"]

    text = f"""
    Manuscript abstract: We cite {real_id} for finding X.
    We also cite claim_deadbeef which is fake.
    And claim_12345678 also fake.
    See {real_id} again for more details.
    """
    res = find_phantom_citations(text, state)
    assert real_id in res["real"]
    assert "claim_deadbeef" in res["phantom"]
    assert "claim_12345678" in res["phantom"]
    assert res["cited_count_per_id"][real_id] == 2
    assert res["cited_count_per_id"]["claim_deadbeef"] == 1


@pytest.mark.asyncio
async def test_find_phantom_no_citations(tmp_path):
    bootstrap()
    state = _make_state(tmp_path)
    res = find_phantom_citations("Just plain text, no citations here.", state)
    assert res["phantom"] == set()
    assert res["real"] == set()


@pytest.mark.asyncio
async def test_find_phantom_handles_latex_escaped_underscore(tmp_path):
    """v1.1 writing 节点产 .tex 时 `_` 必须转义成 `\\_`；正则要兼容这俩形式。"""
    bootstrap()
    state = _make_state(tmp_path)
    from core.tool_registry import execute as execute_tool
    c = await execute_tool("create_concept", state, canonical_name="X",
                              concept_type="method", description="x")
    cl = await execute_tool("create_claim", state, claim_text="real",
                              claim_type="empirical", confidence=0.6,
                              concept_ids=[c["id"]], sources=["doi:10/a"])
    real_id = cl["id"]                                   # e.g. claim_abcd1234
    hex_part = real_id.split("_", 1)[1]

    # LaTeX 转义形式 + raw 形式 + \cite{} 形式 三种都该命中
    text = (
        f"raw: see claim_{hex_part} here.\n"
        f"latex escape: also claim\\_{hex_part} works.\n"
        f"bibtex cite: \\cite{{claim_{hex_part}}} too.\n"
    )
    res = find_phantom_citations(text, state)
    assert real_id in res["real"]
    assert res["phantom"] == set()
    # 三处都被算作引用同一条 claim
    assert res["cited_count_per_id"][real_id] == 3


@pytest.mark.asyncio
async def test_find_phantom_all_real(tmp_path):
    bootstrap()
    state = _make_state(tmp_path)
    from core.tool_registry import execute as execute_tool
    c = await execute_tool("create_concept", state,
                              canonical_name="Y", concept_type="method",
                              description="ok")
    cl = await execute_tool("create_claim", state,
                              claim_text="real",
                              claim_type="empirical", confidence=0.6,
                              concept_ids=[c["id"]], sources=["doi:10/b"])
    text = f"only real ones: {cl['id']} and {cl['id']} again"
    res = find_phantom_citations(text, state)
    assert res["phantom"] == set()
    assert res["real"] == {cl["id"]}


def test_find_phantom_accepts_real_scientific_epistemic_version(tmp_path):
    """Scientific Capital's immutable epv IDs are first-class trace anchors."""
    state = State.new(node_type="writing", base_dir=tmp_path)
    state.project_root = tmp_path / "project"
    capital_root = state.project_root / "scientific_capital"
    capital_root.mkdir(parents=True)
    db_path = capital_root / "scientific_capital.sqlite"
    real_id = "epv_a1b2c3d4e5f60718293a"
    with sqlite3.connect(db_path) as connection:
        connection.executescript(
            """
            CREATE TABLE epistemic_objects (
                id TEXT PRIMARY KEY,
                object_type TEXT NOT NULL
            );
            CREATE TABLE epistemic_versions (
                id TEXT PRIMARY KEY,
                object_id TEXT NOT NULL,
                support_status TEXT NOT NULL
            );
            """
        )
        connection.execute(
            "INSERT INTO epistemic_objects(id, object_type) VALUES (?, ?)",
            ("epi_11111111111111111111", "claim"),
        )
        connection.execute(
            """INSERT INTO epistemic_versions(id, object_id, support_status)
               VALUES (?, ?, ?)""",
            (real_id, "epi_11111111111111111111", "supported"),
        )

    result = find_phantom_citations(
        f"% trace: {real_id}\n% trace: epv_deadbeefdeadbeefdead",
        state,
    )

    assert result["real"] == {real_id}
    assert result["phantom"] == {"epv_deadbeefdeadbeefdead"}
    assert result["cited_count_per_id"][real_id] == 1


@pytest.mark.asyncio
async def test_validate_artifact_citations_pass(tmp_path):
    bootstrap()
    state = _make_state(tmp_path)
    from core.tool_registry import execute as execute_tool
    c = await execute_tool("create_concept", state,
                              canonical_name="Z", concept_type="method",
                              description="ok")
    cl = await execute_tool("create_claim", state,
                              claim_text="real",
                              claim_type="empirical", confidence=0.6,
                              concept_ids=[c["id"]], sources=["doi:10/c"])
    art = state.save_artifact(
        "manuscript", "ms1",
        f"Cited {cl['id']} for support; see {cl['id']}.",
    )
    res = validate_artifact_citations(art["id"], state)
    assert res["passed"] is True
    assert res["n_phantom"] == 0
    assert res["n_cited"] == 1


@pytest.mark.asyncio
async def test_validate_artifact_citations_fail_with_phantom(tmp_path):
    bootstrap()
    state = _make_state(tmp_path)
    from core.tool_registry import execute as execute_tool
    c = await execute_tool("create_concept", state,
                              canonical_name="W", concept_type="method",
                              description="ok")
    cl = await execute_tool("create_claim", state,
                              claim_text="real",
                              claim_type="empirical", confidence=0.6,
                              concept_ids=[c["id"]], sources=["doi:10/d"])
    art = state.save_artifact(
        "manuscript", "ms2",
        f"Cite real {cl['id']} but also fake claim_aabbccdd (4 times: "
        f"claim_aabbccdd claim_aabbccdd claim_aabbccdd).",
    )
    res = validate_artifact_citations(art["id"], state)
    assert res["passed"] is False
    assert res["n_phantom"] == 1
    assert "claim_aabbccdd" in res["phantom_ids"]
    # phantom_with_counts 反映 4 次引用
    pwc = {p["id"]: p["occurrences"] for p in res["phantom_with_counts"]}
    assert pwc["claim_aabbccdd"] == 4


# ─────────────────────────────────────────────────────────────────────────────
# citation_integrity_check hook
# ─────────────────────────────────────────────────────────────────────────────

def test_hook_scans_new_manuscript_artifact(tmp_path):
    bootstrap()
    from core.harness import NodeHarness
    from core.loop_hooks_builtin import _citation_integrity_on_turn_end
    from core.loop_hooks import HookContext

    state = _make_state(tmp_path)
    art = state.save_artifact(
        "manuscript", "ms_with_phantom",
        "Cite claim_aabbccdd and claim_11223344.",   # 都是合规 hex 格式但 KB 没
    )
    ctx = HookContext(
        harness=NodeHarness(node_type="writing"),
        state=state, messages=[], turn=3,
    )
    result = _citation_integrity_on_turn_end(ctx)
    assert result is None

    lines = state.transcript_path.read_text(encoding="utf-8").splitlines()
    cit_events = [json.loads(l) for l in lines
                   if json.loads(l).get("event") == "citation_validation"]
    assert len(cit_events) == 1
    e = cit_events[0]
    assert e["artifact_id"] == art["id"]
    assert e["artifact_type"] == "manuscript"
    assert e["passed"] is False
    assert e["n_phantom"] == 2
    assert set(e["phantom_ids"]) == {"claim_aabbccdd", "claim_11223344"}


def test_hook_skips_non_target_artifact_type(tmp_path):
    """非 manuscript / experiment_log 类型不扫（如 survey_report、prereg）。"""
    bootstrap()
    from core.harness import NodeHarness
    from core.loop_hooks_builtin import _citation_integrity_on_turn_end
    from core.loop_hooks import HookContext

    state = _make_state(tmp_path)
    state.save_artifact("survey_report", "s1", "Cite claim_aaaaaaaa")
    state.save_artifact("pre_registration", "p1", "Cite claim_bbbbbbbb")
    ctx = HookContext(
        harness=NodeHarness(node_type="writing"),
        state=state, messages=[], turn=1,
    )
    _citation_integrity_on_turn_end(ctx)
    if state.transcript_path.exists():
        lines = state.transcript_path.read_text(encoding="utf-8").splitlines()
        cit_events = [json.loads(l) for l in lines
                       if json.loads(l).get("event") == "citation_validation"]
        assert cit_events == []
    # else: transcript 文件不存在 = hook 没写任何事件 = 也算 pass


def test_hook_dedups_across_turns(tmp_path):
    """同一 artifact 不重复扫（除非新 turn 又改了）。靠 hook_state 记 last_seen。"""
    bootstrap()
    from core.harness import NodeHarness
    from core.loop_hooks_builtin import _citation_integrity_on_turn_end
    from core.loop_hooks import HookContext

    state = _make_state(tmp_path)
    state.save_artifact("manuscript", "ms_persist", "claim_ffffeeee fake")
    ctx = HookContext(
        harness=NodeHarness(node_type="writing"),
        state=state, messages=[], turn=1,
    )
    _citation_integrity_on_turn_end(ctx)
    _citation_integrity_on_turn_end(ctx)  # 第二次 —— 不该再写新事件

    lines = state.transcript_path.read_text(encoding="utf-8").splitlines()
    cit_events = [json.loads(l) for l in lines
                   if json.loads(l).get("event") == "citation_validation"]
    assert len(cit_events) == 1   # 仅 1 次


# ─────────────────────────────────────────────────────────────────────────────
# qc engine surface citation_validation in state_summary
# ─────────────────────────────────────────────────────────────────────────────

def test_reviewer_yaml_includes_citation_integrity_rule():
    from core.loader import load_harness
    h = load_harness("_reviewer")
    sp = h.system_prompt
    # 必含 v0.9 关键 phrase
    assert "citation_validation" in sp
    assert "n_phantom" in sp
    # rules 列表也应该有
    rules_text = " ".join(h.rules)
    assert "citation_validation" in rules_text or "phantom" in rules_text.lower()


def test_writing_yaml_enables_citation_hook_and_write_time_enforcement():
    from core.loader import load_harness
    h = load_harness("writing")
    assert "citation_integrity_check" in h.loop_hooks

    # 2026-08-19：`cited_claim_ids_all_exist_in_kb` 已删 —— 执法点从"turn 末 hook
    # 落事件 → QC 读事件"（三跳）前移到 `save_artifact`：manuscript 类型声明了
    # `cites_kb_claims`，引一个 KB 里不存在的 claim 当场写不出来。
    # hook 仍在（它还负责 verdict↔KB 一致性），只是不再是引用诚信的唯一执法点。
    from shared.lib.artifact_policy import cites_kb_claims
    assert cites_kb_claims("manuscript")


# test_experiment_yaml_enables_citation_hook_and_qc 已随 QC 层删除（2026-08-22）：citation_validation 事件仍由 hook 落盘，reviewer 硬规则继续消费

# ↓ 部分测试已随 #627（QC 判定层整体退场，core/quality_checks.py 删除）移除：
#   它们的被测对象是该层本身（_build_state_summary）。层删了测试跟着走。
#   这批 import 断裂曾把整个 collection 挡住 —— 两个各自全绿的 PR 合并后互咬。

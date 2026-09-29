"""Phase F (dreaming auto-trigger), G (stale dead_end), H (disagreement scan) 测试。"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from core.bootstrap import bootstrap
from core.state import State


# ── Phase F: dreaming_scheduler ────────────────────────────────────────────

def test_on_kb_write_increments_counter(tmp_path):
    bootstrap()
    from core.dreaming_scheduler import _read_counter, on_kb_write
    on_kb_write("p1", entity="claims",
                 record={"id": "c1", "claim_type": "empirical"})
    assert _read_counter("p1") == 1
    on_kb_write("p1", entity="claims",
                 record={"id": "c2", "claim_type": "empirical"})
    assert _read_counter("p1") == 2


def test_threshold_marks_pending(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_DREAMING_NEW_CLAIM_THRESHOLD", "3")
    # force module reload to pick env
    import importlib
    from core import dreaming_scheduler
    importlib.reload(dreaming_scheduler)

    for i in range(2):
        dreaming_scheduler.on_kb_write(
            "p_t", entity="claims",
            record={"id": f"c{i}", "claim_type": "empirical"},
        )
    pending = dreaming_scheduler.read_pending("p_t")
    assert pending is None  # 还没到 3

    dreaming_scheduler.on_kb_write(
        "p_t", entity="claims",
        record={"id": "c3", "claim_type": "empirical"},
    )
    pending = dreaming_scheduler.read_pending("p_t")
    assert pending is not None
    assert pending.get("pending") is True
    assert any("3 KB writes" in r for r in pending.get("reasons", []))


def test_dead_end_marks_pending_immediately(tmp_path):
    from core.dreaming_scheduler import on_kb_write, read_pending
    on_kb_write(
        "p_de", entity="claims",
        record={"id": "c1", "claim_type": "dead_end",
                 "dont_repeat_reason": "test"},
    )
    pending = read_pending("p_de")
    assert pending is not None
    assert any("dead_end" in r for r in pending["reasons"])


def test_high_dispute_marks_pending(tmp_path):
    from core.dreaming_scheduler import on_kb_write, read_pending
    rh = [
        {"from_status": "open", "to_status": "validated"},
        {"from_status": "validated", "to_status": "refuted"},
        {"from_status": "refuted", "to_status": "validated"},
    ]
    on_kb_write(
        "p_dispute", entity="claims",
        record={"id": "c1", "claim_type": "empirical",
                 "review_history": rh},
    )
    pending = read_pending("p_dispute")
    assert pending is not None
    assert any("dispute" in r for r in pending["reasons"])


def test_stale_threshold(tmp_path):
    """stale 是**现算的判决**：返回理由，且**不落盘**。

    契约在 2026-08-22 改过。原来它 `mark_pending("stale …")` 把判决写进
    `dreaming_pending.json`，而 `should_run_dreaming()` 命中 pending 就早退、
    不再核对账本 —— 判决一旦落盘就不会因为事实变化而醒来。实测后果：账本
    寻址修好、curator 真跑过 3 次之后，那条判决仍然从文件里复活，门继续拦，
    writing 被拒 23 次。所以现在事件才落盘，局面一律现算。
    """
    from core.dreaming_scheduler import check_stale, read_pending

    old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    reason = check_stale("p_stale", old)
    assert reason is not None and "stale" in reason
    assert read_pending("p_stale") is None, "判决不许落盘"

    new = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    assert check_stale("p_stale_fresh", new) is None


def test_clear_pending(tmp_path):
    from core.dreaming_scheduler import (
        clear_pending, mark_pending, read_pending,
    )
    mark_pending("p_clr", reason="test")
    assert read_pending("p_clr") is not None
    clear_pending("p_clr")
    assert read_pending("p_clr") is None


def test_should_run_dreaming_no_project_returns_false():
    from core.dreaming_scheduler import should_run_dreaming
    should, reasons = should_run_dreaming(None)
    assert should is False


def test_should_run_dreaming_when_pending(tmp_path):
    from core.dreaming_scheduler import mark_pending, should_run_dreaming
    mark_pending("p_run", reason="threshold met")
    should, reasons = should_run_dreaming("p_run")
    assert should is True
    assert "threshold met" in reasons


# ── Phase F: write_kb 触发 on_kb_write ───────────────────────────────

@pytest.mark.asyncio
async def test_write_kb_triggers_dreaming_hook(tmp_path):
    bootstrap()
    state = State.new(node_type="literature", base_dir=tmp_path / "r",
                       project_id="p_hook")
    from core.dreaming_scheduler import _read_counter
    # 写一条 dead_end claim → 立即 mark + counter++
    rec = {
        "claim_text": "X is a dead end on Y",
        "claim_type": "dead_end",
        "dont_repeat_reason": "scope Y fails because of Z",
        "scope": "org", "promoted_from": {"project_id": "p_prev", "source_id": "src_prev", "approved_by": "test", "at": "2026-08-21T00:00:00Z"}, "sources": ["doi:10.1/x"],
        "concept_ids": [], "orphan_reason": "stub",
        "confidence": 0.7,
    }
    final, created = state.write_kb("claims", rec)
    assert created is True
    assert _read_counter("p_hook") >= 1
    from core.dreaming_scheduler import read_pending
    pending = read_pending("p_hook")
    assert pending is not None


# ── Phase G: find_stale_dead_end_or_refuted ─────────────────────────────

@pytest.mark.asyncio
async def test_find_stale_dead_end_returns_old_ones(tmp_path):
    bootstrap()
    state = State.new(node_type="_curator", base_dir=tmp_path,
                       project_id="p_g")
    # 写一个 dead_end，手动设 last_reviewed_at 200 天前
    old_iso = (datetime.now(timezone.utc) - timedelta(days=200)).isoformat()
    fresh_iso = datetime.now(timezone.utc).isoformat()
    # 直接走 write_kb + 手动 patch last_reviewed_at
    rec_old = {
        "claim_text": "A path that failed long ago",
        "claim_type": "dead_end",
        "dont_repeat_reason": "old failure reason",
        "scope": "org", "promoted_from": {"project_id": "p_prev", "source_id": "src_prev", "approved_by": "test", "at": "2026-08-21T00:00:00Z"}, "sources": ["doi:1"],
        "concept_ids": [], "orphan_reason": "stub", "confidence": 0.7,
    }
    final_old, _ = state.write_kb("claims", rec_old)
    # 直接 patch jsonl 把 last_reviewed_at 改成 200 天前（patch_derived 会拒 lifecycle field）
    org_root = Path(__import__("os").environ["HARNESS_FRAMEWORK_ORG_HOME"])
    cj = org_root / "kb_claims.jsonl"
    lines = cj.read_text(encoding="utf-8").splitlines()
    new_lines = []
    for ln in lines:
        if not ln.strip():
            continue
        r = json.loads(ln)
        if r.get("id") == final_old["id"]:
            r["last_reviewed_at"] = old_iso
            r["updated_at"] = old_iso
            r["created_at"] = old_iso
        new_lines.append(json.dumps(r, ensure_ascii=False))
    cj.write_text("\n".join(new_lines) + "\n", encoding="utf-8")

    rec_fresh = {
        "claim_text": "Fresh dead end just now",
        "claim_type": "dead_end",
        "dont_repeat_reason": "recent failure",
        "scope": "org", "promoted_from": {"project_id": "p_prev", "source_id": "src_prev", "approved_by": "test", "at": "2026-08-21T00:00:00Z"}, "sources": ["doi:2"],
        "concept_ids": [], "orphan_reason": "stub", "confidence": 0.7,
    }
    final_fresh, _ = state.write_kb("claims", rec_fresh)

    # 调工具
    from core.tool_registry import execute as execute_tool
    res = await execute_tool(
        "curator_scan", state, scan_type="stale_dead_end", days=180,
    )
    assert res["status"] == "success"
    stale_ids = [s["claim_id"] for s in res["stale_claims"]]
    assert final_old["id"] in stale_ids
    assert final_fresh["id"] not in stale_ids


# ── Phase H: scan_artifact_disagreements ────────────────────────────────

@pytest.mark.asyncio
async def test_scan_finds_english_and_chinese_patterns(tmp_path):
    bootstrap()
    state = State.new(node_type="_curator", base_dir=tmp_path,
                       project_id="p_h")
    text = (
        "Analysis report:\n\n"
        "Result A supports H1.\n"
        "However, I disagree with claim_abcdef12 because the scope is different "
        "(QM9 vs RMD17).\n\n"
        "Note: claim_99887766 is wrong since dataset Z wasn't actually tested.\n\n"
        "我不同意 claim_11223344 因为 ensemble 选错了.\n"
    )
    state.save_artifact("analysis_report", "test_report", text)

    from core.tool_registry import execute as execute_tool
    res = await execute_tool(
        "scan_artifact_disagreements", state, auto_propose=False,
    )
    assert res["status"] == "success"
    ids = sorted(f["claim_id"] for f in res["found"])
    assert "claim_abcdef12" in ids
    assert "claim_99887766" in ids
    assert "claim_11223344" in ids
    assert res["n_findings"] >= 3


@pytest.mark.asyncio
async def test_scan_with_auto_propose_creates_proposals(tmp_path):
    bootstrap()
    state = State.new(node_type="_curator", base_dir=tmp_path,
                       project_id="p_h_auto")
    # 先种两条 KB claim，scan 才能 propose（propose 校验 target_id 在 KB 真存在）
    from core.tool_registry import execute as execute_tool
    cc = await execute_tool("create_concept", state,
                                canonical_name="X", concept_type="method",
                                description="x method")
    cid = cc["id"]
    c1 = await execute_tool("create_claim", state,
                                claim_text="claim one about X",
                                claim_type="empirical", confidence=0.6,
                                concept_ids=[cid], sources=["doi:10/a"])
    c2 = await execute_tool("create_claim", state,
                                claim_text="claim two about X",
                                claim_type="empirical", confidence=0.6,
                                concept_ids=[cid], sources=["doi:10/b"])
    text = (
        f"I disagree with {c1['id']} because Y.\n"
        f"Also challenge {c2['id']}: it's stale.\n"
        # 另一条 typo id 走 disagree 模式但 KB 不存在 → 应被 skip
        "I disagree with claim_deadbeef because the assumption is wrong.\n"
    )
    state.save_artifact("manuscript", "test_ms", text)
    res = await execute_tool(
        "scan_artifact_disagreements", state, auto_propose=True,
    )
    assert res["status"] == "success"
    assert res["proposals_created"] >= 2
    # typo id 应被识别但 skip
    assert res.get("skipped_unknown_claim_ids", 0) >= 1


@pytest.mark.asyncio
async def test_scan_empty_artifact_no_findings(tmp_path):
    bootstrap()
    state = State.new(node_type="_curator", base_dir=tmp_path,
                       project_id="p_empty")
    state.save_artifact("survey_report", "clean", "Just a normal report, no challenges.")
    from core.tool_registry import execute as execute_tool
    res = await execute_tool("scan_artifact_disagreements", state)
    assert res["status"] == "success"
    assert res["n_findings"] == 0


@pytest.mark.asyncio
async def test_scan_specific_artifact_ids(tmp_path):
    bootstrap()
    state = State.new(node_type="_curator", base_dir=tmp_path,
                       project_id="p_specific")
    a1 = state.save_artifact(
        "report", "with_disagree", "I disagree with claim_aaaaaaaa because X."
    )
    a2 = state.save_artifact(
        "report", "clean", "Just normal content."
    )
    from core.tool_registry import execute as execute_tool
    # 只扫 a1
    res = await execute_tool(
        "scan_artifact_disagreements", state,
        artifact_ids=[a1["id"]], auto_propose=False,
    )
    assert res["n_findings"] >= 1
    # 只扫 a2 → 0
    res2 = await execute_tool(
        "scan_artifact_disagreements", state,
        artifact_ids=[a2["id"]], auto_propose=False,
    )
    assert res2["n_findings"] == 0


# ── Phase E: context engine 注入 KB heuristic rule ─────────────────────

def test_kb_heuristic_injected_for_producing_nodes():
    from core.context_engine import _should_inject_kb_heuristic
    assert _should_inject_kb_heuristic("literature")
    assert _should_inject_kb_heuristic("experiment")
    assert _should_inject_kb_heuristic("_curator")
    assert not _should_inject_kb_heuristic("_orchestrator")


def test_kb_heuristic_appears_in_system_prompt(tmp_path):
    """build_messages 给 producing 节点应含启发式 rule。"""
    bootstrap()
    from core.context_engine import build_messages
    from core.loader import load_harness
    state = State.new(node_type="literature", base_dir=tmp_path)
    h = load_harness("literature")
    msgs = build_messages(h, state, {})
    sys = msgs[0].content or ""
    assert "KB 使用启发式" in sys or "历史记录参考" in sys
    assert "I disagree with claim_" in sys

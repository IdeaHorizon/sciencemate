"""core/api.py 只读 API 单测。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import api
from core.paths import org_root


def _seed_project(home: Path, proj_id: str, *,
                   n_claims: int = 3, n_validated: int = 1):
    pdir = home / "projects" / proj_id
    pdir.mkdir(parents=True, exist_ok=True)
    with (pdir / "kb_claims.jsonl").open("w") as f:
        for i in range(n_claims):
            r = {
                "id": f"claim_{proj_id}_{i:03d}",
                "claim_text": f"claim {i}",
                "claim_type": "empirical",
                "status": "validated" if i < n_validated else "provisional",
                "concept_ids": ["c1"],
                "sources": [f"chunk_{i}"],
                "confidence": 0.9 if i < n_validated else 0.5,
            }
            f.write(json.dumps(r) + "\n")
    (pdir / "PROJECT_MANIFEST.md").write_text("# manifest\n", encoding="utf-8")
    return pdir


def _write_run(rdir: Path, run_id: str, project_id: str | None, status: str):
    (rdir / "artifacts").mkdir(parents=True, exist_ok=True)
    (rdir / "summary.json").write_text(json.dumps({
        "run_id": run_id, "node_type": "test", "project_id": project_id,
        "status": status, "turns": 5, "tokens_used": 1000,
        "started_at": "2026-05-16T00:00:00Z",
        "ended_at": "2026-05-16T00:10:00Z",
        "artifacts": [],
    }), encoding="utf-8")
    return rdir


def _seed_run(home: Path, run_id: str, project_id: str, status: str = "completed"):
    """[v0.8] 新主布局：projects/<id>/runs/<run_id>/。"""
    return _write_run(home / "projects" / project_id / "runs" / run_id,
                      run_id, project_id, status)


def _seed_run_anon(home: Path, run_id: str, status: str = "completed"):
    """[v0.8] 无 project_id 的 ad-hoc run：runs_anon/<run_id>/。"""
    return _write_run(home / "runs_anon" / run_id, run_id, None, status)


def _seed_run_flat_legacy(home: Path, run_id: str, project_id: str,
                           status: str = "completed"):
    """[deprecated] 旧 flat 布局 runs/<run_id>/ —— 只测 backward-compat 读。"""
    return _write_run(home / "runs" / run_id, run_id, project_id, status)


def test_list_projects_empty(isolate_harness_home):
    assert api.list_projects() == []


def test_list_projects_with_one(isolate_harness_home):
    _seed_project(isolate_harness_home, "proj_a", n_claims=5, n_validated=2)
    out = api.list_projects()
    assert len(out) == 1
    assert out[0]["project_id"] == "proj_a"
    assert out[0]["claims"] == 5
    assert out[0]["validated"] == 2


def test_project_summary(isolate_harness_home):
    _seed_project(isolate_harness_home, "proj_a", n_claims=3, n_validated=1)
    s = api.project_summary("proj_a")
    assert s["claims_total"] == 3
    assert s["by_status"]["validated"] == 1
    assert s["by_status"]["provisional"] == 2
    assert s["by_claim_type"]["empirical"] == 3


def test_project_summary_nonexistent(isolate_harness_home):
    assert api.project_summary("nope") is None


def test_list_runs_filter_by_project(isolate_harness_home):
    _seed_run(isolate_harness_home, "r1", "proj_a")
    _seed_run(isolate_harness_home, "r2", "proj_b")
    _seed_run(isolate_harness_home, "r3", "proj_a")
    all_runs = api.list_runs()
    assert len(all_runs) == 3
    proj_a = api.list_runs("proj_a")
    assert len(proj_a) == 2


def test_run_in_nested_layout_visible(isolate_harness_home):
    """[v0.8 回归] projects/<id>/runs/<rid>/ 下的 run 必须能被
    list_runs + run_detail 看到（api.py 曾只读旧 flat runs/，对新 run 全盲）。"""
    rdir = _seed_run(isolate_harness_home, "r_nested", "proj_a")
    assert rdir == isolate_harness_home / "projects" / "proj_a" / "runs" / "r_nested"

    runs = api.list_runs()
    assert [r["run_id"] for r in runs] == ["r_nested"]
    assert runs[0]["project_id"] == "proj_a"
    assert runs[0]["path"] == str(rdir)

    detail = api.run_detail("r_nested")
    assert detail is not None
    assert detail["path"] == str(rdir)
    assert detail["summary"]["run_id"] == "r_nested"


def test_list_runs_spans_anon_and_legacy_flat(isolate_harness_home):
    """runs_anon/ 与旧 flat runs/ 都要可见（flat 是 backward-compat 只读）。"""
    _seed_run(isolate_harness_home, "r_nested", "proj_a")
    _seed_run_anon(isolate_harness_home, "r_anon")
    _seed_run_flat_legacy(isolate_harness_home, "r_flat", "proj_a")

    runs = api.list_runs()
    assert {r["run_id"] for r in runs} == {"r_nested", "r_anon", "r_flat"}
    # anon run 没 project_id → project 过滤时不该混进来
    proj_a = api.list_runs("proj_a")
    assert {r["run_id"] for r in proj_a} == {"r_nested", "r_flat"}
    # run_detail 对旧 flat 同样可用
    assert api.run_detail("r_flat") is not None
    assert api.run_detail("r_anon") is not None


def test_kb_stats_org(isolate_harness_home):
    org_root().mkdir(parents=True, exist_ok=True)
    (org_root() / "kb_concepts.jsonl").write_text(
        json.dumps({"id": "concept_x", "canonical_name": "X",
                     "concept_type": "method", "description": "y"}) + "\n",
        encoding="utf-8",
    )
    s = api.kb_stats(None)
    assert s["scope"] == "org"
    assert s["concepts"] == 1


def test_find_last_error_none(isolate_harness_home):
    _seed_run(isolate_harness_home, "r1", "proj_a", status="completed")
    assert api.find_last_error() is None


def test_find_last_error_found(isolate_harness_home):
    rdir = _seed_run(isolate_harness_home, "r1", "proj_a", status="error")
    # add a transcript with an error event
    (rdir / "transcript.jsonl").write_text(
        json.dumps({"event": "tool_call_end", "tool_name": "run_bash",
                     "result": {"status": "error", "error": "LAMMPS exited 1"},
                     "at": "2026-05-16T00:05:00Z"}) + "\n",
        encoding="utf-8",
    )
    err = api.find_last_error()
    assert err is not None
    assert err["run_id"] == "r1"
    assert err["error_event"]["result"]["status"] == "error"


def test_cost_estimate(isolate_harness_home):
    _seed_run(isolate_harness_home, "r1", "proj_a")
    _seed_run(isolate_harness_home, "r2", "proj_a")
    c = api.cost_estimate("proj_a")
    assert c["run_count"] == 2
    assert c["total_tokens"] == 2000


def test_pending_proposals(isolate_harness_home):
    pdir = isolate_harness_home / "projects" / "proj_a"
    pdir.mkdir(parents=True)
    (pdir / "kb_proposals.jsonl").write_text(
        "\n".join([
            json.dumps({"id": "p1", "status": "pending", "proposal_type": "kb_action"}),
            json.dumps({"id": "p2", "status": "accepted", "proposal_type": "kb_action"}),
        ]) + "\n",
        encoding="utf-8",
    )
    pending = api.pending_proposals("proj_a")
    assert len(pending) == 1
    assert pending[0]["id"] == "p1"


def test_cache_info(monkeypatch, isolate_harness_home):
    monkeypatch.setenv("HARNESS_LLM_CACHE", "on")
    s = api.cache_info()
    assert "path" in s
    assert s["count"] == 0
    assert s["enabled"] is True

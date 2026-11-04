"""Reviewer 要拿用户的副作用约束对一遍被审 run 实际改了什么（#973）。

现场（2026-09-12）：审稿在 seq 224/243 明说「全程只读」「未安装/编译/修改」，并以
confidence 0.92 approve —— 它核对了产物和日志，却没把改动清单和用户那句「不要
修改文件」放在一起比。那 24 个文件都在 transcript 的 `workspace_changed` 里。

裁决（wangd 2026-10-21）：`_reviewer` 加副作用检查。这里钉三件事：
  · 约束与三类改动由框架组进审稿的 context（不靠它想起来去翻 transcript）；
  · 只读约束下改了用户源文件 → 审查不能 approve（set_verdict 拒收）；
  · critique 里的 `side_effect_check` 由框架盖章。
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from core.context_engine import build_messages
from core.loader import load_harness
from core.side_effect_contract import declare_read_only
from core.state import State
from nodes._reviewer.tools.critique_builder import _compose_review_critique

_EVIDENCE = ["experiments/runtime/environment_audit_report.md",
             "experiments/artifacts/clean_results.json",
             "experiments/repro/run_manifest.json"]


def _producer(runs: Path, *, read_only: bool, user_source: list[str] = ()) -> State:
    producer = State.new("experiment", runs, project_id="p973")
    if read_only:
        declare_read_only(producer, quote="只做检查和报告，不要修改文件", said_by="user")
    producer.append_transcript("workspace_changed", paths=_EVIDENCE)
    if user_source:
        producer.append_transcript("workspace_out_of_scope_writes", paths=list(user_source))
    return producer


def _reviewer(runs: Path, producer: State) -> tuple[State, dict]:
    reviewer = State.new("_reviewer", runs, project_id="p973")
    reviewer.save_artifact("survey_report", "x", "review subject")
    inputs = {"artifact_id": "survey_report__x", "source_node_type": "experiment",
              "producer_run_id": producer.run_id}
    reviewer.hook_state["node_inputs"] = dict(inputs)
    return reviewer, inputs


def _context(reviewer: State, inputs: dict) -> str:
    messages = build_messages(load_harness("_reviewer"), reviewer, node_inputs=inputs)
    return "\n".join(m.content or "" for m in messages)


def _compose(state, action, **kw):
    return asyncio.run(_compose_review_critique(state, action, **kw))


def test_the_contract_and_the_three_classes_reach_the_reviewers_context(tmp_path):
    producer = _producer(tmp_path / "runs", read_only=True)
    reviewer, inputs = _reviewer(tmp_path / "runs", producer)

    text = _context(reviewer, inputs)

    assert "被审 run 的文件改动" in text, "副作用事实没组进审稿的 context"
    assert "只读（不修改文件）" in text and "只做检查和报告" in text
    assert "用户源文件 0 个" in text and "节点证据 3 个" in text
    assert "未修改文件" in text, "没告诉审稿：节点证据不为 0 时「未修改文件」就是失实"
    assert "⛔" not in text, "只有节点证据不构成矛盾 —— 不许误报"


def test_a_contradiction_forbids_approval(tmp_path):
    producer = _producer(tmp_path / "runs", read_only=True, user_source=["src/model.py"])
    reviewer, inputs = _reviewer(tmp_path / "runs", producer)
    assert "⛔" in _context(reviewer, inputs)

    for verdict in ("approve", "approve_with_revisions"):
        refused = _compose(reviewer, "set_verdict", verdict=verdict,
                           confidence=0.92, summary="全程只读，未修改文件")
        assert refused["status"] == "error", f"{verdict} 在矛盾面前被收下了"
        assert "src/model.py" in refused["error"]
    assert _compose(reviewer, "set_verdict", verdict="major_concerns",
                    confidence=0.9, summary="改了用户源文件")["status"] == "success"


def test_without_a_contradiction_approval_is_the_reviewers_call(tmp_path):
    """反作弊：节点证据、或没有只读约束 —— 都不许被框架拦成不能 approve。"""
    for read_only in (True, False):
        runs = tmp_path / f"runs_{read_only}"
        producer = _producer(runs, read_only=read_only, user_source=(
            [] if read_only else ["src/model.py"]))
        reviewer, inputs = _reviewer(runs, producer)
        _context(reviewer, inputs)
        ok = _compose(reviewer, "set_verdict", verdict="approve", confidence=0.9, summary="ok")
        assert ok["status"] == "success", (read_only, ok)


def test_the_critique_carries_a_framework_stamped_side_effect_check(tmp_path):
    producer = _producer(tmp_path / "runs", read_only=True, user_source=["src/model.py"])
    reviewer, inputs = _reviewer(tmp_path / "runs", producer)
    # 不经 context 组装也算得出来（续跑的审稿没有那一步）—— 盖章不靠模型记得
    _compose(reviewer, "set_verdict", verdict="block", confidence=0.9, summary="越界")
    _compose(reviewer, "set_recommended_action", recommended_action="revise",
             feedback_to_next_run="撤回对 src/model.py 的改动")
    result = _compose(reviewer, "finalize", name="experiment_critique_t",
                      artifact_under_review="survey_report__x", source_node_type="experiment")
    assert result.get("status") != "error", result

    record = reviewer.read_artifact(result["artifact_id"])
    content = json.loads(record["content"])
    check = content["side_effect_check"]
    assert check["stamped_by"] == "framework"
    assert check["promised_read_only"] is True and check["contradiction"] is True
    assert check["user_source"] == ["src/model.py"]
    assert check["counts"] == {"user_source": 1, "node_evidence": 3, "bookkeeping": 0}
    meta = record["metadata"]
    meta = json.loads(meta) if isinstance(meta, str) else meta
    assert meta["side_effect_check"]["contradiction"] is True


def test_a_producer_run_id_cannot_walk_out_of_the_runs_directory(tmp_path):
    from core.side_effect_contract import producer_side_effect_check

    reviewer = State.new("_reviewer", tmp_path / "runs", project_id="p973")
    assert producer_side_effect_check(reviewer, {"producer_run_id": "../../etc"}) is None
    assert producer_side_effect_check(reviewer, {"producer_run_id": "no-such-run"}) is None

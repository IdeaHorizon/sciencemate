"""#879 复审第三条：closure 有效视图必须覆盖 raw/clean，否则死锁换个类型就复现。

活体形态（2026-09-08 CMake）：run 先被判 scientific，存下一份未冻结草稿，随后
改判 operation → closure 把该草稿计成 foreign → kind=conflict → sealed → 路线不可
再改，而修好它恰恰需要改路线。此前只有 experiment_log 有否定通道，把剧本换成
raw_results / clean_results 就原样死锁。

这里钉住的不变量：
- 三类 closure 草稿共用同一个有效视图，冲突判定不因类型不同而分叉；
- raw/clean 是同一份证据的两半，能一起否定时由一条记录成对否定；
- frozen 产物永不可否定（目标本身冻结仍然硬拒）；但**配对的另一半**无法随本次
  一起否定时如实记账后只否定本半 —— 2026-09-09 复审实测：拒绝在这里造出的正是
  它声称要防的半份视图（同一 raw 下两份 clean，否定第一份成功并带走 raw，否定
  第二份被拒 → active clean 非空、active raw 为空），放行第二份才让账本收敛；
- 跨 run 的另一半在候选阶段就过滤，不当作拒绝理由：否则正文如实写出处的被罚，
  写一个不存在的 id 的反而放行；
- 否定记录必须冻结、属本 run、payload 与 metadata 自洽，且自称的类型要与被否定
  产物的实际类型一致 —— 一条 log 的否定记录不能用来隐藏一份 raw_results；
- 冲突拒绝必须带可执行出口（BF-12）。
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from core.project_workspace import _NODE_WORKSPACES
from core.state import State
from nodes.experiment.tools import run_contract
from nodes.experiment.tools.contract_audit import (
    _supersede_closure_draft, active_closure_artifacts,
)
from nodes.experiment.tools.operation_completion import (
    _closure_state, operation_closure_status,
)


def _operation_state(tmp_path: Path) -> State:
    state = State.new("experiment", tmp_path)
    state.hook_state.setdefault(
        "node_inputs", {"experiment_focus": "验证一次受管操作"})
    asyncio.run(run_contract._classify_experiment_scope(
        state, scope="operation", operation_category="format_validation",
        reason="机械验收，不推导科学结论"))
    return state


def _two_runs_sharing_a_worktree(tmp_path: Path) -> tuple[State, State]:
    """两个 run 绑定同一个 Project worktree、共用工作区账本（C1 的真实布局）。"""
    worktree = tmp_path / "worktree"
    records = worktree / _NODE_WORKSPACES["experiment"]
    records.mkdir(parents=True)
    runs = []
    for _ in range(2):
        run = State.new("experiment", tmp_path / "runs")
        run.project_worktree = worktree
        run.workspace_records_dir = records
        runs.append(run)
    return runs[0], runs[1]


def _draft_pair(state: State) -> tuple[str, str]:
    """先按 scientific 存下的一对 raw/clean 草稿（clean 声明它引用的 raw）。"""
    raw = state.save_artifact(
        "raw_results", "mistaken_raw", json.dumps({"files": []}))
    clean = state.save_artifact(
        "clean_results", "mistaken_clean",
        json.dumps({"raw_results_artifact_id": raw["id"], "rows": []}))
    return raw["id"], clean["id"]


def test_raw_clean_pair_is_superseded_atomically_from_either_half(tmp_path):
    state = _operation_state(tmp_path)
    raw_id, clean_id = _draft_pair(state)
    assert operation_closure_status(state)["kind"] == "conflict"

    # 只给 clean 一个 id，raw 必须一起被否定。
    result = asyncio.run(_supersede_closure_draft(
        state, clean_id, reason="先按 scientific 存下的误建草稿"))

    assert result["status"] == "success", result
    assert result["artifact_type"] == "clean_results"
    assert result["linked_superseded_id"] == raw_id
    for artifact_type in ("raw_results", "clean_results"):
        active, superseded = active_closure_artifacts(state, artifact_type)
        assert active == [], (artifact_type, active)
        assert len(superseded) == 1
    assert _closure_state(state, f"{state.run_id}:operation")["kind"] == "empty"


def test_superseding_the_raw_half_also_negates_its_clean(tmp_path):
    state = _operation_state(tmp_path)
    raw_id, clean_id = _draft_pair(state)

    result = asyncio.run(_supersede_closure_draft(
        state, raw_id, reason="误建草稿"))

    assert result["status"] == "success", result
    assert result["linked_superseded_id"] == clean_id
    assert active_closure_artifacts(state, "clean_results")[0] == []


def test_a_frozen_half_is_disclosed_not_refused(tmp_path):
    """配对的另一半已冻结：只否定本半 + 如实记账，不拒绝。

    原实现在这里硬拒，理由是"不能只隐藏未冻结的那一半"。但 frozen 产物被
    ``active_closure_artifacts`` 无条件保留在有效视图里，本来就藏不掉 ——
    该危害在代码上不成立，而拒绝会让误建的 clean 永远撤不下来。
    """
    state = _operation_state(tmp_path)
    raw_id, clean_id = _draft_pair(state)
    state.mark_frozen(raw_id)

    result = asyncio.run(_supersede_closure_draft(
        state, clean_id, reason="撤下误建的 clean，raw 已是已验证证据"))

    assert result["status"] == "success", result
    assert "linked_superseded_id" not in result
    assert result["unpaired_half"] == {
        "linked_artifact_id": raw_id,
        "linked_artifact_type": "raw_results",
        "unpaired_reason": "linked_half_frozen"}
    # 本半撤下；冻结的那一半原样留在有效视图里，没有被隐藏。
    assert active_closure_artifacts(state, "clean_results")[0] == []
    assert [i["id"] for i in active_closure_artifacts(state, "raw_results")[0]] \
        == [raw_id]
    # 注记进了不可变记录，不只是返回值。
    negation = state.read_artifact(result["supersession_id"])
    assert json.loads(negation["content"])["unpaired_half"]["unpaired_reason"] \
        == "linked_half_frozen"


def test_a_frozen_draft_can_never_be_superseded(tmp_path):
    state = _operation_state(tmp_path)
    saved = state.save_artifact(
        "raw_results", "verified_raw", json.dumps({"files": []}))
    state.mark_frozen(saved["id"])

    result = asyncio.run(_supersede_closure_draft(
        state, saved["id"], reason="试图否定已验证证据"))

    assert result["status"] == "error", result
    assert "已冻结" in result["error"]


def test_a_log_supersession_cannot_hide_a_raw_results(tmp_path):
    """否定记录自称的类型必须与被否定产物的实际类型一致。"""
    state = _operation_state(tmp_path)
    raw = state.save_artifact(
        "raw_results", "target_raw", json.dumps({"files": []}))
    # 手工伪造一条"experiment_log 类型"的否定记录，指向一份 raw_results。
    forged = state.save_artifact(
        "experiment_log_supersession", "forged",
        json.dumps({"superseded_id": raw["id"], "artifact_type": "experiment_log",
                    "reason": "伪造", "run_id": state.run_id},
                   ensure_ascii=False, sort_keys=True),
        metadata={"superseded_id": raw["id"], "artifact_type": "experiment_log",
                  "frozen": True})
    assert forged["id"]

    active, superseded = active_closure_artifacts(state, "raw_results")

    assert [item["id"] for item in active] == [raw["id"]]
    assert superseded == []


def test_operation_conflict_refusal_names_an_executable_exit(tmp_path):
    """BF-12：拒绝必须让调用方在一轮内知道怎么做。"""
    from nodes.experiment.tools.operation_completion import (
        _record_operation_completion,
    )
    state = _operation_state(tmp_path)
    _draft_pair(state)
    evidence = Path(state.root) / "op.stdout"
    evidence.write_text("returncode=0\n", encoding="utf-8")

    result = asyncio.run(_record_operation_completion(
        state, task_kind="generic", objective="验证受管操作",
        outcome="success", checks=[], artifact_paths=[str(evidence)],
        next_step="report"))

    assert result["status"] == "error"
    assert result["error_code"] == "operation_closure_conflict"
    assert result["recovery_tool"] == "supersede_closure_draft"
    assert "supersede_closure_draft(artifact_id=" in result["error"]


def test_a_partner_from_another_run_is_filtered_at_the_candidate_stage(tmp_path):
    """配对的另一半属于别的 run：候选阶段过滤，只否定本半。

    可达路径（已实测）：artifacts 目录跨 run 共享，本 run 的 clean_results 正文由
    模型书写，其声明的 raw_results_artifact_id 可以指向上一个 run 留下的 raw。
    那份 raw 本来就不在本 run 的 closure 有效视图里，成对否定无必要也不可能完成；
    节点规则要求外 run 的合法产物在候选阶段过滤，而不是当作本 run 的完整性错误。
    """
    prior, state = _two_runs_sharing_a_worktree(tmp_path)
    state.hook_state.setdefault(
        "node_inputs", {"experiment_focus": "验证一次受管操作"})
    asyncio.run(run_contract._classify_experiment_scope(
        state, scope="operation", operation_category="format_validation",
        reason="机械验收"))
    raw = prior.save_artifact(
        "raw_results", "prior_raw", json.dumps({"files": []}))
    clean = state.save_artifact(
        "clean_results", "this_run_clean",
        json.dumps({"raw_results_artifact_id": raw["id"], "rows": []}))

    result = asyncio.run(_supersede_closure_draft(
        state, clean["id"], reason="误建草稿"))

    assert result["status"] == "success", result
    # 跨 run 的那一半不写进 linked_superseded_id：本 run 的记录不去声明别的
    # run 的证据 —— 那才是这里真正的账本完整性顾虑。
    assert "linked_superseded_id" not in result
    assert result["unpaired_half"]["unpaired_reason"] \
        == "declared_partner_from_another_run"
    assert result["unpaired_half"]["declared_raw_results_artifact_id"] == raw["id"]
    assert active_closure_artifacts(state, "clean_results")[0] == []


def test_a_second_clean_draft_can_still_be_superseded(tmp_path):
    """CA-03 复现件：同一份 raw 下的第二份 clean 草稿必须能撤下来。

    原实现在"配对的另一半已有否定记录"处硬拒，结果是 active clean 非空、
    active raw 为空 —— 正是它注释里说要防的半份视图。放行第二份才收敛。
    """
    state = _operation_state(tmp_path)
    raw = state.save_artifact("raw_results", "R", json.dumps({"files": []}))
    drafts = [
        state.save_artifact(
            "clean_results", name,
            json.dumps({"raw_results_artifact_id": raw["id"], "rows": []}))["id"]
        for name in ("C1", "C2")
    ]

    first = asyncio.run(_supersede_closure_draft(
        state, drafts[0], reason="误建草稿其一"))
    assert first["status"] == "success", first
    assert first["linked_superseded_id"] == raw["id"]

    second = asyncio.run(_supersede_closure_draft(
        state, drafts[1], reason="误建草稿其二"))

    assert second["status"] == "success", second
    assert second["unpaired_half"] == {
        "linked_artifact_id": raw["id"],
        "linked_artifact_type": "raw_results",
        "unpaired_reason": "linked_half_already_superseded",
        "linked_supersession_id": first["supersession_id"]}
    # 两类都清空：账本一致，没有半份视图。
    for artifact_type in ("raw_results", "clean_results"):
        assert active_closure_artifacts(state, artifact_type)[0] == [], artifact_type


def test_honest_provenance_is_not_punished_harder_than_a_bogus_id(tmp_path):
    """如实写出处的 clean，不能比正文写了个不存在 id 的 clean 更难撤下。

    原实现里这条不对称是真实存在的：partner 读不出 → 当场放行；partner 读得出
    但属于别的 run → 硬拒。撤销一份误建草稿的难度不该取决于它有多诚实。
    """
    prior, state = _two_runs_sharing_a_worktree(tmp_path)
    state.hook_state.setdefault(
        "node_inputs", {"experiment_focus": "验证一次受管操作"})
    asyncio.run(run_contract._classify_experiment_scope(
        state, scope="operation", operation_category="format_validation",
        reason="机械验收"))
    prior_raw = prior.save_artifact(
        "raw_results", "prior_raw", json.dumps({"files": []}))
    honest = state.save_artifact(
        "clean_results", "honest",
        json.dumps({"raw_results_artifact_id": prior_raw["id"], "rows": []}))
    bogus = state.save_artifact(
        "clean_results", "bogus",
        json.dumps({"raw_results_artifact_id": "does_not_exist", "rows": []}))

    honest_result = asyncio.run(_supersede_closure_draft(
        state, honest["id"], reason="误建"))
    bogus_result = asyncio.run(_supersede_closure_draft(
        state, bogus["id"], reason="误建"))

    assert honest_result["status"] == bogus_result["status"] == "success"
    # 两者都留下注记，差别只在原因，不在能不能撤。
    assert honest_result["unpaired_half"]["unpaired_reason"] \
        == "declared_partner_from_another_run"
    assert bogus_result["unpaired_half"]["unpaired_reason"] \
        == "declared_partner_unreadable"

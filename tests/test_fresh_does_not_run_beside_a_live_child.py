"""fresh 是「放弃续跑另起一条」，不是「在旧的还活着时并排再起一条」（#1199，#1052 验收 4）。

`run_node(resume_run_id='fresh')` 此前只写一条 `run_node_fresh_requested` 就新开 child，旧的
同任务 child 有没有停下、终态有没有落定，都不管。#1164 之后「旧 child 还在跑」不会再被**续**出
第二个 loop，但 fresh 仍然可以在旧 child 还活着的时候，为同一件事并排再起一个 —— 两个执行者为
同一件事各写一份结果，正是 #1052 里两份互相矛盾的交付的来路。

裁决（wangd 2026-10-21）：拒绝并给出 cancel 出口；不自动 cancel（取消不可逆，要模型显式做）；
不允许并存。「终态」以 finalize 写完为准（run_end 是它的最后一笔）。

判据：
1. 同任务身份、还有执行者的 child 在 → `live_same_task_children` 认得出；执行者一走 → 空；
   有 run_end → 空；身份对不上 → 空；没带身份 → 空（CLI、老 run 判不了，不把缺省改成硬失败）。
2. 接线：`run_node` 的 fresh 分支**调用**了它（AST 查 Call，不查名字出现过）。
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from core.bootstrap import bootstrap
from core.pause import ActiveRunInfo, clear_all, register_active, unregister_active
from core.state import State

bootstrap()

from shared.tools import run_node as rn   # noqa: E402

_IDENTITY = {
    "task_instance_uuid": "task-uuid-1",
    "task_contract_revision": 1,
    "task_contract_digest": "digest-1",
}


@pytest.fixture(autouse=True)
def _clean_registry():
    clear_all()
    yield
    clear_all()


def _orchestrator(tmp_path: Path) -> State:
    return State.new(node_type="_orchestrator", base_dir=tmp_path / "runs")


def _child(parent: State, run_id: str, *, node_type: str = "experiment",
           identity: dict | None = _IDENTITY, ended: bool = False) -> Path:
    d = parent.root.parent / run_id
    d.mkdir(parents=True, exist_ok=True)
    with (d / "transcript.jsonl").open("w", encoding="utf-8") as f:
        f.write(json.dumps({"event": "run_start", "node_type": node_type,
                            "parent_run_id": parent.run_id, **(identity or {})}) + "\n")
        f.write(json.dumps({"event": "tool_call", "name": "x"}) + "\n")
        if ended:
            f.write(json.dumps({"event": "run_end", "status": "completed"}) + "\n")
    return d


def _mark_live(run_id: str, base: Path, node_type: str = "experiment") -> None:
    live = State.reopen(node_type=node_type, base_dir=base, run_id=run_id)
    register_active(ActiveRunInfo(run_id=run_id, node_type=node_type, state=live))


def test_a_live_child_of_the_same_task_is_found_and_goes_away_with_its_executor(tmp_path):
    parent = _orchestrator(tmp_path)
    _child(parent, "run_exp_1")
    assert rn.live_same_task_children(parent, "experiment", _IDENTITY) == [], "没人在跑 = 被打断，不是活的"

    _mark_live("run_exp_1", parent.root.parent)
    live = rn.live_same_task_children(parent, "experiment", _IDENTITY)
    assert [info["run_id"] for info in live] == ["run_exp_1"]

    unregister_active("run_exp_1")
    assert rn.live_same_task_children(parent, "experiment", _IDENTITY) == []


def test_a_finished_child_and_a_different_task_are_not_live_children(tmp_path):
    parent = _orchestrator(tmp_path)
    _child(parent, "run_done", ended=True)
    _child(parent, "run_other", identity={**_IDENTITY, "task_contract_digest": "digest-2"})
    _child(parent, "run_other_node", node_type="observation")
    for run_id in ("run_done", "run_other", "run_other_node"):
        _mark_live(run_id, parent.root.parent,
                   node_type="observation" if run_id == "run_other_node" else "experiment")
    assert rn.live_same_task_children(parent, "experiment", _IDENTITY) == []


def test_without_a_task_identity_the_question_cannot_be_asked(tmp_path):
    """CLI / 老 run 没有身份：判不了就返回空，别把一条缺省改成硬失败。"""
    parent = _orchestrator(tmp_path)
    _child(parent, "run_exp_1")
    _mark_live("run_exp_1", parent.root.parent)
    assert rn.live_same_task_children(parent, "experiment", None) == []
    assert rn.live_same_task_children(parent, "experiment", {"task_instance_uuid": ""}) == []


def test_the_fresh_branch_actually_calls_the_check():
    """接线：fresh 分支里真的调了 live_same_task_children，并以 fresh_target_still_running 拒绝。
    查 Call 不查名字出现过 —— 一个只在注释里提到的名字不是接线。"""
    source = Path(rn.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    fresh_branches = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.If) and isinstance(node.test, ast.Compare)
        and isinstance(node.test.left, ast.Name) and node.test.left.id == "resume_run_id"
        and any(isinstance(c, ast.Constant) and c.value == "fresh" for c in node.test.comparators)
    ]
    assert fresh_branches, "找不到 `if resume_run_id == \"fresh\"` 分支"
    branch = fresh_branches[0]
    calls = [n for n in ast.walk(branch) if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Name) and n.func.id == "live_same_task_children"]
    assert calls, "fresh 分支没有调用 live_same_task_children —— 判据存在但没接到路径"
    codes = [n.value for n in ast.walk(branch) if isinstance(n, ast.Constant)
             and n.value == "fresh_target_still_running"]
    assert codes, "fresh 分支没有以 fresh_target_still_running 拒绝"
    text = ast.get_source_segment(source, branch) or ""
    assert "cancel" in text, "拒绝要给出 cancel 出口"

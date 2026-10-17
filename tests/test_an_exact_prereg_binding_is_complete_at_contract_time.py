"""exact 预注册绑定在**写合同那一刻**就三件齐全（#1099 Core 部分）。

## 之前

`task(action='contract', prereg_artifact_id=X)` 只给 id 也照写：`PreregAssignment.exact`
只要 artifact_id，工具 schema 把 version / content_hash 标成可选。可执行节点要的是
(id, 正整数 version, 64 位 hash) 三件一起 —— 于是合同写进去了、派发也走了，子 run
起步时才以 `prereg_assignment_invalid` 拒掉。错在父这一步，看见它却要等子 run
起来；子 run 无权改派发输入，只能 blocked 等重派。

## 现在

只给 id 时从该 prereg 的**冻结 head** 补齐；head 未冻结、草稿压在旧冻结版上、id 找不
到、给的版本 / 哈希对不上账 —— 都在写合同时拒，报错写明缺哪几件、账上有哪些冻结版。
全部走真入口 `execute_tool("task", ...)`。
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from core.bootstrap import bootstrap
from core.ledger import record_version
from core.state import State
from core.task_contract import (
    ASSIGNMENT_EXACT,
    PreregAssignment,
    TaskContractError,
    TaskContractLog,
)
from core.tool_registry import execute as execute_tool


def _worktree(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    for args in (["init", "-b", "main"], ["config", "user.name", "T"],
                 ["config", "user.email", "t@example.test"]):
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)
    (root / "project.yaml").write_text("schema_version: 2\nname: prereg contract\n",
                                       encoding="utf-8")
    for node in ("hypothesis", "experiment"):
        (root / node / "artifacts").mkdir(parents=True)
    subprocess.run(["git", "-C", str(root), "add", "--all"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(root), "commit", "-m", "init"], check=True,
                   capture_output=True)
    return root


@pytest.fixture()
def project(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_FRAMEWORK_HOME", str(tmp_path / "hf-home"))
    bootstrap()
    worktree = _worktree(tmp_path)
    parent = State.new(node_type="_orchestrator", base_dir=tmp_path / "runs",
                       project_id="prereg-contract", project_worktree=worktree)
    producer = State.new(node_type="hypothesis", base_dir=tmp_path / "producer-runs",
                         project_id="prereg-contract", project_worktree=worktree)
    return parent, producer


async def _task(parent: State) -> str:
    created = await execute_tool("task", parent, action="create", title="跑 H1 的正式测量")
    assert created["status"] == "success", created
    return created["task"]["id"]


async def _contract(parent: State, task_id: str, **kw) -> dict:
    return await execute_tool("task", parent, action="contract", task_id=task_id,
                              target_node="experiment", **kw)


def _revisions(parent: State) -> list:
    return TaskContractLog(parent.project_root / "tasks").all_revisions()


@pytest.mark.asyncio
async def test_an_id_only_binding_is_filled_from_the_frozen_head(project) -> None:
    parent, producer = project
    saved = producer.save_artifact("pre_registration", "h1", "# H1\n\nalpha=0.05\n")
    frozen = producer.mark_frozen(saved["id"])

    out = await _contract(parent, await _task(parent), prereg_artifact_id=saved["id"])

    assert out["status"] == "success", out
    assert out["prereg_assignment"] == ASSIGNMENT_EXACT
    assert out["prereg_binding"] == {
        "kind": ASSIGNMENT_EXACT, "artifact_id": saved["id"],
        "version": str(record_version(frozen)), "content_hash": frozen["content_hash"],
        "filled_from": "frozen_head",
    }
    # 落进合同账本的就是这三件 —— 子 run 读到的是完整绑定，不是只有 id。
    (rev,) = _revisions(parent)
    assert rev.assignment.version == str(record_version(frozen))
    assert rev.assignment.content_hash == frozen["content_hash"]


@pytest.mark.asyncio
async def test_a_never_frozen_prereg_is_refused_at_contract_time(project) -> None:
    parent, producer = project
    saved = producer.save_artifact("pre_registration", "h1", "# H1 draft\n")

    out = await _contract(parent, await _task(parent), prereg_artifact_id=saved["id"])

    assert out["status"] == "error", out
    assert out["error_code"] == "prereg_assignment_unresolved"
    assert out["reason_code"] == "never_frozen"
    assert out["missing"] == ["prereg_version", "prereg_content_hash"]
    assert _revisions(parent) == [], "拒了就不许留下一条半截合同"


@pytest.mark.asyncio
async def test_a_draft_over_a_frozen_version_is_not_guessed(project) -> None:
    """草稿压在旧冻结版上：两个答案都说得通，不替调用方选；点名版本后才写。"""
    parent, producer = project
    saved = producer.save_artifact("pre_registration", "h1", "# H1\n\nalpha=0.05\n")
    v1 = producer.mark_frozen(saved["id"])
    producer.save_artifact("pre_registration", "h1", "# H1\n\nalpha=0.01\n",
                           amendment_reason="收紧阈值")
    task_id = await _task(parent)

    refused = await _contract(parent, task_id, prereg_artifact_id=saved["id"])
    assert refused["status"] == "error", refused
    assert refused["reason_code"] == "head_is_unfrozen_draft"
    assert refused["frozen_versions"] == [
        {"version": record_version(v1), "content_hash": v1["content_hash"]}]
    assert _revisions(parent) == []

    named = await _contract(parent, task_id, prereg_artifact_id=saved["id"],
                            prereg_version=str(record_version(v1)))
    assert named["status"] == "success", named
    assert named["prereg_binding"]["content_hash"] == v1["content_hash"]
    assert named["prereg_binding"]["filled_from"] == "exact_version"


@pytest.mark.asyncio
async def test_a_binding_that_does_not_match_the_ledger_is_refused(project) -> None:
    parent, producer = project
    saved = producer.save_artifact("pre_registration", "h1", "# H1\n")
    frozen = producer.mark_frozen(saved["id"])
    task_id = await _task(parent)

    wrong_hash = await _contract(parent, task_id, prereg_artifact_id=saved["id"],
                                 prereg_version=str(record_version(frozen)),
                                 prereg_content_hash="0" * 64)
    assert wrong_hash["reason_code"] == "content_hash_mismatch", wrong_hash

    missing = await _contract(parent, task_id, prereg_artifact_id="pre_registration__nope")
    assert missing["reason_code"] == "not_found", missing

    assert _revisions(parent) == []

    exact = await _contract(parent, task_id, prereg_artifact_id=saved["id"],
                            prereg_version=str(record_version(frozen)),
                            prereg_content_hash=frozen["content_hash"])
    assert exact["status"] == "success", exact
    assert exact["prereg_binding"]["filled_from"] == "caller"


def test_the_assignment_type_itself_refuses_an_id_only_exact_binding() -> None:
    """不经工具直接构造也不行 —— exact 的定义就是三件一起。"""
    with pytest.raises(TaskContractError, match="version"):
        PreregAssignment.exact("pre_registration__H1")
    with pytest.raises(TaskContractError, match="content_hash"):
        PreregAssignment.exact("pre_registration__H1", version="1", content_hash="abc")
    ok = PreregAssignment.exact("pre_registration__H1", version=2, content_hash="A" * 64)
    assert (ok.version, ok.content_hash) == ("2", "a" * 64)

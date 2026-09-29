"""Project v2：读路径必须跟着写路径一起改。

写路径早就重定向到节点 Git 目录了（记录 = 节点目录里的原生文件 + 工作区账本），
读路径却还停在自己的 run 目录。P5 实测事故：literature 服务把 3 个产物写进
`literature/`，调用方 list_artifacts() 看到的却是空的，只好自己去
猜文件路径，最后判 incomplete。写得进读不出，等于没交付。
"""
from __future__ import annotations

import json
import os
import subprocess

import pytest

from core.ledger import LEDGER_RELATIVE
from core.project_workspace import bind_project_workspace
from core.state import State, StateContractError


def _worktree(tmp_path):
    root = tmp_path / "wt"
    root.mkdir()
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", "init"],
                   cwd=root, check=True, env=env)
    return root


def _bound(tmp_path, node_type, root):
    state = State(run_id=f"r-{node_type}", node_type=node_type,
                  root=tmp_path / f"run-{node_type}")
    bind_project_workspace(state, root)
    return state


def test_caller_sees_service_node_output(tmp_path):
    root = _worktree(tmp_path)
    literature = _bound(tmp_path, "literature", root)
    literature.save_artifact("literature_evidence_package", "mlip", "5 papers", {})

    orchestrator = _bound(tmp_path, "_orchestrator", root)
    listed = orchestrator.list_artifacts()
    assert [a["id"] for a in listed] == ["literature_evidence_package__mlip"]
    assert listed[0]["owner_node"] == "literature"

    record = orchestrator.read_artifact("literature_evidence_package__mlip")
    assert record is not None and record["content"] == "5 papers"


def test_type_filter_still_applies_across_nodes(tmp_path):
    root = _worktree(tmp_path)
    _bound(tmp_path, "literature", root).save_artifact("survey_report", "s", "body", {})
    _bound(tmp_path, "hypothesis", root).save_artifact("research_plan", "p", "plan", {})

    caller = _bound(tmp_path, "_orchestrator", root)
    assert [a["type"] for a in caller.list_artifacts("survey_report")] == ["survey_report"]
    assert len(caller.list_artifacts()) == 2


def test_another_nodes_record_cannot_be_shadowed_by_the_same_id(tmp_path):
    """同 id 跨节点碰撞：一个工作区一本账，同 id 就是同一份记录（路径即身份）。

    此前"本节点自己那份胜出"—— 那是两本账各存一份平行身份的产物。现在别的
    节点目录里的记录本节点不能静默覆盖：拒绝、指名所有者、给出路（换名 / 修订）；
    对方那份原样不动，列举也只有一份。
    """
    root = _worktree(tmp_path)
    _bound(tmp_path, "literature", root).save_artifact("survey_report", "x", "theirs", {})
    mine = _bound(tmp_path, "hypothesis", root)
    with pytest.raises(StateContractError) as exc:
        mine.save_artifact("survey_report", "x", "mine", {})
    assert "literature" in str(exc.value) and "amendment_reason" in str(exc.value)

    assert mine.read_artifact("survey_report__x")["content"] == "theirs"
    ids = [a["id"] for a in mine.list_artifacts()]
    assert ids.count("survey_report__x") == 1        # 不重复列
    # 换个 name 另起一份：落本节点自己的目录、盖本节点的章
    own = mine.save_artifact("survey_report", "x_mine", "mine", {})
    assert own["path"] == "plan/survey_report__x_mine.md"
    assert mine.read_artifact(own["id"])["produced_by_node_type"] == "hypothesis"


def test_missing_artifact_is_still_none(tmp_path):
    root = _worktree(tmp_path)
    caller = _bound(tmp_path, "_orchestrator", root)
    assert caller.read_artifact("nope__nope") is None
    assert caller.list_artifacts() == []


def test_corrupt_file_does_not_break_listing(tmp_path):
    """账本里一行坏数据不该让整个交接总线读不出来。"""
    root = _worktree(tmp_path)
    literature = _bound(tmp_path, "literature", root)
    literature.save_artifact("survey_report", "good", "body", {})
    with (root / LEDGER_RELATIVE).open("a", encoding="utf-8") as fh:
        fh.write("{ not json\n")
    caller = _bound(tmp_path, "_orchestrator", root)
    assert [a["id"] for a in caller.list_artifacts()] == ["survey_report__good"]


def test_non_v2_run_is_unchanged(tmp_path):
    """没绑 worktree（CLI / 老路径）→ 只看自己的 run 目录，行为一字不变。"""
    state = State(run_id="r", node_type="hypothesis", root=tmp_path / "run")
    state.save_artifact("research_plan", "p", "plan", {})
    [entry] = state.list_artifacts()
    assert entry["id"] == "research_plan__p"
    assert entry["owner_node"] == "hypothesis"
    assert state.find_artifact_path("research_plan__p").is_relative_to(state.root / "artifacts")


def test_artifact_missing_type_or_name_does_not_crash_listing(tmp_path):
    """issue #299：缺 type/name 的记录曾抛 KeyError 且未被捕获 —— 一条半截
    账本行能让整个列举崩掉。列举是观察，观察不该被坏数据打断。"""
    state = State(run_id="r", node_type="hypothesis", root=tmp_path / "run")
    state.save_artifact("pre_registration", "good", "body", {})
    (state.root / "artifacts" / "half_written__x.md").write_text(
        "只有正文，没有 type/name", encoding="utf-8")
    with (state.root / "records.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"event": "save", "id": "half_written__x",
                             "path": "half_written__x.md", "version": 1}) + "\n")

    listed = state.list_artifacts()
    ids = {a["id"] for a in listed}
    assert "pre_registration__good" in ids and "half_written__x" in ids
    broken = next(a for a in listed if a["id"] == "half_written__x")
    assert broken["type"] == "(unknown)"

    # 带类型过滤时也不炸
    assert [a["id"] for a in state.list_artifacts("pre_registration")] == [
        "pre_registration__good"]

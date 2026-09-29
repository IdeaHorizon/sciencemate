"""data 阶段欠着货就起 experiment —— 记一笔 stage_debt，不拦（issue #283 / #412）。

#283：用户明确说"先整理可复现数据包、不要提前计算结论"，orchestrator 仍直接起了
experiment。而 experiment 的机械必需输入只有 pre_registration —— 框架里没有任何
东西表达"本项目的 data 阶段还欠着"，于是只要预注册在，整条 data 审查链都能被跳过。

#412：data 连挂三次没发布 dataset，experiment 接手自己写了 generate_dataset.py，
把密度从冻结预注册的 0.5 漂成 0.7，LAMMPS 真跑通了 —— 一份不是 data 交付物、
且违反预注册的输入，产出了看起来合格的结果。

判据只用磁盘事实：data 跑过没有、dataset 在不在。"用户是不是要求先整理数据"是
自然语言，框架不猜。

判决拆除第三波（run_node:1312 降格）：曾经 return error 要求带 dataset_waiver_reason
重发；现在派发照跑，债的事实 + 理由（没写就是 not_declared）进 transcript 事件
`stage_debt`，随派发返回值带回。墙加回去，这里的用例转红。
"""
from __future__ import annotations

import json
import os
import subprocess

from core.bootstrap import bootstrap
from core.project_workspace import bind_project_workspace
from core.state import State
from shared.tools.run_node import _data_stage_debt

bootstrap()


def _worktree(tmp_path):
    root = tmp_path / "wt"
    root.mkdir()
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", "init"],
                   cwd=root, check=True, env=env)
    return root


def _caller(tmp_path, root, runs_dir, project_id="p"):
    (runs_dir / "orch").mkdir(parents=True, exist_ok=True)
    state = State(run_id="orch", node_type="_orchestrator",
                  root=runs_dir / "orch", project_id=project_id)
    bind_project_workspace(state, root)
    return state


def _data_run(runs_dir, name="1000-data", *, project_id="p", blockers=(),
              status="blocked"):
    d = runs_dir / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "summary.json").write_text(json.dumps({
        "node_type": "data", "project_id": project_id, "status": status,
        "missing_required_outputs": ["dataset"],
        "artifacts": [], "blockers": list(blockers),
    }), encoding="utf-8")
    return d


BLOCKER = {"category": "missing_capability",
           "summary": "preprocessing planning schema failure，无法生成可执行计划",
           "retryable_after_change": True}


def test_data_ran_but_delivered_nothing_is_a_debt(tmp_path):
    root = _worktree(tmp_path)
    runs_dir = tmp_path / "runs"
    _data_run(runs_dir, blockers=[BLOCKER])
    state = _caller(tmp_path, root, runs_dir)

    debt = _data_stage_debt(state, "experiment", {})
    assert debt is not None
    assert debt["kind"] == "data_stage_debt"
    assert "status" not in debt and "error" not in debt      # 事实，不是拒绝
    # data 最后报的是什么，必须摆出来 —— 调用方与 referee 要知道该修什么
    assert debt["last_data_blockers"][0]["summary"] == BLOCKER["summary"]
    assert debt["dataset_waiver_reason"] == "not_declared"


def test_a_delivered_dataset_clears_the_debt(tmp_path):
    root = _worktree(tmp_path)
    runs_dir = tmp_path / "runs"
    _data_run(runs_dir)
    state = _caller(tmp_path, root, runs_dir)
    producer = State(run_id="r-data", node_type="data", root=tmp_path / "run-data")
    (tmp_path / "run-data").mkdir(parents=True, exist_ok=True)
    bind_project_workspace(producer, root)
    producer.save_artifact("dataset", "lj_static", "包路径与哈希", {})

    assert _data_stage_debt(state, "experiment", {}) is None


def test_no_data_run_means_the_framework_does_not_plan_stages(tmp_path):
    """data 阶段根本不在场时没有债 —— 该不该有 data 阶段是规划判断，不是磁盘事实。"""
    root = _worktree(tmp_path)
    runs_dir = tmp_path / "runs"
    state = _caller(tmp_path, root, runs_dir)
    assert _data_stage_debt(state, "experiment", {}) is None


def test_a_stated_reason_is_recorded_with_the_debt(tmp_path):
    """理由不是出口，是账的一部分。"""
    root = _worktree(tmp_path)
    runs_dir = tmp_path / "runs"
    _data_run(runs_dir)
    state = _caller(tmp_path, root, runs_dir)
    debt = _data_stage_debt(
        state, "experiment",
        {"dataset_waiver_reason": "初始构型按预注册参数直接生成，不经 data 服务"})
    assert debt is not None
    assert debt["dataset_waiver_reason"] == "初始构型按预注册参数直接生成，不经 data 服务"


def test_blank_reason_is_not_declared(tmp_path):
    root = _worktree(tmp_path)
    runs_dir = tmp_path / "runs"
    _data_run(runs_dir)
    state = _caller(tmp_path, root, runs_dir)
    assert _data_stage_debt(
        state, "experiment", {"dataset_waiver_reason": "   "})["dataset_waiver_reason"] == "not_declared"


def test_other_nodes_are_untouched(tmp_path):
    root = _worktree(tmp_path)
    runs_dir = tmp_path / "runs"
    _data_run(runs_dir)
    state = _caller(tmp_path, root, runs_dir)
    for node_type in ("observation", "postprocess", "writing", "literature"):
        assert _data_stage_debt(state, node_type, {}) is None


def test_another_project_data_run_does_not_count(tmp_path):
    root = _worktree(tmp_path)
    runs_dir = tmp_path / "runs"
    _data_run(runs_dir, project_id="other")
    state = _caller(tmp_path, root, runs_dir, project_id="p")
    assert _data_stage_debt(state, "experiment", {}) is None


def test_the_debt_is_wired_into_dispatch_as_a_record_not_a_refusal():
    import inspect

    from shared.tools import run_node

    source = inspect.getsource(run_node._run_node_tool)
    assert "_data_stage_debt" in source
    assert 'append_transcript("stage_debt"' in source
    assert "return _data_debt" not in source


def test_the_escape_key_is_advertised_where_the_caller_reads():
    from core.tool_registry import get_tool

    definition = get_tool("run_node")
    assert definition is not None
    assert "dataset_waiver_reason" in json.dumps(
        definition.parameters_schema, ensure_ascii=False)

"""run_node 自动 forward required_input_artifact_types 测试。

根因修复：caller 不传 forward_artifact_ids 时，框架按子节点
required_input_artifact_types 自动从父 state 选最新 artifact 转发。

覆盖：
  - 自动 resolve happy path（每 type 唯一候选）
  - 同 type 多候选 → 选最新 + 报 ambiguous_candidates
  - 0 候选 → error 列出 parent 有什么 type
  - 显式 forward_artifact_ids → 完全按 caller，不自动补
  - required_input_artifact_types 为空 → 不 forward 任何 artifact
"""
from __future__ import annotations

import asyncio
import tempfile
import time
from pathlib import Path

from core.state import State
from shared.tools.run_node import _auto_resolve_required_inputs


def _make_state() -> State:
    return State.new(node_type="parent", base_dir=Path(tempfile.mkdtemp()))


def test_auto_resolve_happy_path():
    """3 个 required type，父 state 各有 1 个候选 → 自动选齐。"""
    state = _make_state()
    state.save_artifact("pre_registration", "h1", "prereg body")
    state.save_artifact("dataset", "ds1", "dataset body")
    state.save_artifact("survey_report", "lit", "survey body")

    auto_ids, ambig, missing = _auto_resolve_required_inputs(
        state, ["pre_registration", "dataset"],
    )
    assert len(auto_ids) == 2
    assert "pre_registration__h1" in auto_ids
    assert "dataset__ds1" in auto_ids
    assert ambig == {}
    assert missing == []


def test_ordering_survives_a_clock_that_does_not_move(monkeypatch):
    """时钟**完全不走**时，连续登记的产物仍然必须分得出先后。

    这是 CI 上 `test_auto_resolve_picks_latest_on_ambiguity` 偶发红的确定性版本。
    那条测试靠 `time.sleep(0.01)` 制造时间差，容器里时钟粒度一粗就并列。

    ⚠️ 判据必须**冻结时钟**，不能靠"连着写两次、不 sleep"来逼近：开发机的
    `datetime.now()` 精度足够高，连写两次也不会相等 —— 实测把单调保证整个拿掉，
    那种写法照样全绿。**一条只在低精度时钟上才会红的测试，如果自己依赖真实时钟，
    就只会在 CI 上红、在本机永远绿**，也就是现在这个病本身。
    """
    from datetime import datetime as _dt
    from datetime import timezone as _tz

    from core import state as state_mod

    frozen = _dt(2026, 8, 22, 10, 0, 0, tzinfo=_tz.utc)

    class _StoppedClock:
        @staticmethod
        def now(tz=None):
            return frozen

    monkeypatch.setattr(state_mod, "datetime", _StoppedClock)

    state = _make_state()
    state.save_artifact("pre_registration", "h_old", "old")
    state.save_artifact("pre_registration", "h_new", "new")

    auto_ids, ambig, _ = _auto_resolve_required_inputs(state, ["pre_registration"])
    assert auto_ids == ["pre_registration__h_new"], (
        "时钟不走时分不出先后 —— 时序在指望时钟精度，而不是写入方自己保证")
    assert ambig["pre_registration"][0] == "pre_registration__h_new"


def test_identical_timestamps_are_at_least_deterministic():
    """时间戳一模一样时，至少要**确定**（同样的盘面每次给同样的答案）。

    ⚠️ 这里只能断言确定性，**不能**断言"选到后写入的那份" —— 因为那个信息
    在时间戳相等时**根本不存在**。`State.list_artifacts` 的排序键
    第二元是 artifact_id，只保证"同秒登记时顺序仍然确定"，不保证
    正确：字典序里 `h_new` < `h_old`，于是后写入的 h_new 反而被判成更旧。

    要真正保证写入序，产物记录需要一个**由写入方单调发放的序号**（时间戳不是
    序号，时钟粒度一粗就并列），那是独立的一件事，不在本 PR 范围内。
    记在这里，免得下次有人拿"选最新"当已保证的语义去依赖它。
    """
    import json

    state = _make_state()
    state.save_artifact("pre_registration", "h_old", "old")
    state.save_artifact("pre_registration", "h_new", "new")

    # 把两条账本行的登记时刻改成一模一样（模拟时钟粒度粗到并列）
    frozen = "2026-08-22T10:00:00+00:00"
    ledger = state.root / "records.jsonl"
    rows = [json.loads(line) for line in
            ledger.read_text(encoding="utf-8").splitlines() if line.strip()]
    for row in rows:
        if row.get("event") == "save" and str(row.get("id", "")).startswith("pre_registration__"):
            row["created_at"] = frozen
    ledger.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
                      encoding="utf-8")

    first = _auto_resolve_required_inputs(state, ["pre_registration"])
    for _ in range(5):
        assert _auto_resolve_required_inputs(state, ["pre_registration"]) == first, \
            "同样的盘面给出了不同的答案 —— 选料不确定"
    # 候选必须齐全，不能因为并列而丢掉一个
    assert len(first[1]["pre_registration"]) == 2


def test_auto_resolve_picks_latest_on_ambiguity():
    """同 type 多个候选 → 选最新 + 把候选都列出来。"""
    state = _make_state()
    state.save_artifact("pre_registration", "h_old", "old")
    time.sleep(0.01)  # 保证 created_at 不同
    state.save_artifact("pre_registration", "h_new", "new")

    auto_ids, ambig, missing = _auto_resolve_required_inputs(
        state, ["pre_registration"],
    )
    assert len(auto_ids) == 1
    assert auto_ids[0] == "pre_registration__h_new"   # 最新
    assert "pre_registration" in ambig
    assert len(ambig["pre_registration"]) == 2
    # 候选按 created_at 倒序
    assert ambig["pre_registration"][0] == "pre_registration__h_new"
    assert missing == []


def test_auto_resolve_zero_candidates_reports_missing():
    """父 state 缺某 type → 进 missing 列表。"""
    state = _make_state()
    state.save_artifact("dataset", "ds1", "x")

    auto_ids, ambig, missing = _auto_resolve_required_inputs(
        state, ["pre_registration", "dataset"],
    )
    assert "pre_registration" in missing
    # dataset 仍然 resolve 成功（部分成功）
    assert any("dataset" in a for a in auto_ids)


def test_auto_resolve_empty_required_returns_empty():
    """子节点 required_input_artifact_types=[] → 不 forward 任何东西。"""
    state = _make_state()
    state.save_artifact("foo", "bar", "x")
    auto_ids, ambig, missing = _auto_resolve_required_inputs(state, [])
    assert auto_ids == []
    assert ambig == {}
    assert missing == []


# ── 端到端：run_node 工具调用 ────────────────────────────────────────────

def test_run_node_auto_forwards_required_inputs(monkeypatch):
    """模拟父调子，验证不传 forward_artifact_ids 时框架自动 forward。"""
    from core.bootstrap import bootstrap
    bootstrap()
    from core.tool_registry import execute
    from unittest.mock import AsyncMock, MagicMock

    state = _make_state()
    state.depth = 0
    state.hook_state["_callable_nodes"] = ["literature"]
    state.save_artifact("survey_report", "fake", "fake survey")  # literature 不要 input

    # mock execute_node 不真跑 LLM，只验证 upstream_artifacts 被正确传入
    captured = {}

    async def fake_execute_node(**kw):
        captured["upstream_artifacts"] = kw.get("upstream_artifacts")
        return {
            "run_id": "fake_child_run",
            "node_type": kw["node_type"],
            "project_id": None,
            "status": "completed",
            "missing_required_outputs": [],
            "turns": 1,
            "tool_call_count": 0,
            "artifacts": [],
            "final_text_preview": "",
            "state_dir": str(Path(tempfile.mkdtemp())),
            "project_root": None,
            "depth": 1,
            "sub_run_id": "test",
        }

    import core.executor
    monkeypatch.setattr(core.executor, "execute_node", fake_execute_node)
    import shared.tools.run_node as rn
    monkeypatch.setattr("core.llm.LLMClient", lambda: MagicMock())

    # literature 节点 required_input_artifact_types=[]，所以应该不 forward 任何东西
    # 用节点声明的输入键（v2.1 起 run_node 在派发处机械校验输入契约）
    result = asyncio.run(execute("run_node", state, node_type="literature", user_note="测试派发",
                                    node_inputs={"research_question": "x"}))
    assert result["status"] == "success"
    assert captured["upstream_artifacts"] == []   # literature 不需要任何输入


def test_run_node_explicit_forward_bypasses_auto(monkeypatch):
    """显式传 forward_artifact_ids 时不走自动 resolve。"""
    from core.bootstrap import bootstrap
    bootstrap()
    from core.tool_registry import execute
    from unittest.mock import AsyncMock, MagicMock

    state = _make_state()
    state.depth = 0
    state.hook_state["_callable_nodes"] = ["literature"]
    state.save_artifact("survey_report", "explicit_one", "explicit body")
    state.save_artifact("survey_report", "auto_would_pick_this", "auto body")

    captured = {}

    async def fake_execute_node(**kw):
        captured["upstream_artifacts"] = kw.get("upstream_artifacts")
        return {
            "run_id": "r", "node_type": kw["node_type"], "project_id": None,
            "status": "completed", "missing_required_outputs": [],
            "turns": 1, "tool_call_count": 0, "artifacts": [],
            "final_text_preview": "", "state_dir": str(Path(tempfile.mkdtemp())),
            "project_root": None, "depth": 1, "sub_run_id": "t",
        }

    import core.executor
    monkeypatch.setattr(core.executor, "execute_node", fake_execute_node)
    import shared.tools.run_node as rn
    monkeypatch.setattr("core.llm.LLMClient", lambda: MagicMock())

    result = asyncio.run(execute("run_node", state, node_type="literature", user_note="测试派发",
                                    node_inputs={},
                                    forward_artifact_ids=["survey_report__explicit_one"]))
    assert result["status"] == "success"
    fwd_names = [a["name"] for a in captured["upstream_artifacts"]]
    assert fwd_names == ["explicit_one"]    # 只有显式那个，没有 auto


def _bound_state(tmp_path):
    """绑真 worktree 的父 state（判决拆除第三波：legacy 无 worktree 的转发分支已删，
    生产入口都绑 worktree，测试跟着绑）。"""
    import os
    import subprocess

    from core.project_workspace import bind_project_workspace

    wt = tmp_path / "wt"
    wt.mkdir()
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    subprocess.run(["git", "init", "-q"], cwd=wt, check=True)
    subprocess.run(["git", "commit", "-q", "--allow-empty", "-m", "init"],
                   cwd=wt, check=True, env=env)
    state = State.new(node_type="_orchestrator", base_dir=tmp_path / "runs", project_id="pf")
    bind_project_workspace(state, wt)
    return state


def _a_task_for(state) -> str:
    """建一个任务 + 一条合同，返回 task_instance_uuid（#1080 之后的派发前置）。"""
    from core.task_contract import TaskContractLog
    from core.tasks import TaskList

    tl = TaskList(state.project_root / "tasks")
    t = tl.create("测试任务", "", "experiment", state.run_id)
    TaskContractLog(state.project_root / "tasks").append(
        task_instance_uuid=t.task_instance_uuid, objective="测试任务", actor="test")
    return t.task_instance_uuid


def test_run_node_missing_required_input_is_not_a_refusal(monkeypatch, tmp_path):
    """父 worktree 缺 required type → **不拦**，子节点如实产降级产物（#522 的判据：
    零个候选是合法局面）。判决拆除第三波：legacy 分支对同一问题答「缺必需类型→
    拒绝派发」，与 v2 的「零个→不拦」是同一问题两答案 —— legacy 整段删，只剩一答。
    墙加回去这条转红。

    **节点名现扫，不写死。** `required_input_artifact_types` 是各节点自己的契约，
    这里注入一份道具契约，验的是框架行为。
    """
    import dataclasses

    from core.bootstrap import bootstrap
    bootstrap()
    from core import loader as _loader
    from core.tool_registry import execute
    from unittest.mock import MagicMock

    victim, missing, present = "experiment", "pre_registration", "dataset"

    # 契约**注入**，不借用生产节点声明的那份：验的是框架这道门，节点声明只是
    # 道具。原来借 experiment 真实的 `required_input: [pre_registration]`，
    # 2026-08-13 experiment 把 prereg 挪进运行时硬门（诊断/构建 run 本来就不
    # 该被一刀切要求）之后，这条测试当场红 —— 报的还是
    # "object MagicMock can't be used in 'await' expression"：门没拦住，run 一路
    # 跑到真去调 LLM 才炸。**报错指向假原因**，看着像节点把框架搞坏了。
    _real_load = _loader.load_harness

    def _load(node_type, *a, **kw):
        harness = _real_load(node_type, *a, **kw)
        if node_type == victim:
            return dataclasses.replace(
                harness, required_input_artifact_types=[missing]
            )
        return harness

    monkeypatch.setattr(_loader, "load_harness", _load)

    state = _bound_state(tmp_path)
    state.depth = 0
    state.hook_state["_callable_nodes"] = [victim]
    state.save_artifact(present, "only_thing", "x")

    captured = {}

    async def fake_execute_node(**kw):
        captured["selected_input_ids"] = kw.get("selected_input_ids")
        captured["upstream_artifacts"] = kw.get("upstream_artifacts")
        return {
            "run_id": "r", "node_type": kw["node_type"], "project_id": "pf",
            "status": "completed", "missing_required_outputs": [],
            "turns": 1, "tool_call_count": 0, "artifacts": [],
            "final_text_preview": "", "state_dir": str(Path(tempfile.mkdtemp())),
            "project_root": None, "depth": 1, "sub_run_id": "t",
        }

    async def no_flow(*a, **k):
        return None

    import core.executor
    monkeypatch.setattr(core.executor, "execute_node", fake_execute_node)
    monkeypatch.setattr("shared.tools.run_node._run_post_producing_flow", no_flow)
    monkeypatch.setattr("core.llm.LLMClient", lambda: MagicMock())

    # 派 experiment 现在必须带任务身份（#1080 第 3 条）：这一趟在做哪件事不能靠
    # 子节点去扫项目现状认领。这条测试验的是别的东西，所以前置照最短路径建。
    _uuid = _a_task_for(state)
    result = asyncio.run(execute("run_node", state, node_type=victim, user_note="测试派发",
                                    node_inputs={"experiment_spec": "x"},
                                    task_instance_uuid=_uuid))
    assert result["status"] == "success", result
    assert "MagicMock" not in str(result)
    # 没人能替子节点选到一份不存在的输入：选择为空、文件也不搬
    assert not captured["selected_input_ids"]
    assert captured["upstream_artifacts"] == []

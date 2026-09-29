"""任务身份要跟着派发走，续跑要按它挑（#1080 第 3/4/5 条）。

派发链上此前一个任务字段都没有：`run_node` 收的 `task_id` 只用来改 Task 状态和记
父 run 的 flow，`execute_node` 的参数、child State、`run_start` 事件里都没有它。
于是节点判"我这趟在做哪件事"只能去扫「此刻项目里有几份预注册」—— 而续跑、接管、
上游重派这三类跨 run 场景下，那个答案随时会变。
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from core.bootstrap import bootstrap
from core.state import State
from core.task_contract import TaskContractLog
from core.tasks import TaskList
from shared.tools.run_node import (
    TASK_IDENTITY_REQUIRED_NODES,
    _resolve_task_identity,
    _resumable_run_for,
    task_identity_mismatch,
)


def _state(tmp_path: Path) -> State:
    bootstrap()
    st = State.new(node_type="_orchestrator", base_dir=tmp_path / "runs", project_id="p")
    st.project_root = tmp_path / "project"
    (st.project_root / "tasks").mkdir(parents=True, exist_ok=True)
    return st


def _a_task_with_contract(st: State):
    tl = TaskList(st.project_root / "tasks")
    t = tl.create("测 2D Ising 的 Tc", "", "experiment", "r-parent")
    rev = TaskContractLog(st.project_root / "tasks").append(
        task_instance_uuid=t.task_instance_uuid, objective="测 Tc", actor="orch")
    return t, rev


# ── 验收 3：生产 Experiment 派发缺身份 → 明确错误 ──────────────────────────


def test_dispatching_experiment_without_a_task_is_refused(tmp_path: Path) -> None:
    st = _state(tmp_path)
    identity, err = _resolve_task_identity(st, "experiment", None, None)
    assert identity is None
    assert err and err["error_code"] == "task_identity_required"
    # 报错要给出**正确答案**，否则调用方只能猜（而它猜的方向就是别派了）
    assert "task(action='create'" in err["error"]
    assert "task_instance_uuid" in err["error"]


@pytest.mark.parametrize("node_type", sorted(TASK_IDENTITY_REQUIRED_NODES))
def test_the_three_self_claiming_nodes_all_require_it(node_type, tmp_path: Path) -> None:
    """这三个节点今天都会从全项目冻结 prereg 里自行认领（#1097 §3）。"""
    _identity, err = _resolve_task_identity(_state(tmp_path), node_type, None, None)
    assert err and err["error_code"] == "task_identity_required"


def test_other_nodes_are_not_blocked(tmp_path: Path) -> None:
    """literature / writing 这些不认领 prereg 的照常派 —— 别把一条缺省改成硬失败。"""
    identity, err = _resolve_task_identity(_state(tmp_path), "literature", None, None)
    assert err is None and identity is None


def test_a_dangling_task_uuid_is_refused(tmp_path: Path) -> None:
    """带了身份就必须找得到那份合同 —— 引用一份不存在的授权不是"先放行再说"。"""
    st = _state(tmp_path)
    _identity, err = _resolve_task_identity(st, "experiment", "no-such-uuid", None)
    assert err and err["error_code"] == "task_contract_missing"


def test_a_forked_contract_must_be_named_exactly(tmp_path: Path) -> None:
    """有两版合同时不替调用方抽签。"""
    st = _state(tmp_path)
    t, rev = _a_task_with_contract(st)
    log = TaskContractLog(st.project_root / "tasks")
    second = log.append(task_instance_uuid=t.task_instance_uuid, objective="改了主意",
                        actor="orch", parent_revision_digest=rev.digest)

    _identity, err = _resolve_task_identity(st, "experiment", t.task_instance_uuid, None)
    assert err and err["error_code"] == "task_contract_ambiguous"

    identity, err = _resolve_task_identity(
        st, "experiment", t.task_instance_uuid, second.digest)
    assert err is None
    assert identity["task_contract_digest"] == second.digest
    assert identity["task_contract_revision"] == 2


# ── 验收 4：四个字段逐字进 child State 与 run_start ────────────────────────


def test_the_identity_reaches_the_child_state_and_run_start(tmp_path: Path) -> None:
    import asyncio
    from unittest.mock import patch

    from core.executor import execute_node

    bootstrap()
    captured: dict = {}

    async def _fake_run_loop(harness, state, messages, llm):
        captured["state"] = state
        raise RuntimeError("stop here — 只验证入口装配")

    with patch("core.executor.run_loop", _fake_run_loop):
        try:
            asyncio.run(execute_node(
                "_curator", state_dir=tmp_path / "runs", project_id=None,
                task_instance_uuid="uuid-1",
                task_contract_revision=3,
                task_contract_digest="digest-1",
                parent_dispatch_id="disp_abc",
            ))
        except Exception:
            pass

    child = captured["state"]
    assert child.task_instance_uuid == "uuid-1"
    assert child.task_contract_revision == 3
    assert child.task_contract_digest == "digest-1"
    assert child.parent_dispatch_id == "disp_abc"

    run_start = next(
        json.loads(line) for line
        in child.transcript_path.read_text(encoding="utf-8").splitlines()
        if line.strip() and json.loads(line)["event"] == "run_start")
    assert run_start["task_instance_uuid"] == "uuid-1"
    assert run_start["task_contract_revision"] == 3
    assert run_start["task_contract_digest"] == "digest-1"
    assert run_start["parent_dispatch_id"] == "disp_abc", (
        "审计接不上「父这边的哪一次调用」和「子那边的哪一个 run」")


def test_the_parent_records_the_same_dispatch_id() -> None:
    """父 run 那条 `subagent_call_start` 要带同一个 dispatch id。"""
    import inspect

    import shared.tools.run_node as run_node_mod

    src = inspect.getsource(run_node_mod)
    at = src.index('"subagent_call_start"')
    assert "dispatch_id=_dispatch_id" in src[at:at + 900], (
        "父 run 上没记这次派发的身份 —— child 的 parent_dispatch_id 无从对上")


# ── 验收 5：续跑按身份挑 ───────────────────────────────────────────────────


def _interrupted(uuid: str | None, digest: str | None, revision: int | None = 1) -> dict:
    return {"run_id": "run_x", "node_type": "experiment", "has_checkpoint": True,
            "session_id": "s1", "n_tool_calls": 3,
            "task_instance_uuid": uuid, "task_contract_digest": digest,
            "task_contract_revision": revision}


def test_a_run_serving_another_task_is_not_resumed(tmp_path: Path, monkeypatch) -> None:
    st = _state(tmp_path)
    st.session_id = "s1"
    monkeypatch.setattr(
        "shared.tools.run_node.scan_interrupted_child_runs",
        lambda state, scope="all": [_interrupted("other-uuid", "other-digest")])

    identity = {"task_instance_uuid": "mine", "task_contract_digest": "mine-digest",
                "task_contract_revision": 1}
    assert _resumable_run_for(st, "experiment", task_identity=identity) is None, (
        "把另一件事的尸体当成这件事的半成品续上了 —— #1052 那条翼型会话的形状")

    same = {"task_instance_uuid": "other-uuid", "task_contract_digest": "other-digest",
            "task_contract_revision": 1}
    assert _resumable_run_for(st, "experiment", task_identity=same) is not None


def test_a_dispatch_without_identity_still_resumes(tmp_path: Path, monkeypatch) -> None:
    """对照：没带身份的派发（CLI、老 run）维持原样 —— 否则历史 run 永远续不上。"""
    st = _state(tmp_path)
    st.session_id = "s1"
    monkeypatch.setattr(
        "shared.tools.run_node.scan_interrupted_child_runs",
        lambda state, scope="all": [_interrupted(None, None, None)])
    assert _resumable_run_for(st, "experiment", task_identity=None) is not None


def test_the_mismatch_is_explained_field_by_field() -> None:
    mine = {"task_instance_uuid": "u1", "task_contract_revision": 2,
            "task_contract_digest": "d2"}
    assert task_identity_mismatch(_interrupted("u1", "d2", 2), mine) is None
    assert "任务身份对不上" in task_identity_mismatch(_interrupted("u9", "d2", 2), mine)
    assert "合同版本对不上" in task_identity_mismatch(_interrupted("u1", "d2", 1), mine)
    assert "合同摘要对不上" in task_identity_mismatch(_interrupted("u1", "d9", 2), mine)
    assert task_identity_mismatch(_interrupted("u1", "d2", 2), None) is None, (
        "本次派发没带身份时不比 —— 不比和比过了是两回事")


def test_an_explicit_resume_to_a_foreign_run_is_an_error() -> None:
    """显式 resume_run_id 指向对不上的 run：报错，既不静默新开也不静默续上。"""
    import inspect

    import shared.tools.run_node as run_node_mod

    src = inspect.getsource(run_node_mod._run_node_tool)
    assert "resume_task_identity_mismatch" in src
    at = src.index("resume_task_identity_mismatch")
    assert "fresh" in src[at:at + 800], "报错没给出合法出口"

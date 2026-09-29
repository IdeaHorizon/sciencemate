"""跨节点回放：Analysis 冻结的 prereg，experiment 那边真的过得去吗。

单测各自绿不代表链路通 —— #324 就是例子：run_role 补上了，结果把局面从
"跑得动但不算数"变成"根本跑不动"（experiment 的 preflight 对 primary run
要求 expected_params，而当时没有任何节点写它，safe_bash.py:3108 直接硬拒）。

**这个文件自己也栽过同一个坑。** 它最初用一个假 State，把 prereg 直接放进
experiment 的记录目录 —— 那正好迎合了 `load_run_contract` 当时"glob
自己目录"的写法，于是一直绿。而真实布局是 prereg 在 `plan/`，
experiment 永远读不到（E2E v14/v15/v16 三轮的真实死因）。测试在验证一个
生产中不存在的场景。

所以现在用**真 Git worktree** + 真的双节点目录布局：预注册是 `plan/` 下的原生
文件，出处与冻结在工作区账本（`core/ledger`）里。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from core.ledger import write_record
from core.project_workspace import _NODE_WORKSPACES as _DIRS  # 节点 → 目录
from core.project_workspace import bind_project_workspace
from core.state import State
from shared.tools.library.artifacts_extra import _freeze_artifact

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "nodes" / "experiment"))
from tools.preflight import audit_execution_contract  # noqa: E402
from tools.run_contract import resolve_run_acceptance  # noqa: E402

NODES = ("literature", "hypothesis", "data", "experiment", "postprocess", "writing")

PARAMS = {"n_particles": 4000, "timestep": 0.005, "T_start": 2.0}

_STUB_PREREG = (
    "## Research Questions\n\n### Q1: stub question for freeze-path tests\n"
    "- output_kind: 一个数\n```yaml\n- statement: \"stub closure condition\"\n```\n"
)


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def _worktree(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.name", "Test Platform")
    _git(root, "config", "user.email", "platform@example.test")
    (root / "project.yaml").write_text("schema_version: 2\nname: Test\n", encoding="utf-8")
    for node in NODES:
        (root / _DIRS[node]).mkdir(parents=True)
        (root / _DIRS[node] / "README.md").write_text(f"# {node}\n", encoding="utf-8")
    _git(root, "add", "--all")
    _git(root, "commit", "-m", "Initialize Project")
    return root


def _node_state(tmp_path: Path, root: Path, node_type: str, run_id: str) -> State:
    state = State(run_id=run_id, node_type=node_type, root=tmp_path / f"runstate-{run_id}")
    bind_project_workspace(state, root)
    return state


def _draft_prereg(root: Path) -> None:
    """Analysis 写的草稿：落 `plan/`，账本记出处，尚未冻结。"""
    write_record(root, artifact_type="pre_registration", name="X", content=_STUB_PREREG,
                 directory=_DIRS["hypothesis"], metadata={},
                 produced_by_node_type="hypothesis", produced_by_run_id="h0")


async def _freeze(tmp_path: Path, root: Path, **kwargs) -> dict:
    """走 Analysis 的真入口冻结，产物在 plan/，冻结是账本上的一行。"""
    return await _freeze_artifact(
        state=_node_state(tmp_path, root, "hypothesis", "h1"),
        artifact_id="pre_registration__X",
        **kwargs,
    )


def _accept_bound(state: State, artifact_id: str, frozen: dict) -> dict:
    """派发方指名这一版 prereg，铸出本 run 的 write-once acceptance。

    #1099 之后 catalog 不再拥有 assignment authority：目录里躺着一份冻结
    prereg，不等于这个 run 被绑到了它身上。authority 只有一个来源 —— 派发方
    的 typed `prereg_assignment`，冻结进本 run 的 acceptance 收据。夹具不提供
    它，experiment 看到的就是「未绑定」，参数门根本不进 applicable 分支 ——
    那正是这三条用例曾经的红法，不是门松了。

    caller 侧不写 `source`；收据 writer 会规范化成 `explicit_node_input`。
    """
    state.hook_state["node_inputs"] = {
        "experiment_focus": "Exercise the exact frozen prereg contract.",
        "prereg_assignment": {
            "kind": "bound",
            "artifact_id": artifact_id,
            "version": int(frozen["version"]),
            "content_hash": frozen["content_hash"],
        },
    }
    accepted = resolve_run_acceptance(state, bind_if_absent=True, requested_mode="scientific")
    assert accepted["passed"] is True, accepted
    assert accepted["receipt"]["prereg_assignment"]["kind"] == "bound", accepted
    return accepted


@pytest.mark.asyncio
async def test_frozen_prereg_lets_a_primary_simulation_through(tmp_path: Path) -> None:
    """Analysis 按契约冻结 → experiment 放行。这条通了，链路才算接上。"""
    root = _worktree(tmp_path)
    _draft_prereg(root)
    result = await _freeze(tmp_path, root, run_role="primary",
                           analysis_eligible=True, expected_params=PARAMS)
    assert result["status"] == "success"

    state = _node_state(tmp_path, root, "experiment", "e1")
    frozen = state.latest_frozen_artifact("pre_registration__X")
    assert frozen is not None
    _accept_bound(state, "pre_registration__X", frozen)

    audit = audit_execution_contract(state, PARAMS, stage="simulation")
    assert audit["passed"] is True, audit
    # `applicable` 必须一起断言 —— 没有 exact assignment 时
    # requires_hypothesis_verdict 为 false，audit 会返回 secondary/不适用的
    # passed=true。那种绿说明链路**没**接上，而它和真的接上长得一模一样。
    assert audit["applicable"] is True, audit
    assert not audit.get("blocking_reasons")


@pytest.mark.asyncio
async def test_parameter_drift_is_still_blocked(tmp_path: Path) -> None:
    """别为了让链路通就把防挑数据的门弄松了：实际参数偏离照样拒。"""
    root = _worktree(tmp_path)
    _draft_prereg(root)
    await _freeze(tmp_path, root, run_role="primary",
                  analysis_eligible=True, expected_params=PARAMS)

    state = _node_state(tmp_path, root, "experiment", "e2")
    frozen = state.latest_frozen_artifact("pre_registration__X")
    assert frozen is not None
    _accept_bound(state, "pre_registration__X", frozen)

    audit = audit_execution_contract(
        state, {**PARAMS, "n_particles": 8000}, stage="simulation")
    assert audit["passed"] is False
    assert audit["applicable"] is True, audit
    assert "execution_params_mismatch" in audit.get("blocking_reasons", [])


def test_a_primary_prereg_without_params_would_have_been_blocked(tmp_path: Path) -> None:
    """回放 #324 单独上线时的局面 —— 这条红了说明这层保护没了。

    直接落一份"声明 primary 但没有 expected_params"的冻结记录（现在的冻结门禁
    已不让这样冻出来，夹具绕过工具直接写账本），确认 experiment 侧确实硬拒。
    """
    root = _worktree(tmp_path)
    frozen = write_record(
        root, artifact_type="pre_registration", name="X", content=_STUB_PREREG,
        directory=_DIRS["hypothesis"],
        metadata={"run_role": "primary", "analysis_eligible": True},
        produced_by_node_type="hypothesis", produced_by_run_id="h0", frozen=True)

    state = _node_state(tmp_path, root, "experiment", "e3")
    _accept_bound(state, frozen["id"], frozen)

    audit = audit_execution_contract(state, PARAMS, stage="simulation")
    assert audit["passed"] is False
    assert audit["applicable"] is True, audit
    assert "expected_params_missing" in audit.get("blocking_reasons", [])

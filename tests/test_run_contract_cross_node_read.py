"""experiment 必须读得到 **Analysis 冻结的** pre_registration。

v2.1 workspace-first 之后每个节点只拥有自己的目录：预注册落在 `plan/`，而
experiment 的记录目录是 `experiments/`。`load_run_contract` 原本直接 glob 自己
的目录 —— **永远找不到预注册**，`contract_source` 恒为 "default"，于是每个实验
run 都被记成 secondary，verdict 只能 inconclusive。

E2E v14 / v15 / v16 三轮全栽在这上面。前两轮我误判成"Analysis 没声明"，补了
声明（run_role / expected_params）之后 v16 依旧 secondary —— 因为声明得再全，
消费方也看不见。

2026-09-21（#979）：断言改读 `requires_hypothesis_verdict`。原先这三处直接下标取
`contract["analysis_eligible"]`，而那个字段 2026-09-11 已从 experiment 的目标契约
删除（`nodes/experiment/AGENTS.md:143`），节点为了不让根测试长红，保留了一个派生
只读别名 —— 语义完全等价（两者都是 `execution_mode == "scientific" and
run_role == "primary"`），只差一个已被删掉的降格效应。根测试不改，那个别名就永远
删不掉：**别人的兼容层是被我们的断言钉在那里的**。

v2.1 的规矩是"读跨节点、写不跨节点"，跨节点读的正规入口是
`state.list_artifacts()`（默认扫全 worktree 账本）+ `read_artifact()`。

夹具照现行记录模型：正文是节点目录下的原生文件，类型 / 出处 / 冻结进工作区
账本（`core/ledger`）；"冻结"是账本上的一行，不是 metadata 里的一个字段。
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

import pytest

from core.ledger import write_record
from core.project_workspace import _NODE_WORKSPACES as _DIRS  # 节点 → 目录
from core.project_workspace import bind_project_workspace
from core.state import State

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "nodes" / "experiment"))
from tools.run_contract import (  # noqa: E402
    _classify_experiment_scope,
    load_run_contract,
    resolve_run_acceptance,
)

NODES = ("literature", "hypothesis", "data", "experiment", "postprocess", "writing")

PARAMS = {"n_particles": 1000, "timestep": 0.005}


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


def _write_prereg(root: Path, **metadata) -> None:
    """Analysis 产出的预注册：落 `plan/`，账本记出处；`frozen=True` 再追一行 freeze。"""
    frozen = bool(metadata.pop("frozen", False))
    write_record(root, artifact_type="pre_registration", name="X", content="H1: ...",
                 directory=_DIRS["hypothesis"], metadata=metadata,
                 produced_by_node_type="hypothesis", produced_by_run_id="r-hyp",
                 frozen=frozen)


def _experiment_state(tmp_path: Path, root: Path) -> State:
    state = State(run_id="r1", node_type="experiment", root=tmp_path / "runstate")
    bind_project_workspace(state, root)
    return state


def _accept(state: State, node_inputs: dict) -> dict:
    """铸出本 run 的 write-once acceptance 收据。

    #1099 之后 catalog 只提供**候选可见性**，不提供 assignment authority：工作区
    里躺着冻结 prereg（一份也好两份也好）都不会让这个 run 绑上去。绑定只能来自
    派发方的 typed `prereg_assignment`。
    """
    state.hook_state["node_inputs"] = node_inputs
    accepted = resolve_run_acceptance(state, bind_if_absent=True, requested_mode="scientific")
    assert accepted["passed"] is True, accepted
    return accepted


def _accept_bound(state: State, artifact_id: str) -> dict:
    frozen = state.latest_frozen_artifact(artifact_id)
    assert frozen is not None, f"{artifact_id} 还没有冻结版"
    return _accept(state, {
        "experiment_focus": "跑派发方指名的这一版 prereg。",
        "prereg_assignment": {
            "kind": "bound",
            "artifact_id": artifact_id,
            "version": int(frozen["version"]),
            "content_hash": frozen["content_hash"],
        },
    })


def test_contract_is_read_from_the_analysis_node_directory(tmp_path: Path) -> None:
    root = _worktree(tmp_path)
    _write_prereg(root, frozen=True, run_role="primary",
                  expected_params=PARAMS)

    # 指名的三元组来自 `plan/` 里那份 artifact —— 跨节点**读**得到，才谈得上绑得上。
    state = _experiment_state(tmp_path, root)
    _accept_bound(state, "pre_registration__X")
    contract = load_run_contract(state)

    assert contract["run_role"] == "primary"
    assert contract["requires_hypothesis_verdict"] is True
    assert contract["expected_params"] == PARAMS
    # 这条是核心：来源必须是那份 artifact，不是兜底默认
    assert contract["contract_source"].startswith("artifact:"), contract["contract_source"]


def test_experiments_own_directory_never_holds_the_prereg(tmp_path: Path) -> None:
    """守住前提：这条如果哪天不成立了，上面那条就测不到东西了。"""
    root = _worktree(tmp_path)
    _write_prereg(root, frozen=True, run_role="primary",
                  expected_params=PARAMS)
    state = _experiment_state(tmp_path, root)

    assert Path(state.records_dir) == root / "experiments"
    assert not list(Path(state.records_dir).glob("pre_registration__*"))
    # 预注册的正文在 Analysis 的目录里 —— 读到它只可能是跨节点读
    assert state.find_artifact_path("pre_registration__X").parent == root / "plan"


def test_unfrozen_prereg_does_not_grant_primary(tmp_path: Path) -> None:
    """草稿的声明不作数 —— 冻结才是承诺，否则可以事后改了再宣称 primary。"""
    root = _worktree(tmp_path)
    _write_prereg(root, run_role="primary", expected_params=PARAMS)

    contract = load_run_contract(_experiment_state(tmp_path, root))

    assert contract["run_role"] == "secondary"
    assert contract["requires_hypothesis_verdict"] is False


def test_no_prereg_still_falls_back_to_secondary(tmp_path: Path) -> None:
    """没有预注册时保持 fail-closed：不能因为读不到就当成正式实验。"""
    root = _worktree(tmp_path)

    contract = load_run_contract(_experiment_state(tmp_path, root))

    assert contract["run_role"] == "secondary"
    assert contract["requires_hypothesis_verdict"] is False


def _write_named_prereg(root: Path, name: str, **metadata) -> None:
    write_record(root, artifact_type="pre_registration", name=name, content="H1: ...",
                 directory=_DIRS["hypothesis"], metadata=metadata,
                 produced_by_node_type="hypothesis", produced_by_run_id="r-hyp",
                 frozen=True)


def _two_frozen(tmp_path: Path):
    """跨 session 继承的旧 prereg + 本轮新冻的 —— v20 的真实局面。"""
    root = _worktree(tmp_path)
    _write_named_prereg(root, "Cooling_Rate_Tg", run_role="primary",
                        expected_params={"cooling_steps": [38000, 380000, 3800000]})
    _write_named_prereg(root, "Cooling_Rate_Tg_v11_lowT", run_role="primary",
                        expected_params={"cooling_steps": [38000, 120253, 380000]})
    return root


def test_a_single_frozen_prereg_is_still_not_an_auto_claim(tmp_path: Path) -> None:
    """只有一份也不自动认领（#1099 废止旧的 sole-frozen auto-claim）。

    「无歧义就直接认领」省掉的那一步，正是**谁决定这个 run 受哪份约束**。省掉之后
    authority 落在 catalog 里：上游往 `plan/` 放一份冻结 prereg，下游的 run 就被
    绑上了，而没有任何人做过这个决定。歧义与否是候选数量的性质，跟 authority 无关。
    """
    root = _worktree(tmp_path)
    _write_named_prereg(root, "OnlyOne", run_role="primary", expected_params=PARAMS)
    state = _experiment_state(tmp_path, root)
    accepted = _accept(state, {"experiment_focus": "只有一份候选，派发方仍未指名"})

    assert accepted["receipt"]["prereg_assignment"] == {"kind": "pending"}, accepted
    witness = accepted["receipt"]["unbound_prereg_visibility_witness"]
    assert [c["artifact_id"] for c in witness["frozen"]] == ["pre_registration__OnlyOne"]

    contract = load_run_contract(state)
    assert contract["run_role"] != "primary", "见证里有候选 ≠ 这个 run 被提成 primary"
    assert contract.get("expected_params") is None, "没指名就不许把候选的参数拿来用"


def test_multiple_frozen_preregs_are_never_guessed_between(tmp_path: Path) -> None:
    """多份冻结 prereg 时不许替调用方抽签。

    E2E v20 实测：旧实现按文件名排序 + setdefault（先见者赢），跨 session 继承
    的旧 prereg 压死本轮新 prereg，experiment 拿上一轮 cooling_steps 卡本轮方案，
    且它没有 amendment 工具可自救。改成时间戳排序只是换个启发式 —— 同一 session
    为两个子问题各冻一份完全合法，那时照样猜错。根子是"受哪份约束"该由调度方
    声明。
    """
    root = _two_frozen(tmp_path)
    state = _experiment_state(tmp_path, root)
    accepted = _accept(state, {"experiment_focus": "两份冻结 prereg，派发方没指名"})

    # 现在的形状不是「契约先猜出一张歧义名单」，而是「根本没绑」：两份候选只出现在
    # authorizing=false 的可见性见证里。
    assert accepted["receipt"]["prereg_assignment"] == {"kind": "pending"}, accepted
    witness = accepted["receipt"]["unbound_prereg_visibility_witness"]
    assert sorted(c["artifact_id"] for c in witness["frozen"]) == [
        "pre_registration__Cooling_Rate_Tg",
        "pre_registration__Cooling_Rate_Tg_v11_lowT",
    ], witness

    contract = load_run_contract(state)
    # 没有指名时不许自作主张挑一份的参数
    assert "expected_params" not in contract or not contract.get("expected_params")


def test_the_declared_prereg_wins_over_any_ordering(tmp_path: Path) -> None:
    """调度方指名 = 权威。文件名排序在前的那份不该因此占便宜。"""
    root = _two_frozen(tmp_path)
    state = _experiment_state(tmp_path, root)
    state.hook_state["node_inputs"] = {
        "prereg_artifact_id": "pre_registration__Cooling_Rate_Tg_v11_lowT"}

    contract = load_run_contract(state)

    assert contract["expected_params"]["cooling_steps"] == [38000, 120253, 380000]
    assert "ambiguous_preregs" not in contract
    assert "v11_lowT" in contract["contract_source"]


def test_declaring_a_missing_prereg_fails_loudly(tmp_path: Path) -> None:
    """指名了一份不存在/未冻结的，不能默默退回扫描 —— 那等于声明无效。"""
    root = _two_frozen(tmp_path)
    state = _experiment_state(tmp_path, root)
    state.hook_state["node_inputs"] = {"prereg_artifact_id": "pre_registration__ghost"}

    contract = load_run_contract(state)

    assert contract["contract_source"] == "declared_prereg_not_found"
    assert contract["run_role"] == "secondary"      # fail-closed


# ── 执行契约门必须真的挡住 ────────────────────────────────────────────────
#
# 上面几条只证明 load_run_contract **算出**了歧义。歧义算出来不等于挡得住 ——
# 变异验证实测：把 preflight 里那道歧义闸整段删掉，上面 8 条测试**一条都不红**。
# "机制存在但没接到路径"这次差点又发生在自己的补丁上，所以把门本身也钉住。

from tools.preflight import audit_execution_contract  # noqa: E402


def test_declaring_scientific_scope_is_refused_while_assignment_is_pending(
    tmp_path: Path,
) -> None:
    """没指名就不许开工 —— 而且要说清这事归谁、候选有哪些。

    这条钉的是「挡得住」，不是「算得出」：`load_run_contract` 认出没绑定，只是一个
    读数；真正要成立的是**科学动作在声明作用域那一刻就被拒**。拒绝信里少了
    candidate_bindings 或 next_action.owner，child 就只能原地猜或原地补写，而这
    个决定本来就不归它。
    """
    root = _two_frozen(tmp_path)
    state = _experiment_state(tmp_path, root)
    state.hook_state["node_inputs"] = {"experiment_focus": "两份冻结 prereg，派发方没指名"}

    result = asyncio.run(_classify_experiment_scope(
        state, scope="scientific",
        reason="Declare the scientific scope before any real effect."))

    assert result["status"] == "error"
    assert result["error_code"] == "prereg_assignment_required"
    assert result["prereg_assignment"] == {"kind": "pending"}
    # 补不了：这个决定在 child 里没有合法出路
    assert result["retryable_in_current_run"] is False
    assert result["next_action"]["owner"] == "dispatching_parent"
    # 候选必须全列出来，一份都不能少 —— 少了就等于替派发方缩小了选择范围
    assert sorted(c["artifact_id"] for c in result["candidate_bindings"]) == [
        "pre_registration__Cooling_Rate_Tg",
        "pre_registration__Cooling_Rate_Tg_v11_lowT",
    ], result["candidate_bindings"]


def test_gate_blocks_a_submit_when_the_declared_prereg_is_missing(tmp_path: Path) -> None:
    root = _two_frozen(tmp_path)
    state = _experiment_state(tmp_path, root)
    state.hook_state["node_inputs"] = {"prereg_artifact_id": "pre_registration__ghost"}

    result = audit_execution_contract(
        state, {"cooling_steps": [38000, 120253, 380000]}, stage="simulation")

    assert result["passed"] is False
    assert result["blocking_reasons"] == ["declared_prereg_not_found"]


def test_gate_lets_a_correctly_declared_submit_through(tmp_path: Path) -> None:
    """闸门不能只会挡 —— 指名正确且参数一致时必须放行，否则就是把路堵死。"""
    root = _two_frozen(tmp_path)
    state = _experiment_state(tmp_path, root)
    state.hook_state["node_inputs"] = {
        "prereg_artifact_id": "pre_registration__Cooling_Rate_Tg_v11_lowT"}

    result = audit_execution_contract(
        state, {"cooling_steps": [38000, 120253, 380000]}, stage="simulation")

    assert result["passed"] is True, result

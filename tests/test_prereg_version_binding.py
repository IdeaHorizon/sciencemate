"""experiment 与预注册版本的绑定（RFC 2026-08-18，#453-2 / #395-3 的解）。

旧现场：工作区躺着 6 份 prereg，experiment 报「pre_registration 未 freeze」——
它问的其实是「我这一轮绑定的那份」，而没有任何东西回答这个问题。现在契约绑
`(artifact_id, version, content_hash)` 三元组；修订草稿挂着时派发 fail-loud。

夹具用没绑 worktree 的 State（run 本地账本）：冻结是账本上的一行
（`state.mark_frozen`），修订是带 amendment_reason 的 save，版本由账本发。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from core.state import State

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "nodes" / "experiment"))
from tools.run_contract import load_run_contract, resolve_run_acceptance  # noqa: E402


@pytest.fixture()
def state(tmp_path: Path) -> State:
    root = tmp_path / "run1"
    (root / "artifacts").mkdir(parents=True)
    return State(run_id="r1", node_type="experiment", root=root)


def _freeze(state: State, artifact_id: str) -> None:
    state.mark_frozen(artifact_id)


def _prereg(state: State, name: str = "study1", *, frozen: bool = True) -> str:
    r = state.save_artifact("pre_registration", name, f"# prereg {name}",
                            metadata={"run_role": "primary", "expected_params": {"n": 3}})
    if frozen:
        _freeze(state, r["id"])
    return r["id"]


def _assign(state: State, artifact_id: str, *, version: int, content_hash: str) -> dict:
    """派发方指名「本轮跑这一版」，冻进本 run 的 write-once acceptance 收据。

    #1099 之后 catalog 不再是 assignment authority：工作区里躺着一份（哪怕只有
    一份）冻结 prereg，不等于这个 run 被绑到了它身上。三元组只能从派发方的
    typed `prereg_assignment` 来 —— 夹具不给，契约读出来就是未绑定，而「未绑定」
    与「绑好了但参数对得上」在 passed 上长得一模一样。
    """
    node_inputs = dict(state.hook_state.get("node_inputs") or {})
    node_inputs["prereg_assignment"] = {
        "kind": "bound",
        "artifact_id": artifact_id,
        "version": int(version),
        "content_hash": content_hash,
    }
    state.hook_state["node_inputs"] = node_inputs
    accepted = resolve_run_acceptance(state, bind_if_absent=True, requested_mode="scientific")
    assert accepted["passed"] is True, accepted
    return accepted


def _assign_latest_frozen(state: State, artifact_id: str) -> dict:
    frozen = state.latest_frozen_artifact(artifact_id)
    assert frozen is not None, f"{artifact_id} 还没有冻结版"
    return _assign(state, artifact_id, version=int(frozen["version"]),
                   content_hash=frozen["content_hash"])


def test_the_contract_binds_the_version_triple(state: State) -> None:
    pid = _prereg(state)
    _assign_latest_frozen(state, pid)
    contract = load_run_contract(state)
    assert contract["prereg_artifact_id"] == pid
    assert contract["prereg_version"] == 1
    assert len(contract["prereg_content_hash"]) == 64


def test_a_pending_amendment_blocks_dispatch_loudly(state: State) -> None:
    """修订草稿挂着 → 不许静默按旧冻结版跑（抢跑窗口）。"""
    pid = _prereg(state)
    state.save_artifact("pre_registration", "study1", "# revised draft",
                        metadata={}, amendment_reason="review 要求改")

    contract = load_run_contract(state)
    assert contract.get("prereg_version") is None, "草稿挂着不能有静默绑定"
    assert contract["execution_contract_valid"] is False
    pending = contract["pending_amendments"]
    assert pending[0]["artifact_id"] == pid
    assert pending[0]["latest_frozen_version"] == 1
    assert pending[0]["draft_version"] == 2
    assert "prereg_amendment_pending" in contract["contract_warnings"]


def test_an_explicit_version_declaration_is_the_legal_escape(state: State) -> None:
    """调度方显式声明「本轮按 v1 跑」→ 合法绑定旧冻结版。"""
    pid = _prereg(state)
    state.save_artifact("pre_registration", "study1", "# revised draft",
                        metadata={}, amendment_reason="review 要求改")

    state.hook_state["node_inputs"] = {"prereg_artifact_id": pid, "prereg_version": 1}
    contract = load_run_contract(state)
    assert contract["prereg_artifact_id"] == pid
    assert contract["prereg_version"] == 1
    assert "pending_amendments" not in contract
    assert contract["run_role"] == "primary", "绑定版本的 metadata 要生效"


def test_refreezing_the_amendment_clears_the_block(state: State) -> None:
    """重新冻结之后派发的 run，指名 v2 就按 v2 跑，抢跑窗口关上。"""
    pid = _prereg(state)
    state.save_artifact("pre_registration", "study1", "# revised",
                        metadata={"run_role": "primary", "expected_params": {"n": 7}},
                        amendment_reason="review 要求改")
    _freeze(state, pid)

    # 指名的是 v2 —— 重新冻结改变的是「有哪些候选」，不会替派发方作选择。
    _assign_latest_frozen(state, pid)

    contract = load_run_contract(state)
    assert contract["prereg_version"] == 2, "重新冻结后绑定新版"
    assert "pending_amendments" not in contract
    assert contract["expected_params"] == {"n": 7}, "现行承诺是 v2 的设计"


def test_a_refreeze_does_not_rewrite_an_existing_binding(state: State) -> None:
    """已经绑了 v1 的 run，不会因为上游重新冻结就被改成 v2。

    acceptance 收据是 write-once：一条已经在跑的 run，它按哪一版跑是既成事实。
    让重新冻结去改写它，等于事后改一条已经产生了数据的 run 的承诺。
    """
    pid = _prereg(state)
    _assign(state, pid, version=1,
            content_hash=state.latest_frozen_artifact(pid)["content_hash"])

    state.save_artifact("pre_registration", "study1", "# revised",
                        metadata={"run_role": "primary", "expected_params": {"n": 7}},
                        amendment_reason="review 要求改")
    _freeze(state, pid)
    assert state.latest_frozen_artifact(pid)["version"] == 2, "上游确实又冻了一版"

    contract = load_run_contract(state)
    assert contract["prereg_version"] == 1, "本 run 的承诺还是 v1"
    assert contract["expected_params"] == {"n": 3}, "跑的还是 v1 的设计"

    # 连「把 node_inputs 改指 v2 再 resolve 一次」都改写不了它。
    re_resolved = _assign(state, pid, version=2,
                          content_hash=state.latest_frozen_artifact(pid)["content_hash"])
    assert re_resolved["receipt"]["prereg_assignment"]["version"] == 1, re_resolved


def test_two_parallel_studies_are_only_candidates_never_an_assignment(state: State) -> None:
    """两份并行研究仍必须由调度方指名 —— 但「指不出来」的形状不是歧义报告。

    旧写法断言契约自己先猜出一张 `ambiguous_preregs` 名单、再靠执行参数门把
    run 拦下来。那张名单本身就是 catalog 在行使 authority。现在 catalog 只提供
    候选可见性：没有 typed assignment 就是 `pending`，两份候选只出现在
    authorizing=false 的可见性见证里，契约一个参数都不选。
    """
    a = _prereg(state, "study_a")
    b = _prereg(state, "study_b")
    state.hook_state["node_inputs"] = {"experiment_focus": "两份并行研究，没指名"}
    accepted = resolve_run_acceptance(state, bind_if_absent=True, requested_mode="scientific")

    assert accepted["receipt"]["prereg_assignment"] == {"kind": "pending"}, accepted
    witness = accepted["receipt"]["unbound_prereg_visibility_witness"]
    assert sorted(c["artifact_id"] for c in witness["frozen"]) == sorted([a, b]), witness

    contract = load_run_contract(state)
    assert contract["prereg_assignment"] == {"kind": "pending"}
    assert contract.get("prereg_version") is None, "没指名就不该有版本"
    assert contract.get("prereg_content_hash") is None
    assert contract.get("expected_params") is None, "候选的参数一个都不许被选上"
    assert contract["run_role"] != "primary", "见证里有候选 ≠ 这个 run 被提成 primary"


def test_the_manifest_carries_the_binding(state: State, tmp_path: Path) -> None:
    """run_manifest 落盘版本三元组 —— "run 4–9 跑在 v2 下"机械可查。"""
    pid = _prereg(state)
    _assign_latest_frozen(state, pid)
    from tools.run_contract import create_run_manifest

    manifest = create_run_manifest(state)
    assert manifest["prereg_version"] == 1
    assert len(manifest["prereg_content_hash"]) == 64

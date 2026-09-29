"""被合法否定的草稿，不该继续在兑现账本上占位（#901）。

## 病例（2026-09-08 真实 scientific UI run）

1. 早期 **blocked** 的 experiment_log 写入 8 条 ``not_run``；
2. **同一个生产 run** 用一条合法、已冻结的 ``experiment_log_supersession`` 否定了那份
   未冻结草稿；
3. 该 run 在 canonical 冻结日志里写入 8 条 ``discharged``。

Experiment 节点自己的 active view 与 scientific audit **都读到了新日志**；
``core/prereg_commitments.py`` 仍返回 **0/8**。

后果：冻结回执产生**假 closure debt**，decision card 错报 0/8，Writing 在
``fulfilled == 0`` 时可能被错误硬拦 —— 一个已经做完并诚实交付的 run，被账本判成
"一条都没兑现"。

## 根因

``list_artifacts()`` 按 created_at 升序（契约的一部分），``_absorb_block`` 用
``setdefault`` —— 最早那份 ``not_run`` 永久占位。

改成"后写覆盖先写"不安全：那等于允许任意一份较晚的、非 canonical 的产物改账。
正解是把**被否定的那份整个排除**。

## 这套测试的判别力

正例一条（否定生效），反例五条（每条各破坏一个合法性要件，证据必须留在账上）。
只写正例的话，"把所有 supersession 都当合法"也能全绿 —— 那会把否定通道变成一条
任何人都能拿来抹账的路。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from core.bootstrap import bootstrap
from core.prereg_commitments import declared_discharges
from core.state import State
from core.supersession import superseded_artifact_ids

_KEYS = [f"Q1#{i}" for i in range(1, 9)]


@pytest.fixture()
def state(tmp_path: Path):
    bootstrap(force=True)
    return State.new(node_type="experiment", base_dir=tmp_path)


def _log(state, name: str, status: str, *, frozen: bool = False) -> str:
    """造一条 experiment_log。``frozen=True`` 走**真的冻结通道**。

    2026-09-15：这里原本写 ``metadata={"frozen": True}``。布局重构（记录是原生
    文件 + 一本账本）之后冻结是**账本上的事实**，metadata 里那个键谁也不读 ——
    于是测试"冻"了、被测代码看不见，两边一起对着一个不再生效的位置说话。
    用 `mark_frozen` 走真通道，判据才落在真事上。
    """
    saved = state.save_artifact(
        "experiment_log", name, f"## Experiment Log\n{name}\n",
        metadata={
            "closure_discharges": {
                key: {"status": status, "evidence": f"experiment_log__{name}"}
                for key in _KEYS
            },
        },
    )
    artifact_id = str(saved["id"])
    if frozen:
        state.mark_frozen(artifact_id)
    return artifact_id


def _negate(state, target_id: str, **overrides) -> str:
    """按 ``supersede_experiment_log`` 的线上形状造一条否定记录。

    形状取自 nodes/experiment/tools/contract_audit.py：正文是 JSON
    ``{superseded_id, reason, run_id}``，metadata 带 ``superseded_id``，随后立即冻结。
    """
    payload = {
        "superseded_id": overrides.get("payload_target", target_id),
        "reason": overrides.get("reason", "与 canonical log 重复的第二份草稿"),
        "run_id": overrides.get("run_id", state.run_id),
    }
    saved = state.save_artifact(
        "experiment_log_supersession", f"supersede_{target_id}",
        json.dumps(payload, ensure_ascii=False, sort_keys=True),
        metadata={"superseded_id": overrides.get("metadata_target", target_id)},
    )
    artifact_id = str(saved["id"])
    if overrides.get("frozen", True):
        state.mark_frozen(artifact_id)   # 走真通道，不写 metadata.frozen
    return artifact_id


class TestTheNegationIsHonoured:
    def test_the_superseded_draft_stops_occupying_the_ledger(self, state):
        """#901 的正题：8×not_run 草稿 → 合法 supersede → 8×discharged canonical。"""
        draft = _log(state, "blocked_draft", "not_run")
        _negate(state, draft)
        _log(state, "canonical", "discharged", frozen=True)

        tally = declared_discharges(state)
        assert set(tally) == set(_KEYS)
        assert all(entry["status"] == "discharged" for entry in tally.values()), (
            f"被合法否定的草稿仍在账上占位 —— 假 closure debt（#901）：{tally}"
        )

    def test_without_the_negation_the_earliest_still_wins(self, state):
        """判别力自检：没有否定时，最早的仍然赢 —— 否则上一条测的不是这件事。

        这也钉住了"别改成后写覆盖"：那条路会让任意较晚的产物改账。
        """
        _log(state, "blocked_draft", "not_run")
        _log(state, "canonical", "discharged", frozen=True)

        tally = declared_discharges(state)
        assert all(entry["status"] == "not_run" for entry in tally.values())


class TestAnIllegitimateNegationHidesNothing:
    """五条反例：任何一条合法性要件不满足，证据必须留在账上。"""

    def test_an_unfrozen_negation_does_not_count(self, state):
        draft = _log(state, "draft", "not_run")
        _negate(state, draft, frozen=False)
        assert superseded_artifact_ids(state) == frozenset(), "没冻结的否定还能改"

    def test_a_frozen_target_cannot_be_negated(self, state):
        draft = _log(state, "draft", "not_run", frozen=True)
        _negate(state, draft)
        assert superseded_artifact_ids(state) == frozenset(), (
            "frozen log 是已验证证据，一律不可否定"
        )

    def test_payload_and_metadata_must_agree(self, state):
        draft = _log(state, "draft", "not_run")
        other = _log(state, "other", "not_run")
        _negate(state, draft, metadata_target=other)
        assert superseded_artifact_ids(state) == frozenset(), "正文与 metadata 分叉"

    def test_an_empty_reason_is_not_a_reason(self, state):
        draft = _log(state, "draft", "not_run")
        _negate(state, draft, reason="   ")
        assert superseded_artifact_ids(state) == frozenset()

    def test_a_foreign_producer_cannot_negate(self, state):
        """否定记录声称的 run 与它自己的出身对不上 → 不作数。"""
        draft = _log(state, "draft", "not_run")
        _negate(state, draft, run_id="some-other-run")
        assert superseded_artifact_ids(state) == frozenset()


def _bound_project(tmp_path: Path) -> Path:
    """真绑一个 Project worktree —— 不绑就看不见跨节点这条边界。

    [[feedback_tests_dont_bind_project]]：Project v2 的协作模型是"下游读上游的节点
    目录取料"，那条读路径只有绑了 worktree 才在场。不绑的话两个 state 各看各的 run
    目录，这条测试会以"跳过"的形式静静消失。
    """
    root = tmp_path / "negation-project"
    root.mkdir()
    for args in (
        ["init", "-b", "main"],
        ["config", "user.name", "Negation Test"],
        ["config", "user.email", "negation@example.test"],
    ):
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)
    (root / "project.yaml").write_text(
        "schema_version: 2\nname: Ledger negation test\n", encoding="utf-8")
    for node in ("experiment", "writing"):
        (root / node / "artifacts").mkdir(parents=True)
    subprocess.run(["git", "-C", str(root), "add", "--all"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "init"],
                   check=True, capture_output=True)
    return root


def test_a_downstream_consumer_reads_the_same_answer(tmp_path: Path):
    """跨消费者 run：Writing 读 Experiment 的证据是常态。

    合法性判据要求 ``payload.run_id == 否定记录.produced_by_run_id ==
    被否定产物.produced_by_run_id`` —— **三者互相同一**，但**不要求等于当前 run**。
    要求等于当前 run 会把所有下游消费者挡在门外，那正是 #901 想避免的另一种错：
    一个假 debt 换成另一个假 debt。
    """
    bootstrap(force=True)
    worktree = _bound_project(tmp_path)
    producer = State.new(node_type="experiment", base_dir=tmp_path / "runs",
                         project_id="ledger-negation", project_worktree=worktree)
    draft = _log(producer, "draft", "not_run")
    _negate(producer, draft)
    _log(producer, "canonical", "discharged", frozen=True)

    consumer = State.new(node_type="writing", base_dir=tmp_path / "runs",
                         project_id="ledger-negation", project_worktree=worktree)
    assert consumer.run_id != producer.run_id
    seen = [str(item.get("id")) for item in consumer.list_artifacts("experiment_log")]
    assert draft in seen, "下游根本没读到上游产物 —— 这条测试没测到它要测的东西"
    assert draft in superseded_artifact_ids(consumer)
    tally = declared_discharges(consumer)
    assert tally and all(entry["status"] == "discharged" for entry in tally.values()), (
        f"下游消费者仍读到假 debt（#901 的 Writing 侧后果）：{tally}"
    )

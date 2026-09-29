"""子 run 跑完了，到底关掉了父任务的**哪一部分**（#1097 第 4 / 5 条）。

Experiment 的一个 operation run 可以收尾完整、审计通过，而它做的只是装环境、编译、
跑通 —— 节点已经把 `upstream_goal_effect=operational_subtask_only` 写进日志、closure
input、artifact metadata 和 clean payload。但**没有任何一条到得了父 run**：ROC 返回
没有、`run_node` 的结构化返回没有、decision package 只拿到 `final_text_preview[:800]`
的自由文本、结果压缩器的白名单会把新对象整个丢掉。

于是父侧看到 child `completed`，把整个 scientific objective 当成完成了。

判据落在**每一跳**上：删掉任一跳，对应用例必须转红（#1097 验收 7）。
"""
from __future__ import annotations

import inspect

import pytest

from core.obligation_effect import (
    CONTRIBUTION_EVIDENCE,
    CONTRIBUTION_NONE,
    EFFECT_FULL,
    EFFECT_OPERATIONAL,
    ChildObligationEffect,
    remaining_obligation,
)
from core.task_contract import PreregAssignment, TaskContractLog


def _effect(kind=EFFECT_OPERATIONAL, contribution=CONTRIBUTION_NONE):
    return ChildObligationEffect(
        upstream_goal_effect=kind, scientific_contribution=contribution,
        task_contract_revision_digest="d1",
        source_receipt={"artifact_id": "a1", "closure_id": "c1", "content_hash": "h1"})


def _revision(tmp_path, assignment):
    return TaskContractLog(tmp_path / "tasks").append(
        task_instance_uuid="u1", objective="测 Tc", actor="orch",
        prereg_assignment=assignment)


# ── reducer：completed 只关掉它自己声明关掉的那部分 ─────────────────────────


def test_a_pure_build_child_does_not_close_the_scientific_objective(tmp_path) -> None:
    """#1097 验收 6：exact-bound 的科学任务下，纯构建 child 可以 completed，
    但父侧机械读到 `scientific_contribution=none`，科学目标保持 open。"""
    rev = _revision(tmp_path, PreregAssignment.exact("pre_registration__H1"))
    out = remaining_obligation(rev, _effect(), child_status="completed")

    assert out["operational_subtask_closed"] is True
    assert out["scientific_objective_open"] is True, (
        "child completed 就把科学目标算成做完了 —— 这正是这条 issue 的形状")


def test_a_real_scientific_child_closes_it(tmp_path) -> None:
    """对照：真推进了科学目标的那一趟要能关掉它，否则 reducer 成了永不满足的墙。"""
    rev = _revision(tmp_path, PreregAssignment.exact("pre_registration__H1"))
    out = remaining_obligation(
        rev, _effect(EFFECT_FULL, CONTRIBUTION_EVIDENCE), child_status="completed")
    assert out["scientific_objective_open"] is False


def test_an_unreported_effect_is_not_read_as_done(tmp_path) -> None:
    """「没报告」和「报告说全做完了」必须是两个答案。"""
    rev = _revision(tmp_path, PreregAssignment.exact("pre_registration__H1"))
    out = remaining_obligation(rev, None, child_status="completed")
    assert out["scientific_objective_open"] is True
    assert out["operational_subtask_closed"] is False
    assert "不替它声明" in out["reason"]


def test_a_task_with_no_prereg_has_no_scientific_objective_to_close(tmp_path) -> None:
    rev = _revision(tmp_path, PreregAssignment.none("这趟只是装环境"))
    out = remaining_obligation(rev, _effect(), child_status="completed")
    assert out["scientific_objective_open"] is False


def test_a_child_that_did_not_finish_closes_nothing(tmp_path) -> None:
    rev = _revision(tmp_path, PreregAssignment.exact("pre_registration__H1"))
    out = remaining_obligation(rev, _effect(EFFECT_FULL, CONTRIBUTION_EVIDENCE),
                               child_status="blocked")
    assert out["scientific_objective_open"] is True
    assert out["operational_subtask_closed"] is False


def test_a_malformed_effect_is_not_invented(tmp_path) -> None:
    """读不出来就是 None —— 造一个默认值等于替子 run 声明它做了什么。"""
    assert ChildObligationEffect.from_dict({"upstream_goal_effect": "随便写的"}) is None
    assert ChildObligationEffect.from_dict("done") is None
    assert ChildObligationEffect.from_dict(
        {"upstream_goal_effect": EFFECT_FULL})is not None


# ── 每一跳都要在 ──────────────────────────────────────────────────────────


def test_the_effect_survives_the_child_summary() -> None:
    import core.executor as executor_mod

    assert '"child_obligation_effect"' in inspect.getsource(executor_mod), (
        "summary 里没有它 —— 这一跳断了，后面每一跳都拿不到")


def test_the_effect_survives_the_run_node_return() -> None:
    import shared.tools.run_node as run_node_mod

    src = inspect.getsource(run_node_mod)
    assert 'out["child_obligation_effect"]' in src, "run_node 的返回没点它的名"


def test_the_effect_survives_the_result_compactor() -> None:
    """压缩器的白名单漏掉一个新对象，父侧就退回"只看 status"。"""
    import shared.tools.run_node as run_node_mod

    src = inspect.getsource(run_node_mod._compact_run_node_result)
    assert "child_obligation_effect" in src


def test_the_effect_survives_the_background_notification() -> None:
    import shared.tools.run_node as run_node_mod

    src = inspect.getsource(run_node_mod._report_background_done)
    assert "child_obligation_effect" in src, (
        "后台跑的和前台跑的在父侧成了两种语义")


def test_the_effect_reaches_the_decision_card() -> None:
    """人要在这张卡上做决定，那个机械事实就得摆在他眼前。"""
    import shared.tools.library.decision_package as dp

    src = inspect.getsource(dp._render_decision_package)
    assert "upstream_goal_effect" in src
    assert "没有**推进科学目标本身" in src


# ── 下游引用端的半程防线（#1097 第 5 条）───────────────────────────────────


def test_an_operational_artifact_cannot_be_cited_as_confirmatory() -> None:
    from shared.lib.evidence_use import (
        confirmatory_use_note, may_serve_as_confirmatory_evidence,
    )

    operational = {"metadata": {"upstream_goal_effect": "operational_subtask_only"}}
    assert may_serve_as_confirmatory_evidence(operational) is False
    note = confirmatory_use_note(operational)
    assert note and "不是**确证证据" in note
    # 只说"不行"会让模型无路可走 —— 正当用途要逐条说清
    assert "运维事实" in note and "post-hoc" in note


def test_an_ordinary_artifact_is_untouched() -> None:
    from shared.lib.evidence_use import (
        confirmatory_use_note, may_serve_as_confirmatory_evidence,
    )

    for record in ({"metadata": {}}, {"metadata": {"upstream_goal_effect": ""}}, {}):
        assert may_serve_as_confirmatory_evidence(record) is True
        assert confirmatory_use_note(record) is None


@pytest.mark.asyncio
async def test_the_note_travels_with_the_artifact_itself(tmp_path) -> None:
    """走真入口：挂在**读**这一侧，谁读这份产物谁就同时拿到那句话。

    判据不落在"源码里有没有这个名字"上 —— 那种写法在我把 `_use_note` 换成常量
    `None` 之后照样全绿（实测），而缺陷原样活着。
    """
    from core.bootstrap import bootstrap
    from core.state import State
    from shared.tools.builtin import _read_artifact

    bootstrap()
    state = State.new(node_type="writing", base_dir=tmp_path / "runs", project_id=None)
    state.save_artifact("clean_results", "build_only", "编译通过",
                        metadata={"upstream_goal_effect": "operational_subtask_only"})
    state.save_artifact("clean_results", "real_science", "T_c = 2.269 ± 0.0001")

    ids = {a["name"]: a["id"] for a in state.list_artifacts()}
    flagged = await _read_artifact(state, ids["build_only"])
    assert "evidence_use" in flagged, (
        "读到一份只是运维子任务的产物，envelope 里却没有任何归属说明 —— "
        "它和一份真的确证结果长得一模一样")
    assert "不是**确证证据" in flagged["evidence_use"]

    ordinary = await _read_artifact(state, ids["real_science"])
    assert "evidence_use" not in ordinary, "普通产物被连坐了"


def test_nobody_reimplements_the_check() -> None:
    """判据只有一份 —— 各节点各写一句字符串比较就是 N 份会各自演化的抄件。"""
    from pathlib import Path

    repo = Path(__file__).resolve().parents[1]
    offenders = []
    for path in list((repo / "core").rglob("*.py")) + list((repo / "shared").rglob("*.py")):
        if path.name in {"evidence_use.py", "obligation_effect.py"}:
            continue
        body = path.read_text(encoding="utf-8", errors="ignore")
        if '"operational_subtask_only"' in body or "'operational_subtask_only'" in body:
            offenders.append(str(path.relative_to(repo)))
    assert not offenders, (
        f"这些地方自己写了一遍判据，而它只该有一份（shared/lib/evidence_use）：{offenders}")


# ── 生产方入口：节点怎么写这条效应 ─────────────────────────────────────────


class _State:
    def __init__(self, digest="d-from-dispatch"):
        self.hook_state: dict = {}
        self.transcript: list = []
        self.task_contract_digest = digest

    def append_transcript(self, kind, **kw):
        self.transcript.append((kind, kw))


def test_the_node_cannot_declare_which_contract_it_ran_under() -> None:
    """合同摘要由框架从 state 填 —— 让被派的一方自己声明等于自己发授权。"""
    from core.obligation_effect import record_obligation_effect

    st = _State()
    rec = record_obligation_effect(
        st, upstream_goal_effect=EFFECT_OPERATIONAL,
        source_receipt={"artifact_id": "a1"})
    assert rec["task_contract_revision_digest"] == "d-from-dispatch"
    assert st.hook_state["child_obligation_effect"] == rec
    assert [k for k, _ in st.transcript] == ["child_obligation_effect_recorded"]


def test_the_vocabulary_is_closed_on_the_producing_side() -> None:
    from core.obligation_effect import record_obligation_effect

    with pytest.raises(ValueError):
        record_obligation_effect(_State(), upstream_goal_effect="差不多做完了")
    with pytest.raises(ValueError):
        record_obligation_effect(_State(), upstream_goal_effect=EFFECT_FULL,
                                 scientific_contribution="有一点")


def test_a_child_reads_its_assignment_in_one_call(tmp_path) -> None:
    """节点不必自己拼项目路径，更不该去扫"此刻项目里有几份 prereg"。"""
    from core.task_contract import ASSIGNMENT_EXACT, ASSIGNMENT_PENDING, assignment_for_state

    rev = _revision(tmp_path, PreregAssignment.exact("pre_registration__H1"))

    class _Child:
        task_instance_uuid = "u1"
        task_contract_digest = rev.digest
        project_root = tmp_path

    got, kind = assignment_for_state(_Child())
    assert kind == ASSIGNMENT_EXACT
    assert got.assignment.artifact_id == "pre_registration__H1"

    class _Unassigned:
        task_instance_uuid = ""
        task_contract_digest = ""
        project_root = tmp_path

    assert assignment_for_state(_Unassigned()) == (None, ASSIGNMENT_PENDING)

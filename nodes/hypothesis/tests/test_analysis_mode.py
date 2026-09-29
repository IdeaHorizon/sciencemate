"""Analysis 两档模式 —— 拿实验结果回来不该被逼着重跑整套生成流程。

回归锚点：`validate_hypothesis_outputs` 的三条 blocking 判据从 **per-run**
transcript 取料，而 artifacts 是跨 run 持久的。第 2 轮开局 transcript 是空的
→ 判据全 fail → 只能重新 freeze 一份 prereg 才能收尾 → 于是整套
generate / cluster / evolve / HIF 又演一遍。
"""
from __future__ import annotations

import asyncio
import json
import subprocess
import tempfile
from pathlib import Path

from core.ledger import write_record
from core.state import State
from nodes.hypothesis.tools.analysis_mode import (
    MODE_PLAN,
    MODE_REVISE,
    resolve_mode,
    run_authored_commitments,
    unaccounted_experiments,
    unresolvable_adjudication_evidence,
)
from nodes.hypothesis.tools.output_validator import _validate_hypothesis_outputs
from nodes.hypothesis.tools.research_state import _update_research_state

_PREREG = """## Research Questions

### H1: 冷却速率是否改变势能极小值分布？
- output_kind: 对一条命题的裁决
- proposition: 慢冷样品的 inherent structure 能量显著低于快冷样品
- assumption: 冷却速率区间覆盖了会发生结构变化的那一段
```yaml
- metric: IS_energy_delta
  comparison: "<"
  threshold: -0.05
```
"""

_PLAN = (
    "Plan sections: experimental_design · computational_workflow · "
    "baselines · resource_estimates · risk_analysis\n\n"
    "## Computational Workflow\n\n"
    "```mermaid\nflowchart TD\n  S1[\"quench\"] --> S2[\"analysis\"]\n```\n\n"
    "| Step ID | 任务类型 | 前置步骤 | 软件/方法 | 模型尺度 | 关键参数 | 产出 | 对应 falsifier |\n"
    "| S1 | quench | - | LAMMPS | 原胞 | 依据：文献冷却速率区间 | 轨迹 | H1 |\n"
    "| S2 | analysis | S1 | Python | - | IS 能量统计 | 表 | H1 |\n\n"
    "### 步骤依赖与门控\n- S2 需 S1 完成\n"
)


def _make_state() -> State:
    return State.new(node_type="hypothesis", base_dir=Path(tempfile.mkdtemp()))


def _seed_round_one(state: State) -> None:
    """模拟第 1 轮**已经跑完并落盘**的样子（记录在，transcript 不在）。"""
    prereg = state.save_artifact("pre_registration", "P", _PREREG)
    state.mark_frozen(prereg["id"])     # 冻结 = 账本一行，不是 metadata 里的一个键
    state.save_artifact("research_plan", "Plan", _PLAN)
    state.save_artifact(
        "hypothesis_innovation_report", "HIF",
        "```json\n" + json.dumps({
            "summary": {"n_assessed": 1, "max_hif": 60},
            "assessments": [{
                "label": "H1", "claim_text": "慢冷样品的 IS 能量更低",
                "dimensions": {"R": 1, "Q": 4}, "tier": "moderate",
                "plausibility_reject": False,
            }],
        }) + "\n```\n",
        metadata={"n_assessed": 1, "max_hif": 60},
    )
    state.save_artifact(
        "hypothesis_research_overview", "Overview", "# Overview\n" + "x" * 300,
    )
    # 第 1 轮把假说登记进 KB —— chunk + claim 都是**项目级持久**的，
    # 后续轮的 prereg_frozen_and_registered 靠它们，而不是靠本轮 transcript。
    chunk, _ = state.write_kb("chunks", {
        "text": _PREREG, "source": "artifact:pre_registration__P",
        "origin_artifact_id": "pre_registration__P",
        "origin_artifact_frozen": True, "offset": 0, "length": len(_PREREG),
    })
    state.write_kb("claims", {
        "claim_text": "慢冷样品的 IS 能量更低",
        "claim_type": "hypothesis",
        "prereg_chunk_id": chunk["id"],
        "sources": [chunk["id"]],
        "orphan_reason": "测试桩：不建 concept",
        "falsification_criteria_text": "慢冷组 IS 能量不低于快冷组则证伪",
        "predicted_outcome": "慢冷组 IS 能量显著更低",
    })
    asyncio.run(_update_research_state(
        state, verdict="continue",
        hypotheses=[{"id": "H1", "status": "active"}],
        research_question="冷却速率与 IS 能量",
    ))


def test_round_one_is_plan_mode() -> None:
    state = _make_state()
    assert resolve_mode(state)["mode"] == MODE_PLAN


def test_round_two_is_revise_mode_without_anyone_declaring_it() -> None:
    """模式是项目状态的事实 —— 调用方忘了传也必须算对。"""
    state = _make_state()
    _seed_round_one(state)
    resolution = resolve_mode(state, node_inputs=None)
    assert resolution["mode"] == MODE_REVISE
    assert resolution["declared"] is None


def test_caller_can_force_a_full_replan() -> None:
    state = _make_state()
    _seed_round_one(state)
    assert resolve_mode(state, {"mode": "plan"})["mode"] == MODE_PLAN


def test_revise_cannot_be_declared_into_existence() -> None:
    """否则 revise 就是"跳过整套预注册"的绕行路径。"""
    state = _make_state()
    resolution = resolve_mode(state, {"mode": "revise"})
    assert resolution["mode"] == MODE_PLAN
    assert resolution["downgraded"] is True
    assert "pre_registration" in resolution["reason"]


def test_second_round_validates_without_refreezing_anything() -> None:
    """核心回归：第 2 轮**一次工具调用都没有**，也该过收尾自检。

    改之前：prereg_frozen_and_registered / structured_falsification_present /
    not_conclusion_restatement 三条全 fail（transcript 空），节点只能重跑整套。
    """
    state = _make_state()
    _seed_round_one(state)
    assert run_authored_commitments(state) is False

    result = asyncio.run(_validate_hypothesis_outputs(state))
    assert result["mode"] == MODE_REVISE
    assert result["passed"] is True, result["failed_checks"]
    for name in ("structured_falsification_present", "not_conclusion_restatement",
                 "hif_plausibility_gate"):
        assert name in result["not_applicable"]
    # 冻结协议这条**仍然要审**，只是改成看盘上的产物而不是看本轮的对话
    assert "prereg_frozen_and_registered" not in result["not_applicable"]
    assert "prereg_frozen_and_registered" not in result["failed_checks"]


def test_second_round_still_fails_when_the_protocol_is_gone() -> None:
    """放宽取料口不等于放宽判据：没有冻结协议照样过不去。"""
    state = _make_state()
    _seed_round_one(state)
    # 冻结不可撤销；"协议没了"在新契约下的形状是：head 被修订成未冻结的草稿
    # （冻结的 v1 留在历史里，但项目**现在**没有一份生效的协议）。
    for entry in state.list_artifacts("pre_registration"):
        record = state.read_artifact(entry["id"])
        state.save_artifact(
            "pre_registration", record["name"], record["content"],
            amendment_reason="修订中：协议暂时没有生效版本",
        )
        assert not state.read_artifact(entry["id"])["metadata"].get("frozen")
    result = asyncio.run(_validate_hypothesis_outputs(state))
    assert result["passed"] is False
    assert "prereg_frozen_and_registered" in result["failed_checks"]


def test_authoring_a_new_prereg_puts_the_hypothesis_checks_back() -> None:
    """revise 轮真的新增了命题 → 假说类判据重新生效，不是永久豁免。"""
    state = _make_state()
    _seed_round_one(state)
    # 新契约：承诺住在 prereg 里。本轮新写了一版**声明命题但没给判据**的 prereg
    # 草稿 → 假说类判据重新生效且必须抓住缺口。
    # 修订冻结件必须走 amend 链（不带 amendment_reason 会被 save 原语当场拒绝
    # —— 上一版这个测试就是被它拒掉的，架构在替自己作证）。
    state.save_artifact(
        "pre_registration", "P",
        "## Inquiry Contract\n\n### Q1: 新命题？\n- decides_a_proposition: yes\n"
        "- proposition: 新机制成立\n",
        amendment_reason="revise 轮新增命题",
    )
    state.append_transcript("tool_call", name="save_artifact",
                            args={"artifact_type": "pre_registration"})
    assert run_authored_commitments(state) is True

    result = asyncio.run(_validate_hypothesis_outputs(state))
    assert "structured_falsification_present" not in result["not_applicable"]
    assert "structured_falsification_present" in result["failed_checks"]


def test_hypothesis_commitments_outside_the_contract_are_still_audited() -> None:
    """防线升级：'契约外登记假说'这条路已在工具层关死。

    旧防线是事后审计（transcript 里发现 create_hypothesis 却没契约 → fail）。
    新防线在源头：hypothesis 节点调 create_claim(claim_type=hypothesis) 直接被
    拒，报错必须给出 amend 正路 —— 承诺只能进预注册。"""
    import asyncio as _asyncio

    from shared.tools.library.kb import _create_claim

    state = _make_state()
    state.node_type = "hypothesis"
    result = _asyncio.run(_create_claim(
        state, claim_text="无契约的假说", claim_type="hypothesis"))
    # 第三波（一审 O1）：「hypothesis 节点不许写 claim」的角色闸删。现在挡住这条
    # 路的是身份契约（C 类，呈裁 b 判 keep）：hypothesis 类 claim 必须带 hypothesis_id
    # 作为原地更新的身份锚——承诺仍只能经预注册进来，但拒绝的是契约不成形，不是身份。
    assert result["status"] == "error"
    assert "hypothesis_id" in result["error"]


# ── revise 放宽之后新补的两道闸 ────────────────────────────────────────


def _seed_experiment_result(state: State, name: str = "Quench_Run") -> str:
    """experiment 节点在同一工作区留下的一份结果：原生文件进 experiments/，
    事实（产出方、run id）进工作区账本。"""
    record = write_record(
        state.project_worktree, artifact_type="experiment_log", name=name,
        content="run ok", directory="experiments",
        metadata={"run_id": "run-abc"},
        produced_by_node_type="experiment", produced_by_run_id="run-abc",
        created_at="2026-08-16T00:00:00+00:00",
    )
    return record["id"]


def _worktree_state() -> State:
    """带 project_worktree 的 state —— 跨节点产物只有 v2 底座才看得见。"""
    root = Path(tempfile.mkdtemp())
    worktree = root / "worktree"
    worktree.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(worktree)], check=True)
    for args in (["add", "-A"],
                 ["-c", "user.email=t@t", "-c", "user.name=t",
                  "commit", "-q", "--allow-empty", "-m", "seed"]):
        subprocess.run(["git", "-C", str(worktree), *args], check=True)
    return State.new(node_type="hypothesis", base_dir=root / "runs",
                     project_worktree=worktree)


def test_experiment_results_must_be_accounted_for() -> None:
    state = _worktree_state()
    _seed_round_one(state)
    artifact_id = _seed_experiment_result(state)
    assert unaccounted_experiments(state) == [artifact_id]

    result = asyncio.run(_validate_hypothesis_outputs(state))
    assert result["passed"] is False
    assert "experiment_results_accounted" in result["failed_checks"]


def test_accounting_for_the_result_clears_the_gate() -> None:
    state = _worktree_state()
    _seed_round_one(state)
    artifact_id = _seed_experiment_result(state)
    asyncio.run(_update_research_state(
        state, verdict="continue",
        hypotheses=[{"id": "H1", "status": "supported", "evidence": [artifact_id]}],
        change_reason="第 2 轮：读到 quench 结果，H1 被支持",
    ))
    assert unaccounted_experiments(state) == []
    assert unresolvable_adjudication_evidence(state) == []
    result = asyncio.run(_validate_hypothesis_outputs(state))
    assert result["passed"] is True, result["failed_checks"]


def test_evidence_that_points_at_nothing_is_not_evidence() -> None:
    state = _worktree_state()
    _seed_round_one(state)
    artifact_id = _seed_experiment_result(state)
    asyncio.run(_update_research_state(
        state, verdict="continue",
        hypotheses=[{
            "id": "H1", "status": "refuted",
            # 交代了那份实验，但裁决挂的是一个不存在的 id
            "evidence": ["experiment_log__Imaginary"],
            "note": f"参考 {artifact_id}",
        }],
        change_reason="第 2 轮",
    ))
    bad = unresolvable_adjudication_evidence(state)
    assert [row["id"] for row in bad] == ["H1"]
    result = asyncio.run(_validate_hypothesis_outputs(state))
    assert result["passed"] is False
    assert "adjudication_evidence_resolvable" in result["failed_checks"]

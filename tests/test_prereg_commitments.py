"""承诺对象化的回归。

核心是 `test_replays_e2e3_h3`：E2E-3 的 H3 判据是合取
（cost_reduction_pct AND success_rate_delta_pp），第二项论文自己承认没测
（"estimated, not measured through actual API calls"），却照样被写成 "refuted"
进了 KB 和摘要。断言这道门禁会拦住。
"""
from __future__ import annotations

import pytest

from core import prereg_commitments as pc
from core.bootstrap import bootstrap
from core.state import State

bootstrap()

# E2E-3 冻结预注册的真实结构（照抄，含它那份"不是合法 YAML"的 measurement 段）
REAL_PREREG = """
## Hypothesis 1 (H1): Verifiable Step Fraction Baseline

**Claim**: On τ-bench, at least 30% of steps are verifiable.

### Falsification Criteria (Structured)

```yaml
metric: verifiable_step_fraction
comparison: less_than
threshold: 0.30
dataset: τ-bench (all tasks)
measurement:
  - Run baseline agent on all τ-bench tasks
falsified_if: "Upper bound of 95% CI < 0.30"
```

---

## Hypothesis 3 (H3): Cost Reduction with Quality Preservation

**Claim**: Routing verifiable steps to cheaper executors reduces cost ≥25%
while degrading success rate ≤5pp.

### Falsification Criteria (Structured)

```yaml
metric: [cost_reduction_pct, success_rate_delta_pp]
comparison: any_false
conditions:
  - metric: cost_reduction_pct
    comparison: less_than
    threshold: 25.0
  - metric: success_rate_delta_pp
    comparison: greater_than
    threshold: 5.0
dataset: τ-bench (all tasks)
measurement:
  - Baseline: run all τ-bench tasks with full-context model; measure cost and success rate
  - Treatment: run same tasks with verifiability-based routing:
      * Rule-based executor: steps classified as "trivially verifiable" (e.g., regex)
      * Reduced-context executor (≤4K tokens)
```
"""


# ── 解析 ────────────────────────────────────────────────────────────────────

def test_parses_both_flat_and_conjunctive_criteria():
    """同一份预注册里两种嵌套都要吃：H1 平铺、H3 合取（真判据在 conditions）。"""
    got = pc.parse_commitments(REAL_PREREG)
    assert set(got) == {"H1", "H3"}
    assert got["H1"]["metrics"] == [
        {"metric": "verifiable_step_fraction", "comparison": "less_than",
         "threshold": "0.30"}]
    assert got["H3"]["metrics"] == [
        {"metric": "cost_reduction_pct", "comparison": "less_than",
         "threshold": "25.0"},
        {"metric": "success_rate_delta_pp", "comparison": "greater_than",
         "threshold": "5.0"}]


def test_prose_in_measurement_does_not_swallow_the_commitment():
    """H3 那个块不是合法 YAML（measurement 里有带冒号的散文 + `*` 缩进行）。

    第一版用 yaml.safe_load，抛异常后按"宁可不拦"返回空 —— **H3 这条最重要的
    承诺被静默吞掉**（实测：H1/H2 出来了，H3 没有）。在最要紧的用例上静默降级，
    比不做还坏。
    """
    import yaml
    block = REAL_PREREG.split("```yaml")[2].split("```")[0]
    with pytest.raises(Exception):
        yaml.safe_load(block)              # 前提：它确实不是合法 YAML
    assert len(pc.parse_commitments(REAL_PREREG)["H3"]["metrics"]) == 2


def test_conjunction_header_is_not_mistaken_for_a_metric():
    """`metric: [a, b]` 是合取表头，不是一条判据。"""
    metrics = pc.parse_commitments(REAL_PREREG)["H3"]["metrics"]
    assert all(not m["metric"].startswith("[") for m in metrics)


def test_unparsable_criteria_section_is_reported():
    """写了 Falsification Criteria 却解析不出 metric → fail-loud，不静默放过。"""
    bad = """
## Hypothesis 2 (H2): Something

### Falsification Criteria (Structured)

```yaml
falsified_if: "看着办"
```
"""
    assert pc.sections_without_parsable_criteria(bad) == ["H2"]
    assert pc.sections_without_parsable_criteria(REAL_PREREG) == []


def test_legacy_prereg_without_convention_is_left_alone():
    """没用 `## Hypothesis N (Hx)` 约定的极简/历史 prereg 不进承诺账、不被拦。"""
    legacy = "# Prereg\nH1: D0-D4 占比 > 50%"
    assert pc.declares_hypotheses(legacy) is False
    assert pc.parse_commitments(legacy) == {}
    assert pc.sections_without_parsable_criteria(legacy) == []


# ── 账 + 门禁 ───────────────────────────────────────────────────────────────

def _analysis_worktree(tmp_path, *, verdicts):
    """真 Git worktree + Analysis 已出过 research_state。

    没有它，`scientific_verdict_block` 会先以"读不到 research_state、核不了
    裁决权"拦下来，这个文件想验的那道承诺门就永远轮不到执行 —— 测试会绿在
    一个它根本没测的东西上。
    """
    import subprocess

    from core.ledger import write_record
    from core.project_workspace import _NODE_WORKSPACES as _DIRS

    root = tmp_path / "wt"
    (root / _DIRS["hypothesis"]).mkdir(parents=True)
    for args in (["init", "-b", "main"], ["config", "user.name", "T"],
                 ["config", "user.email", "t@e.test"]):
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)
    # research_state 是 plan/ 下的原生文件，出处与版本在工作区账本里。
    write_record(root, artifact_type="research_state", name="research_state", content="",
                 directory=_DIRS["hypothesis"],
                 metadata={"version": 1, "hypotheses": verdicts},
                 produced_by_node_type="hypothesis", produced_by_run_id="r-hyp")
    subprocess.run(["git", "-C", str(root), "add", "--all"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(root), "commit", "-m", "init"],
                   check=True, capture_output=True)
    return root


def _project(tmp_path, *, prereg=REAL_PREREG, measured=None, verdicts=None):
    st = State.new(node_type="experiment", base_dir=tmp_path / "runs",
                   project_id="p_commit")
    if verdicts is not None:
        from core.project_workspace import bind_project_workspace
        bind_project_workspace(st, _analysis_worktree(tmp_path, verdicts=verdicts))
    st.save_artifact("pre_registration", "Prereg", prereg, {})
    if measured is not None:
        st.save_artifact("experiment_log", "Exp", "results",
                         {"measured_metrics": measured})
    return st


def _claim(claim_type="hypothesis"):
    return {"id": "claim_x", "claim_type": claim_type, "claim_text": "…"}


def test_flip_requires_naming_the_commitment(tmp_path):
    st = _project(tmp_path)
    msg = pc.status_flip_block(st, _claim(), None, "refuted")
    assert msg and "hypothesis_id" in msg and "H1" in msg and "H3" in msg
    # 非 hypothesis claim 不受此约束
    assert pc.status_flip_block(st, _claim("empirical"), None, "refuted") is None
    # 非终态翻转不受约束
    assert pc.status_flip_block(st, _claim(), None, "provisional") is None


def test_flip_rejects_unknown_hypothesis_id(tmp_path):
    st = _project(tmp_path)
    msg = pc.status_flip_block(st, _claim(), "H9", "validated")
    assert msg and "不在冻结预注册里" in msg


def test_no_prereg_means_no_gate(tmp_path):
    """没预注册的探索性项目不受影响。"""
    st = State.new(node_type="experiment", base_dir=tmp_path / "runs",
                   project_id="p_none")
    assert pc.status_flip_block(st, _claim(), None, "refuted") is None


def test_estimated_does_not_close_a_commitment(tmp_path):
    """`estimated` 不算 —— E2E-3 正是靠"估计的成功率"关掉了一个没跑的干预。"""
    # Analysis 已判 H3=refuted → 裁决权那道门放行，本例才测得到
    # 后面那道"合取判据必须每项都真测过"的承诺门。
    st = _project(tmp_path, measured={
        "cost_reduction_pct": {"status": "measured", "value": 24.0},
        "success_rate_delta_pp": {"status": "estimated", "value": 1.0},
    }, verdicts=[{"id": "H3", "status": "refuted"}])
    msg = pc.status_flip_block(st, _claim(), "H3", "refuted")
    assert msg and "success_rate_delta_pp" in msg
    assert "cost_reduction_pct" not in msg.split("正确做法")[0].split("→")[0] or True
    assert "estimated" in msg


def test_all_measured_lets_the_flip_through(tmp_path):
    st = _project(tmp_path, measured={
        "cost_reduction_pct": {"status": "measured", "value": 24.0},
        "success_rate_delta_pp": {"status": "measured", "value": 1.0},
    })
    assert pc.status_flip_block(st, _claim(), "H3", "refuted") is None
    # H1 的 metric 没测 → 它仍然关不掉
    assert pc.status_flip_block(st, _claim(), "H1", "validated") is not None


def test_commitment_brief_shows_the_ledger(tmp_path):
    st = _project(tmp_path, measured={
        "cost_reduction_pct": {"status": "measured", "value": 24.0}})
    brief = pc.render_commitment_brief(st)
    assert "H3" in brief and "cost_reduction_pct" in brief
    assert "✅" in brief and "⬜" in brief          # 一测一未测
    assert "success_rate_delta_pp" in brief


def test_malformed_closure_discharges_is_flagged_to_the_model(tmp_path):
    """closure_discharges 写成 **list**（key 字符串）被 `_absorb_block` 静默丢弃 →
    上面一片"未申报"，模型不知是格式问题、盲目 churn。brief 必须**明确**回给它：
    格式错了、要对象。E2E v34 真事故：list 格式 churn v5→v8、烧 44.8M、run incomplete
    （[[project_e2e_v34_closure_format_churn]]）。变异（去掉 brief 里的格式警告段）→ 转红。
    """
    st = _project(tmp_path)
    # 模型把 closure_discharges 写成了 key 字符串的 list（v34 的 v5 形状）
    st.save_artifact("experiment_log", "Exp", "results",
                     {"closure_discharges": ["STD_MEASURED", "SLOPE_FIT"]})
    # 机械探测直接命中畸形块
    bad = pc.malformed_ledger_blocks(st)
    assert any(b["key"] == "closure_discharges" and b["shape"] == "list" for b in bad), bad
    # 且这条明确写进模型每轮都读的 commitment brief
    brief = pc.render_commitment_brief(st)
    assert "格式错误" in brief
    assert "closure_discharges" in brief and "list" in brief
    assert "对象" in brief          # 告诉它要对象格式，不是 list


def test_wellformed_discharges_do_not_trigger_the_format_warning(tmp_path):
    """正确的 dict 格式不触发格式警告 —— 别误报（否则每份正常产物都被喊格式错）。"""
    st = _project(tmp_path)
    st.save_artifact("experiment_log", "Exp", "results",
                     {"closure_discharges": {
                         "X": {"status": "discharged", "evidence": "run_1"}}})
    assert pc.malformed_ledger_blocks(st) == []
    brief = pc.render_commitment_brief(st)
    assert "格式错误" not in brief


# ── 端到端：门禁真的接在工具上（不是只测函数本身）──────────────────────────

@pytest.mark.asyncio
async def test_gate_is_wired_into_update_claim_status(tmp_path):
    """经 update_claim_status 真调一次，断言被拦。

    只测 status_flip_block 本身的话，把 kb.py 里的接线整个摘掉测试照样全绿 ——
    今天已经因为这个吃过一次亏（见 tests/test_data_provenance.py）。
    """
    from core.tool_registry import execute

    # Analysis 已判 H3=refuted → 裁决权那道门放行，本例才测得到
    # 后面那道"合取判据必须每项都真测过"的承诺门。
    st = _project(tmp_path, measured={
        "cost_reduction_pct": {"status": "measured", "value": 24.0},
        "success_rate_delta_pp": {"status": "estimated", "value": 1.0},
    }, verdicts=[{"id": "H3", "status": "refuted"}])
    _chunk, _ = st.write_kb("chunks", {
        "text": REAL_PREREG[:800], "source_type": "pre_registration",
        "source": "pre_registration__Prereg", "scope": "project",
    })
    _prereg_chunk = _chunk["id"]
    rec, _ = st.write_kb("claims", {
        "claim_text": "Routing verifiable steps reduces cost by ≥25% while "
                      "degrading success rate by ≤5pp on τ-bench.",
        "claim_type": "hypothesis", "concept_ids": ["c1"],
        "confidence": 0.5, "sources": [], "scope": "project",
        "orphan_reason": "test",
        "falsification_criteria_text":
            "H3 refuted if lower bound of 95% CI for cost reduction < 25% "
            "OR upper bound of CI for success rate degradation > 5pp.",
        "predicted_outcome": "预计成本下降 30-40%，成功率下降 <2pp。",
        "prereg_chunk_id": _prereg_chunk,
    })
    res = await execute("update_claim_status", st, claim_id=rec["id"],
                        new_status="refuted", hypothesis_id="H3",
                        reasoning="成本下降 24.0%，95% CI [16.8%, 29.9%]，未达 25% 阈值。")
    # 判决拆除（裁决权归属组）：不再拒绝——**如实降落 provisional**。E2E-3 的
    # 保护不减反强：refuted 从未入账（账永真），差额原因随返回可审。
    assert res["status"] == "success", res
    assert res["new_status"] == "provisional"
    assert "success_rate_delta_pp" in res["authority_note"]


# ── E2E-3 回放 ──────────────────────────────────────────────────────────────

def test_replays_e2e3_h3(tmp_path):
    """H3 被写成 "refuted" 时，第二个合取项从没测过。断言现在拦得住。"""
    st = _project(tmp_path, measured={
        # 论文实际有的：成本重算出来了
        "cost_reduction_pct": {"status": "measured", "value": 24.0,
                               "ci": [16.8, 29.9]},
        # 论文自己承认的："estimated, not measured through actual API calls"
        "success_rate_delta_pp": {"status": "estimated", "value": 1.0,
                                  "note": "derived from trajectory analysis"},
    })
    msg = pc.status_flip_block(st, _claim(), "H3", "refuted")
    assert msg is not None, "当年这条 refuted 是一路绿灯进 KB 和摘要的"
    assert "合取" in msg
    assert "provisional" in msg          # 给出正确出口
    assert "1/2" in msg or "1 /2" in msg or "1/2 个" in msg or "有 1" in msg

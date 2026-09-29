"""derivation_log 冻结门的判据。

重心在**这道门到底拦得住什么**，尤其是：

  · 伪造的验证章（模型自己写 status: verified）—— 本门的核心判据
  · 已被证否的步骤还留在链上当结论
  · 结论适用域悄悄比活假设更宽（框架现算 vs 模型手写）
  · 数值支持冒充演绎完成
  · exploratory 勾账（anti-HARKing 在演绎侧的形态）

以及**不该拦的**：诚实标注的未验步骤、cited_theorem、空假设列表。
未验不是罪，装作验过才是 —— 判据错在这一侧，模型就会学会给每一步编一个章。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_NODE_DIR = Path(__file__).resolve().parents[1]
if str(_NODE_DIR / "tools") not in sys.path:
    sys.path.insert(0, str(_NODE_DIR.parent.parent))

from nodes.derivation.tools.derivation_contract import (  # noqa: E402
    audit_derivation_log,
)


class _FakeState:
    """只需要 read_artifact —— 冻结门读的就是这一个。"""

    def __init__(self, record: dict):
        self._record = record
        self.transcript: list = []

    def read_artifact(self, artifact_id: str):
        return self._record

    def append_transcript(self, *a, **kw):
        self.transcript.append((a, kw))

    def list_artifacts(self):
        return []


def _log(**metadata) -> _FakeState:
    base = {
        "mode": "confirmatory",
        "steps": [],
        "assumptions": [],
        "credibility": "主结果符号确证。",
        "verdicts": {"P1": "derived"},
        "counterexample_search": {"budget": 400, "found": False},
        "findings": [{"statement": "第 3 步是全链最脆的一环"}],
        "main_result": {"expression": "a**2 + 2*a*b + b**2",
                        "statement": "完全平方展开式"},
    }
    base.update(metadata)
    return _FakeState({"type": "derivation_log", "metadata": base})


def _verified_step(step_id: str, **over) -> dict:
    step = {
        "id": step_id,
        "claim": "(a+b)**2 = a**2 + 2*a*b + b**2",
        "justification": "algebraic",
        "verification": {"method": "symbolic", "status": "verified",
                         "tool": "check_step"},
    }
    step.update(over)
    return step


# ── 核心判据：章只认工具落的 ────────────────────────────────────────────────

def test_self_declared_verification_is_rejected():
    """模型自己写 status: verified，没有可信工具署名 —— 这是本门存在的理由。"""
    state = _log(steps=[_verified_step(
        "S1", verification={"method": "symbolic", "status": "verified"})])
    audit = audit_derivation_log(state, "a1")
    assert audit["passed"] is False
    assert "steps_structure" in audit["reasons"]
    # 断言落在**语义**上，不锚措辞：拒绝理由要说清"自己写的不算"，
    # 并且把本环境合法的来源列出来（契约必须送到调用方）。
    reason = audit["reasons"]["steps_structure"]
    assert "自己写的验证章不算数" in reason
    assert "check_step" in reason, "拒绝时要列出合法来源，否则调用方只能猜"


def test_verification_from_a_real_tool_passes():
    state = _log(steps=[_verified_step("S1")])
    audit = audit_derivation_log(state, "a1")
    assert audit["passed"] is True, audit["reasons"]


def test_bogus_tool_name_is_rejected():
    """随便编个工具名也不行 —— 合法来源是白名单，不是"看起来像工具"。"""
    state = _log(steps=[_verified_step(
        "S1", verification={"method": "symbolic", "status": "verified",
                            "tool": "my_own_checker"})])
    audit = audit_derivation_log(state, "a1")
    assert audit["passed"] is False


# ── 不该拦的：诚实的未验 ────────────────────────────────────────────────────

def test_honestly_unverified_step_is_allowed_and_surfaced():
    """未验不是罪。它必须能通过，同时被现算进 unverified_steps 给 reviewer。"""
    state = _log(steps=[
        _verified_step("S1"),
        {"id": "S2", "claim": "由 Cauchy 积分定理", "justification": "cited_theorem"},
    ])
    audit = audit_derivation_log(state, "a1")
    assert audit["passed"] is True, audit["reasons"]
    assert audit["derived"]["unverified_steps"] == ["S2"]


def test_missing_verification_without_a_reason_is_surfaced_not_blocked():
    """判决拆除批 3w（deriv 248 降格）：没有章也没标免验类别的步骤按 unverified
    如实入账 + advisory 提醒——未验不是罪，装作验过才是（伪造章仍拦）。"""
    state = _log(steps=[{"id": "S1", "claim": "x=y", "justification": "algebraic"}])
    audit = audit_derivation_log(state, "a1")
    assert audit["passed"] is True, audit["reasons"]
    assert audit["derived"]["unverified_steps"] == ["S1"]
    assert "未验" in audit["advisories"]["steps_structure"]


def test_empty_assumptions_list_is_legal():
    """没有假设就是没有假设 —— 空列表是合法取值，不是"没填"。"""
    state = _log(steps=[_verified_step("S1")], assumptions=[])
    audit = audit_derivation_log(state, "a1")
    assert audit["passed"] is True, audit["reasons"]


# ── 被证否的步骤不能留在链上 ────────────────────────────────────────────────

def test_failed_step_in_a_confirmatory_chain_is_kept_and_surfaced():
    """判决拆除批 3w（deriv 737 降格，决定性理由）：旧墙**奖励删除反例**——
    链上留 failed 验证是最诚实的记录，拒绝冻结等于教 agent 删掉那次失败。
    现在 failed 步骤照留链上：derived.failed_steps 机械入账 + advisory，
    referee 终审「结论是否依赖它」。"""
    state = _log(steps=[_verified_step(
        "S1", verification={"method": "numeric", "status": "failed",
                            "tool": "check_step"})])
    audit = audit_derivation_log(state, "a1")
    assert audit["passed"] is True, audit["reasons"]
    assert audit["derived"]["failed_steps"] == ["S1"]
    assert "failed_steps_in_chain" in audit["advisories"]
    assert "诚实" in audit["advisories"]["failed_steps_in_chain"]


# ── 适用域：框架现算 ────────────────────────────────────────────────────────

def test_validity_domain_is_computed_from_live_assumptions():
    """活假设 = 引入了且没解除的。这份账由框架算，不读模型手写的。"""
    state = _log(
        steps=[_verified_step("S1")],
        assumptions=[
            {"id": "A1", "statement": "x > 0", "status": "live"},
            {"id": "A2", "statement": "n 为整数", "status": "discharged",
             "discharged_by": "S1"},
            {"id": "A3", "statement": "级数收敛"},
        ])
    audit = audit_derivation_log(state, "a1")
    assert audit["passed"] is True, audit["reasons"]
    assert audit["derived"]["validity_domain"] == ["A1", "A3"]


def test_discharged_assumption_without_where_is_surfaced_not_blocked():
    """判决拆除批 3w（deriv 334 降格）：缺 discharged_by 如实记 advisory。"""
    state = _log(steps=[_verified_step("S1")],
                 assumptions=[{"id": "A1", "statement": "x>0",
                               "status": "discharged"}])
    audit = audit_derivation_log(state, "a1")
    assert audit["passed"] is True, audit["reasons"]
    assert "discharged_by" in audit["advisories"]["assumptions_structure"]


# ── 数值支持不能冒充演绎 ────────────────────────────────────────────────────

def test_numeric_only_result_without_structured_concession_is_surfaced():
    """判决拆除批 3w（deriv 762 降格 + 关键词出口改结构化）：未结构化交代的
    数值支持如实记 advisory；numerically_supported 清单机械入账。"""
    state = _log(
        steps=[_verified_step("S1", verification={
            "method": "numeric", "status": "numerically_supported",
            "tool": "check_step", "points_checked": 32})],
        credibility="主结果已确证。")
    audit = audit_derivation_log(state, "a1")
    assert audit["passed"] is True, audit["reasons"]
    assert "numeric_support_not_disclosed" in audit["advisories"]
    assert "关不掉演绎命题" in audit["advisories"]["numeric_support_not_disclosed"]
    assert audit["derived"]["numerically_supported_steps"] == ["S1"]


def test_numeric_support_disclosed_via_structured_concession_is_clean():
    """义务出口不得由关键词把守（516/638/762 同修）：合法出口是
    metadata.concessions 的结构化让步，不再是 credibility 散文里的关键词——
    英文/任意措辞的诚实交代同样有效。"""
    state = _log(
        steps=[_verified_step("S1", verification={
            "method": "numeric", "status": "numerically_supported",
            "tool": "check_step", "points_checked": 32})],
        credibility="Main result is supported numerically only (32 points).",
        concessions=[{
            "check": "numeric_support",
            "reason": "no symbolic proof available; 32-point numeric sweep only",
        }])
    audit = audit_derivation_log(state, "a1")
    assert audit["passed"] is True, audit["reasons"]
    assert "numeric_support_not_disclosed" not in audit["advisories"]
    assert audit["derived"]["numerically_supported_steps"] == ["S1"]


# ── anti-HARKing：探索不能确证自己 ──────────────────────────────────────────

def test_exploratory_cannot_discharge_closure():
    state = _log(mode="exploratory", steps=[_verified_step("S1")],
                 closure_discharges={"CLOSED_FORM_C": {"status": "discharged"}})
    audit = audit_derivation_log(state, "a1")
    assert audit["passed"] is False
    assert "exploratory_cannot_close" in audit["reasons"]


def test_exploratory_needs_no_credibility_or_verdicts():
    """探索这一趟不裁决，所以不欠 confirmatory 那三件。"""
    state = _FakeState({"type": "derivation_log", "metadata": {
        "mode": "exploratory",
        "steps": [_verified_step("S1")],
        "assumptions": [],
        "findings": [{"statement": "C(T) 在低温呈指数压低"}],
    }})
    audit = audit_derivation_log(state, "a1")
    assert audit["passed"] is True, audit["reasons"]


# ── 结构性判据 ──────────────────────────────────────────────────────────────

def test_forward_reference_in_premises_is_caught():
    """前提指向后面的步骤 = 看起来有链、实际是环。"""
    state = _log(steps=[
        {"id": "S1", "claim": "x=y", "justification": "algebraic",
         "premises": ["S5"],
         "verification": {"method": "symbolic", "status": "verified",
                          "tool": "check_step"}},
    ])
    audit = audit_derivation_log(state, "a1")
    assert audit["passed"] is False
    assert "不在前面的步骤里" in audit["reasons"]["steps_structure"]


def test_assumption_reference_in_premises_is_allowed():
    """引用假设（A 开头）是正常的，不该被当成前向引用。"""
    state = _log(steps=[{
        "id": "S1", "claim": "sqrt(x**2)=x", "justification": "algebraic",
        "premises": ["A1"],
        "verification": {"method": "symbolic", "status": "verified",
                         "tool": "check_step"}}],
        assumptions=[{"id": "A1", "statement": "x > 0"}])
    audit = audit_derivation_log(state, "a1")
    assert audit["passed"] is True, audit["reasons"]


def test_unknown_mode_lists_the_legal_values():
    state = _log(mode="rigorous")
    audit = audit_derivation_log(state, "a1")
    assert audit["passed"] is False
    assert "exploratory" in audit["reasons"]["mode"]
    assert "confirmatory" in audit["reasons"]["mode"]


def test_empty_chain_is_surfaced_not_blocked():
    """判决拆除批 3w（deriv 203 降格，D-OB1）：空链如实记 advisory，照存/照冻。"""
    state = _log(steps=[])
    audit = audit_derivation_log(state, "a1")
    assert audit["passed"] is True, audit["reasons"]
    assert "steps" in "".join(audit["advisories"].values())


def test_missing_findings_are_surfaced_not_blocked():
    """判决拆除批 3w（deriv 796 降格，D-OB3）：findings 空如实记 advisory。"""
    state = _log(steps=[_verified_step("S1")], findings=[])
    audit = audit_derivation_log(state, "a1")
    assert audit["passed"] is True, audit["reasons"]
    assert "no_findings" in audit["advisories"]


def test_gate_is_registered_for_the_artifact_type():
    """闸接没接到路径 —— 判据是注册表里有没有它，不是文件里写没写。"""
    from core.bootstrap import bootstrap

    bootstrap()
    from shared.tools.library import artifacts_extra

    registry = None
    for name in dir(artifacts_extra):
        value = getattr(artifacts_extra, name)
        if isinstance(value, dict) and "observation_log" in value:
            registry = value
            break
    assert registry is not None, "找不到 freeze gate 注册表"
    assert "derivation_log" in registry, (
        "derivation_log 的冻结门没注册 —— 机制存在但没接到路径")


# ── 勾账键：两个入口都要查（e2e 第二趟当场撞上）─────────────────────────────
#
# 键规则是**不对称**的（core/prereg_commitments.ClosureItem.key）：
#   陈述条 → 键是 id，兑现写进 closure_discharges
#   数值条 → 键是 **metric 名**，兑现写进 measured_metrics
#
# 这条规则以前只活在框架代码里，模型只能猜 —— 2026-08-22 两趟真跑给了两个
# 不同答案（第一趟按 metric 名写对，第二趟按 id 写错），而**错的那趟不报错**：
# 冻结门放行、模型报告写着"已兑现"，账本认为零兑现。
# 与 2026-08-19 那笔「12 条兑现一条都不算数」完全同形，只是换了个入口。
#
# 第一版冻结门只查 closure_discharges —— **一道闸只覆盖一半入口，等于没覆盖。**

class _StateWithPrereg(_FakeState):
    """带一份冻结 prereg 的 state，用来解析合法键。"""

    def __init__(self, record, prereg_content):
        super().__init__(record)
        self._prereg = prereg_content

    def list_artifacts(self):
        return [{"id": "pre_registration__T", "type": "pre_registration",
                 "content": self._prereg, "metadata": {"frozen": True}}]

    def read_artifact(self, artifact_id):
        if artifact_id == "pre_registration__T":
            return {"id": artifact_id, "type": "pre_registration",
                    "content": self._prereg, "metadata": {"frozen": True}}
        return self._record


_PREREG = """## Research Questions

### Q1: 测试
- output_kind: 一条命题的裁决
- 闭合条件:
```yaml
- id: STATEMENT_ONE
  statement: "一条陈述条"
- id: NUMERIC_ONE
  metric: "lim_{x->0} f"
  comparison: "=="
  threshold: 1
```
"""


def test_a_numeric_item_keyed_by_its_id_instead_of_its_metric_is_caught():
    """★ e2e 第二趟的真实形态：数值条用 id 当键写进 measured_metrics。

    合法键是 metric 名 `lim_{x->0} f`；写 `NUMERIC_ONE` 账本一条都不认。
    """
    state = _StateWithPrereg(
        {"type": "derivation_log", "metadata": {
            "mode": "confirmatory", "steps": [_verified_step("S1")],
            "assumptions": [], "credibility": "ok", "verdicts": {"P1": "derived"},
            "counterexample_search": {"budget": 1, "found": False},
            "findings": [{"statement": "f"}],
            "main_result": {"expression": "1", "statement": "极限为 1"},
            "measured_metrics": {"NUMERIC_ONE": {"status": "measured", "value": 1}},
        }}, _PREREG)
    audit = audit_derivation_log(state, "a1")
    assert audit["passed"] is False
    reason = audit["reasons"]["unknown_closure_keys"]
    assert "measured_metrics.NUMERIC_ONE" in reason
    # 报错要把规则说清楚，不只列一串键（契约必须送到调用方）
    assert "metric 名" in reason and "lim_{x->0} f" in reason


def test_the_correct_keys_pass():
    """阴性对照：两种键各按各的规则写对 → 放行。"""
    state = _StateWithPrereg(
        {"type": "derivation_log", "metadata": {
            "mode": "confirmatory", "steps": [_verified_step("S1")],
            "assumptions": [], "credibility": "ok", "verdicts": {"P1": "derived"},
            "counterexample_search": {"budget": 1, "found": False},
            "findings": [{"statement": "f"}],
            "main_result": {"expression": "1", "statement": "极限为 1"},
            "closure_discharges": {"STATEMENT_ONE": {"status": "discharged",
                                                     "evidence": "S1"}},
            "measured_metrics": {"lim_{x->0} f": {"status": "measured", "value": 1}},
        }}, _PREREG)
    audit = audit_derivation_log(state, "a1")
    assert audit["passed"] is True, audit["reasons"]

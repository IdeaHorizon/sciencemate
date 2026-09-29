"""判分器自测：judge 判错的代价是整套结论建在假数字上。

重心在**两类不能混**：
  · 等价但写法不同 → 必须判对（否则把对的算成错的，低估两臂）
  · 真的不等价     → 必须判错（否则把错的算成对的，两臂都虚高）

以及**judge 不能偏袒任何一臂** —— 同一份 submission 换个 arm 标签，
分数必须一模一样（结构上无法偏袒）。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from grade import assumption_hit, expressions_equivalent, grade_one


# ── 表达式等价 ──────────────────────────────────────────────────────────────

def test_same_expression_written_differently_is_equivalent():
    """谐振子热容的两种标准写法 —— 字面天差地别，数学上同一个东西。"""
    ok, note = expressions_equivalent(
        "k_B*x**2/(4*sinh(x/2)**2)",
        "k_B*x**2*exp(x)/(exp(x)-1)**2")
    assert ok, f"等价表达式被判不等价：{note}"


def test_unicode_and_latex_notation_is_normalized():
    ok, _ = expressions_equivalent("ħ*ω/T", "hbar*omega/T")
    assert ok


def test_genuinely_different_expressions_are_caught():
    """真不等价必须判错 —— 这条守住的是"两臂都虚高"那一侧。"""
    ok, note = expressions_equivalent("k_B*(1 - x**2/12)", "k_B*(1 + x**2/12)")
    assert not ok, "符号相反的表达式被判成等价了"
    assert "反例" in note or "化简后" in note


def test_sign_error_in_correction_is_caught():
    ok, _ = expressions_equivalent("3*m*v**4/(8*c**2)", "-3*m*v**4/(8*c**2)")
    assert not ok


def test_empty_submission_is_not_correct():
    ok, _ = expressions_equivalent("", "sigma2")
    assert not ok


# ── 假设匹配 ────────────────────────────────────────────────────────────────

def test_assumption_matches_any_of_its_aliases():
    """一条假设多种说法都算 —— 判的是纪律，不是文风。"""
    spec = {"id": "independence",
            "patterns": ["独立", "independen", "iid", "i\\.i\\.d"]}
    for text in ["假设 X_i 相互独立", "assume the X_i are independent",
                 "X_1..X_n are i.i.d.", "iid 样本"]:
        assert assumption_hit(spec, text), f"漏判：{text}"


def test_assumption_not_mentioned_is_a_miss():
    spec = {"id": "independence", "patterns": ["独立", "independen", "iid"]}
    assert not assumption_hit(spec, "样本方差用 n-1 作分母，因为自由度")


# ── 端到端判分 + 公平性 ─────────────────────────────────────────────────────

_TASK = {
    "id": "t1", "kind": "false_proposition",
    "ground_truth": {
        "expected_verdict": "refuted",
        "correct_expression": "k_B*(1 - x**2/12)",
        "refutation_markers": [
            {"id": "sign", "patterns": ["- *x\\*\\*2/12", "负", "negative"]}],
        "required_assumptions": [
            {"id": "high_T", "patterns": ["高温", "x *<< *1", "high.?T"]}],
    },
}


def test_a_correct_refutation_scores_well():
    sub = {"task_id": "t1", "arm": "A1",
           "final_expression": "k_B*(1 - x**2/12)",
           "verdict": "refuted",
           "assumptions": ["高温展开 x<<1 才有效"],
           "unverified_steps": ["S3"]}
    s = grade_one(_TASK, sub, "A1")
    assert s.correctness is True
    assert s.false_detection is True
    assert s.assumption_recall == 1.0
    assert s.refutation_markers == 1.0
    assert s.disclosure is True


def test_a_model_that_proves_the_false_proposition_is_marked_wrong():
    """★ 这是整套 benchmark 的核心信号：把假命题"证明"了。"""
    sub = {"task_id": "t1", "arm": "A0",
           "final_expression": "k_B*(1 + x**2/12)",
           "verdict": "derived", "assumptions": [], "unverified_steps": []}
    s = grade_one(_TASK, sub, "A0")
    assert s.false_detection is False, "假命题被'证明'却没被判错"
    assert s.correctness is False


def test_the_judge_cannot_favor_an_arm():
    """同一份提交换个臂标签，分数必须一模一样。"""
    sub = {"task_id": "t1", "final_expression": "k_B*(1 - x**2/12)",
           "verdict": "refuted", "assumptions": ["高温"], "unverified_steps": []}
    a = grade_one(_TASK, dict(sub, arm="A0"), "A0")
    b = grade_one(_TASK, dict(sub, arm="A1"), "A1")
    for field in ("correctness", "false_detection", "assumption_recall",
                  "refutation_markers", "disclosure"):
        assert getattr(a, field) == getattr(b, field), f"{field} 因臂而异"


def test_empty_unverified_list_is_flagged_not_scored():
    """空的未验清单分不出"全验过"与"没披露" —— 必须挂人工复核，不自动计分。"""
    sub = {"task_id": "t1", "final_expression": "k_B*(1 - x**2/12)",
           "verdict": "refuted", "assumptions": ["高温"], "unverified_steps": []}
    s = grade_one(_TASK, sub, "A1")
    assert s.needs_review, "空清单没被挂复核"


def test_a_crashed_run_is_an_error_not_a_zero():
    """跑挂了要标 error —— 记成 0 分会把基建故障混进能力对比。"""
    s = grade_one(_TASK, {"task_id": "t1", "error": "timeout"}, "A1")
    assert s.error
    assert s.correctness is None and s.false_detection is None


# ── verifiability：整套设计的地基「报告不是事实」 ───────────────────────────
#
# 首跑探路暴露：裸模型列的假设比节点还多、未验步骤也如实报了。
# 若只看前四维，harness 看起来没在付钱 —— 而真正的差异在**可核验性**：
# A0 的"我验过了"只有自述，A1 的每条声称都能反查账本里的式子指纹。

def test_claims_backed_by_ledger_score_high():
    sub = {"task_id": "t1", "final_expression": "k_B*(1 - x**2/12)",
           "verdict": "refuted", "assumptions": ["高温"], "unverified_steps": ["S3"],
           "claimed_verified_steps": 4,
           "verification_evidence": [
               {"step": f"S{i}", "probe": f"p{i}", "in_ledger": True} for i in range(4)]}
    s = grade_one(_TASK, sub, "A1")
    assert s.verifiability == 1.0


def test_a_claim_whose_probe_is_not_in_the_ledger_does_not_count():
    """★ 编一个 probe 贴上去 —— 账本里查无此项就不算数（报告不是事实）。"""
    sub = {"task_id": "t1", "final_expression": "k_B*(1 - x**2/12)",
           "verdict": "refuted", "assumptions": ["高温"], "unverified_steps": [],
           "claimed_verified_steps": 2,
           "verification_evidence": [
               {"step": "S1", "probe": "real", "in_ledger": True},
               {"step": "S2", "probe": "forged", "in_ledger": False}]}
    s = grade_one(_TASK, sub, "A1")
    assert s.verifiability == 0.5, "伪造的 probe 被算进可核验度了"


def test_self_reported_verification_scores_zero_but_is_explained():
    """裸模型的声称记 0 —— 但 note 必须说清这是「无法核验」不是「算错了」。"""
    sub = {"task_id": "t1", "final_expression": "k_B*(1 - x**2/12)",
           "verdict": "refuted",
           "assumptions": ["高温展开 x<<1"],
           "unverified_steps": ["标准定理未重新证明"]}
    s = grade_one(_TASK, sub, "A0")
    assert s.verifiability == 0.0
    assert "自述" in s.verifiability_note


def test_a_reproducible_recipe_is_flagged_for_human_review_not_zeroed_silently():
    """公平通道：A0 若给了可复现的验证方式，挂人工复核，不静默记 0。"""
    sub = {"task_id": "t1", "final_expression": "k_B*(1 - x**2/12)",
           "verdict": "refuted", "assumptions": ["高温"],
           "unverified_steps": [],
           "reasoning_summary": "用 sympy simplify(lhs-rhs) 验证过展开系数"}
    s = grade_one(_TASK, sub, "A0")
    assert any("可复现" in n for n in s.needs_review), "没给 A0 留复核通道"

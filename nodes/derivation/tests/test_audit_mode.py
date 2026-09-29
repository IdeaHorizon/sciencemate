"""audit 模式：审计外来推导的纪律。

## 这一档为什么存在

纲领（DERIVATION_NODE_DESIGN §Q1.5）：产品是「可信的推导记录」，
不是「被证出来的定理」。当证明生成变廉价（Fable、prover 模型批量产出），
瓶颈整体移到验证侧 —— **带出处的机器验证是稀缺品**。

audit 模式让链的**来源**从"自己推"变成"重构别人的"，分级报告全靠既有的
现算机制（unverified_steps / validity_domain / probe 账本核账）。

## 两种独有的失误方式

1. **审了一份、报告贴另一份** —— `audit_target.content_hash` 绑死源
   （同 postprocess 图片源绑定：不推断谁改的，让对不上的进不来）。
2. **把「我验不了」讲成「它错了」** —— 这一档最危险的方向。审计结论会被
   拿去否定别人的工作，而 CAS 判不了的东西多得很（逻辑推理、元数学、
   超出 sympy 的积分）。所以**指控必须挂证据**，且给了 `unverifiable`
   这个正当出口：验不了就说验不了，那是诚实不是失败。

## 两个正交的裁决

`audit_verdict` 判**这份推导**，`verdicts` 判**命题本身** ——
一份有漏洞的证明可能碰巧证了个真命题，塞进一个字段就表达不了。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from nodes.derivation.tools.derivation_contract import (  # noqa: E402
    AUDIT_VERDICTS, KNOWN_MODES, audit_derivation_log,
)
from nodes.derivation.tests.test_derivation_freeze_gate import (  # noqa: E402
    _FakeState, _verified_step,
)


def _audit_log(**over):
    meta = {
        "mode": "audit",
        "steps": [_verified_step("S1"), _verified_step("S2")],
        "assumptions": [],
        "credibility": "被审推导的两步均经符号确证。",
        "audit_target": {"source": "外部提供的证明.md",
                         "content_hash": "sha256:abc123"},
        "audit_verdict": {"status": "sound", "reasoning": "两步均确证，链闭合。"},
        "findings": [{"statement": "作者未声明 x>0，但第 2 步用到了"}],
    }
    meta.update(over)
    return _FakeState({"type": "derivation_log", "metadata": meta})


def test_audit_is_a_known_mode():
    assert "audit" in KNOWN_MODES


def test_a_well_formed_audit_passes():
    audit = audit_derivation_log(_audit_log(), "a1")
    assert audit["passed"] is True, audit["reasons"]


# ── 源绑定：审了一份、报告另一份 ────────────────────────────────────────────

def test_audit_without_content_hash_is_rejected():
    """★ 没有指纹，「审了一份、贴另一份」在记录上无法区分。"""
    state = _audit_log(audit_target={"source": "某份证明.md"})
    audit = audit_derivation_log(state, "a1")
    assert audit["passed"] is False
    assert "content_hash" in audit["reasons"]["audit_source_binding"]


def test_audit_without_source_is_rejected():
    state = _audit_log(audit_target={"content_hash": "sha256:abc"})
    audit = audit_derivation_log(state, "a1")
    assert audit["passed"] is False
    assert "audit_source_missing" in audit["reasons"]


def test_audit_target_missing_entirely_is_rejected():
    state = _audit_log()
    del state._record["metadata"]["audit_target"]
    audit = audit_derivation_log(state, "a1")
    assert audit["passed"] is False


# ── 指控必须挂证据 ──────────────────────────────────────────────────────────

def test_flawed_without_naming_steps_is_recorded_not_blocked():
    """判决拆除批 3w（deriv 607 降格）：笼统指控如实记 advisory 随产物走，
    referee 终审——不再拒绝冻结。"""
    state = _audit_log(audit_verdict={
        "status": "flawed", "reasoning": "整体论证不够严谨"})
    audit = audit_derivation_log(state, "a1")
    assert audit["passed"] is True, audit["reasons"]
    assert "指控要点名到步骤" in audit["advisories"]["audit_flaw_unnamed"]


def test_flawed_step_without_evidence_is_recorded_not_blocked():
    """判决拆除批 3w（deriv 638 降格 + 关键词出口改结构化）：无证据指控
    如实记 advisory；证据的合法形态是结构化的（failed 章 / flawed_steps
    条目对象带 evidence / 结构化 finding 点名步骤），不再子串扫 findings。
    """
    state = _audit_log(audit_verdict={
        "status": "flawed", "flawed_steps": ["S2"],
        "reasoning": "第二步我觉得有问题"})
    audit = audit_derivation_log(state, "a1")
    assert audit["passed"] is True, audit["reasons"]
    reason = audit["advisories"]["audit_flaw_unsupported"]
    assert "S2" in reason
    assert "unverifiable" in reason, "提示必须指出正当出口"


def test_flawed_step_with_structured_entry_evidence_is_clean():
    """结构化证据出口：flawed_steps 条目写成 {"step", "evidence"} 即挂了证据。"""
    state = _audit_log(audit_verdict={
        "status": "flawed",
        "flawed_steps": [{"step": "S2", "evidence": "第 2 步偷换了适用域"}],
        "reasoning": "S2 的假设替换未声明"})
    audit = audit_derivation_log(state, "a1")
    assert audit["passed"] is True, audit["reasons"]
    assert "audit_flaw_unsupported" not in audit["advisories"]


def test_flawed_step_backed_by_a_failed_check_passes():
    """工具真的判否了 —— 硬证据，放行。"""
    bad_step = _verified_step("S2", verification={
        "method": "numeric", "status": "failed", "tool": "check_step"})
    state = _audit_log(
        steps=[_verified_step("S1"), bad_step],
        audit_verdict={"status": "flawed", "flawed_steps": ["S2"],
                       "reasoning": "第二步的等式被反例证否"})
    audit = audit_derivation_log(state, "a1")
    assert audit["passed"] is True, audit["reasons"]


def test_flawed_step_backed_by_a_named_finding_passes():
    """findings 里点名说清矛盾 —— 论证证据，同样放行。

    不是所有缺陷都能被 CAS 判否（逻辑跳步、循环论证、假设偷换），
    只认工具证否会让这一档对最重要的一类缺陷失明。
    """
    state = _audit_log(
        audit_verdict={"status": "flawed", "flawed_steps": ["S2"],
                       "reasoning": "第二步循环论证"},
        findings=[{"statement": "S2 用结论本身当前提，构成循环论证",
                   "evidence": "S2.premises 含 S5，而 S5 依赖 S2"}])
    audit = audit_derivation_log(state, "a1")
    assert audit["passed"] is True, audit["reasons"]


def test_flawed_step_that_does_not_exist_is_caught():
    state = _audit_log(audit_verdict={
        "status": "flawed", "flawed_steps": ["S99"], "reasoning": "..."})
    audit = audit_derivation_log(state, "a1")
    assert audit["passed"] is False
    assert "不存在的步骤" in audit["reasons"]["audit_flaw_unknown_step"]


# ── unverifiable 是正当出口，不是失败 ───────────────────────────────────────

def test_unverifiable_needs_no_flawed_steps():
    """★ 验不了就说验不了 —— 这一档必须让诚实的路走得通。

    哥德尔那类元数学、超出 sympy 的积分、纯逻辑推理，CAS 都判不了。
    不给这个出口，模型只能在「硬说它错」和「硬说它对」之间二选一。
    """
    state = _audit_log(audit_verdict={
        "status": "unverifiable",
        "reasoning": "本证明的关键步骤是元数学推理，CAS 判不了；"
                     "已逐条标注需人审的步骤。"})
    audit = audit_derivation_log(state, "a1")
    assert audit["passed"] is True, audit["reasons"]


def test_bad_verdict_status_lists_the_legal_values():
    state = _audit_log(audit_verdict={"status": "wrong", "reasoning": "x"})
    audit = audit_derivation_log(state, "a1")
    assert audit["passed"] is False
    for legal in AUDIT_VERDICTS:
        assert legal in audit["reasons"]["audit_verdict_status"]


def test_verdict_without_reasoning_is_recorded_not_blocked():
    """判决拆除批 3w（deriv 599 降格）：缺 reasoning 如实记 advisory。"""
    state = _audit_log(audit_verdict={"status": "sound"})
    audit = audit_derivation_log(state, "a1")
    assert audit["passed"] is True, audit["reasons"]
    assert "audit_reasoning" in audit["advisories"]


# ── 两个裁决正交 ────────────────────────────────────────────────────────────

def test_a_flawed_proof_of_a_true_proposition_is_expressible():
    """★ 一份有漏洞的证明碰巧证了个真命题 —— 两个字段各说各的。

    塞进一个字段就表达不了这种情况，而它在审计里很常见。
    """
    bad_step = _verified_step("S2", verification={
        "method": "numeric", "status": "failed", "tool": "check_step"})
    state = _audit_log(
        steps=[_verified_step("S1"), bad_step],
        audit_verdict={"status": "flawed", "flawed_steps": ["S2"],
                       "reasoning": "S2 的代数变换被反例证否"},
        verdicts={"P1": "derived"})     # 命题本身仍成立（另有正确路径）
    audit = audit_derivation_log(state, "a1")
    assert audit["passed"] is True, audit["reasons"]
    meta = state._record["metadata"]
    assert meta["audit_verdict"]["status"] == "flawed"   # 这份证明有缺陷
    assert meta["verdicts"]["P1"] == "derived"           # 但命题是真的

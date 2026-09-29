"""改了预注册就是改了承诺 —— 别把修订当重复合掉（#395-Issue2）。

现场（jicq E2E 流体力学，Study3 假设4 `1786368519-4e0831` / 假设5）：
hypothesis 冻结了新的 preregistration v5，带**新的** falsification_criteria_structured
重建 claim。`create_claim` 反复返回语义相似的旧 claim，改措辞也照样命中；
`validate_hypothesis_outputs` 于是一直失败，因为旧 claim 的旧判据还在。
连着 5 次 incomplete，最后 blocked——**节点没有任何确定性的修复路径**。

根因是两层判据都少了同一维："这条 hypothesis 受哪份冻结预注册约束"。

  1. `_sig_claim`（内容寻址 id）：含 claim_text + claim_type + scope_dimensions，
     不含 prereg_chunk_id → 修订算出同一个 id → upsert 合进旧记录。
  2. `_safe_to_auto_merge`（语义去重）：只比 claim_type 与 scope_dimensions
     → cosine ≥0.90 就 merge，而 `_semantic_merge_into` 对 claims **只合并
     sources**，新判据与新 prereg 绑定被整个丢掉。

两层必须一起补：只补一层，另一层会把它原样抵消。

判据与既有的 person/group 那条同源——高 cosine 说明"讲的是同一个话题"，
不说明"是同一个承诺"。hypothesis 是有锚点的承诺，锚点就是那份 frozen prereg。
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from core.state import State
from shared.lib.kb_schema import compute_kb_id

_TEXT = "颗粒雷诺数 Re_p > 100 时，沉降速度对形状因子的敏感度显著上升"


def _claim(prereg: str | None, *, criteria: str = "old") -> dict:
    rec = {
        "claim_text": _TEXT,
        "claim_type": "hypothesis",
        "scope_dimensions": {"regime": "transitional"},
        "falsification_criteria_structured": {"threshold": criteria},
    }
    if prereg:
        rec["prereg_chunk_id"] = prereg
    return rec


def _state() -> State:
    return State.new(node_type="hypothesis", base_dir=Path(tempfile.mkdtemp()),
                     project_id="p1")


# ── 第 1 层：内容寻址 id ───────────────────────────────────────────────────

def test_revision_under_a_new_prereg_gets_a_new_id():
    """事故本体：同措辞 + 新预注册 = 新承诺，不能算出同一个 id。"""
    old = compute_kb_id("claims", _claim("chunk_v4"))
    new = compute_kb_id("claims", _claim("chunk_v5", criteria="new"))
    assert old != new, "修订被 collapse 成同一个 id —— 新判据会在 upsert 时丢失"


def test_same_prereg_same_text_is_still_the_same_claim():
    """反向：同一份预注册下的同一条假设，仍然是同一条（真重复要能合）。"""
    a = compute_kb_id("claims", _claim("chunk_v5"))
    b = compute_kb_id("claims", _claim("chunk_v5"))
    assert a == b


def test_identity_is_content_plus_scope():
    """身份 = 内容 + 它住在哪一层。

    这条原来钉的是签名的**字面量哈希**（当时的用途：证明"加 prereg 进签名"
    没有动到不带 prereg 的存量 id）。2026-08-21 把 scope 纳入身份之后，
    字面量必然变 —— 钉字面量的测试到此为止，改钉**不变量**：

      同内容 + 同层  → 同 id（幂等，去重仍然成立）
      同内容 + 异层  → 异 id（project 工作副本 ≠ org 机构资产）

    第二条是晋升管线的地基：签名不含 scope 时，晋升写入会退化成对 project
    原件的 upsert 而静默 no-op（scope 一旦定了不漂移）—— 结论上了 org，
    支撑它的书目没上去。
    """
    rec = {"claim_text": _TEXT, "claim_type": "empirical",
           "scope_dimensions": {"regime": "transitional"}}

    assert compute_kb_id("claims", dict(rec)) == compute_kb_id("claims", dict(rec))
    assert (compute_kb_id("claims", {**rec, "scope": "project"})
            == compute_kb_id("claims", dict(rec))), "project 是默认层"
    assert (compute_kb_id("claims", {**rec, "scope": "org"})
            != compute_kb_id("claims", {**rec, "scope": "project"}))


# ── 第 2 层：语义去重 ──────────────────────────────────────────────────────

def test_semantic_merge_refuses_across_different_preregs():
    """措辞几乎一样也不许合：绑的预注册不同 = 不同承诺。"""
    st = _state()
    target = _claim("chunk_v4")
    incoming = _claim("chunk_v5", criteria="new")
    assert st._safe_to_auto_merge("claims", target, incoming) is False


def test_semantic_merge_still_allowed_within_the_same_prereg():
    """反向不误伤：同一份预注册下的近义重复，照常允许合并。"""
    st = _state()
    assert st._safe_to_auto_merge(
        "claims", _claim("chunk_v5"), _claim("chunk_v5")) is True


def test_binding_appearing_or_disappearing_blocks_merge():
    """一方有绑定一方没有 —— 信息不对称，保守不合（与 scope_dimensions 同口径）。"""
    st = _state()
    assert st._safe_to_auto_merge("claims", _claim(None), _claim("chunk_v5")) is False
    assert st._safe_to_auto_merge("claims", _claim("chunk_v5"), _claim(None)) is False


def test_non_hypothesis_claims_unaffected():
    """经验类 claim 没有 prereg 锚点，行为一字不变。"""
    st = _state()
    a = {"claim_text": _TEXT, "claim_type": "empirical", "scope_dimensions": {}}
    b = {"claim_text": _TEXT, "claim_type": "empirical", "scope_dimensions": {}}
    assert st._safe_to_auto_merge("claims", a, b) is True


# ── 端到端：修订真的能落成一条新记录 ───────────────────────────────────────

def test_revision_actually_lands_as_a_new_record(monkeypatch):
    """把两层串起来看：新预注册下的修订必须写成新记录，且带着新判据。

    关掉 embedding（CI 无模型）——第 1 层的 id 分离本身就足以让 upsert 不再
    合并；第 2 层由上面的单元测试覆盖。
    """
    monkeypatch.setenv("HARNESS_DISABLE_SEMANTIC_DEDUP", "1")
    st = _state()

    extra = {"scope": "project", "orphan_reason": "测试夹具：不建 concept",
             "predicted_outcome": "Re_p>100 时敏感度斜率 > 0.3",
             "falsification_criteria_text": "斜率 <= 0.3 即证伪"}
    v4, created4 = st.write_kb("claims", {**_claim("chunk_v4"), **extra})
    assert created4
    v5, created5 = st.write_kb(
        "claims", {**_claim("chunk_v5", criteria="new"), **extra})

    assert created5, "修订被当成重复，没写成新记录"
    assert v5["id"] != v4["id"]
    assert v5["falsification_criteria_structured"]["threshold"] == "new"
    # 旧的那条原样保留（冻结的承诺不可被覆盖）
    assert st.get_kb_record("claims", v4["id"])[
        "falsification_criteria_structured"]["threshold"] == "old"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))

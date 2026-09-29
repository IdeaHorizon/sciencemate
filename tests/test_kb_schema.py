"""KB v3 unit tests — 4 entity + 10 claim_type + scope + provenance + 强校验。

覆盖：
  - compute_kb_id 内容寻址稳定 + 同名归一
  - smart_default_scope 各 claim_type 行为（含 dead_end → org）
  - validate_record：hypothesis 三件套 / synthesis 多 source / replication / dead_end 校验
  - status 转换合法性 + derive_status 行为
  - merge_kb_record upsert 合并行为
  - State.write_kb 端到端 + scope 路由
  - update_lifecycle：reasoning < 10 字符 raise / 非法转换 raise
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from shared.lib.kb_schema import (
    CLAIM_TYPES, CONCEPT_TYPES,
    SchemaValidationError,
    can_transition_claim_status,
    compute_kb_id,
    derive_status,
    fill_defaults,
    merge_kb_record,
    smart_default_scope,
    validate_record,
)


# ─────────────────────────────────────────────────────────────────────────────
# 内容寻址 id
# ─────────────────────────────────────────────────────────────────────────────

def test_compute_kb_id_concept_normalizes_case_punctuation():
    a = compute_kb_id("concepts", {"canonical_name": "MACE-MP-0"})
    b = compute_kb_id("concepts", {"canonical_name": "mace mp 0"})
    assert a == b, "concept id 应大小写/标点不敏感"


def test_compute_kb_id_prefix_per_entity():
    cid = compute_kb_id("concepts", {"canonical_name": "x"})
    lid = compute_kb_id("claims", {"claim_text": "x"})
    eid = compute_kb_id("experiments", {"experiment_text": "x", "run_at": "t"})
    ckid = compute_kb_id("chunks", {"text": "x", "source": "doi:1"})
    assert cid.startswith("concept_")
    assert lid.startswith("claim_")
    assert eid.startswith("experiment_")
    assert ckid.startswith("chunk_")


def test_compute_kb_id_experiment_includes_run_at():
    """同描述但不同 run_at 应不同 id（experiment 是动作记录）。"""
    a = compute_kb_id("experiments", {"experiment_text": "GAP MD", "run_at": "2026-05-01"})
    b = compute_kb_id("experiments", {"experiment_text": "GAP MD", "run_at": "2026-05-02"})
    assert a != b


# ─────────────────────────────────────────────────────────────────────────────
# Scope 智能默认
# ─────────────────────────────────────────────────────────────────────────────

def test_everything_is_born_project():
    """出生一律 project —— org 没有出生通道，只有晋升通道。

    这里原有 6 条测试，逐个钉住旧的类型启发式（concept 按类型直通 org、
    dead_end 直通 org、empirical 按 scope_dimensions 里有没有 seed 猜、
    chunk 按有没有外部锚分流）。那台机器把治理门整个绕开了 ——
    晋升的历史调用数 = 0，而 org 层堆着单项目直写的条目。

    新契约只有一条不变量，因此只需要一条测试：**任何 entity、任何内容，
    出生都是 project**。见 RFC §1/§14。
    """
    cases = [
        ("concepts", {"concept_type": "dataset"}),      # 曾直通 org
        ("concepts", {"concept_type": "person"}),
        ("chunks", {"source": "doi:10.1/abc"}),         # 曾按锚直通 org
        ("chunks", {"source": "artifact:foo"}),
        ("claims", {"claim_type": "dead_end"}),         # 曾直通 org
        ("claims", {"claim_type": "hypothesis"}),
        ("claims", {"claim_type": "empirical",
                    "scope_dimensions": {"dataset": "QM9", "metric": "MAE"}}),
        ("claims", {"claim_type": "empirical",
                    "scope_dimensions": {"seed": 42}}),
        ("experiments", {}),
    ]
    for entity, record in cases:
        assert smart_default_scope(entity, record) == "project", (entity, record)


def test_no_type_heuristic_grew_back():
    """防复发：出生判据不许再按内容分流。

    判据是**行为**不是源码文本：随机内容组合全部必须落 project ——
    任何"某某类型/某某字段直通 org"的启发式长回来，这条当场红。
    """
    import itertools

    for ct, sd in itertools.product(
        ("empirical", "methodological", "dead_end", "synthesis", "hypothesis",
         "theoretical", "causal", "assumption", "conjecture", "replication"),
        ({}, {"seed": 1}, {"dataset": "X"}, {"run_id": "r"}, {"metric": "m"}),
    ):
        assert smart_default_scope(
            "claims", {"claim_type": ct, "scope_dimensions": sd}) == "project"


def test_smart_default_scope_experiment_is_project():
    assert smart_default_scope("experiments", {}) == "project"


# ─────────────────────────────────────────────────────────────────────────────
# Validation：hypothesis / synthesis / replication / dead_end 强校验
# ─────────────────────────────────────────────────────────────────────────────

def _valid_claim_base(**overrides):
    rec = {
        "claim_text": "x is y",
        "claim_type": "empirical",
        "concept_ids": ["concept_aaaaaaaa"],
        "sources": ["chunk_bbbbbbbbbbb1"],  # ≥ 14 chars passes is_chunk_id
        "confidence": 0.5,
        "scope_dimensions": {},
    }
    rec.update(overrides)
    return rec


def test_validate_claim_missing_concept_ids_raises():
    with pytest.raises(SchemaValidationError, match="concept_ids"):
        validate_record("claims", _valid_claim_base(concept_ids=[]))


def test_validate_claim_orphan_reason_allows_empty_concept_ids():
    rec = _valid_claim_base(concept_ids=[], orphan_reason="cross-cutting methodology")
    validate_record("claims", rec)


def test_validate_hypothesis_requires_falsifier():
    rec = _valid_claim_base(claim_type="hypothesis",
                             prereg_chunk_id="chunk_xxxxxxxxx0001",
                             predicted_outcome="MAE < 100")
    with pytest.raises(SchemaValidationError, match="falsification_criteria"):
        validate_record("claims", rec)


def test_validate_hypothesis_requires_prereg():
    rec = _valid_claim_base(claim_type="hypothesis",
                             falsification_criteria_text="MAE > 100 refutes",
                             predicted_outcome="MAE < 100")
    with pytest.raises(SchemaValidationError, match="prereg_chunk_id"):
        validate_record("claims", rec)


def test_validate_hypothesis_requires_predicted_outcome():
    rec = _valid_claim_base(claim_type="hypothesis",
                             falsification_criteria_text="MAE > 100 refutes",
                             prereg_chunk_id="chunk_xxxxxxxxx0001")
    with pytest.raises(SchemaValidationError, match="predicted_outcome"):
        validate_record("claims", rec)


def test_validate_hypothesis_full_passes():
    rec = _valid_claim_base(
        claim_type="hypothesis",
        falsification_criteria_text="MAE > 100 refutes",
        prereg_chunk_id="chunk_xxxxxxxxx0001",
        predicted_outcome="MAE in [80, 100] meV/atom",
    )
    validate_record("claims", rec)


def test_validate_synthesis_requires_two_claim_sources():
    rec = _valid_claim_base(
        claim_type="synthesis",
        sources=["claim_aaaaaaaa0001"],  # only 1 claim_id
    )
    with pytest.raises(SchemaValidationError, match="synthesis"):
        validate_record("claims", rec)


def test_validate_synthesis_passes_with_two_claims():
    rec = _valid_claim_base(
        claim_type="synthesis",
        sources=["claim_aaaaaaaa0001", "claim_bbbbbbbb0002"],
    )
    validate_record("claims", rec)


# 墓碑：`replication` 类型的强校验守卫。类型已删（收敛 10→5）——
# 复现是**事件**不是类型，`replication_count` 字段已经在，且跨项目合并时
# 会累加。双轨收敛到字段那一轨，守卫随类型一起退场。
# 替代守卫：test_claim_types_converged_to_five / merge 语义的跨项目复现测试。


def test_legacy_claim_types_still_read():
    """旧数据读得进来 —— 按仓库惯例不写迁移器，但老 checkpoint 不该炸。"""
    from shared.lib.kb_schema import normalize_claim_type

    assert normalize_claim_type("theoretical") == "empirical"
    assert normalize_claim_type("conjecture") == "hypothesis"
    assert normalize_claim_type("replication") == "empirical"
    assert normalize_claim_type("empirical") == "empirical"


def test_claim_types_converged_to_five():
    """存在判据 = 有机械消费者。没有闸区别对待的分类不是信息，是装饰。"""
    from shared.lib.kb_schema import CLAIM_TYPES

    assert set(CLAIM_TYPES) == {
        "empirical", "methodological", "hypothesis", "synthesis", "dead_end"}


def test_legacy_types_are_refused_on_write():
    """只在读取端兼容 —— 写入端接受旧名等于收敛没做。"""
    for legacy in ("theoretical", "causal", "assumption", "conjecture", "replication"):
        rec = _valid_claim_base(claim_type=legacy)
        with pytest.raises(SchemaValidationError):
            validate_record("claims", rec)


def test_validate_dead_end_requires_reason():
    rec = _valid_claim_base(claim_type="dead_end", sources=[], orphan_reason="x")
    with pytest.raises(SchemaValidationError, match="dont_repeat_reason"):
        validate_record("claims", rec)


def test_validate_concept_unknown_type_raises():
    with pytest.raises(SchemaValidationError, match="concept_type"):
        validate_record("concepts", {
            "canonical_name": "x", "concept_type": "blah",
            "description": "y",
        })


def test_validate_concept_person_type_passes():
    """v3 新增 person/group concept_type。"""
    validate_record("concepts", {
        "canonical_name": "Geoff Hinton",
        "concept_type": "person",
        "description": "DL pioneer",
    })


def test_validate_experiment_requires_setup_and_outcome():
    with pytest.raises(SchemaValidationError):
        validate_record("experiments", {
            "experiment_text": "x",
            "outcome": "unknown_outcome",
            "run_at": "t",
            "setup_text": "y",
        })


# ─────────────────────────────────────────────────────────────────────────────
# Status 转换 + derive
# ─────────────────────────────────────────────────────────────────────────────

def test_can_transition_terminal_states():
    assert not can_transition_claim_status("refuted", "validated")
    assert not can_transition_claim_status("superseded", "open")
    assert can_transition_claim_status("validated", "refuted")
    assert can_transition_claim_status("open", "validated")


def test_derive_status_high_conf_validated_review():
    rec = {"confidence": 0.9, "review_history":
           [{"to_status": "validated"}], "claim_type": "empirical"}
    assert derive_status(rec) == "validated"


def test_derive_status_open_for_fresh_hypothesis():
    rec = {"confidence": 0.5, "claim_type": "hypothesis",
           "replication_count": 0, "review_history": []}
    assert derive_status(rec) == "open"


def test_derive_status_refuted():
    rec = {"confidence": 0.1, "claim_type": "empirical",
           "review_history": [{"to_status": "refuted"}]}
    assert derive_status(rec) == "refuted"


# ─────────────────────────────────────────────────────────────────────────────
# Merge upsert
# ─────────────────────────────────────────────────────────────────────────────

def test_merge_claim_takes_max_confidence_and_unions_sources():
    old = {"id": "claim_x", "claim_text": "x", "claim_type": "empirical",
           "concept_ids": ["c1"], "sources": ["chunk_a"], "confidence": 0.6,
           "replication_count": 1, "scope_dimensions": {}, "review_history": [],
           "scope": "project"}
    new = {"claim_text": "x", "claim_type": "empirical",
           "concept_ids": ["c1", "c2"], "sources": ["chunk_b"], "confidence": 0.8,
           "replication_count": 2, "scope_dimensions": {},
           "review_history": []}
    merged = merge_kb_record("claims", old, new, now="2026-05-15T00:00:00Z")
    assert set(merged["sources"]) == {"chunk_a", "chunk_b"}
    assert merged["confidence"] == 0.8  # 取较高
    assert merged["replication_count"] == 2  # 取较大
    assert "c1" in merged["concept_ids"] and "c2" in merged["concept_ids"]


def test_merge_concept_canonical_immutable():
    old = {"id": "concept_x", "canonical_name": "MACE",
           "concept_type": "method", "description": "old desc",
           "aliases": ["m"], "attributes": {"family": "GNN"}}
    new = {"canonical_name": "EVIL-RENAME",  # 应被忽略
           "concept_type": "tool",          # 应被忽略
           "description": "new much longer description here",
           "aliases": ["m2"], "attributes": {"family": "GNN", "year": 2023}}
    merged = merge_kb_record("concepts", old, new, now="2026-05-15T00:00:00Z")
    assert merged["canonical_name"] == "MACE"
    assert merged["concept_type"] == "method"
    assert "year" in merged["attributes"]
    assert set(merged["aliases"]) == {"m", "m2"}


# ─────────────────────────────────────────────────────────────────────────────
# State.write_kb 端到端（scope 路由 / merge / lifecycle）
# ─────────────────────────────────────────────────────────────────────────────

def _build_state(home: str, project_id: str = "p1"):
    from core.state import State
    os.environ["HARNESS_FRAMEWORK_HOME"] = home
    os.environ["HARNESS_FRAMEWORK_ORG_HOME"] = str(Path(home) / "org")
    root = Path(home) / "runs" / "r1"
    (root / "artifacts").mkdir(parents=True)
    s = State(run_id="r1", node_type="hypothesis", root=root, project_id=project_id)
    s.project_root = Path(home) / "projects" / project_id
    s.project_root.mkdir(parents=True, exist_ok=True)
    return s


def test_write_kb_concept_lands_in_project():
    with tempfile.TemporaryDirectory() as home:
        s = _build_state(home)
        rec, created = s.write_kb("concepts", {
            "canonical_name": "QM9", "concept_type": "dataset",
            "description": "molecular dataset",
        })
        assert created
        # 出生一律 project —— concept 不再按类型直通共享层
        assert rec["scope"] == "project"
        assert not (Path(home) / "org" / "kb_concepts.jsonl").exists()


def test_write_kb_dead_end_is_born_project_and_broadcasts_by_promotion():
    with tempfile.TemporaryDirectory() as home:
        s = _build_state(home)
        rec, _ = s.write_kb("claims", {
            "claim_text": "Fine-tuning MACE with lr=1e-3 breaks equivariance",
            "claim_type": "dead_end",
            "concept_ids": ["concept_xx"],
            "sources": [],
            "orphan_reason": "n/a",
            "dont_repeat_reason": "Verified: equivariance broken after 1 epoch.",
        })
        # "失败必须跨项目广播"这个需求不变，兑现它的部件变了：
        # 从"出生直落 org"改为"终态批量晋升的机械车道"——广播的需求靠车道满足，
        # 谨慎靠门满足，两者不冲突（RFC §15.3）。
        assert rec["scope"] == "project"


def test_write_kb_hypothesis_lands_in_project():
    with tempfile.TemporaryDirectory() as home:
        s = _build_state(home)
        rec, _ = s.write_kb("claims", {
            "claim_text": "OOD MAE improves <10% with MACE pretraining",
            "claim_type": "hypothesis",
            "concept_ids": ["concept_xx"],
            "sources": ["chunk_xxxxxxxxx0001"],
            "falsification_criteria_text": "Δ MAE OOD ≥ 10%",
            "prereg_chunk_id": "chunk_xxxxxxxxx0001",
            "predicted_outcome": "improvement in [3,10] meV/atom",
        })
        assert rec["scope"] == "project"
        proj_file = Path(home) / "projects" / "p1" / "kb_claims.jsonl"
        assert proj_file.exists()


def test_write_kb_idempotent_same_content_merges():
    with tempfile.TemporaryDirectory() as home:
        s = _build_state(home)
        rec_in = {"canonical_name": "MACE", "concept_type": "method",
                  "description": "ML potential", "aliases": ["mace-mp"]}
        r1, c1 = s.write_kb("concepts", dict(rec_in))
        rec_in["aliases"] = ["mace-mp-0"]
        rec_in["description"] = "ML potential — foundation model variant"
        r2, c2 = s.write_kb("concepts", dict(rec_in))
        assert c1 is True
        assert c2 is False, "同名 concept 应内容寻址命中已有"
        assert r1["id"] == r2["id"]
        assert set(r2["aliases"]) >= {"mace-mp", "mace-mp-0"}


def test_update_lifecycle_reasoning_is_not_length_gated():
    """判决拆除（state:1555）：State 层字数闸删。翻 verdict 的真实门槛
    （validate_status_flip：空 reasoning 拒、validated/refuted 须挂证据）活在
    kb 工具路径上，由 test_prereg_commitments / kb 工具测试覆盖——本层两个
    生产调用方（kb 工具、kb_edges 传播）都自带 reasoning。"""
    with tempfile.TemporaryDirectory() as home:
        s = _build_state(home)
        rec, _ = s.write_kb("claims", {
            "claim_text": "MAE on QM9 OOD is 156 meV/atom",
            "claim_type": "empirical",
            "concept_ids": ["concept_xx"],
            "sources": ["chunk_xxxxxxxxx0001"],
        })
        out = s.update_lifecycle("claims", rec["id"],
                                 status_change={"to_status": "needs_review"},
                                 reasoning="短")
        assert out is not None
def test_update_lifecycle_unusual_transition_is_recorded_not_refused():
    """判决拆除（state:1584）：表外转换（refuted → validated）放行，如实标
    `unusual_transition: true` 进 review_history；从前这里 raise「非法 status 转换」。

    转换连同 reasoning / by_user / by_run 全在账上，referee 终审读得到；
    「新证据能不能翻案」是科学判断，不是流程能拍的板。把拒绝加回去这条必转红。
    """
    with tempfile.TemporaryDirectory() as home:
        s = _build_state(home)
        rec, _ = s.write_kb("claims", {
            "claim_text": "y",
            "claim_type": "empirical",
            "concept_ids": ["concept_xx"],
            "sources": ["chunk_xxxxxxxxx0001"],
        })
        # 先 refute（terminal）
        s.update_lifecycle("claims", rec["id"],
                               status_change={"to_status": "refuted"},
                               reasoning="evidence shows MAE > threshold")
        # 再翻回 validated：放行 + 标记
        out = s.update_lifecycle("claims", rec["id"],
                                 status_change={"to_status": "validated"},
                                 reasoning="new replication overturns the refutation")
        assert out is not None and out["status"] == "validated"
        last = out["review_history"][-1]
        assert last["from_status"] == "refuted" and last["to_status"] == "validated"
        assert last["unusual_transition"] is True
        assert last["reasoning"] == "new replication overturns the refutation"
        # 表内转换不带这个标 —— 标记只指向真正反常的那一次
        assert "unusual_transition" not in out["review_history"][-2]


def test_list_kb_unions_project_and_org():
    with tempfile.TemporaryDirectory() as home:
        s = _build_state(home)
        # org concept —— 现在只能显式指定（模拟晋升写入），没有出生通道
        s.write_kb("concepts", {
            "canonical_name": "QM9", "concept_type": "dataset",
            "description": "x", "scope": "org",
            # org 写入 = 晋升写入，必带出处（护栏 org_provenance_errors）
            "promoted_from": {"project_id": "p_prev", "source_id": "concept_prev",
                              "approved_by": "wangd", "at": "2026-08-21T00:00:00Z"},
        })
        # project claim
        s.write_kb("claims", {
            "claim_text": "y",
            "claim_type": "empirical",
            "concept_ids": ["c"],
            "sources": ["chunk_xxxxxxxxx0001"],
            "scope": "project",
        })
        # list 跨层
        cs = s.list_kb("concepts")
        ls = s.list_kb("claims")
        assert len(cs) == 1 and cs[0]["scope"] == "org"
        assert len(ls) == 1 and ls[0]["scope"] == "project"
        # scope_filter='org' 不返回 project claim
        assert s.list_kb("claims", scope_filter="org") == []


# ─────────────────────────────────────────────────────────────────────────────
# 工具层 smoke（执行而非仅 schema）
# ─────────────────────────────────────────────────────────────────────────────

def test_tools_register_and_execute():
    import shared.tools.library.kb as kbv3  # noqa: F401
    from core.tool_registry import all_tool_names

    # v1.5 refine: 5 个 find_* 已删，4 个 scan tools 合到 curator_scan
    expected = {"create_concept", "create_claim", "create_experiment", "update_claim_status", "search_kb",
                "kb_ingest",
                "curator_scan",                       # ★ 合并 4 scan tools
                }
    names = set(all_tool_names())
    assert expected.issubset(names), f"missing: {expected - names}"

    # 直接调 KB 模块函数（绕开 registry 走纯实现层）
    with tempfile.TemporaryDirectory() as home:
        s = _build_state(home)

        async def run():
            r = await kbv3._create_concept(
                state=s, canonical_name="MACE", concept_type="method",
                description="ML potential")
            # 出生一律 project（org 只有晋升通道）
            assert r["status"] == "success" and r["scope"] == "project"
            return r

        r = asyncio.run(run())
        assert r["id"].startswith("concept_")


# ─────────────────────────────────────────────────────────────────────────────
# v3.3 evidence-grade confidence ceiling
# ─────────────────────────────────────────────────────────────────────────────

def _claim(**over):
    base = {
        "claim_text": "some empirical observation about X",
        "claim_type": "empirical",
        "concept_ids": ["c1"],
        "confidence": 0.95,
    }
    base.update(over)
    return base


def test_confidence_ceiling_orphan_capped_to_half():
    """无 source 的 claim → confidence 上限 0.5。"""
    r = fill_defaults("claims", _claim(sources=[], orphan_reason="pilot"))
    assert r["confidence"] == 0.5
    assert r["derived"]["confidence_ceiling_applied"]["capped_to"] == 0.5


def test_confidence_ceiling_single_source_zero_replication_capped_to_07():
    """单来源 + 零复现的 empirical claim → 上限 0.7。"""
    r = fill_defaults("claims", _claim(sources=["chunk_a"], replication_count=0))
    assert r["confidence"] == 0.7


def test_confidence_ceiling_exempt_when_validated_review():
    """有 validated 复审 → 豁免，保留高 confidence。"""
    r = fill_defaults("claims", _claim(
        sources=["chunk_a"], replication_count=0,
        review_history=[{"to_status": "validated"}],
    ))
    assert r["confidence"] == 0.95


def test_confidence_ceiling_exempt_for_dead_end():
    """dead_end 的 confidence 语义不同 → 不压。"""
    r = fill_defaults("claims", _claim(
        claim_type="dead_end", sources=[],
        dont_repeat_reason="tried and failed for real", confidence=0.9,
    ))
    assert r["confidence"] == 0.9


def test_confidence_ceiling_not_applied_with_replication():
    """有复现（replication≥1）→ 证据够，不压。"""
    r = fill_defaults("claims", _claim(sources=["chunk_a"], replication_count=2))
    assert r["confidence"] == 0.95


def test_confidence_ceiling_two_independent_sources_not_capped():
    """两个独立来源即使零复现也不压（independent_source_count≥2）。"""
    r = fill_defaults("claims", _claim(
        sources=["chunk_a", "chunk_b"], replication_count=0,
        independent_source_count=2,
    ))
    assert r["confidence"] == 0.95


# ─────────────────────────────────────────────────────────────────────────────
# v3.3 org scope 准入：证据不足的 claim 降级 project（关 org 直写）
# ─────────────────────────────────────────────────────────────────────────────

# ── 墓碑：v3.3 org 降级补丁的三条守卫 ────────────────────────────────────
# 补丁本身已删（出生一律 project → 没有东西会"自动落 org"，它防的事在结构上
# 不再发生）。显式 scope="org" 的写入是**晋升通道**，由 curator 终态批量 +
# 人批把关，不该在写入层被二次降级 —— 那会让晋升本身失效。
# 替代守卫：test_everything_is_born_project / test_no_type_heuristic_grew_back。

def test_org_kept_with_two_independent_sources():
    """两个独立来源 → 保留 org。"""
    r = fill_defaults("claims", _claim(
        scope="org", sources=["chunk_a", "chunk_b"], replication_count=0,
        independent_source_count=2))
    assert r["scope"] == "org"


def test_org_kept_with_validated_review():
    """有 validated 复审 → 保留 org。"""
    r = fill_defaults("claims", _claim(
        scope="org", sources=["chunk_a"], replication_count=0,
        review_history=[{"to_status": "validated"}]))
    assert r["scope"] == "org"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))

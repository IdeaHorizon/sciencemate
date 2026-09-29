"""org 没有出生通道 —— 这道守卫必须**扫盘**，不能是一张手写名单。

硬编码"挡住 create_claim 和 create_concept"的写法，下一个新增的 KB 写入工具
默认漏过，而且落地即死、测试全绿没人知道。所以守卫接在唯一咽喉
（`_validate_common`，被每个实体校验器调用），这里的测试就按实体全表扫，
新增实体自动纳入。

见 docs/RFC_KB_TWO_TIERS_20260820.md §3。
"""
from __future__ import annotations

import pytest

from shared.lib.kb_schema import (
    ENTITIES, SchemaValidationError, PROMOTION_PROVENANCE_FIELD,
    org_provenance_errors, validate_record,
)


def _minimal(entity: str, scope: str) -> dict:
    """够过该实体自身必填校验的最小记录 —— 剩下唯一会拦它的就是出处守卫。"""
    base = {"scope": scope}
    if entity == "claims":
        base |= {"claim_text": "低温段接受率需按温度重标定", "claim_type": "empirical",
                 "concept_ids": [], "orphan_reason": "stub",
                 "sources": ["doi:10.1/x"], "confidence": 0.6}
    elif entity == "concepts":
        base |= {"canonical_name": "Metropolis 接受率", "concept_type": "method",
                 "description": "单自旋翻转的接受概率"}
    elif entity == "chunks":
        base |= {"text": "论文正文片段" * 3, "source": "doi:10.1/x"}
    elif entity == "experiments":
        base |= {"experiment_text": "扫温度求磁化率峰位",
                 "setup_text": "L=32, 10^5 sweeps",
                 "run_at": "2026-08-21T00:00:00Z", "outcome": "success"}
    return base


@pytest.mark.parametrize("entity", sorted(ENTITIES))
def test_every_entity_refuses_org_without_promotion_provenance(entity):
    """全实体扫盘：没有 promoted_from 的 org 写入，一律拒。"""
    with pytest.raises(SchemaValidationError) as exc:
        validate_record(entity, _minimal(entity, "org"))
    assert "晋升" in str(exc.value) or PROMOTION_PROVENANCE_FIELD in str(exc.value), (
        f"{entity} 的 org 写入被拒了，但报错没说是出处的问题 —— "
        f"报错指向假原因比不报错更费时间：{exc.value}")


@pytest.mark.parametrize("entity", sorted(ENTITIES))
def test_every_entity_accepts_project_scope_with_no_gate(entity):
    """project 层零门禁 —— 同一条记录换成 project scope 必须写得进去。

    这一半和上一半是一对：只测"org 被挡住"会让"把所有写入都挡住"也通过。
    """
    validate_record(entity, _minimal(entity, "project"))


@pytest.mark.parametrize("entity", sorted(ENTITIES))
def test_every_entity_accepts_org_with_full_provenance(entity):
    """带齐出处的 org 写入必须通过 —— 否则晋升管线自己就走不通。"""
    rec = _minimal(entity, "org") | {
        PROMOTION_PROVENANCE_FIELD: {
            "project_id": "ising_2026", "source_id": "claim_abc123def456",
            "approved_by": "wangd", "at": "2026-08-21T00:00:00Z"},
    }
    validate_record(entity, rec)


def test_partial_provenance_is_refused_and_names_the_missing_keys():
    """半截出处不算出处，且报错要指名缺哪几个键（别逼调用方猜）。"""
    errs = org_provenance_errors({
        "scope": "org",
        PROMOTION_PROVENANCE_FIELD: {"project_id": "ising_2026"},
    })
    assert errs
    joined = "".join(errs)
    for key in ("source_id", "approved_by", "at"):
        assert key in joined, f"报错没点名缺失的 {key!r}：{joined}"

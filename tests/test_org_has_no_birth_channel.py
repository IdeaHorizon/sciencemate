"""org 层只有晋升通道 —— 出生通道在三个面上都关死。

## 现场（2026-08-20 本机实测）

org 层躺着 72 条 dogfood 沉积物：46 chunk + 7 claim + 19 concept，
无一条说得出自己从哪个项目来、谁批准的。而设计中的正路
`propose_org_promotion` **历史调用数 = 0**。

原因是"这条该不该进 org"有三个互相矛盾的真相源：
  1. 出生启发式（smart_default_scope 按类型/字段猜）→ 创建瞬间就在 org 里
  2. 写入层降级补丁（v3.3）→ 发现绕开之后打的
  3. 治理晋升（propose_org_promotion）→ 设计的正路，没人走

P1 把 1 和 2 整体删除，只留 3。本文件守这条不变量的三个面。
"""
from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import pytest

from core.state import State
from shared.lib.kb_schema import (
    SchemaValidationError,
    fill_defaults,
    org_provenance_errors,
    smart_default_scope,
    validate_record,
)


def _state() -> State:
    return State.new(node_type="_curator", base_dir=Path(tempfile.mkdtemp()),
                     project_id="p_birth")


# ── 面 1：默认层 ────────────────────────────────────────────────────────────


def test_no_content_can_be_born_org():
    """任何 entity、任何内容组合，出生都是 project。

    判据是**行为**不是源码：穷举类型与字段组合，任何"某某直通 org"的
    启发式长回来，这条当场红。
    """
    import itertools

    for entity, key, values in (
        ("concepts", "concept_type",
         ("person", "group", "method", "theory", "metric", "tool",
          "dataset", "phenomenon", "task", "domain")),
        ("claims", "claim_type",
         ("empirical", "methodological", "dead_end", "synthesis", "hypothesis",
          "theoretical", "causal", "assumption", "conjecture", "replication")),
        ("chunks", "source", ("doi:10.1/x", "arxiv:2401.1", "artifact:foo", "")),
    ):
        for v, sd in itertools.product(values, ({}, {"seed": 1}, {"dataset": "X"})):
            got = smart_default_scope(entity, {key: v, "scope_dimensions": sd})
            assert got == "project", f"{entity} {key}={v} sd={sd} → {got}"


# ── 面 2：工具层 ────────────────────────────────────────────────────────────


def test_create_claim_refuses_explicit_org_scope():
    """显式传 scope='org' 是枚举违约：create_claim 的 schema 里 scope enum=['project']，
    派发口核一次；正路（晋升）写在 schema description 里送到调用方。"""
    from core.bootstrap import bootstrap
    from core.tool_registry import execute, get_tool

    bootstrap()
    result = asyncio.run(execute(
        "create_claim", _state(), claim_text="a universally true thing",
        claim_type="empirical", sources=["doi:10/x"], scope="org",
    ))
    assert result["status"] == "error"
    assert result["parameter_violations"]
    assert "scope" in result["error"] and "project" in result["error"]
    # 契约必须送到调用方：schema 自己说清正路是什么
    scope_desc = get_tool("create_claim").parameters_schema["properties"]["scope"]["description"]
    assert "晋升" in scope_desc


def test_create_claim_still_accepts_project():
    from shared.tools.library.kb import _create_claim

    result = asyncio.run(_create_claim(
        _state(), claim_text="a project-scoped finding", claim_type="empirical",
        concept_ids=["concept_x"], sources=["doi:10/x"],
    ))
    assert result["status"] == "success"
    assert result["scope"] == "project"


# ── 面 3：账本层（出处护栏）────────────────────────────────────────────────


def test_org_record_without_provenance_is_rejected():
    """org 记录必须说得出从哪来 —— 写不出出处的，就不是晋升，是绕道。"""
    errs = org_provenance_errors({"scope": "org", "claim_text": "x"})
    assert errs and "promoted_from" in errs[0]

    with pytest.raises(SchemaValidationError, match="promoted_from"):
        validate_record("claims", fill_defaults("claims", {
            "claim_text": "sneaked in", "claim_type": "empirical",
            "concept_ids": ["c"], "sources": ["doi:10/x"], "scope": "org",
        }))


def test_org_record_with_partial_provenance_is_rejected():
    """出处四要素缺一不可：哪个项目、哪条源记录、谁批准、何时。"""
    errs = org_provenance_errors({
        "scope": "org",
        "promoted_from": {"project_id": "p1", "source_id": "claim_x"},
    })
    assert errs and "approved_by" in errs[0] and "at" in errs[0]


def test_org_record_with_full_provenance_passes():
    rec = fill_defaults("claims", {
        "claim_text": "promoted knowledge", "claim_type": "empirical",
        "concept_ids": ["c"], "sources": ["doi:10/x"], "scope": "org",
        "promoted_from": {"project_id": "p_src", "source_id": "claim_src",
                          "approved_by": "wangd", "at": "2026-08-21T00:00:00Z"},
    })
    validate_record("claims", rec)      # 不抛 = 出处齐全
    assert rec["scope"] == "org"
    assert org_provenance_errors(rec) == []


def test_project_records_need_no_provenance():
    """project 层零门槛 —— 它是工作记忆，不承担对组织为真的义务。"""
    assert org_provenance_errors({"scope": "project"}) == []
    rec = fill_defaults("claims", {
        "claim_text": "working note", "claim_type": "empirical",
        "concept_ids": ["c"], "sources": ["doi:10/x"],
    })
    assert rec["scope"] == "project"


# ── 反向：删掉的机器不许长回来 ──────────────────────────────────────────────


def test_the_v33_downgrade_patch_stays_deleted():
    """v3.3 降级补丁必须保持删除状态。

    它防的是"出生启发式把证据不足的 claim 自动落 org"。出生通道关死后，
    显式 scope='org' 的写入**就是晋升**（已过人批与出处校验）——
    此时再在写入层二次降级，会让晋升本身失效。
    """
    import shared.lib.kb_schema as ks

    assert not hasattr(ks, "_org_scope_downgrade_needed")
    rec = fill_defaults("claims", {
        "claim_text": "promoted, single source, zero replication",
        "claim_type": "empirical", "concept_ids": ["c"],
        "sources": ["chunk_a"], "replication_count": 0, "scope": "org",
        "promoted_from": {"project_id": "p", "source_id": "s",
                          "approved_by": "wangd", "at": "t"},
    })
    assert rec["scope"] == "org", "晋升写入不该被写入层降级"
    assert "org_downgraded" not in (rec.get("derived") or {})

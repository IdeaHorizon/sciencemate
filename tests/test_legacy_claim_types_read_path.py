"""旧盘上的 claim_type 必须读得进来 —— 否则老 claim 对晋升管线静默隐身。

类型从 10 收敛到 5（2026-08-21），按仓库惯例不写迁移器。但已有项目的
kb_claims.jsonl 里躺着 `theoretical` / `conjecture` / `replication`：
`KIND_BY_CLAIM_TYPE` 查不到这些键，那些 claim 就再也不会出现在晋升候选里 ——
**不报错，只是消失**。归一接在 `_read_kb_records`（KB 读取的唯一咽喉）。
"""
from __future__ import annotations

import json

import pytest

from core.bootstrap import bootstrap
from core.state import State


LEGACY_TO_CURRENT = {
    "theoretical": "empirical",
    "causal": "empirical",
    "assumption": "empirical",
    "conjecture": "hypothesis",
    "replication": "empirical",
}


def _seed_legacy_row(state: State, claim_id: str, legacy_type: str) -> None:
    """绕过写入端校验，直接往盘上放一条旧格式记录 —— 模拟老 checkpoint。"""
    path = state._kb_path("claims", scope="project")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps({
            "id": claim_id,
            "claim_text": f"一条 {legacy_type} 年代的老结论",
            "claim_type": legacy_type,
            "scope": "project",
            "sources": ["doi:10.1/old"],
            "concept_ids": [], "orphan_reason": "stub",
            "confidence": 0.7,
        }, ensure_ascii=False) + "\n")


@pytest.mark.parametrize("legacy,current", sorted(LEGACY_TO_CURRENT.items()))
def test_legacy_row_reads_back_normalized(tmp_path, legacy, current):
    bootstrap()
    state = State.new(node_type="_curator", base_dir=tmp_path / legacy,
                      project_id=f"legacy_{legacy}")
    cid = f"claim_{legacy[:8]:0<12}".replace(" ", "0")
    _seed_legacy_row(state, cid, legacy)

    rec = state.get_kb_record("claims", cid)
    assert rec is not None, "老记录读不出来了 —— 归一层把它吞了"
    assert rec["claim_type"] == current
    # 归一是解读不是事实：原值必须留痕，否则日后追不回「当年按什么类型写的」
    assert rec["legacy_claim_type"] == legacy


def test_normalized_legacy_claim_is_visible_to_promotion_routing(tmp_path):
    """真正要保住的东西：老 claim 还能被晋升分道路由到。"""
    from core.kb_promotion import KIND_BY_CLAIM_TYPE

    bootstrap()
    state = State.new(node_type="_curator", base_dir=tmp_path,
                      project_id="legacy_routing")
    _seed_legacy_row(state, "claim_oldtheory01", "theoretical")

    rec = state.get_kb_record("claims", "claim_oldtheory01")
    assert rec["claim_type"] in KIND_BY_CLAIM_TYPE, (
        "老 claim 归一后仍然不在分道表里 —— 它对晋升管线是隐身的")


def test_current_types_are_untouched(tmp_path):
    """归一只动旧名 —— 现行类型不该被加上 legacy_claim_type 噪音。"""
    bootstrap()
    state = State.new(node_type="_curator", base_dir=tmp_path,
                      project_id="legacy_noop")
    _seed_legacy_row(state, "claim_current0001", "methodological")

    rec = state.get_kb_record("claims", "claim_current0001")
    assert rec["claim_type"] == "methodological"
    assert "legacy_claim_type" not in rec


def test_no_live_lookup_table_keys_on_dead_types():
    """查找表的键挂在词表上，词表改了表必须跟着改 —— 这里扫全部活代码。

    收敛 10→5 时 _CLAIM_TYPE_PRIORITY 曾漏改：theoretical/causal/assumption
    归一后从 90/80/75 静默掉到 55（存活类型最低），把"该浮上来"的一批 claim
    系统性压到注入队列底部。不报错，只是它们不再被注入。
    """
    import re
    from pathlib import Path

    from shared.lib.kb_schema import CLAIM_TYPES, LEGACY_CLAIM_TYPE_ALIASES

    root = Path(__file__).resolve().parent.parent
    offenders: list[str] = []
    for mod in [*root.glob("core/**/*.py"), *root.glob("shared/**/*.py")]:
        text = mod.read_text(encoding="utf-8", errors="replace")
        for i, line in enumerate(text.splitlines(), 1):
            code = line.split("#", 1)[0]          # 注释里提旧名是墓碑，合法
            for legacy in LEGACY_CLAIM_TYPE_ALIASES:
                assert legacy not in CLAIM_TYPES
                if re.search(rf"""['"]{legacy}['"]""", code):
                    offenders.append(f"{mod.relative_to(root)}:{i}: {line.strip()[:90]}")
    # 唯一合法出现处：归一映射表本身
    offenders = [o for o in offenders if "kb_schema.py" not in o]
    assert not offenders, (
        "活代码仍在用已删的 claim_type 当键/分支条件 —— 归一后这些分支"
        "永远命不中（或命中错档），且不报错：\n" + "\n".join(offenders))

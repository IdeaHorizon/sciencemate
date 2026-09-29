"""晋升产出的每一种 org 条目，送达面都得消费得到。

## 为什么要这道扫盘

2026-08-21 Ising 闭环回放实测：晋升产出 3 条 org 卡，其中 **2 条是 recipe**，
而 `org_orientation` 只读 `verified_finding` / `dead_end` —— 方法配方（跨项目
复利最强的一类）**产出即黑洞**，不报错，只是永远送不到任何新项目。

这是「机制存在但没接到路径」在**契约层**的版本：两端各自都对，接缝没人管。
逐个补是打地鼠 —— 下一次新增 org_kind 照样漏。所以判据按**扫盘**写：
从 KIND_* 常量现算，新增种类自动纳入。
"""
from __future__ import annotations

import inspect

import pytest

from core import kb_promotion as kp
from core import org_delivery


def _producible_kinds() -> set[str]:
    """晋升管线能产出的 org_kind —— 从常量现算，不手抄。"""
    return {v for k, v in vars(kp).items()
            if k.startswith("KIND_") and isinstance(v, str)}


def _delivered_kinds() -> set[str]:
    """送达面实际会去查的 org_kind。"""
    src = inspect.getsource(org_delivery)
    return {k for k in _producible_kinds() if f'kind="{k}"' in src}


def test_every_producible_kind_reaches_some_delivery_path():
    producible = _producible_kinds()
    delivered = _delivered_kinds()
    # biblio 走的是锚点复用（biblio_hits 按 source 锚查 chunks，不按 org_kind），
    # exemplar 走范例检索。两者有各自的送达路径，不经 org_orientation。
    exempt = {kp.KIND_BIBLIO, kp.KIND_EXEMPLAR}
    missing = producible - delivered - exempt
    assert not missing, (
        f"这些 org_kind 晋升产得出来、送达面却从不查：{sorted(missing)}。"
        f"产出即黑洞 —— 不报错，只是永远送不到新项目。"
        f"要么接进 org_orientation，要么在 exempt 里写明它走哪条别的送达路径。")


def test_biblio_has_its_own_delivery_path():
    """豁免不是免检：写明豁免的，得真有另一条路。"""
    assert hasattr(org_delivery, "biblio_hits")


def test_delivery_reads_no_field_promotion_never_writes():
    """送达面读的字段，晋升器得写得出来 —— 否则那段渲染永远是空的。

    实测过的坑：dead_end 红旗按 `trigger` 匹配，而晋升器从不写 trigger，
    于是红旗一次都没响过。
    """
    delivery_src = inspect.getsource(org_delivery)
    promo_src = inspect.getsource(kp)
    import re

    read = set(re.findall(r'rec\.get\("([a-z_]+)"\)', delivery_src))
    # 这些来自 claim/chunk 的通用字段或有显式退路，不由晋升器新写
    generic = {
        "id", "claim_text", "claim_type", "org_kind", "scope", "promoted_from",
        "text", "source", "group_readings", "statement", "applicability",
        "confidence_basis", "replication_count", "why", "practice", "domain",
    }
    unwritable = set()
    for field in read - generic:
        # 晋升器写得出（出现在 kb_promotion 里）或有 fallback（送达面里带 or）
        if field in promo_src:
            continue
        if re.search(rf'rec\.get\("{field}"\)\s*or\s', delivery_src):
            continue
        unwritable.add(field)
    assert not unwritable, (
        f"送达面读这些字段，但晋升器不写、送达面也没给退路：{sorted(unwritable)}。"
        f"那段渲染永远是空的 —— 机制在场但从不生效。")


def test_orientation_actually_renders_a_recipe(tmp_path):
    """扫盘之外再钉一条行为：recipe 真能出现在注入正文里。

    静态扫盘只保证"查了这个 kind"，不保证渲染分支写对了。
    """
    from core.bootstrap import bootstrap
    from core.state import State

    bootstrap()
    st = State.new(node_type="hypothesis", base_dir=tmp_path, project_id="p_recipe")
    st.write_kb("claims", {
        "claim_text": "预注册必须写明 FSS 拟合的加权方案",
        "statement": "预注册必须写明 FSS 拟合的加权方案",
        "practice": "falsifier 里连权重来源一起写",
        "why": "交叉点少时单个高精度点主导截距",
        "claim_type": "methodological", "org_kind": "recipe",
        "domain": "cond-mat.stat-mech",
        "concept_ids": [], "orphan_reason": "stub", "scope": "org",
        "sources": ["doi:10.1/x"], "confidence": 0.85,
        "promoted_from": {"project_id": "ising", "source_id": "claim_" + "a" * 12,
                          "approved_by": "wangd", "at": "2026-08-21T00:00:00Z"},
    })
    text = org_delivery.org_orientation(st)
    assert text and "方法配方" in text
    assert "加权方案" in text
    assert "据此该怎么做" in text

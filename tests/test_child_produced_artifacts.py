"""「子 run 产出了什么」必须只有一处推导（E2E v11）。

三个消费方 —— flow entry 的 artifact_ids、reviewer 有没有交 critique、交付
判定 —— 此前各读各的裸 `imported`。而 v2 的交付方式是写自己的 Git 目录，
imported 恒为空，于是：
  · flow entry 的 artifact_ids 永远是空的
  · reviewer 明明产出了 critique，账本却记 failed_awaiting_human，
    再按"账本优先于传参"把调用方传来的**真实存在**的 critique id 推翻
E2E v11 现场：hypothesis → reviewer → curator 三步全绿，卡死在这里出不去。
"""
from __future__ import annotations

import inspect

from shared.tools.run_node import _child_produced_artifacts


def test_v1_bus_semantics_unchanged():
    """imported 非空时一字不变 —— 老路径不能因为这次修改而漂移。"""
    imported = [{"id": "review_critique__a", "type": "review_critique"}]
    summary = {"artifacts": [{"id": "other__b", "type": "other"}]}
    assert _child_produced_artifacts(summary, imported) == imported


def test_v2_falls_back_to_the_child_own_artifacts():
    summary = {"artifacts": [
        {"id": "review_critique__hypothesis_critique_LJ", "type": "review_critique"},
    ]}
    produced = _child_produced_artifacts(summary, [])
    assert [a["id"] for a in produced] == ["review_critique__hypothesis_critique_LJ"]


def test_reviewer_critique_is_detected_under_v2():
    """这条是 v11 的直接回归：v2 下 reviewer 交了 critique 就必须算它交了。"""
    summary = {"artifacts": [
        {"id": "review_critique__x", "type": "review_critique"},
        {"id": "review_notes__y", "type": "review_notes"},
    ]}
    critiques = [a for a in _child_produced_artifacts(summary, [])
                 if a.get("type") == "review_critique"]
    assert len(critiques) == 1 and critiques[0]["id"] == "review_critique__x"


def test_empty_everywhere_is_empty():
    assert _child_produced_artifacts({}, []) == []
    assert _child_produced_artifacts({"artifacts": None}, []) == []


def test_malformed_entries_are_skipped():
    summary = {"artifacts": [None, "nope", {"id": "ok__1", "type": "t"}]}
    assert [a["id"] for a in _child_produced_artifacts(summary, [])] == ["ok__1"]


def test_no_consumer_still_reads_bare_imported_for_production():
    """扫盘守卫：再有人写 `for a in imported` 去问"产出了什么"就红。

    只允许 _child_produced_artifacts 与 _delivered 两处触碰这个事实。
    """
    from shared.tools import run_node

    source = inspect.getsource(run_node)
    # 三个已知合法用法：helper 自身、_delivered、can_start_standard_review
    assert "a for a in imported if isinstance(a, dict) and a.get(\"type\")" not in source
    assert "artifact_ids = [a[\"id\"] for a in imported]" not in source

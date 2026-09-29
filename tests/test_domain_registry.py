"""域注册表：投稿选分类，不是自由填空。

钉四条契约：
  1. 骨架 = arXiv 分类；自由文本拒收且报错列最近匹配（契约送到调用方）
  2. 本地叶挂骨架父、随晋升人批一起注册（词表治理不另开审批流）
  3. 送达按上行链命中最细的有正典的节点（卡片单说法，正典做聚合）
  4. 机械建议查得到就给、查不到不猜
"""
from __future__ import annotations

import pytest

from core.bootstrap import bootstrap
from core.domain_registry import (
    ancestors, register_leaf, spine_categories, validate_domain,
)
from core.state import State


def test_spine_is_the_arxiv_taxonomy():
    cats = spine_categories()
    assert len(cats) > 140
    for c in ("cond-mat.stat-mech", "physics.comp-ph", "cs.LG", "stat.ML",
              "gr-qc", "hep-th"):
        assert c in cats


def test_free_text_is_refused_and_error_names_valid_values():
    v = validate_domain(None, "机器学习势")
    assert not v.ok
    assert v.suggestions, "拒收没给最近匹配 —— 逼调用方猜"
    v2 = validate_domain(None, "cond-mat")   # archive 本身不是分类（有子目录的）
    assert not v2.ok
    assert any("cond-mat" in s for s in v2.suggestions)


def test_leaf_requires_a_spine_parent():
    ok = validate_domain(None, "physics.comp-ph/mlip-robustness")
    assert ok.status == "registrable"
    bad = validate_domain(None, "不存在的父/mlip-robustness")
    assert bad.status == "invalid"
    assert "悬空" in bad.error


def test_leaf_registration_rides_the_human_batch(tmp_path, monkeypatch):
    bootstrap()
    state = State.new(node_type="_curator", base_dir=tmp_path,
                      project_id="leaf_reg")
    # 无人批背书 → 拒
    res = register_leaf(state, domain="physics.comp-ph/mlip-robustness",
                        description="MLIP 稳健性", approved_by="", at="t")
    assert res["status"] == "error" and res["code"] == "approval_required"
    # 带背书 → 注册，此后是合法叶
    res = register_leaf(state, domain="physics.comp-ph/mlip-robustness",
                        description="MLIP 稳健性", approved_by="wangd",
                        at="2026-08-21T00:00:00Z")
    assert res["code"] == "registered"
    assert validate_domain(state, "physics.comp-ph/mlip-robustness").status == "leaf"
    # 幂等：同批第二张同域卡不报错
    res = register_leaf(state, domain="physics.comp-ph/mlip-robustness",
                        description="", approved_by="wangd", at="t2")
    assert res["code"] == "already_registered"


def test_promote_registers_the_new_leaf_with_the_same_approval(tmp_path):
    """晋升就是投稿时刻：批卡的那次人批顺带背书新叶。"""
    from tests.test_kb_promotion import (
        _good_card, _make_terminal, _seed_claim, _seed_evidence, _state,
    )
    from core import kb_promotion as kp

    st = _state()
    _make_terminal(st)
    cid = _seed_claim(st, _seed_evidence(st))
    card = _good_card()   # domain = cond-mat.mtrl-sci/mlip-robustness（registrable）
    scan = kp.promotion_scan(st, drafts={cid: card})
    cand = next(c for c in scan["human_batch"] if c.source_id == cid)
    res = kp.promote(st, cand, project_id="p_si", approved_by="wangd",
                     at="2026-08-21T00:00:00Z")
    assert res["status"] == "success"
    assert validate_domain(st, card["domain"]).status == "leaf", (
        "人批过了、卡落了，叶却没注册 —— 下一张同域卡又会走一遍 registrable")


def test_ancestor_walk_orders_specific_to_broad():
    assert ancestors("physics.comp-ph/mlip-robustness") == (
        "physics.comp-ph/mlip-robustness", "physics.comp-ph", "physics")
    assert ancestors("cond-mat.stat-mech") == ("cond-mat.stat-mech", "cond-mat")
    assert ancestors("gr-qc") == ("gr-qc",)


def test_canon_delivery_walks_up_to_the_finest_written_survey(tmp_path):
    """叶没综述读父级的 —— 粗粒度转述是上层综述的活，卡片不多说法。"""
    from core.org_canon import canon_for_domain, write_canon

    bootstrap()
    state = State.new(node_type="_curator", base_dir=tmp_path,
                      project_id="canon_walk")
    write_canon(state, domain="physics.comp-ph",
                body="# 计算物理方法综述\n\n共识与争论…",
                absorbed_ids=[], at="2026-08-21T00:00:00Z")
    hit = canon_for_domain(state, "physics.comp-ph/mlip-robustness")
    assert hit is not None
    assert hit["domain"] == "physics.comp-ph"


def test_suggestion_does_not_guess_without_evidence(tmp_path):
    """证据链上没有 arXiv 分类元数据 → 空建议，不猜。"""
    from core.domain_registry import suggest_from_evidence

    bootstrap()
    state = State.new(node_type="_curator", base_dir=tmp_path,
                      project_id="no_guess")
    cl, _ = state.write_kb("claims", {
        "claim_text": "一条没有 arXiv 证据的结论", "claim_type": "empirical",
        "concept_ids": [], "orphan_reason": "stub", "scope": "project",
        "sources": ["doi:10.1/x"], "confidence": 0.6,
    })
    assert suggest_from_evidence(state, cl["id"]) == ()


def test_every_spine_category_has_a_human_readable_label():
    """slug 是给机器寻址的，名字是给人挑的 —— 两份清单必须逐项对齐。

    名字和 slug 同处一个文件，但仍然是两个 dict：新增一个分类而忘了配名字，
    界面上就会出现一个光秃秃的 `cond-mat.mtrl-sci`，而没有任何一层会报错
    （它照样能寻址，只是没人看得懂）。所以这条判据是**扫盘**：
    枚举全部骨架分类，逐个要求 `domain_label` 给出与 slug 不同的名字。
    """
    from core.domain_registry import domain_label, spine_categories

    missing = [slug for slug in spine_categories() if domain_label(slug) == slug]
    assert missing == [], f"这些分类没有人读名：{missing}"


def test_labels_do_not_leak_across_archives_that_share_a_suffix():
    """`cs.CG` 是计算几何，`math.CG` 是元胞自动机 —— 后缀相同不代表是同一个东西。

    名字表按**全 slug** 存就是为了这个：按后缀存会让它们互相覆盖，而覆盖之后
    仍然每个分类都有名字，扫盘那条判据照样绿。
    """
    from core.domain_registry import domain_label

    assert domain_label("cs.CG") == "Computational Geometry"
    assert domain_label("nlin.CG") == "Cellular Automata and Lattice Gases"
    assert domain_label("cs.IT") == domain_label("math.IT") == "Information Theory"


def test_the_catalog_offers_every_spine_category_exactly_once():
    """"让人挑域"的界面必须能挑到词表里的每一个分类，且不重复。"""
    from core.domain_registry import domain_catalog, spine_categories

    offered = [
        option["domain"]
        for group in domain_catalog()
        for option in group["categories"]
    ]
    assert sorted(offered) == sorted(spine_categories())
    assert len(offered) == len(set(offered))


def test_a_local_leaf_label_falls_back_to_its_own_name_not_a_guess():
    """本地叶只有注册它的人知道叫什么。查不到就用叶名本身，**不编**。"""
    from core.domain_registry import domain_label

    assert domain_label("physics.comp-ph/mlip-robustness") == "mlip robustness"


def test_every_spine_category_has_a_chinese_label_too():
    """中英两套名字都要逐项覆盖骨架。

    只覆盖一半的后果是界面上中英混排 —— 而混排不会报错，只是难看且不专业。
    判据是**扫盘**：枚举全部骨架分类，两种语言都要给出与 slug 不同的名字。
    """
    from core.domain_registry import domain_label, spine_categories

    for lang in ("en", "zh"):
        missing = [s for s in spine_categories() if domain_label(s, lang=lang) == s]
        assert missing == [], f"{lang} 缺这些分类的名字：{missing}"


def test_chinese_labels_also_keep_same_suffix_categories_apart():
    """`cs.CG` 是计算几何，`nlin.CG` 是元胞自动机 —— 中文侧同样不许混。"""
    from core.domain_registry import domain_label

    assert domain_label("cs.CG", lang="zh") == "计算几何"
    assert domain_label("nlin.CG", lang="zh") == "元胞自动机与格子气"
    # cs.LG 与 stat.ML 的英文名撞车（都叫 Machine Learning），中文侧分得开
    # 反而更好 —— 但两者都必须有名字。
    assert domain_label("cs.LG", lang="zh") == "机器学习"
    assert domain_label("stat.ML", lang="zh") == "机器学习（统计）"


def test_the_language_choice_does_not_leak_into_the_slug():
    """换语言只换**显示名**，寻址键一个字节都不能变。

    slug 是正典层的寻址键；它要是跟着界面语言变，同一个域在两处就成了两个域。
    """
    from core.domain_registry import domain_catalog, spine_categories

    for lang in ("en", "zh"):
        offered = [o["domain"] for g in domain_catalog(lang=lang) for o in g["categories"]]
        assert sorted(offered) == sorted(spine_categories())

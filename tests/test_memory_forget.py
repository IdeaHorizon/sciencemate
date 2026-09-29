"""遗忘层：三项机械作业 + dreaming 判据现算。

两条不变量：
  1. 遗忘按**适用面失效**，不按日历。机械层只留证据不判决。
  2. "上次 dreaming 什么时候"从 run 账本现算 —— 跳过不会产生 run，无章可盖。
"""
from __future__ import annotations

import json

import pytest

from core import memory as M
from core import memory_forget as F
from core.bootstrap import bootstrap
from core.state import State


@pytest.fixture
def st(tmp_path, mem_worktree):
    bootstrap()
    s = State.new(node_type="_curator", base_dir=tmp_path / "runs",
                  project_id="forget_test", project_worktree=mem_worktree)
    M.ensure_skeleton(s)
    return s


def _note(s, text, **kw):
    kw.setdefault("section", M.SECTION_PITFALL)
    kw.setdefault("run_id", "r1")
    kw.setdefault("nodes", ["experiment"])
    return M.append_manual(s, text=text, **kw)


# ── 引用失效 ────────────────────────────────────────────────────────────────


def test_dangling_reference_is_reported_not_deleted(st):
    """条目点名的工具没了 → 报出来，但**不动盘**。

    错删一条真教训比多留一条废条目贵得多，所以机械层只留证据。
    """
    # 用一个**确实**已被删除的工具名：add_memory_candidate（候选队列的入口，
    # 随队列一起退场）。这正是这道扫描要抓的现实场景 —— 教训点名了一个
    # 曾经存在、现在没了的东西。
    _note(st, "记经验用 add_memory_candidate 之前先想清楚适用面",
          tools=["add_memory_candidate"])
    res = F.scan_for_forgetting(st)
    assert len(res["dangling_reference"]) == 1
    assert "add_memory_candidate" in res["dangling_reference"][0]["missing_tools"]
    assert len(M.manual_entries(st)) == 1, "scan 不该动盘"
    assert "提议" in res["note"]


def test_live_references_are_not_flagged(st):
    """另一半：只测"会报"会让"什么都报"也通过。"""
    _note(st, "save_artifact 之前确认类型注册过", tools=["save_artifact"])
    assert F.scan_for_forgetting(st)["dangling_reference"] == []


def test_unknown_registry_reports_nothing_rather_than_guessing(st, monkeypatch):
    """拿不到工具名单时**一条都不报** —— 拿残缺名单去误杀真教训更糟。"""
    monkeypatch.setattr(F, "_known_tools", lambda: set())
    monkeypatch.setattr(F, "_known_nodes", lambda: set())
    _note(st, "调一个根本不存在的工具", tools=["totally_made_up_tool"])
    assert F.scan_for_forgetting(st)["dangling_reference"] == []


# ── 矛盾：呈现不裁决 ────────────────────────────────────────────────────────


def test_contradiction_is_surfaced_without_a_verdict(st):
    _note(st, "低温段应当使用更长的弛豫时间来平衡系统", nodes=["experiment"])
    _note(st, "低温段不要使用更长的弛豫时间来平衡系统", nodes=["experiment"])
    res = F.scan_for_forgetting(st)
    assert len(res["contradictions"]) == 1
    c = res["contradictions"][0]
    assert "不裁决" in c["why"]
    assert "experiment" in c["shared_scope"]
    # 两条都还在 —— 机械层没有替谁做决定
    assert len(M.manual_entries(st)) == 2


def test_different_scopes_are_not_called_contradictory(st):
    """适用面不相交就不是矛盾 —— 不同场景下的不同做法是常态。"""
    _note(st, "低温段应当使用更长的弛豫时间", nodes=["experiment"])
    _note(st, "低温段不要使用更长的弛豫时间", nodes=["postprocess"])
    assert F.scan_for_forgetting(st)["contradictions"] == []


# ── 落地：合并 / 退休 ───────────────────────────────────────────────────────


def test_merge_inherits_union_of_scope_and_max_recurrence(st):
    _note(st, "冻结前要先登记 chunk 否则引用会悬空", tools=["freeze_artifact"],
          nodes=["experiment"])
    _note(st, "登记 chunk 这一步不能省，否则下游引用悬空", tools=["save_artifact"],
          nodes=["writing"])
    res = F.merge_entries(
        st, ["冻结前要先登记 chunk", "登记 chunk 这一步"],
        "引用产物前必须先把它登记成 chunk，否则引用悬空")
    assert res["status"] == "success" and res["merged_count"] == 2
    e = M.manual_entries(st, section=M.SECTION_PITFALL)
    assert len(e) == 1
    assert set(e[0].tools) == {"freeze_artifact", "save_artifact"}
    assert set(e[0].nodes) == {"experiment", "writing"}


def test_retire_removes_from_delivery_but_git_still_has_it(st):
    _note(st, "一条已经过时的教训需要退休掉")
    res = F.retire_entries(st, ["一条已经过时的教训"])
    assert res["retired"]
    assert M.manual_entries(st) == []
    assert "遗忘不是销毁" in res["note"]


def test_retire_reports_what_it_could_not_find(st):
    """报错要说清哪条没匹配上 —— 静默失败会让人以为删掉了。"""
    _note(st, "一条真实存在的教训")
    res = F.retire_entries(st, ["这条根本不存在的前缀"])
    assert res["not_found"] == ["这条根本不存在的前缀"]
    assert len(M.manual_entries(st)) == 1


def test_short_prefix_that_is_unique_is_enough(st):
    """判据是唯一匹配，不是前缀长度（与 memory.retire_law 同一把尺）。

    从前 `len(_norm(p)) < 6` 静默丢弃短前缀：中文五个字往往已经唯一，
    却被当作「没给前缀」——而且丢得悄无声息。把长度阈值加回去这条必转红。
    """
    _note(st, "低温段应当使用更长的弛豫时间")
    _note(st, "冻结前要先登记 chunk 否则引用会悬空")
    res = F.retire_entries(st, ["低温段"])          # 规范化后 3 字符，唯一
    assert res["retired"] and res["not_found"] == [] and res["ambiguous"] == []
    assert [e.text for e in M.manual_entries(st)] == ["冻结前要先登记 chunk 否则引用会悬空"]


def test_ambiguous_prefix_touches_nothing_and_lists_the_candidates(st):
    """一个前缀命中多条 → 一条都不动，把候选摆出来让调用方缩小。"""
    _note(st, "低温段应当使用更长的弛豫时间")
    _note(st, "低温段的采样步长要减半")
    res = F.retire_entries(st, ["低温段"])
    assert res["retired"] == []
    assert len(M.manual_entries(st)) == 2
    assert res["ambiguous"][0]["prefix"] == "低温段"
    assert len(res["ambiguous"][0]["matches"]) == 2


def test_merge_with_short_unique_prefixes(st):
    _note(st, "冻结前要先登记 chunk 否则引用会悬空", tools=["freeze_artifact"])
    _note(st, "登记这一步不能省否则下游引用悬空", tools=["save_artifact"])
    res = F.merge_entries(st, ["冻结前", "登记这"], "引用产物前必须先登记成 chunk")
    assert res["status"] == "success" and res["merged_count"] == 2


def test_merge_hint_names_the_criterion_not_a_length(st):
    """hint 与判据同源：说「唯一指向」，不说「≥6 字符」。"""
    _note(st, "这一节里只有一条教训可以匹配")
    res = F.merge_entries(st, ["这一节里"], "合并成什么都没有对象")
    assert res["status"] == "error" and res["code"] == "need_two_matches"
    assert "唯一" in res["hint"] and "6" not in res["hint"]


# ── dreaming 判据：跳过 ≠ 做过 ──────────────────────────────────────────────


def _seed_run(project_root, run_id, node_type, status):
    d = project_root / "runs" / run_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "summary.json").write_text(json.dumps({
        "run_id": run_id, "node_type": node_type, "status": status,
    }), encoding="utf-8")


def test_last_dreaming_comes_from_the_run_ledger(tmp_path, monkeypatch):
    """账本里的事实伪造不了：没跑过 curator 就没有 completed 的 curator run。"""
    from core import dreaming_scheduler as ds

    home = tmp_path / "home"
    monkeypatch.setenv("HARNESS_FRAMEWORK_HOME", str(home))
    proj = home / "projects" / "ledger_test"
    proj.mkdir(parents=True)

    assert ds.last_dreaming_at("ledger_test") is None
    _seed_run(proj, "1780000000-aaaaaa", "_curator", "incomplete")
    assert ds.last_dreaming_at("ledger_test") is None, "没跑完的不算"
    _seed_run(proj, "1780000100-bbbbbb", "literature", "completed")
    assert ds.last_dreaming_at("ledger_test") is None, "别的节点不算"
    _seed_run(proj, "1780000200-cccccc", "_curator", "completed")
    assert ds.last_dreaming_at("ledger_test") is not None


def test_skip_does_not_get_stamped_as_done(tmp_path, monkeypatch):
    """`/skip-dreaming` 与失败路径都走 clear_pending —— 它不许盖章。

    上一代 `clear_pending()` 无条件调 `stamp_last_dreaming()`，于是跳过一次
    就把 14 天 stale 时钟整个推后。skip 语义被实现成了 done。
    """
    from core import dreaming_scheduler as ds

    home = tmp_path / "home"
    monkeypatch.setenv("HARNESS_FRAMEWORK_HOME", str(home))
    (home / "projects" / "skip_test").mkdir(parents=True)

    ds.mark_pending("skip_test", reason="test")
    ds.clear_pending("skip_test")                 # 模拟 /skip-dreaming
    assert ds.last_dreaming_at("skip_test") is None, "跳过被记成做过了"


def test_stamp_function_is_gone():
    """墓碑：`stamp_last_dreaming`。判据不再建在"希望被写下来的记录"上。"""
    from core import dreaming_scheduler as ds

    assert not hasattr(ds, "stamp_last_dreaming")
    assert not hasattr(ds, "check_candidate_threshold")


def test_contradiction_needs_same_subject_not_just_same_scope(st):
    """真实数据回放的回归钉：判据太松会淹没红旗。

    2026-08-21 首跑：175 条真手册报出 **166 对**"矛盾"。当时的判据是
    「同节点 + 共享 ≥4 个 shingle + 一句带否定词」，而中文 bigram 里同领域
    两句话轻易共享十几个字对 —— 于是几乎恒真。
    误报会让人学会忽略红旗，那比没有红旗更糟。
    """
    # 同一个节点、都带 experiment_log、一条带否定词 —— 但讲的不是同一件事
    _note(st, "在 experiment_log 冻结前务必执行 cited_claim_ids 的存在性验证")
    _note(st, "在 experiment_log 中不要留空的方法学章节标题")
    assert F.scan_for_forgetting(st)["contradictions"] == []


def test_real_contradiction_still_fires(st):
    """另一半：收紧判据不等于把检测器关掉。"""
    _note(st, "低温段应当使用更长的弛豫时间来平衡系统")
    _note(st, "低温段不要使用更长的弛豫时间来平衡系统")
    assert len(F.scan_for_forgetting(st)["contradictions"]) == 1

"""core/memory.py：MEMORY.md 是项目记忆的唯一落盘物。

守的不变量：
  - 节级所有制（手册节不可整节覆写）
  - 零门禁但有机械约束（applies_to / evidence 必填）
  - 近似去重对中文有效（空格位置不改变意思）
  - 历史无标记正文不被吃掉
  - 并发写不丢记录
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from core import memory as M


class _State:
    """最小 state 替身 —— 记忆层只需要 project_worktree。"""

    def __init__(self, worktree: Path | None) -> None:
        self.project_worktree = worktree


@pytest.fixture
def st(tmp_path) -> _State:
    wt = tmp_path / "wt"
    wt.mkdir()
    for c in (["git", "init", "-q"], ["git", "config", "user.email", "t@t"],
              ["git", "config", "user.name", "t"]):
        subprocess.run(c, cwd=wt, check=True)
    s = _State(wt)
    M.ensure_skeleton(s)
    return s


def _note(s, text, **kw):
    kw.setdefault("section", M.SECTION_PITFALL)
    kw.setdefault("nodes", ["experiment"])
    kw.setdefault("run_id", "r_test")
    return M.append_manual(s, text=text, **kw)


# ── 路径 ────────────────────────────────────────────────────────────────────


def test_no_worktree_means_no_project_memory(tmp_path):
    """匿名 run 没有项目记忆 —— 读空、写报错，不静默落到别处。

    上一代的分叉正是从"没绑 worktree 就退回 project_root"开始的，
    造成过两份互不相干的记忆。这里只有一条路径。
    """
    s = _State(None)
    assert M.memory_path(s) is None
    assert M.read_document(s) == ""
    assert M.read_section(s, M.SECTION_LAW) == ""
    with pytest.raises(M.MemoryError_):
        M.write_section(s, M.SECTION_LAW, "x")


# ── 节级所有制 ──────────────────────────────────────────────────────────────


def test_manual_sections_reject_whole_section_overwrite(st):
    """手册节只能逐条追加 —— 整节覆写会把别人写的教训一次抹掉。"""
    for section in M.MANUAL_SECTIONS:
        with pytest.raises(M.MemoryError_, match="逐条追加"):
            M.write_section(st, section, "- 我把整节换掉")


def test_constitution_over_budget_is_refused_with_a_way_out(st):
    """超预算拒写，且报错必须指出该往哪放 —— 硬墙没有出路等于死路。"""
    with pytest.raises(M.MemoryError_) as e:
        M.write_section(st, M.SECTION_GOAL, "目" * M.CONSTITUTION_BYTE_CAP)
    msg = str(e.value)
    assert "手册" in msg and "局面" in msg


def test_laws_are_appended_one_by_one_not_overwritten(st):
    """一条新律不该顺手抹掉旧律 —— 铁律是最不该被顺手抹掉的东西。"""
    with pytest.raises(M.MemoryError_, match="逐条追加"):
        M.write_section(st, M.SECTION_LAW, "- 我要整节覆写铁律")

    M.append_law(st, text="修复必须改产生问题的那一层", derived_from=["不准打补丁"])
    M.append_law(st, text="实验必须先冻预注册", derived_from=["先冻结再跑"])
    assert len(M.laws(st)) == 2


def test_law_carries_its_provenance(st):
    """正文是抽象的，出处是可审计的 —— 两者都要在盘上。"""
    M.append_law(st, text="任何修复必须回答：改的是哪一层？",
                 derived_from=["坚决不能打补丁", "该重写就彻底重写"])
    e = M.laws(st)[0]
    assert e.text.startswith("任何修复")
    assert list(e.derived_from) == ["坚决不能打补丁", "该重写就彻底重写"]
    assert e.at


def test_duplicate_law_is_not_added_twice(st):
    M.append_law(st, text="修复必须改产生问题的那一层", derived_from=["x"])
    res = M.append_law(st, text="修复必须改产生问题的那一层", derived_from=["y"])
    assert res["created"] is False
    assert len(M.laws(st)) == 1


def test_law_can_be_retired_individually(st):
    M.append_law(st, text="第一条：修复必须改产生问题的那一层", derived_from=["a"])
    M.append_law(st, text="第二条：实验必须先冻预注册", derived_from=["b"])
    res = M.retire_law(st, "第一条：修复")
    assert res["status"] == "success"
    remaining = M.laws(st)
    assert len(remaining) == 1 and remaining[0].text.startswith("第二条")


def test_retiring_a_missing_law_lists_what_is_there(st):
    """报错要给出现有清单 —— 逼调用方猜是上一代反复踩的坑。"""
    M.append_law(st, text="唯一的一条铁律在这里", derived_from=["a"])
    res = M.retire_law(st, "根本不存在的前缀")
    assert res["code"] == "not_found"
    assert any("唯一的一条" in c for c in res["current"])


def test_sections_do_not_clobber_each_other(st):
    M.write_section(st, M.SECTION_GOAL, "测 2D Ising 临界温度")
    M.write_section(st, M.SECTION_NARRATIVE, "先扫小 L 再外推")
    _note(st, "这条教训要活过别的节的写入")
    assert "Ising" in M.read_section(st, M.SECTION_GOAL)
    assert "外推" in M.read_section(st, M.SECTION_NARRATIVE)
    assert len(M.manual_entries(st)) == 1


def test_unsectioned_history_is_not_eaten(st):
    """老 MEMORY.md 整份是无标记正文 —— 第一次分节写入不许吃掉它。"""
    p = M.memory_path(st)
    p.write_text("# 老记忆\n\n这段是人手写的，没有任何分节标记。\n", encoding="utf-8")
    M.write_section(st, M.SECTION_GOAL, "新目标")
    doc = M.read_document(st)
    assert "人手写的" in doc
    assert "新目标" in doc


# ── 零门禁 + 机械约束 ───────────────────────────────────────────────────────


def test_note_lands_immediately_no_queue(st):
    """写下来就是入册 —— 没有候选队列，没有人审核环节。"""
    res = _note(st, "声明检查通过前必须实际读 QC 结果")
    assert res["created"] is True
    assert "QC" in M.read_section(st, M.SECTION_PITFALL)


def test_applies_to_is_required_and_says_why(st):
    """写不出适用面 = 还没想清楚。报错要讲清它为什么必填。"""
    with pytest.raises(M.MemoryError_) as e:
        M.append_manual(st, text="一条没有适用面的教训",
                        section=M.SECTION_PITFALL, run_id="r")
    assert "送达地址" in str(e.value) and "遗忘判据" in str(e.value)


def test_evidence_is_required(st):
    with pytest.raises(M.MemoryError_, match="evidence"):
        M.append_manual(st, text="一条没有证据的教训",
                        section=M.SECTION_PITFALL, nodes=["experiment"])


def test_short_is_accepted_empty_is_not(st):
    """判决拆除：字数闸删——长度验不出诚意，短条目原样入册。"""
    rec = _note(st, "短")
    assert rec


# ── 去重 ────────────────────────────────────────────────────────────────────


def test_chinese_near_dup_merges_regardless_of_spacing(st):
    """中文里空格位置是任意的，不该改变去重判定。

    这条是回归钉：按空格分段再取 bigram 时，跨空格的字对凭空消失，
    实测 Jaccard 掉到 0.571，近似去重整个不响。
    """
    _note(st, "声明所有检查通过前必须实际读 QC 结果，否则会 hallucinate 通过")
    res = _note(st, "声明 所有检查通过 之前 必须 实际 读 QC 结果 否则 会 hallucinate 通过")
    assert res["created"] is False
    assert res["seen"] == 2
    assert len(M.manual_entries(st, section=M.SECTION_PITFALL)) == 1


def test_genuinely_different_lessons_are_not_merged(st):
    """另一半：只测"会合并"会让"把什么都合并"也通过。"""
    _note(st, "网格加密到 128 以下有限尺度效应会吃掉临界指数")
    res = _note(st, "低温段的自旋翻转接受率必须按温度重标定")
    assert res["created"] is True
    assert len(M.manual_entries(st, section=M.SECTION_PITFALL)) == 2


def test_recurrence_escalates_to_defect_and_says_stop_recording(st):
    """复发不是知识，是待修的系统性缺陷 —— 到阈值要劝停止记录。"""
    base = "experiment_log 的 verdict 章节又一次缺失导致质量门失败"
    res = None
    for i in range(M.DEFECT_RECURRENCE):
        res = _note(st, base + ("" if i == 0 else f" 第{i}次"))
    assert res["defect"] is True
    assert "修根因" in res["note"]
    assert len(M.manual_entries(st, section=M.SECTION_PITFALL)) == 1


# ── 往返 ────────────────────────────────────────────────────────────────────


def test_entry_metadata_round_trips(st):
    _note(st, "冻结前必须先登记 chunk", tools=["freeze_artifact"],
          nodes=["experiment", "postprocess"], run_id="r42", commit="abc1234")
    e = M.manual_entries(st, section=M.SECTION_PITFALL)[0]
    assert e.tools == ("freeze_artifact",)
    assert e.nodes == ("experiment", "postprocess")
    assert e.run_id == "r42" and e.commit == "abc1234"
    assert e.seen == 1


def test_unparseable_lines_are_kept_not_dropped(st):
    """解析不了的行不丢 —— 记忆的默认动作是保留，不是丢弃。"""
    M.write_section(st, M.SECTION_NARRATIVE, "x")   # 先建文档
    doc = M.read_document(st)
    doc = doc.replace(f"<!-- section:{M.SECTION_PITFALL} -->\n",
                      f"<!-- section:{M.SECTION_PITFALL} -->\n- 一条没有元信息的老条目\n")
    M.memory_path(st).write_text(doc, encoding="utf-8")
    entries = M.manual_entries(st, section=M.SECTION_PITFALL)
    assert len(entries) == 1
    assert "老条目" in entries[0].text


# ── 并发 ────────────────────────────────────────────────────────────────────


def test_concurrent_writes_do_not_lose_entries(st):
    """并行 subagent 是常态。上一代无锁，实测会静默吞掉记录。"""
    import threading

    def w(i: int) -> None:
        try:
            _note(st, f"并发教训第 {i} 条，内容互不相同 {'甲乙丙丁戊己庚辛'[i]}",
                  run_id=f"r{i}")
        except M.MemoryError_:
            pass

    threads = [threading.Thread(target=w, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(M.manual_entries(st, section=M.SECTION_PITFALL)) == 8

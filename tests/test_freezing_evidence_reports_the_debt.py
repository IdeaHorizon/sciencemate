"""宣布证据的那一刻，欠账要报在手边。

## 病例（2026-09-07 真机）

`render_commitment_brief` 已经把逐条 ⬜/✅ 清单摆给模型看了 —— **每个节点每一轮**，
连字段格式都写了。experiment 在 160 多轮里看着那 8 个 ⬜，把 experiment_log 冻上，
收工。硬闸在**三次派工之后**的 writing 输入门上：

    writing 白跑一轮（还排版编译出一份「我为什么写不出论文」的 PDF）
      → 决策卡 REDIRECT → experiment 回去补账 → 再派一次 writing

缺的不是「告诉过没有」，是**告诉的时机**。常驻简报是背景，可以一直往后放；冻结这
份产物是在宣布「这就是我这一轮的证据」，欠账在那一刻才真正变成现在就该处理的事。

同一条原理对着人的那半边是 PR#843（把闭合账亮到决策卡上）。这条对着模型。

## 边界

**不拒绝。** 冻结照常成功 —— 这一层早就定过调子（顺序闸降格：冻结件机械写入未闭合
状态即账真，拒绝只制造第二份判决）。这里只把事实送到手边。
"""
from __future__ import annotations

import pytest

import shared.tools.library.artifacts_extra as ax


class _Tally:
    def __init__(self, *, total, fulfilled, open_items=()):
        self.total = total
        self.fulfilled = fulfilled
        self.open_items = tuple(open_items)
        self.open_numeric = len(self.open_items)
        self.open_statement = 0
        self.degraded = 0
        self.degraded_items = ()

    @property
    def open_total(self) -> int:
        return len(self.open_items)


@pytest.fixture
def owing(monkeypatch):
    """一份带兑现账的产物 + 一本欠着 2/8 的闭合账。"""
    import core.prereg_commitments as pc

    monkeypatch.setattr(pc, "closure_tally", lambda _s: _Tally(
        total=8, fulfilled=6,
        open_items=("Q1_Tc：测出 Tc 与不确定度", "Q2_chi_peak：χ 峰落在窗内")))
    return {"type": "experiment_log"}


def test_the_debt_is_reported_when_evidence_is_frozen(owing) -> None:
    """核心：欠账、欠哪几条、写在哪个字段、以及不补的后果，都要在。"""
    note = ax._closure_debt_note(object(), owing)

    assert note, "冻结带兑现账的产物时一个字都没说"
    assert "2/8" in note, "没报欠了几条"
    assert "Q1_Tc" in note and "Q2_chi_peak" in note, "没说欠的是哪几条"
    assert "closure_discharges" in note and "measured_metrics" in note, "没说写在哪"
    assert "散文" in note, (
        "没点破那个真实的错法 —— 兑现写在正文里、账本只读结构化字段"
    )
    assert "writing" in note and "REDIRECT" in note, (
        "没说不补的后果；只说『你欠着』不说代价，模型有理由继续往后放"
    )


def test_it_names_the_cheap_way_out(owing) -> None:
    """要给出**现在就能走**的那一步，否则等于只报警不给路。"""
    note = ax._closure_debt_note(object(), owing)
    assert "amend_artifact" in note, "没告诉它现在怎么补"
    assert "not_run" in note or "not_applicable" in note, (
        "没给『真做不到』的显式出口 —— 那会逼它要么造假要么卡死"
    )


def test_an_artifact_without_a_ledger_is_left_alone(monkeypatch) -> None:
    """不带兑现账的产物类型：一个字都不加。"""
    import core.prereg_commitments as pc

    monkeypatch.setattr(pc, "closure_tally", lambda _s: _Tally(
        total=8, fulfilled=0, open_items=("Q1",)))
    assert ax._closure_debt_note(object(), {"type": "figure"}) is None


def test_a_settled_ledger_says_nothing(monkeypatch) -> None:
    """账清了就闭嘴 —— 每次冻结都唠叨一遍，很快就没人读了。"""
    import core.prereg_commitments as pc

    monkeypatch.setattr(pc, "closure_tally", lambda _s: _Tally(total=8, fulfilled=8))
    assert ax._closure_debt_note(object(), {"type": "experiment_log"}) is None


def test_research_without_frozen_commitments_says_nothing(monkeypatch) -> None:
    """`closure_tally` 返回 None = **没有承诺**，不是零兑现。两者含义相反。"""
    import core.prereg_commitments as pc

    monkeypatch.setattr(pc, "closure_tally", lambda _s: None)
    assert ax._closure_debt_note(object(), {"type": "experiment_log"}) is None


def test_a_broken_diagnostic_never_breaks_the_freeze(monkeypatch) -> None:
    """冻结是「从此改不了」的那一步，不能因为一个诊断算不出来而失败。"""
    import core.prereg_commitments as pc

    def _boom(_s):
        raise RuntimeError("ledger exploded")

    monkeypatch.setattr(pc, "closure_tally", _boom)
    assert ax._closure_debt_note(object(), {"type": "experiment_log"}) is None


def test_the_note_actually_reaches_the_freeze_result() -> None:
    """判据落在**返回给模型的那份结果**上，不是"有这么个函数"。

    只写 helper 不接线，是本仓库反复出现的死法（修复落在没人走的路上）。
    """
    import ast
    import pathlib

    tree = ast.parse(pathlib.Path(ax.__file__).read_text(encoding="utf-8"))
    called = any(
        isinstance(n, ast.Call) and getattr(n.func, "id", "") == "_closure_debt_note"
        for n in ast.walk(tree)
    )
    assert called, "`_closure_debt_note` 没有被调用 —— 模型永远收不到"

    src = pathlib.Path(ax.__file__).read_text(encoding="utf-8")
    assert 'result["note"]' in src.split("_closure_debt_note(state, record)")[1][:400], (
        "算出来了却没并进 result['note'] —— 模型读的是 note"
    )


def test_freezing_still_succeeds_while_owing() -> None:
    """不拒绝。这一层的调子是「机械写入未闭合状态即账真，拒绝只制造第二份判决」。"""
    import inspect

    src = inspect.getsource(ax._closure_debt_note)
    assert "status" not in src.split('"""')[2], (
        "这个函数动了 status —— 它只该产出一段文字，不该改变冻结的成败"
    )

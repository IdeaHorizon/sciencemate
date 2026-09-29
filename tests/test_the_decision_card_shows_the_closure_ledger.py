"""按下 PROCEED 的那个人，得先看见闭合账的分数。

## 病例（2026-09-07 真机第五轮）

experiment 冻结 `experiment_log` 时闭合账是 **0/8**。节点照常收工、reviewer 给了
`approve`、决策卡递到我面前 —— **屏幕上没有任何地方写着那个 0**。我按了 PROCEED。

三次派工之后，writing 的输入门读同一本账，把它拦下来：

    preflight_status = blocked_missing_required_upstream
    closure_ledger.fulfilled = 0/8

于是：writing 白跑一轮（还排版编译出一份「我为什么写不出论文」的 PDF）→ 决策卡
REDIRECT → experiment 回去补账 → 再派一次 writing。**绕路的原因不是没人知道，
是拿决策的人看不见。**

## 不是「没告诉模型」

`render_commitment_brief` 每个节点每一轮都在打印逐条 ⬜/✅ 清单，连字段格式都写
了。模型在 160 多轮里看着那 8 个 ⬜ 收了工。所以这条判据要修的不是"提示不够"，
而是**这个事实没有送到做决定的那一方手里**。

同一个文件里已经有先例：`owner_spec_loaded=false` 那条警告，加它的理由逐字相同
—— *"自报失效必须有消费者……决策包是人看审查结果的唯一入口，就在这里亮出来。"*

## 判据

1. 有欠账时，卡上必须有那个分数、那些未兑现条目、以及「现在 PROCEED 会被下游拦
   回来」这句后果；
2. 定性降级（🔻）要单独标出来 —— 它计入 fulfilled，混在里面就等于把"数据不可得
   所以估了一个"洗成"测出来了"；
3. 没有冻结预注册时（自由探索/服务型工作）**一个字都不加** —— `closure_tally`
   返回 None 是"没有承诺"，不是"零兑现"；
4. 这个诊断算不出来时不许把整张卡弄崩。
"""
from __future__ import annotations

import shared.tools.library.decision_package as dp


class _Tally:
    """够用的 ClosureTally 替身（真对象是 frozen dataclass，构造要全字段）。"""

    def __init__(self, *, total, fulfilled, open_items=(), degraded=0, degraded_items=()):
        self.total = total
        self.fulfilled = fulfilled
        self.open_items = tuple(open_items)
        self.open_numeric = len(self.open_items)
        self.open_statement = 0
        self.degraded = degraded
        self.degraded_items = tuple(degraded_items)

    @property
    def open_total(self) -> int:
        return len(self.open_items)


def _lines_for(monkeypatch, tally) -> list[str]:
    import core.prereg_commitments as pc

    monkeypatch.setattr(pc, "closure_tally", lambda _state: tally)
    return dp._closure_ledger_lines(object())


def test_an_unpaid_ledger_is_on_the_card_with_its_consequence(monkeypatch) -> None:
    """0/8 那次：分数、未兑现条目、以及「现在 PROCEED 会被拦回来」都要在。"""
    lines = _lines_for(monkeypatch, _Tally(
        total=8, fulfilled=0,
        open_items=("Q1_Tc：测出 Tc 与不确定度", "Q2_chi_peak：χ 峰落在窗内")))
    text = "\n".join(lines)

    assert "0/8" in text, f"卡上没有那个分数：{text}"
    assert "Q1_Tc" in text and "Q2_chi_peak" in text, "没列出欠着的是哪几条"
    assert "writing" in text and "拦" in text, (
        "没说出后果 —— 人看到一个数字不会知道现在放行意味着下游白跑一轮"
    )
    assert "closure_discharges" in text and "measured_metrics" in text, "没说兑现写在哪"


def test_a_degraded_item_is_never_shown_as_measured(monkeypatch) -> None:
    """🔻 单独标出来：它计入 fulfilled，混在里面就是把降级洗成测量。"""
    lines = _lines_for(monkeypatch, _Tally(
        total=3, fulfilled=3, degraded=1,
        degraded_items=("cost_reduction_pct：数据不可得，按同类估",)))
    text = "\n".join(lines)

    assert "3/3" in text
    assert "🔻" in text and "降级" in text, f"降级条目没被单独标注：{text}"
    assert "cost_reduction_pct" in text


def test_a_settled_ledger_says_so_without_the_warning(monkeypatch) -> None:
    """全兑现时报分数，但不该再吓唬人。"""
    lines = _lines_for(monkeypatch, _Tally(total=8, fulfilled=8))
    text = "\n".join(lines)
    assert "8/8" in text
    assert "拦" not in text, "没有欠账还在警告 —— 那样警告很快就没人看了"


def test_research_without_frozen_commitments_adds_nothing(monkeypatch) -> None:
    """`closure_tally` 返回 None = **没有承诺**，不是零兑现。两者含义相反。

    自由探索 / 服务型工作不该在卡上凭空多出一段「0/0 未兑现」。
    """
    assert _lines_for(monkeypatch, None) == []


def test_a_broken_diagnostic_never_breaks_the_card(monkeypatch) -> None:
    """算不出来就当没有 —— 决策包是人看审查结果的唯一入口。"""
    import core.prereg_commitments as pc

    def _boom(_state):
        raise RuntimeError("ledger exploded")

    monkeypatch.setattr(pc, "closure_tally", _boom)
    assert dp._closure_ledger_lines(object()) == []


def test_the_card_actually_carries_it() -> None:
    """判据落在**卡片正文**上，不是"有这么个函数"。

    只写 helper 不接到渲染上，是这个仓库里反复出现的"修复落在没人走的路上"。
    """
    import ast
    import pathlib

    tree = ast.parse(pathlib.Path(dp.__file__).read_text(encoding="utf-8"))
    wired = any(
        isinstance(node, ast.keyword)
        and node.arg == "closure_lines"
        and isinstance(node.value, ast.Call)
        and getattr(node.value.func, "id", "") == "_closure_ledger_lines"
        for node in ast.walk(tree)
    )
    assert wired, "`_closure_ledger_lines` 没有被传进决策包渲染 —— 人还是看不见"

    rendered = any(
        isinstance(node, ast.Name) and node.id == "closure_lines"
        for fn in ast.walk(tree)
        if isinstance(fn, ast.FunctionDef) and fn.name == "_render_decision_package"
        for node in ast.walk(fn)
    )
    assert rendered, "渲染函数收了参数却没用它"

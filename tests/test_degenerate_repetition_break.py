"""输出退化成复读 → 停机，而且**不许再加输出预算**。

## 事故（2026-08-17 实测，UI 上并发跑两个 E2E）

literature 节点前 7 轮完全正常（输出 123–487 token、工具调用正常、跑完 4 组
检索、识别出真实文献）。第 7 轮两次 `search_papers` 把上下文从 2.4 万顶到
6.9 万，第 8 轮就崩了：

    turn8  输出 16,384 撞上限、0 工具调用、纯复读
    turn9  输出 32,768 撞上限、0 工具调用、复读词组本身在崩解
    turn10 输出 11,105，还是复读

**框架自己把伤害翻了一倍**：截断恢复看到 `finish=length` 就把预算从 16,384
抬到 32,768 —— 那套逻辑是为"内容真的写不下"设计的，对复读是火上浇油。

三轮共 6 万输出 token、零产物。整个会话烧掉 178 万 token。

## 为什么现有三道都接不住

  - `repeat`（进展熔断的判决档）要求两轮输出**逐字节相同**；复读文本每轮不同
  - `stall`（证据档）只警告不停机，而且 15 轮才提示一次
  - 截断恢复只数"给了几次机会"，不看那几次的输出是不是同一种坏

判据取自 67 条真实长输出：复读的**尾部**压缩比 ≤0.05，正常收尾的 ≥0.17。
看尾部不看整段 —— 实测有一条 34,339 字的输出开头是真实论文清单、结尾崩成
`.__.__.__`，整段压缩比被好开头稀释到 0.0412，只看尾部立刻掉到 0.0147。
"""
from __future__ import annotations

import pytest

from core.progress_breaker import (
    is_degenerate_repetition,
    should_not_raise_budget,
    tail_repetition_ratio,
)

# —— 真实样本的形态（取自实测 transcript 的尾部）——
REAL_DEGENERATE = [
    "我已经收集到足够多的核心文献。" * 200,
    "先写 prereg 草稿。" * 300,
    "帚" * 3000,
    ".__" * 1000,
    "罚/" * 1500,
    "reminder/" * 400,
    "677_" * 800,
]

# 正常收尾的长输出：中文分析 / 英文推理 / 带结构的清单
REAL_HEALTHY = [
    "让我先读取关键的上游 artifact 和 KB 记录，确认 experiment 的实际状态，"
    "然后修正 manuscript 中的结论。这里的关键是 H2 的判定依赖于 clean_results "
    "里的 tau 分布，而那份产物在第 3 轮被重算过一次。" * 8,
    "Let me strip the extra fields from journal_fit and try again. The venue "
    "profile expects only title, abstract and keywords; everything else is "
    "rejected entirely or reported as a missing field." * 10,
    "\n".join(f"{i}. **Paper {i}** — Author{i} et al. {1990+i}, DOI 10.1000/x{i} "
              f"— 研究了 LJ 流体在 rho*={0.6+i/50:.2f} 下的自扩散行为，"
              f"给出 D(T) 的幂律拟合与误差棒。" for i in range(40)),
]


@pytest.mark.parametrize("text", REAL_DEGENERATE)
def test_degenerate_outputs_are_caught(text):
    assert is_degenerate_repetition(text), f"漏判：尾部压缩比 {tail_repetition_ratio(text):.4f}"
    assert should_not_raise_budget(text)


@pytest.mark.parametrize("text", REAL_HEALTHY)
def test_healthy_outputs_are_not_flagged(text):
    """误伤检验 —— 停机是判决，误停一条正常 run 比多说一句话贵得多。"""
    assert not is_degenerate_repetition(text), (
        f"误伤：尾部压缩比 {tail_repetition_ratio(text):.4f}")


def test_a_good_prefix_does_not_hide_a_degenerate_tail():
    """整段压缩比会被好开头稀释 —— 这正是实测里那条 34,339 字输出的形态。"""
    good = ("最相关的候选论文：1. **Hussain et al. 2025** — A universal formula for "
            "the diffusion coefficient of Lennard-Jones fluids；2. **Meier et al. "
            "2004** — 高精度基准数据；3. **Asad & Wu 2011** — rho*=0.84 附近。" * 30)
    degenerate_tail = ".__" * 800
    assert not is_degenerate_repetition(good)
    assert is_degenerate_repetition(good + degenerate_tail), "尾部崩了就该判出来"


def test_short_or_empty_output_is_never_flagged():
    """短输出的压缩比天然偏高/偏低都不可靠，不能拿它停机。"""
    for text in ("", None, "好的。", "调用工具。"):
        assert not is_degenerate_repetition(text)


def test_thresholds_keep_the_verdict_stricter_than_the_budget_brake():
    """两档必须有间隔：宽的那档只是不加预算，严的那档才停机。"""
    from core.progress_breaker import (
        _DEGENERATE_TAIL_RATIO, _NO_BUDGET_RAISE_TAIL_RATIO,
    )
    assert _DEGENERATE_TAIL_RATIO < _NO_BUDGET_RAISE_TAIL_RATIO

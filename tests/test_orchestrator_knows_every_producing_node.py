"""调度器的认知与实际节点集合不能分叉。

## 这条测试防的是什么

2026-08-23 真跑抓到：`observation` 和 `derivation` 早就存在、`callable_nodes`
是 `["*"]`、框架也扫盘注入了它们的输入契约 —— 但 `_orchestrator` 的引导里
点名次数是 **1 和 0**（对比 writing 20 / experiment 17 / hypothesis 14）。
那张「先读这张表再调度」的分类表里，producing 一行只写了三个节点。

后果不是派不了，是**没有依据去判断该不该派**：真跑里调度器面对一道命题裁决
题直接自己答了 —— 答案数学上完全正确，但只有自述，没有验证章、没有假设账本、
没有冻结时间戳。同一道题派 derivation 跑得到 17 轮 + 6 条账本可反查的章。

## 判据：扫盘对比，不是断言字符串在不在

「断言文案还在 ≈ 什么都没断言」。这里测的是**两个集合相等**：
框架实际存在的 producing 节点，和调度器引导里点名的 producing 节点。

新增一个 producing 节点却忘了让调度器知道，这条测试会红 —— 而这正是
2026-08-23 那个缺陷的形态（机制到位、认知缺席）。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from core.bootstrap import bootstrap  # noqa: E402
from core.loader import list_harnesses, load_harness  # noqa: E402


@pytest.fixture(autouse=True)
def _boot():
    bootstrap()


def _producing_nodes() -> set[str]:
    """框架里实际存在的 producing 节点（扫盘现算，不写名单）。"""
    out = set()
    for name in list_harnesses():
        if name.startswith("_"):
            continue
        try:
            harness = load_harness(name)
        except Exception:
            continue
        if not harness.is_service:
            out.add(name)
    return out


def _orchestrator_prompt() -> str:
    return load_harness("_orchestrator").system_prompt or ""


def test_every_producing_node_is_named_in_the_dispatch_guidance():
    """★ 每个 producing 节点都必须在调度器引导里被点名。

    不点名 = 调度器没有依据判断什么时候派它。框架的契约注入只告诉它
    「这个节点存在、参数这么传」，不告诉它「什么时候该派」。
    """
    prompt = _orchestrator_prompt()
    missing = sorted(n for n in _producing_nodes() if n not in prompt)
    assert not missing, (
        f"这些 producing 节点在 _orchestrator 引导里一次都没被点名：{missing}。\n"
        f"框架能派它们（callable_nodes + 契约扫盘注入），但调度器没有依据判断"
        f"**什么时候**该派 —— 2026-08-23 实测：derivation 因此在一道该派它的"
        f"命题裁决题上被跳过，调度器自己答了（答案对，但没有可核验记录）。")


def test_the_classification_table_lists_them_as_producing():
    """★ 光被提到不够 —— 要出现在那张「先读这张表再调度」的分类表里。

    调度器读的是那张表来决定走不走 post-producing 3-step flow。
    漏在表外的节点会被当成 service 处理：不调 reviewer、不调 curator、
    不出 decision package —— 一份没人审的证据记录直接进了项目。
    """
    prompt = _orchestrator_prompt()
    marker = "| **producing** |"
    assert marker in prompt, "分类表的 producing 行不见了 —— 引导结构变了？"
    row = next(line for line in prompt.splitlines() if marker in line)
    missing = sorted(n for n in _producing_nodes() if f"`{n}`" not in row)
    assert not missing, (
        f"这些 producing 节点不在分类表的 producing 行里：{missing}。\n"
        f"当前那一行：{row.strip()[:200]}\n"
        f"漏在表外 = 调度器会当它是 service，跑完不走审查流。")


def test_the_dispatch_criterion_is_about_being_cited_not_difficulty():
    """★ 「该不该起 producing run」的判据必须是**会不会被下游引用**。

    2026-08-23 wangd 拍板。判据挂在"难不难"上会两头错：简单但要写进论文的
    引理被直答（没有可核验记录），复杂但用户只是随口问的问题被起了 run
    （浪费 + 决策疲劳）。
    """
    prompt = _orchestrator_prompt()
    assert "会不会被别人引用" in prompt or "会不会被下游引用" in prompt, (
        "找不到「会不会被引用」这条判据 —— 它是 2026-08-23 定的调度依据")
    # 反面也要在场：不给反面，引导会被读成"什么都派"
    assert "随口问" in prompt, (
        "缺少反面 —— 不写明「随口问就直接答」，调度器会对每个问题都起 run，"
        "那比现在更糟")


def test_the_three_evidence_modalities_are_distinguished():
    """★ 三种取证方式的**种差**要写清 —— 不然调度器只能按名字猜。

    experiment / observation / derivation 是并列的取证方式，不是流程先后。
    最常见的误派是「算一下」听起来像跑东西 → 派给 experiment，
    而防伪造执行的闸拦不住偷换假设。
    """
    prompt = _orchestrator_prompt()
    for node in ("experiment", "observation", "derivation"):
        assert f"`{node}`" in prompt, f"{node} 没有出现在模态对照里"
    assert "原罪" in prompt, "要写清每种模态的闸专门防什么，否则区分不出种差"

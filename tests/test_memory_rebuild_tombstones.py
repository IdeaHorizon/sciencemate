"""墓碑：记忆重建（2026-08-21）删掉的东西，以及删它们的判据。

删除清单本身是设计的一部分 —— 这个文件让"为什么删"留在仓库里，
而不是只留在某次 PR 描述里。

替代守卫：
  tests/test_memory_core.py       节级所有制 / 零门禁 + 机械约束 / 中文去重
  tests/test_memory_tools.py      引文防伪 / 单写者 / 首用附单 / reviewer 红旗
  tests/test_recall.py            送达无门控 + 全节点覆盖
"""
from __future__ import annotations

import pathlib

REPO = pathlib.Path(__file__).resolve().parents[1]

#: 模块 → 删它的判据
DELETED_MODULES = {
    "core/memory_v2.py":
        "候选队列 + topic 分文件 + episodes 的实现。三样都退场了。",
    "core/run_end_hooks.py":
        "每个 producing run 烧 1–2 次 LLM call 写叙事摘要，读取方接近零。"
        "日志已有 transcript / Git history / 决策账本三份权威载体。",
    "shared/tools/library/memory_v2_tools.py":
        "5 个工具，其中 3 个已从模型面撤下却仍被 prompt 点名（幽灵工具），"
        "1 个（add_runtime_directive）在平台模式下是空操作还回报 success。",
    "shared/tools/library/memory_v2_curator.py":
        "curator 的 4 个加工工具。加工链路整体退场 —— 写下来就是入册。",
    "shared/tools/library/recall.py":
        "recall_experience。并进 memory_recall：主动路曾是被动路的真子集"
        "（少三个 bucket），模型不查它是理性的。",
}

#: 符号 → 删它的判据
DELETED_SYMBOLS = {
    "add_runtime_directive":
        "写 memory/directives.md，而平台模式的注入分支只读 MEMORY.md —— "
        "写进去从不被读，还回报 success。语义归宪法与叙事两节。",
    "add_memory_candidate":
        "候选队列的入口。队列是「在出生时设门」：值不值得留的判据在写下那一刻"
        "不具备，实测 47% 从未被加工、最老积压 49 天。",
    "pre_run_briefing":
        "被 kb_query 门控挡死 —— 同一字段在两个消费方语义相反，"
        "实测 9 节点裸调度 8 个收不到。改为 memory_onboarding（无门控）。",
    "check_candidate_threshold":
        "数的是候选队列积压条数，而队列不存在了。",
    "_render_directives_for_node":
        "directives.md 的 per-node 过滤，随该通道一起退场。",
}


def test_deleted_modules_stay_deleted():
    """删了就别悄悄回来。要恢复请先推翻 RFC 里删它的判据。"""
    for rel, why in DELETED_MODULES.items():
        assert not (REPO / rel).exists(), f"{rel} 又出现了。当初删它是因为：{why}"


def test_deleted_symbols_are_gone_from_live_code():
    """符号级扫盘 —— 文案、prompt、yaml 一并算，幽灵工具名就是这么来的。"""
    live = []
    for pat in ("core", "shared", "nodes"):
        for f in (REPO / pat).rglob("*"):
            if f.suffix not in (".py", ".yaml", ".yml", ".md"):
                continue
            if "backup" in f.name or "test_runs" in str(f) or ".example" in str(f):
                continue
            if f.name == "test_memory_rebuild_tombstones.py":
                continue
            try:
                text = f.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for sym in DELETED_SYMBOLS:
                for line in text.splitlines():
                    if sym in line and not line.strip().startswith(("#", "//")):
                        live.append(f"{f.relative_to(REPO)}: {line.strip()[:90]}")
    assert not live, (
        "已删符号还活在代码/文案里（幽灵工具名会让模型跳过整道检查）：\n"
        + "\n".join(live[:20]))

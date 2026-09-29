#!/usr/bin/env python3
"""在真实历史 checkpoint 上回放压缩策略，量三件事：

1. 压了多少（tokens before/after，够不够回到窗口内）
2. 核心内容保没保住（原文里的 artifact id / 子 run id / 文件路径，在压缩后
   要么还在文本里，要么其所在消息带可恢复指针 —— 两者都不满足才算真丢）
3. 前缀稳定性（第一条被改动消息之前有多少 tokens 逐字节不变 —— prompt cache
   能继续命中的部分）

用法：.venv/bin/python scripts/replay_summarizer_on_checkpoints.py [N]
"""
from __future__ import annotations

import asyncio
import glob
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.bootstrap import bootstrap
from core.harness import SummarizerConfig
from core.llm import LLMMessage
from core import summarizer as sm

FACT_PATTERNS = [
    re.compile(r"\b[a-z_]+__[A-Za-z0-9][\w\-]{3,}"),   # artifact ids
    re.compile(r"\b\d{10}-[0-9a-f]{6}\b"),               # run ids
    re.compile(r"\bH[123]\b"),                            # hypothesis ids
]


class _H:  # 最小 harness 替身：只提供 summarizer cfg + 窗口
    node_type = "_replay"
    max_context_tokens = 120_000
    summarizer = SummarizerConfig()


def _facts(text: str) -> set[str]:
    out: set[str] = set()
    for pat in FACT_PATTERNS:
        out.update(pat.findall(text))
    return out


def _msg_text(m: LLMMessage) -> str:
    parts = [m.content or ""]
    if m.tool_calls:
        parts.append(json.dumps(m.tool_calls, ensure_ascii=False, default=str))
    return "\n".join(parts)


def _all_text(msgs: list[LLMMessage]) -> str:
    return "\n".join(_msg_text(m) for m in msgs)


def _prefix_stable_tokens(old: list[LLMMessage], new: list[LLMMessage]) -> int:
    n = 0
    for a, b in zip(old, new):
        if a.role == b.role and (a.content or "") == (b.content or "") \
           and a.tool_calls == b.tool_calls:
            n += sm.estimate_tokens([a])
        else:
            break
    return n


async def replay(path: str) -> dict | None:
    d = json.load(open(path))
    raw = d.get("messages") or []
    if len(raw) < 10:
        return None
    msgs = [LLMMessage(**{k: v for k, v in m.items() if k in
            ("role", "content", "tool_calls", "tool_call_id", "name",
             "reasoning_content")}) for m in raw]
    before = sm.estimate_tokens(msgs)
    if before < 60_000:
        return None

    ctx = sm.SummarizerContext(harness=_H(), state=None, messages=msgs,
                               estimated_tokens=before, llm=None, turn=99)
    new = await sm._strategy_clear_tool_results(ctx)
    after = sm.estimate_tokens(new)

    # 旧策略对照：drop_tool_results（截 200 字符，无指针）
    ctx2 = sm.SummarizerContext(harness=_H(), state=None, messages=msgs,
                                estimated_tokens=before, llm=None, turn=99)
    old_style = await sm._strategy_drop_tool_results(ctx2)
    old_after = sm.estimate_tokens(old_style)

    # 事实三态：可见 / 凭指针可恢复 / 销毁。
    # 一个事实若不再可见，看它原来所在的消息：被清成带指针的占位符 → 可恢复
    # （一次工具调用取回）；被无指针截断 → 销毁。
    orig_facts = _facts(_all_text(msgs))
    new_text = _all_text(new)
    old_text = _all_text(old_style)
    cleared_idx = {i for i, m in enumerate(new) if (m.content or "").startswith(
        (sm._CLEARED_MARKER, sm._COMPACTED_MARKER))}
    fact_home: dict[str, set[int]] = {}
    for i, m in enumerate(msgs):
        for f in _facts(_msg_text(m)):
            fact_home.setdefault(f, set()).add(i)
    recoverable_new, destroyed_new, lost_old = 0, 0, 0
    for f in orig_facts:
        if f not in new_text:
            if fact_home.get(f, set()) & cleared_idx:
                recoverable_new += 1
            else:
                destroyed_new += 1
        if f not in old_text:
            lost_old += 1

    cleared = sum(1 for m in new if (m.content or "").startswith(
        (sm._CLEARED_MARKER, sm._COMPACTED_MARKER)))
    return {
        "path": path.split("/runs/")[-1].split("/")[0][:30],
        "before": before, "after": after, "old_after": old_after,
        "cleared": cleared,
        "facts": len(orig_facts),
        "facts_recoverable": recoverable_new,
        "facts_destroyed": destroyed_new, "facts_lost_old": lost_old,
        "stable_prefix": _prefix_stable_tokens(msgs, new),
        "back_in_window": after <= 120_000,
    }


#: checkpoint 可能在的位置。布局变过好几轮，这里全扫 —— 只写一条路径的代价
#: 是：布局一变，工具就安静地回放 0 个 checkpoint、打印一张空表、exit 0。
#: 任何人按纪律跑一遍都会以为验过了。（实测：旧的单条 glob 已经扫不到任何东西。）
CHECKPOINT_GLOBS = (
    # 当前布局
    "output/*/messages_checkpoint.json",
    "../harness-framework-latest/output/*/messages_checkpoint.json",
    # 项目嵌套布局
    "~/.harness-framework/projects/*/runs/*/messages_checkpoint.json",
    "projects/*/runs/*/messages_checkpoint.json",
    # 平台 worktree 布局
    "platform/backend/data/project_worktrees/*/*/"
    ".research/cache/runtime/runs/*/messages_checkpoint.json",
)


def find_checkpoints() -> list[str]:
    """扫所有已知布局。`HARNESS_REPLAY_CHECKPOINTS` 可指定额外 glob（冒号分隔）。

    相对 glob **锚定到仓库根和它的父目录**，不是 CWD —— 在 worktree 里跑时
    CWD 的 `..` 根本不是仓库父目录（这条我自己先踩了一次）。
    """
    import os

    repo = Path(__file__).resolve().parent.parent
    anchors = [repo, repo.parent]

    pats = list(CHECKPOINT_GLOBS)
    extra = os.environ.get("HARNESS_REPLAY_CHECKPOINTS")
    if extra:
        pats = [p for p in extra.split(":") if p.strip()] + pats

    seen: set[str] = set()
    out: list[str] = []
    for pat in pats:
        p = Path(pat).expanduser()
        candidates = [str(p)] if p.is_absolute() else [
            str(a / pat) for a in anchors
        ] + [pat]                      # 也试 CWD，兼容手工传相对路径
        for c in candidates:
            for hit in glob.glob(c):
                rp = str(Path(hit).resolve())
                if rp not in seen:
                    seen.add(rp)
                    out.append(rp)
    return out


async def main() -> None:
    bootstrap(force=True)
    found = find_checkpoints()
    if not found:
        # 空表 + exit 0 是最坏的结果：纪律照跑，判据其实归零而没人知道。
        print("✗ 没有找到任何 messages_checkpoint.json —— 本次回放什么都没验证。",
              file=sys.stderr)
        print("  已扫描的位置：", file=sys.stderr)
        for g in CHECKPOINT_GLOBS:
            print(f"    {g}", file=sys.stderr)
        print("  用 HARNESS_REPLAY_CHECKPOINTS='<glob>[:<glob>]' 指定实际位置。",
              file=sys.stderr)
        raise SystemExit(2)

    paths = sorted(found, key=lambda p: -Path(p).stat().st_size)[
        : int(sys.argv[1]) if len(sys.argv) > 1 else 12
    ]
    print(f"找到 {len(found)} 个 checkpoint，回放体积最大的 {len(paths)} 个。\n")
    rows = [r for r in [await replay(p) for p in paths] if r]
    hdr = (f"{'run':32s} {'before':>8s} {'clear后':>8s} {'旧drop后':>8s} "
           f"{'清除':>4s} {'事实':>5s} {'可恢复':>6s} {'销毁':>4s} {'旧销毁':>6s} "
           f"{'稳定前缀':>8s} 回窗")
    print(hdr); print("-" * len(hdr))
    for r in rows:
        print(f"{r['path']:32s} {r['before']:8d} {r['after']:8d} "
              f"{r['old_after']:8d} {r['cleared']:4d} {r['facts']:5d} "
              f"{r['facts_recoverable']:6d} {r['facts_destroyed']:4d} "
              f"{r['facts_lost_old']:6d} "
              f"{r['stable_prefix']:8d} {'✓' if r['back_in_window'] else '✗'}")

    # 判据要能让脚本自己失败，否则表打出来也没人盯着看。
    destroyed = sum(r["facts_destroyed"] for r in rows)
    print()
    if destroyed:
        print(f"✗ 事实销毁 {destroyed} 处 —— 压缩弄丢了既不在文本里、也无可恢复"
              f"指针的信息。这是回归。", file=sys.stderr)
        raise SystemExit(1)
    print(f"✓ {len(rows)} 个 checkpoint 回放完毕，事实销毁 0。")


if __name__ == "__main__":
    asyncio.run(main())

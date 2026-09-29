#!/usr/bin/env python3
"""工具失败率审计 —— 把"卡了但最后过了"变成一个数。

## 为什么需要它

2026-08-10 复盘 v22（那轮**跑出了论文**）：`freeze_and_register` 15 次成功、
22 次失败，失败率 59%。失败原因清一色是同一个跨节点查找缺陷，散落在
experiment_log / clean_results / manuscript 上。

它没让 v22 崩掉，是因为 agent 每次都靠**重试和换工具**磨过去了。于是这个缺陷
以"偶尔卡一下"的形式存在了两个月，没人把它当成 bug —— 直到它恰好落在一条
没有替代路径的链条上（预注册冻结），才变成硬死锁。

**agent 的韧性会掩盖框架缺陷。** 韧性是好事，但它把"框架有洞"翻译成了
"今天有点慢"。这个脚本就是把那层掩盖揭开：**看失败率，不看最终有没有过。**

## 用法

    python scripts/audit_tool_failure_rates.py <worktree 或 project 目录> [--top N]
    python scripts/audit_tool_failure_rates.py <dir> --assert-below 0.15

`--assert-below` 给 CI / E2E 收尾用：任何被调用 ≥5 次的工具，失败率超过阈值
就退出码 1 并列出典型错误。修复的验收判据应该是**这个数降下来**，而不是
"这次跑通了"。
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path


def _iter_transcripts(root: Path):
    yield from root.rglob("transcript.jsonl")


def collect(root: Path) -> tuple[Counter, Counter, dict[str, Counter]]:
    """→ (调用数, 失败数, {工具: 错误摘要计数})"""
    calls: Counter = Counter()
    fails: Counter = Counter()
    reasons: dict[str, Counter] = defaultdict(Counter)
    for path in _iter_transcripts(root):
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            name = event.get("name")
            if not name:
                continue
            kind = event.get("event")
            if kind == "tool_call":
                calls[name] += 1
            elif kind == "tool_result":
                preview = str(event.get("result_preview") or "")
                failed = event.get("ok") is False or '"status": "error"' in preview \
                    or "'status': 'error'" in preview
                if failed:
                    fails[name] += 1
                    # 错误摘要：取前 60 字做聚类键，够区分不同根因，又不至于
                    # 因为 id 不同就散成一堆独立条目。
                    marker = preview.split("error", 1)[-1][:60].strip("\"':, ")
                    reasons[name][marker or "(未给原因)"] += 1
    return calls, fails, reasons


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("--top", type=int, default=12)
    parser.add_argument("--min-calls", type=int, default=5)
    parser.add_argument("--assert-below", type=float, default=None)
    args = parser.parse_args()

    calls, fails, reasons = collect(args.root)
    if not calls:
        print("没有找到任何 transcript。检查路径。")
        sys.exit(2)

    rows = []
    for name, total in calls.items():
        bad = fails.get(name, 0)
        rows.append((bad / total if total else 0.0, total, bad, name))
    rows.sort(reverse=True)

    print(f"{'失败率':>7}  {'调用':>5} {'失败':>5}  工具")
    print("-" * 58)
    for rate, total, bad, name in rows[: args.top]:
        if bad == 0:
            continue
        print(f"{rate:>6.0%}  {total:>5} {bad:>5}  {name}")
        for marker, count in reasons[name].most_common(2):
            print(f"{'':>14}   └ ×{count} {marker[:52]}")

    print(f"\n总计：{sum(calls.values())} 次调用，{sum(fails.values())} 次失败"
          f"（{sum(fails.values()) / max(1, sum(calls.values())):.0%}）")

    if args.assert_below is not None:
        offenders = [
            (rate, total, bad, name)
            for rate, total, bad, name in rows
            if total >= args.min_calls and rate > args.assert_below
        ]
        if offenders:
            print(f"\n✗ 断言失败：{len(offenders)} 个工具的失败率超过 "
                  f"{args.assert_below:.0%}（调用 ≥{args.min_calls} 次）。")
            print("  agent 会靠重试磨过去 —— 所以这类缺陷不会让 run 变红，"
                  "只会让它变慢变贵。")
            for rate, total, bad, name in offenders:
                print(f"    {name}: {bad}/{total} = {rate:.0%}")
            sys.exit(1)
        print(f"\n✓ 断言通过：没有工具的失败率超过 {args.assert_below:.0%}。")


if __name__ == "__main__":
    main()

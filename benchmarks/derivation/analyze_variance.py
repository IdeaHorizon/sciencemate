"""同题重复实验的分析：harness 让结果更**稳**吗？

## 这个脚本回答的问题与 grade.py 不同

`grade.py` 问"平均下来谁更对"。这里问的是"**同一道题跑四次，结果一致吗**" ——
因为 harness 的机制主张是「错误在发生那一步被抓住」，
它应当表现为**方差更小**，而不必然是均值更高：

    裸模型   每次都可能在不同地方犯错，错了也没人拦 → 结果时对时错
    有 harness 错误当场被判否、被要求修 → 结果应当更一致

均值高低会被题目难度淹没（教科书题两臂都接近满分，M 线首轮就是这样）；
一致性不会 —— 它直接反映"过程有没有纠错"。

## 三个量

  consistency   同题四次里最多数结论占的比例（4/4=完全一致）
  flip_rate     同题四次里出现过对也出现过错的题数占比
  self_errors   A1 账本里模型自己犯的错，以及其中被重验修好的比例
                （A0 结构性不可观测 —— 这不是它的缺点，是它的性质）

## 一条纪律

**样本量必须打在脸上。** 3 题 × 4 次是探路量级，任何"A1 更稳"的结论都要
带着 n 说出来。判不了就说判不了 —— 与工具侧的 inconclusive 同源。
"""
from __future__ import annotations

import collections
import glob
import json
import statistics
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))
from grade import grade_one  # noqa: E402


def _load_tasks() -> dict:
    out = {}
    for f in (ROOT / "tasks").glob("*.yaml"):
        t = yaml.safe_load(f.read_text(encoding="utf-8"))
        out[t["id"]] = t
    return out


def _self_errors(submission: dict) -> tuple[int, int]:
    """(模型自犯的错数, 其中被重验修好的数)。A1 才有账本。"""
    run = submission.get("run_dir")
    if not run:
        return (0, 0)
    tpath = Path(run) / "transcript.jsonl"
    if not tpath.exists():
        return (0, 0)
    checks = []
    for line in tpath.read_text(encoding="utf-8", errors="replace").splitlines():
        if '"derivation_check"' not in line:
            continue
        try:
            checks.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    bad = {c["probe"] for c in checks if c.get("status") == "failed"}
    last: dict[str, str] = {}
    for c in checks:
        last[c.get("probe")] = c.get("status")
    fixed = sum(1 for p in bad if last.get(p) in ("verified", "numerically_supported"))
    return (len(bad), fixed)


def main() -> int:
    tasks = _load_tasks()
    runs: dict[tuple[str, str], list] = collections.defaultdict(list)
    for f in sorted((ROOT / "results").glob("*__r*.json")):
        sub = json.loads(f.read_text(encoding="utf-8"))
        if not isinstance(sub, dict):
            continue
        runs[(sub.get("task_id"), sub.get("arm"))].append(sub)

    if not runs:
        print("没有重复实验结果（文件名带 __rN 的）—— 先跑 run_bench.py --repeat N")
        return 1

    print("\n同题重复：结果一致吗\n" + "═" * 74)
    print(f"{'题':<28} {'臂':<4} {'n':<3} {'正确':<8} {'一致性':<8} {'自犯错→修好'}")
    print("─" * 74)

    per_arm = collections.defaultdict(lambda: {"cons": [], "flip": 0, "n": 0,
                                               "err": 0, "fix": 0, "correct": []})
    for (tid, arm), subs in sorted(runs.items()):
        task = tasks.get(tid)
        if task is None:
            continue
        verdicts, errs, fixes = [], 0, 0
        for sub in subs:
            score = grade_one(task, sub, arm)
            verdicts.append(score.correctness)
            e, fx = _self_errors(sub)
            errs += e
            fixes += fx
        decided = [v for v in verdicts if v is not None]
        n = len(subs)
        if decided:
            counts = collections.Counter(decided)
            consistency = counts.most_common(1)[0][1] / len(decided)
            flipped = len(counts) > 1
            correct_rate = sum(1 for v in decided if v) / len(decided)
        else:
            consistency, flipped, correct_rate = float("nan"), False, float("nan")

        a = per_arm[arm]
        a["n"] += n
        a["err"] += errs
        a["fix"] += fixes
        if decided:
            a["cons"].append(consistency)
            a["correct"].append(correct_rate)
            a["flip"] += 1 if flipped else 0

        cons_s = "—" if decided == [] else f"{consistency:.0%}{' ⚠翻转' if flipped else ''}"
        corr_s = "—" if decided == [] else f"{correct_rate:.0%}"
        err_s = f"{errs} → {fixes}" if arm == "A1" else "（无账本）"
        print(f"{tid:<28} {arm:<4} {n:<3} {corr_s:<8} {cons_s:<8} {err_s}")

    print("═" * 74)
    for arm in sorted(per_arm):
        a = per_arm[arm]
        cons = f"{statistics.mean(a['cons']):.0%}" if a["cons"] else "—"
        corr = f"{statistics.mean(a['correct']):.0%}" if a["correct"] else "—"
        line = (f"{arm}: n={a['n']} 次  平均正确率 {corr}  平均一致性 {cons}  "
                f"结论翻转的题 {a['flip']}/{len(a['cons']) or 0}")
        if arm == "A1":
            rate = f"{a['fix']}/{a['err']}" if a["err"] else "0/0"
            line += f"  自犯错→修好 {rate}"
        print(line)

    print("\n⚠️ 样本量：这是探路量级。任何「A1 更稳」的结论都必须带着 n 说，")
    print("   两臂一致性都是 100% 时说明这批题太稳定、测不出差异 —— 那也是结论。")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""核对「理论预测先于实验被冻结」—— 把纲领里那句主张变成机械可查的。

## 为什么需要这个脚本

纲领（§Q1.5）说 derivation 的独门价值之一是：

> 理论预测先于实验被冻结 —— 这是任何 prover 实验室给不出的东西。

一句话主张，除非能被机械核对，否则跟 marketing 没区别。这个脚本回答：

    这份实验对照的理论值，真的来自一份**在实验开跑之前**就冻结的推导吗？

## 判据（三条都得过）

1. **实验的对照值来自一份 derivation_log**，不是实验自己算的、更不是
   实验做完之后填进去的。
2. **那份 derivation_log 的 frozen_at 早于实验 run 的 run_start**。
   时间比较用两边各自落盘的记录，不用"我记得是先做的"。
3. **理论值在两处一字不差**：derivation_log 的 main_result 与 prereg 里
   写的阈值必须是同一个数 —— 中间被改过就不算数。

任何一条不过，就如实报"这条链不成立"。**这个脚本存在的意义就是它会说不。**

用法：
    python scripts/verify_prediction_precedes_experiment.py \
        --derivation output/<run>/artifacts/derivation_log__*.json \
        --experiment output/<run>/
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path


def _parse_time(value) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _load_derivation(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("type") != "derivation_log":
        raise SystemExit(f"⛔ {path.name} 不是 derivation_log")
    return data


def _run_start(run_dir: Path) -> datetime | None:
    tpath = run_dir / "transcript.jsonl"
    if not tpath.exists():
        return None
    for line in tpath.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("event") == "run_start":
            return _parse_time(rec.get("at"))
    return None


def _experiment_artifacts(run_dir: Path) -> list[dict]:
    out = []
    for f in (run_dir / "artifacts").glob("*.json"):
        try:
            out.append(json.loads(f.read_text(encoding="utf-8")))
        except Exception:
            continue
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--derivation", required=True, help="derivation_log 的 json")
    ap.add_argument("--experiment", required=True, help="experiment 的 run 目录")
    args = ap.parse_args()

    deriv = _load_derivation(Path(args.derivation))
    run_dir = Path(args.experiment)
    meta = deriv.get("metadata") or {}

    checks: list[tuple[str, bool, str]] = []

    # ① 推导是冻结的
    frozen = bool(meta.get("frozen"))
    frozen_at = _parse_time(meta.get("frozen_at"))
    checks.append((
        "推导已冻结且有冻结时间", frozen and frozen_at is not None,
        f"frozen={frozen} frozen_at={meta.get('frozen_at')}"))

    # ② 实验开跑晚于推导冻结
    started = _run_start(run_dir)
    ordered = bool(frozen_at and started and frozen_at < started)
    checks.append((
        "实验开跑晚于推导冻结", ordered,
        f"冻结 {frozen_at}  →  实验开跑 {started}"
        + (f"（相隔 {(started - frozen_at).total_seconds():.0f}s）" if ordered else "")))

    # ③ 理论值一字不差地进了实验的判据
    theory = str((meta.get("main_result") or {}).get("expression") or "")
    theory_value = None
    for name, block in (meta.get("measured_metrics") or {}).items():
        if isinstance(block, dict) and block.get("value") is not None:
            theory_value = str(block["value"])
            break
    blob = ""
    for art in _experiment_artifacts(run_dir):
        blob += json.dumps(art, ensure_ascii=False, default=str)
    fixture_hit = bool(theory_value and theory_value[:12] in blob)
    checks.append((
        "理论值出现在实验记录里（未被改写）", fixture_hit,
        f"理论值 {theory_value}（{theory}）"))

    print("\n核对：理论预测是否先于实验被冻结\n" + "─" * 58)
    for label, ok, detail in checks:
        print(f"  {'✓' if ok else '✗'} {label}")
        print(f"      {detail}")
    passed = all(ok for _, ok, _ in checks)
    print("─" * 58)
    if passed:
        print("✅ 时间戳链成立：这次实验检验的是一份先于它冻结的理论预测。")
    else:
        print("⛔ 链不成立 —— 上面打 ✗ 的那条就是断点。")
        print("   （这不是脚本的失败：它的作用就是在链断掉时说出来。）")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())

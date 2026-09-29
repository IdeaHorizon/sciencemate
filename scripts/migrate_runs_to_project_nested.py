"""v0.8 migration：把 flat run 目录搬到项目嵌套布局。

旧（≤ v0.7）：
  ~/.harness-framework/runs/<run_id>/                  # 或 STATE_DIR/<run_id>/
  <repo>/output/<run_id>/                              # 老 STATE_DIR

新（v0.8+）：
  ~/.harness-framework/projects/<project_id>/runs/<run_id>/   # 有 project_id
  ~/.harness-framework/runs_anon/<run_id>/                    # 无

用法：
  # dry-run（看会怎么搬，不真动）
  python scripts/migrate_runs_to_project_nested.py

  # 真搬
  python scripts/migrate_runs_to_project_nested.py --apply

  # 扫额外 legacy 位置
  python scripts/migrate_runs_to_project_nested.py --apply --extra ./output

策略：
  - 每个 run 读 summary.json 拿 project_id（缺则视为 anon）
  - 不能 mv 时（跨设备 / 权限错）报告，不影响其它 run
  - 默认源不动 → 复制 + 验证 + 删源（防止 mv 失败丢数据）；
    --fast 直接 mv（同 fs 上瞬间）
  - in-flight runs（没 summary.json）：默认 skip（用 --include-running 强搬，
    但跑中数据可能损坏）
  - 不处理 cross-project 引用（外部 hard link / 软链）—— 已知限制
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.paths import home, projects_root, runs_anon_root, runs_root


def _read_project_id(run_dir: Path) -> tuple[str | None, str]:
    """返 (project_id, reason)。project_id None 时 reason 解释为啥。"""
    summary = run_dir / "summary.json"
    if not summary.exists():
        return None, "no summary.json (in-flight or crashed)"
    try:
        d = json.loads(summary.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        return None, f"summary parse error: {type(e).__name__}: {e}"
    pid = d.get("project_id")
    if not pid:
        return None, "summary.json has no project_id (anon run)"
    return pid, "ok"


def _planned_dest(run_id: str, project_id: str | None) -> Path:
    if project_id:
        return projects_root() / project_id / "runs" / run_id
    return runs_anon_root() / run_id


def _collect_sources(extras: list[Path]) -> list[Path]:
    """所有要扫的旧 flat 目录。"""
    srcs: list[Path] = []
    legacy = runs_root()
    if legacy.exists():
        srcs.append(legacy)
    state_dir = os.getenv("STATE_DIR")
    if state_dir:
        p = Path(state_dir)
        if p.exists() and p not in srcs:
            srcs.append(p)
    for e in extras:
        if e.exists() and e not in srcs:
            srcs.append(e)
    return srcs


def _move(src: Path, dst: Path, *, fast: bool) -> tuple[bool, str]:
    """搬 src → dst。失败返 (False, error)。"""
    if dst.exists():
        return False, f"dest already exists: {dst}"
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        if fast:
            src.rename(dst)
        else:
            shutil.copytree(src, dst)
            # 验证：dest 至少跟 src 同级别文件数
            if sum(1 for _ in dst.rglob("*")) < sum(1 for _ in src.rglob("*")) - 5:
                shutil.rmtree(dst)
                return False, "verify failed (file count mismatch); aborted"
            shutil.rmtree(src)
    except (OSError, shutil.Error) as e:
        return False, f"{type(e).__name__}: {e}"
    return True, "ok"


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--apply", action="store_true", help="真搬（默认只 dry-run）")
    p.add_argument("--fast", action="store_true",
                    help="同 fs 上用 rename 不 copy（更快但失败丢数据）")
    p.add_argument("--include-running", action="store_true",
                    help="也搬没 summary.json 的（默认 skip，怕跑中数据损坏）")
    p.add_argument("--extra", action="append", default=[], type=Path,
                    help="额外扫的 legacy 目录（可多次：--extra ./output --extra /old）")
    args = p.parse_args()

    print(f"HOME = {home()}")
    sources = _collect_sources(args.extra)
    if not sources:
        print("(没找到任何旧 flat run 目录可搬)")
        return 0
    print(f"\n扫源 ({len(sources)}):")
    for s in sources:
        print(f"  {s}")

    stats = {"by_project": {}, "anon": 0, "skipped_running": 0,
             "skipped_existing": 0, "failed": 0, "moved": 0}
    plan: list[tuple[Path, Path, str]] = []   # (src, dst, project_id or 'anon')
    for src_root in sources:
        for entry in sorted(src_root.iterdir()):
            if not entry.is_dir():
                continue
            run_id = entry.name
            pid, reason = _read_project_id(entry)
            if pid is None and not args.include_running:
                if "in-flight" in reason:
                    stats["skipped_running"] += 1
                    continue
            label = pid or "anon"
            dst = _planned_dest(run_id, pid)
            if dst.exists():
                stats["skipped_existing"] += 1
                continue
            plan.append((entry, dst, label))
            if pid:
                stats["by_project"][pid] = stats["by_project"].get(pid, 0) + 1
            else:
                stats["anon"] += 1

    print(f"\nplanned moves: {len(plan)}")
    for pid, n in sorted(stats["by_project"].items(), key=lambda x: -x[1]):
        print(f"  → projects/{pid}/runs/  ×{n}")
    if stats["anon"]:
        print(f"  → runs_anon/  ×{stats['anon']}")
    print(f"  skipped (still running): {stats['skipped_running']}")
    print(f"  skipped (dest exists):   {stats['skipped_existing']}")

    if not args.apply:
        print("\n(dry-run；加 --apply 才真搬)")
        return 0

    print("\n开搬...")
    for src, dst, label in plan:
        ok, msg = _move(src, dst, fast=args.fast)
        if ok:
            stats["moved"] += 1
        else:
            stats["failed"] += 1
            print(f"  ✗ {src.name} → {label}: {msg}")
    print(f"\n✓ moved {stats['moved']}, ✗ failed {stats['failed']}")
    return 0 if stats["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

"""把旧的 <repo>/output/<run_id>/ 搬到 ~/.harness-framework/runs/<run_id>/。

v3 起 run-local state 统一在 home 下，跟 KB 同根，方便备份/同步/团队共享。

用法：
    python scripts/migrate_output_to_runs.py --dry-run
    python scripts/migrate_output_to_runs.py             # 真搬
    python scripts/migrate_output_to_runs.py --copy      # 复制不删原（保险）

已经在新位置的 run_id 跳过。冲突（两边都有同 id 但内容不同）报错不覆盖。

迁移后：本地 deliverable 类 artifact 自动按 policy 升级到
~/.harness-framework/projects/<id>/deliverables/<dir>/ 不需要在这一步做
（artifact 是 frozen=true 但 _promote_to_deliverable 只在 freeze 时触发；
此脚本 *不* re-trigger，因为我们没在 freeze 之后再 freeze 一次的语义）。
要补 deliverable 升级，跑 scripts/promote_legacy_deliverables.py（如有）。
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.paths import legacy_output_root, runs_root


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--copy", action="store_true",
                     help="复制不删原（保险）；默认是 move")
    args = ap.parse_args()

    src_root = legacy_output_root()
    if src_root is None:
        print("✅ <repo>/output/ 不存在，无需迁移")
        return 0

    dst_root = runs_root()
    dst_root.mkdir(parents=True, exist_ok=True)

    runs = sorted(p for p in src_root.iterdir() if p.is_dir())
    if not runs:
        print(f"✅ {src_root} 是空的，无需迁移")
        return 0

    print(f"🔍 找到 {len(runs)} 个 run 在 {src_root}")
    print(f"→ 目标：{dst_root}{' (dry-run)' if args.dry_run else ''}")

    moved = skipped = conflict = 0
    for src in runs:
        dst = dst_root / src.name
        if dst.exists():
            # 已搬过 / 名字冲突 → 简单 size 比较；不一致报错
            src_size = sum(f.stat().st_size for f in src.rglob("*") if f.is_file())
            dst_size = sum(f.stat().st_size for f in dst.rglob("*") if f.is_file())
            if src_size == dst_size:
                print(f"  ↪ {src.name} 已存在 (size 一致) → 跳过")
                skipped += 1
            else:
                print(f"  ✗ {src.name} 已存在但 size 不同 src={src_size} dst={dst_size} → 跳过（人工处理）")
                conflict += 1
            continue

        if args.dry_run:
            print(f"  + {src.name} → {dst}")
        else:
            if args.copy:
                shutil.copytree(src, dst)
            else:
                shutil.move(str(src), str(dst))
            print(f"  ✓ {src.name}")
        moved += 1

    print()
    print(f"{'dry-run 完成' if args.dry_run else '迁移完成'}：moved={moved} skipped={skipped} conflicts={conflict}")
    if not args.dry_run and not args.copy and moved > 0:
        # 清空原 output/（move 后子目录已空，删空 dir）
        try:
            src_root.rmdir()
            print(f"✓ 已删空 {src_root}")
        except OSError:
            print(f"⚠️ {src_root} 不为空（可能还有非 run 文件），未删")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""迁移：把已经错误落在各项目本地 kb_proposals.jsonl 里的 org-scope target
proposal 挪进共享 org/kb_proposals.jsonl。

背景（2026-07 v10c dogfood 实测发现的根因 bug）：propose() 之前不论
target KB record 的真实 scope，一律把 proposal 写进"恰好触发这次扫描/提议
的项目"的本地文件。find_synthesis_candidates 等自动扫描工具用
state.list_kb() 合并读 project+org 两层 KB，扫到的 org 共享 concept/claim
（可能来自完全不相关的其它项目）就这样被塞进了当前项目的私有队列。
实测 e2e-v10c-lj-flash 一个项目 47 条 proposal 里 42 条（89%）target 的其实
是别的课题的 org 概念。propose()/find_synthesis_candidates 本身的路由已在
shared/tools/library/proposals.py + kb.py 修好（新 propose 按 scope 正确
路由）；这个脚本处理迁移前已经写歪的存量数据。

做什么：
  - 扫 $HARNESS_FRAMEWORK_HOME/projects/*/kb_proposals.jsonl
  - 对每条 proposal，按 target_entity/target_id 判定其 target 的真实 scope：
      - target_entity 不是 KB entity（如 PROFILE.md / PROJECT.md）→ 保留原地
      - target 在本项目自己的 kb_<entity>.jsonl 里 → 保留原地（project-scope，
        或项目内确实自己 shadow 了同 id 的 org 记录，同样保留在本地更安全）
      - target 只在 org 的 kb_<entity>.jsonl 里 → 挪进 org/kb_proposals.jsonl
      - target 哪都找不到（已被 supersede / typo）→ 保留原地，报告为 unresolved
  - 挪动时按 proposal id 去重（同 id 已在 org 文件里 → 跳过，不重复追加）
  - 原文件备份为 kb_proposals.jsonl.pre-org-route.bak

用法：
  python scripts/migrate_proposal_org_scope.py            # dry-run，只报告
  python scripts/migrate_proposal_org_scope.py --apply    # 真写
  python scripts/migrate_proposal_org_scope.py --home ~/.harness-framework
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shared.lib.kb_schema import ENTITIES  # noqa: E402


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _write_jsonl(path: Path, records: list[dict]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    tmp.replace(path)


def _ids_in(path: Path) -> set[str]:
    return {r["id"] for r in _read_jsonl(path) if r.get("id")}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="真写；默认 dry-run 只报告")
    ap.add_argument("--home", default=None, help="HARNESS_FRAMEWORK_HOME（默认 ~/.harness-framework）")
    args = ap.parse_args()

    home = Path(args.home).expanduser() if args.home else Path.home() / ".harness-framework"
    org_dir = home / "org"
    projects_dir = home / "projects"
    if not projects_dir.exists():
        print(f"没找到 {projects_dir}，无事可做。")
        return 0

    # org 侧各 entity 的 id 集合（判定"这是 org record"用）
    org_ids: dict[str, set[str]] = {
        ent: _ids_in(org_dir / f"kb_{ent}.jsonl") for ent in ENTITIES
    }

    org_proposals_path = org_dir / "kb_proposals.jsonl"
    org_proposal_ids_existing = {p["id"] for p in _read_jsonl(org_proposals_path) if p.get("id")}

    total_scanned = 0
    total_moved = 0
    total_kept_project = 0
    total_unresolved = 0
    to_append_to_org: list[dict] = []
    per_project_report: list[tuple[str, int, int]] = []

    project_dirs = sorted(p for p in projects_dir.iterdir() if p.is_dir())
    for pdir in project_dirs:
        prop_path = pdir / "kb_proposals.jsonl"
        if not prop_path.exists():
            continue
        records = _read_jsonl(prop_path)
        if not records:
            continue

        # 本项目自己各 entity 的 id 集合（判定"这条其实是本项目自己的
        # scope=project 记录，或本项目 shadow 了一份同 id 的 org 记录"）
        proj_ids: dict[str, set[str]] = {
            ent: _ids_in(pdir / f"kb_{ent}.jsonl") for ent in ENTITIES
        }

        keep: list[dict] = []
        moved_here = 0
        unresolved_here = 0
        for rec in records:
            total_scanned += 1
            entity = rec.get("target_entity")
            tid = rec.get("target_id")
            if entity not in ENTITIES:
                # PROFILE.md / PROJECT.md 等 —— 天然项目特定，保留原地
                keep.append(rec)
                total_kept_project += 1
                continue
            if tid in proj_ids.get(entity, set()):
                keep.append(rec)
                total_kept_project += 1
                continue
            if tid in org_ids.get(entity, set()):
                # target 只在 org KB —— 挪去共享队列（按 id 去重）
                if rec["id"] not in org_proposal_ids_existing:
                    to_append_to_org.append(rec)
                    org_proposal_ids_existing.add(rec["id"])
                moved_here += 1
                total_moved += 1
                continue
            # 哪都找不到（已 supersede / typo / 数据早于本次迁移前就已损坏引用）
            keep.append(rec)
            unresolved_here += 1
            total_unresolved += 1

        if moved_here:
            per_project_report.append((pdir.name, moved_here, unresolved_here))
            if args.apply:
                backup = prop_path.with_suffix(prop_path.suffix + ".pre-org-route.bak")
                if not backup.exists():
                    shutil.copy2(prop_path, backup)
                _write_jsonl(prop_path, keep)

    if args.apply and to_append_to_org:
        org_dir.mkdir(parents=True, exist_ok=True)
        if org_proposals_path.exists():
            backup = org_proposals_path.with_suffix(org_proposals_path.suffix + ".pre-org-route.bak")
            if not backup.exists():
                shutil.copy2(org_proposals_path, backup)
        with org_proposals_path.open("a", encoding="utf-8") as f:
            for rec in to_append_to_org:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    print(f"扫描 {len(project_dirs)} 个项目，{total_scanned} 条 proposal：")
    print(f"  保留在本项目（scope=project 或非 KB target）: {total_kept_project}")
    print(f"  {'已挪' if args.apply else '待挪'}进 org 共享队列（target 是 org-scope KB record）: {total_moved}")
    print(f"  无法判定 scope（target 已不存在，原样保留）: {total_unresolved}")
    print()
    if per_project_report:
        print("受影响的项目：")
        for name, moved, unresolved in per_project_report:
            extra = f"，{unresolved} 条无法判定" if unresolved else ""
            print(f"  {name}: {moved} 条挪去 org{extra}")
    if not args.apply:
        print()
        print("这是 dry-run。加 --apply 真正写入（会先备份 .pre-org-route.bak）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

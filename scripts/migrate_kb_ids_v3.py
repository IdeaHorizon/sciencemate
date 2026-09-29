"""v3.1 KB id 迁移：8-hex → 12-hex + normalize 语义算符签名变更。

背景（2026-06 审计）：
  1. normalize() 旧版剥掉比较运算符 → '>90%' 与 '<90%' 同 id 静默合并
     （高危#1）。新 normalize 保留语义算符 → 这类 claim 的签名变了。
  2. id 哈希段 8 hex（32 bit）在 ~30k 条时有 ~10% 生日碰撞率 → 加宽 12 hex。

两者都改变 content-addressed id ⇒ 存量 KB（org + 各 project）必须一次性
重算 id 并重写全部引用，否则新旧写入会产生重复记录、dedup 断链。

做什么：
  - 扫 org + 所有 project 的 kb_{concepts,claims,experiments,chunks}.jsonl
  - 按新 compute_kb_id 重算每条 id → old→new 映射
  - 深度遍历所有 jsonl（含 kb_edges.jsonl / kb_proposals.jsonl / memory 等），
    把任何精确等于旧 id 的字符串替换为新 id
  - 原文件备份为 <name>.pre-v3.1.bak；映射写 id_migration_map.json
  - 删除派生的 vector index（.npy 可由 jsonl 全量重建）

注意：旧 normalize 已把对立 claim 合并成一条的，**无法自动拆开**——
报告里会列出 claim_text 含语义算符的记录，请人工复核这些条目。

用法：
  python scripts/migrate_kb_ids_v3.py            # dry-run，只报告
  python scripts/migrate_kb_ids_v3.py --apply    # 真写
  python scripts/migrate_kb_ids_v3.py --home ~/.harness-framework  # 指定根
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shared.lib.kb_schema import ENTITIES, compute_kb_id, _SEMANTIC_OPS  # noqa: E402

_KB_FILES = [f"kb_{e}.jsonl" for e in ENTITIES]
_EXTRA_FILES = ["kb_edges.jsonl", "kb_proposals.jsonl"]
_OPS_RE = re.compile(f"[{re.escape(_SEMANTIC_OPS)}]")


def _read_jsonl(path: Path) -> list[dict]:
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records),
        encoding="utf-8",
    )


def _deep_replace(obj, mapping: dict[str, str]):
    """深度遍历，精确匹配旧 id 的字符串替换为新 id。"""
    if isinstance(obj, str):
        return mapping.get(obj, obj)
    if isinstance(obj, list):
        return [_deep_replace(x, mapping) for x in obj]
    if isinstance(obj, dict):
        return {k: _deep_replace(v, mapping) for k, v in obj.items()}
    return obj


def _scope_dirs(home: Path) -> list[Path]:
    dirs = []
    org = home / "org"
    if org.is_dir():
        dirs.append(org)
    projects = home / "projects"
    if projects.is_dir():
        dirs.extend(sorted(p for p in projects.iterdir() if p.is_dir()))
    return dirs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真写（默认 dry-run）")
    ap.add_argument("--home", default=str(Path.home() / ".harness-framework"))
    args = ap.parse_args()

    home = Path(args.home).expanduser()
    if not home.is_dir():
        print(f"home 不存在：{home}（无 KB 可迁移，直接退出）")
        return 0

    dirs = _scope_dirs(home)
    if not dirs:
        print("没有 org / project scope 目录，无事可做。")
        return 0

    # ── Pass 1：全局 old→new 映射（跨 scope 引用要求全局图）────────────────
    mapping: dict[str, str] = {}
    review_needed: list[tuple[str, str, str]] = []   # (scope, new_id, claim_text)
    collisions: list[tuple[str, str, str]] = []
    for d in dirs:
        for fname in _KB_FILES:
            path = d / fname
            if not path.exists():
                continue
            entity = fname[len("kb_"):-len(".jsonl")]
            for rec in _read_jsonl(path):
                old_id = rec.get("id")
                if not old_id:
                    continue
                new_id = compute_kb_id(entity, rec)
                if old_id != new_id:
                    if old_id in mapping and mapping[old_id] != new_id:
                        collisions.append((str(d), old_id, new_id))
                        continue
                    mapping[old_id] = new_id
                if entity == "claims" and _OPS_RE.search(rec.get("claim_text") or ""):
                    review_needed.append(
                        (d.name, new_id, (rec.get("claim_text") or "")[:100]))

    print(f"扫描 {len(dirs)} 个 scope 目录；需迁移 id：{len(mapping)} 条")
    if collisions:
        print(f"⚠️ 同一旧 id 映射到多个新 id（不应发生）：{len(collisions)} 条")
        for c in collisions[:5]:
            print("   ", c)
    if review_needed:
        print(f"⚠️ {len(review_needed)} 条 claim 含语义算符 —— 旧 normalize 可能已把"
              f"对立命题合并进这些记录，请人工复核（列前 10）：")
        for scope, nid, text in review_needed[:10]:
            print(f"   [{scope}] {nid}  {text}")

    if not mapping:
        print("所有 id 已是新格式，无需迁移。")
        return 0

    if not args.apply:
        print("\n(dry-run 结束 —— 加 --apply 真写)")
        return 0

    # ── Pass 2：重写全部文件（id 字段 + 深度引用替换）────────────────────
    n_files = 0
    for d in dirs:
        for fname in _KB_FILES + _EXTRA_FILES + ["memory.jsonl"]:
            path = d / fname
            if not path.exists():
                continue
            records = _read_jsonl(path)
            new_records = [_deep_replace(r, mapping) for r in records]
            if new_records != records:
                shutil.copy2(path, path.with_suffix(path.suffix + ".pre-v3.1.bak"))
                _write_jsonl(path, new_records)
                n_files += 1
        # 派生 vector index：直接删，下次使用自动 rebuild（jsonl 是 source of truth）
        for idx in d.glob("kb_index_*"):
            idx.unlink()
        vec_dir = d / "vector_index"
        if vec_dir.is_dir():
            shutil.rmtree(vec_dir)
        # 映射存档（citation 兼容 / 审计溯源）
        (d / "id_migration_map.json").write_text(
            json.dumps(mapping, ensure_ascii=False, indent=1), encoding="utf-8")

    print(f"✓ 重写 {n_files} 个文件；映射存 id_migration_map.json；"
          f"vector index 已清（会自动重建）")
    print("提醒：跑一遍 `hf kb stats` 验证计数一致；旧 transcript/artifact 里的"
          "历史 id 不改（终态记录），citation 检查只对新写内容生效。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

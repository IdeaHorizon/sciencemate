"""v3.2 KB 修复脚本（2026-07 KB 审计 Bug#1 + Bug#2 的存量数据修复）。

配套的**防复发**改动在 core/state.py（person/group 禁语义自动合并）和
shared/lib/kb_schema.py（is_external_uri 拒内网/伪造 host）。本脚本修**已经
发生**的污染。

修两类：
  1. person/group concept 被 embedding 语义合并误并（同论文不同作者被当 aliases）。
     用 derived.semantic_merge_log 的 from_record_signature 找回被并的名字；
     **姓氏 token 启发式**区分：
       - 与 canonical 共享姓氏 token（G. Bussi ↔ Giovanni Bussi）→ 判为真名变体，保留 alias
       - 无共享 token（Whitmore ↔ Ramezani）→ 判为不同实体，拆成独立 concept
  2. 内部 artifact chunk 被伪造 https://*.internal host 泄漏进 org scope →
     迁回来源 project（run→project 可查时）或标记待人工处理。

用法：
  python scripts/repair_kb_v32.py            # dry-run 报告
  python scripts/repair_kb_v32.py --apply    # 真写（自动 .pre-v32.bak 备份）
"""
from __future__ import annotations

import argparse
import glob
import json
import re
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from shared.lib.kb_schema import is_external_uri, compute_kb_id, now_iso  # noqa: E402

_HOME = Path.home() / ".harness-framework"
_ORG = _HOME / "org"
_STOPWORDS = {"de", "van", "von", "der", "la", "el", "jr", "iii", "ii", "dr"}


def _name_tokens(name: str) -> set[str]:
    """姓名里长度 ≥ 3 的字母 token（去首字母缩写 'M.'、停用词）。"""
    toks = re.findall(r"[a-zA-ZÀ-ÿ]+", (name or "").lower())
    return {t for t in toks if len(t) >= 3 and t not in _STOPWORDS}


def _same_person(a: str, b: str) -> bool:
    """两名字是否可能同一人：共享 ≥ 1 个长 token（多为姓氏）。"""
    ta, tb = _name_tokens(a), _name_tokens(b)
    return bool(ta & tb)


def _read(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out


def _write(path: Path, records: list[dict]) -> None:
    path.write_text(
        "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records),
        encoding="utf-8",
    )


def _backup(path: Path) -> None:
    if path.exists():
        shutil.copy2(path, path.with_suffix(path.suffix + ".pre-v32.bak"))


def repair_persons(apply: bool) -> None:
    path = _ORG / "kb_concepts.jsonl"
    records = _read(path)
    to_add: list[dict] = []
    n_fixed = 0
    n_split = 0
    for r in records:
        if r.get("concept_type") not in ("person", "group"):
            continue
        canonical = r.get("canonical_name", "")
        log = (r.get("derived") or {}).get("semantic_merge_log") or []
        merged_names = {e.get("from_record_signature") for e in log
                        if e.get("from_record_signature")}
        merged_names.discard(canonical)
        if not merged_names:
            continue

        keep_aliases: list[str] = []
        split_out: list[str] = []
        for nm in sorted(merged_names):
            if _same_person(canonical, nm):
                keep_aliases.append(nm)     # 真名变体，留 alias
            else:
                split_out.append(nm)        # 不同实体，拆出去

        if not split_out:
            continue   # 全是名变体，不动
        n_fixed += 1

        # 现有 aliases 里也把 split_out 的名字剔掉
        old_aliases = r.get("aliases") or []
        r["aliases"] = [a for a in old_aliases if a not in split_out]
        for ka in keep_aliases:
            if ka not in r["aliases"]:
                r["aliases"].append(ka)
        # 清掉被拆名字的 merge log（保留名变体的）
        new_log = [e for e in log
                   if e.get("from_record_signature") not in split_out]
        r.setdefault("derived", {})["semantic_merge_log"] = new_log
        r.setdefault("derived", {})["v32_repair"] = {
            "at": now_iso(), "split_out": split_out, "kept_as_alias": keep_aliases,
        }
        r["updated_at"] = now_iso()

        # 为每个拆出的独立实体建最小 concept（原描述已丢，标注恢复来源）
        for nm in split_out:
            new_rec = {
                "canonical_name": nm,
                "concept_type": r.get("concept_type"),
                "description": (f"（v3.2 修复恢复：此实体曾被 embedding 语义合并"
                                f"误并入 concept '{canonical}'；原描述已丢失，待"
                                f"literature 重新 enrich。）"),
                "aliases": [], "attributes": {},
                "created_by_role": "agent_auto",
                "created_by_node_type": "kb_repair_v32",
                "scope": "org",
            }
            new_rec["id"] = compute_kb_id("concepts", new_rec)
            n_split += 1
            to_add.append(new_rec)
        print(f"  拆分 [{canonical}]: 独立实体={split_out}  留名变体={keep_aliases}")

    print(f"\nperson/group 修复: {n_fixed} 个 concept 被清理, 拆出 {n_split} 个独立实体")
    if apply and (n_fixed or n_split):
        _backup(path)
        # 去重后追加（可能与已有 id 撞，撞则跳过）
        existing_ids = {r.get("id") for r in records}
        for nr in to_add:
            if nr["id"] not in existing_ids:
                records.append(nr)
                existing_ids.add(nr["id"])
        _write(path, records)
        print("  ✓ 已写回 org/kb_concepts.jsonl（备份 .pre-v32.bak）")


def _run_project_map() -> dict[str, str]:
    """run_id → project_id（从 output/*/summary.json 建表）。"""
    m: dict[str, str] = {}
    for s in glob.glob("output/*/summary.json"):
        try:
            d = json.load(open(s))
            if d.get("run_id") and d.get("project_id"):
                m[d["run_id"]] = d["project_id"]
        except Exception:
            pass
    return m


def repair_leaked_chunks(apply: bool) -> None:
    path = _ORG / "kb_chunks.jsonl"
    records = _read(path)
    run_proj = _run_project_map()
    keep: list[dict] = []
    moved: dict[str, list[dict]] = {}     # project_id → [chunk]
    orphan = 0
    for r in records:
        src = (r.get("source") or "")
        leaked = (src.lower().startswith(("http://", "https://"))
                  and not is_external_uri(src))
        if not leaked:
            keep.append(r)
            continue
        proj = run_proj.get(r.get("created_by_run_id"))
        if proj:
            r["scope"] = "project"
            moved.setdefault(proj, []).append(r)
        else:
            orphan += 1     # 查不到来源 project → 从 org 删（内部数据不该留 org）

    total_leaked = len(records) - len(keep)
    print(f"\n泄漏 chunk 修复: {total_leaked} 条, 可迁回 project {sum(len(v) for v in moved.values())} 条, "
          f"来源不明(删) {orphan} 条")
    for proj, chunks in moved.items():
        print(f"  → project {proj}: {len(chunks)} 条")
    if apply and total_leaked:
        _backup(path)
        _write(path, keep)     # org 只留真外部 chunk
        for proj, chunks in moved.items():
            ppath = _HOME / "projects" / proj / "kb_chunks.jsonl"
            ppath.parent.mkdir(parents=True, exist_ok=True)
            _backup(ppath)
            existing = _read(ppath)
            existing_ids = {c.get("id") for c in existing}
            for c in chunks:
                if c["id"] not in existing_ids:
                    existing.append(c)
            _write(ppath, existing)
        print("  ✓ 已迁移（org + 各 project kb_chunks.jsonl 均备份）")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    print("=== v3.2 KB 存量修复 " + ("(APPLY)" if args.apply else "(DRY-RUN)") + " ===")
    repair_persons(args.apply)
    repair_leaked_chunks(args.apply)
    if not args.apply:
        print("\n(dry-run —— 加 --apply 真写)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

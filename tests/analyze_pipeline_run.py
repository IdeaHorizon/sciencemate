"""Post-run 分析：从 transcript 提取 3 个焦点的证据。

  1. **节点间路由合理性**：orchestrator 调度顺序、是否漏调、是否多余调
  2. **KB agent 更新质量**：每次 curator run 写了什么 / 数量 / 类型
  3. **KB agent 调起时机**：每个 producing 节点之后是否紧接 curator？延迟多少 turn？

用法：
  python tests/analyze_pipeline_run.py output/orchestrator__lammps_smoke_<ts>
"""
from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def load_transcript(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def analyze_orchestrator_routing(events: list[dict]) -> dict:
    """从 orchestrator transcript 抽出"调起了哪些子节点 + 顺序"。"""
    subagent_calls = []
    for ev in events:
        if ev.get("event") == "subagent_call_start":
            subagent_calls.append({
                "child_type": ev.get("child_node_type"),
                "depth": ev.get("child_depth"),
                "forwarded": ev.get("forwarded_artifacts", []),
                "inputs_preview": ev.get("node_inputs_preview", "")[:200],
            })
        elif ev.get("event") == "subagent_call_end":
            if subagent_calls and "child_status" not in subagent_calls[-1]:
                subagent_calls[-1].update({
                    "child_run_id": ev.get("child_run_id"),
                    "child_status": ev.get("child_status"),
                    "child_turns": ev.get("child_turns"),
                    "imported": ev.get("imported_artifacts", []),
                })
    return {
        "n_subagent_calls": len(subagent_calls),
        "sequence": [c["child_type"] for c in subagent_calls],
        "calls": subagent_calls,
    }


def analyze_curator_timing(routing: dict) -> dict:
    """检查 curator 在 producing 节点之后多久被调起。"""
    PRODUCING = {"literature", "hypothesis", "experiment", "analysis",
                 "writing", "data", "postprocess"}

    calls = routing["calls"]
    timing: list[dict] = []
    pending_producing: dict | None = None

    for i, c in enumerate(calls):
        ct = c["child_type"]
        if ct in PRODUCING:
            if pending_producing is not None:
                # 上一个 producing 还没等到 curator 就跑了下一个 producing
                timing.append({
                    "producing": pending_producing["child_type"],
                    "next_call": ct,
                    "curator_gap": "❌ skipped - 下个 producing 抢先",
                    "delta_calls": i - pending_producing["idx"],
                })
            pending_producing = {**c, "idx": i}
        elif ct == "_curator":
            if pending_producing is not None:
                delta = i - pending_producing["idx"]
                timing.append({
                    "producing": pending_producing["child_type"],
                    "producing_status": pending_producing.get("child_status"),
                    "curator_idx": i,
                    "delta_calls": delta,
                    "curator_status": c.get("child_status"),
                    "curator_turns": c.get("child_turns"),
                    "timing": ("✅ immediate" if delta == 1
                                  else f"⚠️ delayed by {delta - 1} calls"),
                })
                pending_producing = None
            else:
                timing.append({
                    "producing": None,
                    "curator_idx": i,
                    "timing": "ℹ️ curator without preceding producing（可能 dreaming）",
                })
    if pending_producing is not None:
        timing.append({
            "producing": pending_producing["child_type"],
            "next_call": None,
            "curator_gap": "❌ never_called",
        })
    return {"events": timing}


_KB_WRITE_TOOLS = {
    "create_concept", "create_claim", "create_synthesis", "create_hypothesis",
    "create_question", "create_opportunity", "create_experiment",
    "create_decision", "create_failure",
    "update_claim_status", "update_hypothesis_verdict",
    "update_question_status", "update_opportunity_status",
    "kb_register_artifact_as_chunk", "kb_ingest",
}

_MEMORY_WRITE_TOOLS = {
    "add_memory", "archive_memory", "promote_memory", "supersede_memory",
}


def analyze_curator_kb_writes(child_dir: Path) -> dict:
    """从 curator sub-run 的 transcript 抽出它写了什么 KB。"""
    events = load_transcript(child_dir / "transcript.jsonl")
    writes: dict[str, int] = Counter()
    write_details: list[dict] = []
    proposals: list[dict] = []
    KB_WRITE_TOOLS = _KB_WRITE_TOOLS

    for ev in events:
        if ev.get("event") != "tool_call":
            continue
        name = ev.get("name")
        if name in KB_WRITE_TOOLS:
            writes[name] += 1
            args = ev.get("args") or {}
            preview = {k: v for k, v in args.items()
                       if k in ("canonical_name", "claim_text", "hypothesis_text",
                                  "question_text", "opportunity_text", "title",
                                  "new_status", "verdict", "summary")}
            write_details.append({"tool": name, "preview": preview})
        elif name == "propose":
            args = ev.get("args") or {}
            proposals.append({
                "proposal_type": args.get("proposal_type"),
                "reasoning_preview": (args.get("reasoning") or "")[:120],
            })

    return {
        "n_total_writes": sum(writes.values()),
        "by_tool": dict(writes),
        "details": write_details,
        "proposals": proposals,
    }


def analyze_subrun_kb_and_memory(child_dir: Path) -> dict:
    """分析一个 sub-run（非 curator）的 KB + memory 写入。"""
    events = load_transcript(child_dir / "transcript.jsonl")
    kb_writes: dict[str, int] = Counter()
    mem_by_kind: dict[str, int] = Counter()
    mem_details: list[dict] = []
    proposals: list[dict] = []
    node_type = None
    for ev in events:
        if ev.get("event") == "run_start":
            node_type = ev.get("node_type")
        if ev.get("event") != "tool_call":
            continue
        name = ev.get("name")
        args = ev.get("args") or {}
        if name in _KB_WRITE_TOOLS:
            kb_writes[name] += 1
        elif name in _MEMORY_WRITE_TOOLS:
            kind = args.get("kind", "—")
            mem_by_kind[f"{name}/{kind}"] += 1
            txt = args.get("text", "")
            if name == "add_memory":
                mem_details.append({
                    "kind": kind,
                    "tags": args.get("tags") or [],
                    "applies_to_node": args.get("applies_to_node"),
                    "text_preview": txt[:120],
                })
        elif name == "propose":
            proposals.append({
                "proposal_type": args.get("proposal_type"),
                "reasoning_preview": (args.get("reasoning") or "")[:120],
            })
    return {
        "node_type": node_type,
        "kb_writes": dict(kb_writes),
        "mem_by_kind": dict(mem_by_kind),
        "mem_details": mem_details,
        "proposals": proposals,
    }


def analyze_input_dependency(routing: dict, base_dir: Path) -> dict:
    """检查每个 sub-run 启动时是否拿到了 required_input_artifact_types。"""
    from core.loader import load_harness
    issues = []
    for c in routing["calls"]:
        nt = c.get("child_type")
        if not nt:
            continue
        try:
            h = load_harness(nt)
        except Exception:
            continue
        req = h.required_input_artifact_types
        if not req:
            continue
        fwd_types = []
        for f in (c.get("forwarded") or []):
            if ":" in f:
                fwd_types.append(f.split(":", 1)[0])
        # blocked_missing_inputs 表示框架检测到缺
        if c.get("child_status") == "blocked_missing_inputs":
            issues.append({"node": nt, "required": req, "issue": "blocked_missing_inputs"})
        # forwarded 不包含 required 不一定有问题（可能 child 状态目录里已经有了）
        missing_in_fwd = [r for r in req if r not in fwd_types]
        if missing_in_fwd:
            issues.append({
                "node": nt, "required": req, "forwarded_types": fwd_types,
                "issue": f"required {missing_in_fwd} not in forwarded_artifact_ids（可能依赖 KB / state 残留）",
            })
    return {"issues": issues}


def analyze_run(run_dir: Path) -> None:
    print(f"\n{'═' * 75}")
    print(f"📂 RUN: {run_dir.name}")
    print("═" * 75)

    orch_transcript = run_dir / "transcript.jsonl"
    if not orch_transcript.exists():
        print(f"❌ 没找到 transcript.jsonl in {run_dir}")
        return

    events = load_transcript(orch_transcript)
    routing = analyze_orchestrator_routing(events)
    timing = analyze_curator_timing(routing)

    # ── 1. 节点路由 ─────────────────────────────────────────────────
    print("\n## 1. 节点路由")
    print(f"\norchestrator 共调起 {routing['n_subagent_calls']} 个子节点：")
    print("    " + " → ".join(routing["sequence"]))

    print("\n详细：")
    for i, c in enumerate(routing["calls"], 1):
        s = c.get("child_status", "?")
        emoji = {"completed": "✅", "incomplete": "⚠️", "paused": "⏸",
                  "blocked_missing_inputs": "🚫"}.get(s, "❓")
        ct = c.get("child_turns", "?")
        fwd = len(c.get("forwarded", []))
        imp = len(c.get("imported", []))
        print(f"   {i:2}. {emoji} {c['child_type']:<14} status={s:<12} "
              f"turns={ct} fwd={fwd} imported={imp}")

    # ── 2. KB curator 调起时机 ──────────────────────────────────
    print("\n## 2. curator 调起时机")
    for e in timing["events"]:
        if "curator_gap" in e:
            print(f"   ❌ producing={e['producing']} → "
                   f"{e['curator_gap']}（之后 next_call={e.get('next_call')}）")
        elif e.get("producing"):
            print(f"   {e['timing']:<25} {e['producing']:<14} → curator "
                   f"(curator_status={e.get('curator_status')}, "
                   f"turns={e.get('curator_turns')})")
        else:
            print(f"   {e['timing']:<25}")

    # ── 3. KB 写入质量（per-curator detail） ───────────────────────
    print("\n## 3. curator 每次 run 的 KB 写入")
    curator_runs = [c for c in routing["calls"] if c["child_type"] == "_curator"]
    for i, cur in enumerate(curator_runs, 1):
        child_run_id = cur.get("child_run_id")
        if not child_run_id:
            continue
        child_dir = run_dir.parent / child_run_id
        if not child_dir.exists():
            print(f"\n   curator #{i}：子目录 {child_dir} 不存在")
            continue
        kb = analyze_curator_kb_writes(child_dir)
        print(f"\n   ┌─ curator #{i} ({cur.get('child_status')}, "
               f"{cur.get('child_turns')} turns) ─────────")
        print(f"   │ 总写入: {kb['n_total_writes']} 次 by tool: {kb['by_tool']}")
        if kb["proposals"]:
            print(f"   │ propose: {len(kb['proposals'])} 条")
            for p in kb["proposals"][:3]:
                print(f"   │   - {p['proposal_type']}: {p['reasoning_preview']}")
        if kb["details"][:3]:
            print(f"   │ 写入示例:")
            for d in kb["details"][:5]:
                p = d["preview"]
                tn = d["tool"]
                summary = next((str(v)[:90] for k, v in p.items()
                                if v and isinstance(v, str)), "")
                print(f"   │   [{tn}] {summary}")

    # ── 4. 各 sub-run 的 memory 写入 + 直接 KB 写入 ─────────────────
    # 注：节点直接调 create_* 不一定是越权 —— hypothesis / analysis 都有合法白名单。
    # 这里只观察"哪些节点在 curator 之外也写了 KB"，是设计 review 信号，不是 bug。
    print("\n## 4. 各 sub-run 的 memory + 直接 KB 写入分布")
    direct_kb_by_node: dict[str, dict] = {}
    for c in routing["calls"]:
        nt = c.get("child_type")
        if nt == "_curator":     # curator 单独看过了
            continue
        child_run_id = c.get("child_run_id")
        if not child_run_id:
            continue
        child_dir = run_dir.parent / child_run_id
        if not child_dir.exists():
            continue
        a = analyze_subrun_kb_and_memory(child_dir)
        if not a["mem_by_kind"] and not a["kb_writes"] and not a["proposals"]:
            print(f"   {nt:<14}: (无 memory / KB 写入)")
            continue
        line = f"   {nt:<14}"
        if a["mem_by_kind"]:
            mems = ", ".join(f"{k}={v}" for k, v in a["mem_by_kind"].items())
            line += f"  mem[{mems}]"
        if a["kb_writes"]:
            kws = ", ".join(f"{k}={v}" for k, v in a["kb_writes"].items())
            line += f"  kb_writes[{kws}]"
            direct_kb_by_node[nt] = a["kb_writes"]
        if a["proposals"]:
            line += f"  proposals={len(a['proposals'])}"
        print(line)

    if direct_kb_by_node:
        print(f"\n   ℹ️ 非 curator 节点也直接写 KB（不一定是问题，看设计意图）:")
        for nt, kws in direct_kb_by_node.items():
            print(f"     {nt}: {kws}")
        print("     —— 设计 review：这些是该节点 yaml.tools 白名单允许的吗？")
        print("        如果是有意 → OK；如果是 LLM 越权 → 收紧 tools 白名单。")

    # ── 5. memory 内容采样（producing 节点写的 observation 是不是真有信息量）─
    print("\n## 5. memory 内容采样（每个 producing 节点最多 3 条）")
    for c in routing["calls"]:
        nt = c.get("child_type")
        if nt == "_curator":
            continue
        child_run_id = c.get("child_run_id")
        if not child_run_id:
            continue
        child_dir = run_dir.parent / child_run_id
        if not child_dir.exists():
            continue
        a = analyze_subrun_kb_and_memory(child_dir)
        if not a["mem_details"]:
            continue
        print(f"\n   ── {nt} ({len(a['mem_details'])} memories):")
        for m in a["mem_details"][:3]:
            scope = f"→{m['applies_to_node']}" if m["applies_to_node"] else ""
            tags = ",".join(m["tags"]) if m["tags"] else "-"
            print(f"     [{m['kind']:<11}{scope}] tags={tags}: {m['text_preview']}")

    # ── 6. 节点 input 依赖检查 ────────────────────────────────────
    print("\n## 6. 节点 required_input 依赖检查")
    dep = analyze_input_dependency(routing, run_dir.parent)
    if not dep["issues"]:
        print("   ✅ 所有节点启动时 required_input_artifact_types 都满足")
    else:
        for issue in dep["issues"]:
            print(f"   ⚠️ {issue['node']}: {issue['issue']}")
            if "forwarded_types" in issue:
                print(f"      required={issue['required']} forwarded={issue['forwarded_types']}")

    print()


def main():
    if len(sys.argv) < 2:
        # 自动找最近的 orchestrator__lammps_smoke_*
        candidates = sorted(Path("output").glob("orchestrator__lammps_smoke_*"))
        if not candidates:
            print("Usage: python tests/analyze_pipeline_run.py <run_dir>")
            return 1
        run_dir = candidates[-1]
        print(f"(自动选了最新的 run: {run_dir})")
    else:
        run_dir = Path(sys.argv[1])
    analyze_run(run_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())

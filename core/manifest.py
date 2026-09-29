"""自动生成 PROJECT_MANIFEST.md / ORG_MANIFEST.md。

ARA-inspired：把 KB 的关键状态压成 ~500 token 的 narrative summary，让 user / agent
进入 / 跨项目时不用 read 整库 jsonl 就知道项目长啥样。

调用：
    from core.manifest import write_project_manifest, write_org_manifest
    write_project_manifest(project_id='lammps_smoke_xxx')
    write_org_manifest()

或 CLI（见 scripts/regenerate_manifests.py）。
"""
from __future__ import annotations

import json
import os
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path


def _home() -> Path:
    from core.paths import home  # 「根在哪」一处回答（含 Windows 分支）

    return home()


def projects_root() -> Path:
    from core.paths import projects_root as told  # 项目层在哪 —— 一处回答（平台会告诉它）

    return told()


def _org_home() -> Path:
    return Path(os.getenv("HARNESS_FRAMEWORK_ORG_HOME", str(_home() / "org")))


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return out


def _scope_dir_records(scope_dir: Path) -> dict[str, list[dict]]:
    return {
        e: _read_jsonl(scope_dir / f"kb_{e}.jsonl")
        for e in ("concepts", "claims", "experiments", "chunks")
    }


# ─────────────────────────────────────────────────────────────────────────────
# Render project manifest
# ─────────────────────────────────────────────────────────────────────────────

def render_project_manifest(project_id: str) -> str:
    proj_dir = projects_root() / project_id
    org_dir = _org_home()
    if not proj_dir.exists():
        return f"# PROJECT_MANIFEST: {project_id}\n\n(project dir not found at {proj_dir})\n"

    proj = _scope_dir_records(proj_dir)
    org = _scope_dir_records(org_dir)

    # Aggregate stats
    n_concepts = len(org["concepts"])
    n_claims_proj = len(proj["claims"])
    n_claims_org = len(org["claims"])
    n_experiments = len(proj["experiments"])
    n_chunks_proj = len(proj["chunks"])
    n_chunks_org = len(org["chunks"])

    # claim_type breakdown (project)
    claim_type_counts = Counter(c.get("claim_type", "unknown") for c in proj["claims"])
    # status breakdown
    status_counts = Counter(c.get("status", "unknown") for c in proj["claims"])

    # Top concepts referenced from project claims
    concept_refs: Counter = Counter()
    for c in proj["claims"]:
        for cid in (c.get("concept_ids") or []):
            concept_refs[cid] += 1
    org_concepts_by_id = {c["id"]: c for c in org["concepts"]}

    top_concepts = []
    for cid, count in concept_refs.most_common(8):
        rec = org_concepts_by_id.get(cid)
        if rec:
            top_concepts.append({
                "name": rec.get("canonical_name", cid),
                "type": rec.get("concept_type", "?"),
                "count": count,
            })

    # Hypotheses (project claims with claim_type='hypothesis')
    hypotheses = [c for c in proj["claims"] if c.get("claim_type") == "hypothesis"]
    h_by_status: dict = defaultdict(list)
    for h in hypotheses:
        h_by_status[h.get("status", "open")].append(h)

    # Dead-ends learned (these auto-default to org scope!)
    dead_ends_org = [c for c in org["claims"] if c.get("claim_type") == "dead_end"]
    project_dead_ends = [
        c for c in dead_ends_org
        if (c.get("created_by_run_id") or "").startswith(project_id)
        or (c.get("created_by_node_type") and project_id in str(c.get("created_by_run_id", "")))
    ]

    # 开放问题：来自 org 的 open_question 条目（矛盾/缺口机械生成）。
    # 原来读 `claim_type='conjecture' and status='open'` —— conjecture 类型
    # 已随收敛删除（提案期的思考不进 KB）。开放问题现在是 org 的一等条目，
    # 由 dreaming 的矛盾呈现作业产出，那才是"实验室现在最想知道什么"。
    open_questions = [c for c in proj["claims"]
                      if c.get("org_kind") == "open_question"
                      and c.get("status") == "open"]

    # High-confidence validated claims (the project's takeaways)
    validated = sorted(
        [c for c in proj["claims"] if c.get("status") == "validated"
         and float(c.get("confidence", 0)) >= 0.8],
        key=lambda c: -float(c.get("confidence", 0)),
    )[:5]

    now = datetime.now(timezone.utc).isoformat()

    # Render
    lines = []
    lines.append(f"# PROJECT_MANIFEST · {project_id}")
    lines.append("")
    lines.append(f"_Generated: {now}_")
    lines.append("")
    lines.append("## At a glance")
    lines.append("")
    lines.append(f"- **Concepts (org-shared)**: {n_concepts}")
    lines.append(f"- **Claims (this project)**: {n_claims_proj} "
                 f"({dict(claim_type_counts.most_common())})")
    lines.append(f"- **Claims (org-shared, may be touched by this project)**: {n_claims_org}")
    lines.append(f"- **Experiments**: {n_experiments}")
    lines.append(f"- **Chunks**: {n_chunks_proj} project · {n_chunks_org} org")
    lines.append(f"- **Status breakdown**: {dict(status_counts.most_common())}")
    lines.append("")

    if top_concepts:
        lines.append("## Most-referenced concepts")
        lines.append("")
        for c in top_concepts:
            lines.append(f"- **{c['name']}** ({c['type']}) — referenced by {c['count']} claims")
        lines.append("")

    if hypotheses:
        lines.append("## Hypotheses under test / resolved")
        lines.append("")
        for status, hs in h_by_status.items():
            lines.append(f"- **{status}** ({len(hs)})")
            for h in hs[:3]:
                lines.append(f"  - {(h.get('claim_text') or '')[:120]}")
        lines.append("")

    if validated:
        lines.append("## Top validated findings")
        lines.append("")
        for v in validated:
            conf = v.get("confidence", 0)
            lines.append(f"- ({conf:.2f}) {(v.get('claim_text') or '')[:160]}")
        lines.append("")

    if open_questions:
        lines.append("## Open questions")
        lines.append("")
        for q in open_questions[:5]:
            lines.append(f"- {(q.get('claim_text') or '')[:160]}")
        lines.append("")

    if project_dead_ends:
        lines.append("## Dead-ends (don't repeat)")
        lines.append("")
        for d in project_dead_ends[:5]:
            lines.append(f"- {(d.get('claim_text') or '')[:160]}")
            r = d.get("dont_repeat_reason", "")
            if r:
                lines.append(f"  - why: {r[:200]}")
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("_This manifest is auto-generated from KB v3. Edit the underlying claims/concepts; "
                 "rerun `regenerate_manifests.py` or call `core.manifest.write_project_manifest()`._")

    return "\n".join(lines)


def write_project_manifest(project_id: str) -> Path:
    proj_dir = projects_root() / project_id
    proj_dir.mkdir(parents=True, exist_ok=True)
    content = render_project_manifest(project_id)
    path = proj_dir / "PROJECT_MANIFEST.md"
    path.write_text(content, encoding="utf-8")
    return path


# ─────────────────────────────────────────────────────────────────────────────
# Render org manifest (cross-project)
# ─────────────────────────────────────────────────────────────────────────────

def render_org_manifest() -> str:
    org_dir = _org_home()
    proj_root = projects_root()

    org = _scope_dir_records(org_dir)

    project_dirs = [p for p in (proj_root.iterdir() if proj_root.exists() else [])
                     if p.is_dir() and any(p.glob("kb_*.jsonl"))]
    n_projects = len(project_dirs)

    # Per-project claim counts
    proj_summaries = []
    for p in project_dirs:
        proj_claims = _read_jsonl(p / "kb_claims.jsonl")
        validated = sum(1 for c in proj_claims if c.get("status") == "validated")
        proj_summaries.append({
            "id": p.name,
            "claims": len(proj_claims),
            "validated": validated,
        })

    # Concept type breakdown
    ct_counts = Counter(c.get("concept_type") for c in org["concepts"])
    persons = [c for c in org["concepts"] if c.get("concept_type") == "person"]
    groups = [c for c in org["concepts"] if c.get("concept_type") == "group"]

    # Org-shared claims (cross-project knowledge)
    org_claim_types = Counter(c.get("claim_type") for c in org["claims"])
    org_dead_ends = [c for c in org["claims"] if c.get("claim_type") == "dead_end"]
    org_validated_methodological = [
        c for c in org["claims"]
        if c.get("claim_type") == "methodological" and c.get("status") == "validated"
    ]

    now = datetime.now(timezone.utc).isoformat()

    lines = []
    lines.append("# ORG_MANIFEST · cross-project knowledge")
    lines.append("")
    lines.append(f"_Generated: {now}_")
    lines.append("")
    lines.append("## At a glance")
    lines.append("")
    lines.append(f"- **Projects on file**: {n_projects}")
    lines.append(f"- **Concepts**: {len(org['concepts'])} {dict(ct_counts.most_common())}")
    lines.append(f"- **Org-shared claims**: {len(org['claims'])} {dict(org_claim_types.most_common())}")
    lines.append(f"- **Researchers tracked**: {len(persons)} persons · {len(groups)} groups")
    lines.append(f"- **Org chunks**: {len(org['chunks'])}")
    lines.append("")

    if proj_summaries:
        lines.append("## Projects")
        lines.append("")
        for p in sorted(proj_summaries, key=lambda x: -x["validated"]):
            lines.append(f"- **{p['id']}**: {p['claims']} claims, {p['validated']} validated")
        lines.append("")

    if org_dead_ends:
        lines.append("## Cross-project dead-ends (failed lessons)")
        lines.append("")
        for d in org_dead_ends[:10]:
            lines.append(f"- {(d.get('claim_text') or '')[:160]}")
            r = d.get("dont_repeat_reason", "")
            if r:
                lines.append(f"  - why: {r[:200]}")
        lines.append("")

    if org_validated_methodological:
        lines.append("## Methodological consensus (validated, cross-project)")
        lines.append("")
        for m in org_validated_methodological[:10]:
            lines.append(f"- {(m.get('claim_text') or '')[:160]}")
        lines.append("")

    if persons:
        lines.append("## Tracked researchers")
        lines.append("")
        for p in sorted(persons, key=lambda c: c.get("canonical_name", ""))[:15]:
            attr = p.get("attributes") or {}
            affs = attr.get("affiliations") or []
            note = attr.get("reputation_note", "")
            extra = f" — {note}" if note else ""
            aff_str = f" ({', '.join(affs)})" if affs else ""
            lines.append(f"- **{p.get('canonical_name')}**{aff_str}{extra}")
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("_Org manifest is auto-generated from org-scope KB v3 entities + per-project summaries._")

    return "\n".join(lines)


def write_org_manifest() -> Path:
    org_dir = _org_home()
    org_dir.mkdir(parents=True, exist_ok=True)
    content = render_org_manifest()
    path = org_dir / "ORG_MANIFEST.md"
    path.write_text(content, encoding="utf-8")
    return path


def regenerate_all() -> dict:
    """生成 ORG_MANIFEST.md + 每个 project 的 PROJECT_MANIFEST.md。"""
    out: dict = {"org_manifest": None, "project_manifests": []}
    out["org_manifest"] = str(write_org_manifest())
    proj_root = projects_root()
    if proj_root.exists():
        for p in sorted(proj_root.iterdir()):
            if p.is_dir() and any(p.glob("kb_*.jsonl")):
                path = write_project_manifest(p.name)
                out["project_manifests"].append(str(path))
    return out

"""平台只读 API —— `hf` CLI / 未来前端的统一查询入口。

设计：纯函数 + 简单 dict 返回值，不暴露 jsonl 文件路径细节。
覆盖：项目列表 / 项目状态 / run 列表 + 详情 / KB 统计 / 最近 error / cost / cache 状态。

不在这里的：
  - 写操作（用 KB 工具 / propose / freeze 等）
  - LLM 交互（用 LLMClient）
"""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path

from core.paths import (
    home, org_root, projects_root, runs_root, runs_anon_root,
    find_run_dir,
)


# ─────────────────────────────────────────────────────────────────────────────
# Projects
# ─────────────────────────────────────────────────────────────────────────────

def list_projects() -> list[dict]:
    """所有本机项目，含一行汇总。"""
    root = projects_root()
    if not root.exists():
        return []
    out = []
    for p in sorted(root.iterdir()):
        if not p.is_dir():
            continue
        out.append(_project_one_line(p))
    return out


def _project_one_line(p: Path) -> dict:
    claims = _count_jsonl(p / "kb_claims.jsonl")
    validated = _count_jsonl(p / "kb_claims.jsonl",
                             where=lambda r: r.get("status") == "validated")
    proposals_path = p / "kb_proposals.jsonl"
    pending = _count_jsonl(proposals_path, where=lambda r: r.get("status") == "pending")
    return {
        "project_id": p.name,
        "claims": claims,
        "validated": validated,
        "pending_proposals": pending,
    }


def project_summary(project_id: str) -> dict | None:
    p = projects_root() / project_id
    if not p.exists():
        return None
    claims = _read_jsonl(p / "kb_claims.jsonl")
    return {
        "project_id": project_id,
        "path": str(p),
        "claims_total": len(claims),
        "by_claim_type": dict(Counter(c.get("claim_type", "?") for c in claims)),
        "by_status":     dict(Counter(c.get("status", "?")     for c in claims)),
        "experiments":   _count_jsonl(p / "kb_experiments.jsonl"),
        "chunks":        _count_jsonl(p / "kb_chunks.jsonl"),
        # v1 的 memory.jsonl 已废弃（见 memory_entries 注释）——
        # 数未消化的候选：那才是"这个项目还欠多少记忆整理"。
        "memory_entries": _count_jsonl(p / "memory" / "candidates.jsonl"),
        "deliverables":  _count_deliverables(project_id),
        "concept_refs":  _top_concept_refs(claims, limit=10),
        "pending_proposals": _count_jsonl(p / "kb_proposals.jsonl",
                                          where=lambda r: r.get("status") == "pending"),
        "manifest_path": str(p / "PROJECT_MANIFEST.md") if (p / "PROJECT_MANIFEST.md").exists() else None,
    }


def _top_concept_refs(claims: list[dict], limit: int) -> list[dict]:
    """统计 concept_ids 被 claim 引用次数，附 canonical_name（从 org concepts 查）。"""
    counts: Counter = Counter()
    for c in claims:
        for cid in (c.get("concept_ids") or []):
            counts[cid] += 1
    concepts = _read_jsonl(org_root() / "kb_concepts.jsonl")
    by_id = {c["id"]: c for c in concepts}
    out = []
    for cid, n in counts.most_common(limit):
        rec = by_id.get(cid)
        out.append({
            "concept_id": cid,
            "name": rec.get("canonical_name", cid) if rec else cid,
            "type": rec.get("concept_type", "?") if rec else "?",
            "count": n,
        })
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Runs
# ─────────────────────────────────────────────────────────────────────────────

def _iter_run_dirs():
    """[v0.8] 跨布局枚举所有 run 目录，yield `(dir, implied_project_id)`。

    覆盖三种布局（v0.8 起 runs 项目嵌套，见 core/paths.py 头注释）：
      1. projects/<id>/runs/<run_id>/   —— 新主布局（implied_project_id=<id>）
      2. runs_anon/<run_id>/            —— 新 ad-hoc run（implied None）
      3. runs/<run_id>/                 —— [deprecated] 旧 flat，只读 fallback

    同 run_id 出现在多个布局（如迁移一半）时新布局优先，只 yield 一次。
    """
    seen: set[str] = set()
    pr = projects_root()
    if pr.exists():
        for proj in sorted(pr.iterdir()):
            runs = proj / "runs"
            if not proj.is_dir() or not runs.exists():
                continue
            for d in runs.iterdir():
                if d.is_dir() and d.name not in seen:
                    seen.add(d.name)
                    yield d, proj.name
    for root in (runs_anon_root(), runs_root()):
        if not root.exists():
            continue
        for d in root.iterdir():
            if d.is_dir() and d.name not in seen:
                seen.add(d.name)
                yield d, None


def list_runs(project_id: str | None = None, limit: int = 20) -> list[dict]:
    """跨项目列最近的 run，按 ended_at 倒序。可按 project_id 过滤。

    [v0.8] 枚举走 `_iter_run_dirs()`（项目嵌套 + runs_anon + 旧 flat）。
    project_id 优先取 summary.json；summary 里没写就用目录蕴含的项目名。
    """
    runs = []
    for d, implied_pid in _iter_run_dirs():
        s = _read_summary(d / "summary.json")
        if not s:
            continue
        pid = s.get("project_id") or implied_pid
        if project_id and pid != project_id:
            continue
        runs.append({
            "run_id": d.name,
            "node_type": s.get("node_type"),
            "project_id": pid,
            "status": s.get("status"),
            "turns": s.get("turns"),
            "tokens_used": s.get("tokens_used"),
            "started_at": s.get("started_at"),
            "ended_at": s.get("ended_at"),
            "n_artifacts": len(s.get("artifacts") or []),
            "path": str(d),
        })
    runs.sort(key=lambda r: r.get("ended_at") or r.get("started_at") or "", reverse=True)
    return runs[:limit]


def run_detail(run_id: str) -> dict | None:
    # [v0.8] 跨布局查（projects/*/runs → runs_anon → 旧 flat → STATE_DIR）
    d = find_run_dir(run_id)
    if d is None:
        return None
    summary = _read_summary(d / "summary.json")
    return {
        "run_id": run_id,
        "path": str(d),
        "summary": summary,
        "transcript_path": str(d / "transcript.jsonl") if (d / "transcript.jsonl").exists() else None,
        "n_artifacts": _n_run_records(d),
    }


def find_last_error(project_id: str | None = None) -> dict | None:
    """返回该 project（或所有）最近一次 error 上下文。"""
    runs = list_runs(project_id, limit=200)
    for r in runs:
        if r["status"] not in ("error", "incomplete"):
            continue
        # [v0.8] list_runs 已带跨布局的真实路径，别再假设旧 flat runs/
        d = Path(r["path"])
        events = _read_jsonl(d / "transcript.jsonl")
        # 最后一次 error tool_call 或 llm error
        err_events = [
            e for e in events
            if (isinstance(e.get("result"), dict) and e["result"].get("status") == "error")
               or e.get("error")
               or (e.get("event", "").endswith("_error"))
        ]
        if err_events:
            ctx = err_events[-1]
            return {
                "run_id": r["run_id"],
                "node_type": r["node_type"],
                "project_id": r["project_id"],
                "ended_at": r["ended_at"],
                "error_event": ctx,
            }
        return {
            "run_id": r["run_id"],
            "node_type": r["node_type"],
            "project_id": r["project_id"],
            "ended_at": r["ended_at"],
            "error_event": None,
            "note": "run failed but no explicit error event in transcript",
        }
    return None


# ─────────────────────────────────────────────────────────────────────────────
# KB query
# ─────────────────────────────────────────────────────────────────────────────

def kb_stats(project_id: str | None = None) -> dict:
    """KB 统计。project_id=None → org 层。"""
    if project_id is None:
        root = org_root()
        scope = "org"
    else:
        root = projects_root() / project_id
        scope = f"project:{project_id}"
    return {
        "scope": scope,
        "concepts":    _count_jsonl(root / "kb_concepts.jsonl"),
        "claims":      _count_jsonl(root / "kb_claims.jsonl"),
        "experiments": _count_jsonl(root / "kb_experiments.jsonl"),
        "chunks":      _count_jsonl(root / "kb_chunks.jsonl"),
        "by_claim_type": dict(Counter(
            c.get("claim_type") for c in _read_jsonl(root / "kb_claims.jsonl")
        )),
    }


def kb_show(entity: str, kb_id: str) -> dict | None:
    """跨 project + org 找一条 record。"""
    roots = [org_root()]
    if projects_root().exists():
        roots.extend(p for p in projects_root().iterdir() if p.is_dir())
    for root in roots:
        path = root / f"kb_{entity}.jsonl"
        if not path.exists():
            continue
        for r in _read_jsonl(path):
            if r.get("id") == kb_id:
                return {**r, "_scope_root": str(root)}
    return None


def kb_list(entity: str = "claims", project_id: str | None = None,
            limit: int = 50, offset: int = 0) -> list[dict]:
    """列一个 entity 的全部 record（project 层 shadow org 层，同 id 只出一次）。

    `kb_search` 只能按关键词找，`kb_stats` 只给计数 —— 前端的列表页要的是
    「把这个项目的 concept / claim 摊开给我看」。口径与 `State.list_kb` 一致：
    project 先、org 后，先见者赢。
    """
    roots = []
    if project_id:
        roots.append(projects_root() / project_id)
    roots.append(org_root())
    seen: set[str] = set()
    out: list[dict] = []
    for root in roots:
        for r in _read_jsonl(root / f"kb_{entity}.jsonl"):
            rid = r.get("id")
            if not rid or rid in seen:
                continue
            seen.add(rid)
            out.append(r)
    return out[offset:offset + limit]


def memory_entries(project_id: str, limit: int = 50) -> list[dict]:
    """项目记忆 —— MEMORY.md 的五个节。

    记忆现在**只有一个落盘物**：Project worktree 根下的 `MEMORY.md`
    （见 `core/memory.py`）。候选队列、topic 分文件、episodes 都已删除 ——
    候选队列是"在出生时设门"，实测 47% 从未被加工。

    返回每节一条；手册两节额外展开成条目，供 UI 列表渲染。
    """
    from core import memory as M

    class _S:            # api 层没有 State，记忆只需要 worktree 路径
        project_worktree = None

    root = projects_root() / project_id
    if not root.is_dir():
        return []
    st = _S()
    st.project_worktree = root
    if M.memory_path(st) is None or not M.memory_path(st).is_file():
        return []

    out: list[dict] = []
    for name in (M.SECTION_GOAL, M.SECTION_LAW, M.SECTION_NARRATIVE):
        body = M.read_section(st, name)
        if body.strip():
            out.append({"kind": name, "title": M.SECTION_TITLE[name],
                        "content": body})
    for e in M.manual_entries(st):
        out.append({"kind": e.section, "title": M.SECTION_TITLE[e.section],
                    **e.as_dict()})
    return out[:limit]


def kb_search(query: str, entity: str = "claims",
              project_id: str | None = None, limit: int = 20) -> list[dict]:
    q = query.lower()
    paths = []
    if project_id:
        paths.append(projects_root() / project_id / f"kb_{entity}.jsonl")
        paths.append(org_root() / f"kb_{entity}.jsonl")
    else:
        # 跨项目搜
        paths.append(org_root() / f"kb_{entity}.jsonl")
        if projects_root().exists():
            for p in projects_root().iterdir():
                if p.is_dir():
                    paths.append(p / f"kb_{entity}.jsonl")
    out: list[dict] = []
    for path in paths:
        for r in _read_jsonl(path):
            blob = " ".join(str(r.get(k, "")) for k in
                            ("canonical_name", "claim_text", "experiment_text", "text"))
            if q in blob.lower():
                out.append(r)
                if len(out) >= limit:
                    return out
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Pending proposals
# ─────────────────────────────────────────────────────────────────────────────

def pending_proposals(project_id: str | None = None) -> list[dict]:
    if project_id:
        roots = [projects_root() / project_id]
    elif projects_root().exists():
        roots = [p for p in projects_root().iterdir() if p.is_dir()]
    else:
        return []
    out = []
    for root in roots:
        for r in _read_jsonl(root / "kb_proposals.jsonl"):
            if r.get("status") == "pending":
                r["_project_id"] = root.name
                out.append(r)
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Cost
# ─────────────────────────────────────────────────────────────────────────────

def cost_estimate(project_id: str | None = None) -> dict:
    """从所有 run.summary.json 的 tokens_used 估算累计 token 量。

    tokens 是 summary 的历史口径（跨全部布局，覆盖记账层上线之前的 run）。
    金额与缓存命中率来自 `.harness/llm_cost.jsonl`（见 core/cost_ledger），
    只覆盖记账层上线之后的调用 —— 两者口径不同，所以分开呈现，不相加。
    """
    runs = list_runs(project_id, limit=10_000)
    total_tokens = sum(r.get("tokens_used") or 0 for r in runs)
    by_node = defaultdict(int)
    for r in runs:
        by_node[r.get("node_type") or "?"] += r.get("tokens_used") or 0
    return {
        "scope": project_id or "all",
        "run_count": len(runs),
        "total_tokens": total_tokens,
        "by_node_type": dict(by_node),
        "ledger": _ledger_summary(project_id),
    }


def _ledger_summary(project_id: str | None) -> dict | None:
    """跨项目汇总成本账本。没有任何账本文件 → None（还没跑过带记账的 run）。"""
    from core import cost_ledger

    roots: list = []
    pr = projects_root()
    if pr.exists():
        for proj in sorted(pr.iterdir()):
            if not proj.is_dir():
                continue
            if project_id and proj.name != project_id:
                continue
            roots.append(proj)

    total = cost_ledger.Rollup()
    found = False
    for root in roots:
        r = cost_ledger.read_rollup(root)
        if r.calls == 0:
            continue
        found = True
        total.calls += r.calls
        total.prompt_tokens += r.prompt_tokens
        total.completion_tokens += r.completion_tokens
        total.cache_read += r.cache_read
        total.cost_usd += r.cost_usd
        total.cost_known_calls += r.cost_known_calls
        total.cache_reported_calls += r.cache_reported_calls
        for k, v in r.by_node.items():
            total.by_node[k] = round(total.by_node.get(k, 0.0) + v, 6)
        for k, v in r.by_model.items():
            total.by_model[k] = round(total.by_model.get(k, 0.0) + v, 6)
    if not found:
        return None
    total.cost_usd = round(total.cost_usd, 6)
    return {
        "calls": total.calls,
        "prompt_tokens": total.prompt_tokens,
        "completion_tokens": total.completion_tokens,
        "cache_read": total.cache_read,
        "cache_hit_ratio": total.cache_hit_ratio,
        "cost_usd": total.cost_usd,
        "cost_is_partial": total.cost_is_partial,
        "by_node": total.by_node,
        "by_model": total.by_model,
        "text": cost_ledger.format_rollup(total),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Cache info
# ─────────────────────────────────────────────────────────────────────────────

def cache_info() -> dict:
    from core import llm_cache
    return llm_cache.stats()


# ─────────────────────────────────────────────────────────────────────────────
# Deliverables
# ─────────────────────────────────────────────────────────────────────────────

def _n_run_records(run_dir: Path) -> int:
    """run 本地账本里登记的身份数（core/ledger）。"""
    from core.ledger import RecordStore

    return len(RecordStore(run_dir / "artifacts", run_dir / "records.jsonl").heads())


def list_deliverables(project_id: str) -> dict:
    """项目工作区账本里**冻结且永久**的记录 —— 这就是交付物，没有第二份拷贝。"""
    from core.ledger import workspace_store
    from core.project_bootstrap import project_worktree_path
    from shared.lib.artifact_policy import is_permanent

    root = project_worktree_path(project_id)
    if root is None or not root.is_dir():
        return {"project_id": project_id, "deliverables": {}}
    store = workspace_store(root)
    out: dict[str, list[dict]] = {}
    for head in store.heads().values():
        if not head.frozen_version or not is_permanent(head.artifact_type):
            continue
        out.setdefault(head.artifact_type, []).append({
            "id": head.artifact_id, "path": str(store.abs_path(head)),
            "version": head.frozen_version, "frozen_at": head.frozen_at,
        })
    return {"project_id": project_id, "deliverables": out}


def _count_deliverables(project_id: str) -> int:
    return sum(len(items) for items in list_deliverables(project_id)["deliverables"].values())


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    except OSError:
        pass
    return out


def _count_jsonl(path: Path, *, where=None) -> int:
    records = _read_jsonl(path)
    if where is None:
        return len(records)
    return sum(1 for r in records if where(r))


def _read_summary(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None

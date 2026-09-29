"""hf —— Harness Framework CLI

入口（`pip install -e .` 后）：hf <command> ...
或直接：python -m core.cli <command> ...

子命令：
  hf status [project_id]            项目状态 / 全部项目列表
  hf runs [-p project_id] [-n N]    最近 N 个 run（按时间）
  hf log <run_id> [--filter ...]    打印 run 的 transcript（核心事件）
  hf last-error [-p project_id]     最近一次失败
  hf kb stats [-p project_id]       KB 统计
  hf kb show <entity> <id>          单条 record 详情
  hf kb search <query> [-e claims]  跨 project 搜
  hf inbox [-p project_id]          pending proposals
  hf cost [-p project_id]           token 消耗汇总
  hf cache stats                    LLM cache 状态
  hf cache clear                    清 LLM cache
  hf sandbox new <name>             起一个临时 home 沙盒
  hf sandbox list                   列沙盒
  hf sandbox prune [--older Nd]     清旧沙盒
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import api, llm_cache
from core.paths import home, runs_root, projects_root


# ─── 颜色 / 格式 helpers ───────────────────────────────────────────────────

def _supports_color() -> bool:
    return sys.stdout.isatty() and os.getenv("NO_COLOR") is None


_C = {
    "reset": "\033[0m", "bold": "\033[1m", "dim": "\033[2m",
    "red": "\033[31m", "green": "\033[32m", "yellow": "\033[33m",
    "blue": "\033[34m", "magenta": "\033[35m", "cyan": "\033[36m",
}


def c(text: str, color: str) -> str:
    if not _supports_color():
        return text
    return f"{_C.get(color, '')}{text}{_C['reset']}"


def header(text: str) -> str:
    return c(f"═══ {text} ═══", "bold")


def kv(k: str, v) -> str:
    return f"  {c(k, 'dim'):<24}  {v}"


def humanize_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024
    return f"{n:.1f} TB"


def status_icon(status: str | None) -> str:
    return {
        "completed": c("✅", "green"),
        "incomplete": c("⚠️ ", "yellow"),
        "error": c("❌", "red"),
        "paused": c("⏸ ", "yellow"),
        "running": c("▶️ ", "blue"),
    }.get(status or "", c("·", "dim"))


# ─── 子命令 impl ───────────────────────────────────────────────────────────

def cmd_status(args):
    if args.project_id:
        s = api.project_summary(args.project_id)
        if not s:
            print(c(f"❌ project {args.project_id!r} 不存在", "red"))
            return 1
        print(header(f"{args.project_id}"))
        print(kv("path", s["path"]))
        print()
        print(c("KB:", "bold"))
        print(kv("claims (total)", s["claims_total"]))
        if s["by_claim_type"]:
            print(kv("by claim_type",
                     ", ".join(f"{k}:{v}" for k, v in s["by_claim_type"].items())))
        if s["by_status"]:
            print(kv("by status",
                     ", ".join(f"{k}:{v}" for k, v in s["by_status"].items())))
        print(kv("experiments", s["experiments"]))
        print(kv("chunks", s["chunks"]))
        print(kv("memory entries", s["memory_entries"]))
        print(kv("deliverables", s["deliverables"]))
        print()
        if s["concept_refs"]:
            print(c("Top concept refs:", "bold"))
            for cr in s["concept_refs"]:
                print(kv(f"{cr['name']} ({cr['type']})", f"{cr['count']} claims"))
        print()
        runs = api.list_runs(args.project_id, limit=5)
        if runs:
            print(c("Recent runs (last 5):", "bold"))
            for r in runs:
                print(f"  {status_icon(r['status'])} {c(r['run_id'][:16], 'cyan'):<20} "
                      f"{r['node_type']:<14} {r['turns'] or '?':>3} turns")
        if s["pending_proposals"]:
            print()
            print(c(f"⚠️  {s['pending_proposals']} pending proposals "
                    "(run `hf inbox -p ...`)", "yellow"))
        return 0

    # 全部项目
    projects = api.list_projects()
    if not projects:
        print(c("No projects on this machine.", "dim"))
        return 0
    print(header("Projects on this machine"))
    for p in projects:
        marker = c(f" · {p['pending_proposals']} pending", "yellow") if p["pending_proposals"] else ""
        print(f"  • {c(p['project_id'], 'cyan'):<40} "
              f"{p['claims']} claims  {p['validated']} validated{marker}")
    return 0


def cmd_runs(args):
    runs = api.list_runs(args.project, limit=args.n)
    if not runs:
        print(c("No runs found.", "dim"))
        return 0
    scope = f"project={args.project}" if args.project else "all projects"
    print(header(f"Recent runs ({scope}, last {len(runs)})"))
    for r in runs:
        print(f"  {status_icon(r['status'])} {c(r['run_id'][:18], 'cyan'):<24} "
              f"{r['node_type']:<14} {(r['turns'] or 0):>3}t  "
              f"{c((r['project_id'] or '-')[:30], 'dim')}")
    return 0


def cmd_log(args):
    detail = api.run_detail(args.run_id)
    if not detail:
        print(c(f"❌ run {args.run_id!r} 不存在", "red"))
        return 1
    transcript_path = Path(detail["transcript_path"]) if detail["transcript_path"] else None
    if not transcript_path or not transcript_path.exists():
        print(c("no transcript.jsonl", "dim"))
        return 1
    show_filter = args.filter.split(",") if args.filter else None
    for line in transcript_path.read_text(encoding="utf-8").splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        etype = ev.get("event", "?")
        if show_filter and not any(f in etype for f in show_filter):
            continue
        ts = ev.get("at", "")[11:19]
        if etype == "tool_call_start":
            print(f"{c(ts, 'dim')} {c(etype, 'blue')} {c(ev.get('tool_name', '?'), 'cyan')} "
                  f"{c(json.dumps(ev.get('tool_args', {}))[:80], 'dim')}")
        elif etype == "tool_call_end":
            r = ev.get("result") or {}
            s = r.get("status", "?") if isinstance(r, dict) else "?"
            col = "red" if s == "error" else "green"
            print(f"{c(ts, 'dim')} {c(etype, 'blue')} {ev.get('tool_name', '?')} "
                  f"→ {c(s, col)}")
        elif etype.startswith("llm_call"):
            print(f"{c(ts, 'dim')} {c(etype, 'magenta')}")
        else:
            print(f"{c(ts, 'dim')} {c(etype, 'yellow')}")
    return 0


def cmd_last_error(args):
    err = api.find_last_error(args.project)
    if not err:
        print(c("✅ No recent errors.", "green"))
        return 0
    print(header(f"Latest error · run {err['run_id'][:16]}"))
    print(kv("node", err["node_type"]))
    print(kv("project", err["project_id"]))
    print(kv("ended_at", err["ended_at"]))
    if err["error_event"]:
        e = err["error_event"]
        print()
        print(c("Error event:", "bold"))
        print(json.dumps(e, ensure_ascii=False, indent=2)[:1500])
    else:
        print(c(f"\n  {err.get('note', '')}", "dim"))
    return 0


def cmd_kb_stats(args):
    s = api.kb_stats(args.project)
    print(header(f"KB stats · scope={s['scope']}"))
    print(kv("concepts", s["concepts"]))
    print(kv("claims", s["claims"]))
    print(kv("experiments", s["experiments"]))
    print(kv("chunks", s["chunks"]))
    if s["by_claim_type"]:
        print()
        print(c("by claim_type:", "bold"))
        for k, v in s["by_claim_type"].items():
            print(kv(k, v))
    return 0


def cmd_kb_show(args):
    r = api.kb_show(args.entity, args.id)
    if not r:
        print(c(f"❌ {args.entity}/{args.id} 找不到", "red"))
        return 1
    scope_root = r.pop("_scope_root", "")
    print(header(f"{args.entity[:-1]} {args.id}"))
    print(kv("scope root", scope_root))
    print()
    print(json.dumps(r, ensure_ascii=False, indent=2))
    return 0


def cmd_kb_search(args):
    out = api.kb_search(args.query, entity=args.entity,
                         project_id=args.project, limit=args.limit)
    if not out:
        print(c("no matches", "dim"))
        return 0
    print(header(f"{len(out)} matches in {args.entity}"))
    for r in out:
        preview = (r.get("claim_text") or r.get("canonical_name")
                   or r.get("experiment_text") or r.get("text") or "")[:120]
        print(f"  {c(r.get('id', '?'), 'cyan')}  "
              f"[{r.get('status') or r.get('claim_type') or '?'}]  {preview}")
    return 0


def cmd_inbox(args):
    proposals = api.pending_proposals(args.project)
    if not proposals:
        print(c("✅ Inbox empty.", "green"))
        return 0
    print(header(f"Pending proposals ({len(proposals)})"))
    for p in proposals:
        kind = p.get("proposal_type") or p.get("kind") or "?"
        proj = p.get("_project_id", "")
        print(f"  {c(p.get('id', '?'), 'cyan')}  {c(kind, 'yellow'):<25} "
              f"{c('['+proj+']', 'dim')}")
        text = (p.get("payload") or {}).get("claim_text") or p.get("description") or ""
        if text:
            print(f"    {text[:100]}")
    return 0


def cmd_cost(args):
    c_ = api.cost_estimate(args.project)
    print(header(f"Cost · scope={c_['scope']}"))
    print(kv("runs counted", c_["run_count"]))
    print(kv("total tokens", f"{c_['total_tokens']:,}"))
    if c_["by_node_type"]:
        print()
        print(c("by node_type:", "bold"))
        for k, v in sorted(c_["by_node_type"].items(), key=lambda x: -x[1]):
            print(kv(k, f"{v:,}"))

    led = c_.get("ledger")
    print()
    if led is None:
        print(c("金额与缓存命中率：暂无账本（记账层上线后的 run 才有）", "dim"))
    else:
        print(c("账本（.harness/llm_cost.jsonl）:", "bold"))
        for line in (led.get("text") or "").splitlines():
            print("  " + line)
    return 0


def cmd_cache(args):
    if args.action == "stats":
        s = api.cache_info()
        print(header("LLM cache"))
        print(kv("path", s["path"]))
        print(kv("enabled", s.get("enabled", False)))
        print(kv("entries", s["count"]))
        print(kv("size", humanize_bytes(s["size_bytes"])))
        if not s.get("enabled"):
            print(c("\n  set HARNESS_LLM_CACHE=on to enable", "dim"))
    elif args.action == "clear":
        n = llm_cache.clear()
        print(c(f"✓ cleared {n} cache entries", "green"))
    return 0


def cmd_doctor(args):
    """框架健康检查 + 偏离默认行为的节点提示。"""
    from core.custom_loop import list_nodes_with_custom_loop
    from core.paths import home, identity_path

    issues = 0
    print(header("Framework health check"))

    # identity
    if identity_path().exists():
        print(kv("identity.json", c("✓", "green") + " " + str(identity_path())))
    else:
        print(kv("identity.json", c("✗", "yellow") + " 缺，首次跑节点会自动生成"))

    # home
    print(kv("home", str(home())))

    # 节点偏离 framework 默认行为
    print()
    print(c("节点偏离默认行为：", "bold"))
    custom_loops = list_nodes_with_custom_loop()
    if custom_loops:
        for n in custom_loops:
            print(f"  {c('⚠️ ', 'yellow')} {n} 用 custom agent_loop")
            print(c(f"     framework 保证减弱，详见 templates/agent_loop.py.template", "dim"))
            issues += 1
    else:
        print(c("  ✓ 所有节点用默认 agent_loop", "green"))

    # summarizer override
    from pathlib import Path as _P
    nodes_dir = _P(__file__).resolve().parent.parent / "nodes"
    if nodes_dir.exists():
        with_summarizer = sorted(
            p.name for p in nodes_dir.iterdir()
            if p.is_dir() and (p / "summarizer.py").exists()
        )
        with_hooks = sorted(
            p.name for p in nodes_dir.iterdir()
            if p.is_dir() and (p / "hooks.py").exists()
        )
        if with_summarizer:
            print(f"\n  {c('•', 'cyan')} custom summarizer: {', '.join(with_summarizer)}")
        if with_hooks:
            print(f"  {c('•', 'cyan')} custom hooks: {', '.join(with_hooks)}")

    print()
    if issues == 0:
        print(c("✅ no issues", "green"))
    else:
        print(c(f"⚠️  {issues} 项需注意（不一定是 bug，提示而已）", "yellow"))
    return 0


# ─── sandbox ───────────────────────────────────────────────────────────────

def _sandbox_tmp_root() -> Path:
    """tempfile 默认的临时根（macOS /var/folders/...，Linux /tmp/）。"""
    return Path(tempfile.gettempdir())


def _find_sandboxes() -> list[Path]:
    """扫 tempdir + /tmp 找 hf-sandbox-* 目录。"""
    roots = {_sandbox_tmp_root(), Path("/tmp")}
    out = []
    for r in roots:
        if r.exists():
            out.extend(r.glob("hf-sandbox-*"))
    # dedup by realpath
    seen = set()
    uniq = []
    for p in out:
        rp = p.resolve()
        if rp not in seen:
            seen.add(rp)
            uniq.append(p)
    return sorted(uniq)


def cmd_sandbox(args):
    if args.action == "new":
        path = Path(tempfile.mkdtemp(prefix=f"hf-sandbox-{args.name}-"))
        print(c(f"✓ sandbox created at {path}", "green"))
        print()
        print("To use:")
        print(c(f"  export HARNESS_FRAMEWORK_HOME={path}", "cyan"))
        print(c("  hf status      # 看的就是这个沙盒", "dim"))
    elif args.action == "list":
        boxes = _find_sandboxes()
        if not boxes:
            print(c("no sandboxes", "dim"))
            return 0
        print(header(f"Sandboxes ({len(boxes)})"))
        now = time.time()
        for b in boxes:
            age_days = (now - b.stat().st_mtime) / 86400
            print(f"  {b}  ({age_days:.1f}d old)")
    elif args.action == "prune":
        cutoff = time.time() - args.older * 86400
        deleted = 0
        for b in _find_sandboxes():
            if b.stat().st_mtime < cutoff:
                shutil.rmtree(b, ignore_errors=True)
                deleted += 1
        print(c(f"✓ pruned {deleted} sandboxes (older than {args.older}d)", "green"))
    return 0


# ─── argparse ──────────────────────────────────────────────────────────────

def main():
    # 最前面：Windows 上没在 UTF-8 模式就带 PYTHONUTF8=1 re-exec 一次，否则头一句
    # print（doctor 的框边/中文）就 UnicodeEncodeError（cp1252）。POSIX/已是 utf-8=无操作。
    from shared.lib.platform_env import ensure_utf8_mode
    ensure_utf8_mode()

    p = argparse.ArgumentParser(prog="hf", description="Harness Framework CLI")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("status", help="项目状态")
    sp.add_argument("project_id", nargs="?", default=None)
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("runs", help="最近 run 列表")
    sp.add_argument("-p", "--project", default=None)
    sp.add_argument("-n", type=int, default=20)
    sp.set_defaults(func=cmd_runs)

    sp = sub.add_parser("log", help="单 run 的 transcript 简化输出")
    sp.add_argument("run_id")
    sp.add_argument("--filter", default=None,
                     help="只显示含这些 substring 的 event（逗号分）")
    sp.set_defaults(func=cmd_log)

    sp = sub.add_parser("last-error", help="最近一次失败")
    sp.add_argument("-p", "--project", default=None)
    sp.set_defaults(func=cmd_last_error)

    sp = sub.add_parser("kb", help="KB 操作")
    kb_sub = sp.add_subparsers(dest="kb_cmd", required=True)
    ks = kb_sub.add_parser("stats")
    ks.add_argument("-p", "--project", default=None)
    ks.set_defaults(func=cmd_kb_stats)
    kshow = kb_sub.add_parser("show")
    kshow.add_argument("entity", choices=["concepts", "claims", "experiments", "chunks"])
    kshow.add_argument("id")
    kshow.set_defaults(func=cmd_kb_show)
    ksearch = kb_sub.add_parser("search")
    ksearch.add_argument("query")
    ksearch.add_argument("-e", "--entity", default="claims",
                          choices=["concepts", "claims", "experiments", "chunks"])
    ksearch.add_argument("-p", "--project", default=None)
    ksearch.add_argument("-l", "--limit", type=int, default=20)
    ksearch.set_defaults(func=cmd_kb_search)

    sp = sub.add_parser("inbox", help="pending proposals")
    sp.add_argument("-p", "--project", default=None)
    sp.set_defaults(func=cmd_inbox)

    sp = sub.add_parser("cost", help="token 消耗")
    sp.add_argument("-p", "--project", default=None)
    sp.set_defaults(func=cmd_cost)

    sp = sub.add_parser("cache", help="LLM cache 管理")
    sp.add_argument("action", choices=["stats", "clear"])
    sp.set_defaults(func=cmd_cache)

    sp = sub.add_parser("doctor", help="框架健康检查 + 偏离默认的节点提示")
    sp.set_defaults(func=cmd_doctor)

    sp = sub.add_parser("sandbox", help="临时 HOME 沙盒")
    sb_sub = sp.add_subparsers(dest="action", required=True)
    sn = sb_sub.add_parser("new")
    sn.add_argument("name", help="沙盒名（拼进路径方便识别）")
    sn.set_defaults(func=cmd_sandbox, action="new")
    sl = sb_sub.add_parser("list")
    sl.set_defaults(func=cmd_sandbox, action="list")
    spr = sb_sub.add_parser("prune")
    spr.add_argument("--older", type=int, default=7, help="清 N 天前的（默认 7）")
    spr.set_defaults(func=cmd_sandbox, action="prune")

    args = p.parse_args()
    sys.exit(args.func(args) or 0)


if __name__ == "__main__":
    main()

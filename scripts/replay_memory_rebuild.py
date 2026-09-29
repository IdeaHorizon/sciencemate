"""记忆重建的真实数据体检：装入真实手册 → 送达 → 遗忘扫描。

单测只验我想到的失败方式。这个脚本跑在**真实沉积**上，用来问单测问不出的：

  · 真实项目的 MEMORY.md 长什么样、条目质量如何
  · 9 个节点裸调度实际收到多少字节、命中几条
  · 三项遗忘作业在真数据上报出多少 —— **误报率**只有真数据能告诉你

2026-08-21 首跑抓出一个单测全绿也没抓到的缺陷：矛盾检测在 175 条手册上
报出 **166 对**误报（判据是"共享 ≥4 个 shingle"，而中文 bigram 里同领域
两句话轻易共享十几个字对）。误报会让人学会忽略红旗，那比没有红旗更糟。
判据改成"措辞近似度落在带内"后归零，且真矛盾仍然命中。

## 数据源

记忆只有一个落点：`<Project worktree>/MEMORY.md`（docs/memory-system.md §1）。
旧布局（memory/candidates.jsonl、topic 分文件）的一次性吸收器已于 2026-09-02
删除 —— 还带着旧布局的项目不会被吸收，也不在本脚本视野内。

worktree 用 `--worktrees` 显式给；不给则在 harness home 的 `projects/*/workspace`
里找带 MEMORY.md 的 Git worktree。

## 安全

**永远跑在副本上。** 脚本把每个 worktree 复制到临时目录再跑 ——
体检不该有写回真盘的可能性。

    python scripts/replay_memory_rebuild.py
    python scripts/replay_memory_rebuild.py --worktrees /path/to/project/worktree ...
    python scripts/replay_memory_rebuild.py --home /path/to/harness-home
"""
import argparse
import os, sys, shutil, subprocess, tempfile
from pathlib import Path

_ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
_ap.add_argument("--home", default=str(Path.home() / ".harness-framework"),
                 help="harness home（只用来找 projects/*/workspace；不会被写）")
_ap.add_argument("--worktrees", nargs="*", default=None,
                 help="要体检的 Project worktree 路径；不给则自动在 home 里找")
OPTS = _ap.parse_args()

WORK = Path(tempfile.mkdtemp(prefix="mem-replay-"))
os.environ["HARNESS_FRAMEWORK_HOME"] = str(WORK)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _is_worktree(p: Path) -> bool:
    return (p / ".git").exists() and (p / "MEMORY.md").is_file()


def _discover(home: Path, n: int = 4) -> list[Path]:
    projects = home / "projects"
    if not projects.is_dir():
        sys.exit(f"找不到 {projects} —— 用 --home 或 --worktrees 指定")
    scored = []
    for d in projects.iterdir():
        ws = d / "workspace"
        if _is_worktree(ws):
            scored.append(((ws / "MEMORY.md").stat().st_size, ws))
    return [ws for _, ws in sorted(scored, reverse=True)[:n]]


SOURCES = [Path(p).expanduser().resolve() for p in OPTS.worktrees] if OPTS.worktrees \
    else _discover(Path(OPTS.home))
if not SOURCES:
    sys.exit("没有找到带 MEMORY.md 的 Project worktree —— 用 --worktrees 指定")
for src in SOURCES:
    if not _is_worktree(src):
        sys.exit(f"{src} 不是带 MEMORY.md 的 Git worktree")

from core.bootstrap import bootstrap
from core.state import State
from core import memory as M, memory_forget as F
from core.memory_delivery import constitution_block, onboarding_slice, tool_briefing
from core.loader import load_harness
bootstrap()

def rule(t): print(f"\n{'='*76}\n  {t}\n{'='*76}")

rule("1 · 装入：真实 worktree 的 MEMORY.md（副本）")
states = {}
for src in SOURCES:
    pid = src.parent.name if src.name == "workspace" else src.name
    wt = WORK / "wt" / pid
    shutil.copytree(src, wt, symlinks=True,
                    ignore=shutil.ignore_patterns("*.lock", "kb_embeddings"))
    # 副本必须仍是 Git worktree（记忆跟着 Git 走，docs §9）；不是就现场 init
    if subprocess.run(["git", "-C", str(wt), "rev-parse", "--show-toplevel"],
                      capture_output=True).returncode != 0:
        for c in (["git", "init", "-q"], ["git", "config", "user.email", "t@t"],
                  ["git", "config", "user.name", "t"]):
            subprocess.run(c, cwd=wt, check=True)
        subprocess.run(["git", "add", "-A"], cwd=wt, check=True)
        subprocess.run(["git", "commit", "-qm", "replay copy"], cwd=wt, check=True)
    st = State.new(node_type="_curator", base_dir=WORK / "runs" / pid,
                   project_id=pid, project_worktree=wt)
    states[pid] = st
    size = M.memory_path(st).stat().st_size
    ents = M.manual_entries(st)
    laws = M.parse_laws(M.read_section(st, M.SECTION_LAW))
    print(f"  {pid:<30} MEMORY.md {size:>8,}B  手册 {len(ents):>4} 条  "
          f"铁律 {len(laws):>3} 条  目标={'✓' if M.read_section(st, M.SECTION_GOAL).strip() else '—'}")

rule("2 · 送达：9 个节点裸调度实际收到多少")
st = next(iter(states.values()))
pid = next(iter(states))
if st:
    M.write_section(st, M.SECTION_LAW,
        "- 任何修复必须回答：改的是产生问题的那一层，还是症状层？")
    print(f"{'节点':<15}{'宪法':<8}{'开工切片':<12}{'命中条目'}")
    print("-"*50)
    for n in ("literature","hypothesis","data","experiment","postprocess",
              "writing","_curator","_reviewer","_orchestrator"):
        s2 = State.new(node_type=n, base_dir=WORK/"runs"/f"d_{n}",
                       project_id=pid, project_worktree=st.project_worktree)
        c = constitution_block(s2)
        names = [getattr(t,"name","") for t in (load_harness(n).tools or [])]
        sl = onboarding_slice(s2, n, names)
        cnt = sl.count("\n- ") if sl else 0
        print(f"{n:<15}{'✓' if c else '✗':<8}{(f'{len(sl):,}B' if sl else '—'):<12}{cnt}")

rule("3 · 首用附单：动手时刻弹出")
if st:
    hits = 0
    for tool in ("save_artifact","freeze_artifact","create_claim","search_kb"):
        b = tool_briefing(st, tool)
        if b:
            hits += 1
            print(f"  {tool}: {b.splitlines()[1][:88]}")
    if not hits:
        print("  （无命中 —— 条目 applies_to.tools 为空，只按节点送达）")

rule("4 · 遗忘扫描：三项机械作业")
if st:
    res = F.scan_for_forgetting(st)
    print(f"  手册总量：{res['total_entries']} 条")
    print(f"  引用失效：{len(res['dangling_reference'])}  "
          f"衰减：{len(res['decayed'])}  矛盾：{len(res['contradictions'])}")
    for d in res["dangling_reference"][:3]:
        print(f"    ⚠️ {d['text'][:70]}… → 缺 {d['missing_tools'] or d['missing_nodes']}")
    for c in res["contradictions"][:2]:
        print(f"    ⚡ A: {c['a'][:60]}…")
        print(f"       B: {c['b'][:60]}…")

rule("5 · MEMORY.md 抽样")
if st:
    doc = M.read_document(st)
    print(f"  总大小 {len(doc.encode()):,}B")
    for sec in M.SECTIONS:
        body = M.read_section(st, sec)
        n = len(M.parse_entries(body, sec)) if sec in M.MANUAL_SECTIONS else 0
        print(f"    {M.SECTION_TITLE[sec]:<12} {len(body.encode()):>7,}B"
              + (f"  {n} 条" if sec in M.MANUAL_SECTIONS else ""))
    ents = M.manual_entries(st)
    if ents:
        print("\n  条目样例：")
        for e in ents[:3]:
            print(f"    - {e.text[:78]}")
            print(f"      适用：nodes={list(e.nodes)} tools={list(e.tools)} seen={e.seen}")

print(f"\n副本：{WORK}（只读源，写只发生在副本里）")

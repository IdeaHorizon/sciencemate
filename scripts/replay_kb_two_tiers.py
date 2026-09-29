"""KB 两层（project / org）在**真实沉积数据**上的回放体检。

单测只验我想到的失败方式。这个脚本跑在真 KB 上，用来问单测问不出的问题：

  1. 现存 org 层在新规矩下是什么成色（P1 的诊断价值）
  2. 类型收敛后老数据还进得了晋升分道吗（P5 归一层）
  3. 拿真项目跑终态扫盘，候选清单像不像话（P2）
  4. 真 claim 原样当知识卡投进去，会被哪一条拦下（卡片质量）
  5. 新项目开题机械收到的到底是什么，预算撑不撑得住（P3）
  6. dreaming 在真 org 上找出了什么（P4）

2026-08-21 首跑抓出三个单测全绿也没抓到的缺陷：
  · 三条测试 fixture 漏进了真 org KB，正在被机械注入进每个新项目的开题
  · 知识卡没有 `domain`，正典层于是把 224 条互不相干的结论塞进同一篇综述
  · `check_deprojectified` 把「卡片缺字段」和「含项目指代」合并报，
    调用方（本脚本自己）据此写出了指向假原因的结论

## 安全

**永远跑在副本上。** 默认把 `~/.harness-framework` 复制到临时目录再跑 ——
体检不该有写回真盘的可能性。传 `--home <path>` 指定别的数据源（同样会复制）。

    python scripts/replay_kb_two_tiers.py
    python scripts/replay_kb_two_tiers.py --home /path/to/harness-home
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from collections import Counter
from pathlib import Path

_args = argparse.ArgumentParser(description=__doc__.splitlines()[0])
_args.add_argument("--home", default=str(Path.home() / ".harness-framework"),
                   help="数据源 harness home（会被复制，不会被写）")
_args.add_argument("--keep", action="store_true", help="保留副本目录")
OPTS = _args.parse_args()

_SRC = Path(OPTS.home)
if not (_SRC / "org").exists():
    sys.exit(f"找不到 {_SRC / 'org'} —— 用 --home 指定 harness home")

WORK = Path(tempfile.mkdtemp(prefix="kb-replay-"))
shutil.copytree(_SRC / "org", WORK / "org")
if (_SRC / "projects").exists():
    shutil.copytree(_SRC / "projects", WORK / "projects",
                    ignore=shutil.ignore_patterns("*.lock", "runs"))
for lock in WORK.rglob("*.lock"):
    lock.unlink(missing_ok=True)

os.environ["HARNESS_FRAMEWORK_HOME"] = str(WORK)
os.environ.setdefault("HARNESS_DISABLE_SEMANTIC_DEDUP", "1")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.bootstrap import bootstrap  # noqa: E402
from core.state import State  # noqa: E402

SCRATCH = WORK
ORG = WORK / "org"
PROJECTS = WORK / "projects"


def rule(title: str) -> None:
    print(f"\n{'═' * 78}\n  {title}\n{'═' * 78}")


def sub(title: str) -> None:
    print(f"\n── {title} " + "─" * max(0, 74 - len(title)))


def load(path: Path) -> list[dict]:
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


# ═══════════════════════════════════════════════════════════════════════════
# 1. P1：现存 org 层在新规矩下的成色
# ═══════════════════════════════════════════════════════════════════════════
def audit_existing_org() -> list[dict]:
    rule("1 · P1 诊断：现存 org 层在新规矩下的成色")
    from shared.lib.kb_schema import org_provenance_errors

    claims = load(ORG / "kb_claims.jsonl")
    chunks = load(ORG / "kb_chunks.jsonl")
    print(f"org 现存：{len(claims)} 条 claim，{len(chunks)} 条 chunk")

    with_prov = [c for c in claims if not org_provenance_errors(
        {**c, "scope": "org"})]
    print(f"\n带完整晋升出处的：{len(with_prov)} / {len(claims)}")
    if not with_prov:
        print(f"  → 一条都没有。这不是 bug，是 P1 要说的那件事：这 {len(claims)} 条")
        print("    全部从「写入时智能默认 scope=org」那条通道进来的，而那条通道")
        print("    已经封了。它们现在是**历史债**，不是可信资产。")

    sub("它们当年是从哪个项目来的（现在还查得到吗）")
    by_proj = Counter(str(c.get("source_project_id") or c.get("project_id") or "—")
                      for c in claims)
    for proj, n in by_proj.most_common(6):
        print(f"  {n:>4}  {proj}")
    if by_proj.most_common(1)[0][0] == "—":
        print("  → 主流是「查不到」。org 条目不知道自己从哪来 = 走不回证据。")
        print("    这正是 kb_provenance 会如实报 intact:false 的那一类。")
    return claims


# ═══════════════════════════════════════════════════════════════════════════
# 2. P5：类型收敛后老数据还看得见吗
# ═══════════════════════════════════════════════════════════════════════════
def audit_type_convergence(org_claims: list[dict]) -> None:
    rule("2 · P5 归一层：类型收敛后，老数据还进得了晋升分道吗")
    from shared.lib.kb_schema import CLAIM_TYPES, normalize_claim_type
    from core.kb_promotion import KIND_BY_CLAIM_TYPE

    all_claims = list(org_claims)
    files = sorted(PROJECTS.glob("*/kb_claims.jsonl"))
    for f in files:
        all_claims += load(f)
    print(f"全盘 claim（org + {len(files)} 个项目）：{len(all_claims)} 条")

    raw = Counter(str(c.get("claim_type") or "—") for c in all_claims)
    sub("盘上的原始类型分布")
    legacy_total = 0
    for t, n in raw.most_common():
        cur = normalize_claim_type(t)
        if t == "—":
            mark = "  （记录本身没有 claim_type）"
        elif t in CLAIM_TYPES:
            mark = ""
        else:
            mark = f"  → 归一为 {cur}"
            legacy_total += n
        print(f"  {n:>4}  {t:<18}{mark}")

    print(f"\n旧类型共 {legacy_total} 条（{legacy_total / len(all_claims):.0%}）。")
    sub("归一层救回了多少条")
    blind_raw = sum(n for t, n in raw.items() if t not in KIND_BY_CLAIM_TYPE)
    after = Counter(normalize_claim_type(str(c.get("claim_type") or ""))
                    for c in all_claims)
    blind_after = sum(n for t, n in after.items() if t not in KIND_BY_CLAIM_TYPE)
    by_design = sum(n for t, n in after.items()
                    if t in CLAIM_TYPES and t not in KIND_BY_CLAIM_TYPE)
    print(f"  归一前进不了分道：{blind_raw:>4} 条")
    print(f"  归一后进不了分道：{blind_after:>4} 条")
    print(f"    其中 {by_design} 条是**设计内不晋升**"
          f"（hypothesis / synthesis 是项目内的思考与记账，不是跨项目知识）")
    print(f"  → 归一层救回 {blind_raw - blind_after} 条：它们本来会因为类型改名而")
    print(f"    从候选清单里静默消失 —— 不报错，只是再也不出现。")


# ═══════════════════════════════════════════════════════════════════════════
# 3+4. P2：真项目终态扫盘 + 知识卡质量
# ═══════════════════════════════════════════════════════════════════════════
def replay_terminal_scan() -> None:
    rule("3 · P2 终态扫盘：拿真项目跑晋升管线")
    from core import kb_promotion

    for proj_dir in sorted(PROJECTS.glob("*")):
        claims = load(proj_dir / "kb_claims.jsonl")
        if len(claims) < 8:
            continue
        sub(f"项目 {proj_dir.name}（{len(claims)} 条 project claim）")
        state = State.new(node_type="_curator",
                          base_dir=WORK / "_replay_runs" / proj_dir.name,
                          project_id=proj_dir.name)

        checks = {
            "terminal_batch": kb_promotion.check_terminal_batch(state),
        }
        for name, chk in checks.items():
            status = "通过" if chk.passed else "拦下"
            print(f"  [{status}] {name}：{chk.reason}")

        types = Counter(str(c.get("claim_type") or "—") for c in claims)
        from shared.lib.kb_schema import normalize_claim_type
        lanes = Counter()
        for c in claims:
            kind = kb_promotion.KIND_BY_CLAIM_TYPE.get(
                normalize_claim_type(str(c.get("claim_type") or "")))
            lanes[kb_promotion.LANE_BY_KIND.get(kind, "（无归属）")] += 1
        print(f"  类型：{dict(types)}")
        print(f"  分道：{dict(lanes)}")


def inspect_card_quality() -> None:
    rule("4 · 知识卡质量：六个字段在真数据上填得出来吗")
    from core import kb_promotion
    from shared.lib.kb_schema import normalize_claim_type

    pool: list[tuple[str, dict]] = []
    for f in sorted(PROJECTS.glob("*/kb_claims.jsonl")):
        for c in load(f):
            pool.append((f.parent.name, c))

    print(f"候选池：{len(pool)} 条真实 project claim")
    print(f"知识卡字段：{kb_promotion.KNOWLEDGE_CARD_FIELDS}")

    sub("把真 project claim 原样当知识卡投进去，会被哪一条拦下")
    by_cause = Counter()
    samples: dict[str, tuple] = {}
    clean = []
    for proj, c in pool:
        draft = {"statement": c.get("claim_text") or "",
                 "why": c.get("rationale") or "",
                 "applicability": c.get("scope_dimensions") or {}}
        chk = kb_promotion.check_deprojectified(draft)
        if chk.passed:
            clean.append((proj, c))
            continue
        by_cause[chk.cause] += 1
        samples.setdefault(chk.cause, (proj, c, chk))

    print(f"  原样够格：{len(clean)} / {len(pool)}")
    for cause, n in by_cause.most_common():
        print(f"  被拦 · {cause}：{n} 条")
    print("\n  → 这正是「晋升是**改写**不是搬运」的机械依据：project claim 是")
    print("    一句观察，知识卡要求 why（机制）/ practice（据此怎么做）/")
    print("    confidence_basis（凭什么信）。缺 why 的东西是数据不是知识 ——")
    print("    这三个字段填不出来，说明这条还没被想清楚到能讲给外人听。")

    print("\n  各类失败的真实样本：")
    for cause, (proj, c, chk) in samples.items():
        text = (c.get("claim_text") or "")[:86].replace("\n", " ")
        ctype = normalize_claim_type(str(c.get("claim_type") or ""))
        print(f"    · [{cause}] [{ctype}] {text}…")
        print(f"      {chk.reason[:130]}")

    if clean:
        print("\n  原样够格的样本：")
        for proj, c in clean[:3]:
            text = (c.get("claim_text") or "")[:86].replace("\n", " ")
            print(f"    · {text}…")


# ═══════════════════════════════════════════════════════════════════════════
# 5. P3：新项目开题真能收到什么
# ═══════════════════════════════════════════════════════════════════════════
def replay_orientation() -> None:
    rule("5 · P3 送达：新项目开题时，机械注入的到底是什么")
    from core.org_delivery import (
        org_orientation, dead_end_flags, biblio_hits,
        INJECT_MAX_FINDINGS, INJECT_MAX_DEAD_ENDS,
    )

    state = State.new(node_type="hypothesis",
                      base_dir=WORK / "_replay_runs" / "_orient",
                      project_id="brand_new_project")

    text = org_orientation(state)
    print(f"预算上限：结论 {INJECT_MAX_FINDINGS} 条 / 死路 {INJECT_MAX_DEAD_ENDS} 条（常数，不随 org 规模变）")
    if text is None:
        print("\n注入内容：None")
        print("  → org 层 227 条里**一条都送不出去**。这不是 bug，是设计预期：")
        print("    送达面只认 org_kind（verified_finding / dead_end / recipe …），")
        print("    那是晋升管线给知识卡打的标。历史债条目没有这个字段，")
        print("    所以它们对新项目是不可见的 —— 沉在账本里，不占注入面。")
        print("    「本组没读过这个方向」本身是信息，函数不编造。")
    else:
        print(f"\n注入 {len(text)} 字符（约 {len(text) // 3} tokens）：\n")
        print(text[:2600])

    sub("reviewer 死路红旗（机械比对计划正文）")
    plan = ("我们打算用 LAMMPS 跑 Lennard-Jones 液体的快速淬火，"
            "用单次 100 ps 的降温轨迹直接读出玻璃转变温度 Tg。")
    flags = dead_end_flags(state, plan)
    print(f"  计划：{plan}")
    print(f"  命中死路：{len(flags)} 条")
    for fl in flags[:4]:
        print(f"    ⚑ {fl['trigger']} → {str(fl['warning'])[:110]}")
    if not flags:
        print("    （无命中。判据保守设计：触发词全中才算 —— 误报会让人学会")
        print("     忽略红旗，那比没有红旗更糟。召回率取决于 trigger 写得好不好，")
        print("     这是 RFC §18.5 如实标注的风险点。）")

    sub("书目复用（literature 检索前先撞脊柱）")
    chunks = load(ORG / "kb_chunks.jsonl")
    anchors = [str(c.get("source") or "") for c in chunks if c.get("source")][:3]
    if anchors:
        hits = biblio_hits(state, tuple(anchors))
        print(f"  拿 org 里真实存在的 {len(anchors)} 个锚点回查 → 命中 {len(hits)} 个")
        for a, h in list(hits.items())[:2]:
            print(f"    · {a[:70]} → {h['text'][:80]}…")
    else:
        print("  org 里没有带锚点的 chunk")


# ═══════════════════════════════════════════════════════════════════════════
# 6. P4：dreaming 在真 org 上找出了什么
# ═══════════════════════════════════════════════════════════════════════════
def replay_dreaming() -> None:
    rule("6 · P4 dreaming：在真 org claim 上找出了什么")
    from core import org_dreaming, org_canon

    state = State.new(node_type="_curator",
                      base_dir=WORK / "_replay_runs" / "_dream",
                      project_id="_org_maintenance")

    report = org_dreaming.run_dreaming(state, now_iso="2026-08-21T00:00:00Z")
    for key, items in report.items():
        if not isinstance(items, list):
            print(f"\n{key}: {items}")
            continue
        print(f"\n{key}：{len(items)} 项")
        for it in items[:3]:
            line = json.dumps(it, ensure_ascii=False)[:220]
            print(f"    · {line}")

    sub("正典层现状（P4b）")
    domains = org_canon.survey_domains(state)
    print(f"  域数：{len(domains)}（阈值 {org_canon.CANON_REFRESH_THRESHOLD} 条待吸收触发刷新）")
    for ds in domains[:6]:
        d = ds.as_dict()
        print(f"    · {d['domain']:<16} 待吸收 {len(d['pending']):>4}  "
              f"已吸收 {d['absorbed']:>3}  需刷新={d['needs_refresh']}")
    jobs = org_canon.refresh_jobs(state)
    print(f"  刷新作业：{len(jobs)} 个")
    for j in jobs[:3]:
        if j["kind"] == "unfiled_backlog":
            print(f"    · [{j['kind']}] {j['count']} 条没有 domain")
            print(f"      {j['instruction'][:170]}")
            print("      → 这一条是本次真实数据回放**逼出来的**：改之前它是一条")
            print("        canon_refresh，要 curator 把 224 条 LAMMPS + 元胞自动机 +")
            print("        MLIP 的结论写进同一篇 1–3 页综述。域不填就没有送达地址。")
        else:
            print(f"    · [{j['kind']}] 域 {j['domain']}：吸收 {len(j['absorb'])} 条")
            print(f"      {j['instruction'][:150]}")


def audit_domain_registry() -> None:
    rule("7 · 域注册表：存量条目离「有地址」有多远")
    from core.domain_registry import spine_categories, validate_domain

    claims = load(ORG / "kb_claims.jsonl")
    print(f"骨架分类：{len(spine_categories())} 个（arXiv 词表内置）")
    with_domain = [c for c in claims if str(c.get("domain") or "").strip()]
    print(f"org 存量带 domain 字段的：{len(with_domain)} / {len(claims)}")
    valid = sum(1 for c in with_domain
                if validate_domain(None, str(c["domain"])).ok)
    if with_domain:
        print(f"  其中注册表合法的：{valid}")
    print("  → 补域走 unfiled_backlog + suggest_from_evidence（查得到给建议、"
          "查不到不猜），人批确认。")


def main() -> None:
    bootstrap()
    print(f"数据源：{_SRC}")
    print(f"副本：  {WORK}（本脚本只读副本，绝不写回原处）")
    org_claims = audit_existing_org()
    audit_type_convergence(org_claims)
    replay_terminal_scan()
    inspect_card_quality()
    replay_orientation()
    replay_dreaming()
    audit_domain_registry()
    rule("回放结束")
    if OPTS.keep:
        print(f"副本保留在 {WORK}")
    else:
        shutil.rmtree(WORK, ignore_errors=True)


if __name__ == "__main__":
    main()

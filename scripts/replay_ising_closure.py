"""复利闭环首证：Ising 终态项目重建 → 晋升 → 新课题开题受益。

## 这个脚本要证明什么

两层重构的沉淀端和送达端各自验过，但**「项目 A 晋升 → 项目 B 开题受益」
全程零次发生**。没有这次测量，两层架构只是一个更整洁的抽屉。

## 材料是真的

`deliverables/e2e_v26_ising_tc_20260813/` 是平台 2026-08-13 真跑出来的课题：
Wolff 团簇 + Binder 累积量有限尺度标度复现 Onsager 精确解。产物本来就是
harness 的 artifact JSON 格式（带真 provenance / run_id / metadata），
直接重建即可，不编造内容。

三条知识全部来自该项目的**真实结论**（论文摘要 + 实验日志逐字）：

  recipe   预注册必须显式指定 FSS 拟合的加权方案 —— 只有 4 个交叉点时，
           加权 vs 不加权把判决从 refuted(2.8σ) 翻成 supported(1.17σ)
  recipe   Binder 累积量两套约定（U_L vs U'，定点处 U'=1.5·U_L）；引用文献
           阈值区间时必须写明是哪套
  dead_end 160M 团簇翻转按 2 小时估算算力，实际 8.85 小时（4.4×）

## 安全

跑在临时 home 上，绝不碰真 KB。

    python scripts/replay_ising_closure.py
    python scripts/replay_ising_closure.py --keep
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DELIVERABLE = REPO.parent / "deliverables" / "e2e_v26_ising_tc_20260813"

_ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
_ap.add_argument("--deliverable", default=str(DELIVERABLE))
_ap.add_argument("--keep", action="store_true", help="保留重建出的 home")
OPTS = _ap.parse_args()

SRC = Path(OPTS.deliverable)
if not SRC.exists():
    sys.exit(f"找不到交付物目录 {SRC}")

HOME = Path(tempfile.mkdtemp(prefix="ising-closure-"))
os.environ["HARNESS_FRAMEWORK_HOME"] = str(HOME)
os.environ.setdefault("HARNESS_DISABLE_SEMANTIC_DEDUP", "1")
sys.path.insert(0, str(REPO))

from core.bootstrap import bootstrap                      # noqa: E402
from core.state import State                              # noqa: E402
from core.tool_registry import execute as tool            # noqa: E402

APPROVER = "wangd"
AT = "2026-08-21T12:00:00Z"
PROJECT = "ising-tc-2026"


def rule(t: str) -> None:
    print(f"\n{'═' * 78}\n  {t}\n{'═' * 78}")


def sub(t: str) -> None:
    print(f"\n── {t} " + "─" * max(0, 74 - len(t)))


def load_artifact_json(name: str) -> dict:
    return json.loads((SRC / name).read_text(encoding="utf-8"))


# ═══════════════════════════════════════════════════════════════════════════
# 1. 重建终态项目
# ═══════════════════════════════════════════════════════════════════════════
def rebuild(state: State) -> dict[str, str]:
    rule("1 · 重建 Ising 终态项目（真产物，不编造）")
    ids: dict[str, str] = {}

    for fname, kind in (
        ("experiment_log__2D_Ising_Tc_ExperimentLog.json", "experiment_log"),
        ("clean_results__2D_Ising_Tc_CleanResults.json", "clean_results"),
    ):
        rec = load_artifact_json(fname)
        saved = state.save_artifact(
            kind, rec["name"], rec["content"],
            metadata={**(rec.get("metadata") or {}), "frozen": True,
                      "reconstructed_from": fname},
        )
        ids[kind] = saved["id"]
        print(f"  ✓ {kind:16} {saved['id']}  ({len(rec['content']):,} 字符，已冻结)")

    paper = (SRC / "paper_text.txt")
    if not paper.exists():
        import subprocess
        pdf = SRC / "paper_2D_Ising_Tc_v4_FINAL.pdf"
        try:
            text = subprocess.run(["pdftotext", str(pdf), "-"],
                                  capture_output=True, text=True, timeout=60).stdout
        except Exception:
            text = ""
    else:
        text = paper.read_text(encoding="utf-8")
    if not text.strip():
        text = load_artifact_json(
            "experiment_log__2D_Ising_Tc_ExperimentLog.json")["content"]
        print("  ! 论文正文取不到，用实验日志占位（终态判据只看冻结的 manuscript 存在）")
    ms = state.save_artifact("manuscript", "2D_Ising_Tc_Paper_v4_FINAL", text,
                             metadata={"frozen": True, "venue": "e2e v26",
                                       "reconstructed_from": "paper_..._FINAL.pdf"})
    ids["manuscript"] = ms["id"]
    print(f"  ✓ {'manuscript':16} {ms['id']}  ({len(text):,} 字符，已冻结)")

    from core import kb_promotion as kp
    chk = kp.check_terminal_batch(state)
    print(f"\n  终态检查：{'✅ 通过' if chk.passed else '❌ ' + chk.reason}")
    return ids


# ═══════════════════════════════════════════════════════════════════════════
# 2. 证据登记 + 项目 claim（真实结论）
# ═══════════════════════════════════════════════════════════════════════════
async def seed_knowledge(state: State, art: dict[str, str]) -> dict[str, str]:
    rule("2 · 登记证据链 + 写下项目结论（内容逐字来自论文与实验日志）")

    chunks: dict[str, str] = {}
    for kind, snippet in (
        ("experiment_log",
         "An unweighted fit yields Tc = 2.269287 ± 0.000087 (1.17σ), demonstrating "
         "that the verdict is sensitive to the weighting scheme when only four "
         "crossing pairs are available."),
        ("clean_results",
         "binder_convention_note: Our U_L -> 2/3 (low-T), 0 (high-T). Literature "
         "U' = 0.5*(3-<m^4>/<m^2>^2) -> 1 (low-T), 0 (high-T). U' = 1.5*U_L at "
         "fixed point."),
    ):
        rec, _ = state.write_kb("chunks", {
            "text": snippet, "source": f"artifact:{art[kind]}",
            "origin_artifact_id": art[kind], "scope": "project",
        })
        chunks[kind] = rec["id"]
        print(f"  ✓ chunk ← {kind}: {rec['id']}")

    claims: dict[str, str] = {}
    specs = [
        ("weighting", "methodological",
         "只有 4 个交叉点的 Binder 累积量 FSS 外推，加权与不加权最小二乘会给出"
         "互相矛盾的判决：加权 T_c=2.268776±0.000148（2.8σ，证伪），"
         "不加权 T_c=2.269287±0.000087（1.17σ，支持）。",
         [chunks["experiment_log"]]),
        ("convention", "methodological",
         "Binder 累积量有两套约定：U_L = 1 − ⟨m⁴⟩/(3⟨m²⟩²)（低温趋 2/3）与文献"
         "常用 U' = ½(3 − ⟨m⁴⟩/⟨m²⟩²)（低温趋 1），定点处 U' = 1.5·U_L。"
         "本次实测 U_L*=0.612150 换算 U'=0.918226，落在文献区间 [0.910, 0.922] 内。",
         [chunks["clean_results"]]),
        ("budget", "dead_end",
         "把 160M 次团簇翻转（5 尺寸 × 32 温度点 × 5 种子 × 10⁵ 测量）的算力"
         "预算估成 2 小时。实际 8.85 小时 —— 16 核上约 5,000 flips/s。",
         [chunks["experiment_log"]]),
    ]
    for key, ctype, text, srcs in specs:
        rec = {"claim_text": text, "claim_type": ctype, "scope": "project",
               "concept_ids": [], "orphan_reason": "本项目未注册受控词条",
               "sources": srcs, "confidence": 0.85,
               "produced_by_experiment_id": "exp_" + "1786637133"[:12]}
        if ctype == "dead_end":
            rec["dont_repeat_reason"] = (
                "团簇算法的单位代价被系统性低估：Wolff 每次翻转的期望团簇尺寸"
                "在临界点附近随 L 增长，按小格点外推大格点必然低估。")
        c, _ = state.write_kb("claims", rec)
        claims[key] = c["id"]
        print(f"  ✓ claim[{ctype:15}] {c['id']}  {text[:44]}…")
    return claims


# ═══════════════════════════════════════════════════════════════════════════
# 3. 起草知识卡（curator 的动作）
# ═══════════════════════════════════════════════════════════════════════════
async def draft_cards(state: State, claims: dict[str, str]) -> None:
    rule("3 · 起草知识卡：把项目内的一句观察改写成讲给外人听的知识")

    cards = {
        "weighting": dict(
            domain="cond-mat.stat-mech",
            statement="有限尺度标度外推的拟合加权方案必须在预注册里显式指定 —— "
                      "交叉点少于 5 个时，加权与不加权可给出跨越证伪阈值的不同判决。",
            applicability={"method": "Binder cumulant FSS",
                           "regime": "crossing pairs ≤ 5",
                           "estimator": "weighted vs unweighted least squares"},
            why="加权最小二乘按 1/σ² 分配权重；交叉点少时单个高精度点主导截距，"
                "而该点自身的系统误差（有限尺度修正）未被计入误差棒，于是被过度信任。",
            practice="预注册的 falsifier 里写明拟合的加权方案与权重来源；"
                     "若两种方案给出不同判决，如实报告二者并说明选择依据，"
                     "不要事后挑一个通过的。",
            confidence_basis="单课题实测（2D Ising，Onsager 精确解对照）；"
                             "同一份数据两种拟合分别给出 2.8σ 与 1.17σ。",
            evidence=[],
        ),
        "convention": dict(
            domain="cond-mat.stat-mech",
            statement="引用 Binder 累积量的文献阈值区间前必须确认约定：常见两套定义"
                      "在定点处相差 1.5 倍（U' = 1.5·U_L）。",
            applicability={"quantity": "Binder cumulant U*",
                           "conventions": ["U_L = 1 - <m^4>/(3<m^2>^2)",
                                           "U' = 0.5*(3 - <m^4>/<m^2>^2)"]},
            why="两套定义的低温极限不同（2/3 与 1），文献报的 U*≈0.916 属于后者；"
                "拿前者的数值直接对照后者的区间会得到假阴性。",
            practice="预注册写阈值区间时连同约定一起写；实现里把换算写成显式"
                     "一行，不要留给读者推。",
            confidence_basis="单课题实测：U_L*=0.612150 换算 U'=0.918226 落在"
                             "文献区间 [0.910, 0.922]，未换算则落在区间外。",
            evidence=[],
        ),
    }
    cards["budget"] = dict(
        domain="cond-mat.stat-mech",
        statement="临界点附近的团簇蒙特卡洛算力预算不能按小格点线性外推 —— "
                  "Wolff 每次翻转的期望团簇尺寸随 L 增长，大格点的单位代价被系统低估。",
        applicability={"algorithm": "Wolff / Swendsen-Wang cluster",
                       "regime": "near criticality", "L_max": 256},
        why="临界点附近关联长度发散，团簇尺寸随之增长；按小 L 测得的 flips/s "
            "外推到大 L 会低估总时长。",
        practice="按最大格点实测 flips/s 再乘总翻转数估预算，并留 3–5 倍余量；"
                 "预注册里的算力预算写成区间而不是点值。",
        confidence_basis="单课题实测：160M 次翻转按 2 小时估，实际 8.85 小时（4.4×）。",
        evidence=[],
        trigger="团簇 温度 尺寸 预算 小时",
        cost_when_hit="8.85 小时算力（预注册估 2 小时，超 4.4 倍）",
    )

    for key, card in cards.items():
        res = await tool("draft_knowledge_card", state, claim_id=claims[key], **card)
        mark = "✓" if res.get("status") == "success" else "✗"
        print(f"  {mark} {key}: {res.get('status')}  {str(res.get('error') or res.get('note'))[:88]}")


# ═══════════════════════════════════════════════════════════════════════════
# 4. 终态扫盘 → 人批 → 晋升
# ═══════════════════════════════════════════════════════════════════════════
async def promote_all(state: State) -> list[str]:
    rule("4 · 终态扫盘 → 三查 → 晋升（人批由本脚本代表 wangd 背书）")
    from core import kb_promotion as kp

    scan = kp.promotion_scan(state)          # 草稿从盘上自己捡
    print(f"  终态：{scan['terminal']}")
    print(f"  机械直落车道：{len(scan['mechanical'])} 条")
    print(f"  人批车道：    {len(scan['human_batch'])} 条")
    if scan["blocked"]:
        sub("被拦下的（「还差什么」显式可见）")
        for b in scan["blocked"][:5]:
            print(f"    · {b['source_id']}: {b['blocking']}")

    org_ids = []
    for cand in scan["mechanical"] + scan["human_batch"]:
        res = kp.promote(state, cand, project_id=PROJECT,
                         approved_by=APPROVER, at=AT)
        if res.get("status") == "success":
            org_ids.append(res["org_id"])
            print(f"  ✓ 晋升 {cand.kind:17} {cand.source_id} → {res['org_id']}"
                  f"（随行证据 {len(res['written']) - 1} 条）")
        else:
            print(f"  ✗ {cand.source_id}: {res.get('blocking') or res.get('error')}")
    return org_ids


# ═══════════════════════════════════════════════════════════════════════════
# 5. 新课题开题 —— 复利在这里兑现或落空
# ═══════════════════════════════════════════════════════════════════════════
def new_project_benefit() -> None:
    rule("5 · 新课题开题：机械注入送到了什么")
    from core.org_delivery import org_orientation, dead_end_flags

    fresh = State.new(node_type="hypothesis", base_dir=HOME / "_new",
                      project_id="xy-model-kt-2026")
    print("  新课题：二维 XY 模型的 Kosterlitz–Thouless 转变温度（同域，不同体系）\n")

    text = org_orientation(fresh, domain="cond-mat.stat-mech")
    if not text:
        print("  ❌ 注入为空 —— 复利没兑现")
        return
    print(f"  注入 {len(text)} 字符（约 {len(text)//3} tokens）：\n")
    print("\n".join("  │ " + ln for ln in text.splitlines()))

    sub("reviewer 死路红旗：新课题的计划撞不撞已知的坑")
    plan = ("我们打算用团簇算法扫 32 个温度点、5 个尺寸、每点 5 个独立种子，"
            "预算 2 小时算力，用 Binder 累积量交叉点做有限尺度标度外推。")
    flags = dead_end_flags(fresh, plan)
    print(f"  计划：{plan}")
    print(f"  命中：{len(flags)} 条")
    for f in flags:
        print(f"    ⚑ {f['warning'][:150]}")


# ═══════════════════════════════════════════════════════════════════════════
def measure(st: State, org_ids: list[str]) -> None:
    rule("6 · 闭环测量")
    org = [r for r in st.list_kb("claims") if r.get("scope") == "org"]
    biblio = [r for r in st.list_kb("chunks") if r.get("scope") == "org"]
    print(f"  org 新增：{len(org)} 条知识卡 + {len(biblio)} 条随行证据")
    for r in org:
        prov = r.get("promoted_from") or {}
        print(f"\n  ▸ [{r.get('org_kind')}] domain={r.get('domain')}")
        print(f"    {(r.get('statement') or r.get('claim_text') or '')[:150]}")
        print(f"    出处：{prov.get('project_id')} / {prov.get('source_id')}"
              f" / 批准 {prov.get('approved_by')}")

    sub("出处链走查 A：源项目内（curator 晋升后的真实上下文）")
    from core.kb_provenance import kb_provenance
    for r in org:
        res = kb_provenance(st, r["id"])
        mark = "✅ intact" if res.get("intact") else f"❌ 断链 {res.get('broken')}"
        print(f"  {mark}  {r['id']}（{len(res.get('hops') or [])} 跳）")

    sub("出处链走查 B：**别的项目**（org 卡真正要服务的读者）")
    outsider = State.new(node_type="writing", base_dir=HOME / "_outsider",
                         project_id="xy-model-kt-2026")
    for r in org:
        res = kb_provenance(outsider, r["id"])
        hops = res.get("hops") or []
        crossed = [h for h in hops
                   if h.get("kind") == "artifact"
                   and "源项目" in str(h.get("summary") or "")]
        mark = "✅ intact" if res.get("intact") else f"❌ 断链 {res.get('broken')}"
        print(f"  {mark}  {r['id']}（{len(hops)} 跳，其中跨项目证据锚 {len(crossed)} 个）")
        for h in crossed[:1]:
            print(f"      ▸ {h['summary']}")
            print(f"        {h.get('detail')}")


async def main() -> None:
    bootstrap()
    print(f"交付物：{SRC}")
    print(f"重建到：{HOME}（临时 home，不碰真 KB）")
    state = State.new(node_type="_curator", base_dir=HOME / "_build",
                      project_id=PROJECT)
    art = rebuild(state)
    claims = await seed_knowledge(state, art)
    await draft_cards(state, claims)
    org_ids = await promote_all(state)
    measure(state, org_ids)
    new_project_benefit()
    sub("效果传感器：这一趟注入被记下来了吗")
    from core.org_usage import usage_report

    rep = usage_report(State.new(node_type="_curator", base_dir=HOME / "_sensor",
                                 project_id=PROJECT))
    if not rep:
        print("  ❌ 账本是空的 —— 注入没被记账，老化复查又要按日历猜了")
    for oid, u in rep.items():
        print(f"  · {oid}  注入 {u.injected} 次 → {list(u.injected_into)}"
              f"｜引用 {u.cited} 次")
    print("\n  → 「送出去了没人引用」和「从没被送出去」现在是两类作业："
          "\n    前者该重写或降级（卡的问题），后者原样留着（域休眠）。")

    rule("闭环结束")
    if OPTS.keep:
        print(f"重建的 home 保留在 {HOME}")
    else:
        shutil.rmtree(HOME, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())

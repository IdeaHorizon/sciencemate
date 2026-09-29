"""org 知识的送达 —— 沉淀只是回路的一半。

## 为什么送达必须机械化

「只要还需要模型主动去查，它就有一半概率不查」—— 这是实测出来的，不是猜的
（契约不查、validate 不调、reviewer 猜 artifact id 猜出 404）。org KB 若只提供
`search_kb`，它就是个没人再看的抽屉：本机实测 org 层 72 条沉积物，
零机械消费路径。

所以复利不押注在模型的主动性上。三个注入点全部由框架触发：

  开题注入      新项目立问题之前，先摆出"本组已知什么"
  reviewer 红旗 审查时机械比对 org 死路 —— 是免疫系统，不是图书馆
  书目复用      literature 检索先撞书目脊柱，已读过的直接取读后结论

这三条 + 终态沉淀，才构成飞轮。缺送达侧，org KB 退化成一个 RAG 库
（RFC §12.1/§18.4）。

## 预算

任何 org 读取的输出量必须与 org 总量无关（「KB 有界读取原则」：
dreaming 一条 search_kb 顶爆 88k context 的教训）。这里全部走
"命中上限 + 单条截断"，且上限是常数不是比例。
"""
from __future__ import annotations

from typing import Any

#: 开题注入的预算（RFC §12.1 待拍板第 3 项的实现值）。
#: 常数上限，不随 org 规模变 —— 三年后 org 有一万条，注入量还是这些。
INJECT_MAX_FINDINGS = 10
INJECT_MAX_RECIPES = 8
INJECT_MAX_DEAD_ENDS = 5
INJECT_MAX_OPEN_QUESTIONS = 5
INJECT_MAX_EXEMPLARS = 2
#: 单条摘要截断（整段注入的软上限约 3k tokens）
SNIPPET_CHARS = 220
#: 活综述的注入截断 —— 全文用 read_artifact 取，注入面只放开头
CANON_SNIPPET_CHARS = 2000

ORG_ORIENTATION_PREFIX = "📚 **本组已知**"


def org_orientation(state: Any, *, domain_terms: tuple[str, ...] = (),
                    domain: str | None = None) -> str | None:
    """开题注入：把 org 的相关知识摆到新项目面前。

    `domain_terms` 为空时不做主题过滤，按新近度取 —— 冷启动阶段
    （org 还很小）这是对的；org 大了之后由正典树按域命中（P4）。

    返回 None 表示 org 层没有可送达的东西 —— **这本身是信息**（"本组没读过
    这个方向"），由调用方决定要不要如实说出来，本函数不编造。
    """
    # 正典优先：成熟 org KB 主要通过活综述被阅读，卡片按需下钻。
    # 新项目开题不该读 400 条原子 claim，该读 2 页领域综述（RFC §8/§18.6）。
    canon_body = None
    if domain:
        try:
            from core.org_canon import canon_for_domain

            hit = canon_for_domain(state, domain)
            canon_body = (hit or {}).get("body") or None
        except Exception:
            canon_body = None

    findings = _org_records(state, kind="verified_finding", terms=domain_terms,
                            limit=INJECT_MAX_FINDINGS,
                            skip_absorbed=bool(canon_body))
    # 方法配方是跨项目复利最强的一类（"别人怎么做的"比"别人发现了什么"更可搬）。
    # 2026-08-21 Ising 闭环回放实测：晋升产出 3 条卡，2 条是 recipe，而注入面
    # 只读 verified_finding / dead_end —— 67% 的价值产出即黑洞。
    recipes = _org_records(state, kind="recipe", terms=domain_terms,
                           limit=INJECT_MAX_RECIPES,
                           skip_absorbed=bool(canon_body))
    dead_ends = _org_records(state, kind="dead_end", terms=domain_terms,
                             limit=INJECT_MAX_DEAD_ENDS)
    if not findings and not recipes and not dead_ends and not canon_body:
        return None

    lines = [f"{ORG_ORIENTATION_PREFIX}（机械注入，来自组织知识库）", ""]

    if canon_body:
        lines += [f"**领域活综述 · {domain}**", "",
                  canon_body[:CANON_SNIPPET_CHARS]
                  + ("…（用 read_artifact 取全文）"
                     if len(canon_body) > CANON_SNIPPET_CHARS else ""),
                  ""]

    if findings:
        lines.append("**验证结论**")
        for rec in findings:
            lines.append(f"  • [{rec.get('id')}] {_snippet(rec)}")
            basis = str(rec.get("confidence_basis") or "").strip()
            rc = int(rec.get("replication_count") or 0)
            if basis or rc > 1:
                lines.append(f"    凭据：{basis or '—'}"
                             + (f"（跨项目复现 {rc} 次）" if rc > 1 else ""))
            applic = rec.get("applicability")
            if isinstance(applic, dict) and applic:
                lines.append(f"    适用：{_compact(applic)}")
        lines.append("")

    if recipes:
        lines.append("**方法配方（本组趟过的做法）**")
        for rec in recipes:
            lines.append(f"  • [{rec.get('id')}] {_snippet(rec)}")
            practice = str(rec.get("practice") or "").strip()
            if practice:
                lines.append(f"    据此该怎么做：{practice[:SNIPPET_CHARS]}")
            why = str(rec.get("why") or "").strip()
            if why:
                lines.append(f"    机制：{why[:160]}")
        lines.append("")

    if dead_ends:
        lines.append("**死路（别再走一遍）**")
        for rec in dead_ends:
            lines.append(f"  • [{rec.get('id')}] {_snippet(rec)}")
            cost = rec.get("cost_when_hit")
            if cost:
                lines.append(f"    上次代价：{_compact(cost)}")
        lines.append("")

    lines.append("以上是**组织资产**，每条可用 get_kb_record(方括号里的 id) 取全文与证据链。"
                 "与你的课题冲突时，冲突本身值得记下来 —— 那是开放问题，不是错误。"
                 "如果你**手里的证据**表明其中一条不成立或已有更好的说法，用 "
                 "propose_org_correction(org_id=…, verdict=refuted|superseded, reason=…, "
                 "evidence_ids=[…]) 交给组织的管理员裁 —— 组织的知识不删，只被推翻或取代。")

    # 记一笔：这些卡被送到了哪个项目面前。人批只是入口质量，卡的最终判据是
    # 「下游用了它，并且没被坑」—— 不记这一笔，老化复查就只能按日历猜。
    try:
        from datetime import datetime, timezone

        from core.org_usage import record_injection

        record_injection(
            state,
            [str(r.get("id")) for r in (findings + recipes + dead_ends)],
            project_id=str(getattr(state, "project_id", "") or ""),
            at=datetime.now(timezone.utc).isoformat(),
        )
    except Exception:
        pass          # 传感器坏了不该让开题跑不起来

    return "\n".join(lines)


def dead_end_flags(state: Any, plan_text: str) -> list[dict]:
    """reviewer 红旗：这份计划有没有在重走已知的死路。

    机械比对 org 死路的**触发条件**与计划正文。判据保守（触发词全中才算），
    宁可漏也不要把 reviewer 变成噪音源 —— 误报会让人学会忽略红旗，
    那比没有红旗更糟。

    ⚠️ 已知局限：召回率取决于触发条件写得好不好。这是设计里诚实标注的
    风险点（RFC §12.2/§18.5），靠 dogfood 校准，不靠加词表。
    """
    text = (plan_text or "").lower()
    if not text:
        return []
    flags: list[dict] = []
    for rec in _org_records(state, kind="dead_end", limit=200):
        # trigger 是"什么样的计划会撞上这条死路"。它由起草者写（experiment 撞上
        # 死路的当时最清楚）；没写就退回卡片正文 —— 但正文的实词全中才算，
        # 判据比 trigger 更保守，宁可漏也不要把 reviewer 变成噪音源。
        trigger = str(rec.get("trigger") or "").strip()
        fallback = not trigger
        if fallback:
            trigger = str(rec.get("statement") or rec.get("claim_text") or "").strip()
        if not trigger:
            continue
        # 长度下限按**字符集**分别定：拉丁语的 3 字符门槛对中文是灾难性的 ——
        # 「团簇」「温度」「预算」都是 2 字，一刀切会把整条 trigger 过滤空，
        # 红旗于是一次都不响。2026-08-21 Ising 闭环回放实测到（与 dreaming 那次
        # CJK 分词漏修是同一类：判据里藏着拉丁语假设）。
        min_len = 4 if fallback else 3
        tokens = [t for t in _tokens(trigger) if _significant(t, min_len)]
        if fallback:
            tokens = tokens[:8]      # 正文太长时只取前几个实词，否则永不命中
        if tokens and all(t in text for t in tokens):
            flags.append({
                "org_id": rec.get("id"),
                "trigger": trigger,
                "matched_on": "trigger" if not fallback else "statement_fallback",
                "warning": rec.get("warning") or rec.get("claim_text") or "",
                "from_project": (rec.get("promoted_from") or {}).get("project_id"),
            })
    return flags


def biblio_hits(state: Any, anchors: tuple[str, ...]) -> dict[str, dict]:
    """书目复用：这些锚点本组读过没有。

    命中的直接返回"本组读后结论"，literature 就不必重爬重读 ——
    预算全部花在新论文上。
    """
    wanted = {a.strip().lower() for a in anchors if a and a.strip()}
    if not wanted:
        return {}
    out: dict[str, dict] = {}
    for rec in _org_records(state, entity="chunks", limit=2000):
        anchor = str(rec.get("source") or "").strip().lower()
        if anchor in wanted:
            out[anchor] = {
                "org_id": rec.get("id"),
                "text": _snippet(rec),
                "group_readings": rec.get("group_readings") or [],
            }
    return out


# ── 读取（全部有界）────────────────────────────────────────────────────────


def _org_records(state: Any, *, kind: str | None = None,
                 entity: str = "claims", terms: tuple[str, ...] = (),
                 limit: int = 20, skip_absorbed: bool = False) -> list[dict]:
    """读 org 层记录。**输出量与 org 总量无关** —— limit 是常数上限。

    `skip_absorbed`：已被正典吸收的卡片降权（**不是删除** —— 它们还在账本里，
    只是不再占注入面的位置，那个位置该留给综述没讲到的新东西）。
    """
    absorbed: set[str] = set()
    if skip_absorbed:
        try:
            from core.org_canon import _canon_index

            for entry in _canon_index(state).values():
                absorbed |= set(entry.get("absorbed_ids") or ())
        except Exception:
            absorbed = set()
    try:
        rows = state.list_kb(entity) or []
    except Exception:
        return []
    from core.org_corrections import in_force

    out: list[dict] = []
    for rec in rows:
        if not isinstance(rec, dict) or rec.get("scope") != "org":
            continue
        # 组织推翻 / 取代了的不再送达：不进开题、不响红旗、不当书目复用。
        # 账本里还在（组织页上看得到它为什么不作数），只是不再当「本组已知」。
        if not in_force(rec):
            continue
        if kind and rec.get("org_kind") != kind and rec.get("claim_type") != kind:
            continue
        if terms and not _matches(rec, terms):
            continue
        if absorbed and str(rec.get("id")) in absorbed:
            continue
        out.append(rec)
        if len(out) >= limit:
            break
    return out


def _matches(rec: dict, terms: tuple[str, ...]) -> bool:
    hay = " ".join(str(rec.get(f) or "") for f in
                   ("statement", "claim_text", "why", "trigger", "text")).lower()
    return any(t.lower() in hay for t in terms if t)


def _snippet(rec: dict) -> str:
    text = str(rec.get("statement") or rec.get("claim_text")
               or rec.get("text") or "").strip()
    return text[:SNIPPET_CHARS] + ("…" if len(text) > SNIPPET_CHARS else "")


def _compact(value: Any) -> str:
    if isinstance(value, dict):
        return "，".join(f"{k}={v}" for k, v in list(value.items())[:4])
    return str(value)[:120]


def _tokens(text: str) -> list[str]:
    import re

    return [t for t in re.split(r"[^\w一-鿿]+", text.lower()) if t]


def _significant(token: str, min_len: int) -> bool:
    """这个词够不够实 —— 长度下限按字符集分别定。

    一个 2 字中文词（团簇 / 温度 / 预算）承载的信息量约等于一个 5–6 字母的
    英文词；用同一个字符数门槛卡两者，等于把中文 trigger 全部过滤掉。
    """
    import re

    cjk = len(re.findall(r"[\u4e00-\u9fff]", token))
    if cjk:
        return cjk >= 2 if min_len <= 3 else cjk >= 3
    return len(token) >= min_len

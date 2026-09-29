"""晋升：project 工作记忆 → org 机构资产。org 唯一的入口。

## 为什么晋升是「转换」而不是「搬运」

project 条目的读者是**本项目**（上下文已加载，可以说"我们的 run"、带着 seed）；
org 条目的读者是**一个还不存在的、做别的课题的项目**。同一条内容，两个读者，
必须是两份文本。所以晋升做三件事：

  去项目化   剥掉项目内指代，把 scope_dimensions 的项目参数改写成**适用条件**
  自足化     读完即可用，不必解引用 —— org 卡是微型文档，不是指针
  写明 why   机制/解释。缺 why 的只是数据，不是知识

`why` 必填是**准入审查本身**：写不出机制的候选，多半还不到晋升的火候。
与"写不出去项目化版本就不该晋升"是同一道门的两问。

## 为什么在终态、按批

跨项目复利的判据（结论站没站住、证据冻没冻、能否泛化）**在过程中不存在**，
只有终态才具备。过程中零散上传就是拿"当时看着不错"当"对组织为真"。

## 两条车道

机械车道直落（书目 / 带证据的死路 / 与既有 org 条目匹配的复现归并）——
它们的判据是机械的，不需要人的判断；判断类（验证结论 / 方法配方 / 范例）
走人批。分层的理由是量：组级吞吐下人批约每周 5–15 条，可持续；
不分层则每年数千个批准决策，inbox 必死。

见 docs/RFC_KB_TWO_TIERS_20260820.md §15/§17/§19。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# ── 车道 ────────────────────────────────────────────────────────────────────

LANE_MECHANICAL = "mechanical"
LANE_HUMAN = "human_batch"

# ── org 条目种类 ────────────────────────────────────────────────────────────

KIND_BIBLIO = "biblio"
KIND_VERIFIED_FINDING = "verified_finding"
KIND_RECIPE = "recipe"
KIND_DEAD_END = "dead_end"
KIND_EXEMPLAR = "exemplar"

#: 哪种 org 条目走哪条车道。机械车道的共同点：判据不需要人的判断。
LANE_BY_KIND = {
    KIND_BIBLIO: LANE_MECHANICAL,          # 被采纳 claim 引用过的书目，客观
    KIND_DEAD_END: LANE_MECHANICAL,        # 带证据的失败，广播义务优先
    KIND_VERIFIED_FINDING: LANE_HUMAN,
    KIND_RECIPE: LANE_HUMAN,
    KIND_EXEMPLAR: LANE_HUMAN,
}

#: project claim_type → org 条目种类。没列出的类型不晋升
#: （hypothesis/synthesis 是项目内的思考与记账，不是跨项目知识）。
KIND_BY_CLAIM_TYPE = {
    "empirical": KIND_VERIFIED_FINDING,
    "methodological": KIND_RECIPE,
    "dead_end": KIND_DEAD_END,
}

#: org 知识卡的必填字段。`evidence` 是背书（证据链入口），其余是自足载荷。
KNOWLEDGE_CARD_FIELDS = (
    "domain",            # 归入哪个领域 —— 见下方"为什么域是必填"
    "statement",         # 结论本身（去项目化）
    "applicability",     # 什么条件下成立/不成立
    "why",               # 机制或解释 —— 缺它就只是数据不是知识
    "practice",          # 据此该怎么做 / 别怎么做
    "confidence_basis",  # 凭什么信（复现记数、证据强度）
    "evidence",          # 证据链入口
)

#: dead_end 卡的专属字段。不是全类必填 —— 但死路卡缺了它们就**送不出去**：
#: `trigger` 决定 reviewer 红旗匹配什么样的计划，`cost_when_hit` 是"上次代价"。
#: 由起草者写：撞上死路的当时最清楚什么样的计划会撞上它、代价是多少。
DEAD_END_CARD_FIELDS = ("trigger", "cost_when_hit")

# 为什么 `domain` 是必填（2026-08-21 真实数据回放的结论）：
#
# 正典层按域组织活综述，而 `org_canon.domain_of()` 明确**不猜** —— 猜错的分域
# 比没有分域更糟，它会让开题注入送错知识，而错误的"本组已知"比没有已知更有害
# （人会照着它走）。所以域只能由写卡的人指定。
#
# 在真 org 数据上回放暴露了不指定的后果：224 条全部落进 `unsorted`，正典层于是
# 提出「把这 224 条 LAMMPS + 元胞自动机 + MLIP 的结论吸收进一篇 1–3 页综述」——
# 一个机制存在、但在真数据上无法运作的状态。
#
# 填不进任何领域的卡片，是一张永远不会被读到的卡片。所以这不是元数据洁癖：
# 域是**送达的地址**，没有地址的知识不构成资产。

#: 项目内指代 —— 出现在 statement/why/practice 里就说明没去项目化。
#: 判据是**词面**，刻意保守：它只负责"明显没改写"这一档，
#: 真正的去项目化审查在人批那一关。
_PROJECT_DEIXIS = (
    "本项目", "本课题", "我们的 run", "这次 run", "本 run",
    "this project", "our run", "this run",
)

#: scope_dimensions 里的项目参数 —— 晋升时必须换成适用条件，不能原样带走。
PROJECT_SPECIFIC_DIMENSIONS = ("seed", "run_id", "project_id", "attempt_no")

#: 晋升时**不跨层**的字段：身份由内容寻址重算，出处另有 promoted_from，
#: 草稿区和项目内记账不属于 org，证据由闭包重建。
_NOT_INHERITED = frozenset({
    "id", "scope", "promoted_from", "org_kind",
    "card_draft", "card_draft_history",           # 草稿是项目内的活草稿
    "sources", "replication_count",               # 由闭包/归并重建
    "created_at", "updated_at", "created_by_role",
    "promoted_to_org_id", "revision_history",
    "produced_by_experiment_id", "tested_by_experiment_ids",  # 指向项目内实验
    "independent_source_count", "literature_reported",
    "prereg_chunk_id", "hypothesis_id", "prereg_artifact_id",  # 项目内承诺锚
})


def _inheritable_claim_fields() -> frozenset[str]:
    """哪些 canonical 字段随晋升带过去 —— 从 schema 现算，不手抄。

    手抄的名单会和 schema 各自演化，且分叉时不报错：schema 新增一个类型必填
    字段（比如 dead_end 的 dont_repeat_reason），手抄名单漏掉它，晋升就在那个
    类型上永久 fail-closed，而单测夹具往往不带该字段。
    """
    from shared.lib.kb_schema import canonical_fields

    return frozenset(canonical_fields("claims")) - _NOT_INHERITED


_INHERITABLE_CLAIM_FIELDS = _inheritable_claim_fields()


@dataclass
class Check:
    """三查里的一项。`passed=False` 时 `reason` 必须说清缺什么。

    `cause` 是**机读子因**。一道检查里可能有几种失败方式（比如 deprojectified
    既管卡片字段齐不齐，也管正文有没有项目指代）：只给一句人话 reason，
    调用方就会按检查的**名字**去理解失败原因，然后指向假原因。
    实测：本次 e2e 回放脚本读到 `deprojectified` 失败，就写了「含项目指代
    132/132」—— 实际全是卡片字段没填。这就是那个坑。
    """

    name: str
    passed: bool
    reason: str = ""
    cause: str = ""          # missing_card_fields / missing_applicability /
                             # project_deixis / leaked_dimensions / no_draft


@dataclass
class Candidate:
    """一条晋升候选。`draft` 只在人批车道需要（机械车道不需要改写）。"""

    source_id: str
    source_kind: str          # claims / chunks / artifacts
    kind: str                 # KIND_*
    lane: str
    checks: list[Check] = field(default_factory=list)
    evidence_closure: tuple[str, ...] = ()
    draft: dict[str, Any] | None = None

    @property
    def eligible(self) -> bool:
        return all(c.passed for c in self.checks)

    def blocking(self) -> list[str]:
        return [f"{c.name}: {c.reason}" for c in self.checks if not c.passed]


# ── 三查 ────────────────────────────────────────────────────────────────────


def check_terminal_batch(state: Any) -> Check:
    """项目到终态了吗 —— 判据是**冻结的交付物**，不是模型自述"我做完了"。

    终态信号：存在已冻结的 manuscript（论文类项目），或已冻结的 evidence_record
    且编排工作已闭环。两者都读盘上的事实。
    """
    frozen_manuscripts = [
        a for a in _artifacts(state, "manuscript") if _is_frozen(state, a)
    ]
    if frozen_manuscripts:
        return Check("terminal_batch", True)

    from shared.lib.artifact_policy import evidence_record_types

    for t in evidence_record_types():
        if any(_is_frozen(state, a) for a in _artifacts(state, t)):
            try:
                from core.closure import open_post_node_flows

                if open_post_node_flows(state):
                    return Check("terminal_batch", False,
                                 "还有未闭环的 post-producing flow")
            except Exception:
                pass
            return Check("terminal_batch", True)

    return Check("terminal_batch", False,
                 "项目未到终态：没有已冻结的 manuscript 或证据记录")


def check_evidence_frozen(state: Any, claim_id: str,
                          closure: tuple[str, ...]) -> Check:
    """这条断言的证据都冻了吗 —— 没冻的证据还会变，晋升上去就是断链。

    查的是**闭包里每个 chunk 的来源产物**，不是 claim.sources：
    `claim.sources` 按 schema 只能装 chunk_id / claim_id / 外部 URI
    （产物必须先登记成 chunk 才能被引用），所以"产物冻没冻"这个事实
    挂在 `chunk.origin_artifact_id` 上，不在 claim 那一层。
    """
    unfrozen = []
    for cid in closure:
        if not cid.startswith("chunk_"):
            continue
        chunk = _get_kb(state, "chunks", cid) or {}
        origin = str(chunk.get("origin_artifact_id") or "")
        if not origin:
            continue          # 外部文献 chunk 无来源产物，不适用
        art = _read_artifact(state, origin)
        if art is not None and not (art.get("metadata") or {}).get("frozen"):
            unfrozen.append(origin)
    if unfrozen:
        return Check("evidence_frozen", False,
                     f"证据来源产物未冻结：{sorted(set(unfrozen))}")
    return Check("evidence_frozen", True)


def check_deprojectified(draft: dict | None) -> Check:
    """能写出不含项目指代的版本吗 —— 写不出就不该晋升。

    这一查同时是**准入审查**：它检查的不是格式合规，是这条内容有没有被
    真正想清楚到能讲给外人听。
    """
    if not draft:
        return Check("deprojectified", False, "还没有去项目化草稿", "no_draft")

    missing = [f for f in KNOWLEDGE_CARD_FIELDS if not str(draft.get(f) or "").strip()
               and f != "applicability" and f != "evidence"]
    if missing:
        return Check("deprojectified", False, f"知识卡缺字段：{missing}",
                     "missing_card_fields")
    if not draft.get("applicability"):
        return Check("deprojectified", False, "缺 applicability（适用条件）",
                     "missing_applicability")

    # 域必须在注册表内 —— 自由文本的域会碎片化（「MLIP」vs「机器学习势」两个域
    # = 同一领域两篇综述各自演化）。registrable（骨架父合法的新叶）放行，
    # promote 时随人批一起注册。
    from core.domain_registry import validate_domain

    dv = validate_domain(None, str(draft.get("domain") or ""))
    if not dv.ok:
        return Check("deprojectified", False,
                     f"{dv.error} 最近匹配：{list(dv.suggestions)}",
                     "invalid_domain")

    prose = " ".join(str(draft.get(f) or "") for f in
                     ("statement", "why", "practice"))
    hits = [d for d in _PROJECT_DEIXIS if d in prose]
    if hits:
        return Check("deprojectified", False,
                     f"正文仍含项目内指代：{hits} —— 读者是别的课题的项目",
                     "project_deixis")

    leaked = [k for k in PROJECT_SPECIFIC_DIMENSIONS
              if k in (draft.get("applicability") or {})]
    if leaked:
        return Check("deprojectified", False,
                     f"applicability 里还带着项目参数：{leaked} —— "
                     "它们该被改写成适用条件，不是原样带走",
                     "leaked_dimensions")
    return Check("deprojectified", True)


# ── 证据闭包 ────────────────────────────────────────────────────────────────


def evidence_closure(state: Any, claim_id: str, *, _seen: set | None = None) -> tuple[str, ...]:
    """一条 claim 的传递证据闭包（它引的 chunk、chunk 引的…）。

    晋升必须**原子地**带走整个闭包 —— 否则源项目归档后，org 里挂着断链的
    "真理"：结论还在，支撑它的东西没了，谁也没法回验。
    """
    seen = _seen if _seen is not None else set()
    if claim_id in seen:
        return ()
    seen.add(claim_id)
    out: list[str] = []
    rec = _get_kb(state, "claims", claim_id) or _get_kb(state, "chunks", claim_id)
    for src in (rec or {}).get("sources") or []:
        sid = str(src)
        if sid.startswith("chunk_"):
            if sid not in seen:
                seen.add(sid)
                out.append(sid)
                out.extend(evidence_closure(state, sid, _seen=seen))
        elif sid.startswith("claim_"):
            out.append(sid)
            out.extend(evidence_closure(state, sid, _seen=seen))
    return tuple(dict.fromkeys(out))


# ── 范例预筛 ────────────────────────────────────────────────────────────────
#
# 三关（RFC §19.2），**没有一关是 LLM-judge**：
#   1. 机械预筛（这里）：门禁全过 + 预注册问题诚实闭合 + 证据链干净
#   2. 人批：PI 点选。冷启动阶段组织品味 = 人的品味，这是 bootstrap 不是缺陷
#   3. 使用验证：追踪模仿者成绩，不比基线好就降位 —— 真正的裁判
#
# 为什么不用模型打分选范例：writing judge 掷硬币烧掉 143M tokens。
# 机械信号衡量的是**纪律**，不是重要性 —— 所以它只配出候选，不配定夺。

# 「哪类产物能当范例」是一条**类型性质**，声明在 shared.lib.artifact_policy
# （`exemplar_candidate`）。这里原来写了一张名单 —— 被那条扫盘护栏当场逮住，
# 它逮得对：注册表外的类型名单会随新类型默认漏过。


def exemplar_candidates(state: Any) -> list[dict]:
    """机械预筛出范例候选 —— 只出候选，不定夺。

    三条机械信号：

      **冻结**      范例是给人照着写的，必须是不会再变的那一版
      **诚实闭合**  预注册的问题都有裁决，且**证伪也算**（甚至更算）——
                    E2E v26 把自己的 supported 翻成 refuted，那份记录正是范例
      **无门禁欠账** 收尾时必需产出齐全

    刻意不打分、不排序：机械信号分不出"哪份更值得学"，那是人批那一关的事。
    """
    if not check_terminal_batch(state).passed:
        return []

    honest = _honest_closure_summary(state)
    out: list[dict] = []
    from shared.lib.artifact_policy import exemplar_candidate_types

    for a_type in exemplar_candidate_types():
        for entry in _artifacts(state, a_type):
            aid = str(entry.get("id") or "")
            rec = _read_artifact(state, aid)
            if not rec or not (rec.get("metadata") or {}).get("frozen"):
                continue
            out.append({
                "artifact_id": aid,
                "artifact_type": a_type,
                "signals": {
                    "frozen": True,
                    "no_failed_checks": True,
                    **honest,
                },
                # why_good 由人在批准时写 —— 它必须指向**决策**而不是格式
                # （"好在把定性判据写成了可观察条件"，不是"好在结构分五节"），
                # 机械层写不出这种句子，也不该假装能写。
                "why_good": None,
                "status": "awaiting_human_selection",
            })
    return out


def _honest_closure_summary(state: Any) -> dict:
    """预注册问题的闭合诚实度 —— 证伪与支持同等计入。

    这里刻意**不**把 refuted 算作减分：一个如实把自己的假设翻成 refuted 的
    项目，恰恰是最该被当范例的。把"结论好看"当范例判据，就是在教下一个
    项目粉饰。
    """
    verdicts = {"validated": 0, "refuted": 0, "open": 0}
    for rec in _list_kb(state, "claims"):
        if rec.get("claim_type") != "hypothesis":
            continue
        status = str(rec.get("status") or "")
        if status in ("validated", "refuted"):
            verdicts[status] += 1
        else:
            verdicts["open"] += 1
    decided = verdicts["validated"] + verdicts["refuted"]
    return {
        "hypotheses_decided": decided,
        "hypotheses_open": verdicts["open"],
        "includes_honest_refutation": verdicts["refuted"] > 0,
        "closure_honest": verdicts["open"] == 0 and decided > 0,
    }


# ── 扫盘 ────────────────────────────────────────────────────────────────────


def card_drafts(state: Any) -> dict[str, dict]:
    """盘上已起草的知识卡（claim_id → 卡）。

    草稿写在 claim 上，不是攒在调用方手里 —— 起草发生在**上下文热**的时候
    （项目进行中，"为什么当时这么判"还记得），落盘后终态扫盘自己捡起来。
    原来要求 curator 在终态一次性把所有卡片当参数传进来：一是那时上下文已凉，
    二是漏传一张就静默少一个候选。
    """
    out: dict[str, dict] = {}
    for rec in _list_kb(state, "claims"):
        if rec.get("scope") != "project":
            continue
        draft = rec.get("card_draft")
        if isinstance(draft, dict) and draft:
            out[str(rec.get("id") or "")] = draft
    return out


def promotion_scan(state: Any, *, drafts: dict[str, dict] | None = None) -> dict:
    """终态晋升扫盘：出候选清单，**不落地**。

    草稿默认从盘上读（claim 的 `card_draft`，由 `draft_knowledge_card` 在项目
    进行中写下）。`drafts` 参数仍可传，用于覆盖/补充 —— 传入的优先。
    没有草稿的候选仍会列出，但 `deprojectified` 不通过 —— 让"还差什么"
    显式可见，而不是静默漏掉。
    """
    drafts = {**card_drafts(state), **(drafts or {})}
    terminal = check_terminal_batch(state)

    candidates: list[Candidate] = []
    for rec in _list_kb(state, "claims"):
        if rec.get("scope") != "project":
            continue
        from shared.lib.kb_schema import normalize_claim_type

        kind = KIND_BY_CLAIM_TYPE.get(
            normalize_claim_type(str(rec.get("claim_type") or "")))
        if kind is None:
            continue
        cid = str(rec.get("id") or "")
        lane = LANE_BY_KIND[kind]
        draft = drafts.get(cid)

        closure = evidence_closure(state, cid)
        checks = [terminal, check_evidence_frozen(state, cid, closure)]
        # 机械车道不需要去项目化改写：死路的价值在触发条件本身，
        # 书目本来就无项目性。判断类才需要重写成知识卡。
        if lane == LANE_HUMAN:
            checks.append(check_deprojectified(draft))

        candidates.append(Candidate(
            source_id=cid, source_kind="claims", kind=kind, lane=lane,
            checks=checks, evidence_closure=closure, draft=draft,
        ))

    # 书目：被**可晋升 claim** 引用过的 chunk（"被采纳"的机械形态）
    cited: set[str] = set()
    for c in candidates:
        cited.update(x for x in c.evidence_closure if x.startswith("chunk_"))
    for chunk_id in sorted(cited):
        rec = _get_kb(state, "chunks", chunk_id)
        if not rec or rec.get("scope") != "project":
            continue
        if not _has_external_anchor(rec):
            continue          # 无外部锚的 chunk 不是书目，不进 org
        candidates.append(Candidate(
            source_id=chunk_id, source_kind="chunks", kind=KIND_BIBLIO,
            lane=LANE_MECHANICAL, checks=[terminal],
        ))

    eligible = [c for c in candidates if c.eligible]
    return {
        "terminal": terminal.passed,
        "exemplars": exemplar_candidates(state),
        "terminal_reason": terminal.reason,
        "candidates": candidates,
        "eligible": eligible,
        "mechanical": [c for c in eligible if c.lane == LANE_MECHANICAL],
        "human_batch": [c for c in eligible if c.lane == LANE_HUMAN],
        "blocked": [{"source_id": c.source_id, "blocking": c.blocking()}
                    for c in candidates if not c.eligible],
    }


# ── 落地 ────────────────────────────────────────────────────────────────────


def promote(state: Any, candidate: Candidate, *, approved_by: str,
            project_id: str, at: str) -> dict:
    """把一条合格候选写进 org —— 连同它的证据闭包，原子。

    org 记录是**新写入**，不是就地改标：`scope` 一旦定了不漂移（既有不变量），
    而且两层 schema 本来就该各自演化。原 project 条目留在原地，
    org 条目带 `promoted_from` 回链。
    """
    if not candidate.eligible:
        return {"status": "error", "code": "candidate_not_eligible",
                "blocking": candidate.blocking()}

    prov = {"project_id": project_id, "source_id": candidate.source_id,
            "approved_by": approved_by, "at": at}
    written: list[str] = []

    # 书目候选本身就是一段外部文献：它过河的样子是一条 org 书目（同锚合并），
    # 不是一条 claim。下面那段"条目本体"读的是 claims —— 书目走进去会读空，
    # 写出一条没有正文的 org claim。
    if candidate.source_kind == "chunks":
        src = _get_kb(state, "chunks", candidate.source_id)
        rec = _write_biblio(state, src, prov=prov,
                            source_chunk_id=candidate.source_id) if src else None
        if rec is None:
            return {"status": "error", "code": "biblio_not_written",
                    "blocking": [f"{candidate.source_id}: 读不到这段文献或它没有外部锚"]}
        return {"status": "success", "org_id": rec["id"], "written": [rec["id"]]}

    # 0. 新叶随本次人批一起注册 —— 词表治理和知识准入同一道人批，不另开流程
    if candidate.draft and candidate.draft.get("domain"):
        from core.domain_registry import register_leaf, validate_domain

        dv = validate_domain(state, str(candidate.draft["domain"]))
        if dv.status == "registrable":
            register_leaf(state, domain=dv.domain,
                          description=str(candidate.draft.get("statement") or "")[:120],
                          approved_by=approved_by, at=at)

    # 1. 证据闭包先行 —— 结论落地时它的支撑必须已经在 org
    #
    # 两种证据，两条路：
    #   外部文献  身份是锚（DOI/arXiv），多项目天然合并到同一条 → 书目条目
    #   自产产物  身份不能是 DOI，但可以是**源项目 + 冻结产物 + 内容哈希**
    #
    # 原来只有第一条路：`_has_external_anchor` 为假就整条跳过。后果是
    # **计算类项目的每张 org 卡出生即断链** —— 结论过河了，支撑它的实验日志
    # 没过河（2026-08-21 Ising 闭环回放：3/3 张卡全部 intact=false）。
    for ev_id in candidate.evidence_closure:
        if not ev_id.startswith("chunk_"):
            continue
        src = _get_kb(state, "chunks", ev_id)
        if not src:
            continue
        if _has_external_anchor(src):
            rec = _write_biblio(state, src, prov=prov, source_chunk_id=ev_id)
        else:
            rec = _write_evidence_record(state, src, prov=prov)
        if rec:
            written.append(rec["id"])

    # 2. 条目本体
    #
    # 字段继承按 schema 的 canonical 字段表**系统性**做，不手挑。
    # 手挑的写法每加一个类型必填字段（dead_end 的 dont_repeat_reason、
    # orphan_reason…）就多一条 fail-closed 路径，而单测夹具通常不带这些字段，
    # 于是缺陷只在真数据上现形 —— 2026-08-21 Ising 闭环回放连撞两个。
    src_rec = _get_kb(state, "claims", candidate.source_id) or {}
    body = {k: v for k, v in src_rec.items()
            if k in _INHERITABLE_CLAIM_FIELDS and v not in (None, "", [], {})}
    body.update({"scope": "org", "promoted_from": prov,
                 "org_kind": candidate.kind})

    if candidate.draft:
        # 人批车道：卡片是**改写后的**内容，覆盖继承来的原文
        for k in KNOWLEDGE_CARD_FIELDS + DEAD_END_CARD_FIELDS:
            if candidate.draft.get(k) not in (None, "", [], {}):
                body[k] = candidate.draft[k]
        body["claim_text"] = candidate.draft["statement"]
    body.setdefault("claim_text", src_rec.get("claim_text", ""))
    body.setdefault("claim_type", _claim_type_for(candidate.kind))
    body.setdefault("concept_ids", [])
    if not body.get("concept_ids"):
        # concept_ids 和 orphan_reason 是**一对**：要么有受控词条，要么说明为什么没有
        body.setdefault(
            "orphan_reason",
            "晋升自尚未注册受控词条的项目结论；词条归并由 curator 的 org 维护轮处理")
    body["sources"] = list(written) or list(src_rec.get("sources") or [])
    body["replication_count"] = 1        # 首次晋升 = 一次观察；复现由 dreaming 归并累加
    rec, _ = state.write_kb("claims", body)
    written.append(rec["id"])
    return {"status": "success", "org_id": rec["id"], "written": written}


def _write_biblio(state: Any, chunk: dict, *, prov: dict,
                  source_chunk_id: str) -> dict | None:
    """写一条 org 书目条目。

    **它不是 project chunk 的副本。**副本走不通也不该走：chunk 是内容寻址的，
    同样的选段在两层会撞同一个 id，而 scope 不漂移 —— 复制会静默 no-op。

    书目条目的身份是**锚（DOI/arXiv）**：同一篇论文被多个项目读到，天然落到
    同一条记录上，各家的"读后结论"并列累积。这既避开了 id 冲突，也**就是**
    dreaming 的同锚合并作业本身（RFC §9/§16.3）—— 不是绕开冲突的权宜之计。
    """
    anchor = str(chunk.get("source") or "")
    takeaway = {"project_id": prov["project_id"],
                "source_chunk_id": source_chunk_id,
                "at": prov["at"]}
    return _upsert_biblio(state, anchor=anchor,
                          text=str(chunk.get("text") or ""),
                          takeaway=takeaway, prov=prov)


def _write_evidence_record(state: Any, chunk: dict, *,
                           prov: dict) -> dict | None:
    """自产证据过河：正文带过去，产物本体留在源项目，锚记成可核对的指针。

    org 卡要自足 —— 读它的人不该为了看一眼证据去 mount 另一个项目。所以
    **证据正文本身跨层**（它是内容，不是指针）；产物是项目级的，跨不过来，
    但「源项目 / 产物 id / 版本 / 内容哈希」跨得过来，链因此走得通：
    要复核就去那个项目按哈希对，对不上说明产物被改过。
    """
    origin = str(chunk.get("origin_artifact_id") or "")
    if not origin:
        return None            # 既无外部锚又无来源产物 —— 没有可核对的东西
    project_id = prov["project_id"]
    version = chunk.get("origin_artifact_version")
    anchor = (f"project:{project_id}/artifact:{origin}"
              + (f"@v{version}" if version else ""))
    body = {
        "scope": "org", "promoted_from": prov,
        "text": str(chunk.get("text") or ""),
        "source": anchor,
        "origin_project_id": project_id,
        "origin_artifact_id": origin,
        "origin_artifact_frozen": True,   # 三查之一已验过（check_evidence_frozen）
    }
    for k in ("origin_artifact_version", "origin_content_hash", "origin_run_id"):
        if chunk.get(k):
            body[k] = chunk[k]
    try:
        rec, _ = state.write_kb("chunks", body)
        return rec
    except Exception:
        return None


def _upsert_biblio(state: Any, *, anchor: str, text: str,
                   takeaway: dict, prov: dict) -> dict | None:
    """同锚合并：已有该锚的 org 书目 → 追加读后结论；否则新建。"""
    for rec in _list_kb(state, "chunks"):
        if rec.get("scope") == "org" and rec.get("source") == anchor:
            readings = list(rec.get("group_readings") or [])
            if not any(r.get("project_id") == takeaway["project_id"]
                       for r in readings):
                readings.append(takeaway)
            merged = {**rec, "group_readings": readings}
            out, _ = state.write_kb("chunks", merged)
            return out
    out, _ = state.write_kb("chunks", {
        "text": text, "source": anchor, "scope": "org",
        "org_kind": KIND_BIBLIO,
        "group_readings": [takeaway],
        "promoted_from": {**prov, "source_id": takeaway["source_chunk_id"]},
    })
    return out


def _claim_type_for(kind: str) -> str:
    for ct, k in KIND_BY_CLAIM_TYPE.items():
        if k == kind:
            return ct
    return "empirical"


# ── 交给组织 ────────────────────────────────────────────────────────────────
#
# 扫盘只回答「哪些够格」。这一节回答「然后呢」—— 它们真的到了组织：
#
#   机械车道   直接落进 org 层（`promote`，approved_by = MECHANICAL）
#   人批车道   进**组织的**待审（org 层里的 REVIEW_QUEUE）；管理员在组织页上采纳 / 退回
#
# 此前两件都没发生（2026-09-24 读代码核实）：扫盘结果里有 `mechanical`，谁也不拿它
# 调 `promote`；人批提议写成**项目里**的一件 artifact，而 `resolve_proposal` 只认
# jsonl 队列 —— 采纳的那一下根本找不到它，找到了也只改状态。`promote()` 在生产代码里
# 零调用方：晋升这件事从来没发生过一次。
#
# 待审住在 org 层，不在项目的 inbox 里：晋升改变的是**全组织**读到什么，裁它的是组织
# 管理员 —— 不是这个项目的 curator，也不是 agent（`resolve_proposal` 碰不到这个队列）。
#
# 管理员读到的就是落地的：提上去时把那张卡**原样抄进**待审记录。采纳时落的是这一份，
# 不是那一刻 claim 上的草稿 —— 草稿在项目里还会被改，而管理员批的是他看到的那张。

MECHANICAL = "mechanical"
REVIEW_QUEUE = "promotion_proposals.jsonl"

REVIEW_PENDING = "pending"
REVIEW_ADOPTED = "adopted"
REVIEW_DECLINED = "declined"


def _queue_path():
    from core.paths import org_root

    return org_root() / REVIEW_QUEUE


def review_queue() -> list[dict]:
    """组织的待审，全部（按提上来的先后）。"""
    import json

    path = _queue_path()
    if not path.exists():
        return []
    out: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict) and rec.get("id"):
            out.append(rec)
    return out


def _rewrite_queue(records: list[dict]) -> None:
    import json

    path = _queue_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    tmp.replace(path)


def _holding_the_queue():
    from shared.lib import filelock

    return filelock.exclusive(_queue_path().with_suffix(".lock"))


def _already_in_org(state: Any, candidate: Candidate, project_id: str) -> str | None:
    """这条是不是已经从这个项目进过组织 —— 是就回 org 那一条的 id。

    判据是出处（`promoted_from` / 书目的 `group_readings`），不是正文：同一个项目的
    同一条结论，第二次终态扫盘（或第二次宣称完成）不该再落一份。
    """
    if candidate.source_kind == "chunks":
        anchor = str((_get_kb(state, "chunks", candidate.source_id) or {}).get("source") or "")
        for rec in _list_kb(state, "chunks"):
            if (rec.get("scope") == "org" and anchor and rec.get("source") == anchor
                    and any(r.get("project_id") == project_id
                            for r in rec.get("group_readings") or [])):
                return str(rec.get("id") or "")
        return None
    for rec in _list_kb(state, "claims"):
        prov = rec.get("promoted_from") or {}
        if (rec.get("scope") == "org" and prov.get("project_id") == project_id
                and prov.get("source_id") == candidate.source_id):
            return str(rec.get("id") or "")
    return None


def _candidate_view(c: Candidate) -> dict:
    return {"source_id": c.source_id, "kind": c.kind, "lane": c.lane,
            "eligible": c.eligible, "blocking": c.blocking(),
            "evidence_closure": list(c.evidence_closure), "has_draft": bool(c.draft)}


def offer_to_the_organisation(state: Any, *, project_id: str, at: str,
                              source_home: str = "", send: bool = True,
                              drafts: dict[str, dict] | None = None) -> dict:
    """终态扫盘，然后把够格的交给组织。**幂等**：进过组织的、已在待审里的都不再动。

    `send=False` 只算不交（想先看清单时）。`source_home` 是这个项目的知识住在哪个
    harness home —— 采纳时要回到那里读原 claim 和它的证据；不给就是当前这个。
    """
    res = promotion_scan(state, drafts=drafts)
    out: dict[str, Any] = {
        "terminal": res["terminal"], "reason": res["terminal_reason"],
        "landed": [], "queued": [], "already": [], "failed": [],
        "blocked": res["blocked"],
        "candidates": [_candidate_view(c) for c in res["candidates"]],
        "exemplars": res["exemplars"],
    }
    if not res["terminal"]:
        return out

    queue = review_queue()
    pending = {(p.get("project_id"), p.get("source_id")) for p in queue
               if p.get("status") == REVIEW_PENDING}
    # 退回过的：同一张卡不再提 —— 否则每次宣称完成都把管理员退回的那张原样送回去。
    # 卡改过了（照退回理由补了）才是新的一次提交。
    declined = {(p.get("project_id"), p.get("source_id"), _card_key(p.get("card")))
                for p in queue if p.get("status") == REVIEW_DECLINED}
    for c in res["mechanical"]:
        org_id = _already_in_org(state, c, project_id)
        if org_id:
            out["already"].append({"source_id": c.source_id, "org_id": org_id})
            continue
        if not send:
            continue
        done = promote(state, c, approved_by=MECHANICAL, project_id=project_id, at=at)
        if done.get("status") == "success":
            out["landed"].append({"source_id": c.source_id, "kind": c.kind,
                                  "org_id": done["org_id"]})
        else:
            out["failed"].append({"source_id": c.source_id,
                                  "blocking": done.get("blocking") or [done.get("code")]})

    for c in res["human_batch"]:
        org_id = _already_in_org(state, c, project_id)
        if org_id:
            out["already"].append({"source_id": c.source_id, "org_id": org_id})
            continue
        if (project_id, c.source_id) in pending:
            out["already"].append({"source_id": c.source_id, "awaiting_review": True})
            continue
        if (project_id, c.source_id, _card_key(c.draft)) in declined:
            out["already"].append({"source_id": c.source_id, "declined": True})
            continue
        if not send:
            continue
        queued = _enqueue(state, c, project_id=project_id, at=at,
                          source_home=source_home or _this_home())
        if queued is not None:
            out["queued"].append(queued)

    # 这个项目站得住的结论和「本组已知」的某条方向相反 → 那一条提出来等管理员裁。
    # 只比有知识卡的（卡上有适用条件，判据才谈得上「说的是同一件事」）；项目自己翻成
    # refuted 的不比 —— 它的正文说的是被否掉的那一面，拿它比方向会比反。
    out["corrections"] = []
    if send:
        out["corrections"] = _raise_what_the_project_contradicts(
            state, res["human_batch"], project_id=project_id, at=at,
            source_home=source_home or _this_home())
    return out


def _raise_what_the_project_contradicts(state: Any, candidates: list, *, project_id: str,
                                        at: str, source_home: str) -> list[dict]:
    from core import org_corrections

    raised = []
    for c in candidates:
        if not c.draft or c.kind != KIND_VERIFIED_FINDING:
            continue
        src = _get_kb(state, "claims", c.source_id) or {}
        if str(src.get("status") or "") == "refuted":
            continue
        for older in org_corrections.at_odds_with_the_organisation(state, c.draft):
            done = org_corrections.propose(
                state, str(older["id"]), verdict=org_corrections.VERDICT_REFUTED,
                reason=("一个做完的项目在重叠的适用条件下得出了相反的结论："
                        f"「{c.draft.get('statement') or ''}」（证据已冻结，{len(c.evidence_closure)} 条）。"),
                by=MECHANICAL, at=at, origin=org_corrections.ORIGIN_CONTRADICTION,
                project_id=project_id, evidence=c.evidence_closure,
                source_home=source_home, about=str(c.draft.get("statement") or ""))
            if done.get("status") == "success" and done.get("proposal"):
                raised.append({"org_id": older["id"], "proposal_id": done["proposal"]["id"],
                               "source_id": c.source_id})
    return raised


def _card_key(card: Any) -> str:
    import json

    return json.dumps(card or {}, ensure_ascii=False, sort_keys=True)


def _this_home() -> str:
    from core.paths import home

    return str(home())


def _enqueue(state: Any, c: Candidate, *, project_id: str, at: str,
             source_home: str) -> dict | None:
    import uuid

    src = _get_kb(state, "claims", c.source_id) or {}
    rec = {
        "id": f"promo_{uuid.uuid4().hex[:12]}",
        "status": REVIEW_PENDING,
        "project_id": project_id,
        "source_id": c.source_id,
        "kind": c.kind,
        # 管理员读的、采纳时落的，都是这一份（见本节开头）。
        "card": dict(c.draft or {}),
        "original": str(src.get("claim_text") or ""),
        "evidence_closure": list(c.evidence_closure),
        "source_home": source_home,
        "proposed_at": at,
    }
    # 锁外看过一次，锁里再看一次：两个会话同时宣称完成，只该进一条。
    queued = enqueue(rec, unless=lambda p: (
        p.get("status") == REVIEW_PENDING and p.get("project_id") == project_id
        and p.get("source_id") == c.source_id))
    if queued is None:
        return None
    return {"proposal_id": rec["id"], "source_id": c.source_id, "kind": c.kind}


def enqueue(rec: dict, *, unless) -> dict | None:
    """放进组织的待审。锁里再看一次有没有同一件事在等（`unless`）—— 有就不放、回 None。

    晋升和更正（`core.org_corrections`）进的是同一个待审：管理员在一个地方裁。
    """
    with _holding_the_queue():
        records = review_queue()
        if any(unless(p) for p in records):
            return None
        records.append(rec)
        _rewrite_queue(records)
    return rec


def adopt(state: Any, proposal_id: str, *, approved_by: str, at: str) -> dict:
    """管理员采纳一条待审：把**提上来的那张卡**落进 org 层，连同证据闭包。

    `state` 必须是那个项目的（`project_id` 对得上、home 是 `source_home`）——
    原 claim 和它的证据住在那里。
    """
    from core import org_corrections

    with _holding_the_queue():
        records = review_queue()
        rec = next((p for p in records if p.get("id") == proposal_id), None)
        if rec is None:
            return {"status": "error", "code": "not_found",
                    "error": f"待审里没有 {proposal_id}"}
        if rec.get("status") != REVIEW_PENDING:
            return {"status": "error", "code": "already_decided",
                    "error": f"这一条已经{_said(rec.get('status'))}了"}
        if rec.get("type") == org_corrections.PROPOSAL_TYPE:
            # 更正不读哪个项目：它改的是组织自己那一条。
            done = org_corrections.adopt(state, rec, approved_by=approved_by, at=at)
            if done.get("status") != "success":
                return done
            rec.update(status=REVIEW_ADOPTED, decided_by=approved_by, decided_at=at)
            _rewrite_queue(records)
            return {"status": "success", "proposal": rec}
        if getattr(state, "project_id", None) != rec.get("project_id"):
            return {"status": "error", "code": "wrong_project",
                    "error": "采纳要在提出它的那个项目里读原结论和证据"}
        if _get_kb(state, "claims", str(rec["source_id"])) is None:
            # 读不到原结论，`promote` 照样能拿卡片正文写出一条 org 记录 —— 但证据闭包一条都
            # 带不过去，出处也是空的：一条「组织知道」却说不出凭什么的知识。
            return {"status": "error", "code": "source_unreadable",
                    "error": f"在项目 {rec.get('project_id')} 里读不到原结论 {rec['source_id']}，"
                             "没法连同证据一起采纳"}
        card = rec.get("card") or None
        candidate = Candidate(
            source_id=str(rec["source_id"]), source_kind="claims",
            kind=str(rec.get("kind") or KIND_VERIFIED_FINDING), lane=LANE_HUMAN,
            # 三查在提上来时已过；冻结的不会再变，卡是抄下来的那一份。
            checks=[Check("offered", True)],
            evidence_closure=tuple(rec.get("evidence_closure") or ()),
            draft=card if isinstance(card, dict) and card else None,
        )
        org_id = _already_in_org(state, candidate, str(rec["project_id"]))
        if org_id is None:
            done = promote(state, candidate, approved_by=approved_by,
                           project_id=str(rec["project_id"]), at=at)
            if done.get("status") != "success":
                return {"status": "error", "code": "promote_failed",
                        "error": "；".join(done.get("blocking") or [str(done.get("code"))])}
            org_id = done["org_id"]
        rec.update(status=REVIEW_ADOPTED, decided_by=approved_by, decided_at=at,
                   org_id=org_id)
        _rewrite_queue(records)
    # 锁放开以后再比：新进来的这条和组织已有的某条方向相反，就把那条提出来等裁
    # （`enqueue` 自己要拿同一把锁）。
    _raise_what_it_contradicts(state, org_id, at=at)
    return {"status": "success", "proposal": rec}


def _raise_what_it_contradicts(state: Any, org_id: str, *, at: str) -> list[dict]:
    from core import org_corrections

    new = _get_kb(state, "claims", org_id)
    if not new:
        return []
    raised = []
    for older in org_corrections.at_odds_with_the_organisation(state, new, exclude=(org_id,)):
        done = org_corrections.propose(
            state, str(older["id"]), verdict=org_corrections.VERDICT_SUPERSEDED,
            # 理由写给以后读到这条裁定的人：它为什么不再作数，自己说得清（另一面的原话在里面）。
            # 「认可 / 驳回各意味着什么」是给管理员的，界面上说。
            reason=("后来采纳的一条在重叠的适用条件下与它方向相反："
                    f"「{new.get('statement') or new.get('claim_text') or ''}」。"),
            by=MECHANICAL, at=at, origin=org_corrections.ORIGIN_CONTRADICTION,
            superseded_by=org_id,
            about=str(new.get("statement") or new.get("claim_text") or ""))
        if done.get("status") == "success" and done.get("proposal"):
            raised.append(done["proposal"])
    return raised


def decline(proposal_id: str, *, declined_by: str, reason: str, at: str) -> dict:
    """管理员退回一条待审。理由必填 —— 退回是说给提上来的那个项目听的。"""
    reason = str(reason or "").strip()
    if not reason:
        return {"status": "error", "code": "reason_required",
                "error": "退回要写理由：提上来的人要知道差在哪"}
    with _holding_the_queue():
        records = review_queue()
        rec = next((p for p in records if p.get("id") == proposal_id), None)
        if rec is None:
            return {"status": "error", "code": "not_found",
                    "error": f"待审里没有 {proposal_id}"}
        if rec.get("status") != REVIEW_PENDING:
            return {"status": "error", "code": "already_decided",
                    "error": f"这一条已经{_said(rec.get('status'))}了"}
        rec.update(status=REVIEW_DECLINED, decided_by=declined_by, decided_at=at,
                   reason=reason)
        _rewrite_queue(records)
    return {"status": "success", "proposal": rec}


def _said(status: Any) -> str:
    return {REVIEW_ADOPTED: "采纳", REVIEW_DECLINED: "退回"}.get(str(status), str(status))


# ── 盘面读取（全部有界、容错）──────────────────────────────────────────────


def _artifacts(state: Any, artifact_type: str) -> list[dict]:
    try:
        return list(state.list_artifacts(artifact_type) or [])
    except Exception:
        return []


def _read_artifact(state: Any, artifact_id: str) -> dict | None:
    try:
        return state.read_artifact(artifact_id)
    except Exception:
        return None


def _is_frozen(state: Any, entry: dict) -> bool:
    rec = _read_artifact(state, str(entry.get("id") or ""))
    return bool((rec or {}).get("metadata", {}).get("frozen"))


def _list_kb(state: Any, entity: str) -> list[dict]:
    try:
        return [r for r in (state.list_kb(entity) or []) if isinstance(r, dict)]
    except Exception:
        return []


def _get_kb(state: Any, entity: str, rid: str) -> dict | None:
    try:
        return state.get_kb_record(entity, rid)
    except Exception:
        return None


def _has_external_anchor(chunk: dict) -> bool:
    from shared.lib.kb_schema import is_external_uri

    return is_external_uri(str(chunk.get("source") or ""))

"""KB v3 schema —— 4 entity × 10 concept_type × 10 claim_type + scope + provenance。

设计原则见 docs/kb-system.md。核心：

  - 4 entity：concept / claim / experiment / chunk
  - hypothesis 不再独立 entity，而是 claim with claim_type='hypothesis' + 强校验
  - synthesis / question / opportunity / decision / failure 都归入 claim
  - 每个 entity 加 `scope` 字段（org / project，按内容智能默认）
  - 每个 entity 加 `created_by_role` provenance tag
  - claim 加连续 `confidence: float [0,1]` + `replication_count: int`
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any, Callable, Literal

from shared.lib.schema_check import schema_errors

# ─────────────────────────────────────────────────────────────────────────────
# 基础 helpers
# ─────────────────────────────────────────────────────────────────────────────

# v3.1 fix（2026-06 审计 高危#1）：语义算符必须参与签名。
# 旧 normalize 剥掉所有标点 → '>90%' 与 '<90%' 归一后相同 → 语义相反的
# claim 同 id 被静默合并、confidence 取 max —— 防重机制变成证据篡改通道。
# 这些字符携带命题方向/比较语义，保留并独立成 token（'x>90' == 'x > 90'）。
_SEMANTIC_OPS = "<>=!~%±≥≤≠→↑↓"
_PUNCT_RE = re.compile(rf"[^\w\s{re.escape(_SEMANTIC_OPS)}]+")
_OP_TOKEN_RE = re.compile(rf"([{re.escape(_SEMANTIC_OPS)}]+)")
_SPACE_RE = re.compile(r"\s+")


def normalize(s: str) -> str:
    """大小写 / 标点 / 多空格归一（语义算符保留为独立 token）。"""
    s = (s or "").lower()
    s = _PUNCT_RE.sub(" ", s)
    s = _OP_TOKEN_RE.sub(r" \1 ", s)
    s = _SPACE_RE.sub(" ", s).strip()
    return s


def sha8(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:8]


def sha12(s: str) -> str:
    """v3.1: id 哈希段从 8 hex（32 bit，30k 条时 ~10% 生日碰撞）加宽到
    12 hex（48 bit，千万条量级才有可感碰撞率）。旧 8 位 id 由
    scripts/migrate_kb_ids_v3.py 一次性重写。"""
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:12]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ─────────────────────────────────────────────────────────────────────────────
# 枚举：Entity / concept_type / claim_type / status / scope / created_by_role
# ─────────────────────────────────────────────────────────────────────────────

ENTITIES = ("concepts", "claims", "experiments", "chunks")

# 4 个 entity 的单数名（id prefix）
_SINGULAR = {
    "concepts": "concept",
    "claims": "claim",
    "experiments": "experiment",
    "chunks": "chunk",
}


def entity_singular(entity: str) -> str:
    return _SINGULAR.get(entity, entity.rstrip("s") or entity)


# 10 个 concept_type
CONCEPT_TYPES: tuple[str, ...] = (
    "method",        # 算法 / 数学过程（RMSD 计算）
    "tool",          # 软件 / 实现（LAMMPS, MACE-MP-0）
    "dataset",       # 标定数据集合（QM9, MD17）
    "phenomenon",    # 可观测行为（OOD 泛化 gap）
    "theory",        # 解释框架（DFT, Statistical mechanics）
    "metric",        # 测量尺度（MAE, RMSE）
    "domain",        # 应用领域（drug discovery, materials）
    "task",          # 形式化的研究问题（energy prediction）
    "person",        # 研究者
    "group",         # 实验室 / 机构 / 公司
)


# 10 个 claim_type
#: claim 的类型集。**存在判据 = 有机械消费者** —— 没有任何闸区别对待的
#: 分类不是信息，是装饰，而装饰会被撒得到处都是（撒得越远越没人核）。
#:
#: 2026-08-21 收敛 10 → 5。删掉的五个与去向：
#:
#:   theoretical / causal / assumption
#:       **零消费者**（全仓 grep：没有一处按这三个类型分支）。语义差异写进
#:       正文与 scope_dimensions，不占类型位。→ empirical / synthesis
#:   conjecture
#:       提案期的思考。承诺住预注册、思考住 memory —— 与 hypothesis 同理
#:       出 KB（KB 只收被采纳的断言，见 v21-collaboration-model §7）。
#:       唯一消费方 core/manifest 的"开放问题"改读 org open_question。
#:   replication
#:       复现是**事件**不是类型。`replication_count` 字段已经在，且跨项目
#:       合并时会累加（见 merge 语义）—— 双轨收敛到字段那一轨。
CLAIM_TYPES: tuple[str, ...] = (
    "empirical",        # 观测/测量 —— 证据类主力
    "methodological",   # 关于怎么做 —— org 的方法配方来源
    "hypothesis",       # 可证伪 + prereg ★ 强校验（采纳后才进 KB）
    "synthesis",        # 多 claim 聚的高阶
    "dead_end",         # 已知不通的路 —— 复利最强的一类
)

#: 读取端兼容：旧数据里的类型映射到现行类型。**只在读取端**，
#: 写入端不接受旧名 —— 否则收敛等于没做（新数据还会继续长出旧类型）。
#: 按仓库惯例（PR#505）不写迁移器：不兼容就从头再跑，这张表只保证
#: 老 checkpoint 读得进来不炸。
LEGACY_CLAIM_TYPE_ALIASES: dict[str, str] = {
    "theoretical": "empirical",
    "causal": "empirical",
    "assumption": "empirical",
    "conjecture": "hypothesis",
    "replication": "empirical",
}


def normalize_claim_type(claim_type: str) -> str:
    """读取端把旧类型映射到现行类型。写入端不该调它。"""
    ct = str(claim_type or "")
    return LEGACY_CLAIM_TYPE_ALIASES.get(ct, ct)


# 6 个 claim.status（连续 confidence 之上的 derived）
CLAIM_STATUSES: tuple[str, ...] = (
    "open",           # 提出但未验证（conjecture / hypothesis 默认）
    "provisional",    # 有 evidence 但未定论
    "validated",      # 高 confidence + has validated review
    "refuted",        # 低 confidence + has refuted review
    "superseded",     # 被新 claim 取代
    "needs_review",   # v3.1: 上游依赖被 refute，等待复审（propagate_refutation 写入）
)


# Concept lifecycle status
CONCEPT_STATUSES: tuple[str, ...] = ("active", "merged_into", "deprecated")


# Experiment outcome
EXPERIMENT_OUTCOMES: tuple[str, ...] = (
    "success", "refuted", "inconclusive", "error",
)


# Scope: 每条 entity 的存储域
SCOPES: tuple[str, ...] = ("org", "project")


# Created-by-role: provenance tag（trust calibration 关键）
CREATED_BY_ROLES: tuple[str, ...] = (
    "agent_auto",                    # agent 直写（curator Mode 3 AUTO 等低风险路径）
    "agent_proposed_user_accepted",  # 走过 propose gate
    "user_directed",                  # user 在 chat 直接指令产生
    "user_authored",                  # user 自己编辑文件
    "imported_from_external",         # 从 paper / 其它 KB 导入
)


# ─────────────────────────────────────────────────────────────────────────────
# ID 计算（content-addressed）
# ─────────────────────────────────────────────────────────────────────────────

def _sig_concept(r: dict) -> str:
    return normalize(r.get("canonical_name", ""))


def _sig_claim(r: dict) -> str:
    """v0.3.2.1 fix: claim 内容寻址签名加 claim_type + 核心 scope_dimensions。

    历史 bug：纯 claim_text 作签名 → 字面同但 claim_type / scope_dimensions 不同
    的 claim 被错误 collapse 到同一 id。例：
      - "X fails on Y" empirical
      - "X fails on Y" theoretical (= 不同 claim 但 id 一样)
      - "X fails on Y" + dataset=QM9
      - "X fails on Y" + dataset=RMD17  (= 不同 scope 但 id 一样)
    现在签名含 claim_type + dataset + regime + task + split，让真不同 claim 真分开。
    """
    text = normalize(r.get("claim_text", ""))
    ct = (r.get("claim_type") or "empirical").lower()
    dims = r.get("scope_dimensions") or {}
    # 只取影响 claim identity 的关键字段（dataset/regime/task/split）
    # sample_size / hardware 等不影响 identity 不算
    key_dims = []
    for k in ("dataset", "regime", "task", "split"):
        v = dims.get(k)
        if v:
            key_dims.append(f"{k}={str(v).strip().lower()}")
    dims_str = "|".join(key_dims) if key_dims else ""
    sig = f"{text}|ct:{ct}|{dims_str}"
    # v3.3（#395-Issue2）：hypothesis 的身份锚点还包括**它绑的那份冻结预注册**。
    # 上面那段历史 bug 少列了一维：措辞与 scope 全同、但受不同 prereg 约束的两条
    # hypothesis 是**修订**，不是同一条 claim，却被 collapse 到同一个 id。
    #
    # 实测（jicq E2E 流体力学 Study3）：冻了 prereg v5 带新 falsification 判据重建
    # claim → 算出同一个 id → 走 upsert 合进旧记录（新判据丢失）→
    # validate_hypothesis_outputs 照样失败，改措辞也没用。语义去重那层的守卫
    # 若不配上这一层，会在 id 层被原样抵消。
    #
    # 只对**带 prereg 绑定**的 claim 生效：其余 claim（含历史记录）签名一字不变，
    # 不动存量 id。
    # RFC 2026-08-18（artifact 身份+版本）：hypothesis claim 的身份挂在**研究
    # 对象**上 —— (预注册身份, 问题 id)，不挂在措辞或某一版的 chunk 上。
    #
    # 为什么：v3.3 把 prereg_chunk_id 掺进签名，修订确实不再被 collapse 了 ——
    # 但每冻一版 prereg 就有一个新 chunk，于是修订产生**新 claim id**，旧
    # claim 没有任何机制标 superseded，新旧并存把 validation 卡死（#395-2 的
    # 后半段）。结构化身份让"H1 的修订"命中同一个 id，走 upsert 原地更新 +
    # revision_history —— 重复 claim 按构造不再产生，也就无需 supersede。
    #
    # 只对**同时带 hypothesis_id + prereg_artifact_id**的 claim 生效（新路径
    # 由 create_claim 机械补齐这两个字段）；存量 claim 缺字段 → 走旧签名，
    # 已有 id 一个都不动。
    if ct == "hypothesis":
        hyp = (r.get("hypothesis_id") or "").strip()
        lineage = (r.get("prereg_artifact_id") or "").strip()
        if hyp and lineage:
            return f"hyp|{lineage}|{hyp}|ct:{ct}"
    prereg = (r.get("prereg_chunk_id") or "").strip()
    if prereg:
        sig = f"{sig}|prereg:{prereg}"
    return sig


def _sig_experiment(r: dict) -> str:
    # 同样描述+run_at 是同一 experiment
    return f"{normalize(r.get('experiment_text', ''))}|at:{r.get('run_at', '')}"


def _sig_chunk(r: dict) -> str:
    text = r.get("text", "")
    source = (r.get("source") or "").strip().lower()
    return f"{normalize(text)}|src:{source}"


_SIGNATURES: dict[str, Callable[[dict], str]] = {
    "concepts": _sig_concept,
    "claims": _sig_claim,
    "experiments": _sig_experiment,
    "chunks": _sig_chunk,
}


def compute_kb_id(entity: str, record: dict) -> str:
    """`<entity_singular>_<sha12(signature)>` 内容寻址 id（v3.1 起 12 hex）。

    ## scope 是身份的一部分（2026-08-21）

    signature 原来只算内容。于是**同样的内容在两层会撞同一个 id**，而
    `scope` 一旦定了不漂移 —— 晋升写入退化成对 project 原件的 upsert，
    静默 no-op：结论上了 org，支撑它的书目没上去，org 里挂着断链的"真理"。
    实测于 P2 晋升管线（RFC §15）。

    两层的同名内容本来就是**两种东西**：project chunk 是本项目的工作副本，
    org 书目条目是机构资产（带各项目读后结论、跨项目累积）。身份包含它住在
    哪一层，这不是为绕开冲突加的字段，是本来就该有的区分。

    副作用正好是想要的：多个项目晋升同一篇论文 → 同锚同层 → 同一个 id →
    天然合并（dreaming 的同锚合并作业，RFC §9）。
    """
    sigfn = _SIGNATURES.get(entity)
    if sigfn is None:
        sig = json.dumps(record, sort_keys=True, ensure_ascii=False)
    else:
        sig = sigfn(record)
    scope = str(record.get("scope") or "project")
    return f"{entity_singular(entity)}_{sha12(f'{sig}|scope:{scope}')}"


# ─────────────────────────────────────────────────────────────────────────────
# 外部 URI 识别（用于 scope 默认 + chunk validation）
# ─────────────────────────────────────────────────────────────────────────────

_EXTERNAL_URI_PREFIXES = (
    "doi:", "arxiv:", "http://", "https://", "pmid:", "isbn:",
)

# v3.2（2026-07 KB 审计 Bug#2）：非外部 host —— 项目内部数据被伪装成
# `https://project-internal/...` 骗过前缀判断 → chunk 默认 scope=org → 实验
# 结果泄漏进跨项目永久层。这些 host 不是真外部来源，一律拒。
_NON_EXTERNAL_HOSTS = (
    "project-internal", "localhost", "127.0.0.1", "0.0.0.0",
    "internal", "local", "::1",
)


def is_external_uri(s: str) -> bool:
    if not s:
        return False
    s_lower = s.lower().strip()
    if not any(s_lower.startswith(p) for p in _EXTERNAL_URI_PREFIXES):
        return False
    # http(s):// 的要额外查 host —— 内网/伪造 host 不算外部来源
    for scheme in ("http://", "https://"):
        if s_lower.startswith(scheme):
            host = s_lower[len(scheme):].split("/", 1)[0].split(":", 1)[0]
            if any(host == h or host.endswith("." + h) or host.startswith(h)
                   for h in _NON_EXTERNAL_HOSTS):
                return False
            # 私有网段 IP 也不算外部
            if (host.startswith("192.168.") or host.startswith("10.")
                    or host.startswith("172.16.")):
                return False
    return True


def is_chunk_id(s: str) -> bool:
    return isinstance(s, str) and s.startswith("chunk_") and len(s) >= 14


def is_claim_id(s: str) -> bool:
    return isinstance(s, str) and s.startswith("claim_") and len(s) >= 14


def is_concept_id(s: str) -> bool:
    return isinstance(s, str) and s.startswith("concept_") and len(s) >= 16


# ─────────────────────────────────────────────────────────────────────────────
# Scope 智能默认（核心新机制）
# ─────────────────────────────────────────────────────────────────────────────

def smart_default_scope(entity: str, record: dict) -> str:
    """出生 scope —— **一律 project**。org 没有出生通道，只有晋升通道。

    ## 删掉了什么

    这里原来是一台类型启发式机器：concept 按 concept_type 直通 org、
    chunk 按有没有外部 URI 分流、claim 按 claim_type 分流，
    empirical/methodological 还去猜 `scope_dimensions` 里有没有 seed/run_id。
    **它把治理门整个绕开了** —— 条目在创建那一瞬间就已经在 org 里面，
    当年那个 `propose_org_promotion` 工具（设计中的正路）历史调用数 = 0
    —— 它已于 2026-08-21 从工具面下架，晋升现在只有终态扫盘
    `curator_scan(scan_type='org_promotion_candidates')` 一个入口。

    v3.3 的写入层降级补丁（`_org_scope_downgrade_needed`）是发现绕开之后打的，
    与本函数一并删除：没有东西出生在 org，它防的事不再发生。

    于是"这条该不该进 org"从三个互相矛盾的真相源（出生启发式 / 降级补丁 /
    治理晋升）收敛成一个：**晋升**。

    ## 为什么一律 project 而不是"更聪明的启发式"

    org 条目的读者是**一个还不存在的、做别的课题的项目**。创建那一刻，
    没有任何信息能判断这条内容对未来项目值不值得花 context tokens 读 ——
    这个判断的依据（项目是否终态、结论是否被验证、能否写出去项目化版本）
    全部在晋升时刻才存在。出生时猜它，就是「模型能判断的事别硬编码成阈值」
    的反面：一个必然猜错、且猜错没人报错的阈值。

    调用方仍可显式传 scope（迁移工具、curator 晋升写入走这条）。

    见 docs/RFC_KB_TWO_TIERS_20260820.md §1/§14。
    """
    return "project"


# ─────────────────────────────────────────────────────────────────────────────
# Status 从 confidence + review_history 推导
# ─────────────────────────────────────────────────────────────────────────────

def derive_status(record: dict) -> str:
    """从 confidence + review_history + claim_type 算 status。"""
    # 显式标 superseded 直接返
    if record.get("superseded_by_claim_id"):
        return "superseded"

    history = record.get("review_history") or []

    # v3.1: 最近一条 review 把它送进 needs_review（上游被 refute）且之后没有
    # 新的复审结论 → 保持 needs_review，不被 confidence 推导覆盖
    if history and history[-1].get("to_status") == "needs_review":
        return "needs_review"
    has_validated_review = any(r.get("to_status") == "validated" for r in history)
    has_refuted_review = any(r.get("to_status") == "refuted" for r in history)

    conf = record.get("confidence", 0.5)
    if not isinstance(conf, (int, float)):
        conf = 0.5
    conf = max(0.0, min(1.0, float(conf)))

    if conf < 0.25 and has_refuted_review:
        return "refuted"
    if conf > 0.85 and has_validated_review:
        return "validated"

    # claim_type 决定 open vs provisional 默认
    ct = record.get("claim_type", "empirical")
    # "conjecture" 不再单列：写入端已拒收，读取端归一成 hypothesis —— 到这里
    # 的记录只可能是 hypothesis。
    if ct == "hypothesis":
        # 没立得住的 evidence 时是 open
        rep_count = record.get("replication_count", 0)
        if rep_count == 0 and not history:
            return "open"
    return "provisional"


# ─────────────────────────────────────────────────────────────────────────────
# Status 转换合法性（claim 5 status）
# ─────────────────────────────────────────────────────────────────────────────

_CLAIM_STATUS_TRANSITIONS: dict[str, set[str]] = {
    "open":         {"provisional", "validated", "refuted", "superseded", "needs_review"},
    "provisional":  {"validated", "refuted", "superseded", "needs_review"},
    "validated":    {"refuted", "superseded", "needs_review"},   # 可被新证据翻 / 依赖被反驳待复审
    "needs_review": {"open", "provisional", "validated", "refuted", "superseded"},
    "refuted":      set(),                        # terminal
    "superseded":   set(),                        # terminal
}


def can_transition_claim_status(from_s: str, to_s: str) -> bool:
    if from_s == to_s:
        return True
    return to_s in _CLAIM_STATUS_TRANSITIONS.get(from_s, set())


def is_experiment_id(s: str) -> bool:
    return isinstance(s, str) and s.startswith("experiment_") and len(s) >= 19


#: 翻 verdict 的契约：reasoning 非空 + evidence_ids 每项是 chunk_/experiment_/claim_ id
#: （前缀 + ≥8 位，与 is_chunk_id / is_experiment_id / is_claim_id 同口径）。
_STATUS_FLIP_SCHEMA: dict = {
    "type": "object",
    "required": ["reasoning"],
    "properties": {
        "reasoning": {"type": "string", "minLength": 1},
        "evidence_ids": {
            "type": "array",
            "items": {"type": "string", "pattern": r"^(chunk_|experiment_|claim_).{8,}$"},
        },
    },
}


def validate_status_flip(
    record: dict,
    new_status: str,
    *,
    reasoning: str,
    evidence_ids: list[str] | None = None,
) -> None:
    """翻 claim verdict 的**契约**检查（C 类）：reasoning 非空、evidence_ids 引用成形。

    判决拆除第三波（kb_schema:465/474 降格）：这里曾经还判「翻到 validated/refuted
    必须 ≥1 条证据」「hypothesis 的证据不能只引 claim」—— 那是证据充分性判决，
    evidence_ids=[] 如实进 review_history 账不假；同一调用方 kb.update_claim_status
    里 authority / prereg 两道早已改成「降落 provisional」，充分性由那条路承接
    （scientific_verdict_downgraded_to_provisional），referee 终审。
    文案与判据同源：reasoning 只查非空（一审删了字数闸），不再说「≥30 字符」。
    """
    errs = schema_errors(_STATUS_FLIP_SCHEMA, {
        "reasoning": (reasoning or "").strip() or None,
        "evidence_ids": [e for e in (evidence_ids or []) if e is not None],
    })
    if errs:
        raise SchemaValidationError(
            "；".join(errs)
            + "。reasoning 要说清判定依据：对照了哪条 falsification_criteria、看了哪个结果；"
            "evidence_ids 每项须是 chunk_ / experiment_ / claim_ id。"
        )


# ─────────────────────────────────────────────────────────────────────────────
# 字段分段（canonical / derived / lifecycle）
# ─────────────────────────────────────────────────────────────────────────────

_COMMON_CANONICAL = {
    "id", "created_at", "created_by_run_id", "created_by_node_type",
    "created_by_role",      # ★ v3 新增 provenance tag
    "created_by_user_id",   # ★ v3 新增 multi-user identity
    "scope",                 # ★ v3 新增 scope
    "schema_version", "notes",
}
_COMMON_DERIVED = {
    "derived_at", "derived_by_model", "derived_by_curator_run_id",
    "embedding_hash", "embedding_model",
    "last_referenced_at", "merge_count", "updated_at",
    "reverted_by_curator_run_id", "usage_count",
}
_COMMON_LIFECYCLE = {"status", "last_reviewed_at", "review_history"}


_FIELDS: dict[str, dict[str, set[str]]] = {
    "concepts": {
        "canonical": {"canonical_name", "concept_type", "description",
                       "aliases", "attributes"},
        "derived": {"related_to"},
        "lifecycle": {"merged_into_concept_id"},
    },
    "claims": {
        "canonical": {
            "claim_text", "claim_type",
            "concept_ids", "sources",  # 必填
            "orphan_reason",
            "confidence",                     # ★ v3 连续 [0,1]
            "replication_count",              # ★ v3
            "scope_dimensions",
            # hypothesis-only canonical fields
            "falsification_criteria_structured",
            "falsification_criteria_text",
            "prereg_chunk_id",
            # RFC 2026-08-18：hypothesis claim 的结构化身份锚（见 _sig_claim）
            "hypothesis_id", "prereg_artifact_id",
            "predicted_outcome",
            # synthesis-only
            "synthesis_pattern",
            # methodological-only
            "rationale", "alternatives_considered",
            # 已删（2026-08-21 类型收敛 10→5）：causal 的 mechanism /
            # confounders_controlled、assumption 的 domain_of_validity、
            # open/opportunity 的 expected_answer_form / suggested_actions、
            # replication 的 replicates_claim_id / replication_outcome。
            # 这些字段的所有者类型都不存在了，留在字段表里等于给 create_claim
            # 继续广告"你可以填"—— 而 schema 会拒收它们的 claim_type。
            # dead_end-only
            "dont_repeat_reason", "next_try",
            # dead_end 的 org 卡专属：送达面按它们工作 ——
            # trigger 决定 reviewer 红旗匹配什么，cost_when_hit 是"上次代价"。
            # 没有它们的死路卡是**送不出去的**（红旗永不响、代价栏永远空）。
            "trigger", "cost_when_hit",
            # v3.4：sources 全为外部来源（论文/DOI）→ 文献转述类。ceiling 豁免，
            # 且永不进承重层（一次检索就能重新推导的事实不构成科研资本）。
            "literature_reported",
        },
        "derived": {
            "subject_concept_id", "mentioned_concept_ids",
            "support_count", "verdict_at", "verdict_reasoning",
            "tested_by_experiment_ids", "supporting_claim_ids",
            "refuting_claim_ids",
            # ── 知识卡草稿区（原「承重层」，2026-08-21 合并）───────────────
            # 一条 project claim 带草稿 = 它进设计期 briefing，且终态时直接
            # 流入晋升三查。**带不带草稿就是唯一信号**，没有平行的 bool 标记：
            # 原来 load_bearing / decision_relevance / capital_* 五个字段维护的
            # 是同一件事的两套记账，且 decision_relevance（"会改变哪类选择"）
            # 与知识卡的 practice（"据此该怎么做"）是同一个问题。
            "card_draft", "card_draft_history",
            "produced_by_experiment_id",
        },
        "lifecycle": {"revision_history",
                      "superseded_by_claim_id", "refuted_by_claim_id",
                       "promoted_to_org_id",   # ★ v3 跨 scope promote tracking
                      # 组织还认不认这条（只在 scope=org 上有）。和 `status` 是两个问题：
                      # `status` 随晋升从项目带过来，说的是**项目**对原结论的判断；
                      # 组织推翻 / 取代一条知识是组织自己的裁定，见 `core/org_corrections`。
                      "org_standing", "org_standing_history"},
    },
    "experiments": {
        "canonical": {
            "experiment_text", "tested_hypothesis_ids",
            "setup_structured", "setup_text",
            "frozen_log_chunk_id",
            "run_at", "run_by_run_id",
            "outcome", "wall_time_seconds", "cost_estimate",
            "replication_index",
        },
        "derived": {"about_concept_ids", "produced_claim_ids"},
        "lifecycle": set(),
    },
    "chunks": {
        "canonical": {
            "text", "source",
            "origin_run_id", "origin_artifact_id", "origin_artifact_frozen",
            # RFC 2026-08-18（artifact 身份+版本）：chunk 是**某一版**的快照。
            # 记版本号 + 内容哈希，"唯一的冻结 prereg chunk"从此机械可答。
            "origin_artifact_version", "origin_content_hash",
            # 自产证据晋升到 org 时记源项目 —— 产物是项目级的，跨不过来，
            # 但"哪个项目的哪一版哪个哈希"跨得过来，链就走得通。
            "origin_project_id",
            "offset", "length",
            "author_concept_ids",            # ★ v3 paper 作者 wiring
            "corresponding_author_concept_id",
        },
        "derived": {
            "referenced_by_claim_ids",
            "referenced_by_experiment_ids",
        },
        # 修订链：同一 artifact 身份注册新版 chunk 时，旧版 chunk 由框架**自动**
        # 打上 superseded_by_chunk_id（内部函数 _kb_register_artifact_as_chunk 里做，
        # 模型零参与也就漏不掉）。行永不删 —— 老实验引用的旧 chunk 仍可溯源。
        "lifecycle": {"superseded_by_chunk_id", "superseded_at"},
    },
}


def canonical_fields(entity: str) -> set[str]:
    return _COMMON_CANONICAL | _FIELDS.get(entity, {}).get("canonical", set())


def derived_fields(entity: str) -> set[str]:
    return _COMMON_DERIVED | _FIELDS.get(entity, {}).get("derived", set())


def lifecycle_fields(entity: str) -> set[str]:
    return _COMMON_LIFECYCLE | _FIELDS.get(entity, {}).get("lifecycle", set())


def all_known_fields(entity: str) -> set[str]:
    return canonical_fields(entity) | derived_fields(entity) | lifecycle_fields(entity)


def classify_field(entity: str, field: str) -> str:
    """返回字段所属段：'canonical' / 'derived' / 'lifecycle' / 'unknown'。"""
    if field in canonical_fields(entity):
        return "canonical"
    if field in derived_fields(entity):
        return "derived"
    if field in lifecycle_fields(entity):
        return "lifecycle"
    return "unknown"


# ─────────────────────────────────────────────────────────────────────────────
# 写入时强校验
# ─────────────────────────────────────────────────────────────────────────────

class SchemaValidationError(ValueError):
    """v3 schema 校验失败。"""


def validate_claim_source(source: str) -> tuple[bool, str]:
    """source 必须是 chunk_id / claim_id（synthesis）/ 外部 URI 之一。

    宿主逻辑（is_external_uri 的内网 host 黑名单是代码不是 pattern），保留手写。
    """
    if not source:
        return False, "source 不能为空"
    if is_chunk_id(source):
        return True, ""
    if is_claim_id(source):
        return True, ""    # synthesis 允许引用其它 claim
    if is_external_uri(source):
        return True, ""
    return False, (
        f"source {source!r} 既不是 chunk_id 也不是 claim_id 也不是外部 URI "
        f"(doi:/arxiv:/https:/pmid:)。本 run 的 artifact 先 freeze_artifact —— "
        f"冻结的返回值里就带 `chunk_id`，填那个。"
    )


# ── 写入契约：每 entity 一份声明式 schema，一次通用校验（判决拆除第三波）─────
#
# 这里曾是四个 validate_* 里 27 处手写 raise —— required / enum / 区间 / 非空 /
# 条件必填各写一遍，其中 concept_type / claim_type / confidence / replication_count
# 与 create_concept / create_claim 工具 schema 是同一声明的两份抄件。现在：
# 字段级约束 = `_ENTITY_SCHEMAS`（shared/lib/schema_check 执行）；
# 条件必填（JSON-schema 的 if/then + anyOf）= `_CONDITIONAL_RULES` 这张小表；
# 宿主逻辑只剩两处：sources 每项走 validate_claim_source，org 出处走
# org_provenance_errors（B 类，账本真实性）。
_COMMON_PROPERTIES: dict = {
    "scope": {"enum": SCOPES},
    "created_by_role": {"enum": CREATED_BY_ROLES},
}

_ENTITY_SCHEMAS: dict[str, dict] = {
    "concepts": {
        "type": "object",
        "required": ["canonical_name", "concept_type", "description"],
        "properties": {
            "canonical_name": {"type": "string", "minLength": 1},
            "concept_type": {"enum": CONCEPT_TYPES},
            "description": {"type": "string", "minLength": 1},
            **_COMMON_PROPERTIES,
        },
    },
    "claims": {
        "type": "object",
        "required": ["claim_text", "claim_type", "confidence"],
        "properties": {
            "claim_text": {"type": "string", "minLength": 1},
            "claim_type": {"enum": CLAIM_TYPES},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "replication_count": {"type": "integer", "minimum": 0},
            "concept_ids": {"type": "array"},
            "sources": {"type": "array"},
            **_COMMON_PROPERTIES,
        },
    },
    "experiments": {
        "type": "object",
        "required": ["experiment_text", "run_at", "outcome"],
        "properties": {
            "experiment_text": {"type": "string", "minLength": 1},
            "outcome": {"enum": EXPERIMENT_OUTCOMES},
            **_COMMON_PROPERTIES,
        },
    },
    "chunks": {
        "type": "object",
        "required": ["text", "source"],
        "properties": {
            "text": {"type": "string", "minLength": 1},
            "source": {"type": "string", "minLength": 1},
            **_COMMON_PROPERTIES,
        },
    },
}

#: 条件必填。每条：`when`（字段等于）/ `unless`（字段不等于）限定适用范围；
#: `any_of` = 这些字段至少一个非空；`min_matching` = (字段, 前缀, n) 该列表里
#: 以该前缀开头的项至少 n 个。`msg` 是给调用方看的那句话（{claim_type} 可插值）。
_CONDITIONAL_RULES: dict[str, list[dict]] = {
    "claims": [
        {"any_of": ("concept_ids", "orphan_reason"),
         "msg": "claim 必须 concept_ids ≥ 1 个，或显式填 orphan_reason。"},
        # dead_end 允许空 sources，但必须 orphan_reason（conjecture 已删，写入端拒收）
        {"unless": {"claim_type": "dead_end"}, "any_of": ("sources", "orphan_reason"),
         "msg": "claim_type={claim_type!r} 必须有 sources ≥ 1 或 orphan_reason 解释为啥没。"},
        {"when": {"claim_type": "hypothesis"},
         "any_of": ("falsification_criteria_structured", "falsification_criteria_text"),
         "msg": "claim_type='hypothesis' 必须有 falsification_criteria_structured "
                "或 falsification_criteria_text 至少其一。"},
        # KB 只收采纳后的假设，prereg_chunk_id 是它的身份锚（_sig_claim），判 C 保留
        {"when": {"claim_type": "hypothesis"}, "any_of": ("prereg_chunk_id",),
         "msg": "claim_type='hypothesis' 必须有 prereg_chunk_id"
                "（= freeze_artifact 冻结 pre_registration 时返回值里的 `chunk_id`）。"},
        {"when": {"claim_type": "hypothesis"}, "any_of": ("predicted_outcome",),
         "msg": "claim_type='hypothesis' 必须有 predicted_outcome 字段（明示预测）。"},
        {"when": {"claim_type": "synthesis"}, "min_matching": ("sources", "claim_", 2),
         "msg": "claim_type='synthesis' 必须 sources 含 ≥ 2 个 claim_id（这是 synthesis 的本质）。"},
        # 文案与判据同源：只查非空（一审删了「≥10 字符」），不再许诺不存在的字数闸
        {"when": {"claim_type": "dead_end"}, "any_of": ("dont_repeat_reason",),
         "msg": "claim_type='dead_end' 必须有 dont_repeat_reason（非空，说清为啥不重试）。"},
    ],
    "experiments": [
        {"any_of": ("setup_structured", "setup_text"),
         "msg": "experiment 必须 setup_structured 或 setup_text 至少其一。"},
    ],
}


def _present(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, dict, set)):
        return len(value) > 0
    return True


def _conditional_errors(entity: str, record: dict) -> list[str]:
    out: list[str] = []
    for rule in _CONDITIONAL_RULES.get(entity, ()):
        if any(record.get(k) != v for k, v in (rule.get("when") or {}).items()):
            continue
        if any(record.get(k) == v for k, v in (rule.get("unless") or {}).items()):
            continue
        ok = True
        if "any_of" in rule:
            ok = any(_present(record.get(k)) for k in rule["any_of"])
        if ok and "min_matching" in rule:
            field, prefix, n = rule["min_matching"]
            ok = sum(1 for x in (record.get(field) or [])
                     if isinstance(x, str) and x.startswith(prefix)) >= n
        if not ok:
            out.append(rule["msg"].format(claim_type=record.get("claim_type")))
    return out


def _validate_common(record: dict) -> None:
    """每个实体校验器都经过的咽喉：org 出处守卫在这里全实体扫盘（B 类，见
    org_provenance_errors）；scope / created_by_role 的 enum 在 _COMMON_PROPERTIES。"""
    if not ORG_PROVENANCE_DEBT_ALLOWED:
        errs = org_provenance_errors(record)
        if errs:
            raise SchemaValidationError("；".join(errs))


def _validate_entity(entity: str, record: dict) -> None:
    errs = schema_errors(_ENTITY_SCHEMAS[entity], record, entity_singular(entity))
    errs += _conditional_errors(entity, record)
    if entity == "claims":
        for src in record.get("sources") or []:
            ok, msg = validate_claim_source(src)
            if not ok:
                errs.append(f"claim.sources 含非法项：{msg}")
    if errs:
        raise SchemaValidationError("；".join(errs))
    _validate_common(record)


def validate_concept(record: dict) -> None:
    _validate_entity("concepts", record)


def validate_claim(record: dict) -> None:
    # ── 两处数字准入闸已删（2026-08-21，KB 两层重构 P5）───────────────────
    # HIGH_TIER 闸（independent_source_count ≥ 2）教模型改 claim_type 过门，而类型是
    # 晋升分道的路由键；cross-project promote 闸全仓零写入方。准入只在晋升侧
    # （core/kb_promotion.py）；project 层是零门禁工作记忆。见 docs/RFC_KB_TWO_TIERS_20260820.md。
    _validate_entity("claims", record)


def validate_experiment(record: dict) -> None:
    _validate_entity("experiments", record)


def validate_chunk(record: dict) -> None:
    _validate_entity("chunks", record)


#: org 记录的晋升出处 —— 缺它就说不清"这条为什么在共享层"。
#:
#: 本机实测（2026-08-20）：org 层 72 条 dogfood 沉积物，无一条说得出自己
#: 从哪来、谁批的。出生一律 project 之后 org 只有晋升通道，出处因此是**可要求
#: 的**：晋升写入必带，写不出就不是晋升，是绕道。
PROMOTION_PROVENANCE_FIELD = "promoted_from"
_PROMOTION_REQUIRED_KEYS = ("project_id", "source_id", "approved_by", "at")

#: 存量欠账：本护栏上线前已在 org 层的记录。清单**只许变短** ——
#: 补出处或降回 project 都算还债；往里加名字意味着又开了一个绕道口。
#: （与 artifact 类型名单同款治理：名单可以存在，维护它的是测试。）
ORG_PROVENANCE_DEBT_ALLOWED = False   # True 仅用于历史数据只读回放


def org_provenance_errors(record: dict) -> list[str]:
    """org 记录的出处完整性检查。返回违规清单（空 = 合格）。"""
    if record.get("scope") != "org":
        return []
    prov = record.get(PROMOTION_PROVENANCE_FIELD)
    if not isinstance(prov, dict) or not prov:
        return [f"org 记录必须带 {PROMOTION_PROVENANCE_FIELD}（从哪个项目、"
                f"哪条源记录、谁批准、何时）—— org 没有出生通道，只有晋升通道"]
    missing = [k for k in _PROMOTION_REQUIRED_KEYS if not prov.get(k)]
    return ([f"{PROMOTION_PROVENANCE_FIELD} 缺字段：{missing}"] if missing else [])


def validate_record(entity: str, record: dict) -> None:
    """统一入口：按 entity 类型校验，不合规抛 SchemaValidationError。

    entity 不在 ENTITIES 里是程序员错误（调用方是 state.write_kb 不是模型），
    直接 KeyError —— 不再单独写一条拒绝文案。
    """
    _validate_entity(entity, record)


# ─────────────────────────────────────────────────────────────────────────────
# 默认值填充（写入前）
# ─────────────────────────────────────────────────────────────────────────────

# evidence-grade confidence ceiling 豁免的 claim_type：这些类型的 confidence 语义
# 不是"经验证据强度"（dead_end=对失败的确信、conjecture/hypothesis=未验证的猜想/
# 待检验预测），不该被 replication/source 上限压。
_CONFIDENCE_CEILING_EXEMPT_TYPES = {"dead_end", "conjecture", "hypothesis"}


# ── v3.5 承重层（load-bearing capital）──────────────────────────────────────
# 两次 E2E 的教训：KB 里 40-50 条 claim，零复用、零自产发现、curator 花 5000 万
# token 管理它们 —— 治理成本超过被治理知识的价值。根因是"什么都记"：没有稀缺，
# 就没有承重感，也没人有义务消费。
#
# 承重层 = 少而硬的决策资本。用**预算制造稀缺**：每项目默认 12 个承重位，满了
# 必须先降级一条才能晋升新的（组合管理，不是无限堆积）。晋升是 curator 的
# 显式动作，不是产出节点写 claim 时的副产品。
#: 一个项目同时能有几张知识卡草稿。稀缺是**设计**：草稿全量注入设计期
#: briefing，不做相关性过滤 —— 挤满了就等于没有重点。
CARD_DRAFT_BUDGET_DEFAULT = 12


def card_draft_source_errors(record: dict) -> list[str]:
    """一条 project claim 够不够格**被起草成知识卡**。返回违规清单。

    卡片正文的完整性由 `kb_promotion.check_deprojectified` 管（域/why/practice/
    confidence_basis 那一套）；这里只管**源 claim 本身的资格**，两条：

      1. 文献转述（literature_reported）不起草：来源是论文本身，一次检索即可
         重新推导，转述层信息量为零。该由书目条目承担这个职责。
      2. 自产观察必须关联实验：否则实验失效时无法级联作废这张卡。

    原来这里还有第三条「decision_relevance ≥30 字符」—— 已删，因为它问的
    正是知识卡 `practice` 字段的问题（"据此该怎么做"）。同一个问题两套记账，
    收敛到卡片那一套。
    """
    errs: list[str] = []
    if record.get("literature_reported") is True:
        errs.append(
            "文献转述类 claim 不起草知识卡：它的来源是论文本身，一次检索即可重新"
            "推导。让书目条目承担该职责；草稿位留给花费真实算力/实验才获得的结论。"
        )
    ct = normalize_claim_type(str(record.get("claim_type") or ""))
    if (ct == "empirical"
            and not record.get("literature_reported")
            and not (record.get("produced_by_experiment_id")
                     or record.get("tested_by_experiment_ids"))):
        errs.append(
            "自产的 empirical 结论要起草成知识卡，必须关联产生它的实验"
            "（produced_by_experiment_id 或 tested_by_experiment_ids）——"
            "否则实验失效时无法级联作废这张卡。"
        )
    return errs


def _evidence_confidence_ceiling(record: dict) -> float | None:
    """按证据强度给 confidence 定上限（None = 不限）。KB 信任校准（v3.3）：

    E2E 审计发现 19 条 claim 平均 confidence 0.887、replication 全 0 —— pilot 观察
    看起来像稳定知识，还能骗过 curator org-promote 的 confidence≥0.85 门。规则：
      - 有 validated 复审 → 豁免（真被独立核验过）
      - 无 source（orphan）→ ≤ 0.5
      - 零复现 且 独立来源 < 2 → ≤ 0.7（单来源 pilot 上限）
    只对经验/派生类 claim 生效（见 _CONFIDENCE_CEILING_EXEMPT_TYPES）。clamp 不 raise。
    """
    ct = record.get("claim_type", "empirical")
    if ct in _CONFIDENCE_CEILING_EXEMPT_TYPES:
        return None
    # v3.4 类别修正（E2E#2 实测 15 次 ceiling 触发全是误伤文献转述）：
    # "论文 X 报告了 Y" 的 source 就是那篇论文，replication 概念不适用 ——
    # 其 confidence 语义是"转述是否忠实"，不是"经验发现是否稳固"。
    # literature_reported 由 create_claim 在工具层机械判定（全部 sources
    # 为外部 URI 或指向外部来源 chunk），schema 层信任该标记。
    if record.get("literature_reported") is True:
        return None
    history = record.get("review_history") or []
    if any(r.get("to_status") == "validated" for r in history):
        return None
    sources = record.get("sources") or []
    if not sources:
        return 0.5
    rc = record.get("replication_count") or 0
    indep = record.get("independent_source_count")
    if not isinstance(indep, int):
        indep = len(sources)
    if int(rc) == 0 and indep < 2:
        return 0.7
    return None


def fill_defaults(entity: str, record: dict, *, now: str | None = None) -> dict:
    """新建 record 时补充必填默认。

    返回 mutated 的 record（同时返同一对象，便于链式）。
    """
    now = now or now_iso()
    record.setdefault("created_at", now)
    record.setdefault("schema_version", 3)

    # scope 智能默认（如果调用方没传）
    if "scope" not in record:
        record["scope"] = smart_default_scope(entity, record)

    # created_by_role 默认 agent_auto（最保守，调用方应显式覆盖）
    record.setdefault("created_by_role", "agent_auto")

    # entity-specific defaults
    if entity == "concepts":
        record.setdefault("status", "active")
        record.setdefault("aliases", [])
        record.setdefault("attributes", {})
        record.setdefault("usage_count", 0)
        record.setdefault("related_to", [])

    elif entity == "claims":
        record.setdefault("confidence", 0.5)
        record.setdefault("replication_count", 0)
        record.setdefault("concept_ids", [])
        record.setdefault("sources", [])
        record.setdefault("scope_dimensions", {})
        record.setdefault("review_history", [])
        # evidence-grade confidence ceiling（v3.3 KB 信任校准）：证据不足时机械压低
        # confidence，避免 pilot claim 冒充稳定知识 / 骗过 org-promote 门。clamp 后
        # 再 derive status，保证 status 与压低后的 confidence 一致。
        _ceiling = _evidence_confidence_ceiling(record)
        if _ceiling is not None:
            try:
                _c = float(record.get("confidence") or 0.5)
            except (TypeError, ValueError):
                _c = 0.5
            if _c > _ceiling:
                record["confidence"] = _ceiling
                record.setdefault("derived", {})
                record["derived"]["confidence_ceiling_applied"] = {
                    "capped_to": _ceiling, "reason": "insufficient_evidence",
                }
        # v3.3 的 org 降级补丁已随出生启发式一并删除 —— 出生一律 project，
        # 没有东西会"自动落 org"，这道补丁防的事在结构上不再发生。
        # status derive
        record.setdefault("status", derive_status(record))

    elif entity == "experiments":
        record.setdefault("review_history", [])
        record.setdefault("about_concept_ids", [])
        record.setdefault("produced_claim_ids", [])
        record.setdefault("tested_hypothesis_ids", [])

    elif entity == "chunks":
        record.setdefault("author_concept_ids", [])
        record.setdefault("referenced_by_claim_ids", [])
        record.setdefault("referenced_by_experiment_ids", [])

    return record


# ─────────────────────────────────────────────────────────────────────────────
# Merge（upsert 时合并 old + new）
# ─────────────────────────────────────────────────────────────────────────────

def _merge_strings(old: str | None, new: str | None) -> str | None:
    new = (new or "").strip()
    old = (old or "").strip()
    if new and (not old or len(new) > len(old)):
        return new
    return old or None


def _merge_list_unique(old: list | None, new: list | None,
                       key: Callable | None = None) -> list:
    out: list = []
    seen: set = set()
    for item in (old or []) + (new or []):
        if key is None:
            k = item if isinstance(item, (str, int, float, bool, type(None))) else repr(item)
        else:
            k = key(item)
        if k in seen:
            continue
        seen.add(k)
        out.append(item)
    return out


def _merge_dict_deep(old: dict | None, new: dict | None) -> dict:
    out = dict(old or {})
    for k, v in (new or {}).items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _merge_dict_deep(out[k], v)
        else:
            out[k] = v
    return out


def merge_kb_record(entity: str, old: dict, new: dict, *, now: str) -> dict:
    """同 id 已存在时的 upsert 合并。新 → old 优先级；list 字段并集；
    confidence 取较高；replication_count 取较大；review_history append。
    """
    merged = dict(old)

    # common audit
    merged["updated_at"] = now
    merged["merge_count"] = (old.get("merge_count") or 0) + 1
    # scope 不变（一旦定了不漂移；要 promote 必走终态扫盘
    # curator_scan(scan_type='org_promotion_candidates')）
    # created_by_role 不变（首次创建的 origin 不漂移）

    if entity == "concepts":
        merged["canonical_name"] = old["canonical_name"]   # canonical 永不改
        merged["concept_type"] = old["concept_type"]
        merged["description"] = _merge_strings(
            old.get("description"), new.get("description"),
        ) or old.get("description")
        merged["aliases"] = _merge_list_unique(
            old.get("aliases"), new.get("aliases"),
        )
        merged["attributes"] = _merge_dict_deep(
            old.get("attributes"), new.get("attributes"),
        )

    elif entity == "claims":
        # RFC 2026-08-18：结构化身份的 hypothesis claim，同 id 重写 = **修订**。
        # 修订必须能替换措辞和证伪判据（否则节点没有确定性修复路径，#395-2），
        # 且旧值进 revision_history、判据变了就把 status 退回 open ——
        # 旧判据下的裁决对新判据不成立，这是机械事实不是判断。
        structural = (
            (old.get("claim_type") == "hypothesis")
            and (old.get("hypothesis_id") or "").strip()
            and (old.get("prereg_artifact_id") or "").strip()
        )
        revisable = ("claim_text", "falsification_criteria_structured",
                     "falsification_criteria_text", "prereg_chunk_id",
                     "predicted_outcome")
        content_changed = structural and any(
            key in new and new.get(key) != old.get(key) for key in revisable
        )
        if content_changed:
            criteria_changed = any(
                key in new and new.get(key) != old.get(key)
                for key in ("falsification_criteria_structured",
                            "falsification_criteria_text")
            )
            entry = {
                "at": now,
                "changed_fields": sorted(
                    k for k in revisable if k in new and new.get(k) != old.get(k)),
                "previous": {k: old.get(k) for k in revisable if old.get(k) is not None},
                "previous_status": old.get("status"),
                "by_run_id": new.get("created_by_run_id"),
            }
            merged.setdefault("revision_history", [])
            merged["revision_history"] = (old.get("revision_history") or []) + [entry]
            for key in revisable:
                if key in new:
                    merged[key] = new[key]
            if criteria_changed:
                merged["status"] = "open"
                merged.setdefault("review_history", old.get("review_history") or [])
                merged["review_history"] = merged["review_history"] + [{
                    "from_status": old.get("status"),
                    "to_status": "open",
                    "reasoning": "证伪判据随预注册修订变更 —— 旧判据下的裁决不再适用，机械重置",
                    "by_run_id": new.get("created_by_run_id"),
                    "at": now,
                }]
            merged["claim_type"] = old["claim_type"]
            merged["concept_ids"] = _merge_list_unique(
                old.get("concept_ids"), new.get("concept_ids"))
            merged["sources"] = _merge_list_unique(
                old.get("sources"), new.get("sources"))
            merged["scope_dimensions"] = _merge_dict_deep(
                old.get("scope_dimensions"), new.get("scope_dimensions"))
            if not criteria_changed:
                merged["review_history"] = (old.get("review_history") or []) +                                             (new.get("review_history") or [])
                merged["status"] = derive_status(merged)
            return merged
        merged["claim_text"] = old["claim_text"]            # canonical
        merged["claim_type"] = old["claim_type"]
        merged["concept_ids"] = _merge_list_unique(
            old.get("concept_ids"), new.get("concept_ids"),
        )
        merged["sources"] = _merge_list_unique(
            old.get("sources"), new.get("sources"),
        )
        # confidence: 取较高
        old_conf = old.get("confidence", 0.5)
        new_conf = new.get("confidence", 0.5)
        merged["confidence"] = max(float(old_conf), float(new_conf))
        # replication_count —— 判据按**出处**分，不是一律取大
        #
        # 取大对"同一个项目把同一条结论重写一遍"是对的（那不是复现）。
        # 但 org 层的合并常常是**两个项目独立得出同一条结论** —— 那正是
        # 跨项目复现，是 org 卡置信度的唯一支撑。一律 max 会把它静默吞掉：
        # 实测两个项目晋升同一条结论后 replication_count 仍是 1，
        # 而第二个项目的出处也丢了。
        #
        # 这也是本平台相对真实实验室的独有能力（RFC §12.1/§13.1）：
        # 系统性的跨项目复现记账。吞掉它等于把最值钱的信号扔了。
        _old_proj = (old.get("promoted_from") or {}).get("project_id")
        _new_proj = (new.get("promoted_from") or {}).get("project_id")
        _cross_project = bool(_old_proj and _new_proj and _old_proj != _new_proj)
        if _cross_project:
            merged["replication_count"] = (
                int(old.get("replication_count", 1) or 1)
                + int(new.get("replication_count", 1) or 1)
            )
            # 出处也要留全 —— "哪些项目独立得出过它"是可审计的事实
            history = list(old.get("promotion_history") or [])
            for prov in (old.get("promoted_from"), new.get("promoted_from")):
                if prov and prov not in history:
                    history.append(prov)
            merged["promotion_history"] = history
        else:
            merged["replication_count"] = max(
                int(old.get("replication_count", 0)),
                int(new.get("replication_count", 0)),
            )
        merged["scope_dimensions"] = _merge_dict_deep(
            old.get("scope_dimensions"), new.get("scope_dimensions"),
        )
        # review_history append
        merged["review_history"] = (old.get("review_history") or []) + \
                                    (new.get("review_history") or [])
        # status re-derive
        merged["status"] = derive_status(merged)

    elif entity == "experiments":
        # experiment id 含 run_at，正常不会重复；如果碰上就保 old
        pass

    elif entity == "chunks":
        merged["text"] = old["text"]
        merged["source"] = old["source"]
        merged["author_concept_ids"] = _merge_list_unique(
            old.get("author_concept_ids"), new.get("author_concept_ids"),
        )

    return merged


# ─────────────────────────────────────────────────────────────────────────────
# Public API summary（给调用方看的）
# ─────────────────────────────────────────────────────────────────────────────

__all__ = [
    # constants
    "ENTITIES", "CONCEPT_TYPES", "CLAIM_TYPES", "CLAIM_STATUSES",
    "CONCEPT_STATUSES", "EXPERIMENT_OUTCOMES", "SCOPES", "CREATED_BY_ROLES",
    # helpers
    "normalize", "sha8", "sha12", "now_iso", "entity_singular", "compute_kb_id",
    "org_provenance_errors", "PROMOTION_PROVENANCE_FIELD",
    "is_external_uri", "is_chunk_id", "is_claim_id", "is_concept_id",
    "is_experiment_id",
    # scope + status
    "smart_default_scope", "derive_status", "can_transition_claim_status",
    "validate_status_flip",
    # fields
    "canonical_fields", "derived_fields", "lifecycle_fields",
    "all_known_fields", "classify_field",
    # validation + defaults + merge
    "SchemaValidationError",
    "validate_record", "validate_concept", "validate_claim",
    "validate_experiment", "validate_chunk", "validate_claim_source",
    "fill_defaults", "merge_kb_record",
]

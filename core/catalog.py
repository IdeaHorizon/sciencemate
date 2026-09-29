"""这个项目产出了什么 —— **一个问题，一个真相源**。

## 缺口（wangd 2026-09-09）

「这个文件管理系统很混乱，比如每个节点一大堆没用的 log，而且还有看不见的
目录。」现场是 qinp 的 Podsys 论文项目：9171 个文件，用户找不到自己那篇论文。

查下来，"这个项目产出了什么"这一个问题，仓库里有**六个**地方各自回答：

    Project files 面   扫 Git 树              → 按路径字母序，切 5000
    Artifacts 面       读后端 artifacts 表    → 只有 publish 过的才有行
    交付物条           读 <state>/…/deliverables/ 的**复制品**（已删）
    Research state 面  读单个 artifact
    会话变更面板       读 change-set
    收尾清单           读账本（core/ledger）

同一篇论文因此可能出现在三处（各有各的名字）或者零处。而它们分叉不报错 ——
分叉的表现只是"用户在这个页面上找不到，去另一个页面又能找到"。

## 这一层是什么

**索引，不是第二套账。** 目录里的每一个字都是现算的：产物记录是盘上的 JSON、
冻结与否问账本、是不是永久交付物问 `artifact_policy`、伴随文件问产物自己的
metadata。没有任何一个字段被存下来 —— 判决不可持久化，只有证据可以
（[[feedback_verdict_vs_evidence]]）。删掉整个目录再重建，结果逐字相同。

因此这里**只组合已有的答案，不新造答案**（[[feedback_call_it_dont_reimplement_it]]）：

    "哪些版本冻了"          core.ledger（账本）
    "这类产物是不是交付物"   shared.lib.artifact_policy（策略表）
    "它带着哪些能看的文件"   core.closing_manifest（扫值，三种路径形状都收）
    "这个目录归谁"          目录名本身（core.project_workspace._NODE_WORKSPACES）

新加一种产物类型时，这里**不需要改**：类型来自记录自己的 `type` 字段，
交付与否来自策略表。写一份类型名单在这里，就是给"新类型默认不显示"埋雷
（[[feedback_guardrails_must_scan_not_list]]）。

## 谁消费它

平台后端经 `harness_contract` 拿它渲染「研究产出」页 —— 那一页替掉上面前三
个各说各话的入口。CLI 的收尾清单继续走 `closing_manifest`（它回答的是另一个
问题："收尾正文里摆出来了没有"）。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field, asdict
from pathlib import Path

log = logging.getLogger("catalog")



def _camel(name: str) -> str:
    head, *rest = name.split("_")
    return head + "".join(part[:1].upper() + part[1:] for part in rest)


@dataclass
class CatalogEntry:
    """一件研究产出。

    `files` 是**用户点得开的东西**（PDF / 图 / bundle），`record_path` 是那份
    JSON 信封。区分两者是因为用户要的是前者：让人去点一个
    `manuscript__podsys.json` 才能知道论文在哪，正是这次要修的毛病。
    """

    artifact_id: str
    #: 产物类型，直接取自记录的 `type` —— 不在这里维护第二份类型名单。
    kind: str
    name: str
    owner_node: str
    version: int
    frozen: bool
    #: 冻结且 permanent = 交付物。两个条件缺一不可：没冻的是草稿，
    #: 非 permanent 的是中间件（`artifact_policy` 说了算）。
    permanent: bool
    #: TIER_DELIVERABLE / TIER_OUTPUT / TIER_WORKING —— 见 `_TIER_BY_RETENTION`。
    tier: str
    #: 工作区相对：那份 JSON 记录。
    record_path: str
    #: 工作区相对：这件产物 metadata 指到的、盘上真的存在的可呈现文件。
    files: list[str] = field(default_factory=list)
    created_at: str = ""
    frozen_at: str = ""
    produced_by_run_id: str = ""

    @property
    def is_deliverable(self) -> bool:
        return self.frozen and self.permanent

    def as_dict(self) -> dict:
        """给界面读的那一份 —— 键名是 camelCase。

        转换是**机械**的（`_camel`），不是一张手写的映射表：映射表加字段时
        会漏，而漏掉的字段在界面上只是"这一列一直是空的"，不报错。
        """
        data = {_camel(key): value for key, value in asdict(self).items()}
        data["isDeliverable"] = self.is_deliverable
        return data


#: 三个分层。名字回答的是"用户现在要看的是不是它"，不是"它值不值钱"。
TIER_DELIVERABLE = "deliverable"   # 冻结且永久：论文、图、预注册、结果表
TIER_OUTPUT = "output"             # 研究产出，但还没冻 / 是证据不是交付物
TIER_WORKING = "working"           # 工作过程与框架内务：收据、快照、压缩日志

#: 分层判据取自策略表**已经声明**的 `retention` / `framework_internal`。
#:
#: 2026-09-09 在一个真项目上量过（`afs-local-deploy` 的 nonnormal_ews）：204 件
#: 产物里 12 件是交付物，而 `compression_log` 26 件、`latex_build_receipt` 18 件、
#: `visual_*` 42 件 —— 用户说的"每个节点一大堆没用的 log"就是它们。
#:
#: 刻意**不**在这里写一份"哪些类型算内务"的名单：那样新类型默认会被当成研究
#: 产出摆到用户面前，而且没人会发现（[[feedback_guardrails_must_scan_not_list]]）。
#: `retention` 恰好就是"这东西值得留多久"的已有声明，一个新类型只要照常声明
#: retention，这里自动分对。
_TIER_BY_RETENTION = {
    "permanent": TIER_OUTPUT,
    "chunk_then_drop": TIER_OUTPUT,
    "transient": TIER_WORKING,
}


def _policy(artifact_type: str) -> dict:
    """问策略表；问不到就当**最保守**的那一档。

    宁可把一件东西排在"工作过程"里（用户展开还找得到），也不要把中间件当成
    交付物端到用户面前 —— 后者是平台在撒谎。
    """
    try:
        from shared.lib import artifact_policy

        return dict(artifact_policy.get_policy(str(artifact_type)) or {})
    except Exception:  # pragma: no cover - 策略表读不到时不该拖垮整页
        log.warning("artifact policy unavailable for %r", artifact_type)
        return {}


def build(worktree: Path | str | None) -> list[CatalogEntry]:
    """这个工作区里的全部研究记录，按"交付物在前"排序。

    读的是账本（`core/ledger`），不是扫盘：正文文件本身没有类型和出处，账本
    才有。冻没冻、第几版、谁产的、伴随文件（metadata 里指到的盘上真存在的
    文件）全从同一处折出来。
    """
    if worktree is None:
        return []
    root = Path(worktree)
    if not root.is_dir():
        return []
    from core import closing_manifest
    from core.ledger import workspace_store

    store = workspace_store(root)
    entries: list[CatalogEntry] = []
    for head in store.heads().values():
        if not (root / head.path).is_file():
            # 账本里有、盘上没有：目录页列一条点开 404 的"交付物"，正是要修的毛病
            # （与 closing_manifest.frozen_deliverables 同一条规则）。
            continue
        kind = head.artifact_type
        policy = _policy(kind)
        is_frozen = bool(head.frozen_version)
        permanent = policy.get("retention") == "permanent"
        if policy.get("framework_internal"):
            tier = TIER_WORKING
        elif is_frozen and permanent:
            tier = TIER_DELIVERABLE
        else:
            tier = _TIER_BY_RETENTION.get(str(policy.get("retention") or ""), TIER_WORKING)
        record_path = head.path
        files = [
            path for path in closing_manifest._companions_of(root / record_path, head.metadata, root)
            if path != record_path
        ]
        entries.append(
            CatalogEntry(
                artifact_id=head.artifact_id,
                kind=kind,
                name=head.name or head.artifact_id,
                owner_node=head.produced_by_node_type,
                version=head.frozen_version if is_frozen else head.version,
                frozen=is_frozen,
                permanent=permanent,
                tier=tier,
                record_path=record_path,
                files=files,
                created_at=head.created_at,
                frozen_at=head.frozen_at,
                produced_by_run_id=head.produced_by_run_id,
            )
        )
    entries.sort(key=_ordering)
    return entries


#: 同一档之内按**读者要什么**排，不是按类型名的字母序。
#:
#: 2026-09-10 本机真机看出来的：一个项目里 `figure` 按字母序排在 `manuscript`
#: 前面，于是"论文"在交付物里排第二；换成那个 264 件产物的真项目，交付物有
#: 8 份 `clean_results` + 3 张图，论文会被压到第 12 行 —— 而用户来这一页就是
#: 找它的。
#:
#: 与 `closing_manifest._PRESENTABILITY` 同一个做法、同一条纪律：**排序不是
#: 排除**。表里没有的类型落在最后一档，仍然在列表里、仍然可达；名单式排除才
#: 会让新类型消失。
_READER_PRIORITY = (
    ("manuscript", "accepted_paper", "paper_pdf", "paper_draft"),  # 论文本身
    ("pre_registration", "search_protocol", "research_plan"),      # 说好要做什么
    ("figure", "figure_package"),                                  # 看得见的结果
    ("clean_results", "analysis_report", "derivation_log"),        # 结果与分析
    ("survey_report", "literature_index"),                         # 背景
)


def _reader_rank(kind: str) -> int:
    for rank, group in enumerate(_READER_PRIORITY):
        if kind in group:
            return rank
    return len(_READER_PRIORITY)


def _ordering(entry: CatalogEntry) -> tuple:
    """交付物在前；同一档之内按读者优先级，再按类型、名字。

    排序**不是**过滤：草稿和中间件照样在列表里，只是排在后面。少列一条，用户
    就会以为那件东西不存在（[[project_deliverables_are_shown]]）。
    """
    rank = {TIER_DELIVERABLE: 0, TIER_OUTPUT: 1, TIER_WORKING: 2}
    return (
        rank.get(entry.tier, 3),
        _reader_rank(entry.kind),
        entry.kind,
        entry.name,
        entry.artifact_id,
    )


def deliverables(entries: list[CatalogEntry]) -> list[CatalogEntry]:
    """冻结且永久 = 交付物。"""
    return [entry for entry in entries if entry.is_deliverable]


def by_tier(entries: list[CatalogEntry]) -> dict[str, list[CatalogEntry]]:
    """按分层分组 —— 三个键恒在，空的也在（"这一档是空的"本身是个答案）。"""
    grouped: dict[str, list[CatalogEntry]] = {
        TIER_DELIVERABLE: [], TIER_OUTPUT: [], TIER_WORKING: [],
    }
    for entry in entries:
        grouped.setdefault(entry.tier, []).append(entry)
    return grouped

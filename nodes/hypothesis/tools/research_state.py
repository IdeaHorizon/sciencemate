"""research_state —— Analysis 节点持有的、版本化的研究状态。

v2.1 拓扑把 hypothesis 升格成 **Analysis**：它不再只是"开头出一份 prereg"，
而是研究计划的持续维护者 + 实验证据的科学裁决者。承载这个职责的是一份
**版本化 artifact**：每一轮 Analysis 出一个新版本，父子成链，改动必须写
理由，假说状态变化必须挂证据。

为什么必须是工具而不是 prompt 约定
-----------------------------------
"每轮都要更新研究状态、不许把上一轮的假说悄悄丢掉、状态从 active 变
supported/refuted 必须给证据" —— 这三句话写进 prompt 属于**倡议**。
E2E 反复证明 prompt 里的"必须"不是机制（hypothesis 的 validate 在 prompt
里写了三遍、56 次工具调用一次没调）。所以判据全部落在这里，机械执行：

1. **版本号由工具算**，不由模型报。模型报的版本号只会错。
2. **父版本里的假说不得凭空消失**。要放弃就显式改成 refuted /
   inconclusive / withdrawn 并给理由，不能从列表里删掉。这是"任务退化"
   在时间维度上的对应物：单轮里靠 must_cover 挡，跨轮里靠这条挡。
3. **active → supported / refuted 必须挂证据**（experiment 产物 id 或
   run id）。没有证据的裁决就是换个地方拍脑袋。
4. **冻结 prereg 里的假说必须全部在册**。prereg 冻结机制管的是"协议不可
   改"，这条管的是"协议不可无视" —— 冻结之后把假说从研究状态里抹掉，
   等价于绕过冻结。**冻结条目的撤回必须写 withdrawn_reason**（S4：预注册
   偏离必须申报）。
5. verdict 只能取四个值之一。

以上是账真与契约（B/C），仍 fail-loud（status=error + 明确缺什么）。
change_reason / 未冻结条目的撤回理由 / ready_candidate 带未裁决条目——
这三条自 2026-08-31 判决拆除批 3w 起**降格为 advisory**：照写入，未过项
如实记进 metadata.advisories 随版本走，reviewer/referee 终审。
"""
from __future__ import annotations

import json
import re
from typing import Any

from core.state import State
from core.tool_registry import ToolDefinition, register_tool

ARTIFACT_TYPE = "research_state"

VERDICTS = ("continue", "pivot", "ready_candidate", "abort")

#: 活跃 = 还没裁决；其余三个是终态裁决；withdrawn 是"人为撤回"的显式出口。
ACTIVE_STATUSES = frozenset({"active"})
RESOLVED_STATUSES = frozenset({"supported", "refuted", "inconclusive"})
STATUSES = frozenset(ACTIVE_STATUSES | RESOLVED_STATUSES | {"withdrawn"})

#: 需要证据才能进入的状态。inconclusive 不要求 —— "做了但没结论"本身
#: 常常正是"证据不足"，强行要求证据会逼出编造。
EVIDENCE_REQUIRED_STATUSES = frozenset({"supported", "refuted"})

_HYPOTHESIS_ID_RE = re.compile(r"\b(H\d+)\b")


# ── 读取既有版本 ──────────────────────────────────────────────────────


#: 单一身份的 name（RFC 2026-08-18）：research_state 是**一个** artifact 身份，
#: 版本由 save_artifact 的版本原语维护 —— 不再用 `name="v{N}"` 造出一个文件
#: 一版的碎片（那是与 prereg 换名碎裂同型的第二套版本习语）。
CANONICAL_NAME = "research_state"
CANONICAL_ID = f"{ARTIFACT_TYPE}__{CANONICAL_NAME}"


def load_versions(state: State) -> list[tuple[int, dict[str, Any]]]:
    """按版本号升序返回 (version, artifact record)。

    读取转发唯一实现 `core/research_state_reader`（一个问题一个真相源 ——
    obligations / verdict_authority / 简报 / 本节点此前各有一份会分叉的抄件）。
    未绑 worktree 的 run（fixture / 单测）退回本 run 的 artifacts 扫描。
    """
    from core import research_state_reader as _rs

    worktree = getattr(state, "project_worktree", None)
    out: dict[int, dict[str, Any]] = {}
    if worktree:
        for record in _rs.iter_records(worktree):
            version = _rs.version_of(record)
            if version >= 1:
                out[version] = record
    else:
        for record in state.artifact_versions(CANONICAL_ID):
            version = _rs.version_of(record)
            if version >= 1:
                out[version] = record
    return sorted(out.items(), key=lambda item: item[0])


def current_state(state: State) -> dict[str, Any] | None:
    versions = load_versions(state)
    return versions[-1][1] if versions else None


def _meta(record: dict[str, Any] | None) -> dict[str, Any]:
    if not record:
        return {}
    meta = record.get("metadata")
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except json.JSONDecodeError:
            return {}
    return meta if isinstance(meta, dict) else {}


def _hypothesis_map(meta: dict[str, Any]) -> dict[str, dict[str, Any]]:
    rows = meta.get("hypotheses")
    out: dict[str, dict[str, Any]] = {}
    if isinstance(rows, list):
        for row in rows:
            if isinstance(row, dict) and str(row.get("id") or "").strip():
                out[str(row["id"]).strip()] = row
    return out


# ── 冻结 prereg 里的假说 id ────────────────────────────────────────────


def frozen_prereg_hypothesis_ids(state: State) -> set[str]:
    """已冻结协议里**要进裁决账的全部条目** —— 就是全部研究问题。

    v0.5：这里原本只扫 `H<数字>`。研究问题成为一等公民之后，没有命题的研究
    （探索 / 表征 / 方法 / 解释 / 复现 / 推导）的条目编号是 `Q1`，按旧判据
    **整条裁决账直接落空**：上一版条目可以凭空消失、带着一堆没答的问题也能
    宣布 ready_candidate、收尾闸不触发。

    改的是**一处定义**，不是加三条检查 —— 下面 `validate_update` 里那三条
    规则一个字没动就同时对新旧格式生效。

    解析走 `core.prereg_commitments`（唯一真相源）；它认新格式，也永远认
    legacy 的 `## Hypothesis N (Hx)`。metadata.hypothesis_ids 若有则优先
    （结构化、权威）。扫盘而不是要求模型申报 —— 申报会漏。
    """
    ids: set[str] = set()
    for entry in state.list_artifacts("pre_registration"):
        # 版本原语：head 可能是未冻结修订草稿 —— 在册假说以该身份**最新冻结版**
        # 为准（草稿的声明不作数，但也不能因为有草稿就把已冻结承诺看丢）。
        record = state.latest_frozen_artifact(str(entry["id"])) or {}
        meta = _meta(record)
        if not meta.get("frozen"):
            continue
        declared = meta.get("hypothesis_ids")
        if isinstance(declared, list) and declared:
            ids.update(str(item).strip() for item in declared if str(item).strip())
            continue
        content = str(record.get("content") or "")
        try:
            from core.prereg_commitments import parse_questions

            parsed = list(parse_questions(content))
        except Exception:
            parsed = []
        ids.update(parsed or _HYPOTHESIS_ID_RE.findall(content))
    return {value for value in ids if value}


# ── 校验 ──────────────────────────────────────────────────────────────


def validate_update(
    *,
    parent_meta: dict[str, Any] | None,
    hypotheses: list[dict[str, Any]],
    verdict: str,
    change_reason: str,
    frozen_ids: set[str],
) -> tuple[list[str], list[str]]:
    """返回 (problems, advisories)。纯函数，便于单测穷举。

    problems 仍拒绝写入（B/C：账真与契约）；advisories 不拦——照写入，
    如实记进 metadata.advisories 随版本走（判决拆除批 3w，
    docs/verdict_demolition/verdicts_writing_nodes.md hypothesis 节）。
    """
    problems: list[str] = []
    advisories: list[str] = []

    if verdict not in VERDICTS:
        problems.append(f"verdict 必须是 {list(VERDICTS)} 之一，收到 {verdict!r}")

    # 「hypotheses 不能为空」已删（判决拆除 research_state.py:176 档一：
    # 非空闸，且与 2026-08-16「假设非必需」演进矛盾）。

    seen: set[str] = set()
    for row in hypotheses:
        hid = str(row.get("id") or "").strip()
        if not hid:
            problems.append("每条 hypothesis 必须有 id（如 H1）")
            continue
        if hid in seen:
            problems.append(f"{hid} 重复出现")
        seen.add(hid)
        status = str(row.get("status") or "").strip()
        if status not in STATUSES:
            problems.append(f"{hid}.status 必须是 {sorted(STATUSES)} 之一，收到 {status!r}")
            continue
        evidence = row.get("evidence")
        evidence_list = [str(x).strip() for x in evidence] if isinstance(evidence, list) else []
        evidence_list = [x for x in evidence_list if x]
        if status in EVIDENCE_REQUIRED_STATUSES and not evidence_list:
            problems.append(
                f"{hid} 判为 {status} 但没给 evidence —— "
                "填 experiment 产物 id 或 run id；无证据的裁决不算裁决"
            )
        if status == "withdrawn" and not str(row.get("withdrawn_reason") or "").strip():
            # 判决拆除 research_state.py:200 拆条：**冻结 prereg 约束的假说**
            # 撤回必须写 reason——升 B 保留（withdrawn_reason 就是 S4 申报机制，
            # 不申报账变假）；未冻结的工作假说降格 H-OB1（如实提醒，不拒绝）。
            if hid in frozen_ids:
                problems.append(
                    f"{hid} 是已冻结 pre_registration 的条目，撤回必须写 "
                    "withdrawn_reason（预注册偏离必须申报）"
                )
            else:
                advisories.append(f"{hid} 撤回未写 withdrawn_reason——建议补上撤回理由")

    if parent_meta is not None:
        if not change_reason.strip():
            # 判决拆除 research_state.py:204 降格→H-OB1。
            advisories.append(
                "有父版本但 change_reason 为空——版本链没有理由等于没有链，建议补写"
            )
        missing = sorted(set(_hypothesis_map(parent_meta)) - seen)
        if missing:
            problems.append(
                "上一版的假说不得凭空消失：" + ", ".join(missing) +
                "。要放弃请显式给 status=withdrawn + withdrawn_reason，"
                "或给出 refuted/inconclusive 裁决"
            )

    missing_frozen = sorted(frozen_ids - seen)
    if missing_frozen:
        problems.append(
            "已冻结 pre_registration 中的假说必须全部在册：" + ", ".join(missing_frozen) +
            "。冻结之后把假说从研究状态里抹掉，等价于绕过冻结"
        )

    if verdict == "ready_candidate":
        # 判决拆除 research_state.py:232 降格→H-OB1：带未裁决条目宣布
        # ready_candidate 不再拒绝——如实记进 advisories，写作面与 referee
        # 都看得见「哪些条目还 active」。
        unresolved = sorted(
            str(row.get("id") or "").strip()
            for row in hypotheses
            if str(row.get("status") or "") in ACTIVE_STATUSES
        )
        if unresolved:
            advisories.append(
                "verdict=ready_candidate 但仍有未裁决的条目：" +
                ", ".join(unresolved) +
                "（supported/refuted/inconclusive/withdrawn 均可为终态）——"
                "该状态差随本版本如实记录，交 reviewer/referee 裁量"
            )

    return problems, advisories


# ── 渲染 ──────────────────────────────────────────────────────────────


def render_markdown(
    *,
    version: int,
    parent_version: int | None,
    verdict: str,
    change_reason: str,
    research_question: str,
    hypotheses: list[dict[str, Any]],
    plan_version: str,
    completed_experiments: list[dict[str, Any]],
    gaps: list[str],
    next_steps: list[str],
) -> str:
    lines = [
        f"# Research State v{version}",
        "",
        f"- **verdict**: `{verdict}`",
        f"- parent: {'v' + str(parent_version) if parent_version else '(首版)'}",
        f"- research_plan: {plan_version or '(未记录)'}",
    ]
    if change_reason.strip():
        lines += ["", "## change_reason", "", change_reason.strip()]
    if research_question.strip():
        lines += ["", "## research_question", "", research_question.strip()]

    lines += ["", "## hypotheses", "", "| id | status | evidence | note |", "|---|---|---|---|"]
    for row in hypotheses:
        evidence = row.get("evidence")
        evidence_text = ", ".join(str(x) for x in evidence) if isinstance(evidence, list) else ""
        note = str(row.get("note") or row.get("withdrawn_reason") or "").replace("|", "\\|")
        lines.append(
            f"| {row.get('id')} | {row.get('status')} | {evidence_text or '—'} | {note or '—'} |"
        )

    if completed_experiments:
        lines += ["", "## completed_experiments", ""]
        for row in completed_experiments:
            ref = row.get("run_id") or row.get("artifact_id") or "?"
            lines.append(
                f"- `{ref}` — credibility: {row.get('credibility', 'unknown')}"
                + (f" — {row['note']}" if row.get("note") else "")
            )
    if gaps:
        lines += ["", "## gaps", ""] + [f"- {item}" for item in gaps]
    if next_steps:
        lines += ["", "## next_steps", ""] + [f"- {item}" for item in next_steps]
    return "\n".join(lines) + "\n"


# ── 工具 ──────────────────────────────────────────────────────────────


async def _update_research_state(
    state: State,
    verdict: str = "",
    hypotheses: list[dict[str, Any]] | None = None,
    change_reason: str = "",
    research_question: str = "",
    plan_version: str = "",
    completed_experiments: list[dict[str, Any]] | None = None,
    gaps: list[str] | None = None,
    next_steps: list[str] | None = None,
    **_: Any,
) -> dict[str, Any]:
    rows = [r for r in (hypotheses or []) if isinstance(r, dict)]
    versions = load_versions(state)
    parent_version, parent_record = versions[-1] if versions else (None, None)
    parent_meta = _meta(parent_record) if parent_record else None
    frozen_ids = frozen_prereg_hypothesis_ids(state)

    problems, advisories = validate_update(
        parent_meta=parent_meta,
        hypotheses=rows,
        verdict=str(verdict or "").strip(),
        change_reason=str(change_reason or ""),
        frozen_ids=frozen_ids,
    )
    if problems:
        return {
            "status": "error",
            "error": "research_state 未通过校验",
            "problems": problems,
            "advisories": advisories,
            "parent_version": parent_version,
            "frozen_prereg_hypotheses": sorted(frozen_ids),
        }

    version = (parent_version or 0) + 1
    experiments = [r for r in (completed_experiments or []) if isinstance(r, dict)]
    gap_list = [str(x) for x in (gaps or []) if str(x).strip()]
    step_list = [str(x) for x in (next_steps or []) if str(x).strip()]

    content = render_markdown(
        version=version,
        parent_version=parent_version,
        verdict=verdict,
        change_reason=change_reason,
        research_question=research_question,
        hypotheses=rows,
        plan_version=plan_version,
        completed_experiments=experiments,
        gaps=gap_list,
        next_steps=step_list,
    )
    metadata = {
        "version": version,
        "parent_version": parent_version,
        "change_reason": change_reason,
        "verdict": verdict,
        "hypotheses": rows,
        "plan_version": plan_version,
        "completed_experiments": experiments,
        "gaps": gap_list,
        "next_steps": step_list,
        "frozen_prereg_hypotheses": sorted(frozen_ids),
        # 降格项的如实记录（判决拆除批 3w）：未过项随版本落盘，reviewer 可见。
        "advisories": advisories,
    }
    # 走 typed 写入口：research_state 是裁决凭据，通用 save_artifact 造不出来。
    from core.artifact_capabilities import save_typed_artifact

    saved = save_typed_artifact(
        state,
        artifact_type=ARTIFACT_TYPE,
        name=CANONICAL_NAME,
        content=content,
        metadata=metadata,
    )

    resolved = sum(1 for r in rows if str(r.get("status")) in RESOLVED_STATUSES)
    result = {
        "status": "success",
        "artifact_id": saved.get("artifact_id") or CANONICAL_ID,
        "version": version,
        "parent_version": parent_version,
        "verdict": verdict,
        "n_hypotheses": len(rows),
        "n_resolved": resolved,
    }
    if advisories:
        result["advisories"] = advisories
    return result


async def _read_research_state(state: State, version: int | None = None, **_: Any) -> dict[str, Any]:
    versions = load_versions(state)
    if not versions:
        return {
            "status": "success",
            "exists": False,
            "message": (
                "本项目还没有 research_state —— 这是第一轮 Analysis。"
                "走完 prereg 冻结后用 update_research_state 建立 v1。"
            ),
        }
    if version is None:
        picked_version, record = versions[-1]
    else:
        matches = [item for item in versions if item[0] == int(version)]
        if not matches:
            return {
                "status": "error",
                "error": f"没有 v{version}",
                "available": [item[0] for item in versions],
            }
        picked_version, record = matches[0]
    meta = _meta(record)
    return {
        "status": "success",
        "exists": True,
        "version": picked_version,
        "available_versions": [item[0] for item in versions],
        "verdict": meta.get("verdict"),
        "hypotheses": meta.get("hypotheses") or [],
        "gaps": meta.get("gaps") or [],
        "next_steps": meta.get("next_steps") or [],
        "completed_experiments": meta.get("completed_experiments") or [],
        "content": str(record.get("content") or ""),
    }


_HYPOTHESIS_ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "id": {
            "type": "string",
            "description": (
                "与 pre_registration 一致的条目 id。命题裁决型 → 假说 id（如 H1）；"
                "非裁决型研究没有假说 → 用 Inquiry Contract 里的问题 id（如 Q1）"
            ),
        },
        "status": {
            "type": "string",
            "enum": sorted(STATUSES),
            "description": "active=未裁决；supported/refuted 需 evidence；inconclusive=做了但无结论；withdrawn 需 withdrawn_reason",
        },
        "evidence": {
            "type": "array",
            "items": {"type": "string"},
            "description": "支撑本次裁决的 experiment 产物 id 或 run id",
        },
        "note": {"type": "string"},
        "withdrawn_reason": {"type": "string"},
    },
    "required": ["id", "status"],
}


register_tool(
    ToolDefinition(
        name="update_research_state",
        description=(
            "写入新一版 research_state（研究状态）。**版本号由工具计算，不要自己报。**\n"
            "机械门禁（拒绝写入）：上一版的假说不得凭空消失（要放弃就显式 "
            "withdrawn；冻结条目还必须写 withdrawn_reason）；假说判为 "
            "supported/refuted 必须给 evidence；已冻结 prereg 里的假说必须全部在册。\n"
            "建议项（不拦写入，如实记进 metadata.advisories）：有父版本时写 "
            "change_reason；ready_candidate 前尽量裁决完所有条目。\n\n"
            "**Use when**：首轮 —— prereg 冻结后建 v1；后续轮 —— 读完 experiment "
            "结果、更新完假说状态之后出新版本。"
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "verdict": {
                    "type": "string",
                    "enum": list(VERDICTS),
                    "description": (
                        "continue=按计划继续；pivot=改方向（须在 change_reason 说明）；"
                        "ready_candidate=证据够了可以进 writing"
                        "（要求 prereg 假说全部已裁决）；abort=终止"
                    ),
                },
                "hypotheses": {
                    "type": "array",
                    "items": _HYPOTHESIS_ITEM_SCHEMA,
                    "description": (
                        "在册条目。有假说就填假说；非命题裁决型研究填 Inquiry "
                        "Contract 的问题（Q1/Q2…），status 表示这个问题答完没有"
                    ),
                },
                "change_reason": {"type": "string", "description": "相对上一版改了什么、为什么"},
                "research_question": {"type": "string"},
                "plan_version": {"type": "string", "description": "当前 research_plan 的版本标识"},
                "completed_experiments": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "run_id": {"type": "string"},
                            "artifact_id": {"type": "string"},
                            "credibility": {"type": "string"},
                            "note": {"type": "string"},
                        },
                    },
                },
                "gaps": {"type": "array", "items": {"type": "string"}},
                "next_steps": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["verdict", "hypotheses"],
        },
        allowed_node_types=["hypothesis"],
    ),
    _update_research_state,
)

register_tool(
    ToolDefinition(
        name="read_research_state",
        description=(
            "读当前（或指定版本的）research_state。**后续轮 Analysis 的第一件事** —— "
            "先知道上一轮判到哪、哪些假说还没裁决，再决定这一轮做什么。"
            "首轮返回 exists=false。"
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "version": {"type": "integer", "description": "省略 = 最新版"},
            },
        },
        allowed_node_types=["hypothesis"],
    ),
    _read_research_state,
)

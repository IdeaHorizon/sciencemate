"""Skill 系统工具（v2.1 重构：skill 独立于 KB）。

工具清单：
  list_skills                  浏览所有已注册 skill
  load_skill                    取某个 skill 的完整正文 / asset（两级加载的 L2）
  search_skill                  按 query / tools_used / concept 搜
  record_skill_usage           记录一次 skill 使用 → skill_usage.jsonl
  skill_usage_stats             查某 skill 的成功率 / 用量
  propose_skill                 写一份 skill 提议到 inbox（runtime / dreaming）
  list_skill_proposals          浏览待审 skill proposals
  accept_skill_proposal         审批通过 → 落 SKILL.md 文件夹（人审之后 curator 调）
  deprecate_skill               把已存在 skill 标 deprecated（改 SKILL.md frontmatter）
  export_skill_to_folder        把 in-memory skill 落成 SKILL.md folder（用于
                                 runtime 临时创建后想 promote）

设计原则：
  - agent **永远不直接** mkdir + 写 SKILL.md；走 propose_skill → 人审 →
    accept_skill_proposal
  - skill 不进 KB
  - usage 记录用 core/skill_usage.py 独立日志
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.skill_registry import (
    Skill, all_skills, clear_registry, get_skill, register_skill,
    visible_skills_for,
)
from core.skill_usage import (
    record_usage as _record_usage,
    usage_stats as _usage_stats,
    all_skill_stats as _all_stats,
)
from core.state import State
from core.tool_registry import ToolDefinition, register_tool


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _org_root() -> Path:
    from core.paths import home as _root  # 「根在哪」一处回答（含 Windows 分支）

    org = Path(os.getenv("HARNESS_FRAMEWORK_ORG_HOME", str(_root() / "org")))
    org.mkdir(parents=True, exist_ok=True)
    return org


def _proposals_path() -> Path:
    return _org_root() / "skill_proposals.jsonl"


def _read_proposals() -> list[dict]:
    p = _proposals_path()
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _append_proposal(rec: dict) -> None:
    p = _proposals_path()
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _rewrite_proposals(records: list[dict]) -> None:
    p = _proposals_path()
    tmp = p.with_suffix(p.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    tmp.replace(p)


def _skill_brief(s: Skill) -> dict:
    return {
        "name": s.name,
        "description": s.description,
        "status": s.status,
        "origin": s.origin,
        "applies_when": s.applies_when,
        "tools_used": s.tools_used,
        "relevant_concepts": s.relevant_concepts,
        "source_dir": s.source_dir,
        "n_assets": len(s.assets),
    }


# ─────────────────────────────────────────────────────────────────────────────
# list_skills
# ─────────────────────────────────────────────────────────────────────────────

async def _list_skills(
    state: State,
    status_filter: str | None = None,
    origin_filter: str | None = None,
    tools_used_contains: str | None = None,
    concept_id: str | None = None,
    for_node: str | None = None,
    include_other_nodes: bool = False,
    **_: Any,
) -> dict:
    """列出可见 skill。

    可见性默认：自动按 caller 的 node_type 过滤（node-local skill 不属本节点的不显示）。
    - for_node：覆盖 caller node_type（用于 orchestrator 帮 owner 看某节点能用啥）
    - include_other_nodes=True：禁用过滤，看所有 node-local skill（调试用）
    """
    if include_other_nodes:
        skills = all_skills()
        if status_filter is None:
            skills = [s for s in skills if s.status != "deprecated"]
    else:
        node_type = for_node or state.node_type
        skills = visible_skills_for(node_type)

    if status_filter:
        skills = [s for s in skills if s.status == status_filter]
    if origin_filter:
        skills = [s for s in skills if s.origin == origin_filter]
    if tools_used_contains:
        t = tools_used_contains.lower()
        skills = [s for s in skills
                    if any(t in (x or "").lower() for x in s.tools_used)]
    if concept_id:
        skills = [s for s in skills if concept_id in s.relevant_concepts]

    return {"status": "success",
            "count": len(skills),
            "skills": [_skill_brief(s) for s in skills],
            # brief 里的 source_dir 是诊断信息，**不是可以 read_file 的位置**
            # （框架目录在 Project 读边界之外）。取正文的合法通道只有一个，
            # 必须在返回里说清楚，否则模型会照着路径去撞墙。
            "note": "以上只是 brief。要正文：load_skill(name='<上面的 name>')"
                     "（source_dir 是框架安装路径，read_file 读不到）。"}


register_tool(
    ToolDefinition(
        name="list_skills",
        description=(
            "浏览**当前节点可见**的 skill（v2.1：folder-based，独立于 KB）。"
            "可见性 = framework + imported + 本节点自己 nodes/<X>/skills/* 下的。"
            "别的节点的 node-local skill 默认不显示。"
            "只返回 brief（不含正文）；正文用 `load_skill(name=...)` 取。"
            "默认隐藏 deprecated。可按 status / origin / tools_used / concept_id 过滤。"
            "for_node 覆盖 caller node_type（orchestrator 用）。"
            "include_other_nodes=true 显示所有（调试用）。"
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "status_filter": {"type": "string",
                                    "enum": ["proposed", "validated", "deprecated"]},
                "origin_filter": {"type": "string",
                                    "description": "framework / imported / node:<name> / runtime"},
                "tools_used_contains": {"type": "string"},
                "concept_id": {"type": "string"},
                "for_node": {"type": "string",
                              "description": "覆盖 caller node_type（看某节点能用啥）"},
                "include_other_nodes": {"type": "boolean", "default": False},
            },
        },
        risk_level="low",
    ),
    _list_skills,
)


# ─────────────────────────────────────────────────────────────────────────────
# load_skill —— 两级加载的 L2（取正文）
# ─────────────────────────────────────────────────────────────────────────────
#
# 为什么正文不能用 read_file 取（2026-08-17 实测）：
#
#   两级加载（#447）把正文换成了一行 `read_file('<框架安装目录>/SKILL.md')`。
#   但 v2.1 的项目读边界（core/project_workspace.resolve_tool_path）规定：
#   绑了 project_worktree 的 run，路径必须落在 run root 或 project root 之内。
#   框架安装目录（shared/skills/、nodes/*/skills/）**永远在边界外** ——
#   于是平台上每一个 project-bound run 的每一个 skill 正文都读不到，
#   而 CLI / fixture run 没绑 project，读侧不设边界，测试因此全绿。
#
#   node20 平台 UI 上 literature 节点连撞两次（systematic_literature_search、
#   literature_classify），模型自述"Skill 文件在项目边界外无法直接读取"后
#   凭理解硬跑 —— 索引在场、正文不可达，等于 skill 只剩标题。
#
# 修法是**换通道**，不是放宽边界：正文本来就在注册表内存里（body_markdown 是
# loader 启动时读进来的），根本不需要再过一次文件系统。给框架安装目录开读
# 白名单会把整棵源码树暴露给模型，代价远大于收益。
#
# assets（examples/ references/ validation/）走同一个工具的 `asset` 参数，
# 且只允许 loader 扫出来的那份清单里的相对路径 —— 不做路径拼接放行，
# 免得这个工具本身变成绕过边界的通道。

_ASSET_DEFAULT_LINES = 800


def _visible_or_error(node_type: str, name: str) -> tuple[Skill | None, dict | None]:
    """取一个对 `node_type` **可见**的 skill；取不到就返回带合法取值的报错。

    报错必须列出合法值：运行时才说"不存在"而不说"有哪些"，模型唯一能做的
    就是猜名字（契约必须送到调用方）。
    """
    visible = visible_skills_for(node_type)
    for s in visible:
        if s.name == name:
            return s, None
    hidden = get_skill(name)
    if hidden is not None:
        return None, {
            "status": "error",
            "error": (
                f"skill {name!r} 存在但对节点 {node_type!r} 不可见"
                f"（origin={hidden.origin}，node-local skill 只对属主节点可见）。"
                f"要看别的节点的 SOP，传 for_node='<那个节点>'（与 list_skills 同参）。"
            ),
        }
    return None, {
        "status": "error",
        "error": (
            f"skill {name!r} 不存在或对节点 {node_type!r} 不可见。可见的 skill："
            + (", ".join(s.name for s in visible) or "（无）")
        ),
    }


async def _load_skill(
    state: State,
    name: str,
    asset: str | None = None,
    offset: int = 0,
    limit: int | None = None,
    for_node: str | None = None,
    **_: Any,
) -> dict:
    """取 skill 正文（默认）或它的某个 asset。"""
    skill, err = _visible_or_error(for_node or state.node_type, name)
    if err is not None:
        return err
    assert skill is not None

    if not asset:
        return {
            "status": "success",
            "name": skill.name,
            "description": skill.description,
            "origin": skill.origin,
            "skill_status": skill.status,
            "applies_when": skill.applies_when,
            "tools_used": skill.tools_used,
            "expected_outcome": skill.expected_outcome,
            "body_markdown": skill.body_markdown,
            "assets": skill.assets,
            "note": (
                f"assets 用 load_skill(name='{skill.name}', asset='<上面清单里的相对路径>') 取。"
                if skill.assets else ""
            ),
        }

    # ── asset 分支 ───────────────────────────────────────────────────────────
    if asset not in skill.assets:
        return {
            "status": "error",
            "error": (
                f"skill {skill.name!r} 没有 asset {asset!r}。它的 asset 清单："
                + (", ".join(skill.assets) or "（无）")
            ),
        }
    if not skill.source_dir:
        return {"status": "error",
                "error": f"skill {skill.name!r} 没有落盘目录（runtime proposal），无 asset 可读。"}
    root = Path(skill.source_dir).resolve()
    target = (root / asset).resolve()
    # 清单是 loader 扫出来的，理论上不会越界；仍然校验一次 —— 这个工具是
    # 读边界的合法出口，它自己失守就等于边界不存在。
    if not (target == root or target.is_relative_to(root)):
        return {"status": "error", "error": f"asset 路径越出 skill 目录：{asset!r}"}
    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return {"status": "error", "error": f"读不了 asset {asset!r}：{exc}"}

    lines = text.splitlines()
    start = max(0, int(offset or 0))
    count = _ASSET_DEFAULT_LINES if limit is None else max(1, int(limit))
    window = lines[start:start + count]
    truncated = start + len(window) < len(lines)
    out = {
        "status": "success",
        "name": skill.name,
        "asset": asset,
        "content": "\n".join(window),
        "offset": start,
        "lines_returned": len(window),
        "total_lines": len(lines),
        "truncated": truncated,
    }
    if truncated:
        # 截断必须自己说出来并给出续读方式，否则模型会把半截当全文。
        out["note"] = (
            f"只返回了第 {start}~{start + len(window)} 行（共 {len(lines)} 行）。"
            f"续读：load_skill(name='{skill.name}', asset='{asset}', "
            f"offset={start + len(window)})"
        )
    return out


register_tool(
    ToolDefinition(
        name="load_skill",
        description=(
            "取一个 skill 的**完整正文**（system_prompt 里的 skill 索引只有用途和"
            "适用场景，正文要用这个工具取）。正文直接来自 skill 注册表，"
            "不经过文件系统 —— skill 装在框架目录里，那是项目读边界之外，"
            "用 read_file 取不到。\n"
            "asset='examples/xxx.md' 取该 skill 自带的 examples/ references/ "
            "validation/ 文件（清单在不带 asset 的返回里）。asset 内容长时按行"
            "分页，用 offset 续读。"
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string",
                          "description": "skill 名（skill 索引或 list_skills 里的 name）"},
                "asset": {"type": "string",
                           "description": "可选：取该 skill 的某个 asset 相对路径，"
                                          "必须来自它自己的 assets 清单"},
                "offset": {"type": "integer",
                            "description": "asset 分页起始行（0 开始），只对 asset 有效"},
                "limit": {"type": "integer",
                           "description": f"asset 单次返回行数，默认 {_ASSET_DEFAULT_LINES}"},
                "for_node": {"type": "string",
                              "description": "覆盖可见性所用的节点（与 list_skills 同参；"
                                             "reviewer 查被审节点该守哪份 SOP 时用）"},
            },
            "required": ["name"],
        },
        risk_level="low",
    ),
    _load_skill,
)


# ─────────────────────────────────────────────────────────────────────────────
# record_skill_usage
# ─────────────────────────────────────────────────────────────────────────────

async def _record_skill_usage_tool(
    state: State,
    skill_name: str,
    outcome: str,
    reasoning: str = "",
    applied_during: str | None = None,
    **_: Any,
) -> dict:
    if get_skill(skill_name) is None:
        return {"status": "error", "error": f"skill {skill_name!r} 不存在"}
    # outcome 枚举由 skill_admin 的 parameters_schema 声明、派发口核一次；
    # core.skill_usage.record_usage 对同一枚举再抛的 ValueError 因此不可达。
    rec = _record_usage(
        skill_name,
        used_by_run_id=state.run_id,
        used_by_node=state.node_type,
        outcome=outcome,
        applied_during=applied_during,
        reasoning=reasoning,
    )

    stats = _usage_stats(skill_name)
    return {"status": "success",
            "skill_name": skill_name,
            "outcome": outcome,
            "usage_count": stats["usage_count"],
            "success_rate": round(stats["success_rate"], 3)}

# ─────────────────────────────────────────────────────────────────────────────
# skill_usage_stats
# ─────────────────────────────────────────────────────────────────────────────

async def _skill_usage_stats_tool(
    state: State,
    skill_name: str | None = None,
    **_: Any,
) -> dict:
    if skill_name:
        return {"status": "success", "stats": _usage_stats(skill_name)}
    return {"status": "success", "all_stats": _all_stats()}

# ─────────────────────────────────────────────────────────────────────────────
# Skill propose / list-proposals / accept / reject 工具已合并进统一的
# proposals.py（v2.1 瘦身）。本文件保留 _write_skill_md helper 供 proposals.py
# resolve_proposal accepting skill_candidate 时调用。
# ─────────────────────────────────────────────────────────────────────────────


def _write_skill_md(target_dir: Path, skill_data: dict, origin_note: str) -> None:
    """落 SKILL.md 文件到 target_dir/<name>/SKILL.md。"""
    skill_dir = target_dir / skill_data["name"]
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_md = skill_dir / "SKILL.md"

    fm_lines = ["---",
                 f"name: {skill_data['name']}",
                 f"description: {skill_data.get('description', '')}"]
    if skill_data.get("applies_when"):
        fm_lines.append("applies_when:")
        for a in skill_data["applies_when"]:
            fm_lines.append(f"  - {a}")
    if skill_data.get("tools_used"):
        fm_lines.append("tools_used:")
        for t in skill_data["tools_used"]:
            fm_lines.append(f"  - {t}")
    if skill_data.get("expected_outcome"):
        fm_lines.append(f"expected_outcome: {skill_data['expected_outcome']}")
    if skill_data.get("relevant_concepts"):
        fm_lines.append("relevant_concepts:")
        for c in skill_data["relevant_concepts"]:
            fm_lines.append(f"  - {c}")
    fm_lines.append(f"status: {skill_data.get('status', 'validated')}")
    fm_lines.append("---")
    fm_lines.append("")
    fm_lines.append(skill_data.get("body_markdown", ""))
    if origin_note:
        fm_lines.append("")
        fm_lines.append(f"<!-- {origin_note} -->")

    skill_md.write_text("\n".join(fm_lines), encoding="utf-8")


# ─────────────────────────────────────────────────────────────────────────────
# deprecate_skill —— 把已存在 skill 标 deprecated
# ─────────────────────────────────────────────────────────────────────────────

async def _deprecate_skill(
    state: State,
    skill_name: str,
    reasoning: str,
    **_: Any,
) -> dict:
    """改 SKILL.md frontmatter 的 status 为 deprecated。

    reasoning 非空由 skill_admin 的 parameters_schema 声明（minLength:1）。
    """
    s = get_skill(skill_name)
    if s is None:
        return {"status": "error", "error": f"skill {skill_name!r} 不存在"}
    if not s.source_dir:
        return {"status": "error",
                "error": f"skill {skill_name!r} 没有 source_dir（runtime-only），无法落 deprecated"}

    skill_md = Path(s.source_dir) / "SKILL.md"
    if not skill_md.exists():
        return {"status": "error", "error": f"找不到 {skill_md}"}

    # 读、改 status、写回
    text = skill_md.read_text(encoding="utf-8")
    import re
    # 改 frontmatter 里 status: 那行；没有就在 --- 后追加
    new_text, n_sub = re.subn(
        r"^(status:\s*).+$", r"\1deprecated", text, count=1, flags=re.MULTILINE,
    )
    if n_sub == 0:
        # 在第一个 --- 后插入
        new_text = re.sub(
            r"^---\n", "---\nstatus: deprecated\n", text, count=1, flags=re.MULTILINE,
        )

    # 加 deprecation note
    deprecation_note = f"\n\n<!-- deprecated at {_now()} by run {state.run_id}: {reasoning} -->\n"
    new_text = new_text.rstrip() + deprecation_note

    skill_md.write_text(new_text, encoding="utf-8")

    # 更新 in-memory
    s.status = "deprecated"

    return {"status": "success", "skill_name": skill_name,
            "skill_md_path": str(skill_md)}

# ─────────────────────────────────────────────────────────────────────────────
# v1.8 refine: skill_admin —— 合并 record_skill_usage + skill_usage_stats + deprecate_skill
# ─────────────────────────────────────────────────────────────────────────────

_SKILL_ADMIN_ACTIONS = ("record_use", "stats", "deprecate")


async def _skill_admin(
    state: State,
    action: str,
    skill_name: str = "",
    outcome: str = "",
    reasoning: str = "",
    notes: str = "",
    **_: Any,
) -> dict:
    """skill 管理统一入口。`action` 决定操作。

    Args:
      action ∈
        - `record_use`：记一次使用。必填 skill_name + outcome ∈ success/failure/partial。
          可选 notes。
        - `stats`：查统计。skill_name=None 时返全 skill 的 stats，指定时返单个。
        - `deprecate`：标 deprecated。必填 skill_name + reasoning（非空，说清为什么）。

    action 枚举由 parameters_schema 声明、派发口核一次。
    """
    if action == "record_use":
        if not skill_name or not outcome:
            return {"status": "error",
                    "error": "action='record_use' 需要 skill_name + outcome"}
        return await _record_skill_usage_tool(
            state, skill_name=skill_name, outcome=outcome, notes=notes,
        )

    if action == "stats":
        return await _skill_usage_stats_tool(
            state, skill_name=skill_name or None,
        )

    # action == "deprecate"
    if not skill_name:
        return {"status": "error", "error": "action='deprecate' 需要 skill_name"}
    return await _deprecate_skill(
        state, skill_name=skill_name, reasoning=reasoning,
    )


register_tool(
    ToolDefinition(
        name="skill_admin",
        description=(
            "**skill 管理统一入口**。`action` 决定操作：\n\n"
            "  - `record_use`：记一次使用 → skill_usage.jsonl。必填 skill_name + outcome\n"
            "    (success / failure / partial)，可选 notes。\n"
            "  - `stats`：查使用统计 (usage_count / success_rate / last_used_at)。\n"
            "    skill_name=None 时返全 skill 的；指定时返单个。\n"
            "  - `deprecate`：标 skill deprecated（改 SKILL.md frontmatter）。\n"
            "    必填 skill_name + reasoning（非空，说清为什么废弃）。\n\n"
            "查浏览 skill 列表用独立工具 `list_skills`。"
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string", "enum": list(_SKILL_ADMIN_ACTIONS),
                },
                "skill_name": {
                    "type": "string",
                    "description": "record_use / deprecate 必填；stats 可选（None=全部）",
                },
                "outcome": {
                    "type": "string",
                    "enum": ["success", "failure", "partial"],
                    "description": "record_use 必填",
                },
                "notes": {
                    "type": "string",
                    "description": "record_use 可选",
                },
                "reasoning": {
                    "type": "string", "minLength": 1,
                    "description": "deprecate 必填：非空，说清为什么废弃",
                },
            },
            "required": ["action"],
        },
        risk_level="low",
    ),
    _skill_admin,
)


# ═══════════════════════════════════════════════════════════════════════════
# v3.5 skill 主动沉淀：把"反复出现且被真实运行验证过的做法"升级为 skill
# ═══════════════════════════════════════════════════════════════════════════
#
# 为什么需要：三轮 E2E 零 skill 沉淀。机制原因是**根本没有提议 skill 的工具**
# —— `skill_admin` 只有 record_use/stats/deprecate，唯一路径是通用 `propose`
# 配一个没人知道的 `extra.skill` 形状。于是该成为 skill 的执行经验（"搜完必须
# 提取全文""固定轮次强制推进""biber 缺失怎么绕"）全部滞留在 memory prose 里，
# 每个新 run 重新踩。
#
# 复发证据与工具名核对**钉在提案上，不当门**（判决拆除 O9）：这是提案通道，
# 下一步就是人审 inbox —— 「复发够不够」「引用的工具在不在」都是人审一眼要看
# 的事实，框架把它们机械算出来挂在提案上（evidence_strength / unknown_tools /
# evidence_verified），比替人拒掉更强：拒掉之后人什么都看不见。

_SKILL_NAME_PATTERN = r"^[a-z][a-z0-9_]{2,48}$"


def _evidence_strength(n_candidates: int, max_recurrence: int) -> str:
    """复发证据的机械分档，钉在提案上给人审看。"""
    if n_candidates == 0:
        return "none"
    if n_candidates >= 2 or max_recurrence >= 2:
        return "recurrent"
    return "single"


def _referenced_unknown_tools(body: str) -> list[str]:
    """扫 body 里 `backtick` 包裹的疑似工具名，返回注册表里不存在的那些。"""
    import re as _re

    from core.tool_registry import _REGISTRY
    known = set(_REGISTRY.tools)
    # 只看形如 `foo_bar` / `foo_bar(...)` 的 backtick 片段，且必须像工具名
    cands = set()
    for m in _re.findall(r"`([a-z][a-z0-9_]{2,60})\s*\(?", body or ""):
        cands.add(m)
    # 只对"看起来像已注册工具家族"的名字较真：含下划线且不是常见英文短语
    suspicious = {c for c in cands if "_" in c and c not in known}
    # 放过明显不是工具的（路径 / 文件名 / 变量）
    return sorted(c for c in suspicious
                  if not c.endswith((".py", ".md", ".sh", ".json", ".yaml")))


async def _propose_skill_from_memory(
    state: State,
    name: str,
    description: str,
    body_markdown: str,
    source_entries: list[str] | None = None,
    applies_to_nodes: list[str] | None = None,
    **_: Any,
) -> dict:
    """把执行经验升级成 skill 提议（走 inbox，人审后落 SKILL.md）。

    契约（schema 声明、派发口核）：name 合法格式（pattern）、description /
    body_markdown 非空。物理冲突：skill 名已被占用；引用不存在的手册条目。

    钉在提案上给人审的事实（**不拒**）：
      - `evidence_strength`：none（没给 source_entries）/ single（一条、复发计数
        1）/ recurrent（≥2 条不同，或单条复发计数 ≥2）；
      - `evidence_verified`：无 Project worktree 时核对不了手册，记 false；
      - `unknown_tools`：body 里 backtick 引用而注册表里没有的工具名 ——
        幻觉工具名会持续误导所有节点，所以要**显眼地**挂在提案上。
    """
    from core import memory as _M

    if get_skill(name) is not None:
        return {"status": "error", "error": f"skill {name!r} 已存在；要改直接编辑 SKILL.md"}

    ids = [i for i in (source_entries or []) if i]
    evidence_verified = bool(getattr(state, "project_worktree", None))
    max_rec = 0
    if ids and evidence_verified:
        # 定位判据与 memory_forget 同一把尺：唯一匹配，不是长度（调它，别再实现一遍）。
        from core.memory_forget import _resolve as _resolve_entries
        found, missing, ambiguous = _resolve_entries(_M.manual_entries(state), ids)
        if missing or ambiguous:
            return {"status": "error",
                    "error": (f"手册里定位不到这些条目（正文前缀须唯一指向一条）："
                              f"找不到 {missing}；有歧义 {ambiguous}")}
        max_rec = max(int(e.seen or 1) for e in found)
    strength = _evidence_strength(len(set(ids)), max_rec)
    unknown = _referenced_unknown_tools(body_markdown)

    from shared.tools.library.proposals import _propose
    out = await _propose(
        state,
        proposal_type="skill_candidate",
        target_entity="claims",       # skill 层不挂 KB entity，占位
        target_id=f"skill_{name}",
        proposed_action=f"新增 skill {name}",
        reasoning=(f"从 {len(ids)} 条 memory candidate 升级"
                    f"（evidence_strength={strength}, max recurrence={max_rec}）："
                    f"{description[:120]}"),
        extra={
            "skill": {
                "name": name,
                "description": description.strip(),
                "body_markdown": body_markdown.strip(),
                "applies_to_nodes": applies_to_nodes or [],
            },
            "source_entries": ids,
            "evidence_strength": strength,
            "evidence_verified": evidence_verified,
            "recurrence_evidence": {"n_candidates": len(set(ids)),
                                     "max_recurrence_count": max_rec},
            "unknown_tools": unknown,
        },
    )
    if out.get("status") == "success":
        out["evidence_strength"] = strength
        out["evidence_verified"] = evidence_verified
        out["unknown_tools"] = unknown
        notes = []
        if strength != "recurrent":
            notes.append(f"复发证据 {strength}（一次性经验通常留在手册即可）")
        if not evidence_verified:
            notes.append("无 Project worktree，手册条目未核对")
        if unknown:
            notes.append(f"body 引用了注册表里没有的工具：{unknown}——人审会看到")
        if notes:
            out["note"] = "提案已入 inbox；如实钉在提案上：" + "；".join(notes) + "。"
    return out


register_tool(
    ToolDefinition(
        name="propose_skill_from_memory",
        description=(
            "把**反复出现的执行经验**升级成 skill 提议（进 inbox，人审后落 SKILL.md）。\n\n"
            "skill 承担的是「怎么做才不踩坑」的可复用流程知识（与 KB 承重层的"
            "「该选什么方向」互补）。\n\n"
            "**契约**：name 未被占用、小写下划线格式；description / body_markdown "
            "非空（写清触发条件 / 步骤 / 失败信号）。\n"
            "**钉在提案上给人审的事实（不拒）**：source_entries 指向的手册条目"
            "构成多强的复发证据（evidence_strength: none / single / recurrent，"
            "一次性经验通常留在 memory 即可）；body 里 backtick 引用而注册表里"
            "没有的工具名（unknown_tools —— 幻觉工具名会持续误导所有节点）。\n\n"
            "dreaming 时若发现某条 pitfall/workflow 已复发多次且做法明确，用它沉淀。"
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "pattern": _SKILL_NAME_PATTERN,
                          "description": "小写字母开头、[a-z0-9_]、3-49 字符，"
                                         "如 literature_search_depth_gate"},
                "description": {"type": "string", "minLength": 1,
                                "description": "非空：什么场景该用它"},
                "body_markdown": {"type": "string", "minLength": 1,
                                   "description": "非空：触发条件 / 步骤 / 失败信号"},
                "source_entries": {
                    "type": "array", "items": {"type": "string"},
                    "description": "支撑它的手册条目正文前缀（唯一匹配即可，不看长度）；"
                                   "有几条、复发几次会算成 evidence_strength 钉在提案上"},
                "applies_to_nodes": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["name", "description", "body_markdown"],
        },
        allowed_node_types=["_curator"],
    ),
    _propose_skill_from_memory,
)

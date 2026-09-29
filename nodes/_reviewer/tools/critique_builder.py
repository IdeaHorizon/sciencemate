"""compose_review_critique —— review_critique 的增量拼装落盘（#184/#202 建议 5）。

## 为什么需要它（实测背景，qinp 2026-07-26/28 两轮 E2E）

review_critique 此前只有一条落盘路径：把整份 critique JSON（实测 6-8KB）塞进
save_artifact 的单个 `content` 参数。弱端点（glm-5.1 网关实测）的 tool-call
参数序列化随长度劣化：

    args 长度   结果
    ~2400      成功
    5959       截断 @char 5959（Expecting ',' delimiter）
    6457       截断
    7710       截断

四次失败三次都是**审稿工作已全部完成、只在最后落盘一步崩掉** —— run 判
incomplete、review 门卡死，触发了后面 orchestrator 自产 critique 的事故（#202）。
#184 的有界重发能把解析错误喂回去，但模型拿到"column 5959"级别的提示后往往
直接放弃（实测 turn 10 空 stop）。结构性解法只有一个：**别让任何单次调用携带
大参数**。

## 设计

单工具 + action enum（与 task / curator_scan 同 pattern）：

    set_verdict(verdict, confidence, summary)          # 一次，几百字节
    add_concern(severity, title, description, ...)     # 一条一调
    add_strength(text)
    set_scores(scores)                                 # per_dimension_scores
    set_project_synthesis(project_verdict, mode, ...)  # project-level gate fields
    add_actionable_next_step(...)                      # one project step per call
    set_recommended_action(action, target_node, feedback_to_next_run)
    status                                             # 看草稿现状
    finalize(name, artifact_under_review, source_node_type)
    reset

草稿存 state.hook_state（run 内跨 turn 持久）。finalize 由**框架**拼 JSON 并
经 typed persistence boundary 落盘 —— 因此：
  - content 永远是合法 JSON（#202 现象 2 "Markdown critique 判 completed 但
    下游不可用" 在此路径上**由构造保证**不会发生）；
  - metadata 的 verdict / recommended_action / n_concerns 等契约字段自动填写、
    与 content 恒一致（决策包直读，#202 D 的 metadata 回退也永远有料）；
  - provenance 章由 typed boundary 收口自动盖（node_type=_reviewer）；
  - 被审 artifact 的 content hash、version 与 manuscript PDF hash 由框架读取并绑定。

`review_critique` 已是 typed-only 凭证，不存在通用 save_artifact 直写旁路。
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from core.state import State
from core.tool_registry import ToolDefinition, register_tool

_DRAFT_KEY = "_critique_draft"

_ACTIONS = (
    "set_verdict",
    "add_concern",
    "add_strength",
    "set_scores",
    "set_project_synthesis",
    "add_actionable_next_step",
    "set_recommended_action",
    "status",
    "finalize",
    "reset",
)
_VERDICTS = ("approve", "approve_with_revisions", "major_concerns", "block")
_REC_ACTIONS = ("proceed", "revise", "redirect_upstream", "abort",
                "escalate_to_human")
_SEVERITIES = ("critical", "major", "minor")
_PROJECT_VERDICTS = ("ready_to_write", "iterate", "pivot", "abort")
_PROJECT_MODES = ("research", "material")
_PROJECT_STEP_ACTIONS = (
    "redo_experiment",
    "new_hypothesis",
    "deeper_literature",
    "fix_evidence_gap",
    "rescope",
    "flag_duplicate_work",
)


def _review_subject_snapshot(state: State, artifact_id: str) -> dict[str, Any]:
    """Resolve and hash the exact review subject; names are never sufficient."""
    record = state.read_artifact(artifact_id)
    if record is None:
        raise ValueError(f"artifact_under_review not found: {artifact_id}")
    from core.ledger import sha256_text

    content_hash = sha256_text(str(record.get("content") or ""))
    if record.get("content_hash") != content_hash:
        raise ValueError(
            f"artifact_under_review {artifact_id} has a stale record content_hash"
        )

    pdf_hashes: dict[str, str] = {}
    metadata = record.get("metadata") or {}
    variants = metadata.get("pdf_variants") or {}
    if isinstance(variants, dict):
        for name in ("review", "clean"):
            variant = variants.get(name)
            raw = variant.get("pdf_path") if isinstance(variant, dict) else None
            if not isinstance(raw, str) or not raw.strip():
                continue
            path = Path(raw).expanduser()
            if not path.is_absolute():
                worktree = getattr(state, "project_worktree", None)
                candidate = Path(worktree) / path if worktree is not None else None
                if candidate is not None and candidate.is_file():
                    path = candidate
                else:
                    from core.project_workspace import resolve_tool_path

                    path = resolve_tool_path(state, raw)
            if not path.is_file():
                raise ValueError(f"review subject PDF is missing: {path}")
            pdf_hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    # 「manuscript 评审必须同时有 review+clean 两个 PDF」已删（判决拆除批 3w，
    # critique_builder.py:123 档一：重复抄件+让同行评审做不成——变体齐不齐由
    # writing 侧披露，评审绑的是**实际存在**的那些字节）。
    return {
        "artifact_id": artifact_id,
        "artifact_type": record.get("type"),
        "version": record.get("version"),
        "content_hash": content_hash,
        "pdf_variant_sha256": pdf_hashes,
    }


def _draft(state: State) -> dict:
    d = state.hook_state.get(_DRAFT_KEY)
    if not isinstance(d, dict):
        d = {"concerns": [], "strengths": [], "actionable_next_steps": []}
        state.hook_state[_DRAFT_KEY] = d
    else:
        d.setdefault("actionable_next_steps", [])
    return d


def _summarize(d: dict) -> dict:
    """草稿现状（status / 每次调用后回给模型，便于它知道还缺什么）。"""
    missing = []
    if not d.get("verdict"):
        missing.append("verdict（set_verdict）")
    if not (d.get("recommended_action") or {}).get("action"):
        missing.append("recommended_action（set_recommended_action）")
    return {
        "verdict": d.get("verdict"),
        "confidence": d.get("confidence"),
        "n_concerns": len(d.get("concerns") or []),
        "n_critical_concerns": sum(
            1 for c in (d.get("concerns") or [])
            if c.get("severity") == "critical"),
        "n_strengths": len(d.get("strengths") or []),
        "recommended_action": (d.get("recommended_action") or {}).get("action"),
        "project_verdict": d.get("project_verdict"),
        "project_mode": d.get("project_mode"),
        "n_actionable_next_steps": len(d.get("actionable_next_steps") or []),
        "missing_before_finalize": missing,
    }


async def _compose_review_critique(
    state: State,
    action: str,
    # set_verdict
    verdict: str | None = None,
    confidence: float | None = None,
    summary: str = "",
    # add_concern
    severity: str | None = None,
    title: str = "",
    description: str = "",
    suggestion: str = "",
    # add_strength
    text: str = "",
    # set_scores
    scores: dict | None = None,
    # set_project_synthesis
    project_verdict: str | None = None,
    mode: str | None = None,
    user_requirement_summary: str = "",
    current_state_summary: str = "",
    # add_actionable_next_step
    step_action: str | None = None,
    step_target_node: str | None = None,
    step_target_artifact: str | None = None,
    step_why: str = "",
    step_how: str = "",
    blocks_writing: bool | None = None,
    # set_recommended_action
    recommended_action: str | None = None,
    target_node: str | None = None,
    feedback_to_next_run: str = "",
    # finalize
    name: str = "",
    artifact_under_review: str = "",
    source_node_type: str = "",
    **_: Any,
) -> dict:
    # 取值契约归 schema（判决拆除三波）：action / verdict / severity /
    # project_verdict / mode / step_action / recommended_action 的 enum 与
    # confidence 的 [0,1] 区间都声明在 parameters_schema 里，`execute()` 派发口
    # 核一次并把合法值列给模型；工具体内不再手写第二份。
    d = _draft(state)

    if action == "reset":
        state.hook_state[_DRAFT_KEY] = {
            "concerns": [],
            "strengths": [],
            "actionable_next_steps": [],
        }
        return {"status": "success", "hint": "草稿已清空"}

    if action == "status":
        return {"status": "success", "draft": _summarize(d)}

    if action == "set_verdict":
        if verdict is not None:
            d["verdict"] = verdict
        if confidence is not None:
            # 非数字在这里崩成 TypeError/ValueError，派发口回头按 schema 的
            # type=number 报「参数形状不对」；区间由 schema minimum/maximum 核。
            d["confidence"] = float(confidence)
        if summary.strip():
            d["summary"] = summary.strip()
        return {"status": "success", "draft": _summarize(d)}

    if action == "add_concern":
        sev = str(severity or "").lower()
        if not description.strip() and not title.strip():
            return {"status": "error",
                    "error": "concern 至少要有 title 或 description（不接受空喊）"}
        concern = {
            "severity": sev,
            "title": title.strip(),
            "description": description.strip(),
            # summary 与 description 同值：红线判定读 description
            # （decision_package._red_line_reason），metadata 回退读 summary ——
            # 两个消费方历史字段名不一致，双写保证都可读。
            "summary": (title or description).strip(),
        }
        if suggestion.strip():
            concern["suggestion"] = suggestion.strip()
        d["concerns"].append(concern)
        return {"status": "success", "draft": _summarize(d)}

    if action == "add_strength":
        if not text.strip():
            return {"status": "error", "error": "add_strength 需要 text"}
        d["strengths"].append(text.strip())
        return {"status": "success", "draft": _summarize(d)}

    if action == "set_scores":
        # 空 dict 照收：per_dimension_scores 为空在 finalize 时如实记
        # advisory「缺 scores」，不在这里拒绝。
        clean: dict[str, float] = {}
        for k, v in (scores or {}).items():
            try:
                clean[str(k)] = float(v)
            except (TypeError, ValueError):
                return {"status": "error", "error": f"维度 {k!r} 的分值不是数字：{v!r}"}
        d["per_dimension_scores"] = clean
        return {"status": "success", "draft": _summarize(d)}

    if action == "set_project_synthesis":
        # 两个 summary 非空闸降格（判决拆除批 3w，critique_builder.py:294/296
        # → R-OB1 评审完整性）：空着照收，缺项在 finalize 时如实记进 payload
        # 的 advisories，referee/下游可见。
        d["scope"] = "project_synthesis"
        d["project_verdict"] = project_verdict
        d["project_mode"] = mode
        d["user_requirement_summary"] = user_requirement_summary.strip()
        d["current_state_summary"] = current_state_summary.strip()
        return {"status": "success", "draft": _summarize(d)}

    if action == "add_actionable_next_step":
        # why/how 非空闸降格（判决拆除批 3w，critique_builder.py:314 → R-OB1）：
        # 空着照收，finalize 时如实记 advisories。blocks_writing 未按 boolean
        # 声明同样照收、finalize 记 advisory（schema type 派发口不查）。
        d["actionable_next_steps"].append(
            {
                "action": step_action,
                "target_node": (
                    str(step_target_node).strip() or None
                    if step_target_node is not None
                    else None
                ),
                "target_artifact": (
                    str(step_target_artifact).strip() or None
                    if step_target_artifact is not None
                    else None
                ),
                "why": step_why.strip(),
                "how": step_how.strip(),
                "blocks_writing": blocks_writing,
            }
        )
        return {"status": "success", "draft": _summarize(d)}

    if action == "set_recommended_action":
        if recommended_action == "redirect_upstream":
            # #153 的教训在源头挡：缺 target 的 redirect 下游会被降级成 revise，
            # 不如在 reviewer 这里就要求写清楚。
            #
            # 2026-09-17 补：**"写了"还不够，得"写的那个真能关掉这条 flow"**。
            # REDIRECT 授权出去之后，run_node 的绑定/空转计数/闭合全挂在
            # node_owes_post_node_flow(target) 上；服务节点（post_run_flow: none）
            # 满足不了它 → 义务永远关不掉，且空转熔断看不见。yuankk 那条会话就是
            # reviewer 指向 postprocess（figures 服务）后空转 40 轮。
            # 合法值在这里**当场列出来**送到调用方，不靠 harness 正文里举的例子
            # —— 那三个例子（literature / postprocess / analysis）今天全都已经
            # 不可执行了，正说明举例是会烂的，判据必须现算。
            from core.loader import list_harnesses, node_owes_post_node_flow

            _t = str(target_node or "").strip()
            if not _t or not node_owes_post_node_flow(_t):
                try:
                    _legal = sorted(
                        n for n in list_harnesses() if node_owes_post_node_flow(n)
                    )
                except Exception:
                    _legal = []
                _why = (
                    "必须带 target_node（要退回哪个上游节点）" if not _t else
                    f"{_t!r} 是服务节点/系统节点，跑完不进 post-node flow 账本 —— "
                    f"退回它，这条审查义务永远关不掉"
                )
                return {"status": "error",
                        "error": (
                            f"redirect_upstream {_why}。\n"
                            f"可退回的上游只有：{_legal or '（读不到节点清单）'}。\n"
                            f"若症结在某个**服务**（文献检索、数据前处理、出图…），"
                            f"那不是 redirect —— 用 revise 让产出节点自己重新调用它，"
                            f"并把要改什么写进 feedback_to_next_run。"
                        )}
        d["recommended_action"] = {
            "action": recommended_action,
            "target_node": (str(target_node).strip() or None) if target_node else None,
            "feedback_to_next_run": feedback_to_next_run.strip(),
        }
        return {"status": "success", "draft": _summarize(d)}

    # ── finalize ────────────────────────────────────────────────────────────
    # 判决拆除批 3w（_reviewer 节 → R-OB1 评审完整性）：
    #   357（verdict/recommended_action 必须已设）、370（必须 _project）、
    #   381（synthesis/scores 必须已设）、389（ready_to_write 不得有 blocking
    #   step）→ 降格：缺项/矛盾**如实记录随 payload 返回**，referee 终审；
    #   395（iterate 至少一条 blocking step）→ 删（强迫 referee 凭空造阻塞项
    #   ＝墙制造它要防的东西；与 389 一起是开火榜第一 writing-gate 的上游
    #   供给端）。name / artifact_under_review 仍必填（身份，C）。
    if not name.strip():
        return {"status": "error",
                "error": "finalize 需要 name（如 '<source_node_type>_critique_<short_id>'）"}
    if not artifact_under_review.strip():
        return {"status": "error", "error": "finalize requires artifact_under_review"}

    advisories: list[str] = []
    if not d.get("verdict"):
        advisories.append("verdict 未设（set_verdict）——本评审不完整")
    if not (d.get("recommended_action") or {}).get("action"):
        advisories.append("recommended_action 未设（set_recommended_action）——本评审不完整")
    for index, step in enumerate(d.get("actionable_next_steps") or []):
        if not str(step.get("why") or "").strip() or not str(step.get("how") or "").strip():
            advisories.append(
                f"actionable_next_steps[{index}]（{step.get('action')}）缺 why/how"
            )
        if not isinstance(step.get("blocks_writing"), bool):
            advisories.append(
                f"actionable_next_steps[{index}]（{step.get('action')}）"
                f"blocks_writing 未按 boolean 声明（收到 {step.get('blocks_writing')!r}）"
                "——是否阻塞写作未定"
            )

    is_project_synthesis = source_node_type == "_project" or bool(d.get("project_verdict"))
    if is_project_synthesis:
        if source_node_type != "_project":
            advisories.append(
                "带 project_verdict 的 critique 未使用 source_node_type='_project'"
                f"（实际：{source_node_type or '空'}）——下游按 source_node_type 检索"
                "项目综合评审时可能看不到本份"
            )
        if d.get("scope") != "project_synthesis" or not d.get("project_verdict"):
            advisories.append("缺 project_synthesis（set_project_synthesis）")
        if not d.get("per_dimension_scores"):
            advisories.append("缺 scores（set_scores）")
        if not str(d.get("user_requirement_summary") or "").strip():
            advisories.append("user_requirement_summary 为空")
        if not str(d.get("current_state_summary") or "").strip():
            advisories.append("current_state_summary 为空")
        steps = d.get("actionable_next_steps") or []
        blocking_steps = [step for step in steps if step.get("blocks_writing") is True]
        if d.get("project_verdict") == "ready_to_write" and blocking_steps:
            advisories.append(
                "意见矛盾：project_verdict='ready_to_write' 同时存在 "
                f"{len(blocking_steps)} 条 blocks_writing=true step——矛盾如实入账，"
                "referee 终审裁决"
            )
    try:
        review_subject = _review_subject_snapshot(state, artifact_under_review.strip())
    except (OSError, ValueError) as exc:
        return {"status": "error", "error": str(exc)}

    # rubric_source 机械盖章：spec 是否送达是框架事实（context engine 注入时
    # 记在 hook_state），不由模型申报。实测：让模型手写这个块，它把 JSON 写坏
    # —— 自报字段成了废纸，下游（决策包的"未按 owner spec"警示）就瞎了。
    _delivery = state.hook_state.get("_owner_review_spec_delivery") or {}
    rubric_source = {
        "owner_spec_loaded": bool(_delivery.get("owner_spec_loaded")),
        "owner_spec_path": _delivery.get("owner_spec_path"),
        "stamped_by": "framework",
    }
    recommended = d.get("recommended_action") or {}
    review_incomplete = not d.get("verdict") or not recommended.get("action")
    content_obj = {
        "verdict": d.get("verdict"),
        "confidence": d.get("confidence"),
        "summary": d.get("summary", ""),
        "concerns": d["concerns"],
        "strengths": d["strengths"],
        "recommended_action": recommended,
        "rubric_source": rubric_source,
        "review_subject": review_subject,
        # R-OB1：缺项与意见矛盾如实随 payload 走（判决拆除批 3w）。
        "advisories": advisories,
        "_composed_by": "compose_review_critique",   # 审计：框架拼装，非模型单发
    }
    if d.get("per_dimension_scores"):
        content_obj["per_dimension_scores"] = d["per_dimension_scores"]

    project_metadata: dict[str, Any] = {}
    if is_project_synthesis:
        project_metadata = {
            "scope": "project_synthesis",
            "mode": d.get("project_mode"),
            "project_verdict": d.get("project_verdict"),
            "user_requirement_summary": d.get("user_requirement_summary", ""),
            "current_state_summary": d.get("current_state_summary", ""),
            "actionable_next_steps": d.get("actionable_next_steps") or [],
            "scores": d.get("per_dimension_scores") or {},
        }
        content_obj.update(project_metadata)

    n_critical = sum(1 for c in d["concerns"] if c.get("severity") == "critical")
    metadata = {
        "artifact_under_review": artifact_under_review or None,
        "source_node_type": source_node_type or None,
        "verdict": d.get("verdict"),
        "confidence": d.get("confidence"),
        "n_concerns": len(d["concerns"]),
        "n_critical_concerns": n_critical,
        "recommended_action": recommended.get("action"),
        "recommended_target_node": recommended.get("target_node"),
        "owner_spec_loaded": rubric_source["owner_spec_loaded"],  # 机械读取者用
        "review_subject": review_subject,
        "advisories": advisories,
    }
    if review_incomplete:
        # 账真：不完整的评审如实盖章——冻结门读这个字段区分「审过」与
        # 「审了一半」，缺 verdict 的 critique 不冒充完整评审。
        metadata["review_incomplete"] = True
    metadata.update(project_metadata)
    from core.artifact_capabilities import save_typed_artifact

    saved = save_typed_artifact(
        state,
        artifact_type="review_critique",
        name=name.strip(),
        content=json.dumps(content_obj, ensure_ascii=False, indent=2),
        metadata=metadata,
    )
    state.append_transcript(
        "review_critique_composed",
        artifact_id=saved["id"],
        n_concerns=len(d["concerns"]),
        n_critical_concerns=n_critical,
        verdict=d.get("verdict"),
        project_verdict=d.get("project_verdict"),
        advisories=advisories,
    )
    state.hook_state[_DRAFT_KEY] = {
        "concerns": [],
        "strengths": [],
        "actionable_next_steps": [],
    }
    result = {"status": "success", "artifact_id": saved["id"],
              "hint": "review_critique 已落盘（框架拼装，JSON 结构保证合法），草稿已清空"}
    if advisories:
        result["advisories"] = advisories
        result["hint"] += (
            "；注意：本份评审带 " + str(len(advisories)) + " 条缺项/矛盾记录"
            "（advisories 已随 payload 落盘，referee 可见）"
        )
    return result


register_tool(
    ToolDefinition(
        name="compose_review_critique",
        description=(
            "**增量拼装并落盘 review_critique**（推荐路径）。`action` 决定操作：\n"
            "  - `set_verdict`：verdict（approve/approve_with_revisions/major_concerns/block）"
            "+ confidence + summary\n"
            "  - `add_concern`：一条 concern 一次调用（severity=critical/major/minor + "
            "title + description + 可选 suggestion）\n"
            "  - `add_strength`：一条 strength\n"
            "  - `set_scores`：per_dimension_scores（维度 → 1-5）\n"
            "  - `set_project_synthesis`：project_synthesis 专用，写入 project_verdict、"
            "mode、user_requirement_summary、current_state_summary\n"
            "  - `add_actionable_next_step`：project_synthesis 专用，一次添加一条"
            " actionable_next_steps（避免长参数）\n"
            "  - `set_recommended_action`：proceed/revise/redirect_upstream/abort/"
            "escalate_to_human（redirect 必须带 target_node，且目标必须是**科研产出节点** ——\n"
            "  文献检索/数据前处理/出图这类服务节点退不回去，工具会当场列出合法值）\n"
            "  - `status`：看草稿还缺什么\n"
            "  - `finalize`：框架拼 JSON 落盘（name、artifact_under_review 必填；"
            "会绑定该版本正文及 PDF SHA-256）\n"
            "  - `reset`：清空草稿重来\n\n"
            "**为什么用它而不是 save_artifact 整份 JSON**：整份 critique 6-8KB 塞单个"
            "参数，弱端点 tool-call 序列化实测会在 ~6KB 截断 —— 审稿全做完、最后落盘"
            "一步崩掉，run 白跑。本工具每次调用只有几百字节，finalize 由框架拼装，"
            "JSON 结构、metadata 契约字段和审查对象哈希保证合法一致。"
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": list(_ACTIONS)},
                "verdict": {"type": "string", "enum": list(_VERDICTS)},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "summary": {"type": "string"},
                "severity": {"type": "string", "enum": list(_SEVERITIES)},
                "title": {"type": "string"},
                "description": {"type": "string"},
                "suggestion": {"type": "string"},
                "text": {"type": "string"},
                "scores": {"type": "object"},
                "project_verdict": {"type": "string", "enum": list(_PROJECT_VERDICTS)},
                "mode": {"type": "string", "enum": list(_PROJECT_MODES)},
                "user_requirement_summary": {"type": "string"},
                "current_state_summary": {"type": "string"},
                "step_action": {"type": "string", "enum": list(_PROJECT_STEP_ACTIONS)},
                "step_target_node": {"type": "string"},
                "step_target_artifact": {"type": "string"},
                "step_why": {"type": "string"},
                "step_how": {"type": "string"},
                "blocks_writing": {"type": "boolean"},
                "recommended_action": {"type": "string", "enum": list(_REC_ACTIONS)},
                "target_node": {"type": "string"},
                "feedback_to_next_run": {"type": "string"},
                "name": {"type": "string"},
                "artifact_under_review": {"type": "string"},
                "source_node_type": {"type": "string"},
            },
            "required": ["action"],
        },
        allowed_node_types=["_reviewer"],
    ),
    _compose_review_critique,
)

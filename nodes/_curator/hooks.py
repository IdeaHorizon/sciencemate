"""_curator loop hooks。

`curator_integration_receipt`（issue #229）
------------------------------------------
背景（qinp 2026-07-29 实测）：一个 Mode 1 integration curator run 读到了外部
survey artifact，但**没有成功扫描、没写 KB、没产 proposal、最后返回空响应**，
`summary.json` 仍是 `status=completed` / `missing_required_outputs=[]`。
orchestrator 据此把 `pending_curator_integrations` **全部清空**、把所有 pending
flow entry 的 `curator_state` 改成 `done` —— 整合门禁凭一次空跑就开了。

三层 fail-open 叠加：
  1. `_curator` 既无 required_output 也无 quality_check，空响应满足完成条件；
  2. `blank_stop` 分类要求"整轮没有成功工具调用"，本 run 有 13 次只读调用 →
     绕过（#184 的分类判据在这里够不着）；
  3. `_finish_child` 的 integration 分支**连 summary status 都不看**（对比
     dreaming 分支明确要求 completed），且一次返回无条件清空全部 pending。

为什么 receipt 由**框架**生成、不做成"要求 curator 调 save_integration_receipt"：
空转的模型同样不会去调那个工具 —— 那只是把 fail-open 平移一层。框架在 run 结束
机械扫 transcript 得出结论，是唯一不依赖模型配合的路径（同
`high_risk_command_audit` / `structure_file_validation` 的做法）。

合法 no-op 仍然允许：完整扫描后确认零候选是正常结果 —— 但必须**扫过**。
"""
from __future__ import annotations

import json
import logging
from typing import Any

from core.loop_hooks import HookContext, LoopHook, register_loop_hook

log = logging.getLogger("curator.hooks")

# Mode 1 的模式别名（与 shared/tools/run_node.py 的 integration 分支保持一致）
INTEGRATION_MODES = (None, "integration", "mode_1")

# 算作"真整合动作"的写入类工具（KB 写入 / proposal / chunk 登记）
_KB_WRITE_TOOLS = frozenset({
    "create_concept", "create_claim", "create_experiment",
    "update_claim_status", "draft_knowledge_card",
})
_PROPOSAL_TOOLS = frozenset({
    "propose", "propose_skill_from_memory", "memory_note",
})
_READ_TOOLS = frozenset({"read_artifact", "read_external_artifact"})

RECEIPT_EVENT = "curator_integration_receipt"


def _iter_events(state) -> list[dict]:
    p = getattr(state, "transcript_path", None)
    if p is None or not p.exists():
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


def _target_artifact_ids(node_inputs: dict) -> list[str]:
    raw = node_inputs.get("artifact_ids")
    if isinstance(raw, str):
        raw = [raw]
    return [str(x) for x in (raw or []) if str(x).strip()]


def _split_by_integrability(ids: list[str]) -> tuple[list[str], list[str]]:
    """把调用方传来的清单分成「该整合的」和「运行时内务」。

    2026-08-19 实测死锁的根因就在这条缝：本函数此前不存在，`targets` 直接等于
    调用方传的 `artifact_ids`。于是 summarizer 自动落盘的
    `compression_log__compression_turn_21` 被当成整合目标，而
    `scan_artifact_disagreements` 结构上扫不到它（它扫的是 claim 分歧）——
    `n_unintegrated` 恒为 1 → curator_state 恒 pending → 下游永远被拦。
    **框架把一个自己扫不到的东西列成目标，再用一道 fail-closed 的门禁卡住
    「这个目标没被扫」。**

    上游（run_node 建 flow entry、Step 3 提示）已经按同一个判据过滤了；这里再过
    一次不是把规则写两遍 —— 调用的是同一个 `shared.lib.artifact_policy`，而门禁的判据
    不该假设调用方一定守规矩。判据只有一处，检查点可以有多处。

    被排除的**记进回执**（`ignored_non_integrable`），不静默丢弃：调用方传错了
    该看得见，而不是发现"我传了 5 个它只认 4 个"。
    """
    from shared.lib.artifact_policy import integration_targets

    integrable = integration_targets(list(ids))
    keep = set(integrable)
    return integrable, [i for i in ids if i not in keep]


def build_integration_receipt(state, node_inputs: dict | None = None,
                              tool_records: list[dict] | None = None) -> dict:
    """机械扫本 run 的工具调用记录，得出整合完成度收据。

    **数据源必须是 `loop_result.tool_calls`**（每条 `{name, args, result}` 带
    完整结果），不能只读 transcript —— transcript 的 `tool_result` 事件只存
    `result_preview`（截断字符串），拿不到 `scan_artifact_disagreements` 结果里的
    `scanned_artifacts` 列表。踩过一次：先按 transcript 写，永远扫不出已扫目标。
    transcript 仅在没有 tool_records 时兜底（拿 tool_call 的 args 至少算读取）。

    判据（每条都只看确定事实，不看模型自述）：
      - 目标 artifact 是否被成功读取（read_artifact / read_external_artifact
        返回 success，且 id 出现在调用参数或结果里）；
      - 目标 artifact 是否被 `scan_artifact_disagreements` 真正扫到
        （读该工具结果里的 `scanned_artifacts` —— 事故现场第一次扫返回的是
        空列表，正是这里暴露的）；
      - KB 写入 / proposal 次数。

    `n_unintegrated` = 未(读到 且 扫到)的目标数 —— QC 直接比它 <= 0。
    非 integration 模式（dreaming / scheduled）不适用，恒 0。
    """
    ni = node_inputs if isinstance(node_inputs, dict) else {}
    if not ni:
        ni = state.hook_state.get("node_inputs") or {}
    mode = ni.get("mode")
    requested = _target_artifact_ids(ni)
    targets, non_integrable = _split_by_integrability(requested)

    if mode not in INTEGRATION_MODES:
        return {"mode": mode, "applicable": False, "verdict": "not_applicable",
                "n_targets": 0, "n_unintegrated": 0}

    records = list(tool_records or [])
    if not records:
        # 兜底：只有 transcript 时，用 tool_call 事件的 args 尽力还原（拿不到
        # scanned_artifacts —— 所以这条路径下扫描判定会偏保守，即 fail-closed）。
        records = [{"name": e.get("name"), "args": e.get("args") or {}, "result": {}}
                   for e in _iter_events(state) if e.get("event") == "tool_call"]

    read_ok: set[str] = set()
    scanned: set[str] = set()
    n_kb_writes = 0
    n_proposals = 0
    n_tool_calls = len(records)

    for rec in records:
        name = rec.get("name")
        args = rec.get("args") or {}
        result = rec.get("result")
        if isinstance(result, str):
            try:
                result = json.loads(result)
            except json.JSONDecodeError:
                result = {}
        result = result if isinstance(result, dict) else {}
        ok = str(result.get("status", "success")).lower() == "success"

        if name in _KB_WRITE_TOOLS:
            n_kb_writes += 1
        elif name in _PROPOSAL_TOOLS:
            n_proposals += 1

        if name in _READ_TOOLS and ok:
            for key in ("artifact_id", "id"):
                if args.get(key):
                    read_ok.add(str(args[key]))
            if result.get("artifact_id"):
                read_ok.add(str(result["artifact_id"]))
        elif name == "scan_artifact_disagreements" and ok:
            for aid in (result.get("scanned_artifacts") or []):
                scanned.add(str(aid))

    unintegrated = [t for t in targets if t not in read_ok or t not in scanned]

    receipt: dict[str, Any] = {
        "mode": mode or "integration",
        "applicable": True,
        "producer_run_id": ni.get("producer_run_id") or ni.get("trigger_run_id"),
        "trigger_node": ni.get("trigger_node"),
        "target_artifact_ids": targets,
        "requested_artifact_ids": requested,
        # 传进来但不该整合的（压缩日志之类的运行时内务）。记下来，不静默吞掉。
        "ignored_non_integrable": non_integrable,
        "n_targets": len(targets),
        "artifacts_read": sorted(read_ok & set(targets)),
        "artifacts_scanned": sorted(scanned & set(targets)),
        "unintegrated_targets": unintegrated,
        "n_unintegrated": len(unintegrated),
        "n_kb_writes": n_kb_writes,
        "n_proposals": n_proposals,
        "n_tool_calls": n_tool_calls,
    }
    if not targets and non_integrable:
        # 传了东西，但没有一件是该整合的 —— 这不是"空调用"，是"本轮无可整合
        # 产物"。判成失败会让流程卡在一个人工也解不开的条件上（fail-closed 的
        # 门禁必须能回答"怎样才能过"，这里答不出来，因为答案不在人手里）。
        receipt["verdict"] = "ok_nothing_to_integrate"
        receipt["n_unintegrated"] = 0
        receipt["reason"] = (
            f"传入的 {len(non_integrable)} 个 artifact 都是运行时内务"
            f"（{', '.join(non_integrable[:3])}"
            + ("…" if len(non_integrable) > 3 else "")
            + "），没有任何节点声明它们是交付物 —— 本轮无可整合产物，合法 no-op。"
        )
    elif not targets:
        # #229 复核残留（qinp 2026-07-30）：`artifact_ids=[]` 时 targets 为空 →
        # unintegrated 也为空 → n_unintegrated=0 → 门禁白过（还会顺带清 flow）。
        # 空目标的 integration run **什么都整合不了**，必须是不可放行的失败，
        # 不能跟"扫全了确认零候选"的合法 no-op 混为一谈。
        # n_unintegrated=1 让 QC（n_unintegrated <= 0）与 _finish_child 门禁
        # 一起 fail-closed。
        receipt["verdict"] = "empty_targets"
        receipt["n_unintegrated"] = 1
        receipt["reason"] = (
            "本次 integration 调用没有传入任何 artifact_ids —— 空目标整合不了"
            "任何东西，不能当作完成。请带上要整合的 artifact_ids 重新调起 curator。"
        )
    elif unintegrated:
        receipt["verdict"] = "incomplete_scan"
        receipt["reason"] = (
            f"{len(unintegrated)} 个目标 artifact 未完成"
            f"「成功读取 + 被 scan_artifact_disagreements 扫到」："
            f"{', '.join(unintegrated[:3])}"
            + ("…" if len(unintegrated) > 3 else "")
        )
    elif n_kb_writes == 0 and n_proposals == 0:
        # 合法 no-op：扫全了、确认零候选。必须记明，别让它和"没扫"混在一起。
        receipt["verdict"] = "ok_no_op"
        receipt["reason"] = ("全部目标已读取并扫描，未产生 KB 写入 / proposal "
                             "—— 视为「本轮无候选」的合法 no-op。")
    else:
        receipt["verdict"] = "ok"
    return receipt


def _on_end(ctx: HookContext, loop_result: Any) -> None:
    try:
        receipt = build_integration_receipt(
            ctx.state, ctx.state.hook_state.get("node_inputs"),
            tool_records=list(getattr(loop_result, "tool_calls", None) or []))
        ctx.state.hook_state["_curator_integration_receipt"] = receipt
        ctx.state.append_transcript(RECEIPT_EVENT, **receipt)
    except Exception as e:                                   # noqa: BLE001
        log.warning("curator integration receipt 生成失败：%s", e)


curator_integration_receipt = LoopHook(
    name="curator_integration_receipt",
    description=(
        "run 结束机械扫 transcript，产出 Mode 1 integration 完成度收据"
        "（目标 artifact 是否真被读取+扫描、KB 写入/proposal 计数、no-op 原因），"
        f"写 `{RECEIPT_EVENT}` 事件供 QC 与 _finish_child 门禁判定。"
    ),
    on_end=_on_end,
    emits=(RECEIPT_EVENT,),
)
register_loop_hook(curator_integration_receipt)

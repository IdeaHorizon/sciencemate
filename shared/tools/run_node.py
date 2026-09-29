"""run_node / run_nodes_parallel 工具：让 LLM 把任意子节点当作 subagent 调起来。

核心契约：
  - 调起一个子节点 = 起一个独立 agent loop（独立 messages、独立 max_turns）。
  - 父子节点共享 project（同一 project_id → memory + KB 跨节点持久化）。
  - artifact 是显式契约：父在调用时显式传 forward_artifact_ids，子完成后自动
    把 required_output_artifact_types 的产出回填到父的 artifacts/。
  - 安全约束：
      * 父 harness 必须在 callable_nodes 里声明允许调哪些子节点。
      * 递归深度 ≤ MAX_DEPTH（默认 4），防止 LLM 失控自递归。
      * 并行最多 MAX_PARALLEL 个（默认 5），避免一次起几十个把 LLM 配额烧光。

用法（LLM 视角）：
  run_node(node_type="literature", node_inputs={"research_question": "..."},
            forward_artifact_ids=["pre_registration__h1"])
  → 返回 child summary（status、artifacts、final_text_preview 等）。

  run_nodes_parallel(jobs=[
    {"node_type": "literature", "node_inputs": {"research_question": "A"}},
    {"node_type": "literature", "node_inputs": {"research_question": "B"}},
  ])
  → 并行起多个 child，返回 list of summary。一个失败不影响其它。

也可以读取子 run 的非 required_output 产出：
  read_external_artifact(run_id="<child run id>", artifact_id="...")
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4
from typing import Any

from core import run_history
from core.state import State
from core.tool_registry import ToolDefinition, register_tool

log = logging.getLogger("run_node_tool")

MAX_DEPTH = int(os.getenv("HARNESS_FRAMEWORK_MAX_SUBAGENT_DEPTH", "4"))
MAX_PARALLEL = int(os.getenv("HARNESS_FRAMEWORK_MAX_PARALLEL", "5"))


# 「这个节点欠不欠 post-node flow」的唯一真相源在 core/loader 里 —— 下决定的那一端
# （present_decision_package 授权 redirect 目标时）必须问得到同一个函数，否则它就
# 只能校验"目标非空"，校验不了"目标能不能把这条 flow 关掉"。搬家理由见那边的注释。
from core.loader import node_owes_post_node_flow  # noqa: E402

# ── #143 gap 3：run 与 task 生命周期机械绑定 ─────────────────────────────────
# 根因：run_node 不持有 task_id，child incomplete/error/cancelled 后没有任何
# 机制闭合关联 task —— 只能指望 orchestrator LLM 记得手动 update，实测没做到，
# task 永久卡 in_progress，恢复项目时无法从 ledger 判断该阶段是否还在跑。
# 修法：run_node 收可选 task_id，框架在 start / 非 completed 终态时确定性更新。
# 全部 best-effort（无 project_root / 无该 task / 状态非法都不炸，不能让 task
# 记账反噬主流程）。


def _task_start_best_effort(state: State, task_id: str | None, owner_node: str) -> None:
    if not task_id:
        return
    try:
        from shared.tools.library.tasks import _get_task_list

        tl = _get_task_list(state)
        if tl is None:
            return
        t = tl.get(task_id)
        if t is not None and t.status == "pending":
            tl.start(task_id, owner_node)  # 已 in_progress / 别的状态 → 不动
    except Exception as e:
        log.debug("task_start_best_effort(%s) 忽略：%s", task_id, e)


def _task_block_best_effort(state: State, task_id: str | None, reason: str) -> None:
    if not task_id:
        return
    try:
        from shared.tools.library.tasks import _get_task_list

        tl = _get_task_list(state)
        if tl is None:
            return
        t = tl.get(task_id)
        if t is not None and t.status != "completed":
            tl.block(task_id, reason)
    except Exception as e:
        log.debug("task_block_best_effort(%s) 忽略：%s", task_id, e)


# ── #155：post-producing flow 是否"走完并被人工接受" ────────────────────────
# 根因：旧 gate 只看 `decision_state == "pending"`。但 decision_state 以前在
# **呈递** decision package 时就写 done（那时用户还没回答），于是
#   review 挂了 → 呈递 → decision_state=done → 下游 producing 不再被拦
# = "没有有效 critique 也能进下一阶段"的静默路径（qinp #155 现象 3）。
# 现在改成：只要 entry 还在 pending_post_node_flow 里且没走完整链
# （有效 critique → curator 整合 → 人工决策），一律拦下游，并说清卡在哪。
# PROCEED/ABORT 会闭合 entry；REVISE/REDIRECT 会保留成一项已授权、待执行的
# 状态转换，直到替代 producer 真正 completed 且回填 required outputs 才闭合。


def _unresolved_flow_reason(entry: dict) -> str | None:
    """entry 未走完的原因；None = 已走完（不拦）。"""
    r = entry.get("review_state")
    if r == "skipped":  # owner 显式 opt-out review（合法）
        pass
    elif r == "pending":
        return "reviewer 还没跑（review_state=pending）"
    elif r == "failed_awaiting_human":
        return (
            "上次 review 没产出有效 critique，正等人工在 decision package 里选 "
            "RETRY REVIEWER / REVISE / REDIRECT / ABORT"
        )
    elif r == "retry_authorized":
        return "人工已授权 reviewer-only retry，但 retry 还没跑完"
    elif r == "aborted":
        return "人工已 ABORT 本 flow（pipeline 应停止，不要起新 producing 节点）"
    elif r == "done":
        if not entry.get("review_critique_artifact_id"):
            return "review_state=done 但没有 review_critique artifact（信号缺失）"
    else:
        return f"review_state 未知：{r!r}"

    d = entry.get("decision_state")
    if d == "action_authorized":
        return (
            f"人工已授权 {entry.get('authorized_action')!r}，等待启动指定节点 "
            f"{entry.get('authorized_target_node')!r}"
        )
    if d == "action_in_progress":
        return (
            f"{_action_label(entry)} 正在节点 "
            f"{_in_progress_target(entry)!r} 执行"
            f"（第 {entry.get('action_attempt_count') or 1} 次；"
            f"started_at={entry.get('action_started_at')}）"
        )
    if d == "deferred_to_analysis":
        return (
            f"本轮裁决已顺延给 {entry.get('deferred_to_node') or 'hypothesis'}（Analysis）—— "
            "等它读完实验结果、更新 research_state。在那之前不能起别的 producing 节点。"
        )
    if d == "awaiting_manual_edit":
        return "人工选择了 EDIT，等待人工修改并重新作出明确 decision"
    if d != "done":
        return f"人工还没做 decision（decision_state={d!r}）"
    return None


def _unresolved_flow_next_step(entry: dict) -> str:
    """给 orchestrator 的可执行下一步（跟着 _unresolved_flow_reason 走）。"""
    r = entry.get("review_state")
    pid = entry.get("producing_run_id")
    node = entry.get("producing_node")
    aids = (entry.get("artifact_ids") or [])[:5]
    if r == "pending":
        return (
            f"  run_node(node_type='_reviewer', node_inputs={{\n"
            f"      'source_node_type': {node!r},\n"
            f"      'producer_run_id': {pid!r},\n"
            f"      'artifact_id': {(aids[0] if aids else '<artifact id>')!r}}})"
        )
    if r == "failed_awaiting_human":
        return (
            f"  present_decision_package(source_node_type={node!r}, "
            f"producing_run_id={pid!r},\n"
            f"      review_failed_reason="
            f"{(entry.get('review_failed_reason') or '')[:80]!r}, ...)\n"
            f"  → 用户选 [1] RETRY REVIEWER 才能重跑 reviewer；"
            f"选 REVISE/REDIRECT 后必须执行获授权节点；ABORT 才直接关闭本 flow。"
        )
    if r == "retry_authorized":
        return (
            f"  run_node(node_type='_reviewer', ...)  # 人工已授权，直接重跑即可\n"
            f"      source_node_type={node!r}, producer_run_id={pid!r}"
        )
    if r == "aborted":
        return "  人工已 ABORT —— 停止本 pipeline，不要再起 producing 节点。"
    if entry.get("decision_state") == "action_authorized":
        target = entry.get("authorized_target_node")
        feedback = (entry.get("recommended_feedback") or "")[:180]
        return (
            f"  run_node(node_type={target!r}, node_inputs={{...}})\n"
            f"  # authorized_action={entry.get('authorized_action')!r}; "
            f"reviewer_feedback={feedback!r}"
        )
    if entry.get("decision_state") == "action_in_progress":
        return (
            f"  等待节点 {_in_progress_target(entry)!r} 结束，不要重复启动。\n"
            "  # 若该 run 已经不在了（平台重启/被停止），本 entry 会在下一次会话恢复时\n"
            "  #   自动退回可重启状态；也可直接 present_decision_package 重新裁决。"
        )
    if entry.get("decision_state") == "deferred_to_analysis":
        target = entry.get("deferred_to_node") or "hypothesis"
        return (
            f"  run_node(node_type={target!r}, node_inputs={{...}})\n"
            f"  # 裁决已顺延给 Analysis：它读 experiment/ 结果 → 更新假说状态 → 出新一版 research_state"
        )
    if entry.get("decision_state") == "awaiting_manual_edit":
        return "  等待人工完成 EDIT；完成后重新呈递 decision package。"
    return (
        f"  present_decision_package(source_node_type={node!r}, "
        f"producing_run_id={pid!r},\n"
        f"      artifact_ids_produced={aids},\n"
        f"      review_critique_artifact_id={entry.get('review_critique_artifact_id')!r}, ...)"
    )


def _authorized_action_target(entry: dict) -> str | None:
    state_value = entry.get("decision_state")
    if state_value == "deferred_to_analysis":
        # 顺延 ≠ 放行。被授权的下一个 producing 节点**只有** Analysis；
        # 别的（writing / 再来一轮 experiment）照旧拦住 —— 否则"顺延"就
        # 退化成"跳过审查链"，那正是 #151 里我造过的静默路径。
        return str(entry.get("deferred_to_node") or "hypothesis").strip() or None
    if state_value != "action_authorized":
        return None
    target = str(entry.get("authorized_target_node") or "").strip()
    return target or None


# ── 「谁被起来执行这个 flow」只能有一个真相源 ─────────────────────────────
# 起 producing 节点的授权有两个来源：人工 REVISE/REDIRECT（写
# `authorized_target_node`）和框架顺延给 Analysis（只写 `deferred_to_node`）。
# `_authorized_action_target()` 已经把这两者统一成"现在谁可以起"，但闭合
# （`decision_action_completed`）和失败重置（`_reset_authorized_action`）当初
# 各自去读 `authorized_target_node` —— 顺延来的 entry 那个字段永远是 None，
# 于是它进了 action_in_progress 就再也出不来：既不闭合也不重置，下游全部
# producing 节点被"等一个根本不存在的 run"永久拦住（2026-08-17 实测 5.5 小时、
# hypothesis 空转 11 次）。
#
# 这和本文件里 `_delivered` 那处注释是同一个类：**同一个事实推导两遍，两遍
# 就会不一致**。修法一样 —— 起的那一刻把结论机械落到 entry 上，之后所有人读
# 同一个字段。
_ACTION_TARGET_KEY = "action_target_node"


def _in_progress_target(entry: dict) -> str | None:
    """这个 entry 当初实际起的是哪个节点。

    `action_target_node` 是起的时候落的权威值。回退链只为**读旧 entry**存在
    （本次修复之前落盘的 flow entry 没有这个字段）：先认人工授权目标，再认
    顺延目标 —— 两者正是 `_authorized_action_target()` 当时会返回的东西。
    """
    for key in (_ACTION_TARGET_KEY, "authorized_target_node", "deferred_to_node"):
        value = str(entry.get(key) or "").strip()
        if value:
            return value
    return None


def _action_label(entry: dict) -> str:
    """给人看的动作名 —— 顺延没有 authorized_action，别打 None。"""
    action = entry.get("authorized_action")
    if action:
        return f"已授权的 {action!r}"
    if entry.get("deferred_to_node") or entry.get("decision_state") == "deferred_to_analysis":
        return "顺延给 Analysis 的裁决"
    return "已授权的返修"


def _prior_state(entry: dict) -> str:
    """action_in_progress 之前它是什么状态 —— 失败/重启时退回这里。"""
    prior = str(entry.get("action_prior_state") or "").strip()
    if prior:
        return prior
    # 旧 entry 没记：顺延来的看得出来（deferred_to_node 只有顺延路径会写）。
    if entry.get("deferred_to_node"):
        return "deferred_to_analysis"
    return "action_authorized"


# 同一个 flow entry 反复起同一个节点却始终关不掉 —— 这不是进展，是空转。
# 阈值只是兜底：真闭合链修好之后它永远不该触发；触发了就说明又有一条路径
# 让 entry 出不去，那时候要的是当场吵，不是再默默起第 12 次。
_MAX_ACTION_ATTEMPTS = 5


def _find_in_progress_entry(flow: list | None, node_type: str) -> dict | None:
    """flow 里正由 `node_type` 执行的那条 entry —— 闭合与失败重置共用这一份判据。"""
    for entry in flow or []:
        if not isinstance(entry, dict):
            continue
        if (entry.get("decision_state") == "action_in_progress"
                and _in_progress_target(entry) == node_type):
            return entry
    return None


def _reset_authorized_action(
    state: State,
    node_type: str,
    *,
    reason: str,
) -> None:
    """Make a failed authorized action retryable without dropping its review."""
    entry = _find_in_progress_entry(
        state.hook_state.get("pending_post_node_flow"), node_type)
    if entry is None:
        return
    # 退回它**进来时**的状态：人工授权的退回 action_authorized，框架顺延的退回
    # deferred_to_analysis。一律退成 action_authorized 会把"框架顺延"伪装成
    # "人工已授权"，报错文案跟着一起说谎。
    entry["decision_state"] = _prior_state(entry)
    entry["action_last_failure"] = reason[:500]
    state.append_transcript(
        "decision_action_failed",
        producing_run_id=entry.get("producing_run_id"),
        authorized_target_node=node_type,
        restored_state=entry["decision_state"],
        reason=reason[:500],
    )


def recover_interrupted_decision_actions(state: State) -> int:
    """Recover process-local revision work after a process restart.

    ``run_node(background=True)`` children live inside the chat.py process.
    Consequently a persisted ``action_in_progress`` state can never still be
    running after that process has restarted.  Leaving it untouched creates a
    permanent false wait: continuous mode sees "in progress" and correctly
    refuses to launch a duplicate, although no child exists anymore.

    This function is deliberately called only by session-resume entry points,
    not during a live process.  It converts the transient state back to the
    durable authorization, preserving the reviewer feedback and attempt count.
    """
    recovered = 0
    for entry in state.hook_state.get("pending_post_node_flow") or []:
        if not isinstance(entry, dict):
            continue
        if entry.get("decision_state") != "action_in_progress":
            continue
        entry["decision_state"] = _prior_state(entry)
        entry["action_last_failure"] = "parent process interrupted during authorized action"
        entry["action_recovered_at"] = datetime.now(UTC).isoformat()
        state.append_transcript(
            "decision_action_recovered_after_restart",
            producing_run_id=entry.get("producing_run_id"),
            authorized_action=entry.get("authorized_action"),
            authorized_target_node=_in_progress_target(entry),
            restored_state=entry["decision_state"],
            action_attempt_count=entry.get("action_attempt_count", 0),
        )
        recovered += 1
    return recovered


def scan_interrupted_child_runs(state: State, *, scope: str = "lineage") -> list[dict]:
    """扫出本会话血缘内**被打断**的子 run（有 run_start、没 run_end）。

    判据全机械，来源全是盘上的 transcript。`brief_interrupted_child_runs`
    （告知调度器）与 `_resumable_run_for` （派发时自动转续跑）共用这一份 ——
    两处各写一遍判据，迟早分叉。
    """
    runs_root = state.root.parent
    if not runs_root.is_dir():
        return []
    scanned: dict[str, dict] = {}
    for run_dir in runs_root.iterdir():
        transcript = run_dir / "transcript.jsonl"
        if run_dir == state.root or not transcript.is_file():
            continue
        info: dict = {
            "run_id": run_dir.name,
            "node_type": None,
            "task_instance_uuid": None,
            "task_contract_revision": None,
            "task_contract_digest": None,
            "parent_run_id": None,
            "has_end": False,
            "n_tool_calls": 0,
            "paths": [],
            "last_narration": "",
            "has_checkpoint": (run_dir / "messages_checkpoint.json").is_file(),
            "session_id": None,
        }
        try:
            with transcript.open(encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    event = record.get("event")
                    if event == "run_start" and info["node_type"] is None:
                        info["node_type"] = record.get("node_type")
                        info["parent_run_id"] = record.get("parent_run_id")
                        info["session_id"] = record.get("session_id")
                        # 任务身份（#1080 第 5 条）：续跑要拿它逐字比对，
                        # 不能再按 node_type + session 去猜。
                        info["task_instance_uuid"] = record.get("task_instance_uuid")
                        info["task_contract_revision"] = record.get("task_contract_revision")
                        info["task_contract_digest"] = record.get("task_contract_digest")
                    elif event == "run_end":
                        info["has_end"] = True
                        break
                    elif event == "tool_call":
                        info["n_tool_calls"] += 1
                    elif event == "workspace_changed":
                        for path in record.get("paths") or []:
                            if path not in info["paths"]:
                                info["paths"].append(path)
                    elif event == "llm_response":
                        text = str(
                            record.get("content")
                            or record.get("content_preview")
                            or ""
                        ).strip()
                        if text:
                            info["last_narration"] = text[:300]
        except OSError:
            continue
        if info["node_type"]:
            scanned[run_dir.name] = info

    def _descends_from_me(run_id: str) -> bool:
        seen: set[str] = set()
        current = scanned.get(run_id, {}).get("parent_run_id")
        while current and current not in seen:
            if current == state.run_id:
                return True
            seen.add(current)
            current = scanned.get(current, {}).get("parent_run_id")
        return False

    return [
        info for run_id, info in sorted(scanned.items())
        if not info["has_end"]
        and (scope == "all" or _descends_from_me(run_id))
    ]


#: 这些节点的派发**必须**带任务身份（#1080 第 3 条）。
#:
#: experiment 是 issue 点名的那个：它的 scientific/operation 判定只能挂在 run 上，
#: 而续跑、接管、上游重派这三类跨 run 场景没有可比较的任务身份，只能重扫项目现状
#: —— #1052 的无出口拒绝有一半在这里。observation / derivation 同形（它们也会从
#: 全项目冻结 prereg 里自行认领，见 #1097 §3），一并要求。
#:
#: 名单写死在这里是**有意的**：它不是"哪些节点危险"的启发式，是"哪些节点今天会
#: 拿项目现状当授权"的事实清单，加一个节点就该是一次显式的决定。
TASK_IDENTITY_REQUIRED_NODES = frozenset({"experiment", "observation", "derivation"})


def _resolve_task_identity(state, node_type: str,
                           task_instance_uuid: str | None,
                           task_contract_digest: str | None) -> tuple[dict | None, dict | None]:
    """把派发参数里的任务身份解析成 (identity, error)。

    带了 uuid 就必须在合同账本上找得到那条 revision —— 找不到不是"先放行再说"，
    而是这次派发引用了一份不存在的授权。
    """
    uuid = str(task_instance_uuid or "").strip()
    digest = str(task_contract_digest or "").strip()
    if not uuid:
        if node_type in TASK_IDENTITY_REQUIRED_NODES:
            return None, {
                "status": "error",
                "error_code": "task_identity_required",
                "error": (
                    f"派 {node_type} 必须带 task_instance_uuid —— 这一趟在做**哪件事**"
                    "不能靠它自己去扫项目现状认领。\n"
                    "两步：\n"
                    "  1) task(action='create', title='...', description='...') "
                    "拿到 task_instance_uuid；\n"
                    "  2) run_node(node_type=%r, task_instance_uuid='<那个 uuid>', ...)。\n"
                    "已经有任务了就用 task(action='list') 找它的 task_instance_uuid。"
                    % node_type),
                "node_type": node_type,
            }
        return None, None

    project_root = getattr(state, "project_root", None)
    if project_root is None:
        return None, {
            "status": "error",
            "error_code": "task_identity_unavailable",
            "error": "这个 run 没有 project_root，任务账本无处可读（任务是项目级的）。",
        }
    from core.task_contract import TaskContractLog

    log_ = TaskContractLog(Path(project_root) / "tasks")
    revisions = log_.revisions_for(uuid)
    if not revisions:
        return None, {
            "status": "error",
            "error_code": "task_contract_missing",
            "error": (
                f"任务 {uuid[:12]}… 在合同账本上没有任何 revision。"
                "先用 task(action='contract', ...) 写下这一趟要做什么、绑哪份预注册，"
                "再派发 —— 派发引用的是那份合同，不是任务标题。"),
        }
    if digest:
        chosen = log_.get(uuid, digest)
        if chosen is None:
            return None, {
                "status": "error",
                "error_code": "task_contract_digest_unknown",
                "error": (
                    f"合同摘要 {digest[:12]}… 不在任务 {uuid[:12]}… 的链上。"
                    "摘要是精确取一条的唯一方式（这本账没有「取最新」）。"),
            }
    elif len(revisions) == 1:
        chosen = revisions[0]
    else:
        # 分叉了：**不替调用方抽签**。这正是这本账存在的理由。
        return None, {
            "status": "error",
            "error_code": "task_contract_ambiguous",
            "error": (
                f"任务 {uuid[:12]}… 有 {len(revisions)} 条合同 revision，"
                "必须用 task_contract_digest 精确指名一条。\n"
                "候选：" + "、".join(
                    f"{r.digest[:12]}…(rev{r.revision})" for r in revisions[-4:])),
        }
    return {
        "task_instance_uuid": chosen.task_instance_uuid,
        "task_contract_revision": chosen.revision,
        "task_contract_digest": chosen.digest,
    }, None


def _resumable_run_for(state: State, node_type: str,
                       task_identity: dict | None = None) -> dict | None:
    """这个节点类型有没有一个**可续**的被打断 run（有 checkpoint）。

    ## 作用域是**会话**，不是"我的后代"（2026-08-18 实测修正）

    第一版按血缘过滤，实测漏掉了最常见的一种：literature 上次由 orchestrator
    派、这次由 hypothesis 派 —— 从 hypothesis 的视角，那条尸体不是它的后代，
    于是又新开了一条。而"上次那个 literature 还没跑完"跟"这次是谁派的"没有
    关系：它是**这个会话**里未完成的工作。

    判据用 transcript 里的 `session_id`（append_transcript 每条都写），
    精确且机械。CLI 那种没有 session_id 的场景退回血缘判定 —— 那里
    runs 目录是按项目分的，不按会话，不能无差别地认。
    """
    my_session = getattr(state, "session_id", None)
    scanned = scan_interrupted_child_runs(state, scope="all" if my_session else "lineage")
    candidates = [
        info for info in scanned
        if info["node_type"] == node_type
        and info["has_checkpoint"]
        and (not my_session or info["session_id"] == my_session)
    ]
    # ── 续跑要**按任务身份**挑，不按 node_type 猜（#1080 第 5 条）───────────
    #
    # 从前这里只问三件事：同 node_type、同 session、有 checkpoint，然后取最后
    # 一个。续上之后这次的 `node_inputs` 会覆盖进 hook_state，还会提示模型
    # 「不一致就以这条为准」—— 于是**同一个 run_id 先后服务两个不同的任务**。
    # #1052 的翼型会话就是这么让新任务落回旧 Experiment child 的上下文的。
    #
    # 带任务身份时：只认身份**逐字相等**的那些。对不上的不是候选 —— 它是
    # 另一件事的尸体，不是这件事的半成品。
    # 不带身份时（CLI、老 run、还没建任务的派发）：维持原样，否则每一条历史
    # run 都会因为"没有身份"而永远续不上，那是把一条缺省改成了硬失败。
    if task_identity:
        want_uuid = str(task_identity.get("task_instance_uuid") or "")
        want_digest = str(task_identity.get("task_contract_digest") or "")
        candidates = [
            info for info in candidates
            if str(info.get("task_instance_uuid") or "") == want_uuid
            and str(info.get("task_contract_digest") or "") == want_digest
        ]
    return candidates[-1] if candidates else None


def task_identity_mismatch(info: dict, task_identity: dict | None) -> str | None:
    """这条 run 的任务身份和本次派发对得上吗；对不上返回一句人话。

    三个字段逐字相等才算对得上（#1080 验收 5）。`None` = 对得上（或本次派发
    没带身份，那时不比 —— 不比和比过了是两回事，调用方据返回值区分）。
    """
    if not task_identity:
        return None
    pairs = (
        ("task_instance_uuid", "任务身份"),
        ("task_contract_revision", "合同版本"),
        ("task_contract_digest", "合同摘要"),
    )
    for key, label in pairs:
        want = task_identity.get(key)
        got = info.get(key)
        if want is None and got is None:
            continue
        if str(want or "") != str(got or ""):
            return (f"{label}对不上：本次派发是 {str(want or '（无）')[:16]}，"
                    f"那个 run 上记的是 {str(got or '（无）')[:16]}")
    return None


def brief_interrupted_child_runs(state: State) -> int:
    """进程死亡恢复时，把被打断子 run 的**既成事实**机械送达调度器。

    ## 现场（2026-08-18，会话 c9deb4f2）

    curator 执行到第 35 个动作时 worker 被杀（后端重启连带子进程）。恢复后
    调度器只知道"要跑 curator"，不知道上一个 curator **已经注册过 claim**
    —— 于是重新派了一个从头跑，Q1 命题在 KB 里被注册了两次。中断的代价
    不只是浪费，是**不幂等副作用做两遍造成的腐蚀**。

    事实全在盘上（transcript + workspace_changed 记录），只是没人送达 ——
    与 PR#398（截断≠做完）同方向：模型手工拿到事实立刻走对，修法是机械
    送达，不是指望它猜。

    ## 判据与来源（全部机械）

    - "被打断" = transcript 有 run_start、没有 run_end；
    - "是这一会话的后代" = 沿 run_start.parent_run_id 爬到本 orchestrator；
    - "已经做了什么" = tool_call 计数 + workspace_changed 的文件清单
      （不做工具名名单 —— 护栏要扫盘，不要写名单）+ 最后一条 llm_response；
    - 防重复简报：hook_state["briefed_interrupted_child_runs"] 记账。

    注入走 injected_messages —— agent_loop 每轮开头机械 drain，模型必见。
    单个尸体解析失败跳过并留 witness，恢复流程不因corpse损坏而死。
    """
    briefed: list[str] = list(
        state.hook_state.get("briefed_interrupted_child_runs") or []
    )
    my_session = getattr(state, "session_id", None)
    interrupted = [
        info for info in scan_interrupted_child_runs(
            state, scope="all" if my_session else "lineage"
        )
        if info["run_id"] not in briefed
        and (not my_session or info["session_id"] == my_session)
    ]
    if not interrupted:
        return 0

    sections: list[str] = []
    for info in interrupted:
        lines = [
            f"- {info['node_type']}（run {info['run_id']}）：执行了 "
            f"{info['n_tool_calls']} 次工具调用后进程被中断，没有完成记录。"
        ]
        if info["paths"]:
            shown = info["paths"][:12]
            more = len(info["paths"]) - len(shown)
            lines.append(
                "  已落盘的文件改动：" + "、".join(shown)
                + (f"（另有 {more} 个）" if more > 0 else "")
            )
        if info["last_narration"]:
            lines.append(f"  它最后的自述：「{info['last_narration']}」")
        sections.append("\n".join(lines))
        briefed.append(info["run_id"])
        state.append_transcript(
            "interrupted_child_briefed",
            child_run_id=info["run_id"],
            node_type=info["node_type"],
            n_tool_calls=info["n_tool_calls"],
            n_paths=len(info["paths"]),
        )

    state.hook_state["briefed_interrupted_child_runs"] = briefed
    state.hook_state.setdefault("injected_messages", []).append({
        "content": (
            "（系统恢复简报）上次进程中断时，以下子节点跑到一半被杀，"
            "它们**已经落盘的动作不会自动消失**：\n"
            + "\n".join(sections)
            + "\n**你不需要手工处理它们**：再次派发同类节点时，框架会自动"
            "接上那条 run 的上下文续跑（不会从头再来），产物与 KB 状态都还在。"
            "真要放弃某条，用 run_node(..., resume_run_id='fresh') 显式新开。"
        ),
        "source": "system_recovery",
    })
    return len(interrupted)


def _is_allowed_callee(parent_callable_nodes: list[str], child_node_type: str) -> bool:
    if not parent_callable_nodes:
        return False
    if "*" in parent_callable_nodes:
        return True
    return child_node_type in parent_callable_nodes


def _resolve_forward_artifacts(
    parent_state: State,
    artifact_ids: list[str],
) -> tuple[list[dict], list[str]]:
    """读父 state 里的 artifact_ids，转成 upstream_artifacts 入参格式。

    返回 (ok_artifacts, missing_ids)。
    """
    ok: list[dict] = []
    missing: list[dict] = []
    for aid in artifact_ids:
        rec = parent_state.read_artifact(aid)
        if rec is None:
            missing.append({"id": aid, "reason": "找不到这个 id"})
            continue
        # 坏记录进 missing，不抛 —— 而且要带真原因。
        #
        # `read_artifact()` 是 json.loads 裸透传，对记录形状零契约；这里原来
        # 直接 `rec["type"]`，一个手写或半截的产物文件就能让整次派发崩在
        # KeyError 上（2026-08-21 实测，一次 experiment 修复派发作废）。
        #
        # 同一个类的 bug 在**观察侧**修过：`list_artifacts` 缺字段时标
        # `malformed=True`，注释写着"让上层看得见"。但那个标记全仓零消费方 ——
        # 于是坏记录以"type=(unknown)、可转发"的样子出现在模型眼前，模型按 id
        # 转发它，动作侧照炸。观察侧修了，动作侧没跟着扫盘。
        #
        # 理由必须是真的：塞进"找不到这个 id"会让模型去 list_artifacts 核对，
        # 而那里 id 明明在 —— 报错指向假原因，模型只能原地打转。
        # 账本行缺 type/name 时 RecordStore.assemble 仍给出空串键 —— 判值不判键。
        if not rec.get("type") or not rec.get("name"):
            missing.append({
                "id": aid,
                "reason": "记录缺 type/name，不是合法 artifact（账本行可能是手写或写了一半）",
            })
            continue
        ok.append(
            {
                "type": rec["type"],
                "name": rec["name"],
                "content": rec.get("content", ""),
                # Forwarding is a provenance-preserving hop.  Omitting this block
                # makes executor fall back to "produced by the child" and silently
                # launders imported/external material after one run_node call.
                "provenance": rec.get("provenance"),
                "metadata": {
                    **(rec.get("metadata") or {}),
                    "forwarded_from_run_id": parent_state.run_id,
                    "forwarded_from_artifact_id": aid,
                },
            }
        )
    return ok, missing


def _unusable_forward_message(unusable: list[dict], *, auto: bool = False) -> str:
    """把「这些 id 用不了」说清楚 —— 分别说明每一条为什么。

    合成一句笼统的"找不到"会把两种完全不同的处境混成一种：id 真的不存在
    （去 list_artifacts 换一个）vs 文件在但记录是坏的（换 id 没用，得重新
    产出）。后者被说成前者时，模型会去核对 id、发现 id 明明在，然后原地打转。
    """
    lines = [f"  - {u['id']}：{u['reason']}" for u in unusable]
    head = ("自动选中的上游产物里有用不了的" if auto
            else "forward_artifact_ids 里有用不了的")
    tail = ("这几份不是靠换 id 能解决的 —— 记录坏了就得让产出它的节点重跑；"
            "id 不存在则先 list_artifacts 看现有产物。")
    return f"{head}：\n" + "\n".join(lines) + f"\n{tail}"


def _auto_resolve_required_inputs(
    parent_state: State,
    required_types: list[str],
) -> tuple[list[str], dict[str, list[str]], list[str]]:
    """根据子节点 required_input_artifact_types，从父 state 自动选 artifact 转发。

    每个 required type 选**最新创建的**一个 artifact（按 created_at 倒序）。

    返回:
      auto_ids        ：自动选定的 artifact_id 列表（按 required type 顺序）
      ambiguous       ：{type: [候选 id 列表]} —— 同一 type 多个候选时
      missing_types   ：父 state 找不到任何匹配的 type 列表
    """
    auto_ids: list[str] = []
    ambiguous: dict[str, list[str]] = {}
    missing_types: list[str] = []

    if not required_types:
        return auto_ids, ambiguous, missing_types

    # 按 type 分组，**直接用 list_artifacts 的顺序**，不在这里重排一次。
    #
    # `State.list_artifacts()` 的 docstring 把顺序写进了契约：「按 created_at
    # 升序，末位 = 最新」，其排序键是 `_artifact_order_key` =
    # `(created_at, 文件名)` —— 第二元的存在理由就写在它自己的 docstring 里：
    # **「保证同秒登记时顺序仍然确定」**。
    #
    # 这里原本自己又排了一次，只按 `created_at`，把那个 tiebreak 丢了。后果不是
    # "顺序随机"，是**稳定地取到最旧的那个**：Python 的 sort 稳定，相等元素保持
    # 原顺序（也就是 list_artifacts 的升序），`reverse=True` 不会反转相等元素，
    # 于是 `candidates[0]` 正好是最旧的一份 —— 与本函数的语义完全相反。
    # CI 上表现为 `test_auto_resolve_picks_latest_on_ambiguity` 偶发红。
    #
    # 这是"一个问题一个真相源"的标准病例：同一个问题（怎么给产物定时序）有两份
    # 答案，其中一份修好了（core/state 加了第二元），另一份没跟着修，而两边都不
    # 报错。修法是让这一份**调用**那一份，不是把 tiebreak 再抄一遍。
    by_type: dict[str, list[str]] = {}
    for a in parent_state.list_artifacts():
        # 坏记录不参与自动选料（同 _resolve_forward_artifacts）。list_artifacts
        # 对缺字段的记录会标 malformed 并把 type 填成 "(unknown)"，如实标注而不
        # 是丢掉 —— 这里据此过滤，不再自己 read_artifact 复核一遍。
        if a.get("malformed"):
            continue
        rec_type = a.get("type")
        if not rec_type or rec_type == "(unknown)":
            continue
        by_type.setdefault(rec_type, []).append(a["id"])

    for req_type in required_types:
        # list_artifacts 是升序，末位最新 → 倒过来就是"最新优先"
        candidates = list(reversed(by_type.get(req_type, [])))
        if not candidates:
            missing_types.append(req_type)
            continue
        # 选最新的
        auto_ids.append(candidates[0])
        if len(candidates) > 1:
            # 记录候选，让 LLM 可以在错误时显式选别的
            ambiguous[req_type] = candidates

    return auto_ids, ambiguous, missing_types


def _canonical_input_selection(
    parent_state: State,
    node_type: str,
    required_types: list[str],
) -> tuple[list[str], dict | None]:
    """调用方没指名时，本轮的输入集合到底是哪几份（issue #522）。

    返回 `(selected_ids, refusal)` —— refusal 非 None 时调用方必须原样返回它。

    v2 下"子节点直读共享 worktree"解决了**取料**，却把**选料**整个留空：调用方
    不传 `forward_artifact_ids` 时框架一句话都不说，子节点面对同类型多份历史产物
    只能猜。2026-08-19 实测：hypothesis / experiment 各跑了多轮，writing 拿到多份
    上游材料、无法确定哪一组属于同一条研究链路，连出两份"材料不足报告"。

    框架能机械回答的只有一句：**这个类型上有几个候选**。

      恰好一个 → 替调用方选定并送达（本来就没有第二种可能）
      多个     → 歧义 fail-loud，把候选摆给**调用方** —— 它知道研究链路，也只有
                 它改得动 `forward_artifact_ids`。把这句话说给子节点听是 #395-3
                 的教训：experiment 收到"请在 node_inputs 里指名"却改不了自己的
                 node_inputs，于是原样重派、一模一样地再失败。
      零个     → 不拦。"材料还没有"是合法局面，节点该如实产降级产物。

    **不猜哪一份是权威版本**：同类型的多份来自多轮上游工作，谁取代谁是研究链路上
    的事实，框架手里没有这个事实（账本只管同一身份的版本，不管跨身份的
    取代）。宁可把选择权交回去，也不按"最新那个"替它决定 —— 那正是 E2E v22
    把真 experiment_log 挤掉的形状。
    """
    if not required_types:
        return [], None
    auto_ids, ambiguous, _missing = _auto_resolve_required_inputs(
        parent_state, required_types)
    if not ambiguous:
        return auto_ids, None
    parent_state.append_transcript(
        "run_node_blocked_input_version_conflict",
        child_node_type=node_type,
        candidates=ambiguous,
    )
    lines = [
        f"⛔ 无法确定 {node_type} 本轮该用哪一份上游产物 —— "
        f"同一类型有多个候选，而你没有指名。",
        "候选如下（同一类型的多份 = 多轮上游工作留下的不同研究链路，"
        "框架无法机械判断哪一条是本轮的权威版本）：",
    ]
    for artifact_type, ids in sorted(ambiguous.items()):
        lines.append(f"  {artifact_type}:")
        lines += [f"    - {i}" for i in ids[:10]]
    example = ", ".join(repr(ids[0]) for ids in list(ambiguous.values())[:3])
    lines += [
        "重发这次调用，用 forward_artifact_ids 指名本轮的输入集合，例如：",
        f"  run_node(node_type={node_type!r}, forward_artifact_ids=[{example}])",
        "要一次带上多份（例如多组实验结果都要进论文）就把它们都列进去 —— "
        "显式传了框架就完全按你给的来，不再自动补。",
    ]
    return [], {
        "status": "error",
        "error": "\n".join(lines),
        "version_conflict": ambiguous,
        "blocker": {"kind": "input_version_conflict", "node_type": node_type},
    }


# ── 后台子节点（2026-07-09，R1 异步基座 v1）─────────────────────────────────
# 科研任务的时间常数（小时~天）与对话（秒）本质不匹配：同步 run_node 会把对话
# 冻结几十分钟。background=true 让 child 在进程内后台跑（同 dreaming 的模式），
# 对话立即返回；完成/暂停/失败事件走两条路：
#   1. _CHILD_EVENT_SINK —— chat.py 注册的打印回调（用户即时看到）
#   2. parent hook_state['injected_messages'] —— 下一轮注入（模型也看到，
#      保持"用户可见 = 模型可见"不变量）
# 限制（诚实声明）：进程内后台 ≠ daemon —— 终端关掉 child 就死（崩溃遗留由
# chat.py 启动扫描兜底）。真正跨进程 detach 是后续 RFC 的事。

_CHILD_EVENT_SINK = None
_BACKGROUND_TASKS: set = set()
# 后台 child pause 后的续跑上下文：child_run_id → 完成 bookkeeping 所需信息
_BG_PAUSED_CONTINUATIONS: dict[str, dict] = {}


def set_child_event_sink(fn) -> None:
    """chat.py 注册 fn(event: dict)；event.kind ∈ completed/paused/failed。"""
    global _CHILD_EVENT_SINK
    _CHILD_EVENT_SINK = fn


def _notify_child_event(event: dict) -> None:
    if _CHILD_EVENT_SINK is None:
        return
    try:
        _CHILD_EVENT_SINK(event)
    except Exception:
        pass


def _inject_to_parent(parent_state: State, content: str, source: str) -> None:
    parent_state.hook_state.setdefault("injected_messages", []).append(
        {
            "content": content,
            "source": source,
        }
    )


# ── v3.7 派发时重复失败检测（与 chat.py 的轮间熔断互补）──────────────────────
# 阈值：警告后仍可再试一次，第 4 次直接拒绝派发。env 可调。
# ⚠️ 这个集合只回答"该不该**机械重派**"（瞬态故障，重跑本身就是有效扰动）。
# "该不该**计入卡死统计**"是另一个问题，真相源在
# run_history.EXTERNAL_FAILURE_CATEGORIES（含框架门禁 —— 确定性失败，
# 不计入统计但也**绝不能**机械重派，原样重跑必然再撞同一道门）。
#: 成因在 provider / 基础设施侧、且值得**原地机械重试**的失败类别。
#: 与 `EXTERNAL_FAILURE_CATEGORIES`（"算不算节点的账"）是两个问题：门禁拒绝
#: 属于后者但不属于这里（原样重跑必然再撞）。
_INFRA_FAILURE_CATEGORIES = frozenset({
    "provider_tool_call_protocol_error",
    "provider_unavailable",        # 断流 / 超时打穿重试预算（#480）
})
_INFRA_RETRY_MAX = int(os.getenv("HARNESS_INFRA_RETRY_MAX", "2") or 2)

_REPEAT_DISPATCH_WARN = int(os.getenv("HARNESS_REPEAT_DISPATCH_WARN", "3") or 3)
_REPEAT_DISPATCH_BLOCK = int(os.getenv("HARNESS_REPEAT_DISPATCH_BLOCK", "4") or 4)


def _child_produced_artifacts(summary: dict, imported: list[dict]) -> list[dict]:
    """子 run **自己产出**的 artifact —— v1/v2 单一口径。

    这个事实有三个消费方（flow entry 的 artifact_ids、reviewer 有没有交
    critique、交付判定），此前各读各的：都读裸 `imported`。而 v2 下 imported
    恒为空（交付方式是写自己的 Git 目录，不再往父 state 搬副本），于是：
      - flow entry 的 artifact_ids 永远是空的
      - reviewer 明明产出了 review_critique，账本却记 failed_awaiting_human，
        再按"账本优先于传参"把调用方传来的**真实存在**的 critique id 推翻
        （E2E v11 实测：hypothesis→reviewer→curator 三步全绿，卡在这里出不去）

    v1 优先用 imported（父 state 视角的 id，语义一字不变），v2 回落到子 run
    自己的 summary.artifacts。同一个事实，一处推导。
    """
    if imported:
        return [a for a in imported if isinstance(a, dict)]
    return [a for a in (summary.get("artifacts") or []) if isinstance(a, dict)]


def _repeated_failure_for(state: State, node_type: str, *, scan_limit: int = 40) -> dict | None:
    """本节点最近的连续失败段。口径全在 core.run_history —— 别在这里再造一套。

    - 别的 producing 节点成功过 → 断链（上游状况已变，旧失败不该锁着本节点）
    - 外因失败（协议抽风 / 框架门禁）不计入卡死统计 —— 判据在
      run_history.consecutive_failures 内部（externally_caused），不靠这里传参
    """
    if node_type.startswith("_"):
        return None  # 系统节点不由本机制管
    runs = run_history.load_runs(
        state.root.parent,
        project_id=state.project_id,
        exclude_run_id=state.run_id,
        limit=scan_limit,
    )
    return run_history.consecutive_failures(
        runs,
        node_type,
        break_on_other_producing_success=True,
    )


def _blocked_dispatch_refusal(
    state: State,
    node_type: str,
    node_inputs: dict | None,
) -> dict | None:
    """上一次这个节点如实报了阻塞，而局面一个字节都没变 —— 别再派一次。

    与上面的重复失败熔断是**两条互不覆盖的路径**：那条数的是失败，而
    "我完整地报告了我被卡住"在账本上是一次成功（`final_status="blocked"`
    但必需产出齐 → `reevaluated_success` 判它成了），熔断器第一眼就断链。
    判据与解除路径全在 core/dispatch_gate。
    """
    if node_type.startswith("_"):
        return None                      # 系统节点不由本机制管（同重复失败熔断）
    prior = _prior_runs_of(state, node_type, limit=5)
    if not prior:
        return None
    last = prior[0]
    blockers = list(last.blockers)
    if not blockers:
        return None
    from core import dispatch_gate

    decision = dispatch_gate.evaluate(
        blockers=blockers,
        prior_situation=last.blocked_situation,
        worktree=getattr(state, "project_worktree", None),
        node_type=node_type,
        node_inputs=node_inputs,
    )
    if decision is None:
        return None
    from core.upstream_routing import upstream_candidates

    cands = upstream_candidates(node_type)
    state.append_transcript(
        "run_node_blocked_situation_unchanged",
        node_type=node_type,
        prior_run_id=last.run_id,
        blocker_categories=sorted({str(b.get("category") or "other") for b in blockers}),
    )
    return {
        "status": "error",
        "error": dispatch_gate.render_refusal(decision, cands),
        "prior_run_id": last.run_id,
        "blockers": blockers[:5],
        "upstream_candidates": cands,
        # 机械门禁拒绝的统一契约（executor._is_gate_blocked_record 消费）
        "blocker": {"kind": "blocked_situation_unchanged", "node_type": node_type},
    }


def _prior_runs_of(state: State, node_type: str, *, limit: int = 60) -> list[run_history.RunRecord]:
    """本 project 内某 node_type 的历史 run，新→旧。口径见 core.run_history。"""
    return run_history.load_runs(
        state.root.parent,
        project_id=state.project_id,
        exclude_run_id=state.run_id,
        node_type=node_type,
        limit=limit,
    )


def _baseline_run(
    state: State,
    node_type: str,
    prior: list[run_history.RunRecord] | None = None,
) -> run_history.RunRecord | None:
    """修订基线用哪一次 —— note 和 read_own_prior_attempt **必须走同一个入口**。

    PR#198 只改了 note 的选择，工具默认还读最近那次，于是节点照着 note 里的 id
    去读，连报两次"这个 run 里没有该 artifact"。这就是为什么它现在只有一处实现。
    """
    if prior is None:
        prior = _prior_runs_of(state, node_type)
    try:
        from core.loader import load_harness as _lh

        required = _lh(node_type).required_output_artifact_types or []
    except Exception:
        required = []
    return run_history.best_attempt(prior, node_type, required)


def _revision_baseline_note(state: State, node_type: str) -> dict | None:
    """重新调起 node_type 时，给它一份"上一版在哪、上次挂在哪"的清单。

    只在**上一次是失败**时给：上一次成功的话本次多半是新一轮工作，不是修订。
    见文件末尾 _read_own_prior_attempt 的根因说明（E2E-3：改一个 bib 条目被做成
    50 轮 4.3M token 的完整重做）。
    """
    if node_type.startswith("_"):
        return None
    prior = _prior_runs_of(state, node_type)
    if not prior:
        return None
    if prior[0].is_completed:
        return None  # 上一次干净成功 → 这是新一轮工作，不是修订

    last = _baseline_run(state, node_type, prior)
    if last is None:
        return None
    arts = [a for a in last.artifacts if isinstance(a, dict) and a.get("id")]
    _is_latest = last.run_id == prior[0].run_id
    lines = [
        f"你在本项目里已经跑过 {len(prior)} 次。走得最远的一次是 run "
        f"`{last.run_id}`（status={last.status}"
        + ("" if _is_latest else "，**不是最近那次** —— 最近那次进展更少")
        + "）。**没有从零开始的必要** —— 它的产物还在：",
    ]
    lines += [f"  - `{a['id']}`（type={a.get('type')}）" for a in arts[:15]]
    lines.append("用 `read_own_prior_attempt(artifact_id=...)` 读回来，**在它基础上改**。")
    lines.append("⚠️ 从零重做是**默认的错误做法**：它会丢掉上一版已经做对的部分，往往越改越差。")
    # v3.7.2b：这个基线机制本身带一个科学性风险 —— 它在鼓励复用。对产出**测量
    # 数据**的节点（experiment / data），"别从零开始"很容易被读成"沿用上次的
    # 结果"。E2E-3 现场就选中了一个带 clean_results / experiment_log /
    # repro_bundle 的 completed run 当基线。所以护栏跟基线**同时**给，不能等
    # 下游 QC 去抓。
    lines.append(
        "⚠️ 基线只对**同一个任务的修订**有效。如果本次的研究问题 / 假设 / 条件"
        "与上一版不同，它就**不适用** —— 老老实实重新做。"
    )
    lines.append(
        "⚠️ 基线里的**测量结果与数据**（experiment_log / clean_results / "
        "repro_bundle 等）**不得**当成本次新产生的结果。确有理由复用，必须在"
        "产物里显式写明来源 run id 与复用理由 —— 沉默复用等于伪造。"
    )
    # `failed_checks`：消费方（revision_baseline_injected 事件）读这个键。QC 判定层
    # 已随 #627 退场，不再有"失败检查清单"这个概念 —— 诚实值是上一版的失败类目
    # （若失败）或空表。此前这个键**根本没产出**，而消费方 `_baseline["failed_checks"]`
    # 硬取 → 每次重新调起节点都 KeyError（E2E v29 实测：writing 拿到 freeze 工具后
    # 一去重调就崩在这，manuscript 冻不了）。构造方漏一个键、消费方硬取一个键，
    # 契约两头对不上 —— 补齐它，消费侧也改 .get 兜底。
    failed = [
        x for x in (getattr(last, "failure_category", None),
                    getattr(last, "failure_subcategory", None)) if x
    ]
    return {
        "note": "\n".join(lines),
        "run_id": last.run_id,
        "n_artifacts": len(arts),
        "failed_checks": failed,
    }


def _is_retryable_infra_failure(summary: dict) -> bool:
    """后端协议抽风、且这一轮**什么都没干成** → 值得原地重试。

    E2E-3 实测：postprocess 起来后第 1 turn 就收到 7 个 completion token 的空
    回复（finish_reason=stop、content=""、tool_calls=[]），框架正确判成
    `provider_tool_call_protocol_error/dsml_leak`，但这个"基础设施打嗝"被当成
    节点失败原样交回 orchestrator，orchestrator 据此判 blocked 停掉了整个自主
    run。基础设施故障应由框架自己吞掉重试，不该消耗 orchestrator 的决策预算，
    更不该终止 run。

    只在**零产出**时重试：已经产出了 artifact 的 run 重跑一遍既浪费又可能覆盖
    已有成果。

    "零产出"的判据是 `produced_artifact_types`（executor 用完成度门禁那一份
    `produced_types` 算的，已排除 ghost 与转发件），**不是** tool_call_count。
    issue #253（jicq E2E 实测）：literature 跑了 8 轮检索、一个 artifact 都没
    落地就撞上 provider 空停 —— 检索是只读的，重跑什么都毁不掉，但旧判据
    `tool_call_count == 0 and turns <= 2` 把它挡在门外，于是这次基础设施打嗝被
    原样当成节点失败交回 orchestrator。工具调用次数只是"干没干活"的代理量，
    真正该问的是"有没有留下值得保住的东西"。

    老 summary（或测试里手搓的 dict）没有这个字段 → 退回旧代理量，保守不放宽。
    """
    if summary.get("status") == "completed":
        return False
    if summary.get("failure_category") not in _INFRA_FAILURE_CATEGORIES:
        return False
    produced = summary.get("produced_artifact_types")
    if produced is None:
        return int(summary.get("tool_call_count") or 0) == 0 and int(summary.get("turns") or 0) <= 2
    return not produced


def _review_target(node_type: str, artifact_ids: list[str]) -> str:
    """派审对象 = 节点声明的交付物，不是产物列表第一项。

    2026-09-17 实测（yuankk 的 astra-simv2）：writing 先 stage 图再存稿，产物列表
    第一项是 writing_asset_receipt，终稿两次 reviewer 审的都是那张收据，决策包据此
    推荐 PROCEED。交付物类型由 harness 的 required_output_artifact_types 声明；
    按声明顺序在本轮产物里找第一份匹配的（id 形如 <type>__<name>）；一份都没有
    才退回列表第一项，并且这种退回本身就是一条事实（见 transcript）。
    """
    try:
        from core.loader import load_harness
        wanted = list(load_harness(node_type).required_output_artifact_types or [])
    except Exception:  # noqa: BLE001
        wanted = []
    for t in wanted:
        for a in artifact_ids:
            if str(a).startswith(f"{t}__"):
                return str(a)
    return str(artifact_ids[0])


async def _execute_with_infra_retry(state: State, node_type: str, exec_kwargs: dict) -> dict:
    """execute_node + 基础设施故障机械重试（见 _is_retryable_infra_failure）。

    委派登记（note_delegated_workspace）在工具入口做过：它现在只给事后
    见证降噪（子节点写自己目录不该被报成父的越界写），没有执法动作。
    """
    from core.executor import execute_node

    return await _execute_with_infra_retry_inner(state, node_type, exec_kwargs, execute_node)


async def _execute_one(state: State, node_type: str, exec_kwargs: dict,
                       execute_node, attempt: int) -> dict:
    """跑一次子节点。provider 断流会**抛异常**而不是返回 summary —— 接住它。

    ## 为什么这里要接（issue #480）

    LLM provider 的 ReadError / RemoteProtocolError / ReadTimeout 打穿重试预算
    之后，`execute_node` 写完 summary.json 就 re-raise。异常沿着工具调用往上冒，
    于是这一层的机械重试**根本没机会看到它**：`_is_retryable_infra_failure` 读的
    是 summary 的 `failure_category`，而这条路上压根没有 summary 可读。

    实测后果（jicq 流体力学多轮 E2E）：literature / hypothesis 在没产出任何
    产物的情况下被一次接口抖动打死，orchestrator 只看到一段 traceback，
    继续 continuous 空转等待。

    executor 已经把这次失败写成 `failure_category=provider_unavailable` 的
    summary 落盘了 —— 这里把它读回来，异常路径于是和返回路径合流，走同一套
    有限重试。读不回来就退回一份最小的等价 summary，绝不把异常吞掉。
    """
    import time as _time

    from core.llm import describe_provider_error, is_transient_provider_error

    started = _time.time()
    try:
        return await execute_node(**exec_kwargs)
    except Exception as exc:                        # noqa: BLE001
        if not is_transient_provider_error(exc) or attempt >= _INFRA_RETRY_MAX:
            raise                                   # 非 provider 故障 / 重试用尽：行为不变
        state.append_transcript(
            "run_node_provider_unavailable",
            node_type=node_type,
            attempt=attempt,
            error=describe_provider_error(exc),
        )
        summary = _crashed_child_summary(state, node_type, started)
        if summary is not None:
            return summary
        # 读不回来（summary 也没写成）→ 最小等价件。**不写 produced_artifact_types**：
        # 缺这个字段时 `_is_retryable_infra_failure` 退回旧代理量，宁可保守也
        # 不要凭空断言"这一轮什么都没产出"。
        return {
            "status": "error",
            "failure_category": "provider_unavailable",
            "failure_subcategory": type(exc).__name__,
            "provider_error": describe_provider_error(exc),
            "tool_call_count": 0,
            "turns": 0,
        }


def _crashed_child_summary(state: State, node_type: str, started: float) -> dict | None:
    """刚才那个被 provider 打死的子 run 自己写下的 summary。

    executor 在 re-raise **之前**已经把 summary.json 落盘了（含
    `produced_artifact_types` / `failure_category`）。读回它，重试判定就用得上
    真实产出，而不是凭空假设"什么都没产出"——已经产出过东西的 run 重跑一遍
    既浪费又可能覆盖成果。
    """
    from core import run_history

    try:
        runs = run_history.load_runs(
            state.root.parent, project_id=state.project_id,
            node_type=node_type, limit=5)
    except Exception:                               # noqa: BLE001
        return None
    for record in runs:                             # load_runs 已按新鲜度排序
        if float(getattr(record, "finished_at", 0) or 0) + 1 < started:
            continue
        raw = dict(getattr(record, "raw", None) or {})
        if raw.get("status") == "error":
            return raw
    return None


async def _execute_with_infra_retry_inner(
    state: State, node_type: str, exec_kwargs: dict, execute_node
) -> dict:
    summary = await _execute_one(state, node_type, exec_kwargs, execute_node, 0)
    for attempt in range(1, _INFRA_RETRY_MAX + 1):
        if not _is_retryable_infra_failure(summary):
            break
        state.append_transcript(
            "run_node_infra_retry",
            node_type=node_type,
            attempt=attempt,
            failed_run_id=summary.get("run_id"),
            failure_subcategory=summary.get("failure_subcategory"),
        )
        await asyncio.sleep(min(2**attempt, 8))
        summary = await _execute_one(state, node_type, exec_kwargs, execute_node, attempt)
    if summary.get("failure_category") == "provider_unavailable":
        # 归因句由框架定死，不留给模型发挥。2026-08-20 实测：结果里明明带着
        # failure_category=provider_unavailable，调度器仍向用户转述成"被框架
        # 错误打断"——模型服务的锅扣在了框架头上。机械事实要以不可误读的
        # 形态送到调用方嘴边（结果只给类别码 = 逼模型自己造句）。
        summary.setdefault(
            "failure_human",
            "上游模型服务不可用（{sub}）——供应商侧故障，不是研究框架或"
            "平台错误；已产出的工作保留在盘上。".format(
                sub=summary.get("failure_subcategory") or "连接中断"),
        )
    return summary


#: 证据生产节点 → 它擅长兑现的闭合条目类型。查的是**模态**，不是节点名的字面。
#
# ⚠️ `derivation` 刻意**不在这张表里**，这不是漏了。推导两类条目都兑现得了：
# 解析解给出一个精确数值（数值条），完整演绎链兑现"给出证明"（陈述条）。
# 填任何一个值都会让另一类的派发凭空多一道要理由的闸 —— 那是闸自己制造的
# 摩擦。`expected is None` 直接放行，正是这里要的行为。
# （模型能判断的事，别硬编码成阈值。）
_MODALITY_BY_NODE = {
    "experiment": "numeric",       # 干预式：让世界产生新数据 → 数值条
    "observation": "statement",    # 检视式：系统性取证与论证 → 陈述条
}


def _modality_deviation(state, node_type: str, node_inputs: dict) -> dict | None:
    """派发的证据模态与还欠的闭合条目类型对不上 —— 摆出事实，不拒绝。

    返回 None = 范畴对得上（或无账可依）；返回 dict = 偏离的机械事实，调用方把它
    连同 `modality_rationale`（没写就是 ``"not_declared"``）写进永久记录后照派。

    判决拆除（run_node:1217 降格）：这里曾经 return error 要求调用方带着理由重发
    一次 —— 「理由」是申报，不是准入；申报缺席本身就是可持久化的事实，往返一次
    只多烧一轮 token。模态匹配是判断，判断归模型；框架只负责把机械事实摆出来、
    并让这次偏离**被记下来**（进审计留痕、进 referee 终审）。
    """
    expected = _MODALITY_BY_NODE.get(node_type)
    if expected is None:
        return None

    try:
        from core.research_situation import compute_situation

        closure = compute_situation(state).closure
    except Exception:
        return None      # 算不出局面就没有事实可记：这是提醒，不是安全边界
    if closure is None or closure.open_total == 0:
        return None

    open_numeric, open_statement = closure.open_numeric, closure.open_statement
    if expected == "numeric" and open_numeric == 0:
        actual, suggest = "陈述条", "observation"
    elif expected == "statement" and open_statement == 0:
        actual, suggest = "数值条", "experiment"
    else:
        return None

    rationale = str(node_inputs.get("modality_rationale") or "").strip() or "not_declared"
    return {
        "node_type": node_type,
        "expected_modality": expected,
        "open_items_are": actual,
        "closure_open": {"numeric": open_numeric, "statement": open_statement},
        "suggested_node": suggest,
        "modality_rationale": rationale,
        "note": (
            f"本项目还欠的 {closure.open_total} 条闭合条目全是{actual}，而 `{node_type}` "
            f"兑现的是另一类；{actual}靠 `{suggest}` 兑现更直接。"
            + ("调用方没有说明这一趟为什么是它（modality_rationale 缺席）。"
               if rationale == "not_declared" else "")
        ),
    }


#: 会消费 data 交付物的节点。查的是"这一趟要不要吃上游数据包"，不是节点名字面。
_DATASET_CONSUMERS = frozenset({"experiment"})


def _data_stage_debt(state, node_type: str, node_inputs: dict) -> dict | None:
    """data 阶段已经在场却没交出 dataset，起 experiment 就是它要自己造输入（#283 / #412）。

    返回 None = 没有这笔债；返回 dict = 债的机械事实 + 调用方给的
    `dataset_waiver_reason`（没写就是 ``"not_declared"``）。调用方把它写进永久记录
    后照派 —— 判决拆除（run_node:1312 降格）：这里曾经 return error 要求带理由重发，
    与 `_modality_deviation` 同形，同一条理由降格。

    ## 现场

    #283：用户明确说"先整理可复现数据包、不要提前计算结论"，orchestrator 仍直接
    起了 experiment —— 而 `nodes/experiment/harness.yaml` 的机械必需输入只有
    `pre_registration`，框架没有任何东西表达"本项目的 data 阶段还欠着"，于是
    只要预注册在，整条 data 审查链都能被跳过。

    #412：data 连挂三次没发布 dataset，experiment 接手自己写了 generate_dataset.py
    并把密度从冻结预注册的 0.5 漂成 0.7，LAMMPS 真跑通了 —— 于是一份**不是 data
    交付物、且违反预注册**的输入，产出了看起来合格的结果。

    ## 判据只用机械事实

      本项目有过 data 的 run（这一阶段确实在场）
      且盘上一份 `dataset` 都没有（它确实没交出来）

    "用户是不是要求了先整理数据"是自然语言，框架不猜；"data 跑过没有 / 交没交货"
    是磁盘上的事实，框架只问这个。该不该这么干是判断、判断归模型 —— 框架负责的是
    **这次偏离必须被记下来**，而不是像从前那样连发生过都没人知道。
    """
    if node_type not in _DATASET_CONSUMERS:
        return None
    try:
        if any(a.get("type") == "dataset" for a in state.list_artifacts("dataset")):
            return None              # data 已经交货
        data_runs = run_history.load_runs(
            state.root.parent, project_id=state.project_id,
            node_type="data", limit=20, include_in_flight=True)
    except Exception:                # noqa: BLE001
        return None                  # 算不出局面就没有事实可记：这是提醒，不是安全边界
    if not data_runs:
        return None                  # data 阶段根本不在场 —— 不替调度器规划阶段
    last = data_runs[0]
    blockers = [b for b in getattr(last, "blockers", ()) if isinstance(b, dict)]
    waiver = str(node_inputs.get("dataset_waiver_reason") or "").strip() or "not_declared"
    return {
        "kind": "data_stage_debt",
        "node_type": node_type,
        "data_runs": len(data_runs),
        "last_data_run_id": last.run_id,
        "last_data_blockers": [
            {"category": b.get("category") or "other",
             "summary": str(b.get("summary") or "")[:300]}
            for b in blockers[:3]
        ],
        "dataset_waiver_reason": waiver,
        "note": (
            f"本项目的 data 阶段已经在场（跑过 {len(data_runs)} 次，最近一次 "
            f"`{last.run_id}`），但盘上一份 dataset 都没有 —— 此时起 {node_type} "
            "意味着它要自己造输入；自产的输入必须用冻结预注册里的科学参数。"
            + ("调用方没有说明这一趟为什么不需要 data 的交付物"
               "（dataset_waiver_reason 缺席）。" if waiver == "not_declared" else "")
        ),
    }


async def _run_node_tool(
    state: State,
    node_type: str,
    node_inputs: dict | None = None,
    forward_artifact_ids: list[str] | None = None,
    background: bool = False,
    task_id: str | None = None,
    user_note: str | None = None,
    resume_run_id: str | None = None,
    deliverable: dict | None = None,
    planned_stop_authorized: bool = False,
    planned_stop_note: str = "",
    task_instance_uuid: str | None = None,
    task_contract_digest: str | None = None,
    **_: Any,
) -> dict:
    """工具实现。延迟 import 避免循环依赖。"""
    from core.llm import LLMClient
    from core.loader import load_harness
    from core.project_workspace import note_delegated_workspace

    # ── 派发前必须先跟用户说一句（2026-08-17）──────────────────────────────
    #
    # 实测一个真会话：跑完 hypothesis→reviewer→curator→dreaming→writing，
    # **assistant 消息 0 条**，用户面前只有 51 条子节点内部独白 + 190 张工具卡。
    # 调度器自己的 8 句话混在里面，跟节点独白同样式、同字号。用户原话：
    # 「连他妈一开始都不说话了？起码得让用户了解现在在干啥吧」。
    #
    # 回复契约（chat.py:1818）管的是**一轮终态**必须有干净文本。而这一轮跨了
    # 五个节点、一个多小时还没结束 —— 契约一次都没触发。粒度错了：用户需要的
    # 是**每个决策点**说一句，不是等一轮跑完。
    #
    # 为什么不写进 prompt：prompt 里的「必须」不是机制（hypothesis 那次写了
    # 三遍「结束前必须校验」，agent 56 次工具调用一次没调）。所以钉在 schema
    # 上（required + minLength:1）—— 派发口核一次、报错直接说清要写什么；
    # 工具体内不再手写第二份（判决拆除刀 1：契约声明一次、核一次）。
    _note = (user_note or "").strip()
    # 宣告**不在这里**发 —— 见下方真正派发点前的那处。

    # ── 派发前范畴检查（2026-08-18）──────────────────────────────────────
    #
    # 现场：一份 S0-S17 全是文献裁决的 research_plan 被派给 experiment，因为当时
    # 账本只认 experiment 的产物。整趟 1353 万 tokens，63% 烧在让文化史课题去
    # 满足计算实验的契约上。
    #
    # 判据是机械的：**还没兑现的闭合条目全是陈述条** = 这批账靠系统性取证与论证
    # 兑现，不是靠让世界产生新数据。反过来也一样（全是数值条却派 observation）。
    #
    # 但结论不归框架下 —— 陈述条也可能确实需要跑个模拟才能兑现。所以这里不拦，
    # 只**要一句理由**：形状照抄上面的 user_note（"钉在 required 上，缺了调不动，
    # 报错说清要写什么"）。出口永远可用（写一句话就过），因此拦不出死锁 ——
    # #151 的教训：过严的 guard 会自己造出第二个死锁。
    # 判决拆除（run_node:1217 / 1312 降格）：这两道曾是「带理由重发」的往返闸。
    # 现在派发照跑，偏离与理由（没写就是 not_declared）如实进永久记录 ——
    # transcript 事件 `modality_deviation` / `stage_debt`，并随本次返回值带回。
    _dispatch_deviations: list[dict] = []
    _deviation = _modality_deviation(state, node_type, node_inputs or {})
    if _deviation:
        state.append_transcript("modality_deviation", **_deviation)
        _dispatch_deviations.append({"kind": "modality_deviation", **_deviation})

    # 同一形状的第二笔账：data 阶段欠着货就起 experiment = 它要自己造输入。
    # 只用磁盘事实（data 跑过没有 / dataset 在不在），不猜用户的自然语言意图。
    _data_debt = _data_stage_debt(state, node_type, node_inputs or {})
    if _data_debt:
        state.append_transcript("stage_debt", **_data_debt)
        _dispatch_deviations.append(_data_debt)

    # 委派登记放在**工具入口**：run_node 有多条派发路径（同步 / 后台 / 重试
    # 包装），登记埋在其中任何一条深处都会漏 —— 实测埋在 _execute_with_infra_retry
    # 里，真实路径没走到，守卫照旧把子节点写自己目录的产出回滚。
    # 这里是所有路径的必经点，且早于任何子节点写入。
    note_delegated_workspace(state, node_type)

    authorized_flow_entry: dict | None = None

    # ── v3.7：重复失败拦截必须发生在**派发这一刻** ────────────────────────────
    # E2E-3 实测：orchestrator 在**同一个 turn 内**连续起了 4 个 writing（每次
    # 失败后立刻再起一个），而 continuous 的重复失败熔断只在**两轮之间**评估 ——
    # 检测器算出 count=4 完全正确，那段代码却从没被执行到。真正的决策点是
    # "我正要再起一次那个一直失败的节点"，即此处。
    # 这道熔断**不是**盲目的重试计数器 —— 它数的是"同一个失败信号连续出现了
    # 几次"，别的 producing 节点成功就断链，基础设施抽风不计入。也就是说它问的
    # 正是"有没有东西变过"。
    #
    # 曾经因为"v2 会把结构化 blocker 交回调度器、固定次数熔断判断不了环境是否
    # 改变"而把 v2 整个排除在外 —— 但判据本来就是按信号算的，理由不成立，
    # 结果是 v2 下**完全没有熔断**。P5 实测：postprocess 连续 6 次栽在同两个
    # QC 上，orchestrator 一路重派，3M token 全烧在同一个死循环里。
    # 这和 reviewer 门当初"排除 v2"是同一个错误：不该关门，该修判据。
    _repeat = _repeated_failure_for(state, node_type)
    if _repeat:
        _n = _repeat["count"]
        _sig = ", ".join(_repeat["signals"][:3])
        if _n >= _REPEAT_DISPATCH_BLOCK:
            from core.upstream_routing import upstream_candidates

            _cands = upstream_candidates(node_type)
            state.append_transcript(
                "run_node_blocked_repeated_failure",
                node_type=node_type,
                count=_n,
                signals=_repeat["signals"],
            )
            return {
                "status": "error",
                "error": (
                    f"⛔ 拒绝再次启动 {node_type}：它已连续 {_n} 次失败在 `{_sig}` 上。"
                    f"同一节点反复挂同一处 = **根因在上游产物**，再跑一次仍是同样"
                    f"结果（实测有过连跑 5 次 × 50 轮全部失败）。\n"
                    f"必须改做三件事之一：\n"
                    f"  (a) 退回上游补齐 —— 候选：{_cands or '（无上游）'}；"
                    f"用 run_node 启动它，并把『缺什么 / 达到什么标准算补齐』写进 "
                    f"node_inputs；\n"
                    f"  (b) 让 {node_type} 产**降级产物**（pilot / gap report），"
                    f"在 node_inputs 里显式要求，并如实标注缺口；\n"
                    f"  (c) 确属 human-only 卡点 → 输出 CONTINUOUS_STATUS: blocked；\n"
                    f"  (d) 若失败签名指向**框架/工具缺陷**（参数被拒、契约不匹配、"
                    f"工具内部报错），不要换个姿势继续绕 —— 用 request_human_input "
                    f"呈一个**诊断型**问题：附上失败签名与 failed_check_reasons 里的"
                    f"因果线索，让用户裁决『这是框架 bug / 任务与节点不匹配 / 换法再试』。"
                    f"同一签名反复失败时，绕过比停下更贵（实测一晚烧 3M token）。"
                ),
                "repeated_failure": _repeat,
                "upstream_candidates": _cands,
                # 机械门禁拒绝的统一契约（executor._is_gate_blocked_record 消费）
                "blocker": {"kind": "repeated_failure_dispatch_block",
                            "node_type": node_type, "count": _n},
            }
        if _n >= _REPEAT_DISPATCH_WARN:
            state.append_transcript(
                "run_node_repeated_failure_warning",
                node_type=node_type,
                count=_n,
                signals=_repeat["signals"],
            )
            state.hook_state["_repeat_dispatch_warning"] = (
                f"⚠️ {node_type} 已连续 {_n} 次失败在 `{_sig}`。再起第 {_n + 1} 次"
                f"多半仍是同样结果 —— 优先考虑退回上游补齐，或改产降级产物。"
                f"连续 {_REPEAT_DISPATCH_BLOCK} 次将被拒绝派发。"
            )

    # 防御：部分 LLM 把 node_inputs 当 JSON 字符串传（应该是 dict）—— 自动解析
    if isinstance(node_inputs, str):
        try:
            import json as _json

            node_inputs = _json.loads(node_inputs)
        except (ValueError, TypeError):
            node_inputs = None

    # ── #524 / #395-9：报过阻塞、局面没变，就不该再派一次 ────────────────────
    # 上面那道重复失败熔断数的是**失败**。而 writing 判定"上游材料不足"时会如实
    # report_blocker、写一份材料不足报告 —— 必需产出齐、QC 全过，账本上那是一次
    # **成功**（`blocked` 被 reevaluated_success 判回成），熔断器见到 is_completed
    # 第一眼就断链，对这条路径完全不在场。2026-08-19 实测：同一份材料不足报告被
    # continuous 重跑三次，前两次逐字节同样的输入、同样的结论。
    # 放在 node_inputs 规范化**之后**：指纹要比的是"调用方这次的要求"，字符串
    # 形态和 dict 形态是同一个要求，不能因为传法不同就漏过。
    _blocked = _blocked_dispatch_refusal(state, node_type, node_inputs)
    if _blocked is not None:
        return _blocked

    # ── 父 harness 的 callable_nodes 白名单 ─────────────────────────────────
    parent_callable = getattr(state, "_parent_callable_nodes", None)
    if parent_callable is None:
        # state 上没有记 → 走 harness 上的（agent_loop 启动时由 caller 注入）
        parent_callable = state.hook_state.get("_callable_nodes") or []
    if not _is_allowed_callee(list(parent_callable), node_type):
        return {
            "status": "error",
            "error": (
                f"父节点 callable_nodes 不允许调起 {node_type!r}。"
                f"在父 harness yaml 里加 `callable_nodes: [{node_type}]` 或 `[*]` 来授权。"
            ),
            "parent_callable_nodes": list(parent_callable),
        }

    # 起 `_reviewer` 前的资格门（#143/#151/#155）已删除（2026-08-19，Move 1d 续）。
    #
    # 它查的是"这个 producer 有没有一条 review_state 合格的 flow entry"，四种
    # 不合格情况各给一段诊断。服务对象是**调度器手动调 reviewer** —— 而 reviewer
    # 现在由运行时在 `_run_post_producing_flow` 里派，参数直接从 flow entry 取，
    # 构造上不可能不合格。四选一诊断是写给"抄错了的模型"看的，没有模型在抄了。
    #
    # 重试授权没有被削弱：能不能重审仍由 `record_decision_answer` 的封顶逻辑管
    # （人工在决策包里选 RETRY REVIEWER 才把 review_state 置成 retry_authorized），
    # 而运行时只在 `review_state == "pending"` 时派首审。判据还在，只是不在派发口
    # 再查第二遍 —— 同一个判据查两处，正是它们会分叉的原因。

    # ── v0.5.1 硬卡：起新 producing 节点前必须先 present_decision_package ────
    # post_node_review_flow 之前只是 hook 软提醒；LLM 看到 reviewer revise
    # 反馈太详细会直接调 run_node(source_node) 重跑，**跳过 decision_package**
    # —— user/auto-approve 永远没机会说"PROCEED 收尾"，无限 revise 循环。
    # v0.5 dogfood v2 在 analysis 阶段实测复现：9 次 analysis run / 0 次 decision_package。
    #
    # 修法：起任何 producing 节点之前，pending_post_node_flow 里若有
    # decision_state="pending" 的 entry，必须先调 present_decision_package
    # 处理它（user / auto-approve 决定 PROCEED / REVISE / REDIRECT），entry
    # 才被清。然后才能起新 producing 节点。
    #
    # 例外（不卡）：起 _reviewer / _curator —— 它们正是用来把 pending entry
    # 推进的；起其它 producing 节点（包括"重跑 source_node"）才卡。
    # 授权动作的目标节点：记下 entry，供下面的空转熔断计数。
    #
    # 这里原来还有一道墙：flow 没闭合就拒绝起任何 producing 节点。**它已经没有
    # 可达的触发条件了**（2026-08-19，Move 1d 续）——
    #
    #   review / decision 待办  → 运行时在 run_node 内部走完，调度器无控制权
    #   人选了 REVISE/REDIRECT → 运行时在 pause_driver 里直接执行目标节点
    #   人选了 EDIT            → 那一级**保持 pause**（选项文案自己写着 pauses），
    #                            调度器全程不参与
    #
    # 三个窗口逐一关掉之后，"调度器手握控制权且有 flow 开着"这个状态不再存在，
    # 墙也就无处可撞。删它不是拆防 —— 防守对象没了。
    #
    # 保留的是**空转熔断**（下面 _MAX_ACTION_ATTEMPTS）：运行时自动执行之后，
    # 无限重跑反而更容易发生，那道闸比以前更需要。
    if node_owes_post_node_flow(node_type):
        for _e in state.hook_state.get("pending_post_node_flow") or []:
            if _unresolved_flow_reason(_e) and _authorized_action_target(_e) == node_type:
                authorized_flow_entry = _e
                break

    # ── issue #166 硬卡：redirect 踢皮球检测（A→B 且 B→A）────────────────
    # 根因：单边硬门禁 + 对面无对称义务 + 框架无 redirect 账本 = 无限对踢。
    # experiment 的 no_self_fabricated_preprocessing 硬性要求"前处理缺失必须
    # redirect_upstream: data"，而 data 若以"这是计算任务"退回，两边就来回弹，
    # 框架此前看不见这个事实（redirect 全靠 orchestrator LLM 记着执行），
    # 因此也永远不会仲裁 —— e2e 实测 POSCAR 生成任务卡死在这里。
    #
    # 正常流程里"上游产出后重跑下游"是 **重跑**、不是 redirect；所以账本里
    # 出现反向 redirect 边本身即异常：两个节点对同一份工作的归属没有共识。
    # 这不是机器能判的事（归属取决于科研意图），必须交人工仲裁 —— 框架的
    # 职责是**停下来把冲突摆到台面上**，而不是继续弹。
    if node_owes_post_node_flow(node_type):
        from .library.decision_package import (
            clear_redirect_pingpong,
            detect_redirect_pingpong,
        )

        _pp = detect_redirect_pingpong(state, node_type)
        if _pp:
            _a, _b = _pp["pair"]
            # 关键：**消费掉**这一对边再返回 —— 拦截的目的是把冲突摆到人面前
            # 一次，不是永久堵死。若不清账本，人工裁定后再起该节点会被同一条
            # 规则再拦一次 → 我自己造出第二个死锁（#151 的教训：过严的 guard
            # 制造无出口的 dead-end）。清掉后本次放行；两边若**再**对踢一轮，
            # 新边重新累积、再拦一次 —— 每轮往返最多浪费一次，且永远不静默。
            clear_redirect_pingpong(state, _pp["pair"])
            state.hook_state.setdefault("_redirect_pingpong_history", []).append(_pp)
            state.append_transcript(
                "redirect_pingpong_blocked",
                pair=_pp["pair"],
                blocked_node=node_type,
                n_edges=len(_pp["edges"]),
                n_escalations=len(state.hook_state["_redirect_pingpong_history"]),
            )
            return {
                "status": "error",
                "error": (
                    f"⛔ 检测到 {_a!r} 与 {_b!r} 之间的 redirect 踢皮球"
                    f"（双向 redirect 各至少一次，共 {len(_pp['edges'])} 条边）——"
                    f"框架不再继续弹，必须先人工仲裁工作归属。\n\n"
                    f"两个节点对同一份工作的归属没有共识，再起任何一方都只会"
                    f"再弹一次（而且每弹一轮都在烧 token）。\n\n"
                    f"具体下一步：调 request_human_input，把冲突原样摆给用户 ——\n"
                    f"  1. 争议的具体工作是什么（哪个产物 / 哪一步）\n"
                    f"  2. {_a} 的理由、{_b} 的理由（各自 redirect 时写的原因）\n"
                    f"  3. 请用户裁定：由哪个节点做，或是否需要改任务定义\n\n"
                    f"本次拦截已消费掉这一对账本：**用户裁定后直接起对应节点即可，"
                    f"不会被再次拦下**。但若两边再对踢一轮，会再拦一次。"
                ),
                "redirect_pingpong": _pp,
                "next_step": "request_human_input（工作归属仲裁）",
                "blocking_is_one_shot": True,
            }

    # ── 这里曾经有一道「起 writing 前必须先跑 curator dreaming」的硬门。删了。────
    #
    # 别加回来。2026-08-21 整晚实测：它把一个已经做完的研究钉死了 6.5 小时，
    # 论文一个字没写出来，烧掉全场约一半的 token（总 2.28 亿）。两层独立断裂，
    # 任意一层都足以死锁：
    #
    #   1. 门的注释承诺"curator dreaming 跑完 clear_pending() 重置，第二次
    #      writing 即放行，不死循环" —— **那句话没有代码支撑**。clear_pending()
    #      的生产调用点只有 chat.py 两处和平台一个 skip 端点；走 run_node 跑完
    #      curator 没有任何人清 pending。CLI 之外的每一个驱动方都中招。
    #   2. 就算清了，下一次 should_run_dreaming() 会落到 stale 兜底 ——
    #      last_dreaming_at() 锚 runs_parent()，而平台底座的 run 落在会话
    #      worktree 里，账本恒空 → 无条件重新标记 "never run dreaming"。
    #
    # 于是 curator 实跑 3 次全部 completed，writing 被拒 23 次，连 UI 上那个
    # "跳过 dreaming" 按钮都无效（它只删标志，下一次检查立刻重新标记）。
    # 出口数量：零。
    #
    # 但真正该记住的不是这两个 bug，是**这道门就不该存在**：
    #   · 代价极不对称 —— 拦错=交付归零；放过=KB 晚点整理（随时可补）。
    #   · 它站在"几小时工作"和"交付"之间，失败得最晚、最贵。
    #   · 同族的门都有出口（踢皮球一次性消费、熔断四条出路、synthesis 有
    #     override、blocked 门还配了 env 逃生阀），只有它零出口。
    #
    # 它当年治的病是真的（自主模式没人触发 dreaming、KB 只存不代谢），但那是
    # **调度问题**，不是准入问题：解法是让提醒准确并在节点间隙自然发生
    # （loop_hooks_builtin 的 dreaming_due_reminder），不是拿交付当人质。
    # 判决归判决，检测归检测 —— 与 PR#381 把 35 条 QC 降级成检测同一条教训。

    # ── 这里曾经还有一道「起 writing 前必须有 verdict=ready_to_write 的
    # project_synthesis」的 writing-gate（v3.2 机械门禁 + present_writing_gate_override
    # 授权环）。判决拆除·第三波删了（run_node:1690/1725 降格，writing_gate 模块退场）。
    #
    # 它是 fire_data 里 28 次开火的冠军、单 run 连撞 24 次的骚扰墙：「还没做综合
    # 评估」是**事实**不是**资格**——写进稿件局限节即可（writing 的 input_audit
    # 把 scientific_basis.project_synthesis 如实入账，缺席就是 None），referee 终审
    # 看得见；「verdict 仍是 iterate」同理。事前审批（S3）不是框架的活。
    # 30 行外原本就有它的降格形态（conservative_with_limitations 注入）——现在那
    # 就是默认形态，不再需要一个 override 工具来「授权」写一份如实披露局限的稿子。

    # ── 递归深度 ────────────────────────────────────────────────────────────
    # v1.4: owner_config 可以 per-node override（通过 state.hook_state）
    effective_max_depth = state.hook_state.get("_subagent_max_depth_override", MAX_DEPTH)
    new_depth = state.depth + 1
    if new_depth > effective_max_depth:
        return {
            "status": "error",
            "error": (
                f"递归深度超过上限 {effective_max_depth}（当前 depth={state.depth}）。"
                f"用 HARNESS_FRAMEWORK_MAX_SUBAGENT_DEPTH env 或 owner_config.yaml "
                f"subagent.max_depth 可改，但通常意味着 LLM 在打转 —— 先看父 "
                f"harness rules 是否够严。"
            ),
        }

    # ── harness 存在性预检 ────────────────────────────────────────────────────
    try:
        child_harness = load_harness(node_type)
    except FileNotFoundError as e:
        _hint = ""
        if node_type == "review":
            # v0.4 删了 'review' 节点：同一事实（harness 不存在）只在这一处答，
            # 顺手把别名指路带上，别让模型再猜一次。
            _hint = (
                "\nnode_type='review' 已废弃（v0.4 删）。Manuscript 审稿用 `_reviewer`：\n"
                "  run_node(node_type='_reviewer', node_inputs={'artifact_id': '<manuscript id>', "
                "'source_node_type': 'writing', 'producer_run_id': '<writing 子 run id>'})\n"
                "_reviewer 会自动按 source_node 加载 nodes/<source>/review_spec.md。"
            )
        return {"status": "error", "error": str(e) + _hint}

    if background and getattr(state, "project_worktree", None) is not None:
        return {
            "status": "error",
            "error": (
                "Project v2 的一个 Session worktree 只有一条 Git mutation lane；"
                "后台 child 会与父节点/Platform checkpoint 并发改同一工作树。"
                "请去掉 background 同步运行；真正需要并行研究时应创建独立 Session worktree。"
            ),
        }

    # 本 session 确实在做项目工作了 —— 维护类 hook（如 dreaming_due）据此判断
    # 该不该打扰用户；纯问答/闲聊不会走到这里。
    state.hook_state["_dispatched_any_child"] = True

    # ── 输入契约机械校验（v2.1）────────────────────────────────────────────
    # 实测事故（2026-08-07 UI）：orchestrator 直调 postprocess 服务时传了
    # `figure_spec`，而该节点所有 visual 工具都要 `visual_requests`。服务用
    # report_blocker 如实报了 missing_input，调用方没消费，原样重试 4 次后整条
    # run 被 cancel —— 烧掉 4 个 12 轮子 run 才发现只是参数名不对。
    #
    # 根因不是 orchestrator prompt 少写一句：`expected_inputs` 本来就声明在每个
    # harness 里、也被 loader 读进 NodeHarness，只是 run_node 从不把它告诉调用方
    # —— 调用方只能靠 prompt 背参数名（writing 的 harness 里硬写了 visual_requests，
    # 那是 owner 知识，换个调用方就没有）。契约是声明数据，派发处就该拿它把关。
    #
    # 位置：放在所有流程门（callable 白名单 / 审查门 / 递归深度）**之后** ——
    # "你不该起这个节点"必须盖过"你参数名不对"，否则报错会误导。
    # 判据保守：只有"一个声明键都没命中"才拦（几乎必然是契约不匹配）；命中任一
    # 声明键、额外再带自定义字段照常放行。
    _expected_inputs = dict(child_harness.expected_inputs or {})
    if node_inputs and _expected_inputs and not (set(node_inputs) & set(_expected_inputs)):
        return {
            "status": "error",
            "error": (
                f"node_inputs 的键与 {node_type!r} 声明的输入契约完全不匹配。"
                f"传入 {sorted(node_inputs)}，该节点声明的是 {sorted(_expected_inputs)}。"
                f"用声明的键重发 —— 不要重试同样的调用。"
            ),
            "expected_inputs": _expected_inputs,
            "received_keys": sorted(node_inputs),
        }

    # ── 服务节点不许异步（2026-08-04 e2e8 实测）────────────────────────────
    #
    # `post_run_flow: none` 声明的是"我是服务，我的返回值就是调用方要的东西"
    # （见 core/harness.py 的 is_service）。这种调用天生同步 —— 需求方拿不到
    # 货就没法往下做。
    #
    # 实测事故：writing 缺图，正确地派了 postprocess，但传了 background=true。
    # 框架于是回它"已在后台启动……**不要**轮询等待"。它照做了：写了个占位框
    # （placeholder.tex → compile_latex → 装进 figures/），编译，收工。半小时后
    # 真图画好了，稿子早已定稿 —— 交付出去的论文里是 "[Figure placeholder]"。
    #
    # 判决拆除（run_node:1839 降格）：这里曾机械拒绝服务节点 background。二审核出
    # 送达口是存在的（_report_background_done → _inject_to_parent），所以「拿不到货」
    # 不是物理事实，是预测判决。现在放行，但**账要记**：hook_state 里登记一笔
    # `pending_service_results`（服务结果未到不得定稿），结果到货即消
    # （见 _report_background_done）。派发返回值也把这句话带回给调用方。
    _service_pending = bool(background and child_harness.is_service)

    # ── v3.7.2：重新调起一个跑过的节点 → 机械附上"修订基线"清单 ────────────
    # 光有工具不够：节点得知道上一版存在、id 是什么、上次挂在哪。见文件末尾
    # _read_own_prior_attempt 的根因说明。
    _baseline = _revision_baseline_note(state, node_type)
    if _baseline:
        node_inputs = {**(node_inputs or {}), "修订基线（重新调起时必读）": _baseline["note"]}
        state.append_transcript(
            "revision_baseline_injected",
            node_type=node_type,
            prior_run_id=_baseline["run_id"],
            prior_failed_checks=_baseline.get("failed_checks", []),
            n_artifacts=_baseline["n_artifacts"],
        )

    # ── issue #254/#260：curator integration 的 artifact 必须真搬进去 ────────
    # 实测（jicq 两次独立复现）：orchestrator 起 curator 只在 node_inputs 里传了
    # `artifact_ids`（一串 id 字符串），**没有 forward 实体**。curator 于是只能
    # `read_external_artifact` 跨 run 读——读得到，但 `scan_artifact_disagreements`
    # 只扫本地 `state.list_artifacts()`，永远扫不到它们 → 我 #229 加的整合收据记
    # `artifacts_scanned=[]` → 机械 QC 必挂 → 整合门禁**谁都过不去**。
    #
    # 那是我造的门：把"必须被 scan 扫到"当作整合的机械证据时，没有验证 curator
    # 到底拿不拿得到本地 artifact（#151 同款错误形状）。
    #
    # forward 机制本来就有（`forward_artifact_ids` → executor 会 save_artifact
    # 进子 run，标 `_forwarded_input`，`list_artifacts()` 看得见）——缺的只是
    # 没接到这条路径上。这里机械补齐：**光靠 prompt 指引不够**，模型漏传一次
    # 就又是一轮白跑。caller 显式传了就尊重 caller，没传才自动补。
    if (
        node_type == "_curator"
        and not forward_artifact_ids
        and isinstance(node_inputs, dict)
        and node_inputs.get("mode") in (None, "integration", "mode_1")
    ):
        _target_ids = node_inputs.get("artifact_ids")
        if isinstance(_target_ids, str):
            _target_ids = [_target_ids]
        _target_ids = [str(x) for x in (_target_ids or []) if str(x).strip()]
        if _target_ids:
            forward_artifact_ids = _target_ids
            state.append_transcript(
                "curator_integration_autoforward",
                artifact_ids=_target_ids,
                note="caller 未传 forward_artifact_ids；框架按 node_inputs."
                "artifact_ids 自动 forward，否则 scan 扫不到本地 artifact "
                "（issue #254/#260）",
            )
        # producer_run_id 同样别指望模型传（实测 receipt 里是 null）——账本里
        # 有确定答案：pending_post_node_flow 里按目标 artifact 或 trigger_node
        # 反查产出它的那个 run。（legacy 的 pending_curator_integrations 镜像随
        # curator 退出 flow 一并删除。）
        if not node_inputs.get("producer_run_id"):
            _tgt = set(_target_ids)
            _trigger = node_inputs.get("trigger_node")
            _found = None
            if True:
                for _e in state.hook_state.get("pending_post_node_flow") or []:
                    _ids = {str(x) for x in (_e.get("artifact_ids") or [])}
                    _hit = (_tgt and _ids & _tgt) or (
                        _trigger and _e.get("producing_node") == _trigger
                    )
                    if _hit:
                        _found = _e.get("producing_run_id")
                        break
            if _found:
                node_inputs = {**node_inputs, "producer_run_id": _found}

    # ── 解析 forwarded artifacts ────────────────────────────────────────────
    # 显式 forward_artifact_ids（caller 给了）→ 完全按 caller 传的，不自动补
    # 缺省（caller 没传）→ 框架按 child.required_input_artifact_types 自动从父 state 选
    autoresolved_note: dict | None = None
    selected_input_ids: list[str] | None = None
    upstream_artifacts: list[dict] = []
    _bound = getattr(state, "project_worktree", None) is not None
    if forward_artifact_ids:
        # 显式选择：引用不成形只在这一处判（判决拆除：1994/2020 两份抄件已并入）。
        _ok, missing = _resolve_forward_artifacts(state, forward_artifact_ids)
        if missing:
            return {
                "status": "error",
                "error": _unusable_forward_message(missing),
            }
        selected_input_ids = [str(aid) for aid in forward_artifact_ids]
        if _bound:
            # Project 模式**不搬文件**（子节点直读共享 worktree），但 forward_artifact_ids
            # 是**选择**，必须送达子 run —— 此前这里整个忽略掉（还发
            # artifact_forward_ignored_project_v2 事件、工具描述让 caller "不用费心挑"），
            # 于是同类型多份可见时子节点没有任何机械通道知道用哪份，writing 只好自造
            # `upstream_artifact_inventory` 私有申报格式绕行，其宽容解析器静默丢弃
            # 写错形状的项 → 调度器整晚困在假 ambiguous 里（2026-08-18 实测）。
            # 修法：文件照旧不搬，选择经 executor 落 hook_state 机械送达。
            state.append_transcript(
                "artifact_selection_forwarded_project",
                child_node_type=node_type,
                artifact_ids=selected_input_ids,
            )
            autoresolved_note = {
                "project_workspace": True,
                "selected_input_ids": selected_input_ids,
                "note": (
                    "No files were forwarded. The child reads the shared Session "
                    "worktree directly; forward_artifact_ids was recorded as the "
                    "child's input selection."
                ),
            }
        else:
            # 未绑 Project 的 CLI / fixture run：真正搬运文件。
            upstream_artifacts = _ok
    elif _bound:
        # ── #522：没人选，就不能让子节点自己猜 ─────────────────────────
        # v2 下"子节点直读共享 worktree"解决了取料，却把**选料**整个留空：
        # 调用方不传 forward_artifact_ids 时，框架一句话都不说，子节点面对
        # 同类型多份历史产物只能猜。2026-08-19 实测：hypothesis / experiment
        # 各跑了多轮，writing 拿到多份上游材料、无法确定哪一组属于同一条
        # 研究链路，连出两份"材料不足报告"。
        #
        # 能机械回答的只有一句：**这个类型上有几个候选**。
        #   恰好一个 → 框架替调用方选定并送达（本来就没有第二种可能）
        #   多个     → 歧义 fail-loud，把候选摆给**调用方**（它知道链路），
        #              而不是把一句执行不了的话说给子节点听（#395-3 的教训：
        #              experiment 拿到"请在 node_inputs 里指名"却改不了自己
        #              的 node_inputs，于是原样重派、一模一样地再失败）。
        #   零个     → 不拦。"材料还没有"是合法局面，节点该如实产降级产物。
        #
        # 判决拆除：这里曾有一条 legacy（无 worktree）自动选料分支，对同一问题
        # 答的是「缺必需类型→拒绝派发」—— 同一问题两答案。生产入口（chat.py 与
        # 平台）都绑 worktree，那条分支只有 CLI 裸跑/fixture 走得到，整段删了；
        # 未绑 worktree 且没显式选择时，子节点不带任何上游文件（材料层归节点）。
        _auto_ids, _conflict = _canonical_input_selection(
            state, node_type,
            list(child_harness.required_input_artifact_types or []))
        if _conflict is not None:
            return _conflict
        if _auto_ids:
            selected_input_ids = list(_auto_ids)
            state.append_transcript(
                "artifact_selection_autoresolved_project",
                child_node_type=node_type,
                artifact_ids=selected_input_ids,
                note="每个必需输入类型都只有一个候选，框架代为选定并送达",
            )
        autoresolved_note = {
            "project_workspace": True,
            "selected_input_ids": selected_input_ids,
            "note": (
                "No files were forwarded. The child reads upstream node directories "
                "directly from the shared Session worktree."
                + ("" if not selected_input_ids else
                   " Each required input type had exactly one candidate; the "
                   "framework resolved the selection and delivered it.")
            ),
        }

    # ── 输出目录：父 state.root 的兄弟（output/<child_run>/）─────────────────
    base_dir = state.root.parent

    if authorized_flow_entry is not None:
        # 数的是**同一个失败信号连续出现了几次**，不是裸次数（一审附修正，
        # 判决拆除第三波落地）：上次失败原因变了 = 有东西变过 → 断链重数。
        # 「无记录（子 run 完成了但 flow 没闭合）」的空转正是这道闸要抓的，
        # 它的签名恒为空串，照旧累计。
        _last_failure = str(authorized_flow_entry.get("action_last_failure") or "")
        _streak = authorized_flow_entry.get("action_streak_signature")
        if _streak and _last_failure != _streak:
            state.append_transcript(
                "decision_action_streak_reset",
                producing_run_id=authorized_flow_entry.get("producing_run_id"),
                authorized_target_node=node_type,
                previous_signature=str(_streak)[:200],
                new_signature=_last_failure[:200],
                attempts_before_reset=int(
                    authorized_flow_entry.get("action_attempt_count") or 0),
            )
            authorized_flow_entry["action_attempt_count"] = 0
        authorized_flow_entry["action_streak_signature"] = _last_failure
        _attempt = int(authorized_flow_entry.get("action_attempt_count") or 0) + 1
        if _attempt > _MAX_ACTION_ATTEMPTS:
            return {
                "status": "error",
                "error": (
                    f"⛔ 同一个 post-producing flow（{authorized_flow_entry.get('producing_node')!r} "
                    f"run {authorized_flow_entry.get('producing_run_id')}）已经起过 "
                    f"{_attempt - 1} 次 {node_type!r} 且始终没能关闭，这是空转不是进展。\n\n"
                    "不要再起了。改为：present_decision_package(...) 重新裁决这一轮，"
                    "或 report_blocker 把这个闭合失败交给人。\n"
                    f"上次失败原因：{authorized_flow_entry.get('action_last_failure') or '（无记录 —— 子 run 完成了但 flow 没闭合）'}"
                ),
                "flow_entry": {
                    "producing_node": authorized_flow_entry.get("producing_node"),
                    "producing_run_id": authorized_flow_entry.get("producing_run_id"),
                    "decision_state": authorized_flow_entry.get("decision_state"),
                    "action_target_node": _in_progress_target(authorized_flow_entry),
                    "action_attempt_count": _attempt - 1,
                },
            }
        # 权威字段必须在**起之前**落盘：闭合、失败重置、重启恢复三处都读它。
        # 少写这一笔，顺延来的 entry 就永远匹配不上 —— 那正是本次修复的病根。
        authorized_flow_entry["action_prior_state"] = (
            authorized_flow_entry.get("decision_state") or "action_authorized"
        )
        authorized_flow_entry[_ACTION_TARGET_KEY] = node_type
        authorized_flow_entry["decision_state"] = "action_in_progress"
        authorized_flow_entry["action_attempt_count"] = _attempt
        authorized_flow_entry["action_started_at"] = datetime.now(UTC).isoformat()
        state.append_transcript(
            "decision_action_started",
            producing_run_id=authorized_flow_entry.get("producing_run_id"),
            authorized_action=authorized_flow_entry.get("authorized_action"),
            authorized_target_node=node_type,
            prior_state=authorized_flow_entry["action_prior_state"],
            action_attempt_count=_attempt,
        )

    # ── 顺延的裁决必须真的送到 Analysis 手上（v2.1 P3d）──────────────────
    # 顺延只是"不问人"，不是"没人管"。experiment 那轮的 reviewer 建议、
    # infeasible 强制 REDIRECT 之类的框架结论，都记在 flow entry 上；如果不
    # 交到接手的 Analysis 面前，它就只是个躺在 hook_state 里的字段 ——
    # 机制存在但没接到路径，今晚已经栽过好几次。
    _deferred = next(
        (e for e in (state.hook_state.get("pending_post_node_flow") or [])
         if e.get("decision_state") in ("deferred_to_analysis", "action_in_progress")
         and (e.get("deferred_to_node") or "") == node_type),
        None,
    )
    if _deferred is not None:
        _brief = [
            f"上一个 producing 节点 {_deferred.get('producing_node')!r}"
            f"（run {_deferred.get('producing_run_id')}）已跑完，"
            "review 与 curator 均通过，本轮**裁决顺延给你**（不占用人工决策）。",
            f"框架/reviewer 的推荐动作：{_deferred.get('decision_recommended_action')!r}",
        ]
        _fb = str(_deferred.get("recommended_feedback") or "").strip()
        if _fb:
            _brief.append("reviewer 反馈：" + _fb[:1200])
        _brief.append(
            "请读它的产物、更新假说状态与计划，并出新一版 research_state；"
            "你的 verdict 决定下一步。"
        )
        node_inputs = {**(node_inputs or {}),
                       "顺延给你的裁决（必读）": "\n".join(_brief)}

    # ── 任务身份：解析 + 核对（#1080 第 3/4 条）────────────────────────────
    _identity, _identity_error = _resolve_task_identity(
        state, node_type, task_instance_uuid, task_contract_digest)
    if _identity_error:
        return _identity_error
    _dispatch_id = f"disp_{uuid4().hex}"

    state.append_transcript(
        "subagent_call_start",
        child_node_type=node_type,
        child_depth=new_depth,
        background=background,
        forwarded_artifacts=[a["type"] + ":" + a["name"] for a in upstream_artifacts],
        autoresolved=autoresolved_note,
        node_inputs_preview=str(node_inputs or {})[:300],
        # 这次派发的身份（#1080 第 4 条）。子 run 的 `parent_dispatch_id` 就是它
        # —— 审计据此把「父这边的哪一次调用」和「子那边的哪一个 run」接上。
        dispatch_id=_dispatch_id,
        task_instance_uuid=(_identity or {}).get("task_instance_uuid"),
        task_contract_revision=(_identity or {}).get("task_contract_revision"),
        task_contract_digest=(_identity or {}).get("task_contract_digest"),
    )

    sub_run_id = f"{state.node_type}->{node_type}@d{new_depth}"
    # ── 被打断的同类 run：**续它，不新开**（wangd 2026-08-18）──────────────
    #
    #     「上一个 curator 被打断，然后就让继续，然后新开一个 curator ——
    #       我觉得这都非常的离谱。」
    #
    # 离谱之处不只是浪费：新开的那个会把已经做过的不幂等操作再做一遍
    # （实测 Q1 命题在 KB 被注册两次）。而续跑的原材料一直都在 —— 每轮写的
    # messages_checkpoint 就在那个 run 目录里。
    #
    # 这一步**机械**：判据是"同类型 + 被打断 + 有 checkpoint"，不问模型。
    # 模型要真想放弃重来，显式传 resume_run_id="fresh"。
    _resume_run_id: str | None = None
    if resume_run_id == "fresh":
        state.append_transcript(
            "run_node_fresh_requested", node_type=node_type)
    elif resume_run_id:
        _resume_run_id = resume_run_id
        # 显式指名续跑：身份对不上**报错**，既不静默新开也不静默续上
        # （#1080 验收 5）。挑出那条 run 的记录来比 —— 比不到就是另一回事，
        # 交给 executor 的续跑失败路径（#1081）如实说。
        if _identity:
            _target = next(
                (info for info in scan_interrupted_child_runs(state, scope="all")
                 if info["run_id"] == resume_run_id), None)
            _why = task_identity_mismatch(_target, _identity) if _target else None
            if _why:
                return {
                    "status": "error",
                    "error_code": "resume_task_identity_mismatch",
                    "error": (
                        f"run {resume_run_id} 服务的不是这个任务：{_why}。\n"
                        "续上去等于让同一个 run 先后服务两件不同的事（那正是 #1052 里"
                        "新任务落回旧 child 上下文的形状）。要么带对身份，要么"
                        "`resume_run_id='fresh'` 显式新开。"),
                    "requested_resume_run_id": resume_run_id,
                }
    else:
        _candidate = _resumable_run_for(state, node_type, task_identity=_identity)
        if _candidate:
            _resume_run_id = _candidate["run_id"]
            state.append_transcript(
                "run_node_auto_resumed",
                node_type=node_type,
                resumed_run_id=_resume_run_id,
                n_tool_calls_before=_candidate["n_tool_calls"],
            )

    # ── 宣告放在**所有闸门之后**（wangd 2026-08-18 实测）─────────────────
    #
    # 这句话原来发在函数开头 —— 而它和真正的派发之间隔着 18 个 error 出口
    # （节点名校验 / 白名单 / 服务节点同步要求 / worktree 单车道 / 冻结闸 /
    # 权限 …）。任何一个挡下来，用户已经看到"我要去做 X 了"，而 X 从没发生。
    #
    # 实测后果：writing 被宣告 17 次，磁盘上只有 1 条 writing run —— 对话里
    # 16 句没兑现的承诺，用户看到的就是"怎么一直在说同一件事"。
    #
    # 宣告是**对用户的承诺**，必须发在承诺能兑现的地方：过了所有闸门、下一步
    # 就是真的跑。这一行之后不再有 pre-dispatch 的拒绝出口。
    state.append_transcript(
        "node_dispatch_announced", node_type=node_type, user_note=_note[:1000],
        background=bool(background),
    )

    # ── 交付投影契约（2026-08-31）：派发时就把路径验掉 ────────────────────
    # 判据与落盘端同一份（validate_deliverable_projection）。在这里报错，
    # 好过让子节点跑完 26 分钟再发现用户点名的文件写不进去。
    if deliverable:
        from core.project_workspace import (
            ProjectWorkspaceError,
            validate_deliverable_projection,
        )

        _dl_type = str(deliverable.get("artifact_type") or "").strip()
        _dl_path = str(deliverable.get("path") or "").strip()
        if not _dl_type or not _dl_path:
            return {"status": "error",
                    "error": "deliverable 要给全两个字段："
                             '{"artifact_type": <节点会产的类型>, "path": <用户点名的文件>}'}
        _root = getattr(state, "project_worktree", None)
        if _root is not None:
            try:
                _dl_path = validate_deliverable_projection(_root, node_type, _dl_path)
            except ProjectWorkspaceError as exc:
                return {"status": "error", "error": str(exc)}
        deliverable = {"artifact_type": _dl_type, "path": _dl_path}

    exec_kwargs = dict(
        node_type=node_type,
        state_dir=base_dir,
        project_id=state.project_id,
        node_inputs=node_inputs or {},
        upstream_artifacts=upstream_artifacts,
        selected_input_ids=selected_input_ids,
        parent_state=state,
        depth=new_depth,
        sub_run_id=sub_run_id,
        llm=LLMClient(),
        resume_run_id=_resume_run_id,
        deliverable=deliverable,
        # 「这个任务允许中途停下作业吗」只能由派发方回答（#1084 第二节）。
        planned_stop_authorized=bool(planned_stop_authorized),
        planned_stop_note=str(planned_stop_note or ""),
        # 任务与派发身份进 child State 与 run_start（#1080 第 4 条）。
        task_instance_uuid=(_identity or {}).get("task_instance_uuid"),
        task_contract_revision=(_identity or {}).get("task_contract_revision"),
        task_contract_digest=(_identity or {}).get("task_contract_digest"),
        parent_dispatch_id=_dispatch_id,
    )

    # #143 gap 3：绑定的 task 随 run 一起进 in_progress（best-effort）。
    _task_start_best_effort(state, task_id, node_type)

    # ── background 模式：进程内后台跑，立即返回（R1 异步基座 v1）────────────
    if background:
        task = asyncio.create_task(
            _run_child_background(
                state,
                node_type,
                node_inputs,
                child_harness,
                autoresolved_note,
                exec_kwargs,
                task_id,
            )
        )
        _BACKGROUND_TASKS.add(task)
        task.add_done_callback(_BACKGROUND_TASKS.discard)
        _bg_out: dict = {
            "status": "started_background",
            "child_node_type": node_type,
            "note": (
                f"{node_type} 已在后台启动。完成/暂停/失败会自动通知你和用户；"
                "在那之前正常继续对话即可，**不要**轮询等待，也不要重复起同一个节点。"
            ),
        }
        if _service_pending:
            _pending = {
                "node_type": node_type,
                "sub_run_id": sub_run_id,
                "requested_by_run_id": state.run_id,
                "started_at": datetime.now(UTC).isoformat(),
            }
            state.hook_state.setdefault("pending_service_results", []).append(_pending)
            state.append_transcript("service_result_pending", **_pending)
            _bg_out["service_result_pending"] = _pending
            _bg_out["note"] += (
                f"\n⚠️ {node_type} 是**服务节点**（post_run_flow: none）—— 它的返回值就是"
                "你要的东西。这笔账已登记（pending_service_results）：**服务结果未到不得"
                "定稿**（不许用占位符顶替它的产出）；结果到货会作为系统消息回报并销账。"
            )
        if _dispatch_deviations:
            _bg_out["dispatch_deviations"] = _dispatch_deviations
        return _bg_out

    try:
        summary = await _execute_with_infra_retry(state, node_type, exec_kwargs)
    except Exception as exc:
        _reset_authorized_action(
            state,
            node_type,
            reason=f"{type(exc).__name__}: {exc}",
        )
        raise

    # ── 子 run pause → 把 pause 沿着 run_node 工具结果**冒泡**给父 loop ───
    # 父 agent_loop 检测到本工具返回 status="pause" 也会跟着 unwind。
    if summary.get("status") == "paused":
        # 给 paused 子 run 补上 parent_tool_call_id —— chat.py cascade resume 用
        from core.pause import get_paused_run

        child_ctx = get_paused_run(summary["paused_run_id"])
        if child_ctx is not None:
            # 父 agent_loop 还没把 tool_call_id 暴露给我；用 state.hook_state 临时存
            child_ctx.parent_tool_call_id = state.hook_state.get(
                "_current_tool_call_id_being_executed"
            )
        state.append_transcript(
            "subagent_call_paused",
            child_node_type=node_type,
            child_run_id=summary["run_id"],
            pause_question=(summary.get("pause_event") or {}).get("question", "")[:200],
        )
        return {
            "status": "pause",
            "pause_event": summary.get("pause_event") or {},
            "child_run_id": summary["run_id"],
            "child_node_type": node_type,
            "note": "child run paused for human input; will resume after answer",
        }

    out = _finish_child(
        state, node_type, node_inputs, summary, child_harness, autoresolved_note, task_id=task_id
    )

    # ── post-producing flow 归运行时（Move 1d，2026-08-19）────────────────
    #
    # 此前这条链由调度器**逐步照抄提醒执行**：hook 每轮把下一步调用连参数打印
    # 出来，模型抄一遍，框架再用一排墙防它抄错。铁证就是那份提醒本身 ——
    # **框架已经能把答案连参数一起打印出来**，能被打印出来的东西不是判断，是手续。
    # 代价：调度器烧掉一个 session 32M tokens 里的 16.2M（50.5%），大头在这条走廊
    # 上抄参数、撞墙、读错误、换个姿势再抄。
    #
    # 现在：一次 `run_node(<producing>)` 调用内部把 review → decision 走完，
    # 返回的要么是决策 pause（等人），要么是这一步的结果。调度器一轮都不花在
    # 手续上，只在真正要判断的地方被叫醒。
    flow_out = await _run_post_producing_flow(state, node_type, out)
    result = flow_out if flow_out is not None else out
    if _dispatch_deviations and isinstance(result, dict):
        result = {**result, "dispatch_deviations": _dispatch_deviations}
    return result


async def _run_child_background(
    parent_state: State,
    node_type: str,
    node_inputs: dict | None,
    child_harness,
    autoresolved_note: dict | None,
    exec_kwargs: dict,
    task_id: str | None = None,
) -> None:
    """后台包装：跑 child + 完成后做同一套 bookkeeping + 双路通知。"""

    try:
        summary = await _execute_with_infra_retry(parent_state, node_type, exec_kwargs)
    except Exception as e:
        _reset_authorized_action(
            parent_state,
            node_type,
            reason=f"background {type(e).__name__}: {e}",
        )
        # #143 gap 3：后台崩溃也机械 block 绑定 task（否则永久卡 in_progress）。
        _task_block_best_effort(
            parent_state, task_id, f"后台 {node_type} 崩溃：{type(e).__name__}: {e}"
        )
        _inject_to_parent(
            parent_state,
            f"[后台子节点回报] {node_type} 后台运行崩溃：{type(e).__name__}: {e}。"
            "需要的话检查 runs 目录下的 transcript 或重跑。",
            "background_child",
        )
        _notify_child_event(
            {"kind": "failed", "node_type": node_type, "error": f"{type(e).__name__}: {e}"}
        )
        return

    if summary.get("status") == "paused":
        # 后台 child 等人工输入：登记续跑上下文，提示用 /answer 答复
        run_id = summary.get("run_id", "?")
        question = (summary.get("pause_event") or {}).get("question", "")
        _BG_PAUSED_CONTINUATIONS[run_id] = {
            "parent_state": parent_state,
            "node_type": node_type,
            "node_inputs": node_inputs,
            "child_harness": child_harness,
            "autoresolved_note": autoresolved_note,
            "task_id": task_id,
        }
        _inject_to_parent(
            parent_state,
            f"[后台子节点回报] {node_type}[{run_id}] 暂停等人工输入："
            f"{question[:300]} —— 用户用 /answer <答复> 直接回给它。",
            "background_child",
        )
        _notify_child_event(
            {"kind": "paused", "node_type": node_type, "run_id": run_id, "question": question}
        )
        return

    out = _finish_child(
        parent_state,
        node_type,
        node_inputs,
        summary,
        child_harness,
        autoresolved_note,
        task_id=task_id,
    )
    _report_background_done(parent_state, node_type, summary, out)


def _report_background_done(parent_state: State, node_type: str, summary: dict, out: dict) -> None:
    run_id = summary.get("run_id", "?")
    status = summary.get("status", "?")
    # 服务结果到货即销账（与 run_node 里的 service_result_pending 成对）。
    _pending = parent_state.hook_state.get("pending_service_results")
    if isinstance(_pending, list):
        _left = [e for e in _pending if not (isinstance(e, dict) and e.get("node_type") == node_type)]
        if len(_left) != len(_pending):
            parent_state.hook_state["pending_service_results"] = _left
            parent_state.append_transcript(
                "service_result_delivered", node_type=node_type, run_id=run_id, status=status)
    imported_ids = [
        a.get("id") for a in (out.get("imported_artifacts") or []) if isinstance(a, dict)
    ]
    _inject_to_parent(
        parent_state,
        f"[后台子节点回报] {node_type}[{run_id}] 已结束，status={status}，"
        f"turns={summary.get('turns')}，回填 artifact={imported_ids or '无'}。"
        f"若它是 producing 节点且 completed：post-producing 3-step flow 已登记，"
        f"按流程推进（_reviewer → _curator → present_decision_package）。"
        f"向用户汇报结果时引用真实产出，不要凭记忆编。",
        "background_child",
    )
    _notify_child_event(
        {
            "kind": "completed",
            "node_type": node_type,
            "run_id": run_id,
            "status": status,
            "turns": summary.get("turns"),
            "imported": imported_ids,
            # #1097 第 4 条：后台回报至少要留住 status + effect 摘要，否则
            # "后台跑的"和"前台跑的"在父侧是两种语义。
            "child_obligation_effect": summary.get("child_obligation_effect"),
            "node_task_outcome": summary.get("node_task_outcome"),
        }
    )


async def resume_background_paused(ctx, answer: str) -> None:
    """chat.py /answer 入口：resume 一个 pause 中的后台 child 并走完收尾。

    ctx: core.pause.PausedRunContext（get_deepest_paused() 拿到的）。
    """
    from core.agent_loop import resume_loop
    from core.executor import finalize_run

    run_id = ctx.run_id
    cont = _BG_PAUSED_CONTINUATIONS.pop(run_id, None)
    try:
        result = await resume_loop(ctx, answer)
    except Exception as e:
        _notify_child_event(
            {
                "kind": "failed",
                "node_type": ctx.state.node_type,
                "run_id": run_id,
                "error": f"resume 失败 {type(e).__name__}: {e}",
            }
        )
        return

    if result.status == "paused":
        # 又一次 pause → 保留续跑上下文，等下一个 /answer
        if cont:
            _BG_PAUSED_CONTINUATIONS[run_id] = cont
        q = result.pause_event.question if result.pause_event else ""
        _notify_child_event(
            {"kind": "paused", "node_type": ctx.state.node_type, "run_id": run_id, "question": q}
        )
        if cont:
            _inject_to_parent(
                cont["parent_state"],
                f"[后台子节点回报] {ctx.state.node_type}[{run_id}] 回答后又一次"
                f"暂停等输入：{q[:300]} —— 用户继续用 /answer 答复。",
                "background_child",
            )
        return

    summary = await finalize_run(ctx.state, ctx.harness, result, ctx.llm, depth=ctx.state.depth)
    if cont:
        out = _finish_child(
            cont["parent_state"],
            cont["node_type"],
            cont["node_inputs"],
            summary,
            cont["child_harness"],
            cont["autoresolved_note"],
            task_id=cont.get("task_id"),
        )
        _report_background_done(cont["parent_state"], cont["node_type"], summary, out)
    else:
        _notify_child_event(
            {
                "kind": "completed",
                "node_type": ctx.state.node_type,
                "run_id": run_id,
                "status": summary.get("status"),
                "turns": summary.get("turns"),
                "imported": [],
            }
        )


# ── curator 整合门禁的机械判据（issue #229）─────────────────────────────────
#
# 门禁凭据必须是"目标 artifact 真被整合过"，不是"curator 子 run 返回了"。
# receipt 由 nodes/_curator/hooks.py 在 run 结束机械生成（扫工具调用记录：目标
# 是否成功读取 + 是否被 scan_artifact_disagreements 真正扫到），这里只负责读它
# 并决定放不放行 —— 判定与模型自述无关。


def _curator_receipt_from_child(summary: dict) -> dict | None:
    """从 curator 子 run 的 transcript 读最新一条 integration receipt。"""
    state_dir = summary.get("state_dir")
    if not state_dir:
        return None
    p = Path(state_dir) / "transcript.jsonl"
    if not p.exists():
        return None
    latest = None
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            if e.get("event") == "curator_integration_receipt":
                latest = e
    except OSError:
        return None
    return latest


def _curator_integration_verdict(summary: dict, ni: dict) -> tuple[bool, str, dict]:
    """(放行?, 不放行的原因, receipt)。

    放行条件（缺一不可）：
      1. 子 run status == completed；
      2. 有 receipt（hook 跑过 —— 没有就是审计缺失，fail-closed）；
      3. receipt.n_unintegrated == 0（每个目标都读到且扫到）。
    合法 no-op（扫全后零候选）照常放行 —— 卡的是"没扫"，不是"没写"。
    """
    receipt = _curator_receipt_from_child(summary) or {}
    status = summary.get("status")
    if status != "completed":
        return False, (f"curator 子 run status={status!r}（非 completed），整合未完成。"), receipt
    if not receipt:
        return (
            False,
            (
                "curator 子 run 没有 integration receipt 事件（"
                "curator_integration_receipt hook 未跑）—— 审计缺失，fail-closed。"
            ),
            receipt,
        )
    if not receipt.get("applicable", True):
        return True, "", receipt  # 非 integration 模式，不该走到这里
    n_un = int(receipt.get("n_unintegrated") or 0)
    if n_un > 0:
        return False, (receipt.get("reason") or f"{n_un} 个目标 artifact 未完成读取+扫描"), receipt
    return True, "", receipt


async def _run_post_producing_flow(
    state: State, node_type: str, finished: dict
) -> dict | None:
    """producing 节点交付之后，由**运行时**把 review → decision 走完。

    返回决策 pause（要人答复）或 None（这条链不适用 / 无需呈递，照常返回原结果）。

    为什么放在派发工具里而不是新起一个组件：这条链的两端本来就在这里 ——
    "子节点跑完了"和"要起新节点了"都从 `run_node` 过。把它做成一次调用内部的
    顺序执行，比新增一个需要被调用的编排器更少接缝（`调它，别再实现一遍`）。

    不适用的情况一律返回 None，让调用方拿到原本的结果：
      - 这个节点不欠 flow（服务节点 / 系统节点 / 未交付）
      - 本轮没有为它登记 flow entry（例如 status != completed）
      - entry 已经不在 pending list（被别的路径关掉了）
    """
    if not node_owes_post_node_flow(node_type):
        return None
    flow = state.hook_state.get("pending_post_node_flow") or []
    entry = next(
        (e for e in reversed(flow)
         if e.get("producing_node") == node_type
         and e.get("producing_run_id") == (finished or {}).get("child_run_id")),
        None,
    )
    if entry is None:
        return None

    # ── Step 1：独立审查 ────────────────────────────────────────────────
    # reviewer 失败不 block —— 失败原因带进决策包，由人裁决（既有语义不变）。
    if entry.get("review_state") == "pending":
        artifact_ids = list(entry.get("artifact_ids") or [])
        if not artifact_ids:
            # 没有可审的产物就没有可审的东西。如实记账，跳到呈递。
            entry["review_state"] = "skipped"
            state.append_transcript(
                "post_node_review_skipped_no_artifacts",
                producing_run_id=entry.get("producing_run_id"))
        else:
            # user_note 是**无条件**必填（2026-08-17：派发前必须先跟用户说一句）。
            # 运行时自己派也一样要说 —— 用户在对话里看不到节点内部过程，这一句
            # 是他唯一的信号，而"谁发起的"不改变这一点。
            #
            # 2026-08-19 本地 e2e 抓到：第一版这里没传，于是运行时派 reviewer
            # **当场被自己的门拦下**，reviewer 根本不跑，流程带着
            # review_state=pending 直接去呈递决策。八条单测全绿 —— 因为它们把
            # `_run_node_tool` 整个 mock 掉了，边界层不在场。
            _rv = await _run_node_tool(
                state,
                "_reviewer",
                node_inputs={
                    "artifact_id": _review_target(node_type, artifact_ids),
                    "source_node_type": node_type,
                    "producer_run_id": entry.get("producing_run_id"),
                },
                forward_artifact_ids=artifact_ids,
                user_note=(f"{node_type} 交付完成，我先请独立审查看一遍"
                           f"（{_review_target(node_type, artifact_ids)}），再把结果连同审查意见一起交给你裁决。"),
            )
            # **派发失败要说出来。**
            #
            # 第一版这里不看返回值：派发一旦失败（artifact 转发不到、节点自己
            # 报错、门禁拦下），流程照样往下走，呈递出来的决策包是
            # `review_state=failed_awaiting_human` + `review_failed_reason=None`
            # —— 人看到"审查失败"，但没有任何原因。而失败原因**此刻就在手上**。
            #
            # 2026-08-19 本地 e2e 抓到。这是「fail-open 十有八九是防线边界划错」
            # 的另一种形态：不是没防，是防了却把手上的事实丢了。
            if isinstance(_rv, dict) and _rv.get("status") == "error":
                entry["review_failed_reason"] = str(_rv.get("error") or "")[:600]
                state.append_transcript(
                    "runtime_reviewer_dispatch_failed",
                    producing_run_id=entry.get("producing_run_id"),
                    error=entry["review_failed_reason"])

    # ── Step 2：呈递决策包 ──────────────────────────────────────────────
    if entry.get("decision_state") not in (None, "pending", "awaiting_human"):
        return None
    from shared.tools.library.decision_package import _present_decision_package

    return await _present_decision_package(
        state,
        source_node_type=node_type,
        producing_run_id=str(entry.get("producing_run_id") or ""),
        producing_summary=str((finished or {}).get("final_text_preview") or "")[:800],
        # 自由文本只展示，机械事实另走一路（#1097 第 4 条）。
        child_obligation_effect=(finished or {}).get("child_obligation_effect"),
        artifact_ids_produced=list(entry.get("artifact_ids") or []),
        review_critique_artifact_id=entry.get("review_critique_artifact_id"),
        review_failed_reason=entry.get("review_failed_reason"),
    )


def _finish_child(
    state: State,
    node_type: str,
    node_inputs,
    summary: dict,
    child_harness,
    autoresolved_note: dict | None,
    task_id: str | None = None,
) -> dict:
    """子 run 结束后的共享 bookkeeping（前台 run_node 与后台包装共用）：
    回填 required outputs、登记 post-producing flow、reviewer/curator 状态推进、
    transcript 记账，返回给 LLM 看的精简结果。"""
    # ── 回填 required_output_artifact_types：从子 run dir 复制到父 ──────────
    imported = (
        []
        if getattr(state, "project_worktree", None) is not None
        else _import_required_outputs(state, summary, child_harness)
    )

    # "这次子 run 算不算真的交付了东西" —— 这个判据在下面 flow 登记处也要用。
    # 它必须**只有一个定义**：v1 看 imported artifact，v2 看写进 Git 节点目录的
    # 路径。此前这里用裸 `imported`、下面用 `_delivered`，于是 v2 下
    # （imported 恒为 []）被授权的返修/顺延永远关不掉，flow 卡死。
    # 同一个事实推导两遍，两遍就会不一致 —— 这已经是今晚第三次了。
    _delivered = bool(imported) or bool(
        (summary.get("project_workspace") or {}).get("paths")
    )

    # A REVISE/REDIRECT decision remains an active state transition until its
    # authorized producer really returns a completed replacement.  Only then
    # retire the old reviewed round; the normal block below immediately creates
    # a fresh reviewer flow for the replacement artifacts.
    authorized_predecessor = _find_in_progress_entry(
        state.hook_state.get("pending_post_node_flow"), node_type)
    if authorized_predecessor is not None:
        if summary.get("status") == "completed" and _delivered:
            old_run_id = authorized_predecessor.get("producing_run_id")
            state.hook_state["pending_post_node_flow"] = [
                entry
                for entry in (state.hook_state.get("pending_post_node_flow") or [])
                if entry is not authorized_predecessor
            ]
            state.append_transcript(
                "decision_action_completed",
                producing_run_id=old_run_id,
                authorized_action=authorized_predecessor.get("authorized_action"),
                authorized_target_node=node_type,
                replacement_run_id=summary.get("run_id"),
                imported_artifact_ids=[item.get("id") for item in imported],
                old_flow_closed=True,
            )
        else:
            _reset_authorized_action(
                state,
                node_type,
                reason=(
                    f"child status={summary.get('status')!r}; "
                    f"imported_required_outputs={len(imported)}"
                ),
            )

    # v0.4：producing 节点完成 → 自动登记 post-node flow（review + curator + decision package）
    #
    # v2.1 修正：交付信号必须同时认 v1 的 imported artifact 和 v2 的 Git 目录
    # 写入。此前只认 imported —— Project v2 的交付方式是写自己的节点目录，
    # 于是 v2 下**永远登记不上 flow**；而下面 #143 那道 reviewer 门要求
    # "有 matching entry 才放行"，两者相加会拦掉 v2 的所有 review，所以当初
    # 直接给那道门加了 `project_worktree is None` 把 v2 整个排除掉。
    # 后果（2026-08-07 E2E 实测）：v2 下任何 producer —— 哪怕自己的机械输出
    # 校验没过 —— 都能被派 reviewer。hypothesis 跑满 40 轮、output validation
    # 失败，orchestrator 照样起 reviewer 又跑 40 轮去审这个不合格产物，
    # 4.6M token 里大部分烧在这，最终零 artifact。
    # 同一份"算不算交付"的判据本来就在下面的 can_start_standard_review 里
    # （imported or project_files），这里对齐它，而不是把门关掉。
    # （_delivered 在上面 authorized_predecessor 之前就算好了 —— 那里也要用它。）
    if node_owes_post_node_flow(node_type) and summary.get("status") == "completed" and _delivered:
        # 检查 owner 是否 opt-out reviewer（child_harness.skip_post_node_review）
        skip_review = bool(getattr(child_harness, "skip_post_node_review", False))

        # 只收**有 owner 声明**的交付物。此前这里收子 run 产出的全部 artifact，
        # 于是 summarizer 自动落盘的 compression_log 被列成 curator 的整合目标，
        # 而 scan_artifact_disagreements 结构上扫不到它 → n_unintegrated 恒为 1
        # → curator_state 恒 pending → 下游永远被拦（2026-08-19 实测死锁）。
        # 判据在类型注册表里声明一次：shared.lib.artifact_policy.framework_internal。
        from shared.lib.artifact_policy import integration_targets

        artifact_ids = integration_targets([
            a["id"] for a in _child_produced_artifacts(summary, imported) if a.get("id")
        ])
        flow_entry = {
            "producing_node": node_type,
            "producing_run_id": summary["run_id"],
            # 这一轮**原来**的任务输入。REVISE / REDIRECT 由运行时执行时要带着它
            # 重跑（`core.pause_driver._execute_authorized_action`）：只给反馈、
            # 不给原任务的重跑，节点这边定义不出该做什么 —— experiment 要从这里
            # 取 experiment_spec / prereg_artifact_id 这类锚点。hook 告诉调度器的
            # 说法本来就是「original research inputs + reviewer feedback」
            # （`core/loop_hooks_builtin.py`），运行时自己派发时不该说另一套。
            "producing_node_inputs": (
                dict(node_inputs) if isinstance(node_inputs, dict) else {}
            ),
            # #183：绑定发起本 run 的 task，让 task(complete) 一致性 gate 能精确
            # 匹配（没传 task_id 时为 None → gate 保守拦截，见
            # decision_package.blocking_decision_for_task）。
            "task_id": task_id,
            "artifact_ids": artifact_ids,
            "at": summary.get("started_at") or summary.get("run_id"),
            "review_state": ("skipped" if skip_review else "pending"),
            "review_critique_artifact_id": None,
            "review_failed_reason": None,
            "decision_state": "pending",
        }
        state.hook_state.setdefault("pending_post_node_flow", []).append(flow_entry)

        # 不再登记 pending_curator_integrations。
        #
        # curator 从「每个 producing 节点跑完都要走的收尾步」改成**按需调取的
        # 后台节点**（wangd 2026-08-19）。判据是实测：同一个 session 里 curator
        # 跑了 5 次、KB 写入 **0** 次，三次明确 verdict=ok_no_op（扫全了零候选），
        # 代价 2.42M tokens —— 比真去查文献的 literature 节点还贵。
        #
        # 为什么不是「跳过时记账」：记账等于把执行当默认、把不执行当例外，负担
        # 仍在「不做」这一侧。按需执行的默认就是**不执行**，没有要解释的东西。

    elif node_type == "_reviewer":
        # v3.2 根因级（2026-07 v9 dogfood）：reviewer 的产出是 **critique（信号）**，
        # 不是 deliverable。review 是否"成功"应当只取决于「有没有产出一份可读的
        # critique」，而**不是** reviewer run 自己的 status。实测教训：一份精准抓到
        # P_SK=-2.7 参考值错误（severity=critical）的 critique，因 recommended_action
        # enum 漂移 + n_concerns 类型小瑕疵把 reviewer run 判成 incomplete，旧代码
        # 就把它标 review_state='failed' 丢掉 critique_id —— 检查机制反噬检查结果。
        # 现在：只要产出了 critique 就 done（带格式告警），真 failed 仅"根本没产出"。
        # 防御：imported 可能含非 dict 项；node_inputs 可能是 str（LLM 误传 JSON 字符串）
        try:
            review_artifacts = [
                a for a in _child_produced_artifacts(summary, imported)
                if a.get("type") == "review_critique"
            ]
        except Exception:
            review_artifacts = []
        # `[-1]` 而不是 `[0]`：State.list_artifacts 的契约明写「按 created_at
        # 升序，**末位 = 最新**」。取 [0] 拿到的是**最老**那份 —— 2026-09-01
        # 本机 E2E 实测：reviewer 09:56 刚写出
        # `review_critique__experiment_critique_variance_trajectory_linear_mechanism`，
        # 账本里记下的却是 8-30 那份基线 critique，于是
        #   · flow entry 的 review_critique_artifact_id 从一开始就是陈年的；
        #   · decision_package 的「账本权威」拿到的也是陈年的（账本被同一个
        #     错误来源毒化，两边一致所以看不出分歧）；
        #   · 只剩「critique 早于本 run 开跑」这道兜底闸能拦住，连拦三轮。
        review_critique_id = (
            review_artifacts[-1].get("id")
            if review_artifacts and isinstance(review_artifacts[-1], dict)
            else None
        )
        produced = review_critique_id is not None
        ni = node_inputs if isinstance(node_inputs, dict) else {}
        target_producing_run = ni.get("producer_run_id")
        for entry in state.hook_state.get("pending_post_node_flow") or []:
            # #151/#155：匹配首审(pending) 和**人工授权过的重试**(retry_authorized)。
            # retry 成功要能把它翻成 done，否则重试出了 critique 也永远卡住。
            if entry.get("review_state") in ("pending", "retry_authorized") and (
                target_producing_run is None
                or entry.get("producing_run_id") == target_producing_run
            ):
                _was_retry = entry.get("review_state") == "retry_authorized"
                entry["review_attempt_count"] = int(entry.get("review_attempt_count") or 0) + 1
                entry["review_last_run_id"] = summary.get("run_id")
                if produced:
                    entry["review_state"] = "done"
                    entry["review_critique_artifact_id"] = review_critique_id
                    entry.pop("review_retryable", None)
                    entry.pop("retry_authorized_at", None)
                    entry.pop("retry_authorized_by", None)
                    entry["review_failed_reason"] = None
                    if _was_retry:
                        # #155 现象 4：retry 拿到**新** critique → 之前那轮基于
                        # "review 失败"做的 curator/decision 全部作废，必须基于
                        # 新 critique 重新走 curator → decision，否则下游会拿着
                        # 旧状态放行。
                        entry["decision_state"] = "pending"
                        entry["accepted_action"] = None
                        state.append_transcript(
                            "review_retry_succeeded_chain_reset",
                            producing_run_id=entry.get("producing_run_id"),
                            new_critique_artifact_id=review_critique_id,
                            review_attempt_count=entry["review_attempt_count"],
                        )
                    if summary.get("status") != "completed":
                        # critique 内容进决策，但如实标注它自己的 run 格式没过 qc
                        entry["review_format_warning"] = (
                            f"reviewer run status={summary.get('status')!r}"
                            f"（多半 metadata 格式没过自身 qc）；critique 内容已产出并纳入决策"
                        )
                else:
                    # #155：不再自己回到"可重试"——回到**等人工授权**。
                    # 框架不自动重试（会无限循环烧钱），必须再过一次 decision package。
                    entry["review_state"] = "failed_awaiting_human"
                    entry["review_retryable"] = True
                    entry.pop("retry_authorized_at", None)
                    entry.pop("retry_authorized_by", None)
                    entry["review_failed_reason"] = (
                        f"reviewer status={summary.get('status')!r} 且未产出 review_critique"
                        f"（第 {entry['review_attempt_count']} 次尝试）；"
                        f"final_text={(summary.get('final_text') or '')[:150]!r}"
                    )
                break
        else:
            # 没匹配到 → 兼容性日志，不阻断
            state.append_transcript(
                "post_node_flow_review_orphan",
                producer_run_id=target_producing_run,
                review_critique_id=review_critique_id,
            )

    elif node_type == "_curator":
        ni = node_inputs if isinstance(node_inputs, dict) else {}
        if ni.get("mode") in (None, "integration", "mode_1"):
            # curator 不再是 post-producing flow 的一环（wangd 2026-08-19）——
            # 它是**按需调取的后台节点**。所以这里没有任何 flow 状态迁移：没有
            # curator_state 可标 done，也没有门禁靠它解锁。
            #
            # #229 那条仍然成立：**跑了就得真跑过**。一个读到 artifact 却没成功
            # 扫描、没写 KB、最后空响应的 curator run（status 仍 completed）必须
            # 如实记成 rejected，否则调用方会以为整合过了。但这个判定不再驱动
            # 任何门禁，只驱动这条记录 —— 证据可持久化，判决不由这里做出。
            _ok, _why, _receipt = _curator_integration_verdict(summary, ni)
            state.append_transcript(
                "curator_integration_accepted" if _ok else "curator_integration_rejected",
                curator_run_id=summary.get("run_id"),
                child_status=summary.get("status"),
                verdict=(_receipt or {}).get("verdict"),
                n_targets=(_receipt or {}).get("n_targets"),
                n_kb_writes=(_receipt or {}).get("n_kb_writes"),
                n_proposals=(_receipt or {}).get("n_proposals"),
                reason=None if _ok else _why,
            )
    state.append_transcript(
        "subagent_call_end",
        child_node_type=node_type,
        child_run_id=summary.get("run_id"),
        child_status=summary.get("status"),
        child_turns=summary.get("turns"),
        imported_artifacts=[a["id"] for a in imported],
        pending_integration_registered=(
            node_owes_post_node_flow(node_type) and summary.get("status") == "completed"
        ),
    )

    # #143 gap 3：child 非 completed（incomplete/error/cancelled）→ 机械 block
    # 绑定 task，写清原因 + run id；否则 task 永久卡 in_progress，恢复项目时
    # 无法从 ledger 判断该阶段还在跑 / 失败待修 / 已可接受。completed 的常规
    # 流程走 reviewer→curator→decision，人工 PROCEED 后才 complete，这里不动。
    child_status = summary.get("status")
    if task_id and child_status != "completed":
        _task_block_best_effort(
            state,
            task_id,
            f"child {node_type}[{summary.get('run_id')}] status={child_status}",
        )

    # #143 gap 2/3：把"能否进标准 review"显式暴露给 orchestrator，
    # 别只给模糊的 status=incomplete。
    project_files = (summary.get("project_workspace") or {}).get("paths") or []
    can_start_standard_review = bool(
        node_owes_post_node_flow(node_type)
        and child_status == "completed"
        and (imported or project_files)
    )

    # 给 LLM 看的精简返回。
    #
    # ⚠️ 全部 `.get()`：这一段原来有四个方括号取值，而 summary 有好几个产出口
    # （正常收尾 / provider 打死后的最小等价件 / 崩溃后读回的残件），并不是每
    # 个都写全字段。实测 `KeyError: 'turns'` 8 次（最近 2026-08-20）：**子节点
    # 其实跑完了**，只因为一个装饰性的计数字段缺席，整个 run_node 调用被判成
    # 异常，产出、状态、blocker 一起丢掉，调度器只看到一句 KeyError。
    #
    # 记账字段缺席不该销毁事实。缺 status 才是真的不知道结果，按 incomplete 记
    # —— 它会照常触发下面的 task block，比 None 一路往下漂安全。
    out = {
        "status": "success" if summary.get("status") == "completed"
                  else (summary.get("status") or "incomplete"),
        "child_run_id": summary.get("run_id"),
        "child_node_type": node_type,
        "child_status": summary.get("status") or "incomplete",
        "child_turns": summary.get("turns"),
        "can_start_standard_review": can_start_standard_review,
        "imported_artifacts": imported,
        "all_child_artifacts": summary.get("artifacts", []),
        "missing_required_outputs": summary.get("missing_required_outputs", []),
        "missing_input_artifact_types": summary.get("missing_input_artifact_types", []),
        "blockers": summary.get("blockers", []),
        "memory_candidates": summary.get("memory_candidates", []),
        "project_workspace": summary.get("project_workspace"),
        "final_text_preview": summary.get("final_text_preview", "")[:500],
        "child_state_dir": summary.get("state_dir"),
    }
    # 失败原因三元组过投影。这张"给 LLM 看的精简返回"是按名点收的 ——
    # 不点名，summary 里写得再清楚的 failure_category/failure_human 也到不了
    # 调度器嘴边，它就只能自己造句（实测造出"被框架错误打断"）。
    for key in ("failure_category", "failure_subcategory", "failure_human"):
        if summary.get(key):
            out[key] = summary[key]
    # #1081：请求了续跑但没续上 —— 必须说出来。不说的话，调用方拿到的是一份
    # 新 run 的正常 `status=success`，和"真的接着上一轮跑完了"逐字段相同，
    # 只有把入参里的 run id 和返回的 child_run_id 人工比一遍才看得出换过 run。
    # #1097 第 4 条：子 run 声明的 obligation effect 要**按名点收**地过河。
    # 这张返回是给父 LLM 和父记账用的；不点名，收据里写得再清楚也停在子 run 里。
    _effect = summary.get("child_obligation_effect")
    if isinstance(_effect, dict) and _effect:
        out["child_obligation_effect"] = _effect
        state.append_transcript(
            "child_obligation_effect",
            child_run_id=summary.get("run_id"),
            child_node_type=node_type,
            upstream_goal_effect=_effect.get("upstream_goal_effect"),
            scientific_contribution=_effect.get("scientific_contribution"),
            task_contract_revision_digest=_effect.get("task_contract_revision_digest"),
        )
    _resume = summary.get("resume") or {}
    if _resume.get("requested_run_id") and not _resume.get("resumed"):
        out["resume_failed"] = {
            "requested_run_id": _resume.get("requested_run_id"),
            "started_fresh_run_id": summary.get("run_id"),
            "reason": _resume.get("reason"),
        }
        out["note"] = (
            f"⚠️ 请求续跑 {_resume.get('requested_run_id')} 没能续上"
            f"（{_resume.get('reason')}），这一轮是**新开**的 "
            f"{summary.get('run_id')} —— 上一轮的上下文和进度都不在里面。"
        )
    if task_id:
        out["task_id"] = task_id
    if autoresolved_note:
        out["framework_autoresolved_inputs"] = autoresolved_note
    return out


def _translate_run_relative_paths(rec: dict, child_root: Path) -> tuple[dict, list[str]]:
    """把 record 里指向**子 run 内真实文件**的相对路径改写成绝对路径。

    2026-08-04 e2e8 实测：postprocess 画完图，figure artifact 里写的是

        metadata.image_path = "outputs/postprocess/figures/burden_vs_coverage.png"

    这条路径相对**子 run 的根**。回填给 writing 时字符串原样搬过去，于是它在
    writing 的坐标系里指向一个不存在的地方。服务把图交了，交的是一张只在对方
    坐标系里有效的地图 —— writing 装不上，最后编了个占位框塞进论文。

    跨 run 边界搬工件时，只搬字符串不搬语义，就会这样。（同一个函数今天上午
    刚修过 provenance 被洗白 —— 同一个位置的同一类缺陷。）

    规则很保守：**只翻译真的存在的东西**。候选串必须在子 run 根下解析到一个
    真实文件才改写，否则原样保留 —— 不猜、不造。返回 (新 record, 改写清单)。
    """
    translated: list[str] = []
    mapping: dict[str, str] = {}

    def _try(s: object) -> object:
        if not isinstance(s, str) or len(s) < 4 or "\n" in s:
            return s
        if s in mapping:
            return mapping[s]
        # 整段路径试探都要接住 OSError：pathlib 的 is_file() 只吞
        # ENOENT/ENOTDIR/EBADF/ELOOP 那几类，ENAMETOOLONG(36) 会原样抛穿 ——
        # 一段长 metadata 描述（"Synthetic settling pilot dataset: 225 …"）
        # 走到这里就把整次导入炸掉：子 run 完成了，父级却丢了全部产物
        # （issue #395-1）。字符串不是路径就原样保留，不猜、不炸。
        try:
            if Path(s).is_absolute():
                return s
            cand = (child_root / s).resolve()
            if not cand.is_file():
                return s
        except (OSError, ValueError):
            return s
        # 越界保护：解析结果必须仍在子 run 根内（防 ../.. 逃逸）
        try:
            cand.relative_to(child_root.resolve())
        except ValueError:
            return s
        mapping[s] = str(cand)
        translated.append(f"{s} → {cand}")
        return str(cand)

    def _walk(v: object) -> object:
        if isinstance(v, str):
            return _try(v)
        if isinstance(v, list):
            return [_walk(x) for x in v]
        if isinstance(v, dict):
            return {k: _walk(x) for k, x in v.items()}
        return v

    out = dict(rec)
    out["metadata"] = _walk(rec.get("metadata") or {})
    # content 里同样的相对路径（markdown 图片引用等）跟着一起改，否则
    # metadata 说一套、正文说另一套，消费方读哪个都可能错。
    content = rec.get("content")
    if isinstance(content, str) and mapping:
        for old, new in mapping.items():
            content = content.replace(old, new)
        out["content"] = content
    return out, translated


def _import_required_outputs(parent_state: State, child_summary: dict, child_harness) -> list[dict]:
    """把子 run 的 required_output artifact 复制回父 state.artifacts/。

    只复制 child_harness.required_output_artifact_types 列出的 type，避免父
    artifacts 列表被中间产物淹没。其它子产物保留在子 run dir，父可用
    read_external_artifact 显式拉取。
    """
    import json

    required = set(
        child_harness.required_output_artifact_types or child_harness.required_outputs or []
    )
    if not required:
        return []

    # v3.1（审计 correctness）：qc 失败 / 未完成的子 run，其 artifact **不再**
    # 自动回填父节点 —— 否则残次品会成为 auto-forward 的"最新"候选，静默绕过
    # review/curator/decision 三道门。父节点仍可用 read_external_artifact 显式
    # 拉取（有意识的选择 ≠ 自动扩散）。
    #
    # v3.2 根因级（2026-07 v9 dogfood）：隔离规则本是给 **producing 节点的
    # deliverable** 设的（半成品别自动流下去）。但它被一刀切套到所有子 run，
    # 包括 _reviewer —— 而 review_critique 是**信号**不是 deliverable：reviewer
    # 存在的唯一目的就是暴露问题，把它的 critique 隔离掉 = 让检查机制反噬检查
    # 结果。旧代码打了个 project_synthesis 专属补丁豁免，但 single_artifact scope
    # 的 critique（实测精准抓到 -2.7 参考值错误那份）照样被雪藏。
    # 根因修复：**按 artifact 角色隔离** —— review_critique（任意 scope）恒放行；
    # 只有 producing deliverable 的半成品才隔离。删掉 project_synthesis 特例补丁。
    is_completed = child_summary.get("status") == "completed"
    if not is_completed:
        log.info(
            "子 run %s status=%s ≠ completed —— 隔离 producing deliverable 残次品"
            "（review_critique 是信号，恒放行）。",
            child_summary.get("run_id"),
            child_summary.get("status"),
        )

    child_state_dir = Path(child_summary["state_dir"])
    from core.ledger import RecordStore

    child_store = RecordStore(child_state_dir / "artifacts", child_state_dir / "records.jsonl")
    if not child_store.ledger_path.exists():
        return []

    existing_ids = {a["id"] for a in parent_state.list_artifacts()}
    imported: list[dict] = []
    for child_art in child_summary.get("artifacts", []):
        if child_art["type"] not in required:
            continue
        rec = child_store.record(child_art["id"])
        if rec is None:
            log.warning("子 artifact 不在账本上：%s", child_art["id"])
            continue
        try:
            rec = dict(rec)
        except (TypeError, ValueError):
            log.warning("子 artifact 记录形状不对：%s", child_art["id"])
            continue

        # 按角色隔离：review_critique 是信号（不管什么 scope），恒放行进决策；
        # producing deliverable 的半成品才隔离。
        is_signal = rec.get("type") == "review_critique"
        if not is_completed and not is_signal:
            continue  # producing deliverable 残次品隔离；review_critique 恒放行

        # 跨 run 边界：子 run 里的相对文件引用在父 run 解析不到 —— 翻译成绝对
        # 路径，否则父节点拿到的是一张只在子 run 坐标系里有效的地图。
        rec, _path_fixes = _translate_run_relative_paths(rec, child_state_dir)
        if _path_fixes:
            parent_state.append_transcript(
                "artifact_paths_translated",
                artifact_id=child_art["id"],
                child_run_id=child_summary.get("run_id"),
                translations=_path_fixes,
            )

        meta = dict(rec.get("metadata") or {})
        fixture_origin = meta.get("_test_fixture_origin")
        if fixture_origin:
            from core.runtime_capabilities import (
                WRITING_FIXTURE_DELIVERY_CAPABILITY,
                has_runtime_capability,
            )

            if not has_runtime_capability(parent_state, WRITING_FIXTURE_DELIVERY_CAPABILITY):
                log.error(
                    "测试夹具 artifact %s 试图进入生产 parent —— 已隔离（origin=%s）",
                    child_art["id"],
                    fixture_origin,
                )
                parent_state.append_transcript(
                    "test_fixture_artifact_quarantined",
                    child_run_id=child_summary.get("run_id"),
                    artifact_id=child_art["id"],
                    fixture_origin=fixture_origin,
                )
                continue
        meta.setdefault("source_run_id", child_summary["run_id"])
        meta.setdefault("source_node_type", child_summary["node_type"])
        # ── 框架自有的产出方标记（不与上面两个 setdefault 撞车）───────────────
        # 为什么必须另起键名：`source_node_type` 在不同上下文含义**相反** ——
        #   • _import_required_outputs（这里）：产出该 artifact 的子节点
        #   • _reviewer 的 harness 契约：review_critique 里它表示**被审查对象**
        #     的来源节点（审 literature 的产物就写 literature）
        # setdefault 会保留 reviewer 先写的那个，于是导入后的 critique 上
        # `source_node_type='literature'`。PR#209 的 provenance 校验拿它当
        # "谁写的 critique"，把五次成功审稿全判成"不是独立 reviewer"、强制
        # retry_reviewer（qinp/jicq 2026-07-30 实测：5 个项目 reviewer 全
        # completed+approve，却全被要求重审）。
        # 这两个键**始终**由框架覆盖写入，节点不能伪造。
        meta["produced_by_node_type"] = child_summary["node_type"]
        meta["produced_by_run_id"] = child_summary["run_id"]

        # v3.1（审计 correctness）：并行同类子节点回填同 type+name 会静默互相
        # 覆盖（artifact id = type__slug(name)）。撞 id 且来源不同 run → 名字
        # 加子 run 短 id 后缀，两份都保留。
        name = rec["name"]
        from core.state import _slug

        candidate_id = f"{rec['type']}__{_slug(name)}"
        if candidate_id in existing_ids:
            prev = parent_state.read_artifact(candidate_id) or {}
            prev_src = (prev.get("metadata") or {}).get("source_run_id")
            if prev_src and prev_src != child_summary["run_id"]:
                name = f"{name}__{str(child_summary['run_id'])[-6:]}"

        # 2026-08-04：回填也是转发 —— 顶层 provenance 保留**子 run 那份的原始
        # 来源**，而不是把父节点记成产出方。上面两个 metadata 键此前是唯一能
        # 保住真来源的地方（所以 decision_package 得先读 metadata 再退顶层）；
        # 现在顶层 provenance 是权威，metadata 保留只为兼容老消费方。
        # 尤其重要：子 run 里那份 artifact 若本身是 imported（外部材料），
        # 回填后必须仍然是 imported，否则一次 run_node 就洗白了。
        from core import artifact_provenance as _prov

        saved = parent_state.save_artifact(
            artifact_type=rec["type"],
            name=name,
            content=rec.get("content", ""),
            metadata=meta,
            provenance=_prov.forwarded(
                rec.get("provenance")
                or _prov.produced(child_summary["node_type"], child_summary["run_id"]),
                via_node_type=parent_state.node_type,
                via_run_id=parent_state.run_id,
            ),
        )
        # 回填也是转发：子 run 那份是冻结的，父 run 的本地账本里也落一行 freeze
        # （save 行里的 frozen 键一律剥掉，冻结只出自 freeze 行）。
        if meta.get("frozen"):
            parent_state.mark_frozen(
                saved["id"],
                {k: meta[k] for k in ("freeze_reason",) if k in meta},
                frozen_at=str(meta.get("frozen_at") or "") or None,
            )
        existing_ids.add(saved["id"])
        imported.append({"id": saved["id"], "type": rec["type"], "name": name})
    return imported



def _compact_run_node_result(content: str) -> str:
    """旧 run_node 结果 → 决策要点摘要（全文都在盘上，带指针）。

    真实数据（E2E v20 orchestrator checkpoint）：中段 231k tokens 里 217k 是
    run_node 结果 —— 每条 ~7k，其中派发决策真正需要的只有：status、child_run_id、
    产出了什么、错误头。子 run 的完整 summary/transcript/产物都持久化在它自己的
    run 目录和节点 Git 目录里，清掉不丢信息。
    """
    import json as _json

    try:
        d = _json.loads(content)
    except (TypeError, ValueError):
        head = (content or "")[:400]
        return f"[压缩的 run_node 结果 —— 非 JSON，前 400 字符] {head}"
    keep: dict = {}
    for k in ("status", "child_run_id", "child_node_type", "verdict",
              "completion_kind", "missing_required_outputs",
              # #1097 第 4 条：压缩之后也要留住它。白名单漏掉一个新对象，
              # 父侧就退回"只看 status"，而那正是这条 issue 的形状。
              "child_obligation_effect", "node_task_outcome"):
        if d.get(k) not in (None, "", []):
            keep[k] = d[k]
    arts = d.get("produced_artifacts") or d.get("artifacts") or []
    if isinstance(arts, list) and arts:
        ids = [a.get("id") if isinstance(a, dict) else str(a) for a in arts]
        keep["produced_artifacts"] = ids[:20]
    if d.get("error"):
        keep["error_head"] = str(d["error"])[:300]
    rid = keep.get("child_run_id") or "?"
    return (
        "[压缩的 run_node 结果 —— 要点如下；全文在子 run "
        f"{rid} 的 summary/transcript，产物用 read_artifact 按 id 取]\n"
        + _json.dumps(keep, ensure_ascii=False)
    )


register_tool(
    ToolDefinition(
        name="run_node",
        result_compactor=_compact_run_node_result,
        description=(
            "把任意子节点当作 subagent 调起来。"
            "子节点跑自己独立的 agent loop（独立 messages / max_turns），"
            "共享同一个 project_id（memory + KB 跨节点持久化）。\n\n"
            "**节点分类不要背名单** —— producing 还是 service 以各节点 harness 的 "
            "`post_run_flow` 声明为准，会随「🔌 可调子节点契约」注入给你："
            "producing（hypothesis / experiment / writing）跑完自动登记收尾三步；"
            "service（literature / data / postprocess 等 `post_run_flow: none`）"
            "跑完直接看结果，不走审查流。\n\n"
            "**Artifact 转发**：绑定 Project worktree 的 run（平台常态）**不搬运 "
            "artifact**（子节点直接读共享 worktree 里各节点目录），但 "
            "`forward_artifact_ids` 是**输入选择**，会机械送达子节点审计：同类型"
            "有多份产物时必须用它指名用哪份，否则子节点（如 writing）会 blocked "
            "并要求重派指名；不传时每个必需输入类型恰好一个候选的由框架代选。"
            "未绑 Project 的 CLI / fixture run 只在你显式传 `forward_artifact_ids=[...]` "
            "时真正搬运文件（完全按你给的），不传则不带任何上游文件。\n"
            "子节点 required_output 类型的 artifact 会自动回填到本节点 artifacts/，"
            "其它产物保留在子 run 目录（用 read_external_artifact 拉）。\n"
            "⚠️ 父 harness.callable_nodes 决定能调哪些子节点；递归深度 ≤ 4。\n\n"
            "**v0.5.1 hard gate**：上一个 producing 节点的收尾三步（reviewer → "
            "curator → decision）没走完就再起 producing 节点，会被直接拒绝 —— "
            "防跳过 user 决策门陷入无限 revise 循环。_reviewer / _curator 不受此"
            "约束（它们正是用来推进 pending entry 的）。\n\n"
            "**background=true（长任务后台跑）**：预计跑得久（实验/大调研，几十分钟"
            "以上）且用户还想继续对话时用。立即返回 started_background；child 完成/"
            "暂停/失败会自动作为系统消息回报给你和用户。起了后台节点就**正常继续"
            "对话**，不要轮询、不要重复起同一节点。需要严格顺序衔接下一步的流程"
            "（如 post-producing 3-step）用默认前台模式。"
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "node_type": {
                    "type": "string",
                    "description": (
                        "要调起的节点名（如 'hypothesis'、'experiment'、'observation'、"
                        "'_reviewer'）。\n"
                        "证据生产有两种模态，按**还欠哪类闭合条目**选：\n"
                        "  `experiment`  干预式 —— 让世界（或它的模型）产生新数据。"
                        "兑现**数值条**。\n"
                        "  `observation` 检视式 —— 系统性检视既有记录（文献/档案/既有"
                        "数据集/观测）。兑现**陈述条**。\n"
                        "选反了框架不拦，但会把这次偏离连同你在 node_inputs 里写的 "
                        "`modality_rationale`（没写就记 not_declared）写进永久记录、"
                        "交 referee 终审 —— 陈述条确实可能需要跑一次模拟才能兑现，"
                        "所以请把理由写上。\n"
                        "另：本项目的 data 阶段已经跑过、却一份 `dataset` 都没交出来"
                        "时，起 `experiment` 同样会被记一笔 stage_debt，连同你的 "
                        "`dataset_waiver_reason`（说明这一趟为什么不需要 data 的交付物；"
                        "没写就记 not_declared）。正式科学实验的正解是先把 data 修通，"
                        "别让 experiment 自己造输入。"
                    ),
                },
                "deliverable": {
                    "type": "object",
                    "properties": {
                        "artifact_type": {"type": "string", "minLength": 1},
                        "path": {"type": "string", "minLength": 1},
                    },
                    "required": ["artifact_type", "path"],
                    "description": (
                        "用户点名要的交付文件（2026-08-31）。"
                        '{"artifact_type": <该节点会产的类型，如 survey_report>, '
                        '"path": <用户点名的文件，如 LITERATURE_REVIEW.md>}。'
                        "节点照常 save_artifact，**框架**在落账的同时把 content "
                        "投影到这个路径（项目根的无主之地或节点自己的目录）——"
                        "不需要你事后搬运。用户说了要一份具体文件时**总是带上它**。"
                    ),
                },
                "user_note": {
                    "type": "string",
                    "minLength": 1,
                    "description": (
                        "**必填。一到两句中文，直接说给用户听**（不是给你自己的备注）："
                        "我现在要做什么、为什么是这一步、期望拿到什么。\n"
                        "这是用户在对话里唯一能看到的「你在干什么」—— 节点内部的每轮"
                        "独白和工具调用对他是噪音，这一句才是信号。\n"
                        "写法：说人话，别复述参数。\n"
                        "  ✅『先做文献调研：项目里还没有任何证据基础，我需要先摸清"
                        "英国饮食声誉的既有研究，拿到一份综述再谈研究设计。』\n"
                        "  ❌『调起 literature 节点』（复述了参数，用户看了等于没看）\n"
                        "  ❌『Starting literature node with mode=landscape』（这是日志不是对话）"
                    ),
                },
                "task_instance_uuid": {
                    "type": "string",
                    "description": (
                        "**这一趟在做哪件事**的不可变身份（`task(action='create')` 的返回值；"
                        "`Txx` 是别名，不是身份）。\n"
                        "派 experiment / observation / derivation **必须**带 —— 没有它，"
                        "子节点只能去扫「此刻项目里有几份预注册」来认领自己该做什么，"
                        "而续跑、接管、上游重派这三类场景下那个答案随时会变。"
                    ),
                },
                "task_contract_digest": {
                    "type": "string",
                    "description": (
                        "要按**哪一版**合同执行（`task(action='contract')` 的返回值）。"
                        "一个任务只有一版时可省略；有多版（改过主意）时必须指名 —— "
                        "这本账没有「取最新」，分叉时替你抽签正是它要消灭的东西。"
                    ),
                },
                "planned_stop_authorized": {
                    "type": "boolean",
                    "description": (
                        "这个任务**授权**子节点中途停下正在跑的作业吗（默认 false）。\n"
                        "用户说「让它跑一分钟就停下」「跑到收敛就可以停」这类话时填 true，"
                        "并在 planned_stop_note 里写清是哪句话授权的。\n"
                        "为什么必须由你来填：子节点自己判断不了 —— 它只能看到任务正文里"
                        "有没有某句话，而「正文里提到了时长」和「授权了中途停止」是两回事。"
                        "执行者不能自己给自己发授权。"
                    ),
                },
                "planned_stop_note": {
                    "type": "string",
                    "description": "授权的依据：用户原话里授权停止的那一句。",
                },
                "node_inputs": {
                    "type": "object",
                    "description": (
                        "传给子节点的 node_inputs（如 {research_question: '...'}）。\n"
                        "  ⚠️ **本轮受哪份预注册约束，不在这里说** —— 它是任务合同的一部分："
                        "`task(action='contract', prereg_artifact_id=… | no_prereg_reason=…)`，"
                        "派发时随 `task_contract_digest` 一起到达子节点。\n"
                        "  原来这里写着「项目里只有一份时可省略」——**那条已经删掉**："
                        "「恰好只有一份」是巧合不是授权（跨 session 继承来的旧 prereg、未冻结的"
                        "amendment、重复 QID 都会让子节点认错），而省略之后子节点只能去扫项目"
                        "现状替你认领。"
                    ),
                },
                "forward_artifact_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "可选。**省略时框架自动按子节点 "
                        "required_input_artifact_types 选最新 artifact 转发**。"
                        "只在你要显式选特定 artifact（同 type 多候选）时填，"
                        "如 ['pre_registration__h1']。先用 list_artifacts 看 id。"
                        "Project 模式下不搬文件，但会作为子节点的**输入选择**"
                        "机械送达（writing 等按它审计用哪份）。"
                    ),
                },
                "resume_run_id": {
                    "type": "string",
                    "description": (
                        "一般不用填。同类型节点上次被进程中断时，框架会**自动**"
                        "接上那条 run 的上下文续跑（不会从头再来）。"
                        "填 'fresh' = 明确放弃续跑、开一条全新的 run；"
                        "填具体 run_id = 指定续哪一条。"
                    ),
                },
                "background": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "可选。true = 子节点后台跑，本调用立即返回，完成/暂停/失败"
                        "自动回报。用于长任务（实验/大调研）+ 用户还想继续对话的场景。"
                    ),
                },
                "task_id": {
                    "type": "string",
                    "description": (
                        "可选。绑定的 TaskList task id（如 'T02'）。框架会随本 run 把它"
                        "置 in_progress，并在 child incomplete/error/cancelled 时自动 block "
                        "（写清原因 + child_run_id）—— 你不用记得手动 update，task ledger "
                        "不会再永久卡 in_progress。completed 的常规 review→decision 流程照旧。"
                    ),
                },
            },
            "required": ["node_type", "user_note"],
        },
        risk_level="high",  # 调起整个 agent loop —— 谨慎
    ),
    _run_node_tool,
)


# ── run_nodes_parallel ─────────────────────────────────────────────────────


async def _run_nodes_parallel(
    state: State, jobs: list[dict] | None = None, max_parallel: int | None = None, **_: Any
) -> dict:
    """并行起 N 个子节点。每个 job 是一个 run_node 入参 dict。

    使用 asyncio.gather + Semaphore 限制并发；失败不阻塞其它。
    返回 jobs 顺序对应的结果列表。
    """
    # jobs 非空 = schema minItems:1（派发口核）；不是数组就让它炸，注册表回头按
    # schema 报形状。job 缺 node_type 在下面 _one 里查：注册表的取值校验不核
    # **嵌套** required，这一处是它的共享检查。
    if not isinstance(jobs, list):
        raise TypeError(f"jobs 须是数组，收到 {type(jobs).__name__}")
    # v1.4: owner_config 可 per-node override（state.hook_state）
    effective_max_parallel = state.hook_state.get("_subagent_max_parallel_override", MAX_PARALLEL)
    cap = min(int(max_parallel or effective_max_parallel), effective_max_parallel)
    sem = asyncio.Semaphore(cap)

    async def _one(job: dict, idx: int) -> dict:
        async with sem:
            if not isinstance(job, dict) or "node_type" not in job:
                return {
                    "status": "error",
                    "error": f"job[{idx}] 缺 node_type 字段。",
                    "job_index": idx,
                }
            try:
                return {
                    "job_index": idx,
                    **(
                        await _run_node_tool(
                            state,
                            node_type=job["node_type"],
                            node_inputs=job.get("node_inputs") or {},
                            forward_artifact_ids=job.get("forward_artifact_ids") or [],
                            # 并行派发同样要先跟用户说一句 —— 漏传这一个参数，
                            # 整条并行路径就会被 run_node 的契约全数拒掉，而且
                            # 症状是"并行怎么都起不来"，指不回这里。
                            user_note=job.get("user_note"),
                            # 并行派发也要带任务身份（#1080 第 3 条）：漏传的话
                            # 同一批并行子 run 会各自去扫项目现状认领自己该做什么，
                            # 而那正是串行路径刚堵上的洞。serial 与 parallel 语义
                            # 必须一致（#1097 验收 1）。
                            task_instance_uuid=job.get("task_instance_uuid"),
                            task_contract_digest=job.get("task_contract_digest"),
                        )
                    ),
                }
            except Exception as e:
                return {
                    "status": "error",
                    "error": f"job[{idx}] 异常：{type(e).__name__}: {e}",
                    "job_index": idx,
                }

    state.append_transcript(
        "subagent_parallel_start",
        job_count=len(jobs),
        cap=cap,
        node_types=[j.get("node_type") if isinstance(j, dict) else None for j in jobs],
    )

    results = await asyncio.gather(
        *(_one(j, i) for i, j in enumerate(jobs)),
        return_exceptions=False,  # _one 已经把异常包成 dict
    )

    state.append_transcript(
        "subagent_parallel_end",
        statuses=[r.get("status") for r in results],
    )

    # v3.1（审计 高危#13）：并行子节点的 pause 以前被埋进 failures —— 子 run
    # 的 HITL 问题永远无人回答。现在单独拎出来：paused 子 run 的 pause_event
    # 完整暴露给父 LLM（可 runtime_control(action='inject') 作答/转发给 user），
    # 且不计 failure。
    paused_jobs = [
        {
            "job_index": r.get("job_index"),
            "paused_run_id": r.get("paused_run_id"),
            "pause_event": r.get("pause_event"),
        }
        for r in results
        if isinstance(r, dict) and r.get("status") == "pause"
    ]
    out = {
        "status": "success",
        "job_count": len(jobs),
        "results": results,
        "successes": sum(1 for r in results if r.get("status") == "success"),
        "paused": len(paused_jobs),
        "failures": sum(
            1 for r in results if r.get("status") not in ("success", "completed", "pause")
        ),
    }
    if paused_jobs:
        out["paused_jobs"] = paused_jobs
        out["note"] = (
            f"{len(paused_jobs)} 个并行子节点在等人工输入（见 paused_jobs[*]."
            f"pause_event.question）。处理：用 runtime_control(action='inject', "
            f"child_run_id=..., content=<答案>) 作答，或把问题转给 user"
            f"（request_human_input）再注入。不处理它们会一直挂起。"
        )
    return out


register_tool(
    ToolDefinition(
        name="run_nodes_parallel",
        description=(
            "并行调起 N 个子节点。每个 job 是一个 run_node 入参 dict（node_type / node_inputs / "
            "forward_artifact_ids）。返回各 job 顺序对应的结果列表。一个失败不阻塞其它。"
            f"并行上限 {MAX_PARALLEL}（HARNESS_FRAMEWORK_MAX_PARALLEL 控制）。"
            "适合：对一组对象做同类调研 / 同类实验。"
            "⚠️ 同样受 callable_nodes 白名单 + 递归深度上限约束。"
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "jobs": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "properties": {
                            "node_type": {"type": "string", "minLength": 1},
                            "node_inputs": {"type": "object"},
                            "forward_artifact_ids": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "user_note": {
                                "type": "string",
                                "minLength": 1,
                                "description": (
                                    "**必填**。一到两句说给用户听：这一路在做什么、为什么。"
                                    "并行时用户更需要知道你同时铺开了几件事、各是什么。"
                                ),
                            },
                            "task_instance_uuid": {
                                "type": "string",
                                "description": (
                                    "这个 job 在做哪件事（与串行 run_node 同义）。"
                                    "派 experiment / observation / derivation 必须带。"),
                            },
                            "task_contract_digest": {
                                "type": "string",
                                "description": "按哪一版合同执行（同串行）。",
                            },
                        },
                        "required": ["node_type", "user_note"],
                    },
                    "description": "要并行起的子节点 job 列表。",
                },
                "max_parallel": {
                    "type": "integer",
                    "description": (
                        f"本次调用的并行上限（≤ {MAX_PARALLEL} 全局上限）。省略 = 用全局上限。"
                    ),
                },
            },
            "required": ["jobs"],
        },
        risk_level="high",
    ),
    _run_nodes_parallel,
)


# ── read_external_artifact ─────────────────────────────────────────────────


async def _read_external_artifact(state: State, run_id: str, artifact_id: str, **_: Any) -> dict:
    """读另一次 run 产出的 artifact（一般是子 run 的非 required_output 产物）。

    ## 边界就是一句话：读到的文件必须在**我自己的 runs 目录**里（issue #720）

    原来这里 sibling 未命中就回退到 `core.paths.find_run_dir`，而那个函数**按
    设计**扫 `runs_anon → projects/<any>/runs → 旧 flat → STATE_DIR`。于是拿到
    任意 run_id 就能读任意项目的产物 —— 所谓跨项目隔离只是"run_id 不在上下文
    里"的提示词级纪律。那条回退在本工具上从来没有合法用途：子 run 由
    `run_node`（本文件 `base_dir = state.root.parent`）创建，**永远是同级目录**，
    工具描述里说的 run_id 也只来自 `child_run_id`。所以删掉它，而不是给它加闸。

    删掉之后，穿越与越权收敛成同一个不变量，两次 containment 判完：

      · run 目录必须是 `state.root.parent` 的**直接子目录**
        —— `..`、`a/b`、绝对路径、指向别处的 symlink 一并出局；
      · artifact 文件必须在那个 run 的 `artifacts/` 内
        —— `../../secrets`、`../transcript` 一并出局。

    不做 id 正则：字符白名单是"长得像不像"，而这里要的是"落不落在界内"——
    后者才是真判据，且不用维护第二份形态说明。
    """
    from core.ledger import RecordStore

    runs_root = state.root.parent.resolve()
    run_dir = (state.root.parent / run_id).resolve()
    if run_dir.parent != runs_root or not run_dir.is_dir():
        return {
            "status": "error",
            "error": (
                f"读不到 run_id={run_id!r} 的 {artifact_id!r}。本工具只读**本项目**"
                f"其它 run 的产物：run_id 用 run_node 返回的 child_run_id，"
                f"artifact_id 见 child summary 的 all_child_artifacts。"
            ),
        }
    store = RecordStore(run_dir / "artifacts", run_dir / "records.jsonl")
    head = store.head(artifact_id)
    if head is None:
        return {"status": "error", "error": f"找不到 artifact：{run_id}/{artifact_id}"}
    # 账本说文件在这儿，文件本身还得真在这个 run 目录里：指向别处的 symlink 出局。
    try:
        resolved = store.abs_path(head).resolve(strict=True)
    except OSError:
        return {"status": "error", "error": f"找不到 artifact：{run_id}/{artifact_id}"}
    if run_dir not in resolved.parents:
        return {
            "status": "error",
            "error": (f"artifact {run_id}/{artifact_id} 的文件不在该 run 的目录内"
                      f"（指向 {resolved}），拒绝读取。"),
        }
    rec = store.record(artifact_id)
    if rec is None:
        return {"status": "error", "error": f"找不到 artifact：{run_id}/{artifact_id}"}
    return {"status": "success", "artifact": rec, "path": str(resolved)}


register_tool(
    ToolDefinition(
        name="read_external_artifact",
        # 纯读：结果可用同样参数重调取回。重复调用紧凑化与压缩器都扫这个
        # 声明（core/tool_call_cache.cacheable_tools），不再各写一份名单。
        replayable_read=True,
        description=(
            "读取**本项目**另一次 run 产出的 artifact（一般是子 run 的非 required_output "
            "中间产物）。run_id 来自 run_node 返回的 child_run_id。artifact_id 在 child "
            "summary 的 all_child_artifacts 列表里。别的项目读不到 —— 那不是参数写错了。"
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "run_id": {"type": "string", "description": "目标 run 的 id。"},
                "artifact_id": {"type": "string", "description": "目标 artifact 的 id。"},
            },
            "required": ["run_id", "artifact_id"],
        },
        risk_level="low",
    ),
    _read_external_artifact,
)


# ── query_project_status ───────────────────────────────────────────────────


async def _query_project_status(state: State, **_: Any) -> dict:
    """给 orchestrator 看的项目级总览。

    本次 state（orchestrator 自己的 run）的 artifacts + 项目级 KB / memory 统计 +
    可见的子 run 摘要（sibling output/<run>/summary.json）。
    """

    out: dict[str, Any] = {
        "status": "success",
        "project_id": state.project_id,
        "current_run_id": state.run_id,
        "current_node_type": state.node_type,
        "tokens_used": state.tokens_used,
        "tokens_limit": state.tokens_limit,
        "tool_calls_made": state.tool_calls_made,
    }

    # 自身 artifact
    out["artifacts"] = state.list_artifacts()

    # 项目级 memory / KB 统计
    if state.project_root and state.project_root.exists():
        # v2 存储：未消化的候选数 = "这个项目还欠多少记忆整理"。
        # 此前数的是 v1 的 memory.jsonl —— 那个文件自 2026-05 起没有写入方，
        # 所以这个计数对任何新项目都恒为 0。
        _cand = state.project_root / "memory" / "candidates.jsonl"
        out["memory_count"] = (
            sum(1 for _ in _cand.open(encoding="utf-8")) if _cand.exists() else 0
        )
        kb_stats: dict[str, int] = {}
        for entity in ("concepts", "claims", "experiments", "chunks"):
            p = state.project_root / f"kb_{entity}.jsonl"
            if p.exists():
                kb_stats[entity] = sum(1 for _ in p.open(encoding="utf-8"))
        out["kb_stats"] = kb_stats
    else:
        # 没有 project_root（ad-hoc run）—— 没有项目级记忆可数。
        out["memory_count"] = 0
        out["kb_stats"] = {}

    # 兄弟 run 摘要 —— 口径同样走 core.run_history（新→旧，取最近 20 次）
    out["recent_runs"] = [
        {
            "run_id": r.run_id,
            "node_type": r.node_type,
            "status": r.status,
            "turns": r.turns,
            "depth": (r.raw or {}).get("depth", 0),
            "artifact_types": sorted(x for x in r.artifact_types() if x),
        }
        for r in run_history.load_runs(
            state.root.parent, project_id=state.project_id, exclude_run_id=state.run_id, limit=20
        )
    ]

    return out


register_tool(
    ToolDefinition(
        name="query_project_status",
        # 纯读：结果可用同样参数重调取回。重复调用紧凑化与压缩器都扫这个
        # 声明（core/tool_call_cache.cacheable_tools），不再各写一份名单。
        replayable_read=True,
        description=(
            "查询本项目的总体状态：当前 artifact / 项目级 KB+memory 统计 / 最近的子 run 列表。"
            "orchestrator 处理'状态查询'意图时优先用这个工具一次拿全。"
            "返回的 recent_runs 来自同一 project_id 的兄弟 run 的 summary.json。"
        ),
        parameters_schema={
            "type": "object",
            "properties": {},
        },
        risk_level="low",
    ),
    _query_project_status,
)


# ── forward_artifact 工具已删（判决拆除第三波，run_node:3593 X）────────────
# 它把 artifact 登记进 hook_state.forwarded_artifacts，而那张登记表全仓零生产
# 读者；真机制一直是 run_node(forward_artifact_ids=[...])。


# ═══════════════════════════════════════════════════════════════════════════
# v3.6 节点申诉权：request_upstream_rework
# ═══════════════════════════════════════════════════════════════════════════
#
# 三轮 E2E 的同一根因：**卡住的节点自己没有申诉渠道**。writing 发现"我的引用
# 不存在，是因为 literature 没入库那些论文"——它说不出这句话，只能 QC 失败被
# 重跑（E2E#3 连重 5 次，每次 45-50 轮）；curator 发现"7/16 chunk 没接 author，
# 这得 literature 补"——只能反复查 KB（E2E#2 单个 run 查了 51,937 次）。
#
# 唯一能说"根因在上游"的是 reviewer，而那是 LLM 自由裁量：E2E#2 全程 0 次
# redirect、110 次 proceed。所以申诉权必须交到**当事节点**手上 —— 它最清楚
# 自己缺什么。
#
# 本工具不直接调度上游（producing 节点无权自己起 run，那会绕过 review flow），
# 而是**登记一份结构化诉求**：写进 run summary 与 transcript，orchestrator 在
# 本 run 结束后据此路由。这样保持"调度权归调度器"，同时让诊断结论不再丢失。


async def _request_upstream_rework(
    state: State,
    upstream_node: str,
    missing: str,
    acceptance: str,
    blocking: bool = True,
    **_: Any,
) -> dict:
    """声明"我被上游卡住了"：缺什么、需要谁补、补到什么程度算合格。

    典型场景（都来自真实 E2E）：
      - writing：被引论文没进 KB → upstream_node='literature'
      - curator：survey chunk 缺 author 接线 → upstream_node='literature'
      - postprocess：clean_results 缺单位/区间语义/样本量或坐标元数据 → upstream_node='experiment'
    """
    from core.upstream_routing import PIPELINE_ORDER, upstream_candidates

    up = (upstream_node or "").strip()
    if up == state.node_type:
        return {
            "status": "error",
            "error": "upstream_node 不能是自己 —— 自己能修的就直接修，"
            "这个工具是用来申诉**上游产物不足**的。",
        }
    if up not in PIPELINE_ORDER and not up.startswith("_"):
        # 实测被填进来的第一个非法值是 'orchestrator' —— 节点想说"这事得调度器
        # 决定"，但申诉工具只收 producing 上游。光报"不是已知节点"等于把它挡在
        # 门外却不说门在哪：调度器 / 人的通道是 report_blocker。
        _hint = ""
        if up in {"orchestrator", "_orchestrator", "user", "human"}:
            _hint = (
                "\n想把问题交给调度器或人（不是让某个上游节点补产物）用 "
                "report_blocker(...)，不是这个工具 —— 本工具只受理"
                "「上游 producing 节点的产物不足」。"
            )
        return {
            "status": "error",
            "error": (
                f"upstream_node={up!r} 不是已知的上游 producing 节点。"
                f"合法取值：{upstream_candidates(state.node_type)}{_hint}"
            ),
            "valid_upstream_nodes": upstream_candidates(state.node_type),
        }
    # upstream_node / missing / acceptance 非空 = schema minLength:1，派发口核；
    # 这里不再手写第二份（判决拆除刀 1）。
    req = {
        "requested_by_node": state.node_type,
        "requested_by_run_id": state.run_id,
        "upstream_node": up,
        "missing": missing.strip(),
        "acceptance": acceptance.strip(),
        "blocking": bool(blocking),
        "at": datetime.now(UTC).isoformat(),
    }
    reqs = state.hook_state.setdefault("upstream_rework_requests", [])
    reqs.append(req)
    state.append_transcript("upstream_rework_requested", **req)
    return {
        "status": "success",
        "recorded": req,
        "note": (
            f"已登记上游返工诉求（{state.node_type} → {up}）。本 run 结束后由 "
            f"orchestrator 调度 {up} 补齐，再重跑本节点。"
            + (
                "你现在应当停止在本节点继续硬做：产出已完成的部分 + 明确标注该缺口，"
                "然后结束本 run。"
                if blocking
                else "非阻塞诉求：可继续完成力所能及的部分。"
            )
        ),
    }


register_tool(
    ToolDefinition(
        name="request_upstream_rework",
        description=(
            "**被上游卡住时用这个申诉，不要反复重试或硬做。**\n\n"
            "当你发现产出不达标的根因**不在你自己**、而在上游产物不足时"
            "（如：要引用的论文没进 KB、experiment_log 样本量不够、"
            "前置 artifact 缺字段），用它登记一份结构化诉求：\n"
            "  upstream_node —— 谁该补；missing —— 具体缺什么；\n"
            "  acceptance —— 补到什么程度算合格（要可机械核对）。\n\n"
            "登记后由 orchestrator 调度上游补齐再重跑你。**这比反复重写有效得多**："
            "实测同一节点连续失败 5 次、每次 45-50 轮，根因始终在上游。\n"
            "注意：只在根因确实在上游时用；自己能修的直接修。"
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "upstream_node": {"type": "string", "minLength": 1,
                                  "description": "该补齐的上游节点 type"},
                "missing": {
                    "type": "string", "minLength": 1,
                    "description": "非空，说清具体缺什么（哪些 artifact / 字段 / 证据量）——"
                                   "不要写'资料不足'这种没法验收的描述。",
                },
                "acceptance": {
                    "type": "string", "minLength": 1,
                    "description": "非空，说清补到什么程度算合格（可机械核对的标准），"
                                   "否则上游补完仍不知道够不够。",
                },
                "blocking": {
                    "type": "boolean",
                    "description": "true=没有它本节点无法完成（默认 true）",
                },
            },
            "required": ["upstream_node", "missing", "acceptance"],
        },
    ),
    _request_upstream_rework,
)


# ═══════════════════════════════════════════════════════════════════════════
# v3.7.2 修订基线：节点必须能读到**自己上一次**的产物
#
# E2E-3 现场（决定性证据）：writing 第 1 次跑 31 轮，产出完整 manuscript、62 项
# preflight 全过，只挂 1 条 check。orchestrator 给出的重试指令是精准的：
#   "Previous run failed ONLY because of \cite{prereg} placeholder citation."
# 但第 2 次跑仍然**从零重做整条流水线**（重跑 audit → 重建 preflight plan → 重新
# prepare project → 重写全文），50 轮撞上限、烧 4.3M token，反而挂了 5 条 check。
#
# 根因不是反馈质量，是**能力缺失**：上一次的 manuscript 根本没交回给它。失败 run
# 的 deliverable 被 v3.1 隔离规则挡在父 state 外（正确 —— 半成品不该成为
# auto-forward 候选、绕过 review/curator/decision 三道门），而 writing 拿不到
# read_external_artifact（持有面是 _reviewer / _curator / experiment 三家 ——
# 2026-09-01 更正：原文写"不在任何 producing 节点的白名单里"，与
# experiment/harness.yaml:430 矛盾）。于是"改一个 bib 条目"这种 2 轮的活，只能
# 走成一次完整重做。
#
# 修法刻意收窄：不给 producing 节点通用的 read_external_artifact（那会开新洞 ——
# writing 可以去读 experiment 失败的产物当上游输入，绕过 auto-forward 门禁），
# 而是给一个**按构造就封死跨节点、跨项目**的窄工具：只能读本 node_type 在本
# project 里自己以前那些 run 的产物。隔离不变量不动：产物仍不进父 state、仍不能
# auto-forward、本轮产出仍要重新过全套门禁。


async def _read_own_prior_attempt(
    state: State, artifact_id: str, run_id: str | None = None, **_: Any
) -> dict:
    """读**自己**上一次（或指定 run_id）产出的 artifact，用于增量修订。"""
    self_node = state.node_type or ""
    prior = _prior_runs_of(state, self_node)
    if not prior:
        return {"status": "error", "error": "本项目里本节点没有更早的 run —— 没有可修订的基线。"}
    if run_id:
        match = [r for r in prior if r.run_id == run_id]
        if not match:
            # load_runs 已按 node_type + project 过滤 —— 别人的 run 根本不在
            # prior 里，跨节点/跨项目按构造读不到，不靠这里再判一次。
            return {
                "status": "error",
                "error": (
                    f"run_id={run_id!r} 不是本节点（{self_node}）在本项目里的历史 run。"
                    "本工具**只能**读你自己以前的产物；别人的产物走正常的上游转发。"
                ),
            }
        target = match[0]
    else:
        # 和 _revision_baseline_note 走**同一个入口** —— note 广告哪一次，
        # 默认就读哪一次（PR#200 的教训）。
        target = _baseline_run(state, self_node, prior) or prior[0]

    # ── 产物落点：先问漏斗，再退回 run-local 缓存 ─────────────────────────
    #
    # 这行原来只看 `target.state_dir / "artifacts"` —— 那是 v2.1 **之前**的
    # 落点。PR #346 把产物锚点搬进了节点的 Git 目录（`<worktree>/<node>/
    # artifacts/`，run-local 目录在 `.research/cache/` 下、是 gitignored 的），
    # 这个工具没跟着改。
    #
    # 后果实测（v22，那轮**跑出了论文**）：本工具 **48 次调用、48 次失败**，
    # 失败率 100%。而报错还附着 `available_artifact_ids`（那份列表来自 run
    # 记录，逻辑上确实存在）—— agent 看到"没有 X"同时又看到 X 在清单里，
    # 只能一遍遍重试。这就是"agent 的韧性掩盖框架缺陷"的标本。
    #
    # 更深一层：v2.1 之后**同一节点的所有 run 写同一个目录**，"上一次 run 的
    # 产物"和"当前产物"是同一个文件路径（历史版本在 Git 里）。所以正确做法是
    # 走 state 的查找入口拿到当前落点；只有历史遗留的 run 才需要回退到缓存。
    rec = state.read_artifact(artifact_id)
    if rec is None:
        from core.ledger import RecordStore

        _legacy_store = RecordStore(target.state_dir / "artifacts", target.state_dir / "records.jsonl")
        rec = _legacy_store.record(artifact_id)
    if rec is None:
        have = [a.get("id") for a in target.artifacts if isinstance(a, dict)]
        return {
            "status": "error",
            "error": (
                f"找不到 artifact {artifact_id!r}（在本节点的产物目录与 run "
                f"{target.run_id} 的缓存里都找过）。"
                + (f" 本节点已有：{', '.join(str(x) for x in have[:8])}" if have else "")
            ),
            "available_artifact_ids": have,
        }
    return {
        "status": "success",
        "from_run_id": target.run_id,
        "from_run_status": target.status,
        "artifact": rec,
        "note": (
            "这是你自己上一次的产物。它**没有**通过门禁，不能直接当成品交差；"
            "但你应当在它基础上**改**，而不是从零重做。"
        ),
    }


register_tool(
    ToolDefinition(
        name="read_own_prior_attempt",
        replayable_read=True,
        description=(
            "读**你自己**上一次 run 产出的 artifact，作为本次修订的基线。"
            "被重新调起（revise/retry）时**先调它**：在上一版基础上改，"
            "比从零重做又快又稳 —— 重做会丢掉上一版已经做对的部分。"
            "⚠️ 只能读你自己（同 node_type + 同 project）的历史产物；"
            "别人节点的产物走正常的上游 artifact 转发。"
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "artifact_id": {
                    "type": "string",
                    "description": "要读的 artifact id（见节点输入里的修订基线清单）。",
                },
                "run_id": {
                    "type": "string",
                    "description": "可选。默认读最近一次；指定则读那一次。",
                },
            },
            "required": ["artifact_id"],
        },
        risk_level="low",
    ),
    _read_own_prior_attempt,
)

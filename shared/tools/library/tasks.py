"""v2.0：`task` dispatcher 工具 —— first-class TaskList API。

跟 KB 的 `curator_scan(scan_type=...)` / memory 的 `update_memory(action=...)`
同样 pattern：单工具 + action enum。

跨节点持久（项目级）。task 清单是节点的私账：它不拦执行——真正的执行闸在
run_node 派发前查 `pending_post_node_flow`；这里只如实记账。

详见 `core/tasks.py`。
"""
from __future__ import annotations

from typing import Any

from core.state import State
from core.tasks import TaskList, TaskListError
from core.tool_registry import ToolDefinition, register_tool

_TASK_ACTIONS = ("create", "contract", "start", "complete", "block", "unblock", "list", "get")
_TASK_FILTERS = ("pending", "in_progress", "completed", "blocked", "all")


def _get_task_list(state: State) -> TaskList | None:
    """从 state 找 tasks dir；无 project_root 返 None（不能用 task 系统）。"""
    if not state.project_root:
        return None
    tasks_dir = state.project_root / "tasks"
    return TaskList(tasks_dir)


async def _task(
    state: State,
    action: str,
    task_id: str | None = None,
    title: str = "",
    description: str = "",
    parent_id: str | None = None,
    blocked_reason: str = "",
    notes: str = "",
    filter: str | None = None,
    objective: str = "",
    intended_use: str = "",
    target_node: str = "",
    prereg_artifact_id: str = "",
    prereg_version: str = "",
    prereg_content_hash: str = "",
    no_prereg_reason: str = "",
    parent_revision_digest: str = "",
    reason: str = "",
    **_: Any,
) -> dict:
    """任务管理统一入口。`action` 决定操作。

    Args:
      action ∈ {create | start | complete | block | unblock | list | get}
      task_id: 操作目标（除 create / list 外都需要）
      title, description: create 用
      parent_id: create 时可选，挂在哪个父 task 下
      blocked_reason: block 时必填
      notes: complete 时可选备注
      filter: list 时过滤 ∈ {pending | in_progress | completed | blocked | all}

    action / filter 枚举由 parameters_schema 声明、派发口核一次。条件必填
    （create / list 之外都要 task_id）schema 表达不了，只在这里查一次；空 title
    与空 blocked_reason 交 core/tasks（前者如实记「(未命名)」，后者 TaskListError
    经下方转述）。
    """
    tl = _get_task_list(state)
    if tl is None:
        return {
            "status": "error",
            "error": "task 系统需要 project_id（项目级持久）。"
                       "起 run 时传 --project-id 或 chat.py --project 即可。",
        }
    if action not in ("create", "list") and not task_id:
        return {"status": "error", "error": f"{action} 需要 task_id"}

    try:
        if action == "create":
            t = tl.create(
                title=title, description=description,
                owner_node=state.node_type, run_id=state.run_id,
                parent_id=parent_id,
                session_id=str(getattr(state, "session_id", "") or ""),
            )
            return {
                "status": "success", "task": t.to_dict(),
                "task_instance_uuid": t.task_instance_uuid,
                "hint": (
                    "新 task 创建为 pending。**Txx 只是别名，派发要用 "
                    "task_instance_uuid**。派 experiment / observation / derivation "
                    "之前先 action='contract' 写下这一趟要做什么、绑哪份预注册。"),
            }

        if action == "contract":
            # ── 写一条合同 revision（#1080 第 2 条 / #1097 第 1 条）──────────
            #
            # 它和 status 是**两本账**：status 说"做到哪了"（可改写），合同说
            # "被安排去做什么"（不可改写）。这里只写合同，一个字都不碰 status。
            from core.task_contract import (
                PreregAssignment, TaskContractError, TaskContractLog,
            )

            t = tl.get(task_id) if task_id else None
            if t is None:
                return {"status": "error",
                        "error": f"找不到任务 {task_id!r}（可以传 Txx 或 task_instance_uuid）"}
            uuid = t.task_instance_uuid
            if not uuid:
                return {
                    "status": "error",
                    "error": (
                        f"任务 {t.id} 是旧记录，没有 task_instance_uuid —— 合同必须挂在"
                        "一个不可变身份上。请新建一个任务再写合同。"),
                }
            if prereg_artifact_id and no_prereg_reason:
                return {
                    "status": "error",
                    "error": ("prereg_artifact_id 与 no_prereg_reason 互斥：要么绑一份"
                              "确切的预注册，要么明说这一趟不绑并给理由。两个都填等于"
                              "没做决定。"),
                }
            assignment = None
            try:
                if prereg_artifact_id:
                    assignment = PreregAssignment.exact(
                        prereg_artifact_id, version=prereg_version,
                        content_hash=prereg_content_hash)
                elif no_prereg_reason:
                    assignment = PreregAssignment.none(no_prereg_reason)
                rev = TaskContractLog(state.project_root / "tasks").append(
                    task_instance_uuid=uuid,
                    objective=objective or t.title,
                    intended_use=intended_use,
                    target_node=target_node or t.owner_node,
                    prereg_assignment=assignment,
                    actor=f"{state.node_type}:{state.run_id}",
                    reason=reason,
                    parent_revision_digest=parent_revision_digest,
                )
            except TaskContractError as exc:
                return {"status": "error", "error": str(exc)}
            return {
                "status": "success",
                "task_instance_uuid": uuid,
                "task_contract_revision": rev.revision,
                "task_contract_digest": rev.digest,
                "prereg_assignment": rev.assignment_kind,
                "hint": (
                    "派发时带上 task_instance_uuid + task_contract_digest。"
                    + ("⚠️ 这份合同**没有**安排预注册绑定（pending_assignment）："
                       "要么 prereg_artifact_id 绑一份，要么 no_prereg_reason 明说不绑。"
                       if rev.assignment_kind == "pending_assignment" else "")),
            }

        if action == "start":
            t = tl.start(task_id, owner_node=state.node_type)
            return {"status": "success", "task": t.to_dict()}

        if action == "complete":
            # task 清单是私账，标 complete 不改变任何执行事实；人工决定
            # （retry/revise/redirect）真正的执行闸在 run_node 派发前查
            # pending_post_node_flow，不在这里立第二份判决。
            t = tl.complete(task_id, notes=notes or None)
            return {"status": "success", "task": t.to_dict()}

        if action == "block":
            t = tl.block(task_id, reason=blocked_reason)
            return {"status": "success", "task": t.to_dict()}

        if action == "unblock":
            t = tl.unblock(task_id)
            return {"status": "success", "task": t.to_dict()}

        if action == "list":
            f = filter or "all"
            tasks = tl.list_all() if f == "all" else tl.filter(status=f)
            return {
                "status": "success",
                "count": len(tasks),
                "tasks": [t.to_dict() for t in tasks],
            }

        # action == "get"
        t = tl.get(task_id)
        if t is None:
            return {"status": "error", "error": f"task_id={task_id!r} 不存在"}
        return {"status": "success", "task": t.to_dict()}

    except TaskListError as e:
        return {"status": "error", "error": str(e)}
    except Exception as e:
        return {"status": "error",
                "error": f"{type(e).__name__}: {str(e)[:200]}"}


register_tool(
    ToolDefinition(
        name="task",
        description=(
            "**长任务清单管理（first-class TaskList）**。`action` 决定操作：\n\n"
            "  - `create`：新建 task（status=pending）。title 空则记「(未命名)」，可选 description / parent_id（挂在哪个父 task 下）。"
            "返回里的 **task_instance_uuid 才是身份**，Txx 只是给人读的别名\n"
            "  - `contract`：给这个 task 写一条**不可改写**的合同 revision —— 这一趟要做什么"
            "（objective / intended_use / target_node），以及**绑哪份预注册**：\n"
            "      · `prereg_artifact_id`(+`prereg_version`/`prereg_content_hash`) = 确切绑这一份；\n"
            "      · `no_prereg_reason` = 明说这一趟不绑（这是一个决定，所以要理由）；\n"
            "      · 两个都不给 = **pending_assignment（还没安排）**，不等于「明确不绑」。\n"
            "    改主意就带 `parent_revision_digest` 再写一条，旧的留着 —— 这本账只追加。\n"
            "    派 experiment / observation / derivation **必须**先有合同，派发时带 "
            "`task_instance_uuid` + `task_contract_digest`\n"
            "  - `start`：开始做某 task（同一 owner 可并行多个 in_progress）。\n"
            "  - `complete`：标记完成。可选 notes 写完成笔记\n"
            "  - `block`：标 blocked。必填 blocked_reason（非空，说清为什么被卡）\n"
            "  - `unblock`：blocked → pending（恢复可 start）\n"
            "  - `list`：列 task（可 filter ∈ pending/in_progress/completed/blocked/all）\n"
            "  - `get`：按 task_id 看单个详情\n\n"
            "**何时用 task vs scratchpad vs memory**：\n"
            "  - **task**: 结构化 plan，跨 run 持久，每轮 LLM 看到，状态机驱动\n"
            "  - **scratchpad**: 单 run 内自由速记下一步思路（write_scratchpad）\n"
            "  - **memory**: 跨 run soft recall（pitfalls / workflows / preferences）"
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": list(_TASK_ACTIONS),
                },
                "task_id": {"type": "string",
                             "description": "create / list 外都需要"},
                "title": {"type": "string", "description": "create 用；空则记「(未命名)」"},
                "description": {"type": "string",
                                  "description": "create 可选"},
                "parent_id": {"type": "string",
                                "description": "create 可选，挂在哪个父 task 下"},
                "blocked_reason": {"type": "string",
                                    "description": "block 必填：非空，说清为什么被卡"},
                "notes": {"type": "string",
                            "description": "complete 可选"},
                "filter": {"type": "string",
                            "enum": list(_TASK_FILTERS),
                            "description": "list 可选"},
                "objective": {"type": "string",
                                "description": "contract：这一趟要达成什么（不填则用 task 标题）"},
                "intended_use": {"type": "string",
                                   "description": "contract：产出打算怎么用（如 confirmatory / 构建 / 诊断）"},
                "target_node": {"type": "string",
                                  "description": "contract：安排给哪个节点"},
                "prereg_artifact_id": {"type": "string",
                                         "description": "contract：确切绑这一份预注册。与 no_prereg_reason 互斥"},
                "prereg_version": {"type": "string", "description": "contract 可选"},
                "prereg_content_hash": {"type": "string", "description": "contract 可选"},
                "no_prereg_reason": {"type": "string",
                                       "description": "contract：明说这一趟不绑预注册，并给理由。与 prereg_artifact_id 互斥"},
                "parent_revision_digest": {"type": "string",
                                             "description": "contract：改主意时指明接在哪条之后"},
                "reason": {"type": "string", "description": "contract：为什么写这一版"},
            },
            "required": ["action"],
        },
        risk_level="low",
    ),
    _task,
)

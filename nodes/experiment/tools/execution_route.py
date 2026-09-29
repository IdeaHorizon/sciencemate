"""Experiment 的规范执行路线。

这里不建立通用工作流引擎，也不保存第二份 ``current_step``。本模块只负责：

1. 校验 ``declared_route`` v2 的静态步骤 DAG；
2. 把旧构建契约归一化成只读内存视图；
3. 使用 Core 已有的同身份版本、修订和冻结原语，声明唯一 canonical route；
4. 确定性读取本节点、本 run 所拥有的冻结 route triple。

步骤进度、尝试和失败事实后续从 transcript 与各领域权威状态派生，绝不写回路线。
路线也只是意图和约束，不能授予路径权限。
"""
from __future__ import annotations

import functools
import glob
import hashlib
import json
import os
import re
import stat as statmod
import time
import uuid
from copy import deepcopy
from pathlib import Path
from typing import Any

from core.state import _slug as _artifact_slug
from core.tool_registry import ToolDefinition, register_tool

try:
    from shared.tools.library.artifacts_extra import (
        _freeze_artifact,
        register_save_gate,
    )
except ImportError:  # pragma: no cover - standalone node bootstrap compatibility.
    from tools.artifacts_extra import _freeze_artifact, register_save_gate

try:
    from .build_contract import parse_contract, validate_contract
    from .execution_envelope import (
        execution_envelope_gate_mode,
        execution_envelope_refs_for_action,
        missing_execution_envelope_steps,
        refresh_execution_envelope_binding,
        validate_route_execution_envelope_refs,
    )
    from .path_roles import CANONICAL_ROLES, collect_path_roles
    from .run_contract import (
        compare_execution_intent_binding_receipt,
        execution_intent_binding_receipt,
        load_execution_mode_view,
        prereg_assignment_scientific_block,
    )
except ImportError:  # pragma: no cover - standalone node bootstrap compatibility.
    from tools.build_contract import parse_contract, validate_contract
    from tools.execution_envelope import (
        execution_envelope_gate_mode,
        execution_envelope_refs_for_action,
        missing_execution_envelope_steps,
        refresh_execution_envelope_binding,
        validate_route_execution_envelope_refs,
    )
    from tools.path_roles import CANONICAL_ROLES, collect_path_roles
    from tools.run_contract import (
        compare_execution_intent_binding_receipt,
        execution_intent_binding_receipt,
        load_execution_mode_view,
        prereg_assignment_scientific_block,
    )


ROUTE_SCHEMA_VERSION = 2
CANONICAL_ROUTE_NAME = "execution_route"
# 2026-09-14 之前全项目共用的固定身份。只为升级前已在它名下声明过路线的 run 保留
# （见 _canonical_route_name）；删除条件：不再需要续跑本提交之前声明路线的 run。
CANONICAL_ROUTE_ARTIFACT_ID = f"declared_route__{CANONICAL_ROUTE_NAME}"
_ROUTE_INTENT_BINDING_METADATA_KEY = "execution_intent_binding"


def _canonical_route_name(state: Any) -> str:
    """本 run 的 canonical 路线名 ``execution_route__<run 身份>``。

    路线的语义是「本 run 的执行路线」：步骤绑定与执行事件都在本 run 的 transcript 里，
    读取也要求产出方是当前 run。但绑定工作区的 run 共用本节点账本（d10d6c85），修订
    又保留原产出方（09-01 core「修订不是产出」）。固定名加上这两条，同一工作区的第二个
    run 只能修订第一个 run 的路线、修订又永远不归它——声明不了也读不到（2026-09-14
    第三会话复审 §六）。所以身份按 run 区分，那两条规则一条不松。

    兼容：固定名下已有本 run 产出的路线（升级前声明、升级后继续跑）→ 沿用固定名，不迁移，
    已冻结版本的绑定与恢复 lineage 不变。只读账本头行，不读正文；头行读不出时按「不是
    本 run 的」处理——别的 run 的坏记录不能挡住本 run。
    """
    run_id = str(getattr(state, "run_id", "") or "")
    head_of = getattr(state, "artifact_head", None)
    legacy = None
    if callable(head_of):
        try:
            legacy = head_of(CANONICAL_ROUTE_ARTIFACT_ID)
        except Exception:
            legacy = None
    if legacy is not None and getattr(legacy, "produced_by_run_id", None) == run_id:
        return CANONICAL_ROUTE_NAME
    token = _artifact_slug(run_id)
    if token != run_id or len(token) > 40:
        token = hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:24]
    return f"{CANONICAL_ROUTE_NAME}__{token}"


def _canonical_route_artifact_id(state: Any) -> str:
    return f"declared_route__{_canonical_route_name(state)}"


def _canonical_route_save_gate(
    state: Any,
    draft: dict[str, Any],
) -> dict[str, Any]:
    """canonical route 及其恢复 metadata 只能由生命周期工具铸造。"""
    slug = _artifact_slug(str((draft or {}).get("name") or ""))
    if slug != CANONICAL_ROUTE_NAME and not slug.startswith(f"{CANONICAL_ROUTE_NAME}__"):
        return {}
    return {
        "failures": {
            "canonical_route_owner": (
                "canonical execution_route 包含冻结 DAG、版本链和已验证恢复收据，"
                "不能由通用 save_artifact 创建或覆盖"
            ),
        },
        "hint": (
            "使用 declare_execution_route；旧 declared_route 可保留其他名称，"
            "但不能竞争 canonical identity。"
        ),
    }


_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
_CONTAINER_RUNTIME_ID_RE = re.compile(r"^[0-9a-f]{64}$")
_MAX_STEPS = 128
# A compound receipt must stay auditable and bounded even though one route may
# carry many steps.  Static toolchain payloads normally need only a handful.
_MAX_PROGRAM_SEQUENCE = 64
_PROGRESS_FIELDS = frozenset({"status", "current", "current_step", "attempts"})

# 有限、应用无关的副作用词汇。它描述的是框架需要安装哪类守卫，不描述软件
# 领域。新增 effect 必须先有明确的执行语义，不能仅靠路线写一个词就假装已保护。
ROUTE_EFFECTS = frozenset({
    "network_access",
    "workspace_write",
    "source_change",
    "environment_change",
    "process_tree",
    "external_job",
    "scientific_execution",
})

# 这是当前已实现“动作前绑定 + 完成收据”的封闭工具集。跨节点 Data
# 交付、人工授权和 blocker 各有自己的领域生命周期，不能伪装成可由 shell
# 返回码完成的路线步骤。新增受管工具必须先实现绑定与收据语义。
_KNOWN_ACTION_TOOLS = frozenset({
    "safe_run_bash",
    "safe_execute_python",
    "submit_job",
})


@functools.lru_cache(maxsize=1)
def _experiment_tool_names() -> frozenset[str]:
    """experiment harness 列出的工具名。只认本节点可用的工具：别的节点以后注册了与
    某个真实程序同名的工具，不该误伤这里的路线声明（第三会话复审 0914c K10 P3）。"""
    try:
        import yaml

        data = yaml.safe_load(
            (Path(__file__).resolve().parents[1] / "harness.yaml").read_text(encoding="utf-8"))
        tools = data.get("tools") if isinstance(data, dict) else None
        return frozenset(item for item in tools or [] if isinstance(item, str))
    except Exception:
        return frozenset()


def unbindable_route_step_reason(action: dict[str, Any]) -> str | None:
    """这一步按 program 判定永远绑定不上执行动作时返回原因，否则 None（收敛任务书 K10）。

    - ``tool_name``：program 是工具名。fetch_resource 这类工具不带 route_step_id，
      safe_run_bash 等三件执行工具的名字也不是可执行入口；
    - ``env_wrapper``：safe_run_bash + program=env。env 只是包装器，执行时观测到的是它
      后面的程序；
    - ``read_only_command``：safe_run_bash + 只读名单里的裸命令名。只读调用判为
      route_not_required，不建 attempt。

    只认裸名：``./cat``、``/opt/x/cat`` 这类路径入口不走只读快速路径，照常绑定。
    submit_job 里跑只读命令也照常有 attempt，不算。
    """
    program = str(action.get("program") or "").strip()
    if not program:
        return None
    if program in _KNOWN_ACTION_TOOLS or program in _experiment_tool_names():
        return "tool_name"
    if action.get("tool") != "safe_run_bash" or "/" in program:
        return None
    if program == "env":
        return "env_wrapper"
    try:
        from .safe_bash import _READ_ONLY_COMMANDS
    except ImportError:
        from tools.safe_bash import _READ_ONLY_COMMANDS
    return "read_only_command" if program in _READ_ONLY_COMMANDS else None


def _program_needs_managed_lifecycle(program: str) -> bool:
    """声明期只凭 action.program（argv[0]）就能判定的真构建 / 真启动器。

    与执行期 `_bash_route_action` 用同一套分类器（safe_bash._is_major_build /
    _is_major_run），不另抄名单，但分界不完全相同：执行期看得到参数，把
    `make --version`、`make -n` 这类探查放行；声明期只有 program，一律按构建算——
    探查本就不该写成路线步骤。cmake、编译器这类要看参数才知道是不是构建的入口，
    这里判不出，照旧留给执行期。分类器取不到时返回 False：执行期那道检查仍在。
    """
    try:
        try:
            from .safe_bash import _is_major_build, _is_major_run
        except ImportError:
            from tools.safe_bash import _is_major_build, _is_major_run
        return bool(_is_major_build(program) or _is_major_run(program))
    except Exception:
        return False


def external_job_contract_violation(
    tool: Any,
    effects: Any,
) -> dict[str, str] | None:
    """返回 external_job 与 submit_job 的唯一所有权契约冲突。

    external_job 不是普通资源提示：它表示工作会逃离本地进程 cgroup，
    因而必须由 submit_job 的持久身份/收据生命周期接管。反方向同样成立：
    每个真实 submit_job 路线步骤都必须声明 external_job。validator 与
    runtime 复用本纯函数，避免“声明时一套规则、执行时另一套规则”。
    """
    normalized_tool = str(tool or "").strip()
    effect_items = [effects] if isinstance(effects, str) else (effects or [])
    normalized_effects = {
        str(item).strip()
        for item in effect_items
        if str(item).strip()
    }
    has_external_job = "external_job" in normalized_effects
    # 2026-09-12：判据从 process_tree（会不会 fork 子进程）换成 managed_lifecycle
    # （要不要一个活过本次调用的身份）。几乎什么都 fork —— tar 调 gzip、make 调编译器，
    # 于是"解个压缩包"被逼去走提交作业那条重路。process_tree 仍是机械事实，继续驱动
    # 资源守卫、进程数限额与策略档位，只是不再单独决定所有权。
    # managed_lifecycle 只由动作派生产生（真启动器、真构建），不进 ROUTE_EFFECTS，
    # 所以模型不需要多声明一个词。
    needs_managed_lifecycle = bool(normalized_effects.intersection({
        "managed_lifecycle", "scientific_execution",
    }))
    uses_submit_job = normalized_tool == "submit_job"
    if has_external_job and not uses_submit_job:
        return {
            "contract_code": "external_job_requires_submit_job",
            "required_tool": "submit_job",
            "message": (
                "external_job 只能由 submit_job 承担；safe_run_bash/"
                "safe_execute_python 没有外部作业身份与完成收据生命周期"
            ),
        }
    if needs_managed_lifecycle and not uses_submit_job:
        return {
            "contract_code": "process_tree_requires_submit_job",
            "required_tool": "submit_job",
            "message": (
                "真正的构建/启动器与正式科学执行必须由 submit_job 承担："
                "它们需要一个活过本次调用的作业身份、恢复状态与完成收据，"
                "而 safe_run_bash 只拥有有界的诊断生命周期。"
                "有界的本地动作（解包、configure、拉取依赖）不在此列，"
                "照常用 safe_run_bash 即可。修订时只需把该步骤的 action.tool 改成 "
                "submit_job；external_job effect 由声明期自动补上，不必再单独修一版"
            ),
        }
    if uses_submit_job and not has_external_job:
        return {
            "contract_code": "submit_job_requires_external_job",
            "required_effect": "external_job",
            "message": "submit_job 路线步骤必须显式声明 external_job effect",
        }
    return None

_ROUTE_TOP_FIELDS = frozenset({
    "schema_version", "goal", "evidence_refs", "steps",
})
_ROUTE_STEP_FIELDS = frozenset({
    "id", "goal", "after", "action", "effects", "workdir_role",
    "expected_outputs",
})
_ROUTE_ACTION_FIELDS = frozenset({
    "tool", "program", "program_sequence", "evidence_refs",
})

_LOCAL_CORRECTION_WITNESS_SCOPE = "run_shared_workdir_time_window"
LOCAL_CORRECTION_LIMITATION_CHECK = (
    "local_exact_output_correction_witness_limitations"
)


def _step_definition_hash_fields() -> list[str]:
    """Return the public leaf-field contract from the validator's own schema.

    ``step_definition_hash`` hashes the complete normalized step.  Keeping a
    handwritten list beside that implementation already drifted in four
    places, including advertising the nonexistent ``action.workdir_role``.
    Derive the disclosure from the same accepted-field sets instead.
    """
    step_fields = sorted(_ROUTE_STEP_FIELDS.difference({"action"}))
    action_fields = [
        f"action.{field}" for field in sorted(_ROUTE_ACTION_FIELDS)
    ]
    return sorted([*step_fields, *action_fields])


def _step_identity_contract() -> dict[str, Any]:
    return {
        "definition_hash_fields": _step_definition_hash_fields(),
        "step_goal_semantics": (
            "step.goal is execution intent; changing it invalidates an "
            "already-verified step binding"
        ),
        "route_goal_semantics": (
            "route.goal is route-level prose and is not part of any step "
            "definition hash"
        ),
    }

# Provider 侧 schema 负责让模型在第一次调用前看懂结构；runtime validator
# 仍独立执行同一契约，因为 YAML/string 输入和内部调用不能依赖 provider 校验。
ROUTE_V2_TOOL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "schema_version": {"type": "integer", "const": ROUTE_SCHEMA_VERSION},
        "goal": {"type": "string", "minLength": 1},
        "evidence_refs": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
            "minItems": 1,
        },
        "steps": {
            "type": "array",
            "minItems": 1,
            "maxItems": _MAX_STEPS,
            "items": {
                "type": "object",
                "properties": {
                    "id": {
                        "type": "string",
                        "pattern": _ID_RE.pattern,
                    },
                    "goal": {"type": "string", "minLength": 1},
                    "after": {
                        "type": "array",
                        "items": {"type": "string", "pattern": _ID_RE.pattern},
                        "uniqueItems": True,
                        "description": (
                            "显式依赖步骤 id；根步骤写 []。存在先后关系时不得"
                            "把所有步骤扁平化为根步骤。"
                        ),
                    },
                    "action": {
                        "type": "object",
                        "properties": {
                            "tool": {
                                "type": "string",
                                "enum": sorted(_KNOWN_ACTION_TOOLS),
                            },
                            "program": {
                                "type": "string",
                                "pattern": r"^\S+$",
                                "description": (
                                    "只填单个可执行入口（argv[0]），不填参数、"
                                    "重定向、管道或 compound:*。"
                                ),
                            },
                            "program_sequence": {
                                "type": "array",
                                "minItems": 2,
                                "maxItems": _MAX_PROGRAM_SEQUENCE,
                                "items": {
                                    "type": "string",
                                    "pattern": r"^\S+$"
                                },
                                "description": (
                                    "仅 submit_job 的静态线性复合 payload 使用；"
                                    "按 AST 直接执行入口的真实顺序完整填写，不能省略"
                                    "echo/cat/set 等条目，不能填 compound:*。"
                                    "这是多个**可执行入口**的序列（如 "
                                    "`make && ./run` → [\"make\", \"./run\"]），"
                                    "不是单个程序的参数表；`python3 x.py 1 2` 只有"
                                    " python3 一个入口，不要填本字段。"
                                ),
                            },
                            "evidence_refs": {
                                "type": "array",
                                "items": {"type": "string", "minLength": 1},
                                "uniqueItems": True,
                            },
                        },
                        "required": ["tool", "program"],
                        "additionalProperties": False,
                    },
                    "effects": {
                        "type": "array",
                        "items": {"type": "string", "enum": sorted(ROUTE_EFFECTS)},
                        "uniqueItems": True,
                        "description": (
                            "选择框架要安装的机械守卫：会起子进程的本地程序（含解包、"
                            "configure、构建、operation smoke run）都用 process_tree；"
                            "scientific_execution 只用于已分类为 scientific 的正式科学"
                            "执行，不能用于安装、构建或非科学最小运行。真正的构建与"
                            "启动器另由框架从命令本身派生所有权要求，要走 submit_job，"
                            "不需要你多声明一个词。"
                        ),
                    },
                    "workdir_role": {
                        "type": "string",
                        "enum": sorted(CANONICAL_ROLES),
                    },
                    "expected_outputs": {
                        "type": "array",
                        "items": {"type": "string", "minLength": 1},
                        "uniqueItems": True,
                        "description": (
                            "相对 workdir_role 的产物路径/通配模式；无文件产物时写 []，"
                            "完成仍须受管收据或领域权威状态。"
                        ),
                    },
                },
                "required": [
                    "id", "goal", "after", "action", "effects",
                    "workdir_role", "expected_outputs",
                ],
                "additionalProperties": False,
            },
        },
    },
    "required": ["schema_version", "goal", "evidence_refs", "steps"],
    "additionalProperties": False,
}


def _nonempty_strings(value: Any) -> list[str] | None:
    if not isinstance(value, list):
        return None
    normalized: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            return None
        normalized.append(item.strip())
    return normalized


def _has_cycle(dependencies: dict[str, list[str]]) -> bool:
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(step_id: str) -> bool:
        if step_id in visiting:
            return True
        if step_id in visited:
            return False
        visiting.add(step_id)
        for dependency in dependencies.get(step_id, []):
            if dependency in dependencies and visit(dependency):
                return True
        visiting.remove(step_id)
        visited.add(step_id)
        return False

    return any(visit(step_id) for step_id in dependencies)


def validate_route_v2(
    route_or_content: Any, *, declaring: bool = False,
) -> dict[str, Any]:
    """校验并规范化 v2 静态路线；不读取 state，也不推导执行进度。

    读取路径（load_canonical_route、_known_route_step_bindings）也经这里规范化**已冻结**
    的历史版本。只该挡新声明的规则放在 ``declaring`` 后面，只由 _declare_execution_route
    打开：回头判旧版本会让升级前合法冻结的路线整条失效，重声明随即绕过「作业还在进行」
    的检查，已提交作业的步骤掉回待执行（2026-09-14 第三会话复审 P2）。
    """
    route = parse_contract(route_or_content)
    errors: list[str] = []
    warnings: list[str] = []
    if not route:
        return {
            "valid": False,
            "errors": ["declared_route v2 必须是 JSON/YAML object"],
            "warnings": [],
            "route": {},
        }
    route = deepcopy(route)

    unknown_top = sorted(set(route).difference(_ROUTE_TOP_FIELDS))
    if unknown_top:
        errors.append(f"route 含不支持字段：{unknown_top}")

    if route.get("schema_version") != ROUTE_SCHEMA_VERSION:
        errors.append("schema_version 必须严格等于整数 2")
    forbidden_top = sorted(_PROGRESS_FIELDS.intersection(route))
    if forbidden_top:
        errors.append(
            f"路线不能保存执行进度字段：{forbidden_top}；进度必须从客观事实派生"
        )

    goal = route.get("goal")
    if not isinstance(goal, str) or not goal.strip():
        errors.append("goal 必须是非空字符串")
    else:
        route["goal"] = goal.strip()

    evidence_refs = _nonempty_strings(route.get("evidence_refs"))
    if not evidence_refs:
        errors.append("evidence_refs 必须是至少含一项的非空字符串列表")
        evidence_refs = []
    route["evidence_refs"] = evidence_refs

    raw_steps = route.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        errors.append("steps 必须是至少含一个步骤的列表")
        raw_steps = []
    elif len(raw_steps) > _MAX_STEPS:
        errors.append(f"steps 不能超过 {_MAX_STEPS} 项")

    normalized_steps: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    dependencies: dict[str, list[str]] = {}
    for index, raw_step in enumerate(raw_steps):
        prefix = f"steps[{index}]"
        if not isinstance(raw_step, dict):
            errors.append(f"{prefix} 必须是 object")
            continue
        step = deepcopy(raw_step)
        unknown_step = sorted(set(step).difference(_ROUTE_STEP_FIELDS))
        if unknown_step:
            errors.append(f"{prefix} 含不支持字段：{unknown_step}")
        forbidden_step = sorted(_PROGRESS_FIELDS.intersection(step))
        if forbidden_step:
            errors.append(
                f"{prefix} 不能保存执行进度字段：{forbidden_step}"
            )

        step_id = step.get("id")
        if not isinstance(step_id, str) or not _ID_RE.fullmatch(step_id.strip()):
            errors.append(
                f"{prefix}.id 必须匹配 {_ID_RE.pattern}"
            )
            step_id = f"__invalid_{index}"
        else:
            step_id = step_id.strip()
            if step_id in seen_ids:
                errors.append(f"{prefix}.id={step_id!r} 重复")
            seen_ids.add(step_id)
        step["id"] = step_id

        step_goal = step.get("goal")
        if not isinstance(step_goal, str) or not step_goal.strip():
            errors.append(f"{prefix}.goal 必须是非空字符串")
        else:
            step["goal"] = step_goal.strip()

        if "after" not in step:
            errors.append(f"{prefix}.after 必须显式提供；根步骤写 []")
        after = _nonempty_strings(step.get("after", []))
        if after is None:
            errors.append(f"{prefix}.after 必须是字符串列表")
            after = []
        if len(after) != len(set(after)):
            errors.append(f"{prefix}.after 不能包含重复依赖")
        if step_id in after:
            errors.append(f"{prefix}.after 不能依赖自身")
        step["after"] = after
        dependencies[step_id] = after

        action = step.get("action")
        if not isinstance(action, dict):
            errors.append(f"{prefix}.action 必须是 object")
            action = {}
        else:
            action = deepcopy(action)
        unknown_action = sorted(set(action).difference(_ROUTE_ACTION_FIELDS))
        if unknown_action:
            errors.append(f"{prefix}.action 含不支持字段：{unknown_action}")
        tool = action.get("tool")
        if not isinstance(tool, str) or not _ID_RE.fullmatch(tool.strip()):
            errors.append(f"{prefix}.action.tool 必须是非空工具名")
        else:
            action["tool"] = tool.strip()
            if action["tool"] not in _KNOWN_ACTION_TOOLS:
                errors.append(
                    f"{prefix}.action.tool={action['tool']!r} 没有实现步骤绑定和"
                    "完成收据；不能声明为可执行路线步骤"
                )
        program = action.get("program")
        if not isinstance(program, str) or not program.strip():
            errors.append(f"{prefix}.action.program 必须是非空入口标识")
        else:
            program = program.strip()
            action["program"] = program
            if re.search(r"\s", program) or program.startswith("compound:"):
                errors.append(
                    f"{prefix}.action.program 只填单个可执行入口，不含参数、"
                    "管道或复合命令"
                )
            # 这几类步骤永远不会有 attempt，路线也就永远到不了 complete（K10；活体
            # fetch_public_resource 因此耗了 794 秒）。只在声明时查，不回头判已冻结版本。
            unbindable = unbindable_route_step_reason(action) if declaring else None
            if unbindable == "tool_name" and program in _KNOWN_ACTION_TOOLS:
                errors.append(
                    f"{prefix}.action.program={program!r} 是工具名，不是可执行入口："
                    "用哪个工具已经写在 action.tool 里，program 填这一步真正执行的"
                    "程序（如 make、python、./solver）"
                )
            elif unbindable == "tool_name":
                errors.append(
                    f"{prefix}.action.program={program!r} 是工具名，不是可执行入口："
                    f"{program} 这类工具不走路线、不带 route_step_id，直接调用它即可，"
                    "不要声明成路线步骤；program 只填这一步真正执行的程序"
                )
            elif unbindable == "env_wrapper":
                errors.append(
                    f"{prefix}.action.program='env' 只是包装器：program 填 env 之后真正"
                    "执行的程序（如 env OMP_NUM_THREADS=4 ./solver 填 ./solver）"
                )
            elif unbindable == "read_only_command":
                errors.append(
                    f"{prefix}.action.program={program!r} 是只读命令：safe_run_bash 的只读"
                    "调用直接执行、不建 attempt，也不带 route_step_id，这一步永远完成不了；"
                    "不要把它声明成路线步骤（要核对的产出写进产出步骤的 expected_outputs）"
                )
        raw_sequence = action.get("program_sequence")
        if raw_sequence is not None:
            sequence = _nonempty_strings(raw_sequence)
            if (
                action.get("tool") != "submit_job"
                or sequence is None
                or len(sequence) < 2
                or len(sequence) > _MAX_PROGRAM_SEQUENCE
            ):
                errors.append(
                    f"{prefix}.action.program_sequence 只允许 submit_job 的"
                    "至少两个静态直接入口"
                )
                sequence = []
            else:
                invalid_entries = [
                    item for item in sequence
                    if re.search(r"\s", item)
                    or item.startswith(("compound:", "legacy:"))
                    or (declaring and (
                        item in _KNOWN_ACTION_TOOLS or item in _experiment_tool_names()))
                ]
                if invalid_entries:
                    errors.append(
                        f"{prefix}.action.program_sequence 每项必须是单个入口，"
                        "不能含空白、compound:*、legacy:* 或工具名"
                    )
                if isinstance(program, str) and program.strip() and (
                    _normalized_program(program) != _normalized_program(sequence[0])
                ):
                    errors.append(
                        f"{prefix}.action.program 必须与 program_sequence 第一项的"
                        "规范化主要入口一致"
                    )
            action["program_sequence"] = sequence

        action_evidence = _nonempty_strings(action.get("evidence_refs", []))
        if action_evidence is None:
            errors.append(f"{prefix}.action.evidence_refs 必须是字符串列表")
            action_evidence = []
        action["evidence_refs"] = action_evidence
        step["action"] = action

        if "effects" not in step:
            errors.append(f"{prefix}.effects 必须显式提供；无副作用写 []")
        effects = _nonempty_strings(step.get("effects", []))
        if effects is None:
            errors.append(f"{prefix}.effects 必须是字符串列表")
            effects = []
        if len(effects) != len(set(effects)):
            errors.append(f"{prefix}.effects 不能包含重复项")
        unknown_effects = sorted(set(effects).difference(ROUTE_EFFECTS))
        if unknown_effects:
            errors.append(
                f"{prefix}.effects 含未实现语义 {unknown_effects}；不能把未知词当作已安装守卫"
            )
        # 051 摩擦清理：tool=submit_job 的步骤天然产生一个外部作业，external_job 由此
        # 派生，不再让模型多声明一个词再被拒一轮（09-22 三次活体 run 每次都为这一句
        # 多改一版路线）。推导只加严：这里只往 effects 里加，从不减；派生出来的词进
        # 冻结路线，之后的修订不带它也会再派生一次，身份哈希稳定。
        if (
            declaring
            and action.get("tool") == "submit_job"
            and "external_job" not in effects
        ):
            effects = [*effects, "external_job"]
            warnings.append(
                f"{prefix}.effects 自动补上 external_job（submit_job 步骤天然产生外部作业）"
            )
        step["effects"] = effects
        # 声明期也按 program 派生所有权要求（不写回 effects：managed_lifecycle 不是
        # 声明词）。ea19b807 把判据从 process_tree 换成 managed_lifecycle 之后，诚实
        # 声明 safe_run_bash + make 的路线 validate 照过，要到执行期才被拒，平白多一轮
        # "声明→执行被拒→再修订"。只在声明时派生（见 declaring）。
        ownership_effects = set(effects)
        declared_program = action.get("program")
        if (
            declaring
            and action.get("tool") in {"safe_run_bash", "safe_execute_python"}
            and isinstance(declared_program, str) and declared_program.strip()
            and _program_needs_managed_lifecycle(declared_program.strip())
        ):
            ownership_effects.add("managed_lifecycle")
        ownership_violation = external_job_contract_violation(
            action.get("tool"), ownership_effects)
        if ownership_violation is not None:
            errors.append(
                f"{prefix}: {ownership_violation['message']}"
                + (f"（action.program={declared_program!r}）"
                   if "managed_lifecycle" in ownership_effects.difference(effects)
                   else "")
            )

        role = step.get("workdir_role")
        if not isinstance(role, str) or role.strip() not in CANONICAL_ROLES:
            errors.append(
                f"{prefix}.workdir_role 必须是已有 canonical path role"
            )
        else:
            step["workdir_role"] = role.strip()

        if "expected_outputs" not in step:
            errors.append(
                f"{prefix}.expected_outputs 必须显式提供；无文件产物写 []"
            )
        expected = _nonempty_strings(step.get("expected_outputs", []))
        if expected is None:
            errors.append(f"{prefix}.expected_outputs 必须是字符串列表")
            expected = []
        for output in expected:
            output_path = Path(output)
            if output_path.is_absolute() or ".." in output_path.parts:
                errors.append(
                    f"{prefix}.expected_outputs 必须是相对 workdir_role 的安全路径/通配模式"
                )
        step["expected_outputs"] = expected
        normalized_steps.append(step)

    known_ids = set(dependencies)
    for step_id, after in dependencies.items():
        for dependency in after:
            if dependency not in known_ids:
                errors.append(
                    f"步骤 {step_id!r} 依赖不存在的步骤 {dependency!r}"
                )
    if _has_cycle(dependencies):
        errors.append("steps.after 形成依赖环")

    if len(normalized_steps) > 1 and all(
        not step.get("after") for step in normalized_steps
    ):
        warnings.append(
            "所有步骤均声明为可立即并行；若任务实际存在配置→构建→运行等"
            "前置关系，请在执行前用 after 表达依赖"
        )

    route["schema_version"] = ROUTE_SCHEMA_VERSION
    route["steps"] = normalized_steps
    return {
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "route": route,
    }


def _legacy_evidence_refs(contract: dict[str, Any]) -> list[str]:
    refs: list[str] = []
    for domain in (contract.get("env_domains") or {}).values():
        if not isinstance(domain, dict):
            continue
        probes = domain.get("probes")
        if isinstance(probes, list):
            refs.extend(str(item).strip() for item in probes if str(item).strip())
        elif str(probes or "").strip():
            refs.append(str(probes).strip())
    for risk in contract.get("accepted_risks") or []:
        if isinstance(risk, dict):
            evidence = str(risk.get("evidence") or risk.get("probe") or "").strip()
            if evidence:
                refs.append(evidence)
        elif str(risk or "").strip():
            refs.append(str(risk).strip())
    return list(dict.fromkeys(refs)) or ["legacy:declared_route"]


def _normalize_legacy_route(contract: dict[str, Any]) -> dict[str, Any]:
    activities = contract.get("activities")
    if not isinstance(activities, dict):
        activities = {"compile": True, "run": False}
    path_roles = contract.get("path_roles")
    if not isinstance(path_roles, dict):
        path_roles = {}

    specs: list[tuple[str, str, str, list[str]]] = []
    if activities.get("manage_dependencies"):
        specs.append((
            "legacy_dependencies", "执行旧契约声明的依赖准备",
            "build_root", ["environment_change", "workspace_write"],
        ))
    if activities.get("modify_source"):
        specs.append((
            "legacy_source_change", "执行旧契约声明的源码修改",
            "source_worktree_root", ["source_change", "workspace_write"],
        ))
    if activities.get("compile", True):
        specs.append((
            "legacy_build", "执行旧契约声明的构建",
            "build_root", ["workspace_write", "process_tree"],
        ))
    if activities.get("run"):
        specs.append((
            "legacy_run", "执行旧契约声明的最小运行",
            "run_root", ["workspace_write", "process_tree"],
        ))
    if not specs:
        specs.append((
            "legacy_action", "执行旧契约声明的动作",
            "run_root", ["workspace_write"],
        ))

    legacy_expected = [
        str(item.get("path")).strip()
        for item in contract.get("expected_artifacts") or []
        if isinstance(item, dict) and str(item.get("path") or "").strip()
    ]
    evidence = _legacy_evidence_refs(contract)
    evidence.extend(f"legacy_expected:{item}" for item in legacy_expected)
    steps: list[dict[str, Any]] = []
    previous: str | None = None
    for index, (step_id, step_goal, fallback_role, effects) in enumerate(specs):
        role = fallback_role
        if role not in path_roles and fallback_role == "source_worktree_root":
            role = "build_root"
        managed_lifecycle = bool(set(effects).intersection({
            "process_tree", "scientific_execution",
        }))
        if managed_lifecycle and "external_job" not in effects:
            effects = [*effects, "external_job"]
        step = {
            "id": step_id,
            "goal": step_goal,
            "after": [previous] if previous else [],
            "action": {
                "tool": "submit_job" if managed_lifecycle else "safe_run_bash",
                "program": f"legacy:{step_id}",
                "evidence_refs": evidence,
            },
            "effects": effects,
            "workdir_role": role,
            # 旧契约常保存绝对 target 路径，无法安全解释为某一步 workdir 的
            # 相对产物；保留为只读 evidence，不提升为 v2 完成判据。
            "expected_outputs": [],
        }
        steps.append(step)
        previous = step_id
    return {
        "schema_version": ROUTE_SCHEMA_VERSION,
        "goal": str(contract.get("goal") or "兼容旧 declared_route 构建契约").strip(),
        "evidence_refs": evidence,
        "steps": steps,
    }


def normalize_declared_route(
    route_or_content: Any,
    *,
    platform_profile: dict[str, Any] | None = None,
    source_recon: dict[str, Any] | None = None,
    prereg_text: str = "",
) -> dict[str, Any]:
    """识别 v2 或严格校验后的旧构建契约。

    显式声明了未知 ``schema_version`` 的内容绝不按 legacy 猜测。
    """
    parsed = parse_contract(route_or_content)
    if not parsed:
        return {
            "valid": False,
            "source_format": "unstructured",
            "errors": ["declared_route 必须是 JSON/YAML object"],
            "warnings": [],
            "route": {},
        }
    if "schema_version" in parsed:
        if parsed.get("schema_version") != ROUTE_SCHEMA_VERSION:
            return {
                "valid": False,
                "source_format": "unsupported",
                "errors": [
                    "不支持该 schema_version；格式错误或未知版本不能退回 legacy 放行"
                ],
                "warnings": [],
                "route": {},
            }
        report = validate_route_v2(parsed)
        return {**report, "source_format": "v2"}

    legacy = validate_contract(
        parsed,
        platform_profile=platform_profile,
        source_recon=source_recon,
        prereg_text=prereg_text,
    )
    if not legacy["valid"]:
        return {
            "valid": False,
            "source_format": "legacy",
            "errors": legacy["errors"],
            "warnings": legacy["warnings"],
            "route": {},
        }
    normalized = validate_route_v2(_normalize_legacy_route(legacy["contract"]))
    return {
        **normalized,
        "source_format": "legacy",
        "warnings": [*legacy["warnings"], *normalized["warnings"]],
    }


def _canonical_content(route: dict[str, Any]) -> str:
    return json.dumps(route, ensure_ascii=False, indent=2, sort_keys=True)


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _route_ref(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "artifact_id": f"declared_route__{_artifact_slug(str(record.get('name') or ''))}",
        "version": record.get("version"),
        "content_hash": record.get("content_hash"),
    }


def load_canonical_route(state: Any) -> dict[str, Any]:
    """读取本节点、本 run 的 canonical frozen route，不做跨节点/时间猜测。"""
    route_name = _canonical_route_name(state)
    try:
        record = state.read_artifact(f"declared_route__{route_name}")
    except (OSError, ValueError) as exc:
        return {
            "status": "invalid",
            "reason": "route_record_unreadable",
            "error": f"{type(exc).__name__}: {exc}",
        }
    if not isinstance(record, dict):
        # 账本里有这个路线身份、正文文件却读不到：不是「没声明过」（verify 清单 #4）。
        # 原先一律报 route_not_declared——声明入口按初次声明处理、ROC 当成不适用放行。
        head_of = getattr(state, "artifact_head", None)
        try:
            ledger_head = head_of(f"declared_route__{route_name}") if callable(head_of) else None
        except Exception:
            ledger_head = None
        if ledger_head is not None:
            return {
                "status": "invalid",
                "reason": "route_record_missing",
                "ledger_version": getattr(ledger_head, "version", None),
            }
        return {"status": "unavailable", "reason": "route_not_declared"}
    if record.get("type") != "declared_route" or record.get("name") != route_name:
        return {"status": "invalid", "reason": "route_identity_mismatch"}
    if record.get("produced_by_node_type") != "experiment":
        return {"status": "unavailable", "reason": "route_not_owned_by_experiment"}
    if record.get("produced_by_run_id") != state.run_id:
        return {
            "status": "unavailable",
            "reason": "route_not_owned_by_current_run",
        }
    metadata = record.get("metadata")
    if not isinstance(metadata, dict) or metadata.get("frozen") is not True:
        return {"status": "unavailable", "reason": "route_not_frozen"}
    content = record.get("content")
    if not isinstance(content, str):
        return {"status": "invalid", "reason": "route_content_missing"}
    if record.get("content_hash") != _content_hash(content):
        return {"status": "invalid", "reason": "route_content_hash_mismatch"}
    version = record.get("version")
    if not isinstance(version, int) or version < 1:
        return {"status": "invalid", "reason": "route_version_invalid"}
    normalized = normalize_declared_route(content)
    if not normalized["valid"]:
        return {
            "status": "invalid",
            "reason": "route_schema_invalid",
            "errors": normalized["errors"],
            "warnings": normalized["warnings"],
        }
    return {
        "status": "ready",
        "route": normalized["route"],
        "route_ref": _route_ref(record),
        "source_format": normalized["source_format"],
        "warnings": normalized["warnings"],
        "record": record,
    }


def _route_execution_intent_binding(
    state: Any,
    snapshot: dict[str, Any],
    *,
    require: bool,
) -> dict[str, Any]:
    """Compare the current scope against the receipt frozen with this route."""
    if snapshot.get("status") != "ready":
        return {
            "passed": not require,
            "applicable": False,
            "status": "route_unavailable",
        }
    metadata = ((snapshot.get("record") or {}).get("metadata") or {})
    stored_receipt = (
        metadata.get(_ROUTE_INTENT_BINDING_METADATA_KEY)
        if isinstance(metadata, dict) else None
    )
    comparison = compare_execution_intent_binding_receipt(
        state, stored_receipt, require=require
    )
    return {
        **comparison,
        "route_ref": snapshot.get("route_ref"),
        "stored_receipt": stored_receipt,
    }


def step_definition_hash(step: dict[str, Any]) -> str:
    """步骤定义的稳定哈希；只要定义变化，旧执行证据就自然失效。"""
    payload = json.dumps(
        step,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return _content_hash(payload)


def step_execution_contract_hash(step: dict[str, Any]) -> str:
    """哈希会改变执行、依赖或验收语义的字段，排除纯说明文本。

    完整 ``step_definition_hash`` 仍是进度证据的身份；本哈希只用于恢复链
    对账，防止改写 ``goal`` 就被误认为已经改变了失败条件。
    """
    contract = {
        key: deepcopy(step.get(key))
        for key in (
            "id", "action", "after", "effects", "expected_outputs",
            "workdir_role",
        )
    }
    payload = json.dumps(
        contract,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return _content_hash(payload)


_BLOCKING_EVENT_HISTORY_WARNINGS = frozenset({
    "transcript_unreadable",
    "transcript_corrupt_before_tail",
    "ignored_malformed_jsonl_line",
    "transcript_tail_unterminated",
    "route_history_audit_failed",
    "route_bound_missing_attempt_id",
    "duplicate_attempt_identity",
    "orphan_external_identity_resolution",
})


def _blocking_event_history_warning(*warning_sets: Any) -> str | None:
    """返回首个会使追加执行收据不再可信的历史告警。"""
    for warnings in warning_sets:
        for warning in warnings or []:
            value = str(warning or "")
            if value in _BLOCKING_EVENT_HISTORY_WARNINGS:
                return value
    return None


def _event_history_error_code(warning: Any) -> str:
    value = str(warning or "")
    if value in {
        "ignored_malformed_jsonl_line",
        "transcript_tail_unterminated",
    }:
        return "route_transcript_tail_unwritable"
    if value == "transcript_unreadable":
        return "route_transcript_unreadable"
    return "route_event_history_invalid"


def _read_transcript_events(state: Any) -> tuple[list[dict[str, Any]], list[str]]:
    """读取追加事件，容忍崩溃留下的半行，但不把半行当事实。"""
    path = getattr(state, "transcript_path", None)
    if path is None or not path.is_file():
        return [], []
    events: list[dict[str, Any]] = []
    malformed = False
    try:
        raw_text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return [], ["transcript_unreadable"]
    lines = raw_text.splitlines()
    tail_unterminated = bool(
        raw_text
        and not raw_text.endswith((chr(10), chr(13)))
        and lines
        and lines[-1].strip()
    )
    nonempty_indexes = [index for index, line in enumerate(lines) if line.strip()]
    last_nonempty = nonempty_indexes[-1] if nonempty_indexes else -1
    corrupt_before_tail = False
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            if index == last_nonempty:
                malformed = True
            else:
                corrupt_before_tail = True
            continue
        if isinstance(event, dict):
            events.append(event)
        else:
            if index == last_nonempty:
                malformed = True
            else:
                corrupt_before_tail = True
    warnings: list[str] = []
    if corrupt_before_tail:
        warnings.append("transcript_corrupt_before_tail")
    if malformed:
        warnings.append("ignored_malformed_jsonl_line")
    elif tail_unterminated:
        warnings.append("transcript_tail_unterminated")
    return events, warnings


def _known_route_step_bindings(state: Any) -> set[tuple[str, int, str, str, str]]:
    """返回本 run 冻结版本链中真实存在过的步骤定义绑定。"""
    bindings: set[tuple[str, int, str, str, str]] = set()
    route_id = _canonical_route_artifact_id(state)
    try:
        versions = state.artifact_versions(route_id)
    except Exception:
        versions = []
    for record in versions:
        if not isinstance(record, dict):
            continue
        if record.get("type") != "declared_route":
            continue
        if record.get("produced_by_node_type") != "experiment":
            continue
        if record.get("produced_by_run_id") != state.run_id:
            continue
        metadata = record.get("metadata")
        if not isinstance(metadata, dict) or metadata.get("frozen") is not True:
            continue
        content = record.get("content")
        version = record.get("version")
        content_hash = record.get("content_hash")
        if not isinstance(content, str) or not isinstance(version, int):
            continue
        if not isinstance(content_hash, str) or content_hash != _content_hash(content):
            continue
        normalized = normalize_declared_route(content)
        if not normalized["valid"]:
            continue
        for step in normalized["route"]["steps"]:
            bindings.add((
                route_id,
                version,
                content_hash,
                step["id"],
                step_definition_hash(step),
            ))
    return bindings


def _outcome_has_completion_evidence(
    event: dict[str, Any],
    expected_outputs: list[str] | None = None,
) -> bool:
    expected = set(expected_outputs or [])
    if expected:
        verified_specs = {
            str(item) for item in (event.get("verified_output_specs") or [])
            if str(item).strip()
        }
        if not expected.issubset(verified_specs):
            return False
    receipt = event.get("managed_tool_receipt")
    if isinstance(receipt, dict) and receipt.get("status") == "success":
        return receipt.get("returncode") in (None, 0)
    if isinstance(receipt, str) and receipt.strip():
        return True
    for field in ("evidence_refs", "verified_outputs", "domain_receipts"):
        values = event.get(field)
        if isinstance(values, list) and any(str(item).strip() for item in values):
            return True
    if str(event.get("evidence_artifact_id") or "").strip():
        return True
    return False


_EXTERNAL_PROJECTION_EVENT_TYPES = frozenset({
    "route_step_external_execution_verified",
    "route_step_external_finalized",
})


def external_finalization_projection(
    state: Any, reference: dict[str, Any],
) -> dict[str, Any] | None:
    """该作业**仍然生效**的最新一条路线收尾事件（只读查询，不产生任何事件）；没有则 None。

    只认生效中的投影：钉住的收尾在步骤契约变了之后被丢弃，不再算「已写下」
    （第三会话复审 0914c 971fd66a P3）。调用方要看事件里的 route_outcome：finalize 缺预期
    产物时同样会写收尾事件（failure_class=expected_outputs_missing），「已写下」不等于成功
    （第三会话复审 307e4079 P2）。
    """
    try:
        events, _warnings = _read_transcript_events(state)
    except Exception:
        return None
    expected = _external_receipt_key(reference)
    if not expected:
        return None
    attempts = list(dict.fromkeys(
        str(event.get("attempt_id") or "") for event in events
        if event.get("event") == "route_step_external_finalized"
        and _external_receipt_key(event) == expected
    ))
    active = [
        event
        for attempt_id in attempts if attempt_id
        for event in _active_attempt_projection_events(
            state, events, event_type="route_step_external_finalized",
            attempt_id=attempt_id)
        if _external_receipt_key(event) == expected
    ]
    return active[-1] if active else None


def external_finalization_already_projected(
    state: Any, reference: dict[str, Any],
) -> bool:
    """该作业是否已由 finalize 写下**仍然生效**的路线终态事实（只读查询，不产生任何事件）。

    run 级闭环用它跳过二次投影：作业收尾时已经把这条事实写进路线，闭环再投一次
    要么是重复，要么在两次之间发生过修订时直接撞
    route_external_execution_verification_conflict。写下的是成功还是失败，用
    external_finalization_projection 读 route_outcome。
    """
    return external_finalization_projection(state, reference) is not None


def _active_attempt_projection_events(
    state: Any,
    events: list[dict[str, Any]],
    *,
    event_type: str,
    attempt_id: str,
) -> list[dict[str, Any]]:
    """返回同一 attempt 中通过严格纠正校验后仍有效的投影事件。

    这不是全局 route reducer。唯一可取代的事实是本 attempt 的
    ``expected_outputs_missing``：当前 route 必须带匹配的 validated recovery，
    新旧事件还必须引用同一完整外部身份与同一终态证据。任一条件不满足时两个
    事件都会保留，交给调用方按 conflict 处理。
    """
    if event_type not in _EXTERNAL_PROJECTION_EVENT_TYPES:
        return []
    candidates = [
        event for event in events
        if event.get("event") == event_type
        and str(event.get("attempt_id") or "") == attempt_id
    ]
    # 纠正依据在**写入那一刻**已经验过，并钉进事件本身
    # （``supersession_basis_validated`` 与当时的步骤契约 hash）。不按当前 route
    # 重算，否则与该步骤无关的合法修订也会让一条写入时有效的 supersession 被反悔。
    # 但钉住只担保**那份契约**：该步骤的执行契约后来变了，旧结果证明不了新契约
    # （2026-09-13 审判：改回原路径后钉住的成功照旧生效，路线卡进 invalid_event_history；
    # 方案 C，用户定）。这时丢弃这条钉住——不取代、也不进 active——它想取代的失败原样
    # 保留，路线回到 blocked，出口是带 recovery_basis 重新纠正。不能两条都留：那会重演
    # 两条 finalized 同时复活、finalize 与 cancel 双双死锁。
    #
    # 兼容：没有钉住字段的旧事件，回退到按当前 lineage 重算。
    # owner=experiment；删除条件：不再需要读 2026-09-09 之前产生的 run 事件。
    legacy_correction_valid = (
        _validated_expected_outputs_correction(state, events, attempt_id)
        is not None
    )
    current_contract_hash, current_outputs = _attempt_step_contract(
        state, events, attempt_id)
    active: list[dict[str, Any]] = []
    for event in candidates:
        superseded = str(event.get("supersedes_failure_class") or "")
        pinned = event.get("supersession_basis_validated") is True
        if pinned and not _pinned_supersession_holds(
                event, current_contract_hash, current_outputs):
            continue
        correction_valid = pinned or legacy_correction_valid
        event_identity = _external_receipt_key(event)
        identity_complete = bool(
            event_identity[0]
            and event_identity[5]
            and str(event.get("route_attempt_id") or "") == attempt_id
            and (
                event_identity[0] != "local"
                or _valid_local_external_identity(event)
            )
        )
        matching_failures = [
            previous for previous in active
            if str(previous.get("failure_class") or "") == superseded
            and _external_receipt_key(previous) == event_identity
            and str(previous.get("route_attempt_id") or "") == attempt_id
        ]
        can_supersede = bool(
            correction_valid
            and identity_complete
            and superseded == "expected_outputs_missing"
            and event.get("route_outcome") == "success"
            and not str(event.get("failure_class") or "")
            and len(matching_failures) == 1
        )
        if can_supersede and event_type == (
            "route_step_external_execution_verified"
        ):
            previous = matching_failures[0]
            previous_receipt = previous.get("verification_receipt")
            current_receipt = event.get("verification_receipt")
            can_supersede = bool(
                str(previous.get("verification_digest") or "")
                and previous.get("verification_digest")
                == event.get("verification_digest")
                and isinstance(previous_receipt, dict)
                and isinstance(current_receipt, dict)
                and previous_receipt.get("terminal") is True
                and previous_receipt.get("success_verified") is True
                and current_receipt.get("terminal") is True
                and current_receipt.get("success_verified") is True
            )
        elif can_supersede and event_type == "route_step_external_finalized":
            previous = matching_failures[0]
            can_supersede = bool(
                str(previous.get("domain_outcome") or "")
                == str(event.get("domain_outcome") or "")
                and str(previous.get("evidence_artifact_id") or "")
                == str(event.get("evidence_artifact_id") or "")
            )
        if can_supersede:
            active.remove(matching_failures[0])
        active.append(event)
    return active


def build_route_snapshot(
    state: Any,
    *,
    require_recovery_lineage: bool = False,
) -> dict[str, Any]:
    """从 frozen route + 追加事件派生步骤事实，不保存另一份进度状态。"""
    loaded = load_canonical_route(state)
    if loaded.get("status") != "ready":
        return {**loaded, "route_state": "unavailable", "ready_step_ids": []}

    events, transcript_warnings = _read_transcript_events(state)
    known_bindings = _known_route_step_bindings(state)
    steps = loaded["route"]["steps"]
    current_hashes = {
        step["id"]: step_definition_hash(step)
        for step in steps
    }

    outcomes_by_attempt: dict[str, list[dict[str, Any]]] = {}
    identity_resolutions_by_attempt: dict[str, list[dict[str, Any]]] = {}
    external_verifications_by_attempt: dict[str, list[dict[str, Any]]] = {}
    external_finalizations_by_attempt: dict[str, list[dict[str, Any]]] = {}
    for event in events:
        attempt_id = event.get("attempt_id")
        if not isinstance(attempt_id, str) or not attempt_id:
            continue
        if event.get("event") == "route_step_outcome":
            outcomes_by_attempt.setdefault(attempt_id, []).append(event)
        elif event.get("event") == "route_step_external_identity_resolved":
            identity_resolutions_by_attempt.setdefault(
                attempt_id, []).append(event)
        elif event.get("event") == "route_step_external_execution_verified":
            external_verifications_by_attempt.setdefault(attempt_id, []).append(event)
        elif event.get("event") == "route_step_external_finalized":
            external_finalizations_by_attempt.setdefault(attempt_id, []).append(event)

    bounds_by_step: dict[str, list[dict[str, Any]]] = {
        step["id"]: [] for step in steps
    }
    history_warnings: list[str] = []
    seen_attempts: dict[str, tuple[str, str]] = {}
    for event in events:
        if event.get("event") != "route_step_bound":
            continue
        attempt_id = event.get("attempt_id")
        step_id = event.get("route_step_id")
        definition_hash = event.get("step_definition_hash")
        binding = (
            event.get("route_artifact_id"),
            event.get("route_version"),
            event.get("route_content_hash"),
            step_id,
            definition_hash,
        )
        if binding not in known_bindings:
            continue
        if current_hashes.get(step_id) != definition_hash:
            continue
        if not isinstance(attempt_id, str) or not attempt_id:
            history_warnings.append("route_bound_missing_attempt_id")
            continue
        previous = seen_attempts.get(attempt_id)
        identity = (str(step_id), str(definition_hash))
        if previous is not None and previous != identity:
            history_warnings.append("duplicate_attempt_identity")
            continue
        seen_attempts[attempt_id] = identity
        bounds_by_step[str(step_id)].append(event)

    if set(identity_resolutions_by_attempt).difference(seen_attempts):
        history_warnings.append("orphan_external_identity_resolution")

    derived: dict[str, dict[str, Any]] = {}
    blocking_history_warning = _blocking_event_history_warning(
        transcript_warnings, history_warnings,
    )
    invalid_history = blocking_history_warning is not None
    terminal_states = {
        "failed": "failed",
        "blocked": "blocked",
        "cancelled": "cancelled",
        "timeout": "failed",
        "unknown": "interrupted",
        "submitted": "in_progress",
    }
    # 纠正 lineage：各冻结路线版本里的恢复收据，按 attempt 收集——不看当前版本的单槽。
    recovery_lineage = _recovery_lineage(
        state, fail_on_read_error=require_recovery_lineage)
    run_id = str(getattr(state, "run_id", "") or "")
    correction_witness_limitations: list[dict[str, Any]] = []
    for step in steps:
        step_id = step["id"]
        info: dict[str, Any] = {
            "state": "pending",
            "definition_hash": current_hashes[step_id],
            "after": list(step["after"]),
        }
        bounds = bounds_by_step[step_id]
        if bounds:
            bound = bounds[-1]
            attempt_id = str(bound["attempt_id"])
            info["attempt_id"] = attempt_id
            info["bound_route_version"] = bound.get("route_version")
            info["applied_policy"] = bound.get("applied_policy")
            outcomes = outcomes_by_attempt.get(attempt_id, [])
            resolutions = identity_resolutions_by_attempt.get(attempt_id, [])
            resolution_error = ""
            if len(resolutions) > 1:
                resolution_error = "multiple_external_identity_resolutions"
            elif resolutions:
                original = outcomes[0] if len(outcomes) == 1 else {}
                original_is_unresolved = (
                    not outcomes
                    or (
                        original.get("outcome") == "unknown"
                        and original.get("failure_class")
                        == "external_identity_reconciliation_required"
                    )
                )
                valid_resolution = (
                    original_is_unresolved
                    and len(outcomes) <= 1
                    and bound.get("tool") == "submit_job"
                    and bound.get("applied_policy") == "managed_external_job"
                    and _valid_external_identity_resolution_event(
                        state, resolutions[0], attempt_id)
                )
                if not valid_resolution:
                    resolution_error = "invalid_external_identity_resolution"
                else:
                    outcomes = [{
                        "outcome": "submitted",
                        "identity_resolved": True,
                        "domain_receipts": [resolutions[0]["domain_receipt"]],
                    }]
                    info["identity_resolution"] = "exact"
            if resolution_error:
                info["state"] = "event_conflict"
                info["reason"] = resolution_error
                invalid_history = True
            elif len(outcomes) > 1:
                info["state"] = "event_conflict"
                info["reason"] = "multiple_route_step_outcomes"
                invalid_history = True
            elif not outcomes:
                info["state"] = "interrupted"
                info["reason"] = "bound_without_outcome"
            else:
                outcome = outcomes[0]
                value = str(outcome.get("outcome") or "")
                info["outcome"] = value
                if value == "success":
                    if _outcome_has_completion_evidence(
                        outcome, step.get("expected_outputs")):
                        info["state"] = "verified"
                    else:
                        info["state"] = "unverified"
                        info["reason"] = (
                            "expected_outputs_not_verified"
                            if step.get("expected_outputs")
                            else "success_without_completion_evidence"
                        )
                elif value == "submitted":
                    verifications = _active_attempt_projection_events(
                        state,
                        events,
                        event_type="route_step_external_execution_verified",
                        attempt_id=attempt_id,
                    )
                    finalizations = _active_attempt_projection_events(
                        state,
                        events,
                        event_type="route_step_external_finalized",
                        attempt_id=attempt_id,
                    )
                    if len(verifications) > 1:
                        info["state"] = "event_conflict"
                        info["reason"] = "multiple_external_execution_verifications"
                        invalid_history = True
                    elif len(finalizations) > 1:
                        info["state"] = "event_conflict"
                        info["reason"] = "multiple_external_finalizations"
                        invalid_history = True
                    elif verifications:
                        verification = verifications[0]
                        projected = str(verification.get("route_outcome") or "")
                        info["external_outcome"] = verification.get(
                            "verification_status")
                        finalization = finalizations[0] if finalizations else None
                        final_projected = str(
                            (finalization or {}).get("route_outcome") or "")
                        blocked_after_observation = bool(
                            finalization
                            and final_projected == "blocked"
                            and str(finalization.get("domain_outcome") or "")
                            == "operation_blocked"
                            and projected in {"success", "failed"}
                            and _external_receipt_key(finalization)
                            == _external_receipt_key(verification)
                            and str(finalization.get("route_attempt_id") or "")
                            == attempt_id
                        )
                        if finalization:
                            info["workflow_outcome"] = finalization.get(
                                "domain_outcome")
                        if (
                            finalization
                            and final_projected != projected
                            and not blocked_after_observation
                        ):
                            info["state"] = "event_conflict"
                            info["reason"] = (
                                "external_verification_finalization_conflict"
                            )
                            invalid_history = True
                        elif blocked_after_observation:
                            # Execution/output observation and workflow
                            # disposition are different axes.  A later,
                            # independently witnessed blocker must not erase a
                            # real route success or output failure, but it also
                            # must not unlock dependent steps.
                            info["execution_observation"] = projected
                            if projected == "failed":
                                info["state"] = "failed"
                                info["reason"] = str(
                                    verification.get("failure_class")
                                    or "failed")
                            else:
                                info["state"] = "blocked"
                                info["reason"] = "operation_blocked"
                        elif (
                            projected == "success"
                            and _outcome_has_completion_evidence(
                                verification, step.get("expected_outputs"))
                        ):
                            info["state"] = "verified"
                        elif projected in {"failed", "blocked", "cancelled"}:
                            info["state"] = projected
                            info["reason"] = str(
                                verification.get("failure_class") or projected)
                        else:
                            info["state"] = "event_conflict"
                            info["reason"] = (
                                "invalid_external_execution_verification"
                            )
                            invalid_history = True
                    elif not finalizations:
                        info["state"] = "in_progress"
                        info["reason"] = "submitted"
                        # 048 v5：让 in_progress 的出口能逐字写出 finalize_external_job 的
                        # 实参（原来快照没带 scheduler/job_id，那段"逐字调用"从未成立）。
                        receipts = outcome.get("domain_receipts") or []
                        first = receipts[0] if receipts and isinstance(receipts[0], dict) else {}
                        info["scheduler"] = first.get("scheduler")
                        info["job_id"] = first.get("job_id")
                    else:
                        # 兼容升级前只写 route_step_external_finalized 的历史 run。
                        # 新 run 的 success 路线只由上面的 execution verification
                        # 推进；workflow finalization 本身不再是执行成功证据。
                        finalization = finalizations[0]
                        projected = str(finalization.get("route_outcome") or "")
                        info["external_outcome"] = finalization.get("domain_outcome")
                        if (
                            projected == "success"
                            and _outcome_has_completion_evidence(
                                finalization, step.get("expected_outputs"))
                        ):
                            info["state"] = "verified"
                        elif projected in {"failed", "blocked", "cancelled"}:
                            info["state"] = projected
                            info["reason"] = str(
                                finalization.get("domain_outcome") or projected)
                        else:
                            info["state"] = "event_conflict"
                            info["reason"] = "invalid_external_finalization"
                            invalid_history = True
                elif value == "rejected":
                    # 零执行的基础设施拒绝：payload 从未启动，重试不违反
                    # “科学执行不静默重试”。步骤安全地回到 pending/ready；
                    # 重试是新 attempt，本条已冻结的拒绝事实不被改写。
                    info["state"] = "pending"
                    info["reason"] = str(
                        outcome.get("failure_class")
                        or "infrastructure_rejection"
                    )
                elif value in terminal_states:
                    info["state"] = terminal_states[value]
                    info["reason"] = str(
                        outcome.get("failure_class") or value
                    )
                else:
                    info["state"] = "event_conflict"
                    info["reason"] = "unknown_route_step_outcome"
                    invalid_history = True
        elif (
            reused := _reused_exact_output_correction(
                recovery_lineage, step, run_id)
        ) is not None:
            recovered_external = reused.get("external_execution_reused") is True
            recovered_local = reused.get("local_execution_reused") is True
            info.update({
                "state": "verified",
                "outcome": "success",
                "attempt_id": reused.get("attempt_id"),
                "recovered_from_external_attempt": recovered_external,
                "recovered_from_local_attempt": recovered_local,
            })
            if recovered_external:
                info["external_outcome"] = "success_verified"
            if recovered_local:
                limitation = _local_correction_limitation(reused)
                if limitation is not None:
                    correction_witness_limitations.append(limitation)
        if (
            info.get("state") == "failed"
            and info.get("reason") == "expected_outputs_missing"
            and str(info.get("attempt_id") or "")
        ):
            correction_facts = _expected_output_correction_facts(
                events, str(info["attempt_id"]), step_id,
            )
            info["expected_output_correction"] = dict(
                correction_facts["guidance"])
        derived[step_id] = info

    ready_step_ids = [
        step["id"]
        for step in steps
        if derived[step["id"]]["state"] == "pending"
        and all(
            derived.get(dependency, {}).get("state") == "verified"
            for dependency in step["after"]
        )
    ]
    states = {info["state"] for info in derived.values()}
    if invalid_history:
        route_state = "invalid_event_history"
        ready_step_ids = []
    elif states == {"verified"}:
        route_state = "complete"
    elif "interrupted" in states:
        route_state = "interrupted"
    elif states.intersection({"failed", "blocked", "cancelled", "unverified"}):
        route_state = "blocked"
    elif "in_progress" in states:
        route_state = "in_progress"
    else:
        route_state = "actionable"
    return {
        **loaded,
        "route_state": route_state,
        "steps": derived,
        "ready_step_ids": ready_step_ids,
        "correction_witness_limitations": correction_witness_limitations,
        "event_history_error": blocking_history_warning,
        "transcript_warnings": list(dict.fromkeys([
            *transcript_warnings,
            *history_warnings,
        ])),
    }


def _current_blocking_event_history_warning(state: Any) -> str | None:
    """在执行边界重新读取路线事件历史，覆盖无路线和检查后竞态。"""
    _events, transcript_warnings = _read_transcript_events(state)
    warning = _blocking_event_history_warning(transcript_warnings)
    if warning is not None:
        return warning
    try:
        snapshot = build_route_snapshot(state)
    except Exception:
        return "route_history_audit_failed"
    return _blocking_event_history_warning(
        snapshot.get("transcript_warnings") or [],
    )


def _normalized_program(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if text.startswith("legacy:") or text.startswith("compound:"):
        return text
    text = text.replace("\\", "/")
    if "/" not in text:
        return text
    normalized = os.path.normpath(text).replace("\\", "/")
    # ``./compile`` 表示项目内明确入口，不等同于 PATH 上任意 ``compile``。
    if text.startswith("./") and not normalized.startswith(("./", "../", "/")):
        return "./" + normalized
    return normalized


def _has_explicit_program_path(value: Any) -> bool:
    return "/" in str(value or "").replace("\\", "/")


def _normalized_program_sequence(value: Any) -> tuple[str, ...] | None:
    """Normalize a structured static compound entry sequence.

    A joined ``compound:a+b`` token is not a safe identity because executable
    paths may contain ``+``.  Keep the sequence structured and defensively
    validate it here as resolver callers need not pass provider validation.
    """
    if not isinstance(value, (list, tuple)) or len(value) < 2 or len(value) > _MAX_PROGRAM_SEQUENCE:
        return None
    normalized: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            return None
        program = _normalized_program(item)
        if (
            not program
            or re.search(r"\s", program)
            or program.startswith(("compound:", "legacy:"))
        ):
            return None
        normalized.append(program)
    return tuple(normalized)


def _tool_program_matches_step(action: dict[str, Any], step: dict[str, Any]) -> bool:
    declared = step.get("action") or {}
    if str(action.get("tool") or "") != str(declared.get("tool") or ""):
        return False
    declared_sequence = _normalized_program_sequence(
        declared.get("program_sequence"))
    if declared_sequence is not None:
        if _normalized_program(declared.get("program")) != declared_sequence[0]:
            return False
        observed_sequence = _normalized_program_sequence(
            action.get("program_sequence"))
        if observed_sequence is None:
            return False
        return (
            observed_sequence == declared_sequence
            and _normalized_program(action.get("program"))
            == observed_sequence[0]
        )
    observed_program = action.get("program")
    declared_program = declared.get("program")
    if _has_explicit_program_path(declared_program):
        return (
            _has_explicit_program_path(observed_program)
            and _normalized_program(observed_program)
            == _normalized_program(declared_program)
        )
    return (
        _normalized_program(observed_program).rsplit("/", 1)[-1]
        == _normalized_program(declared_program)
    )


def _action_matches_step(action: dict[str, Any], step: dict[str, Any]) -> bool:
    requested_step_id = str(action.get("route_step_id") or "").strip()
    if requested_step_id and requested_step_id != str(step.get("id") or ""):
        return False
    # Python 的机械入口始终只是 ``python``，无法像 Bash/submit 一样由程序名
    # 区分两个步骤。要求调用方显式绑定 step id，避免一条无关的诊断代码误把
    # 空产物 Python 步骤标成完成；未绑定的低风险 Python 仍可按既有守卫执行，
    # 但不产生路线收据。
    if str(action.get("tool") or "") == "safe_execute_python" and not requested_step_id:
        return False
    return _tool_program_matches_step(action, step)


def _binding_mismatch_detail(
    action: dict[str, Any],
    step: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    """返回有界入口身份；不复制参数或完整命令文本。"""
    declared_action = step.get("action") or {}
    declared = {
        "tool": str(declared_action.get("tool") or ""),
        "program": _normalized_program(declared_action.get("program")),
    }
    observed = {
        "tool": str(action.get("tool") or ""),
        "program": _normalized_program(action.get("program")),
    }
    declared_sequence = _normalized_program_sequence(
        declared_action.get("program_sequence"))
    observed_sequence = _normalized_program_sequence(
        action.get("program_sequence"))
    if declared_sequence is not None:
        declared["program_sequence"] = list(declared_sequence)
        observed["program_sequence"] = (
            list(observed_sequence) if observed_sequence is not None else None
        )
        kind = "compound_sequence_mismatch"
        if observed_sequence is None:
            # 本次提交根本不是静态复合命令 —— 说"不要增删重排入口"是误导：
            # 调用方没有重排任何东西，它是把 program_sequence 当成了 argv。
            # 声明期查不出这一点（路线里没有命令文本），所以这条提示是唯一
            # 能纠正概念的地方。2026-09-02 真实 E2E：节点据此得出「submit_job
            # 不接受 program_sequence 参数」这个错误结论才绕开。
            hint = (
                "本次提交的命令只有一个执行入口，不是静态复合命令，因此没有 "
                "program_sequence。program_sequence 描述的是 `a && b && c` 这类"
                "**多入口** payload 里各个可执行入口的顺序，不是单个程序的参数表"
                "（argv）—— `python3 x.py 1 2` 只有 python3 一个入口。"
                "单入口步骤请只声明 action.program，删掉 program_sequence。"
            )
        else:
            hint = (
                "静态复合 submit_job 必须与冻结的 program_sequence 完全一致；"
                "不要增删、重排入口，也不要改用脚本、管道或条件分支。"
            )
    elif observed["program"].startswith("compound:"):
        kind = "compound_action"
        hint = (
            "action.program 只填单个可执行入口；每个高后果步骤单独调用，"
            "不要追加 &&、; 或管道命令。"
        )
    elif declared["tool"] != observed["tool"]:
        kind = "tool_mismatch"
        hint = "使用冻结步骤声明的受管工具，或基于新证据修订同一条路线。"
    elif _has_explicit_program_path(declared["program"]):
        kind = "explicit_path_mismatch"
        hint = "显式相对/绝对入口必须逐字匹配，不能退化成 PATH 中的同名程序。"
    else:
        kind = "program_mismatch"
        hint = "使用冻结步骤声明的单个入口，参数只放在本次受管工具调用中。"
    return kind, {
        "declared": declared,
        "observed": observed,
        "hint": hint,
    }


def _policy_for_effects(effects: set[str], *, read_only: bool) -> str:
    if read_only:
        return "read_only"
    if "scientific_execution" in effects:
        return "formal_scientific_execution"
    if "external_job" in effects:
        return "managed_external_job"
    if effects.intersection({
        "environment_change", "managed_lifecycle", "process_tree",
    }):
        return "guarded_process"
    if effects.difference(ROUTE_EFFECTS):
        return "guarded_unknown_effect"
    return "low_risk_effectful"


def resolve_execution_action(
    snapshot: dict[str, Any],
    action: dict[str, Any],
) -> dict[str, Any]:
    """纯函数：把一个机械观察到的动作匹配到当前路线步骤。"""
    observed_effects = {
        str(item) for item in (action.get("observed_effects") or [])
        if str(item).strip()
    }
    # 机械副作用事实高于调用方/分类器的 read_only 标签；两者矛盾时必须按
    # effectful 解析，不能让一个误分类直接绕过路线。
    read_only = action.get("read_only") is True and not observed_effects
    base = {
        "tool": str(action.get("tool") or ""),
        "program": _normalized_program(action.get("program")),
        "requested_route_step_id": str(action.get("route_step_id") or "").strip(),
        "read_only": read_only,
        "dry_run": action.get("dry_run") is True,
        "observed_effects": sorted(observed_effects),
    }
    if read_only:
        return {
            **base,
            "decision": "route_not_required",
            "policy": "read_only",
            "effective_effects": sorted(observed_effects),
        }
    if snapshot.get("route_state") == "invalid_event_history":
        warnings = list(snapshot.get("transcript_warnings") or [])
        history_warning = (
            snapshot.get("event_history_error")
            or _blocking_event_history_warning(warnings)
            or "invalid_event_history"
        )
        return {
            **base,
            "decision": "invalid_event_history",
            "reason": history_warning,
            "history_warning": history_warning,
            "history_warnings": warnings,
            "policy": _policy_for_effects(observed_effects, read_only=False),
            "effective_effects": sorted(observed_effects),
        }
    if snapshot.get("status") != "ready":
        return {
            **base,
            "decision": "route_unavailable",
            "reason": snapshot.get("reason") or snapshot.get("status"),
            "policy": _policy_for_effects(observed_effects, read_only=False),
            "effective_effects": sorted(observed_effects),
        }

    route_steps = snapshot["route"]["steps"]
    requested_step_id = str(action.get("route_step_id") or "").strip()
    requested_steps = [
        step for step in route_steps if step["id"] == requested_step_id
    ] if requested_step_id else []
    if requested_step_id and (
        len(requested_steps) != 1
        or not _tool_program_matches_step(action, requested_steps[0])
    ):
        declared_effects = {
            str(item)
            for step in requested_steps
            for item in (step.get("effects") or [])
        }
        effective = observed_effects | declared_effects
        result = {
            **base,
            "decision": "route_step_binding_mismatch",
            "reason": (
                "unknown_route_step_id" if not requested_steps
                else "requested_step_action_mismatch"
            ),
            "route_step_id": requested_step_id,
            "route_ref": snapshot["route_ref"],
            "policy": _policy_for_effects(effective, read_only=False),
            "effective_effects": sorted(effective),
        }
        if requested_steps:
            mismatch_kind, mismatch = _binding_mismatch_detail(
                action, requested_steps[0]
            )
            result["mismatch_kind"] = mismatch_kind
            result["binding_mismatch"] = mismatch
        return result
    ready = set(snapshot.get("ready_step_ids") or [])
    # A verified Python step is immutable history, not a candidate for a new
    # *unbound Python diagnostic*.  Keep every unresolved Python state
    # (pending, failed, interrupted, ...) in the candidate set so a blocked
    # route cannot fall through to an unbound low-risk execution.  This
    # exception is deliberately Python-only: task 040 does not change Bash or
    # other tools step matching, where verified repeated entrypoints still
    # require an explicit binding.  An explicit route_step_id also resolves
    # against the full route below, including verified history.
    unresolved_steps = [
        step
        for step in route_steps
        if str(
            (snapshot.get("steps") or {}).get(step["id"], {}).get("state")
            or ""
        ) != "verified"
    ]
    unbound_candidate_steps = (
        unresolved_steps
        if str(action.get("tool") or "") == "safe_execute_python"
        else route_steps
    )
    same_entry_steps = [
        step
        for step in unbound_candidate_steps
        if _tool_program_matches_step(action, step)
    ]
    if not requested_step_id and len(same_entry_steps) > 1:
        return {
            **base,
            "decision": "route_step_ambiguous",
            "reason": "route_step_id_required_for_repeated_entrypoint",
            "candidate_step_ids": [step["id"] for step in same_entry_steps],
            "policy": _policy_for_effects(observed_effects, read_only=False),
            "effective_effects": sorted(observed_effects),
        }
    ready_python_steps = [
        step for step in route_steps
        if step["id"] in ready
        and str((step.get("action") or {}).get("tool") or "")
        == "safe_execute_python"
    ]
    if (
        str(action.get("tool") or "") == "safe_execute_python"
        and not requested_step_id
        and ready_python_steps
    ):
        effective = observed_effects | {
            str(item)
            for step in ready_python_steps
            for item in (step.get("effects") or [])
        }
        return {
            **base,
            "decision": "route_step_id_required",
            "reason": "python_route_step_requires_explicit_id",
            "candidate_step_ids": [step["id"] for step in ready_python_steps],
            "route_ref": snapshot["route_ref"],
            "policy": _policy_for_effects(effective, read_only=False),
            "effective_effects": sorted(effective),
        }
    ready_matches = [
        step for step in route_steps
        if step["id"] in ready and _action_matches_step(action, step)
    ]
    if len(ready_matches) > 1:
        return {
            **base,
            "decision": "route_step_ambiguous",
            "candidate_step_ids": [step["id"] for step in ready_matches],
            "policy": _policy_for_effects(observed_effects, read_only=False),
            "effective_effects": sorted(observed_effects),
        }
    if len(ready_matches) == 1:
        step = ready_matches[0]
        declared_effects = {str(item) for item in step.get("effects") or []}
        effective = observed_effects | declared_effects
        workdir_roles = {
            str(item) for item in (action.get("workdir_roles") or [])
        }
        decision = (
            "matched_ready_step"
            if snapshot.get("source_format") == "v2"
            else "matched_legacy_step"
        )
        envelope_refs = execution_envelope_refs_for_action(step.get("action"))
        return {
            **base,
            "decision": decision,
            "authoritative": snapshot.get("source_format") == "v2",
            "route_ref": snapshot["route_ref"],
            "route_step_id": step["id"],
            "step_definition_hash": step_definition_hash(step),
            "step_execution_contract_hash": step_execution_contract_hash(step),
            "declared_workdir_role": step["workdir_role"],
            "expected_outputs": list(step.get("expected_outputs") or []),
            "workdir_role_observed": step["workdir_role"] in workdir_roles,
            **({"execution_envelope_ref": envelope_refs[0]}
               if len(envelope_refs) == 1 else {}),
            "effective_effects": sorted(effective),
            "policy": _policy_for_effects(effective, read_only=False),
        }

    nonready_candidates = (
        route_steps if requested_step_id else unbound_candidate_steps
    )
    nonready_matches = [
        step for step in nonready_candidates if _action_matches_step(action, step)
    ]
    if len(nonready_matches) == 1:
        step = nonready_matches[0]
        info = snapshot["steps"][step["id"]]
        if info["state"] == "interrupted":
            return {
                **base,
                "decision": "reconcile_interrupted_attempt",
                "route_ref": snapshot["route_ref"],
                "route_step_id": step["id"],
                "attempt_id": info.get("attempt_id"),
                "step_state": info.get("state"),
                "step_reason": info.get("reason"),
                "policy": _policy_for_effects(
                    observed_effects | set(step.get("effects") or []),
                    read_only=False,
                ),
                "effective_effects": sorted(
                    observed_effects | set(step.get("effects") or [])
                ),
            }
        return {
            **base,
            "decision": "route_step_not_ready",
            "route_ref": snapshot["route_ref"],
            "route_step_id": step["id"],
            "attempt_id": info.get("attempt_id"),
            "step_state": info["state"],
            "step_reason": info.get("reason"),
            **({"expected_output_correction": deepcopy(
                info["expected_output_correction"])}
               if isinstance(info.get("expected_output_correction"), dict)
               else {}),
            # 重开这一步的机械条件是它的定义哈希要变；把当前值与整体路线态一起
            # 带出去，模型才知道自己要把哪个数改掉、以及别的步骤还能不能动。
            "step_definition_hash": info.get("definition_hash"),
            "route_state": snapshot.get("route_state"),
            "ready_step_ids": list(snapshot.get("ready_step_ids") or []),
            "dependencies": info["after"],
            "policy": _policy_for_effects(
                observed_effects | set(step.get("effects") or []),
                read_only=False,
            ),
            "effective_effects": sorted(
                observed_effects | set(step.get("effects") or [])
            ),
        }
    return {
        **base,
        "decision": "route_action_mismatch",
        "ready_step_ids": list(snapshot.get("ready_step_ids") or []),
        "route_ref": snapshot["route_ref"],
        "policy": _policy_for_effects(observed_effects, read_only=False),
        "effective_effects": sorted(observed_effects),
    }


def _action_signature(action: dict[str, Any]) -> str:
    bounded = {
        "tool": str(action.get("tool") or ""),
        "program": _normalized_program(action.get("program")),
        "program_sequence": list(
            _normalized_program_sequence(action.get("program_sequence")) or ()
        ),
        "route_step_id": str(action.get("route_step_id") or "").strip(),
        "read_only": action.get("read_only") is True,
        "observed_effects": sorted({
            str(item) for item in (action.get("observed_effects") or [])
            if str(item).strip()
        }),
        "workdir_roles": sorted({
            str(item) for item in (action.get("workdir_roles") or [])
            if str(item).strip()
        }),
        "dry_run": action.get("dry_run") is True,
    }
    return _content_hash(json.dumps(
        bounded,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ))


def _execution_scope_projection(
    state: Any,
    *,
    required: bool,
) -> dict[str, Any]:
    mode_view = load_execution_mode_view(state)
    mode = str(mode_view.get("mode") or "")
    status = str(mode_view.get("status") or "absent")
    try:
        try:
            from .run_contract import audit_execution_intent_binding
        except ImportError:  # pragma: no cover - standalone node bootstrap.
            from tools.run_contract import audit_execution_intent_binding
        intent_binding = audit_execution_intent_binding(state, require=required)
    except Exception as exc:
        # A real write/launch must not continue when its immutable upstream
        # input receipt cannot be checked.  Read-only and dry-run calls keep
        # their existing diagnostic path.
        intent_binding = {
            "passed": not required,
            "applicable": bool(required),
            "status": "binding_audit_error",
            "reason": f"execution intent binding audit failed: {type(exc).__name__}",
        }
    projection: dict[str, Any] = {
        "scope_required": required,
        "scope_status": status,
        "scope_mode": mode or None,
        "scope_source": mode_view.get("source"),
        "intent_binding": intent_binding,
    }
    if required and status != "classified":
        projection["required_tool"] = "classify_experiment_scope"
    return projection


def resolve_execution_context(state: Any, action: dict[str, Any]) -> dict[str, Any]:
    """读取一次客观快照并调用纯 resolver；scope 只做正交投影。"""
    observed_effects = {
        str(item) for item in (action.get("observed_effects") or [])
        if str(item).strip()
    }
    read_only = action.get("read_only") is True and not observed_effects
    dry_run = action.get("dry_run") is True
    fallback_policy = _policy_for_effects(
        observed_effects, read_only=read_only)
    scope_projection = _execution_scope_projection(
        state, required=not read_only and not dry_run)
    try:
        snapshot = (
            {"status": "unavailable"}
            if read_only
            else build_route_snapshot(state)
        )
        decision = dict(resolve_execution_action(snapshot, action))
        if decision.get("execution_envelope_ref"):
            try:
                decision = refresh_execution_envelope_binding(state, decision)
            except Exception as exc:
                decision = {
                    **decision,
                    "execution_envelope_status": "invalid",
                    "execution_envelope_reason": "execution_envelope_resolution_failed",
                    "execution_envelope_error": f"{type(exc).__name__}: {exc}",
                }
        if decision.get("authoritative") is True:
            decision["route_intent_binding"] = _route_execution_intent_binding(
                state, snapshot, require=bool(scope_projection.get("scope_required"))
            )
        policy = str(decision.get("policy") or fallback_policy)
        if (
            scope_projection.get("scope_required") is True
            and scope_projection.get("scope_mode") == "operational"
            and policy == "formal_scientific_execution"
        ):
            scope_projection = {
                **scope_projection,
                "scope_status": "incompatible",
            }
        return {**decision, **scope_projection}
    except Exception as exc:
        # resolver 自身不可用时保留机械 effects 与 scope 投影。真实执行会在
        # pre-materialization 阶段统一 fail-closed；只读与 dry-run 仍可诊断。
        return {
            "decision": "resolver_error",
            "reason": type(exc).__name__,
            "tool": str(action.get("tool") or ""),
            "policy": fallback_policy,
            "read_only": read_only,
            "dry_run": dry_run,
            "observed_effects": sorted(observed_effects),
            "effective_effects": sorted(observed_effects),
            **scope_projection,
        }


_HIGH_CONSEQUENCE_POLICIES = frozenset({
    "guarded_process",
    "guarded_unknown_effect",
    "managed_external_job",
    "formal_scientific_execution",
})


#: 从未被尝试过的步骤状态。它们不是失败：没有根因可诊断，也没有定义要改。
_NEVER_ATTEMPTED_STEP_STATES = frozenset({"pending", "ready"})


def _step_not_ready_message(decision: dict[str, Any]) -> str:
    """还轮不到执行的步骤，给的必须是「怎么往下走」，不是「怎么重开失败步骤」。

    2026-09-10 两份活体实测：step_state=pending 时照发重开失败步骤那套指引（先诊断根因、
    改 step_definition_hash、补 remediation_refs）。可这一步从未失败——模型照着做只能
    白转一圈，还会把一条本来好好的路线改出问题。真实原因通常只有两个：前置步骤没核验完，
    或者这根本不是当前该做的那一步。
    """
    state_name = str(decision.get("step_state") or "")
    ready = [str(item) for item in (decision.get("ready_step_ids") or [])]
    deps = [str(item) for item in (decision.get("dependencies") or [])]
    parts = [
        f"该路线步骤还轮不到执行（step_state={state_name}）。它从未失败过，"
        "所以这里**不是**失败恢复：不用先做诊断，也不用修订这一步本身。"
    ]
    parts.append(
        f"它声明的前置步骤 {deps} 还没有全部核验通过。" if deps
        else "它没有声明前置步骤，所以挡住它的不是依赖，而是它不是当前该执行的那一步。"
    )
    parts.append(
        f"现在可以直接执行的步骤：{ready}。先做它们；"
        "如果本步骤的 after 写了并不真实存在的依赖，就修订 after。"
        if ready else
        f"当前没有任何可执行步骤（route_state={decision.get('route_state')}）："
        "整条路线都在等前置或等修订，先读 route_state 再决定修订哪一步。"
    )
    return "".join(parts)


def _genuine_step_reopen_message(decision: dict[str, Any]) -> str:
    """Generic recovery guidance for a genuine failed attempt."""
    detail = (
        f"（step_state={decision.get('step_state')}"
        + (f", reason={decision.get('step_reason')}"
           if decision.get("step_reason") else "")
        + "）" if decision.get("step_state") else ""
    )
    return (
        "该路线步骤当前不可执行"
        + detail
        + "。重开它需要同时满足两条：一、先做只读诊断分类根因，把失败后"
        "新建的证据 artifact 同时写进 route.evidence_refs 与 "
        "recovery_basis.evidence_refs；二、**该步骤自身的定义必须改变**"
        "（step_definition_hash 变化），只改 route.goal 或 evidence_refs "
        "不算——步骤不变就仍绑在旧 attempt 上。若重提的 action payload "
        "与上次逐字相同，还须提供 remediation_refs。"
    )


def _expected_output_step_message(decision: dict[str, Any]) -> str:
    """Describe only correction paths admitted by the shared receipt facts."""
    correction = decision.get("expected_output_correction")
    correction = correction if isinstance(correction, dict) else {}
    mode = str(correction.get("mode") or "not_applicable")
    if (
        correction.get("local_pure_correction_available") is True
        and mode == "validated_local_attempt_receipt_no_reexecution"
    ):
        return (
            "该本地步骤已成功执行，失败仅因 expected_outputs_missing。"
            "合法出路是带 amendment_reason 修订同一条冻结路线：只改变该步骤的 "
            "expected_outputs，使其指向原 attempt 时间窗内真实写下、能通过本地 "
            "witness 的具体文件；设置 recovery_basis.evidence_refs=[]，"
            "路线其余字段保持不变。修订后复用原 attempt 并重新核验，不重新执行。"
        )
    if mode == "validated_external_attempt_receipt_no_resubmit":
        return (
            "本地纯纠正不适用：该 attempt 是 managed_external_job。"
            "不要重新提交作业；带 amendment_reason 修订同一条冻结路线，"
            "recovery_basis 绑定原 attempt 且 failure_class=expected_output，"
            "整份路线只改变该步骤 expected_outputs。不得清空已报缺失的声明；"
            "只可改指向该 attempt 提交时登记且通过外部 witness 的同名文件。"
            "若不复用该 attempt，可保留 step id、声明新的非空 expected_outputs，"
            "提供新诊断/修复证据后重跑；此时 recovery_basis.failure_class "
            "必须填写证据支持的真实根因（如 parameter、environment、execution "
            "或 source），不得继续填写 expected_output。若无法安全重跑，则调用 "
            "report_blocker "
            "并以 operation_blocked 收尾。"
        )

    reason = str(correction.get("reason") or "shared_receipt_facts_missing")
    why = {
        "unique_step_binding_missing": (
            "事件历史没有唯一绑定到本步骤的 attempt 收据"
        ),
        "valid_local_success_receipt_missing": (
            "事件历史没有通过门判据的有效的本地成功收据"
            "（包括 managed status=success、returncode=0/None）"
        ),
        "valid_external_success_receipt_missing": (
            "受管外部 attempt 没有通过门判据的 terminal success_verified 收据"
        ),
        "receipt_source_not_local": (
            "成功收据不是可复用的本地 route_step_outcome"
        ),
        "shared_receipt_facts_missing": (
            "当前 resolver 没有取得门所用的 exact-output 收据事实"
        ),
    }.get(reason, f"门判据返回 {reason}")
    return (
        f"本地纯纠正不适用：{why}。不能把旧 attempt 当作成功执行来复用；"
        + _genuine_step_reopen_message(decision)
    )


def _route_step_not_ready_message(decision: dict[str, Any]) -> str:
    state_name = str(decision.get("step_state") or "")
    if state_name in _NEVER_ATTEMPTED_STEP_STATES:
        return _step_not_ready_message(decision)
    if state_name == "verified":
        return (
            "该路线步骤已经成功并核验（step_state=verified），原 attempt 不应重跑。"
            "若任务确实需要再次执行，请在路线中声明目标和身份明确的新的步骤。"
        )
    if (
        state_name == "failed"
        and decision.get("step_reason") == "expected_outputs_missing"
    ):
        return _expected_output_step_message(decision)
    return _genuine_step_reopen_message(decision)


def _route_step_not_ready_action(decision: dict[str, Any]) -> str:
    state_name = str(decision.get("step_state") or "")
    if state_name in _NEVER_ATTEMPTED_STEP_STATES:
        return "run_ready_step_or_fix_dependencies"
    if state_name == "verified":
        return "declare_new_step"
    correction = decision.get("expected_output_correction")
    mode = str(
        correction.get("mode") if isinstance(correction, dict) else "")
    if mode == "validated_local_attempt_receipt_no_reexecution":
        return "correct_expected_outputs_without_reexecution"
    if mode == "validated_external_attempt_receipt_no_resubmit":
        return "correct_external_expected_outputs_without_resubmit"
    return "diagnose_then_amend_route"


def _route_block_payload(
    decision: dict[str, Any],
    *,
    reason: str,
    message: str,
    suggested_owner: str,
    node_action: str,
) -> dict[str, Any]:
    correction = decision.get("expected_output_correction")
    correction_mode = str(
        correction.get("mode") if isinstance(correction, dict) else "")
    definition_reopen_applicable = bool(
        str(decision.get("step_state") or "")
        not in {*_NEVER_ATTEMPTED_STEP_STATES, "verified"}
        and correction_mode not in {
            "validated_local_attempt_receipt_no_reexecution",
            "validated_external_attempt_receipt_no_resubmit",
        }
    )
    details = {
        key: deepcopy(decision[key])
        for key in (
            "binding_mismatch", "mismatch_kind", "candidate_step_ids",
            "declared_workdir_role", "step_state", "step_reason",
            "required_tool", "required_effect", "contract_code",
            "scope_status", "scope_mode", "history_warning", "history_warnings",
            "execution_envelope_ref", "execution_envelope_reason",
            "execution_envelope_error",
            "intent_binding", "route_intent_binding",
            "expected_output_correction",
            # 重开一步的真实谓词与当前路线态：不给这三样，模型只知道"不可执行"，
            # 不知道差什么。step_definition_hash 是重开的机械条件本身。
            "step_definition_hash", "route_state", "ready_step_ids",
        )
        if key in decision
        and (
            key != "step_definition_hash"
            or definition_reopen_applicable
        )
    }
    blocker = {
        "kind": reason,
        "suggested_owner": suggested_owner,
        "node_action": node_action,
        "retryable_after_change": True,
        "resolver_decision": decision.get("decision"),
        "resolver_reason": decision.get("reason"),
        "route_step_id": decision.get("route_step_id"),
        "attempt_id": decision.get("attempt_id"),
        **details,
    }
    return {
        "status": "error",
        "reason": reason,
        "error": message,
        "route_blocked": True,
        "blocker": blocker,
        **details,
    }


def _pre_materialization_block(
    decision: dict[str, Any],
) -> dict[str, Any] | None:
    """在任何目录物化、预检落盘或进程创建前检查正交执行契约。"""
    kind = str(decision.get("decision") or "resolver_error")
    policy = str(decision.get("policy") or "guarded_unknown_effect")
    effects = {
        str(item) for item in (
            decision.get("effective_effects")
            or decision.get("observed_effects")
            or []
        )
    }
    if decision.get("dry_run") is True:
        return None
    if (
        (decision.get("read_only") is True or policy == "read_only")
        and not effects
    ):
        return None
    if kind == "invalid_event_history":
        history_warning = str(
            decision.get("history_warning") or decision.get("reason") or ""
        )
        error_code = _event_history_error_code(history_warning)
        if str(decision.get("tool") or "") == "safe_execute_python":
            message = (
                "transcript 尾部存在未完成 JSONL，继续追加会把步骤开始/结果收据"
                "写进损坏行；真实执行已在物化和启动前拒绝。safe_execute_python "
                "在此状态下也会被拒；若只需只读诊断，请改用 safe_run_bash；"
                "请由 framework owner 对账并修复追加日志。"
                if error_code == "route_transcript_tail_unwritable" else
                "路线事件历史不可读取或存在身份冲突；真实执行已在物化和启动前"
                "拒绝。safe_execute_python 在此状态下也会被拒；若只需只读诊断，"
                "请改用 safe_run_bash；请由 framework owner 对账。"
            )
        else:
            message = (
                "transcript 尾部存在未完成 JSONL，继续追加会把步骤开始/结果收据"
                "写进损坏行；真实执行已在物化和启动前拒绝。只读诊断仍可继续，"
                "请由 framework owner 对账并修复追加日志。"
                if error_code == "route_transcript_tail_unwritable" else
                "路线事件历史不可读取或存在身份冲突；真实执行已在物化和启动前"
                "拒绝。只读诊断仍可继续，请由 framework owner 对账。"
            )
        return _route_block_payload(
            decision,
            reason=error_code,
            message=message,
            suggested_owner="framework",
            node_action="reconcile_route_event_history_before_execution",
        )
    if kind == "resolver_error":
        tool = str(decision.get("tool") or "当前工具")
        return _route_block_payload(
            decision,
            reason="execution_route_resolver_failed",
            message=(
                f"路线解析器异常，{tool} 的本次调用已在目录物化和启动前拒绝；"
                "该异常路径同时阻断 safe_execute_python 与 safe_run_bash，"
                "不要切换工具或重复尝试。请调用 report_blocker，交由 framework "
                "owner 修复。"
            ),
            suggested_owner="framework",
            node_action="report_blocker",
        )
    if (
        decision.get("scope_required") is True
        and decision.get("scope_mode") == "operational"
        and (
            decision.get("scope_status") == "incompatible"
            or policy == "formal_scientific_execution"
        )
    ):
        return _route_block_payload(
            decision,
            reason="execution_scope_route_mismatch",
            message=(
                "当前 run 已不可变地分类为 operation，但冻结路线把本次动作声明为"
                "正式 scientific_execution；未执行。请基于同一 scope 修订路线，"
                "不能靠 stage 或声明顺序绕过科学契约。"
            ),
            suggested_owner="experiment",
            node_action="amend_route_to_match_immutable_scope",
        )
    if (
        decision.get("scope_required") is True
        and decision.get("scope_status") != "classified"
    ):
        read_only_hint = (
            "如果只是只读诊断，请改用 safe_run_bash。"
            if str(decision.get("tool") or "") == "safe_execute_python"
            else ""
        )
        return _route_block_payload(
            decision,
            reason="experiment_scope_classification_required",
            message=(
                "所有真实执行动作必须先调用 classify_experiment_scope，确定本 run "
                "是 operation 还是 scientific；本次动作未物化目录、未执行。"
                "该分类会决定 prereg、科学门和最终闭环，不能在执行后补填。"
                + read_only_hint
            ),
            suggested_owner="experiment",
            node_action="classify_experiment_scope_before_execution",
        )
    intent_binding = decision.get("intent_binding")
    if (
        decision.get("scope_required") is True
        and isinstance(intent_binding, dict)
        and not intent_binding.get("passed", False)
    ):
        binding_status = str(intent_binding.get("status") or "binding_required")
        reason = {
            "intent_changed": "experiment_execution_intent_changed",
            "prereg_binding_changed": "experiment_bound_prereg_changed",
            "bound_prereg_unavailable": "experiment_bound_prereg_unavailable",
        }.get(binding_status, "experiment_execution_intent_binding_required")
        tool = str(decision.get("tool") or "当前工具")
        diagnostic_hint = (
            "safe_execute_python 的本次真实动作已拒绝；如需做不改变状态的诊断，"
            "请改用 safe_run_bash 的精确只读命令。"
            if tool == "safe_execute_python" else
            f"{tool} 的本次真实动作已拒绝；只有经机械判定为只读的 "
            "safe_run_bash 诊断仍可使用。"
        )
        return _route_block_payload(
            decision,
            reason=reason,
            message=(
                "上游任务输入或其冻结 prereg 绑定已漂移/不可核验；本次真实动作在目录、"
                "脚本和进程产生前拒绝。" + diagnostic_hint + "不得把原任务"
                "替换为 proxy、简化方法或新研究问题。请恢复原输入，或由上游重派新 run。"
            ),
            suggested_owner="upstream",
            node_action="restore_bound_inputs_or_start_new_run",
        )
    route_intent_binding = decision.get("route_intent_binding")
    if (
        decision.get("scope_required") is True
        and decision.get("authoritative") is True
        and isinstance(route_intent_binding, dict)
        and not route_intent_binding.get("passed", False)
    ):
        receipt_status = str(route_intent_binding.get("status") or "binding_required")
        reason = {
            "receipt_missing": "route_execution_intent_binding_missing",
            "receipt_changed": "route_execution_intent_binding_changed",
        }.get(receipt_status, "route_execution_intent_binding_required")
        return _route_block_payload(
            decision,
            reason=reason,
            message=(
                "当前真实动作匹配的冻结 declared_route 没有绑定本 run 的上游输入收据，"
                "或其收据已与当前 immutable scope 不同；目录、脚本和进程均未产生。"
                "先在同一上游输入下重新冻结路线；若上游目标已变，必须新 run。"
            ),
            suggested_owner="experiment",
            node_action="redeclare_route_with_current_intent_or_start_new_run",
        )
    return None


def execution_route_block(
    decision: dict[str, Any] | None,
    *,
    phase: str = "pre_spawn",
) -> dict[str, Any] | None:
    """按后果分级决定路线问题是否阻断；不读取 state，便于机械验证。

    只读探查保持无分类快速路径；所有真实执行先完成一次 run scope 分类，
    没有路线的低风险可逆写入在既有边界内保持流畅（P0a v2：它们进 census 记为
    incidental——可见、入账，既不满足也不毒化真实执行义务）。会派生进程树、提交外部
    作业或执行正式科学任务的动作必须精确绑定冻结 v2 路线。只要动作已经
    匹配某一步，错误 cwd、失败后重试、歧义和中断未对账就不再降级放行。
    """
    if not isinstance(decision, dict):
        decision = {"decision": "resolver_error", "reason": "missing_decision"}
    if phase not in {"pre_materialization", "pre_spawn"}:
        raise ValueError(f"unsupported execution route phase: {phase}")
    pre_block = _pre_materialization_block(decision)
    if pre_block is not None:
        return pre_block
    kind = str(decision.get("decision") or "resolver_error")
    policy = str(decision.get("policy") or "guarded_unknown_effect")
    effective_effects = {
        str(item) for item in (
            decision.get("effective_effects")
            or decision.get("observed_effects")
            or []
        )
    }
    if policy == "read_only" and effective_effects:
        policy = _policy_for_effects(effective_effects, read_only=False)
    if ((decision.get("read_only") is True or policy == "read_only")
            and not effective_effects):
        return None
    # submit_job dry-run 只生成/检查脚本，不 spawn/submit，也不产生步骤收据；
    # 它继续经过路径、Bash AST 和危险命令检查，但不是路线授权动作。
    if decision.get("dry_run") is True:
        return None
    if decision.get("execution_envelope_status") == "invalid":
        return _route_block_payload(
            decision,
            reason="execution_envelope_binding_invalid",
            message=(
                "冻结 execution envelope 已不可精确解析或其支持证据已漂移；"
                "未物化目录、未启动进程。请基于新证据修订同一条 route。"
            ),
            suggested_owner="experiment",
            node_action="repair_or_replace_frozen_execution_envelope_then_amend_route",
        )
    ownership_violation = external_job_contract_violation(
        decision.get("tool"), effective_effects)
    if ownership_violation is not None:
        contract_decision = {**decision, **ownership_violation}
        lifecycle_required = (
            ownership_violation.get("contract_code")
            == "process_tree_requires_submit_job"
        )
        return _route_block_payload(
            contract_decision,
            reason=(
                "execution_route_managed_lifecycle_required"
                if lifecycle_required
                else "execution_route_external_job_owner_mismatch"
            ),
            message=ownership_violation["message"] + "；本次动作未执行。",
            suggested_owner="experiment",
            node_action="amend_route_and_use_submit_job",
        )
    # 二道防线：路线在 warn/off 档下声明后环境翻到 enforce，spawn 时刻仍要求
    # scientific_execution 已绑定 envelope。dry_run/read_only 探测在上方既有
    # 豁免中已放行，warn/off 档不在此拦截。
    if (
        phase == "pre_spawn"
        and kind == "matched_ready_step"
        and "scientific_execution" in effective_effects
        and not decision.get("execution_envelope_ref")
        and execution_envelope_gate_mode() == "enforce"
    ):
        return _route_block_payload(
            decision,
            reason="execution_envelope_required",
            message=(
                "enforce 档下 scientific_execution 必须在 spawn 前绑定冻结 "
                "execution envelope；本次动作未执行。纠偏序列：① "
                "declare_execution_envelope(route_step_id=<step id>, "
                "assurance_class=evidence_bearing, ...)；② 把返回的 "
                "evidence_ref 逐字放入该 step.action.evidence_refs；③ 带 "
                "amendment_reason 修订同一条冻结路线后重试。"
            ),
            suggested_owner="experiment",
            node_action="declare_execution_envelope_then_amend_route",
        )
    if (
        phase == "pre_materialization"
        and (
            kind == "matched_ready_step"
            or policy not in _HIGH_CONSEQUENCE_POLICIES
        )
    ):
        return None

    if kind == "matched_ready_step":
        if not decision.get("authoritative"):
            return _route_block_payload(
                decision,
                reason="execution_route_not_authoritative",
                message="当前动作只匹配到非权威路线视图；未执行。请声明并冻结 v2 路线。",
                suggested_owner="experiment",
                node_action="declare_or_amend_canonical_route",
            )
        if (decision.get("workdir_role_observed") is not True
                or decision.get("workdir_resolution_status")
                not in {"resolved", "explicit"}):
            return _route_block_payload(
                decision,
                reason="execution_route_workdir_mismatch",
                message=(
                    "本次工作目录不属于路线步骤声明的可写路径角色；未执行。"
                    "请修正 cwd/path role，不要靠命令内 cd 绕过。"
                ),
                suggested_owner="experiment",
                node_action="correct_workdir_or_path_role",
            )
        return None

    if kind in {"route_step_not_ready", "reconcile_interrupted_attempt"}:
        interrupted = kind == "reconcile_interrupted_attempt"
        return _route_block_payload(
            decision,
            reason=(
                "execution_route_attempt_reconciliation_required"
                if interrupted else "execution_route_step_not_ready"
            ),
            message=(
                "该路线步骤存在有始无终的尝试，必须先对账执行身份和客观状态，"
                "禁止直接重试。"
                if interrupted else
                _route_step_not_ready_message(decision)
            ),
            suggested_owner="experiment",
            node_action=(
                "reconcile_attempt_before_retry" if interrupted
                else _route_step_not_ready_action(decision)
            ),
        )

    if kind in {
        "route_step_ambiguous",
        "route_step_id_required",
        "route_step_binding_mismatch",
    }:
        reason = {
            "route_step_ambiguous": "execution_route_step_ambiguous",
            "route_step_id_required": "execution_route_step_id_required",
            "route_step_binding_mismatch": "execution_route_step_binding_mismatch",
        }[kind]
        mismatch = decision.get("binding_mismatch")
        detail = ""
        if isinstance(mismatch, dict):
            declared = mismatch.get("declared") or {}
            observed = mismatch.get("observed") or {}
            detail = (
                f"冻结入口={declared.get('tool')}:{declared.get('program')}；"
                f"观察入口={observed.get('tool')}:{observed.get('program')}。"
                f"{mismatch.get('hint') or ''}"
            )
        return _route_block_payload(
            decision,
            reason=reason,
            message=(
                "本次动作无法唯一、精确地绑定请求的路线步骤；未执行。"
                + (detail or "请提供正确 route_step_id，并保持入口与冻结步骤一致。")
            ),
            suggested_owner="experiment",
            node_action="correct_route_step_binding",
        )

    # 已完成 run scope 分类的低风险局部写入继续沿用既有路径/高危门并记录 advisory。
    # 它不产生 route 收据，也不能完成某个已声明步骤。
    if policy not in _HIGH_CONSEQUENCE_POLICIES:
        return None
    if kind == "matched_legacy_step":
        return _route_block_payload(
            decision,
            reason="execution_route_v2_required",
            message="旧 declared_route 仅供兼容读取，不能授权高后果执行；请声明 v2 路线。",
            suggested_owner="experiment",
            node_action="declare_canonical_v2_route",
        )
    return _route_block_payload(
        decision,
        reason="execution_route_required",
        message=(
            # 文案顺序是有意的：先说"要往下走该做什么"，再说豁免。原先整段先讲
            # 五行豁免规则，唯一可执行的那句"或声明/修订路线步骤"埋在最后，也不点
            # 工具名——2026-09-10 两份活体里模型都是撞了这堵墙才知道要先声明路线。
            "本次动作未执行：构建、主要运行、外部提交或正式科学执行必须先在冻结的 "
            "v2 路线里有一个与它精确对应的可执行步骤。"
            "**往下走**：调 `declare_execution_route` 声明一条路线，其中一个 step 的 "
            "action 要和本次调用对得上（按 tool、program、step 身份匹配；effects 只写"
            "可声明的那些——本次观察到但不在可声明集合里的效果，例如 "
            "unknown_executable，不必也不能写进 step，观察到的与声明的效果取并集只用于"
            "有效效果与风险判定，不参与匹配），"
            "然后重试本次调用并把那个 step 的 id 传进 route_step_id。"
            "只读调查不受此限制——若本次"
            "只是想核验入口是否存在/版本，优先用不依赖入口信任的兜底形式："
            "`ls -l <路径>` / `stat <路径>` / `file <路径>` / `which <裸名>`。"
            "直接跑 `<入口> --version` 也可免路线，但前提较严：参数须恰为单个 "
            "--version/--help/-V（不得带其他参数、重定向或管道），入口须是"
            "绝对路径或裸名（相对路径一律不免），且该入口不在本 run 可写根内"
            "（run 自己能写出来的程序不豁免）。前提不成立时不要重复重试同一"
            "形式——同签名连续拒绝会升级为 exhausted 阻断；改用上面的 "
            "`ls -l`/`stat` 兜底，或声明/修订路线步骤。"
        ),
        suggested_owner="experiment",
        node_action="investigate_then_declare_or_amend_route",
    )


_ROUTE_BLOCK_MAX_ATTEMPTS = 3


def _same_key_route_block_count(
    events: list[dict[str, Any]],
    *,
    route_step_id: str,
    action_signature: str,
    resolver_decision: Any,
) -> int:
    """按事件序统计既往同键 execution_route_blocked 次数。

    键为 (route_step_id 或无 step 时 action_signature, resolver_decision)；
    同一 step 出现 route_step_bound 后计数自然归零。不引入持久状态。
    """
    count = 0
    for event in events:
        name = event.get("event")
        if name == "route_step_bound":
            if route_step_id and event.get("route_step_id") == route_step_id:
                count = 0
            continue
        if name != "execution_route_blocked":
            continue
        if event.get("resolver_decision") != resolver_decision:
            continue
        if route_step_id:
            if event.get("route_step_id") != route_step_id:
                continue
        elif (
            event.get("route_step_id")
            or event.get("action_signature") != action_signature
        ):
            continue
        count += 1
    return count


_DRAFT_GOAL = "<按任务填写：这一步要达成什么>"


def _route_step_draft(
    state: Any, action: dict[str, Any], decision: dict[str, Any],
) -> dict[str, Any] | None:
    """execution_route_required 的下一步草稿；见 _route_step_draft_with_preflight。"""
    return _route_step_draft_with_preflight(state, action, decision)[0]


def _route_step_draft_with_preflight(
    state: Any, action: dict[str, Any], decision: dict[str, Any],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """execution_route_required 的下一步草稿（收敛任务书 K8）→ (draft, withheld)。

    withheld 是 declare 此刻会原样返回的拒绝（只读预检之一没过）：草稿因此扣下，
    拒绝文案由调用方原样附上——出口与真实入口同源，不再承诺一条走不通的路。

    由本次调用机械观察到的 tool、program、effects、workdir_role 生成：没有路线时给整条
    单步路线，已有冻结路线时给追加这一步的修订。照抄 action / effects / workdir_role，
    重试时带上 route_step_id 就能精确绑定；goal 与 expected_outputs 按任务填。
    草稿过不了声明校验（例如构建要改走 submit_job），或过不了 declare 入口的只读预检
    （_route_amendment_preflight、envelope 两道门）时不给——错的草稿会被照抄进新一轮
    循环。不为 recovery_basis 生成草稿。
    """
    try:
        tool = str(action.get("tool") or "").strip()
        program = _normalized_program(action.get("program"))
        if not tool or not program or program.startswith(("compound:", "legacy:")):
            return None, None
        effects = sorted({
            str(item) for item in (
                decision.get("effective_effects") or action.get("observed_effects") or [])
        }.intersection(ROUTE_EFFECTS))
        roles = [str(item) for item in (action.get("workdir_roles") or [])
                 if str(item).strip() in CANONICAL_ROLES]
        fill_in = ["goal", "expected_outputs"] + ([] if roles else ["workdir_role"])
        loaded = load_canonical_route(state)
        if loaded.get("status") == "ready":
            existing = loaded.get("route") or {}
        elif loaded.get("reason") == "route_not_declared":
            existing = None
        else:
            # 048 v5（对抗审查）：路线正文被改/丢了（load 判 invalid）——declare 会以
            # canonical_route_record_integrity_failed 拒；把那份拒绝交出去，不再什么都不说。
            route_id = f"declared_route__{_canonical_route_name(state)}"
            try:
                record = state.read_artifact(route_id)
            except Exception:
                record = None
            return None, _canonical_route_integrity_problem(state, route_id, record)
        taken = {str(step.get("id") or "") for step in (existing or {}).get("steps") or []}
        base = re.sub(r"[^A-Za-z0-9_-]+", "_", os.path.basename(program)).strip("_-")
        base = base if _ID_RE.fullmatch(base or "") else "step"
        step_id, suffix = base[:56], 2
        while step_id in taken:
            step_id, suffix = f"{base[:56]}_{suffix}", suffix + 1
        step_action: dict[str, Any] = {"tool": tool, "program": program}
        sequence = action.get("program_sequence")
        if isinstance(sequence, (list, tuple)) and len(sequence) >= 2:
            step_action["program_sequence"] = [str(item) for item in sequence]
        step = {
            "id": step_id, "goal": _DRAFT_GOAL, "after": [], "action": step_action,
            "effects": effects, "workdir_role": roles[0] if roles else "run_root",
            "expected_outputs": [],
        }
        if existing is None:
            route = {"schema_version": 2, "goal": "<按任务填写：整条路线的目标>",
                     "evidence_refs": ["user_original_input"], "steps": [step]}
            arguments: dict[str, Any] = {"route": route}
        else:
            route = deepcopy(existing)
            route["steps"] = [*(route.get("steps") or []), step]
            arguments = {"route": route, "amendment_reason": "<说明为什么追加这一步>"}
        if not validate_route_v2(route, declaring=True)["valid"]:
            return None, None
        # 048 v4/v5：declare 在 schema 之后的只读门，按 declare 的顺序跑——envelope ref
        # 校验 → operation run 里的 scientific 步骤 → enforce 档下的 envelope 缺失 →
        # 正文完整性 → closure / 事件历史 / in_progress / 无 basis 的 blocked。两处同一
        # 顺序，两道门同时不过时扣下的也是 declare 第一个会返回的那份。
        withheld = (
            _route_envelope_refs_refusal(state, route)
            or _route_scope_effect_refusal(state, route)
            or _route_envelope_enforce_refusal(route)
        )
        if withheld is not None:
            return None, withheld
        if existing is not None:
            route_id = f"declared_route__{_canonical_route_name(state)}"
            try:
                record = state.read_artifact(route_id)
            except Exception:
                record = None
            withheld = _canonical_route_integrity_problem(state, route_id, record)
            if withheld is not None:
                return None, withheld
        withheld = _route_amendment_preflight(
            state, build_route_snapshot(state), recovery_basis=None)
        if withheld is not None:
            return None, withheld
        return {
            "tool": "declare_execution_route",
            "arguments": arguments,
            "then": f"重试本次调用，并传 route_step_id={step_id!r}",
            "fill_in": fill_in,
            "note": ("草稿由本次调用观察到的 tool/program/effects/workdir_role 生成；"
                     f"只改 {fill_in} 这几项，其余照抄才能精确绑定"),
        }, None
    except Exception:
        return None, None


def enforce_execution_route(
    state: Any,
    action: dict[str, Any],
    decision: dict[str, Any],
    *,
    phase: str = "pre_spawn",
) -> dict[str, Any] | None:
    """执行两阶段统一门并写有界审计；不记录完整 command/code。"""
    observed_effects = {
        str(item) for item in (
            decision.get("effective_effects")
            or action.get("observed_effects")
            or []
        )
    }
    exact_read_only = action.get("read_only") is True and not observed_effects
    if not exact_read_only:
        try:
            history_warning = _current_blocking_event_history_warning(state)
        except Exception:
            history_warning = "route_history_audit_failed"
        if history_warning is not None:
            reason = _event_history_error_code(history_warning)
            history_decision = {
                **decision,
                "decision": "invalid_event_history",
                "reason": history_warning,
                "history_warning": history_warning,
            }
            return _route_block_payload(
                history_decision,
                reason=reason,
                message=(
                    "路线事件历史无法安全读取或追加，真实动作已在目录物化和"
                    "进程/作业启动前拒绝；safe_execute_python 在此状态下也会被拒；"
                    "若只需只读诊断，请改用 safe_run_bash。"
                    if str(action.get("tool") or "") == "safe_execute_python"
                    else
                    "路线事件历史无法安全读取或追加，真实动作已在目录物化和"
                    "进程/作业启动前拒绝；只读诊断仍可继续。"
                ),
                suggested_owner="framework",
                node_action="reconcile_route_event_history_before_execution",
            )
    if "scientific_execution" in observed_effects:
        assignment_block = prereg_assignment_scientific_block(
            state,
            signal_source="managed_action_admission",
            signal_details={
                "tool": str(action.get("tool") or ""),
                "route_step_id": decision.get("route_step_id"),
                "effective_effects": sorted(observed_effects),
            },
        )
        if assignment_block is not None:
            return assignment_block
    try:
        try:
            from .execution_action_census import pending_operation_action_block
        except ImportError:
            from tools.execution_action_census import pending_operation_action_block
        pending_block = pending_operation_action_block(state, action, decision)
    except Exception as exc:
        # This extra gate exists only for the temporary v2 pending-operation
        # lane.  Release an effectful action only when the authority positively
        # proves that this run is outside that lane.  Missing/corrupt authority
        # is not evidence of safety.  Exact read-only diagnosis remains usable
        # so the operator can inspect and repair the authority.
        read_only_diagnostic = bool(
            exact_read_only
            and decision.get("decision") == "route_not_required"
            and decision.get("policy") == "read_only"
            and decision.get("read_only") is True
        )
        authority_release_proven = read_only_diagnostic
        if not authority_release_proven:
            try:
                try:
                    from .run_contract import resolve_run_acceptance
                except ImportError:
                    from tools.run_contract import resolve_run_acceptance
                accepted = resolve_run_acceptance(state, bind_if_absent=False)
                receipt = accepted.get("receipt") if accepted.get("passed") else None
                assignment = (
                    receipt.get("prereg_assignment")
                    if isinstance(receipt, dict) else None
                )
                authority_release_proven = bool(
                    isinstance(receipt, dict)
                    and (
                        receipt.get("schema_version") == 1
                        or (
                            receipt.get("schema_version") == 2
                            and isinstance(assignment, dict)
                            and assignment.get("kind") in {"bound", "none"}
                        )
                    )
                )
            except Exception:
                authority_release_proven = False
        if not authority_release_proven:
            return {
                "status": "error",
                "error_code": "execution_action_census_guard_unavailable",
                "reason": "pending_operation_authority_or_guard_unavailable",
                "error": (
                    "执行动作账本门无法核验，动作已在副作用前拒绝；不得从缺失事实"
                    "推断本 run 符合临时 pending-operation 兼容条件。"
                ),
                "route_blocked": True,
                "blocker": {
                    "kind": "execution_action_census_guard_unavailable",
                    "reason": type(exc).__name__,
                    "suggested_owner": "experiment",
                    "node_action": "repair_action_census_guard_before_retry",
                },
            }
        pending_block = None
    if pending_block is not None:
        return pending_block
    block = execution_route_block(decision, phase=phase)
    if block is None:
        return None
    if block.get("reason") == "execution_route_required":
        draft = _route_step_draft(state, action, decision)
        withheld = (
            None if draft is not None
            else _route_step_draft_with_preflight(state, action, decision)[1]
        )
        if draft is not None:
            block["next_action"] = draft
        elif isinstance(withheld, dict):
            # 048 v4：草稿被 declare 的只读预检扣下——把 declare 此刻会给的拒绝原样附上
            # （同一段文案、同一个出口），不附草稿、不承诺"照抄"。
            block["route_declare_preflight"] = {
                key: withheld[key] for key in (
                    "error_code", "step_ids", "errors", "in_progress_attempts",
                    "violations", "route_state")
                if key in withheld
            }
            block["error"] = (
                str(block.get("error") or "")
                + "本次未附路线草稿——此刻 declare_execution_route 会被拒（"
                + str(withheld.get("error_code") or "")
                + "）："
                + str(withheld.get("error") or "")
            )
        # 048（2026-09-20，047 走一遍脚本在脏态下撞出）：路线已经冻结时，"声明一条
        # 路线"照字面做会撞 route_amendment_reason_required，要到第二次拒绝才知道
        # 修订必须带 amendment_reason。
        # 048 v2（Codex 复审 03 号）：提示**只能来自真实附上的草稿**——第一版按
        # load_canonical_route 是否 ready 判"可修订"，而 ready 只说明路线可读；
        # blocked / interrupted 路线不给草稿（要 recovery_basis），第一版却在那里
        # 承诺"草稿已附"并教模型带 amendment_reason 修订，照做立刻撞
        # route_recovery_basis_required——在最需要恢复指引的状态上造了死路。
        if draft is not None:
            block["error"] = (
                str(block.get("error") or "")
                + "next_action 里附了照本次调用生成的草稿，直接照抄、把 fill_in 列出的"
                  "字段填成真实值（goal / expected_outputs 不能留占位符）即可。"
            )
            draft_arguments = draft.get("arguments") if isinstance(draft, dict) else None
            if isinstance(draft_arguments, dict) and "amendment_reason" in draft_arguments:
                block["error"] = (
                    str(block.get("error") or "")
                    + "本 run 已有冻结的 canonical 路线：**不要新声明一条平行路线**，草稿就是对"
                      "同一身份的修订（原步骤照抄 + 追加这一步）——把 amendment_reason 换成一句"
                      "真实的修订依据再调，否则会被 route_amendment_reason_required 拒。"
                )
    if block.get("reason") in {
        "route_transcript_tail_unwritable",
        "route_transcript_unreadable",
        "route_event_history_invalid",
    }:
        # Do not append to the medium whose JSONL tail is already unsafe.
        return block
    action_signature = _action_signature(action)
    route_step_id = str(decision.get("route_step_id") or "").strip()
    events, _ = _read_transcript_events(state)
    attempts = 1 + _same_key_route_block_count(
        events,
        route_step_id=route_step_id,
        action_signature=action_signature,
        resolver_decision=decision.get("decision"),
    )
    try:
        state.append_transcript(
            "execution_route_blocked",
            action_signature=action_signature,
            tool=str(action.get("tool") or ""),
            program=_normalized_program(action.get("program")),
            resolver_decision=decision.get("decision"),
            resolver_policy=decision.get("policy"),
            route_step_id=decision.get("route_step_id"),
            phase=phase,
            reason=block["reason"],
        )
    except Exception:
        pass
    if attempts < _ROUTE_BLOCK_MAX_ATTEMPTS:
        return block
    # 同键拒绝已达上限：升级为一次性结构化 blocker，指明两条出路。
    # 升级绝不放行——不匹配的动作在任何情况下都不会被执行。
    blocker = {
        **(block.get("blocker") or {}),
        "kind": "route_attempts_exhausted",
        "underlying_kind": block["reason"],
        "attempts": attempts,
        "max_attempts": _ROUTE_BLOCK_MAX_ATTEMPTS,
        "node_action": "amend_route_with_amendment_reason_or_report_blocker",
    }
    exhausted = {
        **block,
        "reason": "route_attempts_exhausted",
        "underlying_reason": block["reason"],
        "attempts": attempts,
        "max_attempts": _ROUTE_BLOCK_MAX_ATTEMPTS,
        "error": (
            f"同一动作已被同一原因拒绝 {attempts} 次（上限 "
            f"{_ROUTE_BLOCK_MAX_ATTEMPTS}），重复同一动作不会被放行。"
            "停止重复，并按下面原始拒绝给出的合法纠偏动作处理；"
            "如果该动作需要修改冻结路线，须带 amendment_reason。"
            "若合法纠偏本身不可完成，则 report_blocker 如实上报阻塞。"
            "原始拒绝："
            + str(block.get("error") or "")
        ),
        "blocker": blocker,
    }
    try:
        state.append_transcript(
            "route_attempts_exhausted",
            action_signature=action_signature,
            resolver_decision=decision.get("decision"),
            route_step_id=decision.get("route_step_id"),
            underlying_reason=block["reason"],
            attempts=attempts,
            phase=phase,
        )
    except Exception:
        pass
    return exhausted


def route_default_workdir(
    state: Any,
    decision: dict[str, Any],
    *,
    create: bool = False,
) -> dict[str, Any]:
    """把已匹配步骤的 role 解析到既有路径权威；路线本身不产生权限。"""
    if decision.get("decision") != "matched_ready_step" or not decision.get("authoritative"):
        return {"status": "not_applicable", "reason": decision.get("decision")}
    role_name = str(decision.get("declared_workdir_role") or "")
    candidates = [
        role for role in collect_path_roles(state)
        if role.role == role_name and role.writable and not role.container_only
    ]
    by_path = {role.path: role for role in candidates}
    if not by_path:
        return {"status": "unavailable", "role": role_name}
    if len(by_path) > 1:
        return {
            "status": "ambiguous",
            "role": role_name,
            "paths": sorted(by_path),
        }
    path_text, role = next(iter(by_path.items()))
    path = Path(path_text)
    if create and not path.exists():
        if not str(role.source).startswith("framework:"):
            return {
                "status": "missing_explicit_root",
                "role": role_name,
                "path": path_text,
            }
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return {
                "status": "create_failed",
                "role": role_name,
                "path": path_text,
                "error": type(exc).__name__,
            }
    return {"status": "resolved", "role": role_name, "path": path_text}


def _execution_guidance(state: Any) -> dict[str, Any]:
    """从 canonical route、客观进度和 path roles 派生即时执行摘要。

    该返回值不落盘、不创建目录，也不是新的 current_step 真相源；真正执行时
    safe_run_bash/submit_job 仍会重新读取路线与路径权威。
    """
    snapshot = build_route_snapshot(state)
    if snapshot.get("status") != "ready":
        return {
            "route_state": snapshot.get("route_state") or "unavailable",
            "ready_step_ids": [],
            "steps": [],
            "step_identity_contract": _step_identity_contract(),
            "single_entrypoint_per_call": True,
            "static_linear_submit_job_sequence_supported": True,
        }
    ready_ids = list(snapshot.get("ready_step_ids") or [])
    ready = set(ready_ids)
    steps: list[dict[str, Any]] = []
    for step in snapshot["route"]["steps"]:
        if step["id"] not in ready:
            continue
        resolution = route_default_workdir(state, {
            "decision": "matched_ready_step",
            "authoritative": True,
            "declared_workdir_role": step["workdir_role"],
        })
        candidates: list[str] = []
        if resolution.get("status") == "resolved":
            candidates = [str(resolution.get("path") or "")]
        elif resolution.get("status") == "ambiguous":
            candidates = [str(path) for path in resolution.get("paths") or []]
        action = step.get("action") or {}
        guidance = {
            "route_step_id": step["id"],
            "after": list(step.get("after") or []),
            "tool": str(action.get("tool") or ""),
            "program": str(action.get("program") or ""),
            "workdir_role": step["workdir_role"],
            "workdir_resolution": resolution.get("status"),
            "workdir_candidates": candidates,
            "route_step_id_required": True,
        }
        if isinstance(action.get("program_sequence"), list):
            guidance["program_sequence"] = list(action["program_sequence"])
        steps.append(guidance)
    return {
        "route_state": snapshot.get("route_state"),
        "ready_step_ids": ready_ids,
        "steps": steps,
        "step_identity_contract": _step_identity_contract(),
        "correction_witness_limitations": deepcopy(
            snapshot.get("correction_witness_limitations") or []
        ),
        "correction_witness_limitation_check": _correction_limitation_check(
            snapshot.get("correction_witness_limitations") or []),
        "single_entrypoint_per_call": True,
        "static_linear_submit_job_sequence_supported": True,
        "notice": (
            "每次高后果工具调用默认只执行一个入口并传 route_step_id；唯一例外是 "
            "submit_job 已冻结、完整匹配的静态线性 program_sequence。候选路径是"
            "即时派生视图，执行前会重新校验。"
        ),
    }


def record_execution_route_shadow(
    state: Any,
    action: dict[str, Any],
    decision: dict[str, Any],
) -> None:
    """写有界影子摘要；不记录完整 command/code。"""
    legacy_policy = {
        key: value for key, value in (action.get("legacy_policy") or {}).items()
        if key in {"stage", "guarded_build", "formal_simulation"}
    }
    resolver_guarded = decision.get("policy") in {
        "guarded_process",
        "guarded_unknown_effect",
        "managed_external_job",
        "formal_scientific_execution",
    }
    policy_diff: list[str] = []
    if bool(legacy_policy.get("guarded_build")) != resolver_guarded:
        policy_diff.append("resource_guard_classification")
    if bool(legacy_policy.get("formal_simulation")) != (
        decision.get("policy") == "formal_scientific_execution"
    ):
        policy_diff.append("scientific_gate_classification")
    if decision.get("decision") not in {
        "route_not_required", "matched_ready_step", "matched_legacy_step",
    }:
        policy_diff.append("route_action_binding")
    # 影子期已经结束：常态判断不再逐调用复制进 transcript。
    # 只保留新旧策略真实分歧，供迁移回归定位；它不是执行权威。
    if not policy_diff:
        return
    event = {
        "action_signature": _action_signature(action),
        "tool": str(action.get("tool") or ""),
        "program": _normalized_program(action.get("program")),
        "read_only": action.get("read_only") is True,
        "dry_run": action.get("dry_run") is True,
        "observed_effects": sorted({
            str(item) for item in (action.get("observed_effects") or [])
            if str(item).strip()
        }),
        "workdir_roles": sorted({
            str(item) for item in (action.get("workdir_roles") or [])
            if str(item).strip()
        }),
        "legacy_policy": legacy_policy,
        "policy_diff": policy_diff,
        "resolver_decision": decision.get("decision"),
        "resolver_policy": decision.get("policy"),
        "route_step_id": decision.get("route_step_id"),
        "route_ref": decision.get("route_ref"),
        "effective_effects": decision.get("effective_effects", []),
    }
    try:
        state.append_transcript("route_resolution_shadow", **event)
    except Exception:
        pass


def shadow_execution_route(state: Any, action: dict[str, Any]) -> dict[str, Any]:
    """影子解析：记录差异但绝不参与本次工具的允许/拒绝结果。"""
    decision = resolve_execution_context(state, action)
    record_execution_route_shadow(state, action, decision)
    return decision


def begin_route_step_attempt(
    state: Any,
    decision: dict[str, Any] | None,
    *,
    tool: str,
    action: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """在 spawn/submit 前绑定一次路线尝试；未精确匹配时不伪造收据。"""
    if not isinstance(decision, dict):
        return None
    if decision.get("decision") != "matched_ready_step":
        return None
    if not decision.get("authoritative") or not decision.get("workdir_role_observed"):
        return None
    if decision.get("workdir_resolution_status") not in {"resolved", "explicit"}:
        return None
    route_ref = decision.get("route_ref") or {}
    payload_digest = (
        str((action or {}).get("payload_digest") or "").strip()
        or (_action_signature(action) if action else "")
    )
    events, _warnings = _read_transcript_events(state)
    history_warning = _current_blocking_event_history_warning(state)
    if history_warning is not None:
        return {
            "binding_error": _event_history_error_code(history_warning),
            "history_warning": history_warning,
            "route_step_id": decision.get("route_step_id"),
        }
    bound_steps = {
        str(event.get("attempt_id") or ""): str(
            event.get("route_step_id") or "")
        for event in events
        if event.get("event") == "route_step_bound"
        and str(event.get("attempt_id") or "").strip()
    }
    del bound_steps
    recoveries: list[dict[str, Any]] = []
    current_step_id = str(decision.get("route_step_id") or "")
    # 恢复依据的权威来源是纠正 lineage（各冻结路线版本账本行里的收据）；transcript
    # 事件只是审计回显。取本步骤最新的一条；之后已有针对这个步骤的绑定，就算被消费过。
    loaded_route = load_canonical_route(state)
    loaded_ref = loaded_route.get("route_ref") or {}
    if loaded_route.get("status") == "ready" and loaded_ref == route_ref:
        step_heads = [
            entries[-1] for entries in _recovery_lineage(state).values()
            if entries
            and str(entries[-1].get("route_step_id") or "") == current_step_id
        ]
        if step_heads:
            latest = max(
                step_heads, key=lambda entry: int(entry.get("route_version") or 0))
            consumed = any(
                event.get("event") == "route_step_bound"
                and str(event.get("route_step_id") or "") == current_step_id
                and int(event.get("route_version") or 0)
                >= int(latest.get("route_version") or 0)
                for event in events
            )
            if not consumed:
                recoveries.append(latest)
    if recoveries:
        recovery = recoveries[-1]
        expected_output_contract_fix = (
            recovery.get("recovery_validated") is True
            and recovery.get("observed_failure_class")
            == "expected_outputs_missing"
            and recovery.get("expected_outputs_changed") is True
            and str(decision.get("step_execution_contract_hash") or "")
            == str(recovery.get("next_step_execution_contract_hash") or "")
        )
        if (
            payload_digest
            and payload_digest
            == str(recovery.get("previous_action_payload_digest") or "")
            and not (recovery.get("remediation_receipts") or [])
            and not expected_output_contract_fix
        ):
            return {
                "binding_error": "route_recovery_payload_unchanged",
                "route_step_id": decision.get("route_step_id"),
                "previous_attempt_id": recovery.get("attempt_id"),
                "step_execution_contract_hash": decision.get(
                    "step_execution_contract_hash"),
            }
    attempt_id = "route-" + uuid.uuid4().hex
    binding = {
        "attempt_id": attempt_id,
        "route_artifact_id": route_ref.get("artifact_id"),
        "route_version": route_ref.get("version"),
        "route_content_hash": route_ref.get("content_hash"),
        "route_step_id": decision.get("route_step_id"),
        "step_definition_hash": decision.get("step_definition_hash"),
        "step_execution_contract_hash": decision.get(
            "step_execution_contract_hash"),
        "tool": tool,
        "resolved_workdir_role": decision.get("declared_workdir_role"),
        "resolved_workdir": decision.get("resolved_workdir"),
        "expected_outputs": list(decision.get("expected_outputs") or []),
        "applied_policy": decision.get("policy"),
        "bound_at_ns": time.time_ns(),
        "action_payload_digest": payload_digest or None,
        **({"execution_envelope_ref": decision["execution_envelope_ref"]}
           if decision.get("execution_envelope_ref") else {}),
    }
    binding["expected_output_baseline"] = _expected_output_baseline(binding)
    try:
        state.append_transcript("route_step_bound", **binding)
    except Exception as exc:
        return {
            "binding_error": "route_binding_persistence_failed",
            "error_type": type(exc).__name__,
            "route_step_id": decision.get("route_step_id"),
        }
    return binding


def route_binding_block(
    binding: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if not isinstance(binding, dict) or not binding.get("binding_error"):
        return None
    if binding.get("binding_error") == "route_recovery_payload_unchanged":
        return {
            "status": "error",
            "reason": "route_recovery_payload_unchanged",
            "error": (
                "失败后拟执行的 command/code/cwd/关键参数与上次完全相同，且没有"
                "持久化 remediation 证据；高后果进程未启动。"
            ),
            "route_blocked": True,
            "blocker": {
                "kind": "route_recovery_payload_unchanged",
                "previous_attempt_id": binding.get("previous_attempt_id"),
                "node_action": "apply_and_record_remediation_before_retry",
                "retryable_after_change": True,
            },
        }
    if binding.get("binding_error") in {
        "route_transcript_tail_unwritable",
        "route_transcript_unreadable",
        "route_event_history_invalid",
    }:
        reason = str(binding["binding_error"])
        return {
            "status": "error",
            "reason": reason,
            "error": (
                "路线事件历史无法安全追加，步骤开始收据未写入，高后果进程未启动。"
            ),
            "route_blocked": True,
            "blocker": {
                "kind": reason,
                "history_warning": binding.get("history_warning"),
                "suggested_owner": "framework",
                "node_action": "reconcile_route_event_history_before_execution",
                "retryable_after_change": True,
            },
        }
    return {
        "status": "error",
        "reason": "route_binding_persistence_failed",
        "error": "路线步骤开始收据无法持久化，高后果进程未启动。",
        "route_blocked": True,
        "blocker": {
            "kind": "route_binding_persistence_failed",
            "reason": binding.get("error_type"),
            "suggested_owner": "framework",
            "node_action": "repair_transcript_persistence_before_retry",
            "retryable_after_change": True,
        },
    }


# 绑定后二道防线的“零执行基础设施拒绝”错误类。这些 reason 只出现在
# payload/容器启动之前的确定性检查里（路径契约 TOCTOU 复检、本地沙箱
# launch 通道缺失/准入拒绝、build_host 实时内存准入）；什么都没执行过，
# 重试是安全的，不得写成永久 failed。
_INFRASTRUCTURE_REJECTION_REASONS = frozenset({
    # _submit_sync 内的路径契约 TOCTOU 复检（早于 intent/脚本/容器）
    "path_capability_required",
    "local_sandbox_path_contract_rejected",
    # 本地 Docker 提交通道在 launch 之前的可用性/准入拒绝
    "local_submission_state_missing",
    "local_launch_adapter_unavailable",
    "sandbox_admission_rejected",
    # build_host 实时内存准入（payload 启动前的可行性判定）
    "build_host_memory_probe_unavailable",
    "build_host_memory_request_infeasible",
    "build_host_memory_admission_denied",
    # payload 可执行目标预检（E-14）：提交链在脚本/intent/容器之前的
    # 零执行拒绝，以及批准后内容变化的 _submit_sync 复检拒绝。
    "payload_exec_preflight_rejected",
    "payload_abi_preflight_rejected",
    "payload_executable_changed_after_approval",
})


def _infrastructure_rejection_reason(payload: dict[str, Any]) -> str | None:
    """仅当拒绝可识别且确无任何执行/身份证据时，返回其 reason。"""
    reason = str(payload.get("reason") or "")
    if reason not in _INFRASTRUCTURE_REJECTION_REASONS:
        return None
    if payload.get("returncode") is not None:
        return None
    if payload.get("submission_boundary_crossed"):
        return None
    if str(payload.get("job_id") or "") or str(
            payload.get("container_runtime_id") or ""):
        return None
    return reason


def _failure_class(result: dict[str, Any]) -> str:
    text = " ".join(str(result.get(key) or "") for key in (
        "status", "reason", "error", "stderr_tail",
    )).lower()
    if "cancel" in text:
        return "cancelled"
    if "timeout" in text or "timed out" in text:
        return "timeout"
    if "memory" in text or "oom" in text or "pids" in text:
        return "resource_limit"
    return "execution_error"


try:
    from . import output_postconditions as _output_postconditions
except ImportError:  # pragma: no cover - standalone node bootstrap.
    import tools.output_postconditions as _output_postconditions  # type: ignore

# 035a：产物存在性 / 新鲜度 / 身份的唯一实现在 output_postconditions；这里只再导出
# （P0a v4 起 ROC 与测试按 execution_route.output_identity 引用）并做 binding 适配。
output_lexical_key = _output_postconditions.output_lexical_key
output_identity = _output_postconditions.output_identity
output_identity_matches = _output_postconditions.output_identity_matches
_OUTPUT_HASH_CAP_BYTES = _output_postconditions._OUTPUT_HASH_CAP_BYTES
#: 每个 attempt 收据的条目上限；测试可 monkeypatch，调用时透传给 evaluator。
_OUTPUT_OBSERVATION_CAP = _output_postconditions.DEFAULT_OBSERVATION_CAP


def _safe_expected_output_matches(
    binding: dict[str, Any],
) -> dict[str, list[Path]]:
    """展开安全的预期产物；任何匹配都不能逃出 resolved_workdir。"""
    base_text = str(binding.get("resolved_workdir") or "").strip()
    if not base_text:
        return {}
    base = Path(base_text).expanduser().resolve(strict=False)
    result: dict[str, list[Path]] = {}
    for spec in binding.get("expected_outputs") or []:
        spec = str(spec).strip()
        if not spec:
            continue
        matches: list[Path] = []
        for item in glob.glob(str(base / spec)):
            match = Path(item).resolve(strict=False)
            try:
                match.relative_to(base)
            except ValueError:
                continue
            if match.exists():
                matches.append(match)
        result[spec] = sorted(set(matches), key=str)
    return result


def _output_set_fingerprint(base: Path, matches: list[Path]) -> str:
    rows: list[tuple[Any, ...]] = []
    for match in matches:
        try:
            stat = match.stat()
            rows.append((
                str(match.relative_to(base)),
                stat.st_mode,
                stat.st_ino,
                stat.st_size,
                stat.st_mtime_ns,
                stat.st_ctime_ns,
            ))
        except (OSError, ValueError):
            continue
    return _content_hash(json.dumps(rows, ensure_ascii=False, separators=(",", ":")))


def _expected_output_baseline(binding: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """绑定时的产物基线（每 spec 的匹配成员身份；目录 spec 记其条目）。"""
    base_text = str(binding.get("resolved_workdir") or "").strip()
    if not base_text:
        return {}
    return _output_postconditions.output_baseline(
        base_text, [str(item) for item in (binding.get("expected_outputs") or [])])


def _evaluate_binding_outputs(
    binding: dict[str, Any], *, observation_cap: int | None = None,
) -> dict[str, Any] | None:
    """把 route binding 喂给共享 evaluator；没有 resolved_workdir 时返回 None。

    binding 里的 ``not_after_ns``（外部作业：物理结束 / 首次终态观测时刻）是上界：
    晚于它写出的文件不是作业的产物。"""
    base_text = str(binding.get("resolved_workdir") or "").strip()
    if not base_text:
        return None
    bound_at = binding.get("bound_at_ns")
    upper = binding.get("not_after_ns")
    return _output_postconditions.evaluate_declared_outputs(
        [str(item) for item in (binding.get("expected_outputs") or []) if str(item).strip()],
        root=base_text,
        baseline=binding.get("expected_output_baseline"),
        not_before_ns=int(bound_at) if isinstance(bound_at, int) and bound_at > 0 else None,
        not_after_ns=int(upper) if isinstance(upper, int) and upper > 0 else None,
        allow_empty_realpaths=[
            str(item) for item in (binding.get("allow_empty_realpaths") or []) if str(item)
        ],
        observation_cap=(
            observation_cap if observation_cap is not None else _OUTPUT_OBSERVATION_CAP),
    )


def _verify_expected_outputs(
    binding: dict[str, Any],
) -> tuple[list[str], list[str], list[str]]:
    """返回 (已验证 spec, 实际路径, 缺失/未更新/不合格 spec)。"""
    expected = [
        str(item) for item in (binding.get("expected_outputs") or [])
        if str(item).strip()
    ]
    if not expected:
        return [], [], []
    result = _evaluate_binding_outputs(binding)
    if result is None:
        return [], [], expected
    return (
        list(result["verified_specs"]),
        list(result["verified_paths"]),
        list(result["failed_specs"]),
    )


def _unchanged_expected_outputs(
    binding: dict[str, Any], missing: list[str],
) -> list[str]:
    """缺失清单里「文件在、但本次执行没有更新它」的那几项（收敛任务书 K5，缺陷 #15）。

    只用来把报错说准，判定不变：指纹没变也可能是作业确实没写。
    """
    try:
        result = _evaluate_binding_outputs(binding)
    except Exception:
        return []
    if result is None:
        return []
    unchanged = set(_output_postconditions.unchanged_specs(result))
    return [spec for spec in missing if spec in unchanged]


def _safe_expected_output_lexical_matches(
    binding: dict[str, Any],
) -> dict[str, list[str]]:
    """每个 spec 在声明根内的 lexical 匹配（与 evaluator 同一套边界）。"""
    base_text = str(binding.get("resolved_workdir") or "").strip()
    if not base_text:
        return {}
    return {
        str(spec).strip(): _output_postconditions.expand_declared_output(base_text, str(spec))
        for spec in (binding.get("expected_outputs") or [])
        if str(spec).strip()
    }


def _output_observations(
    binding: dict[str, Any], verified_specs: list[str],
) -> tuple[list[dict[str, Any]], bool]:
    """已验证 spec 的逐文件身份观测（冻结进 attempt 收尾事件）——evaluator 的 observation
    原样，只留通过的那些。"""
    try:
        result = _evaluate_binding_outputs(binding)
    except Exception:
        return [], False
    if result is None:
        return [], False
    wanted = {str(item) for item in verified_specs}
    rows = [
        dict(row) for row in result["observations"]
        if row.get("passed") and str(row.get("spec")) in wanted
    ]
    return rows, bool(result["observations_truncated"])


def finish_route_step_attempt(
    state: Any,
    binding: dict[str, Any] | None,
    *,
    result: dict[str, Any] | None = None,
    error: BaseException | None = None,
    external_submission: bool = False,
) -> dict[str, Any] | None:
    """在受管工具返回后闭合 attempt；失败记录本身不得覆盖原工具结果。"""
    if not binding or binding.get("binding_error"):
        return
    event: dict[str, Any] = {
        "attempt_id": binding["attempt_id"],
        "route_artifact_id": binding.get("route_artifact_id"),
        "route_version": binding.get("route_version"),
        "route_content_hash": binding.get("route_content_hash"),
        "route_step_id": binding.get("route_step_id"),
        "step_definition_hash": binding.get("step_definition_hash"),
        "step_execution_contract_hash": binding.get(
            "step_execution_contract_hash"),
    }
    if error is not None:
        cancelled = type(error).__name__ == "CancelledError"
        event.update({
            "outcome": "cancelled" if cancelled else "failed",
            "failure_class": "cancelled" if cancelled else "tool_exception",
            "error_type": type(error).__name__,
        })
    else:
        payload = result if isinstance(result, dict) else {}
        status = str(payload.get("status") or "error")
        persistence = payload.get("submission_persistence") or {}
        durable_external_receipt = bool(
            str(payload.get("submission_artifact_id") or "").strip()
            or (
                persistence.get("status") == "recovered"
                and str(persistence.get("artifact_id") or "").strip()
            )
        )
        normalized_identity = _normalized_external_identity(payload)
        known_external_identity = bool(
            normalized_identity["scheduler"]
            and normalized_identity["job_id"]
            and (
                normalized_identity["scheduler"] != "local"
                or _valid_local_external_identity(normalized_identity)
            )
        )
        if (external_submission
                and status in {"success", "submitted_needs_recovery"}
                and known_external_identity
                and durable_external_receipt):
            event.update({
                "outcome": "submitted",
                "domain_receipts": [{
                    "scheduler": payload.get("scheduler"),
                    "job_id": payload.get("job_id"),
                    "namespace": payload.get("namespace"),
                    "launch_host": payload.get("launch_host"),
                    "scheduler_cluster": payload.get("scheduler_cluster"),
                    "resource_uid": payload.get("resource_uid"),
                    "submission_nonce": payload.get("submission_nonce"),
                    "process_group_id": payload.get("process_group_id"),
                    "process_start_ticks": payload.get("process_start_ticks"),
                    "container_runtime_id": payload.get("container_runtime_id"),
                    "submission_artifact_id": payload.get("submission_artifact_id"),
                    "submission_recovery_artifact_id": persistence.get("artifact_id"),
                    "workflow_task_id": payload.get("external_workflow_task_id"),
                }],
            })
            event["domain_receipts"][0].update(normalized_identity)
        elif external_submission and (
            status in {
                "success",
                "accepted_identity_unresolved",
                "submission_outcome_unknown",
                "submitted_needs_recovery",
            }
        ):
            event.update({
                "outcome": "unknown",
                "failure_class": "external_identity_reconciliation_required",
            })
        elif status == "success":
            verified_specs, verified_paths, missing = _verify_expected_outputs(binding)
            observations, observations_truncated = _output_observations(
                binding, verified_specs)
            if missing:
                event.update({
                    "outcome": "failed",
                    "failure_class": "expected_outputs_missing",
                    "managed_tool_receipt": {
                        "status": "success",
                        "returncode": payload.get("returncode"),
                    },
                    "missing_expected_outputs": missing,
                    "unchanged_expected_outputs": _unchanged_expected_outputs(
                        binding, missing),
                    "verified_output_specs": verified_specs,
                    "verified_outputs": verified_paths,
                    "output_observations": observations,
                    "output_observations_truncated": observations_truncated,
                })
            else:
                event.update({
                    "outcome": "success",
                    "managed_tool_receipt": {
                        "status": "success",
                        "returncode": payload.get("returncode"),
                    },
                    "verified_output_specs": verified_specs,
                    "verified_outputs": verified_paths,
                    # P0a v4：产物身份收据，ROC(build) 按它归属声明的产物。
                    "output_observations": observations,
                    "output_observations_truncated": observations_truncated,
                })
        else:
            rejection_reason = _infrastructure_rejection_reason(payload)
            if rejection_reason is not None:
                event.update({
                    "outcome": "rejected",
                    "failure_class": "infrastructure_rejection",
                    "rejection_reason": rejection_reason,
                })
            else:
                failure_class = _failure_class(payload)
                event.update({
                    "outcome": (
                        "cancelled" if failure_class == "cancelled"
                        else "timeout" if failure_class == "timeout"
                        else "failed"
                    ),
                    "failure_class": failure_class,
                })
    event["receipt_persisted"] = True
    try:
        state.append_transcript("route_step_outcome", **event)
    except Exception as exc:
        event["receipt_persisted"] = False
        event["persistence_error_type"] = type(exc).__name__
    return event


def _suggested_recovery_failure_class(value: Any) -> str:
    raw = str(value or "").strip()
    if raw == "expected_outputs_missing":
        return "expected_output"
    if raw in {"resource_limit", "resource"}:
        return "resource"
    if raw == "external_identity_reconciliation_required":
        return "external_identity"
    if raw in {"timeout", "cancelled"}:
        return raw
    return "execution"


def _event_recovery_context(event: dict[str, Any]) -> dict[str, Any]:
    """给调用方可直接照填的有界恢复上下文；它不是持久化状态。"""
    attempt_id = str(event.get("attempt_id") or "")
    observed = str(event.get("failure_class") or event.get("outcome") or "")
    suggested = _suggested_recovery_failure_class(observed)
    expected_output_correction = observed == "expected_outputs_missing"
    if expected_output_correction:
        sequence = [
            "核对本 attempt 的受管执行成功收据与 missing_expected_outputs",
            "整份路线只修订该步骤 expected_outputs，不改 goal、DAG、入口、effects 或其他步骤",
            (
                "以 evidence_refs=[] 修订同一个 declared_route；不要制造 "
                "raw_results、clean_results 或 experiment_log"
            ),
            (
                "由修订时冻结的本地 time-window witness 复用原 attempt；"
                "不要重新执行已经成功的负载"
            ),
            (
                "若无法对原 attempt 做纯改指向而要放弃它并重跑：保留 step id、"
                "声明新的非空 expected_outputs，提供失败后的新诊断/修复证据，"
                "并把 failure_class 改成证据支持的真实根因（如 parameter、"
                "environment、execution 或 source）；不得继续使用 expected_output"
            ),
        ]
        diagnosis = "<旧 expected_outputs 与入口真实输出语义不一致>"
        evidence_refs: list[str] = []
    else:
        sequence = [
            "先做只读诊断并分类根因，不直接重复高后果动作",
            "用 save_artifact 保存失败后产生的非闭环诊断证据",
            "把该引用同时写入 route.evidence_refs 与 recovery_basis.evidence_refs",
            "若重复相同 payload，必须实际修复并在 remediation_refs 中提供修复 artifact",
        ]
        diagnosis = "<由失败后新证据支持的根因>"
        evidence_refs = ["artifact:<失败后新建的非闭环证据 artifact_id>"]
    result = {
        "attempt_id": attempt_id,
        "route_step_id": event.get("route_step_id"),
        "observed_failure_class": observed,
        "suggested_failure_class": suggested,
        "required_sequence": sequence,
        "prohibited_evidence_artifact_types": sorted(
            _RECOVERY_RESERVED_ARTIFACT_TYPES),
        "recovery_basis_template": {
            "attempt_id": attempt_id,
            "failure_class": suggested,
            "diagnosis": diagnosis,
            "evidence_refs": evidence_refs,
        },
    }
    if expected_output_correction:
        result["recovery_basis_template_scope"] = (
            "上面的 expected_output 模板只用于复用原 attempt 的纯改指向；"
            "放弃旧 attempt 并重跑时必须改用证据支持的真实 failure_class。"
        )
    return result


def route_attempt_receipt(event: dict[str, Any] | None) -> dict[str, Any] | None:
    """把已持久化路线结果投影为工具返回中的有界收据。"""
    if not isinstance(event, dict) or not str(event.get("attempt_id") or ""):
        return None
    receipt = {
        key: deepcopy(event.get(key))
        for key in (
            "attempt_id", "route_artifact_id", "route_version",
            "route_content_hash", "route_step_id",
            "step_execution_contract_hash", "action_payload_digest",
            "outcome", "failure_class", "missing_expected_outputs",
            "receipt_persisted",
        )
        if event.get(key) is not None
    }
    if str(event.get("outcome") or "") in {
        "failed", "timeout", "cancelled", "unknown",
    }:
        receipt["recovery_context"] = _event_recovery_context(event)
    return receipt


def managed_execution_result_receipt(result: Any) -> dict[str, Any] | None:
    """保留受管执行的有界 tail/日志收据，不复制 command 或完整日志。"""
    if not isinstance(result, dict):
        return None
    allowed = (
        "status", "reason", "returncode", "stdout_tail", "stderr_tail",
        "log_path", "log_sha256", "log_bytes", "execution_supervisor",
    )
    receipt = {
        key: deepcopy(result.get(key))
        for key in allowed if result.get(key) is not None
    }
    return receipt or None


def route_outcome_block(event: dict[str, Any] | None) -> dict[str, Any] | None:
    """把“工具成功但路线产物缺失”提升为诚实的动作失败。"""
    if isinstance(event, dict) and event.get("receipt_persisted") is False:
        return {
            "status": "error",
            "reason": "route_outcome_persistence_failed",
            "error": "动作已返回，但路线结果收据无法持久化；状态未知，禁止重试。",
            "blocker": {
                "kind": "route_outcome_persistence_failed",
                "reason": event.get("persistence_error_type"),
                "suggested_owner": "framework",
                "node_action": "repair_and_reconcile_attempt_before_retry",
                "retryable_after_change": True,
            },
        }
    if not isinstance(event, dict) or event.get("failure_class") != "expected_outputs_missing":
        return None
    missing = list(event.get("missing_expected_outputs") or [])
    unchanged = [spec for spec in (event.get("unchanged_expected_outputs") or [])
                 if spec in missing]
    absent = [spec for spec in missing if spec not in unchanged]
    recovery_context = _event_recovery_context(event)
    if unchanged:
        detail = "；".join(filter(None, (
            f"缺失 {absent}" if absent else "",
            f"预期产物存在但本次执行没有更新它 {unchanged}（与执行前指纹相同；也可能是作业确实没写）",
        )))
        error = f"命令返回成功，但路线步骤缺少本次产生的预期产物：{detail}；已停止路线并等待诊断。"
    else:
        error = f"命令返回成功，但路线步骤缺少预期产物：{missing}；已停止路线并等待诊断。"
    return {
        "status": "error",
        "reason": "route_expected_outputs_missing",
        "error": error,
        "missing_expected_outputs": missing,
        **({"unchanged_expected_outputs": unchanged} if unchanged else {}),
        "attempt_id": event.get("attempt_id"),
        "route_step_id": event.get("route_step_id"),
        "recovery_context": recovery_context,
        "blocker": {
            "kind": "route_expected_outputs_missing",
            "node_action": "diagnose_then_amend_route",
            "retryable_after_change": True,
            "attempt_id": event.get("attempt_id"),
            "route_step_id": event.get("route_step_id"),
        },
    }


def _valid_local_external_identity(receipt: dict[str, Any]) -> bool:
    return bool(
        str(receipt.get("scheduler") or "").strip().lower() == "local"
        and str(receipt.get("submission_nonce") or "").strip()
        and _CONTAINER_RUNTIME_ID_RE.fullmatch(
            str(receipt.get("container_runtime_id") or "").strip()
        )
    )


def _external_receipt_key(
    receipt: dict[str, Any],
) -> tuple[str, ...]:
    scheduler = str(receipt.get("scheduler") or "").strip().lower()
    local = scheduler == "local"
    return (
        scheduler,
        str(receipt.get("namespace") or "").strip(),
        str(receipt.get("launch_host") or "").strip().casefold(),
        str(receipt.get("scheduler_cluster") or "").strip().casefold(),
        str(receipt.get("resource_uid") or "").strip(),
        str(receipt.get("job_id") or "").strip(),
        str(receipt.get("submission_nonce") or "").strip(),
        "" if local else str(receipt.get("process_group_id") or "").strip(),
        "" if local else str(receipt.get("process_start_ticks") or "").strip(),
        str(receipt.get("container_runtime_id") or "").strip(),
    )


def _external_submission_receipts(
    events: list[dict[str, Any]],
) -> list[tuple[str, dict[str, Any]]]:
    """返回 attempt 与权威 external identity；兼容后续 identity reconcile 事件。"""
    receipts: list[tuple[str, dict[str, Any]]] = []
    for event in events:
        attempt_id = str(event.get("attempt_id") or "").strip()
        if not attempt_id:
            continue
        if event.get("event") == "route_step_outcome" and event.get("outcome") == "submitted":
            candidates = event.get("domain_receipts") or []
        elif event.get("event") == "route_step_external_identity_resolved":
            candidate = (
                event.get("external_job_ref")
                or event.get("domain_receipt")
                or event
            )
            candidates = [candidate]
        else:
            continue
        for receipt in candidates:
            if not isinstance(receipt, dict):
                continue
            normalized = _normalized_external_identity(receipt)
            if not normalized["scheduler"] or not normalized["job_id"]:
                continue
            if (
                normalized["scheduler"] == "local"
                and not _valid_local_external_identity(normalized)
            ):
                continue
            receipts.append((attempt_id, {**receipt, **normalized}))
    return receipts


def _external_receipt_matches_query(
    receipt: dict[str, Any],
    query: dict[str, Any],
) -> bool:
    """Remote legacy scope may be omitted; local immutable identity may not."""
    normalized_receipt = _normalized_external_identity(receipt)
    normalized_query = _normalized_external_identity(query)
    receipt_key = _external_receipt_key(normalized_receipt)
    query_key = _external_receipt_key(normalized_query)
    if receipt_key[0] != query_key[0] or receipt_key[5] != query_key[5]:
        return False
    local = receipt_key[0] == "local"
    if local and not (
        _valid_local_external_identity(normalized_receipt)
        and _valid_local_external_identity(normalized_query)
        and receipt_key[6] == query_key[6]
        and receipt_key[9] == query_key[9]
    ):
        return False
    optional = [
        ("namespace", 1),
        ("launch_host", 2),
        ("scheduler_cluster", 3),
        ("resource_uid", 4),
    ]
    if not local:
        optional.extend([
            ("submission_nonce", 6),
            ("process_group_id", 7),
            ("process_start_ticks", 8),
            ("container_runtime_id", 9),
        ])
    return all(
        not str(query.get(field) or "").strip()
        or receipt_key[index] == query_key[index]
        for field, index in optional
    )


def _external_route_declared(state: Any) -> tuple[bool, dict[str, Any]]:
    loaded = load_canonical_route(state)
    if (
        loaded.get("status") == "unavailable"
        and loaded.get("reason") == "route_not_declared"
    ):
        return False, loaded
    if loaded.get("status") != "ready":
        return True, loaded
    has_external_step = any(
        "external_job" in (step.get("effects") or [])
        for step in (loaded.get("route") or {}).get("steps") or []
    )
    return has_external_step, loaded


def _external_verification_digest(
    reference: dict[str, Any],
    success_evidence: dict[str, Any],
) -> str:
    payload = {
        "external_identity": {
            "scheduler": _external_receipt_key(reference)[0],
            "namespace": _external_receipt_key(reference)[1],
            "launch_host": _external_receipt_key(reference)[2],
            "scheduler_cluster": _external_receipt_key(reference)[3],
            "resource_uid": _external_receipt_key(reference)[4],
            "job_id": _external_receipt_key(reference)[5],
            "submission_nonce": _external_receipt_key(reference)[6],
            "process_group_id": _external_receipt_key(reference)[7],
            "process_start_ticks": _external_receipt_key(reference)[8],
            "container_runtime_id": _external_receipt_key(reference)[9],
            "route_attempt_id": str(
                reference.get("route_attempt_id") or "").strip(),
        },
        "success_evidence": success_evidence,
    }
    return _content_hash(json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ))


_EXTERNAL_IDENTITY_FIELDS = (
    "scheduler",
    "job_id",
    "namespace",
    "launch_host",
    "scheduler_cluster",
    "resource_uid",
    "submission_nonce",
    "process_group_id",
    "process_start_ticks",
    "container_runtime_id",
    "submission_artifact_id",
)


def _normalized_external_identity(receipt: dict[str, Any]) -> dict[str, Any]:
    normalized = {
        field: receipt.get(field)
        for field in _EXTERNAL_IDENTITY_FIELDS
    }
    normalized["scheduler"] = str(
        normalized.get("scheduler") or "").strip().lower()
    normalized["job_id"] = str(normalized.get("job_id") or "").strip()
    normalized["namespace"] = str(
        normalized.get("namespace") or "").strip() or None
    normalized["launch_host"] = str(
        normalized.get("launch_host") or "").strip().casefold() or None
    normalized["scheduler_cluster"] = str(
        normalized.get("scheduler_cluster") or "").strip().casefold() or None
    normalized["resource_uid"] = str(
        normalized.get("resource_uid") or "").strip() or None
    normalized["submission_nonce"] = str(
        normalized.get("submission_nonce") or "").strip()
    normalized["process_group_id"] = str(
        normalized.get("process_group_id") or "").strip() or None
    normalized["process_start_ticks"] = str(
        normalized.get("process_start_ticks") or "").strip() or None
    normalized["container_runtime_id"] = str(
        normalized.get("container_runtime_id") or "").strip() or None
    if normalized["scheduler"] == "local":
        normalized["process_group_id"] = None
        normalized["process_start_ticks"] = None
    normalized["submission_artifact_id"] = str(
        normalized.get("submission_artifact_id") or "").strip()
    return normalized


def _external_identity_digest(receipt: dict[str, Any]) -> str:
    return _content_hash(json.dumps(
        _normalized_external_identity(receipt),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ))


def _current_run_artifact_payload(
    state: Any,
    artifact_id: str,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    try:
        record = state.read_artifact(artifact_id)
    except Exception:
        return None, None
    if not (
        isinstance(record, dict)
        and record.get("produced_by_node_type") == "experiment"
        and record.get("produced_by_run_id") == state.run_id
    ):
        return None, None
    try:
        payload = json.loads(str(record.get("content") or ""))
    except (TypeError, ValueError, json.JSONDecodeError):
        return record, None
    return record, payload if isinstance(payload, dict) else None


def _valid_external_submission_artifact(
    state: Any,
    artifact_id: str,
    attempt_id: str,
    receipt: dict[str, Any],
) -> bool:
    record, payload = _current_run_artifact_payload(state, artifact_id)
    return bool(
        isinstance(record, dict)
        and record.get("type") in {
            "job_submission", "external_job_submission_recovery",
        }
        and isinstance(payload, dict)
        and payload.get("status") == "success"
        and str(payload.get("route_attempt_id") or "") == attempt_id
        and str(payload.get("submission_nonce") or "") == attempt_id
        and _external_receipt_key(payload) == _external_receipt_key(receipt)
    )


def _valid_identity_reconciliation_artifact(
    state: Any,
    artifact_id: str,
    submission_artifact_id: str,
    attempt_id: str,
) -> bool:
    if artifact_id == submission_artifact_id:
        return True
    record, payload = _current_run_artifact_payload(state, artifact_id)
    return bool(
        isinstance(record, dict)
        and record.get("type") == "external_job_submission_recovery"
        and isinstance(payload, dict)
        and str(payload.get("route_attempt_id") or "") == attempt_id
        and str(payload.get("submission_nonce") or "") == attempt_id
    )


def _valid_external_identity_resolution_event(
    state: Any,
    event: dict[str, Any],
    attempt_id: str,
) -> bool:
    receipt = event.get("domain_receipt")
    if not isinstance(receipt, dict):
        return False
    normalized = _normalized_external_identity(receipt)
    if not (
        normalized["scheduler"]
        and normalized["job_id"]
        and normalized["submission_nonce"]
        and normalized["submission_artifact_id"]
    ):
        return False
    if (
        normalized["scheduler"] == "local"
        and not _valid_local_external_identity(normalized)
    ):
        return False
    if str(event.get("route_attempt_id") or "") != attempt_id:
        return False
    if _external_receipt_key(event) != _external_receipt_key(normalized):
        return False
    if str(event.get("identity_digest") or "") != _external_identity_digest(
        normalized
    ):
        return False
    reconciliation_artifact_id = str(
        event.get("reconciliation_artifact_id") or "").strip()
    return bool(
        normalized["submission_nonce"] == attempt_id
        and _valid_external_submission_artifact(
            state,
            normalized["submission_artifact_id"],
            attempt_id,
            normalized,
        )
        and reconciliation_artifact_id
        and _valid_identity_reconciliation_artifact(
            state,
            reconciliation_artifact_id,
            normalized["submission_artifact_id"],
            attempt_id,
        )
    )


def record_external_route_identity_resolution(
    state: Any,
    *,
    route_attempt_id: str,
    domain_receipt: dict[str, Any],
    reconciliation_artifact_id: str,
) -> dict[str, Any]:
    """将一次未知提交对账为唯一 external identity，不触发重提交。"""
    attempt_id = str(route_attempt_id or "").strip()
    receipt = (
        _normalized_external_identity(domain_receipt)
        if isinstance(domain_receipt, dict) else {}
    )
    reconciliation_id = str(reconciliation_artifact_id or "").strip()
    if not attempt_id or not receipt:
        return {"status": "error", "reason": "route_external_identity_invalid"}
    if not (
        receipt.get("scheduler")
        and receipt.get("job_id")
        and receipt.get("submission_artifact_id")
    ):
        return {"status": "error", "reason": "route_external_identity_invalid"}
    if (
        receipt["scheduler"] == "local"
        and not _valid_local_external_identity(receipt)
    ):
        return {
            "status": "error",
            "reason": "route_local_container_identity_incomplete",
        }
    if not receipt.get("submission_nonce"):
        return {"status": "error", "reason": "route_external_identity_invalid"}
    if receipt["submission_nonce"] != attempt_id:
        return {
            "status": "error",
            "reason": "route_external_nonce_mismatch",
        }
    if not (
        _valid_external_submission_artifact(
            state,
            receipt["submission_artifact_id"],
            attempt_id,
            receipt,
        )
        and reconciliation_id
        and _valid_identity_reconciliation_artifact(
            state,
            reconciliation_id,
            receipt["submission_artifact_id"],
            attempt_id,
        )
    ):
        return {
            "status": "error",
            "reason": "route_external_identity_evidence_missing",
        }

    events, warnings = _read_transcript_events(state)
    if _current_blocking_event_history_warning(state) is not None:
        return {"status": "error", "reason": "invalid_event_history"}
    bindings = [
        event for event in events
        if event.get("event") == "route_step_bound"
        and str(event.get("attempt_id") or "") == attempt_id
    ]
    if len(bindings) != 1 or not (
        bindings[0].get("tool") == "submit_job"
        and bindings[0].get("applied_policy") == "managed_external_job"
    ):
        return {
            "status": "error",
            "reason": "route_external_attempt_not_managed_submission",
        }
    outcomes = [
        event for event in events
        if event.get("event") == "route_step_outcome"
        and str(event.get("attempt_id") or "") == attempt_id
    ]
    if len(outcomes) > 1:
        return {"status": "error", "reason": "invalid_event_history"}
    if outcomes and not (
        outcomes[0].get("outcome") == "unknown"
        and outcomes[0].get("failure_class")
        == "external_identity_reconciliation_required"
    ):
        return {
            "status": "error",
            "reason": "route_attempt_not_identity_unresolved",
        }

    digest = _external_identity_digest(receipt)
    existing = [
        event for event in events
        if event.get("event") == "route_step_external_identity_resolved"
        and (
            str(event.get("attempt_id") or "") == attempt_id
            or _external_receipt_key(event) == _external_receipt_key(receipt)
        )
    ]
    if existing:
        if len(existing) == 1 and (
            str(existing[0].get("attempt_id") or "") == attempt_id
            and str(existing[0].get("identity_digest") or "") == digest
            and str(existing[0].get("reconciliation_artifact_id") or "")
            == reconciliation_id
        ):
            return {
                "status": "success",
                "already_projected": True,
                "attempt_id": attempt_id,
                "identity_digest": digest,
            }
        return {
            "status": "error",
            "reason": "route_external_identity_resolution_conflict",
            "attempt_id": attempt_id,
        }
    # 同一 external identity 不能同时绑定到另一个已知 submission attempt。
    for known_attempt, known_receipt in _external_submission_receipts(events):
        if (
            known_attempt != attempt_id
            and _external_receipt_key(known_receipt)
            == _external_receipt_key(receipt)
        ):
            return {
                "status": "error",
                "reason": "route_external_identity_resolution_conflict",
                "attempt_id": attempt_id,
                "conflicting_attempt_id": known_attempt,
            }
    event = {
        "attempt_id": attempt_id,
        "route_attempt_id": attempt_id,
        **receipt,
        "domain_receipt": receipt,
        "identity_digest": digest,
        "reconciliation_artifact_id": reconciliation_id,
    }
    try:
        state.append_transcript(
            "route_step_external_identity_resolved", **event)
    except Exception as exc:
        return {
            "status": "error",
            "reason": "route_external_identity_resolution_persistence_failed",
            "error_type": type(exc).__name__,
            "attempt_id": attempt_id,
        }
    return {"status": "success", **event}


def _recorded_at_ns(receipt: dict[str, Any]) -> int:
    from datetime import datetime

    try:
        return int(datetime.fromisoformat(
            str(receipt.get("recorded_at") or "").replace("Z", "+00:00")
        ).timestamp() * 1_000_000_000)
    except (TypeError, ValueError):
        return 0


_LOCAL_FILE_IDENTITY_KEYS = (
    "device", "inode", "mode", "link_count", "size", "mtime_ns", "ctime_ns",
)


def _exact_nonnegative_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _local_identity_fingerprint(
    relative_path: str, identity: tuple[int, ...],
) -> str:
    return _content_hash(json.dumps(
        (relative_path, *identity),
        ensure_ascii=False,
        separators=(",", ":"),
    ))


def _local_file_record_identity(
    record: dict[str, Any],
) -> tuple[int, ...] | None:
    identity = tuple(record.get(key) for key in _LOCAL_FILE_IDENTITY_KEYS)
    return identity if all(_exact_nonnegative_int(value) for value in identity) else None


def _valid_local_correction_witness(
    receipt: dict[str, Any],
    step: dict[str, Any],
    run_id: str,
) -> dict[str, Any] | None:
    """Validate every frozen identity/time binding before local attempt reuse."""
    witness = receipt.get("local_output_correction_witness")
    attempt_receipt = receipt.get("attempt_receipt")
    if not (
        receipt.get("local_execution_reused") is True
        and isinstance(witness, dict)
        and isinstance(attempt_receipt, dict)
        and witness.get("physical_postcondition_verified") is True
        and witness.get("witness_scope") == _LOCAL_CORRECTION_WITNESS_SCOPE
        and witness.get("semantic_identity_independently_verified") is False
        and str(witness.get("run_id") or "") == run_id
        and str(receipt.get("run_id") or "") == run_id
        and str(witness.get("attempt_id") or "")
        == str(receipt.get("attempt_id") or "")
        and str(witness.get("route_step_id") or "")
        == str(receipt.get("route_step_id") or "")
        == str(step.get("id") or "")
        and str(witness.get("corrected_route_content_hash") or "")
        == str(receipt.get("next_route_content_hash") or "")
        and str(witness.get("corrected_step_definition_hash") or "")
        == str(receipt.get("next_step_definition_hash") or "")
        == step_definition_hash(step)
        and str(witness.get("corrected_step_execution_contract_hash") or "")
        == str(receipt.get("next_step_execution_contract_hash") or "")
        == step_execution_contract_hash(step)
        and str(witness.get("original_step_definition_hash") or "")
        == str(attempt_receipt.get("step_definition_hash") or "")
        and str(witness.get("original_step_execution_contract_hash") or "")
        == str(receipt.get("previous_step_execution_contract_hash") or "")
        == str(attempt_receipt.get("step_execution_contract_hash") or "")
        and witness.get("original_route_ref") == {
            "artifact_id": str(attempt_receipt.get("route_artifact_id") or ""),
            "version": attempt_receipt.get("route_version"),
            "content_hash": str(attempt_receipt.get("route_content_hash") or ""),
        }
        and str(witness.get("resolved_workdir") or "")
        == str(attempt_receipt.get("resolved_workdir") or "")
        and witness.get("bound_at_ns") == attempt_receipt.get("bound_at_ns")
        and witness.get("terminal_at_ns") == _recorded_at_ns(attempt_receipt)
        and witness.get("basename_continuity_verified") is True
    ):
        return None
    original_outputs = list(attempt_receipt.get("expected_outputs") or [])
    corrected_outputs = list(step.get("expected_outputs") or [])
    if not (
        witness.get("original_expected_outputs") == original_outputs
        and witness.get("corrected_expected_outputs") == corrected_outputs
        and witness.get("verified_outputs") == corrected_outputs
        and sorted(os.path.basename(str(item)) for item in original_outputs)
        == sorted(os.path.basename(str(item)) for item in corrected_outputs)
    ):
        return None
    bound_ns = witness.get("bound_at_ns")
    terminal_ns = witness.get("terminal_at_ns")
    files = witness.get("files")
    if not (
        _exact_nonnegative_int(bound_ns) and bound_ns > 0
        and _exact_nonnegative_int(terminal_ns) and terminal_ns > 0
        and isinstance(files, list)
        and len(files) == len(corrected_outputs)
        and all(isinstance(item, dict) for item in files)
        and [item.get("path") for item in files] == corrected_outputs
    ):
        return None
    for item in files:
        identity = _local_file_record_identity(item)
        if not (
            identity is not None
            and item.get("regular_file") is True
            and item.get("symlink_free") is True
            and statmod.S_ISREG(identity[2])
            and identity[3] == 1
            and identity[5] >= bound_ns
            and identity[6] >= bound_ns
            and max(identity[5], identity[6]) <= terminal_ns
            and item.get("fingerprint") == _local_identity_fingerprint(
                str(item.get("path") or ""), identity)
        ):
            return None
    return witness


def _correction_reuse_outputs_match(
    route_recovery: dict[str, Any],
    step: dict[str, Any],
    run_id: str,
) -> bool:
    """The current outputs must be exactly those frozen by this correction."""
    current = sorted(
        str(item) for item in (step.get("expected_outputs") or [])
        if str(item).strip()
    )
    if route_recovery.get("local_execution_reused") is True:
        return bool(current and _valid_local_correction_witness(
            route_recovery, step, run_id))
    if not current and route_recovery.get("external_execution_reused") is True:
        return True
    witness = route_recovery.get("external_output_repoint_witness")
    return bool(
        isinstance(witness, dict)
        and sorted(str(item) for item in (witness.get("verified_outputs") or []))
        == current
    )


def _frozen_route_step_definition_hash(
    record: dict[str, Any],
    step_id: str,
) -> str:
    """Recover a legacy receipt's full step identity from its frozen version."""
    metadata = record.get("metadata")
    content = record.get("content")
    content_hash = record.get("content_hash")
    if not (
        record.get("type") == "declared_route"
        and record.get("produced_by_node_type") == "experiment"
        and isinstance(metadata, dict)
        and metadata.get("frozen") is True
        and isinstance(content, str)
        and isinstance(content_hash, str)
        and content_hash == _content_hash(content)
    ):
        return ""
    normalized = normalize_declared_route(content)
    if not normalized["valid"]:
        return ""
    matches = [
        step for step in normalized["route"]["steps"]
        if str(step.get("id") or "") == step_id
    ]
    return step_definition_hash(matches[0]) if len(matches) == 1 else ""


def _recovery_lineage(
    state: Any,
    *,
    fail_on_read_error: bool = False,
) -> dict[str, list[dict[str, Any]]]:
    """纠正 lineage：各**冻结**路线版本 metadata 里的恢复收据，按 attempt_id 收集，版本升序、末位为头。

    权威事实就是账本行。每次带 recovery_basis 的修订都把收据与路线同一次落盘
    （``_declare_execution_route``），这里只读回，不另存、不逐版复制——之后不带 basis
    的修订不会把已验证的纠正弄丢（2026-09-13 审判：单槽只看当前版本，任何一次无关
    修订都让已验证外部步骤回到 pending；第二次纠正也会覆盖第一次）。

    ``artifact_versions`` 的 metadata 取自账本 save 行，正文读不到也在；只收冻结
    版本，未冻结的草稿不算已生效的纠正。路线身份按 run 区分（_canonical_route_name）；
    沿用旧固定名的 run 里修订保留原产出方，所以仍不按 ``produced_by_run_id`` 过滤，
    attempt_id 本身就区分了别的 run。
    """
    lineage: dict[str, list[dict[str, Any]]] = {}
    try:
        versions = state.artifact_versions(_canonical_route_artifact_id(state))
    except Exception:
        if fail_on_read_error:
            raise
        return lineage
    for version in versions or []:
        metadata = version.get("metadata") if isinstance(version, dict) else None
        if not isinstance(metadata, dict) or metadata.get("frozen") is not True:
            continue
        receipt = metadata.get(_RECOVERY_METADATA_KEY)
        if not isinstance(receipt, dict) or receipt.get("recovery_validated") is not True:
            continue
        attempt_id = str(receipt.get("attempt_id") or "").strip()
        if attempt_id:
            entry = {**receipt, "route_version": version.get("version")}
            if not str(entry.get("next_step_definition_hash") or ""):
                entry["next_step_definition_hash"] = (
                    _frozen_route_step_definition_hash(
                        version,
                        str(entry.get("route_step_id") or ""),
                    )
                )
            lineage.setdefault(attempt_id, []).append(entry)
    return lineage


def _correction_head_matches_step(
    head: dict[str, Any] | None,
    step: dict[str, Any] | None,
    run_id: str = "",
) -> bool:
    """一条 lineage 头对当前步骤是否仍然有效（方案 C 的三条件）。

    头存在且是 expected_outputs 纠正；头核过的完整步骤身份和执行契约都仍是当前
    步骤；当前输出就是头核过的那组（清空，或恰为改指向见证核过的文件）。任何一条
    不成立都失败关闭，不回退到 transcript，也不因为"没有见证字段"就跳过核对。
    """
    return bool(
        isinstance(head, dict) and isinstance(step, dict)
        and head.get("recovery_validated") is True
        and head.get("observed_failure_class") == "expected_outputs_missing"
        and head.get("expected_outputs_changed") is True
        and (
            head.get("external_execution_reused") is True,
            head.get("local_execution_reused") is True,
            str((head.get("attempt_receipt") or {}).get("source_event") or ""),
        ) in {
            (True, False, "route_step_external_execution_verified"),
            (False, True, "route_step_outcome"),
        }
        and str(head.get("route_step_id") or "") == str(step.get("id") or "")
        and str(head.get("next_step_definition_hash") or "")
        == step_definition_hash(step)
        and str(head.get("next_step_execution_contract_hash") or "")
        == step_execution_contract_hash(step)
        and _correction_reuse_outputs_match(head, step, run_id)
    )


def _reused_exact_output_correction(
    lineage: dict[str, list[dict[str, Any]]],
    step: dict[str, Any],
    run_id: str,
) -> dict[str, Any] | None:
    """Return the newest still-active immutable-attempt correction."""
    candidates = [
        entries[-1] for entries in lineage.values()
        if entries and _correction_head_matches_step(
            entries[-1], step, run_id)
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda entry: int(entry.get("route_version") or 0))


def _local_correction_limitation(
    receipt: dict[str, Any],
) -> dict[str, Any] | None:
    """Project a receipt already accepted by the correction-head matcher."""
    witness = receipt.get("local_output_correction_witness")
    attempt_receipt = receipt.get("attempt_receipt")
    if not isinstance(witness, dict) or not isinstance(attempt_receipt, dict):
        return None
    authoritative_receipt = {
        key: value for key, value in receipt.items() if key != "route_version"
    }
    return {
        "attempt_id": str(receipt.get("attempt_id") or ""),
        "route_step_id": str(receipt.get("route_step_id") or ""),
        "witness_scope": witness["witness_scope"],
        "physical_postcondition_verified": True,
        "semantic_identity_independently_verified": False,
        "verified_outputs": deepcopy(witness.get("verified_outputs") or []),
        "files": deepcopy(witness.get("files") or []),
        "validated_recovery_receipt_source": {
            "route_artifact_id": str(
                attempt_receipt.get("route_artifact_id") or ""),
            "original_route_version": attempt_receipt.get("route_version"),
            "correction_route_version": receipt.get("route_version"),
            "source_event": str(attempt_receipt.get("source_event") or ""),
            "validated_recovery_receipt_sha256": _content_hash(json.dumps(
                authoritative_receipt,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )),
        },
    }


def _correction_limitation_check(
    limitations: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if not limitations:
        return None
    return {
        "name": LOCAL_CORRECTION_LIMITATION_CHECK,
        "passed": True,
        "evidence": {"corrections": limitations},
    }


def route_correction_witness_disclosure(state: Any) -> dict[str, Any]:
    """Project one active view from the route-owned recovery receipts."""
    snapshot = build_route_snapshot(state, require_recovery_lineage=True)
    if snapshot.get("status") != "ready":
        if snapshot.get("reason") == "route_not_declared":
            limitations: list[dict[str, Any]] = []
        else:
            raise RuntimeError(
                "canonical route unavailable while deriving correction "
                f"disclosure: {snapshot.get('reason') or snapshot.get('status')}"
            )
    else:
        limitations = deepcopy(
            snapshot.get("correction_witness_limitations") or [])
    return {
        "check_name": LOCAL_CORRECTION_LIMITATION_CHECK,
        "limitations": limitations,
        "check": _correction_limitation_check(limitations),
    }


def _attempt_step_contract(
    state: Any, events: list[dict[str, Any]], attempt_id: str,
) -> tuple[str, list[str] | None]:
    """该 attempt 所属步骤在**当前** canonical route 里的执行契约 hash 与 expected_outputs。

    读不到路线或步骤时返回 ``("", None)``：钉住的依据无从对照，按失败关闭处理。
    """
    step_id = next((
        str(event.get("route_step_id") or "") for event in events
        if event.get("event") == "route_step_bound"
        and str(event.get("attempt_id") or "") == attempt_id
    ), "")
    if not step_id:
        return "", None
    loaded = load_canonical_route(state)
    if loaded.get("status") != "ready":
        return "", None
    step = next((
        candidate for candidate in ((loaded.get("route") or {}).get("steps") or [])
        if str(candidate.get("id") or "") == step_id
    ), None)
    if not isinstance(step, dict):
        return "", None
    return step_execution_contract_hash(step), sorted(
        str(item) for item in (step.get("expected_outputs") or []) if str(item).strip())


def _pinned_supersession_holds(
    event: dict[str, Any], current_contract_hash: str, current_outputs: list[str] | None,
) -> bool:
    """钉住的 supersession 是否仍对应当前步骤契约。"""
    pinned_hash = str(event.get("supersession_step_execution_contract_hash") or "")
    if pinned_hash:
        return bool(current_contract_hash) and pinned_hash == current_contract_hash
    # 本字段之前写下的钉住：比较当时核过的输出与当前声明。
    basis = event.get("supersession_basis_expected_outputs")
    if basis is None:
        basis = event.get("verified_output_specs")
    return current_outputs is not None and sorted(
        str(item) for item in (basis or []) if str(item).strip()) == current_outputs


def _validated_expected_outputs_correction(
    state: Any,
    events: list[dict[str, Any]],
    attempt_id: str,
) -> list[str] | None:
    """已验证的 expected_outputs 纠正 → 返回当前 route 的修正后 expected_outputs；否则 None。

    反作弊门：只有该 attempt 的纠正 lineage 头（``_recovery_lineage``）对当前步骤仍然
    有效时，才能解开一个缓存的 ``expected_outputs_missing`` 投影。裸改路线清空或改回
    expected_outputs 而未走恢复验证的，这里返回 None——投影保持锁死。

    不再回退到 transcript 的 ``declared_route_recovery_basis`` 事件：那条回退原本只为
    metadata 收据出现之前的旧 run 保留，事件不带见证，却会在单槽丢失后被当成依据
    （2026-09-13 审判：借此伪造出完整成功）。C1 记录层之下没有这批旧 run。
    ``events`` 参数保留给调用方签名，本函数不再读它。
    """
    del events
    loaded = load_canonical_route(state)
    if loaded.get("status") != "ready":
        return None
    head = (_recovery_lineage(state).get(attempt_id) or [None])[-1]
    if not isinstance(head, dict):
        return None
    step_id = str(head.get("route_step_id") or "")
    step = next((
        candidate
        for candidate in ((loaded.get("route") or {}).get("steps") or [])
        if str(candidate.get("id") or "") == step_id
    ), None)
    if not _correction_head_matches_step(
        head, step, str(getattr(state, "run_id", "") or "")
    ):
        return None
    return [
        str(item) for item in (step.get("expected_outputs") or [])
        if str(item).strip()
    ]


def _validated_precomputed_output_observation(
    state: Any,
    events: list[dict[str, Any]],
    attempt_id: str,
    observation: Any,
) -> tuple[list[str], list[str], list[str]] | None:
    """Validate one read-only output observation for reuse by the writer.

    The completion gate owns the decision to observe early, before immutable
    closure and cleanup. Reusing that exact observation avoids a second glob
    and its TOCTOU window, while the current route contract still owns which
    output specs are admissible.
    """
    if not isinstance(observation, dict):
        return None
    contract_hash, contract_outputs = _attempt_step_contract(
        state, events, attempt_id
    )
    expected = observation.get("expected_outputs")
    verified = observation.get("verified_output_specs")
    paths = observation.get("verified_outputs")
    missing = observation.get("missing_expected_outputs")
    if not all(
        isinstance(value, list) for value in (expected, verified, paths, missing)
    ):
        return None
    expected = [str(item) for item in expected if str(item).strip()]
    verified = [str(item) for item in verified if str(item).strip()]
    paths = [str(item) for item in paths if str(item).strip()]
    missing = [str(item) for item in missing if str(item).strip()]
    if (
        observation.get("status") != "ready"
        or str(observation.get("attempt_id") or "") != attempt_id
        or not contract_hash
        or str(observation.get("step_execution_contract_hash") or "")
        != contract_hash
        or contract_outputs is None
        or sorted(expected) != contract_outputs
        or sorted([*verified, *missing]) != sorted(expected)
        or bool(observation.get("passed")) != (not missing)
    ):
        return None
    return verified, sorted(set(paths)), missing


def _precomputed_output_observations(
    observation: Any,
) -> tuple[list[dict[str, Any]], bool] | None:
    """完成门早期观测里随附的产物身份收据（P0a v4）；形状不对就当没有。"""
    if not isinstance(observation, dict):
        return None
    rows = observation.get("output_observations")
    if not isinstance(rows, list) or not all(
        isinstance(row, dict) and str(row.get("path") or "") and row.get("kind")
        for row in rows
    ):
        return None
    return [dict(row) for row in rows], bool(
        observation.get("output_observations_truncated"))


def record_external_route_execution_verification(
    state: Any,
    *,
    external_job_ref: dict[str, Any],
    terminal: bool,
    success_verified: bool,
    success_evidence: dict[str, Any],
    expected_output_observation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """把 scheduler/领域成功终态投影为 route fact，而不是 workflow 归档。

    调用方必须先通过受管 external-job verifier 得到终态与成功证据。本函数只
    接受完整 identity 与精确 route attempt，并在同一 append-only transcript
    中写一次幂等收据；失败、取消、未知状态都不会推进 route complete。
    """
    evidence = success_evidence if isinstance(success_evidence, dict) else {}
    scheduler_terminated = evidence.get("scheduler_terminated") is True
    if not (
        terminal is True
        and success_verified is True
        and evidence.get("verified") is True
        # 退出码 0，或提交时锚定任务原文声明的预期终止被机械判定为符合（第 5 步 5b）。
        # 预期终止这条只对"作业自己结束"有效：被调度器杀掉的作业即便 evidence 里
        # 还带着 termination_matched=True，也不能从这条侧门解锁 —— 判据在两个产生端
        # 都已收紧，这里再兜一层，防止将来有第三个产生端漏带。
        and (evidence.get("succeeded") is True
             or (evidence.get("termination_matched") is True
                 and not scheduler_terminated))
    ):
        return {
            "status": "error",
            "reason": "external_execution_not_successfully_verified",
        }
    if not isinstance(external_job_ref, dict):
        return {"status": "error", "reason": "route_external_identity_invalid"}
    reference = {
        **external_job_ref,
        **_normalized_external_identity(external_job_ref),
    }
    expected_key = _external_receipt_key(reference)
    if not expected_key[0] or not expected_key[5]:
        return {"status": "error", "reason": "route_external_identity_invalid"}
    if expected_key[0] == "local" and not _valid_local_external_identity(
        reference
    ):
        return {
            "status": "error",
            "reason": "route_local_container_identity_incomplete",
        }

    events, warnings = _read_transcript_events(state)
    if _current_blocking_event_history_warning(state) is not None:
        return {"status": "error", "reason": "invalid_event_history"}
    receipts = _external_submission_receipts(events)
    identity_matches = [
        (attempt_id, receipt)
        for attempt_id, receipt in receipts
        if _external_receipt_key(receipt) == expected_key
    ]
    requested_attempt = str(reference.get("route_attempt_id") or "").strip()
    if identity_matches and not requested_attempt:
        return {
            "status": "error",
            "reason": "route_external_attempt_missing",
        }
    if identity_matches and requested_attempt not in {
        attempt_id for attempt_id, _receipt in identity_matches
    }:
        return {
            "status": "error",
            "reason": "route_external_attempt_mismatch",
            "route_attempt_id": requested_attempt,
        }
    matches = [
        (attempt_id, receipt)
        for attempt_id, receipt in identity_matches
        if attempt_id == requested_attempt
    ]
    if len(matches) != 1:
        route_declared, route = _external_route_declared(state)
        if not route_declared:
            return {"status": "not_applicable", "reason": "route_not_declared"}
        attempt_receipts = [
            receipt for attempt_id, receipt in receipts
            if attempt_id == requested_attempt
        ]
        return {
            "status": "error",
            "reason": (
                "route_external_identity_mismatch"
                if attempt_receipts or not matches
                else "route_submission_ambiguous"
            ),
            "route_status": route.get("status"),
            "route_reason": route.get("reason"),
        }
    attempt_id, authoritative_receipt = matches[0]
    precomputed_outputs = None
    if expected_output_observation is not None:
        precomputed_outputs = _validated_precomputed_output_observation(
            state,
            events,
            attempt_id,
            expected_output_observation,
        )
        if precomputed_outputs is None:
            return {
                "status": "error",
                "reason": "route_expected_output_observation_invalid",
                "attempt_id": attempt_id,
            }
    # identity key 相等仍不足以信任 caller；事件落盘采用提交收据的原字段，
    # route_attempt_id 则来自已绑定 attempt，而不是由 caller 自由声明。
    authoritative_ref = {
        "scheduler": authoritative_receipt.get("scheduler"),
        "job_id": authoritative_receipt.get("job_id"),
        "namespace": authoritative_receipt.get("namespace"),
        "launch_host": authoritative_receipt.get("launch_host"),
        "scheduler_cluster": authoritative_receipt.get("scheduler_cluster"),
        "resource_uid": authoritative_receipt.get("resource_uid"),
        "submission_nonce": authoritative_receipt.get("submission_nonce"),
        "process_group_id": authoritative_receipt.get("process_group_id"),
        "process_start_ticks": authoritative_receipt.get("process_start_ticks"),
        "container_runtime_id": authoritative_receipt.get(
            "container_runtime_id"),
        "route_attempt_id": attempt_id,
    }
    digest = _external_verification_digest(authoritative_ref, evidence)
    matching_verifications = [
        event for event in events
        if event.get("event") == "route_step_external_execution_verified"
        and (
            str(event.get("attempt_id") or "") == attempt_id
            or _external_receipt_key(event) == expected_key
        )
    ]
    # 同一外部 identity 出现在另一个 attempt 的验证事件里：**记账，不拒绝**。
    #
    # 受支持路径上构造不出这个前提：_external_receipt_key 的第 7 位是
    # submission_nonce，而它在提交期被硬绑成 attempt_id
    # （resource_manager.py 的 submission_nonce=route_binding.attempt_id），
    # 身份解析期又有一道更早、逐字更强的墙 —— receipt["submission_nonce"]
    # != attempt_id 直接 route_external_nonce_mismatch。两个不同 attempt
    # 拿不到相同的 key。
    #
    # 而它一旦被外部改写 transcript 之类的手段触发，后果比它防的事更糟：
    # finalize 与 cancel 双双返回 route_submission_ambiguous、snapshot 卡在
    # in_progress，全仓没有任何否定通道 —— 无出口死路。节点规则要求每个可达
    # 非终态至少有一个合法出口，也把"增加拒绝却没有恢复和终态出口"列为
    # 不得作为默认方案的症状补丁。
    #
    # 真正的伪造威胁（在**本** attempt 上凭空造一条 success）由下面
    # len(prior) != 1 的既有判据挡着，那条有测试覆盖。
    cross_attempt_identity_conflict = sorted({
        str(event.get("attempt_id") or "")
        for event in matching_verifications
        if str(event.get("attempt_id") or "") != attempt_id
        and _external_receipt_key(event) == expected_key
    })
    prior = _active_attempt_projection_events(
        state,
        events,
        event_type="route_step_external_execution_verified",
        attempt_id=attempt_id,
    )
    if prior:
        if len(prior) != 1:
            return {
                "status": "error",
                "reason": "route_external_execution_verification_conflict",
                "attempt_id": attempt_id,
            }
        previous = prior[0]
        identical = (
            str(previous.get("attempt_id") or "") == attempt_id
            and _external_receipt_key(previous) == expected_key
            and str(previous.get("verification_digest") or "") == digest
        )
        if not identical:
            return {
                "status": "error",
                "reason": "route_external_execution_verification_conflict",
                "attempt_id": attempt_id,
                "previous_verification_digest": previous.get(
                    "verification_digest"),
            }
        if previous.get("failure_class") == "expected_outputs_missing":
            corrected = _validated_expected_outputs_correction(
                state, events, attempt_id)
            if corrected is None:
                # 无已验证纠正 → 维持锁死（反作弊：不许裸清 expected_outputs 蒙混）。
                return {
                    "status": "error",
                    "reason": "route_expected_outputs_missing",
                    "already_projected": True,
                    "attempt_id": attempt_id,
                    "missing_expected_outputs": previous.get(
                        "missing_expected_outputs") or [],
                }
            # 已验证纠正：拿当前 route 的修正 expected_outputs 对同一外部终态重评。
            rebased_binding = {
                **(next((
                    event for event in events
                    if event.get("event") == "route_step_bound"
                    and str(event.get("attempt_id") or "") == attempt_id
                ), {})),
                "expected_outputs": list(corrected),
            }
            if precomputed_outputs is None:
                corrected_specs, corrected_paths, corrected_missing = (
                    _verify_expected_outputs(rebased_binding)
                )
            else:
                corrected_specs, corrected_paths, corrected_missing = (
                    precomputed_outputs
                )
            if corrected_missing:
                return {
                    "status": "error",
                    "reason": "route_expected_outputs_missing",
                    "attempt_id": attempt_id,
                    "missing_expected_outputs": corrected_missing,
                }
            corrected_observations = (
                _precomputed_output_observations(expected_output_observation)
                or _output_observations(rebased_binding, corrected_specs)
            )
            corrected_projection = {
                "attempt_id": attempt_id,
                **authoritative_ref,
                "verification_status": "success",
                "route_outcome": "success",
                "verification_digest": digest,
                "verification_receipt": {
                    "terminal": True,
                    "success_verified": True,
                    "success_evidence": evidence,
                },
                "domain_receipts": [authoritative_ref],
                "verified_output_specs": corrected_specs,
                "verified_outputs": corrected_paths,
                "output_observations": corrected_observations[0],
                "output_observations_truncated": corrected_observations[1],
                "supersedes_failure_class": "expected_outputs_missing",
                # 依据随事实一起冻住：此刻 corrected is not None 已经验过，
                # 之后的合法路线修订不得反悔这条 supersession。
                "supersession_basis_validated": True,
                "supersession_basis_expected_outputs": list(corrected),
                # 钉住只担保这份契约；契约后来变了，读取侧丢弃这条钉住（方案 C）。
                "supersession_step_execution_contract_hash": _attempt_step_contract(
                    state, events, attempt_id)[0],
            }
            if cross_attempt_identity_conflict:
                corrected_projection["cross_attempt_identity_conflict"] = (
                    cross_attempt_identity_conflict)
            try:
                state.append_transcript(
                    "route_step_external_execution_verified",
                    **corrected_projection)
            except Exception as exc:
                return {
                    "status": "error",
                    "reason": (
                        "route_external_execution_verification_"
                        "persistence_failed"),
                    "error_type": type(exc).__name__,
                    "attempt_id": attempt_id,
                }
            return {"status": "success", **corrected_projection}
        return {
            "status": "success",
            "already_projected": True,
            "attempt_id": attempt_id,
            "verification_digest": digest,
        }

    bindings = {
        str(event.get("attempt_id") or ""): event
        for event in events
        if event.get("event") == "route_step_bound"
    }
    if precomputed_outputs is None:
        verified_specs, verified_paths, missing_outputs = (
            _verify_expected_outputs(bindings.get(attempt_id) or {})
        )
    else:
        verified_specs, verified_paths, missing_outputs = precomputed_outputs
    observations = (
        _precomputed_output_observations(expected_output_observation)
        or _output_observations(bindings.get(attempt_id) or {}, verified_specs)
    )
    projection = {
        "attempt_id": attempt_id,
        **authoritative_ref,
        "verification_status": (
            "expected_outputs_missing" if missing_outputs else "success"),
        "route_outcome": "failed" if missing_outputs else "success",
        "verification_digest": digest,
        "verification_receipt": {
            "terminal": True,
            "success_verified": True,
            "success_evidence": evidence,
        },
        "domain_receipts": [authoritative_ref],
        "verified_output_specs": verified_specs,
        "verified_outputs": verified_paths,
        # P0a v4：外部作业的产物身份收据（完成门早期观测优先，其次写入时观测）。
        "output_observations": observations[0],
        "output_observations_truncated": observations[1],
    }
    if missing_outputs:
        projection.update({
            "failure_class": "expected_outputs_missing",
            "missing_expected_outputs": missing_outputs,
        })
    try:
        if cross_attempt_identity_conflict:
            # 同一外部作业被另一个 attempt 也声称验证过 —— 事实进账本，
            # 由下游按出处判定，不在这里替它做裁决。
            projection["cross_attempt_identity_conflict"] = (
                cross_attempt_identity_conflict)
        state.append_transcript(
            "route_step_external_execution_verified", **projection)
    except Exception as exc:
        return {
            "status": "error",
            "reason": "route_external_execution_verification_persistence_failed",
            "error_type": type(exc).__name__,
            "attempt_id": attempt_id,
        }
    if missing_outputs:
        return {
            "status": "error",
            "reason": "route_expected_outputs_missing",
            **projection,
        }
    return {"status": "success", **projection}


_ROUTE_SCOPED_EVENT_PREFIXES = (
    "route_", "execution_route", "declared_route",
)
# 只用不可伪造、跨 run 唯一的 identity 片段做痕迹扫描：命中即"说不清归属"。
_EXTERNAL_IDENTITY_TRACE_FIELDS = (
    "job_id", "submission_nonce", "container_runtime_id", "resource_uid",
)


def _external_identity_trace_tokens(identity: dict[str, Any]) -> set[str]:
    return {
        str(identity.get(field) or "").strip()
        for field in _EXTERNAL_IDENTITY_TRACE_FIELDS
        if str(identity.get(field) or "").strip()
    }


def _mentions_identity_token(
    value: Any, tokens: set[str], depth: int = 0,
) -> bool:
    if depth > 8:
        # 深到读不动的结构不能当作"没提过"，按提过处理（fail-closed）。
        return True
    if isinstance(value, dict):
        return any(
            _mentions_identity_token(item, tokens, depth + 1)
            for item in value.values()
        )
    if isinstance(value, (list, tuple)):
        return any(
            _mentions_identity_token(item, tokens, depth + 1) for item in value
        )
    if isinstance(value, bool):
        return False
    if isinstance(value, (str, int)):
        return str(value).strip() in tokens
    return False


def describe_external_route_submission_presence(
    state: Any,
    *,
    scheduler: str,
    job_id: str,
    namespace: str | None = None,
    launch_host: str | None = None,
    scheduler_cluster: str | None = None,
    resource_uid: str | None = None,
    submission_nonce: str | None = None,
    process_group_id: str | None = None,
    process_start_ticks: str | int | None = None,
    container_runtime_id: str | None = None,
) -> dict[str, Any]:
    """正面判定一个 external job 是否属于**本 run** 的执行路线。

    返回 status：

    - ``present``：本 run 的路线里有该 job 的提交记录（投影有对象）；
    - ``absent``：本 run 声明过带 external 效应的路线，且路线事件里机械确认
      不存在任何指向该 job 的痕迹 —— 即跨 run 遗留作业，本 run 没有任何路线
      终态需要为它投影；
    - ``not_applicable``：本 run 没声明带 external 效应的路线，投影本就不适用；
    - ``indeterminate``：事件历史读不动/有半信半疑的痕迹 —— 一律按"确认不了"
      处理，调用方必须保持既有拒绝路径。
    """
    identity = _normalized_external_identity({
        "scheduler": scheduler,
        "job_id": job_id,
        "namespace": namespace,
        "launch_host": launch_host,
        "scheduler_cluster": scheduler_cluster,
        "resource_uid": resource_uid,
        "submission_nonce": submission_nonce,
        "process_group_id": process_group_id,
        "process_start_ticks": process_start_ticks,
        "container_runtime_id": container_runtime_id,
    })
    if not identity["scheduler"] or not identity["job_id"]:
        return {
            "status": "indeterminate",
            "reason": "external_identity_incomplete",
            "external_identity": identity,
        }
    try:
        events, transcript_warnings = _read_transcript_events(state)
    except Exception as exc:
        return {
            "status": "indeterminate",
            "reason": "route_transcript_unreadable",
            "error_type": type(exc).__name__,
            "external_identity": identity,
        }
    blocking = _blocking_event_history_warning(transcript_warnings)
    if blocking is not None:
        return {
            "status": "indeterminate",
            "reason": blocking,
            "external_identity": identity,
        }
    try:
        if _current_blocking_event_history_warning(state) is not None:
            return {
                "status": "indeterminate",
                "reason": "invalid_event_history",
                "external_identity": identity,
            }
        route_declared, _loaded = _external_route_declared(state)
        receipts = _external_submission_receipts(events)
    except Exception as exc:
        return {
            "status": "indeterminate",
            "reason": "route_history_audit_failed",
            "error_type": type(exc).__name__,
            "external_identity": identity,
        }
    for attempt_id, receipt in receipts:
        if _external_receipt_matches_query(receipt, identity):
            return {
                "status": "present",
                "reason": "route_submission_recorded_in_current_route",
                "attempt_id": attempt_id,
                "external_identity": identity,
                "route_declared": route_declared,
            }
    tokens = _external_identity_trace_tokens(identity)
    route_events = [
        event for event in events
        if str(event.get("event") or "").startswith(
            _ROUTE_SCOPED_EVENT_PREFIXES)
    ]
    traces = [
        str(event.get("event") or "")
        for event in route_events
        if _mentions_identity_token(event, tokens)
    ]
    if traces:
        # 身份线索出现在本 run 的路线事件里却匹配不上任何提交：归属说不清，
        # 不许当作跨 run 遗留。
        return {
            "status": "indeterminate",
            "reason": "route_identity_trace_without_submission",
            "traces": sorted(set(traces))[:8],
            "external_identity": identity,
            "route_declared": route_declared,
        }
    if not route_declared:
        return {
            "status": "not_applicable",
            "reason": "route_external_effect_not_declared",
            "external_identity": identity,
            "route_declared": False,
        }
    return {
        "status": "absent",
        "reason": "no_submission_in_current_route",
        "external_identity": identity,
        "route_declared": True,
        "scanned_route_events": len(route_events),
        "scanned_route_submissions": len(receipts),
    }


def record_external_route_finalization(
    state: Any,
    *,
    scheduler: str,
    job_id: str,
    namespace: str | None,
    launch_host: str | None = None,
    scheduler_cluster: str | None = None,
    resource_uid: str | None = None,
    submission_nonce: str | None = None,
    process_group_id: str | None = None,
    process_start_ticks: str | int | None = None,
    container_runtime_id: str | None = None,
    domain_outcome: str,
    evidence_artifact_id: str,
    termination_matched: bool = False,
) -> dict[str, Any]:
    """归档 external workflow 终态；成功路线由 execution verification 推进。

    对升级前尚无 execution verification 的科学/operation finalizer，本兼容入口
    会先写独立的执行终态收据，再写归档事件。已经由 operation completion 写过
    收据时，本函数只归档，不改变 route fact。
    """
    events, warnings = _read_transcript_events(state)
    identity_query = {
        "scheduler": scheduler,
        "namespace": namespace,
        "launch_host": launch_host,
        "scheduler_cluster": scheduler_cluster,
        "resource_uid": resource_uid,
        "submission_nonce": submission_nonce,
        "process_group_id": process_group_id,
        "process_start_ticks": process_start_ticks,
        "container_runtime_id": container_runtime_id,
        "job_id": job_id,
    }
    identity_query = _normalized_external_identity(identity_query)
    if (
        identity_query["scheduler"] == "local"
        and not _valid_local_external_identity(identity_query)
    ):
        return {
            "status": "error",
            "reason": "route_local_container_identity_incomplete",
        }
    if _current_blocking_event_history_warning(state) is not None:
        return {"status": "error", "reason": "invalid_event_history"}
    matches = [
        (attempt_id, receipt)
        for attempt_id, receipt in _external_submission_receipts(events)
        if _external_receipt_matches_query(receipt, identity_query)
    ]
    matches = list({
        (attempt_id, _external_receipt_key(receipt)): (attempt_id, receipt)
        for attempt_id, receipt in matches
    }.values())
    if len(matches) != 1:
        route_declared, _route = _external_route_declared(state)
        return {
            "status": "error" if matches or route_declared else "not_applicable",
            "reason": "route_submission_not_found" if not matches else "route_submission_ambiguous",
        }
    attempt_id, authoritative_receipt = matches[0]
    expected_key = _external_receipt_key(authoritative_receipt)
    existing = [
        event for event in events
        if event.get("event") == "route_step_external_finalized"
        and _external_receipt_key(event) == expected_key
    ]
    same = _active_attempt_projection_events(
        state,
        events,
        event_type="route_step_external_finalized",
        attempt_id=attempt_id,
    )
    supersedes_finalization_failure = ""
    if same:
        # 取最新的一条 active 投影（基线行为）。#879 改成 same[0] 外加对
        # len(same) > 1 硬拒，而 same 的成员资格每次都按当前 route 重算 ——
        # 一次合法路线修订就能让两条 finalized 同时复活并撞上那堵墙，
        # 此后 finalize 与 cancel 同时无出口。依据钉进事件后不再反悔。
        # 真正的冲突仍由下面的 identical 兜住，而且判据从"和第一条比"
        # 加强成"**每一条** active 投影都必须一致"。
        previous = same[-1]
        identical = all(
            _external_receipt_key(candidate) == expected_key
            and str(candidate.get("domain_outcome") or "") == str(domain_outcome)
            and str(candidate.get("evidence_artifact_id") or "")
            == str(evidence_artifact_id)
            for candidate in same
        )
        if not identical:
            return {
                "status": "error",
                "reason": "route_external_finalization_conflict",
                "attempt_id": attempt_id,
                "previous_domain_outcome": previous.get("domain_outcome"),
                "previous_evidence_artifact_id": previous.get(
                    "evidence_artifact_id"),
            }
        if previous.get("failure_class") == "expected_outputs_missing":
            corrected = _validated_expected_outputs_correction(
                state, events, attempt_id)
            if corrected is None:
                # 尚未做过已验证的路线纠正 —— 锁按原样保持（反作弊：不许裸清
                # expected_outputs 蒙混）。这里 already_projected 是真的：上一次
                # 的失败投影确实已在账上，调用方读到的是那条缓存事实。
                return {
                    "status": "error",
                    "reason": "route_expected_outputs_missing",
                    "lock_reason": "not_yet_corrected",
                    "already_projected": True,
                    "attempt_id": attempt_id,
                    "missing_expected_outputs": previous.get(
                        "missing_expected_outputs") or [],
                }
            supersedes_finalization_failure = "expected_outputs_missing"
        else:
            return {
                "status": "success", "already_projected": True,
                "attempt_id": attempt_id,
            }
    success_outcomes = {
        "operation_completed", "analyzed_success", "analyzed_inconclusive",
    }
    # 提交时锚定任务原文声明了预期终止、且机械判定符合的 operation_failed：结果词仍是物理
    # 事实，路线步骤的契约（作业按任务要求的方式终止）已达成（第 5 步 5b）。
    # 计划内停止（D07）：提交时声明 planned_stop、取消已确认的本地作业。物理上没跑完
    # （succeeded=False），但按任务要求的方式结束了（termination_matched），产物照常核对。
    projects_success = domain_outcome in success_outcomes or (
        domain_outcome == "operation_failed" and termination_matched is True) or (
        domain_outcome == "stopped_as_planned")
    verified_specs: list[str] = []
    verified_paths: list[str] = []
    missing_outputs: list[str] = []
    execution_events = _active_attempt_projection_events(
        state,
        events,
        event_type="route_step_external_execution_verified",
        attempt_id=attempt_id,
    )
    if len(execution_events) > 1:
        return {
            "status": "error",
            "reason": "route_external_execution_verification_conflict",
            "attempt_id": attempt_id,
        }
    if projects_success:
        verification_receipt: dict[str, Any] | None = None
        if not execution_events:
            verification_receipt = {
                "terminal": True,
                "success_verified": True,
                "success_evidence": {
                    "verified": True,
                    "succeeded": domain_outcome in success_outcomes,
                    **({"termination_matched": True}
                       if domain_outcome not in success_outcomes else {}),
                    "source": "domain_finalization",
                    "domain_outcome": domain_outcome,
                    "evidence_artifact_id": evidence_artifact_id,
                },
            }
        elif (
            len(execution_events) == 1
            and execution_events[0].get("failure_class")
            == "expected_outputs_missing"
            and _validated_expected_outputs_correction(
                state, events, attempt_id) is not None
        ):
            # direct-finalize 也必须能完成已验证的 expected_outputs 纠正。
            # 复用旧 verification receipt，保证这是对同一终态的机械重评；
            # finalization 自己生成的 domain evidence 会改变 digest，不能替代它。
            # 这里曾经再验一遍 persisted 收据的 terminal / success_verified /
            # success_evidence.verified / success_evidence.succeeded。四项都由
            # 构造保证：唯一写入方 record_external_route_execution_verification
            # 的入口门在写任何事件之前就要求这四项全为 True
            # （external_execution_not_successfully_verified），落盘处三处写的
            # 都是字面量 True。它唯一的独立效果是把一个不可能发生的 KeyError
            # 换成一个与跨 attempt 归属冲突**撞同一个 reason 码**的信封，让
            # 调用方分不清两种完全不同的病。留 isinstance 守卫防崩即可。
            persisted = execution_events[0].get("verification_receipt")
            if isinstance(persisted, dict):
                verification_receipt = deepcopy(persisted)
        if verification_receipt is not None:
            verification = record_external_route_execution_verification(
                state,
                external_job_ref={
                    "scheduler": authoritative_receipt.get("scheduler"),
                    "job_id": authoritative_receipt.get("job_id"),
                    "namespace": authoritative_receipt.get("namespace"),
                    "launch_host": authoritative_receipt.get("launch_host"),
                    "scheduler_cluster": authoritative_receipt.get(
                        "scheduler_cluster"),
                    "resource_uid": authoritative_receipt.get("resource_uid"),
                    "submission_nonce": authoritative_receipt.get(
                        "submission_nonce"),
                    "process_group_id": authoritative_receipt.get(
                        "process_group_id"),
                    "process_start_ticks": authoritative_receipt.get(
                        "process_start_ticks"),
                    "container_runtime_id": authoritative_receipt.get(
                        "container_runtime_id"),
                    "route_attempt_id": attempt_id,
                },
                terminal=verification_receipt["terminal"],
                success_verified=verification_receipt["success_verified"],
                success_evidence=verification_receipt["success_evidence"],
            )
            if (
                verification.get("status") != "success"
                and verification.get("reason")
                != "route_expected_outputs_missing"
            ):
                return verification
            events, _warnings = _read_transcript_events(state)
            if _current_blocking_event_history_warning(state) is not None:
                return {"status": "error", "reason": "invalid_event_history"}
            execution_events = _active_attempt_projection_events(
                state,
                events,
                event_type="route_step_external_execution_verified",
                attempt_id=attempt_id,
            )
        execution = execution_events[0] if len(execution_events) == 1 else {}
        if _external_receipt_key(execution) != expected_key:
            return {
                "status": "error",
                "reason": "route_external_execution_verification_conflict",
                "attempt_id": attempt_id,
            }
        verified_specs = list(execution.get("verified_output_specs") or [])
        verified_paths = list(execution.get("verified_outputs") or [])
        missing_outputs = list(execution.get("missing_expected_outputs") or [])
        route_outcome = str(execution.get("route_outcome") or "")
        if route_outcome not in {"success", "failed"}:
            return {
                "status": "error",
                "reason": "route_external_execution_verification_conflict",
                "attempt_id": attempt_id,
            }
    elif domain_outcome == "operation_blocked":
        route_outcome = "blocked"
    elif domain_outcome == "cancelled":
        route_outcome = "cancelled"
    else:
        route_outcome = "failed"
    if execution_events and not projects_success:
        execution_event = execution_events[0]
        if not (
            _external_receipt_key(execution_event) == expected_key
            and str(execution_event.get("route_attempt_id") or "")
            == attempt_id
        ):
            return {
                "status": "error",
                "reason": "route_external_execution_verification_conflict",
                "attempt_id": attempt_id,
            }
        execution_outcome = str(execution_event.get("route_outcome") or "")
        if (
            execution_outcome == "success"
            and route_outcome != "success"
            and domain_outcome != "operation_blocked"
        ):
            return {
                "status": "error",
                "reason": "route_external_finalization_conflict",
                "attempt_id": attempt_id,
                "previous_domain_outcome": "execution_verified",
            }
    if supersedes_finalization_failure and route_outcome != "success":
        # validated route 修订只是允许重评，不把旧的成功终态验证本身改写成成功。
        # execution verification 尚未按新 expected_outputs 投影成功时，锁仍生效。
        #
        # 信封三处修正（原先这三样都在误导调用方）：
        # · already_projected 去掉 —— 本次一条事件都没投影出去，它为真会被读成
        #   "早就投影过了"，而实际语义是"这次重评没转成功，锁继续生效"。
        # · missing_expected_outputs 改回吐**本次**重评算出的清单（上面 4440 行
        #   刚从 execution 事件取出），原先回吐的是 previous 里那份陈旧缓存 ——
        #   调用方照着旧清单去补文件，补完还是过不去。
        # · 补 lock_reason 子码，与"尚未做过纠正"那一处区分开：两处共用同一个
        #   reason 码，调用方分不清自己该做纠正还是该补产物。
        return {
            "status": "error",
            "reason": "route_expected_outputs_missing",
            "lock_reason": "corrected_but_still_missing",
            "attempt_id": attempt_id,
            "missing_expected_outputs": (
                list(missing_outputs)
                or (previous.get("missing_expected_outputs") or [])),
        }
    projection = {
        "attempt_id": attempt_id,
        "scheduler": authoritative_receipt.get("scheduler"),
        "namespace": authoritative_receipt.get("namespace"),
        "launch_host": authoritative_receipt.get("launch_host"),
        "scheduler_cluster": authoritative_receipt.get("scheduler_cluster"),
        "resource_uid": authoritative_receipt.get("resource_uid"),
        "submission_nonce": authoritative_receipt.get("submission_nonce"),
        "process_group_id": authoritative_receipt.get("process_group_id"),
        "process_start_ticks": authoritative_receipt.get(
            "process_start_ticks"),
        "container_runtime_id": authoritative_receipt.get(
            "container_runtime_id"),
        "job_id": authoritative_receipt.get("job_id"),
        "route_attempt_id": attempt_id,
        "domain_outcome": domain_outcome,
        "route_outcome": route_outcome,
        "evidence_artifact_id": evidence_artifact_id,
        "verified_output_specs": verified_specs,
        "verified_outputs": verified_paths,
    }
    if supersedes_finalization_failure:
        projection["supersedes_failure_class"] = (
            supersedes_finalization_failure)
        # 同上：写入时已验过的依据钉进事件，不留给读取侧按当前 route 重算；
        # 连同当时的步骤契约 hash，契约后来变了读取侧丢弃这条钉住（方案 C）。
        projection["supersession_basis_validated"] = True
        projection["supersession_step_execution_contract_hash"] = (
            _attempt_step_contract(
                state, _read_transcript_events(state)[0], attempt_id)[0])
    elif projects_success and execution.get("supersession_basis_validated") is True:
        # verify 清单 #1：这次成功收尾靠的是一条纠正后钉住的执行核验，它只担保那份契约。
        # 收尾事件也钉同一个契约 hash——契约后来变了，两条一起丢弃、原失败复活、路线回到
        # blocked，出口是带 recovery_basis 重新纠正。不钉的话，钉住的核验被丢弃后这条成功
        # 仍在，与复活的失败冲突，路线卡进 invalid_event_history、run 内无出口。
        # 只钉纠正后的成功，不给所有成功投影加 hash。
        projection["supersession_basis_validated"] = True
        projection["supersession_step_execution_contract_hash"] = str(
            execution.get("supersession_step_execution_contract_hash") or "")
        if execution.get("supersession_basis_expected_outputs") is not None:
            projection["supersession_basis_expected_outputs"] = list(
                execution.get("supersession_basis_expected_outputs") or [])
    if missing_outputs:
        projection.update({
            "failure_class": "expected_outputs_missing",
            "missing_expected_outputs": missing_outputs,
        })
    try:
        state.append_transcript("route_step_external_finalized", **projection)
    except Exception as exc:
        return {
            "status": "error",
            "reason": "route_external_projection_persistence_failed",
            "error_type": type(exc).__name__,
            "attempt_id": attempt_id,
        }
    if missing_outputs:
        return {
            "status": "error",
            "reason": "route_expected_outputs_missing",
            **projection,
        }
    return {"status": "success", **projection}


_RECOVERY_FAILURE_CLASSES = frozenset({
    "environment", "dependency", "parameter", "source", "resource",
    "timeout", "cancelled", "execution", "expected_output", "external_identity",
})
_RECOVERY_RESERVED_ARTIFACT_TYPES = frozenset({
    "raw_results", "clean_results", "experiment_log",
    "declared_route", "pre_registration",
})
_RECOVERY_METADATA_KEY = "validated_recovery_receipt"


def _attempt_backend_output_paths(state: Any, attempt_id: str) -> set[str]:
    """本 attempt 提交时由受管后端登记下来的产物文件（stdout/stderr 及 bootstrap 日志）。

    这是"这个文件是这次作业写的"唯一**不靠事后看盘**的证据：路径在提交那一刻就进了
    job_submission，模型改不了已冻结的提交记录。2026-09-12 审查实测：只按目录名前缀认
    "作业目录"挡不住伪造——作业根在 run 的可写树里，模型自己 mkdir 一个同前缀目录、
    甚至直接往真作业目录里写一个同名文件，都能过见证。所以改指向只认这一组路径。
    """
    paths: set[str] = set()
    try:
        summaries = state.list_artifacts("job_submission")
    except Exception:
        return paths
    for summary in summaries or []:
        try:
            record = state.read_artifact(summary.get("id")) or {}
            payload = json.loads(str(record.get("content") or "") or "{}")
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        identity = {
            str(payload.get("submission_nonce") or ""),
            str(payload.get("route_attempt_id") or ""),
        }
        if attempt_id not in identity:
            continue
        for key in ("stdout_path", "stderr_path",
                    "bootstrap_stdout_path", "bootstrap_stderr_path"):
            value = str(payload.get(key) or "").strip()
            if value:
                paths.add(os.path.realpath(value))
    return paths


def _external_output_repoint_witness(
    previous_bound: dict[str, Any],
    attempt_receipt: dict[str, Any],
    attempt_id: str,
    next_outputs: list[str],
    backend_outputs: set[str] | None = None,
) -> tuple[dict[str, Any] | None, list[str]]:
    """受管外部作业的 expected_outputs 改指向：逐个核对新路径就是这次作业真实写下的文件。

    新版 AGENTS 长期不变量 1：exact-output correction 要对同一不可变结果重新机械
    验证，保留旧、新声明及依据，**不得靠清空有意义的 expected outputs 完成纠正**。
    原实现对外部作业只接受清空（2026-08-27 起），于是模型把声明改指向真实落盘位置
    也被判不算纠正，一个实际成功的作业收不了尾（2026-09-10 验收 2 活体实测）。

    放行改指向，但作业已结束、不能重跑，所以证据只能是「文件本身」，必须防止它被
    事后伪造。每条新路径都要满足：
      * 真实路径不越出绑定的工作目录，不经符号链接，是普通文件，硬链接数为 1；
      * 是本 attempt 提交时由受管后端**登记过的**产物路径（``backend_outputs``）——
        作业根在 run 的可写树里，只按目录名前缀认"作业目录"挡不住伪造：模型可以自己
        mkdir 一个同前缀目录，也可以直接往真作业目录里写同名文件（2026-09-12 审查实测）。
        提交记录在提交那一刻就冻住了，改不了，所以只认它登记的那组路径；
      * 文件名与原先报缺失的那组一一对应——挡住 results.nc 改指向 stdout 这种降级；
      * mtime 不早于作业绑定，mtime 与 ctime 都不晚于终态核验时刻——ctime 用户态改不了，
        挡住作业结束后覆盖或 touch 改时间。
    返回 (见证, 违规列表)；见证随路线恢复收据一起冻结，后续只读它。
    """
    import os
    from datetime import datetime

    violations: list[str] = []
    workdir = str(previous_bound.get("resolved_workdir") or "")
    if not workdir:
        return None, ["绑定记录缺 resolved_workdir，无法定位作业产物，不能改指向"]
    root = os.path.realpath(workdir)
    bound_ns = int(previous_bound.get("bound_at_ns") or 0)
    terminal_ns = 0
    try:
        terminal_ns = int(datetime.fromisoformat(
            str(attempt_receipt.get("recorded_at") or "").replace("Z", "+00:00")
        ).timestamp() * 1_000_000_000)
    except (TypeError, ValueError):
        pass
    slack_ns = 2_000_000_000
    allowed = set(backend_outputs or ())
    if not allowed:
        return None, [
            "找不到本 attempt 的受管提交记录（stdout/stderr 登记路径），"
            "无法证明任何文件是这次作业写的；改指向不可用，"
            "调用 report_blocker 后以 operation_blocked 如实收尾"]
    old_names = sorted(
        os.path.basename(str(item))
        for item in (attempt_receipt.get("missing_expected_outputs") or [])
    )
    new_names = sorted(os.path.basename(str(item)) for item in next_outputs)
    if new_names != old_names:
        violations.append(
            f"改指向后的文件名必须与原先报缺失的那组一一对应（原 {old_names}，"
            f"新 {new_names}）；改文件名不是纠正路径，是换了一个产物")
    files: list[dict[str, Any]] = []
    for rel in next_outputs:
        rel = str(rel)
        if any(ch in rel for ch in "*?["):
            violations.append(f"{rel}：改指向不接受通配符，必须是具体文件")
            continue
        candidate = os.path.join(workdir, rel)
        real = os.path.realpath(candidate)
        if real != root and not real.startswith(root + os.sep):
            violations.append(f"{rel}：解析后越出本步骤的工作目录 {workdir}")
            continue
        if os.path.normpath(os.path.abspath(candidate)) != real:
            violations.append(f"{rel}：路径经过符号链接，不能作为作业产物证据")
            continue
        try:
            st = os.lstat(real)
        except OSError:
            violations.append(f"{rel}：文件不存在")
            continue
        if not statmod.S_ISREG(st.st_mode):
            violations.append(f"{rel}：不是普通文件")
            continue
        if st.st_nlink != 1:
            violations.append(f"{rel}：存在硬链接，无法证明是本次作业写下的")
            continue
        if real not in allowed:
            violations.append(
                f"{rel}：不是本 attempt 提交时登记的产物路径。改指向只认受管后端自己"
                "登记下来的文件（作业的 stdout/stderr），因为作业根在本 run 的可写树里，"
                "盘上看到的文件证明不了是谁写的")
            continue
        if any(item["path"] for item in files if os.path.realpath(
                os.path.join(workdir, item["path"])) == real):
            violations.append(f"{rel}：与另一条声明产物指向同一个文件，一份顶不了两份")
            continue
        if bound_ns and st.st_mtime_ns < bound_ns - slack_ns:
            violations.append(f"{rel}：修改时间早于作业开始，不是这次作业写下的")
            continue
        if terminal_ns and max(st.st_mtime_ns, st.st_ctime_ns) > terminal_ns + slack_ns:
            violations.append(
                f"{rel}：在作业终态核验之后被写入或改动过，疑似事后生成")
            continue
        files.append({"path": rel, "size": st.st_size, "inode": st.st_ino,
                      "mtime_ns": st.st_mtime_ns, "ctime_ns": st.st_ctime_ns})
    if violations:
        return None, violations
    return {"mode": "repoint", "attempt_id": attempt_id,
            "verified_outputs": [str(item) for item in next_outputs],
            "files": files}, []


def _local_file_stat_identity(stat_result: Any) -> tuple[int, ...]:
    return (
        stat_result.st_dev,
        stat_result.st_ino,
        stat_result.st_mode,
        stat_result.st_nlink,
        stat_result.st_size,
        stat_result.st_mtime_ns,
        stat_result.st_ctime_ns,
    )


def _local_output_repoint_witness(
    previous_bound: dict[str, Any],
    attempt_receipt: dict[str, Any],
    attempt_id: str,
    next_outputs: list[str],
) -> tuple[dict[str, Any] | None, list[str]]:
    """Freeze a bounded physical witness for one successful local attempt.

    Local synchronous execution has no submission-time output registry.  This
    witness therefore proves only that each corrected concrete file appeared
    or changed inside the run-shared workdir during the immutable attempt's
    bound/outcome time window.  The limitation is part of the witness itself;
    it must never be advertised as external-job provenance or as independent
    proof of the file's task-semantic identity.
    """
    violations: list[str] = []
    workdir = str(previous_bound.get("resolved_workdir") or "").strip()
    if not workdir:
        return None, ["绑定记录缺 resolved_workdir，无法定位本地 attempt 的产物"]
    if not next_outputs:
        return None, [
            "本地 exact-output correction 必须指向原 attempt 窗口内真实写下的"
            "具体文件；不能靠清空 expected_outputs 完成纠正"
        ]

    root = os.path.realpath(workdir)
    bound_ns = int(previous_bound.get("bound_at_ns") or 0)
    terminal_ns = _recorded_at_ns(attempt_receipt)
    if bound_ns <= 0:
        violations.append("绑定记录缺 bound_at_ns，无法证明文件位于 attempt 时间窗")
    if terminal_ns <= 0:
        violations.append("attempt 收据缺终态时间，无法证明文件位于 attempt 时间窗")

    old_names = sorted(
        os.path.basename(str(item))
        for item in (previous_bound.get("expected_outputs") or [])
    )
    new_names = sorted(os.path.basename(str(item)) for item in next_outputs)
    if new_names != old_names:
        violations.append(
            f"修正后的文件名必须与原 expected_outputs 一一对应（原 {old_names}，"
            f"新 {new_names}）；本路径只纠正 path base，不改变产物身份"
        )

    files: list[dict[str, Any]] = []
    seen_real_paths: set[str] = set()
    for raw in next_outputs:
        rel = str(raw).strip()
        if any(ch in rel for ch in "*?["):
            violations.append(f"{rel}：本地纠正不接受通配符，必须是具体文件")
            continue
        candidate = os.path.join(workdir, rel)
        real = os.path.realpath(candidate)
        if real != root and not real.startswith(root + os.sep):
            violations.append(f"{rel}：解析后越出本步骤工作目录 {workdir}")
            continue
        if os.path.normpath(os.path.abspath(candidate)) != real:
            violations.append(f"{rel}：路径经过符号链接，不能作为本地产物见证")
            continue
        try:
            st = os.lstat(real)
        except OSError:
            violations.append(f"{rel}：文件不存在")
            continue
        if not statmod.S_ISREG(st.st_mode):
            violations.append(f"{rel}：不是普通文件")
            continue
        if st.st_nlink != 1:
            violations.append(f"{rel}：存在硬链接，无法绑定本 attempt 的物理文件")
            continue
        if real in seen_real_paths:
            violations.append(f"{rel}：与另一条声明指向同一文件，一份顶不了两份")
            continue
        seen_real_paths.add(real)
        flags = (
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0)
        )
        try:
            descriptor = os.open(real, flags)
        except OSError:
            violations.append(f"{rel}：无法稳定打开普通文件，可能在见证期间被替换")
            continue
        try:
            before = os.fstat(descriptor)
            if not statmod.S_ISREG(before.st_mode):
                violations.append(
                    f"{rel}：稳定打开后已不再是普通文件，拒绝竞态见证")
                continue
            fingerprint = _local_identity_fingerprint(
                rel, _local_file_stat_identity(before))
            after = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        try:
            final_path_stat = os.lstat(real)
        except OSError:
            violations.append(f"{rel}：见证期间文件消失或被替换")
            continue
        identities = {
            _local_file_stat_identity(item)
            for item in (st, before, after, final_path_stat)
        }
        if len(identities) != 1:
            violations.append(
                f"{rel}：见证期间文件身份或时间发生变化，拒绝 TOCTOU 混合收据")
            continue
        st = after
        if bound_ns and min(st.st_mtime_ns, st.st_ctime_ns) < bound_ns:
            violations.append(f"{rel}：mtime/ctime 早于 attempt 绑定，不是本次写入")
            continue
        if terminal_ns and max(st.st_mtime_ns, st.st_ctime_ns) > terminal_ns:
            violations.append(f"{rel}：在 attempt 终态后被创建或改动，疑似事后伪造")
            continue
        # 051：同一次见证顺手冻结 P0a 形状的产物身份收据（lexical 路径、kind、
        # dev/ino/size、正文 sha256）。纠正后的步骤没有 outcome=success 事件，
        # ROC 的产物归属门（P0a v4）只认这种收据——没有它，纠正复用了原 attempt、
        # 成功收尾却被 no_producer_receipt 拒，只能记 partial（09-22 活体探针 p2）。
        try:
            observation = output_identity(candidate, spec=rel)
        except Exception:
            observation = None
        files.append({
            "path": rel,
            "size": st.st_size,
            "device": st.st_dev,
            "inode": st.st_ino,
            "mode": st.st_mode,
            "regular_file": True,
            "symlink_free": True,
            "link_count": st.st_nlink,
            "mtime_ns": st.st_mtime_ns,
            "ctime_ns": st.st_ctime_ns,
            "fingerprint": fingerprint,
            **({"output_observation": {**observation, "passed": True}}
               if isinstance(observation, dict) else {}),
        })
    if violations:
        return None, violations
    return {
        "mode": "repoint",
        "attempt_id": attempt_id,
        "verified_outputs": [str(item) for item in next_outputs],
        "resolved_workdir": workdir,
        "bound_at_ns": bound_ns,
        "terminal_at_ns": terminal_ns,
        "physical_postcondition_verified": True,
        "witness_scope": _LOCAL_CORRECTION_WITNESS_SCOPE,
        "semantic_identity_independently_verified": False,
        "files": files,
    }, []


def _meaningful_output_repoint_candidate(
    state: Any,
    previous_bound: dict[str, Any],
    attempt_receipt: dict[str, Any],
    attempt_id: str,
) -> list[str] | None:
    """Return an actionable, fully witnessed external-output re-point suggestion.

    This helper is advisory only: both a complete suggestion and no suggestion
    reject clearing a persisted external missing fact.  It lets the refusal name
    the exact submission-time registered stdout/stderr paths when they are safe
    to use, without treating a directory scan as provenance.
    """
    workdir = str(previous_bound.get("resolved_workdir") or "")
    if not workdir:
        return None
    root = os.path.realpath(workdir)
    wanted = [
        os.path.basename(str(item))
        for item in (attempt_receipt.get("missing_expected_outputs") or [])
    ]
    if not wanted or len(set(wanted)) != len(wanted):
        return None
    registered = _attempt_backend_output_paths(state, attempt_id)
    by_name: dict[str, list[str]] = {name: [] for name in wanted}
    for real in registered:
        name = os.path.basename(real)
        if name in by_name and (real == root or real.startswith(root + os.sep)):
            by_name[name].append(os.path.relpath(real, root))
    if any(len(paths) != 1 for paths in by_name.values()):
        return None
    candidate = [by_name[name][0] for name in wanted]
    witness, _violations = _external_output_repoint_witness(
        previous_bound, attempt_receipt, attempt_id, candidate, registered)
    return candidate if witness is not None else None


def _expected_outputs_only_adjusted(
    previous_route: dict[str, Any],
    next_route: dict[str, Any],
    route_step_id: str,
    *,
    allowed_evidence_additions: set[str] | None = None,
) -> dict[str, Any] | None:
    """上一版路线只套用本步骤新 expected_outputs 之后的样子；步骤缺失或输出没变时为 None。"""
    previous_step = next((
        step for step in (previous_route.get("steps") or [])
        if str(step.get("id") or "") == route_step_id
    ), None)
    next_step = next((
        step for step in (next_route.get("steps") or [])
        if str(step.get("id") or "") == route_step_id
    ), None)
    if not isinstance(previous_step, dict) or not isinstance(next_step, dict):
        return None
    if previous_step.get("expected_outputs") == next_step.get(
        "expected_outputs"
    ):
        return None

    adjusted = deepcopy(previous_route)
    adjusted_step = next((
        step for step in (adjusted.get("steps") or [])
        if str(step.get("id") or "") == route_step_id
    ), None)
    if not isinstance(adjusted_step, dict):
        return None
    adjusted_step["expected_outputs"] = deepcopy(
        next_step.get("expected_outputs"))
    if allowed_evidence_additions is not None:
        before = set(previous_route.get("evidence_refs") or [])
        after = list(next_route.get("evidence_refs") or [])
        if before.issubset(after) and set(after).difference(before).issubset(
                allowed_evidence_additions):
            adjusted["evidence_refs"] = deepcopy(after)
    return adjusted


def _route_changes_only_expected_outputs(
    previous_route: dict[str, Any],
    next_route: dict[str, Any],
    route_step_id: str,
    *,
    allowed_evidence_additions: set[str] | None = None,
) -> bool:
    """证明整份路线只改变一个既有步骤的 expected_outputs。

    ``allowed_evidence_additions`` 只给外部作业改指向用：模型照通用指引把支撑证据
    同时写进 recovery_basis 与 route.evidence_refs 是合理动作（2026-09-10 验收 2
    的 v4 正是这样）。只增不减、且新增的全是 recovery_basis 自己引用的证据时，
    不算"改了别的"。
    """
    adjusted = _expected_outputs_only_adjusted(
        previous_route, next_route, route_step_id,
        allowed_evidence_additions=allowed_evidence_additions)
    return adjusted is not None and adjusted == next_route


_ABSENT = object()


def _route_difference_paths(
    left: Any, right: Any, where: str = "route", *, limit: int = 20,
) -> list[str]:
    """两份路线逐字不同的 JSON 路径；步骤列表按 id 定位（``route.steps[run].goal``）。"""
    found: list[str] = []

    def keyed(items: list[Any]) -> dict[str, Any] | None:
        if not all(isinstance(item, dict) and str(item.get("id") or "") for item in items):
            return None
        by_id = {str(item["id"]): item for item in items}
        return by_id if len(by_id) == len(items) else None

    def walk(a: Any, b: Any, path: str) -> None:
        if len(found) >= limit or a == b:
            return
        if isinstance(a, dict) and isinstance(b, dict):
            for key in sorted(set(a) | set(b), key=str):
                walk(a.get(key, _ABSENT), b.get(key, _ABSENT), f"{path}.{key}")
            return
        if isinstance(a, list) and isinstance(b, list):
            left_by_id, right_by_id = keyed(a), keyed(b)
            if left_by_id is not None and right_by_id is not None:
                before = len(found)
                for item_id in dict.fromkeys([*left_by_id, *right_by_id]):
                    walk(left_by_id.get(item_id, _ABSENT),
                         right_by_id.get(item_id, _ABSENT), f"{path}[{item_id}]")
                if len(found) == before:
                    found.append(f"{path}（顺序）")
                return
            if len(a) == len(b):
                for index, (x, y) in enumerate(zip(a, b)):
                    walk(x, y, f"{path}[{index}]")
                return
        found.append(path)

    walk(left, right, where)
    return found


def _expected_output_attempt_receipt(
    attempt_events: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """验证框架已持久化的“执行成功但声明产物缺失”收据。"""
    candidates = [
        event for event in attempt_events
        if event.get("event") in {
            "route_step_outcome",
            "route_step_external_execution_verified",
        }
        and event.get("failure_class") == "expected_outputs_missing"
    ]
    if len(candidates) != 1:
        return None
    event = candidates[0]
    missing = [
        str(item).strip()
        for item in (event.get("missing_expected_outputs") or [])
        if str(item).strip()
    ]
    if not missing:
        return None

    event_type = str(event.get("event") or "")
    if event_type == "route_step_outcome":
        managed = event.get("managed_tool_receipt")
        if not (
            event.get("outcome") == "failed"
            and isinstance(managed, dict)
            and managed.get("status") == "success"
            and managed.get("returncode") in (None, 0)
        ):
            return None
        execution_receipt = {"managed_tool_receipt": deepcopy(managed)}
    else:
        verified = event.get("verification_receipt")
        if not (
            event.get("route_outcome") == "failed"
            and event.get("verification_status")
            == "expected_outputs_missing"
            and isinstance(verified, dict)
            and verified.get("terminal") is True
            and verified.get("success_verified") is True
        ):
            return None
        execution_receipt = {"verification_receipt": deepcopy(verified)}

    return {
        "source_event": event_type,
        "recorded_at": str(event.get("at") or ""),
        "missing_expected_outputs": missing,
        **execution_receipt,
    }


def _expected_output_correction_facts(
    events: list[dict[str, Any]],
    attempt_id: str,
    route_step_id: str,
) -> dict[str, Any]:
    """Derive the exact-output correction gate inputs from one event view.

    Both route-amendment validation and pre-execution refusal guidance consume
    this result.  In particular, receipt validity (including local returncode)
    must never be reimplemented in the wording layer.
    """
    attempt_events = [
        event for event in events
        if str(event.get("attempt_id") or "") == attempt_id
        and event.get("event") in {
            "route_step_bound",
            "route_step_outcome",
            "route_step_external_execution_verified",
            "route_step_external_finalized",
        }
    ]
    previous_bounds = [
        event for event in attempt_events
        if event.get("event") == "route_step_bound"
    ]
    bound = previous_bounds[0] if len(previous_bounds) == 1 else {}
    bound_matches_step = bool(
        bound
        and str(bound.get("route_step_id") or "") == route_step_id
    )
    attempt_receipt = _expected_output_attempt_receipt(attempt_events)
    bound_policy = str(bound.get("applied_policy") or "")
    receipt_source = str((attempt_receipt or {}).get("source_event") or "")
    local_candidate = bool(
        bound_matches_step
        and receipt_source == "route_step_outcome"
        and bound_policy != "managed_external_job"
    )
    external_candidate = bool(
        bound_matches_step
        and receipt_source == "route_step_external_execution_verified"
        and bound_policy == "managed_external_job"
    )
    if local_candidate:
        mode = "validated_local_attempt_receipt_no_reexecution"
        reason = "validated_local_success_receipt"
    elif external_candidate:
        mode = "validated_external_attempt_receipt_no_resubmit"
        reason = "managed_external_job"
    elif len(previous_bounds) != 1 or not bound_matches_step:
        mode = "not_applicable"
        reason = "unique_step_binding_missing"
    elif bound_policy == "managed_external_job":
        mode = "not_applicable"
        reason = "valid_external_success_receipt_missing"
    elif attempt_receipt is None:
        mode = "not_applicable"
        reason = "valid_local_success_receipt_missing"
    else:
        mode = "not_applicable"
        reason = "receipt_source_not_local"
    return {
        "attempt_events": attempt_events,
        "previous_bounds": previous_bounds,
        "attempt_receipt": attempt_receipt,
        "bound_policy": bound_policy,
        "receipt_source": receipt_source,
        "local_candidate": local_candidate,
        "external_candidate": external_candidate,
        "guidance": {
            "mode": mode,
            "reason": reason,
            "local_pure_correction_available": local_candidate,
        },
    }


def _snapshot_recovery_context(snapshot: dict[str, Any]) -> dict[str, Any]:
    """从既有路线快照派生恢复提示；不建立第二份 current-step 状态。"""
    affected: list[dict[str, Any]] = []
    for step_id, info in (snapshot.get("steps") or {}).items():
        if info.get("state") not in {
            "failed", "blocked", "cancelled", "unverified", "interrupted",
        }:
            continue
        observed = str(info.get("reason") or info.get("state") or "")
        affected.append({
            "attempt_id": info.get("attempt_id"),
            "route_step_id": step_id,
            "step_state": info.get("state"),
            "observed_failure_class": observed,
            "suggested_failure_class": _suggested_recovery_failure_class(
                observed),
            "applied_policy": info.get("applied_policy"),
            "attempt_receipt_may_replace_artifact": (
                observed == "expected_outputs_missing"),
        })

    exact_output_correction = bool(
        len(affected) == 1
        and affected[0]["observed_failure_class"]
        == "expected_outputs_missing"
    )
    external_output_correction = bool(
        exact_output_correction
        and affected[0].get("applied_policy") == "managed_external_job"
    )
    template: dict[str, Any] = {
        "attempt_id": "<从 affected_attempts 选择 exact attempt_id>",
        "failure_class": (
            "expected_output" if exact_output_correction else "<根因分类>"
        ),
        "diagnosis": (
            "<旧 expected_outputs 与入口真实输出语义不一致>"
            if exact_output_correction
            else "<由失败后新证据支持的根因>"
        ),
        "evidence_refs": (
            [] if exact_output_correction
            else ["artifact:<失败后新建的非闭环证据 artifact_id>"]
        ),
    }
    if len(affected) == 1:
        template["attempt_id"] = affected[0]["attempt_id"]
        template["failure_class"] = affected[0]["suggested_failure_class"]

    # 重开一个失败步骤的**真实条件**：该步骤的 step_definition_hash 必须变化。
    # recovery_basis 过了不等于步骤重开——basis 证明的是"你诊断并修复了根因"，
    # hash 变化证明的是"这一步的定义确实不同了"。原来所有恢复指引只讲证据 artifact，
    # 模型永远猜不到还要动步骤本身：三份活体里，一次花 8 次 declare 重开一步，
    # 另一次的 recovery_basis 连吃 9 种不同拒绝。
    reopen_condition = {
        "predicate": (
            "纯 expected-output correction 必须只改变该步骤 expected_outputs；"
            "修订后由 active correction view 复用原 attempt，不重新执行"
            if exact_output_correction else
            "该步骤的 step_definition_hash 必须与上一版不同，否则旧 attempt 仍然"
            "绑定在它上面、步骤仍是 failed —— recovery_basis 通过也不会重开它"),
        "current_step_definition_hash": {
            row["route_step_id"]: (snapshot.get("steps") or {}).get(
                row["route_step_id"], {}).get("definition_hash")
            for row in affected
        },
        "changeable_fields": _step_definition_hash_fields(),
        "step_goal_semantics": _step_identity_contract()[
            "step_goal_semantics"
        ],
        "not_counted": (
            "改 route.goal、route.evidence_refs 或别的步骤都不改变本步骤的 hash"),
        **({} if exact_output_correction else {
            "same_payload_also_needs": (
                "若重提的 action payload 与上次逐字相同，还必须提供 remediation_refs")
        }),
    }

    if exact_output_correction:
        if external_output_correction:
            sequence = [
                "核对 exact 外部 attempt 已有 terminal success_verified 收据",
                "作业确实写了产物、只是声明写错了位置：把该步骤 expected_outputs "
                "改指向本 attempt 提交时登记、且经逐文件 witness 核验的 stdout/stderr；"
                "文件名须与原先报缺失的一一对应，不接受通配符",
                "不得清空已报缺失的 expected_outputs：若受管后端登记的 stdout/stderr "
                "能逐文件见证原产物，改指向它；否则用 report_blocker 后以 "
                "operation_blocked 诚实收尾",
                "整份路线不得改 goal、DAG、入口、effects 或其他步骤；route.evidence_refs "
                "只可追加 recovery_basis.evidence_refs 里的诊断证据，remediation_refs 须为空",
                "修订后由 snapshot 复用原外部终态；禁止重新 submit 同一作业",
                (
                    "若无法纯改指向而要放弃旧 attempt 并重跑：保留 step id、声明新的"
                    "非空 expected_outputs，提供失败后的新诊断/修复证据，并把 "
                    "failure_class 改成证据支持的真实根因（如 parameter、environment、"
                    "execution 或 source）；不得继续使用 expected_output"
                ),
            ]
            mode = "validated_external_attempt_receipt_no_resubmit"
        else:
            sequence = [
                "核对 exact attempt：受管执行成功，且失败原因仅为 expected_outputs_missing",
                "整份路线只修订该步骤 expected_outputs，不改 goal、DAG、入口、effects 或其他步骤",
                "提供 amendment_reason，并以 evidence_refs=[] 修订同一个 declared_route artifact",
                (
                    "修订时逐文件冻结 run-shared-workdir/time-window witness，"
                    "snapshot 复用原 attempt；不得重新执行"
                ),
            ]
            mode = "validated_local_attempt_receipt_no_reexecution"
        requirements: dict[str, Any] = {
            "mode": mode,
            "evidence_refs": [],
            "whole_route_delta": "only affected step.expected_outputs",
            "external_constraint": (
                "new expected_outputs must re-point to this attempt's "
                "registered stdout/stderr (same basenames, inside "
                "jobs/<attempt_id>_*/, written before its terminal verification); "
                "do not clear them or resubmit; if no witness can pass, use "
                "report_blocker then operation_blocked"
                if external_output_correction else None
            ),
        }
    else:
        sequence = [
            "先做只读诊断并分类根因，不直接重复高后果动作",
            "用 save_artifact 保存失败后新证据；不得使用闭环产物或控制契约充当诊断",
            "将同一 artifact 引用加入 route.evidence_refs 和 recovery_basis.evidence_refs",
            "相同 payload 还必须实际修复并提供 remediation_refs；纯改 goal/evidence 不算修复",
        ]
        requirements = {
            "format": "artifact:<artifact_id>",
            "owner": "当前 Experiment run",
            "created_after": "所绑定 attempt 的失败/中断事件",
            "also_required_in": "route.evidence_refs",
            "prohibited_artifact_types": sorted(
                _RECOVERY_RESERVED_ARTIFACT_TYPES),
        }
    result = {
        "reopen_condition": reopen_condition,
        "affected_attempts": affected,
        "required_sequence": sequence,
        "recovery_basis_template": template,
        "allowed_failure_classes": sorted(_RECOVERY_FAILURE_CLASSES),
        "evidence_requirements": requirements,
    }
    if exact_output_correction:
        result["recovery_basis_template_scope"] = (
            "recovery_basis_template 只适用于复用原 attempt 的纯 expected_outputs "
            "改指向；放弃旧 attempt 并重跑时必须改用证据支持的真实 failure_class。"
        )
    return result


def _validate_recovery_basis(
    state: Any,
    basis: Any,
    snapshot: dict[str, Any],
    *,
    previous_route: dict[str, Any],
    next_route: dict[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    affected_by_step = {
        str(step_id): info
        for step_id, info in (snapshot.get("steps") or {}).items()
        if info.get("state") in {
            "failed", "blocked", "cancelled", "unverified", "interrupted",
        }
    }
    affected = list(affected_by_step.values())
    if any(
        info.get("state") == "interrupted"
        and info.get("applied_policy") == "managed_external_job"
        for info in affected
    ):
        return None, (
            "外部提交 attempt 有始无终，必须先按 scheduler/submission identity 对账；"
            "不能靠修订路线清除未知作业"
        )
    if not isinstance(basis, dict):
        return None, "失败或中断后的路线修订必须提供 recovery_basis object"
    attempt_id = str(basis.get("attempt_id") or "").strip()
    affected_ids = {
        str(info.get("attempt_id") or "").strip()
        for info in affected if str(info.get("attempt_id") or "").strip()
    }
    if not attempt_id or attempt_id not in affected_ids:
        return None, "recovery_basis.attempt_id 必须绑定当前失败/中断 attempt"
    matched_steps = [
        step_id for step_id, info in affected_by_step.items()
        if str(info.get("attempt_id") or "") == attempt_id
    ]
    if len(matched_steps) != 1:
        return None, "失败 attempt 无法唯一映射到路线步骤，必须先对账事件历史"
    route_step_id = matched_steps[0]
    observed_failure_class = str(
        affected_by_step[route_step_id].get("reason") or "")
    failure_class = str(basis.get("failure_class") or "").strip()
    if failure_class not in _RECOVERY_FAILURE_CLASSES:
        return None, (
            "recovery_basis.failure_class 必须属于 "
            f"{sorted(_RECOVERY_FAILURE_CLASSES)}"
        )
    diagnosis = str(basis.get("diagnosis") or "").strip()
    if not diagnosis:
        return None, "recovery_basis.diagnosis 必须说明证据支持的根因"

    previous_steps = {
        str(step.get("id") or ""): step
        for step in (previous_route.get("steps") or [])
    }
    next_steps = {
        str(step.get("id") or ""): step
        for step in (next_route.get("steps") or [])
    }
    previous_step = previous_steps.get(route_step_id)
    next_step = next_steps.get(route_step_id)
    contract_fields = (
        "action", "after", "effects", "expected_outputs", "workdir_role",
    )
    contract_delta_fields = [
        field for field in contract_fields
        if (previous_step or {}).get(field) != (next_step or {}).get(field)
    ]
    previous_contract_hash = (
        step_execution_contract_hash(previous_step) if previous_step else ""
    )
    next_contract_hash = (
        step_execution_contract_hash(next_step) if next_step else ""
    )
    next_definition_hash = step_definition_hash(next_step) if next_step else ""
    next_route_content_hash = _content_hash(_canonical_content(next_route))

    events, _warnings = _read_transcript_events(state)
    if _current_blocking_event_history_warning(state) is not None:
        return None, "路线事件历史损坏，不能验证恢复证据时序"
    correction_facts = _expected_output_correction_facts(
        events, attempt_id, route_step_id,
    )
    attempt_events = correction_facts["attempt_events"]
    previous_bounds = correction_facts["previous_bounds"]
    attempt_receipt = correction_facts["attempt_receipt"]
    external_candidate = correction_facts["external_candidate"]
    local_candidate = correction_facts["local_candidate"]
    next_outputs = [
        str(item) for item in ((next_step or {}).get("expected_outputs") or [])
        if str(item).strip()
    ]
    # 外部作业已结束、不能重跑。纯纠正只能改指向有逐文件见证的产物；
    # general recovery 则创建新的执行计划，不复用这个终态 attempt。
    repoint_witness: dict[str, Any] | None = None
    repoint_violations: list[str] = []
    if external_candidate and next_outputs and len(previous_bounds) == 1:
        repoint_witness, repoint_violations = _external_output_repoint_witness(
            previous_bounds[0], attempt_receipt or {}, attempt_id, next_outputs,
            _attempt_backend_output_paths(state, attempt_id))
    local_witness: dict[str, Any] | None = None
    if local_candidate and len(previous_bounds) == 1:
        local_witness, local_violations = _local_output_repoint_witness(
            previous_bounds[0], attempt_receipt or {}, attempt_id,
            next_outputs,
        )
        if local_witness is not None:
            local_witness.update({
                "run_id": str(getattr(state, "run_id", "") or ""),
                "route_step_id": route_step_id,
                "original_route_ref": {
                    "artifact_id": str(previous_bounds[0].get(
                        "route_artifact_id") or ""),
                    "version": previous_bounds[0].get("route_version"),
                    "content_hash": str(previous_bounds[0].get(
                        "route_content_hash") or ""),
                },
                "corrected_route_content_hash": next_route_content_hash,
                "original_step_definition_hash": str(
                    previous_bounds[0].get("step_definition_hash") or ""),
                "corrected_step_definition_hash": next_definition_hash,
                "original_step_execution_contract_hash": (
                    previous_contract_hash),
                "corrected_step_execution_contract_hash": next_contract_hash,
                "original_expected_outputs": deepcopy(
                    (previous_step or {}).get("expected_outputs") or []),
                "corrected_expected_outputs": deepcopy(next_outputs),
                "basename_continuity_verified": True,
            })
        repoint_violations.extend(local_violations)
    allowed_evidence_additions = (
        set(_nonempty_strings(basis.get("evidence_refs", [])) or [])
        if repoint_witness is not None else None)
    whole_route_expected_outputs_only = _route_changes_only_expected_outputs(
        previous_route, next_route, route_step_id,
        allowed_evidence_additions=allowed_evidence_additions,
    )
    external_execution_reused = bool(
        external_candidate
        and (not next_outputs or repoint_witness is not None)
    )
    local_execution_reused = bool(local_candidate and local_witness is not None)
    attempt_receipt_only_recovery = bool(
        observed_failure_class == "expected_outputs_missing"
        and failure_class == "expected_output"
        and whole_route_expected_outputs_only
        and len(previous_bounds) == 1
        and str(previous_bounds[0].get("route_step_id") or "")
        == route_step_id
        and attempt_receipt is not None
        and (local_execution_reused or external_execution_reused)
    )

    # 相二起：形状与证据类的违规**全部累积**，一次给全。
    # 相一（身份）之所以仍逐条硬停：attempt 认不出来时，后面所有检查都无从谈起。
    # 相二不同 —— 这些是可以同时成立的独立缺陷，逐条返回等于让调用方打地鼠：
    # 2026-09-08 活体里 recovery_basis 连吃 9 种不同拒绝才通过一次修订。
    shape_violations: list[str] = []
    # 只拒绝“不触发重跑、直接复用终态 attempt”的纯纠正式清空。带新证据的
    # general recovery、删掉失败步骤或换 step id 都不复用该 attempt，仍按
    # 原 recovery 规则放行；它们必须重新执行，不能从旧终态取得 success。
    forbidden_external_clear = bool(
        observed_failure_class == "expected_outputs_missing"
        and external_candidate
        and failure_class == "expected_output"
        and not next_outputs
        and len(previous_bounds) == 1
        and whole_route_expected_outputs_only
        and str(previous_bounds[0].get("route_step_id") or "") == route_step_id
        and attempt_receipt is not None
    )
    if forbidden_external_clear:
        suggestion = _meaningful_output_repoint_candidate(
            state, previous_bounds[0], attempt_receipt or {}, attempt_id)
        repoint_exit = (
            f"请把 expected_outputs 改指向：{suggestion}。"
            if suggestion else
            "若受管后端登记的 stdout/stderr 能通过逐文件见证，请把 expected_outputs 改指向它；"
        )
        shape_violations.append(
            "外部作业的 expected_outputs_missing 已持久化；不得清空 expected_outputs "
            "来复用该终态 attempt。" + repoint_exit
            + "若需放弃该 attempt，请保留 step id、声明新的非空 expected_outputs，"
            "并提供失败后新诊断/修复证据，使步骤回到 pending 后重跑；"
            "此时 recovery_basis.failure_class 必须改成证据支持的真实根因"
            "（如 parameter、environment、execution 或 source），不得继续使用 "
            "expected_output；"
            "若无法安全重跑，则调用 report_blocker 后以 operation_blocked 诚实收尾。"
        )
    # recovery_basis 只选中一个 attempt，不能借机清空另一条同样已
    # 持久化 missing 的外部步骤；那会把它投影回 pending 并隐式重提。
    for missing_step_id, missing_info in affected_by_step.items():
        if missing_step_id == route_step_id:
            continue
        if (
            missing_info.get("applied_policy") != "managed_external_job"
            or missing_info.get("reason") != "expected_outputs_missing"
        ):
            continue
        previous_missing_outputs = _nonempty_strings(
            (previous_steps.get(missing_step_id) or {}).get("expected_outputs")
            or []
        ) or []
        next_missing_outputs = _nonempty_strings(
            (next_steps.get(missing_step_id) or {}).get("expected_outputs")
            or []
        ) or []
        if previous_missing_outputs and not next_missing_outputs:
            shape_violations.append(
                "外部作业步骤 " + missing_step_id
                + " 的 expected_outputs_missing 已持久化；不得在修订其他 "
                "attempt 时清空它的 expected_outputs。为该步骤改指向已登记并 "
                "逐文件见证的 stdout/stderr，或 report_blocker 后以 "
                "operation_blocked 诚实收尾。"
            )
    if (
        observed_failure_class == "expected_outputs_missing"
        and failure_class == "expected_output"
        and not whole_route_expected_outputs_only
    ):
        # 收敛任务书 K8（缺陷 #4，sgd_checkpoint 441 秒）：判定本身不放宽，但要说出哪个
        # 字段不同——模型重试 5 次都没发现自己顺手改了 run_matrix.goal。
        adjusted = _expected_outputs_only_adjusted(
            previous_route, next_route, route_step_id,
            allowed_evidence_additions=allowed_evidence_additions)
        drift = _route_difference_paths(adjusted, next_route) if adjusted is not None else []
        if drift:
            shape_violations.append(
                "纯 expected_outputs 修正要求路线其余部分与上一版逐字相同，"
                f"这些字段与上一版不同：{drift}；把它们改回上一版原样，"
                f"只保留 route.steps[{route_step_id}].expected_outputs 的改动")

    proposed_evidence_refs = _nonempty_strings(basis.get("evidence_refs", []))
    if (
        observed_failure_class == "expected_outputs_missing"
        and failure_class == "expected_output"
        and next_outputs
        and proposed_evidence_refs
        and not attempt_receipt_only_recovery
    ):
        shape_violations.append(
            "当前修订包含新的非空 expected_outputs 和新诊断证据，且不能复用原 "
            "attempt；这表示正在放弃旧 attempt 并计划重跑，不是纯 expected_outputs "
            "改指向。请把 recovery_basis.failure_class 改成证据支持的真实根因"
            "（如 parameter、environment、execution 或 source），不得继续使用 "
            "expected_output。"
        )

    evidence_refs = _nonempty_strings(basis.get("evidence_refs", []))
    if evidence_refs is None:
        shape_violations.append(
            "recovery_basis.evidence_refs 必须是 artifact 引用列表")
        evidence_refs = []
    remediation_refs = _nonempty_strings(basis.get("remediation_refs", []))
    if remediation_refs is None:
        shape_violations.append(
            "recovery_basis.remediation_refs 必须是 artifact 引用列表")
        remediation_refs = []

    previous_refs = set(previous_route.get("evidence_refs") or [])
    next_refs = set(next_route.get("evidence_refs") or [])
    if attempt_receipt_only_recovery and repoint_witness is not None:
        # 改指向的证据是作业文件本身（见证已逐个核过）。模型附带的诊断笔记照常按
        # 下方 artifact 规则核验，只作补充；没有东西被修复，所以不收 remediation。
        if remediation_refs:
            shape_violations.append(
                "expected_outputs 改指向是纠正声明、不是修复，"
                "recovery_basis.remediation_refs 须为空")
        elif evidence_refs and not set(evidence_refs).issubset(next_refs):
            shape_violations.append(
                "recovery_basis.evidence_refs 必须写入修订后路线的 evidence_refs")
    elif attempt_receipt_only_recovery:
        if evidence_refs or remediation_refs:
            shape_violations.append(
                "纯 expected_outputs 修正必须使用已验证 attempt 收据，"
                "evidence_refs 与 remediation_refs 均须为空")
    else:
        if not evidence_refs:
            shape_violations.append(
                "除纯 expected_outputs 修正外，"
                "recovery_basis.evidence_refs 必须包含至少一条失败后新证据")
        else:
            if not set(evidence_refs).difference(previous_refs):
                shape_violations.append(
                    "recovery_basis 必须引用旧路线中不存在的新证据")
            if not set(evidence_refs).issubset(next_refs):
                shape_violations.append(
                    "recovery_basis.evidence_refs 必须写入修订后路线的 evidence_refs")
    if not set(remediation_refs).issubset(next_refs):
        shape_violations.append(
            "recovery_basis.remediation_refs 必须写入修订后路线的 evidence_refs")

    cutoff = max(
        (str(event.get("at") or "") for event in attempt_events),
        default="",
    )
    evidence_receipts: list[dict[str, Any]] = []
    remediation_receipts: list[dict[str, Any]] = []
    for receipt_kind, ref in [
        *(("evidence", ref) for ref in evidence_refs),
        *(("remediation", ref) for ref in remediation_refs),
    ]:
        if not ref.startswith("artifact:"):
            shape_violations.append(
                f"{ref}：失败恢复证据必须使用 artifact:<artifact_id>，不能用自由"
                "文本或无法机械验证的 transcript 标签")
            continue
        artifact_id = ref.split(":", 1)[1].strip()
        try:
            artifact = state.read_artifact(artifact_id)
        except Exception:
            artifact = None
        if not isinstance(artifact, dict):
            shape_violations.append(f"恢复证据 artifact 不存在：{artifact_id}")
            continue
        artifact_type = str(artifact.get("type") or "")
        if artifact_type in _RECOVERY_RESERVED_ARTIFACT_TYPES:
            shape_violations.append(
                f"{artifact_id}：artifact_type={artifact_type!r} 由闭环或上游契约"
                "生命周期拥有，不得作为路线恢复证据")
            continue
        if (
            artifact.get("produced_by_node_type") != "experiment"
            or artifact.get("produced_by_run_id") != state.run_id
        ):
            shape_violations.append(
                f"恢复证据不属于当前 Experiment run：{artifact_id}")
            continue
        created_at = str(artifact.get("created_at") or "")
        if cutoff and (not created_at or created_at <= cutoff):
            shape_violations.append(
                f"恢复证据必须在失败/中断事实之后产生（须晚于 {cutoff}）："
                f"{artifact_id}")
            continue
        if not str(artifact.get("content_hash") or "").strip():
            shape_violations.append(f"恢复证据缺少内容哈希：{artifact_id}")
            continue
        receipt = {
            "artifact_id": artifact_id,
            "artifact_type": artifact_type,
            "version": artifact.get("version"),
            "content_hash": artifact.get("content_hash"),
            "created_at": created_at,
        }
        if receipt_kind == "remediation":
            remediation_receipts.append(receipt)
        else:
            evidence_receipts.append(receipt)

    if shape_violations:
        if (
            repoint_violations
            and observed_failure_class == "expected_outputs_missing"
            and failure_class == "expected_output"
        ):
            # 本意显然是改指向，却因见证没过被当成普通恢复去要"新证据"——只报后者
            # 会让模型去补诊断笔记，而真正的问题在文件本身。把见证的结论放在最前。
            witness_prefix = (
                "若本意是纠正 expected_outputs，本地 attempt time-window witness "
                "逐文件核验未过："
                if local_candidate else
                "若本意是把 expected_outputs 改指向作业真实写下的文件，逐文件核验未过："
            )
            shape_violations[:0] = [
                witness_prefix + "；".join(repoint_violations)
            ]
        return None, shape_violations

    if attempt_receipt is not None and previous_bounds:
        attempt_receipt = {
            **attempt_receipt,
            "route_artifact_id": previous_bounds[-1].get(
                "route_artifact_id"),
            "route_version": previous_bounds[-1].get("route_version"),
            "route_content_hash": previous_bounds[-1].get(
                "route_content_hash"),
            "step_execution_contract_hash": previous_bounds[-1].get(
                "step_execution_contract_hash"),
            "step_definition_hash": previous_bounds[-1].get(
                "step_definition_hash"),
            "expected_outputs": deepcopy(
                previous_bounds[-1].get("expected_outputs") or []),
            "resolved_workdir": previous_bounds[-1].get("resolved_workdir"),
            "bound_at_ns": previous_bounds[-1].get("bound_at_ns"),
        }
    return {
        "run_id": str(getattr(state, "run_id", "") or ""),
        "attempt_id": attempt_id,
        "route_step_id": route_step_id,
        "observed_failure_class": observed_failure_class,
        "failure_class": failure_class,
        "diagnosis": diagnosis,
        "evidence_refs": evidence_refs,
        "evidence_receipts": evidence_receipts,
        "remediation_refs": remediation_refs,
        "remediation_receipts": remediation_receipts,
        "attempt_receipt_used_as_evidence": attempt_receipt_only_recovery,
        "external_execution_reused": (
            attempt_receipt_only_recovery and external_execution_reused
        ),
        "local_execution_reused": (
            attempt_receipt_only_recovery and local_execution_reused
        ),
        "attempt_receipt": (
            attempt_receipt if attempt_receipt_only_recovery else None
        ),
        "previous_action_payload_digest": str(
            (previous_bounds[-1] if previous_bounds else {}).get(
                "action_payload_digest") or ""
        ),
        "previous_step_execution_contract_hash": previous_contract_hash,
        "next_step_execution_contract_hash": next_contract_hash,
        "next_step_definition_hash": next_definition_hash,
        "next_route_content_hash": next_route_content_hash,
        "contract_delta_fields": contract_delta_fields,
        "whole_route_expected_outputs_only": (
            whole_route_expected_outputs_only),
        "expected_outputs_changed": attempt_receipt_only_recovery,
        "external_output_repoint_witness": (
            repoint_witness
            if attempt_receipt_only_recovery and external_execution_reused
            else None
        ),
        "local_output_correction_witness": (
            local_witness
            if attempt_receipt_only_recovery and local_execution_reused
            else None
        ),
        "recovery_validated": True,
    }, None


def _operation_closure_route_change_block(state: Any) -> dict[str, Any] | None:
    """operation 结果身份一旦开始落盘，本 run 的路线就不再可改。"""
    try:
        try:
            from .operation_completion import operation_closure_status
        except ImportError:
            from tools.operation_completion import operation_closure_status
        closure_status = operation_closure_status(state)
    except Exception as exc:
        return {
            "status": "error",
            "error_code": "operation_closure_audit_failed",
            "error": "无法审计当前 run 的 operation closure，拒绝写入路线。",
            "blocker": {
                "kind": "operation_closure_audit_failed",
                "reason": type(exc).__name__,
                "suggested_owner": "framework",
                "node_action": "repair_operation_closure_audit",
                "retryable_after_change": True,
            },
        }
    if not closure_status.get("sealed"):
        return None
    closure_kind = str(closure_status.get("kind") or "conflict")
    complete = closure_kind == "complete"
    error_code = (
        "execution_route_sealed_by_operation_closure"
        if complete else
        "operation_closure_reconciliation_required"
    )
    return {
        "status": "error",
        "error_code": error_code,
        # 分治：complete 是诚实终态（已冻结产物一律不可否定，run 内确实没有出口），
        # partial/conflict 则往往还有救——未冻结的草稿可以 supersede 掉，之后同一次
        # declare 原样就能成功。原来两档共用一句「开新 run」，把后者说成了假终态。
        "error": (
            "当前 run 的 operation closure 已完成，路线与结果身份已经封口。"
            "已冻结的闭环产物不可否定，本 run 内没有继续执行新路线的出口——"
            "这是诚实终态：把已完成的部分与未完成的原因记录清楚后结束本 run，"
            "由人开 continuation run 继续。"
            if complete else
            "当前 run 存在未完成或冲突的 operation closure，正挡住路线修订。"
            + (" 其中这些草稿尚未冻结，可以先否定掉再原样重试本次 declare："
               + "、".join(
                   (closure_status.get("supersedable_artifact_ids") or [])[:5])
               + "；逐字调用 supersede_closure_draft(artifact_id=..., reason=...)。"
               if closure_status.get("supersedable_artifact_ids") else
               " 没有可否定的未冻结草稿；先按原输入完成闭环，或由人开 "
               "continuation run。")
        ),
        "closure": closure_status,
        "blocker": {
            "kind": error_code,
            "suggested_owner": (
                "framework" if closure_kind == "conflict" else "experiment"
            ),
            "node_action": (
                "start_new_run_for_new_route"
                if complete else
                "reconcile_closure_then_start_new_run"
            ),
            # complete 是真终态；partial/conflict 在否定掉未冻结草稿之后，同一次
            # declare 原样就能成功，标成不可重试是失真。该字段只影响文案，
            # 没有 run 级副作用（core/dispatch_gate 只据它多印一行）。
            "retryable_after_change": not complete,
        },
    }


def _canonical_route_integrity_refusal(
    state: Any, route_id: str, existing: Any,
) -> dict[str, Any] | None:
    """账本里已有本 run 的路线身份，正文却读不到或与账本 sha256 不符：不按初次声明处理
    （verify 清单 #4，第三会话复审实测）。

    原先 existing is None 就当初次声明；hash 不符时快照 unavailable，恢复门被跳过——删掉或
    改一个字节的路线文件，失败的外部步骤不带 recovery_basis 就能重开并重提（不变量 2）。
    账本的 sha256 是权威。按它从历史取回 head 正文需要 Core 的接口（RecordStore.versions 对
    head 只读当前文件，git 回溯是账本私有方法），节点不另起一套 git 查找；这里失败关闭，并给出
    诚实收尾的出口。
    """
    refusal = _canonical_route_integrity_problem(state, route_id, existing)
    if refusal is None:
        return None
    try:
        state.append_transcript(
            "declared_route_rejected",
            reason="canonical_route_record_integrity_failed",
            artifact_id=route_id, ledger_version=refusal.get("ledger_version"),
            problem=refusal.get("problem"),
        )
    except Exception:
        pass
    return refusal


def _canonical_route_integrity_problem(
    state: Any, route_id: str, existing: Any,
) -> dict[str, Any] | None:
    """_canonical_route_integrity_refusal 的只读部分（草稿预检与 declare 共用）。"""
    head_of = getattr(state, "artifact_head", None)
    if not callable(head_of):
        return None
    try:
        head = head_of(route_id)
    except Exception as exc:
        head = exc
    if head is None:
        return None
    if not isinstance(head, Exception) and isinstance(existing, dict) and (
            existing.get("content_hash") == _content_hash(str(existing.get("content") or ""))):
        return None
    version = None if isinstance(head, Exception) else getattr(head, "version", None)
    problem = (
        f"账本读不出（{type(head).__name__}）" if isinstance(head, Exception)
        else "正文文件不见了" if existing is None
        else "正文与账本记录的 sha256 不符（被改动过）")
    return {
        "problem": problem,
        "status": "error",
        "error_code": "canonical_route_record_integrity_failed",
        "error": (
            f"账本记录了本 run 的路线 {route_id}（v{version}），但{problem}。不能按初次声明处理"
            "——那会绕过失败步骤的恢复门、允许重复提交；原正文也无法从历史取回。路线状态已不"
            "可信：不要重新声明，也不要重提作业。"),
        "artifact_id": route_id,
        "ledger_version": version,
        "do_not_resubmit": True,
        "next_actions": [
            f'report_blocker(summary="canonical 路线记录 {route_id} v{version} {problem}", '
            'requested_action="核对工作区记录目录与 git 历史，按账本 sha256 恢复该文件")',
            # record_operation_completion 是 operation run 的收尾工具；scientific run 登记 blocker
            # 后结束本轮（第三会话复审 0914c 971fd66a P3）。
            ("保存已有的有效工作后结束本轮：scientific run 不经 record_operation_completion 收尾"
             if _run_execution_mode(state) == "scientific"
             else 'record_operation_completion(outcome="blocked")——诚实收尾，会永久封口本 run'),
        ],
    }


def _run_execution_mode(state: Any) -> str:
    projection = _execution_scope_projection(state, required=False)
    return str(projection.get("scope_mode") or "").strip().lower()


def _route_recovery_basis_refusal(
    basis_error: Any, previous_snapshot: dict[str, Any],
) -> dict[str, Any]:
    """`route_recovery_basis_required` 的唯一出处：declare 与草稿预检共用同一段文案。"""
    # 相二可能一次给回多条：全部交给调用方，别让它一条一条打地鼠。
    basis_violations = (
        list(basis_error) if isinstance(basis_error, list)
        else [basis_error])
    return {
        "status": "error",
        "error_code": "route_recovery_basis_required",
        "error": (
            basis_violations[0] if len(basis_violations) == 1
            else (f"recovery_basis 有 {len(basis_violations)} 处"
                  "不成立，逐条列在 violations 里，请一次全部修正："
                  + "；".join(basis_violations))),
        "violations": basis_violations,
        "recovery_context": _snapshot_recovery_context(previous_snapshot),
    }


def _route_scope_effect_refusal(
    state: Any, route: dict[str, Any],
) -> dict[str, Any] | None:
    """operation run 声明 scientific_execution 步骤的拒绝（只读）；文案就是 declare 那段。

    048 v5（对抗审查）：草稿照抄既有路线里的 scientific 步骤——run 先按 scientific 冻结
    路线、后改道 operation 时——declare 会在这里拒。"""
    mode_view = load_execution_mode_view(state)
    if mode_view.get("mode") != "operational":
        return None
    scientific_step_ids = [
        step["id"] for step in (route.get("steps") or [])
        if isinstance(step, dict) and "scientific_execution" in (step.get("effects") or [])
    ]
    if not scientific_step_ids:
        return None
    return {
        "status": "error",
        "error_code": "route_scope_effect_mismatch",
        "error": (
            "当前 run 已分类为 operation；安装、构建和非科学 smoke run "
            "不得声明 scientific_execution。普通本地程序使用 process_tree；"
            "只有正式 scientific scope 才能声明 scientific_execution。"
        ),
        "step_ids": scientific_step_ids,
        "suggested_effect": "process_tree",
    }


def _route_envelope_refs_refusal(
    state: Any, route: dict[str, Any],
) -> dict[str, Any] | None:
    """declare 的 envelope ref 校验（只读）：草稿生成器与 declare 共用同一份拒绝。"""
    envelope_errors = validate_route_execution_envelope_refs(state, route)
    if not envelope_errors:
        return None
    return {
        "status": "error",
        "error_code": "execution_envelope_invalid",
        "error": "declared_route 引用的 execution envelope 无效",
        "errors": envelope_errors,
    }


def _route_envelope_enforce_refusal(route: dict[str, Any]) -> dict[str, Any] | None:
    """enforce 档下 scientific 步骤缺 envelope 的拒绝（只读）；文案就是 declare 返回的那段。"""
    missing_envelope_steps = missing_execution_envelope_steps(route)
    if not (missing_envelope_steps and execution_envelope_gate_mode() == "enforce"):
        return None
    return {
        "status": "error",
        "error_code": "execution_envelope_required",
        "error": (
            "scientific_execution 步骤必须先绑定冻结 execution envelope"
            f"（缺失步骤：{missing_envelope_steps}）。纠偏序列："
            "① declare_execution_envelope(route_step_id=<step id>, "
            "assurance_class=evidence_bearing, target_profile_ref=..., "
            "storage_binding=..., environment_lock_artifact_ids=[...])；"
            "② 把返回的 evidence_ref 逐字放入该 step.action.evidence_refs；"
            "③ 重新 declare_execution_route（路线已冻结时带 "
            "amendment_reason 修订同一身份）。"
        ),
        "step_ids": missing_envelope_steps,
    }


def _route_amendment_preflight(
    state: Any,
    previous_snapshot: dict[str, Any],
    *,
    recovery_basis: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """declare 写入前、不看修订内容就能判定的几道只读门；草稿生成器与 declare 共用。

    048 v3（复审）：execution_route_required 附的 next_action 承诺"照抄即可"，它就必须
    过与真实 declare 入口**同一套**无副作用预检。原来 _route_step_draft 只按
    blocked / interrupted 两个字符串排除，真实 in_progress（外部作业已提交未收尾）与
    operation closure 已封口的 run 仍拿到草稿，照做立刻撞
    route_active_attempt_reconciliation_required / execution_route_sealed_by_operation_closure。
    再往草稿里加一个字符串只是把同一个漏洞往后挪：两处各自维护一份状态清单迟早再漂。

    顺序与 declare 一致：closure 封口 → 事件历史冲突 → 进行中 attempt →
    blocked / interrupted 且没带 recovery_basis。带了 recovery_basis 的
    blocked / interrupted 修订不在这里判——那要拿真实的修订内容交给
    _validate_recovery_basis 校验，declare 照旧自己做。返回 None 表示这几道门都过，
    否则就是 declare 会原样返回的拒绝。
    """
    closure_block = _operation_closure_route_change_block(state)
    if closure_block is not None:
        return closure_block
    route_state = previous_snapshot.get("route_state")
    if route_state == "invalid_event_history":
        return {
            "status": "error",
            "error_code": "route_history_reconciliation_required",
            "error": (
                "路线事件历史存在冲突，不能靠修改 step hash 清除。"
                "请由 framework owner 修复/对账追加事件后再修订。"
            ),
            "blocker": {
                "kind": "route_history_reconciliation_required",
                "suggested_owner": "framework",
                "node_action": "reconcile_route_event_history",
            },
        }
    if route_state == "in_progress":
        # 出口必须是这一刻真走得通的那一条。原文案写「先对账或 finalize」，
        # 而受管对账（record_external_route_identity_resolution）只接受
        # outcome 为空或 unknown+external_identity_reconciliation_required；
        # in_progress 的 attempt outcome 恒为 submitted，于是对账恒返回
        # route_attempt_not_identity_unresolved / integration_pending。
        # 「先对账」这四个字在这一格从来没走通过——2026-09-08 与 09-09 两次
        # 活体都在这里空转。真出口只有一条：把进行中的作业收尾。
        in_progress = [
            {
                "route_step_id": step_id,
                "attempt_id": info.get("attempt_id"),
                "scheduler": info.get("scheduler"),
                "job_id": info.get("job_id"),
                "step_state": info.get("state"),
            }
            for step_id, info in (
                (previous_snapshot.get("steps") or {}).items())
            if info.get("state") == "in_progress"
        ]
        calls = [
            (f'finalize_external_job(scheduler="{row["scheduler"]}", '
             f'job_id="{row["job_id"]}", outcome=...)')
            for row in in_progress
            if row.get("scheduler") and row.get("job_id")
        ]
        return {
            "status": "error",
            "error_code": "route_active_attempt_reconciliation_required",
            "error": (
                "路线上还有受管动作没有收尾，此时修订路线会让进行中的 "
                "attempt 失去归属。先把它们逐个收尾，再修订。"
                + (f" 逐字调用：{'; '.join(calls)}。" if calls else
                   " 用 check_external_job_health 查出仍开着的作业，"
                   "再对每个调用 finalize_external_job。")
                + " 作业类别决定 outcome 用哪套词表；收尾被拒时，拒绝信息里"
                "带的就是权威状态与合法出口。"),
            "in_progress_attempts": in_progress,
            "route_state": previous_snapshot.get("route_state"),
        }
    if route_state in {"blocked", "interrupted"} and recovery_basis is None:
        # 没带 recovery_basis 时，_validate_recovery_basis 在读到修订内容之前就已
        # 拒绝（外部 attempt 有始无终 / 缺 recovery_basis object），所以这里用旧路线
        # 顶替 next_route 也得到 declare 一字不差的拒绝。
        previous_route = previous_snapshot.get("route") or {}
        _, basis_error = _validate_recovery_basis(
            state,
            None,
            previous_snapshot,
            previous_route=previous_route,
            next_route=previous_route,
        )
        if basis_error:
            return _route_recovery_basis_refusal(basis_error, previous_snapshot)
    return None


async def _declare_execution_route(
    state: Any,
    route: Any,
    amendment_reason: str | None = None,
    recovery_basis: dict[str, Any] | None = None,
    **_: Any,
) -> dict[str, Any]:
    """校验、保存并冻结 canonical v2 route；修订沿用同一 artifact 身份。"""
    report = validate_route_v2(route, declaring=True)
    if not report["valid"]:
        state.append_transcript(
            "declared_route_rejected",
            reason="route_schema_invalid",
            errors=report["errors"],
        )
        return {
            "status": "error",
            "error_code": "route_schema_invalid",
            "error": "declared_route v2 校验失败",
            "errors": report["errors"],
            "warnings": report["warnings"],
        }

    scientific_step_ids = [
        step["id"] for step in report["route"]["steps"]
        if "scientific_execution" in (step.get("effects") or [])
    ]
    if scientific_step_ids:
        assignment_block = prereg_assignment_scientific_block(
            state,
            signal_source="route_declaration",
            signal_details={"step_ids": scientific_step_ids},
        )
        if assignment_block is not None:
            state.append_transcript(
                "declared_route_rejected",
                reason="prereg_assignment_required",
                step_ids=scientific_step_ids,
                assignment_status=assignment_block.get("assignment_status"),
            )
            return {**assignment_block, "step_ids": scientific_step_ids}

    envelope_refusal = _route_envelope_refs_refusal(state, report["route"])
    if envelope_refusal is not None:
        state.append_transcript(
            "declared_route_rejected",
            reason="execution_envelope_invalid",
            errors=envelope_refusal["errors"],
        )
        return {**envelope_refusal, "warnings": report["warnings"]}

    route_intent_receipt = execution_intent_binding_receipt(state, require=False)
    if not route_intent_receipt.get("passed"):
        state.append_transcript(
            "declared_route_rejected",
            reason="execution_intent_binding_required",
            execution_intent_binding=route_intent_receipt.get("audit"),
        )
        return {
            "status": "error",
            "error_code": "execution_intent_binding_required",
            "error": "当前 scope 的上游输入或冻结科学契约已漂移/不可核验；不得冻结 declared_route。",
            "execution_intent_binding": route_intent_receipt.get("audit"),
        }

    scope_refusal = _route_scope_effect_refusal(state, report["route"])
    if scope_refusal is not None:
        state.append_transcript(
            "declared_route_rejected",
            reason="route_scope_effect_mismatch",
            step_ids=scope_refusal["step_ids"],
            execution_scope="operational",
        )
        return scope_refusal

    envelope_gate_mode = execution_envelope_gate_mode()
    missing_envelope_steps = missing_execution_envelope_steps(report["route"])
    # 摩擦审计：无论档位（含 off 逃生口）都无条件留下生效 gate mode 事实，
    # reviewer 可机械核对本次 declare 是在哪一档下通过的。
    state.append_transcript(
        "execution_envelope_gate_mode",
        gate_mode=envelope_gate_mode,
        missing_envelope_step_ids=missing_envelope_steps,
    )
    enforce_refusal = _route_envelope_enforce_refusal(report["route"])
    if enforce_refusal is not None:
        state.append_transcript(
            "declared_route_rejected",
            reason="execution_envelope_required",
            step_ids=enforce_refusal["step_ids"],
        )
        return {**enforce_refusal, "warnings": report["warnings"]}
    if missing_envelope_steps and envelope_gate_mode == "warn":
        # warn 档不拦，也不再要模型手动补 envelope（收敛任务书 K3）：envelope 在节点外没有
        # 读取方，target_profile_ref 只核格式、从不核对，prereg 三元组与意图绑定在别处已有
        # 权威记录；原先这条警告让模型在每个 scientific 步骤前多走一轮声明与修订。只留结构化
        # 事实供审计；部署切到 enforce 档时，上面的拒绝与 spawn 前门禁仍指向
        # declare_execution_envelope。
        state.append_transcript(
            "execution_envelope_missing",
            gate_mode="warn",
            step_ids=missing_envelope_steps,
        )

    content = _canonical_content(report["route"])
    canonical_route_name = _canonical_route_name(state)
    canonical_route_id = f"declared_route__{canonical_route_name}"
    existing: dict[str, Any] | None = state.read_artifact(canonical_route_id)
    integrity_refusal = _canonical_route_integrity_refusal(
        state, canonical_route_id, existing)
    if integrity_refusal is not None:
        return integrity_refusal
    if existing is not None and not isinstance(existing, dict):
        return {
            "status": "error",
            "error_code": "canonical_route_record_unreadable",
            "error": "canonical declared_route 已存在但无法读取；拒绝覆盖坏记录",
        }
    previous_snapshot: dict[str, Any] | None = None
    if existing is None or existing.get("content") != content:
        # 048 v3：与 _route_step_draft 共用同一份只读预检（closure 封口 / 事件历史冲突 /
        # 进行中 attempt / 没带 recovery_basis 的 blocked·interrupted）。草稿只在这些门
        # 都过时才附上，"照抄即可"的承诺才与真实入口同源。
        previous_snapshot = build_route_snapshot(state)
        preflight_refusal = _route_amendment_preflight(
            state, previous_snapshot, recovery_basis=recovery_basis)
        if preflight_refusal is not None:
            return preflight_refusal
    normalized_recovery_basis: dict[str, Any] | None = None
    if existing is not None:
        same_content = existing.get("content") == content
        frozen = isinstance(existing.get("metadata"), dict) and bool(
            existing["metadata"].get("frozen")
        )
        existing_metadata = existing.get("metadata") or {}
        stored_receipt = (
            existing_metadata.get(_ROUTE_INTENT_BINDING_METADATA_KEY)
            if isinstance(existing_metadata, dict) else None
        )
        current_receipt = route_intent_receipt.get("receipt")
        if frozen and isinstance(stored_receipt, dict):
            if not isinstance(current_receipt, dict):
                return {
                    "status": "error",
                    "error_code": "route_execution_intent_binding_scope_missing",
                    "error": "已绑定上游意图的冻结 declared_route 不能在缺失 scope receipt 时修订或降级为未绑定计划。",
                }
            if stored_receipt != current_receipt:
                return {
                    "status": "error",
                    "error_code": "route_execution_intent_binding_changed",
                    "error": "当前 immutable scope 与冻结 declared_route 的上游意图 receipt 不同；必须由上游创建新 run，不能在同一路线修订。",
                    "stored_receipt": stored_receipt,
                    "current_receipt": current_receipt,
                }
        if (frozen and same_content and stored_receipt is None
                and isinstance(current_receipt, dict)
                and not str(amendment_reason or "").strip()):
            return {
                "status": "error",
                "error_code": "route_intent_binding_refresh_required",
                "error": "路线在 scope 分类前冻结；请带 amendment_reason 重新声明同一路线以写入当前 immutable intent receipt。",
            }
        if frozen and same_content and stored_receipt == current_receipt:
            loaded = load_canonical_route(state)
            if loaded.get("status") == "ready":
                if recovery_basis is not None:
                    snapshot = build_route_snapshot(state)
                    return {
                        "status": "error",
                        "error_code": "route_recovery_basis_not_applicable",
                        "error": (
                            "路线内容未变化；recovery_basis 不得被幂等返回静默忽略。"
                        ),
                        "recovery_context": _snapshot_recovery_context(snapshot),
                    }
                return {
                    "status": "success",
                    "artifact_id": canonical_route_id,
                    "route_ref": loaded["route_ref"],
                    "warnings": report["warnings"],
                    "already_declared": True,
                    "execution_guidance": _execution_guidance(state),
                }
        reason_text = str(amendment_reason or "").strip()
        if frozen and (not reason_text or (reason_text.startswith("<") and reason_text.endswith(">"))):
            # 048 v5：草稿里的占位符原样照抄不算修订依据（文案一直这么承诺，现在成真）。
            return {
                "status": "error",
                "error_code": "route_amendment_reason_required",
                "error": (
                    "canonical declared_route 已冻结；修订必须在同一身份上提供 "
                    "amendment_reason，不能另存平行路线"
                ),
                "next_action": "按 retry_call 原样重调，只把 amendment_reason 换成真实的修订依据",
                "retry_call": {
                    "tool": "declare_execution_route",
                    "arguments": {
                        "route": deepcopy(report["route"]),
                        "amendment_reason": "<说明依据什么新证据修订这条路线>",
                        **({"recovery_basis": deepcopy(recovery_basis)}
                           if recovery_basis is not None else {}),
                    },
                },
            }
        if frozen and not same_content:
            # 事件历史冲突 / 进行中 attempt / 没带 recovery_basis 的 blocked·interrupted
            # 已由上面的 _route_amendment_preflight 拒掉；这里只剩带了 recovery_basis
            # 的恢复修订，要拿真实的修订内容校验。
            if previous_snapshot is None:
                previous_snapshot = build_route_snapshot(state)
            if previous_snapshot.get("route_state") in {"blocked", "interrupted"}:
                normalized_basis, basis_error = _validate_recovery_basis(
                    state,
                    recovery_basis,
                    previous_snapshot,
                    previous_route=previous_snapshot.get("route") or {},
                    next_route=report["route"],
                )
                if basis_error:
                    return _route_recovery_basis_refusal(
                        basis_error, previous_snapshot)
                normalized_recovery_basis = normalized_basis
            elif recovery_basis is not None:
                return {
                    "status": "error",
                    "error_code": "route_recovery_basis_not_applicable",
                    "error": (
                        "当前路线没有待恢复的失败/中断 attempt；"
                        "拒绝持久化未经校验的 recovery_basis。"
                    ),
                }

    if recovery_basis is not None and normalized_recovery_basis is None:
        return {
            "status": "error",
            "error_code": "route_recovery_basis_not_applicable",
            "error": (
                "只有既有路线的失败/中断恢复可以携带 recovery_basis；初始声明不得伪造恢复收据。"
            ),
        }
    route_metadata: dict[str, Any] = {
        "schema_version": ROUTE_SCHEMA_VERSION,
        "route_role": "canonical_execution_route",
    }
    if isinstance(route_intent_receipt.get("receipt"), dict):
        route_metadata[_ROUTE_INTENT_BINDING_METADATA_KEY] = deepcopy(
            route_intent_receipt["receipt"]
        )
    if normalized_recovery_basis is not None:
        # canonical 当前版本与恢复收据同一次 save：freeze 后即使 transcript
        # 投影中断，也不会出现“新路线已生效但恢复约束丢失”的窗口。
        route_metadata[_RECOVERY_METADATA_KEY] = deepcopy(
            normalized_recovery_basis)
    try:
        saved = state.save_artifact(
            "declared_route",
            canonical_route_name,
            content,
            metadata=route_metadata,
            amendment_reason=(str(amendment_reason).strip() if amendment_reason else None),
        )
    except Exception as exc:  # State contract errors must become actionable tool output.
        return {
            "status": "error",
            "error_code": "route_persistence_failed",
            "error": f"{type(exc).__name__}: {exc}",
        }
    frozen = await _freeze_artifact(
        state,
        saved["id"],
        "冻结当前 Experiment 执行路线；后续变更必须同身份修订",
    )
    if frozen.get("status") != "success":
        return {
            "status": "error",
            "error_code": "route_freeze_failed",
            "error": frozen.get("error", "declared_route 冻结失败"),
            "artifact_id": saved["id"],
        }
    loaded = load_canonical_route(state)
    if loaded.get("status") != "ready":
        return {
            "status": "error",
            "error_code": "route_post_freeze_verification_failed",
            "error": "declared_route 冻结后机械复核失败",
            "verification": loaded,
        }
    state.append_transcript(
        "declared_route_bound",
        **loaded["route_ref"],
        goal=loaded["route"]["goal"],
        step_ids=[step["id"] for step in loaded["route"]["steps"]],
        source_format=loaded["source_format"],
    )
    if normalized_recovery_basis:
        recovery_basis = normalized_recovery_basis
        state.append_transcript(
            "declared_route_recovery_basis",
            attempt_id=recovery_basis["attempt_id"],
            route_step_id=recovery_basis["route_step_id"],
            observed_failure_class=recovery_basis["observed_failure_class"],
            failure_class=recovery_basis["failure_class"],
            diagnosis=recovery_basis["diagnosis"],
            evidence_refs=recovery_basis["evidence_refs"],
            evidence_receipts=recovery_basis.get("evidence_receipts") or [],
            remediation_refs=recovery_basis.get("remediation_refs") or [],
            remediation_receipts=(
                recovery_basis.get("remediation_receipts") or []
            ),
            previous_action_payload_digest=recovery_basis.get(
                "previous_action_payload_digest") or "",
            previous_step_execution_contract_hash=recovery_basis.get(
                "previous_step_execution_contract_hash") or "",
            next_step_execution_contract_hash=recovery_basis.get(
                "next_step_execution_contract_hash") or "",
            contract_delta_fields=recovery_basis.get(
                "contract_delta_fields") or [],
            whole_route_expected_outputs_only=bool(
                recovery_basis.get("whole_route_expected_outputs_only")),
            attempt_receipt_used_as_evidence=bool(
                recovery_basis.get("attempt_receipt_used_as_evidence")),
            external_execution_reused=bool(
                recovery_basis.get("external_execution_reused")),
            local_execution_reused=bool(
                recovery_basis.get("local_execution_reused")),
            attempt_receipt=recovery_basis.get("attempt_receipt"),
            external_output_repoint_witness=recovery_basis.get(
                "external_output_repoint_witness"),
            local_output_correction_witness=recovery_basis.get(
                "local_output_correction_witness"),
            expected_outputs_changed=bool(
                recovery_basis.get("expected_outputs_changed")),
            recovery_validated=True,
            **loaded["route_ref"],
        )
    return {
        "status": "success",
        "artifact_id": saved["id"],
        "route_ref": loaded["route_ref"],
        "warnings": report["warnings"],
        "execution_guidance": _execution_guidance(state),
    }


register_tool(
    ToolDefinition(
        name="declare_execution_route",
        description=(
            "声明并冻结当前 Experiment 的唯一执行路线。先根据目标与可靠证据形成少量"
            "里程碑步骤，不要为每条只读探查命令写步骤。路线固定保存为同一个 "
            "declared_route__execution_route 身份；已冻结路线有新证据时，必须带 "
            "amendment_reason 修订同一身份，禁止换名制造平行路线。路线不授予路径权限。"
            "真正的构建（make/ninja/cmake --build）、启动器（mpirun/srun）与正式科学执行"
            "步骤必须声明 action.tool=submit_job —— 它们需要一个活过本次调用的作业身份；"
            "这类步骤的 external_job effect 由声明期自动补上，不必手写。有界的本地动作"
            "（解包、configure、装依赖、只读诊断）用 safe_run_bash，照常声明 process_tree "
            "等真实 effects 即可。"
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "route": {
                    "oneOf": [ROUTE_V2_TOOL_SCHEMA, {"type": "string"}],
                    "description": "schema_version=2 的 JSON/YAML object 或字符串。",
                },
                "amendment_reason": {
                    "type": "string",
                    "description": "修订已冻结路线时必填，写明改变判断的新证据。",
                },
                "recovery_basis": {
                    "type": "object",
                    "description": "仅失败/中断后修订时必填：绑定 attempt、根因分类和新增证据。",
                    "properties": {
                        "attempt_id": {"type": "string"},
                        "failure_class": {
                            "type": "string",
                            "enum": sorted(_RECOVERY_FAILURE_CLASSES),
                        },
                        "diagnosis": {"type": "string"},
                        "evidence_refs": {
                            "type": "array",
                            "items": {"type": "string"},
                            "minItems": 0,
                            "description": (
                                "必须显式提供。仅当 exact attempt 为 "
                                "expected_outputs_missing 且整份路线只修改该步骤 "
                                "expected_outputs 时可为空；其他失败至少一项。"
                            ),
                        },
                        "remediation_refs": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "若计划以完全相同负载重试，必须列出失败后真实应用"
                                "的 remediation artifact；否则可省略。"
                            ),
                        },
                    },
                    "required": ["attempt_id", "failure_class", "diagnosis", "evidence_refs"],
                    "additionalProperties": False,
                },
            },
            "required": ["route"],
            "additionalProperties": False,
        },
        allowed_node_types=["experiment"],
        risk_level="medium",
    ),
    _declare_execution_route,
)

# declared_route 的字段契约与校验器同源（build_contract.DECLARED_ROUTE_CONTRACT）。
# 此前 describe_contract_requirements() 定义了却从没被调用 —— 声明存在、模型看不见，
# 于是 env_domains 这类字段只能靠撞墙学会（tests/test_tool_contracts_reach_the_caller
# 抓到的就是这个）。在 save gate 上登记 content_contract，让同一份声明真的送到调用方。
try:
    from .build_contract import DECLARED_ROUTE_CONTRACT as _DECLARED_ROUTE_CONTRACT
except ImportError:  # pragma: no cover - standalone node bootstrap.
    from tools.build_contract import DECLARED_ROUTE_CONTRACT as _DECLARED_ROUTE_CONTRACT

register_save_gate("declared_route", _canonical_route_save_gate,
                   content_contract=dict(_DECLARED_ROUTE_CONTRACT))


def resolve_external_route_expected_outputs(
    state: Any,
    *,
    not_after_ns: int | None = None,
    scheduler: str,
    job_id: str,
    namespace: str | None = None,
    launch_host: str | None = None,
    scheduler_cluster: str | None = None,
    resource_uid: str | None = None,
    submission_nonce: str | None = None,
    process_group_id: str | None = None,
    process_start_ticks: str | int | None = None,
    container_runtime_id: str | None = None,
) -> dict[str, Any]:
    """Resolve one external attempt's output postcondition without writing state.

    The route binding remains the declaration authority and
    :func:`_verify_expected_outputs` remains the only mechanical verifier. A
    persisted execution-verification event is preferred over a fresh glob so a
    closure can freeze the exact observation later consumed by route
    finalization. A validated exact-output correction is re-evaluated against
    its corrected binding until the existing writer persists the superseding
    observation.
    """
    presence = describe_external_route_submission_presence(
        state,
        scheduler=scheduler,
        job_id=job_id,
        namespace=namespace,
        launch_host=launch_host,
        scheduler_cluster=scheduler_cluster,
        resource_uid=resource_uid,
        submission_nonce=submission_nonce,
        process_group_id=process_group_id,
        process_start_ticks=process_start_ticks,
        container_runtime_id=container_runtime_id,
    )
    status = str(presence.get("status") or "indeterminate")
    if status != "present":
        return {
            "status": status,
            "reason": str(presence.get("reason") or ""),
            "declared": False,
            "passed": status in {"absent", "not_applicable"},
            "expected_outputs": [],
            "verified_output_specs": [],
            "verified_outputs": [],
            "missing_expected_outputs": [],
            "route_presence": presence,
        }

    attempt_id = str(presence.get("attempt_id") or "").strip()
    try:
        events, warnings = _read_transcript_events(state)
    except Exception as exc:
        return {
            "status": "indeterminate",
            "reason": "route_transcript_unreadable",
            "error_type": type(exc).__name__,
            "declared": False,
            "passed": False,
            "attempt_id": attempt_id,
            "route_presence": presence,
        }
    blocking = _blocking_event_history_warning(warnings)
    if (
        blocking is not None
        or _current_blocking_event_history_warning(state) is not None
    ):
        return {
            "status": "indeterminate",
            "reason": blocking or "invalid_event_history",
            "declared": False,
            "passed": False,
            "attempt_id": attempt_id,
            "route_presence": presence,
        }
    identity_query = _normalized_external_identity({
        "scheduler": scheduler,
        "job_id": job_id,
        "namespace": namespace,
        "launch_host": launch_host,
        "scheduler_cluster": scheduler_cluster,
        "resource_uid": resource_uid,
        "submission_nonce": submission_nonce,
        "process_group_id": process_group_id,
        "process_start_ticks": process_start_ticks,
        "container_runtime_id": container_runtime_id,
    })
    matches = [
        (candidate_attempt, receipt)
        for candidate_attempt, receipt in _external_submission_receipts(events)
        if _external_receipt_matches_query(receipt, identity_query)
    ]
    matches = list({
        (candidate_attempt, _external_receipt_key(receipt)):
            (candidate_attempt, receipt)
        for candidate_attempt, receipt in matches
    }.values())
    if len(matches) != 1:
        return {
            "status": "indeterminate",
            "reason": (
                "route_submission_not_found" if not matches
                else "route_submission_ambiguous"
            ),
            "declared": False,
            "passed": False,
            "route_presence": presence,
        }
    attempt_id, authoritative_receipt = matches[0]
    bindings = [
        event for event in events
        if event.get("event") == "route_step_bound"
        and str(event.get("attempt_id") or "") == attempt_id
    ]
    if len(bindings) != 1 or not (
        bindings[0].get("tool") == "submit_job"
        and bindings[0].get("applied_policy") == "managed_external_job"
    ):
        return {
            "status": "indeterminate",
            "reason": "route_external_attempt_not_managed_submission",
            "declared": False,
            "passed": False,
            "attempt_id": attempt_id,
            "route_presence": presence,
        }

    binding = bindings[0]
    corrected = _validated_expected_outputs_correction(state, events, attempt_id)
    effective_binding = (
        {**binding, "expected_outputs": list(corrected)}
        if corrected is not None else binding
    )
    expected = [
        str(item) for item in (effective_binding.get("expected_outputs") or [])
        if str(item).strip()
    ]
    execution_events = _active_attempt_projection_events(
        state,
        events,
        event_type="route_step_external_execution_verified",
        attempt_id=attempt_id,
    )
    if len(execution_events) > 1:
        return {
            "status": "indeterminate",
            "reason": "route_external_execution_verification_conflict",
            "declared": bool(expected),
            "passed": False,
            "attempt_id": attempt_id,
            "expected_outputs": expected,
            "route_presence": presence,
        }

    if execution_events:
        persisted = execution_events[0]
        if (
            _external_receipt_key(persisted)
            != _external_receipt_key(authoritative_receipt)
            or str(persisted.get("route_attempt_id") or "") != attempt_id
        ):
            return {
                "status": "indeterminate",
                "reason": "route_external_execution_verification_conflict",
                "declared": bool(expected),
                "passed": False,
                "attempt_id": attempt_id,
                "expected_outputs": expected,
                "route_presence": presence,
            }

    current_contract_hash, current_outputs = _attempt_step_contract(
        state, events, attempt_id)
    bound_contract_hash = str(binding.get("step_execution_contract_hash") or "")
    if bound_contract_hash != current_contract_hash and corrected is None:
        return {
            "status": "indeterminate",
            "reason": "route_attempt_contract_changed_without_validated_correction",
            "declared": bool(expected),
            "passed": False,
            "attempt_id": attempt_id,
            "expected_outputs": expected,
            "bound_step_execution_contract_hash": bound_contract_hash,
            "step_execution_contract_hash": str(current_contract_hash or ""),
            "route_presence": presence,
        }
    if current_outputs is None or sorted(expected) != current_outputs:
        return {
            "status": "indeterminate",
            "reason": "route_expected_outputs_contract_mismatch",
            "declared": bool(expected),
            "passed": False,
            "attempt_id": attempt_id,
            "expected_outputs": expected,
            "route_presence": presence,
        }

    observation_source = "binding_verifier"
    if execution_events and (
        corrected is None
        or execution_events[0].get("failure_class")
        != "expected_outputs_missing"
    ):
        event = execution_events[0]
        verified_specs = list(event.get("verified_output_specs") or [])
        verified_paths = list(event.get("verified_outputs") or [])
        missing = list(event.get("missing_expected_outputs") or [])
        passed = (
            str(event.get("route_outcome") or "") == "success" and not missing
        )
        observation_source = "route_step_external_execution_verified"
        observations = _precomputed_output_observations(event) or ([], False)
    else:
        # 035a：外部作业的上界——物理结束 / 首次终态观测时刻——从完成门传入。
        bounded_binding = (
            {**effective_binding, "not_after_ns": not_after_ns}
            if isinstance(not_after_ns, int) and not_after_ns > 0 else effective_binding
        )
        verified_specs, verified_paths, missing = _verify_expected_outputs(
            bounded_binding
        )
        passed = not missing
        if corrected is not None:
            observation_source = "validated_correction_reverification"
        # P0a v4：完成门在这里做首次观测，身份收据随它一起交给 writer 冻结。
        observations = _output_observations(bounded_binding, verified_specs)

    return {
        "status": "ready",
        "reason": "route_expected_outputs_resolved",
        "declared": bool(expected),
        "passed": bool(passed),
        "attempt_id": attempt_id,
        "route_step_id": str(binding.get("route_step_id") or ""),
        "step_execution_contract_hash": str(current_contract_hash or ""),
        "bound_step_execution_contract_hash": str(
            binding.get("step_execution_contract_hash") or ""
        ),
        "expected_outputs": expected,
        "verified_output_specs": verified_specs,
        "verified_outputs": verified_paths,
        "missing_expected_outputs": missing,
        "output_observations": observations[0],
        "output_observations_truncated": observations[1],
        "observation_source": observation_source,
        "correction_applied": corrected is not None,
        "route_presence": presence,
    }

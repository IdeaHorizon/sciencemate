"""Methods for checking a frozen natural-language task, not a second task contract.

The model interprets requirements; the runtime executes checks and records their
results. The runtime binds explanations to the frozen received task; neither
verbatim quotation nor paraphrase proves semantic completeness.
"""
from __future__ import annotations

from typing import Any

try:
    from . import obligations
except ImportError:  # pragma: no cover
    from tools import obligations  # type: ignore


SEMANTIC_COVERAGE = "model_interpretation"
LIMITATION = (
    "Task verification methods are the model's interpretation of the frozen task. "
    "The runtime evaluated declared objects and listed checks, not the semantic completeness "
    "or sufficiency of that interpretation. Arbitrary shell checks do not provide a complete "
    "file-read dependency trace; object identity and command execution are separate evidence."
)


class VerificationError(ValueError):
    """Malformed method declaration, distinct from corrupt persisted task data."""

_COMMON = {"kind", "note"}
_FIELDS = {
    "file": _COMMON | {"path", "non_empty"},
    "glob": _COMMON | {"pattern", "min_count"},
    "check": _COMMON | {"command", "expect_exit", "expect_stdout_contains", "timeout_s", "cwd"},
}
_METHOD_SHAPES = {
    "file": '{"kind": "file", "path": "result.txt", "non_empty": false}',
    "glob": '{"kind": "glob", "pattern": "results/*.json", "min_count": 1}',
    "check": '{"kind": "check", "command": "test -f result.txt", "cwd": "<absolute working directory>", "expect_exit": 0}',
}


def prepare(state: Any, verification: Any) -> dict[str, Any]:
    """Validate methods before executing any of them. Never write obligations A.

    There are no model-supplied internal IDs, truth values or required flags.
    Every supplied method is evaluated; formal requirements remain independent.
    """
    source = obligations.task_source(state)
    if verification is None or verification == []:
        return {"source_task": source, "semantic_coverage": "not_assessed", "requirements": [], "checks": []}
    if not isinstance(verification, list) or len(verification) > 100:
        raise VerificationError("verification must be an array of at most 100 requirement/check entries")
    if not source:
        raise VerificationError("verification needs the original task text; formal required checks still run without it")
    entries, checks = [], []
    names = {row["id"] for row in obligations.frozen(state)}
    for index, entry in enumerate(verification):
        if not isinstance(entry, dict) or set(entry) - {"requirement", "checks", "explanation"}:
            raise VerificationError(f"verification[{index}] accepts requirement, checks and optional explanation, not claimed results")
        requirement = entry.get("requirement")
        if not isinstance(requirement, str) or not requirement.strip():
            raise VerificationError(f"verification[{index}].requirement must explain the task requirement")
        explanation = entry.get("explanation", "")
        if not isinstance(explanation, str):
            raise VerificationError(f"verification[{index}].explanation must be a string")
        methods = entry.get("checks")
        if not isinstance(methods, list) or not methods or len(methods) > 100:
            raise VerificationError(f"verification[{index}].checks needs concrete file/glob/check methods, not a passed assertion")
        ids = []
        for method_index, method in enumerate(methods):
            if len(checks) >= 100:
                raise VerificationError("verification supports at most 100 checks per finish call")
            if not isinstance(method, dict) or method.get("kind") not in _FIELDS:
                raise VerificationError("verification checks must have kind file, glob or check")
            kind = method["kind"]
            location = f"verification[{index}].checks[{method_index}]"
            unsupported = set(method) - _FIELDS[kind]
            if unsupported:
                raise VerificationError(
                    f"{location}: {kind} has unsupported fields {sorted(unsupported)}; "
                    f"accepted fields: {', '.join(sorted(_FIELDS[kind]))}. "
                    f"Valid shape: {_METHOD_SHAPES[kind]}. "
                    "Results, IDs and required flags belong to the runtime. "
                    "No verification checks have run in this finish call; correct the fields and retry finish."
                )
            primary = {"file": "path", "glob": "pattern", "check": "command"}[kind]
            if not isinstance(method.get(primary), str) or not method[primary].strip():
                raise VerificationError(
                    f"{location}: {kind} needs a non-empty {primary}. "
                    f"Valid shape: {_METHOD_SHAPES[kind]}. "
                    "No verification checks have run in this finish call; correct the field and retry finish."
                )
            for key in ("note", "expect_stdout_contains", "cwd"):
                if key in method and not isinstance(method[key], str):
                    raise VerificationError(f"verification {key} must be a string")
            for key in ("min_count", "expect_exit", "timeout_s"):
                if key in method and type(method[key]) is not int:
                    raise VerificationError(f"verification {key} must be an integer")
            if "non_empty" in method and type(method["non_empty"]) is not bool:
                raise VerificationError("verification non_empty must be a boolean")
            if method.get("min_count", 1) < 1 or not 1 <= method.get("timeout_s", 120) <= 300:
                raise VerificationError("verification min_count must be positive and timeout_s between 1 and 300")
            row = obligations._normalize(method, len(checks), source="model_interpretation")
            if kind == "file":
                # A method does not silently add a task-level size condition.
                # Keep the frozen formal-obligation default in _normalize intact.
                row["non_empty"] = method.get("non_empty", False)
            # Names only join the derived result rows; they are not task or job identities.
            name = f"task_verification_{len(checks)}"
            while name in names:
                name += "_"
            names.add(name)
            row["id"] = name
            row["requirement"] = requirement
            checks.append(row)
            ids.append(row["id"])
        entries.append({"requirement": requirement, "explanation": explanation, "check_names": ids})
    return {"source_task": source, "semantic_coverage": SEMANTIC_COVERAGE,
            "requirements": entries, "checks": checks}


CHECK_SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": ["file", "glob", "check"],
                 "description": "file 用 path；glob 用 pattern/min_count；check 用 command/cwd，只读核验。不要混用字段。"},
        "path": {"type": "string", "description": "仅 kind=file：单个文件路径，按 Core 工具工作区坐标解释；glob 不使用 path。"},
        "pattern": {"type": "string", "description": "仅 kind=glob：用 pattern（不是 path）给工作区 glob，例如 results/*.json。"},
        "non_empty": {"type": "boolean", "default": False,
                      "description": "仅 kind=file：默认 false，只核对存在、身份与来源，不要求内容非空；false 不证明零字节。任务要求确实为空时，用只读 check 检查实际大小；要求非空时显式 true。"},
        "min_count": {"type": "integer", "minimum": 1, "default": 1,
                      "description": "仅 kind=glob：至少匹配的文件数，正整数，默认 1；配合 pattern，不是每个文件的字节数。"},
        "command": {"type": "string", "description": "仅 kind=check：实际执行的只读核验命令；命令内相对路径基于 cwd。先用执行工具完成写入和作业操作，这里不重做副作用。"},
        "expect_exit": {"type": "integer"},
        "expect_stdout_contains": {"type": "string",
                                   "description": "仅 kind=check：按字面子串匹配标准输出，不作数值等价判断。数值先在检查脚本内断言（浮点比较使用适合任务及精度的容差），全部通过后输出稳定文本标记，避免依赖浮点打印格式。"},
        "timeout_s": {"type": "integer", "minimum": 1, "maximum": 300},
        "cwd": {"type": "string", "description": "仅 kind=check：命令工作目录，建议显式绝对路径；省略时使用当前运行的默认工作目录。"},
        "note": {"type": "string"},
    },
    "required": ["kind"], "additionalProperties": False,
}

SCHEMA = {
    "type": "array", "maxItems": 100,
    "description": (
        "自然任务的实际验收：逐项说明任务要求 requirement，给 file/glob 产物和/或只读 check 命令。"
        "框架实际核验，不接受自报 passed。说明怎样覆盖原要求；不得降低要求或只挑已成功部分。"
        "需写文件或管理进程的实际操作先用执行工具完成，这里只读核对其结果，不重做副作用。"
        "检查方法可以调整，不修改原任务；已有正式检查仍全部生效。工具路径相对工作区，check 内路径相对其 cwd。"
    ),
    "items": {"type": "object", "properties": {
        "requirement": {"type": "string", "minLength": 1, "description": "对任务要求的忠实解释，无需逐字引句；不能降低要求"},
        "checks": {"type": "array", "minItems": 1, "maxItems": 100, "items": CHECK_SCHEMA},
        "explanation": {"type": "string"},
    }, "required": ["requirement", "checks"], "additionalProperties": False},
}

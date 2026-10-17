"""A 里的 required obligations（v3 governing document §2、§3、§6）。

run 开始时框架保存任务原文、已提供的结构化 required obligations 与 harness 的
required artifact 类型。自然任务没有内部验收数组不等于缺少任务：原文完整冻结，
模型在 finish 里解释要求、给出核验方法，运行时记录真实证据；方法不能改写 A。
本模块不解析任意自然语言，也不把这份原文快照当作语义完整性已被机械证明。

冻结后：required 项不可删、不可改成功标准；模型可追加 optional 项（C 中的可选
发现），不能事后修改 A。没有"声明交付物"这样的必经工具。

一条 obligation 的形状（每项一句可核的话）：
    {"id": "wrf_exe", "kind": "file", "path": "<abs or run-relative>", "required": true}
    {"id": "outputs", "kind": "glob", "pattern": "run/out/*.nc", "min_count": 1, "required": true}
    {"id": "smoke",   "kind": "check", "command": "./bin/smoke", "expect_exit": 0,
                      "expect_stdout_contains": "HF_TOOLCHAIN_SMOKE_OK", "required": true}
    {"id": "raw_results", "kind": "artifact", "artifact_type": "raw_results", "required": true}
    {"id": "task_text", "kind": "task", "text": "<原任务全文>", "required": true,
     "source": "node_inputs.experiment_spec"}
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

try:
    from . import ledger as _ledger
except ImportError:  # pragma: no cover - standalone node bootstrap
    from tools import ledger as _ledger  # type: ignore

_OBLIGATIONS_FILE = "obligations.json"
_KINDS = {"file", "glob", "check", "artifact"}
_CLOSURE_ARTIFACT_TYPES = ("raw_results", "clean_results", "experiment_log")
_INPUT_KEYS = ("required_outputs", "deliverables", "obligations")
_TASK_PROSE_KEYS = ("experiment_spec", "experiment_focus", "requested_work")


class ObligationError(ValueError):
    pass


def obligations_path(state: Any) -> Path:
    return _ledger.ledger_dir(state) / _OBLIGATIONS_FILE


def _node_inputs(state: Any) -> dict[str, Any]:
    try:
        payload = state.hook_state.get("node_inputs") or {}
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _input_task_source(inputs: dict[str, Any]) -> dict[str, str] | None:
    """Read declared prose fields only; preserve the complete original text."""
    for key in _TASK_PROSE_KEYS:
        text = inputs.get(key)
        if isinstance(text, str) and text.strip():
            return {"source": f"node_inputs.{key}", "text": text}
    return None


def _normalize(item: Any, index: int, *, source: str) -> dict[str, Any]:
    if isinstance(item, str):
        item = {"path": item}
    if not isinstance(item, dict):
        raise ObligationError(f"obligation #{index} must be an object or path string")
    kind = str(item.get("kind") or ("check" if item.get("command") else "glob" if item.get("pattern")
                                    else "artifact" if item.get("artifact_type") else "file"))
    if kind not in _KINDS:
        raise ObligationError(f"obligation #{index}: unknown kind {kind!r}")
    row: dict[str, Any] = {"id": str(item.get("id") or f"{kind}_{index}"), "kind": kind,
                           "required": bool(item.get("required", True)), "source": source}
    if kind == "file":
        path = str(item.get("path") or "").strip()
        if not path:
            raise ObligationError(f"obligation #{index}: file obligation needs path")
        row["path"] = path
        row["non_empty"] = bool(item.get("non_empty", True))
    elif kind == "glob":
        pattern = str(item.get("pattern") or "").strip()
        if not pattern:
            raise ObligationError(f"obligation #{index}: glob obligation needs pattern")
        row["pattern"] = pattern
        row["min_count"] = int(item.get("min_count") or 1)
    elif kind == "check":
        command = str(item.get("command") or "").strip()
        if not command:
            raise ObligationError(f"obligation #{index}: check obligation needs command")
        row["command"] = command
        row["expect_exit"] = int(item.get("expect_exit", 0))
        if item.get("expect_stdout_contains"):
            row["expect_stdout_contains"] = str(item["expect_stdout_contains"])
        row["timeout_s"] = int(item.get("timeout_s") or 120)
        row["cwd"] = str(item.get("cwd") or "") or None
    else:
        artifact_type = str(item.get("artifact_type") or "").strip()
        if not artifact_type:
            raise ObligationError(f"obligation #{index}: artifact obligation needs artifact_type")
        row["artifact_type"] = artifact_type
    if item.get("note"):
        row["note"] = str(item["note"])
    return row


def derive_from_inputs(state: Any, harness_required_types: list[str] | None = None) -> dict[str, Any]:
    """冻结已有结构化要求，或完整任务原文；不猜自然语言的验收语义。

    返回 {"obligations": [...], "derived_from": [...], "clarification_required": bool}。
    只有正文与结构化交付要求都缺席时才要求澄清；三件 artifact 永远自动含。
    """
    inputs = _node_inputs(state)
    source = _input_task_source(inputs)
    rows: list[dict[str, Any]] = []
    sources: list[str] = []
    for key in _INPUT_KEYS:
        raw = inputs.get(key)
        if isinstance(raw, list) and raw:
            for index, item in enumerate(raw):
                rows.append(_normalize(item, index, source=f"node_inputs.{key}"))
            sources.append(f"node_inputs.{key}")
            break
    if source is not None:
        sources.append(source["source"])
        if not any(row["kind"] != "artifact" and row["required"] for row in rows):
            rows.append({"id": "task_text", "kind": "task", "text": source["text"],
                         "source": source["source"], "required": True})
    types = list(harness_required_types or _CLOSURE_ARTIFACT_TYPES)
    for artifact_type in types:
        rows.append({"id": artifact_type, "kind": "artifact", "artifact_type": artifact_type,
                     "required": True, "source": "harness.required_output_artifact_types"})
    sources.append("harness.required_output_artifact_types")
    deliverable_rows = [r for r in rows if r["kind"] != "artifact" and r["required"]]
    return {"obligations": rows, "derived_from": sources,
            "task_source": source, "clarification_required": not deliverable_rows}


def freeze(state: Any, *, supplied: list[Any] | None = None,
           harness_required_types: list[str] | None = None) -> dict[str, Any]:
    """冻结 required obligations。已冻结则原样返回（幂等），不接受改写。

    supplied：保留既有缺输入调用路径，仅当任务原文和结构化要求均缺席时使用；
    不能覆盖自然任务或正式结构化要求，不是模型必须调用的声明入口。
    """
    path = obligations_path(state)
    if path.exists():
        frozen = json.loads(path.read_text(encoding="utf-8"))
        return {"status": "already_frozen", **frozen}
    derived = derive_from_inputs(state, harness_required_types)
    rows = derived["obligations"]
    sources = derived["derived_from"]
    if derived["clarification_required"]:
        if not supplied:
            return {"status": "clarification_required",
                    "reason": "task inputs contain neither task prose nor structured requirements; clarify the actual task",
                    "template": [{"kind": "file", "path": "<abs path>", "required": True},
                                 {"kind": "check", "command": "<cmd>", "expect_exit": 0,
                                  "expect_stdout_contains": "<marker>", "required": True}],
                    "derived": rows}
        clarified = [_normalize(item, index, source="clarification") for index, item in enumerate(supplied)]
        rows = clarified + rows
        sources = ["clarification", *sources]
    ids = [r["id"] for r in rows]
    if len(ids) != len(set(ids)):
        raise ObligationError(f"duplicate obligation ids: {sorted({i for i in ids if ids.count(i) > 1})}")
    payload = {"obligations": rows, "derived_from": sources,
               "task_source": derived["task_source"], "frozen": True}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    _ledger.try_record(state, "obligations_frozen", count=len(rows), derived_from=sources,
                       ids=ids)
    return {"status": "frozen", **payload}


def frozen(state: Any) -> list[dict[str, Any]]:
    path = obligations_path(state)
    if not path.exists():
        return []
    return list(json.loads(path.read_text(encoding="utf-8")).get("obligations") or [])


def task_source(state: Any) -> dict[str, str] | None:
    """Read only the frozen task source; never substitute mutable resume inputs."""
    path = obligations_path(state)
    if not path.exists():
        return None
    source = json.loads(path.read_text(encoding="utf-8")).get("task_source")
    if not isinstance(source, dict):
        return None
    if not isinstance(source.get("source"), str) or not isinstance(source.get("text"), str):
        return None
    return {"source": source["source"], "text": source["text"]}


def add_optional(state: Any, items: list[Any]) -> dict[str, Any]:
    """模型可追加 optional 项（C 中的可选发现）；不能改 required 项。"""
    path = obligations_path(state)
    if not path.exists():
        raise ObligationError("obligations are not frozen yet")
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = list(payload.get("obligations") or [])
    existing = {r["id"] for r in rows}
    added = []
    for index, item in enumerate(items):
        row = _normalize(item, len(rows) + index, source="model_optional")
        row["required"] = False
        if row["id"] in existing:
            raise ObligationError(f"obligation id {row['id']!r} already exists; required items are immutable")
        rows.append(row)
        added.append(row["id"])
    payload["obligations"] = rows
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    _ledger.try_record(state, "obligations_optional_added", ids=added)
    return {"status": "added", "ids": added}


def resolve_path(state: Any, value: str) -> Path:
    """Core tool coordinates, with the existing explicit role read adapter.

    This grants no write capability. Glob prefixes use the same resolver;
    each expanded target must be checked again by the caller.
    """
    import glob
    from core.project_workspace import resolve_tool_path, ProjectWorkspaceError
    p = Path(value).expanduser()
    for index, part in enumerate(p.parts):
        if glob.has_magic(part):
            prefix = Path(*p.parts[:index]) if index else Path(".")
            return resolve_path(state, str(prefix)).joinpath(*p.parts[index:])
    try:
        return resolve_tool_path(state, value, write=False)
    except ProjectWorkspaceError:
        # Existing node path roles can identify an external dependency/source;
        # a failed Core lookup never means arbitrary absolute paths are allowed.
        from .path_roles import matching_path_roles, validate_path_roles
        target = p.resolve(strict=False)
        contract = validate_path_roles(state)
        conflicting = any(target.is_relative_to(Path(root))
                          for root in contract.get("conflict_paths", []))
        if p.is_absolute() and not conflicting and any(
                not role.container_only for role in matching_path_roles(target, state)):
            return target
        raise


def input_reference(state: Any, path: Path) -> dict[str, str] | None:
    """A known input role, not a model-selectable provenance exemption."""
    from .path_roles import matching_path_roles, validate_path_roles
    if any(path.is_relative_to(Path(root))
           for root in validate_path_roles(state).get("conflict_paths", [])):
        return None
    matches = matching_path_roles(path, state)
    if matches and all(not r.writable and not r.container_only and
                       r.role in {"source_baseline_root", "dependency_root"} for r in matches):
        role = matches[0]
        return {"role": role.role, "root": role.path, "source": role.source}
    return None

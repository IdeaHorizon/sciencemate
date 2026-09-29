"""hypothesis 节点专用 artifact 保存 —— singleton 覆盖 + 禁止 _v2 命名。"""
from __future__ import annotations

import base64
import binascii
import json
import re
from typing import Any

from core.state import State
from core.tool_registry import ToolDefinition, register_tool

# singleton 是类型性质，声明在 shared.lib.artifact_policy —— 这里不再有私有名单。
from shared.lib.artifact_policy import (
    SINGLETON_VERSION_SUFFIX as _VERSION_SUFFIX_RE,
)
from shared.lib.artifact_policy import (
    is_singleton as _is_singleton,
)
from shared.lib.artifact_policy import (
    singleton_types as _singleton_types,
)

from .artifact_staging import stage_draft_content


def normalize_singleton_name(artifact_type: str, name: str) -> tuple[str, str | None]:
    if not _is_singleton(artifact_type):
        return name, None
    cleaned = _VERSION_SUFFIX_RE.sub("", name.strip()).strip("_- ")
    if not cleaned:
        return name, None
    if cleaned != name:
        return cleaned, name
    return name, None


def purge_superseded_singletons(state: State, artifact_type: str, keep_slug: str) -> list[str]:
    """单例类型只留一个身份：别的未冻结身份撤下（账本留行，文件删掉）。

    此前直接 `unlink` 文件 —— 在版本原语之外删除，账本对此一无所知。现在走
    `State.retire_artifact`：冻结的拒绝，其余留痕。
    """
    if not _is_singleton(artifact_type):
        return []
    keep_id = f"{artifact_type}__{keep_slug}"
    removed: list[str] = []
    for entry in state.list_artifacts(artifact_type, own_only=True):
        artifact_id = str(entry.get("id") or "")
        if not artifact_id or artifact_id == keep_id:
            continue
        try:
            if state.retire_artifact(artifact_id, reason=f"superseded by {keep_id}"):
                removed.append(artifact_id)
        except PermissionError:
            continue        # 冻结的不撤
    return removed


def _decode_content_b64(content_b64: str) -> str:
    try:
        raw = base64.b64decode(content_b64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"content_b64 解码失败：{exc}") from exc
    return raw.decode("utf-8")


def _read_content_file(state: State, content_file: str) -> str:
    # 路径解析调框架的唯一解析器，别自己再实现一遍边界。这里原来手写了一份
    # 只认 `state.root`（run 状态缓存）的检查 —— 但 v2.1 之后模型的工作面在
    # Git worktree（bash 的 cwd、草稿的落点都在那），于是 content_file 的合法
    # 空间与模型实际能写文件的空间**零交集**：一次保存连撞七次（三种相对拼法
    # "不存在"、真实存在的绝对路径"必须在 run 目录内"），报错还不说合法根在哪
    # （2026-08-13 现场，run_b25823eb…hypothesis@d1）。
    # resolve_tool_path 的读契约与 read_file 相同：相对路径锚在模型文件工具的
    # cwd（working_directory），绝对路径允许 run 目录或 Project 工作区之内。
    from core.project_workspace import (
        ProjectWorkspaceError,
        resolve_tool_path,
        working_directory,
    )

    try:
        path = resolve_tool_path(state, content_file, write=False)
    except ProjectWorkspaceError as exc:
        raise ValueError(str(exc)) from exc
    if not path.is_file():
        raise ValueError(
            f"content_file 不存在：{content_file}"
            f"（相对路径锚在 {working_directory(state)}；"
            f"也接受 run 目录或 Project 工作区内的绝对路径）"
        )
    return path.read_text(encoding="utf-8")


def resolve_artifact_content(
    state: State,
    *,
    content: str | None = None,
    content_b64: str | None = None,
    content_file: str | None = None,
) -> str:
    sources = [content, content_b64, content_file]
    provided = sum(1 for s in sources if s)
    if provided != 1:
        raise ValueError("content / content_b64 / content_file 三选一必填")
    if content_b64:
        return _decode_content_b64(content_b64)
    if content_file:
        return _read_content_file(state, content_file)
    return content or ""


def save_hypothesis_singleton(
    state: State,
    artifact_type: str,
    name: str,
    content: str,
    metadata: dict | None = None,
) -> dict:
    """Save revisable hypothesis artifact: strip _v2 suffix, drop older same-type files."""
    name, normalized_from = normalize_singleton_name(artifact_type, name)
    meta = dict(metadata or {})
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-") or "x"
    slug = slug[:60]
    superseded = purge_superseded_singletons(state, artifact_type, slug)
    if superseded:
        meta["superseded_artifact_ids"] = superseded
    result = state.save_artifact(artifact_type, name, content, metadata=meta)
    out = {"status": "success", **result, "name": name}
    if normalized_from:
        out["name_normalized_from"] = normalized_from
    if superseded:
        out["superseded"] = superseded
    return out


async def _stage_hypothesis_draft(
    state: State,
    artifact_type: str,
    content: str,
    **_: Any,
) -> dict:
    # artifact_type 的合法集由 parameters_schema 的 enum 声明，派发口按 schema
    # 核取值（判决拆除三波：契约归 schema，工具体内不再手写一份）。
    if not content or not content.strip():
        return {"status": "error", "error": "content 不能为空"}
    rel = stage_draft_content(state, artifact_type, content)
    return {
        "status": "success",
        "staged_file": rel,
        "bytes": len(content.encode("utf-8")),
        "message": (
            f"草稿已写入 {rel}。请调用 save_artifact("
            f"artifact_type={artifact_type!r}, name=..., content_from_file={rel!r})"
        ),
    }



register_tool(
    ToolDefinition(
        name="stage_hypothesis_draft",
        description=(
            "把 hypothesis singleton 产出草稿写入 run 内 drafts/*.md。\n\n"
            "**Use when**：正文较长（如 hypothesis_research_overview），"
            "避免大正文 content 在单 turn 被 max_output_tokens 截断。\n\n"
            "**下一步**：save_artifact(..., content_from_file=<返回的 staged_file>)。"
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "artifact_type": {
                    "type": "string",
                    "enum": list(_singleton_types()),
                },
                "content": {"type": "string"},
            },
            "required": ["artifact_type", "content"],
        },
        allowed_node_types=["hypothesis"],
    ),
    _stage_hypothesis_draft,
)

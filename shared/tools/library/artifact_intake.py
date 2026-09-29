"""外部材料的合法入口 —— `import_artifact`。

不是"放宽门禁"，是把门禁问错的那个问题问对：

    required_input_artifact_types 想保证的是"有没有实验记录"（防 writing 编
    数据）。它实际保证的是"这个平台跑没跑过实验"。拿别人产的数据写论文是
    科研常态，框架不该把它判成非法。

所以给外部材料一条**带诚实标记**的入口。放宽入口的代价必须由出口承担：
导入的工件永远带 `provenance.kind='imported'`，转发多少次都洗不掉
（见 core/artifact_provenance），消费方据此如实披露。

## 边界（想清楚了才敢开）

- **位置即来源（P6，2026-08-08）。** 源文件必须在 `<worktree>/resources/` 下。
  外来件先放进 resources/ 再导入；节点自己目录里的东西是本流水线的产出，
  导它就是来源失真（E2E v19 实测：orchestrator 把没能冻结的节点产物抄出来
  冻上，完成度闸被架空）。P6 落完（QC 门直读文件）后本工具整个删除。
- **只给 orchestrator。** producing 节点自己导入等于自产自销，那正是要防的。
- **不许伪造 frozen。** `pre_registration` 的防篡改语义靠 `metadata.frozen`
  —— 导入时强制剥掉，所以导入一份"已冻结"的预注册**不能**解开 experiment
  的冻结门禁。外部预注册（OSF 之类）要不要认，是另一个决定，不在这里偷渡。
- **不许无源导入。** 必须给真实存在的文件路径，落 sha256 + size，事后可核。
- **内容有界。** 复用 read_file 的上限：导入一个 2MB 单行 JSON 不该把
  context 顶爆（2026-08-04 就是这么死的一个 run）。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from core import artifact_provenance as _prov
from core import paths
from core.state import State

from core.artifact_capabilities import _FREEZE_OWNED_METADATA
from core.tool_registry import ToolDefinition, register_tool

#: 与 read_file 同一个上限 —— 导入不该成为绕过有界读的后门
_MAX_IMPORT_BYTES = 256_000

#: 这些 metadata 键带的是"防篡改/已审查"语义，导入的材料不许自带。
#:
#: 冻结那几个键**从 artifact_capabilities 引用**，不再手抄一份：原先这里抄的是
#: `("frozen", "frozen_at")`，漏了 `freeze_reason` —— 抄件各自演化，而分叉时
#: 两边都不报错。这个漏是 tests/test_freezing_cannot_be_forged.py 的扫盘抓出来的。
_FORBIDDEN_METADATA = (*_FREEZE_OWNED_METADATA, "_forwarded_input",
                       "produced_by_node_type", "produced_by_run_id")


def _classify_source_location(state: State, path: Path) -> str | None:
    """sources/ 下返回 'sources'，其余返回 None。

    位置即来源（P6 设计，2026-08-08 拍板）：外来件唯一的合法落点是用户材料
    目录 `<worktree>/sources/`（`core.materials.MATERIALS_RELATIVE`，不在这里
    再写一遍目录名）。这里不做名单、不做例外 —— 规则越机械越守得住。
    """
    from core.materials import MATERIALS_RELATIVE

    worktree = getattr(state, "project_worktree", None)
    if not worktree:
        return None
    try:
        resolved = path.resolve()
        target = (Path(worktree) / MATERIALS_RELATIVE).resolve()
    except OSError:
        return None
    return "sources" if (resolved == target or target in resolved.parents) else None


def _owning_node_of(state: State, path: Path) -> str | None:
    """这个路径落在哪个节点自有的工作区里？不在任何节点名下就返回 None。

    名单从 `_NODE_WORKSPACES` 现取，不在这里抄一份 —— 以后加节点、改目录，
    这道门自动跟着走；写死名单的话新节点默认漏过。
    """
    worktree = getattr(state, "project_worktree", None)
    if not worktree:
        return None
    from core.project_workspace import _NODE_WORKSPACES

    try:
        resolved = path.resolve()
        root = Path(worktree).resolve()
    except OSError:
        return None

    best: tuple[int, str] | None = None
    for node_type, owned in _NODE_WORKSPACES.items():
        if node_type.startswith("_"):
            continue           # 同一个目录有 `_x` 和 `x` 两个键，报名字报不带下划线的
        target = (root / owned).resolve()
        if resolved == target or target in resolved.parents:
            depth = len(target.parts)
            if best is None or depth > best[0]:
                best = (depth, node_type)   # 最深的匹配才是真正的所有者
    return best[1] if best else None


async def _import_artifact(state: State, artifact_type: str, name: str,
                           source_path: str, note: str | None = None,
                           **_: Any) -> dict:
    # 「只给 orchestrator」归注册表：ToolDefinition.allowed_node_types（见下方
    # register_tool）。producing 节点的工具面上根本没有这个工具，不在函数体里
    # 再查一次角色。
    # 外来件的唯一合法落点是 `<worktree>/sources/` —— 拿 run 根去解相对路径，
    # 恰恰把这个唯一合法写法解到不存在的位置上（"sources/x.pdf" → 找不到文件）。
    p = paths.resolve_display_relpath(state, source_path)
    if not p.exists():
        tried = ", ".join(str(c) for c in paths.display_relpath_candidates(state, source_path))
        return {"status": "error", "error": f"找不到文件：{source_path}（找过：{tried}）"}
    if not p.is_file():
        return {"status": "error", "error": f"{p} 不是文件"}

    location = _classify_source_location(state, p)
    if location != "sources":
        owner = _owning_node_of(state, p)
        if owner is not None:
            return {
                "status": "error",
                "error": (
                    f"{p} 在 {owner} 节点自己的目录里 —— 那是本流水线的产出，"
                    f"不是外部材料。import_artifact 只收外来件（它会把来源标成 "
                    f"provenance='imported'，把内部产物标成外来件就是来源失真）。\n"
                    f"要用别的节点的产物，走跨节点读：list_artifacts() 找到 id，"
                    f"再 read_artifact(id)。\n"
                    f"如果那份产物还没 frozen，那是它自己的完成度门禁没过 —— "
                    f"抄一份出来冻上等于把闸架空。让 {owner} 重跑并补齐，别绕过去。"
                ),
                "owning_node": owner,
            }
        return {
            "status": "error",
            "error": (
                f"{p} 不在本项目的 sources/ 目录里。外来件的**位置即来源**：用户从"
                f"界面交来的文件已经在 <project>/sources/ 里了（绝对路径每轮"
                f"注入给你），直接导入那个路径。"
                f"这条规则是机械的 —— 只有 sources/ 下的文件会被当作外部材料。"
            ),
            "expected_location": "sources/",
        }

    from core.secrets import is_sensitive_path
    denied = is_sensitive_path(p)
    if denied:
        state.append_transcript("sensitive_path_read_denied",
                                path=str(p), tool="import_artifact")
        return {"status": "error", "error": denied}

    sha, size = _prov.hash_file(p)
    raw = p.read_bytes()[:_MAX_IMPORT_BYTES].decode("utf-8", errors="replace")
    content_truncated = size > _MAX_IMPORT_BYTES

    # 源文件本身就是一份 artifact record（我们自己导出的）→ 只取 content，
    # 不把外层壳一起当内容，也绝不继承它的 provenance/metadata。
    inherited_meta: dict = {}
    if p.suffix == ".json" and not content_truncated:
        try:
            outer = json.loads(raw)
            if isinstance(outer, dict) and "content" in outer and "type" in outer:
                inherited_meta = dict(outer.get("metadata") or {})
                raw = outer["content"] if isinstance(outer["content"], str) \
                    else json.dumps(outer["content"], ensure_ascii=False, indent=2)
        except json.JSONDecodeError:
            pass

    stripped = [k for k in _FORBIDDEN_METADATA if k in inherited_meta]
    for k in stripped:
        inherited_meta.pop(k, None)

    metadata = dict(inherited_meta)
    metadata["imported_from"] = str(p)
    metadata["imported_content_truncated"] = content_truncated
    if note:
        metadata["import_note"] = note

    saved = state.save_artifact(
        artifact_type=artifact_type,
        name=name,
        content=raw,
        metadata=metadata,
        provenance=_prov.imported(source_path=str(p), sha256=sha,
                                  size_bytes=size, by_run_id=state.run_id,
                                  note=note),
    )
    state.append_transcript("artifact_imported", artifact_id=saved["id"],
                            artifact_type=artifact_type, source_path=str(p),
                            sha256=sha, size_bytes=size,
                            stripped_metadata=stripped)
    out = {
        "status": "success",
        "artifact_id": saved["id"],
        "artifact_type": artifact_type,
        "provenance": "imported",
        "source_sha256": sha,
        "source_size_bytes": size,
        "content_truncated": content_truncated,
        "note": ("已作为**外部导入**材料登记 —— 它能满足下游节点的机械输入门，"
                 "但 provenance 恒为 imported，转发/回填都洗不掉。任何基于它的"
                 "产出必须如实说明数据不是本平台产生的。"),
    }
    if stripped:
        out["stripped_metadata"] = stripped
        out["stripped_reason"] = (
            "这些键带防篡改/已审查语义，导入的材料不许自带 —— 尤其 frozen："
            "导入一份'已冻结'的预注册不能解开 experiment 的冻结门禁。")
    if content_truncated:
        out["truncation_note"] = (
            f"源文件 {size} bytes，只取前 {_MAX_IMPORT_BYTES} bytes 作为 artifact "
            f"内容（sha256 记的是**完整文件**）。大数据集别整个导进来 —— "
            f"导一份摘要/日志，原始文件留在 workspace 里让节点按需读。")
    return out


register_tool(
    ToolDefinition(
        name="import_artifact",
        description=(
            "把 workspace 里**外部带进来**的材料登记为工件，让它能满足下游节点的"
            "机械输入门（如 writing 要 experiment_log），而不必为了凑工件去重跑"
            "整条流水线。\n"
            "登记后 provenance 恒为 `imported`（带源路径 + sha256），转发和回填都"
            "洗不掉 —— 下游据此如实披露数据来源。\n"
            "**源文件必须在 <project>/resources/ 下**（位置即来源）——用户从界面交来的"
            "文件就在 `resources/materials/` 里；节点目录里的产物一律拒（要用走 "
            "read_artifact）。\n"
            "只给 orchestrator；不许伪造 frozen（导入的预注册解不开 experiment 的"
            "冻结门禁）；只有真实存在的文件才能导入。"
        ),
        parameters_schema={
            "type": "object",
            "properties": {
                "artifact_type": {
                    "type": "string",
                    "description": "登记成哪种工件类型，如 experiment_log / dataset / survey_report。",
                },
                "name": {"type": "string", "description": "工件名（用于生成 id）。"},
                "source_path": {
                    "type": "string",
                    "description": "源文件路径（相对 state.root 或绝对）。必须真实存在。",
                },
                "note": {
                    "type": "string",
                    "description": "这份材料是哪来的、谁产的、为什么可信。会进 provenance。",
                },
            },
            "required": ["artifact_type", "name", "source_path"],
        },
        risk_level="medium",
        # 角色归注册表：producing 节点自己导入外部材料等于自产自销，那正是
        # provenance 要防的 —— 所以它们的工具面上没有这个工具。
        allowed_node_types=["_orchestrator"],
    ),
    _import_artifact,
)

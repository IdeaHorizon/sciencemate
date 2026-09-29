"""Stage large hypothesis artifact bodies on disk — avoid re-sending in LLM output."""
from __future__ import annotations

from pathlib import Path

from core import paths
from core.state import State

_ARTIFACT_DRAFT_FILES = {
    "research_plan": "research_plan.md",
    "hypothesis_research_overview": "hypothesis_research_overview.md",
    "hypothesis_innovation_report": "hypothesis_innovation_report.md",
    "hypothesis_output_validation": "hypothesis_output_validation.md",
}


def draft_filename(artifact_type: str) -> str:
    """草稿的**文件名**。目录由 `paths.hypothesis_drafts_dir` 唯一决定。

    这里以前返回的是一整条相对路径（`outputs/hypothesis/drafts/x.md`），那是
    第三个锚点：文件实际写在 `<worktree>/hypothesis/drafts/`，返回的字符串却
    两个真锚点都对不上，于是它自己那句"下一步 save_artifact(content_from_file=…)"
    在平台上 100% 读不到。目录的真相源只能有一个，这里只管文件名。
    """
    return _ARTIFACT_DRAFT_FILES.get(artifact_type, f"{artifact_type}.md")


def draft_path(state: State, artifact_type: str) -> Path:
    """草稿的绝对落点 —— 写方和读方都问这里，不各自拼。"""
    return paths.hypothesis_drafts_dir(state) / draft_filename(artifact_type)


def stage_draft_content(state: State, artifact_type: str, content: str) -> str:
    """写下草稿，返回**能交给模型**的那个路径。

    返回值会被原样塞进 `save_artifact(content_from_file=…)`，也就是走模型路径
    方言（锚在 `working_directory`）—— 所以必须由 `tool_relpath` 产出，不能
    自己拼。
    """
    from core.project_workspace import tool_relpath

    path = draft_path(state, artifact_type)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    state.files_read.add(str(path.resolve()))
    return tool_relpath(state, path)


def commit_staged_artifact(
    state: State,
    artifact_type: str,
    name: str,
    content: str,
    *,
    metadata: dict | None = None,
) -> dict:
    """Stage to drafts/ then save singleton artifact."""
    from .artifact_save import save_hypothesis_singleton

    rel = stage_draft_content(state, artifact_type, content)
    saved = save_hypothesis_singleton(state, artifact_type, name, content, metadata=metadata)
    return {
        "status": "success",
        "staged_file": rel,
        "saved_via": "audit_auto_commit",
        **saved,
    }

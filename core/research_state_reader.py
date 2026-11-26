"""research_state 的**唯一**读取实现（一个问题一个真相源）。

research_state 是 Analysis（analysis 节点）持有的版本化研究状态：单一身份
`research_state__research_state`，每次更新版本 +1，版本号在 metadata.version（与
账本 version 一致）。历史版本在账本里（`core/ledger`：每次 save 一行，正文按需
从 run 内快照或 git 历史取回）。

四份"最新版"抄件（简报 / 义务账本 / verdict_authority / hypothesis 自己的工具）
曾各自解析文件名，分叉不报错。现在都问这里。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterator

from core.naming import canonical_node_type

#: Analysis 职责所在的节点类型（v2.1 P3c 并进 hypothesis）。
ANALYSIS_NODES = ("analysis",)

HEAD_STEM = "research_state__research_state"


def metadata_of(record: dict[str, Any]) -> dict[str, Any]:
    meta = record.get("metadata") or {}
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except ValueError:
            return {}
    return meta if isinstance(meta, dict) else {}


def version_of(record: dict[str, Any]) -> int:
    try:
        v = int(metadata_of(record).get("version") or record.get("version") or 0)
    except (TypeError, ValueError):
        v = 0
    return v


def iter_located(worktree: Path | str | None) -> Iterator[tuple[dict[str, Any], Path]]:
    """全部 research_state 版本记录 + **head 文件路径**（不去重、不排序）。

    路径一起给出，是因为消费方普遍需要它（简报要给用户一个能直接打开的坐标）。
    历史版本也指向 head：那才是当前版本，"详读哪份"永远是它。
    """
    if not worktree:
        return
    from core.ledger import workspace_store

    root = Path(worktree)
    store = workspace_store(root)
    head = store.head(HEAD_STEM)
    if head is None or head.artifact_type != "research_state":
        return
    if (head.produced_by_node_type
            and canonical_node_type(head.produced_by_node_type) not in ANALYSIS_NODES):
        # 别的节点冒名写的 research_state 不是 Analysis 的状态。typed-only 写权
        # （artifact_capabilities）本就只给 hypothesis；这里再核一次
        # 是因为读的一侧不该信任写的一侧永远守规矩。
        return
    head_path = store.abs_path(head)
    for record in store.versions(HEAD_STEM):
        yield record, head_path


def iter_records(worktree: Path | str | None) -> Iterator[dict[str, Any]]:
    """全部 research_state 版本记录（不去重、不排序）。"""
    for record, _path in iter_located(worktree):
        yield record


def latest(worktree: Path | str | None) -> tuple[int, dict[str, Any]] | None:
    """(version, record) 或 None。"""
    found = latest_located(worktree)
    return (found[0], found[1]) if found else None


def latest_located(
    worktree: Path | str | None,
) -> tuple[int, dict[str, Any], Path] | None:
    """(version, record, head 路径) 或 None。"""
    best: tuple[int, dict[str, Any], Path] | None = None
    for record, path in iter_located(worktree):
        version = version_of(record)
        if version >= 1 and (best is None or version >= best[0]):
            best = (version, record, path)
    return best


def versions_for_state(state: Any) -> list[tuple[int, dict[str, Any]]]:
    """The same version selection for bound projects and run-local records."""
    worktree = getattr(state, "project_worktree", None)
    records = iter_records(worktree) if worktree else state.artifact_versions(HEAD_STEM)
    versions = {version_of(record): record for record in records if version_of(record) >= 1}
    return sorted(versions.items())


def read_for_state(state: Any, version: int | None = None) -> dict[str, Any]:
    """Read-only projection shared by the node tool and context renderer."""
    versions = versions_for_state(state)
    if not versions:
        return {"status":"success", "exists":False, "message":(
            "本项目还没有 research_state —— 这是第一轮 Analysis。"
            "走完 prereg 冻结后用 update_research_state 建立 v1。")}
    if version is None:
        picked_version, record = versions[-1]
    else:
        matches = [item for item in versions if item[0] == int(version)]
        if not matches:
            return {"status":"error", "error":f"没有 v{version}",
                    "available":[item[0] for item in versions]}
        picked_version, record = matches[0]
    meta = metadata_of(record)
    return {
        "status":"success", "exists":True, "version":picked_version,
        "available_versions":[item[0] for item in versions],
        "verdict":meta.get("verdict"), "hypotheses":meta.get("hypotheses") or [],
        "gaps":meta.get("gaps") or [], "next_steps":meta.get("next_steps") or [],
        "completed_experiments":meta.get("completed_experiments") or [],
        "content":str(record.get("content") or ""),
    }

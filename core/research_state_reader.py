"""research_state 的**唯一**读取实现（一个问题一个真相源）。

research_state 是 Analysis（hypothesis 节点）持有的版本化研究状态：单一身份
`research_state__research_state`，每次更新版本 +1，版本号在 metadata.version（与
账本 version 一致）。历史版本在账本里（`core/ledger`：每次 save 一行，正文按需
从 run 内快照或 git 历史取回）。

四份"最新版"抄件（简报 / 义务账本 / verdict_authority / hypothesis 自己的工具）
曾各自解析文件名，分叉不报错。现在都问这里。

## 旧布局

信封时代（2026-09-12 之前）的工作区（`<节点>/artifacts/*.json`、`.frozen.jsonl`）
本版本读不了 —— 但**不能装作没看见**：静默的话这些项目的 research_state 会变成
"不存在"，义务账本认为 Analysis 从没跑过、简报里整段研究状态消失，而没有任何
东西报错。所以 fail-closed。

升级由 `core.record_migration` 做，触发点在平台侧派发前
（`research_migration.upgrade_records_before_use`）：项目要被用了才升，升不上去
就是那一轮的失败。所以这个异常到达用户时该说的是"再发一条消息"，不是"重建项目"。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterator

#: Analysis 职责所在的节点类型（v2.1 P3c 并进 hypothesis，保留 analysis 兼容）。
ANALYSIS_NODES = ("hypothesis", "analysis")

HEAD_STEM = "research_state__research_state"


class UnmigratedWorkspaceError(RuntimeError):
    """工作区是**旧布局**（信封时代），本版本读不了它。"""


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
    from core.ledger import old_layout_fragments, workspace_store

    root = Path(worktree)
    fragments = old_layout_fragments(root)
    if fragments:
        raise UnmigratedWorkspaceError(_unmigrated_message(fragments))
    store = workspace_store(root)
    head = store.head(HEAD_STEM)
    if head is None or head.artifact_type != "research_state":
        return
    if head.produced_by_node_type and head.produced_by_node_type not in ANALYSIS_NODES:
        # 别的节点冒名写的 research_state 不是 Analysis 的状态。typed-only 写权
        # （artifact_capabilities）本就只给 hypothesis / analysis；这里再核一次
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


def legacy_fragments(worktree: Path | str | None) -> list[Path]:
    """旧布局的残留（信封 / 冻结登记表）。非空 = 本版本不读这个工作区。"""
    from core.ledger import old_layout_fragments

    return old_layout_fragments(worktree)


def _unmigrated_message(fragments: list[Path]) -> str:
    listed = "、".join(p.name for p in fragments[:6]) + (
        "…" if len(fragments) > 6 else "")
    return (
        f"这个项目的科研记录还是旧格式（{listed}）。正文和历史都在原工作区。\n"
        "平台会在这个项目下一轮开跑前自动升级 —— 发一条消息就会做完。\n"
        "也可由操作者运行：python -m app.manage migrate-records --worktree <项目目录>"
        "（加 --preview 只看不改）。升级会核验历史与冻结内容并保留 Git 备份；"
        "不要删除 artifacts 目录或重跑替代历史。"
    )

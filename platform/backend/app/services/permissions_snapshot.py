"""数据根的权限快照与账本（RFC_WORKER_START_IS_NOT_A_GATE §3.9，窄单 G）。

## 两样东西

- **账本** `logs/permissions.jsonl`：我们自己对用户目录做的每一次权限改动（Windows 沙箱的 `icacls`
  打标签），由 `core/isolation/_win32_exec.py` 在改的那一刻追加：路径、改前、改后、哪个 run、哪个
  spawn token、时间、返回码。这里只读。
- **快照** `logs/permissions-snapshot.json`：每次启动对关键目录记所有者 / 权限 / 标签的一张表；
  **应用版本号变了**就和上一份比，差异作为 `environment_warning` 报出，并附上这段时间账本里我们自己
  做过的改动 —— 于是任何权限悬案先查两样：账本答「是不是我们干的」，快照答「什么时候、变了什么」。

只扫顶层与一层子目录，不递归整棵树：启动期全量扫描是另一种门槛（RFC §8）。
第一次启动没有上一份就只记不比；快照读不到、写不进都不拦启动。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import stat
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

SNAPSHOT_NAME = "permissions-snapshot.json"
JOURNAL_NAME = "permissions.jsonl"
#: 数据根下值得盯的目录（相对数据根）。不存在的记 missing，不是跳过。
WATCHED = ("harness-state", "project-repositories", "project-worktrees", "payload", "logs")
#: 每个被盯目录往下看一层，最多这么多项（别让一个有几千个项目的数据根把启动拖慢）。
CHILD_CAP = 200

#: 最近一次启动算出的差异（给会话条幅读；进程内一份，重启就重算）。
_latest_change: dict[str, Any] | None = None


def latest_change() -> dict[str, Any] | None:
    return _latest_change


def describe_entry(path: Path) -> dict[str, Any]:
    """一条路径的权限事实：所有者 / mode（POSIX）或 ACL 哈希与标签（Windows）。读不出来写原因。"""
    entry: dict[str, Any] = {"owner": None, "mode": None, "acl_sha256": None, "label": None, "error": None}
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        entry["error"] = "missing"
        return entry
    except OSError as exc:
        entry["error"] = f"{type(exc).__name__}: {exc}"
        return entry
    if sys.platform == "win32":  # pragma: no cover - 只在 Windows 上跑
        text = _icacls_text(path)
        if text is not None:
            entry["acl_sha256"] = acl_fingerprint(text, path)
            entry["label"] = _mandatory_label(text)
        entry["owner"] = _windows_owner(path)
    else:
        entry["mode"] = oct(stat.S_IMODE(st.st_mode))
        entry["owner"] = str(st.st_uid)
    return entry


#: 快照格式。2：ACL 指纹只哈希 ACE 本身（1 版哈希的是 icacls 整段输出 —— 第一行打头就是路径本身，
#: 数据根一挪、每一条都「变了」）。格式不同的两份快照不比。
SCHEMA_VERSION = 2


def acl_fingerprint(icacls_text: str, path: Path | str) -> str:
    """一条路径 ACL 的指纹：只取 ACE 行（去掉打头的路径与末尾「已成功处理 N 个文件」那段）。

    icacls 的输出是 ``<路径> <ACE1>`` 一行、其余 ACE 各缩进一行、空行、汇总一行（本地化的）。
    路径不进指纹：同一组 ACE 换个地方仍是同一份权限。"""
    lines = icacls_text.splitlines()
    aces: list[str] = []
    prefix = str(path)
    for index, line in enumerate(lines):
        if not line.strip():
            break
        if index == 0 and line.startswith(prefix):
            line = line[len(prefix):]
        aces.append(line.strip())
    return hashlib.sha256("\n".join(aces).encode("utf-8", "replace")).hexdigest()[:16]


def not_comparable(previous: dict[str, Any], current: dict[str, Any]) -> str | None:
    """两份快照能不能比；不能就说为什么（不能比 ≠ 零差异）。"""
    if previous.get("schema_version") != current.get("schema_version"):
        return f"快照格式不同（{previous.get('schema_version')} → {current.get('schema_version')}）"
    if str(previous.get("data_root") or "") != str(current.get("data_root") or ""):
        return "数据根换了位置：ACL 继承自新的上级目录，逐条比较没有意义"
    return None


def _icacls_text(path: Path) -> str | None:  # pragma: no cover
    try:
        done = subprocess.run(["icacls", str(path)], capture_output=True, text=True, timeout=15,
                              encoding="utf-8", errors="replace")
    except (OSError, subprocess.SubprocessError):
        return None
    return (done.stdout or "").strip() or None


def _mandatory_label(icacls_text: str) -> str | None:  # pragma: no cover
    for token in ("Mandatory Label\\Low", "Mandatory Label\\Medium", "Mandatory Label\\High"):
        if token in icacls_text:
            return token.rsplit("\\", 1)[-1]
    return None


def _windows_owner(path: Path) -> str | None:  # pragma: no cover
    from app.services.environment_facts import _windows_owner as owner

    return owner(path)


def take_snapshot(data_root: Path, *, app_version: str | None) -> dict[str, Any]:
    entries: dict[str, dict[str, Any]] = {".": describe_entry(data_root)}
    for name in WATCHED:
        top = data_root / name
        entries[name] = describe_entry(top)
        if entries[name]["error"] is not None:
            continue
        try:
            children = sorted(p for p in top.iterdir())[:CHILD_CAP]
        except OSError as exc:
            entries[name]["children_error"] = f"{type(exc).__name__}: {exc}"
            continue
        for child in children:
            entries[f"{name}/{child.name}"] = describe_entry(child)
    return {
        "schema_version": SCHEMA_VERSION,
        "taken_at": datetime.now(UTC).isoformat(),
        "app_version": app_version,
        "platform": sys.platform,
        "data_root": str(data_root),
        "entries": entries,
    }


def compare(previous: dict[str, Any], current: dict[str, Any]) -> list[dict[str, Any]]:
    """两份快照的差异：哪条路径的哪个字段从什么变成了什么。新出现 / 消失的路径不算差异（那是数据，不是权限）。"""
    diffs: list[dict[str, Any]] = []
    before = previous.get("entries") or {}
    after = current.get("entries") or {}
    for key in sorted(set(before) & set(after)):
        old, new = before[key], after[key]
        for field in ("owner", "mode", "acl_sha256", "label", "error"):
            if old.get(field) != new.get(field):
                diffs.append({"path": key, "field": field, "before": old.get(field), "after": new.get(field)})
    return diffs


def read_journal_since(journal: Path, since_iso: str | None) -> list[dict[str, Any]]:
    """账本里这段时间我们自己做过的改动。读不到 / 没有账本 → []。"""
    if not journal.is_file():
        return []
    out: list[dict[str, Any]] = []
    try:
        for line in journal.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict) and (since_iso is None or str(record.get("at") or "") >= since_iso):
                out.append(record)
    except OSError:
        return []
    return out[-500:]


def record_startup(data_root: Path, *, app_version: str | None) -> dict[str, Any] | None:
    """启动时记一份；版本号变了就比。返回差异事件（没有差异 / 第一次 / 同版本 → None）。"""
    global _latest_change
    logs = data_root / "logs"
    snapshot_path = logs / SNAPSHOT_NAME
    previous: dict[str, Any] | None = None
    try:
        previous = json.loads(snapshot_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        previous = None
    current = take_snapshot(data_root, app_version=app_version)
    change: dict[str, Any] | None = None
    if isinstance(previous, dict) and previous.get("app_version") != app_version:
        reason = not_comparable(previous, current)
        if reason:
            logger.info("permissions snapshot not compared across %s → %s: %s",
                        previous.get("app_version"), app_version, reason)
        diffs = [] if reason else compare(previous, current)
        if diffs:
            ours = read_journal_since(logs / JOURNAL_NAME, str(previous.get("taken_at") or "") or None)
            touched_by_us = {str(r.get("path")) for r in ours}
            change = {
                "type": "environment_warning",
                "code": "data_root_permissions_changed",
                "capability": "data_root",
                "severity": "degraded",
                "path": str(data_root),
                "error": None,
                "consequences": [{
                    "zh": f"从 {previous.get('app_version') or '?'} 升到 {app_version or '?'} 之间，"
                          f"数据根下 {len(diffs)} 处权限变了",
                    "en": f"Between {previous.get('app_version') or '?'} and {app_version or '?'}, "
                          f"{len(diffs)} permissions under the data root changed",
                }],
                "diffs": diffs[:200],
                "previous_taken_at": previous.get("taken_at"),
                "previous_version": previous.get("app_version"),
                "our_changes_in_between": ours[-50:],
                "changed_by_us": sorted(d["path"] for d in diffs if str(data_root / d["path"]) in touched_by_us
                                        or d["path"] in touched_by_us),
            }
            logger.warning(
                "data root permissions changed across %s → %s: %d entries (ours in between: %d)",
                previous.get("app_version"), app_version, len(diffs), len(ours),
            )
    _latest_change = change
    try:
        logs.mkdir(parents=True, exist_ok=True)
        tmp = snapshot_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(current, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(snapshot_path)
    except OSError as exc:
        logger.warning("cannot write %s: %s", snapshot_path, exc)
    return change

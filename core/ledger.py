"""研究记录 = 原生文件 + 一份账本（RFC 2026-09-12 §6，取代 JSON 信封与 .versions/）。

## 为什么不再是信封

2026-09-12 之前每份产物是一个 JSON：正文塞在 `"content"` 字段里，外面包着类型、
名字、版本、出处。后果三条，都是同一个根：

1. 文件对人不可读、对工具不可用 —— 右栏要专门拆信封，git diff 一份论文是一行
   转义过的 JSON，项目打包给同行看不懂。
2. 冻结在**原地改写**文件（写 `metadata.frozen`），于是账本钉的是改写后的文件
   哈希而 `content_hash` 只描述正文，两个哈希天然分叉。
3. `.versions/` 快照与 git 历史存同一内容，两套版本账。

现在：**正文就是文件**（`plan/pre_registration__Q1.md`、`paper/manuscript__X.tex`），
框架事实进 `.research/ledger/records.jsonl`。文件哈希 = 正文哈希，冻结不再碰文件，
版本历史归 git（run 内的撤销快照落 run 目录，随 run 生灭）。

## 账本行

append-only、行间哈希链（`prev_row_sha256`）。三种行：

    save    {id, type, name, path, version, sha256, prev_sha256?, created_at,
             provenance, produced_by_node_type, produced_by_run_id, metadata,
             amendment?, by_node, by_run}
    freeze  {id, version, path, sha256, frozen_at, by_node, by_run, metadata_patch}
    retire  {id, version, path, reason, at, by_node, by_run}

`freeze` 行的 `metadata_patch` 就是冻结时曾经写进文件的那几个键（frozen /
frozen_at / freeze_reason / run_role 声明…）；读记录时折进有效 metadata。
于是消费方看到的 record 形状与从前**完全一样**：
`{type, name, content, metadata, created_at, provenance, produced_by_*, version,
content_hash, prev_content_hash?, amendment?}`。

## 判读

「当前版本」= 最后一条 save 行 + 盘上那个文件（head）。「哪版被钉死」=
freeze 行钉 `path@sha256`，其后的带 amendment 的 save 解除（冻结的字节在 git
历史里，checkpoint 改不了历史）。这是**唯一**的账本判读实现；平台的 commit 闸
有一份纯 stdlib 镜像（platform/backend/app/services/frozen_register.py），契约测试
把两边钉在同一份 fixture 上。
"""
from __future__ import annotations

import difflib
import hashlib
import json
import logging
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

log = logging.getLogger("ledger")

#: 工作区账本（相对 worktree 根）。目录带点：文件树把它折进「平台记账」。
LEDGER_RELATIVE = ".research/ledger/records.jsonl"

#: 只有 freeze 行能写的 metadata 键（读时由账本折进 record）。save 行一律剥掉。
FREEZE_OWNED_METADATA = ("frozen", "frozen_at", "frozen_version", "freeze_reason")

#: 正文的落盘格式：类型 → 扩展名。表里没有的按正文形状判（见 `extension_for`）。
#: 只登记**形状不能从正文看出来**的类型；markdown 是默认值不用登记。
_FORMAT_BY_TYPE: dict[str, str] = {
    "manuscript": ".tex",
    "build_env": ".sh",
}

_EXT_RE = re.compile(r"\.[A-Za-z0-9]{1,8}$")


def sha256_text(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def record_version(record: dict) -> int:
    """record 的版本号；缺失/非法按 1 算。"""
    try:
        v = int(record.get("version") or 0)
    except (TypeError, ValueError):
        v = 0
    return v if v >= 1 else 1


def extension_for(artifact_type: str, content: str) -> str:
    """这份正文该以什么扩展名落盘。

    类型表优先（manuscript 是 LaTeX，正文未必以 `\\documentclass` 起手）；其余按
    正文形状：能整段解析成 JSON 的是 `.json`，`#!` 起手是 shell，`%` / `\\` 起手
    是 LaTeX，其余 markdown。判一次、记进账本行的 path 里 —— 读者永远不再猜。
    """
    if artifact_type in _FORMAT_BY_TYPE:
        return _FORMAT_BY_TYPE[artifact_type]
    head = (content or "").lstrip()
    if head.startswith(("{", "[")):
        try:
            json.loads(content)
            return ".json"
        except ValueError:
            pass
    if head.startswith("#!"):
        return ".sh"
    if head.startswith(("%", "\\")):
        return ".tex"
    return ".md"


def compute_amendment_diff(old: dict, new_content: str, new_metadata: dict) -> dict:
    """冻结版 → 修订稿的机械差异：改了哪些 metadata 键 + 正文 unified diff 摘录。"""
    old_meta = old.get("metadata") if isinstance(old.get("metadata"), dict) else {}
    changed_keys = sorted(
        k for k in (set(old_meta) | set(new_metadata or {}))
        if old_meta.get(k) != (new_metadata or {}).get(k)
        and k not in ("frozen", "frozen_at", "freeze_reason")
    )
    old_content = str(old.get("content") or "")
    excerpt = ""
    if old_content != (new_content or ""):
        diff_lines = difflib.unified_diff(
            old_content.splitlines(), (new_content or "").splitlines(),
            fromfile=f"v{record_version(old)}", tofile="revision", lineterm="", n=2,
        )
        excerpt = "\n".join(diff_lines)[:4000]
    return {
        "changed_metadata_keys": changed_keys,
        "content_changed": old_content != (new_content or ""),
        "content_diff_excerpt": excerpt,
        "old_content_hash": sha256_text(old_content),
        "new_content_hash": sha256_text(new_content or ""),
    }


@dataclass
class Head:
    """一个身份折账后的当前状态。`metadata` 已折进冻结补丁。"""

    artifact_id: str
    artifact_type: str
    name: str
    path: str                     # 相对 store.root
    version: int
    sha256: str
    created_at: str
    provenance: dict
    produced_by_node_type: str
    produced_by_run_id: str
    metadata: dict
    prev_sha256: str | None = None
    amendment: dict | None = None
    frozen: bool = False
    frozen_at: str = ""
    frozen_version: int = 0
    frozen_sha256: str = ""
    retired: bool = False
    saves: list[dict] = field(default_factory=list)    # 全部 save 行，版本升序


class RecordStore:
    """一份账本 + 它管辖的正文文件。

    两种实例：工作区（`root=worktree`，账本 `.research/ledger/records.jsonl`，
    正文落各节点目录）和 run 本地（`root=<run>/artifacts`，账本 `<run>/records.jsonl`，
    随 run 生灭）。判读、写法完全相同。
    """

    def __init__(self, root: Path | str, ledger_path: Path | str,
                 snapshot_dir: Path | str | None = None) -> None:
        self.root = Path(root)
        self.ledger_path = Path(ledger_path)
        #: run 内覆盖前的快照落这里（撤销用）；None = 不留快照。
        self.snapshot_dir = Path(snapshot_dir) if snapshot_dir else None
        self._git_cache: dict[tuple[str, str], str | None] = {}

    # ── 行 ──────────────────────────────────────────────────────────────

    def rows(self) -> list[dict]:
        """全部账本行（坏行跳过 —— 判读是观察，不该被一行坏数据打断）。

        账本**不存在** = 没有记录；账本**读不了**（权限、I/O）不是"没有记录"，
        静默成空会让"没有回执"看起来像"没有提交过"，run 就完成得毫无根据 ——
        这种情况抛出去，让读者自己决定停不停。
        """
        out: list[dict] = []
        try:
            lines = self.ledger_path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return out
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                out.append(row)
        return out

    def strict_rows(self) -> list[dict]:
        """全部账本行，任何一行坏就抛 —— 给"宁可停也不猜"的读者（提交凭据）。"""
        try:
            lines = self.ledger_path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise ValueError(f"record ledger is unreadable: {type(exc).__name__}") from exc
        out: list[dict] = []
        for number, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except ValueError as exc:
                raise ValueError(f"record ledger line {number} is invalid") from exc
            if not isinstance(row, dict):
                raise ValueError(f"record ledger line {number} is invalid")
            out.append(row)
        return out

    def _append(self, row: dict) -> dict:
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        prev = None
        try:
            lines = [ln for ln in self.ledger_path.read_text(encoding="utf-8").splitlines()
                     if ln.strip()]
            if lines:
                prev = hashlib.sha256(lines[-1].encode("utf-8")).hexdigest()
        except OSError:
            pass
        if prev:
            row = {**row, "prev_row_sha256": prev}
        with self.ledger_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        return row

    # ── 折账 ─────────────────────────────────────────────────────────────

    def heads(self, *, include_retired: bool = False) -> dict[str, Head]:
        """`{artifact_id: Head}`，按账本顺序折出来。"""
        heads: dict[str, Head] = {}
        for row in self.rows():
            event = str(row.get("event") or "")
            artifact_id = str(row.get("id") or "").strip()
            if not artifact_id:
                continue
            if event == "save":
                head = Head(
                    artifact_id=artifact_id,
                    artifact_type=str(row.get("type") or ""),
                    name=str(row.get("name") or artifact_id),
                    path=str(row.get("path") or ""),
                    version=int(row.get("version") or 1),
                    sha256=str(row.get("sha256") or ""),
                    created_at=str(row.get("created_at") or ""),
                    provenance=dict(row.get("provenance") or {}),
                    produced_by_node_type=str(row.get("produced_by_node_type") or ""),
                    produced_by_run_id=str(row.get("produced_by_run_id") or ""),
                    metadata=dict(row.get("metadata") or {}),
                    prev_sha256=row.get("prev_sha256") or None,
                    amendment=dict(row["amendment"]) if isinstance(row.get("amendment"), dict) else None,
                )
                previous = heads.get(artifact_id)
                head.saves = [*(previous.saves if previous else []), row]
                if previous and previous.frozen and head.amendment is None:
                    # 不该发生：冻结后的覆盖必须带 amendment（State 在写入前拒绝）。
                    # 账本里若真出现，如实保留冻结状态在旧版上，head 是未冻结的新版。
                    pass
                if previous and previous.frozen:
                    # 修订：冻结的那一版仍是"最近冻结版"，head 是草稿。
                    head.frozen_version = previous.frozen_version
                    head.frozen_sha256 = previous.frozen_sha256
                    head.frozen_at = previous.frozen_at
                heads[artifact_id] = head
            elif event == "freeze":
                head = heads.get(artifact_id)
                if head is None:
                    continue
                patch = row.get("metadata_patch")
                if isinstance(patch, dict):
                    head.metadata = {**head.metadata, **patch}
                head.frozen = True
                head.frozen_at = str(row.get("frozen_at") or "")
                head.frozen_version = int(row.get("version") or head.version)
                head.frozen_sha256 = str(row.get("sha256") or head.sha256)
            elif event == "retire":
                head = heads.get(artifact_id)
                if head is not None:
                    head.retired = True
        if include_retired:
            return heads
        return {k: v for k, v in heads.items() if not v.retired}

    def head(self, artifact_id: str) -> Head | None:
        return self.heads().get(artifact_id)

    def pinned(self) -> dict[str, str]:
        """{被钉死的相对路径: sha256} —— 冻结且尚未修订的 head。

        checkpoint 闸据此拒绝这些路径的改动入库。修订（带 amendment 的 save）
        解除钉死：冻结的字节在 git 历史里，历史改不了。
        """
        out: dict[str, str] = {}
        for head in self.heads().values():
            if head.frozen and head.frozen_version == head.version and head.path and head.sha256:
                out[head.path] = head.sha256
        return out

    # ── 读 ──────────────────────────────────────────────────────────────

    def abs_path(self, head: Head) -> Path:
        return self.root / head.path

    def _read_content(self, head: Head) -> str | None:
        """正文。文件必须真在本账本管辖的目录树内：指向别处的 symlink 一律不读
        （账本说它在这儿，不等于它就在这儿）。"""
        path = self.abs_path(head)
        try:
            resolved = path.resolve(strict=True)
            if resolved != self.root.resolve() and self.root.resolve() not in resolved.parents:
                return None
            return resolved.read_bytes().decode("utf-8")
        except (OSError, UnicodeDecodeError):
            return None

    def assemble(self, head: Head, content: str | None) -> dict:
        """把 head + 正文拼成消费方一直在读的那个 record 形状。"""
        record: dict[str, Any] = {
            "type": head.artifact_type,
            "name": head.name,
            "content": content if content is not None else "",
            "metadata": dict(head.metadata),
            "created_at": head.created_at,
            "provenance": dict(head.provenance),
            "produced_by_node_type": head.produced_by_node_type,
            "produced_by_run_id": head.produced_by_run_id,
            "version": head.version,
            "content_hash": head.sha256,
        }
        if head.prev_sha256:
            record["prev_content_hash"] = head.prev_sha256
        if head.amendment is not None:
            record["amendment"] = dict(head.amendment)
        return record

    def record(self, artifact_id: str) -> dict | None:
        """head 的 record；正文读不到（文件没了 / 解析到目录之外 / 不是 UTF-8）
        就是 None —— "账本里有、盘上没有"不是一份内容为空的记录，是读不到的记录。"""
        head = self.head(artifact_id)
        if head is None:
            return None
        content = self._read_content(head)
        if content is None:
            return None
        return self.assemble(head, content)

    def versions(self, artifact_id: str) -> list[dict]:
        """一个身份的全部版本，版本升序，末位 = head。

        旧版正文：先找 run 内快照（`snapshot_dir/<id>@vN.<ext>`），再问 git
        （按账本里那一版的 sha256 在这条路径的历史里找同哈希的 blob）。都找不到
        时 `content` 为空串 —— 但 `content_hash` 永远在，消费方按哈希核对即可。
        """
        heads = self.heads(include_retired=True)
        head = heads.get(artifact_id)
        if head is None:
            return []
        out: list[dict] = []
        for row in head.saves:
            version = int(row.get("version") or 1)
            frame = Head(
                artifact_id=artifact_id,
                artifact_type=str(row.get("type") or ""),
                name=str(row.get("name") or artifact_id),
                path=str(row.get("path") or head.path),
                version=version,
                sha256=str(row.get("sha256") or ""),
                created_at=str(row.get("created_at") or ""),
                provenance=dict(row.get("provenance") or {}),
                produced_by_node_type=str(row.get("produced_by_node_type") or ""),
                produced_by_run_id=str(row.get("produced_by_run_id") or ""),
                metadata=dict(row.get("metadata") or {}),
                prev_sha256=row.get("prev_sha256") or None,
                amendment=dict(row["amendment"]) if isinstance(row.get("amendment"), dict) else None,
            )
            # 每一版各自折进冻结它那一行的补丁：v1 冻过、v2 修订、v3 再冻，历史里
            # v1 仍是冻结过的那一版。
            frame.metadata = {**frame.metadata, **_freeze_patch(head.saves, self.rows(), artifact_id, version)}
            if version == head.version and not head.retired:
                content = self._read_content(head)
            else:
                content = self._historical_content(frame)
            out.append(self.assemble(frame, content))
        return out

    def latest_frozen(self, artifact_id: str) -> dict | None:
        head = self.head(artifact_id)
        if head is None or not head.frozen_version:
            return None
        for record in reversed(self.versions(artifact_id)):
            if record_version(record) == head.frozen_version:
                return record
        return None

    def _historical_content(self, frame: Head) -> str | None:
        if self.snapshot_dir is not None:
            stem = Path(frame.path).stem
            ext = _EXT_RE.search(frame.path)
            candidate = self.snapshot_dir / f"{stem}@v{frame.version}{ext.group(0) if ext else ''}"
            try:
                text = candidate.read_bytes().decode("utf-8")
                if sha256_text(text) == frame.sha256:
                    return text
            except (OSError, UnicodeDecodeError):
                pass
        return self._git_content(frame.path, frame.sha256)

    def _git_content(self, relative: str, sha256: str) -> str | None:
        """在这条路径的 git 历史里找 sha256 相同的那一版正文。"""
        key = (relative, sha256)
        if key in self._git_cache:
            return self._git_cache[key]
        found: str | None = None
        try:
            commits = subprocess.run(
                ["git", "-C", str(self.root), "log", "--format=%H", "--all", "--", relative],
                check=True, capture_output=True, text=True,
            ).stdout.split()
            for commit in commits:
                shown = subprocess.run(
                    ["git", "-C", str(self.root), "show", f"{commit}:{relative}"],
                    capture_output=True,
                )
                if shown.returncode != 0:
                    continue
                if hashlib.sha256(shown.stdout).hexdigest() == sha256:
                    found = shown.stdout.decode("utf-8", errors="replace")
                    break
        except (OSError, subprocess.SubprocessError):
            found = None
        self._git_cache[key] = found
        return found

    # ── 写 ──────────────────────────────────────────────────────────────

    def save(self, *, artifact_id: str, artifact_type: str, name: str, content: str,
             metadata: dict, directory: Path, created_at: str, provenance: dict,
             produced_by_node_type: str, produced_by_run_id: str,
             by_node: str, by_run: str, amendment: dict | None = None) -> dict:
        """落一版正文 + 一条 save 行。返回写入的 record（消费方形状）。

        `directory` 是首次落盘的目录；同一身份之后的版本沿用账本里的 path
        （路径即身份，创建即固定）。
        """
        # 冻结的事实只有一个出处：账本上的 freeze 行（读时折进 metadata）。save 行
        # 里不许自带 frozen/frozen_at —— 否则 `metadata.frozen` 就有了第二个真相源，
        # 读者按它判"已冻结"而账本从没钉死过它（模型面的写入口另有 reject_freeze_forgery
        # 当场驳回；这里管的是进程内的调用方和修订时原样抄来的旧 metadata）。
        metadata = {k: v for k, v in dict(metadata or {}).items() if k not in FREEZE_OWNED_METADATA}
        existing = self.head(artifact_id)
        if existing is not None and existing.path:
            relative = existing.path
            version = existing.version + 1
            prev_sha = existing.sha256
        else:
            ext = extension_for(artifact_type, content)
            target = Path(directory) / f"{artifact_id}{ext}"
            relative = self._relative(target)
            version = 1
            prev_sha = None
        target = self.root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if existing is not None and self.snapshot_dir is not None:
            self._snapshot(existing)
        payload = (content or "").encode("utf-8")
        target.write_bytes(payload)
        row = {
            "event": "save",
            "id": artifact_id,
            "type": artifact_type,
            "name": name,
            "path": relative,
            "version": version,
            "sha256": hashlib.sha256(payload).hexdigest(),
            "created_at": created_at,
            "provenance": provenance,
            "produced_by_node_type": produced_by_node_type,
            "produced_by_run_id": produced_by_run_id,
            "metadata": metadata,
            "by_node": by_node,
            "by_run": by_run,
        }
        if prev_sha:
            row["prev_sha256"] = prev_sha
        if amendment is not None:
            row["amendment"] = amendment
        self._append(row)
        head = self.head(artifact_id)
        assert head is not None
        return self.assemble(head, content)

    def freeze(self, artifact_id: str, *, metadata_patch: dict, by_node: str, by_run: str,
               frozen_at: str | None = None) -> dict:
        """冻结 head：一条 freeze 行，文件一个字节不动。返回冻结后的 record。"""
        head = self.head(artifact_id)
        if head is None:
            raise KeyError(artifact_id)
        stamp = frozen_at or datetime.now(timezone.utc).isoformat()
        patch = {**metadata_patch, "frozen": True, "frozen_at": stamp}
        self._append({
            "event": "freeze",
            "id": artifact_id,
            "version": head.version,
            "path": head.path,
            "sha256": head.sha256,
            "frozen_at": stamp,
            "by_node": by_node,
            "by_run": by_run,
            "metadata_patch": patch,
        })
        return self.record(artifact_id) or {}

    def retire(self, artifact_id: str, *, reason: str, by_node: str, by_run: str) -> bool:
        """撤下一个未冻结的身份：文件删掉，账本留一行。冻结的拒绝。"""
        head = self.head(artifact_id)
        if head is None:
            return False
        if head.frozen and head.frozen_version == head.version:
            raise PermissionError(f"{artifact_id} 已冻结，不能撤下")
        try:
            self.abs_path(head).unlink()
        except OSError:
            pass
        self._append({
            "event": "retire",
            "id": artifact_id,
            "version": head.version,
            "path": head.path,
            "reason": reason,
            "at": datetime.now(timezone.utc).isoformat(),
            "by_node": by_node,
            "by_run": by_run,
        })
        return True

    def _snapshot(self, head: Head) -> Path | None:
        """覆盖前把当前版本抄进 snapshot_dir —— run 内撤销用，随 run 生灭。"""
        if self.snapshot_dir is None:
            return None
        content = self._read_content(head)
        if content is None:
            return None
        stem = Path(head.path).stem
        ext = _EXT_RE.search(head.path)
        target = self.snapshot_dir / f"{stem}@v{head.version}{ext.group(0) if ext else ''}"
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            target.write_bytes(content.encode("utf-8"))
        return target

    def snapshot_path(self, head: Head) -> Path | None:
        if self.snapshot_dir is None:
            return None
        stem = Path(head.path).stem
        ext = _EXT_RE.search(head.path)
        return self.snapshot_dir / f"{stem}@v{head.version}{ext.group(0) if ext else ''}"

    def _relative(self, target: Path) -> str:
        try:
            return target.resolve().relative_to(self.root.resolve()).as_posix()
        except ValueError as exc:
            raise ValueError(f"record must live under the store root {self.root}: {target}") from exc


def _freeze_patch(saves: list[dict], rows: list[dict], artifact_id: str, version: int) -> dict:
    """账本里冻结第 `version` 版时写下的 metadata 补丁（给 versions() 折进历史版）。"""
    for row in rows:
        if (row.get("event") == "freeze" and str(row.get("id")) == artifact_id
                and int(row.get("version") or 0) == version):
            patch = row.get("metadata_patch")
            return dict(patch) if isinstance(patch, dict) else {"frozen": True}
    return {}


# ── 工作区级便捷入口 ───────────────────────────────────────────────────────

def workspace_store(worktree: Path | str, snapshot_dir: Path | str | None = None) -> RecordStore:
    root = Path(worktree)
    return RecordStore(root, root / LEDGER_RELATIVE, snapshot_dir)


def iter_heads(worktree: Path | str | None, *, artifact_type: str | None = None) -> Iterator[Head]:
    """工作区里全部记录的 head（可按类型过滤），按 created_at 升序。"""
    if not worktree:
        return
    store = workspace_store(worktree)
    heads = [h for h in store.heads().values() if not artifact_type or h.artifact_type == artifact_type]
    heads.sort(key=lambda h: (h.created_at, h.artifact_id))
    yield from heads


def old_layout_fragments(worktree: Path | str | None) -> list[Path]:
    """信封时代的残留：`*/artifacts/*.json`、`.frozen.jsonl`、`.versions/`。

    升级由 core.record_migration 一次性转换并校验。迁移前拒绝把旧记录
    当成不存在；普通读取路径不再携带第二套信封解析器。
    """
    if not worktree:
        return []
    root = Path(worktree)
    out: list[Path] = []
    try:
        for node_dir in sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")):
            artifacts = node_dir / "artifacts"
            if artifacts.is_dir():
                out.extend(sorted(artifacts.glob("*.json")))
                if (artifacts / ".frozen.jsonl").exists():
                    out.append(artifacts / ".frozen.jsonl")
    except OSError:
        return out
    return out[:12]


def write_record(worktree: Path | str, *, artifact_type: str, name: str, content: str,
                 directory: str, metadata: dict | None = None,
                 produced_by_node_type: str = "", produced_by_run_id: str = "",
                 created_at: str | None = None, frozen: bool = False,
                 provenance: dict | None = None) -> dict:
    """不经 State 直接落一份记录 —— 给夹具、导入脚本、平台侧工具用。

    走的是同一个 RecordStore：正文落 `<directory>/<type>__<slug>.<ext>`，账本一行；
    `frozen=True` 再追一行 freeze。返回 record（消费方形状）。
    """
    from core.artifact_provenance import produced

    slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", (name or "").strip()).strip("_") or "unnamed"
    artifact_id = f"{artifact_type}__{slug}"
    root = Path(worktree)
    store = workspace_store(root)
    node = produced_by_node_type or "framework"
    run = produced_by_run_id or "fixture"
    record = store.save(
        artifact_id=artifact_id, artifact_type=artifact_type, name=name, content=content,
        metadata=dict(metadata or {}), directory=root / directory,
        created_at=created_at or datetime.now(timezone.utc).isoformat(),
        provenance=provenance or produced(node, run),
        produced_by_node_type=node, produced_by_run_id=run, by_node=node, by_run=run,
    )
    if frozen:
        record = store.freeze(artifact_id, metadata_patch={}, by_node=node, by_run=run)
    return {"id": artifact_id, **record}

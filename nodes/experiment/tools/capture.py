"""action-owned capture 命名空间与 output observation（v3 governing document §4 I10、§5）。

三样东西分开：
* 输入 / 工作根：模型干活的目录（build 根、run 根），可以在显式 predecessor
  lineage 下被顺序 action 串行独占复用；
* capture 命名空间：每个 action 自己的目录 ``<run>/capture/<action_id>/``，
  作业输出根按 intent nonce 在提交前分配；
* observation：action 前后对候选路径做**身份**快照并求差。

身份 = 类型、大小、inode、mtime_ns，小文件再加 sha256。同 inode、同大小的原地
改写靠 mtime_ns 与 hash 分辨；大文件没有 hash 时，三者都相同才算"未变"，并把
identity_basis 写清楚——不假装看见了没看见的东西。

这里没有门：快照失败、超预算都如实记入结果，不拒绝任何 action。
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import time
from collections import deque
from pathlib import Path
from typing import Any, Iterable

try:
    from . import persistence as _persistence
except ImportError:  # pragma: no cover - standalone node bootstrap
    from tools import persistence as _persistence  # type: ignore

_CAPTURE_DIRNAME = "capture"
SMALL_FILE_BYTES = 8 * 1024 * 1024
DEFAULT_MAX_FILES = 20000
DEFAULT_MAX_SECONDS = 5.0
OBSERVATION_SUMMARY_LIMIT = 200


def capture_root(state: Any) -> Path:
    return Path(state.root) / _CAPTURE_DIRNAME


def allocate_record_dir(state: Any, action_id: str) -> Path:
    """为一个 action 分配一个记录目录（存它自己的快照与日志副本）。幂等。

    **它不是这个 action 的输出根**：本地 payload 仍在 ``effective_cwd`` 里跑，
    受管作业的输出根由提交时按 nonce 分配的 job_dir 承担。早先的名字
    ``allocate_namespace`` 暗示它隔离了输出，那是假的（Codex 21 号 P0-B），
    所以按实情改名。真正的并发隔离靠作业各自的输出根。
    """
    safe = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in str(action_id))[:120]
    path = capture_root(state) / safe
    path.mkdir(parents=True, exist_ok=True)
    return path


def persist_observation(state: Any, action_id: str, observation: dict[str, Any]) -> dict[str, Any]:
    """Keep already captured B facts; only their ledger/UI summary is bounded.

    The file is an immutable part of the observation, not a new state owner.
    It is flushed under persistence.py's host platform contract before its
    relative, digest-bound reference enters B. No
    filesystem snapshot or output identity is collected at read/finish time.
    Small observations keep the existing inline representation.
    """
    limit = OBSERVATION_SUMMARY_LIMIT
    sequences = ("created", "modified", "deleted", "unknown")
    mappings = ("identities", "identity_basis", "overlapping_actions")
    coverage = observation.get("coverage") or {}
    managed_coverage = ("root_coverage", "baseline_root_coverage")
    coverage_lists = [*coverage.values(),
                      *(observation.get(key) or [] for key in managed_coverage)]
    coverage_rows = sum(len(rows) for rows in coverage_lists)
    coverage_names = sum(len(row.get("entry_names") or [])
                         for rows in coverage_lists for row in rows)
    if not (any(len(observation.get(k) or []) > limit for k in (*sequences, *mappings))
            or len(observation.get("created") or []) + len(observation.get("modified") or []) > limit
            or coverage_rows > limit or coverage_names > limit):
        return observation
    payload = json.dumps({"action_id": action_id, "observation": observation},
                         sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()
    directory = allocate_record_dir(state, action_id)
    path = directory / f"observation-{digest}.json"
    try:
        with path.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError:
        # Reuse only identical durable bytes, never overwrite a prior fact.
        existing = read_observation(directory, {"file": path.name, "sha256": digest,
                                               "size_bytes": len(payload)}, action_id)
        if existing != observation:
            raise OSError("capture observation identity conflict")
        _persistence.fsync_file(path, nofollow=True)
    for parent in (directory, directory.parent, directory.parent.parent):
        _persistence.fsync_directory(parent)
    summary = dict(observation)
    for key in sequences:
        summary[key] = list(observation.get(key) or [])[:limit]
    for key in mappings:
        summary[key] = dict(list((observation.get(key) or {}).items())[:limit])
    if coverage_rows > limit or coverage_names > limit:
        rows_remaining = limit
        names_remaining = limit

        def summarized_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
            nonlocal rows_remaining, names_remaining
            displayed = []
            for row in rows:
                if not rows_remaining:
                    break
                rows_remaining -= 1
                shown = dict(row)
                if "entry_names" in row:
                    names = row["entry_names"]
                    # One display budget across ALL rows and shapes, not
                    # 200 rows times 200 names for each before/after list.
                    shown["entry_names"] = (names[:names_remaining]
                                            if isinstance(names, list) else None)
                    names_remaining -= len(shown["entry_names"] or [])
                if row.get("status") == "direct_complete" or row.get("direct_complete"):
                    shown.update(status="direct_partial", direct_complete=False)
                displayed.append(shown)
            return displayed
        if "coverage" in observation:
            summary["coverage"] = {side: summarized_rows(rows)
                                   for side, rows in coverage.items()}
        for key in managed_coverage:
            if key in observation:
                summary[key] = summarized_rows(observation[key])
        summary["coverage_summary_truncated"] = True
    summary.update(summary_truncated=True,
                   observation_counts={key: len(observation.get(key) or []) for key in (*sequences, *mappings)},
                   observation_ref={"file": path.name, "sha256": digest, "size_bytes": len(payload)})
    return summary


def read_observation(directory: Path, reference: dict[str, Any], action_id: str) -> dict[str, Any]:
    """Read only a digest-named child of the existing action capture directory."""
    if not isinstance(reference, dict):
        raise ValueError("invalid capture observation reference")
    digest = reference.get("sha256")
    name = reference.get("file")
    size = reference.get("size_bytes")
    if (not isinstance(digest, str) or len(digest) != 64
            or any(ch not in "0123456789abcdef" for ch in digest)
            or name != f"observation-{digest}.json"
            or not isinstance(size, int) or isinstance(size, bool) or size < 0):
        raise ValueError("invalid capture observation reference")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    with os.fdopen(os.open(directory / name, flags), "rb") as handle:
        found = os.fstat(handle.fileno())
        if not stat.S_ISREG(found.st_mode) or found.st_size != size:
            raise ValueError("capture observation file identity mismatch")
        payload = handle.read(size + 1)
    if len(payload) != size or hashlib.sha256(payload).hexdigest() != digest:
        raise ValueError("capture observation digest mismatch")
    decoded = json.loads(payload)
    if (not isinstance(decoded, dict) or decoded.get("action_id") != action_id
            or not isinstance(decoded.get("observation"), dict)):
        raise ValueError("capture observation owner mismatch")
    return decoded["observation"]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    # lstat and open are not atomic: a regular file could become a FIFO or
    # symlink between them. Never block on a pipe or hash a substituted link.
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
    with os.fdopen(os.open(path, flags), "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise OSError("capture hash target is no longer a regular file")
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def identity(path: Path, *, hash_small: bool = True) -> dict[str, Any]:
    """一个路径此刻的身份。observation 冻结它，finish 复核时再算一次比对。"""
    return _identity(path, hash_small=hash_small)


def _identity(path: Path, *, hash_small: bool) -> dict[str, Any]:
    try:
        st = path.lstat()
    except FileNotFoundError:
        return {"kind": "missing"}
    except OSError as exc:
        return {"kind": "error", "error": f"{type(exc).__name__}: {exc}"}
    if stat.S_ISLNK(st.st_mode):
        try:
            return {"kind": "symlink", "target": os.readlink(path)}
        except OSError as exc:
            return {"kind": "error", "error": f"{type(exc).__name__}: {exc}"}
    if stat.S_ISDIR(st.st_mode):
        return {"kind": "dir", "inode": st.st_ino, "mtime_ns": st.st_mtime_ns}
    if not stat.S_ISREG(st.st_mode):
        return {"kind": "other", "mode": stat.S_IFMT(st.st_mode), "size": st.st_size,
                "inode": st.st_ino, "mtime_ns": st.st_mtime_ns}
    row: dict[str, Any] = {"kind": "file", "size": st.st_size, "inode": st.st_ino,
                           "mtime_ns": st.st_mtime_ns}
    if hash_small and st.st_size <= SMALL_FILE_BYTES:
        try:
            row["sha256"] = _sha256(path)
        except OSError as exc:
            row["hash_error"] = f"{type(exc).__name__}: {exc}"
    return row


def _junction(st: os.stat_result) -> bool:
    """Windows junction check also works on supported Python 3.11 hosts."""
    tag = getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", None)
    return tag is not None and getattr(st, "st_reparse_tag", None) == tag


def snapshot(candidates: Iterable[Path], roots: Iterable[Path] = (), *,
             max_files: int = DEFAULT_MAX_FILES, max_seconds: float = DEFAULT_MAX_SECONDS,
             hash_small: bool = True, excluded_roots: Iterable[Path] = (),
             include_directories: bool = False) -> dict[str, Any]:
    """对显式候选路径与有界遍历的根做身份快照。超预算 → truncated=True。

    include_directories adds root/traversed-directory identities to the same
    budget. It proves directory identity, not its members' semantic validity.
    """
    started = time.monotonic()
    entries: dict[str, dict[str, Any]] = {}
    truncated = False
    scanned = 0
    enumerated = 0
    coverage: list[dict[str, Any]] = []
    excluded = [Path(p) for p in excluded_roots]
    coverage.extend({"root": str(p), "status": "excluded"} for p in excluded)
    scan_limit = max_files
    time_limit = started + max_seconds

    def observe(path: Path) -> bool:
        nonlocal scanned, truncated
        key = str(path)
        if key in entries:
            return True
        if scanned >= scan_limit or time.monotonic() > time_limit:
            truncated = True
            return False
        entries[key] = _identity(path, hash_small=hash_small)
        scanned += 1
        return True

    for path in candidates:
        p = Path(path)
        if any(p.is_relative_to(root) for root in excluded):
            continue
        if not observe(p):
            break
    root_paths = [Path(root) for root in roots
                  if not any(Path(root).is_relative_to(boundary) for boundary in excluded)]
    for number, r in enumerate(root_paths):
        # Each remaining root receives a share of the SAME remaining budgets.
        # A wide scandir or deep tree in A cannot spend B's shallow coverage.
        # A completed/missing root leaves its unused share for later roots;
        # no cross-root directory iterators or extra filesystem budget survive.
        remaining = len(root_paths) - number
        scan_limit = scanned + (max_files - scanned) // remaining
        name_limit = enumerated + (max_files - enumerated) // remaining
        now = time.monotonic()
        time_limit = now + max(0.0, started + max_seconds - now) / remaining
        covered: dict[str, Any] = {"root": str(r), "status": "complete"}
        coverage.append(covered)
        local_coverage = []
        try:
            root_stat = r.lstat()
        except FileNotFoundError:
            covered["status"] = "missing"
            continue
        except OSError as exc:
            covered.update(status="error", error=f"{type(exc).__name__}: {exc}")
            truncated = True
            continue
        if not stat.S_ISDIR(root_stat.st_mode) or _junction(root_stat):
            if not observe(r):
                covered["status"] = "truncated"
            elif stat.S_ISLNK(root_stat.st_mode) or _junction(root_stat):
                covered["status"] = "excluded"
            continue
        if include_directories and not observe(r):
            covered["status"] = "truncated"
            continue
        errors: list[OSError] = []
        pending = deque([r])
        while pending:
            if scanned >= scan_limit or time.monotonic() > time_limit:
                covered["status"] = "truncated"
                truncated = True
                break
            directory = pending.popleft()
            try:
                directory_stat = directory.lstat()
                if stat.S_ISLNK(directory_stat.st_mode) or _junction(directory_stat):
                    coverage.append({"root": str(directory), "status": "excluded"})
                    continue
                if not stat.S_ISDIR(directory_stat.st_mode):
                    raise NotADirectoryError(str(directory))
            except OSError as exc:
                errors.append(exc)
                coverage.append({"root": str(directory), "status": "error",
                                 "error": str(exc)})
                continue
            if include_directories and not observe(directory):
                covered["status"] = "truncated"
                break
            # Enumerate a directory's direct names before hashing its files.
            # The separate bounded name budget prevents a wide directory from
            # creating an unbounded queue or an unbounded absence claim.
            children = []
            names_complete = True
            try:
                with os.scandir(directory) as entries_in_directory:
                    for entry in entries_in_directory:
                        if enumerated >= name_limit or time.monotonic() > time_limit:
                            names_complete = False
                            covered["status"] = "truncated"
                            truncated = True
                            break
                        enumerated += 1
                        child = directory / entry.name
                        is_directory = entry.is_dir(follow_symlinks=False)
                        is_link_directory = ((entry.is_symlink() and entry.is_dir())
                                             or (is_directory and _junction(entry.stat(follow_symlinks=False))))
                        children.append((child, is_directory, is_link_directory))
            except OSError as exc:
                errors.append(exc)
                names_complete = False
                coverage.append({"root": str(directory), "status": "error",
                                 "error": str(exc)})
            if names_complete:
                direct = {"root": str(directory), "status": "direct_complete",
                          "entry_names": [child.name for child, _, _ in children]}
                coverage.append(direct)
                local_coverage.append(direct)
            for child, is_directory, is_link_directory in children:
                if (any(child.is_relative_to(root) for root in excluded)
                        or is_link_directory
                        or (is_directory and child.name.startswith(".git"))):
                    coverage.append({"root": str(child), "status": "excluded"})
                    continue
                if is_directory:
                    if include_directories and not observe(child):
                        covered["status"] = "truncated"
                        break
                    pending.append(child)
                    continue
                if not observe(child):
                    covered["status"] = "truncated"
                    break
            if covered["status"] == "truncated":
                break
        if errors:
            covered.update(status="error", error=str(errors[0]), error_scope="subtree")
            truncated = True
        if covered["status"] == "complete":
            # Keep complete-tree consumers compatible: these directory
            # subtrees really are complete when the whole root walk finished.
            for row in local_coverage:
                row.update(status="complete", direct_complete=True)
    return {"entries": entries, "truncated": truncated, "scanned": scanned,
            "root_coverage": coverage,
            "seconds": round(time.monotonic() - started, 3), "taken_at": time.time()}


def _same_identity(a: dict[str, Any], b: dict[str, Any]) -> tuple[bool, str]:
    """返回 (相同?, 判断依据)。"""
    if a.get("kind") != b.get("kind"):
        return False, "kind"
    if a.get("kind") == "symlink":
        return a.get("target") == b.get("target"), "link_target"
    if a.get("kind") != "file":
        return (a.get("inode") == b.get("inode") and a.get("mtime_ns") == b.get("mtime_ns")), "inode+mtime"
    same = (a.get("size") == b.get("size") and a.get("inode") == b.get("inode")
            and a.get("mtime_ns") == b.get("mtime_ns"))
    if "sha256" in a and "sha256" in b:
        if a["sha256"] != b["sha256"]:
            return False, "sha256"
        # Equal bytes do not make two physical writes the same action. A
        # retry can legitimately regenerate the same result with new inputs.
        return same, "size+inode+mtime+sha256"
    if "sha256" in a or "sha256" in b or a.get("hash_error") or b.get("hash_error"):
        return False, "hash_unavailable"
    return same, "size+inode+mtime"


def same_identity(a: dict[str, Any], b: dict[str, Any]) -> tuple[bool, str]:
    """Shared observation/readback comparison; byte equality is not lineage."""
    return _same_identity(a, b)


def _coverage_index(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Ephemeral lookup only; the serialized coverage remains the single B fact."""
    direct: dict[Path, set[Path] | None] = {}
    recursive: dict[Path, str] = {}
    barriers: set[Path] = set()
    for row in snapshot.get("root_coverage") or []:
        directory = Path(row["root"])
        status = row.get("status")
        if status == "direct_partial":
            # A presentation-only list cannot establish that a name is absent.
            # The verified immutable reference is required to recover facts.
            barriers.add(directory)
            continue
        if status == "direct_complete" or row.get("direct_complete"):
            names = row.get("entry_names")
            if (not isinstance(names, list)
                    or any(not isinstance(name, str) or name in {"", ".", ".."}
                           or Path(name).name != name for name in names)):
                direct[directory] = None
            elif directory not in direct:
                direct[directory] = {Path(name) for name in names}
            elif direct[directory] is not None:
                direct[directory].update(Path(name) for name in names)
        else:
            recursive.setdefault(directory, status)
            if status == "excluded" or (status == "error" and row.get("error_scope") != "subtree"):
                barriers.add(directory)
    return {"direct": direct, "recursive": recursive, "barriers": barriers}


def _absence_known(snapshot: dict[str, Any], path: str, *,
                   coverage_index: dict[str, Any] | None = None) -> bool:
    entries = snapshot.get("entries") or {}
    if path in entries:
        return entries[path].get("kind") == "missing"
    coverage = snapshot.get("root_coverage")
    if coverage is None:
        return not snapshot.get("truncated") and not snapshot.get("snapshot_error")
    candidate = Path(path)
    index = coverage_index if coverage_index is not None else _coverage_index(snapshot)
    ancestors = (candidate, *candidate.parents)
    if any(ancestor in index["barriers"] for ancestor in ancestors):
        return False
    for directory in candidate.parents:
        if directory in index["direct"]:
            names = index["direct"][directory]
            if names is None:
                return False
            # This proves absence of precisely one direct child. A descendant
            # is also absent only when that ancestor name did not exist.
            # An existing but unvisited child never proves subtree absence.
            child_name = Path(candidate.relative_to(directory).parts[0])
            return child_name not in names
    for ancestor in ancestors:
        if ancestor in index["recursive"]:
            return index["recursive"][ancestor] in {"complete", "missing"}
    return False


def diff(before: dict[str, Any], after: dict[str, Any], *, limit: int | None = 200) -> dict[str, Any]:
    """Diff bounded snapshots; ``None`` retains all already observed facts.

    ``limit`` is a legacy summary limit, not an additional filesystem budget.
    The B writers retain the full diff, then bound only its presentation.
    """
    b_entries = before.get("entries") or {}
    a_entries = after.get("entries") or {}
    created: list[str] = []
    modified: list[str] = []
    unchanged: list[str] = []
    deleted: list[str] = []
    unknown: list[str] = []
    basis: dict[str, str] = {}
    before_coverage = _coverage_index(before)
    after_coverage = _coverage_index(after)
    for key, after_row in a_entries.items():
        before_row = b_entries.get(key, {"kind": "missing"})
        if before_row.get("kind") == "error" or after_row.get("kind") == "error":
            unknown.append(key)
            continue
        if after_row.get("kind") == "missing":
            if before_row.get("kind") != "missing":
                deleted.append(key)
            continue
        if before_row.get("kind") == "missing":
            (created if _absence_known(before, key, coverage_index=before_coverage) else unknown).append(key)
            continue
        same, why = _same_identity(before_row, after_row)
        basis[key] = why
        (unchanged if same else modified).append(key)
    for key, before_row in b_entries.items():
        if key not in a_entries and before_row.get("kind") != "missing":
            (deleted if _absence_known(after, key, coverage_index=after_coverage) else unknown).append(key)
    return {
        "created": created[:limit], "modified": modified[:limit], "unchanged_count": len(unchanged),
        "deleted": deleted[:limit],
        "unknown": unknown[:limit],
        "truncated": bool(before.get("truncated") or after.get("truncated")
                          or (limit is not None and (len(created) + len(modified) > limit
                              or len(deleted) > limit or len(unknown) > limit))),
        "identity_basis": {k: basis[k] for k in (created + modified)[:limit] if k in basis},
    }


def written_in_action(observation: dict[str, Any], path: Path) -> str:
    """一条 obligation 路径在这次 observation 里的结论：created / modified / unchanged /
    missing / unknown。"""
    key = str(Path(path))
    if key in (observation.get("created") or []):
        return "created"
    if key in (observation.get("modified") or []):
        return "modified"
    if key in (observation.get("deleted") or []):
        return "missing"
    if key in (observation.get("unknown") or []):
        return "unknown"
    if observation.get("truncated"):
        return "unknown"
    return "unchanged"

"""Declared-output evaluator shared by the route verifier, health finalizer and ROC.

035a（C5，SCOPE G2/G4/G5；口径见 035/05）：三个入口原来各用一把尺——ROC 只看
``is_file && size>0``，route 只比 stat 指纹是否变化，health 只看 ``exists ∧ mtime ≥
submitted_at``。结果空文件、空目录、指向旧二进制的软链/硬链、只 chmod 的旧文件都
被收成"本次产出"。这里是唯一的判定实现；三处只负责喂输入、消费结论。

同一份产物身份（P0a v4）：``output_identity`` / ``output_lexical_key`` /
``output_identity_matches`` 也住在这里，``execution_route`` 只是再导出——attempt 收尾
冻结的 ``output_observations`` 与本模块的 observation 是同一形状。

判定（一条声明 spec）：
* kind：原始 spec 以 ``/`` 结尾 = directory，否则 = file；在任何 realpath / glob 之前判。
* 匹配：glob 相对声明根展开；解析后逃出根的一律丢弃。
* file：lexical 位置上是普通文件，或指向根内普通文件的软链；``size > 0``，除非其
  realpath 在 ``allow_empty_realpaths``（只能是当前受管作业精确登记的 stdout/stderr）。
* 新鲜（绑定基线在时）：与基线同 lexical 路径比 (dev, ino, size, mtime)——变了才算；
  只动 ctime（chmod）不算。基线里没有的路径：ctime 必须不早于 ``not_before_ns``
  （老文件经软链/改名冒充会露馅），且 ``nlink == 1`` 或 mtime 不早于 ``not_before_ns``
  （硬链到旧内容会露馅；``cp -p`` 出的新 inode 不受影响——mtime 不是 producer identity）。
* directory：lexical 位置上是目录（不跟软链），且至少含一个按上面规则新鲜、非空的
  普通文件条目。
* 明确残余：``touch`` 一个既有文件（同 inode、同尺寸、mtime 变）与合法原位改写在
  stat 事实上不可区分；本模块**不宣称**封它，observation 的 ``freshness`` 会如实写
  ``mtime_changed_same_inode``。
"""
from __future__ import annotations

import glob
import hashlib
import os
import stat as statmod
from pathlib import Path
from typing import Any, Iterable

_OUTPUT_HASH_CAP_BYTES = 256 * 1024 * 1024
DEFAULT_OBSERVATION_CAP = 500
#: 粗时钟滞后容差（只用于识别旧内容，见 _freshness）。
STALE_TOLERANCE_NS = 100_000_000

REASON_MISSING = "missing"
REASON_EMPTY_FILE = "empty_file"
REASON_EMPTY_DIRECTORY = "empty_directory"
REASON_NO_FRESH_ENTRY = "no_fresh_entry"
REASON_KIND_MISMATCH = "kind_mismatch"
REASON_NOT_REGULAR = "not_regular_file"
REASON_ESCAPES_ROOT = "escapes_root"
REASON_UNCHANGED = "unchanged_since_bind"
REASON_STALE_TARGET = "stale_target"
REASON_HARDLINK_STALE = "hardlink_to_stale_content"


# ── 产物身份（P0a v4 起的唯一实现） ─────────────────────────────────────────

def output_lexical_key(path_text: str) -> str:
    """产物的 lexical 身份：父目录解析到底，最后一段**不**跟符号链接。"""
    path = os.path.abspath(os.path.expanduser(str(path_text)))
    parent, name = os.path.split(path.rstrip(os.sep) or path)
    if not name:
        return os.path.realpath(path)
    return os.path.join(os.path.realpath(parent), name)


def output_identity(path_text: str, *, spec: str | None = None) -> dict[str, Any]:
    """一个产物此刻的身份：kind（lstat）、dev/ino/nlink/size、正文 sha256、链接目标。"""
    key = output_lexical_key(path_text)
    row: dict[str, Any] = {
        "path": key, "spec": spec, "kind": "missing",
        "st_dev": None, "st_ino": None, "st_nlink": None, "size_bytes": None,
        "mtime_ns": None, "ctime_ns": None,
        "sha256": None, "sha256_skipped": None, "link_target": None,
    }
    try:
        st = os.lstat(key)
    except OSError:
        return row
    row.update({
        "kind": _kind_of(st.st_mode),
        "st_dev": int(st.st_dev), "st_ino": int(st.st_ino), "st_nlink": int(st.st_nlink),
        "size_bytes": int(st.st_size),
        "mtime_ns": int(st.st_mtime_ns), "ctime_ns": int(st.st_ctime_ns),
    })
    if row["kind"] == "symlink":
        try:
            row["link_target"] = os.readlink(key)
        except OSError:
            row["link_target"] = None
        # 对抗审查（09-21）：只记链接字符串，目标事后改写不会露馅——目标身份一并记。
        resolved = os.path.realpath(key)
        row["target_path"] = resolved
        target = _member(resolved) if os.path.lexists(resolved) else None
        if target is None:
            row["target_kind"] = "missing"
        else:
            row["target_kind"] = target["kind"]
            row["target_st_dev"] = target["st_dev"]
            row["target_st_ino"] = target["st_ino"]
            row["target_size_bytes"] = target["size_bytes"]
            if target["kind"] == "file":
                row["target_sha256"], row["target_sha256_skipped"] = _file_digest(
                    resolved, target["size_bytes"])
    elif row["kind"] == "file":
        row["sha256"], row["sha256_skipped"] = _file_digest(key, st.st_size)
    elif row["kind"] == "directory":
        # 对抗审查（09-21）：目录作为产物时，事后原地改写其中的文件不改目录的
        # inode/size/ctime——把条目身份（含摘要）一并冻结。
        entries: list[dict[str, Any]] = []
        truncated = False
        for entry in _directory_entries(key):
            if len(entries) >= DEFAULT_OBSERVATION_CAP:
                truncated = True
                break
            member = _member(entry)
            if member is None:
                continue
            item = {"name": os.path.basename(entry), "kind": member["kind"],
                    "st_ino": member["st_ino"], "size_bytes": member["size_bytes"]}
            if member["kind"] == "file":
                item["sha256"], item["sha256_skipped"] = _file_digest(entry, member["size_bytes"])
            entries.append(item)
        row["entries"] = entries
        row["entries_truncated"] = truncated
    return row


def _file_digest(path: str, size: int) -> tuple[str | None, str | None]:
    if size > _OUTPUT_HASH_CAP_BYTES:
        return None, "size_over_cap"
    try:
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest(), None
    except OSError:
        return None, "unreadable"


def output_identity_matches(recorded: Any, current: Any) -> tuple[bool, str | None]:
    """收据里的身份与此刻的身份是否同一个产物；不同时说清是哪一种不同。"""
    if not isinstance(recorded, dict) or not isinstance(current, dict):
        return False, "identity_unreadable"
    kind = str(recorded.get("kind") or "missing")
    if kind == "missing":
        return False, "not_present_at_attempt_end"
    if str(current.get("kind") or "missing") != kind:
        return False, "kind_changed_since_attempt"
    hashed = bool(recorded.get("sha256") and current.get("sha256"))
    if kind == "file" and hashed and recorded["sha256"] != current["sha256"]:
        return False, "content_changed_since_attempt"
    for field in ("st_dev", "st_ino", "size_bytes"):
        if recorded.get(field) != current.get(field):
            return False, "identity_changed_since_attempt"
    if kind == "symlink":
        if recorded.get("link_target") != current.get("link_target"):
            return False, "identity_changed_since_attempt"
        # 链接指向的内容也是产物的一部分：目标被改写/顶替 → 不是同一个产物。
        if recorded.get("target_kind") != current.get("target_kind"):
            return False, "target_changed_since_attempt"
        target_hashed = bool(recorded.get("target_sha256") and current.get("target_sha256"))
        if target_hashed and recorded["target_sha256"] != current["target_sha256"]:
            return False, "target_content_changed_since_attempt"
        for field in ("target_st_dev", "target_st_ino", "target_size_bytes"):
            if recorded.get(field) != current.get(field):
                return False, "target_changed_since_attempt"
    elif kind == "directory":
        before = {e.get("name"): e for e in (recorded.get("entries") or []) if isinstance(e, dict)}
        now = {e.get("name"): e for e in (current.get("entries") or []) if isinstance(e, dict)}
        if set(before) != set(now):
            return False, "directory_entries_changed_since_attempt"
        for name, item in before.items():
            other = now[name]
            if item.get("kind") != other.get("kind"):
                return False, "directory_entries_changed_since_attempt"
            if item.get("sha256") and other.get("sha256"):
                if item["sha256"] != other["sha256"]:
                    return False, "directory_entry_content_changed_since_attempt"
            elif (item.get("st_ino"), item.get("size_bytes")) != (other.get("st_ino"), other.get("size_bytes")):
                return False, "directory_entries_changed_since_attempt"
    elif kind == "file" and not hashed:
        if recorded.get("ctime_ns") != current.get("ctime_ns"):
            # 大文件没有摘要：任何改写都会推进 ctime（用户态改不回去）。
            return False, "identity_changed_since_attempt"
    return True, None


def _kind_of(mode: int) -> str:
    if statmod.S_ISLNK(mode):
        return "symlink"
    if statmod.S_ISREG(mode):
        return "file"
    if statmod.S_ISDIR(mode):
        return "directory"
    return "other"


# ── 声明解析与展开 ────────────────────────────────────────────────────────────

def declared_output_kind(spec: str) -> str:
    """尾 ``/`` 是目录声明的唯一语法；必须在任何规范化之前判。"""
    return "directory" if str(spec).rstrip().endswith("/") else "file"


def _pattern(spec: str) -> str:
    text = str(spec).strip()
    return text.rstrip("/") if declared_output_kind(text) == "directory" else text


def _within(path: str, root: str) -> bool:
    return path == root or path.startswith(root + os.sep)


def expand_declared_output(root: str, spec: str) -> list[str]:
    """spec 在声明根内的 lexical 匹配；解析后逃出根的丢弃；按声明 kind 过滤。"""
    base = os.path.realpath(os.path.expanduser(str(root)))
    kind = declared_output_kind(spec)
    pattern = _pattern(spec)
    if not pattern:
        return []
    found: list[str] = []
    for item in glob.glob(os.path.join(base, pattern)):
        real = os.path.realpath(item)
        if not _within(real, base) or not os.path.exists(real):
            continue
        key = output_lexical_key(item)
        try:
            st = os.lstat(key)
        except OSError:
            continue
        lexical_kind = _kind_of(st.st_mode)
        if kind == "directory":
            if lexical_kind != "directory":
                continue
        elif lexical_kind == "directory":
            continue
        found.append(key)
    return sorted(set(found))


def _member(key: str) -> dict[str, Any] | None:
    try:
        st = os.lstat(key)
    except OSError:
        return None
    row = {
        "kind": _kind_of(st.st_mode), "st_dev": int(st.st_dev), "st_ino": int(st.st_ino),
        "st_nlink": int(st.st_nlink), "size_bytes": int(st.st_size),
        "mtime_ns": int(st.st_mtime_ns), "ctime_ns": int(st.st_ctime_ns),
    }
    if row["kind"] == "symlink":
        row["target"] = os.path.realpath(key)
    return row


def _directory_entries(key: str) -> list[str]:
    try:
        with os.scandir(key) as it:
            return sorted(os.path.join(key, entry.name) for entry in it)
    except OSError:
        return []


def output_baseline(root: str, specs: Iterable[str]) -> dict[str, dict[str, Any]]:
    """绑定时的基线：每个 spec 的匹配成员身份（目录 spec 记其条目）。"""
    result: dict[str, dict[str, Any]] = {}
    for spec in specs:
        spec = str(spec).strip()
        if not spec:
            continue
        matches = expand_declared_output(root, spec)
        members: dict[str, dict[str, Any]] = {}
        for key in matches:
            row = _member(key)
            if row is None:
                continue
            members[key] = row
            if row["kind"] == "directory":
                for entry in _directory_entries(key):
                    entry_row = _member(entry)
                    if entry_row is not None:
                        members[entry] = entry_row
        result[spec] = {"count": len(matches), "members": members}
    return result


# ── 新鲜度 ────────────────────────────────────────────────────────────────────

REASON_AFTER_TERMINAL = "written_after_terminal"


def _freshness(
    key: str, now: dict[str, Any], baseline_members: dict[str, Any] | None,
    not_before_ns: int | None, not_after_ns: int | None = None,
) -> tuple[bool, str]:
    """(fresh?, basis)。basis 既是通过依据也是拒绝原因。"""
    # 上界（对抗审查 09-21）：外部作业的收据在核验时刻观测，作业结束到核验之间由
    # 模型 shell 写出的文件会被收进收据。有物理结束时间 / 首次终态观测时刻时，
    # ctime 晚于它的文件不是作业写的。
    if not_after_ns is not None and now["ctime_ns"] > not_after_ns + STALE_TOLERANCE_NS:
        return False, REASON_AFTER_TERMINAL
    before = (baseline_members or {}).get(key)
    if isinstance(before, dict):
        if (before.get("st_dev"), before.get("st_ino")) != (now["st_dev"], now["st_ino"]):
            return True, "new_inode"
        if before.get("size_bytes") != now["size_bytes"]:
            return True, "rewritten_same_inode"
        if before.get("mtime_ns") != now["mtime_ns"]:
            # 明确残余：touch 与合法原位同尺寸改写在这里不可区分。
            return True, "mtime_changed_same_inode"
        return False, REASON_UNCHANGED
    if not_before_ns is None:
        return True, "present"
    # 基线里没有的路径只可能是绑定之后出现的：这里的时间下界只用来识别**旧内容**
    # （老文件经软链 / 硬链冒充），不承担归属——归属由基线成员身份与收据判。文件
    # 时间戳来自内核粗时钟、比 time.time_ns() 滞后几毫秒，所以留 STALE_TOLERANCE_NS；
    # 它放不进任何绑定前就存在的文件（那些在基线里，按身份变化判）。
    floor = not_before_ns - STALE_TOLERANCE_NS
    if now["ctime_ns"] < floor:
        return False, REASON_STALE_TARGET
    if now["st_nlink"] > 1 and now["mtime_ns"] < floor:
        return False, REASON_HARDLINK_STALE
    return True, "created_during_attempt"


def _evaluate_file(
    key: str, root: str | None, baseline_members: dict[str, Any] | None,
    not_before_ns: int | None, allow_empty: set[str], not_after_ns: int | None = None,
) -> tuple[bool, str, dict[str, Any]]:
    now = _member(key)
    if now is None:
        return False, REASON_MISSING, {}
    extra: dict[str, Any] = {}
    target_key = key
    target = now
    if now["kind"] == "symlink":
        resolved = os.path.realpath(key)
        extra["resolved_target"] = resolved
        if root is not None and not _within(resolved, root):
            return False, REASON_ESCAPES_ROOT, extra
        target_key = output_lexical_key(resolved)
        target = _member(target_key)
        if target is None:
            return False, REASON_MISSING, extra
    if target["kind"] != "file":
        return False, REASON_NOT_REGULAR, extra
    if target["size_bytes"] <= 0 and os.path.realpath(target_key) not in allow_empty:
        return False, REASON_EMPTY_FILE, extra
    fresh, basis = _freshness(target_key, target, baseline_members, not_before_ns, not_after_ns)
    extra["freshness"] = basis
    return fresh, basis, extra


def _evaluate_directory(
    key: str, root: str | None, baseline_members: dict[str, Any] | None,
    not_before_ns: int | None, allow_empty: set[str], not_after_ns: int | None = None,
) -> tuple[bool, str, dict[str, Any]]:
    now = _member(key)
    if now is None:
        return False, REASON_MISSING, {}
    if now["kind"] != "directory":
        return False, REASON_KIND_MISMATCH, {}
    entries = _directory_entries(key)
    if not entries:
        return False, REASON_EMPTY_DIRECTORY, {"entry_count": 0}
    fresh_entries: list[str] = []
    for entry in entries:
        ok, _basis, _extra = _evaluate_file(
            entry, root, baseline_members, not_before_ns, allow_empty, not_after_ns)
        if ok:
            fresh_entries.append(entry)
    if not fresh_entries:
        return False, REASON_NO_FRESH_ENTRY, {"entry_count": len(entries), "fresh_entry_count": 0}
    return True, "fresh_entries", {
        "entry_count": len(entries), "fresh_entry_count": len(fresh_entries),
        "fresh_entries": [os.path.basename(item) for item in fresh_entries],
    }


def evaluate_declared_outputs(
    specs: Iterable[str],
    *,
    root: str,
    baseline: dict[str, Any] | None = None,
    not_before_ns: int | None = None,
    not_after_ns: int | None = None,
    allow_empty_realpaths: Iterable[str] = (),
    observation_cap: int = DEFAULT_OBSERVATION_CAP,
) -> dict[str, Any]:
    """三处共用的判定。返回确定性的、JSON-safe 的结论与逐文件 observation。

    一条 spec 的成员**逐个**判（对抗审查 09-21：集合指纹变了就把所有匹配收进收据，
    绑定前就在的假产物跟着一起被归属）：只有通过的成员进入 verified_paths / 收据；
    spec 有至少一个通过成员即算交付，未通过的成员如实留在 observation 里。字面
    （无通配）spec 先于通配 spec 记录，避免大 glob 把明确声明的产物挤出上限。"""
    base = os.path.realpath(os.path.expanduser(str(root)))
    allow_empty = {os.path.realpath(str(item)) for item in allow_empty_realpaths if str(item)}
    verified_specs: list[str] = []
    verified_paths: list[str] = []
    failed_specs: list[str] = []
    failures: dict[str, str] = {}
    observations: list[dict[str, Any]] = []
    truncated = False
    ordered = sorted(
        (str(raw).strip() for raw in specs if str(raw).strip()),
        key=lambda item: any(ch in item for ch in "*?["),
    )
    for spec in ordered:
        kind = declared_output_kind(spec)
        matches = expand_declared_output(base, spec)
        spec_baseline = (baseline or {}).get(spec) if isinstance(baseline, dict) else None
        members = spec_baseline.get("members") if isinstance(spec_baseline, dict) else None
        legacy_fingerprint = (
            spec_baseline.get("fingerprint")
            if isinstance(spec_baseline, dict) and members is None else None
        )
        passed_paths: list[str] = []
        reasons: list[str] = []
        for key in matches:
            if kind == "directory":
                ok, reason, extra = _evaluate_directory(
                    key, base, members, not_before_ns, allow_empty, not_after_ns)
            else:
                ok, reason, extra = _evaluate_file(
                    key, base, members, not_before_ns, allow_empty, not_after_ns)
            if ok and legacy_fingerprint is not None and kind == "file":
                # 旧绑定记录只有集合指纹：沿用原判据（指纹没变 = 本次没更新）。
                if _legacy_fingerprint(base, matches) == str(legacy_fingerprint):
                    ok, reason = False, REASON_UNCHANGED
            row = output_identity(key, spec=spec)
            row.update({"declared_kind": kind, "passed": bool(ok), "reason": None if ok else reason})
            row.setdefault("freshness", extra.get("freshness") or reason if ok else None)
            for extra_key, value in extra.items():
                row[extra_key] = value
            if len(observations) < observation_cap:
                observations.append(row)
            else:
                truncated = True
            if ok:
                passed_paths.append(os.path.realpath(key) if kind == "file" else key)
            else:
                reasons.append(reason)
        if passed_paths:
            verified_specs.append(spec)
            verified_paths.extend(passed_paths)
        else:
            failed_specs.append(spec)
            failures[spec] = sorted(set(reasons))[0] if reasons else REASON_MISSING
    return {
        "passed": not failed_specs,
        "root": base,
        "verified_specs": verified_specs,
        "verified_paths": sorted(set(verified_paths)),
        "failed_specs": failed_specs,
        "failure_reasons": failures,
        "observations": observations,
        "observations_truncated": truncated,
        "not_before_ns": not_before_ns,
        "not_after_ns": not_after_ns,
    }


def _legacy_fingerprint(base: str, matches: list[str]) -> str:
    """基线只有集合指纹的旧绑定记录：按原算法重算（相对路径、mode、ino、size、mtime、ctime）。"""
    import json

    rows: list[tuple[Any, ...]] = []
    for key in sorted(matches):
        try:
            resolved = Path(os.path.realpath(key))
            st = resolved.stat()
            rows.append((
                str(resolved.relative_to(base)), st.st_mode, st.st_ino, st.st_size,
                st.st_mtime_ns, st.st_ctime_ns,
            ))
        except (OSError, ValueError):
            continue
    return hashlib.sha256(
        json.dumps(rows, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def unchanged_specs(result: dict[str, Any]) -> list[str]:
    """失败清单里「文件在、但本次没更新它」的那几项（只用来把报错说准）。"""
    reasons = result.get("failure_reasons") or {}
    return [spec for spec in result.get("failed_specs") or [] if reasons.get(spec) == REASON_UNCHANGED]


# ── ROC：单个声明路径（无根、无窗口） ─────────────────────────────────────────

def evaluate_declared_path(
    path_text: str, *, declared_kind: str = "file", allow_empty_realpaths: Iterable[str] = (),
) -> dict[str, Any]:
    """ROC 声明的 artifact / executable / json 路径：存在、类型、非空；没有基线也没有
    时间窗（归属由 P0a v4 的收据另判）。"""
    key = output_lexical_key(path_text)
    allow_empty = {os.path.realpath(str(item)) for item in allow_empty_realpaths if str(item)}
    if declared_kind == "directory":
        ok, reason, extra = _evaluate_directory(key, None, None, None, allow_empty, None)
    else:
        ok, reason, extra = _evaluate_file(key, None, None, None, allow_empty, None)
    row = output_identity(key)
    row.update({"declared_kind": declared_kind, "passed": bool(ok), "reason": None if ok else reason})
    for extra_key, value in extra.items():
        row[extra_key] = value
    return row


# ── health：对冻结的快照判定（不重新读盘） ────────────────────────────────────

def snapshot_row(path: str, *, entry_cap: int = DEFAULT_OBSERVATION_CAP) -> dict[str, Any]:
    """health 探针的一条 completion path 快照：便宜的 stat 事实（不算摘要），目录带条目。"""
    now = _member(path)
    if now is None:
        return {"path": path, "exists": False}
    from datetime import datetime, timezone

    row: dict[str, Any] = {
        "path": path, "exists": True, "is_file": now["kind"] == "file",
        "size_bytes": now["size_bytes"] if now["kind"] == "file" else None,
        "mtime": datetime.fromtimestamp(now["mtime_ns"] / 1e9, timezone.utc).isoformat(),
        "mtime_epoch_s": now["mtime_ns"] / 1e9,
        "kind": now["kind"], "st_dev": now["st_dev"], "st_ino": now["st_ino"],
        "st_nlink": now["st_nlink"], "mtime_ns": now["mtime_ns"], "ctime_ns": now["ctime_ns"],
    }
    if now["kind"] == "directory":
        entries: list[dict[str, Any]] = []
        truncated = False
        for entry in _directory_entries(path):
            if len(entries) >= entry_cap:
                truncated = True
                break
            member = _member(entry)
            if member is not None:
                entries.append({"path": entry, **member})
        row["entries"] = entries
        row["entries_truncated"] = truncated
    return row


def _snapshot_file_ok(
    row: dict[str, Any], not_before_ns: int | None, allow_empty: set[str],
) -> tuple[bool, str]:
    if row.get("kind") != "file":
        return False, REASON_NOT_REGULAR
    if int(row.get("size_bytes") or 0) <= 0 and os.path.realpath(str(row.get("path"))) not in allow_empty:
        return False, REASON_EMPTY_FILE
    now = {
        "st_dev": row.get("st_dev"), "st_ino": row.get("st_ino"),
        "st_nlink": int(row.get("st_nlink") or 1), "size_bytes": int(row.get("size_bytes") or 0),
        "mtime_ns": int(row.get("mtime_ns") or 0), "ctime_ns": int(row.get("ctime_ns") or 0),
    }
    return _freshness(str(row.get("path")), now, None, not_before_ns)


def evaluate_health_snapshots(
    snapshots: Iterable[Any], *, kinds: dict[str, str] | None = None,
    not_before_ns: int | None, allow_empty_realpaths: Iterable[str] = (),
) -> tuple[bool, str, list[dict[str, Any]]]:
    """health completion_paths：新探针快照按完整规则；只有 exists/mtime_epoch_s 的旧快照
    沿用「exists ∧ mtime ≥ submitted」。返回 (全部通过?, 第一条失败原因文本, 逐条结论)。"""
    allow_empty = {os.path.realpath(str(item)) for item in allow_empty_realpaths if str(item)}
    results: list[dict[str, Any]] = []
    first_failure = ""
    for snapshot in snapshots:
        if not isinstance(snapshot, dict):
            results.append({"path": snapshot, "passed": False, "reason": REASON_MISSING})
            first_failure = first_failure or f"completion path 不存在：{snapshot}"
            continue
        path = str(snapshot.get("path") or "")
        if not snapshot.get("exists"):
            results.append({"path": path, "passed": False, "reason": REASON_MISSING})
            first_failure = first_failure or f"completion path 不存在：{path}"
            continue
        if "kind" not in snapshot:
            # 旧快照：没有类型与 ctime，只能按原判据。
            mtime = snapshot.get("mtime_epoch_s")
            fresh = isinstance(mtime, (int, float)) and (
                not_before_ns is None or float(mtime) * 1e9 >= not_before_ns - STALE_TOLERANCE_NS)
            results.append({"path": path, "passed": bool(fresh),
                            "reason": None if fresh else REASON_STALE_TARGET, "legacy_snapshot": True})
            if not fresh:
                first_failure = first_failure or f"completion path 的 mtime 早于 submitted_at：{path}"
            continue
        declared = (kinds or {}).get(path) or "file"
        if declared == "directory":
            if snapshot.get("kind") != "directory":
                ok, reason = False, REASON_KIND_MISMATCH
            else:
                entries = [e for e in (snapshot.get("entries") or []) if isinstance(e, dict)]
                fresh_entries = [e for e in entries if _snapshot_file_ok(e, not_before_ns, allow_empty)[0]]
                ok = bool(fresh_entries)
                reason = None if ok else (REASON_EMPTY_DIRECTORY if not entries else REASON_NO_FRESH_ENTRY)
        else:
            if snapshot.get("kind") == "directory":
                ok, reason = False, REASON_KIND_MISMATCH
            else:
                ok, reason = _snapshot_file_ok(snapshot, not_before_ns, allow_empty)
                reason = None if ok else reason
        results.append({"path": path, "passed": bool(ok), "reason": reason, "declared_kind": declared})
        if not ok:
            first_failure = first_failure or _HEALTH_REASON_TEXT.get(reason, reason).format(path=path)
    return (all(r["passed"] for r in results) and bool(results)), first_failure, results


_HEALTH_REASON_TEXT = {
    REASON_EMPTY_FILE: "completion path 是空文件：{path}",
    REASON_EMPTY_DIRECTORY: "completion path 是空目录：{path}",
    REASON_NO_FRESH_ENTRY: "completion path 目录里没有本次作业写出的非空文件：{path}",
    REASON_KIND_MISMATCH: "completion path 的类型与声明不符（目录声明须以 / 结尾）：{path}",
    REASON_NOT_REGULAR: "completion path 不是普通文件：{path}",
    REASON_STALE_TARGET: "completion path 的内容早于作业提交（ctime 早于 submitted_at）：{path}",
    REASON_HARDLINK_STALE: "completion path 是指向旧内容的硬链接：{path}",
    REASON_UNCHANGED: "completion path 本次没有更新：{path}",
}

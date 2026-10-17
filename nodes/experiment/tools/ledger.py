"""B 事实的规范化 provenance / event index（v3 governing document §2、§5、§9）。

一个 run 一份 append-only 的 JSONL 账本，落在 run 目录下：

    <run>/ledger/ledger.meta.json   纪元与 schema 标记、run_id、创建时间
    <run>/ledger/events.jsonl       一行一条记录，先 fsync 再返回
    <run>/ledger/owner.lock         权威 owner（会话身份 + pid + 主机）

规则：
* 任何会话都能只读打开；只有权威 owner 能追加、reconcile、cancel。
* 每条记录都带 protocol_epoch 与 schema_version；缺任一即旧协议或损坏。
* crash-durable：写入 = 序列化一行 + write + fsync。读取时**只有最后一行**允许
  是不完整的（进程在写到一半时死掉），它被记为 corrupt_tail 并忽略；中间出现
  坏行是损坏，fail-loud。
* 旧协议 run（run 目录已有历史但没有账本 meta）确定性判为 old_protocol：只读，
  不会被误识别成新 run。
* 这里只有事实与投影，没有门：账本不判断任何事情。
"""

from __future__ import annotations

import json
import os
import socket
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from shared.lib.filelock import exclusive

try:
    from .protocol_epoch import (
        LEDGER_SCHEMA_VERSION,
        PROTOCOL_EPOCH,
        is_current_epoch,
        record_marker,
    )
except ImportError:  # pragma: no cover - standalone node bootstrap
    from tools.protocol_epoch import (  # type: ignore
        LEDGER_SCHEMA_VERSION,
        PROTOCOL_EPOCH,
        is_current_epoch,
        record_marker,
    )


class LedgerError(RuntimeError):
    """账本层唯一的异常基类。"""


class LedgerReadOnly(LedgerError):
    """本会话不是该 run 的权威 owner，只能读。"""


class LedgerCorrupt(LedgerError):
    """账本中间出现无法解析的记录（不是尾部截断）。"""


_LEDGER_DIRNAME = "ledger"
_META = "ledger.meta.json"
_EVENTS = "events.jsonl"
_EVENTS_LOCK = "events.lock"
_LOCK = "owner.lock"
_READY = "bootstrap.ready"   # bootstrap 成功建立最低 A 之后才写；没有它 = initializing

#: 旧协议（v2：路线 / 收据 / verify / finalize 手续）**只有旧 run 才会有**的落盘痕迹。
#: 注意：v2 的分类会在同一进程里留下 .run_acceptance_receipt.lock 与 transcript，
#: 新协议过渡期的 run 也会有它们，所以它们不能当标记。
_OLD_PROTOCOL_FILE_MARKERS = ("operation_evidence",)
_OLD_PROTOCOL_ARTIFACT_OWNER = "record_operation_completion"
_OLD_PROTOCOL_ARTIFACT_TYPE_PREFIXES = ("execution_route", "route_")
_CLOSURE_ARTIFACT_TYPES = ("raw_results", "clean_results", "experiment_log")



def ledger_dir(state: Any) -> Path:
    return Path(state.root) / _LEDGER_DIRNAME


def _session_identity(state: Any) -> str:
    session = str(getattr(state, "session_id", None) or "").strip()
    return session or f"run:{state.run_id}"


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    from shared.lib.process_control import alive
    return alive(pid)


def _now() -> float:
    return time.time()


def _fsync_dir(path: Path) -> None:
    try:
        _fsync_dir_strict(path)
    except OSError:
        pass


def _fsync_dir_strict(path: Path) -> None:
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_file_and_parent(path: Path) -> None:
    """Confirm an already-visible event file and its directory are durable."""
    fd = os.open(str(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    _fsync_dir_strict(path.parent)


def _exact_json_equal(left: Any, right: Any) -> bool:
    """Compare values exactly as their persisted JSON material."""
    def canonical(value: Any) -> str:
        persisted = json.loads(json.dumps(
            value, ensure_ascii=False, default=str,
        ))
        return json.dumps(
            persisted, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"),
        )

    return canonical(left) == canonical(right)


def detect_epoch(state: Any) -> str:
    """'v3'：账本 meta 带当前标记；'old_protocol'：没有账本但 run 目录已有历史；
    'fresh'：什么都没有；'foreign'：有 meta 但标记不匹配（更新或更旧的协议）。"""
    d = ledger_dir(state)
    meta = d / _META
    if meta.exists():
        try:
            payload = json.loads(meta.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return "foreign"
        return PROTOCOL_EPOCH if is_current_epoch(payload) else "foreign"
    root = Path(state.root)
    for marker in _OLD_PROTOCOL_FILE_MARKERS:
        if (root / marker).exists():
            return "old_protocol"
    try:
        records = state.list_artifacts(None, own_only=True)
    except Exception:
        records = []
    for record in records or []:
        artifact_type = str(record.get("type") or record.get("artifact_type") or "")
        if artifact_type.startswith(_OLD_PROTOCOL_ARTIFACT_TYPE_PREFIXES):
            return "old_protocol"
        if artifact_type in _CLOSURE_ARTIFACT_TYPES:
            try:
                full = state.read_artifact(str(record.get("id") or "")) or {}
            except Exception:
                full = {}
            metadata = full.get("metadata") or {}
            if isinstance(metadata, dict) and metadata.get("operation_closure_owner") == _OLD_PROTOCOL_ARTIFACT_OWNER:
                return "old_protocol"
    return "fresh"


@dataclass
class OwnerLock:
    session: str
    pid: int
    host: str
    claimed_at: float

    def to_json(self) -> dict[str, Any]:
        return {"session": self.session, "pid": self.pid, "host": self.host,
                "claimed_at": self.claimed_at, **record_marker()}

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> "OwnerLock":
        return cls(session=str(payload.get("session") or ""), pid=int(payload.get("pid") or 0),
                   host=str(payload.get("host") or ""), claimed_at=float(payload.get("claimed_at") or 0.0))


@dataclass
class Ledger:
    root: Path
    epoch: str
    read_only: bool
    owner: OwnerLock | None
    reason: str = ""
    _seq: int = field(default=0, repr=False)

    # ── 读 ────────────────────────────────────────────────────────────────
    def records(self) -> list[dict[str, Any]]:
        rows, _ = _read_events(self.root / _EVENTS)
        return rows

    def tail_report(self) -> dict[str, Any]:
        _, report = _read_events(self.root / _EVENTS)
        return report

    # ── 写 ────────────────────────────────────────────────────────────────
    def _validate_append(self, kind: str) -> None:
        if self.read_only:
            raise LedgerReadOnly(self.reason or "ledger is read-only for this session")
        if not kind or not isinstance(kind, str):
            raise LedgerError("record kind must be a non-empty string")

    def _append_unlocked(
        self,
        kind: str,
        fields: dict[str, Any],
        *,
        strict_parent_fsync: bool = False,
    ) -> dict[str, Any]:
        """Append one complete line while the caller holds ``events.lock``.

        Same-owner ``Ledger`` objects may coexist after reopen, so the cached
        sequence is never authoritative. Re-read it under the OS lock and
        remove a crash-truncated tail before appending the next complete row.
        """
        path = self.root / _EVENTS
        _truncate_corrupt_tail(path)
        self._seq = _last_seq(path) + 1
        row = {"seq": self._seq, "ts": _now(), "kind": kind, **record_marker(), **fields}
        payload = (json.dumps(row, ensure_ascii=False, default=str) + "\n").encode("utf-8")
        existed = path.exists()
        fd = os.open(str(path), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            view = memoryview(payload)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError("ledger append made no progress")
                view = view[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
        if strict_parent_fsync:
            _fsync_dir_strict(self.root)
        elif not existed:
            _fsync_dir(self.root)
        return row

    def append(self, kind: str, /, **fields: Any) -> dict[str, Any]:
        self._validate_append(kind)
        with exclusive(self.root / _EVENTS_LOCK):
            return self._append_unlocked(kind, fields)

    def append_once(self, kind: str, idempotency_key: str, /,
                    **fields: Any) -> dict[str, Any]:
        """Append exactly one event for a durable idempotency key.

        Replaying identical material re-confirms the event file and parent
        directory durability before returning the first row. Reusing the key
        for different material is corruption, not a new version.
        """
        self._validate_append(kind)
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            # 调用方是框架代码，不是模型：按内部调用错误写，不写成模型可见的字段契约。
            raise LedgerError("ledger append_once needs a non-empty idempotency_key (internal caller error)")
        key = idempotency_key.strip()
        with exclusive(self.root / _EVENTS_LOCK):
            path = self.root / _EVENTS
            _truncate_corrupt_tail(path)
            matches = [row for row in self.records()
                       if row.get("idempotency_key") == key]
            if len(matches) > 1:
                raise LedgerCorrupt(f"duplicate idempotency key: {key}")
            expected = {"kind": kind, "idempotency_key": key, **fields}
            if matches:
                existing = matches[0]
                if any(field not in existing
                       or not _exact_json_equal(existing[field], value)
                       for field, value in expected.items()):
                    raise LedgerCorrupt(f"idempotency conflict: {key}")
                _fsync_file_and_parent(path)
                return existing
            return self._append_unlocked(
                kind, {"idempotency_key": key, **fields},
                strict_parent_fsync=True,
            )

    # ── 投影（纯函数，不持久化）────────────────────────────────────────────
    def project_jobs(self) -> dict[str, dict[str, Any]]:
        return project_jobs(self.records())

    def project_actions(self) -> list[dict[str, Any]]:
        return project_actions(self.records())


def _read_events(path: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    report: dict[str, Any] = {"corrupt_tail_bytes": 0, "lines": 0}
    if not path.exists():
        return rows, report
    data = path.read_bytes()
    if not data:
        return rows, report
    lines = data.split(b"\n")
    tail = lines[-1]
    body = lines[:-1]
    if tail:
        # 最后一段没有换行：写到一半死掉的那条。允许，记下来，忽略。
        report["corrupt_tail_bytes"] = len(tail)
    for index, raw in enumerate(body):
        if not raw.strip():
            continue
        try:
            row = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise LedgerCorrupt(f"ledger line {index + 1} is not valid JSON: {exc}") from exc
        if not is_current_epoch(row):
            raise LedgerCorrupt(f"ledger line {index + 1} lacks the current epoch markers")
        rows.append(row)
    report["lines"] = len(rows)
    return rows, report


def _last_seq(path: Path) -> int:
    rows, _ = _read_events(path)
    return int(rows[-1].get("seq") or len(rows)) if rows else 0


def _read_lock(path: Path) -> OwnerLock | None:
    if not path.exists():
        return None
    try:
        return OwnerLock.from_json(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError):
        return None


def _write_meta(root: Path, state: Any) -> None:
    payload = {"run_id": state.run_id, "created_at": _now(), **record_marker()}
    tmp = root / (_META + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, root / _META)
    _fsync_dir(root)


def bootstrap_phase(state: Any) -> str:
    """'none'（old / foreign / 未开始的 fresh）| 'initializing'（meta 在、ready 不在）| 'ready'。

    meta 只说明 bootstrap **开始**了；最低 A（义务、包络）冻结成功之后才写 ready。
    中途失败的 run 停在 initializing：既不能当普通 v3 writer 继续，也不能退回旧协议
    的 pass-through（Codex 27 号 P0-1）——它需要 runtime 接管修复，不是继续干活。
    """
    if detect_epoch(state) != PROTOCOL_EPOCH:
        return "none"
    return "ready" if (ledger_dir(state) / _READY).exists() else "initializing"


def v3_active(state: Any) -> bool:
    """这个 run 是不是已经**正式**在 v3 协议下（bootstrap 成功建立了最低 A）。

    纯判定，不产生任何副作用。只有 ``bootstrap_phase == "ready"`` 才是 True。
    写入者必须先问它——否则一次 fetch 或 submit 就能把一个旧 harness 的 run 静默
    带进半个 v3。2026-09-23 实测：`record_completed_effect` 曾把 fresh run 直接推成 v3。
    """
    return bootstrap_phase(state) == "ready"


def mark_bootstrap_ready(state: Any) -> None:
    """最低 A 建立成功后由 bootstrap 调用，一次，durable。"""
    root = ledger_dir(state)
    tmp = root / (_READY + ".tmp")
    tmp.write_text(json.dumps({"ready_at": _now(), **record_marker()}), encoding="utf-8")
    os.replace(tmp, root / _READY)
    _fsync_dir(root)


def open_ledger(state: Any, *, write: bool = False, reattach: bool = False,
                bootstrap: bool = False) -> Ledger:
    """打开账本。

    write=False：任何会话都能读（旧协议 run 也能读它没有的账本——返回空）。
    write=True：要成为权威 owner。规则：
      * fresh run：创建 meta，本会话成为 owner；
      * 已有 owner 且是同一会话身份：继续；
      * 已有 owner 是别的会话且其进程仍活着（同主机可判）：只读；
      * 已有 owner 进程已死：只有 reattach=True（人工重开）才接管，否则只读；
      * old_protocol / foreign：永远只读。
    """
    root = ledger_dir(state)
    epoch = detect_epoch(state)
    lock_path = root / _LOCK
    if not write:
        return Ledger(root=root, epoch=epoch, read_only=True, owner=_read_lock(lock_path),
                      reason="opened read-only")
    if epoch in {"old_protocol", "foreign"}:
        return Ledger(root=root, epoch=epoch, read_only=True, owner=_read_lock(lock_path),
                      reason=f"{epoch} run: new protocol never writes old control state")
    if epoch == "fresh" and not bootstrap:
        # 只有 run 启动时的 bootstrap 能开启 v3 纪元；任何 writer 都不行
        # （Codex 25 号 P0-1：协议切换必须原子，不能被首次 fetch/submit 偶然触发）。
        return Ledger(root=root, epoch=epoch, read_only=True, owner=None,
                      reason="run is not on the v3 epoch: only run bootstrap may start it")
    root.mkdir(parents=True, exist_ok=True)
    if epoch == "fresh":
        _write_meta(root, state)
    me = OwnerLock(session=_session_identity(state), pid=os.getpid(), host=socket.gethostname(),
                   claimed_at=_now())
    current = _read_lock(lock_path)
    if current is not None:
        same_session = current.session == me.session
        if not same_session:
            alive = current.host == me.host and _pid_alive(current.pid)
            if alive:
                return Ledger(root=root, epoch=PROTOCOL_EPOCH, read_only=True, owner=current,
                              reason=f"owned by live session {current.session!r} (pid {current.pid})")
            if not reattach:
                return Ledger(root=root, epoch=PROTOCOL_EPOCH, read_only=True, owner=current,
                              reason="previous owner is gone; reattach=True is required to take over")
    tmp = root / (_LOCK + ".tmp")
    tmp.write_text(json.dumps(me.to_json(), ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, lock_path)
    _fsync_dir(root)
    ledger = Ledger(root=root, epoch=PROTOCOL_EPOCH, read_only=False, owner=me)
    recovered = _truncate_corrupt_tail(root / _EVENTS)
    ledger._seq = _last_seq(root / _EVENTS)
    if recovered:
        ledger.append("corrupt_tail_recovered", bytes=recovered)
    return ledger


def _truncate_corrupt_tail(path: Path) -> int:
    """owner 接手时把没有换行的尾段截掉（写到一半死掉的那条），返回截掉的字节数。
    不动任何完整的行；中间坏行由 _read_events fail-loud。"""
    if not path.exists():
        return 0
    data = path.read_bytes()
    if not data or data.endswith(b"\n"):
        return 0
    cut = data.rfind(b"\n") + 1
    tail = len(data) - cut
    with path.open("r+b") as handle:
        handle.truncate(cut)
        handle.flush()
        os.fsync(handle.fileno())
    return tail


def record(state: Any, kind: str, /, **fields: Any) -> dict[str, Any]:
    """写一条记录（要求本会话是 owner）。工具层的记账入口。

    ``state`` 与 ``kind`` 是仅位置参数：记录里合法的字段名包括 ``state``、
    ``kind``、``seq``、``ts``（作业与 action 的状态字段就叫 state），位置参数化
    之后它们不会和函数自己的参数撞名。撞名的后果是静默的——2026-09-23 第三次
    方法预览里 9 次 action 结算因为 ``state=<状态>`` 与位置参数 ``state`` 重名
    全部抛 TypeError、被上层 except 吞掉，账本只剩 9 条开着的 action。
    """
    return open_ledger(state, write=True).append(kind, **fields)


def try_record(state: Any, kind: str, /, **fields: Any) -> dict[str, Any] | None:
    """记账失败不打断工具本身：返回 None 并把原因写进 hook_state 供视图披露。"""
    try:
        return record(state, kind, **fields)
    except LedgerError as exc:
        try:
            state.hook_state.setdefault("ledger_write_failures", []).append(
                {"kind": kind, "reason": str(exc), "ts": _now()})
        except Exception:
            pass
        return None


# ── 投影 ────────────────────────────────────────────────────────────────────

_JOB_KINDS = {"intent", "job_bound", "job_terminal", "job_cancelled", "job_reconciled", "job_unknown", "job_absent"}


def project_jobs(records: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """按 correlation key（nonce）汇总作业：最新状态、外部句柄、终态原文。"""
    jobs: dict[str, dict[str, Any]] = {}
    for row in records:
        kind = row.get("kind")
        if kind not in _JOB_KINDS:
            continue
        key = str(row.get("nonce") or row.get("correlation_key") or "")
        if not key:
            continue
        job = jobs.setdefault(key, {"nonce": key, "state": "intent", "external_id": None,
                                     "scheduler": row.get("scheduler"), "history": []})
        job["history"].append({"seq": row.get("seq"), "kind": kind})
        if kind == "intent":
            job["state"] = "intent"
            job["output_root"] = row.get("output_root")
            job["scheduler"] = row.get("scheduler") or job.get("scheduler")
        elif kind == "job_bound":
            job["state"] = "submitted"
            job["external_id"] = row.get("external_id")
        elif kind == "job_terminal":
            job["state"] = "terminal"
            job["terminal"] = {k: row.get(k) for k in ("derived_state", "exit_code", "raw") if k in row}
        elif kind == "job_cancelled":
            job["state"] = "terminal"
            job["terminal"] = {"derived_state": "cancelled", "raw": row.get("raw")}
        elif kind == "job_reconciled":
            job["state"] = str(row.get("state") or "submitted")
            job["external_id"] = row.get("external_id") or job.get("external_id")
        elif kind == "job_unknown":
            job["state"] = "unknown"
            job["unknown_reason"] = row.get("reason")
        elif kind == "job_absent":
            # 确定没进队列（adapter 正向查过）或提交前就失败：intent 关闭，可重提（Codex 41 P0）
            job["state"] = "absent"
            job["absent_reason"] = row.get("reason")
    return jobs


def open_jobs(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return [job for job in project_jobs(records).values() if job["state"] in {"intent", "submitted", "unknown"}]


def project_output_observations(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """One view of local observations and outputs held by the job's terminal fact.

    A managed job is not a second local action. Its stable producer handle is a
    projection of its nonce, not another lifecycle or an invented exit code.
    """
    observations = []
    capture_directories: dict[str, Path] = {}
    known_paths: set[str] = set()
    for row in records:
        if row.get("kind") == "action_started" and row.get("capture_record"):
            capture_directories[str(row.get("action_id"))] = Path(row["capture_record"])
        if row.get("kind") == "observation":
            observation = row
        elif row.get("kind") == "job_terminal" and isinstance(row.get("output_diff"), dict):
            observation = {**row["output_diff"], "kind": "observation",
                           "action_id": row.get("output_observation"),
                           "source_kind": "job_terminal", "nonce": row.get("nonce"), "seq": row.get("seq")}
            if row.get("capture_record"):
                capture_directories[str(row.get("output_observation"))] = Path(row["capture_record"])
        else:
            continue
        if "observation_ref" in observation:
            try:
                from .capture import read_observation
                action_id = str(observation.get("action_id"))
                complete = read_observation(capture_directories[action_id], observation["observation_ref"], action_id)
                observation = {**observation, **complete}
            except (OSError, ValueError, KeyError, TypeError) as exc:
                # Lost details cannot make the 200-row summary authoritative.
                # Nor can an earlier producer survive an unobserved rewrite in
                # this same boundary. Other actions/roots remain unaffected.
                boundary = observation.get("observation_boundary") or {}
                uncertain = set(observation.get("created") or []) | set(observation.get("modified") or [])
                uncertain.update(observation.get("unknown") or [])
                for path in known_paths:
                    if any(Path(path).is_relative_to(Path(r)) for r in boundary.get("roots", [])) and not any(
                            Path(path).is_relative_to(Path(r)) for r in boundary.get("excluded_roots", [])):
                        uncertain.add(path)
                observation = {**observation, "created": [], "modified": [], "identities": {},
                               "unknown": sorted(uncertain), "truncated": True,
                               "observation_error": f"{type(exc).__name__}: {exc}"}
        known_paths.update(observation.get("created") or [])
        known_paths.update(observation.get("modified") or [])
        known_paths.update(observation.get("unknown") or [])
        observations.append(observation)
    return observations


def project_actions(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """按账本中首次出现的顺序汇总 action，身份字符串不承载时间顺序。"""
    actions: dict[str, dict[str, Any]] = {}
    for row in records:
        kind = row.get("kind")
        if kind not in {"action_started", "action_settled", "observation", "effect"}:
            continue
        key = str(row.get("action_id") or "")
        if not key:
            continue
        action = actions.setdefault(key, {"action_id": key, "state": "started", "tool": row.get("tool"),
                                          "namespace": row.get("namespace")})
        if kind == "action_started":
            action.update({"state": "started", "tool": row.get("tool"), "command": row.get("command"),
                           "namespace": row.get("namespace"), "work_root": row.get("work_root"),
                           "predecessor": row.get("predecessor")})
            if "execution_context" in row:
                action["execution_context"] = row["execution_context"]
        elif kind == "action_settled":
            action.update({"state": str(row.get("state") or "settled"), "exit_code": row.get("exit_code"),
                           "duration_s": row.get("duration_s")})
        elif kind == "observation":
            action["observation"] = {k: row.get(k) for k in ("created", "modified", "unchanged", "deleted",
                                                           "truncated", "identity_basis", "unknown",
                                                           "overlapping_actions", "coverage", "scan_errors",
                                                           "observation_boundary", "summary_truncated",
                                                           "observation_counts", "observation_ref") if k in row}
        elif kind == "effect":
            action.setdefault("effects", []).append({k: row.get(k) for k in ("tool", "path", "artifact_id",
                                                                             "artifact_type", "bytes") if k in row})
    # Dict insertion order follows the canonical event stream. Random action
    # identifiers cannot tell whether a success happened before or after a
    # failure; sorting them can both reject real retries and hide later failures.
    return list(actions.values())

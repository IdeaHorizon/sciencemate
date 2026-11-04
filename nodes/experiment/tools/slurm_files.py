"""File staging and verified replicas for the existing SSH Slurm job record.

Remote coordinates never pass through Windows Path. Transfers do not create
jobs or infer scientific success; receipts refer to the original terminal
observation in the existing ledger. No archive extraction or remote service.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

from . import capture, ledger, persistence, slurm_ssh
from .path_roles import experiment_output_dir

DEFAULT_MAX_BYTES = 256 * 1024 * 1024
_IDENTITY_FIELDS = ("st_dev", "st_ino", "size_bytes", "mtime_ns", "ctime_ns")


def _argv(action: str, data: dict[str, Any]) -> list[str]:
    source = Path(__file__).with_name("slurm_files_remote.py").read_text(encoding="utf-8")
    return ["python3", "-c", source, action, json.dumps(data, ensure_ascii=True, separators=(",", ":"))]


def _call(target: dict, action: str, data: dict, *, source=None, timeout=30, expected_identity=None) -> dict:
    if expected_identity is not None:
        data = {**data, "expected_identity": expected_identity}
    if source is None:
        result = slurm_ssh.run(target, _argv(action, data), timeout=timeout)
    else:
        result = slurm_ssh.run_stream(target, _argv(action, data), stdin_file=source, timeout=timeout)
    if not result.get("ok"):
        raise OSError(result.get("error") or result.get("stderr") or "remote file operation failed")
    decoded = json.loads(result.get("stdout") or "")
    if not isinstance(decoded, dict):
        raise ValueError("invalid remote file response")
    return decoded


def _within(path: str, root: str) -> bool:
    return PurePosixPath(path).is_relative_to(PurePosixPath(root))


def _roots(record: dict) -> list[str]:
    values = [*(record.get("output_roots") or []), record.get("scheduler_output_dir"),
              record.get("bootstrap_log_dir")]
    return list(dict.fromkeys(slurm_ssh.normalize_path(p) for p in values if p))


def _read_path(record: dict, value: str) -> str:
    path = slurm_ssh.normalize_path(value, base=record.get("workdir"))
    if any(part in {".git", ".ssh", ".research"} for part in PurePosixPath(path).parts):
        raise ValueError("protected remote metadata is not a job output")
    if not any(_within(path, root) for root in _roots(record)):
        raise ValueError("remote read is outside this job's recorded output roots")
    return path


def _component(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9_.-]{0,119}", value):
        raise ValueError("invalid job path component")
    return value


def bootstrap_path(nonce: str, job_name: str, output_dir: str) -> str:
    return str(PurePosixPath(slurm_ssh.normalize_path(output_dir)) /
               f".harness-bootstrap-{_component(nonce)}_{_component(job_name)}")


def remote_stage_in(stage_in: list[dict] | None, bootstrap_dir: str) -> list[dict]:
    root = PurePosixPath(slurm_ssh.normalize_path(bootstrap_dir)) / "inputs"
    return [{**row, "src": str(root / str(i))} for i, row in enumerate(stage_in or [])]


def check_paths(target: dict, workdir: str, output_dir: str, *, expected_identity=None) -> dict:
    try:
        result = _call(target, "check", {"workdir": slurm_ssh.normalize_path(workdir),
                                          "output_dir": slurm_ssh.normalize_path(output_dir)}, expected_identity=expected_identity)
        return {**result, "ok": True, "workdir": slurm_ssh.normalize_path(result["workdir"]),
                "output_dir": slurm_ssh.normalize_path(result["output_dir"])}
    except (OSError, ValueError, TypeError, KeyError) as exc:
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}


def _open_source(path: Path):
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode):
        raise ValueError("stage source must be a regular file, not a link")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0))
    stream = os.fdopen(fd, "rb")
    after = os.fstat(fd)
    if not stat.S_ISREG(after.st_mode) or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
        stream.close()
        raise ValueError("stage source changed while opening")
    return stream


def _digest(stream) -> str:
    digest = hashlib.sha256()
    for block in iter(lambda: stream.read(1024 * 1024), b""):
        digest.update(block)
    stream.seek(0)
    return digest.hexdigest()


def prepare_submission(state: Any, target: dict, *, nonce: str, job_name: str, workdir: str,
                       output_dir: str, stage_in: list[dict] | None, script_path: Path,
                       remote_user: str | None = None, scheduler_cluster: str | None = None) -> dict:
    """Called after durable intent, before submission_attempted or sbatch."""
    boot = bootstrap_path(nonce, job_name, output_dir)
    try:
        expected = _expected({"remote_user": remote_user, "scheduler_cluster": scheduler_cluster})
        checked = check_paths(target, workdir, output_dir, expected_identity=expected)
        if not checked.get("ok"):
            raise OSError(checked.get("error", "remote directories unavailable"))
        if checked["workdir"] != workdir or checked["output_dir"] != output_dir:
            raise ValueError("remote directory resolved differently from the approved coordinates")
        _call(target, "prepare", {"bootstrap": boot}, expected_identity=expected)
        uploads = [(Path(row["src"]), str(PurePosixPath(boot) / "inputs" / str(i)),
                    row.get("sha256"), row.get("mode", "0644")) for i, row in enumerate(stage_in or [])]
        remote_script = str(PurePosixPath(boot) / "job.sh")
        uploads.append((Path(script_path), remote_script, None, "0700"))
        for path, destination, approved_hash, mode in uploads:
            with _open_source(path) as source:
                digest = _digest(source)
                if approved_hash is not None and digest != approved_hash:
                    raise ValueError("stage input changed after approval")
                _call(target, "upload", {"path": destination, "size": os.fstat(source.fileno()).st_size,
                                          "sha256": digest, "mode": str(mode)}, source=source, timeout=120,
                      expected_identity=expected)
        return {"ok": True, "bootstrap_log_dir": boot, "remote_script_path": remote_script}
    except (OSError, ValueError, TypeError, KeyError) as exc:
        return {"ok": False, "bootstrap_log_dir": boot, "error": f"{type(exc).__name__}: {exc}",
                "job_submitted": False, "staging_may_exist": True}


def snapshot(target: dict, roots: list[str], *, expected_identity=None) -> dict:
    roots = list(dict.fromkeys(slurm_ssh.normalize_path(p) for p in roots))
    try:
        result = _call(target, "snapshot", {"roots": roots}, timeout=45, expected_identity=expected_identity)
        if not isinstance(result.get("entries"), dict) or not isinstance(result.get("root_coverage"), list):
            raise ValueError("malformed remote snapshot")
        for path, row in result["entries"].items():
            if slurm_ssh.normalize_path(path) != path or not any(_within(path, r) for r in roots):
                raise ValueError("remote snapshot escaped its requested roots")
            if not isinstance(row, dict):
                raise ValueError("invalid remote file identity")
        for row in result["root_coverage"]:
            path = slurm_ssh.normalize_path(row["root"])
            if not any(_within(path, r) for r in roots):
                raise ValueError("remote coverage escaped its requested roots")
        return result
    except (OSError, ValueError, TypeError, KeyError) as exc:
        return {"entries": {}, "root_coverage": [{"root": r, "status": "error"} for r in roots],
                "truncated": True, "snapshot_error": f"{type(exc).__name__}: {exc}"}


def health_snapshot(record: dict, path: str) -> dict:
    try:
        path = _read_path(record, path)
        return _call(record["slurm_target"], "health", {"path": path}, expected_identity=_expected(record))
    except (OSError, ValueError, TypeError, KeyError) as exc:
        return {"path": path, "kind": "error", "exists": False, "error": f"{type(exc).__name__}: {exc}"}


def output_identity(record: dict, path: str) -> dict:
    try:
        path = _read_path(record, path)
        return _call(record["slurm_target"], "identity", {"path": path}, timeout=45, expected_identity=_expected(record))
    except (OSError, ValueError, TypeError, KeyError) as exc:
        return {"path": path, "kind": "error", "error": f"{type(exc).__name__}: {exc}"}


def tail(record: dict, path: str, limit: int = 8192) -> str:
    try:
        path = _read_path(record, path)
        return str(_call(record["slurm_target"], "tail", {"path": path, "limit": limit},
                         expected_identity=_expected(record)).get("text", ""))
    except (OSError, ValueError, TypeError, KeyError):
        return ""


def _absence(snapshot: dict, path: str) -> bool:
    coverage = [r for r in snapshot.get("root_coverage", []) if _within(path, r["root"])]
    if not coverage:
        return False
    closest = max(coverage, key=lambda r: len(PurePosixPath(r["root"]).parts))
    return closest.get("status") in {"complete", "missing"}


def remote_diff(before: dict, after: dict) -> dict:
    """Reuse capture comparison with explicit, case-sensitive POSIX absences."""
    copies = [{**snap, "entries": dict(snap.get("entries") or {})} for snap in (before, after)]
    keys = set(copies[0]["entries"]) | set(copies[1]["entries"])
    for snap in copies:
        for key in keys - set(snap["entries"]):
            snap["entries"][key] = {"kind": "missing" if _absence(snap, key) else "error"}
    before, after = copies
    changes = capture.diff(before, after, limit=None)
    identities = {p: {"before": before["entries"].get(p), "after": after["entries"].get(p),
                       "basis": changes.get("identity_basis", {}).get(p)}
                  for p in [*changes["created"], *changes["modified"]]}
    uncertain = {p for p, row in identities.items() if row["basis"] == "hash_unavailable"}
    for field in ("created", "modified"):
        changes[field] = [p for p in changes[field] if p not in uncertain]
    changes["unknown"] = list(dict.fromkeys([*changes["unknown"], *sorted(uncertain)]))
    return {**changes, "identities": {p: r for p, r in identities.items() if p not in uncertain},
            "coordinate_space": "slurm_remote", "remote_snapshot": after,
            "status": "partial" if changes["truncated"] or changes["unknown"] else "observed",
            "observed_at": after.get("taken_at"), "root_coverage": after.get("root_coverage", []),
            "baseline_root_coverage": before.get("root_coverage", []),
            "limitation": "Remote changes describe the submission-to-observation window, not an exclusive writer or physical exit instant. Files without a frozen hash use metadata identity."}


def _frozen_observation(terminal: dict) -> dict:
    observation = terminal.get("output_diff") or {}
    if "observation_ref" in observation:
        return capture.read_observation(Path(terminal["capture_record"]), observation["observation_ref"],
                                        terminal["output_observation"])
    return observation


def _expected(record: dict) -> dict:
    if not record.get("remote_user") or not record.get("scheduler_cluster"):
        raise ValueError("remote user/cluster is missing from the recorded job")
    return {"user": record["remote_user"], "cluster": record["scheduler_cluster"]}


def _verify_target(record: dict) -> None:
    probe = slurm_ssh.probe(record["slurm_target"])
    if (not probe.get("ok") or not record.get("remote_user") or not record.get("scheduler_cluster")
            or probe.get("user") != record["remote_user"] or probe.get("cluster") != record["scheduler_cluster"]):
        raise ValueError("remote user/cluster differs from the recorded job or cannot be verified")


def _mirror_root(state: Any, nonce: str) -> Path:
    base = experiment_output_dir(state, "runtime", create=True)
    path = base / "returned" / _component(nonce)
    for parent in (base, base / "returned", path):
        parent.mkdir(exist_ok=True)
        if parent.is_symlink() or (hasattr(parent, "is_junction") and parent.is_junction()):
            raise ValueError("local return directory must not be a link or junction")
        persistence.fsync_directory(parent.parent)
    if not path.resolve().is_relative_to(base.resolve()):
        raise ValueError("local return directory escaped the runtime root")
    return path


def _receipt(state: Any, terminal: dict, remote_path: str, remote_identity: dict,
             destination: Path, digest: str) -> dict:
    # A digest and a later path stat can describe different files. Freeze the
    # identity of the same open object whose bounded bytes were verified,
    # including large replicas for which capture normally omits a hash.
    visible_before = destination.lstat()
    with _open_source(destination) as stream:
        before = os.fstat(stream.fileno())
        if before.st_size != remote_identity.get("size_bytes"):
            raise OSError("returned file changed before its receipt")
        observed, remaining = hashlib.sha256(), before.st_size
        while remaining:
            block = stream.read(min(1024 * 1024, remaining))
            if not block:
                raise OSError("returned file truncated before its receipt")
            observed.update(block)
            remaining -= len(block)
        after = os.fstat(stream.fileno())
        visible = destination.lstat()
        fields = lambda st: (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)
        # Compare ctime within each API: Windows fstat and lstat can expose
        # different timestamp semantics for the same unchanged file.
        if (observed.hexdigest() != digest or fields(after) != fields(before)
                or after.st_ctime_ns != before.st_ctime_ns
                or fields(visible) != fields(before) or fields(visible_before) != fields(visible)
                or visible.st_ctime_ns != visible_before.st_ctime_ns
                or not stat.S_ISREG(visible.st_mode)):
            raise OSError("returned file identity/content changed before its receipt")
        local_identity = {"kind": "file", "size": before.st_size, "inode": before.st_ino,
                          "mtime_ns": before.st_mtime_ns}
        if before.st_size <= capture.SMALL_FILE_BYTES:
            local_identity["sha256"] = digest
    identity_key = hashlib.sha256(json.dumps([str(destination), local_identity, digest], sort_keys=True).encode()).hexdigest()
    return ledger.open_ledger(state, write=True).append_once(
        "job_output_collected", f"job_output_collected:{terminal['nonce']}:{identity_key}",
        nonce=terminal["nonce"], terminal_seq=terminal["seq"], remote_path=remote_path,
        remote_identity=remote_identity, content_sha256=digest, local_path=str(destination),
        local_identity=local_identity,
        verification_basis="frozen_sha256" if remote_identity.get("sha256") else "frozen_metadata_and_transfer_sha256")


def collect_outputs(state: Any, record: dict, *, max_bytes: int = DEFAULT_MAX_BYTES) -> dict:
    """Retry file transport only; consume the same immutable terminal snapshot."""
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
        return {"ok": False, "error": "max_output_bytes must be a positive integer"}
    nonce = str(record.get("submission_nonce") or "")
    report: dict[str, Any] = {"ok": False, "files": [], "bytes_transferred": 0,
                              "retained_remote": [], "errors": [], "submission_nonce": nonce}
    try:
        rows = ledger.open_ledger(state).records()
        terminals = [r for r in rows if r.get("kind") == "job_terminal" and r.get("nonce") == nonce]
        if len(terminals) != 1:
            raise ValueError("no unique frozen terminal observation; query the original job first")
        terminal = terminals[0]
        if (terminal.get("slurm_target") != record.get("slurm_target")
                or _expected(terminal) != _expected(record)
                or str(terminal.get("external_id")) != str(record.get("job_id"))):
            raise ValueError("collection target differs from the frozen terminal job")
        observation = _frozen_observation(terminal)
        if observation.get("coordinate_space") != "slurm_remote":
            raise ValueError("this terminal does not describe SSH output")
        _verify_target(record)
        root = _mirror_root(state, nonce)
        report["local_root"] = str(root)
        entries = (observation.get("remote_snapshot") or {}).get("entries") or {}
        candidates = set(observation.get("created") or []) | set(observation.get("modified") or []) | set(observation.get("unknown") or [])
        remaining = max_bytes
        # Every returned file remains available for inspection; only changes
        # proven in the original terminal window qualify as result producers.
        for remote_path, ident in sorted(entries.items()):
            if ident.get("kind") != "file" or remote_path not in candidates:
                continue
            try:
                _read_path(record, remote_path)
                size = ident.get("size_bytes")
                if (isinstance(size, bool) or not isinstance(size, int) or size < 0
                        or any(not isinstance(ident.get(k), int) for k in _IDENTITY_FIELDS)):
                    raise ValueError("incomplete frozen remote file identity")
                suffix = re.sub(r"[^A-Za-z0-9_.-]", "_", PurePosixPath(remote_path).name)[-60:] or "output"
                destination = root / (hashlib.sha256(remote_path.encode()).hexdigest()[:24] + "_" + suffix)
                prior = [r for r in rows if r.get("kind") == "job_output_collected" and r.get("nonce") == nonce
                         and r.get("remote_path") == remote_path and r.get("local_path") == str(destination)]
                existing_digest = None
                if destination.exists() or destination.is_symlink():
                    with _open_source(destination) as stream:
                        digest = _digest(stream)
                    expected = ident.get("sha256") or (prior[-1].get("content_sha256") if prior else None)
                    if (expected is not None and expected != digest) or destination.stat().st_size != size:
                        raise ValueError("existing local mirror differs; preserve it and resolve the conflict before retry")
                    if expected is not None:
                        persistence.fsync_file(destination, nofollow=True)
                        persistence.fsync_directory(root)
                        receipt = _receipt(state, terminal, remote_path, ident, destination, digest)
                        report["files"].append({"remote_path": remote_path, "local_path": str(destination),
                                                "sha256": digest, "reused": True, "receipt_seq": receipt["seq"]})
                        continue
                    # The process may have stopped after publication but before
                    # the receipt. Without a frozen hash, re-fetch the same
                    # metadata-bound remote file and compare; never bless a
                    # guessed digest or overwrite a conflicting local file.
                    existing_digest = digest
                if size > remaining:
                    report["retained_remote"].append({"path": remote_path, "size_bytes": size, "reason": "transfer_budget"})
                    continue
                fd, temporary = tempfile.mkstemp(prefix=".incoming-", dir=root)
                try:
                    with os.fdopen(fd, "w+b") as stream:
                        result = slurm_ssh.run_stream(record["slurm_target"], _argv("fetch", {"path": remote_path, "identity": ident,
                                                      "expected_identity": _expected(record)}),
                                                      stdout_file=stream, timeout=120, max_output_bytes=size + 1)
                        if not result.get("ok"):
                            raise OSError(result.get("error") or result.get("stderr") or "remote transfer failed")
                        if stream.tell() != size:
                            raise ValueError("returned file size mismatch")
                        stream.seek(0)
                        digest = _digest(stream)
                        if ident.get("sha256") and digest != ident["sha256"]:
                            raise ValueError("returned file hash mismatch")
                        stream.flush()
                        os.fsync(stream.fileno())
                    # A hash-derived owned filename, with no archive members or
                    # remote spelling interpreted as a Windows destination.
                    if existing_digest is not None:
                        with _open_source(destination) as existing:
                            if digest != existing_digest or _digest(existing) != digest:
                                raise ValueError("existing local mirror differs from the verified remote bytes")
                        persistence.fsync_file(destination, nofollow=True)
                    else:
                        if destination.exists() or destination.is_symlink():
                            raise ValueError("local mirror appeared during transfer")
                        if os.name == "nt":
                            # Windows rename fails when the destination exists.
                            os.rename(temporary, destination)
                        else:
                            # POSIX rename replaces; link publishes without an
                            # overwrite window. Unsupported filesystems fail
                            # truthfully and retain the remote output.
                            os.link(temporary, destination)
                            os.unlink(temporary)
                    persistence.fsync_directory(root)
                    receipt = _receipt(state, terminal, remote_path, ident, destination, digest)
                    remaining -= size
                    report["bytes_transferred"] += size
                    report["files"].append({"remote_path": remote_path, "local_path": str(destination),
                                            "sha256": digest, "reused": False, "receipt_seq": receipt["seq"]})
                finally:
                    if os.path.exists(temporary):
                        os.unlink(temporary)
            except (OSError, ValueError, TypeError, KeyError, ledger.LedgerError) as exc:
                report["errors"].append({"path": remote_path, "error": f"{type(exc).__name__}: {exc}"})
        report["snapshot_truncated"] = bool((observation.get("remote_snapshot") or {}).get("truncated"))
        report["ok"] = not report["errors"] and not report["retained_remote"] and not report["snapshot_truncated"]
        report["status"] = "complete" if report["ok"] else "partial"
        report["next_action"] = None if report["ok"] else (
            "Retry job_status with collect_outputs for transport errors, or raise max_output_bytes for retained files. "
            "The original job is not resubmitted."
        )
        if report["snapshot_truncated"]:
            report["snapshot_limitation"] = (
                "The terminal snapshot was bounded; collection retry cannot reconstruct omitted files at that earlier instant. "
                "Inspect retained remote files separately and keep their original production identity unknown."
            )
            if not report["errors"] and not report["retained_remote"]:
                report["next_action"] = report["snapshot_limitation"]
    except (OSError, ValueError, TypeError, KeyError, ledger.LedgerError) as exc:
        report["errors"].append({"error": f"{type(exc).__name__}: {exc}"})
        report["status"] = "unavailable"
    return report

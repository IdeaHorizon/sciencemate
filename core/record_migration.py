"""One-way import of envelope records into native files and their Git history.

The platform calls this while it owns the project lock and no worker writes the
worktree. Preparation and verification happen in a detached worktree; the final
change is a fast-forward. Original envelopes remain reachable through a backup
ref. Normal record readers never parse an envelope after this operation.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
import re
import tempfile
from uuid import uuid4

from core.ledger import FREEZE_OWNED_METADATA, LEDGER_RELATIVE, extension_for, workspace_store
from core.project_workspace import _NODE_WORKSPACES, _git as _workspace_git

MANIFEST_PATH = ".research/migrations/native-records-v1.json"


class RecordMigrationError(RuntimeError):
    pass


def _git(root: Path, *args: str, check: bool = True) -> str:
    return _workspace_git(root, *args, check=check).strip()


def _digest(raw: bytes) -> str:
    return sha256(raw).hexdigest()


def _record(raw: bytes, source: str) -> dict:
    try:
        record = json.loads(raw)
        if not isinstance(record, dict) or not isinstance(record.get("content"), str):
            raise ValueError("not a research record envelope")
        if not isinstance(record.get("type"), str) or not record["type"]:
            raise ValueError("missing record type")
        metadata = record.get("metadata") or {}
        if isinstance(metadata, str):
            metadata = json.loads(metadata)
        if not isinstance(metadata, dict):
            raise ValueError("invalid metadata")
        record["metadata"] = metadata
        version = record.get("version", 1)
        if type(version) is not int or version < 1:
            raise ValueError("invalid version")
        record["version"] = version
        if record.get("content_hash") and record["content_hash"] != _digest(record["content"].encode()):
            raise ValueError("body checksum mismatch")
        return record
    except (ValueError, TypeError, UnicodeError) as exc:
        raise RecordMigrationError(f"Cannot migrate {source}: {exc}") from exc


@dataclass
class LegacyRecord:
    artifact_id: str
    directory: str
    versions: list[dict]
    paths: list[str]
    retired: bool

    @property
    def native_path(self) -> str:
        head = self.versions[-1]
        return f"{self.directory}/{self.artifact_id}{extension_for(head['type'], head['content'])}"


def inspect_legacy_records(root: Path) -> tuple[list[LegacyRecord], dict[str, str]]:
    """Validate all available versions and frozen envelopes without writing."""
    root = root.resolve()
    records: dict[str, LegacyRecord] = {}
    sources: dict[str, str] = {}
    for directory in sorted(root.glob("*/artifacts")):
        if directory.is_symlink() or directory.parent.name in {".git", ".research"}:
            raise RecordMigrationError(f"Unexpected legacy record directory: {directory}")
        heads = {path.stem: path for path in directory.glob("*.json")}
        snapshots: dict[str, list[Path]] = {}
        for path in (directory / ".versions").glob("*.json"):
            match = re.fullmatch(r"(.+)@v([1-9][0-9]*)", path.stem)
            if not match:
                raise RecordMigrationError(f"Unrecognized version snapshot: {path}")
            snapshots.setdefault(match[1], []).append(path)
        for artifact_id in sorted(set(heads) | set(snapshots)):
            if artifact_id in records:
                raise RecordMigrationError(f"Ambiguous record identity across directories: {artifact_id}")
            paths = [*sorted(snapshots.get(artifact_id, [])), *([heads[artifact_id]] if artifact_id in heads else [])]
            versions: dict[int, dict] = {}
            aliases = []
            for path in paths:
                relative = path.relative_to(root).as_posix()
                if path.is_symlink() or not path.resolve().is_relative_to(root):
                    raise RecordMigrationError(f"Record escapes its worktree: {relative}")
                raw = path.read_bytes()
                record = _record(raw, relative)
                if "@v" in path.stem and int(path.stem.rsplit("@v", 1)[1]) != record["version"]:
                    raise RecordMigrationError(f"Snapshot version does not match envelope: {relative}")
                existing = versions.get(record["version"])
                if existing and existing["content"] != record["content"]:
                    raise RecordMigrationError(f"Conflicting bodies for one version: {relative}")
                versions[record["version"]] = record
                sources[relative] = _digest(raw)
                aliases.append(relative)
            highest = max(versions)
            # Recover older declared versions from the branch's own Git history.
            old_path = f"{directory.relative_to(root).as_posix()}/{artifact_id}.json"
            if len(versions) != highest:
                for commit in _git(root, "log", "--format=%H", "HEAD", "--", old_path).splitlines():
                    raw = _git(root, "show", f"{commit}:{old_path}", check=False)
                    if raw:
                        prior = _record(raw.encode(), f"{commit}:{old_path}")
                        versions.setdefault(prior["version"], prior)
                missing = sorted(set(range(1, highest + 1)) - versions.keys())
                if missing:
                    raise RecordMigrationError(f"Missing historical versions for {artifact_id}: {missing}; restore them from backup before migrating")
            owner = directory.parent.name
            owner = _NODE_WORKSPACES.get(owner, owner)
            if Path(owner).suffix:
                owner = "notes"
            record = LegacyRecord(artifact_id, owner, [versions[v] for v in sorted(versions)], aliases, artifact_id not in heads)
            if (root / record.native_path).exists():
                raise RecordMigrationError(f"Migration would overwrite an existing native file: {record.native_path}")
            records[artifact_id] = record
        register = directory / ".frozen.jsonl"
        if register.exists():
            previous = None
            pinned: dict[str, str] = {}
            for line in register.read_bytes().splitlines():
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                    if row.get("prev_row_sha256") != previous:
                        raise ValueError("frozen register hash chain mismatch")
                    previous = _digest(line)
                    action = row.get("action", "freeze")
                    if action == "freeze":
                        pinned[row["path"]] = row["sha256"]
                    elif action in {"amend", "migrate"}:
                        pinned.pop(row["path"], None)
                        pinned[row["snapshot_path"]] = row["snapshot_sha256"]
                    else:
                        raise ValueError(f"unknown frozen register action {action!r}")
                except (ValueError, KeyError, TypeError) as exc:
                    raise RecordMigrationError(f"Invalid frozen register {register}: {exc}") from exc
            for path, expected in pinned.items():
                candidate = Path(path)
                if candidate.is_absolute():
                    # Old local roots can move between installations; the ledger
                    # path must still identify exactly one inventoried record.
                    matches = [name for name in sources if candidate.as_posix().endswith('/' + name)]
                    if len(matches) != 1:
                        raise RecordMigrationError(f"Unresolvable frozen path: {path}")
                    path = matches[0]
                if sources.get(path) != expected:
                    raise RecordMigrationError(f"Frozen envelope checksum mismatch: {path}")
            sources[register.relative_to(root).as_posix()] = _digest(register.read_bytes())
    if sources and (root / LEDGER_RELATIVE).exists():
        # 混合工作区：原生账本已经在记事，旧信封却还没转进来。这条判据以前
        # 只长在 `migrate_records` 里 —— 于是 `--preview` 报一切正常，真跑
        # 才被拒。检查放在两条路共用的这一处：预览看见的就是迁移会看见的。
        raise RecordMigrationError(
            "Native and legacy ledgers coexist; resolve this mixed workspace before migration")
    return list(records.values()), sources


def migrate_records(root: Path | str, *, checkpoint_pending: bool = False) -> dict:
    """Migrate under the platform's exclusive project lock, with a backup ref."""
    root = Path(root).resolve()
    records, sources = inspect_legacy_records(root)
    if not sources:
        manifest = root / MANIFEST_PATH
        return json.loads(manifest.read_text(encoding="utf-8")) if manifest.exists() else {"migrated": False}
    original_head = _git(root, "rev-parse", "HEAD")
    tracked = set(_git(root, "ls-files").splitlines())
    pending = bool(_git(root, "status", "--porcelain", "--untracked-files=normal")) or not sources.keys() <= tracked
    if pending and not checkpoint_pending:
        raise RecordMigrationError("The project has pending changes. Stop its worker and checkpoint them before migrating (or use --checkpoint-pending)")
    # A named backup points to the complete pre-upgrade state, including pending
    # research work. Nothing is discarded or force-pushed to a shared branch.
    if pending:
        _git(root, "add", "--all")
        _git(root, "add", "--force", "--", *sources)
        _git(root, "commit", "-m", "migration: checkpoint pending research before native records")
    backup_head = _git(root, "rev-parse", "HEAD")
    backup_ref = f"refs/research-migrations/native-records-v1/{uuid4().hex}"
    _git(root, "update-ref", backup_ref, backup_head)
    report = {"migrated": True, "format": 1, "original_head": original_head,
              "backup_ref": backup_ref, "backup_commit": backup_head,
              "records": [], "source_sha256": sources}
    with tempfile.TemporaryDirectory(prefix="science-record-migration-") as temporary:
        staged = Path(temporary) / "worktree"
        _git(root, "worktree", "add", "--detach", str(staged), backup_head)
        try:
            # Content hashes identify exact bytes. Git's user-level autocrlf
            # policy must never rewrite a migrated scientific body on checkout.
            attributes = staged / ".gitattributes"
            existing_attributes = attributes.read_bytes() if attributes.exists() else b""
            protected_paths = [record.native_path for record in records] + [LEDGER_RELATIVE]
            exact_bytes = "\n# Scientific record hashes refer to exact bytes.\n" + "".join(
                json.dumps(str(path), ensure_ascii=False) + " -text\n" for path in protected_paths)
            attributes.write_bytes(existing_attributes + exact_bytes.encode("utf-8"))
            _git(staged, "add", "--", ".gitattributes")
            store = workspace_store(staged)
            for record in records:
                relative = record.native_path
                target = staged / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                for envelope in record.versions:
                    content = envelope["content"]
                    metadata = {k: v for k, v in envelope["metadata"].items() if k not in FREEZE_OWNED_METADATA}
                    target.write_bytes(content.encode("utf-8"))
                    row = {"event": "save", "id": record.artifact_id, "path": relative,
                           "type": envelope["type"], "name": envelope.get("name", record.artifact_id),
                           "version": envelope["version"], "sha256": _digest(content.encode()),
                           "created_at": envelope.get("created_at", ""),
                           "provenance": envelope.get("provenance") or {},
                           "produced_by_node_type": envelope.get("produced_by_node_type", ""),
                           "produced_by_run_id": envelope.get("produced_by_run_id", ""),
                           "metadata": metadata, "by_node": "platform", "by_run": "native-record-migration"}
                    if envelope.get("prev_content_hash"):
                        row["prev_sha256"] = envelope["prev_content_hash"]
                    if envelope.get("amendment"):
                        row["amendment"] = envelope["amendment"]
                    store._append(row)
                    if envelope["metadata"].get("frozen"):
                        stamp = envelope["metadata"].get("frozen_at") or envelope.get("created_at")
                        if not stamp:
                            raise RecordMigrationError(f"Frozen record has no timestamp: {record.artifact_id}")
                        store.freeze(record.artifact_id, by_node=row["produced_by_node_type"],
                                     by_run=row["produced_by_run_id"], frozen_at=stamp,
                                     metadata_patch={key: envelope["metadata"][key] for key in FREEZE_OWNED_METADATA if key in envelope["metadata"]})
                    _git(staged, "add", "--", relative, LEDGER_RELATIVE)
                    _git(staged, "commit", "-m", f"migration: preserve {record.artifact_id} v{envelope['version']}")
                if record.retired:
                    target.unlink()
                    store._append({"event": "retire", "id": record.artifact_id, "path": relative,
                                   "version": record.versions[-1]["version"], "reason": "No head existed in the source workspace"})
                report["records"].append({"id": record.artifact_id, "path": relative,
                    "previous_paths": record.paths, "retired": record.retired,
                    "versions": [{"version": v["version"], "sha256": _digest(v["content"].encode()),
                                  "frozen": bool(v["metadata"].get("frozen"))} for v in record.versions]})
            for source in sources:
                (staged / source).unlink()
            manifest = staged / MANIFEST_PATH
            manifest.parent.mkdir(parents=True, exist_ok=True)
            manifest.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            _git(staged, "add", "--all")
            _git(staged, "commit", "-m", "migration: complete native research records with verified history")
            # Read through the normal product reader, including every Git-backed
            # historical body. A successful write is not migration acceptance.
            for record in records:
                imported = workspace_store(staged).versions(record.artifact_id)
                if len(imported) != len(record.versions):
                    raise RecordMigrationError(f"Version count changed: {record.artifact_id}")
                for old, new in zip(record.versions, imported, strict=True):
                    for field in ("content", "version", "provenance", "produced_by_node_type", "produced_by_run_id"):
                        if old.get(field, {} if field == "provenance" else "") != new.get(field):
                            raise RecordMigrationError(f"Migration changed {record.artifact_id} v{old['version']} {field}")
                    for field in ("type", "name", "created_at"):
                        expected = old.get(field, record.artifact_id if field == "name" else "")
                        if new.get(field) != expected:
                            raise RecordMigrationError(f"Migration changed {record.artifact_id} v{old['version']} {field}")
                    for key, value in old["metadata"].items():
                        if new["metadata"].get(key) != value:
                            raise RecordMigrationError(f"Migration changed metadata {record.artifact_id} v{old['version']} {key}")
                    if bool(old["metadata"].get("frozen")) != bool(new["metadata"].get("frozen")):
                        raise RecordMigrationError(f"Migration changed freeze status: {record.artifact_id}")
            # Last precondition, before touching the original worktree.
            if _git(root, "rev-parse", "HEAD") != backup_head or _git(root, "status", "--porcelain"):
                raise RecordMigrationError("The project changed during migration; verified conversion is not applied")
            for name, expected in sources.items():
                if _digest((root / name).read_bytes()) != expected:
                    raise RecordMigrationError(f"Source changed during migration: {name}")
            _git(root, "merge", "--ff-only", _git(staged, "rev-parse", "HEAD"))
            return report
        finally:
            _git(root, "worktree", "remove", "--force", str(staged), check=False)

"""Upgrade actual record versions, with rollback evidence and no silent loss."""
import hashlib
import json
from pathlib import Path
import subprocess

import pytest

from core.ledger import workspace_store
from core.record_migration import RecordMigrationError, inspect_legacy_records, migrate_records


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], text=True, encoding="utf-8").strip()


def legacy_project(tmp_path, *, frozen=True):
    root = tmp_path / "project"
    root.mkdir()
    git(root, "init", "-b", "main")
    git(root, "config", "core.autocrlf", "true")
    git(root, "config", "user.name", "Migration test")
    git(root, "config", "user.email", "migration@example.org")
    directory = root / "hypothesis/artifacts"
    directory.mkdir(parents=True)
    (directory / ".versions").mkdir()
    snapshots = []
    for version in (1, 2):
        content = f"# 研究计划，第 {version} 版\n材料：data/input.csv\n"
        record = {"type": "pre_registration", "name": "plan", "content": content,
                  "version": version, "created_at": "2026-09-12T01:00:00Z",
                  "content_hash": hashlib.sha256(content.encode()).hexdigest(),
                  "metadata": {"question": "可以复现吗？", "input": "data/input.csv"},
                  "provenance": {"kind": "produced", "by_node_type": "hypothesis", "by_run_id": "test"},
                  "produced_by_node_type": "hypothesis", "produced_by_run_id": "test"}
        if version == 1:
            record["metadata"].update(frozen=True, frozen_at="2026-09-12T01:01:00Z")
        elif frozen:
            record["metadata"].update(frozen=True, frozen_at="2026-09-12T01:02:00Z")
        path = directory / (".versions/pre_registration__plan@v1.json" if version == 1 else "pre_registration__plan.json")
        path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
        snapshots.append(record)
    head = directory / "pre_registration__plan.json"
    if frozen:
        (directory / ".frozen.jsonl").write_text(json.dumps({"action": "freeze",
            "artifact_id": head.stem, "path": head.relative_to(root).as_posix(), "version": 2,
            "sha256": hashlib.sha256(head.read_bytes()).hexdigest()}) + "\n")
    (root / "data").mkdir()
    (root / "data/input.csv").write_bytes(b"x,y\n1,2\n")
    git(root, "add", "--all")
    git(root, "commit", "-m", "Existing research")
    return root, snapshots


def test_upgrade_preserves_versions_metadata_frozen_history_and_binary_materials(tmp_path):
    root, original = legacy_project(tmp_path)
    before = git(root, "rev-parse", "HEAD")
    report = migrate_records(root)
    assert report["backup_commit"] == before
    assert git(root, "rev-parse", report["backup_ref"]) == before
    assert len(report["records"]) == 1
    versions = workspace_store(root).versions("pre_registration__plan")
    for old, new in zip(original, versions, strict=True):
        for field in ("version", "content", "metadata", "provenance", "produced_by_run_id", "content_hash"):
            assert new[field] == old[field]
    assert (root / "plan/pre_registration__plan.md").is_file()
    assert (root / "data/input.csv").read_bytes() == b"x,y\n1,2\n"
    assert inspect_legacy_records(root) == ([], {})
    assert migrate_records(root) == report
    assert git(root, "status", "--porcelain") == ""
    # The backup is readable using the old release format, with exact bytes.
    old = json.loads(git(root, "show", f"{report['backup_ref']}:hypothesis/artifacts/pre_registration__plan.json"))
    assert old == original[-1]


def test_amended_draft_keeps_the_old_frozen_version(tmp_path):
    root, _ = legacy_project(tmp_path, frozen=False)
    migrate_records(root)
    store = workspace_store(root)
    assert not store.head("pre_registration__plan").frozen
    assert store.latest_frozen("pre_registration__plan")["version"] == 1
    assert store.record("pre_registration__plan")["version"] == 2


def test_corrupt_frozen_envelope_is_refused_without_changing_git_or_files(tmp_path):
    root, _ = legacy_project(tmp_path)
    head = root / "hypothesis/artifacts/pre_registration__plan.json"
    record = json.loads(head.read_text(encoding="utf-8"))
    record["metadata"]["question"] = "tampered"
    head.write_text(json.dumps(record), encoding="utf-8")
    before = (git(root, "rev-parse", "HEAD"), head.read_bytes(), git(root, "status", "--porcelain"))
    with pytest.raises(RecordMigrationError, match="Frozen envelope checksum mismatch"):
        migrate_records(root, checkpoint_pending=True)
    assert (git(root, "rev-parse", "HEAD"), head.read_bytes(), git(root, "status", "--porcelain")) == before


def test_pending_work_is_checkpointed_and_preserved_only_when_explicitly_requested(tmp_path):
    root, _ = legacy_project(tmp_path)
    (root / "notes.md").write_text("尚未提交的研究笔记\n", encoding="utf-8", newline="")
    with pytest.raises(RecordMigrationError, match="pending changes"):
        migrate_records(root)
    report = migrate_records(root, checkpoint_pending=True)
    assert (root / "notes.md").read_text(encoding="utf-8") == "尚未提交的研究笔记\n"
    assert git(root, "show", f"{report['backup_ref']}:notes.md") == "尚未提交的研究笔记"


def test_missing_history_never_becomes_invented_versions(tmp_path):
    root, _ = legacy_project(tmp_path)
    (root / "hypothesis/artifacts/.versions/pre_registration__plan@v1.json").unlink()
    with pytest.raises(RecordMigrationError, match="Missing historical versions"):
        migrate_records(root, checkpoint_pending=True)


def test_a_mixed_workspace_is_refused_by_the_preview_too(tmp_path):
    """混合工作区（原生账本已在记事，旧信封还没转进来）—— 预览就要说出来。

    这条判据以前只长在 `migrate_records` 里：`--preview` 报一切正常，真跑才被拒。
    操作者拿预览当"能不能迁"的答案，于是问的和答的是两件事。
    """
    root, _ = legacy_project(tmp_path)
    store = workspace_store(root)
    store.save(artifact_id="derivation_log__later", artifact_type="derivation_log", name="later",
               content="原生记录\n", metadata={}, directory=root / "derivation",
               created_at="2026-09-13T00:00:00Z", provenance={},
               produced_by_node_type="derivation", produced_by_run_id="test",
               by_node="derivation", by_run="test")
    before = git(root, "rev-parse", "HEAD")

    with pytest.raises(RecordMigrationError, match="Native and legacy ledgers coexist"):
        inspect_legacy_records(root)
    with pytest.raises(RecordMigrationError, match="Native and legacy ledgers coexist"):
        migrate_records(root, checkpoint_pending=True)
    assert git(root, "rev-parse", "HEAD") == before, "被拒的工作区一个字不动"

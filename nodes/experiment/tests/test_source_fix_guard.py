"""Source fixer defaults and process-owner write gate."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.diagnose import SourceFixer, fix_source


def test_source_fixer_blocks_real_write_without_owner_capability(tmp_path, monkeypatch):
    monkeypatch.delenv("HARNESS_ALLOW_SOURCE_WRITE", raising=False)
    source = tmp_path / "input.f90"
    source.write_text("old_name\n", encoding="utf-8")

    fixer = SourceFixer()
    fixer.add_pattern(
        name="rename",
        search="old_name",
        replace="new_name",
        file_glob="*.f90",
    )
    result = fixer.apply(str(tmp_path), dry_run=False)

    assert result["summary"]["failed"] == 1
    assert source.read_text(encoding="utf-8") == "old_name\n"


def test_fix_source_remains_dry_run_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("HARNESS_ALLOW_SOURCE_WRITE", raising=False)
    source = tmp_path / "input.f90"
    source.write_text("old_name\n", encoding="utf-8")

    result = fix_source(
        str(tmp_path),
        [{"name": "rename", "search": "old_name", "replace": "new_name", "file_glob": "*.f90"}],
    )

    assert result["dry_run"] is True
    assert source.read_text(encoding="utf-8") == "old_name\n"


def test_source_fixer_writes_only_declared_worktree_and_records_patch(
        tmp_path, monkeypatch):
    worktree = tmp_path / "worktree"
    records = tmp_path / "patches"
    worktree.mkdir()
    source = worktree / "input.f90"
    source.write_text("old_name\n", encoding="utf-8")
    monkeypatch.setenv("HARNESS_ALLOW_SOURCE_WRITE", "1")
    monkeypatch.setenv(
        "EXPERIMENT_SOURCE_WORKTREE_ROOTS", f'["{worktree}"]')
    monkeypatch.setenv(
        "EXPERIMENT_SOURCE_PATCH_ROOTS", f'["{records}"]')

    result = fix_source(
        str(worktree),
        [{"name": "rename", "search": "old_name", "replace": "new_name",
          "file_glob": "*.f90"}],
        dry_run=False,
    )

    assert result["summary"]["failed"] == 0
    assert source.read_text(encoding="utf-8") == "new_name\n"
    assert list(records.glob("*.bak"))
    assert list(records.glob("*.patch"))


def test_source_fixer_rejects_baseline_even_with_owner_flag(
        tmp_path, monkeypatch):
    baseline = tmp_path / "baseline"
    worktree = tmp_path / "worktree"
    baseline.mkdir()
    source = baseline / "input.f90"
    source.write_text("old_name\n", encoding="utf-8")
    monkeypatch.setenv("HARNESS_ALLOW_SOURCE_WRITE", "1")
    monkeypatch.setenv(
        "EXPERIMENT_SOURCE_WORKTREE_ROOTS", f'["{worktree}"]')

    result = fix_source(
        str(baseline),
        [{"name": "rename", "search": "old_name", "replace": "new_name",
          "file_glob": "*.f90"}],
        dry_run=False,
    )

    assert result["summary"]["failed"] == 1
    assert source.read_text(encoding="utf-8") == "old_name\n"

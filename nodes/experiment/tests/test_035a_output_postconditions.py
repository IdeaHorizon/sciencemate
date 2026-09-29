"""035a (C5): one shared declared-output evaluator for the route channel.

SCOPE G2/G4/G5 on the frozen base: an empty file or an empty directory counts as a
delivered output; a symlink or hardlink to an hour-old binary, or a chmod-only
touch of a pre-existing file, all read as "the attempt produced it" because the
verifier only compares a stat fingerprint (mtime/ctime/inode) against the bound
baseline.  These tests pin the approved口径 (035/05):

* A  non-empty: an ordinary declared file must have size > 0;
* trailing ``/`` is the only directory syntax, parsed before any normalisation;
  a directory delivery must contain at least one fresh regular file; the same
  path declared without ``/`` is a kind mismatch;
* symlinks: in-root, regular, non-empty and fresh target → pass; escaping,
  old-target symlinks/hardlinks and chmod-only → fail;
* preserved-mtime staging (``cp -p`` / tar / copy2) inside the attempt stays a
  delivery (P0a v4 ruling: mtime is not producer identity);
* touch-only remains a disclosed residual (no content identity, no Core write-set).
"""
from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

from nodes.experiment.tools import execution_route as er


def _binding(workdir: Path, specs: list[str]) -> dict:
    """A route binding exactly as begin_route_step_attempt records it (baseline at bind)."""
    binding = {
        "expected_outputs": list(specs), "resolved_workdir": str(workdir),
        "bound_at_ns": time.time_ns(),
    }
    binding["expected_output_baseline"] = er._expected_output_baseline(binding)
    return binding


def _old(path: Path, content: str = "old binary\n", hours: float = 1.0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    stamp = time.time() - hours * 3600
    os.utime(path, (stamp, stamp))
    return path


def _verify(binding: dict) -> tuple[list[str], list[str], list[str]]:
    return er._verify_expected_outputs(binding)


# ── A：非空 ───────────────────────────────────────────────────────────────────

def test_route_empty_file_is_not_a_delivery(tmp_path):
    binding = _binding(tmp_path, ["out/result.txt"])
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "result.txt").write_text("")          # ./solver > out/result.txt crashed
    verified, paths, missing = _verify(binding)
    assert missing == ["out/result.txt"], (verified, paths, missing)
    assert verified == [] and paths == []


def test_route_non_empty_new_file_is_a_delivery(tmp_path):
    binding = _binding(tmp_path, ["out/result.txt"])
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "result.txt").write_text("42\n")
    verified, paths, missing = _verify(binding)
    assert verified == ["out/result.txt"] and missing == []
    assert paths == [er.output_lexical_key(str(tmp_path / "out" / "result.txt"))]


# ── 目录：尾斜杠 ───────────────────────────────────────────────────────────────

def test_route_empty_directory_is_not_a_delivery(tmp_path):
    binding = _binding(tmp_path, ["results/"])
    (tmp_path / "results").mkdir()                              # mkdir -p before the crash
    verified, _paths, missing = _verify(binding)
    assert missing == ["results/"] and verified == []


def test_route_directory_with_a_fresh_entry_is_a_delivery(tmp_path):
    binding = _binding(tmp_path, ["results/"])
    (tmp_path / "results").mkdir()
    (tmp_path / "results" / "step_001.nc").write_text("data\n")
    verified, paths, missing = _verify(binding)
    assert verified == ["results/"] and missing == []
    assert paths == [er.output_lexical_key(str(tmp_path / "results"))]


def test_route_directory_declared_without_slash_is_a_kind_mismatch(tmp_path):
    binding = _binding(tmp_path, ["results"])
    (tmp_path / "results").mkdir()
    (tmp_path / "results" / "step_001.nc").write_text("data\n")
    verified, _paths, missing = _verify(binding)
    assert missing == ["results"] and verified == []


def test_route_directory_present_at_bind_needs_a_new_or_changed_entry(tmp_path):
    (tmp_path / "results").mkdir()
    _old(tmp_path / "results" / "stale.nc")
    binding = _binding(tmp_path, ["results/"])
    verified, _paths, missing = _verify(binding)
    assert missing == ["results/"], "nothing changed inside the directory"
    (tmp_path / "results" / "fresh.nc").write_text("new\n")
    verified, _paths, missing = _verify(binding)
    assert verified == ["results/"] and missing == []


# ── 软链 / 硬链 / chmod-only / 保留 mtime / touch-only ────────────────────────

def test_route_symlink_to_fresh_in_root_target_is_a_delivery(tmp_path):
    binding = _binding(tmp_path, ["wrf.exe"])
    (tmp_path / "main").mkdir()
    (tmp_path / "main" / "wrf.exe").write_text("fresh build\n")
    os.symlink(tmp_path / "main" / "wrf.exe", tmp_path / "wrf.exe")   # WRF-style link
    verified, _paths, missing = _verify(binding)
    assert verified == ["wrf.exe"] and missing == []


def test_route_symlink_to_old_target_is_not_a_delivery(tmp_path):
    old = _old(tmp_path / "prev" / "wrf.exe.bak")
    # "old" means the content predates the attempt: user space cannot back-date
    # ctime, so let the bind happen after the stale-content tolerance has elapsed.
    time.sleep(0.15)
    binding = _binding(tmp_path, ["wrf.exe"])
    os.symlink(old, tmp_path / "wrf.exe")
    verified, _paths, missing = _verify(binding)
    assert missing == ["wrf.exe"] and verified == []


def test_route_hardlink_to_old_target_is_not_a_delivery(tmp_path):
    old = _old(tmp_path / "prev" / "wrf.exe.bak")
    binding = _binding(tmp_path, ["wrf.exe"])
    os.link(old, tmp_path / "wrf.exe")
    verified, _paths, missing = _verify(binding)
    assert missing == ["wrf.exe"] and verified == []


def test_route_escaping_symlink_is_not_a_delivery(tmp_path):
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    (outside / "wrf.exe").write_text("fresh but outside\n")
    root = tmp_path / "root"
    root.mkdir()
    binding = _binding(root, ["wrf.exe"])
    os.symlink(outside / "wrf.exe", root / "wrf.exe")
    verified, _paths, missing = _verify(binding)
    assert missing == ["wrf.exe"] and verified == []


def test_route_directory_entry_escaping_the_root_does_not_count(tmp_path):
    """Directory entries are not filtered by the glob expansion, so the escape check
    inside the per-file rule is the only thing that stops a directory delivery from
    being satisfied by a symlink to an outside file."""
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    (outside / "data.nc").write_text("fresh but outside\n")
    root = tmp_path / "root"
    (root / "results").mkdir(parents=True)
    binding = _binding(root, ["results/"])
    os.symlink(outside / "data.nc", root / "results" / "data.nc")
    verified, _paths, missing = _verify(binding)
    assert missing == ["results/"] and verified == []


def test_route_chmod_only_on_a_preexisting_file_is_not_a_delivery(tmp_path):
    existing = _old(tmp_path / "solver")
    binding = _binding(tmp_path, ["solver"])
    time.sleep(0.01)
    existing.chmod(0o755)                                        # ctime moves, content does not
    verified, _paths, missing = _verify(binding)
    assert missing == ["solver"] and verified == []
    assert er._unchanged_expected_outputs(binding, missing) == ["solver"]


def test_route_preserved_mtime_copy_inside_the_attempt_is_a_delivery(tmp_path):
    """P0a v4 ruling: cp -p / tar / rsync -t keep the source mtime; a new inode
    written by this attempt is a delivery regardless of its mtime."""
    source = _old(tmp_path / "prebuilt" / "solver")
    binding = _binding(tmp_path, ["bin/solver"])
    (tmp_path / "bin").mkdir()
    shutil.copy2(source, tmp_path / "bin" / "solver")
    verified, _paths, missing = _verify(binding)
    assert verified == ["bin/solver"] and missing == []


def test_route_touch_only_is_a_disclosed_residual(tmp_path):
    """Without content identity or a Core write-set a touch is indistinguishable
    from a legitimate in-place rewrite of the same size: 035a does NOT claim to
    close G4's ``preexisting_touch_only``; the observation says which basis passed it."""
    existing = _old(tmp_path / "solver")
    binding = _binding(tmp_path, ["solver"])
    time.sleep(0.01)
    existing.touch()
    verified, _paths, missing = _verify(binding)
    assert verified == ["solver"] and missing == []
    rows, _truncated = er._output_observations(binding, verified)
    assert rows[0]["freshness"] == "mtime_changed_same_inode"


# ── 观测形状：与 P0a v4 的产物身份收据同一份 ───────────────────────────────────

def test_route_observations_carry_output_identity_and_declared_kind(tmp_path):
    binding = _binding(tmp_path, ["out/result.txt", "results/"])
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "result.txt").write_text("42\n")
    (tmp_path / "results").mkdir()
    (tmp_path / "results" / "a.nc").write_text("x\n")
    verified, _paths, missing = _verify(binding)
    assert missing == [] and verified == ["out/result.txt", "results/"]
    rows, truncated = er._output_observations(binding, verified)
    assert truncated is False
    by_spec = {row["spec"]: row for row in rows}
    assert by_spec["out/result.txt"]["kind"] == "file"
    assert by_spec["out/result.txt"]["declared_kind"] == "file"
    assert by_spec["out/result.txt"]["sha256"] == er.output_identity(
        str(tmp_path / "out" / "result.txt"))["sha256"]
    assert by_spec["results/"]["kind"] == "directory"
    assert by_spec["results/"]["declared_kind"] == "directory"
    for row in rows:
        for key in ("path", "st_dev", "st_ino", "size_bytes", "mtime_ns", "ctime_ns", "freshness"):
            assert key in row, key

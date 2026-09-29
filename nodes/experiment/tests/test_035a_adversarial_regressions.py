"""Adversarial review (2026-09-21, three lenses × independent reproduction) of P0a v4:
the receipt vouched for more than the attempt produced.  Pinned here on the 035a
evaluator, which is where the producer receipt is now computed:

* glob specs — the whole match set counted as "verified" once its fingerprint moved,
  so a fake planted *before* the attempt rode into the receipt beside one real
  sibling (P1, three lenses);
* symlink products — the receipt kept only the link string, so the target could be
  rewritten after the attempt (P1, two lenses);
* directory products — a directory's inode/size/ctime do not change when a file
  inside it is rewritten in place (P2);
* external jobs — the observation is taken at verification time, so anything the
  model wrote between job end and verification was receipted (P1, two lenses);
  bounded now by the frozen first-terminal observation / physical end time.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

from nodes.experiment.tests.test_p0a_v4_producer_receipt import (
    _pending_state, _refused_as_not_produced, _roc_build, _route_backed_attempt, _write_product,
)
from nodes.experiment.tools import execution_action_census as census
from nodes.experiment.tools import execution_route as er


def test_glob_spec_does_not_vouch_for_a_fake_planted_before_the_attempt(tmp_path):
    state, run_root = _pending_state(tmp_path)
    fake = run_root / "bin" / "solver"
    _write_product(fake, "#!/bin/sh\necho fake\n")                 # planted before the attempt
    helper = run_root / "bin" / "helper"
    finished = _route_backed_attempt(
        state, run_root, produce=lambda: _write_product(helper), expected_outputs=["bin/*"])
    assert finished["verified_output_specs"] == ["bin/*"]
    receipted = {row["path"] for row in finished["output_observations"]}
    assert receipted == {er.output_lexical_key(str(helper))}, receipted   # the fake is not in it
    refused = _refused_as_not_produced(_roc_build(state, fake), reason="no_producer_receipt")
    assert refused["unattributed_paths"][0]["path"] == er.output_lexical_key(str(fake))
    assert _roc_build(state, helper)["status"] == "success"


def test_symlink_product_is_bound_to_its_target_content(tmp_path):
    state, run_root = _pending_state(tmp_path)
    real = run_root / "build" / "solver.real"
    link = run_root / "bin" / "solver"

    def _install() -> None:
        _write_product(real, "#!/bin/sh\nexit 0\n")
        link.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(os.path.relpath(real, link.parent), link)

    finished = _route_backed_attempt(
        state, run_root, produce=_install, expected_outputs=["bin/solver", "build/solver.real"])
    rows = {row["spec"]: row for row in finished["output_observations"]}
    assert rows["bin/solver"]["kind"] == "symlink"
    assert rows["bin/solver"]["target_sha256"] == rows["build/solver.real"]["sha256"]
    real.write_text("#!/bin/sh\necho substituted\n")               # rewrite the target afterwards
    _refused_as_not_produced(_roc_build(state, link), reason="target_content_changed_since_attempt")

    # control: untouched, the symlink product closes (a run's closure is idempotent, hence a fresh run)
    state, run_root = _pending_state(tmp_path / "ok")
    real, link = run_root / "build" / "solver.real", run_root / "bin" / "solver"
    _route_backed_attempt(
        state, run_root, produce=_install, expected_outputs=["bin/solver", "build/solver.real"])
    assert _roc_build(state, link)["status"] == "success"


def test_directory_product_is_bound_to_its_entries(tmp_path):
    state, run_root = _pending_state(tmp_path)
    build = run_root / "build"
    finished = _route_backed_attempt(
        state, run_root, produce=lambda: _write_product(build / "solver"), expected_outputs=["build/"])
    row = finished["output_observations"][0]
    assert row["kind"] == "directory" and [e["name"] for e in row["entries"]] == ["solver"]
    with (build / "solver").open("w") as handle:                     # in-place rewrite, same inode
        handle.write("#!/bin/sh\necho substituted\n")
    _refused_as_not_produced(_roc_build(state, build), reason="directory_entry_content_changed_since_attempt")

    state, run_root = _pending_state(tmp_path / "ok")               # control on a fresh run
    build = run_root / "build"
    _route_backed_attempt(
        state, run_root, produce=lambda: _write_product(build / "solver"), expected_outputs=["build/"])
    assert _roc_build(state, build)["status"] == "success"


def test_external_upper_bound_rejects_files_written_after_the_terminal_observation(tmp_path):
    """An external job's receipt is observed at verification: without an upper bound a
    file the model wrote after the job ended would be receipted.  The bound is the
    frozen first-terminal observation (or the physical end time when known)."""
    binding = {"expected_outputs": ["out/*.nc"], "resolved_workdir": str(tmp_path),
               "bound_at_ns": time.time_ns()}
    binding["expected_output_baseline"] = er._expected_output_baseline(binding)
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "job.nc").write_text("written by the job\n")
    terminal_ns = time.time_ns()
    time.sleep(0.15)
    (tmp_path / "out" / "late.nc").write_text("written by the model afterwards\n")
    verified, paths, missing = er._verify_expected_outputs({**binding, "not_after_ns": terminal_ns})
    assert verified == ["out/*.nc"] and missing == []
    assert [Path(p).name for p in paths] == ["job.nc"]
    rows, _ = er._output_observations({**binding, "not_after_ns": terminal_ns}, verified)
    assert [Path(r["path"]).name for r in rows] == ["job.nc"]
    # without the bound both would be receipted — that is exactly the hole
    _v, unbounded, _m = er._verify_expected_outputs(binding)
    assert sorted(Path(p).name for p in unbounded) == ["job.nc", "late.nc"]


def test_receipts_expose_only_passed_members(tmp_path):
    state, run_root = _pending_state(tmp_path)
    empty = run_root / "out" / "empty.log"
    real = run_root / "out" / "result.nc"

    def _produce() -> None:
        _write_product(real, "data\n")
        empty.parent.mkdir(parents=True, exist_ok=True)
        empty.write_text("")

    finished = _route_backed_attempt(state, run_root, produce=_produce, expected_outputs=["out/*"])
    receipts = census.satisfying_attempt_output_receipts(state)
    assert [Path(r["path"]).name for r in receipts[0]["output_observations"]] == ["result.nc"]
    _refused_as_not_produced(_roc_build(state, empty), reason="no_producer_receipt")

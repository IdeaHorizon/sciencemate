"""Small real trees reproduce deployment scan starvation, without a package install."""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest

from core.state import State
from nodes.experiment.tools import action_boundary, capture, finish, job_settlement, ledger, run_bootstrap


@pytest.fixture(autouse=True)
def stable_directory_enumeration(monkeypatch):
    """Fix only enumeration order, not files, identity reads, coverage or verdicts."""
    original = capture.os.scandir

    class Ordered:
        def __init__(self, path):
            with original(path) as entries:
                self.children = iter(sorted(entries, key=lambda child: child.name))

        def __iter__(self):
            return self

        def __next__(self):
            return next(self.children)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.close()

        def close(self):
            pass

    monkeypatch.setattr(capture.os, "scandir", Ordered)


def _layout(root, *, deep_files=80):
    package = root / "deployment"
    deep = package / ".environment/Lib/site-packages/dense/deeper"
    deep.mkdir(parents=True, exist_ok=True)
    for number in range(deep_files):
        (deep / f"dependency-{number:03}.txt").write_text("dependency")
    log = package / "logs/01-create.log"
    script = package / ".environment/Scripts/python.exe"
    output = package / "artifacts/result.dat"
    for path in (log, script, output):
        path.parent.mkdir(parents=True, exist_ok=True)
    return log, script, output


def _snapshot(root, **kwargs):
    return capture.snapshot([], [root], max_files=20, max_seconds=60,
                            include_directories=True, **kwargs)


def test_deep_library_cannot_starve_shallow_sibling_identities(tmp_path):
    log, script, output = _layout(tmp_path)
    for path in (log, script, output):
        path.write_text(path.name)
    snapshot = _snapshot(tmp_path)
    assert snapshot["truncated"], "The deep tree really exceeds the common budget"
    assert snapshot["scanned"] <= 20
    assert all(str(path) in snapshot["entries"] for path in (log, script, output))


def test_new_shallow_output_has_real_identity_despite_unfinished_deep_tree(tmp_path):
    log, script, output = _layout(tmp_path)
    log.write_text("earlier log")
    script.write_text("earlier executable")
    before = _snapshot(tmp_path)
    output.write_text("new actual result")
    after = _snapshot(tmp_path)
    observed = capture.diff(before, after, limit=None)
    assert before["truncated"] and after["truncated"]
    assert str(output) in observed["created"]
    assert after["entries"][str(output)]["sha256"]


@pytest.fixture
def state(tmp_path):
    state = State.new("experiment", tmp_path / "runs", project_id="shallow-capture", session_id="owner")
    state.hook_state["node_inputs"] = {
        "experiment_spec": "Produce and retain the requested output.",
        "prereg_assignment": {"kind": "none", "reason": "file-only capture regression"},
    }
    asyncio.run(run_bootstrap.establish(state))
    assert ledger.bootstrap_phase(state) == "ready"
    return state


def _bounded_action(state, root):
    handle = action_boundary.begin(state, tool="capture-file-probe", work_root=str(root))
    assert isinstance(handle, action_boundary.ActionHandle)
    return handle


def _settle(state, handle):
    result = action_boundary.settle(state, handle, outcome="settled", exit_code=0)
    assert result["status"] == "recorded", result
    return ledger.open_ledger(state).records()


def _bound_snapshots(monkeypatch):
    real = capture.snapshot
    monkeypatch.setattr(capture, "snapshot",
                        lambda *args, **kwargs: real(*args, **{**kwargs, "max_files": 20, "max_seconds": 60}))


def test_actual_action_records_a_new_shallow_output_without_redeclaring_a_file_obligation(state, monkeypatch):
    root = state.root / "outputs/experiment/runtime"
    root.mkdir(parents=True, exist_ok=True)
    _, _, output = _layout(root)
    _bound_snapshots(monkeypatch)
    handle = _bounded_action(state, root)
    output.write_text("actual task output")
    records = _settle(state, handle)
    observation = ledger.project_output_observations(records)[-1]
    assert observation["truncated"]
    assert finish._file_evidence(state, records, {"path": str(output)})["passed"]


def test_dependency_growth_preserves_observed_unchanged_shallow_producers(state, monkeypatch):
    root = state.root / "outputs/experiment/runtime"
    root.mkdir(parents=True, exist_ok=True)
    log, script, _ = _layout(root, deep_files=0)
    _bound_snapshots(monkeypatch)
    first = _bounded_action(state, root)
    log.write_text("original creation log")
    script.write_text("original interpreter identity")
    original = _settle(state, first)
    original_producers = {str(path): finish._producers(original, path) for path in (log, script)}
    assert all(original_producers.values())
    install = _bounded_action(state, root)
    _layout(root, deep_files=80)
    records = _settle(state, install)
    assert ledger.project_output_observations(records)[-1]["truncated"]
    for path in (log, script):
        assert finish._producers(records, path) == original_producers[str(path)]


def test_existing_but_unhashed_directory_is_not_invented_as_new(tmp_path):
    existing = tmp_path / "existing"
    existing.mkdir()
    before = capture.snapshot([], [tmp_path], include_directories=False)
    assert str(existing) not in before["entries"]
    after = capture.snapshot([existing], [tmp_path], include_directories=False)
    observation = capture.diff(before, after)
    assert str(existing) not in observation["created"]
    assert str(existing) in observation["unknown"]


def test_new_parent_directory_does_not_hide_a_shallow_result(tmp_path):
    _, _, output = _layout(tmp_path)
    output.parent.rmdir()
    before = _snapshot(tmp_path)
    output.parent.mkdir()
    output.write_text("actual output in a newly created directory")
    after = _snapshot(tmp_path)
    assert before["truncated"] and after["truncated"]
    assert str(output) in capture.diff(before, after)["created"]


def test_a_completed_parent_listing_does_not_cover_an_existing_deep_subtree(tmp_path):
    nested = tmp_path / "existing/deeper/old.dat"
    nested.parent.mkdir(parents=True)
    nested.write_text("old file")
    before = {"entries": {}, "truncated": True, "root_coverage": [
        {"root": str(tmp_path), "status": "truncated"},
        {"root": str(tmp_path), "status": "direct_complete", "entry_names": ["existing"]},
    ]}
    after = capture.snapshot([nested])
    observation = capture.diff(before, after)
    assert str(nested) in observation["unknown"]
    assert str(nested) not in observation["created"]


@pytest.mark.parametrize("names", [None, ["bad/name"], [".."], ["existing.dat"]])
def test_existing_unhashed_or_invalid_local_listing_cannot_prove_absence(tmp_path, names):
    existing = tmp_path / "existing.dat"
    existing.write_text("not a new object")
    before = {"entries": {}, "truncated": True, "root_coverage": [
        {"root": str(tmp_path), "status": "truncated"},
        {"root": str(tmp_path), "status": "direct_complete", "entry_names": names},
    ]}
    observation = capture.diff(before, capture.snapshot([existing]))
    assert str(existing) in observation["unknown"]
    assert str(existing) not in observation["created"]


def test_failed_local_enumeration_never_backfills_an_existing_file(tmp_path, monkeypatch):
    denied = tmp_path / "denied"
    denied.mkdir()
    old = denied / "old.dat"
    old.write_text("existing")
    real_scandir = capture.os.scandir

    def unreadable(path):
        if Path(path) == denied:
            raise PermissionError("injected local enumeration denial")
        return real_scandir(path)

    with monkeypatch.context() as context:
        context.setattr(capture.os, "scandir", unreadable)
        before = capture.snapshot([], [tmp_path], include_directories=True)
    observation = capture.diff(before, capture.snapshot([old], [tmp_path], include_directories=True))
    assert before["truncated"]
    assert str(old) in observation["unknown"]
    assert str(old) not in observation["created"]


def test_excluded_readonly_subtree_is_not_hashed_or_promoted_by_local_coverage(tmp_path, monkeypatch):
    readonly = tmp_path / "readonly"
    readonly.mkdir()
    old = readonly / "old.dat"
    old.write_text("private input")
    real_hash = capture._sha256

    def observed_hash(path):
        assert not path.is_relative_to(readonly), "excluded read-only subtree was read"
        return real_hash(path)

    with monkeypatch.context() as context:
        context.setattr(capture, "_sha256", observed_hash)
        before = capture.snapshot([], [tmp_path], excluded_roots=[readonly], include_directories=True)
    observation = capture.diff(before, capture.snapshot([old]))
    assert str(old) in observation["unknown"]
    assert str(old) not in observation["created"]


def test_overlapping_roots_preserve_local_absence_without_claiming_deep_completeness(tmp_path):
    _, _, output = _layout(tmp_path)
    roots = [tmp_path, output.parent]
    before = capture.snapshot([], roots, max_files=20, max_seconds=60, include_directories=True)
    output.write_text("new output")
    after = capture.snapshot([], roots, max_files=20, max_seconds=60, include_directories=True)
    assert before["truncated"] and after["truncated"]
    assert after["scanned"] <= 20
    assert str(output) in capture.diff(before, after)["created"]


def test_old_snapshot_without_local_listing_retains_unknown_absence(tmp_path):
    old = {"entries": {}, "truncated": True,
           "root_coverage": [{"root": str(tmp_path), "status": "truncated"}]}
    assert not capture._absence_known(old, str(tmp_path / "new.dat"))
    complete = {"entries": {}, "truncated": False,
                "root_coverage": [{"root": str(tmp_path), "status": "complete"}]}
    assert capture._absence_known(complete, str(tmp_path / "new.dat"))


@pytest.mark.skipif(os.name != "nt", reason="Windows path case semantics")
def test_case_alias_of_existing_unhashed_name_is_not_a_new_file(tmp_path):
    existing = tmp_path / "Result.DAT"
    existing.write_text("old bytes")
    before = capture.snapshot([], [tmp_path])
    requested = tmp_path / "result.dat"
    after = capture.snapshot([requested], [tmp_path])
    observation = capture.diff(before, after)
    assert str(requested) not in observation["created"]
    assert str(requested) in observation["unknown"]


def test_partial_name_enumeration_cannot_prove_an_unseen_existing_child_absent(tmp_path):
    for number in range(6):
        (tmp_path / f"a-{number}").write_text("existing")
    old = tmp_path / "z-existing.dat"
    old.write_text("existing late child")
    before = capture.snapshot([], [tmp_path], max_files=3, max_seconds=60)
    after = capture.snapshot([old])
    assert before["truncated"]
    assert not any(row["status"] == "direct_complete" for row in before["root_coverage"])
    assert str(old) in capture.diff(before, after)["unknown"]


def test_current_local_error_blocks_older_complete_listing(tmp_path):
    before = {"entries": {}, "truncated": True, "root_coverage": [
        {"root": str(tmp_path), "status": "truncated"},
        {"root": str(tmp_path), "status": "direct_complete", "entry_names": []},
        {"root": str(tmp_path), "status": "error", "error": "later listing failed"},
    ]}
    assert not capture._absence_known(before, str(tmp_path / "child/output.dat"))


def test_failed_other_subtree_does_not_erase_a_completed_local_listing(tmp_path, monkeypatch):
    denied = tmp_path / "denied"
    denied.mkdir()
    real_scandir = capture.os.scandir

    def unreadable(path):
        if Path(path) == denied:
            raise PermissionError("unreadable sibling")
        return real_scandir(path)

    with monkeypatch.context() as context:
        context.setattr(capture.os, "scandir", unreadable)
        before = capture.snapshot([], [tmp_path], include_directories=True)
    output = tmp_path / "new-directory/result.dat"
    output.parent.mkdir()
    output.write_text("real result")
    observation = capture.diff(before, capture.snapshot([], [tmp_path], include_directories=True))
    assert before["truncated"]
    assert str(output) in observation["created"]
    assert not capture._absence_known(before, str(denied / "unseen.dat"))


@pytest.mark.parametrize("link_kind", [
    "symlink",
    pytest.param("junction", marks=pytest.mark.skipif(os.name != "nt", reason="Windows junction")),
])
def test_new_directory_alias_cannot_claim_existing_external_content(state, tmp_path, link_kind):
    root = state.root / "outputs/experiment/runtime"
    root.mkdir(parents=True, exist_ok=True)
    external = tmp_path / "earlier-external-data"
    external.mkdir()
    old = external / "old.dat"
    old.write_text("pre-existing input, not a new result")
    alias = root / "new-alias"
    handle = action_boundary.begin(state, tool="alias-file-probe", work_root=str(root),
                                   output_candidates=[alias / old.name])
    assert isinstance(handle, action_boundary.ActionHandle)
    if link_kind == "symlink":
        try:
            alias.symlink_to(external, target_is_directory=True)
        except OSError as exc:
            if getattr(exc, "winerror", None) == 1314:
                pytest.skip("current Windows account lacks symlink privilege")
            raise
    else:
        linked = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(alias), str(external)],
            capture_output=True, timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        assert linked.returncode == 0, linked
    records = _settle(state, handle)
    observation = ledger.project_output_observations(records)[-1]
    assert not any(str(old) in observation.get(key, []) for key in ("created", "modified"))
    assert not finish._file_evidence(state, records, {"path": str(alias / old.name)})["passed"]
    assert not finish._producers(records, old.resolve())
    assert old.read_text() == "pre-existing input, not a new result"


def test_large_direct_name_facts_use_the_existing_immutable_observation_reference(state):
    root = state.root / "outputs/experiment/runtime"
    root.mkdir(parents=True, exist_ok=True)
    for number in range(260):
        (root / f"old-{number:03}").write_text("unchanged")
    handle = _bounded_action(state, root)
    records = _settle(state, handle)
    stored = next(row for row in records if row.get("kind") == "observation")
    assert stored["observation_ref"]
    assert stored["coverage_summary_truncated"]
    complete = ledger.project_output_observations(records)[-1]
    names = [row["entry_names"] for row in complete["coverage"]["before"]
             if (row.get("status") == "direct_complete" or row.get("direct_complete"))
             and Path(row["root"]) == root]
    assert {f"old-{number:03}" for number in range(260)} <= set(names[0])
    assert not complete["created"]
    assert not complete["modified"]
    summary_before = {"entries": {}, "truncated": True,
                      "root_coverage": stored["coverage"]["before"]}
    assert not capture._absence_known(summary_before, str(root / "old-259"))


@pytest.mark.parametrize("damage", ["missing", "hash_mismatch"])
def test_lost_name_reference_never_turns_a_partial_listing_into_a_producer(state, damage):
    root = state.root / "outputs/experiment/runtime"
    root.mkdir(parents=True, exist_ok=True)
    for number in range(260):
        (root / f"old-{number:03}").write_text("existing")
    handle = _bounded_action(state, root)
    target = root / "new-output"
    target.write_text("new actual data")
    records = _settle(state, handle)
    stored = next(row for row in records if row.get("kind") == "observation")
    assert stored["coverage_summary_truncated"] and stored["observation_ref"]
    assert finish._file_evidence(state, records, {"path": str(target)})["passed"]
    directory = Path(next(row for row in records if row.get("kind") == "action_started")["capture_record"])
    ref_path = directory / stored["observation_ref"]["file"]
    if damage == "missing":
        ref_path.unlink()
    else:
        ref_path.write_bytes(ref_path.read_bytes() + b" ")
    projected = ledger.project_output_observations(records)[-1]
    assert projected["observation_error"]
    assert str(target) in projected["unknown"]
    assert not projected["created"]
    assert not finish._file_evidence(state, records, {"path": str(target)})["passed"]


def test_real_file_child_process_is_captured_beside_a_deep_library(state, monkeypatch):
    root = state.root / "outputs/experiment/runtime"
    root.mkdir(parents=True, exist_ok=True)
    _, _, output = _layout(root)
    output.parent.rmdir()
    _bound_snapshots(monkeypatch)
    handle = _bounded_action(state, root)
    child = subprocess.run(
        [sys.executable, "-c",
         "from pathlib import Path; import sys; p=Path(sys.argv[1]); "
         "p.parent.mkdir(); p.write_bytes(b'actual child file\\n'); print('FILE_PROBE_OK')",
         str(output)],
        capture_output=True, text=True, timeout=10,
    )
    assert child.returncode == 0 and child.stdout.strip() == "FILE_PROBE_OK", child
    assert output.read_bytes() == b"actual child file\n"
    records = _settle(state, handle)
    assert ledger.project_output_observations(records)[-1]["truncated"]
    assert finish._file_evidence(state, records, {"path": str(output)})["passed"]


def _disjoint_roots(root, tree_kind):
    dependencies = root / "A-dependencies"
    deep = dependencies if tree_kind == "wide" else dependencies / "Lib/site-packages/dense/deeper"
    deep.mkdir(parents=True)
    for number in range(80):
        (deep / f"dependency-{number:03}").write_text("existing dependency")
    outputs = root / "B-small-output"
    outputs.mkdir()
    return dependencies, outputs


@pytest.mark.parametrize("tree_kind", ["deep", "wide"])
@pytest.mark.parametrize("reverse", [False, True])
def test_disjoint_roots_share_the_common_budget_without_starving_small_output(tmp_path, tree_kind, reverse):
    dependencies, outputs = _disjoint_roots(tmp_path, tree_kind)
    roots = [outputs, dependencies] if reverse else [dependencies, outputs]
    target = outputs / "result.dat"
    before = capture.snapshot([], roots, max_files=20, max_seconds=60, include_directories=True)
    target.write_text("actual small result")
    after = capture.snapshot([], roots, max_files=20, max_seconds=60, include_directories=True)
    assert before["truncated"] and after["truncated"]
    assert before["scanned"] <= 20 and after["scanned"] <= 20
    assert str(target) in after["entries"]
    assert str(target) in capture.diff(before, after)["created"]
    large_coverage = next(row for row in after["root_coverage"] if row["root"] == str(dependencies))
    assert large_coverage["status"] == "truncated"


@pytest.mark.parametrize("tree_kind", ["deep", "wide"])
@pytest.mark.parametrize("reverse", [False, True])
def test_multiroot_real_action_retains_its_small_producer(state, monkeypatch, tree_kind, reverse):
    root = state.root / "outputs/experiment/runtime"
    root.mkdir(parents=True, exist_ok=True)
    dependencies, outputs = _disjoint_roots(root, tree_kind)
    roots = [outputs, dependencies] if reverse else [dependencies, outputs]
    _bound_snapshots(monkeypatch)
    handle = action_boundary.begin(
        state, tool="multiroot-file-probe", work_root=str(root),
        observation_boundary={"roots": list(map(str, roots)), "excluded_roots": []},
    )
    assert isinstance(handle, action_boundary.ActionHandle)
    target = outputs / "result.dat"
    target.write_text("actual small output in the second approved root")
    records = _settle(state, handle)
    assert ledger.project_output_observations(records)[-1]["truncated"]
    assert finish._file_evidence(state, records, {"path": str(target)})["passed"]


@pytest.mark.parametrize("shape", ["root_coverage", "baseline_root_coverage", "both", "mixed"])
def test_managed_coverage_shapes_reuse_full_reference_and_partial_summary(state, shape):
    root = state.root / "outputs/experiment/runtime"
    root.mkdir(parents=True, exist_ok=True)
    rows = [{"root": str(root), "status": "complete"},
            {"root": str(root), "status": "complete", "direct_complete": True,
             "entry_names": [f"old-{number:03}" for number in range(260)]}]
    observation = {"created": [], "modified": [], "deleted": [], "unknown": [],
                   "identities": {}, "identity_basis": {}, "truncated": False}
    keys = [shape] if shape in {"root_coverage", "baseline_root_coverage"} else [
        "root_coverage", "baseline_root_coverage"]
    observation.update({key: rows for key in keys})
    if shape == "mixed":
        observation["coverage"] = {"before": rows, "after": rows}
    action_id = "managed-shape-" + shape
    summary = capture.persist_observation(state, action_id, observation)
    assert summary["observation_ref"]
    assert summary["coverage_summary_truncated"]
    for key in keys:
        assert summary[key][1]["status"] == "direct_partial"
        assert len(summary[key][1]["entry_names"]) <= 200
        assert not capture._absence_known(
            {"entries": {}, "truncated": True, "root_coverage": summary[key]},
            str(root / "old-259"),
        )
    records = [
        {"kind": "action_started", "action_id": action_id,
         "capture_record": str(capture.allocate_record_dir(state, action_id))},
        {"kind": "observation", "action_id": action_id, **summary},
    ]
    restored = ledger.project_output_observations(records)[-1]
    assert all(restored[key] == rows for key in keys)
    if shape == "mixed":
        assert restored["coverage"] == observation["coverage"]


@pytest.mark.parametrize("damage", [None, "missing", "hash_mismatch"])
def test_real_managed_terminal_spills_names_and_requires_valid_reference(state, damage):
    root = state.root / "outputs/experiment/runtime/managed"
    root.mkdir(parents=True, exist_ok=True)
    for number in range(260):
        (root / f"old-{number:03}").write_text("existing")
    nonce = "managed-coverage-names"
    ledger.record(state, "intent", nonce=nonce, output_root=str(root), output_roots=[str(root)])
    baseline = job_settlement.capture_submission_outputs(state, nonce)
    ledger.record(state, "submission_attempted", nonce=nonce, output_baseline=baseline)
    target = root / "new-output"
    target.write_text("actual terminal output")
    settled = job_settlement.settle_terminal(
        state, {"submission_nonce": nonce, "scheduler": "local", "job_id": "test-managed-job"},
        evidence={"sandbox_status": "exited", "exit_code": 0},
    )
    assert settled["settled"]
    terminal = settled["record"]
    summary = terminal["output_diff"]
    assert summary["observation_ref"]
    assert summary["coverage_summary_truncated"]
    assert any(row.get("status") == "direct_partial" for row in summary["root_coverage"])
    records = ledger.open_ledger(state).records()
    assert finish._file_evidence(state, records, {"path": str(target)})["passed"]
    if damage:
        ref_path = Path(terminal["capture_record"]) / summary["observation_ref"]["file"]
        if damage == "missing":
            ref_path.unlink()
        else:
            ref_path.write_bytes(ref_path.read_bytes() + b" ")
        projected = ledger.project_output_observations(records)[-1]
        assert projected["observation_error"]
        assert str(target) in projected["unknown"]
        assert not finish._file_evidence(state, records, {"path": str(target)})["passed"]


def _coverage_lists(observation):
    return [*(observation.get("coverage") or {}).values(),
            *(observation.get(key) or [] for key in ("root_coverage", "baseline_root_coverage"))]


def _coverage_counts(observation):
    lists = _coverage_lists(observation)
    return sum(map(len, lists)), sum(len(row.get("entry_names") or []) for rows in lists for row in rows)


@pytest.mark.parametrize("shape", ["action", "managed", "mixed"])
@pytest.mark.parametrize("character", ["x", "测"])
def test_coverage_summary_shares_one_name_budget_across_directories_and_shapes(state, shape, character):
    root = state.root / "outputs/experiment/runtime"
    rows = [{"root": str(root / f"directory-{directory:03}"), "status": "complete",
             "direct_complete": True,
             "entry_names": [f"{number:03}-" + character * 226 for number in range(64)]}
            for directory in range(12)]
    observation = {"created": [], "modified": [], "deleted": [], "unknown": [],
                   "identities": {}, "identity_basis": {}, "truncated": False}
    if shape in {"action", "mixed"}:
        observation["coverage"] = {"before": rows, "after": rows}
    if shape in {"managed", "mixed"}:
        observation.update(root_coverage=rows, baseline_root_coverage=rows)
    action_id = "spread-names-" + shape + ("-unicode" if character != "x" else "-ascii")
    summary = capture.persist_observation(state, action_id, observation)
    assert summary["observation_ref"] and summary["coverage_summary_truncated"]
    displayed_rows, displayed_names = _coverage_counts(summary)
    assert displayed_rows <= capture.OBSERVATION_SUMMARY_LIMIT
    assert displayed_names <= capture.OBSERVATION_SUMMARY_LIMIT
    assert len(__import__("json").dumps(summary, ensure_ascii=True).encode()) < 1_000_000
    for group in _coverage_lists(summary):
        for row in group:
            assert row["status"] == "direct_partial" and row["direct_complete"] is False
    restored = capture.read_observation(
        capture.allocate_record_dir(state, action_id), summary["observation_ref"], action_id)
    assert restored == observation


@pytest.mark.parametrize("shape", ["managed", "mixed"])
def test_coverage_rows_share_one_display_budget_across_shapes(state, shape):
    root = state.root / "outputs/experiment/runtime"
    rows = [{"root": str(root / f"directory-{number:03}"), "status": "complete",
             "direct_complete": True, "entry_names": []} for number in range(150)]
    observation = {"created": [], "modified": [], "deleted": [], "unknown": [],
                   "identities": {}, "identity_basis": {}, "truncated": False,
                   "root_coverage": rows, "baseline_root_coverage": rows}
    if shape == "mixed":
        observation["coverage"] = {"before": rows, "after": rows}
    summary = capture.persist_observation(state, "spread-rows-" + shape, observation)
    assert summary["observation_ref"]
    assert _coverage_counts(summary)[0] <= capture.OBSERVATION_SUMMARY_LIMIT


@pytest.mark.parametrize("damage", [None, "missing", "hash_mismatch"])
def test_multidirectory_reference_restores_real_producer_without_large_inline_lists(state, damage):
    root = state.root / "outputs/experiment/runtime/spread"
    root.mkdir(parents=True, exist_ok=True)
    for directory in range(8):
        folder = root / f"d-{directory}"
        folder.mkdir()
        for number in range(40):
            (folder / f"old-{number:03}.dat").write_text("unchanged")
    handle = _bounded_action(state, root)
    target = root / "actual-new-output"
    target.write_text("real action output")
    records = _settle(state, handle)
    stored = next(row for row in records if row.get("kind") == "observation")
    assert stored["observation_ref"] and stored["coverage_summary_truncated"]
    assert _coverage_counts(stored)[1] <= capture.OBSERVATION_SUMMARY_LIMIT
    assert finish._file_evidence(state, records, {"path": str(target)})["passed"]
    projected = ledger.project_output_observations(records)[-1]
    assert _coverage_counts(projected)[1] > capture.OBSERVATION_SUMMARY_LIMIT
    if damage:
        ref_path = Path(next(row for row in records if row.get("kind") == "action_started")[
            "capture_record"]) / stored["observation_ref"]["file"]
        if damage == "missing":
            ref_path.unlink()
        else:
            ref_path.write_bytes(ref_path.read_bytes() + b" ")
        projected = ledger.project_output_observations(records)[-1]
        assert projected["observation_error"] and str(target) in projected["unknown"]
        assert not finish._file_evidence(state, records, {"path": str(target)})["passed"]

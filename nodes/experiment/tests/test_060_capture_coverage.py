"""Bounded observations must distinguish absence from unobserved paths."""
from pathlib import Path

import pytest

from nodes.experiment.tools import capture


def test_failed_root_probe_does_not_prove_prelaunch_absence(tmp_path, monkeypatch):
    root = tmp_path / "outputs"
    root.mkdir()
    old = root / "old.dat"
    old.write_text("existing")
    original = Path.stat

    def denied(path, *args, **kwargs):
        if path == root:
            raise PermissionError("injected root probe failure")
        return original(path, *args, **kwargs)

    with monkeypatch.context() as scoped:
        scoped.setattr(Path, "stat", denied)
        before = capture.snapshot([], [root])
    after = capture.snapshot([], [root])
    observation = capture.diff(before, after)
    assert before["root_coverage"][0]["status"] == "error"
    assert str(old) in observation["unknown"]
    assert str(old) not in observation["created"]


def test_unknown_path_query_does_not_report_unchanged(tmp_path):
    path = tmp_path / "result.dat"
    assert capture.written_in_action(
        {"created": [], "modified": [], "unknown": [str(path)], "truncated": False},
        path,
    ) == "unknown"


@pytest.mark.parametrize("subtree_kind", ["excluded", "symlink"])
def test_unvisited_subtree_does_not_prove_absence(tmp_path, subtree_kind):
    root = tmp_path / "outputs"
    root.mkdir()
    if subtree_kind == "excluded":
        subtree = root / ".git"
        subtree.mkdir()
    else:
        external = tmp_path / "external"
        external.mkdir()
        subtree = root / "linked"
        subtree.symlink_to(external, target_is_directory=True)
    old = subtree / "existing.dat"
    old.write_text("predates the submission")
    before = capture.snapshot([], [root])
    after = capture.snapshot([old], [root])
    observation = capture.diff(before, after)
    assert str(old) in observation["unknown"]
    assert str(old) not in observation["created"]


def test_complete_root_proves_new_sibling_even_if_another_subtree_is_excluded(tmp_path):
    root = tmp_path / "outputs"
    root.mkdir()
    (root / ".git").mkdir()
    before = capture.snapshot([], [root])
    new = root / "result.dat"
    new.write_text("new result")
    observation = capture.diff(before, capture.snapshot([], [root]))
    assert str(new) in observation["created"]
    assert str(new) not in observation["unknown"]

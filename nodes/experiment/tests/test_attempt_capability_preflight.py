"""Regression coverage for inherited RunAttempt capabilities.

Experiment may diagnose an immutable capability mismatch, but must never widen
or replace a Core-owned manifest. The fixture deliberately contains a parent
attempt manifest and a distinct child Experiment state.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from core import sandbox
from core.state import State
from nodes.experiment.tools import safe_bash as sb


def _child_with_parent_manifest(tmp_path: Path) -> State:
    child_root = tmp_path / "runs" / "child-run"
    child_root.mkdir(parents=True)
    workspace = tmp_path / "project" / "experiments"
    workspace.mkdir(parents=True)
    parent_root = tmp_path / "runs" / "parent-run"
    parent_root.mkdir(parents=True)
    project_root = workspace.parent

    state = State(run_id="child-run", node_type="experiment", root=child_root)
    state.project_worktree = project_root
    state.workspace_root = workspace
    state.workspace_relative_path = "experiment"

    mounts = tuple(sorted(
        ((str(project_root), "ro"), (str(parent_root), "rw")),
        key=lambda item: (len(Path(item[0]).parts), item[0]),
    ))
    parent = sandbox.SandboxManifest(
        attempt_id="local:parent-attempt",
        run_id="parent-run",
        mounts=mounts,
        image_id="sha256:" + "0" * 64,
        security_profile="hardened",
    )
    state.sandbox_manifest = parent.canonical_payload()
    state.sandbox_manifest_hash = parent.sha256
    return state


def test_inherited_parent_manifest_is_terminal_core_blocker_before_payload(
        tmp_path: Path, monkeypatch):
    state = _child_with_parent_manifest(tmp_path)
    spawned: list[tuple[object, ...]] = []

    capability_gap = sb._frozen_attempt_capability_gap(state)

    assert capability_gap is not None
    assert capability_gap["manifest_run_id"] == "parent-run"
    assert capability_gap["state_run_id"] == "child-run"
    assert capability_gap["missing_writable_roles"] == ["workspace_root", "run_root"]
    # Diagnostics are structured but do not leak host paths.
    assert str(state.root) not in json.dumps(capability_gap)

    monkeypatch.setattr(
        sb, "resolve_required_workdir",
        lambda *_args, **_kwargs: (str(state.root), None),
    )

    async def must_not_spawn(*args, **kwargs):
        spawned.append(args)
        raise AssertionError("payload must not start for an inherited manifest")

    monkeypatch.setattr(sb, "spawn_and_wait", must_not_spawn)
    result = asyncio.run(sb._exec_and_log(
        state, "printf should-not-run", sandbox_roots=([state.root], []),
    ))

    assert spawned == []
    assert result["reason"] == "sandbox_capability_not_frozen"
    assert result["blocker"]["suggested_owner"] == "core"
    assert result["blocker"]["node_action"] == (
        "core_reissue_child_attempt_with_bound_workspace_and_run_root"
    )
    assert result["blocker"]["retry_policy"] == "do_not_retry_same_child_run"
    assert result["blocker"]["capability_diff"] == capability_gap
    assert "start_new_run_with_required_capabilities" not in json.dumps(result)


def test_child_scoped_manifest_has_no_capability_gap(tmp_path: Path):
    state = _child_with_parent_manifest(tmp_path)
    workspace = Path(state.workspace_root)
    project_root = Path(state.project_worktree)
    mounts = tuple(sorted(
        (
            (str(project_root), "ro"),
            (str(workspace), "rw"),
            (str(state.root), "rw"),
        ),
        key=lambda item: (len(Path(item[0]).parts), item[0]),
    ))
    child = sandbox.SandboxManifest(
        attempt_id="local:child-attempt",
        run_id=state.run_id,
        mounts=mounts,
        image_id="sha256:" + "1" * 64,
        security_profile="hardened",
    )
    state.sandbox_manifest = child.canonical_payload()
    state.sandbox_manifest_hash = child.sha256

    assert sb._frozen_attempt_capability_gap(state) is None

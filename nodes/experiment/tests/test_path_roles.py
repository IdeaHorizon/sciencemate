from __future__ import annotations

from nodes.experiment.tools.build_contract import validate_contract
from nodes.experiment.tools import path_roles
from nodes.experiment.tools.path_roles import _iter_role_values
from nodes.experiment.tools.path_roles import collect_path_roles
from nodes.experiment.tools.path_roles import matching_path_roles
from nodes.experiment.tools.path_roles import validate_path_roles
from core.state import State


def test_nested_build_contract_exposes_build_root():
    values = list(_iter_role_values({
        "build_contract": {
            "source_baseline_root": "/tmp/source",
            "build_root": "/tmp/build",
            "run_root": "/tmp/run",
        }
    }, "test"))
    roles = {role: spec for role, spec, _source, _alias in values}
    assert roles["source_baseline_root"] == "/tmp/source"
    assert roles["build_root"] == "/tmp/build"
    assert roles["run_root"] == "/tmp/run"


def test_agent_declared_route_never_grants_path_roles():
    class State:
        hook_state = {"path_roles": {
            "run_root": {"path": "/tmp/run", "writable": True},
            "build_root": {"path": "/tmp/run/build", "writable": True},
        }}

        def list_artifacts(self):
            return [{"id": "route", "type": "declared_route"}]

        def read_artifact(self, _artifact_id):
            return {"metadata": {"source_path": "/tmp/run/build"},
                    "content": "{\"source_path\": \"/tmp/run/build\"}"}

    roles = collect_path_roles(State(), include_runtime=False)
    assert not any(r.path == "/tmp/run/build" and r.role == "source_baseline_root"
                   for r in roles)


def test_trusted_source_path_alias_means_immutable_baseline():
    class State:
        hook_state = {"node_inputs": {"source_path": "/tmp/source"}}

    roles = collect_path_roles(State(), include_runtime=False)
    baseline = next(r for r in roles if r.role == "source_baseline_root")
    assert baseline.path == "/tmp/source"
    assert baseline.writable is False
    assert baseline.legacy_alias == "source_path"


def test_build_contract_rejects_nested_build_under_source_alias():
    contract = {
        "route_type": "official_build_system",
        "activities": {"uses_source": True, "compile": True, "run": False},
        "source_path": "/tmp/source",
        "build_dir": "/tmp/source/build",
        "env_domains": {
            "compiler": {"selected": "gcc", "probes": ["gcc --version"]},
            "build_discovery": {"selected": "cmake", "probes": ["cmake --version"]},
        },
        "expected_artifacts": [{"path": "/tmp/source/build/app", "type": "executable"}],
        "cache_invalidation": {"clean_on_toolchain_change": True},
    }
    result = validate_contract(contract)
    assert result["valid"] is False
    assert "source_baseline_root and build_root must not overlap" in result["errors"]


def test_baseline_cannot_overlap_build_root():
    class State:
        hook_state = {"path_roles": {
            "source_baseline_root": "/tmp/gromacs-build",
            "build_root": {"path": "/tmp/gromacs-build", "writable": True},
            "run_root": {"path": "/tmp/gromacs-run", "writable": True},
        }}

        def list_artifacts(self):
            return []

    result = validate_path_roles(State())
    assert result["valid"] is False
    assert any("build_root" in error for error in result["errors"])


def test_every_run_has_a_managed_source_default_without_upstream_stage_flags(tmp_path):
    state = State.new(
        node_type="experiment",
        base_dir=tmp_path / "runs",
        project_id="managed-source-default",
    )

    managed = [
        role for role in collect_path_roles(state)
        if role.role == "managed_source_root"
    ]

    assert len(managed) == 1
    assert managed[0].writable is True
    assert managed[0].source == "framework:experiment_managed_source"
    assert managed[0].path.endswith("/outputs/experiment/runtime/source")



def test_normalize_path_accepts_windows_absolute_roots():
    """A Windows drive / UNC root is absolute on any host.

    Regression: a POSIX-only anchor check (``startswith(('/', '~'))``) rejected
    every ``C:\\…`` path, so on Windows the whole role contract normalised to
    nothing — the experiment node declared *zero* writable roots and the scope
    guard refused every scheduler write (``declared_usable_roots: []``).  The
    predicate must recognise Windows roots even when this test runs on POSIX,
    so it cannot lean on host-specific ``os.path.isabs``.
    """
    _normalize_path = path_roles._normalize_path
    # Drive (back- and forward-slash) and UNC roots are accepted.
    assert _normalize_path(r"C:\Users\fcbay\afs\runs\x\runtime") is not None
    assert _normalize_path("C:/Users/fcbay/afs/runs/x/build") is not None
    assert _normalize_path(r"\\host\share\dir") is not None
    # A drive letter with no separator is drive-relative, not absolute.
    assert _normalize_path("C:relative") is None
    # Relative strings and bare role names stay rejected.
    assert _normalize_path("output/experiment/runtime") is None
    assert _normalize_path("run_root") is None
    # POSIX anchors are unchanged.
    assert _normalize_path("/tmp/run") is not None
    assert _normalize_path("~/afs/run") is not None


def test_windows_run_still_gets_default_writable_roots(monkeypatch):
    """On Windows the framework run roots must still be declared.

    ``experiment_output_dir`` returns ``C:\\…`` paths on Windows; before the fix
    ``_normalize_path`` rejected each one and the default run_root / build_root /
    managed_source_root loop silently ``continue``d past every one — leaving the
    node with no writable role and every ``submit_job`` write refused.  This
    reproduces the Windows path shape on any host via ``PureWindowsPath``.
    """
    from pathlib import PureWindowsPath

    def fake_output_dir(state, kind="", *, create=False):
        base = PureWindowsPath(r"C:\Users\fcbay\AppData\Local\afs\runs\x")
        return base / kind if kind else base

    monkeypatch.setattr(path_roles, "experiment_output_dir", fake_output_dir)

    class WinState:
        hook_state: dict = {}
        workspace_root = r"C:\Users\fcbay\AppData\Local\afs\projects\p\experiment"

    declared = {
        role.role for role in collect_path_roles(WinState())
        if role.writable and not role.container_only
    }
    assert {"run_root", "build_root", "managed_source_root",
            "workspace_root"} <= declared


def test_relative_run_root_is_rejected_and_never_matches_absolute_workdir(tmp_path, monkeypatch):
    """A relative declaration must never inherit authority from the CWD."""
    monkeypatch.chdir(tmp_path)
    run_root = tmp_path / "output" / "experiment" / "runtime"
    run_root.mkdir(parents=True)

    class State:
        hook_state = {"path_roles": {
            "run_root": {"path": "output/experiment/runtime", "writable": True},
        }}

        def list_artifacts(self):
            return []

    assert matching_path_roles(run_root / "qe", State()) == []
    report = validate_path_roles(State())
    assert any("not an absolute path" in warning for warning in report["warnings"])


def test_matching_path_roles_normalizes_a_legacy_relative_root(tmp_path, monkeypatch):
    """Matching is defensive when a legacy role escaped declaration normalization."""
    monkeypatch.chdir(tmp_path)
    run_root = tmp_path / "output" / "experiment" / "runtime"
    run_root.mkdir(parents=True)
    legacy_role = path_roles.PathRole(
        role="run_root", path="output/experiment/runtime", writable=True,
        container_only=False, patch_tracked=False, source="legacy-test",
    )
    monkeypatch.setattr(path_roles, "collect_path_roles", lambda _state: [legacy_role])

    matches = path_roles.matching_path_roles(run_root / "qe", object())
    assert [(role.role, role.writable) for role in matches] == [("run_root", True)]


def test_sandbox_project_root_does_not_invent_a_conflicting_workspace(tmp_path):
    """Sandbox runs use run-local outputs until a worktree is actually bound."""
    class State:
        workspace_root = None
        project_worktree = None
        project_root = tmp_path / "project-memory-only"

    assert path_roles.project_workspace_dir(State()) is None

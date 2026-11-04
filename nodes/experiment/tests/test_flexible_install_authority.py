"""External installation paths consume existing Core authority, never role claims."""
from __future__ import annotations

import asyncio
import copy
import os
import shlex
import sys
from pathlib import Path

import pytest

from core import isolation, sandbox
from core.state import State
from nodes.experiment.tools import ledger, run_bootstrap, safe_bash as sb
from nodes.experiment.tools.subprocess_policy import bash_sandbox_roots
from shared.lib.cancellable_subprocess import spawn_and_wait


@pytest.fixture
def installation(tmp_path):
    state = State.new("experiment", tmp_path / "runs")
    project = tmp_path / "project"
    workspace = project / "experiments"
    workspace.mkdir(parents=True)
    external = tmp_path / "user-environment"
    external.mkdir()
    state.project_worktree = project
    state.workspace_root = workspace
    state.workspace_relative_path = "experiments"
    state.hook_state["path_roles"] = {
        "dependency_root": {"path": str(external), "writable": True},
    }
    return state, external


def _freeze(state, external, *, readonly=(), grant=True):
    """A trusted test fixture supplies the same immutable Core capability shape."""
    modes = {
        str(Path(state.project_worktree).resolve()): "ro",
        str(Path(state.workspace_root).resolve()): "rw",
        str(Path(state.root).resolve()): "rw",
        str(Path(sb.__file__).resolve().parents[3]): "ro",
    }
    if grant:
        modes[str(external.resolve())] = "rw"
    modes.update({str(Path(path).resolve()): "ro" for path in readonly})
    manifest = sandbox.SandboxManifest(
        attempt_id="flexible-install-fixture",
        # Covered child runs may legitimately share their parent's attempt.
        run_id="a-shared-platform-run",
        mounts=tuple(sorted(modes.items(), key=lambda item: (len(Path(item[0]).parts), item[0]))),
        backend=isolation.native_backend_name() or "auto",
    )
    manifest.validate()
    state.sandbox_manifest = manifest.canonical_payload()
    state.sandbox_manifest_hash = manifest.sha256
    return manifest


def test_core_granted_external_dependency_is_usable_without_another_approval(installation):
    state, external = installation
    _freeze(state, external)
    target = external / "new-package" / "result.txt"

    assert sb._classify_write_path(target, state)[0] == "safe"
    assert not target.parent.exists(), "classification must not materialize an install directory"
    assert sb._bash_path_effects_guard(
        state, "touch result.txt", cwd=str(external), allow_authorization=False,
    ) is None
    writable, readonly = bash_sandbox_roots(state, str(external), authorized_targets=[str(target)])
    assert external.resolve() in writable
    sandbox.manifest_for(state, writable_roots=writable, readonly_roots=readonly)


@pytest.mark.parametrize("forged", ["role", "approved_role", "approved_capability"])
def test_unfrozen_role_claims_cannot_grant_an_external_install_path(installation, forged):
    state, external = installation
    if forged != "role":
        state.hook_state["path_roles"] = {
            "approved_write_root": {"path": str(external), "authority": "human"},
        }
    if forged == "approved_capability":
        state.hook_state["_approved_subprocess_write_roots"] = [str(external)]
    assert sb._classify_write_path(external / "x", state)[0] != "safe"
    assert state.sandbox_manifest is None
    assert state.sandbox_manifest_hash is None


@pytest.mark.parametrize("defect", ["not_granted", "bad_hash", "missing_hash", "malformed"])
def test_invalid_or_ungranted_manifest_cannot_authorize_installation(installation, defect):
    state, external = installation
    _freeze(state, external, grant=defect != "not_granted")
    if defect == "bad_hash":
        state.sandbox_manifest_hash = "0" * 64
    elif defect == "missing_hash":
        state.sandbox_manifest_hash = None
    elif defect == "malformed":
        state.sandbox_manifest["version"] = "invalid"
    before = copy.deepcopy((state.sandbox_manifest, state.sandbox_manifest_hash))
    assert sb._classify_write_path(external / "x", state)[0] != "safe"
    assert (state.sandbox_manifest, state.sandbox_manifest_hash) == before


def test_more_specific_core_readonly_mount_still_blocks_writable_role(installation):
    state, external = installation
    protected = external / "existing-environment"
    protected.mkdir()
    original = protected / "keep.txt"
    original.write_text("keep", encoding="utf-8")
    _freeze(state, external, readonly=[protected])
    assert sb._classify_write_path(original, state)[0] != "safe"
    assert sb._classify_write_path(protected / "new-file", state)[0] != "safe"
    assert original.read_text(encoding="utf-8") == "keep"


def test_core_grant_does_not_expand_to_parent_or_sibling(installation):
    state, external = installation
    _freeze(state, external)
    for broader in (external.parent, external.with_name(external.name + "-sibling")):
        state.hook_state["path_roles"] = {
            "dependency_root": {"path": str(broader), "writable": True},
        }
        assert sb._classify_write_path(broader / "x", state)[0] != "safe"


@pytest.mark.parametrize("variable", ["CONDA_PREFIX", "VIRTUAL_ENV"])
def test_core_granted_personal_environment_is_not_a_global_install_block(installation, monkeypatch, variable):
    state, external = installation
    _freeze(state, external)
    monkeypatch.setenv(variable, str(external))
    assert sb._classify_write_path(external / "new-package", state)[0] == "safe"
    state.sandbox_manifest_hash = "0" * 64
    assert sb._classify_write_path(external / "new-package", state)[0] != "safe"


@pytest.mark.parametrize("prefix", sorted({sys.prefix, sys.base_prefix}))
def test_core_grant_cannot_authorize_changes_to_the_running_framework_python(installation, monkeypatch, prefix):
    state, _external = installation
    # A disposable prefix exercises the node's protection even on systems
    # where Core already refuses a host-wide prefix such as /usr.
    runtime = _external / "framework-runtime"
    runtime.mkdir()
    for name in ("prefix", "base_prefix", "exec_prefix", "base_exec_prefix"):
        if getattr(sys, name) == prefix:
            monkeypatch.setattr(sys, name, str(runtime))
    _freeze(state, runtime)
    state.hook_state["path_roles"] = {
        "dependency_root": {"path": str(runtime), "writable": True},
    }
    assert sb._classify_write_path(runtime / "do-not-install", state)[0] != "safe"


def test_external_grant_does_not_expose_framework_state_or_source(installation):
    state, external = installation
    repo = Path(sb.__file__).resolve().parents[3]
    git = external / ".git"
    git.mkdir()
    for protected in (Path(state.root), repo, git):
        _freeze(state, protected)
        state.hook_state["path_roles"] = {
            "build_root": {"path": str(protected), "writable": True},
        }
        assert sb._classify_write_path(protected / "must-not-write", state)[0] != "safe"


def test_symlink_cannot_redirect_external_grant_to_ungranted_directory(installation):
    state, external = installation
    outside = external.parent / "ungranted"
    outside.mkdir()
    alias = external / "alias"
    try:
        alias.symlink_to(outside, target_is_directory=True)
    except OSError as error:
        if os.name != "nt" or getattr(error, "winerror", None) != 1314:
            raise
        import _winapi
        _winapi.CreateJunction(str(outside), str(alias))
    _freeze(state, external)
    # A lexical child role cannot widen the Core root through its resolved alias.
    state.hook_state["path_roles"] = {
        "dependency_root": {"path": str(alias), "writable": True},
    }
    assert sb._classify_write_path(alias / "escaped.txt", state)[0] != "safe"
    assert not (outside / "escaped.txt").exists()


@pytest.mark.parametrize("blocked_location", ["sibling", "readonly_child", "runtime_child"])
def test_native_payload_consumes_external_grant_without_widening_it(installation, monkeypatch, blocked_location):
    state, external = installation
    allowed = external / "allowed-package"
    allowed.mkdir()
    protected = external / "existing-environment"
    protected.mkdir()
    _freeze(state, external, readonly=[protected] if blocked_location == "readonly_child" else [])
    assert sb._classify_write_path(allowed / "installed.txt", state)[0] == "safe"
    backend_name = isolation.native_backend_name()
    if backend_name is None:
        pytest.skip("this platform has no native execution backend")
    backend = isolation.select_backend(backend_name)
    if isolation.Invariant.WRITE_BOUNDARY not in backend.capabilities():
        pytest.skip("native backend cannot enforce its write boundary on this host")
    monkeypatch.setenv(isolation.EXECUTOR_ENV, backend_name)
    if blocked_location == "runtime_child":
        # A disposable interpreter prefix represents ScienceMate's runtime;
        # the test never writes to the real Python installation.
        for name in ("prefix", "base_prefix", "exec_prefix", "base_exec_prefix"):
            monkeypatch.setattr(sys, name, str(protected))
    sibling = external.parent / "ungranted-sibling"
    sibling.mkdir()
    target = allowed / "installed.txt"
    forbidden = (protected if blocked_location != "sibling" else sibling) / "must-not-exist.txt"
    program = (
        "from pathlib import Path\n"
        f"Path({str(target)!r}).write_text('installed', encoding='utf-8')\n"
        "try:\n"
        f"    Path({str(forbidden)!r}).write_text('forbidden', encoding='utf-8')\n"
        "except PermissionError:\n"
        "    pass\n"
        "else:\n"
        "    raise RuntimeError('ungranted sibling was writable')\n"
    )
    writable, readonly = sb._sandbox_roots_for_payload(
        state, str(external), sandbox_profile="bash", authorized_targets=[str(target)])
    sandbox.manifest_for(state, writable_roots=writable, readonly_roots=readonly)
    status, code, _stdout, stderr = asyncio.run(spawn_and_wait(
        sys.executable, "-I", "-B", "-c", program, state=state, timeout=30,
        cwd=str(external), writable_roots=writable, readonly_roots=readonly,
    ))
    assert (status, code) == ("done", 0), stderr
    assert target.read_text(encoding="utf-8") == "installed"
    assert not forbidden.exists()


def test_nested_readonly_fails_closed_without_backend_carveout_support(installation, monkeypatch):
    state, external = installation
    protected = external / "existing-environment"
    protected.mkdir()
    for index in range(513):
        (external / f"existing-{index}").touch()
    _freeze(state, external, readonly=[protected])

    monkeypatch.setattr(isolation, "enforcement_snapshot", lambda: {
        "enforced": [isolation.Invariant.WRITE_BOUNDARY.value],
    })
    with pytest.raises(ValueError, match="readonly_carveout_unavailable"):
        sb._sandbox_roots_for_payload(
            state, str(external), sandbox_profile="bash", authorized_targets=[])


@pytest.mark.parametrize("declare_role", [True, False])
def test_real_safe_run_bash_captures_core_authorized_external_prefix(installation, declare_role):
    state, external = installation
    if not declare_role:
        state.hook_state.pop("path_roles")
    backend_name = isolation.native_backend_name()
    if backend_name is None or isolation.Invariant.WRITE_BOUNDARY not in isolation.select_backend(backend_name).capabilities():
        pytest.skip("native backend cannot enforce its write boundary on this host")
    state.hook_state["node_inputs"] = {
        "experiment_spec": "Write installed to the user-selected external prefix.",
        "prereg_assignment": {"kind": "none", "reason": "bounded install boundary test"},
    }
    asyncio.run(run_bootstrap.establish(state))
    assert ledger.bootstrap_phase(state) == "ready"
    _freeze(state, external)
    target = external / "installed.txt"
    command = "printf installed > " + shlex.quote(target.as_posix())
    result = asyncio.run(sb._safe_run_bash(
        state, cmd=command, cwd=str(state.workspace_root), timeout=30,
    ))
    assert result.get("returncode") == 0, result
    assert target.read_text(encoding="utf-8") == "installed"
    observations = ledger.project_output_observations(ledger.open_ledger(state).records())
    observed = [row for row in observations if str(target) in (row.get("created") or [])]
    assert observed, "the permitted external write must participate in real output capture"
    assert observed[-1]["identities"][str(target)]["after"]["sha256"]


def test_existing_environment_read_error_never_falls_back_to_host(installation, monkeypatch):
    from nodes.experiment.tools import env_provision
    state, external = installation
    state.hook_state["node_inputs"] = {
        "experiment_spec": "Write installed to the selected prefix.",
        "prereg_assignment": {"kind": "none", "reason": "environment failure regression"},
    }
    asyncio.run(run_bootstrap.establish(state))
    _freeze(state, external)
    script = Path(state.root) / "env" / "build_env.sh"
    script.parent.mkdir()
    script.write_text("export SELECTED_ENV=1\n", encoding="utf-8")
    def unreadable(_state):
        raise PermissionError("selected build environment is unreadable")
    monkeypatch.setattr(env_provision, "existing_env_script", unreadable, raising=False)
    target = external / "must-not-exist.txt"
    result = asyncio.run(sb._safe_run_bash(
        state, cmd="printf forbidden > " + shlex.quote(target.as_posix()),
        cwd=str(state.workspace_root), timeout=30,
    ))
    assert result.get("reason") == "selected_build_environment_unavailable", result
    assert result.get("payload_spawned") is False
    assert not target.exists()


def test_recon_fingerprint_passes_state_and_selected_env_without_global_selector(installation, monkeypatch):
    from nodes.experiment.tools import repro_snapshot
    state, external = installation
    script = external / "build_env.sh"
    script.write_text("export SELECTED_ENV=1\n", encoding="utf-8")
    monkeypatch.setenv("EXPERIMENT_ACTIVE_BUILD_ENV", "unrelated-session.sh")
    calls = []

    def probe(command, **kwargs):
        calls.append(kwargs)
        assert os.environ["EXPERIMENT_ACTIVE_BUILD_ENV"] == "unrelated-session.sh"
        return "fixture-probe"

    monkeypatch.setattr(repro_snapshot, "_run", probe)
    fingerprint = sb._recon_fingerprint(str(external), env_path=str(script), state=state)
    assert fingerprint and len(calls) == 3
    assert all(call == {"env_path": str(script), "state": state} for call in calls)
    assert os.environ["EXPERIMENT_ACTIVE_BUILD_ENV"] == "unrelated-session.sh"


def test_dependency_hint_treats_project_environment_as_advice():
    hint = sb._dependency_install_hint("unknown_absolute", str(Path.home() / ".local"))
    assert "Core" in hint and "建议" in hint
    assert "不要安装到" not in hint

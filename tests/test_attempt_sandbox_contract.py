"""Pure contract tests for frozen RunAttempt capabilities (manifest v4 + legacy)."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from core import sandbox


def _manifest(tmp_path, *, attempt_id: str = "attempt-1", authorized=(), **overrides):
    fields = dict(
        attempt_id=attempt_id,
        run_id="run-1",
        mounts=((str(tmp_path.resolve()), "rw"),),
        authorized_risk_classes=tuple(authorized),
        backend="darwin",
    )
    fields.update(overrides)
    return sandbox.SandboxManifest(**fields)


def test_authorization_changes_the_capability_identity(tmp_path) -> None:
    restricted = _manifest(tmp_path)
    continuous = _manifest(tmp_path, authorized=("*",))

    restricted.validate()
    continuous.validate()
    assert restricted.sha256 != continuous.sha256


def test_backend_is_part_of_the_frozen_capability(tmp_path) -> None:
    darwin = _manifest(tmp_path, backend="darwin")
    linux = _manifest(tmp_path, backend="linux")
    darwin.validate()
    linux.validate()
    assert darwin.sha256 != linux.sha256
    assert darwin.canonical_payload()["version"] == 4
    assert "image_id" not in darwin.canonical_payload(), "v4 不再有镜像字段"


def test_legacy_manifest_keeps_its_hardened_meaning(tmp_path) -> None:
    """v1 payload 是 Docker 年代的形状：解析仍认、hash 不变、hardened 语义不变。"""
    legacy = sandbox.SandboxManifest(
        attempt_id="attempt-legacy",
        run_id="run-1",
        mounts=((str(tmp_path.resolve()), "rw"),),
        backend="image",
        version=1,
        image_id="sha256:" + "a" * 64,
        security_profile="hardened",
    ).canonical_payload()
    legacy.pop("security_profile", None)  # v1 payload 里没有这个字段

    parsed = sandbox.parse_manifest(legacy)

    assert parsed.security_profile == "hardened"
    assert parsed.backend == "image"
    assert parsed.canonical_payload() == legacy

    legacy["security_profile"] = "portable"
    with pytest.raises(sandbox.SandboxContractError, match="legacy.*hardened"):
        sandbox.parse_manifest(legacy)


def test_a_v4_manifest_cannot_name_the_removed_image_backend(tmp_path) -> None:
    with pytest.raises(sandbox.SandboxContractError, match="image backend was removed"):
        _manifest(tmp_path, backend="image").validate()


def test_authorization_manifest_is_canonical_not_order_dependent(tmp_path) -> None:
    with pytest.raises(sandbox.SandboxContractError, match="sorted and unique"):
        _manifest(tmp_path, authorized=("z", "a")).validate()


def test_operator_ceiling_is_parsed_once_as_a_validated_object(monkeypatch) -> None:
    monkeypatch.setenv(
        "HARNESS_SANDBOX_CEILING",
        json.dumps({"memory_bytes": 64 * 1024**3, "cpus": 16}),
    )

    ceiling = sandbox.SandboxCeiling.from_environment()

    assert ceiling.memory_bytes == 64 * 1024**3
    assert ceiling.cpus == 16
    assert ceiling.storage_bytes == 64 * 1024**3


def test_operator_ceiling_rejects_unknown_fields(monkeypatch) -> None:
    monkeypatch.setenv("HARNESS_SANDBOX_CEILING", '{"unbounded": true}')

    with pytest.raises(sandbox.SandboxContractError, match="unknown fields"):
        sandbox.SandboxCeiling.from_environment()


def test_operator_ceiling_must_admit_the_default_profile(monkeypatch) -> None:
    monkeypatch.setenv(
        "HARNESS_SANDBOX_CEILING",
        json.dumps({"memory_bytes": 2 * 1024**3}),
    )

    with pytest.raises(sandbox.SandboxContractError, match="default 4GiB"):
        sandbox.SandboxCeiling.from_environment()


def test_local_attempt_freezes_the_state_capability_not_a_narrow_command_root(
    tmp_path, monkeypatch
) -> None:
    from core import isolation

    class _Native:
        name = "darwin"

        def capabilities(self):
            return frozenset({isolation.Invariant.WRITE_BOUNDARY})

        def prepare(self, spec, *, state):
            raise AssertionError("not used")

    monkeypatch.setattr(isolation, "select_backend", lambda name=None: _Native())
    run_root = tmp_path / "run"
    first = run_root / "deps"
    later = run_root / "workspace"
    first.mkdir(parents=True)
    later.mkdir()
    state = SimpleNamespace(run_id="local-run", root=run_root)

    manifest = sandbox.manifest_for(state, writable_roots=[first])

    assert manifest.mounts == ((str(run_root.resolve()), "rw"),)
    assert manifest.backend == "darwin" and manifest.version == 4
    assert state.sandbox_manifest_hash == manifest.sha256
    assert sandbox.manifest_for(state, writable_roots=[later]).sha256 == manifest.sha256


def test_most_specific_mount_mode_cannot_be_widened_by_a_writable_parent(
    tmp_path,
) -> None:
    protected = tmp_path / "protected"
    protected.mkdir()
    manifest = sandbox.SandboxManifest(
        attempt_id="attempt-specific-mode",
        run_id="run-1",
        mounts=((str(tmp_path.resolve()), "rw"), (str(protected.resolve()), "ro")),
        backend="linux",
    )
    manifest.validate()
    state = SimpleNamespace(
        sandbox_manifest=manifest.canonical_payload(),
        sandbox_manifest_hash=manifest.sha256,
    )

    with pytest.raises(sandbox.SandboxContractError, match="not frozen"):
        sandbox.manifest_for(state, writable_roots=[protected])


def test_manifest_rejects_two_modes_for_the_same_path(tmp_path) -> None:
    manifest = sandbox.SandboxManifest(
        attempt_id="attempt-ambiguous-mode",
        run_id="run-1",
        mounts=((str(tmp_path.resolve()), "ro"), (str(tmp_path.resolve()), "rw")),
        backend="linux",
    )

    with pytest.raises(sandbox.SandboxContractError, match="unique"):
        manifest.validate()


def test_explicit_rw_resources_outside_the_worktree_become_writable_roots(tmp_path) -> None:
    """平台冻进 manifest 的 rw 数据集目录：Docker 年代靠挂载，原生后端靠可写根。
    worktree 里的 rw 挂载不放宽（节点只能写自己的目录）。"""
    worktree = tmp_path / "wt"
    (worktree / "literature").mkdir(parents=True)
    dataset = tmp_path / "dataset"
    dataset.mkdir()
    run_root = tmp_path / "run"
    run_root.mkdir()
    manifest = sandbox.SandboxManifest(
        attempt_id="attempt-rw",
        run_id="run-1",
        mounts=tuple(sorted(
            ((str(worktree.resolve()), "rw"), (str(dataset.resolve()), "rw")),
            key=lambda item: (len(item[0].split("/")), item[0]),
        )),
        backend="darwin",
    )
    manifest.validate()
    state = SimpleNamespace(
        project_worktree=worktree,
        workspace_root=worktree / "literature",
        workspace_relative_path="literature",
        root=run_root,
        sandbox_manifest=manifest.canonical_payload(),
        sandbox_manifest_hash=manifest.sha256,
    )

    roots = sandbox.write_roots_for(state)

    assert dataset.resolve() in roots
    assert (worktree / "literature").resolve() in roots
    assert worktree.resolve() not in roots, "整个 worktree 的 rw 挂载不能把节点目录放宽成整棵树"

"""attempt 出生时冻的「谁来守」：manifest v4 只记后端名。

Docker 年代 manifest 认镜像身份，没有 sha256 就不合法；PR C 之后 backend 是 manifest
的一部分，镜像字段只在解析 v1–v3 老 payload 时还认（DB 里存量 attempt 的 hash 不能变）。

变异判据：把 parse_manifest 的 v≤3 默认 backend 改掉，v2 那条红；把 v4 拒绝 image 的
分支去掉，"removed image backend" 那条红。
"""

from __future__ import annotations

import pytest

from core import isolation, sandbox
from core.isolation import Invariant, attempt_capability


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.delenv(isolation.EXECUTOR_ENV, raising=False)
    isolation._reset_for_tests()
    yield
    isolation._reset_for_tests()


def _manifest(tmp_path, **overrides):
    fields = dict(
        attempt_id="attempt-1",
        run_id="run-1",
        mounts=((str(tmp_path.resolve()), "rw"),),
        backend="darwin",
    )
    fields.update(overrides)
    return sandbox.SandboxManifest(**fields)


class _Native:
    name = "darwin"

    def capabilities(self):
        return frozenset({Invariant.WRITE_BOUNDARY, Invariant.NET_DENY})

    def prepare(self, spec, *, state):
        raise AssertionError("not used")


def test_native_manifest_says_who_guards_it_and_roundtrips(tmp_path) -> None:
    manifest = _manifest(tmp_path)
    manifest.validate()
    payload = manifest.canonical_payload()
    assert payload["backend"] == "darwin" and payload["version"] == 4
    assert set(payload) == {"version", "attempt_id", "run_id", "mounts", "ceiling",
                            "authorized_risk_classes", "backend"}
    parsed = sandbox.parse_manifest(payload)
    assert parsed.backend == "darwin" and parsed.sha256 == manifest.sha256


def test_v2_and_v3_payloads_still_parse_with_their_old_hash(tmp_path) -> None:
    v2 = sandbox.SandboxManifest(
        attempt_id="a", run_id="r", mounts=((str(tmp_path.resolve()), "rw"),),
        backend="image", version=2, image_id="sha256:" + "a" * 64, security_profile="portable")
    v3 = sandbox.SandboxManifest(
        attempt_id="a", run_id="r", mounts=((str(tmp_path.resolve()), "rw"),),
        backend="darwin", version=3, image_id="", security_profile="native")
    for legacy in (v2, v3):
        payload = legacy.canonical_payload()
        parsed = sandbox.parse_manifest(payload)
        assert parsed.version == legacy.version
        assert parsed.backend == legacy.backend
        assert parsed.sha256 == legacy.sha256, "存量 attempt 的 hash 不能因为删了 Docker 而变"
    assert "backend" not in v2.canonical_payload()
    assert v3.canonical_payload()["backend"] == "darwin"


def test_v4_refuses_the_removed_image_backend(tmp_path) -> None:
    with pytest.raises(sandbox.SandboxContractError, match="image backend was removed"):
        _manifest(tmp_path, backend="image").validate()


def test_legacy_image_manifest_still_requires_its_sha256(tmp_path) -> None:
    broken = sandbox.SandboxManifest(
        attempt_id="a", run_id="r", mounts=((str(tmp_path.resolve()), "rw"),),
        backend="image", version=2, image_id="", security_profile="portable")
    with pytest.raises(sandbox.SandboxContractError, match="immutable image id"):
        broken.validate()


def test_attempt_capability_is_just_the_backend_name(monkeypatch) -> None:
    assert attempt_capability(_Native()) == {"backend": "darwin"}
    monkeypatch.setattr(isolation, "select_backend", lambda name=None: _Native())
    assert attempt_capability() == {"backend": "darwin"}


def test_local_manifest_is_minted_for_the_selected_backend(tmp_path, monkeypatch) -> None:
    """CLI 路径：第一次 shell 调用时按当前后端铸本地 manifest。"""
    monkeypatch.setattr(isolation, "select_backend", lambda name=None: _Native())
    run_root = tmp_path / "run"
    run_root.mkdir()

    class _State:
        root = run_root
        project_worktree = None
        run_id = "run-x"
        tenant_id = project_id = session_id = None
        sandbox_manifest = None
        sandbox_manifest_hash = None

    manifest = sandbox.manifest_for(_State(), writable_roots=[run_root])
    assert manifest.backend == "darwin" and manifest.version == 4
    assert manifest.image_id == ""

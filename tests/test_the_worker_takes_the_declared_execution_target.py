"""worker 收到每条请求带来的执行目标（``platform_runtime._apply_execution_target``）。

与档位同一条规矩：字段不在 = 没谈，保持原样；在 = 此刻的目标，照做。换目标等于换「谁来守」，
在跑的 RunAttempt 冻结的后端不能原地变 —— 空闲时丢掉旧绑定，在飞时响亮地拒绝。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import platform_runtime
from core import execution_target as et


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv(et.ENV, raising=False)
    et._reset_for_tests()
    yield
    et._reset_for_tests()


def _session(*, frozen_backend: str | None = None, active: bool = False):
    events: list[tuple[str, dict]] = []
    state = SimpleNamespace(
        sandbox_manifest={"backend": frozen_backend} if frozen_backend else None,
        platform_attempt_id="a1" if frozen_backend else None,
        sandbox_manifest_hash="h" if frozen_backend else None,
        append_transcript=lambda kind, **kw: events.append((kind, kw)),
    )
    return SimpleNamespace(state=state, _operation_active=active), events


def test_absent_field_leaves_the_target_alone() -> None:
    et.declare("wsl:Ubuntu-24.04")
    session, _ = _session()
    platform_runtime._apply_execution_target(session, {}, (False, False, False))
    assert et.current().spec == "wsl:Ubuntu-24.04"


def test_the_declared_target_takes_effect_and_is_recorded() -> None:
    session, events = _session()
    platform_runtime._apply_execution_target(
        session, {"execution_target": "wsl:Ubuntu-24.04"}, (False, False, False))
    assert et.current().spec == "wsl:Ubuntu-24.04"
    assert ("execution_target_declared",
            {"target": "wsl:Ubuntu-24.04", "previous": "local"}) in events


def test_a_bad_spelling_is_a_request_error_not_local() -> None:
    session, _ = _session()
    with pytest.raises(platform_runtime.RequestError) as caught:
        platform_runtime._apply_execution_target(
            session, {"execution_target": "wsl"}, (False, False, False))
    assert caught.value.code == "invalid_execution_target"
    assert et.current().is_local


def test_a_live_attempt_cannot_change_target_in_place() -> None:
    session, _ = _session(frozen_backend="win32", active=True)
    with pytest.raises(platform_runtime.RequestError) as caught:
        platform_runtime._apply_execution_target(
            session, {"execution_target": "wsl:Ubuntu-24.04"}, (True, True, True))
    assert caught.value.code == "sandbox_target_mismatch"
    assert et.current().is_local, "拒绝时目标不能已经被换掉"


def test_an_idle_worker_drops_the_stale_binding_and_switches() -> None:
    session, _ = _session(frozen_backend="win32", active=False)
    platform_runtime._apply_execution_target(
        session, {"execution_target": "wsl:Ubuntu-24.04"}, (False, False, False))
    assert session.state.sandbox_manifest is None and session.state.platform_attempt_id is None
    assert et.current().spec == "wsl:Ubuntu-24.04"


def test_a_matching_frozen_target_is_kept() -> None:
    session, _ = _session(frozen_backend="wsl:Ubuntu-24.04", active=True)
    platform_runtime._apply_execution_target(
        session, {"execution_target": "wsl:Ubuntu-24.04"}, (True, True, True))
    assert session.state.sandbox_manifest == {"backend": "wsl:Ubuntu-24.04"}

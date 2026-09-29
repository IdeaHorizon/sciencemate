"""worker 是个 python.exe——Windows 上缺了 SYSTEMROOT 等系统变量就起不来（连
ws2_32 / 加密 DLL 都加载不了）。`_child_environment` 必须把它们放行。"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services import harness_sessions as hs


@pytest.fixture
def _stub_provider(monkeypatch):
    backend = SimpleNamespace(id="b", model="m", context_window_tokens=0)
    monkeypatch.setattr(hs, "reasoning_backend", lambda _rb: backend)
    monkeypatch.setattr(hs, "_provider_base_url", lambda _b: "http://127.0.0.1:1/v1")
    monkeypatch.setattr(hs, "resolved_api_key", lambda _b: "key")
    monkeypatch.setattr(hs, "_role_delivery_payload", lambda _rb: "payload")
    return backend


def test_windows_system_vars_reach_the_worker(monkeypatch, _stub_provider):
    monkeypatch.setenv("SYSTEMROOT", r"C:\Windows")
    monkeypatch.setenv("COMSPEC", r"C:\Windows\System32\cmd.exe")
    monkeypatch.setenv("PATHEXT", ".COM;.EXE;.BAT")
    env = hs._child_environment(Path("/harness/root"), {"reasoning": object()})
    assert env["SYSTEMROOT"] == r"C:\Windows"
    assert env["COMSPEC"] == r"C:\Windows\System32\cmd.exe"
    assert env["PATHEXT"] == ".COM;.EXE;.BAT"


def test_the_worker_inherits_the_published_data_root(monkeypatch, _stub_provider):
    """worker 用 core.paths 读 HARNESS_FRAMEWORK_HOME 当数据根；后端 publish 把它写进
    os.environ。不透传的话 worker 退回自己的默认根 → 两边分叉（Windows 上尤其致命）。"""
    monkeypatch.setenv("HARNESS_FRAMEWORK_HOME", r"C:\Users\u\AppData\Local\afs")
    env = hs._child_environment(Path("/harness/root"), {"reasoning": object()})
    assert env["HARNESS_FRAMEWORK_HOME"] == r"C:\Users\u\AppData\Local\afs"


def test_credentials_still_do_not_leak_through(monkeypatch, _stub_provider):
    # 放行的是系统变量，凭据仍走 HARNESS_MODEL_ROLES 一条通道，不透传随机 env。
    monkeypatch.setenv("SYSTEMROOT", r"C:\Windows")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "should-not-pass")
    env = hs._child_environment(Path("/harness/root"), {"reasoning": object()})
    assert "AWS_SECRET_ACCESS_KEY" not in env
    assert "SYSTEMROOT" in env

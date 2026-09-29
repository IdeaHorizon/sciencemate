"""Windows UTF-8 模式：把整进程的默认文本编码扳成 utf-8，一个杠杆不打补丁。

背景：Windows 解释器默认文本编码是 ``cp1252``，产品到处写读中文（transcript/memory/
node README/LaTeX）。真机实测 ``hf doctor`` 头一句 print 就 ``UnicodeEncodeError``。解法=
``PYTHONUTF8=1``/``-X utf8``：外部启动的入口没开就 re-exec，框架 spawn 的子进程从 env 带上。

这套测试用 mock 验 re-exec 的**逻辑**（平台无关，Mac/CI 上跑），加一道**机械闸**钉住入口与
子环境的接线不许悄悄回退。真机判据（``hf doctor`` 在 Windows 上真跑通）在 PR 里另附。
"""

from __future__ import annotations

import ast
import os
from pathlib import Path

import pytest

from shared.lib import platform_env

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _clean_env():
    for k in ("PYTHONUTF8", platform_env._UTF8_REEXEC_SENTINEL):
        os.environ.pop(k, None)
    yield
    for k in ("PYTHONUTF8", platform_env._UTF8_REEXEC_SENTINEL):
        os.environ.pop(k, None)


# ── utf8_mode_env ────────────────────────────────────────────────────────────

def test_utf8_mode_env_is_windows_only(monkeypatch):
    monkeypatch.setattr(platform_env.sys, "platform", "linux")
    assert platform_env.utf8_mode_env() == {}
    monkeypatch.setattr(platform_env.sys, "platform", "darwin")
    assert platform_env.utf8_mode_env() == {}
    monkeypatch.setattr(platform_env.sys, "platform", "win32")
    assert platform_env.utf8_mode_env() == {"PYTHONUTF8": "1"}


# ── ensure_utf8_mode ─────────────────────────────────────────────────────────

def test_ensure_is_noop_on_posix(monkeypatch):
    calls = []
    monkeypatch.setattr(platform_env.sys, "platform", "linux")
    monkeypatch.setattr(platform_env.subprocess, "call", lambda *a: calls.append(a))
    platform_env.ensure_utf8_mode()
    assert calls == []
    assert "PYTHONUTF8" not in os.environ  # POSIX 不碰环境


def test_ensure_is_noop_when_already_utf8(monkeypatch):
    calls = []
    monkeypatch.setattr(platform_env.sys, "platform", "win32")
    monkeypatch.setattr(platform_env, "_in_utf8_mode", lambda: True)
    monkeypatch.setattr(platform_env.subprocess, "call", lambda *a: calls.append(a))
    platform_env.ensure_utf8_mode()
    assert calls == [], "已在 utf-8 模式还去 re-exec（会死循环）"


def test_ensure_reexecs_on_win32_when_not_utf8(monkeypatch):
    calls = []
    monkeypatch.setattr(platform_env.sys, "platform", "win32")
    monkeypatch.setattr(platform_env, "_in_utf8_mode", lambda: False)
    monkeypatch.setattr(platform_env.sys, "orig_argv", ["py.exe", "-m", "core.cli", "doctor"])
    monkeypatch.setattr(platform_env.sys, "executable", "py.exe")
    monkeypatch.setattr(platform_env.subprocess, "call", lambda argv: calls.append(list(argv)) or 7)
    with pytest.raises(SystemExit) as result:
        platform_env.ensure_utf8_mode()
    assert result.value.code == 7
    # 原样重跑同一命令，只是这回带着 PYTHONUTF8=1
    assert calls == [["py.exe", "-m", "core.cli", "doctor"]]
    assert os.environ["PYTHONUTF8"] == "1"
    assert os.environ[platform_env._UTF8_REEXEC_SENTINEL] == "1"


def test_ensure_does_not_loop_when_sentinel_already_set(monkeypatch):
    calls, recon = [], []
    monkeypatch.setattr(platform_env.sys, "platform", "win32")
    monkeypatch.setattr(platform_env, "_in_utf8_mode", lambda: False)
    monkeypatch.setenv(platform_env._UTF8_REEXEC_SENTINEL, "1")
    monkeypatch.setattr(platform_env.subprocess, "call", lambda *a: calls.append(a))
    monkeypatch.setattr(platform_env, "_reconfigure_std_streams", lambda: recon.append(True))
    platform_env.ensure_utf8_mode()
    assert calls == [], "带 sentinel 了还再 re-exec = 死循环"
    assert recon == [True], "循环兜底时应降级修打印"


def test_ensure_degrades_when_no_orig_argv(monkeypatch):
    calls, recon = [], []
    monkeypatch.setattr(platform_env.sys, "platform", "win32")
    monkeypatch.setattr(platform_env, "_in_utf8_mode", lambda: False)
    monkeypatch.setattr(platform_env.sys, "orig_argv", [])
    monkeypatch.setattr(platform_env.subprocess, "call", lambda *a: calls.append(a))
    monkeypatch.setattr(platform_env, "_reconfigure_std_streams", lambda: recon.append(True))
    platform_env.ensure_utf8_mode()
    assert calls == [], "没有可复现的 argv 不该 execv"
    assert recon == [True]
    assert os.environ["PYTHONUTF8"] == "1", "仍设 env——即便本进程降级，子进程也该 utf-8"


def test_ensure_degrades_when_relaunch_fails(monkeypatch):
    recon = []

    def _boom(*_a):
        raise OSError("cannot exec here")

    monkeypatch.setattr(platform_env.sys, "platform", "win32")
    monkeypatch.setattr(platform_env, "_in_utf8_mode", lambda: False)
    monkeypatch.setattr(platform_env.sys, "orig_argv", ["py.exe", "x"])
    monkeypatch.setattr(platform_env.subprocess, "call", _boom)
    monkeypatch.setattr(platform_env, "_reconfigure_std_streams", lambda: recon.append(True))
    platform_env.ensure_utf8_mode()  # 不该把 OSError 抛出去
    assert recon == [True]


# ── 机械闸：入口/子环境接线不许悄悄回退 ─────────────────────────────────────────

_WIRING = [
    ("core/cli.py", "main", "ensure_utf8_mode"),
    ("platform/backend/app/launcher.py", "main", "ensure_utf8_mode"),
    ("platform_runtime.py", "main", "ensure_utf8_mode"),
    ("core/isolation/_native.py", "payload_environment", "utf8_mode_env"),
    # 后端起 harness 子进程的 env 走一处回答 harness_subprocess_env（它带上 utf8_mode_env）；
    # worker 的 _child_environment 经它拿到 UTF-8，不再直接调 utf8_mode_env（PR fix/win-harness-subprocess-env）。
    ("platform/backend/app/services/harness_runtime.py", "harness_subprocess_env", "utf8_mode_env"),
    ("platform/backend/app/services/harness_sessions.py", "_child_environment", "harness_subprocess_env"),
]


def _function_calls(path: str, func: str, callee: str) -> bool:
    tree = ast.parse((REPO / path).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func:
            for call in ast.walk(node):
                if isinstance(call, ast.Call):
                    f = call.func
                    if isinstance(f, ast.Name) and f.id == callee:
                        return True
                    if isinstance(f, ast.Attribute) and f.attr == callee:
                        return True
            return False
    return False


@pytest.mark.parametrize(("path", "func", "callee"), _WIRING)
def test_entrypoints_and_child_envs_are_wired_for_utf8(path, func, callee):
    assert _function_calls(path, func, callee), (
        f"{path}::{func} 不再调用 {callee} —— Windows UTF-8 接线被摘了，"
        "整进程会退回 cp1252（真机 hf doctor 头一句 print 就崩）")


@pytest.mark.skipif(os.name != "nt", reason="Actual Windows command line and UTF-8 relaunch")
def test_native_relaunch_preserves_quoted_arguments_and_exit_code(tmp_path):
    import json
    import subprocess
    import sys
    script = """
import json, sys
from shared.lib.platform_env import ensure_utf8_mode
ensure_utf8_mode()
assert sys.flags.utf8_mode
print(json.dumps(sys.argv[1:], ensure_ascii=False))
raise SystemExit(7)
"""
    arguments = ["two words", 'a"quote', "中文文件", "line\nbreak", ""]
    environment = {**os.environ, "PYTHONUTF8": "0", "PYTHONPATH": str(REPO)}
    environment.pop(platform_env._UTF8_REEXEC_SENTINEL, None)
    result = subprocess.run([sys.executable, "-c", script, *arguments], env=environment,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
    assert result.returncode == 7, result.stderr.decode("utf-8", "replace")
    assert json.loads(result.stdout.decode("utf-8")) == arguments

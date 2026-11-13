"""The CLI adapts the shared runtime without becoming a worker dependency."""

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import chat as runtime
from cli import chat, commands, terminal

ROOT = Path(__file__).resolve().parents[2]


def test_worker_import_does_not_load_the_terminal():
    script = (
        "import sys, chat, platform_runtime; "
        "print(__import__('json').dumps(sorted(n for n in sys.modules "
        "if n == 'cli' or n.startswith('cli.') or n == 'readline')))"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=ROOT, capture_output=True, text=True, check=True
    )
    assert json.loads(result.stdout) == []


def test_frontend_restores_output_on_failure(monkeypatch):
    previous = object()
    monkeypatch.setattr(runtime, "_terminal_output", previous)

    async def fail():
        assert runtime._terminal_output is terminal
        raise RuntimeError("frontend stopped")

    monkeypatch.setattr(chat, "_run_repl", fail)
    with pytest.raises(RuntimeError, match="frontend stopped"):
        asyncio.run(chat._main())
    assert runtime._terminal_output is previous


def test_slash_command_calls_the_shared_control(monkeypatch):
    called = []
    shown = []
    state = SimpleNamespace(hook_state={})
    monkeypatch.setattr(runtime, "_continuous_loop", lambda _: True)
    monkeypatch.setattr(runtime, "_set_continuous_loop", lambda s, on: called.append((s, on)))
    monkeypatch.setattr(terminal, "_emit_above_prompt", shown.append)

    assert asyncio.run(commands._handle_slash("/stop", state, [])) is False
    assert called == [(state, False)]
    assert shown


@pytest.mark.parametrize("entry", [["-m", "cli"], ["chat.py"]])
def test_both_launchers_support_help_without_a_session(tmp_path, entry):
    home = tmp_path / "harness-home"
    result = subprocess.run(
        [sys.executable, *entry, "--help"],
        cwd=ROOT,
        env={**os.environ, "HARNESS_FRAMEWORK_HOME": str(home)},
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert "--project" in result.stdout
    assert "--session" in result.stdout
    assert not home.exists()


@pytest.mark.parametrize("entry", [["-m", "cli"], ["chat.py"]])
def test_both_launchers_can_exit_an_actual_repl(tmp_path, entry):
    env = {
        **os.environ,
        "HARNESS_FRAMEWORK_HOME": str(tmp_path / "home"),
        "LLM_API_KEY": "unused-cli-smoke",
        "LLM_BASE_URL": "http://127.0.0.1:9/v1",
        "LLM_MODEL": "cli-smoke",
        "HARNESS_DISABLE_SEMANTIC_DEDUP": "1",
    }
    result = subprocess.run(
        [sys.executable, *entry],
        input="/exit\n",
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert "对话已保存到" in result.stdout
    assert "会话正常结束" in result.stdout

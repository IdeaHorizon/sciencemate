"""起 harness 子进程的环境是**一处回答**（harness_subprocess_env）。

真机缘起（09-08 非管理员 Windows）：后端启动的执行边界探针 `python -c "import core…"`
崩在 `import asyncio → _overlapped` 的 `OSError WinError 10106`——它的 env allowlist 漏了
`SYSTEMROOT`，Winsock 起不来。于是后端**谎报「没有执行边界」**，而 win32 沙箱其实好好的
（`doctor` 报五项全在）。根因不是这一处，是**六处各抄了一份 allowlist**、P0-5 只收了 worker
那处（`_child_environment`），naming/kb/feed/runtime/启动探针都漏了系统变量。

这套测试钉两件事：①`harness_subprocess_env` 真把系统变量 + UTF-8 开关带上；②**机械闸**——
platform/backend 里不许再有第二处手搓 `os.environ` allowlist（新抄一份就默认在 Windows 上
漏系统变量、谎报没边界）。
"""

from __future__ import annotations

import ast
import types
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parents[1] / "app"


# ── 行为：系统变量 + UTF-8 + passthrough/extra 都对 ──────────────────────────────

def test_harness_subprocess_env_carries_system_env_and_utf8(monkeypatch):
    from app.services import harness_runtime

    fake_platform_env = types.SimpleNamespace(
        system_env_passthrough=lambda: {"SYSTEMROOT": r"C:\WINDOWS"},   # Windows：非空
        utf8_mode_env=lambda: {"PYTHONUTF8": "1"},
    )
    monkeypatch.setattr("app.services.harness_imports.harness_module",
                        lambda name: fake_platform_env)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("HARNESS_EXECUTOR", "win32")
    monkeypatch.setenv("NOISE_VAR", "should-not-pass")

    env = harness_runtime.harness_subprocess_env(
        "/harness/root", passthrough=("HARNESS_EXECUTOR",), extra={"LLM_API_KEY": "k"})

    # 系统变量必须带上——否则子进程 import asyncio → _overlapped → WinError 10106
    assert env["SYSTEMROOT"] == r"C:\WINDOWS"
    assert env["PYTHONUTF8"] == "1"          # 同族第二件事：UTF-8 模式
    assert env["PATH"] == "/usr/bin:/bin"    # 基础放行
    assert env["HARNESS_EXECUTOR"] == "win32"  # 调用方点名的 passthrough
    assert env["PYTHONPATH"] == "/harness/root"
    assert env["LLM_API_KEY"] == "k"         # extra
    assert "NOISE_VAR" not in env, "没点名的变量不该漏进子进程"


def test_posix_passthrough_is_empty_is_a_noop(monkeypatch):
    from app.services import harness_runtime

    fake_platform_env = types.SimpleNamespace(
        system_env_passthrough=lambda: {},   # POSIX：空集
        utf8_mode_env=lambda: {},
    )
    monkeypatch.setattr("app.services.harness_imports.harness_module",
                        lambda name: fake_platform_env)
    monkeypatch.setenv("PATH", "/usr/bin")
    env = harness_runtime.harness_subprocess_env("/root")
    assert "PYTHONUTF8" not in env and "SYSTEMROOT" not in env
    assert env["PATH"] == "/usr/bin" and env["PYTHONPATH"] == "/root"


# ── 机械闸：不许再有第二处手搓 os.environ allowlist ──────────────────────────────

def _hand_rolled_env_allowlists(pyfile: Path) -> list[int]:
    """找「{…for … in os.environ.items() if key in <字面量集合>}」这种手搓 allowlist。

    ``harness_subprocess_env`` 自己用的是 `if key in names`（变量，不是字面量集合），因此
    天然不被这道闸命中——不用写豁免名单。"""
    tree = ast.parse(pyfile.read_text(encoding="utf-8"))
    hits: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.DictComp, ast.SetComp, ast.ListComp)):
            continue
        for gen in node.generators:
            it = gen.iter
            over_environ = (
                isinstance(it, ast.Call) and isinstance(it.func, ast.Attribute)
                and it.func.attr == "items"
                and isinstance(it.func.value, ast.Attribute)
                and it.func.value.attr == "environ"
            )
            if not over_environ:
                continue
            for cond in gen.ifs:
                for sub in ast.walk(cond):
                    if isinstance(sub, ast.Compare) and any(isinstance(o, ast.In) for o in sub.ops):
                        for comp in sub.comparators:
                            is_literal_set = isinstance(comp, ast.Set) or (
                                isinstance(comp, ast.Call)
                                and isinstance(comp.func, ast.Name)
                                and comp.func.id == "frozenset")
                            if is_literal_set:
                                hits.append(node.lineno)
    return hits


def test_no_second_hand_rolled_harness_subprocess_env():
    offenders: list[str] = []
    for pyfile in APP.rglob("*.py"):
        for line in _hand_rolled_env_allowlists(pyfile):
            offenders.append(f"{pyfile.relative_to(APP.parent)}:{line}")
    assert not offenders, (
        "又有人手搓 os.environ allowlist（默认在 Windows 上漏系统变量、谎报没有执行边界）——"
        "改走 app.services.harness_runtime.harness_subprocess_env：\n  " + "\n  ".join(offenders))

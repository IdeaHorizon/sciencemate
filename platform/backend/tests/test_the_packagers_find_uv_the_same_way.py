"""构建机上的工具，别在共用那层假设它在 PATH 上。

## 为什么有这一份

2026-09-21 打 0.5.2 的专业版 Windows 包，死在 `FileNotFoundError: [WinError 2]`：
共用的装配层 `_vendor_one` 直接 `subprocess.run(["uv", ...])`，而那台构建机的 uv 装在
`%USERPROFILE%\\.local\\bin\\uv.exe`，从 ssh 进去的非登录 shell 里 PATH 根本没有它。
同一个脚本里 `build_windows_app.find_uv()` 早就处理了这件事 —— **两处各答一遍，
于是打包器找得到、装配层找不到**。

Mac 上 uv 一直在 PATH 里，所以这条路在 Mac 上永远是绿的：这正是
[[feedback_the_gate_nobody_ran_hides_its_own_cause]] 说的那种缺陷。

判据是两条：**没有第二个人自己去找 uv**（AST 扫，不是列名单），以及
**找法真的不依赖 PATH**（把 PATH 清空，放一个假 uv 在家目录里，仍要找得到）。
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
PACKAGE_DIR = REPO / "scripts" / "package"
sys.path.insert(0, str(PACKAGE_DIR))
import payload_release  # noqa: E402

#: 唯一被允许回答「uv 在哪」的地方。
THE_ONE_ANSWER = "the_uv_executable"


def _packager_sources() -> list[Path]:
    found = sorted(PACKAGE_DIR.glob("build_*_app.py")) + [PACKAGE_DIR / "payload_release.py"]
    return [p for p in found if p.is_file()]


def test_there_is_something_to_scan() -> None:
    assert len(_packager_sources()) >= 3, "打包脚本没扫到，这道闸是空的"


@pytest.mark.parametrize("source", _packager_sources(), ids=lambda p: p.name)
def test_nobody_runs_a_bare_uv(source: Path) -> None:
    """没有人把裸 `"uv"` 当命令跑 —— 那是在赌构建机的 PATH。"""
    tree = ast.parse(source.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        name = getattr(node.func, "attr", getattr(node.func, "id", ""))
        if name not in ("run", "check_output", "Popen", "check_call"):
            continue
        argv = node.args[0]
        if not isinstance(argv, ast.List) or not argv.elts:
            continue
        first = argv.elts[0]
        if isinstance(first, ast.Constant) and first.value == "uv":
            raise AssertionError(
                f"{source.name}:{node.lineno} 拿裸 'uv' 起进程 —— "
                f"构建机上它不一定在 PATH 里，该走 payload_release.{THE_ONE_ANSWER}()")


@pytest.mark.parametrize("source", _packager_sources(), ids=lambda p: p.name)
def test_only_one_place_looks_uv_up(source: Path) -> None:
    """`shutil.which("uv")` 只许出现在那一个函数里；别处再找一遍就是第二个真相源。"""
    text = source.read_text(encoding="utf-8")
    tree = ast.parse(text)
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "which"
                and node.args and isinstance(node.args[0], ast.Constant)
                and node.args[0].value == "uv"):
            continue
        owner = next((f.name for f in ast.walk(tree)
                      if isinstance(f, ast.FunctionDef)
                      and f.lineno <= node.lineno <= (f.end_lineno or f.lineno)), "")
        assert owner == THE_ONE_ANSWER, (
            f"{source.name}:{node.lineno} 在 {owner or '模块层'} 里又找了一遍 uv —— "
            f"这个问题只许 {THE_ONE_ANSWER} 回答")


def test_it_finds_uv_when_the_path_does_not_have_it(tmp_path, monkeypatch) -> None:
    """把 PATH 清空，uv 只在家目录里 —— 仍要找得到。这就是那台 Windows 构建机的处境。"""
    home = tmp_path / "home"
    (home / ".local" / "bin").mkdir(parents=True)
    planted = home / ".local" / "bin" / ("uv.exe" if sys.platform == "win32" else "uv")
    planted.write_text("#!/bin/sh\n", encoding="utf-8")
    planted.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path / "nothing-here"))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOME", str(home))
    assert payload_release.the_uv_executable() == str(planted)


def test_a_build_machine_without_uv_is_told_so(tmp_path, monkeypatch) -> None:
    """找不到就明说，别让它在 `WinError 2` 里出现 —— 那条报错不指向任何可做的事。"""
    monkeypatch.setenv("PATH", str(tmp_path / "nothing-here"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "empty-home"))
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    with pytest.raises(SystemExit, match="找不到 uv"):
        payload_release.the_uv_executable()


@pytest.mark.parametrize("module", ["build_mac_app", "build_windows_app", "payload_release"],)
def test_importing_a_packager_does_not_require_a_build_machine(module: str, tmp_path, monkeypatch) -> None:
    """跑测试的机器不是构建机 —— import 打包脚本不许去找 uv。

    第一版把它写成模块级常量 `UV = the_uv_executable()`，于是「没装 uv」变成了
    「这个模块 import 不了」，CI 当场红。构建机的东西，要用的那一刻才问。
    """
    import importlib
    monkeypatch.setenv("PATH", str(tmp_path / "nothing-here"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "empty-home"))
    monkeypatch.setenv("HOME", str(tmp_path / "empty-home"))
    for name in (module, f"{module}_reimport_probe"):
        sys.modules.pop(name, None)
    sys.modules.pop(module, None)
    importlib.import_module(module)      # 炸了就是判据红

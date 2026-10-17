"""同一个模块里，顶层函数 / 类只定义一次（#893 / #895）。

core/sandbox.py 曾有两个 `gpu_capability()`：上面一份返回 dict（带 `_visible_gpus`
与导出的 `GPU_ISOLATION_NONE`），下面一份返回 `GpuCapability`。Python 默默取后者，
前者一个调用方都没有却照样被读、被导出 —— 两份答案并存，读源码的人分不清哪份在
生效，也不报错。

判据按 AST 扫模块顶层：同名的 def / class 出现两次就红。条件分支里的平台特化定义
（`if sys.platform == ...: def f` / `else: def f`）不在顶层，不受影响。
"""
from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ROOTS = ("core", "shared", "nodes", "platform/backend/app")


def _shadowed_definitions() -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for root in ROOTS:
        for path in sorted((REPO / root).rglob("*.py")):
            if ".venv" in path.parts or "node_modules" in path.parts:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            names = Counter(
                node.name for node in tree.body
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)))
            twice = sorted(name for name, count in names.items() if count > 1)
            if twice:
                found[path.relative_to(REPO).as_posix()] = twice
    return found


def test_no_top_level_definition_is_silently_shadowed() -> None:
    shadowed = _shadowed_definitions()
    assert not shadowed, (
        "这些模块把同一个名字在顶层定义了不止一次 —— 先定义的那份被后一份静默遮住，"
        f"成了没人调用、却仍被读、被导出的第二个答案：{shadowed}")

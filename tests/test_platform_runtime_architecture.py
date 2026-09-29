"""Architecture gates for the App Server -> root scheduler bridge.

These tests intentionally inspect source and git history.  The bridge may own
transport, identity, lifecycle and event projection, but it must never become a
second scientific executor beside the root orchestrator.
"""

from __future__ import annotations

import ast
import subprocess

import pytest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "platform_runtime.py"


def _qualified_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _qualified_name(node.value)
        return f"{parent}.{node.attr}" if parent else node.attr
    return ""


def test_platform_runtime_cannot_execute_tools_or_write_scientific_artifacts():
    tree = ast.parse(RUNTIME.read_text(encoding="utf-8"), filename=str(RUNTIME))
    violations: list[str] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        called = _qualified_name(node.func)
        leaf = called.rsplit(".", 1)[-1]
        if called.endswith("tool_registry.execute") or leaf == "_run_node_tool":
            violations.append(f"line {node.lineno}: direct tool execution via {called}")
        if leaf == "save_artifact":
            violations.append(f"line {node.lineno}: direct artifact write via {called}")
        if called.endswith("agent_loop.run_loop"):
            violations.append(f"line {node.lineno}: agent loop bypass via {called}")
        if leaf == "NodeHarness":
            violations.append(f"line {node.lineno}: adapter-owned harness construction")

    assert violations == [], "\n".join(violations)


def test_platform_runtime_defines_no_platform_pseudo_nodes():
    tree = ast.parse(RUNTIME.read_text(encoding="utf-8"), filename=str(RUNTIME))
    pseudo_nodes = sorted(
        {
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and node.value.startswith("_platform_")
        }
    )
    assert pseudo_nodes == []


def _first_party_top_levels() -> set[str]:
    """仓库根上**本仓库自己的**顶层 import 名 —— 扫出来，不写名单。

    新加一个顶层包（哪天多了个 `services/`）自动进入守卫范围；写死名单的话，
    新东西默认漏过。
    """
    return {
        entry.name
        for entry in ROOT.iterdir()
        if entry.is_dir() and (entry / "__init__.py").exists()
    }


def test_platform_runtime_imports_nothing_first_party_at_module_level():
    """本文件必须能在"harness 仓库根不在 sys.path 上"时按路径加载。

    `platform/backend` 的测试就是这么用它的（只为拿真的 `_project_lock`，
    不肯把 harness 根塞进 sys.path —— 那里的 `tests/`、`platform/` 会和后端
    自己的顶层同名项撞车）。所以文件里十几处 `core.*` import 全是函数内延迟
    的，这不是风格偏好，是接口的一部分。

    2026-08-13 顶层多了一行 `from core.session_driver import ...`：平台后端 CI
    立刻红，而报错写的是"测试文件 ImportError → No module named 'core'"，
    指向的地方离病根隔了两跳。这条守卫让它在**本仓库**里当场红，且指到行号。
    """
    tree = ast.parse(RUNTIME.read_text(encoding="utf-8"), filename=str(RUNTIME))
    first_party = _first_party_top_levels()
    assert "core" in first_party, "扫顶层包的方式失效了，这条守卫会静默归零"

    violations: list[str] = []
    for node in tree.body:                          # 只看模块级；函数内延迟的正是解法
        if isinstance(node, ast.ImportFrom):
            roots = [(node.module or "").split(".")[0]] if node.level == 0 else []
        elif isinstance(node, ast.Import):
            roots = [alias.name.split(".")[0] for alias in node.names]
        else:
            continue
        violations += [
            f"line {node.lineno}: 模块级 import 了本仓库的 `{name}`"
            for name in roots if name in first_party
        ]
    assert violations == [], (
        "platform_runtime.py 顶层不能 import 本仓库的包 —— 平台后端按文件路径加载它，"
        "那个进程里 harness 根不在 sys.path 上。把 import 挪进用到它的函数里。\n"
        + "\n".join(violations)
    )


# 2026-08-07 退役：test_platform_branch_does_not_modify_producing_nodes
#
# 它的前提是"平台桥接工作不该碰节点"，用 git diff origin/main 兜底。v2.1 架构
# 改造把 data/literature 降格为服务、给 experiment/hypothesis 接上调用权 ——
# 节点改动从"越界嫌疑"变成方案里显式授权的一部分，这条守卫的前提已经不在。
#
# 真正的越界防线仍在，而且更强：`.scope_map.yaml` + `scripts/check_pr_scope.py`
# 在 PR 层按 author 查可改路径（pre-receive / 合并脚本都跑）。同一件事不留两套
# 机制，尤其不留弱的那套 —— 弱守卫失效时没人知道，只会给人虚假安全感。


def test_platform_runtime_self_locates_its_root_when_loaded_by_path(tmp_path):
    """按路径加载（平台后端回收测试的用法）时，函数内延迟的 `core.*` / `shared.*`
    import 也要成立 —— 本文件顶部自己把所在目录追加到 sys.path 末尾。在一个
    harness 根不在 sys.path 上的干净子进程里验：cwd 是空目录、没有 PYTHONPATH。
    2026-08-13 那次是顶层多了一行 import 才红；这条守的是反方向 —— 延迟 import
    不能只在"环境碰巧对"时成立。"""
    import os
    import subprocess
    import sys
    import textwrap

    runtime = Path(__file__).resolve().parents[1] / "platform_runtime.py"
    script = textwrap.dedent(
        f"""
        import importlib.util
        spec = importlib.util.spec_from_file_location("pr_under_test", {str(runtime)!r})
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        from shared.lib import filelock  # 只有模块自己定位了根，这一行才成立
        print("shared importable:", filelock.__name__)
        """
    )
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    proc = subprocess.run(
        [sys.executable, "-c", script], cwd=tmp_path, env=env,
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert "shared importable: shared.lib.filelock" in proc.stdout


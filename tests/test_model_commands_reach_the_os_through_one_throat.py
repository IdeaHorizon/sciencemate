"""模型命令进操作系统只有一条路：``spawn_and_wait``。这条测试扫盘钉住它。

判据（docs/RFC_EXECUTOR_TIERS_20260904.md §3.3）：运行时 import 面上，任何
argv **不是纯字面量**的子进程调用（``subprocess.*`` / ``os.system`` / ``os.exec*`` /
``asyncio.create_subprocess_*``）都必须登记在 ``framework_exemptions.yaml`` 的
``subprocess_call_sites`` 里，按「文件 + 所在函数」对账：

* 没登记的 → 红（新开了一条绕过咽喉的路）
* 登记了但代码里已经没有的 → 红（名单烂掉比没有名单更糟）
* 同一文件里函数名多重集对不上 → 红（同一函数里多开了一个 spawn）

登记不是放行，是分类：``argv_source`` 说清 argv 里有没有模型给的东西。
``model_command``（模型自己拼的命令跑在宿主上）必须带 owner / migrate_to / deadline，
到期即红。这就是把 [[feedback_guardrails_must_scan_not_list]] 用在 spawn 面上：
扫是穷举的，名单只存理由。

为什么扫 AST 而不是 grep 名字：撤了调用留着 import 照样命中字符串；
``import asyncio as _asyncio`` 这种别名 grep 也追不到（derivation_check 的 Lean
调用正是这么写的）。见 [[feedback_grep_for_a_name_is_not_wiring]]。

直接运行本文件会打印当前扫描结果（登记表的样子），方便对账：

    python tests/test_model_commands_reach_the_os_through_one_throat.py
"""

from __future__ import annotations

import ast
import sys
from collections import Counter
from datetime import date
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
REGISTRY = REPO / "framework_exemptions.yaml"
THROAT = "shared/lib/cancellable_subprocess.py"

# 运行时 import 面。测试、脚本、fixture、vendored 模板、容器内 PID 1 都不在墙外侧：
#   * scripts/、nodes/*/scripts/ 是人手跑的开发脚本，模型碰不到
#   * nodes/writing/project_templates/ 是 vendored 的 LaTeX 模板仓库
SCAN_ROOTS = (
    "core",
    "shared",
    "platform/backend/app",
    "platform_runtime.py",
    "run_node.py",
    "chat.py",
    "deploy/platform",
    "nodes",
)
EXCLUDED_PARTS = frozenset({"tests", "scripts", "fixtures", "project_templates", "__pycache__",
                            ".venv", "node_modules", "docs", "skills"})

# (根模块, 属性名) → 认定为子进程调用。别名在 _AliasResolver 里解析。
_SPAWN_ATTRS = {
    "subprocess": {"run", "Popen", "call", "check_call", "check_output", "getoutput",
                   "getstatusoutput"},
    "os": {"system", "popen", "execv", "execve", "execvp", "execvpe", "execl", "execlp",
           "spawnv", "spawnve", "spawnvp", "spawnl", "spawnlp", "posix_spawn", "posix_spawnp"},
    # `asyncio.windows_utils.Popen` 是 `subprocess.Popen` 的子类（overlapped 管道版，
    # Windows 上把子进程的管道接给 proactor 就得用它）——**它也是一个起进程的调用点**。
    # 不认它的话，一条 `from asyncio import windows_utils; windows_utils.Popen(...)`
    # 在这道闸眼里根本不存在（[[feedback_guardrails_must_scan_not_list]]：
    # 枚举写法就意味着新写法默认漏过）。
    "asyncio": {"create_subprocess_exec", "create_subprocess_shell", "Popen"},
}

ARGV_SOURCES = ("throat", "framework", "model_paths", "model_command")
_REQUIRED_FOR_MODEL_COMMAND = ("owner", "migrate_to", "deadline")


def _is_excluded(path: Path) -> bool:
    return bool(EXCLUDED_PARTS & set(path.parts)) or path.name.startswith("test_")


def _iter_runtime_files() -> list[Path]:
    files: list[Path] = []
    for root in SCAN_ROOTS:
        base = REPO / root
        if base.is_file():
            files.append(base)
            continue
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            if not _is_excluded(path.relative_to(REPO)):
                files.append(path)
    return files


class _AliasResolver:
    """把 ``_asyncio.create_subprocess_exec`` / ``from subprocess import run`` 还原成根模块+属性。"""

    def __init__(self, tree: ast.AST) -> None:
        self.module_alias: dict[str, str] = {}   # 本地名 → 根模块名（asyncio / subprocess / os）
        self.name_alias: dict[str, tuple[str, str]] = {}  # 本地名 → (根模块, 属性)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name.split(".")[0]
                    if root in _SPAWN_ATTRS:
                        self.module_alias[alias.asname or root] = root
            elif isinstance(node, ast.ImportFrom) and node.module:
                root = node.module.split(".")[0]
                if root in _SPAWN_ATTRS:
                    for alias in node.names:
                        if alias.name in _SPAWN_ATTRS[root]:
                            self.name_alias[alias.asname or alias.name] = (root, alias.name)
                        elif alias.name in ("subprocess", "windows_utils") and root == "asyncio":
                            # from asyncio import subprocess as _sp → _sp.create_subprocess_exec
                            # from asyncio import windows_utils   → windows_utils.Popen
                            self.module_alias[alias.asname or alias.name] = "asyncio"

    def spawn_callee(self, call: ast.Call) -> str | None:
        func = call.func
        if isinstance(func, ast.Name):
            hit = self.name_alias.get(func.id)
            return f"{hit[0]}.{hit[1]}" if hit else None
        if isinstance(func, ast.Attribute):
            attr = func.attr
            # 一路剥到最里面的 Name：asyncio.subprocess.create_subprocess_exec 也算
            base = func.value
            while isinstance(base, ast.Attribute):
                base = base.value
            if not isinstance(base, ast.Name):
                return None
            root = self.module_alias.get(base.id)
            if root and attr in _SPAWN_ATTRS[root]:
                return f"{root}.{attr}"
        return None


def _argv_node(call: ast.Call) -> ast.AST | None:
    if call.args:
        return call.args[0]
    for kw in call.keywords:
        if kw.arg in {"args", "cmd", "command"}:
            return kw.value
    return None


def _is_literal(node: ast.AST | None) -> bool:
    if node is None:
        return False
    if isinstance(node, ast.Constant):
        return True
    if isinstance(node, (ast.List, ast.Tuple)):
        return all(isinstance(e, ast.Constant) for e in node.elts)
    return False


def _enclosing_function(parents: dict[ast.AST, ast.AST], node: ast.AST) -> str:
    cur = node
    while cur in parents:
        cur = parents[cur]
        if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return cur.name
    return "<module>"


def scan() -> dict[str, Counter]:
    """{相对路径: Counter(所在函数名)}，只含 argv 非字面量的调用点。"""
    found: dict[str, Counter] = {}
    for path in _iter_runtime_files():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError as exc:  # pragma: no cover - 语法错误由别的闸管
            raise AssertionError(f"{path}: {exc}") from exc
        resolver = _AliasResolver(tree)
        parents: dict[ast.AST, ast.AST] = {}
        for parent in ast.walk(tree):
            for child in ast.iter_child_nodes(parent):
                parents[child] = parent
        sites: Counter = Counter()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or resolver.spawn_callee(node) is None:
                continue
            if _is_literal(_argv_node(node)):
                continue
            sites[_enclosing_function(parents, node)] += 1
        if sites:
            found[path.relative_to(REPO).as_posix()] = sites
    return found


def _registry() -> list[dict]:
    data = yaml.safe_load(REGISTRY.read_text(encoding="utf-8")) or {}
    entries = data.get("subprocess_call_sites")
    assert isinstance(entries, list) and entries, (
        "framework_exemptions.yaml 缺 subprocess_call_sites 段；"
        "跑 `python tests/test_model_commands_reach_the_os_through_one_throat.py` 生成")
    return entries


def _render(found: dict[str, Counter]) -> str:
    lines = ["subprocess_call_sites:"]
    for file, sites in sorted(found.items()):
        names = sorted(sites.elements())
        lines.append(f"  - file: {file}")
        lines.append(f"    sites: [{', '.join(names)}]")
        lines.append("    argv_source: framework  # throat | framework | model_paths | model_command")
        lines.append('    reason: ""')
    return "\n".join(lines)


# ── 测试 ─────────────────────────────────────────────────────────────────────


def test_every_non_literal_spawn_site_is_registered_and_nothing_stale_remains() -> None:
    found = scan()
    registered = {entry["file"]: entry for entry in _registry()}

    unregistered = sorted(set(found) - set(registered))
    stale = sorted(set(registered) - set(found))
    mismatched: list[str] = []
    for file in sorted(set(found) & set(registered)):
        expected = Counter(registered[file].get("sites") or [])
        actual = found[file]
        if expected != actual:
            mismatched.append(
                f"{file}: 登记 {sorted(expected.elements())} ≠ 实际 {sorted(actual.elements())}"
            )

    problems = []
    if unregistered:
        problems.append(
            "未登记的子进程调用点（新开了一条绕过 spawn_and_wait 的路？）:\n  "
            + "\n  ".join(f"{f}: {sorted(found[f].elements())}" for f in unregistered)
        )
    if stale:
        problems.append("登记了但代码里已不存在（名单烂了）:\n  " + "\n  ".join(stale))
    if mismatched:
        problems.append("同一文件里调用点对不上:\n  " + "\n  ".join(mismatched))
    assert not problems, (
        "\n\n".join(problems)
        + "\n\n当前扫描结果（可直接抄进 framework_exemptions.yaml）:\n"
        + _render(found)
    )


def test_registry_entries_are_classified_and_model_commands_carry_a_deadline() -> None:
    today = date.today()
    bad: list[str] = []
    seen_throat = False
    for entry in _registry():
        file = entry.get("file", "?")
        source = entry.get("argv_source")
        if source not in ARGV_SOURCES:
            bad.append(f"{file}: argv_source={source!r} 不在 {ARGV_SOURCES}")
            continue
        if not str(entry.get("reason") or "").strip():
            bad.append(f"{file}: reason 为空")
        if source == "throat":
            seen_throat = True
            if file != THROAT:
                bad.append(f"{file}: 只有 {THROAT} 能自称 throat")
        if source == "model_command":
            missing = [k for k in _REQUIRED_FOR_MODEL_COMMAND if not entry.get(k)]
            if missing:
                bad.append(f"{file}: model_command 缺 {missing}")
            else:
                deadline = entry["deadline"]
                if not isinstance(deadline, date):
                    bad.append(f"{file}: deadline 不是日期: {deadline!r}")
                elif deadline < today:
                    bad.append(
                        f"{file}: model_command 豁免已于 {deadline} 到期，"
                        f"迁移方向: {entry['migrate_to']}"
                    )
    assert seen_throat, f"登记表里没有 throat 条目（{THROAT}）"
    assert not bad, "\n".join(bad)


def test_the_throat_itself_is_the_only_file_that_spawns_a_backend_launch() -> None:
    """spawn_and_wait 之外没人拿 core.isolation 的后端去起进程。

    后端交回的 Launch.argv 只该由咽喉起；别处 ``select_backend().prepare`` 就是在
    绕开 kill_event / 超时 / 记账那整套。
    """
    # core/sandbox.py 是给 experiment 节点留的兼容垫片（prepare_attempt_command /
    # prepare_launch / availability）：老调用面在那里拿后端，而那几处"拿了 argv 自己
    # subprocess.run"的调用点已在 subprocess_call_sites 里登记为绕咽喉。垫片本身
    # 不起进程。
    allowed = {THROAT, "core/sandbox.py"}
    offenders = []
    for path in _iter_runtime_files():
        rel = path.relative_to(REPO).as_posix()
        if rel in allowed or rel.startswith("core/isolation/"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            # 判 Call 不判字符串：后端启动探针把 `select_backend()` 写在一段要交给
            # harness 解释器跑的脚本字面量里，那是问能力，不是起进程。
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name == "select_backend":
                offenders.append(f"{rel}:{node.lineno}")
    assert not offenders, f"这些文件绕过咽喉直接拿后端: {offenders}"


if __name__ == "__main__":
    print(_render(scan()))
    sys.exit(0)

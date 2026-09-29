"""扫盘闸：运行时代码里跨进程文件锁只许有一个答案 —— `shared.lib.filelock`。

原来七处各自 `import fcntl`，其中三处「Windows 退化成 no-op」。收口之后，任何
新的 `fcntl` / `msvcrt.locking` 用法都是把第二个答案（和它的平台谎言）加回来。
用 AST 扫 import 与属性引用，不扫文本：注释、文档串里提到 fcntl 不算。
"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: 运行时代码的范围。tests/ 不在内（测试可以直接拿 flock 造对手）。
SCAN = [
    ROOT / "core",
    ROOT / "shared",
    ROOT / "nodes",
    ROOT / "platform_runtime.py",
    ROOT / "chat.py",
    ROOT / "platform" / "backend" / "app",
]
THE_ONLY_ANSWER = ROOT / "shared" / "lib" / "filelock.py"
LOCK_MODULES = {"fcntl", "msvcrt"}


def _runtime_files():
    for entry in SCAN:
        if entry.is_file():
            yield entry
            continue
        for path in entry.rglob("*.py"):
            if "tests" in path.parts:
                continue
            yield path


def _lock_uses(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    hits: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in LOCK_MODULES:
                    hits.append(f"{path.relative_to(ROOT)}:{node.lineno} import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            if node.module in LOCK_MODULES:
                hits.append(f"{path.relative_to(ROOT)}:{node.lineno} from {node.module}")
        elif isinstance(node, ast.Attribute):
            if isinstance(node.value, ast.Name) and node.value.id in LOCK_MODULES:
                hits.append(f"{path.relative_to(ROOT)}:{node.lineno} {node.value.id}.{node.attr}")
    return hits


def test_the_scanner_sees_the_one_place_that_may_lock():
    """非空证明：扫描器认得出 filelock.py 里的 fcntl/msvcrt，否则下面那条守卫
    就是静默归零。"""
    assert _lock_uses(THE_ONLY_ANSWER), "扫描器认不出 shared/lib/filelock.py 里的锁调用"


def test_no_runtime_code_locks_files_outside_filelock():
    offenders = [
        hit
        for path in _runtime_files()
        if path != THE_ONLY_ANSWER
        for hit in _lock_uses(path)
    ]
    assert not offenders, (
        "跨进程文件锁只能经 shared.lib.filelock（acquire/release/exclusive/try_hold）"
        "—— 别把第二个答案加回来：\n  " + "\n  ".join(offenders)
    )

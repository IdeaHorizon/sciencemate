"""错误处理器自己不能炸。

2026-09-04 落 `user_files` hook 时照出来的：`core/loop_hooks_builtin.py` 里有
四处 `except Exception as e: log.debug(...)`，而那个模块**从来没有定义过
`log`**。于是每一个这样的处理器一旦真被走到，就抛 `NameError` —— "注入失败
但不打断这一轮"当场变成"这一轮炸掉"，而且报错指向一个跟真因毫无关系的名字。

一个从来没跑过的错误处理器，和没有错误处理器是一回事
（`feedback_absent_check_looks_like_passed_check`）。它能潜伏是因为 happy path
永远走不到它，测试也不会去撞它。

判据是扫盘不是名单：任何模块只要拿 `log` / `logger` / `_log` 当对象用，这个
名字就必须在这个文件里真的被绑定过。新模块自动被覆盖。
"""
from __future__ import annotations

import ast
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

#: 这几个名字在本仓里就是"记日志的那个东西"。
_LOGGER_NAMES = frozenset({"log", "logger", "_log"})

#: 扫哪些树。`nodes/` 也在内了 —— 落这道闸时 experiment 节点有同样的缺陷，
#: 那是别人的目录不代修，于是先记名（一条断言"它还坏着"的占位测试）。
#: 2026-09-04 它被 `fix(experiment): 省略 walltime …`（9b534fe6b）修好，占位
#: 测试如约自己转红，本次把它删掉、把 `nodes` 纳进扫盘。
#: 判决不该比它描述的事实活得更久。
_ROOTS = ("core", "shared", "platform/backend/app", "nodes")


def _bound_names(tree: ast.Module) -> set[str]:
    """这个文件里**任何地方**绑定过的名字。

    刻意宽松：不做作用域分析。真正要抓的形状是"整个文件从头到尾都没有这个
    名字"，那才是必然的 NameError。函数里自己造一个同名局部变量的写法，宽松
    判据会放过 —— 放过一个可疑写法，比误伤一堆正常写法划算。
    """
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            found.add(node.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            found.add(node.name)
        elif isinstance(node, ast.arg):
            found.add(node.arg)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                found.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.Global):
            found.update(node.names)
    return found


def _logger_uses(tree: ast.Module) -> set[str]:
    used: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id in _LOGGER_NAMES
        ):
            used.add(node.value.id)
    return used


def _python_files() -> list[Path]:
    found: list[Path] = []
    for root in _ROOTS:
        for path in sorted((REPO / root).rglob("*.py")):
            if "/tests/" in str(path) or "__pycache__" in str(path):
                continue
            found.append(path)
    return found


def test_every_module_that_logs_has_a_logger() -> None:
    offenders: list[str] = []
    for path in _python_files():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:            # 语法错另有闸管，别在这儿吞掉
            continue
        missing = _logger_uses(tree) - _bound_names(tree)
        for name in sorted(missing):
            offenders.append(f"{path.relative_to(REPO)}: 用了 `{name}.…` 但从没定义它")
    assert offenders == [], (
        "这些模块的错误处理器一旦真被走到就抛 NameError —— 加一行 "
        "`log = logging.getLogger(__name__)`：\n  " + "\n  ".join(offenders)
    )

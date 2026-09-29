"""函数体里不许留 `raise`/`return` 之后的死代码。

背景：chat.py 曾有 680 行（全文的 43%）在无条件 `raise`/`return` 之后 ——
四条 legacy chat 路径的全部实现，连注释都写着 "retained for unreachable
legacy code below"。它们照常被 import、被 lint、被读，唯独永远不执行：
读代码的人（和我）会把它们当现状去推理，于是"平台还有一套自己的 harness
内核在跑 chat"这种误判就成立了。

这条护栏是**扫盘**，不是名单：新写的任何文件都自动在检查范围内。
"""

import ast
import pathlib

APP = pathlib.Path(__file__).resolve().parents[1] / "app"


def _unreachable_spans(tree: ast.AST):
    """同一 block 内，第一个 raise/return 之后的语句一定执行不到。

    只看函数体顶层这一种最无争议的形状 —— 不做控制流分析，宁可漏报不误报。
    """
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for index, node in enumerate(fn.body):
            if isinstance(node, (ast.Raise, ast.Return)):
                rest = fn.body[index + 1 :]
                if rest:
                    end = max(getattr(n, "end_lineno", n.lineno) for n in rest)
                    yield fn.name, rest[0].lineno, end
                break


def test_no_unreachable_statements_in_app():
    offenders = []
    for path in sorted(APP.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError:  # pragma: no cover - 语法错另有测试兜
            continue
        for name, start, end in _unreachable_spans(tree):
            rel = path.relative_to(APP.parent)
            offenders.append(f"{rel}:{start}-{end} ({name}, {end - start + 1} 行)")

    assert not offenders, "以下代码在 raise/return 之后，永远执行不到：\n  " + "\n  ".join(
        offenders
    )

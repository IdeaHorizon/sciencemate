"""「忙」不再是一个 API 概念（RFC 异步运行时 D10 删除清单）。

## 删的是什么

8-21 那次事故的病根是一把 operation lock 被迫回答三个不同的问题，一个都答
不对。三个概念现在各归各位：

- **所有权**（谁可以写这个工作区）= flock。仍然存在，仍然会拒绝 —— 两个
  进程同时写一个工作区是真的不行。它的 code 叫 `project_busy`。
- **活动**（此刻有没有计算在飞）= worker 自报 + 心跳租约。它是**排队**的
  依据，不是拒收的依据。
- **可寻址**（这句话能不能送达）= 恒真。

所以 `session_busy` 那一维（"会话被占着，你的话我不收"）**整个消失**：
产生它的三处 raise 删了，文案删了，chat 入口那个第二判据也删了。

## 为什么要一道扫盘闸

这类概念不是一次删干净就完事的 —— 它会以"顺手加一个 busy 检查"的形式长
回来，而长回来的那一次不会有人记得 8-21。判据扫的是**这件事**（拒收的理由
是不是"占用"），不是某个字符串：任何以 `session_busy` 为 code 的路径、任何
名字里含 busy 的会话属性，都会被抓到。
"""
from __future__ import annotations

import ast
import pathlib

APP = pathlib.Path(__file__).resolve().parents[1] / "app"
HARNESS = pathlib.Path(__file__).resolve().parents[3]


def _python_sources() -> list[pathlib.Path]:
    """产品代码：App Server 的 app/ 加 worker 的两个入口。

    两边都扫 —— 「忙 = 拒收」这个概念当初就是两个进程各有一份的。
    """
    return sorted(APP.rglob("*.py")) + [
        HARNESS / "platform_runtime.py",
        HARNESS / "chat.py",
    ]


def _docstring_ids(tree: ast.AST) -> set[int]:
    """所有 docstring 节点的 id。

    注释和 docstring 里**必须**讲得了这段历史 —— 那正是我们希望留下的东西。
    按行首 `#` 之类的字面判据分不出"文档里提到它"和"代码里用了它"
    （实测：一句写在 docstring 中间的解释被判成违规）。走 AST，判据落在
    "这个字符串是不是被当成值用"上。
    """
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", None)
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                ids.add(id(body[0].value))
    return ids


def test_nothing_refuses_an_input_because_the_session_is_occupied() -> None:
    """没有任何一条**代码路径**以 `session_busy` 为由拒收。"""
    offenders: list[str] = []
    for path in _python_sources():
        if not path.is_file():
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        docstrings = _docstring_ids(tree)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and "session_busy" in node.value
                and id(node) not in docstrings
            ):
                offenders.append(f"{path.name}:{node.lineno} {node.value[:60]!r}")
    assert not offenders, (
        "「会话被占用」又变回拒收理由了（RFC D10 删除清单）：\n  "
        + "\n  ".join(offenders)
        + "\n\n占用是**排队**的理由。要拒收只有一个合法原因：没有工作区"
        "（那是所有权，code 叫 project_busy）。"
    )


def test_no_session_attribute_is_called_busy() -> None:
    """会话对象上不许再有叫 `busy` 的东西。

    名字本身就是那个缺陷：一个词答三个问题。现在那三个问题各有各的名字
    （`conversation_in_flight` / `manager.is_occupied()` / 恒真的可寻址）。
    """
    offenders: list[str] = []
    for path in _python_sources():
        if not path.is_file():
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr == "busy":
                offenders.append(f"{path.name}:{node.lineno} .{node.attr}")
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "busy":
                offenders.append(f"{path.name}:{node.lineno} def busy()")
    assert not offenders, (
        "又出现了叫 `busy` 的属性/方法：\n  " + "\n  ".join(offenders)
        + "\n\n一个名字只回答一个问题 —— 想问什么就用问什么的那个名字。"
    )


def test_the_ownership_dimension_is_still_there() -> None:
    """反向对照：删的是"占用"，**不是**"所有权"。

    少了这条，把 `project_busy` 也一起删掉能让上面两条照样绿 —— 而那会让
    "两个进程抢同一个工作区"变成一句兜底的「平台内部错误」。
    """
    from app.services.run_failures import _COPY

    assert "project_busy" in _COPY
    assert "工作区" in _COPY["project_busy"].body["zh"]
    assert "workspace" in _COPY["project_busy"].body["en"]

"""主循环里每一条"再来一轮"都要说明为什么（issue #733 第一步的闸）。

PR #739 给 `_run_loop_body` 加了 `turn_transition` 事件，但那是**逐个手加**的
——手加就会漏，而且漏了没人知道：复核发现 context-400 回滚重试
（`_ctx400_retries`）继续下一轮时没写 reason，于是回放这条 run 时"上一轮为什么
没停"答不出来，而这正是 #733 的验收判据。

所以闸不能是"我数了一下现在有 4 个"（硬编码枚举 = 新东西默认漏过），得是扫盘：
主循环里每一条 `continue`，之前必须有一条 turn_transition。下次谁再加恢复路径、
忘了写 reason，就是红的。
"""
from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "core" / "agent_loop.py"


def _own_continues(node: ast.AST) -> list[ast.stmt]:
    """属于**这一层**循环的 continue 及其所在块（内层循环的不算）。"""
    found = []
    for field in ("body", "orelse", "finalbody"):
        for block in ([getattr(node, field, None)] if field != "handlers" else []):
            if not isinstance(block, list):
                continue
            for i, stmt in enumerate(block):
                if isinstance(stmt, ast.Continue):
                    found.append((block, i))
                elif not isinstance(stmt, (ast.For, ast.AsyncFor, ast.While)):
                    found.extend(_own_continues(stmt))
    for handler in getattr(node, "handlers", []) or []:
        found.extend(_own_continues(handler))
    return found


def _is_turn_transition(stmt: ast.stmt) -> bool:
    if not isinstance(stmt, ast.Expr):
        return False
    call = stmt.value.value if isinstance(stmt.value, ast.Await) else stmt.value
    return (isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and call.func.attr == "append_transcript"
            and bool(call.args)
            and isinstance(call.args[0], ast.Constant)
            and call.args[0].value == "turn_transition")


def test_every_continue_in_the_turn_loop_declares_a_reason():
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
              and n.name == "_run_loop_body")
    loop = next(n for n in ast.walk(fn)
                if isinstance(n, ast.For) and isinstance(n.target, ast.Name)
                and n.target.id == "turn")

    sites = _own_continues(loop)
    assert sites, "主循环里一条 continue 都扫不到 —— 判据自己失效了"

    missing = [block[i].lineno for block, i in sites
               if not any(_is_turn_transition(s) for s in block[:i])]
    assert not missing, (
        f"core/agent_loop.py 这些行的 `continue` 没有先写 turn_transition：{missing}\n"
        "→ 每条'再来一轮'都要说明 reason，否则回放时答不出'上一轮为什么没停'（#733）"
    )

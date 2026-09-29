"""401 不能只是一个数字 —— 被拒的原因必须到得了读的人手里（#836 / #824）。

CI runner 上间歇性出现「本该有效的 token 拿到 401」，每次打在不同的端点上，
本机从未复现。401 本身不带任何信息，于是**三次立案都只能归档成「说不清」**。

中间补过一处诊断，用的是 `logger.info`。它一次都没响过：这套 pytest 没配
`log_level`，root logger 就停在 WARNING，`logger.info(...)` 连 LogRecord 都不会
生成；而把等级调到 INFO 的 `setup_logging` 只在 lifespan 里跑，测试走
`ASGITransport`，根本不进 lifespan。**诊断没到读的人手里，和没有诊断在报告里
长得一模一样**——这才是那三次查不出来的真正原因。

所以这两条判据都不问"代码里有没有写日志"：
- 一条验效果：真走一次 HTTP 拒绝，在**失败报告默认会印出来的那一层**看得见原因；
- 一条扫盘：`get_current_user` 里任何一条拒绝路径都必须经过 `_refuse`，不写
  分支名单（名单式判据下一条新分支默认变哑）。
"""
from __future__ import annotations

import ast
import inspect
import logging

from app import auth


async def test_a_refusal_is_visible_at_the_level_the_suite_actually_captures(client, caplog):
    """判据钉在 WARNING 这一层，因为这就是失败报告默认会印出来的那一层。"""
    caplog.set_level(logging.WARNING, logger="app.auth")

    refused = await client.get(
        "/api/v1/auth/me", headers={"Authorization": "Bearer not-a-real-token"}
    )
    assert refused.status_code == 401

    said = [r for r in caplog.records if r.name == "app.auth" and "refused" in r.getMessage()]
    assert said, (
        "401 发出去了，日志里一个字都没有 —— 下一次 CI 上随机 401 又只能归档成"
        f"「说不清」。caplog: {caplog.text!r}"
    )
    assert all(r.levelno >= logging.WARNING for r in said)
    assert "not-a-real-token" not in caplog.text  # 留证据 ≠ 把凭据写进日志


def test_every_refusal_in_get_current_user_goes_through_the_one_place_that_speaks():
    """不写分支名单：扫 `get_current_user` 自己的 AST。

    名单式判据的毛病是新加的那条分支默认不在名单上，而漏掉的症状正是这个 bug
    最贵的部分 —— 一个不带任何信息的 401。
    """
    tree = ast.parse(inspect.getsource(auth.get_current_user))
    raises = [node for node in ast.walk(tree) if isinstance(node, ast.Raise) and node.exc]
    assert raises, "扫描本身落空了（函数里一条 raise 都没找到）"

    offenders = [
        ast.unparse(node.exc)
        for node in raises
        if not (
            isinstance(node.exc, ast.Call)
            and isinstance(node.exc.func, ast.Name)
            and node.exc.func.id == "_refuse"
        )
    ]
    assert not offenders, (
        f"这些拒绝路径绕过了 `_refuse`，于是它们发出的 401 不说为什么：{offenders}"
    )

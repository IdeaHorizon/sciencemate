"""跨层调用的关键字参数必须真的对得上 —— 2026-08-19 的回归。

事故：`choiceId 端到端` 那次改动，六跳里五跳都改了，唯独
`HarnessSessionManager.answer` 那一跳的编辑**没落到盘上**，而 commit message 里
写了"harness_sessions RPC 带 choice"。语法检查全过、单测全绿、tsc 干净 —— 因为
没有任何一条检查去问"调用方传的参数，被调方收得到吗"。

结果：`local_execution` 传 `choice=`，`manager.answer()` 没这个形参 →
**TypeError，答复任何 pause 当场炸**。比改之前更糟：改之前只是丢授权，改之后
是整轮判死。

这条测试不写名单：用 AST 找出调用点实际传的每个关键字，逐个对被调方的签名。
下一个跨层参数漏接同样会被它逮住。
"""
import ast
import inspect
import pathlib

from app.services.harness_sessions import HarnessSessionManager

_BACKEND = pathlib.Path(__file__).resolve().parents[1]


def _kwargs_passed_to(source: pathlib.Path, attr_name: str) -> set[str]:
    """源码里所有 `<something>.<attr_name>(...)` 调用实际传的关键字名。"""
    tree = ast.parse(source.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == attr_name:
            names.update(kw.arg for kw in node.keywords if kw.arg)
    return names


def test_local_execution_only_passes_kwargs_that_the_manager_accepts():
    passed = _kwargs_passed_to(
        _BACKEND / "app" / "services" / "local_execution.py", "answer")
    assert passed, "没找到 manager.answer 的调用点 —— 测试本身失效了，先修测试"

    accepted = set(inspect.signature(HarnessSessionManager.answer).parameters)
    missing = sorted(passed - accepted)
    assert not missing, (
        f"local_execution 传了 manager.answer 收不到的参数：{missing}。"
        "跨层参数只改一半 = 调用当场 TypeError。"
    )


def test_the_human_choice_actually_reaches_the_rpc_envelope():
    """`choice` 不只是签名上有，得真进 RPC 信封。

    只对签名不够：加一个形参然后在函数体里不用它，签名测试照样绿，而人的
    选择依旧到不了 harness —— 那正是"机制存在但没接到路径"。
    """
    session_src = (_BACKEND / "app" / "services" / "harness_sessions.py").read_text(
        encoding="utf-8")
    assert '"op": "answer"' in session_src
    envelope = session_src[session_src.index('"op": "answer"'):]
    envelope = envelope[:envelope.index("},")]
    assert "choice" in envelope, (
        "RPC 信封里没有 choice —— 人点的按钮身份到不了 harness，"
        "会静默回落到按文案解析（正是这次要消灭的那条路）"
    )

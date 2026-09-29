"""桥失败的时候必须说出为什么 —— 一次性调用只有一份实现。

## 根因（2026-09-10 真机）

Windows 上装好的应用里 session autoname 失败，日志里全部的信息就是：

    app.services.session_naming.SessionNamingError: runtime_error

`runtime_error` 是桥的 error 事件里的 `code`。**解释它的那几行 traceback 就在
被丢掉的 stderr 里** —— `stdout, _ = await process.communicate(...)`：stderr 用
`PIPE` 收了，然后扔掉。`harness_kb._ask` 和 `session_naming._ask_harness_for_title`
是同一段四十行舞步的两份手抄件，两份都这么丢。

`harness_runtime` 那条流式路径早就做对了（drain、打码、把 stderr 尾巴带进错误），
两份抄件却没跟上 —— 这是「一个问题几份抄件就有几个答案，分叉时两边都不报错」。

判据钉在**行为**上：真起一个往 stderr 吐话、然后回一个光秃秃 error 事件的子进程，
断言抛出来的消息里**能看到那句话**。钉源码里有没有 `stderr` 这个词是没用的 ——
它本来就在（在被丢掉的那一侧）。
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

WHAT_THE_CHILD_SAID = "Traceback: 这一句必须出现在错误里，否则没人查得动"


def _fake_harness(root: Path, script: str) -> None:
    """一个最小 harness checkout：够过两个调用方的入口校验，行为由脚本定。"""
    (root / "core").mkdir(parents=True, exist_ok=True)
    (root / "core" / "llm.py").write_text("", encoding="utf-8")
    (root / "core" / "api.py").write_text("", encoding="utf-8")
    (root / "platform_runtime.py").write_text(script, encoding="utf-8")


def _child(*, says: str, then_prints: str) -> str:
    return (
        "import sys\n"
        "sys.stdin.readline()\n"
        f"sys.stderr.write({says!r})\n"
        "sys.stderr.flush()\n"
        f"sys.stdout.write({then_prints!r})\n"
        "sys.stdout.flush()\n"
    )


@pytest.fixture()
def bridge_env(tmp_path, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "harness_root", str(tmp_path))
    monkeypatch.setattr(settings, "harness_python", sys.executable)
    return tmp_path


@pytest.mark.asyncio
async def test_a_bare_error_code_still_tells_you_what_the_child_said(bridge_env) -> None:
    """桥只给了个 code（真机上就是 `runtime_error`）—— stderr 是唯一线索，必须带上。"""
    from app.services import harness_kb

    _fake_harness(bridge_env, _child(says=WHAT_THE_CHILD_SAID,
                                     then_prints='{"type": "error", "code": "runtime_error"}\n'))
    with pytest.raises(harness_kb.HarnessKBError) as caught:
        await harness_kb._ask({"op": "kb_query"}, expect="kb_query_result")
    message = str(caught.value)
    assert "runtime_error" in message, "桥自己那句 code 没带上来"
    assert WHAT_THE_CHILD_SAID in message, (
        "子进程往 stderr 说的话被丢了 —— 这条失败没人查得动（真机上就是这样）")


@pytest.mark.asyncio
async def test_no_result_at_all_reports_the_exit_code_and_the_stderr(bridge_env) -> None:
    """一个字都没回：错误里要有退出码和 stderr，而不是一句「没有结果」。"""
    from app.services import harness_kb

    _fake_harness(bridge_env, _child(says=WHAT_THE_CHILD_SAID, then_prints="") + "raise SystemExit(7)\n")
    with pytest.raises(harness_kb.HarnessKBError) as caught:
        await harness_kb._ask({"op": "kb_query"}, expect="kb_query_result")
    message = str(caught.value)
    assert WHAT_THE_CHILD_SAID in message
    assert "7" in message, "退出码没带上来 —— 「桥没给结果」和「桥崩了」看起来会一样"


@pytest.mark.asyncio
async def test_the_naming_path_surfaces_it_too(bridge_env) -> None:
    """判据要落在**真调用方**上：helper 做对了但没接线，等于没做。

    这一条走 `session_naming._ask_harness_for_title` 的真实入口，顺带钉住
    「凭据不许进日志」：子进程把 API key 吐到 stderr，错误消息里必须是打码后的。
    """
    from app.services import session_naming

    secret = "sk-must-not-appear-1234567890"
    _fake_harness(bridge_env, _child(
        says=f"{WHAT_THE_CHILD_SAID} key={secret}",
        then_prints='{"type": "error", "code": "runtime_error"}\n'))
    with pytest.raises(session_naming.SessionNamingError) as caught:
        await session_naming._ask_harness_for_title(
            user_id="u1", project_id="p1", message="你好",
            api_key=secret, base_url="http://localhost:1/v1", model="m")
    message = str(caught.value)
    assert WHAT_THE_CHILD_SAID in message, "命名这条路没接上 —— 真机上它就是哑的那一条"
    assert secret not in message, "模型凭据原样进了错误消息（会落进日志）"
    assert "[REDACTED]" in message


def test_one_shot_bridge_calls_have_exactly_one_implementation() -> None:
    """起 `platform_runtime` 子进程的地方要么是收口的那一处，要么是有理由的长连接。

    扫的是**这件事**（AST 里的调用），不是措辞：注释里为了解释这条规则一定会写出
    `platform_runtime` 这几个字。允许名单是白名单 —— 新加一处就得在这里说明它
    为什么不能走 `harness_bridge_once`，否则默认漏过。
    """
    services = Path(__file__).resolve().parents[1] / "app" / "services"
    allowed = {
        # 收口后的唯一一次性实现。
        "harness_bridge_once.py",
        # 流式：一路读事件回放给用户，不是问一个问题拿一个答案。
        "harness_runtime.py",
        # 长连接 worker（`--serve`），生命周期跨多轮。
        "harness_sessions.py",
        # 资讯流：自己的流式协议 + 进度回调。
        "bridge.py",
    }
    spawners = set()
    for path in services.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if "create_subprocess_exec" not in ast.unparse(node.func):
                continue
            if any(isinstance(a, ast.Constant) and a.value == "platform_runtime" for a in node.args):
                spawners.add(path.name)
    stray = spawners - allowed
    assert not stray, (
        f"{sorted(stray)} 又手抄了一遍「起桥问一个问题」—— 走 harness_bridge_once.ask_once；"
        "抄件的代价是同一个字段在两处被同样地丢掉（2026-09-10 就是这么丢的）")
    for gone in ("harness_kb.py", "session_naming.py"):
        assert gone not in spawners, f"{gone} 又自己起子进程了 —— 收口白做了"

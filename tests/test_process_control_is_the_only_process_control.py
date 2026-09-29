"""扫盘闸：运行时代码碰进程组 / 信号 / 父进程只许经 `shared.lib.process_control`。

三样东西在 Windows 上各有一种静默失效（`start_new_session=True` 被忽略、
`signal.SIGKILL` 不存在、`os.getppid()` 父死不变），收口之后任何新的直接调用都是
把其中一种加回来。用 AST 扫调用与关键字，不扫文本。
"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

SCAN = [
    ROOT / "core",
    ROOT / "shared",
    ROOT / "nodes",
    ROOT / "platform_runtime.py",
    ROOT / "chat.py",
    ROOT / "platform" / "backend" / "app",
]
THE_ONLY_ANSWER = ROOT / "shared" / "lib" / "process_control.py"

#: `os.<name>(...)` 这些调用只许出现在 process_control 里。
OS_CALLS = {"killpg", "setsid", "getpgid", "fork", "kill", "getppid"}
#: `signal.SIGKILL` 在 Windows 上不存在。
SIGNAL_ATTRS = {"SIGKILL"}
#: 起进程时的这个关键字在 Windows 上被静默忽略。
SPAWN_KEYWORDS = {"start_new_session"}


def _runtime_files():
    for entry in SCAN:
        if entry.is_file():
            yield entry
            continue
        for path in entry.rglob("*.py"):
            if "tests" in path.parts:
                continue
            yield path


def _uses(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    rel = path.relative_to(ROOT)
    hits: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            if (
                isinstance(fn, ast.Attribute)
                and isinstance(fn.value, ast.Name)
                and fn.value.id == "os"
                and fn.attr in OS_CALLS
            ):
                hits.append(f"{rel}:{node.lineno} os.{fn.attr}()")
            for kw in node.keywords:
                if kw.arg in SPAWN_KEYWORDS:
                    hits.append(f"{rel}:{node.lineno} {kw.arg}=")
        elif isinstance(node, ast.Attribute):
            if isinstance(node.value, ast.Name) and node.value.id in {"signal", "_signal"} and node.attr in SIGNAL_ATTRS:
                hits.append(f"{rel}:{node.lineno} signal.{node.attr}")
    return hits


def test_the_scanner_sees_the_one_place_that_may_touch_processes():
    assert _uses(THE_ONLY_ANSWER), "扫描器认不出 process_control.py 里的 os.killpg / setsid，守卫会静默归零"


def test_no_runtime_code_touches_process_groups_outside_process_control():
    offenders = [hit for path in _runtime_files() if path != THE_ONLY_ANSWER for hit in _uses(path)]
    assert not offenders, (
        "进程组 / 信号 / 父进程只能经 shared.lib.process_control"
        "（Group / group_spawn_kwargs / detached_spawn_kwargs / alive / terminate / kill / "
        "parent_probe / watch_parent / daemonize）—— 别把 Windows 上静默失效的写法加回来：\n  "
        + "\n  ".join(offenders)
    )

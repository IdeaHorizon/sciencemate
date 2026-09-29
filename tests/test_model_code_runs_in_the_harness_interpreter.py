"""交给模型代码跑的 Python，只有一个答案：跑着 harness 的那一个。

## 这条测试为什么存在

2026-09-06 真机实测（Mac 安装包，一个真课题跑到 experiment 节点）：
`safe_execute_python` 用的解释器写死成 `/usr/bin/python3`，而在原生 macOS 上
那是 **Xcode 的 python 3.9，没有 numpy**。现场是模型接连跑四条命令满硬盘找
numpy：

    ls -l /usr/bin/python3 /opt/homebrew/bin/python3; which python3
    find /usr/local/lib /opt/homebrew/lib /Library/Frameworks -name numpy -type d
    === any venv in project ===

沙箱本身没问题（四条命令全 returncode 0）。缺的只是"用哪个 Python"这个答案，
而它一直就在进程自己身上。

同一时刻，框架自己的 `python_exec` 用的是 `sys.executable` —— 同一个问题两个
答案，其中一个只在 Docker 年代成立（容器里的 `/usr/bin/python3` 就是那份装好
科学栈的解释器）。删掉 Docker、改成原生执行之后，那个答案在**任何**原生安装上
都是错的。

## 判据的形状

不写"哪些文件不许出现哪个字符串"这种名单 —— 扫的是**执行点**：凡是把一个
Python 解释器交给子进程去跑的地方，用的必须是那个函数，不能是绝对路径字面量。
新加的执行点默认违规。
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

import pytest

from shared.lib.cancellable_subprocess import the_interpreter_for_model_code

_REPO = Path(__file__).resolve().parents[1]

#: 扫这几棵树。`tests/` 与 `docs/` 不在其中：测试可以为了复现而写死路径，
#: 文档里的路径是举例。
_TREES = ("core", "shared", "nodes")

#: 长得像"一个 Python 解释器的绝对路径"。
_AN_ABSOLUTE_PYTHON = re.compile(r"^/(usr|opt|Library|System)/[^\s\"']*python[0-9.]*$")


def test_the_answer_is_the_running_interpreter() -> None:
    """它就是这个进程自己 —— 不是配置、不是探测、不是路径。

    源码 checkout、wheel 安装、`.app` 里随包分发，三种形态下模型代码要的依赖
    与 harness 自己的是同一套、装在同一份运行时里。任何绝对路径都只在其中一种
    形态下成立。
    """
    assert the_interpreter_for_model_code() == sys.executable
    assert Path(the_interpreter_for_model_code()).is_file()


def _hardcoded_interpreters_handed_to_a_subprocess() -> list[str]:
    """扫盘：把一个绝对 Python 路径当成命令去跑的地方。

    只认**执行点**：f-string 拼进 `python_cmd` / 直接进 `spawn_*` 的第一个实参。
    注释、docstring、报错文案里出现路径都不算 —— 那些是在说明，不是在执行。
    """
    offenders: list[str] = []
    for tree in _TREES:
        for path in sorted((_REPO / tree).rglob("*.py")):
            if "/tests/" in path.as_posix() or path.name.startswith("test_"):
                continue
            try:
                source = path.read_text(encoding="utf-8")
            except OSError:  # pragma: no cover
                continue
            try:
                tree_ast = ast.parse(source)
            except SyntaxError:  # pragma: no cover - 模板之类
                continue
            for node in ast.walk(tree_ast):
                # 形如 f"/usr/bin/python3 -c {...}" 或 "/usr/bin/python3"
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    head = node.value.strip().split(" ", 1)[0]
                    if _AN_ABSOLUTE_PYTHON.match(head):
                        rel = path.relative_to(_REPO).as_posix()
                        offenders.append(f"{rel}:{node.lineno} → {head}")
    return offenders


def test_no_execution_site_hardcodes_an_interpreter() -> None:
    """没有执行点写死解释器路径。

    扫盘不写名单：下一个执行点默认违规。名单式的护栏挡不住"下一个人"，而这条
    缺陷的全部代价就发生在"第二个人照着抄一个绝对路径"那一刻。
    """
    offenders = _hardcoded_interpreters_handed_to_a_subprocess()
    assert offenders == [], (
        "这些地方把一个绝对 Python 路径交给子进程去跑：\n  "
        + "\n  ".join(offenders)
        + "\n改成 the_interpreter_for_model_code()。"
    )


def test_the_interpreter_can_actually_run_model_code() -> None:
    """答出来的那个解释器要真的能跑代码。

    "返回了一个字符串"和"那个字符串能跑"是两件事：`/usr/bin/python3` 也返回得
    出来，它只是没有 numpy。
    """
    import subprocess

    done = subprocess.run(
        [the_interpreter_for_model_code(), "-c", "print(6 * 7)"],
        capture_output=True, text=True, timeout=60,
    )
    assert done.returncode == 0, done.stderr[:300]
    assert done.stdout.strip() == "42"


@pytest.mark.parametrize("module", ["json", "pathlib"])
def test_the_standard_library_is_there(module: str) -> None:
    """随包分发的解释器要是完整的一份，不是壳。"""
    import subprocess

    done = subprocess.run(
        [the_interpreter_for_model_code(), "-c", f"import {module}"],
        capture_output=True, text=True, timeout=60,
    )
    assert done.returncode == 0, done.stderr[:300]

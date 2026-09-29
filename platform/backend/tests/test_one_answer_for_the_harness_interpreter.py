"""「哪个 Python 跑 harness」只有一个答案。

2026-09-06 真机实测，装好的 `.app` 里 `HARNESS_PYTHON` 是空的：

- **spawn 那条路**写的是 `settings.harness_python or sys.executable` —— 有兜底，
  worker 起得来，作业照跑，沙箱照守（同一份 harness 直接问，五项全在）。
- **启动探针那条路**写的是 `Path(settings.harness_python)` —— 没兜底。空串经
  `os.path.abspath("")` 变成**当前工作目录**，而 Launch Services 起的应用 cwd 是
  `/`，于是探针去执行 `/`，拿回 `[Errno 13] Permission denied`。

日志因此写着「这台机器没有执行边界」，而边界好好的 —— 平台在往"更不安全"的方向
谎报自己。两处各答一次、其中一处忘了兜底，是这类缺陷的固定长相；判据因此不落在
"探针能不能跑"上，而落在**"这个问题只有一个地方回答"**上。
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

from app.services.harness_runtime import the_interpreter_that_runs_the_harness

_BACKEND = Path(__file__).resolve().parents[1]

#: 允许读 `harness_python` 这个配置项的地方：定义它的 config，和回答这个问题的
#: 那个函数所在的模块。别处一律走那个函数。
_MAY_READ_IT = {"app/config.py", "app/services/harness_runtime.py"}


def test_no_configuration_still_gives_a_usable_interpreter(monkeypatch) -> None:
    """没配 = 用跑着这个后端的那一个，不是空串。

    空串会经 `os.path.abspath("")` 变成当前工作目录 —— 一个**看起来像路径**的
    答案，于是失败发生在 exec 那一刻，报的是 Permission denied，指不到病因。
    """
    from app.config import settings

    monkeypatch.setattr(settings, "harness_python", "")
    answer = the_interpreter_that_runs_the_harness()
    assert answer == sys.executable
    assert Path(answer).is_file(), "答出来的东西必须真的是个可执行文件"


def test_an_explicit_choice_wins(monkeypatch) -> None:
    """显式配了就用配的 —— node20 那类部署要指到一个特定的 venv。"""
    from app.config import settings

    monkeypatch.setattr(settings, "harness_python", "/somewhere/bin/python")
    assert the_interpreter_that_runs_the_harness() == "/somewhere/bin/python"


def _modules_reading_the_setting() -> list[str]:
    """扫盘：谁在读 `settings.harness_python`。"""
    offenders: list[str] = []
    for path in sorted(_BACKEND.glob("app/**/*.py")):
        relative = path.relative_to(_BACKEND).as_posix()
        if relative in _MAY_READ_IT:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Attribute)
                and node.attr == "harness_python"
                and isinstance(node.value, ast.Name)
                and node.value.id == "settings"
            ):
                offenders.append(f"{relative}:{node.lineno}")
    return offenders


def test_nobody_else_reads_the_setting_directly() -> None:
    """别处一律走那个函数。

    扫盘，不写名单：新加的读取点默认违规。名单化的护栏挡不住"下一个人"，而
    这条缺陷的全部代价就发生在"第二个人照着抄、但忘了抄兜底"那一刻。
    """
    offenders = _modules_reading_the_setting()
    assert offenders == [], (
        "这些地方自己读了 harness_python，绕过了唯一的答案："
        + ", ".join(offenders)
        + "。改成调用 the_interpreter_that_runs_the_harness()。"
    )


@pytest.mark.parametrize("cwd_is_root", [True])
def test_the_probe_does_not_try_to_execute_a_directory(monkeypatch, cwd_is_root: bool) -> None:
    """空配置 + cwd 是 `/` 时，算出来的不能是一个目录。

    这是真机上那次失败的最小复现：`.app` 由 Launch Services 启动，cwd 是 `/`。
    """
    import os

    from app.config import settings

    monkeypatch.setattr(settings, "harness_python", "")
    monkeypatch.chdir("/")
    resolved = Path(os.path.abspath(Path(the_interpreter_that_runs_the_harness()).expanduser()))
    assert resolved != Path("/")
    assert resolved.is_file()

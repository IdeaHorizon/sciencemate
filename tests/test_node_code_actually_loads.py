"""节点的代码没加载起来，就不许当成一个能跑的节点。

## 为什么（2026-08-21 实测）

`nodes/hypothesis/tools/artifact_save.py` 里有一行

    f"... {sorted(t for t in ("research_plan", ...))} ..."

f-string 里嵌同种引号是 PEP 701 —— **Python 3.12+ 才合法**。CI 的基础镜像
（node:20-bookworm）是 Python 3.11，这行是 SyntaxError。

后果不是"红"，是**静默**：`core.bootstrap` 对节点模块的 import 失败一向是
`log.warning` 然后继续，于是 `nodes.hypothesis.tools` 和 `nodes.hypothesis.hooks`
整个没加载 —— hypothesis 节点**一件工具都没注册**，却照常启动、照常开跑。
harness.yaml 里点名的 11 个工具（`audit_*` 全套 / `get_research_goal` /
`update_research_state` / `validate_hypothesis_outputs`）模型一个都调不到。
现场看起来是"模型不好好干活"，真相是"代码没加载"。日志里只有两行 WARNING。

这一片管两件事：

1. **扫盘，不写名单**：仓库里每个 .py 在**当前解释器**上都得能 parse。
   PEP 701 那一类"在我机器上能跑、在 CI 上是语法错"的写法，判据必须是跑
   CI 那个 Python 去 parse，而不是列一张禁用写法名单。
2. **失效方向要翻转**：bootstrap 记账 + `load_harness` 拒绝启动。对框架来说
   一个节点坏了不该拖垮别的节点（所以别的节点照常 boot）；对**这个**节点来说
   "少了全部工具"必须是拒绝，不是降级。
"""
from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _repo_python_files() -> list[Path]:
    """我们自己的 .py —— 判据是「git 跟踪它」，不是一张排除名单。

    别用 `rglob("*.py")` + 排除目录名：那是一张名单，新冒出来的 vendor 目录
    （node_modules / .venv / vendor / .tox / 某次 npm ci 的残留）默认会被扫进来。
    self-hosted runner 的 workspace 还跨 run 存活，扫到别人家的代码就会拿别人的
    语法给我们判红。git 跟踪面就是"我们的代码"这个问题的真相源。
    """
    try:
        out = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "ls-files", "-z", "*.py"],
            capture_output=True, check=True,
        ).stdout.decode("utf-8", errors="replace")
        tracked = [REPO_ROOT / name for name in out.split("\0")
                   if name and (REPO_ROOT / name).is_file()]
        if tracked:
            return tracked
    except (OSError, subprocess.SubprocessError):
        pass
    # 不是 git 仓库（tarball / 打包镜像）就退回走目录 —— 边走边剪，别先收集再过滤。
    # 退回的是**更吵**的那一档（可能多扫到东西），不是静默跳过：护栏宁可误报也
    # 不能假绿。
    import os

    pruned = {".git", ".venv", "venv", "node_modules", "site-packages",
              "__pycache__", ".tox", ".mypy_cache", ".claude"}
    found: list[Path] = []
    for root, dirs, files in os.walk(REPO_ROOT):
        dirs[:] = [d for d in dirs if d not in pruned]
        found.extend(Path(root) / f for f in files if f.endswith(".py"))
    return found


def test_every_python_file_parses_on_this_interpreter() -> None:
    """每个 .py 在跑测试的这个 Python 上都得 parse 得动。

    判据故意用**当前解释器**：CI 上跑的就是 CI 那个版本，所以"只有新版本才认
    的语法"会在 CI 上被逐字抓住，不需要维护一张写法黑名单。
    """
    broken: list[str] = []
    for path in sorted(_repo_python_files()):
        try:
            ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError as exc:
            rel = path.relative_to(REPO_ROOT)
            broken.append(f"{rel}:{exc.lineno}  {exc.msg}")
        except ValueError as exc:
            # 含 null 字节之类：`ast.parse` 抛的是 ValueError 不是 SyntaxError。
            # 不接住的话这条护栏自己会以未处理异常收场 —— 报错指向 ast.py，
            # 而不是指向那个文件。
            broken.append(f"{path.relative_to(REPO_ROOT)}  {exc}")
    assert not broken, (
        f"以下文件在 Python {sys.version.split()[0]} 上是语法错 —— "
        "多半是用了更高版本才认的写法（如 PEP 701 的 f-string 嵌同种引号）：\n"
        + "\n".join(broken)
        + "\nCI 跑的就是这个版本；在这里过不了 = 线上那个模块根本 import 不进去。"
    )


def test_bootstrap_leaves_no_node_import_failures() -> None:
    """bootstrap 之后不该有任何节点模块 import 失败。

    这条比 `test_prompt_named_tools_exist` 更靠上游：那条抓的是"工具没注册"的
    **后果**，这条抓的是"模块没加载"的**原因**，且不依赖 harness.yaml 恰好点了名。
    """
    from core.bootstrap import bootstrap, node_import_failures

    bootstrap(force=True)
    failures = node_import_failures()
    assert not failures, (
        "以下节点模块在 bootstrap 时 import 失败 —— 它们注册的工具/hook 一件都不在 "
        "registry 里，而节点照常会被启动：\n"
        + "\n".join(f"  {node}: {mods}" for node, mods in sorted(failures.items()))
    )


def test_load_harness_refuses_a_node_whose_code_did_not_load(monkeypatch) -> None:
    """闸真的会响：账本里有这个节点 → load_harness 拒绝，且不误伤别的节点。

    直接往账本里塞一条（而不是真去写坏一个文件）—— 要验的是"账本 → 拒绝"这段
    接线，写坏文件那半段由上面两条覆盖。
    """
    from core import bootstrap as bootstrap_mod
    from core.loader import load_harness

    bootstrap_mod.bootstrap()
    monkeypatch.setitem(
        bootstrap_mod._node_import_failures, "hypothesis",
        {"nodes.hypothesis.tools": "SyntaxError: injected"},
    )

    with pytest.raises(RuntimeError) as excinfo:
        load_harness("hypothesis")
    message = str(excinfo.value)
    assert "hypothesis" in message
    # 报错必须把真正的原因带到眼前，而不是只说"启动失败"。
    assert "nodes.hypothesis.tools" in message and "injected" in message

    # 别的节点不受影响：一个节点坏了不该拖垮全框架。
    load_harness("literature")

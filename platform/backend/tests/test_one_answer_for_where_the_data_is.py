"""「数据在哪」这个问题，整个后端只有一个答案。

## 病例（2026-09-07 真机）

拿安装包起一个干净实例：

    PLATFORM_DATA_ROOT=/tmp/fresh-afs "…/Agent for Science.app/…/python3" \
        -m app.launcher start --port 53999 --no-browser

开机第一行印的是 `数据在 /Users/wddddds/.harness-framework`，而库真的建在
`/tmp/fresh-afs/db.sqlite`。用户读到的第一句话就是错的。

印错只是表面。真正的代价在 harness：它整个状态根取 `HARNESS_FRAMEWORK_HOME`
（`core/paths.py`），而 launcher 用**自己那份抄件**去 setdefault 这个变量。于是
后端把项目写进 A、worker 把知识库/记忆/运行态写进 B，两边都不报错 —— 08-21 丢掉
43 个会话就是这个形状。

根因不是某一行写错，是这个问题**有四个各自演化的答案**：`config.py` 一份、
`launcher.py` 一份、`services/instructions.py` 一份、`services/feed/
literature_projection.py` 一份，每份都是
``os.environ.get("HARNESS_FRAMEWORK_HOME", "~/.harness-framework")``，只在默认
路径下碰巧一致。

## 判据

1. 后端里**只有 `app/config.py`** 可以提到那个环境变量名或那个默认路径；
2. 两个变量同时配、指着不同目录时，**起不来**（不是挑一个用）；
3. `publish_the_data_root()` 交给 harness 的，就是 `the_data_root()` 那一个。
"""
from __future__ import annotations

import ast
import os
from pathlib import Path

import pytest

from tests.repository_sources import REPO_ROOT, source_files

#: 允许知道这两个字面量的唯一文件。它是这个问题的 owner。
#: 「数据在哪」的两个 owner —— 而且只能是这两个：
#:   config.py             回答"这台部署配的是哪"（the_data_root / publish 给 harness）
#:   data_root_default.py  回答"没配时默认是哪"（零依赖，launcher 在导入 config 之前就要它）
#: 第二个文件存在的唯一理由是自更新：载荷指针住在数据根里，launcher 得先切换载荷、
#: 再让 config 拍快照。它不 import 任何 app 模块，config 自己也从它读。
_THE_OWNERS = {
    Path("platform/backend/app/config.py"),
    Path("platform/backend/app/data_root_default.py"),
}

#: 后端范围。harness（core/、shared/、nodes/）读这个变量是**合法的** ——
#: 环境变量正是平台交给 harness 的那条接口；这条闸管的是平台这一侧不许各算各的。
_BACKEND = Path("platform/backend")

_LITERALS = ("HARNESS_FRAMEWORK_HOME", ".harness-framework")


def _backend_sources() -> list[tuple[Path, str]]:
    return [
        (path, text)
        for path, text in source_files("*.py")
        if _BACKEND in path.parents or str(path).startswith(str(_BACKEND))
    ]


def _code_literals(text: str) -> list[tuple[int, str]]:
    """源码里**参与计算**的字符串字面量（行号, 内容）。

    散文不是答案。注释里写 "数据根是 ~/.harness-framework" 没有决定任何事情，
    文档字符串同理；决定事情的是被代码用到的那个字面量。所以这里按 AST 取
    字面量、扣掉模块/类/函数的文档字符串 —— 注释根本不是字面量，天然不在内。

    （扫盘闸命中自己的解释性注释，在这个仓库里是第四次了。判据要扫**这件事**，
    不是扫写法。）
    """
    try:
        tree = ast.parse(text)
    except SyntaxError:  # pragma: no cover - 语法错另有闸管
        return []
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = getattr(node, "body", None)
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            docstrings.add(id(body[0].value))
    return [
        (node.lineno, node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    ]


def test_only_config_knows_where_the_default_root_is() -> None:
    """抄件为零：别的文件想知道根在哪，只能调 `the_data_root()`。"""
    offenders: list[str] = []
    for path, text in _backend_sources():
        if path in _THE_OWNERS or "tests" in path.parts:
            continue
        for line_number, literal in _code_literals(text):
            if any(name in literal for name in _LITERALS):
                offenders.append(f"{path}:{line_number}: {literal.strip()[:100]}")

    assert not offenders, (
        "这些地方自己算了一遍「数据在哪」：\n" + "\n".join(offenders) +
        "\n抄件之间只在默认路径下碰巧一致，分叉时两边都不报错。"
        "改成 `from app.config import the_data_root`。"
    )


def test_the_scan_is_not_vacuous() -> None:
    """语料里确实有后端源码，而且 owner 自己确实提到了那两个字面量。

    没有这条，`_backend_sources()` 一旦筛空（路径规则写歪、遍历规则换了），
    上面那条会**全绿着**把闸关掉。
    """
    sources = _backend_sources()
    assert len(sources) > 50, f"只扫到 {len(sources)} 个后端文件 —— 判据在空跑"

    owner_text = "\n".join(
        value
        for owner in sorted(_THE_OWNERS)
        for _, value in _code_literals((REPO_ROOT / owner).read_text(encoding="utf-8"))
    )
    assert all(literal in owner_text for literal in _LITERALS), (
        "owner 文件里没有这两个字面量 —— 要么它们换了写法（那这条闸要跟着改），"
        "要么这个问题搬家了"
    )


def test_the_two_names_must_point_at_the_same_directory(monkeypatch, tmp_path) -> None:
    """两个变量指着不同目录 → 起不来，而不是静默挑一个。"""
    from app import config

    monkeypatch.setattr(config.settings, "platform_data_root", str(tmp_path / "platform"))
    monkeypatch.setenv(config.HARNESS_HOME_VARIABLE, str(tmp_path / "harness"))

    with pytest.raises(config.DataRootError) as excinfo:
        config.publish_the_data_root()
    message = str(excinfo.value)
    assert "platform" in message and "harness" in message, "报错得把两个路径都摆出来"


def test_agreeing_names_are_fine(monkeypatch, tmp_path) -> None:
    """两个变量指着同一个目录（含 `~` 与尾斜杠的写法差异）不算分叉。"""
    from app import config

    monkeypatch.setattr(config.settings, "platform_data_root", str(tmp_path))
    monkeypatch.setenv(config.HARNESS_HOME_VARIABLE, f"{tmp_path}/")
    assert config.publish_the_data_root() == tmp_path


def test_publishing_hands_the_harness_the_same_answer(monkeypatch, tmp_path) -> None:
    """没配 harness 那个变量时，交出去的就是 `the_data_root()`。"""
    from app import config

    monkeypatch.setattr(config.settings, "platform_data_root", str(tmp_path))
    # 先 setenv 再 delenv：`delenv` 一个本来不存在的键时 pytest **不记录**，
    # 于是 `publish_the_data_root()` 往 `os.environ` 写的那一笔没人复原，
    # 泄漏给后面的测试（实测把 launcher 的三条冲红了）。
    monkeypatch.setenv(config.HARNESS_HOME_VARIABLE, str(tmp_path))
    monkeypatch.delenv(config.HARNESS_HOME_VARIABLE)

    published = config.publish_the_data_root()
    assert published == config.the_data_root()
    assert os.environ[config.HARNESS_HOME_VARIABLE] == str(tmp_path)


def test_a_relative_root_is_refused(monkeypatch) -> None:
    """相对路径 = 数据位置取决于谁用什么 cwd 起的进程。"""
    from app import config

    monkeypatch.setattr(config.settings, "platform_data_root", "data")
    with pytest.raises(config.DataRootError):
        config.the_data_root()


# ── 只配一个变量时，两处也必须给同一个答案 ──────────────────────────────


def test_the_launcher_and_config_agree_when_only_the_harness_variable_is_set(monkeypatch) -> None:
    """只配 `HARNESS_FRAMEWORK_HOME`（DELIVERY.md 里写着的那个）时，两处必须一致。

    上面那道闸问的是「两个都配了吗」；这一条问的是**只配了一个**时，另一个"没配"
    算什么。原来两处答得不一样：config（个人档）认 `HARNESS_FRAMEWORK_HOME`，
    launcher 用的 `data_root_before_config()` 不认、退回默认根。

    后果不是报错，是**自更新永远装不上**：暂存写进 A，启动时去 B 找暂存，找不到；
    `GET /update` 一直显示 `installed=旧版 staged=新版`，点多少次都一样。
    2026-09-16 打 0.5.0 做自更新真机验时撞上。
    """
    from app.data_root_default import data_root_before_config

    monkeypatch.delenv("PLATFORM_DATA_ROOT", raising=False)
    monkeypatch.setenv("HARNESS_FRAMEWORK_HOME", "/tmp/only-the-harness-variable")

    assert data_root_before_config() == Path("/tmp/only-the-harness-variable"), (
        "launcher 在 config 之前算出的数据根，和 config 个人档算出的不是同一个 —— "
        "暂存的更新会写进一个根、启动时去另一个根找，自更新因此永远装不上且不报错"
    )


def test_an_explicit_platform_data_root_still_wins(monkeypatch) -> None:
    from app.data_root_default import data_root_before_config

    monkeypatch.setenv("PLATFORM_DATA_ROOT", "/tmp/explicit")
    monkeypatch.setenv("HARNESS_FRAMEWORK_HOME", "/tmp/harness")
    assert data_root_before_config() == Path("/tmp/explicit")

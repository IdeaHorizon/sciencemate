"""防复发闸：每一列都要过一遍尺子。

## 为什么是"登记义务"而不是"违规名单"

名单只枚举已知病灶，新东西默认漏过 —— 落地即死，而 CI 全绿没人知道
（[[feedback_guardrails_must_scan_not_list]]）。这里反过来：扫的是
`Base.metadata` 的**全部**列，每一列必须在 `_classification` 里有归属，
漏一个就红。加新列的人因此必须回答一次"它是哪一类"。

## 尺子

    IDENTITY    身份与外键
    DECISION    人拍的板
    PAST        已经发生的事（append-only）
    PROJECTION  别处可重建的投影（判据不许读）

"关于现在的落盘判决"没有格子可填 —— 那正是这张表的用意。
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from app.database import Base
from app.models._classification import (
    BY_TABLE,
    COMMON,
    DECISION,
    IDENTITY,
    PAST,
    PROJECTION,
)

VALID = {IDENTITY, DECISION, PAST, PROJECTION}


def _classification(table: str, column: str) -> str | None:
    return BY_TABLE.get(table, {}).get(column) or COMMON.get(column)


def test_every_column_has_been_through_the_razor() -> None:
    """新增一列而不登记 —— 这里当场红。

    这条的价值全在它会怎么坏：谁顺手给新表加一个 `status`（CRUD 的肌肉记忆），
    就必须先回答"它是身份、决定、过去，还是投影"。而"关于现在的落盘判决"
    在这四类里没有位置。
    """
    missing: list[str] = []
    for table_name, table in sorted(Base.metadata.tables.items()):
        for column in table.columns:
            if _classification(table_name, column.name) is None:
                missing.append(f"{table_name}.{column.name}")
    assert not missing, (
        "这些列还没过尺子。给每一个在 app/models/_classification.py 里登记：\n"
        "  identity / decision / past / projection —— "
        "如果你想登记的是「它现在是什么状态」，那说明它不该落盘。\n"
        + "\n".join(f"  - {item}" for item in missing)
    )


def test_the_registry_has_no_entries_for_columns_that_no_longer_exist() -> None:
    """登记表也不许留尸体 —— 删了列而登记还在，下一个人会以为它还在。"""
    stale: list[str] = []
    for table_name, columns in BY_TABLE.items():
        table = Base.metadata.tables.get(table_name)
        if table is None:
            stale.append(f"{table_name}（整表已删）")
            continue
        actual = {column.name for column in table.columns}
        stale.extend(f"{table_name}.{name}" for name in columns if name not in actual)
    assert not stale, f"登记表里这些条目已经没有对应的列了：{stale}"


def test_every_classification_is_one_of_the_four() -> None:
    for table_name, columns in BY_TABLE.items():
        for name, kind in columns.items():
            assert kind in VALID, f"{table_name}.{name} 的分类 {kind!r} 不在四类里"
    for name, kind in COMMON.items():
        assert kind in VALID, f"COMMON.{name} 的分类 {kind!r} 不在四类里"


# ── 禁令：判据不许读投影 ─────────────────────────────────────────────────────


#: 做判断的地方。这些模块里的代码决定"要不要做某件事"，读投影就是把判据
#: 建在一份会过期的转述上（2026-08-27 那次三矛盾并存就是这么来的）。
_JUDGMENT_MODULES = ("app/services/execution_view.py", "app/services/run_liveness.py")

#: 判据模块里点名禁止的投影列。写成 `<Model>.<column>` 的属性访问形式 ——
#: 这是 ORM 判据唯一的写法，不是"含这个词就算"。
_FORBIDDEN_IN_JUDGMENTS = {
    ("Run", "status"),
    ("Run", "retry_count"),
    ("RunAttempt", "status"),
}


@pytest.mark.parametrize("module_path", _JUDGMENT_MODULES)
def test_judgment_modules_do_not_read_projections(module_path: str) -> None:
    """现算三态的那两个模块，不许把判据建在投影列上。

    ⚠️ `run_liveness` 有一处例外并且**必须**有：`_status_value(run)` 要读
    `run.status` 才能把它翻译成现算结果 —— 那是投影的**入口**，不是判据。
    例外只此一处，靠"只允许出现在 _status_value 里"钉住。
    """
    root = pathlib.Path(__file__).resolve().parents[1]
    source = (root / module_path).read_text(encoding="utf-8")
    tree = ast.parse(source)

    allowed_functions = {"_status_value"}
    offenders: list[str] = []

    class Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.function: str | None = None

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
            previous, self.function = self.function, node.name
            self.generic_visit(node)
            self.function = previous

        visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

        def visit_Attribute(self, node: ast.Attribute) -> None:  # noqa: N802
            if isinstance(node.value, ast.Name):
                pair = (node.value.id, node.attr)
                normalized = (pair[0].lstrip("_").capitalize(), pair[1])
                for model, column in _FORBIDDEN_IN_JUDGMENTS:
                    if pair == (model, column) or normalized == (model, column):
                        if self.function not in allowed_functions:
                            offenders.append(
                                f"{module_path}:{node.lineno} {pair[0]}.{pair[1]}"
                                f"（在 {self.function}）"
                            )
            self.generic_visit(node)

    Visitor().visit(tree)
    assert not offenders, (
        "判据读了投影列 —— 那是一份会过期的转述，判据要问现算入口：\n"
        + "\n".join(f"  - {item}" for item in offenders)
    )

"""模型不许把自己钉死在 Postgres 上。

## 现场（2026-09-05，个人版第一步）

个人档的库是 SQLite —— 用户不该为了打开这个软件先装一个 Postgres。可
`project_configs` 有 5 列直接写了 `mapped_column(JSONB, ...)`，`JSONB` 是
`sqlalchemy.dialects.postgresql` 的类型，在 SQLite 上编译不出 DDL：建表那一刻
`CompileError`，服务起不来。

测试套件长期看不见这件事，因为 `conftest.py` 里挂了三张 `@compiles(..., "sqlite")`
垫片，把 JSONB / ARRAY / Vector 在 SQLite 上翻译成 JSON。垫片让**测试**能建表，
生产的 SQLite 却没有垫片 —— 于是"测试全绿"和"个人档起不来"同时成立。

修法是端正写法：`JSON().with_variant(JSONB(), "postgresql")`。Postgres 上仍是
JSONB，别处退化成 JSON。仓库里 artifact.py / feed.py / user.py 一直是这么写的，
project.py 是那个漏网的。垫片随裸类型一起删了。

## 判据

两条，缺一不可：
- **行为**：把所有模型建到一个真的 SQLite 库上，不许报错。这是最终判据。
- **结构**：扫源码，不许再出现裸的方言类型。行为判据只在"这一版模型"上成立，
  结构判据挡住下一个新列。默认拒绝 + 显式例外，不写名单。
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

import app.models  # noqa: F401 —— 让所有模型注册进 Base.metadata
from app.database import Base

MODELS_DIR = Path(app.models.__file__).parent

#: 只能出现在方言变体里的类型。裸着当列类型用 = 这张表只在 Postgres 上建得出来。
DIALECT_ONLY = {"JSONB", "ARRAY", "Vector", "INET", "MACADDR", "TSVECTOR", "HSTORE"}

#: 例外：允许裸用的位置。为空 —— 目前没有任何一列需要例外。加一条要写清理由，
#: 并说明这张表在 SQLite 上怎么办。
ALLOWED_BARE: dict[str, set[str]] = {}


def _bare_dialect_columns(source: str) -> list[tuple[int, str]]:
    """源码里所有「裸方言类型当列类型」的位置。

    认的是 `mapped_column(JSONB, ...)` 和 `mapped_column(ARRAY(String), ...)`
    这种把类型直接放在第一个位置实参上的写法；
    `JSON().with_variant(JSONB(), "postgresql")` 里的 JSONB 出现在 with_variant
    的实参上，不在这里，所以不会被误报。
    """
    found: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        name = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", "")
        if name not in {"mapped_column", "Column"} or not node.args:
            continue
        first = node.args[0]
        if isinstance(first, ast.Name) and first.id in DIALECT_ONLY:
            found.append((node.lineno, first.id))
        elif isinstance(first, ast.Call) and isinstance(first.func, ast.Name) \
                and first.func.id in DIALECT_ONLY:
            found.append((node.lineno, first.func.id))
    return found


@pytest.mark.parametrize("path", sorted(MODELS_DIR.glob("*.py")), ids=lambda p: p.name)
def test_no_model_column_is_postgres_only(path: Path) -> None:
    offenders = [
        (line, kind) for line, kind in _bare_dialect_columns(path.read_text(encoding="utf-8"))
        if kind not in ALLOWED_BARE.get(path.name, set())
    ]
    assert not offenders, (
        f"{path.name} 有裸的方言类型当列类型：{offenders}。\n"
        f'写成 `JSON().with_variant({offenders[0][1]}(), "postgresql")`（见本文件顶部）。\n'
        "裸着写 = 这张表在个人档的 SQLite 上建不出来 = 软件打不开。"
    )


@pytest.mark.asyncio
async def test_every_table_builds_on_sqlite(tmp_path: Path) -> None:
    """最终判据：所有模型能在一个真 SQLite 库上建出来，不靠任何测试垫片。"""
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'portable.db'}")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    finally:
        await engine.dispose()

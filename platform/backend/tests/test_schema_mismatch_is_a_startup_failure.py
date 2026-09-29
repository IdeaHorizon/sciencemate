"""库落后就不许起；迁移 id 不许长到记不下。

## 现场（2026-08-10）

我加了 `project_configs.autonomous_authorized_risk_classes` 的模型字段和迁移
`021`，**忘了在跑着的库上执行**。E2E 重启后第一条消息就炸，报的是：

    InFailedSQLTransactionError: current transaction is aborted,
    commands ignored until end of transaction block

完全看不出病因。真因藏在 postgres 日志里：

    ERROR: column project_configs.autonomous_authorized_risk_classes does not exist

一个不存在的列让整个事务中止，之后同事务里的每条查询都连带失败，冒出来的是
最后那条无辜的 SELECT。

**一个跑在自己没被写来适配的 schema 上的服务，产生的错误必然指不到病因。**

## 两个缺陷

1. 比对**本来就写好了** —— 在 `/readyz` 里，逻辑一字不差。但它长在一条没人
   走的路上：App Server 明知库落后也照常起。判据要前移到启动。
2. 我那个 revision id 叫 `021_autonomous_authorization_scope`，**34 个字符**，
   而 `alembic_version.version_num` 是 `varchar(32)`。DDL 跑完了、记版本号那
   一步炸在 `StringDataRightTruncationError`，整个迁移回滚 —— 于是"我明明写了
   迁移"和"库里没有这列"同时成立。
"""
from __future__ import annotations

import inspect
from pathlib import Path

from app import main as app_main

#: `alembic_version.version_num` 的列宽。Alembic 建表时写死的，不是我们能调的。
_VERSION_NUM_MAX = 32


def _versions_dir() -> Path:
    return Path(app_main.__file__).resolve().parents[1] / "alembic" / "versions"


def test_every_revision_id_fits_the_column() -> None:
    """扫盘，不写名单：新加的迁移自动被检查。

    超长的后果特别阴险 —— DDL 成功、记账失败、整体回滚，而报错在
    `UPDATE alembic_version` 上，完全不像"你的名字太长了"。
    """
    offenders = []
    for path in sorted(_versions_dir().glob("*.py")):
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("revision = "):
                value = stripped.split("=", 1)[1].strip().strip("\"'")
                if len(value) > _VERSION_NUM_MAX:
                    offenders.append((path.name, value, len(value)))
                break
    assert not offenders, (
        "这些 revision id 超过 varchar(32)，迁移会在记版本号那一步回滚：\n  "
        + "\n  ".join(f"{n}: {v!r} ({ln} 字符)" for n, v, ln in offenders)
    )


def test_startup_refuses_a_schema_it_was_not_written_for() -> None:
    source = inspect.getsource(app_main.lifespan)
    assert "_refuse_to_serve_a_schema_we_were_not_written_for" in source, (
        "比对必须在启动路径上 —— 只长在 /readyz 里等于没人走"
    )


# 三态（没有 alembic_version 表 ≠ 落后）、报错给解法、连不上库不算 schema 不匹配
# —— 这三条从前是 `inspect.getsource()` 的字符串比对。字符串比对只证明"那一行
# 文字还在"：实现从 Postgres-only 的 `to_regclass` 换成方言中立的 inspector 时，
# 行为完全正确，断言却红（2026-09-05 实测）。现在它们在
# test_personal_edition_runs_on_sqlite.py 里走真函数、真 SQLite 库。

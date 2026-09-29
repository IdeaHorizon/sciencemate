"""Readiness checks that must track the deployed migration graph.

这里**故意**写死当前 head。加一个迁移就得回来改这一行 —— 那正是它的作用：
schema 变更不许悄悄溜进部署，必须有人在 PR 里显式确认一次。

（这跟"护栏要扫盘、不要写名单"不冲突：名单式护栏的毛病是**新东西默认漏过**；
这里是新东西默认**变红**，方向相反。）

下半部分是相反的例子：`/health/ready` 要求哪些表存在，**不许**写死。写死的后
果 2026-08-13/14 每次部署都在日志里：迁移 022 把本服务的平行 KB/memory 领域
退役了，名单没跟着走，于是就绪端点长期 degraded，红的一直是同六张**故意不存
在**的表。一个长期红着的健康检查等于没有健康检查。
"""

import importlib
import inspect
import pkgutil

from app.database import Base
from app.main import _expected_alembic_heads, _tables_this_build_reads, readiness_check


def test_expected_alembic_heads_resolves_current_repository_head() -> None:
    assert _expected_alembic_heads() == {"051_project_two_states"}


def test_published_marine_rss_revision_remains_in_migration_graph() -> None:
    """A revision already stamped on node20 must never disappear from Git."""
    from pathlib import Path

    from app import main as app_main

    versions = Path(app_main.__file__).resolve().parents[1] / "alembic" / "versions"
    assert (versions / "033_marine_environment_rss.py").is_file()
    merge_source = (versions / "034_merge_rss_sandbox_heads.py").read_text(encoding="utf-8")
    assert '"033_marine_environment_rss"' in merge_source
    assert '"033_attempt_sandbox_manifest"' in merge_source


def test_required_tables_are_exactly_the_ones_this_build_has_models_for() -> None:
    """判据从模型现算 —— 退役的自动消失，新增的自动出现。

    这条断言的价值在它会怎么坏：谁把名单写回去，或者删了模型忘了改判据，两边
    立刻对不上。
    """
    import app.models

    for module in pkgutil.iter_modules(app.models.__path__):
        importlib.import_module(f"{app.models.__name__}.{module.name}")

    required = set(_tables_this_build_reads())
    assert required, "就绪检查一张表都不要求 = 这道防线没在防任何东西"
    assert required == set(Base.metadata.tables)


def test_readiness_does_not_require_the_tables_022_retired() -> None:
    """本 bug 的回归测试：六张被迁移删掉的表不许再出现在要求里。

    表名从迁移文件里读，不在这儿抄第二份 —— 抄件会各自演化。
    """
    retired = _retired_by_migration_022()
    assert retired, "读不到 022 的退役名单，这条测试就什么也没验"

    required = set(_tables_this_build_reads())
    assert not (required & retired), (
        "就绪检查要求了 022 故意删掉的表，/health/ready 会永远 degraded："
        f"{sorted(required & retired)}"
    )


def test_readiness_source_holds_no_hand_written_table_list() -> None:
    """机械挡住"下次再手抄一份"。"""
    source = inspect.getsource(readiness_check)
    assert "_tables_this_build_reads()" in source
    for retired in _retired_by_migration_022():
        assert retired not in source, f"{retired} 又被写回就绪检查里了"


def _retired_by_migration_022() -> set[str]:
    """从迁移 022 的源码里取它删掉的表名。

    `alembic/versions` 不是可导入的包（revision 文件以数字开头），所以按 AST
    读那个模块级的 `_TABLES`。
    """
    import ast
    from pathlib import Path

    from app import main as app_main

    path = (
        Path(app_main.__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "022_retire_platform_kb_domain.py"
    )
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "_TABLES" for t in node.targets
        ):
            return {
                element.value
                for element in node.value.elts  # type: ignore[attr-defined]
                if isinstance(element, ast.Constant)
            }
    return set()

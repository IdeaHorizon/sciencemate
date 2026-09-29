"""删掉十五张空脚手架表 —— 零生产写者，而读者还在照常答题。

## 判据

这批表全部通不过一条尺子：**盘上只许有过去（append-only 事件）和决定（人拍的
板）；关于"现在"的一切现算永不落盘；其余一切要么是投影（须挂声明+重建脚本+
禁令），要么就该删。**

删除前逐表核对了线上两个库的真实行数（2026-08-27）：

| 表 | 行数 | 判据 |
|---|---|---|
| conversations | 0 | 011 迁移已把全部行搬进 sessions/session_messages；创建端点自己 409 |
| watchlists | 0 | 全仓零引用；organization_id 类型写第一行就会在 PG 上炸 |
| tool_audit_logs | 0 | 形状正确但零写者。真审计在 harness transcript 与 git trailer 里 |
| consumption_records | 0 | BudgetManager 从未被实例化 |
| budgets | 84 / 6 | 每项目两条播种默认值，consumed 全 0、阈值全同：无一行被消费过 |
| nodes | 31 / 0 | 全部停在 status=planned，类型是已废弃的旧词表（planning/exploration/survey/analysis） |
| edges | 18 / 0 | 与 nodes 同期同命 |
| branches | 42 / 3 | 每项目恰好一条 main，零读取 |
| graph_snapshots | 0 | 零序列化、零反序列化，"时间旅行"契约从未实现 |
| dreaming_jobs / dreaming_runs | 0 | 唯一活引用是另一个模型里一条把空表当设计约束的注释 |
| reflection_results | 0 | 唯一写者在 ReflectionEngine 里，而它全仓只出现过一次：自己的 class 行 |
| evidence_chains / claims | 0 | 预定生产者是已退役的服务内 agent loop |
| artifact_relations | 0 | 零写零读零 UI；语义已由 .frozen.jsonl 的 amend 行与 provenance 承担 |

## 代价从来不是"多几张空表"

和 032 一样，代价是**读者对着空表持续给出看起来像答案的答案**：

- `GET /projects/{id}` 的 `active_node_count` 在数一张空表，永远是 0；
- convergence / reviews 两个端点读 `Node.execution_metadata["review_verdict"]`
  —— 一个全仓没有任何写者的键 —— 前端还为它们建了两个导航入口，于是用户
  点进去看到的是两个永远"暂无数据"的页面，而它们恰好被介绍为这一版主打的
  科学机制；
- `evidence_chains` 是 assets/summary 里最后一个还在读本地空表的计数，同一个
  函数里 KB / memory 早已改问 harness。

## 外键连坐

`artifacts.node_id` 与 `artifact_versions.created_by_node_id` 是指向 nodes 的
外键，两列都零写者，随表一起删。

Revision ID: 035_drop_empty_scaffolding
Revises: 034_merge_rss_sandbox_heads
"""

import sqlalchemy as sa
from alembic import op

revision = "035_drop_empty_scaffolding"
down_revision = "034_merge_rss_sandbox_heads"
branch_labels = None
depends_on = None


# 严格先子后父。`nodes` 与 `branches` 互指（nodes.branch_id ↔
# branches.fork_point_node_id），是一个真环 —— 先显式打断那条边，不用
# DROP ... CASCADE：CASCADE 会连带删掉它自己找到的一切，而"它找到了什么"
# 不出现在这份迁移里，等于把删除范围交给运行时决定。
_TABLES = [
    "artifact_relations",
    "claims",
    "evidence_chains",
    "reflection_results",
    "dreaming_runs",
    "dreaming_jobs",
    "graph_snapshots",
    "edges",
    "consumption_records",
    "budgets",
    "nodes",
    "branches",
    "tool_audit_logs",
    "watchlists",
    "conversations",
]


def _has(table: str) -> bool:
    bind = op.get_bind()
    return sa.inspect(bind).has_table(table)


def _drop_column_if_present(table: str, column: str) -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table(table):
        return
    if column not in {col["name"] for col in inspector.get_columns(table)}:
        return
    op.drop_column(table, column)


def upgrade() -> None:
    # 外键列先走，否则 PG 会拦住 nodes 的 DROP。
    _drop_column_if_present("artifacts", "node_id")
    _drop_column_if_present("artifact_versions", "created_by_node_id")
    if _has("branches"):
        op.execute(
            "ALTER TABLE branches DROP CONSTRAINT IF EXISTS fk_branches_fork_point_node"
        )
    for table in _TABLES:
        if _has(table):
            op.drop_table(table)


def downgrade() -> None:
    # 不重建。这十五张表没有一张持有别处没有的事实：产物的身份与版本在 git，
    # 运行时的死活靠租约现算，用量在 harness 的账本里，对话早已搬进 sessions。
    # 空表重建出来，只是把那些"对着空表答题"的读者再装回去一次。
    raise NotImplementedError(
        "these fifteen tables had zero production writers; "
        "nothing reads them and nothing is restorable from them"
    )

"""instructions are read live; the frozen copy is not a column

041 把三张指令表砍成会话行上的一列，但**只走了一半**：列建成 nullable、
没有 backfill，而消费方（`execute_local_turn`）缺了就硬 raise。node20 实测
99 个会话里 90 个命中 —— 041 之前建的会话从此一轮都发不出去，用户看到的
只有一句「这一轮没能完成」。

这一列买的是「会话开跑那一刻的指令不能事后被改」。那件事**本来就已经有
机制**：会话有自己的 git worktree 分支，它读到的 `PROJECT.md` 就是那条分支
上的那一份。抄一份进数据库，只是给同一个问题加了第二个真相源 —— 而两个
源分叉时（实测：界面写 `<data_root>/projects/<id>/PROJECT.md`，agent 读
worktree 里那份）两边都不报错。

所以列删掉，指令每轮从文件现读（RFC X3）。缺失不再是一种状态：没写过指令
的用户读出来是空字符串，全函数，没有一条路走得到 raise。

Revision ID: 044_no_instruction_snapshot
Revises: 043_revoked_access_tokens
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "044_no_instruction_snapshot"
down_revision = "043_revoked_access_tokens"
branch_labels = None
depends_on = None

_JSON = sa.JSON().with_variant(JSONB(), "postgresql")


def upgrade() -> None:
    with op.batch_alter_table("sessions") as batch:
        batch.drop_column("instruction_snapshot")


def downgrade() -> None:
    # 列回得来，内容回不来 —— 它本来就是文件的抄件，而那些文件还在。
    # ⚠️ 回退到 041..043 的代码之后，**没有 backfill 的老问题原样复活**：
    # 这一列对所有会话都是 NULL，`execute_local_turn` 会对每一条消息 raise。
    # 真要回退，先把 041 那半边一起处理掉。
    with op.batch_alter_table("sessions") as batch:
        batch.add_column(sa.Column("instruction_snapshot", _JSON, nullable=True))

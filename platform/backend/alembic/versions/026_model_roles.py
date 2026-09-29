"""模型角色：一条连接可以服务多个角色，默认是**按角色**的。

在此之前这张表只能回答一个问题："这个用户发消息用哪个后端"。于是节点内的
辅助模型（postprocess 审图 VLM）完全在体系之外：provider/model/base_url 写死
在节点里，key 只从一个写死的环境变量名读，UI 上既看不见也注册不了。

改成按**角色**（能力槽）指派：

  roles              这条连接被授权服务哪些角色
  default_for_roles  它是哪些角色的 scope 默认

`is_scope_default` 被 `default_for_roles` 取代而不是并存 —— 两个字段同时表达
"是不是默认"，就会有一天它们不一致，而两边都不报错。

`user_model_backend_preferences` 的主键从 (user_id) 变成 (user_id, role)：
用户可以给主模型选 A、给审图选 B。存量偏好回填成 reasoning。

回填：所有存量连接 = 只服务 reasoning（它们本来就只被当主模型用）。这不是
猜，是把既有事实如实写下来。

Revision ID: 026_model_roles
Revises: 025_research_feed
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "026_model_roles"
down_revision = "025_research_feed"
branch_labels = None
depends_on = None

#: 与 ORM 同一个类型（app/models/model_backend.py 的 _STRING_ARRAY）：PG 上
#: 原生数组，别的方言退 JSON。两边类型不一致，迁移建出来的列 ORM 读不对。
_STRING_ARRAY = sa.JSON().with_variant(postgresql.ARRAY(sa.String()), "postgresql")


def upgrade() -> None:
    op.add_column(
        "model_backend_configs",
        sa.Column("roles", _STRING_ARRAY, nullable=False, server_default="{}"),
    )
    op.add_column(
        "model_backend_configs",
        sa.Column("default_for_roles", _STRING_ARRAY, nullable=False, server_default="{}"),
    )
    # 存量事实：每条连接都只被当主模型用过。
    op.execute("UPDATE model_backend_configs SET roles = ARRAY['reasoning']")
    op.execute(
        "UPDATE model_backend_configs SET default_for_roles = ARRAY['reasoning'] "
        "WHERE is_scope_default"
    )
    op.drop_column("model_backend_configs", "is_scope_default")

    # 偏好按角色。老表一行一个用户，回填成 reasoning 那一行。
    op.add_column(
        "user_model_backend_preferences",
        sa.Column("role", sa.String(64), nullable=False, server_default="reasoning"),
    )
    op.drop_constraint(
        "user_model_backend_preferences_pkey",
        "user_model_backend_preferences",
        type_="primary",
    )
    op.create_primary_key(
        "user_model_backend_preferences_pkey",
        "user_model_backend_preferences",
        ["user_id", "role"],
    )
    op.alter_column("user_model_backend_preferences", "role", server_default=None)

    # 视觉能力是**能观测的**：给一张 1×1 图看它收不收，比按模型名去猜可靠。
    # 跟凭据健康分开两组字段 —— "key 被拒"和"这个模型不认图"是两件事，合成
    # 一个 last_probe_ok 会让状态词指错方向（凭据好好的却显示 credentials_rejected）。
    op.add_column(
        "model_backend_configs", sa.Column("last_vision_ok", sa.Boolean(), nullable=True)
    )
    op.add_column(
        "model_backend_configs", sa.Column("last_vision_detail", sa.String(500), nullable=True)
    )



def downgrade() -> None:
    op.drop_column("model_backend_configs", "last_vision_detail")
    op.drop_column("model_backend_configs", "last_vision_ok")
    op.add_column(
        "model_backend_configs",
        sa.Column("is_scope_default", sa.Boolean(), nullable=False, server_default="false"),
    )
    op.execute(
        "UPDATE model_backend_configs SET is_scope_default = true "
        "WHERE 'reasoning' = ANY(default_for_roles)"
    )
    op.alter_column("model_backend_configs", "is_scope_default", server_default=None)
    op.drop_column("model_backend_configs", "default_for_roles")
    op.drop_column("model_backend_configs", "roles")

    # 降级会丢掉非 reasoning 的偏好 —— 那些行在旧 schema 里无处安放。
    op.execute("DELETE FROM user_model_backend_preferences WHERE role <> 'reasoning'")
    op.drop_constraint(
        "user_model_backend_preferences_pkey",
        "user_model_backend_preferences",
        type_="primary",
    )
    op.create_primary_key(
        "user_model_backend_preferences_pkey", "user_model_backend_preferences", ["user_id"]
    )
    op.drop_column("user_model_backend_preferences", "role")

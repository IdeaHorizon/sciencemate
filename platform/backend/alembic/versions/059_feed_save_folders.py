"""User-owned feed save folders; existing saves remain in the default location."""

import sqlalchemy as sa

from alembic import op

revision = "059_feed_save_folders"
down_revision = "058_desktop_model_inheritance"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "feed_save_folders",
        sa.Column("id", sa.Uuid(as_uuid=False), primary_key=True),
        sa.Column(
            "user_id",
            sa.Uuid(as_uuid=False),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("user_id", "name", name="uq_feed_save_folders_user_name"),
    )
    op.create_index("ix_feed_save_folders_user_id", "feed_save_folders", ["user_id"])
    with op.batch_alter_table("feed_engagements") as batch:
        batch.add_column(sa.Column("folder_id", sa.Uuid(as_uuid=False), nullable=True))
        batch.create_foreign_key(
            "fk_feed_engagements_folder",
            "feed_save_folders",
            ["folder_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch.create_index("ix_feed_engagements_user_folder", ["user_id", "folder_id"])


def downgrade():
    with op.batch_alter_table("feed_engagements") as batch:
        batch.drop_index("ix_feed_engagements_user_folder")
        batch.drop_constraint("fk_feed_engagements_folder", type_="foreignkey")
        batch.drop_column("folder_id")
    op.drop_index("ix_feed_save_folders_user_id", table_name="feed_save_folders")
    op.drop_table("feed_save_folders")

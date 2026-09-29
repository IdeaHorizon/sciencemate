"""Separate Session message Command and Run identities.

Revision ID: 012_session_message_run_identity
Revises: 011_session_revision_foundation
"""

import sqlalchemy as sa

from alembic import op

revision = "012_session_message_run_identity"
down_revision = "011_session_revision_foundation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("session_messages", sa.Column("run_id", sa.String(128), nullable=True))
    op.create_index(
        "ix_session_messages_run_id", "session_messages", ["run_id"], unique=False
    )
    op.execute(
        r"""
        UPDATE session_messages
        SET run_id = command_id,
            command_id = NULL
        WHERE command_id LIKE 'run\_%' ESCAPE '\'
        """
    )


def downgrade() -> None:
    op.execute(
        r"""
        UPDATE session_messages
        SET command_id = run_id
        WHERE command_id IS NULL
          AND run_id LIKE 'run\_%' ESCAPE '\'
        """
    )
    op.drop_index("ix_session_messages_run_id", table_name="session_messages")
    op.drop_column("session_messages", "run_id")

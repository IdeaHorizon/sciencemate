"""Record when each account last rotated its password.

Changing a password used to leave every already-issued JWT valid until its own
expiry, so the one action a user takes after suspecting a leak did not end the
leaked session. ``users.password_changed_at`` is the cutoff: ``get_current_user``
refuses any token whose ``iat`` predates it.

Nullable, and deliberately not backfilled. NULL means "never rotated" — no
cutoff, no session disturbed. Stamping ``now()`` across the table here would
sign every existing user out the moment this migration ran.

Revision ID: 023_password_rotation_cutoff
Revises: 022_retire_platform_kb_domain
"""

import sqlalchemy as sa
from alembic import op

revision = "023_password_rotation_cutoff"
down_revision = "022_retire_platform_kb_domain"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("password_changed_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("users", "password_changed_at")

"""Drop the password-rotation cutoff — a token is bound to its credential now.

``users.password_changed_at`` existed to answer one question: "was this token
issued before the current password was set?"  It answered it by comparing two
wall-clock readings — the token's ``iat`` and this column.  Both come from the
same unreliable well.  A backward step of the server clock of a few tens of
milliseconds puts the cutoff *before* an older token's ``iat``, and the stolen
session the rotation was meant to end survives (#836 / #933).  That side cannot
be fixed with slack: a rotation that leaves a hole is not a rotation.

The question is now answered by identity instead of by order: the token carries
a fingerprint of the credential that signed it, and ``get_current_user``
compares it with the account's current one.  bcrypt salts every hash, so even
"change it to the same password" mints a new fingerprint.  Nothing reads this
column any more, so it goes.

Every token issued before this deploy lacks the ``cred`` claim and is refused —
everyone signs in again once.  That is the fail-closed direction, and it is the
same thing a key rotation would do.

Revision ID: 045_token_bound_to_credential
Revises: 044_no_instruction_snapshot
"""

from alembic import op
import sqlalchemy as sa

revision = "045_token_bound_to_credential"
down_revision = "044_no_instruction_snapshot"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("users", "password_changed_at")


def downgrade() -> None:
    op.add_column(
        "users",
        sa.Column("password_changed_at", sa.DateTime(timezone=True), nullable=True),
    )

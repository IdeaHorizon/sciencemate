"""Add canonical Research Sessions, memberships, and revision staging.

Revision ID: 011_session_revision_foundation
Revises: 010_local_multiuser_runtime
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision = "011_session_revision_foundation"
down_revision = "010_local_multiuser_runtime"
branch_labels = None
depends_on = None

EMPTY_MANIFEST_HASH = "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a"


def upgrade() -> None:
    op.add_column("projects", sa.Column("current_revision_id", UUID(as_uuid=False)))
    op.create_index(
        "ix_projects_current_revision_id", "projects", ["current_revision_id"], unique=False
    )

    op.add_column(
        "sessions",
        sa.Column(
            "title",
            sa.String(300),
            nullable=False,
            server_default="Recovered research session",
        ),
    )
    op.add_column("sessions", sa.Column("summary", sa.Text(), nullable=True))
    op.add_column("sessions", sa.Column("created_by_user_id", sa.String(64), nullable=True))
    op.add_column("sessions", sa.Column("primary_driver_user_id", sa.String(64), nullable=True))
    op.add_column(
        "sessions", sa.Column("driver_lease_until", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "sessions",
        sa.Column(
            "lifecycle_status", sa.String(24), nullable=False, server_default="active"
        ),
    )
    op.add_column("sessions", sa.Column("base_revision_id", UUID(as_uuid=False), nullable=True))
    op.add_column("sessions", sa.Column("policy_snapshot_id", sa.String(128), nullable=True))
    op.add_column("sessions", sa.Column("model_backend_id", UUID(as_uuid=False), nullable=True))
    op.add_column("sessions", sa.Column("knowledge_read_watermark", JSONB, nullable=True))
    op.add_column(
        "sessions",
        sa.Column("next_message_sequence", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("sessions", sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index(
        "ix_sessions_created_by_user_id", "sessions", ["created_by_user_id"], unique=False
    )
    op.create_index(
        "ix_sessions_primary_driver_user_id",
        "sessions",
        ["primary_driver_user_id"],
        unique=False,
    )
    op.create_index(
        "ix_sessions_base_revision_id", "sessions", ["base_revision_id"], unique=False
    )
    op.create_check_constraint(
        "ck_sessions_lifecycle_status",
        "sessions",
        "lifecycle_status IN ('active', 'completed', 'archived')",
    )
    op.create_check_constraint(
        "ck_sessions_message_sequence_nonnegative", "sessions", "next_message_sequence >= 0"
    )

    op.execute(
        """
        UPDATE sessions
        SET created_by_user_id = initiating_user_id,
            lifecycle_status = 'archived',
            archived_at = COALESCE(archived_at, updated_at),
            title = 'Recovered execution session'
        """
    )

    op.create_table(
        "session_messages",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "session_id",
            sa.String(64),
            sa.ForeignKey("sessions.session_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("actor_user_id", sa.String(64), nullable=True),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("command_id", sa.String(64), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("session_id", "sequence", name="uq_session_messages_sequence"),
        sa.UniqueConstraint("command_id", "role", name="uq_session_messages_command_role"),
        sa.CheckConstraint("sequence >= 1", name="ck_session_messages_sequence_positive"),
        sa.CheckConstraint(
            "role IN ('user', 'assistant', 'system')", name="ck_session_messages_role"
        ),
    )
    op.create_index(
        "ix_session_messages_session_id", "session_messages", ["session_id"], unique=False
    )
    op.create_index(
        "ix_session_messages_actor_user_id",
        "session_messages",
        ["actor_user_id"],
        unique=False,
    )
    op.create_index(
        "ix_session_messages_session_sequence",
        "session_messages",
        ["session_id", "sequence"],
        unique=False,
    )

    op.create_table(
        "project_memberships",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column(
            "project_id",
            UUID(as_uuid=False),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            UUID(as_uuid=False),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("role", sa.String(24), nullable=False),
        sa.Column(
            "created_by_user_id",
            UUID(as_uuid=False),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "updated_by_user_id",
            UUID(as_uuid=False),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("removed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "role IN ('lead', 'researcher', 'reviewer', 'viewer')",
            name="ck_project_memberships_role",
        ),
    )
    op.create_index(
        "uq_project_memberships_project_user",
        "project_memberships",
        ["project_id", "user_id"],
        unique=True,
    )
    op.create_index(
        "ix_project_memberships_user_active",
        "project_memberships",
        ["user_id", "removed_at"],
        unique=False,
    )
    op.execute(
        """
        INSERT INTO project_memberships (
            id, project_id, user_id, role, created_by_user_id, updated_by_user_id
        )
        SELECT p.id, p.id, p.owner_id, 'lead', p.owner_id, p.owner_id
        FROM projects p
        ON CONFLICT (project_id, user_id) DO UPDATE
        SET role = 'lead', removed_at = NULL, updated_by_user_id = EXCLUDED.updated_by_user_id
        """
    )

    op.create_table(
        "project_revisions",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column(
            "project_id",
            UUID(as_uuid=False),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("revision_no", sa.Integer(), nullable=False),
        sa.Column("parent_revision_ids", JSONB, nullable=False, server_default="[]"),
        sa.Column(
            "source_session_id",
            sa.String(64),
            sa.ForeignKey("sessions.session_id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("source_run_id", sa.String(128), nullable=True),
        sa.Column(
            "created_by_user_id",
            UUID(as_uuid=False),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("message", sa.String(500), nullable=False),
        sa.Column("manifest", JSONB, nullable=False, server_default="{}"),
        sa.Column("manifest_hash", sa.String(64), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("project_id", "revision_no", name="uq_project_revisions_number"),
        sa.CheckConstraint("revision_no >= 0", name="ck_project_revisions_number_nonnegative"),
    )
    op.create_index(
        "ix_project_revisions_project_created",
        "project_revisions",
        ["project_id", sa.text("created_at DESC")],
        unique=False,
    )
    op.create_foreign_key(
        "fk_projects_current_revision",
        "projects",
        "project_revisions",
        ["current_revision_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_sessions_base_revision",
        "sessions",
        "project_revisions",
        ["base_revision_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.execute(
        f"""
        INSERT INTO project_revisions (
            id, project_id, revision_no, parent_revision_ids, created_by_user_id,
            message, manifest, manifest_hash
        )
        SELECT p.id, p.id, 0, '[]'::jsonb, p.owner_id,
               'Initial project state', '{{}}'::jsonb, '{EMPTY_MANIFEST_HASH}'
        FROM projects p
        ON CONFLICT (project_id, revision_no) DO NOTHING
        """
    )
    op.execute(
        """
        UPDATE projects p
        SET current_revision_id = r.id
        FROM project_revisions r
        WHERE r.project_id = p.id AND r.revision_no = 0 AND p.current_revision_id IS NULL
        """
    )
    op.execute(
        """
        UPDATE sessions s
        SET base_revision_id = p.current_revision_id
        FROM projects p
        WHERE s.project_id = p.id::text AND s.base_revision_id IS NULL
        """
    )

    _migrate_conversations()

    op.create_table(
        "change_sets",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column(
            "project_id",
            UUID(as_uuid=False),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "session_id",
            sa.String(64),
            sa.ForeignKey("sessions.session_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "base_revision_id",
            UUID(as_uuid=False),
            sa.ForeignKey("project_revisions.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("status", sa.String(24), nullable=False, server_default="open"),
        sa.Column(
            "published_revision_id",
            UUID(as_uuid=False),
            sa.ForeignKey("project_revisions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_by_user_id",
            UUID(as_uuid=False),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "status IN ('open', 'publishing', 'published', 'conflicted', 'abandoned')",
            name="ck_change_sets_status",
        ),
    )
    op.create_index(
        "ix_change_sets_session_status", "change_sets", ["session_id", "status"], unique=False
    )
    op.create_index(
        "ix_change_sets_project_created",
        "change_sets",
        ["project_id", sa.text("created_at DESC")],
        unique=False,
    )
    op.create_index(
        "uq_change_sets_one_active_session",
        "change_sets",
        ["session_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('open', 'publishing', 'conflicted')"),
    )

    op.add_column("artifact_versions", sa.Column("resource_key", sa.String(500), nullable=True))
    op.add_column("artifact_versions", sa.Column("session_id", sa.String(64), nullable=True))
    op.add_column("artifact_versions", sa.Column("change_set_id", UUID(as_uuid=False)))
    op.add_column(
        "artifact_versions",
        sa.Column(
            "lifecycle_status", sa.String(24), nullable=False, server_default="published"
        ),
    )
    op.add_column("artifact_versions", sa.Column("content", sa.Text(), nullable=True))
    op.add_column(
        "artifact_versions", sa.Column("created_by_user_id", UUID(as_uuid=False), nullable=True)
    )
    op.create_foreign_key(
        "fk_artifact_versions_session",
        "artifact_versions",
        "sessions",
        ["session_id"],
        ["session_id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_artifact_versions_change_set",
        "artifact_versions",
        "change_sets",
        ["change_set_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_artifact_versions_created_by",
        "artifact_versions",
        "users",
        ["created_by_user_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_check_constraint(
        "ck_artifact_versions_lifecycle_status",
        "artifact_versions",
        "lifecycle_status IN ('candidate', 'published', 'frozen')",
    )
    op.create_index(
        "ix_artifact_versions_resource_key", "artifact_versions", ["resource_key"], unique=False
    )
    op.create_index(
        "ix_artifact_versions_change_set",
        "artifact_versions",
        ["change_set_id", "lifecycle_status"],
        unique=False,
    )

    op.create_table(
        "change_items",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column(
            "change_set_id",
            UUID(as_uuid=False),
            sa.ForeignKey("change_sets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("resource_type", sa.String(32), nullable=False),
        sa.Column("resource_key", sa.String(500), nullable=False),
        sa.Column("operation", sa.String(16), nullable=False),
        sa.Column(
            "base_version_id",
            UUID(as_uuid=False),
            sa.ForeignKey("artifact_versions.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column(
            "proposed_version_id",
            UUID(as_uuid=False),
            sa.ForeignKey("artifact_versions.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.CheckConstraint(
            "resource_type IN ('artifact', 'project_doc', 'project_config')",
            name="ck_change_items_resource_type",
        ),
        sa.CheckConstraint(
            "operation IN ('create', 'update', 'delete')", name="ck_change_items_operation"
        ),
        sa.UniqueConstraint("change_set_id", "resource_key", name="uq_change_items_resource_key"),
    )
    op.create_index(
        "ix_change_items_resource", "change_items", ["resource_type", "resource_key"]
    )

    op.create_table(
        "merge_conflicts",
        sa.Column("id", UUID(as_uuid=False), primary_key=True),
        sa.Column(
            "change_set_id",
            UUID(as_uuid=False),
            sa.ForeignKey("change_sets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("resource_type", sa.String(32), nullable=False),
        sa.Column("resource_key", sa.String(500), nullable=False),
        sa.Column("base_version_id", UUID(as_uuid=False), nullable=True),
        sa.Column("project_version_id", UUID(as_uuid=False), nullable=True),
        sa.Column("proposed_version_id", UUID(as_uuid=False), nullable=True),
        sa.Column("status", sa.String(24), nullable=False, server_default="open"),
        sa.Column("resolution", JSONB, nullable=True),
        sa.Column(
            "resolved_by_user_id",
            UUID(as_uuid=False),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("status IN ('open', 'resolved')", name="ck_merge_conflicts_status"),
        sa.UniqueConstraint(
            "change_set_id", "resource_key", name="uq_merge_conflicts_resource_key"
        ),
    )
    op.create_index(
        "ix_merge_conflicts_change_status",
        "merge_conflicts",
        ["change_set_id", "status"],
    )


def _migrate_conversations() -> None:
    op.execute(
        """
        INSERT INTO sessions (
            tenant_id, workspace_id, project_id, session_id, initiating_user_id,
            title, summary, created_by_user_id, lifecycle_status, base_revision_id,
            next_message_sequence, next_event_sequence, created_at, updated_at, archived_at
        )
        SELECT
            'local-tenant',
            COALESCE(u.group_id, u.institution_id, 'local'),
            COALESCE(c.project_id::text, 'global-' || left(c.user_id::text, 8)),
            c.id::text,
            c.user_id::text,
            c.title,
            c.summary,
            c.user_id::text,
            CASE WHEN c.project_id IS NULL THEN 'archived' ELSE 'active' END,
            p.current_revision_id,
            jsonb_array_length(COALESCE(c.messages, '[]'::jsonb)),
            0,
            c.created_at,
            c.updated_at,
            CASE WHEN c.project_id IS NULL THEN c.updated_at ELSE NULL END
        FROM conversations c
        JOIN users u ON u.id = c.user_id
        LEFT JOIN projects p ON p.id = c.project_id
        ON CONFLICT (session_id) DO UPDATE SET
            title = EXCLUDED.title,
            summary = EXCLUDED.summary,
            created_by_user_id = COALESCE(sessions.created_by_user_id, EXCLUDED.created_by_user_id),
            lifecycle_status = EXCLUDED.lifecycle_status,
            base_revision_id = COALESCE(sessions.base_revision_id, EXCLUDED.base_revision_id),
            archived_at = EXCLUDED.archived_at,
            next_message_sequence = GREATEST(
                sessions.next_message_sequence, EXCLUDED.next_message_sequence
            )
        """
    )
    op.execute(
        """
        INSERT INTO session_messages (
            id, session_id, sequence, actor_user_id, role, content, command_id, created_at
        )
        SELECT
            md5(c.id::text || ':message:' || item.ordinality::text),
            c.id::text,
            item.ordinality::integer,
            CASE WHEN item.value->>'role' = 'user' THEN c.user_id::text ELSE NULL END,
            CASE
                WHEN item.value->>'role' IN ('user', 'assistant', 'system')
                THEN item.value->>'role'
                ELSE 'system'
            END,
            COALESCE(item.value->>'content', ''),
            'legacy:' || c.id::text || ':' || item.ordinality::text,
            c.created_at + ((item.ordinality - 1) * interval '1 microsecond')
        FROM conversations c
        CROSS JOIN LATERAL jsonb_array_elements(COALESCE(c.messages, '[]'::jsonb))
            WITH ORDINALITY AS item(value, ordinality)
        ON CONFLICT (id) DO NOTHING
        """
    )


def downgrade() -> None:
    op.drop_index("ix_merge_conflicts_change_status", table_name="merge_conflicts")
    op.drop_table("merge_conflicts")
    op.drop_index("ix_change_items_resource", table_name="change_items")
    op.drop_table("change_items")
    op.drop_index("ix_artifact_versions_change_set", table_name="artifact_versions")
    op.drop_index("ix_artifact_versions_resource_key", table_name="artifact_versions")
    op.drop_constraint(
        "ck_artifact_versions_lifecycle_status", "artifact_versions", type_="check"
    )
    op.drop_constraint(
        "fk_artifact_versions_created_by", "artifact_versions", type_="foreignkey"
    )
    op.drop_constraint(
        "fk_artifact_versions_change_set", "artifact_versions", type_="foreignkey"
    )
    op.drop_constraint("fk_artifact_versions_session", "artifact_versions", type_="foreignkey")
    for column in (
        "created_by_user_id",
        "content",
        "lifecycle_status",
        "change_set_id",
        "session_id",
        "resource_key",
    ):
        op.drop_column("artifact_versions", column)
    op.drop_index("uq_change_sets_one_active_session", table_name="change_sets")
    op.drop_index("ix_change_sets_project_created", table_name="change_sets")
    op.drop_index("ix_change_sets_session_status", table_name="change_sets")
    op.drop_table("change_sets")
    op.drop_constraint("fk_sessions_base_revision", "sessions", type_="foreignkey")
    op.drop_constraint("fk_projects_current_revision", "projects", type_="foreignkey")
    op.drop_index("ix_project_revisions_project_created", table_name="project_revisions")
    op.drop_table("project_revisions")
    op.drop_index("ix_project_memberships_user_active", table_name="project_memberships")
    op.drop_index("uq_project_memberships_project_user", table_name="project_memberships")
    op.drop_table("project_memberships")
    op.drop_index("ix_session_messages_session_sequence", table_name="session_messages")
    op.drop_index("ix_session_messages_actor_user_id", table_name="session_messages")
    op.drop_index("ix_session_messages_session_id", table_name="session_messages")
    op.drop_table("session_messages")
    op.drop_constraint("ck_sessions_message_sequence_nonnegative", "sessions", type_="check")
    op.drop_constraint("ck_sessions_lifecycle_status", "sessions", type_="check")
    op.drop_index("ix_sessions_base_revision_id", table_name="sessions")
    op.drop_index("ix_sessions_primary_driver_user_id", table_name="sessions")
    op.drop_index("ix_sessions_created_by_user_id", table_name="sessions")
    for column in (
        "archived_at",
        "next_message_sequence",
        "knowledge_read_watermark",
        "model_backend_id",
        "policy_snapshot_id",
        "base_revision_id",
        "lifecycle_status",
        "driver_lease_until",
        "primary_driver_user_id",
        "created_by_user_id",
        "summary",
        "title",
    ):
        op.drop_column("sessions", column)
    op.drop_index("ix_projects_current_revision_id", table_name="projects")
    op.drop_column("projects", "current_revision_id")

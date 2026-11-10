"""CNKI result schema upgrade shared by Alembic and personal SQLite startup."""
import sqlalchemy as sa


def upgrade_incremental_results(operations) -> bool:
    """Keep existing result timestamps; SQLite needs a staged add/backfill/rebuild.

    A callable Python default cannot populate old SQLite rows. CURRENT_TIMESTAMP
    cannot be added directly to a populated table either. This is the same data
    migration for the server and the desktop, including interrupted upgrades.
    """
    inspector = sa.inspect(operations.get_bind())
    if "cnki_papers" not in inspector.get_table_names():
        return False
    columns = {column["name"]: column for column in inspector.get_columns("cnki_papers")}
    column = columns.get("updated_at")
    indexed = any(index["name"] == "ix_cnki_papers_updates"
                  for index in inspector.get_indexes("cnki_papers"))
    needs_alter = column is None or column["nullable"] or column["default"] is None
    if not needs_alter and indexed:
        return False
    if column is None:
        operations.add_column("cnki_papers", sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=True))
    # Existing timestamps belong to their original observations, not upgrade time.
    operations.execute(sa.text(
        "UPDATE cnki_papers SET updated_at = observed_at WHERE updated_at IS NULL"))
    with operations.batch_alter_table("cnki_papers") as batch:
        if needs_alter:
            batch.alter_column("updated_at", existing_type=sa.DateTime(timezone=True),
                               nullable=False, server_default=sa.func.current_timestamp())
        if not indexed:
            batch.create_index("ix_cnki_papers_updates", ["user_id", "updated_at", "id"])
    return True


def upgrade_personal_results(connection) -> bool:
    """The desktop has no Alembic revision table, but uses the same operations."""
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    return upgrade_incremental_results(Operations(MigrationContext.configure(connection)))

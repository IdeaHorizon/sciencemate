"""Migrate every legacy Project to the Git-native repository layout."""

from __future__ import annotations

import argparse
import asyncio
from uuid import UUID

from sqlalchemy import select, text

from app.database import get_session_factory
from app.models.project import Project
from app.models.user import User
from app.services.project_governance import commit_project_governance
from app.services.project_migration import migrate_legacy_project_content

EXPECTED_SCHEMA_VERSION = "018_git_project_repository"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Migrate legacy database-owned Projects into Git repositories."
    )
    parser.add_argument(
        "--project-id",
        type=UUID,
        help="Migrate only this Project UUID (default: every Project).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List selected Projects after schema validation without writing anything.",
    )
    return parser.parse_args()


async def main(*, project_id: UUID | None = None, dry_run: bool = False) -> None:
    factory = get_session_factory()
    async with factory() as db:
        version = (
            await db.execute(text("SELECT version_num FROM alembic_version LIMIT 1"))
        ).scalar_one_or_none()
        if version != EXPECTED_SCHEMA_VERSION:
            raise SystemExit(
                "Database schema is not ready for Git migration: "
                f"expected Alembic {EXPECTED_SCHEMA_VERSION}, got {version or 'missing'}. "
                "Run `alembic upgrade head` first."
            )
        query = select(Project).order_by(Project.created_at)
        if project_id:
            query = query.where(Project.id == project_id)
        projects = list((await db.execute(query)).scalars())
        if dry_run:
            print(f"{len(projects)} Project(s) selected; no changes written.")
            for project in projects:
                print(f"{project.id}: {project.name}")
            return
        for project in projects:
            owner = await db.get(User, project.owner_id)
            if not owner:
                raise RuntimeError(f"Project {project.id} has no owner")
            await commit_project_governance(
                db,
                project=project,
                user=owner,
                message="migration: project governance snapshot",
            )
            revision = await migrate_legacy_project_content(
                db, project=project, actor_id=owner.id
            )
            await db.commit()
            print(
                f"{project.id}: "
                f"{revision.git_commit_sha if revision else 'already migrated'}"
            )


if __name__ == "__main__":
    args = parse_args()
    asyncio.run(main(project_id=args.project_id, dry_run=args.dry_run))

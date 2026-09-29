"""PostgreSQL-only integration coverage for project creation serialization."""

from __future__ import annotations

import os
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.auth import get_current_user, hash_password
from app.database import get_db
from app.main import app
from app.models.user import User


@pytest.mark.asyncio
async def test_postgres_project_create_returns_fully_loaded_response() -> None:
    database_url = os.environ.get("TEST_POSTGRES_DATABASE_URL")
    if not database_url:
        pytest.skip("set TEST_POSTGRES_DATABASE_URL to run PostgreSQL integration tests")

    engine = create_async_engine(database_url, pool_pre_ping=True)
    connection = await engine.connect()
    transaction = await connection.begin()
    db = AsyncSession(bind=connection, expire_on_commit=False)
    suffix = uuid4().hex
    user = User(
        email=f"project-creator-{suffix}@example.test",
        hashed_password=hash_password("unused-in-dependency-override"),
        display_name="Project Creator",
        role="researcher",
        institution_id="institution-test",
        institution_name="Test Institution",
        group_id="group-test",
        group_name="Test Group",
    )
    db.add(user)
    await db.flush()

    async def override_get_db():
        yield db

    async def override_current_user() -> User:
        return user

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_current_user
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://postgres-integration"
        ) as client:
            response = await client.post(
                "/api/v1/projects/",
                json={
                    "name": "Async project response regression",
                    "description": (
                        "Verify server-generated timestamps are loaded before serialization."
                    ),
                    "research_domain": "software reliability",
                },
            )

        assert response.status_code == 201
        payload = response.json()
        assert payload["name"] == "Async project response regression"
        assert payload["member_count"] == 1
        assert payload["active_session_count"] == 0
        assert payload["created_at"]
        assert payload["updated_at"]
        assert "drive" in payload["capabilities"]
    finally:
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_current_user, None)
        await db.close()
        if transaction.is_active:
            await transaction.rollback()
        await connection.close()
        await engine.dispose()

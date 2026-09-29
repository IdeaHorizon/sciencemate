# ── 第二份版本账那几条测试（2026-09-05 随 RFC X1 删）─────────────────────────
#
# artifact-candidates / receipts / 资源级自动变基 / CAS 冲突 / viewer 不能 stage：
# 它们验的是 change_sets + change_items + merge_conflicts + project_revisions 这套
# 机制本身。那套机制没了 —— 改动就是 git diff，冲突就是两边动了同一个文件，
# 发布就是把会话路径回放到 main。同样的问题现在由 test_git_is_the_only_ledger.py
# 回答，判据落在 git 上。


"""End-to-end contract tests for the local multi-user runtime."""

import asyncio
import json
import sys
import textwrap
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from uuid import uuid4
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles

from app.auth import hash_password
from app.config import settings
from app.database import Base, _pool_options, get_db
from app.main import app
from app.models.artifact import Artifact, ArtifactVersion
from app.models.execution import (
    REQUIRES_LIVE_RUNTIME_STATUSES,
    EXECUTION_TABLES,
    Command,
    Decision,
    ExecutionEvent,
    Run,
    RunAttempt,
    SessionMessage,
    SessionProjection,
)
from app.models.model_backend import ModelBackendConfig, UserModelBackendPreference
from app.models.project import OperationMode, Project, ProjectConfig, ProjectMembership
from app.models.invitation import Invitation
from app.models.research_settings import UserResearchSettings
from app.models.resource import ProjectResource
from app.models.user import User
from tests._authentication_tables import AUTHENTICATION_TABLES
from app.services import local_execution
from app.services.harness_sessions import (
    AppRunBinding,
    HarnessSessionError,
    HarnessSessionManager,
    HarnessSessionProcessError,
    HarnessSessionStaleError,
    assert_conversation_runtime_available,
    harness_session_manager,
    mark_orphaned_harness_runs,
)
from app.services.local_execution import (
    _pause_status,
    _validate_resume_binding,
)
from app.services.model_backends import encrypt_api_key
from app.services.run_liveness import runtime_lost
from app.services.project_repository import run_in_repository_thread

PASSWORD = "AtriumDemo2026!"
INSTITUTION = "atrium-university"
GROUP = "computational-science"


def test_pause_status_distinguishes_human_input_from_permission() -> None:
    assert _pause_status({"question": "Choose scope"}) == "waiting_human"
    assert _pause_status({"metadata": {"type": "highrisk_confirm"}}) == "waiting_permission"


async def _initialize_manager_worktree(*, user: User, project_id: str, session_id: str) -> Path:
    from app.services.project_repository import get_project_repository, run_in_repository_thread

    repository = get_project_repository()
    project = await run_in_repository_thread(
        repository.initialize_project,
        project_id=project_id,
        name="Manager boundary test",
        description=None,
        research_domain=None,
        owner_id=user.id,
    )
    workspace = await run_in_repository_thread(
        repository.ensure_session_workspace,
        project_id=project_id,
        session_id=session_id,
        base_commit=project.head_commit,
        title="Manager boundary test",
        created_by=user.id,
    )
    return Path(workspace.path)


@compiles(JSONB, "sqlite")
def _compile_jsonb_for_sqlite(_type, _compiler, **_kwargs) -> str:
    """Keep the production JSONB mapping while exercising ProjectConfig locally."""
    return "JSON"


RUNTIME_TABLES = [
    *AUTHENTICATION_TABLES,
    Invitation.__table__,
    UserResearchSettings.__table__,
    Project.__table__,
    ProjectConfig.__table__,
    ProjectResource.__table__,
    ProjectMembership.__table__,
    ModelBackendConfig.__table__,
    UserModelBackendPreference.__table__,
    *EXECUTION_TABLES,
    SessionMessage.__table__,
    Artifact.__table__,
    ArtifactVersion.__table__,
]


@pytest_asyncio.fixture
async def runtime_client(tmp_path_factory):
    # 库的形状照个人档来：一个文件、按产品的方言给池（`get_engine` 同一套）。
    # 于是每个会话有**自己的**连接、自己的事务，和产品一样。
    #
    # 从前这里是内存库 + `StaticPool`：所有会话接在同一条 sqlite3 连接上，也就
    # 共用一个事务 —— 任何一个会话关掉时的 ROLLBACK，撤的是当时正在别的会话里
    # 进行的那一半。脱离请求的执行协程、测试自己的轮询、`_autoname` 同时在写，
    # 于是 CI 上间歇撞 `execution_events` 的序号唯一约束（见
    # test_a_reader_closing_between_allocation_and_insert_does_not_reissue_the_number）。
    # 那是夹具造出来的并发，产品里没有；而产品里真有的并发，它又测不到。
    url = f"sqlite+aiosqlite:///{tmp_path_factory.mktemp('runtime-db') / 'runtime.sqlite'}"
    engine = create_async_engine(url, **_pool_options(url))
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: Base.metadata.create_all(sync_connection, tables=RUNTIME_TABLES)
        )
        # Project detail reports legacy graph counts.  This runtime fixture does
        # not exercise the legacy graph and intentionally omits its JSONB-heavy
        # model set, so provide only the read projection required by that route.
        await connection.exec_driver_sql(
            "CREATE TABLE nodes ("
            "id TEXT PRIMARY KEY, project_id TEXT NOT NULL, status TEXT NOT NULL"
            ")"
        )
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async with factory() as db:
        users = {}
        for email, name, role in (
            ("institution.admin@atrium.local", "Institution Admin", "institution_admin"),
            ("colleague@atrium.local", "Colleague", "researcher"),
            ("researcher@atrium.local", "Researcher", "researcher"),
            ("project.member@atrium.local", "Project Member", "researcher"),
            ("project.viewer@atrium.local", "Project Viewer", "researcher"),
        ):
            user = User(
                email=email,
                hashed_password=hash_password(PASSWORD),
                display_name=name,
                role=role,
                institution_id=INSTITUTION,
                institution_name="Atrium University",
                group_id=GROUP,
                group_name="Computational Science Lab",
            )
            db.add(user)
            users[email] = user
        outsider = User(
            email="outsider@other.local",
            hashed_password=hash_password(PASSWORD),
            display_name="Outsider",
            role="researcher",
            institution_id="other-institution",
            institution_name="Other Institution",
            group_id="other-group",
            group_name="Other Group",
        )
        db.add(outsider)
        await db.flush()

        projects = []
        for email, name in (
            ("institution.admin@atrium.local", "Institution Project"),
            ("colleague@atrium.local", "Colleague Project"),
            ("researcher@atrium.local", "Researcher Project"),
        ):
            project = Project(owner_id=users[email].id, name=name)
            db.add(project)
            projects.append((project, users[email]))
        outsider_project = Project(owner_id=outsider.id, name="Outsider Project")
        db.add(outsider_project)
        projects.append((outsider_project, outsider))
        await db.flush()
        for project, owner in projects:
            db.add(
                ProjectMembership(
                    project_id=project.id,
                    user_id=owner.id,
                    role="lead",
                    created_by_user_id=owner.id,
                    updated_by_user_id=owner.id,
                )
            )
            db.add(ProjectConfig(project_id=project.id))
            await db.flush()
        db.add(
            ModelBackendConfig(
                scope_kind="institution",
                scope_id=INSTITUTION,
                provider="demo",
                display_name="Demo",
                model="atrium-demo-v1",
                credential_source="none",
                roles=["reasoning"],
                default_for_roles=["reasoning"],
                created_by_user_id=users["institution.admin@atrium.local"].id,
            )
        )
        db.add(
            ModelBackendConfig(
                scope_kind="institution",
                scope_id=INSTITUTION,
                provider="anthropic",
                display_name="Anthropic",
                model="claude-sonnet-4-6",
                credential_source="environment",
                created_by_user_id=users["institution.admin@atrium.local"].id,
            )
        )
        await db.commit()

    async def override_get_db():
        async with factory() as db:
            try:
                yield db
                await db.commit()
            except Exception:
                await db.rollback()
                raise

    app.dependency_overrides[get_db] = override_get_db
    # 覆盖 `get_db` 只管到请求处理器。脱离请求的写者（事实摄取、自动命名、
    # 替人点推荐项…）拿的是**模块级**工厂，它指着真 DATABASE_URL —— 不指过来，
    # 那些写者在测试里要么连不上、要么连上开发机上真的那个库，两种都等于
    # 不在测试的视野里。
    import app.database as database

    saved_engine, saved_factory = database._engine, database._session_factory
    database._engine = engine
    database._session_factory = factory
    transport = ASGITransport(app=app)
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client, factory
    finally:
        database._engine, database._session_factory = saved_engine, saved_factory
        app.dependency_overrides.clear()
        await engine.dispose()


async def _token(client: AsyncClient, email: str) -> str:
    """这个人的一张 token —— 直接签，不走登录端点。

    登录那扇门（`/auth/login`）是专业版的（`app/pro/api/auth.py`），公开树里没有它；
    而这里的测试题目是项目、会话、运行，不是登录。签 token 的原语（`create_access_token`）
    是核心的账号基础设施，两棵树都有。登录端点自己的判据在 tests/pro 里。
    """
    from sqlalchemy import select

    from app import database
    from app.auth import create_access_token

    async with database._session_factory() as db:
        user = await db.scalar(select(User).where(User.email == email))
    assert user is not None, f"没有 {email} 这个用户 —— runtime_client 没种它"
    return create_access_token(user)


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_two_roles_and_project_scope_are_mechanical(runtime_client) -> None:
    """组织两档身份（`UserRole`）。「我的项目」（`GET /projects/`）人人只有自己建的和参与的 ——
    管理员也一样：他看得见全组织的项目，但那在组织页的项目 tab 里（RFC_ORGANISATION_PAGE §3.3，
    从前管理员的侧栏被同事的项目填满）。"""
    client, _factory = runtime_client
    expected = {
        "institution.admin@atrium.local": ("institution_admin", 1, "projects.read_all"),
        "colleague@atrium.local": ("researcher", 1, "projects.read_own"),
        "researcher@atrium.local": ("researcher", 1, "projects.read_own"),
    }
    for email, (role, project_count, permission) in expected.items():
        token = await _token(client, email)
        me = await client.get("/api/v1/auth/me", headers=_headers(token))
        projects = await client.get("/api/v1/projects/", headers=_headers(token))
        assert me.status_code == 200
        assert me.json()["role"] == role
        assert permission in me.json()["permissions"]
        assert len(projects.json()) == project_count
    assert (await client.get("/api/v1/projects/")).status_code == 401


# 指令层的权限 / CAS / 会话快照那条测试随三张指令表一起走了（2026-09-05，RFC X2）。
# 它验的是「起草—发布—版本」那套流程；指令现在是文件，版本是 git，冻结是会话行
# 上的一列。新的判据在 test_instructions_are_files.py 里，验的是同一件事的现在
# 这个形状：文件是源、冻结不可变、harness 收到的仍是三层文本。


@pytest.mark.asyncio
async def test_interface_settings_defaults_save_and_preserve_other_preferences(
    runtime_client,
) -> None:
    client, factory = runtime_client
    researcher = await _token(client, "researcher@atrium.local")
    outsider = await _token(client, "outsider@other.local")

    defaults = {
        "theme": "system",
        "density": "comfortable",
        "font_scale": 100,
        "reduce_motion": False,
        # 默认中文：资讯流是按中文使用者做的（wangd 2026-08-23）。
        "language": "zh",
        # 打开平台先看今天这个领域发生了什么，而不是先看一块白板
        # （wangd 2026-08-22 拍板）。
        "default_landing": "feed",
        "show_run_usage": True,
        "execution_detail": "standard",
        "auto_collapse_completed_tools": True,
        "auto_collapse_completed_steps": True,
        "follow_active_run": True,
        "onboarding_done": False,
        "project_guide_done": False,
    }
    initial = await client.get("/api/v1/settings/interface", headers=_headers(researcher))
    assert initial.status_code == 200
    assert initial.json() == defaults

    async with factory() as db:
        user = await db.scalar(select(User).where(User.email == "researcher@atrium.local"))
        assert user is not None
        user.preferences = {"research": {"keep": "untouched"}}
        await db.commit()

    replacement = {
        "theme": "dark",
        "density": "compact",
        "font_scale": 110,
        "reduce_motion": True,
        "language": "en",
        "default_landing": "projects",
        "show_run_usage": False,
        "execution_detail": "trace",
        "auto_collapse_completed_tools": False,
        "auto_collapse_completed_steps": False,
        "follow_active_run": False,
        # 开场那个标记也走同一条存取路：存进去、读回来，一趟不少。
        "onboarding_done": True,
        "project_guide_done": True,
    }
    saved = await client.put(
        "/api/v1/settings/interface",
        headers=_headers(researcher),
        json=replacement,
    )
    assert saved.status_code == 200, saved.text
    assert saved.json() == replacement
    assert (
        await client.get("/api/v1/settings/interface", headers=_headers(researcher))
    ).json() == replacement
    assert (
        await client.get("/api/v1/settings/interface", headers=_headers(outsider))
    ).json() == defaults
    assert (
        await client.put(
            "/api/v1/settings/interface",
            headers=_headers(researcher),
            json={**replacement, "unknown": True},
        )
    ).status_code == 422

    async with factory() as db:
        user = await db.scalar(select(User).where(User.email == "researcher@atrium.local"))
        assert user is not None
        assert user.preferences == {
            "research": {"keep": "untouched"},
            "interface": replacement,
        }


@pytest.mark.asyncio
async def test_notification_preferences_are_in_app_only_persistent_and_isolated(
    runtime_client,
) -> None:
    client, factory = runtime_client
    researcher = await _token(client, "researcher@atrium.local")
    outsider = await _token(client, "outsider@other.local")
    defaults = {
        "decision_required": True,
        "run_failed": True,
        "run_completed": True,
        "budget_warning": True,
        "delivery_capabilities": ["in_app"],
    }
    initial = await client.get("/api/v1/settings/notifications", headers=_headers(researcher))
    assert initial.status_code == 200
    assert initial.json() == defaults

    replacement = {
        "decision_required": True,
        "run_failed": True,
        "run_completed": False,
        "budget_warning": False,
    }
    saved = await client.put(
        "/api/v1/settings/notifications",
        headers=_headers(researcher),
        json=replacement,
    )
    assert saved.status_code == 200, saved.text
    assert saved.json() == {**replacement, "delivery_capabilities": ["in_app"]}
    assert (
        await client.get("/api/v1/settings/notifications", headers=_headers(outsider))
    ).json() == defaults
    assert (
        await client.put(
            "/api/v1/settings/notifications",
            headers=_headers(researcher),
            json={**replacement, "email": True},
        )
    ).status_code == 422

    async with factory() as db:
        user = await db.scalar(select(User).where(User.email == "researcher@atrium.local"))
        assert user is not None
        assert user.preferences["notifications"] == replacement


@pytest.mark.asyncio
async def test_usage_is_aggregated_only_from_sessions_owned_by_current_user(
    runtime_client,
) -> None:
    client, factory = runtime_client
    researcher_token = await _token(client, "researcher@atrium.local")
    today = datetime.now(UTC).date()

    async with factory() as db:
        researcher = await db.scalar(select(User).where(User.email == "researcher@atrium.local"))
        other = await db.scalar(select(User).where(User.email == "colleague@atrium.local"))
        project = await db.scalar(select(Project).where(Project.owner_id == researcher.id))
        assert researcher is not None and other is not None and project is not None

        owned_both = SessionProjection(
            tenant_id=settings.runtime_tenant_id,
            workspace_id=GROUP,
            project_id=str(project.id),
            session_id="usage-owned-both",
            initiating_user_id=researcher.id,
            git_base_commit_sha="0" * 40,
            created_by_user_id=researcher.id,
            title="Owned by both fields",
        )
        owned_initiated = SessionProjection(
            tenant_id=settings.runtime_tenant_id,
            workspace_id=GROUP,
            project_id=str(project.id),
            session_id="usage-owned-initiated",
            initiating_user_id=researcher.id,
            git_base_commit_sha="0" * 40,
            created_by_user_id=other.id,
            title="Owned by initiating user",
        )
        other_session = SessionProjection(
            tenant_id=settings.runtime_tenant_id,
            workspace_id=GROUP,
            project_id=str(project.id),
            session_id="usage-other-user",
            initiating_user_id=other.id,
            git_base_commit_sha="0" * 40,
            created_by_user_id=other.id,
            title="Must stay excluded",
        )
        db.add_all([owned_both, owned_initiated, other_session])
        await db.flush()

        def usage_run(
            run_id: str,
            session_id: str,
            days_ago: int,
            total_tokens: int,
            *,
            cost: Decimal | None = None,
            currency: str | None = None,
            retry_count: int = 1,
        ) -> Run:
            return Run(
                id=run_id,
                tenant_id=settings.runtime_tenant_id,
                workspace_id=GROUP,
                project_id=str(project.id),
                session_id=session_id,
                status="completed",
                prompt_tokens=max(0, total_tokens - 1),
                completion_tokens=1,
                total_tokens=total_tokens,
                cost=cost,
                cost_currency=currency,
                usage_coverage="complete" if cost is not None else "partial",
                retry_count=retry_count,
                created_at=datetime.now(UTC) - timedelta(days=days_ago),
            )

        db.add_all(
            [
                usage_run(
                    "usage-run-today",
                    "usage-owned-both",
                    0,
                    10,
                    cost=Decimal("1.25"),
                    currency="USD",
                ),
                usage_run("usage-run-yesterday", "usage-owned-both", 1, 20),
                usage_run(
                    "usage-run-day-3",
                    "usage-owned-both",
                    3,
                    30,
                    cost=Decimal("2.00"),
                    currency="EUR",
                ),
                usage_run("usage-run-day-8", "usage-owned-both", 8, 40),
                usage_run("usage-run-day-9", "usage-owned-both", 9, 50),
                usage_run("usage-run-day-10", "usage-owned-both", 10, 60),
                usage_run("usage-run-day-366", "usage-owned-both", 366, 70),
                usage_run(
                    "usage-run-second-session",
                    "usage-owned-initiated",
                    0,
                    5,
                    cost=Decimal("0.00"),
                    currency="USD",
                ),
                usage_run(
                    "usage-run-other-user",
                    "usage-other-user",
                    0,
                    9999,
                    cost=Decimal("99.00"),
                    currency="USD",
                    retry_count=99,
                ),
            ]
        )
        await db.commit()

    response = await client.get("/api/v1/settings/usage", headers=_headers(researcher_token))
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["session_count"] == 2
    assert payload["run_count"] == 8
    assert payload["prompt_tokens"] == 277
    assert payload["completion_tokens"] == 8
    assert payload["total_tokens"] == 285
    assert payload["retry_count"] == 8
    assert payload["known_cost"] == 3.25
    assert payload["cost_currency"] is None
    assert payload["cost_known_runs"] == 3
    assert payload["cost_unknown_runs"] == 5
    assert payload["active_days"] == 7
    assert payload["current_streak"] == 2
    assert payload["longest_streak"] == 3
    assert len(payload["daily"]) == 6
    today_row = next(row for row in payload["daily"] if row["date"] == today.isoformat())
    assert today_row == {"date": today.isoformat(), "total_tokens": 15, "run_count": 2}
    assert 9999 not in {row["total_tokens"] for row in payload["daily"]}


@pytest.mark.asyncio
async def test_project_resources_are_scoped_capability_gated_and_secret_safe(
    runtime_client,
) -> None:
    client, factory = runtime_client
    researcher = await _token(client, "researcher@atrium.local")
    governor = await _token(client, "institution.admin@atrium.local")
    outsider = await _token(client, "outsider@other.local")
    project_id = (await client.get("/api/v1/projects/", headers=_headers(researcher))).json()[0][
        "id"
    ]
    secret_reference = "vault://research-platform/projects/demo/database"
    create_payloads = [
        {
            "resource_type": "storage",
            "name": "Project workspace",
            "provider": "project_workspace",
            "workspace_binding": "default-workspace",
            "config": {"retention": "project_lifetime"},
        },
        {
            "resource_type": "dataset",
            "name": "Screening corpus",
            "provider": "object_storage",
            "endpoint": "s3://research-data/corpus",
            "config": {"format": "parquet", "read_only": True},
        },
        {
            "resource_type": "database",
            "name": "Evidence database",
            "provider": "postgresql",
            "endpoint": "postgresql://database.internal/research",
            "config": {"schema": "evidence"},
            "secret_ref": secret_reference,
        },
        {
            "resource_type": "compute",
            "name": "Local execution",
            "provider": "local_process",
            "config": {"scheduler": "none"},
        },
    ]
    created = []
    for payload in create_payloads:
        response = await client.post(
            f"/api/v1/projects/{project_id}/resources",
            headers=_headers(researcher),
            json=payload,
        )
        assert response.status_code == 201, response.text
        item = response.json()
        assert item["health_status"] == "unknown"
        assert "secret_ref" not in item
        assert secret_reference not in response.text
        created.append(item)
    assert created[2]["has_secret_reference"] is True
    assert all(item["project_id"] == project_id for item in created)
    repository_status = await client.get(
        f"/api/v1/projects/{project_id}/repository",
        headers=_headers(researcher),
    )
    assert repository_status.status_code == 200
    assert repository_status.json()["authority"] == "git"
    assert repository_status.json()["defaultBranch"] == "main"
    assert len(repository_status.json()["headCommitSha"]) == 40
    assert "path" not in repository_status.json()
    from app.services.project_repository import get_project_repository

    repository = get_project_repository()
    refs = (await run_in_repository_thread(repository.read_file_at_revision, project_id, "resources/secrets.refs.yaml"))
    registry = (await run_in_repository_thread(repository.read_file_at_revision, project_id, "resources/registry.yaml"))
    assert secret_reference in refs
    assert "user:password" not in refs + registry
    assert "apiKey" not in refs + registry

    visible_to_governor = await client.get(
        f"/api/v1/projects/{project_id}/resources",
        headers=_headers(governor),
    )
    assert visible_to_governor.status_code == 200
    assert len(visible_to_governor.json()) == 4
    assert secret_reference not in visible_to_governor.text
    denied_mutation = await client.patch(
        f"/api/v1/projects/{project_id}/resources/{created[0]['id']}",
        headers=_headers(governor),
        json={"name": "Governor must not mutate"},
    )
    assert denied_mutation.status_code == 403
    assert (
        await client.get(f"/api/v1/projects/{project_id}/resources", headers=_headers(outsider))
    ).status_code == 403

    filtered = await client.get(
        f"/api/v1/projects/{project_id}/resources?resource_type=database",
        headers=_headers(researcher),
    )
    assert [item["resource_type"] for item in filtered.json()] == ["database"]
    async with factory() as db:
        stored_secret_ref = await db.get(ProjectResource, created[2]["id"])
        assert stored_secret_ref is not None
        assert stored_secret_ref.secret_ref == secret_reference
    updated = await client.patch(
        f"/api/v1/projects/{project_id}/resources/{created[2]['id']}",
        headers=_headers(researcher),
        json={
            "name": "Evidence database v2",
            "endpoint": "postgresql://database.internal/research_v2",
            "secret_ref": None,
        },
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["has_secret_reference"] is False
    assert updated.json()["name"] == "Evidence database v2"

    duplicate = await client.post(
        f"/api/v1/projects/{project_id}/resources",
        headers=_headers(researcher),
        json=create_payloads[0],
    )
    assert duplicate.status_code == 409
    for invalid_payload in (
        {
            "resource_type": "database",
            "name": "Embedded credential",
            "provider": "postgresql",
            "endpoint": "postgresql://user:password@database.internal/research",
        },
        {
            "resource_type": "storage",
            "name": "Host path",
            "provider": "local",
            "endpoint": "/Users/researcher/private/project",
        },
        {
            "resource_type": "dataset",
            "name": "Secret config",
            "provider": "http",
            "config": {"apiKey": "must-not-be-stored"},
        },
        {
            "resource_type": "dataset",
            "name": "Self granted host mount",
            "provider": "local",
            "config": {"sandbox_mount": {"path": "/srv/private", "mode": "ro"}},
        },
    ):
        invalid = await client.post(
            f"/api/v1/projects/{project_id}/resources",
            headers=_headers(researcher),
            json=invalid_payload,
        )
        assert invalid.status_code == 422, invalid.text

    disabled = await client.delete(
        f"/api/v1/projects/{project_id}/resources/{created[0]['id']}",
        headers=_headers(researcher),
    )
    assert disabled.status_code == 204
    enabled_list = await client.get(
        f"/api/v1/projects/{project_id}/resources",
        headers=_headers(researcher),
    )
    assert created[0]["id"] not in {item["id"] for item in enabled_list.json()}
    all_resources = await client.get(
        f"/api/v1/projects/{project_id}/resources?include_disabled=true",
        headers=_headers(researcher),
    )
    disabled_item = next(item for item in all_resources.json() if item["id"] == created[0]["id"])
    assert disabled_item["is_enabled"] is False
    assert disabled_item["disabled_at"] is not None

    async with factory() as db:
        resources = list(
            (
                await db.scalars(
                    select(ProjectResource).where(ProjectResource.project_id == project_id)
                )
            ).all()
        )
        assert len(resources) == 4
        assert {item.tenant_id for item in resources} == {settings.runtime_tenant_id}
        database = next(item for item in resources if item.resource_type == "database")
        assert database.secret_ref is None
        assert all("/Users/" not in json.dumps(item.config) for item in resources)


@pytest.mark.asyncio
async def test_local_compute_inventory_is_observed_and_marks_unknowns(
    runtime_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.services import compute_inventory

    monkeypatch.setattr(
        compute_inventory,
        "_gpu_inventory",
        lambda: {"status": "unknown", "count": None, "devices": []},
    )
    client, _factory = runtime_client
    researcher = await _token(client, "researcher@atrium.local")
    assert (await client.get("/api/v1/compute/inventory")).status_code == 401

    response = await client.get("/api/v1/compute/inventory", headers=_headers(researcher))
    assert response.status_code == 200
    inventory = response.json()
    assert inventory["scope"] == "local_development"
    assert inventory["health"]["status"] == "online"
    assert inventory["nodes"][0]["status"] == "online"
    assert inventory["nodes"][0]["gpu"] == {
        "status": "unknown",
        "count": None,
        "devices": [],
    }
    assert inventory["capacity"]["gpu_count"] is None
    assert inventory["capacity"]["status"] == "unknown"
    assert inventory["schedulers"] == [
        {
            "id": "local-process",
            "kind": "in_process",
            "status": "online",
            "supports_queue": False,
            "queue_depth": None,
            "active_sessions": 0,
        }
    ]
    assert inventory["recent_jobs"] == {"supported": False, "items": []}
    serialized = json.dumps(inventory).lower()
    assert "hourly" not in serialized
    assert all("cost" not in item for item in inventory)
    assert str(Path.cwd()) not in response.text

    monkeypatch.setattr(
        compute_inventory.shutil,
        "disk_usage",
        lambda _path: (_ for _ in ()).throw(OSError("definitive probe failure")),
    )
    failed_probe = await client.get("/api/v1/compute/inventory", headers=_headers(researcher))
    assert failed_probe.status_code == 200
    failed = failed_probe.json()
    assert failed["nodes"][0]["storage"]["status"] == "offline"
    assert failed["capacity"]["status"] == "offline"


def test_nvidia_inventory_reports_observed_memory_and_utilization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import compute_inventory

    monkeypatch.setattr(compute_inventory.shutil, "which", lambda _name: "/usr/bin/nvidia-smi")

    class Result:
        stdout = (
            "0, NVIDIA A100-SXM4-80GB, 81920, 25871, 4\n"
            "1, NVIDIA A100-SXM4-80GB, 81920, 81145, 0\n"
        )

    monkeypatch.setattr(compute_inventory.subprocess, "run", lambda *args, **kwargs: Result())
    gpu = compute_inventory._gpu_inventory()

    assert gpu["status"] == "online"
    assert gpu["count"] == 2
    assert gpu["devices"][0] == {
        "id": "gpu-0",
        "name": "NVIDIA A100-SXM4-80GB",
        "memory_total_bytes": 81920 * 1024 * 1024,
        "memory_available_bytes": 25871 * 1024 * 1024,
        "utilization_percent": 4,
    }


def test_memory_inventory_reports_observed_memory() -> None:
    """内存清单必须**报出真值**，不是恒 unknown。旧写法用 os.sysconf，Windows 没有它就
    静默 unknown；现在经 shared.lib.hostinfo（psutil）跨平台读得到。这条钉住"真用上了
    hostinfo"——把它改回恒 unknown（或不调 hostinfo）就红。"""
    from app.services import compute_inventory

    memory = compute_inventory._memory_inventory()
    assert memory["status"] == "online", memory
    assert memory["total_bytes"] and memory["total_bytes"] > 0
    assert 0 <= memory["available_bytes"] <= memory["total_bytes"]


@pytest.mark.asyncio
async def test_personal_research_settings_are_persistent_and_user_isolated(
    runtime_client,
) -> None:
    client, factory = runtime_client
    researcher = await _token(client, "researcher@atrium.local")
    outsider = await _token(client, "outsider@other.local")

    default_response = await client.get("/api/v1/settings/research", headers=_headers(researcher))
    assert default_response.status_code == 200
    assert default_response.json() == {
        "response_language": "auto",
        "citation_style": "author_year",
        "evidence_standard": "balanced",
        "memory_enabled": True,
        "instructions": [],
        "effective_layers": [
            {
                "kind": "institution",
                "name": "Atrium University",
                "editable": False,
                "instruction_count": 0,
                "summary": "No published institution behavior instructions are active.",
            },
            {
                "kind": "group",
                "name": "Computational Science Lab",
                "editable": False,
                "instruction_count": 0,
                "summary": "No published research-group behavior instructions are active.",
            },
            {
                "kind": "personal",
                "name": "Researcher",
                "editable": True,
                "instruction_count": 0,
                "summary": "0 of 0 personal instructions enabled.",
            },
        ],
        "updated_at": None,
    }

    private_instruction = "PRIVATE-INSTRUCTION-ALPHA: separate observations from inference."
    replacement = {
        "response_language": "zh-CN",
        "citation_style": "numeric",
        "evidence_standard": "strict",
        "memory_enabled": False,
        "instructions": [
            {
                "id": "observed-vs-inferred",
                "title": "Evidence boundary",
                "scope": "all",
                "instruction": private_instruction,
                "enabled": True,
            },
            {
                "id": "disabled-draft-rule",
                "title": "Disabled draft rule",
                "scope": "writing",
                "instruction": "Do not inject this disabled instruction.",
                "enabled": False,
            },
        ],
    }
    updated = await client.put(
        "/api/v1/settings/research",
        headers=_headers(researcher),
        json=replacement,
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["instructions"] == replacement["instructions"]
    assert updated.json()["memory_enabled"] is False
    assert updated.json()["updated_at"] is not None
    assert updated.json()["effective_layers"][-1]["instruction_count"] == 2
    project_id = (await client.get("/api/v1/projects/", headers=_headers(researcher))).json()[0][
        "id"
    ]
    session_response = await client.post(
        f"/api/v1/projects/{project_id}/sessions",
        headers=_headers(researcher),
        json={"title": "Personal memory disabled"},
    )
    assert session_response.status_code == 201, session_response.text
    async with factory() as db:
        session = await db.get(SessionProjection, session_response.json()["id"])
        assert session is not None
        assert session.research_settings_snapshot["memory_enabled"] is False
        assert session.research_settings_snapshot["instructions"] == []

    outsider_default = await client.get("/api/v1/settings/research", headers=_headers(outsider))
    assert outsider_default.status_code == 200
    assert outsider_default.json()["instructions"] == []
    assert private_instruction not in outsider_default.text
    outsider_update = await client.put(
        "/api/v1/settings/research",
        headers=_headers(outsider),
        json={
            "response_language": "en",
            "citation_style": "apa",
            "evidence_standard": "exploratory",
            "memory_enabled": True,
            "instructions": [
                {
                    "title": "Independent setting",
                    "scope": "review",
                    "instruction": "OUTSIDER-ONLY-INSTRUCTION",
                    "enabled": True,
                }
            ],
        },
    )
    assert outsider_update.status_code == 200
    researcher_again = await client.get("/api/v1/settings/research", headers=_headers(researcher))
    assert researcher_again.json()["instructions"] == replacement["instructions"]
    assert "OUTSIDER-ONLY-INSTRUCTION" not in researcher_again.text

    delete_by_replace = await client.put(
        "/api/v1/settings/research",
        headers=_headers(researcher),
        json={**replacement, "instructions": replacement["instructions"][:1]},
    )
    assert delete_by_replace.status_code == 200
    assert [item["id"] for item in delete_by_replace.json()["instructions"]] == [
        "observed-vs-inferred"
    ]
    invalid = await client.put(
        "/api/v1/settings/research",
        headers=_headers(researcher),
        json={
            **replacement,
            "instructions": [replacement["instructions"][0]] * 2,
        },
    )
    assert invalid.status_code == 422

    async with factory() as db:
        records = list((await db.scalars(select(UserResearchSettings))).all())
        assert len(records) == 2
        assert {record.tenant_id for record in records} == {settings.runtime_tenant_id}
        assert len({record.user_id for record in records}) == 2


@pytest.mark.asyncio
async def test_resume_binding_cannot_reopen_a_terminal_durable_run(runtime_client) -> None:
    client, factory = runtime_client
    lead = await _token(client, "researcher@atrium.local")
    lead_user = (await client.get("/api/v1/auth/me", headers=_headers(lead))).json()
    project_id = (await client.get("/api/v1/projects/", headers=_headers(lead))).json()[0]["id"]
    created_session = await client.post(
        f"/api/v1/projects/{project_id}/sessions",
        headers=_headers(lead),
        json={"title": "Resume boundary"},
    )
    session_id = created_session.json()["id"]
    binding = AppRunBinding(lead_user["id"], session_id, "run_terminal_pause", session_id)
    async with factory() as db:
        session = await db.scalar(
            select(SessionProjection).where(SessionProjection.session_id == session_id)
        )
        assert session is not None
        run = Run(
            id=binding.run_id,
            tenant_id=session.tenant_id,
            workspace_id=session.workspace_id,
            project_id=session.project_id,
            session_id=session.session_id,
            status="completed",
            summary={"executionKernel": "formal_harness"},
        )
        attempt = RunAttempt(
            tenant_id=session.tenant_id,
            workspace_id=session.workspace_id,
            project_id=session.project_id,
            session_id=session.session_id,
            run_id=run.id,
            attempt_no=1,
            status="completed",
        )
        db.add_all([run, attempt])
        await db.commit()

        # COMPLETED 是人的定案：拒绝续跑的文案要指向真相（是"已定案"，
        # 不是"找不到 Run"）——报错指假因会把排查引去错的地方。
        with pytest.raises(HarnessSessionStaleError, match="already closed"):
            await _validate_resume_binding(
                db,
                binding=binding,
                user_id=lead_user["id"],
                project_id=project_id,
                conversation_id=session_id,
            )
        await db.refresh(run)
        await db.refresh(attempt)
        assert run.status == "completed"
        assert attempt.status == "completed"
        assert run.status != "stale_unknown"


@pytest.mark.asyncio
async def test_model_backend_secret_is_write_only_and_scope_protected(runtime_client) -> None:
    client, factory = runtime_client
    admin = await _token(client, "institution.admin@atrium.local")
    researcher = await _token(client, "researcher@atrium.local")
    secret = "test-secret-never-return"
    created = await client.post(
        "/api/v1/settings/model-backends",
        headers=_headers(admin),
        json={
            "provider": "openai_compatible",
            "display_name": "Test backend",
            "model": "test-model",
            "base_url": "http://127.0.0.1:9999/v1",
            "api_key": secret,
        },
    )
    assert created.status_code == 201
    assert created.json()["has_api_key"] is True
    assert "api_key" not in created.json()
    backend_id = created.json()["id"]

    visible = await client.get("/api/v1/settings/model-backends", headers=_headers(admin))
    assert secret not in visible.text
    forbidden = await client.put(
        f"/api/v1/settings/model-backends/{backend_id}",
        headers=_headers(researcher),
        json={"display_name": "tampered"},
    )
    assert forbidden.status_code in {403, 404}
    async with factory() as db:
        stored = await db.get(ModelBackendConfig, backend_id)
        assert stored is not None
        assert stored.encrypted_api_key and stored.encrypted_api_key != secret


@pytest.mark.asyncio
async def test_run_and_harness_events_are_visible_before_model_turn_finishes(
    runtime_client, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    client, factory = runtime_client
    monkeypatch.setattr(settings, "harness_bridge_enabled", True)
    monkeypatch.setattr(settings, "harness_state_root", str(tmp_path))
    researcher = await _token(client, "researcher@atrium.local")
    created_backend = await client.post(
        "/api/v1/settings/model-backends",
        headers=_headers(researcher),
        json={
            "provider": "openai_compatible",
            "display_name": "Durability test",
            "model": "test-model",
            "base_url": "http://provider.invalid/v1",
            "api_key": "durability-test-key",
        },
    )
    await client.post(
        f"/api/v1/settings/model-backends/{created_backend.json()['id']}/default",
        headers=_headers(researcher),
    )
    project = (await client.get("/api/v1/projects/", headers=_headers(researcher))).json()[0]
    conversation = await client.post(
        f"/api/v1/projects/{project['id']}/sessions",
        headers=_headers(researcher),
        json={"title": "Durable in flight"},
    )
    conversation_id = conversation.json()["id"]
    transcript = tmp_path / "durable" / "transcript.jsonl"
    transcript.parent.mkdir(parents=True)
    native = {
        "event": "llm_response",
        "at": datetime.now(UTC).isoformat(),
        "usage": {"prompt_tokens": 9, "completion_tokens": 4, "total_tokens": 13},
    }
    encoded = (json.dumps(native) + "\n").encode()
    from app.services.project_repository import get_project_repository

    repository = get_project_repository()
    workspace = (await run_in_repository_thread(repository.session_status, project["id"], conversation_id))
    relative = "literature/scripts/collect.py"
    script = Path(workspace.path) / relative
    script.parent.mkdir(parents=True)
    script.write_text("print('durable')\n", encoding="utf-8")
    checkpoint_native = {
        "event": "workspace_checkpoint_requested",
        "at": datetime.now(UTC).isoformat(),
        "node_type": "literature",
        "run_id": "harness-durable-run",
        "run_status": "completed",
        "workspace_prefix": "literature",
        "paths": [relative],
        "files_changed": 1,
        "additions": 1,
        "deletions": 0,
    }
    checkpoint_encoded = (json.dumps(checkpoint_native) + "\n").encode()
    transcript.write_bytes(encoded + checkpoint_encoded)
    observations: dict[str, object] = {}

    async def fake_harness_turn(**kwargs):
        async with factory() as observer:
            run = await observer.get(Run, kwargs["run_id"])
            attempt = await observer.scalar(
                select(RunAttempt).where(RunAttempt.run_id == kwargs["run_id"])
            )
            observations["run_status_during_model"] = run.status if run else None
            observations["attempt_status_during_model"] = attempt.status if attempt else None
            observations["sandbox_manifest_committed_before_model"] = bool(
                attempt
                and attempt.sandbox_manifest
                and attempt.sandbox_manifest_hash
                and attempt.sandbox_manifest.get("attempt_id") == attempt.id
                and attempt.sandbox_manifest.get("run_id") == run.id
                and attempt.sandbox_manifest.get("version") == 4
                and bool(attempt.sandbox_manifest.get("backend"))
                and len(attempt.sandbox_manifest_hash) == 64
                and attempt.lease_until is not None
            )
            dispatched_attempt = kwargs.get("sandbox_attempt")
            observations["dispatch_received_frozen_attempt"] = bool(
                attempt
                and dispatched_attempt
                and dispatched_attempt.id == attempt.id
                and dispatched_attempt.sandbox_manifest_hash
                == attempt.sandbox_manifest_hash
            )
            messages = list(
                (
                    await observer.scalars(
                        select(SessionMessage)
                        .where(SessionMessage.session_id == conversation_id)
                        .order_by(SessionMessage.sequence)
                    )
                ).all()
            )
            observations["messages_during_model"] = [
                (item.role, item.content, item.run_id) for item in messages
            ]
        await kwargs["on_protocol_event"](
            {"type": "token_delta", "request_id": "turn-token-test", "text": "Durable "}
        )
        await kwargs["on_protocol_event"](
            {
                "type": "token_delta",
                "request_id": "turn-token-test",
                "text": "execution completed.",
            }
        )
        await kwargs["on_protocol_event"](
            {
                "type": "transcript",
                "transcript_path": str(transcript),
                "byte_start": 0,
                "byte_end": len(encoded),
                "event": native,
            }
        )
        await kwargs["on_protocol_event"](
            {
                "type": "transcript",
                "transcript_path": str(transcript),
                "byte_start": len(encoded),
                "byte_end": len(encoded) + len(checkpoint_encoded),
                "event": checkpoint_native,
            }
        )
        async with factory() as observer:
            observations["usage_events_during_model"] = await observer.scalar(
                select(func.count())
                .select_from(ExecutionEvent)
                .where(
                    ExecutionEvent.run_id == kwargs["run_id"],
                    ExecutionEvent.kind == "usage.updated",
                )
            )
        return {
            "status": "completed",
            "run_id": "harness-durable-run",
            "final_text": "Durable execution completed.",
            "tokens_used_delta": 13,
            "transcript_path": str(transcript),
            "artifact_paths": [],
            "pause_event": None,
            "pause_pending_path": None,
            "_session_resumable": False,
        }

    monkeypatch.setattr(harness_session_manager, "turn", fake_harness_turn)
    response = await client.post(
        f"/api/v1/chat/projects/{project['id']}/stream",
        headers=_headers(researcher),
        json={"answer": {"kind": "text", "text": "Start durable work"}, "conversation_id": conversation_id},
    )
    assert response.status_code == 200
    stream_events = [
        json.loads(line.removeprefix("data: "))
        for line in response.text.splitlines()
        if line.startswith("data: ")
    ]
    token_events = [event for event in stream_events if event["type"] == "token"]
    assert [event["text"] for event in token_events] == [
        "Durable ",
        "execution completed.",
    ]
    assert "".join(event["text"] for event in token_events) == "Durable execution completed."
    started = next(
        event
        for event in stream_events
        if event.get("type") == "progress" and event.get("event") == "run.started"
    )
    done = next(event for event in stream_events if event.get("type") == "done")
    assert started["runId"] == done["run_id"]
    assert started["sessionId"] == conversation_id
    assert started["projectId"] == project["id"]
    messages_during_model = observations.pop("messages_during_model")
    assert observations == {
        "run_status_during_model": "running",
        "attempt_status_during_model": "running",
        "sandbox_manifest_committed_before_model": True,
        "dispatch_received_frozen_attempt": True,
        "usage_events_during_model": 1,
    }
    assert len(messages_during_model) == 1
    role, content, run_id = messages_during_model[0]
    assert (role, content) == ("user", "Start durable work")
    assert run_id.startswith("run_")
    async with factory() as observer:
        durable_kinds = list(
            (
                await observer.scalars(
                    select(ExecutionEvent.kind).where(ExecutionEvent.run_id == run_id)
                )
            ).all()
        )
    assert "token.delta" not in durable_kinds
    checkpointed = (await run_in_repository_thread(repository.session_status, project["id"], conversation_id))
    assert checkpointed.clean is True
    assert checkpointed.head_commit != workspace.head_commit
    assert relative in (await run_in_repository_thread(repository.changed_paths, project["id"], conversation_id))


@pytest.mark.asyncio
async def test_explicit_cancel_stops_addressable_run_and_projects_terminal_state(
    runtime_client, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    client, _factory = runtime_client
    monkeypatch.setattr(settings, "harness_bridge_enabled", True)
    monkeypatch.setattr(settings, "harness_state_root", str(tmp_path))
    researcher = await _token(client, "researcher@atrium.local")
    created_backend = await client.post(
        "/api/v1/settings/model-backends",
        headers=_headers(researcher),
        json={
            "provider": "openai_compatible",
            "display_name": "Cancellation test",
            "model": "test-model",
            "base_url": "http://provider.invalid/v1",
            "api_key": "cancellation-test-key",
        },
    )
    await client.post(
        f"/api/v1/settings/model-backends/{created_backend.json()['id']}/default",
        headers=_headers(researcher),
    )
    project = (await client.get("/api/v1/projects/", headers=_headers(researcher))).json()[0]
    conversation = await client.post(
        f"/api/v1/projects/{project['id']}/sessions",
        headers=_headers(researcher),
        json={"title": "Cancellation"},
    )
    conversation_id = conversation.json()["id"]
    entered_model = asyncio.Event()
    allow_model_exit = asyncio.Event()
    active_run_id: str | None = None

    async def fake_harness_turn(**kwargs):
        nonlocal active_run_id
        active_run_id = kwargs["run_id"]
        entered_model.set()
        await allow_model_exit.wait()
        raise RuntimeError("runtime process ended after cancellation")

    async def fake_cancel_run(**kwargs):
        harness_session_manager._cancelled_run_ids.add(kwargs["run_id"])
        return kwargs["run_id"] == active_run_id

    monkeypatch.setattr(harness_session_manager, "turn", fake_harness_turn)
    monkeypatch.setattr(harness_session_manager, "cancel_run", fake_cancel_run)
    streaming = asyncio.create_task(
        client.post(
            f"/api/v1/chat/projects/{project['id']}/stream",
            headers=_headers(researcher),
            json={"answer": {"kind": "text", "text": "Start then cancel"}, "conversation_id": conversation_id},
        )
    )
    await asyncio.wait_for(entered_model.wait(), timeout=5)
    runs = await client.get(
        "/api/v1/runs",
        headers=_headers(researcher),
        params={"sessionId": conversation_id},
    )
    run_id = runs.json()["items"][0]["id"]
    assert runs.json()["items"][0]["status"] == "running"
    cancelled = await client.post(
        f"/api/v1/runs/{run_id}/cancel",
        headers=_headers(researcher),
        json={"reason": "User stopped the task"},
    )
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json() == {"runId": run_id, "status": "cancelled"}
    allow_model_exit.set()
    response = await asyncio.wait_for(streaming, timeout=5)
    assert response.status_code == 200
    detail = await client.get(f"/api/v1/runs/{run_id}", headers=_headers(researcher))
    assert detail.json()["run"]["status"] == "cancelled"
    assert detail.json()["attempts"][0]["status"] == "cancelled"
    projection = await client.get(
        f"/api/v1/sessions/{conversation_id}/events",
        headers=_headers(researcher),
        params={"runId": run_id},
    )
    assert [item["kind"] for item in projection.json()["items"]].count("run.cancelled") == 1


@pytest.mark.asyncio
async def test_sse_observer_disconnect_does_not_cancel_server_owned_execution(
    runtime_client, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from app.api.v1.chat import ChatRequest, TextAnswer, _local_execution_stream
    from app.services.local_execution import detached_execution_count

    client, factory = runtime_client
    monkeypatch.setattr(settings, "harness_bridge_enabled", True)
    monkeypatch.setattr(settings, "harness_state_root", str(tmp_path))
    researcher_token = await _token(client, "researcher@atrium.local")
    created_backend = await client.post(
        "/api/v1/settings/model-backends",
        headers=_headers(researcher_token),
        json={
            "provider": "openai_compatible",
            "display_name": "Disconnect test",
            "model": "test-model",
            "base_url": "http://provider.invalid/v1",
            "api_key": "disconnect-test-key",
        },
    )
    await client.post(
        f"/api/v1/settings/model-backends/{created_backend.json()['id']}/default",
        headers=_headers(researcher_token),
    )
    project_payload = (
        await client.get("/api/v1/projects/", headers=_headers(researcher_token))
    ).json()[0]
    conversation_payload = await client.post(
        f"/api/v1/projects/{project_payload['id']}/sessions",
        headers=_headers(researcher_token),
        json={"title": "Disconnect"},
    )
    conversation_id = conversation_payload.json()["id"]
    entered_model = asyncio.Event()
    allow_model_exit = asyncio.Event()
    cancel_calls: list[dict] = []
    transcript = tmp_path / "detached" / "transcript.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_bytes(b"")

    async def fake_harness_turn(**_kwargs):
        entered_model.set()
        await allow_model_exit.wait()
        return {
            "status": "completed",
            "run_id": "harness-detached-run",
            "final_text": "Detached execution completed after navigation.",
            "tokens_used_delta": 7,
            "transcript_path": str(transcript),
            "artifact_paths": [],
            "pause_event": None,
            "pause_pending_path": None,
            "_session_resumable": False,
        }

    async def fake_cancel_run(**kwargs):
        cancel_calls.append(kwargs)
        return True

    monkeypatch.setattr(harness_session_manager, "turn", fake_harness_turn)
    monkeypatch.setattr(harness_session_manager, "cancel_run", fake_cancel_run)
    monkeypatch.setattr("app.api.v1.chat.LOCAL_SSE_KEEPALIVE_SECONDS", 0.01)
    async with factory() as request_db:
        user = await request_db.scalar(
            select(User).where(User.email == "researcher@atrium.local")
        )
        project = await request_db.get(Project, project_payload["id"])
        conversation = await request_db.get(SessionProjection, conversation_id)
        response = _local_execution_stream(
            data=ChatRequest(
                answer=TextAnswer(kind="text", text="Long operation survives navigation"),
                conversation_id=conversation_id,
            ),
            user=user,
            db=request_db,
            conversation=conversation,
            project=project,
        )
    observer = response.body_iterator
    while True:
        first_event = await asyncio.wait_for(anext(observer), timeout=5)
        if "run.started" in str(first_event):
            break
    await asyncio.wait_for(entered_model.wait(), timeout=5)
    keepalive = await asyncio.wait_for(anext(observer), timeout=1)
    assert str(keepalive) == ": keepalive\n\n"
    await observer.aclose()

    assert detached_execution_count() == 1
    assert cancel_calls == []
    active_runs = await client.get(
        "/api/v1/runs",
        headers=_headers(researcher_token),
        params={"sessionId": conversation_id},
    )
    assert active_runs.status_code == 200, active_runs.text
    active_run_id = active_runs.json()["items"][0]["id"]
    allow_model_exit.set()

    run = None
    for _attempt in range(100):
        async with factory() as observer_db:
            run = await observer_db.scalar(
                select(Run)
                .where(Run.session_id == conversation_id)
                .order_by(Run.created_at.desc())
            )
            if run and run.status == "completed":
                break
        await asyncio.sleep(0.05)
    assert run is not None and run.status == "completed"
    for _attempt in range(100):
        if detached_execution_count() == 0:
            break
        await asyncio.sleep(0.01)

    assert detached_execution_count() == 0
    assert cancel_calls == []
    followed = await client.get(
        f"/api/v1/sessions/{conversation_id}/events/stream",
        headers=_headers(researcher_token),
        params={"runId": active_run_id, "afterSequence": 0},
    )
    assert followed.status_code == 200, followed.text
    assert "event: execution" in followed.text
    assert "event: end" in followed.text
    assert f'"runId":"{active_run_id}"' in followed.text
    assert '"status":"completed"' in followed.text
    assert "client_disconnected" not in followed.text
    replayed_sequences = [
        int(line.removeprefix("id: "))
        for line in followed.text.splitlines()
        if line.startswith("id: ")
    ]
    assert replayed_sequences == sorted(replayed_sequences)
    reconnect_after = replayed_sequences[0]
    reconnected = await client.get(
        f"/api/v1/sessions/{conversation_id}/events/stream",
        headers=_headers(researcher_token),
        params={"runId": active_run_id, "afterSequence": reconnect_after},
    )
    assert reconnected.status_code == 200, reconnected.text
    assert f"id: {reconnect_after}\nevent: execution" not in reconnected.text
    assert "event: end" in reconnected.text
    async with factory() as observer:
        run = await observer.get(Run, run.id)
        assert run.status == "completed"
        assert (run.summary or {}).get("cancelReason") is None
        attempt = await observer.scalar(select(RunAttempt).where(RunAttempt.run_id == run.id))
        assert attempt.status == "completed"
        commands = list(
            (await observer.scalars(select(Command).where(Command.run_id == run.id))).all()
        )
        # 「成功收尾」= 有结果、没错误。命令不再另存一个状态词。
        assert [(c.result is not None, c.error is None) for c in commands] == [(True, True)]
        assistant_message = await observer.scalar(
            select(SessionMessage).where(
                SessionMessage.session_id == conversation_id,
                SessionMessage.role == "assistant",
            )
        )
        assert assistant_message.content == "Detached execution completed after navigation."
        completed_events = await observer.scalar(
            select(func.count())
            .select_from(ExecutionEvent)
            .where(
                ExecutionEvent.run_id == run.id,
                ExecutionEvent.kind == "run.completed",
            )
        )
        assert completed_events == 1


@pytest.mark.asyncio
async def test_a_reader_closing_between_allocation_and_insert_does_not_reissue_the_number(
    runtime_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """一个会话发号、写行之间，另一个会话读完关掉 —— 下一个号不能再发一遍。

    这是上面那条测试在 CI 上红的那一次（2026-09-24，PR #1156 run 3685）的现场，
    固定成确定的交错：执行协程给 `usage.updated` 领到 4 号（`UPDATE sessions …
    RETURNING`）、还没 INSERT 那一行，测试自己的轮询会话（或 `_autoname`）读完
    关掉。夹具从前用 `StaticPool` 把所有会话接在**同一条** sqlite3 连接上，于是
    "关掉"发出的 ROLLBACK 撤的是**执行协程**那一半事务：自增没了，行却在随后的
    隐式事务里照常提交 —— 库里有 4 号行、计数器还停在 3，下一个事件再领到 4，
    `UNIQUE (tenant_id, session_id, sequence)` 当场拒收，run 一路失败。

    产品里每个会话有自己的连接（文件库走 `AsyncAdaptedQueuePool`，组织档是
    Postgres），别人的 ROLLBACK 碰不到这一半事务。夹具必须同形，否则它测出来的
    并发既不是产品的，也藏住产品的。
    """
    from app.services.app_events import record_app_event
    from app.services.execution_ingest import ExecutionIngestService

    client, factory = runtime_client
    token = await _token(client, "researcher@atrium.local")
    project_id = (await client.get("/api/v1/projects/", headers=_headers(token))).json()[0]["id"]
    created = await client.post(
        f"/api/v1/projects/{project_id}/sessions",
        headers=_headers(token),
        json={"title": "Numbering"},
    )
    session_id = created.json()["id"]

    allocate = ExecutionIngestService._next_sequence
    readers_closed = 0

    async def allocate_then_a_reader_closes(self, db, session):
        nonlocal readers_closed
        number = await allocate(self, db, session)
        # 号已经领到、行还没写：别的会话此刻读一行、关掉。
        async with factory() as reader:
            await reader.scalar(select(func.count()).select_from(Run))
        readers_closed += 1
        return number

    monkeypatch.setattr(ExecutionIngestService, "_next_sequence", allocate_then_a_reader_closes)
    async with factory() as writer:
        first = await record_app_event(
            writer, session_id=session_id, kind="usage.updated", payload={}, dedupe_key="first"
        )
        await writer.commit()
    assert readers_closed == 1  # 交错真的发生了，不是空跑
    monkeypatch.setattr(ExecutionIngestService, "_next_sequence", allocate)

    async with factory() as writer:
        second = await record_app_event(
            writer, session_id=session_id, kind="session.message", payload={}, dedupe_key="second"
        )
        await writer.commit()

    assert second.sequence == first.sequence + 1
    async with factory() as observer:
        issued = (
            await observer.scalars(
                select(ExecutionEvent.sequence)
                .where(ExecutionEvent.session_id == session_id)
                .order_by(ExecutionEvent.sequence)
            )
        ).all()
        last_issued = await observer.scalar(
            select(SessionProjection.next_sequence).where(
                SessionProjection.session_id == session_id
            )
        )
    assert last_issued == second.sequence == max(issued)


@pytest.mark.asyncio
async def test_run_event_sse_replays_then_follows_new_durable_sequence(
    runtime_client,
) -> None:
    from app.api.v1.execution import stream_session_events
    from app.services.execution_ingest import ExecutionIngestService, IngestContext
    from app.services.execution_observers import (
        notify_execution_observers,
        publish_run_transient,
    )

    client, factory = runtime_client
    researcher_token = await _token(client, "researcher@atrium.local")
    project_id = (
        await client.get("/api/v1/projects/", headers=_headers(researcher_token))
    ).json()[0]["id"]
    created = await client.post(
        f"/api/v1/projects/{project_id}/sessions",
        headers=_headers(researcher_token),
        json={"title": "Event SSE follow"},
    )
    assert created.status_code == 201, created.text
    session_id = created.json()["id"]
    run_id = "run_event_sse_follow"
    adapter_state: dict = {}
    service = ExecutionIngestService()

    async def ingest(db, *, raw: dict, offset: int) -> None:
        session = await db.get(SessionProjection, session_id)
        user = await db.scalar(
            select(User).where(User.email == "researcher@atrium.local")
        )
        encoded = json.dumps(raw, sort_keys=True).encode()
        await service.ingest_raw_record(
            db,
            context=IngestContext(
                tenant_id=session.tenant_id,
                workspace_id=session.workspace_id,
                project_id=session.project_id,
                session_id=session_id,
                run_id=run_id,
                actor_user_id=user.id,
            ),
            file_identity="test:event-sse-follow",
            byte_offset=offset,
            raw_line=encoded,
            raw=raw,
            adapter_state=adapter_state,
        )
        await db.commit()
        await notify_execution_observers()

    async with factory() as writer:
        await ingest(
            writer,
            raw={"at": datetime.now(UTC).isoformat(), "event": "run_start"},
            offset=0,
        )
    async with factory() as request_db:
        user = await request_db.scalar(
            select(User).where(User.email == "researcher@atrium.local")
        )
        response = await stream_session_events(
            session_id=session_id,
            run_id=run_id,
            after_sequence=0,
            user=user,
            tenant_id=settings.runtime_tenant_id,
            db=request_db,
        )
    follower = response.body_iterator
    replayed = await asyncio.wait_for(anext(follower), timeout=2)
    assert "event: execution" in str(replayed)
    assert '"kind":"run.started"' in str(replayed)

    next_token = asyncio.create_task(anext(follower))
    await asyncio.sleep(0.01)
    await publish_run_transient(
        tenant_id=settings.runtime_tenant_id,
        session_id=session_id,
        run_id=run_id,
        event={"type": "token", "text": "future token"},
    )
    token = await asyncio.wait_for(next_token, timeout=2)
    assert "event: token" in str(token)
    assert 'data: {"runId":"run_event_sse_follow","text":"future token"}' in str(token)
    assert "id:" not in str(token)

    next_event = asyncio.create_task(anext(follower))
    await asyncio.sleep(0.01)
    async with factory() as writer:
        await ingest(
            writer,
            raw={
                "at": datetime.now(UTC).isoformat(),
                "event": "run_end",
                "status": "completed",
            },
            offset=100,
        )
    followed = await asyncio.wait_for(next_event, timeout=2)
    assert "event: execution" in str(followed)
    assert '"kind":"run.completed"' in str(followed)
    end = await asyncio.wait_for(anext(follower), timeout=2)
    assert "event: end" in str(end)
    assert '"status":"completed"' in str(end)


@pytest.mark.asyncio
async def test_run_transient_queue_is_bounded_and_signals_live_gap() -> None:
    from app.services.execution_observers import (
        publish_run_transient,
        subscribe_run_transients,
        unsubscribe_run_transients,
    )

    coordinates = {
        "tenant_id": "tenant-transient-bound",
        "session_id": "session-transient-bound",
        "run_id": "run-transient-bound",
    }
    queue = subscribe_run_transients(**coordinates)
    try:
        assert queue.maxsize == 256
        for index in range(300):
            await publish_run_transient(
                **coordinates,
                event={"type": "token", "text": str(index)},
            )
        buffered = []
        while not queue.empty():
            buffered.append(queue.get_nowait())
        assert len(buffered) <= 256
        assert any(event.get("type") == "token_gap" for event in buffered)
        assert buffered[-1] == {"type": "token", "text": "299"}
    finally:
        unsubscribe_run_transients(queue, **coordinates)


@pytest.mark.asyncio
async def test_conversation_creates_durable_run_events_and_project_file(runtime_client) -> None:
    client, factory = runtime_client
    researcher = await _token(client, "researcher@atrium.local")
    projects = await client.get("/api/v1/projects/", headers=_headers(researcher))
    project_id = projects.json()[0]["id"]
    conversation = await client.post(
        f"/api/v1/projects/{project_id}/sessions",
        headers=_headers(researcher),
        json={"title": "New research"},
    )
    conversation_id = conversation.json()["id"]
    user_request = (
        "Create an executable literature review plan with auditable evidence, explicit gaps, "
        "and reproducible screening criteria"
    )
    response = await client.post(
        f"/api/v1/chat/projects/{project_id}/stream",
        headers=_headers(researcher),
        json={
            "answer": {"kind": "text", "text": user_request},
            "conversation_id": conversation_id,
        },
    )
    assert response.status_code == 200
    events = [
        json.loads(line.removeprefix("data: "))
        for line in response.text.splitlines()
        if line.startswith("data: ")
    ]
    done = next(event for event in events if event["type"] == "done")
    assert done["conversation_id"] == conversation_id
    assert done["artifact_id"] is None
    assert done["command_id"]
    assert done["command_id"] != done["run_id"]

    detail = await client.get(
        f"/api/v1/projects/{project_id}/sessions/{conversation_id}", headers=_headers(researcher)
    )
    run = await client.get(f"/api/v1/runs/{done['run_id']}", headers=_headers(researcher))
    projection = await client.get(
        f"/api/v1/sessions/{done['session_id']}/events", headers=_headers(researcher)
    )
    artifacts = await client.get(
        f"/api/v1/projects/{project_id}/artifacts/", headers=_headers(researcher)
    )
    assert detail.json()["title"] == f"{user_request[:77].rstrip()}..."
    assert len(detail.json()["title"]) <= 80
    canonical_messages = await client.get(
        f"/api/v1/projects/{project_id}/sessions/{conversation_id}/messages",
        headers=_headers(researcher),
    )
    assert canonical_messages.status_code == 200
    assert [item["role"] for item in canonical_messages.json()["items"]] == [
        "user",
        "assistant",
    ]
    assert all(
        item["sessionId"] == conversation_id
        and "actorUserId" in item
        and item["commandId"] == done["command_id"]
        and item["runId"] == done["run_id"]
        for item in canonical_messages.json()["items"]
    )
    assert run.json()["run"]["status"] == "completed"
    assert run.json()["attempts"][0]["status"] == "completed"
    kinds = [event["kind"] for event in projection.json()["items"]]
    assert kinds == [
        "run.started",
        "session.message",
        "usage.updated",
        "session.message",
        "run.completed",
    ]
    assert artifacts.json() == []
    staged = await client.get(
        f"/api/v1/projects/{project_id}/sessions/{conversation_id}/change-set",
        headers=_headers(researcher),
    )
    assert staged.status_code == 200
    assert staged.json()["filesChanged"] == 1
    assert "runs/extensions/demo/" in staged.json()["patch"]
    # 树是一层一层给的，所以这里问的是那个 run 自己那一层 —— 不是"把整棵树
    # 拿来找一遍"（那正是被切到 5000 条、让整个目录消失的那种问法）。
    tree = await client.get(
        f"/api/v1/projects/{project_id}/repository/tree",
        params={"sessionId": conversation_id, "path": "runs/extensions/demo"},
        headers=_headers(researcher),
    )
    assert tree.status_code == 200
    memo_path = None
    for run_dir in tree.json()["entries"]:
        inside = await client.get(
            f"/api/v1/projects/{project_id}/repository/tree",
            params={"sessionId": conversation_id, "path": run_dir["path"]},
            headers=_headers(researcher),
        )
        memo_path = next(
            (entry["path"] for entry in inside.json()["entries"] if entry["kind"] == "file"),
            memo_path,
        )
    assert memo_path is not None, tree.json()
    memo = await client.get(
        f"/api/v1/projects/{project_id}/repository/file",
        params={"sessionId": conversation_id, "path": memo_path},
        headers=_headers(researcher),
    )
    assert memo.status_code == 200
    assert user_request in memo.json()["content"]
    session_before_publish = await client.get(
        f"/api/v1/projects/{project_id}/sessions/{conversation_id}",
        headers=_headers(researcher),
    )
    published_turn = await client.post(
        f"/api/v1/projects/{project_id}/sessions/{conversation_id}/publish",
        headers=_headers(researcher),
        json={"message": "Publish real Session turn"},
    )
    assert published_turn.status_code == 200, published_turn.text
    # 发布的结果是一个 git 提交 —— 拿得到就能 `git show`，不是只在一张表里
    # 有意义的 revision id。
    assert len(published_turn.json()["commitSha"]) == 40
    published_artifacts = await client.get(
        f"/api/v1/projects/{project_id}/artifacts/", headers=_headers(researcher)
    )
    assert published_artifacts.json() == []

    outsider = await _token(client, "outsider@other.local")
    assert (
        await client.get(f"/api/v1/runs/{done['run_id']}", headers=_headers(outsider))
    ).status_code == 404
    assert (
        await client.get(
            f"/api/v1/sessions/{done['session_id']}/events", headers=_headers(outsider)
        )
    ).status_code == 404


async def _seed_session_with_run(client, factory, researcher, *, title, run_id, status):
    project_id = (
        await client.get("/api/v1/projects/", headers=_headers(researcher))
    ).json()[0]["id"]
    session = await client.post(
        f"/api/v1/projects/{project_id}/sessions",
        headers=_headers(researcher),
        json={"title": title},
    )
    assert session.status_code == 201, session.text
    session_id = session.json()["id"]
    async with factory() as db:
        projection = await db.get(SessionProjection, session_id)
        assert projection is not None
        db.add(
            Run(
                id=run_id,
                tenant_id=projection.tenant_id,
                workspace_id=projection.workspace_id,
                project_id=projection.project_id,
                session_id=projection.session_id,
                status=status,
            )
        )
        await db.commit()
    return project_id, session_id


@pytest.mark.asyncio
async def test_a_session_with_work_in_flight_never_starts_a_second_turn(
    runtime_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """有人在跑的时候，消息分流成插话，`harness.turn` 不许被碰。

    ## 判据从"run 行说忙"换成"现场说忙"（RFC D10，2026-08-23）

    这条原来按五种 run 状态参数化（queued/dispatching/running/…），也就是拿
    **库里那行**当占用判据。8-21 那次事故的一半就出在它身上：run 行是平台对
    worker 的**转述**，它会和现场分叉，而分叉时没有任何一层报错。

    现在只有一个判据 `is_occupied()`（本进程的锁 **或** worker 落在盘上的
    自报活动，两者都是现场）。所以这里也只 stub 那一个 —— run 行是什么状态
    与"能不能开新一轮"无关了，那五个参数从此没有意义。
    """
    client, factory = runtime_client
    researcher = await _token(client, "researcher@atrium.local")
    project_id, session_id = await _seed_session_with_run(
        client, factory, researcher,
        title="Work in flight", run_id="run_active_guard", status="running",
    )
    monkeypatch.setattr(harness_session_manager, "is_occupied", lambda *_a, **_k: True)

    async def _delivered(**kwargs):
        return {"delivered": True, "item_id": "in-guard", "occupancy": "working"}

    monkeypatch.setattr(harness_session_manager, "deliver", _delivered)

    async def must_not_start_harness(**_kwargs):
        pytest.fail("busy Session must interject, never start a concurrent turn")

    monkeypatch.setattr(harness_session_manager, "turn", must_not_start_harness)
    response = await client.post(
        f"/api/v1/chat/projects/{project_id}/stream",
        headers=_headers(researcher),
        json={"answer": {"kind": "text", "text": "Start another ordinary command"}, "conversation_id": session_id},
    )
    assert response.status_code == 200, response.text
    done = next(
        json.loads(line.removeprefix("data: "))
        for line in response.text.splitlines()
        if line.startswith("data: ") and '"done"' in line
    )
    assert done["routed"] == "interject"
    assert done["runId"] == "run_active_guard"


@pytest.mark.asyncio
async def test_a_run_row_that_says_running_with_nobody_running_starts_a_turn(
    runtime_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """这条是上面那条**被拆掉的第二判据**留下的窟窿，现在补上。

    库里写着 `running`、而 worker 早就没了（重启、接不回来、被回收）——
    从前这会被判成"忙"→ 走插话 → 而投递面换成 socket 之后就**送不到** →
    409 把用户的话弹回去。正确的行为是：这句话开一条新 run，从 checkpoint
    接着跑。

    ## 它接替了 `test_..._with_no_live_process_is_still_accepted`

    那条测的是"进程死了插话也照收"（wangd 2026-08-17：「我在插话，为什么会
    得到 409？不应该是调度器能很好的承接吗」）。**要守的性质一个字没变**
    —— 用户的话永远有地方去 —— 变的是它怎么被满足：从前靠"把话写进工作区
    的文件，等某个未来的 worker 来捡"，现在靠"没人在跑就直接开一轮"。

    后者严格更好：话立刻被处理，而不是躺在文件里等一个可能永远不会发生的
    续跑。「可寻址恒真」不是靠某一个判据准，是靠没有一条路径通向拒收。
    """
    client, factory = runtime_client
    researcher = await _token(client, "researcher@atrium.local")
    project_id, session_id = await _seed_session_with_run(
        client, factory, researcher,
        title="Ghost run", run_id="run_ghost", status="running",
    )
    # 现场：没有人在跑（注册表里没有这个会话）。库里那行说 running。
    monkeypatch.setattr(harness_session_manager, "is_occupied", lambda *_a, **_k: False)

    async def _must_not_be_delivered(**_kwargs):
        pytest.fail("没有人在跑的时候不该走插话 —— 那条路会把用户的话弹回 409")

    monkeypatch.setattr(harness_session_manager, "deliver", _must_not_be_delivered)
    response = await client.post(
        f"/api/v1/chat/projects/{project_id}/stream",
        headers=_headers(researcher),
        json={"answer": {"kind": "text", "text": "还在吗"}, "conversation_id": session_id},
    )
    assert response.status_code == 200, response.text
    done = next(
        json.loads(line.removeprefix("data: "))
        for line in response.text.splitlines()
        if line.startswith("data: ") and '"done"' in line
    )
    assert done.get("routed") != "interject", (
        f"库里那行说忙，就把用户的话拦下来了 —— 而根本没有人在跑：{done}"
    )


@pytest.mark.asyncio
async def test_the_explicit_interrupt_endpoint_says_why_it_cannot_deliver(
    runtime_client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**显式**插话门面（`/interrupt`）在没人跑的时候如实 409。

    与 chat 那条路的区别是判断谁做：chat 入口自己分流（没人跑就开一轮，
    用户的话永远有地方去）；`/interrupt` 是调用方**明说**"我要插话"，那就
    只能如实回答"没有当前轮可以插" —— 替它改成开新一轮是替调用方改主意。

    两种答案都不许含糊：409 必须说得出原因（契约要送到调用方）。
    """
    client, factory = runtime_client
    researcher = await _token(client, "researcher@atrium.local")
    project_id = (
        await client.get("/api/v1/projects/", headers=_headers(researcher))
    ).json()[0]["id"]
    session = await client.post(
        f"/api/v1/projects/{project_id}/sessions",
        headers=_headers(researcher),
        json={"title": "No worktree"},
    )
    session_id = session.json()["id"]
    async with factory() as db:
        projection = await db.get(SessionProjection, session_id)
        assert projection is not None
        projection.git_worktree_path = None
        db.add(
            Run(
                id="run_no_worktree",
                tenant_id=projection.tenant_id,
                workspace_id=projection.workspace_id,
                project_id=projection.project_id,
                session_id=projection.session_id,
                status="running",
            )
        )
        await db.commit()

    response = await client.post(
        f"/api/v1/projects/{project_id}/sessions/{session_id}/interrupt",
        headers=_headers(researcher),
        json={"text": "有人吗"},
    )
    assert response.status_code == 409, response.text
    assert "Nothing is running" in response.text, (
        f"409 说不出为什么投不进去，调用方只能猜：{response.text}"
    )


@pytest.mark.asyncio
async def test_autonomous_project_publishes_qualified_completed_run(runtime_client) -> None:
    client, factory = runtime_client
    researcher = await _token(client, "researcher@atrium.local")
    projects = await client.get("/api/v1/projects/", headers=_headers(researcher))
    project_id = projects.json()[0]["id"]
    async with factory() as db:
        config = await db.scalar(
            select(ProjectConfig).where(ProjectConfig.project_id == project_id)
        )
        assert config is not None
        config.operation_mode = OperationMode.AUTONOMOUS
        await db.commit()

    conversation = await client.post(
        f"/api/v1/projects/{project_id}/sessions",
        headers=_headers(researcher),
        json={"title": "Continuous research"},
    )
    conversation_id = conversation.json()["id"]
    response = await client.post(
        f"/api/v1/chat/projects/{project_id}/stream",
        headers=_headers(researcher),
        json={"answer": {"kind": "text", "text": "Produce a qualified analysis note"}, "conversation_id": conversation_id},
    )
    events = [
        json.loads(line.removeprefix("data: "))
        for line in response.text.splitlines()
        if line.startswith("data: ")
    ]
    done = next(event for event in events if event["type"] == "done")

    assert done["status"] == "completed"
    assert done["project_commit_sha"] is not None, [
        event for event in events if str(event.get("event", "")).startswith("workspace.")
    ]
    assert done["artifact_id"] is None
    repository_tree = await client.get(
        f"/api/v1/projects/{project_id}/repository/tree",
        params={"path": "runs/extensions"},
        headers=_headers(researcher),
    )
    assert [item["name"] for item in repository_tree.json()["entries"]] == ["demo"], (
        repository_tree.json()
    )


@pytest.mark.asyncio
async def test_harness_pause_is_waiting_human_and_not_fake_completion(
    runtime_client, monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    client, factory = runtime_client
    monkeypatch.setattr(settings, "harness_bridge_enabled", True)
    monkeypatch.setattr(settings, "harness_state_root", str(tmp_path))
    researcher = await _token(client, "researcher@atrium.local")
    catalog = await client.get("/api/v1/settings/model-backends", headers=_headers(researcher))
    anthropic = next(item for item in catalog.json() if item["provider"] == "anthropic")
    assert anthropic["status"] == "unsupported_by_harness"
    unsupported_default = await client.post(
        f"/api/v1/settings/model-backends/{anthropic['id']}/default",
        headers=_headers(researcher),
    )
    assert unsupported_default.status_code == 409
    created = await client.post(
        "/api/v1/settings/model-backends",
        headers=_headers(researcher),
        json={
            "provider": "openai_compatible",
            "display_name": "Harness test",
            "model": "test-model",
            "base_url": "http://provider.invalid/v1",
            "api_key": "test-key",
        },
    )
    assert created.status_code == 201
    selected = await client.post(
        f"/api/v1/settings/model-backends/{created.json()['id']}/default",
        headers=_headers(researcher),
    )
    assert selected.status_code == 200

    transcript = tmp_path / "orchestrator__project" / "transcript.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text(
        json.dumps(
            {
                "event": "human_input_requested",
                "at": datetime.now(UTC).isoformat(),
                "question": "请选择研究范围",
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    checkpoint = transcript.parent / "pause_pending.json"
    checkpoint.write_text("{}", encoding="utf-8")

    binding: AppRunBinding | None = None

    async def fake_harness_turn(**kwargs):
        nonlocal binding
        binding = AppRunBinding(
            kwargs["user"].id,
            kwargs["conversation_id"],
            kwargs["run_id"],
            kwargs["session_id"],
        )
        return {
            "status": "paused",
            "run_id": "orchestrator__project",
            "final_text": "",
            "tokens_used": 12,
            "transcript_path": str(transcript),
            "artifact_paths": [],
            "pause_event": {
                "question": "请选择研究范围",
                "context": "两个范围的成本和证据覆盖不同。",
                "options": ["窄范围", "宽范围"],
                "asking_node_type": "literature",
            },
            "pause_pending_path": str(checkpoint),
            "_session_resumable": True,
        }

    async def fake_harness_answer(**_kwargs):
        nonlocal binding
        transcript.write_text(
            transcript.read_text(encoding="utf-8")
            + json.dumps(
                {
                    "event": "loop_resume",
                    "at": datetime.now(UTC).isoformat(),
                },
                ensure_ascii=False,
            )
            + "\n"
            + json.dumps(
                {
                    "event": "llm_response",
                    "at": datetime.now(UTC).isoformat(),
                    "usage": {"total_tokens": 8, "coverage": "partial"},
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        binding = None
        return {
            "status": "completed",
            "run_id": "orchestrator__project",
            "final_text": "已按窄范围继续并完成。",
            "tokens_used": 20,
            "transcript_path": str(transcript),
            "artifact_paths": [],
            "pause_event": None,
            "pause_pending_path": None,
            "_session_resumable": False,
        }

    monkeypatch.setattr(harness_session_manager, "turn", fake_harness_turn)
    monkeypatch.setattr(harness_session_manager, "answer", fake_harness_answer)
    monkeypatch.setattr(
        harness_session_manager,
        "paused_binding",
        lambda _project_id, _session_id: binding,
    )

    projects = await client.get("/api/v1/projects/", headers=_headers(researcher))
    project_id = projects.json()[0]["id"]
    conversation = await client.post(
        f"/api/v1/projects/{project_id}/sessions",
        headers=_headers(researcher),
        json={"title": "Pause E2E"},
    )
    conversation_id = conversation.json()["id"]
    response = await client.post(
        f"/api/v1/chat/projects/{project_id}/stream",
        headers=_headers(researcher),
        json={"answer": {"kind": "text", "text": "开始研究"}, "conversation_id": conversation_id},
    )
    events = [
        json.loads(line.removeprefix("data: "))
        for line in response.text.splitlines()
        if line.startswith("data: ")
    ]
    done = next(event for event in events if event["type"] == "done")
    assert done["status"] == "waiting_human"
    # 终帧送的是**同一个 view**，不是它的第三种形状（2026-09-01）。
    # 断言落在"人能不能答上"这个效果上：入口是那张卡，卡就在入口里。
    answer = done["view"]["answer"]
    assert answer["via"] == "pause"
    assert answer["pause"]["prompt"] == "请选择研究范围"
    assert answer["pause"]["options"] == ["窄范围", "宽范围"]
    assert answer["pause"]["askingNodeType"] == "literature"
    assert done["artifact_id"] is None

    run = await client.get(f"/api/v1/runs/{done['run_id']}", headers=_headers(researcher))
    projection = await client.get(
        f"/api/v1/sessions/{done['session_id']}/events", headers=_headers(researcher)
    )
    assert run.json()["run"]["status"] == "waiting_human"
    # 「这个暂停还能不能答」不再是 summary 里的一条落盘判决，而是 view 现算的：
    # 在等人（waitingOn）且运行时还在（phase=alive）。
    assert run.json()["run"]["view"]["waitingOn"]["kind"] == "human"
    assert run.json()["run"]["view"]["phase"] == "alive"
    assert run.json()["run"]["summary"]["pause"]["context"].startswith("两个范围")
    assert run.json()["attempts"][0]["status"] == "running"
    assert str(tmp_path) not in run.text
    kinds = [event["kind"] for event in projection.json()["items"]]
    assert "run.paused" in kinds
    assert "run.completed" not in kinds
    pause_projection = next(
        event for event in projection.json()["items"] if event["kind"] == "run.paused"
    )
    assert pause_projection["payload"]["options"] == ["窄范围", "宽范围"]
    assert pause_projection["payload"]["askingNodeType"] == "literature"

    resumed = await client.post(
        f"/api/v1/chat/projects/{project_id}/stream",
        headers=_headers(researcher),
        json={"answer": {"kind": "text", "text": "窄范围"}, "conversation_id": conversation_id},
    )
    assert resumed.status_code == 200
    assert "stale_unknown" not in resumed.text
    resumed_events = [
        json.loads(line.removeprefix("data: "))
        for line in resumed.text.splitlines()
        if line.startswith("data: ")
    ]
    # resume 不许被冲突挡回来。409 有两种出口，各判各的：
    #   · 进流之前抛 HTTPException → 上面 status_code == 200 已经拦住；
    #   · 进流之后炸 → 只能是 {"type": "error"} 事件（chat.py 的两条 emit
    #     出口都带这个 type），所以判事件，不判裸文本。
    # 原来这里写的是 `assert "409" not in resumed.text` —— 对整段 SSE 做子串
    # 匹配，而响应里全是随机 UUID：只要哪个 UUID 里出现 `409`（实测约 4.5%
    # 每次，PR#425 就是栽在 `…-4096-…` 上），测试就红，报的还是"resume 被
    # 409 挡回来了"这种假原因。判据得盯结构化字段，别盯字符串里碰巧的三位数。
    assert [event for event in resumed_events if event["type"] == "error"] == []
    resumed_done = next(event for event in resumed_events if event["type"] == "done")
    assert resumed_done["run_id"] == done["run_id"]
    assert resumed_done["status"] == "completed"
    completed = await client.get(f"/api/v1/runs/{done['run_id']}", headers=_headers(researcher))
    assert completed.json()["run"]["status"] == "completed"
    assert completed.json()["attempts"][0]["status"] == "completed"
    assert resumed_done["command_id"] != done["command_id"]
    assert resumed_done["run_id"] == done["run_id"]
    completed_projection = await client.get(
        f"/api/v1/sessions/{done['session_id']}/events", headers=_headers(researcher)
    )
    completed_kinds = [event["kind"] for event in completed_projection.json()["items"]]
    assert "step.started" not in completed_kinds
    assert "step.completed" not in completed_kinds
    messages = await client.get(
        f"/api/v1/projects/{project_id}/sessions/{conversation_id}/messages",
        headers=_headers(researcher),
    )
    assert [item["content"] for item in messages.json()["items"]] == [
        "开始研究",
        "请选择研究范围",
        "窄范围",
        "已按窄范围继续并完成。",
    ]
    assert [item["commandId"] for item in messages.json()["items"]] == [
        done["command_id"],
        done["command_id"],
        resumed_done["command_id"],
        resumed_done["command_id"],
    ]
    assert {item["runId"] for item in messages.json()["items"]} == {done["run_id"]}
    async with factory() as db:
        stored_messages = list(
            (
                await db.execute(
                    select(SessionMessage).where(SessionMessage.session_id == conversation_id)
                )
            )
            .scalars()
            .all()
        )
        assert len(stored_messages) == 4


@pytest.mark.asyncio
async def test_two_formal_turns_only_ingest_live_transcript_ranges(
    runtime_client, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    client, factory = runtime_client
    monkeypatch.setattr(settings, "harness_bridge_enabled", True)
    monkeypatch.setattr(settings, "harness_state_root", str(tmp_path))
    researcher = await _token(client, "researcher@atrium.local")
    created = await client.post(
        "/api/v1/settings/model-backends",
        headers=_headers(researcher),
        json={
            "provider": "openai_compatible",
            "display_name": "Live transcript test",
            "model": "test-model",
            "base_url": "http://provider.invalid/v1",
            "api_key": "test-key",
        },
    )
    assert created.status_code == 201
    selected = await client.post(
        f"/api/v1/settings/model-backends/{created.json()['id']}/default",
        headers=_headers(researcher),
    )
    assert selected.status_code == 200

    transcript = tmp_path / "orchestrator__two-turns" / "transcript.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_bytes(b"")
    byte_offsets: list[int] = []
    deltas = (5, 7)
    cumulative = 0

    async def fake_harness_turn(**kwargs):
        nonlocal cumulative
        index = len(byte_offsets)
        delta = deltas[index]
        cumulative += delta
        raw_event = {
            "event": "llm_response",
            "at": datetime.now(UTC).isoformat(),
            "usage": {"total_tokens": delta, "coverage": "partial"},
        }
        raw_line = (json.dumps(raw_event) + "\n").encode()
        byte_start = transcript.stat().st_size
        byte_offsets.append(byte_start)
        with transcript.open("ab") as output:
            output.write(raw_line)
        await kwargs["on_protocol_event"](
            {
                "type": "transcript",
                "transcript_path": str(transcript),
                "byte_start": byte_start,
                "byte_end": byte_start + len(raw_line),
                "event": raw_event,
            }
        )
        return {
            "status": "completed",
            "run_id": "orchestrator__two-turns",
            "final_text": f"formal turn {index + 1}",
            "tokens_used": cumulative,
            "tokens_used_delta": delta,
            "transcript_path": str(transcript),
            "artifact_paths": [],
            "pause_event": None,
            "pause_pending_path": None,
            "_session_resumable": False,
        }

    monkeypatch.setattr(harness_session_manager, "turn", fake_harness_turn)
    monkeypatch.setattr(harness_session_manager, "paused_binding", lambda *_args: None)
    project_id = (await client.get("/api/v1/projects/", headers=_headers(researcher))).json()[0][
        "id"
    ]
    conversation_id = (
        await client.post(
            f"/api/v1/projects/{project_id}/sessions",
            headers=_headers(researcher),
            json={"title": "Two formal turns"},
        )
    ).json()["id"]

    done_events = []
    for message in ("first formal turn", "second formal turn"):
        response = await client.post(
            f"/api/v1/chat/projects/{project_id}/stream",
            headers=_headers(researcher),
            json={"answer": {"kind": "text", "text": message}, "conversation_id": conversation_id},
        )
        assert response.status_code == 200
        events = [
            json.loads(line.removeprefix("data: "))
            for line in response.text.splitlines()
            if line.startswith("data: ")
        ]
        done_events.append(next(event for event in events if event["type"] == "done"))

    assert done_events[0]["run_id"] != done_events[1]["run_id"]
    assert done_events[0]["command_id"] != done_events[1]["command_id"]
    async with factory() as db:
        for index, done_event in enumerate(done_events):
            run = await db.get(Run, done_event["run_id"])
            assert run is not None
            assert run.total_tokens == deltas[index]
            usage_events = list(
                (
                    await db.execute(
                        select(ExecutionEvent).where(
                            ExecutionEvent.run_id == done_event["run_id"],
                            ExecutionEvent.kind == "usage.updated",
                        )
                    )
                )
                .scalars()
                .all()
            )
            assert len(usage_events) == 1
            assert usage_events[0].byte_offset == byte_offsets[index]


@pytest.mark.asyncio
async def test_failed_formal_turn_survives_reload_and_can_retry(
    runtime_client, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    client, factory = runtime_client
    monkeypatch.setattr(settings, "harness_bridge_enabled", True)
    monkeypatch.setattr(settings, "harness_state_root", str(tmp_path))
    researcher = await _token(client, "researcher@atrium.local")
    created = await client.post(
        "/api/v1/settings/model-backends",
        headers=_headers(researcher),
        json={
            "provider": "openai_compatible",
            "display_name": "Fail-loud test",
            "model": "test-model",
            "base_url": "http://provider.invalid/v1",
            "api_key": "test-key",
        },
    )
    assert created.status_code == 201
    assert (
        await client.post(
            f"/api/v1/settings/model-backends/{created.json()['id']}/default",
            headers=_headers(researcher),
        )
    ).status_code == 200

    transcript = tmp_path / "orchestrator__retry" / "transcript.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_bytes(b"")
    call_count = 0

    async def fake_harness_turn(**_kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError(
                "HTTP 402 Payment Required; api_key=sk-super-secret; "
                "/Users/researcher/private/provider.log"
            )
        return {
            "status": "completed",
            "run_id": "orchestrator__retry",
            "final_text": "retry completed",
            "tokens_used": 3,
            "tokens_used_delta": 3,
            "transcript_path": str(transcript),
            "artifact_paths": [],
            "pause_event": None,
            "pause_pending_path": None,
            "_session_resumable": False,
        }

    monkeypatch.setattr(harness_session_manager, "turn", fake_harness_turn)
    monkeypatch.setattr(harness_session_manager, "paused_binding", lambda *_args: None)
    project_id = (await client.get("/api/v1/projects/", headers=_headers(researcher))).json()[0][
        "id"
    ]
    conversation_id = (
        await client.post(
            f"/api/v1/projects/{project_id}/sessions",
            headers=_headers(researcher),
            json={"title": "New research"},
        )
    ).json()["id"]
    user_request = "Run the payment-gated research model"

    failed_response = await client.post(
        f"/api/v1/chat/projects/{project_id}/stream",
        headers=_headers(researcher),
        json={"answer": {"kind": "text", "text": user_request}, "conversation_id": conversation_id},
    )
    assert failed_response.status_code == 200
    failed_events = [
        json.loads(line.removeprefix("data: "))
        for line in failed_response.text.splitlines()
        if line.startswith("data: ")
    ]
    failure = next(event for event in failed_events if event["type"] == "error")
    # 「HTTP 402 Payment Required」是**上游明确拒绝**，不是"我们认不出这是什么"。
    # 2026-08-22 之前这两件事共用兜底文案，用户读到「平台内部错误 / 再发一次
    # 即可」—— 归因和下一步都是反的。
    assert failure["code"] == "upstream_rejected"
    assert failure["retryable"] is False, "同一个欠费账号，重发一万次都是这个结果"
    assert failure["status"] == "failed"
    assert failure["runId"].startswith("run_")
    assert failure["commandId"]
    assert failure["commandId"] != failure["runId"]
    # provider 的原文进**折叠区**，不进正文 —— 正文是产品文案。
    # 这条断言以前是 `in failure["message"]`，那正是 2026-08-11 会话页上整条
    # SQL 转储的来源：`message` 装的就是 `str(exc)`。
    assert "HTTP 402 Payment Required" in failure["detail"]
    assert "HTTP 402" not in failure["message"]
    assert "sk-super-secret" not in failed_response.text
    assert "/Users/researcher" not in failed_response.text

    reloaded = await client.get(
        f"/api/v1/projects/{project_id}/sessions/{conversation_id}/messages",
        headers=_headers(researcher),
    )
    assert reloaded.status_code == 200
    # 失败**不再在对话里留第二份抄件**（wangd 2026-08-22）。
    #
    # 原来这里还有一条 role="system" 的消息，内容是 `title. body` ——
    # 权威那份（run.summary["failure"]）的一个更笨的副本：没有 recovery、
    # 不分档，前端渲染成红色 role="alert"。于是同一件事在页面上出现两次，
    # 一行灰的说"接着跑"，一行红的说"内部错误"。
    #
    # 事实一个字没丢：执行事件里那条 session.message 还在（见下面
    # `failed_kinds`），run summary 里是完整的一份。
    assert [item["role"] for item in reloaded.json()["items"]] == ["user"]
    assert reloaded.json()["items"][0]["content"] == user_request
    assert {item["commandId"] for item in reloaded.json()["items"]} == {failure["commandId"]}
    assert {item["runId"] for item in reloaded.json()["items"]} == {failure["runId"]}

    run_detail = await client.get(f"/api/v1/runs/{failure['runId']}", headers=_headers(researcher))
    assert run_detail.status_code == 200
    assert run_detail.json()["run"]["status"] == "failed"
    assert run_detail.json()["run"]["summary"]["assistantMessage"] is None
    # 权威那份仍然完整：用户看得到的每一句都在这里，前端按 retryable 分档渲染。
    persisted_failure = run_detail.json()["run"]["summary"]["failure"]
    assert persisted_failure["code"] == "upstream_rejected"
    assert persisted_failure["retryable"] is False
    assert persisted_failure["title"] and persisted_failure["body"]
    assert persisted_failure["recovery"], "要用户动手的失败必须说得出现在能做什么"
    assert "内部错误" not in json.dumps(persisted_failure, ensure_ascii=False)
    assert run_detail.json()["eventCount"] == 6
    failed_projection = await client.get(
        f"/api/v1/sessions/{conversation_id}/events", headers=_headers(researcher)
    )
    failed_kinds = [event["kind"] for event in failed_projection.json()["items"]]
    assert failed_kinds == [
        "run.started",
        "session.message",
        "step.started",
        "step.failed",
        "session.message",
        "run.failed",
    ]
    failed_session = await client.get(
        f"/api/v1/projects/{project_id}/sessions/{conversation_id}", headers=_headers(researcher)
    )
    assert failed_session.json()["title"] == user_request
    async with factory() as db:
        failed_runs = list(
            (
                await db.execute(
                    select(Run).where(
                        Run.session_id == conversation_id,
                        Run.status == "failed",
                    )
                )
            )
            .scalars()
            .all()
        )
        failed_commands = list(
            (
                await db.execute(
                    select(Command).where(
                        Command.session_id == conversation_id,
                        Command.error.is_not(None),
                    )
                )
            )
            .scalars()
            .all()
        )
        assert [run.id for run in failed_runs] == [failure["runId"]]
        assert [command.id for command in failed_commands] == [failure["commandId"]]
        persisted = json.dumps(
            {
                "run": failed_runs[0].summary,
                "command": failed_commands[0].error,
                "messages": [item["content"] for item in reloaded.json()["items"]],
            }
        )
        assert "sk-super-secret" not in persisted
        assert "/Users/researcher" not in persisted

    retry_response = await client.post(
        f"/api/v1/chat/projects/{project_id}/stream",
        headers=_headers(researcher),
        json={"answer": {"kind": "text", "text": "Retry now"}, "conversation_id": conversation_id},
    )
    retry_events = [
        json.loads(line.removeprefix("data: "))
        for line in retry_response.text.splitlines()
        if line.startswith("data: ")
    ]
    retried = next(event for event in retry_events if event["type"] == "done")
    assert retried["status"] == "completed"
    assert retried["run_id"] != failure["runId"]
    assert retried["command_id"] != failure["commandId"]
    messages_after_retry = await client.get(
        f"/api/v1/projects/{project_id}/sessions/{conversation_id}/messages",
        headers=_headers(researcher),
    )
    # 失败那一轮不再往对话里塞 system 抄件 —— 重发之后读到的是干净的一问一答，
    # 而失败本身在上一条 run 的 summary 和执行事件里，一个字没丢。
    assert [item["role"] for item in messages_after_retry.json()["items"]] == [
        "user",
        "user",
        "assistant",
    ]


async def _a_turn_whose_database_fails_mid_write(
    client: AsyncClient,
    factory,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    bookkeeping_fails_too: bool,
) -> Run:
    """一轮跑完了，收尾写账时库报错 —— 异常落在**开着的事务**里。

    文件库上是 `database is locked`，Postgres 上是断掉的连接；CI run 3685 上是
    序号撞唯一约束。形状都一样：行已经 flush、还没提交，库抛了。
    """
    import sqlite3

    from sqlalchemy.exc import OperationalError

    from app.services.execution_ingest import ExecutionIngestService

    monkeypatch.setattr(settings, "harness_bridge_enabled", True)
    monkeypatch.setattr(settings, "harness_state_root", str(tmp_path))
    researcher = await _token(client, "researcher@atrium.local")
    backend = await client.post(
        "/api/v1/settings/model-backends",
        headers=_headers(researcher),
        json={
            "provider": "openai_compatible",
            "display_name": "Database failure test",
            "model": "test-model",
            "base_url": "http://provider.invalid/v1",
            "api_key": "test-key",
        },
    )
    await client.post(
        f"/api/v1/settings/model-backends/{backend.json()['id']}/default",
        headers=_headers(researcher),
    )
    transcript = tmp_path / "database-failure" / "transcript.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_bytes(b"")

    async def fake_harness_turn(**_kwargs):
        return {
            "status": "completed",
            "run_id": "harness-database-failure",
            "final_text": "The research itself finished.",
            "tokens_used_delta": 1,
            "transcript_path": str(transcript),
            "artifact_paths": [],
            "pause_event": None,
            "pause_pending_path": None,
            "_session_resumable": False,
        }

    monkeypatch.setattr(harness_session_manager, "turn", fake_harness_turn)
    monkeypatch.setattr(harness_session_manager, "paused_binding", lambda *_args: None)

    ingest = ExecutionIngestService.ingest_raw_record
    database_failed = False

    def locked() -> OperationalError:
        return OperationalError(
            "INSERT INTO execution_events", {}, sqlite3.OperationalError("database is locked")
        )

    async def ingest_then_the_database_fails(self, db, **kwargs):
        nonlocal database_failed
        if database_failed and bookkeeping_fails_too:
            raise locked()
        outcome = await ingest(self, db, **kwargs)
        raw = kwargs["raw"]
        if raw.get("event") == "session_message" and raw.get("role") == "assistant":
            database_failed = True
            raise locked()  # 回复那一行已经 flush，事务开着
        return outcome

    monkeypatch.setattr(
        ExecutionIngestService, "ingest_raw_record", ingest_then_the_database_fails
    )
    project_id = (await client.get("/api/v1/projects/", headers=_headers(researcher))).json()[0][
        "id"
    ]
    conversation_id = (
        await client.post(
            f"/api/v1/projects/{project_id}/sessions",
            headers=_headers(researcher),
            json={"title": "Database failure"},
        )
    ).json()["id"]
    response = await client.post(
        f"/api/v1/chat/projects/{project_id}/stream",
        headers=_headers(researcher),
        json={
            "answer": {"kind": "text", "text": "Finish, then fail"},
            "conversation_id": conversation_id,
        },
    )
    assert response.status_code == 200
    assert database_failed, "库没报错 —— 这条测试没走到它要验的那条路"
    # 流在 `error` 帧上收，而那一帧在 execute_local_turn 的失败处理（含兜底）
    # 跑完之后才发 —— 这里读到的就是它留下的终态，不用等。
    assert '"type": "error"' in response.text
    async with factory() as observer:
        return await observer.scalar(select(Run).where(Run.session_id == conversation_id))


@pytest.mark.asyncio
async def test_a_database_error_inside_an_open_transaction_is_recorded_as_a_failure(
    runtime_client, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog
) -> None:
    """收尾写账撞上库错误：fail-loud 记账自己把 run 记成 failed，不掉进兜底。

    回滚让 `command` 过期；从前失败处理读 `command.id` → MissingGreenlet，
    记账没开始就炸了（CI run 3685 的第二跳）。
    """
    client, factory = runtime_client
    run = await _a_turn_whose_database_fails_mid_write(
        client, factory, monkeypatch, tmp_path, bookkeeping_fails_too=False
    )
    assert run is not None and run.status == "failed"
    assert "note" not in (run.summary or {}), "终态是兜底写的 —— fail-loud 记账没走完"
    assert run.summary["failure"]["title"]
    assert "MissingGreenlet" not in caplog.text
    assert "Could not record a terminal state" not in caplog.text
    async with factory() as observer:
        kinds = list(
            (
                await observer.scalars(
                    select(ExecutionEvent.kind)
                    .where(ExecutionEvent.run_id == run.id)
                    .order_by(ExecutionEvent.sequence)
                )
            ).all()
        )
    assert kinds[-1] == "run.failed"


@pytest.mark.asyncio
async def test_when_bookkeeping_itself_fails_the_rescue_still_writes_a_terminal_state(
    runtime_client, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog
) -> None:
    """记账自身也失败 → 兜底用独立连接写终态，而且经 D11 的漏斗写。

    兜底从前直接 `dead.status = …`：D11 的运行时闸（2026-08-23）之后这一句每次
    都 RunStatusWriteError，"Could not record a terminal state"，run 停在
    running（CI run 3685 的第三跳）。
    """
    client, factory = runtime_client
    run = await _a_turn_whose_database_fails_mid_write(
        client, factory, monkeypatch, tmp_path, bookkeeping_fails_too=True
    )
    assert run is not None and run.status == "failed"
    assert run.summary["note"] == "fail-loud 记账自身失败，状态由兜底路径写入"
    assert run.summary["failure"]["title"]
    written = run.summary["statusLog"][-1]
    assert (written["to"], written["source"], written["evidence"]["path"]) == (
        "failed",
        "transport_failure",
        "rescue",
    )
    assert "Could not record a terminal state" not in caplog.text


@pytest.mark.asyncio
async def test_lost_kept_alive_pause_becomes_stale_unknown(runtime_client) -> None:
    client, factory = runtime_client
    async with factory() as db:
        project = await db.scalar(select(Project).where(Project.name == "Researcher Project"))
        user = await db.scalar(select(User).where(User.email == "researcher@atrium.local"))
        backend = await db.scalar(
            select(ModelBackendConfig).where(
                ModelBackendConfig.scope_kind == "institution",
                ModelBackendConfig.provider == "demo",
            )
        )
        conversation_id = uuid4().hex
        session = SessionProjection(
            tenant_id=settings.runtime_tenant_id,
            workspace_id=GROUP,
            project_id=str(project.id),
            session_id=conversation_id,
            initiating_user_id=user.id,
            title="Lost pause",
            git_base_commit_sha="0" * 40,
            created_by_user_id=user.id,
            model_backend_id=backend.id,
            policy_snapshot_id="policy-snapshot-1",
            research_settings_snapshot_id="research-snapshot-1",
            research_settings_snapshot={"snapshot_id": "research-snapshot-1"},
            knowledge_read_watermark={"claim_sequence": 17},
        )
        run = Run(
            id="run_stale_pause",
            tenant_id=settings.runtime_tenant_id,
            workspace_id=GROUP,
            project_id=str(project.id),
            session_id=session.session_id,
            status="waiting_human",
            summary={"executionKernel": "formal_harness"},
        )
        attempt = RunAttempt(
            tenant_id=settings.runtime_tenant_id,
            workspace_id=GROUP,
            project_id=str(project.id),
            session_id=session.session_id,
            run_id=run.id,
            attempt_no=1,
            status="running",
        )
        db.add_all([session, run, attempt])
        await db.flush()
        await db.commit()

        assert await mark_orphaned_harness_runs(db) == 1
        await db.commit()
        await db.refresh(run)
        await db.refresh(attempt)
        # D11：判决不落盘 —— status 归投影器，平台只留见证。
        # attempt 的终态照旧写：那一**次尝试**确实结束了，是事实不是猜测。
        # `resumable` 一个字不写（2026-08-24：存储判决误伤活 pause，已删）。
        assert run.summary["runtimeWitness"][-1]["reason"] == "app_server_restart"
        assert run.summary["staleReason"] == "app_server_restart"
        assert runtime_lost(run, has_live_binding=False) is True
        assert attempt.status == "stale_unknown"
        with pytest.raises(HarnessSessionStaleError):
            await assert_conversation_runtime_available(
                db,
                project_id=str(project.id),
                conversation_id=session.session_id,
                user_id=user.id,
            )

    researcher = await _token(client, "researcher@atrium.local")

    # Session 没有「关了」这个状态：运行时丢了，发消息照样成 —— 死掉的是那个
    # 挂起的**询问**，不是会话。对话在磁盘上（messages_checkpoint.json），新
    # worker 起来时自己会读回去。
    #
    # 这里原来断言 409 + "只能开新 Session"。而开新 Session 会铸新 session_id，
    # state root 按 session_id 拼，于是新 worker 去空目录找 checkpoint ——
    # 2026-08-13 实测丢了 15 条消息 / 152KB 的上下文。
    continued = await client.post(
        f"/api/v1/chat/projects/{project.id}/stream",
        headers=_headers(researcher),
        json={"answer": {"kind": "text", "text": "继续"}, "conversation_id": conversation_id},
    )
    assert continued.status_code == 200, continued.text
    # 作废必须说出来 —— 用户正看着那个问题，以为自己答上了。
    assert "pause.abandoned" in continued.text, continued.text[:800]

    async with factory() as db:
        stale = await db.get(Run, run.id)
        assert stale.summary.get("pauseAbandonedAt"), "作废没留痕"

    # 「开新 Session」仍然是一个合法选择 —— 人主动甩掉一段上下文时用它。
    recover_href = f"/api/v1/projects/{project.id}/sessions/{conversation_id}/recover"
    recovered_response = await client.post(recover_href, headers=_headers(researcher))
    assert recovered_response.status_code == 201
    recovered_payload = recovered_response.json()
    assert recovered_payload["status"] == "created"
    assert recovered_payload["action"] == "start_new_session"
    assert recovered_payload["reason"] == "lost_runtime_state"
    assert recovered_payload["sourceSessionId"] == conversation_id
    assert recovered_payload["sourceRunId"] == run.id
    assert "external side effects" in recovered_payload["suggestedMessage"]
    recovered = recovered_payload["session"]
    assert recovered["id"] != conversation_id
    assert recovered["recoveredFromSessionId"] == conversation_id
    assert recovered["recoverySourceRunId"] == run.id
    assert recovered["baseCommitSha"], "恢复出来的会话要有自己的 git 基线"
    assert recovered["modelBackendId"] == backend.id

    repeated = await client.post(recover_href, headers=_headers(researcher))
    assert repeated.status_code == 200
    assert repeated.json()["status"] == "existing"
    assert repeated.json()["session"]["id"] == recovered["id"]

    async with factory() as db:
        source = await db.get(SessionProjection, conversation_id)
        continuation = await db.get(SessionProjection, recovered["id"])
        assert continuation.policy_snapshot_id == source.policy_snapshot_id
        assert continuation.research_settings_snapshot == source.research_settings_snapshot
        assert continuation.knowledge_read_watermark == source.knowledge_read_watermark
        # 接着上一轮 = 从**源会话停下的地方**长出去（见 recover_stale_session 里
        # 那段注释：从项目基线重开会把几小时的 checkpoint 丢掉）。从前这一条查的
        # 是两个会话的 ChangeSet 共用 base_revision_id —— 同一件事的投影，而且
        # 投影记的还是"当初的起点"，不是"停下的地方"。
        assert continuation.git_base_commit_sha == (
            source.git_head_commit_sha or continuation.git_base_commit_sha
        ), "接续会话要从源会话停下的地方长出去；源会话没跑过就从项目当前 head"
        assert (
            await db.scalar(
                select(func.count())
                .select_from(SessionProjection)
                .where(SessionProjection.recovered_from_session_id == source.session_id)
            )
            == 1
        )


@pytest.mark.asyncio
async def test_recovery_anchors_on_the_run_that_lost_its_runtime_not_the_newest(
    runtime_client,
) -> None:
    """血缘锚点认「平台记下运行时丢了」的那条 run，不认「最近更新的那一行」。

    ## 现场（2026-08-21）

    `test_lost_kept_alive_pause_becomes_stale_unknown` 在 CI 上间歇红：

        assert 'run_f1649990dbcf4ae8b2d8eb78f4cb1e6d' == 'run_stale_pause'

    同一棵树 PR run 绿、push run 红（`6537bfb1` vs `33b79516`，`git diff` 为空）。
    病根是 `recover_stale_session` 里一个 `source_run = 最近更新的那一行` 同时
    回答了两个问题：「这会话还能不能驱动」（该看当前状态）和「我们在接续哪个
    丢掉的 run」（该看丢失事实）。pause 作废后用户接着发消息，同一 session 下
    就多出更新的 run，于是锚点被它抢走；两条 run 的 `updated_at` 落同一刻度时
    才靠 `Run.id` 字典序兜底，所以平时是绿的。

    ## 这条测试为什么是确定性的

    它**显式**把普通 failed 的那条造得更新（`updated_at` 严格更大），
    把原来那条测试要靠运气才撞上的情形固定下来。判据是平台自己写下的事实
    `staleReason` —— 普通失败走 `terminal_state_for` 的最后一支，返回
    `(failed, None)`，**不写**它。
    """
    client, factory = runtime_client
    async with factory() as db:
        project = await db.scalar(select(Project).where(Project.name == "Researcher Project"))
        user = await db.scalar(select(User).where(User.email == "researcher@atrium.local"))
        backend = await db.scalar(
            select(ModelBackendConfig).where(
                ModelBackendConfig.scope_kind == "institution",
                ModelBackendConfig.provider == "demo",
            )
        )
        conversation_id = uuid4().hex
        session = SessionProjection(
            tenant_id=settings.runtime_tenant_id,
            workspace_id=GROUP,
            project_id=str(project.id),
            session_id=conversation_id,
            initiating_user_id=user.id,
            title="Anchor",
            git_base_commit_sha="0" * 40,
            created_by_user_id=user.id,
            model_backend_id=backend.id,
        )
        lost = Run(
            id="run_lost_runtime",
            tenant_id=settings.runtime_tenant_id,
            workspace_id=GROUP,
            project_id=str(project.id),
            session_id=session.session_id,
            status="stale_unknown",
            summary={
                "executionKernel": "formal_harness",
                "staleReason": "app_server_restart",
                "resumable": False,
            },
        )
        # 之后那次续跑：普通失败，**没有** staleReason，而且更新时间更晚。
        newer = Run(
            id="run_after_the_loss",
            tenant_id=settings.runtime_tenant_id,
            workspace_id=GROUP,
            project_id=str(project.id),
            session_id=session.session_id,
            status="failed",
            summary={"executionKernel": "formal_harness"},
        )
        db.add_all([session, lost, newer])
        await db.flush()
        # 把"更新"这件事钉死，不靠写入顺序碰运气。
        lost.updated_at = datetime(2026, 8, 21, 10, 0, tzinfo=UTC)
        newer.updated_at = datetime(2026, 8, 21, 11, 0, tzinfo=UTC)
        await db.commit()

    researcher = await _token(client, "researcher@atrium.local")
    response = await client.post(
        f"/api/v1/projects/{project.id}/sessions/{conversation_id}/recover",
        headers=_headers(researcher),
    )
    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload["sourceRunId"] == "run_lost_runtime", (
        "锚点被最近更新的那一行抢走了 —— 它回答的是另一个问题"
    )
    assert payload["session"]["recoverySourceRunId"] == "run_lost_runtime"


@pytest.mark.asyncio
async def test_kept_alive_manager_turn_answer_and_terminate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    harness_root = tmp_path / "serve-harness"
    # stdio 是一种**配置出来的模式**，不再是「连不上命令面就回退」——
    # 这个桩读 stdin，所以它要的就是 stdio，明说出来（以前靠回退才走到这条路）。
    monkeypatch.setattr(settings, "harness_control_socket", False)
    (harness_root / "core").mkdir(parents=True)
    (harness_root / "core" / "__init__.py").write_text("", encoding="utf-8")
    (harness_root / "core" / "agent_loop.py").write_text("", encoding="utf-8")
    (harness_root / "platform_runtime.py").write_text(
        textwrap.dedent(
            """
            import json
            import os
            import sys
            import time
            from pathlib import Path

            transcript = None
            for line in sys.stdin:
                request = json.loads(line)
                request_id = request["request_id"]
                if request["op"] == "init":
                    starts = Path(request["state_dir"]) / "starts.txt"
                    starts.parent.mkdir(parents=True, exist_ok=True)
                    with starts.open("a", encoding="utf-8") as stream:
                        stream.write(f"{os.getpid()}\\n")
                    (Path(request["state_dir"]) / "platform-context.json").write_text(
                        json.dumps(request.get("platform_context")),
                        encoding="utf-8",
                    )
                    transcript = (
                        Path(request["state_dir"])
                        / "orchestrator__project-serve"
                        / "transcript.jsonl"
                    )
                    transcript.parent.mkdir(parents=True, exist_ok=True)
                    transcript.write_text("", encoding="utf-8")
                    print(json.dumps({
                        "type": "ready", "request_id": request_id,
                        "run_id": "orchestrator__project-serve",
                        "sandbox_protocol_version": 2,
                    }), flush=True)
                elif request["op"] == "turn":
                    if request["message"] == "slow":
                        time.sleep(0.10)
                        print(json.dumps({"type": "result", "request_id": request_id, "data": {
                            "status": "completed", "run_id": "orchestrator__project-serve",
                            "final_text": "slow turn completed", "tokens_used": 5,
                            "transcript_path": str(transcript), "artifact_paths": [],
                            "pause_event": None, "pause_id": None, "pause_pending_path": None,
                        }}), flush=True)
                        continue
                    print(json.dumps({
                        "type": "started", "request_id": request_id,
                        "run_id": "orchestrator__project-serve",
                    }), flush=True)
                    print(json.dumps({
                        "type": "transcript", "request_id": request_id,
                        "event": {"event": "turn_start"},
                    }), flush=True)
                    print(json.dumps({
                        "type": "child_event", "request_id": request_id,
                        "child_run_id": "child-serve", "detail": "internal child lifecycle",
                    }), flush=True)
                    print(json.dumps({
                        "type": "progress", "request_id": request_id,
                        "tool_name": "arxiv_search", "message": "Searching arXiv",
                    }), flush=True)
                    print(json.dumps({
                        "type": "progress", "request_id": request_id,
                        "tool_name": "arxiv_search", "message": "Reviewing search results",
                    }), flush=True)
                    print(json.dumps({
                        "type": "background_wait", "request_id": request_id,
                        "job_id": "job-serve", "message": "Waiting for the compute job",
                    }), flush=True)
                    print(json.dumps({
                        "type": "pause_required", "request_id": request_id,
                        "pause_id": "pause-1", "question": "Choose scope",
                    }), flush=True)
                    print(json.dumps({"type": "result", "request_id": request_id, "data": {
                        "status": "paused", "run_id": "orchestrator__project-serve",
                        "final_text": "", "tokens_used": 2,
                        "transcript_path": str(transcript), "artifact_paths": [],
                        "pause_event": {
                            "question": "Choose scope",
                            "pending_tool_call_id": "pause-1",
                        },
                        "pause_id": "pause-1",
                        "pause_pending_path": None,
                    }}), flush=True)
                elif request["op"] == "answer":
                    answers = transcript.parents[1] / "answer-pause-ids.txt"
                    with answers.open("a", encoding="utf-8") as stream:
                        stream.write(f"{request.get('pause_id', '')}\\n")
                    expected = "pause-2" if request["answer"] == "finish" else "pause-1"
                    if request.get("pause_id") != expected:
                        print(json.dumps({
                            "type": "error", "request_id": request_id,
                            "code": "pause_conflict",
                            "message": "pause_id does not match current pause",
                        }), flush=True)
                    elif request["answer"] == "again":
                        print(json.dumps({"type": "result", "request_id": request_id, "data": {
                            "status": "paused", "run_id": "orchestrator__project-serve",
                            "final_text": "", "tokens_used": 3,
                            "transcript_path": str(transcript), "artifact_paths": [],
                            "pause_event": {
                                "question": "Confirm",
                                "pending_tool_call_id": "pause-2",
                            },
                            "pause_id": "pause-2", "pause_pending_path": None,
                        }}), flush=True)
                    else:
                        print(json.dumps({"type": "result", "request_id": request_id, "data": {
                            "status": "completed", "run_id": "orchestrator__project-serve",
                            "final_text": "resumed", "tokens_used": 4,
                            "transcript_path": str(transcript), "artifact_paths": [],
                            "pause_event": None, "pause_id": None, "pause_pending_path": None,
                        }}), flush=True)
                elif request["op"] == "terminate":
                    print(json.dumps({"type": "terminated", "request_id": request_id}), flush=True)
                    break
            """
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(settings, "harness_bridge_enabled", True)
    monkeypatch.setattr(settings, "harness_root", str(harness_root))
    monkeypatch.setattr(settings, "harness_python", sys.executable)
    monkeypatch.setattr(settings, "harness_state_root", str(tmp_path / "serve-state"))
    backend = ModelBackendConfig(
        id="backend-serve",
        scope_kind="personal",
        scope_id="user-serve",
        provider="openai_compatible",
        display_name="Serve bridge",
        model="fake-model",
        base_url="http://provider.invalid/v1",
        credential_source="encrypted",
        encrypted_api_key=encrypt_api_key("serve-secret"),
        created_by_user_id="user-serve",
    )
    user = User(
        id="user-serve",
        email="serve@example.test",
        hashed_password="unused",
        institution_id="institution-1",
        institution_name="Institution",
    )
    manager = HarnessSessionManager()
    first_worktree = await _initialize_manager_worktree(
        user=user,
        project_id="project-serve",
        session_id="conversation-serve",
    )
    # 绑定漂移的载体：平台上下文。从前这里用 `research_profile_snapshot` ——
    # 那个字段 `platform_runtime` 一个字都不读，拿它当载体，等于用一个死字段
    # 去测一条活机制（RFC X3 删了它）。指令**不再**是绑定的一部分：worker 每轮
    # 自己读文件，改了下一轮生效，不需要换进程。
    platform_context = {"version": 1, "baseCommitSha": "a" * 40}
    protocol: list[dict] = []
    progress: list[dict] = []

    async def record_protocol(event: dict) -> None:
        protocol.append(event)

    async def record_progress(event: dict) -> None:
        progress.append(event)

    paused = await manager.turn(
        user=user,
        project_id="project-serve",
        conversation_id="conversation-serve",
        run_id="run-serve",
        session_id="conversation-serve",
        message="start",
        role_bindings={"reasoning": backend},
        platform_context_snapshot=platform_context,
        on_progress=record_progress,
        on_protocol_event=record_protocol,
    )
    assert paused["status"] == "paused"
    assert paused["_session_resumable"] is True
    assert manager.paused_binding("project-serve", "conversation-serve").run_id == "run-serve"
    assert [event["type"] for event in protocol] == [
        "ready",
        "started",
        "transcript",
        "child_event",
        "progress",
        "progress",
        "background_wait",
        "pause_required",
        "result",
    ]
    assert [event["event"] for event in progress] == [
        "tool.progress",
        "tool.progress",
        "run.waiting_compute",
        "run.paused",
    ]
    assert progress[0]["id"] == progress[1]["id"]
    assert len({event["id"] for event in progress}) == 3
    context_file = (
        first_worktree / ".research" / "runtime" / "runs" / "platform-context.json"
    )
    assert json.loads(context_file.read_text(encoding="utf-8")) == platform_context
    # 绑定漂移不再是死角：停靠中的 worker 被换掉、从 checkpoint 接续，新快照
    # **真正生效**。旧 pause 随旧进程退场，由 stale-binding 钩子立刻标成可恢复
    # —— 和登出同一条处理路（2026-08-21 之前这里 raise，用户只能撞
    # "平台内部错误"，登出再登录反而能好）。
    stale_bindings: list = []

    async def _record_stale(binding) -> None:
        stale_bindings.append(binding)

    manager.set_stale_binding_handler(_record_stale)
    drifted_context = {**platform_context, "baseCommitSha": "b" * 40}
    drift = await manager.turn(
        user=user,
        project_id="project-serve",
        conversation_id="conversation-serve",
        run_id="run-serve-drift",
        session_id="conversation-serve",
        message="drift",
        role_bindings={"reasoning": backend},
        platform_context_snapshot=drifted_context,
        on_progress=lambda _event: _noop_async(),
        on_protocol_event=lambda _event: _noop_async(),
    )
    assert drift["status"] == "paused"
    # 被送走的正是原来停靠的那条 run —— 钩子拿到它才能标 orphaned。
    assert [binding.run_id for binding in stale_bindings] == ["run-serve"]
    assert (
        manager.paused_binding("project-serve", "conversation-serve").run_id
        == "run-serve-drift"
    )
    # respawn 的意义：新 worker 吃到的是**漂移后的**快照，记录与执行一致。
    assert json.loads(context_file.read_text(encoding="utf-8")) == drifted_context

    paused_again = await manager.answer(
        user=user,
        project_id="project-serve",
        conversation_id="conversation-serve",
        run_id="run-serve-drift",
        session_id="conversation-serve",
        answer="again",
        on_progress=lambda _event: _noop_async(),
        on_protocol_event=lambda _event: _noop_async(),
    )
    assert paused_again["status"] == "paused"
    session = manager._sessions[manager._key("project-serve", "conversation-serve")]
    assert session.pause_id == "pause-2"

    session.pause_id = "pause-1"
    with pytest.raises(HarnessSessionError, match="pause_id does not match"):
        await manager.answer(
            user=user,
            project_id="project-serve",
            conversation_id="conversation-serve",
            run_id="run-serve-drift",
            session_id="conversation-serve",
            answer="finish",
            on_progress=lambda _event: _noop_async(),
            on_protocol_event=lambda _event: _noop_async(),
        )
    session.pause_id = "pause-2"
    completed = await manager.answer(
        user=user,
        project_id="project-serve",
        conversation_id="conversation-serve",
        run_id="run-serve-drift",
        session_id="conversation-serve",
        answer="finish",
        on_progress=lambda _event: _noop_async(),
        on_protocol_event=lambda _event: _noop_async(),
    )
    assert completed["status"] == "completed"
    assert manager.paused_binding("project-serve", "conversation-serve") is None
    assert session.pause_id is None
    assert manager.active_count == 1

    # The control-plane timeout must never become a wall-clock limit for a
    # scientific turn. This fake turn is deliberately slower than the patched
    # control deadline and must still reach its terminal result.
    monkeypatch.setattr(settings, "harness_timeout_seconds", 0.02)
    slow = await manager.turn(
        user=user,
        project_id="project-serve",
        conversation_id="conversation-serve",
        run_id="run-serve-slow",
        session_id="conversation-serve",
        message="slow",
        role_bindings={"reasoning": backend},
        # 带**当前**绑定（漂移后的那份）——这里测的是"控制面超时不封顶科学
        # 轮"，要 reuse 活 worker；带旧快照会构成又一次绑定漂移 → respawn。
        platform_context_snapshot=drifted_context,
        on_progress=lambda _event: _noop_async(),
        on_protocol_event=lambda _event: _noop_async(),
    )
    assert slow["status"] == "completed"
    assert slow["final_text"] == "slow turn completed"
    assert session.alive is True
    monkeypatch.setattr(settings, "harness_timeout_seconds", 900)

    second_user = User(
        id="user-serve-2",
        email="serve-2@example.test",
        hashed_password="unused",
        institution_id="institution-1",
        institution_name="Institution",
    )
    second_worktree = await _initialize_manager_worktree(
        user=second_user,
        project_id="project-serve",
        session_id="conversation-serve-2",
    )
    second_turn = await manager.turn(
        user=second_user,
        project_id="project-serve",
        conversation_id="conversation-serve-2",
        run_id="run-serve-2",
        session_id="conversation-serve-2",
        message="start",
        role_bindings={"reasoning": backend},
        on_progress=lambda _event: _noop_async(),
        on_protocol_event=lambda _event: _noop_async(),
    )
    assert second_turn["status"] == "paused"
    first_starts = first_worktree / ".research" / "runtime" / "runs" / "starts.txt"
    second_starts = second_worktree / ".research" / "runtime" / "runs" / "starts.txt"
    first_pids = first_starts.read_text(encoding="utf-8").splitlines()
    second_pids = second_starts.read_text(encoding="utf-8").splitlines()
    # 2 = 首次 spawn + 绑定漂移那次 respawn —— 这行就是"respawn 真的发生了"
    # 的机械证据。
    assert len(first_pids) == 2
    assert len(second_pids) == 1
    assert first_pids != second_pids
    assert manager.active_count == 2
    await manager.terminate_all()
    assert manager.active_count == 0


@pytest.mark.asyncio
async def test_kept_alive_manager_surfaces_subprocess_exit_stderr(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    harness_root = tmp_path / "missing-runtime-harness"
    # stdio 是一种**配置出来的模式**，不再是「连不上命令面就回退」——
    # 这个桩读 stdin，所以它要的就是 stdio，明说出来（以前靠回退才走到这条路）。
    monkeypatch.setattr(settings, "harness_control_socket", False)
    (harness_root / "core").mkdir(parents=True)
    (harness_root / "core" / "agent_loop.py").write_text("", encoding="utf-8")
    monkeypatch.setattr(settings, "harness_bridge_enabled", True)
    monkeypatch.setattr(settings, "harness_root", str(harness_root))
    monkeypatch.setattr(settings, "harness_python", sys.executable)
    monkeypatch.setattr(settings, "harness_state_root", str(tmp_path / "missing-runtime-state"))
    backend = ModelBackendConfig(
        id="backend-missing-runtime",
        scope_kind="personal",
        scope_id="user-missing-runtime",
        provider="openai_compatible",
        display_name="Missing runtime bridge",
        model="fake-model",
        base_url="http://provider.invalid/v1",
        credential_source="encrypted",
        encrypted_api_key=encrypt_api_key("must-not-leak"),
        created_by_user_id="user-missing-runtime",
    )
    user = User(
        id="user-missing-runtime",
        email="missing-runtime@example.test",
        hashed_password="unused",
        institution_id="institution-1",
        institution_name="Institution",
    )
    manager = HarnessSessionManager()
    await _initialize_manager_worktree(
        user=user,
        project_id="project-missing-runtime",
        session_id="conversation-missing-runtime",
    )

    with pytest.raises(HarnessSessionProcessError) as captured:
        await manager.turn(
            user=user,
            project_id="project-missing-runtime",
            conversation_id="conversation-missing-runtime",
            run_id="run-missing-runtime",
            session_id="conversation-missing-runtime",
            message="one sentence, no tools",
            role_bindings={"reasoning": backend},
            on_progress=lambda _event: _noop_async(),
            on_protocol_event=lambda _event: _noop_async(),
        )

    assert captured.value.code == "harness_process_exited"
    assert captured.value.exit_code != 0
    assert "No module named platform_runtime" in str(captured.value)
    assert "must-not-leak" not in str(captured.value)
    assert manager.active_count == 0


async def _noop_async() -> None:
    return None


@pytest.mark.asyncio
async def test_answering_a_pause_always_leaves_a_terminal_run(
    runtime_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """回答 pause 的路上炸了，run 也必须落终态 —— 不能停在 waiting_human。

    ## 现场（2026-08-10 E2E v25）

    无人值守批准了一次高危提交，之后 LLM 后端回 503，harness 会话结束：

        run 状态              waiting_human / running
        最后一条执行事件       run.resumed（17:54）
        之后                  **一个终态事件都没有**，停摆 2 小时 25 分

    `execute_local_turn` 的 `except` 里，`is_answer` 分支原本只在
    `HarnessSessionStaleError` 时记账，别的异常直接 raise；而下面那整段
    fail-loud 记账 `is_answer` 走不到。**"回答 pause"正是无人值守审批流最常走
    的那条路** —— 覆盖漏掉的恰好是最没人看着的那条。

    ## 为什么这条测试在这里、且是行为测试

    它原来在 `test_every_failure_path_leaves_a_terminal_run.py` 里，用
    `inspect.getsource()` 断言分支里有某几行字面量。那种写法保持行为不变的重构
    会打红（2026-08-12 就是：函数多了个 `run_id=` 参数），而把代码改成字面相似
    但语义损坏的样子反而照样绿 —— 复刻判据等于自己给自己打分。

    这里走真实 HTTP 入口、真实 `execute_local_turn`，只看**库里那条 run 最后
    是什么**。实现怎么写都行，结果对就行。
    """
    from app.services.harness_sessions import HarnessSessionStaleError

    client, factory = runtime_client
    researcher = await _token(client, "researcher@atrium.local")

    async def _seed(run_id: str) -> tuple[str, str]:
        async with factory() as db:
            project = await db.scalar(select(Project).where(Project.name == "Researcher Project"))
            user = await db.scalar(select(User).where(User.email == "researcher@atrium.local"))
            conversation_id = uuid4().hex
            session = SessionProjection(
                tenant_id=settings.runtime_tenant_id, workspace_id=GROUP,
                project_id=str(project.id), session_id=conversation_id,
                initiating_user_id=user.id,
            )
            db.add(session)
            await db.flush()
            db.add_all([
                Run(
                    id=run_id, tenant_id=settings.runtime_tenant_id, workspace_id=GROUP,
                    project_id=str(project.id), session_id=session.session_id,
                    status="waiting_human", summary={"executionKernel": "formal_harness"},
                ),
                RunAttempt(
                    tenant_id=settings.runtime_tenant_id, workspace_id=GROUP,
                    project_id=str(project.id), session_id=session.session_id,
                    run_id=run_id, attempt_no=1, status="running",
                ),
            ])
            await db.commit()
            return str(project.id), session.session_id

    async def _answer_and_explode(run_id: str, exc: Exception) -> Run:
        project_id, session_id = await _seed(run_id)
        binding = AppRunBinding(
            (await _user_id(factory)), session_id, run_id, session_id
        )
        monkeypatch.setattr(
            harness_session_manager, "paused_binding",
            lambda pid, sid: binding if sid == session_id else None,
        )

        async def explode(*_args, **_kwargs):
            raise exc

        monkeypatch.setattr(harness_session_manager, "answer", explode)
        await client.post(
            f"/api/v1/chat/projects/{project_id}/stream",
            headers=_headers(researcher),
            json={"answer": {"kind": "text", "text": "approve"}, "conversation_id": session_id},
        )
        async with factory() as db:
            return await db.get(Run, run_id)

    # 非 stale（v25 那次 503 的形状）→ failed
    run = await _answer_and_explode("run_answer_boom", RuntimeError("LLM API HTTP 503: nope"))
    assert run.status not in {s.value for s in REQUIRES_LIVE_RUNTIME_STATUSES}, (
        f"run 停在 {run.status} —— 这个状态声称有活进程，而进程已经没了"
    )
    assert run.status == "failed"
    assert "resumable" not in run.summary, "resumable 是关于未来的判决，不该落盘"
    assert run.summary["failure"]["title"], "只标终态不留原因 = 把排查推回人工翻日志"
    assert run.summary["failure"]["reference"] == run.id
    assert "HTTP 503" not in run.summary["failure"]["message"], "正文不该带异常原文"
    assert "HTTP 503" in run.summary["failure"]["detail"], "细节该留的没留"

    # 进程真没了 → stale_unknown（可恢复），不是 failed（不可恢复）
    stale_run = await _answer_and_explode(
        "run_answer_stale", HarnessSessionStaleError("process is gone")
    )
    assert stale_run.status == "stale_unknown"


async def _user_id(factory) -> str:
    async with factory() as db:
        user = await db.scalar(select(User).where(User.email == "researcher@atrium.local"))
        return user.id


@pytest.mark.asyncio
async def test_a_restart_leaves_the_run_recoverable_not_failed(
    runtime_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """重启 App Server 打断的 run 落**可恢复态**，不是 failed。

    2026-08-12 实测：重启一次，在跑的 run 变成
    `failed` + "Harness runtime process exited before replying (exit code -15)"，
    UI 建议用户"改改请求再试"。而 `-15` 是 SIGTERM，**我们自己发的** ——
    研究一点问题没有。

    「这次研究失败了」和「平台重启打断了它」是两件事：终态不同（一个不可恢复
    一个可恢复），给用户的话也不同。
    """
    from app.services import lifecycle
    from app.services.harness_sessions import HarnessSessionProcessError

    client, factory = runtime_client
    researcher = await _token(client, "researcher@atrium.local")
    run_id = "run_restart_interrupted"

    async with factory() as db:
        project = await db.scalar(select(Project).where(Project.name == "Researcher Project"))
        user = await db.scalar(select(User).where(User.email == "researcher@atrium.local"))
        conversation_id = uuid4().hex
        session = SessionProjection(
            tenant_id=settings.runtime_tenant_id, workspace_id=GROUP,
            project_id=str(project.id), session_id=conversation_id,
            initiating_user_id=user.id,
        )
        db.add(session)
        await db.flush()
        db.add_all([
            Run(
                id=run_id, tenant_id=settings.runtime_tenant_id, workspace_id=GROUP,
                project_id=str(project.id), session_id=session.session_id,
                status="waiting_human", summary={"executionKernel": "formal_harness"},
            ),
            RunAttempt(
                tenant_id=settings.runtime_tenant_id, workspace_id=GROUP,
                project_id=str(project.id), session_id=session.session_id,
                run_id=run_id, attempt_no=1, status="running",
            ),
        ])
        await db.commit()
        project_id, session_id, user_id = str(project.id), session.session_id, user.id

    binding = AppRunBinding(user_id, session_id, run_id, session_id)
    monkeypatch.setattr(
        harness_session_manager, "paused_binding",
        lambda pid, sid: binding if sid == session_id else None,
    )

    async def killed_by_our_own_restart(*_args, **_kwargs):
        raise HarnessSessionProcessError(
            "Harness runtime process exited before replying (exit code -15)", exit_code=-15
        )

    monkeypatch.setattr(harness_session_manager, "answer", killed_by_our_own_restart)

    lifecycle.begin_shutdown()          # ← 正在关机，这才是区分点
    try:
        await client.post(
            f"/api/v1/chat/projects/{project_id}/stream",
            headers=_headers(researcher),
            json={"answer": {"kind": "text", "text": "approve"}, "conversation_id": session_id},
        )
    finally:
        lifecycle.reset_for_tests()

    async with factory() as db:
        run = await db.get(Run, run_id)
    # 不变量不变：重启打断**不能**落成研究失败的终态。D11 之后它连 status 都
    # 不改了 —— 事实保持原样，"运行时没了"由 run_liveness 现算。
    assert run.status not in {"failed", "incomplete"}, (
        f"重启打断落成了 {run.status} —— 那是「研究失败」的终态，会让用户去改一件没错的东西"
    )
    assert run.summary["staleReason"] == "app_server_shutdown"
    failure = run.summary["failure"]
    assert failure["code"] == "app_server_restarted"
    assert "revise" not in failure["recovery"].lower()


@pytest.mark.asyncio
async def test_a_child_run_id_can_be_fetched_through_the_api(runtime_client) -> None:
    """子节点 run 的 id 必须能原样出现在 URL 里取回来。

    ## 现场（2026-08-12）

    为了保证全局唯一，我把子 run id 写成 `<父>/<子>`。而这个 id 要在 URL 里
    旅行：

        GET /api/v1/runs/run_93acf64a…/_orchestrator->hypothesis@d1 → 404

    `/` 是**路径分隔符** —— 路由把 id 切成两段，取详情永远 404（百分号编码也
    救不回来）。前端拿不到 run detail，UI 上就是「Execution record unavailable」。

    「保证唯一性的分隔符」和「URL 的路径分隔符」是两件事，用同一个字符就等于
    让它们互相破坏。

    ## 这条测试**不能**自己拼那个 id

    第一版我写的是 `child_id = f"{parent}::{sub_run_id}"` —— 把格式串在测试里
    复刻一遍。结果把实现改回 `/`，测试照样绿：它测的是自己写的字符串，不是
    平台生成的那个。变异验证抓到了这一点。

    现在走**真实摄取**造出子节点 run，再把库里落下来的那个 id 拿去发请求。
    实现怎么改，测试都跟着它走。
    """
    import json as _json

    from app.config import settings
    from app.models.execution import ExecutionEvent, Run, RunStatus, SessionProjection
    from app.services.execution_ingest import ExecutionIngestService, IngestContext

    client, factory = runtime_client
    researcher = await _token(client, "researcher@atrium.local")
    parent_id = "run_url_probe"

    async with factory() as db:
        project = await db.scalar(select(Project).where(Project.name == "Researcher Project"))
        user = await db.scalar(select(User).where(User.email == "researcher@atrium.local"))
        conversation_id = uuid4().hex
        session_id = conversation_id
        db.add(SessionProjection(
            tenant_id=settings.runtime_tenant_id, workspace_id=GROUP,
            project_id=str(project.id), session_id=session_id,
            initiating_user_id=user.id,
        ))
        db.add(Run(
            id=parent_id, tenant_id=settings.runtime_tenant_id, workspace_id=GROUP,
            project_id=str(project.id), session_id=session_id, parent_run_id=None,
            status=RunStatus.RUNNING.value, summary={},
        ))
        await db.flush()

        # ↓ 真实摄取：子节点 run 的 id 由**平台**生成，测试不参与拼装。
        raw = {
            "event": "run_start", "at": "2026-08-12T10:00:00+00:00",
            "node_type": "hypothesis", "depth": 1,
            "sub_run_id": "_orchestrator->hypothesis@d1",
            "parent_run_id": "orchestrator__whatever__session__whatever",
        }
        await ExecutionIngestService().ingest_raw_record(
            db,
            context=IngestContext(
                tenant_id=settings.runtime_tenant_id, workspace_id=GROUP,
                project_id=str(project.id), session_id=session_id, run_id=parent_id,
                actor_user_id=None, decision_authority=None,
            ),
            file_identity="hyp-transcript", byte_offset=0,
            raw_line=_json.dumps(raw, ensure_ascii=False).encode(),
            raw=raw, adapter_state={},
        )
        await db.commit()

        child_id = await db.scalar(
            select(ExecutionEvent.run_id)
            .where(ExecutionEvent.session_id == session_id,
                   ExecutionEvent.parent_run_id == parent_id)
            .limit(1)
        )

    assert child_id and child_id != parent_id, f"摄取没造出子节点 run：{child_id!r}"

    response = await client.get(f"/api/v1/runs/{child_id}", headers=_headers(researcher))
    assert response.status_code == 200, (
        f"平台生成的子节点 run id 在 URL 里取不回来（{response.status_code}）：{child_id}\n"
        "UI 上的症状是「Execution record unavailable」"
    )
    assert response.json()["run"]["id"] == child_id


@pytest.mark.asyncio
async def test_the_done_frame_carries_the_authoritative_reply(runtime_client) -> None:
    """流过 token 之后，终帧仍必须带回复契约的那一段（2026-08-17）。

    ## 现场

    以前这一行是 `reply = result.pop("reply")` 之后**只在没流过 token 时才发**。
    于是流式路径上客户端手里只剩逐个 token 拼起来的一坨 —— 那是这一轮所有
    中间轮次的散文首尾相接，不是回复。用户看到同一段话出现两次：上面是拼接
    产物，下面"第 N 轮"是同一段的叙述事件。

    ## 这条测试锁的不变量

    终帧必须**带**权威终稿，客户端才有东西可以把消息收敛过去。流式仍然照发
    （过程可见是它的价值），两者职责分开。
    """
    client, _factory = runtime_client
    researcher = await _token(client, "researcher@atrium.local")
    projects = await client.get("/api/v1/projects/", headers=_headers(researcher))
    project_id = projects.json()[0]["id"]
    conversation = await client.post(
        f"/api/v1/projects/{project_id}/sessions",
        headers=_headers(researcher),
        json={"title": "Reply contract"},
    )
    conversation_id = conversation.json()["id"]

    response = await client.post(
        f"/api/v1/chat/projects/{project_id}/stream",
        headers=_headers(researcher),
        json={"answer": {"kind": "text", "text": "run a short demo turn"}, "conversation_id": conversation_id},
    )
    assert response.status_code == 200, response.text
    events = [
        json.loads(line.removeprefix("data: "))
        for line in response.text.splitlines()
        if line.startswith("data: ")
    ]
    done = next(event for event in events if event["type"] == "done")
    assert "reply" in done, "终帧没带权威终稿 —— 客户端只能把拼接产物留在对话里"
    assert isinstance(done["reply"], str) and done["reply"].strip(), \
        "终稿是空的，收敛就成了把消息清空"


@pytest.mark.asyncio
async def test_a_message_while_running_routes_to_an_interjection(
    runtime_client,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    """单一入口的机械分流（2026-08-17）：会话正忙 + 有活体进程 → 这句话是
    插话 —— 落 user 消息行 + 投收件箱，SSE 立即回 routed=interject 的 done。

    以前这里 409，前端被迫自己分叉一条 /interrupt 路径，并因为一个多余的
    isDriver 检查把用户的话静默吞掉。"话该走哪"是后端的事实判断。
    """
    from app.services.harness_sessions import harness_session_manager

    client, factory = runtime_client
    researcher = await _token(client, "researcher@atrium.local")
    project_id = (
        await client.get("/api/v1/projects/", headers=_headers(researcher))
    ).json()[0]["id"]
    session = await client.post(
        f"/api/v1/projects/{project_id}/sessions",
        headers=_headers(researcher),
        json={"title": "Interject routing"},
    )
    session_id = session.json()["id"]
    async with factory() as db:
        projection = await db.get(SessionProjection, session_id)
        assert projection is not None
        projection.git_worktree_path = str(tmp_path)
        db.add(
            Run(
                id="run_interject_target",
                tenant_id=projection.tenant_id,
                workspace_id=projection.workspace_id,
                project_id=projection.project_id,
                session_id=projection.session_id,
                status="running",
            )
        )
        await db.commit()

    deposited: list[tuple[str, str]] = []

    async def _delivered(**kwargs):
        deposited.append((kwargs.get("kind"), kwargs.get("text"), kwargs))
        return {"delivered": True, "item_id": "in-0001", "occupancy": "working"}

    monkeypatch.setattr(harness_session_manager, "deliver", _delivered)
    # 占用判据只有一个（RFC D10）：现场说忙。run 行不再参与。
    monkeypatch.setattr(harness_session_manager, "is_occupied", lambda *_a, **_k: True)

    async def must_not_start_harness(**_kwargs):
        pytest.fail("忙时消息必须分流成插话，不能开新一轮")

    monkeypatch.setattr(harness_session_manager, "turn", must_not_start_harness)

    response = await client.post(
        f"/api/v1/chat/projects/{project_id}/stream",
        headers=_headers(researcher),
        json={"answer": {"kind": "text", "text": "跑的怎么样了？"}, "conversation_id": session_id},
    )
    assert response.status_code == 200, response.text
    events = [
        json.loads(line.removeprefix("data: "))
        for line in response.text.splitlines()
        if line.startswith("data: ")
    ]
    done = next(event for event in events if event["type"] == "done")
    assert done["routed"] == "interject"
    assert done["runId"] == "run_interject_target"
    assert done["status"] == "delivered"
    # `liveness` 字段删了：走到这个分支就是"确实有人在跑"，一个恒真的字段
    # 不是事实。当下状态由 **worker 的回执**给（它自己报的），不由我们推。
    assert done["occupancy"] == "working"

    assert [(k, t) for k, t, _ in deposited] == [("message", "跑的怎么样了？")]

    # 插话是对话的一部分 —— user 消息行必须存在，且归属到正在跑的 run。
    async with factory() as db:
        rows = (
            await db.execute(
                select(SessionMessage)
                .where(SessionMessage.session_id == session_id)
                .order_by(SessionMessage.sequence)
            )
        ).scalars().all()
        assert [(m.role, m.content) for m in rows] == [("user", "跑的怎么样了？")]
        assert rows[0].run_id == "run_interject_target"

    # message_id 必须随投递一起走到 worker：回执和答复靠它锚回**这条**
    # 消息。丢了它，答复只能挂进 run 的活动窗口 —— 渲染在提问上面
    # （2026-08-18 实测的症状，正是本 PR 要修的）。
    assert deposited[0][2].get("message_id") == rows[0].id


@pytest.mark.asyncio
async def test_a_refused_checkpoint_is_witnessed_but_does_not_kill_the_turn(
    runtime_client, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """checkpoint 是记账层，抄写失败不许让科研整轮陪葬（2026-08-22 事故）。

    被拒的入库必须：1) 吵 —— SSE 上有 workspace.checkpoint_failed 见证；
    2) 不杀轮 —— 流以 done 收尾、run 记 completed；3) 不丢 —— 工作区照旧
    是脏的，下一次 checkpoint 连同这次一起重试。把 local_execution 里的
    try/except 拿掉，这个测试立刻红。
    """
    client, factory = runtime_client
    monkeypatch.setattr(settings, "harness_bridge_enabled", True)
    monkeypatch.setattr(settings, "harness_state_root", str(tmp_path))
    researcher = await _token(client, "researcher@atrium.local")
    created_backend = await client.post(
        "/api/v1/settings/model-backends",
        headers=_headers(researcher),
        json={
            "provider": "openai_compatible",
            "display_name": "Checkpoint refusal test",
            "model": "test-model",
            "base_url": "http://provider.invalid/v1",
            "api_key": "checkpoint-refusal-key",
        },
    )
    await client.post(
        f"/api/v1/settings/model-backends/{created_backend.json()['id']}/default",
        headers=_headers(researcher),
    )
    project = (await client.get("/api/v1/projects/", headers=_headers(researcher))).json()[0]
    conversation = await client.post(
        f"/api/v1/projects/{project['id']}/sessions",
        headers=_headers(researcher),
        json={"title": "Checkpoint refusal"},
    )
    conversation_id = conversation.json()["id"]

    from app.services.project_repository import get_project_repository

    repository = get_project_repository()
    workspace = (await run_in_repository_thread(repository.session_status, project["id"], conversation_id))
    relative = "literature/scripts/collect.py"
    script = Path(workspace.path) / relative
    script.parent.mkdir(parents=True)
    script.write_text("print('survives')\n", encoding="utf-8")

    transcript = tmp_path / "refusal" / "transcript.jsonl"
    transcript.parent.mkdir(parents=True)
    # workspace_prefix 与节点归属不符 → checkpoint 被 ProjectRepositoryError
    # 拒绝。选这个失败方式是因为它走真实校验路径，不靠打桩。
    checkpoint_native = {
        "event": "workspace_checkpoint_requested",
        "at": datetime.now(UTC).isoformat(),
        "node_type": "literature",
        "run_id": "harness-refused-run",
        "run_status": "completed",
        "workspace_prefix": "observation",
        "paths": [relative],
        "files_changed": 1,
        "additions": 1,
        "deletions": 0,
    }
    checkpoint_encoded = (json.dumps(checkpoint_native) + "\n").encode()
    transcript.write_bytes(checkpoint_encoded)

    async def fake_harness_turn(**kwargs):
        await kwargs["on_protocol_event"](
            {
                "type": "transcript",
                "transcript_path": str(transcript),
                "byte_start": 0,
                "byte_end": len(checkpoint_encoded),
                "event": checkpoint_native,
            }
        )
        return {
            "status": "completed",
            "run_id": "harness-refused-run",
            "final_text": "Work finished despite the refused checkpoint.",
            "tokens_used_delta": 7,
            "transcript_path": str(transcript),
            "artifact_paths": [],
            "pause_event": None,
            "pause_pending_path": None,
            "_session_resumable": False,
        }

    monkeypatch.setattr(harness_session_manager, "turn", fake_harness_turn)
    response = await client.post(
        f"/api/v1/chat/projects/{project['id']}/stream",
        headers=_headers(researcher),
        json={"answer": {"kind": "text", "text": "Keep going"}, "conversation_id": conversation_id},
    )
    assert response.status_code == 200
    stream_events = [
        json.loads(line.removeprefix("data: "))
        for line in response.text.splitlines()
        if line.startswith("data: ")
    ]

    assert not [event for event in stream_events if event["type"] == "error"]
    done = next(event for event in stream_events if event.get("type") == "done")
    witness = next(
        event
        for event in stream_events
        if event.get("type") == "progress"
        and event.get("event") == "workspace.checkpoint_failed"
    )
    assert "没有丢失" in witness["detail"]

    async with factory() as observer:
        run = await observer.get(Run, done["run_id"])
        assert run is not None and run.status == "completed"

    after = (await run_in_repository_thread(repository.session_status, project["id"], conversation_id))
    assert after.head_commit == workspace.head_commit
    assert after.clean is False


@pytest.mark.asyncio
async def test_the_ingest_path_never_writes_through_the_turns_session(
    runtime_client, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """一个 session 只能有一个写者 —— 2026-08-25 00:03 事故的根因。

    摄取由 **reader task** 调（`harness_sessions._read_forever`），这一轮的收尾
    跑在 worker task 上。两者从前共用 `worker_db` 这一个 AsyncSession。
    `_read_forever` 每读一行是"先摄取、再把事件放进等它的队列"，所以主协程被
    终止事件唤醒时 reader 后面还排着行 —— events.jsonl 里 result 在
    16:03:38.789976，两条 transcript 在 .794000 / .794122。主协程此刻正在
    `_connection_for_bind()` 里取连接：

        InvalidRequestError: This session is provisioning a new connection;
                             concurrent operations are not permitted
        → except 里的 db.rollback() 跟着炸成 IllegalStateChangeError

    原始异常被二级异常吞掉，用户拿到一句 `execution_failed`。那一轮跑了两小时、
    4463055 token，harness 早已 `run.completed`、论文也交付了，而 `session_messages`
    里 0 条 assistant —— 收尾的写入还没提交就被 rollback 丢了。
    （事实本身没丢：4406 条 execution_events 都在，摄取一路是自己 commit 的。）

    ## 判据为什么落在"用的是不是同一个 session"上

    试过两版行为面判据，变异（改回共用 session）**都是绿的**：

      · 复现时序：把摄取按在飞行中再交付终止事件 —— ISCE 要求 session 正卡在
        `_connection_for_bind()` 里，而测试替身停在发起 IO 之前，两个协程根本
        没在同一次 IO 上重叠。
      · 断言事实在失败后还在 —— 摄取本来就一路自己 commit，共用 session 时
        那句 commit 同样把事实定了稿，所以两边都绿。

    复现不了的时序不是判据。真正被修好的东西是**结构**：摄取不再碰这一轮的
    session。那就直接验它 —— 断言落在 session 的对象身份上，不落在文案或
    异常类型上。
    """
    client, factory = runtime_client
    monkeypatch.setattr(settings, "harness_bridge_enabled", True)
    monkeypatch.setattr(settings, "harness_state_root", str(tmp_path))
    researcher = await _token(client, "researcher@atrium.local")
    created = await client.post(
        "/api/v1/settings/model-backends",
        headers=_headers(researcher),
        json={
            "provider": "openai_compatible",
            "display_name": "Single writer test",
            "model": "test-model",
            "base_url": "http://provider.invalid/v1",
            "api_key": "test-key",
        },
    )
    assert created.status_code == 201
    assert (
        await client.post(
            f"/api/v1/settings/model-backends/{created.json()['id']}/default",
            headers=_headers(researcher),
        )
    ).status_code == 200

    ingest_sessions: list[int] = []
    turn_sessions: list[int] = []
    real_wrapper = local_execution.ingest_transcript_wrapper
    real_append = local_execution.append_session_message

    async def record_ingest_session(db, **kwargs):
        ingest_sessions.append(id(db))
        return await real_wrapper(db, **kwargs)

    async def record_turn_session(db, **kwargs):
        turn_sessions.append(id(db))
        return await real_append(db, **kwargs)

    monkeypatch.setattr(local_execution, "ingest_transcript_wrapper", record_ingest_session)
    monkeypatch.setattr(local_execution, "append_session_message", record_turn_session)

    transcript = tmp_path / "orchestrator__single-writer" / "transcript.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_bytes(b"")

    async def fake_harness_turn(**kwargs):
        raw_event = {
            "event": "llm_response",
            "at": datetime.now(UTC).isoformat(),
            "usage": {"total_tokens": 11, "coverage": "partial"},
        }
        raw_line = (json.dumps(raw_event) + "\n").encode()
        byte_start = transcript.stat().st_size
        with transcript.open("ab") as output:
            output.write(raw_line)
        await kwargs["on_protocol_event"](
            {
                "type": "transcript",
                "transcript_path": str(transcript),
                "byte_start": byte_start,
                "byte_end": byte_start + len(raw_line),
                "event": raw_event,
            }
        )
        return {
            "status": "completed",
            "run_id": "orchestrator__single-writer",
            "final_text": "the research finished",
            "tokens_used": 11,
            "tokens_used_delta": 11,
            "transcript_path": str(transcript),
            "artifact_paths": [],
            "pause_event": None,
            "pause_pending_path": None,
        }

    monkeypatch.setattr(harness_session_manager, "turn", fake_harness_turn)
    monkeypatch.setattr(harness_session_manager, "paused_binding", lambda *_args: None)
    project_id = (await client.get("/api/v1/projects/", headers=_headers(researcher))).json()[0][
        "id"
    ]
    conversation_id = (
        await client.post(
            f"/api/v1/projects/{project_id}/sessions",
            headers=_headers(researcher),
            json={"title": "Single writer"},
        )
    ).json()["id"]

    response = await client.post(
        f"/api/v1/chat/projects/{project_id}/stream",
        headers=_headers(researcher),
        json={"answer": {"kind": "text", "text": "run the research"}, "conversation_id": conversation_id},
    )
    assert response.status_code == 200

    assert ingest_sessions, "这一轮没有摄取发生 —— 用例没走到要验的那条路"
    assert turn_sessions, "这一轮没有收尾写入 —— 用例没走到要验的那条路"
    assert not (set(ingest_sessions) & set(turn_sessions)), (
        "摄取又在往这一轮的 session 上写了 —— 一个 session 两个写者，"
        "2026-08-25 00:03 的 IllegalStateChangeError 就是这么来的"
    )

    # 而且这一轮**确实**收尾成功了：结论进了对话。
    async with factory() as db:
        replies = (
            await db.scalars(
                select(SessionMessage).where(
                    SessionMessage.session_id == conversation_id,
                    SessionMessage.role == "assistant",
                )
            )
        ).all()
    assert [message.content for message in replies] == ["the research finished"]

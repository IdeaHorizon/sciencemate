"""One desktop default, scoped remote credentials, and explicit remote overrides."""

from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from app.auth import get_current_user, implicit_local_user
from app.config import settings
from app.main import app
from app.models.model_backend import ModelBackendConfig
from app.services import model_backends, remote_models, remote_servers


@pytest.fixture(autouse=True)
def bridge_enabled(monkeypatch):
    monkeypatch.setattr(settings, "harness_bridge_enabled", True)
    remote_models._delivered.clear()


def bundle(source=None, backend=None, **changes):
    return remote_models.DesktopModels(
        source_desktop_id=source or uuid4(),
        models=[
            {
                "source_backend_id": backend or uuid4(),
                "provider": "openai_compatible",
                "display_name": "App default",
                "model": "example-model",
                "base_url": "https://provider.example.test/v1",
                "api_key": "test-model-credential",
                "context_window_tokens": 128000,
                "roles": ["reasoning"],
                **changes,
            }
        ],
    )


async def selected(db, user):
    return await model_backends.select_effective_backend(db, user, role="reasoning")


@pytest.mark.asyncio
async def test_an_empty_remote_inherits_the_actual_default_and_encrypts_its_key(db_session):
    user = await implicit_local_user(db_session)
    data = bundle()
    await remote_models.apply_desktop_defaults(db_session, user, data)
    model = await selected(db_session, user)
    assert model.model == "example-model" and model.context_window_tokens == 128000
    assert model.inherited_from_desktop == str(data.source_desktop_id)
    assert model.encrypted_api_key != "test-model-credential"
    assert model_backends.resolved_api_key(model) == "test-model-credential"
    await remote_models.apply_desktop_defaults(db_session, user, data)
    assert (await selected(db_session, user)).id == model.id
    assert await db_session.scalar(select(func.count()).select_from(ModelBackendConfig)) == 1


@pytest.mark.asyncio
async def test_rotation_and_model_changes_update_the_source_identity_without_duplicate_rows(
    db_session,
):
    user = await implicit_local_user(db_session)
    data = bundle()
    await remote_models.apply_desktop_defaults(db_session, user, data)
    old = await selected(db_session, user)
    old.last_probe_ok = True
    identity = old.id
    changed = bundle(
        data.source_desktop_id,
        data.models[0].source_backend_id,
        model="next-model",
        api_key="rotated-model-key",
    )
    await remote_models.apply_desktop_defaults(db_session, user, changed)
    current = await selected(db_session, user)
    assert current.id == identity and current.model == "next-model"
    assert model_backends.resolved_api_key(current) == "rotated-model-key"
    assert current.last_probe_ok is None


@pytest.mark.asyncio
async def test_a_remote_owned_default_is_preserved(db_session):
    user = await implicit_local_user(db_session)
    own = ModelBackendConfig(
        scope_kind="personal",
        scope_id=user.id,
        provider="openai_compatible",
        display_name="Remote choice",
        model="remote-model",
        base_url="https://remote.example.test/v1",
        roles=["reasoning"],
        credential_source="encrypted",
        encrypted_api_key=model_backends.encrypt_api_key("remote-owned-key"),
        created_by_user_id=user.id,
    )
    db_session.add(own)
    await db_session.flush()
    await model_backends.set_effective_default(db_session, user, own)
    await remote_models.apply_desktop_defaults(db_session, user, bundle())
    assert (await selected(db_session, user)).id == own.id
    assert model_backends.resolved_api_key(own) == "remote-owned-key"


@pytest.mark.asyncio
async def test_explicit_remote_edits_take_ownership_instead_of_being_overwritten(db_session):
    from app.api.v1.settings import ModelBackendUpdate, update_model_backend

    user = await implicit_local_user(db_session)
    data = bundle()
    await remote_models.apply_desktop_defaults(db_session, user, data)
    own = await selected(db_session, user)
    await update_model_backend(
        own.id, ModelBackendUpdate(display_name="My remote configuration"), user, db_session, "en"
    )
    assert own.inherited_from_desktop is None
    await remote_models.apply_desktop_defaults(
        db_session,
        user,
        bundle(
            data.source_desktop_id,
            data.models[0].source_backend_id,
            api_key="different-desktop-key",
        ),
    )
    assert own.display_name == "My remote configuration"
    assert model_backends.resolved_api_key(own) == "test-model-credential"


@pytest.mark.asyncio
async def test_revocation_clears_credentials_and_keeps_historical_model_identity(db_session):
    user = await implicit_local_user(db_session)
    data = bundle()
    await remote_models.apply_desktop_defaults(db_session, user, data)
    model = await selected(db_session, user)
    await remote_models.apply_desktop_defaults(
        db_session,
        user,
        remote_models.DesktopModels(source_desktop_id=data.source_desktop_id, models=[]),
    )
    assert await selected(db_session, user) is None
    assert await db_session.get(ModelBackendConfig, model.id) is not None
    assert model.encrypted_api_key is None and not model.is_enabled


@pytest.mark.asyncio
async def test_import_endpoint_requires_the_runtime_device_token(client, db_session, monkeypatch):
    user = await implicit_local_user(db_session)
    await db_session.commit()

    async def current_user():
        return user

    app.dependency_overrides[get_current_user] = current_user
    monkeypatch.setattr(settings, "profile", "personal")
    monkeypatch.setattr(settings, "remote_runtime_token", "test-runtime-device-token")
    data = remote_models._wire(bundle())
    rejected = await client.post("/api/v1/remote-runtime/models", json=data)
    assert rejected.status_code == 401
    accepted = await client.post(
        "/api/v1/remote-runtime/models",
        json=data,
        headers={"Authorization": "Bearer test-runtime-device-token"},
    )
    assert accepted.status_code == 200, accepted.text
    assert "test-model-credential" not in accepted.text


@pytest.mark.asyncio
async def test_delivery_tracks_current_desktop_defaults_and_key_rotation(db_session, monkeypatch):
    user = await implicit_local_user(db_session)
    data = bundle()
    await remote_models.apply_desktop_defaults(db_session, user, data)
    model = await selected(db_session, user)
    monkeypatch.setattr(settings, "profile", "personal")
    monkeypatch.setattr(settings, "remote_runtime_token", "")
    remote_models._delivered.clear()
    delivered = []

    async def ask(server, method, path, **kwargs):
        delivered.append(kwargs["json"])
        return {"state": "synchronized"}

    monkeypatch.setattr(remote_servers, "ask", ask)
    server = "remote:" + str(uuid4())
    assert await remote_models.ensure_desktop_defaults(server) == "synchronized"
    assert await remote_models.ensure_desktop_defaults(server) == "current"
    assert len(delivered) == 1
    model.encrypted_api_key = model_backends.encrypt_api_key("rotated-source-key")
    await db_session.commit()
    await remote_models.ensure_desktop_defaults(server)
    assert delivered[-1]["models"][0]["api_key"] == "rotated-source-key"
    assert len(delivered) == 2


@pytest.mark.asyncio
async def test_shared_backends_and_headless_runtimes_never_export_models(monkeypatch):
    monkeypatch.setattr(settings, "profile", "org")
    assert await remote_models.ensure_desktop_defaults("remote:unused") == "not_applicable"
    monkeypatch.setattr(settings, "profile", "personal")
    monkeypatch.setattr(settings, "remote_runtime_token", "device")
    assert await remote_models.ensure_desktop_defaults("remote:unused") == "not_applicable"


@pytest.mark.asyncio
async def test_model_role_conflicts_are_rejected_without_partial_writes(db_session):
    user = await implicit_local_user(db_session)
    data = bundle()
    data.models.append(data.models[0].model_copy(update={"source_backend_id": uuid4()}))
    with pytest.raises(HTTPException, match="422"):
        await remote_models.apply_desktop_defaults(db_session, user, data)
    assert await db_session.scalar(select(func.count()).select_from(ModelBackendConfig)) == 0

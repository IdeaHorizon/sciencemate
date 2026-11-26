"""Desktop defaults projected into its authenticated personal SSH runtimes."""

from __future__ import annotations

import asyncio
import hashlib
import json
from uuid import UUID
from weakref import WeakValueDictionary

from fastapi import HTTPException
from pydantic import BaseModel, Field, SecretStr
from sqlalchemy import select

from app.config import settings
from app.models.model_backend import ModelBackendConfig, UserModelBackendPreference
from app.services import model_backends, remote_servers
from app.services.model_role_catalog import catalog

_locks = WeakValueDictionary()
_delivered: dict[tuple[str, str], str] = {}


class InheritedModel(BaseModel):
    source_backend_id: UUID
    provider: str = Field(min_length=1, max_length=64)
    display_name: str = Field(min_length=1, max_length=200)
    model: str = Field(min_length=1, max_length=200)
    base_url: str | None = Field(default=None, max_length=1000)
    api_key: SecretStr | None = None
    context_window_tokens: int | None = Field(default=None, ge=8000, le=2_000_000)
    roles: list[str]


class DesktopModels(BaseModel):
    source_desktop_id: UUID
    models: list[InheritedModel] = Field(max_length=32)


async def desktop_defaults(db, user) -> DesktopModels:
    if not await model_backends.list_visible_backends(db, user):
        return DesktopModels(source_desktop_id=user.id, models=[])
    selected = {}
    for role in catalog():
        backend = await model_backends.select_effective_backend(db, user, role=role.id)
        if backend is None:
            continue
        if backend.id not in selected:
            selected[backend.id] = {
                "source_backend_id": backend.id,
                "provider": backend.provider,
                "display_name": backend.display_name,
                "model": backend.model,
                "base_url": backend.base_url,
                "api_key": model_backends.resolved_api_key(backend),
                "context_window_tokens": backend.context_window_tokens,
                "roles": [],
            }
        selected[backend.id]["roles"].append(role.id)
    return DesktopModels(source_desktop_id=user.id, models=list(selected.values()))


def _wire(bundle: DesktopModels) -> dict:
    value = bundle.model_dump(mode="json")
    for model, row in zip(bundle.models, value["models"], strict=True):
        row["api_key"] = model.api_key.get_secret_value() if model.api_key else None
    return value


async def ensure_desktop_defaults(server_id: str, *, force=False) -> str:
    # A shared backend must never export another user's credentials, and a
    # headless runtime must not recursively publish to other machines.
    from app.assembly import owns_personal_model_settings

    if not owns_personal_model_settings() or settings.remote_runtime_token:
        return "not_applicable"
    from app.auth import implicit_local_user
    from app.database import get_session_factory
    from app.services.other_homes import ThereIsNoSuchHomeError

    lock = _locks.setdefault(server_id, asyncio.Lock())
    async with lock:
        async with get_session_factory()() as db:
            user = await implicit_local_user(db)
            bundle = await desktop_defaults(db, user)
            await db.commit()
        payload = _wire(bundle)
        identity = (server_id, str(bundle.source_desktop_id))
        fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        if not force and _delivered.get(identity) == fingerprint:
            return "current"
        try:
            async with asyncio.timeout(10):
                await remote_servers.ask(
                    server_id, "POST", "/api/v1/remote-runtime/models", json=payload
                )
        except ThereIsNoSuchHomeError as exc:
            if exc.status in {404, 405}:
                # Old runtimes keep their own configuration and running tasks.
                return "requires_runtime_update"
            raise
        _delivered[identity] = fingerprint
        return "synchronized"


async def apply_desktop_defaults(db, user, bundle: DesktopModels) -> dict:
    source = str(bundle.source_desktop_id)
    roles = {role.id for role in catalog()}
    requested = [role for model in bundle.models for role in model.roles]
    if len(set(requested)) != len(requested) or not set(requested) <= roles:
        raise HTTPException(422, "Each known model role must have at most one desktop default")
    if any(
        model.provider not in model_backends.HARNESS_COMPATIBLE_PROVIDERS for model in bundle.models
    ):
        raise HTTPException(422, "Unsupported inherited model provider")
    existing = {
        role: await model_backends.select_effective_backend(
            db, user, role=role, require_ready=False
        )
        for role in roles
    }
    owned = list(
        (
            await db.scalars(
                select(ModelBackendConfig).where(
                    ModelBackendConfig.scope_kind == "personal",
                    ModelBackendConfig.scope_id == user.id,
                    ModelBackendConfig.inherited_from_desktop == source,
                )
            )
        ).all()
    )
    by_source = {row.inherited_backend_id: row for row in owned}
    retained = set()
    for model in bundle.models:
        source_id = str(model.source_backend_id)
        row = by_source.get(source_id)
        clash = await model_backends.find_conflicting_backend(
            db,
            scope_kind="personal",
            scope_id=user.id,
            provider=model.provider,
            model=model.model,
            base_url=model.base_url,
            exclude_id=row.id if row else None,
        )
        if clash is not None:
            # A manually configured remote connection owns its credential.
            chosen = clash
        else:
            old_identity = (row.provider, row.model, row.base_url) if row else None
            if row is None:
                row = ModelBackendConfig(
                    scope_kind="personal",
                    scope_id=user.id,
                    created_by_user_id=user.id,
                    inherited_from_desktop=source,
                    inherited_backend_id=source_id,
                )
                db.add(row)
            row.provider, row.model, row.base_url = model.provider, model.model, model.base_url
            row.display_name = model.display_name
            row.roles = list(model.roles)
            row.context_window_tokens = model.context_window_tokens
            key = model.api_key.get_secret_value() if model.api_key else None
            changed = (
                model_backends.resolved_api_key(row) != key if row.credential_source else True
            ) or old_identity != (model.provider, model.model, model.base_url)
            row.encrypted_api_key = model_backends.encrypt_api_key(key) if key else None
            row.credential_source = "encrypted" if key else "none"
            row.is_enabled = True
            if changed:
                row.last_probe_at = row.last_probe_ok = row.last_probe_detail = None
                row.last_vision_ok = row.last_vision_detail = None
            await db.flush()
            if key is None:
                await model_backends.record_backend_probe(db, row)
            retained.add(row.id)
            chosen = row
        for role in model.roles:
            current = existing[role]
            if current is not None and current.inherited_from_desktop != source:
                continue  # Preserve explicit remote defaults and other desktops.
            preference = await db.get(UserModelBackendPreference, (user.id, role))
            if preference:
                preference.backend_id = chosen.id
            else:
                db.add(UserModelBackendPreference(user_id=user.id, role=role, backend_id=chosen.id))
    # Revocation removes live credentials but retains model identity for history.
    for row in owned:
        if row.id not in retained:
            row.is_enabled = False
            row.encrypted_api_key = None
            row.credential_source = "none"
    await db.commit()
    return {"state": "synchronized", "models": len(retained)}

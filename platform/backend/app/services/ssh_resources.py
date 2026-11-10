"""One SSH execution service for the Resources API and Harness tools.

SQL owns registrations; remote records own job state. Private encrypted files
hold account credentials, bound to their owner, tenant, project and connection.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from pathlib import Path
from uuid import uuid4

from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException
from sqlalchemy import select

from app.config import settings
from app.models.resource import ProjectResource
from app.models.user import User
from app.services.harness_imports import harness_module
from app.services.sessions import require_capability
from app.services.user_interface import Language, language_for, said_of

log = logging.getLogger(__name__)


def _vault_path(resource_id: str) -> Path:
    from uuid import UUID

    return Path(settings.platform_data_root) / "private" / "ssh" / (str(UUID(resource_id)) + ".enc")


def _cipher():
    from app.services.credential_key import master_key

    return Fernet(master_key())


def _binding(resource):
    return dict(
        tenant_id=resource.tenant_id,
        project_id=resource.project_id,
        resource_id=resource.id,
        owner_id=resource.created_by_user_id,
        endpoint=resource.endpoint,
        connection=(resource.config or {}).get("ssh"),
    )


def _save_account(resource, password):
    data = dict(binding=_binding(resource), password=password)
    private_files = harness_module("shared.lib.private_files")
    path = _vault_path(resource.id)
    temporary = path.with_name(path.name + "." + uuid4().hex)
    try:
        private_files.write_private_bytes(temporary, _cipher().encrypt(json.dumps(data).encode()))
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _transport(resource, user):
    if resource.created_by_user_id != user.id:
        raise HTTPException(
            403, "This SSH account belongs to another user; connect your own account"
        )
    if resource.secret_ref != "secret://ssh/" + resource.id:
        raise HTTPException(409, "SSH credential was removed; reconnect this resource")
    try:
        account = json.loads(_cipher().decrypt(_vault_path(resource.id).read_bytes()))
    except (OSError, ValueError, InvalidToken):
        raise HTTPException(409, "SSH credential is unavailable; reconnect this resource")
    if account["binding"] != _binding(resource):
        raise HTTPException(
            409, "SSH connection changed; reconnect before using the saved credential"
        )
    ssh = harness_module("core.ssh_transport")
    return ssh.SSHTransport(
        ssh.SSHConnection(**account["binding"]["connection"]), account["password"]
    )


async def connect(
    db,
    user,
    project_id: str,
    *,
    host: str,
    user_name: str | None = None,
    port: int | None = None,
    password: str | None = None,
    jump: str | None = None,
    name: str | None = None,
    lang: Language | None = None,
):
    lang = lang or language_for(user)
    project = await require_capability(db, user, project_id, "manage_resources")
    ssh = harness_module("core.ssh_transport")
    try:
        connection = ssh.SSHConnection(host, user_name, port, jump)
    except ValueError as exc:
        log.warning("Invalid SSH connection details", exc_info=True)
        raise HTTPException(
            422,
            said_of(
                exc,
                lang,
                otherwise={
                    "zh": "SSH 连接信息无效，请检查地址、账号和端口。",
                    "en": "Invalid SSH connection details. Check the host, user and port.",
                },
            ),
        ) from exc
    transport = ssh.SSHTransport(connection, password)
    try:
        observed = await asyncio.to_thread(transport.call, {"action": "probe"})
    except ssh.SSHError as exc:
        log.warning("SSH connection probe failed", exc_info=True)
        raise HTTPException(
            502,
            said_of(
                exc,
                lang,
                otherwise={
                    "zh": "SSH 连接失败，请检查地址、登录信息和服务器主机密钥。",
                    "en": "SSH connection failed. Check the address, sign-in details and server host key.",
                },
            ),
        ) from exc
    resource_name = (name or host).strip()
    if not resource_name or len(resource_name) > 200:
        raise HTTPException(422, "Resource name must contain 1 to 200 characters")
    resource = await db.scalar(
        select(ProjectResource).where(
            ProjectResource.tenant_id == settings.runtime_tenant_id,
            ProjectResource.project_id == project_id,
            ProjectResource.resource_type == "compute",
            ProjectResource.name == resource_name,
        )
    )
    if resource and resource.created_by_user_id != user.id:
        raise HTTPException(403, "Resource name belongs to another user; choose a different name")
    if resource is None:
        resource = ProjectResource(
            id=str(uuid4()),
            tenant_id=settings.runtime_tenant_id,
            project_id=project_id,
            resource_type="compute",
            name=resource_name,
            provider="ssh",
            created_by_user_id=user.id,
        )
        db.add(resource)
    resource.provider = "ssh"
    resource.endpoint = f"{host}:{port}" if port else host
    resource.config = dict(
        ssh=connection.public(), observation=observed, execution_boundary="remote_account"
    )
    resource.is_enabled = True
    resource.disabled_at = None
    resource.updated_by_user_id = user.id
    resource.secret_ref = "secret://ssh/" + resource.id
    await db.flush()
    await asyncio.to_thread(_save_account, resource, password)
    from app.services.project_record import commit_project_record

    await commit_project_record(
        db, project=project, user=user, message="resources: connect SSH " + resource.name
    )
    await db.refresh(resource)
    from app.api.v1.resources import _resource_response

    return dict(status="success", resource=_resource_response(resource), observation=observed)


async def operate(
    db, user, project_id: str, resource_id: str, action: str, *,
    lang: Language | None = None, **params,
):
    from app.api.v1.resources import _get_resource

    await require_capability(db, user, project_id, "drive_session")
    resource = await _get_resource(db, project_id=project_id, resource_id=resource_id)
    if resource.provider != "ssh" or resource.resource_type != "compute":
        raise HTTPException(422, "Resource is not an SSH compute connection")
    if not resource.is_enabled and action not in ("status", "cancel", "jobs"):
        raise HTTPException(409, "SSH resource is disabled")
    if action not in ("probe", "exec", "submit", "status", "cancel", "jobs", "put", "get"):
        raise HTTPException(422, "Unsupported SSH action")
    transport = await asyncio.to_thread(_transport, resource, user)
    request = {key: value for key, value in params.items() if value is not None}
    # Scope and ownership always come from the authenticated service, never
    # from a command payload or a model-provided node input.
    request.update(action=action, project_id=project_id, resource_id=resource_id)
    if action in ("submit", "exec"):
        key = request.pop("idempotency_key", None)
        if action == "submit" and (not isinstance(key, str) or not key.strip() or len(key) > 200):
            raise HTTPException(
                422, "submit requires a stable idempotency_key (reuse it after a lost response)"
            )
        request["job_id"] = (
            hashlib.sha256((resource_id + ":" + key).encode()).hexdigest()[:32]
            if key
            else uuid4().hex
        )
        if action == "exec" and not 0 < request.get("timeout", 60) <= 300:
            raise HTTPException(
                422, "Synchronous execution timeout must be at most 300 seconds; submit longer jobs"
            )
    timeout = max(40, float(request.get("timeout", 60)) + 15) if action == "exec" else 40
    ssh = harness_module("core.ssh_transport")
    try:
        result = await asyncio.to_thread(transport.call, request, timeout=timeout)
    except ssh.SSHInputManifestConflict as exc:
        raise HTTPException(
            409,
            dict(
                error_code="input_manifest_conflict",
                message=said_of(
                    exc,
                    lang or language_for(user),
                    otherwise={
                        "zh": "此提交键已绑定到另一份输入清单。",
                        "en": "Submission key already belongs to a different input manifest",
                    },
                ),
                resource_id=resource_id,
                job_id=request["job_id"],
                existing_input_manifest_sha256=exc.existing_input_manifest_sha256,
                requested_input_manifest_sha256=request.get("input_manifest_sha256"),
            ),
        ) from exc
    except (ssh.SSHError, ValueError) as exc:
        detail = dict(message=str(exc), resource_id=resource_id)
        if request.get("job_id"):
            detail["job_id"] = request["job_id"]
        raise HTTPException(502, detail) from exc
    if action == "probe":
        resource.config = {**resource.config, "observation": result}
        await db.flush()
    # Account secrets must not be reflected by command output either.
    if transport.password:
        result = harness_module("core.secrets").redact(
            result, secrets={transport.password: "SSH_PASSWORD"}
        )
    return {
        **result,
        "resource_id": resource_id,
        "status": "error" if action == "exec" and result.get("state") != "succeeded" else "success",
    }


async def dispatch(db, user, project_id, action, **params):
    from app.schemas.resource import SSHConnectRequest, SSHOperationRequest

    if action == "connect":
        return await connect(
            db, user, project_id, **SSHConnectRequest.model_validate(params).model_dump()
        )
    if action == "list":
        from app.api.v1.resources import list_project_resources

        resources = await list_project_resources(project_id, "compute", False, user, db)
        return dict(status="success", resources=resources)
    resource_id = params.pop("resource_id")
    validated = SSHOperationRequest.model_validate(dict(action=action, **params))
    return await operate(
        db, user, project_id, resource_id, **validated.model_dump(exclude_none=True)
    )


def install_worker_bridge(context: dict, *, user_id: str, project_id: str):
    """Called only with the process-owner delivery and App Server binding."""
    from core.remote_resources import install

    from app.database import get_session_factory

    # Worker settings are supplied by the App Server, including org database
    # configuration. The launcher directory must never choose another database.
    for key in ("database_url", "platform_data_root", "runtime_tenant_id", "profile", "secret_key"):
        setattr(settings, key, context[key])

    async def handler(state, action, params):
        if str(state.project_id) != project_id:
            raise HTTPException(403, "Resource request does not belong to the bound project")
        async with get_session_factory()() as db:
            user = await db.get(User, user_id)
            if user is None or not user.is_active:
                raise HTTPException(403, "Resource account owner no longer exists")
            result = await dispatch(db, user, project_id, action, **params)
            await db.commit()
            from fastapi.encoders import jsonable_encoder

            return jsonable_encoder(result)

    install(handler)

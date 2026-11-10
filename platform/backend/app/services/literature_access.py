"""Loopback capability for E2E workers to use the platform's private sources.

The backend owns browser jobs and institution authorization. Workers receive a
private capability file, never a login password, browser cookie or database URL.
Every request rechecks the bound user and their current credential fingerprint.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from pathlib import Path
import secrets

from sqlalchemy import select
from shared.lib.private_files import write_private_bytes

from app.auth import credential_fingerprint
from app.config import data_root
from app.database import get_session_factory
from app.models.cnki import CnkiJob
from app.models.user import User
from app.schemas.cnki import JobIn
from app.services import cnki, school_library


class LiteratureAccess:
    def __init__(self):
        self.server = None
        self.grants: dict[str, dict] = {}
        self.lock = asyncio.Lock()

    def directory(self) -> Path:
        return data_root("state") / "literature-access"

    def _write(self, path: Path, value: dict):
        temporary = path.with_suffix(".tmp")
        write_private_bytes(temporary, json.dumps(value).encode("utf-8"))
        temporary.replace(path)

    async def start(self):
        async with self.lock:
            if self.server is not None:
                return
            self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0, limit=65536)
            port = self.server.sockets[0].getsockname()[1]
            # Existing workers reread their own file on each request. A backend
            # restart updates the local endpoint without restarting research runs.
            for path in self.directory().glob("*.json"):
                try:
                    grant = json.loads(path.read_text())
                    if not all(grant.get(k) for k in ("token", "user_id", "credential")):
                        continue
                    grant.update(host="127.0.0.1", port=port)
                    self._write(path, grant)
                    self.grants[hashlib.sha256(grant["token"].encode()).hexdigest()] = grant
                except (OSError, ValueError, TypeError):
                    continue

    async def stop(self):
        if self.server:
            self.server.close()
            await self.server.wait_closed()
            self.server = None
        self.grants.clear()

    async def environment(self, user: User) -> dict[str, str]:
        await self.start()
        async with self.lock:
            path = self.directory() / (hashlib.sha256(str(user.id).encode()).hexdigest() + ".json")
            try:
                previous = json.loads(path.read_text())
            except (OSError, ValueError):
                previous = {}
            credential = credential_fingerprint(user)
            token = previous.get("token") if previous.get("credential") == credential else None
            grant = {"host": "127.0.0.1", "port": self.server.sockets[0].getsockname()[1],
                     "user_id": str(user.id), "credential": credential,
                     "token": token or secrets.token_urlsafe(32)}
            self._write(path, grant)
            self.grants[hashlib.sha256(grant["token"].encode()).hexdigest()] = grant
            return {"HARNESS_LITERATURE_ACCESS_FILE": str(path)}

    async def dispatch(self, request: dict) -> dict:
        token = str(request.get("token") or "")
        grant = self.grants.get(hashlib.sha256(token.encode()).hexdigest())
        if not grant or not hmac.compare_digest(token, grant["token"]):
            raise PermissionError("source_access_denied")
        op = request.get("op")
        payload = request.get("payload") or {}
        if not isinstance(payload, dict) or op not in {"school", "cnki_submit", "cnki_status", "cnki_availability"}:
            raise ValueError("invalid_source_operation")
        async with get_session_factory()() as db:
            user = await db.get(User, grant["user_id"])
            if not user or not user.is_active or not hmac.compare_digest(
                credential_fingerprint(user), grant["credential"]
            ):
                raise PermissionError("source_access_expired")
            if op == "cnki_availability":
                from app.services.cnki_browser import availability
                return availability()
            if op == "cnki_submit":
                # Ordinary one-off metadata query only: no recurring subscriptions,
                # arbitrary user IDs, credentials or executor overrides from the model.
                job = JobIn(query=payload.get("query", ""), limit=payload.get("limit", 200))
                result = await cnki.create_job(db, user, job)
                await db.commit()
                return result
            if op == "cnki_status":
                job = await db.scalar(select(CnkiJob).where(
                    CnkiJob.id == str(payload.get("job_id") or ""),
                    CnkiJob.user_id == user.id,
                ))
                if job is None:
                    raise PermissionError("source_job_not_found")
                return await cnki.job_view(db, job, papers=True)
            query = str(payload.get("query") or "").strip()
            if not query or len(query) > 2000:
                raise ValueError("invalid_query")
            rows = await asyncio.to_thread(school_library.search, query,
                                          institution_id=str(user.institution_id), limit=200)
            return school_library.merge_search_results({"query": query, "papers": []}, rows, limit=200)

    async def _handle(self, reader, writer):
        try:
            async with asyncio.timeout(30):
                request = json.loads(await reader.readline())
                if not isinstance(request, dict):
                    raise ValueError("invalid_request")
                response = {"ok": True, "result": await self.dispatch(request)}
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Do not serialize request bodies, tokens, SQL or browser internals.
            response = {"ok": False, "error": type(exc).__name__}
        try:
            writer.write(json.dumps(response, ensure_ascii=False, default=str).encode() + b"\n")
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()


literature_access = LiteratureAccess()

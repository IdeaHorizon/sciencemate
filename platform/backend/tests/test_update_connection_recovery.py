"""Update recovery is bounded, explicit, authenticated, and preserves trust."""

import ssl
from collections import Counter

import httpx
import pytest

from app.config import settings
from app.services import release_feed as feed


def transport(monkeypatch, handler):
    client = httpx.Client
    options = []

    def create(**kwargs):
        options.append(kwargs)
        return client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(feed.httpx, "Client", create)
    return options


def test_a_reachable_list_does_not_hide_a_transient_manifest_timeout(monkeypatch):
    calls = Counter()

    def response(request):
        calls[request.url.path] += 1
        if request.url.path == "/releases":
            return httpx.Response(200, json=[])
        if calls[request.url.path] == 1:
            raise httpx.ReadTimeout("temporary", request=request)
        return httpx.Response(200, content=b"signed manifest bytes")

    transport(monkeypatch, response)
    assert feed.get_json("https://updates.test/releases") == []
    assert feed.get_bytes("https://updates.test/manifest.json") == b"signed manifest bytes"
    assert calls == {"/releases": 1, "/manifest.json": 2}


def test_direct_is_an_explicit_update_only_choice_with_the_same_tls_trust(monkeypatch):
    options = transport(monkeypatch, lambda request: httpx.Response(200, content=b"ok"))
    feed.get_bytes("https://updates.test/manifest.json")
    assert options[-1]["trust_env"] is True
    feed.write_connection_mode("direct")
    feed.get_bytes("https://updates.test/manifest.json")
    assert feed.connection_mode() == "direct"
    assert options[-1]["trust_env"] is False
    assert isinstance(options[-1]["verify"], ssl.SSLContext)
    assert options[-1]["verify"].verify_mode == ssl.CERT_REQUIRED
    assert options[-1]["verify"].check_hostname


def test_failure_names_the_stage_and_host_without_exposing_signed_urls(monkeypatch):
    attempts = []

    def response(request):
        attempts.append(request)
        raise httpx.ReadTimeout(
            "credentials must not be echoed " + str(request.url), request=request
        )

    transport(monkeypatch, response)
    with pytest.raises(RuntimeError) as failed:
        feed.get_bytes("https://login:password@updates.test/manifest.json.sig?signature=private")
    assert len(attempts) == 2
    text = str(failed.value)
    assert "signature" in text and "updates.test" in text and "ReadTimeout" in text
    assert "password" not in text and "private" not in text and "login" not in text


def test_a_partially_delivered_archive_is_never_replayed_into_the_same_sink(monkeypatch):
    class Interrupted(httpx.SyncByteStream):
        def __iter__(self):
            yield b"x" * (256 * 1024)
            raise httpx.ReadTimeout("interrupted")

    attempts = []

    def response(request):
        attempts.append(request)
        return httpx.Response(200, stream=Interrupted())

    transport(monkeypatch, response)
    received = bytearray()
    with pytest.raises(RuntimeError) as failed:
        feed.stream("https://updates.test/app.zip", received.extend)
    assert len(attempts) == 1
    assert len(received) == 256 * 1024
    assert failed.value.diagnostics["bytes_delivered"] == len(received)
    assert failed.value.diagnostics["stage"] == "archive"


def test_http_rejection_is_not_retried_as_a_connection_failure(monkeypatch):
    calls = []
    transport(monkeypatch, lambda request: calls.append(request) or httpx.Response(403))
    with pytest.raises(RuntimeError):
        feed.get_bytes("https://updates.test/manifest.json")
    assert len(calls) == 1


def test_a_deployment_setting_cannot_be_overridden_by_a_local_preference(monkeypatch):
    monkeypatch.setattr(settings, "update_connection_mode", "system")
    with pytest.raises(ValueError):
        feed.write_connection_mode("direct")
    assert feed.connection_mode() == "system"


@pytest.mark.asyncio
@pytest.mark.parametrize("personal", [True, False])
async def test_connection_choice_uses_the_authenticated_desktop_boundary(
    client, db_session, monkeypatch, personal
):
    from app.auth import get_current_user, implicit_local_user
    from app.main import app

    user = await implicit_local_user(db_session)

    async def current_user():
        return user

    app.dependency_overrides[get_current_user] = current_user
    monkeypatch.setattr(settings, "profile", "personal" if personal else "org")
    response = await client.put("/api/v1/update/connection", json={"mode": "direct"})
    assert response.status_code == (200 if personal else 403), response.text
    assert feed.connection_mode() == ("direct" if personal else "system")
    status = (await client.get("/api/v1/update")).json()
    assert status["connection_editable"] is personal

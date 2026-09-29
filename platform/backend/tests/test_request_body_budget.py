"""Chunked uploads consume the same bounded receive budget as declared bodies."""
import httpx
import pytest
from fastapi import FastAPI, Request, UploadFile
from app import middleware


@pytest.fixture
def upload_app(monkeypatch):
    monkeypatch.setattr(middleware.settings, "material_max_bytes", 128)
    monkeypatch.setattr(middleware, "_MULTIPART_ENVELOPE_ALLOWANCE", 0)
    app = FastAPI()
    app.add_middleware(middleware.RequestBodyCapMiddleware)

    @app.post("/body")
    async def body(request: Request):
        return {"size": len(await request.body())}

    @app.post("/multipart")
    async def multipart(file: UploadFile):
        return {"size": len(await file.read())}

    return app


@pytest.mark.asyncio
@pytest.mark.parametrize("declared", [False, True])
async def test_oversize_upload_stops_before_consuming_the_rest(upload_app, declared):
    chunks_read = []
    async def chunks():
        for i in range(5):
            chunks_read.append(i)
            yield b"x" * 64
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=upload_app), base_url="http://test") as client:
        response = await client.post("/body", content=chunks(), headers={"Content-Length": "320"} if declared else {})
    assert response.status_code == 413
    assert response.json()["detail"]["code"] == "request_body_too_large"
    assert chunks_read == ([] if declared else [0, 1, 2])


@pytest.mark.asyncio
async def test_chunked_body_at_the_limit_arrives_unchanged(upload_app):
    async def chunks():
        yield b"x" * 64
        yield b"y" * 64
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=upload_app), base_url="http://test") as client:
        response = await client.post("/body", content=chunks())
    assert response.status_code == 200
    assert response.json() == {"size": 128}


@pytest.mark.asyncio
async def test_chunked_multipart_is_rejected_during_parsing(upload_app):
    async def chunks():
        yield b'--qa\r\nContent-Disposition: form-data; name="file"; filename="data.bin"\r\n\r\n'
        yield b"x" * 128
        yield b"\r\n--qa--\r\n"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=upload_app), base_url="http://test") as client:
        response = await client.post("/multipart", content=chunks(), headers={"Content-Type": "multipart/form-data; boundary=qa"})
    assert response.status_code == 413
    assert response.json()["detail"]["maxBytes"] == 128

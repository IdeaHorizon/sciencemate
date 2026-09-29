"""问发布页要东西 —— 桌面自更新和组织服务器自升级共用这一处。

两者问的是**同一个发布页**、用**同一把签名钥匙**验（`self_update.parse_manifest`）、
私有仓库时带**同一个凭据**（`UPDATE_SOURCE_TOKEN`）。从前这几个函数只住在桌面的
`/update` 端点里；组织服务器也要自己升级之后，抄一份就是两份各自演化的答案。
"""
from __future__ import annotations

import base64

import httpx

TIMEOUT = httpx.Timeout(20.0, connect=10.0)


def auth_headers() -> dict[str, str]:
    """私有 Forgejo：`user:token` 走 Basic，裸 token 走 `token …`。空 = 匿名。"""
    from app.config import settings

    token = (settings.update_source_token or "").strip()
    if not token:
        return {}
    if ":" in token:
        return {"Authorization": "Basic " + base64.b64encode(token.encode()).decode()}
    return {"Authorization": f"token {token}"}


def get_json(url: str):
    with httpx.Client(timeout=TIMEOUT, follow_redirects=True, headers=auth_headers()) as client:
        response = client.get(url)
        response.raise_for_status()
        return response.json()


def get_bytes(url: str) -> bytes:
    with httpx.Client(timeout=TIMEOUT, follow_redirects=True, headers=auth_headers()) as client:
        response = client.get(url)
        response.raise_for_status()
        return response.content


def stream(url: str, sink) -> None:
    with httpx.Client(timeout=TIMEOUT, follow_redirects=True, headers=auth_headers()) as client:
        with client.stream("GET", url) as response:
            response.raise_for_status()
            for chunk in response.iter_bytes(1024 * 256):
                sink(chunk)

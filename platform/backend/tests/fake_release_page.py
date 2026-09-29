"""一个假的 Forgejo 发布页 —— 桌面查更新、组织服务器自升级、CI 的 server-compat 共用这一份。

形状照 2026-09-28 真读回来的 `wangd/sciencemate-pro`：列表新的在前、`draft` / `prerelease`
两个布尔、`assets[].name` + `browser_download_url`（Forgejo 按自己的 ROOT_URL 拼的对外地址）；
manifest 照 v0.5.3 真发出去的那份（带 extras，专业版带 server）。

只有**传上去了的**资产下得到，别的一律 404 —— 去下一个还不在的文件，就是 2026-09-27 那一下。
哪些 host 问得到列表由 `reachable` 定，下得到文件由 `files_on` 定（默认同 `reachable`）：
CI 走得通的那个 host、用户填的更新源、资产里记的对外地址，不一定是一个。
"""
from __future__ import annotations

import base64
import json
from urllib.parse import urlsplit

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.services import self_update as su

CI_HOST = "http://forgejo.ci:3000"
PUBLIC_HOST = "https://desktop-9el2944.taile9f15e.ts.net"
REPO = "wangd/sciencemate-pro"


def release_dir(version: str) -> list[str]:
    """一版专业版发布目录里的文件（v0.5.3 真发出去的那一份，外加 0.5.4 起的 org_wire.json）。"""
    return ["SHA256SUMS", f"ScienceMate-Pro-{version}-arm64.dmg", "ScienceMate-Pro-Setup.exe",
            f"extras-{version}.tar.gz", "install.ps1", "install.sh", "manifest.json", "manifest.json.sig",
            "org_wire.json", f"payload-{version}.tar.gz", f"sciencemate-server-{version}.tar.gz"]


def upload_order(version: str) -> list[str]:
    """发布器一个一个传的顺序（`publish_release.publish`：`sorted(release_dir.iterdir())`）。"""
    return sorted(release_dir(version))


def public_key_of(key: Ed25519PrivateKey) -> str:
    return base64.b64encode(key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()


class FakeReleasePage:
    def __init__(self, key: Ed25519PrivateKey, *, reachable: tuple[str, ...] = (PUBLIC_HOST,),
                 files_on: tuple[str, ...] | None = None) -> None:
        self.key = key
        self.reachable = reachable
        self.files_on = reachable if files_on is None else files_on
        self.releases: list[dict] = []
        self.files: dict[tuple[str, str], bytes] = {}      # (tag, 文件名) → 字节
        self.asked: list[str] = []

    def repo_url(self, host: str = PUBLIC_HOST) -> str:
        return f"{host}/{REPO}"

    def list_url(self, host: str = PUBLIC_HOST) -> str:
        return f"{host}/api/v1/repos/{REPO}/releases?limit={su.LOOK_BACK}&draft=false&pre-release=false"

    def publish(self, version: str, uploaded: list[str], *, prerelease: bool = False, draft: bool = False,
                with_a_server: bool = True, signed_by: Ed25519PrivateKey | None = None) -> dict:
        tag = f"v{version}"
        manifest = {"schema": 1, "version": version, "edition": "pro", "archive": f"payload-{version}.tar.gz",
                    "sha256": "0" * 64, "size": 1, "unit": list(su.UNIT), "notes": f"{version} 的说明",
                    "extras": {"archive": f"extras-{version}.tar.gz", "sha256": "2" * 64, "size": 1,
                               "units": ["app"], "files": {"app": 1}}}
        if with_a_server:
            manifest["server"] = {"archive": f"sciencemate-server-{version}.tar.gz", "sha256": "1" * 64,
                                  "size": 1, "version": version, "org_protocol": 1}
        data = json.dumps(manifest).encode()
        contents = {"manifest.json": data,
                    "manifest.json.sig": base64.b64encode((signed_by or self.key).sign(data))}
        for name in uploaded:
            self.files[(tag, name)] = contents.get(name, name.encode())
        release = {
            "tag_name": tag, "draft": draft, "prerelease": prerelease,
            "html_url": f"{PUBLIC_HOST}/{REPO}/releases/tag/{tag}",
            "assets": [{"name": name,
                        "browser_download_url": f"{PUBLIC_HOST}/{REPO}/releases/download/{tag}/{name}"}
                       for name in uploaded]}
        self.releases.insert(0, release)        # 后建的排前面 —— Forgejo 按建立时间倒序给
        return manifest

    def get_json(self, url: str):
        self.asked.append(url)
        if url not in {self.list_url(host) for host in self.reachable}:
            raise AssertionError(f"问了发布页列表以外的地址：{url}")
        return json.loads(json.dumps(self.releases))

    def get_bytes(self, url: str) -> bytes:
        self.asked.append(url)
        parts = urlsplit(url)
        prefix = f"/{REPO}/releases/download/"
        if f"{parts.scheme}://{parts.netloc}" in self.files_on and parts.path.startswith(prefix):
            tag, _, name = parts.path[len(prefix):].partition("/")
            if (tag, name) in self.files:
                return self.files[(tag, name)]
        httpx.Response(404, request=httpx.Request("GET", url)).raise_for_status()
        raise AssertionError("unreachable")

    def stream(self, url: str, sink) -> None:
        sink(self.get_bytes(url))

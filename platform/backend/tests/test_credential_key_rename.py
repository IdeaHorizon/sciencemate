"""钥匙串服务名与 .app 的 BUNDLE_ID 必须是同一个串。

钥匙串条目按服务名取，而 .app 的身份是 BUNDLE_ID —— 它们本来就是一件事。分开写
两份，改名时改了一处没改另一处，结果是**主密钥在新名下找不到 → 重新生成一把 →
已加密的 API key 再也解不开**，而且全程不报错。
"""
from __future__ import annotations

import importlib.util
import pathlib

from app.services import credential_key as ck


def test_the_service_name_matches_the_bundle_id() -> None:
    repo = pathlib.Path(__file__).resolve().parents[3]
    spec = importlib.util.spec_from_file_location(
        "afs_pkg_mac", repo / "scripts" / "package" / "build_mac_app.py")
    mac = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mac)

    assert ck._KEYCHAIN_SERVICE == mac.BUNDLE_ID

"""发布签名密钥 —— 自更新的信任根。

## 为什么必须有

自更新 = 应用从网上下一份代码然后跑它。没有签名，任何能改 DNS / 劫持 HTTP /
拿到发布服务器的人，都得到一条对每台装了这个应用的机器的远程执行通道。所以
**没有签名的载荷客户端一律拒绝**，不给"先跑着"这条路。

## 形状

ed25519（`cryptography` 已随包，零新依赖）。私钥在发布机上：
`~/.harness-framework/release-signing/ed25519.key`，0600，base64 的 32 字节种子。
公钥烧进应用：`platform/backend/app/release_pubkey.py`。

线上格式（与 `app/services/self_update.py` 的 verify 一字不差，有交叉测试钉住）：
  - 公钥：base64(raw 32 bytes)
  - 签名：base64(ed25519_sign(manifest.json 的**精确字节**))

## 丢了私钥怎么办

已装出去的应用只认这把公钥。私钥没了 = 再也发不出它们认的更新，只能让每个人
重装。所以 `generate()` 默认**拒绝覆盖**已有的私钥，且这个文件要备份。
"""
from __future__ import annotations

import base64
import os
from pathlib import Path



def _ed25519():
    """`cryptography` 只在真要签/验的时候才导入。

    打包器跑在构建机的系统 python3 上，那台机器不一定装了 cryptography ——
    缺它该挡住的是**签名**这一步（吵着挡），不是整个打包。
    """
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import (
            Ed25519PrivateKey,
            Ed25519PublicKey,
        )
    except ImportError as exc:
        raise SystemExit(
            "签发布载荷需要 `cryptography`（pip install cryptography）。"
            "它只在签名/验签时用到 —— 不签就不需要。"
        ) from exc
    return serialization, Ed25519PrivateKey, Ed25519PublicKey


def key_dir() -> Path:
    home = os.environ.get("HARNESS_FRAMEWORK_HOME", "~/.harness-framework")
    return Path(home).expanduser() / "release-signing"


def private_key_path() -> Path:
    return key_dir() / "ed25519.key"


def generate(*, force: bool = False) -> str:
    """造一对密钥，私钥落盘 0600，返回公钥 base64。已有私钥时拒绝覆盖。"""
    serialization, Ed25519PrivateKey, _ = _ed25519()
    path = private_key_path()
    if path.exists() and not force:
        raise FileExistsError(
            f"{path} 已存在。覆盖它 = 已装出去的应用再也收不到更新，只能重装。"
            f"确定要换钥匙就传 force=True，并同时更新 release_pubkey.py。"
        )
    key = Ed25519PrivateKey.generate()
    raw = key.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as handle:
        handle.write(base64.b64encode(raw))
    return public_key_b64(key)


def load_private():
    """没有私钥 → None（未签名路径）。有私钥但没 cryptography → 吵着退出。"""
    path = private_key_path()
    if not path.is_file():
        return None
    _, Ed25519PrivateKey, _ = _ed25519()
    raw = base64.b64decode(path.read_bytes().strip())
    return Ed25519PrivateKey.from_private_bytes(raw)


def public_key_b64(key) -> str:
    serialization, _, _ = _ed25519()
    raw = key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return base64.b64encode(raw).decode("ascii")


def sign(data: bytes, key) -> str:
    return base64.b64encode(key.sign(data)).decode("ascii")


def verify(public_b64: str, data: bytes, signature_b64: str) -> bool:
    try:
        _, _, Ed25519PublicKey = _ed25519()
        public = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_b64))
        public.verify(base64.b64decode(signature_b64), data)
        return True
    except Exception:
        return False


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "generate":
        force = "--force" in sys.argv
        pub = generate(force=force)
        print(f"私钥：{private_key_path()}（备份它）")
        print(f"公钥（写进 platform/backend/app/release_pubkey.py）：{pub}")
    else:
        key = load_private()
        print(public_key_b64(key) if key else "(没有私钥；先跑 generate)")

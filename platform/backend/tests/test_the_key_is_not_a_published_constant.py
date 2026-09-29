"""模型凭据不许用源码里那个常量加密，数据根不许世界可读。

## 事实（2026-09-06 实测）

个人档的主密钥是 `sha256("dev-secret-key-change-in-production")` —— `assembly`
只对组织档要求换掉 `secret_key`（那条判断是对的：个人档不签发 token）。于是
**每一份个人版安装，都用仓库里同一个常量加密所有人的 API key**。我拿那个字面量
把真机上的 key 解开了（只验证、没打印）。叠加文件权限：

    drwxr-xr-x  ~/.harness-framework
    -rw-r--r--  ~/.harness-framework/db.sqlite

世界可读。同机器上任何别的用户、任何有磁盘访问权的程序、任何把 home 纳入范围的
备份/同步/MDM，拿到的都是可解密的密文。而首次运行向导上写着「key 只存在这台
机器上」—— 就传输而言是真的，读的人会理解成"存得安全"。

## 判据钉住三件事

1. 新写入的密文，**用出厂常量解不开**；
2. 已经存着的弱密文，启动时被**真的换掉**（"以后新写的安全了"不是修复）；
3. 数据根与库文件是私有的。
"""
from __future__ import annotations

import base64
import hashlib
import os
import stat
import uuid
from pathlib import Path

import pytest
from cryptography.fernet import Fernet, InvalidToken

from app.config import settings
from app.services import model_backends

_THE_PUBLISHED_CONSTANT = "dev-secret-key-change-in-production"


def _the_published_constant_fernet() -> Fernet:
    return Fernet(base64.urlsafe_b64encode(
        hashlib.sha256(_THE_PUBLISHED_CONSTANT.encode()).digest()))


@pytest.fixture()
def a_personal_install(tmp_path, monkeypatch):
    """一份干净的个人档安装：数据根在 tmp，密钥落文件（不碰真钥匙串）。"""
    monkeypatch.setattr(settings, "profile", "personal")
    monkeypatch.setattr(settings, "platform_data_root", str(tmp_path))
    monkeypatch.setattr(settings, "secret_key", _THE_PUBLISHED_CONSTANT)
    from app.services import credential_key

    # 测试不该往用户的真钥匙串里写东西 —— 那是这台机器上的持久副作用。
    monkeypatch.setattr(credential_key, "_keychain_is_available", lambda: False)
    return tmp_path


def test_a_fresh_install_does_not_use_the_published_constant(a_personal_install) -> None:
    """新装的机器上，密文用出厂常量**解不开**。"""
    ciphertext = model_backends.encrypt_api_key("sk-a-real-looking-secret")
    with pytest.raises(InvalidToken):
        _the_published_constant_fernet().decrypt(ciphertext.encode())
    # 而它自己解得开 —— 换密钥不能把凭据变成"再也拿不回来"
    assert model_backends.decrypt_api_key(ciphertext) == "sk-a-real-looking-secret"


def test_two_installs_do_not_share_a_key(a_personal_install, tmp_path, monkeypatch) -> None:
    """两份安装各有各的密钥 —— 否则拿到一台就等于拿到所有。"""
    first = model_backends.encrypt_api_key("sk-one")

    other_root = tmp_path.parent / f"other-{uuid.uuid4().hex[:8]}"
    other_root.mkdir()
    monkeypatch.setattr(settings, "platform_data_root", str(other_root))
    second_key_ciphertext = model_backends.encrypt_api_key("sk-one")

    assert first != second_key_ciphertext
    with pytest.raises(RuntimeError):
        model_backends.decrypt_api_key(first)


def test_the_key_file_is_not_readable_by_anyone_else(a_personal_install) -> None:
    """退回文件时，那个文件是 0600。

    先建成 0600 再写 —— 先写后 chmod 之间有一个别人读得到的窗口。
    """
    model_backends.encrypt_api_key("sk-whatever")
    key_file = Path(a_personal_install) / "credential.key"
    assert key_file.is_file()
    mode = stat.S_IMODE(key_file.stat().st_mode)
    assert mode == 0o600, f"密钥文件是 {oct(mode)}"


def test_a_legacy_ciphertext_is_still_readable(a_personal_install) -> None:
    """换密钥之前存进去的，还解得开 —— 否则对用户就是"我的 key 没了"。"""
    legacy = _the_published_constant_fernet().encrypt(b"sk-stored-long-ago").decode()
    assert model_backends.decrypt_api_key(legacy) == "sk-stored-long-ago"
    assert model_backends.was_encrypted_with_the_published_constant(legacy) is True


@pytest.mark.asyncio
async def test_the_stored_weak_ciphertext_is_actually_replaced(
    db_session, a_personal_install
) -> None:
    """存量**真的被换掉**，不是只对新写入生效。

    这条是这个 PR 的要害：只改新写入的话，用户已经存进去的那把 —— 也就是唯一
    真正需要保护的那把 —— 会一直用公开常量躺在库里。
    """
    from app.models.model_backend import ModelBackendConfig

    legacy = _the_published_constant_fernet().encrypt(b"sk-the-users-real-key").decode()
    row = ModelBackendConfig(
        scope_kind="personal", scope_id=str(uuid.uuid4()), provider="deepseek", model="m",
        base_url="http://x", display_name="d", credential_source="encrypted",
        encrypted_api_key=legacy, created_by_user_id=str(uuid.uuid4()),
    )
    db_session.add(row)
    await db_session.flush()

    rekeyed = await model_backends.rekey_legacy_credentials(db_session)

    assert rekeyed == 1
    assert row.encrypted_api_key != legacy
    assert model_backends.was_encrypted_with_the_published_constant(
        row.encrypted_api_key) is False
    # 换完之后内容一字不差
    assert model_backends.decrypt_api_key(row.encrypted_api_key) == "sk-the-users-real-key"


@pytest.mark.asyncio
async def test_rekeying_is_idempotent(db_session, a_personal_install) -> None:
    """已经换过的不再换 —— 每次启动都重写一遍等于每次都动一次凭据。"""
    from app.models.model_backend import ModelBackendConfig

    row = ModelBackendConfig(
        scope_kind="personal", scope_id=str(uuid.uuid4()), provider="deepseek", model="m",
        base_url="http://x", display_name="d", credential_source="encrypted",
        encrypted_api_key=model_backends.encrypt_api_key("sk-already-fine"),
        created_by_user_id=str(uuid.uuid4()),
    )
    db_session.add(row)
    await db_session.flush()

    assert await model_backends.rekey_legacy_credentials(db_session) == 0


def test_the_data_root_is_private(a_personal_install, monkeypatch) -> None:
    """数据根 0700、库文件 0600 —— 包括**已经存在**的那些。

    umask 只管新建的；一台已经装过的机器上那些目录是宽的，装配时要收回来。
    """
    root = Path(a_personal_install)
    os.chmod(root, 0o755)
    (root / "db.sqlite").write_text("x", encoding="ascii")
    os.chmod(root / "db.sqlite", 0o644)

    from app import assembly

    assembly.prepare_the_data_root()

    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert stat.S_IMODE((root / "db.sqlite").stat().st_mode) == 0o600
    assert os.umask(0o022) == 0o077, "进程 umask 没有被收紧 —— 之后新建的文件仍是宽的"
    os.umask(0o077)

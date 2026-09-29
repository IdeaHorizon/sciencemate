"""加密存储凭据用的主密钥 —— 它从哪儿来。

## 问题

模型的 API key 存在库里，用 Fernet 加密。密钥从哪儿来？原来是
``sha256(settings.secret_key)``，而个人档的 ``secret_key`` **一直是出厂值**
（`assembly.install()` 只对组织档要求换掉它，那里的理由是对的：个人档不签发
token，为一件不存在的事要求配密钥就是在教育用户）。

后果：**每一份个人版安装，都用源码里同一个常量加密所有人的 key。**
2026-09-06 实测，我拿仓库里那个字面量把真机上的 key 解开了。这是混淆，不是
加密 —— 密钥在公开仓库里躺着。

## 第一性原理

「把一把密钥安全地放在这台机器上」是操作系统**已经有专门部件**在做的事。
自己造一个存法，最好的结果也只是把它重做一遍，通常更差。所以：

- **有钥匙串就用钥匙串**（macOS 的 `security`，系统自带，无新依赖）。密钥受
  用户登录态保护，别的用户读不到，备份/同步工具也带不走。
- **没有就退回文件**，`0600`，放在数据根里，内容是随机的。这**挡不住**能读
  整个 home 的人 —— 但它把"任何读得到源码的人"挡在门外，相对现状是数量级的
  差别。退回时如实记下用了哪种，不假装。

组织档不走这条路：那里的 ``SECRET_KEY`` 由运维配置且必须换掉，多个进程/多台
机器要能算出同一把密钥 —— 那正是"由操作者管理的密钥"该有的样子。

## 换了密钥，旧密文怎么办

`decrypt_api_key` 会依次试当前密钥与**出厂密钥**；启动时跑一次
`rekey_legacy_credentials()`，把还用出厂密钥的行重新加密成当前密钥。不这么做的
话，那些弱密文会一直躺在库里 —— "以后新写的安全了"不是修复。
"""
from __future__ import annotations

import base64
import hashlib
import os
import secrets
import shutil
import subprocess
from pathlib import Path

from app.config import settings

#: 出厂密钥。这是**已经泄露**的那一把：它就在仓库里。留着它只为一件事 ——
#: 把用它加密过的存量解开、换成当前密钥。任何新写入都不许用它。
_THE_PUBLISHED_CONSTANT = "dev-secret-key-change-in-production"

#: 钥匙串里这条记录的身份。改名 = 换一把密钥 = 所有存量解不开，所以它是常量。
#: 与 .app 的 BUNDLE_ID 是同一个串（钥匙串条目按服务名取）——
#: 两边分叉就是一个问题两个答案，由 test_credential_key_service_name 钉住。
_KEYCHAIN_SERVICE = "com.ieit.sciencemate"
_KEYCHAIN_ACCOUNT = "model-credential-key"

_KEY_FILE_NAME = "credential.key"


def _as_fernet_key(secret: bytes) -> bytes:
    return base64.urlsafe_b64encode(hashlib.sha256(secret).digest())


def the_published_constant_key() -> bytes:
    """出厂密钥派生出来的那把 —— **只用于解开存量**。"""
    return _as_fernet_key(_THE_PUBLISHED_CONSTANT.encode())


# ── 钥匙串 ────────────────────────────────────────────────────────────────────


#: 问钥匙串一句最多等多久。
#:
#: 它要么立刻答，要么这个上下文里就用不了它：锁着的钥匙串、SSH 进来的终端、
#: 没有图形会话的守护进程 —— `security` 会停在那儿等一个永远没人点的对话框。
#: 原来这里写的是 20 秒，代价是**保存 API key 卡 20 秒然后 500**（2026-09-07
#: 实测，一次跑批里 24 条测试全挂在这上面）。
#:
#: 三秒的依据：本机命中是毫秒级；超过这个数量级就不是"慢"，是在等人。
_KEYCHAIN_PATIENCE_S = 3.0


def _keychain_is_available() -> bool:
    return shutil.which("security") is not None


def _ask_the_keychain(argv: list[str]) -> subprocess.CompletedProcess | None:
    """问钥匙串一句。**答不上来一律当作"用不了"**，不往上抛。

    钥匙串在这里是增强项，不是前提 —— 模块开头那段写着"没有就退回文件"。
    既然如此，"它没答"和"它不在"就必须走同一条路：任何别的处理方式，都是让
    一个本来有退路的场景变成用户面前的一次失败。

    捕获面按**这个调用会怎么失败**取，不按"我想到了哪几种"：超时、命令在
    `which` 之后消失、权限不足 —— 它们对调用方是同一件事。
    """
    try:
        return subprocess.run(
            argv, capture_output=True, text=True, timeout=_KEYCHAIN_PATIENCE_S)
    except (subprocess.SubprocessError, OSError):
        return None


def _keychain_read() -> str | None:
    done = _ask_the_keychain(
        ["security", "find-generic-password",
         "-s", _KEYCHAIN_SERVICE, "-a", _KEYCHAIN_ACCOUNT, "-w"])
    if done is None or done.returncode != 0:
        return None
    value = done.stdout.strip()
    return value or None


def _keychain_write(secret: str) -> bool:
    done = _ask_the_keychain(
        ["security", "add-generic-password",
         "-s", _KEYCHAIN_SERVICE, "-a", _KEYCHAIN_ACCOUNT, "-w", secret, "-U"])
    return done is not None and done.returncode == 0


# ── 文件 ─────────────────────────────────────────────────────────────────────


def _key_file() -> Path:
    return Path(settings.platform_data_root).expanduser() / _KEY_FILE_NAME


def _file_read() -> str | None:
    path = _key_file()
    if not path.is_file():
        return None
    value = path.read_text(encoding="ascii").strip()
    return value or None


def _file_write(secret: str) -> None:
    path = _key_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    # 先建成 0600 再写：先写后 chmod 之间存在一个别人读得到的窗口。
    handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(handle, "w", encoding="ascii") as file:
        file.write(secret)
    os.chmod(path, 0o600)


# ── 对外 ─────────────────────────────────────────────────────────────────────


def where_the_key_lives() -> str:
    """密钥存在哪 —— 给 doctor / 日志用的一句人话，也是"没假装"的凭据。"""
    from app import assembly

    if assembly.the_credential_key_is_operator_managed():
        return "operator-configured SECRET_KEY"
    if _keychain_is_available() and _keychain_read() is not None:
        return "OS keychain"
    if _file_read() is not None:
        return f"{_key_file()} (0600)"
    return "(not created yet)"


def master_key() -> bytes:
    """这份安装用来加密凭据的主密钥。

    组织档：由运维配置的 ``SECRET_KEY`` 派生 —— 多进程/多机要算出同一把。
    个人档：本机自己的一把随机密钥，优先放钥匙串，否则 0600 文件。
    """
    from app import assembly

    if assembly.the_credential_key_is_operator_managed():
        return _as_fernet_key(settings.secret_key.encode())

    existing = None
    if _keychain_is_available():
        existing = _keychain_read()
    if existing is None:
        existing = _file_read()
    if existing is not None:
        return _as_fernet_key(existing.encode())

    minted = secrets.token_urlsafe(48)
    if not (_keychain_is_available() and _keychain_write(minted)):
        # 钥匙串写不进去（没有登录态、或这个平台没有）——退回文件并如实记下。
        _file_write(minted)
    return _as_fernet_key(minted.encode())

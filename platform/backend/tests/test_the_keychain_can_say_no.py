"""钥匙串答不上来的时候，凭据照样存得下。

## 病例（2026-09-07）

`credential_key` 里两处 `subprocess.run(..., timeout=20)` 没有 except。
`security` 在锁着的钥匙串 / 没有图形会话 / SSH 终端上会停下来等一个永远没人点
的对话框 —— 20 秒后 `TimeoutExpired` 一路抛到 API：

    ERROR app.http POST /api/v1/settings/model-backends → 500 (20024ms)

用户填完 API key 点保存，转 20 秒，然后是一个"内部错误"。同一次跑批里 24 条
测试全挂在这上面。

## 判据

模块开头写着「有钥匙串就用钥匙串，没有就退回文件」。既然钥匙串是**增强项**，
那"它没答"和"它不在"就必须走同一条路 —— 否则一个本来有退路的场景，会变成用户
面前的一次失败。
"""
from __future__ import annotations

import subprocess

import pytest

from app.services import credential_key


def _the_keychain_hangs(*_args, **_kwargs):
    raise subprocess.TimeoutExpired(cmd="security", timeout=3.0)


def _the_keychain_vanished(*_args, **_kwargs):
    raise FileNotFoundError("security")


@pytest.mark.parametrize("failure", [_the_keychain_hangs, _the_keychain_vanished],
                         ids=["hangs", "vanished"])
def test_a_key_is_still_minted(monkeypatch, tmp_path, failure) -> None:
    """钥匙串卡住/消失 → 照样拿得到一把密钥，并且它落在文件上。"""
    monkeypatch.setattr(credential_key.settings, "platform_data_root", str(tmp_path))
    monkeypatch.setattr(credential_key.shutil, "which", lambda _name: "/usr/bin/security")
    monkeypatch.setattr(credential_key.subprocess, "run", failure)

    key = credential_key.master_key()
    assert isinstance(key, bytes) and key
    assert (tmp_path / "credential.key").is_file(), "没退回文件 —— 那这把密钥下次就对不上了"
    assert (tmp_path / "credential.key").stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("failure", [_the_keychain_hangs, _the_keychain_vanished],
                         ids=["hangs", "vanished"])
def test_the_same_key_comes_back(monkeypatch, tmp_path, failure) -> None:
    """退回文件之后要**稳定**：第二次问必须还是那一把。

    不然每次重启都换一把密钥，库里所有已存的凭据当场作废 —— 那比报错更糟，
    因为它安安静静。
    """
    monkeypatch.setattr(credential_key.settings, "platform_data_root", str(tmp_path))
    monkeypatch.setattr(credential_key.shutil, "which", lambda _name: "/usr/bin/security")
    monkeypatch.setattr(credential_key.subprocess, "run", failure)

    assert credential_key.master_key() == credential_key.master_key()


def test_it_says_where_the_key_actually_lives(monkeypatch, tmp_path) -> None:
    """`where_the_key_lives()` 不许假装用了钥匙串。"""
    monkeypatch.setattr(credential_key.settings, "platform_data_root", str(tmp_path))
    monkeypatch.setattr(credential_key.shutil, "which", lambda _name: "/usr/bin/security")
    monkeypatch.setattr(credential_key.subprocess, "run", _the_keychain_hangs)

    credential_key.master_key()
    assert str(tmp_path) in credential_key.where_the_key_lives()
    assert "keychain" not in credential_key.where_the_key_lives().lower()


def test_the_wait_is_short_enough_to_be_a_save_button(monkeypatch) -> None:
    """等待上限得配得上"点了保存"这个动作。

    这条钉的是**出厂值**：上面那些用例把 `subprocess.run` 换成了替身，等多久
    它们一概看不见 —— 有人把耐心改回 20 秒，它们一条都不会红。
    """
    assert credential_key._KEYCHAIN_PATIENCE_S <= 5.0, (
        f"钥匙串要等 {credential_key._KEYCHAIN_PATIENCE_S}s —— 用户点了保存之后"
        "盯着转圈，而这段时间里钥匙串多半是在等一个没人点的对话框"
    )


def test_every_keychain_call_goes_through_the_one_that_catches(monkeypatch, tmp_path) -> None:
    """所有对 `security` 的调用都经过会兜底的那一个入口。

    判据落在**行为**上而不是源码长相：把兜底入口换成"总是失败"，读和写就都
    必须表现为"钥匙串用不了"。哪条路绕过去了，这里就看得见。
    """
    monkeypatch.setattr(credential_key.settings, "platform_data_root", str(tmp_path))
    monkeypatch.setattr(credential_key.shutil, "which", lambda _name: "/usr/bin/security")
    monkeypatch.setattr(credential_key, "_ask_the_keychain", lambda _argv: None)

    assert credential_key._keychain_read() is None
    assert credential_key._keychain_write("x") is False
